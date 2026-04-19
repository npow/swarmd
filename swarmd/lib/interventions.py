"""Shared intervention queue read / ack helpers.

Used by all hooks and the coordinator's re-issue loop. Single source of
truth so the queue protocol can evolve without divergence.
"""

from __future__ import annotations

import json
import time

from swarmd.lib.locking import write_line
from swarmd.lib.paths import interventions_acked_path, interventions_path
from swarmd.schemas.intervention import Intervention


def _acked_ids(session_id: str) -> set[str]:
    out: set[str] = set()
    p = interventions_acked_path(session_id)
    if not p.exists():
        return out
    with p.open() as f:
        for line in f:
            try:
                d = json.loads(line)
                if isinstance(d, dict) and "id" in d:
                    out.add(d["id"])
            except Exception:
                continue
    return out


def read_pending(session_id: str) -> list[Intervention]:
    """Return interventions that have not yet been acked."""
    path = interventions_path(session_id)
    if not path.exists():
        return []
    acked = _acked_ids(session_id)
    out: list[Intervention] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                iv = Intervention.model_validate_json(line)
            except Exception:
                continue
            if iv.id not in acked:
                out.append(iv)
    return out


def read_all(session_id: str) -> list[Intervention]:
    """Return every intervention ever written, regardless of ack state."""
    path = interventions_path(session_id)
    if not path.exists():
        return []
    out: list[Intervention] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Intervention.model_validate_json(line))
            except Exception:
                continue
    return out


def ack(session_id: str, intervention_id: str, consumed_at: str) -> None:
    """Append a single ack record."""
    write_line(
        interventions_acked_path(session_id),
        json.dumps(
            {"id": intervention_id, "consumed_at": consumed_at, "ts": time.time()}
        ),
    )


def ack_all(session_id: str, ivs: list[Intervention], consumed_at: str) -> None:
    for iv in ivs:
        ack(session_id, iv.id, consumed_at)


def intervention_age_sec(iv: Intervention) -> float:
    """Extract age in seconds from intervention id (format: i-<ms>-<short>)."""
    try:
        ms = int(iv.id.split("-")[1])
        return max(0.0, time.time() - ms / 1000.0)
    except (IndexError, ValueError):
        return 0.0
