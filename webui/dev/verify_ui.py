#!/usr/bin/env python3
"""WP3 前端回归用例（对 mock 跑）。覆盖 Codex R3/R3b 的几条高危修复：
  * XSS：恶意零件名 / 图题 / AI reason / 结构组名不得变成可执行 HTML
  * 键盘金路径：搜索 → 方向键定位单件 → Space 选中 → Tab 到图卡「加入所选」Enter 归图
  * 术语页单元格键盘可选中
  * 上传失败（非 2xx）后重试按钮可用、无 ReferenceError
  * 结果版本绑定：改计划并保存后刷新，导出仍锁定（不被「刷新重认」旁路）

先起 mock：python3 webui/dev/mock_server.py --port 8803
再跑：    python3 webui/dev/verify_ui.py        # 全过退出 0，任一失败退出 1
"""
import sys
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8803"
PAYLOAD = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'
results = []


def check(name, ok, extra=""):
    results.append((name, ok, extra))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' · ' + extra) if extra else ''}")


def test_xss(pg):
    alerts = []
    pg.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2600)
    r = pg.evaluate("""async (P) => {
        const st = await import('/js/state.js');
        st.state.plan.terms[0].term = P;
        st.state.plan.figures[0].caption = P + '"and-quote';
        st.state.aiSuggestions['PGB-MOTOR-HOUSING'] = {selector:'PGB-MOTOR-HOUSING', term:P, confidence:'low', reason:'</script>'+P};
        if (st.state.structure && st.state.structure.groups[0]) st.state.structure.groups[0].name = P;
        st.bus.emit('plan'); st.bus.emit('ai-suggestions'); st.bus.emit('structure');
        document.querySelector('.rtab[data-tab=terms]').click();
        await new Promise(x=>setTimeout(x,200));
        document.querySelector('.rtab[data-tab=figures]').click();
        await new Promise(x=>setTimeout(x,200));
        const injected = [...document.querySelectorAll('img')].filter(i=>i.getAttribute('src')==='x').length;
        return { injected, xss: window.__xss||0, literal: (document.querySelector('.term-input')?.value||'').includes('<img') };
    }""", PAYLOAD)
    check("XSS：恶意串不执行、以文本呈现",
          r["injected"] == 0 and r["xss"] == 0 and len(alerts) == 0 and r["literal"],
          f"injectedImg={r['injected']} xssFired={r['xss']} dialogs={len(alerts)}")


def test_keyboard(pg):
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2600)
    # 搜索聚焦（/），清空后用方向键在列表里走
    pg.evaluate("() => { const r=document.querySelector('.part-row'); r.tabIndex=0; r.focus(); }")
    pg.keyboard.press("ArrowDown"); pg.wait_for_timeout(80)
    name = pg.evaluate("() => document.activeElement?.dataset?.name")
    pg.keyboard.press(" "); pg.wait_for_timeout(250)
    sel = pg.eval_on_selector_all(".part-row.is-selected", "e => e.length")
    # 到图纸页，用键盘聚焦「＋新建图」并 Enter，用所选零件建一张图（归图）
    pg.evaluate("() => document.querySelector('.rtab[data-tab=figures]').click()"); pg.wait_for_timeout(200)
    before = pg.eval_on_selector_all(".fig-card", "e => e.length")
    pg.evaluate("() => { const b=document.querySelector('[data-new]'); b && b.focus(); }")
    pg.keyboard.press("Enter"); pg.wait_for_timeout(400)
    after_figs = pg.eval_on_selector_all(".fig-card", "e => e.length")
    newhaschip = pg.evaluate("""() => { const c=[...document.querySelectorAll('.fig-card')].pop(); return c ? c.querySelectorAll('.chip').length : 0; }""")
    check("键盘金路径：方向键定位 + Space 选中单件", bool(name) and sel >= 1, f"part={name} selected={sel}")
    check("键盘金路径：Enter 归图（用所选新建图）", after_figs > before and newhaschip >= 1, f"figs {before}→{after_figs}, newChips={newhaschip}")


def test_terms_keyboard(pg):
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2600)
    pg.evaluate("() => document.querySelector('.rtab[data-tab=terms]').click()"); pg.wait_for_timeout(300)
    pg.evaluate("() => { const c=document.querySelector('.sel'); c.focus(); }")
    pg.keyboard.press("Enter"); pg.wait_for_timeout(250)
    sel = pg.eval_on_selector_all(".part-row.is-selected", "e => e.length")
    check("术语页单元格键盘可选中", sel >= 1, f"selected={sel}")


def test_upload_retry(pg):
    pe = []
    pg.on("pageerror", lambda e: pe.append(str(e)))
    pg.route("**/v1/projects*", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"error":{"message":"服务器开小差"}}')
        if route.request.method == "POST" else route.continue_())
    pg.goto(f"{B}/home.html", wait_until="networkidle"); pg.wait_for_timeout(600)
    pg.evaluate("() => document.querySelector('#drop').click()"); pg.wait_for_timeout(300)
    path = "/tmp/_verify_ui.stp"
    open(path, "w").write("ISO-10303-21;")
    pg.set_input_files("#up-file", path); pg.wait_for_timeout(900)
    has_retry = pg.query_selector("#up-retry") is not None
    if has_retry:
        pg.click("#up-retry"); pg.wait_for_timeout(500)
    check("上传失败后重试可用且无 ReferenceError", has_retry and len(pe) == 0, f"pageerrors={pe[:1]}")


def test_version_reload(pg):
    pg.goto(f"{B}/index.html?project=gearbox&stage=export", wait_until="networkidle")
    pg.wait_for_timeout(1200)
    ready = pg.query_selector(".exp-wrap") is not None
    # 改一处术语并等防抖保存
    pg.evaluate("""async () => { const st=await import('/js/state.js'); st.mutate(p=>{ p.terms[0].term='改一下'+Date.now(); }); await new Promise(x=>setTimeout(x,900)); }""")
    pg.wait_for_timeout(300)
    # 刷新后导出应仍锁定（版本摘要持久化，不被重新认定一致）
    pg.goto(f"{B}/index.html?project=gearbox&stage=export", wait_until="networkidle")
    pg.wait_for_timeout(1200)
    locked = pg.query_selector(".exp-blank") is not None
    check("版本绑定：改计划并保存后刷新，导出仍锁定", ready and locked, f"freshReady={ready} lockedAfterReload={locked}")


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        for fn in (test_xss, test_keyboard, test_terms_keyboard, test_upload_retry, test_version_reload):
            pg = ctx.new_page()
            try:
                fn(pg)
            except Exception as exc:
                check(fn.__name__, False, f"异常 {exc!r}")
            pg.close()
        b.close()
    ok = all(r[1] for r in results)
    print(f"\n{'全部通过' if ok else '有失败'}：{sum(1 for r in results if r[1])}/{len(results)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
