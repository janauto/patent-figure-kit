# API 契约对齐表（mock / 前端 vs WP1 openapi）

基准：`~/development/pfk-wp1/docs/openapi.yaml`（只读参考，WP1 分支 `01b7299` 时的版本）与 WP1 实现
（`scripts/workbench.py`、`scripts/patent_figure/jobs.py`、`export.py`）。WP3 开发时 WP1 尚未提供 openapi，
mock 起初按架构 §6 自拟；修复轮 7–8 起逐条换成契约字段。下表是**修复轮 8 的现状**：

- **已对齐**：mock 的请求 / 响应与 WP1 一致，前端按契约字段读写，`verify_ui.py` 覆盖。
- **待 WP1 合并联调**：差异已知、前端已留适配点，但必须对着真实 WP1 后端验收（接线工作包，见
  `README.md`「四、接线阶段清单」）。

## 已对齐

| 端点 | 契约（WP1） | 备注 |
| --- | --- | --- |
| `POST /v1/projects`、`POST /v1/projects/from-example` | 202 `JobRef {project_id, job_id}` | mock 忽略文件体，起 import 作业 |
| `GET /v1/projects/{id}/plan` | 响应体**就是 plan 本身**，版本在 `ETag` 头 | 修复轮 8 前 mock / 前端用 `{plan}` 包了一层，已改 |
| `PUT /v1/projects/{id}/plan` | 请求体**就是 plan 本身** + `If-Match`；200 `{ok}` + 新 `ETag` / 409 | 前端原先发 `{plan}`，在 WP1 上会把整个包写成 plan——已改；mock 收到包装体直接 400 防回归。前端把任何 409 都判成「冲突」（WP1 错误体无 code） |
| `POST /v1/projects/{id}/render` | 202 `{job_id, inputs: InputSnapshot}` | mock 的 `inputs` 只给 `plan_etag / plan_hash`（WP1 另有 `terms_hash / step_sha / revision`，前端不用） |
| 渲染结果 | 带 `render_job_id`（= 版本 rid）；输入快照 `plan_etag` | WP1 放顶层 `plan_etag`，mock 放 `result.inputs.plan_etag`，`state.js:resultEtag / resultVersion` 两处都认 |
| 失败的渲染 | 失败作业：`error {code, message, hint}` + `result`（诊断），**不发布** | `E_RENDER_NOT_OK`（未全部过闸）/ `E_PLAN_INVALID`（计划未过校验） |
| `GET /v1/jobs/{id}` | `Job {id, kind, project_id, state, stage, progress, inputs, logs, result, error, idempotency_key, committed, created_at, updated_at, last_event_id}` | 修复轮 8：作业 ID 字段是 **`id`**（不是 `job_id`）；去掉了 mock 自拟的 `stages / diag` |
| `DELETE /v1/jobs/{id}` | 取消 | |
| `POST /v1/projects/{id}/export` | 请求 `{variant, formats, dpi, render_job_id, plan_etag}`；202 `{export_id, job_id, render_job_id}`（`export_id` = 导出作业 id）；409（无可导出的成功渲染 / `plan_etag` 与该渲染冻结输入不符）；400（`sheet_preset`、未知 `variant`） | 前端发当前计划 ETag（后端与渲染冻结输入比对），409 →「计划已变更，请重新出图」 |
| `GET /v1/exports/{id}` | 返回 **Job**；`result = {export_id, render_job_id, render_plan_etag, manifest, notes, dwg_log, download_url}`，`manifest` 键为「图号/版本/格式」（版本 = 交底版 / 递交版），值 `ok / skip / fail` | 修复轮 8：完成页按此逐图展示「图号 × 版本 × 格式」矩阵 + 按格式汇总；skip 原因由前端按格式说明（pdf 在 WP2、Word 只出交底版、DWG 转换器缺失），fail 取 `notes` |
| `GET /v1/exports/{id}/download` | zip | `download_url` 可能是绝对地址（基于 public-url），前端两种都认 |
| `POST /v1/projects/{id}/ai/draft-terms` | 无请求体；服务端起草并**只填空白术语、直接写 plan**；200 `{suggestions:[{selector, term, label, confidence, reason}], filled:[selector], plan_valid, issues}`；失败 422 `{detail}` | 修复轮 8：原 mock 是 `POST /v1/ai/draft-terms {project}` 且只返回建议——已改。前端先落盘本地改动，完成后**重读 plan 与 ETag**（否则下次保存会带旧 ETag 409）；范围是全工程空白件，不再按选中筛选 |
| `GET /p/{pid}/api/render/status` | `RenderStatus {running, started, result=已发布版本, version, job=RenderJobStatus}` | mock 的 `/p/{id}/` 子应用按此返回；`api/state` 同理给 `render + version + render_job` |

## 待 WP1 合并联调

| # | 项 | 现状与适配点 |
| --- | --- | --- |
| 1 | `GET /v1/projects/{id}` | WP1 只给摘要（`version`、`rendered_at`、`figures` 计数…），没有 `plan / assembly / structure / render / render_job`。mock 返回完整详情。接线时 live 适配器从 `/p/{pid}/api/state` 取已发布结果与本次作业。 |
| 2 | 渲染状态数据源 | `api.getRenderStatus()` 已归一为 `RenderStatus` 形状；live 在 mock 阶段由 `GET /v1/jobs/{id}` + 工程详情拼出，接线时改读 `/p/{pid}/api/render/status`（WP1 的 `Job` 没有 `diag`，诊断在 RenderStatus 的 `job.diag`）。 |
| 3 | SSE 帧格式 | WP1：`id / event / data`，`data = {stage, progress, data}`——`log` 的 `data` 是字符串，`done` 的是 `result`，`error` 的是 `error` 对象或字符串。mock 是扁平的（`log {line}`、`stage {stage, progress, seconds, label}`、`done {result}`、`error {code, message, hint}`）。`api.jobEvents` 接线时按 WP1 帧解包；上传时间线的阶段名（接收 / 解析 / 网格 / 候选分图 / 试渲染）需与 WP1 prepare 作业的阶段对齐。 |
| 4 | 错误体 | WP1（FastAPI）是 `{detail}`，无错误码；mock 另给 `{error: {code, message, hint}}`。前端读 `error.message ‖ detail`，冲突按 HTTP 409 判，不依赖码。 |
| 5 | `PUT plan` 的校验结果 | WP1 只回 `{ok}`；mock 另回 `validate`。接线时校验结果改从 `/p/{pid}/api/state.validate`（或校验端点）取。 |
| 6 | `GET /v1/projects` | WP1：`{projects:[summary], workbench}`；mock 另给 `llm / data_dir`。首页的 AI 状态与数据目录接线时改从设置 / 能力接口取。 |
| 7 | AI / DWG 能力 | mock 的 `GET /v1/llm/status {provider, provider_label, configured, dwg_available}` 是自拟；WP1 的 AI 设置在 `/api/wb/settings`。接线时统一成一个能力探测。 |
| 8 | 预览 URL | WP1 的预览 / 文件地址带版本：`/api/preview/<name>?v=<rid>`；前端只做相对化、后端给什么用什么——联调时确认结果里的 URL 已带 `?v=`。 |
| 9 | 候选分图（F2-lite） | `POST /v1/projects/{id}/plan/suggest` 在 WP1 是占位（501）；候选结构 `{fig_id, caption, kind, members:[{group}/{part}], members_display, status, reason, labels}` 为 WP3 自拟，由 WP2 实现时对齐。 |
| 10 | 静态 manifest | `manifest.json`（T0 只读站）为 WP3 自拟，接线时换成 WP4 静态站生成器的产物。 |

## 旧版后端兼容模式（legacy）——开发用，不随产品发布

`api.js` 的 legacy 分支对接旧同步 `/api/*`。按修复轮 7 的架构裁决，它只用于开发期浏览 / 出图 / 改术语 / 预览，
**导出一律锁定**（`api.export / getExport` 直接拒绝，导出屏给升级指引）。原因：旧接口出图不冻结输入、结果无
版本 ID、打包读共享输出目录，跨页面竞态只能由后端（输入冻结 + 导出原子校验）关闭，前端回读无法证明。
兼容模式下读 `/api/render/status` 时：新版子应用给出 `job / version` 就按 `RenderStatus` 语义处理，
旧后端没有则按 `running / result` 判定。
