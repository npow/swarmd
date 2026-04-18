"""``read_recent_events`` Temporal activity — tail ``events.jsonl``.

Per plan Task 15 (PatternDetectorWorkflow):

    read_recent_events(session_id, offset) -> dict

    Read new rows from ``{state_dir}/events.jsonl`` starting at byte
    ``offset``. Returns ``{"events": [...], "next_offset": <int>}`` so the
    workflow can checkpoint progress without scanning the file again on
    subsequent cycles.

The workflow tracks ``next_offset`` in-memory between cycles; across a
``continue_as_new`` it is carried on the child's own state. This is the
standard ``tail -f``-style incremental read pattern, adapted to Temporal's
I/O contract (workflows can't do file I/O directly — activities must).

Design notes:

* Non-fatal on missing file — a mission that hasn't emitted any events
  yet returns an empty list. ``events.jsonl`` is created lazily by the
  hook that appends to it.
* Returns events as plain ``dict``s (not ``Event`` pydantic models) so
  the workflow sandbox doesn't need to import the schema (it can, but
  this keeps the activity/workflow boundary narrower).
* Truncations in progress (partial last line) are tolerated — we stop
  at the last newline-terminated row and return ``next_offset`` pointing
  at the start of the partial row so the next cycle retries it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from temporalio import activity


@activity.defn(name="read_recent_events")
async def read_recent_events(
    session_id: str, offset: int
) -> dict[str, Any]:
    """Read new events since ``offset`` bytes into ``events.jsonl``.

    Args:
        session_id: Session ID whose ``events.jsonl`` to read (passed to
            ``swarm.lib.paths.events_path``).
        offset: Byte offset to seek to before reading. 0 = whole file.

    Returns:
        ``{"events": list[dict], "next_offset": int}``. ``events`` is a
        list of parsed JSON objects (one per JSONL row). ``next_offset``
        is the new byte position after the last complete line read — pass
        it back on the next call to resume.

    Never raises for the common cases (missing file, empty file, partial
    last line). JSON parse errors on a single row are skipped silently —
    the event scribe occasionally writes a malformed row during a crash
    and we don't want pattern detection to die for that.
    """
    # Lazy import so the activity module stays cheap to import at
    # worker-registration time. ``swarm.lib.paths`` resolves SWARM_ROOT
    # which may touch env vars; deferring the import also means tests
    # that monkeypatch ``SWARM_ROOT`` can do so before this function runs.
    from swarm.lib.paths import events_path

    path: Path = events_path(session_id)
    if not path.exists():
        return {"events": [], "next_offset": offset}

    events: list[dict[str, Any]] = []
    try:
        size = path.stat().st_size
    except OSError:
        return {"events": [], "next_offset": offset}

    # The file may have been truncated below the previous offset — e.g.
    # an operator deleted + recreated events.jsonl. In that case reset to
    # byte 0 and re-scan.
    if offset > size:
        offset = 0

    new_offset = offset
    with path.open("rb") as f:
        f.seek(offset)
        # Read all remaining bytes. ``events.jsonl`` is bounded in
        # practice by the PatternDetectorWorkflow's cadence — we only
        # read the delta since the last poll, which is small. Reading in
        # one syscall keeps latency predictable.
        data = f.read()

    # Split on newlines. If the last byte is not ``\n`` the final line is
    # partial (a write is in flight); drop it and leave the offset at the
    # end of the last COMPLETE line so the next call retries.
    if not data:
        return {"events": [], "next_offset": offset}

    # ``splitlines(keepends=False)`` would also work but we need the byte
    # length of the complete portion to compute the new offset. Iterate
    # over the raw bytes so we can compute offset accurately.
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    # The last element of split("\n") is either "" (file ended in \n,
    # clean boundary) or a partial final line (write in progress).
    has_trailing_newline = text.endswith("\n")
    complete_lines = lines[:-1] if not has_trailing_newline else lines[:-1]
    # If the trailing element is empty (clean boundary), lines[:-1] is
    # all complete lines; otherwise the last element is a partial line
    # we must exclude. Both branches produce the same slice `lines[:-1]`
    # because a clean-boundary split appends "" — included above for
    # clarity.
    for row in complete_lines:
        if not row:
            continue
        try:
            events.append(json.loads(row))
        except json.JSONDecodeError:
            # A malformed row — skip silently; see module docstring.
            continue

    # Compute the byte offset of the end of the last complete line.
    # ``\n`` is 1 byte so we sum encoded lengths. Using len(s.encode())
    # is robust to multi-byte characters (UTF-8 can expand a codepoint
    # to up to 4 bytes).
    consumed_bytes = 0
    for row in complete_lines:
        consumed_bytes += len(row.encode("utf-8")) + 1  # +1 for \n
    new_offset = offset + consumed_bytes

    return {"events": events, "next_offset": new_offset}
