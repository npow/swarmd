"""Resource-monitoring Temporal activities — zombie / memory / disk checks.

Per plan Task 17 (ResourceMonitorWorkflow):

    check_zombies(mission_id) -> list[Finding]
    check_memory(mission_id) -> list[Finding]
    check_disk(mission_id) -> list[Finding]

Each activity is a thin wrapper around one of the resource heuristics
ported from ``specialists/resource_monitor.py`` (``_count_zombies``,
``_rss_mb``, ``_state_disk_mb`` + thresholds). The workflow schedules
them in parallel from its cadence loop and forwards any findings to
the parent via ``_emit_to_parent``.

Design notes:

* Each activity is best-effort cross-platform: ``ps`` on macOS/Linux,
  ``shutil.disk_usage`` for disk (portable), ``psutil.virtual_memory``
  when available otherwise graceful degradation.
* All three activities return a ``list[dict]`` (finding dicts ready for
  ``emit_finding``) rather than dataclasses — the workflow cares only
  about the list, and using dicts avoids a pydantic/dataclass import
  surface here.
* Thresholds are module-level constants matching the legacy specialist
  (``FD_WARN_RATIO`` etc. are NOT exported — these activities only care
  about zombie/memory/disk; file-descriptor exhaustion is covered by the
  same legacy module but is NOT part of the Task 17 scope, matching spec
  §6.2 which calls out only zombies/memory/disk).
* Findings are uniform in shape with the rest of the activity layer
  (``source``, ``type``, ``subtype``, ``severity``, ``verdict``,
  ``evidence``, plus ``mission_id`` passthrough). ``type`` is always
  ``"meta"`` per the legacy specialist.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from temporalio import activity

# Thresholds — ported verbatim from ``specialists/resource_monitor.py``
# so operators see the same numbers in findings regardless of which code
# path emitted them.
_ZOMBIE_WARN = 5
_ZOMBIE_CRIT = 20
_RSS_WARN_MB = 2000     # 2GB
_RSS_CRIT_MB = 5000     # 5GB
_DISK_WARN_RATIO = 0.85  # 85%
_DISK_CRIT_RATIO = 0.95  # 95%


def _finding(
    mission_id: str,
    subtype: str,
    severity: str,
    verdict: str,
    claim: str,
) -> dict[str, Any]:
    """Build one resource finding in the shared activity shape.

    Kept as a module-level helper so all three checks produce identically
    shaped payloads — the parent workflow's ``emit_finding`` dispatcher
    doesn't need per-activity special-casing.
    """
    return {
        "source": f"resource_monitor.{subtype}",
        "type": "meta",
        "subtype": subtype,
        "severity": severity,
        "verdict": verdict,
        "mission_id": mission_id,
        "subject_session": mission_id,
        "spawner_id": mission_id,
        "evidence": {"claim_excerpt": claim},
    }


# ------------------------------------------------------------- zombie check ---


@activity.defn(name="check_zombies")
async def check_zombies(mission_id: str) -> list[dict[str, Any]]:
    """Return a finding (or an empty list) for zombie-process pressure.

    Ported from ``resource_monitor._count_zombies``. The legacy version
    optionally scoped the count to tracked PIDs; the durable version
    reports ALL zombies on the host because:

    * The Temporal worker is one process among others. A sibling zombie
      on the same box still signals reaper failure that will eventually
      hit the mission's children.
    * Per-PID filtering requires a heartbeat/tree file which isn't
      synchronized with Temporal's view of the world.

    A single ``ps -A`` subprocess is the portable path (Linux + macOS).
    FileNotFoundError / TimeoutExpired are swallowed and return empty
    findings so a broken environment never crashes the workflow — the
    workflow will emit eventually when ``ps`` returns.
    """
    try:
        r = subprocess.run(
            ["ps", "-A", "-o", "pid=,state="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    zombies = 0
    for line in r.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        try:
            int(parts[0])  # validate pid
        except ValueError:
            continue
        state = parts[1]
        # 'Z' on Linux/macOS indicates a zombie/defunct process.
        if state.startswith("Z"):
            zombies += 1

    if zombies >= _ZOMBIE_CRIT:
        return [
            _finding(
                mission_id=mission_id,
                subtype="zombie_flood",
                severity="critical",
                verdict=(
                    f"{zombies} zombie processes — reaper is failing"
                ),
                claim=f"count={zombies}",
            )
        ]
    if zombies >= _ZOMBIE_WARN:
        return [
            _finding(
                mission_id=mission_id,
                subtype="zombies",
                severity="major",
                verdict=f"{zombies} zombie processes detected",
                claim=f"count={zombies}",
            )
        ]
    return []


# --------------------------------------------------------------- memory check -


@activity.defn(name="check_memory")
async def check_memory(mission_id: str) -> list[dict[str, Any]]:
    """Return findings for memory-pressure thresholds.

    The legacy specialist summed RSS across a set of tracked PIDs. The
    durable activity uses ``psutil.virtual_memory()`` when available and
    falls back to parsing ``ps -o rss=`` on the current process.
    ``psutil`` is a soft dependency — if it's not installed, the fallback
    covers the common case.

    Thresholds (``_RSS_WARN_MB`` / ``_RSS_CRIT_MB``) are applied against
    the **used** memory figure: for psutil that is
    ``virtual_memory().used`` / 1MB; for ``ps`` fallback it is the
    process RSS itself. The two sources measure different things, so
    the finding mentions which source was used.
    """
    used_mb = 0.0
    source = "unknown"
    try:
        import psutil

        vm = psutil.virtual_memory()
        used_mb = vm.used / (1024 * 1024)
        source = "psutil.virtual_memory"
    except ImportError:
        # Fallback: RSS of the current process. This undercounts (it's
        # this process only, not the whole host), which is fine —
        # a real OOM would still hit our own RSS first.
        try:
            r = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(os.getpid())],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode == 0 and r.stdout.strip():
                used_mb = int(r.stdout.strip()) / 1024.0
                source = "ps_rss_self"
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            return []

    if used_mb >= _RSS_CRIT_MB:
        return [
            _finding(
                mission_id=mission_id,
                subtype="memory_pressure",
                severity="critical",
                verdict=(
                    f"used memory {used_mb:.0f}MB >= critical {_RSS_CRIT_MB}MB "
                    f"(source={source})"
                ),
                claim=f"used_mb={used_mb:.0f};source={source}",
            )
        ]
    if used_mb >= _RSS_WARN_MB:
        return [
            _finding(
                mission_id=mission_id,
                subtype="memory_warning",
                severity="major",
                verdict=(
                    f"used memory {used_mb:.0f}MB >= warn {_RSS_WARN_MB}MB "
                    f"(source={source})"
                ),
                claim=f"used_mb={used_mb:.0f};source={source}",
            )
        ]
    return []


# ----------------------------------------------------------------- disk check -


@activity.defn(name="check_disk")
async def check_disk(mission_id: str) -> list[dict[str, Any]]:
    """Return findings for disk-usage thresholds.

    Uses ``shutil.disk_usage`` against the session state directory. The
    legacy specialist looked at the SIZE of the state dir (growing logs);
    the durable version checks the filesystem's overall fill ratio
    because that's the actually-limiting resource — the state dir can
    grow to 500MB on a 2TB disk without being a problem, but a 95%-full
    / filesystem WILL kill the mission regardless of state-dir size.

    The path is derived from the mission_id via ``swarm.lib.paths``.
    If the path doesn't exist yet (very early in mission startup),
    returns an empty list.
    """
    # Lazy import — deferred to match the ``read_recent_events`` pattern.
    from swarm.lib.paths import session_dir

    try:
        d = session_dir(mission_id)
    except ValueError:
        # Mission ID didn't validate — not a retryable error for us.
        return []

    # ``disk_usage`` needs an existing path. If the session dir is not
    # yet materialized, measure the parent (which always exists on a
    # well-formed SWARM_ROOT). This means we still emit findings even
    # before the mission has opened its first file.
    check_path = d if d.exists() else d.parent
    if not check_path.exists():
        return []

    try:
        usage = shutil.disk_usage(check_path)
    except OSError:
        return []

    ratio = usage.used / usage.total if usage.total else 0.0
    used_gb = usage.used / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)

    if ratio >= _DISK_CRIT_RATIO:
        return [
            _finding(
                mission_id=mission_id,
                subtype="disk_full",
                severity="critical",
                verdict=(
                    f"filesystem at {int(ratio * 100)}% "
                    f"({used_gb:.1f}GB / {total_gb:.1f}GB)"
                ),
                claim=f"ratio={ratio:.2f};used_gb={used_gb:.1f}",
            )
        ]
    if ratio >= _DISK_WARN_RATIO:
        return [
            _finding(
                mission_id=mission_id,
                subtype="disk_warning",
                severity="major",
                verdict=(
                    f"filesystem at {int(ratio * 100)}% "
                    f"({used_gb:.1f}GB / {total_gb:.1f}GB)"
                ),
                claim=f"ratio={ratio:.2f};used_gb={used_gb:.1f}",
            )
        ]
    return []
