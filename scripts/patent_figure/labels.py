"""2D leader and numeral placement on the sheet (pure — no OCC, no ezdxf).

Coordinate system: **millimetre sheet coordinates**. `LabelRequest.lo/hi`, `obstacles` and
`sheet_lo/sheet_hi` have already been multiplied by the fit scale `s` and translated into the
frame by the renderer (impl-contract §11.0 / §11.7). `sheet_lo/sheet_hi` is the whole FRAME
(180x250), not the geometry usable area: numerals may fall into the label margin, geometry may not.

Hard postconditions (impl-contract §5.3 / §11.8 P2..P5b):

  P2  no two placed numeral boxes overlap
  P3  no numeral box intersects a geometry obstacle
  P4  every numeral box lies inside sheet_lo/sheet_hi
  P5  no two leaders cross
  P5b no leader passes through any committed numeral box

These are *not* best-effort. When they cannot all be met the function raises `LabelError` carrying
`unplaced` (the numerals) and `tried` (candidate slots per numeral); the caller turns that into
`E_LABELS_UNPLACEABLE` and asks the model to split the figure. `place_labels` never falls back to
placing a label anyway — that unsatisfiability *is* the mechanism that forces a split.

Every constant below is sourced from impl-contract §11.2.3; no bare numbers in function bodies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

# --------------------------------------------------------------------------------------------
# Constants — impl-contract §11.2.3. Each name appears exactly once (§7 rule 7).
# --------------------------------------------------------------------------------------------
RANK_DEC = 9
"""§7 rule 4: quantise before every comparison."""

EPS_SHEET_REL = 1e-9
"""Comparison tolerance = 1e-9 * frame diagonal (relative, same reasoning as layout's EPS_REL)."""

N_DIRS = 12
"""30 degree grid. GB/T 4457.2 admits leader angles that are multiples of 30/45/60/90 degrees;
30 is the smallest common step of that set."""

PHASE_DEG = 15.0
"""Phase offset makes the candidate angles 15/45/.../345 — none of them horizontal, vertical or
parallel to an outline (GB/T 4457.2). Side benefit: both components of every direction are
non-zero, so `_ray_exit_aabb` needs no divide-by-zero branch."""

DIR_ROUND_DEC = 12
"""The 12 direction cos/sin are rounded to 12 decimals at import: libm trig error is ~1 ULP
(relative ~1e-16), fully absorbed at 1e-12, so every platform gets the same constants."""

CHAR_W = 0.71
"""GB/T 14691 type B character width = h/sqrt(2) = 0.7071, rounded UP (better a numeral box
computed too wide than too narrow)."""

PAD_X = 0.30
"""x h. GB character spacing a = 0.2h. Horizontal clear distance between two numeral boxes is
2*PAD_X*h = 0.6h = 3 character spacings, so an adjacent "1" and "2" cannot be read as "12"."""

PAD_Y = 0.20
"""x h. Line height = h + 2*PAD_Y*h = 1.4h = GB/T 14691 type B minimum line spacing."""

LAND_K = 2.0
"""x h. GB/T 4457.2: the landing line is not shorter than the text it carries. A two digit
numeral is 2*0.71h = 1.42h wide; 2.0h makes the landing read as a line, not a hook."""

RUNOUT_K = 0.50
"""x h. Gap between the text and the landing end = 2.5 character spacings."""

RING_BASE_K = 1.0
"""x h. Distance of ring 0 from the part bbox: clear at least one text height so that the start
of the leader is visible."""

RING_GROWTH = 1.8
"""Geometric ratio of the ring radii."""

RING_COUNT = 4
"""Four rings cover 1.0 / 1.8 / 3.24 / 5.83 h. Farther out the leader is longer than the label
footprint and reads as a stray line."""

GRID_CELL_K = 4.0
"""x h. Obstacle index cell size. The largest numeral box edge is about
2*CHAR_W*h + 2*PAD_X*h ~= 2.0h, so one box spans at most 2x2 cells."""

CLEAR_K = 0.50
"""x h. Scoring-only inflation: a numeral box that still touches geometry after growing 0.5h is
scored as "grazing"."""

CROWD_R_K = 3.0
"""x h. Crowding radius. 3h is about the diagonal of two numeral boxes; beyond that two numerals
no longer read as a group."""

W_LEN = 1.0
"""Scoring baseline: 1 point per h of leader length. The three weights below are explicit
exchange rates expressed in "how many h of leader is this worth", not fitted values."""

W_DIR = 2.0
"""A 60 degree deviation from the preferred direction (1 - cos60 = 0.5) costs 1.0 point = 1h."""

W_CLR = 3.0
"""Grazing geometry costs 3 points: better to detour three text heights than to touch a line."""

W_CROWD = 2.0
"""2 points for every already-placed numeral box inside CROWD_R_K*h."""

MAX_REPAIR = 3
"""Bounded backtracking rounds. Every round bans at least one already-placed candidate and the
ban set grows monotonically, so termination is guaranteed; still unsolved after 3 rounds means the
figure is overloaded and must be split rather than squeezed."""

TEXT_RATIO = 0.45
"""§5.3 freezes `text_height_for(slot, ratio=0.45)`; §11.2.2 lists the same value under the name
TEXT_RATIO. The function lives in this module, so the constant is defined here and `layout.py` /
the renderer should import it from here rather than redefining it (§7 rule 7)."""


class LabelError(RuntimeError):
    """Carries `unplaced` (list of numerals) and `tried` (candidate slots per numeral), §5.3."""

    def __init__(self, msg: str, unplaced: List[int], tried: int) -> None:
        super().__init__(msg)
        self.unplaced = unplaced
        self.tried = tried


@dataclass
class LabelRequest:
    key: str
    numeral: int
    lo: np.ndarray
    hi: np.ndarray
    anchor_hint: Optional[np.ndarray] = None
    #: (N, 2) sampled points of THIS part's own placed curves, in sheet millimetres. When given,
    #: every candidate direction anchors on a real outline point instead of on the bounding box.
    #: Without it the anchor falls back to the AABB, which for a ring, an L or any concave part
    #: sits in empty space — measured at up to 12% of the sheet diagonal away from the part.
    outline: Optional[np.ndarray] = None


@dataclass
class LabelPlacement:
    numeral: int
    key: str
    text_pos: Tuple[float, float]
    text_align: str                       # "left" | "right"
    leader: List[Tuple[float, float]]     # [anchor_on_part, elbow, landing_end]; 3 points
    score: float


# 12 candidate directions, computed once at import and rounded to DIR_ROUND_DEC (§11.7).
_DIRS = tuple(
    (round(math.cos(math.radians(PHASE_DEG + 360.0 * k / N_DIRS)), DIR_ROUND_DEC),
     round(math.sin(math.radians(PHASE_DEG + 360.0 * k / N_DIRS)), DIR_ROUND_DEC))
    for k in range(N_DIRS))


# ------------------------- geometric predicates (§11.7, verbatim) ---------------------------
def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _seg_cross(p0, p1, q0, q1, eps) -> bool:
    """Proper crossing. Shared endpoints and collinear overlap count as NOT crossing (a leader
    starts flush against the geometry). Four orientation predicates, rounded before the sign test
    — this is the bedrock of the zero-crossing invariant and must not be rewritten."""
    d1 = round(_cross(p1[0] - p0[0], p1[1] - p0[1], q0[0] - p0[0], q0[1] - p0[1]), RANK_DEC)
    d2 = round(_cross(p1[0] - p0[0], p1[1] - p0[1], q1[0] - p0[0], q1[1] - p0[1]), RANK_DEC)
    d3 = round(_cross(q1[0] - q0[0], q1[1] - q0[1], p0[0] - q0[0], p0[1] - q0[1]), RANK_DEC)
    d4 = round(_cross(q1[0] - q0[0], q1[1] - q0[1], p1[0] - q0[0], p1[1] - q0[1]), RANK_DEC)
    return ((d1 > 0.0) != (d2 > 0.0)) and ((d3 > 0.0) != (d4 > 0.0))


def _box_overlap(alo, ahi, blo, bhi, eps) -> bool:
    return (round(alo[0], RANK_DEC) < round(bhi[0] - eps, RANK_DEC) and
            round(blo[0], RANK_DEC) < round(ahi[0] - eps, RANK_DEC) and
            round(alo[1], RANK_DEC) < round(bhi[1] - eps, RANK_DEC) and
            round(blo[1], RANK_DEC) < round(ahi[1] - eps, RANK_DEC))


def _box_inside(blo, bhi, slo, shi, eps) -> bool:
    return (round(blo[0], RANK_DEC) >= round(slo[0] - eps, RANK_DEC) and
            round(blo[1], RANK_DEC) >= round(slo[1] - eps, RANK_DEC) and
            round(bhi[0], RANK_DEC) <= round(shi[0] + eps, RANK_DEC) and
            round(bhi[1], RANK_DEC) <= round(shi[1] + eps, RANK_DEC))


def _seg_hits_box(p0, p1, blo, bhi, eps) -> bool:
    """Segment vs AABB (an endpoint inside the box counts). Endpoint test first, then the four
    edges through `_seg_cross`."""
    for pt in (p0, p1):
        if (blo[0] - eps <= pt[0] <= bhi[0] + eps and blo[1] - eps <= pt[1] <= bhi[1] + eps):
            return True
    c = ((blo[0], blo[1]), (bhi[0], blo[1]), (bhi[0], bhi[1]), (blo[0], bhi[1]))
    for k in range(4):
        if _seg_cross(p0, p1, c[k], c[(k + 1) % 4], eps):
            return True
    return False


def _outline_anchor(outline: np.ndarray, d):
    """The outline point that reaches furthest along ``d`` — where a draftsman would attach a
    leader arriving from that direction, and always ON the part.

    Same recipe as the contract's §11.7.2 anchor_hint, only parameterised by direction instead
    of being computed once: rank on ``(round(-p·d), round(x), round(y))`` and take the smallest.
    Being an extreme point along ``d`` it lies on the convex hull, so it is on the silhouette for
    every part shape, and it is the farthest point along ``d`` — so ``anchor + d * dist`` is
    guaranteed to clear the whole part.
    """
    proj = np.round(-(outline[:, 0] * d[0] + outline[:, 1] * d[1]), RANK_DEC)
    best = proj.min()
    tie = outline[proj == best]
    if tie.shape[0] > 1:                       # deterministic tie-break, lexicographic on (x, y)
        order = np.lexsort((np.round(tie[:, 1], RANK_DEC), np.round(tie[:, 0], RANK_DEC)))
        tie = tie[order]
    return (float(tie[0, 0]), float(tie[0, 1]))


def _seg_beyond_box(a, b, lo, hi, eps):
    """The portion of segment a->b that lies beyond the box, as a new start point.

    Used to clip a leader's root before testing it against OTHER parts. The anchor sits on its
    own part's silhouette, so the first stretch of the leader is legitimately inside that part's
    own envelope; testing that stretch against neighbours punishes a leader for where it is
    required to start. In an assembly view part boxes overlap heavily, so without this clip a
    nested part becomes unlabellable — measured on the synthetic fixture, numeral 3 lost all 48
    candidates. Slab method; if a->b never leaves the box, return b (nothing left to test).
    """
    ax, ay = float(a[0]), float(a[1])
    dx, dy = float(b[0]) - ax, float(b[1]) - ay
    t_exit = 0.0
    for p0, d0, l0, h0 in ((ax, dx, lo[0], hi[0]), (ay, dy, lo[1], hi[1])):
        if abs(d0) <= eps:
            continue
        t1, t2 = (l0 - p0) / d0, (h0 - p0) / d0
        t_exit = max(t_exit, min(1.0, max(t1, t2)))
    t_exit = min(1.0, max(0.0, t_exit))
    return (ax + dx * t_exit, ay + dy * t_exit)


def _ray_exit_aabb(c, d, lo, hi):
    """Exit point of the ray c + t*d (t>0) from an AABB. Both components of d are non-zero
    (guaranteed by PHASE_DEG = 15)."""
    tx = (hi[0] - c[0]) / d[0] if d[0] > 0.0 else (lo[0] - c[0]) / d[0]
    ty = (hi[1] - c[1]) / d[1] if d[1] > 0.0 else (lo[1] - c[1]) / d[1]
    t = min(tx, ty)
    return (c[0] + d[0] * t, c[1] + d[1] * t)


# ============================================================================================
def place_labels(requests: List[LabelRequest], *, obstacles: List[np.ndarray],
                 text_height: float, sheet_lo, sheet_hi) -> List[LabelPlacement]:
    """Place a numeral + leader for every request. impl-contract §11.7, implemented literally.

    Deterministic: no randomness, every choice is an index scan over a finite ordered candidate
    table, every sort key is a total order ending in a globally unique value.

    `obstacles` carries only the placed geometry polylines; the leaders and numeral boxes already
    committed during this call are accumulated internally (§5.3).
    """
    h = float(text_height)
    if not (h > 0.0):
        raise LabelError("place_labels: text_height 必须为正，收到 %r" % (text_height,), [], 0)
    if not requests:
        return []
    EPS = EPS_SHEET_REL * float(np.hypot(float(sheet_hi[0]) - float(sheet_lo[0]),
                                         float(sheet_hi[1]) - float(sheet_lo[1])))
    slo = (float(sheet_lo[0]), float(sheet_lo[1]))
    shi = (float(sheet_hi[0]), float(sheet_hi[1]))

    # ---------- L0 obstacle index: uniform grid ----------
    # A correct answer needs no index, but a 90-part global HLR yields ~1e5 segments and
    # 20 labels x 48 candidates x 1e5 = 1e8 segment tests takes a figure from sub-second to
    # minutes. The index is mandatory, not an optimisation.
    SEG: List[Tuple[float, float, float, float]] = []   # pushed in obstacles index order (a list)
    for arr in obstacles:
        a = np.asarray(arr, dtype=np.float64)
        if a.ndim != 2 or a.shape[0] < 2:
            continue
        for t_i in range(a.shape[0] - 1):
            SEG.append((float(a[t_i, 0]), float(a[t_i, 1]),
                        float(a[t_i + 1, 0]), float(a[t_i + 1, 1])))
    CELL = GRID_CELL_K * h
    GRID: Dict[Tuple[int, int], List[int]] = {}         # point queries only, NEVER iterated
    for si, (x0, y0, x1, y1) in enumerate(SEG):
        for gx in range(int(math.floor(min(x0, x1) / CELL)),
                        int(math.floor(max(x0, x1) / CELL)) + 1):
            for gy in range(int(math.floor(min(y0, y1) / CELL)),
                            int(math.floor(max(y0, y1) / CELL)) + 1):
                GRID.setdefault((gx, gy), []).append(si)

    def _box_hits_geometry(blo, bhi) -> bool:
        for gx in range(int(math.floor(blo[0] / CELL)), int(math.floor(bhi[0] / CELL)) + 1):
            for gy in range(int(math.floor(blo[1] / CELL)), int(math.floor(bhi[1] / CELL)) + 1):
                for si in GRID.get((gx, gy), ()):       # list, index order; existence query
                    x0, y0, x1, y1 = SEG[si]
                    if _seg_hits_box((x0, y0), (x1, y1), blo, bhi, EPS):
                        return True
        return False

    # ---------- L1 preferred direction ----------
    # Centroid via a fixed-order Python sum, not np.mean (pairwise reduction blocking varies).
    cx = sum(round(0.5 * (float(r.lo[0]) + float(r.hi[0])), RANK_DEC)
             for r in requests) / len(requests)
    cy = sum(round(0.5 * (float(r.lo[1]) + float(r.hi[1])), RANK_DEC)
             for r in requests) / len(requests)

    def _pref_dir(r: LabelRequest):
        """Preferred direction: away from the drawing centroid (outward). When the caller supplied
        an anchor_hint, use its direction from the part centre instead — that makes anchor_hint an
        actual input to the decision rather than decoration."""
        c = (0.5 * (float(r.lo[0]) + float(r.hi[0])), 0.5 * (float(r.lo[1]) + float(r.hi[1])))
        if r.anchor_hint is not None:
            vx, vy = float(r.anchor_hint[0]) - c[0], float(r.anchor_hint[1]) - c[1]
        else:
            vx, vy = c[0] - cx, c[1] - cy
        n = math.hypot(vx, vy)
        if n <= EPS:                                    # part sits exactly on the centroid: +x
            return (1.0, 0.0), c
        return (vx / n, vy / n), c

    # ---------- L2 candidate generation ----------
    # Candidate order = (directions by ascending deviation from the preferred one, rings inside
    # out); the candidate index is the final tie-break key.
    def _candidates(r: LabelRequest) -> List[Dict[str, Any]]:
        (px, py), c = _pref_dir(r)
        lo = (float(r.lo[0]), float(r.lo[1]))
        hi = (float(r.hi[0]), float(r.hi[1]))
        # Direction sort key: (-cos(delta) ascending = deviation ascending, direction index).
        order_d = sorted(range(N_DIRS),
                         key=lambda k: (round(-(px * _DIRS[k][0] + py * _DIRS[k][1]), RANK_DEC), k))
        # With an outline every direction gets a real silhouette anchor. anchor_hint then only
        # still matters for the preferred direction above; keeping it as the snap_dir anchor as
        # well is harmless because _outline_anchor reproduces it exactly for that direction.
        snap_dir = order_d[0]
        outline = r.outline if (r.outline is not None and len(r.outline)) else None
        ndig = len(str(int(r.numeral)))
        tw = CHAR_W * h * ndig
        out: List[Dict[str, Any]] = []
        for rank, k in enumerate(order_d):
            d = _DIRS[k]
            base = _ray_exit_aabb(c, d, lo, hi)      # elbow/ring reference, see below
            if outline is not None:
                anchor = _outline_anchor(outline, d)
            elif r.anchor_hint is not None and k == snap_dir:
                anchor = (float(r.anchor_hint[0]), float(r.anchor_hint[1]))
            else:
                anchor = base
            dth = math.acos(max(-1.0, min(1.0, px * d[0] + py * d[1])))   # angle vs preferred dir
            for j in range(RING_COUNT):
                dist = RING_BASE_K * (RING_GROWTH ** j) * h
                # Elbow stays measured from the AABB ray-exit, NOT from the anchor. Anchoring it
                # to the silhouette instead was tried and reverted: it moves the numeral box
                # laterally to wherever the part happens to reach furthest along d, and in a
                # dense assembly view that lands the box on other geometry — numeral 3 of the
                # synthetic assembly lost all 48 candidates, 24 of them to box_on_geom. Where
                # the leader ATTACHES is the anchor's job; where the numeral SITS is the
                # elbow's, and the two want different reference points.
                elbow = (base[0] + d[0] * dist, base[1] + d[1] * dist)
                sgn = 1.0 if round(d[0], RANK_DEC) > 0.0 else -1.0    # PER CANDIDATE, not global
                land = (elbow[0] + sgn * LAND_K * h, elbow[1])        # horizontal landing (GB)
                tx = land[0] + sgn * RUNOUT_K * h                     # text sits OUTSIDE the line
                x0 = tx if sgn > 0.0 else tx - tw
                blo = (x0 - PAD_X * h, land[1] - 0.5 * h - PAD_Y * h)
                bhi = (x0 + tw + PAD_X * h, land[1] + 0.5 * h + PAD_Y * h)
                out.append({
                    "idx": rank * RING_COUNT + j,       # generation index = tie-break key
                    "req": r, "dir": k, "ring": j, "dth": dth,
                    "anchor": anchor, "elbow": elbow, "land": land,
                    # Leader root clipped to outside its own part — see _seg_beyond_box.
                    "root_out": _seg_beyond_box(anchor, elbow, lo, hi, EPS),
                    "text_pos": (tx, land[1]),
                    "align": "left" if sgn > 0.0 else "right",
                    "blo": blo, "bhi": bhi,
                    "llen": math.hypot(elbow[0] - anchor[0], elbow[1] - anchor[1]) + LAND_K * h,
                })
        return out

    CAND = [_candidates(r) for r in requests]           # 12 * 4 = 48 candidates per request
    TRIED = N_DIRS * RING_COUNT

    # ---------- L3 static hard vetoes (independent of placement order) ----------
    def _static_ok(cd: Dict[str, Any], i: int) -> bool:
        if not _box_inside(cd["blo"], cd["bhi"], slo, shi, EPS):
            return False                                # outside the frame
        if _box_hits_geometry(cd["blo"], cd["bhi"]):
            return False                                # numeral box on top of geometry
        for j, rq in enumerate(requests):
            if j == i:
                continue
            rlo = (float(rq.lo[0]), float(rq.lo[1]))
            rhi = (float(rq.hi[0]), float(rq.hi[1]))
            if _box_overlap(cd["blo"], cd["bhi"], rlo, rhi, EPS):
                return False                            # numeral box inside another part's box
            # GB: a leader must not run through another labelled part
            if _seg_hits_box(cd["root_out"], cd["elbow"], rlo, rhi, EPS):
                return False
        return True

    STATIC = [[cd for cd in CAND[i] if _static_ok(cd, i)] for i in range(len(requests))]

    # ---------- L4 dynamic hard vetoes (relation to what is already placed) ----------
    def _dyn_ok(cd: Dict[str, Any], placed: List[Dict[str, Any]]) -> bool:
        for q in placed:
            if _box_overlap(cd["blo"], cd["bhi"], q["blo"], q["bhi"], EPS):
                return False                            # numeral boxes never overlap
            for A, B in ((cd["anchor"], cd["elbow"]), (cd["elbow"], cd["land"])):
                for C, D in ((q["anchor"], q["elbow"]), (q["elbow"], q["land"])):
                    if _seg_cross(A, B, C, D, EPS):
                        return False                    # leaders never cross
                # §5.3: obstacles grow with the greedy loop — committed numeral boxes are
                # obstacles too. The new leader must not run through an old numeral box:
                if _seg_hits_box(A, B, q["blo"], q["bhi"], EPS):
                    return False
            for C, D in ((q["anchor"], q["elbow"]), (q["elbow"], q["land"])):
                # ... and an old leader must not run through the new numeral box:
                if _seg_hits_box(C, D, cd["blo"], cd["bhi"], EPS):
                    return False
        return True

    # ---------- L5 scoring (every term an explicit "worth this many h of leader" rate) ----------
    def _score(cd: Dict[str, Any], placed: List[Dict[str, Any]]) -> float:
        s = W_LEN * (cd["llen"] / h)
        s += W_DIR * (1.0 - math.cos(cd["dth"]))
        infl_lo = (cd["blo"][0] - CLEAR_K * h, cd["blo"][1] - CLEAR_K * h)
        infl_hi = (cd["bhi"][0] + CLEAR_K * h, cd["bhi"][1] + CLEAR_K * h)
        if _box_hits_geometry(infl_lo, infl_hi):
            s += W_CLR                                  # grazing
        bx = 0.5 * (cd["blo"][0] + cd["bhi"][0])
        by = 0.5 * (cd["blo"][1] + cd["bhi"][1])
        for q in placed:
            qx = 0.5 * (q["blo"][0] + q["bhi"][0])
            qy = 0.5 * (q["blo"][1] + q["bhi"][1])
            if abs(bx - qx) < CROWD_R_K * h and abs(by - qy) < CROWD_R_K * h:
                s += W_CROWD
        return round(s, RANK_DEC)

    # ---------- L6 greedy order: most constrained first ----------
    # The key depends only on the static feasible count and the request itself — independent of
    # any placement result, hence reproducible, and it cuts the backtracking rate sharply.
    order = sorted(range(len(requests)), key=lambda i: (
        len(STATIC[i]),
        round(float((requests[i].hi[0] - requests[i].lo[0])
                    * (requests[i].hi[1] - requests[i].lo[1])), RANK_DEC),
        requests[i].numeral))

    # ---------- L7 greedy + bounded repair ----------
    # banned[i] holds candidate INDICES that may no longer be chosen; membership tests only,
    # never iterated. Indices rather than candidate objects: candidates hold tuples/floats, so a
    # list.index() lookup would hit numpy truth ambiguity and depend on dict comparison order.
    banned: Dict[int, Set[int]] = {i: set() for i in range(len(requests))}
    chosen: Dict[int, Dict[str, Any]] = {}
    for attempt in range(MAX_REPAIR + 1):
        chosen, placed, failed = {}, [], []
        for i in order:
            best = None
            for cd in STATIC[i]:                        # generation order; idx is the tie-break
                if cd["idx"] in banned[i]:
                    continue
                if not _dyn_ok(cd, placed):
                    continue
                key = (_score(cd, placed), cd["idx"])
                if best is None or key < best[0]:
                    best = (key, cd)
            if best is None:
                failed.append(i)
            else:
                chosen[i] = best[1]
                placed.append(best[1])
        if not failed:
            break
        if attempt == MAX_REPAIR:
            raise LabelError(
                "place_labels: 附图标记 %s 无法落位（每个已试 %d 个候选位，回退 %d 轮）。"
                "修复：对标准件设 label:\"none\"；或按 assembly.json:split_suggestions 拆图。"
                % (sorted(requests[i].numeral for i in failed), TRIED, MAX_REPAIR),
                unplaced=sorted(requests[i].numeral for i in failed), tried=TRIED)
        # Repair: find the placed labels that actually block the first failure and ban their
        # CURRENT candidate index. The criterion is decidable: with j removed, does the failing
        # request gain at least one feasible candidate?
        f = failed[0]
        progressed = False
        for j in sorted(chosen):                        # sorted, never dict insertion order
            rest = [chosen[q] for q in sorted(chosen) if q != j]
            if any(cd["idx"] not in banned[f] and _dyn_ok(cd, rest) for cd in STATIC[f]):
                if chosen[j]["idx"] not in banned[j]:
                    banned[j].add(chosen[j]["idx"])
                    progressed = True
        if not progressed:
            raise LabelError(
                "place_labels: 附图标记 %d 无解且无可禁的阻挡者——这张图过载。"
                "修复：对标准件设 label:\"none\"；或按 assembly.json:split_suggestions 拆图。"
                % requests[f].numeral, unplaced=[requests[f].numeral], tried=TRIED)
        # banned grows monotonically over a finite candidate table => termination is guaranteed

    # ---------- L8 postcondition re-check (catches implementation bugs, not algorithm gaps) ----
    ks = sorted(chosen)
    if len(ks) != len(requests):
        raise LabelError("place_labels: 仅落位 %d/%d 个标记（内部错误）"
                         % (len(ks), len(requests)),
                         unplaced=sorted(requests[i].numeral for i in range(len(requests))
                                         if i not in chosen), tried=TRIED)
    for x in range(len(ks)):
        a = chosen[ks[x]]
        if _box_hits_geometry(a["blo"], a["bhi"]):
            raise LabelError("后置条件破坏：标记 %d 的数字框压住几何"
                             % requests[ks[x]].numeral, [requests[ks[x]].numeral], TRIED)
        if not _box_inside(a["blo"], a["bhi"], slo, shi, EPS):
            raise LabelError("后置条件破坏：标记 %d 越出图框"
                             % requests[ks[x]].numeral, [requests[ks[x]].numeral], TRIED)
        for y in range(x + 1, len(ks)):
            b = chosen[ks[y]]
            if _box_overlap(a["blo"], a["bhi"], b["blo"], b["bhi"], EPS):
                raise LabelError("后置条件破坏：标记 %d/%d 的数字框重叠"
                                 % (requests[ks[x]].numeral, requests[ks[y]].numeral),
                                 [requests[ks[x]].numeral, requests[ks[y]].numeral], TRIED)
            for A, B in ((a["anchor"], a["elbow"]), (a["elbow"], a["land"])):
                for C, D in ((b["anchor"], b["elbow"]), (b["elbow"], b["land"])):
                    if _seg_cross(A, B, C, D, EPS):
                        raise LabelError("后置条件破坏：标记 %d/%d 的引线交叉"
                                         % (requests[ks[x]].numeral, requests[ks[y]].numeral),
                                         [requests[ks[x]].numeral, requests[ks[y]].numeral], TRIED)
                if _seg_hits_box(A, B, b["blo"], b["bhi"], EPS):
                    raise LabelError("后置条件破坏：标记 %d 的引线穿过标记 %d 的数字框"
                                     % (requests[ks[x]].numeral, requests[ks[y]].numeral),
                                     [requests[ks[x]].numeral, requests[ks[y]].numeral], TRIED)
            for C, D in ((b["anchor"], b["elbow"]), (b["elbow"], b["land"])):
                if _seg_hits_box(C, D, a["blo"], a["bhi"], EPS):
                    raise LabelError("后置条件破坏：标记 %d 的引线穿过标记 %d 的数字框"
                                     % (requests[ks[y]].numeral, requests[ks[x]].numeral),
                                     [requests[ks[x]].numeral, requests[ks[y]].numeral], TRIED)

    # ---------- L9 output, ascending numeral (decoupled from layer write order) ----------
    out: List[LabelPlacement] = []
    for i in sorted(ks, key=lambda t: requests[t].numeral):
        cd = chosen[i]
        out.append(LabelPlacement(
            numeral=requests[i].numeral, key=requests[i].key,
            text_pos=(round(cd["text_pos"][0], RANK_DEC), round(cd["text_pos"][1], RANK_DEC)),
            text_align=cd["align"],
            leader=[(round(cd["anchor"][0], RANK_DEC), round(cd["anchor"][1], RANK_DEC)),
                    (round(cd["elbow"][0], RANK_DEC), round(cd["elbow"][1], RANK_DEC)),
                    (round(cd["land"][0], RANK_DEC), round(cd["land"][1], RANK_DEC))],
            score=_score(cd, [chosen[q] for q in ks if q != i])))
    return out


def text_height_for(slot: float, ratio: float = TEXT_RATIO) -> float:
    """slot * ratio. The QA gate requires height/slot <= 0.6, so 0.45 leaves margin.

    This is the RAW text height: it has not been snapped to the GB text-height series and has not
    been checked against the absolute floor (§11.6.1)."""
    return slot * ratio
