"""Tests for the ``run_claude_cli`` Temporal activity.

Per plan Task 9 and spec §7.3:

    run_claude_cli(session_id, mission_prose) -> ClaudeResult

    * Attempt-based resume: attempt 1 uses ``claude --session-id <sid>``;
      attempts > 1 use ``claude --resume <sid>``.
    * Spawns subprocess in its own process group (``start_new_session=True``)
      so cancellation can SIGTERM the entire group.
    * Independent 30s heartbeat timer — NOT tied to event emission. Handles
      long reasoning pauses where claude runs 3-5min without emitting tool
      events.
    * Tails ``~/.swarm/state/<sid>/events.jsonl`` and pushes the most recent
      event metadata into the heartbeat payload.
    * On ``CancelledError`` (``swarm abort``): SIGTERM process group, wait
      5s, SIGKILL the group if still alive, re-raise.
    * Non-zero subprocess exit raises ``TransientError``.

The activity spawns a REAL subprocess. To keep tests hermetic we swap
``spawn_claude`` for a helper that invokes ``sh -c`` with scripts that mimic
the behaviors we care about (fast exit, slow exit, trap SIGTERM, trap-and-
ignore SIGTERM, write events to the tail path).

Tests use ``temporalio.testing.ActivityEnvironment`` so no Temporal server is
required. ``attempt`` is overridden by mutating ``env.info`` (a frozen
dataclass, but ``dataclasses.replace`` works).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess

import pytest
from temporalio.testing import ActivityEnvironment

from swarmd.durable.activities.run_claude_cli import (
    ClaudeResult,
    run_claude_cli,
    should_use_resume,
)
from swarmd.durable.errors import TransientError


# ---------------------------------------------------------------------------
# Helpers — fake-claude subprocess factories that mimic the shapes we care
# about. The activity only distinguishes ``claude`` from ``sh`` via the
# ``spawn_claude`` callable, which tests monkeypatch; the returned object is
# a real ``subprocess.Popen`` that the activity drives uniformly.
# ---------------------------------------------------------------------------


def _spawn_sh(script: str) -> subprocess.Popen:
    """Spawn ``sh -c script`` in its own process group, mirroring the
    contract of ``spawn_claude``.
    """

    return subprocess.Popen(
        ["sh", "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


# ---------------------------------------------------------------------------
# 1. Attempt-based resume (pure helper + integration)
# ---------------------------------------------------------------------------


def test_should_use_resume_first_attempt_false():
    """``activity.info().attempt == 1`` → first launch, use --session-id."""
    assert should_use_resume(1) is False


@pytest.mark.parametrize("attempt", [2, 3, 5, 20])
def test_should_use_resume_retry_true(attempt):
    """Any retry (attempt > 1) uses --resume."""
    assert should_use_resume(attempt) is True


@pytest.mark.asyncio
async def test_first_attempt_spawns_with_use_resume_false(tmp_path, monkeypatch):
    """ActivityEnvironment defaults attempt=1, so spawn_claude is called with
    ``use_resume=False``. Verified by capturing the args."""

    calls: list[dict] = []

    def fake_spawn(session_id: str, mission_prose: str, use_resume: bool):
        calls.append(
            {
                "session_id": session_id,
                "mission_prose": mission_prose,
                "use_resume": use_resume,
            }
        )
        # Exits immediately with 0 so the activity's gather() completes.
        return _spawn_sh("exit 0")

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.spawn_claude", fake_spawn
    )
    # Point tail at an empty tmp dir so the tail task has nothing to do.
    monkeypatch.setenv("HOME", str(tmp_path))

    env = ActivityEnvironment()
    result = await env.run(run_claude_cli, "sid-1", "do the thing")

    assert len(calls) == 1
    assert calls[0]["use_resume"] is False
    assert calls[0]["session_id"] == "sid-1"
    assert calls[0]["mission_prose"] == "do the thing"
    assert isinstance(result, ClaudeResult)
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_retry_attempt_spawns_with_use_resume_true(tmp_path, monkeypatch):
    """Mutating env.info.attempt=2 → activity asks for --resume."""

    calls: list[dict] = []

    def fake_spawn(session_id: str, mission_prose: str, use_resume: bool):
        calls.append({"use_resume": use_resume})
        return _spawn_sh("exit 0")

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.spawn_claude", fake_spawn
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=3)
    await env.run(run_claude_cli, "sid-2", "retry prose")

    assert len(calls) == 1
    assert calls[0]["use_resume"] is True


# ---------------------------------------------------------------------------
# 2. Heartbeat is time-driven, not event-driven
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_heartbeat_fires_at_least_once_during_run(
    tmp_path, monkeypatch
):
    """Heartbeats are on an independent timer. Even for a subprocess that
    emits no events, the activity must heartbeat at least once before it
    exits.

    The loop cadence in production is 30s; for the test we override it via
    the ``HEARTBEAT_INTERVAL_SEC`` module attribute so the test stays fast.
    """

    # Shrink the interval to 0.1s so we can observe 1-2 heartbeats in <1s.
    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.HEARTBEAT_INTERVAL_SEC", 0.1
    )

    def fake_spawn(session_id, mission_prose, use_resume):
        # Sleep 0.5s — during which the heartbeat loop should fire 3-5 times.
        return _spawn_sh("sleep 0.5; exit 0")

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.spawn_claude", fake_spawn
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    captured: list[dict] = []

    env = ActivityEnvironment()
    env.on_heartbeat = lambda *a, **kw: captured.append(a[0] if a else {})

    await env.run(run_claude_cli, "sid-hb", "prose")

    assert len(captured) >= 1, "heartbeat never fired during a 0.5s run"
    # The payload shape must match the spec.
    payload = captured[0]
    assert set(payload.keys()) >= {
        "last_event_id",
        "last_tool",
        "event_count",
    }


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_heartbeat_reflects_event_tailer_updates(
    tmp_path, monkeypatch
):
    """As events land in events.jsonl, the ``latest`` payload must update.
    The last heartbeat before subprocess exit should reflect the latest
    tailed event.
    """
    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.HEARTBEAT_INTERVAL_SEC", 0.05
    )

    session_id = "sid-events"
    events_dir = tmp_path / ".swarm" / "state" / session_id
    events_dir.mkdir(parents=True)
    events_path = events_dir / "events.jsonl"
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_spawn(sid, prose, use_resume):
        # The tail task polls every 0.5s by default, so make the subprocess
        # live long enough for a tail cycle to observe the appended lines.
        return _spawn_sh("sleep 1.5; exit 0")

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.spawn_claude", fake_spawn
    )
    # Shrink the tail poll interval too for test speed.
    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.TAIL_POLL_INTERVAL_SEC", 0.05
    )

    captured: list[dict] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *a, **kw: captured.append(dict(a[0]) if a else {})

    # Write events as the activity runs — do it from a background task.
    async def writer():
        await asyncio.sleep(0.2)
        events_path.write_text(
            json.dumps({"id": "ev-1", "tool_name": "Read"}) + "\n"
        )
        await asyncio.sleep(0.2)
        with events_path.open("a") as f:
            f.write(json.dumps({"id": "ev-2", "tool_name": "Edit"}) + "\n")

    writer_task = asyncio.create_task(writer())
    try:
        result = await env.run(run_claude_cli, session_id, "prose")
    finally:
        writer_task.cancel()
        try:
            await writer_task
        except (asyncio.CancelledError, Exception):
            pass

    assert result.events == 2
    # The last heartbeat should reflect ev-2.
    last = captured[-1]
    assert last["event_count"] == 2
    assert last["last_event_id"] == "ev-2"
    assert last["last_tool"] == "Edit"


# ---------------------------------------------------------------------------
# 3. Cancellation SIGTERMs the process group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_cancel_sigterms_process_group(tmp_path, monkeypatch):
    """On ``activity.cancel()``, the activity must SIGTERM the process group
    of the subprocess. Verified by using a script that exits cleanly on
    SIGTERM and checking the subprocess has exited.
    """

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.HEARTBEAT_INTERVAL_SEC", 0.05
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.TAIL_POLL_INTERVAL_SEC", 0.05
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    # sh script: install a TERM trap that exits 0 cleanly, then sleep forever.
    cancel_script = "trap 'exit 0' TERM; sleep 60 & wait"

    # We need access to the Popen object from the test to inspect its state.
    captured_proc: dict[str, subprocess.Popen] = {}

    def fake_spawn(sid, prose, use_resume):
        p = _spawn_sh(cancel_script)
        captured_proc["p"] = p
        return p

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.spawn_claude", fake_spawn
    )

    env = ActivityEnvironment()

    async def _run():
        return await env.run(run_claude_cli, "sid-cancel", "prose")

    task = asyncio.create_task(_run())
    # Let the activity start the subprocess.
    await asyncio.sleep(0.3)
    assert "p" in captured_proc
    assert captured_proc["p"].poll() is None  # still running

    env.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # Subprocess should have received SIGTERM and exited within ~5s.
    # Give it a moment to reap.
    for _ in range(50):
        if captured_proc["p"].poll() is not None:
            break
        await asyncio.sleep(0.1)
    assert (
        captured_proc["p"].poll() is not None
    ), "subprocess should have exited after SIGTERM"


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_cancel_sigkills_stubborn_process_group(tmp_path, monkeypatch):
    """If the subprocess traps SIGTERM and refuses to exit, the activity
    must escalate to SIGKILL after 5s.

    For test speed we override ``SIGTERM_GRACE_SEC`` so we don't wait the
    full 5s — we only need to verify the escalation path works.
    """

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.HEARTBEAT_INTERVAL_SEC", 0.05
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.TAIL_POLL_INTERVAL_SEC", 0.05
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.SIGTERM_GRACE_SEC", 0.5
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    # Trap TERM and do nothing — only SIGKILL can kill this.
    stubborn_script = "trap '' TERM; sleep 60"

    captured_proc: dict[str, subprocess.Popen] = {}

    def fake_spawn(sid, prose, use_resume):
        p = _spawn_sh(stubborn_script)
        captured_proc["p"] = p
        return p

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.spawn_claude", fake_spawn
    )

    env = ActivityEnvironment()

    async def _run():
        return await env.run(run_claude_cli, "sid-stubborn", "prose")

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.3)
    env.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # SIGKILL should have taken it down.
    for _ in range(50):
        if captured_proc["p"].poll() is not None:
            break
        await asyncio.sleep(0.1)
    assert (
        captured_proc["p"].poll() is not None
    ), "stubborn subprocess should have been SIGKILLed"


# ---------------------------------------------------------------------------
# 4. Non-zero exit raises TransientError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_non_zero_exit_raises_transient_error(tmp_path, monkeypatch):
    """A non-zero exit code on normal completion must surface as
    ``TransientError`` so Temporal's retry policy can back off and retry.
    """

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.HEARTBEAT_INTERVAL_SEC", 0.05
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.TAIL_POLL_INTERVAL_SEC", 0.05
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_spawn(sid, prose, use_resume):
        return _spawn_sh(
            "echo 'boom' 1>&2; exit 7"
        )  # non-zero exit with stderr

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.spawn_claude", fake_spawn
    )

    env = ActivityEnvironment()
    with pytest.raises(TransientError) as exc_info:
        await env.run(run_claude_cli, "sid-fail", "prose")

    # The message should name the exit code for debuggability.
    assert "7" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. Zero exit returns ClaudeResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_zero_exit_returns_claude_result(tmp_path, monkeypatch):
    """A clean 0-exit must return a ``ClaudeResult`` with events=0 (no
    events were emitted to the tail) and exit_code=0."""

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.HEARTBEAT_INTERVAL_SEC", 0.05
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.TAIL_POLL_INTERVAL_SEC", 0.05
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    def fake_spawn(sid, prose, use_resume):
        return _spawn_sh("echo ok; exit 0")

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.spawn_claude", fake_spawn
    )

    env = ActivityEnvironment()
    result = await env.run(run_claude_cli, "sid-ok", "prose")

    assert isinstance(result, ClaudeResult)
    assert result.exit_code == 0
    assert result.events == 0


# ---------------------------------------------------------------------------
# 6. spawn_claude argv shape (direct helper test — no subprocess)
# ---------------------------------------------------------------------------


def test_spawn_claude_first_attempt_argv(monkeypatch):
    """When ``use_resume=False`` the argv must start with
    ``claude --session-id <sid> <mission_prose>`` so the session is created
    with its mission prose.
    """
    captured: dict = {}
    real_popen = subprocess.Popen  # capture before monkeypatch

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        # Return a dummy exited subprocess-like object.
        return real_popen(["sh", "-c", "exit 0"], **kwargs)

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.subprocess.Popen", fake_popen
    )

    from swarmd.durable.activities.run_claude_cli import spawn_claude

    p = spawn_claude("sid-x", "my prose", use_resume=False)
    p.wait()

    assert captured["args"] == ["claude", "--session-id", "sid-x", "my prose"]
    # Contract: new process group + piped stdout/stderr.
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stdout"] == subprocess.PIPE
    assert captured["kwargs"]["stderr"] == subprocess.PIPE


def test_spawn_claude_resume_attempt_argv(monkeypatch):
    """When ``use_resume=True`` the argv must be ``claude --resume <sid>`` —
    the mission_prose is dropped because claude CLI reads it from the
    persisted session state.
    """
    captured: dict = {}
    real_popen = subprocess.Popen  # capture before monkeypatch

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_popen(["sh", "-c", "exit 0"], **kwargs)

    monkeypatch.setattr(
        "swarmd.durable.activities.run_claude_cli.subprocess.Popen", fake_popen
    )

    from swarmd.durable.activities.run_claude_cli import spawn_claude

    p = spawn_claude("sid-y", "ignored on resume", use_resume=True)
    p.wait()

    assert captured["args"] == ["claude", "--resume", "sid-y"]
    assert captured["kwargs"]["start_new_session"] is True
