"""annotate_figure_sheet.py：图号 + 件号名称表（只在副本上做）。

纯 ezdxf 构造的小图纸，不依赖 OCC，跑得快。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest
from ezdxf.enums import TextEntityAlignment

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import annotate_figure_sheet as AN  # noqa: E402

CLI = _SCRIPTS / "annotate_figure_sheet.py"


def _sheet(path: Path, numerals, geom_boxes, caption="回转组件分解示意图", h=5.0):
    doc = ezdxf.new("R2018")
    for layer in ("GEOM", "LEADER", "NUM", "CAPTION"):
        doc.layers.add(layer, linetype="CONTINUOUS")
    doc.styles.add("HZ", font="simfang.ttf")
    doc.styles.add("NUM", font="txt.shx")
    msp = doc.modelspace()
    for x0, y0, x1, y1 in geom_boxes:
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                           dxfattribs={"layer": "GEOM"})
    for i, n in enumerate(numerals):
        t = msp.add_text(str(n), height=h, dxfattribs={"layer": "NUM", "style": "NUM"})
        t.set_placement((150.0, 230.0 - 12 * i), align=TextEntityAlignment.MIDDLE_LEFT)
    cap = msp.add_text(caption, height=1.6 * h, dxfattribs={"layer": "CAPTION", "style": "HZ"})
    cap.set_placement((85.0, 8.0), align=TextEntityAlignment.MIDDLE_CENTER)
    doc.saveas(str(path))


def _numerals(path: Path, mapping):
    path.write_text(json.dumps({"schema": "patent-numerals/1", "numerals": [
        {"numeral": n, "term": t, "selector": "SYN-%d" % n, "figures": ["fig1"]}
        for n, t in mapping.items()]}, ensure_ascii=False), encoding="utf-8")


def _texts(path: Path, layer: str):
    return [e.dxf.text for e in ezdxf.readfile(str(path)).modelspace().query("TEXT")
            if e.dxf.layer == layer]


def _table_box(path: Path):
    xs, ys = [], []
    for e in ezdxf.readfile(str(path)).modelspace().query("LINE"):
        if e.dxf.layer == "TABLE":
            for p in (e.dxf.start, e.dxf.end):
                xs.append(p.x); ys.append(p.y)
    return min(xs), min(ys), max(xs), max(ys)


def test_caption_becomes_figure_number_and_source_untouched(tmp_path):
    src, num = tmp_path / "fig1.dxf", tmp_path / "num.json"
    _sheet(src, [1, 2], [(60, 120, 140, 240)])
    _numerals(num, {1: "底壳", 2: "中框", 3: "未上图的件"})
    before = hashlib.sha256(src.read_bytes()).hexdigest()
    rep = AN.annotate(src, tmp_path / "out.dxf", num, 3)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == before
    assert _texts(tmp_path / "out.dxf", "CAPTION") == ["图3"]
    assert rep["figure_description"] == "图3为回转组件分解示意图；"


def test_table_lists_only_numerals_on_this_sheet_in_order(tmp_path):
    src, num = tmp_path / "fig1.dxf", tmp_path / "num.json"
    _sheet(src, [7, 2, 12], [(60, 120, 140, 240)])
    _numerals(num, {2: "中框", 7: "摇臂", 12: "输出齿轮", 30: "别的图上的件"})
    rep = AN.annotate(src, tmp_path / "out.dxf", num, 1)
    assert [r["numeral"] for r in rep["table"]["rows"]] == [2, 7, 12]
    table_text = _texts(tmp_path / "out.dxf", "TABLE")
    assert table_text[:2] == ["序号", "名 称"]
    assert "输出齿轮" in table_text and "别的图上的件" not in table_text


def test_table_sits_in_free_space_without_touching_geometry(tmp_path):
    src, num = tmp_path / "fig1.dxf", tmp_path / "num.json"
    geom = [(80, 100, 165, 245), (100, 20, 165, 95)]   # 左下角空着
    _sheet(src, [1, 2, 3], geom)
    _numerals(num, {1: "底壳", 2: "中框", 3: "内齿轮支架"})
    rep = AN.annotate(src, tmp_path / "out.dxf", num, 1)
    assert rep["table"]["outside_usable_area"] is False
    assert rep["table"]["corner"] == "左下"
    x0, y0, x1, y1 = _table_box(tmp_path / "out.dxf")
    for gx0, gy0, gx1, gy1 in geom:
        assert not (x0 < gx1 and gx0 < x1 and y0 < gy1 and gy0 < y1)


def test_full_sheet_falls_back_below_drawing_with_warning(tmp_path):
    src, num = tmp_path / "fig1.dxf", tmp_path / "num.json"
    _sheet(src, [1, 2, 3, 4], [(0, 20, 170, 250)])
    _numerals(num, {1: "底壳", 2: "中框", 3: "内齿轮支架", 4: "第一回转轴承"})
    rep = AN.annotate(src, tmp_path / "out.dxf", num, 1)
    assert rep["table"]["outside_usable_area"] is True and rep["warnings"]
    _, _, _, y1 = _table_box(tmp_path / "out.dxf")
    assert y1 < 20                                  # 整张表在图形下方


def test_many_rows_split_into_two_blocks_when_tall(tmp_path):
    src, num = tmp_path / "fig1.dxf", tmp_path / "num.json"
    nums = list(range(1, 25))
    _sheet(src, nums, [(60, 150, 165, 250)], h=7.0)
    _numerals(num, {n: "零件%d" % n for n in nums})
    rep = AN.annotate(src, tmp_path / "out.dxf", num, 5)
    assert rep["table"]["blocks"] == 2
    assert _texts(tmp_path / "out.dxf", "TABLE").count("序号") == 2


def test_no_table_mode_only_renumbers_caption(tmp_path):
    src, num = tmp_path / "fig1.dxf", tmp_path / "num.json"
    _sheet(src, [1], [(60, 120, 140, 240)])
    _numerals(num, {1: "底壳"})
    rep = AN.annotate(src, tmp_path / "out.dxf", num, 2, with_table=False)
    assert rep["table"] is None
    assert _texts(tmp_path / "out.dxf", "TABLE") == []
    assert _texts(tmp_path / "out.dxf", "CAPTION") == ["图2"]


def test_numeral_without_name_is_an_error(tmp_path):
    src, num = tmp_path / "fig1.dxf", tmp_path / "num.json"
    _sheet(src, [1, 9], [(60, 120, 140, 240)])
    _numerals(num, {1: "底壳"})
    with pytest.raises(AN.AnnotateError):
        AN.annotate(src, tmp_path / "out.dxf", num, 1)


def test_cli_refuses_to_overwrite_input(tmp_path):
    src, num = tmp_path / "fig1.dxf", tmp_path / "num.json"
    _sheet(src, [1], [(60, 120, 140, 240)])
    _numerals(num, {1: "底壳"})
    proc = subprocess.run([sys.executable, str(CLI), str(src), "--numerals", str(num),
                           "--figure-number", "1", "-o", str(src)],
                          capture_output=True, text=True)
    assert proc.returncode == 2


def test_hollow_outline_counts_as_solid_when_layout_json_present(tmp_path):
    """壳体轮廓围出的空心不是空白：有 layout.json 时按零件包围盒整体占位。"""
    src, num = tmp_path / "fig1.dxf", tmp_path / "num.json"
    doc = ezdxf.new("R2018")
    for layer in ("GEOM", "NUM", "CAPTION"):
        doc.layers.add(layer)
    doc.styles.add("HZ", font="simfang.ttf")
    doc.styles.add("NUM", font="txt.shx")
    msp = doc.modelspace()
    # 一个 160 x 220 的空心「机身」，由四条独立线段围成
    for a, b in (((5, 25), (165, 25)), ((165, 25), (165, 245)),
                 ((165, 245), (5, 245)), ((5, 245), (5, 25))):
        msp.add_line(a, b, dxfattribs={"layer": "GEOM"})
    t = msp.add_text("1", height=5.0, dxfattribs={"layer": "NUM", "style": "NUM"})
    t.set_placement((168.0, 200.0), align=TextEntityAlignment.MIDDLE_LEFT)
    cap = msp.add_text("整体结构示意图", height=8, dxfattribs={"layer": "CAPTION", "style": "HZ"})
    cap.set_placement((85.0, 8.0), align=TextEntityAlignment.MIDDLE_CENTER)
    doc.saveas(str(src))
    (tmp_path / "fig1.layout.json").write_text(json.dumps(
        {"body_boxes": [{"key": "A#c0", "lo": [5, 25], "hi": [165, 245], "members": ["A#0"]}]}),
        encoding="utf-8")
    _numerals(num, {1: "机身"})
    rep = AN.annotate(src, tmp_path / "out.dxf", num, 1)
    x0, y0, x1, y1 = _table_box(tmp_path / "out.dxf")
    assert not (x0 < 165 and 5 < x1 and y0 < 245 and 25 < y1), "表格被放进了机身轮廓里"
    assert rep["table"]["outside_usable_area"] is True
