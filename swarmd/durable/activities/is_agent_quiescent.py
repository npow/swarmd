"""``is_agent_quiescent`` — check whether the agent has been idle long enough
for criterion checks to produce stable results.

Scans the workspace for recently-modified files. If any file was touched
within ``quiet_period_sec``, the agent is considered active and the verifier
should defer criterion checks to avoid catching intermediate state.

General-purpose: doesn't assume anything about what the agent is doing or
what files it modifies. Any write to the workspace resets the quiet clock.
"""

from __future__ import annotations

import time
from pathlib import Path

from temporalio import activity


@activity.defn(name="is_agent_quiescent")
async def is_agent_quiescent(workspace: str, quiet_period_sec: int) -> bool:
    """Return True if no file in ``workspace`` was modified in the last N seconds.

    Walks the workspace tree (max depth 4, skips .git / .swarm / __pycache__
    / node_modules to keep the scan fast). Returns False (= agent active) if
    ANY file's mtime is within ``quiet_period_sec`` of now.

    If ``quiet_period_sec <= 0``, always returns True (gating disabled).
    """
    if quiet_period_sec <= 0:
        return True

    cutoff = time.time() - quiet_period_sec
    root = Path(workspace)
    skip = {".git", ".swarm", "__pycache__", "node_modules", ".venv", ".tox"}

    def _walk(p: Path, depth: int = 0) -> bool:
        """Return True if a recently-modified file is found."""
        if depth > 4:
            return False
        try:
            for child in p.iterdir():
                if child.name in skip:
                    continue
                try:
                    if child.stat().st_mtime > cutoff:
                        return True
                except OSError:
                    continue
                if child.is_dir():
                    if _walk(child, depth + 1):
                        return True
        except OSError:
            pass
        return False

    recently_modified = _walk(root)
    return not recently_modified
