# 墨线 Web 工作台 · 前端开发（WP3）

本目录是 **WP3 界面重做**的前端开发脚手架：一个纯标准库的 mock 服务器、三个公开示例的真实
资产、静态只读 manifest，以及截图 / 性能自测脚本。**后端（WP1/WP2）尚未接线，前端对 mock 开发。**

> 文件归属（架构 §7.3）：WP3 只拥有 `webui/`。`scripts/ tests/ acceptance/ docs/ README.md`
> 一律不动。与后端接线由 WP2 之后的工作包做。

## 一、起 mock 并打开

```bash
# 正常实例（AI 已接入、DWG 可用）
python3 webui/dev/mock_server.py --port 8803
# 失败场景实例（AI 未接入、DWG 转换器不可用）
python3 webui/dev/mock_server.py --port 8804 --no-ai --no-dwg
```

浏览器打开：

- 首页 / 工程列表：`http://127.0.0.1:8803/home.html`
- 规划台：`http://127.0.0.1:8803/index.html?project=gearbox`
- 静态只读站（读 `manifest.json`，模拟 GitHub Pages）：`http://127.0.0.1:8803/home.html?static`
- 旧版后端兼容模式（见下方「legacy 的定位」）：`http://127.0.0.1:8803/p/gearbox/?stage=qa`——mock 的 `/p/{id}/`
  子应用（同步 `/api/*`、无 ETag；`api/render/status` 按 openapi `RenderStatus` 给 `result/version/job`），
  页面走 `api.js` 的真实 legacy 分支（`verify_ui.py` 的兼容模式用例即对它跑）。

### legacy 的定位：开发兼容模式，不随产品发布（修复轮 7 架构裁决）

- `api.js` 的 `legacy` 分支（页面在 `/p/{id}/`、走旧同步 `/api/*`）只为**开发期**在旧后端上看新界面，
  **不随产品发布**。真正要发布的是 `live` 路径（WP1 的 `/v1/*` 契约）。
- 兼容模式保留：浏览工程与 3D、出图、编辑术语与分图、预览与查看 QA。
- 兼容模式**导出一律锁定**：旧接口出图不冻结输入、结果没有版本 ID、打包读共享输出目录——跨页面竞态
  只能由后端关闭（Codex R3g），前端回读无法证明打包内容对应哪一版计划。导出屏只给原因与「升级到新版后端」指引，
  不凭任何本地摘要 / 结果身份解锁；顶栏显示中性提示「旧版后端兼容模式：导出需升级后端」。
- 接线（WP1 合并后）：WP1 的工作台同样在 `/p/{id}/` 挂子应用，届时需按后端能力（如 `api/state` 带
  `version` / `render_job`、根路径有 `/v1`）把这类页面切到 `live`，不能继续按路径判成兼容模式。

三个示例工程：`gearbox`（行星齿轮减速器，全套真实资产）、`bench-vise`（台虎钳，整机候选被闸门拦下的教学样例）、`ball-valve`（球阀，候选为空 → 提示手动框选）。

## 二、五屏四态怎么触发

界面是数据驱动的；为了四态截图确定（不依赖 SSE 时序），加了只读的 `?scene=` / `?upload=` /
`?state=` 开关（仅开发用）。

| 屏 | 空 | 加载 | 成功 | 失败 |
|---|---|---|---|---|
| ① 工程列表 | `home.html?scene=empty` | `home.html?scene=loading` | `home.html` | `home.html?scene=error` |
| ② 新建与上传 | `home.html?upload=idle` | `home.html?upload=running` | `home.html?upload=done` | `home.html?upload=fail` |
| ③ 规划台 | `index.html?project=ball-valve` | `…?project=gearbox&state=loading` | `…?project=gearbox` | `…?project=gearbox&state=fail` |
| ④ 出图与 QA | `…?project=bench-vise&stage=qa` | `…?project=gearbox&stage=qa&state=loading` | `…?project=gearbox&stage=qa` | `…?project=gearbox&stage=qa&state=fail` |
| ⑤ 导出 | `…?stage=export&state=empty`（QA 未过锁定） | `…?stage=export&state=packing` | `…?stage=export` | — |

补充场景：`8804` 实例的首页演示 **AI 未接入**（中性灰徽标，非红报错）；`…?stage=export` 演示
**DWG 不可用**（DWG 卡禁选 + 原因）。真实交互（拖放、载入示例、渲染、打包）走真正的异步作业 + SSE，
不需要这些开关。

## 三、设计与数据契约要点

- 令牌来自 `research/03 §四`，集中在 `webui/tokens.css`（亮色实现 + 暗色令牌就绪；图纸 /
  3D / 预览恒白）。产品名与署名只走 `webui/js/brand.js` 的 `DISPLAY_NAME="墨线"`、`BRAND="品胜 AI Lab"`。
- 界面任何地方不出现「千问办公」：接入入口写「MCP / API」，对话框措辞为「MCP 客户端（如 Claude Code、Codex）」。
- 数据契约不变：预览 SVG 的 `text[data-numeral]` 点击联动、GLB 节点名→零件名映射、`name#index` 实例标识。
- `api.js` 是适配器：`live`（§6 `/v1/*`）与 `static`（读 `manifest.json`）；无 token 不 fatal。
- 首页零 three.js；规划台进入时动态 `import('./viewer.js')`。`viewer.js` 按需渲染
  （`requestRender`）、拾取 rAF 合帧、拖转 pixelRatio 降 1。

## 四、与后端接线的检查清单（WP2 之后）

1. `api.js` 的 `base` 前缀与真实挂载路径一致（单工程 `/`，工作台 `/p/<id>/`）；token 走
   `Authorization: Bearer`（已带）。
2. 用真实 `docs/openapi.yaml` 核对 `/v1/*` 的请求/响应字段；本 mock 的契约为自拟，**差异点见
   `webui/dev/CONTRACT_NOTES.md`**，逐条对齐。
3. SSE：事件名 `stage/log/done/error`、带 `id`、可用 `Last-Event-ID` 重连——确认后端一致；
   SSE 不与 GZip 同用。
4. plan 的 `ETag` / `If-Match` / 409 冲突语义对齐（`state.js` 的 `flush` 已处理 409 → `conflict` 态）。
   出图证据只认后端：结果的输入快照 `plan_etag` + 结果 ID（`render_job_id` / `version`）；导出请求带
   `render_job_id` + `plan_etag`，由后端与该渲染的冻结输入原子比对，409 → 前端提示「计划已变更，请重新出图」
   并锁定。**不传 `sheet_preset`**（WP1 传入即 400）。
   渲染状态按 openapi `RenderStatus`：`result` = 已发布版本、`job` = 本次作业；失败作业不发布，界面显示
   `job` 的诊断并标明预览 / 导出属于上一成功版本（`api.getRenderStatus` 已按此归一，接线时把 live 的数据源
   换成 `/p/{pid}/api/render/status`）。
5. 预览 SVG 必须仍带 `data-numeral`；GLB 节点名必须仍等于零件名；渲染结果的
   `numerals.numerals[].parts` 用于点击联动。
6. 导出清单字段：`items[].status in {ok,skip,fail}` + `reason`；DWG 不可用时 `skip` + 原因；
   `manifest[]` 文件名列表；`download_url` 基于 public-url。
7. AI 起名（F9）：无通道时 `/v1/ai/draft-terms` 返回 409 `ai_not_configured`，前端进中性提示。
8. 六条鉴权路径（首页、子应用、SSE、下载、REST、MCP）都要带 token 并通过。
9. **无/失效 token**：现无 token 不 fatal（进只读/引导）。接线后真实后端返回 **401** 时，前端目前
   落到通用错误 banner——建议在 `home.js` 的 `errorState` 对 `err.status===401` 改成「需要 token，
   从终端启动日志里的完整地址进入」的引导态，而不是「连接失败」。

## 五、脚本

```bash
# 五屏四态截图 → webui/dev/screens/
python3 webui/dev/capture_screens.py          # 需要 8803 与 8804 都在跑
# 按需渲染前后对比 + 首页体积/TTI
python3 webui/dev/perf_measure.py              # 需要 8803 在跑
```

## 六、fixtures（只作 mock 数据，体积已压小）

`webui/dev/fixtures/` 里的 GLB / SVG / QA JSON / 件号表由现有 CLU 从 `examples/planetary-gearbox`
的三个公开演示 STEP 生成（`export_step_glb.py` / `render_patent_figure.py` /
`annotate_figure_sheet.py` / `plan_studio.dxf_to_svg`）。它们**只作 mock 数据**，不进确定性渲染链，
不代表真实出图性能。
