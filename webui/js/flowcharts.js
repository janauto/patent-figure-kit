// 右栏·流程图页签：一段方法描述 →（AI 或手写）语义节点/连线 → 脚本出图（S101 步骤号由程序发放）。
// 编辑器只暴露语义：节点类型、文字、连线与「是/否」。没有坐标、尺寸、步骤号输入框。
import { api } from './api.js';
import { state } from './state.js';
import { toast } from './main.js';

let root;
let current = null;         // 当前编辑的 {id, spec}
let showJson = false;
let busy = '';

const KIND = { start: '开始', process: '处理', decision: '判断', io: '输入/输出', end: '结束' };
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

async function refresh(select) {
  const res = await api.flowcharts();
  state.flowcharts = res.flowcharts;
  if (select) current = state.flowcharts.find((f) => f.id === select) || null;
  else if (current) current = state.flowcharts.find((f) => f.id === current.id) || null;
  render();
}

function nodeRows(spec) {
  return spec.nodes.map((n, i) => `<tr>
    <td class="mono">${esc(n.id)}</td>
    <td><select data-n="${i}" data-k="kind">${Object.entries(KIND).map(([k, v]) =>
    `<option value="${k}"${n.kind === k ? ' selected' : ''}>${v}</option>`).join('')}</select></td>
    <td><input data-n="${i}" data-k="text" value="${esc(n.text)}"></td>
    <td><button class="row-del" data-ndel="${i}" title="删除节点及其连线">×</button></td></tr>`).join('');
}

function edgeRows(spec) {
  const opts = (sel) => spec.nodes.map((n) =>
    `<option value="${esc(n.id)}"${n.id === sel ? ' selected' : ''}>${esc(n.id)} · ${esc(n.text).slice(0, 8)}</option>`).join('');
  return spec.edges.map((e, i) => `<tr>
    <td><select data-e="${i}" data-k="from">${opts(e.from)}</select></td>
    <td>→</td>
    <td><select data-e="${i}" data-k="to">${opts(e.to)}</select></td>
    <td><select data-e="${i}" data-k="label">
      ${['', '是', '否'].map((l) => `<option${(e.label || '') === l ? ' selected' : ''}>${l}</option>`).join('')}</select></td>
    <td><button class="row-del" data-edel="${i}">×</button></td></tr>`).join('');
}

function render() {
  const list = state.flowcharts || [];
  const cards = list.map((f) => `
    <article class="flow-card${current?.id === f.id ? ' is-active' : ''}" data-open="${f.id}">
      <b>图${f.number}</b> <span>${esc(f.title || f.id)}</span>
      <em class="hint">${f.result?.ok ? `${f.result.steps.length} 步 ✓` : '未出图'}</em>
    </article>`).join('');
  const spec = current?.spec;
  const res = current?.result;
  const issues = (current?.issues || []).filter((i) => i.severity === 'error');
  root.innerHTML = `
    <div class="panel-head"><span class="stamp">04</span>方法流程图
      <span class="head-btn-group"><button class="btn-min" data-new>＋ 空白流程图</button></span></div>
    <div class="flow-ai">
      <textarea data-text rows="4" placeholder="粘贴一段方法步骤描述，例如：设备上电后先采集箱体温度，温度高于设定值时启动风扇降温，否则继续采集……">${esc(state.flowDraftText || '')}</textarea>
      <div class="row"><input data-title placeholder="图名（可选），如：箱体温度控制方法的流程图"
        value="${esc(state.flowDraftTitle || '')}">
      <button class="btn-ai" data-ai ${busy || !state.llm || state.llm.provider === 'none' ? 'disabled' : ''}>
        ${busy === 'ai' ? '<span class="spin dark"></span> 生成中…' : '✦ AI 生成流程图'}</button></div>
      <p class="hint">AI 只整理步骤与走向；S101 步骤号、框与连线的位置都由程序计算。图号接在结构附图之后。</p>
    </div>
    <div class="flow-list">${cards || '<p class="hint pad">还没有流程图</p>'}</div>
    ${spec ? `<div class="flow-edit">
      <div class="row"><input class="flow-title" data-spec-title value="${esc(spec.title)}" placeholder="图名">
        <label class="check-inline"><input type="checkbox" data-json ${showJson ? 'checked' : ''}>JSON</label></div>
      ${showJson ? `<textarea class="flow-json" data-jsontext rows="16">${esc(JSON.stringify(spec, null, 1))}</textarea>`
    : `<div class="ann-title">节点</div>
      <table class="flow-table"><tbody>${nodeRows(spec)}</tbody></table>
      <button class="btn-min" data-addnode>＋ 节点</button>
      <div class="ann-title">连线</div>
      <table class="flow-table"><tbody>${edgeRows(spec)}</tbody></table>
      <button class="btn-min" data-addedge>＋ 连线</button>`}
      ${issues.length ? `<ul class="issue-list">${issues.map((i) => `<li class="issue error"><b class="mono">${i.code}</b> ${esc(i.message)}${i.hint ? `<div class="qa-hint">↳ ${esc(i.hint)}</div>` : ''}</li>`).join('')}</ul>` : ''}
      <div class="row">
        <button class="btn-render small" data-render ${busy ? 'disabled' : ''}>${busy === 'render' ? '<span class="spin"></span> 出图中…' : '保存并出图'}</button>
        <button class="btn-min" data-del>删除</button></div>
      ${res?.ok ? `<div class="flow-preview" data-svg></div>
        <div class="desc-box"><div class="ann-title">步骤号对照</div>
        <p>${res.steps.map((s) => `${s.step}：${esc(s.text)}`).join('<br>')}</p>
        ${res.figure_description ? `<p class="hint">附图说明：${esc(res.figure_description)}</p>` : ''}
        ${(res.warnings || []).map((w) => `<p class="hint warn">⚠ ${esc(w)}</p>`).join('')}</div>` : ''}
    </div>` : ''}`;
  bind();
  if (res?.ok) loadSvg(res.svg);
}

async function loadSvg(src) {
  const pane = root.querySelector('[data-svg]');
  if (!pane || !src) return;
  try { pane.innerHTML = await (await fetch(api.previewUrl(src) + `&t=${Date.now()}`)).text(); } catch { /* 留空 */ }
}

function readEditor() {
  if (!current) return null;
  if (showJson) {
    try { current.spec = JSON.parse(root.querySelector('[data-jsontext]').value); } catch (e) {
      toast('JSON 格式不对：' + e.message, 'bad'); return null;
    }
  }
  const t = root.querySelector('[data-spec-title]');
  if (t) current.spec.title = t.value.trim();
  return current.spec;
}

function bind() {
  const $ = (s) => root.querySelector(s);
  root.querySelector('[data-text]')?.addEventListener('input', (e) => { state.flowDraftText = e.target.value; });
  root.querySelector('[data-title]')?.addEventListener('input', (e) => { state.flowDraftTitle = e.target.value; });
  $('[data-new]')?.addEventListener('click', async () => {
    const res = await api.newFlowchart();
    await refresh(res.id);
  });
  $('[data-ai]')?.addEventListener('click', async () => {
    const text = (state.flowDraftText || '').trim();
    if (text.length < 6) { toast('先写一段方法步骤描述', 'bad'); return; }
    busy = 'ai'; render();
    try {
      const res = await api.aiFlowchart(text, state.flowDraftTitle || '');
      state.flowcharts = res.flowcharts;
      current = res.flowcharts.find((f) => f.id === res.id) || null;
      if (current) current.issues = res.issues;
      toast(res.result?.ok ? `已生成并出图：${res.result.caption}，${res.result.steps.length} 个步骤` : 'AI 给出的流程有校验问题，请在下方修改', res.result?.ok ? 'ok' : 'bad');
    } catch (err) { toast('AI 生成失败：' + err.message, 'bad'); }
    busy = ''; render();
  });
  root.querySelectorAll('[data-open]').forEach((el) => el.addEventListener('click', () => {
    current = state.flowcharts.find((f) => f.id === el.dataset.open) || null; render();
  }));
  if (!current) return;
  $('[data-json]')?.addEventListener('change', (e) => { if (readEditor()) { showJson = e.target.checked; render(); } });
  root.querySelectorAll('[data-n]').forEach((el) => el.addEventListener('change', () => {
    current.spec.nodes[Number(el.dataset.n)][el.dataset.k] = el.value;
  }));
  root.querySelectorAll('[data-e]').forEach((el) => el.addEventListener('change', () => {
    const e = current.spec.edges[Number(el.dataset.e)];
    if (el.dataset.k === 'label' && !el.value) delete e.label; else e[el.dataset.k] = el.value;
  }));
  root.querySelectorAll('[data-ndel]').forEach((el) => el.addEventListener('click', () => {
    readEditor();
    const [gone] = current.spec.nodes.splice(Number(el.dataset.ndel), 1);
    current.spec.edges = current.spec.edges.filter((e) => e.from !== gone.id && e.to !== gone.id);
    render();
  }));
  root.querySelectorAll('[data-edel]').forEach((el) => el.addEventListener('click', () => {
    readEditor(); current.spec.edges.splice(Number(el.dataset.edel), 1); render();
  }));
  $('[data-addnode]')?.addEventListener('click', () => {
    readEditor();
    let k = current.spec.nodes.length + 1;
    while (current.spec.nodes.some((n) => n.id === `n${k}`)) k += 1;
    current.spec.nodes.push({ id: `n${k}`, kind: 'process', text: '新步骤' });
    render();
  });
  $('[data-addedge]')?.addEventListener('click', () => {
    readEditor();
    const ns = current.spec.nodes;
    if (ns.length >= 2) current.spec.edges.push({ from: ns[ns.length - 2].id, to: ns[ns.length - 1].id });
    render();
  });
  $('[data-render]')?.addEventListener('click', async () => {
    const spec = readEditor();
    if (!spec) return;
    busy = 'render'; render();
    try {
      const saved = await api.saveFlowchart(current.id, spec);
      current.issues = saved.issues;
      if (!saved.issues.some((i) => i.severity === 'error')) {
        const out = await api.renderFlowchart(current.id);
        state.flowcharts = out.flowcharts;
        const keep = current.id;
        current = state.flowcharts.find((f) => f.id === keep) || null;
        if (current) current.issues = saved.issues;
      }
    } catch (err) { toast('出图失败：' + err.message, 'bad'); }
    busy = ''; render();
  });
  $('[data-del]')?.addEventListener('click', async () => {
    if (!window.confirm(`删除流程图 ${current.id}？`)) return;
    const res = await api.deleteFlowchart(current.id);
    state.flowcharts = res.flowcharts; current = null; render();
  });
}

export function initFlowcharts(el) {
  root = el;
  render();
  refresh();
}
