"""VersionStore mechanics (WP1 R1b/R1c publish model). OCP-free, fault-injected."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import versions as V  # noqa: E402


def _commit(store, rid, revision):
    """Commit the pointer + refresh the view — what publish.commit_version does after sealing
    (VersionStore has no publish() shortcut any more, R1f bypass list)."""
    became = store.commit_pointer(rid, revision)
    if became:
        store.refresh_view()
    return became


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
        assert _commit(s, "r1", 1) is True
    assert s.current_id() == "r1" and s.current_dir() == s.version_dir("r1")
    assert (s.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf-r1"
    assert s.snapshot()[2]["ok"] is True


def test_publish_refuses_incomplete_version(tmp_path):
    s = V.VersionStore(tmp_path)
    s.begin("r1")
    with pytest.raises(ValueError):
        with V.state_lock(tmp_path):
            _commit(s, "r1", 1)
    assert s.current_id() is None


def test_monotonic_revision_refuses_stale(tmp_path):
    """R1c #3: a later-finishing OLDER revision must not overwrite a newer current version."""
    s = V.VersionStore(tmp_path)
    _seal(s, "rB", 2)
    _seal(s, "rA", 1)                       # older revision, finishes later
    with V.state_lock(tmp_path):
        assert _commit(s, "rB", 2) is True
        assert _commit(s, "rA", 1) is False  # refused as stale
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
        _commit(s, "r1", 1)
    assert (s.out / "fig1.dxf").is_file() and not (s.out / "stale.dxf").exists()


def test_copy_fallback_when_symlink_unavailable(tmp_path, monkeypatch):
    """Windows path: symlink raises → copy swap still produces a correct out view."""
    s = V.VersionStore(tmp_path)
    _seal(s, "r1", 1)
    monkeypatch.setattr(s, "_symlink_swap",
                        lambda target: (_ for _ in ()).throw(OSError("no symlink")))
    with V.state_lock(tmp_path):
        _commit(s, "r1", 1)
    assert not s.out.is_symlink()
    assert (s.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf-r1"


def test_recover_reports_incomplete_versions(tmp_path):
    s = V.VersionStore(tmp_path)
    _seal(s, "done", 1)
    s.begin("half")
    with V.state_lock(tmp_path):
        _commit(s, "done", 1)
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
            _commit(s, "r%d" % i, i)            # current advances to r7
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
            _commit(s, "r%d" % i, i)
    s.begin("failed")                       # a never-sealed version
    removed = s.reclaim(keep_success=2)
    kept = {d.name for d in s.results.iterdir()}
    assert s.current_id() == "r5" and "r5" in kept  # current protected
    assert "r4" in kept                              # within keep_success=2
    assert "failed" in removed                       # stale incomplete reclaimed
    assert "r1" in removed                           # old success reclaimed


# ----------------------------------------------------------------------------- R1e (repair 5)

def _seal_ok(store, rid, revision, required=None, files=None, ok=True):
    """Seal a version with a declared artefact list (R1e #2)."""
    vd = store.begin(rid)
    for name, text in (files or {"fig1.dxf": "dxf-" + rid}).items():
        (vd / name).write_text(text, encoding="utf-8")
    body = {"figures": [{"id": "fig1", "pass": ok}], "ok": ok}
    store.write_result(rid, body)
    m = dict(body)
    m["required_artefacts"] = list(required or ["fig1.dxf"])
    store.seal(rid, m, revision)
    return vd


def test_verify_checks_declared_artefacts_not_just_present_files(tmp_path):
    """R1e #2: a version that claims ok but lacks an artefact on its declared list fails verify
    and can never become current — the seal no longer only registers what happened to exist."""
    s = V.VersionStore(tmp_path)
    _seal_ok(s, "r1", 1, required=["fig1.dxf", "fig1_filing.dxf"])
    assert not s.verify("r1") and not s.is_publishable("r1")
    with pytest.raises(ValueError):
        with V.state_lock(tmp_path):
            s.commit_pointer("r1", 1)
    assert s.current_id() is None


def test_verify_rejects_zero_byte_required_artefact(tmp_path):
    """R1e #3: a required artefact sealed at zero bytes is not deliverable (size matches the
    seal, so only the declared-list check catches it)."""
    s = V.VersionStore(tmp_path)
    _seal_ok(s, "r1", 1, files={"fig1.dxf": ""})
    assert not s.verify("r1")
    with pytest.raises(ValueError):
        s.acquire_verified_version("r1")
    assert s.referenced() == set(), "a refused acquire must not leave a reference behind"


def test_okfalse_version_is_never_committed(tmp_path):
    """R1e #1/#2: the store itself refuses to point current at a sealed ok=false version."""
    s = V.VersionStore(tmp_path)
    _seal_ok(s, "good", 1)
    with V.state_lock(tmp_path):
        assert s.commit_pointer("good", 1)
    _seal_ok(s, "bad", 2, ok=False)
    assert s.verify("bad"), "ok=false is intact (kept for diagnosis) ..."
    assert not s.is_publishable("bad"), "... but not publishable"
    with pytest.raises(ValueError):
        with V.state_lock(tmp_path):
            s.commit_pointer("bad", 2)
    assert s.current_id() == "good"


def test_view_race_pause_a_commit_b_resume_a_ends_on_b(tmp_path, monkeypatch):
    """R1e #6: A's view refresh pauses mid-swap; B commits and refreshes; A resumes. The view
    must end on B — refreshes are serialised by the view lock and re-read current inside it."""
    import threading
    s = V.VersionStore(tmp_path)
    _seal_ok(s, "A", 1)
    _seal_ok(s, "B", 2)
    with V.state_lock(tmp_path):
        assert s.commit_pointer("A", 1)
    real = V.VersionStore.sync_out
    paused, resume = threading.Event(), threading.Event()

    def slow_sync(self, rid):
        if rid == "A" and not paused.is_set():
            paused.set()
            assert resume.wait(10)
        return real(self, rid)

    monkeypatch.setattr(V.VersionStore, "sync_out", slow_sync)
    ta = threading.Thread(target=lambda: V.VersionStore(tmp_path).refresh_view())
    ta.start()
    assert paused.wait(10)

    def commit_b():
        sb = V.VersionStore(tmp_path)
        with V.state_lock(tmp_path):
            assert sb.commit_pointer("B", 2)
        sb.refresh_view()

    tb = threading.Thread(target=commit_b)
    tb.start()
    import time as _t
    _t.sleep(0.2)                       # B has committed and is waiting for the view lock
    assert s.current_id() == "B"
    resume.set()
    ta.join(10)
    tb.join(10)
    assert os.path.realpath(str(s.out)) == os.path.realpath(str(s.version_dir("B")))
    assert (s.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf-B"
    assert not s.view_is_dirty()


def test_view_race_commit_from_outside_the_lock_is_caught_by_recheck(tmp_path, monkeypatch):
    """R1e #6 (cross-process flavour): B commits WITHOUT taking the in-process view lock (as a
    second process would). A's post-swap re-read of current sees B and installs it."""
    s = V.VersionStore(tmp_path)
    _seal_ok(s, "A", 1)
    _seal_ok(s, "B", 2)
    with V.state_lock(tmp_path):
        assert s.commit_pointer("A", 1)
    real = V.VersionStore.sync_out
    done = {"b": False}

    def sync_then_b_commits(self, rid):
        real(self, rid)
        if rid == "A" and not done["b"]:
            done["b"] = True
            from patent_figure import project_io as pio
            pio.atomic_write_json(self.current_file, {"render_job_id": "B", "revision": 2})

    monkeypatch.setattr(V.VersionStore, "sync_out", sync_then_b_commits)
    assert s.refresh_view() is True
    assert (s.out / "fig1.dxf").read_text(encoding="utf-8") == "dxf-B"


def test_install_view_second_rename_failure_restores_old_view(tmp_path, monkeypatch):
    """R1e #1 fault injection — "the second rename fails": a directory view (Windows copy /
    legacy dir) must be moved aside before the new one is renamed in. If that second rename
    fails, the old view is renamed BACK: nothing is deleted, the commit stands, the view is
    marked dirty and repaired by the next refresh."""
    s = V.VersionStore(tmp_path)
    monkeypatch.setattr(s, "_symlink_swap",
                        lambda target: (_ for _ in ()).throw(OSError("no symlink")))
    _seal_ok(s, "r1", 1)
    with V.state_lock(tmp_path):
        assert _commit(s, "r1", 1)
    assert not s.out.is_symlink() and (s.out / "fig1.dxf").read_text("utf-8") == "dxf-r1"
    _seal_ok(s, "r2", 2)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        if str(dst) == str(s.out) and "out.new" in str(src):
            calls["n"] += 1
            raise OSError("injected: second rename failed")
        return real_replace(src, dst)

    monkeypatch.setattr(V.os, "replace", flaky_replace)
    with V.state_lock(tmp_path):
        assert s.commit_pointer("r2", 2)                  # the commit itself succeeds
    assert s.refresh_view() is False and calls["n"] == 1  # the view install failed
    assert s.current_id() == "r2"                         # commit stands
    assert (s.out / "fig1.dxf").read_text("utf-8") == "dxf-r1", "old view restored, not lost"
    assert s.view_is_dirty()
    assert not list(tmp_path.glob("out.new.*")) and not list(tmp_path.glob("out.old.*"))
    assert s.version_dir("r1").is_dir() and s.version_dir("r2").is_dir()
    monkeypatch.setattr(V.os, "replace", real_replace)
    s.ensure_view()                                       # next read repairs the view
    assert (s.out / "fig1.dxf").read_text("utf-8") == "dxf-r2" and not s.view_is_dirty()


def test_symlink_view_replace_failure_keeps_old_link(tmp_path, monkeypatch):
    """POSIX view: ONE os.replace of the link. If it fails the old link is untouched."""
    s = V.VersionStore(tmp_path)
    _seal_ok(s, "r1", 1)
    with V.state_lock(tmp_path):
        assert _commit(s, "r1", 1)
    _seal_ok(s, "r2", 2)
    real_replace = os.replace
    monkeypatch.setattr(V.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("boom"))
                        if str(b) == str(s.out) else real_replace(a, b))
    with V.state_lock(tmp_path):
        assert s.commit_pointer("r2", 2)
    assert s.refresh_view() is False
    assert (s.out / "fig1.dxf").read_text("utf-8") == "dxf-r1"
    assert not list(tmp_path.glob("out.lnk.*")), "staged link cleaned up"


def test_reconcile_current_rolls_back_okfalse_current_to_last_ok(tmp_path):
    """R1e #4: a current left pointing at a verify-clean but ok=false version (an older build
    could do that) is isolated — rolled back to the last publishable version, view re-pointed."""
    s = V.VersionStore(tmp_path)
    _seal_ok(s, "good", 1)
    with V.state_lock(tmp_path):
        _commit(s, "good", 1)
    _seal_ok(s, "bad", 2, ok=False)
    from patent_figure import project_io as pio
    pio.atomic_write_json(s.current_file, {"render_job_id": "bad", "revision": 2})
    s.sync_out("bad")
    assert V.VersionStore(tmp_path).reconcile_current() == "bad"
    assert s.current_id() == "good"
    assert (s.out / "fig1.dxf").read_text("utf-8") == "dxf-good"
    assert V.VersionStore(tmp_path).reconcile_current() is None     # idempotent


def test_reconcile_current_clears_pointer_and_view_without_a_good_version(tmp_path):
    """R1e #4: no publishable version to fall back to → pointer cleared AND the out/ view
    removed, so nothing keeps naming the quarantined version."""
    s = V.VersionStore(tmp_path)
    _seal_ok(s, "bad", 1, ok=False)
    from patent_figure import project_io as pio
    pio.atomic_write_json(s.current_file, {"render_job_id": "bad", "revision": 1})
    s.sync_out("bad")
    assert s.out.is_symlink()
    assert s.reconcile_current() == "bad"
    assert s.current_id() is None
    assert not s.out.exists() and not s.out.is_symlink()
    assert s.version_dir("bad").is_dir(), "the failed version is kept for inspection"


def test_acquire_and_reclaim_are_one_critical_section(tmp_path, monkeypatch):
    """R1e #7: verify + ref happen under the state lock that reclaim decides under. Pause the
    acquire inside its verify; a concurrent reclaim must wait and then see the pin."""
    import threading
    s = V.VersionStore(tmp_path)
    for i in range(1, 4):
        _seal_ok(s, "r%d" % i, i)
        with V.state_lock(tmp_path):
            _commit(s, "r%d" % i, i)
    real = V.VersionStore.is_publishable
    inside, go = threading.Event(), threading.Event()

    def slow(self, rid):
        if rid == "r1" and not inside.is_set():
            inside.set()
            assert go.wait(10)
        return real(self, rid)

    monkeypatch.setattr(V.VersionStore, "is_publishable", slow)
    got = {}
    ta = threading.Thread(target=lambda: got.setdefault(
        "pin", V.VersionStore(tmp_path).acquire_verified_version("r1")))
    ta.start()
    assert inside.wait(10)
    removed = {}
    tb = threading.Thread(target=lambda: removed.setdefault(
        "ids", V.VersionStore(tmp_path).reclaim(keep_success=0)))
    tb.start()
    import time as _t
    _t.sleep(0.2)
    assert tb.is_alive(), "reclaim must block while the acquire holds the state lock"
    go.set()
    ta.join(10)
    tb.join(10)
    assert got["pin"][0] == "r1"
    assert "r1" not in removed["ids"] and s.version_dir("r1").is_dir()
    s.release("r1")
    assert "r1" in s.reclaim(keep_success=0)


def test_acquire_after_reclaim_refuses_without_leaking_a_ref(tmp_path):
    s = V.VersionStore(tmp_path)
    _seal_ok(s, "old", 1)
    _seal_ok(s, "new", 2)
    with V.state_lock(tmp_path):
        _commit(s, "new", 2)
    assert "old" in s.reclaim(keep_success=0)
    with pytest.raises(ValueError):
        s.acquire_verified_version("old")
    assert s.referenced() == set()


# ----------------------------------------------------------------------------- R1f #1 (cross-process)

import subprocess  # noqa: E402
import time  # noqa: E402

_SCRIPTS = str(_ROOT / "scripts")


def _py(code: str, *args) -> "subprocess.Popen":
    return subprocess.Popen([sys.executable, "-c", code] + [str(a) for a in args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_next_revision_is_unique_across_processes(tmp_path):
    """R1f #1: revision allocation is under the cross-process store lock — 6 processes drawing
    25 revisions each concurrently get 150 distinct, gap-free numbers."""
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from patent_figure import versions as V\n"
            "s = V.VersionStore(sys.argv[1])\n"
            "print(' '.join(str(s.next_revision()) for _ in range(25)))\n") % _SCRIPTS
    procs = [_py(code, tmp_path) for _ in range(6)]
    got = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        got += [int(x) for x in out.split()]
    assert sorted(got) == list(range(1, 151)), "duplicate or lost revisions across processes"


_COMMIT_CHILD = r'''
import sys, time, json
from pathlib import Path
sys.path.insert(0, %r)
from patent_figure import project_io as pio, versions as V, publish as P
root, rid, rev, pause, marker = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
s = V.VersionStore(root)
real = pio.atomic_write_json
def slow(path, obj):
    if Path(path).name == "current.json" and pause > 0:
        Path(marker).write_text("inside")          # past the stale check, before the write
        time.sleep(pause)
    return real(path, obj)
pio.atomic_write_json = slow
vd = s.begin(rid)
(vd / "fig1.dxf").write_text("dxf-" + rid)
print(json.dumps({"became": P.commit_version(
    s, rid, rev, {"figures": []}, {"required_artefacts": ["fig1.dxf"]}, ok=True),
    "t": time.time()}))
''' % _SCRIPTS


def test_commit_compare_and_replace_is_atomic_across_processes(tmp_path):
    """R1f #1 (Codex repro, real processes): OLD (revision 1) passes the revision comparison
    and pauses before writing current.json; NEW (revision 2) commits meanwhile. With
    thread-only locks NEW was overwritten by OLD (current regressed to 1). With the cross-process
    store lock NEW waits, then wins: current never goes backwards."""
    marker = tmp_path / "old-inside"
    old = _py(_COMMIT_CHILD, tmp_path / "store", "old", 1, 3.0, marker)
    t0 = time.time()
    while not marker.exists():
        assert time.time() - t0 < 30, old.stderr.read() if old.poll() is not None else "timeout"
        time.sleep(0.02)
    new = _py(_COMMIT_CHILD, tmp_path / "store", "new", 2, 0, tmp_path / "unused")
    o_out, o_err = old.communicate(timeout=60)
    n_out, n_err = new.communicate(timeout=60)
    assert old.returncode == 0 and new.returncode == 0, (o_err, n_err)
    s = V.VersionStore(tmp_path / "store")
    assert s.current_id() == "new" and s.current_revision() == 2, \
        "current regressed to the older revision"
    assert (s.out / "fig1.dxf").read_text("utf-8") == "dxf-new"
    import json as _json
    assert _json.loads(o_out)["t"] <= _json.loads(n_out)["t"], "NEW must have waited for OLD"


def test_store_lock_excludes_another_process(tmp_path):
    """While this process holds a store's state lock, another process cannot take it."""
    code = ("import sys, time; sys.path.insert(0, %r)\n"
            "from patent_figure import versions as V\n"
            "with V.state_lock(sys.argv[1]):\n"
            "    print(time.time())\n") % _SCRIPTS
    with V.state_lock(tmp_path):
        p = _py(code, tmp_path)
        time.sleep(1.0)
        released = time.time()
    out, err = p.communicate(timeout=30)
    assert p.returncode == 0, err
    assert float(out.strip()) >= released, "the other process got the lock while we held it"


def test_in_place_target_must_be_an_unsealed_version_dir(tmp_path):
    """R1f bypass list: --in-place may only fill an in-progress version dir."""
    s = V.VersionStore(tmp_path)
    vd = s.begin("r1")
    assert V.is_unsealed_version_dir(vd)
    assert not V.is_unsealed_version_dir(tmp_path)              # arbitrary folder
    _seal_ok(s, "r2", 1)
    assert not V.is_unsealed_version_dir(s.version_dir("r2"))   # sealed = immutable
    _commit(s, "r2", 1)
    assert not V.is_unsealed_version_dir(s.out)                 # the view
    s.mark_cancelled("r1")
    assert not V.is_unsealed_version_dir(vd)


def test_no_compat_publish_shortcuts_remain():
    """R1f bypass list: the store exposes no publish shortcut that skips commit_version."""
    for name in ("publish", "write_manifest", "current_result"):
        assert not hasattr(V.VersionStore, name), name
