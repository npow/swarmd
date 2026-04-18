"""Launcher-liveness handshake for specialists.

Problem this solves
-------------------
Specialist daemons (coordinator, pattern_detector, ...) are spawned by
launch.sh. The launcher installs a bash `trap cleanup EXIT INT TERM` that
SIGTERMs the process group on exit. But bash traps do NOT fire on SIGKILL,
on an uncontrolled terminal SIGHUP, or on a machine crash — in those cases
the specialists are reparented to launchd/init and run forever. This has
been observed in the wild: 74 orphaned python daemons across 45 session
IDs with no matching state dirs.

Mechanism
---------
The launcher writes `$SESSION_STATE/launcher.pid` BEFORE spawning any
specialists. Each specialist calls `exit_if_launcher_dead(session_id)`
at the top of every main-loop tick. If the pid file is missing, malformed,
or points to a dead/invalid pid, the specialist calls `sys.exit(0)` —
clean, traceable, and bounded by the loop cadence.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from swarm.lib.paths import session_dir


def launcher_pid_path(session_id: str) -> Path:
    """Path to the launcher-pid file for a session."""
    return session_dir(session_id) / "launcher.pid"


def write_launcher_pid(session_id: str, pid: int | None = None) -> None:
    """Record the launcher's PID so specialists can check it's still alive.

    Called once by launch.sh (via a small helper) before specialists spawn,
    so there is no window where a specialist could be running without a
    valid pid file. Idempotent — safe to re-run.
    """
    path = launcher_pid_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    effective_pid = pid if pid is not None else os.getpid()
    path.write_text(f"{effective_pid}\n")


def launcher_alive(session_id: str) -> bool:
    """Return True iff the launcher's pid is recorded AND the pid is running.

    Returns False for:
      - missing pid file (launcher never started, or state cleaned up)
      - empty or malformed pid file
      - pid <= 0 (invalid; 0 and negative are process-group selectors in
        os.kill, which would otherwise produce spurious Trues)
      - ProcessLookupError from os.kill(pid, 0)
    Returns True when os.kill(pid, 0) succeeds OR raises PermissionError
    (which means the pid exists but is owned by a different user — still
    "alive" from the specialist's point of view).
    """
    path = launcher_pid_path(session_id)
    if not path.exists():
        return False
    try:
        raw = path.read_text().strip()
    except OSError:
        return False
    if not raw:
        return False
    try:
        pid = int(raw)
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # pid exists, just not ours to signal — still alive.
        return True
    except OSError:
        return False
    return True


def exit_if_launcher_dead(
    session_id: str, logger: logging.Logger | None = None
) -> None:
    """If the launcher is gone, log and SystemExit(0).

    Intended for use at the top of every specialist main-loop iteration.
    A clean exit (code 0) distinguishes orphan-cleanup from real crashes
    in logs and metrics.
    """
    if launcher_alive(session_id):
        return
    if logger is not None:
        logger.warning(
            "launcher.pid missing or points to a dead process for "
            "session=%s; exiting cleanly to avoid specialist leak",
            session_id,
        )
    raise SystemExit(0)
