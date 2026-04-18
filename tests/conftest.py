"""Shared pytest fixtures for swarm tests."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

# Make `swarm` importable
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_swarm_root(tmp_path, monkeypatch):
    """Isolate SWARM_ROOT and SWARM_CONFIG to a tmp dir per test."""
    root = tmp_path / "swarm"
    cfg = tmp_path / "config"
    root.mkdir()
    cfg.mkdir()
    monkeypatch.setenv("SWARM_ROOT", str(root))
    monkeypatch.setenv("SWARM_CONFIG", str(cfg))
    monkeypatch.setenv("PEER_CONSULT_DISABLED", "1")
    # Reset lazy cache so this test's env vars take effect
    from swarm.lib.paths import _reset_for_tests

    _reset_for_tests()
    return root


@pytest.fixture
def session_id(tmp_swarm_root):
    from swarm.lib.paths import ensure_session_dirs

    sid = uuid.uuid4().hex[:12]
    ensure_session_dirs(sid)
    return sid


@pytest.fixture
def sample_mission(tmp_path):
    """A minimal but valid Mission object for tests."""
    from swarm.schemas.mission import Mission, SuccessCriterion

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Mission(
        mission="Test mission",
        workspace=str(workspace),
        success_criteria=[
            SuccessCriterion(
                id="always_pass",
                description="trivial",
                check="true",
                timeout_sec=5,
            )
        ],
    )
