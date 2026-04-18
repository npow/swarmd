"""Swarm Temporal worker daemon — registers all workflows + activities.

Task 18 replaces the Task 19 stub with a real worker that:

* Connects to Temporal at ``$TEMPORAL_ADDRESS`` (default ``localhost:7233``)
  using the :mod:`temporalio.contrib.pydantic` ``pydantic_data_converter`` so
  Mission / Verification / Criterion / Invariants pydantic models round-trip
  losslessly between workflow calls. The workflow tests
  (``tests/test_workflows/``) register their test Workers with the same
  converter; production parity matters because the converter governs whether
  arguments arrive as dicts or pydantic instances inside the workflow.

* Registers the four ``@workflow.defn`` classes — the top-level
  ``MissionWorkflow`` plus the three observer specialists (pattern detector,
  LLM critic, resource monitor).

* Registers all aliased ``*_activity`` functions re-exported from
  :mod:`swarm.durable.activities`. The aliases exist so the re-exports don't
  shadow their submodules at attribute lookup time (see the comments on each
  alias in that package's ``__init__.py``). The worker only cares about the
  functions — Temporal matches by the ``@activity.defn(name=...)`` string the
  activity registers with, not by the Python attribute name — so the alias
  suffix is immaterial to routing.

* Polls the ``swarm`` task queue (overridable via ``SWARM_TASK_QUEUE``) and
  blocks indefinitely until cancelled / ``KeyboardInterrupt``.

CLI wiring: :mod:`swarm.cli` invokes ``asyncio.run(worker_mod.main())``
(no positional args) from the ``swarm worker`` subcommand. ``main`` therefore
must keep working with zero arguments; env vars are consulted by
``cli_main`` (the ``python -m swarm.durable.worker`` entry point) but not by
``main`` itself, so tests that patch env vars and call ``main`` directly
still get deterministic defaults.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Final

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from swarm.durable.activities import (
    check_criterion,
    check_disk_activity,
    check_memory_activity,
    check_zombies_activity,
    completion_judge_activity,
    detect_scope_shrinking_activity,
    emit_finding_activity,
    enforce_invariants,
    goal_drift_check_activity,
    intervention_judge_activity,
    progress_audit_activity,
    read_recent_events_activity,
    restart_subprocess_activity,
    run_anticheat_dimension_activity,
    run_claude_cli_activity,
    spawn_subagent_activity,
    verify_tamper,
)
from swarm.durable.specialists import (
    LLMCriticWorkflow,
    PatternDetectorWorkflow,
    ResourceMonitorWorkflow,
)
from swarm.durable.workflow import MissionWorkflow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_HOST: Final[str] = "localhost:7233"
DEFAULT_TASK_QUEUE: Final[str] = "swarm"

# Concurrency knobs. The numbers match ``temporalio``'s own defaults in spirit
# (100 per slot) — generous enough that a single-worker dev setup doesn't
# artificially serialize work, small enough that an accidentally-leaky
# activity doesn't spawn thousands of tasks. Production deployments should
# tune via the kwargs on ``main``.
DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS: Final[int] = 100
DEFAULT_MAX_CONCURRENT_ACTIVITIES: Final[int] = 100

# ---------------------------------------------------------------------------
# Registration manifests
# ---------------------------------------------------------------------------
#
# Kept as module-level constants (rather than inlined into ``main``) so tests
# can assert registration coverage without spinning up a Worker. When a new
# workflow or activity lands, add it here — the worker picks it up
# automatically and the startup test will keep the counts honest.

WORKFLOWS: Final[list] = [
    MissionWorkflow,
    PatternDetectorWorkflow,
    LLMCriticWorkflow,
    ResourceMonitorWorkflow,
]

ACTIVITIES: Final[list] = [
    # Foundation (Tasks 3-6) — unaliased because the original imports
    # already lived at the package root under these names.
    check_criterion,
    verify_tamper,
    enforce_invariants,
    # Emit finding (Task 7).
    emit_finding_activity,
    # Judges (Tasks 8, 10).
    intervention_judge_activity,
    completion_judge_activity,
    # Claude CLI wrapper (Task 9).
    run_claude_cli_activity,
    # Progress / goal drift / anti-cheat (Tasks 10-11).
    progress_audit_activity,
    goal_drift_check_activity,
    run_anticheat_dimension_activity,
    # Subprocess lifecycle (Task 12).
    spawn_subagent_activity,
    restart_subprocess_activity,
    detect_scope_shrinking_activity,
    # Observer inputs (Tasks 15, 17).
    read_recent_events_activity,
    check_zombies_activity,
    check_memory_activity,
    check_disk_activity,
]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def main(
    host: str = DEFAULT_HOST,
    task_queue: str = DEFAULT_TASK_QUEUE,
    max_concurrent_workflow_tasks: int = DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS,
    max_concurrent_activities: int = DEFAULT_MAX_CONCURRENT_ACTIVITIES,
) -> None:
    """Connect to Temporal and run the swarm worker until cancelled.

    Blocks forever on an ``asyncio.Event().wait()`` so the caller controls
    shutdown (``KeyboardInterrupt`` via ``cli_main`` or explicit task
    cancellation in tests).
    """
    # The pydantic data converter is load-bearing: the workflow tests rely on
    # Mission / MissionState round-tripping as pydantic models, and production
    # must match that contract so a workflow started from the CLI behaves
    # identically to one started from a test. Do not remove.
    client = await Client.connect(host, data_converter=pydantic_data_converter)

    logger.info(
        "Swarm worker connecting to %s, polling task queue %r "
        "(%d workflows, %d activities registered)",
        host,
        task_queue,
        len(WORKFLOWS),
        len(ACTIVITIES),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        max_concurrent_workflow_tasks=max_concurrent_workflow_tasks,
        max_concurrent_activities=max_concurrent_activities,
    ):
        logger.info("Swarm worker running; Ctrl+C to stop.")
        # Block forever. The ``async with`` machinery will call
        # ``Worker.__aexit__`` on cancellation, which drains in-flight
        # activities cleanly.
        await asyncio.Event().wait()


def cli_main() -> None:
    """Synchronous entry point for ``python -m swarm.durable.worker``.

    Reads ``TEMPORAL_ADDRESS`` / ``SWARM_TASK_QUEUE`` / ``SWARM_LOG_LEVEL``
    from the environment and installs a minimal logging config before
    handing off to ``main``. Catches ``KeyboardInterrupt`` so Ctrl+C exits
    cleanly with a log line instead of a traceback.

    The :mod:`swarm.cli` ``worker`` subcommand calls ``main()`` directly
    (without this wrapper) because Click owns logging + signal handling in
    that path; this function exists for folks who invoke the worker module
    standalone.
    """
    logging.basicConfig(
        level=os.environ.get("SWARM_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    host = os.environ.get("TEMPORAL_ADDRESS", DEFAULT_HOST)
    task_queue = os.environ.get("SWARM_TASK_QUEUE", DEFAULT_TASK_QUEUE)
    try:
        asyncio.run(main(host=host, task_queue=task_queue))
    except KeyboardInterrupt:
        logger.info("Swarm worker shutting down (Ctrl+C)")


__all__ = [
    "ACTIVITIES",
    "DEFAULT_HOST",
    "DEFAULT_MAX_CONCURRENT_ACTIVITIES",
    "DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS",
    "DEFAULT_TASK_QUEUE",
    "WORKFLOWS",
    "cli_main",
    "main",
]


if __name__ == "__main__":
    cli_main()
