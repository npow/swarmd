"""Tests for the `swarm-spawn` CLI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "swarm" / "swarm-spawn"


def _run(args: list[str], session_id: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["SESSION_ID"] = session_id
    return subprocess.run(
        ["python3", str(CLI), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_cli_is_executable():
    assert CLI.exists()
    assert os.access(CLI, os.X_OK), f"{CLI} not executable"


def test_cli_admits_simple_spawn(tmp_swarm_root, session_id):
    r = _run(
        [
            "--parent",
            "root",
            "--depth",
            "1",
            "--mission",
            "build it",
            "--dry-run",
        ],
        session_id,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["verdict"] == "admit"
    assert data["dry_run"] is True


def test_cli_rejects_depth_over_max(tmp_swarm_root, session_id):
    r = _run(
        [
            "--parent",
            "root",
            "--depth",
            "999",
            "--mission",
            "x",
            "--max-depth",
            "3",
        ],
        session_id,
    )
    assert r.returncode != 0
    data = json.loads(r.stdout)
    assert data["verdict"] == "reject"
    assert "depth" in data["reason"]


def test_cli_queues_at_budget(tmp_swarm_root, session_id):
    # Seed tree.json at the max-live budget of 1
    from swarmd.lib.paths import session_dir

    sdir = session_dir(session_id)
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "tree.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "a": {"parent": "root", "status": "running", "depth": 1}
                },
                "queue": [],
                "spawned_total": 1,
            }
        )
    )
    r = _run(
        [
            "--parent",
            "beta",
            "--depth",
            "1",
            "--mission",
            "x",
            "--max-live",
            "1",
        ],
        session_id,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["verdict"] == "queue"
    # The queued request should appear in tree.json
    tree = json.loads((sdir / "tree.json").read_text())
    assert len(tree["queue"]) == 1


def test_cli_refuses_without_session(tmp_path):
    # No SESSION_ID env and no --session
    env = {"PYTHONPATH": str(REPO_ROOT), "PATH": os.environ.get("PATH", "")}
    r = subprocess.run(
        ["python3", str(CLI), "--parent", "x", "--depth", "1", "--mission", "y"],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert r.returncode != 0
    assert "SESSION_ID" in r.stderr or "session" in r.stderr.lower()


def test_cli_admit_registers_child_id(tmp_swarm_root, session_id):
    r = _run(
        [
            "--parent",
            "root",
            "--depth",
            "1",
            "--mission",
            "go",
        ],
        session_id,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["verdict"] == "admit"
    assert "child_id" in data
    # Child was recorded in tree.json
    from swarmd.lib.paths import session_dir

    tree = json.loads((session_dir(session_id) / "tree.json").read_text())
    assert data["child_id"] in tree["nodes"]
