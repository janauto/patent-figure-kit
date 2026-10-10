"""End-to-end golden test: synthetic.stp -> figure-plan.json -> DXF.

impl-contract §7 rule 8: *"``normalized_digest`` of two runs of the same plan must be equal —
``tests/test_golden.py`` enforces this by rendering twice in one process and once in a
subprocess."*  That is what this file does, and it also pins the digests themselves in
``tests/fixtures/golden_digest.txt`` so an unintended change to any module in the chain
(HLR collection, layout, labels, sheet emission) surfaces as one failing assertion rather
than as a drawing nobody looks at closely.

Requires ``cadquery-ocp``; every other test file in this suite runs without it.

To regenerate the baseline after an intentional change::

    python3 tests/test_golden.py --update

and read the diff before committing it — a changed digest means the drawing changed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from patent_figure import sheet as SH  # noqa: E402  (pure ezdxf, no OCC)

try:  # OCP is the only heavy dependency in this file; everything else is skipped without it
    import OCP  # noqa: F401
    _HAS_OCP = True
except Exception:  # pragma: no cover - environment dependent
    _HAS_OCP = False

pytestmark = pytest.mark.skipif(
    not _HAS_OCP, reason="需要 cadquery-ocp（OCP.*）才能从 STEP 出图；纯逻辑测试不受影响")

FIXTURE = _ROOT / "tests" / "fixtures" / "synthetic.stp"
GOLDEN = _ROOT / "tests" / "fixtures" / "golden_digest.txt"
ANALYZE_CLI = _SCRIPTS / "analyze_assembly.py"
RENDER_CLI = _SCRIPTS / "render_patent_figure.py"

FIGURE_IDS = ("fig1", "fig2")


def plan_document() -> dict:
    """The plan the baseline is pinned against.

    Every selector is a ``SYN-*`` synthetic name (impl-contract §10 forbids real part names in
    the repository), and the three ``label`` modes plus both ``kind`` values are exercised.
    """
    return {
        "schema": "patent-figure-plan/1",
        "source": {"step": str(FIXTURE), "include": ["SYN-*"], "exclude": []},
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


# --------------------------------------------------------------------------- pipeline


def prepare(work: Path) -> tuple:
    """Write plan.json and build assembly.json in ``work``; return both paths."""
    work.mkdir(parents=True, exist_ok=True)
    plan_path = work / "plan.json"
    plan_path.write_text(json.dumps(plan_document(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
    assembly_path = work / "assembly.json"
    from patent_figure import analyze as AN
    doc = AN.build_assembly(FIXTURE)
    assert AN.validate_assembly(doc) == []
    AN.write_assembly(doc, assembly_path)
    return plan_path, assembly_path


def render_in_process(plan_path: Path, assembly_path: Path, out: Path, extra=()) -> int:
    import render_patent_figure as R
    return R.main([str(plan_path), "--assembly", str(assembly_path), "-o", str(out)]
                  + list(extra))


def render_in_subprocess(plan_path: Path, assembly_path: Path, out: Path, extra=()):
    return subprocess.run(
        [sys.executable, str(RENDER_CLI), str(plan_path), "--assembly", str(assembly_path),
         "-o", str(out)] + list(extra),
        capture_output=True, text=True, cwd=str(_ROOT))


def digests(out: Path) -> dict:
    return {fid + ".dxf": SH.normalized_digest(out / (fid + ".dxf")) for fid in FIGURE_IDS}


# --------------------------------------------------------------------------- golden file


def read_golden() -> dict:
    out = {}
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, digest = line.split()
        out[name] = digest
    return out


def write_golden(values: dict, note: str) -> None:
    lines = [
        "# tests/fixtures/golden_digest.txt",
        "# sheet.normalized_digest (impl-contract §5.4) of the figures rendered from",
        "# tests/fixtures/synthetic.stp by the plan in tests/test_golden.py.",
        "# Regenerate with:  python3 tests/test_golden.py --update",
        "# A changed digest means the DRAWING changed — read the diff, do not just paste it.",
        "# " + note,
        "",
    ]
    for name in sorted(values):
        lines.append("%s  %s" % (name, values[name]))
    GOLDEN.write_text("\n".join(lines) + "\n", encoding="utf-8")


def environment_note() -> str:
    import ezdxf
    import numpy
    try:
        from importlib import metadata
        ocp = metadata.version("cadquery-ocp")
    except Exception:  # pragma: no cover - environment dependent
        ocp = "unknown"
    return "recorded on cadquery-ocp %s, ezdxf %s, numpy %s, python %d.%d" % (
        ocp, ezdxf.__version__, numpy.__version__, sys.version_info[0], sys.version_info[1])


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Render the same plan twice: once in this process, once in a fresh interpreter."""
    work = tmp_path_factory.mktemp("golden")
    plan_path, assembly_path = prepare(work)
    out_a, out_b = work / "out_a", work / "out_b"

    code = render_in_process(plan_path, assembly_path, out_a)
    proc = render_in_subprocess(plan_path, assembly_path, out_b)
    return {"work": work, "plan": plan_path, "assembly": assembly_path,
            "out_a": out_a, "out_b": out_b, "code_a": code, "proc_b": proc}


# --------------------------------------------------------------------------- tests


def test_both_runs_succeed_and_write_every_artefact(rendered):
    assert rendered["code_a"] == 0
    assert rendered["proc_b"].returncode == 0, rendered["proc_b"].stderr
    assert "Traceback" not in rendered["proc_b"].stderr
    for out in (rendered["out_a"], rendered["out_b"]):
        for fid in FIGURE_IDS:
            assert (out / (fid + ".dxf")).is_file()
            assert (out / (fid + ".layout.json")).is_file()   # qa's sidecar, §11.10 #5
            assert (out / (fid + ".qa.json")).is_file()
        assert (out / "reference-numerals.json").is_file()


def test_the_same_plan_renders_to_the_same_digest_in_two_processes(rendered):
    """impl-contract §7 rule 8. Byte-identical output after normalisation is a correctness
    property here, not a nicety — it is what makes a regression visible at all."""
    a, b = digests(rendered["out_a"]), digests(rendered["out_b"])
    assert a == b
    # and a third time, in this process, over the file already on disk
    assert digests(rendered["out_a"]) == a


def test_digests_match_the_recorded_baseline(rendered):
    got = digests(rendered["out_a"])
    assert GOLDEN.is_file(), "缺少基准文件；用 python3 tests/test_golden.py --update 生成"
    expected = read_golden()
    assert got == expected, (
        "渲染结果与 %s 记录的基准不一致。\n"
        "这意味着附图变了：先看清楚哪一个模块改了输出（HLR 收集、layout、labels、sheet），"
        "确认是有意的改动之后，再用 python3 tests/test_golden.py --update 重新生成基准。\n"
        "注意基准依赖本机的 OCCT 构建（%s）。\n实测 %r\n基准 %r"
        % (GOLDEN.name, environment_note(), got, expected))


def test_no_two_figures_share_a_digest(rendered):
    """A sanity check on the digest recipe itself: two genuinely different drawings must not
    hash the same. §5.4 leaves halign/valign out of the canonical form, so this is worth
    pinning explicitly."""
    values = digests(rendered["out_a"])
    assert len(set(values.values())) == len(values)


def test_qa_passes_on_both_figures(rendered):
    for fid in FIGURE_IDS:
        report = json.loads((rendered["out_a"] / (fid + ".qa.json")).read_text(encoding="utf-8"))
        failed = [c["id"] for c in report["checks"] if not c["pass"]]
        assert report["pass"] is True, "%s: %s" % (fid, failed)


def test_figures_carry_only_what_section_8_allows(rendered):
    """impl-contract §8: geometry, leaders, numerals and the caption. No parts table, no part
    names, no internal part codes, no title block — the R2 failure, made unrepeatable."""
    import ezdxf
    from patent_figure import qa as QA
    import re
    forbidden = [re.compile(p) for p in QA.FORBIDDEN_TEXT_PATTERNS]
    for fid in FIGURE_IDS:
        doc = ezdxf.readfile(str(rendered["out_a"] / (fid + ".dxf")))
        layers = sorted({e.dxf.layer for e in doc.modelspace()})
        assert set(layers) <= {"GEOM", "HIDDEN", "LEADER", "NUM", "CAPTION"}, layers
        assert "TABLE" not in layers and "NOTE" not in layers
        assert int(doc.header["$INSUNITS"]) == 4          # millimetres, §5.4
        for entity in doc.modelspace():
            if entity.dxftype() not in ("TEXT", "MTEXT"):
                continue
            text = (entity.dxf.text if entity.dxftype() == "TEXT" else entity.text).strip()
            assert "SYN-" not in text, text               # no part name reaches the sheet
            assert not any(rx.search(text) for rx in forbidden), text
            if entity.dxf.layer == "NUM":
                assert text.isdigit()
                assert entity.dxf.height >= 3.5           # TEXT_FLOOR_MM


def test_reference_numerals_document(rendered):
    doc = json.loads((rendered["out_a"] / "reference-numerals.json").read_text(encoding="utf-8"))
    assert doc["schema"] == "patent-numerals/1"
    numerals = [row["numeral"] for row in doc["numerals"]]
    assert numerals == sorted(numerals)
    assert doc["description_zh"].startswith("附图标记说明：")
    # §4.2: a term nothing labels does not reach the table (SYN-H08 is label:"none")
    assert all("紧固螺钉" != row["term"] for row in doc["numerals"])
    assert "紧固螺钉" not in doc["description_zh"]
    # every numeral in the table is used by at least one figure
    for row in doc["numerals"]:
        assert row["figures"], row


def test_only_renders_a_single_figure(rendered, tmp_path):
    out = tmp_path / "only"
    proc = render_in_subprocess(rendered["plan"], rendered["assembly"], out, ["--only", "fig2"])
    assert proc.returncode == 0, proc.stderr
    assert (out / "fig2.dxf").is_file()
    assert not (out / "fig1.dxf").exists()
    # and it is the same drawing as the full run produced
    assert SH.normalized_digest(out / "fig2.dxf") == \
        SH.normalized_digest(rendered["out_a"] / "fig2.dxf")


def test_cache_hit_produces_an_identical_drawing(rendered, tmp_path):
    """§5.1: 'A cache hit must be bit-identical to a recompute.'"""
    cache = tmp_path / "cache"
    cold = tmp_path / "cold"
    warm = tmp_path / "warm"
    a = render_in_subprocess(rendered["plan"], rendered["assembly"], cold, ["--cache", str(cache)])
    assert a.returncode == 0, a.stderr
    b = render_in_subprocess(rendered["plan"], rendered["assembly"], warm, ["--cache", str(cache)])
    assert b.returncode == 0, b.stderr
    assert digests(cold) == digests(warm) == digests(rendered["out_a"])


def test_preview_writes_a_png(rendered, tmp_path):
    pytest.importorskip("matplotlib")
    out = tmp_path / "preview"
    proc = render_in_subprocess(rendered["plan"], rendered["assembly"], out,
                                ["--only", "fig2", "--preview"])
    assert proc.returncode == 0, proc.stderr
    png = out / "fig2.png"
    assert png.is_file() and png.stat().st_size > 0


def test_missing_plan_is_a_usage_error(tmp_path, rendered):
    proc = render_in_subprocess(tmp_path / "nope.json", rendered["assembly"], tmp_path / "o")
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr


# --------------------------------------------------------------------------- regeneration


def _update() -> int:  # pragma: no cover - developer entry point
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        plan_path, assembly_path = prepare(work)
        out = work / "out"
        code = render_in_process(plan_path, assembly_path, out)
        if code != 0:
            sys.stderr.write("渲染未通过（退出码 %d），基准未更新\n" % code)
            return 1
        values = digests(out)
        write_golden(values, environment_note())
    sys.stdout.write("已更新 %s\n" % GOLDEN)
    for name in sorted(values):
        sys.stdout.write("  %s  %s\n" % (name, values[name]))
    return 0


if __name__ == "__main__":  # pragma: no cover
    if "--update" in sys.argv[1:]:
        raise SystemExit(_update())
    sys.stderr.write("用法：python3 tests/test_golden.py --update\n")
    raise SystemExit(2)


def test_label_none_is_reported_as_intended_not_as_a_defect(rendered):
    """回归：`label:"none"` 的术语被略去时，不能提示用户去删掉它。

    验收实测暴露的矛盾：模型被要求「紧固螺钉这类标准件不用标号」，
    照设计设了 `label:"none"`，渲染器却警告「标记号出现空号，修复：把它从 terms 里删掉」——
    工具在劝模型撤销用户刚提的要求。金样计划里 SYN-H08* 正是 `label:"none"`，
    所以这条能稳定复现。

    分流标准：`none` 是计划里要的结果，只作说明、不给修复动作；
    `once`/`all` 落空才是真事故（编号出现空洞），要给修复路径。
    """
    # 警告走 stderr（它是诊断信息不是产出），所以两个流都读，避免以后改了流向测试静默失效。
    proc = rendered["proc_b"]
    log = (proc.stderr or "") + "\n" + (proc.stdout or "")
    none_lines = [ln for ln in log.splitlines() if "紧固螺钉" in ln]
    assert none_lines, "金样计划里 SYN-H08* 是 label:none，应当有一条说明"
    line = none_lines[0]
    assert 'label:"none"' in line, "说明里应点明这是 label:\"none\" 的结果：%s" % line
    assert "无需处理" in line, "label:none 是计划要的结果，不该要求处理：%s" % line
    assert "删掉" not in line, "不得劝用户删掉一个他刻意设为 none 的术语：%s" % line
