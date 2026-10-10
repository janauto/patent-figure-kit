// 数据适配器：两种后端形态走同一组方法。
//   live   —— 连本地工作台（新 §6 /v1/* 契约；保留少量旧 /api/* 只读兼容）。
//   static —— 读版本化 manifest.json，供 GitHub Pages 等静态演示站只读运行（S2-16）。
// 选择：URL 带 ?static 或页面在 file:// 下 → static；否则 live。
// 无 token 不 fatal：live 无 token 时首页进入只读/引导态，不抛错（S2-16）。

const params = new URLSearchParams(location.search);
const forcedStatic = params.has('static') || location.protocol === 'file:';

const fromUrl = params.get('token');
if (fromUrl) { try { sessionStorage.setItem('studio-token', fromUrl); } catch { /* 隐私模式 */ } }
export const token = fromUrl || (() => {
  try { return sessionStorage.getItem('studio-token') || ''; } catch { return ''; }
})();

export const MODE = forcedStatic ? 'static' : 'live';
export const readOnly = MODE === 'static';
// 相对路径根：static 站挂在 Pages 子路径时用相对 URL（不得用 /static 绝对路径）。
const base = new URL('.', location.href).pathname.replace(/\/js\/?$/, '/');

function rel(p) { return (p || '').replace(/^\//, ''); }

class ApiError extends Error {
  constructor(message, status, code, hint) {
    super(message); this.status = status; this.code = code; this.hint = hint;
  }
}

async function call(method, path, body, headers = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      ...(token ? { 'X-Studio-Token': token, Authorization: `Bearer ${token}` } : {}),
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText; let code = 'error'; let hint = '';
    try {
      const j = await res.json();
      msg = (j.error && j.error.message) || j.detail || msg;
      code = (j.error && j.error.code) || code;
      hint = (j.error && j.error.hint) || '';
    } catch { /* 保底用 statusText */ }
    throw new ApiError(msg, res.status, code, hint);
  }
  return { body: await res.json().catch(() => ({})), etag: res.headers.get('ETag') };
}

// -------------------------------------------------------------- static 适配
let _manifest = null;
async function manifest() {
  if (_manifest) return _manifest;
  const res = await fetch(`${base}dev/manifest.json`);
  if (!res.ok) throw new ApiError('静态 manifest 缺失', 404, 'no_manifest');
  _manifest = await res.json();
  api._manifestSync = _manifest;        // 供 modelUrl 等同步取用
  return _manifest;
}

// -------------------------------------------------------------- 统一接口
export const api = {
  mode: MODE,
  readOnly,

  async listProjects(scene) {
    if (MODE === 'static') {
      const m = await manifest();
      return { projects: m.projects.map((p) => p.summary), llm: m.llm,
        data_dir: m.data_dir, display_name: m.display_name, static: true };
    }
    const q = scene ? `?scene=${encodeURIComponent(scene)}` : '';
    return (await call('GET', `${base}v1/projects${q}`)).body;
  },

  async getProject(id) {
    if (MODE === 'static') {
      const m = await manifest();
      const p = m.projects.find((x) => x.id === id);
      if (!p) throw new ApiError('静态站没有这个工程', 404, 'not_found');
      return p.detail;
    }
    return (await call('GET', `${base}v1/projects/${id}`)).body;
  },

  async getPlan(id) {
    if (MODE === 'static') {
      const p = (await manifest()).projects.find((x) => x.id === id);
      return { plan: p?.detail?.plan, etag: p?.detail?.plan_etag };
    }
    const { body, etag } = await call('GET', `${base}v1/projects/${id}/plan`);
    return { plan: body.plan, etag };
  },

  async putPlan(id, plan, etag) {
    if (MODE === 'static') throw new ApiError('静态演示站只读，改动不保存', 0, 'read_only');
    const { body, etag: newEtag } = await call('PUT', `${base}v1/projects/${id}/plan`,
      { plan }, etag ? { 'If-Match': etag } : {});
    return { ...body, etag: newEtag };
  },

  async suggest(id, labelStandardParts = false) {
    return (await call('POST', `${base}v1/projects/${id}/plan/suggest`,
      { label_standard_parts: labelStandardParts })).body;
  },
  async render(id, opts = {}) {
    const q = opts.fail ? `?fail=${opts.fail}` : '';
    return (await call('POST', `${base}v1/projects/${id}/render${q}`,
      { plan_etag: opts.etag })).body;
  },
  async export(id, opts) {
    return (await call('POST', `${base}v1/projects/${id}/export`, opts)).body;
  },
  async getJob(jobId) { return (await call('GET', `${base}v1/jobs/${jobId}`)).body; },
  async getExport(exportId) { return (await call('GET', `${base}v1/exports/${exportId}`)).body; },
  async cancelJob(jobId) { return (await call('DELETE', `${base}v1/jobs/${jobId}`)).body; },
  async fromExample(ex) {
    return (await call('POST', `${base}v1/projects/from-example`, { example: ex })).body;
  },
  async createProject(opts = {}) {
    const q = opts.fail ? `?fail=${opts.fail}` : '';
    return (await call('POST', `${base}v1/projects${q}`, {})).body;
  },
  async draftTerms(id) {
    return (await call('POST', `${base}v1/ai/draft-terms`, { project: id })).body;
  },
  async llmStatus() {
    if (MODE === 'static') return (await manifest()).llm;
    return (await call('GET', `${base}v1/llm/status`)).body;
  },

  // ---- 资源 URL（相对，static 与 live 都可用）----
  modelUrl(id) { return MODE === 'static'
    ? `${base}${rel((this._m(id) || {}).model_url || `v1/projects/${id}/model.glb`)}`
    : `${base}v1/projects/${id}/model.glb`; },
  previewUrl(p) { return `${base}${rel(p)}`; },
  _manifestSync: null,
  _m(id) { return (this._manifestSync?.projects || []).find((x) => x.id === id)?.detail; },

  // ---- SSE：返回一个可关闭的订阅；static 下立即「已完成」----
  jobEvents(jobId, handlers = {}) {
    if (MODE === 'static') {
      setTimeout(() => handlers.done && handlers.done({}), 0);
      return { close() {} };
    }
    const es = new EventSource(`${base}v1/jobs/${jobId}/events`);
    const on = (name, fn) => fn && es.addEventListener(name, (e) => {
      let d = {}; try { d = JSON.parse(e.data); } catch { /* 空事件 */ } fn(d, e.lastEventId);
    });
    on('stage', handlers.stage);
    on('log', handlers.log);
    on('done', (d, id) => { handlers.done && handlers.done(d, id); es.close(); });
    on('error', (d, id) => { handlers.fail && handlers.fail(d, id); es.close(); });
    es.onerror = () => { handlers.disconnect && handlers.disconnect(); };
    return { close() { es.close(); } };
  },
};

export { ApiError };
