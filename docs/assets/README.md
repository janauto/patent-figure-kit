# README 用图与生成脚本

这里的图都来自真实运行，可以一条命令重拍。

| 文件 | 内容 | 怎么来的 |
| --- | --- | --- |
| `hero.gif` | 首屏 30 秒动图：空工作台 → 载入示例 → 候选分图 → 出图 → 导出附图包 | `capture_readme.py`：全新数据目录上连续走一遍金路径，CDP screencast 连续录屏；等待处定格、不快进，右上角计时取自录制日志 |
| `readme/hero-run.json` | 上面那一次运行的分段计时、机型、系统、Python 与提交号 | 同上 |
| `readme/step*.jpg` | 三步成图的静帧（时间线、候选分图、出图与 QA、导出完成页） | 同上（第三步在同一实例上补拍已发布版本） |
| `readme/feat-*.jpg`、`readme/home.jpg` | 功能总览：候选被拦、结构树、透明与爆炸、框选、台虎钳闸门与出图失败、流程图出图、工程列表 | 同上，金路径之后在同一实例上补拍 |
| `package/pdf-filing-p1.png` | 递交版 PDF 第 1 页 | 导出包原文件经 `pypdfium2` 栅格 |
| `package/docx-disclosure-p1.png` | 交底版 Word 的 Quick Look 文档预览（**非实际分页验证**：底部会露出下一张图；Word 只做结构核验，实际排版以 Word 打开为准） | macOS Quick Look（`qlmanage`）预览渲染；非 macOS 上跳过 |
| `package/dxf-source.png` | 交底版 DXF 在查看器里的样子 | `ezdxf` drawing add-on（matplotlib 后端），白底黑线 |
| `package/figure-description.txt`、`package/checklist.txt` | 导出包里的 `附图说明.txt` 与 `核对清单.txt` 原文 | 原样拷出 |
| `readme/runs/` | README 性能表、验收、安装时长与零件数的原始记录（命令、机型、版本、缓存状态、每次样本），索引见 `readme/runs/README.md` | `measure_readme.py`、`scripts/bench.py`、`acceptance/run_acceptance.py`、`安装并启动.command` 的实际运行输出，已去掉本机路径与 token |
| `screens/` | 五屏四态截图（20 张 + 补充 3 张），`screens.json` 逐张说明来源 | `webui/dev/capture_live.py` |

## 重拍

```bash
python3 docs/assets/capture_readme.py          # 全部重拍
python3 docs/assets/capture_readme.py --no-gif # 不重做动图
python3 webui/dev/capture_live.py              # 五屏四态（screens/）
python3 docs/assets/measure_readme.py --reps 3 --out docs/assets/readme/runs/perf-golden.json   # 性能表（冷渲染加 --no-suggest）
```

`capture_readme.py` 的约定：

- 用全新的临时数据目录、临时配置目录与随机 token 启动一个真实工作台（`scripts/workbench.py --no-example`），结束后删除；**不读写** `~/.patent-workbench` 与 `~/.config/patent-workbench`。
- **AI 通道显式关闭**（配置写 `provider: none`），开拍前用能力接口确认，不调用任何大模型。
- 只用三个公开演示模型（`scripts/make_demo_assemblies.py` 生成的减速器 / 台虎钳 / 球阀，与任何真实产品无关）。
- 需要 `pip install playwright`、本机 Chrome（没有时退回 Playwright 自带 Chromium）与 `ffmpeg`（做 GIF）。

`screens/x1-ai-outbound.png`（AI 外发确认）是 WP3b 端到端测试里接本机模拟大模型通道拍的，属于**演示态**，不是真实 AI 结果；README 引用时已注明。
