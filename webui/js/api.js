// 与 Plan Studio 服务器的全部通信。路径一律相对：单工程模式挂在 /，工作台里挂在 /p/<id>/。
// token 来自地址栏 ?token=，存进 sessionStorage，页内跳转后仍可用。
const fromUrl = new URLSearchParams(location.search).get('token');
if (fromUrl) { try { sessionStorage.setItem('studio-token', fromUrl); } catch { /* 隐私模式 */ } }
export const token = fromUrl || (() => {
  try { return sessionStorage.getItem('studio-token') || ''; } catch { return ''; }
})();

async function call(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: {
      'X-Studio-Token': token,
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* 保底用 statusText */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function upload(path, file) {
  const res = await fetch(`${path}?name=${encodeURIComponent(file.name)}&token=${encodeURIComponent(token)}`, {
    method: 'POST', headers: { 'X-Studio-Token': token }, body: file,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* 保底 */ }
    throw new Error(detail);
  }
  return res.json();
}

// 服务器返回的预览地址形如 /api/preview/x.svg —— 去掉开头的 / 变成相对路径
const rel = (p) => (p || '').replace(/^\//, '');
const withToken = (p) => `${rel(p)}${p.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`;

export const api = {
  state: () => call('GET', 'api/state'),
  importBom: (file) => upload('api/bom', file),
  savePlan: (plan) => call('PUT', 'api/plan', { plan }),
  render: () => call('POST', 'api/render'),
  renderStatus: () => call('GET', 'api/render/status'),
  exportAll: (dwg) => call('POST', 'api/export', { dwg }),
  exportUrl: (zip) => withToken(`api/export-file/${zip}`),
  modelUrl: () => withToken('api/model.glb'),
  previewUrl: (p) => withToken(p),
  // AI
  llmStatus: () => call('GET', 'api/llm/status'),
  draftTerms: (opts) => call('POST', 'api/llm/draft-terms', opts || { apply: true }),
  snapshot: async (name, dataUrl) => {
    const blob = await (await fetch(dataUrl)).blob();
    const res = await fetch(`api/snapshot?name=${encodeURIComponent(name)}&token=${encodeURIComponent(token)}`,
      { method: 'POST', headers: { 'X-Studio-Token': token }, body: blob });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  },
  // 结构识别
  structure: () => call('GET', 'api/structure'),
  analyzeStructure: () => call('POST', 'api/structure/analyze'),
  renameGroups: (groups) => call('PUT', 'api/structure', { groups }),
  applyStructureNames: (overwrite) => call('POST', 'api/structure/apply-names', { overwrite: !!overwrite }),
  // 流程图
  flowcharts: () => call('GET', 'api/flowcharts'),
  newFlowchart: (spec) => call('POST', 'api/flowcharts', spec ? { spec } : {}),
  saveFlowchart: (id, spec) => call('PUT', `api/flowcharts/${id}`, { spec }),
  renderFlowchart: (id) => call('POST', `api/flowcharts/${id}/render`),
  deleteFlowchart: (id) => call('DELETE', `api/flowcharts/${id}`),
  aiFlowchart: (text, title, id) => call('POST', 'api/flowcharts-ai', { text, title, id }),
};
