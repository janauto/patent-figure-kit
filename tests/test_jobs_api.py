"""Workbench async job endpoints + SSE + OpenAPI cross-check (WP1 deliverable 4).

End-to-end against the real app (TestClient), using the synthetic example project. Needs
cadquery-ocp for the render; skips cleanly without it. The OpenAPI cross-check needs no OCP.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import workbench_llm as L  # noqa: E402

try:
    import OCP  # noqa: F401
    _HAS_OCP = True
except Exception:  # pragma: no cover
    _HAS_OCP = False

H = {"Authorization": "Bearer t0k"}


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(L, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(L, "_codebuddy_path", lambda: None)


# ------------------------------------------------------------ OpenAPI cross-check (no OCP)

def test_openapi_paths_match_live_routes():
    """Every /v1 and /mcp path+method declared in docs/openapi.yaml must exist on the app —
    this is what keeps WP3's mock honest (advisor's recommendation)."""
    import yaml
    import workbench as W

    reg = W.Registry(Path("/tmp/does-not-matter-openapi"), "t0k")  # no projects needed
    app = W.build_workbench(reg, "t0k", 8790)
    live = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        for m in methods:
            live.add((m.upper(), getattr(route, "path", "")))

    spec = yaml.safe_load((_ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    missing = []
    for path, item in spec["paths"].items():
        for method in item:
            if method.lower() not in ("get", "post", "put", "delete", "patch"):
                continue
            # suggest is explicitly documented as WP2-not-yet-implemented
            if path.endswith("/plan/suggest"):
                continue
            if (method.upper(), path) not in live:
                missing.append("%s %s" % (method.upper(), path))
    assert not missing, "openapi.yaml 声明了实现里没有的路由：%s" % missing


def test_openapi_is_valid_and_declares_job_states():
    import yaml
    spec = yaml.safe_load((_ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    states = spec["components"]["schemas"]["Job"]["properties"]["state"]["enum"]
    assert set(states) == {"queued", "running", "succeeded", "failed", "cancelled", "interrupted"}


# ------------------------------------------------------------ live job flow (needs OCP)

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    if not _HAS_OCP:
        pytest.skip("需要 cadquery-ocp")
    import workbench as W
    from starlette.testclient import TestClient
    data = tmp_path_factory.mktemp("jobsapi")
    reg = W.Registry(data / "data", "t0k")
    W.ensure_synthetic(reg)
    c = TestClient(W.Dispatcher(W.build_workbench(reg, "t0k", 8790), reg),
                   base_url="http://127.0.0.1:8790")
    t0 = time.time()
    while True:
        st = c.get("/api/wb/projects/example-synthetic", headers=H).json()
        if st["status"] == "ready" and not st["message"]:
            break
        assert time.time() - t0 < 300, "示例工程准备超时"
        time.sleep(1)
    return c


PID = "example-synthetic"


def _poll(c, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get("/v1/jobs/%s" % job_id, headers=H).json()
        if j["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return j
        time.sleep(0.5)
    raise AssertionError("作业 %s 超时未结束" % job_id)


def test_render_async_202_then_poll(client):
    r = client.post("/v1/projects/%s/render" % PID, headers=H)
    assert r.status_code == 202
    body = r.json()
    assert "job_id" in body and body["inputs"]["plan_hash"]
    j = _poll(client, body["job_id"])
    assert j["state"] == "succeeded"
    assert j["result"]["ok"] is True
    assert j["kind"] == "render"


def test_render_wait_1_is_synchronous(client):
    r = client.post("/v1/projects/%s/render?wait=1" % PID, headers=H)
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_render_idempotency_key_reuses_job(client):
    k = "idem-xyz"
    r1 = client.post("/v1/projects/%s/render" % PID, headers={**H, "Idempotency-Key": k})
    r2 = client.post("/v1/projects/%s/render" % PID, headers={**H, "Idempotency-Key": k})
    assert r1.json()["job_id"] == r2.json()["job_id"]
    _poll(client, r1.json()["job_id"])


def test_sse_replays_and_terminates(client):
    r = client.post("/v1/projects/%s/render" % PID, headers=H)
    job_id = r.json()["job_id"]
    _poll(client, job_id)                      # let it finish, then subscribe for replay
    frames = []
    with client.stream("GET", "/v1/jobs/%s/events" % job_id, headers=H) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        for line in resp.iter_lines():
            frames.append(line)
            if len(frames) > 400:
                break
    text = "\n".join(frames)
    assert "event: done" in text
    assert "id: 1" in text                     # the first event is replayed


def test_sse_last_event_id_skips_earlier(client):
    r = client.post("/v1/projects/%s/render" % PID, headers=H)
    job_id = r.json()["job_id"]
    j = _poll(client, job_id)
    last = j["last_event_id"]
    # Subscribe from the final id: no further events, stream ends immediately.
    with client.stream("GET", "/v1/jobs/%s/events" % job_id,
                       headers={**H, "Last-Event-ID": str(last)}) as resp:
        body = "".join(resp.iter_text())
    assert ("id: %d" % last) not in body       # already-seen event not replayed


def test_plan_etag_and_if_match(client):
    r = client.get("/v1/projects/%s/plan" % PID, headers=H)
    assert r.status_code == 200
    etag = r.headers["etag"]
    assert etag
    plan = r.json()
    # stale If-Match -> 409
    bad = client.put("/v1/projects/%s/plan" % PID, headers={**H, "If-Match": "deadbeef"},
                     json=plan)
    assert bad.status_code == 409
    # correct If-Match -> 200 + a new ETag (content identical here, so etag stays same, still ok)
    ok = client.put("/v1/projects/%s/plan" % PID, headers={**H, "If-Match": etag}, json=plan)
    assert ok.status_code == 200
    assert ok.headers["etag"]


def test_export_async_then_download(client):
    r = client.post("/v1/projects/%s/export" % PID, headers=H,
                    json={"variant": "both", "formats": ["pdf", "docx", "dxf"]})
    assert r.status_code == 202
    eid = r.json()["export_id"]
    j = _poll(client, eid)
    assert j["state"] == "succeeded", j
    assert j["result"]["manifest"]
    dl = client.get("/v1/exports/%s/download" % eid, headers=H)
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"
    assert len(dl.content) > 200


def test_cancel_terminal_job_returns_state(client):
    r = client.post("/v1/projects/%s/render?wait=1" % PID, headers=H)
    assert r.status_code == 200
    # a fresh async job then cancel after it is done
    r2 = client.post("/v1/projects/%s/render" % PID, headers=H)
    jid = r2.json()["job_id"]
    _poll(client, jid)
    d = client.delete("/v1/jobs/%s" % jid, headers=H)
    assert d.status_code == 200
    assert d.json()["state"] in ("succeeded", "cancelled")


def test_job_404(client):
    assert client.get("/v1/jobs/nope", headers=H).status_code == 404
    assert client.delete("/v1/jobs/nope", headers=H).status_code == 404


def test_mcp_has_new_job_tools():
    import workbench_mcp as M
    names = {t["name"] for t in M.TOOLS}
    assert {"submit_render", "get_job", "cancel_job", "get_export"} <= names
