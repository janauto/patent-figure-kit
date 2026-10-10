// 工作台首页：示例轮播、工程列表（新建 / 打开 / 准备进度）、设置、外部接入说明。
const fromUrl = new URLSearchParams(location.search).get('token');
if (fromUrl) { try { sessionStorage.setItem('studio-token', fromUrl); } catch { /* 隐私模式 */ } }
const token = fromUrl || (() => { try { return sessionStorage.getItem('studio-token') || ''; } catch { return ''; } })();

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

async function call(method, path, body, raw) {
  const res = await fetch(path, {
    method,
    headers: { 'X-Studio-Token': token, ...(body !== undefined && !raw ? { 'Content-Type': 'application/json' } : {}) },
    body: body === undefined ? undefined : (raw ? body : JSON.stringify(body)),
  });
  if (!res.ok) {
    let d = res.statusText;
    try { d = (await res.json()).detail || d; } catch { /* 保底 */ }
    throw new Error(d);
  }
  return res.json();
}

// ---------------------------------------------------------------- 轮播
const AUTOPLAY_MS = 5200;
let slides = []; let idx = 0; let timer = null; let started = 0; let paused = false;

function showSlide(i) {
  if (!slides.length) return;
  idx = (i + slides.length) % slides.length;
  document.querySelectorAll('.slide').forEach((el, k) => el.classList.toggle('on', k === idx));
  document.querySelectorAll('#dots button').forEach((el, k) => el.classList.toggle('on', k === idx));
  const s = slides[idx];
  $('#cap').innerHTML = `<span class="lbl">${esc(s.label)}</span>
    <span class="txt">${esc(s.caption || '')}</span>
    <span class="meta">${s.kind === 'flow' ? `${s.rows} 个步骤` : `件号表 ${s.rows} 行`} · ${esc(s.project_title)}</span>`;
  started = performance.now();
}

function tick(now) {
  const bar = $('#progress');
  if (slides.length > 1 && !paused) {
    const p = Math.min(1, (now - started) / AUTOPLAY_MS);
    bar.style.width = `${p * 100}%`;
    if (p >= 1) showSlide(idx + 1);
  } else if (paused) { started = now - (parseFloat(bar.style.width) || 0) / 100 * AUTOPLAY_MS; }
  timer = requestAnimationFrame(tick);
}

async function loadCarousel() {
  const data = await call('GET', '/api/wb/carousel');
  slides = data.slides;
  const box = $('#slides');
  if (!slides.length) {
    box.innerHTML = `<div class="slide on"><p class="empty-slide">示例工程正在出图……<br>完成后这里轮播交底版附图与流程图。</p></div>`;
    $('#cap').innerHTML = ''; $('#dots').innerHTML = '';
    return false;
  }
  box.innerHTML = slides.map((s) => `<div class="slide" data-project="${s.project}">
      <span class="tag${s.example ? ' ex' : ''}">${s.example && !s.project_title.startsWith('示例') ? '示例 · ' : ''}${esc(s.project_title)}</span>
      <img src="${s.src}" alt="${esc(s.label)} ${esc(s.caption)}" loading="lazy"></div>`).join('');
  $('#dots').innerHTML = slides.map((_, k) => `<button aria-label="第 ${k + 1} 张" data-dot="${k}"></button>`).join('');
  box.querySelectorAll('.slide').forEach((el) => el.addEventListener('click', () => {
    location.href = `/p/${el.dataset.project}/?token=${encodeURIComponent(token)}`;
  }));
  document.querySelectorAll('[data-dot]').forEach((b) => b.addEventListener('click', () => showSlide(Number(b.dataset.dot))));
  showSlide(Math.min(idx, slides.length - 1));
  return true;
}

function initCarousel() {
  document.querySelectorAll('.carousel .nav').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation(); showSlide(idx + Number(b.dataset.step));
  }));
  const c = $('#carousel');
  c.addEventListener('mouseenter', () => { paused = true; });
  c.addEventListener('mouseleave', () => { paused = false; started = performance.now(); });
  document.addEventListener('keydown', (e) => {
    if (document.querySelector('dialog[open]') || e.target.matches('input, textarea')) return;
    if (e.key === 'ArrowRight') showSlide(idx + 1);
    if (e.key === 'ArrowLeft') showSlide(idx - 1);
  });
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) timer = requestAnimationFrame(tick);
}

// ---------------------------------------------------------------- 功能介绍卡片（静态图，取自本机示例工程）
const withToken = (u) => `${u}${u.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;
const studioUrl = (pid) => `/p/${pid}/?token=${encodeURIComponent(token)}`;
const SC_COLORS = ['#2c7fb8', '#c8552c', '#7a9a01', '#8e5ba6', '#d09a00',
  '#0d8a8a', '#b04a76', '#5b6ee1', '#946b3d', '#6b6b6b'];

function imgCard(card, src, href, meta, emptyText) {
  card.querySelectorAll('.sc-meta').forEach((m) => m.remove());
  const box = $('.sc-img', card);
  if (!src) { box.innerHTML = `<p class="sc-hint">${emptyText}</p>`; return; }
  box.innerHTML = `<a href="${href}"><img src="${withToken(src)}" alt="${esc(meta)}" loading="lazy"></a>`;
  card.insertAdjacentHTML('beforeend', `<p class="sc-meta">${esc(meta)}</p>`);
}

async function loadShowcase() {
  const data = await call('GET', '/api/wb/showcase');
  document.querySelectorAll('[data-slot]').forEach((card) => {
    const it = data.pick[card.dataset.slot];
    const which = card.dataset.slot;
    const src = it && (which === 'filing' ? it.filing : (it.annotated || it.src));
    const label = it ? (it.number ? `图${it.number}` : (it.label || '')) : '';
    imgCard(card, src, it ? studioUrl(it.project) : '#',
      it ? `${it.project_title} · ${label} ${it.caption || ''}` : '', '示例工程出图后，这里展示我们自己模型的成果');
  });
  const st = data.structure;
  if (!st) {
    $('#struct-list').innerHTML = '<p class="sc-hint">示例工程完成 AI 结构识别后，这里展示看得懂的结构组</p>';
  } else {
    $('#struct-sub').textContent = `${st.project_title}：${st.total} 个零件归成 ${st.groups.length} 个结构组`;
    $('#struct-list').innerHTML = st.groups.map((g, i) => `
      <a class="sg-row" style="--g:${SC_COLORS[i % SC_COLORS.length]}" href="${studioUrl(st.project)}">
        <header><b>${esc(g.name)}</b><em>${g.count} 件</em></header>
        <p>${esc(g.sample.join('、'))}${g.count > g.sample.length ? '…' : ''}</p>
        ${g.code ? `<code>${esc(g.code.code)} → <b>${esc(g.code.name)}</b></code>` : ''}
      </a>`).join('');
  }
  document.querySelectorAll('[data-snap]').forEach((card) => {
    const src = st?.snaps?.[card.dataset.snap];
    imgCard(card, src, st ? studioUrl(st.project) : '#', st ? `${st.project_title} · 规划台 3D 视图` : '',
      '在规划台里调好透明度或爆炸后点「存为展示图」，这里就会显示');
  });
}

// ---------------------------------------------------------------- 工程
let pollTimer = null;
const STATUS = { ready: '就绪', preparing: '准备中…', error: '失败' };

function card(p) {
  const href = `/p/${p.id}/?token=${encodeURIComponent(token)}`;
  const thumb = p.thumb ? `<img src="${p.thumb}" alt="${esc(p.title)} 附图预览">` : '<span class="ph">图</span>';
  const stats = p.status === 'ready'
    ? `${p.figures} 张附图${p.figures ? `（QA 通过 ${p.figures_pass}）` : ''} · ${p.flowcharts} 张流程图`
    : '';
  return `<article class="card">
    <a class="thumb" href="${href}">${thumb}</a>
    <div class="body">
      <h3>${p.example ? '<span class="chip-ex">示例</span>' : ''}${esc(p.title)}</h3>
      ${p.description ? `<p class="desc">${esc(p.description)}</p>` : ''}
      <p class="stats">${esc(p.step_name)}${stats ? ' · ' + stats : ''}</p>
      <p class="status ${p.status}">${STATUS[p.status] || p.status}${p.message ? ' · ' + esc(p.message) : ''}</p>
      <div class="row">
        <a class="open${p.status === 'ready' ? '' : ' disabled'}" href="${href}">打开</a>
        ${p.example ? '' : `<button class="btn-min" data-del="${p.id}">删除</button>`}
      </div>
    </div></article>`;
}

function newCard() {
  return `<article class="card new">
    <label class="drop" id="drop">
      <strong>＋ 新建工程</strong>
      <span>拖入 STEP 装配体（.stp / .step），或点这里选择文件</span>
      <input type="file" accept=".stp,.step,.STP,.STEP" hidden id="file">
    </label>
    <div class="body">
      <input type="text" id="new-title" placeholder="工程名称（如：回转机构）">
      <input type="text" id="new-path" placeholder="或填本机 STEP 路径，回车创建">
    </div></article>`;
}

async function loadProjects() {
  const st = await call('GET', '/api/wb/state');
  $('#data-dir').textContent = `数据目录：${st.data_dir}（不进 git）`;
  renderBadge(st.llm);
  $('#grid').innerHTML = st.projects.map(card).join('') + newCard();
  bindGrid();
  const busy = st.projects.some((p) => p.status === 'preparing' || (p.status === 'ready' && p.message));
  clearTimeout(pollTimer);
  if (busy) pollTimer = setTimeout(refreshAll, 3000);
  return st;
}

async function refreshAll() {
  await loadProjects();
  await loadCarousel();
  await loadShowcase();
}

async function create(file, path) {
  const title = $('#new-title').value.trim();
  try {
    if (file) {
      $('#drop strong').textContent = `上传中 ${(file.size / 1048576).toFixed(1)} MB…`;
      await call('POST', `/api/wb/projects?name=${encodeURIComponent(file.name)}&title=${encodeURIComponent(title || file.name.replace(/\.(stp|step)$/i, ''))}`, file, true);
    } else {
      await call('POST', '/api/wb/projects', { step_path: path, title: title || undefined });
    }
  } catch (err) { window.alert('新建失败：' + err.message); }
  refreshAll();
}

function bindGrid() {
  const drop = $('#drop'); const file = $('#file');
  file.addEventListener('change', () => file.files[0] && create(file.files[0]));
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => { const f = e.dataTransfer.files[0]; if (f) create(f); });
  $('#new-path').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.value.trim()) create(null, e.target.value.trim());
  });
  document.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
    if (!window.confirm('删除这个工程？本机的工程目录（含出图结果）会一起删除。')) return;
    await call('DELETE', `/api/wb/projects/${b.dataset.del}`);
    refreshAll();
  }));
}

// ---------------------------------------------------------------- 设置 / 接入
function renderBadge(llm) {
  const b = $('#llm-badge');
  b.textContent = llm.provider === 'none' ? 'AI 未配置' : `AI：${llm.provider_label}`;
  b.classList.toggle('off', llm.provider === 'none');
}

async function openSettings() {
  const s = await call('GET', '/api/wb/settings');
  const f = $('#form-settings');
  f.provider.value = s.llm.configured_provider || 'auto';
  f.api_key.value = '';
  f.base_url.value = s.llm.base_url || '';
  f.model.value = s.llm.model || '';
  $('#key-state').textContent = s.llm.api_key_set
    ? `已配置密钥（${s.llm.key_source}，末四位 ${s.llm.api_key_hint}）` : '尚未配置 DeepSeek 密钥';
  $('#test-result').textContent = `当前生效：${s.llm.provider_label}`;
  $('#dlg-settings').showModal();
}

async function saveSettings(extra) {
  const f = $('#form-settings');
  const body = { provider: f.provider.value, deepseek: { api_key: f.api_key.value.trim(),
    base_url: f.base_url.value.trim(), model: f.model.value.trim() }, ...extra };
  const res = await call('PUT', '/api/wb/settings', body);
  f.api_key.value = '';
  renderBadge(res.llm);
  $('#key-state').textContent = res.llm.api_key_set
    ? `已配置密钥（${res.llm.key_source}，末四位 ${res.llm.api_key_hint}）` : '尚未配置 DeepSeek 密钥';
  $('#test-result').textContent = `已保存 · 当前生效：${res.llm.provider_label}`;
}

async function openIntegrate() {
  const s = await call('GET', '/api/wb/settings');
  $('#mcp-url').value = `${s.mcp_url}?token=${s.token}`;
  $('#qwen-json').value = JSON.stringify(s.qwen_mcp_json, null, 2);
  $('#docs-link').href = s.rest_docs; $('#docs-link').textContent = s.rest_docs;
  $('#token').value = s.token;
  const base = s.mcp_url.replace(/\/mcp$/, '');
  $('#curl').value = `# 列出工程\ncurl -H "Authorization: Bearer $TOKEN" ${base}/v1/projects\n\n`
    + `# 文字生成流程图（DeepSeek）\ncurl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \\\n`
    + `  -d '{"text":"上电后采集箱体温度，高于设定值则启动风扇……","title":"箱体温度控制方法的流程图"}' \\\n  ${base}/v1/flowcharts/generate`;
  $('#dlg-integrate').showModal();
}

function initDialogs() {
  document.querySelectorAll('[data-open]').forEach((b) => b.addEventListener('click', () => {
    (b.dataset.open === 'settings' ? openSettings : openIntegrate)().catch((e) => window.alert(e.message));
  }));
  $('[data-save]').addEventListener('click', () => saveSettings({}).catch((e) => { $('#test-result').textContent = e.message; }));
  $('[data-clear-key]').addEventListener('click', () => {
    if (window.confirm('清除本机保存的 DeepSeek 密钥？')) saveSettings({ clear_key: true });
  });
  $('[data-test]').addEventListener('click', async () => {
    $('#test-result').textContent = '测试中…';
    try {
      const r = await call('POST', '/api/wb/settings/test');
      $('#test-result').textContent = `✓ ${r.provider} 连接正常（${r.seconds}s）`;
    } catch (e) { $('#test-result').textContent = `✗ ${e.message}`; }
  });
  $('[data-reveal]').addEventListener('click', (e) => {
    const t = $('#token'); t.type = t.type === 'password' ? 'text' : 'password';
    e.target.textContent = t.type === 'password' ? '显示' : '隐藏';
  });
  document.querySelectorAll('[data-copy]').forEach((b) => b.addEventListener('click', async () => {
    const el = document.getElementById(b.dataset.copy);
    try { await navigator.clipboard.writeText(el.value); b.textContent = '已复制'; } catch { el.select(); }
    setTimeout(() => { b.textContent = b.dataset.copy === 'qwen-json' ? '复制 JSON' : '复制'; }, 1500);
  }));
}

async function start() {
  if (!token) {
    document.body.innerHTML = '<div class="fatal">缺少访问 token：请从启动工作台时终端打印的完整地址进入。</div>';
    return;
  }
  initCarousel(); initDialogs();
  try { await refreshAll(); } catch (e) {
    document.querySelector('#home').insertAdjacentHTML('afterbegin', `<div class="fatal">连接失败：${esc(e.message)}</div>`);
  }
}

start();
