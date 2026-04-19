"""Tests for the ``verify_tamper`` Temporal activity.

Per plan Task 5 and spec §6.3 (the ``verify_tamper`` row):

    verify_tamper(mission_dir, out_of_tree_sha_path) →
        {detected: bool, finding: dict | None}

    Short (seconds). Non-idempotent in the sense of being observational —
    safe to re-run, but the result may change if the files change between
    calls. That is the intended semantic.

These tests drive the activity through ``temporalio.testing.ActivityEnvironment``
so we do not need a running Temporal server. Since ``pyproject.toml`` sets
``asyncio_mode = "auto"``, the ``@pytest.mark.asyncio`` decorator is optional;
we keep it explicit for readability and to guard against future config drift.

The activity is a port of the ``verify_tamper`` function in
``specialists/success_verifier.py`` lines 117-164. The detection semantics
are preserved, but the new activity:

* Does not depend on the old ``Mission``/``Finding`` schemas.
* Takes explicit ``mission_dir`` and ``out_of_tree_sha_path`` arguments so
  the caller owns path resolution (which is what a Temporal activity should
  do — avoid hidden globals).
* Returns a plain ``TamperResult`` dataclass rather than a ``Finding`` object,
  so the workflow can decide what to do with the detection (emit a finding,
  pause, intervene, etc.) without this activity having to know.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from temporalio.testing import ActivityEnvironment

from swarmd.durable.activities.verify_tamper import (
    TamperResult,
    verify_tamper,
)


def _write_mission(tmp_path, files: dict[str, str]) -> tuple[str, str]:
    """Create a mission dir with ``files`` (rel_path → content) and a matching
    ``mission.lock.json`` + out-of-tree sha. Return ``(mission_dir, sha_path)``.

    This helper is deliberately NOT in ``conftest.py`` — the tests read more
    cleanly when the invariant setup is right here next to the assertions.
    """
    mission_dir = tmp_path / "mission"
    mission_dir.mkdir()
    file_hashes: dict[str, str] = {}
    for rel, content in files.items():
        fp = mission_dir / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        file_hashes[rel] = hashlib.sha256(content.encode()).hexdigest()

    lock = {"files": file_hashes}
    lock_bytes = json.dumps(lock, sort_keys=True).encode()
    lock_path = mission_dir / "mission.lock.json"
    lock_path.write_bytes(lock_bytes)

    sha_path = tmp_path / "mission.lock.sha"
    sha_path.write_text(hashlib.sha256(lock_bytes).hexdigest())

    return str(mission_dir), str(sha_path)


@pytest.mark.asyncio
async def test_lock_intact_returns_not_detected(tmp_path):
    """Baseline: pristine mission files + matching lock + matching sha →
    ``detected=False`` and ``finding=None``. If this test fails, every other
    path in this activity is also broken."""
    mission_dir, sha_path = _write_mission(
        tmp_path, {"src/foo.py": "print('hello')\n", "README.md": "# hi\n"}
    )

    env = ActivityEnvironment()
    result = await env.run(verify_tamper, mission_dir, sha_path)

    assert isinstance(result, TamperResult)
    assert result.detected is False
    assert result.finding is None


@pytest.mark.asyncio
async def test_file_modified_returns_detected(tmp_path):
    """Mutating a pinned file after the lock is written must be detected.
    The finding must use the spec's ``tamper_detected`` subtype so downstream
    consumers (anti-cheat panel, intervention judge) can route it."""
    mission_dir, sha_path = _write_mission(tmp_path, {"src/foo.py": "original\n"})

    # Tamper with the file — the lock still records the original hash.
    (tmp_path / "mission" / "src" / "foo.py").write_text("tampered\n")

    env = ActivityEnvironment()
    result = await env.run(verify_tamper, mission_dir, sha_path)

    assert result.detected is True
    assert result.finding is not None
    assert result.finding["type"] == "meta"
    assert result.finding["subtype"] == "tamper_detected"
    assert result.finding["severity"] == "critical"
    # verdict should identify which file was tampered with so humans don't
    # have to diff by hand.
    assert "src/foo.py" in result.finding["verdict"]


@pytest.mark.asyncio
async def test_lock_file_missing_returns_detected(tmp_path):
    """An absent ``mission.lock.json`` is itself a tamper signal: the mission
    was never locked, or an attacker deleted it. Either way, we cannot verify
    anything, so fail closed."""
    mission_dir = tmp_path / "mission"
    mission_dir.mkdir()
    # No lock written.
    sha_path = tmp_path / "mission.lock.sha"
    sha_path.write_text("deadbeef")

    env = ActivityEnvironment()
    result = await env.run(verify_tamper, str(mission_dir), str(sha_path))

    assert result.detected is True
    assert result.finding is not None
    assert result.finding["subtype"] == "tamper_detected"
    assert "lock" in result.finding["verdict"].lower()


@pytest.mark.asyncio
async def test_out_of_tree_sha_missing_returns_detected(tmp_path):
    """The out-of-tree sha is the anchor: if it's gone, the in-tree lock is
    unverifiable and we must assume tamper."""
    mission_dir = tmp_path / "mission"
    mission_dir.mkdir()
    lock_bytes = json.dumps({"files": {}}, sort_keys=True).encode()
    (mission_dir / "mission.lock.json").write_bytes(lock_bytes)
    # No sha file.
    sha_path = tmp_path / "mission.lock.sha"

    env = ActivityEnvironment()
    result = await env.run(verify_tamper, str(mission_dir), str(sha_path))

    assert result.detected is True
    assert result.finding is not None
    assert result.finding["subtype"] == "tamper_detected"
    assert "sha" in result.finding["verdict"].lower()


@pytest.mark.asyncio
async def test_sha_mismatch_returns_detected(tmp_path):
    """If someone rewrites the in-tree lock (say, to legitimize a tampered
    file) but can't touch the out-of-tree sha, the hashes won't line up and
    we must detect it."""
    mission_dir = tmp_path / "mission"
    mission_dir.mkdir()
    lock_bytes = json.dumps({"files": {}}, sort_keys=True).encode()
    (mission_dir / "mission.lock.json").write_bytes(lock_bytes)

    sha_path = tmp_path / "mission.lock.sha"
    # Deliberately wrong sha — does not correspond to the lock contents.
    sha_path.write_text("0" * 64)

    env = ActivityEnvironment()
    result = await env.run(verify_tamper, str(mission_dir), str(sha_path))

    assert result.detected is True
    assert result.finding is not None
    assert result.finding["subtype"] == "tamper_detected"
    assert "hash" in result.finding["verdict"].lower() or "mismatch" in result.finding["verdict"].lower()


@pytest.mark.asyncio
async def test_missing_pinned_file_returns_detected(tmp_path):
    """Deleting a pinned file is tamper: the lock references a file that no
    longer exists. The verdict should name the missing path so operators
    know what to restore."""
    mission_dir, sha_path = _write_mission(tmp_path, {"src/foo.py": "print('hi')\n"})
    # Delete the pinned file.
    (tmp_path / "mission" / "src" / "foo.py").unlink()

    env = ActivityEnvironment()
    result = await env.run(verify_tamper, mission_dir, sha_path)

    assert result.detected is True
    assert result.finding is not None
    assert result.finding["subtype"] == "tamper_detected"
    assert "src/foo.py" in result.finding["verdict"]
