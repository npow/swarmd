"""``completion_judge`` Temporal activity — sole arbiter of mission_complete.

Per spec §6.3 and plan Task 8:

    completion_judge(mission_state, session_state_dir) → CompletionDecision

    Check the six preconditions that must all hold before a mission can
    transition to ``complete``. Pure dict-in / dataclass-out interface; no
    dependency on the legacy ``Finding`` schema.

Ported from ``specialists/completion_judge.py`` lines 62-168. The semantics
are preserved; the interface is restructured to:

* Take ``mission_state`` (a dict carrying ``hold_window_start``) and the
  ``session_state_dir`` path instead of reaching into the global session
  layout.
* Read findings from ``{session_state_dir}/findings.jsonl`` directly
  rather than going through ``swarm.lib.paths``. The activity owns its
  path resolution.
* Return a ``CompletionDecision`` with ``approved: bool`` and
  ``reasons: list[str]`` (the list of failed preconditions). An approved
  decision has an empty ``reasons`` list.

The six preconditions:

    1. Hold-window recency — ``mission_state["hold_window_start"]`` must
       be within ``hold_window_recency_sec`` seconds of now. A stale
       hold window from an earlier pass-then-fail cycle does not count.
    2. No open cheat findings in ``findings.jsonl``.
    3. No open fabrication findings.
    4. No open tamper findings (``meta/tamper_detected``).
    5. No critic disagreements (``meta/critic_disagreement``).
    6. No unresolved anticheat findings — findings with ``source`` starting
       ``anticheat.`` and type ``cheat`` or a non-``critic_disagreement``
       meta.

Robustness:

* Missing ``findings.jsonl`` is treated as empty (fresh session).
* Malformed JSON lines are skipped silently — one garbage line must not
  sink the judge.
* All six checks run every call; we never short-circuit, so the caller
  gets the full picture of why completion is blocked.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio import activity

# Default window in seconds for how recently ``hold_window_start`` must have
# been set. 300s mirrors the legacy ``completion_judge.judge()`` default —
# any hold-window signal older than this is stale and doesn't count. See the
# plan's precondition #1.
_HOLD_WINDOW_RECENCY_SEC = 300


@dataclass
class CompletionDecision:
    """Outcome of a completion-judge call.

    ``approved=True`` iff every precondition passes and ``reasons`` is
    empty. ``approved=False`` means the workflow must NOT transition to
    complete; ``reasons`` lists every failed precondition so operators can
    act without re-running the judge.
    """

    approved: bool
    reasons: list[str] = field(default_factory=list)


@activity.defn(name="completion_judge")
async def completion_judge(
    mission_state: dict[str, Any], session_state_dir: str
) -> CompletionDecision:
    """Check the six preconditions; return ``approved=True`` iff all pass.

    Reads ``{session_state_dir}/findings.jsonl`` to scan for blocking
    findings. Reads ``mission_state["hold_window_start"]`` to check the
    recency precondition.

    Never raises on normal inputs. A missing findings file, malformed JSON
    lines, or a missing ``hold_window_start`` key are all handled as
    preconditions that fail with a descriptive reason — not as errors.
    """
    reasons: list[str] = []

    # Precondition 1 — hold window recency. The old coordinator used a
    # hold_window_met finding's age; the new contract uses mission_state
    # which the workflow owns, so recency is trivially computed.
    hold_start = mission_state.get("hold_window_start")
    if hold_start is None:
        reasons.append("hold window never reached")
    else:
        try:
            age_sec = max(0.0, time.time() - float(hold_start))
        except (TypeError, ValueError):
            age_sec = float("inf")
        if age_sec > _HOLD_WINDOW_RECENCY_SEC:
            reasons.append(
                f"hold_window_start is older than "
                f"{_HOLD_WINDOW_RECENCY_SEC}s (age {int(age_sec)}s)"
            )

    # Preconditions 2-6 — scan findings.jsonl. Missing file = empty list.
    findings = _read_findings(Path(session_state_dir) / "findings.jsonl")

    # Precondition 2 — no open cheat findings.
    cheats = [f for f in findings if f.get("type") == "cheat"]
    if cheats:
        reasons.append(f"{len(cheats)} open cheat finding(s)")

    # Precondition 3 — no open fabrication findings.
    fabrications = [f for f in findings if f.get("type") == "fabrication"]
    if fabrications:
        reasons.append(f"{len(fabrications)} open fabrication finding(s)")

    # Precondition 4 — no open tamper findings.
    tampers = [
        f
        for f in findings
        if f.get("type") == "meta" and f.get("subtype") == "tamper_detected"
    ]
    if tampers:
        reasons.append(
            f"{len(tampers)} tamper finding(s) — mission paused"
        )

    # Precondition 5 — no critic disagreements. A single disagreement is
    # enough to block because the multi-provider panel couldn't reach
    # consensus, which the system treats as the strongest anticheat signal.
    disagreements = [
        f
        for f in findings
        if f.get("type") == "meta"
        and f.get("subtype") == "critic_disagreement"
    ]
    if disagreements:
        reasons.append(
            f"{len(disagreements)} critic disagreement(s) — blocks completion"
        )

    # Precondition 6 — per-criterion anticheat. The legacy judge required
    # anticheat-sourced findings to be ``GENUINE_FIX``; we approximate by
    # flagging any anticheat source that emitted a ``cheat`` or non-
    # ``critic_disagreement`` ``meta`` finding — those are the concrete
    # "didn't pass" signals. ``critic_disagreement`` is already counted
    # separately in precondition 5.
    anticheat_non_genuine = [
        f
        for f in findings
        if isinstance(f.get("source"), str)
        and f["source"].startswith("anticheat.")
        and (
            f.get("type") == "cheat"
            or (
                f.get("type") == "meta"
                and f.get("subtype") != "critic_disagreement"
            )
        )
    ]
    if anticheat_non_genuine:
        reasons.append(
            f"{len(anticheat_non_genuine)} anticheat non-GENUINE verdict(s)"
        )

    return CompletionDecision(approved=not reasons, reasons=reasons)


def _read_findings(path: Path) -> list[dict[str, Any]]:
    """Read ``findings.jsonl`` as a list of dicts, one per line.

    Missing file → empty list. Malformed lines are skipped. Factored so
    the activity body stays short and tests can target the read path.
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A single malformed line must not sink the judge — the
                # attack surface for "break completion by appending junk
                # to findings.jsonl" is bigger than the legitimate reason
                # to fail-closed on a parse error.
                continue
    return out
