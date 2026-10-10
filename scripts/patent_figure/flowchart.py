"""专利方法流程图：语义 JSON（节点 + 连线）→ 确定性版面 → DXF。

与结构附图同一条原则：作者（大模型或人）只写**语义**——有哪些步骤、谁连谁、判断的
「是/否」走向。步骤号（S101、S102…）、框的尺寸与坐标、连线走向、图号全部由本模块
计算，作者写不进来：节点文字里出现 ``S101`` 这类步骤号会被校验拒绝。

版面规则（黑白、实线、无底色，符合专利附图的线条图要求）：

* 主流程竖排成一列；判断框的「是」分支沿主列向下，另一分支开到右侧一列；
* 回到前面步骤的连线（循环）从左侧绕行，每条回环占一条独立的走线道；
* 步骤号写在框的右侧；开始/结束为圆角框，不发步骤号；
* 整图放进 A4 可用区，必要时等比缩小，但字高不低于 3.5 mm——再小就报警建议拆图。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import ezdxf
from ezdxf.enums import TextEntityAlignment

from . import layout as _layout
from . import sheet as _sheet

SCHEMA = "patent-flowchart/1"
KINDS = ("start", "end", "process", "decision", "io")
STEP_STYLES = ("S101", "S1")

FRAME_W, FRAME_H = _layout.FRAME_W, _layout.FRAME_H
TEXT_FLOOR_MM = _layout.TEXT_FLOOR_MM

# 尺寸常数（mm，按 1:1 设计，整图最后统一缩放）
TEXT_H = 4.0            # 框内文字字高
STEP_H = 4.5            # 步骤号字高
LABEL_H = 4.0           # 是/否 字高
CAPTION_H = 6.0         # 图号字高
BOX_W = 70.0            # 处理框宽
TERM_W = 42.0           # 开始/结束框宽
LINE_GAP = 1.45         # 行距 = 1.45 x 字高
BOX_PAD_Y = 4.0         # 框内上下留白
MIN_BOX_H = 13.0
TERM_H = 11.0
DIAMOND_K = 1.75        # 菱形高 = 1.75 x 等宽矩形所需高
ROW_GAP = 13.0          # 上下两框的净距（行间通道，含箭头与是/否）
COL_GAP = 32.0          # 左右两列的净距（步骤号 + 列间走线道）
LANE_K = 0.70           # 列间走线道位于列间距的 70% 处（避开步骤号）
SLOT = 3.0              # 同一走线道上多条线的错开距离
LANE_GAP = 7.0          # 回环走线道间距
STEP_GAP = 2.5          # 步骤号与框的距离
ARROW_L = 3.0           # 箭头长
ARROW_W = 1.1           # 箭头半宽
CJK_W = 1.3             # 全角字宽（x 字高）
ASCII_W = 0.62          # 半角字宽（x 字高）
MAX_TEXT_CHARS = 60

YES = ("是", "Y", "YES", "yes", "Yes", "满足", "成功", "通过")
STEP_IN_TEXT = re.compile(r"(^|[^A-Za-z])[Ss]\d{1,4}([^\d]|$)")


class FlowchartError(ValueError):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__("%s: %s" % (code, message))
        self.code, self.message, self.hint = code, message, hint


# --------------------------------------------------------------------------- #
# 校验                                                                          #
# --------------------------------------------------------------------------- #

def validate(spec: dict) -> List[dict]:
    """返回问题列表 [{code, severity, message, hint}]；无 error 即可渲染。"""
    issues: List[dict] = []

    def err(code, msg, hint=""):
        issues.append({"code": code, "severity": "error", "message": msg, "hint": hint})

    if not isinstance(spec, dict) or spec.get("schema") != SCHEMA:
        err("E_SCHEMA", "schema 必须是 %s" % SCHEMA, '在顶层写 "schema": "%s"' % SCHEMA)
        return issues
    allowed = {"schema", "title", "step_style", "nodes", "edges"}
    extra = set(spec) - allowed
    if extra:
        err("E_SCHEMA", "不认识的顶层字段 %s" % sorted(extra),
            "流程图只接受 title/step_style/nodes/edges——坐标、尺寸、步骤号都由脚本计算")
    if spec.get("step_style", "S101") not in STEP_STYLES:
        err("E_SCHEMA", "step_style 只能是 %s" % (STEP_STYLES,))
    nodes = spec.get("nodes")
    edges = spec.get("edges")
    if not isinstance(nodes, list) or not nodes:
        err("E_SCHEMA", "nodes 必须是非空数组")
        return issues
    if not isinstance(edges, list):
        err("E_SCHEMA", "edges 必须是数组")
        return issues
    ids = set()
    for i, n in enumerate(nodes):
        if not isinstance(n, dict) or set(n) - {"id", "kind", "text"}:
            err("E_SCHEMA", "nodes[%d] 只能有 id/kind/text 三个字段" % i)
            continue
        nid, kind, text = n.get("id"), n.get("kind"), str(n.get("text", ""))
        if not isinstance(nid, str) or not nid:
            err("E_SCHEMA", "nodes[%d].id 必须是非空字符串" % i)
            continue
        if nid in ids:
            err("E_DUP_ID", "节点 id 重复：%s" % nid)
        ids.add(nid)
        if kind not in KINDS:
            err("E_SCHEMA", "节点 %s 的 kind 必须是 %s 之一" % (nid, KINDS))
        if not text.strip():
            err("E_EMPTY_TEXT", "节点 %s 没有文字" % nid)
        if len(text) > MAX_TEXT_CHARS:
            err("E_TEXT_TOO_LONG", "节点 %s 的文字 %d 字，上限 %d" % (nid, len(text), MAX_TEXT_CHARS),
                "把细节挪到说明书，框里只写动作本身")
        if STEP_IN_TEXT.search(text):
            err("E_STEP_NUMBER_IN_TEXT", "节点 %s 的文字里写了步骤号：%s" % (nid, text),
                "删掉步骤号——S101 这类编号由脚本按流程顺序发放")
    outgoing: Dict[str, List[dict]] = {}
    for j, e in enumerate(edges):
        if not isinstance(e, dict) or set(e) - {"from", "to", "label"}:
            err("E_SCHEMA", "edges[%d] 只能有 from/to/label 三个字段" % j)
            continue
        a, b = e.get("from"), e.get("to")
        if a not in ids or b not in ids:
            err("E_EDGE_UNKNOWN_NODE", "edges[%d] 连到了不存在的节点：%s → %s" % (j, a, b))
            continue
        outgoing.setdefault(a, []).append(e)
    kinds = {n.get("id"): n.get("kind") for n in nodes if isinstance(n, dict)}
    for nid, kind in kinds.items():
        out = outgoing.get(nid, [])
        if kind == "decision":
            if len(out) != 2:
                err("E_DECISION_EDGES", "判断节点 %s 必须正好两条出线（现有 %d）" % (nid, len(out)),
                    "一条 label 写「是」，一条写「否」")
            elif not all(str(e.get("label", "")).strip() for e in out):
                err("E_DECISION_EDGES", "判断节点 %s 的两条出线都要写 label（是/否）" % nid)
        elif kind == "end":
            if out:
                err("E_SCHEMA", "结束节点 %s 不能有出线" % nid)
        elif len(out) > 1:
            err("E_SCHEMA", "节点 %s 有 %d 条出线——只有判断节点可以分叉" % (nid, len(out)),
                "把分叉改成一个 decision 节点")
    starts = [nid for nid, k in kinds.items() if k == "start"]
    if len(starts) > 1:
        err("E_SCHEMA", "开始节点只能有一个（现有 %d）" % len(starts))
    if not issues:
        entry = starts[0] if starts else nodes[0]["id"]
        seen, stack = set(), [entry]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(e["to"] for e in outgoing.get(cur, []))
        lost = [nid for nid in kinds if nid not in seen]
        if lost:
            err("E_UNREACHABLE", "从入口走不到这些节点：%s" % lost, "补上连线，或删掉这些节点")
    return issues


# --------------------------------------------------------------------------- #
# 版面                                                                          #
# --------------------------------------------------------------------------- #

@dataclass
class Node:
    id: str
    kind: str
    text: str
    lines: List[str] = field(default_factory=list)
    w: float = 0.0
    h: float = 0.0
    row: int = 0
    col: int = 0
    x: float = 0.0          # 中心
    y: float = 0.0          # 中心
    step: str = ""

    @property
    def top(self): return self.y + self.h / 2

    @property
    def bottom(self): return self.y - self.h / 2

    @property
    def left(self): return self.x - self.w / 2

    @property
    def right(self): return self.x + self.w / 2


def _text_w(s: str, h: float) -> float:
    return sum((ASCII_W if ord(c) < 0x2E80 else CJK_W) for c in s) * h


def _greedy(text: str, width: float, h: float) -> List[str]:
    lines, cur = [], ""
    for ch in text:
        if _text_w(cur + ch, h) > width and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _wrap(text: str, width: float, h: float) -> List[str]:
    """先按最大宽度贪心折行得到行数 k，再把宽度收窄到 总宽/k 重新折，避免最后一行只剩一两个字。"""
    lines = _greedy(text, width, h)
    if len(lines) > 1:
        k = len(lines)
        target = _text_w(text, h) / k
        w = target
        while w <= width:
            trial = _greedy(text, w, h)
            if len(trial) <= k:
                lines = trial
                break
            w += 0.5 * h
    # 行首不放标点
    for i in range(1, len(lines)):
        while lines[i] and lines[i][0] in "，。；、：！？）》」』,.;:!?)" and len(lines[i]) > 1:
            lines[i - 1] += lines[i][0]
            lines[i] = lines[i][1:]
    return lines or [""]


def _size(n: Node) -> None:
    if n.kind in ("start", "end"):
        n.lines = _wrap(n.text, TERM_W - 6, TEXT_H)
        n.w = TERM_W
        n.h = max(TERM_H, len(n.lines) * LINE_GAP * TEXT_H + 4)
    elif n.kind == "decision":
        n.lines = _wrap(n.text, BOX_W * 0.52, TEXT_H)
        inner = len(n.lines) * LINE_GAP * TEXT_H + 2 * BOX_PAD_Y
        n.w = BOX_W
        n.h = max(MIN_BOX_H * 1.6, inner * DIAMOND_K)
    else:
        inset = 18.0 if n.kind == "io" else 14.0
        n.lines = _wrap(n.text, BOX_W - inset, TEXT_H)
        n.w = BOX_W
        n.h = max(MIN_BOX_H, len(n.lines) * LINE_GAP * TEXT_H + 2 * BOX_PAD_Y)


def _is_yes(label: str) -> bool:
    return str(label or "").strip() in YES


@dataclass
class Edge:
    src: str
    dst: str
    label: str
    back: bool = False
    points: List[Tuple[float, float]] = field(default_factory=list)
    label_at: Optional[Tuple[float, float]] = None


@dataclass
class Solved:
    nodes: Dict[str, Node]
    order: List[str]
    edges: List[Edge]
    width: float
    height: float
    scale: float
    text_scale_floor_hit: bool
    title: str
    warnings: List[str]


def solve(spec: dict) -> Solved:
    issues = [i for i in validate(spec) if i["severity"] == "error"]
    if issues:
        first = issues[0]
        raise FlowchartError(first["code"], first["message"], first.get("hint", ""))
    nodes = {n["id"]: Node(n["id"], n["kind"], str(n["text"]).strip()) for n in spec["nodes"]}
    outgoing: Dict[str, List[dict]] = {nid: [] for nid in nodes}
    for e in spec["edges"]:
        outgoing[e["from"]].append(e)
    # 判断节点：「是」在前（沿主列向下）
    for nid, out in outgoing.items():
        out.sort(key=lambda e: 0 if _is_yes(e.get("label")) else 1)

    entry = next((n["id"] for n in spec["nodes"] if n["kind"] == "start"), spec["nodes"][0]["id"])

    # DFS：先序 = 步骤顺序；识别回边
    order: List[str] = []
    state: Dict[str, int] = {}
    back: set = set()

    def dfs(u: str) -> None:
        state[u] = 1
        order.append(u)
        for e in outgoing[u]:
            v = e["to"]
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                dfs(v)
        state[u] = 2

    import sys
    sys.setrecursionlimit(max(1000, 10 * len(nodes)))
    dfs(entry)

    # 行：前向边上的最长路径
    row = {entry: 0}
    for u in order:
        for e in outgoing[u]:
            v = e["to"]
            if (u, v) in back:
                continue
            row[v] = max(row.get(v, 0), row[u] + 1)
    # 拓扑序再松弛一遍（DFS 先序不一定是拓扑序）
    for _ in range(len(nodes)):
        changed = False
        for u in nodes:
            for e in outgoing[u]:
                v = e["to"]
                if (u, v) in back:
                    continue
                if row.get(v, 0) < row.get(u, 0) + 1:
                    row[v] = row[u] + 1
                    changed = True
        if not changed:
            break

    # 列：主链 0 列；判断的「否」分支开到右侧
    col: Dict[str, int] = {entry: 0}
    for u in order:
        for k, e in enumerate(outgoing[u]):
            v = e["to"]
            if (u, v) in back or v in col:
                continue
            col[v] = col[u] + (1 if (nodes[u].kind == "decision" and k == 1) else 0)
    # 同行同列冲突：后来者右移
    taken: Dict[Tuple[int, int], str] = {}
    for u in order:
        key = (row[u], col[u])
        while key in taken:
            col[u] += 1
            key = (row[u], col[u])
        taken[key] = u

    for n in nodes.values():
        n.row, n.col = row[n.id], col[n.id]
        _size(n)

    # 步骤号：按 DFS 先序发给非开始/结束节点
    style = spec.get("step_style", "S101")
    k = 0
    for u in order:
        if nodes[u].kind in ("start", "end"):
            continue
        k += 1
        nodes[u].step = ("S%d" % (100 + k)) if style == "S101" else ("S%d" % k)

    # 坐标（1:1，y 向上）
    n_rows = max(n.row for n in nodes.values()) + 1
    row_h = [max([n.h for n in nodes.values() if n.row == r] or [0]) for r in range(n_rows)]
    y_center = []
    y = 0.0
    for r in range(n_rows):
        y -= row_h[r] / 2
        y_center.append(y)
        y -= row_h[r] / 2 + ROW_GAP
    col_x = lambda c: c * (BOX_W + COL_GAP)
    for n in nodes.values():
        n.x, n.y = col_x(n.col), y_center[n.row]

    # 连线：线只走「行间通道」（两行之间的空隙）与「走线道」（列间空隙、最左侧），不穿框
    def channel_below(r: int) -> float:
        return y_center[r] - row_h[r] / 2 - ROW_GAP / 2

    lane_use: Dict[Tuple[str, int], int] = {}

    def lane_x(kind: str, c: int) -> float:
        k = lane_use.get((kind, c), 0)
        lane_use[(kind, c)] = k + 1
        if kind == "left":
            return col_x(0) - BOX_W / 2 - LANE_GAP * (k + 1)
        if kind == "outer":
            right = max(n.right + STEP_GAP + _text_w(n.step or "S000", STEP_H) for n in nodes.values())
            return right + LANE_GAP * (k + 1)
        return col_x(c) + BOX_W / 2 + COL_GAP * LANE_K + SLOT * k

    def blocked(c: int, r0: int, r1: int) -> bool:
        lo, hi = min(r0, r1), max(r0, r1)
        return any(n.col == c and lo < n.row < hi for n in nodes.values())

    edges: List[Edge] = []
    for u in order:
        a = nodes[u]
        for k, e in enumerate(outgoing[u]):
            b = nodes[e["to"]]
            ed = Edge(a.id, b.id, str(e.get("label", "") or ""), back=(a.id, b.id) in back)
            side = a.kind == "decision" and k == 1      # 判断框的第二分支从右顶点出
            if ed.back:
                if a.col == 0:
                    sx, sy = (a.left, a.y)
                    lx = lane_x("left", 0)
                    ed.points = [(sx, sy), (lx, sy), (lx, b.y), (b.left, b.y)]
                    ed.label_at = (sx - 1.0 - _text_w(ed.label, LABEL_H), sy + LABEL_H * 0.9)
                else:
                    # 右侧外道回环：→ 最右走线道上行 → 目标上方通道 → 自顶并入
                    rx = lane_x("outer", 0)
                    ty = channel_below(b.row - 1) + SLOT if b.row > 0 else b.top + ROW_GAP / 2
                    right_free = not any(n.row == a.row and n.col > a.col for n in nodes.values())
                    if a.kind == "decision" and right_free:
                        # 判断框底部留给「是」分支：回环从右顶点出线，两个标签不叠
                        ed.points = [(a.right, a.y), (rx, a.y), (rx, ty), (b.x, ty), (b.x, b.top)]
                        ed.label_at = (a.right + 2.0, a.y + LABEL_H * 0.9)
                    else:
                        cy = channel_below(a.row) - SLOT
                        ed.points = [(a.x, a.bottom), (a.x, cy), (rx, cy), (rx, ty), (b.x, ty),
                                     (b.x, b.top)]
                        ed.label_at = (a.x + 2.0, a.bottom - LABEL_H * 0.9)
            elif side:
                sx, sy = (a.right, a.y)
                ed.label_at = (sx + 2.0, sy + LABEL_H * 0.9)
                if b.col > a.col and not blocked(b.col, a.row - 1, b.row) and b.row > a.row:
                    ed.points = [(sx, sy), (b.x, sy), (b.x, b.top)]
                else:
                    lx = lane_x("right", a.col)
                    ty = channel_below(b.row - 1) if b.row > 0 else b.top + ROW_GAP / 2
                    ed.points = [(sx, sy), (lx, sy), (lx, ty), (b.x, ty), (b.x, b.top)]
            elif b.col == a.col and b.row == a.row + 1:
                ed.points = [(a.x, a.bottom), (b.x, b.top)]
                ed.label_at = (a.x + 2.0, a.bottom - LABEL_H * 0.9)
            else:
                cy = channel_below(a.row)
                ty = channel_below(b.row - 1)
                ed.label_at = (a.x + 2.0, a.bottom - LABEL_H * 0.9)
                if b.row == a.row + 1:
                    ed.points = [(a.x, a.bottom), (a.x, cy), (b.x, cy), (b.x, b.top)]
                else:
                    lc = min(a.col, b.col)
                    lx = lane_x("right", lc)
                    ed.points = [(a.x, a.bottom), (a.x, cy), (lx, cy), (lx, ty), (b.x, ty),
                                 (b.x, b.top)]
            # 去掉零长度段
            pts = [ed.points[0]]
            for p in ed.points[1:]:
                if abs(p[0] - pts[-1][0]) > 1e-6 or abs(p[1] - pts[-1][1]) > 1e-6:
                    pts.append(p)
            ed.points = pts
            edges.append(ed)

    # 包围盒（含步骤号、走线道）
    xs, ys = [], []
    for n in nodes.values():
        xs += [n.left, n.right + (STEP_GAP + _text_w(n.step, STEP_H) if n.step else 0)]
        ys += [n.top, n.bottom]
    for ed in edges:
        for px, py in ed.points:
            xs.append(px); ys.append(py)
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    for n in nodes.values():
        n.x -= x0; n.y -= y0
    for ed in edges:
        ed.points = [(px - x0, py - y0) for px, py in ed.points]
        if ed.label_at:
            ed.label_at = (ed.label_at[0] - x0, ed.label_at[1] - y0)
    width, height = x1 - x0, y1 - y0

    avail_h = FRAME_H - 3 * CAPTION_H
    fit = min(1.0, FRAME_W / width, avail_h / height)
    # 整图（框、间距、文字）同比例缩放；字高不得低于交付下限，所以缩放也不低于 floor/TEXT_H。
    # 以前只把文字保底、框照缩，文字和步骤号就会溢出框外互相压住。
    s_min = TEXT_FLOOR_MM / TEXT_H
    scale = max(fit, s_min)
    warnings = []
    floor_hit = fit < s_min
    if floor_hit:
        warnings.append("流程过长：要放进 A4 可用区需缩到字高 %.1f mm（低于 %.1f mm），已保持 %.1f mm "
                        "并超出页面——建议拆成两张流程图" % (TEXT_H * fit, TEXT_FLOOR_MM, TEXT_FLOOR_MM))
    return Solved(nodes, order, edges, width, height, scale, floor_hit,
                  str(spec.get("title", "")), warnings)


# --------------------------------------------------------------------------- #
# 写 DXF                                                                        #
# --------------------------------------------------------------------------- #

def _round_rect(cx, cy, w, h, n=8):
    r = h / 2
    pts = []
    for cxx, a0 in ((cx + w / 2 - r, -90), (cx - w / 2 + r, 90)):
        for i in range(n + 1):
            a = math.radians(a0 + 180 * i / n)
            pts.append((cxx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _shape(n: Node, s: float, ox: float, oy: float):
    cx, cy, w, h = ox + n.x * s, oy + n.y * s, n.w * s, n.h * s
    if n.kind in ("start", "end"):
        return _round_rect(cx, cy, w, h)
    if n.kind == "decision":
        return [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)]
    if n.kind == "io":
        k = 5.0 * s
        return [(cx - w / 2 + k, cy + h / 2), (cx + w / 2, cy + h / 2),
                (cx + w / 2 - k, cy - h / 2), (cx - w / 2, cy - h / 2)]
    return [(cx - w / 2, cy + h / 2), (cx + w / 2, cy + h / 2),
            (cx + w / 2, cy - h / 2), (cx - w / 2, cy - h / 2)]


def write(spec: dict, out: Path, figure_number: Optional[int] = None) -> dict:
    sol = solve(spec)
    s = sol.scale
    th = max(TEXT_FLOOR_MM, TEXT_H * s)
    sh = max(TEXT_FLOOR_MM, STEP_H * s)
    lh = max(TEXT_FLOOR_MM, LABEL_H * s)
    w, h = sol.width * s, sol.height * s
    cap_band = 3 * CAPTION_H            # 图号紧贴流程图下方；图号 + 流程图整体在可用区内居中
    block_bottom = (FRAME_H - h - cap_band) / 2   # 超出页面时为负：整图向下延伸，报告里已告警
    ox = (FRAME_W - w) / 2
    oy = block_bottom + cap_band
    cap_y = block_bottom + CAPTION_H * 1.2

    doc = ezdxf.new("R2018", setup=False)
    doc.header["$INSUNITS"] = _sheet.INSUNITS_MM
    for layer in ("GEOM", "LEADER", "NUM", "NOTE", "CAPTION", "TEXT"):
        doc.layers.add(layer, linetype="CONTINUOUS")
    doc.styles.add(_sheet.STYLE_HZ, font=_sheet.STYLE_HZ_FONT)
    doc.styles.add(_sheet.STYLE_NUM, font=_sheet.STYLE_NUM_FONT)
    msp = doc.modelspace()
    geom = {"layer": "GEOM", "linetype": "CONTINUOUS"}

    def text(value, x, y, height, layer, style, align=TextEntityAlignment.MIDDLE_CENTER):
        t = msp.add_text(value, height=height,
                         dxfattribs={"layer": layer, "style": style, "linetype": "CONTINUOUS"})
        t.set_placement((x, y), align=align)

    steps = []
    for nid in sol.order:
        n = sol.nodes[nid]
        msp.add_lwpolyline(_shape(n, s, ox, oy), close=True, dxfattribs=geom)
        cx, cy = ox + n.x * s, oy + n.y * s
        k = len(n.lines)
        for i, line in enumerate(n.lines):
            dy = ((k - 1) / 2 - i) * LINE_GAP * th
            text(line, cx, cy + dy, th, "TEXT", _sheet.STYLE_HZ)
        if n.step:
            if n.kind == "decision":      # 右顶点留给「否」出线，步骤号放到右上斜边外
                sx_, sy_ = ox + (n.x + n.w / 4) * s + STEP_GAP, cy + n.h / 4 * s + sh * 0.6
            else:
                sx_, sy_ = ox + n.right * s + STEP_GAP, cy
            text(n.step, sx_, sy_, sh, "NUM", _sheet.STYLE_NUM, TextEntityAlignment.MIDDLE_LEFT)
            steps.append({"id": n.id, "step": n.step, "kind": n.kind, "text": n.text})

    for ed in sol.edges:
        pts = [(ox + x * s, oy + y * s) for x, y in ed.points]
        (ax, ay), (bx, by) = pts[-2], pts[-1]
        L = math.hypot(bx - ax, by - ay) or 1.0
        ux, uy = (bx - ax) / L, (by - ay) / L
        al, aw = ARROW_L * max(s, 0.75), ARROW_W * max(s, 0.75)
        base = (bx - ux * al, by - uy * al)
        msp.add_lwpolyline(pts[:-1] + [base], dxfattribs=geom)
        msp.add_solid([(bx, by), (base[0] - uy * aw, base[1] + ux * aw),
                       (base[0] + uy * aw, base[1] - ux * aw)],
                      dxfattribs=geom)
        if ed.label and ed.label_at:
            text(ed.label, ox + ed.label_at[0] * s, oy + ed.label_at[1] * s, lh, "NOTE",
                 _sheet.STYLE_HZ, TextEntityAlignment.MIDDLE_LEFT)

    caption = ("图%d" % figure_number) if figure_number else (sol.title or "流程图")
    text(caption, FRAME_W / 2, cap_y, CAPTION_H, "CAPTION", _sheet.STYLE_HZ)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(out))
    return {
        "schema": "patent-flowchart-result/1",
        "dxf": str(out),
        "caption": caption,
        "title": sol.title,
        "figure_description": ("图%d为%s；" % (figure_number, sol.title))
        if figure_number and sol.title else "",
        "steps": steps,
        "scale": round(s, 4),
        "text_height_mm": round(th, 3),
        "warnings": sol.warnings,
    }
