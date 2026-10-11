// 首页：工程列表（屏①）与新建/上传（屏②）。对 mock 开发；四态由数据与 ?scene/?upload 开关驱动。
import { api, readOnly, isLegacy, token } from './api.js';
import { applyBrand, DISPLAY_NAME } from './brand.js';

const $ = (s, el = document) => el.querySelector(s);
import { esc } from './esc.js';
const params = new URLSearchParams(location.search);
const scene = params.get('scene') || '';          // empty | loading | error | ''
const keepStatic = params.has('static');
let cookieSessionOk = false;   // 子应用确认接受 Cookie 会话后，链接不再带 token
// 导航 URL 保留后端形态：legacy 走正式 /p/{id}/（子应用认 Cookie 前仍需带 token）；live/static 走开发入口。
const toStudio = (id) => (isLegacy
  ? `/p/${encodeURIComponent(id)}/${(token && !cookieSessionOk) ? `?token=${encodeURIComponent(token)}` : ''}`
  : `./index.html?project=${encodeURIComponent(id)}${keepStatic ? '&static' : ''}`);

applyBrand();

// ---------------------------------------------------------------- 主题
(function theme() {
  let saved = null;
  try { saved = localStorage.getItem('inkline-theme'); } catch { /* 隐私模式 */ }
  if (saved) document.documentElement.dataset.theme = saved;
  $('#theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.dataset.theme === 'dark' ? '' : 'dark';
    if (cur) document.documentElement.dataset.theme = cur;
    else document.documentElement.removeAttribute('data-theme');
    try { localStorage.setItem('inkline-theme', cur); } catch { /* */ }
  });
})();

function toast(msg, kind = '') {
  const box = $('#toast');
  box.className = `toast ${kind}`;
  box.textContent = msg;
  box.hidden = false;
  clearTimeout(box._t);
  box._t = setTimeout(() => { box.hidden = true; }, 3600);
}

// ---------------------------------------------------------------- AI 徽标（中性，不报错）
function renderAiBadge(llm) {
  const b = $('#ai-badge');
  if (!llm || !llm.configured) {
    b.classList.add('off');
    b.textContent = 'AI 未接入';
    b.title = 'AI 起名可选；未接入时金路径照常走，只是零件名要手填';
  } else {
    b.classList.remove('off');
    b.textContent = `AI · ${llm.provider_label || '已接入'}`;
    b.title = 'AI 只起中文名，编号与版面由程序发';
  }
}

// ---------------------------------------------------------------- 工程卡
const STATUS = {
  ready: { label: '就绪', cls: 'ready' },
  preparing: { label: '准备中', cls: 'preparing' },
  error: { label: '失败', cls: 'error' },
};

function card(p) {
  const st = STATUS[p.status] || { label: p.status, cls: '' };
  const thumb = p.thumb
    ? `<img src="${esc(api.previewUrl(p.thumb))}" alt="${esc(p.title)} 附图预览" loading="lazy">`
    : `<span class="thumb-ph">${p.status === 'preparing' ? '<i class="spin"></i>' : '图'}</span>`;
  const stats = p.status === 'ready'
    ? `<span class="mono">${p.figures} 图</span><i>·</i><span class="mono">QA 过 ${p.figures_pass}/${p.figures}</span>`
    : (p.message ? `<span class="muted">${esc(p.message)}</span>` : '');
  const canOpen = p.status === 'ready';
  return `<article class="card ${st.cls}" data-id="${p.project_id}">
    <a class="card-thumb" href="${canOpen ? toStudio(p.project_id) : '#'}"
       aria-label="${esc(p.title)}（${st.label}）"
       ${canOpen ? '' : 'aria-disabled="true" tabindex="-1"'}>
      ${thumb}
      ${p.example ? '<span class="chip-ex">示例</span>' : ''}
    </a>
    <div class="card-body">
      <h2 class="card-ttl">${esc(p.title)}</h2>
      <p class="card-step mono">${esc(p.step_name)}</p>
      <p class="card-stats"><span class="dot dot-${st.cls}"></span>${st.label}
        ${stats ? `<span class="sep"></span>${stats}` : ''}</p>
      <div class="card-row">
        <a class="btn ${canOpen ? 'primary' : ''}${canOpen ? '' : ' disabled'}"
           href="${canOpen ? toStudio(p.project_id) : '#'}">${canOpen ? '打开' : (p.status === 'error' ? '查看原因' : '准备中…')}</a>
        ${p.example ? '' : `<button class="btn ghost" data-del="${p.project_id}">删除</button>`}
      </div>
    </div></article>`;
}

function newCard() {
  if (readOnly) {
    return `<article class="card new readonly">
      <div class="drop"><strong>静态演示站 · 只读</strong>
      <span>这里是预计算展示，无需上传自有模型。本地安装后即可新建工程。</span></div></article>`;
  }
  return `<article class="card new">
    <button class="drop" id="drop" type="button">
      <span class="drop-plus">＋</span>
      <strong>新建工程</strong>
      <span>拖入 STEP 装配体（.stp / .step），或点此选择文件</span>
    </button></article>`;
}

function skeletonGrid() {
  $('#grid').innerHTML = Array.from({ length: 4 }, () =>
    `<article class="card skel"><div class="card-thumb sk"></div>
      <div class="card-body"><div class="sk sk-line w60"></div>
      <div class="sk sk-line w40"></div><div class="sk sk-line w80"></div></div></article>`).join('');
}

function emptyState() {
  // 空态回答四问：放什么 / 还没有 / 怎么放 / 填满长这样（WP0 §A6、research §五①）
  $('#grid').innerHTML = `<div class="empty-wrap">
    <div class="empty-sheet">
      <i class="reg reg-tl"></i><i class="reg reg-tr"></i><i class="reg reg-bl"></i><i class="reg reg-br"></i>
      <p class="empty-kicker"><span class="seal">空</span>还没有工程</p>
      <h2>把一个 STEP 装配体变成规范专利附图</h2>
      <p class="empty-lede">${DISPLAY_NAME}从 3D 装配体出按国家知识产权局形式要求排版的说明书附图：
        标号、引线、版面由程序确定性计算，可复现、可离线、可审计。</p>
      <div class="empty-acts">
        <button class="btn primary big" id="load-example">一键载入示例 · 行星齿轮减速器</button>
        <button class="btn big" id="empty-upload">上传我的 STEP</button>
      </div>
      <p class="hint">载入示例可随时退出，不会污染你的工程目录。
        <a id="empty-nostep">我没有 STEP 怎么办？</a></p>
    </div></div>`;
  $('#load-example').addEventListener('click', () => startExample('gearbox'));
  $('#empty-upload').addEventListener('click', openUploadIdle);
  $('#empty-nostep').addEventListener('click', () => $('#dlg-nostep').showModal());
}

function errorState(msg, hint) {
  $('#grid').innerHTML = `<div class="banner error" role="alert">
    <div><b>连接不上工作台服务</b><p class="muted">${esc(msg)}</p>
    ${hint ? `<p class="hint">${esc(hint)}</p>` : ''}</div>
    <button class="btn" id="retry">重试</button></div>`;
  $('#retry').addEventListener('click', loadList);
}

function renderGrid(data) {
  $('#data-dir').textContent = data.data_dir ? `数据目录：${data.data_dir}` : '';
  renderAiBadge(data.llm);
  cachedProjects = data.projects || [];
  if (!cachedProjects.length) { emptyState(); return; }
  $('#grid').innerHTML = cachedProjects.map(card).join('') + newCard();
  bindGrid();
  // 若有准备中工程，轮询刷新（真实后端用 SSE，这里简化）
  if (cachedProjects.some((p) => p.status === 'preparing')) {
    clearTimeout(pollTimer); pollTimer = setTimeout(loadList, 4000);
  }
}

let cachedProjects = [];
let pollTimer = null;

function bindGrid() {
  const drop = $('#drop');
  if (drop) {
    drop.addEventListener('click', () => openUploadIdle());
    ['dragenter', 'dragover'].forEach((t) => document.addEventListener(t, (e) => {
      if (!readOnly && e.dataTransfer?.types?.includes('Files') && $('#upload').hidden) {
        e.preventDefault(); openUploadIdle(true);
      }
    }));
  }
  document.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', () => {
    if (window.confirm('删除这个工程？本机的工程目录（含出图结果）会一起删除。')) {
      toast('（演示）已请求删除 ' + b.dataset.del);
    }
  }));
}

async function loadList() {
  if (scene === 'loading') { skeletonGrid(); return; }
  try {
    const data = await api.listProjects(scene);
    if (data.loading) { skeletonGrid(); return; }
    renderGrid(data);
  } catch (err) {
    errorState(err.message, err.hint);
  }
}

// ---------------------------------------------------------------- ② 上传 / 新建
const UPLOAD_STAGES = [
  ['接收', '接收 STEP 文件'],
  ['解析', '解析装配层级'],
  ['网格', '生成 3D 网格（GLB）'],
  ['候选分图', '按结构给候选分图'],
  ['试渲染', '逐候选试渲染并过 QA'],
];
let uploadSub = null;

function openUpload() { $('#upload').hidden = false; }
function closeUpload() {
  $('#upload').hidden = true;
  if (uploadSub) { uploadSub.close(); uploadSub = null; }
}

function openUploadIdle(dragging) {
  if (readOnly) { toast('静态演示站只读，无法新建工程', ''); return; }
  openUpload();
  $('#upload-body').innerHTML = `
    <div class="up-drop ${dragging ? 'over' : ''}" id="up-drop">
      <i class="reg reg-tl"></i><i class="reg reg-tr"></i><i class="reg reg-bl"></i><i class="reg reg-br"></i>
      <div class="up-drop-inner">
        <span class="up-glyph">↧</span>
        <strong>把 Creo / SolidWorks 导出的 .step / .stp 拖到这里</strong>
        <span class="muted">或 <button class="link" id="up-browse" type="button">浏览文件</button> · 单文件上限 200 MB</span>
      </div>
      <input type="file" id="up-file" accept=".stp,.step,.STP,.STEP" hidden>
    </div>
    <div class="up-form">
      <label class="field">工程名称 <input id="up-title" placeholder="默认取文件名，如：回转机构"></label>
      <label class="switch"><input type="checkbox" id="up-ai" checked> 上传后立即跑 AI 结构识别</label>
      <div class="up-or"><span>没有自己的 STEP？</span>
        <button class="btn" id="up-ex-gearbox">示例 · 行星齿轮减速器</button>
        <button class="btn" id="up-ex-vise">示例 · 台虎钳</button>
        <button class="btn" id="up-ex-valve">示例 · 球阀</button>
      </div>
    </div>`;
  const drop = $('#up-drop'); const file = $('#up-file');
  const pick = () => file.click();
  $('#up-browse').addEventListener('click', (e) => { e.stopPropagation(); pick(); });
  drop.addEventListener('click', (e) => { if (!e.target.closest('button')) pick(); });
  file.addEventListener('change', () => { const f = file.files[0]; if (f) startUpload(f); });
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => {
    e.preventDefault(); e.stopPropagation(); drop.classList.add('over');
  }));
  ['dragleave', 'dragend'].forEach((t) => drop.addEventListener(t, () => drop.classList.remove('over')));
  drop.addEventListener('drop', (e) => {
    e.preventDefault(); e.stopPropagation(); drop.classList.remove('over');
    const f = e.dataTransfer?.files?.[0];
    if (f) startUpload(f); else toast('没检测到文件', 'bad');
  });
  $('#up-ex-gearbox').addEventListener('click', () => startExample('gearbox'));
  $('#up-ex-vise').addEventListener('click', () => startExample('vise'));
  $('#up-ex-valve').addEventListener('click', () => startExample('valve'));
}

function uploadTimeline(active, stages, { failed, done, title } = {}) {
  const rows = UPLOAD_STAGES.map(([name, label], i) => {
    const s = stages[i];
    let cls = 'wait'; let mark = '<i class="step-dot"></i>'; let meta = '';
    if (s && s.ok) { cls = 'ok'; mark = '<i class="step-dot ok">✓</i>';
      meta = `<span class="mono step-sec">${s.seconds.toFixed(2)}s</span>`; }
    else if (s && !s.ok) { cls = 'fail'; mark = '<i class="step-dot fail">✕</i>'; }
    else if (i === active) { cls = 'run'; mark = '<i class="spin"></i>'; }
    let detail = label;
    if (name === '解析' && s && s.ok) detail = '解析装配层级 · 15 零件 / 4 子装配';
    if (name === '候选分图' && s && s.ok) detail = '按结构给候选分图 · 2 个候选';
    return `<li class="step ${cls}">${mark}
      <span class="step-label">${detail}</span>${meta}</li>`;
  }).join('');
  return `<div class="up-progress">
    <h2 class="up-title">${esc(title || '新建工程')}</h2>
    <ol class="steps">${rows}</ol>
    ${failed ? `<div class="banner error">
        <div><b>${esc(failed.label)}失败</b><p class="hint">${esc(failed.hint || '')}</p></div>
        <button class="btn" id="up-retry">重试</button></div>`
      : done ? `<div class="up-done">
        <p class="ok-line"><span class="stamp-ok">完成</span>解析与候选分图就绪。</p>
        <button class="btn primary big" id="up-enter">进入规划台 →</button></div>`
      : `<p class="hint up-running-hint">正在处理，可稍候；每步耗时实时显示。</p>`}</div>`;
}

function runImportJob(jobId, projectId, title) {
  openUpload();
  const stages = [];
  let active = 0;
  const paint = (extra) => { $('#upload-body').innerHTML = uploadTimeline(active, stages, { title, ...extra }); bindUploadRun(projectId, title); };
  paint();
  uploadSub = api.jobEvents(jobId, {
    stage(d) {
      if (d.stage === 'queued') return;
      const idx = UPLOAD_STAGES.findIndex(([n]) => n === d.stage);
      if (idx >= 0) { stages[idx] = { ok: true, seconds: d.seconds || 0 }; active = idx + 1; }
      paint();
    },
    fail(d) {
      const idx = UPLOAD_STAGES.findIndex(([n]) => n === d.stage);
      if (idx >= 0) stages[idx] = { ok: false };
      paint({ failed: { label: UPLOAD_STAGES[idx]?.[1] || d.stage, hint: d.hint } });
    },
    done() { active = UPLOAD_STAGES.length; paint({ done: true }); },
  });
}

function bindUploadRun(projectId, title) {
  $('#up-enter')?.addEventListener('click', () => { location.href = toStudio(projectId); });
  $('#up-retry')?.addEventListener('click', () => { if (pendingFile) startUpload(pendingFile, title); else openUploadIdle(); });
}

let pendingFile = null;   // 失败重试保留原文件

function validateStep(file) {
  if (!file) return '请选择 STEP 文件';
  if (!/\.(stp|step)$/i.test(file.name)) return '只收 .stp / .step 装配体——其它格式先从 CAD 导出 STEP';
  if (file.size === 0) return '文件是空的，请重新导出';
  if (file.size > 200 * 1024 * 1024) return `文件 ${(file.size / 1048576).toFixed(0)} MB，超过 200 MB 上限`;
  return '';
}

let pendingTitle = ''; let pendingAi = true;   // 完整上传上下文：重试不丢文件/标题/开关
async function startUpload(file, titleOverride) {
  if (readOnly) return;
  const err0 = validateStep(file);
  if (err0) { toast(err0, 'bad'); return; }
  pendingFile = file;
  const title = (titleOverride || $('#up-title')?.value?.trim() || file.name.replace(/\.(stp|step)$/i, ''));
  const ai = $('#up-ai') ? $('#up-ai').checked : pendingAi;
  pendingTitle = title; pendingAi = ai;
  try {
    const res = await api.createProject({ file, title, ai }, params.get('fail'));
    if (isLegacy) {                 // 旧后端：后台解析，回列表轮询到就绪
      closeUpload(); pendingFile = null;
      toast(`已上传「${title}」，正在后台解析，就绪后可打开`, 'ok');
      loadList();
      return;
    }
    runImportJob(res.job_id, res.project_id, title);
  } catch (err) {                   // 不引用 try 内局部；用保存的上下文让重试可点
    openUpload();
    $('#upload-body').innerHTML = `<div class="up-progress"><h2 class="up-title">${esc(title)}</h2>
      <div class="banner error"><div><b>上传失败</b><p class="hint">${esc(err.hint || err.message)}</p></div>
      <button class="btn" id="up-retry">重试（保留原文件）</button></div></div>`;
    $('#up-retry')?.addEventListener('click', () => { if (pendingFile) startUpload(pendingFile, pendingTitle); });
  }
}

async function startExample(ex) {
  if (readOnly) { toast('静态演示站只读，无法载入示例', ''); return; }
  if (isLegacy) {                   // 旧后端的示例已在列表里，直接打开
    const p = cachedProjects.find((x) => x.example) || cachedProjects[0];
    if (p) { location.href = toStudio(p.project_id); return; }
    toast('示例工程正在准备，请稍候刷新', '');
    return;
  }
  try {
    const res = await api.fromExample(ex);
    const title = { gearbox: '行星齿轮减速器', vise: '台虎钳', valve: '球阀' }[ex];
    runImportJob(res.job_id, res.project_id, `载入示例 · ${title}`);
  } catch (err) { toast('载入示例失败：' + (err.hint || err.message), 'bad'); }
}

// 四态截图用：?upload=idle|running|done|fail 强制冻结上传屏
function forceUpload(mode) {
  openUpload();
  const full = UPLOAD_STAGES.map((_, i) => ({ ok: true, seconds: [0.41, 1.63, 1.22, 0.88, 1.42][i] }));
  if (mode === 'idle') return openUploadIdle();
  if (mode === 'running') {
    const partial = full.slice(0, 2);
    $('#upload-body').innerHTML = uploadTimeline(2, partial, { title: '载入示例 · 行星齿轮减速器' });
  } else if (mode === 'done') {
    $('#upload-body').innerHTML = uploadTimeline(5, full, { title: '载入示例 · 行星齿轮减速器', done: true });
    bindUploadRun('gearbox');
  } else if (mode === 'fail') {
    const partial = [full[0], { ok: false }];
    $('#upload-body').innerHTML = uploadTimeline(1, partial, {
      title: '（我的）液压夹具',
      failed: { label: '解析装配层级', hint: 'STEP 顶层只有 1 个实体，未解析出零件——请确认导出的是装配体而非单件。' } });
  }
}

// ---------------------------------------------------------------- 命令 / 搜索（F5-lite）
function openCmdk() {
  const box = $('#cmdk'); box.hidden = false;
  const input = $('#cmdk-input'); input.value = ''; input.focus();
  paintCmdk('');
}
function closeCmdk() { $('#cmdk').hidden = true; }
// 统一执行：先关命令面板再执行动作，并把焦点移到新界面首个控件（finding 10）
function execCmdk(i) {
  const it = cmdkItems[i];
  if (!it) return;
  closeCmdk();
  it.go();
  requestAnimationFrame(() => {
    const first = document.querySelector('#upload:not([hidden]) #up-browse, #upload:not([hidden]) button, dialog[open] button, .empty-acts button');
    first?.focus();
  });
}
let cmdkIdx = 0; let cmdkItems = [];
function paintCmdk(q) {
  const ql = q.trim().toLowerCase();
  const projItems = cachedProjects
    .filter((p) => !ql || p.title.toLowerCase().includes(ql) || p.step_name.toLowerCase().includes(ql))
    .map((p) => ({ label: p.title, meta: p.step_name, go: () => { location.href = toStudio(p.project_id); } }));
  // 静态只读站不提供任何创建/上传入口（不发写请求）
  const actions = (readOnly ? [
    { label: '接入 MCP / API', meta: '命令', go: () => $('#dlg-integrate').showModal() },
  ] : [
    { label: '新建工程 / 上传 STEP', meta: '命令', go: openUploadIdle },
    { label: '载入示例 · 行星齿轮减速器', meta: '命令', go: () => startExample('gearbox') },
    { label: '接入 MCP / API', meta: '命令', go: () => $('#dlg-integrate').showModal() },
  ]).filter((a) => !ql || a.label.toLowerCase().includes(ql));
  cmdkItems = [...projItems, ...actions];
  cmdkIdx = Math.min(cmdkIdx, Math.max(0, cmdkItems.length - 1));
  $('#cmdk-list').innerHTML = cmdkItems.map((it, i) => `<li class="${i === cmdkIdx ? 'on' : ''}" data-i="${i}">
    <span>${esc(it.label)}</span><span class="mono muted">${esc(it.meta)}</span></li>`).join('')
    || '<li class="empty">没有匹配项</li>';
  $('#cmdk-list').querySelectorAll('[data-i]').forEach((li) => li.addEventListener('click',
    () => execCmdk(Number(li.dataset.i))));
}

// ---------------------------------------------------------------- 对话框与全局键
// ---------------------------------------------------------------- 设置 · AI 通道（legacy 接真实后端）
async function openSettings() {
  const f = $('#form-settings');
  $('#test-result').textContent = ''; f.api_key.value = '';
  try {
    const s = await api.getSettings();
    const llm = s.llm || {};
    if (llm.configured_provider) f.provider.value = llm.configured_provider;
    $('#key-state').textContent = llm.api_key_set
      ? `已配置密钥（${llm.key_source || '本机'}，末 4 位 ${llm.api_key_hint || '····'}）`
      : '尚未配置 DeepSeek 密钥';
    $('#key-state').textContent += api.settingsSupported() ? '' : '（mock 演示，不落盘）';
  } catch (e) { $('#key-state').textContent = '读取设置失败：' + (e.hint || e.message); }
  $('#dlg-settings').showModal();
}
async function saveSettings() {
  const f = $('#form-settings');
  if (!api.settingsSupported()) { $('#dlg-settings').close(); toast('mock 模式不保存设置（仅演示）', ''); return; }
  try {
    const res = await api.saveSettings({ provider: f.provider.value,
      deepseek: { api_key: f.api_key.value.trim() } });
    f.api_key.value = '';
    renderAiBadge(res.llm ? { configured: res.llm.provider !== 'none', provider_label: res.llm.provider_label } : null);
    $('#key-state').textContent = res.llm?.api_key_set
      ? `已配置密钥（末 4 位 ${res.llm.api_key_hint || '····'}）` : '尚未配置密钥';
    toast('已保存 AI 通道设置', 'ok');
  } catch (e) { toast('保存失败：' + (e.hint || e.message), 'bad'); }
}
async function testSettings() {
  $('#test-result').textContent = '测试中…';
  if (!api.settingsSupported()) { $('#test-result').textContent = 'mock 模式无法测试真实连接'; return; }
  try { const r = await api.testConnection(); $('#test-result').textContent = `✓ ${r.provider || ''} 连接正常（${r.seconds ?? '?'}s）`; }
  catch (e) { $('#test-result').textContent = '✗ ' + (e.hint || e.message); }
}

function initDialogs() {
  // 全局兜底：任何地方拖放文件都 preventDefault，避免浏览器把 .stp 当页面打开（导致整站被冲掉）
  ['dragover', 'drop'].forEach((t) => document.addEventListener(t, (e) => {
    if (e.dataTransfer?.types?.includes('Files')) {
      e.preventDefault();
      if (t === 'drop' && $('#upload').hidden && !readOnly) {
        const f = e.dataTransfer.files[0];
        if (f) { openUploadIdle(); startUpload(f); }
      }
    }
  }));
  document.querySelectorAll('[data-open]').forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.open === 'settings') openSettings();
    else $(`#dlg-${b.dataset.open}`).showModal();
  }));
  $('#no-step').addEventListener('click', () => $('#dlg-nostep').showModal());
  if (readOnly) $('#nostep-example')?.remove();   // 静态站不提供「先用示例跑一遍」的创建入口
  $('#nostep-example')?.addEventListener('click', () => { $('#dlg-nostep').close(); startExample('gearbox'); });
  $('#form-settings [data-save]')?.addEventListener('click', saveSettings);
  $('#form-settings [data-test]')?.addEventListener('click', testSettings);
  $('#upload-close').addEventListener('click', closeUpload);
  $('#cmd-open').addEventListener('click', openCmdk);
  document.querySelectorAll('[data-copy]').forEach((b) => b.addEventListener('click', async () => {
    const el = document.getElementById(b.dataset.copy);
    try { await navigator.clipboard.writeText(el.value); b.textContent = '已复制'; } catch { el.select(); }
    setTimeout(() => { b.textContent = '复制'; }, 1500);
  }));
  document.addEventListener('keydown', (e) => {
    if (e.key === '/' && !$('#cmdk').hidden === false && !e.target.matches('input,textarea')) {
      e.preventDefault(); openCmdk();
    }
    if (e.key === 'Escape') { closeCmdk(); if (!$('#upload').hidden) closeUpload(); }
    if (!$('#cmdk').hidden) {
      if (e.key === 'ArrowDown') { e.preventDefault(); cmdkIdx = Math.min(cmdkIdx + 1, cmdkItems.length - 1); paintCmdk($('#cmdk-input').value); }
      if (e.key === 'ArrowUp') { e.preventDefault(); cmdkIdx = Math.max(cmdkIdx - 1, 0); paintCmdk($('#cmdk-input').value); }
      if (e.key === 'Enter') { e.preventDefault(); execCmdk(cmdkIdx); }
    }
  });
  $('#cmdk-input').addEventListener('input', (e) => { cmdkIdx = 0; paintCmdk(e.target.value); });
  $('#cmdk').addEventListener('click', (e) => { if (e.target.id === 'cmdk') closeCmdk(); });
  $('#upload').addEventListener('click', (e) => { if (e.target.id === 'upload') closeUpload(); });
}

// 首页减少 token 泄露：legacy 下若 Cookie 会话可用，就把地址栏里的长期 token 去掉。
// （子应用 /p/{id}/ 目前仍需 URL token——那由 WP1 的子应用 Cookie 鉴权合并后统一去除。）
async function tidyToken() {
  if (!isLegacy || !token || !params.has('token')) return;
  // 只有当「子应用也接受 Cookie 会话」时才敢去掉地址栏 token——否则去掉后进 /p/{pid}/ 会 401。
  // 现阶段 plan_studio 的 guard 不认 Cookie（实测 cookie-only 为 401），所以这里基本是安全空操作，
  // 直到 WP1 的子应用 Cookie 鉴权合并。探测真实子应用，而非只探首页。
  const pid = (cachedProjects.find((p) => p.status === 'ready') || cachedProjects[0])?.project_id;
  if (!pid) return;
  try { document.cookie = `wb_token=${encodeURIComponent(token)}; path=/; SameSite=Strict`; } catch { /* */ }
  try {
    const sub = await fetch(`/p/${encodeURIComponent(pid)}/api/state`, { credentials: 'same-origin' });  // 不带 token 头
    if (sub.ok) {       // 子应用认 Cookie → 可安全去掉地址栏 token，并停止在链接里传播
      cookieSessionOk = true;
      const u = new URL(location.href); u.searchParams.delete('token');
      history.replaceState(null, '', u.pathname + (u.search ? u.search : '') + u.hash);
    }
  } catch { /* 保留地址栏 token */ }
}

// ---------------------------------------------------------------- 启动
async function start() {
  initDialogs();
  if (readOnly) document.body.classList.add('is-static');
  await loadList();
  tidyToken();
  const up = params.get('upload');
  if (up) forceUpload(up);
}
start();
