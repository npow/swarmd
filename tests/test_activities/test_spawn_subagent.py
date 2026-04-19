"""Tests for the ``spawn_subagent`` Temporal activity.

Per plan Task 12 and spec §6.2:

    spawn_subagent(request) -> SpawnResult

    Launches a subagent subprocess with the correct argv and returns its
    identity. Admission control (max_depth / max_fan_out_per_parent /
    max_total_live) lives in the MissionWorkflow — this activity ONLY
    does the actual spawn, fire-and-forget.

The activity spawns a REAL subprocess. To keep tests hermetic we
monkeypatch ``subprocess.Popen`` and verify the argv shape + kwargs.

Tests use ``temporalio.testing.ActivityEnvironment`` so no Temporal server
is required.
"""

from __future__ import annotations

import subprocess
import uuid

import pytest
from temporalio.testing import ActivityEnvironment

from swarmd.durable.activities.spawn_subagent import (
    SpawnResult,
    spawn_subagent,
)
from swarmd.durable.errors import TerminalError, TransientError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(**overrides) -> dict:
    """Default request payload. Override fields via kwargs."""
    base = {
        "parent_id": "parent-abc",
        "depth": 1,
        "prompt": "investigate the flaky test",
        "workspace": "/tmp/workspace",
        "mission_id": "m-xyz",
    }
    base.update(overrides)
    return base


def _fake_popen_factory(captured: dict):
    """Return a ``fake_popen`` that delegates to a trivial `sh -c exit 0`
    so we don't actually launch claude, but still get a real ``Popen``
    back for the activity to drive (important for pid discovery)."""
    real_popen = subprocess.Popen  # capture BEFORE monkeypatch

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        # Return a real, trivial subprocess-like object so .pid / .poll() work.
        return real_popen(
            ["sh", "-c", "exit 0"], start_new_session=True
        )

    return fake_popen


# ---------------------------------------------------------------------------
# 1. Happy path — basic spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_populates_spawn_result(monkeypatch):
    """Basic spawn returns SpawnResult with uuid, pid, depth, and argv."""
    captured: dict = {}
    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        _fake_popen_factory(captured),
    )

    env = ActivityEnvironment()
    req = _make_request()
    result = await env.run(spawn_subagent, req)

    assert isinstance(result, SpawnResult)
    # subagent_id must be uuid-shaped (36 chars with hyphens is uuid4 default).
    assert isinstance(result.subagent_id, str)
    assert len(result.subagent_id) >= 12  # uuid hex or full uuid4
    # Confirm it parses as a UUID (raises ValueError otherwise).
    uuid.UUID(result.subagent_id)
    assert result.pid > 0
    assert result.parent_id == "parent-abc"
    assert result.depth == 1
    assert result.cmd == captured["args"]


@pytest.mark.asyncio
async def test_argv_contains_session_id_and_prompt(monkeypatch):
    """The activity must build argv that includes --session-id,
    --dangerously-skip-permissions, and the prompt text."""
    captured: dict = {}
    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        _fake_popen_factory(captured),
    )

    env = ActivityEnvironment()
    req = _make_request(prompt="diagnose the deadlock")
    result = await env.run(spawn_subagent, req)

    args = captured["args"]
    assert "--session-id" in args
    # session-id must equal the generated subagent_id.
    sid_idx = args.index("--session-id")
    assert args[sid_idx + 1] == result.subagent_id
    assert "--dangerously-skip-permissions" in args
    # The prompt text must be present as one of the argv entries.
    assert "diagnose the deadlock" in args


@pytest.mark.asyncio
async def test_root_spawn_parent_none(monkeypatch):
    """A root spawn has parent_id=None and depth=0."""
    captured: dict = {}
    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        _fake_popen_factory(captured),
    )

    env = ActivityEnvironment()
    req = _make_request(parent_id=None, depth=0)
    result = await env.run(spawn_subagent, req)

    assert result.parent_id is None
    assert result.depth == 0


# ---------------------------------------------------------------------------
# 2. start_new_session=True is passed to Popen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_new_session_true_passed_to_popen(monkeypatch):
    """``start_new_session=True`` is critical for process-group kill on
    restart / cancel — verify it was passed."""
    captured: dict = {}
    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        _fake_popen_factory(captured),
    )

    env = ActivityEnvironment()
    await env.run(spawn_subagent, _make_request())

    assert captured["kwargs"].get("start_new_session") is True


# ---------------------------------------------------------------------------
# 3. Error classification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_popen_oserror_raises_transient(monkeypatch):
    """CLI not found / OS-level spawn failure → TransientError so Temporal
    retries (the CLI may come back, e.g. after a fs remount)."""

    def raise_oserror(*args, **kwargs):
        raise OSError("cannot exec claude")

    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        raise_oserror,
    )

    env = ActivityEnvironment()
    with pytest.raises(TransientError) as exc_info:
        await env.run(spawn_subagent, _make_request())
    assert "claude" in str(exc_info.value) or "cannot" in str(exc_info.value)


@pytest.mark.asyncio
async def test_file_not_found_raises_transient(monkeypatch):
    """FileNotFoundError is a subclass of OSError but explicitly tested
    because it is the most common first-time-setup failure mode."""

    def raise_fnf(*args, **kwargs):
        raise FileNotFoundError("claude not on PATH")

    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        raise_fnf,
    )

    env = ActivityEnvironment()
    with pytest.raises(TransientError):
        await env.run(spawn_subagent, _make_request())


@pytest.mark.asyncio
async def test_missing_prompt_raises_terminal(monkeypatch):
    """A request without a ``prompt`` field is malformed — TerminalError
    so Temporal fails fast instead of burning the retry budget."""
    # No subprocess monkeypatch — activity should reject before spawning.
    env = ActivityEnvironment()
    req = _make_request()
    del req["prompt"]
    with pytest.raises(TerminalError):
        await env.run(spawn_subagent, req)


@pytest.mark.asyncio
async def test_missing_workspace_raises_terminal(monkeypatch):
    """Request without workspace → TerminalError."""
    env = ActivityEnvironment()
    req = _make_request()
    del req["workspace"]
    with pytest.raises(TerminalError):
        await env.run(spawn_subagent, req)


@pytest.mark.asyncio
async def test_missing_depth_raises_terminal(monkeypatch):
    """Request without depth → TerminalError (admission control assumes a
    meaningful depth value)."""
    env = ActivityEnvironment()
    req = _make_request()
    del req["depth"]
    with pytest.raises(TerminalError):
        await env.run(spawn_subagent, req)


# ---------------------------------------------------------------------------
# 4. Model override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_override_shows_up_in_argv(monkeypatch):
    """If request specifies ``model``, it's passed to claude via ``--model``."""
    captured: dict = {}
    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        _fake_popen_factory(captured),
    )

    env = ActivityEnvironment()
    req = _make_request(model="opus")
    await env.run(spawn_subagent, req)

    args = captured["args"]
    assert "--model" in args
    midx = args.index("--model")
    assert args[midx + 1] == "opus"


@pytest.mark.asyncio
async def test_no_model_no_model_flag(monkeypatch):
    """Without ``model`` in the request, ``--model`` should not appear."""
    captured: dict = {}
    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        _fake_popen_factory(captured),
    )

    env = ActivityEnvironment()
    await env.run(spawn_subagent, _make_request())

    assert "--model" not in captured["args"]
