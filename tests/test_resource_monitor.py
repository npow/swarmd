"""Tests for resource_monitor."""

from __future__ import annotations

from swarm.specialists.resource_monitor import (
    FD_CRIT_RATIO,
    FD_WARN_RATIO,
    ResourceSnapshot,
    check_resources,
    evaluate,
)


def _snap(
    fd=100, limit=1024, zombies=0, pids=None, live=0, rss=0.0, disk=0.0
) -> ResourceSnapshot:
    return ResourceSnapshot(
        fd_count=fd,
        fd_limit=limit,
        zombie_count=zombies,
        tracked_pids=pids or [],
        live_tracked=live,
        rss_mb=rss,
        state_disk_mb=disk,
    )


def test_healthy_snapshot_no_findings():
    out = evaluate(_snap(fd=10, limit=1024, zombies=0), "abcdef012345")
    assert out == []


def test_fd_warning_at_threshold():
    limit = 1024
    fd = int(limit * (FD_WARN_RATIO + 0.01))
    out = evaluate(_snap(fd=fd, limit=limit), "abcdef012345")
    assert len(out) == 1
    assert out[0].subtype == "fd_warning"
    assert out[0].severity == "major"


def test_fd_critical_at_threshold():
    limit = 1024
    fd = int(limit * (FD_CRIT_RATIO + 0.01))
    out = evaluate(_snap(fd=fd, limit=limit), "abcdef012345")
    assert len(out) == 1
    assert out[0].subtype == "fd_exhaustion"
    assert out[0].severity == "critical"


def test_zombie_warning():
    out = evaluate(_snap(zombies=10), "abcdef012345")
    assert len(out) == 1
    assert out[0].subtype == "zombies"


def test_zombie_critical():
    out = evaluate(_snap(zombies=50), "abcdef012345")
    assert len(out) == 1
    assert out[0].subtype == "zombie_flood"
    assert out[0].severity == "critical"


def test_rss_warning():
    out = evaluate(_snap(rss=3000.0), "abcdef012345")
    assert any(f.subtype == "memory_warning" for f in out)


def test_rss_critical():
    out = evaluate(_snap(rss=6000.0), "abcdef012345")
    assert any(f.subtype == "memory_pressure" for f in out)


def test_disk_warning():
    out = evaluate(_snap(disk=200.0), "abcdef012345")
    assert any(f.subtype == "state_disk_warning" for f in out)


def test_disk_critical():
    out = evaluate(_snap(disk=800.0), "abcdef012345")
    assert any(f.subtype == "state_disk_full" for f in out)


def test_multiple_thresholds_multiple_findings():
    out = evaluate(_snap(zombies=50, rss=7000.0, disk=600.0), "abcdef012345")
    subtypes = {f.subtype for f in out}
    assert "zombie_flood" in subtypes
    assert "memory_pressure" in subtypes
    assert "state_disk_full" in subtypes


def test_check_resources_does_not_crash(tmp_swarm_root, session_id):
    """End-to-end: check_resources runs on a real session with no crash."""
    out = check_resources(session_id)
    # May be empty or contain findings — just assert no exception
    assert isinstance(out, list)


def test_snapshot_is_frozen():
    s = _snap()
    try:
        s.fd_count = 999  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("ResourceSnapshot should be frozen")
