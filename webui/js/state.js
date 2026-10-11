// 全局状态与事件总线。plan 是唯一真相（与磁盘上的 plan.json 一一对应）；
// 界面上的一切改动都收敛为「改 plan → 防抖保存 → 服务器校验」这一条路。
import { api } from './api.js';

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
  render: null,         // 最近一次渲染结果
  // 渲染结果绑定的「输入版本证据」：live 用后端输入快照的 plan_etag，legacy 用「发请求前」捕获的已保存输入摘要。
  // 没有证据（null）时视为过期并锁定导出——绝不用当前 plan 补造摘要给旧结果背书。
  // raced=true：该作业在途期间计划被改过（legacy 后端在后台线程里读 plan，无法确定它画的是哪一版）→ 一律锁定。
  renderInputs: { etag: null, digest: null, raced: false },
  rendering: false,
  candidates: [],       // F2-lite 候选分图
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
// plan 规范化摘要：绑定渲染结果与导出，证明「这张图对应的就是当前这份 plan」。
// legacy 无 ETag 也能算；键排序后哈希，任何实质改动都会变。
function canon(v) {
  if (Array.isArray(v)) return `[${v.map(canon).join(',')}]`;
  if (v && typeof v === 'object') {
    return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(',')}}`;
  }
  return JSON.stringify(v);
}
// 64 位规范化摘要（djb2 + FNV-1a 两路 32 位），键序无关。
function digest64(v) {
  const s = canon(v);
  let h1 = 5381; let h2 = 0x811c9dc5;
  for (let i = 0; i < s.length; i += 1) {
    const c = s.charCodeAt(i);
    h1 = ((h1 << 5) + h1 + c) >>> 0;
    h2 = Math.imul(h2 ^ c, 0x01000193) >>> 0;
  }
  return h1.toString(16).padStart(8, '0') + h2.toString(16).padStart(8, '0');
}
export function planDigest(plan = state.plan) {
  return plan ? digest64(plan) : '';
}
// 渲染结果的身份：整份结果（图、QA、标记表、完成时间、日志）的规范化摘要。
// legacy 旧后端没有结果 ID，用它把「输入摘要」钉在某一份具体结果上：结果被另一轮渲染替换后身份必变。
export function resultIdentity(r) {
  return r ? `${r.finished_at || r.finished || ''}|${digest64(r)}` : '';
}

// legacy 的「渲染证明」跨会话持久化（每工程一条）：{v, digest（该次渲染的输入摘要）, result（结果身份）}。
// 只在本页亲手完成、且无竞态的一次渲染后写入；发起新渲染前立即作废（Codex R3f #2）；
// 跨会话恢复时必须与当前加载到的结果身份一致才采信——工程级摘要本身不再能单独背书。
const proofKey = (pid) => `inkline-render-proof-${pid}`;
const legacyDigestKey = (pid) => `inkline-render-digest-${pid}`;   // 轮 3–5 的旧格式（无结果身份），一律不再采信
export function proofGet(pid) {
  try {
    const p = JSON.parse(localStorage.getItem(proofKey(pid)) || 'null');
    return (p && p.v === 2 && p.digest && p.result) ? p : null;
  } catch { return null; }
}
export function proofSet(pid, proof) {
  try {
    localStorage.removeItem(legacyDigestKey(pid));
    if (proof && proof.digest && proof.result) {
      localStorage.setItem(proofKey(pid), JSON.stringify({ v: 2, digest: proof.digest, result: proof.result }));
    } else localStorage.removeItem(proofKey(pid));
  } catch { /* 隐私模式：不持久化，刷新后锁定 */ }
}

// ETag 规范化：去掉弱前缀 W/ 与引号再比（代理/框架可能给同一版本加不同修饰）。
export function normEtag(e) {
  if (!e) return null;
  return String(e).trim().replace(/^W\//, '').replace(/^"(.*)"$/, '$1') || null;
}
// 渲染结果自带的输入版本证据：mock / 本前端约定 inputs.plan_etag；WP1 实现放在顶层 plan_etag。
export function resultEtag(r) {
  return normEtag(r?.inputs?.plan_etag || r?.plan_etag || null);
}

// 结果是否过期 / 为何过期：
//   'unsaved'（有未持久化编辑或保存失败——必须先存好再谈一致）| 'raced'（出图在途期间计划被改过）
//   | 'no-proof'（无版本证据）| 'changed' | null
export function renderStaleReason() {
  if (!state.render) return null;
  // 本地还有没落盘的编辑、或上次保存失败 → 立即判过期（live 的 planEtag 此刻还是旧值，不能据此放行）
  if (state.editVersion > state.savedVersion) return 'unsaved';
  if (state.saveState === 'error' || state.saveState === 'conflict') return 'unsaved';
  // 判据：有 ETag（live /v1 后端）→ 用 ETag 比对；无 ETag（legacy 旧 /api/*）→ 用输入摘要比对。
  if (state.planEtag) {                          // live：以后端输入快照的 ETag 为准（权威，冻结于作业提交时）
    const ev = normEtag(state.renderInputs.etag);
    if (!ev) return 'no-proof';
    return ev === normEtag(state.planEtag) ? null : 'changed';
  }
  // legacy：以「发请求前」捕获的已保存输入摘要为准（无 ETag）
  if (state.renderInputs.raced) return 'raced';
  if (!state.renderInputs.digest) return 'no-proof';
  return state.renderInputs.digest === planDigest() ? null : 'changed';
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
    state.savedVersion = Math.max(state.savedVersion, v);
    state.saveState = (state.editVersion === v) ? 'saved' : 'dirty';
  } catch (err) {
    state.saveState = err.code === 'etag_conflict' ? 'conflict' : 'error';
    console.error('保存失败', err);
  }
  bus.emit('save-state');
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
  const data = await api.getProject(projectId);
  state.title = data.title;
  state.step = data.step_name;
  state.assembly = data.assembly;
  state.render = data.render;
  state.rendering = false;
  state.candidates = data.candidates || [];
  state.candidatesUnsupported = !!data.candidates_unsupported;   // 旧后端无候选分图
  state.homeUrl = data.home_url || null;
  state.llm = data.llm || null;
  state.structure = data.structure || null;
  state.validate = data.validate || { ok: true, issues: [] };
  const planRes = await api.getPlan(projectId);
  state.plan = planRes.plan;
  state.planEtag = planRes.etag || null;
  state.editVersion = 0; state.savedVersion = 0;
  // 结果版本证据：绝不用当前 plan 补造。live 用后端结果携带的 plan_etag；legacy 用本工程持久化的渲染证明
  //（输入摘要 + 结果身份，结果身份不符或后端正在渲染即不采信）。
  // 没有证据 → renderInputs 全 null → renderStale 判「no-proof」→ 锁定导出并提示「请重新出图」。
  state.renderInputs = { etag: null, digest: null, raced: false };
  if (state.render) {
    state.renderInputs.etag = resultEtag(state.render);                      // live：结果自带的输入快照
    if (!state.planEtag && !data.rendering) {                                 // legacy：证明须钉在这一份结果上
      const proof = proofGet(projectId);
      if (proof && proof.result === resultIdentity(state.render)) state.renderInputs.digest = proof.digest;
    }
  }
  indexStructure();
  if (state.structure?.source === 'ai') state.view.colorBy = 'structure';
  const groups = state.structure?.groups || [];
  const total = groups.reduce((n, g) => n + g.parts.length, 0);
  groups.forEach((g) => { if (total > 40 && g.parts.length > 12) state.collapsed.add(g.id); });
  state.activeFigure = state.plan?.figures?.[0]?.id || null;
  bus.emit('booted');
}
