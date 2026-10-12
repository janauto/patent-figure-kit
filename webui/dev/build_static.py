#!/usr/bin/env python3
"""生成可独立部署的只读静态站 static-site/（WP4 会复用本脚本）。

正式工作台首页用绝对 /static/* 资源（workbench.py 的挂载）；但部署到普通静态目录或
GitHub Pages 子路径时必须是相对路径，且无 ?static 也要进静态模式。本脚本把首页/规划台
的资源重写为相对路径、注入 <meta name="inkline-mode" content="static">，并带上 manifest
与三个示例的预计算资产（GLB / SVG / QA）。

manifest（WP3b #12：与 live 字段漂移归零）：不再手写一份「前端内部形状」，而是**后端响应的快照**——
每个工程存的就是真实工作台对应接口的响应体：
    capabilities          = GET /v1/capabilities
    projects[].summary    = GET /v1/projects 的条目
    projects[].project    = GET /v1/projects/{id}（摘要 + plan_etag + 候选状态）
    projects[].plan       = GET /v1/projects/{id}/plan（plan_etag = 它的 ETag）
    projects[].state      = GET /p/{id}/api/state（已发布 render / 版本 / 本次作业 / 装配体 / 结构）
    projects[].candidates = GET /v1/projects/{id}/plan/candidates（suggest 作业）
静态模式与 live 走同一组归一化函数（webui/js/api.js 的 composeDetail / normSuggestJob / normJob）。
快照取自 mock_server（它按 docs/openapi.yaml 对齐，见 e2e_live.py 对真实后端的验证），只把资源 URL 改成
静态站内的相对路径。生成后用 docs/openapi.yaml 做字段核对（必需字段齐全、不出现契约外的键），不过即退出 1。

    python3 webui/dev/build_static.py            # 重新生成 manifest.json + static-site/
    python3 webui/dev/build_static.py --check    # 只核对现有 manifest.json 与 openapi 的字段漂移
    # 在不做任何路由重写的静态服务器的子路径下验证：
    cd webui/dev/static-site && python3 -m http.server 8808
    # 打开 http://127.0.0.1:8808/pfk/home.html
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # webui/dev
WEBUI = HERE.parent                             # webui
REPO = WEBUI.parent
OUT = HERE / "static-site" / "pfk"              # 故意放进子目录 pfk/，验证子路径部署
MANIFEST = HERE / "manifest.json"

META = '<meta name="inkline-mode" content="static">'


def inject_meta(html: str) -> str:
    if 'inkline-mode' in html:
        return html
    return html.replace('<meta name="viewport"', META + '\n<meta name="viewport"', 1)


# --------------------------------------------------------------------------- manifest = 后端响应快照
def _static_url(u, pid):
    """后端给的资源地址 → 静态站内的相对路径（fixtures 里的真实资产）。"""
    if not isinstance(u, str):
        return u
    m = re.fullmatch(r"/v1/projects/([\w\-]+)/preview/([\w\-.]+)", u.split("?")[0])
    if m:
        sub = m.group(1) if (HERE / "fixtures" / m.group(1) / m.group(2)).is_file() else "gearbox"
        return "dev/fixtures/%s/%s" % (sub, m.group(2))
    m = re.fullmatch(r"/api/preview/([\w\-.]+)", u.split("?")[0])
    if m:
        sub = pid if (HERE / "fixtures" / pid / m.group(1)).is_file() else "gearbox"
        return "dev/fixtures/%s/%s" % (sub, m.group(1))
    return u


def _rewrite(obj, pid):
    if isinstance(obj, dict):
        return {k: (_static_url(v, pid) if k in ("svg", "thumb", "annotated", "filing", "raw") else _rewrite(v, pid))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite(v, pid) for v in obj]
    return obj


def make_manifest() -> dict:
    sys.path.insert(0, str(HERE))
    import mock_server as M                    # 纯标准库；按 openapi 对齐的契约形状
    M.seed()
    caps = M.capabilities()
    caps["ai"] = dict(caps["ai"], configured=False, provider="none",
                      provider_label="静态演示站不连接 AI 通道")   # 演示站不外发任何内容
    projects = []
    for pid in ("gearbox", "bench-vise", "ball-valve"):
        p = M.PROJECTS[pid]
        state = M.sub_state(pid)
        state.update(step=p["step"], workdir="", home_url="./home.html")
        projects.append({
            "id": pid,
            "summary": _rewrite(M.project_summary(p), pid),
            "project": _rewrite(M.project_brief(p), pid),
            "plan": copy.deepcopy(p["data"]["plan"]),
            "plan_etag": M.norm_etag(p["plan_etag"]),
            "state": _rewrite(state, pid),
            "candidates": _rewrite(M.job_dict(M.latest_suggest(pid)), pid),
            "model_url": "dev/fixtures/models/%s" % M.GLB_BY_PROJECT[pid],
        })
    return {"schema": "inkline-static/2", "version": 2, "display_name": caps["display_name"],
            "note": "后端响应快照（GET /v1/capabilities、/v1/projects、/v1/projects/{id}、/plan、/plan/candidates、"
                    "/p/{id}/api/state）；静态站只读，不发任何写请求。",
            "capabilities": caps, "projects": projects}


# --------------------------------------------------------------------------- 与 openapi 的字段核对
def check_drift(man: dict) -> list:
    import yaml
    spec = yaml.safe_load((REPO / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    S = spec["components"]["schemas"]
    problems = []

    def req(name, obj, where):
        for k in S[name].get("required", []):
            if k not in obj:
                problems.append("%s：缺必需字段 %s.%s" % (where, name, k))

    def closed(name, obj, where, props=None):
        props = props or S[name].get("properties", {})
        for k in obj:
            if k not in props:
                problems.append("%s：%s 出现契约外的键 %r" % (where, name, k))

    closed("Capabilities", man["capabilities"], "capabilities")
    closed("Capabilities", man["capabilities"]["export"], "capabilities.export",
           S["Capabilities"]["properties"]["export"]["properties"])
    closed("Capabilities", man["capabilities"]["ai"], "capabilities.ai",
           S["Capabilities"]["properties"]["ai"]["properties"])
    for p in man["projects"]:
        pid = p["id"]
        job = p["candidates"]
        req("Job", job, pid + ".candidates")
        if job.get("kind") != "suggest" or job.get("state") != "succeeded":
            problems.append("%s.candidates：不是成功的 suggest 作业" % pid)
        res = job.get("result") or {}
        closed("SuggestResult", res, pid + ".candidates.result")
        for c in res.get("candidates", []):
            req("Candidate", c, "%s.candidate[%s]" % (pid, c.get("id")))
            closed("Candidate", c, "%s.candidate[%s]" % (pid, c.get("id")))
            for mref in c.get("members", []):
                if set(mref) - {"part"}:
                    problems.append("%s.candidate[%s]：成员引用只能是 {part}（后端产出不用 {group}）" % (pid, c.get("id")))
        st = p["state"]
        rs = {"running": st.get("rendering"), "result": st.get("render"), "version": st.get("version"), "job": st.get("render_job")}
        req("RenderStatus", rs, pid + ".state")
        closed("RenderJobStatus", st.get("render_job") or {}, pid + ".state.render_job")
        for k in ("assembly", "plan", "validate", "render", "version", "render_job", "structure"):
            if k not in st:
                problems.append("%s.state：缺子应用 api/state 字段 %s" % (pid, k))
        if "project_id" in p["summary"]:
            problems.append("%s.summary：/v1/projects 的条目只有 id，没有 project_id" % pid)
        for k in ("plan_etag", "candidates"):
            if k not in p["project"]:
                problems.append("%s.project：缺 GET /v1/projects/{id} 字段 %s" % (pid, k))
    return problems


def build(manifest: dict):
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # 顶层资源
    for name in ("tokens.css", "home.css", "app.css"):
        shutil.copy2(WEBUI / name, OUT / name)
    shutil.copytree(WEBUI / "js", OUT / "js")
    shutil.copytree(WEBUI / "vendor", OUT / "vendor")   # 规划台 3D 需要 three.js

    # home.html：/static/* → 相对；注入静态 meta
    home = (WEBUI / "home.html").read_text(encoding="utf-8")
    home = home.replace('href="/static/', 'href="').replace('src="/static/', 'src="')
    home = inject_meta(home)
    (OUT / "home.html").write_text(home, encoding="utf-8")

    # index.html：已是相对路径；注入静态 meta
    idx = (WEBUI / "index.html").read_text(encoding="utf-8")
    idx = inject_meta(idx)
    (OUT / "index.html").write_text(idx, encoding="utf-8")

    # manifest + 三个示例的预计算资产（manifest 里用的是 dev/fixtures/... 相对路径）
    (OUT / "dev").mkdir()
    (OUT / "dev" / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.copytree(HERE / "fixtures", OUT / "dev" / "fixtures")

    total = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"静态站已生成：{OUT}")
    print(f"  文件数 {sum(1 for _ in OUT.rglob('*') if _.is_file())}，合计 {total/1048576:.1f} MB")
    print(f"  验证（无路由重写，子路径 /pfk/）：")
    print(f"    cd {OUT.parent}")
    print(f"    python3 -m http.server 8808")
    print(f"    open http://127.0.0.1:8808/pfk/home.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只核对现有 manifest.json 与 openapi 的字段漂移")
    args = ap.parse_args()
    if args.check:
        man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    else:
        man = make_manifest()
        MANIFEST.write_text(json.dumps(man, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    problems = check_drift(man)
    if problems:
        print("manifest 与 openapi 字段漂移：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        sys.exit(1)
    print("manifest 与 openapi 字段核对：0 项漂移（Capabilities / Job / SuggestResult / Candidate / RenderStatus）")
    if not args.check:
        build(man)


if __name__ == "__main__":
    main()
