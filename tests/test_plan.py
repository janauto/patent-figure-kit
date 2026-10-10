"""Unit tests for scripts/patent_figure/plan.py and scripts/validate_figure_plan.py."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from patent_figure import plan as plan_mod  # noqa: E402

CLI = _SCRIPTS / "validate_figure_plan.py"


ASSEMBLY = {
    "schema": "patent-assembly/1",
    "source": {"step": "tests/fixtures/synthetic.stp", "sha256": "0" * 64,
               "instances": 11, "distinct": 8},
    "units": "mm",
    "parts": [
        {"name": "SYN-A01", "instances": 1},
        {"name": "SYN-B02", "instances": 1},
        {"name": "SYN-C03", "instances": 1},
        {"name": "SYN-D04", "instances": 1},
        {"name": "SYN-E05", "instances": 1},
        {"name": "SYN-F06", "instances": 1},
        {"name": "SYN-G07", "instances": 1},
        {"name": "SYN-H08", "instances": 4},
    ],
    "split_suggestions": [
        {"id": "coaxial", "strategy": "coaxial",
         "figures": [{"caption_hint": "组件 A", "members": ["SYN-A01"], "labels": 4},
                     {"caption_hint": "组件 B", "members": ["SYN-B02"], "labels": 4}]}
    ],
    "warnings": [],
}


def good_plan() -> dict:
    return {
        "schema": "patent-figure-plan/1",
        "source": {"step": "tests/fixtures/synthetic.stp"},
        "terms": [
            {"selector": "SYN-A01", "term": "底座"},
            {"selector": "SYN-B02", "term": "回转座"},
            {"selector": "SYN-C03", "term": "立柱"},
            {"selector": "SYN-D04", "term": "密封圈"},
            {"selector": "SYN-E05", "term": "垫片"},
            {"selector": "SYN-F06", "term": "球头"},
            {"selector": "SYN-G07", "term": "盖板"},
            {"selector": "SYN-H08", "term": "紧固螺钉", "label": "none"},
        ],
        "figures": [
            {"id": "fig1", "caption": "整体结构示意图", "kind": "assembly", "members": ["*"]},
            {"id": "fig2", "caption": "回转组件分解示意图", "kind": "exploded",
             "members": ["SYN-A01", "SYN-B02"], "layout": {"explode_axis": "z"}},
        ],
    }


def codes(issues):
    return sorted(issue.code for issue in issues)


# --------------------------------------------------------------------------- happy path


def test_good_plan_has_no_issues():
    assert plan_mod.validate(good_plan(), ASSEMBLY) == []


def test_every_issue_carries_a_hint():
    plan = good_plan()
    plan["terms"][0]["selector"] = "NOPE*"
    plan["terms"][1]["term"] = "PRT0001-A"
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert issues
    for issue in issues:
        assert issue.hint.strip(), issue.code
        assert issue.pointer.startswith("/")
        assert issue.severity in (plan_mod.SEVERITY_ERROR, plan_mod.SEVERITY_WARNING)


# --------------------------------------------------------------------------- defaults


def test_apply_defaults_fills_layout_and_does_not_mutate():
    plan = good_plan()
    before = copy.deepcopy(plan)
    out = plan_mod.apply_defaults(plan)
    assert plan == before
    assert out["layout"] == plan_mod.LAYOUT_DEFAULTS
    assert out["layout"]["axis_angle"] == "auto"
    assert out["figures"][1]["layout"]["explode_axis"] == "z"
    assert out["figures"][1]["layout"]["view"] == "iso"
    assert out["figures"][0]["layout"]["explode_axis"] == "auto"
    assert out["terms"][0]["label"] == "once"
    assert out["source"]["include"] == [] and out["source"]["exclude"] == []


def test_axis_angle_default_is_the_string_auto():
    assert plan_mod.LAYOUT_DEFAULTS["axis_angle"] == "auto"


def test_apply_defaults_clamps_out_of_range_angle():
    plan = good_plan()
    plan["layout"] = {"axis_angle": 200.0, "max_labels_per_figure": 40}
    out = plan_mod.apply_defaults(plan)
    assert out["layout"]["axis_angle"] == plan_mod.ANGLE_HI
    assert out["layout"]["max_labels_per_figure"] == plan_mod.LABELS_PER_FIGURE_HARD_MAX
    issues = plan_mod.validate(plan, ASSEMBLY)
    # the schema rejects 200 outright; the cap warning is a W_CLAMPED
    assert "E_SCHEMA" in codes(issues)


def test_w_clamped_on_a_programmatic_plan():
    plan = good_plan()
    plan["layout"] = {"max_labels_per_figure": 40}
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert codes(issues) == ["W_CLAMPED"]
    assert not plan_mod.has_errors(issues)


# --------------------------------------------------------------------------- error codes


def test_selector_no_match():
    plan = good_plan()
    plan["terms"][0]["selector"] = "SYN-Z99"
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert "E_SELECTOR_NO_MATCH" in codes(issues)
    issue = [i for i in issues if i.code == "E_SELECTOR_NO_MATCH"][0]
    assert issue.pointer == "/terms/0/selector"
    assert "SYN-A01" in issue.hint


def test_term_looks_like_part_code():
    for bad in ("PRT0001-A", "1234-A01", "底座_12_3"):
        plan = good_plan()
        plan["terms"][0]["term"] = bad
        issues = plan_mod.validate(plan, ASSEMBLY)
        assert "E_TERM_LOOKS_LIKE_PART_CODE" in codes(issues), bad


def test_ordinary_chinese_term_is_not_a_part_code():
    plan = good_plan()
    plan["terms"][0]["term"] = "底座组件"
    assert plan_mod.validate(plan, ASSEMBLY) == []


def test_too_many_labels():
    plan = good_plan()
    plan["figures"][1]["layout"] = {"max_labels_per_figure": 1}
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert "E_TOO_MANY_LABELS" in codes(issues)
    issue = [i for i in issues if i.code == "E_TOO_MANY_LABELS"][0]
    assert "fig2" in issue.hint and "split_suggestions" in issue.hint


def test_label_all_counts_instances():
    plan = good_plan()
    plan["terms"][7]["label"] = "all"
    counts = dict((fid, n) for fid, n, _cap in
                  plan_mod.effective_label_counts(plan, ASSEMBLY))
    assert counts["fig1"] == 7 + 4      # 7 once-labelled parts + 4 screw instances
    plan["terms"][7]["label"] = "none"
    counts = dict((fid, n) for fid, n, _cap in
                  plan_mod.effective_label_counts(plan, ASSEMBLY))
    assert counts["fig1"] == 7


def test_member_no_match():
    plan = good_plan()
    plan["figures"][1]["members"] = ["SYN-A01", "SYN-Z*"]
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert "E_MEMBER_NO_MATCH" in codes(issues)
    assert [i for i in issues if i.code == "E_MEMBER_NO_MATCH"][0].pointer == "/figures/1/members/1"


def test_member_matching_only_excluded_parts():
    plan = good_plan()
    plan["source"]["exclude"] = ["SYN-H08"]
    plan["terms"] = plan["terms"][:7]
    plan["figures"][1]["members"] = ["SYN-H08"]
    issues = plan_mod.validate(plan, ASSEMBLY)
    issue = [i for i in issues if i.code == "E_MEMBER_NO_MATCH"][0]
    assert "source/exclude" in issue.hint


def test_duplicate_figure_id():
    plan = good_plan()
    plan["figures"][1]["id"] = "fig1"
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert "E_DUPLICATE_FIGURE_ID" in codes(issues)


def test_unknown_enum():
    plan = good_plan()
    plan["figures"][0]["kind"] = "section"
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert codes(issues) == ["E_UNKNOWN_ENUM"]
    assert "assembly" in issues[0].hint


def test_unlabelled_part_warning():
    plan = good_plan()
    plan["terms"] = plan["terms"][:2]
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert "W_UNLABELLED_PART" in codes(issues)
    assert not plan_mod.has_errors(issues)


def test_schema_rejects_unknown_key():
    plan = good_plan()
    plan["layout"] = {"axis_angle": "auto", "explode_gap": 0.05}
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert codes(issues) == ["E_SCHEMA"]
    assert "additionalProperties" in issues[0].hint


def test_schema_rejects_numeral_in_plan():
    plan = good_plan()
    plan["terms"][0]["numeral"] = 1
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert codes(issues) == ["E_SCHEMA"]


def test_schema_axis_angle_hint_mentions_auto():
    plan = good_plan()
    plan["layout"] = {"axis_angle": 95}
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert codes(issues) == ["E_SCHEMA"]
    assert '"auto"' in issues[0].hint


# --------------------------------------------------------------------------- determinism


def test_issue_order_is_stable_under_key_permutation():
    plan = good_plan()
    plan["terms"][0]["selector"] = "SYN-Z99"
    plan["terms"][3]["term"] = "PRT0001-A"
    first = [i.to_dict() for i in plan_mod.validate(plan, ASSEMBLY)]
    shuffled = {k: plan[k] for k in reversed(list(plan.keys()))}
    second = [i.to_dict() for i in plan_mod.validate(shuffled, ASSEMBLY)]
    assert first == second
    assembly2 = copy.deepcopy(ASSEMBLY)
    assembly2["parts"] = list(reversed(assembly2["parts"]))
    third = [i.to_dict() for i in plan_mod.validate(plan, assembly2)]
    assert first == third


# --------------------------------------------------------------------------- CLI


def _run(tmp_path, plan, extra=()):
    plan_file = tmp_path / "plan.json"
    asm_file = tmp_path / "assembly.json"
    plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    asm_file.write_text(json.dumps(ASSEMBLY, ensure_ascii=False), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(CLI), str(plan_file), "--assembly", str(asm_file)] + list(extra),
        capture_output=True, text=True,
    )


def test_cli_exit_0_on_good_plan(tmp_path):
    proc = _run(tmp_path, good_plan())
    assert proc.returncode == 0, proc.stderr
    assert "校验通过" in proc.stdout


def test_cli_exit_1_and_json_on_bad_plan(tmp_path):
    plan = good_plan()
    plan["terms"][0]["selector"] = "SYN-Z99"
    out = tmp_path / "issues.json"
    proc = _run(tmp_path, plan, ["--json", str(out)])
    assert proc.returncode == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["pass"] is False
    assert payload["summary"]["errors"] >= 1
    assert all(row["hint"] for row in payload["issues"])


def test_cli_exit_2_on_missing_plan(tmp_path):
    asm_file = tmp_path / "assembly.json"
    asm_file.write_text(json.dumps(ASSEMBLY), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(CLI), str(tmp_path / "nope.json"), "--assembly", str(asm_file)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "用法错误" in proc.stderr


def test_cli_exit_2_on_broken_assembly(tmp_path):
    plan_file = tmp_path / "plan.json"
    asm_file = tmp_path / "assembly.json"
    plan_file.write_text(json.dumps(good_plan(), ensure_ascii=False), encoding="utf-8")
    asm_file.write_text("{not json", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(CLI), str(plan_file), "--assembly", str(asm_file)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2


def test_cli_reports_json_syntax_error_as_e_schema(tmp_path):
    plan_file = tmp_path / "plan.json"
    asm_file = tmp_path / "assembly.json"
    plan_file.write_text('{"schema": "patent-figure-plan/1",}', encoding="utf-8")
    asm_file.write_text(json.dumps(ASSEMBLY), encoding="utf-8")
    out = tmp_path / "issues.json"
    proc = subprocess.run(
        [sys.executable, str(CLI), str(plan_file), "--assembly", str(asm_file),
         "--json", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["issues"][0]["code"] == "E_SCHEMA"


# --------------------------------------------------------------------------- axis_angle


def test_axis_angle_accepts_auto_and_any_number_in_the_clamp_range():
    """§4.2: `"auto"` (the default) or a number in [120, 180]. `"auto"` means the renderer
    solves the sheet angle per figure — a fixed default would be a layout constant travelling
    through the model, which is what prime directive #1 forbids."""
    for value in ("auto", plan_mod.ANGLE_LO, 124, 150.5, plan_mod.ANGLE_HI):
        plan = good_plan()
        plan["layout"] = {"axis_angle": value}
        assert plan_mod.validate(plan, ASSEMBLY) == [], value
        assert plan_mod.apply_defaults(plan)["layout"]["axis_angle"] == value
        # a per-figure override takes the same values
        fig_plan = good_plan()
        fig_plan["figures"][1]["layout"] = {"axis_angle": value}
        assert plan_mod.validate(fig_plan, ASSEMBLY) == [], value
        assert plan_mod.figure_layout(plan_mod.apply_defaults(fig_plan),
                                      fig_plan["figures"][1])["axis_angle"] == value


@pytest.mark.parametrize("bad", [119, 181, 0, -124, 359])
def test_axis_angle_outside_the_range_is_rejected_by_the_schema(bad):
    """The schema rejects it before `W_CLAMPED` can fire, so the CLI path reports E_SCHEMA.
    The clamp itself is still exercised through `apply_defaults` below."""
    plan = good_plan()
    plan["layout"] = {"axis_angle": bad}
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert codes(issues) == ["E_SCHEMA"]
    assert '"auto"' in issues[0].hint and "120" in issues[0].hint


@pytest.mark.parametrize("raw,clamped", [(200.0, 180.0), (90.0, 120.0), (1000, 180.0)])
def test_apply_defaults_clamps_an_out_of_range_angle(raw, clamped):
    plan = good_plan()
    plan["layout"] = {"axis_angle": raw}
    assert plan_mod.apply_defaults(plan)["layout"]["axis_angle"] == clamped


def test_w_clamped_is_a_warning_not_an_error():
    """The one `W_CLAMPED` path reachable through the CLI: a plan trying to raise the QA gate
    above `labels_per_figure_max`. It clamps and warns; it never fails the plan."""
    plan = good_plan()
    plan["layout"] = {"max_labels_per_figure": 40}
    issues = plan_mod.validate(plan, ASSEMBLY)
    assert codes(issues) == ["W_CLAMPED"]
    assert issues[0].severity == plan_mod.SEVERITY_WARNING
    assert not plan_mod.has_errors(issues)
    assert plan_mod.apply_defaults(plan)["layout"]["max_labels_per_figure"] == \
        plan_mod.LABELS_PER_FIGURE_HARD_MAX


def test_schema_rejects_unknown_keys_everywhere():
    """§4.2: `additionalProperties: false` everywhere — an unknown key is a typo, and silently
    ignoring it would let a model believe it had configured something."""
    for path, patch in (
        (("terms", 0), {"colour": "red"}),
        (("figures", 0), {"scale": 2}),
        (("source",), {"units": "mm"}),
    ):
        plan = good_plan()
        target = plan[path[0]] if len(path) == 1 else plan[path[0]][path[1]]
        target.update(patch)
        assert codes(plan_mod.validate(plan, ASSEMBLY)) == ["E_SCHEMA"], path
    plan = good_plan()
    plan["reference_numerals"] = [{"numeral": 1, "term": "底座"}]
    assert codes(plan_mod.validate(plan, ASSEMBLY)) == ["E_SCHEMA"]
