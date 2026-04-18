#!/usr/bin/env python3
"""Helper invoked by launch.sh — keeps shell free of inline Python heredocs.

Subcommands:
  workspace <mission.yaml>             # print mission.workspace
  prose <mission.yaml>                 # print mission.mission (the prose)
  hash-pin <session_id> <mission_dir> <out_sha_path>
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = os.environ.get("REPO_ROOT")
if REPO_ROOT:
    sys.path.insert(0, REPO_ROOT)

import yaml  # noqa: E402

from swarm.schemas.mission import Mission  # noqa: E402


def cmd_workspace(args: list[str]) -> int:
    mission_yaml = Path(args[0])
    data = yaml.safe_load(mission_yaml.read_text())
    m = Mission.model_validate(data)
    print(m.workspace)
    return 0


def cmd_prose(args: list[str]) -> int:
    mission_yaml = Path(args[0])
    data = yaml.safe_load(mission_yaml.read_text())
    m = Mission.model_validate(data)
    print(m.mission)
    return 0


def cmd_hash_pin(args: list[str]) -> int:
    session_id, mission_dir, out_sha_path = args
    root = Path(mission_dir)
    files: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "mission.lock.json":
            rel = str(p.relative_to(root))
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            files[rel] = f"sha256:{h}"
    lock = {
        "session_id": session_id,
        "locked_at": datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "files": files,
        "baseline": {"test_count": 0, "assertion_counts": {}},
    }
    (root / "mission.lock.json").write_text(json.dumps(lock, indent=2))
    Path(out_sha_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_sha_path).write_text(json.dumps(files, sort_keys=True))
    print(f"hash-pinned {len(files)} files")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _launch_helper.py <subcommand> [args...]", file=sys.stderr)
        return 2
    cmd, args = sys.argv[1], sys.argv[2:]
    handlers = {
        "workspace": cmd_workspace,
        "prose": cmd_prose,
        "hash-pin": cmd_hash_pin,
    }
    if cmd not in handlers:
        print(f"unknown subcommand: {cmd}", file=sys.stderr)
        return 2
    return handlers[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
