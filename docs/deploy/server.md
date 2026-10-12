# T2 服务器部署（Docker）

> **只支持 linux/amd64。** 几何内核 cadquery-ocp 7.7.2 没有 Linux ARM 轮子：ARM 服务器（含 AWS Graviton、树莓派、Apple Silicon 上的原生 ARM 容器）不支持。镜像由 CI 构建并在容器内经 Caddy 走通「载入示例 → 候选分图 → 出图 → 导出 → 下载」（见 [ci.md](ci.md)）。

## 一租户一容器

企业私有化按**一个租户（一家客户 / 一个团队）一套 compose**部署：一个数据卷、一个 token、一组 Basic Auth 账号。不要在同一实例里混放多家客户的模型——工作台内没有租户隔离，持有 token 的人能看到这个实例里的全部工程。

## 部署

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"      # 填进 WORKBENCH_TOKEN
docker run --rm caddy:2.8.4-alpine caddy hash-password --plaintext '浏览器登录密码'   # 填进 PFK_BASIC_HASH（单引号括起）
# 填 PFK_PUBLIC_URL=https://你的域名、PFK_SITE_ADDRESS=你的域名、PFK_BASIC_USER
docker compose up -d
docker compose ps          # app 显示 healthy 后 Caddy 才启动
```

- 浏览器：打开 `https://你的域名/?token=<WORKBENCH_TOKEN>`，先过 Basic Auth，之后靠会话 Cookie（HttpOnly、SameSite=Strict、HTTPS 下 Secure），地址栏里的 token 可删掉。
- Agent / MCP：`https://你的域名/mcp`，请求头 `Authorization: Bearer <WORKBENCH_TOKEN>`；REST 同理（`/v1/*`）。

## 安全模型

| 层 | 做什么 |
| --- | --- |
| Caddy | 自动 HTTPS（域名 → ACME 证书）；带 `Authorization: Bearer …` 的请求（Agent / MCP，以及浏览器前端发的接口请求）代理不自己判、不消费这个头，先 `forward_auth` 到后端部署闸门的 `/_pfk/auth`（与后端同一组凭据、常数时间比较），有效才转发；其余请求（打开页面、静态资源、事件流）走 Basic Auth，转发前删掉 `Authorization`（Basic 凭据不进后端），后端只认会话 Cookie——代理与后端不争同一个头；`Referrer-Policy: no-referrer`（首次进入的 `?token=` 不随 Referer 外泄）；不压缩（SSE 不能被缓冲）。 |
| 部署闸门（`scripts/patent_figure/deploy_opts.py`） | `--allow-remote` 显式才监听 0.0.0.0；Host 只认回环 + `--public-url` 的主机名 + `--allowed-host`；给了 `--trusted-proxy` 后只接受 Caddy（与容器内回环健康检查）的连接，直连后端端口一律 403；`X-Forwarded-*` 只在对端可信时采信；会话 Cookie 在 https 下补 `Secure`；服务器形态禁用「按服务器本地路径导入 STEP」（只收上传）；`/healthz` 不含数据；`/_pfk/auth` 只回答凭据对不对（204 / 401）。 |
| 后端 | 所有数据路径（`/v1`、`/mcp`、`/files`、`/api/wb`、`/p/<id>/api`）要 token（Bearer 或会话 Cookie）；`?token=` 只作首次引导，命中时响应带弃用头。 |
| 容器 | 非 root（uid 10001）、`no-new-privileges`、`cap_drop: ALL`；后端端口不发布到宿主机；镜像里没有任何密钥（token 只经环境变量注入，启动日志不回显 token）。 |

替代 Basic Auth：已有 SSO 时可把 Caddyfile 里浏览器那段的 `basic_auth` 换成指向 SSO 的 `forward_auth`（如 oauth2-proxy、Authelia），保持 `header_up -Authorization`，不要动带 Bearer 的那段。

## 数据卷与保密声明

- `pfk-data`（容器内 `/data`）：工程（STEP 副本、解析结果、GLB、plan、各版本渲染产物、导出包）在 `/data/workbench`；配置（AI 通道密钥，若在界面里设置）在 `/data/config`（0700 / 0600）。**这是客户的保密资料**：按客户资料的保管规定备份与销毁；删除租户 = `docker compose down -v`（连同卷删除，不可恢复）。
- 作业记录与导出包目前**只增不删**（无 TTL / 自动清理，M2）：长期运行请定期清理或按项目归档后重建卷。
- 出图、导出全部在容器内完成，不访问外网；唯一可能的外发是可选的 AI 起名（`DEEPSEEK_API_KEY` 或界面里配置），外发前界面明示字段。
- `caddy-data`：TLS 证书与私钥。

## 参数（不用 compose 时）

```bash
patent-figure-kit --data /data/workbench --port 8790 \
  --host 0.0.0.0 --allow-remote \
  --public-url https://pfk.example.com \
  --trusted-proxy 172.30.57.10            # 反向代理地址；可多次 / 逗号分隔 / CIDR
# 环境变量等价：PFK_PUBLIC_URL、PFK_TRUSTED_PROXY、PFK_ALLOWED_HOSTS（--host 与 --allow-remote 没有环境变量，必须写在命令里）
```

同机反向代理（不用容器）：工作台保持 `--host 127.0.0.1`（不需要 `--allow-remote`），加 `--public-url https://你的域名 --trusted-proxy 127.0.0.1`，Caddy 的 `PFK_UPSTREAM=127.0.0.1:8790`。

## 排错

- `部署参数有误：监听 0.0.0.0 会把工作台暴露给…`：少了 `--allow-remote`。
- `--allow-remote 需要至少一个对外主机名`：没给 `PFK_PUBLIC_URL`。
- 浏览器 403「仅限允许的主机名访问」：`PFK_PUBLIC_URL` 的主机名与浏览器地址栏不一致。
- 浏览器 403「只接受经可信反向代理的访问」：`PFK_PROXY_IP` 与 Caddy 实际地址不一致（改了网段时三项要一起改）。
- 宿主机已有 172.30.57.0/24 网段：在 `.env` 改 `PFK_SUBNET / PFK_PROXY_IP / PFK_APP_IP`。
