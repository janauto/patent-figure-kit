"""Pure-2D sheet placement for patent figures.

No OCC, no ezdxf, no millimetres: this module works in *projected model units* and only ever
sees a dimensionless ``sheet_aspect``.  Everything here is a literal transcription of
``docs/impl-contract.md`` §11.3 / §11.4 / §11.5; every constant is back-solved from a stated
constraint in §11.2 and is defined exactly once (§7 rule 7).

Layer of responsibility (contract §11.0):

===========================  =========================  ==========================
module                       coordinate system          knows millimetres?
===========================  =========================  ==========================
``layout_exploded``          projected 2D               no  (only ``sheet_aspect``)
``layout_assembly``          projected 2D               no
``fit_to_frame``             projected 2D -> pure ratio only via its own arguments
``place_labels``             sheet mm                   yes
``render_patent_figure.py``  bridge                     yes
===========================  =========================  ==========================

The millimetre-domain constants of §11.2.2 live here too (that table mandates
``定义在 layout.py``); ``render_patent_figure.py`` and ``qa.py`` must *import* them and the three
helpers at the bottom rather than restating the formulas, so the three modules can never drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# =============================================================================================
# §11.2.1 constants — layout geometry
# =============================================================================================

# §5.2 frozen values, copied digit for digit.  Semantics are frozen too: the gap is a fraction of
# the MEDIAN body footprint along the explode direction, NOT a fraction of the total string
# length.  Keying it to the total length is the v1 failure mechanism (a longer string shrank every
# part's slot while the gap grew).
DENSITY: dict[str, float] = {"compact": 0.35, "normal": 0.55, "loose": 0.85}

# §5.2 frozen: A4 portrait minus a 15 mm margin, in millimetres.
# A4 is 297 x 210 mm. The CNIPA drawing-sheet margins (审查指南 五部一章 4.3) are
# top 25 / left 25 / right 15 / bottom 15 mm, so the compliant usable area is
#   width  = 210 - 25 - 15 = 170
#   height = 297 - 25 - 15 = 257
# An earlier draft froze the width at 180, which is geometrically unplaceable: 25 + 180 + 15 = 220
# exceeds the 210 mm sheet, so EVERY figure would have violated the left margin. Height stays at
# the conservative 250 rather than the full 257.
FRAME_W, FRAME_H = 170.0, 250.0

# §7 rule 4: round before comparing.
RANK_DEC = 9

# Relative quantisation floor.  float64 carries 2.2e-16 of relative precision, so 1e-9 leaves seven
# orders of magnitude to absorb the rounding of <=1e5 operations.  It MUST be relative: this module
# receives model units, and the same assembly exported in metres rather than millimetres would get
# an absolute tolerance that is either inert or over-merging.
EPS_REL = 1e-9

# §5.2's frozen 1e-6 overlap tolerance, restated as a relative quantity (§11.10 #5).
OVERLAP_TOL_REL = 1e-6

# §5.1 ``PartShape.degenerate`` (max extent < 1e-6), likewise restated as a relative quantity.
DEGEN_REL = 1e-6

# ``axis2d`` is a UNIT 3D axis projected onto the sheet, so its norm naturally lies in [0, 1] and an
# absolute threshold is legitimate here.  Below 1e-3 the sheet direction has fewer than three
# significant digits while ``_sep_required`` divides by its components.  Legacy used 1e-6: at
# k = 1e-5 the displacement was amplified 1e5-fold without any warning.
AXIS_MIN_NORM = 1e-3

# Per-component version of the same bound: ``_sep_required`` divides by ``e[k]``, and a branch with
# ``|e[k]| < 1e-3`` amplifies by more than 1e3, so it is skipped.  Skipping is safe — that branch's
# ``t`` is astronomically large anyway and ``min()`` would never pick it.
AXIS_COMP_MIN = 1e-3

# §4.2 frozen clamp range for ``axis_angle``.
ANGLE_LO, ANGLE_HI = 120.0, 180.0

# Fixed-step resolution used when the closed-form ``theta*`` has no solution.  Plans write
# ``axis_angle`` in whole degrees, so 1° is the smallest expressible edit; finer buys nothing.
# A fixed 61-step sweep is deterministic and needs no convergence criterion.
ANGLE_GRID_DEG = 1.0

# If qa measures the slot as ``geometry diagonal / label count`` (the failure baseline's algorithm),
# then ``0.45*slot <= 0.6*(diag/N)`` requires ``slot <= 1.333*diag/N``; 1.2 keeps 10% of margin.
# One line of cost buys passage under both readings.
SLOT_QA_CAP_K = 1.2

# Architect ruling D4 (§11.1.2): the closed-form ``theta*`` is 124.17 / 124.17 / 124.40 across the
# three text sizes, rounded to 124.  Kept only as the fallback starting point — §4.2 rules that the
# plan default is the string "auto" and the renderer solves ``axis_angle_opt`` per figure.
AXIS_ANGLE_DEFAULT = 124

# =============================================================================================
# §11.2.2 constants — sheet solving.  Defined here, consumed by render_patent_figure.py and qa.py.
# =============================================================================================

# GB/T 14691 text-height series 1.8/2.5/3.5/5/7/10/14/20, restricted to the three sizes usable for
# patent reference numerals.  DESCENDING order is a precondition of the §11.6 scan.
TEXT_SERIES = (7.0, 5.0, 3.5)

# GB/T 14691 smallest numeral height.  2.5 mm reduced to 2/3 on printing leaves 1.67 mm, illegible
# once filed.
TEXT_FLOOR_MM = 3.5

# §5.3 frozen default ratio of ``text_height_for``; leaves 25% of margin under the QA cap of 0.6.
TEXT_RATIO = 0.45

# §5.5 ``text_slot_ratio_max``.
QA_TEXT_SLOT_MAX = 0.6

# Derived, never given its own literal: the minimum on-sheet outline of a LABELLED part.
SLOT_FLOOR_MM = TEXT_FLOOR_MM / QA_TEXT_SLOT_MAX

# A leader reaching outward from the geometry edge occupies
# RING_BASE_K(1.0) + LAND_K(2.0) + RUNOUT_K(0.5) + PAD_X(0.3) = 3.8h, rounded up to 4.0h.
LABEL_MARGIN_K = 4.0

# = 8.9% x FRAME_W.  Any larger and the two side gutters eat 18% of the sheet width, dropping the
# usable-area fraction below 0.70 and starving ``sheet_fill``.  Only binds when h >= 5.
LABEL_MARGIN_CAP_MM = 16.0

# Caption band = caption height (CAPTION_RATIO*h) + 1.2h of clearance above and below = 4.0h.
CAPTION_K = 4.0

# Same reasoning as LABEL_MARGIN_CAP_MM.
CAPTION_CAP_MM = 16.0

# The caption runs one and a half sizes larger than the reference numerals — standard practice in
# Chinese mechanical drafting — and keeps the caption at 5.6 mm (still readable) when h = 3.5.
CAPTION_RATIO = 1.6

# Architect ruling D2 (§11.1.1): the page-filling gate.
# ``sheet_fill = area(geometry bbox) / (aw*ah) = min(gamma/alpha, alpha/gamma)``.  The denominator
# is the USABLE area, not the whole FRAME: against the full frame, square geometry can only reach
# 0.487-0.513 at any text size — structurally unreachable — while its aspect ratio is
# rotation-invariant, so the "change axis_angle" hint would change nothing at all.
SHEET_FILL_MIN = 0.55


class LayoutError(RuntimeError):
    """Carries enough information for the caller to emit a plan-repair hint (§11.8)."""


# ---------------------------------------------------------------------------------------------
# Data shapes (§5.2)
# ---------------------------------------------------------------------------------------------


def _stack(curves: list[np.ndarray]) -> np.ndarray:
    """Concatenate a piece's polylines into a single (M,2) float64 array."""
    return np.vstack([np.asarray(c, dtype=np.float64) for c in curves])


@dataclass
class Piece:
    """One drawable part instance: view-projected 2D polylines plus a placement offset."""

    key: str
    name: str
    curves: list[np.ndarray]  # 2D, unplaced (view-projected, model position)
    offset: np.ndarray = field(default_factory=lambda: np.zeros(2))

    @property
    def lo(self) -> np.ndarray:
        """Placed bbox min (curves min + offset)."""
        return _stack(self.curves).min(axis=0) + self.offset

    @property
    def hi(self) -> np.ndarray:
        """Placed bbox max (curves max + offset)."""
        return _stack(self.curves).max(axis=0) + self.offset

    def placed(self) -> list[np.ndarray]:
        """The polylines translated by ``offset``.  Never mutates ``curves``."""
        return [np.asarray(c, dtype=np.float64) + self.offset for c in self.curves]


@dataclass
class LayoutResult:
    """Placement outcome.  ``slot`` is dual-gauge (§11.10 #3); millimetre gates live in the
    renderer, which is the only layer that knows millimetres."""

    pieces: list[Piece]
    lo: np.ndarray
    hi: np.ndarray
    slot: float
    rows: int
    diagnostics: dict


# ---------------------------------------------------------------------------------------------
# Deterministic arithmetic helpers (§11.9 items 5 and 6)
# ---------------------------------------------------------------------------------------------


def _dot2(P: np.ndarray, v: np.ndarray) -> np.ndarray:
    """P:(M,2) v:(2,) -> (M,).  Writing ``P @ v`` is FORBIDDEN.

    An (M,2)@(2,) product may be dispatched to a multithreaded BLAS gemv whose blocking and
    reduction order vary with CPU and thread count.  Floating-point addition is not associative, so
    the last bits would vary per machine.  Element-wise multiply-add performs no cross-element
    reduction and is bit-reproducible.
    """
    return P[:, 0] * v[0] + P[:, 1] * v[1]


def _lower_median(vals: list[float]) -> float:
    """Lower median.  ``np.median`` is FORBIDDEN: it goes through introselect partitioning whose
    path varies with the numpy version, and for even lengths it averages two values as well.
    Sorting and taking ``(n-1)//2`` is purely deterministic.
    """
    s = sorted(vals)
    return s[(len(s) - 1) // 2]


def _closed_form_best_angle(L: float, V: float, alpha: float, eps: float) -> float:
    """The ``axis_angle`` (degrees) that maximises ``sheet_fill``, clamped to [ANGLE_LO, ANGLE_HI].

    ``sheet_fill`` peaks where ``Gw/Gh == alpha``, i.e. ``tan|theta| = (L - alpha*V)/(alpha*L - V)``.
    The expression is singular as the denominator approaches zero (``L/V -> 1/alpha``) and has no
    solution at all for ``L/V`` inside ``[alpha, 1/alpha]``, because a block that squat cannot reach
    an aspect ratio of ``alpha`` under any rotation.  Those cases fall back to the fixed-step sweep.
    """
    num, den = L - alpha * V, alpha * L - V
    if den > eps and num > eps:  # eps = EPS_REL * SCALE_REF — the same ruler used everywhere
        th = 180.0 - math.degrees(math.atan(num / den))
        if ANGLE_LO <= th <= ANGLE_HI:
            return round(th, RANK_DEC)
    best, best_fill = ANGLE_LO, -1.0
    steps = int(round((ANGLE_HI - ANGLE_LO) / ANGLE_GRID_DEG))  # fixed count, no convergence test
    for k in range(steps + 1):
        th = ANGLE_LO + k * ANGLE_GRID_DEG
        c, s = abs(math.cos(math.radians(th))), abs(math.sin(math.radians(th)))
        gw, gh = L * c + V * s, L * s + V * c
        sc = min(alpha / gw, 1.0 / gh)
        f = round((gw * sc) * (gh * sc) / alpha, RANK_DEC)
        if f > best_fill:  # strictly greater => ties keep the smaller angle
            best_fill, best = f, th
    return best


#: Refinement grid for :func:`_refine_axis_angle`.  Two fixed passes, no convergence test, so the
#: evaluation count is a constant and the result cannot depend on floating-point luck.
REFINE_SPAN_DEG = 3.0
REFINE_COARSE_DEG = 0.5
REFINE_FINE_DEG = 0.05


def _true_fill(points: np.ndarray, ref_deg: float, deg: float, alpha: float) -> float:
    """``sheet_fill`` of the placed geometry after rolling the sheet from ``ref_deg`` to ``deg``.

    Rolling rotates the whole sheet rigidly — the renderer applies the same rotation to every
    curve — so this is the real objective, not a model of it.
    """
    r = math.radians(deg - ref_deg)
    cos_r, sin_r = math.cos(r), math.sin(r)
    x = points[:, 0] * cos_r - points[:, 1] * sin_r
    y = points[:, 0] * sin_r + points[:, 1] * cos_r
    gw = float(x.max() - x.min())
    gh = float(y.max() - y.min())
    if gw <= 0.0 or gh <= 0.0:
        return 0.0
    k = min(alpha / gw, 1.0 / gh)
    return round((gw * k) * (gh * k) / alpha, RANK_DEC)


def _refine_axis_angle(points: np.ndarray, ref_deg: float, estimate: float,
                       alpha: float) -> float:
    """Correct the closed-form estimate against the geometry that is actually on the sheet.

    :func:`_closed_form_best_angle` models the exploded string as one ``L x V`` rectangle, which is
    what makes it closed form. A real string is not a rectangle — the parts have different
    perpendicular extents — so its rotated bounding box does not follow ``L|cos| + V|sin|`` exactly
    and the estimate lands near the optimum rather than on it. Measured on a 20-part uniform
    string: the estimate gave 121.72 degrees for a true optimum of 122.1, and because sheet_fill is
    sharply peaked there that 0.4 degree cost 1.4% of the page.

    Two fixed-step passes around the estimate, scored on the true objective. Deterministic by
    construction: constant evaluation count, ties keep the smaller angle.
    """
    best, best_fill = estimate, _true_fill(points, ref_deg, estimate, alpha)
    for step in (REFINE_COARSE_DEG, REFINE_FINE_DEG):
        centre = best
        n = int(round(REFINE_SPAN_DEG / step)) if step == REFINE_COARSE_DEG else \
            int(round(REFINE_COARSE_DEG / step))
        for k in range(-n, n + 1):
            probe = round(centre + k * step, RANK_DEC)
            if not (ANGLE_LO <= probe <= ANGLE_HI):
                continue
            fill = _true_fill(points, ref_deg, probe, alpha)
            if fill > best_fill:  # strictly greater => ties keep the smaller angle
                best_fill, best = fill, probe
    return round(best, RANK_DEC)


# ---------------------------------------------------------------------------------------------
# §11.3 layout_exploded
# ---------------------------------------------------------------------------------------------


def layout_exploded(
    pieces: list[Piece],
    axis2d: np.ndarray,
    density: str,
    sheet_aspect: float = 0.75,
    max_rows: int = 1,
) -> LayoutResult:
    """Single-axis one-dimensional string with minimal axial separation (§11.3).

    ``max_rows`` accepts ONLY 1.  §11.1 abolishes two-dimensional rearrangement: displacing parts
    off the assembly axis misrepresents the assembly relationship, which is the one thing an
    exploded patent figure exists to convey.  It must not silently degrade to a single row either —
    a caller that passes the argument and one that forgets it would then render two different
    figures and both would pass QA.
    """
    # ---------- S0 argument validation (never degrade silently) ----------
    if len(pieces) == 0:
        raise LayoutError("layout_exploded: 零件列表为空")
    if density not in DENSITY:
        raise LayoutError(
            "layout_exploded: 未知 density %r，可选 %s" % (density, sorted(DENSITY))
        )
    if int(max_rows) != 1:
        # Architect ruling §11.1: single-axis 1D string only.  Never degrade silently.
        raise LayoutError(
            "layout_exploded: max_rows=%r。本实现按 impl-contract §11.1 只提供单轴一维串，"
            "二维排布已被废止（会破坏装配关系）。请传 max_rows=1；版面不够时的修复动作是"
            "调 layout.axis_angle（推荐 %d）、改 density、或按 assembly.json:split_suggestions 拆图。"
            % (max_rows, AXIS_ANGLE_DEFAULT)
        )
    if not (float(sheet_aspect) > 0.0):
        raise LayoutError("layout_exploded: sheet_aspect 必须为正，收到 %r" % (sheet_aspect,))

    # ---------- S1 global scale reference and the relative quantisation operator ----------
    # Every comparison, sort key and tolerance is normalised by SCALE_REF.  This module receives
    # model units: an absolute ``round(v, 9)`` means 1e-14 of relative precision on 1e5-magnitude
    # coordinates (finer than the rounding noise, so tie decisions would flip between machines) and
    # over-merges on 1e-3-magnitude coordinates.
    pts_all = []
    for pc in pieces:
        if not pc.curves:
            raise LayoutError(
                "layout_exploded: piece %s 无可见曲线，无法排布。"
                "上游 HLR 可能静默丢了它的边；请在 plan 的 exclude 里排除，"
                "或换 view。" % pc.key
            )
        arr = _stack(pc.curves)
        if arr.ndim != 2 or arr.shape[1] != 2 or arr.shape[0] < 1:
            raise LayoutError("layout_exploded: piece %s 的 curves 形状非法" % pc.key)
        if not np.all(np.isfinite(arr)):
            # NaN/Inf poison min/max and make every comparison return False, so the overlap check
            # would "pass" spuriously.
            raise LayoutError("layout_exploded: piece %s 的曲线含 NaN/Inf" % pc.key)
        pts_all.append(arr)
    ALL = np.vstack(pts_all)
    span_x = float(ALL[:, 0].max() - ALL[:, 0].min())
    span_y = float(ALL[:, 1].max() - ALL[:, 1].min())
    SCALE_REF = float(np.hypot(span_x, span_y))
    if not (SCALE_REF > 0.0) or not np.isfinite(SCALE_REF):
        raise LayoutError("layout_exploded: 全体几何退化为一点，无法排布")
    EPS = EPS_REL * SCALE_REF
    TOL = OVERLAP_TOL_REL * SCALE_REF

    def Q(v: float) -> float:  # the one and only comparison / sort quantiser
        return round(float(v) / SCALE_REF, RANK_DEC)

    # ---------- S2 axis normalisation ----------
    a = np.asarray(axis2d, dtype=np.float64).reshape(2)
    na = float(np.hypot(a[0], a[1]))
    if not np.isfinite(na) or na < AXIS_MIN_NORM:
        raise LayoutError(
            "layout_exploded: 爆炸轴在图面上的投影模长 %.3e < %.3e，"
            "轴几乎平行于视线，沿轴排序无意义。修复：改 layout.view，或改 layout.explode_axis。"
            % (na, AXIS_MIN_NORM)
        )
    e = a / na
    # Sign normalisation: force e_y >= 0, and e_x >= 0 when e_y == 0.  Guarantees one physical axis
    # cannot flip because of an upstream sign convention.
    if round(float(e[1]), RANK_DEC) < 0.0 or (
        round(float(e[1]), RANK_DEC) == 0.0 and round(float(e[0]), RANK_DEC) < 0.0
    ):
        e = -e
    p = np.array([-e[1], e[0]], dtype=np.float64)  # sheet normal, always the +90 degree rotation

    # ---------- S3 true projected footprint, per piece ----------
    # Uses the already projected 2D polyline points, not the eight corners of the 3D world AABB:
    # the latter systematically overestimates the footprint of a skewed slender part (legacy
    # defect 1).
    foot = []  # same order as ``pieces``
    for i, pc in enumerate(pieces):
        P = pts_all[i]
        av = _dot2(P, e)
        rec = {
            "i": i,
            "key": pc.key,
            "name": pc.name,
            "xlo": float(P[:, 0].min()),
            "xhi": float(P[:, 0].max()),
            "ylo": float(P[:, 1].min()),
            "yhi": float(P[:, 1].max()),
            "alo": float(av.min()),
            "ahi": float(av.max()),
        }
        rec["w"] = rec["ahi"] - rec["alo"]  # footprint along the axis
        rec["ac"] = 0.5 * (rec["alo"] + rec["ahi"])
        foot.append(rec)
    degenerate = [
        r["key"]
        for r in foot
        if max(r["xhi"] - r["xlo"], r["yhi"] - r["ylo"]) < DEGEN_REL * SCALE_REF
    ]
    if degenerate:
        # Fail loudly instead of skipping: skipping desynchronises the parts list from the drawing,
        # and a zero-width slot glues the neighbours together.
        raise LayoutError(
            "layout_exploded: 退化零件 %s（图面外廓 < %.1e × 全图跨度）。"
            "这通常是基准面/草图线被当成 Part 载入。修复：在 plan 的 source.exclude 里排除它们。"
            % (", ".join(sorted(degenerate)), DEGEN_REL)
        )

    # ---------- S4 bodies: one granularity for separation, HLR and labelling ----------
    # The organising sentence (the only one this module needs remembered):
    #   **a body is the unit of separation, the unit of HLR, and the unit of labelling.**
    # The three must agree, otherwise symmetric groups (a bolt circle) are torn apart, per-piece HLR
    # necessarily draws occlusion wrong, and the postcondition's bbox check has no solution.
    # Rule: instances sharing a name cluster by AXIAL INTERVAL OVERLAP.  Same-named instances at
    # different stations (four identical screws top and bottom of a housing) must land in different
    # bodies, otherwise the union bbox straddles the housing and the displacement is absurd.
    names = sorted({r["name"] for r in foot})  # sorted — never iterate a set
    bodies = []
    for nm in names:
        inst = [r for r in foot if r["name"] == nm]
        inst.sort(key=lambda r: (Q(r["alo"]), Q(r["ahi"]), r["key"]))  # total order, unique last
        clusters, cur = [], [inst[0]]
        for r in inst[1:]:
            # Quantise both sides before comparing: cluster membership is a discrete cliff that
            # cascades through the whole figure, so never compare raw floats here.
            if Q(r["alo"]) <= Q(cur[-1]["ahi"] + EPS):
                cur.append(r)
            else:
                clusters.append(cur)
                cur = [r]
        clusters.append(cur)
        for ci, cl in enumerate(clusters):
            bodies.append(
                {
                    "key": "%s#c%d" % (nm, ci),
                    "members": [r["i"] for r in cl],
                    "xlo": min(r["xlo"] for r in cl),
                    "xhi": max(r["xhi"] for r in cl),
                    "ylo": min(r["ylo"] for r in cl),
                    "yhi": max(r["yhi"] for r in cl),
                    "alo": min(r["alo"] for r in cl),
                    "ahi": max(r["ahi"] for r in cl),
                }
            )
    for b in bodies:
        b["w"] = b["ahi"] - b["alo"]
        b["ac"] = 0.5 * (b["alo"] + b["ahi"])
    # Assembly order along the axis.  Sort by axial CENTRE, not by lower bound: sorting by lower
    # bound lets a housing that spans the whole length seize slot 0 and flings it and its own
    # internals to opposite ends of the sheet (legacy defect 2).  The trailing ``body["key"]`` is
    # the total-order tiebreak, which kills legacy defect 6 (equal lower bounds inheriting the XCAF
    # traversal order, so a re-export changed the drawing).
    bodies.sort(key=lambda b: (Q(b["ac"]), Q(b["alo"]), b["key"]))
    n = len(bodies)

    # ---------- S5 gap ----------
    # §5.2 frozen semantics: a fraction of the MEDIAN body footprint along the explode direction,
    # not of the total string length.  Using the total length reproduces the v1 failure: the longer
    # the string, the smaller each part's slot and the wider the gap.
    med_w = _lower_median([b["w"] for b in bodies])
    G = DENSITY[density] * med_w

    # ---------- S6 minimal axial displacement: a difference-constraint system ----------
    # Displacement is along ``e`` only and carries no normal component, so lateral displacement is
    # strictly zero (authority A executed literally; S8 asserts it machine-verifiably).
    def _sep_required(bi, bj) -> float:
        """Minimum displacement of ``bj`` along +e that makes the two AXIS-ALIGNED bboxes disjoint.

        The key insight: two shapes separated along a diagonal can still have intersecting
        axis-aligned bboxes, and the postcondition and qa both test axis-aligned bboxes.  Legacy
        projected the bboxes onto the axial scalar and did interval separation there, which is why
        a diagonal string always retained axis-aligned overlap (legacy defect 8).  Two AABBs are
        disjoint iff they are separated on x or on y, so both branches are solved and the cheaper
        one wins.
        """
        cands = []
        ex, ey = float(e[0]), float(e[1])
        if abs(ex) > AXIS_COMP_MIN:  # skip a branch whose component is too small: its ``t`` is
            if ex > 0.0:  # astronomically large anyway so ``min`` would not pick it, while the
                cands.append((bi["xhi"] - bj["xlo"]) / ex)  # division amplifies noise by 1/|ex|
            else:
                cands.append((bj["xhi"] - bi["xlo"]) / (-ex))
        if abs(ey) > AXIS_COMP_MIN:
            if ey > 0.0:
                cands.append((bi["yhi"] - bj["ylo"]) / ey)
            else:
                cands.append((bj["yhi"] - bi["ylo"]) / (-ey))
        if not cands:  # unreachable while |e| = 1; kept as a trap for implementation errors
            raise LayoutError(
                "layout_exploded: 轴的两个分量都小于 %.1e（内部错误）" % AXIS_COMP_MIN
            )
        t_req = min(cands)
        if Q(t_req) <= 0.0:
            # KEY: already disjoint at zero displacement => claim no gap at all.  Otherwise a bolt
            # circle at one station gets prised apart one screw at a time by G and its symmetry
            # collapses on the spot.
            return 0.0
        return t_req + G

    # Fixed-order greedy = longest path on a DAG; exactly optimal for this assembly order.
    # The reduction is unrolled over the fixed indices 0..i-1 — never ``np.max`` over an unordered
    # collection, which would make the reduction tree machine-dependent.
    t = [0.0] * n
    for i in range(1, n):
        best = 0.0
        for j in range(i):
            v = t[j] + _sep_required(bodies[j], bodies[i])
            if v > best:
                best = v
        t[i] = best
    # Monotonicity: displacing further than ``_sep_required`` keeps the pair disjoint (the signs of
    # e's components are fixed, so j recedes from i in one direction only), hence taking the max for
    # t[i] is safe and cannot break an already satisfied constraint pair.

    # ---------- S7 placement: rigid translation of the whole body ----------
    for bi, b in enumerate(bodies):
        off = np.round(t[bi] * e, RANK_DEC + 3)  # coordinates rounded to 12 decimals before
        for m in b["members"]:  # writing; normalized_digest keeps only 6 => 6 decimals of headroom
            pieces[m].offset = off.copy()
        b["off"] = off

    # ---------- S8 postconditions (raise, never degrade silently) ----------
    # P0 — zero lateral displacement (authority A's machine-verifiable form).  e.p is exactly zero
    # in floating point, so this assertion is free.
    for b in bodies:
        for m in b["members"]:
            if Q(float(pieces[m].offset[0] * p[0] + pieces[m].offset[1] * p[1])) != 0.0:
                raise LayoutError(
                    "layout_exploded: 不变量 P0 破坏——piece %s 有横向位移分量" % pieces[m].key
                )
    # P1 — placed body bboxes are pairwise disjoint (touching is not overlap; tolerance TOL)
    bad = []
    for i in range(n):
        for j in range(i + 1, n):
            bi, bj = bodies[i], bodies[j]
            ix = Q(bi["xlo"] + bi["off"][0]) < Q(bj["xhi"] + bj["off"][0] - TOL) and Q(
                bj["xlo"] + bj["off"][0]
            ) < Q(bi["xhi"] + bi["off"][0] - TOL)
            iy = Q(bi["ylo"] + bi["off"][1]) < Q(bj["yhi"] + bj["off"][1] - TOL) and Q(
                bj["ylo"] + bj["off"][1]
            ) < Q(bi["yhi"] + bi["off"][1] - TOL)
            if ix and iy:
                bad.append((bi["key"], bj["key"]))
    if bad:
        raise LayoutError(
            "layout_exploded: body 包围盒重叠 %d 对，首例 %s / %s。"
            "差分约束求解未收敛——这是实现错误，不是输入错误。" % (len(bad), bad[0][0], bad[0][1])
        )
    # Note that P1 holds at BODY granularity only.  Members inside one body (a bolt circle of 20
    # screws) may overlap each other — forcing them apart would smear the circle into a straight
    # line and destroy its symmetry.  qa.py must read the body boxes from the sidecar to verify
    # part_bbox_overlap, and must not try to guess part boundaries from the DXF curves (§11.10).

    # ---------- S9 diagnostics (layout judges no millimetre gate; it only supplies the inputs) ----
    lo = np.array(
        [
            min(f["xlo"] + pieces[f["i"]].offset[0] for f in foot),
            min(f["ylo"] + pieces[f["i"]].offset[1] for f in foot),
        ]
    )
    hi = np.array(
        [
            max(f["xhi"] + pieces[f["i"]].offset[0] for f in foot),
            max(f["yhi"] + pieces[f["i"]].offset[1] for f in foot),
        ]
    )
    Gw, Gh = float(hi[0] - lo[0]), float(hi[1] - lo[1])
    alpha = float(sheet_aspect)
    # Fitting scale inside the normalised frame (alpha x 1.0) and the geometry's share of it
    s_rel = min(alpha / Gw, 1.0 / Gh) if Gw > 0.0 and Gh > 0.0 else 0.0
    fill_usable = (Gw * s_rel) * (Gh * s_rel) / alpha  # in (0, 1]; equals min(gamma/alpha, alpha/gamma)
    # String length and width in the (e, p) frame, for the closed-form optimal axis angle.  The
    # span along p is unaffected by axial displacement, so it comes straight from the raw projected
    # points (the offsets have an e component only, so no point's p coordinate changes).
    L_axis = max(b["ahi"] + t[i] for i, b in enumerate(bodies)) - min(
        b["alo"] + t[i] for i, b in enumerate(bodies)
    )
    pv = _dot2(ALL, p)
    V_axis = float(pv.max() - pv.min())
    axis_angle_opt = _closed_form_best_angle(L_axis, V_axis, alpha, EPS)
    # The closed form models the string as a rectangle; refine it against the real placed points.
    _placed_pts = np.vstack([c for piece in pieces for c in piece.placed()])
    _current_deg = round(float(np.degrees(np.arctan2(e[1], e[0]))), RANK_DEC)
    axis_angle_opt = _refine_axis_angle(_placed_pts, _current_deg, axis_angle_opt, alpha)

    diagnostics = {
        "strategy": "axial-1d-minimal-separation",
        "gap": G,
        "median_body_extent": med_w,
        "overlaps": 0,
        "bodies": n,
        "rows": 1,
        "scale_ref": SCALE_REF,
        "axis_angle_deg": round(float(np.degrees(np.arctan2(e[1], e[0]))), RANK_DEC),
        "axis_angle_opt": axis_angle_opt,
        "fill_usable": fill_usable,
        "body_boxes": [
            {
                "key": b["key"],
                "lo": [b["xlo"] + b["off"][0], b["ylo"] + b["off"][1]],
                "hi": [b["xhi"] + b["off"][0], b["yhi"] + b["off"][1]],
                "members": [pieces[m].key for m in b["members"]],
            }
            for b in bodies
        ],
        "extent_by_key": {
            pieces[f["i"]].key: max(f["xhi"] - f["xlo"], f["yhi"] - f["ylo"]) for f in foot
        },
        # Filled in by render_patent_figure.py, which is the only layer that knows which pieces
        # carry a numeral (§11.6.2): slot_labelled = min over LABELLED pieces of extent_by_key.
        # It, not LayoutResult.slot, drives the text height.
        "slot_labelled": None,
        "dropped": [],
    }

    # ---------- S10 slot: clamped across both gauges ----------
    ext = [max(f["xhi"] - f["xlo"], f["yhi"] - f["ylo"]) for f in foot]
    diag = float(np.hypot(Gw, Gh))
    slot_contract = min(ext)  # §5.2's literal gauge
    slot_qa_cap = SLOT_QA_CAP_K * diag / max(len(pieces), 1)  # the failure baseline's gauge
    return LayoutResult(
        pieces=pieces,
        lo=lo,
        hi=hi,
        slot=min(slot_contract, slot_qa_cap),
        rows=1,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------------------------
# §11.4 layout_assembly
# ---------------------------------------------------------------------------------------------


def layout_assembly(pieces: list[Piece]) -> LayoutResult:
    """kind='assembly': parts keep their projected positions, all offsets zero.

    NO overlap check — in an assembly view parts occlude one another by construction, and the
    occlusion is handled by the global HLR in ``scene_curves``.  §5.2's no-overlap postcondition
    constrains ``layout_exploded`` only.
    """
    if len(pieces) == 0:
        raise LayoutError("layout_assembly: 零件列表为空")
    pts_all = []
    for pc in pieces:
        if not pc.curves:
            raise LayoutError("layout_assembly: piece %s 无可见曲线" % pc.key)
        arr = _stack(pc.curves)
        if not np.all(np.isfinite(arr)):
            raise LayoutError("layout_assembly: piece %s 的曲线含 NaN/Inf" % pc.key)
        pts_all.append(arr)
        pc.offset = np.zeros(2)

    lo = np.array(
        [
            min(float(a[:, 0].min()) for a in pts_all),
            min(float(a[:, 1].min()) for a in pts_all),
        ]
    )
    hi = np.array(
        [
            max(float(a[:, 0].max()) for a in pts_all),
            max(float(a[:, 1].max()) for a in pts_all),
        ]
    )
    SCALE_REF = float(np.hypot(hi[0] - lo[0], hi[1] - lo[1]))
    if not (SCALE_REF > 0.0):
        raise LayoutError("layout_assembly: 全体几何退化为一点")

    ext, boxes = [], []
    for i, a in enumerate(pts_all):
        w = float(a[:, 0].max() - a[:, 0].min())
        hgt = float(a[:, 1].max() - a[:, 1].min())
        if max(w, hgt) < DEGEN_REL * SCALE_REF:
            raise LayoutError(
                "layout_assembly: 退化零件 %s；请在 plan 的 source.exclude 里排除" % pieces[i].key
            )
        ext.append(max(w, hgt))
        boxes.append(
            {
                "key": pieces[i].key,
                "lo": [float(a[:, 0].min()), float(a[:, 1].min())],
                "hi": [float(a[:, 0].max()), float(a[:, 1].max())],
                "members": [pieces[i].key],
            }
        )

    diag = float(np.hypot(hi[0] - lo[0], hi[1] - lo[1]))
    return LayoutResult(
        pieces=pieces,
        lo=lo,
        hi=hi,
        slot=min(min(ext), SLOT_QA_CAP_K * diag / max(len(pieces), 1)),
        rows=1,
        diagnostics={
            "strategy": "in-place",
            "gap": 0.0,
            "overlaps": 0,
            "scale_ref": SCALE_REF,
            "bodies": len(pieces),
            "rows": 1,
            "body_boxes": boxes,
            "extent_by_key": {pieces[i].key: ext[i] for i in range(len(pieces))},
            "fill_usable": None,
            "axis_angle_opt": None,
            # Filled in by render_patent_figure.py, exactly as in layout_exploded (§11.6.2).
            "slot_labelled": None,
            "dropped": [],
        },
    )


# ---------------------------------------------------------------------------------------------
# §11.5 fit_to_frame
# ---------------------------------------------------------------------------------------------


def fit_to_frame(
    result: LayoutResult,
    frame_w: float = FRAME_W,
    frame_h: float = FRAME_H,
    margin: float = 0.0,
) -> float:
    """Return the uniform scale factor that fits the geometry bbox into (frame_w, frame_h) inset by
    ``margin``.  NEVER mutates the pieces — the caller applies the scale.

    Architect ruling D5 (§11.5) abolishes the ``s_target`` branch of §5.2 and changes the default
    ``margin`` from 0.06 to 0.0:

    1. ``s_target`` pinned the geometry area to ``TARGET_OCCUPANCY`` of the usable area, but ruling
       D1 proves ``geometry_occupancy`` is scale-invariant, so ``s_target`` improves no QA gate at
       all and merely throws away ``sqrt(0.62) = 21%`` of linear scale — and 21% of text height with
       it.  Measured: at N=20 ``0.45*slot`` falls from 3.83 to 3.02, straight through
       ``TEXT_FLOOR_MM = 3.5``.
    2. ``margin=0.06`` is a second inset: 180x250 is already "A4 minus a 15 mm margin", and the
       label gutter and caption band are subtracted explicitly by the caller per §11.6.  Stacking
       both leaves 77.4% of the area and the geometry can never reach the edge of the paper.
    3. What ``s_target`` was for (headroom for the labels) is already carried, and carried more
       accurately, by ``label_margin_mm`` / ``caption_band_mm`` — sized from the ACTUAL text height
       rather than from a guessed 0.62.

    Caller contract: ``frame_w``/``frame_h`` must already be the usable area after the label gutter
    and caption band have been deducted, and ``margin`` must be 0.0.
    """
    Gw = float(result.hi[0] - result.lo[0])
    Gh = float(result.hi[1] - result.lo[1])
    if not (Gw > 0.0 and Gh > 0.0) or not (math.isfinite(Gw) and math.isfinite(Gh)):
        raise LayoutError("fit_to_frame: 几何包围盒退化（%.6g × %.6g）" % (Gw, Gh))
    aw = frame_w * (1.0 - 2.0 * margin)
    ah = frame_h * (1.0 - 2.0 * margin)
    if aw <= 0.0 or ah <= 0.0:
        raise LayoutError("fit_to_frame: margin=%.3f 把可用区吃光了" % margin)
    return round(min(aw / Gw, ah / Gh), RANK_DEC)


# ---------------------------------------------------------------------------------------------
# §11.6.3 usable-area and text-size helpers.
# Defined here because §11.2.2 places their constants in this module, and because
# render_patent_figure.py (§11.6) and qa.py (§11.10 revision 2) must use IDENTICAL definitions.
# Import them; do not restate the formulas.
# ---------------------------------------------------------------------------------------------


def label_margin_mm(h: float) -> float:
    """Width of one side gutter reserved for reference numerals, in mm, for text height ``h``."""
    return min(LABEL_MARGIN_K * h, LABEL_MARGIN_CAP_MM)


def caption_band_mm(h: float) -> float:
    """Height of the caption band, in mm, for text height ``h``."""
    return min(CAPTION_K * h, CAPTION_CAP_MM)


def snap_text_height(raw_mm: float) -> float | None:
    """Snap to the largest GB/T 14691 size not exceeding ``raw_mm``; None below ``TEXT_FLOOR_MM``."""
    for v in TEXT_SERIES:  # descending (7.0, 5.0, 3.5)
        if v <= raw_mm + 10 ** (-RANK_DEC):
            return v
    return None
