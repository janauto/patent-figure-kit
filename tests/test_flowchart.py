"""patent_figure/flowchart.py：语义 JSON → 确定性流程图。纯 ezdxf，不依赖 OCC。"""

from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

import ezdxf
import pytest
from ezdxf import bbox as _bbox

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from patent_figure import flowchart as FC  # noqa: E402
from patent_figure import sheet as SH  # noqa: E402

SPEC = {
    "schema": "patent-flowchart/1",
    "title": "工件加工检测方法的流程图",
    "nodes": [
        {"id": "s", "kind": "start", "text": "开始"},
        {"id": "detect", "kind": "process", "text": "检测传送带上是否有工件"},
        {"id": "q1", "kind": "decision", "text": "检测到工件？"},
        {"id": "lock", "kind": "process", "text": "夹紧工件并启动加工"},
        {"id": "q2", "kind": "decision", "text": "加工尺寸是否合格？"},
        {"id": "track", "kind": "process", "text": "卸下工件送入成品区"},
        {"id": "alarm", "kind": "process", "text": "停机并提示更换刀具"},
        {"id": "idle", "kind": "process", "text": "传送带空转等待"},
        {"id": "e", "kind": "end", "text": "结束"},
    ],
    "edges": [
        {"from": "s", "to": "detect"}, {"from": "detect", "to": "q1"},
        {"from": "q1", "to": "lock", "label": "是"}, {"from": "q1", "to": "idle", "label": "否"},
        {"from": "lock", "to": "q2"}, {"from": "q2", "to": "track", "label": "是"},
        {"from": "q2", "to": "alarm", "label": "否"}, {"from": "alarm", "to": "detect"},
        {"from": "track", "to": "e"}, {"from": "idle", "to": "e"},
    ],
}


def _codes(spec):
    return {i["code"] for i in FC.validate(spec) if i["severity"] == "error"}


def test_valid_spec_has_no_errors():
    assert _codes(SPEC) == set()


@pytest.mark.parametrize("mutate,code", [
    (lambda s: s["nodes"][1].update(text="S101 检测工件"), "E_STEP_NUMBER_IN_TEXT"),
    (lambda s: s["edges"].append({"from": "e", "to": "zz"}), "E_EDGE_UNKNOWN_NODE"),
    (lambda s: s["edges"].remove({"from": "q1", "to": "idle", "label": "否"}), "E_DECISION_EDGES"),
    (lambda s: s["nodes"].append({"id": "lost", "kind": "process", "text": "孤立步骤"}),
     "E_UNREACHABLE"),
    (lambda s: s["nodes"][1].update(x=10), "E_SCHEMA"),
    (lambda s: s.update(width=120), "E_SCHEMA"),
    (lambda s: s["nodes"].append({"id": "s", "kind": "process", "text": "重名"}), "E_DUP_ID"),
])
def test_validator_catches(mutate, code):
    bad = copy.deepcopy(SPEC)
    mutate(bad)
    assert code in _codes(bad)


def test_steps_issued_in_flow_order_and_skip_terminals():
    sol = FC.solve(SPEC)
    steps = [(sol.nodes[n].id, sol.nodes[n].step) for n in sol.order if sol.nodes[n].step]
    assert steps[0] == ("detect", "S101")
    assert [s for _, s in steps] == ["S%d" % (101 + i) for i in range(len(steps))]
    assert sol.nodes["s"].step == "" and sol.nodes["e"].step == ""
    short = dict(SPEC, step_style="S1")
    assert FC.solve(short).nodes["detect"].step == "S1"


def _segments(points):
    return list(zip(points, points[1:]))


def test_edges_never_pass_through_a_box_interior():
    sol = FC.solve(SPEC)
    for ed in sol.edges:
        for (x0, y0), (x1, y1) in _segments(ed.points):
            for n in sol.nodes.values():
                if n.id in (ed.src, ed.dst):
                    continue
                # 轴向线段与框内部（缩 0.5 mm）不得相交
                lx, hx = sorted((x0, x1)); ly, hy = sorted((y0, y1))
                inside = (lx < n.right - 0.5 and hx > n.left + 0.5
                          and ly < n.top - 0.5 and hy > n.bottom + 0.5)
                assert not inside, "%s→%s 穿过了 %s" % (ed.src, ed.dst, n.id)


def test_dxf_is_continuous_and_deterministic(tmp_path):
    a, b = tmp_path / "a.dxf", tmp_path / "b.dxf"
    res = FC.write(SPEC, a, figure_number=7)
    FC.write(SPEC, b, figure_number=7)
    assert SH.normalized_digest(a) == SH.normalized_digest(b)
    doc = ezdxf.readfile(str(a))
    assert all(e.dxf.linetype in ("CONTINUOUS", "ByLayer") for e in doc.modelspace())
    texts = {e.dxf.text for e in doc.modelspace().query("TEXT")}
    assert "图7" in texts and "S101" in texts
    assert res["figure_description"] == "图7为工件加工检测方法的流程图；"
    assert [s["step"] for s in res["steps"]][:2] == ["S101", "S102"]


def test_everything_fits_the_usable_area(tmp_path):
    out = tmp_path / "f.dxf"
    FC.write(SPEC, out, figure_number=1)
    ext = _bbox.extents(ezdxf.readfile(str(out)).modelspace())
    assert ext.extmin.x >= -0.5 and ext.extmax.x <= FC.FRAME_W + 0.5
    assert ext.extmin.y >= -0.5 and ext.extmax.y <= FC.FRAME_H + 0.5


def test_long_flow_keeps_text_inside_boxes_and_warns(tmp_path):
    """步骤很多时不再「文字保底、框照缩」：整图按字高下限等比缩放，放不下就如实告警。"""
    n = 16
    nodes = [{"id": "s", "kind": "start", "text": "开始"}]
    nodes += [{"id": "p%d" % i, "kind": "process", "text": "第%d道工序：检测并记录工件状态" % i}
              for i in range(n)]
    nodes += [{"id": "e", "kind": "end", "text": "结束"}]
    ids = [x["id"] for x in nodes]
    spec = {"schema": FC.SCHEMA, "title": "长流程",
            "nodes": nodes, "edges": [{"from": a, "to": b} for a, b in zip(ids, ids[1:])]}
    sol = FC.solve(spec)
    assert sol.scale >= FC.TEXT_FLOOR_MM / FC.TEXT_H - 1e-9
    assert sol.warnings and "拆" in sol.warnings[0]
    res = FC.write(spec, tmp_path / "long.dxf", figure_number=3)
    assert res["text_height_mm"] >= FC.TEXT_FLOOR_MM
