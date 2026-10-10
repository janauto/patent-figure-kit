// 右栏·图纸页签：图卡的增删改与成员分配。members 永远写显式零件名（不自造 glob，防误伤）；
// 已有的 glob（比如 LLM 初稿或 "*"）原样展示，可整体移除。
import {
  bus, state, mutate, resolveMembers, labelCount, figureColor,
  setActiveFigure, setSelection, displayName,
} from './state.js';

let root;

const maxLabels = () => state.plan?.layout?.max_labels_per_figure ?? 20;

function nextFigId() {
  let n = 1;
  const ids = new Set((state.plan.figures || []).map((f) => f.id));
  while (ids.has(`fig${n}`)) n += 1;
  return `fig${n}`;
}

function render() {
  const figs = state.plan?.figures || [];
  const cards = figs.map((fig) => {
    const color = figureColor(fig.id);
    const resolved = resolveMembers(fig);
    const labels = labelCount(fig);
    const over = labels > maxLabels();
    const active = fig.id === state.activeFigure ? ' is-active' : '';
    const chips = (fig.members || []).map((m, i) => {
      const isGlob = /[*?\[]/.test(m);
      const cn = isGlob ? '' : displayName(m);
      const n = isGlob ? `<i>${m}</i>` : (cn || m);
      return `<span class="chip${cn ? ' chip-cn' : ' mono'}${isGlob ? ' chip-glob' : ''}" title="${m}">${n}
        <button data-fig="${fig.id}" data-rm="${i}" title="移除">×</button></span>`;
    }).join('');
    return `<article class="fig-card${active}" data-fig="${fig.id}" style="--fig:${color}">
      <header>
        <span class="fig-swatch"></span>
        <input class="fig-caption" data-fig="${fig.id}" value="${fig.caption || ''}"
               placeholder="图题，如：整体结构示意图">
        <button class="icon-btn" data-del="${fig.id}" title="删除本图">✕</button>
      </header>
      <div class="fig-meta">
        <span class="mono">${fig.id}</span>
        <label class="seg">
          <button class="seg-btn${fig.kind === 'assembly' ? ' on' : ''}"
                  data-kind="assembly" data-fig="${fig.id}">装配</button>
          <button class="seg-btn${fig.kind === 'exploded' ? ' on' : ''}"
                  data-kind="exploded" data-fig="${fig.id}">分解</button>
        </label>
        <span class="badge${over ? ' badge-over' : ''}"
              title="有效标记数（服务器校验为准）">${labels}/${maxLabels()}</span>
        <span class="fig-count">${resolved.size} 种零件</span>
      </div>
      <div class="chips">${chips || '<span class="hint">空图——选中零件后点下方按钮加入</span>'}</div>
      <div class="fig-actions">
        <button class="btn-min" data-add="${fig.id}"
                ${state.selection.size ? '' : 'disabled'}>加入所选 (${state.selection.size})</button>
        <button class="btn-min" data-drop="${fig.id}"
                ${state.selection.size ? '' : 'disabled'}>移除所选</button>
        <button class="btn-min" data-pick="${fig.id}">选中本图零件</button>
      </div>
    </article>`;
  }).join('');

  root.innerHTML = `
    <div class="panel-head"><span class="stamp">02</span>图纸
      <button class="btn-min head-btn" data-new>＋ 新建图</button></div>
    <div class="cards">${cards}</div>
    <p class="hint pad">快捷键：选中零件后按 <b>1–9</b> 直接归入第 N 张图；<b>Esc</b> 清空选择。</p>
  `;

  root.querySelector('[data-new]').addEventListener('click', () => {
    const id = nextFigId();
    mutate((plan) => plan.figures.push({
      id, caption: '', kind: 'exploded',
      members: [...state.selection],
    }));
    setActiveFigure(id);
  });
  root.querySelectorAll('.fig-card').forEach((card) => {
    card.addEventListener('click', () => setActiveFigure(card.dataset.fig));
  });
  root.querySelectorAll('.fig-caption').forEach((input) => {
    input.addEventListener('click', (e) => e.stopPropagation());
    input.addEventListener('change', () => mutate((plan) => {
      plan.figures.find((f) => f.id === input.dataset.fig).caption = input.value.trim();
    }));
  });
  root.querySelectorAll('[data-kind]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      mutate((plan) => {
        plan.figures.find((f) => f.id === btn.dataset.fig).kind = btn.dataset.kind;
      });
    });
  });
  root.querySelectorAll('[data-del]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!window.confirm(`删除 ${btn.dataset.del}？`)) return;
      mutate((plan) => {
        plan.figures = plan.figures.filter((f) => f.id !== btn.dataset.del);
      });
    });
  });
  root.querySelectorAll('[data-rm]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      mutate((plan) => {
        plan.figures.find((f) => f.id === btn.dataset.fig)
          .members.splice(Number(btn.dataset.rm), 1);
      });
    });
  });
  root.querySelectorAll('[data-add]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      addSelectionTo(btn.dataset.add);
    });
  });
  root.querySelectorAll('[data-drop]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      mutate((plan) => {
        const fig = plan.figures.find((f) => f.id === btn.dataset.drop);
        fig.members = fig.members.filter((m) => !state.selection.has(m));
      });
    });
  });
  root.querySelectorAll('[data-pick]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const fig = state.plan.figures.find((f) => f.id === btn.dataset.pick);
      setSelection([...resolveMembers(fig)]);
    });
  });
}

export function addSelectionTo(figId) {
  if (!state.selection.size) return;
  mutate((plan) => {
    const fig = plan.figures.find((f) => f.id === figId);
    if (!fig) return;
    const have = new Set(fig.members);
    state.selection.forEach((n) => { if (!have.has(n)) fig.members.push(n); });
  });
}

export function initFigures(el) {
  root = el;
  ['booted', 'plan', 'selection', 'active-figure', 'structure'].forEach((evt) => bus.on(evt, render));
}
