#!/usr/bin/env python3
"""墨线 Web 工作台 · WP3 前端开发用 mock 服务器（纯标准库，Python 3.9+）。

后端（WP1/WP2）尚未接线，前端对本 mock 开发。本文件严格按《顶层架构 v1 §6 API 契约》
模拟全部 /v1/* 端点，并保留旧 /api/* 的少量只读兼容。覆盖：

* 作业的分阶段 SSE 进度（接收 / 解析 / 网格 / 候选分图 / 试渲染）、可重连、带 event id；
* plan 的 ETag 乐观锁与 409 冲突；
* 失败场景：闸门未过（bench-vise 整机候选被拦）、AI 未配置、DWG 不可用；
* 三个公开示例工程的真实资产（GLB / SVG / QA JSON / 件号表，来自 webui/dev/fixtures/，
  由现有 CLI 生成，只作 mock 数据）。

OpenAPI（docs/openapi.yaml）WP1 尚未提供，本 mock 的契约为自拟，差异点见 webui/dev/README.md。

启动：
    python3 webui/dev/mock_server.py --port 8803
然后浏览器打开 http://127.0.0.1:8803/home.html
"""
from __future__ import annotations

import argparse
import copy
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
        "render": {"ok": True, "figures": figs, "numerals": numerals,
                   "figure_descriptions": descs, "finished_at": "2026-10-11 10:31"},
        "candidates": [
            {"fig_id": "fig1", "caption": "整体结构示意图", "kind": "assembly",
             "members": ["电机子装配", "输出与密封", "内齿圈"], "status": "pass",
             "reason": "字高 7.0 mm、占用 0.79，13 项 QA 全过", "labels": 6},
            {"fig_id": "fig2", "caption": "齿轮系分解示意图", "kind": "exploded",
             "members": ["行星齿轮系", "输出轴"], "status": "pass",
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
             "members": ["固定钳身组", "活动钳身组", "丝杠手柄组"], "status": "fail",
             "qa": failing_qa(rows),
             "reason": "整机 22 个标记塞进一张图：字高 2.4<3.5 mm、标记 22>20、引线 3 处交叉。"
                       "建议按结构拆成「固定钳身 / 活动钳身 / 丝杠手柄」三张分解图。", "labels": 22},
            {"fig_id": "v-screw", "caption": "丝杠手柄机构分解图", "kind": "exploded",
             "members": ["丝杠手柄组"], "status": "pass",
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
EXPORTS = {}         # export_id -> manifest
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
    job = {"job_id": jid, "kind": kind, "project_id": project_id,
           "state": "queued", "stage": None, "progress": 0.0, "logs": [],
           "stages": [], "inputs": {}, "result": result, "error": None,
           "_events": [], "_cv": threading.Condition(), "_done": False}
    JOBS[jid] = job
    t = threading.Thread(target=run_job, args=(job, kind, fail_stage, speed), daemon=True)
    t.start()
    return job


def _emit(job, etype, data):
    with job["_cv"]:
        eid = len(job["_events"])
        job["_events"].append({"id": eid, "event": etype, "data": data})
        job["_cv"].notify_all()


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
    job["state"] = "succeeded"
    _emit(job, "done", {"result": job.get("result")})
    with job["_cv"]:
        job["_done"] = True
        job["_cv"].notify_all()


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
        base.update(figures=len(figs),
                    figures_pass=sum(1 for f in figs if f.get("pass")),
                    flowcharts=0,
                    thumb=(f"/v1/projects/{p['id']}/preview/fig1_annotated.svg" if r else None))
    else:
        base.update(figures=0, figures_pass=0, flowcharts=0, thumb=None)
    return base


def project_detail(p):
    out = project_summary(p)
    d = p.get("data") or {}
    out.update(assembly=d.get("assembly"), structure=d.get("structure"),
               candidates=d.get("candidates", []), render=d.get("render"),
               plan=d.get("plan"),                      # 静态只读站要能读到 plan（S2-16）
               model_url=f"/v1/projects/{p['id']}/model.glb")
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
        if path.startswith("/v1/") or path.startswith("/api/"):
            return self.api_get(path, q)
        return self.static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        return self.api_post(u.path, parse_qs(u.query), self._json_body())

    def do_PUT(self):
        u = urlparse(self.path)
        return self.api_put(u.path, parse_qs(u.query), self._json_body())

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
        if rel.startswith("dev/"):
            target = WEBUI / rel
        else:
            target = WEBUI / rel
        target = target.resolve()
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
            return self._send(200, {k: job[k] for k in
                ("job_id", "kind", "project_id", "state", "stage", "progress",
                 "logs", "stages", "inputs", "result", "error")})
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
            return self._send(200, {"plan": p["data"]["plan"]},
                              extra={"ETag": p["plan_etag"]})
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
        m = re.fullmatch(r"/v1/exports/([\w\-]+)", path)
        if m:
            exp = EXPORTS.get(m.group(1))
            if not exp:
                return self._err(404, "没有这个导出")
            return self._send(200, exp)
        m = re.fullmatch(r"/v1/exports/([\w\-]+)/download", path)
        if m:
            exp = EXPORTS.get(m.group(1))
            if not exp:
                return self._err(404, "没有这个导出")
            body = ("这是 mock 的占位 zip（%s）。真实后端在此返回打包好的交付件。"
                    % exp["zip"]).encode("utf-8")
            return self._send(200, body, "application/zip",
                              {"Content-Disposition": f'attachment; filename="{exp["zip"]}"'})
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
            wait = (q.get("wait") or ["0"])[0] == "1"
            fail = (q.get("fail") or [""])[0] == "gate"
            job = new_job("render", project_id=m.group(1),
                          fail_stage=("QA" if fail else None),
                          result={"ok": not fail})
            return self._send(202, {"job_id": job["job_id"]})
        # 导出
        m = re.fullmatch(r"/v1/projects/([\w\-]+)/export", path)
        if m:
            p = PROJECTS.get(m.group(1))
            formats = body.get("formats", ["pdf", "docx", "dxf"])
            variant = body.get("variant", "both")
            want_dwg = "dwg" in formats
            dwg_ok = LLM.get("dwg_available", True)   # 默认可用；demo-dwg-missing 置 false
            exp_id = "exp_" + uuid.uuid4().hex[:8]
            items = []
            matrix = {"pdf": ("递交版 PDF", "ok"), "docx": ("交底版 Word", "ok"),
                      "dxf": ("DXF 源文件", "ok"), "dwg": ("DWG 源文件", "ok" if dwg_ok else "skip")}
            for f in formats:
                label, st = matrix.get(f, (f, "ok"))
                it = {"format": f, "label": label, "status": st}
                if f == "dwg" and not dwg_ok:
                    it["reason"] = "本机未检出 ODA File Converter / LibreDWG，DWG 转换器不可用"
                items.append(it)
            job = new_job("export", project_id=m.group(1))
            EXPORTS[exp_id] = {
                "export_id": exp_id, "job_id": job["job_id"], "variant": variant,
                "items": items, "zip": f"{m.group(1)}-{variant}-20261011.zip",
                "download_url": f"/v1/exports/{exp_id}/download",
                "manifest": ["递交版_图1.pdf", "递交版_图2.pdf", "交底版_说明书附图.docx",
                             "fig1.dxf", "fig2.dxf", "附图说明.txt", "核对清单.txt"],
            }
            return self._send(202, {"export_id": exp_id, "job_id": job["job_id"]})
        # AI 起名（F9）
        if path in ("/v1/ai/draft-terms", "/api/llm/draft-terms"):
            if not LLM["configured"]:
                return self._err(409, "AI 通道未配置", "到设置里填 DeepSeek 密钥，或改用本机 MCP 客户端。",
                                 "ai_not_configured")
            return self._send(200, {"suggestions": [
                {"selector": "PGB-PLANET-PIN", "term": "行星销", "confidence": "low",
                 "reason": "销类零件命名多义，建议人工复核"},
                {"selector": "PGB-OIL-SEAL", "term": "油封", "confidence": "high", "reason": ""},
            ], "filled": 2})
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
            plan = body.get("plan")
            if not isinstance(plan, dict):
                return self._err(400, "body.plan 必须是对象")
            p["data"]["plan"] = plan
            p["plan_rev"] += 1
            p["plan_etag"] = 'W/"%s-%d"' % (p["id"], p["plan_rev"])
            return self._send(200, {"ok": True}, extra={"ETag": p["plan_etag"]})
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
