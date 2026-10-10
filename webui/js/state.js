// 全局状态与事件总线。plan 是唯一真相（与磁盘上的 plan.json 一一对应）；
// 界面上的一切改动都收敛为「改 plan → 防抖保存 → 服务器校验」这一条路。
import { api } from './api.js';

const listeners = new Map();
export const bus = {
  on(evt, fn) { (listeners.get(evt) || listeners.set(evt, []).get(evt)).push(fn); },
  emit(evt, ...args) { (listeners.get(evt) || []).forEach((fn) => fn(...args)); },
};

export const state = {
  assembly: null,
  plan: null,
  validate: null,       // 服务器 validate 结果 {ok, issues}
  render: null,         // 最近一次渲染结果
  rendering: false,
  selection: new Set(), // 零件名集合（plan 以名字寻址，实例不单选）
  activeFigure: null,   // 当前操作的图 id
  saveState: 'clean',   // clean | dirty | saving | saved | error
  workdir: '',
  step: '',
  homeUrl: null,        // 工作台首页（单工程模式为 null）
  llm: null,            // 大模型通道状态
  flowcharts: [],       // 本工程的流程图
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
export function mutate(fn) {
  fn(state.plan);
  state.saveState = 'dirty';
  bus.emit('plan');
  bus.emit('save-state');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flush, 600);
}

export async function flush() {
  if (state.saveState !== 'dirty') return;
  state.saveState = 'saving';
  bus.emit('save-state');
  try {
    const res = await api.savePlan(state.plan);
    state.validate = res.validate;
    state.saveState = 'saved';
    bus.emit('validate');
  } catch (err) {
    state.saveState = 'error';
    console.error('保存失败', err);
  }
  bus.emit('save-state');
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

export async function boot() {
  const data = await api.state();
  state.assembly = data.assembly;
  state.plan = data.plan;
  state.validate = data.validate;
  state.render = data.render;
  state.rendering = data.rendering;
  state.workdir = data.workdir;
  state.step = data.step;
  state.homeUrl = data.home_url ? `${data.home_url}?token=${encodeURIComponent(
    new URLSearchParams(location.search).get('token') || '')}` : null;
  state.llm = data.llm || null;
  state.flowcharts = data.flowcharts || [];
  state.structure = data.structure || null;
  state.structureJob = data.structure_job || null;
  indexStructure();
  if (state.structure?.source === 'ai') state.view.colorBy = 'structure';
  const groups = state.structure?.groups || [];
  const total = groups.reduce((n, g) => n + g.parts.length, 0);
  groups.forEach((g) => { if (total > 40 && g.parts.length > 12) state.collapsed.add(g.id); });
  state.activeFigure = data.plan?.figures?.[0]?.id || null;
  bus.emit('booted');
}
