"""``PatternDetectorWorkflow`` — event-stream pattern rules (Task 15).

Per spec §6.2 lines 156-162 and §6.4 rows 6a/6b:

    PatternDetectorWorkflow(mission_id, session_id, cadence_sec)
      tails ~/.swarm/state/<sid>/events.jsonl
      runs pattern rules (loop, oscillation) on each new batch
      emits findings via signal to parent
      ContinueAsNew every 500 events to bound history

    The scope-shrinking detector (today's
    ``specialists/pattern_detector.detect_scope_shrinking``) lives in the
    same workflow but is gated on a signal from the parent — the parent
    fires ``check_scope_shrinking`` on all-criteria-passing (per §6.4
    row 6b) and this workflow responds by calling the
    ``detect_scope_shrinking`` activity and emitting any resulting
    finding.

Determinism contract:

* Uses ``workflow.execute_activity`` for every file read — no direct
  I/O in workflow code.
* ``_run_pattern_rules`` is a pure function (no time, no randomness,
  no I/O) so it's safe to call inline.
* No ``datetime.now()`` or ``asyncio.sleep`` — ``workflow.sleep``.

Ports the loop-detection and oscillation-detection heuristics verbatim
from ``specialists/pattern_detector.py`` lines 43-141.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from swarm.durable import retry_policies
    from swarm.durable.specialists._utils import _emit_to_parent


# Default thresholds — match ``schemas.mission.PatternThresholds`` defaults.
# Hard-coded here because the workflow doesn't carry ``Mission`` in its run
# signature (it gets ``mission_id, session_id, cadence_sec``). Fetching the
# mission config mid-run would be an extra activity; for v0 we use the
# schema defaults. Task 15+ may add a ``thresholds`` arg to the run
# signature when the parent decides to override.
_DEFAULT_LOOP_REPEAT_COUNT = 5
_DEFAULT_LOOP_WINDOW_EVENTS = 50
_DEFAULT_OSC_REVERT_COUNT = 2
_DEFAULT_OSC_WINDOW_EVENTS = 30

# History-bounding knob — matches spec §6.2 line 161.
_CONTINUE_AS_NEW_EVENT_THRESHOLD = 500


# ============================================================================
# Pure pattern-rule helpers — ported verbatim from
# ``specialists/pattern_detector.py`` lines 33-141 with light adaptations:
#   * events are plain dicts (from the activity) not pydantic ``Event``s
#   * findings are plain dicts (ready for ``emit_finding``)
#   * no ``mint_finding_id`` call — the parent's ``emit_finding`` activity
#     will tag its own ``emitted_at`` and the workflow does not need a
#     client-side ID (parent uses full-finding dedup from its own state).
# ============================================================================


def _normalize_arg(s: str | None) -> str:
    """Whitespace / trailing slash / quote-style normalization.

    Ported from ``specialists/pattern_detector.normalize_arg``.
    Identical semantics — kept inline so the two callers are in one file.
    """
    if s is None:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip("/")
    s = s.replace("'", '"')
    return s


def _detect_loops(
    events: list[dict[str, Any]],
    repeat_count: int = _DEFAULT_LOOP_REPEAT_COUNT,
    window_events: int = _DEFAULT_LOOP_WINDOW_EVENTS,
) -> list[dict[str, Any]]:
    """Emit a ``loop`` finding when any (tool, normalized_input) pair
    appears ``>= repeat_count`` times in the last ``window_events`` events.

    Ported from ``specialists/pattern_detector.detect_loops`` with:
      * events as dicts (``.get()`` instead of attribute access)
      * no Finding/Evidence pydantic wrapping — plain dict out
    """
    if not events:
        return []
    window = events[-window_events:]
    counts: Counter[tuple[str, str]] = Counter()
    for ev in window:
        tool_name = ev.get("tool_name")
        if tool_name is None:
            continue
        key = (tool_name, _normalize_arg(ev.get("tool_input_summary")))
        counts[key] += 1

    findings: list[dict[str, Any]] = []
    # Stable iteration order over the Counter so tests are deterministic.
    for (tool, norm_input), n in counts.items():
        if n >= repeat_count:
            cited = [
                ev.get("id")
                for ev in window
                if ev.get("tool_name") == tool
                and _normalize_arg(ev.get("tool_input_summary")) == norm_input
            ]
            last_ev = window[-1]
            findings.append(
                {
                    "source": "pattern_detector.loop",
                    "subject_session": last_ev.get("session_id", ""),
                    "spawner_id": last_ev.get("spawner_id", last_ev.get("session_id", "")),
                    "type": "loop",
                    "subtype": "repeat_exact_args",
                    "severity": "major",
                    "cited_events": [c for c in cited if c],
                    "evidence": {
                        "tool_calls": [c for c in cited if c],
                        "claim_excerpt": f"{tool}({norm_input[:200]})",
                    },
                    "verdict": f"tool={tool} repeated {n} times",
                }
            )
    return findings


def _detect_oscillation(
    events: list[dict[str, Any]],
    revert_count: int = _DEFAULT_OSC_REVERT_COUNT,
    window_events: int = _DEFAULT_OSC_WINDOW_EVENTS,
) -> list[dict[str, Any]]:
    """Emit a ``thrash/oscillation`` finding when a file's content_hash
    returns to a prior state ``>= revert_count`` times in the last
    ``window_events``.

    Ported from ``specialists/pattern_detector.detect_oscillation``.
    Same regex shape (``file=<path>`` + ``content_hash=<hex>``) — the
    hook-emitted tool summaries are the source of truth for that format.
    """
    if not events:
        return []
    window = events[-window_events:]
    per_file_hashes: dict[str, list[str]] = {}
    per_file_events: dict[str, list[str]] = {}
    for ev in window:
        if ev.get("tool_name") not in {"Edit", "Write"}:
            continue
        m = re.search(r"file=([^\s;]+)", ev.get("tool_input_summary") or "")
        h = re.search(
            r"content_hash=([a-f0-9]+)", ev.get("tool_response_summary") or ""
        )
        if not m or not h:
            continue
        file = m.group(1)
        hsh = h.group(1)
        per_file_hashes.setdefault(file, []).append(hsh)
        ev_id = ev.get("id")
        if ev_id:
            per_file_events.setdefault(file, []).append(ev_id)

    findings: list[dict[str, Any]] = []
    for file, hashes in per_file_hashes.items():
        reverts = 0
        seen: set[str] = set()
        for i, prior_hash in enumerate(hashes):
            if prior_hash in seen and (i == 0 or hashes[i - 1] != prior_hash):
                reverts += 1
            seen.add(prior_hash)
        if reverts >= revert_count:
            last_ev = window[-1]
            findings.append(
                {
                    "source": "pattern_detector.oscillation",
                    "subject_session": last_ev.get("session_id", ""),
                    "spawner_id": last_ev.get("spawner_id", last_ev.get("session_id", "")),
                    "type": "thrash",
                    "subtype": "oscillation",
                    "severity": "major",
                    "cited_events": per_file_events.get(file, []),
                    "evidence": {"files": [file]},
                    "verdict": f"file={file} reverted {reverts} times",
                }
            )
    return findings


def _run_pattern_rules(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run every (non-LLM, non-transcript) pattern rule once.

    Pure function — no I/O, no workflow state. Safe to call from inside a
    Temporal workflow because it only does deterministic dict/regex/Counter
    operations on the input. Output order is deterministic (Counter
    iteration order is insertion order in CPython ≥3.7, which is what we
    rely on here).
    """
    findings: list[dict[str, Any]] = []
    findings.extend(_detect_loops(events))
    findings.extend(_detect_oscillation(events))
    return findings


# ============================================================================
# Workflow class
# ============================================================================


@workflow.defn
class PatternDetectorWorkflow:
    """Long-running observer child workflow for event-pattern detection.

    One instance per mission, spawned by ``MissionWorkflow._start_children``.
    Cadence-driven: on each tick it
      1. Reads new events via ``read_recent_events`` activity.
      2. Runs pure pattern rules on the new batch.
      3. Emits any resulting findings to the parent via signal.
      4. If ``check_scope_shrinking`` signal has fired, calls the
         ``detect_scope_shrinking`` activity and forwards any finding.
      5. If cumulative events since the last cas have crossed
         ``_CONTINUE_AS_NEW_EVENT_THRESHOLD``, triggers ``continue_as_new``
         to bound Temporal history size.

    State carried across ``continue_as_new``:
      * ``last_offset`` — byte offset into ``events.jsonl`` so we pick
        up where the previous incarnation left off.
      * ``events_since_cas`` — resets to 0 on cas.
      * ``scope_shrinking_requested`` — drained each cycle; if the parent
        signals during a cas, the resumed incarnation sees the flag and
        honours it.

    Stub-interface contract preserved from Task 14 placeholder:
      * Run signature is ``(mission_id, session_id, cadence_sec)``
        positional — three strings. Must not change without a parent
        update.
    """

    def __init__(self) -> None:
        # Instance state — reconstructed from constructor args on every
        # incarnation. For ``continue_as_new`` we thread the offset
        # through run args; the instance-level mutation happens inside
        # ``run``.
        self._last_offset: int = 0
        self._scope_shrinking_requested: bool = False

    @workflow.run
    async def run(
        self,
        mission_id: str,
        session_id: str,
        cadence_sec: int,
        # ``last_offset`` + ``events_since_cas`` are optional so the first
        # launch (from the parent) can call with just three positional
        # args. The parent passes None/0 defaults; the cas path threads
        # the carry-values through explicitly.
        last_offset: int = 0,
        events_since_cas: int = 0,
    ) -> dict[str, Any]:
        self._last_offset = int(last_offset)
        self._scope_shrinking_requested = False
        processed_since_cas = int(events_since_cas)

        while True:
            # Step 1 — read new events (via activity; workflows cannot
            # touch the filesystem directly per spec §5).
            result = await workflow.execute_activity(
                "read_recent_events",
                args=[session_id, self._last_offset],
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=retry_policies.READ_EVENTS,
            )
            # Support both dict-of-fields and attribute-bearing return
            # shapes so tests can mock with whichever is convenient.
            events, next_offset = _unpack_events_result(result)

            # Step 2 — pattern rules (pure function, safe inline).
            if events:
                for finding in _run_pattern_rules(events):
                    await _emit_to_parent(mission_id, finding)
                processed_since_cas += len(events)
                self._last_offset = int(next_offset)

            # Step 4 — drain the scope_shrinking signal if it fired. Two
            # concerns: the activity can only be called under a specific
            # context (parent has flipped verifier to all_pass). The
            # signal from the parent is the gate — we run the activity
            # once per signal.
            if self._scope_shrinking_requested:
                self._scope_shrinking_requested = False
                try:
                    ss_result = await workflow.execute_activity(
                        "detect_scope_shrinking",
                        args=[{
                            "mission_id": mission_id,
                            "session_id": session_id,
                            "spawner_id": session_id,
                            # Activity tolerates all-optional context —
                            # the parent will later signal richer
                            # context when Task 18+ wires the
                            # criterion-history feed.
                        }],
                        start_to_close_timeout=timedelta(seconds=10),
                        retry_policy=retry_policies.DETECT_SCOPE_SHRINKING,
                    )
                    ss_finding = _scope_shrinking_finding_of(ss_result)
                    if ss_finding is not None:
                        await _emit_to_parent(mission_id, ss_finding)
                except Exception as exc:  # noqa: BLE001 — defensive
                    workflow.logger.warning(
                        "pattern_detector: scope_shrinking failed: %r", exc
                    )

            # Step 5 — history-bounding.
            if processed_since_cas >= _CONTINUE_AS_NEW_EVENT_THRESHOLD:
                # Never returns — raises ContinueAsNewError.
                workflow.continue_as_new(
                    args=[
                        mission_id,
                        session_id,
                        cadence_sec,
                        self._last_offset,
                        0,  # reset events_since_cas on resume
                    ]
                )

            await workflow.sleep(cadence_sec)

    # --------------------------------------------------------- signals ----

    @workflow.signal
    async def check_scope_shrinking(self) -> None:
        """Parent signals this when all criteria are passing.

        Per spec §6.4 row 6b: the scope-shrinking detector is meaningful
        only when the verifier reports ``all_pass`` — i.e. an agent
        declaring "done" while mission is verifiably complete would NOT
        be a fabrication. The parent owns that state; this signal is
        how the parent grants the child permission to run the detector.

        The flag is drained in the next loop iteration so rapid signals
        don't accumulate unbounded work (the activity is idempotent —
        running it twice does no harm but wastes LLM tokens).
        """
        self._scope_shrinking_requested = True


# ----------------------------------------------------------------- helpers --


def _unpack_events_result(r: Any) -> tuple[list[dict[str, Any]], int]:
    """Normalize ``read_recent_events`` result into ``(events, next_offset)``.

    The activity returns a ``dict`` today, but tests occasionally mock it
    with an object bearing ``.events`` / ``.next_offset`` attributes.
    Accepting both shapes mirrors the pattern used in ``workflow.py``'s
    result-dataclass accessors.
    """
    if isinstance(r, dict):
        events = r.get("events", [])
        next_offset = r.get("next_offset", 0)
    else:
        events = getattr(r, "events", [])
        next_offset = getattr(r, "next_offset", 0)
    # Defensive: in case the mock forgot to wrap in a list.
    if not isinstance(events, list):
        events = []
    try:
        next_offset = int(next_offset)
    except (TypeError, ValueError):
        next_offset = 0
    return events, next_offset


def _scope_shrinking_finding_of(r: Any) -> dict[str, Any] | None:
    """Extract the ``finding`` dict from a ``ScopeShrinkingResult``.

    Mirrors the ``_finding_of``/``_findings_of`` accessors in
    ``workflow.py`` — tolerate dataclass-attr or dict shape.
    """
    if hasattr(r, "finding"):
        return getattr(r, "finding")
    if isinstance(r, dict):
        return r.get("finding")
    return None
