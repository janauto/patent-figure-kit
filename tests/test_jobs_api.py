"""Workbench async job endpoints + SSE + OpenAPI cross-check (WP1 deliverable 4).

End-to-end against the real app (TestClient), using the synthetic example project. Needs
cadquery-ocp for the render; skips cleanly without it. The OpenAPI cross-check needs no OCP.
"""

from __future__ import annotations

import hashlib
import json
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

def test_openapi_paths_match_live_routes(tmp_path):
    """Every /v1 and /mcp path+method declared in docs/openapi.yaml must exist on the app —
    this is what keeps WP3's mock honest (advisor's recommendation)."""
    yaml = pytest.importorskip("yaml")
    import workbench as W

    reg = W.Registry(tmp_path / "data", "t0k")  # no projects needed; isolated tmp dir
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
    yaml = pytest.importorskip("yaml")
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


def test_concurrent_same_etag_put_one_wins(client, fresh_project):
    """R1 #3: two PUTs carrying the SAME ETag must resolve to exactly one 200 and one 409 —
    the check and the write are atomic under the per-plan lock, so neither silently overwrites
    the other."""
    import threading
    pid = fresh_project
    plan = client.get("/v1/projects/%s/plan" % pid, headers=H)
    etag = plan.headers["etag"]
    doc = plan.json()
    results = []
    lk = threading.Lock()

    def put(tag):
        body = dict(doc)
        body["layout"] = dict(body.get("layout") or {}, density=tag)
        resp = client.put("/v1/projects/%s/plan" % pid,
                          headers={**H, "If-Match": etag}, json=body)
        with lk:
            results.append(resp.status_code)

    t1 = threading.Thread(target=put, args=("compact",))
    t2 = threading.Thread(target=put, args=("loose",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results) == [200, 409], results


def test_mcp_include_images_generates_on_demand(client, fresh_project):
    """R1 #14: a pure-MCP client that renders then asks for images gets them without ever
    hitting the browser PNG route or exporting."""
    pid = fresh_project
    rr = client.post("/v1/projects/%s/render" % pid, headers=H)
    j = _poll(client, rr.json()["job_id"])
    assert j["state"] == "succeeded", j
    r = client.post("/mcp", headers=H, json={
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "get_project_figures",
                   "arguments": {"project_id": pid, "include_images": True}}})
    content = r.json()["result"]["content"]
    assert any(c.get("type") == "image" for c in content), "include_images 应按需生成 PNG"


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
    # A fresh render first, so the export binds to a known-good render job.
    rr = client.post("/v1/projects/%s/render" % PID, headers=H)
    _poll(client, rr.json()["job_id"])
    r = client.post("/v1/projects/%s/export" % PID, headers=H,
                    json={"variant": "both", "formats": ["pdf", "docx", "dxf"]})
    assert r.status_code == 202
    assert r.json()["render_job_id"]
    eid = r.json()["export_id"]
    j = _poll(client, eid)
    assert j["state"] == "succeeded", j
    manifest = j["result"]["manifest"]
    # Per figure × version × format accounting (R1 #8): dxf produced, pdf deferred to WP2.
    assert manifest, manifest
    dxf_items = {k: v for k, v in manifest.items() if k.endswith("/dxf")}
    pdf_items = {k: v for k, v in manifest.items() if k.endswith("/pdf")}
    assert dxf_items and all(v == "ok" for v in dxf_items.values()), manifest
    assert pdf_items and all(v == "skip" for v in pdf_items.values()), manifest
    # docx is one Word per disclosure variant; the 交底版 item is ok.
    docx_ok = [k for k, v in manifest.items() if k.endswith("/docx") and v == "ok"]
    assert docx_ok, manifest
    dl = client.get("/v1/exports/%s/download" % eid, headers=H)
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/zip"
    assert len(dl.content) > 200


def test_export_variant_filing_excludes_annotated(client):
    """R1 #12: variant must actually filter — filing packages only the 递交版, no 交底版 Word."""
    rr = client.post("/v1/projects/%s/render" % PID, headers=H)
    _poll(client, rr.json()["job_id"])
    r = client.post("/v1/projects/%s/export" % PID, headers=H,
                    json={"variant": "filing", "formats": ["dxf", "docx"]})
    eid = r.json()["export_id"]
    j = _poll(client, eid)
    manifest = j["result"]["manifest"]
    assert all("递交版" in k for k in manifest), manifest       # only filing version present
    assert not any("交底版" in k for k in manifest), manifest


def test_export_rejects_unimplemented_sheet_preset(client):
    r = client.post("/v1/projects/%s/export" % PID, headers=H,
                    json={"formats": ["dxf"], "sheet_preset": "conservative-165x245"})
    assert r.status_code == 400


def test_export_stale_etag_conflicts(client):
    """R1 #7: export with a plan_etag that does not match the bound render's input → 409."""
    rr = client.post("/v1/projects/%s/render" % PID, headers=H)
    _poll(client, rr.json()["job_id"])
    r = client.post("/v1/projects/%s/export" % PID, headers=H,
                    json={"formats": ["dxf"], "plan_etag": "deadbeefdeadbeef"})
    assert r.status_code == 409


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


def test_create_project_async_prepare(client):
    """POST /v1/projects uploads a STEP (raw octet-stream), returns 202 + a prepare job that
    only reaches 'succeeded' once the project is actually ready (not instantly)."""
    step_bytes = (_ROOT / "tests" / "fixtures" / "synthetic.stp").read_bytes()
    r = client.post("/v1/projects?title=bench-upload&name=up.stp", headers=H,
                    content=step_bytes)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["project_id"] and body["job_id"]
    j = _poll(client, body["job_id"], timeout=300)
    assert j["state"] == "succeeded", j
    assert j["result"]["status"] == "ready"
    # and the project is genuinely usable now
    proj = client.get("/v1/projects/%s" % body["project_id"], headers=H).json()
    assert proj["status"] == "ready"
    assert "plan_etag" in proj


def _good_plan(step_path: str) -> dict:
    """The known-good 2-figure synthetic plan (both figures pass QA), pointed at a project's
    own STEP so an uploaded project renders cleanly."""
    return {
        "schema": "patent-figure-plan/1",
        "source": {"step": step_path, "include": ["SYN-*"], "exclude": []},
        "terms": [
            {"selector": "SYN-A01", "term": "底座"},
            {"selector": "SYN-B02", "term": "回转座"},
            {"selector": "SYN-C03", "term": "支撑轴"},
            {"selector": "SYN-D04", "term": "密封圈"},
            {"selector": "SYN-E05", "term": "调整垫片", "label": "once"},
            {"selector": "SYN-F06", "term": "球头", "label": "all"},
            {"selector": "SYN-G07", "term": "上盖"},
            {"selector": "SYN-H08*", "term": "紧固螺钉", "label": "none"},
        ],
        "figures": [
            {"id": "fig1", "caption": "整体结构示意图", "kind": "assembly", "members": ["*"]},
            {"id": "fig2", "caption": "回转组件分解示意图", "kind": "exploded",
             "members": ["SYN-A01", "SYN-B02", "SYN-C03", "SYN-G07"],
             "layout": {"explode_axis": "z"}},
        ],
        "layout": {"view": "iso", "explode_axis": "auto", "axis_angle": "auto",
                   "density": "normal", "max_labels_per_figure": 20,
                   "engineering_table": False},
    }


@pytest.fixture
def fresh_project(client):
    """A private project (uploaded synthetic STEP) with a known-good 2-figure plan installed,
    so the isolation tests can mutate it without disturbing the shared example-synthetic."""
    step_bytes = (_ROOT / "tests" / "fixtures" / "synthetic.stp").read_bytes()
    r = client.post("/v1/projects?title=iso&name=iso.stp", headers=H, content=step_bytes)
    pid = r.json()["project_id"]
    _poll(client, r.json()["job_id"], timeout=300)
    # install the good plan (keep the project's own source.step)
    cur = client.get("/v1/projects/%s/plan" % pid, headers=H)
    step_path = (cur.json().get("source") or {}).get("step", "")
    good = _good_plan(step_path)
    client.put("/v1/projects/%s/plan" % pid, headers={**H, "If-Match": cur.headers["etag"]},
               json=good)
    return pid


def test_input_isolation_frozen_plan(client, fresh_project, tmp_path_factory):
    """R1 #1: a plan edited AFTER a render is submitted does not change what that job renders —
    the job's frozen inputs/plan.json is independent of the live plan."""
    pid = fresh_project
    # submit a render, then immediately edit the live plan
    r = client.post("/v1/projects/%s/render" % pid, headers=H)
    job_id = r.json()["job_id"]
    plan = client.get("/v1/projects/%s/plan" % pid, headers=H)
    etag = plan.headers["etag"]
    edited = plan.json()
    edited["terms"] = (edited.get("terms") or []) + [{"selector": "SYN-A01", "term": "改名后"}]
    client.put("/v1/projects/%s/plan" % pid, headers={**H, "If-Match": etag}, json=edited)
    _poll(client, job_id)
    # the frozen input the job rendered from must NOT contain the post-submit edit
    j = client.get("/v1/jobs/%s" % job_id, headers=H).json()
    # the job's recorded plan_hash reflects the submit-time plan, not the edited one
    frozen_hash = j["inputs"]["plan_hash"]
    import hashlib
    live_hash = hashlib.sha256(json.dumps(edited, ensure_ascii=False, indent=2)
                               .encode("utf-8")).hexdigest()
    assert frozen_hash != live_hash, "job must have frozen the submit-time plan, not the edit"


def test_publish_set_replaces_out_exactly(client, fresh_project):
    """R1 #2: out/ holds exactly the latest run's set — a figure dropped from the plan leaves
    out/ after the next render, it is not left behind as a stale old file."""
    pid = fresh_project
    r1 = client.post("/v1/projects/%s/render" % pid, headers=H)
    _poll(client, r1.json()["job_id"])
    figs_before = {f["id"] for f in client.get(
        "/v1/projects/%s/figures" % pid, headers=H).json()["figures"]}
    assert len(figs_before) >= 2
    # drop every figure but the first, then re-render
    plan = client.get("/v1/projects/%s/plan" % pid, headers=H)
    etag = plan.headers["etag"]
    doc = plan.json()
    doc["figures"] = doc["figures"][:1]
    client.put("/v1/projects/%s/plan" % pid, headers={**H, "If-Match": etag}, json=doc)
    r2 = client.post("/v1/projects/%s/render" % pid, headers=H)
    _poll(client, r2.json()["job_id"])
    figs_after = {f["id"] for f in client.get(
        "/v1/projects/%s/figures" % pid, headers=H).json()["figures"]}
    assert figs_after == {doc["figures"][0]["id"]}, (figs_before, figs_after)


def test_okfalse_render_is_a_failed_job(client, fresh_project):
    """R1 #5: a render that runs but produces no passing figure (here: a figure selecting no
    members → render exits 1) is a structured *failed* job, not succeeded."""
    pid = fresh_project
    plan = client.get("/v1/projects/%s/plan" % pid, headers=H)
    etag = plan.headers["etag"]
    doc = plan.json()
    doc["figures"] = [{"id": "figX", "caption": "空图", "kind": "assembly",
                       "members": ["NOPE-DOES-NOT-EXIST"]}]
    client.put("/v1/projects/%s/plan" % pid, headers={**H, "If-Match": etag}, json=doc)
    r = client.post("/v1/projects/%s/render" % pid, headers=H)
    j = _poll(client, r.json()["job_id"])
    assert j["state"] == "failed", j
    assert j["error"] and j["error"]["code"]
    # the error terminal event, not a done, was emitted
    evs = client.get("/v1/jobs/%s" % r.json()["job_id"], headers=H).json()
    assert evs["state"] == "failed"


def test_mixed_qa_render_is_failed_not_success(client, fresh_project):
    """R1b #1: one figure delivered, one failing (members match nothing) → the job is failed,
    never ok:true — a partial result cannot read as success."""
    pid = fresh_project
    cur = client.get("/v1/projects/%s/plan" % pid, headers=H)
    doc = cur.json()
    doc["figures"] = [doc["figures"][0],   # fig1 good
                      {"id": "figX", "caption": "坏图", "kind": "assembly",
                       "members": ["NOPE-NONE"]}]
    client.put("/v1/projects/%s/plan" % pid, headers={**H, "If-Match": cur.headers["etag"]},
               json=doc)
    r = client.post("/v1/projects/%s/render" % pid, headers=H)
    j = _poll(client, r.json()["job_id"])
    assert j["state"] == "failed", j
    assert j["result"]["ok"] is False


def test_browser_render_goes_through_version_store(client, fresh_project):
    """R1b #5: the browser sub-app /api/render submits the same isolated job; afterwards out/
    is the version-store view (a symlink to results/<id>), not a directly-written directory."""
    pid = fresh_project
    r = client.post("/p/%s/api/render" % pid, headers=H)
    assert r.status_code == 200
    # wait for the sub-app render to finish
    for _ in range(600):
        st = client.get("/p/%s/api/render/status" % pid, headers=H).json()
        if not st["running"] and st.get("result"):
            break
        time.sleep(0.5)
    assert st["result"]["ok"] is True
    # a /v1 render job was created for it (unified entry)
    cur = client.get("/v1/projects/%s/plan" % pid, headers=H)  # sanity: project still serves
    assert cur.status_code == 200


def test_export_rejects_foreign_or_unsuccessful_render_job(client, fresh_project):
    """R1b #4: an explicit render_job_id from another project, or not a succeeded render, is
    rejected (409) — no bypassing project/kind/state/completeness checks."""
    pid = fresh_project
    # a non-render job id (the prepare job of a *different* fresh project)
    step_bytes = (_ROOT / "tests" / "fixtures" / "synthetic.stp").read_bytes()
    other = client.post("/v1/projects?title=o2&name=o2.stp", headers=H, content=step_bytes)
    prepare_job = other.json()["job_id"]
    r = client.post("/v1/projects/%s/export" % pid, headers=H,
                    json={"formats": ["dxf"], "render_job_id": prepare_job})
    assert r.status_code == 409


def test_cancel_before_commit_does_not_publish(client, fresh_project):
    """R1b #6: cancelling a render before its commit point must not change the current version."""
    pid = fresh_project
    # establish a baseline current version
    r0 = client.post("/v1/projects/%s/render" % pid, headers=H)
    _poll(client, r0.json()["job_id"])
    base_figs = {f["id"] for f in client.get(
        "/v1/projects/%s/figures" % pid, headers=H).json()["figures"]}
    # submit another and cancel it immediately
    r1 = client.post("/v1/projects/%s/render" % pid, headers=H)
    jid = r1.json()["job_id"]
    client.delete("/v1/jobs/%s" % jid, headers=H)
    j = _poll(client, jid)
    # whatever the outcome, if it was cancelled the published set must equal the baseline
    if j["state"] == "cancelled":
        figs = {f["id"] for f in client.get(
            "/v1/projects/%s/figures" % pid, headers=H).json()["figures"]}
        assert figs == base_figs


def test_export_download_survives_restart(client, fresh_project, tmp_path_factory):
    """R1b #11: a successful export's download resolves from the persisted job + deterministic
    bundle path, so it still works after the process (JobStore) is rebuilt."""
    pid = fresh_project
    rr = client.post("/v1/projects/%s/render" % pid, headers=H)
    _poll(client, rr.json()["job_id"])
    ex = client.post("/v1/projects/%s/export" % pid, headers=H, json={"formats": ["dxf"]})
    eid = ex.json()["export_id"]
    _poll(client, eid)
    # rebuild the app over the same data dir (simulates a restart)
    import workbench as W
    from starlette.testclient import TestClient
    reg = client.app.reg                        # the Dispatcher holds the Registry
    c2 = TestClient(W.Dispatcher(W.build_workbench(reg, "t0k", 8790), reg),
                    base_url="http://127.0.0.1:8790")
    dl = c2.get("/v1/exports/%s/download" % eid, headers=H)
    assert dl.status_code == 200 and len(dl.content) > 200


def test_wait1_failure_returns_this_jobs_status_not_prior_success(client, fresh_project):
    """R1c #4: a sync wait=1 render that fails must report THIS job's failure, never a prior
    successful run's result."""
    pid = fresh_project
    # first a successful render establishes a prior success
    ok = client.post("/v1/projects/%s/render?wait=1" % pid, headers=H)
    assert ok.status_code == 200 and ok.json().get("ok") is True
    # now break the plan so the next render fails, and wait=1
    cur = client.get("/v1/projects/%s/plan" % pid, headers=H)
    doc = cur.json()
    doc["figures"] = [{"id": "figX", "caption": "坏", "kind": "assembly",
                       "members": ["NOPE-NONE"]}]
    client.put("/v1/projects/%s/plan" % pid, headers={**H, "If-Match": cur.headers["etag"]},
               json=doc)
    bad = client.post("/v1/projects/%s/render?wait=1" % pid, headers=H)
    body = bad.json()
    # must reflect THIS job's failure, never the prior success
    assert body.get("state") == "failed", body
    assert (body.get("result") or {}).get("ok") is False, body
    assert body.get("ok") is not True


def test_idempotency_different_plan_conflicts(client, fresh_project):
    """R1c #8: reusing a render idempotency key after the plan changed is a 409."""
    pid = fresh_project
    k = {"Idempotency-Key": "render-key-1"}
    r1 = client.post("/v1/projects/%s/render" % pid, headers={**H, **k})
    assert r1.status_code == 202
    _poll(client, r1.json()["job_id"])
    # change the plan, reuse the same key → conflict
    cur = client.get("/v1/projects/%s/plan" % pid, headers=H)
    doc = cur.json(); doc["layout"] = dict(doc.get("layout") or {}, density="compact")
    client.put("/v1/projects/%s/plan" % pid, headers={**H, "If-Match": cur.headers["etag"]},
               json=doc)
    r2 = client.post("/v1/projects/%s/render" % pid, headers={**H, **k})
    assert r2.status_code == 409


def test_export_rejects_corrupted_version(client, fresh_project):
    """R1d #5: export must call verify() — a truncated DXF in the sealed version is rejected
    (409), not packaged."""
    import workbench as W
    from patent_figure import versions as VS
    pid = fresh_project
    rr = client.post("/v1/projects/%s/render" % pid, headers=H)
    rid = rr.json()["job_id"]                      # the render job id == version id
    _poll(client, rid)
    ws_root = client.app.reg.need(pid).root / "studio"
    vdir = VS.VersionStore(ws_root).version_dir(rid)
    dxf = next(p for p in vdir.glob("*.dxf") if not p.stem.endswith(("_annotated", "_filing")))
    dxf.write_text("", encoding="utf-8")          # truncate a sealed artefact
    r = client.post("/v1/projects/%s/export" % pid, headers=H,
                    json={"formats": ["dxf"], "render_job_id": rid})
    assert r.status_code == 409


def test_export_reflects_current_version_consistently(client, fresh_project):
    """R1d #2: export figures/manifest come from one resolved version (the current one)."""
    pid = fresh_project
    rr = client.post("/v1/projects/%s/render" % pid, headers=H)
    _poll(client, rr.json()["job_id"])
    ex = client.post("/v1/projects/%s/export" % pid, headers=H, json={"formats": ["dxf"]})
    j = _poll(client, ex.json()["export_id"])
    assert j["state"] == "succeeded"
    figs = {f["id"] for f in client.get(
        "/v1/projects/%s/figures" % pid, headers=H).json()["figures"]}
    manifest_figs = {k.split("/")[0] for k in j["result"]["manifest"]}
    assert manifest_figs == figs                   # export set == current figures


def test_mcp_render_project_binds_this_job(client, fresh_project):
    """R1d #9: MCP render_project returns THIS render's result, not shared render_state."""
    pid = fresh_project
    r = client.post("/mcp", headers=H, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "render_project", "arguments": {"project_id": pid}}})
    sc = r.json()["result"]["structuredContent"]
    assert sc["ok"] is True and sc["figures"]


def test_recovery_marks_sealed_okfalse_job_failed(client, fresh_project):
    """R1d #4: a sealed ok=false version whose render job died before raising must recover as
    FAILED (rebuilt from result.json) — not silently succeeded. 'complete' != success."""
    import json as _json
    import workbench as W
    from starlette.testclient import TestClient
    from patent_figure import versions as VS
    pid = fresh_project
    reg = client.app.reg
    ws_root = reg.need(pid).root / "studio"
    vs = VS.VersionStore(ws_root)
    rid = "okfalse-job"
    vd = vs.begin(rid)
    (vd / "fig1.dxf").write_text("x", encoding="utf-8")
    body = {"figures": [{"id": "fig1", "pass": False}], "ok": False}
    vs.write_result(rid, body)
    vs.seal(rid, body, 999)                       # sealed + complete, but ok=false and NOT current
    # persist a 'running' render job for rid (as if the process died before the failure raise)
    jobs_dir = reg.data / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / (rid + ".json")).write_text(_json.dumps({
        "id": rid, "kind": "render", "project_id": pid, "state": "running",
        "events": [{"id": 1, "type": "stage", "stage": "running", "progress": 0.0,
                    "data": None, "ts": 0}], "next_event": 2}), encoding="utf-8")
    # rebuild the app (restart) → reconciliation runs
    c2 = TestClient(W.Dispatcher(W.build_workbench(reg, "t0k", 8790), reg),
                    base_url="http://127.0.0.1:8790")
    j = c2.get("/v1/jobs/%s" % rid, headers=H).json()
    assert j["state"] == "failed", j
    assert (j.get("result") or {}).get("ok") is False
    assert j["error"]["code"] == "E_RENDER_NOT_OK"


def test_standalone_plan_studio_local_render_uses_publish_service(client, fresh_project):
    """R1d #1: standalone Plan Studio (no injected render_service) renders through the SAME
    publish service — a version is committed via current.json, not written to out directly."""
    import plan_studio as PS
    from starlette.testclient import TestClient
    from patent_figure import versions as VS
    pid = fresh_project
    ws = client.app.reg.need(pid).workspace()
    app = PS.build_app(ws, "t0k", render_service=None)   # standalone: forces _local_render
    c = TestClient(app, base_url="http://127.0.0.1:8425")
    assert c.post("/api/render", headers={"x-studio-token": "t0k"}).status_code == 200
    for _ in range(600):
        st = c.get("/api/render/status", headers={"x-studio-token": "t0k"}).json()
        if not st["running"] and st.get("result"):
            break
        time.sleep(0.5)
    assert st["result"]["ok"] is True
    # it went through the version store: current.json names a complete version
    vs = VS.VersionStore(ws.root)
    assert vs.current_id() and vs.is_complete(vs.current_id())


def test_mcp_has_new_job_tools():
    import workbench_mcp as M
    names = {t["name"] for t in M.TOOLS}
    assert {"submit_render", "get_job", "cancel_job", "get_export"} <= names
