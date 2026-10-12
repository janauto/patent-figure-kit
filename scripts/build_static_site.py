#!/usr/bin/env python3
"""T0 在线演示站生成器（WP4）：三个公开示例**真实出图** → 版本化 manifest → 只读静态站 → 资产审计。

    python3 scripts/build_static_site.py                       # 输出 static-site/patent-figure-kit/
    python3 scripts/build_static_site.py -o 目录 --site-name 子路径名
    python3 scripts/build_static_site.py --audit-only static-site   # 只审计已生成的站

流程（每一步都走真实后端，与浏览器 / Agent 同一套服务）：

1. 隔离环境：临时数据目录、临时配置目录、随机 token；**不配置任何 AI 通道**（构建过程零外发）。
2. 进程内起工作台（``workbench.build_workbench``，TestClient 驱动 REST）：对 gearbox / vise / valve 依次
   ``POST /v1/projects/from-example`` → ``POST /plan/suggest``（候选分图，逐个试渲染 + QA）→
   ``POST /render``（发布服务：隔离目录渲染、全过闸才发布）→ ``POST /export``（两版 PDF/Word/DXF，
   逐页核验 + 封印）→ 下载 zip。
3. 取已发布版本的 GLB、预览 SVG（原始 / 交底 / 递交三版）、件号表、QA、候选及其预览，改写成相对 URL，
   写版本化 manifest（``inkline-static-manifest/1``；版本 = 包版本 + git 提交 + 生成时间）。
4. 复用 ``webui/dev/build_static.py``（只读调用：把它的输入目录指到本次生成的 manifest / fixtures）拷贝前端、
   把 /static/* 改成相对路径、注入静态模式 meta；再做两处构建期注入（不改 webui 源码）：
   页面底部的「预计算展示；无需上传自有模型 · 下载导出包样例」条，以及无 ``?project=`` 打开规划台时回到首页。
5. 资产审计（失败即退出码 1）：逐个文件给出来源与判定，写到站点目录**之外**的 ``audit.json``——
   禁止 iDock / 千问 / 本机路径 / 用户名 / token / 密钥形状的字符串；GLB 节点名只能是三个公开模型的件名；
   导出包逐成员扫描；manifest 引用的资产必须存在且哈希一致；不得有指向后端的绝对 URL。

依赖：cadquery-ocp + Web extras（fastapi/uvicorn/httpx…，requirements-web.txt）；只能在仓库检出里运行
（需要 webui/dev/build_static.py）。不 import OCP（OCP 只在 worker / 生成器子进程里）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

SITE_NAME = "patent-figure-kit"          # GitHub Pages 项目站的子路径名
MARKER = ".pfk-static-site"              # 输出根目录的归属标记（只清理本工具建的目录）
EXAMPLES = ("gearbox", "vise", "valve")
#: 三个公开演示模型的件名前缀（make_demo_assemblies.py）；GLB 节点名只能落在这里面。
PUBLIC_PART_RE = re.compile(r"^(DEMO-|PGB-|VISE-|VALVE-)[A-Z0-9-]*(#\d+)?$")
BANNER_TEXT = "预计算展示；无需上传自有模型"

# 审计禁用串：iDock 内容、千问办公、本机路径 / 用户名、凭据形状。大小写不敏感。
FORBIDDEN = [
    (re.compile(r"idock", re.I), "iDock 内容"),
    (re.compile(r"千问"), "千问（宣传材料不出现千问办公）"),
    (re.compile(r"qwen", re.I), "Qwen"),
    (re.compile(r"/Users/[A-Za-z0-9._-]+"), "macOS 本机路径"),
    (re.compile(r"/home/[A-Za-z0-9._-]+/"), "Linux 本机路径"),
    (re.compile(r"[A-Za-z]:\\\\?Users\\\\?", re.I), "Windows 本机路径"),
    (re.compile(r"/private/(var|tmp)/"), "临时目录路径"),
    (re.compile(r"\.patent-workbench"), "工作台数据目录"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "API 密钥形状"),
    (re.compile(r"(?i)(api_key|api_token|WORKBENCH_TOKEN)\"?\s*[:=]\s*\"?[A-Za-z0-9_\-]{12,}"),
     "凭据赋值"),
]
TEXT_EXT = {".html", ".css", ".js", ".json", ".svg", ".txt", ".md", ".xml", ".csv"}
ALLOWED_EXT = TEXT_EXT | {".glb", ".zip", ""}


class BuildError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- 小工具


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_rev() -> str:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=12", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        rev = out.stdout.strip()
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return rev + ("-dirty" if dirty else "") if rev else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _pkg_version() -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', (HERE / "pfk_cli.py").read_text(encoding="utf-8"),
                  re.M)
    return m.group(1) if m else "0"


def _log(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------- 真实出图


class Backend:
    """进程内工作台 + TestClient；全部走 REST（与浏览器 / Agent 同一套服务）。"""

    def __init__(self, data: Path) -> None:
        from starlette.testclient import TestClient
        import workbench as W
        self.W = W
        self.token = os.environ["WORKBENCH_TOKEN"]
        self.reg = W.Registry(data, self.token)
        app = W.Dispatcher(W.build_workbench(self.reg, self.token, 8790), self.reg)
        self.c = TestClient(app, base_url="http://127.0.0.1:8790")
        self.h = {"Authorization": "Bearer " + self.token}

    def get(self, path: str, **kw):
        r = self.c.get(path, headers={**self.h, **kw.pop("headers", {})}, **kw)
        if r.status_code >= 400:
            raise BuildError("GET %s → %d %s" % (path, r.status_code, r.text[:300]))
        return r

    def post(self, path: str, body: Optional[dict] = None, headers: Optional[dict] = None):
        r = self.c.post(path, json=body or {}, headers={**self.h, **(headers or {})})
        if r.status_code >= 400:
            raise BuildError("POST %s → %d %s" % (path, r.status_code, r.text[:400]))
        return r

    def fetch_asset(self, pid: str, url: str) -> bytes:
        """后端给的资产地址：``/api/…`` 是工程子应用的相对地址（挂在 /p/<pid>/ 下），其余绝对路径
        （``/files/…``、``/v1/…``、``/p/…``）属主应用。"""
        base, _q, q = str(url).partition("?")
        if base.startswith("/api/"):
            return self.get(_sub(pid, base) + ("?" + q if q else "")).content
        if base.startswith("/"):
            return self.get(url).content
        raise BuildError("意外的资产地址：%r" % url)

    def wait(self, job_id: str, what: str, timeout: float = 1800.0) -> dict:
        t0 = time.time()
        last = ""
        while True:
            job = self.get("/v1/jobs/%s" % job_id).json()
            stage = (job.get("stage") or "")
            if stage != last:
                _log("    · %s：%s" % (what, stage))
                last = stage
            if job.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
                if job["state"] != "succeeded":
                    raise BuildError("%s 作业 %s：%s" % (what, job["state"],
                                                        json.dumps(job.get("error"), ensure_ascii=False)[:600]))
                return job
            if time.time() - t0 > timeout:
                raise BuildError("%s 超时（%ds）" % (what, timeout))
            time.sleep(1.0)


def _sub(pid: str, path: str) -> str:
    return "/p/%s/%s" % (pid, path.lstrip("/"))


def _strip_query(url: str) -> Tuple[str, str]:
    base, _, q = str(url).partition("?")
    return base, q


def run_example(be: Backend, ex: str, fixtures: Path, samples: Path) -> Dict[str, Any]:
    """一个公开示例的完整金路径；返回 manifest 里的工程条目与样例包信息。"""
    t0 = time.time()
    _log("[%s] 载入示例 …" % ex)
    r = be.post("/v1/projects/from-example", {"example": ex}).json()
    pid = r["project_id"]
    be.wait(r["job_id"], "载入")
    _log("[%s] 候选分图 …" % ex)
    sj = be.post("/v1/projects/%s/plan/suggest" % pid, {"label_standard_parts": False}).json()
    be.wait(sj["job_id"], "候选分图")
    cjob = be.get("/v1/projects/%s/plan/candidates" % pid).json()     # 最近一次候选分图作业
    cands = cjob.get("result") or {}
    plan_resp = be.get("/v1/projects/%s/plan" % pid)
    etag = plan_resp.headers.get("ETag", "")
    _log("[%s] 出图 …" % ex)
    rj = be.post("/v1/projects/%s/render" % pid, {"plan_etag": etag}).json()
    render_job = be.wait(rj["job_id"], "出图")
    state = be.get(_sub(pid, "api/state")).json()
    render = state.get("render") or {}
    if not render.get("ok") or not render.get("figures"):
        raise BuildError("%s 出图未全部过闸：%s" % (ex, json.dumps(render, ensure_ascii=False)[:400]))
    _log("[%s] 导出附图包（两版 PDF / Word / DXF，逐页核验）…" % ex)
    xj = be.post("/v1/projects/%s/export" % pid, {
        "variant": "both", "formats": ["pdf", "docx", "dxf"], "dpi": 300,
        "sheet_preset": "standard", "render_job_id": rj["job_id"], "plan_etag": etag}).json()
    export_job = be.wait(xj.get("job_id") or xj.get("export_id"), "导出")
    xres = export_job.get("result") or {}
    if not xres.get("ok"):
        raise BuildError("%s 导出包未通过核验：%s" % (ex, json.dumps(xres.get("reasons"),
                                                                ensure_ascii=False)[:600]))
    zip_bytes = be.get("/v1/exports/%s/download" % xres["export_id"]).content

    # ---- 资产落盘（相对 URL）--------------------------------------------------
    pdir = fixtures / pid
    pdir.mkdir(parents=True, exist_ok=True)
    rel_root = "dev/fixtures/%s" % pid
    assets: List[Dict[str, Any]] = []

    def put(name: str, data: bytes, kind: str) -> str:
        (pdir / name).write_bytes(data)
        assets.append({"path": "%s/%s" % (rel_root, name), "kind": kind, "project": pid,
                       "bytes": len(data), "sha256": _sha256(data)})
        return "%s/%s" % (rel_root, name)

    model_url = put("model.glb", be.get(_sub(pid, "api/model.glb")).content, "glb")
    rid = state.get("version") or ""

    def fetch_preview(url: str, kind: str) -> str:
        name = _strip_query(url)[0].rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_\-]+\.svg", name):
            raise BuildError("意外的预览文件名：%r" % url)
        return put(name, be.fetch_asset(pid, url), kind)

    figures = []
    for f in render.get("figures", []):
        f = json.loads(json.dumps(f))            # 深拷贝
        if f.get("svg"):
            f["svg"] = fetch_preview(f["svg"], "preview-svg")
        variants = {}
        for k, u in (f.get("variants") or {}).items():
            variants[k] = fetch_preview(u, "preview-svg") if u else u
        f["variants"] = variants
        ann = f.get("annotation") or {}
        for key in ("png", "dxf", "svg"):         # 绝对文件地址不进静态站（PNG 只在导出时生成）
            ann.pop(key, None)
        f["annotation"] = ann
        figures.append(_scrub(f))
    render_out = _scrub({k: v for k, v in render.items() if k not in ("figures",)})
    render_out["figures"] = figures
    # 版本证据：这次渲染作业冻结的输入快照（后端给的，不是前端自编）。前端据此判断结果与当前计划一致。
    frozen = render_job.get("inputs") or {}
    render_out["inputs"] = {k: frozen[k] for k in ("plan_etag", "plan_hash", "terms_hash", "step_sha")
                            if k in frozen}

    cand_list = []
    for c in cands.get("candidates", []):
        c = json.loads(json.dumps(c))
        prev = c.get("preview") or {}
        if prev.get("svg"):
            name = "cand-%s.svg" % re.sub(r"[^A-Za-z0-9_\-]", "_", str(c.get("id") or c.get("fig_id")))
            c["preview"] = {"svg": put(name, be.fetch_asset(pid, prev["svg"]), "candidate-svg")}
        cand_list.append(_scrub(c))

    sample_name = "%s-figures.zip" % pid
    (samples / sample_name).write_bytes(zip_bytes)
    sample = {"project": pid, "path": "samples/%s" % sample_name, "bytes": len(zip_bytes),
              "sha256": _sha256(zip_bytes), "variant": "both", "formats": ["pdf", "docx", "dxf"],
              "dpi": 300, "sheet_preset": "standard",
              "items": [{k: i.get(k) for k in ("variant", "format", "status", "reason", "file")}
                        for i in xres.get("items", [])],
              "documents": {name: {"pages": d.get("pages"), "expected_pages": d.get("expected_pages"),
                                   "ok": not d.get("errors")}
                            for name, d in (xres.get("documents") or {}).items()},
              "note": xres.get("note", "")}

    summary = be.get("/v1/projects/%s" % pid).json()
    asm = state.get("assembly") or {}
    src = dict(asm.get("source") or {})
    from patent_figure import examples as EXM
    step_name = EXM.EXAMPLES[ex]["step"]          # 工作区里的副本叫 model.stp；展示用公开模型的原名
    src["step"] = step_name                        # 不留本机路径
    asm = dict(asm, source=src)
    thumb = next((f["variants"].get("annotated") for f in figures if f["variants"].get("annotated")),
                 None)
    plan_etag = frozen.get("plan_etag") or etag
    base = {
        "project_id": pid, "title": summary.get("title"), "description": summary.get("description"),
        "step_name": step_name, "example": True, "status": "ready", "message": "",
        "plan_etag": plan_etag, "llm": STATIC_LLM,
        "figures": len(figures), "figures_pass": sum(1 for f in figures if f.get("pass")),
        "flowcharts": 0, "thumb": thumb, "version": rid,
    }
    detail = dict(base)
    detail.update({
        "assembly": asm, "structure": _scrub(state.get("structure")),
        "candidates": cand_list, "candidates_summary": _scrub(cands.get("summary")),
        "candidates_empty_reason": cands.get("empty_reason"), "candidates_guide": cands.get("guide"),
        "render": render_out,
        "render_job": {"id": render_job.get("id"), "state": render_job.get("state"),
                       "running": False, "ok": True, "error": None},
        "plan": _with_step(plan_resp.json(), step_name), "validate": state.get("validate") or {"ok": True, "issues": []},
        "model_url": model_url, "export_sample": sample["path"],
    })
    detail = _scrub(detail)                      # plan.source.step、render.log 等里的本机路径一并收掉
    _log("[%s] 完成：%d 张图全过闸，候选 %d 个，导出包 %.1f MB，用时 %.0f s"
         % (ex, len(figures), len(cand_list), len(zip_bytes) / 1048576, time.time() - t0))
    return {"entry": {"id": pid, "summary": base, "detail": detail}, "assets": assets,
            "sample": sample, "step_sha256": src.get("sha256"), "step_name": src.get("step")}


def _with_step(plan: dict, step_name: str) -> dict:
    plan = dict(plan)
    plan["source"] = dict(plan.get("source") or {}, step=step_name)
    return plan


STATIC_LLM = {"provider": "none", "provider_label": "AI 未接入（演示站）", "configured": False,
              "dwg_available": False}


#: 字符串里的本机绝对路径（POSIX 或 Windows 盘符），只保留最后一段文件名。前面不能紧跟字母数字，
#: 这样相对资产地址（dev/fixtures/…）不受影响。
_ABS_PATH = re.compile(r"(?<![\w.~-])(?:[A-Za-z]:[\\/]|/)(?:[^\s，。；：、\"'<>()（）/\\]+[/\\])+"
                       r"(?=[^\s，。；：、\"'<>()（）/\\]+)")


def _redact_paths(text: str) -> str:
    return _ABS_PATH.sub("", text)


def _scrub(obj):
    """去掉绝对路径类字段，并把任何字符串里的本机绝对路径收成文件名（静态站只用相对资产）。"""
    drop = {"workdir", "dir", "path", "out_dir", "root", "diag_dir", "data_dir"}
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in drop and isinstance(v, str) and ("/" in v or "\\" in v):
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, str):
        return _redact_paths(obj)
    return obj


# ----------------------------------------------------------------------------- 站点组装


def assemble(site: Path, staging: Path, samples_src: Path) -> None:
    """只读调用 webui/dev/build_static.py：把它的输入目录（manifest + fixtures）指到 staging。"""
    sys.path.insert(0, str(REPO / "webui" / "dev"))
    try:
        import build_static as BS
    finally:
        sys.path.pop(0)
    BS.HERE, BS.OUT = staging, site            # 输入：staging/manifest.json + staging/fixtures；输出：site
    BS.build()
    shutil.copytree(str(samples_src), str(site / "samples"))
    (site / ".nojekyll").write_text("", encoding="utf-8")


_BANNER_STYLE = {
    # 首页：底部居中；规划台：右下角收小，避开 3D 视图底部的工具条
    "home.html": "left:50%;bottom:12px;transform:translateX(-50%);font-size:13px",
    "index.html": "right:12px;bottom:12px;font-size:12px",
}
BANNER_HTML = (
    '<div id="pfk-static-banner" role="note" style="position:fixed;{pos};z-index:9999;'
    'background:#fffdf6;border:1px solid #d8d2c2;border-radius:999px;padding:5px 12px;'
    'line-height:1.4;font-family:system-ui,-apple-system,sans-serif;color:#3a3a3a;'
    'box-shadow:0 2px 8px rgba(0,0,0,.08);max-width:calc(100vw - 32px)">'
    '<strong>%s</strong> · <a href="samples.html" style="color:#0b5c6b">下载导出包样例</a>'
    '</div>') % BANNER_TEXT
INDEX_REDIRECT = ("<script>if(!new URLSearchParams(location.search).get('project'))"
                  "location.replace('./home.html');</script>")


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def strip_flagged_comments(site: Path) -> List[str]:
    """发布前去掉**提到禁用词的注释**（webui 源码里有「不得出现『千问办公』」这类自我提醒注释）：
    HTML 注释、我们自己的 JS 模块里的整行 ``//`` 注释。只删注释、不碰代码；返回改动过的文件。"""
    changed = []
    for p in sorted(site.rglob("*")):
        if not p.is_file() or "vendor" in p.relative_to(site).parts:
            continue
        if p.suffix == ".html":
            s = p.read_text(encoding="utf-8")
            t = _HTML_COMMENT.sub(lambda m: "" if _scan_text(m.group(0)) else m.group(0), s)
        elif p.suffix == ".js":
            s = p.read_text(encoding="utf-8")
            lines = s.split("\n")
            t = "\n".join(l for l in lines if not (l.lstrip().startswith("//") and _scan_text(l)))
        else:
            continue
        if t != s:
            p.write_text(t, encoding="utf-8")
            changed.append(p.relative_to(site).as_posix())
    return changed


def inject(site: Path) -> None:
    for name in ("home.html", "index.html"):
        p = site / name
        s = p.read_text(encoding="utf-8")
        if "pfk-static-banner" not in s:
            s = s.replace("</body>", BANNER_HTML.replace("{pos}", _BANNER_STYLE[name]) + "\n</body>", 1)
        if name == "index.html" and "location.replace('./home.html')" not in s:
            s = s.replace("<head>", "<head>\n" + INDEX_REDIRECT, 1)
        p.write_text(s, encoding="utf-8")


def write_samples_page(site: Path, manifest: dict) -> None:
    rows = []
    titles = {p["id"]: p["summary"]["title"] for p in manifest["projects"]}
    for s in manifest["samples"]:
        docs = "、".join("%s（%s 页%s）" % (html.escape(n), d.get("pages"), "，核验通过" if d.get("ok")
                                                                        else "，未通过")
                        for n, d in sorted(s["documents"].items()))
        rows.append(
            "<tr><td>%s</td><td><a href=\"%s\" download>%s</a></td><td>%.1f MB</td>"
            "<td class=\"mono\">%s…</td><td>%s</td></tr>"
            % (html.escape(titles.get(s["project"], s["project"])), html.escape(s["path"]),
               html.escape(s["path"].rsplit("/", 1)[-1]), s["bytes"] / 1048576,
               s["sha256"][:16], docs))
    note = html.escape(manifest["samples"][0].get("note", "")) if manifest["samples"] else ""
    page = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>导出包样例 · {name}</title>
<style>
:root{{--fg:#222;--bg:#fbfaf5;--line:#ddd6c4;--accent:#0b5c6b}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,-apple-system,"PingFang SC","Noto Sans CJK SC",sans-serif}}
main{{max-width:960px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} .lead{{color:#555;margin:0 0 18px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line)}}
th,td{{text-align:left;padding:8px 10px;border-top:1px solid var(--line);vertical-align:top;font-size:14px}}
th{{background:#f3f0e6;border-top:0}} a{{color:var(--accent)}} .mono{{font-family:ui-monospace,Menlo,monospace;font-size:12px}}
.wrap{{overflow-x:auto}} small{{color:#666}}
</style></head><body><main>
<h1>导出包样例</h1>
<p class="lead"><strong>{banner}</strong>。下面三个附图包由 CI 从三个公开演示模型（减速器 / 台虎钳 / 球阀）真实出图、
逐页核验后打包；本页只提供下载，不在浏览器里出图。<a href="home.html">← 回到演示首页</a></p>
<div class="wrap"><table>
<tr><th>模型</th><th>附图包</th><th>大小</th><th>SHA-256</th><th>文档逐页核验</th></tr>
{rows}
</table></div>
<p><small>{note}</small></p>
<p><small>生成：{version}（{generated}）。包内含递交版 / 交底版的 PDF 与 Word、DXF 源文件、附图说明.txt、核对清单.txt。</small></p>
</main></body></html>
""".format(name=html.escape(manifest.get("display_name", "")), banner=BANNER_TEXT,
           rows="\n".join(rows), note=note, version=html.escape(manifest["version"]),
           generated=html.escape(manifest["generated_at"]))
    (site / "samples.html").write_text(page, encoding="utf-8")


# ----------------------------------------------------------------------------- 审计


def _scan_text(text: str) -> List[str]:
    return [why for rx, why in FORBIDDEN if rx.search(text)]


def _glb_names(data: bytes) -> List[str]:
    import struct
    if len(data) < 20 or data[:4] != b"glTF":
        raise ValueError("不是 GLB")
    jlen, jtype = struct.unpack("<II", data[12:20])
    if jtype != 0x4E4F534A:
        raise ValueError("GLB 首块不是 JSON")
    doc = json.loads(data[20:20 + jlen].decode("utf-8"))
    names = []
    for key in ("nodes", "meshes", "materials", "scenes"):
        names += [str(n.get("name")) for n in doc.get(key, []) if n.get("name")]
    return names


def audit_site(site: Path, extra_forbidden: List[str] = (),
               transformed: List[str] = ()) -> Dict[str, Any]:
    """逐文件判定；返回 {ok, files[], problems[]}。"""
    from patent_figure import zipcheck
    problems: List[str] = []
    files: List[Dict[str, Any]] = []
    extra = [x for x in extra_forbidden if x]
    manifest_path = site / "dev" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    declared = {a["path"]: a for a in manifest.get("assets", [])}
    declared.update({s["path"]: dict(s, kind="export-sample") for s in manifest.get("samples", [])})
    webui = REPO / "webui"
    for p in sorted(x for x in site.rglob("*") if x.is_file()):
        rel = p.relative_to(site).as_posix()
        data = p.read_bytes()
        row = {"path": rel, "bytes": len(data), "sha256": _sha256(data), "source": "", "verdict": "ok",
               "why": []}
        ext = p.suffix.lower()
        if ext not in ALLOWED_EXT:
            row["why"].append("不在允许的文件类型内（%s）" % ext)
        # 来源归类
        if rel in declared:
            row["source"] = "公开示例真实出图（%s）" % declared[rel].get("project", "")
            if declared[rel]["sha256"] != row["sha256"]:
                row["why"].append("与 manifest 登记的哈希不一致")
        elif rel in ("home.html", "index.html"):
            row["source"] = "webui/%s（构建期改写：相对路径 + 静态模式 meta + 演示站提示条）" % rel
        elif rel == "dev/manifest.json":
            row["source"] = "本次生成的版本化 manifest"
        elif rel in ("samples.html", ".nojekyll"):
            row["source"] = "本工具生成"
        elif (webui / rel).is_file() and _file_sha(webui / rel) == row["sha256"]:
            row["source"] = "webui/%s（原样）" % rel
        elif (webui / rel).is_file() and rel in transformed:
            row["source"] = "webui/%s（构建期删去提到禁用词的注释行）" % rel
        else:
            row["why"].append("来源不明（既不是 webui 原样文件，也没有在 manifest 登记）")
        # 内容扫描
        if ext in TEXT_EXT or rel == ".nojekyll":
            text = data.decode("utf-8", errors="replace")
            row["why"] += _scan_text(text)
            if ext == ".json" and re.search(r'"(?:https?:)?//127\.0\.0\.1|"/(?:v1|api|p|files|static)/',
                                            text):
                row["why"].append("manifest / 数据里含指向后端的绝对 URL")
            if ext == ".html" and re.search(r'(?:src|href)="(?:https?://127\.0\.0\.1|/(?:v1|api|p|files|static)/)',
                                            text):
                row["why"].append("页面资源引用了后端绝对地址")
        elif ext == ".glb":
            try:
                bad = [n for n in _glb_names(data) if not PUBLIC_PART_RE.match(n)]
                if bad:
                    row["why"].append("GLB 含非公开模型的节点名：%s" % "、".join(bad[:5]))
            except ValueError as exc:
                row["why"].append("GLB 解析失败：%s" % exc)
            row["why"] += _scan_text(data.decode("latin-1"))
        elif ext == ".zip":
            try:
                mem = zipcheck.members(p)
                row["members"] = [m for m, _ in mem]
                for name, content in zipcheck.iter_contents(p):
                    hits = _scan_text(name) + _scan_text(content.decode("utf-8", errors="replace")) \
                        + _scan_text(content.decode("latin-1"))
                    if extra and any(x in content.decode("latin-1") for x in extra):
                        hits.append("含构建环境敏感串")
                    if hits:
                        row["why"].append("包内 %s：%s" % (name, "、".join(sorted(set(hits)))))
                if not any(m.lower().endswith(".pdf") for m, _ in mem):
                    row["why"].append("导出包里没有 PDF")
            except Exception as exc:  # noqa: BLE001
                row["why"].append("zip 无法读取：%s" % exc)
        if extra and any(x in data.decode("utf-8", errors="replace") or x in data.decode("latin-1")
                         for x in extra):
            row["why"].append("含构建环境敏感串（token / 临时目录 / 用户名 / 主机名）")
        if row["why"]:
            row["verdict"] = "fail"
            row["why"] = sorted(set(row["why"]))
            problems.append("%s：%s" % (rel, "；".join(row["why"])))
        files.append(row)
    for path in declared:
        if not (site / path).is_file():
            problems.append("manifest 登记的资产缺失：%s" % path)
    projects = [p.get("id") for p in manifest.get("projects", [])]
    if sorted(projects) != sorted("example-" + e for e in EXAMPLES):
        problems.append("manifest 工程集合不是三个公开示例：%s" % projects)
    return {"ok": not problems, "site": str(site.name), "files": files, "problems": problems,
            "counts": {"files": len(files), "bytes": sum(f["bytes"] for f in files)}}


# ----------------------------------------------------------------------------- 主流程


def _isolate_env(cfg_dir: Path) -> str:
    """构建期零外发：清掉一切 AI 通道配置，配置 / token 都放临时目录。必须在 import 工作台之前调用。"""
    for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        os.environ.pop(k, None)
    os.environ["PATENT_WORKBENCH_CONFIG"] = str(cfg_dir)
    token = secrets.token_urlsafe(24)
    os.environ["WORKBENCH_TOKEN"] = token
    return token


def build(out_root: Path, site_name: str, keep_data: bool) -> int:
    from patent_figure import pathguard
    why = pathguard.check_outputs([("-o", out_root)])
    if why:
        raise BuildError(why)
    why = pathguard.claim_workdir(out_root, MARKER)
    if why:
        raise BuildError(why)
    site = out_root / site_name
    work = Path(tempfile.mkdtemp(prefix="pfk-static-"))
    token = _isolate_env(work / "config")
    import workbench_llm as LLM
    LLM._codebuddy_path = lambda: None          # 构建期不调用任何本机 AI 通道
    staging = work / "staging"
    fixtures = staging / "fixtures"
    samples = work / "samples"
    fixtures.mkdir(parents=True)
    samples.mkdir(parents=True)
    t0 = time.time()
    try:
        be = Backend(work / "data")
        results = [run_example(be, ex, fixtures, samples) for ex in EXAMPLES]
        now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
        manifest = {
            "schema": "inkline-static-manifest/1",
            "version": "%s+%s.%s" % (_pkg_version(), _git_rev(), now.strftime("%Y%m%d%H%M%S")),
            "generated_at": now.isoformat(),
            "generator": "scripts/build_static_site.py",
            "display_name": _display_name(),
            "data_dir": "（静态演示站 · 预计算展示）",
            "llm": STATIC_LLM,
            "note": "%s。三个公开演示模型由 CI 真实出图、过闸后发布；本站只读，不上传、不出图、不外发。"
                    % BANNER_TEXT,
            "source_models": [{"project": r["entry"]["id"], "step": r["step_name"],
                               "sha256": r["step_sha256"],
                               "generator": "scripts/make_demo_assemblies.py"} for r in results],
            "projects": [r["entry"] for r in results],
            "samples": [r["sample"] for r in results],
            "assets": [a for r in results for a in r["assets"]],
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
        if site.exists():
            shutil.rmtree(str(site))
        assemble(site, staging, samples)
        transformed = strip_flagged_comments(site)
        inject(site)
        write_samples_page(site, manifest)
    finally:
        if keep_data:
            _log("保留构建数据：%s" % work)
        else:
            shutil.rmtree(str(work), ignore_errors=True)
    report = audit_site(site, extra_forbidden=[token, str(work)] + _local_identity(),
                        transformed=transformed)
    (out_root / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    _print_audit(report, out_root / "audit.json")
    total = report["counts"]
    _log("\n静态站：%s（%d 个文件，%.1f MB，用时 %.0f s）" % (site, total["files"],
                                                           total["bytes"] / 1048576, time.time() - t0))
    _log("子路径验证（不做路由重写）：python3 scripts/verify_static_site.py %s" % out_root)
    return 0 if report["ok"] else 1


def _local_identity() -> List[str]:
    """构建机的用户名 / 主机名也不得出现在发布资产里（CI 的通用名如 runner 不计，免得误报）。"""
    import getpass
    import socket
    out = []
    for v in (getpass.getuser(), socket.gethostname().split(".")[0]):
        if v and len(v) >= 5 and v.lower() not in {"runner", "ubuntu", "admin", "localhost"}:
            out.append(v)
    return out


def _display_name() -> str:
    try:
        from patent_figure import capabilities as CAPS
        return CAPS.DISPLAY_NAME
    except Exception:  # noqa: BLE001
        return ""


def _print_audit(report: dict, where: Path) -> None:
    by_src: Dict[str, int] = {}
    for f in report["files"]:
        key = f["source"].split("（", 1)[0] if f["source"] else "来源不明"
        by_src[key] = by_src.get(key, 0) + 1
    _log("\n资产审计：%s（%d 个文件）" % ("通过" if report["ok"] else "未通过", len(report["files"])))
    for k, v in sorted(by_src.items(), key=lambda kv: -kv[1]):
        _log("  %4d  %s" % (v, k))
    for p in report["problems"]:
        _log("  ✗ %s" % p)
    _log("  逐文件明细：%s" % where)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", type=Path, default=REPO / "static-site",
                    help="输出根目录（默认 static-site/；站点在其下的 <site-name>/）")
    ap.add_argument("--site-name", default=SITE_NAME, help="子路径名（默认 patent-figure-kit）")
    ap.add_argument("--keep-data", action="store_true", help="保留临时工作台数据目录以便排查")
    ap.add_argument("--audit-only", action="store_true", help="不重新生成，只审计 -o 下已有的站点")
    args = ap.parse_args(argv)
    if not (REPO / "webui" / "dev" / "build_static.py").is_file():
        print("需要在仓库检出里运行（缺 webui/dev/build_static.py）。", file=sys.stderr)
        return 2
    try:
        if args.audit_only:
            site = args.out / args.site_name
            if not site.is_dir():
                print("没有站点目录：%s" % site, file=sys.stderr)
                return 2
            report = audit_site(site)
            _print_audit(report, Path("（仅打印，未写文件）"))
            return 0 if report["ok"] else 1
        return build(args.out, args.site_name, args.keep_data)
    except BuildError as exc:
        print("生成失败：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
