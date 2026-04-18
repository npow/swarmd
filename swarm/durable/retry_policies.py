"""Per-activity `temporalio.common.RetryPolicy` constants.

Each activity in the durable swarm has its own retry budget calibrated to
the expected latency, failure modes, and blast radius of a retry. The
numbers below match the plan's Task 2 table (spec §7.2):

    Activity                    initial / max / coeff / attempts
    ------------------------    --------------------------------
    RUN_CLAUDE_CLI              2s / 5min  / x2 / 20
    CHECK_CRITERION             1s / 30s   / x2 / 5
    VERIFY_TAMPER               1s / 10s   / x2 / 3
    ENFORCE_INVARIANTS          1s / 10s   / x2 / 3
    PROGRESS_AUDIT              2s / 30s   / x2 / 5
    GOAL_DRIFT_CHECK            2s / 30s   / x2 / 5
    RUN_ANTICHEAT_DIMENSION     5s / 5min  / x2 / 10
    COMPLETION_JUDGE            1s / 10s   / x2 / 3
    INTERVENTION_JUDGE          100ms / 2s / x2 / 3
    SPAWN_SUBAGENT              2s / 1min  / x2 / 3
    RESTART_SUBPROCESS          1s / 10s   / x2 / 5
    EMIT_FINDING                100ms / 5s / x2 / 3

All policies share `backoff_coefficient=2.0` and the `NON_RETRYABLE_ERROR_TYPES`
list from `swarm.durable.errors`, so terminal exceptions abort retries instead
of burning the entire attempt budget.

Plus two heartbeat timeouts used when scheduling activities:

    HEARTBEAT_TIMEOUT_RUN_CLAUDE_CLI   = 2 minutes (long-running CLI shells)
    HEARTBEAT_TIMEOUT_LONG_ACTIVITY    = 30 seconds (audits, judges)

Note: ``classify_prompt`` intentionally has *no* retry policy — it fails
open to CHAT in the UserPromptSubmit hook rather than retrying in Temporal.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

from swarm.durable.errors import NON_RETRYABLE_ERROR_TYPES


def _policy(initial_s: float, max_s: float, attempts: int) -> RetryPolicy:
    """Build a RetryPolicy with the swarm's standard coefficient and
    non-retryable error types."""
    return RetryPolicy(
        initial_interval=timedelta(seconds=initial_s),
        maximum_interval=timedelta(seconds=max_s),
        backoff_coefficient=2.0,
        maximum_attempts=attempts,
        non_retryable_error_types=NON_RETRYABLE_ERROR_TYPES,
    )


# --- Per-activity retry policies (spec §7.2) ---------------------------------

RUN_CLAUDE_CLI = _policy(initial_s=2, max_s=300, attempts=20)
CHECK_CRITERION = _policy(initial_s=1, max_s=30, attempts=5)
VERIFY_TAMPER = _policy(initial_s=1, max_s=10, attempts=3)
ENFORCE_INVARIANTS = _policy(initial_s=1, max_s=10, attempts=3)
PROGRESS_AUDIT = _policy(initial_s=2, max_s=30, attempts=5)
GOAL_DRIFT_CHECK = _policy(initial_s=2, max_s=30, attempts=5)
RUN_ANTICHEAT_DIMENSION = _policy(initial_s=5, max_s=300, attempts=10)
COMPLETION_JUDGE = _policy(initial_s=1, max_s=10, attempts=3)
INTERVENTION_JUDGE = _policy(initial_s=0.1, max_s=2, attempts=3)
SPAWN_SUBAGENT = _policy(initial_s=2, max_s=60, attempts=3)
RESTART_SUBPROCESS = _policy(initial_s=1, max_s=10, attempts=5)
EMIT_FINDING = _policy(initial_s=0.1, max_s=5, attempts=3)
# classify_prompt: no retry — fail-open to CHAT is handled at the hook level,
# not as a Temporal retry, so there is intentionally no policy here.

# --- Heartbeat timeouts ------------------------------------------------------

# Long-running Claude CLI invocations must heartbeat at least every 2 minutes;
# anything longer means the shell is stuck and Temporal should restart it.
HEARTBEAT_TIMEOUT_RUN_CLAUDE_CLI = timedelta(minutes=2)

# Audits and judges are expected to finish in seconds, so we give them a much
# tighter heartbeat deadline.
HEARTBEAT_TIMEOUT_LONG_ACTIVITY = timedelta(seconds=30)
