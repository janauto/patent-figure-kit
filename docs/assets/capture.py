#!/usr/bin/env python3
"""重新生成 README 用的界面截图。

    python3 docs/assets/capture.py

做法：用全新的临时数据目录与临时配置目录启动一个 Workbench 实例，
用 examples/planetary-gearbox 的公开演示装配体建一个示例工程（与任何真实产品无关），
不读取、不写入 ~/.patent-workbench 与 ~/.config/patent-workbench，也不调用任何大模型，用 Playwright 截图后关闭。
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
PROJECT = "example-gearbox"
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
        # 显式关闭 AI 通道：不然建工程时会自动调用本机的 CodeBuddy / DeepSeek 做结构识别
        (Path(tmp) / "cfg").mkdir()
        (Path(tmp) / "cfg" / "config.json").write_text('{"provider": "none"}', encoding="utf-8")
        step_dir = Path(tmp) / "step"
        run = [sys.executable, str(REPO / "scripts/make_demo_assemblies.py"), "-o", str(step_dir)]
        subprocess.run(run, check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        seed = [sys.executable, str(REPO / "scripts/workbench.py"), "--data", f"{tmp}/data", "seed",
                "--step", str(step_dir / "demo-planetary-gearbox.stp"),
                "--plan", str(REPO / "examples/planetary-gearbox/figure-plan.json"),
                "--title", "行星齿轮减速器", "--id", PROJECT, "--example", "--render", "--order", "1"]
        subprocess.run(seed, check=True, env=env, stdout=subprocess.DEVNULL)
        srv = subprocess.Popen(
            [sys.executable, str(REPO / "scripts/workbench.py"), "--data", f"{tmp}/data",
             "serve", "--no-browser", "--no-example", "--port", str(PORT)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_ready()
            with sync_playwright() as p:
                browser = launch(p)
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.goto(f"{BASE}/?token={TOKEN}", wait_until="networkidle")
                page.wait_for_timeout(2500)
                page.screenshot(path=str(OUT / "shot-home.png"))
                page.goto(f"{BASE}/p/{PROJECT}/?token={TOKEN}", wait_until="networkidle")
                page.wait_for_timeout(6000)  # 等 3D 模型加载与首轮渲染
                page.screenshot(path=str(OUT / "shot-studio.png"))
                browser.close()
        finally:
            srv.terminate()
            srv.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
