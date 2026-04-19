"""``run_claude_cli`` Temporal activity — the durability-critical mission runner.

Per spec §6.3 (activity table) and §7.3 (heartbeat contract):

    run_claude_cli(session_id, mission_prose) → ClaudeResult

The activity launches the mission subprocess (`claude` CLI) and is the only
piece of the swarm architecture that talks to the agent runtime. It must
survive any transient failure because Temporal retries it — losing this
activity without crash-safe semantics means losing the mission. The 2026-04-18
crash (HTTP 424) that motivated the whole redesign (spec §2.1) is the exact
failure this activity recovers from.

Contract (spec §7.3, reproduced here so changes can be reviewed in-place):

1.  **Attempt-based resume.** On ``activity.info().attempt == 1`` (first launch)
    spawn ``claude --session-id <sid> <mission_prose>``. On any retry
    (attempt > 1) spawn ``claude --resume <sid>`` — the session state is
    preserved by ``claude``'s own storage in ``~/.claude/``, so resumed
    retries pick up from the last completed tool call.

2.  **Process group.** The subprocess is spawned with
    ``start_new_session=True`` so it has its own process group. This is the
    *only* reliable way to cancel the whole tree (claude → subagents →
    grandchild tools) when the user runs ``swarm abort``.

3.  **Independent 30s heartbeat timer.** Heartbeats are NOT tied to event
    emission. Claude can run 3-5 minutes of pure reasoning with no tool
    calls; if the heartbeat depended on events, Temporal would declare the
    activity dead during those quiet stretches. The heartbeat loop is its
    own asyncio task and always fires on cadence.

4.  **Event tailing.** A concurrent task tails
    ``~/.swarm/state/<sid>/events.jsonl`` (written by the ``PostToolUse``
    hook inside ``claude``) and updates a shared ``latest`` dict. The next
    heartbeat picks up the new event info. This gives the mission workflow
    live progress via ``get_status`` queries without the activity having
    to report out-of-band.

5.  **Cancellation.** When the activity receives ``CancelledError``
    (from ``swarm abort`` or Temporal-side cancellation):
      a. SIGTERM the whole process group.
      b. Wait up to ``SIGTERM_GRACE_SEC`` (5s) for clean exit.
      c. SIGKILL the group if still alive.
      d. Re-raise the ``CancelledError`` so Temporal records the cancel.

6.  **Error classification.** A non-zero subprocess exit raises
    ``TransientError`` — Temporal's retry policy will back off and
    retry. Terminal errors (auth, billing, context overflow) would be
    raised as their specific types by caller-side parsing of stderr; this
    module keeps the subprocess-exit code path simple (transient by
    default) because the richer classification needs the full claude
    error-message corpus (future work).

Module-level constants are written to be monkeypatchable in tests so the
30s heartbeat doesn't make the test suite take forever.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio import activity

from swarmd.durable.errors import TransientError


# --- Timing constants (monkeypatchable in tests) -----------------------------

# Independent cadence of the heartbeat loop. Production is 30s; the
# ``run_claude_cli`` heartbeat timeout in ``retry_policies.HEARTBEAT_TIMEOUT_RUN_CLAUDE_CLI``
# is 2 minutes, so we have 4x safety margin against jittered schedulers.
HEARTBEAT_INTERVAL_SEC: float = 30.0

# How often we poll events.jsonl for new lines. 0.5s is tight enough to catch
# per-tool events without spinning the CPU.
TAIL_POLL_INTERVAL_SEC: float = 0.5

# How often the activity polls the subprocess for exit. 0.2s is tight enough
# that cancellation + clean subprocess exit complete in well under a second.
PROCESS_POLL_INTERVAL_SEC: float = 0.2

# Grace period between SIGTERM and SIGKILL on cancellation. 5s matches the
# spec (§7.3). Tests override this to 0.5s to keep the suite fast.
SIGTERM_GRACE_SEC: float = 5.0


# --- Result dataclass --------------------------------------------------------


@dataclass
class ClaudeResult:
    """Return value from a successful ``run_claude_cli`` invocation.

    ``events`` is the count of events observed via the tail of
    ``events.jsonl`` during this activity's lifetime — NOT the cumulative
    events across retries. The mission workflow aggregates across retries
    from its own Temporal history if it needs the total.

    ``exit_code`` is always 0 on success (non-zero exits raise
    ``TransientError``). It exists as a field for symmetry with other
    subprocess-returning result types and to make the successful-completion
    log line easy to grep.
    """

    events: int
    exit_code: int


# --- Pure helpers ------------------------------------------------------------


def should_use_resume(attempt: int) -> bool:
    """Return ``True`` iff a retry attempt should use ``claude --resume``.

    Factored out from ``run_claude_cli`` so the attempt-based resume logic
    can be unit-tested without spawning a subprocess or constructing an
    ``ActivityEnvironment``. The rule is simple (first attempt = fresh
    session, any retry = resume), but the decision is load-bearing for
    durability so we keep it nameable.
    """
    return attempt > 1


def spawn_claude(
    session_id: str, mission_prose: str, use_resume: bool
) -> subprocess.Popen:
    """Spawn the ``claude`` CLI in its own process group.

    On ``use_resume=False`` (first launch) argv is
    ``["claude", "--session-id", <sid>, <mission_prose>]`` — claude creates
    the session with the given id and starts working on the mission prose.

    On ``use_resume=True`` (retry) argv is ``["claude", "--resume", <sid>]`` —
    claude re-attaches to the existing session and continues from the last
    completed tool call; the mission prose is not re-supplied because
    claude CLI reads it from the persisted session state.

    ``start_new_session=True`` puts the subprocess in its own process group,
    which is essential for ``os.killpg()`` on cancellation to take down
    grandchild processes (subagents, shell tools, etc.) that claude may
    have spawned.

    stdout/stderr are piped so the activity can capture the final exit
    stderr for error messages.
    """
    if use_resume:
        args = ["claude", "--resume", session_id]
    else:
        args = ["claude", "--session-id", session_id, mission_prose]
    return subprocess.Popen(
        args,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _events_path(session_id: str) -> Path:
    """Where ``PostToolUse`` hook writes events for this session.

    Defined as a helper so tests can monkeypatch ``HOME`` and the path
    resolves to a tmp dir. The shape of this path is pinned in spec §8.2.
    """
    return Path.home() / ".swarm" / "state" / session_id / "events.jsonl"


# --- Concurrent tasks --------------------------------------------------------


async def _tail_events(
    session_id: str, latest: dict[str, Any], stop: asyncio.Event
) -> None:
    """Poll ``events.jsonl`` for new lines; push most recent event into
    ``latest``.

    The spec (§8.2) mandates events.jsonl as the source of truth for raw
    agent telemetry; this tailer is the bridge that feeds the heartbeat
    payload so the mission workflow's ``get_status`` query reflects live
    progress.

    Implementation: snapshot the number of lines seen, poll the file every
    ``TAIL_POLL_INTERVAL_SEC``, parse any new lines as JSON, and update
    ``latest`` in place. A ``stop`` event lets the main activity cleanly
    unwind this task when the subprocess exits.

    Malformed JSON lines are silently tolerated — the tailer's job is
    best-effort heartbeat population, not schema enforcement.
    """
    events_path = _events_path(session_id)
    seen = 0
    while not stop.is_set():
        try:
            if events_path.exists():
                # read_text is fine: events.jsonl stays small (on the order
                # of KBs for most missions; spec §11 notes multi-MB is the
                # outlier requiring rotation).
                lines = events_path.read_text().splitlines()
                if len(lines) > seen:
                    for line in lines[seen:]:
                        try:
                            ev = json.loads(line)
                        except Exception:
                            # One corrupt line shouldn't kill the tail loop.
                            continue
                        latest["last_event_id"] = ev.get("id")
                        latest["last_tool"] = ev.get("tool_name")
                        latest["event_count"] = (
                            latest.get("event_count", 0) + 1
                        )
                    seen = len(lines)
        except OSError:
            # Transient filesystem hiccup — keep polling.
            pass
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=TAIL_POLL_INTERVAL_SEC
            )
        except asyncio.TimeoutError:
            # Timeout is the normal path — it means ``stop`` is still
            # unset and we should poll the file again.
            pass


async def _heartbeat_loop(
    latest: dict[str, Any], stop: asyncio.Event
) -> None:
    """Fire ``activity.heartbeat(latest)`` on an independent cadence.

    Spec §7.3: heartbeats are NOT tied to event emission. A long reasoning
    pause (3-5min of claude thinking) must NOT look like a dead activity.

    The loop uses ``asyncio.wait_for(stop.wait(), ...)`` instead of
    ``asyncio.sleep(...)`` so that when the subprocess exits (``stop`` is
    set) we unwind immediately instead of waiting out the full interval.

    The first heartbeat is fired *before* the first sleep so even very
    short subprocesses record at least one heartbeat — useful as a live-ness
    signal for the mission workflow's first status query.
    """
    while not stop.is_set():
        activity.heartbeat(latest)
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=HEARTBEAT_INTERVAL_SEC
            )
        except asyncio.TimeoutError:
            # Timeout is normal — loop around for another heartbeat.
            pass


async def _wait_for_proc(
    proc: subprocess.Popen, stop: asyncio.Event
) -> None:
    """Poll the subprocess for exit; set ``stop`` when it finishes.

    ``subprocess.Popen.poll()`` is non-blocking and sets ``returncode`` as
    a side effect. Polling at ``PROCESS_POLL_INTERVAL_SEC`` gives a tight
    enough response that neither the heartbeat loop nor the tail loop
    lingers after the subprocess finishes.

    Setting ``stop`` is the fan-in signal that lets the other two tasks
    exit cleanly — without it they'd run forever waiting for events or
    heartbeats that will never come.
    """
    while True:
        if proc.poll() is not None:
            stop.set()
            return
        await asyncio.sleep(PROCESS_POLL_INTERVAL_SEC)


# --- The activity itself -----------------------------------------------------


@activity.defn(name="run_claude_cli")
async def run_claude_cli(
    session_id: str, mission_prose: str
) -> ClaudeResult:
    """Launch the ``claude`` mission subprocess and supervise it until exit.

    See the module docstring for the full contract. Summary:

    * Attempt 1 → ``claude --session-id <sid> <prose>``.
    * Attempt >1 → ``claude --resume <sid>``.
    * Independent 30s heartbeats report live progress via the heartbeat
      payload ``{"last_event_id", "last_tool", "event_count"}``.
    * ``CancelledError`` → SIGTERM the process group, wait 5s, SIGKILL,
      re-raise.
    * Non-zero exit → raise ``TransientError`` with the exit code + tail
      of stderr in the message.
    """
    attempt = activity.info().attempt
    use_resume = should_use_resume(attempt)
    proc = spawn_claude(session_id, mission_prose, use_resume=use_resume)

    latest: dict[str, Any] = {
        "last_event_id": None,
        "last_tool": None,
        "event_count": 0,
    }
    stop = asyncio.Event()

    try:
        # gather() runs all three tasks concurrently. ``_wait_for_proc`` is
        # the "driver" — it sets ``stop`` when the subprocess exits, which
        # causes the other two to unwind. The heartbeat and tail loops
        # catch their own timeouts and exceptions so a single task failing
        # does NOT kill the others via gather's default fast-fail
        # semantics.
        await asyncio.gather(
            _heartbeat_loop(latest, stop),
            _tail_events(session_id, latest, stop),
            _wait_for_proc(proc, stop),
        )
    except asyncio.CancelledError:
        # Cancellation path — ``swarm abort`` or Temporal-side cancel. We
        # must take down the whole process group (claude's grandchildren
        # include subagents and shell tools) before re-raising so Temporal
        # records a clean cancel.
        _terminate_process_group(proc)
        raise

    if proc.returncode != 0:
        # Read whatever stderr is available for the error message. Use a
        # non-blocking read so we don't hang forever if the pipe is still
        # buffered — ``communicate()`` is fine because the process has
        # exited.
        stderr_bytes = b""
        if proc.stderr is not None:
            try:
                stderr_bytes = proc.stderr.read() or b""
            except Exception:
                stderr_bytes = b""
        stderr_tail = stderr_bytes.decode(errors="replace")[-500:]
        raise TransientError(
            f"claude exited {proc.returncode}: {stderr_tail}"
        )

    return ClaudeResult(events=latest["event_count"], exit_code=0)


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the subprocess's process group; SIGKILL if still alive.

    Factored out so the cancellation path reads cleanly. Uses ``killpg``
    (not ``kill``) because we spawned with ``start_new_session=True``;
    this catches any grandchild processes claude may have spawned
    (subagents, shell tools).

    ``ProcessLookupError`` / ``OSError`` are tolerated: the process may
    already be dead by the time we try to kill it, in which case there's
    nothing to do.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return

    # Wait up to SIGTERM_GRACE_SEC for the subprocess to exit. Poll
    # synchronously because we're already inside the CancelledError
    # handler and asyncio sleeps may not be honored depending on the
    # cancellation shape — a short busy wait is safest.
    deadline = _monotonic() + SIGTERM_GRACE_SEC
    while _monotonic() < deadline:
        if proc.poll() is not None:
            return
        # Small fixed sleep; OK to block because we're in the cancel
        # handler and need to finish before re-raising.
        _sleep(0.1)

    # Still alive — SIGKILL the group.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


# --- Sleep / clock indirection (kept for test clarity) -----------------------
# Using stdlib directly here because ``_terminate_process_group`` runs inside
# the CancelledError handler where asyncio constructs can behave oddly; a
# bare ``time.sleep`` + ``time.monotonic`` pair is the simplest contract.


def _monotonic() -> float:
    import time

    return time.monotonic()


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
