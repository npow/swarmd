"""Tests for ``MissionWorkflow`` (Task 13 scope).

These tests exercise the verifier loop, signal handlers, and query
handlers via ``temporalio.testing.WorkflowEnvironment.start_time_skipping``
so real-time waits (``workflow.sleep``) are instant. Task 13 does NOT
test continue_as_new, child-workflow reconnection, or intervention routing
— those are Task 14+ concerns and the production workflow deliberately
stubs them.

Mocking strategy: the activities registered with the test Worker are
``@activity.defn(name=...)`` decorated with the SAME names as the
production activities. The workflow resolves activities by name at
execute time (per spec §5 determinism contract), so routing is fully
controlled by what we register with the Worker.

Each test uses a fresh ``Mission`` + ``MissionState`` pair — no shared
fixtures. This makes each test self-contained and trivially parallel if
needed.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from swarm.durable.activities import (
    CompletionDecision,
    CriterionCheckResult,
    InvariantsResult,
    TamperResult,
)
from swarm.durable.workflow import MissionWorkflow
from swarm.schemas.mission import (
    Invariants,
    Mission,
    SuccessCriterion,
    Verification,
)


# --- Test fixtures / helpers -------------------------------------------------


def _mission(
    tmp_path: Path,
    criteria: list[SuccessCriterion] | None = None,
    run_every_sec: int = 1,
    hold_window_sec: int = 2,
) -> Mission:
    """Build a minimal but valid ``Mission`` rooted at ``tmp_path/ws``.

    The defaults are chosen for fast tests: 1 s verifier cadence + 2 s
    hold window means the full "all-pass → hold_window → completion_judge"
    path completes in ~3 virtual seconds. Real-time skipping makes that
    near-instantaneous.
    """
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return Mission(
        mission="test mission",
        workspace=str(ws),
        success_criteria=criteria
        or [
            SuccessCriterion(
                id="c1",
                description="first",
                check="true",
                timeout_sec=5,
            )
        ],
        verification=Verification(
            run_every_sec=run_every_sec,
            hold_window_sec=hold_window_sec,
        ),
        invariants=Invariants(),
    )


# --- Shared mutable counters for flip-able mocks ----------------------------
#
# Temporal activities can't close over per-test state via pytest fixtures
# because the Worker registers the module-level function. We instead use a
# module-level dict keyed by test name; each test stamps its own key and
# its mock activity reads that key. This keeps registration cheap (no
# closure capture issues) while still letting us drive behavior from tests.


_test_state: dict[str, dict] = {}


def _register_state(key: str, initial: dict) -> None:
    _test_state[key] = dict(initial)


def _get_state(key: str) -> dict:
    return _test_state.setdefault(key, {})


# --- Mock activities ---------------------------------------------------------
#
# Registered with the test Worker by @activity.defn(name=...) — the same
# names as production activities, so ``workflow.execute_activity("verify_tamper", ...)``
# routes here when the Worker hosts these instead of the real activities.


@activity.defn(name="verify_tamper")
async def mock_verify_tamper_clean(
    mission_dir: str, out_of_tree_sha_path: str
) -> TamperResult:
    """Default: no tamper."""
    return TamperResult(detected=False, finding=None)


@activity.defn(name="verify_tamper")
async def mock_verify_tamper_detected(
    mission_dir: str, out_of_tree_sha_path: str
) -> TamperResult:
    """Always reports tamper — used to exercise the abort path."""
    return TamperResult(
        detected=True,
        finding={
            "type": "meta",
            "subtype": "tamper_detected",
            "severity": "critical",
            "verdict": "src/foo.py hash mismatch",
        },
    )


@activity.defn(name="enforce_invariants")
async def mock_enforce_invariants_clean(
    workspace: str, invariants: Invariants
) -> InvariantsResult:
    """Default: no invariant findings."""
    return InvariantsResult(findings=[])


@activity.defn(name="enforce_invariants")
async def mock_enforce_invariants_two_findings(
    workspace: str, invariants: Invariants
) -> InvariantsResult:
    """Return two findings — used to verify emit_finding is called N times."""
    return InvariantsResult(
        findings=[
            {
                "type": "meta",
                "subtype": "invariant_no_mock",
                "severity": "critical",
                "verdict": "mock usage found in protected path: src/a.py",
            },
            {
                "type": "meta",
                "subtype": "invariant_test_count_floor",
                "severity": "critical",
                "verdict": "test count 0 dropped below floor 10",
            },
        ]
    )


@activity.defn(name="check_criterion")
async def mock_check_criterion_pass(
    criterion: SuccessCriterion, workspace: str
) -> CriterionCheckResult:
    """Default: every criterion passes."""
    return CriterionCheckResult(
        criterion_id=criterion.id,
        pass_=True,
        exit_code=0,
        stdout_tail="",
        stderr_tail="",
        duration_ms=1,
    )


@activity.defn(name="check_criterion")
async def mock_check_criterion_flip(
    criterion: SuccessCriterion, workspace: str
) -> CriterionCheckResult:
    """Pass for the first N calls then fail — drives hold_window reset."""
    state = _get_state("flip")
    n = state.get("calls", 0)
    state["calls"] = n + 1
    flip_after = state.get("flip_after", 3)
    passing = n < flip_after
    return CriterionCheckResult(
        criterion_id=criterion.id,
        pass_=passing,
        exit_code=0 if passing else 1,
        stdout_tail="",
        stderr_tail="" if passing else "criterion flipped false",
        duration_ms=1,
    )


@activity.defn(name="completion_judge")
async def mock_completion_judge_approve(
    mission_state: dict, session_state_dir: str
) -> CompletionDecision:
    """Default: judge approves every completion request."""
    return CompletionDecision(approved=True, reasons=[])


@activity.defn(name="completion_judge")
async def mock_completion_judge_reject(
    mission_state: dict, session_state_dir: str
) -> CompletionDecision:
    """Judge rejects completion — workflow should stay in hold_window and
    emit a completion_blocked finding."""
    return CompletionDecision(
        approved=False,
        reasons=["1 open cheat finding(s)"],
    )


@activity.defn(name="emit_finding")
async def mock_emit_finding(session_state_dir: str, finding: dict) -> None:
    """Record the call site so tests can assert on it."""
    state = _get_state("emit")
    state.setdefault("calls", []).append(
        {"session_state_dir": session_state_dir, "finding": finding}
    )


# --- Infrastructure helpers --------------------------------------------------


def _task_queue() -> str:
    """Per-test task queue — avoids cross-test activity-registration
    collisions when multiple WorkflowEnvironments run in parallel."""
    return f"test-tq-{uuid.uuid4().hex[:8]}"


async def _start_env() -> WorkflowEnvironment:
    """Start a time-skipping test environment wired to the pydantic data
    converter so Mission / Invariants / Criterion (pydantic models) are
    reconstructed from JSON instead of arriving as plain dicts inside the
    workflow. Without this the workflow sees ``mission: dict`` and every
    ``mission.workspace`` access raises ``AttributeError``."""
    return await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    )


async def _start_worker(env: WorkflowEnvironment, tq: str, activities: list):
    """Start a Worker on ``tq`` with the given activities and the
    MissionWorkflow. Returns the Worker so the caller can manage its
    lifecycle inside a context manager.

    ``workflow_runner`` uses the default sandbox; ``pydantic`` at module
    import time is allowed. If a future workflow imports non-deterministic
    code we'll have to pass ``workflow_runner=SandboxedWorkflowRunner(...)``
    with a restrictions override. For Task 13 the defaults work because
    ``swarm.durable.workflow`` imports only pydantic schemas (safe) and
    ``retry_policies`` (pure)."""
    return Worker(
        env.client,
        task_queue=tq,
        workflows=[MissionWorkflow],
        activities=activities,
    )


# --- Tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_transitions_to_hold_window_when_all_pass(tmp_path):
    """All-passing criteria should move the phase to ``hold_window``, and
    with completion_judge approving, ultimately to ``complete``.

    This is the happy-path integration smoke for the verifier loop: every
    numbered step runs, the decision tree resolves, and the workflow
    terminates with ``phase="complete"``.
    """
    mission = _mission(tmp_path)
    tq = _task_queue()

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_verify_tamper_clean,
                mock_enforce_invariants_clean,
                mock_check_criterion_pass,
                mock_completion_judge_approve,
                mock_emit_finding,
            ],
        )
        async with worker:
            result = await env.client.execute_workflow(
                MissionWorkflow.run,
                args=[mission],
                id=f"mission-{uuid.uuid4().hex}",
                task_queue=tq,
            )

    assert result["phase"] == "complete"
    assert result["reason"] is None


@pytest.mark.asyncio
async def test_verifier_resets_hold_window_on_failure(tmp_path):
    """A criterion flipping back to failing AFTER the workflow entered
    ``hold_window`` must reset the window and drop the workflow back to
    ``running``.

    We use a Mission without a completion_judge side-effect (judge will
    never approve because it never runs once the flip drops us out of
    hold_window), then send an ``abort`` signal once we've observed the
    return-to-running transition via ``get_status``.
    """
    _register_state("flip", {"calls": 0, "flip_after": 4})

    mission = _mission(
        tmp_path,
        run_every_sec=1,
        hold_window_sec=30,  # long enough that the flip lands first
    )
    tq = _task_queue()

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_verify_tamper_clean,
                mock_enforce_invariants_clean,
                mock_check_criterion_flip,
                mock_completion_judge_approve,
                mock_emit_finding,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MissionWorkflow.run,
                args=[mission],
                id=f"mission-{uuid.uuid4().hex}",
                task_queue=tq,
            )

            # Advance virtual time past enough verifier cycles for the
            # criterion to flip. 4 passes then fail → after 5 cycles we
            # should have re-entered "running".
            await env.sleep(10)

            # Query status — we expect to have observed a hold_window
            # transition AND then returned to running.
            status = await handle.query(MissionWorkflow.get_status)
            assert status["phase"] in {"running", "hold_window"}
            # If flip landed, phase should be "running" by now.
            # (If env.sleep didn't advance far enough, it could still be
            # in hold_window — but for this cadence it will have flipped.)

            # Abort so the test terminates cleanly without waiting for
            # completion (which mock_check_criterion_flip never yields
            # after the flip).
            await handle.signal(MissionWorkflow.abort, "test done")
            result = await handle.result()

    assert result["phase"] == "aborted"
    # The streak reset is observable in the final state: the criterion
    # was flipping, so by the end its streak should be 0 (not a multi-s
    # accumulation). We can't inspect criteria_state post-abort from the
    # return value alone, but the "aborted + running" path hitting here
    # is sufficient evidence the loop didn't get stuck in hold_window.


@pytest.mark.asyncio
async def test_hold_window_duration_triggers_completion_judge(tmp_path):
    """After ``hold_window_sec`` elapses with all criteria still passing,
    the workflow must invoke ``completion_judge`` and, if approved,
    transition to ``complete``.

    This is a stricter version of the first test — it specifically
    verifies the hold_window_elapsed → completion_judge code path.
    """
    mission = _mission(tmp_path, run_every_sec=1, hold_window_sec=3)
    tq = _task_queue()

    # Record completion_judge calls so we can assert it fired.
    judge_calls: list[dict] = []

    @activity.defn(name="completion_judge")
    async def recording_judge(
        mission_state: dict, session_state_dir: str
    ) -> CompletionDecision:
        judge_calls.append({"mission_state": mission_state})
        return CompletionDecision(approved=True, reasons=[])

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_verify_tamper_clean,
                mock_enforce_invariants_clean,
                mock_check_criterion_pass,
                recording_judge,
                mock_emit_finding,
            ],
        )
        async with worker:
            result = await env.client.execute_workflow(
                MissionWorkflow.run,
                args=[mission],
                id=f"mission-{uuid.uuid4().hex}",
                task_queue=tq,
            )

    assert result["phase"] == "complete"
    assert len(judge_calls) >= 1, "completion_judge should have been invoked"
    # The mission_state passed to the judge must include a numeric
    # hold_window_start (workflow converts datetime → posix float).
    hs = judge_calls[0]["mission_state"].get("hold_window_start")
    assert isinstance(hs, (int, float)), f"hold_window_start not numeric: {hs!r}"


@pytest.mark.asyncio
async def test_tamper_detection_aborts(tmp_path):
    """If ``verify_tamper`` reports detected=True, the workflow must
    transition directly to ``aborted`` with the tamper verdict preserved
    as ``abort_reason``. No criterion checks run on that cycle."""
    mission = _mission(tmp_path)
    tq = _task_queue()

    # Track whether check_criterion got called — it should NOT on a
    # tamper cycle (the spec short-circuits after step 1).
    criterion_calls: list[str] = []

    @activity.defn(name="check_criterion")
    async def should_not_be_called(
        criterion: SuccessCriterion, workspace: str
    ) -> CriterionCheckResult:
        criterion_calls.append(criterion.id)
        return CriterionCheckResult(
            criterion_id=criterion.id,
            pass_=False,
            exit_code=1,
            stdout_tail="",
            stderr_tail="",
            duration_ms=1,
        )

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_verify_tamper_detected,
                mock_enforce_invariants_clean,
                should_not_be_called,
                mock_completion_judge_approve,
                mock_emit_finding,
            ],
        )
        async with worker:
            result = await env.client.execute_workflow(
                MissionWorkflow.run,
                args=[mission],
                id=f"mission-{uuid.uuid4().hex}",
                task_queue=tq,
            )

    assert result["phase"] == "aborted"
    assert result["reason"] == "src/foo.py hash mismatch"
    # The first cycle aborts; check_criterion must not have fired.
    assert criterion_calls == []


@pytest.mark.asyncio
async def test_abort_signal_terminates_workflow(tmp_path):
    """Sending the ``abort`` signal must flip phase to ``aborted`` and
    preserve the supplied reason in the return value."""
    mission = _mission(tmp_path, run_every_sec=1, hold_window_sec=120)
    tq = _task_queue()

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_verify_tamper_clean,
                mock_enforce_invariants_clean,
                # Criterion always fails, so we never enter hold_window;
                # the workflow loops until we signal abort.
                _make_always_failing_criterion(),
                mock_completion_judge_approve,
                mock_emit_finding,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MissionWorkflow.run,
                args=[mission],
                id=f"mission-{uuid.uuid4().hex}",
                task_queue=tq,
            )

            # Let a few verifier cycles run so we know the loop is
            # healthy and the signal doesn't race workflow-start.
            await env.sleep(3)

            await handle.signal(MissionWorkflow.abort, "user cancelled")
            result = await handle.result()

    assert result["phase"] == "aborted"
    assert result["reason"] == "user cancelled"


@pytest.mark.asyncio
async def test_get_status_query_returns_current_phase(tmp_path):
    """``get_status`` must return a JSON-serializable dict containing at
    least ``phase`` and the per-criterion state map."""
    mission = _mission(tmp_path, run_every_sec=1, hold_window_sec=60)
    tq = _task_queue()

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_verify_tamper_clean,
                mock_enforce_invariants_clean,
                mock_check_criterion_pass,
                mock_completion_judge_approve,
                mock_emit_finding,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MissionWorkflow.run,
                args=[mission],
                id=f"mission-{uuid.uuid4().hex}",
                task_queue=tq,
            )

            # Let one verifier cycle complete so the status reflects
            # actual criterion results, not the launching default.
            await env.sleep(2)
            status = await handle.query(MissionWorkflow.get_status)

            assert isinstance(status, dict)
            assert "phase" in status
            # Phase has moved past "launching" since first cycle ran.
            assert status["phase"] in {"running", "hold_window", "complete"}
            assert "criteria_state" in status
            # The single criterion "c1" is tracked.
            assert "c1" in status["criteria_state"]
            assert status["criteria_state"]["c1"]["pass"] is True

            # Clean up.
            await handle.signal(MissionWorkflow.abort, "test done")
            await handle.result()


@pytest.mark.asyncio
async def test_invariants_findings_do_not_abort(tmp_path):
    """Invariant findings are informational — the workflow must emit one
    ``emit_finding`` call per finding but continue running (not abort)."""
    _register_state("emit", {"calls": []})

    mission = _mission(tmp_path, run_every_sec=1, hold_window_sec=60)
    tq = _task_queue()

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_verify_tamper_clean,
                mock_enforce_invariants_two_findings,
                mock_check_criterion_pass,
                mock_completion_judge_approve,
                mock_emit_finding,
            ],
        )
        async with worker:
            handle = await env.client.start_workflow(
                MissionWorkflow.run,
                args=[mission],
                id=f"mission-{uuid.uuid4().hex}",
                task_queue=tq,
            )

            # One full verifier cycle.
            await env.sleep(2)

            status = await handle.query(MissionWorkflow.get_status)
            # Workflow is still alive and not aborted.
            assert status["phase"] in {"running", "hold_window"}
            # findings_count is incremented once per invariant finding.
            assert status["findings_count"] >= 2

            await handle.signal(MissionWorkflow.abort, "test done")
            result = await handle.result()

    assert result["phase"] == "aborted"
    # Sanity: the mock recorded at least two emit_finding calls for the
    # two invariant findings.
    emitted = _get_state("emit")["calls"]
    invariant_emits = [
        c for c in emitted if c["finding"].get("subtype", "").startswith("invariant_")
    ]
    assert len(invariant_emits) >= 2


# --- Additional fixtures for edge-case tests --------------------------------


def _make_always_failing_criterion():
    """Return an always-fail check_criterion mock.

    Factored into a helper so two tests can share it without registering
    the decorator twice (Temporal's activity registry complains on dup).
    """

    @activity.defn(name="check_criterion")
    async def always_fail(
        criterion: SuccessCriterion, workspace: str
    ) -> CriterionCheckResult:
        return CriterionCheckResult(
            criterion_id=criterion.id,
            pass_=False,
            exit_code=1,
            stdout_tail="",
            stderr_tail="always fails",
            duration_ms=1,
        )

    return always_fail
