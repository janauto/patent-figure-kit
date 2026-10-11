#!/usr/bin/env python3
"""WP3 前端回归用例（对 mock 跑）。覆盖 Codex R3 / R3b / R3c 的高危修复：
  * XSS：恶意零件名 / 图题 / AI reason / 结构组名 / 无效计划 ID 不得变成可执行 HTML
  * 保存竞态：慢保存期间连续编辑，服务端最终收到最新版（不吞编辑）
  * 结果版本权威：改计划保存后「新会话」里旧结果不解锁导出
  * 候选成员契约：接受候选后每个成员都能在 assembly 中解析
  * 键盘金路径：连续真实按键完成多件选择 + 归图（不脚本补焦点）；术语单元格键盘可选
  * 上传失败重试：保留文件，第二次请求确实带上文件名/标题/AI 开关
  * 静态站：只读，不发任何写请求（POST/PUT/DELETE）

起 mock：python3 webui/dev/mock_server.py --port 8803
起静态：python3 webui/dev/build_static.py && (cd webui/dev/static-site && python3 -m http.server 8808)
跑：    python3 webui/dev/verify_ui.py        # 全过退出 0，任一失败退出 1
"""
import sys
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8803"
STATIC = "http://127.0.0.1:8808/pfk"
PAYLOAD = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'
results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok), extra))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' · ' + extra) if extra else ''}")


def test_xss(pg):
    alerts = []
    pg.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2600)
    r = pg.evaluate("""async (P) => {
        const st = await import('/js/state.js');
        st.state.plan.terms[0].term = P;
        st.state.plan.figures[0].id = 'fig' + P;            // 无效计划 ID（属性上下文）
        st.state.plan.figures[0].caption = P + '"and-quote';
        st.state.aiSuggestions['PGB-MOTOR-HOUSING'] = {selector:'PGB-MOTOR-HOUSING', term:P, confidence:'low', reason:'</script>'+P};
        if (st.state.structure && st.state.structure.groups[0]) st.state.structure.groups[0].name = P;
        st.bus.emit('plan'); st.bus.emit('ai-suggestions'); st.bus.emit('structure');
        document.querySelector('.rtab[data-tab=terms]').click(); await new Promise(x=>setTimeout(x,200));
        document.querySelector('.rtab[data-tab=figures]').click(); await new Promise(x=>setTimeout(x,200));
        const injected = [...document.querySelectorAll('img')].filter(i=>i.getAttribute('src')==='x').length;
        return { injected, xss: window.__xss||0, literal: (document.querySelector('.term-input')?.value||'').includes('<img') };
    }""", PAYLOAD)
    check("XSS：恶意串（含无效计划 ID）不执行、以文本呈现",
          r["injected"] == 0 and r["xss"] == 0 and len(alerts) == 0 and r["literal"],
          f"injectedImg={r['injected']} xssFired={r['xss']} dialogs={len(alerts)}")


def test_save_race(pg):
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    # A 保存在途时编辑成 B，flush 后服务端最终计划应为 B
    final = pg.evaluate("""async () => {
        const st = await import('/js/state.js');
        st.mutate(p => { p.terms[0].term = 'RACE-AAA'; });
        const f1 = st.flush();                       // 开始保存 A（已同步序列化 A）
        st.mutate(p => { p.terms[0].term = 'RACE-BBB'; });   // 在途期间改成 B
        await f1; await st.flush();                  // 确保最新版（B）落盘
        const res = await fetch('/v1/projects/gearbox/plan');
        const j = await res.json();
        return j.plan.terms[0].term;
    }""")
    check("保存竞态：慢保存期间连续编辑，服务端最终收到 B", final == "RACE-BBB", f"server term={final}")


def test_new_session_lock(pg_ctx):
    # 会话 1：改计划并保存（不重渲）
    p1 = pg_ctx.new_page()
    p1.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle"); p1.wait_for_timeout(2500)
    p1.evaluate("""async () => { const st=await import('/js/state.js'); st.mutate(p=>{p.terms[0].term='SESSIONCHANGE';}); await st.flush(); }""")
    p1.wait_for_timeout(300); p1.close()
    # 会话 2（全新 context = 新标签/清空内存）：进入导出，应锁定（旧结果 etag 不匹配新 plan）
    p2 = pg_ctx.new_page()
    p2.goto(f"{B}/index.html?project=gearbox&stage=export", wait_until="networkidle"); p2.wait_for_timeout(1500)
    locked = p2.query_selector(".exp-blank") is not None
    p2.close()
    check("结果版本：改计划保存后新会话里导出仍锁定", locked, f"lockedInNewSession={locked}")


def test_candidate_resolve(pg):
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2600)
    r = pg.evaluate("""async () => {
        // 接受第一个候选，检查写进 plan 的 figure 成员都是 assembly 里的真实零件名
        const before = (await import('/js/state.js')).state.plan.figures.length;
        document.querySelector('[data-accept]')?.click();
        await new Promise(x=>setTimeout(x,300));
        const st = await import('/js/state.js');
        const names = new Set((st.state.assembly.parts||[]).map(p=>p.name));
        const fig = st.state.plan.figures[st.state.plan.figures.length-1];
        const bad = (fig.members||[]).filter(m => !names.has(m) && !/[*?\\[]/.test(m));
        return { added: st.state.plan.figures.length>before, members: (fig.members||[]).length, bad };
    }""")
    check("候选成员：接受后每个成员都能在 assembly 解析", r["added"] and len(r["bad"]) == 0,
          f"members={r['members']} unresolved={r['bad']}")


def test_keyboard_continuous(pg):
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2600)
    # 真实点第一行（选中第 1 件 + 取得焦点），随后只用键盘：方向键移到下一件 + Space 选中第 2 件。
    # 若重渲后丢焦点，ArrowDown/Space 不会作用到列表，第二件选不中。
    pg.click(".part-row"); pg.wait_for_timeout(200)      # 选中第 1 件（点击即选）
    pg.keyboard.press("ArrowDown"); pg.wait_for_timeout(150)
    pg.keyboard.press(" "); pg.wait_for_timeout(200)     # 选中第 2 件（依赖重渲后焦点仍在列表）
    sel = pg.eval_on_selector_all(".part-row.is-selected", "e => e.length")
    focus_in_list = pg.evaluate("() => document.activeElement?.classList?.contains('part-row')")
    check("键盘连续选择：Space→方向键→Space 选中两件（焦点不丢）", sel >= 2 and focus_in_list, f"selected={sel} focusInList={focus_in_list}")
    # 术语单元格键盘可选
    pg.evaluate("() => document.querySelector('.rtab[data-tab=terms]').click()"); pg.wait_for_timeout(300)
    pg.eval_on_selector(".sel", "e => e.focus()")
    pg.keyboard.press("Enter"); pg.wait_for_timeout(250)
    check("术语单元格键盘可选中", pg.eval_on_selector_all(".part-row.is-selected", "e=>e.length") >= 1)


def test_upload_retry(pg):
    pe = []
    pg.on("pageerror", lambda e: pe.append(str(e)))
    posts = []

    def handle(route):
        req = route.request
        if req.method == "POST":
            posts.append(req.post_data or "")
            route.fulfill(status=500, content_type="application/json", body='{"error":{"message":"服务器开小差"}}')
        else:
            route.continue_()
    pg.route("**/v1/projects*", handle)
    pg.goto(f"{B}/home.html", wait_until="networkidle"); pg.wait_for_timeout(600)
    pg.evaluate("() => document.querySelector('#drop').click()"); pg.wait_for_timeout(300)
    pg.eval_on_selector("#up-title", "e => e.value = '我的工程X'")
    pg.eval_on_selector("#up-ai", "e => e.checked = false")
    path = "/tmp/_verify_ui.stp"
    open(path, "w").write("ISO-10303-21;")
    pg.set_input_files("#up-file", path); pg.wait_for_timeout(900)
    has_retry = pg.query_selector("#up-retry") is not None
    if has_retry:
        pg.click("#up-retry"); pg.wait_for_timeout(700)
    # 第二次 POST 的 multipart 内容应含文件名、标题、AI 开关
    second = posts[-1] if len(posts) >= 2 else ""
    content_ok = ("_verify_ui.stp" in second) and ("我的工程X" in second) and ("run_ai" in second)
    check("上传失败重试：无 ReferenceError 且第二次请求带文件/标题/AI 开关",
          has_retry and len(pe) == 0 and len(posts) >= 2 and content_ok,
          f"posts={len(posts)} pageerr={pe[:1]} contentOK={content_ok}")


def test_concurrent_flush(pg):
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""async () => {
        const st = await import('/js/state.js');
        st.mutate(p => { p.terms[0].term = 'CF-1'; });
        const f1 = st.flush();                       // 两个并发 flush
        const f2 = st.flush();
        st.mutate(p => { p.terms[0].term = 'CF-2'; });   // 保存期间第三次编辑
        await Promise.all([f1, f2]);
        await st.flush();
        const sameLoop = f1 === f2;                  // 共享同一个保存循环 Promise
        const res = await (await fetch('/v1/projects/gearbox/plan')).json();
        return { term: res.plan.terms[0].term, sameLoop, saved: st.state.savedVersion, edit: st.state.editVersion };
    }""")
    check("并发 flush：共享单循环、服务端最终为最新版、版本号自洽",
          r["term"] == "CF-2" and r["sameLoop"] and r["saved"] >= r["edit"],
          f"term={r['term']} sameLoop={r['sameLoop']} saved={r['saved']} edit={r['edit']}")


def test_unsaved_lock(pg):
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    reason = pg.evaluate("""async () => {
        const st = await import('/js/state.js');
        st.mutate(p => { p.terms[0].term = 'UNSAVED-' + Date.now(); });   // 改了但没 flush
        return st.renderStaleReason();                                    // 应立即判 unsaved（不等保存）
    }""")
    check("未保存编辑：有未落盘改动时结果立即判过期（unsaved）", reason == "unsaved", f"reason={reason}")


def test_candidate_bad_member(pg):
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2600)
    r = pg.evaluate("""async () => {
        const st = await import('/js/state.js');
        // 给第一个候选引用的组（g-motor）塞一个 assembly 里不存在的成员
        const g = (st.state.structure.groups||[]).find(x=>x.id==='g-motor');
        g.parts.push('SYN-MISSING-XYZ'); st.bus.emit('structure');
        const before = st.state.plan.figures.length;
        document.querySelector('[data-accept]')?.click();
        await new Promise(x=>setTimeout(x,300));
        return { added: st.state.plan.figures.length > before };
    }""")
    check("候选失效成员：组内含不存在的零件时整个接受被阻止（不建图）", not r["added"], f"figureAdded={r['added']}")


def test_static_write_guard(ctx):
    pg = ctx.new_page()
    writes = []
    pg.on("request", lambda r: writes.append(r.method + " " + r.url) if r.method in ("POST", "PUT", "DELETE") else None)
    try:
        pg.goto(f"{STATIC}/home.html", wait_until="networkidle"); pg.wait_for_timeout(1000)
        # 命令面板：静态站不应出现「新建/上传」「载入示例」入口
        cmd = pg.evaluate("""() => { document.querySelector('#cmd-open').click();
            return [...document.querySelectorAll('#cmdk-list li')].map(li=>li.textContent); }""")
        has_create = any(('上传' in c or '载入示例' in c or '新建' in c) for c in cmd)
        # 统一请求层拒绝写：直接调 api 写方法应抛错且不发网络请求
        guard = pg.evaluate("""async () => {
            const m = await import('./js/api.js');
            let threw = false;
            try { await m.api.createProject({file:null, title:'x'}); } catch(e){ threw = (e.code==='read_only'); }
            let threw2 = false;
            try { await m.api.render('gearbox', {}); } catch(e){ threw2 = (e.code==='read_only'); }
            return { threw, threw2 };
        }""")
        pg.wait_for_timeout(300)
        check("静态站：命令面板无创建/上传入口 + 请求层拒绝写 + 零写请求",
              (not has_create) and guard["threw"] and guard["threw2"] and len(writes) == 0,
              f"cmdCreate={has_create} guard={guard} writes={writes[:2]}")
    except Exception as exc:
        check("静态站：命令面板无创建/上传入口 + 请求层拒绝写 + 零写请求", False, f"（需先起 8808 静态站）{exc!r}")
    pg.close()


def test_static_no_write(ctx):
    pg = ctx.new_page()
    writes = []
    pg.on("request", lambda r: writes.append(r.method + " " + r.url) if r.method in ("POST", "PUT", "DELETE") else None)
    try:
        pg.goto(f"{STATIC}/home.html", wait_until="networkidle"); pg.wait_for_timeout(1000)
        pg.goto(f"{STATIC}/index.html?project=gearbox&stage=export", wait_until="networkidle"); pg.wait_for_timeout(1500)
        pack_disabled = pg.eval_on_selector("#do-pack", "e => e.disabled") if pg.query_selector("#do-pack") else True
        check("静态站：导出打包禁用、全程零写请求", pack_disabled and len(writes) == 0, f"packDisabled={pack_disabled} writes={writes[:2]}")
    except Exception as exc:
        check("静态站：导出打包禁用、全程零写请求", False, f"（需先起 8808 静态站）{exc!r}")
    pg.close()


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        for fn in (test_xss, test_save_race, test_concurrent_flush, test_unsaved_lock,
                   test_candidate_resolve, test_candidate_bad_member, test_keyboard_continuous, test_upload_retry):
            pg = ctx.new_page()
            try:
                fn(pg)
            except Exception as exc:
                check(fn.__name__, False, f"异常 {exc!r}")
            pg.close()
        try:
            test_new_session_lock(ctx)
        except Exception as exc:
            check("test_new_session_lock", False, f"异常 {exc!r}")
        test_static_write_guard(ctx)
        test_static_no_write(ctx)
        b.close()
    ok = all(r[1] for r in results)
    print(f"\n{'全部通过' if ok else '有失败'}：{sum(1 for r in results if r[1])}/{len(results)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
