"""WP2 新 CLI 的写入目标守卫（修复轮 1 / Codex R2 #2）——跨入口路径碰撞。

被撞的对象是**别的入口**留下的版本库：渲染 CLI 的 ``OUT.versions/``（含 ``current.json``、已封口
版本、``OUT`` 视图）与工作台工程的 ``studio/``。新 CLI（候选分图、导出包、逐页核验、analyze 的
``--instances``）在写任何东西之前都必须拒绝：输入文件本身、版本库保留区、它们的符号链接 / 大小写
别名、互相包含的输出、不是本工具建的工作目录——退出码 2，且被撞的文件一个字节都不变。OCP 无关
（守卫在渲染之前）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_S = _ROOT / "scripts"
sys.path.insert(0, str(_S))

from patent_figure import fsid  # noqa: E402
from patent_figure import publish as P  # noqa: E402
from patent_figure import versions as V  # noqa: E402


def _run(*argv):
    return subprocess.run([sys.executable] + [str(a) for a in argv], capture_output=True,
                          text=True, timeout=300, cwd=str(_ROOT))


@pytest.fixture()
def world(tmp_path):
    """Inputs + a committed render-CLI store (OUT.versions + OUT view) + a workbench-like
    studio store, the way other entries leave them on disk."""
    step = tmp_path / "asm.stp"
    step.write_bytes(b"ISO-10303-21; dummy")
    asm = tmp_path / "assembly.json"
    asm.write_text(json.dumps({"schema": "patent-assembly/1", "source": {"sha256": "x"},
                               "parts": [{"name": "A", "instances": 1, "bbox_size": [1, 1, 1],
                                          "max_dim": 1, "centers": [[0, 0, 0]],
                                          "degenerate": False, "path_sample": "R/A"}],
                               "coaxial_groups": [], "split_suggestions": []}),
                   encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"schema": "patent-figure-plan/1",
                                "source": {"step": str(step), "include": ["*"]},
                                "terms": [{"selector": "A", "term": "底座"}],
                                "figures": [{"id": "fig1", "caption": "x", "kind": "assembly",
                                             "members": ["*"]}]}), encoding="utf-8")
    out = tmp_path / "OUT"
    vs = P.cli_store(out)
    vd = vs.begin("c1")
    for n in ("fig1.dxf", "fig1.qa.json", "reference-numerals.json"):
        (vd / n).write_text(n, encoding="utf-8")
    assert P.commit_version(vs, "c1", vs.next_revision(), {"figures": []},
                            {"required_artefacts": ["fig1.dxf", "fig1.qa.json",
                                                    "reference-numerals.json"]}, ok=True)
    studio = tmp_path / "proj" / "studio"
    ss = V.VersionStore(studio)
    sd = ss.begin("r1")
    (sd / "fig1.dxf").write_text("studio", encoding="utf-8")
    ss.write_result("r1", {"ok": True, "figures": []})
    ss.seal("r1", {"ok": True, "required_artefacts": ["fig1.dxf"]}, 1)
    with V.state_lock(ss.root):
        assert ss.commit_pointer("r1", 1)
    ss.refresh_view()
    (studio / "plan.json").write_text('{"keep": true}', encoding="utf-8")
    return {"tmp": tmp_path, "step": step, "asm": asm, "plan": plan, "out": out,
            "store": vs.root, "studio": studio}


def _snapshot(w):
    files = {}
    for root in (w["store"], w["studio"], w["tmp"]):
        for p in Path(root).rglob("*"):
            if p.is_file() and not p.is_symlink():
                files[str(p)] = p.read_bytes()
    return files


def _case_alias(path: Path) -> Path:
    return path.parent / path.name.swapcase()


def _bad_targets(w):
    """(label, path) — every one must be refused as a write target."""
    store, studio = w["store"], w["studio"]
    link = w["tmp"] / "alias"
    if not link.is_symlink():
        os.symlink(str(store), str(link))
    bad = [("plan(input)", w["plan"]), ("assembly(input)", w["asm"]),
           ("current.json", store / "current.json"),
           ("sealed version", store / "results" / "c1" / "fig1.dxf"),
           ("out view", w["out"] / "fig1.dxf"),
           ("symlink alias", link / "current.json"),
           ("studio current", studio / "current.json"),
           ("studio out view", studio / "out" / "fig1.dxf")]
    if fsid.case_insensitive(str(store)):
        bad.append(("case alias", _case_alias(store) / "CURRENT.JSON"))
    return bad


def test_suggest_cli_refuses_every_protected_or_input_report_path(world):
    w = world
    before = _snapshot(w)
    for label, target in _bad_targets(w):
        r = _run(_S / "suggest_figure_plan.py", "--assembly", w["asm"], "--plan", w["plan"],
                 "--no-render", "--json", target)
        assert r.returncode == 2, (label, r.stdout, r.stderr)
        assert "用法错误" in r.stderr, label
    assert _snapshot(w) == before
    ok = _run(_S / "suggest_figure_plan.py", "--assembly", w["asm"], "--plan", w["plan"],
              "--no-render", "--json", w["tmp"] / "fresh" / "c.json")
    assert ok.returncode == 0, ok.stderr
    assert json.loads((w["tmp"] / "fresh" / "c.json").read_text("utf-8"))["candidates"]


def test_suggest_cli_refuses_a_workdir_that_is_not_its_own(world):
    w = world
    before = _snapshot(w)
    for wd in (w["studio"], w["store"], w["tmp"]):
        r = _run(_S / "suggest_figure_plan.py", "--assembly", w["asm"], "--plan", w["plan"],
                 "--no-render", "-o", wd)
        assert r.returncode == 2, (wd, r.stderr)
    assert _snapshot(w) == before
    wd = w["tmp"] / "sug"
    assert _run(_S / "suggest_figure_plan.py", "--assembly", w["asm"], "--plan", w["plan"],
                "--no-render", "-o", wd).returncode == 0
    assert (wd / ".suggest-workdir").is_file()          # ours now — a re-run is accepted
    assert _run(_S / "suggest_figure_plan.py", "--assembly", w["asm"], "--plan", w["plan"],
                "--no-render", "-o", wd).returncode == 0


def test_export_cli_refuses_protected_json_zip_workdir_and_overlaps(world):
    w = world
    before = _snapshot(w)
    base = [_S / "export_figure_package.py", w["plan"], "--assembly", w["asm"]]
    pkg = w["tmp"] / "pkg"
    cases = [("--json", t) for _l, t in _bad_targets(w)] + \
        [("--zip", t) for _l, t in _bad_targets(w)]
    for flag, target in cases:
        r = _run(*(base + ["-o", pkg, flag, target]))
        assert r.returncode == 2, (flag, target, r.stderr)
    overlaps = [["-o", pkg, "--zip", pkg / "a.zip"], ["-o", pkg, "--json", pkg / "r.json"],
                ["-o", w["tmp"] / "wk" / "pkg", "--workdir", w["tmp"] / "wk"],
                ["-o", pkg, "--workdir", w["studio"]], ["-o", pkg, "--workdir", w["store"]],
                ["-o", pkg, "--workdir", w["tmp"] / "alias2"],
                ["-o", pkg, "--cache", w["store"] / "results" / "c1"],
                ["-o", w["store"] / "results" / "c1"]]
    os.symlink(str(w["studio"]), str(w["tmp"] / "alias2"))      # a link to someone's project
    for extra in overlaps:
        r = _run(*(base + extra))
        assert r.returncode == 2, ([str(x)[-30:] for x in extra], r.stderr[-200:])
    assert not pkg.exists()                            # nothing was even started
    after = _snapshot(w)
    assert {k: v for k, v in after.items() if "/pkg" not in k} == \
        {k: v for k, v in before.items() if "/pkg" not in k}


def test_analyze_and_check_clis_refuse_inputs_and_protected_outputs(world):
    w = world
    before = _snapshot(w)
    for argv in ([_S / "analyze_assembly.py", w["step"], "-o", w["step"]],
                 [_S / "analyze_assembly.py", w["step"], "-o", w["store"] / "current.json"],
                 [_S / "analyze_assembly.py", w["step"], "-o", w["tmp"] / "a.json",
                  "--instances", w["tmp"] / "a.json"],
                 [_S / "analyze_assembly.py", w["step"], "-o", w["tmp"] / "a.json",
                  "--instances", w["studio"] / "current.json"],
                 [_S / "deliverable_check.py", w["plan"], "--sources", w["asm"],
                  "--json", w["plan"]],
                 [_S / "deliverable_check.py", w["plan"], "--sources", w["asm"],
                  "--json", w["store"] / "current.json"]):
        r = _run(*argv)
        assert r.returncode == 2, (argv, r.stdout, r.stderr)
    assert _snapshot(w) == before
