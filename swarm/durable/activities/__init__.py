"""Durable activity implementations for the swarm Temporal worker.

Per spec §6.3 each externally-observable unit of work lives in its own module
here and is registered with the worker as an ``@activity.defn``. Importing a
submodule registers the activity with ``temporalio.activity`` at module-load
time; the worker bootstrap code in ``swarm.durable.worker`` (Task 13) will
iterate over this package and hand each function to ``Worker(...)``.

Keep module boundaries thin: one activity per file, named after the activity
(``check_criterion.py`` → ``check_criterion``). Shared helpers that multiple
activities need belong in ``swarm.durable`` (retry policies, errors), not
here.
"""

from swarm.durable.activities.check_criterion import (
    CriterionCheckResult,
    check_criterion,
)

__all__ = [
    "CriterionCheckResult",
    "check_criterion",
]
