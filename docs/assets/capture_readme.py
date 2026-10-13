#!/usr/bin/env python3
"""重新生成 README 用图（截图即代码）。

    python3 docs/assets/capture_readme.py            # 全部重拍
    python3 docs/assets/capture_readme.py --no-gif   # 不重做首屏 GIF

做法：
* 用全新的临时数据目录、临时配置目录与随机 token 起一个真实工作台（scripts/workbench.py，--no-example），
  **AI 通道显式关闭**，不读写 ~/.patent-workbench 与 ~/.config/patent-workbench，不调用任何大模型。
* 只用三个公开演示模型（scripts/make_demo_assemblies.py 生成的减速器 / 台虎钳 / 球阀，与任何真实产品无关）。
* 第一段在空数据目录上连续走一遍金路径：新建 → 载入示例「行星齿轮减速器」→ 候选分图 → 接受一张 → 出图 →
  导出附图包（两版，PDF + Word + DXF），用 CDP screencast 连续录屏（带时间戳）。首屏 GIF 由这些帧按时间表抽出，
  右上角计时 = 录制日志里的真实时间（等待处定格，不快进）。
* 之后在同一实例上补拍功能图（候选被拦、结构树、爆炸、框选、台虎钳闸门、流程图出图），并把下载的导出包渲染成图：
  PDF 用 pypdfium2 栅格，Word 用 macOS Quick Look（qlmanage；非 macOS 跳过），DXF 用 ezdxf 的 matplotlib 后端。

输出：docs/assets/hero.gif、docs/assets/readme/*.jpg、docs/assets/package/*。
需要：pip install playwright（本机 Chrome；没有时自动退回 Playwright 自带 Chromium），ffmpeg（做 GIF）。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).resolve().parent
OUT_R = ASSETS / "readme"
OUT_P = ASSETS / "package"
PORT = 8796
VIEW = {"width": 1600, "height": 1000}

TMP = Path(tempfile.mkdtemp(prefix="pfk-readme-"))
os.environ.setdefault("WP3B_TOKEN", "readme-" + secrets.token_hex(12))
os.environ.setdefault("WP3B_SCRATCH", str(TMP / "logs"))
sys.path.insert(0, str(REPO / "webui" / "dev"))
import e2e_live as E  # noqa: E402  已对真实后端验证过的后端 / 浏览器工具
from playwright.sync_api import sync_playwright  # noqa: E402

GL = (["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"] if platform.system() == "Darwin"
      else ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])


# --------------------------------------------------------------------------- 录屏
class Rec:
    """CDP screencast（页面有变化就来一帧）+ 停发时补截图；事件带时间戳。"""

    def __init__(self, ctx, pg, name):
        self.pg, self.dir = pg, TMP / "frames" / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames, self.events, self.n = [], [], 0
        self.cdp = ctx.new_cdp_session(pg)
        self.cdp.on("Page.screencastFrame", self._frame)

    def _frame(self, p):
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})
        except Exception:
            pass
        f = self.dir / ("%05d.jpg" % self.n)
        f.write_bytes(base64.b64decode(p["data"]))
        self.frames.append((float(p.get("metadata", {}).get("timestamp") or time.time()), f))
        self.n += 1

    def snap(self):
        f = self.dir / ("%05d.jpg" % self.n)
        self.pg.screenshot(path=str(f), type="jpeg", quality=90)
        self.frames.append((time.time(), f))
        self.n += 1

    def start(self):
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88, "maxWidth": 3200, "maxHeight": 2000})

    def stop(self):
        self.cdp.send("Page.stopScreencast")

    def ev(self, label):
        self.events.append((time.time(), label))

    def idle(self, ms):
        end = time.time() + ms / 1000
        while time.time() < end:
            self.pg.wait_for_timeout(min(250, max(1, (end - time.time()) * 1000)))
            if not self.frames or time.time() - self.frames[-1][0] > 0.45:
                self.snap()

    def at(self, t):
        best = self.frames[0][1]
        for ft, f in sorted(self.frames):
            if ft <= t:
                best = f
        return best


def _sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    except OSError:
        return "?"


def save_jpg(src, dst, width=1600, box=None):
    from PIL import Image
    im = Image.open(src).convert("RGB")
    if box:   # 页面坐标（1600×1000）→ 2x 帧
        s = im.width / 1600
        im = im.crop(tuple(int(v * s) for v in (box[0], box[1], box[0] + box[2], box[1] + box[3])))
    if im.width > width:
        im = im.resize((width, int(im.height * width / im.width)), Image.LANCZOS)
    dst.parent.mkdir(parents=True, exist_ok=True)
    im.save(dst, quality=84, optimize=True)
    print("  →", dst.relative_to(REPO), "%d KB" % (dst.stat().st_size // 1024))


def context(browser, be):
    ctx = browser.new_context(viewport=VIEW, device_scale_factor=2, accept_downloads=True)
    ctx.on("page", lambda p: p.on("dialog", lambda d: d.accept()))
    pg = ctx.new_page()
    pg.goto(f"{be.base}/?token={E.TOKEN}", wait_until="networkidle")
    pg.close()
    return ctx


def studio(pg, be, pid, stage="plan"):
    pg.goto(f"{be.base}/p/{pid}/?stage={stage}", wait_until="networkidle")
    E.studio_ready(pg)
    if stage == "plan":
        pg.wait_for_function("window.planStudioViewer && window.planStudioViewer.partCount>0", timeout=90000)
    pg.wait_for_timeout(900)


def set_ctl(pg, cid, value):
    pg.evaluate("""([id,v])=>{const e=document.getElementById(id); e.value=v;
      e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}));}""", [cid, value])


def box_of(pg, sel):
    h = pg.query_selector(sel)
    b = h.bounding_box() if h else None
    return None if not b else [b["x"], b["y"], b["width"], b["height"]]


# --------------------------------------------------------------------------- 金路径（连续录制）
def golden(browser, be):
    ctx = context(browser, be)
    pg = ctx.new_page()
    r = Rec(ctx, pg, "golden")
    r.start()
    pg.goto(f"{be.base}/", wait_until="networkidle")
    pg.wait_for_selector("#load-example")
    r.idle(2000)
    pg.click("#empty-upload"); r.ev("click_new")
    pg.wait_for_selector("#up-drop"); r.idle(2000)
    r.ev("T0"); pg.click("#up-ex-gearbox")
    pg.wait_for_selector("#up-enter", timeout=300000); r.ev("import_done")
    pg.click("#up-enter")
    pg.wait_for_url(re.compile(r".*/p/example-gearbox/.*"), timeout=30000)
    E.studio_ready(pg)
    pg.wait_for_selector(".cand-card.pass [data-accept]", timeout=60000); r.ev("cands_ready")
    cid = pg.get_attribute(".cand-card.pass", "data-cid")
    pg.click(f'.cand-card[data-cid="{cid}"] [data-accept]')
    pg.wait_for_function(f"() => /已写入图纸页/.test(document.querySelector('.cand-card[data-cid=\"{cid}\"]')?.textContent || '')",
                         timeout=30000); r.ev("accepted")
    pg.click('.stage-tab[data-stage="qa"]')
    pg.wait_for_selector("#qa-render, #rerender-stale, #rerender:not([disabled]), #qa-retry", timeout=30000)
    sel = next(s for s in ("#qa-render", "#rerender-stale", "#rerender:not([disabled])", "#qa-retry") if pg.query_selector(s))
    prev = E.page_state(pg, "st.state.renderJob && st.state.renderJob.id") or ""
    r.ev("render_click"); pg.click(sel)
    E.wait_render_terminal(pg, prev); r.ev("render_done")
    pg.click('.stage-tab[data-stage="export"]')
    pg.wait_for_selector("#do-pack", timeout=20000)
    pg.click('[data-v="both"]')
    E.pick_formats(pg, {"pdf": True, "docx": True, "dxf": True, "dwg": False})
    r.ev("pack_click"); pg.click("#do-pack")
    pg.wait_for_selector(".exp-done", timeout=600000); r.ev("T_end")
    r.idle(2500)
    href = pg.get_attribute(".exp-zip-acts a[download]", "href")
    if href.startswith("/"):
        href = be.base + href
    blob = ctx.request.get(href).body()
    r.stop()
    ctx.close()
    ev = {k: t for t, k in r.events}
    run = {"T0_to_package_s": round(ev["T_end"] - ev["T0"], 2), "import_and_candidates_s": round(ev["import_done"] - ev["T0"], 2),
           "render_s": round(ev["render_done"] - ev["render_click"], 2), "export_s": round(ev["T_end"] - ev["pack_click"], 2),
           "example": "gearbox", "data_dir": "全新临时目录（无缓存首次运行）", "ai": "关闭",
           "machine": _sh(["sysctl", "-n", "machdep.cpu.brand_string"]) if platform.system() == "Darwin" else platform.processor(),
           "os": ("macOS " + platform.mac_ver()[0]) if platform.system() == "Darwin" else platform.platform(),
           "python": platform.python_version(), "commit": _sh(["git", "-C", str(REPO), "rev-parse", "--short=7", "HEAD"]),
           "date": time.strftime("%Y-%m-%d")}
    OUT_R.mkdir(parents=True, exist_ok=True)
    (OUT_R / "hero-run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("  金路径 T0→完成 %.1f s（载入+候选 %.1f / 出图 %.1f / 打包 %.1f）" % (
        run["T0_to_package_s"], run["import_and_candidates_s"], run["render_s"], run["export_s"]))
    save_jpg(r.at(ev["import_done"]), OUT_R / "step1-import.jpg")
    save_jpg(r.at(ev["cands_ready"] + 0.05), OUT_R / "step2-candidates.jpg")
    save_jpg(r.at(ev["T_end"] + 1.0), OUT_R / "step4-export.jpg")
    return r, ev, blob


def hero_gif(r, ev):
    """30 秒首屏 GIF：真实帧按时间表抽取，计时 = 录制日志时间（等待处定格）。"""
    from PIL import Image, ImageDraw, ImageFont
    font = next((f for f in ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
                             "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc") if Path(f).is_file()), None)
    K = [(0, ev["click_new"] - 1.5), (2.5, ev["click_new"]), (5.0, ev["T0"]), (5.0 + ev["import_done"] - ev["T0"], ev["import_done"]),
         (11.8, ev["import_done"]), (12.4, ev["cands_ready"]), (15.0, ev["cands_ready"]), (15.4, ev["accepted"]), (16.0, ev["render_click"]),
         (16.0 + ev["render_done"] - ev["render_click"], ev["render_done"]), (21.0, ev["render_done"]), (22.0, ev["pack_click"]),
         (22.0 + ev["T_end"] - ev["pack_click"], ev["T_end"]), (30.0, ev["T_end"])]
    steps = [("① 载入示例", 0, 11.8), ("② 候选分图", 11.8, 16.0), ("③ 出图 · 13 项 QA", 16.0, 21.0), ("④ 导出附图包", 21.0, 31)]

    def cap(t):
        for (a, ca), (b, cb) in zip(K, K[1:]):
            if a <= t <= b:
                return ca if b == a else ca + (cb - ca) * (t - a) / (b - a)
        return K[-1][1]
    tmp = TMP / "gif"; tmp.mkdir(exist_ok=True)
    W, s = 960, 960 / 1600
    for i in range(300):
        t = i / 10; c = cap(t)
        im = Image.open(r.at(c)).convert("RGB").resize((W, int(1000 * s)), Image.LANCZOS)
        bar = int(64 * s)
        out = Image.new("RGB", (W, im.height + bar), (243, 239, 230)); out.paste(im, (0, bar))
        d = ImageDraw.Draw(out)
        f = ImageFont.truetype(font, int(26 * s)) if font else ImageFont.load_default()
        x = int(24 * s)
        for name, a, b in steps:
            col = (180, 58, 43) if a <= t < b else ((46, 125, 79) if t >= b else (154, 148, 136))
            d.text((x, int(16 * s)), name, font=f, fill=col); x += int(d.textlength(name, font=f) + 36 * s)
        if c >= ev["T0"]:
            txt = "本次运行计时 %.1f s" % (min(c, ev["T_end"]) - ev["T0"])
            w = d.textlength(txt, font=f)
            d.rectangle([W - w - 40 * s, 10 * s, W - 14 * s, 54 * s], fill=(30, 28, 23))
            d.text((W - w - 27 * s, int(16 * s)), txt, font=f, fill=(253, 252, 248))
        d.line([0, bar - 1, W, bar - 1], fill=(30, 28, 23), width=1)
        out.save(tmp / ("%04d.png" % i))
    pal = tmp / "pal.png"; gif = ASSETS / "hero.gif"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "10", "-i", str(tmp / "%04d.png"),
                    "-vf", "palettegen=max_colors=128:stats_mode=diff", str(pal)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "10", "-i", str(tmp / "%04d.png"), "-i", str(pal),
                    "-lavfi", "paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", "-loop", "0", str(gif)], check=True)
    print("  → docs/assets/hero.gif %.1f MB" % (gif.stat().st_size / 2**20))


# --------------------------------------------------------------------------- 导出包渲染
def package(blob):
    OUT_P.mkdir(parents=True, exist_ok=True)
    d = TMP / "pkg"; d.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(d)
        names = z.namelist()
    find = lambda key: next(d / n for n in names if key in n and not n.endswith("/"))
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(find("递交版.pdf")))
    pdf[0].render(scale=110 / 72).to_pil().convert("L").save(OUT_P / "pdf-filing-p1.png", optimize=True)
    print("  → docs/assets/package/pdf-filing-p1.png")
    if shutil.which("qlmanage"):
        q = d / "ql"; q.mkdir()
        subprocess.run(["qlmanage", "-t", "-s", "1100", "-o", str(q), str(find("交底版.docx"))],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from PIL import Image
        Image.open(next(q.glob("*.png"))).convert("L").save(OUT_P / "docx-disclosure-p1.png", optimize=True)
        print("  → docs/assets/package/docx-disclosure-p1.png（Quick Look 预览渲染）")
    else:
        print("  ! 没有 qlmanage（非 macOS）：跳过 Word 页预览图")
    render_dxf(find("交底版_带件号表/fig1_annotated.dxf"), OUT_P / "dxf-source.png", (6.4, 8.6))
    print("  → docs/assets/package/dxf-source.png（ezdxf 渲染）")
    for key, fn in (("附图说明", "figure-description.txt"), ("核对清单", "checklist.txt")):
        (OUT_P / fn).write_text(find(key).read_text(encoding="utf-8"), encoding="utf-8")


def render_dxf(src, dst, size):
    """DXF → PNG（ezdxf drawing add-on，白底黑线）。"""
    import ezdxf
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    doc = ezdxf.readfile(str(src))
    fig = plt.figure(figsize=size, dpi=125)
    ax = fig.add_axes([0, 0, 1, 1])
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(doc.modelspace(), finalize=True)
    fig.savefig(dst, facecolor="white")
    plt.close(fig)


# --------------------------------------------------------------------------- 功能图（热态补拍）
def features(browser, be):
    ctx = context(browser, be)
    pg = ctx.new_page()
    for ex in ("vise", "valve"):        # 首页缩略图要有真实出图
        be.ensure_example(ex)
        _, j, _ = be.rest("POST", "/v1/projects/example-%s/render" % ex)
        be.wait_job(j["job_id"])
    pg.goto(f"{be.base}/", wait_until="networkidle")
    pg.wait_for_selector(".card .card-thumb")
    pg.evaluate("document.querySelectorAll('img[loading=lazy]').forEach(i=>i.loading='eager')")
    pg.wait_for_function("[...document.images].every(i=>i.complete && i.naturalWidth>0)", timeout=30000)
    pg.wait_for_timeout(800)
    shot(pg, "home.jpg")
    # 第三步的静帧：已发布版本的出图与 13 项 QA（连续录制里出图后立即切到了导出页，这里单独补拍）
    studio(pg, be, "example-gearbox", stage="qa")
    pg.wait_for_selector(".svg-pane svg", timeout=30000); pg.wait_for_timeout(800)
    shot(pg, "step3-qa.jpg")
    # 候选：被拦的整机候选（白话原因 + 建议）
    studio(pg, be, "example-gearbox")
    set_ctl(pg, "v-color", "figure"); pg.wait_for_timeout(600)
    pg.locator(".cand-card.fail").first.scroll_into_view_if_needed(); pg.wait_for_timeout(600)
    shot(pg, "feat-candidate-blocked.jpg")
    # 结构树（按 STEP 装配层级自动分组）
    set_ctl(pg, "v-color", "structure"); pg.wait_for_timeout(600)
    shot(pg, "feat-structure.jpg")
    # 透明 + 爆炸（按结构拉开）
    set_ctl(pg, "v-mode", "group"); set_ctl(pg, "v-explode", "60"); set_ctl(pg, "v-opacity", "55")
    pg.click("#btn-frame"); pg.wait_for_timeout(1200)
    shot(pg, "feat-explode.jpg")
    # 框选：B → 左上到右下拖框
    set_ctl(pg, "v-opacity", "100"); pg.wait_for_timeout(500)
    vp = box_of(pg, "#viewport canvas")
    pg.keyboard.press("b"); pg.wait_for_timeout(300)
    a = (vp[0] + vp[2] * 0.30, vp[1] + vp[3] * 0.25); z = (vp[0] + vp[2] * 0.62, vp[1] + vp[3] * 0.72)
    pg.mouse.move(*a); pg.mouse.down()
    for i in range(1, 21):
        pg.mouse.move(a[0] + (z[0] - a[0]) * i / 20, a[1] + (z[1] - a[1]) * i / 20)
    shot(pg, "feat-box-select.jpg")
    pg.mouse.up(); pg.keyboard.press("Escape")
    # 台虎钳：整机候选被拦 → 仍然接受 → 出图失败（上一成功版本保留）
    st, j, _ = be.rest("POST", "/v1/projects/example-vise/plan/suggest", {}); be.wait_job(j["job_id"])
    _, j, _ = be.rest("POST", "/v1/projects/example-vise/render"); be.wait_job(j["job_id"])
    studio(pg, be, "example-vise")
    pg.wait_for_selector(".cand-card.fail", timeout=60000)
    whole = E.page_state(pg, "(st.state.candidates || []).find(c => c.source && c.source.kind === 'whole')")
    card = f'.cand-card[data-cid="{whole["id"]}"]'
    pg.locator(card).scroll_into_view_if_needed(); pg.wait_for_timeout(600)
    shot(pg, "feat-gate-blocked.jpg")
    pg.click(f"{card} [data-accept-fail]")
    pg.wait_for_function(f"() => /已写入图纸页/.test(document.querySelector('{card}')?.textContent || '')", timeout=30000)
    pg.click('.stage-tab[data-stage="qa"]'); pg.wait_for_timeout(600)
    prev = E.page_state(pg, "st.state.renderJob && st.state.renderJob.id") or ""
    sel = next(s for s in ("#qa-render", "#rerender-stale", "#rerender:not([disabled])", "#qa-retry") if pg.query_selector(s))
    pg.click(sel); E.wait_render_terminal(pg, prev); pg.wait_for_timeout(600)
    shot(pg, "feat-gate-failed.jpg")
    be.reset_example_plan("vise")
    # 流程图：手填两步 → 出图（S101 由程序发放），取同一次出图的 SVG
    studio(pg, be, "example-gearbox")
    pg.click('.rtab[data-tab="flows"]'); pg.wait_for_timeout(600)
    pg.click("#flows [data-new]"); pg.wait_for_selector("#flows .flow-edit")
    pg.locator("#flows [data-spec-title]").fill("行星齿轮减速器的装配方法流程图")
    pg.locator('#flows input[data-n="1"][data-k="text"]').fill("将三个行星轮装到行星架上")
    pg.click("#flows [data-render]"); pg.wait_for_selector("#flows .desc-box", timeout=60000)
    fid = pg.get_attribute("#flows .flow-card.is-active", "data-open")
    # 流程图出图本体（DXF，与导出同源）；界面里的 SVG 预览不计文字宽度、会裁掉右侧步骤号，这里直接渲染 DXF
    st, dxf, _ = be.rest("GET", f"/p/example-gearbox/api/flow-preview/{fid}.dxf", raw=True)
    if st == 200:
        f = TMP / "flow.dxf"; f.write_bytes(dxf)
        render_dxf(f, OUT_R / "feat-flowchart.png", (5.2, 6.2))
        save_jpg(OUT_R / "feat-flowchart.png", OUT_R / "feat-flowchart.jpg", width=700)
        (OUT_R / "feat-flowchart.png").unlink()
    ctx.close()


def shot(pg, name):
    f = TMP / name.replace(".jpg", ".png")
    pg.screenshot(path=str(f))
    save_jpg(f, OUT_R / name, width=1400)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gif", action="store_true")
    a = ap.parse_args()
    be = E.Backend("readme", PORT, TMP / "data", TMP / "cfg", {"provider": "none"}, serve_args=["--no-example"])
    be.up()
    try:
        st, caps, _ = be.rest("GET", "/v1/capabilities")
        assert not (caps.get("ai") or {}).get("configured"), "AI 通道必须关闭"
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True, args=GL)
            except Exception:
                browser = p.chromium.launch(headless=True, args=GL)
            r, ev, blob = golden(browser, be)
            if not a.no_gif:
                hero_gif(r, ev)
            package(blob)
            features(browser, be)
            browser.close()
    finally:
        be.down()
        shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
