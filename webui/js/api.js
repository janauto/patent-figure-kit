// 数据适配器：三种后端形态走同一组方法。
//   live   —— 新版工作台（§6 /v1/* 契约，带作业 + SSE + 候选分图），对 mock 开发。
//   legacy —— 仓库现有后端：workbench.py 在 / 挂首页、plan_studio 在 /p/{id}/ 用旧同步 /api/*，
//             没有候选分图 / 作业 / SSE——优雅降级（轮询代替 SSE，候选分图给出「需要新版后端」提示）。
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
    return { plan: body.plan, etag };
  },

  async putPlan(id, plan, etag) {
    if (MODE === 'static') throw new ApiError('静态演示站只读，改动不保存', 0, 'read_only');
    if (MODE === 'legacy') {
      const res = (await call('PUT', `${base}api/plan`, { plan })).body;
      if (_legacyState) _legacyState.plan = plan;
      return { validate: res.validate, etag: null };
    }
    const { body, etag: newEtag } = await call('PUT', `${base}v1/projects/${id}/plan`, { plan }, etag ? { 'If-Match': etag } : {});
    return { ...body, etag: newEtag };
  },

  async suggest(id, labelStandardParts = false) {
    if (MODE === 'legacy') throw new ApiError('候选分图需要新版后端', 0, 'unsupported',
      '旧后端无候选分图：请在 3D 里框选零件后到「图纸」页新建图。');
    return (await call('POST', `${base}v1/projects/${id}/plan/suggest`, { label_standard_parts: labelStandardParts })).body;
  },

  async render(id, opts = {}) {
    if (MODE === 'legacy') {
      await call('POST', `${base}api/render`);     // 202 {started}
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

  async export(id, opts = {}) {
    if (MODE === 'legacy') {
      const bundle = (await call('POST', `${base}api/export`, { dwg: (opts.formats || []).includes('dwg') })).body;
      // 清单按旧后端实际产出来判定——旧后端不出 PDF（F1 属后续工作包），绝不假装生成
      const files = bundle.files || [];
      const has = (ext) => files.some((f) => f.toLowerCase().endsWith(ext));
      const items = [];
      if (has('.docx')) items.push({ format: 'docx', label: '交底版 Word（说明书附图）', status: 'ok' });
      if (has('.dxf')) items.push({ format: 'dxf', label: 'DXF 源文件（交底/递交/原始）', status: 'ok' });
      if (has('.dwg')) items.push({ format: 'dwg', label: 'DWG 源文件', status: 'ok' });
      else if ((opts.formats || []).includes('dwg')) items.push({ format: 'dwg', label: 'DWG 源文件', status: 'fail', reason: 'DWG 转换器不可用' });
      if ((opts.formats || []).includes('pdf')) items.push({ format: 'pdf', label: '递交版 PDF', status: 'skip', reason: '现有后端不产出 PDF，接线后由新版提供' });
      api._lastExport = {
        export_id: 'legacy:export', job_id: 'legacy:export', variant: opts.variant || 'both',
        // 相对路径（不含 base）：下游统一用 previewUrl 只解析一次挂载前缀，避免 /p/{id}/p/{id}/… 重复
        zip: bundle.zip, download_url: `api/export-file/${encodeURIComponent(bundle.zip)}`,
        items, manifest: files, notes: bundle.notes || [],
      };
      return { export_id: 'legacy:export', job_id: 'legacy:export' };
    }
    return (await call('POST', `${base}v1/projects/${id}/export`, opts)).body;
  },
  async getExport(exportId) {
    if (MODE === 'legacy') return api._lastExport;
    return (await call('GET', `${base}v1/exports/${exportId}`)).body;
  },

  async getJob(jobId) {
    if (MODE === 'legacy') {
      if (jobId === 'legacy:render') {
        const st = (await call('GET', `${base}api/render/status`)).body;
        return { state: st.running ? 'running' : 'succeeded', result: st.result };
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
    return (await call('POST', `${base}v1/ai/draft-terms`, { project: id })).body;
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
  _manifestSync: null, _lastExport: null,
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

// legacy：轮询 render/status 模拟进度事件
function legacyPoll(jobId, handlers) {
  if (jobId !== 'legacy:render') { setTimeout(() => handlers.done && handlers.done({}), 0); return { close() {} }; }
  let stopped = false; let p = 0.1;
  const tick = async () => {
    if (stopped) return;
    try {
      const st = (await call('GET', `${base}api/render/status`)).body;
      if (st.running) {
        p = Math.min(0.9, p + 0.12);
        handlers.stage && handlers.stage({ stage: '出图中', progress: p });
        setTimeout(tick, 900);
      } else {
        handlers.done && handlers.done({ result: st.result });
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
