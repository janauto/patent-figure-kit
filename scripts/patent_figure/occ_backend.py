"""OpenCASCADE boundary: STEP loading, view frames, hidden-line removal, geometry cache.

This is the ONLY module in the package that may ``import OCP.*`` (impl-contract §0.3).
Everything it returns is plain numpy / str / float, so the pure modules downstream
(`layout`, `labels`, `numbering`, `plan`, `sheet`, `qa`) never need a CAD library.

The OCC logic is adapted from the frozen legacy script ``scripts/cad_hlr_to_dxf.py``
(copied, not imported — the legacy script stays as the single-view legacy route,
impl-contract §2). Three legacy behaviours are deliberately changed:

* ``load_assembly`` sorts instances on an explicit total key BEFORE assigning
  ``instance_index``; the legacy loader inherited the XCAF traversal order, which is
  not guaranteed stable across OCCT builds (impl-contract §5.1).
* ``roll_for_axis`` uses the closed form instead of the legacy 0.5-degree sweep, which
  compared raw floats and so violated impl-contract §7 rule 4.
* Projections use element-wise multiply-add rather than ``@``; a ``(N,3)@(3,)`` product
  may be dispatched to a threaded BLAS gemv whose reduction order varies with the
  machine, and float addition is not associative (impl-contract §11.9 rule 5).
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

import numpy as np

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_EDGE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

__all__ = [
    "OccBackendError",
    "VIEWS",
    "ViewFrame",
    "roll_for_axis",
    "PartShape",
    "load_assembly",
    "part_curves",
    "scene_curves",
    "GeometryCache",
    "file_sha256",
    "export_glb",
    "hlr_drop_stats",
]


# --------------------------------------------------------------------------- constants
# impl-contract §7 rule 7: every output-affecting constant is named, defined once,
# and no function body carries a bare number.

#: name -> (azimuth, elevation) in degrees. Same seven entries as the legacy script;
#: ``top``/``bottom`` are 89.9 rather than 90 on purpose (a true pole makes the up
#: vector degenerate and the fallback branch would flip the sheet).
VIEWS = {
    "iso": (35.0, 20.0),
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "right": (90.0, 0.0),
    "left": (270.0, 0.0),
    "top": (0.0, 89.9),
    "bottom": (0.0, -89.9),
}

#: Decimals in ``ViewFrame.key``. impl-contract §5.1 freezes the format at four:
#: enough that a 1e-4 degree difference busts the cache, not enough for float noise to.
VIEW_KEY_DEC = 4
#: Decimals used in the ``load_assembly`` sort key. impl-contract §5.1 freezes this at 6
#: so two implementers cannot pick different precisions.
SORT_DEC = 6
#: General rounding rank before any float comparison (impl-contract §7 rule 4).
RANK_DEC = 9
#: ``PartShape.degenerate`` threshold: max bbox extent below this is a degenerate part
#: (impl-contract §5.1). Model units, matching the frozen wording.
DEGENERATE_ABS = 1e-6
#: Below this the up-vector construction is degenerate and the fallback axis is used.
UP_DEGENERATE = 1e-9
#: ``roll_for_axis`` refuses an axis whose projection into the view plane is shorter
#: than this — it is parallel to the view direction and has no sheet angle.
AXIS_PROJ_MIN = 1e-9
#: Full turn, in degrees. Used to normalise the roll into [0, 360).
FULL_TURN_DEG = 360.0
#: Characters of the STEP sha256 used as the cache's first path segment.
STEP_SHA_PREFIX = 12
#: Default chord deflection for edge discretisation, same as the legacy script.
#: Exported for the CLI to use as its ``--deflection`` default. It is deliberately NOT a
#: default on ``part_curves`` / ``scene_curves``: impl-contract §5.1 freezes those
#: signatures without one, and a silent default would let a caller who forgot the
#: argument render at a different resolution than one who passed it, both passing QA.
DEFAULT_DEFLECTION = 0.02
#: HLR result compounds. Omitting the ``OutLine*`` entries breaks the silhouette of any
#: smooth surface — a sphere or a torus has no B-rep edge along its outline, so a drawing
#: built from model edges alone shows the dome and the fillet as broken outlines.
VISIBLE_COMPOUNDS = ("VCompound", "Rg1LineVCompound", "OutLineVCompound")
HIDDEN_COMPOUNDS = ("HCompound", "Rg1LineHCompound", "OutLineHCompound")
#: Cache array naming produced by ``np.savez(fh, *arrays)``.
CACHE_ARRAY_PREFIX = "arr_"
#: Read block size for the file digest.
SHA_BLOCK = 1 << 20


class OccBackendError(RuntimeError):
    """STEP loading or HLR failed in a way the caller has to report, not paper over."""


# Diagnostic only, never read on any output-affecting path: OCCT occasionally refuses to
# discretise a single edge, and the legacy script swallowed that silently. The counters
# make the loss visible without changing any frozen signature. Downstream, an empty curve
# list is caught by layout.py, which raises naming the piece key (impl-contract §11.8).
_HLR_DROPPED = {"edges": 0, "compounds": 0}


def hlr_drop_stats() -> dict:
    """Cumulative count of edges/compounds HLR could not turn into polylines.

    Diagnostics only — nothing in the rendered output depends on it.
    """
    return dict(_HLR_DROPPED)


# --------------------------------------------------------------------------- view frame


class ViewFrame:
    """Right-handed view frame. ``roll`` rotates the sheet, not the model.

    ``x`` is right, ``u`` is up, ``w`` is the view direction, all unit vectors with
    ``cross(x, u) == w``. A point's sheet coordinates are ``(p·x, p·u)``.
    """

    def __init__(self, az: float, el: float, roll: float = 0.0) -> None:
        # Stored raw, exactly as passed: impl-contract §5.1 forbids rounding the stored
        # floats; only `key` formats them.
        self.az = float(az)
        self.el = float(el)
        self.roll = float(roll)

        a, e = math.radians(self.az), math.radians(self.el)
        w = np.array([math.cos(e) * math.sin(a),
                      -math.cos(e) * math.cos(a),
                      math.sin(e)], dtype=np.float64)
        w /= np.linalg.norm(w)

        up = np.array([0.0, 0.0, 1.0], dtype=np.float64)
        u = up - w * float(up[0] * w[0] + up[1] * w[1] + up[2] * w[2])
        if float(np.linalg.norm(u)) < UP_DEGENERATE:
            alt = np.array([1.0, 0.0, 0.0], dtype=np.float64)
            u = alt - w * float(alt[0] * w[0] + alt[1] * w[1] + alt[2] * w[2])
        u /= np.linalg.norm(u)
        x = np.cross(u, w)

        # Applied unconditionally: at roll == 0 this is exact (cos 0 == 1.0, sin 0 == 0.0),
        # so no float equality test is needed to skip it.
        t = math.radians(self.roll)
        ct, st = math.cos(t), math.sin(t)
        u, x = u * ct + x * st, -u * st + x * ct

        self.w = w
        self.u = u
        self.x = x

    def to2d(self, pts: np.ndarray) -> np.ndarray:
        """Project ``(..., 3)`` model points to ``(..., 2)`` sheet coordinates.

        Element-wise on purpose — see the module docstring on BLAS reduction order.
        """
        p = np.asarray(pts, dtype=np.float64)
        if p.ndim < 1 or p.shape[-1] != 3:
            raise ValueError("to2d：输入点集的最后一维必须是 3，收到形状 %r" % (p.shape,))
        sx = p[..., 0] * self.x[0] + p[..., 1] * self.x[1] + p[..., 2] * self.x[2]
        su = p[..., 0] * self.u[0] + p[..., 1] * self.u[1] + p[..., 2] * self.u[2]
        return np.stack((sx, su), axis=-1)

    @property
    def key(self) -> str:
        """Cache key: ``f"{az:.4f}_{el:.4f}_{roll:.4f}"`` (impl-contract §5.1)."""
        fmt = "%%.%df" % VIEW_KEY_DEC
        return "_".join(fmt % v for v in (self.az, self.el, self.roll))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ViewFrame(az=%r, el=%r, roll=%r)" % (self.az, self.el, self.roll)


def roll_for_axis(axis, az: float, el: float, target_deg: float) -> float:
    """Roll that puts ``axis`` at ``target_deg`` on the sheet. Closed form, no search.

    With ``e`` the axis projected into the *unrolled* view plane, the sheet angle is
    ``atan2(e_u, e_x)`` and the roll that moves it onto the target is

        roll = target_deg - degrees(atan2(e_u, e_x))

    normalised into ``[0, 360)``. (Under this frame's roll convention the sheet angle of
    a fixed vector is exactly ``atan2(e_u, e_x) + roll``, so the solution is exact.)
    The legacy implementation stepped through candidate angles and compared raw floats,
    which violates impl-contract §7 rule 4; the closed form removes both problems.

    Raises ``ValueError`` when the axis is parallel to the view direction.
    """
    a = np.asarray(axis, dtype=np.float64).reshape(3)
    na = float(np.linalg.norm(a))
    if not np.isfinite(na) or na < AXIS_PROJ_MIN:
        raise ValueError("roll_for_axis：爆炸轴模长 %.3e 过小或非有限值，无法确定方向" % na)
    a = a / na

    base = ViewFrame(az, el, 0.0)
    ex = float(a[0] * base.x[0] + a[1] * base.x[1] + a[2] * base.x[2])
    eu = float(a[0] * base.u[0] + a[1] * base.u[1] + a[2] * base.u[2])
    if float(math.hypot(ex, eu)) < AXIS_PROJ_MIN:
        raise ValueError(
            "roll_for_axis：轴在图面上的投影模长 %.3e < %.1e，轴几乎平行于视线，"
            "沿轴的图面角度无定义。修复：改 layout.view，或改 layout.explode_axis。"
            % (math.hypot(ex, eu), AXIS_PROJ_MIN))

    roll = float(target_deg) - math.degrees(math.atan2(eu, ex))
    roll = round(roll % FULL_TURN_DEG, RANK_DEC)
    if roll >= FULL_TURN_DEG:  # rounding up from 359.9999999996 must not leave the range
        roll = 0.0
    return roll


# --------------------------------------------------------------------------- part shape


def _shape_bbox(shape) -> Tuple[np.ndarray, np.ndarray]:
    """Axis-aligned model-space bounding box of a shape; zeros for a void box."""
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        return np.zeros(3), np.zeros(3)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return (np.array([x0, y0, z0], dtype=np.float64),
            np.array([x1, y1, z1], dtype=np.float64))


class PartShape:
    """One placed solid from a STEP assembly, already moved into model coordinates.

    ``key`` (``"<name>#<instance_index>"``) is the stable identity used everywhere
    downstream — layout pieces, label requests, numbering, and the geometry cache all
    key on it. It is only stable because ``load_assembly`` sorts before numbering.
    """

    def __init__(self, name: str, path: str, shape: Any, instance_index: int,
                 lo: Optional[np.ndarray] = None, hi: Optional[np.ndarray] = None) -> None:
        self.name = str(name)
        self.path = str(path)
        self.shape = shape
        self.instance_index = int(instance_index)
        self.key = "%s#%d" % (self.name, self.instance_index)
        if lo is None or hi is None:
            lo, hi = _shape_bbox(shape)
        self.lo = np.asarray(lo, dtype=np.float64).reshape(3)
        self.hi = np.asarray(hi, dtype=np.float64).reshape(3)

    @property
    def center(self) -> np.ndarray:
        return (self.lo + self.hi) / 2.0

    @property
    def degenerate(self) -> bool:
        """True when the largest bbox extent is below ``DEGENERATE_ABS`` (§5.1)."""
        ext = float(max(self.hi[0] - self.lo[0],
                        self.hi[1] - self.lo[1],
                        self.hi[2] - self.lo[2]))
        return round(ext, RANK_DEC) < DEGENERATE_ABS

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "PartShape(key=%r, path=%r)" % (self.key, self.path)


def load_assembly(step: Path) -> list:
    """Load every placed solid of a STEP assembly, in a deterministic order.

    The sort key is exactly ``(name, path, round(cx, 6), round(cy, 6), round(cz, 6))``
    where ``c`` is the instance centre in model coordinates. ``instance_index`` is
    assigned AFTER this sort — per part name, counting from 0 — so the same STEP always
    yields the same ``key`` values. Ties beyond the full key keep whatever order OCCT
    produced, which is NOT guaranteed stable across OCCT builds; two instances that tie
    on the full key are geometrically identical at micron resolution, so which one takes
    which index cannot change any output.

    Returns ``list[PartShape]``.
    """
    step = Path(step)
    if not step.is_file():
        raise OccBackendError("找不到 STEP 文件：%s" % step)

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(False)
    reader.SetLayerMode(False)
    if reader.ReadFile(str(step)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise OccBackendError("无法读取 STEP 文件（格式错误或文件损坏）：%s" % step)
    doc = TDocStd_Document(TCollection_ExtendedString("d"))
    if reader.Transfer(doc) is False:
        raise OccBackendError("STEP 装配体转换失败（OCCT Transfer 返回失败）：%s" % step)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def label_name(label) -> str:
        attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
            return attr.Get().ToExtString()
        return "?"

    records = []  # (name, path, shape, lo, hi, center)

    def walk(label, loc, path_str: str) -> None:
        if tool.IsAssembly_s(label):
            seq = TDF_LabelSequence()
            tool.GetComponents_s(label, seq)
            for i in range(1, seq.Length() + 1):
                comp = seq.Value(i)
                child_loc = tool.GetLocation_s(comp)
                ref = TDF_Label()
                target = ref if tool.GetReferredShape_s(comp, ref) else comp
                walk(target, loc.Multiplied(child_loc),
                     "%s/%s" % (path_str, label_name(target)))
            return
        shape = tool.GetShape_s(label)
        if shape is not None and not shape.IsNull():
            placed = shape.Moved(loc)
            lo, hi = _shape_bbox(placed)
            records.append((label_name(label), path_str, placed, lo, hi, (lo + hi) / 2.0))

    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        lab = roots.Value(i)
        walk(lab, tool.GetLocation_s(lab), label_name(lab))

    if not records:
        raise OccBackendError("STEP 中没有找到任何实体形状：%s" % step)

    # sorted() is stable, so a tie on the full key keeps OCCT's order (see docstring).
    records.sort(key=lambda r: (r[0], r[1],
                                round(float(r[5][0]), SORT_DEC),
                                round(float(r[5][1]), SORT_DEC),
                                round(float(r[5][2]), SORT_DEC)))

    parts = []
    counter = {}  # name -> next index; only ever read via .get, never iterated
    for name, path_str, shape, lo, hi, _center in records:
        idx = counter.get(name, 0)
        counter[name] = idx + 1
        parts.append(PartShape(name, path_str, shape, idx, lo=lo, hi=hi))
    return parts


# --------------------------------------------------------------------------------- hlr


def _edges(shape, deflection: float) -> list:
    """Discretise every edge of an HLR result compound into a sheet-space polyline.

    HLR result edges already live in the projection plane (X along ``view.x``,
    Y along ``view.u``), so no further projection is applied here.
    """
    out = []
    if shape is None or shape.IsNull():
        return out
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        try:
            disc = GCPnts_QuasiUniformDeflection(
                BRepAdaptor_Curve(TopoDS.Edge_s(exp.Current())), deflection)
            if disc.IsDone() and disc.NbPoints() >= 2:
                out.append(np.array(
                    [[disc.Value(i).X(), disc.Value(i).Y()]
                     for i in range(1, disc.NbPoints() + 1)], dtype=np.float64))
            else:
                _HLR_DROPPED["edges"] += 1
        except Exception:
            # One unmeshable edge must not kill a whole figure; it is counted so the
            # loss is visible through hlr_drop_stats() instead of being silent.
            _HLR_DROPPED["edges"] += 1
        exp.Next()
    return out


def _run_hlr(shapes: Sequence, view: ViewFrame, deflection: float,
             want_hidden: bool) -> Tuple[list, list]:
    """Analytic hidden-line removal over ``shapes`` as one scene."""
    if not (float(deflection) > 0.0) or not math.isfinite(float(deflection)):
        raise ValueError("HLR：deflection 必须为正的有限值，收到 %r" % (deflection,))
    algo = HLRBRep_Algo()
    added = 0
    for s in shapes:
        if s is not None and not s.IsNull():
            algo.Add(s)
            added += 1
    if added == 0:
        raise OccBackendError("HLR：输入形状为空，无法投影")

    axis = gp_Ax2(gp_Pnt(0.0, 0.0, 0.0),
                  gp_Dir(*(float(v) for v in view.w)),
                  gp_Dir(*(float(v) for v in view.x)))
    algo.Projector(HLRAlgo_Projector(axis))
    algo.Update()
    algo.Hide()
    to_shape = HLRBRep_HLRToShape(algo)

    visible = []
    for getter in VISIBLE_COMPOUNDS:
        try:
            visible += _edges(getattr(to_shape, getter)(), deflection)
        except Exception:
            _HLR_DROPPED["compounds"] += 1
    hidden = []
    if want_hidden:
        for getter in HIDDEN_COMPOUNDS:
            try:
                hidden += _edges(getattr(to_shape, getter)(), deflection)
            except Exception:
                _HLR_DROPPED["compounds"] += 1
    return visible, hidden


def part_curves(part: PartShape, view: ViewFrame, deflection: float) -> list:
    """Per-part HLR: hidden-line removal run on this part alone.

    VALID ONLY FOR PARTS THAT ARE DISJOINT ON THE SHEET. Occlusion is computed within
    the single shape handed in, so anything in front of or behind this part is invisible
    to the algorithm and its silhouette will be drawn straight through.

    Concretely:

    * ``kind="exploded"`` figures may use it *per body*, because impl-contract §11.3 S8
      guarantees, as a postcondition, that placed body bounding boxes do not overlap on
      the sheet. Inside one body (a bolt circle, a set of identical screws) members are
      allowed to overlap, so a body with more than one member must go through
      ``scene_curves(body_members, ...)`` instead — per-part HLR would draw the wrong
      occlusion between them.
    * ``kind="assembly"`` figures must NEVER use it. Parts occlude each other in place;
      use ``scene_curves`` for the whole figure.

    Returns visible curves only, as a list of ``(N, 2)`` float64 sheet-space arrays.
    """
    return _run_hlr([part.shape], view, deflection, False)[0]


def scene_curves(parts: Sequence, view: ViewFrame, deflection: float,
                 want_hidden: bool = False) -> Tuple[list, list]:
    """Global HLR: every shape is added to one scene, so parts occlude each other.

    REQUIRED FOR ``kind="assembly"`` FIGURES — there the parts sit in their projected
    positions and hide one another, and per-part HLR would render occlusion that is
    simply wrong. Also required for any exploded *body* holding more than one member
    (impl-contract §11.3), for the same reason at a smaller scale.

    Returns ``(visible, hidden)`` as lists of ``(N, 2)`` float64 sheet-space arrays;
    ``hidden`` is empty unless ``want_hidden`` is set.
    """
    return _run_hlr([p.shape for p in parts], view, deflection, want_hidden)


# ------------------------------------------------------------------------------- cache


#: Linear deflection for the GLB preview mesh, relative to each shape's bounding box
#: (``isRelative=True``), plus the angular deflection in radians. Preview-only quality:
#: the mesh feeds the Plan Studio picker, never a drawing, so it is deliberately coarser
#: than anything the HLR pipeline uses and does not touch the deterministic outputs.
GLB_LINEAR_DEFLECTION = 0.004
GLB_ANGULAR_DEFLECTION = 0.35


def export_glb(step: Path, out: Path) -> Path:
    """Write a binary glTF of the assembly for the Plan Studio 3D picker.

    Uses OCCT's own ``RWGltf_CafWriter`` over the XCAF document, which carries the STEP
    component names into glTF node names — verified on the synthetic fixture: eleven
    instance nodes, every ``SYN-*`` name intact, repeated instances as repeated nodes.
    The picker maps a raycast hit to its node name and from there to the part name that
    plans are written in; instance identity is not needed because every plan field
    (``terms[].selector``, ``figures[].members``) addresses parts by name.

    This is a PREVIEW artefact. Nothing in the deterministic render chain reads it, so
    its mesh quality, node order and byte content are all allowed to vary between OCCT
    builds without touching any golden digest.
    """
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.Message import Message_ProgressRange
    from OCP.RWGltf import RWGltf_CafWriter
    from OCP.TColStd import TColStd_IndexedDataMapOfStringString
    from OCP.TCollection import TCollection_AsciiString

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(False)
    reader.SetLayerMode(False)
    if reader.ReadFile(str(step)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise OccBackendError("cannot read STEP: %s" % step)
    doc = TDocStd_Document(TCollection_ExtendedString("glb"))
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        shape = tool.GetShape_s(roots.Value(i))
        if shape is not None and not shape.IsNull():
            BRepMesh_IncrementalMesh(shape, GLB_LINEAR_DEFLECTION, True,
                                     GLB_ANGULAR_DEFLECTION, True)
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = RWGltf_CafWriter(TCollection_AsciiString(str(out)), True)  # True => binary .glb
    ok = writer.Perform(doc, TColStd_IndexedDataMapOfStringString(), Message_ProgressRange())
    if not ok or not out.is_file():
        raise OccBackendError("glTF export failed for %s" % step)
    return out


def file_sha256(path: Path) -> str:
    """SHA-256 of a file, streamed. Convenience for callers building a GeometryCache."""
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        while True:
            block = handle.read(SHA_BLOCK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class GeometryCache:
    """On-disk npz cache of HLR curve sets, keyed by STEP digest, view and deflection.

    Layout (impl-contract §5.1)::

        <root>/<step_sha[:12]>/<view.key>_<deflection>/<sha1(part_key)>.npz

    Arrays are stored uncompressed as ``arr_0..arr_n`` and read back in numeric index
    order, so a hit is bit-identical to a recompute — not merely close. Loading the
    reference assembly takes 24 s, which makes this mandatory rather than an
    optimisation.

    A file that exists but holds no arrays is a legitimate hit for an empty curve list;
    only a missing (or unreadable) file is a miss.
    """

    def __init__(self, root: Path, step_sha: str) -> None:
        self.root = Path(root)
        self.step_sha = str(step_sha)
        if not self.step_sha:
            raise ValueError("GeometryCache：step_sha 不能为空")
        self.prefix = self.step_sha[:STEP_SHA_PREFIX]

    # -- paths ---------------------------------------------------------------
    def _view_dir(self, view: ViewFrame, deflection: float) -> Path:
        # repr() of a float is round-trippable and platform-stable, so two different
        # deflections can never collide on one directory name.
        return self.root / self.prefix / ("%s_%s" % (view.key, repr(float(deflection))))

    def _path(self, part_key: str, view: ViewFrame, deflection: float) -> Path:
        stem = hashlib.sha1(str(part_key).encode("utf-8")).hexdigest()
        return self._view_dir(view, deflection) / (stem + ".npz")

    # -- api -----------------------------------------------------------------
    def get(self, part_key: str, view: ViewFrame, deflection: float) -> Optional[list]:
        """Return the cached curves, or None on a miss."""
        path = self._path(part_key, view, deflection)
        if not path.is_file():
            return None
        try:
            with np.load(str(path)) as data:
                names = [n for n in data.files if n.startswith(CACHE_ARRAY_PREFIX)]
                names.sort(key=lambda n: int(n[len(CACHE_ARRAY_PREFIX):]))
                return [np.asarray(data[n], dtype=np.float64) for n in names]
        except Exception:
            # A truncated or foreign file is treated as a miss; the caller recomputes
            # and put() overwrites it.
            return None

    def put(self, part_key: str, view: ViewFrame, deflection: float,
            curves: Sequence) -> None:
        """Store a curve list. Written to a temp file and renamed, so a crash mid-write
        cannot leave a half-file that later reads as a hit."""
        arrays = []
        for i, curve in enumerate(curves):
            arr = np.ascontiguousarray(np.asarray(curve, dtype=np.float64))
            if arr.ndim != 2 or arr.shape[1] != 2:
                raise ValueError("GeometryCache.put：曲线 %d 的形状 %r 非法，应为 (N, 2)"
                                 % (i, arr.shape))
            arrays.append(arr)
        path = self._path(part_key, view, deflection)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        # Passing a file object, not a name: np.savez appends ".npz" to a name that does
        # not already end in it, which would rename the temp file out from under us.
        with open(str(tmp), "wb") as handle:
            np.savez(handle, *arrays)
        os.replace(str(tmp), str(path))
