"""Atomic locked_rmw tests — verify the file is never seen empty."""

from __future__ import annotations

import json
import multiprocessing
import os

from swarmd.lib.locking import locked_rmw


def test_basic_rmw_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    with locked_rmw(p) as (fd, data):
        assert data == b"{}"
        os.write(fd, b'{"x": 1}')
    assert json.loads(p.read_text()) == {"x": 1}

    with locked_rmw(p) as (fd, data):
        assert json.loads(data.decode()) == {"x": 1}
        os.write(fd, b'{"x": 2}')
    assert json.loads(p.read_text()) == {"x": 2}


def test_existing_content_returned(tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"keep": true}')
    with locked_rmw(p) as (fd, data):
        assert json.loads(data.decode()) == {"keep": True}
        os.write(fd, b'{"keep": false}')
    assert json.loads(p.read_text()) == {"keep": False}


def _crash_simulator(path: str, q):
    """Open the rmw context, write garbage, then kill ourselves before commit."""
    from swarmd.lib.locking import locked_rmw as _l

    try:
        with _l(__import__("pathlib").Path(path)) as (fd, data):
            os.write(fd, b'{"INCOMPLETE')
            # SIGKILL ourselves before exiting the context manager
            os.kill(os.getpid(), 9)
    except Exception as e:
        q.put(f"err: {e}")


def test_crash_does_not_corrupt(tmp_path):
    """If the writer is SIGKILL'd mid-write, the original file content is preserved."""
    p = tmp_path / "strikes.json"
    original = '{"sig1": 5, "sig2": 3}'
    p.write_text(original)

    q: multiprocessing.Queue = multiprocessing.Queue()
    proc = multiprocessing.Process(target=_crash_simulator, args=(str(p), q))
    proc.start()
    proc.join(timeout=5)
    # Process was killed, exit code is -9 on Unix
    assert proc.exitcode == -9 or proc.exitcode is None
    # File should still be readable and intact
    assert p.exists()
    assert json.loads(p.read_text()) == json.loads(original)
    # Sentinel temp files may exist; cleanup is best-effort
    for f in tmp_path.iterdir():
        if f.name.startswith(".rmw."):
            f.unlink()
