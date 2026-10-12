"""Static guard for the R1f bypass inventory: every delivery path goes through the three
services — publish (commit), VersionStore.snapshot (read), patent_figure.export (export).

This pins the inventory so a NEW bypass fails CI instead of waiting for the next review:
each rule lists the only files allowed to use a primitive, with the reason."""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _files():
    return sorted(p for p in _SCRIPTS.rglob("*.py"))


def _tokens(path: Path):
    src = path.read_text(encoding="utf-8")
    return list(tokenize.generate_tokens(io.StringIO(src).readline))


def _code_uses(path: Path, dotted: str) -> bool:
    """True if ``a.b(`` style attribute access ``dotted`` (e.g. 'zipfile.ZipFile') occurs in
    CODE (not comments / strings)."""
    names = dotted.split(".")
    toks = [t for t in _tokens(path) if t.type in (tokenize.NAME, tokenize.OP)]
    for i in range(len(toks) - 2 * (len(names) - 1)):
        window = toks[i:i + 2 * len(names) - 1]
        if [t.string for t in window[::2]] == names and all(t.string == "." for t in window[1::2]):
            return True
    return False


def _rel(p: Path) -> str:
    return p.relative_to(_SCRIPTS).as_posix()


def _users(dotted: str):
    return {_rel(p) for p in _files() if _code_uses(p, dotted)}


def test_zip_archives_only_via_the_export_service():
    # bench.py keeps a LABELLED legacy zip only for timing an R1e-era baseline repo that has no
    # version store (export_path = "legacy-zip(baseline repo)"); it never reads a real project.
    # WP2: deliverable_check.py opens the WRITTEN Word file (a zip container) READ-ONLY to verify
    # it page by page — it never creates an archive (pinned by the test below).
    # R2d: deliverable.py writes the .docx CONTAINER of one document (fixed templates) — not an
    # export archive; pinned by test_docx_writer_only_writes_its_own_temp_container below.
    # WP4: patent_figure/zipcheck.py opens DOWNLOADED export bundles READ-ONLY for the static
    # site's asset audit and the deployment smoke test (pinned by the test below).
    assert _users("zipfile.ZipFile") <= {"patent_figure/export.py", "bench.py",
                                         "patent_figure/deliverable_check.py",
                                         "patent_figure/deliverable.py",
                                         "patent_figure/zipcheck.py"}


def test_zipcheck_only_reads():
    src = (_SCRIPTS / "patent_figure" / "zipcheck.py").read_text(encoding="utf-8")
    calls = [line.strip() for line in src.splitlines() if "zipfile.ZipFile(" in line]
    assert calls and all(c.endswith("as zf:") and ", " not in c.split("ZipFile(", 1)[1]
                         for c in calls), calls


def test_docx_writer_only_writes_its_own_temp_container():
    src = (_SCRIPTS / "patent_figure" / "deliverable.py").read_text(encoding="utf-8")
    calls = [line.strip() for line in src.splitlines() if "zipfile.ZipFile(" in line]
    assert calls == ['with zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED) as zf:'], calls
    body = src[src.index("def write_docx("):]
    assert "zipfile.ZipFile(str(tmp)" in body and 'tmp = path.with_name(path.name + ".tmp")' in body


def test_deliverable_check_only_reads_zip_containers():
    src = (_SCRIPTS / "patent_figure" / "deliverable_check.py").read_text(encoding="utf-8")
    calls = [line.strip() for line in src.splitlines() if "zipfile.ZipFile(" in line]
    assert calls and all(", \"w\"" not in c and ", 'w'" not in c and "mode=" not in c
                         for c in calls), calls


def test_pointer_writes_only_in_the_store_and_the_publish_service():
    assert _users("vs.commit_pointer") | _users("self.commit_pointer") | \
        _users("store.commit_pointer") <= {"patent_figure/versions.py", "patent_figure/publish.py"}
    literal = set()
    for p in _files():
        for t in _tokens(p):
            if t.type == tokenize.STRING and t.string.strip("\"'") == "current.json":
                literal.add(_rel(p))
    assert literal == {"patent_figure/versions.py"}


def test_version_pins_only_via_the_export_service():
    assert _users("vs.acquire_verified_version") | _users("store.acquire_verified_version") \
        <= {"patent_figure/export.py"}


def test_in_place_render_only_from_the_publish_service():
    users = set()
    for p in _files():
        for t in _tokens(p):
            if t.type == tokenize.STRING and t.string.strip("\"'") == "--in-place":
                users.add(_rel(p))
    # defined by the renderer, passed only by studio_ext.render_version (the publish service's
    # inner render step, guarded by versions.is_unsealed_version_dir in the renderer)
    assert users == {"render_patent_figure.py", "studio_ext.py"}


def test_no_result_cache_and_no_shared_out_reader():
    for p in _files():
        names = [t.string for t in _tokens(p) if t.type == tokenize.NAME]
        assert "render_state" not in names, _rel(p)
        assert not _code_uses(p, "ws.out"), _rel(p)
        for t in _tokens(p):
            if t.type == tokenize.STRING:
                assert t.string.strip("\"'") != "last_render.json", _rel(p)


#: CLIs that predate WP2 and write a user-named report path. render_patent_figure has its own
#: side-path guard (R1g–R1j); the rest are read-only checkers / generators whose outputs are not
#: delivery paths. Any NEW CLI with an output-path flag must go through patent_figure.pathguard.
_LEGACY_OUTPUT_CLIS = {"annotate_figure_sheet.py", "bench.py", "render_flowchart.py",
                       "doctor.py", "validate_clean_dxf.py", "qa_patent_figure.py",
                       "render_patent_figure.py", "validate_figure_plan.py"}


def test_new_clis_with_output_paths_go_through_the_path_guard():
    """WP2 R2 #2: every CLI that takes an output path (--json / --zip / --instances / -o work
    dir) and is not on the legacy list checks it with ``pathguard.check_outputs`` before writing;
    the long-running ones check again right before writing (defence in depth)."""
    flags = ('add_argument("--json"', 'add_argument("--zip"', '"--json", dest',
             'add_argument("--instances"')
    users = {}
    for p in _files():
        src = p.read_text(encoding="utf-8")
        if any(f in src for f in flags):
            users[_rel(p)] = src
    unguarded = [rel for rel, src in users.items()
                 if Path(rel).name not in _LEGACY_OUTPUT_CLIS
                 and not _code_uses(_SCRIPTS / rel, "pathguard.check_outputs")]
    assert not unguarded, unguarded
    for rel in ("suggest_figure_plan.py", "export_figure_package.py"):
        src = (_SCRIPTS / rel).read_text(encoding="utf-8")
        assert src.count("_guard(") >= 3, rel          # definition + pre-check + late re-check
        assert _code_uses(_SCRIPTS / rel, "pathguard.claim_workdir"), rel
