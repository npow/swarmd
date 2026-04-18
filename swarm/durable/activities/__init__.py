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
from swarm.durable.activities.enforce_invariants import (
    InvariantsResult,
    enforce_invariants,
)
from swarm.durable.activities.verify_tamper import (
    TamperResult,
    verify_tamper,
)

# ``emit_finding`` is re-exported under an alias so it does not shadow the
# ``swarm.durable.activities.emit_finding`` submodule at attribute lookup
# time. Task 6 hit this exact pitfall with ``enforce_invariants`` — once a
# re-export replaces the submodule on the package object, ``patch(
# "swarm.durable.activities.emit_finding._append_jsonl")`` resolves to the
# FUNCTION and then fails (functions have no attributes to patch). Keeping
# the alias avoids the trap for any future test that wants to monkey-patch
# the module. Worker bootstrap (Task 13) imports the activity via this
# alias to register it with ``Worker(...)``.
from swarm.durable.activities.emit_finding import (
    emit_finding as emit_finding_activity,
)

__all__ = [
    "CriterionCheckResult",
    "InvariantsResult",
    "TamperResult",
    "check_criterion",
    "emit_finding_activity",
    "enforce_invariants",
    "verify_tamper",
]
