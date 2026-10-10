"""Preview tests (WP1 deliverable 5): PNG only on demand / at export, CJK font fallback.

The interactive render path now writes SVGs only; PNGs are produced lazily (served from the
sibling DXF) and at export, by dpi. matplotlib is imported lazily inside render_preview, so a
run that never asks for a PNG never imports it. These tests pin that a PNG *can* be produced
and that Chinese text in the drawing drives the font-fallback path without error.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "tests"))

pytest.importorskip("matplotlib")

import test_golden as tg  # noqa: E402
from patent_figure import sheet as SH  # noqa: E402
import studio_ext as EXT  # noqa: E402


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    work = tmp_path_factory.mktemp("preview")
    plan, asm = tg.prepare(work)
    out = work / "out"
    assert tg.render_in_process(plan, asm, out) == 0
    return out


def test_render_preview_produces_a_png_with_chinese_caption(rendered):
    """fig1 carries a Chinese caption; rendering it to PNG exercises the CJK font fallback
    (_preview_cjk_font) and must not raise or produce an empty raster."""
    png = rendered / "fig1.png"
    SH.render_preview(rendered / "fig1.dxf", png, dpi=150)
    assert png.is_file() and png.stat().st_size > 1000


def test_font_fallback_is_idempotent_and_safe():
    # Calling it twice must be a no-op the second time and never raise (cosmetic path).
    SH._preview_cjk_font()
    SH._preview_cjk_font()


def test_dpi_controls_raster_size(rendered):
    lo = rendered / "fig1_lo.png"
    hi = rendered / "fig1_hi.png"
    SH.render_preview(rendered / "fig1.dxf", lo, dpi=96)
    SH.render_preview(rendered / "fig1.dxf", hi, dpi=300)
    assert hi.stat().st_size > lo.stat().st_size


def test_ensure_preview_png_lazily_renders_from_dxf(rendered):
    target = rendered / "fig2.png"
    if target.exists():
        target.unlink()
    assert EXT.ensure_preview_png(target, dpi=120) is True
    assert target.is_file() and target.stat().st_size > 1000


def test_ensure_preview_png_without_dxf_returns_false(tmp_path):
    assert EXT.ensure_preview_png(tmp_path / "nothere.png") is False
