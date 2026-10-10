#!/usr/bin/env python3
"""专利附图工作台：多工程 Web 工作流（Plan Studio 的上层）。

    python3 scripts/workbench.py                 # 启动，浏览器打开 http://127.0.0.1:8790/?token=…
    python3 scripts/workbench.py seed --step ASM.stp --plan plan.json --title "示例" [--glossary names.py]

一个页面串起整条链路：首页（示例轮播 + 工程列表 + 设置）→ 每个工程一个 Plan Studio
（3D 点选 / 框选零件、分图、命名、AI 起草、出图、规范标注、流程图、导出）。

对外有两个入口，共用同一个 token：
* REST：``/v1/*``，文档在 ``/docs``；
* MCP：``POST /mcp``（Streamable HTTP），千问办公等客户端按 URL 挂载。

数据目录默认 ``~/.patent-workbench``（工程、STEP 副本、出图结果，**不进 git**）；
密钥与 token 在 ``~/.config/patent-workbench/config.json``（0600）。
只监听 127.0.0.1，并校验 Host 头。
"""

from __future__ import annotations

import argparse
import ast
import os
import base64
import json
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import plan_studio as PS  # noqa: E402
import studio_ext as EXT  # noqa: E402
import workbench_llm as LLM  # noqa: E402
import workbench_mcp as MCP  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

REPO = SCRIPTS.parent
WEBUI = PS.WEBUI
DEFAULT_DATA = Path.home() / ".patent-workbench"
DEFAULT_PORT = 8790
PID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_\-]+\.(png|svg|dxf|dwg|json|glb)$")
MEDIA = {"png": "image/png", "svg": "image/svg+xml", "dxf": "application/octet-stream",
         "dwg": "application/octet-stream", "json": "application/json",
         "glb": "model/gltf-binary"}
PROTECTED = ("/api/wb", "/v1/", "/mcp", "/files/")
SYNTHETIC_STEP = REPO / "tests" / "fixtures" / "synthetic.stp"


# ----------------------------------------------------------------------------- token


def ensure_token() -> str:
    import os
    env = os.environ.get("WORKBENCH_TOKEN")
    if env:
        return env
    cfg = LLM.load_config()
    if not cfg.get("api_token"):
        cfg["api_token"] = secrets.token_urlsafe(24)
        LLM.save_config(cfg)
    return cfg["api_token"]


# ----------------------------------------------------------------------------- projects


class Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.meta_path = root / "project.json"

    @property
    def meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def update(self, **kw) -> None:
        m = self.meta
        m.update(kw)
        self.meta_path.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def id(self) -> str:
        return self.root.name

    def workspace(self) -> PS.Workspace:
        return PS.Workspace(Path(self.meta["step"]), self.root / "studio")

    def _structure_brief(self) -> dict:
        try:
            data = json.loads((self.root / "studio" / "structure.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"source": None, "groups": []}
        return {"source": data.get("source"),
                "groups": [{"name": g["name"], "count": len(g["parts"])} for g in data.get("groups", [])]}

    def summary(self) -> dict:
        m = self.meta
        studio = self.root / "studio"
        last = {}
        try:
            last = json.loads((studio / "last_render.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        figs = last.get("figures", [])
        thumb = next((f.get("annotation", {}).get("png") for f in figs
                      if f.get("annotation", {}).get("png")), None)
        return {
            "id": self.id, "title": m.get("title", self.id), "status": m.get("status"),
            "message": m.get("message", ""), "example": bool(m.get("example")),
            "description": m.get("description", ""), "created": m.get("created"),
            "step_name": Path(m.get("step", "")).name,
            "figures": len(figs), "figures_pass": sum(1 for f in figs if f.get("pass")),
            "flowcharts": len(list((studio / "flowcharts").glob("*.json"))),
            "rendered_at": last.get("finished_at"),
            "structure": self._structure_brief(),
            "thumb": ("/files/%s/out/%s" % (self.id, Path(thumb).name)) if thumb else None,
        }


class Registry:
    def __init__(self, data: Path, token: str) -> None:
        self.data = data
        self.dir = data / "projects"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.apps: Dict[str, object] = {}
        self.lock = threading.Lock()

    def all(self) -> List[Project]:
        projects = [Project(p) for p in sorted(self.dir.iterdir())
                    if (p / "project.json").is_file()]
        return sorted(projects, key=lambda p: (not p.meta.get("example"),
                                               p.meta.get("order", 50),
                                               p.meta.get("created", "")))

    def get(self, pid: str) -> Optional[Project]:
        if not PID_RE.match(pid or ""):
            return None
        p = Project(self.dir / pid)
        return p if p.meta_path.is_file() else None

    def need(self, pid: str) -> Project:
        p = self.get(pid)
        if p is None:
            raise HTTPException(404, "没有这个工程：%s" % pid)
        return p

    def create(self, title: str, *, step_path: Optional[Path] = None,
               step_bytes: Optional[bytes] = None, filename: str = "model.stp",
               pid: Optional[str] = None, example: bool = False, description: str = "",
               plan: Optional[dict] = None, glossary: Optional[dict] = None,
               flowcharts: Optional[Dict[str, dict]] = None, render: bool = False,
               link: bool = False, order: int = 50) -> Project:
        pid = pid or "p-%s-%s" % (time.strftime("%Y%m%d"), uuid.uuid4().hex[:6])
        if not PID_RE.match(pid):
            raise ValueError("工程 id 只能用小写字母、数字、连字符")
        root = self.dir / pid
        if root.exists():
            raise ValueError("工程 %s 已存在" % pid)
        suffix = Path(filename).suffix.lower() or ".stp"
        if suffix not in (".stp", ".step"):
            raise ValueError("只接受 .stp / .step 装配体")
        (root / "studio").mkdir(parents=True)
        if step_bytes is not None:
            step = root / ("model" + suffix)
            step.write_bytes(step_bytes)
        elif link:
            step = Path(step_path).resolve()
        else:
            step = root / ("model" + suffix)
            shutil.copy2(step_path, step)
        if plan:
            plan = json.loads(json.dumps(plan))
            plan.setdefault("source", {})["step"] = str(step)
            (root / "studio" / "plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if glossary:
            (root / "studio" / "glossary.json").write_text(
                json.dumps({"terms": glossary}, ensure_ascii=False, indent=2), encoding="utf-8")
        for fid, spec in (flowcharts or {}).items():
            EXT._dump(root / "studio" / "flowcharts" / (fid + ".json"), spec)
        proj = Project(root)
        proj.meta_path.write_text(json.dumps({
            "id": pid, "title": title, "step": str(step), "example": example, "order": order,
            "description": description, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "preparing", "message": "正在解析装配体与导出 3D 预览…",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        threading.Thread(target=self._prepare, args=(proj, render), daemon=True).start()
        return proj

    def _prepare(self, proj: Project, render: bool) -> None:
        try:
            PS.prepare(proj.workspace(), quiet=True)
        except SystemExit as exc:
            proj.update(status="error", message="准备失败：%s" % exc)
            return
        except Exception as exc:  # pragma: no cover
            proj.update(status="error", message="准备失败：%r" % exc)
            return
        proj.update(status="ready", message="")
        if render:
            proj.update(message="正在出图…")
            try:
                studio = self.app_for(proj.id).state.studio
                studio.render_and_wait()
                for fid in sorted(p.stem for p in (proj.root / "studio" / "flowcharts").glob("*.json")):
                    EXT.render_flowchart(studio.ws, fid, PS.run_cli, PS.dxf_to_svg)
                proj.update(message="")
            except Exception as exc:
                proj.update(message="出图未完成：%s" % exc)
        self._auto_structure(proj)

    def _auto_structure(self, proj: Project) -> None:
        """有大模型通道且还没识别过结构时，后台识别一次（推理模型可能要几分钟）。"""
        studio = proj.root / "studio"
        if (studio / "structure.json").is_file():
            return
        if LLM.public_settings().get("provider") in (None, "none"):
            return
        keep = proj.meta.get("message", "")
        proj.update(message="AI 正在识别结构…")
        try:
            ws = proj.workspace()
            data = LLM.analyze_structure(
                json.loads(ws.assembly.read_text(encoding="utf-8")),
                json.loads(ws.plan.read_text(encoding="utf-8")),
                glossary=EXT.glossary_of(ws), bom=EXT.bom_names_of(ws))
            (studio / "structure.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            proj.update(message=keep)
        except Exception as exc:
            proj.update(message="结构识别未完成：%s（可在工程里手动重试）" % str(exc)[:80])

    def app_for(self, pid: str):
        proj = self.get(pid)
        if proj is None or proj.meta.get("status") != "ready":
            return None
        with self.lock:
            app = self.apps.get(pid)
            if app is None:
                app = PS.build_app(proj.workspace(), self.token, home_url="/")
                self.apps[pid] = app
            return app

    def studio(self, pid: str):
        self.need(pid)
        app = self.app_for(pid)
        if app is None:
            raise HTTPException(409, "工程还在准备中或准备失败，稍后再试")
        return app.state.studio

    def delete(self, pid: str) -> None:
        proj = self.need(pid)
        with self.lock:
            self.apps.pop(pid, None)
        shutil.rmtree(proj.root)


# ----------------------------------------------------------------------------- tools


class Tools:
    """REST /v1 与 MCP 共用的工具实现。"""

    def __init__(self, reg: Registry, base_url: str) -> None:
        self.reg = reg
        self.base = base_url.rstrip("/")

    def url(self, pid: str, area: str, name: str) -> str:
        return "%s/files/%s/%s/%s" % (self.base, pid, area, name)

    def list_projects(self, args: dict) -> dict:
        return {"projects": [p.summary() for p in self.reg.all()],
                "workbench": self.base + "/"}

    def get_project_parts(self, args: dict) -> dict:
        studio = self.reg.studio(args.get("project_id", ""))
        ws = studio.ws
        asm = json.loads(ws.assembly.read_text(encoding="utf-8"))
        plan = json.loads(ws.plan.read_text(encoding="utf-8"))
        terms = {t.get("selector"): t for t in plan.get("terms", [])}
        only = args.get("only_unnamed", True)
        rows = []
        for p in asm.get("parts", []):
            if p.get("degenerate"):
                continue
            t = terms.get(p["name"], {})
            if only and (t.get("term") or "").strip():
                continue
            rows.append({"selector": p["name"], "instances": p.get("instances"),
                         "size_mm": [round(float(v), 1) for v in p.get("bbox_size", [])],
                         "path": (p.get("path_sample") or "")[-100:],
                         "term": t.get("term", ""), "label": t.get("label", "once")})
        return {"project_id": args["project_id"], "parts": rows, "count": len(rows),
                "glossary": EXT.glossary_of(ws)}

    def update_terms(self, args: dict) -> dict:
        studio = self.reg.studio(args.get("project_id", ""))
        rows = args.get("terms") or []
        bad = [r for r in rows if re.search(r"[0-9A-Za-z_]", str(r.get("term", "")))]
        if bad:
            raise MCP.ToolError("这些名称含数字/字母，不能当专利附图名称：%s"
                                % [r.get("term") for r in bad][:8])
        applied = EXT.apply_terms(studio.ws, rows, overwrite=bool(args.get("overwrite")))
        check = studio.validate_now()
        return {"filled": applied["filled"], "kept_human": applied["kept_human"],
                "added": applied["added"], "plan_valid": check["ok"],
                "issues": check["issues"][:20]}

    def render_project(self, args: dict) -> dict:
        pid = args.get("project_id", "")
        studio = self.reg.studio(pid)
        res = studio.render_and_wait()
        return self._figures(pid, res, brief=True)

    def get_project_figures(self, args: dict):
        pid = args.get("project_id", "")
        studio = self.reg.studio(pid)
        res = studio.render_state.get("result") or {}
        data = self._figures(pid, res, brief=False)
        if args.get("include_images"):
            imgs = []
            for f in data["figures"]:
                png = studio.ws.out / (f["id"] + "_annotated.png")
                if png.is_file():
                    imgs.append(png.read_bytes())
            return data, imgs[:6]
        return data

    def _figures(self, pid: str, res: dict, brief: bool) -> dict:
        figs = []
        for f in res.get("figures", []):
            ann = f.get("annotation") or {}
            failed = [{"check": c["id"], "value": c.get("value"), "hint": c.get("hint")}
                      for c in (f.get("qa") or {}).get("checks", []) if not c.get("pass")]
            row = {"id": f["id"], "number": f.get("number"), "pass": f.get("pass"),
                   "failed_checks": failed,
                   "table": [(r["numeral"], r["name"]) for r in
                             ((ann.get("table") or {}).get("rows") or [])],
                   "figure_description": ann.get("figure_description", "")}
            row["files"] = {
                "交底版PNG": self.url(pid, "out", f["id"] + "_annotated.png"),
                "交底版DXF": self.url(pid, "out", f["id"] + "_annotated.dxf"),
                "递交版DXF": self.url(pid, "out", f["id"] + "_filing.dxf"),
            }
            figs.append(row)
        nums = res.get("numerals") or {}
        return {"project_id": pid, "ok": bool(res.get("ok")), "finished_at": res.get("finished_at"),
                "figures": figs, "附图说明": res.get("figure_descriptions", []),
                "附图标记说明": nums.get("description_zh", ""),
                "studio": "%s/p/%s/" % (self.base, pid),
                "log_tail": (res.get("log") or "")[-1500:] if not res.get("ok") else ""}

    def get_project_structure(self, args: dict) -> dict:
        studio = self.reg.studio(args.get("project_id", ""))
        data = EXT.load_structure(studio.ws)
        names = data.get("names") or {}
        groups = [{"name": g["name"], "role": g.get("role", ""),
                   "parts": [{"selector": n, "name": names.get(n, {}).get("name", "")}
                             for n in g["parts"]]} for g in data.get("groups", [])]
        return {"project_id": args["project_id"], "source": data.get("source"),
                "provider": data.get("provider", ""), "groups": groups}

    def ai_draft_terms(self, args: dict) -> dict:
        studio = self.reg.studio(args.get("project_id", ""))
        ws = studio.ws
        try:
            rows = LLM.draft_terms(json.loads(ws.assembly.read_text(encoding="utf-8")),
                                   json.loads(ws.plan.read_text(encoding="utf-8")),
                                   glossary=EXT.glossary_of(ws), bom=EXT.bom_names_of(ws))
        except LLM.LLMError as exc:
            raise MCP.ToolError("大模型调用失败：%s" % exc)
        applied = EXT.apply_terms(ws, rows)
        check = studio.validate_now()
        return {"suggestions": rows, "filled": applied["filled"], "plan_valid": check["ok"],
                "issues": check["issues"][:20]}

    def get_flowchart_guide(self, args: dict) -> dict:
        return MCP.FLOW_GUIDE

    def render_flowchart(self, args: dict):
        from patent_figure import flowchart as FC
        spec = args.get("spec")
        if not isinstance(spec, dict):
            raise MCP.ToolError("spec 必须是 patent-flowchart/1 对象，先调 get_flowchart_guide 看格式")
        errors = [i for i in FC.validate(spec) if i["severity"] == "error"]
        if errors:
            return {"ok": False, "issues": errors}
        pid = args.get("project_id")
        if pid:
            studio = self.reg.studio(pid)
            fid = EXT.next_flow_id(studio.ws)
            EXT.save_flowchart(studio.ws, fid, spec)
            res = EXT.render_flowchart(studio.ws, fid, PS.run_cli, PS.dxf_to_svg)
            png = EXT.flow_out(studio.ws) / (fid + ".png")
            area_pid, area = pid, "flow"
        else:
            fid = "flow-" + uuid.uuid4().hex[:8]
            work = self.reg.data / "scratch"
            work.mkdir(parents=True, exist_ok=True)
            (work / (fid + ".json")).write_text(json.dumps(spec, ensure_ascii=False), "utf-8")
            res = FC.write(spec, work / (fid + ".dxf"), args.get("figure_number"))
            from patent_figure import sheet as SH
            png = work / (fid + ".png")
            SH.render_preview(work / (fid + ".dxf"), png)
            res["ok"] = True
            area_pid, area = "_scratch", "flow"
        out = {"ok": bool(res.get("ok")), "flowchart_id": fid, "caption": res.get("caption"),
               "steps": res.get("steps", []), "warnings": res.get("warnings", []),
               "figure_description": res.get("figure_description", ""),
               "files": {"PNG": self.url(area_pid, area, fid + ".png"),
                         "DXF": self.url(area_pid, area, fid + ".dxf")}}
        return (out, [png.read_bytes()]) if png.is_file() else out

    def generate_flowchart(self, args: dict):
        from patent_figure import flowchart as FC
        text = str(args.get("text", "")).strip()
        if len(text) < 6:
            raise MCP.ToolError("text 太短：请给一段方法步骤描述")
        try:
            spec = LLM.flowchart_from_text(text, str(args.get("title", "")), validate=FC.validate)
        except LLM.LLMError as exc:
            raise MCP.ToolError("大模型调用失败：%s" % exc)
        res = self.render_flowchart({"spec": spec, "project_id": args.get("project_id")})
        if isinstance(res, tuple):
            res[0]["spec"] = spec
        else:
            res["spec"] = spec
        return res

    def table(self) -> dict:
        return {name: getattr(self, name) for name in (
            "list_projects", "get_project_structure", "get_project_parts", "update_terms",
            "render_project",
            "get_project_figures", "ai_draft_terms", "get_flowchart_guide",
            "render_flowchart", "generate_flowchart")}


# ----------------------------------------------------------------------------- app


def _supplied_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return (request.headers.get("x-studio-token") or request.query_params.get("token")
            or (auth[7:] if auth.lower().startswith("bearer ") else "")
            or request.cookies.get("wb_token") or "")


def build_workbench(reg: Registry, token: str, port: int) -> FastAPI:
    base = "http://127.0.0.1:%d" % port
    tools = Tools(reg, base)
    app = FastAPI(title="专利附图工作台 API", version="1.0.0", docs_url="/docs",
                  openapi_url="/openapi.json", redoc_url=None,
                  description="所有接口需要 token：请求头 `Authorization: Bearer <token>`。"
                              "token 在工作台「设置 → 外部调用」里查看。")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return Response("仅限本机访问", status_code=403)
        path = request.url.path
        if path.startswith(PROTECTED) and not secrets.compare_digest(
                _supplied_token(request), token):
            return JSONResponse({"detail": "token 无效"}, status_code=401)
        response = await call_next(request)
        if path in ("/", "/index.html") and secrets.compare_digest(
                request.query_params.get("token", ""), token):
            # 浏览器里点外部链接（附图 PNG 等）时靠这个 cookie 带上身份
            response.set_cookie("wb_token", token, httponly=True, samesite="strict")
        if not path.startswith(("/files/", "/v1/", "/mcp")):
            response.headers["Cache-Control"] = "no-store"
        return response

    # ---- pages -------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(str(WEBUI / "home.html"))

    app.mount("/static", StaticFiles(directory=str(WEBUI)), name="static")

    # ---- workbench API (浏览器首页用) ----------------------------------------
    @app.get("/api/wb/state", include_in_schema=False)
    def wb_state():
        return {"projects": [p.summary() for p in reg.all()],
                "llm": LLM.public_settings(), "data_dir": str(reg.data),
                "endpoints": {"rest_docs": base + "/docs", "mcp": base + "/mcp"}}

    @app.post("/api/wb/projects", include_in_schema=False)
    async def wb_create(request: Request):
        title = request.query_params.get("title") or "未命名工程"
        name = request.query_params.get("name") or "model.stp"
        ctype = request.headers.get("content-type", "")
        try:
            if ctype.startswith("application/json"):
                body = await request.json()
                step = Path(str(body.get("step_path", ""))).expanduser()
                if not step.is_file():
                    raise HTTPException(422, "找不到 STEP 文件：%s" % step)
                proj = reg.create(body.get("title") or step.stem, step_path=step,
                                  filename=step.name, description=body.get("description", ""))
            else:
                data = await request.body()
                if len(data) < 100:
                    raise HTTPException(422, "上传的 STEP 是空的")
                proj = reg.create(title, step_bytes=data, filename=name)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return proj.summary()

    @app.get("/api/wb/projects/{pid}", include_in_schema=False)
    def wb_project(pid: str):
        return reg.need(pid).summary()

    @app.delete("/api/wb/projects/{pid}", include_in_schema=False)
    def wb_delete(pid: str):
        if reg.need(pid).meta.get("example"):
            raise HTTPException(409, "示例工程不能在网页上删除")
        reg.delete(pid)
        return {"ok": True}

    @app.get("/api/wb/carousel", include_in_schema=False)
    def wb_carousel():
        slides = []
        for p in reg.all():
            s = p.summary()
            studio = p.root / "studio"
            try:
                last = json.loads((studio / "last_render.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                last = {}
            for f in sorted(last.get("figures", []), key=lambda f: f.get("number", 99)):
                ann = f.get("annotation") or {}
                if not ann.get("png"):
                    continue
                slides.append({"project": p.id, "project_title": s["title"],
                               "example": s["example"], "kind": "structure",
                               "label": "图%s" % f.get("number", ""),
                               "caption": re.sub(r"^图\d+为", "", ann.get("figure_description", "")).rstrip("；。"),
                               "rows": len((ann.get("table") or {}).get("rows") or []),
                               "src": "/files/%s/out/%s" % (p.id, Path(ann["png"]).name)})
            for fl in EXT.list_flowcharts(SimpleWS(studio)):
                r = fl.get("result") or {}
                if r.get("ok"):
                    slides.append({"project": p.id, "project_title": s["title"],
                                   "example": s["example"], "kind": "flow",
                                   "label": r.get("caption", ""),
                                   "caption": re.sub(r"^图\d+为", "", r.get("figure_description", "")).rstrip("；。")
                                   or fl.get("title", ""),
                                   "rows": len(r.get("steps", [])),
                                   "src": "/files/%s/flow/%s.png" % (p.id, fl["id"])})
        slides.sort(key=lambda s: (not s["example"]))
        return {"slides": slides[:24]}

    @app.get("/api/wb/settings", include_in_schema=False)
    def wb_settings():
        return {"llm": LLM.public_settings(), "token": token,
                "mcp_url": base + "/mcp", "rest_docs": base + "/docs",
                "qwen_mcp_json": {"mcpServers": {"patent-figure-workbench": {
                    "url": base + "/mcp?token=" + token, "enabled": True,
                    "_displayName_zh": "专利附图工作台"}}}}

    @app.put("/api/wb/settings", include_in_schema=False)
    async def wb_settings_put(request: Request):
        body = await request.json()
        cfg = LLM.load_config()
        if body.get("provider") in ("auto", "deepseek", "codebuddy"):
            cfg["provider"] = body["provider"]
        ds = cfg.setdefault("deepseek", {})
        incoming = body.get("deepseek") or {}
        if str(incoming.get("api_key", "")).strip():
            ds["api_key"] = str(incoming["api_key"]).strip()
        if body.get("clear_key"):
            ds.pop("api_key", None)
        for k in ("base_url", "model"):
            if str(incoming.get(k, "")).strip():
                ds[k] = str(incoming[k]).strip()
        LLM.save_config(cfg)
        return {"llm": LLM.public_settings()}

    @app.post("/api/wb/settings/test", include_in_schema=False)
    def wb_settings_test():
        t0 = time.time()
        try:
            out = LLM.chat_json("你是连通性测试。", '只输出 JSON：{"ok": true, "reply": "连接正常"}',
                                max_tokens=50)
        except LLM.LLMError as exc:
            raise HTTPException(502, str(exc))
        return {"ok": True, "reply": out, "seconds": round(time.time() - t0, 1),
                "provider": LLM.public_settings()["provider_label"]}

    @app.get("/api/wb/projects/{pid}/structure", include_in_schema=False)
    def wb_structure(pid: str):
        proj = reg.need(pid)
        if proj.meta.get("status") != "ready":
            raise HTTPException(409, "工程还没准备好")
        return EXT.load_structure(proj.workspace())

    @app.get("/api/wb/showcase", include_in_schema=False)
    def wb_showcase():
        return build_showcase(reg)

    # ---- files -------------------------------------------------------------
    @app.get("/files/{pid}/{area}/{name}", include_in_schema=False)
    def files(pid: str, area: str, name: str):
        if not FILE_RE.match(name) or area not in ("out", "flow", "model", "snap"):
            raise HTTPException(400, "非法路径")
        if pid == "_scratch":
            path = reg.data / "scratch" / name
        elif area == "snap":
            path = reg.need(pid).root / "studio" / "snapshots" / name
        elif area == "model":
            if name != "model.glb":
                raise HTTPException(400, "非法路径")
            path = reg.need(pid).root / "studio" / "model.glb"
        else:
            proj = reg.need(pid)
            sub = "out" if area == "out" else "flow_out"
            path = proj.root / "studio" / sub / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(str(path), media_type=MEDIA[name.rsplit(".", 1)[1]])

    # ---- REST v1 -------------------------------------------------------------
    def call(name: str, args: dict):
        try:
            out = tools.table()[name](args)
        except MCP.ToolError as exc:
            raise HTTPException(422, str(exc))
        if isinstance(out, tuple):
            data, imgs = out
            data = dict(data)
            data["images_base64"] = [base64.b64encode(b).decode("ascii") for b in imgs]
            return data
        return out

    @app.get("/v1/projects", tags=["工程"], summary="列出工程")
    def v1_projects():
        return call("list_projects", {})

    @app.get("/v1/projects/{pid}/structure", tags=["工程"], summary="结构组（AI 识别的中文结构与零件名）")
    def v1_structure(pid: str):
        return call("get_project_structure", {"project_id": pid})

    @app.get("/v1/projects/{pid}/parts", tags=["工程"], summary="零件清单（起名用）")
    def v1_parts(pid: str, only_unnamed: bool = True):
        return call("get_project_parts", {"project_id": pid, "only_unnamed": only_unnamed})

    @app.post("/v1/projects/{pid}/terms", tags=["工程"], summary="写入零件中文名")
    async def v1_terms(pid: str, request: Request):
        body = await request.json()
        return call("update_terms", {"project_id": pid, "terms": body.get("terms", []),
                                     "overwrite": bool(body.get("overwrite"))})

    @app.post("/v1/projects/{pid}/ai/draft-terms", tags=["AI"], summary="DeepSeek 起草零件名")
    def v1_ai_terms(pid: str):
        return call("ai_draft_terms", {"project_id": pid})

    @app.post("/v1/projects/{pid}/render", tags=["出图"], summary="出图（同步，含规范标注）")
    def v1_render(pid: str):
        return call("render_project", {"project_id": pid})

    @app.get("/v1/projects/{pid}/figures", tags=["出图"], summary="最近一次出图结果")
    def v1_figures(pid: str, include_images: bool = False):
        return call("get_project_figures", {"project_id": pid, "include_images": include_images})

    @app.get("/v1/flowcharts/guide", tags=["流程图"], summary="流程图 JSON 格式说明")
    def v1_flow_guide():
        return MCP.FLOW_GUIDE

    @app.post("/v1/flowcharts/render", tags=["流程图"], summary="按语义 JSON 出流程图")
    async def v1_flow_render(request: Request):
        body = await request.json()
        return call("render_flowchart", body)

    @app.post("/v1/flowcharts/generate", tags=["AI"], summary="文字描述 → DeepSeek → 流程图")
    async def v1_flow_generate(request: Request):
        body = await request.json()
        return call("generate_flowchart", body)

    # ---- MCP ---------------------------------------------------------------
    @app.post("/mcp", include_in_schema=False)
    async def mcp_post(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "Parse error"}},
                                status_code=400)
        from starlette.concurrency import run_in_threadpool
        out = await run_in_threadpool(MCP.handle_payload, payload, tools.table())
        if out is None:
            return Response(status_code=202)
        headers = {}
        if isinstance(payload, dict) and payload.get("method") == "initialize":
            headers["Mcp-Session-Id"] = uuid.uuid4().hex
        return JSONResponse(out, headers=headers)

    @app.get("/mcp", include_in_schema=False)
    def mcp_get():
        return Response("本服务不推送 SSE；请用 POST", status_code=405,
                        headers={"Allow": "POST, DELETE"})

    @app.delete("/mcp", include_in_schema=False)
    def mcp_delete():
        return Response(status_code=200)

    app.state.tools = tools
    return app


def build_showcase(reg: "Registry") -> dict:
    """首页功能介绍的素材：全部取自本机示例工程（不进 git），按通用规则挑选。"""
    examples = [p for p in reg.all() if p.meta.get("example") and p.meta.get("status") == "ready"]
    figures, flows, live, names = [], [], None, []
    for p in examples:
        studio = p.root / "studio"
        try:
            last = json.loads((studio / "last_render.json").read_text(encoding="utf-8"))
            plan = json.loads((studio / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        kinds = {f["id"]: f.get("kind") for f in plan.get("figures", [])}
        for f in last.get("figures", []):
            ann = f.get("annotation") or {}
            if not (f.get("pass") and ann.get("png")):
                continue
            figures.append({
                "project": p.id, "project_title": p.meta.get("title", p.id),
                "number": f.get("number"), "kind": kinds.get(f["id"]),
                "rows": len((ann.get("table") or {}).get("rows") or []),
                "caption": re.sub(r"^图\d+为", "", ann.get("figure_description", "")).rstrip("；。"),
                "annotated": "/files/%s/out/%s_annotated.png" % (p.id, f["id"]),
                "filing": "/files/%s/out/%s_filing.png" % (p.id, f["id"]),
            })
        for fl in EXT.list_flowcharts(SimpleWS(studio)):
            r = fl.get("result") or {}
            if r.get("ok"):
                flows.append({"project": p.id, "project_title": p.meta.get("title", p.id),
                              "caption": fl.get("title", ""), "label": r.get("caption", ""),
                              "steps": len(r.get("steps", [])), "warned": bool(r.get("warnings")),
                              "src": "/files/%s/flow/%s.png" % (p.id, fl["id"])})
        sfile = studio / "structure.json"
        if sfile.is_file():
            data = EXT.load_structure(p.workspace())     # 已按 plan 的排除名单过滤
            if data.get("source") == "ai":
                snaps = {q.stem: "/files/%s/snap/%s" % (p.id, q.name)
                         for q in sorted((studio / "snapshots").glob("*.png"))}
                cand = {"project": p.id, "project_title": p.meta.get("title", p.id),
                        "structure": data, "snaps": snaps}
                # 有展示图的优先；都有或都没有时取排序在前的示例
                if live is None or (snaps and not live["snaps"]):
                    live = cand
    struct = None
    if live:   # 结构卡、对照卡、透明 / 爆炸卡讲同一台机器
        data = live["structure"]
        nm = data.get("names") or {}
        groups = []
        for g in data.get("groups", []):
            cn = [nm[n]["name"] for n in g["parts"] if nm.get(n, {}).get("name")]
            code = next((n for n in g["parts"] if nm.get(n, {}).get("name")
                         and re.search(r"[A-Za-z_]", n)), None)
            groups.append({"name": g["name"], "role": g.get("role", ""), "count": len(g["parts"]),
                           "sample": cn[:4],
                           "code": ({"code": code, "name": nm[code]["name"]} if code else None)})
            if code:
                names.append({"code": code, "name": nm[code]["name"], "group": g["name"]})
        struct = {"project": live["project"], "project_title": live["project_title"],
                  "provider": data.get("provider", ""), "groups": groups,
                  "total": sum(g["count"] for g in groups), "snaps": live["snaps"]}
    assembly = [f for f in figures if f["kind"] == "assembly"]
    exploded = sorted([f for f in figures if f["kind"] == "exploded"], key=lambda f: -f["rows"])
    pick = {
        "full": (assembly or figures or [None])[0],
        "exploded": (exploded or [None])[0],
        "detail": next((f for f in exploded if re.search(r"传动|齿轮|减速|机构", f["caption"])
                        and f is not (exploded or [None])[0]), (exploded[1:] or [None])[0]),
        "filing": None,
        "flow": (sorted(flows, key=lambda f: (f["warned"], -f["steps"])) or [None])[0],
    }
    used = [pick["full"], pick["exploded"], pick["detail"]]
    rest = [f for f in figures if all(f is not u for u in used)]
    other = [f for f in rest if pick["exploded"] and f["project"] != pick["exploded"]["project"]]
    pick["filing"] = (other or rest or [pick["full"]])[0]   # 递交版换一张不同的图
    return {"structure": struct, "pick": pick, "names": names[:8],
            "counts": {"examples": len(examples), "figures": len(figures), "flows": len(flows)}}


class SimpleWS:
    """list_flowcharts 只需要 root/plan 两个属性。"""

    def __init__(self, studio_root: Path) -> None:
        self.root = studio_root
        self.plan = studio_root / "plan.json"


class Dispatcher:
    """/p/<id>/… 转给该工程的 Plan Studio 子应用，其余交给工作台主应用。"""

    def __init__(self, main, reg: Registry) -> None:
        self.main, self.reg = main, reg

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            m = re.match(r"^/p/([a-z0-9][a-z0-9-]{1,47})(/.*)?$", path)
            if m:
                pid = m.group(1)
                if m.group(2) is None:
                    qs = scope.get("query_string", b"").decode()
                    resp = RedirectResponse("/p/%s/%s" % (pid, ("?" + qs) if qs else ""))
                    return await resp(scope, receive, send)
                sub = self.reg.app_for(pid)
                if sub is None:
                    proj = self.reg.get(pid)
                    msg = ("工程还在准备：%s" % proj.meta.get("message", "")) if proj else "没有这个工程"
                    resp = Response(msg, status_code=409 if proj else 404,
                                    media_type="text/plain; charset=utf-8")
                    return await resp(scope, receive, send)
                scope = dict(scope)
                scope["root_path"] = scope.get("root_path", "") + "/p/" + pid
                return await sub(scope, receive, send)
        return await self.main(scope, receive, send)


# ----------------------------------------------------------------------------- seeding


def load_glossary(path: Path) -> Dict[str, str]:
    """names.py（MAP = {'零件名': (标记, '中文名')}）或 JSON（{零件名: 中文名}）。"""
    if path.suffix == ".py":
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) == "MAP" for t in node.targets):
                raw = ast.literal_eval(node.value)
                return {str(k): str(v[1]) for k, v in raw.items()
                        if isinstance(v, (tuple, list)) and len(v) >= 2}
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in (data.get("terms") or data).items()}


SYNTHETIC_PLAN = {
    "schema": "patent-figure-plan/1",
    "source": {"step": "", "include": ["SYN-*"], "exclude": []},
    "terms": [
        {"selector": "SYN-A01", "term": "底座"}, {"selector": "SYN-B02", "term": "回转座"},
        {"selector": "SYN-C03", "term": "支撑轴"}, {"selector": "SYN-D04", "term": "密封圈"},
        {"selector": "SYN-E05", "term": "调整垫片", "label": "once"},
        {"selector": "SYN-F06", "term": "球头", "label": "all"},
        {"selector": "SYN-G07", "term": "上盖"},
        {"selector": "SYN-H08*", "term": "紧固螺钉", "label": "none"}],
    "figures": [
        {"id": "fig1", "caption": "整体结构示意图", "kind": "assembly", "members": ["*"]},
        {"id": "fig2", "caption": "回转组件分解示意图", "kind": "exploded",
         "members": ["SYN-A01", "SYN-B02", "SYN-C03", "SYN-G07"],
         "layout": {"explode_axis": "z"}}],
    "layout": {"view": "iso", "explode_axis": "auto", "axis_angle": "auto", "density": "normal",
               "max_labels_per_figure": 20, "engineering_table": False},
}
SYNTHETIC_FLOW = {
    "schema": "patent-flowchart/1", "title": "回转组件装配方法的流程图",
    "nodes": [{"id": "s", "kind": "start", "text": "开始"},
              {"id": "a", "kind": "process", "text": "将密封圈装入底座的环槽内"},
              {"id": "b", "kind": "process", "text": "把支撑轴穿过回转座并压入底座"},
              {"id": "c", "kind": "decision", "text": "回转座转动是否顺畅？"},
              {"id": "d", "kind": "process", "text": "增减调整垫片后重新装配"},
              {"id": "e", "kind": "process", "text": "装上上盖并拧紧紧固螺钉"},
              {"id": "z", "kind": "end", "text": "结束"}],
    "edges": [{"from": "s", "to": "a"}, {"from": "a", "to": "b"}, {"from": "b", "to": "c"},
              {"from": "c", "to": "e", "label": "是"}, {"from": "c", "to": "d", "label": "否"},
              {"from": "d", "to": "b"}, {"from": "e", "to": "z"}],
}


def ensure_synthetic(reg: Registry) -> None:
    if reg.get("example-synthetic") or not SYNTHETIC_STEP.is_file():
        return
    reg.create("示例·合成装配体（公开演示）", step_path=SYNTHETIC_STEP, filename="synthetic.stp",
               pid="example-synthetic", example=True, render=True, order=90,
               description="仓库自带的合成装配体：8 种零件、两张图、一张装配流程图，"
                           "用来演示整条链路，可放心公开。",
               plan=SYNTHETIC_PLAN, flowcharts={"flow1": SYNTHETIC_FLOW})


def _work_in(path: Path) -> None:
    """把进程工作目录切到数据目录。

    从 iCloud 等受保护目录启动时，进程的 cwd 不可读：httpx 导入 rich 时的 os.getcwd()、
    shutil.make_archive 都会直接抛 PermissionError（实测 DeepSeek 通道因此 500）。
    数据目录一定可读写，切过去后与从哪里启动无关。"""
    try:
        os.chdir(str(path))
    except OSError:
        pass


def cmd_seed(args) -> int:
    for key in ("step", "plan", "glossary"):
        if getattr(args, key):
            setattr(args, key, os.path.abspath(os.path.expanduser(getattr(args, key))))
    args.flow = [os.path.abspath(os.path.expanduser(f)) for f in (args.flow or [])]
    token = ensure_token()
    reg = Registry(Path(args.data).expanduser(), token)
    _work_in(reg.data)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8")) if args.plan else None
    glossary = load_glossary(Path(args.glossary)) if args.glossary else None
    flows = {}
    for i, f in enumerate(args.flow or [], start=1):
        flows["flow%d" % i] = json.loads(Path(f).read_text(encoding="utf-8"))
    if args.id and reg.get(args.id):
        if not args.replace:
            print("工程 %s 已存在（加 --replace 重建）" % args.id, file=sys.stderr)
            return 1
        reg.delete(args.id)
    proj = reg.create(args.title, step_path=Path(args.step).expanduser(),
                      filename=Path(args.step).name, pid=args.id, example=args.example,
                      description=args.description or "", plan=plan, glossary=glossary,
                      flowcharts=flows, render=args.render, link=args.link, order=args.order)
    print("已创建工程 %s，后台准备中…" % proj.id)
    while proj.meta.get("status") == "preparing" or proj.meta.get("message"):
        time.sleep(3)
        m = proj.meta
        if m.get("status") == "error":
            break
    print("状态：%s %s" % (proj.meta.get("status"), proj.meta.get("message", "")))
    return 0 if proj.meta.get("status") == "ready" else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="数据目录（默认 ~/.patent-workbench）")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("serve", help="启动工作台（默认）")
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--no-browser", action="store_true")
    run.add_argument("--no-example", action="store_true", help="不自动创建合成示例工程")
    seed = sub.add_parser("seed", help="从 STEP 建一个工程（可标为示例并立即出图）")
    seed.add_argument("--step", required=True)
    seed.add_argument("--title", required=True)
    seed.add_argument("--id")
    seed.add_argument("--plan", help="现成的 figure-plan.json")
    seed.add_argument("--glossary", help="术语库：names.py（MAP）或 JSON")
    seed.add_argument("--flow", action="append", help="流程图语义 JSON，可重复")
    seed.add_argument("--description")
    seed.add_argument("--example", action="store_true", help="标为示例（首页轮播）")
    seed.add_argument("--render", action="store_true", help="准备好后立即出图")
    seed.add_argument("--link", action="store_true", help="引用原 STEP 路径而不复制")
    seed.add_argument("--replace", action="store_true")
    seed.add_argument("--order", type=int, default=50, help="示例在首页的排序（小的在前）")
    args = ap.parse_args(argv)
    if args.cmd == "seed":
        return cmd_seed(args)
    port = getattr(args, "port", DEFAULT_PORT)
    token = ensure_token()
    reg = Registry(Path(args.data).expanduser(), token)
    _work_in(reg.data)
    if not getattr(args, "no_example", False):
        ensure_synthetic(reg)
    app = Dispatcher(build_workbench(reg, token, port), reg)
    url = "http://127.0.0.1:%d/?token=%s" % (port, token)
    print("\n专利附图工作台已启动：\n  %s\n数据目录：%s\nREST 文档：http://127.0.0.1:%d/docs\n"
          "MCP 端点：http://127.0.0.1:%d/mcp（Bearer token）\n" % (url, reg.data, port, port),
          flush=True)
    if not getattr(args, "no_browser", False):
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
