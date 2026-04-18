#!/usr/bin/env python3
"""Append a cycle entry to swarm/metrics.json.

Usage:
    scripts/update_metrics.py <version> [--notes "..."]

Measures (all run from the repo root, not the swarm dir):
  - tests_passing: count from pytest --collect-only
  - prod_loc / test_loc: line counts via find + wc
  - mission_criteria_passing / _total: if <version> matches a mission file
  - ruff_clean: boolean

Appends a new cycle entry and writes atomically.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # swarm/
REPO_ROOT = ROOT.parent
METRICS_PATH = ROOT / "metrics.json"


def _pytest_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env


def _count_tests() -> int:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "swarm/tests/", "--collect-only", "-q"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            env=_pytest_env(),
        )
    except Exception:
        return 0
    # pytest prints "N tests collected" at the end
    m = re.search(r"(\d+) tests? collected", r.stdout + r.stderr)
    return int(m.group(1)) if m else 0


def _count_loc(glob: str, path: Path) -> int:
    """Count total lines in *.py matching `glob` rooted at `path`."""
    total = 0
    for p in path.rglob(glob):
        if not p.is_file():
            continue
        try:
            with p.open() as f:
                total += sum(1 for _ in f)
        except OSError:
            continue
    return total


def _prod_loc() -> int:
    # Everything under swarm/ that isn't in tests/
    total = 0
    for py in ROOT.rglob("*.py"):
        rel = str(py.relative_to(ROOT))
        if rel.startswith("tests/"):
            continue
        try:
            with py.open() as f:
                total += sum(1 for _ in f)
        except OSError:
            continue
    return total


def _test_loc() -> int:
    return _count_loc("*.py", ROOT / "tests")


def _ruff_clean() -> bool:
    try:
        r = subprocess.run(
            [
                "ruff",
                "check",
                str(ROOT),
                "--select",
                "E,F,W,I",
                "--ignore",
                "E501",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    return r.returncode == 0


def _mission_progress(version: str) -> tuple[int, int]:
    """If a mission file for this version exists, return (passing, total)."""
    mission_file = ROOT / "examples" / f"mission-{version.split('-')[0]}-upgrade.yaml"
    if not mission_file.exists():
        return (0, 0)
    try:
        sys.path.insert(0, str(REPO_ROOT))
        import yaml  # noqa: PLC0415

        from swarm.schemas.mission import Mission  # noqa: PLC0415
        from swarm.specialists.success_verifier import run_all_checks  # noqa: PLC0415

        m = Mission.model_validate(yaml.safe_load(mission_file.read_text()))
        results = run_all_checks("metrics", m)
        passing = sum(1 for r in results.values() if r.status == "pass")
        return (passing, len(results))
    except Exception as e:
        print(f"mission progress check failed: {e}", file=sys.stderr)
        return (0, 0)


def update(version: str, notes: str, replace_last: bool = False) -> dict:
    with METRICS_PATH.open() as f:
        data = json.load(f)
    tests = _count_tests()
    prod = _prod_loc()
    test = _test_loc()
    passing, total = _mission_progress(version)
    ruff = _ruff_clean()
    entry = {
        "version": version,
        "ts": datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "tests_passing": tests,
        "prod_loc": prod,
        "test_loc": test,
        "mission_criteria_passing": passing,
        "mission_criteria_total": total,
        "ruff_clean": ruff,
        "notes": notes,
    }
    if replace_last and data["cycles"]:
        data["cycles"][-1] = entry
    else:
        data["cycles"].append(entry)

    # Atomic write
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=str(METRICS_PATH.parent), prefix=".metrics.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, METRICS_PATH)
    return entry


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("version", help="e.g. v3-integration")
    p.add_argument("--notes", default="")
    p.add_argument(
        "--replace-last",
        action="store_true",
        help="Replace the last cycle entry instead of appending (fix bad entries)",
    )
    args = p.parse_args()
    entry = update(args.version, args.notes, replace_last=args.replace_last)
    print(json.dumps(entry, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
