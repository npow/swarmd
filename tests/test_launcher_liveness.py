"""Unit tests for swarm.lib.launcher_liveness.

Specialists must self-terminate when the launcher bash process is gone
(SIGKILL, terminal SIGHUP, machine reboot, launcher crash). The liveness
check is a pid-file handshake: launcher writes its pid, specialists poll.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from swarmd.lib.launcher_liveness import (
    exit_if_launcher_dead,
    launcher_alive,
    launcher_pid_path,
    write_launcher_pid,
)


def test_launcher_alive_false_when_pid_file_missing(session_id):
    # No launcher.pid has been written — specialists must see "dead".
    assert launcher_alive(session_id) is False


def test_launcher_alive_true_for_live_pid(session_id):
    write_launcher_pid(session_id, os.getpid())
    assert launcher_alive(session_id) is True


def test_launcher_alive_false_for_dead_pid(session_id):
    # Spawn a child that exits immediately, capture its pid, wait for it
    # to die, then check. The pid value should no longer belong to any
    # live process.
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    dead_pid = proc.pid
    proc.wait()
    # Reap so it's not a zombie (zombies still respond to kill(pid, 0)).
    # Popen.wait() reaps, so dead_pid is fully gone.
    # Give the kernel a moment just in case.
    time.sleep(0.1)

    write_launcher_pid(session_id, dead_pid)
    assert launcher_alive(session_id) is False


def test_launcher_alive_false_on_malformed_file(session_id):
    path = launcher_pid_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-a-pid\n")
    assert launcher_alive(session_id) is False


def test_launcher_alive_false_on_zero_pid(session_id):
    # pid 0 is not a valid process — os.kill(0, 0) signals the process
    # group, which would return True spuriously. Guard against it.
    write_launcher_pid(session_id, 0)
    assert launcher_alive(session_id) is False


def test_launcher_alive_false_on_negative_pid(session_id):
    # Negative pid is a process-group selector in os.kill — reject it.
    path = launcher_pid_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("-1\n")
    assert launcher_alive(session_id) is False


def test_launcher_alive_false_on_empty_file(session_id):
    path = launcher_pid_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    assert launcher_alive(session_id) is False


def test_exit_if_launcher_dead_raises_system_exit(session_id):
    # No pid file written → specialists must exit(0).
    with pytest.raises(SystemExit) as excinfo:
        exit_if_launcher_dead(session_id)
    assert excinfo.value.code == 0


def test_exit_if_launcher_dead_returns_when_alive(session_id):
    write_launcher_pid(session_id, os.getpid())
    # Should NOT raise.
    exit_if_launcher_dead(session_id)


def test_launcher_pid_path_is_under_session_dir(session_id):
    p = launcher_pid_path(session_id)
    assert p.name == "launcher.pid"
    # Must be inside the session state dir, not anywhere else.
    assert session_id in str(p)


def test_write_launcher_pid_defaults_to_current_pid(session_id):
    write_launcher_pid(session_id)  # no explicit pid
    assert launcher_pid_path(session_id).read_text().strip() == str(os.getpid())


def test_write_launcher_pid_creates_parent_dir(tmp_swarm_root, session_id):
    # Even if the health subdir etc. haven't been set up, writing the pid
    # must succeed. Deliberately remove the parent first.
    import shutil

    from swarmd.lib.paths import session_dir

    shutil.rmtree(session_dir(session_id))
    write_launcher_pid(session_id, os.getpid())
    assert launcher_pid_path(session_id).exists()
