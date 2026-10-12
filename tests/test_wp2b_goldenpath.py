"""WP2b 金路径后端根因修复：F1 准备作业的 AI 结构识别独立短超时、F2 下载地址按请求生成、F3 出图失败
结构化（E_TERMS_MISSING）、F4 排版阶段错误的修复动作、F5 接受候选时移除未过闸的初始兜底图、
F6 工程详情的 render_job 摘要与 openapi 一致。

无 OCP 的部分总是跑（准备步骤用假 prepare，AI 通道用假函数，导出作业直接造在作业库里）；端到端部分
（真实上传 / 出图 / 候选分图）需要 cadquery-ocp，没装则跳过。
"""

from __future__ import annotations

import json
import re
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
except ImportError:  # pragma: no cover
    _HAS_OCP = False

TOKEN = "t0k"
BEARER = {"Authorization": "Bearer %s" % TOKEN}
PORT = 8808
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(L, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("PFK_AI_STRUCTURE_TIMEOUT_S", raising=False)
    monkeypatch.setattr(L, "_codebuddy_path", lambda: None)


def _build(data: Path, host: str = "127.0.0.1"):
    import workbench as W
    from starlette.testclient import TestClient
    reg = W.Registry(data, TOKEN)
    app = W.build_workbench(reg, TOKEN, PORT)
    c = TestClient(W.Dispatcher(app, reg), base_url="http://%s:%d" % (host, PORT))
    return W, reg, app, c


def _poll(c, job_id, timeout=120.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get("/v1/jobs/%s" % job_id, headers=BEARER).json()
        if j["state"] in TERMINAL:
            return j
        time.sleep(0.1)
    raise AssertionError("作业 %s 超时" % job_id)


# =========================================================================== F1


def _fake_prepare(ws, quiet=False):
    """Stands in for analyze + GLB: writes a tiny assembly and blank plan, nothing else."""
    ws.assembly.write_text(json.dumps({
        "source": {"sha256": "x"}, "parts": [
            {"name": "SYN-A01", "instances": 1, "bbox_size": [1, 2, 3], "centers": [[0, 0, 0]]},
            {"name": "SYN-B02", "instances": 1, "bbox_size": [2, 2, 2], "centers": [[5, 0, 0]]}]}),
        encoding="utf-8")
    if not ws.plan.is_file():
        ws.plan.write_text(json.dumps({
            "schema": "patent-figure-plan/1", "source": {"step": str(ws.step)},
            "terms": [{"selector": "SYN-A01", "term": "", "label": "once"}],
            "figures": [{"id": "fig1", "caption": "整体结构示意图", "kind": "assembly",
                         "members": ["*"]}]}), encoding="utf-8")


@pytest.fixture
def ai_on(monkeypatch):
    """A configured (fake) AI channel; analyze_structure is replaced per test."""
    import plan_studio as PS
    monkeypatch.setattr(PS, "prepare", _fake_prepare)
    real = L.public_settings
    monkeypatch.setattr(L, "public_settings",
                        lambda: dict(real(), provider="deepseek", provider_label="假通道"))
    return monkeypatch


def _upload(c, title="上传"):
    r = c.post("/v1/projects?title=%s&name=m.stp" % title, headers=BEARER,
               content=b"ISO-10303-21;" + b"x" * 200)
    assert r.status_code == 202, r.text
    return r.json()


def test_prepare_job_succeeds_when_the_ai_channel_fails(tmp_path, ai_on):
    """F1: AI configured but the channel is dead → the prepare job still succeeds, promptly, with
    ai_status=failed + the reason (it used to wait out the 1800 s job limit)."""
    def dead(*a, **k):
        raise L.LLMError("DeepSeek 拒绝了密钥（401）——请在设置里核对")
    ai_on.setattr(L, "analyze_structure", dead)
    W, reg, _app, c = _build(tmp_path / "data")
    ref = _upload(c)
    t0 = time.time()
    j = _poll(c, ref["job_id"], timeout=30)
    assert j["state"] == "succeeded", j
    assert time.time() - t0 < 15
    res = j["result"]
    assert res["status"] == "ready" and res["ai_status"] == "failed"
    assert "401" in res["ai_error"]
    proj = c.get("/v1/projects/%s" % ref["project_id"], headers=BEARER).json()
    assert proj["status"] == "ready"
    assert proj["structure"]["ai_status"] == "failed" and "401" in proj["structure"]["ai_error"]
    assert "结构识别未完成" in proj["message"]                  # the UI hint is still there
    assert not (reg.get(ref["project_id"]).root / "studio" / "structure.json").exists()


def test_prepare_job_bounds_a_hanging_ai_call_and_discards_its_late_result(tmp_path, ai_on):
    """F1: the AI step has its own short timeout (configurable). A call that hangs past it is
    abandoned — the job succeeds with ai_status=failed — and its late answer is NOT written."""
    import workbench as W
    ai_on.setenv("PFK_AI_STRUCTURE_TIMEOUT_S", "0.5")
    ai_on.setattr(W, "AI_STRUCTURE_GRACE_S", 0.2)
    seen = {}

    def slow(*a, **k):
        seen["timeout_s"] = k.get("timeout_s")
        time.sleep(3.0)                                  # ignores its deadline on purpose
        return {"schema": "patent-structure/1", "groups": [], "names": {}}
    ai_on.setattr(L, "analyze_structure", slow)
    _W, reg, _app, c = _build(tmp_path / "data")
    ref = _upload(c)
    t0 = time.time()
    j = _poll(c, ref["job_id"], timeout=30)
    took = time.time() - t0
    assert j["state"] == "succeeded" and j["result"]["ai_status"] == "failed", j
    assert "超过 0.5 秒" in j["result"]["ai_error"]
    assert took < 2.5, took                                 # did not wait for the slow call
    assert seen["timeout_s"] == 0.5                          # the call itself got the budget
    stages = [e["data"] for e in reg_job_events(_app, ref["job_id"]) if e["type"] == "stage"]
    assert any("AI 识别结构" in str(s) for s in stages), stages
    time.sleep(max(0.0, 3.3 - (time.time() - t0)))          # the late answer arrives …
    assert not (reg.get(ref["project_id"]).root / "studio" / "structure.json").exists()


def reg_job_events(app, job_id):
    return app.state.jobs.get(job_id).events


def test_prepare_job_reports_ai_ok_and_not_configured(tmp_path, ai_on):
    ai_on.setattr(L, "analyze_structure", lambda *a, **k: {
        "schema": "patent-structure/1", "source": "ai", "groups": [], "names": {}})
    _W, reg, _app, c = _build(tmp_path / "data")
    ref = _upload(c)
    j = _poll(c, ref["job_id"])
    assert j["state"] == "succeeded" and j["result"]["ai_status"] == "ok", j
    assert (reg.get(ref["project_id"]).root / "studio" / "structure.json").is_file()
    # no channel at all → not_configured, nothing called
    ai_on.setattr(L, "public_settings", lambda: {"provider": "none"})
    ai_on.setattr(L, "analyze_structure", lambda *a, **k: pytest.fail("不该外发"))
    ref2 = _upload(c, "无通道")
    j2 = _poll(c, ref2["job_id"])
    assert j2["state"] == "succeeded" and j2["result"]["ai_status"] == "not_configured"


def test_ai_structure_timeout_is_configurable(monkeypatch):
    import workbench as W
    assert W.ai_structure_timeout() == 60.0
    L.save_config({"ai_structure_timeout_s": 12})
    assert W.ai_structure_timeout() == 12.0
    monkeypatch.setenv("PFK_AI_STRUCTURE_TIMEOUT_S", "3.5")
    assert W.ai_structure_timeout() == 3.5
    monkeypatch.setenv("PFK_AI_STRUCTURE_TIMEOUT_S", "nope")
    assert W.ai_structure_timeout() == 12.0


def test_chat_json_never_retries_past_its_budget(monkeypatch):
    """F1: the LLM call itself honours the step's budget — every HTTP attempt's timeout and the
    back-off sleeps are cut to what is left, instead of 4 × 300 s."""
    import httpx
    # this test drives the REAL chat_json against a fake network: lift the suite-wide channel
    # switch and the outbound sentinel (tests/conftest.py) for it only
    monkeypatch.delenv("PFK_AI_PROVIDER", raising=False)
    monkeypatch.setattr(L, "OUTBOUND_HOOKS", [])
    L.save_config({"provider": "deepseek", "deepseek": {"api_key": "sk-test-0000000000"}})
    seen = []

    def post(url, **kw):
        seen.append(kw["timeout"])
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "post", post)
    t0 = time.monotonic()
    with pytest.raises(L.LLMError):
        L.chat_json("s", "u", timeout_s=0.6)
    assert time.monotonic() - t0 < 2.0
    assert seen and all(t <= 0.6 + 1e-6 for t in seen), seen


def _seed_project(reg, pid, meta, plan=True):
    root = reg.dir / pid
    (root / "studio").mkdir(parents=True)
    step = root / "model.stp"
    step.write_bytes(b"ISO-10303-21;" + b"x" * 200)
    m = {"id": pid, "title": pid, "step": str(step), "example": True, "created": "2026-10-13"}
    m.update(meta)
    (root / "project.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return root


def test_from_example_on_a_ready_project_with_a_stale_ai_message_returns_at_once(tmp_path):
    """F1: a project prepared before WP2b whose message still says the AI step failed (and has no
    ai_status) is ready — the job must not wait for the message to clear (it never would)."""
    W, reg, _app, c = _build(tmp_path / "data")
    _seed_project(reg, "example-vise", {"status": "ready",
                                        "message": "结构识别未完成：网络错误（可在工程里手动重试）"})
    r = c.post("/v1/projects/from-example", headers=BEARER, json={"example": "vise"})
    assert r.status_code == 202
    j = _poll(c, r.json()["job_id"], timeout=10)
    assert j["state"] == "succeeded" and j["result"]["ai_status"] == "skipped", j
    assert j["result"]["example"] == "vise"


def test_a_preparation_cut_off_by_a_restart_is_resumed(tmp_path, monkeypatch):
    """F1: a project left 'preparing' by an earlier process has no live preparation thread — the
    prepare job restarts it instead of waiting out the 30-minute limit."""
    import plan_studio as PS
    monkeypatch.setattr(PS, "prepare", _fake_prepare)
    W, reg, _app, c = _build(tmp_path / "data")
    _seed_project(reg, "example-valve", {"status": "preparing", "message": "正在解析…"})
    r = c.post("/v1/projects/from-example", headers=BEARER, json={"example": "valve"})
    j = _poll(c, r.json()["job_id"], timeout=20)
    assert j["state"] == "succeeded", j
    assert reg.get("example-valve").meta["status"] == "ready"


# =========================================================================== F2


def _fake_export(app, legacy_absolute=False):
    from patent_figure import jobs as JOBS
    jobs = app.state.jobs
    job, _ = jobs.create("export", "p-x")
    (jobs.root / job.id).mkdir(parents=True, exist_ok=True)
    (jobs.root / job.id / "bundle.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    url = ("http://127.0.0.1:%d/v1/exports/%s/download" % (PORT, job.id) if legacy_absolute
           else "/v1/exports/%s/download" % job.id)
    job.result = {"export_id": job.id, "ok": True, "download_url": url}
    job._set_state(JOBS.SUCCEEDED)
    job.emit(JOBS.EV_DONE, job.result, stage=JOBS.SUCCEEDED, progress=1.0)
    return job


@pytest.mark.parametrize("legacy", [False, True])
def test_download_url_is_same_origin_relative_without_a_public_url(tmp_path, legacy):
    """F2 (修复轮 1): without a configured public URL the download URL is SAME-ORIGIN RELATIVE —
    opened from localhost or from 127.0.0.1 the browser resolves it on the page's own host, so
    the session cookie goes with it. Records written before WP2b (absolute 127.0.0.1) are
    rebuilt the same way; the stored record is untouched."""
    import workbench as W
    from starlette.testclient import TestClient
    _W, reg, app, _c = _build(tmp_path / "data")
    job = _fake_export(app, legacy_absolute=legacy)
    rel = "/v1/exports/%s/download" % job.id
    for host in ("localhost", "127.0.0.1"):
        c = TestClient(W.Dispatcher(app, reg), base_url="http://%s:%d" % (host, PORT))
        assert c.get("/?token=" + TOKEN).status_code == 200          # session cookie
        for path in ("/v1/exports/%s" % job.id, "/v1/jobs/%s" % job.id):
            res = c.get(path).json()["result"]                         # cookie only
            assert res["download_url"] == rel and res["download_path"] == rel, res
        d = c.get(res["download_url"])                                 # cookie only
        assert d.status_code == 200 and d.content.startswith(b"PK")
    assert app.state.jobs.get(job.id).result["download_url"].endswith("/download")


def test_a_hard_wired_loopback_url_was_the_bug(tmp_path):
    """F2 root cause, pinned: the cookie a localhost page got is not sent to 127.0.0.1."""
    import workbench as W
    from starlette.testclient import TestClient
    _W, reg, app, _c = _build(tmp_path / "data")
    job = _fake_export(app)
    c = TestClient(W.Dispatcher(app, reg), base_url="http://localhost:%d" % PORT)
    c.get("/?token=" + TOKEN)
    assert c.get("http://localhost:%d/v1/exports/%s/download" % (PORT, job.id)).status_code == 200
    assert c.get("http://127.0.0.1:%d/v1/exports/%s/download" % (PORT, job.id)).status_code == 401


def _served(tmp_path, argv, base_url, client=("127.0.0.1", 50000)):
    """The app exactly as ``workbench.py serve <argv>`` builds it: deploy options parsed by the
    same argparse wiring (deploy_opts.add_arguments / from_args) → workbench.serve_app."""
    import argparse
    import workbench as W
    from patent_figure import deploy_opts as D
    from starlette.testclient import TestClient
    ap = argparse.ArgumentParser()
    D.add_arguments(ap)
    opts = D.from_args(ap.parse_args(argv), environ={})
    reg = W.Registry(tmp_path / "data", TOKEN)
    served = W.serve_app(reg, TOKEN, PORT, opts)
    inner = getattr(served, "app", served)                 # DeployGuard → Dispatcher
    return reg, inner.main, TestClient(served, base_url=base_url, client=client)


def _sse_done(c, job, headers):
    from patent_figure import jobs as JOBS
    job.wait_for_event_after = lambda last, timeout: JOBS.Job.wait_for_event_after(job, last, 0.05)
    with c.stream("GET", "/v1/jobs/%s/events" % job.id, headers=headers) as r:
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            if "event: done" in buf and buf.rstrip().endswith("}"):
                break
    return json.loads(buf.split("event: done", 1)[1].split("data: ", 1)[1].split("\n", 1)[0])


def test_public_url_from_the_real_serve_options_drives_query_sse_and_download(tmp_path):
    """F2 (修复轮 1): ``serve --public-url`` behind a trusted reverse proxy — the export query,
    the SSE done frame and MCP all carry the public URL; the download through it works."""
    reg, main, c = _served(tmp_path, ["--host", "0.0.0.0", "--allow-remote",
                                      "--public-url", "https://figs.example.org",
                                      "--trusted-proxy", "172.30.57.10"],
                           "https://figs.example.org", client=("172.30.57.10", 4321))
    job = _fake_export(main)
    want = "https://figs.example.org/v1/exports/%s/download" % job.id
    assert c.get("/v1/exports/%s" % job.id, headers=BEARER).json()["result"]["download_url"] == want
    assert _sse_done(c, job, BEARER)["data"]["download_url"] == want
    r = c.post("/mcp", headers=BEARER, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "get_job",
                                                        "arguments": {"job_id": job.id}}})
    assert r.json()["result"]["structuredContent"]["result"]["download_url"] == want
    assert c.get(want, headers=BEARER).status_code == 200


@pytest.mark.parametrize("host", ["figs.internal", "127.0.0.1:%d" % PORT])
def test_behind_a_proxy_without_public_url_the_url_never_names_a_host(tmp_path, host):
    """F2 (修复轮 1): a reverse proxy without --public-url — whether it forwards its own Host or
    rewrites it to the upstream 127.0.0.1:<port> — gets a same-origin relative URL in the query
    and the SSE frame (no loopback address, no upstream port); the relative download works."""
    reg, main, c = _served(tmp_path, ["--host", "0.0.0.0", "--allow-remote",
                                      "--allowed-host", "figs.internal",
                                      "--trusted-proxy", "172.30.57.10"],
                           "https://figs.internal", client=("172.30.57.10", 4321))
    job = _fake_export(main)
    rel = "/v1/exports/%s/download" % job.id
    hdr = dict(BEARER, Host=host, **{"X-Forwarded-Proto": "https"})
    res = c.get("/v1/exports/%s" % job.id, headers=hdr).json()["result"]
    assert res["download_url"] == rel and "127.0.0.1" not in json.dumps(res)
    assert _sse_done(c, job, hdr)["data"]["download_url"] == rel
    assert c.get(rel, headers=hdr).status_code == 200


def test_local_default_serve_is_relative_for_browsers_and_absolute_for_mcp(tmp_path):
    _reg, main, c = _served(tmp_path, [], "http://localhost:%d" % PORT)
    job = _fake_export(main)
    rel = "/v1/exports/%s/download" % job.id
    assert c.get("/v1/exports/%s" % job.id, headers=BEARER).json()["result"]["download_url"] == rel
    r = c.post("/mcp", headers=BEARER, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "get_job",
                                                        "arguments": {"job_id": job.id}}})
    assert r.json()["result"]["structuredContent"]["result"]["download_url"] == \
        "http://127.0.0.1:%d%s" % (PORT, rel)


def test_a_malformed_host_is_never_echoed_into_the_url(tmp_path):
    _W, _reg, app, c = _build(tmp_path / "data")
    job = _fake_export(app)
    r = c.get("/v1/exports/%s" % job.id, headers={**BEARER, "Host": "localhost:%d@evil.example" % PORT})
    assert r.status_code == 200
    assert r.json()["result"]["download_url"] == "/v1/exports/%s/download" % job.id


# =========================================================================== F3 / F4 (pure)


def _asm(*names, degenerate=()):
    return {"source": {"sha256": "x"}, "parts": [
        {"name": n, "instances": 1, "bbox_size": [1, 1, 1], "centers": [[0, 0, 0]],
         "degenerate": n in degenerate} for n in names]}


def _plan(terms, figures=None):
    return {"schema": "patent-figure-plan/1", "source": {"step": "m.stp"},
            "terms": terms,
            "figures": figures or [{"id": "fig1", "caption": "整体结构示意图", "kind": "assembly",
                                    "members": ["*"]}]}


def test_blank_terms_are_E_TERMS_MISSING_with_the_plain_hint_and_open_terms():
    from patent_figure import render_errors as RE
    asm = _asm("P-1", "P-2", "P-3", "P-4", degenerate=("P-4",))
    plan = _plan([{"selector": n, "term": "", "label": "once"} for n in ("P-1", "P-2", "P-4")]
                 + [{"selector": "P-3", "term": "底座", "label": "once"}])
    e = RE.render_failure(plan, asm, {"figures": [], "render_report": {"errors": []}})
    assert e["code"] == "E_TERMS_MISSING"
    assert e["hint_plain"] == "还有 2 个零件没有名称，去术语页填写或用 AI 起名"
    assert e["fix_action"] == {"kind": "open_terms", "target": {"figure": "fig1", "focus": "names"}}
    assert e["unnamed_parts"] == ["P-1", "P-2"] and e["unnamed_count"] == 2
    assert e["title_zh"] and e["message"] and e["hint"]


def test_other_plan_errors_stay_E_RENDER_NOT_OK_with_a_cause_and_a_fix():
    from patent_figure import render_errors as RE
    asm = _asm("P-1")
    plan = _plan([{"selector": "P-1", "term": "底座", "label": "once"}],
                 [{"id": "fig1", "caption": "甲", "kind": "assembly", "members": ["P-1"]},
                  {"id": "figX", "caption": "坏", "kind": "assembly", "members": ["NOPE"]}])
    e = RE.render_failure(plan, asm, {})
    assert e["code"] == "E_RENDER_NOT_OK" and e["cause_code"] == "E_MEMBER_NO_MATCH"
    assert e["figure"] == "figX"
    assert e["fix_action"] == {"kind": "split_figure", "target": {"figure": "figX"}}
    assert e["issues"] and e["hint_plain"]


def test_layout_and_qa_failures_are_structured_per_figure():
    from patent_figure import qa as QA
    from patent_figure import render_errors as RE
    asm = _asm("P-1", "P-2")
    plan = _plan([{"selector": "P-*", "term": "零件", "label": "once"}],
                 [{"id": "fig1", "caption": "甲", "kind": "assembly", "members": ["P-1"]},
                  {"id": "fig2", "caption": "乙", "kind": "exploded", "members": ["P-*"]},
                  {"id": "fig3", "caption": "丙", "kind": "exploded", "members": ["P-2"]}])
    qa_bad = {"pass": False, "checks": [
        dict({"id": "geometry_occupancy", "pass": True}, **QA.plain_fields("geometry_occupancy", "fig3")),
        dict({"id": "text_height_mm", "pass": False}, **QA.plain_fields("text_height_mm", "fig3"))]}
    result = {"figures": [{"id": "fig1", "pass": True},
                          {"id": "fig3", "pass": False, "qa": qa_bad},
                          {"id": "fig2", "pass": False, "missing": True}],
              "render_report": {"errors": [{
                  "figure": "fig2", "code": "E_LAYOUT_UNSOLVABLE",
                  "message": "layout_exploded: 页面填充 0.308 < 0.55（旋转也救不回来）"}]}}
    e = RE.render_failure(plan, asm, result)
    assert e["code"] == "E_RENDER_NOT_OK"
    rows = {r["figure"]: r for r in e["figures"]}
    assert set(rows) == {"fig2", "fig3"} and [r["figure"] for r in e["figures"]] == ["fig2", "fig3"]
    assert rows["fig2"]["code"] == "E_LAYOUT_UNSOLVABLE" and rows["fig2"]["number"] == 2
    assert rows["fig2"]["fix_action"] == {"kind": "change_view", "target": {"figure": "fig2"}}
    assert "0.308" in rows["fig2"]["hint_plain"] and "页面填充" in rows["fig2"]["detail"]
    assert rows["fig3"]["code"] == "E_QA_FAILED" and rows["fig3"]["check"] == "text_height_mm"
    assert rows["fig3"]["fix_action"] == QA.plain_fields("text_height_mm", "fig3")["fix_action"]
    assert e["cause_code"] == "E_LAYOUT_UNSOLVABLE" and e["figure"] == "fig2"
    assert e["fix_action"] == rows["fig2"]["fix_action"] and "图2" in e["hint_plain"]


def test_layout_error_table_agrees_with_the_qa_table():
    """F4: an error that means the same as a QA check takes that check's plain text and fix
    action (structurally); every row is plain Chinese with a frozen fix kind or None."""
    from patent_figure import qa as QA
    kinds = {"open_terms", "split_figure", "change_view", "change_density"}
    cases = [("E_LAYOUT_UNSOLVABLE", "页面填充 0.308 < 0.55", "sheet_fill"),
             ("E_LAYOUT_UNSOLVABLE", "最大可用字高 2.55 mm < 交付下限 3.5 mm", "text_height_mm"),
             ("E_LAYOUT_UNSOLVABLE", "全体几何退化为一点", "sheet_fill")]
    cases += [(code, "", check) for code, (_t, check) in QA.ERROR_AS_CHECK.items()]
    for code, msg, check in cases:
        row = QA.error_plain_fields(code, "fig5", msg)
        assert row["check"] == check
        assert row["fix_action"] == QA.plain_fields(check, "fig5")["fix_action"], code
    for code in list(QA.ERROR_PLAIN) + ["E_RENDER", "E_INTERNAL", "E_NO_PRINCIPAL_AXIS"]:
        row = QA.error_plain_fields(code, "fig5", "")
        act = row["fix_action"]
        assert act is None or (act["kind"] in kinds and act["target"]["figure"] == "fig5"), code
        if act is None:
            assert "反馈" in row["hint_plain"]
    for code, msg, _c in cases + [(c, "", None) for c in QA.ERROR_PLAIN]:
        row = QA.error_plain_fields(code, "fig5", msg)
        assert re.search(r"[一-鿿]", row["title_zh"]), code
        assert not re.search(r"[A-Za-z_\[\]{}\"=:]", row["hint_plain"]), (code, row["hint_plain"])
    # the frontend's static map (WP3b LAYOUT_ERRORS) lists these as the first fix — same kinds
    assert QA.error_plain_fields("E_LABELS_UNPLACEABLE", "f")["fix_action"]["kind"] == "open_terms"
    assert QA.error_plain_fields("E_LAYOUT_UNSOLVABLE", "f")["fix_action"]["kind"] == "change_view"


def test_candidate_verdicts_carry_a_fix_action_aimed_at_the_candidate():
    """F4: a candidate blocked at the layout stage (no QA rows) now carries the fix action; QA
    rows of a candidate point at the candidate, not at the trial plan's fig1."""
    from patent_figure import qa as QA
    from patent_figure import suggest_plan as SUG
    c = {"id": "c1"}
    SUG.verdict(c, {"figures": [], "errors": [{"figure": "fig1", "code": "E_LAYOUT_UNSOLVABLE",
                                               "message": "页面填充 0.308 < 0.55"}]})
    assert c["status"] == "fail" and c["qa"]["error_code"] == "E_LAYOUT_UNSOLVABLE"
    assert c["fix_action"] == {"kind": "change_view", "target": {"figure": "c1"}}
    assert c["qa"]["fix_action"] == c["fix_action"] and c["qa"]["hint_plain"]
    assert c["reason"].startswith("被闸门拦下")                     # WP2 wording unchanged
    c2 = {"id": "c4"}
    qa = {"pass": False, "checks": [dict({"id": "leader_crossing", "pass": False},
                                         **QA.plain_fields("leader_crossing", "fig1"))]}
    SUG.verdict(c2, {"figures": [{"id": "fig1", "gate_pass": False, "qa": qa}], "errors": []})
    assert c2["qa"]["failed"][0]["fix_action"] == {"kind": "split_figure",
                                                    "target": {"figure": "c4"}}
    assert c2["fix_action"] == c2["qa"]["failed"][0]["fix_action"]
    c3 = {"id": "c2"}
    SUG.verdict(c3, {"figures": [{"id": "fig1", "gate_pass": True, "qa": {"checks": []},
                                  "text_height_mm": 5, "labels": 3}], "errors": []})
    assert c3["status"] == "pass" and c3["fix_action"] is None


def test_job_error_carries_the_structured_fields_into_the_error_event():
    """F3 plumbing: jobs.run passes ``error_fields`` through to job.error and the SSE error
    event — never overriding code / message / hint."""
    from patent_figure import jobs as JOBS
    store = JOBS.JobStore(None)
    job, _ = store.create("render", "p")

    def fn(_j):
        err = RuntimeError("计划校验未通过")
        err.code = "E_TERMS_MISSING"
        err.hint = "填 terms"
        err.error_fields = {"hint_plain": "还有 3 个零件没有名称，去术语页填写或用 AI 起名",
                            "fix_action": {"kind": "open_terms", "target": {"figure": "fig1"}},
                            "code": "SHOULD-NOT-WIN"}
        raise err
    store.run(job, fn)
    assert job.error["code"] == "E_TERMS_MISSING" and job.error["hint"] == "填 terms"
    assert job.error["fix_action"]["kind"] == "open_terms"
    last = job.events[-1]
    assert last["type"] == "error" and last["data"]["hint_plain"].startswith("还有 3 个零件")


# --------------------------------------------------------------- F3/F4 through render_version


def _gate_fail_cli(fig2_fails="qa"):
    """A run_cli standing in for render_patent_figure.py --in-place + annotate_figure_sheet.py:
    fig1 clears the gate; fig2 is rendered but QUARANTINED into diag/ — its QA report (or HLR
    drop) lives only in the renderer's --json report, exactly like the real renderer."""
    import subprocess
    from patent_figure import qa as QA

    def checks(fid, failing):
        rows = []
        for cid in ("geometry_occupancy", "leader_crossing"):
            row = {"id": cid, "pass": cid not in failing, "value": 0, "threshold": "0",
                   "detail": "d", "hint": "h"}
            row.update(QA.plain_fields(cid, fid))
            rows.append(row)
        return {"schema": "patent-figure-qa/1", "file": fid + ".dxf",
                "pass": not failing, "checks": rows}

    def run_cli(argv, timeout=0, **_kw):
        argv = [str(a) for a in argv]
        if argv[0].endswith("render_patent_figure.py"):
            out = Path(argv[argv.index("-o") + 1])
            qa1 = checks("fig1", ())
            qa2 = checks("fig2", ("leader_crossing",) if fig2_fails == "qa" else ())
            (out / "fig1.dxf").write_text("DXF fig1", encoding="utf-8")
            (out / "fig1.qa.json").write_text(json.dumps(qa1), encoding="utf-8")
            (out / "diag").mkdir(exist_ok=True)
            (out / "diag" / "fig2.dxf").write_text("DXF fig2", encoding="utf-8")
            (out / "diag" / "fig2.qa.json").write_text(json.dumps(qa2), encoding="utf-8")
            (out / "reference-numerals.json").write_text(
                json.dumps({"numerals": [], "description_zh": "附图标记说明：无"}), encoding="utf-8")
            Path(argv[argv.index("--json") + 1]).write_text(json.dumps({
                "pass": False, "errors": [], "figures": [
                    {"id": "fig1", "gate_pass": True, "qa": qa1, "hlr_dropped": [0, 0],
                     "text_height_mm": 5.0, "labels": 1},
                    {"id": "fig2", "gate_pass": False, "qa": qa2,
                     "hlr_dropped": [3, 0] if fig2_fails == "hlr" else [0, 0],
                     "text_height_mm": 3.5, "labels": 1}]}), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 1, "figure fig2：QA 未通过", "")
        if argv[0].endswith("annotate_figure_sheet.py"):
            dst = Path(argv[argv.index("-o") + 1])
            dst.write_text("ANN " + dst.name, encoding="utf-8")
            Path(argv[argv.index("--json") + 1]).write_text(json.dumps(
                {"caption": "图", "table": None, "warnings": [],
                 "figure_description": "图为示意图；"}), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError("unexpected CLI %s" % argv[0])
    return run_cli


_VALID_ASM = {"source": {"sha256": "x"}, "parts": [
    {"name": "SYN-A01", "instances": 1, "bbox_size": [1, 2, 3], "centers": [[0, 0, 0]]},
    {"name": "SYN-B02", "instances": 1, "bbox_size": [2, 2, 2], "centers": [[5, 0, 0]]}]}


def _valid_plan(step):
    return {"schema": "patent-figure-plan/1", "source": {"step": str(step)},
            "terms": [{"selector": "SYN-A01", "term": "底座", "label": "once"},
                      {"selector": "SYN-B02", "term": "回转座", "label": "once"}],
            "figures": [{"id": "fig1", "caption": "甲", "kind": "assembly", "members": ["SYN-A01"]},
                        {"id": "fig2", "caption": "乙", "kind": "assembly", "members": ["SYN-B02"]}]}


@pytest.mark.parametrize("kind", ["qa", "hlr"])
def test_render_version_keeps_the_quarantined_figure_detail_for_the_classifier(tmp_path, kind):
    """R2b1 #2: a figure the gate quarantined into diag/ keeps its QA report / HLR counts from the
    renderer's report through the REAL adapter (studio_ext.render_version), so the failure is a
    QA (or HLR) failure with its fix — not "the program produced no figure"."""
    import studio_ext as EXT
    from patent_figure import render_errors as RE
    vd = tmp_path / "v1"
    (vd / "inputs").mkdir(parents=True)
    plan = _valid_plan(tmp_path / "m.stp")
    (vd / "inputs" / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (vd / "inputs" / "assembly.json").write_text(json.dumps(_VALID_ASM), encoding="utf-8")
    ok, result, _m = EXT.render_version(vd, vd / "inputs" / "plan.json", vd / "inputs" / "assembly.json",
                                        tmp_path / "cache", _gate_fail_cli(kind), lambda p: "<svg/>")
    assert ok is False
    row = next(f for f in result["figures"] if f["id"] == "fig2")
    assert row["quarantined"] and row["qa"] and row["pass"] is False
    e = RE.render_failure(plan, _VALID_ASM, result)
    fig2 = e["figures"][0]
    assert fig2["figure"] == "fig2" and fig2["code"] != "E_FIGURE_MISSING"
    if kind == "qa":
        assert fig2["code"] == "E_QA_FAILED" and fig2["check"] == "leader_crossing"
        assert fig2["fix_action"] == {"kind": "split_figure", "target": {"figure": "fig2"}}
    else:
        assert fig2["code"] == "E_HLR_DROPPED" and "边 3" in fig2["detail"]
        assert fig2["fix_action"]["kind"] == "change_view"


def test_render_job_error_sse_and_figure_diagnostics_keep_the_qa_reason(tmp_path, monkeypatch):
    """R2b1 #2 end to end through the workbench render job (real publish service + real
    render_version, only the two CLIs faked): the job error, its SSE error frame, the REST
    figures and the browser's render/status all carry the QA reason and fix action."""
    import plan_studio as PS
    import workbench as W
    from patent_figure import jobs as JOBS
    monkeypatch.setattr(W.WK, "get_shared_worker", lambda: None)
    monkeypatch.setattr(PS, "run_cli", _gate_fail_cli("qa"))
    monkeypatch.setattr(PS, "dxf_to_svg", lambda path: "<svg/>")   # the fake DXFs are not DXF
    _W, reg, app, c = _build(tmp_path / "data")
    root = _seed_project(reg, "p-20261013-qafail", {"status": "ready", "message": ""})
    (root / "studio" / "assembly.json").write_text(json.dumps(_VALID_ASM), encoding="utf-8")
    (root / "studio" / "plan.json").write_text(json.dumps(_valid_plan(root / "model.stp")),
                                               encoding="utf-8")
    r = c.post("/v1/projects/p-20261013-qafail/render", headers=BEARER)
    assert r.status_code == 202, r.text
    j = _poll(c, r.json()["job_id"])
    assert j["state"] == "failed"
    err = j["error"]
    assert err["code"] == "E_RENDER_NOT_OK" and err["cause_code"] == "E_QA_FAILED"
    assert err["figures"][0]["check"] == "leader_crossing"
    assert err["fix_action"] == {"kind": "split_figure", "target": {"figure": "fig2"}}
    assert "图2" in err["hint_plain"]
    job = app.state.jobs.get(j["id"])
    job.wait_for_event_after = lambda last, timeout: JOBS.Job.wait_for_event_after(job, last, 0.05)
    with c.stream("GET", "/v1/jobs/%s/events" % j["id"], headers=BEARER) as s:
        body = "".join(s.iter_text())
    frame = json.loads(body.split("event: error", 1)[1].split("data: ", 1)[1].split("\n", 1)[0])
    assert frame["data"]["cause_code"] == "E_QA_FAILED" and frame["data"]["fix_action"]["kind"] == "split_figure"
    figs = {f["id"]: f for f in j["result"]["figures"]}
    assert [x["check"] for x in figs["fig2"]["failed_checks"]] == ["leader_crossing"]
    assert figs["fig2"]["failed_checks"][0]["fix_action"]["kind"] == "split_figure"
    st = c.get("/p/p-20261013-qafail/api/render/status", headers=BEARER).json()
    assert st["job"]["state"] == "failed" and st["job"]["error"]["cause_code"] == "E_QA_FAILED"
    assert "fig2" in st["job"]["diag"]["failed_figures"]


# =========================================================================== F5 (pure)


def test_default_figure_is_exactly_the_blank_plan_fallback(tmp_path):
    import plan_studio as PS
    from patent_figure import suggest_plan as SUG
    ws = PS.Workspace(tmp_path / "m.stp", tmp_path / "ws")
    ws.assembly.write_text(json.dumps(_asm("P-1")), encoding="utf-8")
    assert PS.blank_plan(ws)["figures"] == [SUG.DEFAULT_FIGURE]
    plan = {"figures": [dict(SUG.DEFAULT_FIGURE), {"id": "fig2", "caption": "乙",
                                                   "kind": "exploded", "members": ["P-1"]}]}
    assert SUG.drop_default_figure(plan) and [f["id"] for f in plan["figures"]] == ["fig2"]
    alone = {"figures": [dict(SUG.DEFAULT_FIGURE)]}
    assert not SUG.drop_default_figure(alone) and len(alone["figures"]) == 1   # never empties
    edited = {"figures": [dict(SUG.DEFAULT_FIGURE, caption="整机"), {"id": "fig2"}]}
    assert not SUG.drop_default_figure(edited)                                # user's figure


def test_default_figure_inputs_follow_what_decides_the_drawing():
    """R2b1 #1: the fallback figure's effective inputs change with the global view / density,
    the part selection and the label modes — not with term TEXT (naming parts after a suggestion
    must not keep a fallback the suggestion already judged)."""
    from patent_figure import suggest_plan as SUG
    asm = _asm("P-1", "P-2")
    base = _plan([{"selector": "P-*", "term": "", "label": "once"}],
                 [dict(SUG.DEFAULT_FIGURE)])
    fp = SUG.default_figure_inputs(base, asm)
    assert fp
    named = json.loads(json.dumps(base))
    named["terms"][0]["term"] = "零件"
    assert SUG.default_figure_inputs(named, asm) == fp
    for mutate in (lambda p: p.setdefault("layout", {}).update(view="front"),
                   lambda p: p.setdefault("layout", {}).update(density="loose"),
                   lambda p: p["source"].update(exclude=["P-2"]),
                   lambda p: p["terms"].insert(0, {"selector": "P-2", "term": "", "label": "none"})):
        q = json.loads(json.dumps(base))
        mutate(q)
        assert SUG.default_figure_inputs(q, asm) != fp
    assert SUG.default_figure_inputs(_plan([], [{"id": "fig1", "caption": "别的",
                                                 "kind": "assembly", "members": ["*"]}]), asm) is None


# =========================================================================== F6


def test_project_detail_matches_openapi_and_carries_the_render_job(tmp_path):
    """F6: GET /v1/projects/{id} returns render_job (ProjectRenderJob) — keys pinned against
    docs/openapi.yaml both ways (no undeclared key, every required key present)."""
    yaml = pytest.importorskip("yaml")
    from patent_figure import jobs as JOBS
    spec = yaml.safe_load((_ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    S = spec["components"]["schemas"]
    ref = spec["paths"]["/v1/projects/{pid}"]["get"]["responses"]["200"]["content"][
        "application/json"]["schema"]["$ref"]
    assert ref.endswith("/ProjectDetail")
    _W, reg, app, c = _build(tmp_path / "data")
    _seed_project(reg, "p-20261013-aaaaaa", {"status": "ready", "message": ""})
    d = c.get("/v1/projects/p-20261013-aaaaaa", headers=BEARER).json()
    props, req = S["ProjectDetail"]["properties"], S["ProjectDetail"]["required"]
    assert set(d) <= set(props), set(d) - set(props)
    assert set(req) <= set(d)
    assert set(d["structure"]) <= set(props["structure"]["properties"])
    assert d["render_job"] is None
    job, _ = app.state.jobs.create("render", "p-20261013-aaaaaa", inputs={"plan_etag": "e1"})
    job.error = {"code": "E_TERMS_MISSING", "message": "m", "hint": "h", "hint_plain": "p",
                 "fix_action": {"kind": "open_terms", "target": {"figure": "fig1"}},
                 "unnamed_parts": ["x"] * 3}
    job._set_state(JOBS.FAILED)
    rj = c.get("/v1/projects/p-20261013-aaaaaa", headers=BEARER).json()["render_job"]
    rprops, rreq = S["ProjectRenderJob"]["properties"], S["ProjectRenderJob"]["required"]
    assert set(rj) == set(rprops), set(rj) ^ set(rprops)
    assert set(rreq) <= set(rj)
    assert rj["job_id"] == job.id and rj["state"] == "failed" and rj["ok"] is False
    assert rj["published"] is False and rj["plan_etag"] == "e1"
    assert rj["error"]["code"] == "E_TERMS_MISSING" and "unnamed_parts" not in rj["error"]
    assert rj["url"] == "/v1/jobs/%s" % job.id
    assert rj["status_url"] == "/p/p-20261013-aaaaaa/api/render/status"
    # the list entry (GET /v1/projects) is the same summary — every key declared
    for p in c.get("/v1/projects", headers=BEARER).json()["projects"]:
        assert set(p) <= set(props)


def test_openapi_declares_every_wp2b_field():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load((_ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    S = spec["components"]["schemas"]
    assert {"title_zh", "hint_plain", "fix_action", "figures", "cause_code"} <= set(
        S["Error"]["properties"])
    assert set(S["PrepareResult"]["properties"]["ai_status"]["enum"]) == {
        "ok", "failed", "skipped", "not_configured"}
    assert {"download_url", "download_path"} <= set(S["ExportResult"]["properties"])
    assert "fix_action" in S["Candidate"]["properties"]
    assert "fix_action" not in S["Candidate"]["required"]          # additive only
    assert "keep_default_figure" in S["AcceptCandidatesRequest"]["properties"]
    assert "removed_default_figure" in S["AcceptCandidatesResult"]["properties"]


# =========================================================================== live (OCP)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """Module-scoped, so it is set up BEFORE the function-scoped config isolation — it isolates
    the LLM config itself for its whole lifetime (no real channel is ever called from here)."""
    if not _HAS_OCP:
        pytest.skip("需要 cadquery-ocp")
    mp = pytest.MonkeyPatch()
    cfg = tmp_path_factory.mktemp("wp2b-cfg")
    mp.setattr(L, "CONFIG_DIR", cfg)
    mp.setattr(L, "CONFIG_FILE", cfg / "config.json")
    mp.delenv("DEEPSEEK_API_KEY", raising=False)
    mp.setattr(L, "_codebuddy_path", lambda: None)
    try:
        data = tmp_path_factory.mktemp("wp2b") / "data"
        W, reg, app, c = _build(data)
        assert L.public_settings()["provider"] == "none"
        yield W, reg, app, c
    finally:
        mp.undo()


def _upload_step(c, path: Path, title: str):
    r = c.post("/v1/projects?title=%s&name=%s" % (title, path.name), headers=BEARER,
               content=path.read_bytes())
    assert r.status_code == 202, r.text
    ref = r.json()
    j = _poll(c, ref["job_id"], timeout=300)
    assert j["state"] == "succeeded", j
    assert j["result"]["ai_status"] == "not_configured"
    return ref["project_id"]


def test_live_render_of_an_unnamed_upload_fails_with_E_TERMS_MISSING(live):
    """F3 on the real golden path: upload a STEP, render straight away → the job error (and its
    SSE error event) says how many parts are unnamed and points at the terms page."""
    _W, _reg, app, c = live
    pid = _upload_step(c, _ROOT / "tests" / "fixtures" / "synthetic.stp", "未起名")
    plan = c.get("/v1/projects/%s/plan" % pid, headers=BEARER).json()
    n = len({t["selector"] for t in plan["terms"] if not t["term"]})
    r = c.post("/v1/projects/%s/render" % pid, headers=BEARER)
    j = _poll(c, r.json()["job_id"], timeout=300)
    assert j["state"] == "failed"
    err = j["error"]
    assert err["code"] == "E_TERMS_MISSING", err
    assert err["hint_plain"] == "还有 %d 个零件没有名称，去术语页填写或用 AI 起名" % n
    assert err["fix_action"]["kind"] == "open_terms"
    assert err["fix_action"]["target"]["focus"] == "names"
    ev = app.state.jobs.get(j["id"]).events[-1]
    assert ev["type"] == "error" and ev["data"]["code"] == "E_TERMS_MISSING"
    rj = c.get("/v1/projects/%s" % pid, headers=BEARER).json()["render_job"]
    assert rj["job_id"] == j["id"] and rj["error"]["code"] == "E_TERMS_MISSING"


@pytest.fixture(scope="module")
def vise_upload(live):
    """The vise demo STEP uploaded as a NEW project (blank plan with the fallback figure) and
    its candidates — the whole-machine candidate of the vise is blocked at the layout stage."""
    W, reg, _app, c = live
    r = c.post("/v1/projects/from-example", headers=BEARER, json={"example": "vise"})
    assert _poll(c, r.json()["job_id"], timeout=600)["state"] == "succeeded"
    step = reg.data / "examples" / W.EXAMPLES_META["vise"]["step"]
    pid = _upload_step(c, step, "台虎钳上传")
    r = c.post("/v1/projects/%s/plan/suggest" % pid, headers=BEARER, json={})
    j = _poll(c, r.json()["job_id"], timeout=600)
    assert j["state"] == "succeeded", j
    return pid, j


def test_live_blocked_candidates_carry_fix_actions(live, vise_upload):
    """F4 on the real golden path: every blocked candidate carries a fix action; one blocked at
    the layout stage maps through the same table as qa.py."""
    from patent_figure import qa as QA
    _pid, j = vise_upload
    cands = j["result"]["candidates"]
    failed = [x for x in cands if x["status"] == "fail"]
    assert failed, "台虎钳应有被拦的候选（整机图铺不满纸面）"
    layout = [x for x in failed if (x.get("qa") or {}).get("error_code")]
    assert layout, [x.get("reason") for x in failed]
    for x in failed:
        assert "fix_action" in x
    for x in layout:
        want = QA.error_plain_fields(x["qa"]["error_code"], x["id"], x.get("detail") or "")
        assert x["fix_action"] == want["fix_action"] and x["fix_action"] is not None
        assert x["fix_action"]["target"]["figure"] == x["id"]
    for x in cands:
        if x["status"] == "pass":
            assert x["fix_action"] is None


def test_live_accept_drops_the_failing_fallback_figure_unless_asked_to_keep(live, vise_upload):
    """F5: accepting candidates on an upload whose whole-machine candidate failed removes the
    untouched initial 整体结构示意图 (response says so); keep_default_figure keeps it."""
    from patent_figure import suggest_plan as SUG
    _W, _reg, _app, c = live
    pid, j = vise_upload
    whole = [x for x in j["result"]["candidates"] if x["source"]["kind"] == "whole"]
    assert whole and whole[0]["status"] == "fail"
    passed = next(x for x in j["result"]["candidates"] if x["status"] == "pass")
    p0 = c.get("/v1/projects/%s/plan" % pid, headers=BEARER)
    assert SUG.DEFAULT_FIGURE in p0.json()["figures"]
    r = c.post("/v1/projects/%s/plan/candidates/accept" % pid,
               headers={**BEARER, "If-Match": p0.headers["etag"]},
               json={"job_id": j["id"], "candidate_ids": [passed["id"]]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["removed_default_figure"] is True and out["added_figures"] == ["fig2"]
    plan = c.get("/v1/projects/%s/plan" % pid, headers=BEARER).json()
    assert [f["id"] for f in plan["figures"]] == ["fig2"]
    # put the fallback back, accept again asking to keep it
    plan["figures"].insert(0, dict(SUG.DEFAULT_FIGURE))
    cur = c.get("/v1/projects/%s/plan" % pid, headers=BEARER).headers["etag"]
    assert c.put("/v1/projects/%s/plan" % pid, headers={**BEARER, "If-Match": cur},
                 json=plan).status_code == 200
    r = c.post("/v1/projects/%s/plan/candidates/accept" % pid, headers=BEARER,
               json={"job_id": j["id"], "candidate_ids": [passed["id"]],
                     "keep_default_figure": True})
    assert r.status_code == 200 and r.json()["removed_default_figure"] is False
    figs = c.get("/v1/projects/%s/plan" % pid, headers=BEARER).json()["figures"]
    assert SUG.DEFAULT_FIGURE in figs


def test_live_changing_the_global_view_after_suggesting_keeps_the_fallback(live):
    """R2b1 #1 regression: suggest → change the GLOBAL view (the fallback figure inherits it) →
    accept an old candidate with the CURRENT ETag → the fallback figure is kept (the failed
    whole-machine verdict was about the old view)."""
    from patent_figure import suggest_plan as SUG
    W, reg, _app, c = live
    if reg.get("example-vise") is None:
        r = c.post("/v1/projects/from-example", headers=BEARER, json={"example": "vise"})
        assert _poll(c, r.json()["job_id"], timeout=600)["state"] == "succeeded"
    pid = _upload_step(c, reg.data / "examples" / W.EXAMPLES_META["vise"]["step"], "改视角")
    r = c.post("/v1/projects/%s/plan/suggest" % pid, headers=BEARER, json={})
    j = _poll(c, r.json()["job_id"], timeout=600)
    whole = [x for x in j["result"]["candidates"] if x["source"]["kind"] == "whole"]
    assert whole and whole[0]["status"] == "fail"
    passed = next(x for x in j["result"]["candidates"] if x["status"] == "pass")
    p0 = c.get("/v1/projects/%s/plan" % pid, headers=BEARER)
    plan = p0.json()
    plan.setdefault("layout", {})["view"] = "front" if plan["layout"].get("view") != "front" else "top"
    p1 = c.put("/v1/projects/%s/plan" % pid, headers={**BEARER, "If-Match": p0.headers["etag"]},
               json=plan)
    assert p1.status_code == 200
    r = c.post("/v1/projects/%s/plan/candidates/accept" % pid,
               headers={**BEARER, "If-Match": p1.headers["etag"]},
               json={"job_id": j["id"], "candidate_ids": [passed["id"]]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["removed_default_figure"] is False and "已改动" in out["default_figure_reason"]
    figs = c.get("/v1/projects/%s/plan" % pid, headers=BEARER).json()["figures"]
    assert SUG.DEFAULT_FIGURE in figs and len(figs) == 2
