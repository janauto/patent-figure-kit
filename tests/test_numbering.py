"""Unit tests for scripts/patent_figure/numbering.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from patent_figure import numbering as nb  # noqa: E402


PARTS = ["SYN-A01", "SYN-B02", "SYN-C03", "SYN-D04", "SYN-H08"]


def test_numerals_follow_terms_order():
    num = nb.assign(
        [
            {"selector": "SYN-A01", "term": "底座"},
            {"selector": "SYN-B02", "term": "回转座"},
            {"selector": "SYN-H08", "term": "紧固螺钉", "label": "none"},
        ],
        PARTS,
    )
    assert num.table() == [
        (1, "底座", "SYN-A01"),
        (2, "回转座", "SYN-B02"),
        (3, "紧固螺钉", "SYN-H08"),
    ]
    assert num.numeral_of("SYN-B02") == 2
    assert num.term_of("SYN-A01") == "底座"
    # a label:"none" term still owns a numeral here; filtering it out of
    # reference-numerals.json is the renderer's job (impl-contract §4.2 vs §5.6)
    assert num.numeral_of("SYN-H08") == 3
    assert num.label_mode("SYN-H08") == "none"


def test_first_matching_selector_wins():
    num = nb.assign(
        [
            {"selector": "SYN-B*", "term": "回转组件"},
            {"selector": "SYN-B02", "term": "回转座"},
        ],
        PARTS,
    )
    assert num.numeral_of("SYN-B02") == 1
    assert num.term_of("SYN-B02") == "回转组件"
    # the shadowed term keeps its numeral but matches nothing
    assert num.unmatched_selectors() == ["SYN-B02"]


def test_unmatched_part_reports_none_mode_and_no_numeral():
    num = nb.assign([{"selector": "SYN-A01", "term": "底座"}], PARTS)
    assert num.numeral_of("SYN-C03") is None
    assert num.term_of("SYN-C03") is None
    assert num.label_mode("SYN-C03") == "none"
    assert num.unmatched_parts() == ["SYN-B02", "SYN-C03", "SYN-D04", "SYN-H08"]


def test_glob_is_case_sensitive():
    """fnmatchcase, never fnmatch (§7 rule 6) -- otherwise Windows would differ."""
    num = nb.assign([{"selector": "syn-a01", "term": "底座"}], PARTS)
    assert num.numeral_of("SYN-A01") is None


def test_description_zh():
    num = nb.assign(
        [
            {"selector": "SYN-A01", "term": "底座"},
            {"selector": "SYN-B02", "term": "回转轴组件"},
        ],
        PARTS,
    )
    assert num.description_zh() == "附图标记说明：1—底座；2—回转轴组件。"
    assert nb.assign([], PARTS).description_zh() == "附图标记说明：无。"


def test_once_picks_the_first_key_in_string_sort_order():
    """impl-contract §4.2: label "once" labels the instance whose key sorts first."""
    keys = ["SYN-H08#3", "SYN-H08#0", "SYN-H08#2", "SYN-H08#1"]
    assert nb.once_instance_key(keys) == "SYN-H08#0"
    assert nb.keys_to_label("once", keys) == ["SYN-H08#0"]
    assert nb.keys_to_label("none", keys) == []
    assert nb.keys_to_label("all", keys) == [
        "SYN-H08#0", "SYN-H08#1", "SYN-H08#2", "SYN-H08#3",
    ]
    # the rule is a PLAIN STRING sort, so #10 precedes #2 -- frozen here so the
    # renderer cannot quietly switch to a numeric sort
    assert nb.once_instance_key(["SYN-H08#2", "SYN-H08#10"]) == "SYN-H08#10"


def test_once_key_helpers_reject_empty_and_unknown_mode():
    with pytest.raises(ValueError):
        nb.once_instance_key([])
    with pytest.raises(ValueError):
        nb.keys_to_label("sometimes", ["SYN-A01#0"])


def test_instance_key_format():
    assert nb.instance_key("SYN-A01", 0) == "SYN-A01#0"


def test_assign_rejects_bad_rows():
    with pytest.raises(ValueError):
        nb.assign([{"selector": "", "term": "底座"}], PARTS)
    with pytest.raises(ValueError):
        nb.assign([{"selector": "SYN-A01"}], PARTS)
    with pytest.raises(ValueError):
        nb.assign([{"selector": "SYN-A01", "term": "底座", "label": "twice"}], PARTS)


def test_input_order_of_part_names_does_not_matter():
    terms = [{"selector": "SYN-*", "term": "零件"}]
    a = nb.assign(terms, PARTS)
    b = nb.assign(terms, list(reversed(PARTS)) + [PARTS[0]])
    assert a.parts_of(1) == b.parts_of(1)
    assert a.part_names == b.part_names


# --------------------------------------------------------------------------- cross-figure


def _plan_module():
    from patent_figure import plan as plan_mod
    return plan_mod


CROSS_ASSEMBLY = {
    "schema": "patent-assembly/1",
    "source": {"step": "tests/fixtures/synthetic.stp", "sha256": "0" * 64,
               "instances": 8, "distinct": 5},
    "units": "mm",
    "parts": [
        {"name": "SYN-A01", "instances": 1},
        {"name": "SYN-B02", "instances": 1},
        {"name": "SYN-C03", "instances": 1},
        {"name": "SYN-G07", "instances": 1},
        {"name": "SYN-H08", "instances": 4},
    ],
    "warnings": [],
}

CROSS_PLAN = {
    "schema": "patent-figure-plan/1",
    "source": {"step": "tests/fixtures/synthetic.stp"},
    "terms": [
        {"selector": "SYN-A01", "term": "底座"},
        {"selector": "SYN-B02", "term": "回转座"},
        {"selector": "SYN-C03", "term": "支撑轴"},
        {"selector": "SYN-G07", "term": "上盖"},
        {"selector": "SYN-H08*", "term": "紧固螺钉", "label": "none"},
    ],
    "figures": [
        {"id": "fig1", "caption": "整体结构示意图", "kind": "assembly", "members": ["*"]},
        {"id": "fig2", "caption": "回转组件分解示意图", "kind": "exploded",
         "members": ["SYN-B02", "SYN-C03"]},
        {"id": "fig3", "caption": "上盖分解示意图", "kind": "exploded",
         "members": ["SYN-A01", "SYN-G07"]},
    ],
}


def test_one_part_carries_the_same_numeral_in_every_figure():
    """impl-contract §4.2: the numeral is a property of ``terms``, never of a figure. A part
    appearing in three figures must be numbered identically in all three, or the specification's
    附图标记说明 contradicts the drawings."""
    plan_mod = _plan_module()
    selected = plan_mod.selected_part_names(CROSS_PLAN, CROSS_ASSEMBLY)
    num = plan_mod.numbering_for(CROSS_PLAN, CROSS_ASSEMBLY)

    seen = {}
    for figure in CROSS_PLAN["figures"]:
        for name in plan_mod.figure_members(figure, selected):
            numeral = num.numeral_of(name)
            seen.setdefault(name, set()).add(numeral)
    assert seen["SYN-B02"] == {2}
    assert seen["SYN-C03"] == {3}
    assert all(len(v) == 1 for v in seen.values())
    # every figure resolves through the SAME table object, so there is no per-figure numbering
    assert num.numeral_of("SYN-A01") == 1 and num.numeral_of("SYN-G07") == 4


def test_numerals_do_not_shift_when_a_figure_is_removed():
    """Numerals follow ``terms`` order, so deleting a figure cannot renumber anything."""
    plan_mod = _plan_module()
    full = plan_mod.numbering_for(CROSS_PLAN, CROSS_ASSEMBLY).table()
    trimmed = dict(CROSS_PLAN)
    trimmed["figures"] = CROSS_PLAN["figures"][:1]
    assert plan_mod.numbering_for(trimmed, CROSS_ASSEMBLY).table() == full


def test_label_mode_none_still_owns_a_numeral_in_this_layer():
    """§5.6 issues numerals to every term; §4.2's "only if some other figure labels it" filter
    is the renderer's job, because ``assign`` never sees the figures. Pinned so the two layers
    cannot be conflated by a later edit."""
    plan_mod = _plan_module()
    num = plan_mod.numbering_for(CROSS_PLAN, CROSS_ASSEMBLY)
    assert num.label_mode("SYN-H08") == "none"
    assert num.numeral_of("SYN-H08") == 5
    assert (5, "紧固螺钉", "SYN-H08*") in num.table()
