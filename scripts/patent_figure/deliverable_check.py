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
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

from patent_figure import deliverable as _D

TOL_MM = 0.5
SCALE_TOL = 0.01          # relative mismatch allowed between the x and y scale of a picture
INK_FRACTION_MIN = 0.015  # share of dark pixels inside the caption box that counts as "drawn"

#: Verdicts of a checked document (WP2 R2j ruling). ``ok`` — every check passed, INCLUDING, for a
#: PDF, an independent render; ``fail`` — some check failed (or the file is not the exported
#: original); ``unverified`` — nothing failed, but a PDF could not be rendered (no renderer
#: installed) or not be parsed at all (no pypdf): never ``ok``, never a pass.
STATUS_OK, STATUS_FAIL, STATUS_UNVERIFIED = "ok", "fail", "unverified"
UNVERIFIED_PDF_REASON = "缺少 pypdfium2，无法核验 PDF 可见性"
UNVERIFIED_PDF_HINT = "（python3 -m pip install pypdfium2 后重新导出）"
TAMPERED_REASON = "文件已被修改，请使用导出包原文件"

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


_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_ALLOWED = {b"IHDR", b"pHYs", b"IDAT", b"IEND"}


def png_whitelist(data: bytes) -> List[str]:
    """The page image of a Word file must be EXACTLY what the writer produces (WP2 R2e): a
    single-frame, 8-bit grayscale, non-interlaced PNG made of IHDR, at most one pHYs, contiguous
    IDAT and an empty final IEND, every CRC valid. Alpha / tRNS / palettes / gamma / ICC /
    animation / text — any other chunk — is refused: such properties can make the visible page
    differ from the stored samples (a fully transparent picture looks blank)."""
    import struct
    import zlib
    if not data.startswith(_PNG_SIG):
        return ["不是 PNG"]
    errors: List[str] = []
    i, kinds = len(_PNG_SIG), []
    while i + 12 <= len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        kind = data[i + 4:i + 8]
        body = data[i + 8:i + 8 + length]
        crc = data[i + 8 + length:i + 12 + length]
        if len(body) != length or len(crc) != 4:
            return errors + ["PNG 块被截断"]
        if struct.unpack(">I", crc)[0] != (zlib.crc32(kind + body) & 0xFFFFFFFF):
            errors.append("PNG 块 %s 校验和不对" % kind.decode("latin-1"))
        kinds.append((kind, body))
        i += 12 + length
        if kind == b"IEND":
            break
    if i != len(data):
        errors.append("PNG 在 IEND 之后还有数据")
    names = [k for k, _b in kinds]
    unknown = sorted({k.decode("latin-1") for k in names if k not in _PNG_ALLOWED})
    if unknown:
        errors.append("PNG 带写出端不会产生的块 %s（透明度 / 调色板 / 色彩管理 / 动画 / 文本等）"
                      % unknown)
    if not names or names[0] != b"IHDR" or names[-1] != b"IEND" or kinds[-1][1]:
        errors.append("PNG 块顺序不对（须以 IHDR 开头、以空 IEND 结尾）")
    if names.count(b"IHDR") != 1 or names.count(b"pHYs") > 1 or names.count(b"IEND") != 1:
        errors.append("PNG 的 IHDR / pHYs / IEND 个数不对")
    idat = [n for n, k in enumerate(names) if k == b"IDAT"]
    if not idat or idat != list(range(idat[0], idat[0] + len(idat))):
        errors.append("PNG 的图像数据块缺失或不连续")
    if b"pHYs" in names and idat and names.index(b"pHYs") > idat[0]:
        errors.append("PNG 的 pHYs 在图像数据之后")
    if names and names[0] == b"IHDR" and len(kinds[0][1]) == 13:
        _w, _h, depth, ctype, comp, filt, inter = struct.unpack(">IIBBBBB", kinds[0][1])
        if (depth, ctype, comp, filt, inter) != (8, 0, 0, 0, 0):
            errors.append("PNG 不是 8 位灰度、无隔行（位深 %d、颜色类型 %d、隔行 %d）"
                          % (depth, ctype, inter))
    return errors


def visible_gray(im):
    """The page as a reader SEES it: any transparency composited onto white, then gray."""
    import numpy as np
    from PIL import Image
    t = im.info.get("transparency")
    if im.mode in ("L", "RGB", "I") and t is not None and not isinstance(t, bytes):
        # tRNS on a gray / truecolour image: one sample value is fully transparent. Pillow's
        # convert() does not apply it for these modes, so build the alpha explicitly.
        arr = np.asarray(im)
        hit = np.all(arr == np.asarray(t), axis=-1) if arr.ndim == 3 else (arr == t)
        rgba = im.convert("RGBA")
        alpha = np.where(hit, 0, 255).astype(np.uint8)
        rgba.putalpha(Image.fromarray(alpha, "L"))
    elif im.mode in ("LA", "RGBA", "PA", "La", "RGBa", "P") or t is not None:
        rgba = im.convert("RGBA")
    else:
        return np.asarray(im.convert("L"))
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    bg.alpha_composite(rgba)
    return np.asarray(bg.convert("L"))


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
    return {"file": Path(path).name, "format": fmt, "ok": ok, "status": STATUS_OK if ok else
            STATUS_FAIL, "pages": max(n_pages, 0), "expected_pages": len(sources),
            "errors": errors, "page_checks": pages}


# --------------------------------------------------------------------------- PDF


def _mat_mul(a, b):
    """PDF 3x2 matrices [a b c d e f]: result = a × b (apply a, then b)."""
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


#: The writer's page image XObject dictionary as pypdf presents it — a CLOSED key set (R2h):
#: every key required, nothing else allowed. (pypdf consumes ``/Length`` while it parses the
#: stream; a ``/Length`` that is not a PDF integer makes that parse fail, which ``check_pdf``
#: reports as a failure.)
_PDF_IMAGE_KEYS = {"/Type", "/Subtype", "/Width", "/Height", "/ColorSpace", "/BitsPerComponent",
                   "/Filter", "/DecodeParms"}
_PDF_IMAGE_MASK_KEYS = ("/SMask", "/Mask", "/ImageMask", "/Decode", "/OC", "/SMaskInData")
_PDF_DEVICE_CS = ("/DeviceGray", "/DeviceRGB")
_PDF_DECODE_KEYS = {"/Predictor", "/Colors", "/Columns"}       # + optional /BitsPerComponent
_PDF_PROCSET = ("/PDF", "/Text", "/ImageB", "/ImageC", "/ImageI")
#: Painting operators: after the page image nothing may be painted over it.
_PDF_PAINT = {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"S", b"s", b"sh", b"Do", b"Tj",
              b"TJ", b"'", b'"', b"BI", b"EI"}


def _obj(x):
    return x.get_object() if hasattr(x, "get_object") else x


def _pdf_image_whitelist(xo, where: str) -> List[str]:
    """The page image XObject must be EXACTLY the writer's kind (WP2 R2e, R2h): a closed key set,
    every value checked by its PDF TYPE before its value — an opaque 8-bit image, gray / RGB or an
    indexed palette over them, a single Flate filter with the writer's PNG-predictor parameters.
    Soft mask / mask / image mask / Decode remap / optional content / boolean flags such as
    ``/Interpolate`` — any key outside the set — are refused. A string ``(/Indexed)`` is not the
    name ``/Indexed`` to a renderer (PDFium then draws nothing) although ``str()`` says equal."""
    from pypdf.generic import (ArrayObject, ByteStringObject, NumberObject, StreamObject,
                               TextStringObject)
    img = "%s的图片" % where
    if not isinstance(xo, StreamObject):
        return ["%s不是 PDF 流对象（实为%s）" % (img, _pdf_kind(xo))]
    errors = []
    keys = set(str(k) for k in xo.keys())
    for k in _PDF_IMAGE_MASK_KEYS:
        if k in keys:
            errors.append("%s带 %s（透明 / 遮罩 / 重映射会改变可见内容）" % (img, k))
    errors += _v_keys(keys, img + "字典", _PDF_IMAGE_KEYS, skip=_PDF_IMAGE_MASK_KEYS)

    def get(key):
        return _obj(xo.get(key))
    errors += _v_name(get("/Type"), img + "的 /Type", ("/XObject",))
    errors += _v_name(get("/Subtype"), img + "的 /Subtype", ("/Image",))
    width = get("/Width")
    errors += _v_int(width, img + "的 /Width", lo=1)
    errors += _v_int(get("/Height"), img + "的 /Height", lo=1)
    errors += _v_int(get("/BitsPerComponent"), img + "的 /BitsPerComponent（须 8 位）", allowed=(8,))
    cs, colors = get("/ColorSpace"), None
    if isinstance(cs, ArrayObject):                       # [/Indexed base hival lookup]
        parts = [_obj(x) for x in cs]
        if len(parts) != 4:
            errors.append("%s的色彩空间数组不是 [/Indexed 基色 hival 查找表] 四项（实为 %d 项）"
                          % (img, len(parts)))
        else:
            e = _v_name(parts[0], img + "色彩空间数组的首项", ("/Indexed",))
            e += _v_name(parts[1], img + "调色板的基色空间", _PDF_DEVICE_CS)
            e += _v_int(parts[2], img + "调色板的 hival", lo=0, hi=255)
            lookup = parts[3]
            # a PDF string either way: pypdf calls it Text- or ByteString by whether its bytes
            # happen to decode; both pypdf's decoder and PDFium use the raw bytes
            raw = (lookup.original_bytes if isinstance(lookup, TextStringObject) else
                   bytes(lookup) if isinstance(lookup, ByteStringObject) else None)
            if raw is None:
                e.append("%s调色板查找表不是 PDF 字符串（实为%s）" % (img, _pdf_kind(lookup)))
            if not e:
                need = (int(parts[2]) + 1) * (1 if str(parts[1]) == "/DeviceGray" else 3)
                if len(raw) != need:
                    e.append("%s调色板查找表 %d 字节，与 hival / 基色空间要求的 %d 字节不符"
                             % (img, len(raw), need))
            errors += e
            colors = 1
    else:
        e = _v_name(cs, img + "的色彩空间", _PDF_DEVICE_CS)
        errors += e
        if not e:
            colors = 1 if str(cs) == "/DeviceGray" else 3
    flt = get("/Filter")
    if isinstance(flt, ArrayObject):
        for n, f in enumerate(flt, start=1):
            errors += _v_name(_obj(f), "%s的 /Filter 数组第 %d 项" % (img, n), ("/FlateDecode",))
        errors.append("%s的 /Filter 是数组——写出端只写单个名称 /FlateDecode" % img)
    else:
        errors += _v_name(flt, img + "的 /Filter", ("/FlateDecode",))
    if "/DecodeParms" in keys:
        dp = get("/DecodeParms")
        e = _v_dict(dp, img + "的 /DecodeParms")
        errors += e
        if not e:
            dkeys = set(str(k) for k in dp.keys())
            errors += _v_keys(dkeys, img + "的 /DecodeParms", _PDF_DECODE_KEYS,
                              optional=("/BitsPerComponent",))
            want = {"/Predictor": (10,), "/Colors": (colors,) if colors else None,
                    "/Columns": (int(width),) if isinstance(width, NumberObject) else None,
                    "/BitsPerComponent": (8,)}
            for k in sorted(dkeys & set(want)):
                errors += _v_int(_obj(dp.get(k)), "%s的 /DecodeParms %s" % (img, k),
                                 allowed=want[k])
    return errors


def _pdf_page_whitelist(page, where: str) -> List[str]:
    """Page-level things that change what is SEEN of the image: annotations drawn over the page,
    graphics states with transparency / soft masks / blend modes, painting AFTER the image."""
    from pypdf.generic import ArrayObject, DictionaryObject
    errors = []
    annots = _obj(page.get("/Annots"))
    if annots is not None and (not isinstance(annots, ArrayObject) or len(annots)):
        errors.append("%s带注释对象（可能盖住图片）" % where)
    res = _obj(page.get("/Resources"))
    gss = _obj(res.get("/ExtGState")) if isinstance(res, DictionaryObject) else None
    for name, gs in (gss.items() if isinstance(gss, DictionaryObject) else ()):
        gs = _obj(gs)
        if not isinstance(gs, DictionaryObject):
            errors.append("%s的图形状态 %s 不是 PDF 字典（实为%s）" % (where, name, _pdf_kind(gs)))
            continue
        bad = sorted(set(str(k) for k in gs.keys()) - {"/Type", "/CA", "/ca"})
        if bad:
            errors.append("%s的图形状态 %s 带 %s（软遮罩 / 混合模式等）" % (where, name, bad))
        for k in ("/CA", "/ca"):
            if k in gs:
                errors += _v_num(_obj(gs[k]), "%s的图形状态 %s 透明度 %s" % (where, name, k), 1.0,
                                 tol=0.0)
    contents = page.get_contents()
    seen_image = False
    for _operands, op in (contents.operations if contents is not None else []):
        if seen_image and op in _PDF_PAINT:
            errors.append("%s在图片之后还有绘制操作 %s（可能盖住图片）" % (where, op.decode("latin-1")))
            break
        if op == b"Do":
            seen_image = True
    return errors


def _pdf_num(x) -> float:
    """A PDF NUMBER operand, as the format defines it (R2g #2): only pypdf's NumberObject /
    FloatObject, finite. A string such as ``(1)`` is NOT a number to a PDF renderer — Python's
    ``float()`` would accept it — so anything else raises ``TypeError``."""
    import math
    from pypdf.generic import FloatObject, NumberObject
    if isinstance(x, bool) or not isinstance(x, (NumberObject, FloatObject)):
        raise TypeError("操作数 %r 不是 PDF 数值（%s）" % (x, type(x).__name__))
    v = float(x)
    if not math.isfinite(v):
        raise TypeError("操作数 %r 不是有限数" % (x,))
    return v


def _pdf_name(x) -> str:
    from pypdf.generic import NameObject
    if not isinstance(x, NameObject):
        raise TypeError("操作数 %r 不是 PDF 名称（%s）" % (x, type(x).__name__))
    return str(x)


# --------------------------------------------------------------------------- PDF native types (R2h)
#
# pypdf models PDF names AND strings as Python ``str`` subclasses and PDF numbers as ``int`` /
# ``float`` subclasses, so ``str(x) == "/Indexed"`` or ``int(x) == 8`` cannot tell the name
# ``/Indexed`` from the string ``(/Indexed)``, or the integer 8 from the string ``(8)``. A renderer
# can: to PDFium ``(/Indexed)`` is no colour space and the picture is simply not drawn, while
# pypdf still decodes it. Every dictionary value the check relies on (after dereferencing) is
# therefore checked by its PDF TYPE first, then by its value; the dictionaries themselves are
# closed key sets. Integer-valued keys take only PDF integers (the writer never writes reals).


def _pdf_kind(x) -> str:
    """The PDF type of an already dereferenced pypdf object, for messages."""
    from pypdf.generic import (ArrayObject, BooleanObject, ByteStringObject, DictionaryObject,
                               FloatObject, IndirectObject, NameObject, NullObject, NumberObject,
                               StreamObject, TextStringObject)
    for cls, kind in ((NameObject, "名称"), (BooleanObject, "布尔"), (NumberObject, "整数"),
                      (FloatObject, "实数"), (ByteStringObject, "字符串"),
                      (TextStringObject, "字符串"), (StreamObject, "流"), (DictionaryObject, "字典"),
                      (ArrayObject, "数组"), (NullObject, "空对象"), (IndirectObject, "间接引用")):
        if isinstance(x, cls):
            return kind
    return "缺失" if x is None else "非 PDF 对象 %s" % type(x).__name__


def _pdf_show(x, n: int = 40) -> str:
    """``x`` for a message, in PDF notation: a string as ``(…)``, bytes as ``<hex>``."""
    from pypdf.generic import ByteStringObject, TextStringObject
    if isinstance(x, TextStringObject):
        s = "(%s)" % str(x)
    elif isinstance(x, ByteStringObject):
        s = "<%s>" % bytes(x).hex()
    elif isinstance(x, (int, float)) and not isinstance(x, bool):
        s = "%g" % x                       # (pypdf's own repr of a FloatObject can raise)
    else:
        s = repr(x)
    return s if len(s) <= n else s[:n] + "…"


def _v_name(v, label: str, allowed: Sequence[str]) -> List[str]:
    """``v`` must be a PDF NAME (not a string spelling it) and one of ``allowed``."""
    from pypdf.generic import NameObject
    if not isinstance(v, NameObject):
        return ["%s 不是 PDF 名称（实为%s %s）" % (label, _pdf_kind(v), _pdf_show(v))]
    if str(v) not in allowed:
        return ["%s 为 %s，不是写出端的取值 %s" % (label, v, " / ".join(allowed))]
    return []


def _v_int(v, label: str, allowed: Optional[Sequence[int]] = None, lo: Optional[int] = None,
           hi: Optional[int] = None) -> List[str]:
    """``v`` must be a PDF INTEGER (``NumberObject``; not a real, boolean or digit string)."""
    from pypdf.generic import NumberObject
    if isinstance(v, bool) or not isinstance(v, NumberObject):
        return ["%s 不是 PDF 整数（实为%s %s）" % (label, _pdf_kind(v), _pdf_show(v))]
    x = int(v)
    if allowed is not None and x not in allowed:
        return ["%s=%d 不是写出端的取值 %s" % (label, x, " / ".join(str(a) for a in allowed))]
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        return ["%s=%d 超出 %s…%s" % (label, x, lo, hi)]
    return []


def _v_num(v, label: str, want: float, tol: Optional[float] = None) -> List[str]:
    """``v`` must be a finite PDF NUMBER (integer or real) within ``tol`` of ``want``."""
    tol = _NUM_TOL if tol is None else tol
    try:
        x = _pdf_num(v)
    except TypeError:
        return ["%s 不是 PDF 数值（实为%s %s）" % (label, _pdf_kind(v), _pdf_show(v))]
    if abs(x - want) > tol:
        return ["%s=%g 不是写出端的取值 %g" % (label, x, want)]
    return []


def _v_dict(v, label: str) -> List[str]:
    from pypdf.generic import DictionaryObject, StreamObject
    if not isinstance(v, DictionaryObject) or isinstance(v, StreamObject):
        return ["%s 不是 PDF 字典（实为%s）" % (label, _pdf_kind(v))]
    return []


def _v_keys(keys, label: str, required, optional: Sequence[str] = (),
            skip: Sequence[str] = ()) -> List[str]:
    """A CLOSED key set: every ``required`` key present and nothing outside ``required`` +
    ``optional`` (``skip``: keys already refused with a more specific message)."""
    errors = []
    extra = sorted(set(keys) - set(required) - set(optional) - set(skip))
    missing = sorted(set(required) - set(keys))
    if extra:
        errors.append("%s带写出端不会产生的键 %s（封闭键集：未知键即拒绝）" % (label, extra))
    if missing:
        errors.append("%s缺少写出端必有的键 %s" % (label, missing))
    return errors


def _pdf_images(page) -> List[Tuple[str, List[float], Optional[Tuple[float, float, float, float]]]]:
    """``(xobject name, CTM, clip)`` for every image painted by the page's content stream
    (q/Q/cm/re W). ``clip`` = the intersection (page points) of the clip rectangles in force."""
    from pypdf.generic import NameObject
    res = page.get("/Resources") or {}
    res = res.get_object() if hasattr(res, "get_object") else res
    xobjs = res.get("/XObject") or {}
    xobjs = xobjs.get_object() if hasattr(xobjs, "get_object") else xobjs
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    clip = None
    stack = []
    pending = None
    out = []
    contents = page.get_contents()
    if contents is None:
        return out
    for operands, op in contents.operations:
        if op in (b"cm", b"re", b"Do"):
            try:                                  # R2g #2: native PDF types only
                if op == b"Do":
                    [_pdf_name(v) for v in operands]
                else:
                    [_pdf_num(v) for v in operands]
            except TypeError:
                out.append(("<bad operand>", None, None, None))
                continue
        if op == b"q":
            stack.append((list(ctm), clip))
        elif op == b"Q":
            ctm, clip = stack.pop() if stack else ([1.0, 0.0, 0.0, 1.0, 0.0, 0.0], None)
        elif op == b"cm" and len(operands) == 6:
            ctm = _mat_mul([_pdf_num(v) for v in operands], ctm)
        elif op == b"re" and len(operands) == 4:
            x, y, w, h = [_pdf_num(v) for v in operands]
            pts = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
            tp = [(px * ctm[0] + py * ctm[2] + ctm[4], px * ctm[1] + py * ctm[3] + ctm[5])
                  for px, py in pts]
            pending = (min(p[0] for p in tp), min(p[1] for p in tp),
                       max(p[0] for p in tp), max(p[1] for p in tp))
        elif op in (b"W", b"W*") and pending is not None:
            clip = pending if clip is None else (max(clip[0], pending[0]), max(clip[1], pending[1]),
                                                  min(clip[2], pending[2]), min(clip[3], pending[3]))
        elif op == b"Do" and operands:
            name = str(operands[0])
            xo = xobjs.get(NameObject(name)) if xobjs else None
            xo = xo.get_object() if hasattr(xo, "get_object") else xo
            if xo is not None and xo.get("/Subtype") == "/Image":
                out.append((name, list(ctm), clip, xo))
        if op not in (b"re",):
            if op not in (b"W", b"W*"):
                pending = None if op in (b"n", b"f", b"S", b"B", b"b", b"s") else pending
    return out


def _pdf_gray(page, name: str):
    target = name.lstrip("/")
    for im in page.images:
        stem = str(im.name).rsplit(".", 1)[0].lstrip("/")
        if stem == target:
            return visible_gray(im.image)          # any (masked) transparency over white
    return None


# --------------------------------------------------------------------------- PDF closed set (R2f)
#
# Same idea as the Word package: the PDF is accepted only when every object and every content
# operation is EXACTLY what the writer (matplotlib ``PdfPages`` of ``deliverable.write_pdf``,
# matplotlib pinned at 3.9.4) produces. Whatever is not on these lists — a clip path, an inline
# image, a second XObject, an optional-content group, a transparency group, an annotation, text,
# a shading — makes the document fail, instead of being interpreted.

_TRAILER_KEYS = {"/Size", "/Root", "/Info", "/ID"}
_INFO_KEYS = {"/Creator", "/Producer", "/Title"}
_PAGE_KEYS = {"/Type", "/Parent", "/Resources", "/MediaBox", "/Contents", "/Annots"}
_RES_KEYS = {"/Font", "/XObject", "/ExtGState", "/Pattern", "/Shading", "/ProcSet"}
_NUM_TOL = 0.01


def _pdf_ops_pattern(k: int) -> List[Tuple[bytes, list]]:
    """The writer's content stream for page ``k``: white page background, a clip equal to the
    picture rectangle, the opaque graphics state, the picture. ``"X","Y","W","H"`` stand for the
    picture rectangle (page points) and must be consistent wherever they appear."""
    pw, ph = _D.PAGE_MM[0] * PT_PER_MM, _D.PAGE_MM[1] * PT_PER_MM
    return [(b"CS", ["/DeviceRGB"]), (b"cs", ["/DeviceRGB"]), (b"j", [1]), (b"g", [1]),
            (b"j", [0]), (b"w", [0]), (b"G", [1]), (b"g", [1]),
            (b"m", [0, 0]), (b"l", [pw, 0]), (b"l", [pw, ph]), (b"l", [0, ph]), (b"h", []),
            (b"f", []),
            (b"q", []), (b"re", ["X", "Y", "W", "H"]), (b"W", []), (b"n", []),
            (b"gs", ["/A1"]), (b"j", [1]), (b"w", [1]), (b"G", [0]), (b"cs", ["/DeviceRGB"]),
            (b"q", []), (b"cm", [1, 0, 0, 1, "X", "Y"]), (b"cm", ["W", 0, 0, "-H", 0, "H"]),
            (b"Do", ["/I%d" % k]), (b"Q", []), (b"Q", [])]


def _pdf_ops_conform(ops, k: int, where: str) -> Tuple[List[str], Optional[Tuple[float, ...]]]:
    """Exact operator / operand match of a page's content stream against the writer's pattern.
    Returns ``(errors, (X, Y, W, H))``."""
    want = _pdf_ops_pattern(k)
    got = [(op, list(operands)) for operands, op in ops]
    if [op for op, _a in got] != [op for op, _a in want]:
        extra = [op.decode("latin-1") for op, _a in got if op not in {w for w, _ in want}]
        return (["%s的内容流不是写出端的固定结构（操作序列不同%s）——可能被裁切、覆盖、插入图片或文字"
                 % (where, "；含未知操作 %s" % sorted(set(extra)) if extra else "")], None)
    bind: Dict[str, float] = {}
    errors: List[str] = []
    for (op, args), (_wop, wargs) in zip(got, want):
        if len(args) != len(wargs):
            errors.append("%s的 %s 操作数个数不对" % (where, op.decode("latin-1")))
            continue
        for a, w in zip(args, wargs):
            if isinstance(w, str) and w.startswith("/"):
                try:
                    name = _pdf_name(a)
                except TypeError as exc:
                    errors.append("%s的 %s %s" % (where, op.decode("latin-1"), exc))
                    continue
                if name != w:
                    errors.append("%s的 %s 操作数 %s 应为 %s" % (where, op.decode("latin-1"), a, w))
                continue
            try:
                v = _pdf_num(a)
            except TypeError as exc:
                errors.append("%s的 %s %s" % (where, op.decode("latin-1"), exc))
                continue
            if isinstance(w, str):
                key, sign = (w[1:], -1.0) if w.startswith("-") else (w, 1.0)
                v *= sign
                if key in bind and abs(bind[key] - v) > _NUM_TOL:
                    errors.append("%s的裁切矩形与图片矩形不一致（%s）" % (where, key))
                bind.setdefault(key, v)
            elif abs(v - float(w)) > _NUM_TOL:
                errors.append("%s的 %s 操作数 %s 不是写出端的取值 %s"
                              % (where, op.decode("latin-1"), a, w))
    rect = tuple(bind.get(x, 0.0) for x in ("X", "Y", "W", "H"))
    return errors, rect


def _pdf_structure(reader, n: int) -> List[str]:
    """Document-level closed set: trailer, catalog (no optional content, actions, forms…), a flat
    page tree, the shared resources (exactly the n page images, the one opaque graphics state,
    no fonts / patterns / shadings) and every page dict (A4 MediaBox, no CropBox / Group /
    Rotate / annotations, one Flate content stream). R2h: every value is checked by its PDF type
    (name / integer / number / array / dictionary / stream) before its value."""
    from pypdf.generic import ArrayObject, DictionaryObject, StreamObject
    errors: List[str] = []
    trailer = reader.trailer
    bad = sorted(set(str(k) for k in trailer.keys()) - _TRAILER_KEYS)
    if bad or "/Encrypt" in trailer:
        errors.append("PDF 文件尾带写出端不会产生的键 %s" % bad)
    errors += _v_int(_obj(trailer.get("/Size")), "PDF 文件尾的 /Size", lo=1)
    root = _obj(trailer["/Root"])
    e = _v_dict(root, "PDF 目录")
    if e:
        return errors + e
    if sorted(str(k) for k in root.keys()) != ["/Pages", "/Type"]:
        errors.append("PDF 目录带写出端不会产生的键 %s（可选内容组 / 动作 / 表单等）"
                      % sorted(set(str(k) for k in root.keys()) - {"/Type", "/Pages"}))
    errors += _v_name(_obj(root.get("/Type")), "PDF 目录的 /Type", ("/Catalog",))
    pages = _obj(root["/Pages"])
    e = _v_dict(pages, "PDF 页树根")
    if e:
        return errors + e
    if sorted(str(k) for k in pages.keys()) != ["/Count", "/Kids", "/Type"]:
        errors.append("PDF 页树带额外键 %s（CropBox / Rotate / MediaBox 等会被页面继承）"
                      % sorted(str(k) for k in pages.keys()))
    errors += _v_name(_obj(pages.get("/Type")), "PDF 页树根的 /Type", ("/Pages",))
    # R2g #1: the tree is checked by REFERENCE, not by what pypdf resolves. The catalog's /Pages
    # must be an indirect reference; every kid an indirect reference to a /Page whose RAW /Parent
    # is that same reference (an off-tree parent could otherwise hand down CropBox / Rotate /
    # MediaBox no one checked); and the pages pypdf iterates are exactly these kids, in order.
    def ref(x):
        return (x.idnum, x.generation) if hasattr(x, "idnum") else None
    pages_ref = ref(root.raw_get("/Pages"))
    if pages_ref is None:
        errors.append("PDF 目录的 /Pages 不是间接引用")
    raw_kids = _obj(pages.raw_get("/Kids")) if "/Kids" in pages else None
    if not isinstance(raw_kids, ArrayObject):
        errors.append("PDF 页树的 /Kids 不是 PDF 数组（实为%s）" % _pdf_kind(raw_kids))
    raw_kids = list(raw_kids) if isinstance(raw_kids, ArrayObject) else []
    kid_refs = [ref(x) for x in raw_kids]
    if not raw_kids or None in kid_refs or len(set(kid_refs)) != len(kid_refs):
        errors.append("PDF 页树的 /Kids 不是互不相同的页面间接引用")
    kids = [_obj(x) for x in raw_kids]
    for k, kid in enumerate(kids, start=1):
        e = _v_dict(kid, "第 %d 页的页面对象" % k)
        if e:
            return errors + e
    errors += _v_int(_obj(pages.get("/Count")), "PDF 页树的 /Count", allowed=(len(kids),))
    for k, kid in enumerate(kids, start=1):
        errors += _v_name(_obj(kid.get("/Type")), "第 %d 页的 /Type" % k, ("/Page",))
        if ref(kid.raw_get("/Parent")) != pages_ref or pages_ref is None:
            errors.append("第 %d 页的 /Parent 不是目录的页树根（离树父对象可能继承裁切 / 旋转）" % k)
    try:
        iter_refs = [ref(pg.indirect_reference) for pg in reader.pages]
    except Exception:
        iter_refs = []
    if iter_refs != kid_refs:
        errors.append("PDF 实际页面序列与页树 /Kids 不一致")
    info = _obj(trailer.get("/Info"))
    if info is not None:
        bad = sorted(set(str(k) for k in info.keys()) - _INFO_KEYS)
        if bad:
            errors.append("PDF 文档信息带额外键 %s" % bad)
        title = str(_obj(info.get("/Title", "")))
        if title and title not in _D.DOCX_TITLES:
            errors.append("PDF 标题 %r 不是写出端的取值" % title)
    pw, ph = _D.PAGE_MM[0] * PT_PER_MM, _D.PAGE_MM[1] * PT_PER_MM
    want_x = ["/I%d" % k for k in range(1, n + 1)]
    for k, page in enumerate(kids, start=1):
        where = "第 %d 页" % k
        keys = set(str(x) for x in page.keys())
        if keys != _PAGE_KEYS:
            errors.append("%s的页面对象键不是写出端的集合（多 %s，少 %s）"
                          % (where, sorted(keys - _PAGE_KEYS), sorted(_PAGE_KEYS - keys)))
        annots = _obj(page.get("/Annots"))
        if not isinstance(annots, ArrayObject):
            errors.append("%s的 /Annots 不是 PDF 数组（实为%s）" % (where, _pdf_kind(annots)))
        elif len(annots):
            errors.append("%s带注释对象" % where)
        box = _obj(page.get("/MediaBox"))
        if not isinstance(box, ArrayObject) or len(box) != 4:
            errors.append("%s的页框 /MediaBox 不是 4 个数的数组（%s）——不是 A4" % (where, _pdf_show(box)))
        else:
            for side, v, want in zip(("左", "下", "右", "上"), box, (0.0, 0.0, pw, ph)):
                errors += _v_num(_obj(v), "%s的页框 /MediaBox %s边（A4）" % (where, side), want)
        cont = _obj(page.get("/Contents"))
        if not isinstance(cont, StreamObject):
            errors.append("%s的内容流不是单个流" % where)
        else:                                     # (pypdf consumed /Length while parsing)
            ck = sorted(set(str(x) for x in cont.keys()) - {"/Filter"})
            if ck:
                errors.append("%s的内容流字典不受控 %s" % (where, ck))
            errors += _v_name(_obj(cont.get("/Filter")), "%s的内容流 /Filter" % where,
                              ("/FlateDecode",))
        res = _obj(page.get("/Resources"))
        e = _v_dict(res, "%s的 /Resources" % where)
        if e:
            errors += e
            continue
        rkeys = set(str(x) for x in res.keys())
        if rkeys != _RES_KEYS:
            errors.append("%s的资源键不是写出端的集合 %s" % (where, sorted(rkeys ^ _RES_KEYS)))
        for name in ("/Font", "/Pattern", "/Shading"):
            if name in rkeys:
                v = _obj(res.get(name))
                e = _v_dict(v, "%s的资源 %s" % (where, name))
                errors += e
                if not e and len(v):
                    errors.append("%s的资源里多了 %s" % (where, name))
        xo = _obj(res.get("/XObject"))
        e = _v_dict(xo, "%s的图片资源 /XObject" % where)
        errors += e
        if not e:
            if sorted(str(x) for x in xo.keys()) != sorted(want_x):
                errors.append("%s的图片资源不是写出端的 %s（实为 %s）——多出的图片可能覆盖页面"
                              % (where, want_x, sorted(str(x) for x in xo.keys())))
            for name in sorted(str(x) for x in xo.keys()):
                if not isinstance(_obj(xo[name]), StreamObject):
                    errors.append("%s的图片资源 %s 不是 PDF 流（实为%s）"
                                  % (where, name, _pdf_kind(_obj(xo[name]))))
        gs = _obj(res.get("/ExtGState"))
        gs_err: List[str] = []
        ok_gs = isinstance(gs, DictionaryObject) and sorted(str(x) for x in gs.keys()) == ["/A1"]
        if ok_gs:
            a1 = _obj(gs["/A1"])
            ok_gs = isinstance(a1, DictionaryObject) and \
                sorted(str(x) for x in a1.keys()) == ["/CA", "/Type", "/ca"]
            if ok_gs:
                gs_err = (_v_name(_obj(a1["/Type"]), "/A1 的 /Type", ("/ExtGState",))
                          + _v_num(_obj(a1["/CA"]), "/A1 的 /CA", 1.0, tol=0.0)
                          + _v_num(_obj(a1["/ca"]), "/A1 的 /ca", 1.0, tol=0.0))
                ok_gs = not gs_err
        if not ok_gs:
            errors.append("%s的图形状态不是写出端的唯一不透明状态（透明度 / 软遮罩 / 混合模式）%s"
                          % (where, "：" + "；".join(gs_err) if gs_err else ""))
        procset = _obj(res.get("/ProcSet"))
        if not isinstance(procset, ArrayObject):
            errors.append("%s的资源 /ProcSet 不是 PDF 数组（实为%s）" % (where, _pdf_kind(procset)))
        else:
            for v in procset:
                errors += _v_name(_obj(v), "%s的资源 /ProcSet 项" % where, _PDF_PROCSET)
    return errors


#: The independent PDF renderer (R2j ruling: a NECESSARY condition of a PDF pass): pypdfium2
#: (BSD-3-Clause / Apache-2.0) — the only one (WP2 R2n ruling: no second native renderer, one
#: native-object lifetime to get right). ``PFK_PDF_RENDERER`` = auto (default) | none |
#: pypdfium2; any other value selects no renderer (PDFs then come out ``unverified``).
RENDERERS = ("pypdfium2",)


def render_backend() -> Optional[str]:
    import importlib
    import os
    want = os.environ.get("PFK_PDF_RENDERER", "auto").strip().lower()
    if want not in ("", "auto") + RENDERERS:
        return None
    try:
        importlib.import_module("pypdfium2")
        return "pypdfium2"
    except ImportError:
        return None


def render_checker_available() -> bool:
    return render_backend() is not None


#: R2l: PDFium must never be entered from two threads at once — pypdfium2 forbids it even for
#: different documents (random "Failed to load page", possible crashes), and every export job runs
#: in its own thread. ONE process-wide lock covers the whole use of the engine: open, render,
#: copying the pixels out, and closing every object. (Concurrency across PROCESSES would need a
#: render subprocess; the service renders in-process.)
_RENDER_LOCK = threading.Lock()
RENDER_ATTEMPTS = 2            # a renderer failure is retried once before it counts


class RenderUnavailable(RuntimeError):
    """The independent renderer itself failed (twice) — the visibility of this page is UNVERIFIED,
    which is not a verdict on the document's content."""


class _EngineError(RuntimeError):
    """One failed render attempt, carrying only the engine's error TEXT (R2n): no traceback, no
    reference to any native object."""


def _foreign_packed_bitmap(width, height, format, rev_byteorder=False, **_kw):
    import pypdfium2 as pdfium
    return pdfium.PdfBitmap.new_foreign(width, height, format, rev_byteorder=rev_byteorder,
                                        force_packed=True)


def _render_gray(path: Path, k: int, dpi: int, backend: str):
    """Page ``k`` rendered to a gray numpy array (a COPY — no view into a native bitmap). Under
    the process-wide lock, the document, page and bitmap are opened, used and explicitly closed;
    an engine exception is caught INSIDE the lock, its traceback frames cleared (they reference
    the native objects), and only its text leaves the lock (as ``_EngineError``) — so no native
    object can be released later, by the garbage collector, outside the lock (R2n)."""
    import traceback
    import numpy as np
    if backend not in RENDERERS:
        raise _EngineError("不支持的渲染器 %r（只支持 pypdfium2）" % (backend,))
    with _RENDER_LOCK:
        arr = error = None
        pdf = page = bitmap = None
        try:
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(str(path))
            page = pdf[k - 1]
            # a PDFium-allocated bitmap (``new_foreign``) is destroyed by its own ``close()`` —
            # below, inside the lock; the default Python-buffer bitmap is never destroyed at all
            bitmap = page.render(scale=dpi / 72.0, grayscale=True,
                                 bitmap_maker=_foreign_packed_bitmap)
            arr = np.array(bitmap.to_numpy(), dtype=np.uint8, copy=True)
        except Exception as exc:                  # the engine's own failure: keep its TEXT only
            error = "%s: %s" % (type(exc).__name__, exc)
            traceback.clear_frames(exc.__traceback__)
            exc.__traceback__ = exc.__context__ = exc.__cause__ = None
        finally:
            for obj in (bitmap, page, pdf):       # children first; explicit, inside the lock
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:             # noqa: BLE001 — already closed / torn down
                        pass
            bitmap = page = pdf = None
        if error is not None:
            raise _EngineError(error)
    return arr.reshape(arr.shape[0], arr.shape[1]) if arr.ndim == 3 else arr


def _render_page(path: Path, k: int, dpi: int, backend: str):
    """``_render_gray`` with ONE retry; a renderer that still fails raises ``RenderUnavailable``
    (R2l: an engine error is not a content failure — the page becomes unverified, not fail)."""
    last = None
    for _attempt in range(RENDER_ATTEMPTS):
        try:
            return _render_gray(path, k, dpi, backend)
        except Exception as exc:                  # the engine's own error, whatever its type
            last = "%s" % exc if isinstance(exc, _EngineError) else "%s: %s" % (
                type(exc).__name__, exc)          # keep the TEXT, not the exception (R2n)
    raise RenderUnavailable("%s 渲染第 %d 页出错（重试 %d 次）：%s"
                            % (backend, k, RENDER_ATTEMPTS - 1, last))


def _render_caption_ink(arr, dpi: int, src: "Source", rect_mm):
    """Defence in depth (R2f #3): on page ``k`` as RENDERED by an independent PDF engine
    (``arr`` at ``dpi``), the non-white share inside the figure-number box as it is actually
    SEEN — clips, overpaint, transparency, optional content, inherited page boxes and annotations
    all take effect there."""
    left, top, w, h = rect_mm
    bx0, by0, bx1, by1 = _D.caption_box(src.caption)
    wx0, wy0, wx1, wy1 = src.window
    to_px = dpi / 25.4
    c0 = int((left + (bx0 - wx0) / (wx1 - wx0) * w) * to_px)
    c1 = int((left + (bx1 - wx0) / (wx1 - wx0) * w) * to_px) + 1
    r0 = int((top + (wy1 - by1) / (wy1 - wy0) * h) * to_px)
    r1 = int((top + (wy1 - by0) / (wy1 - wy0) * h) * to_px) + 1
    crop = arr[max(r0, 0):r1, max(c0, 0):c1]
    whole = arr[int(top * to_px):int((top + h) * to_px), int(left * to_px):int((left + w) * to_px)]
    return (float((crop < _D.INK_LEVEL).mean()) if crop.size else 0.0,
            float((whole < _D.INK_LEVEL).mean()) if whole.size else 0.0)


# --------------------------------------------------------------------------- render ↔ source (R2k)
#
# The independent render is compared SPATIALLY with what the page must look like: the page's
# source DXF rasterised by the writer's own function (``deliverable.rasterize``) at the render dpi
# and placed where the writer places it. Ink proportions alone are not enough — a uniformly gray
# page has "ink" everywhere. Fixed thresholds (contract §14.18):
RENDER_DPI_MAX = 300          # render at the export dpi, at most this
INK_LEVEL = _D.INK_LEVEL       # gray < 128 is ink, in the expected raster and in the render alike
WHITE_LEVEL = 250              # gray < 250 is "not white" (background check)
MATCH_TOL_PX = 2               # spatial tolerance: masks dilated by 2 px (square)
MATCH_IOU_MIN = 0.90           # ink IoU (with tolerance) over the whole page
CAPTION_IOU_MIN = 0.80         # ink IoU (with tolerance) inside the figure-number box
BACKGROUND_NONWHITE_MAX = 0.005  # share of non-white render pixels where nothing is expected


def _dilate(mask, r: int):
    """Binary dilation by a (2r+1)² square, numpy only (separable shifts)."""
    import numpy as np
    out = mask.copy()
    for axis in (0, 1):
        src = out.copy()
        for d in range(1, r + 1):
            a = np.roll(src, d, axis=axis)
            b = np.roll(src, -d, axis=axis)
            if axis == 0:
                a[:d, :] = False
                b[-d:, :] = False
            else:
                a[:, :d] = False
                b[:, -d:] = False
            out |= a | b
    return out


def _tolerant_iou(expected, rendered, r: int) -> Tuple[float, float, float]:
    """``(iou, recall, precision)`` of two ink masks with an ``r``-px tolerance: recall = share of
    expected ink with rendered ink within r px; precision = share of rendered ink with expected
    ink within r px; iou = 1 / (1/recall + 1/precision − 1) (the IoU identity, exact at r = 0)."""
    ne, nr = int(expected.sum()), int(rendered.sum())
    if ne == 0 and nr == 0:
        return 1.0, 1.0, 1.0
    if ne == 0 or nr == 0:
        return 0.0, 0.0 if ne else 1.0, 0.0 if nr else 1.0
    recall = float((expected & _dilate(rendered, r)).sum()) / ne
    precision = float((rendered & _dilate(expected, r)).sum()) / nr
    if recall <= 0 or precision <= 0:
        return 0.0, recall, precision
    return 1.0 / (1.0 / recall + 1.0 / precision - 1.0), recall, precision


def render_dpi(sp: Dict[str, Any]) -> int:
    return int(min(int(sp["dpi"]), RENDER_DPI_MAX))


def _render_match(arr, dpi: int, k: int, src: "Source", sp: Dict[str, Any]) -> Dict[str, Any]:
    """Compare page ``k`` as RENDERED by the independent engine (``arr`` at ``dpi``) with the
    EXPECTED page — the source DXF rasterised by the writer's function at the same dpi, placed
    where the writer puts it on a white A4 page. Returns the metrics (ink IoU, caption IoU,
    background non-white share, recall / precision) and ``errors`` for every threshold not met."""
    import numpy as np
    spd = dict(sp, dpi=dpi)
    exp = _D.rasterize(src.dxf, spd, src.figure, src.number)
    page = np.full(arr.shape, 255, dtype=np.uint8)
    to_px = dpi / 25.4
    r0 = int(round(exp.placement["top"] * to_px))
    c0 = int(round(exp.placement["x"] * to_px))
    h, w = exp.gray.shape
    rr0, cc0 = max(r0, 0), max(c0, 0)
    rr1, cc1 = min(r0 + h, page.shape[0]), min(c0 + w, page.shape[1])
    if rr1 > rr0 and cc1 > cc0:
        page[rr0:rr1, cc0:cc1] = exp.gray[rr0 - r0:rr1 - r0, cc0 - c0:cc1 - c0]
    e_ink, r_ink = page < INK_LEVEL, arr < INK_LEVEL
    iou, recall, precision = _tolerant_iou(e_ink, r_ink, MATCH_TOL_PX)
    background = ~_dilate(page < WHITE_LEVEL, MATCH_TOL_PX)
    bg_nonwhite = float((arr[background] < WHITE_LEVEL).mean()) if background.any() else 0.0
    cap_iou = None
    if src.caption is not None:
        bx0, by0, bx1, by1 = _D.caption_box(src.caption)
        wx0, wy0, wx1, wy1 = exp.window
        pl = exp.placement
        sx, sy = pl["w"] / (wx1 - wx0), pl["h"] / (wy1 - wy0)
        # the caption box (central 80 % of the estimated glyph box) grown by half its height,
        # so whole glyphs are inside; in page pixels
        pad = 0.5 * (by1 - by0)
        cx0 = int((pl["x"] + (bx0 - pad - wx0) * sx) * to_px)
        cx1 = int((pl["x"] + (bx1 + pad - wx0) * sx) * to_px) + 1
        cy0 = int((pl["top"] + (wy1 - by1 - pad) * sy) * to_px)
        cy1 = int((pl["top"] + (wy1 - by0 + pad) * sy) * to_px) + 1
        sl = (slice(max(cy0, 0), max(cy1, 0)), slice(max(cx0, 0), max(cx1, 0)))
        cap_iou = _tolerant_iou(e_ink[sl], r_ink[sl], MATCH_TOL_PX)[0]
    errors = []
    where = "第 %d 页" % k
    if iou < MATCH_IOU_MIN:
        errors.append("%s渲染结果与源图不一致：墨迹重合度 IoU %.3f < %.2f（源图墨迹找回 %.1f%%，"
                      "渲染墨迹能对上源图的 %.1f%%）——图形缺失、错位、缩放或被替换"
                      % (where, iou, MATCH_IOU_MIN, recall * 100, precision * 100))
    if bg_nonwhite > BACKGROUND_NONWHITE_MAX:
        errors.append("%s渲染后背景（源图无墨迹处）有 %.2f%% 非白像素（上限 %.1f%%）——底色、遮盖或错位"
                      % (where, bg_nonwhite * 100, BACKGROUND_NONWHITE_MAX * 100))
    if cap_iou is not None and cap_iou < CAPTION_IOU_MIN:
        errors.append("%s渲染后图号区域与源图重合度 IoU %.3f < %.2f——图号缺失、错位或被遮盖"
                      % (where, cap_iou, CAPTION_IOU_MIN))
    return {"dpi": dpi, "iou": round(iou, 4), "recall": round(recall, 4),
            "precision": round(precision, 4), "caption_iou": None if cap_iou is None else
            round(cap_iou, 4), "background_nonwhite": round(bg_nonwhite, 5), "errors": errors}


def _pdf_page_report(path: Path, k: int, page, src: "Source", sp: Dict[str, Any],
                     render: str) -> Dict[str, Any]:
    box = page.mediabox
    pw = float(box.width) / PT_PER_MM
    ph = float(box.height) / PT_PER_MM
    imgs = _pdf_images(page)
    bad_ops = [i for i in imgs if i[0] == "<bad operand>"]
    imgs = [i for i in imgs if i[0] != "<bad operand>"]
    if not imgs:
        return {"page": k, "figure": src.figure, "ok": False,
                "errors": ["第 %d 页没有图片" % k], "render_caption_ink": None}
    # more than one image is already a failure; the first one is still measured (and the
    # page rendered) so the report shows what a reader actually sees
    name, m, clip, xo = imgs[0]
    where = "第 %d 页" % k
    extra = ([] if len(imgs) == 1 else ["第 %d 页应恰好 1 张图片，实有 %d 张" % (k, len(imgs))])
    if bad_ops:
        extra.append("%s的内容流里有非 PDF 数值 / 名称类型的操作数（几何无法确定）" % where)
    extra += _pdf_image_whitelist(xo, where) + _pdf_page_whitelist(page, where)
    contents = page.get_contents()
    op_errors, _rect = _pdf_ops_conform(contents.operations if contents is not None else [],
                                        k, where)
    extra += op_errors
    # unit square through the CTM → page rectangle (PDF origin bottom-left, points)
    xs = [m[4], m[0] + m[4], m[2] + m[4], m[0] + m[2] + m[4]]
    ys = [m[5], m[1] + m[5], m[3] + m[5], m[1] + m[3] + m[5]]
    if clip is not None and (clip[0] > min(xs) + 0.5 or clip[1] > min(ys) + 0.5
                             or clip[2] < max(xs) - 0.5 or clip[3] < max(ys) - 0.5):
        extra.append("%s的图片被裁切区域截掉一部分" % where)
    x0, x1 = min(xs) / PT_PER_MM, max(xs) / PT_PER_MM
    y0, y1 = min(ys) / PT_PER_MM, max(ys) / PT_PER_MM
    if abs(m[1]) > 1e-6 or abs(m[2]) > 1e-6:
        extra.append("图片被旋转或错切")
    try:
        gray = _pdf_gray(page, name)
    except Exception as exc:                      # R2h: e.g. a /Width that is not an integer
        gray = None
        extra.append("%s的图片像素无法解码（%s: %s）" % (where, type(exc).__name__, exc))
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
    rect_mm = (x0, ph - y1, x1 - x0, y1 - y0)
    rendered = match = render_unverified = None
    if render != "unavailable" and src.caption is not None:
        dpi = render_dpi(sp)
        try:
            arr = _render_page(path, k, dpi, render)
        except RenderUnavailable as exc:
            # R2l: the ENGINE failed (twice) — this says nothing about the document's content:
            # the page's visibility is unverified (the document becomes ``unverified``), not fail
            render_unverified = "%s独立渲染器出错，无法核验可见性（%s）" % (where, exc)
            arr = None
        if arr is not None:
            try:
                cap_ink, whole_ink = _render_caption_ink(arr, dpi, src, rect_mm)
                # R2k: the render must MATCH the source page spatially, not just carry some ink
                match = _render_match(arr, dpi, k, src, sp)
            except Exception as exc:              # the comparison itself broke: not content
                render_unverified = "%s渲染比对无法完成，无法核验可见性（%s: %s）" % (
                    where, type(exc).__name__, exc)
            else:
                rendered = round(cap_ink, 4)
                if cap_ink < INK_FRACTION_MIN:
                    extra.append("%s渲染后图号位置没有墨迹（%.1f%%）——页面上看不到「图%d」"
                                 % (where, cap_ink * 100, k))
                if whole_ink <= 0.0:
                    extra.append("%s渲染后图片区域一片空白" % where)
                extra += match.pop("errors")
    elif render != "unavailable":
        extra.append("%s源图没有图号，无法做渲染比对" % where)
    pc = _page_check(k, src, sp, rect_mm, px, gray, (pw, ph), extra)
    pc["render_caption_ink"] = rendered
    pc["render_match"] = match
    pc["render_unverified"] = render_unverified
    return pc


def _pdf_exc_reason(exc: BaseException) -> str:
    """A parse failure in words. A duplicate dictionary key is named explicitly (R2i): readers
    disagree on it — pypdf keeps the FIRST value, PDFium the LAST — so ``/ca 1 /ca 0`` would be
    opaque to the check and invisible on screen."""
    text = str(exc)
    if "Multiple definitions" in text:
        return "PDF 字典里有重复的键（pypdf 取前值、PDFium 取后值，各阅读器看到的不一样）——%s" % text
    if "Detected loop with self reference" in text:
        # pypdf's answer when an object that already failed to parse is asked for again
        return "引用的对象无法严格解析（循环引用或该对象本身已解析失败，见文档级原因）"
    return "%s: %s" % (type(exc).__name__, text)


def _pdf_parse_all(reader) -> List[str]:
    """R2i: resolve EVERY in-use object of the cross-reference table under ``strict=True`` — a
    duplicate dictionary key, a stream with a wrong ``/Length``, any malformed object anywhere in
    the file fails the document, whether or not the later checks would have touched it (a tolerant
    parse would silently keep one of the conflicting values)."""
    from pypdf.generic import IndirectObject
    errors: List[str] = []
    free = getattr(reader, "xref_free_entry", {}) or {}
    refs = [(idnum, gen) for gen, table in reader.xref.items() for idnum in table
            if not free.get(gen, {}).get(idnum)]
    refs += [(idnum, 0) for idnum in getattr(reader, "xref_objStm", {}) or {}]
    for idnum, gen in sorted(set(refs)):
        try:
            reader.get_object(IndirectObject(idnum, gen, reader))
        except Exception as exc:
            errors.append("PDF 对象 %d %d 无法严格解析：%s" % (idnum, gen, _pdf_exc_reason(exc)))
    return errors


def check_pdf(path: Path, sources: Sequence[Source], sp: Dict[str, Any]) -> Dict[str, Any]:
    path = Path(path)
    if not pdf_checker_available():
        rep = _report(path, "pdf", sources, [], ["未安装 pypdf，无法逐页核验 PDF（pip install pypdf）"
                                                  "——按规则不得报通过"], -1)
        rep.update(status=STATUS_UNVERIFIED, render_check=render_backend() or "unavailable")
        return rep
    from pypdf import PdfReader
    try:
        # R2i: STRICT parsing — the tolerant default keeps the first of duplicated dictionary keys
        # (with only a warning) where PDFium takes the last, repairs broken xref / stream lengths
        # its own way, and so on; the writer's files parse cleanly in strict mode
        reader = PdfReader(str(path), strict=True)
        pdf_pages = list(reader.pages)
    except Exception as exc:
        return _report(path, "pdf", sources, [], ["PDF 读不了（严格解析）：%s" % _pdf_exc_reason(exc)],
                       -1)
    doc_errors = _pdf_parse_all(reader)
    try:
        doc_errors += _pdf_structure(reader, len(pdf_pages))
    except Exception as exc:                      # an unexpected structure is not ours
        doc_errors.append("PDF 结构无法按写出端的固定结构解释：%s" % _pdf_exc_reason(exc))
    render = render_backend() or "unavailable"
    pages = []
    for k, (page, src) in enumerate(zip(pdf_pages, sources), start=1):
        try:
            pages.append(_pdf_page_report(path, k, page, src, sp, render))
        except Exception as exc:
            # R2h: an object pypdf cannot even interpret (e.g. a stream whose /Length is not a
            # PDF integer) is not the writer's — the page fails, the check never raises
            pages.append({"page": k, "figure": src.figure, "ok": False, "render_caption_ink": None,
                          "errors": ["第 %d 页无法按写出端的固定结构解释：%s"
                                     % (k, _pdf_exc_reason(exc))]})
    rep = _report(path, "pdf", sources, pages, doc_errors, len(pdf_pages))
    rep.update(layout_verified="file-geometry",
               layout_note="PDF 的页幅与图片位置取自文件里的绘制指令（CTM）与图片像素；"
                           "内容流与对象结构须与写出端固定结构一致；须经独立渲染确认可见",
               render_check=render)
    if rep["ok"] and render == "unavailable":
        # R2j ruling: the independent render is a NECESSARY condition of a PDF pass. The
        # structural checks above are defence in depth; they cannot prove what a reader SEES
        # (parser differentials: duplicate keys, duplicate xref entries …), so without a renderer
        # a structurally clean PDF is ``unverified`` — not ok, not a pass.
        rep["errors"].append(UNVERIFIED_PDF_REASON + UNVERIFIED_PDF_HINT)
        rep.update(ok=False, status=STATUS_UNVERIFIED)
    engine = [p["render_unverified"] for p in pages if p.get("render_unverified")]
    if rep["ok"] and engine:
        # R2l: nothing failed, but the renderer itself did on some page (after a retry): the
        # document is ``unverified`` with the reason — never ok, never quarantined as "fail"
        rep["errors"] += engine
        rep.update(ok=False, status=STATUS_UNVERIFIED)
    elif engine:
        rep["errors"] += engine                   # reported alongside the content failures
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


def _zero(v) -> bool:
    try:
        return float(v) == 0
    except (TypeError, ValueError):
        return False


def _any(_v) -> bool:
    return True


def _is(*vals):
    return lambda v: str(v) in vals


def _int(v) -> bool:
    try:
        int(str(v))
        return True
    except ValueError:
        return False


def _N(ns: str, local: str) -> str:
    return _q(ns, local)


# The writer's picture, element by element: (tag, {attribute: validator}, [(child spec, min, max)]).
# Anything NOT listed — an unknown attribute or child anywhere in the subtree — is refused, so a
# property nobody thought of (``hidden``, ``title``, effects, locks …) cannot slip through again
# (WP2 R2c). ``hidden`` is additionally reported by name.
_FILL_RECT = (_N(_A, "fillRect"), {"l": _zero, "t": _zero, "r": _zero, "b": _zero}, [])
_PIC_SPEC = (_N(_PIC, "pic"), {}, [
    ((_N(_PIC, "nvPicPr"), {}, [
        ((_N(_PIC, "cNvPr"), {"id": _int, "name": _any, "descr": _any}, []), 1, 1),
        ((_N(_PIC, "cNvPicPr"), {}, []), 1, 1)]), 1, 1),
    ((_N(_PIC, "blipFill"), {}, [
        ((_N(_A, "blip"), {_N(_R, "embed"): _any}, []), 1, 1),
        ((_N(_A, "srcRect"), {"l": _zero, "t": _zero, "r": _zero, "b": _zero}, []), 0, 1),
        ((_N(_A, "stretch"), {}, [(_FILL_RECT, 1, 1)]), 1, 1)]), 1, 1),
    ((_N(_PIC, "spPr"), {}, [
        ((_N(_A, "xfrm"), {"rot": _zero, "flipH": _is("0", "false"), "flipV": _is("0", "false")}, [
            ((_N(_A, "off"), {"x": _zero, "y": _zero}, []), 1, 1),
            ((_N(_A, "ext"), {"cx": _int, "cy": _int}, []), 1, 1)]), 1, 1),
        ((_N(_A, "prstGeom"), {"prst": _is("rect")}, [
            ((_N(_A, "avLst"), {}, []), 0, 1)]), 1, 1)]), 1, 1)])
_INLINE_SPEC = (_N(_WP, "inline"),
                {"distT": _zero, "distB": _zero, "distL": _zero, "distR": _zero}, [
    ((_N(_WP, "extent"), {"cx": _int, "cy": _int}, []), 1, 1),
    ((_N(_WP, "effectExtent"), {"l": _zero, "t": _zero, "r": _zero, "b": _zero}, []), 0, 1),
    ((_N(_WP, "docPr"), {"id": _int, "name": _any, "descr": _any}, []), 1, 1),
    ((_N(_WP, "cNvGraphicFramePr"), {}, [
        ((_N(_A, "graphicFrameLocks"), {"noChangeAspect": _is("1", "true")}, []), 0, 1)]), 0, 1),
    ((_N(_A, "graphic"), {}, [
        ((_N(_A, "graphicData"), {"uri": _is(_PIC_URI)}, [(_PIC_SPEC, 1, 1)]), 1, 1)]), 1, 1)])


def _short(tag: str) -> str:
    for pre, ns in (("wp", _WP), ("a", _A), ("pic", _PIC), ("r", _R), ("w", _W)):
        tag = tag.replace("{%s}" % ns, pre + ":")
    return tag


def _conform(el, spec, path: str, errors: List[str]) -> None:
    """Exact structural conformance of ``el`` to ``spec`` (see ``_PIC_SPEC``)."""
    tag, attrs, children = spec
    for name, value in el.attrib.items():
        check = attrs.get(name)
        if check is None:
            errors.append("%s 带未知属性 %s=%r（只允许写出端产生的属性）" % (path, _short(name), value))
        elif not check(value):
            errors.append("%s 的属性 %s=%r 不是写出端的取值" % (path, _short(name), value))
    allowed = {c[0][0]: c for c in children}
    counts: Dict[str, int] = {}
    for child in el:
        ctag = child.tag
        if ctag not in allowed:
            errors.append("%s 下有未知子元素 <%s>" % (path, _short(ctag)))
            continue
        counts[ctag] = counts.get(ctag, 0) + 1
        _conform(child, allowed[ctag][0], "%s/%s" % (path, _short(ctag)), errors)
    for ctag, (_cspec, lo, hi) in allowed.items():
        n = counts.get(ctag, 0)
        if not lo <= n <= hi:
            errors.append("%s 下 <%s> 应为 %d–%d 个，实有 %d" % (path, _short(ctag), lo, hi, n))


def _picture_whitelist(inl, where: str) -> List[str]:
    """The picture inside ``wp:inline`` must be EXACTLY what the writer produces — every element
    and every attribute is on a whitelist (``_INLINE_SPEC``), so the displayed picture is the
    media file mapped onto the extent, visible, uncropped, unrotated (R2b #2 / R2c). Hidden
    objects are named explicitly; the transform size must equal the extent."""
    errors: List[str] = []
    for el in (inl.find(_q(_WP, "docPr")),
               inl.find(".//%s/%s" % (_q(_PIC, "nvPicPr"), _q(_PIC, "cNvPr")))):
        if el is not None and str(el.get("hidden", "0")).lower() in ("1", "true", "on"):
            errors.append("%s的图片被设为隐藏（%s hidden=%s），页面上不会显示"
                          % (where, _short(el.tag), el.get("hidden")))
    # the common edits, named in plain words (the exact whitelist below catches everything else)
    crop = inl.find(".//" + _q(_A, "srcRect"))
    if crop is not None and not all(_zero(crop.get(a, 0)) for a in ("l", "t", "r", "b")):
        errors.append("%s的图片被裁剪（srcRect %s）" % (where, dict(crop.attrib)))
    xfrm = inl.find(".//" + _q(_A, "xfrm"))
    if xfrm is not None and not _zero(xfrm.get("rot", 0)):
        errors.append("%s的图片被旋转（rot=%s）" % (where, xfrm.get("rot")))
    if xfrm is not None and any(str(xfrm.get(a, "0")).lower() in ("1", "true")
                                for a in ("flipH", "flipV")):
        errors.append("%s的图片被翻转" % where)
    blip = inl.find(".//" + _q(_A, "blip"))
    if blip is not None and len(list(blip)):
        errors.append("%s的图片带图像效果 %s" % (where, [_short(c.tag) for c in blip]))
    _conform(inl, _INLINE_SPEC, where + " wp:inline", errors)
    ext = inl.find(_q(_WP, "extent"))
    xext = inl.find(".//%s/%s/%s" % (_q(_PIC, "spPr"), _q(_A, "xfrm"), _q(_A, "ext")))
    if ext is not None and xext is not None and \
            (xext.get("cx") != ext.get("cx") or xext.get("cy") != ext.get("cy")):
        errors.append("%s的图片内部变换尺寸与显示尺寸（wp:extent）不一致" % where)
    return errors


def _rpr_controlled(rpr) -> bool:
    """The only run / paragraph-mark formatting the writer emits: explicitly visible."""
    kids = [(_local(c), dict(c.attrib)) for c in rpr]
    return kids == [("vanish", {_q(_W, "val"): "0"}), ("webHidden", {_q(_W, "val"): "0"})] \
        and not rpr.attrib


_HIDING = ("vanish", "webHidden", "specVanish")


def _hidden_formatting(root, part: str) -> List[str]:
    """Display-hiding formatting anywhere in a part, named in plain words (the exact package
    comparison rejects it anyway; this says WHY)."""
    out = []
    for el in root.iter():
        name = _local(el)
        val = str(_wattr(el, "val", "1")).lower()
        if name in _HIDING and val not in ("0", "false", "off"):
            out.append("%s 里有隐藏格式 <%s>（内容不会显示）" % (part, name))
        elif name == "color" and val in ("ffffff", "white"):
            out.append("%s 里有白色文字格式（color=%s）" % (part, _wattr(el, "val")))
        elif name in ("sz", "szCs") and val in ("0", "1"):
            out.append("%s 里有近乎为零的字号（%s=%s）" % (part, name, _wattr(el, "val")))
    return out


def _package_conforms(parts: Dict[str, bytes], extents: List[Tuple[int, int]],
                      title: str) -> List[str]:
    """CLOSED-SET check of the whole package (WP2 R2d): exactly the writer's parts — no more, no
    fewer — and every non-media part byte-identical to the writer's fixed template rebuilt from
    the values read back (picture extents, title). Styles (docDefaults rPr/pPr, Normal), settings,
    content types, relationships and the document body cannot carry anything the writer did not
    write — inherited hidden formatting included."""
    errors: List[str] = []
    if title not in _D.DOCX_TITLES:
        errors.append("Word 标题 %r 不是写出端的取值" % title)
        return errors
    expected = _D.docx_parts(extents, title)
    want = set(expected) | {"word/media/image%d.png" % k for k in range(1, len(extents) + 1)}
    extra, missing = sorted(set(parts) - want), sorted(want - set(parts))
    if extra:
        errors.append("Word 包里有写出端不会产生的部件 %s" % extra[:6])
    if missing:
        errors.append("Word 包缺部件 %s" % missing[:6])
    for name, data in expected.items():
        if name in parts and parts[name] != data:
            errors.append("Word 包部件 %s 与写出端固定模板不一致（被修改过：样式、设置、关系或正文结构）"
                          % name)
    return errors


def _attrs_only(el, allowed: set, where: str, errors: List[str]) -> None:
    """Every attribute of ``el`` (namespace-stripped) must be in ``allowed``."""
    for name, value in el.attrib.items():
        if name.rsplit("}", 1)[-1] not in allowed:
            errors.append("%s带未知属性 %s=%r（只允许写出端产生的属性）" % (where, _short(name), value))


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
    _attrs_only(sect, {"rsidR", "rsidRPr", "rsidSect", "rsidDel"}, "节属性", errors)
    _attrs_only(sz, {"w", "h", "orient"}, "页幅", errors)
    _attrs_only(mar, {"top", "right", "bottom", "left", "header", "footer", "gutter"}, "页边",
                errors)
    if cols is not None:
        _attrs_only(cols, {"space", "num"}, "分栏", errors)
    if grid is not None:
        _attrs_only(grid, {"linePitch", "type"}, "文档网格", errors)
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
        _attrs_only(p, set(), where, errors)
        if ppr is not None:
            _attrs_only(ppr, set(), where + "段落属性", errors)
        for name, allowed_attrs in (("spacing", {"before", "after", "line", "lineRule"}),
                                    ("ind", {"left", "right", "firstLine"}),
                                    ("jc", {"val"}), ("pageBreakBefore", {"val"})):
            if name in props:
                _attrs_only(props[name], allowed_attrs, "%s的 %s" % (where, name), errors)
                if len(list(props[name])):
                    errors.append("%s的 %s 有子元素" % (where, name))
        for r in p.findall(_q(_W, "r")):
            _attrs_only(r, set(), where + "文字段", errors)
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
            if _local(el) == "rPr" and not _rpr_controlled(el):
                errors.append("%s图片所在文字段的字符格式不是写出端的受控格式（可能隐藏、升降或缩放）"
                              % where)
            elif _local(el) not in ("rPr", "drawing"):
                errors.append("%s图片所在文字段有额外内容 <%s>" % (where, _local(el)))
        mark = props.get("rPr")
        if mark is not None and not _rpr_controlled(mark):
            errors.append("%s段落标记的字符格式不是写出端的受控格式" % where)
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
            parts = {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}
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
    for name, el in (("word/styles.xml", styles), ("word/document.xml", root),
                     ("word/settings.xml", settings)):
        if el is not None:
            errors += _hidden_formatting(el, name)
    extents = []
    for _i, _p, inl in pictures:
        ext = inl.find(_q(_WP, "extent"))
        try:
            extents.append((int(ext.get("cx")), int(ext.get("cy"))))
        except (TypeError, ValueError, AttributeError):
            extents.append((-1, -1))
    try:
        core = ET.fromstring(parts.get("docProps/core.xml", b"<x/>"))
        title = "".join(t.text or "" for t in core.iter("{http://purl.org/dc/elements/1.1/}title"))
    except ET.ParseError:
        title = ""
    errors += _package_conforms(parts, extents, title)
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
                bad_png = png_whitelist(parts.get(name, b""))
                if bad_png:
                    extra += ["第 %d 页图片：%s" % (k, e) for e in bad_png]
                try:
                    with Image.open(io.BytesIO(parts[name])) as im:
                        if getattr(im, "n_frames", 1) != 1 or im.mode != "L":
                            extra.append("第 %d 页图片不是单帧 8 位灰度（%s，%s 帧）"
                                         % (k, im.mode, getattr(im, "n_frames", 1)))
                        gray = visible_gray(im)     # transparency (if any) over white
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


def sha256_of(path: Path) -> Optional[str]:
    """Hex sha256 of a file's bytes (None if it cannot be read)."""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(str(path), "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def check_document(path: Path, sources: Sequence[Source], sp: Dict[str, Any],
                   expected_sha256: Optional[str] = None) -> Dict[str, Any]:
    """Check one final document. ``expected_sha256`` (WP2 R2j ruling — the HASH SEAL): the digest
    the export recorded for this file when it wrote it. A file from an export bundle is checked
    against it FIRST; any difference (one byte) → ``fail`` 「文件已被修改，请使用导出包原文件」 and
    NO structural check runs — a modified file gets no chance to pass. A match only proves it is
    the original: the full check still runs. Without ``expected_sha256`` (a file of unknown
    origin) the full check runs as before. The report always carries the file's ``sha256``."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in (".pdf", ".docx"):
        raise ValueError("只核验 .pdf / .docx：%s" % path)
    fmt = suffix[1:]
    actual = sha256_of(path)
    if expected_sha256 is not None and (actual is None
                                        or actual != str(expected_sha256).strip().lower()):
        rep = _report(path, fmt, sources, [], ["%s（sha256 应为 %s…，实为 %s）" % (
            TAMPERED_REASON, str(expected_sha256)[:12], (actual or "读不了")[:12])], -1)
        rep.update(sha256=actual, sha256_verified=False)
        return rep
    rep = check_pdf(path, sources, sp) if fmt == "pdf" else check_docx(path, sources, sp)
    rep.update(sha256=actual, sha256_verified=None if expected_sha256 is None else True)
    return rep


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
    ap.add_argument("--sha256", default=None,
                    help="导出时记录的该文件 sha256（服务端导出结果 result.sha256，或导出包《核验报告.json》"
                         "的 sha256）：给出时先比对，不一致即判「文件已被修改」且不做其余核验")
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
    rep = check_document(Path(args.document), srcs, sp, expected_sha256=args.sha256)
    for pg in rep["page_checks"]:
        print("第 %d 页 %s %s：字高 %s mm / %s px，%s dpi，缩放 %s%s" % (
            pg["page"], pg.get("figure"), "通过" if pg["ok"] else "不通过",
            pg.get("min_text_mm"), pg.get("min_text_px"), pg.get("dpi_effective"),
            pg.get("scale"), ("；" + "；".join(pg["errors"])) if pg["errors"] else ""))
    for e in rep["errors"]:
        print("文件：%s" % e)
    verdict = {STATUS_OK: "通过", STATUS_UNVERIFIED: "未核验（不是通过）"}.get(rep.get("status"),
                                                                       "不通过")
    print("%s：%s（%d/%d 页）sha256=%s" % (rep["file"], verdict, rep["pages"],
                                        rep["expected_pages"], rep.get("sha256")))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return 0 if rep["ok"] else 1
