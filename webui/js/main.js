// 规划台装配入口：顶栏、三屏切换（规划 / 出图与 QA / 导出）、键盘金路径、AI 起名浮条。
// three.js 只在进入「规划台」时动态 import()，首页零 three.js（硬约束）。
// 启动先 await api.init()：按能力接口判定 live / legacy（WP3b #1），之后才读工程。
import { boot, bus, state, setSelection, flush, setView, displayName, mutate, fillBlankTerms } from './state.js';
import { api, readOnly, pagePid } from './api.js';
import { applyBrand } from './brand.js';
import { esc } from './esc.js';

const $ = (s, el = document) => el.querySelector(s);
const params = new URLSearchParams(location.search);
const forceState = params.get('state') || '';     // empty|loading|ok|fail（四态截图用，仅开发）
let projectId = '';
let viewer = null;
let viewerMod = null;
let plansMounted = false;

applyBrand();

export function toast(msg, kind = '', ms = 3600) {
  const box = $('#toast');
  box.className = `toast ${kind}`;
  box.textContent = msg;
  box.hidden = false;
  clearTimeout(box._t);
  box._t = setTimeout(() => { box.hidden = true; }, ms);
}

// 页面顶部的常驻提示条（鉴权弃用提示等；可关闭）
export function notice(msg, id = 'page-notice') {
  let bar = document.getElementById(id);
  if (!bar) {
    bar = document.createElement('div');
    bar.id = id; bar.className = 'page-notice'; bar.setAttribute('role', 'status');
    document.body.prepend(bar);
  }
  bar.innerHTML = `<span>${esc(msg)}</span><button class="icon-btn" aria-label="关闭提示">✕</button>`;
  bar.querySelector('button').addEventListener('click', () => bar.remove());
}

// ---------------------------------------------------------------- 主题
(function theme() {
  let saved = null;
  try { saved = localStorage.getItem('inkline-theme'); } catch { /* */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme === 'dark' ? '' : 'dark';
    if (cur) document.documentElement.dataset.theme = cur;
    else document.documentElement.removeAttribute('data-theme');
    try { localStorage.setItem('inkline-theme', cur); } catch { /* */ }
  });
})();

// ---------------------------------------------------------------- 顶栏
function homeHref() {
  if (api.isLegacy) {
    const base = state.homeUrl || '/';
    const tok = api.isLegacy ? (new URLSearchParams(location.search).get('token')
      || (() => { try { return sessionStorage.getItem('studio-token') || ''; } catch { return ''; } })()) : '';
    return (base.includes('token=') || !tok) ? base : `${base}${base.includes('?') ? '&' : '?'}token=${encodeURIComponent(tok)}`;
  }
  if (api.mode === 'static') return './home.html';
  // live：工作台首页在根上（真实后端 / → home.html；mock 的 / 也重定向到 home.html）。不带 token（只用 Cookie）。
  return api.root;
}

function renderToolbar() {
  $('#tb-proj').textContent = `${state.title || projectId} · ${state.step || ''}`;
  const homeLink = $('.tb-home');
  if (homeLink) homeLink.href = homeHref();
  const llm = state.llm || {};
  const badge = $('#ai-badge');
  if (!llm.configured) { badge.classList.add('off'); badge.textContent = 'AI 未接入'; badge.title = 'AI 起名可选；未接入时金路径照常走，零件名在术语页手填'; }
  else { badge.classList.remove('off'); badge.textContent = `AI · ${llm.provider_label || '已接入'}`; badge.title = 'AI 只起中文名，编号与版面由程序发'; }
  const sv = {
    clean: '', dirty: '未保存…', saving: '保存中…', saved: '已保存 ✓',
    error: '保存失败', conflict: 'plan 冲突，请重载', readonly: '只读演示站',
  }[state.saveState] || '';
  const el = $('#tb-save');
  el.innerHTML = state.saveState === 'conflict'
    ? `${esc(sv)} <button class="btn-min" id="tb-reload" title="计划已在别处被修改：重载最新计划（本页未保存的改动会丢失）">重载</button>`
    : esc(sv);
  el.className = `tb-save ${['error', 'conflict'].includes(state.saveState) ? 'bad' : ''}`;
  $('#tb-reload')?.addEventListener('click', () => location.reload());
}

// ---------------------------------------------------------------- 屏切换
function showStage(stage) {
  document.querySelectorAll('.stage-tab').forEach((b) => b.classList.toggle('on', b.dataset.stage === stage));
  document.querySelectorAll('#stage-host > section').forEach((s) => { s.hidden = s.dataset.stage !== stage; });
  if (stage === 'plan' && !plansMounted) mountPlan();
  if (stage === 'qa') import('./qa.js').then((m) => m.mountQa($('#qa-host')));
  if (stage === 'export') import('./export.js').then((m) => m.mountExport($('#export-host')));
  // legacy 入口是 /p/{id}/?token=，工程在挂载里、token 在 URL——绝不能重写掉（否则新开标签 401）
  if (!api.isLegacy) {
    try {                                 // 保留 state / static 开关（四态截图与静态站用）
      const keep = new URLSearchParams(location.search);
      if (!pagePid) keep.set('project', projectId);           // /p/{pid}/ 下工程在路径里，不重复写
      keep.set('stage', stage);
      if (forceState) keep.set('state', forceState); else keep.delete('state');
      keep.delete('token');
      history.replaceState(null, '', `${location.pathname}?${keep.toString()}`);
    } catch { /* */ }
  } else {
    try { history.replaceState(null, '', location.pathname + location.search); } catch { /* */ }
  }
}
export function goStage(stage) { showStage(stage); }

function initStageNav() {
  $('#stage-nav').addEventListener('click', (e) => {
    const btn = e.target.closest('.stage-tab');
    if (btn) showStage(btn.dataset.stage);
  });
}

// ---------------------------------------------------------------- 规划台（屏③）
async function mountPlan() {
  plansMounted = true;
  const { initParts } = await import('./parts.js');
  const { initFigures } = await import('./figures.js');
  const { initTerms } = await import('./terms.js');
  const { initCandidates } = await import('./candidates.js');
  initCandidates($('#candidates'));
  initFigures($('#figures'));
  initTerms($('#terms'), $('#params'));   // 参数页：layout.view / density 等（QA 修复入口指向这里）
  initParts($('#parts'));
  // 方法流程图：子应用 /api/flowcharts*（真实后端有；mock / 静态站无此后端，给出可达但诚实的说明）
  if (await api.probeFlowcharts(projectId)) {
    import('./flowcharts.js').then((m) => m.initFlowcharts($('#flows')));
  } else {
    $('#flows').innerHTML = `<div class="pane-note"><p class="stamp-run">需要后端</p>
      <p>方法专利的流程图（S101 步骤号）由工作台后端生成，${api.mode === 'static' ? '静态演示站' : '当前后端'}未提供——在本机安装的工作台里可用。</p></div>`;
  }
  initRightTabs();
  initAiFloatbar();
  bus.emit('booted');   // 让各面板首渲

  if (forceState === 'loading') { showViewportSkeleton(); return; }
  if (forceState === 'fail') { showViewportError('模型（GLB）解码失败：文件缺失或损坏'); return; }
  // 动态载入 three.js + viewer（首屏之外的 1.2MB 就挡在这里）
  showViewportSkeleton();
  try {
    viewerMod = await import('./viewer.js');
    viewer = await viewerMod.initViewer($('#viewport'));
    clearViewportOverlay();
    initViewTools();
  } catch (err) {
    showViewportError('3D 视口初始化失败：' + err.message);
  }
}

function showViewportSkeleton() {
  $('#viewport-overlay').innerHTML = `<div class="vp-skeleton">
    <i class="spin"></i><p>正在载入 3D 模型…<br><span class="hint">可交互骨架优先，模型就位后可拖转</span></p></div>`;
  $('#viewport-overlay').hidden = false;
}
function showViewportError(msg) {
  $('#viewport-overlay').innerHTML = `<div class="vp-error">
    <p class="stamp-fail">视口</p><p>${esc(msg)}</p>
    <button class="btn" onclick="location.reload()">重试</button></div>`;
  $('#viewport-overlay').hidden = false;
}
function clearViewportOverlay() { $('#viewport-overlay').hidden = true; $('#viewport-overlay').innerHTML = ''; }

function initRightTabs() {
  document.querySelectorAll('.rtab').forEach((btn) => btn.addEventListener('click', () => openRightTab(btn.dataset.tab)));
}
export function openRightTab(tab) {
  document.querySelectorAll('.rtab').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
  document.querySelectorAll('.rpane').forEach((p) => { p.hidden = p.dataset.pane !== tab; });
}

// 修复动作的按钮文案（fix_action.kind 冻结为四种）
export function fixLabel(fa) {
  return ({
    open_terms: fa?.target?.focus === 'standard_parts' ? '去术语页把标准件设「不标」'
      : (fa?.target?.focus === 'names' ? '去术语页改名称' : '去术语页'),
    split_figure: '去图纸页拆分本图', change_view: '去参数页换视角',
    change_density: fa?.target?.engineering_table === false ? '去参数页关掉工程明细表' : '去参数页调疏密',
  })[fa?.kind] || '去修改';
}
// 排版阶段的失败（渲染器还没走到 13 项 QA 就停了）：后端只给错误码（候选的 qa.error_code、出图日志里的
// 「figure <id> 渲染失败 [E_…]」），没有结构化 fix_action。这里按错误码给白话与修复直达（与后端建议同义）。
export const LAYOUT_ERRORS = {
  E_LAYOUT_UNSOLVABLE: { title: '排版放不下', plain: '这张图的图形在纸上铺不开（太扁、太长或字太小），旋转也救不回来。换一个视角，或把零件拆到两张图里。',
    fixes: [{ kind: 'change_view' }, { kind: 'split_figure' }] },
  E_LABELS_UNPLACEABLE: { title: '标号放不下', plain: '标号太多，找不到互不交叉、不压线的位置。把标准件设为不标号，或把这张图拆开。',
    fixes: [{ kind: 'open_terms', target: { focus: 'standard_parts' } }, { kind: 'split_figure' }] },
};
// 从出图日志里取出「figure fig3 渲染失败 [E_LAYOUT_UNSOLVABLE]：…」→ {fig: {code, detail}}
export function layoutFailures(log) {
  const out = {};
  String(log || '').split('\n').forEach((line) => {
    const m = /figure\s+(\S+)\s+渲染失败\s+\[(E_[A-Z_]+)\][：:]\s*(.*)$/.exec(line.trim());
    if (m) out[m[1]] = { code: m[2], detail: m[3] };
  });
  return out;
}

// QA 修复动作直达（WP3b #7）：fix_action {kind, target{figure, focus?, density?}} → 规划台对应页签 + 控件聚焦。
// kind 冻结为四种：open_terms / split_figure / change_view / change_density。返回落点控件（便于高亮与测试）。
export async function applyFixAction(fa) {
  if (!fa || !fa.kind) return null;
  showStage('plan');
  await new Promise((r) => setTimeout(r, 80));
  const t = fa.target || {};
  const { setActiveFigure } = await import('./state.js');
  if (t.figure && (state.plan?.figures || []).some((f) => f.id === t.figure)) setActiveFigure(t.figure);
  let el = null;
  if (fa.kind === 'open_terms') {
    openRightTab('terms');
    await new Promise((r) => setTimeout(r, 30));
    // focus=standard_parts：落到第一个「不标」候选（标准件）的标注下拉；否则第一个空术语输入框；再否则第一行
    const rows = [...document.querySelectorAll('#terms tbody tr')];
    if (t.focus === 'standard_parts') {
      const std = rows.find((tr) => /SCREW|BOLT|NUT|WASHER|PIN|BEARING|螺|垫|销|轴承/i.test(tr.querySelector('.sel')?.textContent || ''));
      el = (std || rows[0])?.querySelector('.label-sel') || null;
    } else if (t.focus === 'names') {
      el = document.querySelector('#terms tr.row-err .term-input') || document.querySelector('#terms .term-input');
    } else {
      el = document.querySelector('#terms .term-input[value=""]') || document.querySelector('#terms .term-input');
    }
  } else if (fa.kind === 'split_figure') {
    openRightTab('figures');
    await new Promise((r) => setTimeout(r, 30));
    const card = t.figure ? document.querySelector(`#figures .fig-card[data-fig="${CSS.escape(t.figure)}"]`) : null;
    el = card?.querySelector('.fig-caption') || document.querySelector('#figures .fig-caption');
    card?.scrollIntoView({ block: 'nearest' });
  } else if (fa.kind === 'change_view') {
    openRightTab('params');
    await new Promise((r) => setTimeout(r, 30));
    el = document.querySelector('#params [data-k="view"]');
  } else if (fa.kind === 'change_density') {
    openRightTab('params');
    await new Promise((r) => setTimeout(r, 30));
    el = t.engineering_table === false
      ? document.querySelector('#params [data-k="engineering_table"]')
      : document.querySelector('#params [data-k="density"]');
  }
  if (el) {
    el.focus();
    el.classList.add('fix-target');
    setTimeout(() => el.classList.remove('fix-target'), 2400);
  }
  return el;
}

function initViewTools() {
  const op = $('#v-opacity'); const ex = $('#v-explode');
  const mode = $('#v-mode'); const col = $('#v-color');
  const sync = () => {
    op.value = Math.round(state.view.opacity * 100);
    $('#v-opacity-out').textContent = `${op.value}%`;
    ex.value = Math.round(state.view.explode * 100);
    $('#v-explode-out').textContent = `${ex.value}%`;
    mode.value = state.view.explodeMode;
    col.value = state.view.colorBy;
  };
  op.addEventListener('input', () => setView({ opacity: Number(op.value) / 100 }));
  ex.addEventListener('input', () => setView({ explode: Number(ex.value) / 100 }));
  ex.addEventListener('change', () => viewer?.frameAll());
  mode.addEventListener('change', () => { setView({ explodeMode: mode.value }); viewer?.frameAll(); });
  col.addEventListener('change', () => setView({ colorBy: col.value }));
  $('#v-reset').addEventListener('click', () => {
    setView({ opacity: 1, explode: 0, hiddenGroups: new Set(), soloGroup: null });
    viewer?.frameAll();
  });
  document.querySelectorAll('[data-preset]').forEach((b) => b.addEventListener('click',
    () => viewerMod?.setPreset(b.dataset.preset)));
  $('#btn-frame').addEventListener('click', () => viewer?.frameAll());
  $('#btn-isolate').addEventListener('change', (e) => viewer?.setIsolate(e.target.checked));
  $('#btn-box').addEventListener('click', () => toggleBox());
  bus.on('view', sync);
  sync();
}

function toggleBox() {
  if (!viewerMod) return;
  const on = !viewerMod.isBoxMode();
  viewerMod.setBoxMode(on);
  const btn = $('#btn-box');
  btn.classList.toggle('on', on);
  $('#box-hint').hidden = !on;
}

// ---------------------------------------------------------------- AI 外发确认（F9 / 结构识别共用）
// 外发前明示：发什么（能力接口 ai.outbound_fields / outbound_note）、发给谁（provider_label）。返回 true = 用户同意。
const FIELD_LABEL = { part_names: 'STEP 零件名', part_codes: '零件代号（常含内部件号）', bbox: '包围盒尺寸',
  paths: '装配路径', parts_list: '完整零件清单' };
export function confirmOutbound({ title = 'AI 起名会外发以下内容', count = 0, extra = '' } = {}) {
  return new Promise((resolve) => {
    let dlg = document.getElementById('dlg-outbound');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.id = 'dlg-outbound'; dlg.className = 'dlg';
      document.body.appendChild(dlg);
    }
    const llm = state.llm || {};
    const fields = (llm.outbound_fields || []).map((f) => FIELD_LABEL[f] || f);
    dlg.innerHTML = `<form method="dialog" class="dlg-body">
      <h2 class="dlg-title"><span class="seal">外发</span>${esc(title)}</h2>
      <p>发送给：<b>${esc(llm.provider_label || '已配置的大模型通道')}</b>${count ? `，本次 <b>${count}</b> 个零件` : ''}</p>
      ${fields.length ? `<ul class="outbound-fields">${fields.map((f) => `<li>${esc(f)}</li>`).join('')}</ul>` : ''}
      <p class="muted">${esc(llm.outbound_note || '几何模型本身不出本机。')}</p>
      ${extra ? `<p class="hint">${esc(extra)}</p>` : ''}
      <div class="dlg-actions">
        <button value="cancel" class="btn ghost" id="ob-cancel">取消</button>
        <button value="ok" class="btn primary" id="ob-ok">同意并发送</button>
      </div></form>`;
    const done = (v) => { dlg.close(); resolve(v); };
    dlg.querySelector('#ob-ok').addEventListener('click', (e) => { e.preventDefault(); done(true); });
    dlg.querySelector('#ob-cancel').addEventListener('click', (e) => { e.preventDefault(); done(false); });
    dlg.addEventListener('cancel', () => resolve(false), { once: true });
    dlg.showModal();
  });
}

// ---------------------------------------------------------------- AI 起名浮条（F9，WP3b #9）
// live：① 先落盘本地改动（保存失败绝不外发）② 外发确认 ③ apply=false 只取建议（不写 plan）、在术语页标出把握
// ④ 用户点「写入空白件」→ 再落盘一次，把预览过的建议按「只填空白」写进计划，经保存循环 PUT /plan + If-Match。
// 写入的是预览过的建议本身（PUT /plan + If-Match，不再 apply=true 让后端重新生成——Codex R3b）。
// 没有 AI 通道：服务端 200 ai_not_configured → 中性提示，不报错。legacy 维持旧的一步式（服务端起名并写入）。
let aiPending = null;          // {selectors, suggestions, etag0}：已取到、待写入的建议
function initAiFloatbar() {
  bus.on('selection', updateFloatbar);
  updateFloatbar();
}
function saveBlocked() {
  return state.saveState === 'error' || state.saveState === 'conflict' || state.editVersion > state.savedVersion;
}
function blockedToast() {
  toast(state.saveState === 'conflict'
    ? '保存未成功（计划已在别处被修改），请先解决冲突再让 AI 起名——本地改动已保留。'
    : '保存未成功，请先解决保存问题再让 AI 起名——本地改动已保留。', 'bad');
}
function updateFloatbar() {
  const bar = $('#ai-floatbar');
  const sel = [...state.selection];
  const blanks = sel.filter((n) => !displayName(n));
  if (aiPending) { paintPending(bar); return; }
  if (!sel.length) { bar.hidden = true; return; }
  const llmOk = state.llm?.configured;
  bar.hidden = false;
  bar.innerHTML = `
    <span class="afb-mark">AI</span>
    <span class="afb-text">已选 <b>${sel.length}</b> 件${blanks.length ? ` · <b>${blanks.length}</b> 件待起名` : '，均已命名'}</span>
    ${blanks.length && llmOk && !readOnly ? `<button class="btn primary sm" id="afb-name" title="只对选中的空白件给建议；写入时只填空白、不动人工填过的">AI 为空白件起名</button>`
      : (!llmOk ? '<span class="hint" id="afb-off">AI 未接入，可在术语页手填</span>' : '')}
    <button class="btn ghost sm" id="afb-clear">清空选择</button>`;
  $('#afb-clear')?.addEventListener('click', () => setSelection([]));
  $('#afb-name')?.addEventListener('click', () => aiNameSelection(blanks));
}
function paintPending(bar) {
  const n = aiPending.suggestions.length;
  const low = aiPending.suggestions.filter((s) => s.confidence === 'low').length;
  bar.hidden = false;
  bar.innerHTML = `<span class="afb-mark">AI</span>
    <span class="afb-text">AI 建议了 <b>${n}</b> 个名称${low ? `（<b>${low}</b> 个把握低，术语页已标黄）` : ''}——尚未写入计划</span>
    <ul class="afb-list">${aiPending.suggestions.slice(0, 6).map((s) => `<li><span class="mono">${esc(s.selector)}</span> → <b>${esc(s.term)}</b>
      <i class="ai-mark ai-${esc(s.confidence)}">${esc({ high: '高', medium: '中', low: '低' }[s.confidence] || s.confidence)}</i></li>`).join('')}
      ${n > 6 ? `<li class="hint">…另 ${n - 6} 个</li>` : ''}</ul>
    <button class="btn primary sm" id="afb-apply" ${n ? '' : 'disabled'}>写入空白件</button>
    <button class="btn ghost sm" id="afb-discard">放弃</button>`;
  $('#afb-apply')?.addEventListener('click', applyAiSuggestions);
  $('#afb-discard')?.addEventListener('click', () => { aiPending = null; updateFloatbar(); });
}

async function aiNameSelection(blanks) {
  const bar = $('#ai-floatbar');
  const btn = bar.querySelector('#afb-name');
  if (btn) btn.outerHTML = '<span class="hint" id="afb-busy"><i class="spin"></i> 先保存本地改动…</span>';
  try {
    await flush();                                   // 先落盘本地改动
    // 保存没成功（409 冲突 / 500 等）或还有未落盘的编辑：绝不发 AI 请求（Codex R3i）
    if (saveBlocked()) { blockedToast(); updateFloatbar(); return; }
    if (api.isLegacy) { await legacyAiName(); return; }
    const busy0 = bar.querySelector('#afb-busy'); if (busy0) busy0.textContent = '等待你确认外发…';
    const ok = await confirmOutbound({ count: blanks.length });
    if (!ok) { toast('已取消，没有外发任何内容。', ''); updateFloatbar(); return; }
    const busy = bar.querySelector('#afb-busy'); if (busy) busy.innerHTML = '<i class="spin"></i> 正在读代号、起中文名…';
    const res = await api.draftTerms(projectId, { selectors: blanks, apply: false });
    if (res.status === 'ai_not_configured') {
      state.llm = { ...(state.llm || {}), configured: false };
      bus.emit('booted');
      toast(res.hint || 'AI 通道未配置：可以在术语页手填零件名（金路径不受影响）。', '', 5000);
      updateFloatbar(); return;
    }
    const sugg = {};
    (res.suggestions || []).forEach((s) => { sugg[s.selector] = s; });
    state.aiSuggestions = { ...state.aiSuggestions, ...sugg };
    bus.emit('ai-suggestions');
    if (!(res.suggestions || []).length) {
      toast('AI 没有可填的空白件（都已命名或已跳过）。', ''); updateFloatbar(); return;
    }
    aiPending = { selectors: (res.suggestions || []).map((s) => s.selector), suggestions: res.suggestions || [] };
    updateFloatbar();
  } catch (err) {
    toast('AI 起草失败：' + (err.hint || err.message), 'bad');
    updateFloatbar();
  }
}

async function applyAiSuggestions() {
  if (!aiPending) return;
  const bar = $('#ai-floatbar');
  const btn = bar.querySelector('#afb-apply');
  if (btn) btn.outerHTML = '<span class="hint"><i class="spin"></i> 写入中…</span>';
  try {
    await flush();
    if (saveBlocked()) { blockedToast(); paintPending(bar); return; }
    // 写入的就是用户刚才预览的建议（不再请后端重新生成）：先在副本上试算「只填空白」，没有可写的就不动计划
    const rows = aiPending.suggestions;
    const names = (state.assembly?.parts || []).map((p) => p.name);
    const trial = fillBlankTerms(JSON.parse(JSON.stringify(state.plan)), rows, names);
    if (!trial.filled.length && !trial.added.length) {
      aiPending = null;
      toast('AI 没有可写入的空白件（这些零件已被人工命名）。', '');
      updateFloatbar(); return;
    }
    let outcome = trial;
    mutate((plan) => { outcome = fillBlankTerms(plan, rows, names); });
    rows.forEach((s) => { state.aiSuggestions[s.selector] = s; });
    bus.emit('ai-suggestions');
    await flush();                                    // 普通保存循环：PUT /plan + If-Match
    if (state.saveState === 'conflict') {
      toast('计划已在别处被修改（ETag 不匹配），AI 名称没有存进服务端——请点顶栏「重载」后再试。', 'bad', 6000);
      updateFloatbar(); return;
    }
    if (state.saveState === 'error' || state.editVersion > state.savedVersion) {
      toast('AI 名称已写进本页计划，但保存未成功——请先解决保存问题。', 'bad', 6000);
      updateFloatbar(); return;
    }
    aiPending = null;
    const filled = outcome.filled.length + outcome.added.length;
    const kept = outcome.kept_human.length;
    const low = rows.filter((s) => s.confidence === 'low' && (outcome.filled.includes(s.selector) || outcome.added.includes(s.selector))).length;
    toast(`AI 已写入 ${filled} 个零件名${low ? `（${low} 个把握低，已标黄，请核对）` : ''}。`
      + (kept ? `${kept} 个人工填过的未改动。` : '人工填过的未改动。'), 'ok');
    updateFloatbar();
  } catch (err) {
    toast('AI 写入失败：' + (err.hint || err.message), 'bad');
    paintPending(bar);
  }
}

// 旧版后端兼容模式：旧接口一步完成（服务端起名并写入），完成后以服务端 plan 为准
async function legacyAiName() {
  const v0 = state.editVersion; const e0 = state.planEtag;
  const res = await api.draftTerms(projectId);
  const sugg = {};
  (res.suggestions || []).forEach((s) => { sugg[s.selector] = s; });
  state.aiSuggestions = { ...state.aiSuggestions, ...sugg };
  bus.emit('ai-suggestions');
  const unchanged = state.editVersion === v0 && state.savedVersion >= v0 && state.planEtag === e0
    && state.saveState !== 'error' && state.saveState !== 'conflict';
  if (!unchanged) {
    toast('AI 已在服务端起名，但等待期间本地计划又有改动——为不覆盖你的编辑，请刷新后查看 AI 名称。', 'bad');
    updateFloatbar(); return;
  }
  if (res.plan) {
    state.plan = res.plan;
    if (state.render) bus.emit('render-stale');
    bus.emit('plan'); bus.emit('validate');
  }
  const filled = res.filled || 0;
  toast(filled ? `AI 起草完成：填了 ${filled} 个零件名。人工填过的未改动。` : 'AI 没有可填的空白件（都已命名）。', filled ? 'ok' : '');
  updateFloatbar();
}

// ---------------------------------------------------------------- 键盘金路径
function initKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (e.target.matches?.('input, select, textarea')) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    if (document.querySelector('dialog[open]')) return;
    if (e.key === 'Escape') {
      if (viewerMod?.isBoxMode()) toggleBox();
      setSelection([]);
    }
    if (e.key === 'b' || e.key === 'B') toggleBox();
    if (e.key === 'f' || e.key === 'F') viewer?.frameAll();
    if (e.key === 'i' || e.key === 'I') {
      const cb = $('#btn-isolate'); cb.checked = !cb.checked; viewer?.setIsolate(cb.checked);
    }
    // 1/2/3 = 机位预设（硬约束，优先于「归入图」）；归入图改到图卡按钮 + Shift+数字
    if (e.key === '1') viewerMod?.setPreset('front');
    if (e.key === '2') viewerMod?.setPreset('top');
    if (e.key === '3') viewerMod?.setPreset('iso');
    if (e.key === '/' ) { /* 聚焦结构树搜索 */ const s = $('#parts .search'); if (s) { e.preventDefault(); s.focus(); } }
  });
  window.addEventListener('beforeunload', flush);
}

// ---------------------------------------------------------------- 启动
// 旧版后端兼容模式（开发用，不随产品发布）：顶栏中性提示，导出恒锁定
function showCompatBadge() {
  if (!api.isLegacy) return;
  const b = document.createElement('span');
  b.className = 'compat-badge'; b.id = 'compat-badge';
  b.textContent = '旧版后端兼容模式：导出需升级后端';
  b.title = '开发用兼容模式（不随产品发布）：可浏览、出图、编辑术语与预览；导出需升级到新版后端。';
  $('.tb-right')?.prepend(b);
}

function fatal(msg, hint) {
  document.body.innerHTML = `<div class="fatal" role="alert">载入工程失败：${esc(msg)}
    ${hint ? `<p class="hint">${esc(hint)}</p>` : ''}
    <br><a href="${esc(homeHref())}">← 回到工程列表</a></div>`;
}

async function start() {
  await api.init();
  // 工程 ID：/p/{pid}/ 页面从路径取（任何模式）；开发入口（mock / 静态站）用 ?project=，缺省才回示例 gearbox
  projectId = pagePid || params.get('project') || (api.isLegacy ? '' : 'gearbox');
  api.onDeprecation((msg) => notice(msg, 'auth-notice'));
  showCompatBadge();
  initStageNav();
  initKeyboard();
  if (readOnly) document.body.classList.add('is-static');
  if (api.authRequired) {
    fatal('需要 token', '请从终端启动日志里打印的完整地址（带 ?token=）进入一次，之后浏览器会记住会话。');
    return;
  }
  try {
    await boot(projectId);
  } catch (err) {
    fatal(err.message, err.hint);
    return;
  }
  renderToolbar();
  ['save-state', 'booted'].forEach((evt) => bus.on(evt, renderToolbar));
  const stage = params.get('stage') || 'plan';
  showStage(stage);
  // 刷新 / 重进时后端仍有在跑的渲染作业：无论停在哪个页签都接上它，终态自动呈现、期间禁用再次渲染
  if (state.renderJob?.running) import('./qa.js').then((m) => m.resumeRender());
  // 候选分图作业还在跑（例如上传后立刻进来）：接上它的 SSE
  if (state.candidatesJob?.running) import('./candidates.js').then((m) => m.resumeSuggest());
}
start();
