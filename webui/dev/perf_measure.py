#!/usr/bin/env python3
"""按需渲染前后对比 + 首页体积/TTI 实测（WP3 报告用）。需 mock 在 8803 运行。
    python3 webui/dev/perf_measure.py
"""
import json
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8803"


def main():
    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)

        # ---- 首页：体积 / TTI / 是否加载 three.js ----
        page = ctx.new_page()
        sizes = {"total": 0, "three": 0}
        def on_resp(r):
            try:
                body = r.body(); n = len(body)
            except Exception:
                n = int(r.headers.get("content-length") or 0)
            sizes["total"] += n
            if "three" in r.url:
                sizes["three"] += n
        page.on("response", on_resp)
        page.goto(f"{B}/home.html", wait_until="networkidle")
        tti = page.evaluate("() => { const n = performance.getEntriesByType('navigation')[0];"
                            " return Math.round(n.domInteractive); }")
        loaded_three = page.evaluate("() => performance.getEntriesByType('resource')"
                                     ".some(r => r.name.includes('three'))")
        out["home"] = {"transfer_bytes": sizes["total"], "three_bytes_on_home": sizes["three"],
                       "dom_interactive_ms": tti, "loaded_three_js": loaded_three}
        page.close()

        # ---- 规划台：按需渲染空闲帧数 vs 连续循环基线 ----
        page = ctx.new_page()
        page.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
        page.wait_for_timeout(3500)   # 等模型就位 + damping 滑完
        c0 = page.evaluate("() => window.planStudioViewer ? window.planStudioViewer.renderCount : -1")
        page.wait_for_timeout(3000)   # 纯空闲 3s
        c1 = page.evaluate("() => window.planStudioViewer.renderCount")
        pr_idle = page.evaluate("() => window.planStudioViewer.pixelRatio")
        # 连续 rAF 基线（模拟旧 animate() 每帧无条件渲染，同机同浏览器）
        base = page.evaluate("""() => new Promise(res => {
            let n = 0; const t0 = performance.now();
            function loop(){ n++; if (performance.now()-t0 < 3000) requestAnimationFrame(loop); else res(n); }
            requestAnimationFrame(loop);
        })""")
        out["viewer"] = {
            "idle_render_frames_3s_ondemand": c1 - c0,     # 期望 0
            "continuous_loop_frames_3s_baseline": base,    # 旧 animate() 同窗口的帧数
            "pixel_ratio_idle": pr_idle,
        }
        # ---- 拖转时 pixelRatio 降 1，停手恢复 ----
        box = page.evaluate("""() => { const el = document.querySelector('#viewport canvas');
            const r = el.getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2}; }""")
        page.mouse.move(box["x"], box["y"])
        page.mouse.down()
        page.mouse.move(box["x"] + 60, box["y"] + 40, steps=6)
        pr_drag = page.evaluate("() => window.planStudioViewer.pixelRatio")
        drag_frames = page.evaluate("() => window.planStudioViewer.renderCount") - c1
        page.mouse.up()
        page.wait_for_timeout(600)
        pr_after = page.evaluate("() => window.planStudioViewer.pixelRatio")
        out["drag"] = {"pixel_ratio_during_drag": pr_drag, "pixel_ratio_after_drag": pr_after,
                       "frames_during_drag": drag_frames}
        page.close()
        browser.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
