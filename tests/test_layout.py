"""Unit tests for scripts/patent_figure/layout.py.

Pure tests: synthetic numpy polylines only, no OCC, no ezdxf, no STEP. They run on a machine
with no CAD stack installed, which is the point of impl-contract §0 directive 3.

What is pinned here, and why:

* the DENSITY table and its semantics (§11.2.1 — three candidate designs mis-copied it by a
  factor of ten and mis-read it as a fraction of the *total string length*, which is the v1
  failure mechanism);
* postcondition P1, at BODY granularity (§11.3 S8) — members of one body may overlap, bodies
  may not;
* postcondition P0, zero lateral displacement;
* bitwise determinism across two in-process runs and one subprocess run (§11.9 item 13) and
  invariance under a permutation of the input list (§11.9 item 14);
* the reachable ``LayoutError`` exits of §11.8, none of which may degrade silently;
* the millimetre-domain helpers of §11.6.3, which ``render_patent_figure.py`` and ``qa.py``
  must import from here rather than restate.
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from patent_figure import layout as L  # noqa: E402


# --------------------------------------------------------------------------- fixtures

#: The sheet angle §11.1.2 solves to; used as the projected explode axis for every case here.
#: The renderer rotates the projection so the assembly axis lands at this angle, then hands
#: layout.py the rotated axis — so an axis at 124 degrees is exactly what layout.py sees.
AXIS_DEG = float(L.AXIS_ANGLE_DEFAULT)


def axis2d(deg: float = AXIS_DEG) -> np.ndarray:
    r = math.radians(deg)
    return np.array([math.cos(r), math.sin(r)], dtype=np.float64)


def box_curves(cx: float, cy: float, w: float, h: float) -> list:
    """One closed rectangular polyline centred on (cx, cy)."""
    return [np.array([[cx - w / 2.0, cy - h / 2.0],
                      [cx + w / 2.0, cy - h / 2.0],
                      [cx + w / 2.0, cy + h / 2.0],
                      [cx - w / 2.0, cy + h / 2.0],
                      [cx - w / 2.0, cy - h / 2.0]], dtype=np.float64)]


def uniform_string(n: int, size: float = 10.0) -> list:
    """``n`` equal squares, all distinct names, all at the origin.

    Distinct names means one body per piece (§11.3 S4 clusters by name first), and a shared
    starting position means the whole separation is produced by the difference-constraint
    solver rather than inherited from the input — which is what the postconditions are about.
    """
    return [L.Piece(key="P%02d#0" % i, name="P%02d" % i, curves=box_curves(0.0, 0.0, size, size))
            for i in range(n)]


def mixed_pieces() -> list:
    """Eight differently sized single-instance parts plus a same-named group at two stations.

    ``BOLT`` appears four times low on the axis and twice high up. §11.8 names the collapse of
    those two stations into one body as a must-fail point: the union bbox would straddle the
    whole housing and the displacement would be absurd.
    """
    pieces = [L.Piece(key="P%02d#0" % i, name="P%02d" % i,
                      curves=box_curves(0.0, 0.0, 10.0 + i, 8.0 + i))
              for i in range(8)]
    for j in range(4):
        pieces.append(L.Piece(key="BOLT#%d" % j, name="BOLT",
                              curves=box_curves(-15.0 + 7.0 * j, -2.0, 4.0, 4.0)))
    for j in range(4, 6):
        pieces.append(L.Piece(key="BOLT#%d" % j, name="BOLT",
                              curves=box_curves(-15.0 + 7.0 * (j - 4), 60.0, 4.0, 4.0)))
    return pieces


def usable_area(h: float):
    """(aw, ah) for text height ``h`` — the §11.6.3 recipe, spelled out so a change in
    ``label_margin_mm`` / ``caption_band_mm`` shows up here as a failure rather than silently."""
    lm, cb = L.label_margin_mm(h), L.caption_band_mm(h)
    return L.FRAME_W - 2.0 * lm, L.FRAME_H - lm - cb


def run(pieces, density: str = "normal", h: float = L.TEXT_FLOOR_MM):
    aw, ah = usable_area(h)
    return L.layout_exploded(pieces, axis2d(), density, sheet_aspect=aw / ah, max_rows=1)


def offsets(result) -> np.ndarray:
    return np.array([p.offset for p in result.pieces], dtype=np.float64)


def golden_offsets_hex() -> str:
    """Serialise the placement of ``mixed_pieces()`` for the subprocess determinism check.

    Lives in the test module so the in-process run and the subprocess run cannot drift into
    two different inputs (§11.9 item 13 asks for bitwise equality, which is only meaningful
    when both sides build the same pieces).
    """
    result = run(mixed_pieces())
    return offsets(result).tobytes().hex()


# --------------------------------------------------------------------------- constants


def test_density_table_is_the_frozen_one():
    """§11.2.1 / §5.2. Three candidate designs wrote 0.035/0.055/0.085 — ten times too small."""
    assert L.DENSITY == {"compact": 0.35, "normal": 0.55, "loose": 0.85}
    assert (L.FRAME_W, L.FRAME_H) == (170.0, 250.0)   # 170 = 210 - 25 - 15, CNIPA margins
    assert L.SLOT_FLOOR_MM == pytest.approx(L.TEXT_FLOOR_MM / L.QA_TEXT_SLOT_MAX)
    assert L.TEXT_SERIES == (7.0, 5.0, 3.5)
    assert list(L.TEXT_SERIES) == sorted(L.TEXT_SERIES, reverse=True)  # §11.6.3 scan precondition


def test_gap_is_a_fraction_of_the_median_body_extent_not_of_the_string():
    """The v1 failure mechanism: keying the gap to the total length makes the gap grow and the
    slot shrink as the string gets longer. Here the gap must be invariant to the part count."""
    short = run(uniform_string(4)).diagnostics
    long_ = run(uniform_string(20)).diagnostics
    assert short["gap"] == pytest.approx(long_["gap"])
    assert short["gap"] == pytest.approx(L.DENSITY["normal"] * short["median_body_extent"])

    gaps = [run(uniform_string(8), d).diagnostics["gap"] for d in ("compact", "normal", "loose")]
    assert gaps[1] / gaps[0] == pytest.approx(L.DENSITY["normal"] / L.DENSITY["compact"])
    assert gaps[2] / gaps[0] == pytest.approx(L.DENSITY["loose"] / L.DENSITY["compact"])


# --------------------------------------------------------------------------- P0 / P1


def _boxes_overlap(a, b, tol: float) -> bool:
    """Independent AABB overlap test: touching edges are not an overlap (§5.2)."""
    return (a["lo"][0] < b["hi"][0] - tol and b["lo"][0] < a["hi"][0] - tol
            and a["lo"][1] < b["hi"][1] - tol and b["lo"][1] < a["hi"][1] - tol)


def test_p1_placed_body_boxes_are_pairwise_disjoint():
    result = run(mixed_pieces())
    boxes = result.diagnostics["body_boxes"]
    tol = L.OVERLAP_TOL_REL * float(result.diagnostics["scale_ref"])
    assert len(boxes) >= 2
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            assert not _boxes_overlap(boxes[i], boxes[j], tol), (
                "%s / %s overlap" % (boxes[i]["key"], boxes[j]["key"]))
    assert result.diagnostics["overlaps"] == 0


def test_same_name_instances_cluster_by_axial_station_not_by_name():
    """§11.8: same-named screws at two stations must land in two bodies."""
    result = run(mixed_pieces())
    bolt_bodies = [b for b in result.diagnostics["body_boxes"] if b["key"].startswith("BOLT#c")]
    assert [b["key"] for b in bolt_bodies] == ["BOLT#c0", "BOLT#c1"]
    assert sorted(bolt_bodies[0]["members"]) == ["BOLT#0", "BOLT#1", "BOLT#2", "BOLT#3"]
    assert sorted(bolt_bodies[1]["members"]) == ["BOLT#4", "BOLT#5"]


def test_members_of_one_body_move_as_a_rigid_group():
    """A bolt circle keeps its shape: every member gets the identical offset, bit for bit."""
    result = run(mixed_pieces())
    by_key = {p.key: p.offset for p in result.pieces}
    station = [by_key["BOLT#%d" % j].tobytes() for j in range(4)]
    assert len(set(station)) == 1
    assert by_key["BOLT#4"].tobytes() == by_key["BOLT#5"].tobytes()
    # Two DIFFERENT bodies may legitimately receive the same displacement — the two BOLT
    # stations are already disjoint on the sheet, so the difference-constraint solver asks
    # nothing of either. Their separation is asserted at body granularity by the P1 test.


def test_members_inside_one_body_are_allowed_to_overlap():
    """P1 holds at body granularity only (§11.3 S8). Four bolts sharing one axial station stay
    where they are relative to each other even though their placed boxes may touch."""
    pieces = [L.Piece(key="PLATE#0", name="PLATE", curves=box_curves(0.0, 0.0, 40.0, 8.0))]
    for j in range(4):
        # deliberately overlapping squares, one station
        pieces.append(L.Piece(key="NUT#%d" % j, name="NUT",
                              curves=box_curves(-6.0 + 4.0 * j, 30.0, 8.0, 8.0)))
    result = run(pieces)
    bodies = {b["key"]: b for b in result.diagnostics["body_boxes"]}
    assert sorted(bodies) == ["NUT#c0", "PLATE#c0"]
    assert len(bodies["NUT#c0"]["members"]) == 4
    nut_offsets = [p.offset.tobytes() for p in result.pieces if p.name == "NUT"]
    assert len(set(nut_offsets)) == 1


def test_p0_displacement_has_no_lateral_component():
    result = run(mixed_pieces())
    e = axis2d() / float(np.hypot(*axis2d()))
    perp = np.array([-e[1], e[0]])
    scale_ref = float(result.diagnostics["scale_ref"])
    for piece in result.pieces:
        lateral = float(piece.offset[0] * perp[0] + piece.offset[1] * perp[1])
        assert round(lateral / scale_ref, L.RANK_DEC) == 0.0, piece.key


# --------------------------------------------------------------------------- determinism


def test_two_in_process_runs_are_bitwise_identical():
    """§11.9 item 13: bitwise, not allclose."""
    a = offsets(run(mixed_pieces()))
    b = offsets(run(mixed_pieces()))
    assert a.tobytes() == b.tobytes()


def test_subprocess_run_is_bitwise_identical():
    """§11.9 item 13, third run: a fresh interpreter must produce the same bytes."""
    code = (
        "import sys; sys.path[:0] = [%r, %r]; import test_layout as t; "
        "sys.stdout.write(t.golden_offsets_hex())" % (str(Path(__file__).resolve().parent),
                                                      str(_SCRIPTS))
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == golden_offsets_hex()


@pytest.mark.parametrize("perm", [
    [9, 0, 3, 11, 2, 13, 1, 5, 7, 4, 12, 6, 8, 10],
    [13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
    [0, 2, 4, 6, 8, 10, 12, 1, 3, 5, 7, 9, 11, 13],
    [7, 6, 5, 4, 3, 2, 1, 0, 13, 12, 11, 10, 9, 8],
    [11, 3, 7, 0, 13, 5, 9, 2, 6, 12, 1, 8, 4, 10],
])
def test_permutation_of_the_input_list_changes_nothing(perm):
    """§11.9 item 14. Re-running the same input proves nothing about insertion order; only a
    permutation shows that every sort key really is a total order ending in a unique value."""
    base = {p.key: p.offset.tobytes() for p in run(mixed_pieces()).pieces}
    shuffled = mixed_pieces()
    result = run([shuffled[i] for i in perm])
    assert {p.key: p.offset.tobytes() for p in result.pieces} == base
    assert [b["key"] for b in result.diagnostics["body_boxes"]] == \
           [b["key"] for b in run(mixed_pieces()).diagnostics["body_boxes"]]


def test_axis_sign_convention_makes_a_flipped_axis_identical():
    """§11.3 S2 normalises the sign, so an upstream convention flip cannot mirror the figure."""
    a = {p.key: p.offset.tobytes() for p in run(mixed_pieces()).pieces}
    aw, ah = usable_area(L.TEXT_FLOOR_MM)
    flipped = L.layout_exploded(mixed_pieces(), -axis2d(), "normal",
                                sheet_aspect=aw / ah, max_rows=1)
    assert {p.key: p.offset.tobytes() for p in flipped.pieces} == a


# --------------------------------------------------------------------------- sheet gates


def _sheet_metrics(result, h: float):
    """The millimetre quantities §11.6.3 computes in the renderer, recomputed here from the
    layout result alone (layout.py deliberately does not know millimetres, §11.0)."""
    aw, ah = usable_area(h)
    s = L.fit_to_frame(result, aw, ah, margin=0.0)
    gw = float(result.hi[0] - result.lo[0]) * s
    gh = float(result.hi[1] - result.lo[1]) * s
    sheet_fill = gw * gh / (aw * ah)
    extents = result.diagnostics["extent_by_key"]
    slot_sheet = s * min(extents[k] for k in sorted(extents))
    return s, sheet_fill, slot_sheet


def test_twenty_parts_meet_both_sheet_gates():
    """§11.1.2's headline claim, measured against the real implementation: a 20-part string at
    the closed-form angle fills the page and still supports a 3.5 mm numeral."""
    h = L.TEXT_FLOOR_MM
    result = run(uniform_string(20), h=h)
    _s, sheet_fill, slot_sheet = _sheet_metrics(result, h)
    assert result.diagnostics["bodies"] == 20
    assert result.rows == 1
    assert sheet_fill >= L.SHEET_FILL_MIN
    # slot >= 5.833 mm is what "h >= 3.5 with h/slot <= 0.6" reduces to (§11.1.1 D3)
    assert slot_sheet >= L.SLOT_FLOOR_MM
    assert L.snap_text_height(L.TEXT_RATIO * slot_sheet) is None or \
        L.snap_text_height(L.TEXT_RATIO * slot_sheet) >= L.TEXT_FLOOR_MM


def test_forty_parts_fall_through_the_slot_floor():
    """The other side of the same gate: this is the value §11.6.3 turns into the P8
    ``LayoutError`` ("标记数没超、件数超了"). layout.py itself does not raise — it cannot, it
    does not know millimetres — so the condition is asserted at the value level."""
    h = L.TEXT_FLOOR_MM
    result = run(uniform_string(40), h=h)
    _s, sheet_fill, slot_sheet = _sheet_metrics(result, h)
    assert sheet_fill >= L.SHEET_FILL_MIN          # the page is full; it is the parts that lose
    assert slot_sheet < L.SLOT_FLOOR_MM
    assert L.snap_text_height(L.TEXT_RATIO * slot_sheet) is None


def test_closed_form_angle_actually_maximises_sheet_fill():
    """§11.1.2. Check the PROPERTY, not a remembered number.

    An earlier version of this test asserted theta* was within 2 degrees of AXIS_ANGLE_DEFAULT
    (124), a value computed back when FRAME_W was 180. Correcting the frame to the compliant
    170 mm moved theta* to about 121.7 and the test failed even though the solver was right.
    A test that pins a derived constant breaks every time an input constant legitimately moves,
    and it tells you nothing about whether the closed form is correct. So assert what the closed
    form is actually for: that no other angle in the window fills the sheet better.
    """
    result = run(uniform_string(20))
    opt = float(result.diagnostics["axis_angle_opt"])
    assert L.ANGLE_LO <= opt <= L.ANGLE_HI

    h = L.TEXT_FLOOR_MM
    aw, ah = usable_area(h)
    alpha = aw / ah

    # Changing the sheet angle rotates the WHOLE sheet rigidly — render_patent_figure.py applies
    # the same _rotate() to every curve, so each part's footprint along the explode direction is
    # unchanged and the string keeps its shape. Only the axis-aligned bounding box turns. Model
    # exactly that: lay out once, then rotate the placed points.
    #
    # An earlier draft of this test instead swept axis2d() while holding the curves still. That
    # changes every footprint, so the string itself changes shape — an operation the renderer
    # never performs — and it "failed" against a solver that was right.
    placed = np.vstack([c for piece in run(uniform_string(20)).pieces for c in piece.placed()])

    def fill_at(deg: float) -> float:
        r = math.radians(deg - AXIS_DEG)
        cos_r, sin_r = math.cos(r), math.sin(r)
        x = placed[:, 0] * cos_r - placed[:, 1] * sin_r
        y = placed[:, 0] * sin_r + placed[:, 1] * cos_r
        gw, gh = float(x.max() - x.min()), float(y.max() - y.min())
        scale = min(aw / gw, ah / gh)
        return (gw * scale) * (gh * scale) / (aw * ah)

    best = fill_at(opt)
    for probe in range(int(L.ANGLE_LO), int(L.ANGLE_HI) + 1):
        got = fill_at(float(probe))
        assert got <= best + 1e-6, (
            "axis_angle_opt=%.3f claims to be optimal but %d fills better (%.6f > %.6f); "
            "alpha=%.6f" % (opt, probe, got, best, alpha))


def test_slot_is_clamped_on_both_gauges():
    """§11.10 #3. The reported slot never exceeds the qa-style diagonal/N gauge."""
    result = run(uniform_string(20))
    extents = result.diagnostics["extent_by_key"]
    contract_gauge = min(extents[k] for k in sorted(extents))
    diag = float(np.hypot(result.hi[0] - result.lo[0], result.hi[1] - result.lo[1]))
    qa_gauge = L.SLOT_QA_CAP_K * diag / len(result.pieces)
    assert result.slot == pytest.approx(min(contract_gauge, qa_gauge))
    # the renderer, not layout, fills this in (§11.6.2)
    assert result.diagnostics["slot_labelled"] is None


# --------------------------------------------------------------------------- fit_to_frame


def test_fit_to_frame_has_no_area_target_and_no_default_inset():
    """§11.10 #1 and #2: s is exactly the fitting scale, and margin defaults to 0.0."""
    result = run(uniform_string(6))
    gw = float(result.hi[0] - result.lo[0])
    gh = float(result.hi[1] - result.lo[1])
    aw, ah = usable_area(L.TEXT_FLOOR_MM)
    assert L.fit_to_frame(result, aw, ah) == pytest.approx(min(aw / gw, ah / gh))
    assert L.fit_to_frame(result, aw, ah, margin=0.0) == L.fit_to_frame(result, aw, ah)
    # the geometry really does reach one edge of the usable area
    s = L.fit_to_frame(result, aw, ah)
    assert min(abs(gw * s - aw), abs(gh * s - ah)) < 1e-6


def test_fit_to_frame_rejects_a_margin_that_eats_the_area():
    result = run(uniform_string(3))
    with pytest.raises(L.LayoutError):
        L.fit_to_frame(result, 152.0, 222.0, margin=0.5)


def test_fit_to_frame_never_mutates_the_pieces():
    result = run(uniform_string(5))
    before = offsets(result).tobytes()
    L.fit_to_frame(result, 152.0, 222.0, margin=0.0)
    assert offsets(result).tobytes() == before


# --------------------------------------------------------------------------- failure modes


def test_max_rows_other_than_one_is_an_error_never_a_downgrade():
    """§11.1 / §11.10 #4. Silent degradation would let a caller that passes the argument and
    one that forgets it render two different figures, both passing QA."""
    aw, ah = usable_area(L.TEXT_FLOOR_MM)
    for bad in (0, 2, 3):
        with pytest.raises(L.LayoutError) as exc:
            L.layout_exploded(uniform_string(3), axis2d(), "normal",
                              sheet_aspect=aw / ah, max_rows=bad)
        assert "max_rows" in str(exc.value)
        assert str(L.AXIS_ANGLE_DEFAULT) in str(exc.value)     # the message carries a repair


def test_empty_piece_list_is_an_error():
    with pytest.raises(L.LayoutError):
        L.layout_exploded([], axis2d(), "normal")
    with pytest.raises(L.LayoutError):
        L.layout_assembly([])


def test_unknown_density_is_an_error():
    with pytest.raises(L.LayoutError) as exc:
        L.layout_exploded(uniform_string(2), axis2d(), "airy")
    assert "density" in str(exc.value)


def test_piece_without_curves_is_named_not_skipped():
    pieces = uniform_string(3)
    pieces[1].curves = []
    with pytest.raises(L.LayoutError) as exc:
        run(pieces)
    assert pieces[1].key in str(exc.value)


def test_non_finite_coordinates_are_caught_before_placement():
    """NaN makes every comparison False, so the overlap check would pass spuriously (§11.8)."""
    pieces = uniform_string(3)
    pieces[2].curves = [np.array([[0.0, 0.0], [np.nan, 1.0]], dtype=np.float64)]
    with pytest.raises(L.LayoutError) as exc:
        run(pieces)
    assert pieces[2].key in str(exc.value)


def test_degenerate_piece_is_listed_with_a_repair_action():
    pieces = uniform_string(3)
    pieces[0].curves = box_curves(0.0, 0.0, 0.0, 0.0)
    with pytest.raises(L.LayoutError) as exc:
        run(pieces)
    assert pieces[0].key in str(exc.value)
    assert "exclude" in str(exc.value)


def test_axis_almost_parallel_to_the_view_direction_is_an_error():
    with pytest.raises(L.LayoutError) as exc:
        L.layout_exploded(uniform_string(3), np.array([1e-6, 0.0]), "normal")
    assert "view" in str(exc.value) or "explode_axis" in str(exc.value)


def test_whole_scene_collapsed_to_a_point_is_an_error():
    pieces = [L.Piece(key="A#0", name="A", curves=[np.array([[1.0, 1.0]], dtype=np.float64)]),
              L.Piece(key="B#0", name="B", curves=[np.array([[1.0, 1.0]], dtype=np.float64)])]
    with pytest.raises(L.LayoutError):
        L.layout_exploded(pieces, axis2d(), "normal")


# --------------------------------------------------------------------------- layout_assembly


def test_layout_assembly_keeps_every_part_in_place():
    pieces = mixed_pieces()
    result = L.layout_assembly(pieces)
    assert all(p.offset.tolist() == [0.0, 0.0] for p in result.pieces)
    assert result.diagnostics["strategy"] == "in-place"
    assert result.diagnostics["gap"] == 0.0
    assert result.diagnostics["fill_usable"] is None
    assert result.diagnostics["axis_angle_opt"] is None
    assert result.diagnostics["slot_labelled"] is None
    assert len(result.diagnostics["body_boxes"]) == len(pieces)
    assert result.rows == 1


def test_layout_assembly_does_not_check_overlap():
    """Parts occlude one another in an assembly view by construction; the global HLR handles it
    (§11.4). Two coincident parts must NOT raise here."""
    pieces = [L.Piece(key="A#0", name="A", curves=box_curves(0.0, 0.0, 10.0, 10.0)),
              L.Piece(key="B#0", name="B", curves=box_curves(1.0, 1.0, 10.0, 10.0))]
    result = L.layout_assembly(pieces)
    assert result.diagnostics["overlaps"] == 0


def test_layout_assembly_rejects_a_degenerate_part():
    pieces = [L.Piece(key="A#0", name="A", curves=box_curves(0.0, 0.0, 10.0, 10.0)),
              L.Piece(key="B#0", name="B", curves=box_curves(5.0, 5.0, 0.0, 0.0))]
    with pytest.raises(L.LayoutError) as exc:
        L.layout_assembly(pieces)
    assert "B#0" in str(exc.value)


# --------------------------------------------------------------------------- §11.6.3 helpers


def test_label_margin_and_caption_band_are_capped():
    assert L.label_margin_mm(3.5) == pytest.approx(L.LABEL_MARGIN_K * 3.5)
    assert L.label_margin_mm(7.0) == L.LABEL_MARGIN_CAP_MM        # the cap binds from h = 4
    assert L.caption_band_mm(3.5) == pytest.approx(L.CAPTION_K * 3.5)
    assert L.caption_band_mm(7.0) == L.CAPTION_CAP_MM


def test_snap_text_height_walks_the_gb_series_downwards():
    assert L.snap_text_height(9.0) == 7.0
    assert L.snap_text_height(7.0) == 7.0
    assert L.snap_text_height(6.9) == 5.0
    assert L.snap_text_height(3.5) == 3.5
    assert L.snap_text_height(3.4999) is None
    assert L.snap_text_height(0.0) is None


def test_piece_geometry_helpers_apply_the_offset():
    piece = L.Piece(key="A#0", name="A", curves=box_curves(0.0, 0.0, 4.0, 2.0))
    piece.offset = np.array([10.0, -3.0])
    assert piece.lo.tolist() == [8.0, -4.0]
    assert piece.hi.tolist() == [12.0, -2.0]
    assert piece.placed()[0][0].tolist() == [8.0, -4.0]
    # placed() must not write back into curves
    assert piece.curves[0][0].tolist() == [-2.0, -1.0]
