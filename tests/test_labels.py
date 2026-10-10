"""Unit tests for scripts/patent_figure/labels.py.

Pure tests: synthetic millimetre-space rectangles, no OCC, no ezdxf.

The postconditions of §5.3 / §11.8 (P2..P5b) are re-verified here with predicates written
*independently of the module* — an orientation test for segment crossing, a plain interval
test for box overlap, and a numeral box reconstructed from CHAR_W / PAD_X / PAD_Y exactly the
way ``qa.py`` reconstructs it from a DXF. Reusing ``labels._seg_cross`` would only prove the
function agrees with itself; the point of the invariant is that a *second* implementation,
reading only the public ``LabelPlacement``, finds nothing wrong.

The unsatisfiable case is tested too, and is not a defect: §5.3 makes it the mechanism that
forces a figure to be split rather than allowing the v1 outcome of 97% overlapping numerals.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from patent_figure import labels as LB  # noqa: E402

SHEET_LO = (0.0, 0.0)
SHEET_HI = (180.0, 250.0)          # §11.7: the whole FRAME, not the geometry usable area


# --------------------------------------------------------------------------- fixtures


def rect(cx: float, cy: float, w: float, h: float) -> np.ndarray:
    return np.array([[cx - w / 2.0, cy - h / 2.0],
                     [cx + w / 2.0, cy - h / 2.0],
                     [cx + w / 2.0, cy + h / 2.0],
                     [cx - w / 2.0, cy + h / 2.0],
                     [cx - w / 2.0, cy - h / 2.0]], dtype=np.float64)


def grid_case(n: int, size: float, gap: float, cols: int = 3,
              x0: float = 40.0, y0: float = 40.0):
    """``n`` square parts on a grid; every part is both a label request and an obstacle."""
    requests, obstacles = [], []
    for i in range(n):
        row, col = divmod(i, cols)
        cx = x0 + col * (size + gap)
        cy = y0 + row * (size + gap)
        requests.append(LB.LabelRequest(
            key="K%02d#0" % i, numeral=i + 1,
            lo=np.array([cx - size / 2.0, cy - size / 2.0]),
            hi=np.array([cx + size / 2.0, cy + size / 2.0])))
        obstacles.append(rect(cx, cy, size, size))
    return requests, obstacles


def place(requests, obstacles, h):
    return LB.place_labels(requests, obstacles=obstacles, text_height=h,
                           sheet_lo=SHEET_LO, sheet_hi=SHEET_HI)


def serialise(placements) -> str:
    return repr([(p.numeral, p.key, p.text_pos, p.text_align, p.leader, round(p.score, 9))
                 for p in placements])


# --------------------------------------------------------------------------- independent geometry


def numeral_box(placement, h: float):
    """Reconstruct the numeral's bounding box from the public placement only.

    Same recipe as ``qa.py`` uses on a DXF (§11.2.3): width = CHAR_W*h per digit, padded by
    PAD_X*h horizontally and PAD_Y*h vertically around a glyph of height h, anchored at
    ``text_pos`` with MIDDLE_LEFT / MIDDLE_RIGHT alignment.
    """
    width = LB.CHAR_W * h * len(str(int(placement.numeral)))
    x = placement.text_pos[0] if placement.text_align == "left" \
        else placement.text_pos[0] - width
    y = placement.text_pos[1]
    return (x - LB.PAD_X * h, y - 0.5 * h - LB.PAD_Y * h,
            x + width + LB.PAD_X * h, y + 0.5 * h + LB.PAD_Y * h)


def boxes_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(p0, p1, q0, q1) -> bool:
    """Proper crossing only — shared endpoints and collinear overlap do not count."""
    d1, d2 = _orient(p0, p1, q0), _orient(p0, p1, q1)
    d3, d4 = _orient(q0, q1, p0), _orient(q0, q1, p1)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def segment_hits_box(p0, p1, box) -> bool:
    for pt in (p0, p1):
        if box[0] <= pt[0] <= box[2] and box[1] <= pt[1] <= box[3]:
            return True
    corners = ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
    for k in range(4):
        if segments_cross(p0, p1, corners[k], corners[(k + 1) % 4]):
            return True
    return False


def leader_segments(placement):
    pts = placement.leader
    return [(pts[0], pts[1]), (pts[1], pts[2])]


def assert_postconditions(placements, requests, h):
    """P2 / P4 / P5 / P5b re-verified from the public result alone."""
    boxes = [numeral_box(p, h) for p in placements]
    for i in range(len(placements)):
        # P4 — inside the sheet
        assert SHEET_LO[0] <= boxes[i][0] and SHEET_LO[1] <= boxes[i][1]
        assert boxes[i][2] <= SHEET_HI[0] and boxes[i][3] <= SHEET_HI[1]
        for j in range(i + 1, len(placements)):
            # P2 — numeral boxes never overlap
            assert not boxes_overlap(boxes[i], boxes[j]), \
                "numerals %d/%d overlap" % (placements[i].numeral, placements[j].numeral)
            # P5 — leaders never cross
            for a in leader_segments(placements[i]):
                for b in leader_segments(placements[j]):
                    assert not segments_cross(a[0], a[1], b[0], b[1]), \
                        "leaders %d/%d cross" % (placements[i].numeral, placements[j].numeral)
            # P5b — a leader never runs through another numeral's box
            for a in leader_segments(placements[i]):
                assert not segment_hits_box(a[0], a[1], boxes[j])
            for b in leader_segments(placements[j]):
                assert not segment_hits_box(b[0], b[1], boxes[i])
    # the leader really starts on the part it labels (§11.7.2)
    for placement, request in zip(placements, sorted(requests, key=lambda r: r.numeral)):
        assert placement.key == request.key


# --------------------------------------------------------------------------- happy path


@pytest.mark.parametrize("n,size,gap,cols,h", [
    (6, 20.0, 15.0, 3, 5.0),
    (9, 20.0, 12.0, 3, 3.5),
    (12, 18.0, 10.0, 4, 3.5),
])
def test_every_label_is_placed_and_the_invariants_hold(n, size, gap, cols, h):
    requests, obstacles = grid_case(n, size, gap, cols)
    placements = place(requests, obstacles, h)
    assert len(placements) == n
    assert [p.numeral for p in placements] == list(range(1, n + 1))   # L9 ascending numeral
    assert_postconditions(placements, requests, h)


def test_numeral_boxes_do_not_sit_on_the_geometry():
    """P3, checked against the obstacle polylines themselves."""
    h = 3.5
    requests, obstacles = grid_case(9, 20.0, 12.0)
    placements = place(requests, obstacles, h)
    for placement in placements:
        box = numeral_box(placement, h)
        for arr in obstacles:
            for k in range(len(arr) - 1):
                assert not segment_hits_box(tuple(arr[k]), tuple(arr[k + 1]), box), \
                    "numeral %d sits on geometry" % placement.numeral


def test_leader_shape_is_slant_then_horizontal_landing():
    """§11.7.3: three points — anchor, elbow, landing end — with a horizontal landing, and the
    text sitting RUNOUT_K*h beyond the landing end, never on it."""
    h = 5.0
    requests, obstacles = grid_case(6, 20.0, 15.0, 3)
    placements = place(requests, obstacles, h)
    for placement in placements:
        anchor, elbow, land = placement.leader
        assert len(placement.leader) == 3
        assert land[1] == pytest.approx(elbow[1])                 # landing is horizontal
        assert abs(land[0] - elbow[0]) == pytest.approx(LB.LAND_K * h)
        assert placement.text_align in ("left", "right")
        sign = 1.0 if placement.text_align == "left" else -1.0
        assert placement.text_pos[0] == pytest.approx(land[0] + sign * LB.RUNOUT_K * h)
        assert placement.text_pos[1] == pytest.approx(land[1])
        # the slanted part is never axis-aligned (PHASE_DEG = 15 => 15/45/.../345 degrees)
        assert round(anchor[0] - elbow[0], LB.RANK_DEC) != 0.0
        assert round(anchor[1] - elbow[1], LB.RANK_DEC) != 0.0


def test_anchor_hint_steers_the_preferred_direction():
    """§11.7.2: the hint must participate in the decision, not be decoration.

    Its first job is to set the preferred direction. (Its second job — overriding the anchor of
    the least-deviating direction — only shows up when that candidate actually wins the score;
    a hint set back from the outline lengthens the leader and can be out-scored by a neighbour
    direction. That is the scoring working as specified in §11.2.3, not a defect, so the test
    pins the direction effect, which is unconditional.)
    """
    h = 5.0
    lo, hi = np.array([80.0, 120.0]), np.array([110.0, 150.0])
    obstacles = [rect(95.0, 135.0, 30.0, 30.0)]
    left = place([LB.LabelRequest(key="A#0", numeral=1, lo=lo, hi=hi,
                                  anchor_hint=np.array([82.0, 132.0]))], obstacles, h)[0]
    none = place([LB.LabelRequest(key="A#0", numeral=1, lo=lo, hi=hi)], obstacles, h)[0]
    assert left.text_pos[0] < float(lo[0])          # hinted left -> label on the left
    assert none.text_pos[0] > float(hi[0])          # single part, no hint -> +x by convention
    assert left.text_align == "right" and none.text_align == "left"


def test_the_leader_starts_on_the_part_outline():
    """§11.7.2: without a hint the anchor is the ray exit of the part AABB, so it always lies
    on the box boundary — never floating in space and never buried inside."""
    h = 3.5
    requests, obstacles = grid_case(9, 20.0, 12.0)
    for placement, request in zip(place(requests, obstacles, h),
                                  sorted(requests, key=lambda r: r.numeral)):
        ax, ay = placement.leader[0]
        lo, hi = request.lo, request.hi
        assert float(lo[0]) - 1e-9 <= ax <= float(hi[0]) + 1e-9
        assert float(lo[1]) - 1e-9 <= ay <= float(hi[1]) + 1e-9
        on_edge = (abs(ax - float(lo[0])) < 1e-9 or abs(ax - float(hi[0])) < 1e-9
                   or abs(ay - float(lo[1])) < 1e-9 or abs(ay - float(hi[1])) < 1e-9)
        assert on_edge, "anchor of numeral %d is not on the outline" % placement.numeral


def test_two_digit_numerals_get_a_wider_box():
    h = 3.5
    one = place([LB.LabelRequest(key="A#0", numeral=9,
                                 lo=np.array([80.0, 120.0]), hi=np.array([100.0, 140.0]))],
                [rect(90.0, 130.0, 20.0, 20.0)], h)[0]
    two = place([LB.LabelRequest(key="A#0", numeral=10,
                                 lo=np.array([80.0, 120.0]), hi=np.array([100.0, 140.0]))],
                [rect(90.0, 130.0, 20.0, 20.0)], h)[0]
    w1 = numeral_box(one, h)[2] - numeral_box(one, h)[0]
    w2 = numeral_box(two, h)[2] - numeral_box(two, h)[0]
    assert w2 - w1 == pytest.approx(LB.CHAR_W * h)


# --------------------------------------------------------------------------- determinism


def test_repeated_calls_are_byte_for_byte_identical():
    """§11.9 item 15: 100 reruns, serialised, all equal."""
    h = 5.0
    first = serialise(place(*grid_case(6, 20.0, 15.0, 3), h=h))
    for _ in range(99):
        assert serialise(place(*grid_case(6, 20.0, 15.0, 3), h=h)) == first


@pytest.mark.parametrize("perm", [
    [5, 4, 3, 2, 1, 0],
    [2, 0, 4, 1, 5, 3],
    [3, 5, 1, 4, 0, 2],
])
def test_request_order_does_not_change_the_result(perm):
    """§11.9 item 15, second half: the greedy order is derived from a total key, so permuting
    the input list cannot change any placement."""
    h = 5.0
    base = serialise(place(*grid_case(6, 20.0, 15.0, 3), h=h))
    requests, obstacles = grid_case(6, 20.0, 15.0, 3)
    shuffled = serialise(place([requests[i] for i in perm],
                               [obstacles[i] for i in perm], h))
    assert shuffled == base


def test_direction_table_is_phase_shifted_and_axis_free():
    """§11.2.3: 12 directions at 15/45/.../345 degrees, none axis-aligned, all rounded to 12
    decimals so libm noise cannot differ between platforms."""
    assert len(LB._DIRS) == LB.N_DIRS
    for dx, dy in LB._DIRS:
        assert dx != 0.0 and dy != 0.0
        assert abs(dx * dx + dy * dy - 1.0) < 1e-9
        assert dx == round(dx, LB.DIR_ROUND_DEC) and dy == round(dy, LB.DIR_ROUND_DEC)


# --------------------------------------------------------------------------- unsatisfiable


@pytest.mark.parametrize("n,size,gap,cols,h", [
    (12, 6.0, 1.0, 4, 7.0),
    (16, 5.0, 0.5, 4, 7.0),
    (20, 4.0, 0.5, 5, 7.0),
])
def test_dense_figure_raises_label_error_with_unplaced_numerals(n, size, gap, cols, h):
    """§5.3: this unsatisfiability is the design, not a defect — it is what forces a split
    instead of the v1 outcome where 97% of the numerals overlapped."""
    requests, obstacles = grid_case(n, size, gap, cols, x0=80.0, y0=110.0)
    with pytest.raises(LB.LabelError) as exc:
        place(requests, obstacles, h)
    err = exc.value
    assert isinstance(err.unplaced, list) and err.unplaced
    assert all(isinstance(v, int) for v in err.unplaced)
    assert set(err.unplaced) <= {r.numeral for r in requests}
    assert err.tried == LB.N_DIRS * LB.RING_COUNT == 48
    assert "split_suggestions" in str(err)      # the message names an executable plan edit


def test_place_labels_never_places_a_label_anyway():
    """The failure must be an exception, never a degraded partial result."""
    requests, obstacles = grid_case(16, 5.0, 0.5, 4, x0=80.0, y0=110.0)
    try:
        place(requests, obstacles, 7.0)
    except LB.LabelError:
        return
    pytest.fail("dense case placed labels instead of raising")


def test_empty_request_list_is_not_an_error():
    """A figure whose parts are all label:"none" is legal (§11.6.3)."""
    assert place([], [rect(90.0, 130.0, 20.0, 20.0)], 3.5) == []


def test_non_positive_text_height_is_rejected():
    for bad in (0.0, -1.0):
        with pytest.raises(LB.LabelError) as exc:
            place([LB.LabelRequest(key="A#0", numeral=1,
                                   lo=np.array([80.0, 120.0]), hi=np.array([100.0, 140.0]))],
                  [rect(90.0, 130.0, 20.0, 20.0)], bad)
        assert exc.value.unplaced == [] and exc.value.tried == 0


# --------------------------------------------------------------------------- text_height_for


def test_text_height_for_is_the_frozen_ratio():
    assert LB.text_height_for(10.0) == pytest.approx(4.5)
    assert LB.text_height_for(10.0, 0.5) == pytest.approx(5.0)
    assert LB.TEXT_RATIO == 0.45
    # §11.6.4: the ratio gate is constructively satisfied and therefore has no discriminating
    # power — that is why qa.py must also carry the absolute text_height_mm floor.
    assert LB.text_height_for(10.0) / 10.0 <= 0.6


import math as _math  # noqa: E402

# --------------------------------------------------------------------------- anchors on outline


def _ring(cx, cy, r_out, r_in, n=64):
    """一个圆环：包围盒中心是空的，包围盒角点更是空的——最能暴露「锚点取包围盒」的错误。"""
    import math as _m
    outer = np.array([[cx + r_out * _m.cos(2 * _m.pi * i / n),
                       cy + r_out * _m.sin(2 * _m.pi * i / n)] for i in range(n + 1)])
    inner = np.array([[cx + r_in * _m.cos(2 * _m.pi * i / n),
                       cy + r_in * _m.sin(2 * _m.pi * i / n)] for i in range(n + 1)])
    return [outer, inner]


def _min_dist_to_points(p, pts):
    return float(np.min(np.hypot(pts[:, 0] - p[0], pts[:, 1] - p[1])))


def test_every_anchor_sits_on_the_part_outline():
    """回归：引线锚点必须落在零件真实轮廓上，不能落在包围盒上。

    实测过的缺陷：除首选方向外，锚点一律取自 AABB 出射点，圆环/L 形零件的锚点因此悬在
    空处——真实装配体上 40 个锚点里 29 个偏离几何 >0.5mm，最远 28.8mm（图幅对角的 12%）。
    这里用圆环阵列复现同一条件：包围盒角点离轮廓最远，若实现退回 AABB 必然失败。
    """
    reqs, outlines = [], {}
    for i in range(6):
        cx, cy = 40.0 * i, 0.0
        curves = _ring(cx, cy, 15.0, 9.0)
        pts = np.vstack(curves)
        outlines[i] = pts
        reqs.append(LB.LabelRequest(
            key="ring%d" % i, numeral=i + 1,
            lo=np.array([cx - 15.0, cy - 15.0]), hi=np.array([cx + 15.0, cy + 15.0]),
            anchor_hint=None, outline=pts))
    placed = LB.place_labels(reqs, obstacles=[np.vstack(list(outlines.values()))],
                            text_height=3.5, sheet_lo=(-60.0, -90.0), sheet_hi=(260.0, 90.0))
    assert len(placed) == len(reqs)
    by_numeral = {p.numeral: p for p in placed}
    for i in range(6):
        anchor = by_numeral[i + 1].leader[0]
        assert _min_dist_to_points(anchor, outlines[i]) <= 1e-6, (
            "标记 %d 的锚点 %r 不在轮廓上——落回包围盒了" % (i + 1, anchor))
