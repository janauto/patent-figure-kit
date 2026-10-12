"""WP2 HTTP 面：能力接口、鉴权六路径、候选分图作业与接受、AI 起名接力语义、导出包参数。

无 OCP 的部分（能力接口、openapi 交叉核对）总是跑；其余需要 cadquery-ocp 出图，没装则跳过。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

try:
    import OCP  # noqa: F401
    _HAS_OCP = True
except ImportError:  # pragma: no cover
    _HAS_OCP = False

TOKEN = "t0k"
BEARER = {"Authorization": "Bearer %s" % TOKEN}
PID = "example-synthetic"


def _app(tmp_path):
    import workbench as W
    from starlette.testclient import TestClient
    reg = W.Registry(tmp_path / "data", TOKEN)
    return W, reg, TestClient(W.Dispatcher(W.build_workbench(reg, TOKEN, 8802), reg),
                              base_url="http://127.0.0.1:8802")


# --------------------------------------------------------------------------- no OCP


def test_capabilities_announce_dwg_docx_presets_and_the_cnipa_note(tmp_path):
    _W, _reg, c = _app(tmp_path)
    r = c.get("/v1/capabilities", headers=BEARER)
    assert r.status_code == 200
    cap = r.json()
    ex = cap["export"]
    assert isinstance(ex["dwg_available"], bool)
    assert ex["note"] == "精确图片参数以当期《电子申请文件格式要求说明》为准"
    assert ex["sheet_presets"]["standard"]["area_mm"] == [170.0, 257.0]
    assert ex["sheet_presets"]["conservative"]["area_mm"] == [165.0, 245.0]
    assert ex["page"]["margins_mm"] == {"top": 25.0, "left": 25.0, "bottom": 15.0, "right": 15.0}
    assert ex["dpi"]["default"] == 300 and ex["primary_formats"] == ["pdf", "docx"]
    assert ex["layout_verification"] == {"pdf": "file-geometry", "docx": "structural",
                                         "docx_note": "Word 按文档结构核验，实际排版以 Word 打开为准"}
    if not ex["dwg_available"]:
        assert ex["unavailable_reasons"]["dwg"]
    assert cap["ai"]["outbound_fields"] == ["part_names", "part_codes"]
    assert set(cap["examples"]) >= {"gearbox", "vise", "valve"}
    assert c.get("/v1/capabilities").status_code == 401


def test_wp2_routes_are_declared_in_openapi(tmp_path):
    yaml = pytest.importorskip("yaml")
    W, _reg, c = _app(tmp_path)
    spec = yaml.safe_load((_ROOT / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    for method, path in (("get", "/v1/capabilities"), ("post", "/v1/projects/{pid}/plan/suggest"),
                         ("get", "/v1/projects/{pid}/plan/candidates"),
                         ("post", "/v1/projects/{pid}/plan/candidates/accept"),
                         ("post", "/v1/projects/{pid}/ai/draft-terms"),
                         ("post", "/v1/projects/{pid}/export")):
        assert method in (spec["paths"].get(path) or {}), "%s %s" % (method, path)
    schemas = spec["components"]["schemas"]
    for name in ("Candidate", "SuggestResult", "AcceptCandidatesRequest", "Capabilities",
                 "ExportResult", "DocumentCheck", "DraftTermsResult", "QACheck"):
        assert name in schemas, name
    cand = schemas["Candidate"]["properties"]
    assert "members" in cand and "members_display" in cand and "ambiguous_members" in cand
    assert set(schemas["QACheck"]["properties"]) >= {"title_zh", "hint_plain", "fix_action"}


def test_from_example_unknown_is_404(tmp_path):
    _W, _reg, c = _app(tmp_path)
    assert c.post("/v1/projects/from-example", headers=BEARER,
                  json={"example": "nope"}).status_code == 404


# --------------------------------------------------------------------------- live (OCP)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    if not _HAS_OCP:
        pytest.skip("需要 cadquery-ocp")
    import workbench_llm as L
    # WP2b: the example's preparation must never reach a real model (not even the
    # auto-detected local CodeBuddy CLI); AI tests below patch their own fake channel.
    mp = pytest.MonkeyPatch()
    cfg = tmp_path_factory.mktemp("wp2api-cfg")
    mp.setattr(L, "CONFIG_DIR", cfg)
    mp.setattr(L, "CONFIG_FILE", cfg / "config.json")
    mp.delenv("DEEPSEEK_API_KEY", raising=False)
    mp.setattr(L, "_codebuddy_path", lambda: None)
    try:
        assert L.public_settings()["provider"] == "none"          # no channel reachable
        W, reg, c = _app(tmp_path_factory.mktemp("wp2api"))
        W.ensure_synthetic(reg)
        t0 = time.time()
        while True:
            # WP2b F1: prepared = ready AND the preparation thread finished (not "message empty")
            st = c.get("/api/wb/projects/%s" % PID, headers=BEARER).json()
            assert st["status"] != "error", st
            if st["status"] == "ready" and not reg.is_preparing(PID):
                break
            assert time.time() - t0 < 300, "示例工程准备超时"
            time.sleep(1)
        yield W, reg, c
    finally:
        mp.undo()


def _poll(c, job_id, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get("/v1/jobs/%s" % job_id, headers=BEARER).json()
        if j["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return j
        time.sleep(0.4)
    raise AssertionError("作业 %s 超时" % job_id)


@pytest.fixture(scope="module")
def export_job(live):
    from patent_figure import deliverable_check as _C
    if not _C.render_checker_available():           # R2j ruling: a PDF passes only after a render
        pytest.skip("导出 PDF 需要独立渲染器（pypdfium2）才能判通过")
    _W, _reg, c = live
    r = c.post("/v1/projects/%s/export" % PID, headers=BEARER,
               json={"variant": "both", "formats": ["pdf", "docx", "dxf"]})
    assert r.status_code == 202, r.text
    j = _poll(c, r.json()["export_id"])
    assert j["state"] == "succeeded", j
    return j


def test_export_package_is_checked_page_by_page(live, export_job):
    _W, reg, c = live
    res = export_job["result"]
    assert res["ok"] is True, res["reasons"]
    # R2k: the export directory (bundle, zip, report, result) is sealed; download verifies it
    from patent_figure import export as EX
    assert EX.verify_export_seal(reg.data / "jobs" / export_job["id"], res["seal"]["sha256"]) == []
    assert c.get("/v1/exports/%s/download" % export_job["id"], headers=BEARER).status_code == 200
    for name in ("说明书附图_递交版.pdf", "说明书附图_交底版.pdf", "说明书附图_递交版.docx",
                 "说明书附图_交底版.docx"):
        doc = res["documents"][name]
        assert doc["ok"] and doc["pages"] == doc["expected_pages"] == 2, doc
        assert doc["layout_verified"] == ("structural" if name.endswith(".docx")
                                          else "file-geometry")
        for pg in doc["page_checks"]:
            assert pg["caption"] == "图%d" % pg["page"] and pg["caption_ink"] > 0
            assert pg["min_text_mm"] >= 3.5 and pg["min_text_px"] >= 24
        assert name in res["files"]
    assert {"附图说明.txt", "核对清单.txt", "核验报告.json"} <= set(res["files"])
    # R2j ruling — hash seal: every file but the report, each document's digest as written
    assert sorted(res["sha256"]) == [f for f in res["files"] if f != "核验报告.json"]
    for name, doc in res["documents"].items():
        assert doc["status"] == "ok" and res["sha256"][doc["path"]] == doc["sha256"]
    rows = {(i["variant"], i["format"]): i for i in res["items"]}
    assert rows[("递交版", "pdf")]["role"] == "主交付" and rows[("递交版", "dxf")]["status"] == "ok"
    assert res["note"].startswith("精确图片参数")


def test_download_works_from_localhost_and_127_with_the_session_cookie(live, export_job):
    """WP2b F2: the download URL used to be hard-wired to 127.0.0.1, so a page opened from
    localhost got 401 (its session cookie belongs to localhost). Without a public URL it is now
    same-origin relative (修复轮 1): both entries download the real export with the cookie alone."""
    from starlette.testclient import TestClient
    _W, _reg, c = live
    for host in ("localhost", "127.0.0.1"):
        b = TestClient(c.app, base_url="http://%s:8802" % host)
        assert b.get("/?token=" + TOKEN).status_code == 200            # session cookie only
        res = b.get("/v1/exports/%s" % export_job["id"]).json()["result"]
        assert res["download_url"] == "/v1/exports/%s/download" % export_job["id"]
        d = b.get(res["download_url"])
        assert d.status_code == 200 and d.content[:2] == b"PK", d.status_code
        assert b.get(res["download_path"]).status_code == 200


def test_a_tampered_export_is_not_served(live):
    """R2k: the download verifies the sealed export directory first — one changed byte of the
    stored zip (or any sealed file) → 409, the zip is not served."""
    import os
    _W, reg, c = live
    r = c.post("/v1/projects/%s/export" % PID, headers=BEARER,
               json={"variant": "filing", "formats": ["dxf"]})
    j = _poll(c, r.json()["export_id"])
    assert j["state"] == "succeeded" and j["result"]["seal"]["sha256"], j
    assert c.get("/v1/exports/%s/download" % j["id"], headers=BEARER).status_code == 200
    z = reg.data / "jobs" / j["id"] / "bundle.zip"
    os.chmod(str(z), 0o644)
    data = bytearray(z.read_bytes())
    data[len(data) // 2] ^= 0x01
    z.write_bytes(bytes(data))
    bad = c.get("/v1/exports/%s/download" % j["id"], headers=BEARER)
    assert bad.status_code == 409 and "封口" in bad.text, bad.text


def test_export_rejects_bad_preset_and_dpi_up_front(live):
    _W, _reg, c = live
    for body in ({"sheet_preset": "conservative-165x245"}, {"dpi": 10}, {"dpi": 5000},
                 {"formats": ["pdf", "tiff"]}):
        r = c.post("/v1/projects/%s/export" % PID, headers=BEARER, json=body)
        assert r.status_code == 400, (body, r.text)


def test_export_dpi_72_fails_the_raster_text_height_and_quarantines_the_documents(live):
    _W, _reg, c = live
    r = c.post("/v1/projects/%s/export" % PID, headers=BEARER,
               json={"variant": "filing", "formats": ["pdf", "docx", "dxf"], "dpi": 72})
    res = _poll(c, r.json()["export_id"])["result"]
    assert res["ok"] is False
    for k, v in res["manifest"].items():
        if k.endswith(("/pdf", "/docx")):
            assert v == "fail" and "px" in res["reasons"][k], (k, res["reasons"].get(k))
        else:
            assert v == "ok"
    assert "_未通过核验/说明书附图_递交版.pdf" in res["files"]
    assert "说明书附图_递交版.pdf" not in res["files"]


# ---- 鉴权六路径：首页 / 子应用 / SSE / 下载 / REST / MCP —— Cookie 与 Bearer 都可用；
# ---- ?token= 只在兼容期可用并带弃用警告头；什么都不带 → 401。


def _six_paths(c, export_job):
    job_id = export_job["id"]
    return [
        ("home", "GET", "/api/wb/state", None),
        ("subapp", "GET", "/p/%s/api/state" % PID, None),
        ("sse", "GET", "/v1/jobs/%s/events" % job_id, None),
        ("download", "GET", "/v1/exports/%s/download" % job_id, None),
        ("rest", "GET", "/v1/projects", None),
        ("mcp", "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    ]


@pytest.mark.parametrize("cred", ["bearer", "cookie", "query", "none"])
def test_auth_six_paths(live, export_job, cred):
    _W, _reg, c = live
    for name, method, path, body in _six_paths(c, export_job):
        headers, url, cookies = {}, path, None
        if cred == "bearer":
            headers = dict(BEARER)
        elif cred == "cookie":
            cookies = {"wb_token": TOKEN}
        elif cred == "query":
            url = path + ("&" if "?" in path else "?") + "token=" + TOKEN
        c.cookies.clear()
        if cookies:
            c.cookies.set("wb_token", TOKEN)
        if method == "GET" and name == "sse":
            with c.stream("GET", url, headers=headers) as resp:
                status, rh = resp.status_code, resp.headers
                if status == 200:
                    assert "text/event-stream" in rh["content-type"]
        elif method == "GET":
            resp = c.get(url, headers=headers)
            status, rh = resp.status_code, resp.headers
        else:
            resp = c.post(url, headers=headers, json=body)
            status, rh = resp.status_code, resp.headers
        c.cookies.clear()
        if cred == "none":
            assert status == 401, (name, status)
            continue
        assert status == 200, (cred, name, status)
        if cred == "query":
            assert rh.get("deprecation") == "true", (name, dict(rh))
            assert "deprecated" in rh.get("warning", "")
        else:
            assert "deprecation" not in rh, (cred, name)


def test_home_query_bootstrap_sets_the_session_cookie(live):
    _W, _reg, c = live
    c.cookies.clear()
    r = c.get("/?token=" + TOKEN)
    assert r.status_code == 200 and "wb_token" in r.headers.get("set-cookie", "")
    c.cookies.clear()


# ---- 候选分图


@pytest.fixture(scope="module")
def suggestion(live):
    _W, reg, c = live
    studio = reg.get(PID).root / "studio"
    before = (studio / "current.json").read_bytes()
    r = c.post("/v1/projects/%s/plan/suggest" % PID, headers=BEARER, json={})
    assert r.status_code == 202, r.text
    j = _poll(c, r.json()["job_id"])
    assert j["state"] == "succeeded", j
    return j, before, studio


def test_suggest_renders_every_candidate_and_never_touches_current(live, suggestion):
    _W, _reg, c = live
    j, before, studio = suggestion
    assert (studio / "current.json").read_bytes() == before        # 不改 current
    res = j["result"]
    assert res["schema"] == "patent-figure-candidates/1"
    assert res["candidates"] and res["summary"]["pending"] == 0
    for cand in res["candidates"]:
        assert cand["status"] in ("pass", "fail") and cand["reason"]
        assert cand["version"] and cand["members"]
        assert all(set(m) == {"part"} for m in cand["members"])
    passed = [x for x in res["candidates"] if x["status"] == "pass"]
    assert passed and res["empty_reason"] is None
    svg = c.get(passed[0]["preview"]["svg"], headers=BEARER)
    assert svg.status_code == 200 and svg.text.lstrip().startswith("<svg")
    # the candidate area only serves sealed CANDIDATE versions
    assert c.get("/files/%s/cand/fig1.svg" % PID, headers=BEARER).status_code == 404
    cur = json.loads(before)["render_job_id"]
    assert c.get("/files/%s/cand/fig1.svg?v=%s" % (PID, cur), headers=BEARER).status_code == 404
    summ = c.get("/v1/projects/%s" % PID, headers=BEARER).json()["candidates"]
    assert summ["job_id"] == j["id"] and summ["summary"]["pass"] == len(passed)
    latest = c.get("/v1/projects/%s/plan/candidates" % PID, headers=BEARER).json()
    assert latest["id"] == j["id"]


def test_accept_rejects_unresolvable_members_with_422_and_writes_nothing(live, suggestion):
    _W, _reg, c = live
    j, _b, _s = suggestion
    plan0 = c.get("/v1/projects/%s/plan" % PID, headers=BEARER)
    cid = j["result"]["candidates"][0]["id"]
    r = c.post("/v1/projects/%s/plan/candidates/accept" % PID, headers=BEARER,
               json={"job_id": j["id"], "candidate_ids": [cid],
                     "members": {cid: [{"part": "NO-SUCH-PART"}, {"group": "sub:NOPE"}]}})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert len(detail["unresolved"]) == 2 and detail["unresolved"][0]["candidate"] == cid
    assert c.get("/v1/projects/%s/plan" % PID, headers=BEARER).headers["etag"] == \
        plan0.headers["etag"]
    r = c.post("/v1/projects/%s/plan/candidates/accept" % PID, headers=BEARER,
               json={"job_id": j["id"], "candidate_ids": ["c99"]})
    assert r.status_code == 422
    r = c.post("/v1/projects/%s/plan/candidates/accept" % PID,
               headers={**BEARER, "If-Match": "stale"},
               json={"job_id": j["id"], "candidate_ids": [cid]})
    assert r.status_code == 409


def test_accept_writes_exact_selectors_and_returns_the_new_etag(live, suggestion):
    _W, _reg, c = live
    j, _b, _s = suggestion
    plan0 = c.get("/v1/projects/%s/plan" % PID, headers=BEARER)
    passed = next(x for x in j["result"]["candidates"] if x["status"] == "pass")
    r = c.post("/v1/projects/%s/plan/candidates/accept" % PID,
               headers={**BEARER, "If-Match": plan0.headers["etag"]},
               json={"job_id": j["id"], "candidate_ids": [passed["id"]]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["plan_etag"] == r.headers["etag"] != plan0.headers["etag"]
    plan = c.get("/v1/projects/%s/plan" % PID, headers=BEARER).json()
    fig = next(f for f in plan["figures"] if f["id"] == out["added_figures"][0])
    assert fig["members"] == sorted(m["part"] for m in passed["members"])
    assert fig["caption"] == passed["caption"] and fig["kind"] == passed["kind"]
    # WP2b F5 negative case: the synthetic example's whole-machine candidate passes the gate, so
    # its 整体结构示意图 (same shape as the upload fallback) is kept.
    whole = [x for x in j["result"]["candidates"] if x["source"]["kind"] == "whole"]
    if whole and all(x["status"] == "pass" for x in whole):
        assert out["removed_default_figure"] is False
        assert any(f["id"] == "fig1" for f in plan["figures"])


# ---- AI 起名接力


def _fresh_plan_with_blank(c):
    r = c.get("/v1/projects/%s/plan" % PID, headers=BEARER)
    plan = r.json()
    plan["terms"] = [dict(t, term="") if t["selector"] == "SYN-A01" else t
                     for t in plan["terms"]]
    w = c.put("/v1/projects/%s/plan" % PID, headers={**BEARER, "If-Match": r.headers["etag"]},
              json=plan)
    assert w.status_code == 200
    return w.headers["etag"]


def test_draft_terms_without_a_channel_is_200_ai_not_configured(live, monkeypatch):
    W, _reg, c = live
    monkeypatch.setattr(W.LLM, "public_settings",
                        lambda: {"provider": "none", "provider_label": "未配置"})

    def boom(*a, **k):
        raise AssertionError("不得在无通道时调用大模型")
    monkeypatch.setattr(W.LLM, "draft_terms", boom)
    r = c.post("/v1/projects/%s/ai/draft-terms" % PID, headers=BEARER, json={})
    assert r.status_code == 200
    out = r.json()
    assert out["status"] == "ai_not_configured" and out["ai_configured"] is False
    assert out["outbound_fields"] == ["part_names", "part_codes"] and out["suggestions"] == []


def test_draft_terms_suggests_only_blank_terms_and_writes_only_with_apply(live, monkeypatch):
    W, _reg, c = live
    etag = _fresh_plan_with_blank(c)
    seen = {}
    monkeypatch.setattr(W.LLM, "public_settings",
                        lambda: {"provider": "deepseek", "provider_label": "测试通道"})

    def fake(asm, plan, **kw):
        seen["parts"] = sorted(p["name"] for p in asm["parts"])
        return [{"selector": n, "term": "测试底座", "label": "once", "confidence": "high",
                 "reason": "形状像底座"} for n in seen["parts"]]
    monkeypatch.setattr(W.LLM, "draft_terms", fake)
    r = c.post("/v1/projects/%s/ai/draft-terms" % PID, headers=BEARER,
               json={"selectors": ["SYN-A01", "SYN-B02", "GHOST"]})
    out = r.json()
    assert r.status_code == 200 and out["status"] == "ok"
    assert seen["parts"] == ["SYN-A01"]                       # only the blank one went out
    assert [s["selector"] for s in out["suggestions"]] == ["SYN-A01"]
    assert out["suggestions"][0]["confidence"] == "high" and out["suggestions"][0]["reason"]
    skipped = {s["selector"]: s["reason"] for s in out["skipped"]}
    assert "已有名称" in skipped["SYN-B02"] and "GHOST" in skipped
    assert out["applied"] is None and out["plan_etag"] == etag   # 默认不写 plan
    assert c.get("/v1/projects/%s/plan" % PID, headers=BEARER).headers["etag"] == etag
    bad = c.post("/v1/projects/%s/ai/draft-terms" % PID, headers={**BEARER, "If-Match": "x"},
                 json={"selectors": ["SYN-A01"], "apply": True})
    assert bad.status_code == 409
    r = c.post("/v1/projects/%s/ai/draft-terms" % PID, headers={**BEARER, "If-Match": etag},
               json={"selectors": ["SYN-A01"], "apply": True})
    out = r.json()
    assert r.status_code == 200 and out["applied"]["filled"] == ["SYN-A01"]
    assert out["plan_etag"] == r.headers["etag"] != etag
    plan = c.get("/v1/projects/%s/plan" % PID, headers=BEARER).json()
    assert next(t for t in plan["terms"] if t["selector"] == "SYN-A01")["term"] == "测试底座"


def test_ai_outbound_payload_carries_only_the_selected_blank_parts(live, monkeypatch):
    """R2 #1: intercept the REAL request body at ``chat_json`` (the serialisation boundary) —
    with one part selected, nothing about other parts goes out: not their STEP names, not their
    terms from the plan, not their glossary or BOM entries. REST and MCP alike."""
    W, reg, c = live
    _fresh_plan_with_blank(c)                                  # SYN-A01 blank, others named
    studio = reg.get(PID).root / "studio"
    (studio / "glossary.json").write_text(json.dumps({"terms": {
        "SYN-A01": "术语甲", "SYN-B02": "术语乙机密", "SYN-C03": "术语丙机密"}},
        ensure_ascii=False), encoding="utf-8")
    (studio / "bom.json").write_text(json.dumps({"matched": {
        "SYN-A01": {"name": "物料甲", "code": "SYN-A01"},
        "SYN-D04": {"name": "物料丁机密", "code": "SYN-D04"}}}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(W.LLM, "public_settings",
                        lambda: {"provider": "deepseek", "provider_label": "测试通道"})
    sent = []

    def fake_chat_json(system, user, **kw):
        sent.append(user)
        return {"terms": [{"selector": "SYN-A01", "term": "底座", "confidence": "high",
                           "reason": "测"}]}
    monkeypatch.setattr(W.LLM, "chat_json", fake_chat_json)
    others = ["SYN-B02", "SYN-C03", "SYN-D04", "SYN-E05", "SYN-F06", "SYN-G07", "SYN-H08",
              "回转座", "术语乙机密", "术语丙机密", "物料丁机密"]
    r = c.post("/v1/projects/%s/ai/draft-terms" % PID, headers=BEARER,
               json={"selectors": ["SYN-A01"]})
    assert r.status_code == 200 and r.json()["suggestions"], r.text
    m = c.post("/mcp", headers=BEARER, json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "ai_draft_terms",
                   "arguments": {"project_id": PID, "selectors": ["SYN-A01"], "apply": False}}})
    assert m.status_code == 200 and not m.json()["result"]["isError"], m.text
    assert len(sent) == 2
    for body in sent:
        payload = json.loads(body)
        assert "SYN-A01" in body and "术语甲" in body and "物料甲" in body
        leaked = [o for o in others if o in body]
        assert not leaked, leaked
        parts = payload["产品零件（STEP 零件名、包围盒、装配路径）"]
        assert [p["name"] for p in parts] == ["SYN-A01"]
    # without selectors = every BLANK part — still never the named ones
    sent.clear()
    c.post("/v1/projects/%s/ai/draft-terms" % PID, headers=BEARER, json={})
    assert sent and not [o for o in others if o in sent[0]]


def test_ai_apply_with_a_shared_wildcard_row_names_only_the_selected_part(live, monkeypatch):
    """R2b #1: a blank wildcard row (``SYN-*``) governs every part. Selecting SYN-A01 with
    apply=true must name SYN-A01 only — SYN-B02's name AND label stay exactly as before — via REST
    and via MCP (whose apply defaults to true)."""
    W, _reg, c = live
    r = c.get("/v1/projects/%s/plan" % PID, headers=BEARER)
    plan = r.json()
    plan["terms"] = [{"selector": "SYN-*", "term": "", "label": "all"}]
    w = c.put("/v1/projects/%s/plan" % PID, headers={**BEARER, "If-Match": r.headers["etag"]},
              json=plan)
    assert w.status_code == 200
    monkeypatch.setattr(W.LLM, "public_settings",
                        lambda: {"provider": "deepseek", "provider_label": "测试通道"})
    monkeypatch.setattr(W.LLM, "draft_terms", lambda asm, plan, **kw: [
        {"selector": p["name"], "term": "名%s" % p["name"][-1], "label": "once",
         "confidence": "high", "reason": "测"} for p in asm["parts"]])

    def governing(name):
        cur = c.get("/v1/projects/%s/plan" % PID, headers=BEARER).json()
        from patent_figure import suggest_plan as SPm
        return SPm.winning_row(cur, name)

    out = c.post("/v1/projects/%s/ai/draft-terms" % PID, headers=BEARER,
                 json={"selectors": ["SYN-A01"], "apply": True}).json()
    assert out["applied"]["filled"] == ["SYN-A01"], out
    assert governing("SYN-A01")["term"] == "名1"
    assert governing("SYN-B02") == {"selector": "SYN-*", "term": "", "label": "all"}
    m = c.post("/mcp", headers=BEARER, json={
        "jsonrpc": "2.0", "id": 11, "method": "tools/call",
        "params": {"name": "ai_draft_terms",
                   "arguments": {"project_id": PID, "selectors": ["SYN-C03"]}}})
    res = m.json()["result"]["structuredContent"]
    assert res["applied"]["filled"] == ["SYN-C03"], res
    assert governing("SYN-C03")["term"] == "名3"
    assert governing("SYN-B02") == {"selector": "SYN-*", "term": "", "label": "all"}
    assert governing("SYN-A01")["term"] == "名1"
