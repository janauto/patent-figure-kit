// 装配入口：布局、工具条、键盘、右栏页签、校验问题面板、框选开关、AI 起草。
import {
  boot, bus, state, setSelection, flush, setView,
} from './state.js';
import { api } from './api.js';
import { initViewer, setBoxMode, isBoxMode } from './viewer.js';
import { initParts } from './parts.js';
import { initFigures, addSelectionTo } from './figures.js';
import { initTerms } from './terms.js';
import { initPreview } from './preview.js';
import { initFlowcharts } from './flowcharts.js';

const $ = (sel) => document.querySelector(sel);

export function toast(msg, kind = '') {
  let box = $('#toast');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toast';
    document.body.appendChild(box);
  }
  box.className = `toast ${kind}`;
  box.textContent = msg;
  box.hidden = false;
  clearTimeout(box._t);
  box._t = setTimeout(() => { box.hidden = true; }, 3600);
}

function renderToolbar() {
  const stepName = state.step.split('/').pop();
  const badge = {
    clean: '', dirty: '未保存…', saving: '保存中…',
    saved: '已保存 ✓', error: '保存失败！',
  }[state.saveState];
  const v = state.validate;
  const errs = (v?.issues || []).filter((i) => i.severity === 'error').length;
  const warns = (v?.issues || []).length - errs;
  const llm = state.llm || {};
  const llmOk = llm.provider && llm.provider !== 'none';
  const home = state.homeUrl
    ? `<a class="tb-home" href="${state.homeUrl}" title="回到工作台首页">← 工作台</a>` : '';
  $('#toolbar').innerHTML = `
    ${home}
    <div class="brand">图纸规划台 <span class="brand-sub">Plan Studio</span></div>
    <div class="tb-step mono" title="${state.step}">${stepName}</div>
    <button class="btn-ai" data-ai ${llmOk && !state.aiBusy ? '' : 'disabled'}
            title="${llm.provider_label || ''}">
      ${state.aiBusy ? '<span class="spin dark"></span> AI 起草中…' : '✦ AI 起草零件名'}</button>
    <span class="tb-llm ${llmOk ? '' : 'off'}" title="${llm.provider_label || ''}">
      ${llmOk ? (llm.provider === 'deepseek' ? 'DeepSeek' : 'CodeBuddy·DeepSeek') : 'AI 未配置'}</span>
    <div class="tb-validate ${errs ? 'v-err' : (warns ? 'v-warn' : 'v-ok')}"
         title="服务器校验（与模型路线同一套错误码）">
      ${errs ? `${errs} 错` : ''}${errs && warns ? ' · ' : ''}${warns ? `${warns} 提示` : ''}
      ${!errs && !warns ? '计划可渲染' : ''}
    </div>
    <div class="tb-save ${state.saveState}">${badge}</div>`;
  $('#toolbar [data-ai]')?.addEventListener('click', aiDraft);
}

async function aiDraft() {
  if (state.aiBusy) return;
  await flush();
  state.aiBusy = true; renderToolbar();
  try {
    const res = await api.draftTerms({ apply: true });
    state.plan = res.plan;
    state.validate = res.validate;
    const filled = res.applied?.filled?.length || 0;
    const added = res.applied?.added?.length || 0;
    const low = (res.suggestions || []).filter((s) => s.confidence === 'low').length;
    bus.emit('plan'); bus.emit('validate');
    toast(`AI 起草完成：填了 ${filled + added} 个零件名`
      + (low ? `（${low} 个把握低，已在术语页标出，请核对）` : '')
      + '。人工填过的名字未改动。', 'ok');
    state.aiSuggestions = Object.fromEntries((res.suggestions || []).map((s) => [s.selector, s]));
    bus.emit('ai-suggestions');
  } catch (err) {
    toast(`AI 起草失败：${err.message}`, 'bad');
  } finally {
    state.aiBusy = false; renderToolbar();
  }
}

function renderIssues() {
  const list = state.validate?.issues || [];
  const el = $('#issues');
  if (!list.length) { el.innerHTML = ''; el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = `<div class="panel-head sub"><span class="stamp">检</span>校验问题
    <span class="head-count">${list.length}</span></div>
    <ul class="issue-list">${list.map((it) => `
      <li class="issue ${it.severity}">
        <b class="mono">${it.code}</b> ${it.message}
        ${it.hint ? `<div class="qa-hint">↳ ${it.hint}</div>` : ''}
      </li>`).join('')}</ul>`;
}

function initTabs() {
  document.querySelectorAll('.rtab').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.rtab').forEach((b) => b.classList.toggle('on', b === btn));
      document.querySelectorAll('.rpane').forEach((p) => {
        p.hidden = p.dataset.pane !== btn.dataset.tab;
      });
    });
  });
}

function syncBoxButton() {
  const btn = $('#btn-box');
  if (!btn) return;
  btn.classList.toggle('on', isBoxMode());
  btn.innerHTML = isBoxMode() ? '⬚ 框选中（B 退出）' : '⬚ 框选';
  $('#box-hint').hidden = !isBoxMode();
}

function initViewTools(viewer) {
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
  ex.addEventListener('change', () => viewer.frameAll());
  mode.addEventListener('change', () => { setView({ explodeMode: mode.value }); viewer.frameAll(); });
  col.addEventListener('change', () => setView({ colorBy: col.value }));
  $('#v-reset').addEventListener('click', () => {
    setView({ opacity: 1, explode: 0, hiddenGroups: new Set(), soloGroup: null });
    viewer.frameAll();
  });
  $('#v-snap').addEventListener('click', async () => {
    const v = state.view;
    const guess = v.explode > 0.05 ? 'explode' : (v.opacity < 0.95 || state.selection.size ? 'xray' : 'view');
    const name = window.prompt('存为展示图的名字（explode=爆炸视图，xray=透明看内部，其他名字也可以）', guess);
    if (!name) return;
    try {
      const res = await api.snapshot(name.trim(), viewer.snapshot());
      toast(`已保存展示图 ${res.name}（${Math.round(res.bytes / 1024)} KB），工作台首页会用到`, 'ok');
    } catch (err) { toast('保存失败：' + err.message, 'bad'); }
  });
  bus.on('view', sync);
  sync();
}

function initKeyboard() {
  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, select, textarea')) return;
    if (e.key === 'Escape') {
      if (isBoxMode()) setBoxMode(false);
      setSelection([]);
    }
    if (e.key === 'b' || e.key === 'B') setBoxMode(!isBoxMode());
    const n = Number(e.key);
    if (n >= 1 && n <= 9 && state.selection.size) {
      const fig = state.plan.figures[n - 1];
      if (fig) addSelectionTo(fig.id);
    }
  });
  window.addEventListener('beforeunload', flush);
}

async function start() {
  try {
    await boot();
  } catch (err) {
    document.body.innerHTML = `<div class="fatal">连接不上 Plan Studio 服务器：${err.message}
      <br>请从终端打印的完整地址（含 token）进入，或回到工作台首页重新打开工程。</div>`;
    return;
  }
  renderToolbar(); renderIssues();
  initParts($('#parts'));
  initFigures($('#figures'));
  initTerms($('#terms'), $('#params'));
  initPreview($('#drawer'));
  initFlowcharts($('#flows'));
  initTabs(); initKeyboard();
  bus.emit('booted');

  const viewer = await initViewer($('#viewport'));
  $('#btn-frame').addEventListener('click', () => viewer.frameAll());
  $('#btn-isolate').addEventListener('change', (e) => viewer.setIsolate(e.target.checked));
  $('#btn-box').addEventListener('click', () => setBoxMode(!isBoxMode()));
  bus.on('box-mode', syncBoxButton);
  bus.on('box-selected', ({ count, crossing }) => {
    toast(`${crossing ? '交叉选' : '窗选'}：框到 ${count} 种零件`
      + (count ? '——在图卡上点「加入所选」或按数字键 1–9 归入对应的图' : ''));
  });
  syncBoxButton();
  initViewTools(viewer);

  ['save-state', 'validate', 'booted'].forEach((evt) => bus.on(evt, () => {
    renderToolbar(); renderIssues();
  }));
}

start();
