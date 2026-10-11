"""VersionStore mechanics (WP1 R1b/R1c publish model). OCP-free, fault-injected."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import versions as V  # noqa: E402


def _seal(store, rid, revision, figs=("fig1",)):
    vd = store.begin(rid)
    (vd / "fig1.dxf").write_text("dxf-" + rid, encoding="utf-8")
    store.write_result(rid, {"figures": [{"id": f, "pass": True} for f in figs], "ok": True})
    store.seal(rid, {"figures": [{"id": f, "pass": True} for f in figs], "ok": True}, revision)
    return vd


def test_seal_complete_and_verify(tmp_path):
    s = V.VersionStore(tmp_path)
    assert not s.is_complete("r1")
    _seal(s, "r1", 1)
    assert s.is_complete("r1") and s.verify("r1")
    assert s.read_result("r1")["ok"] is True


def test_verify_detects_truncation(tmp_path):
    s = V.VersionStore(tmp_path)
    _seal(s, "r1", 1)
    (s.version_dir("r1") / "fig1.dxf").write_text("", encoding="utf-8")  # truncate
    assert not s.verify("r1")


def test_publish_points_current_and_syncs_out(tmp_path):
    s = V.VersionStore(tmp_path)
    _seal(s, "r1", 1)
    with V.state_lock(tmp_path):
        assert s.publish("r1", 1) is True
    assert s.current_id() == "r1" and s.current_dir() == s.version_dir("r1")
    assert (s.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf-r1"
    assert s.current_result()["ok"] is True


def test_publish_refuses_incomplete_version(tmp_path):
    s = V.VersionStore(tmp_path)
    s.begin("r1")
    with pytest.raises(ValueError):
        with V.state_lock(tmp_path):
            s.publish("r1", 1)
    assert s.current_id() is None


def test_monotonic_revision_refuses_stale(tmp_path):
    """R1c #3: a later-finishing OLDER revision must not overwrite a newer current version."""
    s = V.VersionStore(tmp_path)
    _seal(s, "rB", 2)
    _seal(s, "rA", 1)                       # older revision, finishes later
    with V.state_lock(tmp_path):
        assert s.publish("rB", 2) is True
        assert s.publish("rA", 1) is False  # refused as stale
    assert s.current_id() == "rB"
    assert (s.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf-rB"


def test_next_revision_is_monotonic(tmp_path):
    s = V.VersionStore(tmp_path)
    assert [s.next_revision() for _ in range(3)] == [1, 2, 3]


def test_view_refresh_failure_does_not_uncommit_and_recovers(tmp_path, monkeypatch):
    """R1d #3: the pointer commit and the out/ view refresh are separate — a view failure
    (e.g. ENOSPC during the swap) must NOT un-commit; it records a dirty marker and is retried
    on the next read / at startup recovery."""
    s = V.VersionStore(tmp_path)
    _seal(s, "r1", 1)
    monkeypatch.setattr(s, "sync_out", lambda rid: (_ for _ in ()).throw(OSError("ENOSPC")))
    with V.state_lock(tmp_path):
        became = s.commit_pointer("r1", 1)     # pointer commit — does NOT touch the view
    assert became is True
    assert s.refresh_view() is False           # view swap fails → dirty, but no exception
    # the commit stands: current still names r1, and the dirty marker is recorded
    assert s.current_id() == "r1" and s.current_dir() == s.version_dir("r1")
    assert s._view_dirty_file.is_file()
    # a fresh store (restart) retries the view and repairs out
    monkeypatch.undo()
    s2 = V.VersionStore(tmp_path)
    assert s2.recover() == []
    assert (s2.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf-r1"
    assert not s2._view_dirty_file.exists()


def test_sync_out_handles_legacy_real_directory(tmp_path):
    s = V.VersionStore(tmp_path)
    s.out.mkdir()
    (s.out / "stale.dxf").write_text("old", encoding="utf-8")
    _seal(s, "r1", 1)
    with V.state_lock(tmp_path):
        s.publish("r1", 1)
    assert (s.out / "fig1.dxf").is_file() and not (s.out / "stale.dxf").exists()


def test_copy_fallback_when_symlink_unavailable(tmp_path, monkeypatch):
    """Windows path: symlink raises → copy swap still produces a correct out view."""
    s = V.VersionStore(tmp_path)
    _seal(s, "r1", 1)
    monkeypatch.setattr(s, "_symlink_swap",
                        lambda target: (_ for _ in ()).throw(OSError("no symlink")))
    with V.state_lock(tmp_path):
        s.publish("r1", 1)
    assert not s.out.is_symlink()
    assert (s.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf-r1"


def test_recover_reports_incomplete_versions(tmp_path):
    s = V.VersionStore(tmp_path)
    _seal(s, "done", 1)
    s.begin("half")
    with V.state_lock(tmp_path):
        s.publish("done", 1)
    assert V.VersionStore(tmp_path).recover() == ["half"]


def test_cancelled_version_is_not_interrupted(tmp_path):
    s = V.VersionStore(tmp_path)
    s.begin("c1")
    s.mark_cancelled("c1")
    assert V.VersionStore(tmp_path).recover() == []


def test_current_dir_none_when_pointer_incomplete(tmp_path):
    s = V.VersionStore(tmp_path)
    s.begin("r1")
    from patent_figure import project_io as pio
    pio.atomic_write_json(s.current_file, {"render_job_id": "r1", "revision": 1})
    assert s.current_dir() is None


def test_reclaim_protects_referenced_version(tmp_path):
    """R1d #6: a version pinned by an active read (export) survives reclamation even though it
    is neither current nor within keep_success."""
    s = V.VersionStore(tmp_path)
    for i in range(1, 8):
        _seal(s, "r%d" % i, i)
        with V.state_lock(tmp_path):
            s.publish("r%d" % i, i)            # current advances to r7
    s.ref("r1")                                 # an export pins the oldest version
    removed = s.reclaim(keep_success=2)
    kept = {d.name for d in s.results.iterdir()}
    assert "r1" in kept and "r1" not in removed, "referenced version must survive reclaim"
    s.unref("r1")
    assert "r1" in s.reclaim(keep_success=2)     # once released, it is reclaimable


def test_reclaim_keeps_current_and_recent(tmp_path):
    """R1c #11: reclamation keeps current + recent successes, drops older/failed versions."""
    s = V.VersionStore(tmp_path)
    for i in range(1, 6):
        _seal(s, "r%d" % i, i)
        with V.state_lock(tmp_path):
            s.publish("r%d" % i, i)
    s.begin("failed")                       # a never-sealed version
    removed = s.reclaim(keep_success=2)
    kept = {d.name for d in s.results.iterdir()}
    assert s.current_id() == "r5" and "r5" in kept  # current protected
    assert "r4" in kept                              # within keep_success=2
    assert "failed" in removed                       # stale incomplete reclaimed
    assert "r1" in removed                           # old success reclaimed
