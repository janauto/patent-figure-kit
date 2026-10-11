// 屏④ 出图结果与 QA：盖章总览 + 13 格点阵 + 每项「原因 / 建议 / 直达入口」。
// 四态：未渲染 / 渲染中（SSE 日志）/ 全过（导出解锁）/ 有失败（导出禁用 + 修复引导）。
// 预览里点 text[data-numeral] → 联动高亮对应零件（数据契约不变）。
import { bus, state, setSelection, renderStale, renderStaleReason, planDigest, flush, digestStoreSet } from './state.js';
import { api } from './api.js';
import { toast } from './main.js';

let host; let activeFig = null; let variant = 'annotated'; let onlyFail = false;
let rendering = false; let sub = null; const logs = [];
const params = new URLSearchParams(location.search);
import { esc } from './esc.js';
const VARIANTS = { annotated: '交底版 · 图号＋件号表', filing: '递交版 · 仅图号', raw: '原始出图' };

// 13 项 QA 的前端静态映射：中文名 + 白话建议（面向不碰 JSON 的用户）+ 直达入口。
// 后端若提供 hint_plain，优先用后端的（见下）。
const QA_MAP = {
  geometry_occupancy: { name: '几何占版', plain: '图里非几何的东西（表格/说明）占太多，图本身太小。到参数页调小明细表或把版面调紧。', route: ['params', '去参数页调整版面'] },
  sheet_fill: { name: '纸面填充', plain: '图没把纸填满，看起来空。到参数页让系统自动求最优角度，或换个视图/拆图。', route: ['params', '去参数页换视图/角度'] },
  text_height_mm: { name: '标号字高', plain: '标号字太小，多半是被标的零件在图上太小。把螺钉等标准件设成「不标」，或拆成更细的分图。', route: ['terms', '去术语页把标准件设「不标」'] },
  text_slot_ratio: { name: '标号与件比例', plain: '某个被标的零件相对字号太小。把它设成「不标」，或单独拆一张放大图。', route: ['terms', '去术语页设「不标」'] },
  label_overlap_pairs: { name: '标号相撞', plain: '标号互相压在一起。减少本图标号数（标准件设「不标」），或拆图。', route: ['terms', '去术语页减少标号'] },
  leader_hits_numeral_box: { name: '引线压数字', plain: '引线压住了数字。减少标号数或拆图。', route: ['figures', '去图纸页拆分本图'] },
  leader_crossing: { name: '引线交叉', plain: '引线互相交叉，读图者分不清指谁。减少本图标号数，或拆成多张。', route: ['figures', '去图纸页拆分本图'] },
  leader_anchor_gap: { name: '引线落点', plain: '引线没落在它指的零件轮廓上。换个视图或调整零件分组。', route: ['params', '去参数页换视图'] },
  part_bbox_overlap_pairs: { name: '零件压叠', plain: '零件在图面上叠在一起。把本图改成分解图，或把版面调松。', route: ['params', '去参数页调松版面'] },
  labels_per_figure: { name: '单图标号数', plain: '一张图塞了太多标号。按建议把它拆成几张分图，或对标准件设「不标」。', route: ['figures', '去图纸页拆分本图'] },
  non_numeral_text_ratio: { name: '多余文字', plain: '图面出现了不该有的文字。专利附图只允许几何、引线、标号与图题。', route: ['terms', '去术语页检查'] },
  non_continuous: { name: '线型连续性', plain: '这不是计划的问题，是写出端的问题，请把结果交给维护者。', route: null },
  forbidden_text: { name: '禁用文字', plain: '术语里出现了内部件号/图号形状的字符串，改成中文技术名词。', route: ['terms', '去术语页改中文名'] },
  file_readable: { name: '文件可读', plain: '这张图没写出来，确认渲染真的产出了文件。', route: null },
};
const qaName = (id) => QA_MAP[id]?.name || id;

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
  if (force === 'novalidate') {          // 四态截图：计划未过校验、无图
    return { ok: false, figures: [],
      validate: { ok: false, issues: [
        { code: 'E_TERM_FORBIDDEN', severity: 'error', message: '术语 3「PGB-0012-A」像内部件号，不是中文技术名词', hint: '改成中文技术名词，如「行星销」' },
        { code: 'E_FIGURE_EMPTY', severity: 'error', message: '图 fig2 没有任何成员零件', hint: '在 3D 里框选零件后加入该图，或删掉这张空图' }] },
      log: '校验未通过，未渲染。\nE_TERM_FORBIDDEN /terms/3\nE_FIGURE_EMPTY /figures/1' };
  }
  if (force === 'fail' && r?.figures?.length) {
    return { ...r, ok: false, figures: [applyFailOverride(r.figures[0]), ...r.figures.slice(1)] };
  }
  return r;
}

function grid(fig) {
  const checks = fig.qa?.checks || [];
  return `<div class="qa-grid" role="list">${checks.map((c) => `
    <button class="qa-cell ${c.pass ? 'pass' : 'fail'}" data-chk="${esc(c.id)}"
      title="${esc(qaName(c.id))}（${esc(c.id)}） · 实测 ${esc(c.value)} · 阈值 ${esc(c.threshold)} · ${c.pass ? '通过' : '超限'}">
      <span class="qa-cell-mark">${c.pass ? '✓' : '✕'}</span>
      <span class="qa-cell-name">${esc(qaName(c.id))}</span></button>`).join('')}</div>`;
}

function failItems(fig) {
  const fails = (fig.qa?.checks || []).filter((c) => !c.pass);
  if (!fails.length) return '';
  return `<div class="qa-fixes"><h4>需要修的 ${fails.length} 项</h4>${fails.map((c) => {
    const map = QA_MAP[c.id] || {};
    const route = map.route;
    const plain = c.hint_plain || map.plain || '';     // 后端若给白话 hint，优先用后端的
    return `<div class="qa-fix">
      <div class="qa-fix-head"><b>${esc(map.name || c.id)}</b>
        <span class="qa-fix-val mono">实测 ${esc(c.value)} · 阈值 ${esc(c.threshold)}</span></div>
      <p class="qa-fix-hint">${esc(plain)}</p>
      ${c.hint ? `<details class="qa-tech"><summary>技术细节</summary>
        <p class="mono">${esc(c.id)}</p><p>${esc(c.hint)}</p></details>` : ''}
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
        ${api.readOnly ? '<p class="hint">静态演示站为预计算只读展示，不在浏览器里出图（需本地安装后的后端）。</p>'
          : '<button class="btn primary big" id="qa-render">渲染出图</button>'}`}
    </div>`;
    host.querySelector('#qa-render')?.addEventListener('click', () => startRender());
    return;
  }
  // --- 渲染返回了结果但没有图：计划未过校验或渲染异常（plan_studio 返回 {ok:false,figures:[],validate,log}）---
  if (!(r.figures || []).length) {
    const issues = (r.validate?.issues || []);
    host.innerHTML = `<div class="qa-blank qa-error">
      <i class="reg reg-tl"></i><i class="reg reg-tr"></i><i class="reg reg-bl"></i><i class="reg reg-br"></i>
      <p class="stamp-fail">未出图</p>
      <h2>这份计划没能渲染出图</h2>
      <p class="muted">${issues.length ? '服务器校验没通过，先按下面的问题改计划：' : '渲染没有产出任何图。下面是服务器返回的信息：'}</p>
      ${issues.length ? `<ul class="qa-issues">${issues.map((it) => `<li class="${esc(it.severity || '')}">
        <b class="mono">${esc(it.code || '')}</b> ${esc(it.message || '')}${it.hint ? `<span class="hint"> ↳ ${esc(it.hint)}</span>` : ''}</li>`).join('')}</ul>` : ''}
      ${r.log ? `<pre class="qa-log">${esc(String(r.log).slice(-2000))}</pre>` : ''}
      <button class="btn big" id="qa-retry">改完计划 · 重新出图</button>
    </div>`;
    host.querySelector('#qa-retry')?.addEventListener('click', () => startRender());
    return;
  }
  // --- 全过 / 有失败 ---
  const figs = (r.figures || []).slice().sort((a, b) => (a.number || 99) - (b.number || 99));
  if (!activeFig || !figs.find((f) => f.id === activeFig)) activeFig = figs[0]?.id;
  const cur = figs.find((f) => f.id === activeFig);
  const allPass = r.ok && figs.length && figs.every((f) => f.pass);
  const tabs = figs.map((f) => `<button class="qa-tab ${f.id === activeFig ? 'on' : ''} ${f.pass ? '' : 'fail'}"
    data-fig="${esc(f.id)}">图${esc(f.number ?? "")} ${f.pass ? '✓' : '✕'}</button>`).join('');
  const vlist = cur?.variants ? Object.keys(VARIANTS).filter((v) => cur.variants[v]) : [];
  if (cur && vlist.length && !vlist.includes(variant)) [variant] = vlist;
  const seg = vlist.length > 1 ? `<div class="seg">${vlist.map((v) =>
    `<button class="seg-btn ${v === variant ? 'on' : ''}" data-variant="${v}">${VARIANTS[v]}</button>`).join('')}</div>` : '';

  const staleReason = renderStaleReason();
  const stale = !!staleReason;
  const staleMsg = staleReason === 'no-proof'
    ? '无法证明这张图对应当前计划——请重新出图以解锁导出。'
    : (staleReason === 'unsaved'
      ? '计划有改动还没保存好——保存并重新出图后再导出。'
      : '计划已修改，下面的预览可能过期——请重新出图后再导出。');
  host.innerHTML = `
    ${stale ? `<div class="stale-bar">${staleMsg}
      <button class="btn sm" id="rerender-stale" ${rendering ? 'disabled' : ''}>${rendering ? '出图中…' : '重新出图'}</button></div>` : ''}
    <div class="qa-top">
      <div class="qa-tabs">${tabs}</div>
      ${seg}
      <div class="qa-top-right">
        <button class="btn ghost sm" id="rerender" ${rendering ? 'disabled' : ''}>${rendering ? '<i class="spin"></i> 出图中…' : '↻ 重新出图'}</button>
        <label class="switch sm"><input type="checkbox" id="only-fail" ${onlyFail ? 'checked' : ''}> 仅看失败项</label>
        <span class="qa-gate ${allPass && !stale ? 'ok' : 'bad'}">
          ${stale ? '结果已过期，需重新出图'
            : (allPass ? '13 项全过 · 导出已解锁' : `${figs.filter((f) => !f.pass).length} 张未过，导出禁用`)}</span>
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
        ${!onlyFail && cur?.pass ? `<p class="hint pad">13 项全部通过。到「导出」页打包附图包。</p>` : ''}
      </aside>
    </div>`;

  host.querySelector('#rerender')?.addEventListener('click', () => startRender());
  host.querySelector('#rerender-stale')?.addEventListener('click', () => startRender());
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

// evidence：本次渲染作业「启动成功时」捕获的输入证据（{etag,digest}），只属于这一个作业。
async function applyResult(d, evidence) {
  // 读权威结果：优先作业 done 带回的 result，否则回工程/状态取一次
  let result = (d && d.result && d.result.figures) ? d.result : null;
  if (!result) {
    try { result = await api.getRenderResult(state.projectId); }
    catch (err) { rendering = false; toast('读取渲染结果失败：' + (err.hint || err.message), 'bad'); render(); return; }
  }
  state.render = result || null;
  // 绑定版本证据，只用「该作业自己的 evidence」——绝不读共享可变量，避免被后来的点击/被拒作业篡改。
  if (result && (result.figures || []).length) {
    if (state.planEtag) state.renderInputs = { etag: result.inputs?.plan_etag || null, digest: null };  // live：只认后端 ETag
    else { state.renderInputs = { etag: null, digest: evidence.digest }; digestStoreSet(state.projectId, evidence.digest); }  // legacy：该作业输入摘要
  } else {
    state.renderInputs = { etag: null, digest: null };
    if (!state.planEtag) digestStoreSet(state.projectId, null);
  }
  rendering = false;
  activeFig = null;
  bus.emit('rendered');
  const figs = result?.figures || [];
  const allPass = result?.ok && figs.length && figs.every((f) => f.pass);
  let msg;
  if (!figs.length) msg = (result?.validate && result.validate.ok === false)
    ? '计划未过校验，未出图——按页面提示改计划后重渲' : '渲染没有产出任何图';
  else if (allPass) msg = `渲染完成，${figs.length} 张图 13 项 QA 全过`;
  else msg = `渲染完成，但有 ${figs.filter((f) => !f.pass).length} 张未过闸——按提示修改后重渲`;
  toast(msg, allPass ? 'ok' : 'bad');
  render();
}

async function startRender() {
  if (rendering) return;                 // 重入锁：渲染在途时不再启动第二次（按钮也禁用）
  if (api.readOnly) { toast('静态演示站只读，不能出图', ''); return; }
  rendering = true; render();            // 先锁，再 await（任何 await 之前就已置位）
  await flush();                         // 先把本地改动落盘成功，确保渲染用的是当前这份 plan
  if (state.saveState === 'error' || state.saveState === 'conflict') {
    rendering = false; toast('计划没能保存，未渲染——请先解决保存问题（可能是与他处的冲突）。', 'bad'); render(); return;
  }
  let job;
  try {
    job = await api.render(state.projectId, { etag: state.planEtag, fail: params.get('rfail') });
  } catch (err) {                        // 被拒绝（409 等）：不碰任何共享证据，直接退出
    rendering = false; toast('无法开始渲染：' + (err.hint || err.message), 'bad'); render(); return;
  }
  const evidence = { digest: planDigest(), etag: state.planEtag };   // 作业启动成功后才捕获，封闭在闭包里
  logs.length = 0; render();
  sub = api.jobEvents(job.job_id, {
    log(d) { logs.push(d.line); render(); },
    stage(d) { if (d.label) logs.push(`✓ ${d.label} · ${(d.seconds || 0).toFixed(2)}s`); render(); },
    done(d) { applyResult(d, evidence); },
    fail(d) {
      rendering = false;
      toast('出图未完成：' + (d.message || '渲染进程失败'), 'bad');
      api.getRenderResult(state.projectId).then((r) => { if (r) { state.render = r; bus.emit('rendered'); } render(); }).catch(() => render());
    },
    disconnect() {
      api.getJob(job.job_id).then((j) => { if (j.state === 'succeeded') applyResult({ result: j.result }, evidence); }).catch(() => {});
    },
  });
}

export function mountQa(el) {
  host = el;
  render();
  if (!host._bound) {
    bus.on('rendered', render);
    bus.on('render-stale', () => { if (!$stageHidden()) render(); });
    host._bound = true;
  }
}
function $stageHidden() {
  const s = document.querySelector('.stage-qa');
  return !s || s.hidden;
}
