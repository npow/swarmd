"""spawner — admission control for heavy subagents.

Enforces the concurrency budgets declared in mission.yaml:
  - max_total_live: max concurrent live subagents
  - max_depth: deepest a subtree may recurse
  - max_fan_out_per_parent: max live children per single parent

When at budget, requests are QUEUED (never rejected). When max_depth is
exceeded, request is rejected with a drift finding (depth-exceeded is
almost always a bug — a recursive decomposition without a base case).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from swarm.lib.ids import mint_finding_id
from swarm.lib.locking import locked_rmw
from swarm.lib.paths import session_dir
from swarm.schemas.finding import Evidence, Finding
from swarm.schemas.mission import Concurrency

LOG = logging.getLogger("swarm.spawner")


class AdmissionResult(str, Enum):
    ADMIT = "admit"
    QUEUE = "queue"
    REJECT = "reject"


@dataclass
class SpawnRequest:
    parent_id: str
    depth: int
    mission: str
    context_summary: str = ""


@dataclass
class SpawnerState:
    """In-memory snapshot of the tree; persisted to tree.json."""

    nodes: dict[str, dict] = field(default_factory=dict)
    queue: list[dict] = field(default_factory=list)
    spawned_total: int = 0

    def live(self) -> int:
        return sum(1 for n in self.nodes.values() if n.get("status") == "running")

    def children_of(self, parent_id: str) -> list[dict]:
        return [
            n
            for n in self.nodes.values()
            if n.get("parent") == parent_id and n.get("status") == "running"
        ]


def tree_path(session_id: str) -> Path:
    return session_dir(session_id) / "tree.json"


def load_tree(session_id: str) -> SpawnerState:
    p = tree_path(session_id)
    if not p.exists():
        return SpawnerState()
    try:
        data = json.loads(p.read_text())
    except Exception:
        return SpawnerState()
    return SpawnerState(
        nodes=data.get("nodes", {}),
        queue=data.get("queue", []),
        spawned_total=data.get("spawned_total", 0),
    )


def save_tree(session_id: str, state: SpawnerState) -> None:
    p = tree_path(session_id)
    with locked_rmw(p, default=b"{}") as (fd, _data):
        import os as _os

        _os.write(
            fd,
            json.dumps(
                {
                    "nodes": state.nodes,
                    "queue": state.queue,
                    "spawned_total": state.spawned_total,
                }
            ).encode(),
        )


def admit_spawn(
    state: SpawnerState,
    request: SpawnRequest,
    concurrency: Concurrency,
) -> AdmissionResult:
    """Decide whether a spawn request is admitted, queued, or rejected.

    Rules:
      - depth > max_depth → REJECT (bug signal)
      - live count >= max_total_live → QUEUE
      - children_of(parent) >= max_fan_out_per_parent → QUEUE
      - otherwise → ADMIT
    """
    if request.depth > concurrency.max_depth:
        return AdmissionResult.REJECT
    if state.live() >= concurrency.max_total_live:
        return AdmissionResult.QUEUE
    if (
        len(state.children_of(request.parent_id))
        >= concurrency.max_fan_out_per_parent
    ):
        return AdmissionResult.QUEUE
    return AdmissionResult.ADMIT


def depth_exceeded_finding(
    session_id: str, request: SpawnRequest, concurrency: Concurrency
) -> Finding:
    return Finding(
        id=mint_finding_id(),
        source="spawner.depth_exceeded",
        subject_session=session_id,
        spawner_id=session_id,
        type="drift",
        subtype="recursion_no_base",
        severity="major",
        evidence=Evidence(
            claim_excerpt=(
                f"parent={request.parent_id} depth={request.depth} "
                f"max_depth={concurrency.max_depth}"
            ),
        ),
        verdict=(
            f"Spawn rejected at depth {request.depth} (max={concurrency.max_depth}). "
            "Likely an agent recursion without a base case."
        ),
    )


Spawner = Callable[[list[str], dict[str, str]], subprocess.Popen]


def default_spawner(argv: list[str], env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def register_spawn(
    session_id: str,
    state: SpawnerState,
    request: SpawnRequest,
    child_id: str,
    pid: int,
) -> SpawnerState:
    """Add a new child to the tree state."""
    state.nodes[child_id] = {
        "depth": request.depth,
        "pid": pid,
        "parent": request.parent_id,
        "mission": request.mission[:200],
        "status": "running",
        "spawned_at": time.time(),
    }
    state.spawned_total += 1
    save_tree(session_id, state)
    return state


def mark_dead(session_id: str, state: SpawnerState, child_id: str) -> SpawnerState:
    if child_id in state.nodes:
        state.nodes[child_id]["status"] = "dead"
        state.nodes[child_id]["dead_at"] = time.time()
        save_tree(session_id, state)
    return state


def enqueue(
    session_id: str, state: SpawnerState, request: SpawnRequest
) -> SpawnerState:
    state.queue.append(
        {
            "parent_id": request.parent_id,
            "depth": request.depth,
            "mission": request.mission[:200],
            "context_summary": request.context_summary[:400],
            "enqueued_at": time.time(),
        }
    )
    save_tree(session_id, state)
    return state


def drain_queue_one(
    session_id: str, state: SpawnerState, concurrency: Concurrency
) -> SpawnRequest | None:
    """Pop one queued request if admission would now succeed. Else return None."""
    for i, q in enumerate(state.queue):
        req = SpawnRequest(
            parent_id=q["parent_id"],
            depth=q["depth"],
            mission=q["mission"],
            context_summary=q.get("context_summary", ""),
        )
        if admit_spawn(state, req, concurrency) == AdmissionResult.ADMIT:
            state.queue.pop(i)
            save_tree(session_id, state)
            return req
    return None


def reap_zombies(session_id: str) -> int:
    """Reap any zombie child processes of the current process.

    Returns the count of children reaped. Also marks reaped children as `dead`
    in tree.json so the admission controller sees freed slots.

    Uses waitpid(-1, WNOHANG) in a loop until no more zombies are pending.
    Safe to call repeatedly; is a no-op if there are no zombies.
    """
    reaped = 0
    dead_pids: set[int] = set()
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            # No children to wait on
            break
        if pid == 0:
            # Some child exists but none ready to reap
            break
        reaped += 1
        dead_pids.add(pid)

    if dead_pids:
        state = load_tree(session_id)
        for child_id, node in list(state.nodes.items()):
            if int(node.get("pid", -1)) in dead_pids and node.get("status") == "running":
                state = mark_dead(session_id, state, child_id)
        LOG.info("reaped %d zombies: %s", reaped, dead_pids)
    return reaped


def run_daemon_once(
    session_id: str,
    concurrency: Concurrency,
    spawner: Spawner = default_spawner,
    *,
    claude_binary: str = "claude",
) -> int:
    """Drain one queued request and launch its subprocess. Returns 1 if
    something was admitted+launched, 0 otherwise.

    Designed to be called in a loop by a daemon wrapper; a test can call it
    once to verify behavior without an infinite loop.
    """
    import os as _os
    import uuid as _uuid

    state = load_tree(session_id)
    req = drain_queue_one(session_id, state, concurrency)
    if req is None:
        return 0
    child_id = _uuid.uuid4().hex[:12]
    argv = [
        claude_binary,
        "--session-id",
        session_id,
        f"{req.mission}\n[RECOVERY CONTEXT]: {req.context_summary}",
    ]
    env = _os.environ.copy()
    env["SESSION_ID"] = session_id
    try:
        proc = spawner(argv, env)
        register_spawn(session_id, state, req, child_id, pid=proc.pid)
        LOG.info("spawner launched child=%s pid=%s", child_id, proc.pid)
        return 1
    except Exception as e:
        LOG.error("spawner failed to launch %s: %s", child_id, e)
        return 0
