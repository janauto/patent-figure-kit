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
- `POST /v1/projects/{id}/render` `{plan_etag}` → 202 `{job_id}`
- `GET /v1/jobs/{job_id}` → `{state, stage, progress, logs, stages, inputs, result, error}`
- `GET /v1/jobs/{job_id}/events` → SSE：`stage/log/done/error`，带 `id`，支持 `Last-Event-ID`
- `DELETE /v1/jobs/{job_id}` → 取消
- `POST /v1/projects/{id}/export` `{variant, formats, dpi, sheet_preset, plan_etag}` → 202 `{export_id, job_id}`
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
