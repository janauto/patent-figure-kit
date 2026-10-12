# 部署（三级）

| 级 | 适合谁 | 入口 | 文档 |
| --- | --- | --- | --- |
| T0 在线演示 | 只想看看效果 | 浏览器打开演示站（**预计算展示；无需上传自有模型**） | [static-demo.md](static-demo.md) |
| T1 本地 | 代理师、企业 IPR，自己电脑上用 | macOS 双击「安装并启动.command」/ Windows 右键「安装并启动.ps1」/ Linux `./install.sh`；或 wheel | [local.md](local.md) |
| T2 服务器 | 团队共用、企业私有化（一租户一容器） | `docker compose up -d`（**仅 linux/amd64**） | [server.md](server.md) |
| Agent | Claude Code、Codex 等 | CLI（`pfk …`）、REST、MCP | [local.md](local.md#agent--命令行) |

- 支持矩阵与「已验证」的含义：[verified-environments.md](verified-environments.md)（由 `scripts/verified_envs.py` 生成，不手改）。
- 安装时长实测：[install-times.md](install-times.md)（同上）。
- CI 工作流与外发门禁：[ci.md](ci.md)。

共同的边界：
- **默认只在本机**：工作台默认只监听 `127.0.0.1`；监听其他地址必须显式 `--allow-remote` 并给出 `--public-url`。
- **模型不出机**：出图全在本机 / 你的服务器上完成。AI 起名是可选功能，不配置密钥时不外发任何内容；配置后外发前界面会明示外发哪些字段。
- **只收 STEP**：各 CAD 导出 STEP 的方法见 README。
- Python 3.9–3.12；macOS 11+（arm64 / x86_64）、Linux x86_64（glibc ≥ 2.35，如 Ubuntu 22.04+、Debian 12+）、Windows 10/11 x64。**不支持** Linux ARM64、Alpine、Python 3.13+（几何内核 cadquery-ocp 7.7.2 没有对应轮子）。
