// 右栏·候选分图（F2-lite，WP3b #5）：候选来自后端作业（POST /plan/suggest → SSE「试渲染候选 k/N」→
// GET /plan/candidates）。每个候选都已实际渲染 + 过 QA 后才有状态：通过打绿章、未过给朱红章与「原因 + 建议 +
// 修复直达」。接受走 POST /plan/candidates/accept（If-Match；422 列出解析不到的成员；409 提示重载）——
// 成员在服务端按当前装配体解析，前端不再自己展开。没有候选通过时显示后端的 empty_reason + 「请框选分组」引导。
import { bus, state, setActiveFigure, flush, scheduleValidate } from './state.js';
import { api } from './api.js';
import { toast, applyFixAction, fixLabel, LAYOUT_ERRORS } from './main.js';
import { esc } from './esc.js';

let root;
let sub = null;                 // 在跟的候选作业 SSE
let progress = null;            // {label, progress}
const dismissed = new Set();    // 本页「删除」掉的候选（只影响显示）
const accepted = new Map();     // cid -> 写入后的图 id
const unresolvedBy = new Map(); // cid -> 422 的 unresolved[]

let fixSeq = 0; const fixRegistry = new Map();   // data-fix 按钮 → fix_action 对象
const fixBtn = (fa) => { const k = `cf${++fixSeq}`; fixRegistry.set(k, fa); return `<button class="btn-min" data-fix="${k}">${esc(fixLabel(fa))} →</button>`; };

function candCard(c) {
  const pass = c.status === 'pass';
  const pending = c.status === 'pending';
  const stamp = pending
    ? '<span class="stamp-run"><i class="spin"></i> 试渲染中</span>'
    : (pass ? '<span class="stamp-pass">合格</span>' : '<span class="stamp-fail">未过闸</span>');
  const members = (c.members_display || []).map((m, i) => {
    const part = c.members?.[i]?.part || '';
    return `<span class="chip chip-cn" title="${esc(part)}">${esc(m)}</span>`;
  }).join('');
  const std = (c.standard_parts || []).length
    ? `<p class="cand-std hint">标准件默认不标号：${(c.standard_parts || []).map((p) => `<span class="mono">${esc(p)}</span>`).join('、')}</p>` : '';
  const amb = (c.ambiguous_members || []).length
    ? `<ul class="cand-amb">${c.ambiguous_members.map((a) => `<li>⚠ <span class="mono">${esc(a.part)}</span>：${esc(a.message || '')}</li>`).join('')}</ul>` : '';
  const qaFails = (c.qa?.failed || []);
  const layout = LAYOUT_ERRORS[c.qa?.error_code];
  const qaLine = pass ? '' : (qaFails.length
    ? `<ul class="cand-qa">${qaFails.map((k) => `<li><b>${esc(k.title_zh || k.id)}</b> ${esc(k.hint_plain || '')}
        ${k.fix_action ? fixBtn(k.fix_action) : '<span class="hint">不是你的操作问题</span>'}</li>`).join('')}</ul>`
    : (layout ? `<div class="cand-fixes">${layout.fixes.map(fixBtn).join('')}</div>` : ''));
  const unresolved = unresolvedBy.get(c.id);
  const unresolvedLine = unresolved?.length
    ? `<div class="cand-unresolved" role="alert"><b>未写入计划：</b>这些成员在当前装配体里解析不到——
        <ul>${unresolved.map((u) => `<li><span class="mono">${esc(typeof u.member === 'object' ? JSON.stringify(u.member) : (u.member ?? '—'))}</span> ${esc(u.reason || '')}</li>`).join('')}</ul></div>` : '';
  const done = accepted.get(c.id);
  const acts = api.readOnly
    ? (c.preview?.svg ? `<a class="btn ghost sm" href="${esc(api.previewUrl(c.preview.svg))}" target="_blank" rel="noopener">看试渲染图</a>` : '')
    : done
    ? `<span class="hint">已写入图纸页（${esc(done)}）</span>`
    : (pass
      ? `<button class="btn primary sm" data-accept="${esc(c.id)}">接受，建图</button>
         ${c.preview?.svg ? `<a class="btn ghost sm" data-preview="${esc(c.id)}" href="${esc(api.previewUrl(c.preview.svg))}" target="_blank" rel="noopener">看试渲染图</a>` : ''}
         <button class="btn ghost sm" data-drop="${esc(c.id)}">删除</button>`
      : (pending ? '' : `<button class="btn ghost sm" data-accept-fail="${esc(c.id)}" title="这张图出图时会被闸门拦下，需要按提示修改">仍然接受</button>
         <button class="btn ghost sm" data-drop="${esc(c.id)}">删除</button>`));
  return `<article class="cand-card ${esc(c.status)}" data-cid="${esc(c.id)}">
    <header><b>${esc(c.caption)}</b>${stamp}</header>
    <p class="cand-kind mono">${c.kind === 'assembly' ? '装配图' : '分解图'} · ${c.labels ?? '—'} 个标号${c.source?.label ? ` · ${esc(c.source.label)}` : ''}</p>
    <div class="chips">${members}</div>
    ${std}${amb}
    <p class="cand-reason ${pass ? 'ok' : (pending ? '' : 'bad')}">${esc(c.reason || '')}</p>
    ${!pass && c.suggestion ? `<p class="cand-sugg">建议：${esc(c.suggestion)}</p>` : ''}
    ${qaLine}
    ${!pass && c.detail ? `<details class="qa-tech"><summary>技术细节</summary><p class="mono">${esc(c.detail)}</p></details>` : ''}
    ${unresolvedLine}
    <div class="cand-acts">${acts}</div>
  </article>`;
}

function guideBlock(job) {
  const g = job?.guide || {};
  const steps = g.steps?.length ? g.steps : [
    '在视口按 B 进入框选，或在结构树里点组名',
    '框住要申请的一组零件（左→右窗选 / 右→左交叉选）',
    '到「图纸」页点「＋新建图」，或图卡上「加入所选」',
  ];
  return `<div class="cand-empty" data-empty>
    <p class="stamp-run">${esc(g.title || '请框选分组')}</p>
    <p>${esc(job?.empty_reason || '没有给出能过闸的候选分图——需要你手动框选分组。')}</p>
    <ol class="cand-steps">${steps.map((s, i) => `<li><b>${i + 1}</b> ${esc(s)}</li>`).join('')}</ol>
  </div>`;
}

function render() {
  if (!root) return;
  const job = state.candidatesJob;
  const running = !!(job && job.running) || !!sub;
  const head = `<div class="panel-head"><span class="seal">候选</span>候选分图
    ${state.candidatesUnsupported || api.readOnly ? '' : `<button class="btn-min head-btn" data-resuggest ${running ? 'disabled' : ''}>${job ? '重新给候选' : '生成候选'}</button>`}</div>`;
  if (state.candidatesUnsupported) {
    root.innerHTML = `${head}
      <div class="cand-empty">
        <p class="stamp-run">需要新版后端</p>
        <p>当前连接的是旧版工作台后端，<b>还没有候选分图</b>功能。</p>
        <p>照旧流程也能出图：在 3D 里框选要申请的一组零件（按 <kbd>B</kbd>），
          到「图纸」页点「＋新建图」，或在结构树里用某个结构组一键建图。</p>
      </div>`;
    return;
  }
  if (running) {
    const p = progress || {};
    root.innerHTML = `${head}
      <div class="cand-running" role="status">
        <p class="stamp-run"><i class="spin"></i> 正在生成候选分图</p>
        <p class="mono" id="cand-progress-label">${esc(p.label || '排队中…')}</p>
        <div class="pack-bar"><i style="width:${Math.round((p.progress || 0.05) * 100)}%"></i></div>
        <p class="hint">每个候选都会实际渲染并过 13 项 QA 后才显示结果（三个示例各约 3–8 秒）。</p>
      </div>`;
    bindHead();
    return;
  }
  if (!job) {
    root.innerHTML = `${head}
      <div class="cand-empty">
        <p class="stamp-run">还没有候选分图</p>
        <p>让程序按子装配、同轴组和主轴分解给出几张候选图，每张都会实际试渲染并过 QA。</p>
        ${api.readOnly ? '' : '<button class="btn primary" data-resuggest>生成候选分图</button>'}
      </div>`;
    bindHead();
    return;
  }
  if (job.state && job.state !== 'succeeded') {
    const err = job.error || {};
    root.innerHTML = `${head}
      <div class="cand-empty qa-error" role="alert">
        <p class="stamp-fail">候选分图未完成</p>
        <p>${esc(err.message || `作业状态：${job.state}`)}</p>
        ${err.hint ? `<p class="hint">${esc(err.hint)}</p>` : ''}
        <p class="hint">也可以不用候选：在 3D 里框选零件后到「图纸」页新建图。</p>
      </div>`;
    bindHead();
    return;
  }
  // 过闸的排前面（稳定排序，保留后端给的来源顺序）；未过闸的整机候选留作「故意失败」教学
  const rank = (c) => ({ pass: 0, pending: 1 }[c.status] ?? 2);
  const cands = (state.candidates || []).filter((c) => !dismissed.has(c.id))
    .map((c, i) => [c, i]).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(([c]) => c);
  const passed = cands.filter((c) => c.status === 'pass').length;
  const noPass = !passed || job.empty_reason;
  root.innerHTML = `${head}
    ${noPass ? guideBlock(job) : ''}
    ${cands.length ? `<p class="hint pad">${cands.length} 个候选 · ${passed} 个过闸。每个候选都已实际渲染并过 QA 后才显示状态。
      标准件（螺钉、轴承等）默认不标号。</p>
    <div class="cand-list">${cands.map(candCard).join('')}</div>` : ''}
    ${(job.warnings || []).length ? `<ul class="hint pad">${job.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}`;
  bindHead();
  root.querySelectorAll('[data-accept]').forEach((b) => b.addEventListener('click', () => accept(b.dataset.accept)));
  root.querySelectorAll('[data-accept-fail]').forEach((b) => b.addEventListener('click', () => {
    if (window.confirm('这个候选试渲染时被闸门拦下了：写进计划后，出图会报同样的问题，需要按提示修改。仍然接受？')) accept(b.dataset.acceptFail);
  }));
  root.querySelectorAll('[data-drop]').forEach((b) => b.addEventListener('click', () => { dismissed.add(b.dataset.drop); render(); }));
  root.querySelectorAll('[data-fix]').forEach((b) => b.addEventListener('click', () => applyFixAction(fixRegistry.get(b.dataset.fix))));
}

function bindHead() {
  root.querySelectorAll('[data-resuggest]').forEach((b) => b.addEventListener('click', resuggest));
}

// 跟一个候选分图作业到终态：阶段事件显示「试渲染候选 k/N：图题」，完成后读 GET /plan/candidates（权威）
function follow(jobId) {
  try { sub?.close?.(); } catch { /* */ }
  progress = { label: '排队中…', progress: 0.02 };
  let settled = false;
  const finish = async (ok, d) => {
    if (settled) return; settled = true;
    try { sub?.close?.(); } catch { /* */ }
    sub = null;
    try {
      const fresh = await api.getCandidates(state.projectId);
      if (fresh) { state.candidatesJob = fresh; state.candidates = fresh.candidates; }
      else if (!ok) state.candidatesJob = { job_id: jobId, state: 'failed', running: false, error: d || null };
    } catch (err) {
      state.candidatesJob = { job_id: jobId, state: 'failed', running: false, error: { message: err.message, hint: err.hint } };
    }
    if (!ok && state.candidatesJob?.job_id === jobId && state.candidatesJob.state === 'succeeded') { /* 断线后校准到成功 */ }
    dismissed.clear(); accepted.clear(); unresolvedBy.clear();
    progress = null;
    render();
    const s = state.candidatesJob?.summary;
    if (state.candidatesJob?.state === 'succeeded') toast(`候选分图已更新：${s?.total ?? state.candidates.length} 个候选，${s?.pass ?? 0} 个过闸`, 'ok');
    else toast('候选分图未完成：' + (state.candidatesJob?.error?.message || d?.message || ''), 'bad');
    bus.emit('candidates');
  };
  sub = api.jobEvents(jobId, {
    stage(d) {
      if (settled) return;
      if (d.label && d.label !== 'running') progress = { label: d.label, progress: d.progress || 0 };
      const el = root?.querySelector('#cand-progress-label');
      if (el && root.querySelector('.cand-running')) {
        el.textContent = progress.label;
        const bar = root.querySelector('.cand-running .pack-bar i'); if (bar) bar.style.width = `${Math.round((progress.progress || 0) * 100)}%`;
      } else render();
    },
    done() { finish(true); },
    fail(d) { finish(false, d); },
    disconnect() {
      api.getJob(jobId).then((j) => {
        if (j.state === 'succeeded') finish(true);
        else if (['failed', 'cancelled', 'interrupted'].includes(j.state)) finish(false, j.error || { message: j.state });
      }).catch((err) => { if (err.status === 404) finish(false, { message: '找不到候选分图作业（后端可能已重启）' }); });
    },
  });
  render();
}

async function resuggest() {
  if (sub) return;
  try {
    const { job_id: jobId } = await api.suggest(state.projectId, false);
    state.candidatesJob = { job_id: jobId, state: 'queued', running: true };
    follow(jobId);
  } catch (err) { toast('无法生成候选：' + (err.hint || err.message), 'bad'); }
}

// 刷新 / 重进时候选作业仍在跑：接上它
export function resumeSuggest() {
  const j = state.candidatesJob;
  if (!j || !j.running || !j.job_id || sub || api.readOnly) return;
  follow(j.job_id);
}

// 接受候选（WP3b #5）：先落盘本地改动；POST accept + If-Match；成功后以服务端 plan / ETag 为准。
async function accept(cid) {
  const c = (state.candidates || []).find((x) => x.id === cid);
  if (!c) return;
  const btn = root.querySelector(`[data-cid="${CSS.escape(cid)}"] .cand-acts`);
  if (btn) btn.innerHTML = '<span class="hint"><i class="spin"></i> 写入计划…</span>';
  await flush();
  if (state.saveState === 'error' || state.saveState === 'conflict' || state.editVersion > state.savedVersion) {
    toast('计划没能保存，未接受候选——请先解决保存问题（顶栏可重载）。', 'bad'); render(); return;
  }
  const v0 = state.editVersion; const e0 = state.planEtag;
  let res;
  try {
    res = await api.acceptCandidates(state.projectId, {
      job_id: state.candidatesJob?.job_id, candidate_ids: [cid], mode: 'append', apply_standard_labels: true,
    }, state.planEtag);
  } catch (err) {
    if (err.status === 422) {
      unresolvedBy.set(cid, err.detail?.unresolved || []);
      toast(`候选「${c.caption}」有成员在当前装配体里解析不到，未写入计划。`, 'bad', 5000);
    } else if (err.status === 409) {
      const asmChanged = /装配体已变化|候选作废|候选分图作业/.test(err.message || '');
      toast(asmChanged ? `${err.message}` : '计划已在别处被修改（ETag 不匹配），未写入——请点顶栏「重载」后再接受。', 'bad', 6000);
      if (!asmChanged) { state.saveState = 'conflict'; bus.emit('save-state'); }
    } else toast('接受候选失败：' + (err.hint || err.message), 'bad');
    render();
    return;
  }
  let fresh;
  try { fresh = await api.getPlan(state.projectId); } catch (err) { toast('已写入，但重读计划失败：' + err.message, 'bad'); return; }
  const unchanged = state.editVersion === v0 && state.savedVersion >= v0 && state.planEtag === e0
    && state.saveState !== 'error' && state.saveState !== 'conflict';
  if (!unchanged) {
    toast('候选已写入服务端计划，但等待期间本地又有改动——为不覆盖你的编辑，请刷新后查看。', 'bad');
    return;
  }
  state.plan = fresh.plan;
  state.planEtag = fresh.etag || res.etag || null;
  const fig = (res.added_figures || [])[0];
  accepted.set(cid, fig || '新图');
  unresolvedBy.delete(cid);
  if (state.render) bus.emit('render-stale');
  bus.emit('plan');
  if (fig) setActiveFigure(fig);
  scheduleValidate(50);
  const std = (res.standard_labels_set || []).length;
  toast(`已接受「${c.caption}」，写入图纸页（${fig || ''}）${std ? `；${std} 个标准件设为不标号` : ''}`
    + ((res.accepted_failed || []).length ? '。注意：它试渲染时未过闸，出图前请按提示修改。' : ''), (res.accepted_failed || []).length ? 'bad' : 'ok', 4500);
  render();
}

export function initCandidates(el) {
  root = el;
  ['booted', 'structure', 'candidates'].forEach((evt) => bus.on(evt, render));
}
