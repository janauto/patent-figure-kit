"""F1 附图导出包（WP2）：页面合成 + 最终文档逐页检查（OCP 无关；DXF 用 ezdxf 现造）。

每个检查项都有故障注入：dpi 72 字高不达标、页数不等、越过页边、非 A4、图片被拉伸、图号缺墨迹、
页序颠倒、Word 页边过小 / 两图同页 / 多余文字段落、缺 pypdf——全部必须判不通过并给出原因；
导出服务把不通过的文件移进 ``_未通过核验/``，对应清单项 fail，不得报 ok。
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

ezdxf = pytest.importorskip("ezdxf")
pytest.importorskip("matplotlib")

from patent_figure import deliverable as D  # noqa: E402
from patent_figure import deliverable_check as C  # noqa: E402
from patent_figure import export as EX  # noqa: E402
from patent_figure import versions as V  # noqa: E402
from ezdxf.enums import TextEntityAlignment  # noqa: E402


def sheet(path: Path, number: int, height: float = 200.0, text_h: float = 3.5,
          caption: str = None) -> Path:
    """A minimal annotated sheet: a part outline, one numeral + leader, the 「图N」 caption."""
    doc = ezdxf.new("R2018")
    for name in ("GEOM", "LEADER", "NUM", "CAPTION"):
        doc.layers.add(name)
    msp = doc.modelspace()
    msp.add_lwpolyline([(30, 30), (140, 30), (140, 30 + height), (30, 30 + height)],
                       close=True, dxfattribs={"layer": "GEOM"})
    msp.add_line((60, 60), (110, 60 + height * 0.5), dxfattribs={"layer": "GEOM"})
    msp.add_line((140, 100), (152, 100), dxfattribs={"layer": "LEADER"})
    t = msp.add_text("1", dxfattribs={"layer": "NUM", "height": text_h})
    t.set_placement((155, 100), align=TextEntityAlignment.MIDDLE_LEFT)
    c = msp.add_text(caption or ("图%d" % number), dxfattribs={"layer": "CAPTION", "height": 5.0})
    c.set_placement((85, 15), align=TextEntityAlignment.MIDDLE_CENTER)
    doc.saveas(str(path))
    return path


@pytest.fixture()
def two(tmp_path):
    rows = [{"figure": "fig1", "number": 1, "dxf": sheet(tmp_path / "fig1_filing.dxf", 1)},
            {"figure": "fig2", "number": 2,
             "dxf": sheet(tmp_path / "fig2_filing.dxf", 2, height=150, text_h=7.0)}]
    return tmp_path, rows


def _build(rows, sp, out: Path, fmt: str):
    pages = [D.rasterize(r["dxf"], sp, r["figure"], r["number"]) for r in rows]
    (D.write_pdf if fmt == "pdf" else D.write_docx)(pages, out)
    return pages


def _check(path, rows, sp):
    return C.check_document(path, C.sources_from(rows, sp["area_mm"]), sp)


# --------------------------------------------------------------------------- happy path


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_good_document_passes_every_page_check(two, fmt):
    if fmt == "docx":
        pytest.importorskip("docx")
    if fmt == "pdf":
        pytest.importorskip("pypdf")
    tmp, rows = two
    sp = D.spec("standard", 300)
    out = tmp / ("doc." + fmt)
    _build(rows, sp, out, fmt)
    rep = _check(out, rows, sp)
    assert rep["ok"], (rep["errors"], [p["errors"] for p in rep["page_checks"]])
    assert rep["pages"] == rep["expected_pages"] == 2
    p1, p2 = rep["page_checks"]
    assert p1["caption"] == "图1" and p2["caption"] == "图2"
    assert p1["caption_ink"] > C.INK_FRACTION_MIN and p2["caption_ink"] > C.INK_FRACTION_MIN
    assert p1["scale"] == 1.0 and abs(p1["dpi_effective"] - 300) < 1
    assert p1["min_text_mm"] == pytest.approx(3.5) and p1["min_text_px"] >= 24
    left, top = p1["image_mm"]["left"], p1["image_mm"]["top"]
    assert left >= 25 - 0.5 and top >= 25 - 0.5
    assert left + p1["image_mm"]["width"] <= 195.5 and top + p1["image_mm"]["height"] <= 282.5


def test_independent_reader_agrees_on_pdf_geometry(two):
    """Cross-check the checker's PDF geometry with a second, independent PDF library."""
    fitz = pytest.importorskip("fitz")
    pytest.importorskip("pypdf")
    tmp, rows = two
    sp = D.spec("standard", 300)
    out = tmp / "doc.pdf"
    _build(rows, sp, out, "pdf")
    rep = _check(out, rows, sp)
    doc = fitz.open(str(out))
    for page, pc in zip(doc, rep["page_checks"]):
        xref = [x for x in page.get_images() if page.get_image_rects(x[0])][0][0]
        r = page.get_image_rects(xref)[0]
        mm = 25.4 / 72.0
        assert abs(r.x0 * mm - pc["image_mm"]["left"]) < 0.2
        assert abs(r.y0 * mm - pc["image_mm"]["top"]) < 0.2
        assert abs(r.width * mm - pc["image_mm"]["width"]) < 0.2
        assert abs(page.rect.width * mm - 210) < 0.2 and abs(page.rect.height * mm - 297) < 0.2


# --------------------------------------------------------------------------- fault injection


@pytest.mark.parametrize("fmt", ["pdf", "docx"])
def test_dpi_72_fails_the_raster_text_height(two, fmt):
    if fmt == "docx":
        pytest.importorskip("docx")
    if fmt == "pdf":
        pytest.importorskip("pypdf")
    tmp, rows = two
    sp = D.spec("standard", 72)
    out = tmp / ("low." + fmt)
    _build(rows, sp, out, fmt)
    rep = _check(out, rows, sp)
    assert not rep["ok"]
    assert any("px" in e for e in rep["page_checks"][0]["errors"])
    assert rep["page_checks"][0]["min_text_px"] < 24


def test_conservative_preset_shrinks_tall_sheets_and_the_check_says_so(tmp_path):
    pytest.importorskip("pypdf")
    rows = [{"figure": "fig1", "number": 1,
             "dxf": sheet(tmp_path / "tall.dxf", 1, height=238.0, text_h=3.5)}]
    sp = D.spec("conservative", 300)
    out = tmp_path / "c.pdf"
    _build(rows, sp, out, "pdf")
    rep = _check(out, rows, sp)
    pc = rep["page_checks"][0]
    assert pc["scale"] < 1.0 and not rep["ok"]
    assert any("缩放后最小字高" in e for e in pc["errors"])
    sp2 = D.spec("standard", 300)                       # the same sheet fits 1:1 in 170×257
    _build(rows, sp2, tmp_path / "s.pdf", "pdf")
    assert _check(tmp_path / "s.pdf", rows, sp2)["ok"]


def test_page_count_must_equal_figure_count(two):
    pytest.importorskip("pypdf")
    tmp, rows = two
    sp = D.spec("standard", 300)
    _build(rows[:1], sp, tmp / "one.pdf", "pdf")
    rep = _check(tmp / "one.pdf", rows, sp)
    assert not rep["ok"] and "页数 1 ≠ 图数 2" in rep["errors"][0]


def test_swapped_page_order_is_caught_by_the_figure_number(two):
    pytest.importorskip("pypdf")
    tmp, rows = two
    sp = D.spec("standard", 300)
    _build(list(reversed(rows)), sp, tmp / "rev.pdf", "pdf")
    rep = _check(tmp / "rev.pdf", rows, sp)
    assert not rep["ok"]
    assert any("图号应为「图1」" in e or "拉伸" in e for e in rep["page_checks"][0]["errors"])


def test_missing_caption_ink_is_caught_from_the_pixels(two):
    pytest.importorskip("pypdf")
    tmp, rows = two
    sp = D.spec("standard", 300)
    pages = [D.rasterize(r["dxf"], sp, r["figure"], r["number"]) for r in rows]
    # erase the caption area from page 1's raster only (the source DXF still says 图1)
    src = C.Source("fig1", 1, rows[0]["dxf"], sp["area_mm"])
    bx0, by0, bx1, by1 = D.caption_box(src.caption)
    wx0, wy0, wx1, wy1 = src.window
    g = pages[0].gray
    H, W = g.shape
    c0, c1 = int((bx0 - wx0) / (wx1 - wx0) * W) - 40, int((bx1 - wx0) / (wx1 - wx0) * W) + 40
    r0, r1 = int((wy1 - by1) / (wy1 - wy0) * H) - 40, int((wy1 - by0) / (wy1 - wy0) * H) + 40
    g[max(r0, 0):r1, max(c0, 0):c1] = 255
    D.write_pdf(pages, tmp / "noink.pdf")
    rep = _check(tmp / "noink.pdf", rows, sp)
    assert not rep["page_checks"][0]["ok"]
    assert any("没有墨迹" in e for e in rep["page_checks"][0]["errors"])
    assert rep["page_checks"][1]["ok"]


def _pdf_with_placement(path: Path, page: "D.Page", page_mm=(210.0, 297.0), rect=None):
    from matplotlib.backends.backend_pdf import PdfPages
    from matplotlib.figure import Figure
    pw, ph = page_mm
    x, top, w, h = rect or (page.placement["x"], page.placement["top"], page.placement["w"],
                            page.placement["h"])
    with PdfPages(str(path)) as pdf:
        fig = Figure(figsize=(pw / 25.4, ph / 25.4), dpi=72)
        ax = fig.add_axes([x / pw, (ph - top - h) / ph, w / pw, h / ph])
        ax.set_axis_off()
        ax.imshow(page.gray, cmap="gray", vmin=0, vmax=255, interpolation="none", aspect="auto")
        pdf.savefig(fig)


def test_margin_violation_non_a4_and_stretching_are_each_caught(tmp_path):
    pytest.importorskip("pypdf")
    rows = [{"figure": "fig1", "number": 1, "dxf": sheet(tmp_path / "a.dxf", 1)}]
    sp = D.spec("standard", 300)
    page = D.rasterize(rows[0]["dxf"], sp, "fig1", 1)
    pl = page.placement
    _pdf_with_placement(tmp_path / "left.pdf", page, rect=(8.0, pl["top"], pl["w"], pl["h"]))
    errs = _check(tmp_path / "left.pdf", rows, sp)["page_checks"][0]["errors"]
    assert any("越过页边" in e for e in errs)
    _pdf_with_placement(tmp_path / "letter.pdf", page, page_mm=(215.9, 279.4))
    errs = _check(tmp_path / "letter.pdf", rows, sp)["page_checks"][0]["errors"]
    assert any("不是 A4" in e for e in errs)
    _pdf_with_placement(tmp_path / "stretch.pdf", page,
                        rect=(pl["x"], pl["top"], pl["w"], pl["h"] * 0.8))
    errs = _check(tmp_path / "stretch.pdf", rows, sp)["page_checks"][0]["errors"]
    assert any("拉伸" in e for e in errs)


def test_missing_pypdf_is_reported_as_unverifiable_never_ok(two, monkeypatch):
    tmp, rows = two
    sp = D.spec("standard", 300)
    _build(rows, sp, tmp / "x.pdf", "pdf")
    monkeypatch.setattr(C, "pdf_checker_available", lambda: False)
    rep = _check(tmp / "x.pdf", rows, sp)
    assert rep["ok"] is False and "pypdf" in rep["errors"][0]


def _rewrite_docx(src: Path, dst: Path, fn, part: str = "word/document.xml"):
    with zipfile.ZipFile(str(src)) as zin, zipfile.ZipFile(str(dst), "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == part:
                if part.startswith("word/media/"):          # binary part: fn gets/returns bytes
                    data = fn(data)
                else:
                    data = fn(data.decode("utf-8")).encode("utf-8")
            zout.writestr(item, data)


def test_docx_margins_page_breaks_and_stray_text_are_checked(two):
    pytest.importorskip("docx")
    import re
    tmp, rows = two
    sp = D.spec("standard", 300)
    good = tmp / "good.docx"
    _build(rows, sp, good, "docx")
    assert _check(good, rows, sp)["ok"]
    _rewrite_docx(good, tmp / "m.docx",
                  lambda x: re.sub(r'w:left="\d+"', 'w:left="567"', x))       # 10 mm
    rep = _check(tmp / "m.docx", rows, sp)
    assert not rep["ok"] and any("页边" in e for e in rep["errors"])
    _rewrite_docx(good, tmp / "b.docx", lambda x: x.replace("<w:pageBreakBefore/>", ""))
    rep = _check(tmp / "b.docx", rows, sp)
    assert not rep["ok"] and any("没有分页" in e for e in rep["errors"])
    _rewrite_docx(good, tmp / "t.docx", lambda x: x.replace(
        "<w:sectPr", "<w:p><w:r><w:t>多余说明</w:t></w:r></w:p><w:sectPr", 1))
    rep = _check(tmp / "t.docx", rows, sp)
    assert not rep["ok"] and any("图片以外的文字" in e for e in rep["errors"])


def _first_para_open(x: str) -> int:
    return x.index("<w:p>") if "<w:p>" in x else x.index("<w:p ")


@pytest.mark.parametrize("inject, expect", [
    # a blank page in front: an empty paragraph ending in a page break
    (lambda x: x[:_first_para_open(x)] + '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
     + x[_first_para_open(x):], "换行/分页符"),
    # 200 mm left indent on the first picture paragraph
    (lambda x: x.replace('<w:ind w:left="0"', '<w:ind w:left="11339"', 1), "缩进"),
    # an extra page break inside the first picture paragraph (after the picture)
    (lambda x: x.replace("</w:drawing></w:r>", '</w:drawing></w:r><w:r><w:br w:type="page"/></w:r>',
                         1), "换行/分页符"),
    # an empty extra page between the pictures
    (lambda x: x.replace("<w:p><w:pPr><w:pageBreakBefore/>",
                         "<w:p><w:pPr><w:pageBreakBefore/></w:pPr></w:p><w:p><w:pPr><w:pageBreakBefore/>",
                         1), "行内图片"),
    # template line spacing (1.15) instead of explicit single spacing
    (lambda x: x.replace('w:line="240"', 'w:line="276"', 1), "单倍行距"),
    # a paragraph style (could carry indents / frames)
    (lambda x: x.replace("<w:pPr>", '<w:pPr><w:pStyle w:val="Heading1"/>', 1), "段落属性"),
])
def test_word_structure_is_strict_any_layout_deviation_fails(two, inject, expect):
    """R2 #3: pictures are not pages and positions are not assumed — a blank page in front, a
    200 mm indent, an extra page break, an extra empty page, non-single spacing or a paragraph
    style must each FAIL (they change the real Word layout)."""
    pytest.importorskip("docx")
    tmp, rows = two
    sp = D.spec("standard", 300)
    good = tmp / "good.docx"
    _build(rows, sp, good, "docx")
    ok = _check(good, rows, sp)
    assert ok["ok"] and ok["layout_verified"] == "structural"
    assert ok["layout_note"] == "Word 按文档结构核验，实际排版以 Word 打开为准"
    bad = tmp / "bad.docx"
    _rewrite_docx(good, bad, inject)
    rep = _check(bad, rows, sp)
    assert not rep["ok"], rep
    msgs = " ".join(rep["errors"] + [e for p in rep["page_checks"] for e in p["errors"]])
    assert expect in msgs, msgs


def _squash_png(data: bytes) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        out = io.BytesIO()
        im.resize((im.width // 2, im.height)).save(out, "PNG")
        return out.getvalue()


_NORMAL = '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'


@pytest.mark.parametrize("part, inject, expect", [
    ("word/document.xml", lambda x: x.replace("<a:stretch>", '<a:srcRect b="50000"/><a:stretch>', 1),
     "被裁剪"),
    ("word/document.xml", lambda x: x.replace("<a:xfrm>", '<a:xfrm rot="5400000">', 1), "旋转"),
    ("word/document.xml", lambda x: x.replace("<a:xfrm>", '<a:xfrm flipH="1">', 1), "翻转"),
    ("word/document.xml", lambda x: x.replace('<a:off x="0" y="0"/><a:ext cx="',
                                              '<a:off x="0" y="0"/><a:ext cx="1', 1),
     "内部变换尺寸"),
    ("word/document.xml", lambda x: x.replace('<a:blip r:embed="', '<a:blip r:embed="', 1)
     .replace('"/><a:stretch>', '"><a:grayscl/></a:blip><a:stretch>', 1), "图像效果"),
    ("word/document.xml", lambda x: x.replace('<w:ind w:left="0"', '<w:ind w:leftChars="20000" w:left="0"', 1),
     "字符单位"),
    ("word/document.xml", lambda x: x.replace('<w:ind w:left="0"', '<w:ind w:firstLineChars="200" w:left="0"', 1),
     "字符单位"),
    ("word/document.xml", lambda x: x.replace("<w:docGrid", '<w:vAlign w:val="center"/><w:docGrid', 1),
     "额外设置"),
    ("word/styles.xml", lambda x: x.replace(_NORMAL, _NORMAL + '<w:pPr><w:ind w:leftChars="3000"/></w:pPr>', 1),
     "字符/行单位"),
    ("word/styles.xml", lambda x: x.replace(_NORMAL, _NORMAL + '<w:pPr><w:framePr w:w="2000"/></w:pPr>', 1),
     "framePr"),
    ("word/styles.xml", lambda x: x.replace('<w:pPrDefault><w:pPr><w:spacing w:before="0"',
                                            '<w:pPrDefault><w:pPr><w:spacing w:beforeLines="500" w:before="0"', 1),
     "字符/行单位"),
    ("word/media/image1.png", _squash_png, "宽高比"),
])
def test_word_picture_and_inherited_formatting_are_whitelisted(two, part, inject, expect):
    """R2b #2: the full check_docx rejects a cropped / rotated / flipped / resized-inside /
    effect-laden picture, character-unit indents, a page-level vertical alignment, inherited
    indents / frames / line-unit spacing from the default style or docDefaults, and media whose
    pixel aspect does not match the displayed size."""
    pytest.importorskip("docx")
    tmp, rows = two
    sp = D.spec("standard", 300)
    good = tmp / "good.docx"
    _build(rows, sp, good, "docx")
    assert _check(good, rows, sp)["ok"]
    bad = tmp / "bad.docx"
    _rewrite_docx(good, bad, inject, part)
    with zipfile.ZipFile(str(good)) as a, zipfile.ZipFile(str(bad)) as b:
        assert a.read(part) != b.read(part), "注入没有生效"
    rep = _check(bad, rows, sp)
    assert not rep["ok"], rep
    msgs = " ".join(rep["errors"] + [e for p in rep["page_checks"] for e in p["errors"]])
    assert expect in msgs, msgs


@pytest.mark.parametrize("where", ["docPr", "cNvPr"])
@pytest.mark.parametrize("value", ["1", "true"])
def test_word_hidden_picture_is_refused(two, where, value):
    """R2c: a picture marked hidden (on wp:docPr or pic:nvPicPr/pic:cNvPr) is not displayed by
    Word — the full check_docx must refuse it, by name."""
    pytest.importorskip("docx")
    tmp, rows = two
    sp = D.spec("standard", 300)
    good = tmp / "good.docx"
    _build(rows, sp, good, "docx")
    tag = '<wp:docPr id="1"' if where == "docPr" else '<pic:cNvPr id="1"'
    bad = tmp / "hidden.docx"
    _rewrite_docx(good, bad, lambda x: x.replace(tag, tag + ' hidden="%s"' % value, 1))
    rep = _check(bad, rows, sp)
    assert not rep["ok"]
    assert any("隐藏" in e for e in rep["errors"]), rep["errors"]


@pytest.mark.parametrize("inject", [
    lambda x: x.replace('<wp:docPr id="1"', '<wp:docPr id="1" title="x"', 1),           # attribute
    lambda x: x.replace("<pic:cNvPicPr/>", '<pic:cNvPicPr preferRelativeResize="0"/>', 1),
    lambda x: x.replace('</a:prstGeom>', '</a:prstGeom><a:ln w="9525"/>', 1),     # element
    lambda x: x.replace('<wp:extent ', '<wp:extent foo="1" ', 1),
    lambda x: x.replace("<w:r><w:rPr>", '<w:r w:rsidR="00A1"><w:rPr>', 1),
    lambda x: x.replace('<w:jc w:val="center"/>', '<w:jc w:val="center" w:extra="1"/>', 1),
])
def test_word_unknown_attribute_or_element_is_refused(two, inject):
    """R2c: the whitelist is closed — any attribute or child the writer does not produce, anywhere
    in the picture or on the page paragraph, fails the check (no more one-off blind spots)."""
    pytest.importorskip("docx")
    tmp, rows = two
    sp = D.spec("standard", 300)
    good = tmp / "good.docx"
    _build(rows, sp, good, "docx")
    bad = tmp / "unk.docx"
    _rewrite_docx(good, bad, inject)
    with zipfile.ZipFile(str(good)) as a, zipfile.ZipFile(str(bad)) as b:
        assert a.read("word/document.xml") != b.read("word/document.xml"), "注入没有生效"
    rep = _check(bad, rows, sp)
    assert not rep["ok"]
    assert any("未知" in e for e in rep["errors"]), rep["errors"]


_RPR_DEFAULT = "<w:rPrDefault><w:rPr>"


def _add_part(name: str, data: bytes):
    def inject(src: Path, dst: Path):
        with zipfile.ZipFile(str(src)) as zin, zipfile.ZipFile(str(dst), "w") as zout:
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr(name, data)
    return inject


@pytest.mark.parametrize("part, inject, expect", [
    # inherited hidden character formatting (R2d): docDefaults rPr / Normal style rPr
    ("word/styles.xml", lambda x: x.replace(_RPR_DEFAULT, _RPR_DEFAULT + "<w:vanish/>", 1), "隐藏格式"),
    ("word/styles.xml", lambda x: x.replace("<w:qFormat/></w:style>",
                                            "<w:qFormat/><w:rPr><w:vanish/></w:rPr></w:style>", 1),
     "隐藏格式"),
    ("word/styles.xml", lambda x: x.replace('<w:webHidden w:val="0"/>', "<w:webHidden/>", 1),
     "隐藏格式"),
    ("word/styles.xml", lambda x: x.replace('<w:sz w:val="21"/>', '<w:sz w:val="0"/>', 1), "字号"),
    ("word/styles.xml", lambda x: x.replace('<w:color w:val="000000"/>', '<w:color w:val="FFFFFF"/>', 1),
     "白色"),
    ("word/styles.xml", lambda x: x.replace("</w:styles>", '<w:style w:type="character" w:styleId="X">'
                                            '<w:name w:val="X"/><w:rPr><w:specVanish/></w:rPr></w:style></w:styles>', 1),
     "固定模板"),
    # settings / package plumbing
    ("word/settings.xml", lambda x: x.replace("<w:defaultTabStop", "<w:mirrorMargins/><w:defaultTabStop", 1),
     "固定模板"),
    ("word/settings.xml", lambda x: x.replace("<w:defaultTabStop", "<w:doNotDisplayPageBoundaries/><w:defaultTabStop", 1),
     "固定模板"),
    ("[Content_Types].xml", lambda x: x.replace("</Types>", '<Default Extension="jpeg" ContentType="image/jpeg"/></Types>', 1),
     "固定模板"),
    ("word/_rels/document.xml.rels", lambda x: x.replace("</Relationships>",
     '<Relationship Id="rIdH" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/></Relationships>', 1),
     "固定模板"),
    # the run's own explicit visibility switched off
    ("word/document.xml", lambda x: x.replace('<w:r><w:rPr><w:vanish w:val="0"/>', "<w:r><w:rPr><w:vanish/>", 1),
     "隐藏格式"),
])
def test_word_package_is_a_closed_set(two, part, inject, expect):
    """R2d: the whole package must equal the writer's fixed templates — inherited hidden
    formatting (docDefaults / Normal rPr vanish, webHidden, size 0, white text), extra styles,
    settings, content types or relationships all fail; the original export passes."""
    tmp, rows = two
    sp = D.spec("standard", 300)
    good = tmp / "good.docx"
    _build(rows, sp, good, "docx")
    assert _check(good, rows, sp)["ok"]                    # the original export passes
    bad = tmp / "bad.docx"
    _rewrite_docx(good, bad, inject, part)
    with zipfile.ZipFile(str(good)) as a, zipfile.ZipFile(str(bad)) as b:
        assert a.read(part) != b.read(part), "注入没有生效"
    rep = _check(bad, rows, sp)
    assert not rep["ok"], rep
    msgs = " ".join(rep["errors"] + [e for p in rep["page_checks"] for e in p["errors"]])
    assert expect in msgs, msgs


def test_word_package_with_an_extra_part_fails(two):
    tmp, rows = two
    sp = D.spec("standard", 300)
    good = tmp / "good.docx"
    _build(rows, sp, good, "docx")
    bad = tmp / "extra.docx"
    _add_part("word/header1.xml", b"<w:hdr/>")(good, bad)
    rep = _check(bad, rows, sp)
    assert not rep["ok"] and any("不会产生的部件" in e for e in rep["errors"])


def test_written_word_sets_every_layout_property_explicitly(two):
    pytest.importorskip("docx")
    tmp, rows = two
    sp = D.spec("standard", 300)
    _build(rows, sp, tmp / "w.docx", "docx")
    with zipfile.ZipFile(str(tmp / "w.docx")) as zf:
        xml = zf.read("word/document.xml").decode("utf-8")
    assert xml.count('w:line="240"') == 2 and xml.count('<w:jc w:val="center"/>') == 2
    assert xml.count("<w:pageBreakBefore/>") == 1 and xml.count("<w:sectPr") == 1


# --------------------------------------------------------------------------- export service


def _publish(store, rid, tmp):
    vd = store.begin(rid)
    arts = ["reference-numerals.json"]
    figs = []
    for k in (1, 2):
        fid = "fig%d" % k
        for suf in ("", "_annotated", "_filing"):
            sheet(vd / ("%s%s.dxf" % (fid, suf)), k, height=180 + 10 * k)
            (vd / ("%s%s.svg" % (fid, suf))).write_text("<svg/>", encoding="utf-8")
            arts += ["%s%s.dxf" % (fid, suf), "%s%s.svg" % (fid, suf)]
        figs.append({"id": fid, "pass": True, "number": k,
                     "annotation": {"figure_description": "图%d为测试示意图；" % k}})
    (vd / "reference-numerals.json").write_text(json.dumps({"description_zh": "1—底座"}),
                                                encoding="utf-8")
    store.write_result(rid, {"ok": True, "figures": figs})
    store.seal(rid, {"ok": True, "required_artefacts": arts}, 1)
    with V.state_lock(store.root):
        store.commit_pointer(rid, 1)
    return vd


def test_export_service_builds_checks_and_quarantines(tmp_path):
    pytest.importorskip("docx")
    pytest.importorskip("pypdf")
    store = V.VersionStore(tmp_path / "ws")
    _publish(store, "r1", tmp_path)
    with EX.acquire(store.root) as pin:
        ok = EX.build_bundle(pin, tmp_path / "ok", variants=("annotated", "filing"),
                             formats=("pdf", "docx", "dxf", "dwg"), dpi=300,
                             run_cli=lambda *a, **k: None, description_txt=True,
                             checklist_txt=True, dwg_available=False)
    assert ok["ok"] is True, ok["reasons"]
    assert {d for d in ok["documents"]} == {"说明书附图_递交版.pdf", "说明书附图_递交版.docx",
                                           "说明书附图_交底版.pdf", "说明书附图_交底版.docx"}
    assert all(v == "skip" for k, v in ok["manifest"].items() if k.endswith("/dwg"))
    assert "DWG 转换器" in ok["reasons"]["fig1/递交版/dwg"]
    for name in ("附图说明.txt", "核对清单.txt", "核验报告.json"):
        assert name in ok["files"]
    checklist = (tmp_path / "ok" / "核对清单.txt").read_text(encoding="utf-8")
    assert "人工核对" in checklist and "电子申请文件格式要求说明" in checklist
    report = json.loads((tmp_path / "ok" / "核验报告.json").read_text(encoding="utf-8"))
    assert report["missing_files"] == [] and report["documents"]
    with EX.acquire(store.root) as pin:
        low = EX.build_bundle(pin, tmp_path / "low", variants=("filing",),
                              formats=("pdf", "dxf"), dpi=72, checklist_txt=True)
    assert low["ok"] is False
    assert low["manifest"]["fig1/递交版/pdf"] == "fail" and low["manifest"]["fig1/递交版/dxf"] == "ok"
    assert "_未通过核验/说明书附图_递交版.pdf" in low["files"]
    assert "说明书附图_递交版.pdf" not in low["files"]
    item = next(i for i in low["items"] if i["format"] == "pdf")
    assert item["status"] == "fail" and item["file"].startswith("_未通过核验/")
    assert "不要递交" in (tmp_path / "low" / "核对清单.txt").read_text(encoding="utf-8")
    assert store.referenced() == set()


def test_a_checker_that_raises_quarantines_the_written_document(tmp_path, monkeypatch):
    """R2 #4: written OK, then the checker throws → the document is NOT verified, so it is a
    failed one: moved to _未通过核验/, item fail with the reason, nothing left at the package root
    or at the root of the archive."""
    pytest.importorskip("docx")
    store = V.VersionStore(tmp_path / "ws")
    _publish(store, "r1", tmp_path)

    def boom(*a, **k):
        raise RuntimeError("checker exploded")
    monkeypatch.setattr(C, "check_document", boom)
    with EX.acquire(store.root) as pin:
        out = EX.build_bundle(pin, tmp_path / "b", variants=("filing",),
                              formats=("pdf", "docx", "dxf"), dpi=150, checklist_txt=True)
    assert out["ok"] is False
    for fmt in ("pdf", "docx"):
        name = "说明书附图_递交版.%s" % fmt
        assert out["manifest"]["fig1/递交版/%s" % fmt] == "fail"
        assert "核验程序异常" in out["reasons"]["fig1/递交版/%s" % fmt]
        assert not (tmp_path / "b" / name).exists()
        assert (tmp_path / "b" / "_未通过核验" / name).is_file()
        assert out["documents"][name]["path"] == "_未通过核验/" + name
    EX.zip_dir(tmp_path / "b", tmp_path / "b.zip")
    names = zipfile.ZipFile(str(tmp_path / "b.zip")).namelist()
    assert not any(n.startswith("说明书附图_") for n in names), names
    assert "_未通过核验/说明书附图_递交版.pdf" in names
    assert out["manifest"]["fig1/递交版/dxf"] == "ok"


def test_fixed_template_word_opens_in_an_independent_reader(two):
    """The hand-built package is a valid WordprocessingML document for a third-party reader
    (python-docx, when installed): two paragraphs, two inline pictures at the written size."""
    docx = pytest.importorskip("docx")
    tmp, rows = two
    sp = D.spec("standard", 300)
    pages = _build(rows, sp, tmp / "w.docx", "docx")
    doc = docx.Document(str(tmp / "w.docx"))
    assert len(doc.paragraphs) == 2 and len(doc.inline_shapes) == 2
    for shape, page in zip(doc.inline_shapes, pages):
        assert abs(shape.width.mm - page.placement["w"]) < 0.01
        assert abs(shape.height.mm - page.placement["h"]) < 0.01
    sec = doc.sections[0]
    assert abs(sec.page_width.mm - 210) < 0.1 and abs(sec.left_margin.mm - 25) < 0.1
    assert doc.core_properties.title == "说明书附图"


# --------------------------------------------------------------------------- R2e: media transparency


def _rgba_transparent_png(data: bytes) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        rgba = im.convert("RGBA")
    rgba.putalpha(0)                                  # every pixel fully transparent
    out = io.BytesIO()
    rgba.save(out, "PNG")
    return out.getvalue()


def _gray_trns_png(data: bytes) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        g = im.convert("L")
    out = io.BytesIO()
    g.save(out, "PNG", transparency=0)                # black is transparent (tRNS)
    return out.getvalue()


@pytest.mark.parametrize("mutate, expect", [(_rgba_transparent_png, ("PNG", "没有墨迹")),
                                            (_gray_trns_png, ("tRNS",))])
def test_word_transparent_media_is_refused(two, mutate, expect):
    """R2e: same XML, same picture size — only the embedded PNG made transparent (RGBA alpha 0,
    or grayscale with a black tRNS). Composited onto white the page is blank: refused."""
    tmp, rows = two
    sp = D.spec("standard", 300)
    good = tmp / "good.docx"
    _build(rows, sp, good, "docx")
    assert _check(good, rows, sp)["ok"]
    bad = tmp / "transparent.docx"
    _rewrite_docx(good, bad, mutate, "word/media/image1.png")
    rep = _check(bad, rows, sp)
    assert not rep["ok"]
    msgs = " ".join(rep["errors"] + [e for p in rep["page_checks"] for e in p["errors"]])
    assert all(e in msgs for e in expect), msgs
    # whatever survives is measured on the page as SEEN (composited onto white): the solid black
    # strokes of 「图N」 are gone, only anti-aliased edges remain
    assert rep["page_checks"][0]["caption_ink"] < _check(good, rows, sp)["page_checks"][0]["caption_ink"]


def _pdf_mutate(src: Path, dst: Path, fn):
    from pypdf import PdfWriter
    w = PdfWriter(clone_from=str(src))
    fn(w, w.pages[0])
    with open(str(dst), "wb") as fh:
        w.write(fh)


def _pdf_add_smask(w, page):
    from pypdf.generic import DecodedStreamObject, NameObject, NumberObject
    xo = page["/Resources"]["/XObject"]
    img = xo[list(xo.keys())[0]].get_object()
    sm = DecodedStreamObject()
    width, height = int(img["/Width"]), int(img["/Height"])
    sm.set_data(bytes(width * height))                  # alpha 0 everywhere
    sm.update({NameObject("/Type"): NameObject("/XObject"),
               NameObject("/Subtype"): NameObject("/Image"),
               NameObject("/Width"): NumberObject(width), NameObject("/Height"): NumberObject(height),
               NameObject("/ColorSpace"): NameObject("/DeviceGray"),
               NameObject("/BitsPerComponent"): NumberObject(8)})
    img[NameObject("/SMask")] = w._add_object(sm)


def _pdf_alpha_zero(w, page):
    from pypdf.generic import FloatObject, NameObject
    for _n, gs in page["/Resources"]["/ExtGState"].items():
        gs.get_object()[NameObject("/ca")] = FloatObject(0)


def _pdf_paint_over(w, page):
    from pypdf.generic import DecodedStreamObject, NameObject
    cs = DecodedStreamObject()
    cs.set_data(page.get_contents().get_data() + b"\nq 1 g 0 0 595 842 re f Q\n")
    page[NameObject("/Contents")] = w._add_object(cs)


def _pdf_clip_half(w, page):
    from pypdf.generic import DecodedStreamObject, NameObject
    data = page.get_contents().get_data()
    i = data.index(b" re")
    j = data.rindex(b"\n", 0, i) + 1 if b"\n" in data[:i] else 0
    nums = data[j:i].split()
    nums[-1] = b"%.3f" % (float(nums[-1]) / 2)
    cs = DecodedStreamObject()
    cs.set_data(data[:j] + b" ".join(nums) + data[i:])
    page[NameObject("/Contents")] = w._add_object(cs)


@pytest.mark.parametrize("mutate, expect", [(_pdf_add_smask, "/SMask"),
                                            (_pdf_alpha_zero, "透明度"),
                                            (_pdf_paint_over, "盖住"),
                                            (_pdf_clip_half, "裁切")])
def test_pdf_image_transparency_masks_and_overpaint_are_refused(two, mutate, expect):
    """R2e: the PDF page image must stay fully visible — a soft mask (SMask, alpha 0), a graphics
    state with alpha 0, painting over the image after it is drawn, or a clip that cuts it, each
    fail the full check; the original passes."""
    pytest.importorskip("pypdf")
    tmp, rows = two
    sp = D.spec("standard", 300)
    good = tmp / "good.pdf"
    _build(rows, sp, good, "pdf")
    assert _check(good, rows, sp)["ok"]
    bad = tmp / "bad.pdf"
    _pdf_mutate(good, bad, mutate)
    assert bad.read_bytes() != good.read_bytes()
    rep = _check(bad, rows, sp)
    assert not rep["ok"], rep
    msgs = " ".join(rep["errors"] + [e for p in rep["page_checks"] for e in p["errors"]])
    assert expect in msgs, msgs


def test_written_media_passes_the_png_whitelist(two):
    tmp, rows = two
    sp = D.spec("standard", 300)
    pages = [D.rasterize(r["dxf"], sp, r["figure"], r["number"]) for r in rows]
    for pg in pages:
        assert C.png_whitelist(pg.png_bytes()) == []
    assert C.png_whitelist(_gray_trns_png(pages[0].png_bytes()))
    assert C.png_whitelist(_rgba_transparent_png(pages[0].png_bytes()))
