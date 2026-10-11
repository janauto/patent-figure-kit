// 屏④ 出图结果与 QA：盖章总览 + 13 格点阵 + 每项「原因 / 建议 / 直达入口」。
// 四态：未渲染 / 渲染中（SSE 日志）/ 全过（导出解锁）/ 失败（本次作业的诊断 + 修复引导）。
// 「已发布版本」与「本次作业」严格分开（WP1 契约 §13.5.4）：失败的作业不发布，预览与导出仍属上一成功版本。
// 预览里点 text[data-numeral] → 联动高亮对应零件（数据契约不变）。
import { bus, state, setSelection, renderStaleReason, flush, normEtag, resultEtag, resultVersion } from './state.js';
import { api } from './api.js';
import { toast } from './main.js';

let host; let activeFig = null; let variant = 'annotated'; let onlyFail = false;
let sub = null; const logs = [];
// 渲染生命周期（Codex R3h #1/#2）：同一时刻只有一个「当前作业」令牌 active = {seq, jobId}。
// rendering 只由它派生；每个异步回读（SSE 终态、轮询、状态回读）完成后先确认仍是当前 / 最新作业再写状态，
// 迟到的旧作业回读一律丢弃，版本不回退。刷新 / 重进时由 resumeRender() 接上后端仍在跑的作业。
let active = null; let renderSeq = 0; let rendering = false;
function setActive(t) { active = t; rendering = !!t; }
const isCurrent = (t) => active === t;
const isLatest = (t) => renderSeq === t.seq;
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

// 已发布的渲染结果（?state=empty 为四态截图的「未出图」）
function currentRender() {
  if (params.get('state') === 'empty') return null;
  return state.render;
}

// 本次（最近一次）渲染作业。?state=fail / novalidate 为四态截图：演示「本次作业失败」——
// 已发布版本不变，失败诊断来自本次作业（与真实后端的语义一致）。
function currentJob() {
  const force = params.get('state');
  const r = state.render;
  if (force === 'fail' && r?.figures?.length) {
    const f = applyFailOverride(r.figures[0]);
    return { id: 'demo-failed-job', state: 'failed', running: false, ok: false,
      error: { code: 'E_RENDER_NOT_OK', message: `出图未全部过闸/有图缺失：['${f.id}']`, hint: '按每张图的 QA 提示改计划后重渲' },
      result: { ok: false, figures: [f, ...r.figures.slice(1)] }, diag: { failed_figures: [f.id], missing_artefacts: [] } };
  }
  if (force === 'novalidate') {
    return { id: 'demo-failed-job', state: 'failed', running: false, ok: false,
      error: { code: 'E_PLAN_INVALID', message: '计划未通过校验，未渲染', hint: '按校验提示改计划后重渲' },
      result: { ok: false, figures: [], validate: { ok: false, issues: [
        { code: 'E_TERM_FORBIDDEN', severity: 'error', message: '术语 3「PGB-0012-A」像内部件号，不是中文技术名词', hint: '改成中文技术名词，如「行星销」' },
        { code: 'E_FIGURE_EMPTY', severity: 'error', message: '图 fig2 没有任何成员零件', hint: '在 3D 里框选零件后加入该图，或删掉这张空图' }] } },
      diag: { failed_figures: [], missing_artefacts: [], log_tail: '校验未通过，未渲染。\nE_TERM_FORBIDDEN /terms/3\nE_FIGURE_EMPTY /figures/1' } };
  }
  return state.renderJob;
}
const jobFailed = (j) => !!j && ['failed', 'interrupted'].includes(j.state);

// 本次作业里未过闸的图：完整 QA（mock / 子应用）或 WP1 精简行（failed_checks）统一成 qa.checks
function failedFigures(job) {
  return (job.result?.figures || []).filter((f) => !f.pass).map((f) => (f.qa?.checks ? f : {
    ...f, qa: { checks: (f.failed_checks || []).map((c) => ({ id: c.check, value: c.value, threshold: '', hint: c.hint, pass: false })) },
  }));
}

// 本次作业失败的诊断：错误、校验问题、逐图未过项（白话建议 + 直达入口）、缺失产物、日志尾；
// 并写明预览 / 导出属于上一成功版本（本次失败的结果没有发布）。
function jobFailPanel(job, r) {
  const err = (job.error && typeof job.error === 'object') ? job.error : { message: job.error ? String(job.error) : '' };
  const issues = job.result?.validate?.issues || [];
  const missing = job.diag?.missing_artefacts || [];
  const log = job.diag?.log_tail || job.result?.log || '';
  const ver = resultVersion(r);
  return `<section class="job-fail" role="alert">
    <div class="job-fail-head"><span class="stamp-fail">本次出图${job.state === 'interrupted' ? '中断' : '失败'}</span>
      <b>${esc(err.message || '本次出图未成功')}</b>${err.code ? ` <span class="mono hint">${esc(err.code)}</span>` : ''}</div>
    ${err.hint ? `<p class="hint">${esc(err.hint)}</p>` : ''}
    <p class="job-fail-note">${r ? `下面的预览与导出仍是<b>上一成功版本</b>${ver ? `（版本 <span class="mono">${esc(ver)}</span>${r.finished_at ? ` · ${esc(r.finished_at)}` : ''}）` : ''}；本次失败的结果没有发布。`
      : '还没有成功出过图；本次失败的结果没有发布。'}</p>
    ${issues.length ? `<ul class="qa-issues">${issues.map((it) => `<li class="${esc(it.severity || '')}">
      <b class="mono">${esc(it.code || '')}</b> ${esc(it.message || '')}${it.hint ? `<span class="hint"> ↳ ${esc(it.hint)}</span>` : ''}</li>`).join('')}</ul>` : ''}
    ${failedFigures(job).map((f) => `<div class="job-fail-fig"><h4>图${esc(f.number ?? '')}（${esc(f.id)}）未过闸</h4>${failItems(f)}</div>`).join('')}
    ${missing.length ? `<p class="hint">缺失产物：${esc(missing.join('、'))}</p>` : ''}
    ${log ? `<details class="qa-tech"><summary>日志尾部</summary><pre class="qa-log">${esc(String(log).slice(-2000))}</pre></details>` : ''}
  </section>`;
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

function bindCommon() {
  host.querySelector('#rerender')?.addEventListener('click', () => startRender());
  host.querySelector('#rerender-stale')?.addEventListener('click', () => startRender());
  host.querySelector('#qa-render')?.addEventListener('click', () => startRender());
  host.querySelector('#qa-retry')?.addEventListener('click', () => startRender());
  host.querySelectorAll('[data-goto]').forEach((b) => b.addEventListener('click', () => {
    document.querySelector('.stage-tab[data-stage="plan"]').click();
    setTimeout(() => document.querySelector(`.rtab[data-tab="${b.dataset.goto}"]`)?.click(), 60);
  }));
}

function render() {
  if (!host) return;                     // 还没挂载（例如刷新后在别的页签接上了在途作业）
  const r = currentRender();
  const job = currentJob();
  const force = params.get('state');
  const failNow = jobFailed(job) && !rendering;
  // --- 未渲染 / 渲染中 ---
  // legacy 旧后端一开工就清空 out/：渲染期间旧结果的预览文件已不存在，直接显示「渲染中」，不再去取旧预览。
  if (!r || force === 'empty' || (rendering && api.isLegacy)) {
    const running = rendering || force === 'loading';
    host.innerHTML = `<div class="qa-blank ${failNow && !running ? 'qa-error' : ''}">
      <i class="reg reg-tl"></i><i class="reg reg-tr"></i><i class="reg reg-bl"></i><i class="reg reg-br"></i>
      ${running ? `
        <p class="stamp-run"><i class="spin"></i> 渲染中</p>
        <p>正走与模型完全相同的 CLI 与 13 项 QA 闸门，通过后生成交底版与递交版。</p>
        <pre class="qa-log">${esc((logs.length ? logs : ['▶ 解析消隐出图…']).join('\n'))}</pre>`
      : (failNow && force !== 'empty' ? `${jobFailPanel(job, null)}
        <button class="btn big" id="qa-retry">改完计划 · 重新出图</button>`
      : `<p class="stamp-run">未出图</p>
        <h2>把当前计划渲染成专利附图</h2>
        <p class="muted">渲染走与模型完全相同的 CLI 与 QA 闸门；通过后自动生成交底版（图号＋件号表）与递交版（仅图号）。</p>
        ${api.readOnly ? '<p class="hint">静态演示站为预计算只读展示，不在浏览器里出图（需本地安装后的后端）。</p>'
          : '<button class="btn primary big" id="qa-render">渲染出图</button>'}`)}
    </div>`;
    bindCommon();
    return;
  }
  // --- 已发布结果但没有图（只有旧后端会把未过校验的结果当作结果返回：{ok:false,figures:[],validate,log}）---
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
    bindCommon();
    return;
  }
  // --- 已发布结果：全过 / 有失败；本次作业失败时在上方给诊断 ---
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
  const legacy = staleReason === 'legacy';
  const stale = !!staleReason && !legacy;
  const staleMsg = {
    'no-proof': '无法证明这张图对应当前计划——请重新出图以解锁导出。',
    unsaved: '计划有改动还没保存好——保存并重新出图后再导出。',
    conflict: '计划已变更（服务端核对不一致）——请重新出图后再导出。',
  }[staleReason] || '计划已修改，下面的预览可能过期——请重新出图后再导出。';
  const gate = legacy ? '<span class="qa-gate compat">兼容模式 · 导出需升级后端</span>'
    : `<span class="qa-gate ${allPass && !stale ? 'ok' : 'bad'}">${stale ? '结果已过期，需重新出图'
      : (allPass ? `13 项全过 · 导出已解锁${failNow ? '（上一成功版本）' : ''}` : `${figs.filter((f) => !f.pass).length} 张未过，导出禁用`)}</span>`;
  const passHint = legacy ? '13 项全部通过。旧版后端兼容模式下不能导出——升级到新版后端后再打包附图包。'
    : '13 项全部通过。到「导出」页打包附图包。';
  host.innerHTML = `
    <div class="qa-notices">
      ${failNow ? jobFailPanel(job, r) : ''}
      ${stale ? `<div class="stale-bar">${staleMsg}
        <button class="btn sm" id="rerender-stale" ${rendering ? 'disabled' : ''}>${rendering ? '出图中…' : '重新出图'}</button></div>` : ''}
      ${legacy ? `<div class="compat-bar">旧版后端兼容模式：可出图、预览与查看 QA，但无法证明预览对应当前计划；导出需升级到新版后端。</div>` : ''}
    </div>
    <div class="qa-top">
      <div class="qa-tabs">${tabs}</div>
      ${seg}
      <div class="qa-top-right">
        <button class="btn ghost sm" id="rerender" ${rendering ? 'disabled' : ''}>${rendering ? '<i class="spin"></i> 出图中…' : '↻ 重新出图'}</button>
        <label class="switch sm"><input type="checkbox" id="only-fail" ${onlyFail ? 'checked' : ''}> 仅看失败项</label>
        ${gate}
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
        ${!onlyFail && cur?.pass ? `<p class="hint pad">${passHint}</p>` : ''}
      </aside>
    </div>`;

  bindCommon();
  host.querySelectorAll('[data-fig]').forEach((b) => b.addEventListener('click', () => { activeFig = b.dataset.fig; render(); }));
  host.querySelectorAll('[data-variant]').forEach((b) => b.addEventListener('click', () => { variant = b.dataset.variant; render(); }));
  host.querySelector('#only-fail')?.addEventListener('change', (e) => { onlyFail = e.target.checked; render(); });
  host.querySelectorAll('[data-chk]').forEach((b) => b.addEventListener('click', () => {
    host.querySelector('.qa-side .qa-fixes')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }));
  if (cur) loadSvg(cur);
}

async function loadSvg(fig) {
  const pane = host.querySelector('[data-pane]');
  if (!pane) return;
  const src = fig.variants?.[variant] || fig.svg;
  try {
    const res = await fetch(api.previewUrl(src));
    if (!res.ok) throw new Error(`预览 ${res.status}`);   // 错误体不当作 SVG 塞进页面
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

// 把已发布结果的版本证据绑定好——只认后端给的（WP1 契约）：
//   结果自带的输入快照 ETag 与结果 ID（render_job_id / version）优先；结果没带、且它确属本作业时，
//   用本作业提交时后端返回的输入快照（202 / GET job 的 inputs.plan_etag）与作业 ID（= 版本 rid）。
//   前端自己的记录不作背书。旧版后端兼容模式不建立证据（导出恒锁定）。ev 为 null：结果不属于本会话作业。
function bindEvidence(result, ev, jobInputs) {
  state.renderInputs = { etag: null, version: null, conflict: false };
  if (api.isLegacy || !(result && (result.figures || []).length)) return;
  state.renderInputs.etag = resultEtag(result) || (ev ? (normEtag(jobInputs?.plan_etag) || ev.serverEtag) : null);
  state.renderInputs.version = resultVersion(result) || (ev ? ev.jobId : null);
}

// 作业成功：d 是 done 事件或 GET job 的数据；ev 是该作业的后端证据（只属于它）。
async function applyResult(d, token, ev) {
  // 读权威结果：优先作业带回的 result，否则回工程/状态取一次（那份不一定属于本作业 → 不用本作业证据）
  let result = (d && d.result && d.result.figures) ? d.result : null;
  const fromJob = !!result;
  if (!result) {
    try { result = await api.getRenderResult(state.projectId); }
    catch (err) {
      if (!isCurrent(token)) return;
      setActive(null); toast('读取渲染结果失败：' + (err.hint || err.message), 'bad'); render(); return;
    }
    if (!isCurrent(token)) return;       // 回读期间已换了作业：丢弃
  }
  state.render = result || null;
  state.renderJob = { id: ev.jobId || null, state: 'succeeded', running: false, ok: true, error: null, result: null, diag: null };
  bindEvidence(result, fromJob ? ev : null, d?.inputs);
  setActive(null);
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

// 作业失败：先按事件给出失败态并释放锁，再回读渲染状态细化（result = 已发布版本，job = 本次诊断）。
// 回读完成时若期间已发起新作业（renderSeq 变了），这份迟到的回读不得覆盖任何状态（Codex R3h #1）。
async function applyFailure(d, token, ev) {
  if (!isCurrent(token)) return;
  setActive(null);
  state.renderJob = { id: ev.jobId, state: 'failed', running: false, ok: false,
    error: (d?.code || d?.message) ? { code: d.code, message: d.message, hint: d.hint } : null,
    result: d?.job?.result || null, diag: d?.job?.diag || null };
  toast('出图未完成：' + (d?.message || '渲染进程失败'), 'bad');
  bus.emit('rendered');
  render();
  let st = null;
  try { st = await api.getRenderStatus(state.projectId, ev.jobId); } catch { return; }
  if (!isLatest(token) || active) return;          // 迟到：期间已有新作业 → 丢弃，版本不回退
  const pub = st.result;
  const sameVersion = pub && state.render && resultVersion(pub) && resultVersion(pub) === resultVersion(state.render);
  if (!sameVersion) { state.render = pub || null; bindEvidence(state.render, null); }   // 另有版本被发布：只凭它自带的证据
  if (st.job && (!st.job.id || st.job.id === ev.jobId || api.isLegacy)) state.renderJob = st.job;
  bus.emit('rendered');
  render();
}

// 订阅一个作业直到终态。token 是该作业的令牌；ev 是它的后端证据。
function follow(token, ev) {
  let settled = false;                   // 同一作业只采纳一次终态（done / error 与断线校准可能先后到达）
  const finish = (d) => { if (settled) return; settled = true; applyResult(d, token, ev); };
  const fail = (d) => { if (settled) return; settled = true; applyFailure(d, token, ev); };
  try { sub?.close?.(); } catch { /* */ }
  sub = api.jobEvents(token.jobId, {
    log(d) { if (!isCurrent(token)) return; logs.push(d.line); render(); },
    stage(d) { if (!isCurrent(token)) return; if (d.label) logs.push(`✓ ${d.label} · ${(d.seconds || 0).toFixed(2)}s`); render(); },
    done(d) { finish(d); },
    fail(d) { fail(d); },
    disconnect() {                       // 断线：用 GET job 校准终态；作业已不存在（后端重启）就收尾，不让界面卡在「渲染中」
      api.getJob(token.jobId).then((j) => {
        if (!isCurrent(token)) return;
        if (j.state === 'succeeded') finish({ result: j.result, inputs: j.inputs });
        else if (['failed', 'interrupted', 'cancelled'].includes(j.state)) fail({ ...(j.error || {}), message: j.error?.message || `出图${j.state === 'cancelled' ? '已取消' : '未完成'}` });
      }).catch((err) => {
        if (err.status === 404 && isCurrent(token)) fail({ code: 'E_JOB_GONE', message: '找不到这次出图作业（后端可能已重启）', hint: '请重新出图' });
      });
    },
  });
}

const saveBroken = () => state.saveState === 'error' || state.saveState === 'conflict';

async function startRender() {
  if (active) return;                    // 重入锁：有在途作业（含刷新后接上的）时不再启动第二次（按钮也禁用）
  if (api.readOnly) { toast('静态演示站只读，不能出图', ''); return; }
  const token = { seq: ++renderSeq, jobId: null };
  setActive(token); render();            // 先锁，再 await（任何 await 之前就已置位）
  // 先把本地改动落盘成功，确保渲染用的是当前这份 plan（保存期间又有新编辑就再存一轮）
  for (let i = 0; i < 3; i += 1) {
    await flush();
    if (saveBroken() || state.editVersion <= state.savedVersion) break;
  }
  if (!isCurrent(token)) return;
  if (saveBroken()) {
    setActive(null); toast('计划没能保存，未渲染——请先解决保存问题（可能是与他处的冲突）。', 'bad'); render(); return;
  }
  if (state.editVersion > state.savedVersion) {
    setActive(null); toast('计划还在保存中，未渲染——请稍后再出图。', 'bad'); render(); return;
  }
  let job;
  try {
    job = await api.render(state.projectId, { etag: state.planEtag, fail: params.get('rfail') });
  } catch (err) {                        // 被拒绝（409 等）：不碰任何共享证据，直接退出
    if (!isCurrent(token)) return;
    setActive(null); toast('无法开始渲染：' + (err.hint || err.message), 'bad'); render(); return;
  }
  if (!isCurrent(token)) return;
  token.jobId = job.job_id;
  // 本作业的后端证据：提交时冻结的输入快照（WP1 202 的 inputs.plan_etag）+ 作业 ID（= 版本 rid）
  const ev = Object.freeze({ jobId: job.job_id, serverEtag: normEtag(job.inputs?.plan_etag) });
  state.renderJob = { id: job.job_id, state: 'running', running: true, ok: null, error: null, result: null, diag: null };
  logs.length = 0; render();
  follow(token, ev);
}

// 刷新 / 重进工程时接上后端仍在跑的渲染作业（Codex R3h #2）：boot 读到的 render_job 为 queued/running
// 时恢复订阅（live 走 SSE 回放，兼容模式轮询），期间禁用再次渲染，终态自动呈现。幂等。
export function resumeRender() {
  const j = state.renderJob;
  if (active || !j || !j.running || api.readOnly) return;
  const token = { seq: ++renderSeq, jobId: api.isLegacy ? 'legacy:render' : j.id };
  if (!token.jobId) return;
  setActive(token);
  logs.length = 0; logs.push('▶ 接上进行中的出图作业…');
  follow(token, Object.freeze({ jobId: j.id || token.jobId, serverEtag: null }));
  render();
}

export function mountQa(el) {
  host = el;
  resumeRender();
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
