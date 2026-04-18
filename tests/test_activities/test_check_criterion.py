"""Tests for the ``check_criterion`` Temporal activity.

Per plan Task 4 and spec §6.3 (the ``check_criterion`` row):

    check_criterion(criterion) → {pass, exit_code, stdout_tail, stderr_tail, duration_ms}

    Short (seconds). Subject to ``criterion.timeout_sec``. Idempotent.

These tests drive the activity through ``temporalio.testing.ActivityEnvironment``
so we do not need a running Temporal server. Since ``pyproject.toml`` sets
``asyncio_mode = "auto"``, the ``@pytest.mark.asyncio`` decorator is optional;
we keep it explicit for readability and to guard against future config drift.
"""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from swarm.durable.activities.check_criterion import (
    CriterionCheckResult,
    check_criterion,
)
from swarm.schemas.criterion import Criterion


@pytest.mark.asyncio
async def test_passing_criterion_returns_pass_true(tmp_path):
    """A 0-exit shell check must yield pass_=True with a matching criterion_id."""
    (tmp_path / "sentinel").write_text("x")
    c = Criterion(
        id="test_file_exists",
        description="file exists",
        check=f"test -f {tmp_path}/sentinel",
        timeout_sec=5,
        idempotent=True,
    )
    env = ActivityEnvironment()
    result = await env.run(check_criterion, c, str(tmp_path))

    assert isinstance(result, CriterionCheckResult)
    assert result.pass_ is True
    assert result.exit_code == 0
    assert result.criterion_id == "test_file_exists"
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_failing_criterion_returns_pass_false(tmp_path):
    """A non-zero exit must yield pass_=False and propagate the exit code."""
    c = Criterion(
        id="nope",
        description="fails",
        check="false",
        timeout_sec=5,
        idempotent=True,
    )
    env = ActivityEnvironment()
    result = await env.run(check_criterion, c, str(tmp_path))

    assert result.pass_ is False
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_timeout_returns_pass_false_with_stderr(tmp_path):
    """When the subprocess exceeds ``timeout_sec``, report pass_=False and a
    ``stderr_tail`` that mentions the timeout so humans debugging the mission
    can tell a timeout apart from a normal non-zero exit."""
    c = Criterion(
        id="slow",
        description="sleeps",
        check="sleep 10",
        timeout_sec=1,
        idempotent=True,
    )
    env = ActivityEnvironment()
    result = await env.run(check_criterion, c, str(tmp_path))

    assert result.pass_ is False
    assert "timeout" in result.stderr_tail.lower()
    # The contract in the plan says exit_code = -1 for the timeout path.
    assert result.exit_code == -1


@pytest.mark.asyncio
async def test_stdout_tail_truncates(tmp_path):
    """stdout must be tailed to the last 2000 chars so a runaway process
    can't blow up Temporal history with megabytes of output."""
    c = Criterion(
        id="chatty",
        description="emits 5KB of output",
        # ``yes`` would loop forever; ``head -c`` caps the volume.
        check="yes | head -c 5000",
        timeout_sec=5,
        idempotent=True,
    )
    env = ActivityEnvironment()
    result = await env.run(check_criterion, c, str(tmp_path))

    assert len(result.stdout_tail) <= 2000


@pytest.mark.asyncio
async def test_cwd_is_workspace(tmp_path):
    """The subprocess must run with cwd=workspace so criterion checks see the
    mission's files at relative paths. ``pwd`` must resolve to tmp_path."""
    c = Criterion(
        id="pwd_check",
        description="report cwd",
        check="pwd",
        timeout_sec=5,
        idempotent=True,
    )
    env = ActivityEnvironment()
    result = await env.run(check_criterion, c, str(tmp_path))

    # On macOS /tmp is a symlink to /private/tmp, so the subprocess's ``pwd``
    # may resolve the real path. Compare both the supplied workspace and its
    # realpath.
    import os

    workspace = str(tmp_path)
    realpath = os.path.realpath(workspace)
    assert workspace in result.stdout_tail or realpath in result.stdout_tail
