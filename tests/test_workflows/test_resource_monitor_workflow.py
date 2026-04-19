"""Tests for ``ResourceMonitorWorkflow`` (Task 17).

Behaviours covered:
  1. All three checks return healthy → no findings emitted.
  2. ``check_zombies`` reports a finding → forwarded to parent.
  3. ``check_memory`` reports a finding → forwarded to parent.
  4. ``check_disk`` reports a finding → forwarded to parent.
  5. One activity raises → others still run and emit (resilience).

Mocking strategy matches the sister workflow tests.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from swarmd.durable.specialists import ResourceMonitorWorkflow


_received_findings: dict[str, list[dict[str, Any]]] = {}


@workflow.defn(name="ParentRecorder")
class ParentRecorderWorkflow:
    """Records ``finding_emitted`` signals keyed by workflow ID."""

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


def _zombie_finding(mission_id: str) -> dict[str, Any]:
    return {
        "source": "resource_monitor.zombies",
        "type": "meta",
        "subtype": "zombies",
        "severity": "major",
        "verdict": "7 zombies",
        "mission_id": mission_id,
    }


def _memory_finding(mission_id: str) -> dict[str, Any]:
    return {
        "source": "resource_monitor.memory_pressure",
        "type": "meta",
        "subtype": "memory_pressure",
        "severity": "critical",
        "verdict": "used memory >= critical",
        "mission_id": mission_id,
    }


def _disk_finding(mission_id: str) -> dict[str, Any]:
    return {
        "source": "resource_monitor.disk_warning",
        "type": "meta",
        "subtype": "disk_warning",
        "severity": "major",
        "verdict": "fs at 87%",
        "mission_id": mission_id,
    }


@activity.defn(name="check_zombies")
async def mock_check_zombies_empty(mission_id: str) -> list[dict[str, Any]]:
    return []


@activity.defn(name="check_zombies")
async def mock_check_zombies_hit(mission_id: str) -> list[dict[str, Any]]:
    return [_zombie_finding(mission_id)]


@activity.defn(name="check_zombies")
async def mock_check_zombies_raises(mission_id: str) -> list[dict[str, Any]]:
    raise RuntimeError("ps failed")


@activity.defn(name="check_memory")
async def mock_check_memory_empty(mission_id: str) -> list[dict[str, Any]]:
    return []


@activity.defn(name="check_memory")
async def mock_check_memory_hit(mission_id: str) -> list[dict[str, Any]]:
    return [_memory_finding(mission_id)]


@activity.defn(name="check_disk")
async def mock_check_disk_empty(mission_id: str) -> list[dict[str, Any]]:
    return []


@activity.defn(name="check_disk")
async def mock_check_disk_hit(mission_id: str) -> list[dict[str, Any]]:
    return [_disk_finding(mission_id)]


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
        workflows=[ResourceMonitorWorkflow, ParentRecorderWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    )


async def _spawn_recorder(env, tq: str, recorder_id: str):
    return await env.client.start_workflow(
        ParentRecorderWorkflow.run,
        args=[recorder_id],
        id=recorder_id,
        task_queue=tq,
    )


# --- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_healthy_no_findings(tmp_path):
    """Every check returns healthy → no findings signalled."""
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_check_zombies_empty,
                mock_check_memory_empty,
                mock_check_disk_empty,
            ],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                ResourceMonitorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_resource_monitor",
                task_queue=tq,
            )

            await env.sleep(3)
            assert _received_findings.get(recorder_id, []) == []

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_zombie_finding_emitted(tmp_path):
    """A zombie finding returned by the activity gets signalled."""
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_check_zombies_hit,
                mock_check_memory_empty,
                mock_check_disk_empty,
            ],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                ResourceMonitorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_resource_monitor",
                task_queue=tq,
            )

            await env.sleep(3)
            findings = _received_findings.get(recorder_id, [])
            assert any(f.get("subtype") == "zombies" for f in findings), (
                f"expected a zombie finding, got {findings!r}"
            )

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_memory_finding_emitted(tmp_path):
    """A memory finding returned by the activity gets signalled."""
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_check_zombies_empty,
                mock_check_memory_hit,
                mock_check_disk_empty,
            ],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                ResourceMonitorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_resource_monitor",
                task_queue=tq,
            )

            await env.sleep(3)
            findings = _received_findings.get(recorder_id, [])
            assert any(
                f.get("subtype") == "memory_pressure" for f in findings
            ), f"expected a memory finding, got {findings!r}"

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_disk_finding_emitted(tmp_path):
    """A disk finding returned by the activity gets signalled."""
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_check_zombies_empty,
                mock_check_memory_empty,
                mock_check_disk_hit,
            ],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                ResourceMonitorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_resource_monitor",
                task_queue=tq,
            )

            await env.sleep(3)
            findings = _received_findings.get(recorder_id, [])
            assert any(
                f.get("subtype") == "disk_warning" for f in findings
            ), f"expected a disk finding, got {findings!r}"

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)


@pytest.mark.asyncio
async def test_failing_activity_does_not_suppress_other_findings(tmp_path):
    """If ``check_zombies`` raises, the other two still run and emit —
    proving the ``return_exceptions=True`` gather resilience."""
    tq = _task_queue()
    recorder_id = f"mission-{uuid.uuid4().hex}"
    _received_findings.pop(recorder_id, None)

    async with await _start_env() as env:
        worker = await _start_worker(
            env,
            tq,
            activities=[
                mock_check_zombies_raises,
                mock_check_memory_hit,
                mock_check_disk_hit,
            ],
        )
        async with worker:
            recorder_handle = await _spawn_recorder(env, tq, recorder_id)
            child_handle = await env.client.start_workflow(
                ResourceMonitorWorkflow.run,
                args=[recorder_id, "sess-1", 1],
                id=f"{recorder_id}_resource_monitor",
                task_queue=tq,
            )

            # Give the zombie activity time to exhaust its retry budget
            # (0.5s initial, 10s cap, 5 attempts) plus a couple of
            # cycles of the other two.
            await env.sleep(30)
            findings = _received_findings.get(recorder_id, [])
            subtypes = {f.get("subtype") for f in findings}
            assert "memory_pressure" in subtypes, (
                f"memory finding missing after zombie raises: {findings!r}"
            )
            assert "disk_warning" in subtypes, (
                f"disk finding missing after zombie raises: {findings!r}"
            )

            await child_handle.cancel()
            await recorder_handle.signal(ParentRecorderWorkflow.stop)
