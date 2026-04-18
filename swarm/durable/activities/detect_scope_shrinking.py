"""``detect_scope_shrinking`` Temporal activity — pure-computation detector.

Per plan Task 12:

    detect_scope_shrinking(context) → ScopeShrinkingResult

Inspects recent criterion check results and code diffs for signals that
the subject agent is narrowing the problem scope (dropping criteria,
weakening assertions). Emits a ``Finding`` dict when scope-shrinking
is detected, otherwise returns ``detected=False``.

This activity is pure Python — NO network, NO subprocess, NO LLM.
Contract violations (malformed ``context``) raise ``TerminalError`` so
Temporal fails fast instead of burning the retry budget.

Signal types inspected (both are ported from the behavior that
``specialists/pattern_detector.py`` was designed to catch, adapted
to the Task 12 signature):

1. **Criterion dropped from original list.** Any id in
   ``original_criteria`` that never appears in ``criterion_history``
   is evidence the agent stopped checking it. Highlighted in the
   rationale so the finding is actionable.
2. **Net-removed assertions in diffs.** Counts ``-    assert ...``
   minus ``+    assert ...`` lines in the supplied diff text. A net
   removal (more asserts dropped than added) is a scope-shrinking
   signal; a net addition is the reverse signal (explicitly NOT a
   finding).

The finding dict is shaped to match the other activities' finding
payloads (``source``, ``type``, ``severity``, ``verdict``, ``evidence``)
so ``emit_finding`` can consume it without transformation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from temporalio import activity

from swarm.durable.errors import TerminalError


# Regex for diff lines that add or remove an ``assert`` statement. We
# require ``assert`` followed by whitespace OR EOL to avoid matching
# identifiers that start with "assert" (e.g. ``assertion_helper``).
# ``^[+-]\s*assert\b`` anchors to diff add/remove markers at line start.
_DIFF_ADD_ASSERT_RE = re.compile(r"^\+\s*assert\b", re.MULTILINE)
_DIFF_REMOVE_ASSERT_RE = re.compile(r"^-\s*assert\b", re.MULTILINE)


@dataclass
class ScopeShrinkingResult:
    """Outcome of a ``detect_scope_shrinking`` call.

    ``detected`` is ``True`` when any scope-shrinking signal fires.
    ``rationale`` always populated — for a non-detection it describes
    what was inspected. ``finding`` is ``None`` on non-detection and a
    ready-for-``emit_finding`` dict on detection.
    """

    detected: bool
    rationale: str
    finding: dict[str, Any] | None


def _validate_context(context: dict[str, Any]) -> None:
    """Raise TerminalError if the context shape is malformed.

    Retries cannot fix a bad contract, so we signal Temporal to fail fast.
    """
    ch = context.get("criterion_history")
    if ch is not None and not isinstance(ch, list):
        raise TerminalError(
            "detect_scope_shrinking: criterion_history must be a list, "
            f"got {type(ch).__name__}"
        )
    oc = context.get("original_criteria")
    if oc is not None and not isinstance(oc, list):
        raise TerminalError(
            "detect_scope_shrinking: original_criteria must be a list, "
            f"got {type(oc).__name__}"
        )
    rd = context.get("recent_diffs")
    if rd is not None and not isinstance(rd, (str, list)):
        raise TerminalError(
            "detect_scope_shrinking: recent_diffs must be str or list, "
            f"got {type(rd).__name__}"
        )


def _coalesce_diff_text(recent_diffs: Any) -> str:
    """Merge diff inputs (str or list[str]) into one string for regex scanning."""
    if recent_diffs is None:
        return ""
    if isinstance(recent_diffs, str):
        return recent_diffs
    if isinstance(recent_diffs, list):
        # Each entry is either a str or something str-able — tolerate dicts
        # (e.g. parsed hunks) by using str(). The regex only cares about
        # +/- line prefixes.
        return "\n".join(str(e) for e in recent_diffs)
    # _validate_context has already rejected other shapes, but be defensive.
    return ""


def _dropped_criteria(
    criterion_history: list[dict[str, Any]],
    original_criteria: list[str],
) -> list[str]:
    """Return criteria present in ``original_criteria`` but absent from history.

    Each history entry is a dict with (at least) a ``criterion_id`` key.
    We tolerate entries missing that key — they simply do not contribute
    to the "seen" set.
    """
    seen: set[str] = set()
    for entry in criterion_history:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("criterion_id")
        if isinstance(cid, str):
            seen.add(cid)
    return [c for c in original_criteria if isinstance(c, str) and c not in seen]


def _assertion_delta(diff_text: str) -> int:
    """Return (removed_asserts - added_asserts).

    Positive ⇒ assertions were net removed (scope-shrinking signal).
    Zero / negative ⇒ neutral or strengthening (not a signal).
    """
    removed = len(_DIFF_REMOVE_ASSERT_RE.findall(diff_text))
    added = len(_DIFF_ADD_ASSERT_RE.findall(diff_text))
    return removed - added


def _build_finding(
    rationale: str,
    dropped: list[str],
    assertion_delta: int,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the finding dict in the shared activity finding shape."""
    return {
        "source": "detect_scope_shrinking",
        "type": "scope_shrinking",
        "subtype": "scope_shrinking",
        "severity": "major",
        "verdict": rationale,
        "mission_id": context.get("mission_id"),
        "subject_session": context.get("session_id"),
        "spawner_id": context.get("spawner_id"),
        "evidence": {
            "claim_excerpt": rationale[:500],
            "dropped_criteria": dropped,
            "assertion_delta": assertion_delta,
        },
    }


@activity.defn(name="detect_scope_shrinking")
async def detect_scope_shrinking(
    context: dict[str, Any],
) -> ScopeShrinkingResult:
    """Analyze recent criterion history + diffs for scope-shrinking patterns.

    ``context`` keys (all optional, but must be the right shape if present):

    * ``criterion_history``  — list[dict], each with at least ``criterion_id``.
    * ``recent_diffs``       — str or list[str] (diff text or list of hunks).
    * ``original_criteria``  — list[str], criterion ids from the mission.
    * ``mission_id``         — str, passthrough for the finding.
    * ``session_id`` / ``spawner_id`` — str, passthrough.

    Raises:
        TerminalError: Malformed context (wrong type for one of the
            structured fields). Retries cannot fix a bad shape.
    """
    _validate_context(context)

    criterion_history = list(context.get("criterion_history") or [])
    original_criteria = list(context.get("original_criteria") or [])
    diff_text = _coalesce_diff_text(context.get("recent_diffs"))

    dropped = _dropped_criteria(criterion_history, original_criteria)
    delta = _assertion_delta(diff_text)

    reasons: list[str] = []
    if dropped:
        reasons.append(
            f"dropped criteria from history: {', '.join(dropped)}"
        )
    if delta > 0:
        reasons.append(
            f"diffs removed {delta} more assert statement(s) than they added"
        )

    if not reasons:
        return ScopeShrinkingResult(
            detected=False,
            rationale=(
                "no scope-shrinking signals detected: "
                f"{len(criterion_history)} history entr(ies), "
                f"{len(original_criteria)} original criteria, "
                f"assertion_delta={delta}"
            ),
            finding=None,
        )

    rationale = "scope-shrinking detected — " + "; ".join(reasons)
    return ScopeShrinkingResult(
        detected=True,
        rationale=rationale,
        finding=_build_finding(rationale, dropped, delta, context),
    )
