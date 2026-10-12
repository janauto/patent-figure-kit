#!/usr/bin/env python3
"""WP3b 端到端套件：新界面 × 真实后端（scripts/workbench.py），Playwright 驱动，零 JSON、零命令行。

用例（每条都断言零控制台错误；只放行该用例故意制造的 HTTP 状态在浏览器网络层的报错行）：
  golden_gearbox / golden_vise / golden_valve
        首页「新建 → 示例」→ 分阶段时间线（生成演示模型 / 解析 / 候选分图 / 试渲染 k/N）→ 进入规划台 →
        接受 ≥1 个过闸候选（POST /plan/candidates/accept + If-Match）→ 出图（202 → SSE）→ QA 13 项全过 →
        导出 both / pdf+docx → 下载 zip → 解包核对：核验报告.json 的 documents 全部 ok、包内文件 sha256 与导出结果封印一致
  empty_candidates        上传「低件数拥挤」STEP（make_demo_assemblies.py --edge-cases）→ 候选全被拦 → 后端 empty_reason + 「请框选分组」引导
  vise_blocked_fix        台虎钳整机候选被拦（原因 + 建议 + 修复直达）；仍然接受 → 出图失败 → 本次作业诊断、上一成功版本说明、QA 修复直达
  ai_not_configured       AI 未配置：中性徽标 / 浮条提示；能力被改成「已配置」时后端 200 ai_not_configured → 中性提示不报错
  ai_configured           （假 LLM 通道）外发确认列出 outbound_fields → apply=false 预览 → apply=true + If-Match 写入；
                          外发载荷只含选中的空白件；写入前计划被改 → 409 冲突提示
  sse_reconnect           SSE 断线：流中途结束 → 浏览器带 Last-Event-ID 重连；再遇非 200（CLOSED）→ 前端按 ?last_event_id= 手动重连；终态正确
  refresh_resume          后端已有在跑的渲染作业 → 打开页面自动接上、期间禁用再次渲染、终态自动呈现
  etag_409                计划在别处被改 → 本页保存 409 → 顶栏「plan 冲突，请重载」+ 重载按钮；接受候选 409 同理
  cookie_only             Cookie-only 新标签进入子应用；无 Cookie → 「需要 token」引导；旧式 ?token= 链接 → Deprecation 提示 + 换 Cookie + 抹掉地址栏 token
  dwg_conservative_note   能力接口 dwg_available=false（注入）→ DWG 事前禁选并给原因；保守版心提示；CNIPA 旁注
  pdf_unverified          缺 pypdfium2（PFK_PDF_RENDERER=none 实例）→ 导出前提示、PDF 项「未核验」+ 原因、包内 _未通过核验/

后端实例（不写 ~/.patent-workbench；token 与配置各自隔离）：
  main    8806  --data ~/.patent-workbench-wp3b       配置 ~/.patent-workbench-wp3b-config（provider none）
  ai      8811  --data ~/.patent-workbench-wp3b-ai    配置 provider deepseek → 本脚本起的假 LLM（127.0.0.1:8812）
  nopdf   8813  --data ~/.patent-workbench-wp3b-nopdf PFK_PDF_RENDERER=none
端口上已有同 token 的实例就复用，否则本脚本起子进程并在结束时停掉。

    python3 webui/dev/e2e_live.py                 # 全部用例；全过退出 0
    python3 webui/dev/e2e_live.py --only golden_gearbox,etag_409
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from patent_figure import examples as EXM  # noqa: E402  纯数据模块，不导入 OCP

TOKEN = os.environ.get("WP3B_TOKEN", "wp3b-e2e-token-0001")
HOME = Path.home()
SCRATCH = Path(os.environ.get("WP3B_SCRATCH", str(HOME / ".patent-workbench-wp3b-e2e")))
FAKE_LLM_PORT = 8812
VIEW = {"width": 1440, "height": 900}
RESULTS = []
TIMINGS = {}
NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    print(msg, flush=True)


def check(name, ok, extra=""):
    RESULTS.append((name, bool(ok), extra))
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' · ' + extra) if extra else ''}")
    return bool(ok)


# --------------------------------------------------------------------------- 假 LLM（OpenAI 兼容）
FAKE_NAMES = {"PGB-MOTOR-SHAFT": "电机轴", "PGB-OIL-SEAL": "油封", "PGB-SUN-GEAR": "太阳轮",
              "PGB-END-COVER": "端盖", "PGB-RING-GEAR": "内齿圈"}
FAKE_CALLS = []


class FakeLLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        system = body["messages"][0]["content"]
        if "结构工程师" in system:          # 上传 / 示例准备后的自动结构识别：给一个空分组（全部归「其他零件」）
            FAKE_CALLS.append({"kind": "structure", "parts": []})
            return self._reply({"groups": [], "parts": {}})
        user = json.loads(body["messages"][-1]["content"])
        parts = next((v for k, v in user.items() if k.startswith("产品零件")), [])
        FAKE_CALLS.append({"kind": "terms", "parts": [p["name"] for p in parts], "keys": list(user.keys())})
        terms = [{"selector": p["name"], "term": FAKE_NAMES.get(p["name"], "连接件"), "label": "once",
                  "confidence": "low" if p["name"] == "PGB-OIL-SEAL" else "high",
                  "reason": "按零件代号与尺寸判断（e2e 假通道）"} for p in parts]
        return self._reply({"terms": terms})

    def _reply(self, obj):
        out = json.dumps({"choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)},
                                       "finish_reason": "stop"}]}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start_fake_llm():
    srv = ThreadingHTTPServer(("127.0.0.1", FAKE_LLM_PORT), FakeLLM)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --------------------------------------------------------------------------- 后端实例
class Backend:
    def __init__(self, name, port, data, config, cfg_body, env_extra=None, serve_args=()):
        self.name, self.port = name, port
        self.data, self.config = Path(data), Path(config)
        self.cfg_body, self.env_extra = cfg_body, env_extra or {}
        self.serve_args = list(serve_args)
        self.base = "http://127.0.0.1:%d" % port
        self.proc = None
        self.logf = None

    def rest(self, method, path, body=None, headers=None, raw=False, timeout=60):
        h = {"Authorization": "Bearer " + TOKEN}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with NOPROXY.open(req, timeout=timeout) as r:
                payload = r.read()
                return r.status, (payload if raw else json.loads(payload or b"{}")), r.headers   # 头名大小写不敏感
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                j = json.loads(payload or b"{}")
            except ValueError:
                j = {"raw": payload[:200].decode("utf-8", "replace")}
            return e.code, j, e.headers

    def rest_raw(self, method, path, data: bytes):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/octet-stream"})
        try:
            with NOPROXY.open(req, timeout=120) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), e.headers

    def alive(self):
        try:
            st, _, _ = self.rest("GET", "/v1/capabilities", timeout=3)
            return st == 200
        except Exception:
            return False

    def up(self):
        if self.alive():
            log(f"  · 复用已在 {self.port} 的后端（{self.name}）")
            return
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", self.port)) == 0:
                raise SystemExit(f"端口 {self.port} 被别的服务占用（token 不符或不是工作台）")
        self.config.mkdir(parents=True, exist_ok=True)
        cfg = self.config / "config.json"
        cfg.write_text(json.dumps(self.cfg_body, ensure_ascii=False), encoding="utf-8")
        os.chmod(cfg, 0o600)
        env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        env.update({"WORKBENCH_TOKEN": TOKEN, "PATENT_WORKBENCH_CONFIG": str(self.config)}, **self.env_extra)
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.logf = open(SCRATCH / f"backend-{self.name}.log", "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(REPO / "scripts" / "workbench.py"), "--data", str(self.data),
                                      "serve", "--no-browser", "--port", str(self.port)] + self.serve_args,
                                     cwd=str(REPO), env=env, stdout=self.logf, stderr=subprocess.STDOUT)
        for _ in range(120):
            if self.alive():
                log(f"  · 已起后端 {self.name} :{self.port}（--data {self.data}）")
                return
            time.sleep(0.5)
        raise SystemExit(f"后端 {self.name} 没能起来，见 {SCRATCH}/backend-{self.name}.log")

    def down(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.logf:
            self.logf.close()

    # -- 夹具：用例之间的隔离（只改测试数据，不碰发布 / 导出语义）--
    def wait_job(self, job_id, timeout=600):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, j, _ = self.rest("GET", "/v1/jobs/%s" % job_id)
            if st == 200 and j.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
                return j
            time.sleep(0.5)
        raise RuntimeError("作业 %s 超时" % job_id)

    def ensure_example(self, ex):
        st, j, _ = self.rest("POST", "/v1/projects/from-example", {"example": ex})
        assert st == 202, (st, j)
        return self.wait_job(j["job_id"])

    def reset_example_plan(self, ex):
        """把示例工程的计划恢复成仓库里的示例计划（重复跑用例时不累积图卡）。"""
        pid = "example-" + ex
        st, plan, hdr = self.rest("GET", "/v1/projects/%s/plan" % pid)
        if st != 200:
            return False
        fresh = EXM.plan_for(ex)
        fresh.setdefault("source", {})["step"] = (plan.get("source") or {}).get("step", "")
        st2, j, _ = self.rest("PUT", "/v1/projects/%s/plan" % pid, fresh, {"If-Match": hdr.get("ETag", "")})
        assert st2 == 200, (st2, j)
        return True

    def plan(self, pid):
        st, plan, hdr = self.rest("GET", "/v1/projects/%s/plan" % pid)
        return plan, hdr.get("ETag")


# --------------------------------------------------------------------------- 浏览器工具
class Watch:
    """收集一页的控制台 error 与未捕获异常。allow：本用例故意制造的 HTTP 状态（浏览器在网络层会报一行
    「Failed to load resource: … status of N」，只放行这些状态，且只放行这一种文案）。"""

    def __init__(self, page, allow=()):
        self.errors, self.allow = [], set(allow)
        self.allowed = []
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.errors.append("pageerror: %s" % e))
        # 资源与接口的 4xx/5xx（含静态资源、importmap 模块）：只放行本用例故意制造的状态
        page.on("response", lambda r: self.errors.append("HTTP %d %s" % (r.status, r.url))
                if r.status >= 400 and r.status not in self.allow else None)

    def _console(self, m):
        if m.type != "error":
            return
        t = m.text
        mm = re.search(r"Failed to load resource: the server responded with a status of (\d+)", t)
        if mm and int(mm.group(1)) in self.allow:
            self.allowed.append(t)
            return
        if "EventSource's response has" in t and any(str(a) in t for a in self.allow):
            self.allowed.append(t)
            return
        self.errors.append("%s @ %s" % (t, (m.location or {}).get("url", "")))

    def zero(self, name):
        return check(f"{name} · 零控制台错误", not self.errors,
                     ("errors=" + json.dumps(self.errors[:4], ensure_ascii=False)) if self.errors
                     else (f"放行 {len(self.allowed)} 行故意制造的 HTTP 状态" if self.allowed else ""))


def new_ctx(browser, be, bootstrap=True):
    ctx = browser.new_context(viewport=VIEW, accept_downloads=True)
    if bootstrap:
        pg = ctx.new_page()
        pg.on("dialog", lambda d: d.accept())
        pg.goto(f"{be.base}/?token={TOKEN}", wait_until="networkidle")
        pg.wait_for_timeout(500)
        assert "token=" not in pg.url, "地址栏 token 没被抹掉：%s" % pg.url
        pg.close()
    return ctx


def page(ctx, allow=()):
    pg = ctx.new_page()
    pg.on("dialog", lambda d: d.accept())
    return pg, Watch(pg, allow)


ST = "const st = await import('./js/state.js');"


def studio_ready(pg, timeout=60000):
    pg.wait_for_function("() => document.querySelector('#tb-proj') && document.querySelector('#tb-proj').textContent.length > 3",
                         timeout=timeout)
    pg.wait_for_timeout(400)


def stage(pg, name):
    pg.click(f'.stage-tab[data-stage="{name}"]')
    pg.wait_for_timeout(500)


def click_render(pg):
    """点出图按钮；返回点之前的作业 ID（等待终态时据此确认是「这一次」的作业）。"""
    pg.wait_for_selector("#qa-render, #rerender-stale, #rerender:not([disabled]), #qa-retry", timeout=30000)
    prev = page_state(pg, "st.state.renderJob && st.state.renderJob.id")
    pg.click("#qa-render, #rerender-stale, #rerender:not([disabled]), #qa-retry")
    return prev or ""



def wait_js(pg, expr, timeout=60000, step=0.4):
    """轮询页面里的表达式（可用 st = state 模块）直到为真；page.evaluate 会等 async 函数，wait_for_function 不会。"""
    t0 = time.time()
    while time.time() - t0 < timeout / 1000:
        if page_state(pg, expr):
            return True
        time.sleep(step)
    raise TimeoutError("等待超时：" + expr[:80])


def wait_render_terminal(pg, prev="", timeout=600000):
    # 先等本次作业真的提交（作业 ID 变了），再等终态与按钮恢复
    wait_js(pg, "!!(st.state.renderJob && st.state.renderJob.id && st.state.renderJob.id !== %s)" % json.dumps(prev), 30000)
    wait_js(pg, "(() => { const j = st.state.renderJob; return !!(j && !j.running && j.state && j.state !== 'running')"
                " && !document.querySelector('#rerender[disabled]') && !document.querySelector('.qa-blank .stamp-run .spin'); })()",
            timeout)
    pg.wait_for_timeout(600)


def page_state(pg, expr):
    return pg.evaluate(f"async () => {{ {ST} return ({expr}); }}")


def toast_text(pg):
    return pg.evaluate("() => document.querySelector('#toast')?.textContent || ''")


def pick_formats(pg, want):
    """勾选 / 取消导出格式（每次点击后界面整体重绘，所以每次都重新定位，不复用元素句柄）。"""
    for fmt, on in want.items():
        loc = pg.locator(f'[data-fmt="{fmt}"]')
        if loc.count() and not loc.is_disabled() and loc.is_checked() != on:
            loc.click()
            pg.wait_for_timeout(200)


def download_zip(ctx, href):
    resp = ctx.request.get(href)
    return resp.status, resp.body()


def verify_zip(blob, export_result):
    """解包核对：核验报告.json 的 documents 全部 status=ok；包内每个文件 sha256 与导出结果的封印一致。"""
    zf = zipfile.ZipFile(io.BytesIO(blob))
    names = zf.namelist()
    report_name = next((n for n in names if n.endswith("核验报告.json")), None)
    out = {"files": len(names), "report": bool(report_name), "docs": {}, "sha_ok": True, "sha_checked": 0,
           "quarantine": [n for n in names if "_未通过核验/" in n]}
    if report_name:
        rep = json.loads(zf.read(report_name).decode("utf-8"))
        docs = rep.get("documents") or {}
        out["docs"] = {k: (v.get("status") or ("ok" if v.get("ok") else "fail")) for k, v in docs.items()}
    seal = export_result.get("sha256") or {}
    for rel, digest in seal.items():
        hit = next((n for n in names if n == rel or n.endswith("/" + rel)), None)
        if hit is None:
            out["sha_ok"] = False
            out.setdefault("missing", []).append(rel)
            continue
        out["sha_checked"] += 1
        if hashlib.sha256(zf.read(hit)).hexdigest() != digest:
            out["sha_ok"] = False
            out.setdefault("mismatch", []).append(rel)
    return out


# --------------------------------------------------------------------------- 用例
def golden(browser, be, ex):
    name = f"golden_{ex}"
    pid = "example-" + ex
    existed = be.rest("GET", f"/v1/projects/{pid}")[0] == 200
    if existed:
        be.reset_example_plan(ex)
    ctx = new_ctx(browser, be)
    pg, w = page(ctx)
    try:
        pg.goto(f"{be.base}/", wait_until="networkidle")
        pg.wait_for_selector("#drop", timeout=30000)
        pg.click("#drop")
        pg.click(f"#up-ex-{ex}")
        t0 = time.time()
        # 进度态：时间线出现运行中的步骤与「已用 N 秒」
        pg.wait_for_selector(".steps .step.run, .steps .step.ok", timeout=20000)
        saw_running = pg.query_selector("#up-elapsed") is not None
        pg.wait_for_selector("#up-enter", timeout=300000)
        t_import = time.time() - t0
        steps = pg.eval_on_selector_all(".steps .step", "els => els.map(e => [e.className, e.textContent.replace(/\\s+/g,' ').trim()])")
        summary = pg.eval_on_selector(".up-done .ok-line", "e => e.textContent.replace(/\\s+/g,' ').trim()") \
            if pg.query_selector(".up-done .ok-line") else ""
        trial_seen = any("试渲染" in s[1] for s in steps)
        all_ok = all(" ok" in (" " + s[0]) for s in steps)
        TIMINGS[f"{ex}·载入+候选（{'已存在' if existed else '首次'}）"] = round(t_import, 1)
        check(f"{name} · 示例载入时间线（{'已存在' if existed else '首次'} {t_import:.1f}s）",
              all_ok and trial_seen and "候选" in summary and (saw_running or t_import < 3),
              f"steps={[s[1][:28] for s in steps]} summary={summary!r}")
        pg.click("#up-enter")
        pg.wait_for_url(re.compile(rf".*/p/{pid}/.*"), timeout=30000)
        studio_ready(pg)
        pg.wait_for_selector(".cand-card.pass [data-accept]", timeout=60000)
        plan0, _ = be.plan(pid)
        cid = pg.get_attribute(".cand-card.pass", "data-cid")
        accepts = []
        pg.on("request", lambda r: accepts.append(r) if r.method == "POST" and r.url.endswith("/plan/candidates/accept") else None)
        pg.click(f'.cand-card[data-cid="{cid}"] [data-accept]')
        pg.wait_for_function(f"() => /已写入图纸页/.test(document.querySelector('.cand-card[data-cid=\"{cid}\"]')?.textContent || '')",
                             timeout=30000)
        plan1, etag1 = be.plan(pid)
        ui_etag = page_state(pg, "st.state.planEtag")
        ifm = accepts[0].headers.get("if-match") if accepts else None
        check(f"{name} · 接受候选 {cid}（POST accept + If-Match，计划 +1 图，前端 ETag 与服务端一致）",
              len(plan1["figures"]) == len(plan0["figures"]) + 1 and ui_etag == etag1 and bool(ifm),
              f"figs {len(plan0['figures'])}→{len(plan1['figures'])} ifMatch={'有' if ifm else '无'} etag={'一致' if ui_etag == etag1 else (ui_etag, etag1)}")
        # 出图
        stage(pg, "qa")
        t0 = time.time()
        prev = click_render(pg)
        wait_render_terminal(pg, prev)
        TIMINGS[f"{ex}·出图（{len(plan1['figures'])} 张）"] = round(time.time() - t0, 1)
        gate = pg.eval_on_selector(".qa-gate", "e => e.textContent") if pg.query_selector(".qa-gate") else ""
        st = page_state(pg, "{ ver: st.state.renderInputs.version, ev: st.state.renderInputs.etag, plan: st.state.planEtag,"
                            " job: st.state.renderJob && st.state.renderJob.id, figs: (st.state.render?.figures||[]).length,"
                            " allPass: (st.state.render?.figures||[]).every(f => f.pass) }")
        pg.wait_for_selector(".svg-pane svg", timeout=20000)
        numerals = pg.eval_on_selector_all(".svg-pane svg text[data-numeral]", "els => els.length")
        preview_v = pg.evaluate("() => performance.getEntriesByType('resource').map(e => e.name).filter(u => /api\\/preview\\/.+\\?v=/.test(u)).length")
        check(f"{name} · 出图 QA 13 项全过（结果绑定版本 rid 与输入 plan_etag，预览带 ?v=<rid>）",
              "13 项全过" in gate and st["allPass"] and st["ver"] == st["job"] and st["ev"] == st["plan"]
              and numerals > 0 and preview_v > 0,
              f"gate={gate.strip()!r} figs={st['figs']} rid={st['ver']} ev==plan={st['ev'] == st['plan']} numerals={numerals} previewV={preview_v}")
        # 导出 both / pdf + docx
        stage(pg, "export")
        pg.wait_for_selector("#do-pack", timeout=20000)
        note = pg.eval_on_selector("#cnipa-note", "e => e.textContent") if pg.query_selector("#cnipa-note") else ""
        pg.click('[data-v="both"]')
        pick_formats(pg, {"pdf": True, "docx": True, "dxf": False, "dwg": False})
        bodies = []
        pg.on("request", lambda r: bodies.append(json.loads(r.post_data or "{}"))
              if r.method == "POST" and r.url.endswith("/export") else None)
        t0 = time.time()
        pg.click("#do-pack")
        pg.wait_for_selector(".exp-done", timeout=600000)
        TIMINGS[f"{ex}·导出 both/pdf+docx"] = round(time.time() - t0, 1)
        body = bodies[0] if bodies else {}
        items = pg.eval_on_selector_all(".exp-items tbody tr", "els => els.map(e => [e.dataset.item, e.className])")
        docs = pg.eval_on_selector_all(".doc-row", "els => els.map(e => [e.dataset.doc, e.className])")
        ok_head = pg.query_selector(".exp-done .stamp-pass") is not None
        check(f"{name} · 导出请求带 variant/formats/dpi/sheet_preset/render_job_id/plan_etag",
              body.get("variant") == "both" and sorted(body.get("formats") or []) == ["docx", "pdf"]
              and body.get("sheet_preset") == "standard" and body.get("dpi") and body.get("render_job_id") == st["ver"]
              and body.get("plan_etag") == st["plan"], json.dumps(body, ensure_ascii=False)[:200])
        check(f"{name} · 导出清单 items 全 ok、documents 全 ok、CNIPA 旁注在导出屏",
              ok_head and items and all(c.strip() == "ok" for _, c in items) and docs
              and all("ok" in c.split() for _, c in docs) and "以当期" in note,
              f"items={len(items)} docs={[(d, c) for d, c in docs][:4]} note={'有' if note else '无'}")
        href = pg.get_attribute(".exp-zip-acts a[download]", "href")
        st_dl, blob = download_zip(ctx, href)
        export_id = re.search(r"/v1/exports/([^/]+)/download", href).group(1)
        _, job, _ = be.rest("GET", f"/v1/exports/{export_id}")
        z = verify_zip(blob, job.get("result") or {})
        check(f"{name} · 下载 zip 并解包核对（documents 全过 + sha256 封印一致）",
              st_dl == 200 and z["report"] and z["docs"] and all(v == "ok" for v in z["docs"].values())
              and z["sha_ok"] and z["sha_checked"] >= 4 and not z["quarantine"],
              f"http={st_dl} files={z['files']} docs={z['docs']} sha={z['sha_checked']}/{'ok' if z['sha_ok'] else z.get('mismatch') or z.get('missing')}")
    finally:
        w.zero(name)
        ctx.close()


def first_run_example(browser, be):
    """全新数据目录（--no-example）：首页真实空态 →「一键载入示例」→ 首次需现场生成演示 STEP 的进度态（已用秒数 + 首次
    耗时说明 + 分阶段时间线）→ 进入规划台。耗时写进 TIMINGS（「首次」，任务书估计 25–60 s，以实测为准）。"""
    name = "first_run_example"
    ctx = new_ctx(browser, be)
    pg, w = page(ctx)
    try:
        pg.goto(f"{be.base}/", wait_until="networkidle")
        pg.wait_for_selector("#load-example", timeout=20000)
        empty = pg.eval_on_selector(".empty-sheet", "e => e.textContent.replace(/\\s+/g,' ')")
        t0 = time.time()
        pg.click("#load-example")
        pg.wait_for_selector("#up-elapsed", timeout=10000)
        pg.wait_for_timeout(600)
        note = pg.eval_on_selector(".up-progress", "e => e.textContent.replace(/\\s+/g,' ')")
        running = pg.query_selector(".steps .step.run") is not None
        pg.wait_for_selector("#up-enter", timeout=300000)
        t = time.time() - t0
        TIMINGS["gearbox·首次载入示例（生成 STEP + 解析 + 候选）"] = round(t, 1)
        pg.click("#up-enter")
        pg.wait_for_url(re.compile(r".*/p/example-gearbox/.*"), timeout=30000)
        studio_ready(pg)
        n = pg.eval_on_selector_all(".cand-card", "els => els.length")
        check(f"首次载入示例：真实空态 → 进度态（已用秒数 + 首次耗时说明 + 运行中步骤）→ 候选就绪（{t:.1f}s）",
              "还没有工程" in empty and running and "首次载入示例" in note and "已用" in note and n > 0,
              f"候选卡 {n} 张")
    finally:
        w.zero(name)
        ctx.close()


def empty_candidates(browser, be):
    name = "empty_candidates"
    edge = SCRATCH / "edge"
    stp = edge / "edge-crowded.stp"
    if not stp.is_file():
        edge.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(REPO / "scripts" / "make_demo_assemblies.py"), "-o", str(edge), "--edge-cases"],
                       check=True, cwd=str(REPO), capture_output=True)
    ctx = new_ctx(browser, be)
    pg, w = page(ctx)
    try:
        pg.goto(f"{be.base}/", wait_until="networkidle")
        pg.click("#drop")
        pg.fill("#up-title", "e2e 低件数拥挤")
        pg.set_input_files("#up-file", str(stp))
        pg.wait_for_selector("#up-enter", timeout=300000)
        summary = pg.eval_on_selector(".up-progress", "e => e.textContent.replace(/\\s+/g,' ')")
        url_before = pg.url
        pg.click("#up-enter")
        pg.wait_for_url(re.compile(r".*/p/p-\d{8}-[0-9a-f]{6}/.*"), timeout=30000)
        pid = re.search(r"/p/([^/]+)/", pg.url).group(1)
        studio_ready(pg)
        pg.wait_for_selector("[data-empty]", timeout=30000)
        guide = pg.eval_on_selector("[data-empty]", "e => e.textContent.replace(/\\s+/g,' ').trim()")
        _, job, _ = be.rest("GET", f"/v1/projects/{pid}/plan/candidates")
        res = job.get("result") or {}
        steps = res.get("guide", {}).get("steps") or []
        check("候选为空引导态：上传 STEP → 全部候选被拦 → 后端 empty_reason + 「请框选分组」三步",
              bool(res.get("empty_reason")) and res["empty_reason"][:12] in guide and "请框选分组" in guide
              and all(s[:8] in guide for s in steps) and "没过闸" in summary,
              f"pid={pid} empty_reason={res.get('empty_reason')!r} steps={len(steps)} home={url_before.endswith('/')}")
        be.rest("DELETE", f"/api/wb/projects/{pid}")        # 夹具清理
    finally:
        w.zero(name)
        ctx.close()


def vise_blocked_fix(browser, be):
    name = "vise_blocked_fix"
    pid = "example-vise"
    be.reset_example_plan("vise")
    ctx = new_ctx(browser, be)
    pg, w = page(ctx)
    try:
        # 先确保有一个成功发布的版本（失败作业要标明「上一成功版本」）
        st0, prev_proj, _ = be.rest("GET", f"/v1/projects/{pid}")
        _, status, _ = be.rest("GET", f"/p/{pid}/api/render/status")
        if not status.get("version"):
            _, j, _ = be.rest("POST", f"/v1/projects/{pid}/render")
            be.wait_job(j["job_id"])
        _, prev_proj, _ = be.rest("GET", f"/v1/projects/{pid}")
        pg.goto(f"{be.base}/p/{pid}/?stage=plan", wait_until="networkidle")
        studio_ready(pg)
        pg.wait_for_selector(".cand-card.fail", timeout=60000)
        whole = page_state(pg, "(st.state.candidates || []).find(c => c.source && c.source.kind === 'whole')")
        card = f'.cand-card[data-cid="{whole["id"]}"]'
        text = pg.eval_on_selector(card, "e => e.textContent.replace(/\\s+/g,' ')")
        fixes = pg.query_selector_all(f"{card} [data-fix]")
        landed = None
        if fixes:
            fixes[0].click()
            pg.wait_for_timeout(600)
            landed = pg.evaluate("() => { const a = document.activeElement; return a ? (a.dataset.k || a.className || a.tagName) : null; }")
            stage(pg, "plan")
        code = (whole.get("qa") or {}).get("error_code")
        check("台虎钳整机候选被拦：未过闸章 + 白话原因 + 建议 + 修复直达对应控件（布局错误码 → 换视角）",
              whole["status"] == "fail" and "被闸门拦下" in text and "建议" in text and whole.get("suggestion")
              and len(fixes) >= 1 and landed == "view",
              f"reason={whole.get('reason')!r} code={code} fixes={len(fixes)} landed={landed}")
        # 仍然接受 → 出图失败 → 本次作业诊断 + 上一成功版本 + QA 修复直达
        pg.click('.rtab[data-tab="candidates"]')
        pg.click(f"{card} [data-accept-fail]")
        pg.wait_for_function(f"() => /已写入图纸页/.test(document.querySelector('{card}')?.textContent || '')", timeout=30000)
        stage(pg, "qa")
        prev = click_render(pg)
        wait_render_terminal(pg, prev)
        panel = pg.eval_on_selector(".job-fail", "e => e.textContent.replace(/\\s+/g,' ')") if pg.query_selector(".job-fail") else ""
        js = page_state(pg, "{ job: st.state.renderJob && st.state.renderJob.state, ver: st.state.renderInputs.version }")
        fixbtn = pg.query_selector(".job-fail [data-fix]")
        landed2 = None
        if fixbtn:
            fixbtn.click()
            pg.wait_for_timeout(700)
            landed2 = pg.evaluate("() => { const a = document.activeElement; return a ? (a.dataset.k || a.className || a.tagName) : null; }")
        check("出图失败：显示本次作业诊断（未过闸图与白话检查项），预览 / 导出标明属上一成功版本，修复按钮直达控件",
              js["job"] == "failed" and "本次出图失败" in panel and "上一成功版本" in panel and prev_proj["version"] in panel
              and js["ver"] == prev_proj["version"] and fixbtn is not None and landed2 == "view",
              f"job={js['job']} prev={prev_proj['version']} kept={js['ver'] == prev_proj['version']} landed={landed2} panel={panel[:90]!r}")
    finally:
        be.reset_example_plan("vise")
        w.zero(name)
        ctx.close()


def ai_not_configured(browser, be):
    name = "ai_not_configured"
    pid = "example-gearbox"
    ctx = new_ctx(browser, be)
    pg, w = page(ctx)
    try:
        pg.goto(f"{be.base}/p/{pid}/?stage=plan", wait_until="networkidle")
        studio_ready(pg)
        badge = pg.eval_on_selector("#ai-badge", "e => [e.textContent, e.className]")
        page_state(pg, "st.setSelection(['PGB-MOTOR-SHAFT'])")
        pg.wait_for_timeout(300)
        off = pg.query_selector("#afb-off") is not None and pg.query_selector("#afb-name") is None
        check("AI 未配置态：中性徽标「AI 未接入」+ 浮条中性提示，无起名按钮", badge[0] == "AI 未接入" and "off" in badge[1] and off,
              f"badge={badge}")
    finally:
        pg.close()
    # 能力接口说「已配置」但后端判未配置：200 ai_not_configured → 中性提示，不是报错
    pg, w2 = page(ctx)
    try:
        def patch_caps(route):
            r = route.fetch()
            j = r.json()
            j["ai"] = dict(j.get("ai") or {}, configured=True, provider_label="（注入）已配置")
            route.fulfill(response=r, body=json.dumps(j, ensure_ascii=False), headers={**r.headers, "content-type": "application/json"})
        pg.route("**/v1/capabilities*", patch_caps)
        posts = []
        pg.on("response", lambda r: posts.append(r) if r.request.method == "POST" and "ai/draft-terms" in r.url else None)
        pg.goto(f"{be.base}/p/{pid}/?stage=plan", wait_until="networkidle")
        studio_ready(pg)
        page_state(pg, "st.setSelection(['PGB-MOTOR-SHAFT'])")
        pg.wait_for_selector("#afb-name", timeout=5000)
        pg.click("#afb-name")
        pg.wait_for_selector("#dlg-outbound[open]", timeout=10000)
        fields = pg.eval_on_selector("#dlg-outbound", "e => e.textContent.replace(/\\s+/g,' ')")
        pg.click("#ob-ok")
        pg.wait_for_function("() => (document.querySelector('#toast')?.textContent || '').length > 0", timeout=20000)
        t = toast_text(pg)
        cls = pg.eval_on_selector("#toast", "e => e.className")
        body = posts[0].json() if posts else {}
        badge = pg.eval_on_selector("#ai-badge", "e => e.textContent")
        check("AI 未配置（后端 200 ai_not_configured）：外发确认后得到中性提示、徽标回到「AI 未接入」、不报错",
              body.get("status") == "ai_not_configured" and posts[0].status == 200 and "bad" not in cls
              and badge == "AI 未接入" and "STEP 零件名" in fields,
              f"status={body.get('status')} toast={t[:40]!r} cls={cls!r}")
    finally:
        w.errors += w2.errors
        w.zero(name)
        ctx.close()


def ai_configured(browser, be):
    name = "ai_configured"
    pid = "example-gearbox"
    be.ensure_example("gearbox")
    be.reset_example_plan("gearbox")
    plan, etag = be.plan(pid)
    for t in plan["terms"]:                                  # 夹具：留两个空白件
        if t["selector"] in ("PGB-OIL-SEAL",):
            t["term"] = ""
    be.rest("PUT", f"/v1/projects/{pid}/plan", plan, {"If-Match": etag})
    ctx = new_ctx(browser, be)
    pg, w = page(ctx, allow=(409,))
    try:
        reqs = []
        pg.on("request", lambda r: reqs.append((r.headers.get("if-match"), json.loads(r.post_data or "{}")))
              if r.method == "POST" and "ai/draft-terms" in r.url else None)
        pg.goto(f"{be.base}/p/{pid}/?stage=plan", wait_until="networkidle")
        studio_ready(pg)
        FAKE_CALLS.clear()
        page_state(pg, "st.setSelection(['PGB-MOTOR-SHAFT'])")
        pg.click("#afb-name")
        pg.wait_for_selector("#dlg-outbound[open]", timeout=10000)
        dlg = pg.eval_on_selector("#dlg-outbound", "e => e.textContent.replace(/\\s+/g,' ')")
        pg.click("#ob-ok")
        pg.wait_for_selector("#afb-apply", timeout=30000)
        plan_mid, _ = be.plan(pid)
        untouched = next((t for t in plan_mid["terms"] if t["selector"] == "PGB-MOTOR-SHAFT"), {"term": ""})
        term_calls = [c for c in FAKE_CALLS if c.get("kind") == "terms"]
        check("AI 起名 · 外发确认列出 outbound_fields；apply=false 只给建议不写计划；外发载荷只含选中的空白件",
              "STEP 零件名" in dlg and "零件代号" in dlg and reqs and reqs[0][1].get("apply") is False
              and not (untouched.get("term") or "").strip() and term_calls and term_calls[0]["parts"] == ["PGB-MOTOR-SHAFT"],
              f"req={reqs[0][1] if reqs else None} sent={term_calls[0]['parts'] if term_calls else None}")
        pg.click("#afb-apply")
        pg.wait_for_function("() => /AI 已写入/.test(document.querySelector('#toast')?.textContent || '')", timeout=30000)
        plan2, etag2 = be.plan(pid)
        term = next((t["term"] for t in plan2["terms"] if t["selector"] == "PGB-MOTOR-SHAFT"), "")
        ui = page_state(pg, "st.state.planEtag")
        check("AI 起名 · 写入走 apply=true + If-Match，只填空白，前端以服务端新 ETag 为准",
              len(reqs) >= 2 and reqs[1][1].get("apply") is True and bool(reqs[1][0]) and term == "电机轴" and ui == etag2,
              f"ifMatch={'有' if len(reqs) > 1 and reqs[1][0] else '无'} term={term!r} etag同步={ui == etag2}")
        # 409：预览后、写入前计划在别处被改
        page_state(pg, "st.setSelection(['PGB-OIL-SEAL'])")
        pg.wait_for_selector("#afb-name", timeout=5000)
        pg.click("#afb-name")
        pg.wait_for_selector("#dlg-outbound[open]", timeout=10000)
        pg.click("#ob-ok")
        pg.wait_for_selector("#afb-apply", timeout=30000)
        p3, e3 = be.plan(pid)
        p3["layout"]["max_labels_per_figure"] = 19
        be.rest("PUT", f"/v1/projects/{pid}/plan", p3, {"If-Match": e3})
        pg.click("#afb-apply")
        pg.wait_for_function("() => /ETag 不匹配/.test(document.querySelector('#toast')?.textContent || '')", timeout=30000)
        save = pg.eval_on_selector("#tb-save", "e => e.textContent")
        p4, _ = be.plan(pid)
        still_blank = not next((t["term"] for t in p4["terms"] if t["selector"] == "PGB-OIL-SEAL"), "")
        check("AI 起名 · apply=true 时 ETag 不符 → 409：不写入、提示重载", still_blank and "冲突" in save,
              f"tbSave={save!r} blank={still_blank}")
    finally:
        be.reset_example_plan("gearbox")
        w.zero(name)
        ctx.close()


def sse_reconnect(browser, be):
    name = "sse_reconnect"
    pid = "example-gearbox"
    be.reset_example_plan("gearbox")
    ctx = new_ctx(browser, be)
    pg, w = page(ctx, allow=(503,))
    try:
        calls = []
        frame = 'retry: 300\nid: 1\nevent: stage\ndata: {"stage": "running", "progress": 0.0, "data": "running"}\n\n'

        def handle(route):
            n = len(calls)
            calls.append({"url": route.request.url, "lei": route.request.headers.get("last-event-id")})
            if n == 0:      # ① 流中途结束（只给了第 1 帧）→ EventSource 自己带 Last-Event-ID 重连
                route.fulfill(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}, body=frame)
            elif n == 1:    # ② 重连撞上非 200 → EventSource 进入 CLOSED，只能由前端按 ?last_event_id= 手动重连
                route.fulfill(status=503, headers={"Content-Type": "text/plain"}, body="unavailable")
            else:
                route.continue_()
        pg.route("**/v1/jobs/*/events*", handle)
        pg.goto(f"{be.base}/p/{pid}/?stage=qa", wait_until="networkidle")
        studio_ready(pg)
        prev = click_render(pg)
        wait_render_terminal(pg, prev)
        st = page_state(pg, "{ job: st.state.renderJob && st.state.renderJob.state, ver: st.state.renderInputs.version,"
                            " id: st.state.renderJob && st.state.renderJob.id }")
        gate = pg.eval_on_selector(".qa-gate", "e => e.textContent") if pg.query_selector(".qa-gate") else ""
        c1 = calls[1] if len(calls) > 1 else {}
        c2 = calls[2] if len(calls) > 2 else {}
        check("SSE 断线重连：中途断流 → 浏览器带 Last-Event-ID 重连；遇非 200 → 前端按 ?last_event_id= 续接；终态正确、版本即本作业",
              len(calls) >= 3 and c1.get("lei") == "1" and "last_event_id=1" in c2.get("url", "")
              and st["job"] == "succeeded" and st["ver"] == st["id"] and "13 项全过" in gate,
              f"calls={len(calls)} lei={c1.get('lei')} third={c2.get('url', '')[-40:]} job={st['job']}")
    finally:
        w.zero(name)
        ctx.close()


def refresh_resume(browser, be):
    """后端已有在跑的渲染作业时打开（刷新）页面。示例模型带几何缓存时一次出图只要 1–2 秒，来不及开页面——
    夹具经 REST 连续提交 4 个渲染作业（单个常驻 worker 串行执行），最后一个在页面打开时必然还在排队 / 运行。"""
    name = "refresh_resume"
    pid = "example-gearbox"
    be.reset_example_plan("gearbox")
    ctx = new_ctx(browser, be)
    pg, w = page(ctx)
    try:
        jobs = [be.rest("POST", f"/v1/projects/{pid}/render")[1]["job_id"] for _ in range(4)]
        last = jobs[-1]
        t0 = time.time()
        pg.goto(f"{be.base}/p/{pid}/?stage=qa", wait_until="domcontentloaded")
        studio_ready(pg)
        mid = page_state(pg, "{ job: st.state.renderJob && st.state.renderJob.state, id: st.state.renderJob && st.state.renderJob.id,"
                             " busy: !!document.querySelector('.stamp-run .spin, #rerender[disabled]'),"
                             " enabled: !!document.querySelector('#qa-render, #rerender:not([disabled]), #qa-retry') }")
        t_mid = time.time() - t0
        wait_render_terminal(pg)
        end = page_state(pg, "{ job: st.state.renderJob.state, id: st.state.renderJob.id, ver: st.state.renderInputs.version,"
                             " ev: st.state.renderInputs.etag, plan: st.state.planEtag, tabs: document.querySelectorAll('.qa-tab').length }")
        check("刷新恢复：后端已有在跑的渲染作业 → 打开页面接上、期间禁用再次渲染、终态自动呈现（版本 = 该作业）",
              mid["job"] in ("running", "queued") and mid["id"] == last and mid["busy"] and not mid["enabled"]
              and end["job"] == "succeeded" and end["id"] == last and end["ver"] == last and end["ev"] == end["plan"]
              and end["tabs"] > 0,
              f"打开时 {t_mid:.1f}s mid={mid} end={{job:{end['job']}, ver==last:{end['ver'] == last}}}")
    finally:
        for j in jobs[:-1]:
            try:
                be.wait_job(j, 120)
            except Exception:
                pass
        w.zero(name)
        ctx.close()


def etag_409(browser, be):
    name = "etag_409"
    pid = "example-gearbox"
    be.reset_example_plan("gearbox")
    ctx = new_ctx(browser, be)
    pg, w = page(ctx, allow=(409,))
    try:
        pg.goto(f"{be.base}/p/{pid}/?stage=plan", wait_until="networkidle")
        studio_ready(pg)
        p, e = be.plan(pid)                          # 别处（另一个标签 / Agent）改了计划
        p["layout"]["max_labels_per_figure"] = 18
        be.rest("PUT", f"/v1/projects/{pid}/plan", p, {"If-Match": e})
        pg.click('.rtab[data-tab="terms"]')
        inp = pg.query_selector_all("#terms .term-input")[0]
        inp.fill("端盖甲")
        inp.press("Tab")
        pg.wait_for_function("() => /冲突/.test(document.querySelector('#tb-save')?.textContent || '')", timeout=15000)
        save = pg.eval_on_selector("#tb-save", "e => e.textContent")
        reload_btn = pg.query_selector("#tb-reload") is not None
        p2, _ = be.plan(pid)
        kept = p2["layout"]["max_labels_per_figure"] == 18 and p2["terms"][0]["term"] != "端盖甲"
        check("ETag 409：别处已改计划 → 本页保存 409 → 顶栏「plan 冲突，请重载」+ 重载按钮，服务端不被覆盖",
              "冲突" in save and reload_btn and kept, f"tbSave={save!r} reload={reload_btn} serverKept={kept}")
        pg.click("#tb-reload")
        pg.wait_for_load_state("networkidle")
        studio_ready(pg)
        etag_ok = page_state(pg, "st.state.planEtag") == be.plan(pid)[1]
        # 接受候选也带 If-Match：页面打开后计划被别处改 → 409
        pg.wait_for_selector(".cand-card.pass [data-accept]", timeout=30000)
        p, e = be.plan(pid)
        p["layout"]["max_labels_per_figure"] = 17
        be.rest("PUT", f"/v1/projects/{pid}/plan", p, {"If-Match": e})
        n0 = len(be.plan(pid)[0]["figures"])
        pg.click(".cand-card.pass [data-accept]")
        pg.wait_for_function("() => /ETag 不匹配/.test(document.querySelector('#toast')?.textContent || '')", timeout=20000)
        n1 = len(be.plan(pid)[0]["figures"])
        check("ETag 409：接受候选带 If-Match，计划已被改 → 409 不写入、提示重载；重载后 ETag 与服务端一致",
              etag_ok and n0 == n1, f"reloadEtagOk={etag_ok} figs={n0}->{n1}")
    finally:
        be.reset_example_plan("gearbox")
        w.zero(name)
        ctx.close()


def cookie_only(browser, be):
    name = "cookie_only"
    pid = "example-gearbox"
    ctx = new_ctx(browser, be)
    pg, w = page(ctx)
    try:
        bad = []
        pg.on("response", lambda r: bad.append((r.status, r.url)) if r.status in (401, 403) else None)
        tok_in_req = []
        pg.on("request", lambda r: tok_in_req.append(r.url) if "token=" in r.url or r.headers.get("authorization") else None)
        pg.goto(f"{be.base}/p/{pid}/", wait_until="networkidle")
        studio_ready(pg)
        title = pg.eval_on_selector("#tb-proj", "e => e.textContent")
        pg.click('.stage-tab[data-stage="qa"]')
        pg.wait_for_timeout(1500)
        check("Cookie-only 新标签直接进入 /p/{pid}/：零 401、请求里不带 token（无 URL token / 无 Authorization 头）",
              "行星齿轮减速器" in title and not bad and not tok_in_req, f"401={bad[:2]} tokenReqs={tok_in_req[:2]}")
    finally:
        w.zero(name + "·有会话")
        ctx.close()
    ctx = new_ctx(browser, be, bootstrap=False)
    pg, w = page(ctx, allow=(401,))
    try:
        pg.goto(f"{be.base}/p/{pid}/", wait_until="networkidle")
        pg.wait_for_timeout(800)
        fatal = pg.evaluate("() => document.querySelector('.fatal')?.textContent || ''")
        pg.goto(f"{be.base}/", wait_until="networkidle")
        pg.wait_for_timeout(800)
        home = pg.evaluate("() => document.querySelector('#auth-required')?.textContent || ''")
        check("无会话：子应用与首页都给「需要 token，从终端完整地址进入」引导（不是「连接失败」）",
              "需要 token" in fatal and "需要 token" in home and "完整地址" in home, f"fatal={fatal[:30]!r}")
    finally:
        w.zero(name + "·无会话")
        ctx.close()
    ctx = new_ctx(browser, be, bootstrap=False)
    pg, w = page(ctx)
    try:
        deps = []
        pg.on("response", lambda r: deps.append(r.url) if r.headers.get("deprecation") == "true" else None)
        pg.goto(f"{be.base}/p/{pid}/?token={TOKEN}", wait_until="networkidle")
        studio_ready(pg)
        notice = pg.evaluate("() => document.querySelector('#auth-notice')?.textContent || ''")
        url_clean = "token=" not in pg.url
        pg2 = ctx.new_page()
        pg2.goto(f"{be.base}/p/{pid}/", wait_until="networkidle")
        studio_ready(pg2)
        ok2 = "行星齿轮减速器" in pg2.eval_on_selector("#tb-proj", "e => e.textContent")
        pg2.close()
        check("旧式 ?token= 链接：据 Deprecation 头提示弃用、换成会话 Cookie、抹掉地址栏 token，之后 Cookie-only 新标签可进",
              deps and "弃用" in notice and url_clean and ok2, f"deprecationResp={len(deps)} notice={notice[:24]!r} clean={url_clean} tab2={ok2}")
    finally:
        w.zero(name + "·旧链接")
        ctx.close()


def dwg_conservative_note(browser, be):
    name = "dwg_conservative_note"
    pid = "example-gearbox"
    st, proj, _ = be.rest("GET", f"/v1/projects/{pid}")
    plan, etag = be.plan(pid)
    # 导出屏需要「已发布且对应当前计划」的结果：当前计划没出过图就先出一次（夹具）
    _, status, _ = be.rest("GET", f"/p/{pid}/api/render/status")
    if not status.get("version"):
        _, j, _ = be.rest("POST", f"/v1/projects/{pid}/render")
        be.wait_job(j["job_id"])
    ctx = new_ctx(browser, be)
    pg, w = page(ctx)
    try:
        def patch(route):
            r = route.fetch()
            j = r.json()
            j["export"] = dict(j["export"], dwg_available=False, dwg_route=None,
                               unavailable_reasons=dict(j["export"].get("unavailable_reasons") or {},
                                                        dwg="未检测到 DWG 转换器（AutoCAD Core Console 或 LibreDWG）"))
            route.fulfill(response=r, body=json.dumps(j, ensure_ascii=False), headers={**r.headers, "content-type": "application/json"})
        pg.route("**/v1/capabilities*", patch)
        pg.goto(f"{be.base}/p/{pid}/?stage=qa", wait_until="networkidle")
        studio_ready(pg)
        gate = pg.eval_on_selector(".qa-gate", "e => e.textContent") if pg.query_selector(".qa-gate") else ""
        if "13 项全过" not in gate or "过期" in gate:
            prev = click_render(pg)
            wait_render_terminal(pg, prev)
        stage(pg, "export")
        pg.wait_for_selector("#do-pack", timeout=20000)
        dwg_disabled = pg.locator('[data-fmt="dwg"]').is_disabled()
        why = pg.eval_on_selector('[data-why="dwg"]', "e => e.textContent") if pg.query_selector('[data-why="dwg"]') else ""
        note = pg.eval_on_selector("#cnipa-note", "e => e.textContent") if pg.query_selector("#cnipa-note") else ""
        _, caps, _ = be.rest("GET", "/v1/capabilities")
        pg.click('[data-preset="conservative"]')
        pg.wait_for_timeout(200)
        hint = pg.eval_on_selector("#preset-hint", "e => e.textContent") if pg.query_selector("#preset-hint") else ""
        check("能力 dwg_available=false（注入）→ DWG 事前禁选并给原因", dwg_disabled and "未检测到 DWG" in why,
              f"why={why!r}")
        check("导出屏：CNIPA 旁注取能力接口 export.note 原文；保守版心给字高提示（WP2 §6.2）",
              note.strip() == caps["export"]["note"].strip() and "保守版心" in hint and "字高" in hint,
              f"note={'一致' if note.strip() == caps['export']['note'].strip() else note[:30]} hint={hint[:30]!r}")
    finally:
        w.zero(name)
        ctx.close()


def pdf_unverified(browser, be):
    name = "pdf_unverified"
    pid = "example-valve"
    be.ensure_example("valve")
    be.reset_example_plan("valve")
    ctx = new_ctx(browser, be)
    pg, w = page(ctx)
    try:
        pg.goto(f"{be.base}/p/{pid}/?stage=qa", wait_until="networkidle")
        studio_ready(pg)
        prev = click_render(pg)
        wait_render_terminal(pg, prev)
        stage(pg, "export")
        pg.wait_for_selector("#do-pack", timeout=20000)
        pre = pg.eval_on_selector('[data-why="pdf"]', "e => e.textContent") if pg.query_selector('[data-why="pdf"]') else ""
        pick_formats(pg, {"pdf": True, "docx": True, "dxf": False, "dwg": False})
        pg.click("#do-pack")
        pg.wait_for_selector(".exp-done", timeout=600000)
        bad = pg.eval_on_selector(".exp-bad", "e => e.textContent.replace(/\\s+/g,' ')") if pg.query_selector(".exp-bad") else ""
        rows = pg.eval_on_selector_all(".exp-items tbody tr", "els => els.map(e => [e.dataset.item, e.className, e.textContent.replace(/\\s+/g,' ')])")
        pdf_rows = [r for r in rows if r[0].endswith("/pdf")]
        docx_rows = [r for r in rows if r[0].endswith("/docx")]
        href = pg.get_attribute(".exp-zip-acts a[download]", "href")
        st_dl, blob = download_zip(ctx, href)
        export_id = re.search(r"/v1/exports/([^/]+)/download", href).group(1)
        _, job, _ = be.rest("GET", f"/v1/exports/{export_id}")
        z = verify_zip(blob, job.get("result") or {})
        check("缺 pypdfium2：导出前提示；PDF 项「未核验」并写明原因、不报完成；Word 照常通过；PDF 在包内 _未通过核验/",
              "未核验" in pre and pdf_rows and all("unverified" in r[1] and "未核验" in r[2] and "pypdfium2" in r[2] for r in pdf_rows)
              and docx_rows and all(r[1].strip() == "ok" for r in docx_rows) and "未全部通过" in bad
              and any(n.endswith(".pdf") for n in z["quarantine"]) and st_dl == 200,
              f"pre={pre[:24]!r} pdf={[r[2][:40] for r in pdf_rows][:1]} quarantine={z['quarantine'][:2]}")
    finally:
        be.reset_example_plan("valve")
        w.zero(name)
        ctx.close()


CASES = ["first_run_example", "golden_gearbox", "golden_vise", "golden_valve", "empty_candidates", "vise_blocked_fix",
         "ai_not_configured", "sse_reconnect", "refresh_resume", "etag_409", "cookie_only",
         "dwg_conservative_note", "ai_configured", "pdf_unverified"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", default="", help="逗号分隔的用例名")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    only = [c for c in args.only.split(",") if c] or CASES
    main_be = Backend("main", 8806, HOME / ".patent-workbench-wp3b", HOME / ".patent-workbench-wp3b-config",
                      {"provider": "none"})
    ai_be = Backend("ai", 8811, HOME / ".patent-workbench-wp3b-ai", HOME / ".patent-workbench-wp3b-ai-config",
                    {"provider": "deepseek", "deepseek": {"api_key": "e2e-fake-key-0000",
                                                          "base_url": "http://127.0.0.1:%d" % FAKE_LLM_PORT,
                                                          "model": "e2e-fake"}})
    nopdf_be = Backend("nopdf", 8813, HOME / ".patent-workbench-wp3b-nopdf", HOME / ".patent-workbench-wp3b-nopdf-config",
                       {"provider": "none"}, {"PFK_PDF_RENDERER": "none"})
    fresh_be = Backend("fresh", 8814, SCRATCH / ("fresh-%d" % int(time.time())), SCRATCH / "fresh-config",
                       {"provider": "none"}, serve_args=["--no-example"])
    need = {"main": True, "ai": "ai_configured" in only, "nopdf": "pdf_unverified" in only,
            "fresh": "first_run_example" in only}
    fake = start_fake_llm() if need["ai"] else None
    started = []
    t_all = time.time()
    try:
        for key, be in (("main", main_be), ("ai", ai_be), ("nopdf", nopdf_be), ("fresh", fresh_be)):
            if need[key]:
                be.up()
                started.append(be)
        log(f"e2e_live · token 已隔离 · 用例 {len(only)} 个")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            for case in only:
                log(f"\n▶ {case}")
                t0 = time.time()
                try:
                    if case.startswith("golden_"):
                        golden(browser, main_be, case.split("_", 1)[1])
                    elif case == "ai_configured":
                        ai_configured(browser, ai_be)
                    elif case == "pdf_unverified":
                        pdf_unverified(browser, nopdf_be)
                    elif case == "first_run_example":
                        first_run_example(browser, fresh_be)
                    else:
                        globals()[case](browser, main_be)
                except Exception as exc:  # noqa: BLE001
                    import traceback
                    tb = traceback.format_exc()
                    (SCRATCH / f"traceback-{case}.txt").write_text(tb, encoding="utf-8")
                    check(case, False, f"异常 {exc!r}"[:600] + f"（traceback 见 {SCRATCH}/traceback-{case}.txt）")
                log(f"  ({time.time() - t0:.1f}s)")
            browser.close()
    finally:
        for be in started:
            be.down()
        if fake:
            fake.shutdown()
    passed = sum(1 for r in RESULTS if r[1])
    log("\n实测耗时（秒）：" + json.dumps(TIMINGS, ensure_ascii=False))
    log(f"\n{'全部通过' if passed == len(RESULTS) else '有失败'}：{passed}/{len(RESULTS)}（{len(only)} 个用例，"
        f"{time.time() - t_all:.0f}s）")
    sys.exit(0 if passed == len(RESULTS) and RESULTS else 1)


if __name__ == "__main__":
    main()
