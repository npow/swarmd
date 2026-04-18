"""supervisor — heartbeat-watches specialists, restarts crashed ones,
escalates on rotation-exhaustion.

v1 implementation: a polling daemon that checks health beats for every
configured specialist. If a beat is stale, respawns the specialist as a
subprocess. If the same cheat-type finding appears across K recovery
rotations, emits a mission_level_alert_pending finding.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

from swarm.lib.heartbeat import beat, is_stale
from swarm.lib.ids import mint_finding_id
from swarm.lib.launcher_liveness import exit_if_launcher_dead
from swarm.lib.locking import write_line
from swarm.lib.paths import (
    ensure_session_dirs,
    findings_path,
    health_beat_path,
)
from swarm.schemas.finding import Finding

LOG = logging.getLogger("swarm.supervisor")

# How many times the same cheat-subtype must re-appear across recovery
# rotations to trigger a mission_level_alert_pending.
ROTATION_EXHAUSTION_K = 3

DEFAULT_SPECIALISTS = ("pattern_detector", "success_verifier", "coordinator")

# Max heartbeat staleness before we consider a specialist dead
STALE_SEC = 60.0


@dataclass(frozen=True)
class HealthStatus:
    specialist: str
    stale: bool
    pid: int | None


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


def check_all(
    session_id: str,
    specialists: tuple[str, ...] = DEFAULT_SPECIALISTS,
    stale_sec: float = STALE_SEC,
) -> list[HealthStatus]:
    """Return a status for each specialist."""
    statuses: list[HealthStatus] = []
    for name in specialists:
        stale = is_stale(session_id, name, max_age_sec=stale_sec)
        pid = _read_beat_pid(session_id, name)
        statuses.append(HealthStatus(specialist=name, stale=stale, pid=pid))
    return statuses


def _read_beat_pid(session_id: str, specialist: str) -> int | None:
    p = health_beat_path(session_id, specialist)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return int(data.get("pid")) if data.get("pid") else None
    except Exception:
        return None


def respawn(
    session_id: str,
    specialist: str,
    *,
    python: str = sys.executable,
    repo_root: str | None = None,
    spawner: Spawner = default_spawner,
) -> int | None:
    """Respawn a specialist daemon. Returns the new PID or None on failure."""
    repo = repo_root or os.environ.get("REPO_ROOT", "/Users/npow/code/research")
    argv = [python, "-m", f"swarm.specialists.{specialist}", session_id]
    env = os.environ.copy()
    env["PYTHONPATH"] = repo
    try:
        proc = spawner(argv, env)
    except Exception as e:
        LOG.error("respawn %s failed: %s", specialist, e)
        return None
    LOG.info("respawned %s: pid=%s", specialist, proc.pid)
    return proc.pid


def count_rotation_cheats(session_id: str) -> dict[str, int]:
    """Count cheat findings by subtype across the session (across rotations)."""
    path = findings_path(session_id)
    counts: dict[str, int] = {}
    if not path.exists():
        return counts
    with path.open() as f:
        for line in f:
            try:
                fobj = Finding.model_validate_json(line)
            except Exception:
                continue
            if fobj.type == "cheat":
                counts[fobj.subtype] = counts.get(fobj.subtype, 0) + 1
    return counts


def check_rotation_exhaustion(session_id: str) -> list[str]:
    """Return cheat-subtypes that have re-appeared K+ times → need user alert."""
    counts = count_rotation_cheats(session_id)
    return [subtype for subtype, n in counts.items() if n >= ROTATION_EXHAUSTION_K]


def emit_mission_level_alert(
    session_id: str, exhausted_subtypes: list[str]
) -> Finding:
    """Emit a mission_level_alert_pending finding. Caller writes it."""
    return Finding(
        id=mint_finding_id(),
        source="supervisor.rotation_exhaustion",
        subject_session=session_id,
        spawner_id=session_id,
        type="meta",
        subtype="mission_level_alert_pending",
        severity="critical",
        verdict=(
            f"Same cheat pattern has recurred {ROTATION_EXHAUSTION_K}+ times "
            f"across recovery rotations: {', '.join(exhausted_subtypes)}. "
            "The mission may be infeasible as specified, or the current "
            "provider/model cannot solve it without cheating. User review required."
        ),
    )


# -------- daemon --------


def main(session_id: str, period_sec: float = 10.0) -> None:
    ensure_session_dirs(session_id)
    exit_if_launcher_dead(session_id, LOG)
    cycles = 0
    already_alerted: set[str] = set()
    LOG.info("supervisor starting for session=%s", session_id)
    while True:
        exit_if_launcher_dead(session_id, LOG)
        statuses = check_all(session_id)
        for s in statuses:
            if s.stale:
                LOG.warning("specialist %s is stale (pid=%s); respawning", s.specialist, s.pid)
                respawn(session_id, s.specialist)
                # Emit meta finding recording the restart
                finding = Finding(
                    id=mint_finding_id(),
                    source=f"supervisor.{s.specialist}",
                    subject_session=session_id,
                    spawner_id=session_id,
                    type="meta",
                    subtype="specialist_degraded",
                    severity="major",
                    verdict=f"{s.specialist} heartbeat stale; restarted",
                )
                write_line(findings_path(session_id), finding.model_dump_json())

        # Rotation exhaustion escalation
        exhausted = check_rotation_exhaustion(session_id)
        new_exhaustion = [s for s in exhausted if s not in already_alerted]
        if new_exhaustion:
            alert = emit_mission_level_alert(session_id, new_exhaustion)
            write_line(findings_path(session_id), alert.model_dump_json())
            already_alerted.update(new_exhaustion)

        cycles += 1
        beat(session_id, "supervisor", cycles)
        time.sleep(period_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: supervisor.py <session_id>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
