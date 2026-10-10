"""Unit tests for scripts/patent_figure/analyze.py.

No OCC. ``build_assembly`` takes a ``loader`` injection, so the whole §4.1 document is built
here from hand-made ``PartShape``-like objects.

This file owns the degenerate-part path, which the synthetic STEP deliberately cannot reach:
impl-contract §10 explains why a bare vertex is not in the fixture (it loses its name in the
STEP round trip and comes back labelled with the OCCT version string, which would make the
golden digest depend on the installed OCCT build). §10 names the remedy used here — construct
a ``PartShape`` with ``lo == hi`` directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from patent_figure import analyze as AN  # noqa: E402

CLI = _SCRIPTS / "analyze_assembly.py"
FIXTURE = _ROOT / "tests" / "fixtures" / "synthetic.stp"


# --------------------------------------------------------------------------- fixtures


class FakePart:
    """The subset of ``occ_backend.PartShape`` that ``analyze.instance_records`` reads."""

    def __init__(self, name, instance_index, lo, hi, path=None):
        self.name = name
        self.instance_index = int(instance_index)
        self.lo = np.array(lo, dtype=np.float64)
        self.hi = np.array(hi, dtype=np.float64)
        self.path = path or ("root/" + name)
        self.key = "%s#%d" % (name, instance_index)


def fake_step(tmp_path: Path) -> Path:
    """``build_assembly`` stats the STEP for its sha256, so the file must exist; the injected
    loader means its content is never parsed."""
    path = tmp_path / "fake.stp"
    path.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
    return path


def build(tmp_path: Path, parts, **kwargs) -> dict:
    return AN.build_assembly(fake_step(tmp_path), loader=lambda _p: parts, **kwargs)


def healthy_parts():
    return [FakePart("SYN-A01", 0, [0, 0, 0], [60, 60, 6]),
            FakePart("SYN-B02", 0, [4, 4, 6], [56, 56, 24]),
            FakePart("SYN-C03", 0, [21, 21, 24], [39, 39, 58])]


def entry(doc: dict, name: str) -> dict:
    matches = [e for e in doc["parts"] if e["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


# --------------------------------------------------------------------------- degenerate path


def test_lo_equals_hi_is_flagged_degenerate_and_named_in_the_warnings(tmp_path):
    """impl-contract §10: cover the degenerate path by constructing the PartShape directly."""
    parts = healthy_parts() + [FakePart("SYN-P00", 0, [10, 10, 30], [10, 10, 30])]
    doc = build(tmp_path, parts)
    degenerate = entry(doc, "SYN-P00")
    assert degenerate["degenerate"] is True
    assert degenerate["max_dim"] == 0.0
    assert degenerate["bbox_size"] == [0.0, 0.0, 0.0]
    assert all(not entry(doc, n)["degenerate"] for n in ("SYN-A01", "SYN-B02", "SYN-C03"))
    warning = [w for w in doc["warnings"] if "退化" in w]
    assert warning and "SYN-P00" in warning[0]
    assert "exclude" in warning[0]              # the warning names the plan edit that fixes it


def test_sub_tolerance_extent_is_degenerate_too(tmp_path):
    """The threshold is ``max_dim < 1e-6`` (§5.1), not ``lo == hi`` exactly."""
    parts = healthy_parts() + [FakePart("SYN-Q00", 0, [20, 20, 40], [20 + 1e-10, 20, 40])]
    doc = build(tmp_path, parts)
    assert entry(doc, "SYN-Q00")["degenerate"] is True
    assert AN.DEGENERATE_MAX_DIM == 1e-6


def test_degenerate_parts_stay_out_of_the_coaxial_groups(tmp_path):
    """They still appear in ``parts`` and in the PCA — dropping them silently would
    desynchronise the document from the STEP — but they cannot anchor an axis."""
    parts = healthy_parts() + [FakePart("SYN-P00", 0, [30, 30, 30], [30, 30, 30])]
    doc = build(tmp_path, parts)
    assert len(doc["parts"]) == 4
    assert doc["source"]["instances"] == 4
    for group in doc["coaxial_groups"]:
        assert "SYN-P00" not in group["members"]
    assert "SYN-P00" in doc["stack_order"]


def test_an_all_degenerate_assembly_still_produces_a_valid_document(tmp_path):
    """No crash, no NaN: the principal axis falls back to z and says so (§4.1 warnings)."""
    parts = [FakePart("SYN-P00", 0, [1, 1, 1], [1, 1, 1]),
             FakePart("SYN-P01", 0, [2, 2, 2], [2, 2, 2])]
    doc = build(tmp_path, parts)
    assert doc["principal_axis"]["vector"] == [0.0, 0.0, 1.0] or \
        doc["principal_axis"]["nearest"] in ("x", "y", "z")
    assert doc["bbox"]["size"] == [1.0, 1.0, 1.0]
    assert AN.validate_assembly(doc) == []
    assert all(e["degenerate"] for e in doc["parts"])


def test_single_instance_falls_back_to_the_z_axis(tmp_path):
    doc = build(tmp_path, [FakePart("SYN-A01", 0, [0, 0, 0], [60, 60, 6])])
    assert doc["principal_axis"] == {"vector": [0.0, 0.0, 1.0], "nearest": "z",
                                     "spread_ratio": 1.0}
    assert any("主轴" in w for w in doc["warnings"])


def test_non_finite_bbox_is_rejected_by_name(tmp_path):
    parts = healthy_parts() + [FakePart("SYN-N00", 0, [0, 0, 0], [np.nan, 1, 1])]
    with pytest.raises(AN.AnalyzeError) as exc:
        build(tmp_path, parts)
    assert "SYN-N00#0" in str(exc.value)


# --------------------------------------------------------------------------- document shape


def test_document_matches_the_schema_and_the_frozen_shape(tmp_path):
    doc = build(tmp_path, healthy_parts() + [FakePart("SYN-A01", 1, [70, 0, 0], [130, 60, 6])])
    assert AN.validate_assembly(doc) == []
    assert doc["schema"] == AN.SCHEMA_ID == "patent-assembly/1"
    assert doc["units"] == "mm"
    assert doc["source"]["instances"] == 4 and doc["source"]["distinct"] == 3
    assert len(doc["source"]["sha256"]) == 64
    base = entry(doc, "SYN-A01")
    assert base["instances"] == 2
    assert base["centers"] == sorted(base["centers"])       # §4.1: sorted lexicographically
    assert base["bbox_size"] == [60.0, 60.0, 6.0]           # of ONE instance, 3 decimals
    assert base["path_sample"].endswith("SYN-A01")
    assert [t["tier"] for t in doc["size_tiers"]] == ["large", "medium", "small"]


def test_no_negative_zero_survives_into_the_document(tmp_path):
    """-0.0 reads as a different number to a human and to ``repr``, though it compares equal."""
    parts = [FakePart("SYN-A01", 0, [-1e-9, 0, 0], [60, 60, 6]),
             FakePart("SYN-B02", 0, [-1e-9, 0, 6], [50, 50, 24])]
    doc = build(tmp_path, parts)
    blob = json.dumps(doc)
    assert "-0.0" not in blob


def test_instance_records_are_sorted_by_key_whatever_the_loader_returns(tmp_path):
    forward = healthy_parts()
    backward = list(reversed(healthy_parts()))
    assert json.dumps(build(tmp_path, forward), sort_keys=True) == \
        json.dumps(build(tmp_path, backward), sort_keys=True)


def test_include_and_exclude_use_case_sensitive_globs(tmp_path):
    parts = healthy_parts()
    kept = build(tmp_path, parts, include=["SYN-A*"])
    assert [e["name"] for e in kept["parts"]] == ["SYN-A01"]
    dropped = build(tmp_path, parts, exclude=["SYN-C*"])
    assert [e["name"] for e in dropped["parts"]] == ["SYN-A01", "SYN-B02"]
    assert any("过滤" in w for w in dropped["warnings"])
    with pytest.raises(AN.AnalyzeError):
        build(tmp_path, parts, include=["syn-a*"])          # fnmatchcase, never fnmatch


def test_empty_loader_result_is_an_error(tmp_path):
    with pytest.raises(AN.AnalyzeError):
        build(tmp_path, [])


def test_missing_step_file_is_an_error(tmp_path):
    with pytest.raises(AN.AnalyzeError):
        AN.build_assembly(tmp_path / "nowhere.stp", loader=lambda _p: healthy_parts())


def test_matches_any_is_case_sensitive():
    assert AN.matches_any("SYN-A01", ["SYN-*"]) is True
    assert AN.matches_any("SYN-A01", ["syn-*"]) is False
    assert AN.matches_any("SYN-A01", []) is False


# --------------------------------------------------------------------------- the real fixture


def test_synthetic_fixture_has_no_degenerate_part():
    """impl-contract §10 states this as a deliberate property of the fixture; if it ever stops
    being true the golden digest starts depending on the installed OCCT build."""
    pytest.importorskip("OCP")
    doc = AN.build_assembly(FIXTURE)
    assert doc["source"]["instances"] == 11 and doc["source"]["distinct"] == 8
    assert all(not e["degenerate"] for e in doc["parts"])
    assert all(e["name"].startswith("SYN-") for e in doc["parts"])
    assert AN.validate_assembly(doc) == []


def test_cli_writes_the_document_and_exits_zero(tmp_path):
    pytest.importorskip("OCP")
    out = tmp_path / "assembly.json"
    proc = subprocess.run([sys.executable, str(CLI), str(FIXTURE), "-o", str(out)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] == "patent-assembly/1"
    assert "Traceback" not in proc.stderr


def test_cli_exits_two_on_a_missing_step(tmp_path):
    proc = subprocess.run([sys.executable, str(CLI), str(tmp_path / "nope.stp"),
                           "-o", str(tmp_path / "a.json")], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr and proc.stderr.strip()
