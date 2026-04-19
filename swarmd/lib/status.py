"""SessionSnapshot — the read-only abstraction between swarm state files
and any consumer (statusline, notifier, future dashboard).

When Temporal-backed swarm lands (post-2026-05), only `SessionSnapshot.load()`
changes; the dataclass shapes and all consumers remain untouched. Contract
tests in test_status_backend_contract.py enforce the swap.

All fields degrade to a pinned sentinel value (see module constants) when
the source file is missing or unparseable. This module never raises for
missing inputs — it is a display path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CriterionStatus:
    """One row in verifier_status.json. Shape matches what success_verifier
    currently writes: {status, exit_code} per criterion plus a top-level ts.
    stdout_tail and per-criterion held_sec are NOT in this model — richer
    telemetry would require a verifier change, out of scope here."""

    id: str
    status: str                 # "pass" | "fail" | "unknown"
    exit_code: int | None       # None when status == "unknown"
    last_check_ts: float        # top-level "ts" from verifier_status.json; 0.0 if missing


@dataclass(frozen=True)
class SpecialistHealth:
    """Synthesized from health/<name>.beat files."""

    name: str
    pid: int | None
    last_beat_age_sec: float    # math.inf when no beat file exists
    is_stale: bool              # last_beat_age_sec > STALE_SEC (60s default)
    cycles: int


# Sentinels the table in the spec pins
_SENTINEL_TEXT = ""
_SENTINEL_TS = 0.0


STALE_SEC: float = 60.0
MAX_MISSION_TITLE_LEN: int = 80


@dataclass(frozen=True)
class SessionSnapshot:
    """Frozen value — compute once per consumer tick; do not mutate."""

    session_id: str
    mission_title: str          # "" if mission.yaml missing
    workspace: str              # "" if mission.yaml missing
    launcher_alive: bool        # False if launcher.pid missing / dead
    started_at: float           # events.jsonl mtime of first line; 0.0 if empty
    duration_sec: float
    iter_count: int             # max cycles across all heartbeats; 0 if none
    criteria: list[CriterionStatus]
    all_pass: bool
    hold_sec: float             # approximate; 0.0 when not all_pass
    hold_target_sec: float
    findings_total: int
    findings_critical: int
    findings_major: int
    recent_findings: list             # list[Finding], kept untyped here to
                                      # avoid circular import at class-body time
    interventions_total: int
    interventions_pending_ack: int
    recent_interventions: list        # list[Intervention]
    health: list[SpecialistHealth]
    events_per_minute: float
    events_total: int


# ---------------------------------------------------------------------------
# Task 2: load() — empty-state baseline
# ---------------------------------------------------------------------------

from swarmd.lib.paths import session_dir, validate_session_id  # noqa: E402

import json as _json  # local alias to avoid shadowing
import time  # noqa: E402
from typing import Any  # noqa: E402
from swarmd.lib.launcher_liveness import launcher_alive as _launcher_alive_check  # noqa: E402


def _load_mission(session_id: str) -> dict[str, Any]:
    """Return parsed mission.yaml, or {} on any error. Never raises."""
    from swarmd.lib.paths import mission_yaml_path

    path = mission_yaml_path(session_id)
    if not path.exists():
        return {}
    try:
        import yaml  # yaml is already a runtime dep

        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _load_verifier_status(sdir: Path) -> tuple[list[CriterionStatus], bool]:
    """Return (criteria, all_pass). Empty list + False on any error."""
    path = sdir / "verifier_status.json"
    if not path.exists():
        return [], False
    try:
        data = _json.loads(path.read_text())
    except (OSError, ValueError):
        return [], False
    ts = float(data.get("ts") or 0.0)
    per = data.get("per_criterion") or {}
    criteria = [
        CriterionStatus(
            id=cid,
            status=str(row.get("status") or "unknown"),
            exit_code=row.get("exit_code") if row.get("exit_code") is not None else None,
            last_check_ts=ts,
        )
        for cid, row in per.items()
        if isinstance(row, dict)
    ]
    # Sort for deterministic output
    criteria.sort(key=lambda c: c.id)
    return criteria, bool(data.get("all_pass", False))


def _load_findings(
    session_id: str, max_recent: int = 10
) -> tuple[int, int, int, list]:
    """Return (total, critical_count, major_count, recent_last_N).

    Reads by line — each line parsed independently, malformed lines skipped.
    """
    from swarmd.lib.paths import findings_path
    from swarmd.schemas.finding import Finding

    path = findings_path(session_id)
    if not path.exists():
        return 0, 0, 0, []
    total = 0
    crit = 0
    maj = 0
    recent: list = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0, 0, 0, []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            f = Finding.model_validate_json(line)
        except Exception:
            continue
        total += 1
        sev = getattr(f, "severity", None)
        if sev == "critical":
            crit += 1
        elif sev == "major":
            maj += 1
        recent.append(f)
    return total, crit, maj, recent[-max_recent:]


def _load_interventions(
    session_id: str, max_recent: int = 5
) -> tuple[int, int, list]:
    """Return (total, pending_ack, recent_last_N)."""
    from swarmd.lib.paths import interventions_acked_path, interventions_path
    from swarmd.schemas.intervention import Intervention

    ipath = interventions_path(session_id)
    if not ipath.exists():
        return 0, 0, []
    acked: set[str] = set()
    apath = interventions_acked_path(session_id)
    if apath.exists():
        try:
            acked = {line.strip() for line in apath.read_text().splitlines() if line.strip()}
        except OSError:
            pass
    total = 0
    pending = 0
    recent: list = []
    try:
        lines = ipath.read_text().splitlines()
    except OSError:
        return 0, 0, []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            iv = Intervention.model_validate_json(line)
        except Exception:
            continue
        total += 1
        if iv.id not in acked:
            pending += 1
        recent.append(iv)
    return total, pending, recent[-max_recent:]


# ---------------------------------------------------------------------------
# Task 6: health, iter_count, events
# ---------------------------------------------------------------------------

def _load_health(session_id: str, stale_sec: float = STALE_SEC) -> tuple[list[SpecialistHealth], int]:
    """Return (health_list, iter_count) where iter_count is max cycles across beats."""
    from swarmd.lib.paths import session_dir as _sdir

    health_dir = _sdir(session_id) / "health"
    if not health_dir.exists():
        return [], 0
    now = time.time()
    out: list[SpecialistHealth] = []
    iter_count = 0
    for beat_file in sorted(health_dir.glob("*.beat")):
        name = beat_file.stem
        try:
            data = _json.loads(beat_file.read_text())
        except (OSError, ValueError):
            out.append(
                SpecialistHealth(
                    name=name, pid=None, last_beat_age_sec=math.inf,
                    is_stale=True, cycles=0,
                )
            )
            continue
        ts = float(data.get("last_cycle_ts") or 0.0)
        age = now - ts if ts > 0 else math.inf
        cycles = int(data.get("cycles_completed") or 0)
        iter_count = max(iter_count, cycles)
        pid_val = data.get("pid")
        pid = int(pid_val) if pid_val else None
        out.append(
            SpecialistHealth(
                name=name,
                pid=pid,
                last_beat_age_sec=age,
                is_stale=age > stale_sec,
                cycles=cycles,
            )
        )
    return out, iter_count


def _load_events(session_id: str) -> tuple[int, float]:
    """Return (total_count, per_minute_rate). Rate is count in last 60s."""
    import calendar as _cal
    from swarmd.lib.paths import events_path as _ep

    path = _ep(session_id)
    if not path.exists():
        return 0, 0.0
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return 0, 0.0
    non_empty = [ln for ln in lines if ln.strip()]
    total = len(non_empty)
    if total < 2:
        return total, 0.0
    # Rate: count events whose ts_wall is within last 60s
    now_sec = time.time()
    cutoff = now_sec - 60.0
    count_recent = 0
    for ln in non_empty[-1000:]:  # cap scan; statusline shouldn't stall on huge files
        try:
            d = _json.loads(ln)
        except ValueError:
            continue
        ts_wall = d.get("ts_wall") or ""
        # ts_wall in swarm events is ISO "YYYY-MM-DDTHH:MM:SSZ"
        try:
            parsed = time.strptime(ts_wall, "%Y-%m-%dT%H:%M:%SZ")
            epoch = _cal.timegm(parsed)
            if epoch >= cutoff:
                count_recent += 1
        except (ValueError, TypeError):
            continue
    return total, float(count_recent)


# Rebuild _load_snapshot to include health + events (replaces earlier definition)
def _load_snapshot(session_id: str) -> SessionSnapshot:  # type: ignore[no-redef]
    """See SessionSnapshot.load()."""
    validate_session_id(session_id)
    sdir = session_dir(session_id)
    criteria, all_pass = _load_verifier_status(sdir)
    mission = _load_mission(session_id)
    mission_title = str(mission.get("mission") or "")[:MAX_MISSION_TITLE_LEN]
    workspace = str(mission.get("workspace") or "")
    hold_target_sec = float(
        (mission.get("verification") or {}).get("hold_window_sec") or 0.0
    )

    hold_sec = 0.0
    if all_pass:
        vstat = sdir / "verifier_status.json"
        if vstat.exists():
            try:
                verifier_ts = float(_json.loads(vstat.read_text()).get("ts") or 0.0)
                if verifier_ts > 0:
                    hold_sec = max(0.0, time.time() - verifier_ts)
            except (OSError, ValueError):
                pass

    f_total, f_crit, f_maj, f_recent = _load_findings(session_id)
    i_total, i_pending, i_recent = _load_interventions(session_id)
    health, iter_count = _load_health(session_id)
    events_total, events_per_minute = _load_events(session_id)

    return SessionSnapshot(
        session_id=session_id,
        mission_title=mission_title,
        workspace=workspace,
        launcher_alive=_launcher_alive_check(session_id),
        started_at=_SENTINEL_TS,
        duration_sec=0.0,
        iter_count=iter_count,
        criteria=criteria,
        all_pass=all_pass,
        hold_sec=hold_sec,
        hold_target_sec=hold_target_sec,
        findings_total=f_total,
        findings_critical=f_crit,
        findings_major=f_maj,
        recent_findings=f_recent,
        interventions_total=i_total,
        interventions_pending_ack=i_pending,
        recent_interventions=i_recent,
        health=health,
        events_per_minute=events_per_minute,
        events_total=events_total,
    )


# Rebind load() to the updated implementation
SessionSnapshot.load = classmethod(lambda cls, sid: _load_snapshot(sid))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Task 7: find_most_recent
# ---------------------------------------------------------------------------

def _find_most_recent() -> str | None:
    from swarmd.lib.paths import events_path as _ep, swarm_root

    state_root = swarm_root() / "state"
    if not state_root.exists():
        return None
    candidates: list[tuple[float, str]] = []
    for d in state_root.iterdir():
        if not d.is_dir():
            continue
        ep = _ep(d.name)
        if not ep.exists():
            continue
        try:
            candidates.append((ep.stat().st_mtime, d.name))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


SessionSnapshot.find_most_recent = classmethod(  # type: ignore[attr-defined]
    lambda cls: _find_most_recent()
)
