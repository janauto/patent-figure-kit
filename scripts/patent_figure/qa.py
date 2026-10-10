"""Readability and compliance gate for a rendered patent figure (DXF).

This module implements impl-contract.md sections 4.4 / 5.5, with the revisions listed in
section 11.10 ("对 qa.py 的修订要求").  It is the gate that was missing in v1, where a figure
with 97% overlapping numerals, 13.5% geometry occupancy and an internal parts table shipped
without a single automated complaint.

Design rule for this module: **the gate is independent of the modules it audits.**
`qa.py` never imports `layout.py` / `labels.py` / `sheet.py`.  Every constant below is taken
from the contract's constant tables (section 11.2.2 / 11.2.3) and cited inline, so a producer
module that drifts away from the contract is caught instead of being confirmed by its own
numbers.  The only third-party dependency is ezdxf (contract section 1).

`check_figure` never raises: a broken drawing — or a bug in one of the checks — is reported,
not propagated.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import ezdxf
from ezdxf import bbox as ezbbox

__all__ = [
    "Check",
    "DEFAULT_THRESHOLDS",
    "FORBIDDEN_TEXT_PATTERNS",
    "check_figure",
    "label_margin_mm",
    "caption_band_mm",
]

# ---------------------------------------------------------------------------
# Layer names — contract section 5.4 `LAYERS`.  Mirrored here (not imported) so the gate keeps
# working when sheet.py is unavailable or wrong.
# ---------------------------------------------------------------------------
LAYER_GEOM = "GEOM"
LAYER_HIDDEN = "HIDDEN"
LAYER_LEADER = "LEADER"
LAYER_NUM = "NUM"
LAYER_CAPTION = "CAPTION"
GEOMETRY_LAYERS = (LAYER_GEOM, LAYER_HIDDEN)

# ---------------------------------------------------------------------------
# Sheet band constants — contract section 11.2.2, used to rebuild the usable area (aw, ah)
# from the numeral height recorded in the DXF (section 11.10 requirement 2).
# ---------------------------------------------------------------------------
# IMPORTED, never re-declared. layout.py is the single definition point for every sheet constant
# (contract section 11.10). A second copy here would let the two drift, and the moment they drift
# the sheet_fill denominator stops describing the area the renderer actually used, so the gate
# silently measures the wrong thing. layout.py is pure numpy, so importing it costs nothing.
from .layout import (CAPTION_CAP_MM, CAPTION_K, FRAME_H, FRAME_W,  # noqa: E402
                     LABEL_MARGIN_CAP_MM, LABEL_MARGIN_K, TEXT_FLOOR_MM)

# ---------------------------------------------------------------------------
# Numeral box reconstruction — contract section 11.2.3.  A DXF records only the insert point
# and the character height, so the box a numeral actually occupies has to be rebuilt with the
# same formula labels.py used to reserve it:
#
#     text width  tw = CHAR_W * h * n_chars
#     box         = [x0 - PAD_X*h, x0 + tw + PAD_X*h] x [yc - 0.5h - PAD_Y*h, yc + 0.5h + PAD_Y*h]
#
# where x0 is derived from the entity's horizontal alignment and yc from its vertical one.
# ---------------------------------------------------------------------------
CHAR_W = 0.71   # section 11.2.3: GB/T 14691 type-B character width = h/sqrt(2) = 0.7071, rounded up
PAD_X = 0.30    # section 11.2.3: GB letter spacing a = 0.2h; 2*PAD_X*h = 0.6h clear between boxes
PAD_Y = 0.20    # section 11.2.3: line height h + 2*PAD_Y*h = 1.4h, GB/T 14691 type-B minimum

# ---------------------------------------------------------------------------
# Determinism / tolerance constants — contract section 7 rule 4, section 11.2.1, section 11.2.3.
# Two different tolerances on purpose: each mirrors the module that produced the data being
# checked, so the gate does not report a violation the producer was allowed to leave behind.
# ---------------------------------------------------------------------------
RANK_DEC = 9                    # section 11.2.1 / 11.2.3: quantise before every comparison
EPS_SHEET_REL = 1e-9            # section 11.2.3: labels.py compares at 1e-9 * frame diagonal
OVERLAP_TOL_REL = 1e-6          # section 11.2.1 / 11.10 #5: layout P1 separates at 1e-6 * SCALE_REF

NUMERAL_RE = re.compile(r"^[0-9]+$")

# ---------------------------------------------------------------------------
# Thresholds — contract section 5.5, verbatim.
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS: Dict[str, float] = {
    # area(GEOM u HIDDEN bbox) / area(bbox of ALL modelspace entities).  SCALE-INVARIANT: it
    # measures how much of the sheet was diluted by tables and stray text, NOT whether the
    # drawing fills the paper.  Reproduces the failure baseline at 0.1353.  See section 11.1 D1.
    "geometry_occupancy_min": 0.55,
    # area(GEOM u HIDDEN bbox) / (aw * ah), aw/ah = usable area after the label margin and the
    # caption band.  THIS is the gate that measures page filling (section 11.1 D2).
    "sheet_fill_min": 0.55,
    "label_overlap_pairs_max": 0,
    # Absolute floor (section 11.1 D3).  text_slot_ratio alone cannot catch small numerals: the
    # contract freezes text_height_for = slot * 0.45, so that ratio is identically 0.45.
    "text_height_mm_min": 3.5,
    "text_slot_ratio_max": 0.6,
    "part_bbox_overlap_pairs_max": 0,      # exploded figures only, per BODY (section 11.10 #6)
    "labels_per_figure_max": 20,
    "non_numeral_text_ratio_max": 0.10,
    "leader_crossing_max": 0,
    "leader_hits_numeral_box_max": 0,
    # A leader must ATTACH to the part it names. Expressed as a fraction of the sheet diagonal so
    # it is scale-free. 1% catches the defect this gate was written for — anchors taken from a
    # part's bounding box instead of its silhouette, measured at up to 12% of the diagonal away
    # on a real assembly, i.e. leaders visibly pointing at empty paper. It deliberately does NOT
    # demand zero: in an assembly view a labelled part may be partly occluded, so its true
    # silhouette anchor can sit a couple of millimetres from the nearest DRAWN line (measured at
    # 0.8% on the synthetic assembly). Snapping such an anchor onto the nearest visible line
    # would be worse — it can land on a neighbouring part and name the wrong thing.
    "leader_anchor_gap_ratio_max": 0.01,
    "non_continuous_max": 0,
}

FORBIDDEN_TEXT_PATTERNS = [r"^[A-Z]{2,4}[0-9]{4,8}(-|_)", r"^[0-9]{4,6}-[A-Z][0-9]{2}",
                           r"_[0-9]+_[0-9]+$"]

# Checks appear in the report in this order, always.
CHECK_ORDER = (
    "geometry_occupancy",
    "sheet_fill",
    "text_height_mm",
    "text_slot_ratio",
    "label_overlap_pairs",
    "leader_hits_numeral_box",
    "leader_crossing",
    "leader_anchor_gap",
    "part_bbox_overlap_pairs",
    "labels_per_figure",
    "non_numeral_text_ratio",
    "non_continuous",
    "forbidden_text",
)

_MAX_DETAIL_ITEMS = 5           # how many offending items a detail string spells out


# ===========================================================================
# Check record
# ===========================================================================
@dataclass
class Check:
    """One gate.  `passed` is serialised as the JSON key "pass" (contract section 4.4)."""

    id: str
    passed: bool
    value: Any
    threshold: str
    detail: str
    hint: str

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "pass": bool(self.passed), "value": self.value,
                "threshold": self.threshold, "detail": self.detail, "hint": self.hint}


# ===========================================================================
# Sheet geometry helpers — same closed form as contract section 11.6
# ===========================================================================
def label_margin_mm(h: float) -> float:
    """Width of one label gutter for numeral height `h` (contract section 11.6.3)."""
    return min(LABEL_MARGIN_K * float(h), LABEL_MARGIN_CAP_MM)


def caption_band_mm(h: float) -> float:
    """Height of the caption band for numeral height `h` (contract section 11.6.3)."""
    return min(CAPTION_K * float(h), CAPTION_CAP_MM)


def usable_area_mm(h: float) -> Tuple[float, float]:
    """(aw, ah) usable area, contract section 11.6.3 / 11.10 requirement 2."""
    lm = label_margin_mm(h)
    cb = caption_band_mm(h)
    return FRAME_W - 2.0 * lm, FRAME_H - lm - cb


# ===========================================================================
# Deterministic geometric predicates.  Deliberately re-implemented here rather than imported
# from labels.py: a checker that borrows the predicate it is checking proves nothing.
# ===========================================================================
def _q(v: float) -> float:
    return round(float(v), RANK_DEC)


def _point_seg_distance(p, a, b) -> float:
    """Distance from point p to segment a-b. Plain scalar maths: the counts here are small
    (leaders x geometry segments) and a closed form keeps the result platform-independent."""
    ax, ay = float(a[0]), float(a[1])
    vx, vy = float(b[0]) - ax, float(b[1]) - ay
    wx, wy = float(p[0]) - ax, float(p[1]) - ay
    denom = vx * vx + vy * vy
    t = 0.0 if denom <= 0.0 else (wx * vx + wy * vy) / denom
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(wx - vx * t, wy - vy * t)


def _box_area(b: Sequence[float]) -> float:
    return max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))


def _box_union(a: Optional[Sequence[float]], b: Optional[Sequence[float]]):
    if a is None:
        return None if b is None else tuple(float(v) for v in b)
    if b is None:
        return tuple(float(v) for v in a)
    return (min(float(a[0]), float(b[0])), min(float(a[1]), float(b[1])),
            max(float(a[2]), float(b[2])), max(float(a[3]), float(b[3])))


def _box_overlap(a: Sequence[float], b: Sequence[float], eps: float) -> bool:
    """Strict overlap; touching edges are NOT an overlap (contract section 11.7 `_box_overlap`)."""
    return (_q(a[0]) < _q(b[2] - eps) and _q(b[0]) < _q(a[2] - eps) and
            _q(a[1]) < _q(b[3] - eps) and _q(b[1]) < _q(a[3] - eps))


def _boxes_touch(a: Sequence[float], b: Sequence[float], eps: float) -> bool:
    """Overlap OR touch — the adjacency relation used to build geometry clusters."""
    return (_q(a[0]) <= _q(b[2] + eps) and _q(b[0]) <= _q(a[2] + eps) and
            _q(a[1]) <= _q(b[3] + eps) and _q(b[1]) <= _q(a[3] + eps))


def _point_in_box(b: Sequence[float], p: Sequence[float], eps: float) -> bool:
    return (_q(b[0] - eps) <= _q(p[0]) <= _q(b[2] + eps) and
            _q(b[1] - eps) <= _q(p[1]) <= _q(b[3] + eps))


def _point_box_distance(b: Sequence[float], p: Sequence[float]) -> float:
    dx = max(float(b[0]) - float(p[0]), 0.0, float(p[0]) - float(b[2]))
    dy = max(float(b[1]) - float(p[1]), 0.0, float(p[1]) - float(b[3]))
    return math.hypot(dx, dy)


def _cross(ax: float, ay: float, bx: float, by: float) -> float:
    return ax * by - ay * bx


def _seg_cross(p0, p1, q0, q1) -> bool:
    """True crossing only.  Shared endpoints and collinear overlaps are NOT crossings
    (contract section 11.7 `_seg_cross`); signs are quantised before comparison."""
    d1 = round(_cross(p1[0] - p0[0], p1[1] - p0[1], q0[0] - p0[0], q0[1] - p0[1]), RANK_DEC)
    d2 = round(_cross(p1[0] - p0[0], p1[1] - p0[1], q1[0] - p0[0], q1[1] - p0[1]), RANK_DEC)
    d3 = round(_cross(q1[0] - q0[0], q1[1] - q0[1], p0[0] - q0[0], p0[1] - q0[1]), RANK_DEC)
    d4 = round(_cross(q1[0] - q0[0], q1[1] - q0[1], p1[0] - q0[0], p1[1] - q0[1]), RANK_DEC)
    return ((d1 > 0.0) != (d2 > 0.0)) and ((d3 > 0.0) != (d4 > 0.0))


def _seg_hits_box(p0, p1, box: Sequence[float], eps: float) -> bool:
    """Segment vs AABB, endpoints inside included (contract section 11.7 `_seg_hits_box`)."""
    for pt in (p0, p1):
        if (_q(box[0] - eps) <= _q(pt[0]) <= _q(box[2] + eps) and
                _q(box[1] - eps) <= _q(pt[1]) <= _q(box[3] + eps)):
            return True
    c = ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
    for k in range(4):
        if _seg_cross(p0, p1, c[k], c[(k + 1) % 4]):
            return True
    return False


# ===========================================================================
# DXF reading
# ===========================================================================
_H_LEFT, _H_CENTER, _H_RIGHT = "L", "C", "R"


def _text_payload(entity) -> Optional[Tuple[str, float, Tuple[float, float], str, str]]:
    """Return (text, height, anchor_point, halign_kind, valign_kind) or None.

    valign_kind is one of "BASE" | "MID" | "TOP" (the vertical anchor of the anchor point).
    """
    kind = entity.dxftype()
    if kind in ("TEXT", "ATTRIB", "ATTDEF"):
        text = str(entity.dxf.get("text", "") or "")
        height = float(entity.dxf.get("height", 0.0) or 0.0)
        halign = int(entity.dxf.get("halign", 0) or 0)
        valign = int(entity.dxf.get("valign", 0) or 0)
        if halign == 0 and valign == 0:
            p = entity.dxf.get("insert", (0.0, 0.0))
        else:
            p = entity.dxf.get("align_point", entity.dxf.get("insert", (0.0, 0.0)))
        # DXF group codes 72 / 73: 0 left, 1 center, 2 right, 3 aligned, 4 middle, 5 fit
        h_kind = {0: _H_LEFT, 1: _H_CENTER, 2: _H_RIGHT, 3: _H_LEFT,
                  4: _H_CENTER, 5: _H_LEFT}.get(halign, _H_LEFT)
        if halign == 4:
            v_kind = "MID"
        else:
            v_kind = {0: "BASE", 1: "BASE", 2: "MID", 3: "TOP"}.get(valign, "BASE")
        return text, height, (float(p[0]), float(p[1])), h_kind, v_kind
    if kind == "MTEXT":
        try:
            text = str(entity.text or "")
        except Exception:
            text = ""
        height = float(entity.dxf.get("char_height", 0.0) or 0.0)
        p = entity.dxf.get("insert", (0.0, 0.0))
        ap = int(entity.dxf.get("attachment_point", 1) or 1)
        h_kind = {1: _H_LEFT, 2: _H_CENTER, 3: _H_RIGHT,
                  4: _H_LEFT, 5: _H_CENTER, 6: _H_RIGHT,
                  7: _H_LEFT, 8: _H_CENTER, 9: _H_RIGHT}.get(ap, _H_LEFT)
        v_kind = {1: "TOP", 2: "TOP", 3: "TOP", 4: "MID", 5: "MID", 6: "MID",
                  7: "BASE", 8: "BASE", 9: "BASE"}.get(ap, "TOP")
        return text, height, (float(p[0]), float(p[1])), h_kind, v_kind
    return None


def _text_box(text: str, height: float, anchor: Tuple[float, float],
              h_kind: str, v_kind: str) -> Tuple[float, float, float, float]:
    """Rebuild the box a text occupies, using the section 11.2.3 formula.

    Chinese glyphs are wider than CHAR_W*h; the resulting box is therefore a lower bound for
    caption text.  It is exact for the numerals on the NUM layer, which is what the numeral
    gates are measured on.
    """
    h = float(height)
    tw = CHAR_W * h * max(len(text), 1)
    if h_kind == _H_RIGHT:
        x0 = anchor[0] - tw
    elif h_kind == _H_CENTER:
        x0 = anchor[0] - 0.5 * tw
    else:
        x0 = anchor[0]
    if v_kind == "MID":
        yc = anchor[1]
    elif v_kind == "TOP":
        yc = anchor[1] - 0.5 * h
    else:                                        # baseline / bottom
        yc = anchor[1] + 0.5 * h
    return (x0 - PAD_X * h, yc - 0.5 * h - PAD_Y * h,
            x0 + tw + PAD_X * h, yc + 0.5 * h + PAD_Y * h)


def _entity_points(entity) -> List[Tuple[float, float]]:
    """Vertices of a polyline-ish entity, in stored order.  Empty for anything else."""
    kind = entity.dxftype()
    try:
        if kind == "LWPOLYLINE":
            return [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
        if kind == "POLYLINE":
            return [(float(v.dxf.location[0]), float(v.dxf.location[1]))
                    for v in entity.vertices]
        if kind == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            return [(float(s[0]), float(s[1])), (float(e[0]), float(e[1]))]
    except Exception:
        return []
    return []


def _entity_box(entity, text_payload) -> Optional[Tuple[float, float, float, float]]:
    if text_payload is not None:
        return _text_box(*text_payload)
    pts = _entity_points(entity)
    if len(pts) >= 1:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))
    try:
        b = ezbbox.extents([entity], fast=False)
    except Exception:
        return None
    if not b.has_data:
        return None
    return (float(b.extmin.x), float(b.extmin.y), float(b.extmax.x), float(b.extmax.y))


@dataclass
class _Record:
    index: int
    layer: str
    dxftype: str
    box: Optional[Tuple[float, float, float, float]]
    text: Optional[str]
    height: float
    text_box: Optional[Tuple[float, float, float, float]]
    points: List[Tuple[float, float]]
    linetype: str


def _read_records(doc) -> Tuple[List[_Record], List[str]]:
    notes: List[str] = []
    layer_lt: Dict[str, str] = {}
    try:
        for lay in doc.layers:
            layer_lt[str(lay.dxf.name).upper()] = str(lay.dxf.get("linetype", "CONTINUOUS")
                                                      or "CONTINUOUS").upper()
    except Exception as exc:                                  # pragma: no cover - defensive
        notes.append("读取图层表失败：%s" % exc)

    records: List[_Record] = []
    for i, entity in enumerate(doc.modelspace()):
        try:
            layer = str(entity.dxf.get("layer", "0") or "0").upper()
            payload = _text_payload(entity)
            box = _entity_box(entity, payload)
            lt = str(entity.dxf.get("linetype", "BYLAYER") or "BYLAYER").upper()
            if lt == "BYLAYER":
                lt = layer_lt.get(layer, "CONTINUOUS")
            records.append(_Record(
                index=i, layer=layer, dxftype=entity.dxftype(), box=box,
                text=None if payload is None else payload[0],
                height=0.0 if payload is None else float(payload[1]),
                text_box=None if payload is None else _text_box(*payload),
                points=_entity_points(entity), linetype=lt))
        except Exception as exc:
            notes.append("第 %d 个实体解析失败（已跳过）：%s" % (i, exc))
    return records, notes


# ===========================================================================
# Geometry clusters (fallback part boundaries + the slot gauge of section 11.10 #4)
# ===========================================================================
def _cluster_geometry(boxes: List[Tuple[float, float, float, float]],
                      eps: float) -> List[Tuple[float, float, float, float]]:
    """Connected components of geometry bounding boxes (adjacency = overlap or touch).

    Sweep by ascending x so the pairwise work stays local; union-find keeps the result
    independent of the sweep order.
    """
    n = len(boxes)
    if n == 0:
        return []
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    order = sorted(range(n), key=lambda i: (round(boxes[i][0], RANK_DEC),
                                            round(boxes[i][1], RANK_DEC), i))
    active: List[int] = []
    for idx in order:
        b = boxes[idx]
        active = [j for j in active if _q(boxes[j][2]) >= _q(b[0] - eps)]
        for j in active:
            if _boxes_touch(boxes[j], b, eps):
                union(j, idx)
        active.append(idx)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for root in sorted(groups):                       # sorted: never iterate a dict raw
        members = groups[root]
        out.append((min(boxes[m][0] for m in members), min(boxes[m][1] for m in members),
                    max(boxes[m][2] for m in members), max(boxes[m][3] for m in members)))
    return sorted(out, key=lambda c: (round(c[0], RANK_DEC), round(c[1], RANK_DEC),
                                      round(c[2], RANK_DEC), round(c[3], RANK_DEC)))


def _cluster_for_point(clusters: List[Tuple[float, float, float, float]],
                       pt: Tuple[float, float], eps: float):
    """Cluster a leader anchor belongs to: the smallest cluster containing it, else the
    nearest one.  Total order on the tie-break keys, so the choice is reproducible."""
    if not clusters:
        return None
    containing = [c for c in clusters if _point_in_box(c, pt, eps)]
    if containing:
        return min(containing, key=lambda c: (round(_box_area(c), RANK_DEC),
                                              round(c[0], RANK_DEC), round(c[1], RANK_DEC)))
    return min(clusters, key=lambda c: (round(_point_box_distance(c, pt), RANK_DEC),
                                        round(c[0], RANK_DEC), round(c[1], RANK_DEC)))


# ===========================================================================
# Sidecar (renderer-written body boxes, contract section 11.10 requirement 5)
# ===========================================================================
def _sidecar_path(dxf: Path) -> Path:
    return dxf.parent / (dxf.stem + ".layout.json")


def _load_sidecar(dxf: Path):
    """Return (boxes, note).  boxes is None when the sidecar is unusable.

    Accepted shapes: a bare list of body boxes, or an object carrying them under
    "body_boxes" / "bodies" / "boxes".  Each item needs "lo" and "hi".
    """
    path = _sidecar_path(dxf)
    if not path.exists():
        return None, "未找到 sidecar %s" % path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, "sidecar %s 解析失败：%s" % (path.name, exc)
    items = None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("body_boxes", "bodies", "boxes"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if items is None and isinstance(data.get("diagnostics"), dict):
            cand = data["diagnostics"].get("body_boxes")
            if isinstance(cand, list):
                items = cand
    if items is None:
        return None, "sidecar %s 里找不到 body_boxes" % path.name
    boxes = []
    for i, item in enumerate(items):
        try:
            lo = item["lo"]
            hi = item["hi"]
            key = str(item.get("key", "body#%d" % i))
            boxes.append((float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]), key))
        except Exception:
            return None, "sidecar %s 第 %d 条 body 缺少 lo/hi" % (path.name, i)
    return boxes, "sidecar %s（%d 个 body）" % (path.name, len(boxes))


# ===========================================================================
# check_figure
# ===========================================================================
def _fmt(v: float, nd: int = 3) -> str:
    return ("%." + str(nd) + "f") % float(v)


def _threshold_str(op: str, value: Any) -> str:
    if isinstance(value, float):
        text = ("%g" % value)
    else:
        text = str(value)
    return "%s%s" % (op, text)


def _report(dxf: Path, checks: List[Check]) -> Dict[str, Any]:
    passed = sum(1 for c in checks if c.passed)
    return {"schema": "patent-figure-qa/1",
            "file": dxf.name,
            "pass": all(c.passed for c in checks),
            "checks": [c.to_json() for c in checks],
            "summary": {"failed": len(checks) - passed, "passed": passed}}


def check_figure(dxf, *, thresholds: Optional[dict] = None,
                 forbidden: Optional[List[str]] = None, kind: str = "exploded") -> dict:
    """Run every readability / compliance gate on one figure DXF.

    Returns the qa.json shape of contract section 4.4.  Never raises: an unreadable file, a
    malformed entity, or a defect inside one of the checks is reported as a failed check.
    """
    dxf = Path(dxf)
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update(thresholds)
    patterns = list(FORBIDDEN_TEXT_PATTERNS if forbidden is None else forbidden)

    try:
        doc = ezdxf.readfile(str(dxf))
    except Exception as exc:
        return _report(dxf, [Check(
            id="file_readable", passed=False, value=False, threshold="==true",
            detail="无法用 ezdxf 读取 %s：%s" % (dxf.name, exc),
            hint="这不是 plan 的问题：确认 render_patent_figure.py 真的写出了这张图，"
                 "并检查 out/ 目录下的文件是否被截断。")])

    try:
        checks = _run_checks(dxf, doc, th, patterns, kind)
    except Exception as exc:                                  # last-resort net, section 5.5
        return _report(dxf, [Check(
            id="qa_internal_error", passed=False, value=str(exc), threshold="==none",
            detail="qa.py 自身在检查 %s 时异常：%r。这是 qa.py 的实现错误，不是 plan 的问题。"
                   % (dxf.name, exc),
            hint="请把这条信息连同 DXF 一起提给 qa.py 的维护者；在修复前不要把本图当作已通过。")])
    return _report(dxf, checks)


def _run_checks(dxf: Path, doc, th: Dict[str, Any], patterns: List[str],
                kind: str) -> List[Check]:
    records, notes = _read_records(doc)
    note_suffix = ("；" + "；".join(notes)) if notes else ""

    geom_records = [r for r in records if r.layer in GEOMETRY_LAYERS and r.box is not None]
    all_boxes = [r.box for r in records if r.box is not None]

    geom_box = None
    for r in geom_records:
        geom_box = _box_union(geom_box, r.box)
    all_box = None
    for b in all_boxes:
        all_box = _box_union(all_box, b)

    frame_diag = math.hypot(FRAME_W, FRAME_H)
    eps_sheet = EPS_SHEET_REL * frame_diag
    if geom_box is not None:
        geom_diag = math.hypot(geom_box[2] - geom_box[0], geom_box[3] - geom_box[1])
    else:
        geom_diag = frame_diag
    eps_body = OVERLAP_TOL_REL * max(geom_diag, frame_diag)

    texts = [r for r in records if r.text is not None and r.text.strip() != ""]
    num_texts = sorted([r for r in texts if r.layer == LAYER_NUM],
                       key=lambda r: (round(r.text_box[0], RANK_DEC),
                                      round(r.text_box[1], RANK_DEC), r.index))
    leaders = sorted([r for r in records if r.layer == LAYER_LEADER and len(r.points) >= 2],
                     key=lambda r: (round(r.points[0][0], RANK_DEC),
                                    round(r.points[0][1], RANK_DEC), r.index))

    out: Dict[str, Check] = {}

    # ---- 1. geometry_occupancy (contract section 11.1 D1) -------------------
    lim = float(th["geometry_occupancy_min"])
    num_area = _box_area(geom_box) if geom_box is not None else 0.0
    den_area = _box_area(all_box) if all_box is not None else 0.0
    if den_area <= 0.0:
        value = 0.0
        detail = "模型空间没有任何有面积的实体（全部实体包围盒面积为 0）。" + note_suffix
    else:
        value = round(num_area / den_area, 6)
        detail = ("GEOM∪HIDDEN 包围盒 %s×%s=%s mm²，全部实体包围盒 %s×%s=%s mm²。"
                  % (_fmt(geom_box[2] - geom_box[0]) if geom_box else "0",
                     _fmt(geom_box[3] - geom_box[1]) if geom_box else "0",
                     _fmt(num_area, 1),
                     _fmt(all_box[2] - all_box[0]), _fmt(all_box[3] - all_box[1]),
                     _fmt(den_area, 1)) + note_suffix)
    out["geometry_occupancy"] = Check(
        id="geometry_occupancy", passed=value >= lim, value=value,
        threshold=_threshold_str(">=", lim), detail=detail,
        hint="版面被非几何实体稀释了：把 layout.engineering_table 设为 false"
             "（明细表只允许出现在 <id>_engineering.dxf，见契约 §8），"
             "并删掉 TABLE / NOTE 图层上的说明文字；标记边槽过宽时改 figures[].layout 的字号来源"
             "（对标准件设 terms[].label=\"none\" 可减少标记）。")

    # ---- 2. sheet_fill (contract section 11.1 D2 / 11.10 #2) ----------------
    lim = float(th["sheet_fill_min"])
    heights = sorted(round(r.height, RANK_DEC) for r in num_texts if r.height > 0.0)
    if heights:
        h_read = heights[0]
        basis = "h=%s mm（读自 NUM 图层 TEXT）" % _fmt(h_read, 2)
        if len(set(heights)) > 1:
            basis += "；注意 NUM 图层存在 %d 种字高，取最小值" % len(set(heights))
    else:
        h_read = TEXT_FLOOR_MM
        basis = "NUM 图层没有 TEXT 实体，按 h=%s mm 的可用区估算（退化口径）" % _fmt(TEXT_FLOOR_MM, 1)
    aw, ah = usable_area_mm(h_read)
    insunits_note = ""
    try:
        insunits = int(doc.header.get("$INSUNITS", 0))
        if insunits != 4:
            insunits_note = ("；$INSUNITS=%d 而不是 4（毫米），本图的毫米口径可能不成立"
                             % insunits)
    except Exception:
        insunits_note = ""
    if aw <= 0.0 or ah <= 0.0:
        value = 0.0
        detail = "可用区退化（aw=%s, ah=%s），%s" % (_fmt(aw), _fmt(ah), basis)
    else:
        value = round(num_area / (aw * ah), 6)
        detail = ("几何 %s×%s mm，可用区 aw×ah=%s×%s mm（%s）%s"
                  % (_fmt(geom_box[2] - geom_box[0]) if geom_box else "0",
                     _fmt(geom_box[3] - geom_box[1]) if geom_box else "0",
                     _fmt(aw, 1), _fmt(ah, 1), basis, insunits_note))
    out["sheet_fill"] = Check(
        id="sheet_fill", passed=value >= lim, value=value,
        threshold=_threshold_str(">=", lim), detail=detail,
        hint="纸没填满：把 figures[].layout.axis_angle 改回 \"auto\"，让 renderer 逐图闭式求最优角；"
             "已经是 auto 时说明旋转救不回来——换 layout.view，或按 "
             "assembly.json:split_suggestions 拆图。")

    # ---- 3. text_height_mm (contract section 11.1 D3 / 11.10 #3) ------------
    lim = float(th["text_height_mm_min"])
    if not num_texts:
        # 零标记的图是合法的（§11.6.3 明确处理「全部 label:\"none\"」），所以这里空过，
        # 但把事实喊出来；标记被渲染器悄悄丢掉的情形由 labels_per_figure=0 暴露。
        out["text_height_mm"] = Check(
            id="text_height_mm", passed=True, value=None,
            threshold=_threshold_str(">=", lim),
            detail="NUM 图层 0 个标记，本图没有字高可度量（若本图本应有标记，见 labels_per_figure）。",
            hint="本图一个附图标记都没有：检查 plan 的 terms[].label 是否全为 \"none\"，"
                 "或该 figure 的 members 是否漏掉了要标注的零件。")
    else:
        value = round(min(r.height for r in num_texts), 6)
        small = sorted([r.text for r in num_texts if round(r.height, RANK_DEC) < lim])
        out["text_height_mm"] = Check(
            id="text_height_mm", passed=value >= lim, value=value,
            threshold=_threshold_str(">=", lim),
            detail=("NUM 图层最小字高 %s mm（共 %d 个标记）%s"
                    % (_fmt(value, 2), len(num_texts),
                       "；低于下限的标记：" + "、".join(small[:_MAX_DETAIL_ITEMS])
                       if small else "")),
            hint="字太小意味着被标注件在图面上太小：对标准件设 terms[].label=\"none\"，"
                 "按 assembly.json:split_suggestions 拆图，或把 layout.density 改成 \"compact\"。")

    # ---- 4. text_slot_ratio (contract section 11.10 #4) ---------------------
    lim = float(th["text_slot_ratio_max"])
    geom_boxes = [r.box for r in geom_records]
    clusters = _cluster_geometry(geom_boxes, eps_sheet)
    if not num_texts or not leaders or not clusters:
        out["text_slot_ratio"] = Check(
            id="text_slot_ratio", passed=True, value=None,
            threshold=_threshold_str("<=", lim),
            detail="本图没有可度量的「引线 + 几何簇」组合（引线 %d 条、几何簇 %d 个、标记 %d 个），"
                   "本项无从判定。" % (len(leaders), len(clusters), len(num_texts)),
            hint="若本图本应有附图标记，先修 plan 的 terms[].label 与 figures[].members。")
    else:
        h_min = min(r.height for r in num_texts)
        worst = None
        slots = []
        for r in leaders:
            cl = _cluster_for_point(clusters, r.points[0], eps_sheet)
            if cl is None:
                continue
            slot = max(cl[2] - cl[0], cl[3] - cl[1])
            slots.append(slot)
            if worst is None or round(slot, RANK_DEC) < round(worst[0], RANK_DEC):
                worst = (slot, r.points[0])
        if not slots:
            out["text_slot_ratio"] = Check(
                id="text_slot_ratio", passed=True, value=None,
                threshold=_threshold_str("<=", lim),
                detail="所有引线锚点都找不到对应的几何簇，本项无从判定。",
                hint="若本图本应有附图标记，先修 plan 的 terms[].label 与 figures[].members。")
        else:
            slot_min = min(slots)
            # slot_min == 0 means the cluster the leader points at has no extent at all; that is
            # an unbounded ratio, reported as a failure with value None (JSON has no infinity).
            value = round(h_min / slot_min, 6) if slot_min > 0.0 else None
            out["text_slot_ratio"] = Check(
                id="text_slot_ratio", passed=(value is not None and value <= lim), value=value,
                threshold=_threshold_str("<=", lim),
                detail=("最小被标注几何簇外廓 %s mm（锚点 %s, %s），最小字高 %s mm，"
                        "口径为「沿每条 LEADER 首点找几何连通簇取 max(w,h)」（§11.10 #4）。"
                        % (_fmt(slot_min, 2), _fmt(worst[1][0], 1), _fmt(worst[1][1], 1),
                           _fmt(h_min, 2))),
                hint="被标注件相对字号太小：对该零件设 terms[].label=\"none\"，"
                     "或按 assembly.json:split_suggestions 拆图。")

    # ---- 5. label_overlap_pairs --------------------------------------------
    lim = int(th["label_overlap_pairs_max"])
    pairs = []
    for i in range(len(num_texts)):
        for j in range(i + 1, len(num_texts)):
            if _box_overlap(num_texts[i].text_box, num_texts[j].text_box, eps_sheet):
                pairs.append((num_texts[i].text, num_texts[j].text))
    value = len(pairs)
    out["label_overlap_pairs"] = Check(
        id="label_overlap_pairs", passed=value <= lim, value=value,
        threshold=_threshold_str("<=", lim),
        detail=("%d 对附图标记外接框相交（共 %d 个标记）。外接框按 GB/T 14691 B 型字宽 "
                "CHAR_W=%.2f（= h/√2 = 0.7071 向上取整）、水平留白 PAD_X=%.2f·h、"
                "垂直留白 PAD_Y=%.2f·h 还原，来源 §11.2.3%s"
                % (value, len(num_texts), CHAR_W, PAD_X, PAD_Y,
                   "；首例：" + "、".join("%s/%s" % p for p in pairs[:_MAX_DETAIL_ITEMS])
                   if pairs else "。")),
        hint="标记互相碾压：减少本图标记数（对标准件设 terms[].label=\"none\"），"
             "或按 assembly.json:split_suggestions 拆图。")

    # ---- 6. leader_hits_numeral_box (contract section 11.10 #6) ------------
    lim = int(th["leader_hits_numeral_box_max"])
    hits = []
    for r in leaders:
        for k in range(len(r.points) - 1):
            for t in num_texts:
                if _seg_hits_box(r.points[k], r.points[k + 1], t.text_box, eps_sheet):
                    hits.append((r.index, t.text))
    value = len(hits)
    out["leader_hits_numeral_box"] = Check(
        id="leader_hits_numeral_box", passed=value <= lim, value=value,
        threshold=_threshold_str("<=", lim),
        detail=("%d 处引线段与附图标记外接框相交（引线 %d 条）%s"
                % (value, len(leaders),
                   "；首例命中的标记：" + "、".join(sorted({h[1] for h in hits})[:_MAX_DETAIL_ITEMS])
                   if hits else "。")),
        hint="引线压住了数字：减少本图标记数（terms[].label=\"none\"）或按 "
             "assembly.json:split_suggestions 拆图；若反复出现，说明本图标记密度已超载。")

    # ---- 7. leader_crossing -------------------------------------------------
    lim = int(th["leader_crossing_max"])
    segs = []
    for r in leaders:
        for k in range(len(r.points) - 1):
            segs.append((r.index, r.points[k], r.points[k + 1]))
    cross = 0
    first = None
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if segs[i][0] == segs[j][0] and abs(i - j) == 1:
                continue                                  # 同一条引线的相邻段共端点
            if _seg_cross(segs[i][1], segs[i][2], segs[j][1], segs[j][2]):
                cross += 1
                if first is None:
                    first = (segs[i][1], segs[j][1])
    out["leader_crossing"] = Check(
        id="leader_crossing", passed=cross <= lim, value=cross,
        threshold=_threshold_str("<=", lim),
        detail=("%d 对引线段真交叉（共端点与共线重合不计），引线 %d 条%s"
                % (cross, len(leaders),
                   "；首例起点 (%s, %s) 与 (%s, %s)"
                   % (_fmt(first[0][0], 1), _fmt(first[0][1], 1),
                      _fmt(first[1][0], 1), _fmt(first[1][1], 1)) if first else "。")),
        hint="引线交叉：减少本图标记数（terms[].label=\"none\"）或按 "
             "assembly.json:split_suggestions 拆图。")

    # ---- 7b. leader_anchor_gap ----------------------------------------------
    lim_ratio = float(th["leader_anchor_gap_ratio_max"])
    lim_mm = lim_ratio * geom_diag
    gaps = []
    geom_segs = []
    for r in records:
        if r.layer not in GEOMETRY_LAYERS or len(r.points) < 2:
            continue
        pts = r.points
        for i in range(len(pts) - 1):
            geom_segs.append((pts[i], pts[i + 1]))
    for ld in leaders:
        a = ld.points[0]
        best = None
        for (p0, p1) in geom_segs:
            d = _point_seg_distance(a, p0, p1)
            if best is None or d < best:
                best = d
        if best is not None:
            gaps.append((round(best, RANK_DEC), round(a[0], RANK_DEC), round(a[1], RANK_DEC)))
    worst = max(g[0] for g in gaps) if gaps else 0.0
    over = [g for g in gaps if g[0] > lim_mm]
    if not gaps:
        detail = "本图没有引线。"
    else:
        detail = ("%d 条引线的锚点离最近几何：中位 %.3f mm、最大 %.3f mm（上限 %.3f mm = "
                  "图幅对角 %.1f mm 的 %.1f%%）。" %
                  (len(gaps), sorted(g[0] for g in gaps)[len(gaps) // 2], worst,
                   lim_mm, geom_diag, lim_ratio * 100.0))
        if over:
            detail += "超限的锚点：" + "、".join(
                "(%.1f, %.1f) 差 %.2f mm" % (g[1], g[2], g[0]) for g in over[:5])
    out["leader_anchor_gap"] = Check(
        id="leader_anchor_gap", passed=worst <= lim_mm + eps_body,
        value=round(worst, 3), threshold="<=%.3f mm" % lim_mm, detail=detail,
        hint=("引线没有落在它所指的零件轮廓上，读图者无法判断标记指的是哪个件。"
              "若该零件在本视图里被遮挡，改用分解图标注它，或把它从本图的 members 中移除。"))

    # ---- 8. part_bbox_overlap_pairs (contract section 11.10 #5, per BODY) ---
    lim = int(th["part_bbox_overlap_pairs_max"])
    if str(kind).lower() != "exploded":
        out["part_bbox_overlap_pairs"] = Check(
            id="part_bbox_overlap_pairs", passed=True, value=0,
            threshold=_threshold_str("<=", lim),
            detail="kind=%s：总装图的零件本来就互相遮挡，本项只对 exploded 生效（§5.5）。" % kind,
            hint="若本图应当是分解图，把 figures[].kind 改成 \"exploded\"。")
    else:
        bodies, note = _load_sidecar(dxf)
        if bodies is None:
            degraded = True
            bodies = [(c[0], c[1], c[2], c[3], "cluster#%d" % i)
                      for i, c in enumerate(clusters)]
        else:
            degraded = False
        pairs = []
        for i in range(len(bodies)):
            for j in range(i + 1, len(bodies)):
                if _box_overlap(bodies[i][:4], bodies[j][:4], eps_body):
                    pairs.append((bodies[i][4], bodies[j][4]))
        value = len(pairs)
        detail = ("%d 对 body 图面包围盒重叠（共 %d 个 body，容差 %.1e mm = OVERLAP_TOL_REL×几何对角）"
                  % (value, len(bodies), eps_body))
        if degraded:
            detail += "；**退化口径**：%s，已回退到几何连通簇聚类，簇不等于 body" % note
        else:
            detail += "；口径：%s" % note
        if pairs:
            detail += "；首例：" + "、".join("%s/%s" % p for p in pairs[:_MAX_DETAIL_ITEMS])
        out["part_bbox_overlap_pairs"] = Check(
            id="part_bbox_overlap_pairs", passed=value <= lim, value=value,
            threshold=_threshold_str("<=", lim), detail=detail,
            hint="零件在图面上压叠：把 layout.density 改成 \"loose\"，"
                 "或换 figures[].layout.explode_axis / layout.view；仍不行就按 "
                 "assembly.json:split_suggestions 拆图。")

    # ---- 9. labels_per_figure ----------------------------------------------
    lim = int(th["labels_per_figure_max"])
    value = len(num_texts)
    out["labels_per_figure"] = Check(
        id="labels_per_figure", passed=value <= lim, value=value,
        threshold=_threshold_str("<=", lim),
        detail="本图有 %d 个附图标记。" % value,
        hint="拆图：按 assembly.json:split_suggestions 把该 figure 拆成多张，"
             "或对标准件设 terms[].label=\"none\"。")

    # ---- 10. non_numeral_text_ratio ----------------------------------------
    lim = float(th["non_numeral_text_ratio_max"])
    # 契约 §8 允许的文字恰好是「附图标记 + 图题」，所以分子把 CAPTION 图层排除在外，分母仍是
    # 全部文字。否则 N 个标记 + 1 条图题恒为 1/(N+1)，任何 <=8 个标记的合法图都会结构性不达标
    # （§4.2 的 fig2 只有 2 个成员，§11.6.3 还专门处理了零标记图）。分子仍然抓得住明细表、
    # 零件名与说明文字——它们不在 CAPTION 图层上。
    non_numeral = sorted([r.text.strip() for r in texts
                          if r.layer != LAYER_CAPTION and not NUMERAL_RE.match(r.text.strip())])
    total = len(texts)
    value = round(len(non_numeral) / total, 6) if total else 0.0
    out["non_numeral_text_ratio"] = Check(
        id="non_numeral_text_ratio", passed=value <= lim, value=value,
        threshold=_threshold_str("<=", lim),
        detail=("%d/%d 条文字既不是附图标记也不是图题（CAPTION 图层按 §8 计入允许项）%s"
                % (len(non_numeral), total,
                   "；例如：" + "、".join(non_numeral[:_MAX_DETAIL_ITEMS])
                   if non_numeral else "。")),
        hint="专利附图只允许几何、引线、附图标记与图题（契约 §8）："
             "把 layout.engineering_table 设为 false，并删掉零件名 / 说明文字。")

    # ---- 11. non_continuous -------------------------------------------------
    lim = int(th["non_continuous_max"])
    bad = sorted({"%s@%s(%s)" % (r.dxftype, r.layer, r.linetype)
                  for r in records if r.linetype not in ("CONTINUOUS",)})
    value = sum(1 for r in records if r.linetype not in ("CONTINUOUS",))
    out["non_continuous"] = Check(
        id="non_continuous", passed=value <= lim, value=value,
        threshold=_threshold_str("<=", lim),
        detail=("%d 个实体的有效线型不是 CONTINUOUS（BYLAYER 已按图层线型解析）%s"
                % (value, "；" + "、".join(bad[:_MAX_DETAIL_ITEMS]) if bad else "。")),
        hint="这一条不是 plan 的问题，是写出端的问题：sheet.py 必须把所有实体与图层写成 "
             "CONTINUOUS（契约 §5.4）。若线型来自导入的图块，请在 plan 的 source.exclude "
             "里排除对应零件。")

    # ---- 12. forbidden_text -------------------------------------------------
    compiled = []
    bad_patterns = []
    for pat in patterns:
        try:
            compiled.append((pat, re.compile(pat)))
        except re.error as exc:
            bad_patterns.append("%s(%s)" % (pat, exc))
    hits_txt = []
    for r in texts:
        s = r.text.strip()
        for pat, rx in compiled:
            if rx.search(s):
                hits_txt.append("%s@%s" % (s, r.layer))
                break
    hits_txt = sorted(set(hits_txt))
    value = len(hits_txt)
    detail = ("%d 条文字命中内部件号 / 图号形状（共 %d 条文字，%d 条模式）%s"
              % (value, len(texts), len(compiled),
                 "；命中：" + "、".join(hits_txt[:_MAX_DETAIL_ITEMS]) if hits_txt else "。"))
    if bad_patterns:
        detail += "；无法编译的模式：" + "、".join(bad_patterns)
    out["forbidden_text"] = Check(
        id="forbidden_text", passed=value == 0, value=value, threshold="==0",
        detail=detail,
        hint="plan 的 terms[].term 里出现了内部件号 / 图号形状的字符串，改成中文技术名词；"
             "并确认 layout.engineering_table 为 false（契约 §8 禁止附图出现件号）。")

    return [out[cid] for cid in CHECK_ORDER if cid in out]
