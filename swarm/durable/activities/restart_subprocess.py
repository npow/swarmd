"""``restart_subprocess`` Temporal activity — kill + respawn a subagent.

Per plan Task 12 and spec §6.2:

    restart_subprocess(subagent_id, old_pid, respawn_request) → RestartResult

Kills the existing subagent by its process group (same SIGTERM → wait →
SIGKILL pattern as ``run_claude_cli``'s cancel handler) and respawns
it with the provided request. The ``subagent_id`` is PRESERVED across
restart — the caller's contract is "same logical subagent identity,
new OS process." This matters because downstream state (events.jsonl,
findings) is keyed by subagent_id; regenerating it would orphan
existing artifacts.

Contract:

* ``os.killpg(os.getpgid(old_pid), SIGTERM)`` — kill the whole process
  group to sweep any shell tools / grandchildren the subagent spawned.
* Wait up to ``SIGTERM_GRACE_SEC`` seconds, polling ``os.waitpid`` with
  ``WNOHANG``. Any non-zero return means the process was reaped.
* If still alive: ``os.killpg(pgid, SIGKILL)``.
* Respawn via the shared ``_launch_claude_subprocess`` helper from
  ``spawn_subagent``. We import it inside the function to avoid any
  circular-import concern (the sibling module also lives under
  ``swarm.durable.activities``).
* Errors:
    - ``ProcessLookupError`` on ``getpgid`` / ``killpg`` → tolerated
      (the process is already dead, which is fine — proceed to respawn).
    - ``OSError`` from the respawn path → ``TransientError`` via
      ``_launch_claude_subprocess``'s classification (the helper already
      translates).
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass

from temporalio import activity

# Grace period between SIGTERM and SIGKILL. Mirrors
# ``run_claude_cli.SIGTERM_GRACE_SEC``. Kept monkeypatchable for tests.
SIGTERM_GRACE_SEC: float = 5.0

# Poll interval while waiting for the process group to reap after SIGTERM.
# 100ms is tight enough that the restart path completes well under a
# second in the happy case.
POLL_INTERVAL_SEC: float = 0.1


@dataclass
class RestartResult:
    """Return value from ``restart_subprocess``.

    ``old_pid`` and ``new_pid`` let the caller update whatever pid
    bookkeeping it maintains (e.g. the tree.json replacement in the
    MissionWorkflow's state). ``subagent_id`` is the SAME identity that
    was passed in — retained on the result for type symmetry with
    ``SpawnResult`` and so callers can pattern-match either shape.
    """

    old_pid: int
    new_pid: int
    subagent_id: str


def _terminate_process_group(pid: int) -> None:
    """SIGTERM the process group containing ``pid``; SIGKILL if still alive.

    Tolerates:
        * ``ProcessLookupError`` on ``getpgid`` — pid already gone.
        * ``ProcessLookupError`` / ``OSError`` on ``killpg`` — group gone.

    The legacy spawner (``specialists/spawner.py``) did not have this
    restart codepath; the SIGTERM+SIGKILL escalation mirrors the
    cancellation path in ``run_claude_cli`` (see ``_terminate_process_group``
    in that module).
    """
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, OSError):
        # Process is already dead — nothing to kill.
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        # Race: process died between getpgid and killpg. Fine.
        return

    # Poll for exit. ``os.waitpid(pid, WNOHANG)`` returns ``(0, 0)`` when
    # the process is still alive and ``(pid, status)`` once reaped.
    # ECHILD is possible if the process isn't our child; we treat any
    # non-(0, 0) return as "gone".
    deadline = time.monotonic() + SIGTERM_GRACE_SEC
    while time.monotonic() < deadline:
        try:
            done_pid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if done_pid != 0:
            return
        time.sleep(POLL_INTERVAL_SEC)

    # Still alive after the grace window — escalate to SIGKILL.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


@activity.defn(name="restart_subprocess")
async def restart_subprocess(
    subagent_id: str,
    old_pid: int,
    respawn_request: dict,
) -> RestartResult:
    """Kill by process group, wait up to 5s, SIGKILL if needed, then respawn.

    Args:
        subagent_id: The logical id to preserve across restart. This is
            used as the ``--session-id`` for the new process, matching
            the original subagent's session on disk.
        old_pid: The OS pid of the subagent being restarted.
        respawn_request: Same shape as ``spawn_subagent``'s ``request``;
            ``prompt``, ``workspace``, etc. The new process uses THIS
            request — the caller may have adjusted prompt or workspace
            as part of the restart rationale.

    Raises:
        TransientError: OS-level failure during respawn. Temporal will
            retry under the ``RESTART_SUBPROCESS`` policy.
    """
    _terminate_process_group(old_pid)

    # Import inside the function so there is no chance of a circular
    # import during package load. ``_launch_claude_subprocess`` already
    # translates OSError / FileNotFoundError into TransientError.
    from swarm.durable.activities.spawn_subagent import (
        _build_argv,
        _launch_claude_subprocess,
    )

    argv = _build_argv(
        subagent_id=subagent_id,  # PRESERVED across restart.
        prompt=str(respawn_request.get("prompt", "")),
        model=respawn_request.get("model"),
    )
    new_pid, _cmd = _launch_claude_subprocess(
        argv, cwd=str(respawn_request.get("workspace"))
        if respawn_request.get("workspace") is not None
        else None,
    )
    return RestartResult(
        old_pid=old_pid,
        new_pid=new_pid,
        subagent_id=subagent_id,
    )
