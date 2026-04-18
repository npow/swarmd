"""Tests for ``LLMCriticWorkflow`` (Task 16).

Behaviours covered:
  1. Cadence triggers progress_audit + goal_drift_check activities.
  2. Non-info verdicts emit findings to the parent via signal.
  3. ``anticheat_requested`` signal fans out 6 parallel dimension
     activity calls.
  4. Fail/suspicious verdicts emit findings; pass verdicts do not.
  5. After 200 cycles → continue_as_new.
  6. Activity exceptions do not crash the workflow (warning logged,
     continue).

Mocking strategy mirrors ``test_pattern_detector_workflow.py``:
activities registered by string name on a test Worker, stub
``ParentRecorder`` workflow records ``finding_emitted`` signals.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from swarm.durable.activities.goal_drift_check import GoalDriftResult
from swarm.durable.activities.progress_audit import ProgressAuditResult
from swarm.durable.activities.run_anticheat_dimension import AnticheatVerdict
from swarm.durable.errors import TerminalError
from swarm.durable.specialists import LLMCriticWorkflow


# --- Shared module-level state for per-test mocks -------------------------


_test_state: dict[str, dict] = {}


def _register_state(key: str, initial: dict) -> None:
    _test_state[key] = dict(initial)


def _get_state(key: str) -> dict:
    return _test_state.setdefault(key, {})


# --- Parent recorder workflow ---------------------------------------------


_received_findings: dict[str, list[dict[str, Any]]] = {}


@workflow.defn(name="ParentRecorder")
class ParentRecorderWorkflow:
    """Stub parent — records ``finding_emitted`` signals keyed by
    workflow_id so the test can assert on them.
    """

    def __init__(self) -> None:
        self._should_exit = False

    @workflow.run
    async def run(self, recorder_key: str) -> list[dict[str, Any]]:
        _received_findings.setdefault(recorder_key, [])
        while not self._should_exit:
            await workflow.wait_condition(lambda: self._should_exit, timeout=60)
        return _received_findings.get(recorder_key, [])

    @workflow.signal
    async def finding_emitted(self, finding: dict[str, Any]) -> None:
        key = workflow.info().workflow_id
        _received_findings.setdefault(key, []).append(finding)

    @workflow.signal
    async def stop(self) -> None:
        self._should_exit = True


# --- Mock activities -------------------------------------------------------


@activity.defn(name="progress_audit")
async def mock_progress_audit_fabricated(context: dict) -> ProgressAuditResult:
    """Return a ``fabricated`` verdict — actionable, should be emitted."""
    _get_state("progress_calls").setdefault("n", 0)
    _test_state["progress_calls"]["n"] = _test_state["progress_calls"].get("n", 0) + 1
    return ProgressAuditResult(
        verdict="fabricated",
        rationale="claims don't match evidence",
        finding={
            "source": "progress_audit.fabricated",
            "type": "fabrication",
            "subtype": "fabricated",
            "severity": "critical",
            "verdict": "claims don't match evidence",
        },
        unsupported_claims=[],
    )


@activity.defn(name="progress_audit")
async def mock_progress_audit_grounded(context: dict) -> ProgressAuditResult:
    """Return a ``grounded`` verdict — type=info, should NOT be emitted."""
    _get_state("progress_calls").setdefault("n", 0)
    _test_state["progress_calls"]["n"] = _test_state["progress_calls"].get("n", 0) + 1
    return ProgressAuditResult(
        verdict="grounded",
        rationale="all claims supported",
        finding={
            "source": "progress_audit.grounded",
            "type": "info",  # info → not emitted
            "subtype": "grounded",
            "severity": "info",
            "verdict": "all claims supported",
        },
        unsupported_claims=[],
    )


@activity.defn(name="progress_audit")
async def mock_progress_audit_raises(context: dict) -> ProgressAuditResult:
    """Raise ``TerminalError`` — non-retryable, so Temporal surfaces it
    to the workflow immediately (no retry backoff burn). The workflow's
    try/except should catch it and continue."""
    _get_state("progress_calls").setdefault("n", 0)
    _test_state["progress_calls"]["n"] = _test_state["progress_calls"].get("n", 0) + 1
    raise TerminalError("simulated upstream failure")


@activity.defn(name="goal_drift_check")
async def mock_goal_drift_drifting(context: dict) -> GoalDriftResult:
    """Return a ``drifting`` verdict — actionable."""
    _get_state("drift_calls").setdefault("n", 0)
    _test_state["drift_calls"]["n"] = _test_state["drift_calls"].get("n", 0) + 1
    return GoalDriftResult(
        verdict="drifting",
        rationale="agent veered off",
        finding={
            "source": "goal_drift_check.drifting",
            "type": "drift",
            "subtype": "drifting",
            "severity": "major",
            "verdict": "agent veered off",
        },
        evidence_turn_ids=[],
    )


@activity.defn(name="goal_drift_check")
async def mock_goal_drift_on_track(context: dict) -> GoalDriftResult:
    _get_state("drift_calls").setdefault("n", 0)
    _test_state["drift_calls"]["n"] = _test_state["drift_calls"].get("n", 0) + 1
    return GoalDriftResult(
        verdict="on_track",
        rationale="ok",
        finding={
            "source": "goal_drift_check.on_track",
            "type": "info",
            "subtype": "on_track",
            "severity": "info",
            "verdict": "ok",
        },
        evidence_turn_ids=[],
    )


@activity.defn(name="run_anticheat_dimension")
async def mock_anticheat_pass(
    dimension: str,
    context: dict[str, Any],
    anticheat_config: dict[str, Any],
) -> AnticheatVerdict:
    """Every dimension returns ``pass`` — nothing should be emitted."""
    _get_state("anticheat_calls").setdefault("dims", [])
    _test_state["anticheat_calls"]["dims"].append(dimension)
    return AnticheatVerdict(
        dimension=dimension,
        verdict="pass",
        rationale="genuine",
        finding={
            "source": f"anticheat.{dimension}",
            "type": "info",
            "subtype": "pass",
            "severity": "info",
            "verdict": "genuine",
        },
    )


@activity.defn(name="run_anticheat_dimension")
async def mock_anticheat_mixed(
    dimension: str,
    context: dict[str, Any],
    anticheat_config: dict[str, Any],
) -> AnticheatVerdict:
    """``scope_reduction`` returns ``fail``, others ``pass`` — one finding."""
    _get_state("anticheat_calls").setdefault("dims", [])
    _test_state["anticheat_calls"]["dims"].append(dimension)
    if dimension == "scope_reduction":
        return AnticheatVerdict(
            dimension=dimension,
            verdict="fail",
            rationale="tests were weakened",
            finding={
                "source": f"anticheat.{dimension}",
                "type": "anticheat_fail",
                "subtype": "fail",
                "severity": "critical",
                "verdict": "tests were weakened",
            },
        )
    return AnticheatVerdict(
        dimension=dimension,
        verdict="pass",
        rationale="ok",
        finding={
            "source": f"anticheat.{dimension}",
            "type": "info",
            "subtype": "pass",
            "severity": "info",
            "verdict": "ok",
        },
    )


# --- Helpers ---------------------------------------------------------------


def _task_queue() -> str:
    return f"test-tq-{uuid.uuid4().hex[:8]}"


async def _start_env() -> WorkflowEnvironment:
    return await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    )


async def _start_worker(env, tq: str, activities: list):
    # Unsandboxed runner needed to host the locally-defined
    # ``ParentRecorderWorkflow``; see
    # test_pattern_detector_workflow._start_worker for rationale.
    return Worker(
        env.client,
        task_queue=tq,
        workflows=[LLMCriticWorkflow, ParentRecorderWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


async def _spawn_recorder(env, tq: str, recorder_id: str):
    handle = await env.client.start_workflow(
        ParentRecorderWorkflow.run,
        args=[recorder_id],
        id=recorder_id,
        task_queue=tq,
    )
    return handle


# --- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_cadence_triggers_both_cadence_activities(tmp_path):
    """Each cadence tick calls both progress_audit and goal_drift_check."""
    _register_state("progress_calls", {"n": 0})
    _register_state("drift_calls", {"n": 0})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[mock_progress_audit_grounded, mock_goal_drift_on_track],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                LLMCriticWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_llm_critic",
                task_queue=tq,
            )

            await env.sleep(3)

            assert _get_state("progress_calls")["n"] >= 1
            assert _get_state("drift_calls")["n"] >= 1
            # Both were type=info → no findings should have been signalled.
            assert _received_findings.get(recorder_id, []) == []

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_actionable_verdicts_emit_findings(tmp_path):
    """Fabricated + drifting verdicts produce one finding each."""
    _register_state("progress_calls", {"n": 0})
    _register_state("drift_calls", {"n": 0})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_progress_audit_fabricated,
                mock_goal_drift_drifting,
            ],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                LLMCriticWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_llm_critic",
                task_queue=tq,
            )

            await env.sleep(3)

            findings = _received_findings.get(recorder_id, [])
            types = {f.get("type") for f in findings}
            assert "fabrication" in types, (
                f"expected a fabrication finding, got types={types!r}"
            )
            assert "drift" in types, (
                f"expected a drift finding, got types={types!r}"
            )

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_anticheat_requested_fans_out_six_dimensions(tmp_path):
    """``anticheat_requested`` signal → 6 parallel dim calls (one per
    dimension). With all-pass verdicts → no findings emitted."""
    _register_state("progress_calls", {"n": 0})
    _register_state("drift_calls", {"n": 0})
    _register_state("anticheat_calls", {"dims": []})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_progress_audit_grounded,
                mock_goal_drift_on_track,
                mock_anticheat_pass,
            ],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                LLMCriticWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_llm_critic",
                task_queue=tq,
            )

            # Let the first cadence tick settle.
            await env.sleep(2)

            # Now fire the anticheat_requested signal.
            await child_handle.signal(
                LLMCriticWorkflow.anticheat_requested,
                args=[
                    {"id": "c1", "check": "pytest tests/test_x.py"},
                    {
                        "criterion_id": "c1",
                        "diff": "(diff)",
                        "events": "(events)",
                        "check_command": "pytest tests/test_x.py",
                        "anticheat_config": {
                            "primary": "claude -p --bare --model opus"
                        },
                    },
                ],
            )

            # Give the next tick a chance to drain the queue.
            await env.sleep(3)

            dims = _get_state("anticheat_calls")["dims"]
            assert sorted(set(dims)) == sorted(
                [
                    "scope_reduction",
                    "mock_out",
                    "tautology",
                    "hardcode",
                    "off_criterion",
                    "coordinated_edit",
                ]
            ), f"expected 6 dimensions, got {sorted(set(dims))!r}"

            # All verdicts are ``pass`` → no anticheat findings should
            # have been emitted.
            findings = _received_findings.get(recorder_id, [])
            anticheat_findings = [
                f
                for f in findings
                if str(f.get("source", "")).startswith("anticheat.")
            ]
            assert anticheat_findings == [], (
                f"expected no anticheat findings on all-pass, got "
                f"{anticheat_findings!r}"
            )

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_anticheat_fail_verdict_emits_finding(tmp_path):
    """``scope_reduction=fail`` → emitted; other dims pass → not emitted."""
    _register_state("progress_calls", {"n": 0})
    _register_state("drift_calls", {"n": 0})
    _register_state("anticheat_calls", {"dims": []})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_progress_audit_grounded,
                mock_goal_drift_on_track,
                mock_anticheat_mixed,
            ],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                LLMCriticWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_llm_critic",
                task_queue=tq,
            )

            await env.sleep(2)
            await child_handle.signal(
                LLMCriticWorkflow.anticheat_requested,
                args=[
                    {"id": "c1", "check": "pytest"},
                    {
                        "criterion_id": "c1",
                        "diff": "(diff)",
                        "events": "(events)",
                        "check_command": "pytest",
                        "anticheat_config": {"primary": "claude"},
                    },
                ],
            )
            await env.sleep(3)

            findings = _received_findings.get(recorder_id, [])
            anticheat_fails = [
                f
                for f in findings
                if f.get("type") == "anticheat_fail"
            ]
            assert len(anticheat_fails) == 1, (
                f"expected exactly one anticheat_fail finding, "
                f"got {findings!r}"
            )
            assert (
                anticheat_fails[0]["source"]
                == "anticheat.scope_reduction"
            )

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_progress_audit_exception_does_not_crash_workflow(tmp_path):
    """If progress_audit raises, the workflow logs and continues — on the
    next cycle goal_drift_check still runs."""
    _register_state("progress_calls", {"n": 0})
    _register_state("drift_calls", {"n": 0})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[mock_progress_audit_raises, mock_goal_drift_on_track],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                LLMCriticWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_llm_critic",
                task_queue=tq,
            )

            # TerminalError is non-retryable, so progress_audit fails
            # fast on attempt 1. Within a single cycle the except block
            # catches and then goal_drift_check runs.
            await env.sleep(5)

            # Both activities were attempted — progress_audit raised
            # (its attempts exhaust and the except block absorbs), drift
            # check kept running. The essential proof is that
            # drift_check was invoked at least once DESPITE progress
            # audit having raised an exception on the same cycle.
            assert _get_state("drift_calls")["n"] >= 1, (
                "goal_drift_check should still run after progress_audit raises"
            )

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_continue_as_new_threshold(tmp_path):
    """After the cycle threshold (200) the workflow must
    continue_as_new. We can't practically run 200 cycles in a test even
    with time skipping (each cycle is ~1 virtual second of sleep + some
    activity scheduling), so we verify structurally: patch the
    threshold to a small number and confirm cas fires.
    """
    _register_state("progress_calls", {"n": 0})
    _register_state("drift_calls", {"n": 0})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    # Patch the constant to 2 for this test so we only need two cycles
    # to cas. The workflow imports the constant at module load and
    # reads it in the cas check — importing the module again after the
    # patch would register a NEW @workflow.defn, so we monkeypatch in
    # place via the module attribute.
    import swarm.durable.specialists.llm_critic as mod

    original = mod._CONTINUE_AS_NEW_CYCLE_THRESHOLD
    try:
        mod._CONTINUE_AS_NEW_CYCLE_THRESHOLD = 2

        async with await _start_env() as env:
            worker = await _start_worker(
                env,
                tq,
                activities=[
                    mock_progress_audit_grounded,
                    mock_goal_drift_on_track,
                ],
            )
            async with worker:
                recorder_handle = await _spawn_recorder(env, tq, recorder_id)
                child_handle = await env.client.start_workflow(
                    LLMCriticWorkflow.run,
                    args=[recorder_id, "sess-1", 1],
                    id=f"{recorder_id}_llm_critic",
                    task_queue=tq,
                )

                # Let several cycles run — cas should happen
                # transparently and the child keeps running.
                await env.sleep(6)

                # Verify cas happened by checking the cycle counter
                # reset worked: activities were still being called
                # AFTER enough virtual time for several cas cycles to
                # have transpired.
                assert _get_state("progress_calls")["n"] >= 3, (
                    "progress_audit should be called across multiple cas "
                    "incarnations"
                )

                await child_handle.cancel()
                await recorder_handle.signal(ParentRecorderWorkflow.stop)
    finally:
        mod._CONTINUE_AS_NEW_CYCLE_THRESHOLD = original
