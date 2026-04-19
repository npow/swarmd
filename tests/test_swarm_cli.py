"""Tests for the swarm-cli admin tool."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "swarm" / "swarm-cli"


def _run(args: list[str], tmp_swarm_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["SWARM_ROOT"] = str(tmp_swarm_root)
    return subprocess.run(
        ["python3", str(CLI), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def test_cli_exists():
    assert CLI.exists()
    assert os.access(CLI, os.X_OK)


def test_list_sessions_empty(tmp_swarm_root):
    r = _run(["list-sessions"], tmp_swarm_root)
    assert r.returncode == 0
    assert "No sessions" in r.stdout


def test_list_sessions_shows_entries(tmp_swarm_root, session_id):
    import yaml

    from swarmd.lib.paths import mission_yaml_path

    # Seed a mission for the session
    path = mission_yaml_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"mission": "build X", "workspace": "/tmp", "success_criteria": [{"id": "a", "description": "", "check": "true"}]}))

    r = _run(["list-sessions"], tmp_swarm_root)
    assert r.returncode == 0
    assert session_id in r.stdout
    assert "build X" in r.stdout


def test_inspect_rejects_bad_session_id(tmp_swarm_root):
    r = _run(["inspect", "../etc"], tmp_swarm_root)
    assert r.returncode != 0
    assert "Invalid session_id" in r.stderr


def test_inspect_reports_not_found(tmp_swarm_root):
    r = _run(["inspect", "abcdef012345"], tmp_swarm_root)
    assert r.returncode != 0
    assert "not found" in r.stderr


def test_inspect_shows_state(tmp_swarm_root, session_id):
    from swarmd.lib.paths import session_dir

    # Seed some state
    (session_dir(session_id) / "verifier_status.json").write_text(
        json.dumps({"ts": 0, "all_pass": True, "per_criterion": {}})
    )
    r = _run(["inspect", session_id], tmp_swarm_root)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert data["session_id"] == session_id
    assert data["verifier_status"]["all_pass"] is True


def test_tree_no_subagents(tmp_swarm_root, session_id):
    r = _run(["tree", session_id], tmp_swarm_root)
    assert r.returncode == 0
    assert "No tree" in r.stdout


def test_tree_shows_subagents(tmp_swarm_root, session_id):
    from swarmd.lib.paths import session_dir

    tree = {
        "nodes": {
            "c1": {"parent": "root", "status": "running", "pid": 1234, "depth": 1}
        },
        "queue": [],
        "spawned_total": 1,
    }
    (session_dir(session_id) / "tree.json").write_text(json.dumps(tree))
    r = _run(["tree", session_id], tmp_swarm_root)
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["spawned_total"] == 1
    assert "c1" in data["nodes"]


def test_revise_mission_rejects_bad_input(tmp_swarm_root, session_id, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: a: valid: mission: spec")
    r = _run(["revise-mission", session_id, str(bad)], tmp_swarm_root)
    assert r.returncode != 0


def test_revise_mission_replaces_yaml(tmp_swarm_root, session_id, tmp_path):
    import yaml

    from swarmd.lib.paths import mission_yaml_path

    orig = mission_yaml_path(session_id)
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_text(
        yaml.safe_dump(
            {
                "mission": "original",
                "workspace": "/tmp",
                "success_criteria": [{"id": "a", "description": "", "check": "true"}],
            }
        )
    )

    new = tmp_path / "new.yaml"
    new.write_text(
        yaml.safe_dump(
            {
                "mission": "revised",
                "workspace": "/tmp",
                "success_criteria": [
                    {"id": "b", "description": "", "check": "true"}
                ],
            }
        )
    )
    r = _run(["revise-mission", session_id, str(new)], tmp_swarm_root)
    assert r.returncode == 0, r.stderr
    assert "Mission revised" in r.stdout
    assert orig.with_suffix(".yaml.bak").exists()
    # New content present
    data = yaml.safe_load(orig.read_text())
    assert data["mission"] == "revised"


def test_promote_rejects_missing_version(tmp_swarm_root):
    r = _run(["promote", "nonexistent-v99"], tmp_swarm_root)
    assert r.returncode != 0
    assert "no staged version" in r.stderr.lower()


def test_cli_help_exits_cleanly():
    r = subprocess.run(
        ["python3", str(CLI), "--help"], capture_output=True, text=True, timeout=5
    )
    assert r.returncode == 0
    assert "list-sessions" in r.stdout
    assert "inspect" in r.stdout


# ----------------------------------------------------------------------------
# gc: sweep orphaned specialist daemons
# ----------------------------------------------------------------------------


def _spawn_fake_specialist(session_id: str):
    """Spawn a long-running process whose command line looks like a specialist.

    Used in gc tests to verify the sweep picks up processes whose `ps`
    signature matches `swarm.specialists.* <session_id>`. We can't spawn a
    real specialist — it would self-terminate on startup (the whole point
    of the fix). Instead we spawn a Python sleeper with a crafted argv.
    """
    import sys as _sys

    return subprocess.Popen(
        [
            _sys.executable,
            "-c",
            "import time; time.sleep(120)",
            # These argv entries are what `ps -eo command` will display:
            "swarm.specialists.coordinator",
            session_id,
        ]
    )


def _proc_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return True


def test_gc_kills_orphan_specialist(tmp_swarm_root, session_id):
    """gc must kill specialist processes when their launcher.pid is missing.

    This is the post-hoc cleanup path for daemons that already leaked — the
    ones we saw in the wild (74 orphans, 45 sessions). It complements the
    in-process self-termination fix.
    """
    import time as _time

    from swarmd.lib.paths import session_dir

    # No launcher.pid → this session is an orphan.
    assert not (session_dir(session_id) / "launcher.pid").exists()

    fake = _spawn_fake_specialist(session_id)
    try:
        _time.sleep(0.3)  # let the kernel schedule so `ps` can see it
        assert _proc_exists(fake.pid), "fake specialist didn't start"

        r = _run(["gc"], tmp_swarm_root)
        assert r.returncode == 0, f"gc failed:\n{r.stdout}\n{r.stderr}"
        assert str(fake.pid) in r.stdout, (
            "gc output must report the pid it killed; got:\n" + r.stdout
        )

        # Give signals time to propagate.
        try:
            fake.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fake.kill()
            fake.wait()
            raise AssertionError("gc did not kill the orphan process")
        assert not _proc_exists(fake.pid)
    finally:
        if fake.poll() is None:
            fake.kill()
            fake.wait()


def test_gc_leaves_live_session_alone(tmp_swarm_root, session_id):
    """gc must NOT kill specialists whose launcher pid is still alive."""
    import time as _time

    from swarmd.lib.launcher_liveness import write_launcher_pid

    # Record our own pid as the launcher — we're alive, so this session
    # is legitimate, not an orphan.
    write_launcher_pid(session_id, os.getpid())

    fake = _spawn_fake_specialist(session_id)
    try:
        _time.sleep(0.3)

        r = _run(["gc"], tmp_swarm_root)
        assert r.returncode == 0, f"gc failed:\n{r.stdout}\n{r.stderr}"
        assert str(fake.pid) not in r.stdout, (
            "gc must NOT touch live sessions; unexpected pid in output:\n" + r.stdout
        )
        # Still running
        assert _proc_exists(fake.pid)
    finally:
        fake.kill()
        fake.wait()


def test_gc_kills_specialist_with_no_state_dir(tmp_swarm_root):
    """gc must kill specialists whose session state dir is missing entirely.

    This happens when a user cleaned up ~/.swarm/state manually but some
    daemons were still running. They have no pid file because they have no
    dir — still orphans.
    """
    import time as _time
    import uuid

    bogus_session_id = uuid.uuid4().hex[:12]
    # Deliberately do NOT create the state dir.
    from swarmd.lib.paths import session_dir

    assert not session_dir(bogus_session_id).exists()

    fake = _spawn_fake_specialist(bogus_session_id)
    try:
        _time.sleep(0.3)

        r = _run(["gc"], tmp_swarm_root)
        assert r.returncode == 0, f"gc failed:\n{r.stdout}\n{r.stderr}"
        assert str(fake.pid) in r.stdout

        try:
            fake.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fake.kill()
            fake.wait()
            raise AssertionError("gc did not kill the orphan")
    finally:
        if fake.poll() is None:
            fake.kill()
            fake.wait()


def test_gc_reports_nothing_to_clean(tmp_swarm_root):
    """With no orphans at all, gc reports that and exits 0."""
    r = _run(["gc"], tmp_swarm_root)
    assert r.returncode == 0
    assert "0" in r.stdout  # e.g. "killed 0 orphan specialists"
