// 统一 HTML 转义：外部字符串（零件名/图题/AI 建议/后端 hint 等）进模板前一律过它。
// 覆盖 & < > " '，文本节点与单双引号属性都安全。能用 textContent / .value 的地方优先用它们。
const MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => MAP[c]);
