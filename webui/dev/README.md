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

## 四、接线阶段清单（WP1 合并后，由接线工作包验收）

前端对 mock 已按 WP1 契约实现（逐条见 `CONTRACT_NOTES.md`「已对齐」）；下面这些**只能对着真实 WP1 后端验收**，
不在 WP3 范围内关闭：

1. **按后端能力判定 live，不按路径**。WP1 的工作台同样在 `/p/{pid}/` 挂子应用；现在 `api.js` 把 `/p/{pid}/`
   一律判成兼容模式（legacy）。接线时改为能力探测（例如 `api/state` 带 `version / render_job`、根路径有
   `/v1/*`）→ live；只有真的旧后端才进兼容模式（导出恒锁定）。
2. **数据来源**：已发布结果、版本与本次作业从 `/p/{pid}/api/state`（`render + version + render_job`）取；
   渲染状态从 `/p/{pid}/api/render/status`（`RenderStatus`：`result` = 已发布、`job` = 本次）取——
   `api.getRenderStatus()` 已按此归一，只需换数据源。WP1 的 `GET /v1/projects/{id}` 只是摘要。
   作业、导出、plan 的读写走根路径 `/v1/*`（`api.js` 的 `base` 需区分子应用前缀与根路径）。
3. **SSE 帧**：按 WP1 的 `data = {stage, progress, data}` 解包（见 `CONTRACT_NOTES.md` 待联调 #3），确认
   `Last-Event-ID` 重放、终态事件（`done` / `error`）与刷新恢复（`resumeRender()`）在真实后端上成立；SSE 不与 GZip 同用。
4. **Cookie 鉴权**：会话 Cookie 下 SSE（`EventSource` 不能带自定义头）、预览 SVG（`?v=<rid>`）、导出下载、
   REST 与 MCP 六条路径都要通过；首页地址栏 token 的清理（`tidyToken`）在子应用认 Cookie 后生效，
   届时首页那条 Cookie 能力探测的 401 应消失。无 / 失效 token 返回 401 时，`home.js` 的错误态应改为
   「需要 token，从终端启动日志里的完整地址进入」的引导，而不是「连接失败」。
5. **失败 / 取消 / 断线恢复**：渲染失败（`E_RENDER_NOT_OK` / `E_PLAN_INVALID`）显示本次作业诊断、预览与导出
   仍属上一成功版本；取消（`DELETE /v1/jobs/{id}` 后的 `error` 终态）；断线后 `GET /v1/jobs/{id}` 校准；
   后端重启后作业丢失（404）时界面收尾不卡「渲染中」；刷新 / 重进时接上 running 作业、期间禁用再次渲染。
6. **导出闭环**：请求带 `render_job_id` + 当前计划 `plan_etag`，409 时界面锁定并提示重新出图；完成页按
   `Job.result.manifest`（「图号/版本/格式」）逐图展示；下载 zip 内容与出图版本一致。
7. **数据契约不变**：预览 SVG 仍带 `text[data-numeral]`；GLB 节点名仍等于零件名；渲染结果的
   `numerals.numerals[].parts` 用于点击联动。

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
