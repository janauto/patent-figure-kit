#!/usr/bin/env python3
"""WP3 前端回归用例（对 mock 跑）。覆盖 Codex R3 / R3b / R3c 的高危修复：
  * XSS：恶意零件名 / 图题 / AI reason / 结构组名 / 无效计划 ID 不得变成可执行 HTML
  * 保存竞态：慢保存期间连续编辑，服务端最终收到最新版（不吞编辑）
  * 结果版本权威：改计划保存后「新会话」里旧结果不解锁导出
  * 候选成员契约：接受候选后每个成员都能在 assembly 中解析
  * 键盘金路径：连续真实按键完成多件选择 + 归图（不脚本补焦点）；术语单元格键盘可选
  * 上传失败重试：保留文件，第二次请求确实带上文件名/标题/AI 开关
  * 静态站：只读，不发任何写请求（POST/PUT/DELETE）

修复轮 5（Codex R3e）新增：
  * 空循环 flush：无改动时 flush 一次后再编辑，必须照常保存；保存失败后再 flush、再编辑也必须恢复
  * 渲染证据捕获时机：A 请求发出→响应未到期间编辑并保存 B→A 完成，证据仍是 A、导出锁定（改回 A 也不解锁）
  * 导出迟到回调：打包 A 期间改存 B→A 完成→B 重渲过闸→回导出页不显示 A 的包；取清单被扣住时同理

修复轮 6（Codex R3f）新增——走真实 legacy 适配路径（mock 的 /p/{id}/ + /api/* 子应用，不再只是把 ETag 置空）：
  * legacy 渲染证据：A 请求在途期间改存 B，证据仍为 A、判 raced、导出锁定
  * legacy 跨页面：A 完成并持久化 → 改存 B 起渲染 → 改回 A → 关页 → B 完成后重进，导出锁定；
    正常刷新仍可凭「钉在结果身份上的证明」解锁；另一页面重新出图后，本页旧内存证据不能打包
  * legacy 打包交错：A 打包在途 → 改计划重渲 B → 启动 B 打包 → B 响应先回、A 响应后回，展示与下载均为 B

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


# 页内公共工具：等待条件、生成不含「字母+数字」形状的唯一中文术语（避免触发 mock 的件号校验）
JS_HELPERS = """
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const waitFor = async (fn, ms = 15000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { if (await fn()) return true; await sleep(80); }
    return false;
  };
  const uniq = (p) => p + String(Date.now()).slice(-6).split('').map((d) => '零一二三四五六七八九'[d]).join('');
  const stage = async (name) => { document.querySelector(`.stage-tab[data-stage="${name}"]`).click(); await sleep(450); };
  const norm = (e) => (e ? String(e).trim().replace(/^W\\//, '').replace(/^"(.*)"$/, '$1') : null);
"""


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


def test_flush_noop_then_edit(pg):
    """R3e #1：干净计划上 flush 一次（空循环）后，再编辑必须照常保存（旧实现在此永久毒化保存循环）。"""
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + """
        const st = await import('/js/state.js');
        const noop = st.flush();                       // 无待保存内容：首次检查即退出
        await noop;
        const term = uniq('空刷后编辑');
        st.mutate((p) => { p.terms[0].term = term; });
        const f = st.flush();
        await Promise.race([f, sleep(5000)]);
        const res = await (await fetch('/v1/projects/gearbox/plan')).json();
        return { server: res.plan.terms[0].term, term, fresh: f !== noop,
                 saved: st.state.savedVersion, edit: st.state.editVersion, ss: st.state.saveState };
    }""")
    check("空循环 flush：无改动 flush 后再编辑，照常保存到服务端",
          r["server"] == r["term"] and r["fresh"] and r["saved"] >= r["edit"],
          f"server={r['server']!r} want={r['term']!r} newLoop={r['fresh']} saved={r['saved']} edit={r['edit']} state={r['ss']}")


def test_flush_after_failure(pg):
    """R3e #1：保存失败 → 无新编辑时再 flush（旧实现在此毒化）→ 再编辑 → 必须恢复保存。"""
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + """
        const st = await import('/js/state.js');
        const realFetch = window.fetch; let failNext = true; let puts = 0;
        window.fetch = (url, init) => {
          if (init && init.method === 'PUT' && /\/plan$/.test(String(url))) {
            puts += 1;
            if (failNext) {
              failNext = false;
              return Promise.resolve(new Response(JSON.stringify({ error: { code: 'boom', message: '模拟保存失败' } }),
                { status: 500, headers: { 'Content-Type': 'application/json' } }));
            }
          }
          return realFetch(url, init);
        };
        try {
          st.mutate((p) => { p.terms[0].term = uniq('失败前'); });
          await Promise.race([st.flush(), sleep(5000)]);
          const afterFail = st.state.saveState;
          await Promise.race([st.flush(), sleep(5000)]);   // 失败后、无新编辑时再 flush
          const term = uniq('失败后编辑');
          st.mutate((p) => { p.terms[0].term = term; });
          await Promise.race([st.flush(), sleep(5000)]);
          const res = await (await realFetch('/v1/projects/gearbox/plan')).json();
          return { afterFail, server: res.plan.terms[0].term, term, puts,
                   ss: st.state.saveState, saved: st.state.savedVersion, edit: st.state.editVersion };
        } finally { window.fetch = realFetch; }
    }""")
    check("保存失败后恢复：失败→空 flush→再编辑，照常保存",
          r["afterFail"] == "error" and r["server"] == r["term"] and r["ss"] == "saved" and r["saved"] >= r["edit"],
          f"afterFail={r['afterFail']} server={r['server']!r} want={r['term']!r} puts={r['puts']} state={r['ss']}")


def test_render_evidence_race(pg):
    """R3e #2（live 分支）：A 请求发出 → 响应未到期间编辑并保存 B → 响应到 → A 完成；
    证据只认后端输入快照（202 的 inputs.plan_etag / 结果自带），必须是 A、导出锁定。
    legacy 分支走真实 legacy 适配路径，见 test_legacy_render_evidence_race。"""
    pg.goto(f"{B}/index.html?project=gearbox&stage=qa", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    body = """async (mode) => {""" + JS_HELPERS + """
        const st = await import('/js/state.js');
        const orig = st.state.plan.terms[0].term;
        if (mode === 'legacy') st.state.planEtag = null;        // 旧后端：无 ETag，走输入摘要
        const realFetch = window.fetch;
        let release; const gate = new Promise((r) => { release = r; });
        let sent = false; let job202 = null;
        window.fetch = async (url, init) => {
          const m = (init && init.method) || 'GET'; const u = String(url);
          if (mode === 'legacy' && m === 'PUT' && /\/plan$/.test(u)) {   // 旧后端保存：200、无 ETag
            return new Response(JSON.stringify({ ok: true, validate: { ok: true, issues: [] } }),
              { status: 200, headers: { 'Content-Type': 'application/json' } });
          }
          if (m === 'POST' && /\/render(\?|$)/.test(u)) {               // 请求已到后端，响应扣住
            const res = await realFetch(url, init);
            job202 = await res.clone().json(); sent = true; await gate; return res;
          }
          return realFetch(url, init);
        };
        try {
          await stage('qa');
          const etagA = norm(st.state.planEtag); const digestA = st.planDigest();
          const r0 = st.state.render;
          document.querySelector('#rerender, #qa-render').click();
          await waitFor(() => sent, 8000);
          st.mutate((p) => { p.terms[0].term = uniq('在途改乙'); });   // 响应未到：编辑并保存 B
          await st.flush();
          const digestB = st.planDigest(); const etagB = norm(st.state.planEtag);
          release();
          const doneOk = await waitFor(() => st.state.render !== r0 && !document.querySelector('#rerender[disabled]'), 15000);
          const ev = { ...st.state.renderInputs, etag: norm(st.state.renderInputs.etag) };
          const reason = st.renderStaleReason();
          await stage('export');
          const locked = !!document.querySelector('.exp-blank') && !document.querySelector('#do-pack');
          // 再改回 A 的内容并保存：legacy 下旧作业不得因摘要「碰巧相同」而解锁
          st.mutate((p) => { p.terms[0].term = orig; }); await st.flush();
          const reasonRevert = st.renderStaleReason();
          return { mode, doneOk, ev, etagA, etagB, digestA, digestB, reason, locked, reasonRevert,
                   server202: job202 && job202.inputs ? norm(job202.inputs.plan_etag) : null };
        } finally { release(); window.fetch = realFetch; }
    }"""
    r = pg.evaluate(body, "live")
    ok_live = (r["doneOk"] and r["ev"]["etag"] == r["etagA"] and r["server202"] == r["etagA"]
               and r["etagA"] != r["etagB"] and r["reason"] == "changed" and r["locked"])
    check("渲染证据（live ETag）：以后端输入快照为准，请求在途期间改存 B，证据仍为 A、导出锁定", ok_live,
          f"done={r['doneOk']} ev={r['ev']['etag']} A={r['etagA']} 202={r['server202']} reason={r['reason']} locked={r['locked']}")


def test_export_late_callback(pg):
    """R3e #3：打包 A 期间编辑保存 B → A 完成 → B 重渲通过 → 回导出页，不得显示 A 的包；
    再测更晚的回调：打包 B 的清单请求被扣住，期间改存 C 并重渲过闸，放行后也不得发布 B 的包。"""
    pg.goto(f"{B}/index.html?project=gearbox&stage=qa", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + """
        const st = await import('/js/state.js');
        const orig = st.state.plan.terms[0].term;
        const realFetch = window.fetch; const posts = [];
        let holdGet = null; let getHeld = false;
        window.fetch = async (url, init) => {
          const m = (init && init.method) || 'GET'; const u = String(url);
          const res = await realFetch(url, init);
          if (m === 'POST' && /\/export$/.test(u)) posts.push(await res.clone().json());
          if (m === 'GET' && /\/v1\/exports\/[^/]+$/.test(u) && holdGet) { getHeld = true; await holdGet; }
          return res;
        };
        const rerender = async () => {
          await stage('qa');
          const r0 = st.state.render;
          document.querySelector('#rerender, #qa-render').click();
          await waitFor(() => st.state.render !== r0 && !document.querySelector('#rerender[disabled]'), 15000);
          await sleep(200);
          return st.renderStaleReason();
        };
        const jobDone = (id) => waitFor(async () => (await (await realFetch('/v1/jobs/' + id)).json()).state === 'succeeded', 15000);
        const out = {};
        try {
          // ① 当前计划 A 出图过闸 → 打包 A
          out.gateA = await rerender();
          await stage('export');
          if (!document.querySelector('#do-pack')) { out.noPack = 'A'; return out; }
          document.querySelector('#do-pack').click();
          await waitFor(() => posts.length === 1, 8000);
          // ② 打包期间编辑并保存 B
          st.mutate((p) => { p.terms[0].term = uniq('打包中改乙'); }); await st.flush();
          // ③ A 打包作业在服务端完成，迟到的完成回调到达
          out.aDone = await jobDone(posts[0].job_id); await sleep(900);
          out.doneShownAfterA = !!document.querySelector('.exp-done');
          // ④ B 重新出图并过闸 → ⑤ 回导出页
          out.gateB = await rerender();
          await stage('export');
          out.doneShownB = !!document.querySelector('.exp-done');
          out.formB = !!document.querySelector('.exp-wrap');
          out.packEnabledB = !!document.querySelector('#do-pack:not([disabled])');
          // ⑥ 更晚的回调：打包 B，取清单时扣住；改存 C 并重渲过闸后再放行
          let releaseGet; holdGet = new Promise((r) => { releaseGet = r; });
          if (!document.querySelector('#do-pack')) { out.noPack = 'B'; return out; }
          document.querySelector('#do-pack').click();
          out.getHeld = await waitFor(() => getHeld, 12000);
          st.mutate((p) => { p.terms[0].term = uniq('取单时改丙'); }); await st.flush();
          out.gateC = await rerender();
          releaseGet(); holdGet = null; await sleep(600);
          await stage('export');
          out.doneShownC = !!document.querySelector('.exp-done');
          out.formC = !!document.querySelector('.exp-wrap');
          out.packEnabledC = !!document.querySelector('#do-pack:not([disabled])');
        } finally {
          window.fetch = realFetch;
          st.mutate((p) => { p.terms[0].term = orig; }); await st.flush();
        }
        return out;
    }""")
    ok1 = (r.get("gateA") is None and r.get("aDone") and not r.get("doneShownAfterA") and r.get("gateB") is None
           and not r.get("doneShownB") and r.get("formB") and r.get("packEnabledB"))
    check("导出迟到回调：打包 A 期间改存 B→A 完成→B 重渲过闸→回导出页，不显示 A 的包", ok1,
          f"gateA={r.get('gateA')} aDone={r.get('aDone')} shownAfterA={r.get('doneShownAfterA')} gateB={r.get('gateB')} "
          f"shownB={r.get('doneShownB')} form={r.get('formB')} packEnabled={r.get('packEnabledB')} noPack={r.get('noPack')}")
    ok2 = (r.get("getHeld") and r.get("gateC") is None and not r.get("doneShownC") and r.get("formC") and r.get("packEnabledC"))
    check("导出迟到回调：取清单被扣住期间改存 C 并重渲过闸，放行后不发布 B 的包", ok2,
          f"held={r.get('getHeld')} gateC={r.get('gateC')} shownC={r.get('doneShownC')} form={r.get('formC')} packEnabled={r.get('packEnabledC')}")


LP = f"{B}/p/gearbox/"          # mock 的 legacy 子应用入口（api.js 判 legacy：/api/* 同步接口、无 ETag）
JS_LEGACY = """
  const st = await import('./js/state.js');
  const apiMod = await import('./js/api.js');
  const proofKey = 'inkline-render-proof-gearbox';
  const rerender = async () => {
    await stage('qa');
    const r0 = st.state.render;
    document.querySelector('#rerender, #qa-render').click();
    const ok = await waitFor(() => st.state.render !== r0 && !document.querySelector('#rerender[disabled]'), 15000);
    await sleep(200);
    return ok;
  };
"""


def legacy_idle():
    """等 mock legacy 子应用上的渲染跑完（关页后在服务端继续的那一轮）。"""
    import json as _j, time as _t, urllib.request as _u
    for _ in range(80):
        st = _j.loads(_u.urlopen(f"{LP}api/render/status").read())
        if not st.get("running"):
            return True
        _t.sleep(0.15)
    return False


def test_legacy_render_evidence_race(pg):
    """R3f #3 + R3e #2（真实 legacy 适配路径）：A 渲染请求已到后端、响应未回时编辑并保存 B，
    A 完成后证据必须是 A、判 raced、导出锁定；改回 A 也不解锁，且不留持久化证明。"""
    pg.goto(f"{LP}?stage=qa", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + JS_LEGACY + """
        const orig = st.state.plan.terms[0].term;
        const realFetch = window.fetch;
        let release; const gate = new Promise((r) => { release = r; }); let sent = false;
        window.fetch = async (url, init) => {
          const m = (init && init.method) || 'GET';
          if (m === 'POST' && /\\/api\\/render$/.test(String(url))) {
            const res = await realFetch(url, init); sent = true; await gate; return res;
          }
          return realFetch(url, init);
        };
        try {
          await stage('qa');
          const mode = apiMod.api.mode; const etag = st.state.planEtag;
          const digestA = st.planDigest(); const r0 = st.state.render;
          document.querySelector('#rerender, #qa-render').click();
          await waitFor(() => sent, 8000);
          st.mutate((p) => { p.terms[0].term = uniq('旧后端在途改乙'); }); await st.flush();
          const digestB = st.planDigest();
          release();
          const doneOk = await waitFor(() => st.state.render !== r0 && !document.querySelector('#rerender[disabled]'), 15000);
          const evDigest = st.state.renderInputs.digest; const reason = st.renderStaleReason();
          await stage('export');
          const locked = !!document.querySelector('.exp-blank') && !document.querySelector('#do-pack');
          st.mutate((p) => { p.terms[0].term = orig; }); await st.flush();
          await stage('export');
          const reasonRevert = st.renderStaleReason();
          const lockedRevert = !!document.querySelector('.exp-blank');
          return { mode, etag, doneOk, evIsA: evDigest === digestA, aNeB: digestA !== digestB, reason, locked,
                   reasonRevert, lockedRevert, proof: localStorage.getItem(proofKey) };
        } finally { release(); window.fetch = realFetch; }
    }""")
    ok = (r["mode"] == "legacy" and r["etag"] is None and r["doneOk"] and r["evIsA"] and r["aNeB"]
          and r["reason"] == "raced" and r["locked"] and r["reasonRevert"] is not None and r["lockedRevert"]
          and r["proof"] is None)
    check("legacy 渲染证据（/p/{id}/ + /api/*）：请求在途改存 B，证据仍为 A、raced、锁定，改回 A 也不解锁", ok,
          f"mode={r['mode']} done={r['doneOk']} evIsA={r['evIsA']} reason={r['reason']} locked={r['locked']} "
          f"revert={r['reasonRevert']}/{r['lockedRevert']} proof={'有' if r['proof'] else '无'}")


def test_legacy_cross_page(ctx):
    """R3f #2：跨页面证据。① 正常：A 完成 → 刷新仍解锁（证明钉在结果身份上）；
    ② A 完成并持久化 → 改存 B 起渲染 → 改回 A → 关页 → B 完成后重进：必须锁定；
    ③ 另一页面改计划并重新出图后，本页的旧内存证据不能打包（零 /api/export 请求）。"""
    out = {}
    p1 = ctx.new_page()
    p1.goto(f"{LP}?stage=qa", wait_until="networkidle"); p1.wait_for_timeout(2500)
    out.update(p1.evaluate("""async () => {""" + JS_HELPERS + JS_LEGACY + """
        localStorage.removeItem(proofKey);
        const orig = st.state.plan.terms[0].term;
        const ok = await rerender();
        return { aRendered: ok, reasonA: st.renderStaleReason(), orig };
    }"""))
    p1.reload(wait_until="networkidle"); p1.wait_for_timeout(2500)
    out.update(p1.evaluate("""async () => {""" + JS_HELPERS + JS_LEGACY + """
        await stage('export');
        return { reasonReload: st.renderStaleReason(), unlockedReload: !!document.querySelector('#do-pack:not([disabled])') };
    }"""))
    # ② 改存 B → 起渲染（后端读到 B）→ 改回 A → 完成前关页
    out.update(p1.evaluate("""async (orig) => {""" + JS_HELPERS + JS_LEGACY + """
        const planA = st.planDigest();
        st.mutate((p) => { p.terms[0].term = uniq('跨页改乙'); }); await st.flush();
        await stage('qa');
        document.querySelector('#rerender, #qa-render, #rerender-stale').click();
        const started = await waitFor(async () => (await (await fetch('api/render/status')).json()).running, 5000);
        await sleep(150);
        st.mutate((p) => { p.terms[0].term = orig; }); await st.flush();          // 改回 A（与持久化证明同一份计划）
        return { bStarted: started, proofDuringB: !!localStorage.getItem(proofKey), backToA: st.planDigest() === planA };
    }""", out.get("orig")))
    p1.close()
    out["bDone"] = legacy_idle()
    p2 = ctx.new_page()
    p2.goto(f"{LP}?stage=export", wait_until="networkidle"); p2.wait_for_timeout(2500)
    out.update(p2.evaluate("""async () => {""" + JS_HELPERS + JS_LEGACY + """
        return { reasonReenter: st.renderStaleReason(), lockedReenter: !!document.querySelector('.exp-blank') };
    }"""))
    ok2 = (out.get("aRendered") and out.get("reasonA") is None
           and out.get("reasonReload") is None and out.get("unlockedReload"))
    check("legacy 跨页面①：A 完成后刷新，证明与结果身份一致仍解锁（不误锁）", ok2,
          f"rendered={out.get('aRendered')} reasonA={out.get('reasonA')} "
          f"reload={out.get('reasonReload')}/{out.get('unlockedReload')}")
    ok3 = (out.get("bStarted") and out.get("backToA") and out.get("bDone")
           and out.get("reasonReenter") is not None and out.get("lockedReenter"))
    check("legacy 跨页面②：A 持久化→改存 B 起渲染→改回 A→关页→B 完成后重进，导出锁定", ok3,
          f"bStarted={out.get('bStarted')} backToA={out.get('backToA')} proofDuringB={out.get('proofDuringB')} "
          f"bDone={out.get('bDone')} reenter={out.get('reasonReenter')} locked={out.get('lockedReenter')}")
    # ③ 本页（p2）重新出图解锁；另一页（p3）改计划并重新出图；回到 p2 打包必须被拒且不发打包请求
    p2.evaluate("""async () => {""" + JS_HELPERS + JS_LEGACY + """ await rerender(); await stage('export'); }""")
    p3 = ctx.new_page()
    p3.goto(f"{LP}?stage=qa", wait_until="networkidle"); p3.wait_for_timeout(2500)
    p3.evaluate("""async () => {""" + JS_HELPERS + JS_LEGACY + """
        st.mutate((p) => { p.terms[0].term = uniq('别页改丙'); }); await st.flush(); await rerender();
    }""")
    exports = []
    p2.on("request", lambda rq: exports.append(rq.url) if rq.method == "POST" and rq.url.endswith("/api/export") else None)
    r3 = p2.evaluate("""async () => {""" + JS_HELPERS + JS_LEGACY + """
        const before = !!document.querySelector('#do-pack:not([disabled])');
        document.querySelector('#do-pack')?.click();
        await sleep(1500);
        return { packEnabledBefore: before, done: !!document.querySelector('.exp-done') };
    }""")
    p3.evaluate("""async (orig) => {""" + JS_HELPERS + JS_LEGACY + """
        st.mutate((p) => { p.terms[0].term = orig; }); await st.flush();          // 还原演示计划
    }""", out.get("orig"))
    p2.close(); p3.close()
    ok4 = r3["packEnabledBefore"] and not r3["done"] and len(exports) == 0
    check("legacy 跨页面③：另一页面改计划并重新出图后，本页旧证据不能打包（零打包请求）", ok4,
          f"packEnabledBefore={r3['packEnabledBefore']} done={r3['done']} exportPosts={len(exports)}")


def test_legacy_export_interleave(pg):
    """R3f #1：A 打包在途 → 改计划重渲 B → 启动 B 打包 → B 响应先回、A 响应在 B 的完成回调之前回；
    展示与下载都必须是 B 的包（旧实现共享 _lastExport，B 会读到 A 的清单并带上有效票据）。"""
    pg.goto(f"{LP}?stage=qa", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + JS_LEGACY + """
        const orig = st.state.plan.terms[0].term;
        const realFetch = window.fetch; const zips = [];
        let releaseA; const gateA = new Promise((r) => { releaseA = r; }); let aHeld = false; let aReleased = false;
        window.fetch = async (url, init) => {
          const m = (init && init.method) || 'GET';
          const res = await realFetch(url, init);
          if (m === 'POST' && /\\/api\\/export$/.test(String(url))) {
            zips.push((await res.clone().json()).zip);
            if (zips.length === 1) { aHeld = true; await gateA; }      // A 的响应扣住
          }
          return res;
        };
        const origJobEvents = apiMod.api.jobEvents;
        const out = {};
        try {
          out.aRendered = await rerender();
          await stage('export');
          document.querySelector('#do-pack').click();                    // ① 打包 A（响应被扣住）
          await waitFor(() => aHeld, 8000);
          st.mutate((p) => { p.terms[0].term = uniq('打包在途改乙'); }); await st.flush();   // ② 改计划
          out.bRendered = await rerender();                              // ③ 重渲 B
          out.gateB = st.renderStaleReason();
          // ④ B 的响应先回；在 B 的完成回调之前放行 A 的响应，让 A 的适配器先处理完
          apiMod.api.jobEvents = (id, h) => {
            if (!aReleased && String(id).startsWith('legacy:export')) {
              aReleased = true; releaseA();
              setTimeout(() => origJobEvents.call(apiMod.api, id, h), 500);
              return { close() {} };
            }
            return origJobEvents.call(apiMod.api, id, h);
          };
          await stage('export');
          document.querySelector('#do-pack').click();                    // ⑤ 打包 B
          out.shown = await waitFor(() => !!document.querySelector('.exp-done'), 8000);
          await sleep(300);
          out.zipA = zips[0]; out.zipB = zips[1];
          out.shownZip = document.querySelector('.exp-zip b.mono')?.textContent || null;
          const a = document.querySelector('.exp-zip-acts a[download]');
          out.href = a ? a.getAttribute('href') : null;
          if (a) { const res = await realFetch(a.href); out.dlStatus = res.status; out.dlBody = await res.text(); }
        } finally {
          releaseA(); apiMod.api.jobEvents = origJobEvents; window.fetch = realFetch;
          st.mutate((p) => { p.terms[0].term = orig; }); await st.flush();
        }
        return out;
    }""")
    zb = r.get("zipB") or "?"
    ok = (r.get("aRendered") and r.get("bRendered") and r.get("gateB") is None and r.get("shown")
          and r.get("zipA") and r.get("zipA") != zb and r.get("shownZip") == zb
          and (r.get("href") or "").endswith(zb) and r.get("dlStatus") == 200 and zb in (r.get("dlBody") or ""))
    check("legacy 打包交错：B 响应先回、A 后回，展示与下载均为 B 的包", ok,
          f"A={r.get('zipA')} B={r.get('zipB')} shown={r.get('shownZip')} href={r.get('href')} dl={r.get('dlStatus')} gateB={r.get('gateB')}")


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
        for fn in (test_xss, test_save_race, test_concurrent_flush, test_flush_noop_then_edit,
                   test_flush_after_failure, test_unsaved_lock, test_render_evidence_race, test_export_late_callback,
                   test_legacy_render_evidence_race, test_legacy_export_interleave,
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
        lctx = b.new_context(viewport={"width": 1440, "height": 900})   # 独立 localStorage：跨页面证据用例
        try:
            test_legacy_cross_page(lctx)
        except Exception as exc:
            check("test_legacy_cross_page", False, f"异常 {exc!r}"[:400])
        lctx.close()
        test_static_write_guard(ctx)
        test_static_no_write(ctx)
        b.close()
    ok = all(r[1] for r in results)
    print(f"\n{'全部通过' if ok else '有失败'}：{sum(1 for r in results if r[1])}/{len(results)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
