#!/usr/bin/env python3
"""DXF emission, preview rendering and hash normalization for patent figures.

This module is the only writer of drawing files. It receives geometry that is
already solved (millimetre sheet coordinates, produced by ``layout.py`` /
``labels.py`` through ``render_patent_figure.py``) and turns it into a DXF that
satisfies the compliance rules of impl-contract §8 and the QA gates of §5.5.

Design rules that must not be relaxed:

* every layer and every entity carries linetype ``CONTINUOUS`` (§5.4, and the
  ``non_continuous_max = 0`` QA gate);
* ``$INSUNITS`` is written explicitly as 4 (millimetres). ezdxf 1.4.2 defaults a
  fresh R2018 document to 6 (metres) — verified on this machine — so leaving it
  alone would label every millimetre drawing as metres;
* text styles record the font *name* (``simfang.ttf`` / ``txt.shx``), never an
  absolute path, so the file stays portable;
* the engineering parts table is opt-in and off by default (§8).

No layout constant is invented here. Every named constant below is either frozen
by the contract or derived from the §11.2 constant table, and the derivation is
written next to it.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import ezdxf
from ezdxf.enums import TextEntityAlignment

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps sheet.py free of a
    import numpy as np                 # runtime dependency on labels.py/numpy
    from .labels import LabelPlacement


# --------------------------------------------------------------------------- #
# constants                                                                    #
# --------------------------------------------------------------------------- #

# impl-contract §5.4, frozen.
LAYERS = ("GEOM", "HIDDEN", "LEADER", "NUM", "TABLE", "CAPTION", "NOTE")

LINETYPE = "CONTINUOUS"          # §5.4: all entities and layers CONTINUOUS
INSUNITS_MM = 4                  # §5.4: millimetres, written explicitly

STYLE_HZ = "HZ"                  # Chinese-capable style for caption / table
STYLE_NUM = "NUM"                # stick font for reference numerals
STYLE_HZ_FONT = "simfang.ttf"    # font NAME only, never a path
STYLE_NUM_FONT = "txt.shx"

# §11.7.3: "锚点处画 GB/T 4457.2 的小圆点（实心点，直径 0.15*h）——该常数属于 sheet.py".
ANCHOR_DOT_D_K = 0.15            # x text_height, diameter

# §11.2.2 decomposes the caption band as CAPTION_K(4.0h) = 1.2h + CAPTION_RATIO(1.6h) + 1.2h,
# i.e. clear space of 1.2h above and below the caption text. Only the clear-space
# share belongs to sheet.py; the band width itself is solved by the renderer.
CAPTION_CLEAR_K = 1.2            # x text_height

# Vertical half extent of a numeral box, used only to keep the caption clear of
# labels hanging below the geometry: 0.5h half glyph + PAD_Y(0.20h, §11.2.3).
NUM_EXTENT_K = 0.70              # x text_height

# --- engineering parts table (opt-in review copy only, §8) ------------------ #
# Widths are expressed in text heights. A Chinese glyph is one h wide and a
# digit is CHAR_W(0.71h, §11.2.3) wide, so the columns hold: NO. up to 4 digits
# (2.84h), NAME up to 12 glyphs, QTY up to 4 digits, REMARK up to 10 glyphs.
TABLE_COL_K = (3.0, 12.0, 3.0, 10.0)
TABLE_ROW_K = 2.0                # x text_height: glyph h + 0.5h clear above and below
TABLE_TEXT_K = 1.0               # x text_height: table text is the numeral height
TABLE_PAD_K = 0.5                # x text_height: left inset of left-aligned cells
TABLE_GAP_K = 2.0                # x text_height: gap between caption band and table
TABLE_HEADERS = ("NO.", "NAME", "QTY", "REMARK")
# §8: a file that carries the parts table must be named <id>_engineering.dxf so a
# review copy can never be mistaken for a filing copy.
ENGINEERING_SUFFIX = "_engineering"

# --- preview ---------------------------------------------------------------- #
PREVIEW_BG = "#FFFFFF"
PREVIEW_FG = "#000000"

# --- digest ----------------------------------------------------------------- #
DIGEST_DEC = 6                   # §5.4 step 2: round(float(v), 6)

# Verified against ezdxf 1.4.2 on a fresh R2018 document (§5.4). $ACADVER is NOT
# in this list on purpose: it is the DXF version itself, and stripping it would
# hide a real format change.
VOLATILE_HEADER_VARS = ("$TDCREATE", "$TDUPDATE", "$TDINDWG", "$TDUSRTIMER",
                        "$HANDSEED", "$FINGERPRINTGUID", "$VERSIONGUID",
                        "$LASTSAVEDBY", "$MENU", "$DWGCODEPAGE")

_ALIGN = {"left": TextEntityAlignment.MIDDLE_LEFT,
          "right": TextEntityAlignment.MIDDLE_RIGHT}


class SheetError(RuntimeError):
    """Raised when the requested sheet cannot be written as specified."""


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

def _points2d(polyline: Any) -> list:
    """Coerce a polyline to a list of (x, y) float pairs, order preserved."""
    out = []
    for pt in polyline:
        x = float(pt[0])
        y = float(pt[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise SheetError("write_figure: 折线含 NaN/Inf 坐标，无法写盘")
        out.append((x, y))
    return out


def _add_polyline(msp: Any, points: Sequence, layer: str) -> list:
    """Emit one polyline. Two points become a LINE, more become an LWPOLYLINE.

    A leader always carries three points (§5.3), so it is always written as a
    single LWPOLYLINE on layer LEADER, as required by §11.7.3.
    """
    pts = _points2d(points)
    if len(pts) < 2:
        return pts
    attribs = {"layer": layer, "linetype": LINETYPE}
    if len(pts) == 2:
        msp.add_line(pts[0], pts[1], dxfattribs=attribs)
    else:
        msp.add_lwpolyline(pts, format="xy", dxfattribs=attribs)
    return pts


def _add_text(msp: Any, value: str, x: float, y: float, height: float,
              layer: str, style: str, align: Any) -> None:
    entity = msp.add_text(value, height=float(height),
                          dxfattribs={"layer": layer, "linetype": LINETYPE,
                                      "style": style})
    entity.set_placement((float(x), float(y)), align=align)


def _new_document(dxf_version: str) -> Any:
    doc = ezdxf.new(dxf_version, setup=False)
    # ezdxf 1.4.2 defaults a fresh R2018 document to 6 (metres) — verified.
    doc.header["$INSUNITS"] = INSUNITS_MM
    for name in LAYERS:
        doc.layers.add(name, linetype=LINETYPE)
    for layer in doc.layers:          # also fixes layer "0" and "Defpoints"
        layer.dxf.linetype = LINETYPE
    doc.styles.add(STYLE_HZ, font=STYLE_HZ_FONT)
    doc.styles.add(STYLE_NUM, font=STYLE_NUM_FONT)
    return doc


def _draw_table(msp: Any, rows: Sequence, x0: float, top: float,
                text_height: float) -> None:
    """Engineering parts table — review copies only, see write_figure's docstring."""
    cell_h = TABLE_TEXT_K * text_height
    row_h = TABLE_ROW_K * text_height
    xs = [float(x0)]
    for k in TABLE_COL_K:
        xs.append(xs[-1] + k * text_height)
    n = len(rows) + 1                                   # + header row
    for i in range(n + 1):
        y = top - i * row_h
        msp.add_line((xs[0], y), (xs[-1], y),
                     dxfattribs={"layer": "TABLE", "linetype": LINETYPE})
    for x in xs:
        msp.add_line((x, top), (x, top - n * row_h),
                     dxfattribs={"layer": "TABLE", "linetype": LINETYPE})
    for i, head in enumerate(TABLE_HEADERS):
        _add_text(msp, head, 0.5 * (xs[i] + xs[i + 1]), top - 0.5 * row_h,
                  cell_h, "TABLE", STYLE_HZ, TextEntityAlignment.MIDDLE_CENTER)
    for j, row in enumerate(rows):
        y = top - (j + 1.5) * row_h
        cells = [str(c) for c in tuple(row)[:len(TABLE_HEADERS)]]
        while len(cells) < len(TABLE_HEADERS):
            cells.append("")
        for i, cell in enumerate(cells):
            left = i in (1, 3)                          # NAME and REMARK read left
            style = STYLE_NUM if cell.isdigit() else STYLE_HZ
            align = (TextEntityAlignment.MIDDLE_LEFT if left
                     else TextEntityAlignment.MIDDLE_CENTER)
            x = xs[i] + TABLE_PAD_K * text_height if left else 0.5 * (xs[i] + xs[i + 1])
            _add_text(msp, cell, x, y, cell_h, "TABLE", style, align)


# --------------------------------------------------------------------------- #
# public API                                                                   #
# --------------------------------------------------------------------------- #

def write_figure(path: Path, *, geometry: list[np.ndarray], hidden: list[np.ndarray] = (),
                 labels: list[LabelPlacement], caption: str, text_height: float,
                 caption_height: float, engineering_rows: list[tuple] | None = None,
                 dxf_version: str = "R2018") -> None:
    """Write one patent figure to ``path`` as a DXF.

    All coordinates are millimetres in sheet space; nothing is scaled or moved
    here. Entities are emitted in a fixed order — geometry, hidden lines,
    leaders, anchor dots, numerals, caption, table — so two runs with equal
    inputs differ only in the volatile header variables listed in
    ``VOLATILE_HEADER_VARS``, which ``normalized_digest`` ignores.

    Layers and styles: everything CONTINUOUS; ``HZ`` uses ``simfang.ttf`` and
    ``NUM`` uses ``txt.shx``, recorded as font names rather than paths.
    ``$INSUNITS`` is set to 4 explicitly.

    Leaders follow the Chinese drafting convention: a slanted segment from the
    anchor to the elbow, then a short horizontal landing line, with the numeral
    sitting just beyond the landing end. The three points of ``LabelPlacement.
    leader`` become one LWPOLYLINE on layer LEADER (§11.7.3), and the anchor
    carries a small dot of diameter ``ANCHOR_DOT_D_K * text_height``.

    ``engineering_rows`` is ``None`` for patent figures — **the parts table is
    opt-in only**. impl-contract §8: a patent figure carries only geometry,
    leaders, numerals and the caption. A parts table leaks internal part codes
    and names into a filing document, which is exactly the v1 failure this
    package exists to prevent; it also dilutes ``geometry_occupancy`` (the
    failure baseline measured 0.135 with a table on the sheet). When rows *are*
    passed, ``path`` must be named ``<id>_engineering.dxf`` so a review copy can
    never be mistaken for a filing copy — this is enforced, not documented.

    Raises ``SheetError`` on unusable input.
    """
    path = Path(path)
    h = float(text_height)
    ch = float(caption_height)
    if not (h > 0.0 and math.isfinite(h)):
        raise SheetError("write_figure: text_height 必须为正，收到 %r" % (text_height,))
    if not (ch > 0.0 and math.isfinite(ch)):
        raise SheetError("write_figure: caption_height 必须为正，收到 %r" % (caption_height,))
    if engineering_rows is not None and not path.stem.endswith(ENGINEERING_SUFFIX):
        raise SheetError(
            "write_figure: 带明细表的图必须命名为 <图号>%s.dxf（收到 %s）。"
            "依据 impl-contract §8：明细表只用于内部审阅副本，绝不能与申请用附图同名混淆。"
            % (ENGINEERING_SUFFIX, path.name))

    doc = _new_document(dxf_version)
    msp = doc.modelspace()

    # 1) geometry, 2) hidden lines — these two define the drawing's own bbox.
    geom_pts = []
    for poly in geometry:
        geom_pts.extend(_add_polyline(msp, poly, "GEOM"))
    for poly in hidden:
        geom_pts.extend(_add_polyline(msp, poly, "HIDDEN"))
    if not geom_pts:
        raise SheetError("write_figure: 没有任何可绘制的几何折线，拒绝写出空图")

    content_pts = list(geom_pts)

    # 3) leaders — one LWPOLYLINE of three points each (§11.7.3).
    for lp in labels:
        pts = tuple(lp.leader)
        if len(pts) != 3:
            raise SheetError("write_figure: 标记 %r 的引线不是 3 个点" % (lp.numeral,))
        content_pts.extend(_add_polyline(msp, pts, "LEADER"))

    # 4) anchor dots. A CIRCLE, not a filled donut polyline: a donut's chord
    # runs through the anchor, which is the endpoint of that label's own leader,
    # so a segment-based leader_crossing check would report a crossing against a
    # threshold of 0. A CIRCLE contributes no segments and is skipped naturally.
    r_dot = 0.5 * ANCHOR_DOT_D_K * h
    for lp in labels:
        anchor = tuple(lp.leader)[0]
        msp.add_circle((float(anchor[0]), float(anchor[1])), r_dot,
                       dxfattribs={"layer": "LEADER", "linetype": LINETYPE})

    # 5) numerals.
    for lp in labels:
        align = _ALIGN.get(str(lp.text_align))
        if align is None:
            raise SheetError("write_figure: 标记 %r 的对齐方式 %r 非法，只接受 left/right"
                             % (lp.numeral, lp.text_align))
        tx, ty = float(lp.text_pos[0]), float(lp.text_pos[1])
        _add_text(msp, str(lp.numeral), tx, ty, h, "NUM", STYLE_NUM, align)
        content_pts.append((tx, ty - NUM_EXTENT_K * h))
        content_pts.append((tx, ty + NUM_EXTENT_K * h))

    # 6) caption: centred under the drawing (x from the GEOM/HIDDEN bbox), below
    # everything that has been drawn (y from the whole content bbox, so a label
    # hanging below the geometry cannot collide with it). The clear space is
    # CAPTION_CLEAR_K * text_height, per the §11.2.2 caption-band decomposition.
    geom_lo_x = min(p[0] for p in geom_pts)
    geom_hi_x = max(p[0] for p in geom_pts)
    content_lo_y = min(p[1] for p in content_pts)
    y_cursor = content_lo_y
    if caption:
        cy = y_cursor - (CAPTION_CLEAR_K * h + 0.5 * ch)
        _add_text(msp, caption, 0.5 * (geom_lo_x + geom_hi_x), cy, ch,
                  "CAPTION", STYLE_HZ, TextEntityAlignment.MIDDLE_CENTER)
        y_cursor = cy - (0.5 * ch + CAPTION_CLEAR_K * h)

    # 7) engineering parts table — review copies only.
    if engineering_rows is not None:
        _draw_table(msp, list(engineering_rows), geom_lo_x,
                    y_cursor - TABLE_GAP_K * h, h)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(path))


#: Preview-only stand-ins for ``simfang.ttf`` when it is not installed: (collection or font
#: file, face name inside a .ttc). The DXF keeps recording ``simfang.ttf``; only the PNG
#: rasteriser is pointed at a face that actually exists. Without this ezdxf falls back to a
#: face that draws 「图」 as a filled block, which made every caption unreadable.
PREVIEW_CJK_CANDIDATES = (
    ("/System/Library/Fonts/Supplemental/Songti.ttc", "Songti SC Regular"),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", "STSong"),
    ("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc", "Noto Serif CJK SC"),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "Noto Sans CJK SC"),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", "Hiragino Sans GB W3"),
)
_preview_font_done = False


def _preview_cjk_font() -> None:
    """Map ``simfang.ttf`` to an installed CJK face for PNG previews (idempotent, best effort).

    ezdxf reads only face 0 of a .ttc, so the wanted face is extracted once into a per-user
    cache folder as a stand-alone .ttf. Any failure leaves ezdxf's own fallback in place.
    """
    global _preview_font_done
    if _preview_font_done:
        return
    _preview_font_done = True
    try:
        from ezdxf.fonts import fonts
        fm = fonts.font_manager
        if fm.has_font(STYLE_HZ_FONT):
            return
        cache = Path.home() / ".cache" / "patent-drawing" / "fonts"
        for src, face in PREVIEW_CJK_CANDIDATES:
            if not Path(src).is_file():
                continue
            target = cache / ("preview-%s.ttf" % face.replace(" ", ""))
            if not target.is_file():
                from fontTools.ttLib import TTCollection, TTFont
                cache.mkdir(parents=True, exist_ok=True)
                if src.endswith(".ttc"):
                    picked = [f for f in TTCollection(src).fonts
                              if f["name"].getDebugName(4) == face]
                    if not picked:
                        continue
                    picked[0].save(str(target))
                else:
                    TTFont(src).save(str(target))
            fm.scan_folder(cache)
            if fm.has_font(target.name):
                fm.add_synonyms({target.name: STYLE_HZ_FONT}, reverse=False)
                return
    except Exception:  # pragma: no cover - preview cosmetics must never break a render
        return


def render_preview(dxf: Path, png: Path, dpi: int = 150) -> None:
    """Render a PNG preview **from the written DXF**, never from memory.

    Reading the file back is the point: the preview then shows what was actually
    saved, so a bug in the emission path cannot hide behind correct in-memory
    geometry. White background, black lines.
    """
    dxf = Path(dxf)
    png = Path(png)
    # Imported lazily so that writing a DXF and hashing it never needs matplotlib.
    from ezdxf.addons.drawing.matplotlib import qsave

    _preview_cjk_font()
    doc = ezdxf.readfile(str(dxf))
    png.parent.mkdir(parents=True, exist_ok=True)
    qsave(doc.modelspace(), str(png), bg=PREVIEW_BG, fg=PREVIEW_FG,
          dpi=int(dpi), backend="agg")


def _r(value: Any) -> float:
    """round(float(v), 6), with -0.0 folded to 0.0.

    The fold matters because repr(-0.0) != repr(0.0) while -0.0 == 0.0, so
    without it a sign-flipped zero would change the digest without changing the
    drawing. It cannot make round-then-hash disagree with round-then-compare.
    """
    return round(float(value), DIGEST_DEC) + 0.0


def _entity_record(entity: Any) -> tuple:
    """(layer, dxftype, style_or_empty, text_or_empty, tuple_of_rounded_coords).

    Coordinate order is the entity's own vertex order (§5.4 step 2):
    LINE start then end, LWPOLYLINE points in stored order, TEXT insert point
    then height. CIRCLE (the leader anchor dot, which §11.7.3 asks sheet.py to
    draw) is centre then radius — the same round-and-flatten rule extended to
    the one further type this module emits. Handles and owner handles are never
    read.
    """
    dxftype = entity.dxftype()
    layer = str(entity.dxf.layer)
    style = ""
    if entity.dxf.is_supported("style"):
        style = str(entity.dxf.get("style", ""))

    if dxftype == "LINE":
        s, e = entity.dxf.start, entity.dxf.end
        coords = (_r(s[0]), _r(s[1]), _r(s[2]), _r(e[0]), _r(e[1]), _r(e[2]))
        return (layer, dxftype, style, "", coords)
    if dxftype == "LWPOLYLINE":
        flat = []
        for x, y in entity.get_points(format="xy"):
            flat.append(_r(x))
            flat.append(_r(y))
        return (layer, dxftype, style, "", tuple(flat))
    if dxftype == "TEXT":
        p = entity.dxf.insert
        coords = (_r(p[0]), _r(p[1]), _r(p[2]), _r(entity.dxf.height))
        return (layer, dxftype, style, str(entity.dxf.text), coords)
    if dxftype == "CIRCLE":
        c = entity.dxf.center
        coords = (_r(c[0]), _r(c[1]), _r(c[2]), _r(entity.dxf.radius))
        return (layer, dxftype, style, "", coords)
    if dxftype == "SOLID":   # flowchart.py 的实心箭头；结构附图不写 SOLID
        coords = tuple(_r(v) for i in range(4)
                       for v in tuple(entity.dxf.get("vtx%d" % i, (0, 0, 0)))[:3])
        return (layer, dxftype, style, "", coords)
    raise SheetError(
        "normalized_digest: 实体类型 %s 没有规范化规则。write_figure 只会写出 "
        "LINE / LWPOLYLINE / TEXT / CIRCLE；出现其它类型说明文件不是本工具产出的，"
        "或 sheet.py 增加了新实体却没有同步这里的配方。" % dxftype)


def normalized_digest(dxf: Path) -> str:
    """SHA-256 over a canonical form of the drawing (§5.4, recipe followed verbatim).

    1. Read with ezdxf; ignore the header entirely except to assert
       ``$INSUNITS == 4``.
    2. Build one tuple per modelspace entity, see ``_entity_record``.
    3. Sort with Python's default tuple ordering — a total order on these
       values, so no tie-break is needed. Not ``np.argsort``.
    4. Join the ``repr()`` of each tuple with "\\n", encode UTF-8, SHA-256.

    Two runs of the same plan must produce the same digest, which is why the
    volatile header variables never enter the hash.
    """
    doc = ezdxf.readfile(str(Path(dxf)))
    insunits = doc.header.get("$INSUNITS", None)
    if insunits is None or int(insunits) != INSUNITS_MM:
        raise SheetError(
            "normalized_digest: $INSUNITS = %r，期望 %d（毫米）。"
            "该文件不是按本契约 §5.4 写出的，摘要没有意义。" % (insunits, INSUNITS_MM))
    records = [_entity_record(e) for e in doc.modelspace()]
    records.sort()
    blob = "\n".join(repr(rec) for rec in records)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
