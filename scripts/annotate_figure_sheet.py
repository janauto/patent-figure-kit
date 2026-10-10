#!/usr/bin/env python3
"""给已出图（已过 QA）的专利附图加「规范性标注」：图号与件号名称表。

    python3 scripts/annotate_figure_sheet.py out/fig1.dxf \\
        --numerals out/reference-numerals.json --figure-number 1 \\
        -o out/fig1_annotated.dxf --preview

做两件事，都只在**副本**上做，输入的 DXF 一个字节都不改：

1. 图题换成图号「图N」。描述性图题（「整机分解示意图」）不该留在图面上——
   它属于说明书的「附图说明」段，本工具在报告里给出对应的那句话。
2. （默认开启，``--no-table`` 关闭）在图面空白角落放「序号｜名 称」件号表，
   行多时拆成左右双栏。格式取自国内常见的说明书附图（图号 + 序号｜名称表）。

表里每一行都来自这张图上**实际出现的**附图标记（读 DXF 的 NUM 层），名称来自
``reference-numerals.json``——两样都是渲染器产出的权威数据，本工具不发号、
不改号、不编名称。表格放置不碰任何已有实体：先在图纸可用区的四个角找空位
（左下优先，与参考图一致），单栏放不下试双栏，都放不下才把表放到图题下方
并在报告里标 ``outside_usable_area``。

退出码：0 成功；1 失败（读不到文件、编号表缺项等）；2 用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import ezdxf  # noqa: E402
from ezdxf import bbox as _bbox  # noqa: E402
from ezdxf.enums import TextEntityAlignment  # noqa: E402

from patent_figure import layout as _layout  # noqa: E402
from patent_figure import sheet as _sheet  # noqa: E402

# --------------------------------------------------------------------------- #
# 常数——全部以「附图标记字高 h」为单位，或取自既有冻结常数                         #
# --------------------------------------------------------------------------- #

#: 图纸可用区（mm），与渲染器同源：A4 竖放扣除审查指南页边距。
SHEET_W, SHEET_H = _layout.FRAME_W, _layout.FRAME_H

#: 表格文字高 = 0.6h，但不低于交付下限 3.5 mm（参考图：标记 16pt、表格 10pt ≈ 0.63）。
TABLE_TEXT_K = 0.6
TEXT_FLOOR_MM = _layout.TEXT_FLOOR_MM
ROW_K = 1.9               # 行高 = 1.9 x 表格字高
NUM_COL_K = 3.4           # 序号列宽 = 3.4 x 表格字高（两字「序号」+ 留白）
NAME_PAD_K = 1.0          # 名称列 = 最长名称宽 + 1.0 x 字高（左右各留半格）
NAME_MIN_GLYPHS = 4       # 名称列至少容得下「名 称」表头
CLEAR_K = 1.0             # 表格与任何已有实体的净距 = 1.0 x 表格字高
CAPTION_K = 1.15          # 图号字高 = 1.15 x 附图标记字高（参考图：图号 17pt / 标记 16pt）
CAPTION_MIN_MM = 5.0
SPLIT_MIN_ROWS = 8        # 少于这么多行不拆双栏（拆了反而难看）
SPLIT_PREFER_ROWS = 12    # 多于这么多行优先拆双栏（参考图图5：15 行拆成左右两栏）
HEADERS = ("序号", "名 称")

ASCII_W = 0.62            # 半角字符宽（x 字高）
CJK_W = 1.45              # 全角字符宽（x 字高）：DXF 字高是大写高，宋体预览实测约 1.4h，仿宋更窄，取宽者
SCAN_STEP_MM = 2.0        # 表格空位扫描步长


class AnnotateError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 读图                                                                          #
# --------------------------------------------------------------------------- #

def _text_width(s: str, h: float) -> float:
    return sum((ASCII_W if ord(c) < 0x2E80 else CJK_W) for c in s) * h


def _numerals_on_sheet(msp) -> List[int]:
    found = set()
    for e in msp.query("TEXT"):
        if e.dxf.layer == "NUM" and e.dxf.text.strip().isdigit():
            found.add(int(e.dxf.text.strip()))
    return sorted(found)


def _numeral_height(msp) -> float:
    for e in msp.query("TEXT"):
        if e.dxf.layer == "NUM":
            return float(e.dxf.height)
    return TEXT_FLOOR_MM


Box = Tuple[float, float, float, float]  # x0, y0, x1, y1


def _body_boxes(src: Path) -> Optional[List[Box]]:
    """渲染器写在 <id>.layout.json 里的逐零件图面包围盒。

    只看线段会把壳体轮廓围出的「空心」当成空白——表格会被放进机身里。零件包围盒
    把整件当实心，才是「这里有图」的正确口径。"""
    path = src.with_suffix(".layout.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    out = []
    for b in data.get("body_boxes", []):
        (x0, y0), (x1, y1) = b["lo"], b["hi"]
        out.append((float(x0), float(y0), float(x1), float(y1)))
    return out


def _occupied(msp, bodies: Optional[List[Box]] = None) -> List[Box]:
    """除图题外每个实体的包围盒（mm），再并上零件实心包围盒。

    没有 layout.json 时，所有 GEOM 实体的总包围盒整体当作实心（保守）。"""
    boxes = list(bodies or [])
    geom = []
    for e in msp:
        if e.dxf.layer in ("CAPTION",):
            continue
        ext = _bbox.extents([e], fast=True)
        if not ext.has_data:
            continue
        lo, hi = ext.extmin, ext.extmax
        if e.dxftype() == "TEXT":   # fast 模式下 TEXT 只有插入点，按字宽补出盒子
            h = float(e.dxf.height)
            w = _text_width(e.dxf.text, h)
            align = e.get_placement()[0].name
            x = lo.x
            x0 = x - w if align.endswith("RIGHT") else (x - w / 2 if align.endswith("CENTER") else x)
            boxes.append((x0, lo.y - h / 2, x0 + w, lo.y + h / 2))
            continue
        boxes.append((lo.x, lo.y, hi.x, hi.y))
        if e.dxf.layer in ("GEOM", "HIDDEN"):
            geom.append((lo.x, lo.y, hi.x, hi.y))
    if bodies is None and geom:
        boxes.append((min(b[0] for b in geom), min(b[1] for b in geom),
                      max(b[2] for b in geom), max(b[3] for b in geom)))
    return boxes


def _caption(msp):
    for e in msp.query("TEXT"):
        if e.dxf.layer == "CAPTION":
            return e
    return None


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


# --------------------------------------------------------------------------- #
# 表格                                                                          #
# --------------------------------------------------------------------------- #

def _block_size(rows: Sequence[Tuple[int, str]], th: float) -> Tuple[float, float, float]:
    glyphs = max([NAME_MIN_GLYPHS] + [_text_width(n, 1.0) for _, n in rows])
    num_w = NUM_COL_K * th
    name_w = glyphs * th + NAME_PAD_K * th
    height = (len(rows) + 1) * ROW_K * th
    return num_w, name_w, height


def _plan_table(rows: Sequence[Tuple[int, str]], th: float, blocks: int):
    """返回 [(rows_in_block)]、总宽、总高。"""
    if blocks == 1:
        parts = [list(rows)]
    else:
        half = (len(rows) + 1) // 2
        parts = [list(rows[:half]), list(rows[half:])]
    num_w, name_w, _ = _block_size(rows, th)
    block_w = num_w + name_w
    height = (len(parts[0]) + 1) * ROW_K * th
    return parts, num_w, name_w, block_w * len(parts), height


def _find_spot(occupied: Sequence[Box], w: float, h: float, floor_y: float,
               clear: float) -> Optional[Tuple[str, float, float]]:
    """在可用区内按网格扫描能放下 w x h 的空位，取离左下角最近的那个（参考图的位置）。"""
    import numpy as np
    if w > SHEET_W or h > SHEET_H - floor_y:
        return None
    boxes = np.array(occupied, dtype=float).reshape(-1, 4)
    xs = np.arange(0.0, SHEET_W - w + 1e-9, SCAN_STEP_MM)
    ys = np.arange(floor_y, SHEET_H - h + 1e-9, SCAN_STEP_MM)
    if xs.size == 0 or ys.size == 0:
        return None
    gx, gy = np.meshgrid(xs, ys)
    gx = gx.ravel(); gy = gy.ravel()
    order = np.argsort(gx ** 2 + (gy - floor_y) ** 2, kind="stable")
    for i in order:
        x, y = float(gx[i]), float(gy[i])
        if boxes.size and np.any((x - clear < boxes[:, 2]) & (boxes[:, 0] < x + w + clear)
                                 & (y - clear < boxes[:, 3]) & (boxes[:, 1] < y + h + clear)):
            continue
        cx, cy = x + w / 2, y + h / 2
        name = ("左" if cx < SHEET_W / 2 else "右") + ("下" if cy < SHEET_H / 2 else "上")
        return name, x, y
    return None


def _draw_table(msp, parts, num_w, name_w, x0, y0, th) -> None:
    row_h = ROW_K * th
    for k, blk in enumerate(parts):
        bx = x0 + k * (num_w + name_w)
        n_rows = len(parts[0]) + 1
        top = y0 + n_rows * row_h
        attrs = {"layer": "TABLE", "linetype": "CONTINUOUS"}
        for r in range(n_rows + 1):
            y = top - r * row_h
            msp.add_line((bx, y), (bx + num_w + name_w, y), dxfattribs=attrs)
        for x in (bx, bx + num_w, bx + num_w + name_w):
            msp.add_line((x, y0), (x, top), dxfattribs=attrs)

        def put(text, x, y, align, style):
            t = msp.add_text(text, height=th, dxfattribs={
                "layer": "TABLE", "style": style, "linetype": "CONTINUOUS"})
            t.set_placement((x, y), align=align)

        yh = top - row_h / 2
        put(HEADERS[0], bx + num_w / 2, yh, TextEntityAlignment.MIDDLE_CENTER, _sheet.STYLE_HZ)
        put(HEADERS[1], bx + num_w + name_w / 2, yh, TextEntityAlignment.MIDDLE_CENTER,
            _sheet.STYLE_HZ)
        for i, (num, name) in enumerate(blk):
            yy = top - (i + 1.5) * row_h
            put(str(num), bx + num_w / 2, yy, TextEntityAlignment.MIDDLE_CENTER,
                _sheet.STYLE_NUM)
            put(name, bx + num_w + 0.5 * th, yy, TextEntityAlignment.MIDDLE_LEFT,
                _sheet.STYLE_HZ)


# --------------------------------------------------------------------------- #
# 主流程                                                                        #
# --------------------------------------------------------------------------- #

def annotate(src: Path, dst: Path, numerals_json: Path, figure_number: int,
             with_table: bool = True, preview: bool = False) -> dict:
    data = json.loads(numerals_json.read_text(encoding="utf-8"))
    names: Dict[int, str] = {int(n["numeral"]): str(n["term"]) for n in data.get("numerals", [])}

    doc = ezdxf.readfile(str(src))
    msp = doc.modelspace()
    for layer in ("TABLE", "CAPTION"):
        if layer not in doc.layers:
            doc.layers.add(layer, linetype="CONTINUOUS")

    on_sheet = _numerals_on_sheet(msp)
    missing = [n for n in on_sheet if n not in names]
    if missing:
        raise AnnotateError("图上的标记 %s 在 reference-numerals.json 里没有名称——"
                            "两份文件不是同一次渲染的产物" % missing)
    rows = [(n, names[n]) for n in on_sheet]
    h = _numeral_height(msp)

    # 1) 图号
    cap = _caption(msp)
    old_caption = cap.dxf.text if cap is not None else ""
    cap_h = max(CAPTION_MIN_MM, CAPTION_K * h)
    if cap is None:
        cap = msp.add_text("", dxfattribs={"layer": "CAPTION", "style": _sheet.STYLE_HZ})
        cap.set_placement((SHEET_W / 2, cap_h), align=TextEntityAlignment.MIDDLE_CENTER)
    cap.dxf.text = "图%d" % figure_number
    cap.dxf.height = cap_h
    cap_y = cap.get_placement()[1].y
    floor_y = cap_y + cap_h / 2 + CLEAR_K * h        # 表格不得压到图号

    report = {
        "schema": "patent-figure-annotation/1",
        "source": str(src), "output": str(dst),
        "figure_number": figure_number,
        "caption": "图%d" % figure_number,
        "former_caption": old_caption,
        "figure_description": ("图%d为%s；" % (figure_number, old_caption)) if old_caption else "",
        "numerals": on_sheet,
        "table": None,
        "warnings": [],
    }

    # 2) 件号表
    if with_table and rows:
        occupied = _occupied(msp, _body_boxes(src))
        th = max(TEXT_FLOOR_MM, TABLE_TEXT_K * h)
        placed = None
        layouts = [1]
        if len(rows) >= SPLIT_MIN_ROWS:
            layouts = [2, 1] if len(rows) > SPLIT_PREFER_ROWS else [1, 2]
        sizes = [th] + ([TEXT_FLOOR_MM] if th > TEXT_FLOOR_MM else [])
        attempts = [(t, b) for t in sizes for b in layouts]
        for t, blocks in attempts:
            parts, num_w, name_w, w, ht = _plan_table(rows, t, blocks)
            spot = _find_spot(occupied, w, ht, floor_y, CLEAR_K * t)
            if spot:
                placed = (spot, parts, num_w, name_w, w, ht, t, blocks)
                break
        outside = False
        if placed is None:
            # 图面上没有空位：表格放到图形下方靠左，图号与表格底边齐平（参考图图1 的排法）。
            # 这会超出 A4 可用区，报告里明示，插入文档时整体缩放。
            t, blocks = th, layouts[0]
            parts, num_w, name_w, w, ht = _plan_table(rows, t, blocks)
            low = min(b[1] for b in occupied) if occupied else floor_y
            y0 = low - CLEAR_K * t - ht
            placed = (("图形下方", 0.0, y0), parts, num_w, name_w, w, ht, t, blocks)
            # 图号移到表格右侧余下宽度的正中，与表格底行同高
            cap.set_placement(((w + SHEET_W) / 2, y0 + cap_h / 2),
                              align=TextEntityAlignment.MIDDLE_CENTER)
            outside = True
            report["warnings"].append("图面上放不下件号表，已放在图形下方，"
                                      "超出 A4 可用区——插入文档时请整体缩放")
        (corner, x0, y0), parts, num_w, name_w, w, ht, t, blocks = placed
        _draw_table(msp, parts, num_w, name_w, x0, y0, t)
        report["table"] = {
            "rows": [{"numeral": n, "name": nm} for n, nm in rows],
            "corner": corner, "blocks": blocks, "text_height_mm": round(t, 3),
            "box_mm": [round(x0, 3), round(y0, 3), round(x0 + w, 3), round(y0 + ht, 3)],
            "outside_usable_area": outside,
        }

    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(dst))
    if preview:
        png = dst.with_suffix(".png")
        _sheet.render_preview(dst, png)
        report["preview"] = str(png)
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dxf", type=Path, help="render_patent_figure.py 产出的 <id>.dxf")
    ap.add_argument("--numerals", type=Path, required=True,
                    help="同一次渲染的 reference-numerals.json")
    ap.add_argument("--figure-number", type=int, required=True, help="图号 N（写成「图N」）")
    ap.add_argument("-o", "--out", type=Path, required=True, help="输出 DXF 路径（不得等于输入）")
    ap.add_argument("--no-table", action="store_true", help="只换图号，不加件号表（递交版）")
    ap.add_argument("--preview", action="store_true", help="同时出 PNG 预览（从写出的 DXF 渲染）")
    ap.add_argument("--json", type=Path, help="把标注报告写成 JSON")
    args = ap.parse_args(argv)
    if not args.dxf.is_file() or not args.numerals.is_file():
        print("用法错误：输入文件不存在", file=sys.stderr)
        return 2
    if args.out.resolve() == args.dxf.resolve():
        print("用法错误：输出不能覆盖输入——标注只在副本上做", file=sys.stderr)
        return 2
    if args.figure_number < 1:
        print("用法错误：--figure-number 必须 >= 1", file=sys.stderr)
        return 2
    try:
        report = annotate(args.dxf, args.out, args.numerals, args.figure_number,
                          with_table=not args.no_table, preview=args.preview)
    except (AnnotateError, OSError, ValueError) as exc:
        print("标注失败：%s" % exc, file=sys.stderr)
        return 1
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    tbl = report["table"]
    print("%s → %s：%s%s" % (args.dxf.name, args.out.name, report["caption"],
                             "，件号表 %d 行（%s，%d 栏）" % (len(tbl["rows"]), tbl["corner"],
                                                       tbl["blocks"]) if tbl else "，无件号表"))
    for w in report["warnings"]:
        print("警告：" + w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
