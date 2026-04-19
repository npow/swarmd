"""Tests for the ``restart_subprocess`` Temporal activity.

Per plan Task 12 and spec §6.2:

    restart_subprocess(subagent_id, old_pid, respawn_request) -> RestartResult

    Kills an existing subagent by process group (SIGTERM, wait 5s, SIGKILL)
    and respawns it with the provided request. The ``subagent_id`` is
    PRESERVED across restart — the caller's contract is "same logical
    subagent, new OS process."

The tests monkeypatch ``os.killpg``, ``os.getpgid``, ``os.waitpid``, and
``subprocess.Popen`` so no real processes are launched.
"""

from __future__ import annotations

import signal
import subprocess

import pytest
from temporalio.testing import ActivityEnvironment

from swarmd.durable.activities.restart_subprocess import (
    RestartResult,
    restart_subprocess,
)
from swarmd.durable.errors import TransientError


def _respawn_request(**overrides) -> dict:
    base = {
        "parent_id": "parent-abc",
        "depth": 2,
        "prompt": "restarted prompt",
        "workspace": "/tmp/ws",
        "mission_id": "m-abc",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Happy path — clean SIGTERM exit, then respawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_sigterm_respawn(monkeypatch):
    """Old pgid receives SIGTERM, process exits fast, new process spawned,
    RestartResult shows both pids + preserved subagent_id."""
    killed: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killed.append((pgid, sig))

    def fake_getpgid(pid):
        return pid  # pretend pgid == pid

    # Simulate "process exited" on first waitpid call.
    waitpid_calls = {"n": 0}

    def fake_waitpid(pid, flags):
        waitpid_calls["n"] += 1
        return (pid, 0)  # (pid, status) both nonzero means "reaped"

    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.killpg", fake_killpg
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.getpgid",
        fake_getpgid,
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.waitpid",
        fake_waitpid,
    )

    # Popen for the respawn — return a real sh process so .pid works.
    real_popen = subprocess.Popen

    def fake_popen(args, **kwargs):
        return real_popen(["sh", "-c", "exit 0"], start_new_session=True)

    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        fake_popen,
    )

    env = ActivityEnvironment()
    result = await env.run(
        restart_subprocess, "sub-123", 99999, _respawn_request()
    )

    assert isinstance(result, RestartResult)
    assert result.old_pid == 99999
    assert result.new_pid > 0
    # subagent_id must be PRESERVED across restart.
    assert result.subagent_id == "sub-123"
    # SIGTERM was sent first (and that's all needed if the proc exited fast).
    assert killed[0] == (99999, signal.SIGTERM)


# ---------------------------------------------------------------------------
# 2. SIGKILL escalation when process won't exit on SIGTERM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sigkill_after_grace(monkeypatch):
    """If the process won't exit on SIGTERM within the grace window,
    the activity must escalate to SIGKILL."""
    # Override the grace to make the test fast.
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.SIGTERM_GRACE_SEC",
        0.2,
    )

    killed: list[tuple[int, int]] = []

    def fake_killpg(pgid, sig):
        killed.append((pgid, sig))

    def fake_getpgid(pid):
        return pid

    # waitpid returns (0, 0) forever == "process still alive".
    def fake_waitpid(pid, flags):
        return (0, 0)

    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.killpg", fake_killpg
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.getpgid",
        fake_getpgid,
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.waitpid",
        fake_waitpid,
    )

    real_popen = subprocess.Popen

    def fake_popen(args, **kwargs):
        return real_popen(["sh", "-c", "exit 0"], start_new_session=True)

    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        fake_popen,
    )

    env = ActivityEnvironment()
    result = await env.run(
        restart_subprocess, "sub-X", 42, _respawn_request()
    )

    # Both SIGTERM then SIGKILL were sent.
    signals_sent = [s for (_, s) in killed]
    assert signal.SIGTERM in signals_sent
    assert signal.SIGKILL in signals_sent
    # Order: SIGTERM first, then SIGKILL.
    assert signals_sent.index(signal.SIGTERM) < signals_sent.index(
        signal.SIGKILL
    )
    assert isinstance(result, RestartResult)


# ---------------------------------------------------------------------------
# 3. ProcessLookupError on initial killpg is ignored (already dead)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_lookup_error_treated_as_already_dead(monkeypatch):
    """ProcessLookupError on killpg means the process is already dead.
    The activity should proceed to respawn without error."""

    def fake_killpg(pgid, sig):
        raise ProcessLookupError("no such process")

    def fake_getpgid(pid):
        return pid

    def fake_waitpid(pid, flags):
        return (pid, 0)

    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.killpg", fake_killpg
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.getpgid",
        fake_getpgid,
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.waitpid",
        fake_waitpid,
    )

    real_popen = subprocess.Popen

    def fake_popen(args, **kwargs):
        return real_popen(["sh", "-c", "exit 0"], start_new_session=True)

    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        fake_popen,
    )

    env = ActivityEnvironment()
    result = await env.run(
        restart_subprocess, "sub-dead", 1, _respawn_request()
    )

    assert isinstance(result, RestartResult)
    assert result.subagent_id == "sub-dead"
    assert result.new_pid > 0


@pytest.mark.asyncio
async def test_getpgid_process_lookup_tolerated(monkeypatch):
    """os.getpgid raising ProcessLookupError means the process is already
    dead. The activity should proceed to respawn anyway."""

    def fake_getpgid(pid):
        raise ProcessLookupError("no such pid")

    def fake_killpg(pgid, sig):
        pytest.fail("killpg should not be called when getpgid fails")

    def fake_waitpid(pid, flags):
        return (pid, 0)

    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.getpgid",
        fake_getpgid,
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.killpg", fake_killpg
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.waitpid",
        fake_waitpid,
    )

    real_popen = subprocess.Popen

    def fake_popen(args, **kwargs):
        return real_popen(["sh", "-c", "exit 0"], start_new_session=True)

    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        fake_popen,
    )

    env = ActivityEnvironment()
    result = await env.run(
        restart_subprocess, "sub-gone", 7777, _respawn_request()
    )

    assert result.subagent_id == "sub-gone"
    assert result.new_pid > 0


# ---------------------------------------------------------------------------
# 4. Respawn failure → TransientError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respawn_oserror_raises_transient(monkeypatch):
    """If the respawn subprocess.Popen itself raises OSError, the activity
    surfaces TransientError so Temporal retries."""

    def fake_killpg(pgid, sig):
        pass

    def fake_getpgid(pid):
        return pid

    def fake_waitpid(pid, flags):
        return (pid, 0)

    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.killpg", fake_killpg
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.getpgid",
        fake_getpgid,
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.waitpid",
        fake_waitpid,
    )

    def raise_oserror(*args, **kwargs):
        raise OSError("cannot spawn on restart")

    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        raise_oserror,
    )

    env = ActivityEnvironment()
    with pytest.raises(TransientError):
        await env.run(
            restart_subprocess, "sub-err", 10, _respawn_request()
        )


# ---------------------------------------------------------------------------
# 5. subagent_id preservation is the caller's contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_id_preserved_across_restart(monkeypatch):
    """The subagent_id passed in must be identical on the way out. The
    activity does NOT generate a new one — that's the caller's job."""

    def fake_killpg(pgid, sig):
        pass

    def fake_getpgid(pid):
        return pid

    def fake_waitpid(pid, flags):
        return (pid, 0)

    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.killpg", fake_killpg
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.getpgid",
        fake_getpgid,
    )
    monkeypatch.setattr(
        "swarmd.durable.activities.restart_subprocess.os.waitpid",
        fake_waitpid,
    )

    captured: dict = {}
    real_popen = subprocess.Popen

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return real_popen(["sh", "-c", "exit 0"], start_new_session=True)

    monkeypatch.setattr(
        "swarmd.durable.activities.spawn_subagent.subprocess.Popen",
        fake_popen,
    )

    env = ActivityEnvironment()
    result = await env.run(
        restart_subprocess,
        "original-subagent-id-uuid-1234",
        555,
        _respawn_request(),
    )

    assert result.subagent_id == "original-subagent-id-uuid-1234"
    # The same id must also appear in the respawn argv as --session-id.
    args = captured["args"]
    assert "original-subagent-id-uuid-1234" in args
