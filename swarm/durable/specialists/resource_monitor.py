"""``ResourceMonitorWorkflow`` — zombie / memory / disk checks (Task 17).

Per spec §6.2 lines 177-180:

    ResourceMonitorWorkflow(mission_id, session_id, cadence_sec)
      zombie/memory/disk checks on cadence
      emits findings via signal to parent
      cadence_sec default 30s, override via mission.yaml
      observer_config.resource_monitor_sec

Determinism contract:

* Uses ``workflow.execute_activity`` for every subprocess /
  filesystem check — no direct I/O in workflow code.
* Three checks run in parallel via ``asyncio.gather`` — deterministic
  inside Temporal's event loop.
* Activity exceptions are caught per-call so a broken ``ps`` binary
  doesn't kill disk checks.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from swarm.durable import retry_policies
    from swarm.durable.specialists._utils import _emit_to_parent


@workflow.defn
class ResourceMonitorWorkflow:
    """Long-running observer child workflow for resource-pressure checks.

    One instance per mission, spawned by ``MissionWorkflow._start_children``.
    Runs the three independent checks in parallel each tick.

    No ``continue_as_new`` threshold is enforced here: Temporal's own
    ``is_continue_as_new_suggested`` heuristic fires when history hits
    internal thresholds, but the resource-monitor's workflow produces
    minimal history (one gather per tick, no signals, no subworkflows),
    so 50k events = many days of cadence. If a long-running mission
    eventually hits the threshold, Temporal will surface it on the
    worker side; we'd add a cycle counter here then. For v0, simplicity
    wins.

    Stub-interface contract preserved from Task 14 placeholder:
      * Run signature is ``(mission_id, session_id, cadence_sec)``
        positional.
    """

    @workflow.run
    async def run(
        self, mission_id: str, session_id: str, cadence_sec: int
    ) -> dict[str, Any]:
        while True:
            findings = await self._run_all_checks(mission_id)
            for f in findings:
                await _emit_to_parent(mission_id, f)
            await workflow.sleep(cadence_sec)

    # ---------------------------------------------------------- helpers --

    async def _run_all_checks(
        self, mission_id: str
    ) -> list[dict[str, Any]]:
        """Run the three checks in parallel and flatten the result lists.

        Each check returns a ``list[dict]`` (possibly empty). We collect
        via ``asyncio.gather`` with ``return_exceptions=True`` so a
        single failing activity doesn't suppress the other two. Failures
        are logged; the workflow continues.
        """
        results = await asyncio.gather(
            workflow.execute_activity(
                "check_zombies",
                args=[mission_id],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policies.CHECK_RESOURCES,
            ),
            workflow.execute_activity(
                "check_memory",
                args=[mission_id],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policies.CHECK_RESOURCES,
            ),
            workflow.execute_activity(
                "check_disk",
                args=[mission_id],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=retry_policies.CHECK_RESOURCES,
            ),
            return_exceptions=True,
        )
        findings: list[dict[str, Any]] = []
        for r in results:
            if isinstance(r, BaseException):
                workflow.logger.warning(
                    "resource_monitor: check failed: %r", r
                )
                continue
            if isinstance(r, list):
                findings.extend(r)
        return findings
