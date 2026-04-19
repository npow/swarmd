"""``spawn_subagent`` Temporal activity — launches a subagent subprocess.

Per plan Task 12 and spec §6.2:

    spawn_subagent(request) → SpawnResult

PORTED from ``specialists/spawner.py``. The legacy spawner did admission
control (max_depth / max_fan_out_per_parent / max_total_live) inline
with the spawn; per plan §6.2 admission control moves to workflow state
— the MissionWorkflow (Task 13) checks those limits before calling this
activity. This activity ONLY does the actual spawn: build the claude
argv and launch the process in its own process group.

Fire-and-forget: we return as soon as the ``Popen`` handle is in hand.
Supervision (heartbeats, restarts, zombie-reaping) is the workflow's
job — this activity keeps its semantics tight so Temporal's retry
budget only ever covers the spawn itself, not long-running agent
behavior.

Contract:

* Generates a uuid4 for ``subagent_id``.
* Builds argv with ``--session-id`` (so the subagent is addressable via
  the same session-id that Claude CLI persists under ``~/.claude/``) and
  ``--dangerously-skip-permissions`` (subagents run fully unattended).
* Passes ``start_new_session=True`` to ``subprocess.Popen`` so the new
  process has its own process group — a requirement for
  ``restart_subprocess`` to ``killpg`` cleanly on restart.
* Raises ``TerminalError`` on malformed ``request`` payloads (missing
  required keys). Retries cannot fix a bad contract.
* Raises ``TransientError`` on ``OSError`` / ``FileNotFoundError`` from
  ``Popen``. The CLI may become available again (fs remount, PATH
  fix, etc.), so Temporal should back off and retry.

``_launch_claude_subprocess`` is the shared helper that
``restart_subprocess`` also uses — extracted here so both activities
produce identical argv shapes without duplicating launching logic.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field

from temporalio import activity

from swarmd.durable.errors import TerminalError, TransientError


# ``request`` keys the activity REQUIRES to be present. ``parent_id`` is
# allowed to be ``None`` (for the root spawn), so it is not in the required
# set — we only check presence, not truthiness. ``model`` is optional.
_REQUIRED_REQUEST_KEYS = ("depth", "prompt", "workspace", "mission_id")


@dataclass
class SpawnResult:
    """Return value from a successful ``spawn_subagent`` invocation.

    ``subagent_id`` is a freshly minted uuid4 that the workflow uses as
    the logical identity of the subagent. ``pid`` is the OS pid of the
    new subprocess. ``cmd`` is the full argv used; retained for
    debugging — admission-control logs, post-mortem inspection, etc.
    """

    subagent_id: str
    pid: int
    parent_id: str | None
    depth: int
    cmd: list[str] = field(default_factory=list)


def _build_argv(
    subagent_id: str,
    prompt: str,
    model: str | None = None,
) -> list[str]:
    """Construct the claude CLI argv for a subagent spawn.

    The argv mirrors ``run_daemon_once`` in the legacy spawner
    (``--session-id <id>`` and the prompt as the final positional), and
    adds ``--dangerously-skip-permissions`` because subagents run
    unattended and cannot answer interactive permission prompts.

    An optional ``--model <model>`` flag lets the caller pin a non-default
    model (e.g. opus for expensive subagents, sonnet for general, haiku
    for judges).
    """
    argv = [
        "claude",
        "--session-id",
        subagent_id,
        "--dangerously-skip-permissions",
    ]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    return argv


def _launch_claude_subprocess(
    argv: list[str], cwd: str | None = None
) -> tuple[int, list[str]]:
    """Spawn a claude subprocess in its own process group.

    Shared by ``spawn_subagent`` and ``restart_subprocess``. Kept as a
    module-private helper so the two activities never drift in how they
    construct the process.

    Returns ``(pid, argv)`` — the argv is echoed back so the caller can
    record it on the result dataclass without rebuilding it.

    ``start_new_session=True`` puts the subprocess in its own process
    group, which is the only way to ``os.killpg`` the whole tree on
    restart (see ``restart_subprocess``). Stdin/stdout/stderr are
    redirected to ``DEVNULL`` — the subagent writes its own events to
    ``~/.swarm/state/<session_id>/events.jsonl`` via the ``PostToolUse``
    hook, not via the activity's pipes.

    Raises:
        TransientError: ``OSError`` / ``FileNotFoundError`` from ``Popen``.
            The CLI may come back; Temporal should retry with backoff.
    """
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as exc:
        raise TransientError(f"spawn failed: {exc}") from exc
    return proc.pid, argv


@activity.defn(name="spawn_subagent")
async def spawn_subagent(request: dict) -> SpawnResult:
    """Spawn a subagent subprocess. Admission control is the workflow's job.

    ``request`` keys:

    * ``parent_id`` (str | None)  — parent subagent id; ``None`` for root.
    * ``depth`` (int)             — 0 for root, parent.depth+1 for child.
    * ``prompt`` (str)            — the mission/role prompt.
    * ``workspace`` (str)         — cwd the subagent runs in.
    * ``mission_id`` (str)        — passthrough for logging / finding tagging.
    * ``model`` (str, optional)   — claude model id override; default omitted.

    Raises:
        TerminalError: Missing required request keys. Retries cannot fix
            a malformed request.
        TransientError: OS-level spawn failure. Retries may succeed once
            the CLI / PATH / filesystem is healthy again.
    """
    missing = [k for k in _REQUIRED_REQUEST_KEYS if k not in request]
    if missing:
        raise TerminalError(
            f"spawn_subagent: missing required request keys: {missing}"
        )

    subagent_id = str(uuid.uuid4())
    argv = _build_argv(
        subagent_id=subagent_id,
        prompt=str(request["prompt"]),
        model=request.get("model"),
    )
    pid, cmd = _launch_claude_subprocess(argv, cwd=str(request["workspace"]))
    return SpawnResult(
        subagent_id=subagent_id,
        pid=pid,
        parent_id=request.get("parent_id"),
        depth=int(request["depth"]),
        cmd=cmd,
    )
