"""``swarm`` CLI — launch / status / abort / worker / health / findings / logs.

Per spec §10 (CLI surface) and plan Task 19. Seven subcommands:

* ``launch <mission.yaml> [--workspace PATH]`` — validate, lock, start
  the ``MissionWorkflow`` on the local Temporal server. Reuses
  ``swarm.mcp.server._launch_mission`` so the CLI path does the exact
  same pre-flight (task-queue probe, workspace lock, Mission validation)
  as the MCP ``launch`` tool.

* ``status <workflow_id>`` — pretty-print the ``get_status`` query
  result. Workflow-not-found → exit 1.

* ``abort <workflow_id> [--reason "..."]`` — send the ``abort`` signal.
  Default reason is ``"user-abort"``.

* ``worker`` — run the Temporal worker daemon. Delegates to
  ``swarm.durable.worker.main`` (today a stub; Task 18 replaces it).

* ``health`` — three-row readiness table: Temporal reachable, worker
  liveness (pollers > 0), classifier API (1-token Haiku ping).

* ``findings <workflow_id> [--tail N] [--type TYPE]`` — tail
  ``~/.swarm/state/<session_id>/findings.jsonl`` with optional type
  filter. Missing file → "no findings yet" (exit 0).

* ``logs <workflow_id>`` — tail ``.../mission.log`` similarly.

The console-script entry point (see ``pyproject.toml [project.scripts]``)
is ``swarm = "swarmd.cli:cli"``.

Design notes:

* Async commands use ``asyncio.run`` via the ``@async_cmd`` helper. Click
  doesn't support coroutine callbacks natively, and the project doesn't
  depend on ``asyncclick``.

* The launch path delegates to ``_launch_mission(mission)`` (not
  ``_launch_impl``) so the CLI explicitly owns the YAML→dict→Mission
  step. This gives clearer error messages ("mission file not found"
  vs. "validation failed") at the CLI boundary.

* We import ``anthropic`` lazily inside ``health`` so ``swarm --help``
  doesn't pay the anthropic import cost (~150ms).

* Findings/logs find the state dir via the mission's ``workspace``
  (fetched via ``get_status``). The workflow_id IS the session_id in the
  current MissionWorkflow implementation (see ``swarm/durable/workflow.py``
  line 447), so we can also treat workflow_id as session_id directly if
  the status query fails; we try workspace first for correctness.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import click
import yaml
from temporalio.client import Client

from swarmd.durable.errors import TerminalError, TransientError


# --- Constants ---------------------------------------------------------------

# Matches the worker defaults (swarm/durable/worker.py) and the MCP
# server (swarm/mcp/server.py). Keep these in sync if the defaults
# change.
_TEMPORAL_ADDRESS = "localhost:7233"
_TASK_QUEUE = "swarm"

# Haiku model ID for the health-check classifier probe. Matches the
# model pinned in ``swarm/mcp/server.py`` and ``swarm/classifier/llm.py``.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Seconds the Temporal connect probe is allowed. Keeps ``swarm health``
# fast even when Temporal is unreachable.
_TEMPORAL_CONNECT_TIMEOUT_SEC = 2.0

# Default tail size for ``swarm findings`` when --tail is not passed.
_DEFAULT_FINDINGS_TAIL = 50


# --- async/sync bridge -------------------------------------------------------


def async_cmd(func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an async click callback so click can invoke it synchronously.

    Click callbacks must be plain callables. We run the coroutine via
    ``asyncio.run`` which creates a fresh event loop per call — fine for
    a one-shot CLI; a long-running daemon (``swarm worker``) builds its
    own loop inside ``worker.main`` anyway.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper


# --- CLI root ----------------------------------------------------------------


@click.group()
def cli() -> None:
    """swarm — durable multi-agent orchestrator.

    Subcommands talk to a Temporal server at ``localhost:7233``. Start
    one with ``temporal server start-dev`` before using ``launch``/``status``.
    A worker daemon must also be running (``swarm worker``) for missions
    to actually progress.
    """


# --- launch ------------------------------------------------------------------


@cli.command()
@click.argument("mission_yaml", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--workspace",
    type=click.Path(),
    default=None,
    help="Override mission.workspace. Must be an absolute path.",
)
@async_cmd
async def launch(mission_yaml: str, workspace: str | None) -> None:
    """Start a MissionWorkflow from a mission.yaml file.

    Validates the YAML, checks the workspace lock, probes for a worker,
    and starts the workflow on Temporal. Prints ``workflow_id=<id>`` on
    success.
    """
    # Lazy import so the CLI doesn't pay mcp.server's anthropic import
    # cost when the user only runs ``swarm --help``.
    from swarmd.mcp.server import _launch_mission
    from swarmd.schemas.mission import Mission

    # Load and parse the YAML. Errors at this layer are user-facing
    # mistakes (typo'd file, malformed YAML), so we translate to exit 1
    # with a clear message instead of letting pydantic/yaml exceptions
    # bubble up.
    try:
        text = Path(mission_yaml).read_text()
    except OSError as exc:
        click.echo(f"error: cannot read {mission_yaml!r}: {exc}", err=True)
        sys.exit(1)

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        click.echo(f"error: mission YAML is malformed: {exc}", err=True)
        sys.exit(1)

    if not isinstance(data, dict):
        click.echo(
            f"error: mission YAML must be a mapping, got {type(data).__name__}",
            err=True,
        )
        sys.exit(1)

    # Workspace override propagates before validation so the pydantic
    # absolute-path check applies to the effective workspace.
    if workspace is not None:
        data["workspace"] = workspace

    try:
        mission = Mission.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError subclasses broadly
        click.echo(f"error: mission validation failed: {exc}", err=True)
        sys.exit(1)

    # Hand off to the shared launch helper. It does:
    #   1. Client.connect(localhost:7233) → TransientError if unreachable
    #   2. describe_task_queue → {"error": ...} result if pollers == 0
    #   3. Lock {workspace}/.claude/.swarm-lock atomically
    #   4. client.start_workflow("MissionWorkflow", ...)
    try:
        result = await _launch_mission(mission)
    except TransientError as exc:
        # Temporal unreachable is the most common TransientError here.
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except TerminalError as exc:
        # Workspace locked, validation failure downstream, etc.
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        # Unexpected — still fail closed so the caller can debug.
        click.echo(f"error: unexpected launch failure: {exc}", err=True)
        sys.exit(1)

    # No-worker path returns a dict with "error" (not raise) per the MCP
    # contract — the spec wants the user to see actionable text, not a
    # traceback. Match the phrase the CLI-tests assert on.
    if "error" in result:
        msg = result["error"]
        if "no worker" in msg:
            click.echo(
                "error: no worker running. Start one with "
                "`swarm worker &` first",
                err=True,
            )
        else:
            click.echo(f"error: {msg}", err=True)
        sys.exit(1)

    # Happy path — print ``workflow_id=<id>`` so callers can pipe into
    # ``swarm status``/``swarm abort`` trivially.
    click.echo(f"workflow_id={result['workflow_id']}")


# --- status ------------------------------------------------------------------


@cli.command()
@click.argument("workflow_id")
@async_cmd
async def status(workflow_id: str) -> None:
    """Query a running MissionWorkflow for its phase + criteria state."""
    try:
        client = await Client.connect(_TEMPORAL_ADDRESS)
    except Exception as exc:
        click.echo(
            f"error: cannot reach Temporal at {_TEMPORAL_ADDRESS}: {exc}",
            err=True,
        )
        sys.exit(1)

    handle = client.get_workflow_handle(workflow_id)
    try:
        result = await handle.query("get_status")
    except Exception as exc:
        # Same heuristic as mcp.server._is_not_found_error. Not imported
        # directly to keep the CLI independent of MCP internals.
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(
                f"error: workflow {workflow_id!r} not found", err=True
            )
            sys.exit(1)
        click.echo(f"error: status query failed: {exc}", err=True)
        sys.exit(1)

    click.echo(json.dumps(result, indent=2, default=str))


# --- abort -------------------------------------------------------------------


@cli.command()
@click.argument("workflow_id")
@click.option(
    "--reason",
    default="user-abort",
    help="Reason recorded on the workflow's abort_reason field.",
)
@async_cmd
async def abort(workflow_id: str, reason: str) -> None:
    """Send the ``abort`` signal to a running MissionWorkflow."""
    try:
        client = await Client.connect(_TEMPORAL_ADDRESS)
    except Exception as exc:
        click.echo(
            f"error: cannot reach Temporal at {_TEMPORAL_ADDRESS}: {exc}",
            err=True,
        )
        sys.exit(1)

    handle = client.get_workflow_handle(workflow_id)
    try:
        await handle.signal("abort", reason)
    except Exception as exc:
        # Abort on an unknown workflow is a usage error → exit 1 with a
        # clear message rather than letting the Temporal exception leak.
        msg = str(exc).lower()
        if "not found" in msg or "not_found" in msg:
            click.echo(
                f"error: workflow {workflow_id!r} not found", err=True
            )
            sys.exit(1)
        click.echo(f"error: abort signal failed: {exc}", err=True)
        sys.exit(1)

    click.echo(f"abort signal sent to {workflow_id}")


# --- worker ------------------------------------------------------------------


@cli.command()
def worker() -> None:
    """Run the Temporal worker daemon (long-running).

    Delegates to ``swarm.durable.worker.main``. Today that's a stub;
    Task 18 will replace it with a real worker that registers all
    workflows and activities.
    """
    # Import inside the callback so (a) the ``swarm --help`` call doesn't
    # pay the import cost and (b) tests can patch
    # ``swarm.durable.worker.main`` before this runs.
    from swarmd.durable import worker as worker_mod

    asyncio.run(worker_mod.main())


# --- health ------------------------------------------------------------------


@cli.command()
@async_cmd
async def health() -> None:
    """Print a three-row readiness table.

    Rows:
        1. Temporal reachable — ``Client.connect(localhost:7233)``
        2. Worker liveness — ``describe_task_queue("swarm")``
        3. Classifier API — 1-token Haiku ping via anthropic

    Each row is tried independently (a Temporal outage doesn't skip the
    classifier check) so operators can see the full picture in one run.
    """
    rows: list[tuple[str, str, str]] = []

    # Row 1: Temporal reachable. ``asyncio.wait_for`` caps the probe so a
    # stalled TCP connect doesn't hang the whole command.
    temporal_client: Any | None = None
    try:
        temporal_client = await asyncio.wait_for(
            Client.connect(_TEMPORAL_ADDRESS),
            timeout=_TEMPORAL_CONNECT_TIMEOUT_SEC,
        )
        rows.append(("Temporal reachable", "PASS", f"{_TEMPORAL_ADDRESS} reachable"))
    except Exception as exc:
        rows.append(("Temporal reachable", "FAIL", str(exc) or type(exc).__name__))

    # Row 2: Worker liveness. Requires row 1 to have connected — if not,
    # we skip the describe call and report a dependent FAIL so the user
    # sees the worker row didn't get a chance to pass.
    if temporal_client is None:
        rows.append(("Worker liveness", "FAIL", "temporal unreachable"))
    else:
        try:
            resp = await temporal_client.describe_task_queue(_TASK_QUEUE)
            pollers = getattr(resp, "pollers", None) or []
            try:
                n = len(pollers)
            except TypeError:
                n = int(pollers) if isinstance(pollers, int) else 0
            if n > 0:
                rows.append(("Worker liveness", "PASS", f"{n} pollers"))
            else:
                rows.append(
                    (
                        "Worker liveness",
                        "FAIL",
                        f"no pollers on {_TASK_QUEUE!r} task queue",
                    )
                )
        except Exception as exc:
            rows.append(("Worker liveness", "FAIL", str(exc) or type(exc).__name__))

    # Row 3: Classifier API. Tiny Haiku call to exercise the auth path.
    # We keep it at ``max_tokens=1`` to minimise both billed tokens and
    # the chance the model yaks something long. A 2xx response (i.e. no
    # exception) is the only signal we need — we don't inspect content.
    try:
        from anthropic import Anthropic

        def _ping_sync() -> None:
            client = Anthropic()
            client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )

        await asyncio.to_thread(_ping_sync)
        rows.append(("Classifier API", "PASS", "haiku auth OK"))
    except Exception as exc:
        rows.append(("Classifier API", "FAIL", str(exc) or type(exc).__name__))

    # Render as a simple table. We pad the first column to the longest
    # row name so the PASS/FAIL column lines up cleanly. Keeping the
    # rendering dead-simple — no external table library — makes the
    # output grep-able and avoids a dep for one command.
    name_width = max(len(r[0]) for r in rows)
    for name, verdict, detail in rows:
        click.echo(f"{name.ljust(name_width)}  {verdict:4}  {detail}")


# --- findings ----------------------------------------------------------------


@cli.command()
@click.argument("workflow_id")
@click.option(
    "--tail",
    type=int,
    default=_DEFAULT_FINDINGS_TAIL,
    help="Show the last N entries (default 50).",
)
@click.option(
    "--type",
    "type_filter",
    default=None,
    help="Filter by finding type (e.g. 'pattern', 'anticheat').",
)
@async_cmd
async def findings(workflow_id: str, tail: int, type_filter: str | None) -> None:
    """Tail the findings.jsonl disk mirror for a workflow.

    Looks up ``{workspace}/.swarm/state/findings.jsonl`` via the
    workflow's status query. If the workflow isn't reachable we fall
    back to ``~/.swarm/state/<workflow_id>/findings.jsonl`` (the
    canonical path the spec documents).
    """
    path = await _resolve_state_file(workflow_id, "findings.jsonl")
    if path is None or not path.exists():
        click.echo("no findings yet")
        return

    entries = _read_jsonl_tail(path, tail, type_filter)
    for entry in entries:
        ts = entry.get("ts", entry.get("timestamp", ""))
        ftype = entry.get("type", "?")
        verdict = entry.get("verdict", "")
        rationale = entry.get("rationale", "")
        click.echo(f"[{ts}] {ftype} {verdict}: {rationale}")


# --- logs --------------------------------------------------------------------


@cli.command()
@click.argument("workflow_id")
@click.option(
    "--tail",
    type=int,
    default=100,
    help="Show the last N lines (default 100).",
)
@async_cmd
async def logs(workflow_id: str, tail: int) -> None:
    """Tail the mission's stdout log."""
    path = await _resolve_state_file(workflow_id, "mission.log")
    if path is None or not path.exists():
        click.echo("no logs yet")
        return

    # Read whole file then tail N lines. mission.log is bounded by the
    # life of a mission (hours, not days) and our N is small (100 by
    # default), so the memory cost is negligible.
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        click.echo(f"error: cannot read {path}: {exc}", err=True)
        sys.exit(1)

    for line in lines[-tail:]:
        click.echo(line)


# --- helpers -----------------------------------------------------------------


async def _resolve_state_file(workflow_id: str, filename: str) -> Path | None:
    """Find the state file for ``workflow_id``.

    Strategy:
    1. Query the workflow for its status. If the status carries a
       ``workspace`` field (it will once Task 14/15 plumb it through),
       use ``{workspace}/.swarm/state/<filename>``.
    2. Fall back to ``~/.swarm/state/<workflow_id>/<filename>`` — the
       canonical path the spec documents for session state.

    Returns the candidate Path (may not exist — caller checks). Returns
    ``None`` only if we can't construct a plausible path at all.
    """
    # Try the workflow-status-driven path first.
    try:
        client = await Client.connect(_TEMPORAL_ADDRESS)
        handle = client.get_workflow_handle(workflow_id)
        status_result = await handle.query("get_status")
        # MissionWorkflow.get_status returns a dict-like serialization of
        # MissionState. Workspace isn't currently on that dict, so this
        # branch is a forward-looking hook — it lights up automatically
        # once MissionState carries the workspace.
        if isinstance(status_result, dict):
            workspace = status_result.get("workspace")
            if workspace:
                candidate = Path(workspace) / ".swarm" / "state" / filename
                if candidate.exists():
                    return candidate
    except Exception:
        # Temporal unreachable, workflow not found, etc. — silently fall
        # back to the home-dir path. The caller distinguishes missing
        # file from missing session by checking .exists() on the return.
        pass

    # Fall back to ~/.swarm/state/<workflow_id>/.
    home_path = Path.home() / ".swarm" / "state" / workflow_id / filename
    return home_path


def _read_jsonl_tail(
    path: Path, tail: int, type_filter: str | None
) -> list[dict]:
    """Read the last ``tail`` JSONL entries (optionally filtered by type).

    Robust to partial/malformed lines — skips them silently so a
    corrupt last line doesn't blank the whole output. Filters AFTER
    tailing so --tail N --type T behaves as "last N of type T" which
    is the intuitive meaning for a user paging through findings.
    """
    try:
        raw_lines = path.read_text().splitlines()
    except OSError:
        return []

    entries: list[dict] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if type_filter is not None and obj.get("type") != type_filter:
            continue
        entries.append(obj)

    return entries[-tail:]


# --- Entry point -------------------------------------------------------------


def main() -> None:  # pragma: no cover — click handles the real entry
    """Console-script entry point — used by ``swarm = "swarmd.cli:cli"``.

    Kept as a thin delegate so the bare module is runnable via
    ``python -m swarm.cli`` for debug.
    """
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()


# Stable public surface so ``from swarmd.cli import cli`` works without
# scouring for the group. Tests rely on this.
__all__ = [
    "cli",
    "launch",
    "status",
    "abort",
    "worker",
    "health",
    "findings",
    "logs",
    "main",
    "async_cmd",
]
