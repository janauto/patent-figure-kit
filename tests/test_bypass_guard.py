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
    assert _users("zipfile.ZipFile") <= {"patent_figure/export.py", "bench.py"}


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
