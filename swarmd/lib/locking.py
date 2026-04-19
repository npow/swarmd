"""File-locking helpers via fcntl."""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def locked_append(path: Path) -> Iterator[int]:
    """
    Open `path` with O_APPEND, take an exclusive flock, yield the fd, release on exit.

    Use for appending one or more lines atomically under concurrent writers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write_line(path: Path, line: str) -> None:
    """Append a newline-terminated line to `path` under exclusive lock."""
    if not line.endswith("\n"):
        line += "\n"
    with locked_append(path) as fd:
        os.write(fd, line.encode("utf-8"))


@contextlib.contextmanager
def locked_rmw(path: Path, default: bytes = b"{}") -> Iterator[tuple[int, bytes]]:
    """
    Read-modify-write lock for small JSON files. Crash-safe AND concurrent-safe:
    writes go to a temp file under an exclusive lock, then atomic rename.
    A SIGKILL mid-write cannot leave the target file empty.

    Yields (fd, current_bytes). The caller writes new content to `fd` (which
    points at a fresh temp file). On normal exit, the temp file is fsynced
    and renamed over `path` atomically. On exception, the temp file is
    discarded.

    Lock semantics: the exclusive flock is held on a SEPARATE sentinel file
    (`{path}.lock`) that is never renamed, so concurrent writers serialize
    correctly even when the data file is replaced via rename. If we locked
    the data file directly, a rename would invalidate a waiting thread's
    open fd — we would release a lock on a dead inode while the next thread
    thinks it acquired a valid lock. Separating the lock file fixes this.
    """
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        # Read existing bytes from the current (possibly-renamed) data file
        try:
            data = path.read_bytes() or default
        except (FileNotFoundError, OSError):
            data = default
        # Write to a temp file and atomic-rename over data file
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=".rmw.", dir=str(path.parent))
        try:
            yield tmp_fd, data
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            tmp_fd = -1
            os.replace(tmp_name, str(path))
            tmp_name = ""
        finally:
            if tmp_fd >= 0:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
