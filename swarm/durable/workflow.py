"""``MissionWorkflow`` — durable parent workflow per spec §6.2.

Task 13 scope: **skeleton + verifier loop + signals + queries, first-launch
only.** Task 14 will add ``continue_as_new`` with child-reconnection and
child-workflow starts; until then, the workflow spawns no children and never
triggers ``continue_as_new``. The ``_start_children`` stub below raises
``NotImplementedError`` but is gated behind ``if not carry`` so Task 13 tests
(which always pass ``carry=None``) never hit it.

The verifier loop implements the 4 numbered steps in spec §6.2 (lines
107-139) exactly:

    1. Tamper check (``verify_tamper``)
    2. Invariant enforcement (``enforce_invariants``)
    3. Criterion checks in parallel (``check_criterion`` fan-out), then the
       pass-transition / hold-window / completion-judge decision tree.
    4. History-bounding (``continue_as_new``) — STUB for Task 14.

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
with workflow.unsafe.imports_passed_through():
    from swarm.durable import retry_policies
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
        # resumed from continue_as_new. Task 14 will populate carry.
        self._state = carry_state if carry_state is not None else MissionState.empty()
        if carry_state is None:
            self._state.phase = "running"

        # Task 14: start child workflows on first launch only. For Task 13
        # we leave a gated stub so a misconfigured test can't silently enter
        # a code path that would hang waiting for nonexistent children.
        if carry_state is None:
            # Reserved for Task 14 — do nothing in Task 13.
            # self._start_children(mission)
            pass

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

            # Task 14: history-bounding via continue_as_new.
            # if workflow.info().is_continue_as_new_suggested():
            #     workflow.continue_as_new(
            #         self.run, args=[mission, self._state]
            #     )

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

        # Step 4 — History-bounding. Task 14 will fill this in; for now
        # we deliberately do nothing so the workflow history grows
        # unbounded. At expected Task 13 test cadences (verifier_run_every
        # = 1s, hold_window = 2s, durations < 30s) this is safe.
        #
        # Task 14:
        #   if workflow.info().is_continue_as_new_suggested():
        #       workflow.continue_as_new(
        #           self.run, args=[mission, self._state]
        #       )

    # ------------------------------------------------------------ task 14 stub

    def _start_children(self, mission: Mission) -> None:
        """Start the three observer child workflows on first launch.

        Reserved for Task 14. This body must not execute in Task 13 — it is
        gated behind ``if not carry`` in ``run`` where ``carry`` is always
        ``None`` for first launch. The explicit ``NotImplementedError`` is a
        defence against accidental invocation during test-time refactoring.
        """
        raise NotImplementedError("reserved for Task 14")

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
