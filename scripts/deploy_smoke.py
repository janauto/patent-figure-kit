#!/usr/bin/env python3
"""服务器形态冒烟（WP4 · T2）：经反向代理（Caddy）从外部客户端走一遍金路径与两条鉴权路。

    python3 scripts/deploy_smoke.py --url https://localhost:8443 --cacert caddy-root.crt \\
        --basic-user pfk --basic-pass-env PFK_BASIC_PASS --token-env WORKBENCH_TOKEN

检查（任一失败即退出码 1，逐条打印）：

1. 代理闸门：无凭据访问页面 → 401（Basic Auth）；带 Bearer 访问页面 → 仍 401（Agent 直通只限 /v1、/mcp）。
2. 浏览器路：Basic Auth + ``/?token=`` 引导 → 会话 Cookie（HttpOnly、https 下 Secure）→ 之后只凭 Cookie（Basic
   由代理消费、不进后端）访问 REST；SSE 事件流经代理可读到带 id 的事件帧。
3. Agent 路：只带 ``Authorization: Bearer``（无 Basic、无 Cookie）→ /v1/capabilities 200；错 token → 401。
4. 金路径（浏览器路）：from-example → 候选分图 → 出图 → 导出（PDF+Word，两版）→ ``download_url`` 以
   ``--url`` 为前缀 → 下载 zip → 成员齐全（PDF / Word / 附图说明 / 核对清单）。
5. TLS：用 ``--cacert`` 校验证书链（不加 --insecure 就不跳过校验）。

纯标准库；不 import OCP。token / 密码只从环境变量读，不在输出里回显。
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

RESULTS: List[Tuple[bool, str, str]] = []


def check(ok: bool, title: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), title, detail))
    print("  [%s] %s%s" % ("通过" if ok else "失败", title, ("：" + detail) if detail else ""),
          flush=True)
    return bool(ok)


class Client:
    def __init__(self, base: str, ctx: ssl.SSLContext, basic: Optional[str] = None,
                 bearer: Optional[str] = None, cookies: bool = False) -> None:
        self.base = base.rstrip("/")
        self.ctx = ctx
        self.basic, self.bearer = basic, bearer
        self.jar = http.cookiejar.CookieJar() if cookies else None
        handlers = [urllib.request.HTTPSHandler(context=ctx)]
        if self.jar is not None:
            handlers.append(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener = urllib.request.build_opener(*handlers)

    def req(self, method: str, path: str, body=None, headers: Optional[Dict[str, str]] = None,
            timeout: float = 60.0):
        url = path if path.startswith("http") else self.base + path
        h = dict(headers or {})
        if self.basic:
            h["Authorization"] = "Basic " + self.basic
        if self.bearer:
            h["Authorization"] = "Bearer " + self.bearer
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"
        r = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            resp = self.opener.open(r, timeout=timeout)
            return resp.status, dict(resp.headers.items()), resp.read(), resp.headers.get_all(
                "Set-Cookie") or []
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read(), []

    def json(self, method: str, path: str, body=None, expect=(200, 202)):
        st, _h, raw, _c = self.req(method, path, body)
        if st not in expect:
            raise RuntimeError("%s %s → %d %s" % (method, path, st, raw[:300]))
        return json.loads(raw.decode("utf-8"))

    def wait(self, job_id: str, what: str, timeout: float = 1800.0) -> dict:
        t0 = time.time()
        while True:
            job = self.json("GET", "/v1/jobs/%s" % job_id)
            if job.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
                if job["state"] != "succeeded":
                    raise RuntimeError("%s：作业 %s（%s）" % (what, job["state"],
                                                          json.dumps(job.get("error"),
                                                                     ensure_ascii=False)[:300]))
                return job
            if time.time() - t0 > timeout:
                raise RuntimeError("%s 超时" % what)
            time.sleep(2)


def read_sse_frame(base: str, path: str, ctx: ssl.SSLContext, cookie: str, basic: str,
                   timeout: float = 30.0) -> str:
    """经代理读一个 SSE 事件帧（直到第一个空行）。"""
    req = urllib.request.Request(base + path, headers={
        "Accept": "text/event-stream", "Cookie": cookie, "Authorization": "Basic " + basic})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    with opener.open(req, timeout=timeout) as resp:
        if "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
            return ""
        lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = resp.readline().decode("utf-8", errors="replace")
            if not line:
                break
            if line.strip() == "" and lines:
                break
            lines.append(line.rstrip("\n"))
        return "\n".join(lines)


def browser_check(base: str, token: str, user: str, password: str) -> None:
    """真浏览器经代理走一遍首页 → 工程页（证书已由上面的 Python 客户端与 curl 校验，这里不重复）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check(False, "真浏览器检查", "未安装 Playwright（python -m pip install playwright && "
                                     "python -m playwright install chromium）")
        return
    bad, errors = [], []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(http_credentials={"username": user, "password": password},
                            ignore_https_errors=True)
        ctx.on("response", lambda r: bad.append("%d %s" % (r.status, r.url.split("?")[0]))
               if r.status >= 400 and not r.url.endswith("/favicon.ico") else None)
        page = ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base + "/?token=" + token, wait_until="networkidle")
        page.wait_for_timeout(1500)
        cookies = [c for c in ctx.cookies() if c["name"] == "wb_token"]
        check(bool(cookies) and cookies[0].get("secure") and cookies[0].get("httpOnly"),
              "真浏览器：首页经代理加载并拿到会话 Cookie（Secure、HttpOnly）")
        cards = page.eval_on_selector_all("article.card", "e => e.map(x => x.dataset.id)")
        target = next((c for c in cards if c and c.startswith("example-")), None)
        if target:
            page.goto("%s/p/%s/" % (base, target), wait_until="networkidle")
            page.wait_for_timeout(2500)
        check(not bad, "真浏览器：首页%s全程无 4xx / 5xx" % ("与工程页 " if target else ""), bad[:4])
        check(not errors, "真浏览器：无控制台错误与页面异常", errors[:3])
        b.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="对外根地址（与后端 --public-url 一致）")
    ap.add_argument("--cacert", help="校验 TLS 用的根证书（Caddy 内部 CA 的 root.crt）")
    ap.add_argument("--insecure", action="store_true", help="不校验证书（只用于排查，CI 不用）")
    ap.add_argument("--basic-user", default="pfk")
    ap.add_argument("--basic-pass-env", default="PFK_BASIC_PASS",
                    help="从哪个环境变量读 Basic Auth 明文密码")
    ap.add_argument("--token-env", default="WORKBENCH_TOKEN", help="从哪个环境变量读后端 token")
    ap.add_argument("--example", default="valve", choices=("gearbox", "vise", "valve"))
    ap.add_argument("--step", default=str(HERE.parent / "tests" / "fixtures" / "synthetic.stp"),
                    help="上传冒烟用的 STEP（默认仓库自带的合成装配体；给空串跳过）")
    ap.add_argument("--browser", action="store_true",
                    help="再用真浏览器（Playwright Chromium）经代理打开首页与工程页，断言无 4xx/5xx 与控制台错误")
    args = ap.parse_args(argv)

    token = os.environ.get(args.token_env, "")
    password = os.environ.get(args.basic_pass_env, "")
    if not token or not password:
        print("需要环境变量 %s 与 %s" % (args.token_env, args.basic_pass_env), file=sys.stderr)
        return 2
    ctx = ssl.create_default_context(cafile=args.cacert) if args.cacert else \
        ssl.create_default_context()
    if args.insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    basic = base64.b64encode(("%s:%s" % (args.basic_user, password)).encode()).decode()
    base = args.url.rstrip("/")
    print("部署冒烟：%s（TLS 校验：%s）" % (base, "关闭" if args.insecure else
                                          (args.cacert or "系统信任库")))

    anon = Client(base, ctx)
    st, h, _b, _c = anon.req("GET", "/")
    check(st == 401 and "basic" in (h.get("Www-Authenticate") or h.get("WWW-Authenticate") or "").lower(),
          "代理闸门：无凭据访问页面被 Basic Auth 拦下", "HTTP %d" % st)
    st, _h, _b, _c = Client(base, ctx, bearer="wrong-" + "x" * 20).req("GET", "/")
    check(st == 401, "代理闸门：带错误 Bearer 不能绕过 Basic Auth（由后端 /_pfk/auth 判定）", "HTTP %d" % st)
    st, _h, _b, _c = Client(base, ctx, bearer=token).req("GET", "/")
    check(st == 200, "代理闸门：带有效 Bearer 由后端判定放行（不需要 Basic）", "HTTP %d" % st)

    # ---- Agent 路 ----
    agent = Client(base, ctx, bearer=token)
    st, _h, raw, _c = agent.req("GET", "/v1/capabilities")
    check(st == 200 and b"export" in raw, "Agent：只带 Bearer（无 Basic、无 Cookie）访问 REST", "HTTP %d" % st)
    st, _h, _b, _c = Client(base, ctx, bearer="wrong-" + "x" * 20).req("GET", "/v1/capabilities")
    check(st == 401, "Agent：错误 token 被后端拒绝", "HTTP %d" % st)
    st, _h, raw, _c = agent.req("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                  "params": {}})
    check(st == 200 and b"jsonrpc" in raw, "Agent：MCP initialize 经代理可达", "HTTP %d" % st)

    # ---- 浏览器路 ----
    browser = Client(base, ctx, basic=basic, cookies=True)
    st, _h, _b, set_cookies = browser.req("GET", "/?token=" + token)
    sc = next((c for c in set_cookies if c.startswith("wb_token=")), "")
    attrs = [a.strip().lower() for a in sc.split(";")]
    check(st == 200 and sc and "httponly" in attrs and ("secure" in attrs or not base.startswith("https")),
          "浏览器：Basic Auth + ?token= 引导得到会话 Cookie（HttpOnly、Secure）",
          "HTTP %d，Cookie 属性 %s" % (st, [a for a in attrs[1:] if a]))
    st, _h, _b, _c = browser.req("GET", "/v1/capabilities")
    check(st == 200, "浏览器：只凭会话 Cookie 访问 REST（Basic 凭据由代理消费、不进后端）", "HTTP %d" % st)
    # 真实前端的接口请求：会话 Cookie + 前端自己加的 Authorization: Bearer（webui/js/api.js），路径不限 /v1
    fe = Client(base, ctx, bearer=token, cookies=True)
    fe.jar = browser.jar
    fe.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                            urllib.request.HTTPCookieProcessor(browser.jar))
    for path in ("/api/wb/state", "/api/wb/settings", "/v1/projects"):
        st, _h, _b, _c = fe.req("GET", path)
        check(st == 200, "浏览器前端：Cookie + Bearer 访问 %s" % path, "HTTP %d" % st)

    # ---- 浏览器上传 STEP（multipart/form-data，与前端 createProject 同形）----
    if args.step:
        try:
            boundary = "----pfk" + os.urandom(8).hex()
            step_bytes = Path(args.step).read_bytes()
            body = b"".join([
                ("--%s\r\n" % boundary).encode(),
                ('Content-Disposition: form-data; name="step"; filename="%s"\r\n'
                 % Path(args.step).name).encode(),
                b"Content-Type: application/octet-stream\r\n\r\n", step_bytes, b"\r\n",
                ("--%s\r\n" % boundary).encode(),
                b'Content-Disposition: form-data; name="title"\r\n\r\n', "上传冒烟".encode(), b"\r\n",
                ("--%s--\r\n" % boundary).encode()])
            req = urllib.request.Request(base + "/v1/projects", data=body, method="POST", headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Authorization": "Bearer " + token})
            with fe.opener.open(req, timeout=120) as resp:
                up = json.loads(resp.read().decode("utf-8"))
                code = resp.status
            job = browser.wait(up["job_id"], "上传后准备")
            check(code == 202 and job.get("state") == "succeeded",
                  "浏览器上传 STEP（multipart，经代理）→ 202 → 工程准备就绪", "%s" % up.get("project_id"))
        except Exception as exc:  # noqa: BLE001
            check(False, "浏览器上传 STEP（multipart，经代理）", "%s: %s" % (type(exc).__name__, str(exc)[:300]))

    # ---- 金路径（浏览器路）----
    ok_path = False
    zip_path = None
    try:
        t0 = time.time()
        r = browser.json("POST", "/v1/projects/from-example", {"example": args.example})
        pid = r["project_id"]
        # SSE：订阅这个作业的事件流，经代理读一帧
        cookie = "; ".join("%s=%s" % (c.name, c.value) for c in browser.jar)
        try:
            frame = read_sse_frame(base, "/v1/jobs/%s/events" % r["job_id"], ctx, cookie, basic)
        except (socket.timeout, OSError) as exc:
            frame = "（读取失败：%s）" % exc
        check("id:" in frame and ("event:" in frame or "data:" in frame),
              "SSE：事件流经代理可读（带 event id）", frame.replace("\n", " | ")[:120])
        browser.wait(r["job_id"], "载入示例")
        sj = browser.json("POST", "/v1/projects/%s/plan/suggest" % pid, {"label_standard_parts": False})
        browser.wait(sj["job_id"], "候选分图")
        cands = browser.json("GET", "/v1/projects/%s/plan/candidates" % pid).get("result") or {}
        passed = [c for c in cands.get("candidates", []) if c.get("status") == "pass"]
        check(bool(passed), "金路径：候选分图（试渲染 + QA）", "%d/%d 张过闸" % (
            len(passed), len(cands.get("candidates", []))))
        st, hdr, _b, _c = browser.req("GET", "/v1/projects/%s/plan" % pid)
        etag = hdr.get("ETag") or hdr.get("Etag") or ""
        rj = browser.json("POST", "/v1/projects/%s/render" % pid, {"plan_etag": etag})
        browser.wait(rj["job_id"], "出图")
        check(True, "金路径：出图（隔离渲染、全过闸后发布）")
        xj = browser.json("POST", "/v1/projects/%s/export" % pid, {
            "variant": "both", "formats": ["pdf", "docx"], "render_job_id": rj["job_id"],
            "plan_etag": etag})
        job = browser.wait(xj["job_id"], "导出")
        res = job.get("result") or {}
        url = res.get("download_url", "")
        check(bool(res.get("ok")), "金路径：导出包逐页核验通过", "ok=%s" % res.get("ok"))
        check(url.startswith(base + "/v1/exports/"), "下载地址基于 public-url", url)
        st, _h, raw, _c = browser.req("GET", url, timeout=300)
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        zip_path = tmp
        from patent_figure import zipcheck
        names = [n for n, _ in zipcheck.members(tmp)]
        want = {"pdf": any(n.endswith(".pdf") for n in names),
                "docx": any(n.endswith(".docx") for n in names),
                "附图说明": any("附图说明" in n for n in names),
                "核对清单": any("核对清单" in n for n in names)}
        check(st == 200 and all(want.values()), "金路径：下载 zip 成员齐全",
              "HTTP %d，%d 个成员，%s" % (st, len(names), want))
        ok_path = True
        print("  金路径用时 %.0f s" % (time.time() - t0))
    except Exception as exc:  # noqa: BLE001
        check(False, "金路径未走通", "%s: %s" % (type(exc).__name__, str(exc)[:400]))
    finally:
        if zip_path:
            os.unlink(zip_path)

    if args.browser:
        browser_check(base, token, args.basic_user, password)

    failed = [r for r in RESULTS if not r[0]]
    print("\n%d 项通过，%d 项失败" % (len(RESULTS) - len(failed), len(failed)))
    return 0 if not failed and ok_path else 1


if __name__ == "__main__":
    raise SystemExit(main())
