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
_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
_PIC_URI = "http://schemas.openxmlformats.org/drawingml/2006/picture"
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
    if px[0] > 0 and px[1] > 0 and w > 0 and h > 0 and \
            abs((px[0] / px[1]) / (w / h) - 1.0) > SCALE_TOL:
        errors.append("图片像素宽高比 %d×%d 与显示尺寸 %.1f×%.1f mm 不一致（被拉伸）"
                      % (px[0], px[1], w, h))
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
    rep = _report(path, "pdf", sources, pages, [], len(pdf_pages))
    rep.update(layout_verified="file-geometry",
               layout_note="PDF 的页幅与图片位置取自文件里的绘制指令（CTM）与图片像素")
    return rep


# --------------------------------------------------------------------------- Word


def _q(ns: str, tag: str) -> str:
    return "{%s}%s" % (ns, tag)


#: Paragraph properties a page paragraph may carry (all others — styles, numbering, frames,
#: borders, tabs, section breaks … — can move or resize the picture and are refused).
_PPR_ALLOWED = {"pageBreakBefore", "spacing", "jc", "ind", "rPr"}
_RUN_FORBIDDEN = ("t", "br", "cr", "tab", "fldChar", "instrText", "sym", "ptab",
                  "lastRenderedPageBreak", "object", "pict")
_SETTINGS_FORBIDDEN = ("mirrorMargins", "gutterAtTop", "bookFoldPrinting", "bookFoldRevPrinting",
                       "printTwoOnOne")
WORD_LAYOUT_NOTE = "Word 按文档结构核验，实际排版以 Word 打开为准"


def _local(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _wattr(el, name: str, default=None):
    return el.get(_q(_W, name), default) if el is not None else default


def _twip_mm(v) -> float:
    return float(v) / TWIP_PER_MM


def _on(el) -> bool:
    """A WordprocessingML on/off element: present and not val=0/false/off."""
    return el is not None and str(_wattr(el, "val", "1")).lower() not in ("0", "false", "off")


#: Indent / spacing attributes in CHARACTER or LINE units: they take precedence over the twip
#: values and resize with the font, so a zero twip indent says nothing while one is set.
_UNIT_ATTRS = ("leftChars", "rightChars", "startChars", "endChars", "firstLineChars",
               "hangingChars", "beforeLines", "afterLines")


def _unit_attrs(el) -> List[str]:
    return [a for a in _UNIT_ATTRS if el is not None and float(_wattr(el, a, 0) or 0) != 0]


def _inherited_layout(styles) -> List[str]:
    """Formatting a page paragraph would INHERIT (docDefaults + the default paragraph style —
    page paragraphs carry no pStyle) that the explicit direct formatting does not neutralise:
    frames, numbering, borders, sections, character/line-unit indents or spacing."""
    errors: List[str] = []
    if styles is None:
        return errors
    sources = []
    dd = styles.find(_q(_W, "docDefaults"))
    if dd is not None:
        sources.append(("文档默认段落格式", dd.find("%s/%s" % (_q(_W, "pPrDefault"), _q(_W, "pPr")))))
    for st in styles.iter(_q(_W, "style")):
        if _wattr(st, "type") == "paragraph" and str(_wattr(st, "default", "0")) in ("1", "true"):
            sources.append(("默认段落样式「%s」" % _wattr(st, "styleId", "?"), st.find(_q(_W, "pPr"))))
            if st.find(_q(_W, "basedOn")) is not None:
                errors.append("默认段落样式继承自别的样式，继承格式无法确定")
    for label, ppr in sources:
        if ppr is None:
            continue
        for tag in ("framePr", "numPr", "pBdr", "sectPr", "textDirection"):
            if ppr.find(_q(_W, tag)) is not None:
                errors.append("%s带 %s（会移动或改变图片）" % (label, tag))
        for tag in ("ind", "spacing"):
            bad = _unit_attrs(ppr.find(_q(_W, tag)))
            if bad:
                errors.append("%s带字符/行单位的缩进或间距 %s（优先于显式置零的值）" % (label, bad))
    return errors


def _picture_whitelist(inl, where: str) -> List[str]:
    """The picture inside ``wp:inline`` must be EXACTLY what the writer produces: one ``pic:pic``,
    a plain blip (no effects), stretched to the full frame with NO crop, a rectangle with an
    un-rotated, un-flipped transform at the origin whose size equals ``wp:extent`` — otherwise the
    displayed picture is not the media file mapped onto the extent (R2b #2)."""
    errors: List[str] = []
    graphic = inl.find(_q(_A, "graphic"))
    gdata = graphic.find(_q(_A, "graphicData")) if graphic is not None else None
    pics = gdata.findall(_q(_PIC, "pic")) if gdata is not None else []
    if gdata is None or gdata.get("uri") != _PIC_URI or len(pics) != 1 or len(list(gdata)) != 1:
        return ["%s的图形不是单张图片（DrawingML picture）" % where]
    pic = pics[0]
    ext = inl.find(_q(_WP, "extent"))
    bf = pic.find(_q(_PIC, "blipFill"))
    if bf is None:
        return ["%s的图片缺少填充（blipFill）" % where]
    for el in bf:
        name = _local(el)
        if name == "blip":
            if len(list(el)):
                errors.append("%s的图片带图像效果 %s" % (where, [_local(c) for c in el]))
        elif name == "srcRect":
            if any(float(el.get(a, 0) or 0) != 0 for a in ("l", "t", "r", "b")):
                errors.append("%s的图片被裁剪（srcRect %s）" % (where, dict(el.attrib)))
        elif name == "stretch":
            fr = el.find(_q(_A, "fillRect"))
            if fr is None or len(list(el)) != 1 or \
                    any(float(fr.get(a, 0) or 0) != 0 for a in ("l", "t", "r", "b")):
                errors.append("%s的图片不是满框拉伸（fillRect 有偏移）" % where)
        else:
            errors.append("%s的图片填充方式不受控 <%s>" % (where, name))
    if bf.find(_q(_A, "stretch")) is None:
        errors.append("%s的图片不是满框拉伸" % where)
    sppr = pic.find(_q(_PIC, "spPr"))
    kids = [_local(c) for c in (list(sppr) if sppr is not None else [])]
    if sppr is None or sorted(kids) != ["prstGeom", "xfrm"]:
        errors.append("%s的图片形状属性不受控 %s" % (where, kids))
        return errors
    geom = sppr.find(_q(_A, "prstGeom"))
    if geom.get("prst") != "rect":
        errors.append("%s的图片形状不是矩形（%s）" % (where, geom.get("prst")))
    xfrm = sppr.find(_q(_A, "xfrm"))
    if float(xfrm.get("rot", 0) or 0) != 0:
        errors.append("%s的图片被旋转（rot=%s）" % (where, xfrm.get("rot")))
    for a in ("flipH", "flipV"):
        if str(xfrm.get(a, "0")).lower() in ("1", "true"):
            errors.append("%s的图片被翻转（%s）" % (where, a))
    off, xext = xfrm.find(_q(_A, "off")), xfrm.find(_q(_A, "ext"))
    if off is None or float(off.get("x", 0) or 0) != 0 or float(off.get("y", 0) or 0) != 0:
        errors.append("%s的图片内部有偏移（a:off）" % where)
    if xext is None or ext is None or xext.get("cx") != ext.get("cx") \
            or xext.get("cy") != ext.get("cy"):
        errors.append("%s的图片内部变换尺寸与显示尺寸（wp:extent）不一致" % where)
    return errors


def _docx_structure(root, settings, styles, sp: Dict[str, Any]) -> Tuple[List[str], dict, list]:
    """STRICT structural equivalence with what ``deliverable.write_docx`` writes (WP2 R2 #3).
    A Word file is only accepted when its layout is fully determined by its structure: one
    section with exactly the specified A4 page and margins, no headers/footers/columns/gutter,
    and a body of page paragraphs only — each with explicit single spacing (0 before/after,
    line 240 auto), zero indentation, centred, ``pageBreakBefore`` on every paragraph but the
    first, and exactly ONE inline picture with no effect extent / distance. Any deviation (an
    extra paragraph or page break, an indent, a style, a frame, a stray run …) → error. Returns
    ``(errors, geometry, pictures)``; position is COMPUTED from these properties, never assumed."""
    errors: List[str] = []
    body = root.find(_q(_W, "body"))
    if body is None:
        return ["Word 缺少正文（body）"], {}, []
    if len(list(root.iter(_q(_W, "sectPr")))) != 1:
        errors.append("Word 必须是单节文档（发现 %d 个节属性）" % len(list(root.iter(_q(_W, "sectPr")))))
    kids = list(body)
    sect = kids[-1] if kids and _local(kids[-1]) == "sectPr" else None
    if sect is None:
        return errors + ["Word 缺少节属性（sectPr）"], {}, []
    for el in kids[:-1]:
        if _local(el) != "p":
            errors.append("正文里出现图片段落以外的元素 <%s>" % _local(el))
    # ---- section: page, margins, no headers/footers/columns/gutter/grid snapping -------------
    sz, mar = sect.find(_q(_W, "pgSz")), sect.find(_q(_W, "pgMar"))
    if sz is None or mar is None:
        return errors + ["节属性缺少页幅或页边"], {}, []
    pw, ph = _twip_mm(_wattr(sz, "w", 0)), _twip_mm(_wattr(sz, "h", 0))
    if abs(pw - _D.PAGE_MM[0]) > TOL_MM or abs(ph - _D.PAGE_MM[1]) > TOL_MM \
            or str(_wattr(sz, "orient", "portrait")) != "portrait":
        errors.append("页幅 %.1f×%.1f mm 不是 A4 竖放" % (pw, ph))
    margins = {k: _twip_mm(_wattr(mar, k, 0)) for k in ("top", "left", "bottom", "right")}
    for k, need in sp["margins_mm"].items():
        if abs(margins[k] - need) > 0.2:
            errors.append("页边「%s」%.1f mm 与规范 %g mm 不一致" % (k, margins[k], need))
    if abs(_twip_mm(_wattr(mar, "gutter", 0))) > 0.05:
        errors.append("页边有装订线（gutter），实际左页边会变")
    for tag in ("headerReference", "footerReference"):
        if sect.find(_q(_W, tag)) is not None:
            errors.append("节里有页眉/页脚引用，可能挤压版心")
    cols = sect.find(_q(_W, "cols"))
    if cols is not None and int(_wattr(cols, "num", 1) or 1) != 1:
        errors.append("节被分栏")
    grid = sect.find(_q(_W, "docGrid"))
    if grid is not None and str(_wattr(grid, "type", "default")) != "default":
        errors.append("节启用了文档网格（会改变行高）")
    allowed_sect = {"pgSz", "pgMar", "cols", "docGrid"}
    for el in sect:
        if _local(el) not in allowed_sect:
            errors.append("节属性里有额外设置 <%s>" % _local(el))
    if settings is not None:
        for tag in _SETTINGS_FORBIDDEN:
            if settings.find(_q(_W, tag)) is not None and _on(settings.find(_q(_W, tag))):
                errors.append("文档设置启用了 %s（改变页面布局）" % tag)
    errors += _inherited_layout(styles)       # page paragraphs carry no pStyle → Normal + defaults
    # ---- page paragraphs ---------------------------------------------------------------------
    paras = [el for el in kids[:-1] if _local(el) == "p"]
    pictures = []
    for i, p in enumerate(paras):
        where = "第 %d 段" % (i + 1)
        ppr = p.find(_q(_W, "pPr"))
        props = {_local(c): c for c in (list(ppr) if ppr is not None else [])}
        extra = sorted(set(props) - _PPR_ALLOWED)
        if extra:
            errors.append("%s带不允许的段落属性 %s（样式/编号/框架/边框会移动图片）" % (where, extra))
        sp_el = props.get("spacing")
        if sp_el is None or any(str(_wattr(sp_el, a, d)) != v for a, d, v in (
                ("before", "0", "0"), ("after", "0", "0"), ("line", "", "240"),
                ("lineRule", "", "auto"))) or _on(sp_el.find(_q(_W, "x"))) \
                or str(_wattr(sp_el, "beforeAutospacing", "0")) not in ("0", "false") \
                or str(_wattr(sp_el, "afterAutospacing", "0")) not in ("0", "false"):
            errors.append("%s的段落间距不是「段前 0、段后 0、单倍行距」" % where)
        ind = props.get("ind")
        if ind is None or any(float(_wattr(ind, a, 0) or 0) != 0 for a in (
                "left", "right", "start", "end", "firstLine", "hanging")) or _unit_attrs(ind):
            errors.append("%s有缩进（或未显式置零、或用了字符单位缩进），图片位置会偏移" % where)
        if _unit_attrs(sp_el):
            errors.append("%s用了行单位的段前/段后间距 %s" % (where, _unit_attrs(sp_el)))
        jc = props.get("jc")
        if jc is None or str(_wattr(jc, "val")) != "center":
            errors.append("%s不是居中对齐" % where)
        pbb = props.get("pageBreakBefore")
        if i == 0 and _on(pbb):
            errors.append("第 1 段带「段前分页」，会多出一页空白")
        if i > 0 and not _on(pbb):
            errors.append("第 %d 张图片前没有分页（段前分页），两张图会落在同一页" % (i + 1))
        for el in p:
            if _local(el) not in ("pPr", "r"):
                errors.append("%s里有额外元素 <%s>" % (where, _local(el)))
        for tag in _RUN_FORBIDDEN:
            hits = list(p.iter(_q(_W, tag)))
            if hits:
                if tag == "t" and "".join(h.text or "" for h in hits).strip():
                    errors.append("出现图片以外的文字段落（「%s」），会多出页面或挤动图片"
                                  % "".join(h.text or "" for h in hits).strip()[:20])
                elif tag == "br":
                    errors.append("%s里有换行/分页符，会多出页面" % where)
                elif tag not in ("t", "lastRenderedPageBreak"):
                    errors.append("%s里有额外内容 <%s>" % (where, tag))
        drawings = [r for r in p.findall(_q(_W, "r")) if r.find(_q(_W, "drawing")) is not None]
        inl = p.findall(".//" + _q(_WP, "inline"))
        if p.findall(".//" + _q(_WP, "anchor")):
            errors.append("%s的图片是浮动版式（无法确定页面位置），应为嵌入式" % where)
        if len(inl) != 1 or len(drawings) != 1:
            errors.append("%s应恰好含一张行内图片，实有 %d 张" % (where, len(inl)))
            continue
        run = drawings[0]
        for el in run:
            if _local(el) == "rPr" and len(list(el)):
                errors.append("%s图片所在文字段带字符格式（可能升降或缩放）" % where)
            elif _local(el) not in ("rPr", "drawing"):
                errors.append("%s图片所在文字段有额外内容 <%s>" % (where, _local(el)))
        for r in p.findall(_q(_W, "r")):
            if r is not run and any(_local(c) != "rPr" for c in r):
                errors.append("%s有额外的文字段" % where)
        el = inl[0]
        errors += _picture_whitelist(el, where)
        if any(float(el.get(a, 0) or 0) != 0 for a in ("distT", "distB", "distL", "distR")):
            errors.append("%s图片带环绕距离" % where)
        eff = el.find(_q(_WP, "effectExtent"))
        if eff is not None and any(float(eff.get(a, 0) or 0) != 0 for a in ("l", "t", "r", "b")):
            errors.append("%s图片带效果外扩（effectExtent），实际占位变大" % where)
        pictures.append((i, p, el))
    geo = {"page_mm": (pw, ph), "margins": margins,
           "body_mm": (pw - margins["left"] - margins["right"],
                       ph - margins["top"] - margins["bottom"])}
    return errors, geo, pictures


def check_docx(path: Path, sources: Sequence[Source], sp: Dict[str, Any]) -> Dict[str, Any]:
    """Word：严格结构等价核验（见 ``_docx_structure``）+ 逐页像素/字高/图号检查。页数 = 结构上
    每段一页（只有在结构完全受控时这一推断才成立，否则直接判不通过）；图片位置由页边 + 居中 +
    零缩进 + 段前 0 计算得出。``layout_verified = "structural"``：没有用 Word 排版引擎实测。"""
    import numpy as np
    from PIL import Image
    path = Path(path)
    try:
        with zipfile.ZipFile(str(path)) as zf:          # read-only; everything read up front
            names = set(zf.namelist())
            parts = {n: zf.read(n) for n in names
                     if n in ("word/document.xml", "word/_rels/document.xml.rels",
                              "word/settings.xml", "word/styles.xml")
                     or n.startswith("word/media/")}
        root = ET.fromstring(parts["word/document.xml"])
        rels = ET.fromstring(parts["word/_rels/document.xml.rels"])
        settings = ET.fromstring(parts["word/settings.xml"]) if "word/settings.xml" in parts \
            else None
        styles = ET.fromstring(parts["word/styles.xml"]) if "word/styles.xml" in parts else None
    except Exception as exc:
        rep = _report(path, "docx", sources, [], ["Word 读不了：%s" % exc], -1)
        rep.update(layout_verified="structural", layout_note=WORD_LAYOUT_NOTE)
        return rep
    rel_target = {r.get("Id"): r.get("Target") for r in rels.iter(_q(_PKG_REL, "Relationship"))}
    errors, geo, pictures = _docx_structure(root, settings, styles, sp)
    pages = []
    if geo:
        (pw, ph), margins, (body_w, body_h) = geo["page_mm"], geo["margins"], geo["body_mm"]
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
            # computed, not assumed: centred (jc=center) with zero indents → horizontally centred
            # in the body; zero space before on a page-initial paragraph → top of the body.
            left = margins["left"] + (body_w - w) / 2.0
            px = (int(gray.shape[1]), int(gray.shape[0])) if gray is not None else (0, 0)
            pages.append(_page_check(k, src, sp, (left, margins["top"], w, h), px, gray,
                                     (pw, ph), extra))
    # One page per page-paragraph holds ONLY because the structure above is fully controlled; any
    # structural deviation is already an error, so this count is never trusted on its own.
    rep = _report(path, "docx", sources, pages, errors, len(pictures))
    rep.update(layout_verified="structural", layout_note=WORD_LAYOUT_NOTE)
    return rep


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
    from patent_figure import pathguard
    problem = pathguard.check_outputs([("--json", args.json_out)],
                                      inputs=[args.document] + list(args.sources))
    if problem:
        print("用法错误：%s" % problem)
        return 2
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
