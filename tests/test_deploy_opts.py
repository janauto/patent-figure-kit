"""WP4 部署参数：默认零行为变化；远程访问须显式；Host 允许列表；可信代理；public-url 下载地址；
https 下会话 Cookie 带 Secure；服务器形态不回显 token。

端到端部分（子应用 /p/<id>/ 的 Host 归一 + Cookie 鉴权）需要 cadquery-ocp；其余纯 ASGI。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import deploy_opts as D  # noqa: E402

try:
    import OCP  # noqa: F401
    _HAS_OCP = True
except Exception:  # pragma: no cover
    _HAS_OCP = False

TOKEN = "t" * 32


# ----------------------------------------------------------------- 参数校验


def test_default_is_local_mode_and_wrap_is_identity():
    opts = D.DeployOptions()
    assert not opts.server_mode and not opts.secure_cookie
    app = object()
    assert D.wrap(app, opts) is app                       # 本机形态：原样返回内层应用
    assert D.public_base(8790) == "http://127.0.0.1:8790"
    kw = D.uvicorn_kwargs(opts, 8790)
    assert kw == {"host": "127.0.0.1", "port": 8790, "log_level": "warning"}
    banner, url = D.startup_banner(opts, 8790, TOKEN, "/data")
    assert url == "http://127.0.0.1:8790/?token=" + TOKEN and url in banner


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_non_loopback_bind_requires_allow_remote(host):
    with pytest.raises(D.DeployConfigError, match="--allow-remote"):
        D.DeployOptions(bind_host=host, public_url="https://pfk.example.com")


def test_allow_remote_requires_a_public_hostname():
    with pytest.raises(D.DeployConfigError, match="--public-url"):
        D.DeployOptions(bind_host="0.0.0.0", allow_remote=True)
    ok = D.DeployOptions(bind_host="0.0.0.0", allow_remote=True, allowed_hosts=["pfk.lan"])
    assert ok.server_mode and ok.allowed_hosts == ["pfk.lan"]


@pytest.mark.parametrize("raw,norm", [
    ("https://PFK.example.com/", "https://pfk.example.com"),
    ("https://pfk.example.com:443", "https://pfk.example.com"),
    ("http://pfk.example.com:8080", "http://pfk.example.com:8080"),
    ("https://[::1]:8443", "https://[::1]:8443"),
])
def test_public_url_normalised(raw, norm):
    assert D.normalize_public_url(raw) == norm


@pytest.mark.parametrize("bad", ["pfk.example.com", "ftp://x.y", "https://x.y/pfk",
                                 "https://u:p@x.y", "https://x.y/?a=1", "https://x.y:99999"])
def test_public_url_rejected(bad):
    with pytest.raises(D.DeployConfigError):
        D.normalize_public_url(bad)


def test_trusted_proxy_parsing_and_peer_rules():
    opts = D.DeployOptions(bind_host="0.0.0.0", allow_remote=True,
                           public_url="https://pfk.example.com",
                           trusted_proxies=["172.30.57.10", "10.0.0.0/8,192.168.9.1"])
    assert [str(n) for n in opts.trusted_proxies] == ["172.30.57.10/32", "10.0.0.0/8",
                                                      "192.168.9.1/32"]
    assert opts.peer_allowed("127.0.0.1")                 # 容器内健康检查
    assert opts.peer_allowed("172.30.57.10") and opts.peer_allowed("10.2.3.4")
    assert not opts.peer_allowed("172.30.57.11") and not opts.peer_allowed("testclient")
    with pytest.raises(D.DeployConfigError, match="CIDR"):
        D.DeployOptions(trusted_proxies=["caddy"])


def test_warnings_for_plain_http_and_missing_proxy():
    opts = D.DeployOptions(bind_host="0.0.0.0", allow_remote=True,
                           public_url="http://pfk.example.com")
    joined = " ".join(opts.warnings)
    assert "明文" in joined and "--trusted-proxy" in joined


def test_server_banner_never_echoes_the_token():
    opts = D.DeployOptions(bind_host="0.0.0.0", allow_remote=True,
                           public_url="https://pfk.example.com", trusted_proxies=["172.30.57.10"])
    banner, url = D.startup_banner(opts, 8790, TOKEN, "/data")
    assert url == "" and TOKEN not in banner
    assert "https://pfk.example.com/mcp" in banner and "WORKBENCH_TOKEN" in banner


def test_main_refuses_remote_bind_without_allow_remote(tmp_path, capsys):
    pytest.importorskip("fastapi")
    import workbench as W
    with pytest.raises(SystemExit) as exc:
        W.main(["--data", str(tmp_path / "d"), "serve", "--host", "0.0.0.0", "--no-browser",
                "--no-example"])
    assert exc.value.code == 2
    assert "--allow-remote" in capsys.readouterr().err
    assert not (tmp_path / "d").exists()                  # 校验在建数据目录之前


# ----------------------------------------------------------------- ASGI 闸门（不依赖工作台）


def _guard(**kw):
    opts = D.DeployOptions(**{**dict(bind_host="0.0.0.0", allow_remote=True,
                                     public_url="https://pfk.example.com",
                                     trusted_proxies=["172.30.57.10"]), **kw})
    return lambda inner: D.DeployGuard(inner, opts)


def _run(guard_factory, **call_kw):
    holder = {}

    async def inner(scope, receive, send):
        holder.update(scope)
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"set-cookie", b"wb_token=abc; HttpOnly; Path=/; SameSite=strict"),
                                (b"set-cookie", b"other=1; Path=/")]})
        await send({"type": "http.response.body", "body": b"ok"})

    app = guard_factory(inner)
    msgs = []
    path = call_kw.get("path", "/")
    host = call_kw.get("host", "pfk.example.com")
    hdrs = ([(b"host", host.encode())] if host is not None else []) + \
        [(k.lower().encode(), v.encode()) for k, v in call_kw.get("headers", ())]
    scope = {"type": "http", "method": call_kw.get("method", "GET"), "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": hdrs,
             "client": call_kw.get("client", ("172.30.57.10", 1234)), "scheme": "http",
             "server": ("0.0.0.0", 8790), "asgi": {"version": "3.0"}, "http_version": "1.1"}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        msgs.append(m)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in msgs if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in msgs if m["type"] == "http.response.body")
    return start["status"], start.get("headers", []), body, holder


def test_guard_host_allowlist_and_normalisation():
    g = _guard()
    st, _, _, inner = _run(g, host="pfk.example.com:443")
    assert st == 200
    assert dict(inner["headers"])[b"host"] == b"127.0.0.1"   # 内层守卫看到的是回环 Host
    assert _run(g, host="evil.example")[0] == 403
    assert _run(g, host="pfk.example.com.evil.example")[0] == 403
    assert _run(g, host=None)[0] == 403                      # 无 Host 头
    assert _run(g, host="localhost:8790", client=("127.0.0.1", 9))[0] == 200


def test_guard_rejects_untrusted_peers_when_proxy_is_configured():
    g = _guard()
    st, _, body, inner = _run(g, client=("172.30.57.99", 5))
    assert st == 403 and not inner and "可信反向代理" in body.decode("utf-8")
    assert _run(g, client=("127.0.0.1", 5))[0] == 200        # 容器内健康检查走回环
    # 未配置可信代理：不限对端（仍受 Host 与 token 约束）
    g2 = _guard(trusted_proxies=[])
    assert _run(g2, client=("203.0.113.9", 5))[0] == 200


def test_guard_forwarded_headers_only_from_trusted_peers():
    g = _guard()
    _, _, _, inner = _run(g, headers=[("X-Forwarded-For", "198.51.100.7, 172.30.57.10"),
                                      ("X-Forwarded-Proto", "https")])
    assert inner["client"][0] == "198.51.100.7" and inner["scheme"] == "https"
    g2 = _guard(trusted_proxies=[])
    _, _, _, inner2 = _run(g2, client=("203.0.113.9", 5),
                           headers=[("X-Forwarded-For", "10.9.9.9"), ("X-Forwarded-Proto", "https")])
    assert inner2["client"][0] == "203.0.113.9" and inner2["scheme"] == "http"


def test_guard_marks_only_the_session_cookie_secure_under_https():
    _, hdrs, _, _ = _run(_guard())
    cookies = [v for k, v in hdrs if k == b"set-cookie"]
    assert cookies[0].endswith(b"; Secure") and not cookies[1].endswith(b"; Secure")
    _, hdrs2, _, _ = _run(_guard(public_url="http://pfk.example.com"))
    assert not any(v.endswith(b"; Secure") for k, v in hdrs2 if k == b"set-cookie")


def test_guard_health_endpoint():
    st, hdrs, body, inner = _run(_guard(), path="/healthz", client=("127.0.0.1", 1),
                                 host="127.0.0.1:8790")
    assert st == 200 and body == b'{"ok": true}' and not inner


# ----------------------------------------------------------------- 接到真实工作台


def _wb(tmp_path, public_url, **kw):
    pytest.importorskip("fastapi")
    from starlette.testclient import TestClient
    import workbench as W
    reg = W.Registry(tmp_path / "data", TOKEN)
    opts = D.DeployOptions(bind_host="0.0.0.0", allow_remote=True, public_url=public_url, **kw)
    app = D.wrap(W.Dispatcher(W.build_workbench(reg, TOKEN, 8790, opts.public_url), reg), opts)
    return W, reg, TestClient(app, base_url=public_url, client=("172.30.57.10", 4321))


@pytest.fixture()
def _isolated(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import workbench_llm as L
    monkeypatch.setattr(L, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(L, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(L, "_codebuddy_path", lambda: None)


def test_public_url_drives_absolute_urls_and_auth_paths(tmp_path, _isolated):
    _, _, c = _wb(tmp_path, "https://pfk.example.com", trusted_proxies=["172.30.57.10"])
    assert c.get("/").status_code == 200                  # 页面（无 token 也可加载外壳）
    assert c.get("/api/wb/state").status_code == 401      # 数据路径仍要 token
    st = c.get("/api/wb/state", headers={"Authorization": "Bearer " + TOKEN}).json()
    assert st["endpoints"]["mcp"] == "https://pfk.example.com/mcp"
    settings = c.get("/api/wb/settings", headers={"Authorization": "Bearer " + TOKEN}).json()
    assert settings["mcp_url"] == "https://pfk.example.com/mcp"
    # 浏览器：?token= 引导一次 → 会话 Cookie（https 下带 Secure）→ 之后不带 token 也能访问
    r = c.get("/?token=" + TOKEN)
    sc = r.headers.get("set-cookie", "")
    assert sc.startswith("wb_token=") and "Secure" in sc and "HttpOnly" in sc.replace("httponly", "HttpOnly")
    c.cookies.set("wb_token", TOKEN)
    assert c.get("/api/wb/state").status_code == 200
    assert c.get("/v1/capabilities").status_code == 200
    # Basic Auth（反向代理的凭据）不被后端当成 token，也不挡住会话 Cookie
    assert c.get("/v1/capabilities", headers={"Authorization": "Basic dTpw"}).status_code == 200
    c.cookies.clear()
    assert c.get("/v1/capabilities", headers={"Authorization": "Basic dTpw"}).status_code == 401
    assert c.get("/v1/capabilities", headers={"Authorization": "Bearer " + TOKEN}).status_code == 200
    # 别的 Host 一律 403
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 403


@pytest.mark.skipif(not _HAS_OCP, reason="需要 cadquery-ocp 才能准备工程")
def test_sub_app_reachable_through_public_host_with_cookie(tmp_path, _isolated):
    W, reg, c = _wb(tmp_path, "https://pfk.example.com", trusted_proxies=["172.30.57.10"])
    W.ensure_synthetic(reg)
    t0 = time.time()
    H = {"Authorization": "Bearer " + TOKEN}
    while True:
        st = c.get("/api/wb/projects/example-synthetic", headers=H).json()
        if st["status"] == "error":
            pytest.fail(st["message"])
        if st["status"] == "ready" and not st["message"]:
            break
        assert time.time() - t0 < 300
        time.sleep(1)
    # 子应用自己的 Host 守卫只认回环：经外层归一后，用公网 Host 访问照样通过；鉴权仍生效
    assert c.get("/p/example-synthetic/api/state").status_code == 401
    c.cookies.set("wb_token", TOKEN)
    assert c.get("/p/example-synthetic/api/state").status_code == 200
    assert c.get("/p/example-synthetic/").status_code == 200
    assert c.get("/p/example-synthetic/api/state",
                 headers={"Host": "evil.example"}).status_code == 403


def test_guard_blocks_server_side_path_import_but_not_uploads():
    g = _guard()
    st, _, body, inner = _run(g, path="/api/wb/projects", method="POST",
                              headers=[("Content-Type", "application/json")])
    assert st == 403 and not inner and "本地路径" in body.decode("utf-8")
    st2, _, _, inner2 = _run(g, path="/api/wb/projects", method="POST",
                             headers=[("Content-Type", "application/octet-stream")])
    assert st2 == 200 and inner2


def test_env_defaults_for_container_deploys():
    import argparse
    ap = argparse.ArgumentParser()
    D.add_arguments(ap)
    env = {"PFK_PUBLIC_URL": "https://pfk.example.com", "PFK_TRUSTED_PROXY": "172.30.57.10",
           "PFK_ALLOWED_HOSTS": "pfk.lan"}
    opts = D.from_args(ap.parse_args(["--host", "0.0.0.0", "--allow-remote"]), env)
    assert opts.public_url == "https://pfk.example.com"
    assert opts.allowed_hosts == ["pfk.example.com", "pfk.lan"]
    assert [str(n) for n in opts.trusted_proxies] == ["172.30.57.10/32"]
    # 命令行显式给出时以命令行为准；--allow-remote 没有环境变量（远程监听必须写在启动命令里）
    opts2 = D.from_args(ap.parse_args(["--trusted-proxy", "10.0.0.1"]), env)
    assert [str(n) for n in opts2.trusted_proxies] == ["10.0.0.1/32"] and not opts2.allow_remote
    with pytest.raises(D.DeployConfigError, match="--allow-remote"):
        D.from_args(ap.parse_args(["--host", "0.0.0.0"]), env)
