"""The single publish service (WP1 R1e): annotation gate, direct-CLI store, startup reconcile.

OCP-free: the renderer and the annotation CLI are replaced by a fake ``run_cli`` that writes
the files the real ones would, so each fault (an annotation that fails, an empty artefact, a
view install that fails, a legacy ok=false current) can be injected deterministically.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import studio_ext as EXT  # noqa: E402
from patent_figure import jobs as J  # noqa: E402
from patent_figure import project_io as PIO  # noqa: E402
from patent_figure import publish as P  # noqa: E402
from patent_figure import versions as V  # noqa: E402

FIGS = ("fig1", "fig2")


def _ws(tmp_path: Path, figs=FIGS) -> SimpleNamespace:
    root = tmp_path / "studio"
    root.mkdir(parents=True, exist_ok=True)
    (root / "plan.json").write_text(json.dumps(
        {"figures": [{"id": f, "caption": f, "kind": "assembly", "members": ["*"]}
                     for f in figs]}), encoding="utf-8")
    (root / "assembly.json").write_text("{}", encoding="utf-8")
    return SimpleNamespace(root=root, plan=root / "plan.json", cache=root / "cache",
                           out=root / "out")


def _fake_cli(fail_variant=None, empty_variant=None, tag="x"):
    """A run_cli that behaves like render_patent_figure.py --in-place / annotate_figure_sheet.py.
    ``fail_variant``: that annotation exits 1 and writes nothing. ``empty_variant``: it exits 0
    but writes a ZERO-byte DXF."""
    def run_cli(argv, timeout=0):
        argv = [str(a) for a in argv]
        if argv[0].endswith("render_patent_figure.py"):
            out = Path(argv[argv.index("-o") + 1])
            plan = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
            figs = [f["id"] for f in plan["figures"]]
            for fid in figs:
                (out / (fid + ".dxf")).write_text("DXF %s %s" % (fid, tag), encoding="utf-8")
                (out / (fid + ".qa.json")).write_text(json.dumps({"pass": True, "checks": []}),
                                                      encoding="utf-8")
            (out / "reference-numerals.json").write_text(
                json.dumps({"numerals": [], "description_zh": "附图标记说明：无"}),
                encoding="utf-8")
            Path(argv[argv.index("--json") + 1]).write_text(json.dumps(
                {"pass": True, "figures": [{"id": f, "gate_pass": True} for f in figs]}),
                encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        if argv[0].endswith("annotate_figure_sheet.py"):
            dst = Path(argv[argv.index("-o") + 1])
            variant = "filing" if "--no-table" in argv else "annotated"
            if variant == fail_variant:
                return subprocess.CompletedProcess(argv, 1, "", "标注失败：注入")
            dst.write_text("" if variant == empty_variant else "ANN " + dst.name,
                           encoding="utf-8")
            Path(argv[argv.index("--json") + 1]).write_text(json.dumps(
                {"caption": "图", "table": None, "warnings": [],
                 "figure_description": "图为示意图；"}), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError("unexpected CLI %s" % argv[0])
    return run_cli


def _svg(path):
    return "<svg>%s</svg>" % Path(path).name


def _render(ws, rid, run_cli):
    snap = P.prepare(ws, rid)
    return P.run_and_publish(ws, rid, int(snap["revision"]), run_cli, _svg, inputs=snap)


# ----------------------------------------------------------------------------- R1e #2


def test_full_set_commits_and_declares_every_artefact(tmp_path):
    ws = _ws(tmp_path)
    out = _render(ws, "r1", _fake_cli())
    assert out["ok"] and out["became_current"]
    vs = V.VersionStore(ws.root)
    m = vs.read_manifest("r1")
    assert m["required_artefacts"] == EXT.expected_artefacts(FIGS)
    assert len(m["required_artefacts"]) == 1 + 9 * len(FIGS)   # numerals + 3 variants × 3
    assert vs.is_publishable("r1") and vs.current_id() == "r1"
    # every preview URL in the sealed result is bound to this version (R1e #5)
    fig = out["result"]["figures"][0]
    urls = [fig["svg"]] + list(fig["variants"].values()) + [fig["annotation"]["png"]]
    assert all(u.endswith("?v=r1") for u in urls), urls


@pytest.mark.parametrize("fault", ["fail_filing", "fail_annotated", "empty_filing"])
def test_annotation_failure_blocks_the_commit(tmp_path, fault):
    """R1e #2: the commit gate includes annotation. A disclosure/filing variant that fails (or
    comes out empty) makes the whole version ok=false: sealed for inspection, NOT committed —
    the previous complete version stays current."""
    ws = _ws(tmp_path)
    assert _render(ws, "good", _fake_cli())["became_current"]
    kw = {"fail_filing": {"fail_variant": "filing"},
          "fail_annotated": {"fail_variant": "annotated"},
          "empty_filing": {"empty_variant": "filing"}}[fault]
    out = _render(ws, "bad", _fake_cli(tag="bad", **kw))
    assert out["ok"] is False and out["became_current"] is False
    assert out["result"]["ok"] is False and out["result"]["missing_artefacts"]
    vs = V.VersionStore(ws.root)
    assert vs.current_id() == "good"
    assert (vs.out / "fig1.dxf").read_text("utf-8") == "DXF fig1 x"
    m = vs.read_manifest("bad")
    assert m["ok"] is False and m["missing_artefacts"]
    assert vs.is_complete("bad") and not vs.is_publishable("bad")
    with pytest.raises(ValueError):
        with V.state_lock(ws.root):
            vs.commit_pointer("bad", vs.revision_of("bad"))


def test_failed_render_without_numerals_is_structured_not_integrity_error(tmp_path):
    """Regression (found this round): a render that produced nothing (no numerals table) must
    come back as a structured ok=false — not a RuntimeError from the integrity check."""
    ws = _ws(tmp_path)

    def run_cli(argv, timeout=0):
        argv = [str(a) for a in argv]
        Path(argv[argv.index("--json") + 1]).write_text(json.dumps({"pass": False}), "utf-8")
        return subprocess.CompletedProcess(argv, 1, "", "plan 语义错误")

    out = _render(ws, "r1", run_cli)
    assert out["ok"] is False and out["became_current"] is False
    assert V.VersionStore(ws.root).is_complete("r1")


# ----------------------------------------------------------------------------- R1e #1 (CLI)


def _cli_seal(vs, rid, text, ok=True):
    vd = vs.begin(rid)
    (vd / "fig1.dxf").write_text(text, encoding="utf-8")
    (vd / "fig1.qa.json").write_text("{}", encoding="utf-8")
    (vd / "reference-numerals.json").write_text("{}", encoding="utf-8")
    if not ok:
        (vd / "diag").mkdir()
        os.replace(str(vd / "fig1.dxf"), str(vd / "diag" / "fig1.dxf"))
    manifest = {"kind": "cli", "required_artefacts":
                ["fig1.dxf", "fig1.qa.json", "reference-numerals.json"]}
    return P.commit_version(vs, rid, vs.next_revision(), {"figures": []}, manifest, ok=ok)


def test_cli_store_is_a_sibling_and_out_is_the_view(tmp_path):
    vs = P.cli_store(tmp_path / "out")
    assert vs.root == tmp_path / "out.versions" and vs.out == tmp_path / "out"
    assert _cli_seal(vs, "c1", "v1")
    assert (tmp_path / "out" / "fig1.dxf").read_text("utf-8") == "v1"   # N10's path


def test_cli_okfalse_never_switches_and_keeps_diag(tmp_path):
    """R1e #1: a failed CLI set never moves the pointer — out/ is still the previous complete
    version — and its diag/ stays in its own version dir."""
    vs = P.cli_store(tmp_path / "out")
    assert _cli_seal(vs, "c1", "v1")
    assert _cli_seal(vs, "c2", "v2", ok=False) is False
    assert vs.current_id() == "c1"
    assert (tmp_path / "out" / "fig1.dxf").read_text("utf-8") == "v1"
    assert (vs.version_dir("c2") / "diag" / "fig1.dxf").read_text("utf-8") == "v2"
    assert not (tmp_path / "out" / "diag").exists()


def test_cli_second_rename_failure_loses_nothing(tmp_path, monkeypatch):
    """R1e #1 fault injection — the old CLI swap was "rename out→old, rename new→out, finally
    delete both"; a failing second rename deleted BOTH sets. Under the pointer model the commit
    is one current.json write; the only rename pair left is installing a DIRECTORY view
    (Windows copy view), and if its second rename fails the old view is renamed back. Nothing
    is deleted: both versions survive, the commit stands, the view is repaired later."""
    vs = P.cli_store(tmp_path / "out")
    monkeypatch.setattr(V.VersionStore, "_symlink_swap",
                        lambda self, target: (_ for _ in ()).throw(OSError("no symlink")))
    assert _cli_seal(vs, "c1", "v1")
    assert not vs.out.is_symlink() and (vs.out / "fig1.dxf").read_text("utf-8") == "v1"
    real_replace = os.replace

    def flaky(src, dst):
        if str(dst) == str(vs.out) and "out.new" in str(src):
            raise OSError("injected: second rename failed")
        return real_replace(src, dst)

    monkeypatch.setattr(V.os, "replace", flaky)
    assert _cli_seal(vs, "c2", "v2") is True          # committed …
    assert vs.current_id() == "c2"
    assert (vs.out / "fig1.dxf").read_text("utf-8") == "v1"   # … old view intact, not deleted
    assert vs.view_is_dirty()
    assert vs.version_dir("c1").is_dir() and vs.version_dir("c2").is_dir()
    leftovers = [p.name for p in tmp_path.iterdir() if ".out.new." in p.name
                 or p.name.startswith(("out.new", "out.old"))]
    assert not leftovers, leftovers
    monkeypatch.setattr(V.os, "replace", real_replace)
    vs.ensure_view()                                   # next run / read repairs the view
    assert (vs.out / "fig1.dxf").read_text("utf-8") == "v2" and not vs.view_is_dirty()


def test_adopt_legacy_out_moves_aside_never_deletes(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "user-notes.txt").write_text("mine", encoding="utf-8")
    vs = P.cli_store(out)
    aside = P.adopt_legacy_out(vs)
    assert aside is not None and (aside / "user-notes.txt").read_text("utf-8") == "mine"
    assert not out.exists()
    assert _cli_seal(vs, "c1", "v1")
    assert P.adopt_legacy_out(vs) is None              # our own view is left alone
    (tmp_path / "empty").mkdir()
    vs2 = P.cli_store(tmp_path / "empty")
    assert P.adopt_legacy_out(vs2) is None and not (tmp_path / "empty").exists()


# ----------------------------------------------------------------------------- R1e #4


def _seal_job_version(vs, jobs, rid, ok, state, committed=False, revision=1):
    vd = vs.begin(rid)
    (vd / "fig1.dxf").write_text("dxf-" + rid, encoding="utf-8")
    body = {"figures": [{"id": "fig1", "pass": ok}], "ok": ok}
    vs.write_result(rid, body)
    m = dict(body)
    m["required_artefacts"] = ["fig1.dxf"]
    vs.seal(rid, m, revision)
    job, _ = jobs.create("render", "p1")
    data = job.to_disk()
    data.update(id=rid, state=state, committed=committed,
                result={"ok": ok} if state == J.SUCCEEDED else None)
    if state == J.SUCCEEDED:
        data["events"] = data["events"] + [{"id": 1, "type": J.EV_DONE, "stage": J.SUCCEEDED,
                                            "progress": 1.0, "data": {"ok": ok}, "ts": 0}]
        data["next_event"] = 2
    (jobs.root / (rid + ".json")).write_text(json.dumps(data), encoding="utf-8")
    (jobs.root / (job.id + ".json")).unlink()
    return vd


def test_startup_isolates_a_verified_but_okfalse_current(tmp_path):
    """R1e #4: verify passes, sealed result.ok=false, job persisted as SUCCEEDED + committed (as
    an older build could leave it). Startup must NOT keep it: the pointer rolls back to the
    last publishable version, the bad job becomes failed (committed cleared, error, error
    terminal event) and the good one is a consistent succeeded."""
    ws_root = tmp_path / "studio"
    vs = V.VersionStore(ws_root)
    jobs = J.JobStore(tmp_path / "jobs")
    _seal_job_version(vs, jobs, "good", True, J.SUCCEEDED, committed=False, revision=1)
    with V.state_lock(ws_root):
        assert vs.publish("good", 1)
    _seal_job_version(vs, jobs, "bad", False, J.SUCCEEDED, committed=True, revision=2)
    PIO.atomic_write_json(vs.current_file, {"render_job_id": "bad", "revision": 2})
    vs.sync_out("bad")

    jobs2 = J.JobStore(tmp_path / "jobs")                       # restart
    rep = P.reconcile_startup(ws_root, jobs2, "p1")
    assert rep["isolated"] == "bad" and rep["current"] == "good"
    assert vs.current_id() == "good"
    assert (vs.out / "fig1.dxf").read_text("utf-8") == "dxf-good"
    bad = jobs2.get("bad").to_dict()
    assert bad["state"] == J.FAILED and bad["committed"] is False
    assert bad["error"]["code"] == "E_RENDER_NOT_OK"
    assert jobs2.get("bad")._last_terminal_event()["type"] == J.EV_ERROR
    good = jobs2.get("good")
    assert good.state == J.SUCCEEDED and good.committed and good.error is None
    assert good._last_terminal_event()["type"] == J.EV_DONE
    # persisted: a second restart sees the same, and reconciliation is idempotent
    jobs3 = J.JobStore(tmp_path / "jobs")
    assert jobs3.get("bad").state == J.FAILED and not jobs3.get("bad").committed
    assert P.reconcile_startup(ws_root, jobs3, "p1")["changed"] == []


def test_startup_clears_okfalse_current_when_nothing_else_is_publishable(tmp_path):
    ws_root = tmp_path / "studio"
    vs = V.VersionStore(ws_root)
    jobs = J.JobStore(tmp_path / "jobs")
    _seal_job_version(vs, jobs, "bad", False, J.SUCCEEDED, committed=True)
    PIO.atomic_write_json(vs.current_file, {"render_job_id": "bad", "revision": 1})
    vs.sync_out("bad")
    jobs2 = J.JobStore(tmp_path / "jobs")
    rep = P.reconcile_startup(ws_root, jobs2, "p1")
    assert rep["isolated"] == "bad" and rep["current"] is None
    assert vs.current_id() is None and not vs.out.exists() and not vs.out.is_symlink()
    assert jobs2.get("bad").state == J.FAILED


def test_startup_interrupted_job_of_the_current_version_becomes_consistent_success(tmp_path):
    """The current, publishable version's job died before its success persisted: JobStore
    recovery marks it interrupted (with an E_INTERRUPTED error event); reconciliation must make
    state, error and the terminal event agree — succeeded, no error, a done event last."""
    ws_root = tmp_path / "studio"
    vs = V.VersionStore(ws_root)
    jobs = J.JobStore(tmp_path / "jobs")
    _seal_job_version(vs, jobs, "r1", True, J.RUNNING)
    with V.state_lock(ws_root):
        assert vs.publish("r1", 1)
    jobs2 = J.JobStore(tmp_path / "jobs")
    assert jobs2.get("r1").state == J.INTERRUPTED
    P.reconcile_startup(ws_root, jobs2, "p1")
    j = jobs2.get("r1")
    assert j.state == J.SUCCEEDED and j.error is None and j.committed
    assert j._last_terminal_event()["type"] == J.EV_DONE
