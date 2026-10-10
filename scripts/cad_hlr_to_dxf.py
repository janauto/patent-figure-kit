#!/usr/bin/env python3
"""Produce patent line drawings from a 3D CAD assembly with analytic hidden-line removal.

Reads a STEP assembly, optionally explodes it along an axis, runs OpenCASCADE's
HLRBRep hidden-line algorithm (the same class of algorithm as AutoCAD FLATSHOT or
Rhino Make2D), and writes the visible curves to DXF with all-CONTINUOUS linetypes.

Requires: cadquery-ocp (OpenCASCADE bindings), ezdxf, numpy.
"""

from __future__ import annotations

import argparse
import fnmatch
import math
from pathlib import Path

import numpy as np

from OCP.BRep import BRep_Builder
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
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
from OCP.TopoDS import TopoDS, TopoDS_Compound
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

LAYERS = ("GEOM", "HIDDEN", "LEADER", "NUM", "TABLE", "CAPTION", "NOTE")
VIEWS = {
    "iso": (35.0, 20.0),
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "right": (90.0, 0.0),
    "left": (270.0, 0.0),
    "top": (0.0, 89.9),
    "bottom": (0.0, -89.9),
}


# --------------------------------------------------------------------------- model


class Part:
    """One placed solid from the STEP assembly."""

    def __init__(self, name: str, path: str, shape) -> None:
        self.name = name
        self.path = path
        self.shape = shape
        self.offset = np.zeros(3)
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box, False)
        if box.IsVoid():
            self.lo = self.hi = np.zeros(3)
        else:
            x0, y0, z0, x1, y1, z1 = box.Get()
            self.lo = np.array([x0, y0, z0])
            self.hi = np.array([x1, y1, z1])

    @property
    def center(self) -> np.ndarray:
        return (self.lo + self.hi) / 2.0

    def placed(self):
        if np.linalg.norm(self.offset) < 1e-12:
            return self.shape
        trsf = gp_Trsf()
        trsf.SetTranslation(gp_Vec(*(float(v) for v in self.offset)))
        return BRepBuilderAPI_Transform(self.shape, trsf, True).Shape()


def load_step(path: Path) -> list[Part]:
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(False)
    reader.SetLayerMode(False)
    if reader.ReadFile(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise SystemExit(f"cannot read STEP: {path}")
    doc = TDocStd_Document(TCollection_ExtendedString("d"))
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def label_name(label) -> str:
        attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
            return attr.Get().ToExtString()
        return "?"

    parts: list[Part] = []

    def walk(label, loc, path_str: str) -> None:
        if tool.IsAssembly_s(label):
            seq = TDF_LabelSequence()
            tool.GetComponents_s(label, seq)
            for i in range(1, seq.Length() + 1):
                comp = seq.Value(i)
                child_loc = tool.GetLocation_s(comp)
                ref = TDF_Label()
                target = ref if tool.GetReferredShape_s(comp, ref) else comp
                walk(target, loc.Multiplied(child_loc), f"{path_str}/{label_name(target)}")
            return
        shape = tool.GetShape_s(label)
        if shape is not None and not shape.IsNull():
            parts.append(Part(label_name(label), path_str, shape.Moved(loc)))

    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        lab = roots.Value(i)
        walk(lab, tool.GetLocation_s(lab), label_name(lab))
    return parts


# ----------------------------------------------------------------------------- view


class View:
    """Right-handed view frame. `roll` rotates the sheet, not the model."""

    def __init__(self, az: float, el: float, roll: float = 0.0) -> None:
        a, e = math.radians(az), math.radians(el)
        d = np.array([math.cos(e) * math.sin(a), -math.cos(e) * math.cos(a), math.sin(e)])
        d /= np.linalg.norm(d)
        u = np.array([0.0, 0.0, 1.0]) - d * np.dot([0.0, 0.0, 1.0], d)
        if np.linalg.norm(u) < 1e-9:
            u = np.array([1.0, 0.0, 0.0]) - d * np.dot([1.0, 0.0, 0.0], d)
        u /= np.linalg.norm(u)
        x = np.cross(u, d)
        if roll:
            t = math.radians(roll)
            u, x = u * math.cos(t) + x * math.sin(t), -u * math.sin(t) + x * math.cos(t)
        self.d, self.u, self.x = d, u, x

    def to2d(self, pts: np.ndarray) -> np.ndarray:
        pts = np.atleast_2d(pts)
        return np.c_[pts @ self.x, pts @ self.u]


def roll_for_axis(axis, az: float, el: float, target_deg: float) -> float:
    """Roll that puts `axis` at `target_deg` on the sheet, so an explode string
    runs on a clean diagonal without displacing parts sideways."""
    a = np.asarray(axis, float)
    a /= np.linalg.norm(a)
    best = (1e9, 0.0)
    for r in np.arange(0.0, 360.0, 0.5):
        v = View(az, el, r)
        p = np.array([np.dot(a, v.x), np.dot(a, v.u)])
        if np.linalg.norm(p) < 1e-9:
            continue
        th = math.degrees(math.atan2(p[1], p[0]))
        err = abs((th - target_deg + 180.0) % 360.0 - 180.0)
        if err < best[0]:
            best = (err, float(r))
    return best[1]


def explode(parts: list[Part], axis, view: View, gap_frac: float = 0.05) -> None:
    """Space parts along `axis` by their projected footprint, so large parts do
    not overlap and the spacing reads evenly on the sheet."""
    a = np.asarray(axis, float)
    a /= np.linalg.norm(a)
    e = np.array([np.dot(a, view.x), np.dot(a, view.u)])
    k = np.linalg.norm(e)
    if k < 1e-6:
        raise SystemExit("explode axis is parallel to the view direction")
    e /= k
    spans = []
    for p in parts:
        corners = np.array([[x, y, z] for x in (p.lo[0], p.hi[0])
                            for y in (p.lo[1], p.hi[1]) for z in (p.lo[2], p.hi[2])])
        q = view.to2d(corners) @ e
        spans.append((q.min(), q.max(), p))
    spans.sort(key=lambda s: s[0])
    total = max(s[1] for s in spans) - min(s[0] for s in spans)
    gap = total * gap_frac
    cur = 0.0
    for lo, hi, p in spans:
        width = hi - lo
        p.offset = ((cur + width / 2.0) - (lo + hi) / 2.0) / k * a
        cur += width + gap


# ------------------------------------------------------------------------------ hlr


def _compound(shapes):
    builder = BRep_Builder()
    comp = TopoDS_Compound()
    builder.MakeCompound(comp)
    for s in shapes:
        if s is not None and not s.IsNull():
            builder.Add(comp, s)
    return comp


def _edges(shape, deflection: float) -> list[np.ndarray]:
    out: list[np.ndarray] = []
    if shape is None or shape.IsNull():
        return out
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        try:
            disc = GCPnts_QuasiUniformDeflection(BRepAdaptor_Curve(TopoDS.Edge_s(exp.Current())),
                                                 deflection)
            if disc.IsDone() and disc.NbPoints() >= 2:
                out.append(np.array([[disc.Value(i).X(), disc.Value(i).Y()]
                                     for i in range(1, disc.NbPoints() + 1)]))
        except Exception:
            pass
        exp.Next()
    return out


def hidden_line(shapes, view: View, deflection: float = 0.02, want_hidden: bool = False):
    """Analytic HLR. Returns (visible, hidden) polyline lists in sheet coordinates.

    `outline` curves matter: the silhouette of a smooth surface has no
    corresponding B-rep edge, so a drawing built only from model edges shows
    broken outlines on moulded and blended shapes.
    """
    algo = HLRBRep_Algo()
    for s in shapes:
        algo.Add(s)
    origin = gp_Pnt(0, 0, 0)
    axis = gp_Ax2(origin, gp_Dir(*(float(v) for v in view.d)),
                  gp_Dir(*(float(v) for v in view.x)))
    algo.Projector(HLRAlgo_Projector(axis))
    algo.Update()
    algo.Hide()
    to_shape = HLRBRep_HLRToShape(algo)
    visible: list[np.ndarray] = []
    for getter in ("VCompound", "Rg1LineVCompound", "OutLineVCompound"):
        try:
            visible += _edges(getattr(to_shape, getter)(), deflection)
        except Exception:
            pass
    hidden: list[np.ndarray] = []
    if want_hidden:
        for getter in ("HCompound", "Rg1LineHCompound", "OutLineHCompound"):
            try:
                hidden += _edges(getattr(to_shape, getter)(), deflection)
            except Exception:
                pass
    return visible, hidden


# ------------------------------------------------------------------------------ dxf


def write_dxf(path: Path, visible, hidden, *, caption: str = "", rows=None,
              note: str = "", fmt: str = "R2018",
              cjk_style_font: str = "simfang.ttf",
              num_style_font: str = "txt.shx") -> None:
    """All entities and layers get CONTINUOUS, per the cleanup skill's contract.

    Two text styles are declared: a Chinese-capable style for labels and an
    AutoCAD stick font for numerals, which is what engineering sheets use.
    """
    import ezdxf
    from ezdxf.enums import TextEntityAlignment

    doc = ezdxf.new(fmt, setup=False)
    doc.header["$INSUNITS"] = 4  # millimetres
    for name in LAYERS:
        doc.layers.add(name, linetype="CONTINUOUS")
    for layer in doc.layers:
        layer.dxf.linetype = "CONTINUOUS"
    doc.styles.add("HZ", font=cjk_style_font)
    doc.styles.add("NUM", font=num_style_font)
    msp = doc.modelspace()

    def polyline(points, layer):
        pts = [(float(x), float(y)) for x, y in points]
        if len(pts) < 2:
            return
        attribs = {"layer": layer, "linetype": "CONTINUOUS"}
        if len(pts) == 2:
            msp.add_line(pts[0], pts[1], dxfattribs=attribs)
        else:
            msp.add_lwpolyline(pts, dxfattribs=attribs)

    for p in visible:
        polyline(p, "GEOM")
    for p in hidden:
        polyline(p, "HIDDEN")

    allpts = np.vstack(visible) if visible else np.zeros((1, 2))
    lo, hi = allpts.min(0), allpts.max(0)
    size = float(max(hi - lo)) or 1.0
    height = size * 0.026

    def text(x, y, value, h, layer, align=TextEntityAlignment.MIDDLE_CENTER, numeric=False):
        entity = msp.add_text(value, height=float(h),
                              dxfattribs={"layer": layer, "linetype": "CONTINUOUS",
                                          "style": "NUM" if numeric else "HZ"})
        entity.set_placement((float(x), float(y)), align=align)

    y = lo[1] - size * 0.06
    if note:
        text((lo[0] + hi[0]) / 2, y, note, height * 0.62, "NOTE")
        y -= height * 2.0
    if rows:
        width = size * 0.86
        cols = [0.075, 0.235, 0.075, 0.615]
        xs = [lo[0]]
        for frac in cols:
            xs.append(xs[-1] + width * frac)
        rh = height * 1.9
        top = y - size * 0.04
        n = len(rows) + 1
        for i in range(n + 1):
            yy = top - i * rh
            msp.add_line((xs[0], yy), (xs[-1], yy),
                         dxfattribs={"layer": "TABLE", "linetype": "CONTINUOUS"})
        for xx in xs:
            msp.add_line((xx, top), (xx, top - n * rh),
                         dxfattribs={"layer": "TABLE", "linetype": "CONTINUOUS"})
        headers = ("NO.", "NAME", "QTY", "REMARK")
        for i, head in enumerate(headers):
            text((xs[i] + xs[i + 1]) / 2, top - rh / 2, head, height * 0.6, "TABLE")
        for j, row in enumerate(rows):
            yy = top - (j + 1.5) * rh
            for i, cell in enumerate(row[:4]):
                align = (TextEntityAlignment.MIDDLE_LEFT if i in (1, 3)
                         else TextEntityAlignment.MIDDLE_CENTER)
                x = xs[i] + height * 0.5 if i in (1, 3) else (xs[i] + xs[i + 1]) / 2
                text(x, yy, str(cell), height * 0.6, "TABLE", align,
                     numeric=str(cell).isdigit())
        y = top - n * rh
    if caption:
        text((lo[0] + hi[0]) / 2, y - size * 0.05, caption, height * 1.6, "CAPTION")
    doc.saveas(str(path))


# ----------------------------------------------------------------------------- main


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", type=Path)
    p.add_argument("output", type=Path, nargs="?",
                   help="omit when using --list-parts")
    p.add_argument("--view", default="iso", choices=sorted(VIEWS))
    p.add_argument("--az", type=float)
    p.add_argument("--el", type=float)
    p.add_argument("--roll", type=float, default=0.0)
    p.add_argument("--include", action="append", default=[],
                   help="glob on part name; repeatable")
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--explode-axis", choices=["x", "y", "z"])
    p.add_argument("--explode-gap", type=float, default=0.05)
    p.add_argument("--axis-angle", type=float, default=152.0,
                   help="sheet angle for the explode string; auto-derives roll")
    p.add_argument("--deflection", type=float, default=0.02)
    p.add_argument("--hidden", action="store_true", help="also emit hidden lines")
    p.add_argument("--caption", default="")
    p.add_argument("--note", default="")
    p.add_argument("--table", action="store_true", help="emit an auto part table")
    p.add_argument("--dxf-version", default="R2018")
    p.add_argument("--list-parts", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    parts = load_step(args.step)
    if args.list_parts:
        # inspection mode: no output file needed
        seen: dict[str, tuple] = {}
        counts: dict[str, int] = {}
        for x in parts:
            counts[x.name] = counts.get(x.name, 0) + 1
            seen.setdefault(x.name, tuple(round(float(v), 1) for v in (x.hi - x.lo)))
        print(f"{'PART':<40}{'QTY':>5}   SIZE X x Y x Z (mm)")
        for name in sorted(seen):
            w, d, h = seen[name]
            print(f"{name:<40}{counts[name]:>5}   {w} x {d} x {h}")
        print(f"\n{len(parts)} instance(s), {len(seen)} distinct part(s)")
        return 0
    if args.include:
        parts = [p for p in parts
                 if any(fnmatch.fnmatch(p.name, g) for g in args.include)]
    for g in args.exclude:
        parts = [p for p in parts if not fnmatch.fnmatch(p.name, g)]
    if not parts:
        raise SystemExit("no parts selected")

    az, el = VIEWS[args.view]
    az = args.az if args.az is not None else az
    el = args.el if args.el is not None else el
    roll = args.roll
    axis = None
    if args.explode_axis:
        axis = {"x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}[args.explode_axis]
        if not roll:
            roll = roll_for_axis(axis, az, el, args.axis_angle)
    view = View(az, el, roll)
    if axis:
        explode(parts, axis, view, args.explode_gap)

    shapes = [p.placed() for p in parts]
    visible, hidden = hidden_line(shapes, view, args.deflection, args.hidden)
    if not visible:
        raise SystemExit("hidden-line removal produced no visible curves")

    rows = None
    if args.table:
        seen: dict[str, int] = {}
        for p in parts:
            seen[p.name] = seen.get(p.name, 0) + 1
        rows = [(i + 1, name, qty, "") for i, (name, qty) in enumerate(sorted(seen.items()))]

    if args.output is None:
        raise SystemExit("an output .dxf path is required unless --list-parts is used")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_dxf(args.output, visible, hidden, caption=args.caption, rows=rows,
              note=args.note, fmt=args.dxf_version)
    print(f"{args.output}  parts={len(parts)}  visible={len(visible)}  hidden={len(hidden)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
