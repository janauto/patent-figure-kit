#!/usr/bin/env python3
"""墨线 Web 工作台 · WP3 前端开发用 mock 服务器（纯标准库，Python 3.9+）。

后端（WP1/WP2）尚未接线，前端对本 mock 开发。本文件严格按《顶层架构 v1 §6 API 契约》
模拟全部 /v1/* 端点，并保留旧 /api/* 的少量只读兼容。覆盖：

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
    python3 webui/dev/mock_server.py --port 8803
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
#  内存态                                                                      #
# --------------------------------------------------------------------------- #
GLB_BY_PROJECT = {"gearbox": "planetary-gearbox.glb", "bench-vise": "bench-vise.glb",
                  "ball-valve": "ball-valve.glb"}

PROJECTS = {}        # id -> runtime dict
JOBS = {}            # job_id -> job dict
VERSIONS = {}        # 已发布（成功）渲染版本 rid -> {"project", "plan_etag"}；导出只认这里的版本
LOCK = threading.Lock()

# AI 通道：默认已配置；demo-no-ai 场景下置 none（失败场景）
LLM = {"provider": "deepseek", "provider_label": "DeepSeek V3.2（API）", "configured": True}


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
        PROJECTS[pid] = d
    # 准备中 / 失败 两个占位工程，演示列表的加载态与错误态
    PROJECTS["demo-preparing"] = dict(id="demo-preparing", title="（我的）回转支承机构",
        desc="", step="swing-bearing.stp", example=False, status="preparing",
        message="正在解析装配层级…", data=None, plan_etag='W/"prep-0"', plan_rev=0)
    PROJECTS["demo-error"] = dict(id="demo-error", title="（我的）液压夹具",
        desc="", step="hydraulic-fixture.stp", example=False, status="error",
        message="STEP 顶层只有 1 个实体，未解析出零件——请确认导出的是装配体而非单件",
        data=None, plan_etag='W/"err-0"', plan_rev=0)


# --------------------------------------------------------------------------- #
#  作业与 SSE                                                                  #
# --------------------------------------------------------------------------- #
STAGE_SETS = {
    "import": [("接收", "接收 STEP 文件", 0.4), ("解析", "解析装配层级", 1.6),
               ("网格", "生成 3D 网格（GLB）", 1.2), ("候选分图", "按结构给候选分图", 0.9),
               ("试渲染", "逐候选试渲染并过 QA", 1.4)],
    "suggest": [("候选分图", "按结构给候选分图", 0.8), ("试渲染", "逐候选试渲染并过 QA", 1.4)],
    "render": [("消隐", "解析消隐出图", 1.8), ("引线", "发附图标记与引线", 0.8),
               ("标注", "补图号与件号表", 0.6), ("QA", "过 13 项质量闸门", 0.7)],
    "export": [("渲染", "隔离目录渲染", 1.4), ("标注", "生成递交版 / 交底版", 0.8),
               ("转档", "PDF / Word / DXF / DWG", 1.0), ("打包", "打包 zip 与清单", 0.5)],
}


def new_job(kind, project_id=None, fail_stage=None, speed=1.0, result=None):
    jid = "job_" + uuid.uuid4().hex[:10]
    now = time.time()
    job = {"job_id": jid, "kind": kind, "project_id": project_id,
           "state": "queued", "stage": None, "progress": 0.0, "logs": [],
           "inputs": {}, "result": result, "error": None, "diag": None,
           "created_at": now, "updated_at": now, "_final": None,
           "_events": [], "_cv": threading.Condition(), "_done": False}
    JOBS[jid] = job
    t = threading.Thread(target=run_job, args=(job, kind, fail_stage, speed), daemon=True)
    t.start()
    return job


def _emit(job, etype, data):
    with job["_cv"]:
        eid = len(job["_events"])
        job["_events"].append({"id": eid, "event": etype, "data": data})
        job["updated_at"] = time.time()
        if etype == "log":
            job["logs"].append(data.get("line", ""))
        job["_cv"].notify_all()


def job_dict(job):
    """openapi Job（与 WP1 Job.to_dict 同字段）：作业 ID 字段名是 id。"""
    return {"id": job["job_id"], "kind": job["kind"], "project_id": job["project_id"],
            "state": job["state"], "stage": job["stage"], "progress": job["progress"],
            "inputs": dict(job.get("inputs") or {}), "result": job.get("result"), "error": job.get("error"),
            "idempotency_key": None, "committed": job["state"] == "succeeded",
            "created_at": job["created_at"], "updated_at": job["updated_at"],
            "logs": list(job["logs"]), "last_event_id": len(job["_events"]) - 1}


def render_job_status(job):
    """openapi RenderJobStatus：本次（最近一次）渲染作业的状态，与已发布版本分开。"""
    st = job.get("state")
    return {"id": job.get("job_id"), "state": st, "stage": job.get("stage"),
            "running": st in ("queued", "running"), "ok": (st == "succeeded") if st else None,
            "error": job.get("error"), "result": job.get("result"), "diag": job.get("diag")}


EXPORT_VARIANTS = {"filing": ("filing",), "disclosure": ("annotated",), "both": ("annotated", "filing")}
VARIANT_LABEL = {"annotated": "交底版", "filing": "递交版"}


def export_result(jid, rid, ver, formats, variant):
    """WP1 导出作业的 result：manifest 键为「图号/版本/格式」，值 ok|skip|fail；notes 记失败明细。"""
    dwg_ok = LLM.get("dwg_available", True)
    manifest, notes = {}, []
    for fid in ver.get("figures") or []:
        for v in EXPORT_VARIANTS[variant]:
            label = VARIANT_LABEL[v]
            for fmt in formats:
                key = "%s/%s/%s" % (fid, label, fmt)
                if fmt == "pdf":
                    manifest[key] = "skip"                  # PDF 主交付在 WP2
                elif fmt == "docx":
                    manifest[key] = "ok" if label == "交底版" else "skip"   # 仅交底版有 Word
                elif fmt == "dwg":
                    manifest[key] = "ok" if dwg_ok else "skip"
                elif fmt in ("dxf", "png"):
                    manifest[key] = "ok"
                else:
                    manifest[key] = "skip"
                    notes.append("%s：未知格式" % key)
    if "dwg" in formats and not dwg_ok:
        notes.append("DWG：本机未检出 ODA File Converter / LibreDWG，转换器不可用")
    return {"export_id": jid, "render_job_id": rid, "render_plan_etag": ver.get("plan_etag"),
            "manifest": manifest, "notes": notes, "dwg_log": "",
            "download_url": "/v1/exports/%s/download" % jid}


def norm_etag(e):
    e = (e or "").strip()
    if e.startswith("W/"):
        e = e[2:]
    return e.strip('"')


def run_job(job, kind, fail_stage, speed):
    job["state"] = "running"
    stages = STAGE_SETS[kind]
    _emit(job, "stage", {"stage": "queued", "progress": 0.0})
    done = []
    for i, (name, label, secs) in enumerate(stages):
        if job["_done"]:
            return
        job["stage"] = name
        t0 = time.time()
        _emit(job, "log", {"line": f"▶ {label}…"})
        time.sleep(max(0.05, secs * speed))
        elapsed = round(time.time() - t0, 2)
        if fail_stage and name == fail_stage:
            job["state"] = "failed"
            job["error"] = {"code": "stage_failed", "stage": name,
                            "message": f"{label}失败",
                            "hint": "请查看该步的日志；修正后可重试本作业。"}
            done.append({"stage": name, "label": label, "seconds": elapsed, "ok": False})
            job["stages"] = done
            _emit(job, "error", job["error"])
            with job["_cv"]:
                job["_done"] = True
                job["_cv"].notify_all()
            return
        prog = round((i + 1) / len(stages), 3)
        job["progress"] = prog
        done.append({"stage": name, "label": label, "seconds": elapsed, "ok": True})
        job["stages"] = done
        _emit(job, "stage", {"stage": name, "progress": prog, "seconds": elapsed, "label": label})
    if kind == "render" and job.get("project_id") and job.get("result") is not None:
        p = PROJECTS.get(job["project_id"])
        r = job["result"]
        if not r.get("ok"):
            # WP1：未全部过闸 / 计划未过校验 → 失败作业，带诊断；不发布，已发布版本保持不变
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
            _emit(job, "error", dict(err, job_id=job["job_id"]))
            with job["_cv"]:
                job["_done"] = True
                job["_cv"].notify_all()
            return
        # 成功：发布为当前版本（重新进入也能看到），并登记为可导出版本
        job["diag"] = {"version": job["job_id"], "sealed": True, "published": True,
                       "failed_figures": [], "missing_artefacts": [], "log_tail": ""}
        if p and p.get("data") is not None:
            p["data"]["render"] = r
            VERSIONS[job["job_id"]] = {"project": job["project_id"],
                                       "plan_etag": (job.get("inputs") or {}).get("plan_etag"),
                                       "figures": [f["id"] for f in r.get("figures", [])]}
    if job.get("_final") is not None:          # 导出等：结果在成功时才出现（运行中 result 为 null）
        job["result"] = job.pop("_final")
    job["state"] = "succeeded"
    if kind == "render" and job.get("project_id"):
        p = PROJECTS.get(job["project_id"])
        if p and p.get("data") is not None:
            p["data"]["render_job"] = render_job_status(job)
    _emit(job, "done", {"result": job.get("result")})
    with job["_cv"]:
        job["_done"] = True
        job["_cv"].notify_all()


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
#  HTTP                                                                        #
# --------------------------------------------------------------------------- #
def project_summary(p):
    base = {"project_id": p["id"], "title": p["title"], "description": p["desc"],
            "step_name": p["step"], "example": p["example"], "status": p["status"],
            "message": p.get("message", ""), "plan_etag": p["plan_etag"],
            "llm": LLM}
    d = p.get("data")
    if d:
        r = d.get("render")
        figs = (r or {}).get("figures", [])
        # 缩略图用渲染结果里图1的交底版变体 URL（真实资产所在），不靠按工程名拼路径
        thumb = None
        if figs:
            v = figs[0].get("variants") or {}
            thumb = v.get("annotated") or figs[0].get("svg")
        base.update(figures=len(figs),
                    figures_pass=sum(1 for f in figs if f.get("pass")),
                    flowcharts=0, thumb=thumb)
    else:
        base.update(figures=0, figures_pass=0, flowcharts=0, thumb=None)
    return base


def project_detail(p):
    out = project_summary(p)
    d = p.get("data") or {}
    out.update(assembly=d.get("assembly"), structure=d.get("structure"),
               candidates=d.get("candidates", []), render=d.get("render"),
               render_job=d.get("render_job"),          # 本次（最近一次）渲染作业，与已发布 render 分开
               plan=d.get("plan"),                      # 静态只读站要能读到 plan（S2-16）
               validate=d.get("validate") or {"ok": True, "issues": []},
               model_url=f"/v1/projects/{p['id']}/model.glb")
    return out


def render_payload(pid, fail, plan_etag=None):
    """渲染作业的权威结果：gearbox 用真实出图；其它工程借用 gearbox 演示闭环。
    结果携带 inputs.plan_etag 作为版本证据。fail 时体现 QA/校验未过。"""
    if fail == "validate":                   # 计划未过校验：无图 + validate.issues + log（Codex #4）
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
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, message, hint="", ecode="error"):
        self._send(code, {"error": {"code": ecode, "message": message, "hint": hint},
                          "detail": message})

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    # ---- routing ----
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/" :
            return self._redirect("/home.html")
        if path.startswith("/p/"):
            return self.legacy("GET", path, q)
        if path.startswith("/v1/") or path.startswith("/api/"):
            return self.api_get(path, q)
        return self.static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith("/p/"):
            return self.legacy("POST", u.path, parse_qs(u.query))
        return self.api_post(u.path, parse_qs(u.query), self._json_body())

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path.startswith("/p/"):
            return self.legacy("PUT", u.path, parse_qs(u.query))
        return self.api_put(u.path, parse_qs(u.query), self._json_body())

    # ---- legacy 子应用 /p/{pid}/ ----
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
            if rest.startswith("v1/"):
                return self.api_get("/" + rest, q)
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
            # mock 的渲染一律借用 gearbox 的真实出图：本工程没有该文件时回落到 gearbox 的资产
            sub = pid if (FIX / pid / name).is_file() else "gearbox"
            return self.static(f"/dev/fixtures/{sub}/{name}")
        if method == "GET" and api == "flowcharts":
            return self._send(200, {"flowcharts": []})
        if method == "PUT" and api == "plan":
            body = self._json_body()
            # WP1 契约：请求体就是 plan 本身（不是 {plan: …}）；包了一层多半是前端写错，直接拒绝
            plan = body
            if not isinstance(plan, dict) or set(plan) == {"plan"} or "terms" not in plan:
                return self._err(400, "请求体应为 figure-plan 本身（WP1 契约），不是 {plan: …}")
            p["data"]["plan"] = plan
            p["plan_rev"] += 1                                  # live 视图同步换版本
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

    def do_DELETE(self):
        u = urlparse(self.path)
        return self.api_delete(u.path, parse_qs(u.query))

    def _redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        # 列表
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
                    return self._send(200, {"loading": True, "projects": [], "llm": LLM,
                                            "data_dir": "~/墨线数据（演示）"})
                items = [project_summary(PROJECTS[i]) for i in order if i in PROJECTS]
            return self._send(200, {"projects": items, "llm": LLM,
                                    "data_dir": "~/墨线数据（演示）",
                                    "display_name": "墨线"})
        # 作业状态
        m = re.fullmatch(r"/v1/jobs/([\w\-]+)", path)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self._err(404, "没有这个作业")
            return self._send(200, job_dict(job))
        # SSE
        m = re.fullmatch(r"/v1/jobs/([\w\-]+)/events", path)
        if m:
            return self.sse(JOBS.get(m.group(1)), self.headers.get("Last-Event-ID"))
        # 工程详情
        m = re.fullmatch(r"/v1/projects/([\w\-]+)", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p:
                return self._err(404, "没有这个工程")
            return self._send(200, project_detail(p))
        # plan + ETag
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/plan", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p or not p.get("data"):
                return self._err(404, "没有这个工程")
            return self._send(200, p["data"]["plan"], extra={"ETag": p["plan_etag"]})   # WP1：响应体即 plan
        # 模型 GLB
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/model\.glb", path)
        if m:
            glb = GLB_BY_PROJECT.get(m.group(1))
            if not glb:
                return self._err(404, "没有模型")
            return self.static(f"/dev/fixtures/models/{glb}")
        # 预览 SVG
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/preview/([\w\-.]+)", path)
        if m:
            pid, name = m.group(1), m.group(2)
            if not re.fullmatch(r"[\w\-]+\.(svg|png)", name):
                return self._err(400, "非法文件名")
            sub = "gearbox" if pid == "gearbox" else pid
            return self.static(f"/dev/fixtures/{sub}/{name}")
        # 导出清单
        # 导出 = 作业（WP1：GET /v1/exports/{id} 返回 Job，清单在 result 内）
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
        # LLM 状态
        if path in ("/v1/llm/status", "/api/llm/status"):
            return self._send(200, LLM)
        return self._err(404, "未实现的 GET：%s" % path)

    # ---- SSE ----
    def sse(self, job, last_id):
        if not job:
            return self._err(404, "没有这个作业")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        start = (int(last_id) + 1) if (last_id and last_id.isdigit()) else 0
        try:
            while True:
                with job["_cv"]:
                    while len(job["_events"]) <= start and not job["_done"]:
                        job["_cv"].wait(timeout=10)
                    pending = job["_events"][start:]
                    done = job["_done"]
                for ev in pending:
                    chunk = (f"id: {ev['id']}\nevent: {ev['event']}\n"
                             f"data: {json.dumps(ev['data'], ensure_ascii=False)}\n\n")
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                    start = ev["id"] + 1
                if done and start > len(job["_events"]) - 1:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- POST ----
    def api_post(self, path, q, body):
        # 新建（上传）→ 启动 import 作业
        if path == "/v1/projects":
            scene = (q.get("fail") or [""])[0]
            fail = "解析" if scene == "parse" else None
            job = new_job("import", project_id="demo-new",
                          fail_stage=fail, speed=float((q.get("speed") or ["1"])[0]))
            return self._send(202, {"project_id": "demo-new", "job_id": job["job_id"]})
        if path == "/v1/projects/from-example":
            ex = (body.get("example") or "gearbox")
            pid = {"gearbox": "gearbox", "vise": "bench-vise",
                   "valve": "ball-valve"}.get(ex, "gearbox")
            job = new_job("import", project_id=pid, speed=0.6)
            return self._send(202, {"project_id": pid, "job_id": job["job_id"]})
        # 候选分图
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/plan/suggest", path)
        if m:
            job = new_job("suggest", project_id=m.group(1))
            return self._send(202, {"job_id": job["job_id"]})
        # 渲染
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/render", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p:
                return self._err(404, "没有这个工程")
            fail = (q.get("fail") or [""])[0]   # ""|"gate"|"validate"
            # 渲染进程照常跑完（4 阶段都成功）；QA 的过/不过与校验失败体现在 result 里，而不是作业失败
            # 输入快照随作业固化（对齐 WP1 openapi：202 带 inputs{plan_hash, plan_etag, …}）
            plan_raw = json.dumps((p.get("data") or {}).get("plan"), ensure_ascii=False, sort_keys=True)
            inputs = {"plan_etag": p.get("plan_etag"),
                      "plan_hash": hashlib.sha256(plan_raw.encode("utf-8")).hexdigest()}
            speed = float((q.get("speed") or ["1"])[0])   # 开发用：放慢作业（测试刷新恢复）
            job = new_job("render", project_id=m.group(1), speed=speed,
                          result=render_payload(m.group(1), fail, p.get("plan_etag")))
            job["inputs"] = inputs
            job["result"]["render_job_id"] = job["result"]["version"] = job["job_id"]   # 结果 ID = 版本
            if p.get("data") is not None:
                p["data"]["render_job"] = render_job_status(job)
            return self._send(202, {"job_id": job["job_id"], "inputs": inputs})
        # 导出
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/export", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p or not p.get("data"):
                return self._err(404, "没有这个工程")
            if body.get("sheet_preset"):
                return self._err(400, "sheet_preset 尚未实现（WP2）；请不要传该参数", ecode="bad_request")
            # WP1 契约：绑定一次成功渲染（缺省取已发布版本），plan_etag 与该渲染的冻结输入比对
            rid = body.get("render_job_id") or ((p["data"].get("render") or {}).get("render_job_id"))
            ver = VERSIONS.get(rid or "")
            if not ver or ver["project"] != m.group(1):
                return self._err(409, "没有可导出的成功渲染（render_job_id 无效或未成功）",
                                 "请先出图并全部过闸再导出", "render_job_invalid")
            want = body.get("plan_etag")
            if want and norm_etag(want) != norm_etag(ver["plan_etag"]):
                return self._err(409, "请求的 plan_etag 与被导出渲染的输入版本不一致",
                                 "计划已变更，请重新出图后再导出", "plan_changed")
            formats = body.get("formats") or ["docx", "dxf"]
            variant = body.get("variant") or "both"
            if variant not in EXPORT_VARIANTS:
                return self._err(400, "variant 只能是 filing / disclosure / both", ecode="bad_request")
            job = new_job("export", project_id=m.group(1))
            job["inputs"] = {"formats": formats, "variant": variant, "dpi": int(body.get("dpi") or 300),
                             "render_job_id": rid}
            job["_final"] = export_result(job["job_id"], rid, ver, formats, variant)
            # WP1：export_id 就是导出作业的 id
            return self._send(202, {"export_id": job["job_id"], "job_id": job["job_id"], "render_job_id": rid})
        # AI 起名（F9）：WP1 契约 POST /v1/projects/{pid}/ai/draft-terms（无请求体）——服务端起草并只填
        # 空白术语（人填过的不动），返回 {suggestions, filled:[selector], plan_valid, issues}；失败 422 {detail}
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/ai/draft-terms", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p or not p.get("data"):
                return self._err(404, "没有这个工程")
            if not LLM["configured"]:
                return self._send(422, {"detail": "大模型调用失败：AI 通道未配置（演示）"})
            names = {**GEARBOX_NAMES, **VISE_NAMES, **VALVE_NAMES}
            plan = p["data"]["plan"]
            low = {"PGB-PLANET-PIN", "VISE-THRUST-COLLAR", "BV-PACKING"}
            sugg, filled = [], []
            for t in plan.get("terms", []):
                sel = t.get("selector", "")
                if (t.get("term") or "").strip():
                    continue
                term = names.get(sel) or (sel.split("-")[-1].title() + " 件")
                sugg.append({"selector": sel, "term": term, "label": "once",
                             "confidence": "low" if sel in low else "high",
                             "reason": "命名多义，建议人工复核" if sel in low else ""})
                t["term"] = term
                filled.append(sel)
            if filled:                                   # 服务端写了 plan → 换版本（前端须重读 plan 与 ETag）
                p["plan_rev"] += 1
                p["plan_etag"] = 'W/"%s-%d"' % (p["id"], p["plan_rev"])
            return self._send(200, {"suggestions": sugg, "filled": filled, "plan_valid": True, "issues": []})
        return self._err(404, "未实现的 POST：%s" % path)

    # ---- PUT ----
    def api_put(self, path, q, body):
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/plan", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p or not p.get("data"):
                return self._err(404, "没有这个工程")
            if_match = self.headers.get("If-Match")
            if if_match and if_match != p["plan_etag"]:
                return self._err(409, "plan 已被另一处修改（ETag 不匹配）",
                                 "请重新载入最新 plan 后再改。", "etag_conflict")
            # WP1 契约：请求体就是 plan 本身（不是 {plan: …}）；包了一层多半是前端写错，直接拒绝
            plan = body
            if not isinstance(plan, dict) or set(plan) == {"plan"} or "terms" not in plan:
                return self._err(400, "请求体应为 figure-plan 本身（WP1 契约），不是 {plan: …}")
            p["data"]["plan"] = plan
            p["plan_rev"] += 1
            p["plan_etag"] = 'W/"%s-%d"' % (p["id"], p["plan_rev"])
            # 轻量校验：术语里出现件号形状的串报错（与真实后端 FORBIDDEN_TEXT 同源的演示）
            issues = []
            for i, t in enumerate(plan.get("terms", [])):
                term = (t.get("term") or "").strip()
                if term and re.search(r"[A-Za-z]{2,}[-_]?\d{3,}", term):
                    issues.append({"code": "E_TERM_FORBIDDEN", "severity": "error",
                                   "pointer": "/terms/%d" % i,
                                   "message": "术语「%s」像内部件号，不是中文技术名词" % term,
                                   "hint": "改成中文技术名词"})
            validate = {"ok": not issues, "issues": issues}
            p["data"]["validate"] = validate
            return self._send(200, {"ok": True, "validate": validate}, extra={"ETag": p["plan_etag"]})
        # 结构组改名（Codex #9）：更新内存结构，回读可见
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/structure", path)
        if m:
            p = PROJECTS.get(m.group(1))
            if not p or not p.get("data") or not p["data"].get("structure"):
                return self._err(404, "没有这个工程的结构")
            for g in (body.get("groups") or []):
                for grp in p["data"]["structure"].get("groups", []):
                    if grp["id"] == g.get("id") and g.get("name"):
                        grp["name"] = g["name"]
            return self._send(200, {"structure": p["data"]["structure"]})
        return self._err(404, "未实现的 PUT：%s" % path)

    def api_delete(self, path, q):
        m = re.fullmatch(r"/v1/jobs/([\w\-]+)", path)
        if m:
            job = JOBS.get(m.group(1))
            if job:
                job["state"] = "cancelled"
                with job["_cv"]:
                    job["_done"] = True
                    job["_cv"].notify_all()
            return self._send(200, {"ok": True})
        return self._err(404, "未实现的 DELETE：%s" % path)


def main():
    ap = argparse.ArgumentParser(description="墨线 WP3 前端 mock 服务器（纯标准库）")
    ap.add_argument("--port", type=int, default=8803)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-ai", action="store_true", help="演示 AI 未配置失败场景")
    ap.add_argument("--no-dwg", action="store_true", help="演示 DWG 不可用失败场景")
    args = ap.parse_args()
    if args.no_ai:
        LLM.update(provider="none", provider_label="AI 未配置", configured=False)
    if args.no_dwg:
        LLM["dwg_available"] = False
    seed()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"墨线 mock 服务器 · http://{args.host}:{args.port}/home.html")
    print(f"  工程详情示例： http://{args.host}:{args.port}/index.html?project=gearbox")
    print(f"  四态演示见 webui/dev/README.md（?scene= / ?force= / ?state= 开关）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
