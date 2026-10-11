# API 契约差异点（WP3 自拟 vs 架构 §6）

WP1 的 `docs/openapi.yaml` 在 WP3 开发时**尚未提供**，故本 mock 的契约按架构 §6 自拟。接线时以
真实 `openapi.yaml` 为准，逐条核对下列自拟/推断点。

## 完全按 §6 实现的端点

- `POST /v1/projects`（multipart；mock 忽略文件体，直接起 import 作业）→ 202 `{project_id, job_id}`
- `POST /v1/projects/from-example` `{example: gearbox|vise|valve}` → 202 `{project_id, job_id}`
- `GET /v1/projects/{id}` → 摘要 + analyze + 候选分图状态 + `plan_etag`
- `GET /v1/projects/{id}/plan` → `{plan}` + `ETag` 头
- `PUT /v1/projects/{id}/plan`（`If-Match`）→ 200 新 `ETag` / 409 `etag_conflict`
- `POST /v1/projects/{id}/plan/suggest` `{label_standard_parts}` → 202 job
- `POST /v1/projects/{id}/render` `{plan_etag}` → 202 `{job_id, inputs}`（`inputs` = 提交时冻结的输入快照 `{plan_etag, plan_hash}`，对齐 WP1 openapi 的 `InputSnapshot`；前端以其 `plan_etag` 作结果版本证据。WP1 的渲染结果把 `plan_etag` 放在顶层，mock 放在 `result.inputs.plan_etag`，`state.js:resultEtag` 两处都认）
- `GET /v1/jobs/{job_id}` → `{state, stage, progress, logs, stages, inputs, result, error}`
- `GET /v1/jobs/{job_id}/events` → SSE：`stage/log/done/error`，带 `id`，支持 `Last-Event-ID`
- `DELETE /v1/jobs/{job_id}` → 取消
- `POST /v1/projects/{id}/export` `{variant, formats, dpi, render_job_id, plan_etag}` → 202 `{export_id, job_id, render_job_id}`；
  `render_job_id` 必须是本工程一次已发布的成功渲染，`plan_etag` 与该渲染的冻结输入比对（不是与当前计划比），
  不符 409（mock 错误码 `plan_changed` / `render_job_invalid`）；`sheet_preset` 传入即 400（与 WP1 一致）
- `GET /v1/exports/{export_id}` → 清单
- `GET /v1/exports/{export_id}/download` → zip

## 自拟 / 推断，需与后端对齐

1. **错误体 schema**：mock 用 `{"error": {"code", "message", "hint"}, "detail": "<message>"}`。
   `detail` 为兼容旧前端；错误码集合（`etag_conflict / ai_not_configured / stage_failed / not_found …`）
   是自拟，等 WP1 的错误码表。
2. **SSE 事件 data schema**：`stage` = `{stage, progress, seconds?, label?}`；`log` = `{line}`；
   `done` = `{result}`；`error` = `{code, stage, message, hint}`。字段名待对齐。
3. **job 的 `stages[]`**：mock 返回 `[{stage,label,seconds,ok}]` 作为分阶段时间线数据，§6 未规定，
   供屏②时间线用；若后端用别的形状，改 `home.js` 的 `uploadTimeline`。
4. **import 作业的阶段集**：接收/解析/网格/候选分图/试渲染（§2.1 口径）。render 作业：消隐/引线/标注/QA。
   export 作业：渲染/标注/转档/打包。阶段名仅用于展示。
5. **候选分图（F2-lite）结构**：`candidates[] = {fig_id, caption, kind, members[], status:pass|fail|pending, reason, labels, qa?}`。
   §6 未给候选的具体 schema（只在 `GET /projects/{id}` 的"候选分图状态"里），此为自拟。
   `members[]` 这里放**结构组名**（展示友好），接受建图时在前端展开成零件名——若后端直接给零件名，
   改 `candidates.js` 的 `accept()`。
6. **导出清单字段**：`items[] = {format, label, status:ok|skip|fail, reason?}` + `zip` + `manifest[]`
   + `download_url`。§6 说"每格式 ok/skip/fail + 原因"，字段名自拟。
7. **LLM 状态**：`{provider, provider_label, configured, dwg_available?}`。`dwg_available` 是自拟
   （DWG 转换器可用性），§6/§5 要求"DWG 不可用事前禁选"，放这里最省事；也可并进别的能力探测接口。
8. **AI 起名**：mock 的 `POST /v1/ai/draft-terms` 返回 `{suggestions:[{selector,term,confidence,reason}], filled}`；
   §6 写的是既有 `/ai/draft-terms`，字段沿用现有实现即可。
9. **预览 URL**：mock 用 `/v1/projects/{id}/preview/{name}.svg`；真实后端可能是 `/api/preview/{name}.svg`
   或带 job 维度。`api.js` 的 `previewUrl` 只做相对化，图里的 `svg/variants` URL 由后端给什么用什么。
10. **静态 manifest**（`manifest.json`，T0 只读站）：§6 未定义，按 codex S2-16 自拟
    `{schema, version, display_name, data_dir, llm, projects:[{id, summary, detail}]}`，资源 URL 改写成
    相对 `dev/fixtures/…`。接线时换成 WP4 静态站生成器的产物。

## 修复轮 7 对齐 WP1 openapi（`~/development/pfk-wp1/docs/openapi.yaml`，只读参考）

- **渲染结果带版本 ID**：`render_job_id` 与 `version`（= 版本目录 rid）；输入快照 `plan_etag` 在
  `result.inputs.plan_etag`（mock）或顶层 `plan_etag`（WP1），`state.js:resultEtag / resultVersion` 两处都认。
  前端的结果版本证据只用这两样（后端给的），不再有任何前端自算摘要。
- **失败的渲染是失败作业**：未全部过闸 → `E_RENDER_NOT_OK`，计划未过校验 → `E_PLAN_INVALID`；作业带
  `error` + `result`（逐图 QA / 校验问题）+ `diag`（`failed_figures / missing_artefacts / log_tail …`），
  **不发布**，已发布版本保持上一成功版本。（此前 mock 把未过闸结果当成功作业发布，已改。）
- **渲染状态语义（openapi `RenderStatus`）**：`result` = 已发布版本，`version` = 其 ID，`job` = 本次作业
  （`RenderJobStatus`）。mock 的 `/p/{id}/api/render/status` 与 `api/state`（`render` + `version` +
  `render_job`）按此返回；live 的 `api.getRenderStatus` 在 mock 阶段由 `GET /v1/jobs/{id}` + 工程详情拼出同一形状，
  **接线时改读 `/p/{pid}/api/render/status`**。
- **仍待接线对齐**：WP1 的 `GET /v1/projects/{id}` 只是摘要（无 `plan / assembly / render`），live 适配器需改从
  `/p/{pid}/api/state` 取已发布结果与本次作业；`GET /v1/exports/{id}` 返回 Job（清单在 `result` 内，键为
  「图号/版本/格式」）；页面在 `/p/{pid}/` 时的模式判定（见 README「legacy 的定位」）。

## 旧版后端兼容模式（legacy）——开发用，不随产品发布

`api.js` 的 legacy 分支对接旧同步 `/api/*`。按修复轮 7 的架构裁决，它只用于开发期浏览 / 出图 / 改术语 / 预览，
**导出一律锁定**（`api.export / getExport` 直接拒绝，导出屏给升级指引）。原因：旧接口出图不冻结输入、结果无
版本 ID、打包读共享输出目录，跨页面竞态只能由后端（输入冻结 + 导出原子校验）关闭，前端回读无法证明。
