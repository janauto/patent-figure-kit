# 墨线 Web 工作台 · 前端开发（WP3 / WP3b 接线）

本目录是前端的开发脚手架：一个纯标准库的 mock 服务器（按 `docs/openapi.yaml` 对齐真实后端的响应形状）、
三个公开示例的真实资产、静态只读 manifest（后端响应快照），以及回归 / 端到端 / 截图脚本。
**WP3b 起前端已接到真实后端（`scripts/workbench.py`）**：真实后端上的金路径由 `e2e_live.py` 证明。

> 文件归属（架构 §7.3）：前端只拥有 `webui/`。WP3b 没有改 `scripts/`（后端的挂载与路由形态无需调整）。

## 一、三种运行形态

| 形态 | 怎么起 | 前端判定 |
| --- | --- | --- |
| 真实后端 | `python3 scripts/workbench.py --data ~/.patent-workbench-wp3b serve --no-browser --port 8806`，浏览器开终端打印的 `/?token=…` 地址一次 | `GET /v1/capabilities` 200 → **live** |
| mock（新版） | `python3 webui/dev/mock_server.py --port 8803` → `http://127.0.0.1:8803/home.html` | 同上 → live（`/p/{id}/` 是新版子应用；开发入口 `index.html?project=` 也可） |
| mock（旧版工作台） | `python3 webui/dev/mock_server.py --port 8805 --old-backend` | `/v1/capabilities` **404** → legacy 兼容模式 |
| 静态演示站 | `python3 webui/dev/build_static.py && (cd webui/dev/static-site && python3 -m http.server 8808)` | 注入的 meta / `?static` → static（只读） |

失败场景：`--no-ai`（AI 未配置）、`--no-dwg`（DWG 转换器不可用）、`--no-pdf-check`（缺 pypdfium2 → PDF 未核验）。

### 模式与鉴权（WP3b #1 / #10）

- **按能力判定，不按路径**：首页与 `/p/{pid}/` 都先探测工作台根的 `GET /v1/capabilities`：200 → live；401 → live
  （新版后端但没会话：首页 / 规划台显示「需要 token，从终端完整地址进入」）；只有 404 → legacy。
- **live 只用会话 Cookie**（`wb_token`，HttpOnly）：不发 token 头、不存 sessionStorage、链接不带 token。地址栏带
  `?token=` 时先用它探测一次——服务端优先认有效 Cookie，只靠 query 通过时回 `Deprecation` 头，前端据此提示「已弃用」并请求
  `/?token=` 下发 Cookie，随后抹掉地址栏 token。legacy 维持旧行为（旧子应用不认 Cookie）。
- 两个根：`/v1/*`、`/files/*`、`/api/wb/*` 在工作台根；`api/state`、`api/render/status`、`api/preview/*`、`api/model.glb`、
  `api/structure*`、`api/flowcharts*` 在子应用 `/p/{pid}/`。live 无论页面在哪都按工程 ID 拼子应用前缀。

### legacy 的定位：开发兼容模式，不随产品发布（修复轮 7 架构裁决）

- `api.js` 的 `legacy` 分支只为**开发期**在旧后端上看新界面，**不随产品发布**；导出一律锁定（旧接口出图不冻结输入、
  结果无版本 ID）。新版后端一律判 live。

三个 mock 示例工程：`gearbox`（候选全过）、`bench-vise`（整机候选被拦 + QA 未过的候选，带修复直达）、
`ball-valve`（候选全被拦 → 「请框选分组」）。

## 二、五屏四态怎么触发

**真实后端上的五屏四态**由 `capture_live.py` 拍到 `docs/assets/screens/`（每张都是真实接口返回的真实状态，见同目录
`screens.json` 的说明）。下表是 **mock 上的**开发开关：为了截图确定（不依赖 SSE 时序），加了只读的 `?scene=`（只有 mock 认）/
`?upload=` / `?state=` 开关（仅开发用）。

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
- `api.js` 是适配器：`live`（openapi 的 `/v1/*` + 子应用）、`legacy`（旧后端，开发兼容）与 `static`（读 `manifest.json`，
  manifest 是后端响应快照，与 live 共用归一化函数）。
- 首页零 three.js；规划台进入时动态 `import('./viewer.js')`。`viewer.js` 按需渲染
  （`requestRender`）、拾取 rAF 合帧、拖转 pixelRatio 降 1。

## 四、接线（WP3b 已完成，逐项对真实后端验收）

| # | 项 | 前端做法 | 真实后端证据 |
| --- | --- | --- | --- |
| 1 | 能力探测与模式判定 | `api.init()` 探测 `/v1/capabilities`，200/401 → live，404 → legacy | e2e 全部用例；verify_ui 的 legacy 用例跑在 `--old-backend` |
| 2 | 资源挂载 | 首页 `/static/*`，规划台 `/p/{pid}/` 子应用的静态页 + importmap；后端无需改挂载 | e2e 每条用例断言零 4xx/5xx 资源 |
| 3 | 数据源 | 详情 = `GET /v1/projects/{id}`（摘要、plan_etag、候选状态）+ 子应用 `api/state`（装配体、已发布 render、版本、本次作业）+ `GET /plan/candidates`；渲染状态读 `api/render/status`（result / job 分开）；已发布版本的输入证据取 `GET /v1/jobs/{rid}` 的 `inputs.plan_etag` | golden_*、refresh_resume、vise_blocked_fix |
| 4 | SSE | 帧 `{stage, progress, data}` 解包；CONNECTING 由浏览器带 `Last-Event-ID` 重连，CLOSED 由前端按 `?last_event_id=` 重连；终态后回读 | sse_reconnect |
| 5 | 候选分图 | 作业 SSE「试渲染候选 k/N」→ `GET /plan/candidates`；卡片显示 status / reason / suggestion / ambiguous / 标准件 / 修复直达；接受走 `POST /plan/candidates/accept`（If-Match，422 列 unresolved，409 提示重载）；`empty_reason + guide` →「请框选分组」 | golden_*、empty_candidates、etag_409 |
| 6 | 渲染与证据 | 202 → SSE → 读 `api/render/status`，版本 = 作业 ID、证据 = 冻结输入 plan_etag；失败显示 job 诊断并标明预览 / 导出属上一成功版本；排版阶段失败从日志行取错误码给白话 | golden_*、vise_blocked_fix |
| 7 | QA | 后端 `title_zh / hint_plain / fix_action` 优先；`fix_action.target` 直达控件；`null` →「不是你的操作问题」 | vise_blocked_fix |
| 8 | 导出 | 请求带 variant / formats / dpi / sheet_preset / render_job_id / plan_etag；能力事前禁选 DWG、提示 PDF 未核验；结果按 items / documents 四态展示，`_未通过核验/` 给原因；CNIPA 旁注；保守版心提示 | golden_*、dwg_conservative_note、pdf_unverified |
| 9 | AI 起名 | 外发确认（outbound_fields / note）→ `apply=false` 预览 → `apply=true` + If-Match 写入；`ai_not_configured` 中性 | ai_configured（假 LLM 通道）、ai_not_configured |
| 10 | 鉴权 | 见上 | cookie_only |
| 11 | 示例 | `POST /v1/projects/from-example` → 时间线（准备示例 / 解析 / 候选 / 试渲染 k/N，已用秒数 + 首次耗时说明） | first_run_example、golden_* |
| 12 | 静态站 | manifest = 后端响应快照，与 live 共用归一化；`build_static.py` 生成后按 openapi 核对漂移 | `tests/test_e2e_contract_mock.py` |

## 五、脚本

```bash
# 真实后端端到端（自动起 / 复用 8806 等实例；数据与配置隔离，不写 ~/.patent-workbench）
python3 webui/dev/e2e_live.py
# 真实后端上重拍五屏四态 → docs/assets/screens/
python3 webui/dev/capture_live.py
# 前端回归（mock + 静态站；缺的服务会自动起）
python3 webui/dev/verify_ui.py
# 静态站 manifest 与 openapi 的字段核对
python3 webui/dev/build_static.py --check
# 五屏四态截图 → webui/dev/screens/
python3 webui/dev/capture_screens.py          # mock 上的开发截图 → webui/dev/screens/；需要 8803 与 8804（--no-ai --no-dwg）在跑
# 按需渲染前后对比 + 首页体积/TTI
python3 webui/dev/perf_measure.py              # 需要 8803 在跑
```

## 六、fixtures（只作 mock 数据，体积已压小）

`webui/dev/fixtures/` 里的 GLB / SVG / QA JSON / 件号表由现有 CLU 从 `examples/planetary-gearbox`
的三个公开演示 STEP 生成（`export_step_glb.py` / `render_patent_figure.py` /
`annotate_figure_sheet.py` / `plan_studio.dxf_to_svg`）。它们**只作 mock 数据**，不进确定性渲染链，
不代表真实出图性能。
