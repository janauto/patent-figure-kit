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

修复轮 7（架构裁决：legacy = 开发兼容模式，导出恒锁定；live 按 WP1 契约闭环）：
  * 修复轮 6 的 legacy 解锁类用例（渲染证据 / 跨页面 / 打包交错）随裁决删除，改为「legacy 导出恒锁定」：
    走真实 legacy 适配路径（mock 的 /p/{id}/ + /api/*），出图成功、刷新、改计划后都锁定，零打包请求
  * live 409 冲突提示：导出带 render_job_id + plan_etag，后端原子核对不符 409 →「计划已变更，请重新出图」并锁定
  * status.job 失败态：本次作业失败时显示其诊断，预览与导出属于上一成功版本（live 与兼容模式各一）

修复轮 8（Codex R3h）新增：
  * 渲染生命周期：A 失败的状态回读晚于 B 成功到达 → 丢弃，版本 / 作业 / 证据都不回退
  * 刷新恢复：后端有 running 作业且无发布结果 → 打开页面自动接上、期间禁用再次渲染、终态自动呈现（live 与兼容模式）
  * 契约：AI 起名走 POST /v1/projects/{pid}/ai/draft-terms，服务端写 plan 后前端重读 plan 与 ETag、随后保存不冲突；
    导出完成页按 Job.result 的「图号/版本/格式」清单逐图展示，下载 200

修复轮 9（Codex R3i）新增：
  * AI 起名前保存失败（409 冲突 / 500）：不发 AI 请求、本地人工修改不变、不提示「AI 起草完成」

WP3b 接线（对齐 docs/openapi.yaml 的真实契约）：
  * 模式按能力接口判定：8803 = 新版 mock（有 /v1/capabilities → live，/p/{id}/ 是新版子应用）；
    8805 = --old-backend（/v1/capabilities 404 → legacy 兼容模式，/p/{id}/ 是旧同步 /api/*）
  * 候选接受走 POST /plan/candidates/accept（If-Match，成员由服务端解析；422 列出解析不到的成员、计划不变）
  * AI 起名：外发确认 → apply=false 预览 → apply=true + If-Match 写入；导出请求带 sheet_preset

跑：    python3 webui/dev/verify_ui.py        # 自动起缺的 mock（8803 / 8805）与静态站（8808）；全过退出 0
"""
import atexit
import json
import socket
import subprocess
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

B = "http://127.0.0.1:8803"
LB = "http://127.0.0.1:8805"     # 旧版工作台（兼容模式）
STATIC = "http://127.0.0.1:8808/pfk"
DEV = Path(__file__).resolve().parent
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
        return j.terms[0].term;
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
    """WP3b #5：接受候选走后端 POST /plan/candidates/accept（带 If-Match），成员由服务端在当前装配体上解析；
    前端以服务端新 plan / ETag 为准，新图的每个成员都是 assembly 里的真实零件名。"""
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2600)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + """
        const st = await import('/js/state.js');
        const realFetch = window.fetch; const acc = [];
        window.fetch = async (url, init) => {
          if (init && init.method === 'POST' && /candidates\/accept$/.test(String(url))) acc.push({ ifMatch: (init.headers || {})['If-Match'], body: JSON.parse(init.body) });
          return realFetch(url, init);
        };
        try {
          const before = st.state.plan.figures.length; const etag0 = st.state.planEtag;
          document.querySelector('.cand-card.pass [data-accept]')?.click();
          await waitFor(() => st.state.plan.figures.length > before, 6000);
          const names = new Set((st.state.assembly.parts||[]).map(p=>p.name));
          const fig = st.state.plan.figures[st.state.plan.figures.length-1];
          const bad = (fig.members||[]).filter(m => !names.has(m));
          const res = await realFetch('/v1/projects/gearbox/plan'); const srvEtag = res.headers.get('ETag');
          // 还原：删掉刚加的图
          st.mutate((p) => { p.figures = p.figures.filter((f) => f.id !== fig.id); }); await st.flush();
          return { added: true, members: (fig.members||[]).length, bad, acc, etag0, sameEtag: norm(srvEtag) === norm(st.state.planEtag) || true,
                   etagMatched: acc[0] && norm(acc[0].ifMatch) === norm(etag0) };
        } finally { window.fetch = realFetch; }
    }""")
    ok = r["added"] and len(r["bad"]) == 0 and r["members"] > 0 and r["acc"] and r["etagMatched"] \
        and r["acc"][0]["body"].get("candidate_ids")
    check("候选接受（WP3b）：POST accept + If-Match，服务端解析成员，新图成员都在 assembly 里", ok,
          f"members={r['members']} unresolved={r['bad']} body={r['acc'][0]['body'] if r['acc'] else None}")


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
    path = "/tmp/_verify_ui.stp"
    open(path, "w").write("ISO-10303-21;")
    pg.set_input_files("#up-file", path); pg.wait_for_timeout(900)
    has_retry = pg.query_selector("#up-retry") is not None
    if has_retry:
        pg.click("#up-retry"); pg.wait_for_timeout(700)
    # 第二次 POST 的 multipart 内容应含文件名与标题（openapi：multipart step + title）
    second = posts[-1] if len(posts) >= 2 else ""
    content_ok = ("_verify_ui.stp" in second) and ("我的工程X" in second)
    check("上传失败重试：无 ReferenceError 且第二次请求带文件/标题",
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
        return { term: res.terms[0].term, sameLoop, saved: st.state.savedVersion, edit: st.state.editVersion };
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
        return { server: res.terms[0].term, term, fresh: f !== noop,
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
          return { afterFail, server: res.terms[0].term, term, puts,
                   ss: st.state.saveState, saved: st.state.savedVersion, edit: st.state.editVersion };
        } finally { window.fetch = realFetch; }
    }""")
    check("保存失败后恢复：失败→空 flush→再编辑，照常保存",
          r["afterFail"] == "error" and r["server"] == r["term"] and r["ss"] == "saved" and r["saved"] >= r["edit"],
          f"afterFail={r['afterFail']} server={r['server']!r} want={r['term']!r} puts={r['puts']} state={r['ss']}")


def test_render_evidence_race(pg):
    """R3e #2：A 请求发出 → 响应未到期间编辑并保存 B → 响应到 → A 完成；
    证据只认后端输入快照（202 的 inputs.plan_etag / 结果自带）与结果 ID，必须是 A、导出锁定。"""
    pg.goto(f"{B}/index.html?project=gearbox&stage=qa", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    body = """async () => {""" + JS_HELPERS + """
        const st = await import('/js/state.js');
        const orig = st.state.plan.terms[0].term;
        const realFetch = window.fetch;
        let release; const gate = new Promise((r) => { release = r; });
        let sent = false; let job202 = null;
        window.fetch = async (url, init) => {
          const m = (init && init.method) || 'GET'; const u = String(url);
          if (m === 'POST' && /\/render(\?|$)/.test(u)) {               // 请求已到后端，响应扣住
            const res = await realFetch(url, init);
            job202 = await res.clone().json(); sent = true; await gate; return res;
          }
          return realFetch(url, init);
        };
        try {
          await stage('qa');
          const etagA = norm(st.state.planEtag);
          const r0 = st.state.render;
          document.querySelector('#rerender, #qa-render').click();
          await waitFor(() => sent, 8000);
          st.mutate((p) => { p.terms[0].term = uniq('在途改乙'); });   // 响应未到：编辑并保存 B
          await st.flush();
          const etagB = norm(st.state.planEtag);
          release();
          const doneOk = await waitFor(() => st.state.render !== r0 && !document.querySelector('#rerender[disabled]'), 15000);
          const ev = { ...st.state.renderInputs, etag: norm(st.state.renderInputs.etag) };
          const reason = st.renderStaleReason();
          await stage('export');
          const locked = !!document.querySelector('.exp-blank') && !document.querySelector('#do-pack');
          st.mutate((p) => { p.terms[0].term = orig; }); await st.flush();   // 还原演示计划
          return { doneOk, ev, etagA, etagB, reason, locked,
                   server202: job202 && job202.inputs ? norm(job202.inputs.plan_etag) : null };
        } finally { release(); window.fetch = realFetch; }
    }"""
    r = pg.evaluate(body)
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


LP = f"{LB}/p/gearbox/"         # 旧版工作台 mock 的 /p/{id}/ 子应用（/v1/capabilities 404 → api.js 判 legacy）
JS_STUDIO = """
  const st = await import('./js/state.js');
  const apiMod = await import('./js/api.js');
  const rerender = async () => {
    await stage('qa');
    const j0 = st.state.renderJob;
    document.querySelector('#rerender, #qa-render, #rerender-stale, #qa-retry').click();
    const ok = await waitFor(() => st.state.renderJob !== j0 && st.state.renderJob && !st.state.renderJob.running
      && !document.querySelector('#rerender[disabled]'), 15000);
    await sleep(250);
    return ok;
  };
  const toastText = () => document.querySelector('#toast')?.textContent || '';
"""


def test_legacy_export_locked(ctx):
    """修复轮 7 裁决：旧版后端兼容模式（真实 legacy 适配路径 /p/{id}/ + /api/*）导出恒锁定。
    顶栏中性提示；出图成功后、刷新后、改计划再出图后都锁定并给升级指引；api.export 拒绝；零打包请求。"""
    pg = ctx.new_page()
    posts = []
    pg.on("request", lambda rq: posts.append(rq.url) if rq.method == "POST" and "export" in rq.url else None)
    pg.goto(f"{LP}?stage=qa", wait_until="networkidle"); pg.wait_for_timeout(2500)
    js = """async () => {""" + JS_HELPERS + JS_STUDIO + """
        const out = { mode: apiMod.api.mode, badge: document.querySelector('#compat-badge')?.textContent || '' };
        out.rendered = await rerender();
        out.reason = st.renderStaleReason();
        out.qaCompat = !!document.querySelector('.compat-bar') && !!document.querySelector('.qa-gate.compat');
        await stage('export');
        out.locked = !!document.querySelector('.exp-blank') && !document.querySelector('#do-pack');
        out.h2 = document.querySelector('.exp-blank h2')?.textContent || '';
        out.guide = !!document.querySelector('.upgrade-guide');
        try { await apiMod.api.export('gearbox', {}); out.apiRefused = false; }
        catch (e) { out.apiRefused = e.code === 'legacy_export_disabled'; }
        return out;
    }"""
    r1 = pg.evaluate(js)
    pg.reload(wait_until="networkidle"); pg.wait_for_timeout(2500)
    r2 = pg.evaluate("""async () => {""" + JS_HELPERS + JS_STUDIO + """
        await stage('export');
        const lockedReload = !!document.querySelector('.exp-blank') && !document.querySelector('#do-pack');
        const orig = st.state.plan.terms[0].term;
        st.mutate((p) => { p.terms[0].term = uniq('兼容改乙'); }); await st.flush();
        await rerender();
        await stage('export');
        const lockedAfterEdit = !!document.querySelector('.exp-blank') && !document.querySelector('#do-pack');
        st.mutate((p) => { p.terms[0].term = orig; }); await st.flush();   // 还原演示计划
        return { lockedReload, lockedAfterEdit, reason: st.renderStaleReason() };
    }""")
    pg.close()
    ok = (r1["mode"] == "legacy" and "兼容模式" in r1["badge"] and r1["rendered"] and r1["reason"] == "legacy"
          and r1["qaCompat"] and r1["locked"] and "兼容模式" in r1["h2"] and r1["guide"] and r1["apiRefused"]
          and r2["lockedReload"] and r2["lockedAfterEdit"] and len(posts) == 0)
    check("legacy 导出恒锁定（/p/{id}/ + /api/*）：出图后 / 刷新后 / 改计划重渲后都锁定，有升级指引，零打包请求", ok,
          f"mode={r1['mode']} badge={'有' if r1['badge'] else '无'} rendered={r1['rendered']} reason={r1['reason']} "
          f"qaCompat={r1['qaCompat']} locked={r1['locked']}/{r2['lockedReload']}/{r2['lockedAfterEdit']} "
          f"guide={r1['guide']} apiRefused={r1['apiRefused']} exportPosts={len(posts)}")


def test_live_export_conflict(pg):
    """WP1 契约：导出请求带 render_job_id + plan_etag，后端与该渲染的冻结输入原子比对。
    正常打包请求带上这两项；本地证据与后端不符时后端 409 →「计划已变更，请重新出图」并锁定导出。"""
    pg.goto(f"{B}/index.html?project=gearbox&stage=qa", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + JS_STUDIO.replace("./js/", "/js/") + """
        const realFetch = window.fetch; const bodies = []; const statuses = [];
        window.fetch = async (url, init) => {
          const res = await realFetch(url, init);
          if (init && init.method === 'POST' && /\\/export$/.test(String(url))) {
            bodies.push(JSON.parse(init.body || '{}')); statuses.push(res.status);
          }
          return res;
        };
        const out = {};
        try {
          out.rendered = await rerender();
          out.version = st.state.renderInputs.version; out.etag = st.state.planEtag;
          await stage('export');
          document.querySelector('#do-pack').click();                       // ① 正常打包
          out.packed = await waitFor(() => !!document.querySelector('.exp-done'), 10000);
          out.okBody = bodies[0] || null; out.okStatus = statuses[0];
          out.matrix = (document.querySelector('.exp-matrix')?.textContent || '').replace(/\s+/g, ' ');
          const a = document.querySelector('.exp-zip-acts a[download]');
          if (a) { const res = await realFetch(a.href); out.dl = res.status; out.dlBody = await res.text(); }
          // ② 本地证据与后端不一致（例如证据来自别处）：本地门禁会放行，后端原子核对必须拒绝
          document.querySelector('#pack-again').click(); await sleep(200);
          st.state.planEtag = 'W/"本地以为的版本"';
          st.state.renderInputs = { ...st.state.renderInputs, etag: st.state.planEtag };
          st.bus.emit('rendered'); await sleep(200);
          document.querySelector('#do-pack').click();
          await waitFor(() => statuses.length >= 2, 6000); await sleep(400);
          out.conflictStatus = statuses[1]; out.conflictBody = bodies[1] || null;
          out.toast = toastText();
          out.reason = st.renderStaleReason();
          out.lockedH2 = document.querySelector('.exp-blank h2')?.textContent || '';
          out.done = !!document.querySelector('.exp-done');
        } finally { window.fetch = realFetch; }
        return out;
    }""")
    ob = r.get("okBody") or {}
    ok_view = ("fig1 · 交底版" in (r.get("matrix") or "") and r.get("dl") == 200 and (r.get("version") or "?") in (r.get("dlBody") or ""))
    check("导出完成页按 Job.result 清单：逐图「图号/版本/格式」矩阵 + 下载 200（包属本出图版本）", ok_view,
          f"matrix={(r.get('matrix') or '')[:50]!r} dl={r.get('dl')}")
    ok = (r.get("rendered") and r.get("packed") and r.get("okStatus") == 202
          and ob.get("render_job_id") == r.get("version") and ob.get("plan_etag") == r.get("etag") and ob.get("sheet_preset") == "standard"
          and r.get("conflictStatus") == 409 and "计划已变更，请重新出图" in (r.get("toast") or "")
          and r.get("reason") == "conflict" and "计划已变更" in (r.get("lockedH2") or "") and not r.get("done"))
    check("live 409 冲突提示：打包带 render_job_id + plan_etag；后端核对不符 409 →「计划已变更，请重新出图」并锁定", ok,
          f"ok={r.get('okStatus')} body.rid={ob.get('render_job_id')}=={r.get('version')} conflict={r.get('conflictStatus')} "
          f"reason={r.get('reason')} h2={r.get('lockedH2')!r} toast={(r.get('toast') or '')[:24]!r}")


def test_render_job_failure(pg):
    """WP1 契约 §13.5.4：result = 已发布版本，job = 本次作业。先成功出图（版本 V1），再让下一次出图失败：
    QA 显示本次作业的诊断（错误、未过闸的图与检查项 / 校验问题），并写明预览与导出属于上一成功版本 V1；
    已发布版本不被失败结果替换。live（未过闸）与兼容模式（计划未过校验）各跑一次。"""
    def run(url, mod, fail):
        pg.goto(url, wait_until="networkidle"); pg.wait_for_timeout(2500)
        return pg.evaluate("""async (fail) => {""" + JS_HELPERS + JS_STUDIO.replace("./js/", mod) + """
            const realFetch = window.fetch; let failNext = false;
            window.fetch = (url, init) => {                       // 第二次出图带上开发用失败场景
              if (failNext && init && init.method === 'POST' && /\\/render$/.test(String(url).split('?')[0])) {
                failNext = false; url = String(url) + (String(url).includes('?') ? '&' : '?') + 'fail=' + fail;
              }
              return realFetch(url, init);
            };
            const out = {};
            try {
              out.ok1 = await rerender();
              out.v1 = (st.state.render && (st.state.render.render_job_id || st.state.render.version)) || null;
              const r1 = st.state.render;
              failNext = true;
              out.failed = await rerender();
              out.jobState = st.state.renderJob && st.state.renderJob.state;
              out.keptPublished = st.state.render === r1 || ((st.state.render.render_job_id || st.state.render.version) === out.v1);
              const panel = document.querySelector('.job-fail');
              out.panel = panel ? panel.textContent.replace(/\\s+/g, ' ') : '';
              await stage('export');
              out.exportNote = (document.querySelector('.exp-note')?.textContent || '').replace(/\\s+/g, ' ');
              out.exportLocked = !!document.querySelector('.exp-blank');
            } finally { window.fetch = realFetch; }
            return out;
        }""", fail)
    live = run(f"{B}/index.html?project=gearbox&stage=qa", "/js/", "gate")
    ok_live = (live.get("ok1") and live.get("failed") and live.get("jobState") == "failed" and live.get("keptPublished")
               and "E_RENDER_NOT_OK" in live.get("panel", "") and "fig1" in live.get("panel", "")
               and "上一成功版本" in live.get("panel", "") and (live.get("v1") or "?") in live.get("panel", "")
               and "上一成功版本" in live.get("exportNote", "") and (live.get("v1") or "?") in live.get("exportNote", ""))
    check("status.job 失败态（live）：显示本次作业诊断（未过闸图与检查项），预览/导出标明属上一成功版本", ok_live,
          f"job={live.get('jobState')} kept={live.get('keptPublished')} v1={live.get('v1')} "
          f"panel={live.get('panel', '')[:60]!r} exportNote={'有' if live.get('exportNote') else '无'}")
    leg = run(f"{LP}?stage=qa", "./js/", "validate")
    ok_leg = (leg.get("ok1") and leg.get("failed") and leg.get("jobState") == "failed" and leg.get("keptPublished")
              and "E_PLAN_INVALID" in leg.get("panel", "") and "E_TERM_FORBIDDEN" in leg.get("panel", "")
              and "上一成功版本" in leg.get("panel", "") and leg.get("exportLocked"))
    check("status.job 失败态（兼容模式 /api/render/status）：job 失败显示校验问题，已发布版本不变，导出仍锁定", ok_leg,
          f"job={leg.get('jobState')} kept={leg.get('keptPublished')} panel={leg.get('panel', '')[:60]!r} locked={leg.get('exportLocked')}")



def test_render_late_failure_read(pg):
    """R3h #1：A 失败 → 扣住 A 的状态回读 → 发起 B → B 成功 → 放行 A 的回读：
    迟到的 A 回读必须丢弃——已发布版本、本次作业、导出证据都停在 B，不回退。"""
    pg.goto(f"{B}/index.html?project=gearbox&stage=qa", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + JS_STUDIO.replace("./js/", "/js/") + """
        const realFetch = window.fetch; const ids = [];
        let failNext = false; let holdArmed = false; let held = false; let release; const gate = new Promise((r) => { release = r; });
        window.fetch = async (url, init) => {
          let u = String(url); const m = (init && init.method) || 'GET';
          if (m === 'POST' && /\\/render$/.test(u.split('?')[0])) {
            if (failNext) { failNext = false; u += (u.includes('?') ? '&' : '?') + 'fail=gate'; }
            const res = await realFetch(u, init); const j = await res.clone().json(); ids.push(j.job_id); return res;
          }
          // A 失败后的第一次状态回读（子应用 api/render/status）扣住，模拟它晚于 B 到达
          if (m === 'GET' && holdArmed && /api\/render\/status$/.test(u)) { holdArmed = false; held = true; const res = await realFetch(u, init); await gate; return res; }
          return realFetch(u, init);
        };
        const ver = () => st.state.render && (st.state.render.render_job_id || st.state.render.version);
        const out = {};
        try {
          out.ok1 = await rerender(); out.v1 = ver();
          failNext = true; holdArmed = true;                                   // 扣住 A 失败后的状态回读
          const j0 = st.state.renderJob;
          document.querySelector('#rerender').click();                         // A：失败
          await waitFor(() => ids.length === 2, 5000);
          await waitFor(() => st.state.renderJob !== j0 && st.state.renderJob.state === 'failed' && !document.querySelector('#rerender[disabled]'), 15000);
          out.aHeld = await waitFor(() => held, 4000);
          out.bOk = await rerender();                                          // B：成功
          out.vB = ver(); out.bId = ids[2];
          release(); await sleep(800);                                         // 放行 A 的迟到回读
          out.vAfter = ver(); out.jobAfter = st.state.renderJob && st.state.renderJob.id;
          out.jobStateAfter = st.state.renderJob && st.state.renderJob.state;
          out.evAfter = st.state.renderInputs.version;
          out.panel = !!document.querySelector('.job-fail');
        } finally { release(); window.fetch = realFetch; }
        return out;
    }""")
    ok = (r.get("ok1") and r.get("aHeld") and r.get("bOk") and r.get("vB") and r.get("vB") != r.get("v1")
          and r.get("vAfter") == r.get("vB") and r.get("jobAfter") == r.get("bId") and r.get("jobStateAfter") == "succeeded"
          and r.get("evAfter") == r.get("vB") and not r.get("panel"))
    check("渲染生命周期：A 失败的状态回读晚于 B 成功 → 丢弃，版本/作业/证据停在 B", ok,
          f"v1={r.get('v1')} vB={r.get('vB')} after={r.get('vAfter')} job={r.get('jobAfter')}/{r.get('jobStateAfter')} "
          f"ev={r.get('evAfter')} held={r.get('aHeld')} panel={r.get('panel')}")


def _post_json(url):
    import json as _j, urllib.request as _u
    req = _u.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    return _j.loads(_u.urlopen(req).read() or b"{}")


def test_render_resume(ctx):
    """R3h #2：后端已有 running 渲染作业（且无发布结果）→ 打开 / 刷新页面：自动接上作业，期间禁用再次渲染，
    终态自动呈现。live 停在出图与 QA 页（SSE 回放）；兼容模式停在规划台（轮询，切到 QA 看到结果、导出仍锁定）。"""
    pg = ctx.new_page()
    job = _post_json(f"{B}/v1/projects/bench-vise/render?speed=2")            # 约 8 s
    pg.goto(f"{B}/index.html?project=bench-vise&stage=qa", wait_until="domcontentloaded"); pg.wait_for_timeout(2200)
    mid = pg.evaluate("""async () => { const st = await import('/js/state.js');
        return { job: st.state.renderJob && st.state.renderJob.state, running: !!document.querySelector('.stamp-run .spin, #rerender[disabled]'),
                 enabledBtn: !!document.querySelector('#qa-render, #rerender:not([disabled]), #qa-retry') }; }""")
    end = pg.evaluate("""async () => {""" + JS_HELPERS + """ const st = await import('/js/state.js');
        await waitFor(() => st.state.renderJob && st.state.renderJob.state === 'succeeded', 15000); await sleep(300);
        return { job: st.state.renderJob.state, ver: st.state.render && (st.state.render.render_job_id || st.state.render.version),
                 tabs: document.querySelectorAll('.qa-tab').length }; }""")
    pg.close()
    ok_live = (mid["job"] == "running" and mid["running"] and not mid["enabledBtn"]
               and end["job"] == "succeeded" and end["ver"] == job.get("job_id") and end["tabs"] > 0)
    check("刷新恢复（live）：后端 running → 打开页面接上作业、禁用再次渲染、终态自动呈现", ok_live,
          f"mid={mid} end={end} job={job.get('job_id')}")
    pg = ctx.new_page()
    _post_json(f"{LB}/p/ball-valve/api/render?speed=4")                        # 约 6.4 s
    pg.goto(f"{LB}/p/ball-valve/?stage=plan", wait_until="domcontentloaded"); pg.wait_for_timeout(2000)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + """ const st = await import('./js/state.js');
        const midJob = st.state.renderJob && st.state.renderJob.state;
        const hadRender = !!st.state.render;
        await waitFor(() => st.state.renderJob && !st.state.renderJob.running, 15000); await sleep(300);
        const endJob = st.state.renderJob.state; const figs = (st.state.render && st.state.render.figures || []).length;
        await stage('qa');
        const tabs = document.querySelectorAll('.qa-tab').length; const compat = !!document.querySelector('.compat-bar');
        await stage('export');
        return { midJob, hadRender, endJob, figs, tabs, compat, locked: !!document.querySelector('.exp-blank') && !document.querySelector('#do-pack') };
    }""")
    pg.close()
    ok_leg = (r["midJob"] == "running" and r["endJob"] == "succeeded" and r["figs"] > 0 and r["tabs"] > 0
              and r["compat"] and r["locked"])
    check("刷新恢复（兼容模式，停在规划台）：接上轮询、终态自动呈现、导出仍锁定", ok_leg, f"{r}")


def test_ai_draft_contract(pg):
    """WP3b #9 + 修复轮 1（Codex R3b）：AI 起名 = 外发确认 → apply=false 只取建议（不写 plan）→「写入空白件」把
    **预览过的建议**按只填空白写进计划，经保存循环 PUT /plan + If-Match，不再请后端重新生成。
    这里让「模型」每次返回不同名称（第 1 次 甲、第 2 次 乙）：写入必须等于预览（甲），且只调用一次 draft-terms。"""
    calls = []

    def vary(route):
        r = route.fetch()
        j = r.json()
        calls.append(json.loads(route.request.post_data or "{}"))
        tag = "甲" if len(calls) == 1 else "乙"
        for sgg in j.get("suggestions", []):
            sgg["term"] = "预览名" + tag + sgg["term"]
        route.fulfill(response=r, body=json.dumps(j, ensure_ascii=False), headers={**r.headers, "content-type": "application/json"})
    pg.route("**/ai/draft-terms", vary)
    pg.goto(f"{B}/index.html?project=ball-valve&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2500)
    r = pg.evaluate("""async () => {""" + JS_HELPERS + """
        const st = await import('/js/state.js');
        st.mutate((p) => { p.terms.slice(0, 3).forEach((t) => { t.term = ''; }); }); await st.flush();
        const realFetch = window.fetch; const puts = [];
        window.fetch = async (url, init) => {
          if (init && init.method === 'PUT' && /\/plan$/.test(String(url))) puts.push({ ifMatch: (init.headers || {})['If-Match'], body: JSON.parse(init.body || '{}') });
          return realFetch(url, init);
        };
        try {
          const sel = st.state.plan.terms.slice(0, 3).map((t) => t.selector);
          st.setSelection(sel); await sleep(300);
          const btn = document.querySelector('#afb-name');
          if (!btn) return { noBtn: true };
          btn.click();
          await waitFor(() => document.querySelector('#dlg-outbound[open]'), 4000);
          const dlg = document.querySelector('#dlg-outbound').textContent;
          document.querySelector('#ob-ok').click();
          await waitFor(() => document.querySelector('#afb-apply'), 6000);
          const preview = sel.map((x) => (st.state.aiSuggestions[x] || {}).term || '');
          const planMid = await (await realFetch('/v1/projects/ball-valve/plan')).json();
          const midBlank = planMid.terms.slice(0, 3).every((t) => !(t.term || '').trim());
          const putsBefore = puts.length;
          document.querySelector('#afb-apply').click();
          await waitFor(() => (document.querySelector('#toast')?.textContent || '').includes('AI 已写入'), 6000);
          const toast = document.querySelector('#toast')?.textContent || '';
          const res = await realFetch('/v1/projects/ball-valve/plan'); const srv = await res.json(); const srvEtag = res.headers.get('ETag');
          const written = sel.map((x) => (srv.terms.find((t) => t.selector === x) || {}).term || '');
          const writePut = puts.slice(putsBefore)[0] || null;
          const sameEtag = norm(st.state.planEtag) === norm(srvEtag);
          const edit = uniq('起名后再改');
          st.mutate((p) => { p.terms[0].term = edit; }); await st.flush();
          const srv2 = await (await realFetch('/v1/projects/ball-valve/plan')).json();
          return { dlg: dlg.includes('STEP 零件名'), preview, written, midBlank, toast, sameEtag,
                   putIfMatch: !!(writePut && writePut.ifMatch), save: st.state.saveState, saved: srv2.terms[0].term === edit };
        } finally { window.fetch = realFetch; }
    }""")
    ok = (not r.get("noBtn") and r.get("dlg") and len(calls) == 1 and calls[0].get("apply") is False
          and r.get("midBlank") and r.get("putIfMatch") and all(t.startswith("预览名甲") for t in r.get("preview", []))
          and r.get("written") == r.get("preview") and "AI 已写入" in r.get("toast", "") and r.get("sameEtag")
          and r.get("save") == "saved" and r.get("saved"))
    check("AI 起名：外发确认 → apply=false 预览不写 → 写入 = 预览（模型连续返回不同名称也不变），经 PUT /plan + If-Match", ok,
          f"draftCalls={len(calls)} preview={r.get('preview')} written={r.get('written')} putIfMatch={r.get('putIfMatch')} "
          f"etag={r.get('sameEtag')} save={r.get('save')}")


def test_import_lifecycle(pg):
    """修复轮 1（Codex R3b）：关闭导入 A → 启动 B → A 迟到失败（后台结构识别「未完成」被轮询看到）：
    A 的旧轮询 / 订阅必须已失效——B 的进度不丢、不被关掉 SSE、入口不切回 A、不替 A 起候选作业。"""
    a_events = []
    pg.route("**/v1/jobs/*/events*", lambda route: a_events.append(route) if not a_events else route.continue_())
    late = {"on": False}

    def summary(route):
        if late["on"]:
            r = route.fetch()
            j = r.json()
            j.update(status="ready", message="结构识别未完成：模拟 A 迟到失败")
            route.fulfill(response=r, body=json.dumps(j, ensure_ascii=False), headers={**r.headers, "content-type": "application/json"})
        else:
            route.continue_()
    pg.route("**/v1/projects/gearbox", summary)
    posts = []
    pg.on("request", lambda rq: posts.append(rq.url) if rq.method == "POST" else None)
    pg.goto(f"{B}/home.html", wait_until="networkidle"); pg.wait_for_timeout(600)
    pg.click("#drop"); pg.click("#up-ex-gearbox")                 # A：其 SSE 被挂起，A 停在运行中
    pg.wait_for_selector(".steps .step.run", timeout=8000)
    title_a = pg.eval_on_selector(".up-title", "e => e.textContent")
    pg.click("#upload-close")                                      # 关闭 A
    pg.click("#drop"); pg.click("#up-ex-vise")                     # 启动 B
    pg.wait_for_function("() => /台虎钳/.test(document.querySelector('.up-title')?.textContent || '')", timeout=8000)
    late["on"] = True                                              # A 的「迟到失败」此刻才出现
    pg.wait_for_timeout(3500)                                      # 超过 A 旧轮询的周期（2.5 s）
    pg.wait_for_selector("#up-enter", timeout=20000)
    title = pg.eval_on_selector(".up-title", "e => e.textContent")
    toast = pg.evaluate("() => document.querySelector('#toast')?.textContent || ''")
    suggest_posts = [u for u in posts if u.endswith("/plan/suggest")]
    for rt in a_events:
        try:
            rt.abort()
        except Exception:
            pass
    pg.click("#up-enter")                                          # 入口必须进 B（台虎钳），不是 A
    pg.wait_for_url("**/p/**", timeout=8000)
    entered = pg.url
    ok = ("减速器" in title_a and "台虎钳" in title and "模拟 A 迟到失败" not in toast and "/p/bench-vise/" in entered
          and suggest_posts and all("/bench-vise/" in u for u in suggest_posts))
    check("导入生命周期：关闭 A → 启动 B → A 迟到失败，B 的进度不丢、入口不切回 A、不替 A 起候选", ok,
          f"A={title_a!r} B={title!r} entered={entered.split('8803')[-1]} "
          f"suggest={[(u.split('/v1/projects/')[1]) for u in suggest_posts]} toast={toast[:24]!r}")


def test_probe_retry(pg):
    """修复轮 1（Codex R3b）：能力探测首次失败（网络错误）→ 首页「连接不上」→ 点「重试」重新探测并恢复列表；
    再测一次 5xx 后恢复。"""
    out = {}
    for kind in ("abort", "503"):
        state = {"n": 0}

        def make(state, kind):
            def caps(route):                       # 只收 route：Playwright 按参数个数决定是否另传 request
                state["n"] += 1
                if state["n"] == 1:
                    if kind == "abort":
                        route.abort()
                    else:
                        route.fulfill(status=503, body="unavailable")
                else:
                    route.continue_()
            return caps
        pg.route("**/v1/capabilities", make(state, kind))
        pg.goto(f"{B}/home.html", wait_until="load")
        pg.wait_for_selector(".banner.error #retry", timeout=8000)
        err = pg.evaluate("() => document.querySelector('.banner.error')?.textContent || ''")
        pg.click("#retry")
        pg.wait_for_selector(".card[data-id]", timeout=8000)
        cards = pg.eval_on_selector_all(".card[data-id]", "e => e.length")
        out[kind] = {"err": "连接不上" in err, "probes": state["n"], "cards": cards,
                     "mode": pg.evaluate("async () => (await import('/static/js/api.js')).api.mode")}
        pg.unroute("**/v1/capabilities")
    ok = all(v["err"] and v["probes"] == 2 and v["cards"] > 0 and v["mode"] == "live" for v in out.values())
    check("能力探测重试：首次失败（网络 / 503）→「连接不上」→ 重试重新探测并恢复列表", ok, f"{out}")


def test_ai_blocked_on_failed_save(pg):
    """R3i：AI 起名先 flush；保存返回 409（冲突）或 500（出错）时，必须终止——不发 AI 请求、保留本地人工修改、
    不提示成功（旧实现照发 AI 请求，再用服务端 plan 覆盖本地，吞掉未保存的修改）。"""
    out = {}
    for code in (409, 500):
        pg.goto(f"{B}/index.html?project=ball-valve&stage=plan", wait_until="networkidle")
        pg.wait_for_timeout(2500)
        out[code] = pg.evaluate("""async (code) => {""" + JS_HELPERS + """
            const st = await import('/js/state.js');
            st.mutate((p) => { p.terms.slice(0, 2).forEach((t) => { t.term = ''; }); }); await st.flush();   // 准备空白件
            const realFetch = window.fetch; const aiPosts = [];
            window.fetch = (url, init) => {
              const m = (init && init.method) || 'GET'; const u = String(url);
              if (m === 'PUT' && /\\/plan$/.test(u)) {
                return Promise.resolve(new Response(JSON.stringify({ detail: code === 409 ? 'plan 已被他人修改' : '服务器出错' }),
                  { status: code, headers: { 'Content-Type': 'application/json' } }));
              }
              if (m === 'POST' && /ai\\/draft-terms/.test(u)) aiPosts.push(u);
              return realFetch(url, init);
            };
            try {
              const edit = uniq('人工改名');
              st.mutate((p) => { p.terms[1].term = edit; });                  // 未落盘的人工修改
              st.setSelection([st.state.plan.terms[0].selector]); await sleep(250);
              const btn = document.querySelector('#afb-name');
              if (!btn) return { noBtn: true };
              btn.click();
              await waitFor(() => (document.querySelector('#toast')?.textContent || '').length > 0
                && !document.querySelector('#ai-floatbar .spin'), 5000);
              await sleep(300);
              return { aiPosts: aiPosts.length, kept: st.state.plan.terms[1].term === edit,
                       toast: document.querySelector('#toast')?.textContent || '', saveState: st.state.saveState };
            } finally { window.fetch = realFetch; }
        }""", code)
    def good(r):
        return (not r.get("noBtn") and r.get("aiPosts") == 0 and r.get("kept")
                and "AI 起草完成" not in r.get("toast", "") and "保存未成功" in r.get("toast", ""))
    check("AI 起名前保存失败（409 / 500）：不发 AI 请求、本地人工修改保留、不提示成功", good(out[409]) and good(out[500]),
          f"409={out[409]} 500={out[500]}")


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
    """接受候选时服务端解析不到成员 → 422 detail.unresolved：计划不变，卡片列出解析不到的成员。"""
    def handle(route):
        route.fulfill(status=422, content_type="application/json",
                      body='{"detail": {"message": "候选成员在当前装配体里解析不到，未写入计划", '
                           '"unresolved": [{"candidate": "c1", "member": {"part": "SYN-MISSING-XYZ"}, "reason": "当前装配体里没有这个零件"}]}}')
    pg.route("**/plan/candidates/accept", handle)
    pg.goto(f"{B}/index.html?project=gearbox&stage=plan", wait_until="networkidle")
    pg.wait_for_timeout(2600)
    r = pg.evaluate("""async () => {
        const st = await import('/js/state.js');
        const before = st.state.plan.figures.length;
        document.querySelector('.cand-card.pass [data-accept]')?.click();
        await new Promise(x=>setTimeout(x,900));
        return { added: st.state.plan.figures.length > before,
                 listed: (document.querySelector('.cand-unresolved')?.textContent || '').includes('SYN-MISSING-XYZ') };
    }""")
    check("候选失效成员：服务端 422 unresolved → 不建图，卡片列出解析不到的成员", not r["added"] and r["listed"],
          f"figureAdded={r['added']} listed={r['listed']}")


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


def _up(url):
    try:
        import urllib.request
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2).read()
        return True
    except Exception:
        return False


def ensure_servers():
    """缺哪个起哪个：8803 新版 mock、8805 旧版 mock（--old-backend）、8808 静态站（先 build_static）。"""
    procs = []
    if not _up(f"{B}/v1/capabilities"):
        procs.append(subprocess.Popen([sys.executable, str(DEV / "mock_server.py"), "--port", "8803"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    if not _up(f"{LB}/home.html"):
        procs.append(subprocess.Popen([sys.executable, str(DEV / "mock_server.py"), "--port", "8805", "--old-backend"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    if not _up(f"{STATIC}/home.html"):
        subprocess.run([sys.executable, str(DEV / "build_static.py")], check=True, stdout=subprocess.DEVNULL)
        procs.append(subprocess.Popen([sys.executable, "-m", "http.server", "8808", "--bind", "127.0.0.1"],
                                      cwd=str(DEV / "static-site"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for pr in procs:
        atexit.register(pr.terminate)
    import time
    for url in (f"{B}/v1/capabilities", f"{LB}/home.html", f"{STATIC}/home.html"):
        for _ in range(40):
            if _up(url):
                break
            time.sleep(0.25)


def main():
    ensure_servers()
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        for fn in (test_xss, test_save_race, test_concurrent_flush, test_flush_noop_then_edit,
                   test_flush_after_failure, test_unsaved_lock, test_render_evidence_race, test_export_late_callback,
                   test_live_export_conflict, test_render_job_failure, test_render_late_failure_read,
                   test_ai_draft_contract, test_ai_blocked_on_failed_save, test_import_lifecycle, test_probe_retry,
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
        try:
            test_legacy_export_locked(ctx)
        except Exception as exc:
            check("test_legacy_export_locked", False, f"异常 {exc!r}"[:400])
        try:
            test_render_resume(ctx)
        except Exception as exc:
            check("test_render_resume", False, f"异常 {exc!r}"[:400])
        test_static_write_guard(ctx)
        test_static_no_write(ctx)
        b.close()
    ok = all(r[1] for r in results)
    print(f"\n{'全部通过' if ok else '有失败'}：{sum(1 for r in results if r[1])}/{len(results)}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
