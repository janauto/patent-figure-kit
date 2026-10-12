# T1 本地安装

> 装一次：双击脚本或一行命令，几分钟（取决于网络，几何内核约 90–170 MB），可请 IT 代劳；装好后日常使用不碰命令行。
> 各平台实测时长见 [install-times.md](install-times.md)。

## 一键安装并启动

先装 **Python 3.9–3.12**（推荐 3.12）：<https://www.python.org/downloads/>。Windows 安装时勾选「Add python.exe to PATH」。

| 系统 | 操作 |
| --- | --- |
| macOS 11+ | 双击仓库里的 `安装并启动.command`。第一次若提示「无法打开，因为来自身份不明的开发者」：右键 → 打开 → 再点「打开」。若双击没有反应（从 zip 解压后丢了执行权限）：终端里 `bash "拖入安装并启动.command"`。 |
| Windows 10/11 x64 | 右键 `安装并启动.ps1` →「使用 PowerShell 运行」。若被拦（来自网络的文件 / 执行策略）：PowerShell 里先 `Unblock-File -Path .\安装并启动.ps1`，或 `powershell -ExecutionPolicy Bypass -File .\安装并启动.ps1`。 |
| Linux x86_64 | `./install.sh`（Ubuntu / Debian 若提示缺 venv：`sudo apt install python3.12-venv`） |

脚本做的事（可重复运行，第二次起跳过安装、秒开）：

1. 找 Python 3.9–3.12（3.12 → 3.9 依次试；macOS 上没装命令行工具时不会去碰 `/usr/bin/python3` 的安装弹窗；Windows 跳过应用商店的占位 `python.exe`）。
2. 检查平台（macOS ≥ 11、Linux 必须 x86_64 且 glibc ≥ 2.35、Windows 必须 x64）。
3. 在**用户目录**建独立虚拟环境（不放进仓库，免得 iCloud / 网盘同步上万个文件）：
   - macOS `~/Library/Application Support/patent-figure-kit/`
   - Linux `~/.local/share/patent-figure-kit/`
   - Windows `%LOCALAPPDATA%\patent-figure-kit\`
   - 可用环境变量 `PFK_HOME` 改位置。安装日志 `install.log`、计时 `install-timing.json` 也在这里。
4. 安装锁定依赖：`requirements-pinned.txt`（出图链路六项，与 `doctor` 同源）+ `requirements-web.txt`（工作台与导出包，含导出必需的 `pypdfium2`）。依赖清单与 Python 版本都没变时跳过。
5. 自检：`pfk paths --check`（随包资源齐全）+ `pfk doctor --profile web`（出图链路必需项 + Web / 导出依赖）。
6. 启动工作台并打开浏览器（默认端口 8790；被别的程序占用时自动换；工作台已经在运行时直接打开它，不会起第二个实例抢同一个数据目录）。

常用参数：`--no-start`（只安装与自检）、`--port 8791`、`--data 目录`（默认 `~/.patent-workbench`）、`--python 路径`、`--reinstall`、`--no-browser`；PowerShell 对应 `-NoStart -Port -Data -Python -Reinstall -NoBrowser`。

国内网络装得慢：先设 pip 镜像再运行脚本，例如 `export PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`（Windows：`$env:PIP_INDEX_URL="…"`）。

失败时脚本会给一行中文原因与修复建议，并告诉你完整日志的位置；把那几行原样发给维护者即可。

## wheel（一行命令）

PyPI **暂不发布**（M1）；wheel 随 GitHub Release 提供（受首次外发门禁）。拿到 `patent_figure_kit-<版本>-py3-none-any.whl` 后：

```bash
python3.12 -m venv ~/.pfk-venv
~/.pfk-venv/bin/python -m pip install -U pip            # cadquery-ocp 的 manylinux_2_35 标签需要新版 pip
~/.pfk-venv/bin/python -m pip install "patent_figure_kit-0.1.0-py3-none-any.whl[web]"
~/.pfk-venv/bin/pfk doctor --profile web
~/.pfk-venv/bin/patent-figure-kit                         # 启动工作台
```

wheel 把仓库树（`scripts/ webui/ schemas/ examples/ references/ acceptance/ tests/ docs/openapi.yaml`）原样装进包目录，所以 `pfk acceptance` 能在仓库外的全新环境里跑完整验收（含 N10 金样）。`pfk paths` 打印资源位置。

字体：仓库**不打包**任何字体（许可）。DXF 只记录字体名（`simfang.ttf` / `txt.shx`），不受影响；PNG / PDF 预览里的中文用系统字体：macOS 宋体、Linux `fonts-noto-cjk`（`sudo apt install fonts-noto-cjk`）、Windows 由 ezdxf 在系统字体里找 `simfang.ttf`（仿宋）。`pfk paths` 会报告找到的字体。

## Agent / 命令行

```bash
pfk doctor                     # 环境自检
pfk analyze model.stp -o assembly.json
pfk render figure-plan.json --assembly assembly.json -o out/
pfk acceptance                 # 自动验收（19 个节点，约 10 分钟）
pfk start --port 8790          # 工作台（REST /v1、MCP /mcp，Bearer token）
```

仓库检出里等价写法：`python3 scripts/pfk_cli.py …`。命令与退出码的约定见 `docs/impl-contract.md` §6、`SKILL.md`。
