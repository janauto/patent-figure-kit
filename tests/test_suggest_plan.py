"""F2-lite 候选分图（WP2）。

纯逻辑部分不需要 OCP：标准件规则、成员契约（真实零件名 / 组引用 → 解析，解析不了即拒绝）、
同名跨子装配提示、试渲染计划、接受写回、建议为空的诚实口径。

端到端部分（需要 cadquery-ocp）：用 make_demo_assemblies.py --edge-cases 造的四个边界样例
（同名跨子装配 / 方柱 / 全遮挡 / 低件数拥挤）真跑「分析 → 候选 → 逐个试渲染过闸」，并证明候选
渲染**从不改动**项目的当前版本指针。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import suggest_plan as SP  # noqa: E402

try:
    import OCP  # noqa: F401
    _HAS_OCP = True
except ImportError:  # pragma: no cover
    _HAS_OCP = False


# --------------------------------------------------------------------------- fixtures (pure)


def _part(name, inst=1, size=(10.0, 10.0, 10.0), path=None, degenerate=False):
    return {"name": name, "instances": inst, "bbox_size": list(size), "max_dim": max(size),
            "centers": [[0.0, 0.0, 0.0]] * inst, "degenerate": degenerate,
            "path_sample": path or ("ROOT/" + name)}


def _assembly():
    parts = [_part("BASE", size=(200, 90, 30)), _part("JAW", size=(40, 90, 50),
                                                         path="ROOT/FIX/JAW"),
             _part("PLATE", 2, (5, 80, 20), path="ROOT/FIX/PLATE"),
             _part("SCREW-M5", 4, (8, 8, 14), path="ROOT/FIX/SCREW-M5"),
             _part("MJAW", size=(60, 90, 60), path="ROOT/MOV/MJAW"),
             _part("LEAD-SCREW", size=(190, 18, 18), path="ROOT/SP/LEAD-SCREW"),
             _part("COLLAR", size=(10, 30, 30), path="ROOT/SP/COLLAR"),
             _part("SPINDLE-KNOB", size=(16, 16, 16), path="ROOT/SP/SPINDLE-KNOB"),
             _part("DUST", size=(0, 0, 0), degenerate=True)]
    return {"schema": "patent-assembly/1", "source": {"sha256": "abc", "step": "x.stp"},
            "parts": parts, "principal_axis": {"nearest": "x", "vector": [1, 0, 0]},
            "coaxial_groups": [{"id": "g1", "members": ["COLLAR", "LEAD-SCREW"], "instances": 2},
                               {"id": "g2", "members": ["SCREW-M5"], "instances": 2}],
            "split_suggestions": []}


def _instances():
    rows = []
    for name, path in (("BASE", "ROOT/BASE"), ("JAW", "ROOT/FIX/JAW"),
                       ("PLATE", "ROOT/FIX/PLATE"), ("PLATE", "ROOT/MOV/PLATE"),
                       ("SCREW-M5", "ROOT/FIX/SCREW-M5"), ("SCREW-M5", "ROOT/FIX/SCREW-M5"),
                       ("SCREW-M5", "ROOT/MOV/SCREW-M5"), ("SCREW-M5", "ROOT/MOV/SCREW-M5"),
                       ("MJAW", "ROOT/MOV/MJAW"), ("LEAD-SCREW", "ROOT/SP/LEAD-SCREW"),
                       ("COLLAR", "ROOT/SP/COLLAR"), ("SPINDLE-KNOB", "ROOT/SP/SPINDLE-KNOB")):
        rows.append({"name": name, "path": path, "degenerate": False})
    return {"schema": "patent-assembly-instances/1", "source": {"sha256": "abc"},
            "instances": rows}


def _plan(**terms):
    return {"schema": "patent-figure-plan/1",
            "source": {"step": "x.stp", "include": ["*"], "exclude": []},
            "terms": [{"selector": k, "term": v, "label": "once"} for k, v in terms.items()],
            "figures": [{"id": "fig1", "caption": "整体结构示意图", "kind": "assembly",
                         "members": ["*"]}],
            "layout": {"max_labels_per_figure": 20}}


# --------------------------------------------------------------------------- standard parts


def test_standard_part_rule_is_the_spec_regex_with_whole_word_latin_and_lead_screw_exception():
    rule = SP.StandardPartRule()
    assert rule.pattern == SP.STANDARD_PART_PATTERN          # 任务书正则原样
    for n in ("PGB-SCREW-M5", "VISE-BASE-BOLT", "hex_nut", "PLANET-PIN", "OUTPUT-BEARING",
              "WASHER_8", "screw2"):
        assert rule.matches(n), n
    for n in ("MAIN-SPINDLE", "PINION-GEAR", "PEANUT-HOLDER", "BEARINGLESS-HUB"):
        assert not rule.matches(n), n                          # whole-word: no substring hits
    assert rule.matches("A01", "紧固螺钉") and rule.matches("X", "定位销")
    assert not rule.matches("VISE-LEAD-SCREW") and not rule.matches("BALL_SCREW")
    assert not rule.matches("X", "丝杠螺母")                    # 传动件例外（契约 §14.2）
    assert SP.StandardPartRule(except_pattern="").matches("VISE-LEAD-SCREW")   # 可配置
    with pytest.raises(ValueError):
        SP.StandardPartRule(pattern="(")


def test_glob_escape_and_chinese_numbers():
    from fnmatch import fnmatchcase
    for name in ("A*B", "P[1]", "Q?", "PLAIN-NAME"):
        sel = SP.glob_escape(name)
        assert fnmatchcase(name, sel)
        assert not fnmatchcase(name + "X", sel)
    assert not fnmatchcase("AXB", SP.glob_escape("A*B"))
    assert [SP.cn_number(n) for n in (1, 10, 12, 20, 25)] == ["一", "十", "十二", "二十", "二十五"]


# --------------------------------------------------------------------------- candidates


def test_candidates_cover_whole_axis_subassemblies_and_coaxial_with_real_part_members():
    doc = SP.build_candidates(_assembly(), _plan(), _instances())
    kinds = [c["source"]["kind"] for c in doc["candidates"]]
    assert kinds[:2] == ["whole", "axis"]
    assert "subassembly" in kinds and "coaxial" in kinds
    names = {p["name"] for p in _assembly()["parts"] if not p["degenerate"]}
    for c in doc["candidates"]:
        assert c["status"] == "pending" and c["reason"]      # 没渲染就没有状态
        assert c["members"] and all(set(m) == {"part"} and m["part"] in names
                                    for m in c["members"])   # 真实零件名，不是显示名
        assert len(c["members_display"]) == len(c["members"])
        assert "DUST" not in [m["part"] for m in c["members"]]
    whole = doc["candidates"][0]
    assert whole["kind"] == "assembly" and len(whole["members"]) == len(names)
    # single-name coaxial group (g2) is not a candidate; equal member sets are deduplicated
    keys = [(c["kind"], tuple(m["part"] for m in c["members"])) for c in doc["candidates"]]
    assert len(keys) == len(set(keys))
    assert not any(c["source"]["id"] == "coax:g2" for c in doc["candidates"])
    assert "_trial_plan" not in json.dumps(SP.public(doc))


def test_same_name_across_subassemblies_is_flagged_as_selected_n_times():
    doc = SP.build_candidates(_assembly(), _plan(), _instances())
    fix = next(c for c in doc["candidates"] if c["source"]["id"] == "sub:ROOT/FIX")
    amb = {a["part"]: a for a in fix["ambiguous_members"]}
    assert amb["PLATE"]["selected"] == 2 and amb["PLATE"]["in_group"] == 1
    assert "按名称选中 2 处" in amb["PLATE"]["message"]
    assert amb["SCREW-M5"]["selected"] == 4
    assert doc["instances_exact"] is True
    # without the per-instance sidecar it is not detectable — and the result says so
    doc2 = SP.build_candidates(_assembly(), _plan(), None)
    assert doc2["instances_exact"] is False and doc2["warnings"]


def test_trial_plan_fills_placeholders_and_sets_standard_parts_none_without_touching_input():
    plan = _plan(BASE="钳座", **{"SCREW-*": "螺钉"})
    before = json.dumps(plan, sort_keys=True)
    doc = SP.build_candidates(_assembly(), plan, _instances())
    assert json.dumps(plan, sort_keys=True) == before       # the user's plan is never mutated
    assert "SCREW-M5" in doc["standard_parts"] and "LEAD-SCREW" not in doc["standard_parts"]
    trial = doc["candidates"][0]["_trial_plan"]
    assert len(trial["figures"]) == 1
    rows = {t["selector"]: t for t in trial["terms"]}
    assert rows["BASE"]["term"] == "钳座"
    assert rows["SCREW-*"]["label"] == "none"
    assert all(t["term"] for t in trial["terms"])            # placeholders only in the trial
    assert any(t["term"].startswith(SP.PLACEHOLDER_PREFIX) for t in trial["terms"])
    assert not any(ch.isdigit() for t in trial["terms"] for ch in t["term"])
    doc_all = SP.build_candidates(_assembly(), plan, _instances(), label_standard_parts=True)
    assert doc_all["standard_parts"] == []


def test_empty_reason_is_honest_and_carries_the_box_select_guide():
    doc = SP.build_candidates(_assembly(), _plan(), _instances())
    for c in doc["candidates"]:
        c["status"] = "fail"
    SP.finalize(doc)
    assert doc["empty_reason"] and doc["guide"]["title"] == "请框选分组"
    assert len(doc["guide"]["steps"]) == 3
    doc2 = SP.build_candidates(_assembly(), _plan(), _instances())
    SP.finalize(doc2)                                        # pending ≠ empty
    assert doc2["empty_reason"] is None
    none = SP.build_candidates(_assembly(), dict(_plan(), source={"include": ["NOPE*"]}))
    assert none["candidates"] == [] and none["empty_reason"] and none["guide"]


def test_verdict_turns_renderer_facts_into_plain_status():
    c = {"status": "pending"}
    SP.verdict(c, {"figures": [], "errors": [{"figure": "fig1", "code": "E_LAYOUT_UNSOLVABLE",
                                              "message": "figure fig1: 页面填充 0.308 < 0.55（几何…）"}]})
    assert c["status"] == "fail" and "铺不满纸面" in c["reason"] and "layout" not in c["reason"]
    assert c["detail"].startswith("figure fig1") and c["suggestion"]
    c = {"status": "pending"}
    SP.verdict(c, {"figures": [{"id": "fig1", "gate_pass": False, "hlr_dropped": [0, 0],
                                "qa": {"checks": [{"id": "leader_anchor_gap", "pass": False,
                                                   "title_zh": "引线落点", "hint_plain": "挡住了"}]}}]})
    assert c["status"] == "fail" and "引线落点" in c["reason"] and c["suggestion"] == "挡住了"
    c = {"status": "pending"}
    SP.verdict(c, {"figures": [{"id": "fig1", "gate_pass": True, "text_height_mm": 5.0,
                                "labels": 3, "qa": {"checks": [{}] * 13}}]})
    assert c["status"] == "pass" and "13 项" in c["reason"]


# --------------------------------------------------------------------------- accept


def test_resolve_members_accepts_parts_and_group_refs_and_rejects_the_unresolvable():
    asm, plan, inst = _assembly(), _plan(), _instances()
    names, bad = SP.resolve_members([{"part": "BASE"}, {"group": "sub:ROOT/SP"},
                                     {"group": "coax:g1"}], asm, plan, inst)
    assert not bad
    assert names == sorted({"BASE", "LEAD-SCREW", "COLLAR", "SPINDLE-KNOB"})
    names, bad = SP.resolve_members([{"part": "GHOST"}, {"group": "sub:ROOT/NOPE"},
                                     {"part": "DUST"}, "BASE", {"display": "钳座"}],
                                    asm, plan, inst)
    reasons = " ".join(b["reason"] for b in bad)
    assert len(bad) == 5 and "GHOST" in reasons and "sub:ROOT/NOPE" in reasons
    assert "退化" in reasons
    excl = dict(plan, source={"include": ["*"], "exclude": ["BASE"]})
    _n, bad = SP.resolve_members([{"part": "BASE"}], asm, excl, inst)
    assert bad and "排除" in bad[0]["reason"]


def test_accept_into_appends_exact_selectors_and_labels_standard_parts_none():
    asm = _assembly()
    plan = _plan(**{"SCREW-*": "螺钉", "BASE": "钳座"})
    new, info = SP.accept_into(plan, asm, [
        {"caption": "固定钳身组件分解示意图", "kind": "exploded",
         "names": ["JAW", "PLATE", "SCREW-M5"]}], standard_parts=["SCREW-M5"])
    assert [f["id"] for f in new["figures"]] == ["fig1", "fig2"]
    assert new["figures"][1]["members"] == ["JAW", "PLATE", "SCREW-M5"]
    assert info["added_figures"] == ["fig2"] and info["standard_labels_set"] == ["SCREW-M5"]
    rows = {t["selector"]: t for t in new["terms"]}
    assert rows["SCREW-*"]["label"] == "none"
    assert rows["JAW"]["term"] == "" and rows["PLATE"]["term"] == ""   # user names them
    assert plan["figures"] == [plan["figures"][0]]                     # input untouched
    rep, _ = SP.accept_into(plan, asm, [{"caption": "x", "kind": "exploded",
                                          "names": ["BASE"]}], mode="replace")
    assert [f["id"] for f in rep["figures"]] == ["fig1"]
    with pytest.raises(ValueError):
        SP.accept_into(plan, asm, [], mode="merge")


# --------------------------------------------------------------------------- end to end (OCP)


@pytest.fixture(scope="module")
def edge_dir(tmp_path_factory):
    if not _HAS_OCP:
        pytest.skip("需要 cadquery-ocp")
    d = tmp_path_factory.mktemp("edge")
    proc = subprocess.run([sys.executable, str(_ROOT / "scripts" / "make_demo_assemblies.py"),
                           "--edge-cases", "-o", str(d)], capture_output=True, text=True,
                          timeout=600)
    assert proc.returncode == 0, proc.stderr[-800:]
    return d


def _suggest(edge_dir, name):
    from patent_figure.worker import run_cli_via_worker
    from patent_figure.svg_preview import dxf_to_svg
    step = edge_dir / ("edge-%s.stp" % name)
    work = edge_dir / ("w-" + name)
    work.mkdir(exist_ok=True)
    asm_p, inst_p = work / "assembly.json", work / "assembly.instances.json"
    proc = run_cli_via_worker([str(_ROOT / "scripts" / "analyze_assembly.py"), str(step),
                               "-o", str(asm_p), "--instances", str(inst_p)])
    assert proc.returncode == 0, proc.stderr
    asm = json.loads(asm_p.read_text(encoding="utf-8"))
    inst = SP.load_instances(inst_p, asm)
    assert inst is not None
    plan = {"schema": "patent-figure-plan/1",
            "source": {"step": str(step), "include": ["*"], "exclude": []},
            "terms": [{"selector": p["name"], "term": "", "label": "once"}
                      for p in asm["parts"]],
            "figures": [{"id": "fig1", "caption": "x", "kind": "assembly", "members": ["*"]}],
            "layout": {"max_labels_per_figure": 20}}
    doc = SP.build_candidates(asm, plan, inst)
    SP.run_candidates(doc, ws_root=work, cache=work / "cache", job_id="t" + name.replace("-", ""),
                      assembly_path=asm_p, run_cli=run_cli_via_worker, svg_of=dxf_to_svg)
    return SP.public(doc), work


def test_edge_same_name_across_subassemblies(edge_dir):
    doc, _w = _suggest(edge_dir, "same-name")
    subs = [c for c in doc["candidates"] if c["source"]["kind"] == "subassembly"]
    assert len(subs) == 2
    for c in subs:
        assert c["status"] in ("pass", "fail") and c["reason"]
        amb = {a["part"]: a for a in c["ambiguous_members"]}
        assert amb["SN-BRACKET"]["selected"] == 2 and "按名称选中 2 处" in amb["SN-BRACKET"]["message"]
    assert doc["summary"]["pass"] >= 1


def test_edge_square_column_still_gets_rendered_verdicts(edge_dir):
    doc, _w = _suggest(edge_dir, "square-column")
    assert doc["candidates"]
    for c in doc["candidates"]:
        assert c["status"] in ("pass", "fail") and c["reason"]
        assert c["version"]                                  # it really rendered


def test_edge_fully_occluded_part_blocks_the_whole_assembly_view(edge_dir):
    doc, work = _suggest(edge_dir, "occluded")
    whole = doc["candidates"][0]
    assert whole["source"]["kind"] == "whole" and whole["status"] == "fail"
    assert whole["reason"] and whole["suggestion"]
    assert any(c["status"] == "pass" for c in doc["candidates"][1:])   # exploded shows it
    # candidate store: sealed versions, never a current pointer
    store = work / "candidates"
    assert (store / "results").is_dir() and not (store / "current.json").exists()
    for vd in (store / "results").iterdir():
        assert json.loads((vd / "manifest.json").read_text("utf-8"))["candidate"] is True


def test_edge_crowded_low_part_count_ends_empty_with_guide(edge_dir):
    doc, _w = _suggest(edge_dir, "crowded")
    assert doc["summary"]["pass"] == 0
    assert doc["empty_reason"] and doc["guide"]["action"] == "box_select"
    for c in doc["candidates"]:
        assert c["status"] == "fail" and c["suggestion"]
        assert "{" not in c["reason"] and "layout" not in c["reason"]


# --------------------------------------------------------------------------- AI 起名写回（R2b #1）


def test_fill_blank_terms_never_edits_a_shared_wildcard_row():
    names = ["SYN-A01", "SYN-B02", "SYN-C03", "ONLY-ME"]
    plan = {"terms": [{"selector": "SYN-*", "term": "", "label": "all"},
                      {"selector": "ONLY-*", "term": "", "label": "once"}]}
    out = SP.fill_blank_terms(plan, [{"selector": "SYN-A01", "term": "底座", "label": "once"},
                                     {"selector": "ONLY-ME", "term": "壳体", "label": "none"}],
                              names)
    assert out == {"filled": ["SYN-A01", "ONLY-ME"], "kept_human": [], "added": []}
    rows = plan["terms"]
    # an exact row for SYN-A01 is inserted AHEAD of the untouched shared wildcard
    assert rows[0] == {"selector": "SYN-A01", "term": "底座", "label": "once"}
    assert rows[1] == {"selector": "SYN-*", "term": "", "label": "all"}
    # a row that covers only this part is filled in place
    assert rows[2] == {"selector": "ONLY-*", "term": "壳体", "label": "none"}
    assert SP.winning_row(plan, "SYN-B02")["term"] == "" and \
        SP.winning_row(plan, "SYN-B02")["label"] == "all"
    assert SP.term_of(plan, "SYN-A01") == "底座"


def test_fill_blank_terms_keeps_human_names_and_adds_missing_rows():
    plan = {"terms": [{"selector": "A", "term": "人填的", "label": "once"}]}
    out = SP.fill_blank_terms(plan, [{"selector": "A", "term": "机器的"},
                                     {"selector": "B[1]", "term": "新件"}], ["A", "B[1]"])
    assert out == {"filled": [], "kept_human": ["A"], "added": ["B[1]"]}
    assert plan["terms"][0]["term"] == "人填的"
    assert plan["terms"][1] == {"selector": "B[[]1]", "term": "新件", "label": "once"}
