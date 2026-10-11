"""The single export service (WP1 R1e #3/#7).

Browser ``/api/export``, REST ``POST /v1/projects/{id}/export`` and MCP ``get_export`` all export
through this module — one way to choose, verify, pin, read and release a version:

* ``acquire(ws_root, rid=None) -> Pin`` — under the version store's ``state_lock`` (the lock
  ``reclaim`` decides under) resolve the version (``None`` → current), require it to be
  **publishable** (integrity-verified against its declared artefact list, sealed ``ok``,
  result ``ok`` — a missing / truncated / zero-byte required artefact is refused) and register
  a reference, all in ONE critical section. There is no window between "validated" and
  "pinned" in which a concurrent reclaim could delete the source (R1e #7). Raises
  ``ExportRefused`` (→ HTTP 409) otherwise. There is **no fallback** to ``ws.out`` or to a
  caller-supplied result: an export reads one pinned version or nothing.
* ``Pin.release()`` — idempotent; every exit path (job create/start failure, the export body's
  ``finally``) calls it, so a failed export can never leave a version pinned forever.
* ``build_bundle(pin, dest, ...)`` — reads ONLY ``pin.vdir`` (+ the workspace flowcharts when
  asked) and writes ONLY under ``dest``: rasters are rendered into the bundle, never into the
  immutable version dir. Accounting is per figure × version × format (ok / skip / fail); a
  missing or empty source is ``fail``, never silently packaged.
* ``zip_dir(dest, archive)`` — atomic (temp + ``os.replace``).

OCP-free: DXF→PNG via ``patent_figure.sheet`` (matplotlib, lazy), DWG via a caller's ``run_cli``.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from patent_figure import versions as _V

SCRIPTS = Path(__file__).resolve().parents[1]

VARIANT_DIRS = {"raw": "原始出图", "annotated": "交底版_带件号表", "filing": "递交版_仅图号"}
VARIANT_SUFFIX = {"raw": "", "annotated": "_annotated", "filing": "_filing"}
VARIANT_LABEL = {"raw": "原始", "annotated": "交底版", "filing": "递交版"}
#: REST/MCP ``variant`` parameter → the variants it packages.
REST_VARIANTS = {"filing": ("filing",), "disclosure": ("annotated",),
                 "both": ("annotated", "filing")}
DOCX_NAME = "说明书附图_交底版.docx"
NUMERALS_NAME = "reference-numerals.json"
FLOW_DIR = "流程图"


class ExportRefused(Exception):
    """No exportable version: missing, unsealed, failing verify, or not ok. → HTTP 409."""


class Pin:
    """A verified, reference-counted hold on one immutable version directory."""

    def __init__(self, store: "_V.VersionStore", rid: str, vdir: Path) -> None:
        self.store = store
        self.rid = str(rid)
        self.vdir = Path(vdir)
        self.result: Dict[str, Any] = store.read_result(self.rid) or {}
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self.store.release(self.rid)

    @property
    def released(self) -> bool:
        return self._released

    def __enter__(self) -> "Pin":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire(ws_root, rid: Optional[str] = None) -> Pin:
    """Verify + pin a version atomically (see module docstring). ``rid=None`` → current."""
    vs = _V.VersionStore(ws_root)
    try:
        rid, vdir = vs.acquire_verified_version(rid)
    except ValueError as exc:
        raise ExportRefused(str(exc))
    try:
        return Pin(vs, rid, vdir)
    except BaseException:
        vs.release(rid)
        raise


def _nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _png(src_dxf: Path, png: Path, dpi: int) -> None:
    from patent_figure import sheet as _sheet       # matplotlib is imported lazily inside
    _sheet.render_preview(src_dxf, png, dpi=dpi)


def write_docx(pngs: Sequence[Path], path: Path) -> None:
    """说明书附图.docx：每图一页，A4 版心等比缩放（python-docx + Pillow，可选依赖）。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm
    from PIL import Image
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.top_margin, sec.left_margin = Cm(2.5), Cm(2.5)
    sec.bottom_margin, sec.right_margin = Cm(1.5), Cm(1.5)
    max_w, max_h = 17.0, 25.0
    for i, png in enumerate(pngs):
        with Image.open(png) as im:
            w, h = im.size
        width = min(max_w, max_h * w / h)
        para = doc.paragraphs[0] if i == 0 and doc.paragraphs else doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        para.add_run().add_picture(str(png), width=Cm(width))
        if i < len(pngs) - 1:
            doc.add_page_break()
    doc.save(str(path))


def _dwg(src_dxf: Path, sub: Path, run_cli: Callable, keep_dxf: bool) -> Tuple[bool, str]:
    """Convert one DXF to DWG inside the bundle folder ``sub`` (never next to the version's
    file). AutoCAD Core first, LibreDWG as fallback."""
    local = sub / src_dxf.name
    copied = False
    if not local.is_file():
        shutil.copy2(src_dxf, local)
        copied = True
    dwg = local.with_suffix(".dwg")
    try:
        proc = run_cli([str(SCRIPTS / "autocad_core_dxf_to_dwg.py"), str(local), str(dwg)],
                       timeout=600)
        how = "AutoCAD 转换成功"
        if proc.returncode != 0 or not dwg.is_file():
            proc = run_cli([str(SCRIPTS / "libredwg_dxf_to_dwg.py"), str(local), "-o", str(sub)],
                           timeout=600)
            how = "AutoCAD 失败，改用 LibreDWG（exit=%d）" % proc.returncode
    finally:
        if copied and not keep_dxf:
            try:
                local.unlink()
            except OSError:
                pass
    return _nonempty(dwg), how


def build_bundle(pin: Pin, dest: Path, *, variants: Iterable[str] = ("annotated", "filing"),
                 formats: Iterable[str] = ("docx", "dxf"),
                 raw_formats: Optional[Iterable[str]] = None, dpi: int = 300,
                 run_cli: Optional[Callable] = None,
                 flows: Iterable[Tuple[str, Path, dict]] = (),
                 description_txt: bool = False) -> Dict[str, Any]:
    """Build an export bundle from ONE pinned version into ``dest``.

    ``variants`` ⊆ {raw, annotated, filing}; ``formats`` ⊆ {dxf, svg, png, docx, dwg, pdf}
    (``raw_formats`` overrides the list for the raw variant). ``flows`` = ``(id, out_dir,
    result)`` of flowcharts to include under 流程图/ (workspace-level, not version-bound).
    Returns ``{manifest, files, notes, dwg_log, figure_descriptions}`` where ``manifest`` maps
    ``<fig>/<版本>/<fmt>`` → ``ok`` / ``skip`` (not available: pdf → WP2, Word of a
    non-disclosure version, DWG of the raw figure) / ``fail`` (source missing/empty or the
    conversion failed)."""
    vdir, result = pin.vdir, pin.result
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    formats = list(formats)
    raw_formats = list(raw_formats) if raw_formats is not None else formats
    # workbench results mark a delivered figure ``pass``; direct-CLI results ``gate_pass``
    figs = sorted([f for f in result.get("figures", [])
                   if (f.get("pass") if "pass" in f else f.get("gate_pass"))],
                  key=lambda f: (f.get("number") or 999, str(f.get("id"))))
    manifest: Dict[str, str] = {}
    notes: List[str] = []
    dwg_log: List[str] = []
    docx_png: Dict[str, Path] = {}
    for fig in figs:
        fid = str(fig["id"])
        for variant in variants:
            suf, label = VARIANT_SUFFIX[variant], VARIANT_LABEL[variant]
            sub = dest / VARIANT_DIRS[variant]
            src_dxf = vdir / (fid + suf + ".dxf")
            fmts = raw_formats if variant == "raw" else formats
            for fmt in fmts:
                key = "%s/%s/%s" % (fid, label, fmt)
                if fmt == "pdf":
                    manifest[key] = "skip"                       # F1 PDF 在 WP2
                    continue
                if fmt == "docx" and variant != "annotated":
                    manifest[key] = "skip"                       # 仅交底版有 Word (R1b #7)
                    continue
                if fmt == "dwg" and variant == "raw":
                    manifest[key] = "skip"
                    continue
                src = vdir / (fid + suf + (".svg" if fmt == "svg" else ".dxf"))
                if not _nonempty(src):                           # never package a hole
                    manifest[key] = "fail"
                    notes.append("%s：版本内源文件缺失或为空（%s）" % (key, src.name))
                    continue
                sub.mkdir(parents=True, exist_ok=True)
                if fmt in ("dxf", "svg"):
                    shutil.copy2(src, sub / src.name)
                    manifest[key] = "ok" if _nonempty(sub / src.name) else "fail"
                elif fmt in ("png", "docx"):
                    png = sub / (fid + suf + ".png")
                    try:
                        if not _nonempty(png):
                            _png(src_dxf, png, dpi)
                    except Exception as exc:
                        notes.append("%s：PNG 生成失败：%s" % (key, exc))
                    if not _nonempty(png):
                        manifest[key] = "fail"
                    elif fmt == "png":
                        manifest[key] = "ok"
                    else:
                        docx_png[fid] = png
                        manifest[key] = "pending"
                elif fmt == "dwg":
                    if run_cli is None:
                        manifest[key] = "skip"
                        continue
                    ok, how = _dwg(src_dxf, sub, run_cli, keep_dxf="dxf" in fmts)
                    manifest[key] = "ok" if ok else "fail"
                    dwg_log.append("%s/%s: %s" % (VARIANT_DIRS[variant], src_dxf.stem,
                                                  how if ok else how + "，转换失败"))
                else:
                    manifest[key] = "skip"
                    notes.append("%s：未知格式" % key)

    flows = [(fid, Path(odir), res or {}) for fid, odir, res in flows]
    flow_pngs = []
    for fid, odir, _res in flows:
        for ext in (".dxf", ".png", ".svg"):
            src = odir / (fid + ext)
            if _nonempty(src):
                (dest / FLOW_DIR).mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / FLOW_DIR / src.name)
                if ext == ".png":
                    flow_pngs.append(dest / FLOW_DIR / src.name)

    # 交底版 Word: one page per figure whose PNG rendered (+ flowcharts); each figure's docx item
    # is ok only if it actually made it into the document (R1b #7 — no blanket ok).
    if "docx" in formats and (docx_png or flow_pngs):
        ordered = [docx_png[str(f["id"])] for f in figs if str(f["id"]) in docx_png]
        included = set()
        try:
            write_docx(ordered + flow_pngs, dest / DOCX_NAME)
            if _nonempty(dest / DOCX_NAME):
                included = set(docx_png)
        except Exception as exc:                 # python-docx 不在时只少一个文件
            notes.append("Word 未生成：%s" % exc)
        for fid in docx_png:
            manifest["%s/交底版/docx" % fid] = "ok" if fid in included else "fail"

    numerals_src = vdir / NUMERALS_NAME
    numerals_doc: Dict[str, Any] = {}
    if _nonempty(numerals_src):
        shutil.copy2(numerals_src, dest / NUMERALS_NAME)
        try:
            numerals_doc = json.loads(numerals_src.read_text(encoding="utf-8"))
        except ValueError:
            numerals_doc = {}
    desc = [(f.get("annotation") or {}).get("figure_description", "") for f in figs]
    desc += [(res or {}).get("figure_description", "") for _fid, _odir, res in flows]
    desc = [d for d in desc if d]
    if desc:
        desc[-1] = desc[-1].rstrip("；") + "。"
    if description_txt:
        text = ("【附图说明】\n" + "\n".join(desc) + "\n\n"
                + (numerals_doc.get("description_zh") or "") + "\n")
        (dest / "附图说明.txt").write_text(text, encoding="utf-8")
    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    return {"manifest": manifest, "files": files, "notes": notes, "dwg_log": dwg_log,
            "figure_descriptions": desc, "render_job_id": pin.rid}


def zip_dir(src: Path, archive: Path) -> Path:
    """Zip ``src`` into ``archive`` atomically (temp + os.replace). No ``shutil.make_archive``:
    it calls ``os.getcwd()``, which raises when the service's cwd is unreadable."""
    src, archive = Path(src), Path(archive)
    tmp = archive.with_name("%s.%d.%s.tmp" % (archive.name, os.getpid(), os.urandom(4).hex()))
    try:
        with zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    zf.write(str(f), f.relative_to(src).as_posix())
        os.replace(str(tmp), str(archive))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return archive
