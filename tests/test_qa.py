"""Unit tests for scripts/patent_figure/qa.py.

No OCC: every figure under test is built in millimetre sheet coordinates from synthetic
rectangles, laid out with ``labels.place_labels`` and written with ``sheet.write_figure``.
That keeps the fixtures honest — they are produced by the same two modules the renderer uses,
so a QA gate cannot pass here and fail on a real figure for reasons of file shape.

The two gates added by architect rulings D2 and D3 (§11.1.1) get an isolated case each,
because the point of both is that the *old* gates cannot see the defect:

* ``tiny_text`` fails ``text_height_mm`` alone while ``text_slot_ratio`` passes — §11.6.4
  proves the ratio is constructively satisfied and therefore blind;
* ``flat`` fails ``sheet_fill`` alone while ``geometry_occupancy`` passes — D1 proves
  occupancy is scale-invariant and therefore cannot measure page filling.

``dirty`` reproduces the v1 failure signature (parts table on the sheet, internal part codes
in the text) and must fail ``geometry_occupancy``, ``non_numeral_text_ratio`` and
``forbidden_text`` together.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from patent_figure import labels as LB  # noqa: E402
from patent_figure import layout as LY  # noqa: E402
from patent_figure import qa as QA  # noqa: E402
from patent_figure import sheet as SH  # noqa: E402


# --------------------------------------------------------------------------- fixtures


def rect(xlo: float, ylo: float, xhi: float, yhi: float) -> np.ndarray:
    return np.array([[xlo, ylo], [xhi, ylo], [xhi, yhi], [xlo, yhi], [xlo, ylo]],
                    dtype=np.float64)


def write_figure(path: Path, boxes, h: float, caption: str = "结构示意图",
                 engineering_rows=None, label_boxes=None):
    """Write a figure whose geometry is ``boxes`` (x0, y0, x1, y1 in mm) with one numeral per
    entry of ``label_boxes`` (defaults to every box)."""
    geometry = [rect(*b) for b in boxes]
    targets = boxes if label_boxes is None else label_boxes
    requests = [LB.LabelRequest(key="K%d#0" % i, numeral=i + 1,
                                lo=np.array(b[:2], dtype=np.float64),
                                hi=np.array(b[2:], dtype=np.float64))
                for i, b in enumerate(targets)]
    placements = LB.place_labels(requests, obstacles=geometry, text_height=h,
                                 sheet_lo=(0.0, 0.0), sheet_hi=(LY.FRAME_W, LY.FRAME_H))
    SH.write_figure(path, geometry=geometry, labels=placements, caption=caption,
                    text_height=h, caption_height=LY.CAPTION_RATIO * h,
                    engineering_rows=engineering_rows)
    return path


def stacked_boxes(h: float, width_fraction: float = 0.75, n: int = 3):
    """``n`` stacked bars filling the usable area for text height ``h``, leaving a column free
    on the right so the numerals have somewhere legal to go."""
    lm, cb = LY.label_margin_mm(h), LY.caption_band_mm(h)
    aw, ah = LY.FRAME_W - 2.0 * lm, LY.FRAME_H - lm - cb
    box_h = ah / n
    return [(lm, cb + i * box_h + 1.0, lm + aw * width_fraction, cb + (i + 1) * box_h - 1.0)
            for i in range(n)]


def good_figure(tmp_path: Path, h: float = 3.5) -> Path:
    return write_figure(tmp_path / "fig_good.dxf", stacked_boxes(h), h)


def by_id(report: dict) -> dict:
    return {c["id"]: c for c in report["checks"]}


def failed_ids(report: dict):
    return sorted(c["id"] for c in report["checks"] if not c["pass"])


# --------------------------------------------------------------------------- report shape


def test_good_figure_passes_every_gate(tmp_path):
    report = QA.check_figure(good_figure(tmp_path), kind="exploded")
    assert report["pass"] is True, failed_ids(report)
    assert report["schema"] == "patent-figure-qa/1"
    assert report["file"] == "fig_good.dxf"
    assert report["summary"]["failed"] == 0
    assert report["summary"]["passed"] == len(report["checks"])
    assert [c["id"] for c in report["checks"]] == list(QA.CHECK_ORDER)
    for check in report["checks"]:
        assert check["detail"].strip() and check["hint"].strip(), check["id"]
        assert set(check) == {"id", "pass", "value", "threshold", "detail", "hint"}


def test_thresholds_are_the_frozen_ones():
    """§5.5, verbatim — including the two gates added by rulings D2 and D3."""
    assert QA.DEFAULT_THRESHOLDS == {
        "geometry_occupancy_min": 0.55,
        "sheet_fill_min": 0.55,
        "label_overlap_pairs_max": 0,
        "text_height_mm_min": 3.5,
        "text_slot_ratio_max": 0.6,
        "part_bbox_overlap_pairs_max": 0,
        "labels_per_figure_max": 20,
        "non_numeral_text_ratio_max": 0.10,
        "leader_crossing_max": 0,
        # 引线锚点必须落在零件轮廓上：偏离量不得超过图幅对角的 1%。
        # 实测缺陷：锚点曾取自包围盒，真实装配体 40 个里 29 个偏离 >0.5mm、最远达对角的 12%。
        "leader_anchor_gap_ratio_max": 0.01,
        "leader_hits_numeral_box_max": 0,
        "leader_anchor_gap_ratio_max": 0.01,
        "non_continuous_max": 0,
    }
    assert QA.FORBIDDEN_TEXT_PATTERNS == [r"^[A-Z]{2,4}[0-9]{4,8}(-|_)",
                                          r"^[0-9]{4,6}-[A-Z][0-9]{2}",
                                          r"_[0-9]+_[0-9]+$"]


def test_usable_area_matches_the_layout_definition():
    """§11.10 revision 2: qa and layout must reconstruct the same usable area, or ``sheet_fill``
    is measured against a denominator the renderer never used."""
    for h in LY.TEXT_SERIES:
        assert QA.label_margin_mm(h) == LY.label_margin_mm(h)
        assert QA.caption_band_mm(h) == LY.caption_band_mm(h)
        aw, ah = QA.usable_area_mm(h)
        assert aw == LY.FRAME_W - 2.0 * LY.label_margin_mm(h)
        assert ah == LY.FRAME_H - LY.label_margin_mm(h) - LY.caption_band_mm(h)


# --------------------------------------------------------------------------- the v1 signature


def test_dirty_figure_reproduces_the_v1_failure_signature(tmp_path):
    """A parts table on a filing figure: exactly what §8 forbids and what R2 shipped."""
    rows = [("1", "PRT0001-A", "2", "外购"),
            ("2", "1234-A01", "1", "自制"),
            ("3", "底座_12_3", "4", "")]
    path = write_figure(tmp_path / "fig_dirty_engineering.dxf",
                        [(70.0, 120.0, 110.0, 150.0)], 3.5,
                        engineering_rows=rows, label_boxes=[])
    report = QA.check_figure(path, kind="exploded")
    assert report["pass"] is False
    failures = failed_ids(report)
    assert "geometry_occupancy" in failures
    assert "non_numeral_text_ratio" in failures
    assert "forbidden_text" in failures
    checks = by_id(report)
    # the failure baseline measured 0.1353 (§11.1.1 D1); a table dilutes the sheet the same way
    assert checks["geometry_occupancy"]["value"] < 0.3
    assert checks["forbidden_text"]["value"] == 3
    assert "engineering_table" in checks["non_numeral_text_ratio"]["hint"]


def test_forbidden_patterns_hit_part_codes_and_spare_chinese_nouns():
    """The regexes of §5.5, exercised directly so a pattern edit shows up here first."""
    compiled = [re.compile(p) for p in QA.FORBIDDEN_TEXT_PATTERNS]

    def hits(text):
        return any(rx.search(text) for rx in compiled)

    for bad in ("PRT0001-A", "ABCD12345678-", "PRT0001_x", "1234-A01", "123456-Z99",
                "底座_12_3", "SHAFT_07_2"):
        assert hits(bad), bad
    for ok in ("底座", "回转轴组件", "紧固螺钉", "上盖", "1", "12", "整体结构示意图"):
        assert not hits(ok), ok


def test_forbidden_text_is_caught_in_the_caption_too(tmp_path):
    path = write_figure(tmp_path / "fig_caption.dxf", stacked_boxes(3.5), 3.5,
                        caption="PRT0001-A 装配图")
    report = QA.check_figure(path, kind="exploded")
    assert "forbidden_text" in failed_ids(report)


# --------------------------------------------------------------------------- the two new gates


def test_text_height_gate_fires_where_the_ratio_gate_is_blind(tmp_path):
    """Ruling D3. ``text_height_for`` freezes h/slot at 0.45, so ``text_slot_ratio`` can never
    fail; only the absolute floor catches an unreadable numeral."""
    h = 2.5                                     # below TEXT_FLOOR_MM, still a GB series size
    path = write_figure(tmp_path / "fig_tiny_text.dxf", stacked_boxes(h), h)
    report = QA.check_figure(path, kind="exploded")
    assert failed_ids(report) == ["text_height_mm"]
    checks = by_id(report)
    assert checks["text_height_mm"]["value"] == pytest.approx(2.5)
    assert checks["text_height_mm"]["threshold"] == ">=3.5"
    assert checks["text_slot_ratio"]["pass"] is True        # blind, exactly as §11.6.4 says


def test_sheet_fill_gate_fires_where_occupancy_is_blind(tmp_path):
    """Ruling D2. ``geometry_occupancy`` is scale-invariant, so a small, badly proportioned
    drawing can pass it while leaving two thirds of the paper empty."""
    path = write_figure(tmp_path / "fig_flat.dxf", [(14.0, 110.0, 166.0, 150.0)], 3.5)
    report = QA.check_figure(path, kind="exploded")
    assert failed_ids(report) == ["sheet_fill"]
    checks = by_id(report)
    assert checks["geometry_occupancy"]["pass"] is True
    assert checks["sheet_fill"]["value"] < QA.DEFAULT_THRESHOLDS["sheet_fill_min"]
    aw, ah = QA.usable_area_mm(3.5)
    assert checks["sheet_fill"]["value"] == pytest.approx((166.0 - 14.0) * 40.0 / (aw * ah),
                                                          abs=1e-4)
    assert "axis_angle" in checks["sheet_fill"]["hint"]


def test_zero_label_figure_leaves_the_text_gates_unjudged(tmp_path):
    """A figure whose parts are all ``label:"none"`` is legal (§11.6.3): the text gates report
    ``value: null`` and pass rather than failing a figure that has nothing to measure."""
    path = write_figure(tmp_path / "fig_nolabel.dxf", stacked_boxes(3.5), 3.5, label_boxes=[])
    report = QA.check_figure(path, kind="exploded")
    checks = by_id(report)
    assert checks["text_height_mm"]["value"] is None and checks["text_height_mm"]["pass"]
    assert checks["text_slot_ratio"]["value"] is None and checks["text_slot_ratio"]["pass"]
    assert checks["labels_per_figure"]["value"] == 0


# --------------------------------------------------------------------------- sidecar


def _sidecar(path: Path, bodies):
    doc = {"schema": "patent-figure-layout/1", "figure": path.stem, "kind": "exploded",
           "units": "mm", "body_boxes": [
               {"key": key, "lo": [lo[0], lo[1]], "hi": [hi[0], hi[1]], "members": [key]}
               for key, lo, hi in bodies]}
    (path.parent / (path.stem + ".layout.json")).write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_overlapping_bodies_in_the_sidecar_fail_the_gate(tmp_path):
    """§11.10 revision 5: the sidecar is the only input with discriminating power here — two
    overlapping parts merge into one connected cluster under the degraded reading."""
    path = good_figure(tmp_path)
    _sidecar(path, [("A#c0", (20.0, 20.0), (100.0, 100.0)),
                    ("B#c0", (90.0, 90.0), (160.0, 160.0))])
    report = QA.check_figure(path, kind="exploded")
    check = by_id(report)["part_bbox_overlap_pairs"]
    assert check["pass"] is False and check["value"] == 1
    assert "A#c0" in check["detail"] and "B#c0" in check["detail"]
    assert "退化口径" not in check["detail"]


def test_disjoint_bodies_in_the_sidecar_pass(tmp_path):
    path = good_figure(tmp_path)
    _sidecar(path, [("A#c0", (20.0, 20.0), (80.0, 80.0)),
                    ("B#c0", (90.0, 90.0), (160.0, 160.0))])
    report = QA.check_figure(path, kind="exploded")
    assert by_id(report)["part_bbox_overlap_pairs"]["pass"] is True
    assert report["pass"] is True, failed_ids(report)


def test_missing_sidecar_is_reported_as_a_degraded_reading(tmp_path):
    path = good_figure(tmp_path)
    check = by_id(QA.check_figure(path, kind="exploded"))["part_bbox_overlap_pairs"]
    assert check["pass"] is True
    assert "退化口径" in check["detail"]        # says so out loud rather than pretending


def test_assembly_figures_skip_the_overlap_gate(tmp_path):
    """Parts occlude one another in an assembly view by construction (§5.5)."""
    path = good_figure(tmp_path)
    _sidecar(path, [("A#c0", (20.0, 20.0), (100.0, 100.0)),
                    ("B#c0", (90.0, 90.0), (160.0, 160.0))])
    report = QA.check_figure(path, kind="assembly")
    check = by_id(report)["part_bbox_overlap_pairs"]
    assert check["pass"] is True and check["value"] == 0
    assert "assembly" in check["detail"]


# --------------------------------------------------------------------------- robustness


def test_check_figure_never_raises_on_an_unreadable_file(tmp_path):
    """§5.5: 'Never raises on a bad drawing — it reports.'"""
    broken = tmp_path / "not_a.dxf"
    broken.write_text("这不是 DXF", encoding="utf-8")
    report = QA.check_figure(broken)
    assert report["pass"] is False
    assert [c["id"] for c in report["checks"]] == ["file_readable"]
    assert report["checks"][0]["hint"].strip()

    missing = QA.check_figure(tmp_path / "nowhere.dxf")
    assert missing["pass"] is False
    assert missing["checks"][0]["id"] == "file_readable"


def test_thresholds_can_be_overridden_per_call(tmp_path):
    path = write_figure(tmp_path / "fig_flat2.dxf", [(14.0, 110.0, 166.0, 150.0)], 3.5)
    strict = QA.check_figure(path, kind="exploded")
    assert "sheet_fill" in failed_ids(strict)
    relaxed = QA.check_figure(path, kind="exploded", thresholds={"sheet_fill_min": 0.1})
    assert "sheet_fill" not in failed_ids(relaxed)
    assert by_id(relaxed)["sheet_fill"]["threshold"] == ">=0.1"


def test_report_is_stable_across_runs(tmp_path):
    path = good_figure(tmp_path)
    first = json.dumps(QA.check_figure(path, kind="exploded"), ensure_ascii=False, sort_keys=True)
    second = json.dumps(QA.check_figure(path, kind="exploded"), ensure_ascii=False, sort_keys=True)
    assert first == second


def test_anchor_gap_gate_catches_leaders_that_point_at_nothing(tmp_path):
    """回归：引线锚点必须落在它所指的零件上。

    实测缺陷——锚点取自零件包围盒而非轮廓，真实装配体上最远偏离 28.8mm，
    占图幅对角 12%，读图者根本看不出标记指的是哪个件。这里手工把锚点搬离几何来复现。
    """
    import ezdxf
    h = 5.0
    boxes = stacked_boxes(h, n=2)
    path = write_figure(tmp_path / "fig_ok.dxf", boxes, h)
    ok = QA.check_figure(path, kind="exploded")
    assert by_id(ok)["leader_anchor_gap"]["pass"] is True, by_id(ok)["leader_anchor_gap"]

    # 把每条引线的锚点端整体外移——模拟「锚点落在包围盒角点」的效果
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()
    # 引线的锚点端 = 与 LEADER 图层锚点圆重合的那个端点。实测渲染器与本测试的
    # write_figure 辅助都写三点 LWPOLYLINE；两种形态都处理，是为了实体形态一旦改变，
    # 这道闸门的自测不会静默失效（改成 LINE 也照样搬得动）。
    dots = [(e.dxf.center[0], e.dxf.center[1]) for e in msp
            if e.dxf.layer == "LEADER" and e.dxftype() == "CIRCLE"]

    def _is_anchor(x, y):
        return any(abs(x - cx) < 1e-6 and abs(y - cy) < 1e-6 for cx, cy in dots)

    moved = 0
    for e in list(msp):
        if e.dxf.layer != "LEADER":
            continue
        kind = e.dxftype()
        if kind == "LWPOLYLINE":
            pts = [(pt[0], pt[1]) for pt in e.get_points()]
            pts[0] = (pts[0][0] - 25.0, pts[0][1] - 25.0)
            e.set_points(pts, format="xy")
            moved += 1
        elif kind == "LINE":
            sx, sy = float(e.dxf.start.x), float(e.dxf.start.y)
            ex, ey = float(e.dxf.end.x), float(e.dxf.end.y)
            if _is_anchor(sx, sy):
                e.dxf.start = (sx - 25.0, sy - 25.0, 0.0); moved += 1
            elif _is_anchor(ex, ey):
                e.dxf.end = (ex - 25.0, ey - 25.0, 0.0); moved += 1
        elif kind == "CIRCLE":
            cx, cy = float(e.dxf.center.x), float(e.dxf.center.y)
            e.dxf.center = (cx - 25.0, cy - 25.0)
    assert moved > 0, "没有找到可搬动的引线锚点端——引线实体形态与预期不符"
    bad = tmp_path / "fig_bad_anchor.dxf"
    doc.saveas(str(bad))

    report = QA.check_figure(bad, kind="exploded")
    gate = by_id(report)["leader_anchor_gap"]
    assert gate["pass"] is False, gate
    assert gate["value"] > 2.0
    assert "遮挡" in gate["hint"] or "轮廓" in gate["hint"]
    assert report["pass"] is False


def test_anchor_gap_threshold_is_scale_free():
    """阈值按图幅对角的比例给出，换图幅不会误判。"""
    assert QA.DEFAULT_THRESHOLDS["leader_anchor_gap_ratio_max"] == 0.01
