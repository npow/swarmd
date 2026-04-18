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

# Dataclasses from the new judge activities are re-exported unaliased so
# callers can ``from swarm.durable.activities import InterventionDecision``.
# The activity FUNCTIONS, however, use the ``_activity`` suffix so they do
# not shadow their submodules at attribute lookup time. Same pitfall as
# ``emit_finding`` — see the comment on that re-export below.
from swarm.durable.activities.intervention_judge import (
    ESCAPE_LADDER,
    InterventionDecision,
    intervention_judge as intervention_judge_activity,
)
from swarm.durable.activities.completion_judge import (
    CompletionDecision,
    completion_judge as completion_judge_activity,
)
from swarm.durable.activities.progress_audit import (
    ProgressAuditResult,
    progress_audit as progress_audit_activity,
)
from swarm.durable.activities.goal_drift_check import (
    GoalDriftResult,
    goal_drift_check as goal_drift_check_activity,
)
from swarm.durable.activities.run_anticheat_dimension import (
    AnticheatVerdict,
    run_anticheat_dimension as run_anticheat_dimension_activity,
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
from swarm.durable.activities.run_claude_cli import (
    ClaudeResult,
    run_claude_cli as run_claude_cli_activity,
)

# Task 12 — spawn_subagent / restart_subprocess / detect_scope_shrinking.
# Same alias-suffix pattern as the judges above, for the same reason:
# avoid shadowing the submodule at attribute lookup time (keeps
# ``patch("swarm.durable.activities.spawn_subagent._launch_claude_subprocess")``
# and similar monkeypatches working correctly).
from swarm.durable.activities.spawn_subagent import (
    SpawnResult,
    spawn_subagent as spawn_subagent_activity,
)
from swarm.durable.activities.restart_subprocess import (
    RestartResult,
    restart_subprocess as restart_subprocess_activity,
)
from swarm.durable.activities.detect_scope_shrinking import (
    ScopeShrinkingResult,
    detect_scope_shrinking as detect_scope_shrinking_activity,
)

# Task 15 — ``read_recent_events`` for the PatternDetectorWorkflow to tail
# ``events.jsonl``. Aliased (``_activity`` suffix) so the re-export doesn't
# shadow the submodule at attribute lookup time — same pitfall rationale
# as the other aliased activities above.
from swarm.durable.activities.read_recent_events import (
    read_recent_events as read_recent_events_activity,
)

__all__ = [
    "AnticheatVerdict",
    "ClaudeResult",
    "CompletionDecision",
    "CriterionCheckResult",
    "ESCAPE_LADDER",
    "GoalDriftResult",
    "InterventionDecision",
    "InvariantsResult",
    "ProgressAuditResult",
    "RestartResult",
    "ScopeShrinkingResult",
    "SpawnResult",
    "TamperResult",
    "check_criterion",
    "completion_judge_activity",
    "detect_scope_shrinking_activity",
    "emit_finding_activity",
    "enforce_invariants",
    "goal_drift_check_activity",
    "intervention_judge_activity",
    "progress_audit_activity",
    "read_recent_events_activity",
    "restart_subprocess_activity",
    "run_anticheat_dimension_activity",
    "run_claude_cli_activity",
    "spawn_subagent_activity",
    "verify_tamper",
]
