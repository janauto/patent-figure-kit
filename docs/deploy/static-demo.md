# T0 在线演示站（静态）

> **预计算展示；无需上传自有模型。** 演示站只放三个公开演示模型（行星齿轮减速器 / 台虎钳 / 球阀，由 `scripts/make_demo_assemblies.py` 生成，与任何真实产品无关）的预计算结果：可以旋转 3D、看候选分图与闸门判词、翻看三版预览与 13 项 QA、下载导出包样例；不能上传、不能出图（只读，前端请求层拦截一切写请求）。

## 生成

```bash
python3 scripts/build_static_site.py                 # → static-site/patent-figure-kit/ + static-site/audit.json
python3 scripts/verify_static_site.py static-site    # 子路径实测（需要 Playwright + Chromium）
```

生成器做的事（全部走真实后端，与浏览器 / Agent 同一套服务）：

1. 隔离环境：临时数据目录与配置目录、随机 token、**不配置任何 AI 通道**（构建过程零外发）。
2. 对三个示例依次：载入示例 → 候选分图（逐个试渲染 + QA）→ 出图（隔离渲染、全过闸才发布）→ 导出附图包（两版 PDF / Word / DXF，逐页核验 + 封印）→ 下载 zip。
3. 取已发布版本的 GLB、三版预览 SVG、件号表、QA、候选及预览，改写成相对 URL，写版本化 manifest：`inkline-static/2` = **真实后端响应的快照**（`capabilities` = `GET /v1/capabilities`；每个工程 `project` / `plan` / `state` / `candidates` 分别是 `GET /v1/projects/{id}`、`…/plan`、`/p/{id}/api/state`、`…/plan/candidates` 的响应体），并保留旧版静态适配器读的 `detail` 等字段；版本 = 包版本 + git 提交 + 生成时间；含每个资产与源 STEP 的 sha256。
4. 只读调用 `webui/dev/build_static.py` 组装前端（相对路径 + 静态模式 meta），再做构建期处理：注入「预计算展示；无需上传自有模型 · 下载导出包样例」提示条、`samples.html` 样例页、`.nojekyll`；无 `?project=` 打开规划台时回到首页；删去 webui 源码里**提到禁用词的注释**（如「不得出现『千问办公』」这类自我提醒）。
5. **资产审计**（任一不过即退出码 1）：逐文件给出来源（webui 原样 / 构建期改写 / 公开示例真实出图 / 本工具生成）与判定；禁止 iDock、千问、本机路径、用户名、主机名、token、密钥形状的字符串；GLB 节点名只能是三个公开模型的件名；导出包逐成员扫描；manifest 登记的资产必须存在且哈希一致；不得引用后端绝对地址。明细写到站点目录之外的 `audit.json`（不发布）。

## 子路径实测（`verify_static_site.py`）

用 `python -m http.server`（与 GitHub Pages 一样**没有路由重写**）把站点挂在 `/patent-figure-kit/` 子路径下，Playwright Chromium 逐项断言：子路径根落到首页、提示文案在、三张示例卡片；每个示例 GLB 解码（零件数 = manifest 装配体零件种数、快照非空白）、预览 SVG 内联且带 `text[data-numeral]`、导出屏打包按钮禁用；样例页三个 zip 可下载、哈希与 manifest 一致、成员齐全；全程**零写请求、零后端请求**、全部同源且在子路径下、无 4xx/5xx、无控制台错误。

## 发布（受首次外发门禁）

`.github/workflows/pages.yml` **只手动触发**（`workflow_dispatch`）：默认只生成、审计、实测并把站点与截图作为构件上传供人工核对；勾选 `deploy` **且**仓库变量 `PFK_OUTBOUND_GATE=passed` 时才发布到 GitHub Pages（`https://<owner>.github.io/patent-figure-kit/`）。设这个变量之前须过架构 §7.4：用户确认产品名与署名、商标检索留证、全部资产逐项核对（`audit.json` + 截图）、用户确认外发。

已知限制：静态模式的导出屏显示「不在浏览器里打包」，样例下载入口是提示条与 `samples.html`（webui 源码归界面工作包，WP4 不改；后续可让导出屏直接读 manifest 的 `export_sample`）。
