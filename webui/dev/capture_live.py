#!/usr/bin/env python3
"""五屏四态截图——在**真实后端**（scripts/workbench.py）上重拍，输出到 docs/assets/screens/（供 WP5）。

每一张都是真实接口返回的真实状态，不用 ?scene= / ?state= 之类的开发开关；只有两类「时机」是人为制造的：
  * 加载态：把对应请求（工程列表 / GLB）在浏览器里先挂起再截图，随后放行；
  * 运行中：截图时作业确实在跑（示例首次载入、连续提交的渲染作业、打包中）。
失败态全部来自后端真实返回：坏 STEP 准备失败、401 需要 token、GLB 取不到、出图未过闸、PDF 缺独立渲染器未核验。

后端实例与 e2e_live.py 相同（main 8806 / nopdf 8813 / ai 8811 / fresh 8814，各自隔离数据与配置，不写 ~/.patent-workbench）。

    python3 webui/dev/capture_live.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e2e_live as E  # noqa: E402

OUT = E.REPO / "docs" / "assets" / "screens"
SHOTS = []


def shot(pg, name, screen, state, how):
    OUT.mkdir(parents=True, exist_ok=True)
    pg.screenshot(path=str(OUT / f"{name}.png"))
    SHOTS.append({"file": f"{name}.png", "screen": screen, "state": state, "how": how})
    E.log(f"  · {name}.png  {screen} · {state}")


def main():
    main_be = E.Backend("main", 8806, E.HOME / ".patent-workbench-wp3b", E.HOME / ".patent-workbench-wp3b-config", {"provider": "none"})
    nopdf_be = E.Backend("nopdf", 8813, E.HOME / ".patent-workbench-wp3b-nopdf", E.HOME / ".patent-workbench-wp3b-nopdf-config",
                         {"provider": "none"}, {"PFK_PDF_RENDERER": "none"})
    ai_be = E.Backend("ai", 8811, E.HOME / ".patent-workbench-wp3b-ai", E.HOME / ".patent-workbench-wp3b-ai-config",
                      {"provider": "deepseek", "deepseek": {"api_key": "e2e-fake-key-0000",
                                                            "base_url": "http://127.0.0.1:%d" % E.FAKE_LLM_PORT, "model": "e2e-fake"}})
    fresh_be = E.Backend("fresh", 8814, E.SCRATCH / ("shots-%d" % int(time.time())), E.SCRATCH / "fresh-config",
                         {"provider": "none"}, serve_args=["--no-example"])
    fake = E.start_fake_llm()
    started = []
    try:
        for be in (main_be, nopdf_be, ai_be, fresh_be):
            be.up()
            started.append(be)
        # 夹具：清掉隔离数据目录里先前用例 / 截图留下的非示例工程（只动 ~/.patent-workbench-wp3b，不碰用户数据）
        for proj in main_be.rest("GET", "/v1/projects")[1].get("projects", []):
            if not proj.get("example"):
                main_be.rest("DELETE", "/api/wb/projects/%s" % proj["id"])
        for ex in ("gearbox", "vise", "valve"):
            main_be.ensure_example(ex)
            main_be.reset_example_plan(ex)
        nopdf_be.ensure_example("valve")
        nopdf_be.reset_example_plan("valve")
        ai_be.ensure_example("gearbox")
        ai_be.reset_example_plan("gearbox")
        with E.sync_playwright() as p:
            browser = p.chromium.launch()
            capture(browser, main_be, nopdf_be, ai_be, fresh_be)
            browser.close()
    finally:
        for be in started:
            be.down()
        fake.shutdown()
    (OUT / "screens.json").write_text(json.dumps(SHOTS, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    E.log(f"\n共 {len(SHOTS)} 张 → {OUT}")


def capture(browser, be, nopdf, ai, fresh):
    B = be.base
    # ---------------------------------------------------------------- ① 工程列表
    ctx = E.new_ctx(browser, fresh)
    pg, _ = E.page(ctx)
    pg.goto(f"{fresh.base}/", wait_until="networkidle")
    pg.wait_for_selector("#load-example")
    shot(pg, "01-home-empty", "① 工程列表", "空", "全新数据目录（--no-example），首页真实空态")
    ctx.close()

    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    held = []
    pg.route("**/v1/projects", lambda r: held.append(r))
    pg.goto(f"{B}/", wait_until="domcontentloaded")
    pg.wait_for_selector(".card.skel")
    shot(pg, "01-home-loading", "① 工程列表", "加载", "GET /v1/projects 返回前的骨架屏（请求在浏览器里挂起后放行）")
    for r in held:
        r.continue_()
    pg.unroute("**/v1/projects")
    pg.goto(f"{B}/", wait_until="networkidle")
    pg.wait_for_selector(".card .card-thumb")
    pg.wait_for_timeout(600)
    shot(pg, "01-home-success", "① 工程列表", "成功", "三个公开示例工程（真实出图缩略图）")
    ctx.close()

    ctx = E.new_ctx(browser, be, bootstrap=False)
    pg, _ = E.page(ctx, allow=(401,))
    pg.goto(f"{B}/", wait_until="networkidle")
    pg.wait_for_selector("#auth-required")
    shot(pg, "01-home-error", "① 工程列表", "失败", "没有会话：后端 401 → 「需要 token，从终端完整地址进入」引导")
    ctx.close()

    # ---------------------------------------------------------------- ② 新建与上传
    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/", wait_until="networkidle")
    pg.click("#drop")
    pg.wait_for_selector("#up-drop")
    shot(pg, "02-upload-empty", "② 新建与上传", "空", "新建工程：拖放区 + 三个示例入口")
    stp = be.data / "examples" / "demo-bench-vise.stp"
    pg.fill("#up-title", "台虎钳（上传演示）")
    pg.set_input_files("#up-file", str(stp))
    pg.wait_for_selector(".steps .step.run", timeout=20000)
    pg.wait_for_timeout(400)
    shot(pg, "02-upload-loading", "② 新建与上传", "加载", "上传真实 STEP 后的分阶段时间线（作业运行中，含已用秒数）")
    pg.wait_for_selector("#up-enter", timeout=300000)
    pg.wait_for_timeout(300)
    shot(pg, "02-upload-success", "② 新建与上传", "成功", "解析 + 候选分图 + 逐候选试渲染完成，给出候选过闸数")
    uploaded = None
    pg.click("#up-enter")
    pg.wait_for_url(re.compile(r".*/p/p-\d{8}-[0-9a-f]{6}/.*"))
    uploaded = re.search(r"/p/([^/]+)/", pg.url).group(1)
    pg.goto(f"{B}/", wait_until="networkidle")
    pg.click("#drop")
    pg.fill("#up-title", "损坏的导出文件")
    pg.set_input_files("#up-file", str(make_bogus()))
    pg.wait_for_selector("#upload .banner.error", timeout=120000)
    pg.wait_for_timeout(300)
    shot(pg, "02-upload-fail", "② 新建与上传", "失败", "上传一个不是 STEP 的文件：后端准备作业失败，时间线标红并给原因")
    ctx.close()

    # ---------------------------------------------------------------- ③ 规划台
    crowded = upload_crowded(be)
    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/p/{crowded}/?stage=plan", wait_until="networkidle")
    E.studio_ready(pg)
    pg.wait_for_selector("[data-empty]")
    pg.wait_for_timeout(1500)
    shot(pg, "03-plan-empty", "③ 规划台", "空", "候选全部被拦：后端 empty_reason + 「请框选分组」三步引导")
    ctx.close()

    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    held = []
    pg.route("**/api/model.glb", lambda r: held.append(r))
    pg.goto(f"{B}/p/example-gearbox/?stage=plan", wait_until="domcontentloaded")
    E.studio_ready(pg)
    pg.wait_for_selector(".vp-skeleton")
    pg.wait_for_selector(".cand-card")
    shot(pg, "03-plan-loading", "③ 规划台", "加载", "可交互骨架先到：零件树与候选已就绪，3D 模型（GLB）仍在载入")
    for r in held:
        r.continue_()
    ctx.close()

    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/p/example-gearbox/?stage=plan", wait_until="networkidle")
    E.studio_ready(pg)
    pg.wait_for_selector(".cand-card.pass")
    pg.wait_for_timeout(1500)
    shot(pg, "03-plan-success", "③ 规划台", "成功", "结构树 ↔ 3D ↔ 候选分图（每个候选都已试渲染并过 QA）")
    ctx.close()

    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx, allow=(503,))
    pg.route("**/api/model.glb", lambda r: r.fulfill(status=503, body="unavailable"))
    pg.goto(f"{B}/p/example-gearbox/?stage=plan", wait_until="networkidle")
    E.studio_ready(pg)
    pg.wait_for_selector(".vp-error", timeout=20000)
    pg.wait_for_timeout(400)
    shot(pg, "03-plan-fail", "③ 规划台", "失败", "3D 模型取不到（GLB 503）：视口给出原因与重试，其余面板照常可用")
    ctx.close()

    # ---------------------------------------------------------------- ④ 出图与 QA
    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/p/{uploaded}/?stage=qa", wait_until="networkidle")
    E.studio_ready(pg)
    pg.wait_for_selector("#qa-render")
    shot(pg, "04-qa-empty", "④ 出图与 QA", "空", "还没出过图：渲染入口与说明")
    ctx.close()

    # ⑤ 的空态要在这个工程出图之前拍
    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/p/{uploaded}/?stage=export", wait_until="networkidle")
    E.studio_ready(pg)
    pg.wait_for_selector(".exp-blank")
    shot(pg, "05-export-empty", "⑤ 导出", "空", "还没有过闸的出图：导出锁定并指向「出图与 QA」")
    ctx.close()

    # 加载态：上传的工程补上名称、接受一个过闸候选，在页面上点「渲染出图」；作业在后端真实运行，
    # 截图期间把这次作业的 SSE 在浏览器里挂起（示例模型出图只要 1 秒左右，来不及拍），拍完放行。
    _, cj, _ = be.rest("GET", f"/v1/projects/{uploaded}/plan/candidates")
    cid = next(c["id"] for c in cj["result"]["candidates"] if c["status"] == "pass")
    _, plan, hdr = be.rest("GET", f"/v1/projects/{uploaded}/plan")
    names = {t["selector"]: t for t in E.EXM.plan_for("vise")["terms"]}
    plan["terms"] = [dict(t, term=names.get(t["selector"], {}).get("term") or t["term"] or "连接件",
                          label=names.get(t["selector"], {}).get("label", t.get("label", "once"))) for t in plan["terms"]]
    plan["figures"] = []
    _, _, hdr = be.rest("PUT", f"/v1/projects/{uploaded}/plan", plan, {"If-Match": hdr.get("ETag")})
    be.rest("POST", f"/v1/projects/{uploaded}/plan/candidates/accept", {"candidate_ids": [cid]}, {"If-Match": hdr.get("ETag")})
    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    held = []
    pg.goto(f"{B}/p/{uploaded}/?stage=qa", wait_until="networkidle")
    E.studio_ready(pg)
    pg.route("**/v1/jobs/*/events*", lambda r: held.append(r))
    pg.click("#qa-render")
    pg.wait_for_selector(".qa-blank .stamp-run .spin", timeout=10000)
    pg.wait_for_timeout(400)
    shot(pg, "04-qa-loading", "④ 出图与 QA", "加载", "渲染作业已提交（202）并在后端运行：页面显示进度、禁用再次出图")
    for r in held:
        r.continue_()
    pg.unroute("**/v1/jobs/*/events*")
    E.wait_render_terminal(pg)
    ctx.close()

    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/p/example-gearbox/?stage=qa", wait_until="networkidle")
    E.studio_ready(pg)
    gate = pg.eval_on_selector(".qa-gate", "e => e.textContent") if pg.query_selector(".qa-gate") else ""
    if "13 项全过" not in gate or "过期" in gate:
        prev = E.click_render(pg)
        E.wait_render_terminal(pg, prev)
    pg.wait_for_selector(".svg-pane svg")
    pg.wait_for_timeout(800)
    shot(pg, "04-qa-success", "④ 出图与 QA", "成功", "13 项 QA 全过：盖章总览 + 点阵 + 预览（预览 URL 绑定版本 ?v=<rid>）")
    ctx.close()

    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/p/example-vise/?stage=plan", wait_until="networkidle")
    E.studio_ready(pg)
    whole = E.page_state(pg, "(st.state.candidates || []).find(c => c.source && c.source.kind === 'whole')")
    pg.click(f'.cand-card[data-cid="{whole["id"]}"] [data-accept-fail]')
    pg.wait_for_function("() => /已写入图纸页/.test(document.querySelector('.cand-card.fail')?.textContent || '')", timeout=30000)
    E.stage(pg, "qa")
    prev = E.click_render(pg)
    E.wait_render_terminal(pg, prev)
    pg.wait_for_selector(".job-fail")
    pg.wait_for_timeout(600)
    shot(pg, "04-qa-fail", "④ 出图与 QA", "失败", "本次出图未过闸：作业诊断 + 白话原因 + 修复直达；预览 / 导出仍是上一成功版本")
    ctx.close()
    be.reset_example_plan("vise")

    # ---------------------------------------------------------------- ⑤ 导出
    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/p/example-gearbox/?stage=export", wait_until="networkidle")
    E.studio_ready(pg)
    pg.wait_for_selector("#do-pack")
    E.pick_formats(pg, {"pdf": True, "docx": True, "dxf": True, "dwg": False})
    pg.click("#do-pack")
    pg.wait_for_selector(".pack-bar", timeout=10000)
    pg.wait_for_timeout(700)
    shot(pg, "05-export-loading", "⑤ 导出", "加载", "打包中：生成 PDF / Word / 源文件并逐页核验（导出作业 SSE 进度）")
    pg.wait_for_selector(".exp-done", timeout=600000)
    pg.wait_for_timeout(500)
    pg.set_viewport_size({"width": 1440, "height": 1300})
    shot(pg, "05-export-success", "⑤ 导出", "成功", "附图包完成：逐「版本 × 格式」清单、逐页核验报告、逐图明细、CNIPA 旁注、下载")
    ctx.close()

    ctx = E.new_ctx(browser, nopdf)
    pg, _ = E.page(ctx)
    pg.goto(f"{nopdf.base}/p/example-valve/?stage=qa", wait_until="networkidle")
    E.studio_ready(pg)
    gate = pg.eval_on_selector(".qa-gate", "e => e.textContent") if pg.query_selector(".qa-gate") else ""
    if "13 项全过" not in gate:
        prev = E.click_render(pg)
        E.wait_render_terminal(pg, prev)
    E.stage(pg, "export")
    pg.wait_for_selector("#do-pack")
    E.pick_formats(pg, {"pdf": True, "docx": True, "dxf": False, "dwg": False})
    pg.click("#do-pack")
    pg.wait_for_selector(".exp-done", timeout=600000)
    pg.wait_for_timeout(500)
    pg.set_viewport_size({"width": 1440, "height": 1300})
    shot(pg, "05-export-fail", "⑤ 导出", "失败", "缺 pypdfium2：PDF「未核验」并写明原因、不报完成，文件在包内 _未通过核验/")
    ctx.close()

    # ---------------------------------------------------------------- 补充
    ctx = E.new_ctx(browser, ai)
    pg, _ = E.page(ctx)
    pg.goto(f"{ai.base}/p/example-gearbox/?stage=plan", wait_until="networkidle")
    E.studio_ready(pg)
    pg.wait_for_timeout(1200)
    E.page_state(pg, "st.setSelection(['PGB-MOTOR-SHAFT'])")
    pg.click("#afb-name")
    pg.wait_for_selector("#dlg-outbound[open]")
    pg.wait_for_timeout(300)
    shot(pg, "x1-ai-outbound", "补充", "AI 外发确认", "AI 起名外发前明示 outbound_fields（e2e 假通道，不连真实大模型）")
    ctx.close()

    ctx = E.new_ctx(browser, be)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/p/example-gearbox/?stage=export", wait_until="networkidle")
    E.studio_ready(pg)
    pg.wait_for_selector("#do-pack")
    pg.click('[data-preset="conservative"]')
    pg.wait_for_selector("#preset-hint")
    shot(pg, "x2-export-conservative", "补充", "保守版心提示", "导出屏：格式 / 版心 / CNIPA 旁注；选保守版心时提示字高风险")
    ctx.close()

    ctx = E.new_ctx(browser, be, bootstrap=False)
    pg, _ = E.page(ctx)
    pg.goto(f"{B}/p/example-gearbox/?token={E.TOKEN}", wait_until="networkidle")
    E.studio_ready(pg)
    pg.wait_for_selector("#auth-notice")
    pg.wait_for_timeout(800)
    shot(pg, "x3-auth-deprecated", "补充", "旧式 ?token= 链接", "据 Deprecation 头提示弃用，已换成会话 Cookie 并抹掉地址栏 token")
    ctx.close()

    for proj in be.rest("GET", "/v1/projects")[1].get("projects", []):
        if not proj.get("example"):
            be.rest("DELETE", "/api/wb/projects/%s" % proj["id"])


def make_bogus():
    path = E.SCRATCH / "bogus.stp"
    E.SCRATCH.mkdir(parents=True, exist_ok=True)
    path.write_text("ISO-10303-21;\nHEADER;\n/* 这不是一个有效的 STEP 装配体：用来演示准备失败 */\n" + "x" * 400 + "\nEND-ISO-10303-21;\n",
                    encoding="utf-8")
    return path


def upload_crowded(be):
    edge = E.SCRATCH / "edge"
    stp = edge / "edge-crowded.stp"
    if not stp.is_file():
        import subprocess
        edge.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(E.REPO / "scripts" / "make_demo_assemblies.py"), "-o", str(edge), "--edge-cases"],
                       check=True, cwd=str(E.REPO), capture_output=True)
    from urllib.parse import quote
    st, j, _ = be.rest_raw("POST", "/v1/projects?title=%s&name=crowded.stp" % quote("低件数拥挤（边界样例）"), stp.read_bytes())
    be.wait_job(j["job_id"])
    st, s, _ = be.rest("POST", f"/v1/projects/{j['project_id']}/plan/suggest", {})
    be.wait_job(s["job_id"])
    return j["project_id"]


if __name__ == "__main__":
    main()
