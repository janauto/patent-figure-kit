# 3D CAD source to patent line drawing

Use this reference when the geometry authority is a 3D assembly (STEP/STP, IGES,
or a native CAD file that can export one) rather than an existing 2D drawing.
This is the highest-fidelity route: the drawing is computed from the solids, so
no tracing, no redrawing, and no dimensional guesswork.

`scripts/cad_hlr_to_dxf.py` implements the pipeline. Run it with `--list-parts`
first to see the assembly's part names before selecting anything.

## 1. Compute the drawing, do not trace it

Run analytic hidden-line removal on the B-rep. OpenCASCADE's `HLRBRep_Algo` is
the same class of algorithm as AutoCAD `FLATSHOT`, SolidWorks drawing views, and
Rhino `Make2D`. It returns exact curves, so the output is real vector geometry at
any scale.

Do not rasterise the model and re-vectorise the image. A raster round trip loses
tangency, breaks long curves into segments, and cannot tell an occluding edge
from a shading boundary. If a previous attempt produced speckled or doubled
lines, that is the signature of a raster-then-trace pipeline.

## 2. Take the outline curves, not only the model edges

`HLRBRep_HLRToShape` exposes several compounds. Collect at least:

- `VCompound` — visible sharp edges.
- `Rg1LineVCompound` — visible smooth (tangent) edges.
- `OutLineVCompound` — **visible silhouette curves of smooth surfaces**.

The third one is the part people miss. The silhouette of a moulded dome, a
fillet, or any blended surface **has no corresponding edge in the B-rep** — it
depends on the view direction. A drawing assembled only from model edges shows
dome and fillet outlines as broken or missing. Use `HCompound`,
`Rg1LineHCompound` and `OutLineHCompound` for hidden lines when the figure needs
them.

## 3. Suppress tangent seams

Surfaces that meet tangentially produce a seam edge with no visual step. Drawing
every seam makes an injection-moulded shell look faceted. Classify each edge by
the angle between the adjacent face normals and drop the ones below roughly 12–15°,
keeping the silhouette curves from step 2 to carry the outline.

## 4. Explode along the real axis, then roll the sheet

Two rules keep an exploded view readable:

**Space parts by their projected footprint, not by a fixed 3D gap.** Project each
part onto the explode direction as it appears on the sheet, then lay the parts out
end to end with one uniform gap. A fixed 3D gap lets a large disc overlap its
neighbours as soon as the string runs diagonally, and the spacing reads unevenly.

**Do not displace parts sideways to fit the page.** Offsetting parts off the
assembly axis produces a staircase and misrepresents the assembly relationship.
To get a diagonal string on the sheet, roll the *view* instead: rotating the up
vector about the view direction turns the whole sheet without moving any part off
axis. `roll_for_axis()` solves for the roll that puts the assembly axis at a
chosen sheet angle; 150–160° reads well for a vertical assembly axis.

## 5. Section views

For a cut view, intersect each solid with a half space, then take the section
faces and fill them with hatch line segments generated in the cutting plane.
Feed the hatch segments through HLR together with the cut solids so the hatching
is occluded correctly. Put hatch lines on their own layer at a lighter lineweight.

## 6. Sheet conventions

> **A patent figure is not an engineering assembly drawing.** These are two
> different sheets with two different rule sets, and mixing them is how v1
> failed: a GB-style parts table carrying internal part codes was drawn onto a
> filing figure. That table violates three separate CNIPA requirements at once —
> 除必需的词语外不得含有其他注释, 附图周围不得有与图无关的框线, and the rule that a
> parts list belongs in the 说明书·附图说明 section rather than on the drawing.
> Read [patent-figure-spec.md](patent-figure-spec.md) A5 / A10 / A20 before you
> put any text on a sheet. **Default to the patent sheet. The engineering sheet
> is opt-in, for internal review only, and never leaves the building as a filing
> copy.**

### 6.1 Patent figure (the default)

Everything on this sheet is geometry, leaders, reference numerals, and the
figure caption. Nothing else.

| Element | Convention |
| --- | --- |
| Chinese text | Caption only. FangSong (`simfang.ttf`); the CNIPA guideline allows 宋体/仿宋体/楷体, and GB/T 14691 specifies 长仿宋体 for drawings |
| Numerals and Latin | AutoCAD stick font (`txt.shx`), or a plain sans when outlining |
| Reference numerals | Arabic numerals only, issued from `terms[]` order — never a part name, code, or quantity |
| Leader | angled line from the part, then a short horizontal landing, numeral sitting just beyond the landing end |
| Caption | figure number plus figure name, centred directly below the drawing |
| Part table | **none.** The part-name list goes into the 说明书·附图说明 text, delivered as `reference-numerals.json` → `description_zh` |
| Title block, frame, scale, units, signatures, dates, company name | **none** |
| Dimensions, tolerances, surface finish, weld symbols | **none** |
| Layers | `GEOM`, `HIDDEN`, `LEADER`, `NUM`, `CAPTION`, `NOTE` — all `CONTINUOUS` (`TABLE` stays empty) |

### 6.2 Engineering assembly sheet (`layout.engineering_table: true` — internal review only)

This is the GB mechanical-drawing convention. It exists so a mechanical reviewer
can check part identity against the model. It is **off by default**, and a file
produced with it is written as `<figure id>_engineering.dxf` so a review copy can
never be mistaken for a filing copy.

| Element | Convention |
| --- | --- |
| Part table | `NO. | NAME | QTY | REMARK`, lower left, remark carries the fixing method — on the `TABLE` layer |
| Everything else | as in 6.1 |

Enabling it is an explicit plan edit (`layout.engineering_table: true`, see
`docs/impl-contract.md` §4.2 and §8), not a CLI convenience. Never ship the
`_engineering.dxf` to a patent agent, and say plainly in the handoff which file
is which.

### 6.3 Fonts

Write the DXF style with the CAD-portable font *name* (`simfang.ttf`,
`txt.shx`) rather than an absolute path, so the receiving machine resolves it.
Keep the absolute path only for local glyph outlining.

## 7. Text that survives the DWG conversion

Produce both, and say which is which in the handoff:

- **Editable master** — DXF at R2018 (UTF-8), Chinese as real `TEXT`/`MTEXT`.
- **Portable copy** — Chinese glyphs converted to closed polylines, so the file
  displays correctly on a machine without the font, at the cost of editability.

R2000 DXF is single-byte, so `ezdxf` escapes CJK as `\U+XXXX`. AutoCAD reads that
correctly; some viewers do not. Verify by reading the DWG back and comparing the
text values rather than trusting the writer.

## 8. Performance

`HLRBRep_Algo` cost grows sharply with face count. Group parts whose *projected*
bounding boxes do not overlap and run HLR per group: parts that cannot occlude
each other cannot affect each other's result, so the output is identical and the
run is far faster. This matters on exploded views, where most parts are disjoint
on the sheet.

## 9. Verify before delivery

- Reconstruct the assembly bounding box and compare it with the known product
  size. A pipeline bug usually shows up here first.
- Check gear or mating relationships numerically (axis directions, centre
  distances) instead of judging them by eye, and state the measured values.
- Render a preview **from the DXF**, not from the intermediate plot, so the check
  covers what was actually written.
- Run `scripts/validate_clean_dxf.py`. Expect zero non-continuous entities and
  layers. Part names and reference numerals will be reported as suspect text —
  that is the validator offering candidates, not an error, and they should be
  kept on a generation job.
