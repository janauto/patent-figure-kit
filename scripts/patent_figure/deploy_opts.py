"""部署参数（WP4 · T2 服务器形态）：``--host / --allow-remote / --public-url / --trusted-proxy /
--allowed-host``，以及包在工作台 ASGI 应用最外层的「部署闸门」。

设计边界（架构 v1 §4 T2，红线 ⑤「默认只绑 127.0.0.1」）：

* **默认（不带任何部署参数）与改前逐字节同一行为**：只绑 127.0.0.1；``wrap()`` 原样返回内层应用，
  内层两处 Host 校验（workbench 主应用、plan_studio 子应用）照旧只认 127.0.0.1 / localhost；
  启动横幅照旧打印带 token 的本机地址。
* **远程访问必须显式**：监听非回环地址（容器里的 0.0.0.0）必须同时给 ``--allow-remote``，否则拒绝启动；
  ``--allow-remote`` 又必须给出至少一个对外主机名（``--public-url`` 或 ``--allowed-host``），否则远程
  请求的 Host 一律会被拒，开了也没用——直接报错而不是静默。
* **Host 允许列表**由本模块在最外层校验（回环 + public-url 的主机 + ``--allowed-host``）。校验通过后把
  scope 里的 Host 归一成 ``127.0.0.1``，内层的 DNS-rebinding 守卫因此原样通过——本模块不改内层任何代码，
  也不放宽它们；对外的判定全部集中在这里，一处可审。
* **可信代理**：给了 ``--trusted-proxy``（IP 或 CIDR，可多次）后，除回环（容器内健康检查）外只接受来自
  这些地址的连接，直连后端端口一律 403；``X-Forwarded-For / X-Forwarded-Proto`` 只在对端可信时采信。
  uvicorn 自带的代理头处理在服务器形态下关闭，由这里统一做，避免两层各自改写 client 地址。
* **下载 URL 基于 public-url**：``public_base()`` 把工作台对外拼的所有绝对地址（导出包 ``download_url``、
  MCP 地址、REST 文档地址）换成 ``--public-url``；不给时与改前相同（``http://127.0.0.1:<port>``）。
* **浏览器会话与 Agent 分离**：浏览器靠会话 Cookie ``wb_token``（public-url 为 https 时本模块给它补
  ``Secure``）；Agent 用 ``Authorization: Bearer``。反向代理对带 Bearer 的请求不自己判、不消费这个头，而是
  ``forward_auth`` 到本模块的 ``/_pfk/auth``（与后端同一组凭据、常数时间比较），有效才放行；其余请求走代理的
  Basic Auth，转发前删掉 Basic 凭据——见仓库根 ``Caddyfile``。
* **token 不进日志**：服务器形态下启动横幅只打印对外地址，不回显 token（token 由运维经环境变量
  ``WORKBENCH_TOKEN`` 提供，见 ``docs/deploy/server.md``）。

纯标准库；不 import OCP（红线 ③）。
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

#: 内层守卫认的 Host（与 workbench.py / plan_studio.py 的写死值一致）。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
#: 归一后交给内层的 Host 值。
INNER_HOST = b"127.0.0.1"
DEFAULT_BIND = "127.0.0.1"
#: 会话 Cookie 名（workbench.py 设置；本模块只在 https 下补 Secure）。
SESSION_COOKIE = b"wb_token"
HEALTH_PATH = "/healthz"
#: 反向代理的 forward_auth 用：只回答「这次请求带的 token 对不对」（204 / 401），不经业务代码。
AUTH_PATH = "/_pfk/auth"
#: 本机形态专属的「按本地路径导入」入口（workbench.py 的 /api/wb/projects 的 JSON 分支）。
LOCAL_PATH_IMPORT = "/api/wb/projects"


class DeployConfigError(ValueError):
    """部署参数组合不合法（中文消息，直接给用户看）。"""


class DeployOptions:
    """解析并校验后的部署参数。"""

    def __init__(self, bind_host: str = DEFAULT_BIND, allow_remote: bool = False,
                 public_url: Optional[str] = None,
                 trusted_proxies: Sequence[str] = (), allowed_hosts: Sequence[str] = ()) -> None:
        self.bind_host = (bind_host or DEFAULT_BIND).strip()
        self.allow_remote = bool(allow_remote)
        self.public_url = normalize_public_url(public_url) if public_url else None
        self.trusted_proxies = [_parse_network(p) for p in _split_multi(trusted_proxies)]
        hosts = [_norm_hostname(h) for h in _split_multi(allowed_hosts)]
        if self.public_url:
            hosts.insert(0, _norm_hostname(urlsplit(self.public_url).hostname or ""))
        seen: List[str] = []
        for h in hosts:
            if not h:
                raise DeployConfigError("--allowed-host 里有空的主机名")
            if h not in seen:
                seen.append(h)
        self.allowed_hosts = seen
        self.warnings: List[str] = []
        self._validate()

    # -- 判定 -------------------------------------------------------------------------
    @property
    def server_mode(self) -> bool:
        """是否偏离默认的本机形态（任何一个部署参数生效即为 True）。"""
        return bool(self.allow_remote or self.public_url or self.trusted_proxies
                    or self.allowed_hosts or not _is_loopback_bind(self.bind_host))

    @property
    def secure_cookie(self) -> bool:
        return bool(self.public_url and self.public_url.startswith("https://"))

    def _validate(self) -> None:
        if not _is_loopback_bind(self.bind_host) and not self.allow_remote:
            raise DeployConfigError(
                "监听 %s 会把工作台暴露给本机以外的网络；这必须显式确认：再加 --allow-remote，"
                "并用 --public-url 写明对外地址（见 docs/deploy/server.md）。只在本机用就去掉 --host。"
                % self.bind_host)
        if self.allow_remote and not self.allowed_hosts:
            raise DeployConfigError(
                "--allow-remote 需要至少一个对外主机名：用 --public-url https://你的域名 "
                "（或 --allowed-host 你的域名）。否则远程请求的 Host 都会被拒，开了也访问不到。")
        if self.allowed_hosts and not self.allow_remote and _is_loopback_bind(self.bind_host) \
                and not self.trusted_proxies:
            # 只绑回环却声明了对外主机名：典型是本机反向代理（同机 Caddy）转发进来——此时代理就是回环，
            # 不需要 --allow-remote；照常工作，只提示一句。
            self.warnings.append("只监听 %s：对外主机名 %s 需要由本机上的反向代理转发进来。"
                                 % (self.bind_host, "、".join(self.allowed_hosts)))
        if self.public_url and self.public_url.startswith("http://") \
                and _norm_hostname(urlsplit(self.public_url).hostname or "") not in LOOPBACK_HOSTS:
            self.warnings.append("--public-url 是明文 http：浏览器会话 Cookie 与 token 会以明文过网，"
                                 "对外部署请放在 HTTPS 反向代理之后（仓库根 Caddyfile）。")
        if self.allow_remote and not self.trusted_proxies:
            self.warnings.append("未指定 --trusted-proxy：任何能连到端口的客户端都可直连后端"
                                 "（仍需 token 与允许的 Host）。经反向代理部署时请把代理地址写进 --trusted-proxy。")

    def host_allowed(self, hostname: str) -> bool:
        h = _norm_hostname(hostname)
        return h in LOOPBACK_HOSTS or h in self.allowed_hosts

    def peer_allowed(self, peer: Optional[str]) -> bool:
        """没配可信代理时不限对端；配了则只放行回环与可信代理。"""
        if not self.trusted_proxies:
            return True
        return _is_loopback_ip(peer) or self.peer_trusted(peer)

    def peer_trusted(self, peer: Optional[str]) -> bool:
        ip = _parse_ip(peer)
        return bool(ip is not None and any(ip in net for net in self.trusted_proxies))

    def describe(self) -> Dict[str, Any]:
        return {"bind_host": self.bind_host, "allow_remote": self.allow_remote,
                "public_url": self.public_url, "allowed_hosts": list(self.allowed_hosts),
                "trusted_proxies": [str(n) for n in self.trusted_proxies],
                "secure_cookie": self.secure_cookie, "server_mode": self.server_mode}


# ----------------------------------------------------------------------------- 解析工具


def normalize_public_url(url: str) -> str:
    """``https://host[:port]``（可带结尾 /）→ 规范 origin；带路径 / 查询 / 用户信息的一律拒绝。

    工作台把页面与接口挂在根路径（``/static``、``/v1``、``/p/<id>/``），不支持挂在子路径之下。"""
    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DeployConfigError("--public-url 必须形如 https://pfk.example.com（收到 %r）" % raw)
    if parts.username or parts.password:
        raise DeployConfigError("--public-url 不能带用户名或密码")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise DeployConfigError("--public-url 只能是站点根地址（不支持子路径 %r）：工作台挂在根路径下，"
                                "请给它单独的域名或端口" % parts.path)
    try:
        port = parts.port
    except ValueError:
        raise DeployConfigError("--public-url 的端口不合法：%r" % raw)
    host = parts.hostname.lower()
    if ":" in host:                                        # IPv6 字面量
        host = "[%s]" % host
    default = {"http": 80, "https": 443}[parts.scheme]
    netloc = host if port in (None, default) else "%s:%d" % (host, port)
    return "%s://%s" % (parts.scheme, netloc)


def _split_multi(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    for v in values or ():
        out.extend(p.strip() for p in str(v).split(",") if p.strip())
    return out


def _norm_hostname(h: str) -> str:
    h = (h or "").strip().lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h


def _parse_network(spec: str):
    try:
        return ipaddress.ip_network(spec.strip(), strict=False)
    except ValueError:
        raise DeployConfigError("--trusted-proxy 需要 IP 或 CIDR（如 172.30.57.10 或 10.0.0.0/8），收到 %r"
                                % spec)


def _parse_ip(value: Optional[str]):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _is_loopback_ip(value: Optional[str]) -> bool:
    ip = _parse_ip(value)
    return bool(ip is not None and ip.is_loopback)


def _is_loopback_bind(host: str) -> bool:
    h = _norm_hostname(host)
    return h == "localhost" or _is_loopback_ip(h)


def split_host_header(value: str) -> str:
    """``host[:port]`` / ``[v6]:port`` → 主机名（小写，不含端口与方括号）。"""
    v = (value or "").strip()
    if v.startswith("["):
        end = v.find("]")
        return _norm_hostname(v[1:end] if end > 0 else v)
    return _norm_hostname(v.split(":", 1)[0])


# ----------------------------------------------------------------------------- argparse 接线


#: 容器 / compose 里用环境变量给部署参数的默认值（命令行显式给出时以命令行为准）。
#: ``--allow-remote`` 与 ``--host`` 故意**没有**环境变量：远程监听必须在启动命令里显式写出。
ENV_PUBLIC_URL = "PFK_PUBLIC_URL"
ENV_TRUSTED_PROXY = "PFK_TRUSTED_PROXY"
ENV_ALLOWED_HOSTS = "PFK_ALLOWED_HOSTS"


def add_arguments(parser) -> None:
    """给 ``workbench.py serve`` 加部署参数（全部可选；不给 = 改前的本机形态）。"""
    g = parser.add_argument_group("部署（服务器形态，见 docs/deploy/server.md）")
    g.add_argument("--host", default=DEFAULT_BIND,
                   help="监听地址（默认 127.0.0.1）。非回环地址必须同时给 --allow-remote")
    g.add_argument("--allow-remote", action="store_true",
                   help="显式允许本机以外的访问（容器内监听 0.0.0.0 时必须）；需配合 --public-url")
    g.add_argument("--public-url", default=None,
                   help="对外根地址，如 https://pfk.example.com；导出包下载地址、MCP 地址都以它为准，"
                        "其主机名自动进入允许 Host 列表（缺省读环境变量 %s）" % ENV_PUBLIC_URL)
    g.add_argument("--trusted-proxy", action="append", default=None, metavar="IP或CIDR",
                   help="可信反向代理地址（可多次 / 逗号分隔）；给了之后只接受来自它们（及回环）的连接"
                        "（缺省读环境变量 %s）" % ENV_TRUSTED_PROXY)
    g.add_argument("--allowed-host", action="append", default=None, metavar="主机名",
                   help="额外允许的 Host（可多次 / 逗号分隔；缺省读环境变量 %s）" % ENV_ALLOWED_HOSTS)


def from_args(args, environ=None) -> DeployOptions:
    """argparse 结果 → DeployOptions；参数缺省（未走 serve 子命令）时即默认本机形态。
    ``--public-url / --trusted-proxy / --allowed-host`` 未在命令行给出时取对应环境变量。"""
    env = os.environ if environ is None else environ
    public_url = getattr(args, "public_url", None) or env.get(ENV_PUBLIC_URL) or None
    proxies = getattr(args, "trusted_proxy", None)
    if proxies is None:
        proxies = [env.get(ENV_TRUSTED_PROXY, "")]
    hosts = getattr(args, "allowed_host", None)
    if hosts is None:
        hosts = [env.get(ENV_ALLOWED_HOSTS, "")]
    return DeployOptions(bind_host=getattr(args, "host", DEFAULT_BIND) or DEFAULT_BIND,
                         allow_remote=getattr(args, "allow_remote", False),
                         public_url=public_url, trusted_proxies=proxies, allowed_hosts=hosts)


def from_args_or_exit(args) -> DeployOptions:
    try:
        opts = from_args(args)
    except DeployConfigError as exc:
        print("部署参数有误：%s" % exc, file=sys.stderr)
        raise SystemExit(2)
    for w in opts.warnings:
        print("提示：%s" % w, file=sys.stderr)
    return opts


def public_base(port: int, public_url: Optional[str] = None) -> str:
    """工作台对外拼绝对地址用的根：给了 public-url 用它，否则同改前 ``http://127.0.0.1:<port>``。"""
    if public_url:
        return normalize_public_url(public_url)
    return "http://127.0.0.1:%d" % port


def uvicorn_kwargs(opts: DeployOptions, port: int) -> Dict[str, Any]:
    kw: Dict[str, Any] = {"host": opts.bind_host, "port": port, "log_level": "warning"}
    if opts.server_mode:
        # 代理头只由 DeployGuard 按 --trusted-proxy 采信；关掉 uvicorn 自带的那层（它默认信 127.0.0.1）。
        kw["proxy_headers"] = False
    return kw


def startup_banner(opts: DeployOptions, port: int, token: str, data_dir: str) -> Tuple[str, str]:
    """``(终端横幅, 浏览器要打开的地址或空串)``。本机形态与改前一致；服务器形态不回显 token。"""
    if not opts.server_mode:
        url = "http://127.0.0.1:%d/?token=%s" % (port, token)
        banner = ("\n专利附图工作台已启动：\n  %s\n数据目录：%s\nREST 文档：http://127.0.0.1:%d/docs\n"
                  "MCP 端点：http://127.0.0.1:%d/mcp（Bearer token）\n" % (url, data_dir, port, port))
        return banner, url
    base = public_base(port, opts.public_url) if opts.public_url else "http://%s:%d" % (
        opts.bind_host, port)
    if os.environ.get("WORKBENCH_TOKEN") == token:
        where = "（token 不在日志中回显；来自环境变量 WORKBENCH_TOKEN）\n"
    else:
        where = ("（token 不在日志中回显；未设置环境变量 WORKBENCH_TOKEN，已生成并保存在配置文件 "
                 "config.json 的 api_token——服务器部署请改用环境变量，见 docs/deploy/server.md）\n")
    banner = ("\n专利附图工作台已启动（服务器形态）：\n  对外地址：%s/\n  监听：%s:%d\n  数据目录：%s\n"
              "  允许的 Host：%s\n  可信代理：%s\n"
              "  浏览器首次进入：%s/?token=<WORKBENCH_TOKEN>（之后靠会话 Cookie）\n"
              "  Agent / MCP：%s/mcp，请求头 Authorization: Bearer <WORKBENCH_TOKEN>\n  %s"
              % (base, opts.bind_host, port, data_dir,
                 "、".join(dict.fromkeys(list(LOOPBACK_HOSTS) + opts.allowed_hosts)),
                 "、".join(str(n) for n in opts.trusted_proxies) or "（未指定）", base, base, where))
    return banner, ""


# ----------------------------------------------------------------------------- ASGI 闸门


def wrap(app, opts: DeployOptions, token: Optional[str] = None):
    """本机形态原样返回 ``app``（零行为变化）；服务器形态套上 :class:`DeployGuard`。
    ``token`` 只用于 ``/_pfk/auth``（反向代理用后端自己的 token 判定放行带 Bearer 的请求）。"""
    return DeployGuard(app, opts, token) if opts.server_mode else app


def _request_token_ok(headers, token: Optional[str]) -> bool:
    """与 workbench.auth_via 同一组凭据：X-Studio-Token / Bearer / 会话 Cookie；常数时间比较。"""
    if not token:
        return False
    cands = []
    for k, v in headers:
        k = k.lower()
        val = v.decode("latin-1")
        if k == b"x-studio-token":
            cands.append(val)
        elif k == b"authorization" and val[:7].lower() == "bearer ":
            cands.append(val[7:])
        elif k == b"cookie":
            for part in val.split(";"):
                name, _, cv = part.strip().partition("=")
                if name == SESSION_COOKIE.decode():
                    cands.append(cv)
    return any(c and secrets.compare_digest(c.encode("utf-8", "replace"), token.encode("utf-8"))
               for c in cands)


class DeployGuard:
    """最外层 ASGI 闸门：对端（可信代理）→ Host 允许列表 → 归一 Host → 交给内层；响应里给会话 Cookie
    补 Secure。``/healthz`` 在这里直接应答（容器健康检查用，不经内层、不需要 token、不含任何数据）。"""

    def __init__(self, app, opts: DeployOptions, token: Optional[str] = None) -> None:
        self.app, self.opts, self.token = app, opts, token

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        peer = (scope.get("client") or (None, None))[0]
        if not self.opts.peer_allowed(peer):
            return await _plain(send, scope, 403, "只接受经可信反向代理的访问")
        headers = list(scope.get("headers") or [])
        host_raw = ""
        for k, v in headers:
            if k.lower() == b"host":
                host_raw = v.decode("latin-1")
                break
        if not self.opts.host_allowed(split_host_header(host_raw)):
            return await _plain(send, scope, 403, "仅限允许的主机名访问")
        if scope["type"] == "http" and scope.get("method") == "POST" \
                and scope.get("path", "").rstrip("/") == LOCAL_PATH_IMPORT \
                and any(k.lower() == b"content-type" and v.lower().startswith(b"application/json")
                        for k, v in headers):
            # 本机形态的「按服务器本地路径导入 STEP」（JSON step_path）在服务器上等于让持 token 者读取
            # 容器里的任意文件；服务器形态只收上传的文件体。
            return await _plain(send, scope, 403, "服务器形态不支持按服务器本地路径导入 STEP，请上传文件")
        scope = dict(scope)
        if self.opts.peer_trusted(peer):
            _apply_forwarded(scope, headers, self.opts)
        scope["headers"] = [(k, INNER_HOST if k.lower() == b"host" else v) for k, v in headers]
        if not any(k.lower() == b"host" for k, _ in headers):
            scope["headers"].append((b"host", INNER_HOST))
        scope.setdefault("extensions", {})
        if scope["type"] == "http" and scope.get("path") == HEALTH_PATH \
                and scope.get("method") in ("GET", "HEAD"):
            return await _json(send, 200, {"ok": True})
        if scope["type"] == "http" and scope.get("path") == AUTH_PATH:
            ok = _request_token_ok(headers, self.token)
            return await _json(send, 204 if ok else 401, None if ok else {"detail": "token 无效"})
        if not self.opts.secure_cookie or scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_secure(message):
            if message.get("type") == "http.response.start":
                message = dict(message)
                message["headers"] = [_secure_cookie(k, v) for k, v in message.get("headers", [])]
            await send(message)

        return await self.app(scope, receive, send_secure)


def _secure_cookie(k: bytes, v: bytes):
    if k.lower() == b"set-cookie" and v.split(b"=", 1)[0].strip() == SESSION_COOKIE \
            and b"secure" not in [p.strip().lower() for p in v.split(b";")]:
        return k, v + b"; Secure"
    return k, v


def _apply_forwarded(scope: dict, headers, opts: DeployOptions) -> None:
    """对端是可信代理时采信 X-Forwarded-Proto / X-Forwarded-For（取最右一个不可信地址作客户端）。"""
    xff, proto = "", ""
    for k, v in headers:
        k = k.lower()
        if k == b"x-forwarded-for":
            xff = (xff + "," if xff else "") + v.decode("latin-1")
        elif k == b"x-forwarded-proto":
            proto = v.decode("latin-1").split(",")[0].strip().lower()
    if proto in ("http", "https"):
        scope["scheme"] = proto if scope.get("type") == "http" else {"http": "ws",
                                                                     "https": "wss"}[proto]
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        if not opts.peer_trusted(hop):
            if _parse_ip(hop) is not None:
                scope["client"] = (hop, 0)
            break


async def _plain(send, scope, status: int, text: str) -> None:
    if scope.get("type") == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = text.encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def _json(send, status: int, obj) -> None:
    body = b"" if obj is None else json.dumps(obj).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"cache-control", b"no-store"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
