"""Swarm MCP server — exposes ``propose_criteria``, ``launch``, ``query``.

Per spec §9 (MCP handoff) and plan Task 23. The MCP server is the second
way users trigger a mission (the first is the UserPromptSubmit hook in
the classifier cascade). A host like Claude Code connects over stdio and
invokes one of three tools:

1. ``swarm.propose_criteria(prompt, context)`` — Haiku drafts a mission
   spec as JSON; the tool converts to YAML for the user to inspect.
   Low-risk: produces text only, no state mutation.

2. ``swarm.launch(mission_yaml, workspace)`` — Validates the mission,
   acquires a workspace lock, and starts ``MissionWorkflow`` on the
   local Temporal server. High-risk: mutates filesystem + spawns work.

3. ``swarm.query(workflow_id)`` — Sends ``get_status`` to a running
   mission workflow. Pure read.

Transport: stdio (``FastMCP.run()``). The same server is invoked by the
future ``swarm mcp`` subcommand and by ``claude`` via its MCP config.

Error model: tools raise on terminal conditions. Transient errors become
``TransientError`` (Temporal-style retryable) so the MCP client host can
choose to surface them as retryable vs. fatal.

The launch path is factored into ``_launch_mission`` so the future CLI
(Task 19 / ``swarm launch``) can reuse it without re-implementing the
pre-checks (mission validation, lock acquisition, task-queue probe).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import yaml
from anthropic import Anthropic, APIStatusError
from mcp.server.fastmcp import FastMCP
from temporalio.client import Client

from swarmd.durable.errors import (
    TerminalError,
    TransientError,
    classify_http_status,
)
from swarmd.mcp.prompts import PROPOSE_CRITERIA_PROMPT
from swarmd.schemas.mission import Mission


# --- Constants ---------------------------------------------------------------

# Haiku is the drafting model for propose_criteria — small, fast, cheap.
# Pin the exact ID so behavior is stable across SDK upgrades (matches
# progress_audit.py and classifier/llm.py).
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Upper bound on Haiku response tokens for the mission draft. Mission JSON
# is usually 500-1500 tokens; 2048 is generous headroom for malformed
# outputs that cascade into prose before we reject them.
_MAX_TOKENS = 2048

# Asyncio timeout around the Haiku call. Drafting a mission is one-shot
# and interactive; the user is waiting. 30s is a reasonable p99 for Haiku.
_PROPOSE_TIMEOUT_SEC = 30.0

# Temporal target address. Matches the worker config (Task 18).
_TEMPORAL_ADDRESS = "localhost:7233"

# Task queue the worker polls. Matches spec §6.2 and Task 18.
_TASK_QUEUE = "swarm"

# Filename under ``{workspace}/.claude/`` used as a cooperative lock so
# two missions can't step on each other's workspace simultaneously. The
# ``.claude/`` dir is reused because the mission agent itself uses it for
# session state; if ``.claude/`` doesn't exist we create it.
_LOCK_FILENAME = ".swarm-lock"


# --- propose_criteria --------------------------------------------------------


async def _propose_criteria_impl(
    prompt: str, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Invoke Haiku to draft a mission.yaml and return the result dict.

    The LLM contract (enforced in ``prompts.PROPOSE_CRITERIA_PROMPT``) is
    a single-line JSON object with keys ``mission``, ``workspace``,
    ``success_criteria``, ``verification``, ``invariants`` (optional),
    ``summary``, ``warnings`` (optional).

    We separate the LLM-facing JSON schema from Mission.model_validate so
    Haiku never has to learn our pydantic layout. Post-processing:

    - ``summary`` and ``warnings`` are stripped from the dict before YAML
      serialization (they're metadata for the user, not mission fields).
    - ``criteria_preview`` pulls just id/description/check so the host can
      show a compact list.

    Raises:
        AuthError: 401/403 from anthropic.
        TransientError: 429/424/5xx/timeout.
        TerminalError: Bad JSON, missing keys, invalid types.
    """
    context_str = _format_context(context)
    prompt_text = PROPOSE_CRITERIA_PROMPT.format(
        user_prompt=prompt, context=context_str
    )

    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_invoke_haiku_sync, prompt_text),
            timeout=_PROPOSE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError as exc:
        raise TransientError(
            f"propose_criteria: Haiku timed out after {_PROPOSE_TIMEOUT_SEC}s"
        ) from exc

    data = _parse_propose_response(raw)

    # Separate metadata from mission fields before YAML serialization.
    summary = str(data.pop("summary", ""))[:1000] or "(no summary provided)"
    raw_warnings = data.pop("warnings", []) or []
    if not isinstance(raw_warnings, list):
        raw_warnings = [str(raw_warnings)]
    warnings = [str(w)[:500] for w in raw_warnings][:20]

    # Build the preview before yaml dump — easier to extract from the dict
    # than from the serialized YAML.
    criteria_preview = _extract_criteria_preview(
        data.get("success_criteria") or []
    )

    # Serialize to YAML. ``safe_dump`` guarantees no Python-specific tags.
    # ``sort_keys=False`` preserves the schema order we want the user to
    # read (mission → workspace → criteria → …).
    mission_yaml = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)

    return {
        "mission_yaml": mission_yaml,
        "summary": summary,
        "criteria_preview": criteria_preview,
        "warnings": warnings,
    }


def _invoke_haiku_sync(prompt_text: str) -> str:
    """Blocking Haiku call — must run on a worker thread via ``to_thread``.

    Translates anthropic ``APIStatusError`` into the swarm's classified
    taxonomy via ``classify_http_status`` (401 → AuthError, 429/424/5xx
    → TransientError, etc.). Mirrors the pattern in
    ``swarm/classifier/llm.py::_invoke_haiku_sync``.
    """
    client = Anthropic()
    try:
        response = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt_text}],
        )
    except APIStatusError as exc:
        status = exc.response.status_code
        body = (exc.response.content or b"")[:200]
        classify_http_status(status, body)
        # classify_http_status always raises for non-2xx; this raise keeps
        # the type checker happy.
        raise
    return _extract_text(response)


def _extract_text(response: Any) -> str:
    """Pull the plain-text body out of an anthropic Message.

    Defensive against an empty content list (unusual but possible on
    abnormal completions) — returns empty string so ``_parse_propose_response``
    raises a consistent TerminalError rather than IndexError.
    """
    try:
        block = response.content[0]
    except (IndexError, AttributeError):
        return ""
    return getattr(block, "text", "") or ""


def _format_context(context: dict[str, Any] | None) -> str:
    """Render the optional context dict as ``key: value`` lines for the prompt.

    None / empty → ``(none)``. Non-dict or un-iterable → ``(unparseable)``
    rather than crashing.
    """
    if not context:
        return "(none)"
    try:
        lines = [f"{k}: {v}" for k, v in context.items()]
    except Exception:
        return "(unparseable context)"
    return "\n".join(lines) if lines else "(none)"


def _strip_fence(text: str) -> str:
    """Remove a leading ```json or ``` fence and trailing ``` from ``text``.

    Haiku sometimes wraps JSON in a fence despite instructions; strip at
    most one. Ported verbatim from the classifier.
    """
    text = text.strip()
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence):].strip()
            break
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def _parse_propose_response(raw: str) -> dict[str, Any]:
    """Parse Haiku's JSON draft into a Python dict. Raises TerminalError."""
    if not raw or not raw.strip():
        raise TerminalError("propose_criteria: empty model output")

    text = _strip_fence(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TerminalError(
            f"propose_criteria: unparseable model output: {text[:200]!r}"
        ) from exc

    if not isinstance(data, dict):
        raise TerminalError(
            f"propose_criteria: expected JSON object, got {type(data).__name__}"
        )

    # Minimal shape check. We deliberately do NOT run Mission.model_validate
    # here — the draft often has ``/ABSOLUTE/PATH/TO/WORKSPACE`` placeholder
    # workspace values that would fail the absolute-path validator. The user
    # is expected to review and edit the YAML before calling ``launch``.
    for required in ("mission", "workspace", "success_criteria"):
        if required not in data:
            raise TerminalError(
                f"propose_criteria: missing required key {required!r}"
            )

    criteria = data.get("success_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise TerminalError(
            "propose_criteria: success_criteria must be a non-empty list"
        )

    return data


def _extract_criteria_preview(
    criteria: list[Any],
) -> list[dict[str, str]]:
    """Pull just id/description/check from each criterion for a compact preview.

    Defensive: items that aren't dicts are skipped; missing keys are
    replaced with placeholders. We never let a bad criterion shape crash
    the whole tool — the host is about to show this to a human who can
    spot-check.
    """
    out: list[dict[str, str]] = []
    for item in criteria:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "id": str(item.get("id", "(missing)")),
                "description": str(item.get("description", ""))[:200],
                "check": str(item.get("check", ""))[:200],
            }
        )
    return out


# --- launch ------------------------------------------------------------------


async def _launch_impl(
    mission_yaml: str, workspace: str | None = None
) -> dict[str, Any]:
    """Validate, lock, and start a ``MissionWorkflow`` on Temporal.

    Shared entry point used by the MCP tool and (future) the ``swarm
    launch`` CLI. Behavior matches the spec:

    1. Load the YAML (from string OR path — auto-detect).
    2. Optionally override ``workspace``.
    3. Validate via ``Mission.model_validate`` → TerminalError on failure.
    4. Connect to Temporal → TransientError on network failure.
    5. Probe the task queue for pollers → error result (not raise) if 0.
    6. Attempt to acquire the workspace lock → TerminalError if held.
    7. Start ``MissionWorkflow`` with a fresh uuid4 workflow_id.
    """
    # --- 1. Load mission source --------------------------------------------
    mission_dict = _load_mission_source(mission_yaml)

    # --- 2. Workspace override ---------------------------------------------
    if workspace is not None:
        mission_dict["workspace"] = workspace

    # --- 3. Validate -------------------------------------------------------
    try:
        mission = Mission.model_validate(mission_dict)
    except Exception as exc:  # pydantic.ValidationError is a broad surface
        raise TerminalError(
            f"launch: mission validation failed: {exc}"
        ) from exc

    return await _launch_mission(mission)


async def _launch_mission(mission: Mission) -> dict[str, Any]:
    """Private helper — the post-validation launch sequence.

    Factored out so Task 19 (``swarm launch`` CLI) can reuse the
    Temporal-facing logic without re-implementing YAML loading. Accepts
    an already-validated ``Mission``; generates a workflow_id if the
    mission doesn't carry one.
    """
    workflow_id = f"mission-{uuid.uuid4().hex[:16]}"

    # --- 4. Temporal connect -----------------------------------------------
    try:
        client = await Client.connect(_TEMPORAL_ADDRESS)
    except Exception as exc:
        raise TransientError(
            f"launch: cannot reach Temporal at {_TEMPORAL_ADDRESS}: {exc}"
        ) from exc

    # --- 5. Worker probe ---------------------------------------------------
    pollers = await _count_pollers(client, _TASK_QUEUE)
    if pollers == 0:
        return {
            "error": (
                f"no worker running on task queue {_TASK_QUEUE!r} — "
                "start one with `swarm worker &` first"
            ),
            "task_queue": _TASK_QUEUE,
            "pollers": 0,
        }

    # --- 6. Workspace lock -------------------------------------------------
    lock_path = _acquire_workspace_lock(mission.workspace, workflow_id)

    # --- 7. Start the workflow ---------------------------------------------
    # The workflow is referenced by string name, not Python class, so we
    # don't have to import MissionWorkflow (which would pull in the
    # deterministic-sandbox imports module-level). Args match the
    # workflow signature: (mission, carry=None).
    try:
        await client.start_workflow(
            "MissionWorkflow",
            args=[mission.model_dump(mode="json"), None],
            id=workflow_id,
            task_queue=_TASK_QUEUE,
        )
    except Exception:
        # Roll back the lock so a failed start doesn't leave the workspace
        # wedged. The lock file only matters for the happy path; if start
        # fails the user should be able to retry.
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise

    return {
        "workflow_id": workflow_id,
        "task_queue": _TASK_QUEUE,
    }


def _load_mission_source(source: str) -> dict[str, Any]:
    """Parse ``source`` into a dict.

    ``source`` is either YAML text OR a path to a YAML file. We treat
    the arg as a path iff ``os.path.isfile(source)`` returns True — the
    ambiguity is resolved by the filesystem so callers don't have to
    pre-dispatch. An unresolvable path that looks like a file (e.g.
    ``./mission.yaml`` that doesn't exist) falls through to the YAML
    parser, which will then raise a parse error the caller can see.
    """
    # Path only if it's actually a file on disk. A YAML string like
    # "mission: foo\nworkspace: /tmp\n..." would never be a real file
    # because those names contain characters that aren't path-safe.
    try:
        is_path = os.path.isfile(source)
    except (OSError, ValueError):
        is_path = False

    if is_path:
        try:
            text = Path(source).read_text()
        except OSError as exc:
            raise TerminalError(
                f"launch: cannot read mission file {source!r}: {exc}"
            ) from exc
    else:
        text = source

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TerminalError(
            f"launch: mission YAML is malformed: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise TerminalError(
            f"launch: mission YAML must be a mapping, got "
            f"{type(parsed).__name__}"
        )
    return parsed


async def _count_pollers(client: Any, task_queue: str) -> int:
    """Return the number of pollers on ``task_queue``.

    Returns 0 on any describe error rather than raising — a failed
    describe should surface as "no worker" to the user, not a mysterious
    exception. If the Temporal client happens to not expose
    ``describe_task_queue`` (older SDKs) we conservatively return a
    positive count so launch isn't blocked by an SDK mismatch.
    """
    describe = getattr(client, "describe_task_queue", None)
    if describe is None:
        # SDK doesn't expose the probe; assume worker is healthy. The
        # mission will simply not progress if no poller shows up, but
        # that's a visible failure mode at runtime.
        return 1

    try:
        resp = await describe(task_queue)
    except Exception:
        return 0

    # The Temporal Python SDK returns a DescribeTaskQueueResponse whose
    # ``pollers`` attribute is a list. In tests we accept either a list
    # or an int for simplicity.
    pollers = getattr(resp, "pollers", None)
    if pollers is None and isinstance(resp, dict):
        pollers = resp.get("pollers")
    if pollers is None:
        return 0
    if isinstance(pollers, int):
        return pollers
    try:
        return len(pollers)
    except TypeError:
        return 0


def _acquire_workspace_lock(workspace: str, workflow_id: str) -> Path:
    """Create a lock file rooted under ``{workspace}/.claude/``.

    Uses ``open(..., "x")`` for atomicity — if the file exists we raise
    TerminalError with the holder's workflow_id. The ``.claude`` parent
    dir is created on demand so a fresh workspace doesn't require the
    user to pre-create it.
    """
    ws = Path(workspace)
    lock_dir = ws / ".claude"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / _LOCK_FILENAME

    try:
        with open(lock_path, "x") as fh:
            fh.write(workflow_id)
    except FileExistsError as exc:
        try:
            holder = lock_path.read_text().strip()
        except OSError:
            holder = "(unknown)"
        raise TerminalError(
            f"launch: workspace {workspace!r} is locked by another mission "
            f"(holder={holder!r}); remove {lock_path} to force"
        ) from exc
    return lock_path


# --- query -------------------------------------------------------------------


async def _query_impl(workflow_id: str) -> dict[str, Any]:
    """Query a running MissionWorkflow for its current status.

    Dispatches the ``get_status`` query registered on ``MissionWorkflow``.
    Translates Temporal connection errors into ``TransientError`` and
    workflow-not-found into ``TerminalError`` so the MCP host can
    differentiate retry-vs-fatal.
    """
    try:
        client = await Client.connect(_TEMPORAL_ADDRESS)
    except Exception as exc:
        raise TransientError(
            f"query: cannot reach Temporal at {_TEMPORAL_ADDRESS}: {exc}"
        ) from exc

    handle = client.get_workflow_handle(workflow_id)

    try:
        status = await handle.query("get_status")
    except Exception as exc:
        # The Temporal SDK raises RPCError (or subclasses) for workflow
        # lookup failures. We don't import the specific type — matching
        # on message keeps us SDK-version-agnostic. The
        # ``_is_not_found_error`` helper isolates that heuristic so tests
        # can override it without patching internals.
        if _is_not_found_error(exc):
            raise TerminalError(
                f"query: workflow {workflow_id!r} not found"
            ) from exc
        # Any other failure is treated as transient (network blip, worker
        # not yet responding, etc.) so the MCP host can retry.
        raise TransientError(
            f"query: workflow {workflow_id!r} query failed: {exc}"
        ) from exc

    return {"status": status}


def _is_not_found_error(exc: BaseException) -> bool:
    """Heuristic: does ``exc`` represent a workflow-not-found condition?

    The Temporal Python SDK raises ``temporalio.service.RPCError`` with a
    status code (NOT_FOUND = 5) or NotFound subclasses depending on the
    version. We look for the word "not found" or "NOT_FOUND" in either
    the message or a ``status`` attribute. If neither signal is present
    the caller will treat it as transient.
    """
    msg = str(exc).lower()
    if "not found" in msg or "not_found" in msg:
        return True
    status = getattr(exc, "status", None)
    if status is not None and "not_found" in str(status).lower():
        return True
    return False


# --- FastMCP wiring ----------------------------------------------------------

# The FastMCP instance is module-level so tests can import and inspect the
# tool registry if needed. Tools are thin async wrappers around the
# ``_*_impl`` functions — keep the impls pure so tests don't have to
# round-trip through the MCP transport.
app = FastMCP("swarm")


@app.tool()
async def propose_criteria(
    prompt: str, context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Derive a mission.yaml proposal from a user prompt.

    Args:
        prompt: Natural-language description of the mission goal.
        context: Optional metadata (cwd, git branch, recent files, ...).

    Returns:
        {
            "mission_yaml": <YAML text ready to save>,
            "summary": <2-3 sentence plan>,
            "criteria_preview": [{id, description, check}, ...],
            "warnings": [<caveats>]
        }
    """
    return await _propose_criteria_impl(prompt, context)


@app.tool()
async def launch(
    mission_yaml: str, workspace: str | None = None
) -> dict[str, Any]:
    """Start a MissionWorkflow on Temporal.

    Args:
        mission_yaml: YAML string OR a filesystem path to a YAML file.
        workspace: Optional override of ``mission.workspace``.

    Returns:
        {"workflow_id": "<id>", "task_queue": "swarm"} on success,
        or {"error": "<msg>", "task_queue": ..., "pollers": ...} if no
        worker is available.
    """
    return await _launch_impl(mission_yaml, workspace)


@app.tool()
async def query(workflow_id: str) -> dict[str, Any]:
    """Query a running mission workflow.

    Args:
        workflow_id: The ID returned by ``launch``.

    Returns:
        {"status": <dict from MissionWorkflow.get_status()>}
    """
    return await _query_impl(workflow_id)


def main() -> None:
    """Module entry point. Runs the server on stdio.

    Use ``python -m swarm.mcp.server`` or register this as a console
    script to invoke.
    """
    app.run()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "app",
    "propose_criteria",
    "launch",
    "query",
    "main",
    "_propose_criteria_impl",
    "_launch_impl",
    "_launch_mission",
    "_query_impl",
]
