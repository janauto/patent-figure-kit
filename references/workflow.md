# Patent figure to verified DWG workflow

Use this reference when the input is not already a clean DXF, when several figures must be combined, or when Chinese text must survive DWG conversion.

## 1. Preserve and classify the source

Work in a new output directory. Inventory DWG, DXF, SVG, PDF, PNG/JPG, figure-generation scripts, fonts, and any existing previews before drawing.

Choose the highest-quality geometry authority in this order:

1. 3D CAD assembly (STEP/STP/IGES) — compute the view with analytic hidden-line removal.
   See [cad-source-to-drawing.md](cad-source-to-drawing.md).
2. Controlled DWG/DXF or other vector CAD.
3. SVG/PDF vector paths.
4. Figure-generation source such as Matplotlib.
5. Raster image, which requires tracing or reconstruction.

When a 3D model exists, prefer it even if a 2D drawing is also available: the model carries the
real geometry, and views, sections, and exploded states can be regenerated instead of redrawn.

When only a raster exists, preserve proportions but do not invent real-world dimensions. State that the result is a diagrammatic redraw unless dimensions are independently known.

## 2. Build a text decision table

Extract all text containing digits and classify it as:

- Patent reference numeral to remove.
- Figure caption such as `图1` to remove when requested.
- Functional identifier such as `A1`, `J2`, or `CH1` to retain.
- Engineering value such as `24V`, `10 mm`, a tolerance, or a pin number to retain.
- Ambiguous and requiring review.

The reusable cleaner deliberately reports candidates before destructive deletion. Prefer a user-approved or source-derived list over a universal regex.

## 3. Reconstruct vector geometry

For Matplotlib sources, map artists rather than tracing the rendered PNG:

- `Line2D` with two vertices -> DXF `LINE`.
- `Line2D` with more vertices -> `LWPOLYLINE` or 2D `POLYLINE`.
- `Rectangle` -> closed polyline.
- `Circle` -> `CIRCLE`.
- `FancyArrowPatch` -> shaft plus closed triangular arrowhead.
- Text -> `MTEXT` for editable DXF, or glyph outlines for font-independent DWG.

Ignore the source dash pattern when the requested output is all solid. Assign every exported object and its layer the `CONTINUOUS` linetype.

For raster tracing, use straight segments, orthogonal constraints, circles/arcs, and consistent arrowheads. Avoid producing thousands of noisy micro-segments where a line or arc is sufficient.

## 4. Produce two text strategies when needed

Editable DXF:

- Keep retained labels as `TEXT` or `MTEXT`.
- Use a declared Chinese-capable style.
- This is the preferred editing master.

Portable DWG:

- First try Autodesk-native conversion; it normally preserves Unicode better than third-party converters.
- If the receiving machine lacks the font or the labels are corrupted, convert glyphs to closed polylines using an available CJK font.
- Treat outlined glyphs as geometry. They are visible without the font but cannot be edited as text.

Never silently replace Chinese labels with mojibake or omit them just to make conversion succeed.

## 5. Combine multiple figures

Keep each figure as an individual DXF/DWG and optionally create a combined sheet. Use deterministic offsets and enough spacing to avoid overlap. Keep the same scale unless the user requests normalization.

Recommended deliverables:

- One DXF and DWG per figure.
- One combined DXF and DWG.
- One preview PNG per figure.
- A cleanup report listing removed and preserved numeric labels.

## 5b. Sheet conventions for Chinese mechanical drawings

Chinese text in FangSong (`simfang.ttf`; GB/T 14691 specifies 长仿宋体), numerals and Latin in the
AutoCAD stick font (`txt.shx`). Leaders are an angled line from the part plus a short horizontal
landing with the numeral sitting on the landing, not a dot with a floating number. A part table sits
at the lower left as `NO. | NAME | QTY | REMARK`, the remark carrying the fixing or fit method.
Write font *names* into the DXF style, not absolute paths.

## 6. Convert with AutoCAD Core Console on macOS

The common AutoCAD 2026 executable is:

```text
/Applications/Autodesk/AutoCAD 2026/AutoCAD 2026.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole
```

AutoCAD's command-script prompt may misread a Chinese save path even when `/i` can open a Chinese input path. Save to an ASCII-only temporary DWG and move the completed file to the intended path afterward. `scripts/autocad_core_dxf_to_dwg.py` implements this workaround.

Do not use `-SAVEAS` on AutoCAD for macOS Core Console; it is not available in the tested 2026 build. Use `SAVEAS`, answer the format prompt (for example `2018`), then provide the ASCII temporary filename.

## 7. Validate structurally and visually

Structural DXF checks:

- Non-continuous layer count is zero.
- Non-continuous entity count is zero.
- Rejected reference candidates are zero after applying the approved allowlist.
- Entity count is nonzero.

Without AutoCAD, use `scripts/libredwg_dxf_to_dwg.py` and treat a clean `dwgread` re-parse as the
AUDIT equivalent. State which engine was used.

AutoCAD DWG checks:

- Open with Autodesk Core Console or AutoCAD.
- Run `AUDIT` and save any fixes.
- Run `AUDIT` a second time; the final result should report zero errors fixed.
- Report entity count and explicit non-continuous count.

Visual checks:

- Compare preview and source side by side.
- Inspect arrowheads, line joins, junction dots, box boundaries, and text placement.
- Confirm removed reference labels did not leave punctuation fragments or empty parentheses.
- Confirm retained identifiers such as `A1/A2/A3` remain present.
