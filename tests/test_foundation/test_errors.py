"""Tests for the durable error taxonomy and per-activity retry policies.

Covers:
- `classify_http_status` behavior across 2xx, transient, terminal, and
  unknown status codes (per the plan's Task 2 block plus an 418 unknown-status
  case explicitly required by the Task 2 brief).
- `swarm.durable.retry_policies` exposes `temporalio.common.RetryPolicy`
  instances with the expected field values.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from swarm.durable.errors import (
    AuthError,
    BillingError,  # noqa: F401  (re-export smoke import per plan)
    ContextOverflowError,  # noqa: F401  (re-export smoke import per plan)
    NON_RETRYABLE_ERROR_TYPES,
    TerminalError,
    TransientError,
    classify_http_status,
)


def test_200_is_success() -> None:
    # Sanity: should not raise for any 2xx
    assert classify_http_status(200, body=b"") is None


def test_424_is_transient() -> None:
    with pytest.raises(TransientError):
        classify_http_status(424, body=b"")


def test_429_respects_retry_after() -> None:
    with pytest.raises(TransientError) as exc_info:
        classify_http_status(429, body=b"", retry_after_sec=30)
    assert exc_info.value.retry_after_sec == 30


def test_401_is_terminal() -> None:
    with pytest.raises(AuthError):
        classify_http_status(401, body=b"")


def test_400_is_terminal() -> None:
    with pytest.raises(TerminalError):
        classify_http_status(400, body=b"malformed")


def test_500_is_transient() -> None:
    with pytest.raises(TransientError):
        classify_http_status(500, body=b"")


def test_unknown_status_is_transient() -> None:
    # 418 (I'm a teapot) isn't in either table; should classify conservatively
    # as transient per the plan.
    with pytest.raises(TransientError):
        classify_http_status(418, body=b"")


def test_non_retryable_error_types_includes_all_terminals() -> None:
    # Smoke-check the names exported for use as
    # RetryPolicy.non_retryable_error_types.
    assert "TerminalError" in NON_RETRYABLE_ERROR_TYPES
    assert "AuthError" in NON_RETRYABLE_ERROR_TYPES
    assert "BillingError" in NON_RETRYABLE_ERROR_TYPES
    assert "ContextOverflowError" in NON_RETRYABLE_ERROR_TYPES
    assert "UserCancelledError" in NON_RETRYABLE_ERROR_TYPES


def test_retry_policies_module_has_expected_policies() -> None:
    """Verify the retry_policies module imports and exposes RetryPolicy instances."""
    from temporalio.common import RetryPolicy

    from swarm.durable import retry_policies

    # Spot-check RUN_CLAUDE_CLI: 2s / 5min / ×2 / 20 attempts.
    assert isinstance(retry_policies.RUN_CLAUDE_CLI, RetryPolicy)
    assert retry_policies.RUN_CLAUDE_CLI.initial_interval == timedelta(seconds=2)
    assert retry_policies.RUN_CLAUDE_CLI.maximum_interval == timedelta(seconds=300)
    assert retry_policies.RUN_CLAUDE_CLI.backoff_coefficient == 2.0
    assert retry_policies.RUN_CLAUDE_CLI.maximum_attempts == 20
    assert (
        retry_policies.RUN_CLAUDE_CLI.non_retryable_error_types
        == NON_RETRYABLE_ERROR_TYPES
    )

    # Heartbeat timeouts per spec.
    assert retry_policies.HEARTBEAT_TIMEOUT_RUN_CLAUDE_CLI == timedelta(minutes=2)
    assert retry_policies.HEARTBEAT_TIMEOUT_LONG_ACTIVITY == timedelta(seconds=30)

    # INTERVENTION_JUDGE has sub-second initial interval per spec.
    assert retry_policies.INTERVENTION_JUDGE.initial_interval == timedelta(
        milliseconds=100
    )
    assert retry_policies.INTERVENTION_JUDGE.maximum_interval == timedelta(seconds=2)
    assert retry_policies.INTERVENTION_JUDGE.maximum_attempts == 3


def test_all_retry_policies_share_backoff_and_non_retryable_types() -> None:
    """All 12 per-activity policies must use coefficient 2.0 and the shared
    non_retryable_error_types list."""
    from temporalio.common import RetryPolicy

    from swarm.durable import retry_policies

    expected_policies = [
        ("RUN_CLAUDE_CLI", 2, 300, 20),
        ("CHECK_CRITERION", 1, 30, 5),
        ("VERIFY_TAMPER", 1, 10, 3),
        ("ENFORCE_INVARIANTS", 1, 10, 3),
        ("PROGRESS_AUDIT", 2, 30, 5),
        ("GOAL_DRIFT_CHECK", 2, 30, 5),
        ("RUN_ANTICHEAT_DIMENSION", 5, 300, 10),
        ("COMPLETION_JUDGE", 1, 10, 3),
        ("INTERVENTION_JUDGE", 0.1, 2, 3),
        ("SPAWN_SUBAGENT", 2, 60, 3),
        ("RESTART_SUBPROCESS", 1, 10, 5),
        ("EMIT_FINDING", 0.1, 5, 3),
    ]

    for name, initial_s, max_s, attempts in expected_policies:
        policy = getattr(retry_policies, name)
        assert isinstance(policy, RetryPolicy), f"{name} is not a RetryPolicy"
        assert policy.backoff_coefficient == 2.0, f"{name} wrong coefficient"
        assert policy.maximum_attempts == attempts, f"{name} wrong max attempts"
        assert policy.initial_interval == timedelta(
            seconds=initial_s
        ), f"{name} wrong initial interval"
        assert policy.maximum_interval == timedelta(
            seconds=max_s
        ), f"{name} wrong maximum interval"
        assert (
            policy.non_retryable_error_types == NON_RETRYABLE_ERROR_TYPES
        ), f"{name} wrong non_retryable_error_types"
