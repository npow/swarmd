"""Session path helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path

# Lazy first-call pin for SWARM_ROOT / SWARM_CONFIG.
#
# Security goal: a worker-injected env mutation LATER in the session cannot
# redirect hooks/specialists to an attacker-controlled directory.
# The roots are resolved on first call and frozen thereafter. Tests that
# need to override must do so BEFORE any path function is called (which is
# fine for fixture-based setup).
_SWARM_ROOT: Path | None = None
_SWARM_CONFIG: Path | None = None


def _resolve_swarm_root() -> Path:
    global _SWARM_ROOT
    if _SWARM_ROOT is None:
        override = os.environ.get("SWARM_ROOT")
        _SWARM_ROOT = (
            Path(override).expanduser() if override else Path.home() / ".swarm"
        )
    return _SWARM_ROOT


def _resolve_swarm_config() -> Path:
    global _SWARM_CONFIG
    if _SWARM_CONFIG is None:
        override = os.environ.get("SWARM_CONFIG")
        _SWARM_CONFIG = (
            Path(override).expanduser()
            if override
            else Path.home() / ".config" / "swarm"
        )
    return _SWARM_CONFIG


def _reset_for_tests() -> None:
    """Reset the lazy cache. FOR TESTS ONLY — not called from production code."""
    global _SWARM_ROOT, _SWARM_CONFIG
    _SWARM_ROOT = None
    _SWARM_CONFIG = None


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{7,63}$")


def validate_session_id(session_id: str) -> str:
    """Raise ValueError if session_id looks like path traversal or is malformed."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return session_id


def swarm_root() -> Path:
    return _resolve_swarm_root()


def session_dir(session_id: str) -> Path:
    validate_session_id(session_id)
    return _resolve_swarm_root() / "state" / session_id


def mission_dir(session_id: str) -> Path:
    validate_session_id(session_id)
    return _resolve_swarm_root() / "missions" / session_id


def events_path(session_id: str) -> Path:
    return session_dir(session_id) / "events.jsonl"


def findings_path(session_id: str) -> Path:
    return session_dir(session_id) / "findings.jsonl"


def interventions_path(session_id: str) -> Path:
    return session_dir(session_id) / "interventions.jsonl"


def interventions_acked_path(session_id: str) -> Path:
    return session_dir(session_id) / "interventions-acked.jsonl"


def strikes_path(session_id: str) -> Path:
    return session_dir(session_id) / "strikes.json"


def tried_strategies_path(session_id: str) -> Path:
    return session_dir(session_id) / "tried_strategies.jsonl"


def mission_yaml_path(session_id: str) -> Path:
    return mission_dir(session_id) / "mission.yaml"


def mission_lock_path(session_id: str) -> Path:
    return mission_dir(session_id) / "mission.lock.json"


def out_of_tree_lock_path(session_id: str) -> Path:
    validate_session_id(session_id)
    return _resolve_swarm_config() / "locks" / f"{session_id}.sha"


def health_beat_path(session_id: str, specialist: str) -> Path:
    return session_dir(session_id) / "health" / f"{specialist}.beat"


def ensure_session_dirs(session_id: str) -> None:
    for p in [
        session_dir(session_id),
        session_dir(session_id) / "health",
        session_dir(session_id) / "children",
        session_dir(session_id) / "events_detail",
        mission_dir(session_id),
        mission_dir(session_id) / "checks",
        out_of_tree_lock_path(session_id).parent,
    ]:
        p.mkdir(parents=True, exist_ok=True)


def claude_transcript_path(session_id: str, cwd: str) -> Path:
    """
    Derive the path to Claude Code's session transcript.

    Claude Code stores transcripts at ~/.claude/projects/<encoded-cwd>/<session>.jsonl,
    where encoded-cwd replaces / with -.
    """
    encoded = cwd.replace("/", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
