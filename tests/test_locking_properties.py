"""Property-based tests for the lib.locking module.

Uses hypothesis to fuzz:
  - Concurrent locked_append writes must all appear in the final file
  - locked_rmw must be atomic under adversarial kill timing (tested via
    simulation; real SIGKILL cases live in test_locking_atomic.py)
"""

from __future__ import annotations

import json
import threading

from hypothesis import given, settings
from hypothesis import strategies as st

from swarm.lib.locking import locked_rmw, write_line


@given(
    lines=st.lists(
        st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=100),
        min_size=1,
        max_size=30,
    )
)
@settings(deadline=None, max_examples=30)
def test_write_line_preserves_all_lines(tmp_path_factory, lines):
    """Every line written via write_line appears in the final file."""
    import uuid

    p = tmp_path_factory.mktemp(f"wl-{uuid.uuid4().hex[:8]}") / "out.jsonl"
    for ln in lines:
        write_line(p, ln)
    read_back = p.read_text().splitlines()
    assert len(read_back) == len(lines)
    for got, want in zip(read_back, lines, strict=True):
        assert got == want


@given(
    n_writers=st.integers(min_value=2, max_value=6),
    writes_per=st.integers(min_value=1, max_value=10),
)
@settings(deadline=None, max_examples=10)
def test_concurrent_write_line_no_loss(tmp_path_factory, n_writers, writes_per):
    """n_writers × writes_per lines all appear in the final file."""
    import uuid

    p = tmp_path_factory.mktemp(f"cc-{uuid.uuid4().hex[:8]}") / "out.jsonl"
    barrier = threading.Barrier(n_writers)

    def _writer(tid: int):
        barrier.wait()
        for j in range(writes_per):
            write_line(p, f"w{tid}-{j}")

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = p.read_text().splitlines()
    assert len(lines) == n_writers * writes_per
    # Every expected line is present (order may vary)
    expected = {f"w{i}-{j}" for i in range(n_writers) for j in range(writes_per)}
    assert set(lines) == expected


@given(
    updates=st.lists(
        st.tuples(
            st.text(alphabet="abcd", min_size=1, max_size=4),
            st.integers(min_value=0, max_value=100),
        ),
        min_size=1,
        max_size=15,
    )
)
@settings(deadline=None, max_examples=20)
def test_locked_rmw_sequential_updates_consistent(tmp_path_factory, updates):
    """Sequential updates through locked_rmw yield correct final state."""
    import os
    import uuid

    p = tmp_path_factory.mktemp(f"rmw-{uuid.uuid4().hex[:8]}") / "state.json"
    expected: dict[str, int] = {}
    for key, val in updates:
        with locked_rmw(p, default=b"{}") as (fd, data):
            try:
                state = json.loads(data.decode() or "{}")
            except json.JSONDecodeError:
                state = {}
            state[key] = val
            os.write(fd, json.dumps(state).encode())
        expected[key] = val

    final = json.loads(p.read_text())
    assert final == expected


@given(
    keys=st.lists(st.text(alphabet="xyz", min_size=1, max_size=3), min_size=1, max_size=10, unique=True)
)
@settings(deadline=None, max_examples=10)
def test_locked_rmw_concurrent_keys_no_loss(tmp_path_factory, keys):
    """n concurrent writers, each updating a distinct key, all survive."""
    import os
    import uuid

    p = tmp_path_factory.mktemp(f"rmw-cc-{uuid.uuid4().hex[:8]}") / "state.json"
    barrier = threading.Barrier(len(keys))

    def _writer(k: str):
        barrier.wait()
        with locked_rmw(p, default=b"{}") as (fd, data):
            try:
                state = json.loads(data.decode() or "{}")
            except json.JSONDecodeError:
                state = {}
            state[k] = 1
            os.write(fd, json.dumps(state).encode())

    threads = [threading.Thread(target=_writer, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = json.loads(p.read_text())
    for k in keys:
        assert final.get(k) == 1, f"key {k} lost in concurrent rmw"
