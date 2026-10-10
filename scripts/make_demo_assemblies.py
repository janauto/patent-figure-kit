#!/usr/bin/env python3
"""生成三个可公开演示的 STEP 装配体（与任何真实产品无关），给工作台上传演示用。

    python3 scripts/make_demo_assemblies.py -o ~/Desktop/专利附图演示STEP

* ``demo-planetary-gearbox.stp`` 行星齿轮减速器：电机子装配 + 齿轮系 + 轴承端盖，同轴堆叠；
* ``demo-bench-vise.stp``        台虎钳：固定钳身 / 活动钳身 / 丝杠手柄三个子装配；
* ``demo-ball-valve.stp``        球阀：法兰阀体、开孔球芯、阀座、阀杆填料与手柄。

只用 OCP 基本体、平面多边形拉伸与布尔运算建模：齿形是多边形近似，消隐算得快；
球芯、手柄球头等光滑曲面用来演示「没有棱边的轮廓线」。零件名用英文代号，
专门留给工作台的「AI 起草零件名」去起中文名。单位毫米。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder,
                             BRepPrimAPI_MakePrism, BRepPrimAPI_MakeSphere,
                             BRepPrimAPI_MakeTorus)
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.Interface import Interface_Static
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_StepModelType
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFDoc import XCAFDoc_DocumentTool

Vec3 = Tuple[float, float, float]
X, Y, Z = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)


# --------------------------------------------------------------------------- #
# 建模小工具                                                                    #
# --------------------------------------------------------------------------- #

def cyl(r: float, h: float, at: Vec3 = (0, 0, 0), axis: Vec3 = Z):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*at), gp_Dir(*axis)), r, h).Shape()


def box(dx: float, dy: float, dz: float, at: Vec3 = (0, 0, 0)):
    return BRepPrimAPI_MakeBox(gp_Pnt(*at), dx, dy, dz).Shape()


def cbox(dx: float, dy: float, dz: float, center: Vec3 = (0, 0, 0)):
    """以底面中心定位的方块（z 从 center.z 起）。"""
    return box(dx, dy, dz, (center[0] - dx / 2, center[1] - dy / 2, center[2]))


def sphere(r: float, at: Vec3 = (0, 0, 0)):
    return BRepPrimAPI_MakeSphere(gp_Pnt(*at), r).Shape()


def torus(R: float, r: float, at: Vec3 = (0, 0, 0), axis: Vec3 = Z):
    return BRepPrimAPI_MakeTorus(gp_Ax2(gp_Pnt(*at), gp_Dir(*axis)), R, r).Shape()


def cut(a, *bs):
    for b in bs:
        a = BRepAlgoAPI_Cut(a, b).Shape()
    return a


def fuse(a, *bs):
    for b in bs:
        a = BRepAlgoAPI_Fuse(a, b).Shape()
    return a


def tube(ro: float, ri: float, h: float, at: Vec3 = (0, 0, 0), axis: Vec3 = Z):
    return cut(cyl(ro, h, at, axis), cyl(ri, h + 2, _shift(at, axis, -1), axis))


def _shift(p: Vec3, d: Vec3, t: float) -> Vec3:
    return (p[0] + d[0] * t, p[1] + d[1] * t, p[2] + d[2] * t)


def prism_xy(points: Sequence[Tuple[float, float]], h: float, z0: float = 0.0):
    """XY 平面多边形沿 +Z 拉伸。"""
    poly = BRepBuilderAPI_MakePolygon()
    for x, y in points:
        poly.Add(gp_Pnt(x, y, z0))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, h)).Shape()


def gear_profile(teeth: int, module: float, internal: bool = False) -> List[Tuple[float, float]]:
    """梯形齿近似：每齿 4 个点。外齿顶圆 r+m、齿根 r-1.25m；内齿反过来。"""
    r = module * teeth / 2.0
    r_tip, r_root = (r - module, r + 1.25 * module) if internal else (r + module, r - 1.25 * module)
    pts = []
    step = 2 * math.pi / teeth
    for i in range(teeth):
        a = i * step
        for frac, rad in ((-0.30, r_root), (-0.16, r_tip), (0.16, r_tip), (0.30, r_root)):
            ang = a + frac * step
            pts.append((rad * math.cos(ang), rad * math.sin(ang)))
    return pts


def gear(teeth: int, module: float, width: float, bore: float = 0.0, z0: float = 0.0):
    g = prism_xy(gear_profile(teeth, module), width, z0)
    return cut(g, cyl(bore, width + 2, (0, 0, z0 - 1))) if bore > 0 else g


def hexagon(across_flats: float, h: float, z0: float = 0.0):
    r = across_flats / math.sqrt(3)
    return prism_xy([(r * math.cos(math.radians(60 * k + 30)),
                      r * math.sin(math.radians(60 * k + 30))) for k in range(6)], h, z0)


def screw(d: float, length: float, head_d: float = None, head_h: float = None):
    """沿 +Z 的内六角圆柱头螺钉：头在 z∈[0, head_h]，杆向 -Z。"""
    head_d = head_d or 1.6 * d
    head_h = head_h or 0.9 * d
    head = cut(cyl(head_d / 2, head_h), hexagon(0.5 * head_d, head_h * 0.6, head_h * 0.45))
    return fuse(head, cyl(d / 2, length, (0, 0, -length)))


def bolt_circle(n: int, radius: float, phase: float = 0.0) -> List[Tuple[float, float]]:
    return [(radius * math.cos(phase + 2 * math.pi * k / n),
             radius * math.sin(phase + 2 * math.pi * k / n)) for k in range(n)]


# --------------------------------------------------------------------------- #
# 装配树（XCAF）                                                                #
# --------------------------------------------------------------------------- #

def loc(at: Vec3 = (0, 0, 0), rot_axis: Optional[Vec3] = None, deg: float = 0.0,
        rot2: Optional[Tuple[Vec3, float]] = None) -> TopLoc_Location:
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(*at))
    for axis, ang in ([(rot_axis, deg)] if rot_axis else []) + ([rot2] if rot2 else []):
        r = gp_Trsf()
        r.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(*axis)), math.radians(ang))
        t = t.Multiplied(r)
    return TopLoc_Location(t)


class Assy:
    def __init__(self, name: str) -> None:
        self.doc = TDocStd_Document(TCollection_ExtendedString(name))
        self.tool = XCAFDoc_DocumentTool.ShapeTool_s(self.doc.Main())
        self.name = name
        self.root = self.sub(name)
        self.protos = {}
        self.count = 0

    def _named(self, label, name):
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        return label

    def sub(self, name: str):
        return self._named(self.tool.NewShape(), name)

    def proto(self, name: str, shape):
        if name not in self.protos:
            self.protos[name] = self._named(self.tool.AddShape(shape, False), name)
        return self.protos[name]

    def add(self, parent, name: str, shape_or_label, location: TopLoc_Location):
        label = shape_or_label if not hasattr(shape_or_label, "ShapeType") else \
            self.proto(name, shape_or_label)
        comp = self.tool.AddComponent(parent, label, location)
        self._named(comp, name)
        if hasattr(shape_or_label, "ShapeType"):
            self.count += 1
        return comp

    def write(self, path: Path) -> Path:
        self.tool.UpdateAssemblies()
        path.parent.mkdir(parents=True, exist_ok=True)
        Interface_Static.SetCVal_s("write.step.schema", "AP214IS")
        Interface_Static.SetCVal_s("write.step.product.name", self.name)
        w = STEPCAFControl_Writer()
        w.SetNameMode(True)
        w.SetColorMode(False)
        w.SetLayerMode(False)
        w.Transfer(self.doc, STEPControl_StepModelType.STEPControl_AsIs)
        if int(w.Write(str(path))) != 1:
            raise SystemExit("STEP 写出失败：%s" % path)
        return path


# --------------------------------------------------------------------------- #
# 1. 行星齿轮减速器（主轴 Z）                                                    #
# --------------------------------------------------------------------------- #

def planetary_gearbox(out: Path) -> Tuple[Path, int]:
    m = 1.5
    zs, zp, zr = 12, 18, 48            # 太阳轮 / 行星轮 / 齿圈：zr = zs + 2 zp
    a = m * (zs + zp) / 2              # 行星中心距 22.5
    A = Assy("DEMO-PGB-ASSY")

    # 电机子装配（z < 0）
    motor = A.sub("PGB-MOTOR-ASM")
    body = fuse(cyl(28, 58, (0, 0, -60)), cyl(10, 4, (0, 0, -2)))
    for k in range(10):                                   # 散热筋
        ang = 2 * math.pi * k / 10
        body = fuse(body, cbox(3, 6, 40, (29 * math.cos(ang), 29 * math.sin(ang), -52)))
    A.add(motor, "PGB-MOTOR-HOUSING", body, loc())
    cap = cut(cyl(30, 8, (0, 0, -68)), cyl(4, 10, (0, 0, -69)))
    A.add(motor, "PGB-MOTOR-REAR-CAP", cap, loc())
    A.add(motor, "PGB-MOTOR-SHAFT", cyl(4, 30, (0, 0, -8)), loc())
    A.add(A.root, "PGB-MOTOR-ASM", motor, loc())

    # 安装法兰（含 4 孔）
    flange = cut(cbox(84, 84, 10, (0, 0, 2)), cyl(12, 12, (0, 0, 1)),
                 *[cyl(3.3, 12, (x, y, 1)) for x, y in bolt_circle(4, 46, math.pi / 4)])
    A.add(A.root, "PGB-MOUNT-FLANGE", flange, loc())

    # 齿圈壳体：外圆 + 内齿
    ring = cut(cyl(46, 24, (0, 0, 12)), prism_xy(gear_profile(zr, m, internal=True), 26, 11))
    ring = cut(ring, *[cyl(2.6, 26, (x, y, 11)) for x, y in bolt_circle(6, 42)])
    A.add(A.root, "PGB-RING-GEAR", ring, loc())

    # 太阳轮（装在电机轴上）
    A.add(A.root, "PGB-SUN-GEAR", gear(zs, m, 16, bore=4, z0=16), loc())

    # 行星架子装配：行星架 + 3 销 + 3 行星轮
    carrier = A.sub("PGB-CARRIER-ASM")
    plate = cut(cyl(32, 6, (0, 0, 34)), *[cyl(3, 8, (a * math.cos(t), a * math.sin(t), 33))
                                          for t in (0, 2 * math.pi / 3, 4 * math.pi / 3)])
    plate = fuse(plate, cyl(10, 10, (0, 0, 40)))           # 输出凸台
    A.add(carrier, "PGB-PLANET-CARRIER", plate, loc())
    planet = gear(zp, m, 14, bore=3.2)
    pin = cyl(3, 22)
    for k in range(3):
        t = 2 * math.pi * k / 3
        cx, cy = a * math.cos(t), a * math.sin(t)
        A.add(carrier, "PGB-PLANET-GEAR", planet, loc((cx, cy, 18), Z, 360.0 / zp * k / 3))
        A.add(carrier, "PGB-PLANET-PIN", pin, loc((cx, cy, 16)))
    A.add(A.root, "PGB-CARRIER-ASM", carrier, loc())

    # 输出端：轴承 + 端盖 + 输出轴（带键）
    bearing = fuse(tube(22, 16, 10), tube(14, 10, 10))
    A.add(A.root, "PGB-OUTPUT-BEARING", bearing, loc((0, 0, 40)))
    cover = cut(fuse(cyl(46, 8, (0, 0, 36)), cyl(26, 10, (0, 0, 44))),
                cyl(22.2, 12, (0, 0, 39.5)), cyl(10.5, 30, (0, 0, 30)),
                *[cyl(2.6, 10, (x, y, 35)) for x, y in bolt_circle(6, 42)])
    A.add(A.root, "PGB-END-COVER", cover, loc())
    shaft = fuse(cyl(10, 52, (0, 0, 40)), cbox(4, 4, 22, (10, 0, 66)))
    A.add(A.root, "PGB-OUTPUT-SHAFT", shaft, loc())
    A.add(A.root, "PGB-OIL-SEAL", tube(22, 10.5, 5), loc((0, 0, 54)))

    # 标准件
    s_cover = screw(5, 26)
    for x, y in bolt_circle(6, 42):
        A.add(A.root, "PGB-SCREW-M5", s_cover, loc((x, y, 44)))
    s_flange = screw(6, 14)
    for x, y in bolt_circle(4, 46, math.pi / 4):
        A.add(A.root, "PGB-SCREW-M6", s_flange, loc((x, y, 12)))
    return A.write(out), A.count


# --------------------------------------------------------------------------- #
# 2. 台虎钳（丝杠轴 X）                                                          #
# --------------------------------------------------------------------------- #

def bench_vise(out: Path) -> Tuple[Path, int]:
    A = Assy("DEMO-VISE-ASSY")
    # 底座 + 导轨
    base = fuse(cbox(200, 90, 14, (0, 0, 0)), cbox(170, 36, 18, (10, 0, 14)))
    base = cut(base, *[cbox(26, 9, 16, (x, y, -1)) for x in (-82, 82) for y in (-34, 34)])
    A.add(A.root, "VISE-BASE", base, loc())

    # 固定钳身子装配（左端）
    fixed = A.sub("VISE-FIXED-JAW-ASM")
    fj = cut(cbox(40, 96, 56, (-80, 0, 14)), cbox(30, 36.5, 19, (-67.5, 0, 13)),   # 让出导轨
             cyl(9, 60, (-110, 0, 46), X))                 # 丝杠孔
    A.add(fixed, "VISE-FIXED-JAW", fj, loc())
    plate = cbox(5, 88, 24, (0, 0, 0))
    A.add(fixed, "VISE-JAW-PLATE", plate, loc((-57.5, 0, 46)))
    jaw_screw = screw(5, 10)
    for y in (-28, 28):
        A.add(fixed, "VISE-PLATE-SCREW", jaw_screw, loc((-55, y, 58), Y, 90))
    A.add(A.root, "VISE-FIXED-JAW-ASM", fixed, loc())

    # 活动钳身子装配
    moving = A.sub("VISE-MOVABLE-JAW-ASM")
    mj = fuse(cbox(44, 96, 50, (20, 0, 32)), cbox(70, 60, 18, (35, 0, 14)))   # 钳身 + 包住导轨的裙边
    mj = cut(mj, cbox(80, 36.5, 19, (35, 0, 13)), cyl(9, 70, (-10, 0, 46), X))
    A.add(moving, "VISE-MOVABLE-JAW", mj, loc())
    A.add(moving, "VISE-JAW-PLATE", plate, loc((-4.5, 0, 46)))
    for y in (-28, 28):
        A.add(moving, "VISE-PLATE-SCREW", jaw_screw, loc((-2, y, 58), Y, -90))
    nut = cut(cbox(28, 30, 26, (58, 0, 33)), cyl(8.2, 40, (40, 0, 46), X))
    A.add(moving, "VISE-LEAD-NUT", nut, loc())
    A.add(A.root, "VISE-MOVABLE-JAW-ASM", moving, loc())

    # 丝杠手柄子装配（右端）
    spindle = A.sub("VISE-SPINDLE-ASM")
    lead = cyl(8, 190, (-60, 0, 46), X)
    for k in range(18):                                    # 螺纹环示意
        lead = fuse(lead, torus(8, 1.0, (-40 + 7 * k, 0, 46), X))
    A.add(spindle, "VISE-LEAD-SCREW", lead, loc())
    A.add(spindle, "VISE-THRUST-COLLAR", tube(15, 8.2, 10, (100, 0, 46), X), loc())
    hub = cut(cyl(13, 20, (130, 0, 46), X), cyl(4.3, 40, (140, -20, 46), Y))
    A.add(spindle, "VISE-HANDLE-HUB", hub, loc())
    A.add(spindle, "VISE-HANDLE-BAR", cyl(4, 140, (140, -70, 46), Y), loc())
    knob = sphere(8)
    for y in (-72, 72):
        A.add(spindle, "VISE-HANDLE-KNOB", knob, loc((140, y, 46)))
    A.add(A.root, "VISE-SPINDLE-ASM", spindle, loc())

    # 底座螺栓
    bolt = fuse(hexagon(13, 6), cyl(4, 20, (0, 0, -20)))
    for x in (-82, 82):
        for y in (-34, 34):
            A.add(A.root, "VISE-BASE-BOLT", bolt, loc((x, y, 14)))
    return A.write(out), A.count


# --------------------------------------------------------------------------- #
# 3. 球阀（流道 X，阀杆 Z）                                                      #
# --------------------------------------------------------------------------- #

def ball_valve(out: Path) -> Tuple[Path, int]:
    A = Assy("DEMO-VALVE-ASSY")
    L = 44.0                                               # 半长：法兰外端面在 x = ±L
    body = cyl(32, 2 * L, (-L, 0, 0), X)
    for x in (-L, L - 12):                                 # 两端法兰
        fl = cyl(48, 12, (x, 0, 0), X)
        fl = cut(fl, *[cyl(5, 14, (x - 1, py, pz), X) for py, pz in bolt_circle(4, 38, math.pi / 4)])
        body = fuse(body, fl)
    body = fuse(body, cyl(18, 52, (0, 0, 20)), cyl(24, 8, (0, 0, 64)))   # 颈部 + 填料函
    body = cut(body, cyl(15, 2 * L + 10, (-L - 5, 0, 0), X), sphere(27), cyl(7.5, 60, (0, 0, 10)),
               cyl(12, 14, (0, 0, 60)))
    A.add(A.root, "VALVE-BODY", body, loc())

    ball = cut(sphere(25), cyl(13, 60, (-30, 0, 0), X), cbox(8, 30, 10, (0, 0, 20)))
    A.add(A.root, "VALVE-BALL", ball, loc())
    seat = tube(26, 14, 8, (0, 0, 0), X)
    A.add(A.root, "VALVE-SEAT", seat, loc((-30, 0, 0)))
    A.add(A.root, "VALVE-SEAT", seat, loc((22, 0, 0)))

    stem_asm = A.sub("VALVE-STEM-ASM")
    stem = fuse(cyl(7, 100, (0, 0, 18)), cbox(7.5, 14, 6, (0, 0, 18)))
    stem = cut(stem, cbox(20, 3, 12, (0, 8.5, 106)), cbox(20, 3, 12, (0, -8.5, 106)))  # 扳手平面
    A.add(stem_asm, "VALVE-STEM", stem, loc())
    A.add(stem_asm, "VALVE-PACKING-RING", tube(12, 7.2, 5), loc((0, 0, 61)))
    A.add(stem_asm, "VALVE-PACKING-RING", tube(12, 7.2, 5), loc((0, 0, 66)))
    gland = cut(hexagon(34, 14, 72), cyl(7.3, 16, (0, 0, 71)))
    A.add(stem_asm, "VALVE-GLAND-NUT", gland, loc())
    A.add(A.root, "VALVE-STEM-ASM", stem_asm, loc())

    handle = cut(fuse(cbox(96, 20, 6, (40, 0, 104)), cyl(15, 10, (0, 0, 102))),
                 cbox(7.6, 14.2, 14, (0, 0, 100)))
    handle = fuse(handle, sphere(8, (88, 0, 107)))
    A.add(A.root, "VALVE-HANDLE", handle, loc())
    A.add(A.root, "VALVE-HANDLE-NUT", cut(hexagon(16, 7, 112), cyl(4, 9, (0, 0, 111))), loc())

    bolt = fuse(hexagon(14, 7), cyl(4.6, 34, (0, 0, -34)))
    # 螺栓头贴法兰内侧端面，螺杆穿过法兰朝外伸出（去连接管道法兰）
    for x, rot in ((-L + 12, 90), (L - 12, -90)):
        for py, pz in bolt_circle(4, 38, math.pi / 4):
            A.add(A.root, "VALVE-FLANGE-BOLT", bolt, loc((x, py, pz), Y, rot))
    return A.write(out), A.count


BUILDERS: List[Tuple[str, str, Callable[[Path], Tuple[Path, int]]]] = [
    ("demo-planetary-gearbox.stp", "行星齿轮减速器", planetary_gearbox),
    ("demo-bench-vise.stp", "台虎钳", bench_vise),
    ("demo-ball-valve.stp", "球阀", ball_valve),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--outdir", type=Path, required=True)
    args = ap.parse_args(argv)
    for fname, title, build in BUILDERS:
        path, n = build(args.outdir / fname)
        print("%-28s %-8s 零件实例 %2d  %7.1f KB" % (fname, title, n, path.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
