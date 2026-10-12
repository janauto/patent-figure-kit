"""候选版本回收（WP2 修复轮 1 / Codex R2 #5）：回收只拿「已封口或已取消、无活动租约、不属于最近
几次作业」的候选版本。运行中的候选作业对自己全部候选 id 持 WP1 的跨进程租约，预览读取也持租约。

覆盖：四作业交错（最旧的仍在跑）、作业中途取消、预览读取期间回收、另一进程持租约。OCP 无关。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import publish as P  # noqa: E402
from patent_figure import suggest_plan as SP  # noqa: E402


def _seal(store, rid, age=0.0):
    vd = store.begin(rid)
    (vd / "fig1.dxf").write_text("x", encoding="utf-8")
    P.seal_candidate(store, rid, {"figures": []}, {"required_artefacts": []}, ok=True)
    if age:
        t = time.time() - age
        os.utime(str(vd), (t, t))
    return vd


def _unsealed(store, rid, age=0.0):
    vd = store.begin(rid)
    (vd / "partial.dxf").write_text("x", encoding="utf-8")
    if age:
        t = time.time() - age
        os.utime(str(vd), (t, t))
    return vd


def _ids(store):
    return sorted(d.name for d in store.results.iterdir()) if store.results.is_dir() else []


def test_four_interleaved_jobs_never_reclaim_a_running_job(tmp_path):
    store = P.candidate_store(tmp_path)
    # job A (the OLDEST) is still running: c1 sealed, c2 mid-render; it leases both ids
    for rid in ("jobA-c1", "jobA-c2"):
        store.ref(rid)
    _seal(store, "jobA-c1", age=400)
    _unsealed(store, "jobA-c2", age=400)
    for age, job in ((300, "jobB"), (200, "jobC"), (100, "jobD")):
        _seal(store, job + "-c1", age=age)
        _seal(store, job + "-c2", age=age)
    removed = P.prune_candidates(tmp_path, keep_jobs=3, protect={"jobD"})
    assert removed == []                                     # A is leased, B/C/D are kept
    assert {"jobA-c1", "jobA-c2"} <= set(_ids(store))
    # A finishes: seals c2, releases its leases → now the oldest finished job → reclaimable
    vd = _seal(store, "jobA-c2", age=400)
    t = time.time() - 400
    os.utime(str(vd), (t, t))
    for rid in ("jobA-c1", "jobA-c2"):
        store.unref(rid)
    removed = P.prune_candidates(tmp_path, keep_jobs=3)
    assert sorted(removed) == ["jobA-c1", "jobA-c2"]
    assert _ids(store) == sorted(["jobB-c1", "jobB-c2", "jobC-c1", "jobC-c2",
                                  "jobD-c1", "jobD-c2"])


def test_unleased_unsealed_dir_is_never_reclaimed_but_a_cancelled_one_is(tmp_path):
    store = P.candidate_store(tmp_path)
    _unsealed(store, "jobX-c1", age=500)          # another process may be rendering into it
    for age, job in ((300, "jobB"), (200, "jobC"), (100, "jobD")):
        _seal(store, job + "-c1", age=age)
    assert P.prune_candidates(tmp_path, keep_jobs=3) == []
    store.mark_cancelled("jobX-c1")
    assert P.prune_candidates(tmp_path, keep_jobs=3) == ["jobX-c1"]


def _fake_render(calls, *, raise_on=None, prune_during=None):
    """A stand-in for publish.render_candidate that writes a version like the real one, and can
    raise mid-render (cancellation) or run a concurrent reclaim mid-render."""
    def render(ws_root, cache, rid, plan, assembly_path, run_cli, svg_of):
        store = P.candidate_store(ws_root)
        vd = store.begin(rid)
        (vd / "fig1.dxf").write_text("x", encoding="utf-8")
        calls.append(rid)
        if raise_on and rid.endswith(raise_on):
            from patent_figure.jobs import JobCancelled
            raise JobCancelled()
        if prune_during and rid.endswith(prune_during):
            prune_during_out.extend(P.prune_candidates(ws_root, keep_jobs=0))
        P.seal_candidate(store, rid, {"figures": []}, {"required_artefacts": []}, ok=True)
        return {"report": {"figures": [{"id": "fig1", "gate_pass": True, "labels": 1,
                                        "text_height_mm": 7.0, "qa": {"checks": []}}]},
                "svg": None}
    prune_during_out = []
    render.pruned = prune_during_out
    return render


def _doc(n=3):
    return {"candidates": [{"id": "c%d" % (i + 1), "caption": "x", "_trial_plan": {},
                            "plan_errors": []} for i in range(n)]}


def test_a_running_job_leases_all_its_candidates_against_a_concurrent_reclaim(tmp_path,
                                                                                monkeypatch):
    calls = []
    fake = _fake_render(calls, prune_during="c2")
    monkeypatch.setattr(P, "render_candidate", fake)
    doc = SP.run_candidates(_doc(3), ws_root=tmp_path, cache=tmp_path / "cache", job_id="jobR",
                            assembly_path=tmp_path / "a.json", run_cli=None, svg_of=None)
    # a reclaim with keep_jobs=0 ran WHILE c2 was being written: nothing of this job went away
    assert fake.pruned == []
    store = P.candidate_store(tmp_path)
    assert _ids(store) == ["jobR-c1", "jobR-c2", "jobR-c3"]
    assert [c["status"] for c in doc["candidates"]] == ["pass", "pass", "pass"]
    assert store.referenced() == set()                      # leases released at the end
    assert sorted(P.prune_candidates(tmp_path, keep_jobs=0)) == \
        ["jobR-c1", "jobR-c2", "jobR-c3"]


def test_cancel_mid_render_marks_the_partial_version_and_releases_every_lease(tmp_path,
                                                                             monkeypatch):
    from patent_figure.jobs import JobCancelled
    calls = []
    monkeypatch.setattr(P, "render_candidate", _fake_render(calls, raise_on="c2"))
    with pytest.raises(JobCancelled):
        SP.run_candidates(_doc(3), ws_root=tmp_path, cache=tmp_path / "c", job_id="jobK",
                          assembly_path=tmp_path / "a.json", run_cli=None, svg_of=None)
    store = P.candidate_store(tmp_path)
    assert calls == ["jobK-c1", "jobK-c2"]
    assert store.is_complete("jobK-c1") and store.is_cancelled("jobK-c2")
    assert store.referenced() == set()
    assert sorted(P.prune_candidates(tmp_path, keep_jobs=0)) == ["jobK-c1", "jobK-c2"]


def test_a_preview_read_lease_survives_reclaim_until_released(tmp_path):
    store = P.candidate_store(tmp_path)
    _seal(store, "jobOld-c1", age=900)
    for age, job in ((300, "jobB"), (200, "jobC"), (100, "jobD")):
        _seal(store, job + "-c1", age=age)
    store.ref("jobOld-c1")                                   # a reader is serving its SVG
    assert P.prune_candidates(tmp_path, keep_jobs=3) == []
    store.unref("jobOld-c1")
    assert P.prune_candidates(tmp_path, keep_jobs=3) == ["jobOld-c1"]


def test_a_lease_held_by_another_process_protects_until_that_process_dies(tmp_path):
    store = P.candidate_store(tmp_path)
    _seal(store, "jobP-c1", age=900)
    for age, job in ((300, "jobB"), (200, "jobC"), (100, "jobD")):
        _seal(store, job + "-c1", age=age)
    code = ("import sys, time; sys.path.insert(0, %r)\n"
            "from patent_figure import publish as P\n"
            "P.candidate_store(%r).ref('jobP-c1'); print('held', flush=True); time.sleep(60)\n"
            % (str(_ROOT / "scripts"), str(tmp_path)))
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        assert P.prune_candidates(tmp_path, keep_jobs=3) == []
    finally:
        child.kill()
        child.wait()
    assert P.prune_candidates(tmp_path, keep_jobs=3) == ["jobP-c1"]   # dead pid → stale lease
