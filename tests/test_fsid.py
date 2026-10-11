"""Containment by file-system identity (WP1 R1j). OCP-free.

Every alias class that bypassed the spelling-based guard in R1g–R1j is exercised here against
``patent_figure.fsid.inside`` / ``versions.protected_path_reason``: symlinked parents, dangling
links, ``alias/..``, letter case on case-insensitive volumes, Unicode normal forms, and roots
that do not exist yet. The volume's name rule is probed, so the case tests assert against the
host's real behaviour and are complemented by monkeypatched rules for the other kind."""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import fsid as F  # noqa: E402
from patent_figure import versions as V  # noqa: E402


def _host_is_case_insensitive(d: Path) -> bool:
    probe = d / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (d / "cASEpROBE").exists()
    finally:
        probe.unlink()


def test_probe_matches_the_volume(tmp_path):
    (tmp_path / "Entry").mkdir()
    F._rules.clear()
    assert F.case_insensitive(str(tmp_path)) == _host_is_case_insensitive(tmp_path)


def test_existing_root_identity_beats_spelling(tmp_path):
    real = tmp_path / "real"
    (real / "out").mkdir(parents=True)
    (tmp_path / "alias").symlink_to(real)
    (real / "anchor").mkdir()
    (tmp_path / "anc").symlink_to(real / "anchor")
    root = real / "out"
    assert F.inside(root / "fig1.dxf", root)
    assert F.inside(tmp_path / "alias" / "out" / "x", root)              # symlinked parent
    assert F.inside(str(tmp_path / "anc") + "/../out/x", root)            # '..' after a link
    assert not F.inside(tmp_path / "elsewhere.json", root)
    if _host_is_case_insensitive(tmp_path):
        assert F.inside(tmp_path / "REAL" / "OUT" / "fig1.dxf", root)     # letter case


def test_future_root_tail_follows_the_volume_rule(tmp_path, monkeypatch):
    root = tmp_path / "first" / "out"                                    # does not exist yet
    assert F.inside(root / "fig1.dxf", root)
    (tmp_path / "dangling").symlink_to(root)                             # link to the future
    assert F.inside(tmp_path / "dangling" / "fig1.dxf", root)
    variant = tmp_path / "FIRST" / "Out" / "fig1.dxf"
    monkeypatch.setattr(F, "case_insensitive", lambda d: True)
    assert F.inside(variant, root)
    monkeypatch.setattr(F, "case_insensitive", lambda d: False)
    assert not F.inside(variant, root), "a case-sensitive volume keeps them distinct"


def test_unicode_normal_forms_are_one_name():
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd and F.names_equal(nfc, nfd, False) and F.names_equal(nfc, "CAFÉ", True)


def _store(tmp_path: Path) -> Path:
    st = tmp_path / "store"
    (st / "results" / "r1").mkdir(parents=True)
    (st / "current.json").write_text("{}", encoding="utf-8")
    return st


def test_reserved_names_by_identity_and_rule(tmp_path, monkeypatch):
    st = _store(tmp_path)
    assert V.protected_path_reason(st / "current.json")
    assert V.protected_path_reason(st / "results" / "r1" / "fig1.dxf")
    assert V.protected_path_reason(st / "refs" / "r1" / "x.lease")      # not existing yet
    assert V.protected_path_reason(tmp_path / "free.json") is None
    insensitive = _host_is_case_insensitive(tmp_path)
    if insensitive:
        # existing entry under another spelling: the SAME inode → reserved, whatever the rule
        monkeypatch.setattr(F, "case_insensitive", lambda d: False)
        assert V.protected_path_reason(st / "CURRENT.JSON")
        assert V.protected_path_reason(st / "Results" / "r1" / "fig1.dxf")
        monkeypatch.undo()
    monkeypatch.setattr(F, "case_insensitive", lambda d: True)
    assert V.protected_path_reason(st / "REFS" / "r1" / "x.lease")      # future name, rule
    monkeypatch.setattr(F, "case_insensitive", lambda d: False)
    if not insensitive:
        assert V.protected_path_reason(st / "REFS" / "r1" / "x.lease") is None
