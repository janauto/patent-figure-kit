"""The single export service (WP1 R1e #3/#7). OCP-free; DXF/SVG formats only (no matplotlib).

Browser /api/export, REST and MCP all go through ``patent_figure.export``: acquire = verify +
pin under the state lock, build reads only the pinned version, release on every path.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import export as EX  # noqa: E402
from patent_figure import versions as V  # noqa: E402


def _commit(store, rid, revision):
    """Commit the pointer + refresh the view — what publish.commit_version does after sealing
    (VersionStore has no publish() shortcut any more, R1f bypass list)."""
    became = store.commit_pointer(rid, revision)
    if became:
        store.refresh_view()
    return became

ART = ("fig1.dxf", "fig1.svg", "fig1_annotated.dxf", "fig1_annotated.svg",
       "fig1_filing.dxf", "fig1_filing.svg", "reference-numerals.json")


def _publish(store, rid, revision, tag):
    vd = store.begin(rid)
    for name in ART:
        (vd / name).write_text("%s %s" % (name, tag), encoding="utf-8")
    body = {"ok": True, "figures": [{"id": "fig1", "pass": True, "number": 1,
                                     "annotation": {"figure_description": "图1为%s；" % tag}}]}
    store.write_result(rid, body)
    m = {"ok": True, "required_artefacts": list(ART)}
    store.seal(rid, m, revision)
    with V.state_lock(store.root):
        assert _commit(store, rid, revision)
    return vd


def test_acquire_refuses_zero_byte_artefact_and_leaks_no_pin(tmp_path):
    """R1e #3: a zero-byte / truncated required artefact is refused at acquire — nothing is
    packaged — and the refused acquire leaves no reference behind."""
    s = V.VersionStore(tmp_path)
    vd = _publish(s, "r1", 1, "a")
    (vd / "fig1_filing.dxf").write_text("", encoding="utf-8")
    with pytest.raises(EX.ExportRefused):
        EX.acquire(tmp_path)
    assert s.referenced() == set()


def test_acquire_refuses_missing_version_and_no_fallback(tmp_path):
    """No published version → refused. There is no ws.out / cached-result fallback."""
    with pytest.raises(EX.ExportRefused):
        EX.acquire(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "fig1.dxf").write_text("stray", encoding="utf-8")
    with pytest.raises(EX.ExportRefused):
        EX.acquire(tmp_path)


def test_pinned_version_survives_switch_and_reclaim_then_is_released(tmp_path):
    """R1e #3/#7: the bundle reads ONE pinned version even if current moves on and a reclaim
    runs mid-export; after release the version is reclaimable again."""
    s = V.VersionStore(tmp_path)
    _publish(s, "r1", 1, "a")
    pin = EX.acquire(tmp_path)
    assert pin.rid == "r1"
    _publish(s, "r2", 2, "b")                          # current moves on
    assert "r1" not in s.reclaim(keep_success=0)       # pinned → protected
    out = EX.build_bundle(pin, tmp_path / "bundle", variants=("raw", "annotated", "filing"),
                          formats=("dxf", "svg"))
    pin.release()
    pin.release()                                      # idempotent — no double unref
    assert all(v == "ok" for v in out["manifest"].values()), out["manifest"]
    assert (tmp_path / "bundle" / "交底版_带件号表" / "fig1_annotated.dxf").read_text("utf-8") \
        == "fig1_annotated.dxf a"
    assert s.referenced() == set()
    assert "r1" in s.reclaim(keep_success=0)


def test_bundle_never_writes_into_the_version_and_fails_a_vanished_source(tmp_path):
    """The build writes only under dest; a source that vanishes mid-read is 'fail', never a
    silently packaged hole."""
    s = V.VersionStore(tmp_path)
    vd = _publish(s, "r1", 1, "a")
    before = sorted(p.name for p in vd.iterdir())
    with EX.acquire(tmp_path) as pin:
        (vd / "fig1_filing.svg").unlink()              # corrupted after the pin
        out = EX.build_bundle(pin, tmp_path / "b", variants=("annotated", "filing"),
                              formats=("dxf", "svg", "pdf"), description_txt=True)
    assert out["manifest"]["fig1/递交版/svg"] == "fail"
    # WP2 F1: PDF is implemented; these fake "DXF" sources cannot be rasterised, so the PDF is
    # fail WITH a reason — never ok, never a silent skip — and no unverified PDF is packaged.
    assert out["manifest"]["fig1/交底版/pdf"] == "fail"
    assert out["reasons"]["fig1/交底版/pdf"]
    assert "说明书附图_交底版.pdf" not in out["files"]
    assert out["ok"] is False
    assert "递交版_仅图号/fig1_filing.svg" not in out["files"]
    assert "附图说明.txt" in out["files"] and "reference-numerals.json" in out["files"]
    assert sorted(p.name for p in vd.iterdir()) == sorted(set(before) - {"fig1_filing.svg"})


def test_zip_dir_is_atomic(tmp_path):
    src = tmp_path / "b"
    (src / "a").mkdir(parents=True)
    (src / "a" / "x.txt").write_text("x", encoding="utf-8")
    z = EX.zip_dir(src, tmp_path / "b.zip")
    assert zipfile.ZipFile(str(z)).namelist() == ["a/x.txt"]
    assert not list(tmp_path.glob("*.tmp"))


def test_cli_versions_export_through_the_same_service(tmp_path):
    """R1f bypass list: bench.py's export timing (and any tool exporting a direct-CLI output)
    goes through acquire → build_bundle → zip_dir; CLI results mark figures ``gate_pass``."""
    from patent_figure import publish as P
    vs = P.cli_store(tmp_path / "out")
    vd = vs.begin("c1")
    for name in ("fig1.dxf", "fig1.qa.json", "reference-numerals.json"):
        (vd / name).write_text(name, encoding="utf-8")
    assert P.commit_version(vs, "c1", vs.next_revision(),
                            {"figures": [{"id": "fig1", "gate_pass": True}]},
                            {"required_artefacts": ["fig1.dxf", "fig1.qa.json",
                                                    "reference-numerals.json"]}, ok=True)
    with EX.acquire(vs.root) as pin:
        out = EX.build_bundle(pin, tmp_path / "b", variants=("raw",), formats=("dxf",))
        EX.zip_dir(tmp_path / "b", tmp_path / "b.zip")
    assert out["manifest"] == {"fig1/原始/dxf": "ok"}
    assert "原始出图/fig1.dxf" in zipfile.ZipFile(str(tmp_path / "b.zip")).namelist()
    assert vs.referenced() == set()
