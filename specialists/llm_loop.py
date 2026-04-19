"""llm_loop — wraps the LLM specialists in a daemon loop.

Runs goal_drift_critic + progress_auditor on a configurable cadence.
Reads the Claude Code transcript, builds inputs, calls the LLM, parses
verdicts, emits findings. This is the module that makes the LLM critics
actually run — prior to this, goal_drift_critic.judge() existed but no
process called it.

The LLM runner is dependency-injected so tests don't need a real LLM.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from swarmd.lib.heartbeat import beat
from swarmd.lib.launcher_liveness import exit_if_launcher_dead
from swarmd.lib.locking import write_line
from swarmd.lib.paths import (
    claude_transcript_path,
    ensure_session_dirs,
    findings_path,
    mission_yaml_path,
)
from swarmd.schemas.finding import Finding
from swarmd.schemas.mission import Mission
from swarmd.specialists.goal_drift_critic import judge as drift_judge
from swarmd.specialists.progress_auditor import audit as progress_audit

LOG = logging.getLogger("swarm.llm_loop")

LLMRunner = Callable[[str], str]


@dataclass
class CycleResult:
    drift_findings: list[Finding]
    progress_findings: list[Finding]


def one_cycle(
    session_id: str,
    mission: Mission,
    *,
    transcript_path: Path | None = None,
    drift_llm: LLMRunner | None = None,
    progress_llm: LLMRunner | None = None,
) -> CycleResult:
    """Run the LLM specialists once. Pure function; tests inject runners."""
    t_path = transcript_path or claude_transcript_path(session_id, mission.workspace)

    drift_kwargs: dict = {
        "session_id": session_id,
        "spawner_id": session_id,
        "mission": mission.mission,
        "transcript_path": t_path,
    }
    if drift_llm is not None:
        drift_kwargs["llm"] = drift_llm
    drift_findings = drift_judge(**drift_kwargs)

    progress_kwargs: dict = {
        "session_id": session_id,
        "spawner_id": session_id,
        "transcript_path": t_path,
    }
    if progress_llm is not None:
        progress_kwargs["llm"] = progress_llm
    progress_findings = progress_audit(**progress_kwargs)

    return CycleResult(
        drift_findings=drift_findings,
        progress_findings=progress_findings,
    )


def _load_mission(session_id: str) -> Mission:
    return Mission.model_validate(
        yaml.safe_load(mission_yaml_path(session_id).read_text())
    )


def main(session_id: str) -> None:
    """Daemon loop. Runs each LLM critic on its configured cadence."""
    ensure_session_dirs(session_id)
    exit_if_launcher_dead(session_id, LOG)
    mission = _load_mission(session_id)
    cfg = mission.observer_config
    cycles = 0
    last_drift = 0.0
    last_progress = 0.0
    LOG.info(
        "llm_loop starting session=%s drift_cadence=%ss progress_cadence=%ss",
        session_id,
        cfg.goal_drift_cadence_sec,
        cfg.progress_audit_cadence_sec,
    )
    while True:
        exit_if_launcher_dead(session_id, LOG)
        now = time.time()
        transcript = claude_transcript_path(session_id, mission.workspace)

        # Drift critic
        if now - last_drift >= cfg.goal_drift_cadence_sec:
            try:
                for f in drift_judge(
                    session_id=session_id,
                    spawner_id=session_id,
                    mission=mission.mission,
                    transcript_path=transcript,
                ):
                    write_line(findings_path(session_id), f.model_dump_json())
                    LOG.info("drift finding: %s", f.subtype)
                last_drift = now
            except Exception as e:
                LOG.error("drift_judge failed: %s", e)

        # Progress auditor
        if now - last_progress >= cfg.progress_audit_cadence_sec:
            try:
                for f in progress_audit(
                    session_id=session_id,
                    spawner_id=session_id,
                    transcript_path=transcript,
                ):
                    write_line(findings_path(session_id), f.model_dump_json())
                    LOG.info("progress finding: %s", f.subtype)
                last_progress = now
            except Exception as e:
                LOG.error("progress_audit failed: %s", e)

        cycles += 1
        beat(session_id, "llm_loop", cycles)

        # Sleep to the next critic's deadline
        drift_due_in = max(
            0.0, last_drift + cfg.goal_drift_cadence_sec - time.time()
        )
        prog_due_in = max(
            0.0, last_progress + cfg.progress_audit_cadence_sec - time.time()
        )
        time.sleep(min(drift_due_in, prog_due_in, 10.0) or 1.0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: llm_loop.py <session_id>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
