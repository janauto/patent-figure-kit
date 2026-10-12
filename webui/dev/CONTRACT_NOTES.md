# API 契约对齐表（前端 / mock vs 真实后端）

基准：`docs/openapi.yaml`（WP1 + WP2 合并后的冻结契约）与实现（`scripts/workbench.py`、`scripts/plan_studio.py`）。
WP3 阶段前端对 mock 开发、留了「待 WP1 合并联调」清单；**WP3b 已全部对真实后端接线并由 `e2e_live.py` 验收**，
mock 也随之改成真实响应形状（`tests/test_e2e_contract_mock.py` 钉住 mock 与后端的字段一致）。

## 已对齐（真实后端验收）

| 端点 | 契约 | 前端用法 |
| --- | --- | --- |
| `GET /v1/capabilities` | `Capabilities` | 模式探测（200/401 live，404 legacy）；AI 状态、DWG / PDF 核验能力、版心预设、CNIPA 旁注 |
| `GET /v1/projects` | `{projects:[summary], workbench}`，条目只有 `id` | 首页列表（归一成 `project_id`）；AI 徽标取能力接口 |
| `GET /v1/projects/{id}` | 摘要 + `plan_etag` + `candidates{job_id,state,summary,empty_reason,url}` | 详情的一部分 |
| `GET /p/{id}/api/state` | 子应用：`assembly / plan / validate / render / version / render_job / structure / llm …` | 详情的另一部分；PUT plan 后的校验结果也从这里回读 |
| `GET /p/{id}/api/render/status` | `RenderStatus {result=已发布, version, job=本次}` | 出图终态后读权威结果；失败显示 `job` 诊断 |
| `GET /v1/jobs/{rid}` | `Job.inputs.plan_etag` | 已发布版本的输入证据（导出解锁凭据） |
| `GET/PUT /v1/projects/{id}/plan` | 体即 plan，`ETag` / `If-Match`，409 | 保存循环；409 → 顶栏「plan 冲突，请重载」 |
| `POST /v1/projects/{id}/plan/suggest` + SSE + `GET /plan/candidates` | `SuggestResult`、`Candidate`（members 只有 `{part}`） | 候选卡；「试渲染候选 k/N」进度 |
| `POST /v1/projects/{id}/plan/candidates/accept` | `If-Match`；200 新 ETag；422 `detail.unresolved`；409 | 接受后以服务端 plan / ETag 为准 |
| `POST /v1/projects/{id}/render` | 202 `{job_id, inputs}` | 出图；版本 = 作业 ID |
| `GET /v1/jobs/{id}/events` | SSE `id / event / data={stage,progress,data}`，`Last-Event-ID` / `?last_event_id=` | 全部作业进度；断线重连 |
| `POST /v1/projects/{id}/export` + `GET /v1/exports/{id}` + `/download` | `ExportResult`（items / documents / manifest / reasons / sha256 / note / download_url） | 导出屏四态（ok / skip / fail / unverified） |
| `POST /v1/projects/{id}/ai/draft-terms` | `DraftTermsResult`，`apply` 缺省 false，`ai_not_configured` 200，502 `ai_call_failed` | 外发确认 → `apply=false` 预览；写入时前端按与 `fill_blank_terms` 相同的「只填空白」规则改计划，经 `PUT /plan` + If-Match 保存（写入 = 预览） |
| `POST /v1/projects/from-example`、`POST /v1/projects` | 202 `JobRef` | 新建 / 示例时间线，随后自动起候选作业 |
| 鉴权 | Cookie `wb_token`；`?token=` 只靠它通过时回 `Deprecation` / `X-Auth-Deprecated` | 浏览器只用 Cookie；据 Deprecation 头提示 |

## 已知与契约的差异（已在前端处理，未改后端）

| # | 项 | 处理 |
| --- | --- | --- |
| 1 | 任务书写「`GET /v1/projects/{id}` 含 `render_job`」，实现里没有 | 本次作业从子应用 `api/state` / `api/render/status` 取（RenderStatus 语义） |
| 2 | 已发布结果（版本库里的 `result.json`）不带 `plan_etag` | 从 `GET /v1/jobs/{rid}` 的 `inputs.plan_etag` 补（同为后端证据） |
| 3 | 排版阶段就失败的图（`E_LAYOUT_UNSOLVABLE` / `E_LABELS_UNPLACEABLE`）没有 `failed_checks` / `fix_action` | 候选用 `qa.error_code`、出图用日志行「figure <id> 渲染失败 [E_…]」映射到白话与修复直达 |
| 4 | `PUT /v1/projects/{id}/plan` 只回 `{ok}` | 校验结果防抖回读子应用 `api/state.validate` |

## 旧版后端兼容模式（legacy）——开发用，不随产品发布

`api.js` 的 legacy 分支对接旧同步 `/api/*`，只在 `/v1/capabilities` 返回 404 时启用；导出一律锁定（修复轮 7 裁决）。
回归在 `mock_server.py --old-backend` 上跑（`verify_ui.py` 的兼容模式用例）。
