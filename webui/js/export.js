// 屏⑤ 导出：版本段控 + 格式矩阵 + 推荐组合 + 打包进度与清单。
// 递交版 = PDF / Word；DXF / DWG 标「源文件，供再编辑」，不暗示能递交；DWG 不可用时禁选。
// 四态：QA 未全过（禁用 + 指向 QA）/ 就绪 / 打包中 / 完成清单。
import { state } from './state.js';
import { api } from './api.js';
import { toast } from './main.js';

let host; let variant = 'both'; const picked = new Set(['pdf', 'docx', 'dxf']);
let dwgOk = true; let packing = false; let result = null;
const params = new URLSearchParams(location.search);
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');

const FORMATS = [
  { k: 'pdf', name: '递交版 PDF', what: '每图一页 PDF，可直接作为 CPC 递交稿附图', tag: '递交' },
  { k: 'docx', name: '交底版 Word', what: '附图 + 件号名称表 + 附图说明，交代理所', tag: '交底' },
  { k: 'dxf', name: 'DXF', what: '矢量源文件，供在 CAD 里再编辑', tag: '源文件' },
  { k: 'dwg', name: 'DWG', what: 'AutoCAD 源文件，供在 CAD 里再编辑', tag: '源文件' },
];

function gatePassed() {
  if (params.get('state') === 'empty') return false;
  const r = state.render;
  return !!(r && r.ok && r.figures?.length && r.figures.every((f) => f.pass));
}

function render() {
  const force = params.get('state');
  if (force === 'packing') packing = true;   // 四态截图：冻结打包中
  if (force === 'done' && !result) {
    result = { zip: `${state.projectId}-both-20261011.zip`, download_url: '#',
      items: FORMATS.filter((f) => f.k !== 'dwg' || dwgOk).map((f) => ({ format: f.k, label: f.name, status: 'ok' })),
      manifest: ['递交版_图1.pdf', '递交版_图2.pdf', '交底版_说明书附图.docx', 'fig1.dxf', 'fig2.dxf', '附图说明.txt', '核对清单.txt'] };
  }
  if (!gatePassed() && force !== 'done') {
    host.innerHTML = `<div class="exp-blank">
      <i class="reg reg-tl"></i><i class="reg reg-tr"></i><i class="reg reg-bl"></i><i class="reg reg-br"></i>
      <p class="stamp-fail">导出锁定</p>
      <h2>还有图未过 QA，不能导出</h2>
      <p class="muted">未过闸的图不进交付目录——这是红线。请先到「出图与 QA」按提示修改并重渲，全过后再来导出。</p>
      <button class="btn big" id="to-qa">去出图与 QA →</button></div>`;
    host.querySelector('#to-qa')?.addEventListener('click', () => document.querySelector('.stage-tab[data-stage="qa"]').click());
    return;
  }
  if (result) { renderDone(); return; }

  host.innerHTML = `
    <div class="exp-wrap">
      <section class="exp-sec">
        <h3>① 版本</h3>
        <div class="seg big">
          <button class="seg-btn ${variant === 'filing' ? 'on' : ''}" data-v="filing">递交版<span>仅图号</span></button>
          <button class="seg-btn ${variant === 'disclosure' ? 'on' : ''}" data-v="disclosure">交底版<span>图号 + 件号表</span></button>
          <button class="seg-btn ${variant === 'both' ? 'on' : ''}" data-v="both">两版都要<span>推荐</span></button>
        </div>
      </section>

      <section class="exp-sec">
        <h3>② 格式</h3>
        <div class="fmt-grid">${FORMATS.map((f) => {
          const disabled = f.k === 'dwg' && !dwgOk;
          const on = picked.has(f.k) && !disabled;
          return `<label class="fmt-card ${on ? 'on' : ''} ${disabled ? 'disabled' : ''}">
            <input type="checkbox" data-fmt="${f.k}" ${on ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
            <div class="fmt-head"><b>${f.name}</b><span class="fmt-tag ${f.tag === '源文件' ? 'src' : ''}">${f.tag}</span></div>
            <p class="fmt-what">${f.what}</p>
            ${disabled ? '<p class="fmt-why">本机未检出 DWG 转换器（ODA / LibreDWG），不可用</p>' : ''}
          </label>`;
        }).join('')}</div>
        <div class="reco">
          <span class="reco-mark">推荐组合</span>
          <b>递交版 PDF + 交底版 Word</b>
          <span class="muted">递交版只放图号，直接进 CPC 递交稿；交底版带件号表，给代理所核对。
            DXF/DWG 是给要再编辑的人的源文件，不用于递交。</span>
        </div>
      </section>

      <section class="exp-sec">
        <button class="btn primary big" id="do-pack" ${packing ? 'disabled' : ''}>
          ${packing ? '<i class="spin"></i> 打包中…' : '打包交付件'}</button>
        ${packing ? `<div class="pack-bar"><i id="pack-fill"></i></div>
          <p class="hint" id="pack-log">正在隔离目录渲染…</p>` : ''}
      </section>
    </div>`;

  host.querySelectorAll('[data-v]').forEach((b) => b.addEventListener('click', () => { variant = b.dataset.v; render(); }));
  host.querySelectorAll('[data-fmt]').forEach((c) => c.addEventListener('change', () => {
    if (c.checked) picked.add(c.dataset.fmt); else picked.delete(c.dataset.fmt); render();
  }));
  host.querySelector('#do-pack')?.addEventListener('click', doPack);
  if (force === 'packing') {                 // 冻结进度，便于截图
    const fill = host.querySelector('#pack-fill'); if (fill) fill.style.width = '62%';
    const log = host.querySelector('#pack-log'); if (log) log.textContent = 'PDF / Word / DXF / DWG 转档…';
  }
}

function renderDone() {
  const items = result.items || [];
  host.innerHTML = `<div class="exp-done">
    <p class="ok-line"><span class="stamp-pass big">完成</span>交付包已打包好。</p>
    <div class="fmt-result">${items.map((it) => `<div class="fr-row ${it.status}">
      <span class="fr-mark">${it.status === 'ok' ? '✓' : (it.status === 'skip' ? '—' : '✕')}</span>
      <b>${esc(it.label)}</b>
      <span class="muted">${it.status === 'ok' ? '已生成' : esc(it.reason || (it.status === 'skip' ? '已跳过' : '失败'))}</span>
    </div>`).join('')}</div>
    <div class="exp-zip">
      <div><b class="mono">${esc(result.zip)}</b>
        <ul class="manifest">${(result.manifest || []).map((m) => `<li class="mono">${esc(m)}</li>`).join('')}</ul></div>
      <div class="exp-zip-acts">
        <a class="btn primary" href="${api.previewUrl(result.download_url || '#')}" download>⬇ 下载交付包</a>
        <button class="btn ghost sm" id="copy-path" title="仅本机可达">复制本机路径</button>
        <button class="btn ghost sm" id="pack-again">重新打包</button>
      </div>
    </div>
    <p class="hint">包内附《核对清单.txt》，提醒人工核对说明书件号（M1 暂无对表功能）。
      路径仅本机可达，不要贴到公开的地方。</p>
  </div>`;
  host.querySelector('#pack-again')?.addEventListener('click', () => { result = null; render(); });
  host.querySelector('#copy-path')?.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(`~/墨线数据/${state.projectId}/${result.zip}`); toast('已复制本机路径（仅本机可达）'); } catch { /* */ }
  });
}

async function doPack() {
  packing = true; render();
  const fill = host.querySelector('#pack-fill');
  const log = host.querySelector('#pack-log');
  const formats = [...picked];
  try {
    const res = await api.export(state.projectId, { variant, formats, dpi: 300, sheet_preset: '170x257', plan_etag: state.planEtag });
    api.jobEvents(res.job_id, {
      stage(d) { if (fill) fill.style.width = `${(d.progress || 0) * 100}%`; if (log && d.label) log.textContent = d.label + '…'; },
      async done() {
        const manifest = await api.getExport(res.export_id);
        result = manifest; packing = false; render();
        toast('打包完成', 'ok');
      },
      fail(d) { packing = false; toast('打包失败：' + d.message, 'bad'); render(); },
    });
  } catch (err) { packing = false; toast('无法打包：' + (err.hint || err.message), 'bad'); render(); }
}

export async function mountExport(el) {
  host = el;
  try { const llm = await api.llmStatus(); dwgOk = llm.dwg_available !== false; } catch { /* 默认可用 */ }
  render();
}
