#!/usr/bin/env python3
"""figure-plan.json -> 附图 DXF + 预览 PNG + reference-numerals.json（impl-contract §6）。

用法：

    python3 scripts/render_patent_figure.py plan.json --assembly assembly.json -o out/ \\
            [--cache .cache/] [--preview] [--only fig2] [--json report.json]

本脚本是整条链路的桥：它把 plan（模型唯一的输出）与 assembly.json（几何事实）合成为
一张张确定性的附图。**任何版面常数都不经过模型**——字高、边槽、图题带、爆炸间隙、
标记位置、图面角度全部由 patent_figure.* 按 impl-contract §11 算出。

坐标系分工（§11.0）：``occ_backend`` 给出投影后的 2D 模型单位曲线；``layout`` 在模型单位里
排布；本脚本把结果乘上 ``fit_to_frame`` 的比例尺并平移进 180×250 的图框，此后一切都是毫米；
``labels`` 与 ``sheet`` 只见毫米。

退出码：0 全部图渲染并通过 QA；1 渲染失败 / plan 语义错误 / QA 闸门未通过；2 用法错误。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patent_figure import labels as _labels  # noqa: E402
from patent_figure import layout as _layout  # noqa: E402
from patent_figure import numbering as _numbering  # noqa: E402
from patent_figure import occ_backend as _occ  # noqa: E402
from patent_figure import plan as _plan  # noqa: E402
from patent_figure import qa as _qa  # noqa: E402
from patent_figure import sheet as _sheet  # noqa: E402

# =============================================================================== constants
# §7 rule 7: every output-affecting constant is named here once. The layout / label
# constants are IMPORTED, never restated — layout.py is their single definition point
# (§11.2.2) and a second copy here would silently decouple the renderer from the QA gate.

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: Rotation of the projected sheet is applied here rather than through ``ViewFrame.roll``
#: so that the HLR cache stays keyed on the un-rolled view (one HLR per part per view,
#: reused by every figure whatever its sheet angle). ``cos``/``sin`` are rounded to the
#: same 12 decimals ``labels._DIRS`` uses, for the same reason: libm's trig error is ~1 ULP
#: (1e-16) and rounding at 1e-12 absorbs it, so every platform gets the same matrix.
ROT_ROUND_DEC = _labels.DIR_ROUND_DEC

#: §7 rule 4 — round before comparing / before writing.
RANK_DEC = _layout.RANK_DEC

#: Tolerance of the rotation self-check, in degrees. ``roll_for_axis`` rounds its result to
#: RANK_DEC decimals, so the realised sheet angle can differ from the target by ~1e-9 deg.
AXIS_ANGLE_TOL_DEG = 1e-6

#: Named explode axes of ``layout.explode_axis`` (§4.2). ``auto`` resolves to
#: ``assembly.json:principal_axis.vector``.
AXIS_VECTORS: Dict[str, Tuple[float, float, float]] = {
    "x": (1.0, 0.0, 0.0),
    "y": (0.0, 1.0, 0.0),
    "z": (0.0, 0.0, 1.0),
}
AXIS_AUTO = "auto"

#: Cache-key prefix for a multi-shape (scene) HLR run. It is not a ``PartShape.key`` and
#: must never collide with one; ``#`` cannot start a part name coming out of XCAF.
SCENE_KEY_PREFIX = "#scene#"
SCENE_KEY_SEP = "|"

NUMERALS_SCHEMA = "patent-numerals/1"
SIDECAR_SCHEMA = "patent-figure-layout/1"
REPORT_SCHEMA = "patent-render/1"

#: Sidecar file name suffix. qa.py reads ``<dxf stem>.layout.json`` from the same directory
#: (§11.10 requirement 5); without it ``part_bbox_overlap_pairs`` degrades to a connected
#: cluster heuristic that cannot see two overlapping parts at all.
SIDECAR_SUFFIX = ".layout.json"

NUMERALS_FILENAME = "reference-numerals.json"

#: Preview resolution; ``sheet.render_preview``'s own default, named here so the CLI flag
#: and the function cannot drift.
PREVIEW_DPI = 150

#: Columns of the opt-in engineering table (§8: NO./NAME/QTY/REMARK).
ENGINEERING_REMARK = ""


class RenderError(RuntimeError):
    """A figure could not be produced. Carries the user-facing Chinese message."""

    def __init__(self, message: str, code: str = "E_RENDER") -> None:
        super().__init__(message)
        self.code = code


# =============================================================================== geometry


def _rotation(angle_deg: float) -> Tuple[float, float]:
    """``(cos, sin)`` of ``angle_deg``, rounded to :data:`ROT_ROUND_DEC` decimals."""
    rad = math.radians(float(angle_deg))
    return (round(math.cos(rad), ROT_ROUND_DEC), round(math.sin(rad), ROT_ROUND_DEC))


def _rotate(points: np.ndarray, cs: Tuple[float, float]) -> np.ndarray:
    """Rotate an ``(N, 2)`` array counter-clockwise by the given ``(cos, sin)``.

    Rolling the view by ``t`` maps a point's sheet coordinates to
    ``(X cos t - U sin t, X sin t + U cos t)`` — exactly this rotation — because the roll
    turns ``(x, u)`` inside the view plane and leaves the view direction ``w`` untouched.
    Visibility is therefore identical, which is what lets the HLR run once at roll 0.
    Written element-wise (never ``P @ R``): a BLAS gemv's reduction order varies by machine
    and float addition is not associative (§11.9 item 5).
    """
    c, s = cs
    p = np.asarray(points, dtype=np.float64)
    return np.stack((p[:, 0] * c - p[:, 1] * s, p[:, 0] * s + p[:, 1] * c), axis=-1)


def _to_mm(points: np.ndarray, scale: float, shift: Tuple[float, float]) -> np.ndarray:
    """Apply the single layout-units -> millimetres transform used by every consumer."""
    p = np.asarray(points, dtype=np.float64)
    return np.stack((p[:, 0] * scale + shift[0], p[:, 1] * scale + shift[1]), axis=-1)


def _axis_vector(layout_cfg: Dict[str, Any], assembly: dict) -> np.ndarray:
    """The 3D explode axis of one figure: a named axis, or the assembly's principal axis."""
    name = str(layout_cfg.get("explode_axis", AXIS_AUTO))
    if name in AXIS_VECTORS:
        return np.array(AXIS_VECTORS[name], dtype=np.float64)
    principal = assembly.get("principal_axis") if isinstance(assembly, dict) else None
    vector = principal.get("vector") if isinstance(principal, dict) else None
    if not (isinstance(vector, list) and len(vector) == 3):
        raise RenderError(
            "assembly.json 缺少 principal_axis.vector，无法解析 explode_axis=\"auto\"。"
            "修复：重新运行 analyze_assembly.py，或在 plan 的 layout.explode_axis 里写死 x/y/z。",
            code="E_NO_PRINCIPAL_AXIS")
    return np.array([float(v) for v in vector], dtype=np.float64)


def _sheet_axis(view: "_occ.ViewFrame", axis3d: np.ndarray) -> np.ndarray:
    """Project the 3D explode axis onto the (unrolled) sheet: ``(axis·x, axis·u)``."""
    a = np.asarray(axis3d, dtype=np.float64).reshape(3)
    return np.array([a[0] * view.x[0] + a[1] * view.x[1] + a[2] * view.x[2],
                     a[0] * view.u[0] + a[1] * view.u[1] + a[2] * view.u[2]],
                    dtype=np.float64)


# =============================================================================== HLR access


class CurveSource:
    """Per-part and per-scene HLR, memoised in process and (optionally) on disk.

    Everything is computed in the **unrolled** view (roll 0); the renderer rotates the 2D
    result itself. That is what makes one HLR run serve every figure and every candidate
    sheet angle — the cache key carries no angle at all.
    """

    def __init__(self, view: "_occ.ViewFrame", deflection: float,
                 cache: Optional["_occ.GeometryCache"]) -> None:
        self.view = view
        self.deflection = float(deflection)
        self.cache = cache
        self._memo: Dict[str, List[np.ndarray]] = {}
        self.hits = 0
        self.misses = 0

    def _lookup(self, key: str):
        if key in self._memo:
            self.hits += 1
            return self._memo[key]
        if self.cache is not None:
            cached = self.cache.get(key, self.view, self.deflection)
            if cached is not None:
                self.hits += 1
                self._memo[key] = cached
                return cached
        return None

    def _store(self, key: str, curves: List[np.ndarray]) -> List[np.ndarray]:
        self._memo[key] = curves
        if self.cache is not None:
            self.cache.put(key, self.view, self.deflection, curves)
        return curves

    def part(self, part: "_occ.PartShape") -> List[np.ndarray]:
        """Per-part HLR. Legal only where the part is disjoint from the rest on the sheet.

        The renderer uses it for two things and nothing else:

        * as the **measurement** input to ``layout_*`` (footprints, bboxes, extents) — a
          measurement is never wrong for being un-occluded, it is merely an upper bound;
        * as the **drawn** geometry of an exploded body that holds a single member, which
          §11.3 S8's postcondition P1 (placed body bboxes are pairwise disjoint) proves is
          exactly the disjointness ``part_curves`` requires.

        It is NEVER the drawn geometry of a ``kind="assembly"`` figure, nor of a
        multi-member body: there the parts occlude each other and ``scene`` is required.
        """
        found = self._lookup(part.key)
        if found is not None:
            return found
        self.misses += 1
        return self._store(part.key, _occ.part_curves(part, self.view, self.deflection))

    def scene(self, parts: Sequence["_occ.PartShape"]) -> List[np.ndarray]:
        """Global HLR over several parts as one scene, so they occlude each other."""
        keys = sorted(p.key for p in parts)
        key = SCENE_KEY_PREFIX + SCENE_KEY_SEP.join(keys)
        found = self._lookup(key)
        if found is not None:
            return found
        self.misses += 1
        visible, _hidden = _occ.scene_curves(parts, self.view, self.deflection, False)
        return self._store(key, visible)


# =============================================================================== §11.6.3


def solve_figure(make_pieces, density: str, labelled_keys: Sequence[str], n_labels: int,
                 n_pieces: int, figure_id: str, kind: str, axis_angle: Any) -> Dict[str, Any]:
    """§11.6.3 descending text-height scan. Returns the accepted tier's full solution.

    ``make_pieces(angle_deg_or_None)`` rebuilds the ``Piece`` list at a given sheet angle
    and returns ``(pieces, axis2d)``; ``None`` means "no rotation" and is what an assembly
    figure gets. Rebuilding rather than mutating is deliberate: the rotation changes the
    curves themselves, and ``layout_exploded`` writes ``Piece.offset`` in place.

    The scan removes the h <-> margin <-> scale <-> h circular dependency by walking the
    finite ordered ``TEXT_SERIES`` downwards and taking the first self-consistent tier
    (§11.6.3): at most three steps, always terminating, never oscillating.

    ``axis_angle`` is either the plan's explicit number or the string ``"auto"``. Under
    ``"auto"`` the angle is solved per figure and per tier in closed form (§4.2's architect
    ruling): a probe layout at ``AXIS_ANGLE_DEFAULT`` — the constant's documented role as
    the fallback starting point — yields ``diagnostics["axis_angle_opt"]``, and the figure
    is then laid out at that angle. Exactly two layouts per tier, no iteration.

    Raises ``layout.LayoutError`` carrying the P7 (sheet fill) or P8 (text height) message.
    """
    last = None
    fill_fail = None
    auto = not isinstance(axis_angle, (int, float)) or isinstance(axis_angle, bool)

    for h in _layout.TEXT_SERIES:  # 7.0 -> 5.0 -> 3.5
        lm = _layout.label_margin_mm(h)
        cb = _layout.caption_band_mm(h)
        aw = _layout.FRAME_W - 2.0 * lm
        ah = _layout.FRAME_H - lm - cb
        if aw <= 0.0 or ah <= 0.0:
            continue

        if kind == "assembly":
            # No explode axis, no rotation: parts keep their projected positions and the
            # occlusion between them is resolved by the scene HLR, not by displacement.
            pieces, _axis2d = make_pieces(None)
            result = _layout.layout_assembly(pieces)
            angle_used = None
        else:
            alpha = aw / ah
            if auto:
                probe_pieces, probe_axis = make_pieces(float(_layout.AXIS_ANGLE_DEFAULT))
                probe = _layout.layout_exploded(probe_pieces, probe_axis, density,
                                                sheet_aspect=alpha, max_rows=1)
                angle_used = float(probe.diagnostics["axis_angle_opt"])
            else:
                angle_used = float(axis_angle)
            pieces, axis2d = make_pieces(angle_used)
            result = _layout.layout_exploded(pieces, axis2d, density,
                                             sheet_aspect=alpha, max_rows=1)

        s = _layout.fit_to_frame(result, aw, ah, margin=0.0)

        # (1) page-fill gate — layout cannot judge it, it does not know millimetres.
        # The denominator is the USABLE area, not the whole frame (ruling D2).
        gw = float(result.hi[0] - result.lo[0]) * s
        gh = float(result.hi[1] - result.lo[1]) * s
        sheet_fill = round(gw * gh / (aw * ah), RANK_DEC)

        # (2) text height self-consistency.
        ext = result.diagnostics["extent_by_key"]
        keys = sorted(labelled_keys)  # sorted, never iterate a set
        slot_lab_model = min(ext[k] for k in keys)
        slot_sheet = s * slot_lab_model
        raw = _labels.text_height_for(slot_sheet)  # = TEXT_RATIO * slot_sheet
        h_snap = _layout.snap_text_height(raw)
        floor_override = False
        if h_snap is None and round(slot_sheet, RANK_DEC) >= round(_layout.SLOT_FLOOR_MM,
                                                                  RANK_DEC):
            # 0.45*slot has dropped below 3.5 but 3.5/slot is still <= 0.6, so QA's
            # text_slot_ratio still passes. Use the whole envelope rather than waste it.
            h_snap, floor_override = _layout.TEXT_FLOOR_MM, True

        last = (h, sheet_fill, slot_sheet, raw, result.diagnostics.get("axis_angle_opt"))
        if h_snap is not None and h <= h_snap + 10 ** (-RANK_DEC):
            if sheet_fill < _layout.SHEET_FILL_MIN:
                # Not fatal here: a smaller tier has a larger usable area and a slightly
                # different alpha. Remember the first otherwise-valid tier and report after.
                if fill_fail is None:
                    fill_fail = (sheet_fill, gw, gh,
                                 result.diagnostics.get("axis_angle_opt"),
                                 result.diagnostics.get("axis_angle_deg"))
                continue
            result.diagnostics["slot_labelled"] = slot_lab_model
            result.diagnostics["text_height_mm"] = h
            result.diagnostics["text_height_floor_override"] = floor_override
            result.diagnostics["sheet_fill"] = sheet_fill
            return {"result": result, "pieces": result.pieces, "scale": s, "text_height": h,
                    "aw": aw, "ah": ah, "label_margin": lm, "caption_band": cb,
                    "axis_angle": angle_used, "sheet_fill": sheet_fill,
                    "floor_override": floor_override}

    if fill_fail is not None:
        sf, gw, gh, opt, cur = fill_fail
        # Whether rotation can save the figure is exactly whether the closed-form optimum
        # differs from the current angle: for near-square geometry the aspect ratio is
        # rotation invariant and "change axis_angle" would send the user in circles.
        rotatable = (kind != "assembly" and opt is not None and cur is not None
                     and abs(float(opt) - float(cur)) >= _layout.ANGLE_GRID_DEG)
        fix = ("把 layout.axis_angle 改为 %s（当前 %s，本图闭式最优）；或 density 改 compact；"
               % (opt, cur)) if rotatable else \
              ("本图几何的长宽比旋转也救不回来（当前角已是最优）：换一个 layout.view；")
        raise _layout.LayoutError(
            "figure %s: 页面填充 %.3f < %.2f（几何 %.1f×%.1f mm，长宽比 %.3f，可用区长宽比 %.3f）。"
            "修复（按推荐顺序）：%s或按 assembly.json:split_suggestions 拆图。"
            % (figure_id, sf, _layout.SHEET_FILL_MIN, gw, gh, gw / gh,
               (_layout.FRAME_W - 2.0 * _layout.label_margin_mm(_layout.TEXT_FLOOR_MM))
               / (_layout.FRAME_H - _layout.label_margin_mm(_layout.TEXT_FLOOR_MM)
                  - _layout.caption_band_mm(_layout.TEXT_FLOOR_MM)),
               fix))

    if last is None:
        raise _layout.LayoutError("figure %s: 任何 GB 字号档下可用区都为空（FRAME 常量被改坏了？）"
                                  % figure_id)
    h0, _fill0, slot0, raw0, axis_opt0 = last
    raise _layout.LayoutError(
        "figure %s: 本图最大可用字高 %.2f mm < 交付下限 %.1f mm"
        "（被标注件最小图面外廓 %.2f mm，需 >= %.2f mm）。"
        "本图含 %d 个零件实例 / %d 个附图标记——**标记数没有超上限，是件数超了**，"
        "所以 E_TOO_MANY_LABELS 不会触发。修复（按推荐顺序）："
        "(a) 对标准件设 label:\"none\"；(b) 按 assembly.json:split_suggestions 拆图；"
        "(c) density 改 compact；(d) axis_angle 改为 %s。"
        % (figure_id, raw0, _layout.TEXT_FLOOR_MM, slot0, _layout.SLOT_FLOOR_MM,
           n_pieces, n_labels, axis_opt0))


# =============================================================================== labels


def _anchor_hint(curves_mm: Sequence[np.ndarray], direction: Tuple[float, float]):
    """§11.7.2's fixed recipe for a label anchor that really sits on the outline.

    Of every sampled point of the part's own placed curves, take the one whose projection
    along ``direction`` is largest; ties break on the lexicographically smallest
    ``(round(x, 9), round(y, 9))``. The point is therefore on the true silhouette, not on
    the bounding box — which for an L- or U-shaped part can hang in empty space.
    """
    dx, dy = direction
    best = None
    for arr in curves_mm:
        a = np.asarray(arr, dtype=np.float64)
        for i in range(a.shape[0]):
            x = float(a[i, 0])
            y = float(a[i, 1])
            rank = (round(-(x * dx + y * dy), RANK_DEC), round(x, RANK_DEC), round(y, RANK_DEC))
            if best is None or rank < best[0]:
                best = (rank, (x, y))
    if best is None:
        return None
    return np.array(best[1], dtype=np.float64)


def _build_requests(labelled: Sequence[Tuple[str, int]], boxes: Dict[str, Tuple[np.ndarray, np.ndarray]],
                    curves_by_key: Dict[str, List[np.ndarray]]) -> List["_labels.LabelRequest"]:
    """Build the label requests in a canonical order.

    ``labelled`` is ``(instance key, numeral)`` pairs. They are sorted on ``(numeral, key)``
    rather than on numeral alone: under ``label: "all"`` several instances share one numeral,
    and ``place_labels``' greedy order key ends in ``numeral``, so it is not a total order
    for them. Sorting here keeps the input permutation from ever reaching labels.py.
    """
    ordered = sorted(labelled, key=lambda item: (int(item[1]), str(item[0])))
    # Figure centroid, in the same fixed-order Python sum labels.py's _pref_dir uses.
    n = len(ordered)
    cx = sum(round(0.5 * (float(boxes[k][0][0]) + float(boxes[k][1][0])), RANK_DEC)
             for k, _ in ordered) / n
    cy = sum(round(0.5 * (float(boxes[k][0][1]) + float(boxes[k][1][1])), RANK_DEC)
             for k, _ in ordered) / n
    out: List["_labels.LabelRequest"] = []
    for key, numeral in ordered:
        lo, hi = boxes[key]
        px = 0.5 * (float(lo[0]) + float(hi[0])) - cx
        py = 0.5 * (float(lo[1]) + float(hi[1])) - cy
        norm = math.hypot(px, py)
        if round(norm, RANK_DEC) <= 0.0:
            direction = (1.0, 0.0)  # the part sits on the centroid: +x, deterministic
        else:
            direction = (px / norm, py / norm)
        curves = curves_by_key.get(key, [])
        outline = (np.vstack([np.asarray(a, dtype=np.float64) for a in curves])
                   if curves else None)
        out.append(_labels.LabelRequest(
            key=key, numeral=int(numeral), lo=lo, hi=hi,
            anchor_hint=_anchor_hint(curves, direction),
            outline=outline))
    return out


# =============================================================================== one figure


def render_figure(figure: dict, layout_cfg: Dict[str, Any], parts: Sequence["_occ.PartShape"],
                  assembly: dict, num: "_numbering.Numbering", out_dir: Path,
                  sources: Dict[str, CurveSource], deflection: float,
                  cache: Optional["_occ.GeometryCache"], preview: bool) -> Dict[str, Any]:
    """Render one figure: geometry -> layout -> labels -> DXF + sidecar (+ PNG).

    Returns a diagnostics dict. Raises ``RenderError`` / ``LayoutError`` / ``LabelError``.
    """
    figure_id = str(figure.get("id"))
    kind = str(figure.get("kind"))
    caption = str(figure.get("caption", ""))
    density = str(layout_cfg.get("density", "normal"))
    view_name = str(layout_cfg.get("view", "iso"))
    if view_name not in _occ.VIEWS:
        raise RenderError("figure %s: 未知视图 %r，可选 %s"
                          % (figure_id, view_name, ", ".join(sorted(_occ.VIEWS))),
                          code="E_UNKNOWN_ENUM")
    az, el = _occ.VIEWS[view_name]
    view = _occ.ViewFrame(az, el, 0.0)
    # One CurveSource per (view, deflection) for the WHOLE run, not per figure: two figures
    # drawn from the same view share every part's HLR, so an 11-part assembly is projected
    # once whether it appears in one figure or ten, with or without a disk cache.
    source = sources.get(view.key)
    if source is None:
        source = CurveSource(view, deflection, cache)
        sources[view.key] = source
    hits0, misses0 = source.hits, source.misses  # snapshot: report this figure's own share

    if not parts:
        raise RenderError("figure %s: members 没有选中任何零件实例" % figure_id,
                          code="E_MEMBER_NO_MATCH")

    # ---- projected curves, once, in the unrolled view -------------------------------
    base_curves: Dict[str, List[np.ndarray]] = {}
    for part in parts:
        curves = source.part(part)
        if not curves:
            raise RenderError(
                "figure %s: 零件 %s 的 HLR 没有产出任何曲线（上游可能静默丢了它的边）。"
                "修复：在 plan 的 source.exclude 里排除它，或改 layout.view。"
                % (figure_id, part.key), code="E_NO_CURVES")
        base_curves[part.key] = curves

    axis3d = _axis_vector(layout_cfg, assembly)
    axis2d_0 = _sheet_axis(view, axis3d)

    def make_pieces(angle_deg):
        """Rebuild the pieces at a given sheet angle (``None`` = unrolled)."""
        if angle_deg is None:
            rot = 0.0
        else:
            try:
                rot = _occ.roll_for_axis(axis3d, az, el, float(angle_deg))
            except ValueError as exc:
                raise RenderError("figure %s: %s" % (figure_id, exc), code="E_AXIS_PARALLEL")
        cs = _rotation(rot)
        pieces = [_layout.Piece(key=p.key, name=p.name,
                                curves=[_rotate(c, cs) for c in base_curves[p.key]])
                  for p in parts]
        axis2d = _rotate(axis2d_0.reshape(1, 2), cs).reshape(2)
        if angle_deg is not None:
            # Self-check: a sign flip in the rotation would be invisible in the output but
            # would put the string on the wrong diagonal. One assert kills it forever.
            got = math.degrees(math.atan2(float(axis2d[1]), float(axis2d[0]))) % 360.0
            want = float(angle_deg) % 360.0
            delta = min(abs(got - want), 360.0 - abs(got - want))
            if delta > AXIS_ANGLE_TOL_DEG:
                raise RenderError(
                    "figure %s: 图面轴角自检失败（目标 %.6f°，实测 %.6f°）——旋转符号错误，"
                    "这是 render_patent_figure.py 的实现错误，不是 plan 的问题。"
                    % (figure_id, want, got), code="E_INTERNAL")
        return pieces, axis2d

    # ---- which instances carry a numeral --------------------------------------------
    keys_by_name: Dict[str, List[str]] = {}
    for part in parts:
        keys_by_name.setdefault(part.name, []).append(part.key)
    labelled: List[Tuple[str, int]] = []
    for name in sorted(keys_by_name):
        numeral = num.numeral_of(name)
        if numeral is None:
            continue
        for key in _numbering.keys_to_label(num.label_mode(name), keys_by_name[name]):
            labelled.append((key, int(numeral)))
    labelled.sort(key=lambda item: (int(item[1]), str(item[0])))
    labelled_keys = [k for k, _n in labelled]

    # ---- text height / scale / sheet angle (§11.6.3) ---------------------------------
    solved = solve_figure(
        make_pieces, density,
        labelled_keys if labelled_keys else [p.key for p in parts],
        len(labelled), len(parts), figure_id, kind,
        layout_cfg.get("axis_angle", _plan.AXIS_ANGLE_AUTO))

    result = solved["result"]
    pieces = solved["pieces"]
    scale = float(solved["scale"])
    h = float(solved["text_height"])
    aw, ah = solved["aw"], solved["ah"]
    lm, cb = solved["label_margin"], solved["caption_band"]

    # ---- the one layout-units -> millimetres transform -------------------------------
    # The usable area sits inside the frame with the label margin on the left/right/top and
    # the caption band at the bottom; the geometry is centred in it. The frame origin is the
    # frame's lower-left corner (§11.6.3 step 2).
    gw = float(result.hi[0] - result.lo[0]) * scale
    gh = float(result.hi[1] - result.lo[1]) * scale
    shift = (lm + 0.5 * (aw - gw) - float(result.lo[0]) * scale,
             cb + 0.5 * (ah - gh) - float(result.lo[1]) * scale)
    sheet_lo = (0.0, 0.0)
    sheet_hi = (_layout.FRAME_W, _layout.FRAME_H)

    pieces_by_key = {p.key: p for p in pieces}
    parts_by_key = {p.key: p for p in parts}

    # ---- drawn geometry --------------------------------------------------------------
    # HLR granularity (§11.3, "关于 HLR 粒度"): occlusion must be computed over exactly the
    # set of shapes that can overlap on the sheet.
    #   * kind="assembly"  -> ONE scene_curves over every member. Per-part HLR here would
    #     draw hidden edges straight through whatever stands in front of them.
    #   * kind="exploded"  -> one scene_curves PER BODY. Inside a body (a bolt circle) the
    #     members may overlap, so they must be projected together; between bodies the
    #     postcondition P1 asserted inside layout_exploded (placed body bboxes pairwise
    #     disjoint, verified there, not assumed here) makes per-body HLR equivalent to one
    #     global run — that is the licence for per-part HLR, and it is earned, not assumed.
    rot_cs = _rotation(0.0 if solved["axis_angle"] is None
                       else _occ.roll_for_axis(axis3d, az, el, float(solved["axis_angle"])))
    geometry: List[np.ndarray] = []
    if kind == "assembly":
        assert all(np.all(p.offset == 0.0) for p in pieces), \
            "layout_assembly 必须保持零位移；非零 offset 说明上游被改坏了"
        for curve in source.scene(parts):
            geometry.append(_to_mm(_rotate(curve, rot_cs), scale, shift))
    else:
        for body in result.diagnostics["body_boxes"]:
            members = list(body["members"])
            offset = pieces_by_key[members[0]].offset
            if len(members) == 1:
                # Single-member body: P1 guarantees it is disjoint from every other body on
                # the sheet, which is precisely part_curves' documented precondition.
                curves = pieces_by_key[members[0]].placed()
            else:
                members_parts = [parts_by_key[k] for k in sorted(members)]
                curves = [_rotate(c, rot_cs) + offset for c in source.scene(members_parts)]
            for curve in curves:
                geometry.append(_to_mm(curve, scale, shift))
    if not geometry:
        raise RenderError("figure %s: 没有任何可绘制的几何" % figure_id, code="E_NO_CURVES")

    # ---- label requests --------------------------------------------------------------
    boxes_mm: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    curves_mm: Dict[str, List[np.ndarray]] = {}
    for piece in pieces:
        placed = [_to_mm(c, scale, shift) for c in piece.placed()]
        curves_mm[piece.key] = placed
        stacked = np.vstack(placed)
        boxes_mm[piece.key] = (stacked.min(axis=0), stacked.max(axis=0))

    placements: List["_labels.LabelPlacement"] = []
    requests: List["_labels.LabelRequest"] = []
    if labelled:
        requests = _build_requests(labelled, boxes_mm, curves_mm)
        placements = _labels.place_labels(requests, obstacles=geometry, text_height=h,
                                          sheet_lo=sheet_lo, sheet_hi=sheet_hi)

    # ---- sidecar (must exist before QA runs; §11.10 requirement 5) --------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    dxf_path = out_dir / (figure_id + ".dxf")
    sidecar = {
        "schema": SIDECAR_SCHEMA,
        "figure": figure_id,
        "kind": kind,
        "units": "mm",
        "scale": round(scale, RANK_DEC),
        "text_height_mm": h,
        "usable_area_mm": [round(aw, RANK_DEC), round(ah, RANK_DEC)],
        "body_boxes": [{
            "key": str(body["key"]),
            "lo": [round(float(body["lo"][0]) * scale + shift[0], RANK_DEC),
                   round(float(body["lo"][1]) * scale + shift[1], RANK_DEC)],
            "hi": [round(float(body["hi"][0]) * scale + shift[0], RANK_DEC),
                   round(float(body["hi"][1]) * scale + shift[1], RANK_DEC)],
            "members": list(body["members"]),
        } for body in result.diagnostics["body_boxes"]],
    }
    (out_dir / (figure_id + SIDECAR_SUFFIX)).write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---- DXF -------------------------------------------------------------------------
    caption_height = _layout.CAPTION_RATIO * h
    _sheet.write_figure(dxf_path, geometry=geometry, hidden=[], labels=placements,
                        caption=caption, text_height=h, caption_height=caption_height,
                        engineering_rows=None)

    engineering_path = None
    if bool(layout_cfg.get("engineering_table", False)):
        # §8: opt-in review copy only, and it must be a DIFFERENT file — the filing copy
        # written above never carries a table. sheet.write_figure enforces the name.
        engineering_path = out_dir / (figure_id + _sheet.ENGINEERING_SUFFIX + ".dxf")
        _sheet.write_figure(engineering_path, geometry=geometry, hidden=[],
                            labels=placements, caption=caption, text_height=h,
                            caption_height=caption_height,
                            engineering_rows=_engineering_rows(labelled, num, parts))

    png_path = None
    if preview:
        png_path = out_dir / (figure_id + ".png")
        _sheet.render_preview(dxf_path, png_path, dpi=PREVIEW_DPI)

    drawn = np.vstack(geometry)
    return {
        "id": figure_id,
        "kind": kind,
        "dxf": str(dxf_path),
        "png": None if png_path is None else str(png_path),
        "engineering_dxf": None if engineering_path is None else str(engineering_path),
        "sidecar": str(out_dir / (figure_id + SIDECAR_SUFFIX)),
        "view": view_name,
        "density": density,
        "axis_angle": solved["axis_angle"],
        "axis_angle_opt": result.diagnostics.get("axis_angle_opt"),
        "scale": round(scale, RANK_DEC),
        "text_height_mm": h,
        "text_height_floor_override": solved["floor_override"],
        "caption_height_mm": round(caption_height, RANK_DEC),
        "sheet_fill": solved["sheet_fill"],
        "sheet_fill_drawn": round(
            float(drawn[:, 0].max() - drawn[:, 0].min())
            * float(drawn[:, 1].max() - drawn[:, 1].min()) / (aw * ah), RANK_DEC),
        "slot_labelled": result.diagnostics.get("slot_labelled"),
        "bodies": len(result.diagnostics["body_boxes"]),
        "instances": len(parts),
        "labels": len(placements),
        # §11.7.2 requires the renderer to flag any label that fell back to the AABB ray
        # anchor, which for a concave part can hang outside the body.
        "anchor_hint_missing": sorted(r.key for r in requests if r.anchor_hint is None),
        "hlr_reused": source.hits - hits0,
        "hlr_computed": source.misses - misses0,
    }


def _engineering_rows(labelled: Sequence[Tuple[str, int]], num: "_numbering.Numbering",
                      parts: Sequence["_occ.PartShape"]) -> List[tuple]:
    """NO./NAME/QTY/REMARK rows for the opt-in review copy, in numeral order.

    NAME is the plan's Chinese term, never the internal part name: §8 forbids internal part
    codes on any sheet this toolchain writes, and the review copy's job is to map a numeral
    to a human-readable noun, which the term already is.
    """
    counts: Dict[int, int] = {}
    terms: Dict[int, str] = {}
    for key, numeral in sorted(labelled, key=lambda item: (int(item[1]), str(item[0]))):
        counts[numeral] = counts.get(numeral, 0) + 1
        name = key.rsplit(_numbering.INSTANCE_KEY_SEP, 1)[0]
        terms.setdefault(numeral, num.term_of(name) or "")
    return [(str(n), terms[n], str(counts[n]), ENGINEERING_REMARK)
            for n in sorted(counts)]


# =============================================================================== numerals


def _numeral_figures(plan_doc: dict, assembly: dict, num: "_numbering.Numbering") -> Dict[int, List[str]]:
    """Which figures actually label each numeral, over EVERY figure of the plan.

    Computed from the plan and the assembly alone — not from what ``--only`` rendered — so
    ``reference-numerals.json`` is a property of the plan and does not change when a single
    figure is re-rendered.
    """
    selected = _plan.selected_part_names(plan_doc, assembly)
    figures = plan_doc.get("figures") if isinstance(plan_doc, dict) else None
    figures = figures if isinstance(figures, list) else []
    out: Dict[int, List[str]] = {}
    for fig in figures:
        if not isinstance(fig, dict):
            continue
        fig_id = str(fig.get("id", ""))
        for name in _plan.figure_members(fig, selected):
            numeral = num.numeral_of(name)
            if numeral is None or num.label_mode(name) == "none":
                continue
            bucket = out.setdefault(int(numeral), [])
            if fig_id not in bucket:
                bucket.append(fig_id)
    return out


def write_numerals(path: Path, num: "_numbering.Numbering",
                   numeral_figures: Dict[int, List[str]]) -> Tuple[dict, List[str]]:
    """Write ``reference-numerals.json`` (§4.3). Returns ``(document, warnings)``.

    §4.2: a part whose term is ``label: "none"`` everywhere enters the global table only if
    some other figure labels it. ``numbering.assign`` cannot apply that rule — it never sees
    the figures — so the filter belongs here. Numerals are NOT renumbered: they are frozen by
    ``terms`` order (§5.6), so dropping a term leaves a gap, and the gap is reported as a
    warning whose fix is to delete the unused term from the plan.
    """
    kept = [entry for entry in num.entries if numeral_figures.get(entry.numeral)]
    dropped = [entry for entry in num.entries if not numeral_figures.get(entry.numeral)]
    doc = {
        "schema": NUMERALS_SCHEMA,
        "numerals": [{
            "numeral": entry.numeral,
            "term": entry.term,
            "selector": entry.selector,
            "figures": list(numeral_figures.get(entry.numeral, [])),
        } for entry in kept],
        # Reuse numbering.py's own sentence formatter over the filtered rows: the numerals
        # each entry carries are preserved, and the "附图标记说明" wording stays defined once.
        "description_zh": _numbering.Numbering(kept, num.part_names).description_zh(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # A term is dropped from the numeral table when no figure ended up labelling it. Whether
    # that deserves a warning depends entirely on WHY:
    #
    #   label: "none"   the author said "this is a standard part, give it no numeral". Dropping
    #                   it is the requested outcome, so warning about it — and telling them to
    #                   delete the term — asks them to undo what they just asked for. The
    #                   acceptance run caught exactly that: a model was told "紧固螺钉这类标准件
    #                   不用标号", set label:"none" as designed, was warned, and deleted the term.
    #                   It stays a note so the numbering is still explainable, with no fix line.
    #
    #   once / all      the author DID want a numeral and no figure delivered one. That is the
    #                   real accident: the numbering now has a hole. Warn, and say how to fix it.
    warnings = []
    for entry in dropped:
        if entry.label == "none":
            warnings.append(
                "term %d（%s，selector %r）按 label:\"none\" 不发标记，未计入 %s。"
                "这是计划里要求的结果，无需处理。"
                % (entry.numeral, entry.term, entry.selector, NUMERALS_FILENAME))
            continue
        warnings.append(
            "term %d（%s，selector %r，label:%s）在任何一张图上都没有被标注，已从 %s 中略去，"
            "标记号因此出现空号。修复：把它从 plan 的 terms 里删掉，"
            "或让某张图标注它，或把它的 label 改成 \"none\"（若它本就是标准件）。"
            % (entry.numeral, entry.term, entry.selector, entry.label, NUMERALS_FILENAME))
    return doc, warnings


# =============================================================================== CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="render_patent_figure.py",
        description="按 figure-plan.json 渲染专利附图：DXF + 预览 PNG + reference-numerals.json，"
                    "并对每张图跑 QA 闸门。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 scripts/render_patent_figure.py plan.json --assembly assembly.json "
               "-o out/ --cache .cache/ --preview\n"
               "  python3 scripts/render_patent_figure.py plan.json --assembly assembly.json "
               "-o out/ --only fig2\n\n"
               "产物：out/<图号>.dxf、out/<图号>.png（--preview）、out/<图号>.layout.json"
               "（qa 的 part_bbox_overlap 依赖它）、out/reference-numerals.json。\n"
               "版面全部由脚本算出：plan 里没有、也不接受任何坐标、字高或标记号。\n"
               "退出码：0 全部通过；1 渲染失败或 QA 未通过；2 用法错误。")
    p.add_argument("plan", help="figure-plan.json 路径")
    p.add_argument("--assembly", required=True, help="analyze_assembly.py 产出的 assembly.json")
    p.add_argument("-o", "--out", required=True, help="输出目录")
    p.add_argument("--cache", default=None,
                   help="HLR 几何缓存目录（跨次运行复用；不给则只在本进程内复用）")
    p.add_argument("--preview", action="store_true", help="额外渲染 out/<图号>.png")
    p.add_argument("--only", default=None, help="只渲染这一张图（figures[].id）")
    p.add_argument("--json", dest="json_out", default=None, help="把渲染 + QA 报告写到这个文件")
    p.add_argument("--deflection", type=float, default=_occ.DEFAULT_DEFLECTION,
                   help="HLR 弦高（默认 %(default)s，模型单位）")
    return p


def _fail(message: str) -> None:
    sys.stderr.write(message.rstrip("\n") + "\n")


def _print_qa_failures(figure_id: str, report: dict) -> None:
    for check in report.get("checks", []):
        if check.get("pass"):
            continue
        _fail("  [%s] %s  值 %s，阈值 %s" % (figure_id, check.get("id"),
                                             check.get("value"), check.get("threshold")))
        detail = str(check.get("detail", "")).strip()
        hint = str(check.get("hint", "")).strip()
        if detail:
            _fail("      详情：%s" % detail)
        if hint:
            _fail("      修复：%s" % hint)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    plan_path = Path(args.plan)
    assembly_path = Path(args.assembly)
    out_dir = Path(args.out)
    if not plan_path.is_file():
        _fail("用法错误：找不到 plan 文件 %s" % plan_path)
        return EXIT_USAGE
    if not assembly_path.is_file():
        _fail("用法错误：找不到 assembly.json %s" % assembly_path)
        return EXIT_USAGE
    try:
        assembly = json.loads(assembly_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _fail("用法错误：assembly.json 解析失败：%s" % exc)
        return EXIT_USAGE
    if not isinstance(assembly, dict) or not isinstance(assembly.get("parts"), list):
        _fail("用法错误：%s 不是 analyze_assembly.py 产出的 assembly.json" % assembly_path)
        return EXIT_USAGE

    # --- plan: schema then semantics. The plan is the object under test, so its own
    # defects are exit 1 (a failure), matching validate_figure_plan.py.
    try:
        plan_doc = _plan.load_plan(plan_path)
    except _plan.PlanError as exc:
        _fail(_plan.format_issues(exc.issues))
        return EXIT_FAIL
    issues = _plan.validate(plan_doc, assembly)
    text = _plan.format_issues(issues)
    if text.strip():
        _fail(text)
    if _plan.has_errors(issues):
        _fail("plan 语义检查未通过，未渲染任何图。")
        return EXIT_FAIL
    plan_doc = _plan.apply_defaults(plan_doc)

    # --- STEP: exists, and matches the assembly.json this plan is being validated against.
    raw_step = str(plan_doc.get("source", {}).get("step", ""))
    step = Path(raw_step)
    if not step.is_file():
        alt = plan_path.parent / raw_step
        if alt.is_file():
            step = alt
    if not step.is_file():
        _fail("用法错误：plan 的 source.step 指向的文件不存在：%s" % raw_step)
        return EXIT_USAGE
    step_sha = _occ.file_sha256(step)
    expected = str(assembly.get("source", {}).get("sha256", ""))
    if expected and expected != step_sha:
        _fail("STEP 与 assembly.json 不一致：%s 的 sha256 是 %s，"
              "而 assembly.json 记录的是 %s。修复：重新运行 analyze_assembly.py。"
              % (step, step_sha[:12], expected[:12]))
        return EXIT_FAIL

    figures = [f for f in plan_doc.get("figures", []) if isinstance(f, dict)]
    if args.only is not None:
        ids = [str(f.get("id")) for f in figures]
        if args.only not in ids:
            _fail("用法错误：--only %s 不在 plan 的图号里（可选 %s）"
                  % (args.only, ", ".join(ids)))
            return EXIT_USAGE
        figures = [f for f in figures if str(f.get("id")) == args.only]

    try:
        all_parts = _occ.load_assembly(step)
    except _occ.OccBackendError as exc:
        _fail("读取 STEP 失败：%s" % exc)
        return EXIT_FAIL

    selected = _plan.selected_part_names(plan_doc, assembly)
    num = _plan.numbering_for(plan_doc, assembly)
    cache = None
    if args.cache:
        cache = _occ.GeometryCache(Path(args.cache), step_sha)

    out_dir.mkdir(parents=True, exist_ok=True)
    numeral_figures = _numeral_figures(plan_doc, assembly, num)
    numerals_doc, numeral_warnings = write_numerals(
        out_dir / NUMERALS_FILENAME, num, numeral_figures)
    for warning in numeral_warnings:
        _fail("警告：%s" % warning)

    sources: Dict[str, CurveSource] = {}
    rendered: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    ok = True
    for figure in figures:
        figure_id = str(figure.get("id"))
        layout_cfg = _plan.figure_layout(plan_doc, figure)
        members = _plan.figure_members(figure, selected)
        member_names = set(members)
        parts = [p for p in all_parts if p.name in member_names]
        try:
            info = render_figure(figure, layout_cfg, parts, assembly, num, out_dir,
                                 sources, float(args.deflection), cache, bool(args.preview))
        except _labels.LabelError as exc:
            ok = False
            errors.append({"figure": figure_id, "code": "E_LABELS_UNPLACEABLE",
                           "message": str(exc),
                           "hint": "见 assembly.json:split_suggestions —— 拆图，"
                                   "或对标准件设 label:\"none\"。"})
            _fail("figure %s 渲染失败 [E_LABELS_UNPLACEABLE]：%s" % (figure_id, exc))
            continue
        except _layout.LayoutError as exc:
            ok = False
            errors.append({"figure": figure_id, "code": "E_LAYOUT_UNSOLVABLE",
                           "message": str(exc), "hint": ""})
            _fail("figure %s 渲染失败 [E_LAYOUT_UNSOLVABLE]：%s" % (figure_id, exc))
            continue
        except (RenderError, _occ.OccBackendError, _sheet.SheetError) as exc:
            ok = False
            code = getattr(exc, "code", "E_RENDER")
            errors.append({"figure": figure_id, "code": code, "message": str(exc), "hint": ""})
            _fail("figure %s 渲染失败 [%s]：%s" % (figure_id, code, exc))
            continue

        report = _qa.check_figure(Path(info["dxf"]), kind=info["kind"])
        info["qa"] = report
        rendered.append(info)
        qa_path = out_dir / (figure_id + ".qa.json")
        qa_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        status = "通过" if report.get("pass") else "未通过"
        print("figure %s：%s，字高 %.1f mm，比例 %.4f，图面角 %s，%d 件 / %d 标记，QA %s"
              % (figure_id, Path(info["dxf"]).name, info["text_height_mm"], info["scale"],
                 info["axis_angle"], info["instances"], info["labels"], status))
        if not report.get("pass"):
            ok = False
            _fail("figure %s 的 QA 闸门未通过：" % figure_id)
            _print_qa_failures(figure_id, report)

    if args.json_out:
        payload = {
            "schema": REPORT_SCHEMA,
            "plan": str(plan_path),
            "assembly": str(assembly_path),
            "step": str(step),
            "step_sha256": step_sha,
            "out": str(out_dir),
            "pass": ok,
            "figures": rendered,
            "errors": errors,
            "numerals": numerals_doc,
            "warnings": numeral_warnings,
        }
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("共 %d 张图，%s；附图标记说明已写入 %s"
          % (len(rendered), "全部通过" if ok else "有未通过项",
             out_dir / NUMERALS_FILENAME))
    return EXIT_OK if ok else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
