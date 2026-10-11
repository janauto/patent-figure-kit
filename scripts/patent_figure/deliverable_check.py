"""最终文档逐页检查（F1，WP2）：对**已经写出的** PDF / Word 文件逐页核验，不信任写出端自报。

每个文件按「源图」（被导出版本里的规范版本 DXF，按图号排序）逐页核对：

* **页数 = 图数**；每页恰好一张图片；
* **页幅** A4（±0.5 mm）；**页边**：图片矩形完全落在 上 25 / 左 25 / 下 15 / 右 15 mm 之内
  （Word 另核 ``sectPr`` 的页边设置不小于要求），且不超出所选图区预设；
* **缩放后最小字高** = 源 DXF 最小字高 × 实际缩放比（页面上图片宽 ÷ 源图取景窗宽），须 ≥
  ``min_text_mm``；按文件里图片的**实际像素**算出有效 dpi，栅格字高（px）须 ≥ ``min_text_px``；
  宽高两向缩放比须一致（图片未被拉伸）；
* **图号存在**：第 k 页的源图图号文字必须是「图k」，并且在**文件里这张图片的像素上**，图号所在
  位置确有墨迹（不是只看源文件）。

任一项不满足 → 该页 / 该文件 ``ok=false`` 并给出原因（中文）。PDF 解析用可选依赖 ``pypdf``（没装就
如实报「无法核验」，不得报通过）；Word 用标准库 zipfile + ElementTree；像素解码用 Pillow
（matplotlib 的必需依赖）。OCP 无关。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

from patent_figure import deliverable as _D

TOL_MM = 0.5
SCALE_TOL = 0.01          # relative mismatch allowed between the x and y scale of a picture
INK_FRACTION_MIN = 0.015  # share of dark pixels inside the caption box that counts as "drawn"

PT_PER_MM = 72.0 / 25.4
EMU_PER_MM = 36000.0
TWIP_PER_MM = 1440.0 / 25.4

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def pdf_checker_available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------- source facts


class Source:
    """The facts of one source page, read from the SOURCE DXF (never from the writer)."""

    def __init__(self, figure: str, number: int, dxf: Path,
                 area_mm: Optional[Sequence[float]] = None) -> None:
        self.figure, self.number, self.dxf = str(figure), int(number), Path(dxf)
        doc = _D.read_doc(self.dxf)
        msp = doc.modelspace()
        self.window = _D.sheet_window(msp, area_mm)
        heights = [h for _layer, _t, h in _D.text_heights(msp) if h > 0]
        self.min_text_mm = min(heights) if heights else None
        self.caption = _D.caption_of(msp)

    @property
    def window_mm(self) -> Tuple[float, float]:
        return self.window[2] - self.window[0], self.window[3] - self.window[1]


def sources_from(rows: Sequence[Dict[str, Any]],
                 area_mm: Optional[Sequence[float]] = None) -> List[Source]:
    """``rows`` = ``[{figure, number, dxf}]`` → Sources in page (figure-number) order; the
    capture window depends on the chosen drawing-area preset (``area_mm``)."""
    rows = sorted(rows, key=lambda r: (int(r.get("number") or 0), str(r.get("figure"))))
    return [Source(r["figure"], r["number"], Path(r["dxf"]), area_mm) for r in rows]


# --------------------------------------------------------------------------- per-page core


def _page_check(k: int, src: Source, sp: Dict[str, Any], rect_mm: Tuple[float, float, float, float],
                px: Tuple[int, int], gray, page_mm: Tuple[float, float],
                extra_errors: Sequence[str] = ()) -> Dict[str, Any]:
    """``rect_mm`` = (left, top, width, height) of the picture on the page, origin top-left."""
    errors = list(extra_errors)
    left, top, w, h = rect_mm
    pw, ph = page_mm
    m = sp["margins_mm"]
    if abs(pw - _D.PAGE_MM[0]) > TOL_MM or abs(ph - _D.PAGE_MM[1]) > TOL_MM:
        errors.append("页幅 %.1f×%.1f mm 不是 A4（210×297）" % (pw, ph))
    if left < m["left"] - TOL_MM or top < m["top"] - TOL_MM \
            or left + w > pw - m["right"] + TOL_MM or top + h > ph - m["bottom"] + TOL_MM:
        errors.append("图片越过页边（图片在 左%.1f 上%.1f 宽%.1f 高%.1f mm；页边要求 上%g/左%g/下%g/右%g）"
                      % (left, top, w, h, m["top"], m["left"], m["bottom"], m["right"]))
    aw, ah = sp["area_mm"]
    if w > aw + TOL_MM or h > ah + TOL_MM:
        errors.append("图片 %.1f×%.1f mm 超出图区预设 %s（%g×%g mm）" % (w, h, sp["preset"], aw, ah))
    ww, wh = src.window_mm
    sx, sy = w / ww, h / wh
    scale = min(sx, sy)
    if abs(sx - sy) > SCALE_TOL * max(sx, sy):
        errors.append("图片被拉伸（横向缩放 %.4f ≠ 纵向 %.4f）" % (sx, sy))
    dpi_eff = min(px[0] / (w / 25.4), px[1] / (h / 25.4)) if w > 0 and h > 0 else 0.0
    min_mm = min_px = None
    if src.min_text_mm is None:
        errors.append("源图里没有文字（图号缺失）")
    else:
        min_mm = src.min_text_mm * scale
        min_px = min_mm / 25.4 * dpi_eff
        if min_mm < sp["min_text_mm"] - 1e-6:
            errors.append("缩放后最小字高 %.2f mm < %.2f mm（源图 %.2f mm × 缩放 %.4f）"
                          % (min_mm, sp["min_text_mm"], src.min_text_mm, scale))
        if min_px < sp["min_text_px"] - 1e-6:
            errors.append("栅格上最小字高 %.1f px < %.0f px（有效 %.0f dpi）——提高 dpi"
                          % (min_px, sp["min_text_px"], dpi_eff))
    want = "图%d" % k
    cap = src.caption
    ink = None
    if cap is None or cap["text"].replace(" ", "") != want:
        errors.append("第 %d 页的图号应为「%s」，源图图号为「%s」"
                      % (k, want, (cap or {}).get("text", "无")))
    elif gray is None:
        errors.append("读不出第 %d 页的图片像素，无法确认图号已画上" % k)
    else:
        bx0, by0, bx1, by1 = _D.caption_box(cap)
        wx0, wy0, wx1, wy1 = src.window
        H, W = gray.shape[:2]
        c0 = int(max(0, (bx0 - wx0) / (wx1 - wx0) * W))
        c1 = int(min(W, (bx1 - wx0) / (wx1 - wx0) * W + 1))
        r0 = int(max(0, (wy1 - by1) / (wy1 - wy0) * H))
        r1 = int(min(H, (wy1 - by0) / (wy1 - wy0) * H + 1))
        crop = gray[r0:r1, c0:c1]
        ink = float((crop < _D.INK_LEVEL).mean()) if crop.size else 0.0
        if ink < INK_FRACTION_MIN:
            errors.append("第 %d 页图片上「%s」的位置没有墨迹（%.1f%%），图号未画进页面"
                          % (k, want, ink * 100))
    return {"page": k, "figure": src.figure, "ok": not errors, "errors": errors,
            "page_mm": [round(pw, 2), round(ph, 2)],
            "image_mm": {"left": round(left, 2), "top": round(top, 2),
                         "width": round(w, 2), "height": round(h, 2)},
            "image_px": [int(px[0]), int(px[1])], "dpi_effective": round(dpi_eff, 1),
            "scale": round(scale, 4),
            "min_text_mm": None if min_mm is None else round(min_mm, 3),
            "min_text_px": None if min_px is None else round(min_px, 1),
            "caption": (cap or {}).get("text"), "caption_ink": None if ink is None else round(ink, 4)}


def _report(path: Path, fmt: str, sources: Sequence[Source], pages: List[dict],
            errors: List[str], n_pages: int) -> Dict[str, Any]:
    if n_pages >= 0 and n_pages != len(sources):     # n_pages < 0: the file was unreadable
        errors.insert(0, "页数 %d ≠ 图数 %d" % (n_pages, len(sources)))
    ok = not errors and all(p["ok"] for p in pages) and n_pages == len(sources)
    return {"file": Path(path).name, "format": fmt, "ok": ok, "pages": max(n_pages, 0),
            "expected_pages": len(sources), "errors": errors, "page_checks": pages}


# --------------------------------------------------------------------------- PDF


def _mat_mul(a, b):
    """PDF 3x2 matrices [a b c d e f]: result = a × b (apply a, then b)."""
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _pdf_images(page) -> List[Tuple[str, List[float]]]:
    """``(xobject name, CTM)`` for every image painted by the page's content stream (q/Q/cm)."""
    from pypdf.generic import NameObject
    res = page.get("/Resources") or {}
    res = res.get_object() if hasattr(res, "get_object") else res
    xobjs = res.get("/XObject") or {}
    xobjs = xobjs.get_object() if hasattr(xobjs, "get_object") else xobjs
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    stack = []
    out = []
    contents = page.get_contents()
    if contents is None:
        return out
    for operands, op in contents.operations:
        if op == b"q":
            stack.append(list(ctm))
        elif op == b"Q":
            ctm = stack.pop() if stack else [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        elif op == b"cm" and len(operands) == 6:
            ctm = _mat_mul([float(v) for v in operands], ctm)
        elif op == b"Do" and operands:
            name = str(operands[0])
            xo = xobjs.get(NameObject(name)) if xobjs else None
            xo = xo.get_object() if hasattr(xo, "get_object") else xo
            if xo is not None and xo.get("/Subtype") == "/Image":
                out.append((name, list(ctm)))
    return out


def _pdf_gray(page, name: str):
    import numpy as np
    target = name.lstrip("/")
    for im in page.images:
        stem = str(im.name).rsplit(".", 1)[0].lstrip("/")
        if stem == target:
            return np.asarray(im.image.convert("L"))
    return None


def check_pdf(path: Path, sources: Sequence[Source], sp: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(path)
    if not pdf_checker_available():
        return _report(path, "pdf", sources, [], ["未安装 pypdf，无法逐页核验 PDF（pip install pypdf）"
                                                   "——按规则不得报通过"], -1)
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(path))
        pdf_pages = list(reader.pages)
    except Exception as exc:
        return _report(path, "pdf", sources, [], ["PDF 读不了：%s" % exc], -1)
    pages = []
    for k, (page, src) in enumerate(zip(pdf_pages, sources), start=1):
        box = page.mediabox
        pw = float(box.width) / PT_PER_MM
        ph = float(box.height) / PT_PER_MM
        imgs = _pdf_images(page)
        if len(imgs) != 1:
            pages.append({"page": k, "figure": src.figure, "ok": False,
                          "errors": ["第 %d 页应恰好 1 张图片，实有 %d 张" % (k, len(imgs))]})
            continue
        name, m = imgs[0]
        # unit square through the CTM → page rectangle (PDF origin bottom-left, points)
        xs = [m[4], m[0] + m[4], m[2] + m[4], m[0] + m[2] + m[4]]
        ys = [m[5], m[1] + m[5], m[3] + m[5], m[1] + m[3] + m[5]]
        x0, x1 = min(xs) / PT_PER_MM, max(xs) / PT_PER_MM
        y0, y1 = min(ys) / PT_PER_MM, max(ys) / PT_PER_MM
        extra = []
        if abs(m[1]) > 1e-6 or abs(m[2]) > 1e-6:
            extra.append("图片被旋转或错切")
        gray = _pdf_gray(page, name)
        if gray is not None:
            # PDF image space: sample row 0 maps to the TOP of the unit square. A negative CTM
            # scale mirrors that axis on the page (matplotlib stores rows bottom-up and draws
            # with d < 0), so orient the samples as they are DISPLAYED before locating ink.
            import numpy as np
            if m[3] < 0:
                gray = np.flipud(gray)
            if m[0] < 0:
                gray = np.fliplr(gray)
        px = (int(gray.shape[1]), int(gray.shape[0])) if gray is not None else (0, 0)
        pages.append(_page_check(k, src, sp, (x0, ph - y1, x1 - x0, y1 - y0), px, gray,
                                 (pw, ph), extra))
    return _report(path, "pdf", sources, pages, [], len(pdf_pages))


# --------------------------------------------------------------------------- Word


def _q(ns: str, tag: str) -> str:
    return "{%s}%s" % (ns, tag)


def check_docx(path: Path, sources: Sequence[Source], sp: Dict[str, Any]) -> Dict[str, Any]:
    import numpy as np
    from PIL import Image
    path = Path(path)
    try:
        with zipfile.ZipFile(str(path)) as zf:          # read-only; everything read up front
            parts = {n: zf.read(n) for n in zf.namelist()
                     if n in ("word/document.xml", "word/_rels/document.xml.rels")
                     or n.startswith("word/media/")}
        root = ET.fromstring(parts["word/document.xml"])
        rels = ET.fromstring(parts["word/_rels/document.xml.rels"])
    except Exception as exc:
        return _report(path, "docx", sources, [], ["Word 读不了：%s" % exc], -1)
    rel_target = {r.get("Id"): r.get("Target") for r in rels.iter(_q(_PKG_REL, "Relationship"))}
    body = root.find(_q(_W, "body"))
    errors: List[str] = []
    sect = body.find(_q(_W, "sectPr")) if body is not None else None
    if sect is None:
        return _report(path, "docx", sources, [], ["Word 缺少节属性（sectPr）"], -1)
    sz, mar = sect.find(_q(_W, "pgSz")), sect.find(_q(_W, "pgMar"))
    pw = float(sz.get(_q(_W, "w"))) / TWIP_PER_MM
    ph = float(sz.get(_q(_W, "h"))) / TWIP_PER_MM
    margins = {k: float(mar.get(_q(_W, k))) / TWIP_PER_MM for k in ("top", "left", "bottom", "right")}
    for k, need in sp["margins_mm"].items():
        if margins[k] < need - TOL_MM:
            errors.append("页边「%s」%.1f mm 小于要求 %g mm" % (k, margins[k], need))
    body_w = pw - margins["left"] - margins["right"]
    body_h = ph - margins["top"] - margins["bottom"]

    pictures = []          # (paragraph index, inline element)
    paras = [p for p in body if p.tag == _q(_W, "p")]
    for i, p in enumerate(paras):
        inl = p.findall(".//" + _q(_WP, "inline"))
        anc = p.findall(".//" + _q(_WP, "anchor"))
        if anc:
            errors.append("第 %d 段的图片是浮动版式（无法确定页面位置），应为嵌入式" % (i + 1))
        text = "".join(t.text or "" for t in p.iter(_q(_W, "t"))).strip()
        if text:
            errors.append("出现图片以外的文字段落（「%s」），会多出页面或挤动图片" % text[:20])
        for el in inl:
            pictures.append((i, p, el))
    for n, (i, p, _el) in enumerate(pictures):
        if n == 0:
            continue
        ppr = p.find(_q(_W, "pPr"))
        breaks = ppr is not None and ppr.find(_q(_W, "pageBreakBefore")) is not None
        prev_br = any(br.get(_q(_W, "type")) == "page"
                      for q in paras[pictures[n - 1][0]:i] for br in q.iter(_q(_W, "br")))
        if not (breaks or prev_br):
            errors.append("第 %d 张图片前没有分页，两张图会落在同一页" % (n + 1))
        if len(p.findall(".//" + _q(_WP, "inline"))) > 1:
            errors.append("同一段里有多张图片")
    pages = []
    for k, ((i, p, inl), src) in enumerate(zip(pictures, sources), start=1):
        ext = inl.find(_q(_WP, "extent"))
        w = float(ext.get("cx")) / EMU_PER_MM
        h = float(ext.get("cy")) / EMU_PER_MM
        blip = inl.find(".//" + _q(_A, "blip"))
        rid = blip.get(_q(_R, "embed")) if blip is not None else None
        target = rel_target.get(rid or "")
        gray = None
        extra = []
        if not target:
            extra.append("第 %d 页图片引用缺失" % k)
        else:
            name = target if target.startswith("word/") else "word/" + target.lstrip("/")
            try:
                with Image.open(io.BytesIO(parts[name])) as im:
                    gray = np.asarray(im.convert("L"))
            except Exception as exc:
                extra.append("第 %d 页图片解不开：%s" % (k, exc))
        if w > body_w + TOL_MM or h > body_h + TOL_MM:
            extra.append("图片 %.1f×%.1f mm 大于版心 %.1f×%.1f mm（会被挤到下一页或裁切）"
                         % (w, h, body_w, body_h))
        # inline pictures sit at the top of the body, centred horizontally (paragraph centre)
        left = margins["left"] + (body_w - w) / 2.0
        px = (int(gray.shape[1]), int(gray.shape[0])) if gray is not None else (0, 0)
        pages.append(_page_check(k, src, sp, (left, margins["top"], w, h), px, gray,
                                 (pw, ph), extra))
    return _report(path, "docx", sources, pages, errors, len(pictures))


# --------------------------------------------------------------------------- bundle


def check_files(dest: Path, expected: Sequence[str]) -> List[str]:
    """Expected bundle files that are missing or empty."""
    missing = []
    for rel in expected:
        p = Path(dest) / rel
        try:
            if not p.is_file() or p.stat().st_size == 0:
                missing.append(rel)
        except OSError:
            missing.append(rel)
    return missing


def check_document(path: Path, sources: Sequence[Source], sp: Dict[str, Any]) -> Dict[str, Any]:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return check_pdf(path, sources, sp)
    if suffix == ".docx":
        return check_docx(path, sources, sp)
    raise ValueError("只核验 .pdf / .docx：%s" % path)


def main(argv=None) -> int:
    """CLI: ``deliverable_check.py DOC --sources fig1_filing.dxf fig2_filing.dxf
    [--preset standard] [--json report.json]`` — sources in page order (图1, 图2 …)."""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="deliverable_check.py",
                                 description="逐页核验说明书附图 PDF / Word（页幅、页边、缩放后字高、图号、页数）")
    ap.add_argument("document")
    ap.add_argument("--sources", nargs="+", required=True,
                    help="按页序（图1、图2…）给出该文件对应的规范版本 DXF")
    ap.add_argument("--preset", default=_D.DEFAULT_PRESET, choices=sorted(_D.SHEET_PRESETS))
    ap.add_argument("--dpi", type=int, default=_D.DEFAULT_DPI,
                    help="只用于填写规格；有效 dpi 一律按文件里图片的实际像素计算")
    ap.add_argument("--min-text-mm", type=float, default=_D.MIN_TEXT_MM)
    ap.add_argument("--min-text-px", type=float, default=_D.MIN_TEXT_PX)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)
    sp = _D.spec(args.preset, args.dpi)
    sp["min_text_mm"], sp["min_text_px"] = args.min_text_mm, args.min_text_px
    srcs = [Source(Path(p).stem, i, Path(p), sp["area_mm"])
            for i, p in enumerate(args.sources, start=1)]
    rep = check_document(Path(args.document), srcs, sp)
    for pg in rep["page_checks"]:
        print("第 %d 页 %s %s：字高 %s mm / %s px，%s dpi，缩放 %s%s" % (
            pg["page"], pg.get("figure"), "通过" if pg["ok"] else "不通过",
            pg.get("min_text_mm"), pg.get("min_text_px"), pg.get("dpi_effective"),
            pg.get("scale"), ("；" + "；".join(pg["errors"])) if pg["errors"] else ""))
    for e in rep["errors"]:
        print("文件：%s" % e)
    print("%s：%s（%d/%d 页）" % (rep["file"], "通过" if rep["ok"] else "不通过",
                              rep["pages"], rep["expected_pages"]))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return 0 if rep["ok"] else 1
