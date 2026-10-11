// 屏⑤ 导出：版本段控 + 格式矩阵 + 推荐组合 + 打包进度与清单。
// 递交版 = PDF / Word；DXF / DWG 标「源文件，供再编辑」，不暗示能递交；DWG 不可用时禁选。
// 四态：QA 未全过（禁用 + 指向 QA）/ 就绪 / 打包中 / 完成清单。
// 导出按 WP1 契约闭环：请求带 render_job_id（要导出的那次成功渲染）+ plan_etag，后端与该渲染的冻结输入
// 原子比对，不符 409 →「计划已变更，请重新出图」。旧版后端兼容模式（legacy）一律锁定（修复轮 7 裁决）。
import { state, renderStale, renderStaleReason, flush, bus, normEtag, resultVersion } from './state.js';
import { api } from './api.js';
import { toast } from './main.js';
import { esc } from './esc.js';

let host; let variant = 'both'; const picked = new Set(['pdf', 'docx', 'dxf']);
let dwgOk = true; let packing = false; let result = null;
const params = new URLSearchParams(location.search);

// ---- 打包结果的版本绑定（Codex R3e #3）----
// 每次打包在提交前开一张「票」：记下失效代次、计划版本、渲染结果与其输入证据。打包完成（含迟到的回调）
// 只有在票仍与当前计划 + 当前渲染证据完全一致时才发布清单；展示 / 下载缓存结果前再核对一次。
// 失效代次 packGen：计划一改（render-stale）或出现新的渲染结果（rendered）就 +1，并作废在途打包与缓存 ZIP。
let packGen = 0; let packSeq = 0; let activePack = null;
const planKey = () => normEtag(state.planEtag) || '';
const evidenceKey = () => JSON.stringify(state.renderInputs || {});
function openTicket(id) {
  return { id, gen: packGen, plan: planKey(), editVersion: state.editVersion,
    render: state.render, evidence: evidenceKey() };
}
function ticketValid(t) {
  return !!t && t.gen === packGen && t.plan === planKey() && t.editVersion === state.editVersion
    && t.render === state.render && t.evidence === evidenceKey() && gatePassed();
}
function invalidate() {
  packGen += 1;
  result = null;                                   // 缓存 ZIP 与完成清单不得跨计划 / 渲染版本复用
  if (packing && params.get('state') !== 'packing') { packing = false; activePack = null; }   // 在途打包作废
  if (host) render();
}
bus.on('render-stale', invalidate);
bus.on('rendered', invalidate);

const FORMATS = [
  { k: 'pdf', name: '递交版 PDF', what: '每图一页 PDF，按形式要求排版的说明书附图文档（最终以受理为准）', tag: '递交' },
  { k: 'docx', name: '交底版 Word', what: '附图 + 件号名称表 + 附图说明，交代理所', tag: '交底' },
  { k: 'dxf', name: 'DXF', what: '矢量源文件，供在 CAD 里再编辑', tag: '源文件' },
  { k: 'dwg', name: 'DWG', what: 'AutoCAD 源文件，供在 CAD 里再编辑', tag: '源文件' },
];

function gatePassed() {
  if (api.isLegacy) return false;           // 兼容模式：导出恒锁定，不凭任何本地记录解锁
  if (params.get('state') === 'empty') return false;
  if (renderStale()) return false;          // 计划改过 / 无法证明版本一致 → 锁定导出直到重渲
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
  // 展示缓存结果前再核对一次：票与当前计划 / 渲染证据不符 → 丢弃（?state=done 为截图用的演示清单，不核对）
  if (result && force !== 'done' && !ticketValid(result.ticket)) result = null;
  if (api.isLegacy) { renderLegacyLocked(); return; }
  if (!gatePassed() && force !== 'done') {
    const reason = renderStaleReason();
    const h2 = {
      'no-proof': '无法证明结果对应当前计划', unsaved: '计划有改动尚未保存',
      conflict: '计划已变更，请重新出图', changed: '计划改过，结果已过期',
    }[reason] || '还有图未过 QA，不能导出';
    const body = {
      'no-proof': '这份结果缺少后端给出的版本证据（输入快照与结果 ID），不解锁导出。请到「出图与 QA」重新出图以解锁。',
      unsaved: '本地改动还没保存好（或保存失败）。先把计划存好、重新出图并过闸，再来导出。',
      conflict: '服务端核对发现：要导出的出图结果与计划版本不一致（409）。请到「出图与 QA」重新出图、过闸后再导出。',
      changed: '未过闸的图不进交付目录——这是红线。请先到「出图与 QA」重新出图并过闸再来导出。',
    }[reason] || '未过闸的图不进交付目录——这是红线。请先到「出图与 QA」按提示修改并重渲，全过后再来导出。';
    host.innerHTML = `<div class="exp-blank">
      <i class="reg reg-tl"></i><i class="reg reg-tr"></i><i class="reg reg-bl"></i><i class="reg reg-br"></i>
      <p class="stamp-fail">导出锁定</p>
      <h2>${h2}</h2>
      <p class="muted">${body}</p>
      <button class="btn big" id="to-qa">去出图与 QA →</button></div>`;
    host.querySelector('#to-qa')?.addEventListener('click', () => document.querySelector('.stage-tab[data-stage="qa"]').click());
    return;
  }
  if (result) { renderDone(); return; }

  // 最近一次出图失败时：导出的是上一成功版本（已发布版本），写明版本
  const job = state.renderJob;
  const ver = resultVersion(state.render);
  const prevNote = (job && ['failed', 'interrupted'].includes(job.state) && force !== 'done')
    ? `<div class="exp-note">最近一次出图未成功；这里导出的是<b>上一成功版本</b>${ver ? `（版本 <span class="mono">${esc(ver)}</span>${state.render?.finished_at ? ` · ${esc(state.render.finished_at)}` : ''}）` : ''}。</div>` : '';
  host.innerHTML = `
    <div class="exp-wrap">
      ${prevNote}
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
          const noDwg = f.k === 'dwg' && !dwgOk;
          const disabled = noDwg;
          const on = picked.has(f.k) && !disabled;
          const why = noDwg ? '本机未检出 DWG 转换器（ODA / LibreDWG），不可用' : '';
          return `<label class="fmt-card ${on ? 'on' : ''} ${disabled ? 'disabled' : ''}">
            <input type="checkbox" data-fmt="${f.k}" ${on ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
            <div class="fmt-head"><b>${esc(f.name)}</b><span class="fmt-tag ${f.tag === '源文件' ? 'src' : ''}">${esc(f.tag)}</span></div>
            <p class="fmt-what">${esc(f.what)}</p>
            ${why ? `<p class="fmt-why">${esc(why)}</p>` : ''}
          </label>`;
        }).join('')}</div>
        <div class="reco">
          <span class="reco-mark">推荐组合</span>
          <b>递交版 PDF + 交底版 Word</b>
          <span class="muted">递交版只放图号，按形式要求排版，供代理所用于递交稿（最终以受理为准）；交底版带件号表，给代理所核对。DXF/DWG 是给要再编辑的人的源文件，不用于递交。</span>
        </div>
      </section>

      <section class="exp-sec">
        <button class="btn primary big" id="do-pack" ${packing || api.readOnly ? 'disabled' : ''}>
          ${packing ? '<i class="spin"></i> 打包中…' : '打包附图包'}</button>
        ${api.readOnly ? '<p class="hint">静态演示站为预计算只读展示，不在浏览器里打包（需本地安装后的后端）。</p>' : ''}
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

// 旧版后端兼容模式：导出恒锁定（架构裁决，修复轮 7）。只说明原因与升级方式，不提供任何解锁路径。
function renderLegacyLocked() {
  host.innerHTML = `<div class="exp-blank">
    <i class="reg reg-tl"></i><i class="reg reg-tr"></i><i class="reg reg-bl"></i><i class="reg reg-br"></i>
    <p class="stamp-fail">导出锁定</p>
    <h2>旧版后端兼容模式不支持导出</h2>
    <p class="muted">旧版后端出图时不冻结输入、结果也没有版本 ID，无法保证打包内容就是当前计划画出来的那一版——
      所以这里一律不打包。</p>
    <div class="upgrade-guide">
      <h3>升级到新版后端后即可导出</h3>
      <ol>
        <li>换用新版墨线工作台（提供 <span class="mono">/v1</span> 版本化接口：出图冻结输入、结果带版本 ID）。</li>
        <li>从工作台首页重新进入本工程，重新出图并过闸。</li>
        <li>导出时由后端按「出图结果 ID + 计划版本」原子核对，不一致会直接拒绝。</li>
      </ol>
      <p class="hint">兼容模式下仍可：浏览工程与 3D、出图预览与查看 QA、编辑术语与分图。</p>
    </div></div>`;
}

function renderDone() {
  const items = result.items || [];
  host.innerHTML = `<div class="exp-done">
    <p class="ok-line"><span class="stamp-pass big">完成</span>附图包已打包好。</p>
    <div class="fmt-result">${items.map((it) => `<div class="fr-row ${it.status}">
      <span class="fr-mark">${it.status === 'ok' ? '✓' : (it.status === 'skip' ? '—' : '✕')}</span>
      <b>${esc(it.label)}</b>
      <span class="muted">${it.status === 'ok' ? '已生成' : esc(it.reason || (it.status === 'skip' ? '已跳过' : '失败'))}</span>
    </div>`).join('')}</div>
    <div class="exp-zip">
      <div><b class="mono">${esc(result.zip)}</b>
        <ul class="manifest">${(result.manifest || []).map((m) => `<li class="mono">${esc(m)}</li>`).join('')}</ul></div>
      <div class="exp-zip-acts">
        <a class="btn primary" href="${api.previewUrl(result.download_url || '#')}" download>⬇ 下载附图包</a>
        <button class="btn ghost sm" id="copy-path" title="仅本机可达">复制本机路径</button>
        <button class="btn ghost sm" id="pack-again">重新打包</button>
      </div>
    </div>
    <p class="hint">包内附《核对清单.txt》，提醒人工核对说明书件号（M1 暂无对表功能）。
      路径仅本机可达，不要贴到公开的地方。</p>
  </div>`;
  host.querySelector('#pack-again')?.addEventListener('click', () => { result = null; render(); });
  // 下载 / 复制路径前再核对一次：页面停留期间若计划或渲染结果变了，旧包不得被取走
  const stillValid = () => params.get('state') === 'done' || ticketValid(result?.ticket);
  const refuse = () => { result = null; toast('计划或出图结果已变化，这个包已作废——请重新出图、过闸后再打包。', 'bad'); render(); };
  host.querySelector('.exp-zip-acts a[download]')?.addEventListener('click', (e) => {
    if (!stillValid()) { e.preventDefault(); refuse(); }
  });
  host.querySelector('#copy-path')?.addEventListener('click', async () => {
    if (!stillValid()) { refuse(); return; }
    try { await navigator.clipboard.writeText(`~/墨线数据/${state.projectId}/${result.zip}`); toast('已复制本机路径（仅本机可达）'); } catch { /* */ }
  });
}

const saveBroken = () => state.saveState === 'error' || state.saveState === 'conflict';

async function doPack() {
  if (api.readOnly) { toast('静态演示站只读，不能打包', ''); return; }   // 绝不向静态服务器发写请求
  if (packing) return;                                   // 重入锁：一次只有一个打包在途
  const id = ++packSeq; activePack = id; packing = true; render();
  const mine = () => activePack === id;                  // 被 invalidate 作废后，本次打包的任何回调都不再生效
  const end = () => { if (mine()) { activePack = null; packing = false; } };
  // 提交前先把本地改动落盘，再重新核对门禁：保存失败或结果过期一律禁止提交旧版本导出请求
  await flush();
  if (!mine()) return;
  if (saveBroken()) { end(); toast('计划没能保存，未打包——请先解决保存问题。', 'bad'); render(); return; }
  if (state.editVersion > state.savedVersion || !gatePassed()) {
    end(); toast('结果已不对应当前计划或未过闸，未打包——请先到「出图与 QA」重新出图。', 'bad'); render(); return;
  }
  // 提交前开票（同步，紧挨着请求）：完成时只认这张票
  const ticket = openTicket(id);
  const late = () => {                                   // 迟到 / 已作废的完成：不发布，不覆盖当前状态
    end();
    toast('先前那次打包已作废（打包期间计划或出图结果变了），未采用——请重新打包。', 'bad');
    render();
  };
  const formats = [...picked];
  try {
    // render_job_id = 页面上这份结果的版本 ID；plan_etag = 前端认为的当前计划版本。后端把后者与该渲染的
    // 冻结输入比对（而不是与服务端当前计划比），不符 409——所以这里发的是当前计划 ETag，不是结果自带的那个。
    const res = await api.export(state.projectId, { variant, formats, dpi: 300,
      render_job_id: state.renderInputs.version, plan_etag: state.planEtag });
    if (!mine() || !ticketValid(ticket)) { late(); return; }
    api.jobEvents(res.job_id, {
      stage(d) {
        if (!mine()) return;
        const fill = host.querySelector('#pack-fill'); const log = host.querySelector('#pack-log');
        if (fill) fill.style.width = `${(d.progress || 0) * 100}%`; if (log && d.label) log.textContent = d.label + '…';
      },
      async done() {
        if (!mine() || !ticketValid(ticket)) { late(); return; }
        let manifest;
        try { manifest = await api.getExport(res.export_id); } catch (err) {
          if (mine()) { end(); toast('读取打包清单失败：' + (err.hint || err.message), 'bad'); render(); }
          return;
        }
        if (!mine() || !ticketValid(ticket)) { late(); return; }   // 取清单期间又变了
        result = { ...manifest, ticket }; end(); render();
        toast('打包完成', 'ok');
      },
      fail(d) { if (!mine()) return; end(); toast('打包失败：' + d.message, 'bad'); render(); },
    });
  } catch (err) {
    if (!mine()) return;
    end();
    if (err.status === 409) {
      // 后端原子核对不通过：结果与计划版本不一致（或该渲染不可导出）→ 锁定并要求重新出图
      state.renderInputs = { ...state.renderInputs, conflict: true };
      toast(`计划已变更，请重新出图（服务端核对：${err.message}）`, 'bad');
      bus.emit('render-stale');               // 作废缓存包、刷新 QA 提示
    } else toast('无法打包：' + (err.hint || err.message), 'bad');
    render();
  }
}

export async function mountExport(el) {
  host = el;
  try { const llm = await api.llmStatus(); dwgOk = llm.dwg_available !== false; } catch { /* 默认可用 */ }
  render();
}
