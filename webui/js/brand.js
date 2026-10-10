// 产品名与署名的唯一出处。改名只改这里：常量 + 重跑 TTS + 重拍标题卡（见架构 §1.1）。
// 模板里一律用 data-brand / data-display-name 占位，由 applyBrand() 注入，不得硬编码中文名。
export const DISPLAY_NAME = '墨线';            // 开发期显示名（架构未裁决最终名，候选：墨线/图规/准图）
export const DISPLAY_EN = 'Inkline';           // 显示名的英文陪衬（界面副标题用）
export const BRAND = '品胜 AI Lab';            // 署名（受首次外发门禁，架构 §7.4）
export const REPO = 'patent-figure-kit';       // 仓库技术名，保持不变

// 对外绝不出现「千问办公」：接入入口一律写「MCP / API」，客户端举例用 Claude Code、Codex。
export const MCP_CLIENTS = 'MCP 客户端（如 Claude Code、Codex）';

// 把模板里的占位符替换成品牌常量；元素用 data-brand-slot 标注要填什么。
export function applyBrand(root = document) {
  const fill = {
    'display-name': DISPLAY_NAME,
    'display-en': DISPLAY_EN,
    glyph: DISPLAY_NAME[0],      // 徽标取显示名首字，改名即随动
    brand: BRAND,
    repo: REPO,
    'mcp-clients': MCP_CLIENTS,
  };
  root.querySelectorAll('[data-brand-slot]').forEach((el) => {
    const key = el.dataset.brandSlot;
    if (key in fill) el.textContent = fill[key];
  });
  // 页面标题模板：<title> 里写占位，这里替换，避免模板硬编码产品名。
  if (root === document && document.title.includes('{name}')) {
    document.title = document.title.replace('{name}', DISPLAY_NAME);
  }
}
