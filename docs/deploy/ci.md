# CI 工作流与外发门禁

| 工作流 | 触发 | 做什么 | 外发 |
| --- | --- | --- | --- |
| `ci.yml` | PR、手动 | ubuntu-22.04 / Python 3.11：`pfk doctor --profile web` + 验收（N14 = 完整 pytest，含 N10 金样）；部署文件静态检查（shellcheck、hadolint、`caddy validate`、`docker compose config`、PowerShell 解析 + BOM 检查） | 无 |
| `golden-matrix.yml` | tag `v*`、手动 | {ubuntu-22.04, macos-14, windows-2022} × {3.9, 3.11, 3.12} 验收；三平台安装脚本（装两次：首装计时 + 重复运行跳过）+ 启动冒烟；三平台 wheel 在仓库外全新虚拟环境跑验收；汇总生成 `verified-environments.md` / `install-times.md`（作为构件上传，人审后再提交） | 无 |
| `docker.yml` | 相关文件的 PR、手动 | 构建 linux/amd64 镜像；镜像检查（非 root、无密钥、OCP / 字体在、doctor）；一次性容器内跑验收（Linux / 3.11 上复核 N10）；compose 起 app + Caddy，验证直连后端被拒、服务非 root；runner 作为第二客户端经 HTTPS（校验 Caddy 证书链）跑 `scripts/deploy_smoke.py` 与 curl | GHCR 推送：仅手动勾选 **且** `vars.PFK_OUTBOUND_GATE == 'passed'` |
| `pages.yml` | 仅手动 | 生成静态站 → 资产审计 → 子路径实测 → 上传构件 | Pages 发布：仅勾选 deploy **且** `vars.PFK_OUTBOUND_GATE == 'passed'` |

外发门禁变量 `PFK_OUTBOUND_GATE` 在仓库 Settings → Variables 里设置；设为 `passed` 之前必须过架构 §7.4（产品名与署名、商标检索、资产逐项核对、用户确认）。

本地等价验证（没有 Docker 时）：

```bash
python3 -m pytest tests -q && python3 acceptance/run_acceptance.py
actionlint .github/workflows/*.yml && hadolint Dockerfile && shellcheck -s bash install.sh 安装并启动.command
PFK_SITE_ADDRESS=localhost:8443 PFK_UPSTREAM=127.0.0.1:8807 PFK_BASIC_USER=pfk PFK_BASIC_HASH='…' \
  caddy adapt --config Caddyfile --validate
# 同机反向代理冒烟：工作台 --public-url https://localhost:8443 --trusted-proxy 127.0.0.1，Caddy 用同一份 Caddyfile
WORKBENCH_TOKEN=… PFK_BASIC_PASS=… python3 scripts/deploy_smoke.py --url https://localhost:8443 --cacert <Caddy root.crt>
```
