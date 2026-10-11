// 右栏·候选分图（F2-lite）：每个候选渲染 + 过 QA 后才显示状态——
// 通过打绿章、未过给朱红章与「原因 + 建议」；接受即写进图卡。建议为空不假装零点击。
import { bus, state, mutate, setActiveFigure, partByName, resolveMembers } from './state.js';
import { api } from './api.js';
import { toast } from './main.js';
import { esc } from './esc.js';

let root;

// 把候选的稳定成员引用（{group}/{part}）解析成真实零件名；返回 {parts, display, unresolved}。
function resolveCandidate(c) {
  const groups = state.structure?.groups || [];
  const parts = []; const unresolved = [];
  (c.members || []).forEach((m) => {
    if (typeof m === 'string') {                 // 兼容旧形态：当作组名或零件名
      const g = groups.find((x) => x.name === m || x.id === m);
      if (g) parts.push(...g.parts);
      else if (partByName(m) || resolveMembers({ members: [m] }).size) parts.push(m);
      else unresolved.push(m);
    } else if (m.group) {
      const g = groups.find((x) => x.id === m.group);
      if (g) parts.push(...g.parts); else unresolved.push(`组:${m.group}`);
    } else if (m.part) {
      if (partByName(m.part) || resolveMembers({ members: [m.part] }).size) parts.push(m.part);
      else unresolved.push(m.part);
    }
  });
  return { parts: [...new Set(parts)], display: c.members_display || c.members || [], unresolved };
}

function candCard(c) {
  const pass = c.status === 'pass';
  const pending = c.status === 'pending';
  const stamp = pending
    ? '<span class="stamp-run"><i class="spin"></i> 试渲染中</span>'
    : (pass ? '<span class="stamp-pass">合格</span>' : '<span class="stamp-fail">未过闸</span>');
  const members = (c.members_display || c.members || [])
    .map((m) => `<span class="chip chip-cn">${esc(typeof m === 'string' ? m : (m.group || m.part))}</span>`).join('');
  const qaLine = c.qa && !pass
    ? `<ul class="cand-qa">${(c.qa.checks || []).filter((k) => !k.pass)
        .map((k) => `<li><span class="mono">${esc(k.id)}</span> ${esc(k.value)} ${esc(k.threshold)}</li>`).join('')}</ul>`
    : '';
  const acts = pass
    ? `<button class="btn primary sm" data-accept="${esc(c.fig_id)}">接受，建图</button>
       <button class="btn ghost sm" data-drop="${esc(c.fig_id)}">删除</button>`
    : (pending ? '' : `<button class="btn ghost sm" data-split="${esc(c.fig_id)}">按建议拆分</button>
       <button class="btn ghost sm" data-drop="${esc(c.fig_id)}">删除</button>`);
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
    ${state.candidatesUnsupported || api.readOnly ? '' : '<button class="btn-min head-btn" data-resuggest>重新给候选</button>'}</div>`;
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

// 接受候选：把稳定成员引用解析成真实零件名，全部可解析才建图，否则不生成计划并提示
function accept(figId) {
  const c = (state.candidates || []).find((x) => x.fig_id === figId);
  if (!c) return;
  const { parts: members, unresolved } = resolveCandidate(c);
  if (unresolved.length) {
    toast(`候选「${c.caption}」有 ${unresolved.length} 个成员在当前装配里找不到（${unresolved.slice(0, 3).join('、')}）——未建图。`, 'bad');
    return;
  }
  if (!members.length) { toast('这个候选没有可解析的零件，未建图。', 'bad'); return; }
  const safe = String(c.fig_id || 'fig').replace(/[^\w-]/g, '');   // 图 id 只用安全字符
  mutate((plan) => {
    const ids = new Set(plan.figures.map((f) => f.id));
    let id = safe || 'fig'; let n = 2;
    while (ids.has(id)) { id = `${safe}-${n}`; n += 1; }
    plan.figures.push({ id, caption: c.caption, kind: c.kind, members });
    setActiveFigure(id);
  });
  toast(`已接受「${c.caption}」，写入图纸页（${members.length} 种零件）`, 'ok');
  document.querySelector('.rtab[data-tab="figures"]')?.click();
}

export function initCandidates(el) {
  root = el;
  ['booted', 'structure'].forEach((evt) => bus.on(evt, render));
}
