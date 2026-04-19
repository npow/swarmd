"""recovery_spawn — rotate to a fresh claude subprocess after strike exhaustion.

When the escape ladder fails and a signature hits strike-3+, the coordinator
calls spawn_recovery to:
  1. Write a recovery briefing file (postmortem + do-not-repeat list)
  2. Kill the current worker's Claude Code session (via SESSION_ID lookup)
  3. Spawn a fresh `claude` subprocess with the same session_id + mission,
     so the swarm's state carries over but the agent's context resets

v1 scope: the spawn returns a process handle (detached). The caller is
responsible for not awaiting it (the new worker runs for the lifetime of
the mission). For tests, the subprocess call is dependency-injected.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from swarmd.lib.paths import mission_yaml_path, session_dir

LOG = logging.getLogger("swarm.recovery_spawn")

Spawner = Callable[[list[str], dict[str, str]], subprocess.Popen]


def default_spawner(argv: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@dataclass(frozen=True)
class RecoveryResult:
    spawned: bool
    pid: int | None
    briefing_path: Path | None
    error: str | None = None


def write_briefing(
    session_id: str,
    *,
    reason: str,
    failed_signatures: list[str],
    tried_strategies: list[str],
    last_n_findings: list[str],
) -> Path:
    """Write the recovery briefing the new worker reads first."""
    path = session_dir(session_id) / "recovery-briefing.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _format_briefing(
        session_id=session_id,
        reason=reason,
        failed_signatures=failed_signatures,
        tried_strategies=tried_strategies,
        last_n_findings=last_n_findings,
    )
    path.write_text(body)
    return path


def _format_briefing(
    *,
    session_id: str,
    reason: str,
    failed_signatures: list[str],
    tried_strategies: list[str],
    last_n_findings: list[str],
) -> str:
    lines = [
        f"# Recovery Briefing — session {session_id}",
        f"# Written: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "",
        "## Why you were spawned",
        reason,
        "",
        "## Do NOT repeat",
        "The previous worker exhausted its intervention ladder on these",
        "signatures. Approaches that targeted these signatures have already",
        "failed; pick a different strategy.",
        "",
    ]
    for sig in failed_signatures:
        lines.append(f"  - {sig}")
    lines.extend(
        [
            "",
            "## Strategies already tried (no need to retry)",
        ]
    )
    for s in tried_strategies:
        lines.append(f"  - {s}")
    lines.extend(
        [
            "",
            "## Recent findings (most recent last)",
        ]
    )
    for f in last_n_findings[-20:]:
        lines.append(f"  - {f}")
    lines.extend(
        [
            "",
            "## Your job",
            "Read `~/.swarm/missions/<session>/mission.yaml` to re-confirm",
            "the mission. The success_criteria are the contract. Pick a",
            "fundamentally different approach than those listed above.",
        ]
    )
    return "\n".join(lines)


def spawn_recovery(
    session_id: str,
    *,
    reason: str = "strike exhaustion",
    failed_signatures: list[str] | None = None,
    tried_strategies: list[str] | None = None,
    last_n_findings: list[str] | None = None,
    claude_binary: str = "claude",
    spawner: Spawner = default_spawner,
) -> RecoveryResult:
    """Write briefing and launch a new claude subprocess for this session."""
    mission_path = mission_yaml_path(session_id)
    if not mission_path.exists():
        return RecoveryResult(
            spawned=False,
            pid=None,
            briefing_path=None,
            error=f"mission.yaml not found at {mission_path}",
        )
    briefing = write_briefing(
        session_id,
        reason=reason,
        failed_signatures=failed_signatures or [],
        tried_strategies=tried_strategies or [],
        last_n_findings=last_n_findings or [],
    )
    # Read mission prose
    try:
        import yaml

        with mission_path.open() as f:
            data = yaml.safe_load(f) or {}
        mission_prose = str(data.get("mission", "(no mission prose)"))
    except Exception as e:
        return RecoveryResult(
            spawned=False,
            pid=None,
            briefing_path=briefing,
            error=f"failed to read mission: {e}",
        )

    # Compose the recovery prompt: the mission PLUS a pointer to the briefing
    prompt = (
        f"{mission_prose}\n\n"
        "IMPORTANT: This is a RECOVERY spawn. Read the briefing at:\n"
        f"  {briefing}\n"
        "before taking any action. The previous worker was rotated out; its "
        "approaches are listed as DO-NOT-REPEAT. Choose a different direction."
    )

    argv = [claude_binary, "--session-id", session_id, prompt]
    env = os.environ.copy()
    # Preserve SESSION_ID so hooks find the same state dir
    env["SESSION_ID"] = session_id

    try:
        proc = spawner(argv, env)
    except Exception as e:
        return RecoveryResult(
            spawned=False,
            pid=None,
            briefing_path=briefing,
            error=f"spawn failed: {e}",
        )

    LOG.info("recovery spawned: session=%s pid=%s", session_id, proc.pid)
    # Write a marker so the coordinator knows a recovery is in flight
    marker = session_dir(session_id) / "recovery_in_flight.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {"pid": proc.pid, "spawned_at": time.time(), "reason": reason}
        )
    )
    return RecoveryResult(spawned=True, pid=proc.pid, briefing_path=briefing)
