#!/usr/bin/env python3
"""T0 静态站子路径验证（WP4）：在**不做任何路由重写**的静态服务器的子路径下，用真浏览器逐项实测。

    python3 scripts/verify_static_site.py static-site            # 站点在 static-site/patent-figure-kit/
    python3 scripts/verify_static_site.py 目录 --site-name 子路径名 [--port 8817] [--shots 截图目录]

服务器：``python -m http.server``（只会按路径找文件，与 GitHub Pages 同样没有路由重写），站点挂在
``/<site-name>/`` 子路径下。浏览器：Playwright Chromium（``pip install playwright && playwright install chromium``）。

断言（任一失败即退出码 1）：

1. 子路径根 ``/<site-name>/`` 落到首页；页面有「预计算展示；无需上传自有模型」字样；三个公开示例卡片齐全。
2. 每个示例进规划台：GLB 解码成功（three.js 视图里的零件数 = manifest 里装配体的零件种数，且快照非空白）；
   出图与 QA 屏的预览 SVG 内联成功且带 ``text[data-numeral]``；导出屏「打包」按钮禁用（只读）。
3. 样例页三个导出包都能下载，字节哈希与 manifest 一致，包内有 PDF / Word / 附图说明 / 核对清单。
4. 全程网络：**零写请求**（只有 GET）、**零后端请求**（无 /v1、/api、/p/、/files、/mcp）、全部同源且都在
   ``/<site-name>/`` 之下（浏览器自动请求的 /favicon.ico 除外，单列）、无 4xx/5xx；无控制台错误、无页面异常。
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import socket
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

BANNER_TEXT = "预计算展示；无需上传自有模型"
BACKEND_PREFIXES = ("/v1/", "/api/", "/p/", "/files/", "/mcp", "/static/")
RESULTS = []


def check(ok, title, detail=""):
    RESULTS.append((bool(ok), title, detail))
    print("  [%s] %s%s" % ("通过" if ok else "失败", title, ("：" + str(detail)) if detail else ""),
          flush=True)
    return bool(ok)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401 — 安静
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(root: Path, port: int):
    handler = functools.partial(_Quiet, directory=str(root))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path, help="站点的父目录（build_static_site.py 的 -o）")
    ap.add_argument("--site-name", default="patent-figure-kit")
    ap.add_argument("--port", type=int, default=0, help="静态服务器端口（默认随机空闲端口）")
    ap.add_argument("--shots", type=Path, help="把每屏截图存到这个目录（可选）")
    args = ap.parse_args(argv)
    site = args.root / args.site_name
    manifest = json.loads((site / "dev" / "manifest.json").read_text(encoding="utf-8"))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 Playwright：python3 -m pip install playwright && python3 -m playwright install chromium",
              file=sys.stderr)
        return 2
    port = args.port or _free_port()
    httpd = serve(args.root, port)
    origin = "http://127.0.0.1:%d" % port
    sub = "/%s/" % args.site_name
    print("静态站子路径验证：%s%s（python -m http.server，无路由重写）" % (origin, sub))
    if args.shots:
        args.shots.mkdir(parents=True, exist_ok=True)

    requests, failures, console_errors, page_errors = [], [], [], []
    from patent_figure import zipcheck
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
            ctx.on("request", lambda r: requests.append((r.method, r.url, r.resource_type)))
            ctx.on("response", lambda r: failures.append((r.status, r.url)) if r.status >= 400 else None)
            page = ctx.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)))

            # 1) 子路径根 → 首页
            page.goto(origin + sub, wait_until="networkidle")
            page.wait_for_timeout(800)
            check(page.url.split("?")[0].endswith(sub + "home.html"), "子路径根落到首页", page.url)
            body = page.inner_text("body")
            check(BANNER_TEXT in body, "页面显式文案「%s」" % BANNER_TEXT)
            titles = [pr["summary"]["title"] for pr in manifest["projects"]]
            cards = page.eval_on_selector_all("article.card .card-ttl", "els => els.map(e => e.textContent)")
            check(all(t in cards for t in titles), "三个公开示例卡片齐全", cards)
            if args.shots:
                page.screenshot(path=str(args.shots / "00-home.png"))

            # 2) 每个示例：GLB 解码、预览、导出只读
            for pr in manifest["projects"]:
                pid = pr["id"]
                want_parts = len([x for x in (pr["detail"]["assembly"] or {}).get("parts", [])
                                  if not x.get("degenerate")])
                page.goto("%s%sindex.html?project=%s" % (origin, sub, pid), wait_until="networkidle")
                try:
                    page.wait_for_function("() => window.planStudioViewer && "
                                           "window.planStudioViewer.partCount > 0", timeout=20000)
                    parts = page.evaluate("() => window.planStudioViewer.partCount")
                    blank = page.evaluate("""async () => {
                        const url = window.planStudioViewer.snapshot(320, 320);
                        const img = new Image(); img.src = url; await img.decode();
                        const c = document.createElement('canvas'); c.width = 320; c.height = 320;
                        const g = c.getContext('2d'); g.drawImage(img, 0, 0);
                        const d = g.getImageData(0, 0, 320, 320).data; let ink = 0;
                        for (let i = 0; i < d.length; i += 4) {
                          if (Math.abs(d[i] - 0xfb) + Math.abs(d[i+1] - 0xfa) + Math.abs(d[i+2] - 0xf5) > 30) ink++;
                        }
                        return ink;
                    }""")
                except Exception as exc:  # noqa: BLE001
                    parts, blank = 0, 0
                    check(False, "%s：GLB 解码" % pid, exc)
                check(parts == want_parts and blank > 500, "%s：GLB 解码并渲染（零件 %s/%s，非底色像素 %s）"
                      % (pid, parts, want_parts, blank))
                if args.shots:
                    page.screenshot(path=str(args.shots / ("%s-plan.png" % pid)))
                page.goto("%s%sindex.html?project=%s&stage=qa" % (origin, sub, pid), wait_until="networkidle")
                try:
                    page.wait_for_selector("[data-pane] svg text[data-numeral]", timeout=15000)
                    n = page.eval_on_selector_all("[data-pane] svg text[data-numeral]", "e => e.length")
                except Exception as exc:  # noqa: BLE001
                    n = 0
                    check(False, "%s：预览 SVG" % pid, exc)
                check(n > 0, "%s：出图与 QA 屏预览 SVG 内联（%d 个附图标记）" % (pid, n))
                if args.shots:
                    page.screenshot(path=str(args.shots / ("%s-qa.png" % pid)))
                page.goto("%s%sindex.html?project=%s&stage=export" % (origin, sub, pid),
                          wait_until="networkidle")
                page.wait_for_timeout(500)
                el = page.query_selector("#do-pack")
                check(el is None or el.is_disabled(), "%s：导出屏打包按钮禁用（只读）" % pid)

            # 3) 样例页下载
            page.goto(origin + sub + "samples.html", wait_until="networkidle")
            check(BANNER_TEXT in page.inner_text("body"), "样例页有「%s」" % BANNER_TEXT)
            with tempfile.TemporaryDirectory() as tmp:
                for s in manifest["samples"]:
                    with page.expect_download() as dl:
                        page.click('a[href="%s"]' % s["path"])
                    dest = Path(tmp) / Path(s["path"]).name
                    dl.value.save_as(str(dest))
                    data = dest.read_bytes()
                    names = [m for m, _ in zipcheck.members(dest)]
                    has = {k: any(k in m for m in names) for k in (".pdf", ".docx", "附图说明", "核对清单")}
                    check(hashlib.sha256(data).hexdigest() == s["sha256"] and all(has.values()),
                          "样例下载 %s（%.1f MB，哈希一致，成员齐全）" % (Path(s["path"]).name,
                                                                   len(data) / 1048576), has)
            browser.close()
    finally:
        httpd.shutdown()

    # 4) 网络与控制台
    writes = [(m, u) for m, u, _t in requests if m not in ("GET", "HEAD")]
    paths = [(m, urlsplit(u)) for m, u, _t in requests if not u.startswith(("data:", "blob:"))]
    foreign = [u.geturl() for _m, u in paths if "%s://%s" % (u.scheme, u.netloc) != origin]
    favicon = [u.geturl() for _m, u in paths if u.path == "/favicon.ico"]
    outside = [u.geturl() for _m, u in paths
               if not u.path.startswith(sub) and u.path != "/favicon.ico"]
    backend = [u.geturl() for _m, u in paths if any(u.path.startswith(sub.rstrip("/") + b) or
                                                     u.path.startswith(b) for b in BACKEND_PREFIXES)]
    bad = [(s, u) for s, u in failures if urlsplit(u).path != "/favicon.ico"]
    errs = [e for e in console_errors if "favicon" not in e]
    check(not writes, "零写请求（全程 %d 个请求均为 GET）" % len(requests), writes[:3])
    check(not backend, "零后端请求（无 /v1 /api /p /files /mcp /static）", backend[:3])
    check(not foreign, "全部同源（无外部 CDN / 跟踪）", foreign[:3])
    check(not outside, "全部在子路径 %s 之下（相对 URL）" % sub, outside[:3])
    check(not bad, "无 4xx / 5xx", bad[:3])
    check(not errs and not page_errors, "无控制台错误与页面异常", (errs + page_errors)[:3])
    if favicon:
        print("  （浏览器自动请求了 /favicon.ico %d 次，不在站点内，GitHub Pages 上同样 404，不影响）"
              % len(favicon))
    failed = [r for r in RESULTS if not r[0]]
    print("\n%d 项通过，%d 项失败；请求 %d 个" % (len(RESULTS) - len(failed), len(failed), len(requests)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
