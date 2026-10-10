// 屏④ 出图结果与 QA：盖章总览 + 13 格点阵 + 每项「原因 / 建议 / 直达入口」。
// 四态：未渲染 / 渲染中（SSE 日志）/ 全过（导出解锁）/ 有失败（导出禁用 + 修复引导）。
// 预览里点 text[data-numeral] → 联动高亮对应零件（数据契约不变）。
import { bus, state, setSelection } from './state.js';
import { api } from './api.js';
import { toast } from './main.js';

let host; let activeFig = null; let variant = 'annotated'; let onlyFail = false;
let rendering = false; let sub = null; const logs = [];
const params = new URLSearchParams(location.search);
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
const VARIANTS = { annotated: '交底版 · 图号＋件号表', filing: '递交版 · 仅图号', raw: '原始出图' };

// 每条 QA 失败 → 直达哪个修复入口（去规划台的哪一页）
const FIX_ROUTE = {
  text_height_mm: ['terms', '去术语页把标准件设「不标」'],
  text_slot_ratio: ['terms', '去术语页把该件设「不标」'],
  labels_per_figure: ['figures', '去图纸页拆分本图'],
  leader_crossing: ['figures', '去图纸页拆分本图'],
  label_overlap_pairs: ['terms', '去术语页减少标记'],
  part_bbox_overlap_pairs: ['figures', '去图纸页把本图改为分解图'],
  geometry_occupancy: ['figures', '去图纸页调整版面'],
  sheet_fill: ['figures', '去图纸页换视图或拆图'],
};

// ?state=fail 的客户端演示：把真实 QA 的三项翻成失败（沿用 qa.py 的真实 hint）。
function applyFailOverride(fig) {
  const f = JSON.parse(JSON.stringify(fig));
  const over = {
    text_height_mm: { value: 2.4, threshold: '>=3.5',
      hint: '字太小意味着被标注件在图面上太小：对标准件设 terms[].label="none"，按 split_suggestions 拆图，或把 layout.density 改成 "compact"。' },
    labels_per_figure: { value: 22, threshold: '<=20',
      hint: '拆图：按 assembly.json:split_suggestions 把该 figure 拆成多张，或对标准件设 terms[].label="none" 减少标记。' },
    leader_crossing: { value: 3, threshold: '<=0',
      hint: '引线交叉：减少本图标记数（terms[].label="none"）或按 split_suggestions 拆图。' },
  };
  (f.qa.checks || []).forEach((c) => {
    if (over[c.id]) { c.pass = false; Object.assign(c, over[c.id]); }
  });
  f.pass = false;
  const failed = f.qa.checks.filter((c) => !c.pass).length;
  f.qa.summary = { passed: f.qa.checks.length - failed, failed };
  return f;
}

function currentRender() {
  const force = params.get('state');
  const r = state.render;
  if (force === 'empty') return null;
  if (force === 'fail' && r?.figures?.length) {
    return { ...r, ok: false, figures: [applyFailOverride(r.figures[0]), ...r.figures.slice(1)] };
  }
  return r;
}

function grid(fig) {
  const checks = fig.qa?.checks || [];
  return `<div class="qa-grid" role="list">${checks.map((c) => `
    <button class="qa-cell ${c.pass ? 'pass' : 'fail'}" data-chk="${esc(c.id)}"
      title="${esc(c.id)} · 实测 ${esc(c.value)} · 阈值 ${esc(c.threshold)} · ${c.pass ? '通过' : '超限'}">
      <span class="qa-cell-mark">${c.pass ? '✓' : '✕'}</span>
      <span class="qa-cell-id mono">${esc(c.id)}</span></button>`).join('')}</div>`;
}

function failItems(fig) {
  const fails = (fig.qa?.checks || []).filter((c) => !c.pass);
  if (!fails.length) return '';
  return `<div class="qa-fixes"><h4>需要修的 ${fails.length} 项</h4>${fails.map((c) => {
    const route = FIX_ROUTE[c.id];
    return `<div class="qa-fix">
      <div class="qa-fix-head"><span class="mono">${esc(c.id)}</span>
        <span class="qa-fix-val mono">实测 ${esc(c.value)} · 阈值 ${esc(c.threshold)}</span></div>
      <p class="qa-fix-hint">${esc(c.hint || '')}</p>
      ${route ? `<button class="btn ghost sm" data-goto="${route[0]}">${esc(route[1])} →</button>` : ''}
    </div>`;
  }).join('')}</div>`;
}

function render() {
  const r = currentRender();
  const force = params.get('state');
  // --- 未渲染 / 渲染中 ---
  if (!r || force === 'empty') {
    host.innerHTML = `<div class="qa-blank">
      <i class="reg reg-tl"></i><i class="reg reg-tr"></i><i class="reg reg-bl"></i><i class="reg reg-br"></i>
      ${rendering || force === 'loading' ? `
        <p class="stamp-run"><i class="spin"></i> 渲染中</p>
        <p>正走与模型完全相同的 CLI 与 13 项 QA 闸门，通过后生成交底版与递交版。</p>
        <pre class="qa-log">${esc((logs.length ? logs : ['▶ 解析消隐出图…']).join('\n'))}</pre>`
      : `<p class="stamp-run">未出图</p>
        <h2>把当前计划渲染成专利附图</h2>
        <p class="muted">渲染走与模型完全相同的 CLI 与 QA 闸门；通过后自动生成交底版（图号＋件号表）与递交版（仅图号）。</p>
        <button class="btn primary big" id="qa-render">渲染出图</button>`}
    </div>`;
    host.querySelector('#qa-render')?.addEventListener('click', () => startRender());
    return;
  }
  // --- 全过 / 有失败 ---
  const figs = (r.figures || []).slice().sort((a, b) => (a.number || 99) - (b.number || 99));
  if (!activeFig || !figs.find((f) => f.id === activeFig)) activeFig = figs[0]?.id;
  const cur = figs.find((f) => f.id === activeFig);
  const allPass = r.ok && figs.length && figs.every((f) => f.pass);
  const tabs = figs.map((f) => `<button class="qa-tab ${f.id === activeFig ? 'on' : ''} ${f.pass ? '' : 'fail'}"
    data-fig="${f.id}">图${f.number || ''} ${f.pass ? '✓' : '✕'}</button>`).join('');
  const vlist = cur?.variants ? Object.keys(VARIANTS).filter((v) => cur.variants[v]) : [];
  if (cur && vlist.length && !vlist.includes(variant)) [variant] = vlist;
  const seg = vlist.length > 1 ? `<div class="seg">${vlist.map((v) =>
    `<button class="seg-btn ${v === variant ? 'on' : ''}" data-variant="${v}">${VARIANTS[v]}</button>`).join('')}</div>` : '';

  host.innerHTML = `
    <div class="qa-top">
      <div class="qa-tabs">${tabs}</div>
      ${seg}
      <div class="qa-top-right">
        <label class="switch sm"><input type="checkbox" id="only-fail" ${onlyFail ? 'checked' : ''}> 仅看失败项</label>
        <span class="qa-gate ${allPass ? 'ok' : 'bad'}">
          ${allPass ? '13 项全过 · 导出已解锁' : `${figs.filter((f) => !f.pass).length} 张未过，导出禁用`}</span>
      </div>
    </div>
    <div class="qa-body">
      <div class="qa-preview">
        <i class="reg reg-tl"></i><i class="reg reg-tr"></i><i class="reg reg-bl"></i><i class="reg reg-br"></i>
        <div class="svg-pane" data-pane></div>
      </div>
      <aside class="qa-side">
        <div class="qa-stamp">${cur?.pass
          ? '<span class="stamp-pass big">合格</span>'
          : '<span class="stamp-fail big">未通过</span>'}
          <span class="qa-sum mono">${cur?.qa?.summary?.passed ?? (cur?.qa?.checks?.filter((c) => c.pass).length)}/${cur?.qa?.checks?.length} 项通过</span></div>
        ${grid(cur)}
        ${failItems(cur)}
        ${!onlyFail && cur?.pass ? `<p class="hint pad">13 项全部通过。到「导出」页打包递交件。</p>` : ''}
      </aside>
    </div>`;

  host.querySelectorAll('[data-fig]').forEach((b) => b.addEventListener('click', () => { activeFig = b.dataset.fig; render(); }));
  host.querySelectorAll('[data-variant]').forEach((b) => b.addEventListener('click', () => { variant = b.dataset.variant; render(); }));
  host.querySelector('#only-fail')?.addEventListener('change', (e) => { onlyFail = e.target.checked; render(); });
  host.querySelectorAll('[data-chk]').forEach((b) => b.addEventListener('click', () => {
    host.querySelector('.qa-fixes')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }));
  host.querySelectorAll('[data-goto]').forEach((b) => b.addEventListener('click', () => {
    document.querySelector('.stage-tab[data-stage="plan"]').click();
    setTimeout(() => document.querySelector(`.rtab[data-tab="${b.dataset.goto}"]`)?.click(), 60);
  }));
  if (cur) loadSvg(cur);
}

async function loadSvg(fig) {
  const pane = host.querySelector('[data-pane]');
  if (!pane) return;
  const src = fig.variants?.[variant] || fig.svg;
  try {
    const res = await fetch(api.previewUrl(src));
    pane.innerHTML = await res.text();       // 本机 / 静态站生成的 SVG，可信来源
    const svg = pane.querySelector('svg');
    if (svg) {
      svg.removeAttribute('width'); svg.removeAttribute('height');
      svg.style.maxHeight = '100%'; svg.style.maxWidth = '100%';
      svg.querySelectorAll('text[data-numeral]').forEach((t) => {
        t.addEventListener('click', () => {
          const numeral = Number(t.dataset.numeral);
          const entry = (state.render?.numerals?.numerals || []).find((n) => n.numeral === numeral);
          if (entry?.parts?.length) {
            setSelection(entry.parts);
            toast(`图上「${numeral}」= ${entry.term}（已在结构树与 3D 选中）`);
          }
        });
      });
    }
  } catch { pane.innerHTML = '<p class="hint pad">预览加载失败</p>'; }
}

async function startRender() {
  rendering = true; logs.length = 0; render();
  try {
    const { job_id } = await api.render(state.projectId, { etag: state.planEtag });
    sub = api.jobEvents(job_id, {
      log(d) { logs.push(d.line); render(); },
      stage(d) { if (d.label) logs.push(`✓ ${d.label} · ${(d.seconds || 0).toFixed(2)}s`); render(); },
      done() { rendering = false; toast('渲染完成，13 项 QA 全过', 'ok'); render(); },
      fail(d) { rendering = false; toast('渲染未过闸：' + d.message, 'bad'); render(); },
    });
  } catch (err) { rendering = false; toast('无法开始渲染：' + (err.hint || err.message), 'bad'); render(); }
}

export function mountQa(el) {
  host = el;
  render();
  if (!host._bound) { bus.on('rendered', render); host._bound = true; }
}
