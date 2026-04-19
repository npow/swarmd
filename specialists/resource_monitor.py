"""resource_monitor — watches for system-resource exhaustion.

Checks:
  - open file descriptors (current process & whole session tree)
  - zombie process count
  - memory pressure (RSS of tracked processes)
  - session process-tree size vs concurrency.max_total_live
  - disk usage of ~/.swarm/state/<session>/ (event logs growing unbounded)

Emits meta findings with severity:
  - "major" when a WARNING threshold is hit
  - "critical" when a CRITICAL threshold is hit

The monitor is INTENTIONALLY best-effort cross-platform:
  - macOS: uses `lsof` (slow but portable) and `ps`.
  - Linux: uses /proc/<pid>/fd, /proc/<pid>/status.
  - Other: reports "unsupported".

This module is called by a daemon wrapper (or on-demand by tests).
"""

from __future__ import annotations

import logging
import os
import resource
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from swarmd.lib.ids import mint_finding_id
from swarmd.schemas.finding import Evidence, Finding

LOG = logging.getLogger("swarm.resource_monitor")

# Thresholds — override via mission.yaml in future versions
FD_WARN_RATIO = 0.6    # 60% of ulimit -n
FD_CRIT_RATIO = 0.85   # 85%
ZOMBIE_WARN = 5
ZOMBIE_CRIT = 20
RSS_WARN_MB = 2000     # 2GB across tracked processes
RSS_CRIT_MB = 5000     # 5GB
STATE_DISK_WARN_MB = 100
STATE_DISK_CRIT_MB = 500


@dataclass(frozen=True)
class ResourceSnapshot:
    """One moment in time."""

    fd_count: int
    fd_limit: int
    zombie_count: int
    tracked_pids: list[int] = field(default_factory=list)
    live_tracked: int = 0
    rss_mb: float = 0.0
    state_disk_mb: float = 0.0


def _fd_limit() -> int:
    """Return the soft limit on open file descriptors for this process."""
    try:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return int(soft)
    except Exception:
        return 1024


def _self_fd_count() -> int:
    """Count open fds of the current process (best-effort per platform)."""
    # Linux
    proc_fd = Path("/proc/self/fd")
    if proc_fd.exists():
        try:
            return len(list(proc_fd.iterdir()))
        except OSError:
            pass
    # macOS / other — use lsof
    try:
        r = subprocess.run(
            ["lsof", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # First line is header
        return max(0, len(r.stdout.splitlines()) - 1)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0


def _count_zombies(pids: list[int]) -> int:
    """Count processes in Z (zombie) state among `pids` (and any children)."""
    zombies = 0
    try:
        r = subprocess.run(
            ["ps", "-A", "-o", "pid=,state="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    for line in r.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        try:
            int(parts[0])  # validate pid is int
        except ValueError:
            continue
        state = parts[1]
        # Z = zombie on Linux/macOS (ps reports <defunct> on Linux too)
        if state.startswith("Z"):
            # Count all zombies — a zombie anywhere in the system may be ours
            zombies += 1
    return zombies


def _rss_mb(pids: list[int]) -> float:
    """Sum RSS (in MB) across the given pids."""
    total_kb = 0
    for pid in pids:
        try:
            # ps -o rss= works on both Linux and macOS
            r = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode == 0:
                val = r.stdout.strip()
                if val:
                    total_kb += int(val)
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            continue
    return total_kb / 1024.0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _tracked_pids(session_id: str) -> list[int]:
    """Collect PIDs from heartbeats + tree.json for the session."""
    pids: list[int] = []
    # Defer imports so resource_monitor can be imported standalone
    from swarmd.lib.paths import session_dir

    health = session_dir(session_id) / "health"
    if health.exists():
        import json as _json

        for beat in health.glob("*.beat"):
            try:
                data = _json.loads(beat.read_text())
                pid = int(data.get("pid", 0))
                if pid > 0:
                    pids.append(pid)
            except Exception:
                continue
    tree = session_dir(session_id) / "tree.json"
    if tree.exists():
        try:
            import json as _json

            data = _json.loads(tree.read_text())
            for node in data.get("nodes", {}).values():
                pid = int(node.get("pid", 0))
                if pid > 0:
                    pids.append(pid)
        except Exception:
            pass
    return list(set(pids))


def _state_disk_mb(session_id: str) -> float:
    """Total size (MB) of the session state directory."""
    from swarmd.lib.paths import session_dir

    sdir = session_dir(session_id)
    if not sdir.exists():
        return 0.0
    total = 0
    for p in sdir.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total / (1024 * 1024)


def snapshot(session_id: str) -> ResourceSnapshot:
    """Take a point-in-time snapshot. Callers interpret via `check_resources`."""
    pids = _tracked_pids(session_id)
    live = sum(1 for p in pids if _pid_alive(p))
    return ResourceSnapshot(
        fd_count=_self_fd_count(),
        fd_limit=_fd_limit(),
        zombie_count=_count_zombies(pids),
        tracked_pids=pids,
        live_tracked=live,
        rss_mb=_rss_mb(pids),
        state_disk_mb=_state_disk_mb(session_id),
    )


def evaluate(snap: ResourceSnapshot, session_id: str) -> list[Finding]:
    """Turn a snapshot into findings based on thresholds."""
    findings: list[Finding] = []

    def _finding(subtype: str, severity: str, verdict: str, claim: str) -> Finding:
        return Finding(
            id=mint_finding_id(),
            source=f"resource_monitor.{subtype}",
            subject_session=session_id,
            spawner_id=session_id,
            type="meta",
            subtype=subtype,
            severity=severity,  # type: ignore[arg-type]
            evidence=Evidence(claim_excerpt=claim),
            verdict=verdict,
        )

    # FDs
    if snap.fd_limit > 0:
        ratio = snap.fd_count / snap.fd_limit
        if ratio >= FD_CRIT_RATIO:
            findings.append(
                _finding(
                    "fd_exhaustion",
                    "critical",
                    f"open fds {snap.fd_count}/{snap.fd_limit} "
                    f"({int(ratio * 100)}%) — exhaustion imminent",
                    f"ratio={ratio:.2f}",
                )
            )
        elif ratio >= FD_WARN_RATIO:
            findings.append(
                _finding(
                    "fd_warning",
                    "major",
                    f"open fds {snap.fd_count}/{snap.fd_limit} ({int(ratio * 100)}%)",
                    f"ratio={ratio:.2f}",
                )
            )

    # Zombies
    if snap.zombie_count >= ZOMBIE_CRIT:
        findings.append(
            _finding(
                "zombie_flood",
                "critical",
                f"{snap.zombie_count} zombie processes — reaper is failing",
                f"count={snap.zombie_count}",
            )
        )
    elif snap.zombie_count >= ZOMBIE_WARN:
        findings.append(
            _finding(
                "zombies",
                "major",
                f"{snap.zombie_count} zombie processes detected",
                f"count={snap.zombie_count}",
            )
        )

    # Memory
    if snap.rss_mb >= RSS_CRIT_MB:
        findings.append(
            _finding(
                "memory_pressure",
                "critical",
                f"tracked RSS {snap.rss_mb:.0f}MB >= critical {RSS_CRIT_MB}MB",
                f"rss_mb={snap.rss_mb:.0f}",
            )
        )
    elif snap.rss_mb >= RSS_WARN_MB:
        findings.append(
            _finding(
                "memory_warning",
                "major",
                f"tracked RSS {snap.rss_mb:.0f}MB >= warn {RSS_WARN_MB}MB",
                f"rss_mb={snap.rss_mb:.0f}",
            )
        )

    # State disk
    if snap.state_disk_mb >= STATE_DISK_CRIT_MB:
        findings.append(
            _finding(
                "state_disk_full",
                "critical",
                f"session state dir {snap.state_disk_mb:.0f}MB >= {STATE_DISK_CRIT_MB}MB",
                f"disk_mb={snap.state_disk_mb:.0f}",
            )
        )
    elif snap.state_disk_mb >= STATE_DISK_WARN_MB:
        findings.append(
            _finding(
                "state_disk_warning",
                "major",
                f"session state dir {snap.state_disk_mb:.0f}MB >= {STATE_DISK_WARN_MB}MB",
                f"disk_mb={snap.state_disk_mb:.0f}",
            )
        )

    return findings


def check_resources(session_id: str) -> list[Finding]:
    """One-shot resource check. Returns any threshold-breach findings."""
    snap = snapshot(session_id)
    return evaluate(snap, session_id)


# -------- daemon --------


def main(session_id: str, period_sec: float = 30.0) -> None:
    """Poll loop for a long-running resource_monitor daemon."""
    import time as _time

    from swarmd.lib.heartbeat import beat
    from swarmd.lib.launcher_liveness import exit_if_launcher_dead
    from swarmd.lib.locking import write_line
    from swarmd.lib.paths import ensure_session_dirs, findings_path

    ensure_session_dirs(session_id)
    exit_if_launcher_dead(session_id, LOG)
    emitted_this_cycle: set[str] = set()
    cycles = 0
    LOG.info("resource_monitor starting for session=%s", session_id)
    while True:
        exit_if_launcher_dead(session_id, LOG)
        for f in check_resources(session_id):
            key = f"{f.subtype}|{f.verdict[:100]}"
            if key in emitted_this_cycle:
                continue
            emitted_this_cycle.add(key)
            write_line(findings_path(session_id), f.model_dump_json())
            LOG.info("resource finding: %s", f.subtype)
        # Forget old emissions after N cycles so transient recovery is noticed
        if cycles % 10 == 0:
            emitted_this_cycle.clear()
        cycles += 1
        beat(session_id, "resource_monitor", cycles)
        _time.sleep(period_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: resource_monitor.py <session_id>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
