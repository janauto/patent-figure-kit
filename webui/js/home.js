// 首页：工程列表（屏①）与新建/上传（屏②）。启动先 await api.init()（按能力接口判定 live / legacy，WP3b #1）。
// 四态由真实数据驱动；?scene / ?upload 是开发截图开关（只有 mock 认 ?scene）。
import { api, readOnly, isLegacy, token } from './api.js';
import { applyBrand, DISPLAY_NAME } from './brand.js';

const $ = (s, el = document) => el.querySelector(s);
import { esc } from './esc.js';
const params = new URLSearchParams(location.search);
const scene = params.get('scene') || '';          // empty | loading | error | ''
const keepStatic = params.has('static');
let cookieSessionOk = false;   // 子应用确认接受 Cookie 会话后，链接不再带 token
// 导航 URL：静态站走开发入口 index.html?project=；live 与 legacy 都走工作台的 /p/{id}/ 子应用。
// live 只用会话 Cookie，链接不带 token（WP3b #10）；legacy 的旧子应用不认 Cookie，仍随 URL 带 token。
const toStudio = (id) => {
  if (readOnly) return `./index.html?project=${encodeURIComponent(id)}${keepStatic ? '&static' : ''}`;
  if (isLegacy) return `/p/${encodeURIComponent(id)}/${(token && !cookieSessionOk) ? `?token=${encodeURIComponent(token)}` : ''}`;
  return `${api.root}p/${encodeURIComponent(id)}/`;
};

function notice(msg, id = 'page-notice') {
  let bar = document.getElementById(id);
  if (!bar) {
    bar = document.createElement('div');
    bar.id = id; bar.className = 'page-notice'; bar.setAttribute('role', 'status');
    document.body.prepend(bar);
  }
  bar.innerHTML = `<span>${esc(msg)}</span><button class="icon-btn" aria-label="关闭提示">✕</button>`;
  bar.querySelector('button').addEventListener('click', () => bar.remove());
}

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

function toast(msg, kind = '', ms = 3600) {
  const box = $('#toast');
  box.className = `toast ${kind}`;
  box.textContent = msg;
  box.hidden = false;
  clearTimeout(box._t);
  box._t = setTimeout(() => { box.hidden = true; }, ms);
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

function errorState(msg, hint, code) {
  // 401：新版工作台但还没有会话——不是「连接失败」，给出从终端完整地址进入的引导（WP3 接线清单 #4）
  if (code === 'auth_required') {
    $('#grid').innerHTML = `<div class="banner error" role="alert" id="auth-required">
      <div><b>需要 token</b><p class="muted">这个工作台只接受本机会话。请从终端启动日志里打印的完整地址进入一次
      （形如 <span class="mono">http://127.0.0.1:端口/?token=…</span>），之后浏览器会记住会话，链接里不再需要 token。</p>
      <p class="hint">终端里找不到？重新运行 <span class="mono">python3 scripts/workbench.py</span>，它会打印完整地址。</p></div>
      <button class="btn" id="retry">我已从完整地址进入，重试</button></div>`;
    $('#retry').addEventListener('click', () => location.reload());
    return;
  }
  $('#grid').innerHTML = `<div class="banner error" role="alert">
    <div><b>连接不上工作台服务</b><p class="muted">${esc(msg)}</p>
    ${hint ? `<p class="hint">${esc(hint)}</p>` : ''}</div>
    <button class="btn" id="retry">重试</button></div>`;
  $('#retry').addEventListener('click', retryConnect);
}

// 重试：重新探测能力接口（清掉上次的探测错误与缓存的 init），恢复后再取列表（Codex R3b）。
// 重新探测得出的模式若变了（例如原来连不上、现在是旧后端），整页重载，让各模块按新模式初始化。
async function retryConnect() {
  const before = api.mode;
  $('#grid').innerHTML = '';
  skeletonGrid();
  const mode = await api.reprobe();
  if (mode !== before && before !== 'pending') { location.reload(); return; }
  if (api.authRequired) { errorState('需要 token', '', 'auth_required'); return; }
  await loadList();
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
  document.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
    if (!window.confirm('删除这个工程？本机的工程目录（含出图结果）会一起删除。')) return;
    try {
      const r = await api.deleteProject(b.dataset.del);
      if (r?.demo) toast('（演示）mock 不删除工程：' + b.dataset.del);
      else { toast('已删除工程', 'ok'); loadList(); }
    } catch (err) { toast('删除失败：' + (err.hint || err.message), 'bad'); }
  }));
}

async function loadList() {
  if (scene === 'loading') { skeletonGrid(); return; }
  if (!cachedProjects.length) skeletonGrid();          // 真实加载态：列表回来之前先给骨架
  try {
    const data = await api.listProjects(scene);
    if (data.loading) { skeletonGrid(); return; }
    renderGrid(data);
  } catch (err) {
    errorState(err.message, err.hint, err.code);
  }
}

// ---------------------------------------------------------------- ② 上传 / 新建
// 时间线按后端作业的阶段走（WP3b #11）：新建 / 示例作业（stage = from-example / prepare）→ 自动接一个候选分图
// 作业（stage = suggest，标签「生成候选分图」「试渲染候选 k/N：图题」）。每步耗时在前端按阶段切换实测。
const UPLOAD_STAGES = [
  ['receive', '接收 STEP 文件'],
  ['prepare', '解析装配层级并生成 3D 网格'],
  ['suggest', '按结构生成候选分图'],
  ['trial', '逐候选试渲染并过 QA'],
];
// 每次导入一个独立的生命周期（Codex R3b）：它自己的 SSE 订阅、定时器与回调都挂在这里；关闭对话框、回到新建页
// 或开始下一次导入时整体作废——旧导入迟到的事件 / 轮询结果一律不再绘制、不再关别人的订阅、不再起候选作业。
let currentImport = null;
let importGen = 0;          // 发起导入的代次：创建请求在途期间关闭或换了导入，回来的结果不再接管界面
function newImport() {
  if (currentImport) currentImport.dispose();
  const imp = {
    alive: true, subs: new Set(), timers: new Set(),
    track(sub) { if (!this.alive) { try { sub.close(); } catch { /* */ } } else this.subs.add(sub); return sub; },
    untrack(sub) { try { sub?.close(); } catch { /* */ } this.subs.delete(sub); },
    every(fn, ms) { const t = setInterval(() => { if (this.alive) fn(); }, ms); this.timers.add(t); return t; },
    clear(t) { clearInterval(t); this.timers.delete(t); },
    dispose() {
      this.alive = false;
      this.subs.forEach((sub) => { try { sub.close(); } catch { /* */ } });
      this.subs.clear();
      this.timers.forEach((t) => clearInterval(t));
      this.timers.clear();
    },
  };
  currentImport = imp;
  return imp;
}
function abandonImport() {
  importGen += 1;
  if (currentImport) { currentImport.dispose(); currentImport = null; }
}

function openUpload() { $('#upload').hidden = false; }
function closeUpload() {
  $('#upload').hidden = true;
  abandonImport();
}

function openUploadIdle(dragging) {
  if (readOnly) { toast('静态演示站只读，无法新建工程', ''); return; }
  abandonImport();                       // 回到新建页：之前那次导入不再占用这块界面
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
      ${api.caps?.ai?.configured ? `<p class="hint" id="up-ai-note">已接入 AI：解析完成后工作台会在后台用 AI 识别结构，
        会把零件清单（零件名、尺寸、装配路径）发给 ${esc(api.caps.ai.provider_label || '大模型通道')}；几何模型不出本机。</p>` : ''}
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

// tl = {title, stages[{state: wait|run|ok|fail, seconds, detail}], failed?, done?, summary?, note?, t0}
function uploadTimeline(tl) {
  const rows = UPLOAD_STAGES.map(([key, label], i) => {
    const s = tl.stages[i] || { state: 'wait' };
    let mark = '<i class="step-dot"></i>'; let meta = '';
    if (s.state === 'ok') { mark = '<i class="step-dot ok">✓</i>'; meta = `<span class="mono step-sec">${(s.seconds || 0).toFixed(2)}s</span>`; }
    else if (s.state === 'fail') mark = '<i class="step-dot fail">✕</i>';
    else if (s.state === 'run') mark = '<i class="spin"></i>';
    const label2 = (i === 0 && tl.example) ? '准备示例工程' : label;
    return `<li class="step ${s.state === 'run' ? 'run' : s.state}" data-step="${key}">${mark}
      <span class="step-label">${esc(s.detail || label2)}</span>${meta}</li>`;
  }).join('');
  const secs = tl.t0 ? Math.max(0, (Date.now() - tl.t0) / 1000) : 0;
  return `<div class="up-progress">
    <h2 class="up-title">${esc(tl.title || '新建工程')}</h2>
    <ol class="steps">${rows}</ol>
    ${tl.failed ? `<div class="banner error" role="alert">
        <div><b>${esc(tl.failed.label)}失败</b><p class="hint">${esc(tl.failed.hint || '')}</p></div>
        ${tl.failed.canEnter ? '<button class="btn" id="up-enter">仍然进入规划台 →</button>' : '<button class="btn" id="up-retry">重试</button>'}</div>`
      : tl.done ? `<div class="up-done">
        <p class="ok-line"><span class="stamp-ok">完成</span>${esc(tl.summary || '解析与候选分图就绪。')}</p>
        <button class="btn primary big" id="up-enter">进入规划台 →</button></div>`
      : `<p class="hint up-running-hint" id="up-elapsed">正在处理（已用 <span class="mono">${secs.toFixed(0)}</span> 秒）。${esc(tl.note || '每步耗时实时显示。')}</p>`}</div>`;
}

// 一条完整的导入流水线：作业 A（新建 / 示例）→ 作业 B（候选分图），共用一条时间线。
// 所有回调先看 imp.alive：这次导入被作废后（关闭 / 换导入），迟到的事件与轮询结果直接丢弃。
function runImportJob(jobId, projectId, title, { example = false, uploadSeconds = null } = {}) {
  const imp = newImport();
  openUpload();
  const tl = { title, example, stages: [], t0: Date.now(), stageT0: Date.now(),
    note: example ? '首次载入示例要现场生成演示模型 STEP 并解析，视机器性能需要几秒到一分钟；之后再载入只需几秒。' : '' };
  const setRun = (i, detail) => {
    for (let k = 0; k < i; k += 1) {
      if (tl.stages[k]?.state !== 'ok') tl.stages[k] = { state: 'ok', seconds: (Date.now() - tl.stageT0) / 1000 };
    }
    if (tl.stages[i]?.state !== 'run') tl.stageT0 = Date.now();
    tl.stages[i] = { state: 'run', detail: detail || tl.stages[i]?.detail };
  };
  const setOk = (i, detail) => {
    tl.stages[i] = { state: 'ok', seconds: (Date.now() - tl.stageT0) / 1000, detail };
    tl.stageT0 = Date.now();
  };
  const paint = () => {
    if (!imp.alive) return;
    $('#upload-body').innerHTML = uploadTimeline(tl);
    bindUploadRun(projectId, title);
  };
  if (uploadSeconds != null) tl.stages[0] = { state: 'ok', seconds: uploadSeconds };
  setRun(uploadSeconds != null ? 1 : 0);
  paint();
  const elapsed = imp.every(() => {
    const el = document.querySelector('#up-elapsed .mono');
    if (el) el.textContent = ((Date.now() - tl.t0) / 1000).toFixed(0);
  }, 1000);
  const stop = () => imp.clear(elapsed);
  const failAt = (i, d, canEnter) => {
    if (!imp.alive) return;
    stop();
    tl.stages[i] = { state: 'fail', detail: tl.stages[i]?.detail };
    tl.failed = { label: UPLOAD_STAGES[i][1], hint: [d?.message, d?.hint].filter(Boolean).join('——'), canEnter };
    paint();
  };
  const startSuggest = async () => {
    if (!imp.alive) return;
    setOk(1); setRun(2, '按结构生成候选分图'); paint();
    let sj;
    try { sj = await api.suggest(projectId, false); } catch (err) { failAt(2, err, true); return; }
    if (!imp.alive) return;
    let settled = false; let subB = null;
    const finish = async (ok, d) => {
      if (settled || !imp.alive) return; settled = true;
      imp.untrack(subB);
      if (!ok) { failAt(tl.stages[3]?.state === 'run' ? 3 : 2, d, true); return; }
      let summary = '';
      try {
        const c = await api.getCandidates(projectId);
        const s = c?.summary || {};
        summary = c?.empty_reason
          ? `候选分图完成：${s.total ?? 0} 个候选都没过闸——进入后请框选分组。`
          : `候选分图完成：${s.total ?? 0} 个候选，${s.pass ?? 0} 个过闸。`;
      } catch { /* 摘要读不到不影响进入 */ }
      if (!imp.alive) return;
      if (tl.stages[2]?.state !== 'ok') setOk(2);
      setOk(3, tl.stages[3]?.detail ? '逐候选试渲染并过 QA' : undefined);
      stop(); tl.done = true; tl.summary = summary; paint();
    };
    subB = imp.track(api.jobEvents(sj.job_id, {
      stage(d) {
        if (!imp.alive) return;
        const label = d.label || '';
        if (/^试渲染候选/.test(label)) { if (tl.stages[2]?.state !== 'ok') setOk(2); setRun(3, label); }
        else if (label && label !== 'running') setRun(2, label);
        paint();
      },
      done() { finish(true); },
      fail(d) { finish(false, d); },
      disconnect() {
        api.getJob(sj.job_id).then((j) => {
          if (j.state === 'succeeded') finish(true);
          else if (['failed', 'cancelled', 'interrupted'].includes(j.state)) finish(false, j.error || { message: j.state });
        }).catch(() => { /* 等重连 */ });
      },
    }));
  };
  let settledA = false; let subA = null; let watch = null;
  const doneA = () => {
    if (settledA || !imp.alive) return; settledA = true;
    imp.clear(watch); imp.untrack(subA);
    startSuggest();
  };
  const failA = (d) => {
    if (settledA || !imp.alive) return; settledA = true;
    imp.clear(watch); imp.untrack(subA);
    failAt(tl.stages[1]?.state === 'run' ? 1 : 0, d, false);
  };
  // 准备作业要等工程「就绪且没有后台消息」才算完成；已接入 AI 时这包括后台结构识别。识别失败时后端把失败原因
  // 留在工程消息里，作业会一直等到 30 分钟超时——这里同时看工程摘要：已就绪且消息是「…未完成」就照常往下走，
  // 并把原因告诉用户（结构识别是可选的，不挡金路径）。
  watch = imp.every(async () => {
    if (settledA) return;
    let p;
    try { p = (await api.getSummary(projectId)) || null; } catch { return; }
    if (!p || settledA || !imp.alive) return;
    if (p.status === 'ready' && p.message) {
      tl.stages[1] = { ...(tl.stages[1] || {}), state: 'run', detail: p.message };
      paint();
      if (/未完成/.test(p.message)) { toast(p.message, '', 6000); doneA(); }
    }
  }, 2500);
  subA = imp.track(api.jobEvents(jobId, {
    stage(d) {
      if (!imp.alive) return;
      if (d.stage === 'from-example') setRun(0, d.label || '生成演示模型 STEP');
      else if (d.stage === 'prepare') setRun(1, d.label || UPLOAD_STAGES[1][1]);
      paint();
    },
    done() { doneA(); },
    fail(d) { failA(d); },
    disconnect() {
      api.getJob(jobId).then((j) => {
        if (j.state === 'succeeded') doneA();
        else if (['failed', 'cancelled', 'interrupted'].includes(j.state)) failA(j.error || { message: j.state });
      }).catch(() => { /* 等重连 */ });
    },
  }));
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
  const ai = pendingAi;
  pendingTitle = title;
  const t0 = Date.now();
  const gen = ++importGen;
  try {
    const res = await api.createProject({ file, title, ai }, params.get('fail'));
    if (gen !== importGen) return;          // 请求在途期间对话框已关闭或换了导入：不再接管界面
    if (isLegacy) {                 // 旧后端：后台解析，回列表轮询到就绪
      closeUpload(); pendingFile = null;
      toast(`已上传「${title}」，正在后台解析，就绪后可打开`, 'ok');
      loadList();
      return;
    }
    runImportJob(res.job_id, res.project_id, title, { uploadSeconds: (Date.now() - t0) / 1000 });
  } catch (err) {                   // 不引用 try 内局部；用保存的上下文让重试可点
    if (gen !== importGen) return;
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
  const gen = ++importGen;
  try {
    const res = await api.fromExample(ex);
    if (gen !== importGen) return;          // 请求在途期间对话框已关闭或换了导入
    const title = { gearbox: '行星齿轮减速器', vise: '台虎钳', valve: '球阀' }[ex];
    runImportJob(res.job_id, res.project_id, `载入示例 · ${title}`, { example: true });
  } catch (err) { if (gen === importGen) toast('载入示例失败：' + (err.hint || err.message), 'bad'); }
}

// 四态截图用（仅开发）：?upload=idle|running|done|fail 强制冻结上传屏
function forceUpload(mode) {
  openUpload();
  if (mode === 'idle') return openUploadIdle();
  const ok = (seconds, detail) => ({ state: 'ok', seconds, detail });
  if (mode === 'running') {
    $('#upload-body').innerHTML = uploadTimeline({ title: '载入示例 · 行星齿轮减速器', example: true, t0: Date.now() - 19000,
      stages: [ok(14.2), ok(3.1), ok(0.4), { state: 'run', detail: '试渲染候选 3/6：行星架组件分解示意图' }] });
  } else if (mode === 'done') {
    $('#upload-body').innerHTML = uploadTimeline({ title: '载入示例 · 行星齿轮减速器', example: true, done: true,
      summary: '候选分图完成：6 个候选，4 个过闸。', stages: [ok(14.2), ok(3.1), ok(0.4), ok(6.9)] });
    bindUploadRun('gearbox');
  } else if (mode === 'fail') {
    $('#upload-body').innerHTML = uploadTimeline({ title: '（我的）液压夹具', t0: Date.now() - 4000,
      stages: [ok(0.41), { state: 'fail' }],
      failed: { label: '解析装配层级并生成 3D 网格', hint: '工程准备失败：STEP 顶层只有 1 个实体，未解析出零件——请确认导出的是装配体而非单件。' } });
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
// 旧版后端兼容模式（开发用，不随产品发布）：顶栏中性提示，导出恒锁定
function showCompatBadge() {
  if (!isLegacy) return;
  const b = document.createElement('span');
  b.className = 'compat-badge'; b.id = 'compat-badge';
  b.textContent = '旧版后端兼容模式：导出需升级后端';
  b.title = '开发用兼容模式（不随产品发布）：可浏览、出图、编辑术语与预览；导出需升级到新版后端。';
  document.querySelector('.hbar-tools')?.prepend(b);
}

async function start() {
  await api.init();
  api.onDeprecation((msg) => notice(msg, 'auth-notice'));
  showCompatBadge();
  initDialogs();
  if (readOnly) document.body.classList.add('is-static');
  if (api.mode === 'live') {                       // 接入对话框写真实地址（不含 token）
    const mcp = $('#mcp-url'); if (mcp) mcp.value = `${location.origin}${api.root}mcp`;
    const ta = document.querySelector('#dlg-integrate textarea');
    if (ta) ta.value = ta.value.replaceAll('http://127.0.0.1:8803/', `${location.origin}${api.root}`);
  }
  await loadList();
  tidyToken();
  const up = params.get('upload');
  if (up) forceUpload(up);
}
start();
