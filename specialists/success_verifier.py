"""Success verifier — runs mission checks, emits pass/fail findings.

In v0:
- Runs each `check:` in a clean subprocess (env -i + fixed PATH).
- Hash-pins mission files and the out-of-tree lock before each run.
- Emits `pass_new` transitions for anticheat triggering.
- Enforces invariants: test_count_floor (if set), no_mock (path grep).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from swarm.lib.hashing import sha256_file
from swarm.lib.heartbeat import beat
from swarm.lib.ids import mint_finding_id
from swarm.lib.launcher_liveness import exit_if_launcher_dead
from swarm.lib.locking import write_line
from swarm.lib.paths import (
    ensure_session_dirs,
    findings_path,
    mission_dir,
    mission_lock_path,
    mission_yaml_path,
    out_of_tree_lock_path,
    session_dir,
)
from swarm.schemas.finding import Evidence, Finding
from swarm.schemas.lock import MissionLock
from swarm.schemas.mission import Mission, SuccessCriterion

LOG = logging.getLogger("swarm.success_verifier")


@dataclass
class CheckResult:
    id: str
    status: str  # "pass" | "fail"
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


def run_check(
    criterion: SuccessCriterion,
    workspace: str,
    *,
    path_add: list[str] | None = None,
    env_passthrough: list[str] | None = None,
) -> CheckResult:
    """Run one check in a clean subprocess.

    The default env is `env -i` except for PATH, which is set to
    `/usr/local/bin:/usr/bin:/bin`. Mission authors can extend PATH via
    path_add and pass through specific env-var NAMES (not values) via
    env_passthrough — values are read from the parent env at invocation time.
    This resists PATH hijacking while allowing the verifier to find tools
    installed in virtualenvs / conda envs / ~/.local/bin.
    """
    t0 = time.monotonic()
    base_path = "/usr/local/bin:/usr/bin:/bin"
    if path_add:
        # Prepend mission-declared paths, then the base path (defense-in-depth:
        # if a mission-declared path goes away, /usr/bin is still there)
        path = ":".join([*path_add, base_path])
    else:
        path = base_path
    env: dict[str, str] = {"PATH": path}
    if env_passthrough:
        for name in env_passthrough:
            if name in ("PATH",):
                continue  # PATH is handled above
            val = os.environ.get(name)
            if val is not None:
                env[name] = val
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", criterion.check],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=criterion.timeout_sec,
        )
        status = "pass" if proc.returncode == 0 else "fail"
        return CheckResult(
            id=criterion.id,
            status=status,
            exit_code=proc.returncode,
            stdout=proc.stdout[-4000:],
            stderr=proc.stderr[-4000:],
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
    except subprocess.TimeoutExpired as e:
        return CheckResult(
            id=criterion.id,
            status="fail",
            exit_code=-1,
            stdout=(e.stdout or "")[-4000:] if isinstance(e.stdout, str) else "",
            stderr=f"TIMEOUT after {criterion.timeout_sec}s",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


def verify_tamper(session_id: str) -> Finding | None:
    """Check mission file hashes against the lock. Return a finding if mismatch."""
    lock_path = mission_lock_path(session_id)
    out_sha = out_of_tree_lock_path(session_id)
    if not lock_path.exists() or not out_sha.exists():
        return None
    lock = MissionLock.model_validate_json(lock_path.read_text())
    # Verify out-of-tree sha matches what's in lock (cross-verification)
    expected_out = out_sha.read_text().strip()
    if expected_out != json.dumps(lock.files, sort_keys=True):
        return Finding(
            id=mint_finding_id(),
            source="success_verifier.tamper",
            subject_session=session_id,
            spawner_id=session_id,
            type="meta",
            subtype="tamper_detected",
            severity="critical",
            verdict="Out-of-tree hash does not match mission.lock.json",
        )
    for rel_path, expected in lock.files.items():
        full = mission_dir(session_id) / rel_path
        if not full.exists():
            return Finding(
                id=mint_finding_id(),
                source="success_verifier.tamper",
                subject_session=session_id,
                spawner_id=session_id,
                type="meta",
                subtype="tamper_detected",
                severity="critical",
                evidence=Evidence(files=[rel_path]),
                verdict=f"{rel_path} missing",
            )
        actual = sha256_file(full)
        if actual != expected:
            return Finding(
                id=mint_finding_id(),
                source="success_verifier.tamper",
                subject_session=session_id,
                spawner_id=session_id,
                type="meta",
                subtype="tamper_detected",
                severity="critical",
                evidence=Evidence(files=[rel_path]),
                verdict=f"{rel_path} hash mismatch",
            )
    return None


def enforce_invariants(mission: Mission) -> list[Finding]:
    out: list[Finding] = []
    workspace = Path(mission.workspace)
    inv = mission.invariants
    # no_mock enforcement
    for protected in inv.no_mock:
        protected_path = (workspace / protected).resolve()
        if not protected_path.exists():
            continue
        for py in protected_path.rglob("*.py"):
            try:
                text = py.read_text(errors="ignore")
            except OSError:
                continue
            if re.search(r"\b(unittest\.mock|MagicMock|Mock\(|patch\()", text):
                out.append(
                    Finding(
                        id=mint_finding_id(),
                        source="success_verifier.invariant",
                        subject_session="",  # filled by caller
                        spawner_id="",
                        type="cheat",
                        subtype="mock_out",
                        severity="critical",
                        evidence=Evidence(
                            files=[str(py.relative_to(workspace))],
                            claim_excerpt="unittest.mock usage in no_mock path",
                        ),
                        verdict=f"mock usage found in protected path: {py}",
                    )
                )
    # test_count_floor
    if inv.test_count_floor is not None:
        count = _count_tests(workspace)
        if count < inv.test_count_floor:
            out.append(
                Finding(
                    id=mint_finding_id(),
                    source="success_verifier.invariant",
                    subject_session="",
                    spawner_id="",
                    type="cheat",
                    subtype="scope_reduction",
                    severity="critical",
                    verdict=(
                        f"test count {count} dropped below floor {inv.test_count_floor}"
                    ),
                )
            )
    # assertion_count_floor — per-file floor on number of `assert` statements
    for rel_path, floor in (inv.assertion_count_floor or {}).items():
        full = (workspace / rel_path).resolve()
        if not full.exists():
            out.append(
                Finding(
                    id=mint_finding_id(),
                    source="success_verifier.invariant",
                    subject_session="",
                    spawner_id="",
                    type="cheat",
                    subtype="scope_reduction",
                    severity="critical",
                    evidence=Evidence(files=[rel_path]),
                    verdict=f"{rel_path} missing — protected by assertion_count_floor",
                )
            )
            continue
        try:
            text = full.read_text(errors="ignore")
        except OSError:
            continue
        # Count `assert` keywords + `self.assert*(` method calls (Python conventions)
        count = len(
            re.findall(r"^\s*assert\b", text, flags=re.MULTILINE)
        ) + len(re.findall(r"\bself\.assert\w+\s*\(", text))
        if count < floor:
            out.append(
                Finding(
                    id=mint_finding_id(),
                    source="success_verifier.invariant",
                    subject_session="",
                    spawner_id="",
                    type="cheat",
                    subtype="scope_reduction",
                    severity="critical",
                    evidence=Evidence(files=[rel_path]),
                    verdict=(
                        f"{rel_path}: assertion count {count} below floor {floor}"
                    ),
                )
            )
    # allowed_deps — installed pip packages must subset the allowlist
    if inv.allowed_deps:
        violations = _check_allowed_deps(workspace, inv.allowed_deps)
        for v in violations:
            out.append(
                Finding(
                    id=mint_finding_id(),
                    source="success_verifier.invariant",
                    subject_session="",
                    spawner_id="",
                    type="cheat",
                    subtype="dep_violation",
                    severity="critical",
                    verdict=v,
                )
            )
    return out


def _check_allowed_deps(workspace: Path, allowed: list[str]) -> list[str]:
    """Return human-readable strings describing each disallowed installed package.

    For v0 we run `pip freeze` in a clean subprocess and compare. Empty list = no
    violations.
    """
    try:
        proc = subprocess.run(
            ["python3", "-m", "pip", "freeze"],
            cwd=str(workspace),
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    # Parse "package==version" lines from pip freeze
    installed: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or " " in line:
            continue
        if "==" in line:
            name, _, version = line.partition("==")
            installed[name.lower().replace("_", "-")] = version
    # Build allowed_names set (just names, version constraints not enforced in v0)
    allowed_names = set()
    for spec in allowed:
        # Strip version constraints to get bare name
        bare = re.split(r"[<>=!~\[]", spec, 1)[0].strip().lower().replace("_", "-")
        if bare:
            allowed_names.add(bare)
    violations: list[str] = []
    for name in sorted(installed):
        if name not in allowed_names:
            violations.append(
                f"installed package not in allowed_deps: {name}=={installed[name]}"
            )
    return violations


def _count_tests(workspace: Path) -> int:
    count = 0
    for p in workspace.rglob("test_*.py"):
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        count += len(re.findall(r"^\s*def\s+test_\w+\(", text, flags=re.MULTILINE))
    return count


def run_all_checks(session_id: str, mission: Mission) -> dict[str, CheckResult]:
    """Run every criterion atomically. Return {id: result}."""
    path_add = list(mission.verification.path_add)
    env_pt = list(mission.verification.env_passthrough)
    return {
        c.id: run_check(
            c,
            mission.workspace,
            path_add=path_add,
            env_passthrough=env_pt,
        )
        for c in mission.success_criteria
    }


# -------- daemon --------


def _status_path(session_id: str) -> Path:
    return session_dir(session_id) / "verifier_status.json"


def _load_mission(session_id: str) -> Mission:
    raw = yaml.safe_load(mission_yaml_path(session_id).read_text())
    return Mission.model_validate(raw)


def main(session_id: str) -> None:
    ensure_session_dirs(session_id)
    exit_if_launcher_dead(session_id, LOG)
    mission = _load_mission(session_id)
    prev_results: dict[str, str] = {}  # id -> "pass"|"fail"
    all_pass_since: float | None = None
    hold_window_emitted: bool = False
    invariant_sigs_emitted: set[str] = set()  # dedup invariant findings by signature
    cycles = 0

    while True:
        exit_if_launcher_dead(session_id, LOG)
        # 1. Tamper check
        tamper = verify_tamper(session_id)
        if tamper:
            write_line(findings_path(session_id), tamper.model_dump_json())
            time.sleep(30)
            continue

        # 2. Invariants (dedup: same signature only emitted once per session)
        for f in enforce_invariants(mission):
            sig = f"{f.subtype}|{'|'.join(f.evidence.files)}|{f.verdict[:200]}"
            if sig in invariant_sigs_emitted:
                continue
            invariant_sigs_emitted.add(sig)
            f = f.model_copy(update={"subject_session": session_id, "spawner_id": session_id})
            write_line(findings_path(session_id), f.model_dump_json())

        # 3. Checks (atomic snapshot)
        results = run_all_checks(session_id, mission)
        all_pass = all(r.status == "pass" for r in results.values())

        # 4. Emit pass-transition findings
        for cid, r in results.items():
            prev = prev_results.get(cid, "fail")
            if prev == "fail" and r.status == "pass":
                f = Finding(
                    id=mint_finding_id(),
                    source="success_verifier.transition",
                    subject_session=session_id,
                    spawner_id=session_id,
                    type="verification",
                    subtype="pass_transition",
                    severity="major",
                    evidence=Evidence(
                        claim_excerpt=f"criterion={cid} exit=0 stdout_tail={r.stdout[-400:]}"
                    ),
                    verdict=f"{cid} transitioned fail→pass",
                )
                write_line(findings_path(session_id), f.model_dump_json())
            prev_results[cid] = r.status

        # 5. Emit overall status
        status_row = {
            "ts": time.time(),
            "all_pass": all_pass,
            "per_criterion": {
                cid: {"status": r.status, "exit_code": r.exit_code}
                for cid, r in results.items()
            },
        }
        _status_path(session_id).write_text(json.dumps(status_row, indent=2))

        # 6. Track hold window (emit hold_window_met ONCE per achievement)
        if all_pass:
            if all_pass_since is None:
                all_pass_since = time.time()
            held_sec = time.time() - all_pass_since
            if held_sec >= mission.verification.hold_window_sec and not hold_window_emitted:
                f = Finding(
                    id=mint_finding_id(),
                    source="success_verifier.hold_window_met",
                    subject_session=session_id,
                    spawner_id=session_id,
                    type="verification",
                    subtype="hold_window_met",
                    severity="major",
                    verdict=f"all criteria held for {int(held_sec)}s",
                )
                write_line(findings_path(session_id), f.model_dump_json())
                hold_window_emitted = True
        else:
            all_pass_since = None
            hold_window_emitted = False

        cycles += 1
        beat(session_id, "success_verifier", cycles)

        # Randomized cadence
        jitter = random.uniform(0.75, 1.25)
        time.sleep(mission.verification.run_every_sec * jitter)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: success_verifier.py <session_id>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
