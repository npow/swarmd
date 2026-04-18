"""Stub child workflows — Tasks 15-17 will replace these with real implementations.

The interfaces here are the parent-side contract: workflow name, run signature,
and the signals the parent cares about. Swapping in real bodies later must
preserve these shapes so ``MissionWorkflow._start_children`` keeps working
without modification.

The stubs spin in an infinite ``workflow.sleep`` loop — just enough
body to let the parent's ``workflow.get_external_workflow_handle`` return
a live handle post ``continue_as_new``. Once Tasks 15-17 fill these in, the
parent's contract (workflow name + run signature) stays stable: only the
body changes.
"""

from __future__ import annotations

from temporalio import workflow


@workflow.defn
class PatternDetectorWorkflow:
    """Stub — Task 15 will replace the body with real event-tail + pattern rules.

    Run signature is load-bearing: ``(mission_id, session_id, cadence_sec)``.
    The parent passes these three positional args through
    ``workflow.start_child_workflow`` and the same signature must survive the
    Task 15 rewrite.
    """

    @workflow.run
    async def run(
        self, mission_id: str, session_id: str, cadence_sec: int
    ) -> dict:
        # Stub body: sleep indefinitely so the parent can signal / query the
        # child across continue_as_new cycles. Task 15 replaces this with the
        # real event-tail + pattern-detection cadence loop.
        while True:
            await workflow.sleep(cadence_sec)


@workflow.defn
class LLMCriticWorkflow:
    """Stub — Task 16 will replace the body with progress_audit + goal_drift +
    anticheat-panel triggers. The ``anticheat_requested`` signal handler is
    declared here because the parent fires it on criterion pass-transitions.
    """

    @workflow.run
    async def run(
        self, mission_id: str, session_id: str, cadence_sec: int
    ) -> dict:
        # Stub body — Task 16 replaces with cadence-driven critic calls and
        # anticheat panel fan-out. Parent's ``anticheat_requested`` signal
        # handler is declared below so parent-side signal dispatch compiles.
        while True:
            await workflow.sleep(cadence_sec)

    @workflow.signal
    async def anticheat_requested(self, criterion: dict, context: dict) -> None:
        """Stub signal handler — Task 16 will run the 6-dimension panel here.

        Declared now so parent's ``workflow.start_child_workflow`` +
        subsequent signal dispatch against the returned handle typecheck
        correctly.
        """
        # No-op. Task 16 implements the real fan-out.
        pass


@workflow.defn
class ResourceMonitorWorkflow:
    """Stub — Task 17 will replace body with zombie/memory/disk cadence checks."""

    @workflow.run
    async def run(
        self, mission_id: str, session_id: str, cadence_sec: int
    ) -> dict:
        # Stub body — Task 17 replaces with resource-check cadence loop.
        while True:
            await workflow.sleep(cadence_sec)
