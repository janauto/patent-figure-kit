// 右栏·术语页签：件号 → 中文技术名词。行序 = 附图标记发号顺序（号码由脚本发，
// 行首数字只是预演）。含参数页签：layout 只暴露 schema 允许的枚举。
import { bus, state, mutate, partByName, setSelection, flush } from './state.js';
import { api } from './api.js';
import { aiName } from './state.js';

let root;
// 与服务器 FORBIDDEN_TEXT_PATTERNS 同源的前端预警（权威判定在服务器）。
const CODE_LIKE = [/^[A-Z]{2,4}[0-9]{4,8}[-_]/, /^[0-9]{4,6}-[A-Z][0-9]{2}/, /_[0-9]+_[0-9]+$/];

function issueMap() {
  const byPointer = new Map();
  (state.validate?.issues || []).forEach((it) => {
    const m = /^\/terms\/(\d+)/.exec(it.pointer || '');
    if (m) byPointer.set(Number(m[1]), it);
  });
  return byPointer;
}

function renderTerms() {
  // 整表重渲前记住焦点：change 在失焦时触发，此刻用户往往已落到下一格，
  // 不还原焦点的话每填一格就被打断一次。
  const focused = document.activeElement;
  const keepIdx = (focused?.classList?.contains('term-input') && root.contains(focused))
    ? focused.dataset.idx : null;
  const keepPos = keepIdx !== null ? focused.selectionStart : 0;
  const terms = state.plan?.terms || [];
  const issues = issueMap();
  const rows = terms.map((t, i) => {
    const isNone = t.label === 'none';
    const codeLike = t.term && CODE_LIKE.some((re) => re.test(t.term));
    const issue = issues.get(i);
    const cls = issue?.severity === 'error' || codeLike ? ' row-err' : '';
    const qty = partByName(t.selector)?.instances;
    const ai = state.aiSuggestions?.[t.selector];
    return `<tr draggable="true" data-idx="${i}" class="${cls}">
      <td class="drag" title="拖动改发号顺序">⋮⋮</td>
      <td class="num${isNone ? ' num-off' : ''}">${i + 1}</td>
      <td class="mono sel" title="点击在 3D 中选中">${t.selector}${qty > 1 ? `<em>×${qty}</em>` : ''}</td>
      <td><input class="term-input${ai ? ` ai-${ai.confidence}` : ''}" data-idx="${i}" value="${t.term || ''}"
                 placeholder="${aiName(t.selector) ? `AI：${aiName(t.selector)}` : '中文术语，如：底座'}"
                 title="${issue ? issue.message : (ai ? `AI 起草（把握：${({ high: '高', medium: '中', low: '低' })[ai.confidence] || ai.confidence}）${ai.reason ? '：' + ai.reason : ''}` : '')}">${ai ? `<i class="ai-mark ai-${ai.confidence}" title="AI 起草">AI</i>` : ''}</td>
      <td><select class="label-sel" data-idx="${i}">
        <option value="once"${t.label !== 'all' && t.label !== 'none' ? ' selected' : ''}>标一次</option>
        <option value="all"${t.label === 'all' ? ' selected' : ''}>逐实例</option>
        <option value="none"${t.label === 'none' ? ' selected' : ''}>不标</option>
      </select></td>
      <td><button class="row-del" data-del-idx="${i}" title="删除本行">×</button></td>
    </tr>`;
  }).join('');

  root.innerHTML = `
    <div class="panel-head"><span class="stamp">03</span>术语与标记
      <span class="head-btn-group">
        <button class="btn-min" data-import-bom title="用结构 BOM 预填空白术语（不覆盖已填）">导入 BOM</button>
        <button class="btn-min" data-bulk>小件批量不标…</button>
        <button class="btn-min" data-prune title="删除术语为空的行">清理空行</button>
      </span></div>
    <input type="file" data-bom-file accept=".xlsx,.xlsm,.csv,.tsv" hidden>
    <p class="hint pad">行序即发号顺序；号码由脚本发放，此处仅预演。术语必须是中文技术名词，
    件号形态会被校验器拒绝。</p>
    <table class="terms"><thead>
      <tr><th></th><th>号</th><th>零件</th><th>术语</th><th>标注</th><th></th></tr>
    </thead><tbody>${rows}</tbody></table>`;

  root.querySelectorAll('.term-input').forEach((input) => {
    input.addEventListener('change', () => mutate((plan) => {
      plan.terms[Number(input.dataset.idx)].term = input.value.trim();
    }));
  });
  root.querySelectorAll('.label-sel').forEach((sel) => {
    sel.addEventListener('change', () => mutate((plan) => {
      plan.terms[Number(sel.dataset.idx)].label = sel.value;
    }));
  });
  root.querySelectorAll('.sel').forEach((td) => {
    td.addEventListener('click', () => {
      const idx = Number(td.parentElement.dataset.idx);
      setSelection([state.plan.terms[idx].selector].filter((s) => !/[*?\[]/.test(s)));
    });
  });
  root.querySelectorAll('.row-del').forEach((btn) => {
    btn.addEventListener('click', () => mutate((plan) => {
      plan.terms.splice(Number(btn.dataset.delIdx), 1);
    }));
  });
  root.querySelector('[data-prune]').addEventListener('click', () => {
    const empty = state.plan.terms.filter((t) => !(t.term || '').trim()).length;
    if (!empty) { window.alert('没有空术语行'); return; }
    if (!window.confirm(`删除 ${empty} 行空术语？（这些零件仍会出图，只是不标号）`)) return;
    mutate((plan) => { plan.terms = plan.terms.filter((t) => (t.term || '').trim()); });
  });
  const bomFile = root.querySelector('[data-bom-file]');
  root.querySelector('[data-import-bom]').addEventListener('click', () => bomFile.click());
  bomFile.addEventListener('change', async () => {
    const file = bomFile.files[0];
    if (!file) return;
    bomFile.value = '';
    await flush();                        // 先落盘本地改动，避免与服务器写 plan 相互覆盖
    try {
      const res = await api.importBom(file);
      state.plan = res.plan;              // 服务器改了 plan，以它为准
      state.validate = res.validate;
      state.saveState = 'saved';
      bus.emit('plan'); bus.emit('validate'); bus.emit('save-state');
      const r = res.report;
      window.alert(`BOM ${r.bom_rows} 行 · 对上 ${r.matched_parts} 种零件\n`
        + `预填术语 ${r.filled} 条（已填的 ${r.kept_human} 条未动）\n`
        + `未匹配零件 ${r.unmatched_count} 种${r.unmatched_count ? '：\n' + r.unmatched_parts.join('、') : ''}`);
    } catch (err) { window.alert('导入失败：' + err.message); }
  });
  root.querySelector('[data-bulk]').addEventListener('click', () => {
    const raw = window.prompt('最大外形尺寸小于多少毫米的零件设为「不标」？（标准件如螺钉垫片）', '8');
    const threshold = Number(raw);
    if (!raw || Number.isNaN(threshold)) return;
    mutate((plan) => {
      plan.terms.forEach((t) => {
        const p = partByName(t.selector);
        if (p && p.max_dim < threshold) t.label = 'none';
      });
    });
  });

  if (keepIdx !== null) {
    const again = root.querySelector(`.term-input[data-idx="${keepIdx}"]`);
    if (again) { again.focus(); again.setSelectionRange(keepPos, keepPos); }
  }

  // 拖拽排序：发号顺序是语义，所以做成一等交互。
  let dragFrom = null;
  root.querySelectorAll('tbody tr').forEach((tr) => {
    tr.addEventListener('dragstart', () => { dragFrom = Number(tr.dataset.idx); });
    tr.addEventListener('dragover', (e) => { e.preventDefault(); tr.classList.add('drop-hint'); });
    tr.addEventListener('dragleave', () => tr.classList.remove('drop-hint'));
    tr.addEventListener('drop', (e) => {
      e.preventDefault();
      const to = Number(tr.dataset.idx);
      if (dragFrom === null || dragFrom === to) return;
      mutate((plan) => {
        const [moved] = plan.terms.splice(dragFrom, 1);
        plan.terms.splice(to, 0, moved);
      });
    });
  });
}

function renderParams(el) {
  const layout = state.plan?.layout || {};
  const views = ['iso', 'front', 'back', 'left', 'right', 'top', 'bottom'];
  const manual = typeof layout.axis_angle === 'number';
  el.innerHTML = `
    <div class="panel-head"><span class="stamp">04</span>出图参数</div>
    <p class="hint pad">只有契约 schema 允许的旋钮。没有坐标、字高、间距——那些由渲染器反解，
    这正是 v2 防事故的方式。</p>
    <div class="form">
      <label>视角 <select data-k="view">
        ${views.map((v) => `<option${layout.view === v ? ' selected' : ''}>${v}</option>`).join('')}
      </select></label>
      <label>爆炸轴 <select data-k="explode_axis">
        ${['auto', 'x', 'y', 'z'].map((v) =>
    `<option${layout.explode_axis === v ? ' selected' : ''}>${v}</option>`).join('')}
      </select></label>
      <label>疏密 <select data-k="density">
        ${['compact', 'normal', 'loose'].map((v) =>
    `<option${layout.density === v ? ' selected' : ''}>${v}</option>`).join('')}
      </select></label>
      <label>图面角 <select data-k="angle-mode">
        <option value="auto"${manual ? '' : ' selected'}>auto·逐图求解（推荐）</option>
        <option value="manual"${manual ? ' selected' : ''}>手动指定</option>
      </select>
      <input type="number" data-k="axis_angle" min="120" max="180" step="1"
             value="${manual ? layout.axis_angle : 152}"
             style="display:${manual ? 'inline-block' : 'none'};width:5em"></label>
      <label>单图标记上限 <input type="number" data-k="max_labels_per_figure"
             min="1" max="20" value="${layout.max_labels_per_figure ?? 20}" style="width:5em"></label>
      <label class="check"><input type="checkbox" data-k="engineering_table"
             ${layout.engineering_table ? 'checked' : ''}>
        附工程明细表 <span class="tag-warn">仅内部评审，文件名带 _engineering，不得交专利代理</span></label>
    </div>`;

  el.querySelectorAll('[data-k]').forEach((ctl) => {
    ctl.addEventListener('change', () => mutate((plan) => {
      const k = ctl.dataset.k;
      if (k === 'angle-mode') {
        plan.layout.axis_angle = ctl.value === 'auto'
          ? 'auto' : Number(el.querySelector('[data-k="axis_angle"]').value);
        renderParams(el);
      } else if (k === 'axis_angle') plan.layout.axis_angle = Number(ctl.value);
      else if (k === 'max_labels_per_figure') plan.layout[k] = Number(ctl.value);
      else if (k === 'engineering_table') plan.layout[k] = ctl.checked;
      else plan.layout[k] = ctl.value;
    }));
  });
}

export function initTerms(termsEl, paramsEl) {
  root = termsEl;
  const params = () => renderParams(paramsEl);
  ['booted', 'plan', 'validate', 'ai-suggestions', 'structure'].forEach((evt) => bus.on(evt, renderTerms));
  ['booted'].forEach((evt) => bus.on(evt, params));
  bus.on('plan-layout', params);
}
