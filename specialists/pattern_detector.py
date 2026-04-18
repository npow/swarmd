"""Pattern detector — emits loop / oscillation / thrash findings.

Deterministic. No LLM. Polls events.jsonl every 10s in daemon mode; also
exposes a pure function `detect_once(events)` that unit tests can drive.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections import Counter
from pathlib import Path

from swarm.lib.heartbeat import beat
from swarm.lib.ids import mint_finding_id
from swarm.lib.launcher_liveness import exit_if_launcher_dead
from swarm.lib.locking import write_line
from swarm.lib.paths import (
    ensure_session_dirs,
    findings_path,
    mission_yaml_path,
)
from swarm.schemas.event import Event
from swarm.schemas.finding import Evidence, Finding
from swarm.schemas.mission import Mission, PatternThresholds

LOG = logging.getLogger("swarm.pattern_detector")


def normalize_arg(s: str | None) -> str:
    if s is None:
        return ""
    # whitespace, trailing slashes, quote-style normalization
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip("/")
    s = s.replace("'", '"')
    return s


def detect_loops(
    events: list[Event], thresholds: PatternThresholds
) -> list[Finding]:
    if not events:
        return []
    window = events[-thresholds.loop_window_events :]
    # count (tool_name, normalized_input) occurrences
    counts: Counter[tuple[str, str]] = Counter()
    for ev in window:
        if ev.tool_name is None:
            continue
        key = (ev.tool_name, normalize_arg(ev.tool_input_summary))
        counts[key] += 1
    findings: list[Finding] = []
    for (tool, norm_input), n in counts.items():
        if n >= thresholds.loop_repeat_count:
            cited = [
                ev.id
                for ev in window
                if ev.tool_name == tool
                and normalize_arg(ev.tool_input_summary) == norm_input
            ]
            findings.append(
                Finding(
                    id=mint_finding_id(),
                    source="pattern_detector.loop",
                    subject_session=events[-1].session_id,
                    spawner_id=events[-1].spawner_id,
                    type="loop",
                    subtype="repeat_exact_args",
                    severity="major",
                    cited_events=cited,
                    evidence=Evidence(
                        tool_calls=cited,
                        claim_excerpt=f"{tool}({norm_input[:200]})",
                    ),
                    verdict=f"tool={tool} repeated {n} times",
                )
            )
    return findings


def detect_oscillation(
    events: list[Event], thresholds: PatternThresholds
) -> list[Finding]:
    """A file whose content_hash returns to a prior state ≥N times is oscillating.

    Requires content_hash to have been embedded in tool_response_summary by the
    hook — v0 convention: for Edit/Write, summary has `content_hash=<hex>`.
    """
    if not events:
        return []
    window = events[-thresholds.oscillation_window_events :]
    per_file_hashes: dict[str, list[str]] = {}
    per_file_events: dict[str, list[str]] = {}
    for ev in window:
        if ev.tool_name not in {"Edit", "Write"}:
            continue
        m = re.search(r"file=([^\s;]+)", ev.tool_input_summary or "")
        h = re.search(r"content_hash=([a-f0-9]+)", ev.tool_response_summary or "")
        if not m or not h:
            continue
        file = m.group(1)
        hsh = h.group(1)
        per_file_hashes.setdefault(file, []).append(hsh)
        per_file_events.setdefault(file, []).append(ev.id)
    findings: list[Finding] = []
    for file, hashes in per_file_hashes.items():
        reverts = 0
        seen: set[str] = set()
        for i, prior_hash in enumerate(hashes):
            if prior_hash in seen and (i == 0 or hashes[i - 1] != prior_hash):
                reverts += 1
            seen.add(prior_hash)
        if reverts >= thresholds.oscillation_revert_count:
            findings.append(
                Finding(
                    id=mint_finding_id(),
                    source="pattern_detector.oscillation",
                    subject_session=events[-1].session_id,
                    spawner_id=events[-1].spawner_id,
                    type="thrash",
                    subtype="oscillation",
                    severity="major",
                    cited_events=per_file_events[file],
                    evidence=Evidence(files=[file]),
                    verdict=f"file={file} reverted {reverts} times",
                )
            )
    return findings


def detect_once(events: list[Event], mission: Mission) -> list[Finding]:
    """Run every detector once over `events`. Pure function for tests."""
    thresholds = mission.observer_config.pattern_thresholds
    out: list[Finding] = []
    out.extend(detect_loops(events, thresholds))
    out.extend(detect_oscillation(events, thresholds))
    return out


# -------- scope-shrinking detector (reads transcript, not events) --------

# Phrases agents use to unilaterally narrow mission scope while declaring done.
# Running them in a case-insensitive regex; anchor on word boundaries to avoid
# false positives in code comments (e.g., "out of scope for this PR" in a
# LICENSE file is still suspicious, but `/* scoped */` is not).
_SCOPE_SHRINK_PATTERNS = [
    # Explicit scope reduction
    r"\bout\s+of\s+scope\b",
    r"\bbeyond\s+(the\s+)?scope\b",
    r"\bnot\s+in\s+(the\s+)?scope\b",
    r"\bdeferred\s+to\s+(later|future|v\d|a\s+later|the\s+next)\b",
    r"\bleaving\s+(this|that|it)\s+for\s+(later|future|v\d)\b",
    r"\bskip(ping)?\s+(this\s+)?for\s+now\b",
    r"\bwill\s+not\s+implement\b",
    r"\bfor\s+future\s+work\b",
    r"\b(remaining|pending)\s+roadmap\b",
    r"\bexplicitly\s+out\s+of\s+scope\b",
    r"\bnot\s+included\s+in\s+this\s+(run|session|pass|phase)\b",
    r"\b(post(-|\s)?v0|post(-|\s)?v\d|next\s+version)\b",
]

# Deflection patterns: agent declares done implicitly by punting back to the
# human ("you tell me what's next"). Equally a stop-intent signal — the agent
# is declining to autonomously pick a next step.
_DEFLECTION_PATTERNS = [
    r"\b(let\s+me\s+know|tell\s+me)\s+(which|what|if|when)\b",
    r"\bif\s+you\s+(want|need|would\s+like)\s+(me\s+to|more|further)\b",
    r"\bshould\s+i\b[^.]{0,80}\?",
    r"\bdo\s+you\s+want\s+me\s+to\b",
    r"\bwhich\s+direction\s+(is|would\s+be)\s+(useful|helpful)\b",
    r"\bwhat\s+would\s+you\s+like\b",
    r"\b(any|the)\s+next\s+steps?\?",
    r"\bawaiting\s+(your|user|further)\s+(direction|input|guidance|instruction)\b",
    r"\bplease\s+(let\s+me\s+know|advise|confirm|specify)\b",
    r"\bhappy\s+to\s+(continue|help|proceed|adjust)\b",
    r"\bi\s+(can|could)\s+(also|further|next)\b\s+\w+\b\s*[—\-,]",
    r"\bif\s+that('s|\s+is)\s+useful\b",
    r"\b(ready\s+to\s+)?stop\s+here\b",
    r"\b(stopping\s+)?here\s+(for\s+now|seems\s+(good|right))\b",
    r"\bthat\s+(should\s+)?(do\s+it|cover\s+it|wrap\s+(it|things)\s+up)\b",
    r"\bcomplete[ds]?\s*[—\-]?\s*(let\s+me\s+know)\b",
    r"\bare\s+(answered|complete|done)\b\.\s+if\s+you\s+want",
]
_SCOPE_SHRINK_RE = re.compile(
    "|".join(_SCOPE_SHRINK_PATTERNS + _DEFLECTION_PATTERNS), re.IGNORECASE
)


def detect_scope_shrinking(
    session_id: str,
    spawner_id: str,
    transcript_path: Path,
    last_n_turns: int = 20,
    verifier_all_pass: bool = False,
) -> list[Finding]:
    """Scan the most recent assistant turns for scope-shrinking language.

    If ANY scope-shrink phrase appears in assistant output AND the verifier
    does not currently report all_pass, that is a `fabrication` finding — the
    agent is signalling stop-intent while the mission is not verifiably done.
    """
    if verifier_all_pass:
        return []
    try:
        from swarm.lib.transcript import last_n_turns as read_last_n_turns
    except Exception:
        return []
    turns = read_last_n_turns(transcript_path, last_n_turns)
    if not turns:
        return []
    hits: list[tuple[str, str]] = []
    for t in turns:
        if t.role != "assistant":
            continue
        for combined in (t.text, t.thinking):
            if not combined:
                continue
            for m in _SCOPE_SHRINK_RE.finditer(combined):
                snippet = combined[max(0, m.start() - 60) : m.end() + 60]
                hits.append((m.group(0), snippet))
    if not hits:
        return []
    phrases = sorted({h[0].lower() for h in hits})
    snippet_preview = hits[0][1][:400]
    return [
        Finding(
            id=mint_finding_id(),
            source="pattern_detector.scope_shrinking",
            subject_session=session_id,
            spawner_id=spawner_id,
            type="fabrication",
            subtype="scope_shrinking",
            severity="major",
            evidence=Evidence(claim_excerpt=snippet_preview),
            verdict=(
                "Agent used scope-shrinking language "
                f"({', '.join(phrases)}) while mission is not verifiably complete. "
                "The mission's success_criteria are the contract; declaring items "
                "'out of scope' or 'deferred' is a cheat pattern — the work must "
                "be either completed or the user must revise mission.yaml."
            ),
        )
    ]


# -------- daemon entry point --------


def _load_mission(session_id: str) -> Mission:
    import yaml

    raw = yaml.safe_load(mission_yaml_path(session_id).read_text())
    return Mission.model_validate(raw)




def main(session_id: str, period_sec: float = 10.0) -> None:
    from swarm.lib.paths import claude_transcript_path, session_dir
    from swarm.specialists.event_scribe import read_events

    ensure_session_dirs(session_id)
    # Fail fast if launcher is gone — mission load below would crash instead
    # of exiting cleanly (mission.yaml wouldn't exist for an orphan session).
    exit_if_launcher_dead(session_id, LOG)
    mission = _load_mission(session_id)
    seen_keys: set[tuple[str, str]] = set()
    cycles = 0
    LOG.info("pattern_detector starting for session=%s", session_id)
    transcript = claude_transcript_path(session_id, mission.workspace)
    while True:
        exit_if_launcher_dead(session_id, LOG)
        events = read_events(session_id)
        findings = detect_once(events, mission)

        # Scope-shrinking: only when verifier has NOT confirmed all_pass
        verifier_status_file = session_dir(session_id) / "verifier_status.json"
        all_pass = False
        if verifier_status_file.exists():
            try:
                all_pass = bool(
                    json.loads(verifier_status_file.read_text()).get("all_pass", False)
                )
            except json.JSONDecodeError:
                all_pass = False
        findings.extend(
            detect_scope_shrinking(
                session_id=session_id,
                spawner_id=session_id,
                transcript_path=transcript,
                verifier_all_pass=all_pass,
            )
        )

        for f in findings:
            # Dedup: don't re-emit a finding whose (subtype, first cited_event) we've seen
            key = (f.subtype, f.cited_events[0] if f.cited_events else f.id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            write_line(findings_path(session_id), f.model_dump_json())
            LOG.info("finding: %s", f.model_dump_json())
        cycles += 1
        beat(session_id, "pattern_detector", cycles)
        time.sleep(period_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: pattern_detector.py <session_id>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
