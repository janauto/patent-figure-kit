# README 数字的原始记录

README 里每个实测数字都对应这里的一份原始记录。大装配体规模基准（`scripts/bench.py --scaled`）的样例在计划校验阶段即退出，不代表出图性能，这一版不发布、也不在此存放。记录是工具的原样输出，只把本机路径换成 `<仓库>`、`<安装目录>` 这类占位符，并去掉了 token。

| README 里的数字 | 记录 | 命令 | 机型 / 版本 | 缓存与条件 | 样本 |
| --- | --- | --- | --- | --- | --- |
| 首屏动图 15.2 s（5.3 / 2.3 / 6.8） | [`../hero-run.json`](../hero-run.json) | `python3 docs/assets/capture_readme.py` | Apple M5 · macOS 27.0.1 · Python 3.9.6 · 提交 7f2d653 | 全新临时数据目录，无缓存首次运行，AI 关闭 | 单次（就是 GIF 里那一次） |
| 金路径分段表（载入、候选、首次出图、重出图、改计划后出图、导出） | [`perf-golden.json`](perf-golden.json) | `measure.py --reps 3 --out measure.json`（见下方说明） | `env` 字段：Apple M5 · 10 核 · 16 GB · macOS 27.0.1 · Python 3.9.6 · cadquery-ocp 7.7.2 · 提交 7f2d653 · 工作树无改动 | 每次都用全新数据目录和新启动的工作台进程，AI 关闭；首次出图排在候选分图之后 | 每例 3 次（`runs`），中位数在 `median` |
| 冷渲染首次出图 1.19 / 0.55 / 0.63 s | [`perf-golden-cold.json`](perf-golden-cold.json) | 同上，加 `--no-suggest` | 同上 | 同上，但跳过候选分图，几何缓存完全为空 | 每例 3 次 |
| 零件种类数 / 实例数（15/27、12/20、9/18） | [`models.json`](models.json) | 记录里的 `commands` | 提交 d54f8aa（出图代码与 7f2d653 相同）· Python 3.9.6 | 确定性结果，与缓存无关 | — |
| 验收 19/19、单元测试数 | [`acceptance.jsonl`](acceptance.jsonl) | `python3 acceptance/run_acceptance.py` | 记录头：机型、核数、内存、系统、`python3` 版本、提交号、工作树改动 | 记录头的 `load_avg_at_start` 为运行时机器负载；耗时不作对外数字 | 单次 |
| 双击安装：依赖 196 s，全程 272 s；cadquery-ocp 约 166 MB | [`install-command.jsonl`](install-command.jsonl) | 记录头的 `cmd`：`/bin/bash ./安装并启动.command --no-start --data <数据目录>` | 记录头：Apple M5 · 10 核 · 16 GB · macOS 27.0.1；脚本选用 Python 3.12.15（见日志里的 doctor 输出） | `PIP_NO_CACHE_DIR=1`，日志里 0 条 `Using cached`、90 条 `Downloading`；运行时未设置 LANG / LC_*；运行期间机器另有负载 | 单次；时长强依赖网络 |

**关于 `perf-golden*.json` 的命令**
- 这两份记录由 WP5 制作目录里的 `measure.py` 产出。
- 该脚本已移入本仓库，即 [`../../measure_readme.py`](../../measure_readme.py)。测量步骤与计时口径不变，只是改用仓库内的后端工具启动工作台，并额外记录命令行与零件数。
- 用移植后的脚本复测：`python3 docs/assets/measure_readme.py --reps 3 --out <文件>`。
- 计时是服务端作业的 `created_at → updated_at`。另有 `client_s` 字段，是客户端从提交到看到终态的时间，README 未采用。

**其他说明**
- `acceptance.jsonl` 记录头的 `dirty_files` 里，`EADME.md` 是记录脚本解析 `git status` 时截掉了首字符，实际是 `README.md`（只有文档改动）。
