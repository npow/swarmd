"""Shared helpers for the observer child workflows.

Each of the three child workflows (``PatternDetectorWorkflow``,
``LLMCriticWorkflow``, ``ResourceMonitorWorkflow``) emits findings to the
parent via the same signal path. Factoring the helper here keeps that
contract in one place — if the parent ever renames the signal or the
finding dict shape grows a required field, there is one call site to
update, not three.

These helpers are imported INSIDE ``workflow.unsafe.imports_passed_through``
by the parent workflow (see ``workflow.py``), so they must stay pure and
deterministic — no module-level side effects, no I/O at import time.
"""

from __future__ import annotations

from typing import Any

from temporalio import workflow


# Signal name the parent (MissionWorkflow) registers for. Kept as a module
# constant so the three child workflows can't accidentally drift apart in
# what they send. See ``swarm.durable.workflow.MissionWorkflow.finding_emitted``.
_FINDING_SIGNAL_NAME = "finding_emitted"


async def _emit_to_parent(mission_id: str, finding: dict[str, Any]) -> None:
    """Fire-and-forget signal of ``finding`` to the parent MissionWorkflow.

    The parent is addressed by its stable ``mission_id`` (which equals
    its Temporal ``workflow_id`` — see spec §6.2 lines 141-155). Temporal
    guarantees the ID survives ``continue_as_new``, so this works across
    parent restarts without re-acquiring a handle.

    ``signal(...)`` is awaited — Temporal records the send in history,
    then returns. Actual delivery to the parent is the server's problem.
    """
    parent = workflow.get_external_workflow_handle(mission_id)
    await parent.signal(_FINDING_SIGNAL_NAME, finding)
