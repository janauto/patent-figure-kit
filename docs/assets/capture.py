#!/usr/bin/env python3
"""重新生成 README 用的界面截图。

    python3 docs/assets/capture.py

做法：用全新的临时数据目录与临时配置目录启动一个 Workbench 实例（只含合成示例工程，
不读取、不写入 ~/.patent-workbench 与 ~/.config/patent-workbench），用 Playwright 截图后关闭。
需要：pip install playwright，以及本机 Chrome（找不到时改用 `playwright install chromium`）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PORT, TOKEN = 8795, "readme-capture"
BASE = f"http://127.0.0.1:{PORT}"
GL_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


def wait_ready(timeout: float = 90) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"{BASE}/?token={TOKEN}", timeout=2)
            return
        except OSError:
            time.sleep(1)
    raise SystemExit("Workbench 没有在预期时间内启动")


def launch(p):
    try:
        return p.chromium.launch(channel="chrome", args=GL_ARGS)
    except Exception:  # 没装 Chrome：退回 Playwright 自带的 Chromium
        return p.chromium.launch(args=GL_ARGS)


def main() -> int:
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, PATENT_WORKBENCH_CONFIG=f"{tmp}/cfg", WORKBENCH_TOKEN=TOKEN)
        srv = subprocess.Popen(
            [sys.executable, str(REPO / "scripts/workbench.py"), "--data", f"{tmp}/data",
             "serve", "--no-browser", "--port", str(PORT)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_ready()
            with sync_playwright() as p:
                browser = launch(p)
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(f"{BASE}/?token={TOKEN}", wait_until="networkidle")
                page.wait_for_timeout(2500)
                page.screenshot(path=str(OUT / "shot-home.png"))
                page.goto(f"{BASE}/p/example-synthetic/?token={TOKEN}", wait_until="networkidle")
                page.wait_for_timeout(6000)  # 等 3D 模型加载与首轮渲染
                page.screenshot(path=str(OUT / "shot-studio.png"))
                browser.close()
        finally:
            srv.terminate()
            srv.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
