"""``verify_tamper`` Temporal activity — detect mission-file tampering.

Per spec §6.3 row ``verify_tamper``:

    verify_tamper(mission_dir, out_of_tree_sha_path) →
        {detected: bool, finding: dict | None}

    Short (seconds). Observational; safe to re-run.

Ported from ``specialists/success_verifier.py::verify_tamper`` (lines 117-164).
The detection semantics are preserved:

* The in-tree ``mission.lock.json`` pins a sha256 hash for every mission file.
* An out-of-tree sha file pins the sha256 of the lock file itself. This gives
  us a two-level anchor: an attacker who tampers with a mission file either
  has to also rewrite the lock (detected because the lock bytes would no
  longer hash to the out-of-tree sha) OR leave the lock alone (detected
  because the file hash would no longer match the pinned value).
* Missing artifacts — lock absent, out-of-tree sha absent, pinned file
  deleted — are all treated as tamper. The function fails closed.

Implementation contract (from plan Task 5):

* Signature: ``async def verify_tamper(mission_dir, out_of_tree_sha_path)
  -> TamperResult``.
* Pure, stdlib only. No Temporal primitives beyond ``@activity.defn``.
* Returns a ``TamperResult`` dataclass; never raises on normal tamper paths.
  Unexpected errors (e.g. permission denied on ``read_bytes``) propagate and
  are classified as transient by ``swarm.durable.retry_policies``.
* Unlike the original function, this activity is schema-free: the finding is
  a plain ``dict`` with the four spec fields (``type``, ``subtype``,
  ``severity``, ``verdict``). The workflow layer decides whether to wrap it
  in a ``Finding`` object, emit to ``findings.jsonl``, pause the mission,
  etc. Keeping this layer schema-free means this module does not need to
  import the legacy ``swarm.schemas.finding`` module during the migration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity


@dataclass
class TamperResult:
    """Outcome of one tamper check.

    ``detected=False, finding=None`` — clean, safe to proceed.
    ``detected=True, finding={...}`` — tamper observed; ``finding`` is a
    spec-shaped dict the caller can serialize straight into
    ``findings.jsonl`` or pass to the intervention judge.
    """

    detected: bool
    finding: dict | None


@activity.defn(name="verify_tamper")
async def verify_tamper(
    mission_dir: str, out_of_tree_sha_path: str
) -> TamperResult:
    """Check mission file hashes against ``mission.lock.json``.

    Returns ``TamperResult(False, None)`` iff:

    1. ``mission.lock.json`` exists,
    2. the out-of-tree sha file exists,
    3. sha256 of the lock bytes equals the out-of-tree sha,
    4. every file listed in ``lock["files"]`` exists and hashes to its pinned
       value.

    Any other outcome yields ``TamperResult(True, finding)`` with a verdict
    string naming the specific mismatch so operators can act without
    re-deriving the diff.
    """

    mdir = Path(mission_dir)
    lock_path = mdir / "mission.lock.json"
    sha_path = Path(out_of_tree_sha_path)

    # Fail-closed: any missing anchor is tamper. We check the lock first
    # because it's the more interesting signal — a missing out-of-tree sha
    # on its own could (rarely) be a cleanup bug, but a missing in-tree lock
    # means the mission was never locked or was actively tampered.
    if not lock_path.exists():
        return TamperResult(True, _finding(f"lock missing: {lock_path}"))

    if not sha_path.exists():
        return TamperResult(
            True, _finding(f"out-of-tree sha missing: {sha_path}")
        )

    lock_bytes = lock_path.read_bytes()
    expected_sha = sha_path.read_text().strip()
    actual_sha = hashlib.sha256(lock_bytes).hexdigest()
    if actual_sha != expected_sha:
        # Either the lock was rewritten (to legitimize a tampered file) or
        # the out-of-tree sha was replaced with something stale. Both are
        # the same severity from the mission's perspective.
        return TamperResult(True, _finding("lock hash mismatch"))

    try:
        lock = json.loads(lock_bytes)
    except json.JSONDecodeError:
        # The hashes matched but the content is unparseable — exotic, but
        # treat it as tamper because we can't verify files without the map.
        return TamperResult(True, _finding("lock is not valid JSON"))

    files = lock.get("files") or {}
    for rel_path, expected in files.items():
        fp = mdir / rel_path
        if not fp.exists():
            return TamperResult(
                True, _finding(f"pinned file missing: {rel_path}")
            )
        actual = hashlib.sha256(fp.read_bytes()).hexdigest()
        if actual != expected:
            return TamperResult(
                True, _finding(f"tampered file {rel_path}")
            )

    return TamperResult(False, None)


def _finding(reason: str) -> dict:
    """Build a spec §6.3-shaped finding dict for a tamper detection.

    Factored into a helper so the shape stays consistent across every return
    site; a future refactor that tightens the schema (e.g. adds a code) only
    has to touch one place.
    """

    return {
        "type": "meta",
        "subtype": "tamper_detected",
        "severity": "critical",
        "verdict": reason,
    }
