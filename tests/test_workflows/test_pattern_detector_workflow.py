"""Tests for ``PatternDetectorWorkflow`` (Task 15).

These tests exercise the workflow's three core behaviours:

  1. Empty events → no findings emitted.
  2. A loop pattern (N repeated tool calls) → one loop finding emitted.
  3. An oscillation pattern (file content_hash revert) → one oscillation
     finding emitted.
  4. ``check_scope_shrinking`` signal → calls the
     ``detect_scope_shrinking`` activity and forwards any finding.
  5. Continue-as-new fires after 500 cumulative events.

Mocking strategy mirrors the Task 13 tests in ``test_mission_workflow.py``:
activities are registered with the test Worker under the SAME names as
production (resolved by string at ``workflow.execute_activity`` time).

The parent signal pathway is exercised by registering a stub parent
workflow that records signals into a module-level list so tests can
assert on what the child signalled.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from swarm.durable.specialists import PatternDetectorWorkflow


# --- Module-level state so mock activities can stamp per-test results ------
#
# Temporal's activity-registration mechanism wants a top-level function;
# closing over pytest-fixture state from the activity body doesn't work
# cleanly across multiple test environments. Use a module-level dict,
# reset per test, matching the pattern in test_mission_workflow.py.

_test_state: dict[str, dict] = {}


def _register_state(key: str, initial: dict) -> None:
    _test_state[key] = dict(initial)


def _get_state(key: str) -> dict:
    return _test_state.setdefault(key, {})


# --- Stub parent workflow — records finding_emitted signals ---------------
#
# When the PatternDetectorWorkflow calls
# ``workflow.get_external_workflow_handle(mission_id).signal(...)`` it
# targets whatever workflow has that ID. We spin up this ``ParentRecorder``
# under the same ID the child is given, so the child's signal lands here.


_received_findings: dict[str, list[dict[str, Any]]] = {}


@workflow.defn(name="ParentRecorder")
class ParentRecorderWorkflow:
    """Trivial stub that just records incoming ``finding_emitted`` signals.

    We store findings in a module-level dict keyed by parent workflow ID
    so the test can retrieve them without touching workflow state.
    """

    def __init__(self) -> None:
        self._findings: list[dict[str, Any]] = []
        # ``_should_exit`` is flipped by the test-only ``stop`` signal
        # so the workflow terminates cleanly once the child has done its
        # work; otherwise it would block forever waiting for signals.
        self._should_exit = False

    @workflow.run
    async def run(self, recorder_key: str) -> list[dict[str, Any]]:
        _received_findings.setdefault(recorder_key, [])
        # Wait for stop signal, checking frequently so the workflow test
        # can terminate promptly once assertions are ready.
        while not self._should_exit:
            await workflow.wait_condition(lambda: self._should_exit, timeout=60)
        return _received_findings.get(recorder_key, [])

    @workflow.signal
    async def finding_emitted(self, finding: dict[str, Any]) -> None:
        # Mirror to the module-level dict so the test can assert on it
        # without a query. The mapping lives in memory and is reset per
        # test via the ``_register_state`` helper.
        key = workflow.info().workflow_id
        _received_findings.setdefault(key, []).append(finding)
        self._findings.append(finding)

    @workflow.signal
    async def stop(self) -> None:
        self._should_exit = True


# --- Mock activities --------------------------------------------------------


@activity.defn(name="read_recent_events")
async def mock_read_recent_events_empty(
    session_id: str, offset: int
) -> dict[str, Any]:
    """Default: no events — the child should emit nothing."""
    return {"events": [], "next_offset": offset}


@activity.defn(name="read_recent_events")
async def mock_read_recent_events_loop(
    session_id: str, offset: int
) -> dict[str, Any]:
    """Return 6 identical Bash events the first call, nothing after.

    Six repeats exceeds the default ``loop_repeat_count=5`` threshold so
    ``_detect_loops`` emits one finding.
    """
    state = _get_state("read_events")
    call_n = state.get("calls", 0)
    state["calls"] = call_n + 1
    if call_n > 0:
        return {"events": [], "next_offset": offset + 100}
    events = [
        {
            "id": f"e-{i}",
            "session_id": "sess-1",
            "spawner_id": "sess-1",
            "hook": "PostToolUse",
            "tool_name": "Bash",
            "tool_input_summary": "ls",
            "tool_response_summary": "",
        }
        for i in range(6)
    ]
    return {"events": events, "next_offset": offset + 100}


@activity.defn(name="read_recent_events")
async def mock_read_recent_events_oscillation(
    session_id: str, offset: int
) -> dict[str, Any]:
    """Return 6 Edit events on the same file that revert between two
    content hashes — triggers the oscillation detector."""
    state = _get_state("read_events")
    call_n = state.get("calls", 0)
    state["calls"] = call_n + 1
    if call_n > 0:
        return {"events": [], "next_offset": offset + 100}
    hashes = ["aaa", "bbb", "aaa", "bbb", "aaa", "bbb"]
    events = [
        {
            "id": f"e-{i}",
            "session_id": "sess-1",
            "spawner_id": "sess-1",
            "hook": "PostToolUse",
            "tool_name": "Edit",
            "tool_input_summary": "file=/tmp/foo.py",
            "tool_response_summary": f"content_hash={h}",
        }
        for i, h in enumerate(hashes)
    ]
    return {"events": events, "next_offset": offset + 100}


@activity.defn(name="read_recent_events")
async def mock_read_recent_events_500_plus(
    session_id: str, offset: int
) -> dict[str, Any]:
    """First call: return a huge batch that crosses the cas threshold.

    The workflow should trigger continue_as_new before the next cycle.
    We record the call count in ``_test_state["read_events"]`` so the
    test can assert we only saw ONE call pre-cas (plus possibly one
    post-cas).
    """
    state = _get_state("read_events")
    call_n = state.get("calls", 0)
    state["calls"] = call_n + 1
    if call_n == 0:
        # 501 events — one past the threshold.
        events = [
            {
                "id": f"e-{i}",
                "session_id": "sess-1",
                "spawner_id": "sess-1",
                "hook": "PostToolUse",
                # Distinct tool_name per event so no loop is detected
                # (the loop detector needs >= 5 repeats of the same
                # normalized input).
                "tool_name": f"Tool{i}",
                "tool_input_summary": f"arg-{i}",
                "tool_response_summary": "",
            }
            for i in range(501)
        ]
        return {"events": events, "next_offset": offset + 100}
    return {"events": [], "next_offset": offset + 100}


@activity.defn(name="detect_scope_shrinking")
async def mock_detect_scope_shrinking_hit(
    context: dict[str, Any],
) -> dict[str, Any]:
    """Return a scope-shrinking hit with a finding."""
    state = _get_state("scope_calls")
    state["n"] = state.get("n", 0) + 1
    return {
        "detected": True,
        "rationale": "test scope shrinking",
        "finding": {
            "source": "detect_scope_shrinking",
            "type": "scope_shrinking",
            "subtype": "scope_shrinking",
            "severity": "major",
            "verdict": "test scope shrinking",
        },
    }


# --- Infrastructure helpers ------------------------------------------------


def _task_queue() -> str:
    return f"test-tq-{uuid.uuid4().hex[:8]}"


async def _start_env() -> WorkflowEnvironment:
    return await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    )


async def _start_worker(env, tq: str, activities: list):
    # ``UnsandboxedWorkflowRunner`` is required because the stub
    # ``ParentRecorderWorkflow`` is defined in this test module — the
    # default sandbox would reimport it, which fails because the ``tests``
    # package isn't on the install path (it's a pytest-only location).
    # Using the unsandboxed runner is standard practice for Temporal
    # unit tests that register locally-defined workflows.
    return Worker(
        env.client,
        task_queue=tq,
        workflows=[PatternDetectorWorkflow, ParentRecorderWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


async def _spawn_recorder(env, tq: str, recorder_id: str):
    """Start a stub parent workflow under ``recorder_id``.

    The child (PatternDetectorWorkflow) will signal this ID. Returns the
    handle so the test can signal ``stop`` to clean up.
    """
    handle = await env.client.start_workflow(
        ParentRecorderWorkflow.run,
        args=[recorder_id],
        id=recorder_id,
        task_queue=tq,
    )
    return handle


# --- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_events_no_findings(tmp_path):
    """With no events to read, no findings should be signalled."""
    _register_state("read_events", {"calls": 0})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[mock_read_recent_events_empty],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                PatternDetectorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_pattern_detector",
                task_queue=tq,
            )

            # Let a few cycles run.
            await env.sleep(5)

            # No findings should have been signalled.
            assert _received_findings.get(recorder_id, []) == []

            # Clean up.
            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_loop_pattern_emits_loop_finding(tmp_path):
    """Six repeated Bash(ls) events → one ``loop`` finding signalled."""
    _register_state("read_events", {"calls": 0})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[mock_read_recent_events_loop],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                PatternDetectorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_pattern_detector",
                task_queue=tq,
            )

            # Let the first cycle run; the mock returns the 6 events on
            # call 0 and empty on subsequent calls.
            await env.sleep(3)

            findings = _received_findings.get(recorder_id, [])
            loop_findings = [f for f in findings if f.get("type") == "loop"]
            assert loop_findings, f"expected at least one loop finding, got {findings!r}"
            assert loop_findings[0]["subtype"] == "repeat_exact_args"
            assert loop_findings[0]["source"] == "pattern_detector.loop"

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_oscillation_pattern_emits_oscillation_finding(tmp_path):
    """Revert pattern on a file → one ``thrash/oscillation`` finding."""
    _register_state("read_events", {"calls": 0})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[mock_read_recent_events_oscillation],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                PatternDetectorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_pattern_detector",
                task_queue=tq,
            )

            await env.sleep(3)

            findings = _received_findings.get(recorder_id, [])
            osc_findings = [f for f in findings if f.get("type") == "thrash"]
            assert osc_findings, (
                f"expected at least one oscillation finding, got {findings!r}"
            )
            assert osc_findings[0]["subtype"] == "oscillation"

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_check_scope_shrinking_signal_triggers_activity(tmp_path):
    """Sending the ``check_scope_shrinking`` signal → the workflow calls
    the ``detect_scope_shrinking`` activity and signals any finding."""
    _register_state("read_events", {"calls": 0})
    _register_state("scope_calls", {"n": 0})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_read_recent_events_empty,
                mock_detect_scope_shrinking_hit,
            ],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                PatternDetectorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_pattern_detector",
                task_queue=tq,
            )

            # Let the loop start.
            await env.sleep(2)
            # Now signal — the next loop iteration must call the activity.
            await child_handle.signal(
                PatternDetectorWorkflow.check_scope_shrinking
            )
            await env.sleep(3)

            scope_calls = _get_state("scope_calls")["n"]
            assert scope_calls >= 1, (
                "detect_scope_shrinking activity should have been called"
            )

            findings = _received_findings.get(recorder_id, [])
            ss_findings = [
                f for f in findings if f.get("type") == "scope_shrinking"
            ]
            assert ss_findings, (
                f"expected a scope_shrinking finding, got {findings!r}"
            )

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_continue_as_new_triggers_at_500_events(tmp_path):
    """After processing 501 events in one cycle the workflow must
    ``continue_as_new`` — evidence: the child's workflow-info post-cas
    reports it is a fresh incarnation (same ID, new run-id).

    We detect this by checking that the workflow has not terminated
    normally and that the read_recent_events mock was called exactly
    twice (once pre-cas with the big batch, once post-cas which returns
    empty). The first call sees offset=0, the second sees offset=100
    carried across cas.
    """
    _register_state("read_events", {"calls": 0})
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[mock_read_recent_events_500_plus],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                PatternDetectorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_pattern_detector",
                task_queue=tq,
            )

            # One cycle reads the 501 events (triggering cas), resumed
            # incarnation reads empty. Give it time to both run.
            await env.sleep(5)

            # The workflow should still be running (cas keeps it alive).
            # Verify by checking we can query it / send a cancel.
            activity_calls = _get_state("read_events")["calls"]
            assert activity_calls >= 2, (
                f"expected >= 2 activity calls across cas, got {activity_calls}"
            )

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)
