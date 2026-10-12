#!/usr/bin/env python3
"""墨线 Web 工作台 · 前端开发用 mock 服务器（纯标准库，Python 3.9+）。

WP3b 起按 docs/openapi.yaml（WP1 + WP2 合并后的真实契约）对齐：能力接口 /v1/capabilities、作业 SSE 帧
（id / event / data，data = {stage, progress, data}）、/p/{pid}/ 新版子应用（api/state、api/render/status、
api/preview、api/model.glb、api/structure）、候选分图作业 + /plan/candidates + /plan/candidates/accept、
导出 ExportResult（items / documents / unverified）、AI 起名 apply 语义。真实后端的端到端见 e2e_live.py。

两种实例：
  默认            新版工作台（有 /v1/capabilities → 前端判 live；/p/{pid}/ 是新版子应用）
  --old-backend   旧版工作台（/v1/capabilities 404 → 前端判 legacy；/p/{pid}/ 是旧同步 /api/* 子应用）
覆盖：

* 作业的分阶段 SSE 进度（接收 / 解析 / 网格 / 候选分图 / 试渲染）、可重连、带 event id；
* plan 的 ETag 乐观锁与 409 冲突；
* 失败场景：闸门未过（bench-vise 整机候选被拦）、AI 未配置、DWG 不可用；
* 三个公开示例工程的真实资产（GLB / SVG / QA JSON / 件号表，来自 webui/dev/fixtures/，
  由现有 CLI 生成，只作 mock 数据）；
* 渲染作业按 WP1 契约：结果带版本 ID（render_job_id / version）；未全部过闸或计划未过校验的渲染是
  **失败作业**（error + diag），不发布，已发布版本保持上一成功版本；导出须带 render_job_id + plan_etag，
  与被导出渲染的冻结输入原子比对，不符 409；
* /p/{id}/ + /api/* 子应用（供前端的「旧版后端兼容模式」= api.js 的 legacy 分支）：
  api/render/status 按 openapi RenderStatus 返回 {running, result=已发布, version, job=本次作业}。

OpenAPI（docs/openapi.yaml）WP1 尚未提供，本 mock 的契约为自拟，差异点见 webui/dev/README.md。

启动：
    python3 webui/dev/mock_server.py --port 8803                 # 新版
    python3 webui/dev/mock_server.py --port 8805 --old-backend   # 旧版（兼容模式回归用）
然后浏览器打开 http://127.0.0.1:8803/home.html
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent          # webui/dev
WEBUI = HERE.parent                             # webui
FIX = HERE / "fixtures"

mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/javascript", ".js")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
#  示例工程定义：合成的结构组 / 候选分图 + fixtures 里的真实资产                 #
# --------------------------------------------------------------------------- #
GEARBOX_NAMES = {
    "PGB-MOTOR-HOUSING": "电机壳体", "PGB-MOTOR-REAR-CAP": "电机后盖",
    "PGB-MOUNT-FLANGE": "安装法兰", "PGB-RING-GEAR": "内齿圈", "PGB-END-COVER": "端盖",
    "PGB-OUTPUT-SHAFT": "输出轴", "PGB-SUN-GEAR": "太阳轮", "PGB-PLANET-GEAR": "行星轮",
    "PGB-PLANET-PIN": "行星销", "PGB-PLANET-CARRIER": "行星架",
    "PGB-OUTPUT-BEARING": "输出轴承", "PGB-OIL-SEAL": "油封",
}
GEARBOX_STRUCTURE = {
    "source": "ai", "provider": "DeepSeek V3.2", "created": "2026-10-11 10:22",
    "groups": [
        {"id": "g-motor", "name": "电机子装配", "role": "提供旋转动力",
         "parts": ["PGB-MOTOR-HOUSING", "PGB-MOTOR-REAR-CAP", "PGB-MOUNT-FLANGE"]},
        {"id": "g-gear", "name": "行星齿轮系", "role": "减速与扭矩放大",
         "parts": ["PGB-SUN-GEAR", "PGB-PLANET-GEAR", "PGB-PLANET-PIN",
                   "PGB-PLANET-CARRIER", "PGB-RING-GEAR"]},
        {"id": "g-output", "name": "输出与密封", "role": "输出轴承、端盖与密封",
         "parts": ["PGB-OUTPUT-SHAFT", "PGB-OUTPUT-BEARING", "PGB-END-COVER", "PGB-OIL-SEAL"]},
        {"id": "g-std", "name": "标准件", "role": "紧固螺钉（默认不发标记）",
         "parts": ["PGB-SCREW-M3", "PGB-SCREW-M4"]},
    ],
    "names": {k: {"name": v, "confidence": "high"} for k, v in GEARBOX_NAMES.items()},
}
# 低置信样例：行星销标低，演示 AI 起名浮条里的黄标
GEARBOX_STRUCTURE["names"]["PGB-PLANET-PIN"] = {"name": "行星销", "confidence": "low"}

GEARBOX_PLAN = {
    "schema": "patent-figure-plan/1",
    "source": {"step": "demo-planetary-gearbox.stp", "include": ["PGB-*"], "exclude": []},
    "terms": [{"selector": k, "term": v} for k, v in GEARBOX_NAMES.items()]
             + [{"selector": "PGB-SCREW-*", "term": "紧固螺钉", "label": "none"}],
    "figures": [
        {"id": "fig1", "caption": "行星齿轮减速器整体结构示意图", "kind": "assembly",
         "members": ["PGB-MOTOR-HOUSING", "PGB-MOTOR-REAR-CAP", "PGB-MOUNT-FLANGE",
                     "PGB-RING-GEAR", "PGB-END-COVER", "PGB-OUTPUT-SHAFT", "PGB-SCREW-*"]},
        {"id": "fig2", "caption": "行星齿轮减速器齿轮系分解示意图", "kind": "exploded",
         "members": ["PGB-SUN-GEAR", "PGB-PLANET-GEAR", "PGB-PLANET-PIN", "PGB-PLANET-CARRIER",
                     "PGB-RING-GEAR", "PGB-OUTPUT-SHAFT", "PGB-OUTPUT-BEARING",
                     "PGB-END-COVER", "PGB-OIL-SEAL"]},
    ],
    "layout": {"max_labels_per_figure": 20},
}

# bench-vise：整机候选被闸门拦下（教学用「故意失败」）。构造一张失败 QA。
VISE_NAMES = {
    "VISE-BASE": "钳座", "VISE-FIXED-JAW": "固定钳身", "VISE-MOVABLE-JAW": "活动钳身",
    "VISE-JAW-PLATE": "钳口板", "VISE-LEAD-SCREW": "丝杠", "VISE-LEAD-NUT": "丝杠螺母",
    "VISE-HANDLE-BAR": "手柄杆", "VISE-HANDLE-HUB": "手柄座", "VISE-HANDLE-KNOB": "手柄球",
    "VISE-THRUST-COLLAR": "止推环", "VISE-BASE-BOLT": "底座螺栓", "VISE-PLATE-SCREW": "钳口板螺钉",
}
VALVE_NAMES = {
    "BV-BODY": "阀体", "BV-BALL": "球芯", "BV-SEAT": "阀座", "BV-STEM": "阀杆",
    "BV-PACKING": "填料", "BV-HANDLE": "手柄", "BV-FLANGE": "法兰", "BV-NUT": "锁紧螺母",
    "BV-BONNET": "阀盖",
}


def numeral_table(numerals_json, fig_id):
    rows = []
    for n in numerals_json["numerals"]:
        if fig_id in n.get("figures", []):
            rows.append({"numeral": n["numeral"], "name": n["term"]})
    rows.sort(key=lambda r: r["numeral"])
    return rows


def build_gearbox():
    numerals = load(FIX / "gearbox/reference-numerals.json")
    # 解析每个 numeral 对应的零件（供 SVG data-numeral 点击联动）
    for n in numerals["numerals"]:
        sel = n["selector"]
        n["parts"] = [p for p in GEARBOX_NAMES if re.fullmatch(sel.replace("*", ".*"), p)] or [sel]
    figs = []
    for fid, num in (("fig1", 1), ("fig2", 2)):
        qa = load(FIX / f"gearbox/{fid}.qa.json")
        figs.append({
            "id": fid, "number": num, "pass": bool(qa.get("pass")),
            "caption": GEARBOX_PLAN["figures"][num - 1]["caption"],
            "kind": GEARBOX_PLAN["figures"][num - 1]["kind"],
            "qa": qa,
            "svg": f"/v1/projects/gearbox/preview/{fid}.svg",
            "variants": {
                "annotated": f"/v1/projects/gearbox/preview/{fid}_annotated.svg",
                "filing": f"/v1/projects/gearbox/preview/{fid}_filing.svg",
                "raw": f"/v1/projects/gearbox/preview/{fid}.svg",
            },
            "annotation": {"table": {"rows": numeral_table(numerals, fid),
                                     "corner": "左下", "blocks": 1},
                           "warnings": []},
        })
    descs = ["图1 为行星齿轮减速器的整体结构示意图。",
             "图2 为行星齿轮减速器齿轮系的分解示意图。"]
    return {
        "assembly": load(FIX / "gearbox/assembly.json"),
        "structure": GEARBOX_STRUCTURE, "plan": copy.deepcopy(GEARBOX_PLAN),
        # inputs.plan_etag：结果绑定的输入版本证据（与 seed 的初始 plan_etag 一致）
        "render": {"ok": True, "figures": figs, "numerals": numerals,
                   "figure_descriptions": descs, "finished_at": "2026-10-11 10:31",
                   "inputs": {"plan_etag": 'W/"gearbox-1"'}},
        # 候选成员用稳定引用（结构组 id 或真实零件 selector），显示名单独放 members_display
        "candidates": [
            {"fig_id": "fig1", "caption": "整体结构示意图", "kind": "assembly",
             "members": [{"group": "g-motor"}, {"group": "g-output"}, {"part": "PGB-RING-GEAR"}],
             "members_display": ["电机子装配", "输出与密封", "内齿圈"], "status": "pass",
             "reason": "字高 7.0 mm、占用 0.79，13 项 QA 全过", "labels": 6},
            {"fig_id": "fig2", "caption": "齿轮系分解示意图", "kind": "exploded",
             "members": [{"group": "g-gear"}, {"part": "PGB-OUTPUT-SHAFT"}],
             "members_display": ["行星齿轮系", "输出轴"], "status": "pass",
             "reason": "沿轴 121.9° 爆炸，9 个标记无交叉，13 项 QA 全过", "labels": 9},
        ],
    }


def failing_qa(numerals_rows):
    """构造一张未过闸的 QA：字高不足 + 标记过密 + 引线交叉（真实 hint 文案）。"""
    def chk(cid, ok, value, thr, detail, hint):
        return {"id": cid, "pass": ok, "value": value, "threshold": thr,
                "detail": detail, "hint": hint}
    checks = [
        chk("geometry_occupancy", True, 0.71, ">=0.55", "几何占用 0.71", ""),
        chk("sheet_fill", True, 0.63, ">=0.55", "纸面填充 0.63", ""),
        chk("text_height_mm", False, 2.4, ">=3.5",
            "NUM 图层最小字高 2.40 mm（共 22 个标记）",
            "字太小意味着被标注件在图面上太小：对标准件设 terms[].label=\"none\"，"
            "按 assembly.json:split_suggestions 拆图，或把 layout.density 改成 \"compact\"。"),
        chk("text_slot_ratio", False, 0.74, "<=0.6",
            "最小被标注几何簇外廓 18.0 mm，最小字高 2.40 mm",
            "被标注件相对字号太小：对该零件设 terms[].label=\"none\"，"
            "或按 assembly.json:split_suggestions 拆图。"),
        chk("label_overlap_pairs", True, 0, "<=0", "无标记互相碾压", ""),
        chk("leader_hits_numeral_box", True, 0, "<=0", "引线未压数字", ""),
        chk("leader_crossing", False, 3, "<=0", "检测到 3 对引线交叉",
            "引线交叉：减少本图标记数（terms[].label=\"none\"）或按 "
            "assembly.json:split_suggestions 拆图。"),
        chk("leader_anchor_gap", True, 0.4, "<=0.5", "引线均落在轮廓上", ""),
        chk("part_bbox_overlap_pairs", True, 0, "<=0", "零件无压叠", ""),
        chk("labels_per_figure", False, 22, "<=20", "本图 22 个标记，超过上限 20",
            "拆图：按 assembly.json:split_suggestions 把该 figure 拆成多张，"
            "或对标准件设 terms[].label=\"none\" 减少标记。"),
        chk("non_numeral_text_ratio", True, 0.0, "<=0.05", "无多余文字", ""),
        chk("non_continuous", True, 0, "<=0", "全部实体连续线型", ""),
        chk("forbidden_text", True, 0, "==0", "无内部件号 / 图号形状字符串", ""),
    ]
    passed = sum(1 for c in checks if c["pass"])
    return {"schema": "patent-figure-qa/1", "file": "whole.dxf", "pass": False,
            "checks": checks, "summary": {"passed": passed, "failed": len(checks) - passed}}


def build_vise():
    asm = load(FIX / "bench-vise/assembly.json")
    names = VISE_NAMES
    rows = [{"numeral": i + 1, "name": names.get(p["name"], p["name"])}
            for i, p in enumerate(asm["parts"]) if names.get(p["name"])]
    return {
        "assembly": asm,
        "structure": {
            "source": "ai", "provider": "DeepSeek V3.2", "created": "2026-10-11 10:40",
            "groups": [
                {"id": "v-fixed", "name": "固定钳身组", "role": "固定与基座",
                 "parts": ["VISE-BASE", "VISE-FIXED-JAW", "VISE-BASE-BOLT"]},
                {"id": "v-move", "name": "活动钳身组", "role": "夹持与移动",
                 "parts": ["VISE-MOVABLE-JAW", "VISE-JAW-PLATE", "VISE-PLATE-SCREW"]},
                {"id": "v-screw", "name": "丝杠手柄组", "role": "驱动夹紧",
                 "parts": ["VISE-LEAD-SCREW", "VISE-LEAD-NUT", "VISE-THRUST-COLLAR",
                           "VISE-HANDLE-BAR", "VISE-HANDLE-HUB", "VISE-HANDLE-KNOB"]},
            ],
            "names": {k: {"name": v, "confidence": "high"} for k, v in names.items()},
        },
        "plan": {"schema": "patent-figure-plan/1",
                 "source": {"step": "demo-bench-vise.stp", "include": ["VISE-*"], "exclude": []},
                 "terms": [{"selector": k, "term": v} for k, v in names.items()],
                 "figures": [], "layout": {"max_labels_per_figure": 20}},
        "render": None,
        "candidates": [
            {"fig_id": "whole", "caption": "台虎钳整机装配图（兜底候选）", "kind": "assembly",
             "members": [{"group": "v-fixed"}, {"group": "v-move"}, {"group": "v-screw"}],
             "members_display": ["固定钳身组", "活动钳身组", "丝杠手柄组"], "status": "fail",
             "qa": failing_qa(rows),
             "reason": "整机 22 个标记塞进一张图：字高 2.4<3.5 mm、标记 22>20、引线 3 处交叉。"
                       "建议按结构拆成「固定钳身 / 活动钳身 / 丝杠手柄」三张分解图。", "labels": 22},
            {"fig_id": "v-screw", "caption": "丝杠手柄机构分解图", "kind": "exploded",
             "members": [{"group": "v-screw"}], "members_display": ["丝杠手柄组"], "status": "pass",
             "reason": "6 个标记、字高 5.0 mm，13 项 QA 全过", "labels": 6},
        ],
    }


def build_valve():
    asm = load(FIX / "ball-valve/assembly.json")
    names = VALVE_NAMES
    return {
        "assembly": asm,
        "structure": {
            "source": "fallback", "provider": "", "created": "",
            "groups": [
                {"id": "bv-body", "name": "子装配 1", "role": "", "parts": [p["name"] for p in asm["parts"][:5]]},
                {"id": "bv-trim", "name": "子装配 2", "role": "", "parts": [p["name"] for p in asm["parts"][5:]]},
            ],
            "names": {},
        },
        "plan": {"schema": "patent-figure-plan/1",
                 "source": {"step": "demo-ball-valve.stp", "include": ["BV-*"], "exclude": []},
                 "terms": [{"selector": p["name"], "term": ""} for p in asm["parts"]],
                 "figures": [], "layout": {"max_labels_per_figure": 20}},
        "render": None,
        "candidates": [],   # 建议为空：界面必须提示「请框选分组」，不假装零点击
    }


# --------------------------------------------------------------------------- #
#  候选分图（openapi Candidate / SuggestResult）：members 只放真实零件 {part}        #
# --------------------------------------------------------------------------- #
QA_PLAIN = {   # 与 scripts/patent_figure/qa.py CHECK_PLAIN 同义（mock 只用到这几项）
    "text_height_mm": ("标号字高", "标号的字太小，打印出来看不清。把螺钉等标准件设为不标号，或把这张图拆成几张。",
                       {"kind": "open_terms", "target": {"focus": "standard_parts"}}),
    "labels_per_figure": ("标号数量", "这张图的标号超过上限。拆成几张图，或把标准件设为不标号。", {"kind": "split_figure", "target": {}}),
    "leader_crossing": ("引线交叉", "引线互相交叉，读图时容易认错零件。减少标号或把这张图拆开。", {"kind": "split_figure", "target": {}}),
    "text_slot_ratio": ("零件与字号比例", "有的被标零件在图上太小、比标号还小。把这些小零件设为不标号，或单独给它们画一张放大的分图。",
                        {"kind": "split_figure", "target": {}}),
}


def cand(cid, caption, kind, parts, names, status, reason, *, source, labels, qa=None, suggestion=None,
         standard=(), ambiguous=(), preview=None):
    return {"id": cid, "fig_id": cid, "caption": caption, "kind": kind, "source": source,
            "members": [{"part": p} for p in parts], "members_display": [names.get(p, p) for p in parts],
            "labels": labels, "standard_parts": list(standard), "placeholder_terms": 0,
            "ambiguous_members": list(ambiguous), "status": status, "reason": reason, "detail": None,
            "suggestion": suggestion, "plan_errors": [],
            "qa": qa or {"pass": status == "pass", "failed": [], "text_height_mm": 7.0, "labels": labels},
            "version": "seed-%s" % cid, "preview": {"svg": preview} if preview else None}


def seed_candidates(pid, data):
    groups = {g["id"]: g["parts"] for g in (data.get("structure") or {}).get("groups", [])}
    if pid == "gearbox":
        names = GEARBOX_NAMES
        return [
            cand("c1", "行星架组件分解示意图", "exploded", groups["g-gear"] + ["PGB-OUTPUT-SHAFT"], names, "pass",
                 "13 项质量检查全部通过：字高 7.0 mm，6 个标号", labels=6,
                 source={"kind": "subassembly", "id": "sub:PGB/CARRIER", "label": "子装配 行星齿轮系"},
                 ambiguous=[{"part": "PGB-PLANET-GEAR", "selected": 3, "in_group": None, "outside_paths": [],
                             "message": "按名称选中 3 处（同轴组只含其中一部分）"}],
                 preview="/v1/projects/gearbox/preview/fig2_annotated.svg"),
            cand("c2", "电机与输出组件分解示意图", "exploded", groups["g-motor"] + groups["g-output"] + ["PGB-RING-GEAR"], names,
                 "pass", "13 项质量检查全部通过：字高 5.0 mm，8 个标号", labels=8,
                 source={"kind": "coaxial", "id": "coax:g1", "label": "同轴组 g1"}, standard=["PGB-OUTPUT-BEARING"],
                 preview="/v1/projects/gearbox/preview/fig1_annotated.svg"),
        ], None
    if pid == "bench-vise":
        names = VISE_NAMES
        every = [p for g in groups.values() for p in g]
        failed = [{"id": k, "title_zh": QA_PLAIN[k][0], "hint_plain": QA_PLAIN[k][1],
                   "fix_action": dict(QA_PLAIN[k][2], target=dict(QA_PLAIN[k][2]["target"], figure="c2"))}
                  for k in ("text_height_mm", "labels_per_figure", "leader_crossing")]
        return [
            cand("c1", "整体结构示意图", "assembly", every, names, "fail",
                 "被闸门拦下：整组图形太扁或太长，铺不满纸面（填充 0.308，要求不低于 0.55）", labels=10,
                 source={"kind": "whole", "id": "whole", "label": "整机装配图（兜底候选）"},
                 suggestion="一张图放不下这些零件（字太小或图形太扁）：改用分组更小的候选，或在 3D 里框选分组；标准件已默认不标号。",
                 qa={"pass": False, "failed": [], "error_code": "E_LAYOUT_UNSOLVABLE"},
                 standard=["VISE-BASE-BOLT", "VISE-PLATE-SCREW"]),
            cand("c2", "整体分解示意图", "exploded", every, names, "fail",
                 "未过闸：标号字高——标号的字太小，打印出来看不清。", labels=22,
                 source={"kind": "axis", "id": "axis", "label": "沿主轴（x）整机分解"},
                 suggestion="把螺钉等标准件设为不标号，或按子装配拆成几张分图。",
                 qa={"pass": False, "failed": failed, "text_height_mm": 2.4, "labels": 22},
                 standard=["VISE-BASE-BOLT", "VISE-PLATE-SCREW"]),
            cand("c3", "丝杠组件分解示意图", "exploded", groups["v-screw"], names, "pass",
                 "13 项质量检查全部通过：字高 5.0 mm，6 个标号", labels=6,
                 source={"kind": "subassembly", "id": "sub:VISE/SPINDLE", "label": "子装配 丝杠手柄组"}),
        ], None
    if pid == "ball-valve":
        every = [p["name"] for p in (data.get("assembly") or {}).get("parts", [])]
        return [
            cand("c1", "整体结构示意图", "assembly", every, {}, "fail",
                 "被闸门拦下：标号太多，找不到互不交叉、不压线的位置", labels=9,
                 source={"kind": "whole", "id": "whole", "label": "整机装配图（兜底候选）"},
                 suggestion="标号太多、无处可放：改用分组更小的候选，或框选分组。",
                 qa={"pass": False, "failed": [], "error_code": "E_LABELS_UNPLACEABLE"}),
        ], "没有候选通过闸门：这台装配体的子装配层级不够清晰，程序给不出能过闸的分组——需要你在 3D 里框选分组。"
    return [], None


GUIDE = {"action": "box_select", "title": "请框选分组",
         "steps": ["在 3D 视口按 B 进入框选，或在左侧结构树里点组名选中整组",
                   "框住要放进同一张图的零件（左→右窗选 / 右→左交叉选）",
                   "到「图纸」页点「＋新建图」，或在图卡上点「加入所选」，然后出图"]}


def suggest_result(pid, p, cands, empty_reason, job_id):
    n_pass = sum(1 for c in cands if c["status"] == "pass")
    if not n_pass and not empty_reason:
        empty_reason = "没有候选通过闸门——需要你在 3D 里框选分组。"
    return {"schema": "patent-figure-candidates/1", "project_id": pid, "job_id": job_id,
            "plan_etag": norm_etag(p["plan_etag"]), "assembly_sha256": "mock-%s" % pid,
            "standard_part_rule": {"pattern": "SCREW|BOLT|NUT|WASHER|PIN|BEARING|螺钉|螺栓|螺母|垫圈|销|轴承",
                                   "except": "(?:LEAD|BALL)[-_ ]?(?:SCREW|NUT)|丝杠|丝母", "whole_word_latin": True},
            "label_standard_parts": False, "standard_parts": sorted({s for c in cands for s in c["standard_parts"]}),
            "instances_exact": True, "candidates": cands,
            "summary": {"total": len(cands), "pass": n_pass, "fail": len(cands) - n_pass, "pending": 0},
            "empty_reason": empty_reason if not n_pass else None, "guide": GUIDE if not n_pass else None,
            "warnings": []}


# --------------------------------------------------------------------------- #
#  内存态                                                                      #
# --------------------------------------------------------------------------- #
GLB_BY_PROJECT = {"gearbox": "planetary-gearbox.glb", "bench-vise": "bench-vise.glb",
                  "ball-valve": "ball-valve.glb"}

PROJECTS = {}        # id -> runtime dict
JOBS = {}            # job_id -> job dict
VERSIONS = {}        # 已发布（成功）渲染版本 rid -> {"project", "plan_etag"}；导出只认这里的版本
LOCK = threading.Lock()
OLD_BACKEND = False  # --old-backend：模拟旧版工作台（没有 /v1/capabilities）

# AI 通道：默认已配置；--no-ai 场景下置 none
LLM = {"provider": "deepseek", "provider_label": "DeepSeek V3.2（API）", "configured": True}
FLAGS = {"dwg": True, "pdf_check": True}
CNIPA_NOTE = ("按国家知识产权局专利审查指南与《电子申请文件格式要求说明》的附图形式要求排版（A4、黑白线条、图号、"
              "页边与字高下限）；精确图片参数以当期《电子申请文件格式要求说明》为准，最终以受理为准。")
OUTBOUND_NOTE = ("AI 起名会把选中零件的 STEP 零件名（常含内部件号）、包围盒尺寸与装配路径发给所配置的大模型通道；"
                 "几何模型本身不出本机。")


def capabilities():
    reasons = {}
    if not FLAGS["dwg"]:
        reasons["dwg"] = "未检测到 DWG 转换器（AutoCAD Core Console 或 LibreDWG）"
    if not FLAGS["pdf_check"]:
        reasons["pdf"] = "缺少 pypdfium2，无法核验 PDF 可见性：PDF 能生成，但导出结果为「未核验」、不报通过（python3 -m pip install pypdfium2）"
    return {
        "display_name": "墨线",
        "export": {
            "formats": ["pdf", "docx", "dxf", "dwg"], "primary_formats": ["pdf", "docx"], "source_formats": ["dxf", "dwg"],
            "variants": {"filing": "递交版（仅图号）", "disclosure": "交底版（图号 + 件号表）", "both": "两版"},
            "pdf_available": True, "pdf_check_available": FLAGS["pdf_check"],
            "pdf_render_check_available": FLAGS["pdf_check"], "pdf_renderer": "pypdfium2" if FLAGS["pdf_check"] else None,
            "pdf_render_required": True, "docx_available": True, "dwg_available": FLAGS["dwg"],
            "dwg_route": "libredwg" if FLAGS["dwg"] else None, "unavailable_reasons": reasons,
            "sheet_presets": {"standard": {"area_mm": [170.0, 257.0], "label": "标准版心 170×257 mm"},
                              "conservative": {"area_mm": [165.0, 245.0], "label": "保守版心 165×245 mm"}},
            "default_preset": "standard", "dpi": {"default": 300, "min": 72, "max": 600},
            "page": {"size_mm": [210.0, 297.0], "margins_mm": {"top": 25.0, "left": 25.0, "bottom": 15.0, "right": 15.0}},
            "min_text_mm": 3.5, "min_text_px": 24, "note": CNIPA_NOTE,
            "layout_verification": {"pdf": "file-geometry", "docx": "structural",
                                    "docx_note": "Word 按文档结构核验，实际排版以 Word 打开为准"},
            "statuses": ["ok", "skip", "fail", "unverified"],
            "integrity": {"algorithm": "sha256", "note": "核验只对导出包原文件有效"},
            "files": ["说明书附图_递交版.pdf", "说明书附图_递交版.docx", "说明书附图_交底版.pdf",
                      "说明书附图_交底版.docx", "附图说明.txt", "核对清单.txt", "核验报告.json"]},
        "ai": {"configured": LLM["configured"], "provider": LLM["provider"], "provider_label": LLM["provider_label"],
               "outbound_fields": ["part_names", "part_codes"], "outbound_note": OUTBOUND_NOTE},
        "suggest": {"max_candidates": 8, "max_candidates_hard": 16},
        "examples": ["gearbox", "valve", "vise"],
        "auth": {"bearer": True, "session_cookie": "wb_token", "query_token": "deprecated"},
    }


def seed():
    defs = {
        "gearbox": dict(title="行星齿轮减速器", desc="电机 + 行星齿轮系 + 轴承端盖，同轴堆叠",
                        step="demo-planetary-gearbox.stp", example=True, status="ready",
                        data=build_gearbox()),
        "bench-vise": dict(title="台虎钳", desc="固定钳身 / 活动钳身 / 丝杠手柄三个子装配",
                           step="demo-bench-vise.stp", example=True, status="ready",
                           data=build_vise()),
        "ball-valve": dict(title="球阀", desc="法兰阀体、开孔球芯、阀座、阀杆填料与手柄",
                           step="demo-ball-valve.stp", example=True, status="ready",
                           data=build_valve()),
    }
    for pid, d in defs.items():
        d["id"] = pid
        d["plan_etag"] = 'W/"%s-1"' % pid
        d["plan_rev"] = 1
        r = d["data"].get("render")
        if r:                                     # 种子结果 = 一个已发布版本
            rid = "seed-%s" % pid
            r["render_job_id"] = r["version"] = rid
            r.setdefault("inputs", {})["plan_etag"] = d["plan_etag"]
            VERSIONS[rid] = {"project": pid, "plan_etag": d["plan_etag"],
                             "figures": [f["id"] for f in r.get("figures", [])]}
        d["data"]["render_job"] = None
        d["data"].pop("candidates", None)
        cands, empty = seed_candidates(pid, d["data"])
        d["data"]["suggest_def"] = (cands, empty)
        PROJECTS[pid] = d
        # 种子：一次已成功的候选分图作业（GET /plan/candidates 返回它）
        jid = "seed-suggest-%s" % pid
        now = time.time()
        JOBS[jid] = {"job_id": jid, "kind": "suggest", "project_id": pid, "state": "succeeded", "stage": "succeeded",
                     "progress": 1.0, "logs": [], "inputs": {"plan_etag": norm_etag(d["plan_etag"])},
                     "result": suggest_result(pid, d, copy.deepcopy(cands), empty, jid), "error": None, "diag": None,
                     "created_at": now, "updated_at": now, "_final": None, "_events": [], "_next": 1,
                     "_cv": threading.Condition(), "_done": True}
        d["data"]["suggest_job"] = jid
    # 准备中 / 失败 两个占位工程，演示列表的加载态与错误态
    PROJECTS["demo-preparing"] = dict(id="demo-preparing", title="（我的）回转支承机构",
        desc="", step="swing-bearing.stp", example=False, status="preparing",
        message="正在解析装配层级…", data=None, plan_etag='W/"prep-0"', plan_rev=0)
    PROJECTS["demo-error"] = dict(id="demo-error", title="（我的）液压夹具",
        desc="", step="hydraulic-fixture.stp", example=False, status="error",
        message="STEP 顶层只有 1 个实体，未解析出零件——请确认导出的是装配体而非单件",
        data=None, plan_etag='W/"err-0"', plan_rev=0)


# --------------------------------------------------------------------------- #
#  作业与 SSE（WP1 帧：id / event / data，data = {stage, progress, data}）          #
# --------------------------------------------------------------------------- #
def stages_for(kind, project_id=None):
    if kind == "prepare":
        return [("prepare", "解析装配体与导出预览", 1.6)]
    if kind == "from-example":
        return [("from-example", "生成演示模型 STEP", 0.8), ("prepare", "解析装配体与导出预览", 1.0)]
    if kind == "suggest":
        cands = ((PROJECTS.get(project_id) or {}).get("data") or {}).get("suggest_def", ([], None))[0]
        n = len(cands)
        return [("suggest", "生成候选分图", 0.4)] + [
            ("suggest", "试渲染候选 %d/%d：%s" % (i + 1, n, c["caption"]), 0.5) for i, c in enumerate(cands)]
    if kind == "render":
        return [("render", "渲染中", 1.8), ("render", "标注与 13 项质量闸门", 1.0)]
    if kind == "export":
        return [("export", "生成附图包（PDF / Word / 源文件）", 1.4), ("export", "逐页核验完成，打包", 0.6)]
    return [(kind, kind, 0.5)]


def new_job(kind, project_id=None, fail_stage=None, speed=1.0, result=None, inputs=None, start=True):
    jid = "job_" + uuid.uuid4().hex[:10]
    now = time.time()
    job = {"job_id": jid, "kind": kind, "project_id": project_id,
           "state": "queued", "stage": "queued", "progress": 0.0, "logs": [],
           "inputs": dict(inputs or {}), "result": result, "error": None, "diag": None,
           "created_at": now, "updated_at": now, "_final": None,
           "_events": [], "_next": 1, "_cv": threading.Condition(), "_done": False}
    JOBS[jid] = job
    if start:
        threading.Thread(target=run_job, args=(job, kind, fail_stage, speed), daemon=True).start()
    return job


def _emit(job, etype, data=None, stage=None, progress=None):
    with job["_cv"]:
        if stage is not None:
            job["stage"] = stage
        if progress is not None:
            job["progress"] = progress
        ev = {"id": job["_next"], "event": etype, "stage": job["stage"], "progress": round(job["progress"], 4), "data": data}
        job["_next"] += 1
        job["_events"].append(ev)
        job["updated_at"] = time.time()
        if etype == "log":
            job["logs"].append(data or "")
        job["_cv"].notify_all()


def _finish(job):
    with job["_cv"]:
        job["_done"] = True
        job["_cv"].notify_all()


def job_dict(job):
    """openapi Job（与 WP1 Job.to_dict 同字段）：作业 ID 字段名是 id。"""
    return {"id": job["job_id"], "kind": job["kind"], "project_id": job["project_id"],
            "state": job["state"], "stage": job["stage"], "progress": job["progress"],
            "inputs": dict(job.get("inputs") or {}), "result": job.get("result"), "error": job.get("error"),
            "idempotency_key": None, "committed": job["state"] == "succeeded",
            "created_at": job["created_at"], "updated_at": job["updated_at"],
            "logs": list(job["logs"]), "last_event_id": job["_next"] - 1}


def render_job_status(job):
    """openapi RenderJobStatus：本次（最近一次）渲染作业的状态，与已发布版本分开。"""
    st = job.get("state")
    return {"id": job.get("job_id"), "state": st, "stage": job.get("stage"),
            "running": st in ("queued", "running"), "ok": (st == "succeeded") if st else None,
            "error": job.get("error"), "result": job.get("result"), "diag": job.get("diag")}


EXPORT_VARIANTS = {"filing": ("filing",), "disclosure": ("annotated",), "both": ("annotated", "filing")}
VARIANT_LABEL = {"annotated": "交底版", "filing": "递交版"}


def export_result(jid, rid, ver, formats, variant, preset):
    """导出作业的 result（openapi ExportResult）：manifest 键「图号/版本/格式」；items 逐「版本 × 格式」；
    documents 为 PDF / Word 的逐页核验；缺独立渲染器时 PDF = unverified 并隔离到 _未通过核验/。"""
    manifest, reasons, items, documents, files, sha = {}, {}, [], {}, [], {}
    figs = ver.get("figures") or []
    for v in EXPORT_VARIANTS[variant]:
        label = VARIANT_LABEL[v]
        for fmt in formats:
            if fmt in ("pdf", "docx"):
                st = "unverified" if (fmt == "pdf" and not FLAGS["pdf_check"]) else "ok"
                fname = "说明书附图_%s.%s" % (label, fmt)
                path = ("_未通过核验/" + fname) if st != "ok" else fname
                reason = ("缺少 pypdfium2，无法核验 PDF 可见性" if st == "unverified" else "")
                pages = [{"page": i + 1, "figure": f, "ok": True, "errors": [], "page_mm": [210.0, 297.0],
                          "min_text_mm": 3.5 + i * 1.75, "dpi_effective": 300, "scale": 1.0, "caption": "图%d" % (i + 1)}
                         for i, f in enumerate(figs)]
                documents[fname] = {"file": fname, "format": fmt, "ok": st == "ok", "status": st, "pages": len(figs),
                                    "expected_pages": len(figs), "errors": [reason] if reason else [], "page_checks": pages,
                                    "path": path, "layout_verified": "file-geometry" if fmt == "pdf" else "structural",
                                    "layout_note": "" if fmt == "pdf" else "Word 按文档结构核验，实际排版以 Word 打开为准",
                                    "render_check": ("pypdfium2" if FLAGS["pdf_check"] else "unavailable") if fmt == "pdf" else None,
                                    "sha256": hashlib.sha256(fname.encode()).hexdigest(), "sha256_verified": None}
                files.append(path)
                sha[path] = documents[fname]["sha256"]
            elif fmt == "dwg" and not FLAGS["dwg"]:
                st, path, reason = "skip", None, "未检测到 DWG 转换器（AutoCAD Core Console 或 LibreDWG）"
            elif fmt in ("dxf", "dwg", "png"):
                st, path, reason = "ok", "源文件/%s/" % fmt.upper(), ""
            else:
                st, path, reason = "skip", None, "未知格式"
            for fid in figs:
                key = "%s/%s/%s" % (fid, label, fmt)
                manifest[key] = st
                if reason:
                    reasons[key] = reason
            items.append({"variant": label, "format": fmt, "status": st, "reason": reason, "figures": len(figs),
                          "file": path, "role": "主交付" if fmt in ("pdf", "docx") else "源文件，供再编辑"})
    ok = not any(it["status"] in ("fail", "unverified") for it in items)
    return {"export_id": jid, "render_job_id": rid, "render_plan_etag": ver.get("plan_etag"), "ok": ok,
            "manifest": manifest, "reasons": reasons, "items": items, "documents": documents, "missing_files": [],
            "files": files + ["附图说明.txt", "核对清单.txt", "核验报告.json"], "sha256": sha,
            "spec": {"preset": preset, "dpi": 300}, "note": CNIPA_NOTE, "notes": [], "dwg_log": [],
            "download_url": "/v1/exports/%s/download" % jid}


def norm_etag(e):
    e = (e or "").strip()
    if e.startswith("W/"):
        e = e[2:]
    return e.strip('"')


def run_job(job, kind, fail_stage, speed):
    job["state"] = "running"
    _emit(job, "stage", "running", stage="running", progress=0.0)
    if kind == "render" and job.get("project_id"):
        p = PROJECTS.get(job["project_id"])
        if p and p.get("data") is not None and (p["data"].get("render_job") or {}).get("id") == job["job_id"]:
            p["data"]["render_job"] = render_job_status(job)
    stages = stages_for(kind, job.get("project_id"))
    for i, (stage, label, secs) in enumerate(stages):
        if job["_done"]:
            return
        _emit(job, "log", "▶ %s…" % label)
        _emit(job, "stage", label, stage=stage, progress=round(0.05 + 0.9 * i / max(1, len(stages)), 4))
        time.sleep(max(0.05, secs * speed))
        if fail_stage and stage == fail_stage:
            job["state"] = "failed"
            job["error"] = {"code": "E_JOB_FAILED",
                            "message": "工程准备失败：STEP 顶层只有 1 个实体，未解析出零件——请确认导出的是装配体而非单件",
                            "hint": ""}
            _emit(job, "error", job["error"], stage="failed")
            return _finish(job)
    if kind == "render" and job.get("project_id") and job.get("result") is not None:
        p = PROJECTS.get(job["project_id"])
        r = job["result"]
        if not r.get("ok"):
            # 未全部过闸 / 计划未过校验 → 失败作业，带诊断；不发布，已发布版本保持不变
            failed = [f["id"] for f in r.get("figures", []) if not f.get("pass")]
            if r.get("figures"):
                err = {"code": "E_RENDER_NOT_OK", "message": "出图未全部过闸/有图缺失：%s" % failed,
                       "hint": "按每张图的 QA 提示改计划后重渲"}
            else:
                err = {"code": "E_PLAN_INVALID", "message": "计划未通过校验，未渲染",
                       "hint": "按校验提示改计划后重渲"}
            job["state"] = "failed"
            job["error"] = err
            job["diag"] = {"version": job["job_id"], "sealed": True, "published": False,
                           "failed_figures": failed, "missing_artefacts": [],
                           "log_tail": (r.get("log") or "")[-1500:]}
            if p and p.get("data") is not None:
                p["data"]["render_job"] = render_job_status(job)
            _emit(job, "error", err, stage="failed")
            return _finish(job)
        # 成功：发布为当前版本（重新进入也能看到），并登记为可导出版本
        job["diag"] = {"version": job["job_id"], "sealed": True, "published": True,
                       "failed_figures": [], "missing_artefacts": [], "log_tail": ""}
        if p and p.get("data") is not None:
            p["data"]["render"] = r
            VERSIONS[job["job_id"]] = {"project": job["project_id"],
                                       "plan_etag": (job.get("inputs") or {}).get("plan_etag"),
                                       "figures": [f["id"] for f in r.get("figures", [])]}
    if kind == "suggest":
        p = PROJECTS.get(job["project_id"])
        cands, empty = p["data"]["suggest_def"]
        job["_final"] = suggest_result(job["project_id"], p, copy.deepcopy(cands), empty, job["job_id"])
    if job.get("_final") is not None:          # 导出 / 候选：结果在成功时才出现（运行中 result 为 null）
        job["result"] = job.pop("_final")
    if kind in ("prepare", "from-example") and job.get("result") is None:
        job["result"] = {"project_id": job["project_id"], "status": "ready"}
    job["state"] = "succeeded"
    if kind == "render" and job.get("project_id"):
        p = PROJECTS.get(job["project_id"])
        if p and p.get("data") is not None:
            p["data"]["render_job"] = render_job_status(job)
    _emit(job, "done", job.get("result"), stage="succeeded", progress=1.0)
    _finish(job)


def latest_suggest(pid):
    jid = ((PROJECTS.get(pid) or {}).get("data") or {}).get("suggest_job")
    return JOBS.get(jid) if jid else None


# --------------------------------------------------------------------------- #
#  /p/{pid}/ 子应用（前端「旧版后端兼容模式」= api.js 的 legacy 分支）              #
# --------------------------------------------------------------------------- #
# 接口形状仿旧 plan_studio 的同步 /api/*（无 ETag），状态语义按 WP1 冻结的 openapi RenderStatus：
# result = 已发布版本，version = 其 ID，job = 本次（最近一次）渲染作业；失败作业不发布。
# 前端在兼容模式下只用它浏览、渲染、改术语、预览——导出一律锁定（架构裁决，修复轮 7）。
LEGACY = {}                   # pid -> {"running", "result", "version", "started", "job"}
LEGACY_RENDER_SECS = 1.6


def legacy_shape(r):
    """把（live 形态的）渲染结果改成子应用形态：无 inputs/ETag，预览走 /api/preview/。"""
    r.pop("inputs", None)
    for f in r.get("figures", []):
        f["svg"] = "/api/preview/%s" % f["svg"].rsplit("/", 1)[-1]
        f["variants"] = {k: "/api/preview/%s" % v.rsplit("/", 1)[-1]
                         for k, v in (f.get("variants") or {}).items()}
    return r


def legacy_ws(pid):
    p = PROJECTS.get(pid)
    if not p or not p.get("data"):
        return None
    ws = LEGACY.get(pid)
    if ws is None:
        r = copy.deepcopy(p["data"].get("render"))
        ws = LEGACY[pid] = {"running": False, "started": None, "job": None,
                            "result": legacy_shape(r) if r else None,
                            "version": (r or {}).get("version")}
    return ws


def legacy_render_job(pid, ws, jid, fail, speed=1.0):
    plan = json.dumps(PROJECTS[pid]["data"]["plan"], ensure_ascii=False, sort_keys=True)
    time.sleep(LEGACY_RENDER_SECS * speed)
    r = legacy_shape(render_payload(pid, fail))
    r["render_job_id"] = r["version"] = jid
    r["finished"] = time.strftime("%H:%M:%S")
    r["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    r.setdefault("log", "")
    r["log"] = (r["log"] + "\nmock 子应用渲染 · 读到的 plan sha256=%s"
                % hashlib.sha256(plan.encode("utf-8")).hexdigest()[:12]).strip()
    failed = [f["id"] for f in r.get("figures", []) if not f.get("pass")]
    job = {"id": jid, "stage": "render", "running": False, "result": r,
           "diag": {"version": jid, "sealed": True, "published": bool(r.get("ok")),
                    "failed_figures": failed, "missing_artefacts": [], "log_tail": r["log"][-1500:]}}
    if r.get("ok"):
        ws["result"], ws["version"] = r, jid                  # 只有成功才发布
        job.update(state="succeeded", ok=True, error=None)
    else:
        err = ({"code": "E_RENDER_NOT_OK", "message": "出图未全部过闸/有图缺失：%s" % failed,
                "hint": "按每张图的 QA 提示改计划后重渲"} if r.get("figures") else
               {"code": "E_PLAN_INVALID", "message": "计划未通过校验，未渲染", "hint": "按校验提示改计划后重渲"})
        job.update(state="failed", ok=False, error=err)
    ws["job"] = job
    ws["running"] = False


def legacy_state(pid, ws):
    p = PROJECTS[pid]
    d = p["data"]
    return {"step": "/mock/%s" % p["step"], "workdir": "/mock/%s" % pid,
            "assembly": d.get("assembly"), "plan": d.get("plan"),
            "validate": d.get("validate") or {"ok": True, "issues": []},
            "render": ws["result"], "version": ws["version"], "render_job": ws["job"],
            "rendering": ws["running"], "home_url": "/home.html",
            "flowcharts": [], "llm": {"provider": LLM["provider"], "provider_label": LLM["provider_label"]},
            "structure": d.get("structure"), "structure_job": {}}


# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
#  HTTP                                                                        #
# --------------------------------------------------------------------------- #
def project_summary(p):
    """GET /v1/projects 的条目（与 workbench.Project.summary 同字段：只有 id，没有 project_id）。"""
    base = {"id": p["id"], "title": p["title"], "description": p["desc"],
            "step_name": p["step"], "example": p["example"], "status": p["status"],
            "message": p.get("message", ""), "created": "2026-10-11 10:00:00", "flowcharts": 0,
            "structure": {"source": None, "groups": []}}
    d = p.get("data")
    r = (d or {}).get("render")
    figs = (r or {}).get("figures", [])
    thumb = None
    if figs:
        v = figs[0].get("variants") or {}
        thumb = v.get("annotated") or figs[0].get("svg")
    base.update(figures=len(figs), figures_pass=sum(1 for f in figs if f.get("pass")),
                rendered_at=(r or {}).get("finished_at"), version=(r or {}).get("render_job_id"), thumb=thumb)
    return base


def project_brief(p):
    """GET /v1/projects/{id}：摘要 + plan_etag + 候选分图状态（与 workbench.v1_project 同形）。"""
    out = project_summary(p)
    out["plan_etag"] = norm_etag(p["plan_etag"])
    sj = latest_suggest(p["id"])
    res = (sj or {}).get("result") or {}
    out["candidates"] = None if sj is None else {
        "job_id": sj["job_id"], "state": sj["state"], "summary": res.get("summary"),
        "empty_reason": res.get("empty_reason"), "url": "/v1/projects/%s/plan/candidates" % p["id"]}
    return out


def sub_state(pid):
    """新版子应用 GET /p/{pid}/api/state（与 plan_studio.state 同字段）。"""
    p = PROJECTS[pid]
    d = p["data"]
    job = d.get("render_job") or {"id": None, "state": None, "stage": None, "running": False, "ok": None,
                                  "error": None, "result": None, "diag": None}
    return {"step": "/mock/%s" % p["step"], "workdir": "/mock/%s" % pid,
            "assembly": d.get("assembly"), "plan": d.get("plan"),
            "validate": d.get("validate") or {"ok": True, "issues": []},
            "render": d.get("render"), "version": (d.get("render") or {}).get("render_job_id"),
            "render_job": job, "rendering": bool(job.get("running")), "home_url": "/",
            "flowcharts": [], "llm": {"provider": LLM["provider"], "provider_label": LLM["provider_label"],
                                      "configured": LLM["configured"], "dwg_available": FLAGS["dwg"]},
            "structure": d.get("structure"), "structure_job": d.get("structure_job") or
            {"running": False, "started": None, "error": None, "finished": None}}


def render_payload(pid, fail, plan_etag=None):
    """渲染作业的权威结果：gearbox 用真实出图；其它工程借用 gearbox 演示闭环。
    结果携带 inputs.plan_etag 作为版本证据。fail 时体现 QA/校验未过。"""
    if fail == "validate":                   # 计划未过校验：无图 + validate.issues + log
        return {"ok": False, "figures": [], "numerals": None, "figure_descriptions": [],
                "validate": {"ok": False, "issues": [
                    {"code": "E_TERM_FORBIDDEN", "severity": "error", "pointer": "/terms/3",
                     "message": "术语 3 看起来像内部件号（PGB-0012-A），不是中文技术名词",
                     "hint": "把 terms[].term 改成中文技术名词，如「行星销」"},
                    {"code": "E_FIGURE_EMPTY", "severity": "error", "pointer": "/figures/1",
                     "message": "图 fig2 没有任何成员零件", "hint": "在 3D 里框选零件后加入该图，或删掉这张空图"}]},
                "log": "校验未通过，未渲染。\nE_TERM_FORBIDDEN /terms/3\nE_FIGURE_EMPTY /figures/1"}
    src = (PROJECTS.get("gearbox", {}).get("data") or {}).get("render")
    r = copy.deepcopy(src)
    if not r:
        return {"ok": True, "figures": [], "numerals": None, "figure_descriptions": []}
    if fail == "gate":
        f = r["figures"][0]
        rows = f.get("annotation", {}).get("table", {}).get("rows", [])
        f["qa"] = failing_qa(rows)
        f["pass"] = False
        r["ok"] = False
    else:
        r["ok"] = True
    r["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    r["inputs"] = {"plan_etag": plan_etag}   # 绑定渲染时的输入版本
    return r


def fill_blank_terms(plan, rows):
    """只填空白：有精确行且空白 → 填；没有精确行 → 插一行（不动通配行）；人填过的不动。"""
    out = {"filled": [], "kept_human": [], "added": []}
    terms = plan.setdefault("terms", [])
    for r in rows:
        row = next((t for t in terms if t.get("selector") == r["selector"]), None)
        if row is None:
            terms.insert(0, {"selector": r["selector"], "term": r["term"], "label": r.get("label", "once")})
            out["added"].append(r["selector"])
        elif (row.get("term") or "").strip():
            out["kept_human"].append(r["selector"])
        else:
            row["term"] = r["term"]
            out["filled"].append(r["selector"])
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("  mock %s" % (fmt % args))

    # ---- helpers ----
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, message, hint="", ecode="error", detail=None):
        self._send(code, {"error": {"code": ecode, "message": message, "hint": hint},
                          "detail": detail if detail is not None else message})

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def _raw_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    # ---- routing ----
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/":
            return self._redirect("/home.html")
        if path.startswith("/p/"):
            return self.legacy("GET", path, q) if OLD_BACKEND else self.subapp("GET", path, q)
        if path == "/v1/capabilities":
            if OLD_BACKEND:
                return self._err(404, "Not Found")
            return self._send(200, capabilities())
        if path.startswith("/v1/") or path.startswith("/api/"):
            return self.api_get(path, q)
        return self.static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith("/p/"):
            return self.legacy("POST", u.path, parse_qs(u.query)) if OLD_BACKEND \
                else self.subapp("POST", u.path, parse_qs(u.query))
        if u.path == "/v1/projects":
            return self.api_create(parse_qs(u.query))
        return self.api_post(u.path, parse_qs(u.query), self._json_body())

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path.startswith("/p/"):
            return self.legacy("PUT", u.path, parse_qs(u.query)) if OLD_BACKEND \
                else self.subapp("PUT", u.path, parse_qs(u.query))
        return self.api_put(u.path, parse_qs(u.query), self._json_body())

    def do_DELETE(self):
        u = urlparse(self.path)
        return self.api_delete(u.path, parse_qs(u.query))

    def _redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- 新版子应用 /p/{pid}/（与 plan_studio 同路由：静态页 + api/state / render/status / preview / model / structure）----
    def subapp(self, method, path, q):
        m = re.fullmatch(r"/p/([\w\-]+)(/.*)?", path)
        if not m:
            return self._err(404, "没有这个工程")
        pid, rest = m.group(1), (m.group(2) or "")
        if not rest:
            return self._redirect("/p/%s/" % pid)
        rest = rest[1:]
        p = PROJECTS.get(pid)
        if not p or not p.get("data"):
            return self._send(404 if not p else 409, "没有这个工程" if not p else "工程还在准备", "text/plain; charset=utf-8")
        if not rest.startswith("api/"):
            if method != "GET":
                return self._err(405, "方法不允许")
            return self.static("/" + (rest or "index.html"))
        api, d = rest[4:], p["data"]
        if method == "GET" and api == "state":
            return self._send(200, sub_state(pid))
        if method == "GET" and api == "render/status":
            st = sub_state(pid)
            return self._send(200, {"running": st["rendering"], "started": None, "result": st["render"],
                                    "version": st["version"], "job": st["render_job"]})
        if method == "GET" and api == "model.glb":
            glb = GLB_BY_PROJECT.get(pid)
            return self.static(f"/dev/fixtures/models/{glb}") if glb else self._err(404, "没有模型")
        if method == "GET" and api.startswith("preview/"):
            name = api[len("preview/"):]
            if not re.fullmatch(r"[\w\-]+\.(svg|png)", name):
                return self._err(400, "非法文件名")
            sub = pid if (FIX / pid / name).is_file() else "gearbox"
            return self.static(f"/dev/fixtures/{sub}/{name}")
        if method == "GET" and api == "structure":
            return self._send(200, {"structure": d.get("structure"), "job": d.get("structure_job") or {"running": False}})
        if method == "PUT" and api == "structure":
            body = self._json_body()
            for g in (body.get("groups") or []):
                for grp in (d.get("structure") or {}).get("groups", []):
                    if grp["id"] == g.get("id") and g.get("name"):
                        grp["name"] = g["name"]
            return self._send(200, {"structure": d.get("structure")})
        if method == "POST" and api == "structure/analyze":
            if not LLM["configured"]:
                return self._err(409, "没有可用的大模型通道：请在工作台设置里填 DeepSeek 密钥")
            d["structure_job"] = {"running": True, "started": time.strftime("%H:%M:%S"), "error": None, "finished": None}

            def done():
                time.sleep(1.5)
                d["structure_job"] = {"running": False, "started": None, "error": None, "finished": time.strftime("%H:%M:%S")}
            threading.Thread(target=done, daemon=True).start()
            return self._send(200, {"job": d["structure_job"]})
        # mock 没有流程图后端（真实工作台有）：前端探测 404 → 显示「需要后端」
        return self._err(404, "mock 子应用未实现：%s %s" % (method, api))

    # ---- 旧版子应用（--old-backend）/p/{pid}/ ----
    def legacy(self, method, path, q):
        m = re.fullmatch(r"/p/([\w\-]+)(/.*)?", path)
        if not m:
            return self._err(404, "没有这个工程")
        pid, rest = m.group(1), (m.group(2) or "")
        if not rest:
            return self._redirect("/p/%s/" % pid)
        rest = rest[1:]
        ws = legacy_ws(pid)
        if ws is None:
            return self._err(404, "没有这个工程")
        if not rest.startswith("api/"):
            if method != "GET":
                return self._err(405, "方法不允许")
            return self.static("/" + (rest or "index.html"))
        api, p = rest[4:], PROJECTS[pid]
        if method == "GET" and api == "state":
            return self._send(200, legacy_state(pid, ws))
        if method == "GET" and api == "render/status":
            return self._send(200, {"running": ws["running"], "result": ws["result"], "version": ws["version"],
                                    "started": ws["started"], "job": ws["job"]})
        if method == "GET" and api == "model.glb":
            glb = GLB_BY_PROJECT.get(pid)
            return self.static(f"/dev/fixtures/models/{glb}") if glb else self._err(404, "没有模型")
        if method == "GET" and api.startswith("preview/"):
            name = api[len("preview/"):]
            if not re.fullmatch(r"[\w\-]+\.(svg|png)", name):
                return self._err(400, "非法文件名")
            sub = pid if (FIX / pid / name).is_file() else "gearbox"
            return self.static(f"/dev/fixtures/{sub}/{name}")
        if method == "GET" and api == "flowcharts":
            return self._send(200, {"flowcharts": []})
        if method == "PUT" and api == "plan":
            body = self._json_body()
            plan = body.get("plan") if isinstance(body.get("plan"), dict) else None
            if plan is None or "terms" not in plan:
                return self._err(400, "body.plan 必须是对象")
            p["data"]["plan"] = plan
            p["plan_rev"] += 1
            p["plan_etag"] = 'W/"%s-%d"' % (pid, p["plan_rev"])
            return self._send(200, {"validate": {"ok": True, "issues": []}})
        if method == "POST" and api == "render":
            self._json_body()
            fail = (q.get("fail") or [""])[0]                   # ""|"gate"|"validate"（开发用失败场景）
            speed = float((q.get("speed") or ["1"])[0])         # 开发用：放慢作业（测试刷新恢复）
            with LOCK:
                if ws["running"]:
                    return self._err(409, "已有一次渲染在进行——几何缓存不支持并发写入")
                ws["running"] = True
                ws["started"] = time.strftime("%H:%M:%S")
                jid = "lr_" + uuid.uuid4().hex[:10]
                ws["job"] = {"id": jid, "state": "running", "stage": "render", "running": True,
                             "ok": None, "error": None, "result": None, "diag": None}
            threading.Thread(target=legacy_render_job, args=(pid, ws, jid, fail, speed), daemon=True).start()
            return self._send(200, {"started": ws["started"]})
        return self._err(404, "mock 子应用未实现：%s %s" % (method, api))

    # ---- static files (relative paths; serves webui/ and dev/fixtures) ----
    def static(self, path):
        rel = path.lstrip("/")
        # 与正式后端兼容：首页资源走 /static/*（workbench.py 把 /static 挂到 webui/）
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        target = (WEBUI / rel).resolve()
        if WEBUI not in target.parents and target != WEBUI:
            return self._err(403, "越界访问")
        if not target.is_file():
            return self._err(404, "没有这个文件：%s" % path)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ---- GET /v1 ----
    def api_get(self, path, q):
        if path in ("/v1/projects", "/api/wb/state"):
            scene = (q.get("scene") or [""])[0]
            if scene == "empty":
                items = []
            elif scene == "error":
                return self._err(503, "连接不上工作台服务（演示：错误态）",
                                 "这是 mock 的 ?scene=error；真实后端断开时首页应显示此态并给「重试」。")
            else:
                order = ["gearbox", "bench-vise", "ball-valve", "demo-preparing", "demo-error"]
                if scene == "loading":
                    return self._send(200, {"loading": True, "projects": []})
                items = [project_summary(PROJECTS[i]) for i in order if i in PROJECTS]
            if path == "/api/wb/state":              # 旧版工作台首页
                return self._send(200, {"projects": items, "data_dir": "~/墨线数据（演示）",
                                        "llm": {"provider": LLM["provider"], "provider_label": LLM["provider_label"]}})
            return self._send(200, {"projects": items, "workbench": "/"})
        m = re.fullmatch(r"/v1/jobs/([\w\-]+)", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self._err(404, "未找到作业 %s" % m.group(1))
            return self._send(200, job_dict(job))
        m = re.fullmatch(r"/v1/jobs/([\w\-]+)/events", path)
        if m:
            last = self.headers.get("Last-Event-ID") or (q.get("last_event_id") or ["0"])[0]
            return self.sse(JOBS.get(m.group(1)), last)
        m = re.fullmatch(r"/v1/projects/([\w\-]+)", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p:
                return self._err(404, "没有这个工程：%s" % m.group(1))
            return self._send(200, project_brief(p))
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/plan", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p or not p.get("data"):
                return self._err(404, "没有这个工程")
            return self._send(200, p["data"]["plan"], extra={"ETag": p["plan_etag"]})   # 响应体即 plan
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/plan/candidates", path)
        if m:
            sj = latest_suggest(m.group(1))
            if not sj:
                return self._err(404, "这个工程还没有生成过候选分图")
            return self._send(200, job_dict(sj))
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/model\.glb", path)
        if m:
            glb = GLB_BY_PROJECT.get(m.group(1))
            return self.static(f"/dev/fixtures/models/{glb}") if glb else self._err(404, "没有模型")
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/preview/([\w\-.]+)", path)
        if m:
            pid, name = m.group(1), m.group(2)
            if not re.fullmatch(r"[\w\-]+\.(svg|png)", name):
                return self._err(400, "非法文件名")
            sub = "gearbox" if pid == "gearbox" else pid
            if not (FIX / sub / name).is_file():
                sub = "gearbox"
            return self.static(f"/dev/fixtures/{sub}/{name}")
        m = re.fullmatch(r"/v1/exports/([\w\-]+)", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job or job["kind"] != "export":
                return self._err(404, "未找到")
            return self._send(200, job_dict(job))
        m = re.fullmatch(r"/v1/exports/([\w\-]+)/download", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job or job["kind"] != "export" or job["state"] != "succeeded":
                return self._err(404, "没有这个已完成的导出作业")
            name = "%s-%s.zip" % (job["project_id"], job["job_id"])
            body = ("这是 mock 的占位 zip（%s，出图版本 %s）。真实后端在此返回打包好的附图包。"
                    % (name, (job.get("result") or {}).get("render_job_id"))).encode("utf-8")
            return self._send(200, body, "application/zip",
                              {"Content-Disposition": 'attachment; filename="%s"' % name})
        return self._err(404, "未实现的 GET：%s" % path)

    # ---- SSE（WP1 帧）----
    def sse(self, job, last_id):
        if not job:
            return self._err(404, "没有这个作业")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            last = int(last_id or 0)
        except ValueError:
            last = 0
        try:
            while True:
                with job["_cv"]:
                    while not [e for e in job["_events"] if e["id"] > last] and not job["_done"]:
                        job["_cv"].wait(timeout=10)
                    pending = [e for e in job["_events"] if e["id"] > last]
                    done = job["_done"]
                for ev in pending:
                    data = {"stage": ev["stage"], "progress": ev["progress"], "data": ev["data"]}
                    chunk = "id: %d\nevent: %s\ndata: %s\n\n" % (ev["id"], ev["event"], json.dumps(data, ensure_ascii=False))
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                    last = ev["id"]
                if done and not [e for e in job["_events"] if e["id"] > last]:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    # ---- POST /v1/projects（上传）----
    def api_create(self, q):
        self._raw_body()
        scene = (q.get("fail") or [""])[0]
        job = new_job("prepare", project_id="gearbox", fail_stage="prepare" if scene == "parse" else None,
                      speed=float((q.get("speed") or ["1"])[0]))
        return self._send(202, {"project_id": "gearbox", "job_id": job["job_id"]})

    # ---- POST ----
    def api_post(self, path, q, body):
        if path == "/v1/projects/from-example":
            ex = (body.get("example") or "gearbox")
            pid = {"gearbox": "gearbox", "vise": "bench-vise", "valve": "ball-valve"}.get(ex)
            if not pid:
                return self._err(404, "没有内置示例 %r" % ex)
            job = new_job("from-example", project_id=pid, speed=0.6)
            return self._send(202, {"project_id": pid, "job_id": job["job_id"]})
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/plan/suggest", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p or not p.get("data"):
                return self._err(409, "工程还在准备中或准备失败，稍后再试")
            job = new_job("suggest", project_id=m.group(1), inputs={"plan_etag": norm_etag(p["plan_etag"])}, start=False)
            p["data"]["suggest_job"] = job["job_id"]
            threading.Thread(target=run_job, args=(job, "suggest", None, 1.0), daemon=True).start()
            return self._send(202, {"project_id": m.group(1), "job_id": job["job_id"]})
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/plan/candidates/accept", path)
        if m:
            return self.accept(m.group(1), body)
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/render", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p:
                return self._err(404, "没有这个工程")
            fail = (q.get("fail") or [""])[0]   # ""|"gate"|"validate"（mock 专用失败场景）
            plan_raw = json.dumps((p.get("data") or {}).get("plan"), ensure_ascii=False, sort_keys=True)
            inputs = {"plan_etag": p.get("plan_etag"),
                      "plan_hash": hashlib.sha256(plan_raw.encode("utf-8")).hexdigest()}
            speed = float((q.get("speed") or ["1"])[0])   # 开发用：放慢作业（测试刷新恢复）
            job = new_job("render", project_id=m.group(1), speed=speed, inputs=inputs, start=False,
                          result=render_payload(m.group(1), fail, p.get("plan_etag")))
            job["result"]["render_job_id"] = job["result"]["version"] = job["job_id"]   # 结果 ID = 版本
            if p.get("data") is not None:
                p["data"]["render_job"] = render_job_status(job)
            threading.Thread(target=run_job, args=(job, "render", None, speed), daemon=True).start()
            return self._send(202, {"job_id": job["job_id"], "inputs": inputs})
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/export", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p or not p.get("data"):
                return self._err(404, "没有这个工程")
            preset = body.get("sheet_preset") or "standard"
            if preset not in ("standard", "conservative"):
                return self._send(400, {"detail": "sheet_preset 只能是 standard / conservative"})
            rid = body.get("render_job_id") or ((p["data"].get("render") or {}).get("render_job_id"))
            ver = VERSIONS.get(rid or "")
            if not ver or ver["project"] != m.group(1):
                return self._send(409, {"detail": "没有可导出的成功渲染，请先出图"})
            want = body.get("plan_etag")
            if want and norm_etag(want) != norm_etag(ver["plan_etag"]):
                return self._send(409, {"detail": "请求的 plan_etag 与被导出渲染的输入版本不一致",
                                        "render_plan_etag": ver["plan_etag"]})
            formats = body.get("formats") or ["pdf", "docx", "dxf"]
            variant = body.get("variant") or "both"
            if variant not in EXPORT_VARIANTS:
                return self._send(400, {"detail": "variant 只能是 filing / disclosure / both"})
            job = new_job("export", project_id=m.group(1), start=False,
                          inputs={"formats": formats, "variant": variant, "dpi": int(body.get("dpi") or 300),
                                  "sheet_preset": preset, "render_job_id": rid})
            job["_final"] = export_result(job["job_id"], rid, ver, formats, variant, preset)
            threading.Thread(target=run_job, args=(job, "export", None, 1.0), daemon=True).start()
            return self._send(202, {"export_id": job["job_id"], "job_id": job["job_id"], "render_job_id": rid})
        # AI 起名（F9，WP2 语义）：{selectors?, apply=false, plan_etag?}；无通道 200 ai_not_configured
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/ai/draft-terms", path)
        if m:
            return self.draft_terms(m.group(1), body)
        return self._err(404, "未实现的 POST：%s" % path)

    def accept(self, pid, body):
        p = PROJECTS.get(pid)
        if not p or not p.get("data"):
            return self._err(404, "没有这个工程")
        sj = JOBS.get(body.get("job_id") or "") or latest_suggest(pid)
        if not sj or sj["kind"] != "suggest" or sj["project_id"] != pid:
            return self._send(409, {"detail": "没有这个工程的候选分图作业：%s" % (body.get("job_id") or "（无）")})
        if sj["state"] != "succeeded":
            return self._send(409, {"detail": "候选分图作业未成功（state=%s）" % sj["state"]})
        ids = body.get("candidate_ids")
        if not isinstance(ids, list) or not ids:
            return self._send(400, {"detail": "candidate_ids 必须是非空字符串数组"})
        by_id = {c["id"]: c for c in sj["result"]["candidates"]}
        unknown = [c for c in ids if c not in by_id]
        if unknown:
            return self._send(422, {"detail": "候选不存在：%s" % "、".join(unknown)})
        if_match = (self.headers.get("If-Match") or body.get("plan_etag") or "").strip()
        if if_match and norm_etag(if_match) != norm_etag(p["plan_etag"]):
            return self._send(409, {"detail": "plan 已被他人修改（ETag 不匹配，当前 %s）" % norm_etag(p["plan_etag"])})
        names = {x["name"] for x in p["data"]["assembly"]["parts"]}
        unresolved, added = [], []
        plan = copy.deepcopy(p["data"]["plan"])
        for cid in ids:
            c = by_id[cid]
            members = (body.get("members") or {}).get(cid) or c["members"]
            parts = []
            for mem in members:
                part = mem.get("part") if isinstance(mem, dict) else None
                if part in names:
                    parts.append(part)
                else:
                    unresolved.append({"candidate": cid, "member": mem, "reason": "当前装配体里没有这个零件"})
            ids_now = {f["id"] for f in plan["figures"]}
            n = 1
            while "fig%d" % n in ids_now:
                n += 1
            plan["figures"].append({"id": "fig%d" % n, "caption": c["caption"], "kind": c["kind"], "members": parts})
            added.append("fig%d" % n)
        if unresolved:
            return self._send(422, {"detail": {"message": "候选成员在当前装配体里解析不到，未写入计划",
                                               "unresolved": unresolved}})
        p["data"]["plan"] = plan
        p["plan_rev"] += 1
        p["plan_etag"] = 'W/"%s-%d"' % (pid, p["plan_rev"])
        return self._send(200, {"ok": True, "project_id": pid, "job_id": sj["job_id"], "plan_etag": norm_etag(p["plan_etag"]),
                                "mode": body.get("mode", "append"), "added_figures": added, "standard_labels_set": [],
                                "standard_labels_skipped": [],
                                "accepted_failed": [cid for cid in ids if by_id[cid]["status"] != "pass"],
                                "plan_valid": True, "issues": []}, extra={"ETag": p["plan_etag"]})

    def draft_terms(self, pid, body):
        p = PROJECTS.get(pid)
        if not p or not p.get("data"):
            return self._err(404, "没有这个工程")
        base = {"project_id": pid, "ai_configured": LLM["configured"], "provider": LLM["provider"],
                "provider_label": LLM["provider_label"], "outbound_fields": ["part_names", "part_codes"],
                "outbound_note": OUTBOUND_NOTE, "skipped": [], "applied": None,
                "plan_etag": norm_etag(p["plan_etag"])}
        if not LLM["configured"]:
            return self._send(200, dict(base, status="ai_not_configured", suggestions=[],
                                        hint="没有配置大模型通道：在「设置」里填 DeepSeek API 密钥后再试；也可以直接在术语页手填零件名。"),
                              extra={"ETag": p["plan_etag"]})
        names = {**GEARBOX_NAMES, **VISE_NAMES, **VALVE_NAMES}
        plan = p["data"]["plan"]
        have = {t.get("selector"): (t.get("term") or "").strip() for t in plan.get("terms", [])}
        parts = [x["name"] for x in p["data"]["assembly"]["parts"]]
        sel = body.get("selectors")
        pool = [s for s in (sel if isinstance(sel, list) else parts) if s in parts]
        low = {"PGB-PLANET-PIN", "VISE-THRUST-COLLAR", "BV-PACKING"}
        rows = [{"selector": s, "term": names.get(s) or "连接件", "label": "once",
                 "confidence": "low" if s in low else "high", "reason": "命名多义，建议人工复核" if s in low else ""}
                for s in pool if not have.get(s)]
        out = dict(base, status="ok", suggestions=rows,
                   skipped=[{"selector": s, "reason": "已有名称，不覆盖人填的"} for s in pool if have.get(s)])
        if body.get("apply"):
            if_match = (self.headers.get("If-Match") or body.get("plan_etag") or "").strip()
            if if_match and norm_etag(if_match) != norm_etag(p["plan_etag"]):
                return self._send(409, {"detail": "plan 已被他人修改（ETag 不匹配，当前 %s）" % norm_etag(p["plan_etag"])})
            plan = copy.deepcopy(plan)
            out["applied"] = fill_blank_terms(plan, rows)
            p["data"]["plan"] = plan
            if out["applied"]["filled"] or out["applied"]["added"]:
                p["plan_rev"] += 1
                p["plan_etag"] = 'W/"%s-%d"' % (pid, p["plan_rev"])
            out["plan_etag"] = norm_etag(p["plan_etag"])
            out["plan_valid"], out["issues"] = True, []
        return self._send(200, out, extra={"ETag": p["plan_etag"]})

    # ---- PUT ----
    def api_put(self, path, q, body):
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/plan", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p or not p.get("data"):
                return self._err(404, "没有这个工程")
            if_match = self.headers.get("If-Match")
            if if_match and norm_etag(if_match) != norm_etag(p["plan_etag"]):
                return self._send(409, {"detail": "plan 已被他人修改（ETag 不匹配，当前 %s）" % norm_etag(p["plan_etag"])})
            # 请求体就是 plan 本身（不是 {plan: …}）；包了一层多半是前端写错，直接拒绝
            plan = body
            if not isinstance(plan, dict) or set(plan) == {"plan"} or "terms" not in plan:
                return self._err(400, "请求体应为 figure-plan 本身（WP1 契约），不是 {plan: …}")
            p["data"]["plan"] = plan
            p["plan_rev"] += 1
            p["plan_etag"] = 'W/"%s-%d"' % (p["id"], p["plan_rev"])
            # 轻量校验：术语里出现件号形状的串报错（与真实后端 FORBIDDEN_TEXT 同源的演示）；
            # 真实后端 PUT 只回 {ok}，校验结果另从子应用 api/state 读——mock 同样只存不回
            issues = []
            for i, t in enumerate(plan.get("terms", [])):
                term = (t.get("term") or "").strip()
                if term and re.search(r"[A-Za-z]{2,}[-_]?\d{3,}", term):
                    issues.append({"code": "E_TERM_FORBIDDEN", "severity": "error",
                                   "pointer": "/terms/%d" % i,
                                   "message": "术语「%s」像内部件号，不是中文技术名词" % term,
                                   "hint": "改成中文技术名词"})
            p["data"]["validate"] = {"ok": not issues, "issues": issues}
            return self._send(200, {"ok": True}, extra={"ETag": p["plan_etag"]})
        return self._err(404, "未实现的 PUT：%s" % path)

    def api_delete(self, path, q):
        m = re.fullmatch(r"/v1/jobs/([\w\-]+)", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self._err(404, "未找到作业")
            if job["state"] in ("queued", "running"):
                job["state"] = "cancelled"
                _emit(job, "error", "作业已取消", stage="cancelled")
                _finish(job)
            return self._send(200, {"id": job["job_id"], "state": job["state"], "committed": job["state"] == "succeeded"})
        m = re.fullmatch(r"/api/wb/projects/([\w\-]+)", path)
        if m:
            return self._send(200, {"ok": True, "demo": True})     # mock 不删工程
        return self._err(404, "未实现的 DELETE：%s" % path)


def main():
    global OLD_BACKEND
    ap = argparse.ArgumentParser(description="墨线前端 mock 服务器（纯标准库）")
    ap.add_argument("--port", type=int, default=8803)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-ai", action="store_true", help="演示 AI 未配置场景")
    ap.add_argument("--no-dwg", action="store_true", help="演示 DWG 不可用场景")
    ap.add_argument("--no-pdf-check", action="store_true", help="演示缺 pypdfium2：PDF 导出为「未核验」")
    ap.add_argument("--old-backend", action="store_true",
                    help="模拟旧版工作台：没有 /v1/capabilities（前端判 legacy 兼容模式），/p/{id}/ 是旧同步 /api/*")
    args = ap.parse_args()
    OLD_BACKEND = args.old_backend
    if args.no_ai:
        LLM.update(provider="none", provider_label="未配置——请在设置里填 DeepSeek API 密钥", configured=False)
    if args.no_dwg:
        FLAGS["dwg"] = False
    if args.no_pdf_check:
        FLAGS["pdf_check"] = False
    seed()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"墨线 mock 服务器{'（旧版工作台）' if OLD_BACKEND else ''} · http://{args.host}:{args.port}/home.html")
    print(f"  规划台： http://{args.host}:{args.port}/p/gearbox/   （开发入口：/index.html?project=gearbox）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
