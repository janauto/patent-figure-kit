// 全局状态与事件总线。plan 是唯一真相（与磁盘上的 plan.json 一一对应）；
// 界面上的一切改动都收敛为「改 plan → 防抖保存 → 服务器校验」这一条路。
import { api, normJob } from './api.js';

const listeners = new Map();
export const bus = {
  on(evt, fn) { (listeners.get(evt) || listeners.set(evt, []).get(evt)).push(fn); },
  emit(evt, ...args) { (listeners.get(evt) || []).forEach((fn) => fn(...args)); },
};

export const state = {
  projectId: '',        // 当前工程 id（来自 ?project=）
  planEtag: null,       // plan 的 ETag，用于乐观锁
  title: '',
  assembly: null,
  plan: null,
  validate: null,       // 服务器 validate 结果 {ok, issues}
  render: null,         // 已发布的渲染结果（失败的作业不会替换它）
  renderJob: null,      // 本次（最近一次）渲染作业状态 {id, state, running, ok, error, result, diag}
  // 渲染结果绑定的版本证据——只用后端给的：输入快照 plan_etag + 结果 ID（版本 rid = render_job_id）。
  // 没有证据即锁定导出，绝不用当前 plan 补造。conflict=true：导出时后端判定计划已变更（409）。
  // 旧版后端兼容模式（legacy）不建立任何证据：导出一律锁定（修复轮 7 架构裁决）。
  renderInputs: { etag: null, version: null, conflict: false },
  rendering: false,
  candidates: [],       // F2-lite 候选分图（openapi Candidate[]）
  candidatesJob: null,  // 最近一次候选分图作业 {job_id, state, running, empty_reason, guide, summary, error}
  selection: new Set(), // 零件名集合（plan 以名字寻址，实例不单选）
  activeFigure: null,   // 当前操作的图 id
  saveState: 'clean',   // clean | dirty | saving | saved | error
  editVersion: 0,       // 每次 mutate +1；保存按版本确认，慢 PUT 期间的新编辑不被吞
  savedVersion: 0,      // 已持久化到服务端的最高编辑版本
  step: '',
  llm: null,            // 大模型通道状态
  aiBusy: false,
  aiSuggestions: {},    // selector -> {term, confidence, reason}
  structure: null,      // {source: ai|fallback, groups:[{id,name,role,parts}], names:{part:{name}}}
  structureJob: null,   // AI 识别进度
  partsMode: 'structure',
  collapsed: new Set(), // 折叠的结构组 id
  // 3D 观察参数：只影响看与选，不进 plan、不影响出图
  view: {
    opacity: 1, explode: 0, explodeMode: 'group', colorBy: 'figure',
    hiddenGroups: new Set(), soloGroup: null,
  },
};

// ---- 结构组与显示名 ----
export const STRUCT_COLORS = [
  '#2c7fb8', '#c8552c', '#7a9a01', '#8e5ba6', '#d09a00',
  '#0d8a8a', '#b04a76', '#5b6ee1', '#946b3d', '#6b6b6b',
];
let groupIndex = new Map();
export function indexStructure() {
  groupIndex = new Map();
  (state.structure?.groups || []).forEach((g) => g.parts.forEach((n) => groupIndex.set(n, g)));
}
export const groupOf = (name) => groupIndex.get(name) || null;
export function groupColor(gid) {
  const idx = (state.structure?.groups || []).findIndex((g) => g.id === gid);
  return STRUCT_COLORS[(idx < 0 ? 0 : idx) % STRUCT_COLORS.length];
}
export const aiName = (name) => state.structure?.names?.[name]?.name || '';
export function displayName(name) {
  const t = termFor(name);
  return (t?.term || '').trim() || aiName(name);
}
export function setView(patch) {
  Object.assign(state.view, patch);
  bus.emit('view');
}

// 图的着色板：晒图纸上的彩铅色，区分度优先，固定顺序保证同一张图颜色稳定。
export const FIGURE_COLORS = [
  '#c8552c', '#2c7fb8', '#7a9a01', '#8e5ba6', '#d09a00',
  '#0d8a8a', '#b04a76', '#5b6ee1', '#946b3d', '#4a4a4a',
];
export const figureColor = (figId) => {
  const idx = (state.plan?.figures || []).findIndex((f) => f.id === figId);
  return FIGURE_COLORS[((idx < 0 ? 0 : idx)) % FIGURE_COLORS.length];
};

// ---- glob 解析（与服务器 fnmatchcase 同语义：区分大小写，* ? [seq]） ----
const globCache = new Map();
export function globToRegExp(glob) {
  if (globCache.has(glob)) return globCache.get(glob);
  let out = '^';
  for (let i = 0; i < glob.length; i += 1) {
    const ch = glob[i];
    if (ch === '*') out += '.*';
    else if (ch === '?') out += '.';
    else if (ch === '[') {
      const end = glob.indexOf(']', i + 1);
      if (end > i) { out += glob.slice(i, end + 1); i = end; } else out += '\\[';
    } else out += ch.replace(/[.+^${}()|\\]/g, '\\$&');
  }
  const re = new RegExp(out + '$');
  globCache.set(glob, re);
  return re;
}

export const partNames = () => (state.assembly?.parts || []).map((p) => p.name);
export const partByName = (name) =>
  (state.assembly?.parts || []).find((p) => p.name === name) || null;

export function resolveMembers(fig) {
  const names = partNames();
  const hit = new Set();
  (fig.members || []).forEach((glob) => {
    const re = globToRegExp(glob);
    names.forEach((n) => { if (re.test(n)) hit.add(n); });
  });
  return hit;
}

export function figuresContaining(name) {
  return (state.plan?.figures || []).filter((f) => resolveMembers(f).has(name));
}

export function unassignedParts() {
  const covered = new Set();
  (state.plan?.figures || []).forEach((f) => resolveMembers(f).forEach((n) => covered.add(n)));
  return partNames().filter((n) => !covered.has(n));
}

export function termFor(name) {
  for (const t of state.plan?.terms || []) {
    if (globToRegExp(t.selector).test(name)) return t;
  }
  return null;
}

// 前端预演的「本图有效标记数」——权威判定永远是服务器 validate；这里只为图卡上的 n/20 徽标。
export function labelCount(fig) {
  const members = resolveMembers(fig);
  let count = 0;
  const seen = new Set();
  members.forEach((name) => {
    const term = termFor(name);
    const mode = term?.label || 'once';
    if (mode === 'none') return;
    if (mode === 'all') count += partByName(name)?.instances || 1;
    else if (!seen.has(name)) { seen.add(name); count += 1; }
  });
  return count;
}

// ---- 变更与持久化 ----
let saveTimer = null;
// ETag 规范化：去掉弱前缀 W/ 与引号再比（代理/框架可能给同一版本加不同修饰）。
export function normEtag(e) {
  if (!e) return null;
  return String(e).trim().replace(/^W\//, '').replace(/^"(.*)"$/, '$1') || null;
}
// 渲染结果自带的输入版本证据：mock / 本前端约定 inputs.plan_etag；WP1 实现放在顶层 plan_etag。
export function resultEtag(r) {
  return normEtag(r?.inputs?.plan_etag || r?.plan_etag || null);
}
// 渲染结果的版本 ID（WP1：render_job_id，即版本目录 rid；子应用结果另带 version）。
export function resultVersion(r) {
  return r?.render_job_id || r?.version || null;
}

// 结果是否过期 / 为何过期：
//   'unsaved'（有未持久化编辑或保存失败）| 'legacy'（旧版后端兼容模式：不建立证据，导出恒锁定）
//   | 'conflict'（导出时后端 409：计划已变更）| 'no-proof'（缺后端版本证据）| 'changed' | null
export function renderStaleReason() {
  if (!state.render) return null;
  // 本地还有没落盘的编辑、或上次保存失败 → 立即判过期（planEtag 此刻还是旧值，不能据此放行）
  if (state.editVersion > state.savedVersion) return 'unsaved';
  if (state.saveState === 'error' || state.saveState === 'conflict') return 'unsaved';
  if (api.isLegacy) return 'legacy';
  if (state.renderInputs.conflict) return 'conflict';
  // 只认后端证据：结果冻结输入的 ETag 与结果 ID 都要有，且 ETag 与当前计划一致
  const ev = normEtag(state.renderInputs.etag);
  if (!state.planEtag || !ev || !state.renderInputs.version) return 'no-proof';
  return ev === normEtag(state.planEtag) ? null : 'changed';
}
export function renderStale() { return renderStaleReason() !== null; }

export function mutate(fn) {
  fn(state.plan);
  state.editVersion += 1;                        // 每次编辑递增版本
  state.saveState = 'dirty';
  if (state.render) bus.emit('render-stale');   // 改计划 → 旧结果与缓存 ZIP 失效
  bus.emit('plan');
  bus.emit('save-state');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flush, 600);
}

async function doSave() {
  if (api.readOnly) { state.saveState = 'readonly'; bus.emit('save-state'); return; }
  const v = state.editVersion;                   // 本轮要保存的版本（PUT body 在 api 里同步序列化 = 此刻的 plan）
  state.saveState = 'saving';
  bus.emit('save-state');
  try {
    const res = await api.putPlan(state.projectId, state.plan, state.planEtag);
    if (res.etag) state.planEtag = res.etag;
    if (res.validate) { state.validate = res.validate; bus.emit('validate'); }
    else scheduleValidate();                      // live：PUT 只回 {ok}，校验结果另读（子应用 api/state）
    state.savedVersion = Math.max(state.savedVersion, v);
    state.saveState = (state.editVersion === v) ? 'saved' : 'dirty';
  } catch (err) {
    state.saveState = (err.status === 409 || err.code === 'etag_conflict') ? 'conflict' : 'error';
    // 冲突是已处理的业务状态（顶栏给「重载」），不算程序错误；其它保存失败才记 error
    if (state.saveState === 'conflict') console.warn('保存冲突（计划已在别处修改）', err.message);
    else console.error('保存失败', err);
  }
  bus.emit('save-state');
}
// live：保存成功后防抖回读服务端校验（validate_figure_plan.py 的结果），不阻塞保存循环
let validateTimer = null; let validateSeq = 0;
export function scheduleValidate(ms = 900) {
  if (api.readOnly || api.isLegacy) return;
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    const seq = ++validateSeq; const v = state.savedVersion;
    try {
      const val = await api.getValidate(state.projectId);
      // 期间又保存过（新的回读会覆盖）或有更新的编辑在途：丢弃这次旧结果
      if (val && seq === validateSeq && v === state.savedVersion) { state.validate = val; bus.emit('validate'); }
    } catch { /* 校验回读失败不影响保存 */ }
  }, ms);
}

// 全局唯一的保存循环：所有 flush() 调用共享同一个 Promise，只有这一个循环负责发送与清理，
// 杜绝并发 flush 各自发 PUT（同 ETag → 409 / 丢编辑）。循环持续到最新编辑版本已落盘或保存失败。
//
// 时序要点（Codex R3e #1）：循环体必须在 flushLoop 赋值「之后」才开始执行。若用立即执行的 async 函数，
// 无待保存内容时循环在第一个 await 之前就退出，finally 会先同步清空 flushLoop，随后外层赋值又把这个
// 已完成的 Promise 写回——之后所有 flush() 都拿到它、永不再保存。这里用 Promise.resolve().then 启动，
// 保证「赋值 → 首次检查」的先后；退出判定与清理在同一段同步代码里完成，中间不留可被插入的窗口。
let flushLoop = null;
function saveFailed() { return state.saveState === 'error' || state.saveState === 'conflict'; }
export function flush() {
  clearTimeout(saveTimer);
  if (!flushLoop) {
    const loop = Promise.resolve().then(async () => {
      try {
        for (let guard = 0; guard < 100; guard += 1) {
          if (saveFailed()) break;                               // 失败后停下，等下一次编辑/重试
          if (state.savedVersion >= state.editVersion) break;   // 最新编辑已落盘
          await doSave();                                        // 串行，一次只有一个 PUT 在途
        }
      } finally {
        if (flushLoop === loop) flushLoop = null;                // 与最后一次检查同步完成，无窗口
      }
    });
    flushLoop = loop;
  }
  return flushLoop;
}

export function setSelection(names) {
  state.selection = new Set(names);
  bus.emit('selection');
}
export function toggleSelect(name) {
  if (state.selection.has(name)) state.selection.delete(name);
  else state.selection.add(name);
  bus.emit('selection');
}
export function setActiveFigure(id) {
  state.activeFigure = id;
  bus.emit('active-figure');
}

export async function boot(projectId) {
  state.projectId = projectId;
  api.setProject(projectId);
  const data = await api.getProject(projectId);
  state.title = data.title;
  state.step = data.step_name;
  state.assembly = data.assembly;
  state.render = data.render;
  state.rendering = false;
  state.candidates = data.candidates || [];
  state.candidatesJob = data.candidates_job || null;
  state.candidatesUnsupported = !!data.candidates_unsupported;   // 旧后端无候选分图
  state.caps = api.caps || null;
  state.homeUrl = data.home_url || null;
  state.llm = data.llm || null;
  state.structure = data.structure || null;
  state.validate = data.validate || { ok: true, issues: [] };
  const planRes = await api.getPlan(projectId);
  state.plan = planRes.plan;
  state.planEtag = planRes.etag || null;
  state.editVersion = 0; state.savedVersion = 0;
  // 结果版本证据：只认后端——结果携带的输入快照 plan_etag + 结果 ID（版本）。legacy 兼容模式不建立证据。
  state.renderInputs = { etag: null, version: null, conflict: false };
  if (state.render && !api.isLegacy) {
    state.renderInputs.etag = resultEtag(state.render);
    state.renderInputs.version = resultVersion(state.render);
  }
  // 本次（最近一次）渲染作业：新版后端给 render_job；旧后端只有 rendering 标志 → 视作一个在跑的作业
  state.renderJob = data.render_job ? normJob(data.render_job)
    : (data.rendering ? { id: 'legacy:render', state: 'running', running: true, ok: null, error: null, result: null, diag: null } : null);
  // 修复轮 5–6 的 legacy 本地证明已作废（裁决：兼容模式不再凭本地记录解锁），顺手清掉残留
  try { localStorage.removeItem(`inkline-render-proof-${projectId}`); localStorage.removeItem(`inkline-render-digest-${projectId}`); } catch { /* */ }
  indexStructure();
  if (state.structure?.source === 'ai') state.view.colorBy = 'structure';
  const groups = state.structure?.groups || [];
  const total = groups.reduce((n, g) => n + g.parts.length, 0);
  groups.forEach((g) => { if (total > 40 && g.parts.length > 12) state.collapsed.add(g.id); });
  state.activeFigure = state.plan?.figures?.[0]?.id || null;
  bus.emit('booted');
}
