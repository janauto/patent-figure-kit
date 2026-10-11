// 规划台装配入口：顶栏、三屏切换（规划 / 出图与 QA / 导出）、键盘金路径、AI 起名浮条。
// three.js 只在进入「规划台」时动态 import()，首页零 three.js（硬约束）。
import { boot, bus, state, setSelection, flush, setView, displayName } from './state.js';
import { api, readOnly } from './api.js';
import { applyBrand } from './brand.js';
import { esc } from './esc.js';

const $ = (s, el = document) => el.querySelector(s);
const params = new URLSearchParams(location.search);
// 工程 ID：正式入口（legacy /p/{pid}/）从路径取真实 ID，不复用示例默认（Codex #4，避免摘要键串用）；
// 开发入口（mock）用 ?project=，缺省才回示例 gearbox。
const legacyPid = (location.pathname.match(/\/p\/([\w-]+)(?:\/|$)/) || [])[1];
const projectId = api.isLegacy ? (legacyPid || '') : (params.get('project') || 'gearbox');
const forceState = params.get('state') || '';     // empty|loading|ok|fail（四态截图用）
let viewer = null;
let viewerMod = null;
let plansMounted = false;

applyBrand();

export function toast(msg, kind = '') {
  const box = $('#toast');
  box.className = `toast ${kind}`;
  box.textContent = msg;
  box.hidden = false;
  clearTimeout(box._t);
  box._t = setTimeout(() => { box.hidden = true; }, 3600);
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
function renderToolbar() {
  $('#tb-proj').textContent = `${state.title || projectId} · ${state.step || ''}`;
  const homeLink = $('.tb-home');
  if (homeLink) {
    if (api.isLegacy) {
      const base = state.homeUrl || '/';
      const tok = new URLSearchParams(location.search).get('token')
        || (() => { try { return sessionStorage.getItem('studio-token') || ''; } catch { return ''; } })();
      homeLink.href = (base.includes('token=') || !tok) ? base
        : `${base}${base.includes('?') ? '&' : '?'}token=${encodeURIComponent(tok)}`;
    } else {
      homeLink.href = `./home.html${new URLSearchParams(location.search).has('static') ? '?static' : ''}`;
    }
  }
  const llm = state.llm || {};
  const badge = $('#ai-badge');
  if (!llm.configured) { badge.classList.add('off'); badge.textContent = 'AI 未接入'; }
  else { badge.classList.remove('off'); badge.textContent = `AI · ${llm.provider_label || '已接入'}`; }
  const sv = {
    clean: '', dirty: '未保存…', saving: '保存中…', saved: '已保存 ✓',
    error: '保存失败', conflict: 'plan 冲突，请重载', readonly: '只读演示站',
  }[state.saveState] || '';
  const el = $('#tb-save');
  el.textContent = sv;
  el.className = `tb-save ${['error', 'conflict'].includes(state.saveState) ? 'bad' : ''}`;
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
    try {                                 // 保留 state / static 开关（四态截图与静态站用），保留 token
      const keep = new URLSearchParams(location.search);
      keep.set('project', projectId); keep.set('stage', stage);
      if (forceState) keep.set('state', forceState); else keep.delete('state');
      history.replaceState(null, '', `?${keep.toString()}`);
    } catch { /* */ }
  } else {
    try { history.replaceState(null, '', location.pathname + location.search); } catch { /* */ }
  }
}

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
  initTerms($('#terms'), $('#params'));   // 参数页：layout.view / density / 版心预设 等（QA 修复入口指向这里）
  initParts($('#parts'));
  // 方法流程图：正式后端（legacy）可用；mock/静态站无此后端，给出可达但诚实的说明
  if (api.flowchartsSupported()) {
    import('./flowcharts.js').then((m) => m.initFlowcharts($('#flows')));
  } else {
    $('#flows').innerHTML = `<div class="pane-note"><p class="stamp-run">需要后端</p>
      <p>方法专利的流程图（S101 步骤号）由后端生成，mock 未实现——在正式工作台里可用。</p></div>`;
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
    <p class="stamp-fail">视口</p><p>${msg}</p>
    <button class="btn" onclick="location.reload()">重试</button></div>`;
  $('#viewport-overlay').hidden = false;
}
function clearViewportOverlay() { $('#viewport-overlay').hidden = true; $('#viewport-overlay').innerHTML = ''; }

function initRightTabs() {
  document.querySelectorAll('.rtab').forEach((btn) => btn.addEventListener('click', () => {
    document.querySelectorAll('.rtab').forEach((b) => b.classList.toggle('on', b === btn));
    document.querySelectorAll('.rpane').forEach((p) => { p.hidden = p.dataset.pane !== btn.dataset.tab; });
  }));
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

// ---------------------------------------------------------------- AI 起名浮条（F9）
function initAiFloatbar() {
  bus.on('selection', updateFloatbar);
  updateFloatbar();
}
function updateFloatbar() {
  const bar = $('#ai-floatbar');
  const sel = [...state.selection];
  const blanks = sel.filter((n) => !displayName(n));
  if (!sel.length) { bar.hidden = true; return; }
  const llmOk = state.llm?.configured;
  bar.hidden = false;
  bar.innerHTML = `
    <span class="afb-mark">AI</span>
    <span class="afb-text">已选 <b>${sel.length}</b> 件${blanks.length ? ` · <b>${blanks.length}</b> 件待起名` : '，均已命名'}</span>
    ${blanks.length && llmOk && !readOnly ? `<button class="btn primary sm" id="afb-name" title="后端起名只填空白件、不动人工填过的；范围是整个工程">AI 为空白件起名</button>`
      : (!llmOk ? '<span class="hint">AI 未接入，可在术语页手填</span>' : '')}
    <button class="btn ghost sm" id="afb-clear">清空选择</button>`;
  $('#afb-clear')?.addEventListener('click', () => setSelection([]));
  $('#afb-name')?.addEventListener('click', aiNameSelection);
}
async function aiNameSelection() {
  const bar = $('#ai-floatbar');
  bar.querySelector('#afb-name').outerHTML = '<span class="hint"><i class="spin"></i> 正在读代号、起中文名…</span>';
  try {
    await flush();                                   // 先落盘本地改动：后端会在服务端直接写 plan
    const v0 = state.editVersion;
    const res = await api.draftTerms(projectId);     // 新旧后端都是服务端起名、只填空白、写入 plan（不分选中）
    const sugg = {};
    (res.suggestions || []).forEach((s) => { sugg[s.selector] = s; });
    state.aiSuggestions = { ...state.aiSuggestions, ...sugg };
    bus.emit('ai-suggestions');
    const filled = res.filled || 0;
    if (state.editVersion !== v0) {
      // 等待期间本地又改了计划：不拿服务端 plan 覆盖本地编辑（下次保存会按 ETag 冲突提示）
      toast('AI 已在服务端起名，但等待期间本地计划又有改动——为不覆盖你的编辑，请刷新后查看 AI 名称。', 'bad');
      updateFloatbar();
      return;
    }
    if (res.plan) {                                  // 以服务端 plan 与新 ETag 为准
      state.plan = res.plan;
      if (res.etag !== undefined) state.planEtag = res.etag || null;
      if (state.render) bus.emit('render-stale');   // plan 变了 → 旧渲染结果过期
      bus.emit('plan'); bus.emit('validate');
    }
    const low = (res.suggestions || []).filter((s) => s.confidence === 'low').length;
    toast(filled ? `AI 起草完成：填了 ${filled} 个零件名`
        + (low ? `（${low} 个把握低，已标黄，请核对）` : '') + '。人工填过的未改动。'
      : 'AI 没有可填的空白件（都已命名）。', filled ? 'ok' : '');
    updateFloatbar();
  } catch (err) {
    toast('AI 起草失败：' + (err.hint || err.message), 'bad');
    updateFloatbar();
  }
}

// ---------------------------------------------------------------- 键盘金路径
function initKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (e.target.matches?.('input, select, textarea')) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
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

async function start() {
  showCompatBadge();
  initStageNav();
  initKeyboard();
  if (readOnly) document.body.classList.add('is-static');
  try {
    await boot(projectId);
  } catch (err) {
    document.body.innerHTML = `<div class="fatal">载入工程失败：${esc(err.message)}
      <br><a href="./home.html">← 回到工程列表</a></div>`;
    return;
  }
  renderToolbar();
  ['save-state', 'booted'].forEach((evt) => bus.on(evt, renderToolbar));
  const stage = params.get('stage') || 'plan';
  showStage(stage);
  // 刷新 / 重进时后端仍有在跑的渲染作业：无论停在哪个页签都接上它，终态自动呈现、期间禁用再次渲染
  if (state.renderJob?.running) import('./qa.js').then((m) => m.resumeRender());
}
start();
