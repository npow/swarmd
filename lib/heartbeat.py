"""Per-specialist heartbeat file writes."""

from __future__ import annotations

import json
import os
import tempfile
import time

from swarm.lib.paths import health_beat_path


def beat(session_id: str, specialist: str, cycles: int) -> None:
    """Atomically update a specialist's heartbeat file."""
    target = health_beat_path(session_id, specialist)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "last_cycle_ts": time.time(),
            "cycles_completed": cycles,
        }
    )
    # Atomic: write to temp + rename so partial writes can't be observed
    fd, tmp = tempfile.mkstemp(prefix=".beat.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def is_stale(session_id: str, specialist: str, max_age_sec: float = 60.0) -> bool:
    """Return True if the most recent beat is older than max_age_sec or missing."""
    p = health_beat_path(session_id, specialist)
    if not p.exists():
        return True
    try:
        data = json.loads(p.read_text())
        return (time.time() - float(data.get("last_cycle_ts", 0))) > max_age_sec
    except Exception:
        return True
