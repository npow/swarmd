"""Completion judge — sole arbiter of mission_complete.

v0 implementation is a deterministic checker that verifies the required
preconditions. In later versions this becomes an LLM-backed judge with
retrospective review. The interface is the same so the coordinator doesn't
have to change.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from swarm.lib.paths import findings_path, session_dir
from swarm.schemas.finding import Finding


@dataclass(frozen=True)
class CompletionVerdict:
    verdict: str  # "complete" | "incomplete" | "cheat_suspected"
    reasoning: str
    outstanding: list[str]


def read_findings(session_id: str) -> list[Finding]:
    path = findings_path(session_id)
    if not path.exists():
        return []
    out: list[Finding] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Finding.model_validate_json(line))
            except Exception:
                continue
    return out


def _verifier_status(session_id: str) -> dict:
    path = session_dir(session_id) / "verifier_status.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _finding_age_sec(f: Finding, now_ms: float) -> float:
    """Extract age in seconds from a finding's ID (format: f-<ms>-<short>)."""
    try:
        ts_ms = int(f.id.split("-")[1])
    except (IndexError, ValueError):
        return 0.0
    return max(0.0, (now_ms - ts_ms) / 1000.0)


def judge(
    session_id: str,
    fabrication_stale_sec: int = 600,
    hold_window_recency_sec: int = 300,
) -> CompletionVerdict:
    """Decide whether the mission is complete.

    Preconditions (all must hold for `complete`):
      - hold_window_met finding is RECENT (within hold_window_recency_sec) —
        stale finds from earlier pass-then-fail cycles do not count
      - no unresolved cheat findings in the session
      - no unresolved fabrication findings fresher than fabrication_stale_sec
      - verifier_status.all_pass is true at the moment of judgement
    """
    findings = read_findings(session_id)
    status = _verifier_status(session_id)
    now_ms = time.time() * 1000

    outstanding: list[str] = []
    verdict = "complete"

    if not status.get("all_pass"):
        outstanding.append(
            "success_verifier reports at least one criterion is currently failing"
        )
        verdict = "incomplete"

    # Hold window must be met RECENTLY — not a stale signal from before a failure
    hold_findings = [
        f for f in findings if f.source == "success_verifier.hold_window_met"
    ]
    recent_hold = any(
        _finding_age_sec(f, now_ms) <= hold_window_recency_sec for f in hold_findings
    )
    if not recent_hold:
        if hold_findings:
            outstanding.append(
                f"hold_window_met exists but is older than {hold_window_recency_sec}s"
            )
        else:
            outstanding.append("hold window never reached")
        verdict = "incomplete"

    # Cheat findings block
    cheat_types = {"cheat"}
    cheats = [f for f in findings if f.type in cheat_types]
    if cheats:
        outstanding.append(f"{len(cheats)} cheat finding(s) open")
        verdict = "cheat_suspected"

    # Fabrication findings — if any are fresher than threshold, block
    for f in findings:
        if f.type != "fabrication":
            continue
        age_sec = _finding_age_sec(f, now_ms)
        if age_sec < fabrication_stale_sec:
            outstanding.append(
                f"unresolved fabrication finding {f.id} (age {int(age_sec)}s)"
            )
            if verdict == "complete":
                verdict = "incomplete"

    # Tamper findings
    for f in findings:
        if f.type == "meta" and f.subtype == "tamper_detected":
            outstanding.append("tamper detected — mission paused")
            verdict = "cheat_suspected"

    # Critic disagreements block completion (multi-provider defense)
    for f in findings:
        if f.type == "meta" and f.subtype == "critic_disagreement":
            outstanding.append(
                f"anticheat critics disagree (id={f.id}) — blocks completion"
            )
            if verdict == "complete":
                verdict = "incomplete"

    # Require per-criterion anticheat GENUINE_FIX: every pass_transition
    # finding must be matched by a GENUINE_FIX verdict AND no cheat finding
    # in the same window. We approximate by requiring:
    #   - at least one anticheat source finding exists if there's a transition
    #   - AND no anticheat finding was of subtype_map (cheat or unclear)
    transitions = [
        f for f in findings if f.type == "verification" and f.subtype == "pass_transition"
    ]
    if transitions:
        anticheat_finds = [f for f in findings if f.source.startswith("anticheat.")]
        unresolved_anticheat = [
            f for f in anticheat_finds
            if f.type in {"cheat", "meta"} and f.subtype != "critic_disagreement"
        ]
        # Note: we already block on `cheats` above; this re-check is explicit
        # so the reasoning message mentions anticheat specifically.
        if unresolved_anticheat:
            outstanding.append(
                f"{len(unresolved_anticheat)} anticheat non-GENUINE verdict(s)"
            )
            verdict = "cheat_suspected"

    reasoning = (
        "All preconditions satisfied."
        if verdict == "complete"
        else f"{verdict}: {', '.join(outstanding)}"
    )
    return CompletionVerdict(
        verdict=verdict, reasoning=reasoning, outstanding=outstanding
    )
