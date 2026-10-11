// 左栏：零件——默认按「结构组」展示（AI 把看不懂的零件代号归成看得懂的结构，并起中文显示名），
// 也可切回平铺清单。点组名选中整组；「只看」「隐藏」只影响 3D 观察；「＋图」用整组新建一张图。
// 深腔零件 3D 里点不到，这里永远点得到。
import {
  bus, state, mutate, toggleSelect, setSelection, figuresContaining, figureColor,
  unassignedParts, setActiveFigure, displayName, aiName, groupColor, setView, indexStructure,
} from './state.js';
import { api } from './api.js';
import { flashParts } from './viewer.js';
import { toast } from './main.js';

let root; let query = '';
let pollTimer = null;

import { esc } from './esc.js';

function sizeText(p) {
  const [w, d, h] = p.bbox_size || [];
  return (w === undefined) ? '' : `${w.toFixed(0)}×${d.toFixed(0)}×${h.toFixed(0)}`;
}

function matches(p) {
  if (!query) return true;
  return p.name.toLowerCase().includes(query) || displayName(p.name).includes(query)
    || aiName(p.name).includes(query);
}

function partRow(p, missing) {
  const figs = figuresContaining(p.name);
  const dots = figs.map((f) =>
    `<i class="dot" style="background:${figureColor(f.id)}" title="${f.id}"></i>`).join('');
  const sel = state.selection.has(p.name) ? ' is-selected' : '';
  const warn = missing.has(p.name) ? '<span class="tag-warn">未入图</span>' : '';
  const degen = p.degenerate ? '<span class="tag-warn">退化</span>' : '';
  const cn = displayName(p.name);
  return `<li class="part-row${sel}" data-name="${esc(p.name)}" title="${esc(p.name)}"
      role="option" aria-selected="${state.selection.has(p.name)}" tabindex="-1">
    <span class="part-name${cn ? '' : ' unnamed'}">${cn ? esc(cn) : '未命名'}</span>
    <span class="part-meta"><span class="mono">${esc(p.name)}</span> · ×${p.instances} · ${sizeText(p)}</span>
    <span class="part-flags">${dots}${warn}${degen}</span>
  </li>`;
}

let activePart = null;   // 键盘焦点所在的零件（listbox roving tabindex）
function partRows() { return [...root.querySelectorAll('.part-row')]; }
function setActivePart(name, focus) {
  activePart = name;
  partRows().forEach((li) => { li.tabIndex = li.dataset.name === name ? 0 : -1; });
  if (focus) { const el = root.querySelector(`.part-row[data-name="${CSS.escape(name)}"]`); el?.focus(); }
}
function moveActive(dir) {
  const rows = partRows(); if (!rows.length) return;
  let idx = rows.findIndex((r) => r.dataset.name === activePart);
  idx = idx < 0 ? 0 : Math.min(rows.length - 1, Math.max(0, idx + dir));
  setActivePart(rows[idx].dataset.name, true);
}

function structureHead() {
  const s = state.structure;
  const job = state.structureJob || {};
  const llmOk = state.llm && state.llm.configured;
  const source = job.running
    ? '<span class="spin"></span> AI 正在识别结构…（可继续操作）'
    : (s?.source === 'ai' ? `AI 识别 · ${esc(s.provider || '')} · ${esc(s.created || '')}`
      : '按 STEP 装配层级自动分组——点「AI 识别结构」换成看得懂的结构名');
  const emptyTerms = (state.plan?.terms || []).filter((t) => !(t.term || '').trim()).length;
  const canApply = s?.source === 'ai' && Object.keys(s.names || {}).length;
  return `<div class="struct-bar">
    <p class="struct-src${job.error ? ' bad' : ''}">${job.error ? `识别失败：${esc(job.error)}` : source}</p>
    <div class="struct-actions">
      <button class="btn-ai" data-analyze ${job.running || !llmOk ? 'disabled' : ''}
        title="${llmOk ? '' : '未接入 AI；可在术语页手填中文名'}"><span class="ai-mark">AI</span> ${s?.source === 'ai' ? '重新识别' : '识别结构'}</button>
      ${canApply ? `<button class="btn-min" data-apply title="只填术语表里空着的行，人工填过的不动">中文名填入术语${emptyTerms ? `（${emptyTerms} 空）` : ''}</button>` : ''}
    </div></div>`;
}

function groupBlock(g, partsByName, missing) {
  const v = state.view;
  const members = g.parts.map((n) => partsByName.get(n)).filter(Boolean).filter(matches);
  if (query && !members.length && !g.name.includes(query)) return '';
  const open = query || !state.collapsed.has(g.id);
  const hidden = v.hiddenGroups.has(g.id);
  const solo = v.soloGroup === g.id;
  const selCount = g.parts.filter((n) => state.selection.has(n)).length;
  return `<section class="sgroup${hidden ? ' is-hidden' : ''}${solo ? ' is-solo' : ''}" style="--g:${groupColor(g.id)}">
    <header>
      <button class="caret" data-caret="${g.id}" title="展开/收起">${open ? '▾' : '▸'}</button>
      <i class="gdot"></i>
      <b class="gname" data-gsel="${g.id}" title="点击选中整组 · 双击改名">${esc(g.name)}</b>
      <span class="gcount">${selCount ? `${selCount}/` : ''}${g.parts.length} 件</span>
    </header>
    ${g.role ? `<p class="grole">${esc(g.role)}</p>` : ''}
    <div class="gacts">
      <button class="gbtn" data-gsel="${g.id}" title="选中这一组的全部零件">选中</button>
      <button class="gbtn${solo ? ' on' : ''}" data-solo="${g.id}" title="3D 里只看这一组">只看</button>
      <button class="gbtn${hidden ? ' on' : ''}" data-hide="${g.id}" title="在 3D 中隐藏这一组">${hidden ? '显示' : '隐藏'}</button>
      <button class="gbtn" data-newfig="${g.id}" title="用这一组的零件新建一张分解图">＋建图</button>
    </div>
    ${open ? `<ul class="part-list" role="listbox" aria-multiselectable="true" aria-label="零件列表">${members.map((p) => partRow(p, missing)).join('')}</ul>` : ''}
  </section>`;
}

function render() {
  const parts = state.assembly?.parts || [];
  const partsByName = new Map(parts.map((p) => [p.name, p]));
  const missing = new Set(unassignedParts());
  const mode = state.partsMode;
  const groups = state.structure?.groups || [];

  const sugg = (state.assembly?.split_suggestions || []).map((s, i) => `
    <button class="sugg" data-sugg="${i}">
      <b>${{ coaxial: '按共轴组', stack: '按装配栈', size: '按尺寸档' }[s.strategy] || s.strategy}</b>
      <span>${s.figures.length} 张图 · 脚本计算</span>
    </button>`).join('');

  const body = mode === 'structure' && groups.length
    ? `${structureHead()}<div class="sgroups">${groups.map((g) => groupBlock(g, partsByName, missing)).join('')}</div>`
    : `<ul class="part-list" role="listbox" aria-multiselectable="true" aria-label="零件列表">${parts.filter(matches).map((p) => partRow(p, missing)).join('')}</ul>`;

  root.innerHTML = `
    <div class="panel-head"><span class="stamp">01</span>零件
      <label class="seg mode-seg">
        <button class="seg-btn${mode === 'structure' ? ' on' : ''}" data-mode="structure">按结构</button>
        <button class="seg-btn${mode === 'flat' ? ' on' : ''}" data-mode="flat">按零件</button>
      </label>
      <span class="head-count">${parts.length} 种</span></div>
    <input class="search" placeholder="搜中文名或代号…" value="${esc(query)}">
    ${body}
    <div class="sel-actions">
      <span>${state.selection.size ? `已选 ${state.selection.size} 种` : '点 3D、点列表或按 B 框选'}</span>
      ${state.selection.size ? '<button class="link" data-act="clear">清空</button>' : ''}
    </div>
    ${sugg ? `<div class="panel-head sub"><span class="stamp">拆</span>拆分建议</div>${sugg}` : ''}
  `;
  bind();
}

function nextFigId() {
  let n = 1;
  const ids = new Set((state.plan.figures || []).map((f) => f.id));
  while (ids.has(`fig${n}`)) n += 1;
  return `fig${n}`;
}

const groupById = (id) => (state.structure?.groups || []).find((g) => g.id === id);

function bind() {
  const search = root.querySelector('.search');
  search.addEventListener('input', (e) => {
    query = e.target.value.trim().toLowerCase();
    render();
    const el = root.querySelector('.search');
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  });
  root.querySelectorAll('[data-mode]').forEach((b) => b.addEventListener('click', () => {
    state.partsMode = b.dataset.mode; render();
  }));
  root.querySelectorAll('.part-row').forEach((li) => {
    li.addEventListener('click', () => { setActivePart(li.dataset.name); toggleSelect(li.dataset.name); });
    li.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); moveActive(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); moveActive(-1); }
      else if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggleSelect(li.dataset.name); }
      else if (e.key === 'Home') { e.preventDefault(); const r = partRows(); if (r[0]) setActivePart(r[0].dataset.name, true); }
      else if (e.key === 'End') { e.preventDefault(); const r = partRows(); if (r.length) setActivePart(r[r.length - 1].dataset.name, true); }
    });
  });
  // roving tabindex：让列表第一个可达项（或上次焦点项）可 Tab 进入；重渲后恢复焦点
  const rows = partRows();
  if (rows.length) {
    const keep = rows.some((r) => r.dataset.name === activePart) ? activePart : rows[0].dataset.name;
    const hadFocus = root.contains(document.activeElement) && document.activeElement.classList?.contains('part-row');
    setActivePart(keep, hadFocus);
  }
  root.querySelectorAll('[data-caret]').forEach((b) => b.addEventListener('click', () => {
    const id = b.dataset.caret;
    if (state.collapsed.has(id)) state.collapsed.delete(id); else state.collapsed.add(id);
    render();
  }));
  root.querySelectorAll('[data-gsel]').forEach((b) => {
    b.addEventListener('click', () => {
      const g = groupById(b.dataset.gsel);
      if (!g) return;
      setSelection(g.parts);
      flashParts(g.parts);
    });
    if (b.classList.contains('gname')) b.addEventListener('dblclick', async () => {
      if (api.readOnly) { toast('静态演示站只读，不能改结构组名', ''); return; }
      const g = groupById(b.dataset.gsel);
      const name = window.prompt('结构组名称', g.name);
      if (!name || name.trim() === g.name) return;
      const old = g.name;
      g.name = name.trim();                 // 乐观更新
      indexStructure(); render(); bus.emit('structure');
      try {
        const res = await api.renameGroups(state.projectId, [{ id: g.id, name: g.name }]);
        if (res?.structure) { state.structure = res.structure; indexStructure(); render(); bus.emit('structure'); }
      } catch (err) {                       // 保存失败：回滚 + 报错
        g.name = old; indexStructure(); render(); bus.emit('structure');
        toast('改名没保存：' + (err.hint || err.message), 'bad');
      }
    });
  });
  root.querySelectorAll('[data-solo]').forEach((b) => b.addEventListener('click', () => {
    setView({ soloGroup: state.view.soloGroup === b.dataset.solo ? null : b.dataset.solo });
  }));
  root.querySelectorAll('[data-hide]').forEach((b) => b.addEventListener('click', () => {
    const h = new Set(state.view.hiddenGroups);
    if (h.has(b.dataset.hide)) h.delete(b.dataset.hide); else h.add(b.dataset.hide);
    setView({ hiddenGroups: h });
  }));
  root.querySelectorAll('[data-newfig]').forEach((b) => b.addEventListener('click', () => {
    const g = groupById(b.dataset.newfig);
    const id = nextFigId();
    mutate((plan) => plan.figures.push({
      id, caption: `${g.name}分解示意图`, kind: 'exploded', members: [...g.parts],
    }));
    setActiveFigure(id);
    toast(`已用「${g.name}」新建 ${id}（${g.parts.length} 种零件）——零件多时出图前可再拆分`, 'ok');
  }));
  root.querySelector('[data-analyze]')?.addEventListener('click', analyze);
  root.querySelector('[data-apply]')?.addEventListener('click', applyStructureNames);
  const clear = root.querySelector('[data-act="clear"]');
  if (clear) clear.addEventListener('click', () => setSelection([]));
  root.querySelectorAll('.sugg').forEach((btn) => {
    btn.addEventListener('click', () => applySuggestion(Number(btn.dataset.sugg)));
  });
}

// 结构识别：起一个 suggest 作业演示进度（mock 的结构已预置，作业完成后沿用现有结构）。
async function analyze() {
  try {
    state.structureJob = { running: true };
    render();
    const res = await api.analyzeStructure(state.projectId);   // legacy → /api/structure/analyze；live → suggest 作业
    if (api.isLegacy) {
      // 旧后端：轮询 /api/structure 直到识别作业结束
      const poll = async () => {
        const r = await api.getStructure();
        state.structureJob = r.job || null;
        if (r.job?.running) { render(); setTimeout(poll, 3000); return; }
        if (r.structure) { state.structure = r.structure; indexStructure(); }
        if (state.structure?.source === 'ai') setView({ colorBy: 'structure' });
        bus.emit('structure'); render();
        if (r.job?.error) toast('结构识别失败：' + r.job.error, 'bad');
        else toast(`结构识别完成：${state.structure?.groups?.length || 0} 个结构组`, 'ok');
      };
      setTimeout(poll, 3000);
      return;
    }
    api.jobEvents(res.job_id, {
      done() {
        state.structureJob = null;
        if (state.structure?.source === 'ai') setView({ colorBy: 'structure' });
        bus.emit('structure'); render();
        toast(`结构识别完成：${state.structure?.groups?.length || 0} 个结构组`, 'ok');
      },
      fail(d) { state.structureJob = { error: d.message }; render(); toast('结构识别失败：' + d.message, 'bad'); },
    });
  } catch (err) { state.structureJob = null; render(); toast('无法开始识别：' + (err.hint || err.message), 'bad'); }
}

// 把结构里的中文名填进术语表的空行（人工填过的不覆盖），本地完成后保存 plan。
function applyStructureNames() {
  const names = state.structure?.names || {};
  let filled = 0;
  mutate((plan) => {
    const bySel = new Map((plan.terms || []).map((t) => [t.selector, t]));
    Object.entries(names).forEach(([sel, info]) => {
      const row = bySel.get(sel);
      if (row && !(row.term || '').trim()) { row.term = info.name; filled += 1; }
    });
  });
  toast(`已把 ${filled} 个中文名填入术语表；人工填过的未改动`, 'ok');
}

function applySuggestion(index) {
  const s = state.assembly.split_suggestions[index];
  if (!s) return;
  const ok = window.confirm(
    `用「${s.id}」建议重建全部图卡（${s.figures.length} 张）？现有图卡会被替换，术语表不受影响。`);
  if (!ok) return;
  mutate((plan) => {
    plan.figures = s.figures.map((f, i) => ({
      id: `fig${i + 1}`,
      caption: f.caption_hint || `分解示意图${i + 1}`,
      kind: 'exploded',
      members: [...f.members],
    }));
  });
  setActiveFigure('fig1');
}

export function initParts(el) {
  root = el;
  ['booted', 'plan', 'selection', 'active-figure', 'view', 'structure'].forEach((evt) => bus.on(evt, render));
  bus.on('booted', () => { if (state.structureJob?.running) poll(); });
}
