// 数据适配器：三种后端形态走同一组方法。
//   live   —— 新版工作台（docs/openapi.yaml 的 /v1/* 契约 + /p/{pid}/ 子应用，带作业 + SSE + 候选分图）。
//   legacy —— **旧版后端兼容模式（开发用，不随产品发布）**：旧工作台在 /p/{id}/ 用同步 /api/*，没有候选分图 /
//             作业 / SSE / 输入冻结——只保留浏览、渲染、术语编辑、预览；**导出一律锁定**（修复轮 7 架构裁决）。
//   static —— 读版本化 manifest.json，供 GitHub Pages 等静态演示站只读运行（S2-16）。
//
// 模式判定（WP3b 接线 #1）：不按路径判。静态（?static / file:// / 注入的 meta）之外，一律探测工作台根的
// GET /v1/capabilities：200 → live；401 → live（新后端但未登录：首页给「从终端完整地址进入」的引导）；
// 只有 404 → legacy（旧后端没有能力接口）。首页与 /p/{pid}/ 用同一个探测。调用方先 await api.init()。
//
// 两个根（WP3b 接线 #2/#3）：
//   root          工作台根：/v1/*、/files/*、/api/wb/*。页面在 /p/{pid}/ 下时是 /p/ 之前的部分，否则是页面目录。
//   sub(pid)      子应用根 = root + p/{pid}/：api/state、api/render/status、api/preview/*、api/model.glb、
//                 api/structure*、api/flowcharts*。live 无论页面在哪都按工程 ID 拼它（mock 的开发入口也一样）。
//
// 鉴权（WP3b 接线 #10）：live 只用会话 Cookie（wb_token，HttpOnly），不发 token 头、不存 sessionStorage、链接不带 token。
// 地址栏带 ?token= 时：先用它探测一次（服务端优先认有效 Cookie；只靠 query 通过时回 Deprecation 头）→ 据此提示，
// 再请求工作台首页 /?token= 由服务端下发 Cookie，随后把 token 从地址栏抹掉。legacy 维持旧行为（旧后端不认 Cookie）。

const params = new URLSearchParams(location.search);
const path = location.pathname;
// 静态发布：build_static.py 会注入 <meta name="inkline-mode" content="static">
const metaStatic = !!document.querySelector('meta[name="inkline-mode"][content="static"]');
const forcedStatic = params.has('static') || location.protocol === 'file:' || metaStatic;

// 页面所在目录（静态站子路径 / 子应用前缀都靠它）
const base = new URL('.', location.href).pathname;
// /p/{pid}/ 子应用页面：工程 ID 与工作台根都从路径取（与 workbench.Dispatcher 的匹配一致）
const subMatch = path.match(/^(.*\/)p\/([a-z0-9][a-z0-9-]{1,47})\/(?:[^/]*)$/);
export const pagePid = subMatch ? subMatch[2] : null;
export const root = forcedStatic ? base : (subMatch ? subMatch[1] : base);

const urlToken = params.get('token') || '';
// 只有 legacy 用：旧后端的子应用不认 Cookie，token 只能随 URL / 请求头走
export let token = '';

export let MODE = forcedStatic ? 'static' : 'pending';
export const readOnly = forcedStatic;
export let isLegacy = false;

let caps = null;              // GET /v1/capabilities（live）或 manifest.capabilities（static）
let authRequired = false;     // live 但未登录（401）
let probeError = null;        // 网络错误等：首页显示「连接不上」
let deprecatedNotice = null;  // 地址栏 token 只靠 query 通过（Deprecation 头）时的提示文案
let settingsReal = false;     // live 下工作台是否提供 /api/wb/settings（真实后端有，mock 无）
let currentPid = pagePid || params.get('project') || '';

function rel(p) { return (p || '').replace(/^\//, ''); }
const sub = (pid) => `${root}p/${encodeURIComponent(pid || currentPid)}/`;

class ApiError extends Error {
  constructor(message, status, code, hint, detail) {
    super(message); this.status = status; this.code = code; this.hint = hint; this.detail = detail;
  }
}

const deprecationListeners = [];
function noteDeprecation(res) {
  if (!res || !res.headers) return;
  const dep = res.headers.get('X-Auth-Deprecated') || (res.headers.get('Deprecation') === 'true' ? 'query-token' : '');
  if (!dep) return;
  deprecatedNotice = deprecatedNotice || '这次是靠地址里的 token 通过鉴权的——这种方式已弃用（服务端回了 Deprecation 头）。'
    + '已为本次会话换成会话 Cookie；以后请从工作台首页进入，链接里不要再带 token。';
  deprecationListeners.forEach((fn) => { try { fn(deprecatedNotice); } catch { /* */ } });
}

async function raw(method, url, { body, headers = {}, formData } = {}) {
  // 统一写保护：静态只读模式绝不发任何写请求（POST/PUT/DELETE/PATCH），从请求层兜底拦截。
  if (readOnly && !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())) {
    throw new ApiError('静态演示站只读，不能执行写操作', 0, 'read_only');
  }
  const auth = (MODE === 'legacy' && token) ? { 'X-Studio-Token': token, Authorization: `Bearer ${token}` } : {};
  const init = { method, credentials: 'same-origin', headers: { ...auth, ...headers } };
  if (formData) init.body = formData;
  else if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  const res = await fetch(url, init);
  noteDeprecation(res);
  if (!res.ok) {
    let msg = res.statusText; let code = 'error'; let hint = ''; let detail = null;
    try {
      const j = await res.json();
      detail = j.detail ?? null;
      const d = (detail && typeof detail === 'object') ? detail : null;
      msg = (j.error && j.error.message) || (d ? (d.message || JSON.stringify(d)) : detail) || msg;
      code = (j.error && j.error.code) || code;
      hint = (j.error && j.error.hint) || '';
    } catch { /* 保底 statusText */ }
    if (res.status === 401 && MODE === 'live') {
      code = 'auth_required';
      hint = hint || '需要 token：请从终端启动日志里打印的完整地址（带 ?token=）进入一次，之后浏览器会记住会话。';
    }
    throw new ApiError(String(msg || `HTTP ${res.status}`), res.status, code, hint, detail);
  }
  return { body: await res.json().catch(() => ({})), etag: res.headers.get('ETag'), headers: res.headers };
}
const call = (m, p, b, h) => raw(m, p, { body: b, headers: h });

// -------------------------------------------------------------- 模式探测 + 会话引导
let _init = null;
async function probe() {
  if (forcedStatic) return 'static';
  const capsUrl = `${root}v1/capabilities`;
  let res;
  try {
    // 地址栏带 token：用它探测一次。服务端按 header→bearer→cookie→query 取第一个**有效**凭据——
    // 有效 Cookie 在场时不会回 Deprecation；只靠 query 通过才回，据此判断要不要引导 Cookie 并提示。
    res = await fetch(urlToken ? `${capsUrl}?token=${encodeURIComponent(urlToken)}` : capsUrl,
      { credentials: 'same-origin' });
  } catch (err) { probeError = err; return 'live'; }
  if (res.status === 404) return 'legacy';
  if (res.status === 401) { authRequired = true; return 'live'; }
  if (!res.ok) { probeError = new Error(`能力接口 ${res.status}`); return 'live'; }
  try { caps = await res.json(); } catch { caps = null; }
  const viaQuery = res.headers.get('X-Auth-Deprecated') === 'query-token' || res.headers.get('Deprecation') === 'true';
  if (urlToken && viaQuery) {
    // Cookie 不在或已失效：请工作台首页（/?token=）下发会话 Cookie（这是引导，不算弃用），并提示
    try {
      const r = await fetch(`${root}?token=${encodeURIComponent(urlToken)}`, { credentials: 'same-origin' });
      await r.text();                                   // 读完响应体（Set-Cookie 已生效；不留悬空的请求）
    } catch { /* */ }
    noteDeprecation(res);
  }
  return 'live';
}

function tidyUrlToken() {
  if (!urlToken) return;
  try {
    const u = new URL(location.href); u.searchParams.delete('token');
    history.replaceState(history.state, '', u.pathname + u.search + u.hash);
  } catch { /* */ }
}

function legacyTokenInit() {
  if (urlToken) { try { sessionStorage.setItem('studio-token', urlToken); } catch { /* 隐私模式 */ } }
  token = urlToken || (() => { try { return sessionStorage.getItem('studio-token') || ''; } catch { return ''; } })();
}

// -------------------------------------------------------------- 归一化（live 与 static 共用：两者同源于后端形状）
function capsLlm(c) {
  const ai = c?.ai || {};
  return { provider: ai.provider || 'none', provider_label: ai.provider_label || '', configured: !!ai.configured,
    outbound_fields: ai.outbound_fields || [], outbound_note: ai.outbound_note || '',
    dwg_available: c?.export ? c.export.dwg_available !== false : true };
}
// /v1/projects 的条目只有 id；界面统一用 project_id
function normSummary(p) { return p ? { ...p, project_id: p.project_id || p.id } : p; }
function normStructure(s) {
  if (!s) return s;
  return { ...s, groups: (s.groups || []).map((g, i) => ({ ...g, id: g.id || `g${i + 1}` })) };
}
// 候选（openapi Candidate）：fig_id 与 id 同值；members 只有 {part}
function normCand(c) { return { ...c, id: c.id || c.fig_id, fig_id: c.fig_id || c.id }; }
// 候选分图作业（kind=suggest 的 Job）→ 界面形状
export function normSuggestJob(job) {
  if (!job) return null;
  const r = job.result || {};
  return { job_id: job.id || job.job_id || null, state: job.state || null,
    running: ['queued', 'running'].includes(job.state),
    candidates: (r.candidates || []).map(normCand), empty_reason: r.empty_reason || null, guide: r.guide || null,
    summary: r.summary || null, warnings: r.warnings || [], plan_etag: r.plan_etag || null,
    error: job.error || null };
}

// 渲染作业状态归一化（openapi RenderJobStatus）：{id, state, running, ok, error, result, diag}。
// 来源可以是 /v1/jobs/{id}（Job）或子应用 /api/render/status 的 job；两者都归一成同一形状。
export function normJob(j) {
  if (!j) return null;
  const state = j.state || null;
  if (!state && !j.id && !j.job_id && !j.running) return null;      // 子应用「从没渲染过」的空作业
  return {
    id: j.id || j.job_id || null, state,
    running: j.running ?? ['queued', 'running'].includes(state),
    ok: j.ok ?? (state ? state === 'succeeded' : null),
    error: j.error || null, result: j.result || null, diag: j.diag || null,
  };
}

// 已发布结果的版本证据：live 的封口结果自带 render_job_id（= 版本 rid），但输入快照 plan_etag 记在
// 该渲染作业上——从 GET /v1/jobs/{rid} 的 inputs.plan_etag 取（后端证据）；取不到就不补（导出保持锁定）。
async function attachEvidence(result) {
  if (!result || MODE !== 'live') return result;
  if (result.inputs?.plan_etag || result.plan_etag) return result;
  const rid = result.render_job_id || result.version;
  if (!rid) return result;
  try {
    const job = (await call('GET', `${root}v1/jobs/${encodeURIComponent(rid)}`)).body;
    if (job?.kind === 'render' && job.state === 'succeeded' && job.inputs?.plan_etag) {
      return { ...result, inputs: { ...(result.inputs || {}), plan_etag: job.inputs.plan_etag } };
    }
  } catch { /* 作业记录不在：不补证据 */ }
  return result;
}

// -------------------------------------------------------------- static 适配（manifest = 后端响应快照）
let _manifest = null;
async function manifest() {
  if (_manifest) return _manifest;
  const res = await fetch(`${base}dev/manifest.json`);
  if (!res.ok) throw new ApiError('静态 manifest 缺失', 404, 'no_manifest');
  _manifest = await res.json();
  api._manifestSync = _manifest;
  caps = _manifest.capabilities || caps;
  return _manifest;
}
const staticProject = async (id) => {
  const p = (await manifest()).projects.find((x) => x.id === id);
  if (!p) throw new ApiError('静态站没有这个工程', 404, 'not_found');
  return p;
};

// -------------------------------------------------------------- legacy 适配（旧 /api/*）
let _legacyState = null;             // 缓存一次 api/state（studio）
async function legacyState(force) {
  if (_legacyState && !force) return _legacyState;
  _legacyState = (await call('GET', `${base}api/state`)).body;
  return _legacyState;
}
function legacyDetail(s) {
  return {
    project_id: s._pid || '', title: (s.step || '').split('/').pop() || '当前工程',
    step_name: (s.step || '').split('/').pop() || '', status: 'ready', example: false,
    assembly: s.assembly, structure: normStructure(s.structure), render: s.render,
    candidates: [], candidates_unsupported: true,    // 旧后端无候选分图
    plan: s.plan, plan_etag: null, llm: legacyLlm(s.llm),
    rendering: !!s.rendering,
    version: s.version ?? null,
    render_job: s.render_job ? normJob(s.render_job) : null,
    figures: (s.render?.figures || []).length,
    figures_pass: (s.render?.figures || []).filter((f) => f.pass).length,
    home_url: s.home_url, flowcharts: s.flowcharts || [],
    validate: s.validate,
  };
}
function legacyLlm(l) {
  l = l || {};
  const configured = l.provider && l.provider !== 'none';
  return { provider: l.provider, provider_label: l.provider_label || (configured ? 'AI' : 'AI 未配置'),
    configured, dwg_available: true, outbound_fields: [], outbound_note: '' };
}
const LEGACY_EXPORT_OFF = () => new ApiError('旧版后端兼容模式不支持导出——请升级到新版后端', 0,
  'legacy_export_disabled', '升级到含 /v1 接口的新版工作台后，导出会绑定版本化的出图结果。');

// 组装工程详情（live 与 static 共用）：摘要（/v1/projects/{id}）+ 子应用状态（/p/{pid}/api/state）+ 候选作业
function composeDetail(id, summary, st, candJob) {
  const s = normSummary(summary) || { project_id: id };
  const cand = candJob ? normSuggestJob(candJob) : null;
  return {
    project_id: s.project_id || id, title: s.title || id, description: s.description || '',
    step_name: s.step_name || (st?.step || '').split('/').pop() || '', status: s.status || 'ready',
    example: !!s.example, plan_etag: s.plan_etag || null,
    assembly: st?.assembly || null, structure: normStructure(st?.structure || null),
    render: st?.render || null, version: st?.version ?? null,
    render_job: normJob(st?.render_job),
    validate: st?.validate || { ok: true, issues: [] },
    flowcharts: st?.flowcharts || [],
    candidates: cand ? cand.candidates : [], candidates_job: cand,
    candidates_summary: s.candidates || null,
    llm: capsLlm(caps), home_url: root,
  };
}

// -------------------------------------------------------------- 统一接口
export const api = {
  get mode() { return MODE; },
  get isLegacy() { return MODE === 'legacy'; },
  get readOnly() { return readOnly; },
  get caps() { return caps; },
  get authRequired() { return authRequired; },
  get probeError() { return probeError; },
  get deprecatedNotice() { return deprecatedNotice; },
  root, pagePid,
  onDeprecation(fn) { deprecationListeners.push(fn); if (deprecatedNotice) fn(deprecatedNotice); },
  setProject(pid) { if (pid) currentPid = pid; },

  // 必须在任何渲染前 await：判定模式、引导会话、清理地址栏 token
  init() {
    if (!_init) {
      _init = (async () => {
        MODE = await probe();
        isLegacy = MODE === 'legacy';
        if (MODE === 'legacy') legacyTokenInit();
        else if (MODE === 'live') tidyUrlToken();
        if (MODE === 'static') { try { await manifest(); } catch { /* 页面自己报错 */ } }
        return MODE;
      })();
    }
    return _init;
  },
  async refreshCaps() {
    if (MODE !== 'live') return caps;
    caps = (await call('GET', `${root}v1/capabilities`)).body;
    return caps;
  },

  // 首页工程列表（WP3b #3：/v1/projects 的条目是摘要，AI 状态来自能力接口）
  async listProjects(scene) {
    if (MODE === 'static') {
      const m = await manifest();
      return { projects: m.projects.map((p) => normSummary(p.summary)), llm: capsLlm(caps), data_dir: '', static: true };
    }
    if (MODE === 'legacy') {
      const d = (await call('GET', `${base}api/wb/state`)).body;
      return {
        projects: (d.projects || []).map((p) => ({ ...p, project_id: p.id || p.project_id })),
        llm: legacyLlm(d.llm), data_dir: d.data_dir, legacy: true,
      };
    }
    if (authRequired) throw new ApiError('需要 token', 401, 'auth_required',
      '请从终端启动日志里打印的完整地址（带 ?token=）进入一次，之后浏览器会记住会话。');
    if (probeError && !caps) throw new ApiError(`连接不上工作台服务（${probeError.message || probeError}）`, 0, 'network');
    const q = scene ? `?scene=${encodeURIComponent(scene)}` : '';      // ?scene= 只对 mock 有意义，真实后端忽略
    const d = (await call('GET', `${root}v1/projects${q}`)).body;
    return { ...d, projects: (d.projects || []).map(normSummary), llm: capsLlm(caps), data_dir: d.data_dir || '' };
  },

  // 工程摘要（只读 GET /v1/projects/{id}；导入进度里看后台消息用）
  async getSummary(id) {
    if (MODE !== 'live') return null;
    return normSummary((await call('GET', `${root}v1/projects/${encodeURIComponent(id)}`)).body);
  },

  // 工程详情（WP3b #3）：摘要 + 子应用状态（已发布 render / 版本 / 本次作业 / 装配体 / 结构）+ 候选作业
  async getProject(id) {
    currentPid = id || currentPid;
    if (MODE === 'static') {
      const p = await staticProject(id);
      return composeDetail(id, p.project || p.summary, p.state, p.candidates);
    }
    if (MODE === 'legacy') { const s = await legacyState(true); s._pid = id; return legacyDetail(s); }
    const [summary, st] = await Promise.all([
      call('GET', `${root}v1/projects/${encodeURIComponent(id)}`).then((r) => r.body),
      call('GET', `${sub(id)}api/state`).then((r) => r.body),
    ]);
    let candJob = null;
    if (summary?.candidates?.job_id) {
      try { candJob = (await call('GET', `${root}v1/projects/${encodeURIComponent(id)}/plan/candidates`)).body; }
      catch (err) { if (err.status !== 404) throw err; }
    }
    if (st?.render) st.render = await attachEvidence(st.render);
    return composeDetail(id, summary, st, candJob);
  },

  async getPlan(id) {
    if (MODE === 'static') {
      const p = await staticProject(id);
      return { plan: p.plan, etag: p.plan_etag || p.project?.plan_etag || null };
    }
    if (MODE === 'legacy') { const s = await legacyState(); return { plan: s.plan, etag: null }; }
    const { body, etag } = await call('GET', `${root}v1/projects/${encodeURIComponent(id)}/plan`);
    return { plan: body, etag };                     // 响应体就是 plan 本身，版本在 ETag 头
  },

  async putPlan(id, plan, etag) {
    if (MODE === 'static') throw new ApiError('静态演示站只读，改动不保存', 0, 'read_only');
    if (MODE === 'legacy') {
      const res = (await call('PUT', `${base}api/plan`, { plan })).body;
      if (_legacyState) _legacyState.plan = plan;
      return { validate: res.validate, etag: null };
    }
    // 请求体就是 plan 本身，If-Match 带当前 ETag；200 {ok} + 新 ETag / 409
    const { body, etag: newEtag } = await call('PUT', `${root}v1/projects/${encodeURIComponent(id)}/plan`, plan,
      etag ? { 'If-Match': etag } : {});
    return { ...body, etag: newEtag };
  },

  // 校验结果：PUT /v1 只回 {ok}；live 从子应用 api/state 的 validate 取（WP1 CONTRACT_NOTES 待联调 #5）
  async getValidate(id) {
    if (MODE === 'static' || MODE === 'legacy') return null;
    return (await call('GET', `${sub(id)}api/state`)).body?.validate || null;
  },

  // ---- 候选分图（F2-lite，WP3b #5）----
  async suggest(id, labelStandardParts = false) {
    if (MODE === 'legacy') throw new ApiError('候选分图需要新版后端', 0, 'unsupported',
      '旧后端无候选分图：请在 3D 里框选零件后到「图纸」页新建图。');
    return (await call('POST', `${root}v1/projects/${encodeURIComponent(id)}/plan/suggest`,
      { label_standard_parts: labelStandardParts })).body;
  },
  async getCandidates(id) {
    if (MODE === 'static') return normSuggestJob((await staticProject(id)).candidates);
    if (MODE === 'legacy') return null;
    try { return normSuggestJob((await call('GET', `${root}v1/projects/${encodeURIComponent(id)}/plan/candidates`)).body); }
    catch (err) { if (err.status === 404) return null; throw err; }
  },
  // 接受候选：If-Match 带当前计划 ETag；200 新 ETag；422 detail.unresolved；409 计划 / 装配体 / 作业已变
  async acceptCandidates(id, body, etag) {
    if (MODE !== 'live') throw new ApiError('候选分图需要新版后端', 0, 'unsupported');
    const { body: res, etag: newEtag } = await call('POST',
      `${root}v1/projects/${encodeURIComponent(id)}/plan/candidates/accept`, body, etag ? { 'If-Match': etag } : {});
    return { ...res, etag: newEtag || res.plan_etag || null };
  },

  async render(id, opts = {}) {
    if (MODE === 'legacy') {
      const q = opts.fail ? `?fail=${encodeURIComponent(opts.fail)}` : '';   // 开发用失败场景，后端忽略未知参数
      await call('POST', `${base}api/render${q}`);
      return { job_id: 'legacy:render' };
    }
    const q = opts.fail ? `?fail=${encodeURIComponent(opts.fail)}` : '';    // 只有 mock 认，真实后端忽略
    return (await call('POST', `${root}v1/projects/${encodeURIComponent(id)}/render${q}`, { plan_etag: opts.etag })).body;
  },

  // 取权威的已发布结果（终态后调用）：子应用 render/status 的 result（预览 URL 带 ?v=<rid>）
  async getRenderResult(id) {
    if (MODE === 'legacy') { return (await call('GET', `${base}api/render/status`)).body.result; }
    return (await this.getRenderStatus(id)).result;
  },

  // 渲染状态（openapi RenderStatus）：result = 已发布版本，version = 其 ID，job = 本次（最近一次）渲染作业。
  // 失败时 result 仍是上一成功版本，本次失败只在 job 里。live 读子应用 /p/{pid}/api/render/status。
  async getRenderStatus(id) {
    if (MODE === 'static') {
      const p = await staticProject(id);
      return { running: false, result: p.state?.render || null, version: p.state?.version || null, job: null };
    }
    const url = MODE === 'legacy' ? `${base}api/render/status` : `${sub(id)}api/render/status`;
    const st = (await call('GET', url)).body;
    const result = MODE === 'live' ? await attachEvidence(st.result || null) : (st.result || null);
    return { running: !!st.running, result, version: st.version ?? result?.version ?? null, job: normJob(st.job) };
  },

  // 导出（live）：请求体带 variant / formats / dpi / sheet_preset / render_job_id / plan_etag；后端把 plan_etag
  // 与该渲染的冻结输入原子比对，不符 409。legacy 兼容模式不提供导出。
  async export(id, opts = {}) {
    if (MODE === 'legacy') throw LEGACY_EXPORT_OFF();
    return (await call('POST', `${root}v1/projects/${encodeURIComponent(id)}/export`, opts)).body;
  },
  async getExport(exportId) {
    if (MODE === 'legacy') throw LEGACY_EXPORT_OFF();
    return (await call('GET', `${root}v1/exports/${encodeURIComponent(exportId)}`)).body;
  },

  async getJob(jobId) {
    if (MODE === 'legacy') {
      if (jobId === 'legacy:render') {           // 新版子应用给出本次作业；旧后端没有则按 running/result 判
        const st = (await call('GET', `${base}api/render/status`)).body;
        const job = normJob(st.job);
        if (st.running || job?.running) return { state: 'running', result: null };
        return { state: job?.state || 'succeeded', result: job?.result || st.result, error: job?.error || null };
      }
      return { state: 'succeeded' };
    }
    return (await call('GET', `${root}v1/jobs/${encodeURIComponent(jobId)}`)).body;
  },
  async cancelJob(jobId) {
    if (MODE === 'legacy') return { ok: true };
    return (await call('DELETE', `${root}v1/jobs/${encodeURIComponent(jobId)}`)).body;
  },

  async fromExample(ex) {
    if (MODE === 'legacy') throw new ApiError('旧后端从首页「新建」载入示例', 0, 'unsupported');
    return (await call('POST', `${root}v1/projects/from-example`, { example: ex })).body;
  },

  // 上传：multipart 字段 step + title（openapi）；失败不吞文件
  async createProject({ file, title, ai } = {}, failScene) {
    if (MODE === 'legacy') {
      if (file) {
        const url = `${base}api/wb/projects?name=${encodeURIComponent(file.name)}`
          + `&title=${encodeURIComponent(title || file.name.replace(/\.(stp|step)$/i, ''))}`;
        const r = (await raw('POST', url, { formData: file })).body;   // 旧接口收原始文件体
        return { project_id: r.id || r.project_id, job_id: 'legacy:import', legacy_ready: r };
      }
      throw new ApiError('请选择 STEP 文件', 0, 'no_file');
    }
    const fd = new FormData();
    if (file) fd.append('step', file, file.name);
    if (title) fd.append('title', title);
    const q = failScene ? `?fail=${encodeURIComponent(failScene)}` : '';   // 只有 mock 认
    return (await raw('POST', `${root}v1/projects${q}`, { formData: fd })).body;
  },

  // 删除工程：工作台的 DELETE /api/wb/projects/{id}（示例工程不可删，后端 409）
  async deleteProject(id) {
    if (MODE === 'static') throw new ApiError('静态演示站只读', 0, 'read_only');
    return (await call('DELETE', `${MODE === 'legacy' ? base : root}api/wb/projects/${encodeURIComponent(id)}`)).body;
  },

  // AI 起名接力（F9，WP3b #9）：POST /v1/projects/{id}/ai/draft-terms {selectors, apply, plan_etag}。
  //   apply=false（缺省语义）只给建议、不写 plan；apply=true 经服务端「只填空白」写入并回新 ETag（If-Match 不符 409）。
  //   没有 AI 通道：200 status=ai_not_configured（中性）；通道失败 502 error.code=ai_call_failed。
  async draftTerms(id, { selectors, apply = false, etag } = {}) {
    if (MODE === 'legacy') {
      const r = (await call('POST', `${base}api/llm/draft-terms`, { apply: true })).body;
      if (_legacyState && r.plan) _legacyState.plan = r.plan;
      return { status: 'ok', plan: r.plan, suggestions: r.suggestions || [],
        filled: (r.applied?.filled?.length || 0) + (r.applied?.added?.length || 0),
        kept_human: r.applied?.kept_human?.length || 0, server_applied: true, legacy: true };
    }
    const body = { apply: !!apply };
    if (Array.isArray(selectors)) body.selectors = selectors;
    if (apply && etag) body.plan_etag = etag;
    const { body: r, etag: newEtag } = await call('POST', `${root}v1/projects/${encodeURIComponent(id)}/ai/draft-terms`,
      body, (apply && etag) ? { 'If-Match': etag } : {});
    return { ...r, etag: newEtag || r.plan_etag || null };
  },

  // ---- 结构识别（AI，外发完整零件清单）与结构组改名：live 与 legacy 都走子应用 api/structure* ----
  async analyzeStructure(id) {
    if (MODE === 'static') throw new ApiError('静态演示站只读', 0, 'read_only');
    return (await call('POST', `${MODE === 'legacy' ? base : sub(id)}api/structure/analyze`)).body;
  },
  async getStructure(id) {
    if (MODE === 'static') return null;
    const r = (await call('GET', `${MODE === 'legacy' ? base : sub(id)}api/structure`)).body;
    return { ...r, structure: normStructure(r.structure) };
  },
  async renameGroups(id, groups) {
    if (MODE === 'static') throw new ApiError('静态演示站只读，改名不保存', 0, 'read_only');
    const r = (await call('PUT', `${MODE === 'legacy' ? base : sub(id)}api/structure`, { groups })).body;
    return { ...r, structure: normStructure(r.structure) };
  },

  async llmStatus() {
    if (MODE === 'static') { await manifest(); return capsLlm(caps); }
    if (MODE === 'legacy') { const s = await legacyState(); return legacyLlm(s.llm); }
    if (!caps) { try { await this.refreshCaps(); } catch { /* */ } }
    return capsLlm(caps);
  },

  // ---- AI 通道设置：工作台的 /api/wb/settings（真实后端有；mock 无 → 演示，不落盘）----
  settingsSupported() { return MODE === 'legacy' || settingsReal; },
  async getSettings() {
    if (MODE === 'legacy') return (await call('GET', `${base}api/wb/settings`)).body;
    if (MODE === 'live') {
      try { const s = (await call('GET', `${root}api/wb/settings`)).body; settingsReal = true; return s; }
      catch (err) { if (err.status !== 404) throw err; settingsReal = false; }
    }
    const llm = await this.llmStatus();
    return { llm: { configured_provider: 'auto', api_key_set: !!llm.configured, api_key_hint: '',
      base_url: '', model: '', provider_label: llm.provider_label, key_source: '演示' }, demo: true };
  },
  async saveSettings(body) {
    if (MODE === 'legacy') return (await call('PUT', `${base}api/wb/settings`, body)).body;
    if (MODE === 'live' && settingsReal) {
      const r = (await call('PUT', `${root}api/wb/settings`, body)).body;
      try { await this.refreshCaps(); } catch { /* */ }
      return r;
    }
    throw new ApiError('mock / 静态模式不保存设置（仅演示）', 0, 'demo');
  },
  async testConnection() {
    if (MODE === 'legacy') return (await call('POST', `${base}api/wb/settings/test`)).body;
    if (MODE === 'live' && settingsReal) return (await call('POST', `${root}api/wb/settings/test`)).body;
    throw new ApiError('mock / 静态模式无法测试真实连接', 0, 'demo');
  },

  // ---- 流程图（方法专利）：子应用 /api/flowcharts*（真实后端有；mock / 静态站无 → 优雅降级）----
  _flowOk: null,
  flowchartsSupported() { return MODE === 'legacy' || this._flowOk === true; },
  async probeFlowcharts(id) {
    if (MODE === 'legacy') return true;
    if (MODE !== 'live') { this._flowOk = false; return false; }
    try { await call('GET', `${sub(id)}api/flowcharts`); this._flowOk = true; } catch { this._flowOk = false; }
    return this._flowOk;
  },
  _flowBase() { return MODE === 'legacy' ? base : sub(); },
  async flowcharts() {
    if (!this.flowchartsSupported()) return { flowcharts: [] };
    return (await call('GET', `${this._flowBase()}api/flowcharts`)).body;
  },
  async newFlowchart(spec) { return (await call('POST', `${this._flowBase()}api/flowcharts`, spec ? { spec } : {})).body; },
  async saveFlowchart(id, spec) { return (await call('PUT', `${this._flowBase()}api/flowcharts/${id}`, { spec })).body; },
  async renderFlowchart(id) { return (await call('POST', `${this._flowBase()}api/flowcharts/${id}/render`)).body; },
  async deleteFlowchart(id) { return (await call('DELETE', `${this._flowBase()}api/flowcharts/${id}`)).body; },
  async aiFlowchart(text, title, id) { return (await call('POST', `${this._flowBase()}api/flowcharts-ai`, { text, title, id })).body; },

  // ---- 资源 URL ----
  modelUrl(id) {
    if (MODE === 'legacy') return token ? `${base}api/model.glb?token=${encodeURIComponent(token)}` : `${base}api/model.glb`;
    if (MODE === 'static') return `${base}${rel((this._m(id) || {}).model_url || `v1/projects/${id}/model.glb`)}`;
    return `${sub(id)}api/model.glb`;
  },
  // 后端给的资源地址 → 浏览器可取的地址：
  //   http(s) 绝对地址原样；/api/* 属子应用（live 拼 sub(pid)，legacy 拼页面目录）；其余 /x 属工作台根；
  //   静态站 manifest 里的相对路径相对页面目录。live 不再往 URL 里塞 token（只用 Cookie）。
  previewUrl(p, pid) {
    const u = String(p || '');
    if (/^https?:\/\//i.test(u)) return u;
    if (MODE === 'static') return `${base}${rel(u)}`;
    if (MODE === 'legacy') {
      const r = `${base}${rel(u)}`;
      return token ? `${r}${r.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : r;
    }
    if (u.startsWith('/api/')) return `${sub(pid)}${rel(u)}`;
    return `${root}${rel(u)}`;
  },
  _manifestSync: null,
  _m(id) { return (this._manifestSync?.projects || []).find((x) => x.id === id); },

  // ---- 作业事件（WP3b #4）：live 走 SSE；legacy 轮询代替；static 立即完成 ----
  // 帧：id / event / data，data = {stage, progress, data}——stage 的 data 是中文标签、log 的是一行文字、
  // done 的是作业 result、error 的是 {code, message, hint} 或字符串（取消）。
  // 断线：EventSource 处于 CONNECTING 时浏览器自动带 Last-Event-ID 重连；已 CLOSED（例如服务端回了非 200）
  // 时由这里按 ?last_event_id= 手动重连（指数退避，上限 12 次），每次断线都通知上层用 GET /v1/jobs/{id} 校准。
  jobEvents(jobId, handlers = {}) {
    if (MODE === 'static') { setTimeout(() => handlers.done && handlers.done({}), 0); return { close() {} }; }
    if (MODE === 'legacy') return legacyPoll(jobId, handlers);
    let es = null; let lastId = 0; let closed = false; let retries = 0; let timer = null;
    const close = () => { closed = true; clearTimeout(timer); try { es?.close(); } catch { /* */ } };
    const unwrap = (e) => {
      const n = Number(e.lastEventId);
      if (Number.isFinite(n) && n > lastId) lastId = n;
      try { return JSON.parse(e.data); } catch { return {}; }
    };
    const open = () => {
      if (closed) return;
      const url = `${root}v1/jobs/${encodeURIComponent(jobId)}/events${lastId ? `?last_event_id=${lastId}` : ''}`;
      es = new EventSource(url, { withCredentials: true });
      es.onopen = () => { retries = 0; };
      const stageLike = (e) => {
        const f = unwrap(e);
        retries = 0;
        handlers.stage && handlers.stage({ stage: f.stage, progress: f.progress,
          label: typeof f.data === 'string' ? f.data : '', lastEventId: lastId });
      };
      es.addEventListener('stage', stageLike);
      es.addEventListener('progress', stageLike);
      es.addEventListener('log', (e) => {
        const f = unwrap(e);
        handlers.log && handlers.log({ line: typeof f.data === 'string' ? f.data : JSON.stringify(f.data ?? ''), progress: f.progress });
      });
      es.addEventListener('done', (e) => {
        const f = unwrap(e); close();
        handlers.done && handlers.done({ result: f.data ?? null, stage: f.stage }, lastId);
      });
      // 'error' 既可能是业务失败（带 data），也可能是传输错误（无 data）。只有带 data 才当业务失败并关流。
      es.addEventListener('error', (e) => {
        if (e.data == null) {
          if (closed) return;
          handlers.disconnect && handlers.disconnect();
          if (es.readyState === EventSource.CLOSED && retries < 12) {
            retries += 1;
            clearTimeout(timer);
            timer = setTimeout(open, Math.min(8000, 400 * (2 ** (retries - 1))));
          }
          return;
        }
        const f = unwrap(e); close();
        const err = f.data;
        const o = (err && typeof err === 'object') ? err : { message: String(err || '作业失败') };
        handlers.fail && handlers.fail({ code: o.code || (f.stage === 'cancelled' ? 'cancelled' : ''), message: o.message,
          hint: o.hint || '', stage: f.stage }, lastId);
      });
    };
    open();
    return { close, get lastEventId() { return lastId; } };
  },
};

// legacy：轮询 render/status 模拟进度事件。新版子应用的 job（本次作业）失败时报失败，
// 不把仍是上一成功版本的 result 当作本次结果；旧后端没有 job，按 running/result 判定。
function legacyPoll(jobId, handlers) {
  if (jobId !== 'legacy:render') { setTimeout(() => handlers.done && handlers.done({}), 0); return { close() {} }; }
  let stopped = false; let p = 0.1;
  const tick = async () => {
    if (stopped) return;
    try {
      const st = (await call('GET', `${base}api/render/status`)).body;
      const job = normJob(st.job);
      if (st.running || job?.running) {
        p = Math.min(0.9, p + 0.12);
        handlers.stage && handlers.stage({ stage: '出图中', progress: p });
        setTimeout(tick, 900);
      } else if (job && job.state && job.state !== 'succeeded') {
        handlers.fail && handlers.fail({ ...(job.error || {}), message: job.error?.message || '本次出图未成功', job });
      } else {
        handlers.done && handlers.done({ result: job?.result || st.result });
      }
    } catch (e) {
      handlers.disconnect && handlers.disconnect();   // 轮询出错 → 让上层校准，不当作业务失败
      setTimeout(tick, 1500);
    }
  };
  setTimeout(tick, 400);
  return { close() { stopped = true; } };
}

export { ApiError };
