"""VersionStore mechanics (WP1 R1b publish model). OCP-free, fault-injected."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import versions as V  # noqa: E402


def _seal(store, rid, figs=("fig1",)):
    vd = store.begin(rid)
    (vd / "fig1.dxf").write_text("dxf", encoding="utf-8")
    store.write_manifest(rid, {"figures": [{"id": f, "pass": True} for f in figs], "ok": True})
    return vd


def test_begin_and_complete_marker(tmp_path):
    s = V.VersionStore(tmp_path)
    assert not s.is_complete("r1")
    _seal(s, "r1")
    assert s.is_complete("r1")
    assert s.read_manifest("r1")["ok"] is True


def test_publish_points_current_and_syncs_out(tmp_path):
    s = V.VersionStore(tmp_path)
    _seal(s, "r1")
    with V.state_lock(tmp_path):
        s.publish("r1")
    assert s.current_id() == "r1"
    assert s.current_dir() == s.version_dir("r1")
    # out/ resolves to the version dir (symlink on POSIX)
    assert (s.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf"


def test_publish_refuses_incomplete_version(tmp_path):
    s = V.VersionStore(tmp_path)
    s.begin("r1")  # no manifest
    with pytest.raises(ValueError):
        with V.state_lock(tmp_path):
            s.publish("r1")
    assert s.current_id() is None


def test_second_publish_replaces_pointer_and_view(tmp_path):
    s = V.VersionStore(tmp_path)
    _seal(s, "r1", figs=("fig1",))
    with V.state_lock(tmp_path):
        s.publish("r1")
    vd2 = s.begin("r2")
    (vd2 / "fig1.dxf").write_text("v2", encoding="utf-8")
    (vd2 / "only-in-r2.txt").write_text("x", encoding="utf-8")
    s.write_manifest("r2", {"figures": [{"id": "fig1", "pass": True}], "ok": True})
    with V.state_lock(tmp_path):
        s.publish("r2")
    assert s.current_id() == "r2"
    assert (s.out / "fig1.dxf").read_text(encoding="utf-8") == "v2"
    assert (s.out / "only-in-r2.txt").is_file()        # out is exactly r2's set


def test_sync_out_handles_legacy_real_directory(tmp_path):
    """Every pre-redo project has out/ as a REAL directory; the first publish must swap it for
    the view without raising (R1b finding 2)."""
    s = V.VersionStore(tmp_path)
    s.out.mkdir()
    (s.out / "stale.dxf").write_text("old", encoding="utf-8")   # legacy real dir
    _seal(s, "r1")
    with V.state_lock(tmp_path):
        s.publish("r1")
    assert (s.out / "fig1.dxf").is_file()
    assert not (s.out / "stale.dxf").exists()           # the legacy content is gone


def test_second_step_crash_leaves_pointer_valid_and_recovers(tmp_path, monkeypatch):
    """If the out/ swap crashes after current.json is written, the pointer still names a
    complete version and startup recovery repairs out/ (R1b finding 2)."""
    s = V.VersionStore(tmp_path)
    _seal(s, "r1")
    monkeypatch.setattr(s, "sync_out", lambda rid: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        with V.state_lock(tmp_path):
            s.publish("r1")
    # pointer is valid even though the view swap failed
    assert s.current_id() == "r1" and s.current_dir() == s.version_dir("r1")
    # a fresh store (restart) repairs the view
    s2 = V.VersionStore(tmp_path)
    assert s2.recover() == []                           # r1 is complete, not interrupted
    assert (s2.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf"


def test_recover_reports_incomplete_versions(tmp_path):
    s = V.VersionStore(tmp_path)
    _seal(s, "done")
    s.begin("half")                                     # started, never sealed
    with V.state_lock(tmp_path):
        s.publish("done")
    incomplete = V.VersionStore(tmp_path).recover()
    assert incomplete == ["half"]


def test_cancelled_version_is_not_interrupted(tmp_path):
    s = V.VersionStore(tmp_path)
    s.begin("c1")
    s.mark_cancelled("c1")
    assert V.VersionStore(tmp_path).recover() == []     # cancelled != interrupted


def test_current_dir_none_when_pointer_incomplete(tmp_path):
    s = V.VersionStore(tmp_path)
    s.begin("r1")
    # hand-write a pointer at an incomplete version (shouldn't happen via publish)
    from patent_figure import project_io as pio
    pio.atomic_write_json(s.current_file, {"render_job_id": "r1"})
    assert s.current_dir() is None
