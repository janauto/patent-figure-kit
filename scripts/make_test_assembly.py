#!/usr/bin/env python3
"""Build the synthetic STEP assembly used by the test suite.

The repository must never carry a customer model or a real part name, so the golden tests run
against this file instead. It deliberately exercises the awkward paths: repeated instances of one
part, a smooth dome and a torus whose silhouettes have no B-rep edge, and a stack of coaxial parts.

The degenerate zero-extent part is deliberately NOT in this file. A bare vertex loses its name in
the STEP round trip and comes back labelled with the OCCT version string, which would make the
golden digest depend on the installed OCCT build. `tests/test_analyze.py` injects a degenerate
PartShape directly instead, which covers the same code path deterministically.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder,
                             BRepPrimAPI_MakeSphere, BRepPrimAPI_MakeTorus)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf
from OCP.Interface import Interface_Static
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_StepModelType
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFDoc import XCAFDoc_DocumentTool

# The assembly is a stack along +Z so that the principal axis is unambiguous and the exploded
# route has something meaningful to explode. Dimensions are millimetres.
#
# name, builder, (x, y, z) placement, repeat count
PARTS = [
    ("SYN-A01", lambda: _box(60.0, 60.0, 6.0, -30.0, -30.0, 0.0), (0.0, 0.0, 0.0), 1),
    ("SYN-B02", lambda: _cyl(26.0, 18.0), (0.0, 0.0, 6.0), 1),
    ("SYN-C03", lambda: _cyl(9.0, 34.0), (0.0, 0.0, 24.0), 1),
    ("SYN-D04", lambda: _torus(20.0, 3.5), (0.0, 0.0, 30.0), 1),
    ("SYN-E05", lambda: _cyl(22.0, 5.0), (0.0, 0.0, 58.0), 1),
    ("SYN-F06", lambda: _sphere(16.0), (0.0, 0.0, 74.0), 1),
    ("SYN-G07", lambda: _box(44.0, 44.0, 4.0, -22.0, -22.0, 0.0), (0.0, 0.0, 92.0), 1),
    # four instances of one screw, placed on a square pattern: exercises qty and label modes
    ("SYN-H08", lambda: _cyl(2.0, 10.0), (18.0, 18.0, 96.0), 4),
]

SCREW_PATTERN = [(18.0, 18.0), (-18.0, 18.0), (-18.0, -18.0), (18.0, -18.0)]


def _box(dx: float, dy: float, dz: float, ox: float, oy: float, oz: float):
    return BRepPrimAPI_MakeBox(gp_Pnt(ox, oy, oz), dx, dy, dz).Shape()


def _cyl(radius: float, height: float):
    axis = gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0))
    return BRepPrimAPI_MakeCylinder(axis, radius, height).Shape()


def _sphere(radius: float):
    return BRepPrimAPI_MakeSphere(gp_Pnt(0.0, 0.0, 0.0), radius).Shape()


def _torus(major: float, minor: float):
    axis = gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0))
    return BRepPrimAPI_MakeTorus(axis, major, minor).Shape()


def _located(x: float, y: float, z: float) -> TopLoc_Location:
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Pnt(0.0, 0.0, 0.0), gp_Pnt(x, y, z))
    return TopLoc_Location(trsf)


def build(output: Path) -> Path:
    doc = TDocStd_Document(TCollection_ExtendedString("synthetic"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    root: TDF_Label = tool.NewShape()
    TDataStd_Name.Set_s(root, TCollection_ExtendedString("SYN-ASSY"))

    for name, builder, (x, y, z), count in PARTS:
        proto = tool.AddShape(builder(), False)
        TDataStd_Name.Set_s(proto, TCollection_ExtendedString(name))
        if count == 1:
            comp = tool.AddComponent(root, proto, _located(x, y, z))
            TDataStd_Name.Set_s(comp, TCollection_ExtendedString(name))
        else:
            for px, py in SCREW_PATTERN[:count]:
                comp = tool.AddComponent(root, proto, _located(px, py, z))
                TDataStd_Name.Set_s(comp, TCollection_ExtendedString(name))

    tool.UpdateAssemblies()

    output.parent.mkdir(parents=True, exist_ok=True)
    Interface_Static.SetCVal_s("write.step.schema", "AP214IS")
    Interface_Static.SetCVal_s("write.step.product.name", "SYN-ASSY")
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.SetColorMode(False)
    writer.SetLayerMode(False)
    writer.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    status = writer.Write(str(output))
    if int(status) != 1:  # IFSelect_RetDone
        raise SystemExit(f"STEP write failed with status {status}")
    return output


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", type=Path,
                    default=Path(__file__).resolve().parents[1] / "tests/fixtures/synthetic.stp")
    args = ap.parse_args()
    path = build(args.output)
    data = path.read_bytes()
    print(f"{path}  {len(data)} bytes")
    print(f"parts={len(PARTS)}  instances={sum(p[3] for p in PARTS)}")
    print(f"sha256={hashlib.sha256(data).hexdigest()[:16]}  "
          f"(STEP headers carry a timestamp, so the digest is informational only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
