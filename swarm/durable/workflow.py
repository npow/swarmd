"""``MissionWorkflow`` — durable parent workflow per spec §6.2.

Task 13 delivered: skeleton + verifier loop + signals + queries.
Task 14 (this file): ``continue_as_new`` history-bounding + child-workflow
spawn on first launch + child-handle reconnect on resume, per spec §6.2
lines 141-155. The ``carry`` parameter, previously threaded through as a
no-op placeholder, is now the load-bearing signal that distinguishes
first launch from resume: empty ``carry`` → spawn children; non-empty
carry → re-acquire handles via ``get_external_workflow_handle``.

The verifier loop implements the 4 numbered steps in spec §6.2 (lines
107-139) exactly:

    1. Tamper check (``verify_tamper``)
    2. Invariant enforcement (``enforce_invariants``)
    3. Criterion checks in parallel (``check_criterion`` fan-out), then the
       pass-transition / hold-window / completion-judge decision tree.
    4. History-bounding (``continue_as_new``) — implemented in Task 14.

Determinism contract (spec §5):

* Time: use ``workflow.now()``. Never ``datetime.now()`` / ``time.time()``.
* Sleep: use ``workflow.sleep()``. Never ``asyncio.sleep()``.
* I/O: use ``workflow.execute_activity()``. Never direct I/O in workflow code.
* Activities are referenced by **string name** (the ``name=`` arg to their
  ``@activity.defn`` decorator), not by Python reference, because importing
  activity functions directly would run their non-deterministic module code
  at workflow registration time.

Signal/query model:

* ``@workflow.signal`` handlers update state or enqueue work. ``finding_emitted``
  ALSO calls the ``emit_finding`` activity synchronously inside the handler so
  the on-disk ``findings.jsonl`` mirror stays in lock-step with Temporal
  history — this is the "disk mirror stays in sync" guarantee from spec §6.3.
* ``@workflow.query`` handlers are pure reads — no state mutation, no
  activity calls.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import workflow

# The schema imports are pure pydantic / stdlib — no I/O at import time — so
# they are deterministic and safe to import at workflow module top-level. If
# any of these ever grow module-level side effects (DB lookups, network
# calls, etc.) they must be wrapped in ``with workflow.unsafe.imports_passed_through():``.
# ``specialists`` likewise only declares ``@workflow.defn`` classes (no I/O),
# so it is safe to import inside the passthrough. Importing here (not inside
# the method) keeps the import graph static, which Temporal's sandbox
# prefers — dynamic imports from inside workflow methods are legal but
# trigger a passthrough-cache miss every replay.
with workflow.unsafe.imports_passed_through():
    from swarm.durable import retry_policies
    from swarm.durable.specialists import (
        LLMCriticWorkflow,
        PatternDetectorWorkflow,
        ResourceMonitorWorkflow,
    )
    from swarm.durable.state import CriterionState, MissionState
    from swarm.schemas.mission import Mission


# --- Phase-machine terminal states ------------------------------------------

# Any of these phases ends the main verifier loop. Kept as a module-level
# constant so the loop condition and the tests can share the exact set.
_TERMINAL_PHASES: frozenset[str] = frozenset(
    {"complete", "aborted", "failed_terminal"}
)


# --- Path helpers ------------------------------------------------------------
#
# Mission.yaml does not currently declare paths for the tamper-anchor SHA or
# the session state directory (see swarm/schemas/mission.py). For Task 13 we
# derive them from ``mission.workspace`` using the canonical layout
# documented in spec §10 file layout. Task 14 may promote these to explicit
# fields on ``Mission``; that change would be backward-compatible here.


def _state_dir_for(workspace: str) -> str:
    """Session state directory ``{workspace}/.swarm/state``.

    Used as the first arg to ``emit_finding`` so findings are mirrored to
    ``findings.jsonl`` inside the workspace. Task 14 / mission-schema growth
    may replace this with an explicit ``session_state_dir`` field on
    ``Mission``.
    """
    return str(Path(workspace) / ".swarm" / "state")


def _tamper_sha_path_for(workspace: str) -> str:
    """Out-of-tree SHA anchor path ``{workspace}/.swarm/mission.lock.sha``.

    ``verify_tamper(mission_dir, out_of_tree_sha_path)`` needs the second
    arg. For Task 13 we anchor it beside the state dir; Task 14+ may move
    the anchor out of the workspace entirely (per spec §6.3 row 2 which
    describes an ``~/.config/swarm/locks/<sid>.sha`` location). That
    migration is a single-line change here.
    """
    return str(Path(workspace) / ".swarm" / "mission.lock.sha")


def _coerce_mission(value: Any) -> Mission:
    """Normalise ``value`` into a ``Mission`` instance.

    Temporal's default JSON data converter deserialises payloads to
    plain dicts (or, with the pydantic converter, may still emit a dict
    when the sandbox rewrites the type hint). Instead of relying on the
    converter wiring we normalise at the workflow boundary. Accepting a
    ``Mission`` pass-through keeps the test ergonomics good — we can
    construct a ``Mission`` directly and pass it to ``execute_workflow``.
    """
    if isinstance(value, Mission):
        return value
    return Mission.model_validate(value)


def _coerce_carry(value: Any) -> MissionState | None:
    """Normalise ``value`` into a ``MissionState`` instance (or ``None``).

    Same rationale as ``_coerce_mission`` — we can't trust the converter
    to always reconstruct the pydantic model, so do it explicitly at the
    boundary.
    """
    if value is None:
        return None
    if isinstance(value, MissionState):
        return value
    return MissionState.model_validate(value)


@workflow.defn
class MissionWorkflow:
    """Long-running parent workflow — one instance per mission.

    The workflow owns the mission phase-machine and the verifier loop.
    Short-lived work (criterion checks, tamper verification, invariant
    enforcement, completion judging) is delegated to activities; child
    workflows for the three observer specialists will arrive in Task 14.
    """

    # Class-level constant so tests can check the terminal set without
    # instantiating the workflow. Not part of the public API.
    _TERMINAL_PHASES = _TERMINAL_PHASES

    def __init__(self) -> None:
        # ``empty()`` is only a placeholder — the real init happens in
        # ``run`` once we know whether we were started with a ``carry``
        # (continue_as_new) or fresh. Keeping a valid default here means
        # early signal deliveries (before ``run`` begins) don't NPE.
        self._state: MissionState = MissionState.empty()

        # Signal handlers append dicts here so the main loop can observe
        # signal-driven work without fighting the concurrency model. Today
        # this is informational — the disk mirror happens inline in the
        # ``finding_emitted`` handler itself — but Task 14 (intervention
        # flow) will consume it.
        self._pending_signal_work: list[dict] = []

    # ------------------------------------------------------------------ run --

    @workflow.run
    async def run(
        self,
        mission: Mission | dict,
        carry: MissionState | dict | None = None,
    ) -> dict:
        """Main mission loop.

        ``carry`` is populated by Task 14's ``continue_as_new`` chain. For
        Task 13 it is always ``None`` — the workflow is always first-launch.

        ``mission`` may arrive as a plain ``dict`` (Temporal's default data
        converter does not reconstruct pydantic v2 models without explicit
        type hints, and even with ``pydantic_data_converter`` the sandbox
        can strip the model back to a dict). We coerce to ``Mission``
        unconditionally so the rest of the workflow can rely on attribute
        access. Same deal for ``carry`` / ``MissionState``.
        """
        # Normalise potentially-dict inputs to their pydantic forms. Doing
        # this INSIDE the workflow avoids the "data converter decided to
        # pass it as dict" class of failure — a single attribute access on
        # ``mission`` would otherwise NPE a whole mission.
        mission = _coerce_mission(mission)
        carry_state = _coerce_carry(carry)

        # Initialize state. Empty carry → first launch; non-empty carry →
        # resumed from continue_as_new. The ``carry_state is None`` branch
        # is the ONLY place children are spawned — every subsequent
        # incarnation in the continue_as_new chain must skip start_child
        # or Temporal raises WorkflowAlreadyStartedError (spec §6.2 lines
        # 145-148).
        first_launch = carry_state is None
        self._state = carry_state if not first_launch else MissionState.empty()
        if first_launch:
            self._state.phase = "running"
            await self._start_children(mission)
        else:
            # Resumed — re-acquire child handles. No-op today (the handle
            # is a local reference, not an RPC), but kept as a named seam
            # so Task 15-17 rewrites can hook in here if they need to
            # re-bind per-incarnation state.
            await self._reconnect_children()

        # Main verifier loop. Exits only when the phase transitions into a
        # terminal state. ``aborting`` is a transient intermediary set by
        # the ``abort`` signal handler; we flip it to ``aborted`` at the
        # top of the next iteration so the return value is a terminal state.
        while self._state.phase not in self._TERMINAL_PHASES:
            if self._state.phase == "aborting":
                self._state.phase = "aborted"
                break

            await self._verifier_cycle(mission)

            # Bail out immediately if the cycle transitioned to a terminal
            # state (completion_judge approved, tamper fired, abort signal
            # raced). Without this guard we'd burn one more sleep before
            # the while-condition notices.
            if self._state.phase in self._TERMINAL_PHASES:
                break
            if self._state.phase == "aborting":
                self._state.phase = "aborted"
                break

            # Step 4 — History-bounding. Two trigger paths:
            #   (a) Temporal's built-in heuristic (is_continue_as_new_suggested)
            #       which fires when event count / history bytes cross
            #       internal thresholds. Production path.
            #   (b) The ``force_continue_as_new`` flag set by the
            #       test-only ``force_continue_as_new`` signal. Lets tests
            #       deterministically exercise the cas chain without
            #       producing 50k events of history.
            #
            # The cas call NEVER returns (it raises ContinueAsNewError);
            # control does not resume after the call.
            if (
                workflow.info().is_continue_as_new_suggested()
                or self._state.force_continue_as_new
            ):
                # Clear the test flag so the next incarnation doesn't
                # immediately cas again in an infinite loop.
                self._state.force_continue_as_new = False
                workflow.continue_as_new(args=[mission, self._state])

            await workflow.sleep(mission.verification.run_every_sec)

        return {
            "phase": self._state.phase,
            "reason": self._state.abort_reason,
        }

    # ---------------------------------------------------------- verifier cycle

    async def _verifier_cycle(self, mission: Mission) -> None:
        """One pass of the spec §6.2 verifier loop.

        The four numbered steps (tamper → invariants → criterion checks →
        history-bounding) run in order; short-circuits only occur when a
        terminal condition is hit (tamper, completion).
        """
        state_dir = _state_dir_for(mission.workspace)

        # Step 1 — Tamper check. Runs FIRST so a tampered workspace can't
        # "pass" any subsequent criterion check.
        tamper = await workflow.execute_activity(
            "verify_tamper",
            args=[mission.workspace, _tamper_sha_path_for(mission.workspace)],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=retry_policies.VERIFY_TAMPER,
        )
        if _is_tamper_detected(tamper):
            finding = _finding_of(tamper)
            # Mirror the tamper finding to disk so operators see it in
            # ``findings.jsonl`` without having to read Temporal history.
            await workflow.execute_activity(
                "emit_finding",
                args=[state_dir, finding],
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=retry_policies.EMIT_FINDING,
            )
            self._state.abort_reason = (
                finding.get("verdict") if isinstance(finding, dict) else str(finding)
            )
            self._state.phase = "aborting"
            self._state.findings_count += 1
            return

        # Step 2 — Invariants. Findings are informational (disk-mirrored)
        # and do NOT abort the mission; the spec lets the mission keep
        # running so e.g. a dropped test can be reintroduced by the agent.
        inv = await workflow.execute_activity(
            "enforce_invariants",
            args=[mission.workspace, mission.invariants],
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=retry_policies.ENFORCE_INVARIANTS,
        )
        for finding in _findings_of(inv):
            await workflow.execute_activity(
                "emit_finding",
                args=[state_dir, finding],
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=retry_policies.EMIT_FINDING,
            )
            self._state.findings_count += 1

        # Step 3 — Criterion checks, fanned out in parallel. ``asyncio.gather``
        # is deterministic inside Temporal's event-loop (spec §5); each
        # activity future is recorded in history in call order.
        criterion_results = await asyncio.gather(
            *[
                workflow.execute_activity(
                    "check_criterion",
                    args=[c, mission.workspace],
                    start_to_close_timeout=timedelta(seconds=c.timeout_sec + 5),
                    retry_policy=retry_policies.CHECK_CRITERION,
                )
                for c in mission.success_criteria
            ]
        )

        now = workflow.now()
        run_every = mission.verification.run_every_sec

        # Update per-criterion state. A flip from passing → failing resets
        # the streak; a continuing pass adds the verifier cadence to it.
        # First-ever check initialises the streak at 0 (or run_every if
        # already passing) consistent with spec §6.2.
        all_pass = True
        for r in criterion_results:
            cid = _criterion_id_of(r)
            passing = _pass_of(r)
            prior = self._state.criteria_state.get(cid)
            if prior is None:
                streak = float(run_every) if passing else 0.0
            else:
                if passing and prior.pass_:
                    streak = prior.streak_sec + float(run_every)
                elif passing and not prior.pass_:
                    # Newly-passing — start the streak clock from this cycle.
                    streak = float(run_every)
                else:
                    # Failing — reset the streak regardless of prior value.
                    streak = 0.0

            self._state.criteria_state[cid] = CriterionState(
                pass_=passing,
                last_check_ts=now,
                streak_sec=streak,
                exit_code=_exit_code_of(r),
                stderr_tail=_stderr_tail_of(r),
            )
            if not passing:
                all_pass = False

        # Pass-transition / hold-window / completion-judge decision tree.
        phase = self._state.phase
        if all_pass and phase != "hold_window":
            # Task 14 / Task 16: fire anti-cheat panel signal to the
            # LLMCriticWorkflow child here. For Task 13 we just transition
            # to the hold window — children don't exist yet.
            self._state.phase = "hold_window"
            self._state.hold_window_start = now
        elif not all_pass and phase == "hold_window":
            # A criterion flipped back to failing — cancel the hold window
            # and go back to running so the agent has room to recover.
            self._state.hold_window_start = None
            self._state.phase = "running"
        elif phase == "hold_window":
            # Still all-passing. Check whether we've held long enough to
            # invoke the completion judge.
            elapsed = now - (self._state.hold_window_start or now)
            if elapsed >= timedelta(
                seconds=mission.verification.hold_window_sec
            ):
                decision = await workflow.execute_activity(
                    "completion_judge",
                    args=[
                        self._state_to_dict_for_judge(),
                        _state_dir_for(mission.workspace),
                    ],
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=retry_policies.COMPLETION_JUDGE,
                )
                if _approved_of(decision):
                    self._state.phase = "complete"
                else:
                    # Emit a completion_blocked finding but stay in
                    # hold_window so the next cycle tries again after the
                    # blocking conditions clear.
                    block_finding = {
                        "type": "meta",
                        "subtype": "completion_blocked",
                        "severity": "warning",
                        "verdict": "; ".join(_reasons_of(decision))
                        or "completion_judge rejected",
                    }
                    await workflow.execute_activity(
                        "emit_finding",
                        args=[state_dir, block_finding],
                        start_to_close_timeout=timedelta(seconds=5),
                        retry_policy=retry_policies.EMIT_FINDING,
                    )
                    self._state.findings_count += 1

        # Step 4 — History-bounding runs AFTER the per-cycle work in
        # ``run`` (not here), so ``_verifier_cycle`` stays focused on
        # steps 1-3. See ``run`` for the continue_as_new call.

    # ------------------------------------------------------------ child workflows

    async def _start_children(self, mission: Mission) -> None:
        """Start the three observer child workflows per spec §6.2 lines 156-180.

        Called exactly once per mission — from ``run`` on the first launch
        (when ``carry`` is ``None``). Post continue_as_new the parent
        re-acquires handles via ``_reconnect_children`` instead; calling
        ``start_child_workflow`` a second time would raise
        ``WorkflowAlreadyStartedError`` because the child IDs are fixed.

        Child IDs are derived deterministically from the parent's
        ``workflow_id``. Temporal guarantees the parent's ``workflow_id``
        is stable across the continue_as_new chain, so children stay
        reachable by ID across parent restarts — this is the mechanism
        spec §6.2 relies on for signal routing.

        ``parent_close_policy=TERMINATE`` ensures mission abort /
        completion tears down the full tree; otherwise children would
        orphan.
        """
        mid = workflow.info().workflow_id
        observer = mission.observer_config
        # session_id: Mission schema doesn't currently carry one, so we
        # use the parent's workflow_id as a stable identifier. Once the
        # schema grows a ``session_id`` field (or the launcher passes one
        # through), swap this for the explicit value. Behaviour stays
        # identical because Tasks 15-17 use session_id only for
        # events.jsonl path resolution, and that derivation is consistent
        # with workflow_id today.
        session_id = mid

        # Cadence defaults come from spec §6.2 and live on ``ObserverConfig``
        # with defaults (10/120/30). We use ``getattr`` with an explicit
        # default here rather than direct attribute access so the code
        # stays robust if ``Mission`` was constructed with an older
        # ``ObserverConfig`` that predates Task 3's cadence fields — pydantic
        # serialization round-trips through dict form can end up with a
        # model instance where the new fields simply don't appear. This is
        # defence-in-depth; production ObserverConfig has the fields.
        pd_sec = getattr(observer, "pattern_detector_sec", 10)
        lc_sec = getattr(observer, "llm_critic_sec", 120)
        rm_sec = getattr(observer, "resource_monitor_sec", 30)

        # Start each child with its spec-defined cadence default (overridable
        # via mission.yaml observer_config). Await each start sequentially
        # so the workflow history has a predictable event order; parallel
        # ``asyncio.gather`` would also be legal but complicates failure
        # diagnosis if one of the three starts ever throws.
        await workflow.start_child_workflow(
            PatternDetectorWorkflow.run,
            args=[mid, session_id, pd_sec],
            id=f"{mid}_pattern_detector",
            parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
        )
        await workflow.start_child_workflow(
            LLMCriticWorkflow.run,
            args=[mid, session_id, lc_sec],
            id=f"{mid}_llm_critic",
            parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
        )
        await workflow.start_child_workflow(
            ResourceMonitorWorkflow.run,
            args=[mid, session_id, rm_sec],
            id=f"{mid}_resource_monitor",
            parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
        )

        # Record the IDs in durable state so they survive continue_as_new
        # (the resumed incarnation reads these to re-acquire handles).
        self._state.child_workflow_ids = {
            "pattern_detector": f"{mid}_pattern_detector",
            "llm_critic": f"{mid}_llm_critic",
            "resource_monitor": f"{mid}_resource_monitor",
        }

    async def _reconnect_children(self) -> None:
        """Re-acquire handles to the three child workflows post continue_as_new.

        No-op in Task 14: ``workflow.get_external_workflow_handle(id)`` is
        a cheap local reference (not an RPC), so we don't need to eagerly
        call it here — the signal senders in the intervention flow
        (future task) will call it at dispatch time. Kept as a named seam
        so (a) the code path is obvious when reading ``run``, and (b)
        Tasks 15-17 can hook per-incarnation setup here without touching
        ``run`` itself.

        The ``child_workflow_ids`` dict carried over from the previous
        incarnation is already correct — no mutation needed.
        """
        # Intentionally empty. See docstring for rationale.
        return None

    # ----------------------------------------------------------- state helpers

    def _state_to_dict_for_judge(self) -> dict[str, Any]:
        """Serialize ``self._state`` for the ``completion_judge`` activity.

        The judge needs a dict and expects ``hold_window_start`` as a POSIX
        float-seconds timestamp (see ``completion_judge._HOLD_WINDOW_RECENCY_SEC``
        and the ``time.time()`` comparison). Converting the ``datetime`` here
        keeps the activity contract simple.
        """
        data = self._state.model_dump(mode="json", by_alias=True)
        hs = self._state.hold_window_start
        if hs is not None:
            # ``datetime.timestamp()`` handles tz-aware and tz-naive
            # instances alike; workflow.now() returns tz-aware UTC, which
            # is the correct input.
            data["hold_window_start"] = hs.timestamp()
        return data

    def _state_snapshot(self) -> dict[str, Any]:
        """Pure serialization of the MissionState — used by ``get_status``.

        Pydantic ``model_dump(mode="json", by_alias=True)`` emits ISO-8601
        strings for datetimes and plain Python types everywhere else, so
        the result is safe to return from a Temporal query (which must be
        JSON-serializable). ``by_alias=True`` makes ``CriterionState.pass_``
        serialize as ``"pass"`` — the wire name — which matches the MissionState
        round-trip tests in ``test_foundation/test_state.py``.
        """
        return self._state.model_dump(mode="json", by_alias=True)

    # ----------------------------------------------------------------- signals

    @workflow.signal
    async def finding_emitted(self, finding: dict) -> None:
        """Persist a finding to ``findings.jsonl``.

        Called by (future) child workflows AND by the verifier cycle
        itself via signal self-send. Runs the ``emit_finding`` activity
        synchronously so the on-disk mirror stays in lock-step with
        Temporal history — the spec explicitly requires this path.

        We do not have access to ``mission`` from signal handlers (the
        signal arrives asynchronously, not as a call parameter), so we
        derive the state dir from the finding payload if present,
        otherwise skip the disk mirror. Task 14 will store the state dir
        on ``self`` at workflow start so every path has it.
        """
        # Enqueue for observability / future intervention routing.
        self._pending_signal_work.append({"kind": "finding", "payload": finding})
        self._state.findings_count += 1

        # The finding may carry its state dir for routing; if not, the
        # signal is informational (Task 13 does not itself call this
        # handler). Task 14 will plumb the state dir in via __init__.
        state_dir = finding.get("__state_dir__") if isinstance(finding, dict) else None
        if state_dir:
            # Strip the routing key before writing so it doesn't leak to
            # disk.
            clean = {k: v for k, v in finding.items() if k != "__state_dir__"}
            await workflow.execute_activity(
                "emit_finding",
                args=[state_dir, clean],
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=retry_policies.EMIT_FINDING,
            )

    @workflow.signal
    async def abort(self, reason: str) -> None:
        """Request graceful mission abort.

        The main loop observes ``aborting`` at the top of its next
        iteration (or immediately if the cycle is waiting on a sleep).
        """
        self._state.abort_reason = reason
        self._state.phase = "aborting"

    @workflow.signal
    async def intervention_request(self, action: dict) -> None:
        """Placeholder — full intervention flow lives in Task 14+.

        Enqueues for observability; no state mutation.
        """
        self._pending_signal_work.append(
            {"kind": "intervention", "payload": action}
        )

    @workflow.signal
    async def force_continue_as_new(self) -> None:
        """TEST-ONLY: request a deterministic ``continue_as_new`` on next cycle.

        Production code should never send this signal. It exists so tests
        can exercise the cas chain without generating the 50k events /
        50MB of history that Temporal's
        ``is_continue_as_new_suggested`` heuristic normally needs.

        The flag lives on ``MissionState`` (not on ``self``) so it
        survives the very cas it triggers — but the trigger site clears
        it before calling ``continue_as_new``, so the resumed incarnation
        sees ``force_continue_as_new=False`` which prevents an infinite
        cas loop.
        """
        self._state.force_continue_as_new = True

    # ----------------------------------------------------------------- queries

    @workflow.query
    def get_status(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the mission state.

        Safe for external callers (``handle.query(...)``). No history
        entry is created.
        """
        return self._state_snapshot()

    @workflow.query
    def get_findings(self) -> list[dict]:
        """Return the signal-queue findings drained-but-not-yet-processed.

        Task 13 exposes only the signal-work queue for findings; future
        tasks can back this by a more structured in-state log. The on-disk
        ``findings.jsonl`` remains the durable source of truth.
        """
        return [
            item["payload"]
            for item in self._pending_signal_work
            if item.get("kind") == "finding"
        ]


# --- Result-dataclass accessors ---------------------------------------------
#
# Activities return dataclasses (e.g. ``TamperResult``, ``InvariantsResult``,
# ``CriterionCheckResult``, ``CompletionDecision``). Temporal's default JSON
# converter round-trips them losslessly; when tests use mocked activities
# they may return plain dicts. To make the workflow agnostic to which form
# arrives, we access fields through tiny helpers that prefer dataclass
# attributes and fall back to dict lookups. These are module-level (not
# workflow instance methods) so they're cheap to call and obvious at review
# time.


def _is_tamper_detected(r: Any) -> bool:
    """True iff the ``verify_tamper`` result reports tamper."""
    if hasattr(r, "detected"):
        return bool(r.detected)
    if isinstance(r, dict):
        return bool(r.get("detected"))
    return False


def _finding_of(r: Any) -> dict | None:
    """Extract the ``finding`` payload from a ``TamperResult``-like value."""
    if hasattr(r, "finding"):
        return r.finding
    if isinstance(r, dict):
        return r.get("finding")
    return None


def _findings_of(r: Any) -> list[dict]:
    """Extract the ``findings`` list from an ``InvariantsResult``-like value."""
    if hasattr(r, "findings"):
        return list(r.findings or [])
    if isinstance(r, dict):
        return list(r.get("findings") or [])
    return []


def _criterion_id_of(r: Any) -> str:
    """Extract ``criterion_id`` from a ``CriterionCheckResult``-like value."""
    if hasattr(r, "criterion_id"):
        return str(r.criterion_id)
    if isinstance(r, dict):
        return str(r.get("criterion_id", ""))
    return ""


def _pass_of(r: Any) -> bool:
    """Extract ``pass_`` from a ``CriterionCheckResult``-like value."""
    if hasattr(r, "pass_"):
        return bool(r.pass_)
    if isinstance(r, dict):
        # Support both ``pass_`` (Python attr form) and ``pass`` (JSON form).
        if "pass_" in r:
            return bool(r["pass_"])
        return bool(r.get("pass", False))
    return False


def _exit_code_of(r: Any) -> int | None:
    if hasattr(r, "exit_code"):
        return int(r.exit_code) if r.exit_code is not None else None
    if isinstance(r, dict):
        ec = r.get("exit_code")
        return int(ec) if ec is not None else None
    return None


def _stderr_tail_of(r: Any) -> str:
    if hasattr(r, "stderr_tail"):
        return str(r.stderr_tail or "")
    if isinstance(r, dict):
        return str(r.get("stderr_tail") or "")
    return ""


def _approved_of(r: Any) -> bool:
    """Extract ``approved`` from a ``CompletionDecision``-like value."""
    if hasattr(r, "approved"):
        return bool(r.approved)
    if isinstance(r, dict):
        return bool(r.get("approved"))
    return False


def _reasons_of(r: Any) -> list[str]:
    """Extract ``reasons`` list from a ``CompletionDecision``-like value."""
    if hasattr(r, "reasons"):
        return list(r.reasons or [])
    if isinstance(r, dict):
        return list(r.get("reasons") or [])
    return []
