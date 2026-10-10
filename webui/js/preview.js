// 底部抽屉：渲染、QA 红绿灯、SVG 预览（交底版 / 递交版 / 原始出图三种版本）。
// 预览里点附图标记数字 → 3D 里对应零件高亮。导出按钮只在全部 QA 通过后出现——闸门不可绕。
import { api } from './api.js';
import { bus, state, setSelection } from './state.js';
import { flashParts } from './viewer.js';

let root; let activeTab = null; let pollTimer = null;
let variant = 'annotated';
let lastExport = null;

const VARIANT_NAMES = {
  annotated: '交底版 · 图号＋件号表', filing: '递交版 · 仅图号', raw: '原始出图',
};

function checkRow(c) {
  const mark = c.pass ? '<span class="qa-ok">✓</span>' : '<span class="qa-bad">✗</span>';
  const hint = !c.pass && c.hint ? `<div class="qa-hint">↳ ${c.hint}</div>` : '';
  return `<li class="${c.pass ? '' : 'qa-fail'}">${mark}
    <span class="qa-id mono">${c.id}</span>
    <span class="qa-val mono">${c.value}</span>
    <span class="qa-th mono">${c.threshold}</span>${hint}</li>`;
}

function tableBlock(fig) {
  const t = fig.annotation?.table;
  if (!t) return '';
  const warn = (fig.annotation.warnings || []).map((w) => `<p class="hint warn">⚠ ${w}</p>`).join('');
  return `<div class="ann-box">
    <div class="ann-title">件号名称表 <span class="hint">${t.rows.length} 行 · ${t.corner} · ${t.blocks} 栏</span></div>
    <table class="ann-table"><thead><tr><th>序号</th><th>名 称</th></tr></thead><tbody>
    ${t.rows.map((r) => `<tr><td>${r.numeral}</td><td>${r.name}</td></tr>`).join('')}
    </tbody></table>${warn}</div>`;
}

function render() {
  const r = state.render;
  const busy = state.rendering;
  const figs = (r?.figures || []).slice().sort((a, b) => (a.number || 99) - (b.number || 99));
  if (!activeTab && figs.length) activeTab = figs[0].id;
  const allPass = !!(r && r.ok && figs.length && figs.every((f) => f.pass));

  const tabs = figs.map((f) => `
    <button class="tab${f.id === activeTab ? ' on' : ''}${f.pass ? '' : ' tab-fail'}"
            data-tab="${f.id}">${f.number ? `图${f.number}` : f.id} ${f.pass ? '✓' : '✗'}</button>`).join('');
  const current = figs.find((f) => f.id === activeTab);
  const qa = current?.qa?.checks?.map(checkRow).join('') || '';
  const variants = current?.variants ? Object.keys(VARIANT_NAMES).filter((v) => current.variants[v]) : [];
  if (current && variants.length && !variants.includes(variant)) [variant] = variants;
  const seg = variants.length > 1 ? `<div class="seg variant-seg">${variants.map((v) => `
      <button class="seg-btn${v === variant ? ' on' : ''}" data-variant="${v}">${VARIANT_NAMES[v]}</button>`).join('')}</div>` : '';
  const desc = (r?.figure_descriptions || []).length
    ? `<div class="desc-box"><div class="ann-title">附图说明（可直接粘进说明书）</div>
       <p>${r.figure_descriptions.join('<br>')}</p></div>` : '';
  const numeralNote = r?.numerals
    ? `<p class="hint">附图标记说明：${r.numerals.description_zh || ''}</p>` : '';
  const exported = lastExport ? `<a class="btn-download" href="${api.exportUrl(lastExport.zip)}"
      download>⬇ 下载交付包 ${lastExport.zip}</a>` : '';

  root.innerHTML = `
    <div class="drawer-bar">
      <button class="btn-render" data-render ${busy ? 'disabled' : ''}>
        ${busy ? '<span class="spin"></span> 渲染中…' : '渲染出图'}</button>
      <div class="tabs">${tabs}</div>
      ${seg}
      <div class="drawer-right">
        ${exported}
        ${allPass ? `<label class="check-inline">
            <input type="checkbox" data-dwg checked>同时转 DWG</label>
          <button class="btn-export" data-export>导出交付件</button>`
    : (figs.length ? '<span class="hint">有图未过 QA，按提示修改后重渲</span>' : '')}
      </div>
    </div>
    ${current ? `<div class="drawer-body">
      <div class="svg-pane" data-pane></div>
      <div class="qa-pane">
        ${tableBlock(current)}
        <div class="qa-title">${current.pass
    ? '<span class="stamp-pass">合格</span>' : '<span class="stamp-fail">未通过</span>'}
          QA ${current.qa ? `${current.qa.summary?.passed ?? ''}项通过` : ''}</div>
        <ul class="qa-list">${qa}</ul>${desc}${numeralNote}
      </div>
    </div>` : (busy ? '' : '<p class="hint pad">填好计划后点「渲染出图」。渲染走与模型完全相同的 CLI 与 QA 闸门；通过后自动生成交底版（图号＋件号名称表）与递交版（仅图号）。</p>')}
    ${r?.log && !figs.length ? `<pre class="log">${r.log.replace(/</g, '&lt;')}</pre>` : ''}`;

  root.querySelector('[data-render]')?.addEventListener('click', startRender);
  root.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => {
    activeTab = t.dataset.tab; render();
  }));
  root.querySelectorAll('[data-variant]').forEach((b) => b.addEventListener('click', () => {
    variant = b.dataset.variant; render();
  }));
  root.querySelector('[data-export]')?.addEventListener('click', doExport);

  if (current) loadSvg(current);
}

async function loadSvg(fig) {
  const pane = root.querySelector('[data-pane]');
  if (!pane) return;
  const src = fig.variants?.[variant] || fig.svg;
  try {
    const res = await fetch(api.previewUrl(src));
    pane.innerHTML = await res.text();      // 本机服务器生成的 SVG，可信来源
    const svg = pane.querySelector('svg');
    if (svg) {
      svg.querySelectorAll('text[data-numeral]').forEach((t) => {
        t.addEventListener('click', () => {
          const numeral = Number(t.dataset.numeral);
          const entry = (state.render?.numerals?.numerals || [])
            .find((n) => n.numeral === numeral);
          if (entry?.parts?.length) {
            setSelection(entry.parts);
            flashParts(entry.parts);
          }
        });
      });
    }
  } catch { pane.innerHTML = '<p class="hint pad">预览加载失败</p>'; }
}

async function startRender() {
  try { await api.render(); } catch (err) {
    window.alert(err.message);
    return;
  }
  state.rendering = true;
  lastExport = null;
  render();
  poll();
}

function poll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    const st = await api.renderStatus();
    state.rendering = st.running;
    if (!st.running) {
      state.render = st.result;
      activeTab = null;
      bus.emit('rendered');
      render();
    } else poll();
  }, 900);
}

async function doExport() {
  const dwg = root.querySelector('[data-dwg]')?.checked ?? false;
  const btn = root.querySelector('[data-export]');
  btn.disabled = true; btn.textContent = dwg ? '导出中（含 DWG，较慢）…' : '导出中…';
  try {
    lastExport = await api.exportAll(dwg);
  } catch (err) { window.alert('导出失败：' + err.message); }
  render();
}

export function initPreview(el) {
  root = el;
  bus.on('booted', () => { if (state.rendering) poll(); render(); });
}
