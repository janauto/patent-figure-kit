"""工作台：MCP 协议、大模型输出清洗、端到端（合成装配体）。

端到端部分需要 cadquery-ocp；协议与清洗部分不需要。配置目录一律指到 tmp，
绝不读写本机真实的 ~/.config/patent-workbench。
"""

from __future__ import annotations

import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import workbench_llm as L  # noqa: E402
import workbench_mcp as M  # noqa: E402

try:
    import OCP  # noqa: F401
    _HAS_OCP = True
except Exception:  # pragma: no cover
    _HAS_OCP = False


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(L, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(L, "_codebuddy_path", lambda: None)   # 测试绝不调用真实大模型
    monkeypatch.setenv("WORKBENCH_TOKEN", "t0k")


# ----------------------------------------------------------------- MCP 协议

def _rpc(method, params=None, mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}


def test_mcp_initialize_negotiates_version():
    r = M.handle_message(_rpc("initialize", {"protocolVersion": "2025-03-26"}), {})
    assert r["result"]["protocolVersion"] == "2025-03-26"
    r = M.handle_message(_rpc("initialize", {"protocolVersion": "1999-01-01"}), {})
    assert r["result"]["protocolVersion"] == M.PROTOCOL_VERSIONS[0]
    assert "tools" in r["result"]["capabilities"]


def test_mcp_notifications_get_no_reply_and_batches_work():
    assert M.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, {}) is None
    assert M.handle_payload([{"jsonrpc": "2.0", "method": "notifications/initialized"}], {}) is None
    out = M.handle_payload([_rpc("ping", mid=1), _rpc("tools/list", mid=2)], {})
    assert [o["id"] for o in out] == [1, 2]


def test_mcp_tool_errors_are_results_not_crashes():
    def boom(args):
        raise M.ToolError("坏输入")

    r = M.handle_message(_rpc("tools/call", {"name": "x", "arguments": {}}), {"x": boom})
    assert r["result"]["isError"] is True and "坏输入" in r["result"]["content"][0]["text"]
    r = M.handle_message(_rpc("tools/call", {"name": "nope"}), {})
    assert r["error"]["code"] == -32602


def test_every_tool_has_a_closed_input_schema():
    for tool in M.TOOLS:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False


# ----------------------------------------------------------------- 大模型输出清洗

def test_parse_json_tolerates_fences_and_chatter():
    assert L._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert L._parse_json('好的：{"a": [1, 2]} 以上') == {"a": [1, 2]}
    with pytest.raises(L.LLMError):
        L._parse_json("没有 JSON")


def test_draft_terms_drops_codes_and_dedupes(monkeypatch):
    monkeypatch.setattr(L, "chat_json", lambda *a, **k: {"terms": [
        {"selector": "P1", "term": "电路板", "label": "once", "confidence": "high"},
        {"selector": "P2", "term": "电路板", "label": "once", "confidence": "high"},
        {"selector": "P3", "term": "PCB-K板", "label": "once"},          # 含字母：丢弃
        {"selector": "P4", "term": "连接器", "label": "none"},
        {"selector": "ZZ", "term": "不存在的件", "label": "once"},        # 不在零件表：丢弃
    ]})
    asm = {"parts": [{"name": n, "bbox_size": [s, 1, 1]} for n, s in
                     (("P1", 10), ("P2", 60), ("P3", 5), ("P4", 3))]}
    rows = {r["selector"]: r for r in L.draft_terms(asm, {"terms": []})}
    assert set(rows) == {"P1", "P2", "P4"}
    assert rows["P2"]["term"] == "第一电路板" and rows["P1"]["term"] == "第二电路板"
    assert rows["P4"]["label"] == "none"


def test_draft_terms_never_touches_human_terms(monkeypatch):
    seen = {}

    def fake(system, user, **k):
        seen["user"] = json.loads(user)
        return {"terms": []}

    monkeypatch.setattr(L, "chat_json", fake)
    asm = {"parts": [{"name": "A"}, {"name": "B"}]}
    L.draft_terms(asm, {"terms": [{"selector": "A", "term": "底壳"}]})
    asked = [p["name"] for p in seen["user"]["产品零件（STEP 零件名、包围盒、装配路径）"]]
    assert asked == ["B"]


def test_flow_cleanup_strips_step_numbers():
    spec = L._clean_flow({"nodes": [{"id": "a", "kind": "process", "text": "S101：采集图像"},
                                    {"id": "b", "kind": "process", "text": "步骤2 计算偏差"}],
                          "edges": [{"from": "a", "to": "b", "label": ""}]}, "图名")
    assert [n["text"] for n in spec["nodes"]] == ["采集图像", "计算偏差"]
    assert "label" not in spec["edges"][0]


def test_secrets_file_is_private(tmp_path):
    L.save_config({"deepseek": {"api_key": "sk-test-0000-1234"}})
    assert (L.CONFIG_FILE.stat().st_mode & 0o777) == 0o600
    pub = L.public_settings()
    assert pub["api_key_set"] and pub["api_key_hint"] == "…1234"
    assert "sk-test" not in json.dumps(pub)


# ----------------------------------------------------------------- 端到端


@pytest.mark.skipif(not _HAS_OCP, reason="需要 cadquery-ocp 才能从 STEP 出图")
def test_workbench_end_to_end(tmp_path):
    import workbench as W
    from starlette.testclient import TestClient

    reg = W.Registry(tmp_path / "data", "t0k")
    W.ensure_synthetic(reg)
    app = W.Dispatcher(W.build_workbench(reg, "t0k", 8790), reg)
    c = TestClient(app, base_url="http://127.0.0.1:8790")
    H = {"Authorization": "Bearer t0k"}
    t0 = time.time()
    while True:
        st = c.get("/api/wb/projects/example-synthetic", headers=H).json()
        if st["status"] == "error":
            pytest.fail(st["message"])
        if st["status"] == "ready" and not st["message"]:
            break
        assert time.time() - t0 < 300
        time.sleep(1)

    # 鉴权：主应用与挂载的 Studio 子应用都要 token
    assert c.get("/api/wb/state").status_code == 401
    assert c.get("/p/example-synthetic/api/state").status_code == 401
    assert c.get("/api/wb/state", headers={"Host": "evil.example"}).status_code == 403

    state = c.get("/p/example-synthetic/api/state", headers=H).json()
    figs = state["render"]["figures"]
    assert [f["number"] for f in figs] == [1, 2]
    assert all(set(f["variants"]) == {"raw", "annotated", "filing"} for f in figs)
    assert state["render"]["figure_descriptions"][0].startswith("图1为")
    assert state["flowcharts"][0]["number"] == 3          # 流程图接在结构附图之后

    slides = c.get("/api/wb/carousel", headers=H).json()["slides"]
    assert {s["kind"] for s in slides} == {"structure", "flow"}
    assert c.get(slides[0]["src"], headers=H).headers["content-type"] == "image/png"

    def call(name, args):
        r = c.post("/mcp", headers=H, json=_rpc("tools/call", {"name": name, "arguments": args}))
        return r.json()["result"]

    assert c.post("/mcp", headers=H, json=_rpc("initialize")).status_code == 200
    res = call("list_projects", {})
    assert res["structuredContent"]["projects"][0]["id"] == "example-synthetic"
    res = call("render_flowchart", {"spec": M.FLOW_GUIDE["example"]})
    assert res["structuredContent"]["steps"][0]["step"] == "S101"
    assert any(x["type"] == "image" for x in res["content"])
    res = call("update_terms", {"project_id": "example-synthetic",
                                "terms": [{"selector": "SYN-A01", "term": "新名字"}]})
    assert res["structuredContent"]["kept_human"] == ["SYN-A01"]

    out = c.post("/p/example-synthetic/api/export", headers=H, json={"dwg": False}).json()
    z = Path(out["dir"]).with_suffix(".zip")
    names = zipfile.ZipFile(z).namelist()
    assert "说明书附图_交底版.docx" in names and "附图说明.txt" in names
    assert any(n.startswith("交底版_带件号表/") for n in names)
    assert any(n.startswith("流程图/") for n in names)


def test_compound_names_for_one_part_are_collapsed(monkeypatch):
    """同一零件名的多个实例只能有一个名称：「第一阀座、第二阀座」收成「阀座」并降为待核对。"""
    monkeypatch.setattr(L, "chat_json", lambda *a, **k: {"terms": [
        {"selector": "SEAT", "term": "第一阀座、第二阀座", "label": "once", "confidence": "high"}]})
    rows = L.draft_terms({"parts": [{"name": "SEAT", "bbox_size": [50, 8, 50]}]}, {"terms": []})
    assert rows[0]["term"] == "阀座" and rows[0]["confidence"] == "low"


# ----------------------------------------------------------------- 结构识别

_ASM = {"parts": [
    {"name": "BREP_1", "max_dim": 60, "bbox_size": [60, 60, 20], "path_sample": "ROOT/TOP_ASM/BREP_1"},
    {"name": "BREP_2", "max_dim": 30, "bbox_size": [30, 30, 10], "path_sample": "ROOT/TOP_ASM/BREP_2"},
    {"name": "PRT_9", "max_dim": 50, "bbox_size": [50, 50, 50], "path_sample": "ROOT/BASE_ASM/PRT_9"},
    {"name": "PRT_10", "max_dim": 40, "bbox_size": [40, 40, 4], "path_sample": "ROOT/BASE_ASM/PRT_10"},
    {"name": "TRAY", "max_dim": 140, "bbox_size": [140, 20, 120], "path_sample": "ROOT/TRAY"},
    {"name": "GHOST", "degenerate": True, "max_dim": 0, "path_sample": "ROOT/GHOST"},
]}
_PLAN = {"terms": [{"selector": "PRT_9", "term": "底座"}], "source": {"exclude": ["TRAY"]}}


def test_clean_structure_places_every_part_once_and_respects_plan():
    raw = {"groups": [{"name": "顶部风扇结构", "role": "顶部散热", "parts": ["BREP_1", "BREP_2", "PRT_9"]},
                      {"name": "主体结构", "role": "", "parts": ["PRT_9", "TRAY", "NOPE"]}],
           "parts": {"BREP_1": {"name": "壳体"}, "BREP_2": {"name": "壳体"},
                     "PRT_9": {"name": "机座"}, "PRT_10": {"name": "PCB板"}}}
    s = L.clean_structure(raw, _ASM, _PLAN, source="ai")
    placed = [n for g in s["groups"] for n in g["parts"]]
    assert sorted(placed) == ["BREP_1", "BREP_2", "PRT_10", "PRT_9"]        # 排除件、退化件不在
    assert len(placed) == len(set(placed))                                   # 每件只在一组
    assert s["groups"][-1]["name"] == "其他零件" and s["groups"][-1]["parts"] == ["PRT_10"]
    assert s["names"]["PRT_9"]["name"] == "底座"                             # 人工名称优先
    assert {s["names"]["BREP_1"]["name"], s["names"]["BREP_2"]["name"]} == {"第一壳体", "第二壳体"}
    assert "PRT_10" not in s["names"]                                        # 含字母的名字丢弃


def test_fallback_structure_groups_by_assembly_path():
    s = L.fallback_structure(_ASM, _PLAN)
    assert s["source"] == "fallback"
    assert [sorted(g["parts"]) for g in s["groups"]] == [["BREP_1", "BREP_2"], ["PRT_10", "PRT_9"]]


@pytest.mark.skipif(not _HAS_OCP, reason="需要 cadquery-ocp 才能从 STEP 准备工程")
def test_structure_snapshot_and_showcase_endpoints(tmp_path, monkeypatch):
    import workbench as W
    from starlette.testclient import TestClient

    reg = W.Registry(tmp_path / "data", "t0k")
    W.ensure_synthetic(reg)
    t0 = time.time()
    while reg.get("example-synthetic").meta.get("status") != "ready" or \
            reg.get("example-synthetic").meta.get("message"):
        assert time.time() - t0 < 300
        time.sleep(1)
    c = TestClient(W.Dispatcher(W.build_workbench(reg, "t0k", 8790), reg),
                   base_url="http://127.0.0.1:8790")
    H = {"Authorization": "Bearer t0k"}
    st = c.get("/p/example-synthetic/api/structure", headers=H).json()
    assert st["structure"]["source"] == "fallback" and st["structure"]["groups"]
    # 展示图：只收 PNG，名字受限
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    assert c.post("/p/example-synthetic/api/snapshot?name=../x", headers=H, content=png).status_code == 422
    assert c.post("/p/example-synthetic/api/snapshot?name=xray", headers=H, content=b"GIF89a").status_code == 422
    assert c.post("/p/example-synthetic/api/snapshot?name=xray", headers=H, content=png).json()["name"] == "xray.png"
    assert c.get("/files/example-synthetic/snap/xray.png", headers=H).status_code == 200
    # 首页展示：没有 AI 结构时不出结构卡，但出图成果照常挑选
    sc = c.get("/api/wb/showcase", headers=H).json()
    assert sc["structure"] is None
    assert sc["pick"]["full"]["number"] == 1 and sc["pick"]["flow"]["steps"] >= 1
    # 写入一份 AI 结构后，结构卡与展示图都会出现
    ws = reg.get("example-synthetic").workspace()
    raw = {"groups": [{"name": "回转组件", "role": "中部", "parts": ["SYN-B02", "SYN-C03"]}],
           "parts": {"SYN-B02": {"name": "回转座"}}}
    asm = json.loads(ws.assembly.read_text(encoding="utf-8"))
    plan = json.loads(ws.plan.read_text(encoding="utf-8"))
    (ws.root / "structure.json").write_text(json.dumps(
        L.clean_structure(raw, asm, plan, source="ai"), ensure_ascii=False), encoding="utf-8")
    sc = c.get("/api/wb/showcase", headers=H).json()
    assert sc["structure"]["groups"][0]["name"] == "回转组件"
    assert sc["structure"]["snaps"]["xray"].endswith("/snap/xray.png")
