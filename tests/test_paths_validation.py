"""Tests for session_id validation and lazy path resolution."""

from __future__ import annotations

import pytest

from swarmd.lib.paths import (
    _reset_for_tests,
    mission_dir,
    out_of_tree_lock_path,
    session_dir,
    swarm_root,
    validate_session_id,
)


def test_valid_session_ids():
    # UUID (36 char), hex12, mixed with dashes and underscores
    for sid in [
        "abcdef012345",
        "550e8400-e29b-41d4-a716-446655440000",
        "test_session_1",
        "ABC12345",
    ]:
        assert validate_session_id(sid) == sid


def test_invalid_session_ids():
    for bad in [
        "",
        "../etc/passwd",
        "..",
        "a",
        "short",
        "/absolute/path",
        "has spaces",
        "has/slashes",
        "../traversal",
        ".hidden",
        "a" * 100,  # too long
    ]:
        with pytest.raises(ValueError):
            validate_session_id(bad)


def test_session_dir_rejects_bad_id():
    with pytest.raises(ValueError):
        session_dir("../escape")


def test_mission_dir_rejects_bad_id():
    with pytest.raises(ValueError):
        mission_dir("../escape")


def test_out_of_tree_lock_rejects_bad_id():
    with pytest.raises(ValueError):
        out_of_tree_lock_path("../escape")


def test_swarm_root_env_override(tmp_path, monkeypatch):
    _reset_for_tests()
    override = tmp_path / "alt_swarm"
    override.mkdir()
    monkeypatch.setenv("SWARM_ROOT", str(override))
    # Must reset since we're in a test that already triggered a resolution via fixture
    _reset_for_tests()
    assert swarm_root() == override


def test_swarm_root_lazy_cache_freezes_after_first_call(tmp_path, monkeypatch):
    """Once resolved, subsequent env mutations must NOT change the root."""
    _reset_for_tests()
    first = tmp_path / "first"
    first.mkdir()
    monkeypatch.setenv("SWARM_ROOT", str(first))
    assert swarm_root() == first  # first call locks it in

    # Now attacker tries to redirect
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.setenv("SWARM_ROOT", str(second))
    assert swarm_root() == first, "Root must stay frozen after first resolution"
