// 数据适配器：三种后端形态走同一组方法。
//   live   —— 新版工作台（§6 /v1/* 契约，带作业 + SSE + 候选分图），对 mock 开发。
//   legacy —— **旧版后端兼容模式（开发用，不随产品发布）**：workbench.py 在 / 挂首页、plan_studio 在
//             /p/{id}/ 用同步 /api/*，没有候选分图 / 作业 / SSE / 输入冻结——只保留浏览、渲染、术语编辑、
//             预览；**导出一律锁定**（架构裁决，修复轮 7：旧接口无法证明打包内容对应哪一版计划）。
//   static —— 读版本化 manifest.json，供 GitHub Pages 等静态演示站只读运行（S2-16）。
// 选择：?static / file:// → static；页面路径以 home.html|index.html 结尾（mock / 开发入口）→ live；
//       否则（正式入口 / 与 /p/{id}/）→ legacy。无 token 不 fatal。

const params = new URLSearchParams(location.search);
const path = location.pathname;
const flatEntry = /(^|\/)(home|index)\.html$/.test(path);   // mock / 开发 / Pages 入口
// 静态发布：build_static.py 会注入 <meta name="inkline-mode" content="static">，
// 这样部署到任意静态目录/子路径（无 ?static）也能进只读静态模式。
const metaStatic = !!document.querySelector('meta[name="inkline-mode"][content="static"]');
const forcedStatic = params.has('static') || location.protocol === 'file:' || metaStatic;

const fromUrl = params.get('token');
if (fromUrl) { try { sessionStorage.setItem('studio-token', fromUrl); } catch { /* 隐私模式 */ } }
export const token = fromUrl || (() => {
  try { return sessionStorage.getItem('studio-token') || ''; } catch { return ''; }
})();

export const MODE = forcedStatic ? 'static' : (flatEntry ? 'live' : 'legacy');
export const readOnly = MODE === 'static';
export const isLegacy = MODE === 'legacy';
// 相对路径根：静态站 Pages 子路径与 /p/{id}/ 都靠它拿对 URL。
const base = new URL('.', location.href).pathname;

function rel(p) { return (p || '').replace(/^\//, ''); }

class ApiError extends Error {
  constructor(message, status, code, hint) {
    super(message); this.status = status; this.code = code; this.hint = hint;
  }
}

async function raw(method, url, { body, headers = {}, formData } = {}) {
  // 统一写保护：静态只读模式绝不发任何写请求（POST/PUT/DELETE/PATCH），从请求层兜底拦截。
  if (readOnly && !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())) {
    throw new ApiError('静态演示站只读，不能执行写操作', 0, 'read_only');
  }
  const init = { method, headers: { ...(token ? { 'X-Studio-Token': token, Authorization: `Bearer ${token}` } : {}), ...headers } };
  if (formData) init.body = formData;
  else if (body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
  const res = await fetch(url, init);
  if (!res.ok) {
    let msg = res.statusText; let code = 'error'; let hint = '';
    try {
      const j = await res.json();
      msg = (j.error && j.error.message) || j.detail || msg;
      code = (j.error && j.error.code) || code;
      hint = (j.error && j.error.hint) || '';
    } catch { /* 保底 statusText */ }
    throw new ApiError(msg, res.status, code, hint);
  }
  return { body: await res.json().catch(() => ({})), etag: res.headers.get('ETag') };
}
const call = (m, p, b, h) => raw(m, p, { body: b, headers: h });

// -------------------------------------------------------------- static 适配
let _manifest = null;
async function manifest() {
  if (_manifest) return _manifest;
  const res = await fetch(`${base}dev/manifest.json`);
  if (!res.ok) throw new ApiError('静态 manifest 缺失', 404, 'no_manifest');
  _manifest = await res.json();
  api._manifestSync = _manifest;
  return _manifest;
}

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
    assembly: s.assembly, structure: s.structure, render: s.render,
    candidates: [], candidates_unsupported: true,    // 旧后端无候选分图
    plan: s.plan, plan_etag: null, llm: legacyLlm(s.llm),
    rendering: !!s.rendering,
    version: s.version ?? null,                      // 新版子应用：已发布版本 ID（旧后端无）
    render_job: s.render_job ? normJob(s.render_job) : null,   // 新版子应用：本次渲染作业（旧后端无）
    figures: (s.render?.figures || []).length,
    figures_pass: (s.render?.figures || []).filter((f) => f.pass).length,
    home_url: s.home_url, flowcharts: s.flowcharts || [],
    model_url: `${base}api/model.glb`,
  };
}
function legacyLlm(l) {
  l = l || {};
  const configured = l.provider && l.provider !== 'none';
  return { provider: l.provider, provider_label: l.provider_label || (configured ? 'AI' : 'AI 未配置'),
    configured, dwg_available: true };
}

// 渲染作业状态归一化（openapi RenderJobStatus）：{id, state, running, ok, error, result, diag}。
// 来源可以是 /v1/jobs/{id}（Job）或子应用 /api/render/status 的 job；两者都归一成同一形状。
export function normJob(j) {
  if (!j) return null;
  const state = j.state || null;
  return {
    id: j.id || j.job_id || null, state,
    running: j.running ?? ['queued', 'running'].includes(state),
    ok: j.ok ?? (state ? state === 'succeeded' : null),
    error: j.error || null, result: j.result || null, diag: j.diag || null,
  };
}
const LEGACY_EXPORT_OFF = () => new ApiError('旧版后端兼容模式不支持导出——请升级到新版后端', 0,
  'legacy_export_disabled', '升级到含 /v1 接口的新版工作台后，导出会绑定版本化的出图结果。');

// -------------------------------------------------------------- 统一接口
export const api = {
  mode: MODE, readOnly, isLegacy,

  async listProjects(scene) {
    if (MODE === 'static') {
      const m = await manifest();
      return { projects: m.projects.map((p) => p.summary), llm: m.llm, data_dir: m.data_dir, static: true };
    }
    if (MODE === 'legacy') {
      const d = (await call('GET', `${base}api/wb/state`)).body;
      return {
        projects: (d.projects || []).map((p) => ({ ...p, project_id: p.id })),
        llm: legacyLlm(d.llm), data_dir: d.data_dir, legacy: true,
      };
    }
    const q = scene ? `?scene=${encodeURIComponent(scene)}` : '';
    return (await call('GET', `${base}v1/projects${q}`)).body;
  },

  async getProject(id) {
    if (MODE === 'static') {
      const p = (await manifest()).projects.find((x) => x.id === id);
      if (!p) throw new ApiError('静态站没有这个工程', 404, 'not_found');
      return p.detail;
    }
    if (MODE === 'legacy') { const s = await legacyState(true); s._pid = id; return legacyDetail(s); }
    return (await call('GET', `${base}v1/projects/${id}`)).body;
  },

  async getPlan(id) {
    if (MODE === 'static') {
      const p = (await manifest()).projects.find((x) => x.id === id);
      return { plan: p?.detail?.plan, etag: p?.detail?.plan_etag };
    }
    if (MODE === 'legacy') { const s = await legacyState(); return { plan: s.plan, etag: null }; }
    const { body, etag } = await call('GET', `${base}v1/projects/${id}/plan`);
    return { plan: body, etag };                     // WP1：响应体就是 plan 本身，版本在 ETag 头
  },

  async putPlan(id, plan, etag) {
    if (MODE === 'static') throw new ApiError('静态演示站只读，改动不保存', 0, 'read_only');
    if (MODE === 'legacy') {
      const res = (await call('PUT', `${base}api/plan`, { plan })).body;
      if (_legacyState) _legacyState.plan = plan;
      return { validate: res.validate, etag: null };
    }
    // WP1：请求体就是 plan 本身（不是 {plan}），If-Match 带当前 ETag；200 {ok} + 新 ETag / 409
    const { body, etag: newEtag } = await call('PUT', `${base}v1/projects/${id}/plan`, plan, etag ? { 'If-Match': etag } : {});
    return { ...body, etag: newEtag };
  },

  async suggest(id, labelStandardParts = false) {
    if (MODE === 'legacy') throw new ApiError('候选分图需要新版后端', 0, 'unsupported',
      '旧后端无候选分图：请在 3D 里框选零件后到「图纸」页新建图。');
    return (await call('POST', `${base}v1/projects/${id}/plan/suggest`, { label_standard_parts: labelStandardParts })).body;
  },

  async render(id, opts = {}) {
    if (MODE === 'legacy') {
      const q = opts.fail ? `?fail=${encodeURIComponent(opts.fail)}` : '';   // 开发用失败场景，后端忽略未知参数
      await call('POST', `${base}api/render${q}`);
      return { job_id: 'legacy:render' };
    }
    const q = opts.fail ? `?fail=${opts.fail}` : '';
    return (await call('POST', `${base}v1/projects/${id}/render${q}`, { plan_etag: opts.etag })).body;
  },

  // 取权威渲染结果（终态后调用）
  async getRenderResult(id) {
    if (MODE === 'legacy') { return (await call('GET', `${base}api/render/status`)).body.result; }
    const d = await this.getProject(id); return d.render;
  },

  // 渲染状态（WP1 契约 §13.5.4 / openapi RenderStatus）：result = 已发布版本，version = 其 ID，
  // job = 本次（最近一次）渲染作业。失败时 result 仍是上一成功版本，本次失败只在 job 里。
  //   legacy：GET api/render/status——新版子应用给出 job/version；旧后端没有，job 为 null。
  //   live：mock 阶段由 /v1/jobs/{id}（本次作业）+ 工程详情（已发布）拼出同一形状；
  //         接线时换成 /p/{pid}/api/render/status，调用方只依赖归一化后的形状。
  async getRenderStatus(id, jobId) {
    if (MODE === 'legacy') {
      const st = (await call('GET', `${base}api/render/status`)).body;
      return { running: !!st.running, result: st.result || null, version: st.version ?? st.result?.version ?? null,
        job: normJob(st.job) };
    }
    const [proj, job] = await Promise.all([
      this.getProject(id),
      jobId ? call('GET', `${base}v1/jobs/${encodeURIComponent(jobId)}`).then((r) => r.body) : null,
    ]);
    const result = proj.render || null;
    return { running: !!(job && ['queued', 'running'].includes(job.state)), result,
      version: result?.render_job_id || result?.version || null, job: normJob(job || proj.render_job) };
  },

  // 导出（live）：请求体须带 render_job_id（要导出的那次成功渲染）+ plan_etag（前端认为的当前计划版本），
  // 由后端与该渲染的冻结输入原子比对，不符 409（WP1）。legacy 兼容模式不提供导出。
  async export(id, opts = {}) {
    if (MODE === 'legacy') throw LEGACY_EXPORT_OFF();
    return (await call('POST', `${base}v1/projects/${id}/export`, opts)).body;
  },
  async getExport(exportId) {
    if (MODE === 'legacy') throw LEGACY_EXPORT_OFF();
    return (await call('GET', `${base}v1/exports/${exportId}`)).body;
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
    return (await call('GET', `${base}v1/jobs/${jobId}`)).body;
  },
  async cancelJob(jobId) {
    if (MODE === 'legacy') return { ok: true };
    return (await call('DELETE', `${base}v1/jobs/${jobId}`)).body;
  },

  async fromExample(ex) {
    if (MODE === 'legacy') throw new ApiError('旧后端从首页「新建」载入示例', 0, 'unsupported');
    return (await call('POST', `${base}v1/projects/from-example`, { example: ex })).body;
  },

  // 上传：贯通 File + 标题 + AI 开关（FormData）；失败不吞文件
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
    fd.append('run_ai', ai ? '1' : '0');
    const q = failScene ? `?fail=${failScene}` : '';
    return (await raw('POST', `${base}v1/projects${q}`, { formData: fd })).body;
  },

  async draftTerms(id) {
    if (MODE === 'legacy') {
      const r = (await call('POST', `${base}api/llm/draft-terms`, { apply: true })).body;
      if (_legacyState && r.plan) _legacyState.plan = r.plan;
      return { plan: r.plan, suggestions: r.suggestions || [],
        filled: (r.applied?.filled?.length || 0) + (r.applied?.added?.length || 0),
        kept_human: r.applied?.kept_human?.length || 0, server_applied: true };
    }
    // WP1 契约：POST /v1/projects/{pid}/ai/draft-terms（无请求体）——服务端起草并只填空白术语、直接写 plan，
    // 返回 {suggestions, filled:[selector], plan_valid, issues}。plan 已在服务端换版本 → 重读 plan 与 ETag。
    const r = (await call('POST', `${base}v1/projects/${encodeURIComponent(id)}/ai/draft-terms`)).body;
    const fresh = await this.getPlan(id);
    const filled = Array.isArray(r.filled) ? r.filled : [];
    return { suggestions: r.suggestions || [], filled: filled.length, filled_selectors: filled,
      plan_valid: r.plan_valid, issues: r.issues || [], plan: fresh.plan, etag: fresh.etag, server_applied: true };
  },

  async analyzeStructure(id) {
    if (MODE === 'legacy') return (await call('POST', `${base}api/structure/analyze`)).body;
    return this.suggest(id, false);
  },
  async getStructure() {
    if (MODE === 'legacy') return (await call('GET', `${base}api/structure`)).body;
    return null;
  },
  async renameGroups(id, groups) {
    if (MODE === 'static') throw new ApiError('静态演示站只读，改名不保存', 0, 'read_only');
    if (MODE === 'legacy') return (await call('PUT', `${base}api/structure`, { groups })).body;
    return (await call('PUT', `${base}v1/projects/${id}/structure`, { groups })).body;
  },

  async llmStatus() {
    if (MODE === 'static') return (await manifest()).llm;
    if (MODE === 'legacy') { const s = await legacyState(); return legacyLlm(s.llm); }
    return (await call('GET', `${base}v1/llm/status`)).body;
  },

  // ---- AI 通道设置：legacy 接真实 /api/wb/settings；mock/static 为演示（不落盘）----
  settingsSupported() { return MODE === 'legacy'; },
  async getSettings() {
    if (MODE === 'legacy') return (await call('GET', `${base}api/wb/settings`)).body;
    const llm = await this.llmStatus();
    return { llm: { configured_provider: 'auto', api_key_set: !!llm.configured, api_key_hint: '',
      base_url: '', model: '', provider_label: llm.provider_label, key_source: '演示' }, demo: true };
  },
  async saveSettings(body) {
    if (MODE === 'legacy') return (await call('PUT', `${base}api/wb/settings`, body)).body;
    throw new ApiError('mock / 静态模式不保存设置（仅演示）', 0, 'demo');
  },
  async testConnection() {
    if (MODE === 'legacy') return (await call('POST', `${base}api/wb/settings/test`)).body;
    throw new ApiError('mock / 静态模式无法测试真实连接', 0, 'demo');
  },

  // ---- 流程图（方法专利）：legacy 走旧 /api/flowcharts*；live/static 无此后端，优雅降级 ----
  flowchartsSupported() { return MODE === 'legacy'; },
  async flowcharts() {
    if (MODE !== 'legacy') return { flowcharts: [] };
    return (await call('GET', `${base}api/flowcharts`)).body;
  },
  async newFlowchart(spec) { return (await call('POST', `${base}api/flowcharts`, spec ? { spec } : {})).body; },
  async saveFlowchart(id, spec) { return (await call('PUT', `${base}api/flowcharts/${id}`, { spec })).body; },
  async renderFlowchart(id) { return (await call('POST', `${base}api/flowcharts/${id}/render`)).body; },
  async deleteFlowchart(id) { return (await call('DELETE', `${base}api/flowcharts/${id}`)).body; },
  async aiFlowchart(text, title, id) { return (await call('POST', `${base}api/flowcharts-ai`, { text, title, id })).body; },

  // ---- 资源 URL ----
  modelUrl(id) {
    if (MODE === 'legacy') return token ? `${base}api/model.glb?token=${encodeURIComponent(token)}` : `${base}api/model.glb`;
    if (MODE === 'static') return `${base}${rel((this._m(id) || {}).model_url || `v1/projects/${id}/model.glb`)}`;
    return `${base}v1/projects/${id}/model.glb`;
  },
  previewUrl(p) {
    const r = `${base}${rel(p)}`;
    return (MODE === 'legacy' && token) ? `${r}${r.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : r;
  },
  _manifestSync: null,
  _m(id) { return (this._manifestSync?.projects || []).find((x) => x.id === id)?.detail; },

  // ---- 作业事件：live 走 SSE；legacy 轮询代替；static 立即完成 ----
  jobEvents(jobId, handlers = {}) {
    if (MODE === 'static') { setTimeout(() => handlers.done && handlers.done({}), 0); return { close() {} }; }
    if (MODE === 'legacy') return legacyPoll(jobId, handlers);
    const es = new EventSource(`${base}v1/jobs/${jobId}/events`);
    const on = (name, fn) => fn && es.addEventListener(name, (e) => {
      let d = {}; try { d = JSON.parse(e.data); } catch { /* 空 */ } fn(d, e.lastEventId);
    });
    on('stage', handlers.stage);
    on('log', handlers.log);
    on('done', (d, id) => { handlers.done && handlers.done(d, id); es.close(); });
    // 关键：'error' 事件既可能是「业务失败」（带 data），也可能是 EventSource 的「传输错误」（无 data）。
    // 只有带 data 才当业务失败并关流；传输错误保留浏览器自动重连，交给上层用 getJob 校准。
    es.addEventListener('error', (e) => {
      if (e.data == null) { handlers.disconnect && handlers.disconnect(); return; }
      let d = {}; try { d = JSON.parse(e.data); } catch { /* 空 */ }
      handlers.fail && handlers.fail(d, e.lastEventId);
      es.close();
    });
    return { close() { es.close(); } };
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
