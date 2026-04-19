"""``emit_finding`` Temporal activity — persist a finding to disk.

Per spec §6.3 row ``emit_finding``:

    emit_finding(session_state_dir, finding) → None

    Append ``finding`` as a JSONL line to
    ``{session_state_dir}/findings.jsonl``. Intervention-typed findings also
    mirror to ``interventions.jsonl`` so existing hook-based consumers
    (``on_stop.py``, ``on_session_start.py``) keep working unchanged while the
    rest of the system migrates onto Temporal workflow signals.

Design notes:

* Enrichment happens on a copy — the caller's dict is never mutated. The
  workflow keeps the original in its persisted state and must not see
  side-effects from an activity execution (replay guarantee).
* The directory is created on first write (``parents=True``). Missions can
  emit findings before the state dir has been physically materialized.
* Writes use ``"a"`` mode and a single ``json.dumps + "\\n"``. Small JSONL
  writes are atomic at the OS level on POSIX, which is enough for the
  single-worker-at-a-time invariant the mission workflow preserves.
* The file helper is module-level so tests can swap it out if needed and the
  activity body stays thin.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from temporalio import activity


@activity.defn(name="emit_finding")
async def emit_finding(session_state_dir: str, finding: dict) -> None:
    """Append ``finding`` to ``findings.jsonl`` (and, for intervention-typed
    findings, to ``interventions.jsonl``) under ``session_state_dir``.

    Creates the directory if it does not yet exist. Never raises on
    already-existing paths. The returned ``None`` mirrors the spec: the
    activity is fire-and-forget from the workflow's perspective — retries
    on transient I/O failures are handled by the retry policy at the
    workflow call-site.
    """
    d = Path(session_state_dir)
    d.mkdir(parents=True, exist_ok=True)

    enriched = {**finding, "emitted_at": time.time()}
    _append_jsonl(d / "findings.jsonl", enriched)

    if finding.get("type") == "intervention":
        _append_jsonl(d / "interventions.jsonl", enriched)


def _append_jsonl(p: Path, obj: dict) -> None:
    """Append one JSON-encoded object + newline to ``p``.

    Factored out so the activity body stays declarative and so the two write
    sites (findings / interventions) share one well-tested helper.
    """
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
