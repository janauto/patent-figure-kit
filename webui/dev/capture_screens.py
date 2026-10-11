#!/usr/bin/env python3
"""用 Playwright 把五屏四态截图到 webui/dev/screens/。
先起 mock：
    python3 webui/dev/mock_server.py --port 8803
    python3 webui/dev/mock_server.py --port 8804 --no-ai --no-dwg   # 失败场景
再跑：
    python3 webui/dev/capture_screens.py
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "screens"
OUT.mkdir(exist_ok=True)
B = "http://127.0.0.1:8803"
B2 = "http://127.0.0.1:8804"   # --no-ai --no-dwg
VW = {"width": 1440, "height": 900}

# (文件名, URL, 载入后额外等待 ms, 可选：进入前点击的选择器)
SHOTS = [
    # 屏① 首页 / 工程列表
    ("01-home-empty",    f"{B}/home.html?scene=empty",   700),
    ("01-home-loading",  f"{B}/home.html?scene=loading",  500),
    ("01-home-success",  f"{B}/home.html",                900),
    ("01-home-error",    f"{B}/home.html?scene=error",    600),
    # 屏② 新建与上传
    ("02-upload-empty",   f"{B}/home.html?upload=idle",    700),
    ("02-upload-loading", f"{B}/home.html?upload=running",  700),
    ("02-upload-success", f"{B}/home.html?upload=done",     700),
    ("02-upload-fail",    f"{B}/home.html?upload=fail",     700),
    # 屏③ 规划台
    ("03-plan-empty",    f"{B}/index.html?project=ball-valve&stage=plan",       2800),
    ("03-plan-loading",  f"{B}/index.html?project=gearbox&stage=plan&state=loading", 900),
    ("03-plan-success",  f"{B}/index.html?project=gearbox&stage=plan",          3000),
    ("03-plan-fail",     f"{B}/index.html?project=gearbox&stage=plan&state=fail",    900),
    # 屏④ 出图与 QA
    ("04-qa-empty",      f"{B}/index.html?project=bench-vise&stage=qa",          900),
    ("04-qa-loading",    f"{B}/index.html?project=gearbox&stage=qa&state=loading",    900),
    ("04-qa-success",    f"{B}/index.html?project=gearbox&stage=qa",             1400),
    ("04-qa-fail",       f"{B}/index.html?project=gearbox&stage=qa&state=fail",       1400),
    ("04-qa-novalidate", f"{B}/index.html?project=gearbox&stage=qa&state=novalidate",  900),
    # 屏⑤ 导出
    ("05-export-locked", f"{B}/index.html?project=gearbox&stage=export&state=empty",  900),
    ("05-export-ready",  f"{B}/index.html?project=gearbox&stage=export",         1000),
    ("05-export-packing",f"{B}/index.html?project=gearbox&stage=export&state=packing", 900),
    ("05-export-done",   f"{B}/index.html?project=gearbox&stage=export&state=done",    900),
    # 失败场景补充（--no-ai / --no-dwg 实例）
    ("x1-no-ai-home",    f"{B2}/home.html",                                      900),
    ("x2-no-dwg-export", f"{B2}/index.html?project=gearbox&stage=export",        1000),
    # 静态只读站（static 适配器，读 manifest.json）
    ("x3-static-home",     f"{B}/home.html?static",                             1000),
    ("x4-static-studio",   f"{B}/index.html?static&project=gearbox",            3000),
]


# 需脚本驱动的状态（无 URL 开关）：legacy 出图在途期间计划被改过 → 结果版本不确定（raced）→ QA 提示 + 导出锁定。
# 这里在页内直接置出该状态（与 verify_ui.py 的时序用例得到的状态一致），只为截图。
RACED_JS = """async () => {
  const st = await import('/js/state.js');
  st.state.planEtag = null;                      // 旧后端：无 ETag，走输入摘要
  st.state.renderInputs = { etag: null, digest: st.planDigest(), raced: true };
  st.bus.emit('rendered');
}"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=VW, device_scale_factor=2)
        page = ctx.new_page()
        for name, url, wait in SHOTS:
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(wait)
            page.screenshot(path=str(OUT / f"{name}.png"))
            print("captured", name)
        page.goto(f"{B}/index.html?project=gearbox&stage=qa", wait_until="networkidle")
        page.wait_for_timeout(1400)
        page.evaluate(RACED_JS)
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "x5-qa-raced.png"))
        print("captured", "x5-qa-raced")
        page.click('.stage-tab[data-stage="export"]')
        page.wait_for_timeout(900)
        page.screenshot(path=str(OUT / "x6-export-raced.png"))
        print("captured", "x6-export-raced")
        browser.close()
    print("done →", OUT)


if __name__ == "__main__":
    main()
