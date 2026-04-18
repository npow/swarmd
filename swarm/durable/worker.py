"""Temporal worker daemon — STUB.

Task 18 will replace this with a real worker that registers every
workflow (``MissionWorkflow`` + the three observer specialists) and all
activities (see ``swarm/durable/activities/``), then polls the ``swarm``
task queue indefinitely.

Task 19 wires the ``swarm worker`` CLI command to call ``main()`` from
this module so the console entry point exists before the real worker
arrives. Keeping this file present (even as a stub) lets the CLI tests
exercise the wiring — the test patches ``swarm.durable.worker.main`` so
it doesn't actually spin up a worker.
"""

from __future__ import annotations


async def main(host: str = "localhost:7233", task_queue: str = "swarm") -> None:
    """Stub entry point — prints a message and exits.

    Task 18 will replace this with:

        client = await Client.connect(host)
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[MissionWorkflow, PatternDetectorWorkflow, ...],
            activities=[run_claude_cli, check_criterion, ...],
        )
        await worker.run()
    """
    print(
        f"STUB WORKER: not yet implemented. "
        f"Intended host={host}, task_queue={task_queue}"
    )
    print(
        "Task 18 will replace this with a real worker that registers "
        "all workflows + activities."
    )


__all__ = ["main"]
