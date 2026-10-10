// 右栏·候选分图（F2-lite）：每个候选渲染 + 过 QA 后才显示状态——
// 通过打绿章、未过给朱红章与「原因 + 建议」；接受即写进图卡。建议为空不假装零点击。
import { bus, state, mutate, setActiveFigure } from './state.js';
import { api } from './api.js';
import { toast } from './main.js';

let root;
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

function candCard(c) {
  const pass = c.status === 'pass';
  const pending = c.status === 'pending';
  const stamp = pending
    ? '<span class="stamp-run"><i class="spin"></i> 试渲染中</span>'
    : (pass ? '<span class="stamp-pass">合格</span>' : '<span class="stamp-fail">未过闸</span>');
  const members = (c.members || []).map((m) => `<span class="chip chip-cn">${esc(m)}</span>`).join('');
  const qaLine = c.qa && !pass
    ? `<ul class="cand-qa">${(c.qa.checks || []).filter((k) => !k.pass)
        .map((k) => `<li><span class="mono">${esc(k.id)}</span> ${esc(k.value)} ${esc(k.threshold)}</li>`).join('')}</ul>`
    : '';
  const acts = pass
    ? `<button class="btn primary sm" data-accept="${c.fig_id}">接受，建图</button>
       <button class="btn ghost sm" data-drop="${c.fig_id}">删除</button>`
    : (pending ? '' : `<button class="btn ghost sm" data-split="${c.fig_id}">按建议拆分</button>
       <button class="btn ghost sm" data-drop="${c.fig_id}">删除</button>`);
  return `<article class="cand-card ${c.status}">
    <header><b>${esc(c.caption)}</b>${stamp}</header>
    <p class="cand-kind mono">${c.kind === 'assembly' ? '装配图' : '分解图'} · ${c.labels ?? '—'} 个标记</p>
    <div class="chips">${members}</div>
    <p class="cand-reason ${pass ? 'ok' : (pending ? '' : 'bad')}">${esc(c.reason || '')}</p>
    ${qaLine}
    <div class="cand-acts">${acts}</div>
  </article>`;
}

function render() {
  const cands = state.candidates || [];
  const head = `<div class="panel-head"><span class="seal">候选</span>候选分图
    ${state.candidatesUnsupported ? '' : '<button class="btn-min head-btn" data-resuggest>重新给候选</button>'}</div>`;
  if (state.candidatesUnsupported) {
    root.innerHTML = `${head}
      <div class="cand-empty">
        <p class="stamp-run">需要新版后端</p>
        <p>当前连接的是现有工作台后端，<b>还没有候选分图</b>功能。</p>
        <p>照旧流程也能出图：在 3D 里框选要申请的一组零件（按 <kbd>B</kbd>），
          到「图纸」页点「＋新建图」，或在结构树里用某个结构组一键建图。</p>
      </div>`;
    return;
  }
  if (!cands.length) {
    // 建议为空：明确提示「请框选分组」并给三步示意（不假装零点击）
    root.innerHTML = `${head}
      <div class="cand-empty">
        <p class="stamp-run">无候选</p>
        <p>没有给出候选分图——这台装配体的子装配层级不够清晰，需要你<b>手动框选分组</b>。</p>
        <ol class="cand-steps">
          <li><b>1</b> 在视口按 <kbd>B</kbd> 进入框选，或在结构树里点组名</li>
          <li><b>2</b> 框住要申请的一组零件（左→右窗选 / 右→左交叉选）</li>
          <li><b>3</b> 到「图纸」页点「＋新建图」，或图卡上「加入所选」</li>
        </ol>
      </div>`;
    bindHead();
    return;
  }
  const passed = cands.filter((c) => c.status === 'pass').length;
  root.innerHTML = `${head}
    <p class="hint pad">${cands.length} 个候选 · ${passed} 个过闸。每个候选都已实际渲染并过 QA 后才显示状态。
      标准件（螺钉等）默认不发标记。</p>
    <div class="cand-list">${cands.map(candCard).join('')}</div>`;
  bindHead();
  root.querySelectorAll('[data-accept]').forEach((b) => b.addEventListener('click', () => accept(b.dataset.accept)));
  root.querySelectorAll('[data-drop]').forEach((b) => b.addEventListener('click', () => {
    state.candidates = state.candidates.filter((c) => c.fig_id !== b.dataset.drop); render();
  }));
  root.querySelectorAll('[data-split]').forEach((b) => b.addEventListener('click', () => {
    toast('已把该整机候选标记为「按结构拆分」——请到结构树用各子装配分别建图', '');
  }));
}

function bindHead() {
  root.querySelector('[data-resuggest]')?.addEventListener('click', resuggest);
}

async function resuggest() {
  try {
    const { job_id } = await api.suggest(state.projectId, false);
    toast('正在重新给候选分图…', '');
    api.jobEvents(job_id, { done() { toast('候选分图已更新', 'ok'); render(); } });
  } catch (err) { toast('无法重算候选：' + (err.hint || err.message), 'bad'); }
}

// 接受候选：用候选的成员（这里用结构组名→展开成员）建一张图卡
function accept(figId) {
  const c = (state.candidates || []).find((x) => x.fig_id === figId);
  if (!c) return;
  const groups = state.structure?.groups || [];
  const members = [];
  (c.members || []).forEach((gn) => {
    const g = groups.find((x) => x.name === gn);
    if (g) members.push(...g.parts); else members.push(gn);
  });
  mutate((plan) => {
    const ids = new Set(plan.figures.map((f) => f.id));
    let id = c.fig_id; let n = 2;
    while (ids.has(id)) { id = `${c.fig_id}-${n}`; n += 1; }
    plan.figures.push({ id, caption: c.caption, kind: c.kind, members });
    setActiveFigure(id);
  });
  toast(`已接受「${c.caption}」，写入图纸页`, 'ok');
  document.querySelector('.rtab[data-tab="figures"]')?.click();
}

export function initCandidates(el) {
  root = el;
  ['booted', 'structure'].forEach((evt) => bus.on(evt, render));
}
