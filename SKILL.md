---
name: patent-figure-kit
description: Generate patent figures from a 3D CAD assembly (STEP) by writing one figure-plan.json that scripts render deterministically into assembly and exploded DXF sheets with leaders and reference numerals, and clean or convert existing patent line drawings (DWG, DXF, Matplotlib, PDF, raster) into continuous-line DXF plus an AutoCAD-audited DWG with reference numerals removed and functional labels preserved.
---

# Patent Drawing DWG Cleanup

Two routes. Pick one by input type and do not mix them.

| Input | Route | Where the geometry comes from |
| --- | --- | --- |
| 3D CAD assembly — `.stp` / `.step` | **Route A**, below | computed by `scripts/patent_figure/*` from a plan you write |
| Existing DXF | Route B | the source file, cleaned in place on a copy |
| Existing DWG | Route B (convert to DXF first) | the source file |
| Matplotlib or other vector | Route B | the source artists / paths |
| PDF, PNG, JPG | Route B | extracted vector paths, or a traced reconstruction |

`scripts/cad_hlr_to_dxf.py` remains the **legacy single-view** STEP route: one view, no per-part
leaders, no numerals, no multi-figure split. Do not use it to produce a filing figure. Everything
below in Route A supersedes it.

---

## Route A — 3D CAD assembly to patent figures

**Your only output artefact is `figure-plan.json`.** No coordinate, text height, layout constant or
numeral is ever written by you: the scripts compute all of them. The full specification is
[docs/impl-contract.md](docs/impl-contract.md); rationale is
[docs/iteration-plan-v2.md](docs/iteration-plan-v2.md). Read the contract before deviating from
anything on this page — and then do not deviate, report instead (see *Prohibitions*).

### Verifying an install

A new machine or runtime should prove the toolchain works before doing real work:

```bash
python3 acceptance/run_acceptance.py
```

Fourteen nodes, about forty seconds, exit 0 only if every one passes. Two of them earn
their keep: one deliberately breaks a good drawing to prove the QA gate can fail at all,
and one compares this machine's rendering against a digest recorded in the repository —
which is what makes "any runtime renders the same figure" a checked claim rather than a
promise. See [acceptance/README.md](acceptance/README.md); the behavioural half, which no
script can judge, is in [acceptance/行为验收.md](acceptance/行为验收.md).

### Two authors, one plan

A plan can be written by a model (below) or clicked together by a person in **Plan Studio**
(`python3 scripts/plan_studio.py ASM.stp` — local browser UI: pick parts in 3D, group them
into figures, name them, render behind the same QA gate). Both authors produce the same
`figure-plan.json` and go through identical CLIs, so a hand-made plan reproduces
bit-identically like any other. When a human wants to adjust figure grouping, point them at
Plan Studio instead of editing JSON by hand — and never have a model drive the Studio.

### The loop

```
doctor → analyze → write plan → validate → render (QA gate inside) → convert
                        ↑                        │
                        └──── on failure: edit the plan only ────┘
```

Run the commands exactly as written. Every CLI prints its contract under `--help`, writes
machine-readable output with `--json`, and exits `0` pass / `1` failure / `2` usage error.

**A0 — environment probe (first run in a session, always).**

```bash
python3 scripts/doctor.py --json
```

Exit `1` means a required capability is missing. Report what is missing and stop. Never work around
a missing dependency by hand.

**A1 — analyze the assembly.** This is the only place part names and geometry enter the workflow.

```bash
python3 scripts/analyze_assembly.py ASM.stp -o assembly.json --include 'SYN-*' --exclude '*SCREW*'
```

`--include` / `--exclude` are repeatable and **case-sensitive** (`fnmatchcase`), as is every glob in
the system. Read `assembly.json` before writing the plan: `parts[]`, `stack_order`,
`coaxial_groups`, `size_tiers`, `split_suggestions`, `warnings`. `split_suggestions` may legitimately
be `[]` — then the grouping is yours to choose.

**A2 — write `figure-plan.json`.** See the next section. This is the only file you author.

**A3 — validate the plan.** Never skip it; it is cheaper than a failed render.

```bash
python3 scripts/validate_figure_plan.py plan.json --assembly assembly.json --json issues.json
```

**A4 — render.** The QA gate runs inside, and publishing is **atomic after the gate**: each
figure is rendered into a scratch dir and only *published* to the output directory once it
passes QA with no dropped HLR edges. A figure that fails is moved to `out/diag/` instead, so
the output directory never contains a sheet that did not clear the gate (the "过闸才落盘"
guarantee, impl-contract §8).

```bash
python3 scripts/render_patent_figure.py plan.json --assembly assembly.json -o out/ \
        --cache .cache/ --preview
```

Writes, **for every figure that passed**, `out/<figure id>.dxf`, `out/<figure id>.png`
(with `--preview`), `out/<figure id>.layout.json`, `out/<figure id>.qa.json`, and always
`out/reference-numerals.json` (the global numeral table plus the ready-to-paste
「附图标记说明：1—底座；2—…」 sentence — a plan-level product, published regardless). A figure
that fails QA or whose HLR dropped an edge lands under `out/diag/` with the same artefacts for
inspection. Exits `1` if any figure failed, printing the failing checks with their hints.
`--only fig2` re-renders a single figure while iterating.

`--thresholds overrides.json` tightens or loosens the QA gate (a JSON object of
`qa.DEFAULT_THRESHOLDS` keys → values); omit it for the contract-frozen thresholds. Use it for
a stricter review pass, e.g. `{"text_height_mm_min": 4.0}`.

**A5 — re-check one DXF (optional, same checks as the gate in A4).**

```bash
python3 scripts/qa_patent_figure.py out/fig1.dxf --kind exploded --json qa.json
```

**A6 — convert to DWG** with the frozen converters (identical to Route B; see *Create the DWG*).

```bash
python3 scripts/autocad_core_dxf_to_dwg.py out/fig1.dxf out/fig1.dwg
```

**A7 — 规范标注（图号 + 件号名称表）**, on copies only. Chinese filing drafts number figures
「图1、图2…」and, in the common style used for disclosures (交底书),
carry a 「序号｜名 称」table in a free corner of each sheet. The renderer's sheet stays the
canonical output; this CLI derives two copies from it and never touches the source DXF:

```bash
python3 scripts/annotate_figure_sheet.py out/fig1.dxf --numerals out/reference-numerals.json \
        --figure-number 1 -o out/fig1_annotated.dxf --preview          # 交底版：图号 + 件号表
python3 scripts/annotate_figure_sheet.py out/fig1.dxf --numerals out/reference-numerals.json \
        --figure-number 1 -o out/fig1_filing.dxf --no-table --preview  # 递交版：仅图号
```

Table rows are read from the NUM layer of that sheet and named from `reference-numerals.json` —
the CLI issues no numeral and invents no name. The descriptive caption moves into the report as
the 附图说明 sentence (「图1为……；」). The figure number is the figure's position in
`plan.figures` (1-based); do not pass anything else.

### Method flowcharts (方法流程图)

Same split of authorship: you write **only** `flowchart.json` (schema `patent-flowchart/1`:
`title`, optional `step_style` `S101`|`S1`, `nodes[{id,kind,text}]`, `edges[{from,to,label}]`,
`kind` ∈ start / end / process / decision / io). Step numbers, box sizes, coordinates, routing and
the figure number are computed; a node text containing a step number is rejected
(`E_STEP_NUMBER_IN_TEXT`), and every decision needs exactly two labelled out-edges (是 / 否).

```bash
python3 scripts/render_flowchart.py flow.json --check                       # 只校验
python3 scripts/render_flowchart.py flow.json -o out/flow1.dxf --figure-number 6 --preview --json res.json
```

Number flowcharts after the structural figures. `res.json` carries the S-number ↔ step table for
the description.

### Workbench (human UI, multi-project, DeepSeek, REST, MCP)

`python3 scripts/workbench.py` starts a local web workbench (default `http://127.0.0.1:8790/?token=…`):
home page with an example carousel and project list; one Plan Studio per project with click and
**box selection** (left→right window, right→left crossing, Shift add, Alt subtract), AI drafting
of part names, render with the QA gate, automatic 交底版/递交版, method flowcharts, and an export
bundle (DXF/DWG/PNG, 说明书附图 Word, 附图说明). External callers use REST `/v1/*` (docs at `/docs`)
or MCP `POST /mcp` with `Authorization: Bearer <token>`. The large-model channel (DeepSeek API,
or a local CodeBuddy CLI as fallback) only drafts **semantics** — part names and flowchart steps —
and everything it returns goes through the same validators; human-entered names are never
overwritten. As with Plan Studio, **a model must never drive the workbench UI**; it talks to the
REST/MCP tools. Project data lives in `~/.patent-workbench` and secrets in
`~/.config/patent-workbench/config.json` (0600) — neither belongs in a repository.

### The only file you write: `figure-plan.json`

A complete minimal plan, valid against
[schemas/figure-plan.schema.json](schemas/figure-plan.schema.json) and runnable against the
synthetic fixture:

```json
{
  "schema": "patent-figure-plan/1",
  "source": {
    "step": "tests/fixtures/synthetic.stp",
    "include": ["SYN-*"],
    "exclude": []
  },
  "terms": [
    {"selector": "SYN-A01",  "term": "底座"},
    {"selector": "SYN-B02",  "term": "回转座"},
    {"selector": "SYN-C03",  "term": "支撑轴"},
    {"selector": "SYN-D04",  "term": "密封圈"},
    {"selector": "SYN-E05",  "term": "调整垫片", "label": "once"},
    {"selector": "SYN-F06",  "term": "球头", "label": "all"},
    {"selector": "SYN-G07",  "term": "上盖"},
    {"selector": "SYN-H08*", "term": "紧固螺钉", "label": "none"}
  ],
  "figures": [
    {"id": "fig1", "caption": "整体结构示意图", "kind": "assembly", "members": ["*"]},
    {"id": "fig2", "caption": "回转组件分解示意图", "kind": "exploded",
     "members": ["SYN-A01", "SYN-B02", "SYN-C03", "SYN-G07"],
     "layout": {"explode_axis": "z"}}
  ],
  "layout": {
    "view": "iso",
    "explode_axis": "auto",
    "axis_angle": "auto",
    "density": "normal",
    "max_labels_per_figure": 20,
    "engineering_table": false
  }
}
```

Rules the validator enforces — read them as the contract, not as advice:

- **`terms` carries Chinese technical nouns only, never numerals and never a part code.** The order
  of `terms` *is* the numeral issue order: `terms[0]` becomes numeral 1. The script issues the
  numbers; a numeral appearing anywhere in the plan is a defect, not a shortcut.
- `terms[].selector` and `figures[].members` are case-sensitive globs on the part name and must each
  match at least one part in `assembly.json` (`E_SELECTOR_NO_MATCH` / `E_MEMBER_NO_MATCH`).
- `terms[].label` is `once` (default — exactly one instance carries the numeral), `all` (every
  instance), or `none` (standard parts: no leader at all). This is the escape valve for repeated
  hardware; use it before you consider splitting a figure.
- `figures[].kind` is `assembly` or `exploded`. `figures[].id` is also the output file stem.
- **`layout.axis_angle` stays `"auto"`.** The renderer solves the sheet angle per figure in closed
  form; the optimal angle depends on that figure's length-to-width ratio, so it is not a constant.
  Pin a number only when a reviewer asks for a specific look — and never by copying back a value the
  renderer just printed at you. Same for `explode_axis`: `"auto"` unless the assembly axis is known.
- `layout` accepts only these keys and only these enums (`additionalProperties: false` everywhere);
  a figure may override any key in its own `layout` block.
- Terms that look like internal part codes are rejected (`E_TERM_LOOKS_LIKE_PART_CODE`). Internal
  part numbers must never reach a filing figure — that is a confidentiality rule, not a style rule.

### Prohibitions (hard constraints)

These exist because the v1 failure was a 170-line model-authored render script whose invented layout
constants produced 97% overlapping numerals, 13.5% page occupancy and a table leaking internal part
codes. They are not negotiable.

1. **Do not write any throwaway generation script** (`gen_*.py`, a `python3 -c` one-liner that draws,
   a notebook cell). If a figure needs geometry the CLIs do not produce, that is a capability gap.
2. **Do not import package internals.** No `sys.path.insert` and no
   `from patent_figure... import ...` from your side. The scripts are reachable **only** through
   their CLIs.
3. **Do not put layout constants in the plan** — no coordinates, gaps, text heights, margins, colours,
   scales, angles beyond the enumerated `layout` keys.
4. **Do not hand-fill reference numerals**, in the plan or in the DXF, or renumber the output.
5. **Do not edit the scripts, the schemas, or the thresholds** to make a figure pass.
6. **A filing figure carries geometry, leaders, numerals and the caption — nothing else.** No parts
   table, no part names, no internal codes, no dimensions, no title block (contract §8). The
   `NO./NAME/QTY/REMARK` table exists only behind `layout.engineering_table: true`, is off by
   default, and its output is written as `<id>_engineering.dxf`, which is a review copy and never a
   filing copy. *(The "Part table" row in
   [references/cad-source-to-drawing.md](references/cad-source-to-drawing.md) §6 is a mechanical
   assembly-drawing convention; contract §8 overrides it for patent figures.)*
7. **When the CLI cannot express the requirement, stop and report the gap.** Say what is missing and
   which command would need to carry it. A human decides whether to extend the CLI.

### When validate or QA fails

The only permitted repair is **editing `figure-plan.json` and re-running the loop from A3.** Every
error and every failed check carries a `hint` naming the plan edit that fixes it — apply that edit,
not an invention of your own. Typical repairs:

| Failure | Plan edit |
| --- | --- |
| `E_TOO_MANY_LABELS`, or labels that cannot be placed | set `label: "none"` on standard parts, or split the figure per `assembly.json:split_suggestions` |
| page fill too low, rotation would help | restore `layout.axis_angle` to `"auto"` so the renderer re-solves it (this branch only fires when the plan pinned an angle), or `density: "compact"` |
| page fill too low, rotation would not help | change `layout.view`, or split the figure |
| text height below the floor / part outline too small | `label: "none"` on standard parts, split the figure, or `density: "compact"` — note the message may say the **part count**, not the label count, is what overflowed |
| a degenerate part, or a part with no visible curves | add it to `source.exclude` |
| forbidden text on the sheet | fix the offending `terms[].term` |

**Three consecutive failed repairs on the same figure: stop.** Report the failing checks, the edits
already tried, and the capability gap. Do not widen the scope of the fix, and do not fall back to
drawing anything by hand.

### Route A delivery checklist

1. `doctor.py` exits 0, or the missing capability is stated in the handoff.
2. `validate_figure_plan.py` exits 0 on the final plan.
3. `render_patent_figure.py` exits 0 — every figure passed the QA gate.
4. The plan contains no numerals, no coordinates and no layout constants beyond the enumerated keys.
5. No sheet carries a parts table, an internal part code or a dimension; `_engineering.dxf` files,
   if any, are labelled as review copies in the handoff.
6. `out/reference-numerals.json` accompanies the figures, and the 附图标记说明 sentence is quoted in
   the handoff for the specification text.
7. Preview PNGs were rendered from the DXF and inspected.
8. When the deliverable is a filing or disclosure set, `annotate_figure_sheet.py` produced the
   「图N」copies (with or without the 件号名称表, as asked) and the 附图说明 sentences are quoted.

---

## Route B — clean or convert an existing drawing

Produce a clean, editable CAD deliverable while preserving the source files. The expected result is
continuous-line geometry, no patent reference numerals requested for removal, retained functional
labels, and an AutoCAD-audited DWG plus an editable DXF.

- Existing DXF: inspect the text candidates, clean a copy with `scripts/clean_patent_dxf.py`, then validate it with `scripts/validate_clean_dxf.py`.
- Existing DWG: open or convert a copy to DXF with AutoCAD first. Do not treat a compressed DWG as a text-editable file or rewrite it with `ezdxf`.
- Matplotlib or other vector source: preserve the source geometry and export its artists or paths to DXF. Read [references/workflow.md](references/workflow.md) for the artist-to-CAD mapping.
- PDF, PNG, or JPG only: confirm whether the figure is vector or raster. Extract vector paths when present; otherwise trace or reconstruct the geometry. Do not claim that raster-to-vector output is dimensionally exact unless dimensions or controlled CAD are available.

### Separate reference numerals from functional text

First inventory every text object containing numbers. Treat standalone multi-digit labels such as `100`, `310`, or `3411` as candidates, not automatic proof that they are patent references. Preserve functional identifiers and engineering content such as `A1`, `A2`, `24V`, pin names, dimensions, tolerances, and terminal labels unless the user explicitly says to remove them.

Use an explicit accepted list whenever the drawing contains dimensions or mixed technical notation:

```bash
python3 scripts/clean_patent_dxf.py input.dxf output.dxf \
  --reference-number 100 \
  --reference-number 310 \
  --reference-number 3411 \
  --strip-selected-inline-references \
  --remove-figure-labels \
  --report cleanup-report.json
```

For a drawing known to use every standalone multi-digit text object as an attached reference label, `--remove-standalone-candidates` is available. Run `--dry-run` first and review the report. Do not use broad numeric deletion on dimensioned drawings.

### Make linework continuous

Set graphical entities to `CONTINUOUS` and set their layers to `CONTINUOUS`; checking only the visible plot is insufficient. Preserve geometry, arrows, junctions, and functional symbols. Convert former dashed boxes, centerlines, and chain lines to solid geometry only when the user asks for all-solid figures.

Generate a preview image after cleaning and compare it with the source. The preview must show that no outline, arrowhead, junction, or retained label was lost.

### Create the DWG

Prefer Autodesk's own core engine over third-party DWG converters when AutoCAD is installed. The bundled converter writes to an ASCII temporary path so Chinese output paths do not get corrupted by the command-script prompt:

```bash
python3 scripts/autocad_core_dxf_to_dwg.py cleaned.dxf cleaned.dwg
```

The converter runs `AUDIT`, saves as AutoCAD 2018 DWG, re-audits, and reports entity and non-continuous counts. Pass `--autocad-core` if AutoCAD is installed in a nonstandard location.

When AutoCAD is not installed, fall back to LibreDWG:

```bash
python3 scripts/libredwg_dxf_to_dwg.py cleaned.dxf -o dwg/ --deep-audit --report dwg-report.json
```

It writes R2000 and uses `dwgread` in place of the AUDIT round trip: a file that re-parses cleanly
with a non-zero entity count and intact text passes. Say in the handoff which engine produced the DWG.
LibreDWG does not scale to very large combined sheets; convert per figure and deliver an oversized
combined sheet as DXF rather than reporting a DWG that never completed.

If retained Chinese text is substituted or garbled, keep the editable-text DXF and create a second DWG in which the text is converted to vector outlines before conversion. Label that tradeoff clearly: outlined text is portable but no longer editable as text.

### Route B delivery checklist

```bash
python3 scripts/validate_clean_dxf.py cleaned.dxf
python3 scripts/autocad_core_dxf_to_dwg.py cleaned.dxf cleaned.dwg
```

Deliver only after these checks pass:

1. Source files remain unchanged and outputs are in a new folder or new filenames.
2. The DXF contains no rejected reference labels and no non-continuous entity or layer linetypes.
3. The DWG opens in AutoCAD, the final `AUDIT` reports zero errors, and the entity count is nonzero.
4. A visual preview matches the intended source geometry.
5. The handoff states which labels were preserved and whether DWG text is editable text or vector outlines.

Read [references/workflow.md](references/workflow.md) when rebuilding from Matplotlib/raster sources, combining multiple figures, or handling Chinese-font portability.
