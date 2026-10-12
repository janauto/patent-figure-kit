"""DXF → interactive SVG preview (moved verbatim from ``plan_studio.py`` in WP2).

Pure: ezdxf only — no fastapi, no OCP — so command-line tools (``suggest_figure_plan.py``,
``export_figure_package.py``) render the same preview the browser shows. ``plan_studio.dxf_to_svg``
is this very function (re-exported), so every existing caller is unchanged.
"""

from __future__ import annotations

from pathlib import Path

#: SVG stroke widths in millimetres, preview-only (the DXF is the deliverable, this is a picture
#: of it). GEOM heavier than LEADER matches how the sheet is meant to read on paper.
SVG_STROKE = {"GEOM": 0.35, "HIDDEN": 0.2, "LEADER": 0.18, "TABLE": 0.25}
SVG_MARGIN_MM = 6.0


def dxf_to_svg(dxf: Path) -> str:
    """Minimal DXF-to-SVG for THIS renderer's known output vocabulary.

    Hand-rolled instead of ezdxf's SVGBackend for one reason: the preview must be
    interactive — every NUM numeral needs ``data-numeral`` so a click can highlight the
    part in 3D — and a generic backend gives no per-entity hooks. The sheet only ever
    contains LINE / LWPOLYLINE / CIRCLE / TEXT on known layers, so sixty lines cover it.

    Y handling per the sheet's +Y-up convention: geometry sits in a ``scale(1,-1)`` group;
    text is emitted OUTSIDE that group at ``y' = -y``, because a flipped group mirrors
    glyphs. All coordinates are millimetres straight from the DXF.
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf))
    msp = doc.modelspace()
    xs, ys = [], []

    def track(x: float, y: float) -> None:
        xs.append(float(x)); ys.append(float(y))

    shapes, texts = [], []
    for e in msp:
        kind = e.dxftype()
        layer = e.dxf.layer
        if kind == "LINE":
            (x1, y1, _), (x2, y2, _) = e.dxf.start, e.dxf.end
            track(x1, y1); track(x2, y2)
            shapes.append('<line class="ly-%s" x1="%.3f" y1="%.3f" x2="%.3f" y2="%.3f"/>'
                          % (layer, x1, y1, x2, y2))
        elif kind == "LWPOLYLINE":
            pts = [(float(p[0]), float(p[1])) for p in e.get_points()]
            for x, y in pts:
                track(x, y)
            d = " ".join("%.3f,%.3f" % p for p in pts)
            tag = "polygon" if e.closed else "polyline"
            shapes.append('<%s class="ly-%s" points="%s"/>' % (tag, layer, d))
        elif kind == "CIRCLE":
            (cx, cy, _) = e.dxf.center
            r = float(e.dxf.radius)
            track(cx - r, cy - r); track(cx + r, cy + r)
            shapes.append('<circle class="ly-%s dot" cx="%.3f" cy="%.3f" r="%.3f"/>'
                          % (layer, cx, cy, r))
        elif kind == "SOLID":
            pts = [(float(e.dxf.get("vtx%d" % i)[0]), float(e.dxf.get("vtx%d" % i)[1]))
                   for i in range(3)]
            for x, y in pts:
                track(x, y)
            shapes.append('<polygon class="ly-%s solid" points="%s"/>'
                          % (layer, " ".join("%.3f,%.3f" % q for q in pts)))
        elif kind == "TEXT":
            align, p1, _ = e.get_placement()   # ezdxf: (alignment, p1, p2) — 对齐枚举在首位
            x, y = float(p1[0]), float(p1[1])
            h = float(e.dxf.height)
            track(x, y - h); track(x, y + h)
            name = align.name if hasattr(align, "name") else str(align)
            anchor = "end" if name.endswith("RIGHT") else (
                "start" if name.endswith("LEFT") else "middle")
            value = (e.dxf.text.replace("&", "&amp;").replace("<", "&lt;"))
            extra = ""
            if layer == "NUM" and value.strip().isdigit():
                extra = ' data-numeral="%s"' % value.strip()
            texts.append('<text class="ly-%s" x="%.3f" y="%.3f" font-size="%.3f" '
                         'text-anchor="%s"%s>%s</text>'
                         % (layer, x, -y, h, anchor, extra, value))
    if not xs:
        return '<svg xmlns="http://www.w3.org/2000/svg"/>'
    m = SVG_MARGIN_MM
    x0, x1 = min(xs) - m, max(xs) + m
    y0, y1 = min(ys) - m, max(ys) + m
    styles = "".join(".ly-%s{stroke-width:%.2f}" % (k, v) for k, v in SVG_STROKE.items())
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.3f %.3f %.3f %.3f" '
        'font-family="\'Songti SC\', \'STSong\', \'SimSun\', serif">'
        '<style>line,polyline,polygon,circle{stroke:#1a1a1a;fill:none;stroke-linecap:round}'
        'circle.dot,polygon.solid{fill:#1a1a1a;stroke:none}'
        'text.ly-NUM{font-family:Helvetica,Arial,sans-serif}'
        'text{fill:#1a1a1a;dominant-baseline:central}'
        'text[data-numeral]{cursor:pointer}text[data-numeral]:hover{fill:#0a6cbd}%s</style>'
        '<g transform="scale(1,-1)">%s</g>%s</svg>'
        % (x0, -y1, x1 - x0, y1 - y0, styles, "".join(shapes), "".join(texts))
    )
