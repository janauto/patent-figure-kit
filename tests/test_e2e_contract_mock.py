"""WP3b 接线：前端的开发 mock 与静态站 manifest 不得与真实后端的响应形状漂移。

真实后端上的浏览器端到端在 ``webui/dev/e2e_live.py``（Playwright，需要起工作台）；这里只做不需要浏览器、
不需要起服务的静态核对，随 pytest 每次跑：
  * ``webui/dev/manifest.json``（静态演示站的数据 = 后端响应快照）按 docs/openapi.yaml 核对必需字段与契约外的键；
  * mock 的 ``GET /v1/projects`` 条目与 ``workbench.Project.summary()`` 字段一致；
  * mock 的子应用 ``GET /p/{pid}/api/state`` 与 ``plan_studio`` 的 ``state()`` 返回字段一致（从源码 AST 取键）。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEV = REPO / "webui" / "dev"


def _mock():
    sys.path.insert(0, str(DEV))
    import mock_server as M
    if not M.PROJECTS:
        M.seed()
    return M


def test_static_manifest_matches_openapi():
    pytest.importorskip("yaml")
    sys.path.insert(0, str(DEV))
    import build_static
    man = json.loads((DEV / "manifest.json").read_text(encoding="utf-8"))
    assert build_static.check_drift(man) == []


def test_static_manifest_is_a_backend_snapshot_not_ui_shape():
    man = json.loads((DEV / "manifest.json").read_text(encoding="utf-8"))
    assert man["schema"] == "inkline-static/2" and "capabilities" in man
    for p in man["projects"]:
        assert set(p) >= {"id", "summary", "project", "plan", "plan_etag", "state", "candidates", "model_url"}
        assert "detail" not in p                                   # 旧的「前端内部形状」不再出现
        assert not str(p["state"].get("workdir", "")).startswith("/Users/")   # 不带本机路径


def test_mock_project_summary_matches_workbench(tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    import workbench as W
    root = tmp_path / "p-20261013-abcdef"
    (root / "studio").mkdir(parents=True)
    (root / "project.json").write_text(json.dumps({"id": root.name, "title": "t", "step": "/x/model.stp",
                                                   "status": "ready"}), encoding="utf-8")
    real = set(W.Project(root).summary())
    M = _mock()
    mock = set(M.project_summary(M.PROJECTS["gearbox"]))
    assert mock == real, {"mock 多出": sorted(mock - real), "mock 缺少": sorted(real - mock)}


def _plan_studio_state_keys():
    tree = ast.parse((REPO / "scripts" / "plan_studio.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "state":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    return {k.value for k in sub.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("plan_studio.state() 没找到")


def test_mock_subapp_state_matches_plan_studio():
    M = _mock()
    assert set(M.sub_state("gearbox")) == _plan_studio_state_keys()


def test_mock_capabilities_match_workbench_shape():
    sys.path.insert(0, str(REPO / "scripts"))
    from patent_figure import capabilities as CAPS
    M = _mock()
    caps = M.capabilities()
    assert set(caps["export"]) == set(CAPS.export_capabilities())
    assert set(caps["ai"]) == set(CAPS.ai_capabilities({}))
