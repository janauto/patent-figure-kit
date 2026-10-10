# v2 implementation contract

**Read this file completely before writing any code.** It is the single source of truth for module
boundaries, function signatures, JSON shapes, and determinism rules. Where this file and any other
document disagree, this file wins. If something you need is genuinely unspecified here, pick the
option that is most deterministic and record it in your report's `open_issues` — do not invent a
parallel convention.

Rationale for the whole design is in [iteration-plan-v2.md](iteration-plan-v2.md).

---

## 0. Prime directives

1. **The LLM's only output is `figure-plan.json`.** No layout constants, coordinates, text heights,
   or numerals ever come from a model. Everything geometric is computed by these modules.
2. **Determinism is a correctness property, not a nicety.** Same inputs → byte-identical output
   after normalization. Every algorithm here must be reproducible across machines and runs.
3. **One OCC boundary.** `patent_figure/occ_backend.py` is the *only* module that may
   `import OCP.*`. Every other module operates on plain numpy arrays and dicts, so it is unit
   testable without CAD libraries installed.

## 1. Environment (already verified on the dev machine)

| Package | Version | Note |
| --- | --- | --- |
| `cadquery-ocp` | 7.7.2 | OCCT 7.7.2 bindings, namespace `OCP.*`. **pip-installable.** |
| `ezdxf` | 1.4.2 | |
| `numpy` | 2.0.2 | |
| `jsonschema` | 4.25.1 | plan/assembly validation |
| `matplotlib` | 3.9.4 | preview PNG rendering only |
| `pytest` | 8.4.2 | |

Do **not** import `OCC.Core.*` (that is the conda-only `pythonocc-core`). This project uses `OCP.*`.

Also verified on the dev machine, so do not spend agent time rediscovering it:

- `ezdxf.addons.drawing` with the `matplotlib` backend imports cleanly — use it for `render_preview`.
- `simfang.ttf` is **not installed** here (only `Songti.ttc`). `doctor.py` must report this as a
  real finding, not a hard failure: the DXF still records the style name `simfang.ttf`, and the
  consequence is that Chinese text renders with a fallback locally and may box out on a machine
  without the font. The portable-DWG outlined-text route in `references/workflow.md` §4 exists for
  exactly this case.
- AutoCAD 2026 **is** installed at `/Applications/Autodesk/AutoCAD 2026`, and LibreDWG `dwgread`
  **is** on `PATH` at `/opt/homebrew/bin/dwgread`. Both conversion routes are testable here.
Python is 3.9. Verified on this machine: `list[int]` / `dict[str, int]` **do** work at runtime
(PEP 585 landed in 3.9), but `int | str` raises `TypeError`, and `match` does not parse. Add
`from __future__ import annotations` to every module anyway — it makes all annotations lazy, so the
union syntax used throughout this contract's signatures is legal.

## 2. Frozen files — do not modify

`scripts/cad_hlr_to_dxf.py`, `scripts/clean_patent_dxf.py`, `scripts/validate_clean_dxf.py`,
`scripts/autocad_core_dxf_to_dwg.py`, `scripts/libredwg_dxf_to_dwg.py`.

The new package **copies** the OCC logic it needs into `occ_backend.py` (adapted, not imported).
Legacy refactoring is a separate pass. `cad_hlr_to_dxf.py` stays as the single-view legacy route.

## 3. File map and ownership

```
scripts/patent_figure/__init__.py        version string only
scripts/patent_figure/occ_backend.py     STEP load, view frame, HLR, geometry cache   [OCC]
scripts/patent_figure/analyze.py         assembly.json construction                   [OCC via backend]
scripts/patent_figure/plan.py            plan load, schema + semantic validation      [pure]
scripts/patent_figure/numbering.py       deterministic numeral assignment             [pure]
scripts/patent_figure/layout.py          2D sheet placement of parts                  [pure]
scripts/patent_figure/labels.py          2D leader + numeral placement                [pure]
scripts/patent_figure/sheet.py           DXF emission, preview, hash normalization    [ezdxf]
scripts/patent_figure/qa.py              readability + compliance checks on a DXF     [ezdxf]

scripts/analyze_assembly.py              CLI  step        -> assembly.json
scripts/validate_figure_plan.py          CLI  plan+asm    -> exit 0/1 + errors JSON
scripts/render_patent_figure.py          CLI  plan        -> figures + numerals + previews
scripts/qa_patent_figure.py              CLI  dxf         -> qa.json, exit 0/1
scripts/doctor.py                        CLI  ()          -> capability report
scripts/make_test_assembly.py            CLI  ()          -> synthetic STEP fixture

schemas/assembly.schema.json
schemas/figure-plan.schema.json
tests/test_layout.py  tests/test_labels.py  tests/test_numbering.py
tests/test_plan.py    tests/test_qa.py      tests/test_golden.py
tests/fixtures/                            synthetic only — see §10
requirements-pinned.txt
```

## 4. Data shapes

### 4.1 `assembly.json`

```jsonc
{
  "schema": "patent-assembly/1",
  "source": {"step": "<path>", "sha256": "<hex>", "instances": 164, "distinct": 125},
  "units": "mm",
  "bbox": {"lo": [x,y,z], "hi": [x,y,z], "size": [w,d,h]},
  "parts": [{
    "name": "PRT0001",
    "instances": 4,
    "bbox_size": [w,d,h],           // of one instance, rounded to 3 decimals
    "max_dim": 12.5,
    "centers": [[x,y,z], ...],      // one per instance, sorted lexicographically
    "degenerate": false,            // true when max_dim < 1e-6
    "path_sample": "root/sub/PRT0001"
  }],
  "principal_axis": {"vector": [0,0,1], "nearest": "z", "spread_ratio": 4.21},
  "coaxial_groups": [{
    "id": "g1", "axis": [0,0,1], "origin": [x,y,0],
    "members": ["PRT0001", "PRT0002"], "instances": 12,
    "extent": [z_lo, z_hi]
  }],
  "stack_order": ["PRT0007", "PRT0001", ...],   // by projection of center onto principal axis
  "size_tiers": [{"tier": "large", "members": [...]}, {"tier":"medium",...}, {"tier":"small",...}],
  "split_suggestions": [{
    "id": "coaxial", "strategy": "coaxial",
    "figures": [{"caption_hint": "组件 A 分解", "members": ["PRT0001","PRT0002"], "labels": 14}]
  }],
  "warnings": ["3 part(s) have degenerate bounding boxes: PRT0004, PRT0005, ..."]
}
```

When the assembly exceeds the label cap, all three strategies are **attempted** — `coaxial` (by
coaxial group), `stack` (by breaks in `stack_order` spacing), `size` (by `size_tiers`) — and a
strategy is emitted only if every figure it proposes satisfies `labels <= max_labels_per_figure`.
A strategy that cannot reach the cap is reported in `warnings` with the count it got to, never
emitted as a suggestion. If **no** strategy succeeds, `split_suggestions` is `[]` and `warnings`
says so; that is a legitimate outcome, not a bug — the human then has to choose the grouping.

(The earlier wording said three suggestions are "always emitted" *and* that each must satisfy the
cap. Those cannot both hold. Flagged by the cross-model contract audit; see §12.)

### 4.2 `figure-plan.json`

```jsonc
{
  "schema": "patent-figure-plan/1",
  "source": {"step": "<path>", "include": ["glob"], "exclude": ["glob"]},
  "terms": [
    {"selector": "PRT0001", "term": "底座", "label": "once"},
    {"selector": "SCREW*",  "term": "紧固螺钉", "label": "none"}
  ],
  "figures": [
    {"id": "fig1", "caption": "整体结构示意图", "kind": "assembly", "members": ["*"]},
    {"id": "fig2", "caption": "回转组件分解示意图", "kind": "exploded",
     "members": ["PRT0001", "PRT0002"], "layout": {"explode_axis": "z"}}
  ],
  "layout": {                       // defaults; a figure may override any key
    "view": "iso",
    "explode_axis": "auto",
    "axis_angle": "auto",
    "density": "normal",
    "max_labels_per_figure": 20,
    "engineering_table": false
  }
}
```

Rules enforced by `plan.py`:

- `terms[].selector` is an fnmatch glob; **every selector must match ≥1 part** in `assembly.json`.
- `terms[].term` must not match any pattern in `forbidden_term_patterns` (default: internal part-code
  shapes, see §7). Terms are Chinese technical nouns.
- `terms[].label` ∈ `once | all | none`. Default `once`.
  `once` labels **exactly one instance**: the one whose `key` sorts first among that part's
  instances in the figure (`key` is `name#instance_index`, and `instance_index` is assigned after
  the global sort in §5.1, so this is stable). `all` labels every instance; `none` labels none and
  the part still gets a numeral in the global table only if some other figure labels it.
- `terms` order is the numeral issue order. **The plan never contains numerals.**
- `figures[].kind` ∈ `assembly | exploded`.
- `figures[].members` globs resolve against selected parts; a member matching no part is an error.
- Effective label count per figure ≤ `max_labels_per_figure`, else `E_TOO_MANY_LABELS`.
- `layout.view` ∈ the `VIEWS` enum; `explode_axis` ∈ `x|y|z|auto`; `density` ∈ `compact|normal|loose`.
- `layout.axis_angle` is `"auto"` (the default) or a number clamped to `[120, 180]` with `W_CLAMPED`.

  **`"auto"` means the renderer solves the sheet angle itself**, per figure, using the closed form
  in §11.2 (`axis_angle_opt`, the angle at which the geometry aspect ratio matches the usable-area
  aspect ratio and `sheet_fill` is maximised). This is an **architect ruling that supersedes
  §11.10 #7**, which set a fixed default of 124.

  The reason is prime directive #1. The optimal angle depends on the string length to width ratio,
  so it is a property of the figure, not a constant: 124 is optimal for the 20-part stack §11.1.2
  measured, and roughly 111 for the eight-part synthetic fixture — both computed from the same
  closed form. Shipping any fixed number means most assemblies fail the `sheet_fill` gate on the
  first render, and the repair the model is asked to make is to copy a number the script already
  computed into the plan. That is a layout constant travelling through the model, which is exactly
  what this contract exists to prevent. A model may still pin an explicit angle when it wants a
  particular look; it just is not asked to supply one to get a correct drawing.
- **No other keys are accepted.** `additionalProperties: false` everywhere.

### 4.3 `reference-numerals.json`

```jsonc
{"schema": "patent-numerals/1",
 "numerals": [{"numeral": 1, "term": "底座", "selector": "PRT0001", "figures": ["fig1","fig2"]}],
 "description_zh": "附图标记说明：1—底座；2—回转轴组件；……"}
```

### 4.4 `qa.json`

```jsonc
{"schema": "patent-figure-qa/1", "file": "fig1.dxf", "pass": false,
 "checks": [{"id": "geometry_occupancy", "pass": false, "value": 0.135,
             "threshold": ">=0.55", "detail": "...", "hint": "..."}],
 "summary": {"failed": 5, "passed": 4}}
```

## 5. Module signatures

Every module starts with `from __future__ import annotations`.

### 5.1 `occ_backend.py` — the only OCC module

```python
VIEWS: dict[str, tuple[float, float]]        # name -> (az, el); same 7 entries as the legacy script

class ViewFrame:
    def __init__(self, az: float, el: float, roll: float = 0.0) -> None
    x: np.ndarray; u: np.ndarray; w: np.ndarray      # right, up, view direction (unit, right-handed)
    def to2d(self, pts: np.ndarray) -> np.ndarray    # (...,3) -> (...,2)
    @property
    def key(self) -> str
        """Exactly f"{az:.4f}_{el:.4f}_{roll:.4f}" — four decimals, no rounding of the stored
        floats themselves. Four is enough that a 1e-4 degree difference busts the cache and not
        enough for float noise to do so."""

def roll_for_axis(axis, az: float, el: float, target_deg: float) -> float
    """Solve in closed form, do NOT search. With e = the axis projected into the unroll ed view
    plane, the sheet angle is atan2(e_u, e_x); the roll that moves it to target is

        roll = target_deg - degrees(atan2(e_u, e_x))

    normalised into [0, 360). The legacy implementation stepped through candidate angles and
    compared raw floats, which violates §7 rule 4; the closed form removes both problems.
    Raise ValueError when |e| < 1e-9 (axis parallel to the view direction)."""

class PartShape:
    key: str          # f"{name}#{instance_index}" — the stable identity used everywhere downstream
    name: str
    path: str
    instance_index: int
    lo: np.ndarray; hi: np.ndarray                   # 3D bbox
    shape: Any                                       # TopoDS_Shape
    @property
    def center(self) -> np.ndarray
    @property
    def degenerate(self) -> bool                     # max extent < 1e-6

def load_assembly(step: Path) -> list[PartShape]
    """Deterministic order. The sort key is exactly:

        (name, path, round(cx, 6), round(cy, 6), round(cz, 6))

    where c is the instance centre in model coordinates and 6 is the decimal count — fixed here so
    two implementers cannot pick different precisions. Ties beyond that key keep the order OCCT
    produced, which is NOT guaranteed stable across OCCT builds; if two instances tie on the full
    key they are geometrically identical at micron resolution, so which one takes which
    instance_index cannot change any output. instance_index is assigned AFTER this sort, so the
    same STEP always yields the same keys."""

def part_curves(part: PartShape, view: ViewFrame, deflection: float) -> list[np.ndarray]
    """Per-part HLR. VALID ONLY for parts that are disjoint on the sheet."""

def scene_curves(parts: Sequence[PartShape], view: ViewFrame, deflection: float,
                 want_hidden: bool = False) -> tuple[list[np.ndarray], list[np.ndarray]]
    """Global HLR over all parts together. REQUIRED for kind='assembly' figures, where parts
    occlude each other. Returns (visible, hidden) as lists of (N,2) float arrays."""

class GeometryCache:
    def __init__(self, root: Path, step_sha: str) -> None
    def get(self, part_key: str, view: ViewFrame, deflection: float) -> list[np.ndarray] | None
    def put(self, part_key: str, view: ViewFrame, deflection: float,
            curves: list[np.ndarray]) -> None
```

**HLR compounds to collect** (copy the legacy behaviour): `VCompound`, `Rg1LineVCompound`,
`OutLineVCompound` for visible; `HCompound`, `Rg1LineHCompound`, `OutLineHCompound` for hidden.
Omitting `OutLine*` breaks dome and fillet silhouettes.

**Cache**: `<root>/<step_sha[:12]>/<view.key>_<deflection>/<sha1(part_key)>.npz`, storing curves as
`arr_0..arr_n`. A cache hit must be bit-identical to a recompute. Load 24 s on the reference
assembly makes this mandatory, not optional.

### 5.2 `layout.py` — pure 2D, no OCC, no ezdxf

```python
# Gap between adjacent parts on the sheet, as a fraction of the MEDIAN part extent along the
# explode direction — not of the total string length. Keying it to the median is what stops the
# v1 failure where a longer string shrank every part's slot while the gap grew.
DENSITY: dict[str, float] = {"compact": 0.35, "normal": 0.55, "loose": 0.85}

@dataclass
class Piece:
    key: str
    name: str
    curves: list[np.ndarray]      # 2D, unplaced (view-projected, model position)
    offset: np.ndarray = field(default_factory=lambda: np.zeros(2))
    @property
    def lo(self) -> np.ndarray    # placed bbox min  (curves min + offset)
    @property
    def hi(self) -> np.ndarray
    def placed(self) -> list[np.ndarray]

@dataclass
class LayoutResult:
    pieces: list[Piece]
    lo: np.ndarray; hi: np.ndarray     # overall geometry bbox after placement
    slot: float                        # min over pieces of max(hi-lo) — drives text height
    rows: int
    diagnostics: dict                  # {"strategy": ..., "gap": ..., "overlaps": 0}

def layout_assembly(pieces: list[Piece]) -> LayoutResult
    """No displacement. Parts keep their projected positions."""

def layout_exploded(pieces: list[Piece], axis2d: np.ndarray, density: str,
                    sheet_aspect: float = 0.75, max_rows: int = 1) -> LayoutResult
    """<<ALGORITHM SPECIFIED IN §11 — implement exactly what is written there.>>

    `max_rows` accepts ONLY 1. Any other value raises LayoutError. §11.1 rules out two-dimensional
    rearrangement: displacing parts off the assembly axis misrepresents the assembly relationship,
    which is the one thing an exploded patent figure exists to convey. It must not silently degrade
    to a single row either — then a caller that passes the argument and one that forgets it would
    render two different figures and both would pass QA."""

FRAME_W, FRAME_H = 170.0, 250.0   # millimetres. 170 = 210 - 25 - 15 (CNIPA margins, 审查指南
                                  # 五部一章 4.3: top 25 / left 25 / right 15 / bottom 15 on A4).
                                  # 180 was unplaceable: 25 + 180 + 15 = 220 > 210.

def fit_to_frame(result: LayoutResult, frame_w: float = FRAME_W, frame_h: float = FRAME_H,
                 margin: float = 0.0) -> float
    """Return the largest uniform scale factor that still fits:

        usable_w = frame_w * (1 - 2*margin);  usable_h = frame_h * (1 - 2*margin)
        s        = min(usable_w / bbox_w, usable_h / bbox_h)

    There is deliberately NO area-target term. An earlier draft capped s at a TARGET_OCCUPANCY of
    the usable area; §11.1 D1 shows why that was wrong — `geometry_occupancy` is scale-invariant,
    so shrinking the drawing cannot improve it, and the cap only pushed text height down 21% until
    it fell through the 3.5 mm floor. Page filling is measured by the separate `sheet_fill` gate
    (§11.1 D2), which is governed by aspect ratio, not by scale.

    `margin` defaults to 0.0 because FRAME_W/FRAME_H is already the usable area; the label margin
    and caption band are subtracted explicitly by §11.6. Insetting again here double-counts and
    the geometry can never reach the paper edge. Never mutates the pieces."""
```

Hard postcondition of `layout_exploded`: **no two placed BODY bboxes overlap** (a body is a group
of pieces that must stay together — a bolt circle, a set of identical screws; §11.3 S8 defines it,
and members within one body may overlap each other), verified inside the function with a tolerance
of `1e-6 * SCALE_REF` on each axis — a **relative** tolerance, because `layout.py` receives model
units and the same assembly exported in metres versus millimetres would otherwise get a tolerance
that is either inert or over-merging. Touching edges are not an overlap. Raise `LayoutError` if the
invariant cannot be met.

`LayoutResult.slot` is reported on two gauges, because a `label: "none"` washer must not be allowed
to shrink every numeral on the sheet: `slot` is `min(min-over-pieces of max(hi-lo),
SLOT_QA_CAP_K * diagonal / n_labels)`, and `diagnostics["slot_labelled"]` carries the smallest
outline among **labelled** parts only. Text height is solved from `slot_labelled` (§11.6).

All four audit models asked whether that escape hatch is reachable and well defined. It is, and
this is how: the invariant is always satisfiable for `layout_exploded` on its own, because parts
are laid end to end along one direction with a positive gap, so `LayoutError` can only fire on
degenerate input — a piece with zero extent, or a non-finite coordinate. What is **not** always
satisfiable is the combination of zero overlap *and* the occupancy target *and* the label budget;
that tension is resolved by `fit_to_frame` returning a smaller scale and QA reporting the
shortfall, never by the layout silently overlapping parts.

### 5.3 `labels.py` — pure 2D

```python
@dataclass
class LabelRequest:
    key: str
    numeral: int
    lo: np.ndarray; hi: np.ndarray        # placed part bbox
    anchor_hint: np.ndarray | None = None

@dataclass
class LabelPlacement:
    numeral: int
    key: str
    text_pos: tuple[float, float]
    text_align: str                        # "left" | "right"
    leader: list[tuple[float, float]]      # [anchor_on_part, elbow, landing_end]; 3 points
    score: float

def place_labels(requests: list[LabelRequest], *, obstacles: list[np.ndarray],
                 text_height: float, sheet_lo, sheet_hi) -> list[LabelPlacement]
    """<<ALGORITHM SPECIFIED IN §11 — implement exactly what is written there.>>
    Deterministic: no randomness, stable ordering, stable tie-breaks."""

def text_height_for(slot: float, ratio: float = 0.45) -> float
    """slot * ratio. The QA gate requires height/slot <= 0.6, so 0.45 leaves margin."""
```

Hard postcondition: **zero pairwise overlap among placed numeral boxes**, and no numeral box
intersects any geometry obstacle. Unlike the layout invariant this one **is** genuinely
unsatisfiable for dense inputs, and that is by design: it is the mechanism that forces a figure to
be split rather than allowing the v1 outcome where 97% of numerals overlapped. Raise `LabelError`
carrying `unplaced: list[int]` (the numerals) and `tried: int` (candidate positions per numeral);
the caller turns it into `E_LABELS_UNPLACEABLE` whose hint names the figure id and points at
`assembly.json:split_suggestions`. `place_labels` must never fall back to placing a label anyway.

`obstacles` is every placed geometry polyline in the figure plus every leader and numeral box
already committed during this call — i.e. it grows as the greedy loop proceeds. The caller passes
only the geometry; `place_labels` accumulates the rest itself.

### 5.4 `sheet.py`

```python
LAYERS = ("GEOM", "HIDDEN", "LEADER", "NUM", "TABLE", "CAPTION", "NOTE")

def write_figure(path: Path, *, geometry: list[np.ndarray], hidden: list[np.ndarray] = (),
                 labels: list[LabelPlacement], caption: str, text_height: float,
                 caption_height: float, engineering_rows: list[tuple] | None = None,
                 dxf_version: str = "R2018") -> None
    """All entities and layers CONTINUOUS. Styles: HZ=simfang.ttf, NUM=txt.shx.
    engineering_rows is None for patent figures — the parts table is opt-in only.

    MUST set `doc.header["$INSUNITS"] = 4` explicitly. ezdxf 1.4.2 defaults a new R2018 document
    to 6 (metres), verified; leaving it would silently label every millimetre figure as metres."""

def render_preview(dxf: Path, png: Path, dpi: int = 150) -> None
    """Render FROM THE DXF, not from in-memory geometry."""

# Verified against ezdxf 1.4.2 on a fresh R2018 document:
#   $TDCREATE/$TDUPDATE  float julian day        -> volatile
#   $HANDSEED            str, e.g. '100'         -> volatile (entity-count dependent)
#   $FINGERPRINTGUID     str '{...}' per document-> volatile
#   $VERSIONGUID         str '{...}' per save    -> volatile
#   $ACADVER             str 'AC1032'            -> NOT volatile. It is the DXF version itself;
#                                                   stripping it would hide a real format change.
VOLATILE_HEADER_VARS = ("$TDCREATE", "$TDUPDATE", "$TDINDWG", "$TDUSRTIMER",
                        "$HANDSEED", "$FINGERPRINTGUID", "$VERSIONGUID",
                        "$LASTSAVEDBY", "$MENU", "$DWGCODEPAGE")

def normalized_digest(dxf: Path) -> str
    """SHA-256 over a canonical form. The recipe is fixed here in full because three of the four
    audit models independently picked different sort keys from the previous one-line description:

    1. Read with ezdxf. Ignore the header entirely except to assert `$INSUNITS == 4`.
    2. For each modelspace entity build a tuple:
         (layer, dxftype, style_or_empty, text_or_empty, tuple_of_rounded_coords)
       where every coordinate is `round(float(v), 6)` and coords are flattened in the entity's
       own vertex order (LINE: start then end; LWPOLYLINE: points in stored order; TEXT: insert
       point then height). Entity handles and owner handles are never read.
    3. Sort the tuples with Python's default tuple ordering — a total order on these values, so no
       tie-break is needed. Do NOT use np.argsort here.
    4. Join with "\n" using repr() of each tuple, encode UTF-8, SHA-256.

    Rounding at 6 decimals and comparing at 6 decimals is the same operation here, so the
    round-then-hash cannot disagree with round-then-compare (a hazard one audit model raised).
    Two runs of the same plan must produce the same digest."""
```

### 5.5 `qa.py`

```python
@dataclass
class Check:
    id: str; passed: bool; value: Any; threshold: str; detail: str; hint: str

DEFAULT_THRESHOLDS = {
    # area(GEOM u HIDDEN bbox) / area(bbox of ALL modelspace entities). SCALE-INVARIANT: it
    # measures how much of the sheet was diluted by tables and stray text, NOT whether the drawing
    # fills the paper. Reproduces the failure baseline at 0.1353. See §11.1 D1.
    "geometry_occupancy_min": 0.55,
    # area(GEOM u HIDDEN bbox) / (aw * ah), aw/ah = usable area after the label margin and caption
    # band. THIS is the gate that measures page filling. Denominator is the usable area, not the
    # whole frame: square geometry can only reach 0.487-0.513 against the full frame and its aspect
    # ratio is rotation-invariant, so the "change axis_angle" hint would send the user in circles.
    "sheet_fill_min": 0.55,
    "label_overlap_pairs_max": 0,
    # Absolute floor, GB/T 14691 smallest numeral height. This is the gate that actually catches
    # unreadable numerals. `text_slot_ratio_max` alone cannot: the contract freezes
    # text_height_for = slot * 0.45, so the ratio is identically 0.45 and the gate never fires.
    # Keep both — the ratio still catches an implementer who ignores text_height_for.
    "text_height_mm_min": 3.5,
    "text_slot_ratio_max": 0.6,
    "part_bbox_overlap_pairs_max": 0,     # exploded figures only, measured per BODY (§11.10 #6)
    "labels_per_figure_max": 20,
    "non_numeral_text_ratio_max": 0.10,
    "leader_crossing_max": 0,
    "leader_hits_numeral_box_max": 0,
    # The leader must start ON the part it points at. Measured as the distance from the anchor
    # to the nearest GEOM/HIDDEN segment, over the figure's diagonal. An earlier implementation
    # anchored on the axis-aligned bounding box for every direction but one, which for a ring,
    # an L or any concave part sits in open space: on the real assembly 29 of 40 anchors were
    # more than 0.5 mm off the geometry and the worst was 12% of the sheet diagonal away.
    "leader_anchor_gap_ratio_max": 0.01,
    "non_continuous_max": 0,
}

FORBIDDEN_TEXT_PATTERNS = [r"^[A-Z]{2,4}[0-9]{4,8}(-|_)", r"^[0-9]{4,6}-[A-Z][0-9]{2}",
                           r"_[0-9]+_[0-9]+$"]

def check_figure(dxf: Path, *, thresholds: dict | None = None,
                 forbidden: list[str] | None = None, kind: str = "exploded") -> dict
    """Returns the qa.json shape from §4.4. Never raises on a bad drawing — it reports."""
```

Every check carries a `hint` that names the plan edit that would fix it, because the model is only
allowed to edit the plan. Example hint for `labels_per_figure`:
`"split fig2 — see assembly.json:split_suggestions"`.

### 5.6 `numbering.py`, `plan.py`

```python
# numbering.py
class Numbering:
    def numeral_of(self, part_name: str) -> int | None
    def label_mode(self, part_name: str) -> str            # once | all | none
    def term_of(self, part_name: str) -> str | None
    def table(self) -> list[tuple[int, str, str]]          # (numeral, term, selector)
    def description_zh(self) -> str

def assign(terms: list[dict], part_names: Sequence[str]) -> Numbering
    """Numerals 1..n issued in `terms` order. A part matched by several selectors takes the
    FIRST matching term (document this precedence). Selectors matching nothing are the caller's
    error to report, not this function's to silently skip."""

# plan.py
@dataclass
class PlanIssue:
    code: str; severity: str; message: str; hint: str; pointer: str   # JSON-pointer-ish

def load_plan(path: Path) -> dict                          # + jsonschema validation
def validate(plan: dict, assembly: dict) -> list[PlanIssue]
def apply_defaults(plan: dict) -> dict                     # merge layout defaults, clamp ranges
```

Error codes: `E_SELECTOR_NO_MATCH`, `E_TERM_LOOKS_LIKE_PART_CODE`, `E_TOO_MANY_LABELS`,
`E_MEMBER_NO_MATCH`, `E_DUPLICATE_FIGURE_ID`, `E_UNKNOWN_ENUM`, `E_SCHEMA`, `W_CLAMPED`,
`W_UNLABELLED_PART`.

## 6. CLI surface

Every CLI: `--help` is the contract for agents; `--json` writes machine-readable output; exit 0 on
pass, 1 on failure, 2 on usage error. No CLI ever prints a stack trace as its only output.

```bash
python3 scripts/analyze_assembly.py ASM.stp -o assembly.json [--include GLOB]... [--exclude GLOB]...
python3 scripts/validate_figure_plan.py plan.json --assembly assembly.json [--json issues.json]
python3 scripts/render_patent_figure.py plan.json --assembly assembly.json -o out/ \
        [--cache .cache/] [--preview] [--only fig2]
python3 scripts/qa_patent_figure.py out/fig1.dxf [--kind exploded] [--json qa.json]
python3 scripts/doctor.py [--json]
python3 scripts/make_test_assembly.py -o tests/fixtures/synthetic.stp
```

`render_patent_figure.py` writes `out/<figure id>.dxf`, `out/<figure id>.png` (with `--preview`),
and `out/reference-numerals.json`. It runs the QA gate on each figure and exits 1 if any fails,
printing the failing checks with their hints.

## 7. Determinism rules (mandatory, testable)

1. No `random`, no `time`, no `uuid`, no `id()`, no `hash()` of objects in any output-affecting path.
2. Never iterate a `set` or a `dict` built from unsorted input — sort explicitly with a total key.
3. Ties break on a stable secondary key (usually the item's index in a sorted list), never on
   insertion order.
4. Round floats before comparison (`round(v, 9)`); never compare raw floats for equality.
5. Numpy: `np.argsort` defaults to `kind='quicksort'`, which is **not** stable — verified against
   numpy 2.0.2. Always pass `kind="stable"` explicitly. Prefer plain `sorted()` on tuples when the
   data is small; it is stable by definition and needs no flag.
6. Globs: use `fnmatch.fnmatchcase`, never `fnmatch.fnmatch`. `fnmatch` routes through
   `os.path.normcase`, which is identity on macOS/Linux and lowercasing on Windows — verified — so
   the same plan would select different parts on different platforms. This applies to every glob in
   the system: `terms[].selector`, `figures[].members`, `source.include/exclude`.
7. Every module-level constant that affects output lives in **one** place and is named. No magic
   numbers inside function bodies.
8. `normalized_digest` of two runs of the same plan must be equal — `tests/test_golden.py` enforces
   this by rendering twice in one process and once in a subprocess.

## 8. Compliance rules (the R2 fix)

Patent figures carry **only** geometry, leaders, numerals, and the figure caption.
No parts table, no part names, no internal part codes, no dimensions, no title block.
The engineering parts table (`NO./NAME/QTY/REMARK`) is available **only** behind
`layout.engineering_table: true`, is off by default, and files produced with it must be written as
`<id>_engineering.dxf` so a review copy can never be mistaken for a filing copy.

## 9. Reporting format for implementation agents

Return exactly this JSON:

```json
{"files_written": ["scripts/patent_figure/layout.py"],
 "public_api": ["layout_exploded(pieces, axis2d, density, sheet_aspect, max_rows) -> LayoutResult"],
 "decisions": ["gap is a fraction of the median piece extent, not of the total string"],
 "deviations": ["none"],
 "open_issues": ["fit_to_frame margin interacts with label margin — caller must reserve space"],
 "self_test": "python3 -c '...' output summary"}
```

Write real files with the Write tool. Do not print code into the report. Do not run `git`.

## 10. Test fixtures

**Never** use the customer STEP or any real part name in the repository.

`scripts/make_test_assembly.py` is **already written and verified** — do not rewrite it. It emits
`tests/fixtures/synthetic.stp` (64 KB, 8 distinct parts, 11 instances, all named `SYN-*`):

| Part | Shape | Why it is there |
| --- | --- | --- |
| `SYN-A01` | 60×60×6 box | flat base, large footprint |
| `SYN-B02` | ⌀52×18 cylinder | coaxial stack member |
| `SYN-C03` | ⌀18×34 cylinder | tall narrow part — worst case for slot sizing |
| `SYN-D04` | torus R20 r3.5 | silhouette with no B-rep edge (needs `OutLine*`) |
| `SYN-E05` | ⌀44×5 cylinder | thin disc |
| `SYN-F06` | ⌀32 sphere | pure smooth silhouette |
| `SYN-G07` | 44×44×4 box | cover plate |
| `SYN-H08` ×4 | ⌀4×10 cylinder | four instances — exercises qty, `label: once/all/none` |

Verified round trip: `cad_hlr_to_dxf.py tests/fixtures/synthetic.stp --list-parts` reports
`11 instance(s), 8 distinct part(s)` with every `SYN-*` name intact.

**No degenerate part is in the STEP, on purpose.** A bare vertex loses its name in the STEP round
trip and returns labelled with the OCCT version string, which would make the golden digest depend
on the installed OCCT build. Cover the degenerate path in `tests/test_analyze.py` by constructing a
`PartShape` with `lo == hi` directly.

Real-assembly runs happen outside the repo, under the session scratchpad.

## 11. Algorithms

> 本节是 `layout.py`、`labels.py` 以及 `render_patent_figure.py` 版面求解部分的**唯一实现依据**。
> 与 §5.2 / §5.3 的签名冲突时，签名为准；与 §5.2 / §5.3 的**函数体描述**冲突时，本节为准，
> 所有此类偏离在 §11.10 逐条列出并给出理由。伪代码里出现的每一个数字都必须能在 §11.2 找到来源。

### 11.0 模块分工与坐标系（先读这一段，否则 11.3 与 11.7 会互相矛盾）

| 模块 | 坐标系 | 单位 | 知道毫米吗 |
| --- | --- | --- | --- |
| `layout_exploded` / `layout_assembly` | 已投影的图面 2D 坐标 | **模型单位**（未缩放） | **不知道**。只拿到无量纲的 `sheet_aspect` |
| `fit_to_frame` | 同上 | 输入模型单位，输出纯比例 | 只知道调用方传进来的 `frame_w/frame_h` |
| `place_labels` | 图纸坐标 | **毫米**（已乘 `s` 并平移进图框） | 知道 |
| `render_patent_figure.py` | 两者之间的桥 | 毫米 | 知道 |

**这条分工是硬约束。** 三个候选设计里有两个把「字高不动点」写进了 `layout_exploded` 的函数体，
而该函数的冻结签名只有 `sheet_aspect: float`，函数体内根本拿不到毫米量纲，也拿不到图题带高度。
本节把字高求解整体放在 `render_patent_figure.py`（见 §11.6），`layout_exploded` 只做纯 2D 排布。

渲染主循环（`render_patent_figure.py`，每张图一次）：

```
solve_figure(pieces, axis2d, density, labelled_keys, n_labels, figure_id, kind)
  1. 按 §11.6 对 TEXT_SERIES 做降序扫描，每一档 h：
       lm, cb   = label_margin_mm(h), caption_band_mm(h)
       aw, ah   = FRAME_W - 2*lm, FRAME_H - lm - cb
       result   = layout_exploded(pieces, axis2d, density, sheet_aspect=aw/ah, max_rows=1)
       s        = fit_to_frame(result, aw, ah, margin=0.0)
       接受条件见 §11.6
  2. 接受后：把几何按 s 缩放并居中进 (aw, ah)，图框原点取 FRAME 左下角
  3. sheet_lo/sheet_hi = 整个 FRAME（180x250），不是 (aw, ah)——
     标记允许落进边槽，几何不允许
  4. place_labels(...) 在毫米坐标系里跑
  5. 页面填充闸门在这里判（layout 不判，它不知道毫米）：见 §11.6
```

### 11.1 冲突裁决：单轴一维串 vs 二维排布

**裁决：权威 A 成立。`layout_exploded` 只实现单轴一维串，`max_rows` 只接受 1，传入其它值直接抛
`LayoutError`。权威 B 的「段内错列 / 二维装箱」在本项目中被废止，不实现、不保留降级路径。**
代价是明确的：见本节末「放弃了什么」。

#### 11.1.1 先把两个被三个候选设计一致误用的口径钉死

**【架构师裁决 D1｜占版率的口径】** `geometry_occupancy` = `area(GEOM∪HIDDEN 图层包围盒) /
area(模型空间全部实体包围盒)`。依据：用失败基线反算，几何包围盒 2382×1286.1 = 3.063e6，
全实体包围盒 2576×8791 = 2.265e7，比值 **0.1353**，与报告的 13.5% 逐位吻合；同时明细表包围盒
占比回算得 0.765，与报告的 82.3% 同量级。**该指标是尺度不变的**——把整张图等比放大缩小，
分子分母同步变化，比值不变。因此它度量的是「图面被表格 / 文字 / 边槽稀释了多少」，
**不度量「有没有填满纸」**，也**与 `fit_to_frame` 返回的 `s` 完全无关**。
三个候选设计都拿它当「填版面」指标，因而三份表格全部不可采信。

**【架构师裁决 D2｜新增页面填充闸门】** 既然 D1 不度量填版面，就必须补一条度量填版面的：

```
sheet_fill = area(GEOM∪HIDDEN 图层包围盒) / (aw * ah)          aw, ah = 本图的可用区（mm）
```

**分母是可用区，不是整张 FRAME。** 因为 `fit_to_frame` 恒取最大适配缩放，几何必然贴到可用区的
至少一条边，于是恒等式 `sheet_fill = min(γ/α, α/γ)` 成立（`γ = Gw/Gh` 是几何图面长宽比，
`α = aw/ah` 是可用区长宽比）。也就是说这条闸门度量的是**几何长宽比与纸张长宽比的匹配度**，
而在「恒取最大适配」的前提下，匹配度就等价于填版面程度。
分母若取整张 FRAME，正方形几何在任何字号档下都只能得到 0.487~0.513，**结构性地过不了 0.55**，
而正方形几何的长宽比是旋转不变的（`Gw = Gh = L(|cosθ|+|sinθ|)` 对任意 θ 成立），
错误提示里的「改 axis_angle」会推荐一个什么都改变不了的编辑，用户原地打转——
那是把「双重留边」那类标定错误在更高一层重演。取可用区作分母则正方形得 0.679~0.685，正常通过。

DXF 以毫米写出（`$INSUNITS=4`，§5.4 强制），`h` 可从 `NUM` 图层的 TEXT 实体直接读到，
`aw/ah` 由 §11.6 的 `label_margin_mm(h) / caption_band_mm(h)` 闭式还原，所以 qa.py 完全算得出来。
阈值 `sheet_fill_min = 0.55`，与 `geometry_occupancy_min` 同值。
**本节全部「占版率」论证都指 `sheet_fill`。**

**【架构师裁决 D3｜字高闸门的真实口径】** 契约冻结的 `text_height_for(slot, 0.45) = slot*0.45`
使 `text_height/slot ≡ 0.45 ≤ 0.6` **恒成立**——这道闸门是自指的，永远不会因为字太小而报警。
基线的 1.74 是旧代码把字高按整图 `size*0.022` 取比例造成的，签名本身已经消灭了那个 bug。
所以真正有判别力的是**绝对字高**：新增 `text_height_mm_min = 3.5`（GB/T 14691 数字最小字号，
也是专利附图缩印后仍可辨认的下限）。由 `h ≥ 3.5` 与 `h/slot ≤ 0.6` 反解得
**被标注件的图面外廓必须 ≥ 3.5/0.6 = 5.833 mm**，这才是那道闸门真正在管的事。

#### 11.1.2 一维串在什么条件下够用——把账算完

图幅：`FRAME_W×FRAME_H = 170×250 mm`。**注意本节的实测表是按 180 宽算的**，
因为定稿当时 §5.2 冻结的是 180；后续核实《审查指南》五部一章 4.3 的页边要求
（A4 顶 25 / 左 25 / 右 15 / 底 15）后，合规可用宽只有 `210-25-15=170`，
180 在几何上无法合规摆放（`25+180+15=220 > 210`），已改为 170。
宽度收窄 5.6% 会让本节表里的 `sheet_fill` 与最优角略有变化，**结论方向不变**
（一维串在 N≤20 下够用、最优角由闭式解逐图求），但表中的具体数字应以实跑的 qa 为准。
扣掉标记边槽与图题带后的可用区 `aw×ah` 见 §11.2 的 `label_margin_mm/caption_band_mm`。
零件沿轴足迹 `w_i`，间隙 `G = DENSITY[density] * median(w)`（§5.2 冻结语义），
串沿轴总长 `L = Σw_i + (n-1)G`，垂轴宽 `V`。图面轴对齐包围盒：

```
Gw = L*|cos θ| + V*|sin θ|        Gh = L*|sin θ| + V*|cos θ|        θ = axis_angle
s  = min(aw/Gw, ah/Gh)            sheet_fill = min(γ/α, α/γ),  γ = Gw/Gh,  α = aw/ah
```

`sheet_fill` 在 `γ = α` 时取极大。闭式解为

```
tan|θ*| = (L - alpha*V) / (alpha*L - V)          θ* = 180° - atan(...)   （θ ∈ (90°,180°)）
```

（**不要解 `Gw/Gh = alpha` 的等式的化简形式**：当 `alpha*L - V → 0`，即串的长宽比恰为 `1/alpha`
时该式奇异；`L/V` 落在 `[alpha, 1/alpha]` 区间内时根本无解，因为这么胖的块在任何旋转下
都达不到 `alpha` 的长宽比。此时按 §11.3 S9 的 1° 定步长扫描取最优角，见常数 `ANGLE_GRID_DEG`。）

实算（等尺寸件、`density=normal`、`h=3.5` 档的 `aw×ah = 152×222`、`alpha = 0.6847`）：

| axis_angle | 缩放 s | 几何 (mm) | γ = Gw/Gh | sheet_fill | 单件槽位 | 0.45×槽位 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 120° | 8.262 | 132.9×222.0 | 0.599 | 0.875 | 8.26 | 3.72 | 通过 |
| 122° | 8.424 | 143.1×222.0 | 0.645 | 0.941 | 8.42 | 3.79 | 通过 |
| **124°（θ\*）** | **8.512** | **152.0×219.6** | **0.692** | **0.989** | **8.51** | **3.83** | **通过** |
| 126° | 8.125 | 152.0×204.9 | 0.742 | 0.923 | 8.13 | 3.66 | 通过 |
| 130° | 7.473 | 152.0×179.1 | 0.849 | 0.807 | 7.47 | 3.36 | 通过（字高走 floor override） |
| 135° | 6.835 | 152.0×152.0 | 1.000 | 0.685 | 6.83 | 3.08 | 通过（同上） |
| 141° | 6.257 | 152.0×124.8 | 1.218 | 0.562 | 6.26 | 2.82 | 擦线通过 |
| 143.5° | 6.062 | 152.0×114.7 | 1.325 | **0.517** | 6.06 | 2.73 | **sheet_fill 不达标** |
| **152°（现默认）** | 5.557 | 152.0×84.3 | 1.802 | **0.380** | 5.56 | 2.50 | **不达标，且槽位 5.56 < 5.833** |
| 165° | 5.123 | 152.0×45.3 | 3.354 | **0.204** | 5.12 | 2.31 | **不达标** |
| 180° | 4.992 | 152.0×5.0 | 30.45 | **0.022** | 4.99 | 2.25 | **不达标** |

**结论一：`axis_angle` 的现默认值 152° 是失败基线里除明细表之外的第二个真凶。**
在 152° 下几何只吃掉可用区的 38%（对整张 A4 只有 28.5%），纸的高度方向 2/3 是空的。
`references/cad-source-to-drawing.md` §4 推荐的「150–160° 对竖直装配轴读起来舒服」是目视经验，
在 A4 **竖版**上被测量证伪：竖版要求串更陡。

**【架构师裁决 D4】`layout.axis_angle` 的默认值由 152 改为 124。** 该值落在 §4.2 冻结的
夹紧区间 `[120,180]` 内，且正是权威 A 自己开的药方（"To get a diagonal string on the sheet,
roll the view instead"），不需要任何横向位移。`roll_for_axis()`（§5.1 已有闭式解）负责把它变成
`ViewFrame.roll`。三档字号下 `θ*` 分别为 124.17° / 124.17° / 124.40°，取整为 **124**，
对字号档位不敏感，这就是它能当默认值的原因。`sheet_fill ≥ 0.55` 的可行窗口约为 `[120°, 141°]`，
`θ*` 稳稳落在窗口中部。

**结论二：件数上限（等尺寸件，θ=124°，density=normal）。**

| N | 接受字号 h | s | sheet_fill | 槽位 mm | h/槽位 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| 2 | 7.0 | 65.63 | 0.805 | 65.63 | 0.107 | 通过 |
| 4 | 7.0 | 37.11 | 0.892 | 37.11 | 0.189 | 通过 |
| 10 | 7.0 | 16.11 | 0.957 | 16.11 | 0.435 | 通过 |
| 12 | 5.0 | 13.55 | 0.965 | 13.55 | 0.369 | 通过 |
| 16 | 3.5 | 10.56 | 0.983 | 10.56 | 0.331 | 通过 |
| **20** | **3.5** | **8.51** | **0.989** | **8.51** | **0.411** | **通过** |
| 24 | 3.5（floor override） | 7.13 | 0.994 | 7.13 | 0.491 | 通过 |
| 29 | 3.5（floor override） | 5.92 | 0.997 | 5.92 | 0.591 | 通过（边界） |
| 30 | — | 5.73 | 0.998 | 5.73 | — | **LayoutError：槽位 5.73 < 5.833** |

**在 θ=124° 下，单轴一维串可容纳 29 个等尺寸零件实例**，而 §4.2 的标记上限是 20 个标记。
所以在「零件尺寸不悬殊」这一类输入上，一维串有 45% 的件数余量，二维排布带来的额外空间
**买不到任何一道闸门的通过**。为一个买不到收益的目标去破坏装配关系，是拿正确性换零。

#### 11.1.3 必须承认的两件事（不粉饰）

**(1) 「20 个标记」不等于「20 个零件」。** §4.2 的规则原文是 *Effective label count per figure*；
`label: "once"`（同名件只标一次）、`label: "none"`（标准件不标）加上 `members: ["*"]`，
使「90 个实例 / 20 个标记」成为完全合法的 plan，`E_TOO_MANY_LABELS` 不会触发。
**零件数在契约里没有上限，因此必须由 §11.6 的字高求解补上这道闸门**，
失败消息要明说「标记数没超、件数超了」，否则这条阈值会被绕过。

**(2) 尺寸悬殊时一维串确实会输给二维排布，而我选择输。**
实测一组 6:1 尺寸分布的 20 件栈（沿轴足迹 60…10，最小**被标注**件 18 单位）：

| axis_angle | density | s | sheet_fill | 被标注件最小槽位 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 124° | compact | 19.56 | 0.956 | 5.87 mm | 通过（余量仅 0.6%） |
| 124° | normal | 17.49 | 0.962 | 5.25 mm | **LayoutError（< 5.833）** |
| 135° | normal | 14.27 | 0.685 | 4.28 mm | LayoutError |
| 152° | normal | 11.80 | 0.398 | 3.54 mm | LayoutError（两条闸门同时失败） |

同一批零件按面积二维装箱可得约 3 倍的比例尺（`sqrt(Area/N)` vs `D_frame/N`，N=20 时理论 3.1 倍），
能把 5.25 mm 抬到 15 mm 以上，一张图就装下了。**我放弃这个收益。** 理由：

- 分解图的法律功能是表达装配关系。把零件推离它自己的轴，读图者无法再判断「谁装进谁、谁与谁同轴」，
  这一项不可交易；`references` §4 的禁令保护的正是它。
- 二维排布并不是这类输入的正确解药。真正的病因是「一颗 18 单位的小件被要求带标记」，
  正确的 plan 编辑是 `label: "none"`（标准件不标）、或按 `assembly.json:split_suggestions` 拆图、
  或 `density: "compact"`。这三条编辑模型都能做，而「把零件推离轴」模型做不了、也不该做。
- 代价量化：这条裁决会让「尺寸跨度 > 3:1 且小件需要标记」的装配体多出 1~2 张附图。
  多一张附图的成本远低于一张关系失真的附图。

**(3) 我不主张一维串在所有角度都赢。** 上表白纸黑字：152° 下一维串六项里输两项。
这条裁决**是有条件的**，条件就是 D4——`axis_angle` 默认必须改到 124°。
若有人坚持保留 152°，本裁决作废，那时应当拆图，仍然不是横向位移。

#### 11.1.4 放弃了什么（写进 report 的 decisions）

- 放弃 `max_rows > 1`：不实现二维排布，传 >1 抛 `LayoutError`（**绝不静默降级成一维串**——
  静默降级会让「记得传参的调用方」和「忘了传的调用方」渲染出两张不同的图而两边都通过 QA）。
- 放弃 `references` §4 建议的 150–160° 观感，换取竖版填充率；理由与实测见 11.1.2。
- 放弃「尺寸悬殊时把小件也塞进同一张图」，改为报错并给出三条可执行的 plan 编辑。

### 11.2 常数表

**规则：函数体内不得出现任何裸数字**（`0`、`1`、`2`、`0.5` 这类无量纲结构常数除外）。
下表每一行都必须能在实现里以同名常量出现，且只定义一次（§7 规则 7）。

#### 11.2.1 `layout.py`

| 名字 | 值 | 从哪个约束反解出来 |
| --- | --- | --- |
| `DENSITY` | `{"compact":0.35, "normal":0.55, "loose":0.85}` | **§5.2 冻结原值，逐位照抄。** 语义同样冻结：**中位 body 沿爆炸方向足迹的比例，不是串总长的比例**。三个候选设计都误抄成 0.035/0.055/0.085（差 10 倍）并都误用了「总跨度」语义——后者正是 v1「串越长每件槽位越小而间隙越大」的失败机理，§5.2 注释已点名 |
| `FRAME_W, FRAME_H` | `180.0, 250.0` | §5.2 冻结：A4 竖版减 15 mm 边距 |
| `RANK_DEC` | `9` | §7 规则 4 |
| `EPS_REL` | `1e-9` | 一切比较先做 `Q(v) = round(v / SCALE_REF, RANK_DEC)`。float64 相对精度 2.2e-16，留 7 个数量级吸收 ≤1e5 次运算的累积舍入。**必须是相对量**：`layout.py` 拿到的是模型单位，同一装配体以 m 或以 mm 导出，绝对 `round(v,9)` 要么完全不起作用要么过度合并 |
| `OVERLAP_TOL_REL` | `1e-6` | §5.2 冻结的 `1e-6` 重叠容差，改写为 `1e-6 * SCALE_REF`（同上理由；偏离见 §11.10） |
| `DEGEN_REL` | `1e-6` | §5.1 `PartShape.degenerate` 的定义（max extent < 1e-6），同样改写为相对量 |
| `AXIS_MIN_NORM` | `1e-3` | `axis2d` 是**单位** 3D 轴在图面上的投影，模长天然落在 `[0,1]`，故这里用绝对值合法。低于 1e-3 时图面方向不足 3 位有效数字，而 `_sep_required` 要除以它的分量。legacy 用 1e-6：`k=1e-5` 时位移被放大 1e5 倍且不报错 |
| `AXIS_COMP_MIN` | `1e-3` | 同上，逐分量版本。`_sep_required` 里除以 `e[k]`，`|e[k]| < 1e-3` 的那一支放大 >1e3 倍，直接跳过（跳过是安全的：该支给出的 `t` 本来就是天文数字，`min()` 不会选它） |
| `ANGLE_LO, ANGLE_HI` | `120.0, 180.0` | §4.2 冻结的 `axis_angle` 夹紧区间 |
| `ANGLE_GRID_DEG` | `1.0` | 闭式 `θ*` 无解时的定步长扫描分辨率。plan 里 `axis_angle` 以整数度书写，1° 就是可表达的最小改动，再细没有意义。定步长 61 次求值 ⇒ 确定、无收敛判据 |
| `SLOT_QA_CAP_K` | `1.2` | `qa` 若按「几何包围盒对角 / 标记数」度量 slot（失败基线的算法），要保证 `0.45*slot ≤ 0.6*(diag/N)` 需 `slot ≤ 1.333*diag/N`；退 10% 余量取 1.2。一行成本换两种口径都过闸 |
| `AXIS_ANGLE_DEFAULT` | `124` | 【架构师裁决 D4】三档字号下闭式 `θ*` = 124.17/124.17/124.40，取整。见 11.1.2 |

#### 11.2.2 版面求解常数（定义在 `layout.py`，由 `render_patent_figure.py` 使用）

| 名字 | 值 | 从哪个约束反解出来 |
| --- | --- | --- |
| `TEXT_SERIES` | `(7.0, 5.0, 3.5)` | GB/T 14691 字高系列 `1.8/2.5/3.5/5/7/10/14/20` 中适用于专利附图标记的三档。降序排列是 §11.6 扫描方向的前提 |
| `TEXT_FLOOR_MM` | `3.5` | GB/T 14691 数字最小字号；2.5 mm 缩印至 2/3 只剩 1.67 mm，受理后不可辨认。**待核对现行《专利审查指南》原文，与 iteration-plan §7.5 的保留一致** |
| `TEXT_RATIO` | `0.45` | §5.3 冻结的 `text_height_for` 默认比值，为 QA 的 0.6 留 25% 余量 |
| `QA_TEXT_SLOT_MAX` | `0.6` | §5.5 `text_slot_ratio_max` |
| `SLOT_FLOOR_MM` | `TEXT_FLOOR_MM / QA_TEXT_SLOT_MAX` = `5.8333…` | 派生量，不单独取值。被标注件的图面外廓下限 |
| `LABEL_MARGIN_K` | `4.0`（×h） | 一个从几何边缘朝外伸出的标记要占：`RING_BASE_K(1.0) + LAND_K(2.0) + RUNOUT_K(0.5) + PAD_X(0.3)` = 3.8h，取 4.0h |
| `LABEL_MARGIN_CAP_MM` | `16.0` | = 8.9% × `FRAME_W`。再大则两侧边槽合计吃掉 18% 版宽，可用区面积占比跌到 0.70 以下，对 `sheet_fill` 余量不足。该封顶只在 h ≥ 5 时起作用 |
| `CAPTION_K` | `4.0`（×h） | 图题带 = 图题字高 `CAPTION_RATIO*h` + 上下净空各 1.2h = 4.0h |
| `CAPTION_CAP_MM` | `16.0` | 同 `LABEL_MARGIN_CAP_MM` |
| `CAPTION_RATIO` | `1.6` | 图题比附图标记大一档半，是国内机械制图的通行做法；同时保证 h=3.5 时图题 5.6 mm 仍在 GB 系列的可读区 |
| `SHEET_FILL_MIN` | `0.55` | 【架构师裁决 D2】新增闸门。`sheet_fill = 几何包围盒面积 / (aw*ah) = min(γ/α, α/γ)`。分母取**可用区**而不是整张 FRAME：后者会让正方形几何在任何字号档下只得 0.487~0.513，结构性不可达，而正方形的长宽比旋转不变，错误提示里的「改 axis_angle」什么也改变不了 |

#### 11.2.3 `labels.py`（全部以字高 `h` 为单位表达，因此与纸张、比例尺、零件数无关）

| 名字 | 值 | 从哪个约束反解出来 |
| --- | --- | --- |
| `RANK_DEC` | `9` | §7 规则 4 |
| `EPS_SHEET_REL` | `1e-9` | 比较容差 = `1e-9 * 图框对角`。同 `EPS_REL` 的理由 |
| `N_DIRS` | `12` | 30° 栅格。GB/T 4457.2 认可的指引线角度集合是 30/45/60/90° 的倍数；30° 是该集合的最小公倍步长 |
| `PHASE_DEG` | `15.0` | 相位偏置使候选角为 15/45/…/345°，**没有一个是 0° 或 90°**。GB/T 4457.2 要求指引线不得水平、竖直、或平行于轮廓线。附带好处：`d[0]` 与 `d[1]` 恒非零，`_ray_exit_aabb` 不需要除零分支 |
| `DIR_ROUND_DEC` | `12` | 模块导入时把 12 个方向的 `cos/sin` `round` 到 12 位。libm 的三角函数误差在 1 ULP（相对 ~1e-16）量级，round 到 1e-12 后被完全吸收，任何平台得到同一组常量 |
| `CHAR_W` | `0.71` | GB/T 14691 B 型字宽 = `h/√2` = 0.7071，向上取 0.71（宁可把数字框算宽，不可算窄） |
| `PAD_X` | `0.30`（×h） | GB 字距 `a = 0.2h`。两个数字框水平净距 = `2*PAD_X*h = 0.6h = 3` 倍字距 ⇒ 相邻的 "1" 与 "2" 不会被读成 "12" |
| `PAD_Y` | `0.20`（×h） | 行高 = `h + 2*PAD_Y*h = 1.4h` = GB/T 14691 B 型最小行距 |
| `LAND_K` | `2.0`（×h） | GB/T 4457.2：基准线不短于所注文字长度。两位数字宽 `2*0.71h = 1.42h`，取 2.0h 使其读作「线」而非「钩」 |
| `RUNOUT_K` | `0.50`（×h） | 文字与基准线端点的间隙 = 2.5 倍字距。修掉 legacy `gen_exploded.py` L134「数字放在基准线中点却用端对齐 ⇒ 文字压线」 |
| `RING_BASE_K` | `1.0`（×h） | 第 0 圈候选距零件包围盒的距离：至少让开一个字高，引线才看得见起点 |
| `RING_GROWTH` | `1.8` | 几何级数比。四圈覆盖 `1.0 / 1.8 / 3.24 / 5.83` h |
| `RING_COUNT` | `4` | 外圈 5.83h：再远则引线本身比标记足迹还长，读作杂线 |
| `GRID_CELL_K` | `4.0`（×h） | 障碍索引网格边长。数字框最大边约 `2*CHAR_W*h + 2*PAD_X*h ≈ 2.0h`，故一个框最多覆盖 2×2 个格 |
| `CLEAR_K` | `0.50`（×h） | 打分用的「擦边」膨胀量：数字框外扩 0.5h 后仍碰到几何 ⇒ 判为擦边 |
| `CROWD_R_K` | `3.0`（×h） | 拥挤统计半径。3h ≈ 两个数字框的对角，超出此距离视觉上不再成组 |
| `W_LEN` | `1.0` | 打分基准：引线每长 1h 记 1 分。以下三项都是「等价于多少 h 引线长度」的显式汇率，不是拟合值 |
| `W_DIR` | `2.0` | 方向偏离首选方向 60°（`1-cos60° = 0.5`）记 1.0 分 = 1h 引线 |
| `W_CLR` | `3.0` | 擦几何记 3 分：宁可多绕 3 个字高也不擦线 |
| `W_CROWD` | `2.0` | 每有一个已落位数字框落在 `CROWD_R_K*h` 内记 2 分 |
| `MAX_REPAIR` | `3` | 有界回退轮数。每轮至少禁掉一个已落位候选，禁集单调增长 ⇒ 必终止；3 轮之后仍无解说明这张图过载，应当拆图而不是继续挤 |

### 11.3 `layout_exploded` 伪代码

```python
# ============================== layout.py ==============================
from __future__ import annotations          # Python 3.9：签名里的 list[...] / X | Y 才合法
import math
import numpy as np

class LayoutError(RuntimeError):
    """携带足够上层生成 plan 修复提示的信息，见 §11.8。"""


# ---- 确定性算术工具（§11.9 第 5、6 条）--------------------------------------
def _dot2(P: np.ndarray, v: np.ndarray) -> np.ndarray:
    """P:(M,2) v:(2,) -> (M,). **禁止写 `P @ v`。**
    (M,2)@(2,) 可能被派发到多线程 BLAS 的 gemv，其分块与归约次序随 CPU、线程数变化，
    浮点加法不满足结合律 ⇒ 末位结果随机器变。逐元素乘加没有跨元素归约，逐位可复现。"""
    return P[:, 0] * v[0] + P[:, 1] * v[1]


def _lower_median(vals: list[float]) -> float:
    """下中位数。**禁止用 np.median**：它走 introselect 分区，路径随 numpy 版本变，
    偶数长度还多做一次两值平均。这里排序后取 (n-1)//2，纯确定。"""
    s = sorted(vals)
    return s[(len(s) - 1) // 2]


def layout_exploded(pieces, axis2d, density, sheet_aspect=0.75, max_rows=1):
    # ---------- S0 入参校验（不静默降级）----------
    if len(pieces) == 0:
        raise LayoutError("layout_exploded: 零件列表为空")
    if density not in DENSITY:
        raise LayoutError("layout_exploded: 未知 density %r，可选 %s"
                          % (density, sorted(DENSITY)))
    if int(max_rows) != 1:
        # 【架构师裁决 §11.1】只实现单轴一维串。绝不静默降级。
        raise LayoutError(
            "layout_exploded: max_rows=%r。本实现按 impl-contract §11.1 只提供单轴一维串，"
            "二维排布已被废止（会破坏装配关系）。请传 max_rows=1；版面不够时的修复动作是"
            "调 layout.axis_angle（推荐 %d）、改 density、或按 assembly.json:split_suggestions 拆图。"
            % (max_rows, AXIS_ANGLE_DEFAULT))
    if not (float(sheet_aspect) > 0.0):
        raise LayoutError("layout_exploded: sheet_aspect 必须为正，收到 %r" % (sheet_aspect,))

    # ---------- S1 全局尺度基准与相对量化算子 ----------
    # 所有比较、排序键、容差都以 SCALE_REF 归一化。layout.py 拿到的是模型单位，
    # 绝对 round(v, 9) 在 1e5 量级坐标上等于 1e-14 相对精度（比舍入噪声还细，
    # 并列判定会在不同机器上翻转），在 1e-3 量级坐标上又会过度合并。
    pts_all = []
    for pc in pieces:
        if not pc.curves:
            raise LayoutError("layout_exploded: piece %s 无可见曲线，无法排布。"
                              "上游 HLR 可能静默丢了它的边；请在 plan 的 exclude 里排除，"
                              "或换 view。" % pc.key)
        arr = np.vstack(pc.curves)
        if arr.ndim != 2 or arr.shape[1] != 2 or arr.shape[0] < 1:
            raise LayoutError("layout_exploded: piece %s 的 curves 形状非法" % pc.key)
        if not np.all(np.isfinite(arr)):
            # NaN/Inf 会毒化 min/max，并让所有比较返回 False ⇒ 重叠检查会「假通过」
            raise LayoutError("layout_exploded: piece %s 的曲线含 NaN/Inf" % pc.key)
        pts_all.append(arr)
    ALL = np.vstack(pts_all)
    span_x = float(ALL[:, 0].max() - ALL[:, 0].min())
    span_y = float(ALL[:, 1].max() - ALL[:, 1].min())
    SCALE_REF = float(np.hypot(span_x, span_y))
    if not (SCALE_REF > 0.0) or not np.isfinite(SCALE_REF):
        raise LayoutError("layout_exploded: 全体几何退化为一点，无法排布")
    EPS = EPS_REL * SCALE_REF
    TOL = OVERLAP_TOL_REL * SCALE_REF

    def Q(v: float) -> float:                     # 唯一的比较/排序量化算子
        return round(float(v) / SCALE_REF, RANK_DEC)

    # ---------- S2 轴规范化 ----------
    a = np.asarray(axis2d, dtype=np.float64).reshape(2)
    na = float(np.hypot(a[0], a[1]))
    if not np.isfinite(na) or na < AXIS_MIN_NORM:
        raise LayoutError(
            "layout_exploded: 爆炸轴在图面上的投影模长 %.3e < %.3e，"
            "轴几乎平行于视线，沿轴排序无意义。修复：改 layout.view，或改 layout.explode_axis。"
            % (na, AXIS_MIN_NORM))
    e = a / na
    # 符号规范化：强制 e_y >= 0；e_y == 0 时强制 e_x >= 0。保证同一根轴不因上游符号约定翻转
    if round(float(e[1]), RANK_DEC) < 0.0 or (
            round(float(e[1]), RANK_DEC) == 0.0 and round(float(e[0]), RANK_DEC) < 0.0):
        e = -e
    p = np.array([-e[1], e[0]], dtype=np.float64)     # 图面法向，固定取 +90° 旋转

    # ---------- S3 逐件真实投影足迹 ----------
    # 用已投影的 2D 折线点，不用 3D world-AABB 的 8 个角点：后者对斜置长杆系统性高估足迹
    # （legacy 缺陷 1）。
    foot = []            # 与 pieces 同序
    for i, pc in enumerate(pieces):
        P = pts_all[i]
        av = _dot2(P, e)
        rec = {
            "i": i, "key": pc.key, "name": pc.name,
            "xlo": float(P[:, 0].min()), "xhi": float(P[:, 0].max()),
            "ylo": float(P[:, 1].min()), "yhi": float(P[:, 1].max()),
            "alo": float(av.min()), "ahi": float(av.max()),
        }
        rec["w"] = rec["ahi"] - rec["alo"]                     # 沿轴足迹
        rec["ac"] = 0.5 * (rec["alo"] + rec["ahi"])
        foot.append(rec)
    degenerate = [r["key"] for r in foot
                  if max(r["xhi"] - r["xlo"], r["yhi"] - r["ylo"]) < DEGEN_REL * SCALE_REF]
    if degenerate:
        # 明确失败，不静默跳过：静默跳过会让零件表与图面不一致，且零宽槽位会把邻件贴死
        raise LayoutError(
            "layout_exploded: 退化零件 %s（图面外廓 < %.1e × 全图跨度）。"
            "这通常是基准面/草图线被当成 Part 载入。修复：在 plan 的 source.exclude 里排除它们。"
            % (", ".join(sorted(degenerate)), DEGEN_REL))

    # ---------- S4 建 body：分离、HLR、标注的统一粒度 ----------
    # 组织句（整个 layout 只有这一句需要记住）：
    #   **body 是分离的单位、HLR 的单位、标注的单位。**
    # 三者粒度必须一致，否则：对称件（螺栓圈）必被拆散、逐件 HLR 必然画错遮挡、
    # 后置条件的包围盒检查必然无解。
    # 规则：同名实例按【轴向区间是否重叠】聚类。同名但分处不同工位的实例
    # （壳体上下各 4 颗同型螺钉）必须落到不同 body，否则并集包围盒横跨壳体，位移荒谬。
    names = sorted({r["name"] for r in foot})          # sorted，绝不遍历 set
    bodies = []
    for nm in names:
        inst = [r for r in foot if r["name"] == nm]
        inst.sort(key=lambda r: (Q(r["alo"]), Q(r["ahi"]), r["key"]))   # 全序，末位是唯一 key
        clusters, cur = [], [inst[0]]
        for r in inst[1:]:
            # 两侧都先量化再比：聚类归属是会级联改变整张图的离散悬崖，绝不裸比浮点
            if Q(r["alo"]) <= Q(cur[-1]["ahi"] + EPS):
                cur.append(r)
            else:
                clusters.append(cur); cur = [r]
        clusters.append(cur)
        for ci, cl in enumerate(clusters):
            bodies.append({
                "key": "%s#c%d" % (nm, ci),
                "members": [r["i"] for r in cl],
                "xlo": min(r["xlo"] for r in cl), "xhi": max(r["xhi"] for r in cl),
                "ylo": min(r["ylo"] for r in cl), "yhi": max(r["yhi"] for r in cl),
                "alo": min(r["alo"] for r in cl), "ahi": max(r["ahi"] for r in cl),
            })
    for b in bodies:
        b["w"] = b["ahi"] - b["alo"]
        b["ac"] = 0.5 * (b["alo"] + b["ahi"])
    # 沿轴装配序。**按轴向中心排，不按下界**：按下界排会让横跨全长的壳体抢占 0 号槽位，
    # 把它和它内部的零件甩到图面两端（legacy 缺陷 2）。末位 body["key"] 是全序决胜键，
    # 杜绝 legacy 缺陷 6（下界相等时顺序继承 XCAF 遍历序，换个 STEP 导出图就变）。
    bodies.sort(key=lambda b: (Q(b["ac"]), Q(b["alo"]), b["key"]))
    n = len(bodies)

    # ---------- S5 间隙 ----------
    # §5.2 冻结语义：**中位 body 沿爆炸方向足迹的比例，不是串总长的比例**。
    # 用总长会复现 v1 失败：串越长，每件槽位越小而间隙越大。
    med_w = _lower_median([b["w"] for b in bodies])
    G = DENSITY[density] * med_w

    # ---------- S6 最小轴向位移：差分约束系统 ----------
    # 位移只沿 e，不含任何法向分量 ⇒ **横向位移严格为 0**（权威 A 逐字执行，S8 有机器可验证断言）。
    def _sep_required(bi, bj) -> float:
        """bj 相对 bi 沿 +e 需要的最小位移，使两者的【轴对齐】包围盒不相交。
        关键认识：沿对角线分离的两个形体，其 axis-aligned 包围盒仍可能相交，
        而后置条件与 qa 查的正是 axis-aligned 包围盒。legacy 只把包围盒投影到轴向标量上
        做区间分离，所以在对角串下必然残留轴对齐重叠（legacy 缺陷 8）。
        AABB 不相交 ⟺ 在 x 或 y 至少一个轴上分离，故两支分别求解取更省的一支。"""
        cands = []
        ex, ey = float(e[0]), float(e[1])
        if abs(ex) > AXIS_COMP_MIN:                    # |分量| 太小的一支跳过：
            if ex > 0.0:                               # 它给出的 t 本来就是天文数字，min 不会选它，
                cands.append((bi["xhi"] - bj["xlo"]) / ex)      # 但除法会把噪声放大 1/|ex| 倍
            else:
                cands.append((bj["xhi"] - bi["xlo"]) / (-ex))
        if abs(ey) > AXIS_COMP_MIN:
            if ey > 0.0:
                cands.append((bi["yhi"] - bj["ylo"]) / ey)
            else:
                cands.append((bj["yhi"] - bi["ylo"]) / (-ey))
        if not cands:                                  # |e| = 1 时不可达；留作实现错误的捕捉点
            raise LayoutError("layout_exploded: 轴的两个分量都小于 %.1e（内部错误）"
                              % AXIS_COMP_MIN)
        t = min(cands)
        if Q(t) <= 0.0:
            # 【关键】零位移时本已不相交 ⇒ 不索取任何间隙。
            # 否则同工位的螺栓圈会被 G 逐个撑开，对称性当场瓦解。
            return 0.0
        return t + G

    # 固定顺序贪心 = DAG 最长路，对该装配序精确最优。
    # 归约按固定下标 0..i-1 顺序展开，**不对无序集合用 np.max**（避免归约树差异）。
    t = [0.0] * n
    for i in range(1, n):
        best = 0.0
        for j in range(i):
            v = t[j] + _sep_required(bodies[j], bodies[i])
            if v > best:
                best = v
        t[i] = best
    # 单调性：位移超过 _sep_required 后仍保持分离（e 的分量符号固定，j 相对 i 单向远离），
    # 故 t[i] 取 max 是安全的，不会破坏已满足的约束对。

    # ---------- S7 落位：body 整体刚体平移 ----------
    for bi, b in enumerate(bodies):
        off = np.round(t[bi] * e, RANK_DEC + 3)        # 写盘前的坐标 round 到 12 位；
        for m in b["members"]:                          # normalized_digest 只保留 6 位 ⇒ 6 位安全带
            pieces[m].offset = off.copy()
        b["off"] = off

    # ---------- S8 后置条件（不满足就抛，不许静默降级）----------
    # P0 —— 零横向位移（权威 A 的机器可验证形式）。e·p 在浮点里精确为 0，这个断言是免费的。
    for b in bodies:
        for m in b["members"]:
            if Q(float(pieces[m].offset[0] * p[0] + pieces[m].offset[1] * p[1])) != 0.0:
                raise LayoutError("layout_exploded: 不变量 P0 破坏——piece %s 有横向位移分量"
                                  % pieces[m].key)
    # P1 —— body 之间的图面包围盒两两不重叠（触边不算重叠，容差 TOL）
    bad = []
    for i in range(n):
        for j in range(i + 1, n):
            bi, bj = bodies[i], bodies[j]
            ix = (Q(bi["xlo"] + bi["off"][0]) < Q(bj["xhi"] + bj["off"][0] - TOL) and
                  Q(bj["xlo"] + bj["off"][0]) < Q(bi["xhi"] + bi["off"][0] - TOL))
            iy = (Q(bi["ylo"] + bi["off"][1]) < Q(bj["yhi"] + bj["off"][1] - TOL) and
                  Q(bj["ylo"] + bj["off"][1]) < Q(bi["yhi"] + bi["off"][1] - TOL))
            if ix and iy:
                bad.append((bi["key"], bj["key"]))
    if bad:
        raise LayoutError(
            "layout_exploded: body 包围盒重叠 %d 对，首例 %s / %s。"
            "差分约束求解未收敛——这是实现错误，不是输入错误。" % (len(bad), bad[0][0], bad[0][1]))
    # 注意 P1 只在 **body 粒度** 上成立。body 内部成员（如 20 颗螺钉的螺栓圈）允许互相重叠——
    # 强行拆散会把螺栓圈抹成一条直线，对称性彻底丢失。qa.py 必须读 sidecar 的 body 包围盒
    # 来校验 part_bbox_overlap，不要试图从 DXF 曲线猜零件边界（见 §11.10）。

    # ---------- S9 诊断量（layout 不判毫米闸门，只提供求解依据）----------
    lo = np.array([min(f["xlo"] + pieces[f["i"]].offset[0] for f in foot),
                   min(f["ylo"] + pieces[f["i"]].offset[1] for f in foot)])
    hi = np.array([max(f["xhi"] + pieces[f["i"]].offset[0] for f in foot),
                   max(f["yhi"] + pieces[f["i"]].offset[1] for f in foot)])
    Gw, Gh = float(hi[0] - lo[0]), float(hi[1] - lo[1])
    alpha = float(sheet_aspect)
    # 归一化框 (alpha × 1.0) 内的适配缩放与几何占可用区的面积比
    s_rel = min(alpha / Gw, 1.0 / Gh) if Gw > 0.0 and Gh > 0.0 else 0.0
    fill_usable = (Gw * s_rel) * (Gh * s_rel) / alpha          # ∈ (0, 1]，= min(γ/α, α/γ)
    # 串在 (e, p) 系里的长宽，用于闭式最优轴角；沿 p 的跨度对轴向位移不敏感，
    # 直接从原始投影点算（位移只有 e 分量，不改变任何点的 p 坐标）。
    L_axis = max(b["ahi"] + t[i] for i, b in enumerate(bodies)) \
             - min(b["alo"] + t[i] for i, b in enumerate(bodies))
    pv = _dot2(ALL, p)
    V_axis = float(pv.max() - pv.min())
    axis_angle_opt = _closed_form_best_angle(L_axis, V_axis, alpha, EPS)   # 见下

    diagnostics = {
        "strategy": "axial-1d-minimal-separation",
        "gap": G, "median_body_extent": med_w, "overlaps": 0,
        "bodies": n, "rows": 1,
        "scale_ref": SCALE_REF,
        "axis_angle_deg": round(float(np.degrees(np.arctan2(e[1], e[0]))), RANK_DEC),
        "axis_angle_opt": axis_angle_opt,
        "fill_usable": fill_usable,
        "body_boxes": [{"key": b["key"],
                        "lo": [b["xlo"] + b["off"][0], b["ylo"] + b["off"][1]],
                        "hi": [b["xhi"] + b["off"][0], b["yhi"] + b["off"][1]],
                        "members": [pieces[m].key for m in b["members"]]} for b in bodies],
        "extent_by_key": {pieces[f["i"]].key:
                          max(f["xhi"] - f["xlo"], f["yhi"] - f["ylo"]) for f in foot},
        "dropped": [],
    }

    # ---------- S10 slot：双口径夹紧 ----------
    ext = [max(f["xhi"] - f["xlo"], f["yhi"] - f["ylo"]) for f in foot]
    diag = float(np.hypot(Gw, Gh))
    slot_contract = min(ext)                                   # §5.2 的字面口径
    slot_qa_cap = SLOT_QA_CAP_K * diag / max(len(pieces), 1)   # 失败基线的口径
    return LayoutResult(pieces=pieces, lo=lo, hi=hi,
                        slot=min(slot_contract, slot_qa_cap),
                        rows=1, diagnostics=diagnostics)


def _closed_form_best_angle(L: float, V: float, alpha: float, eps: float) -> float:
    """使 sheet_fill 最大的 axis_angle（度），夹紧到 [ANGLE_LO, ANGLE_HI]。
    Gw/Gh = alpha 时 fill 取极大 ⇒ tan|θ| = (L - alpha*V) / (alpha*L - V)。
    分母趋零（L/V → 1/alpha）时该式奇异，且 L/V ∈ [alpha, 1/alpha] 时无解，
    因为那么胖的块在任何旋转下都达不到 alpha 的长宽比。此时退回定步长扫描。"""
    num, den = L - alpha * V, alpha * L - V
    if den > eps and num > eps:                    # eps = EPS_REL * SCALE_REF，同一把尺子
        th = 180.0 - math.degrees(math.atan(num / den))
        if ANGLE_LO <= th <= ANGLE_HI:
            return round(th, RANK_DEC)
    best, best_fill = ANGLE_LO, -1.0
    steps = int(round((ANGLE_HI - ANGLE_LO) / ANGLE_GRID_DEG))     # 定次，无收敛判据
    for k in range(steps + 1):
        th = ANGLE_LO + k * ANGLE_GRID_DEG
        c, s = abs(math.cos(math.radians(th))), abs(math.sin(math.radians(th)))
        gw, gh = L * c + V * s, L * s + V * c
        sc = min(alpha / gw, 1.0 / gh)
        f = round((gw * sc) * (gh * sc) / alpha, RANK_DEC)
        if f > best_fill:                                      # 严格大于 ⇒ 并列取更小的角
            best_fill, best = f, th
    return best
```

**关于 `max_rows`**：本实现恒返回 `rows = 1`。这不是 deviation，是 §11.1 的裁决；
传入 `max_rows != 1` 是**错误**而不是提示，因为静默降级会产生「两个调用方两张图都通过 QA」
的可复现性失败。

**关于 HLR 粒度（必须写进实现说明）**：body 内部成员的图面包围盒允许相交（螺栓圈就是如此），
因此 §5.1 的 `part_curves`（逐件 HLR，仅对图面互不相交的件有效）在 body 内部**不成立**。
必须对每个 body 调用一次 `scene_curves(body.members, ...)`，让体内遮挡被正确计算；
body 之间由后置条件 P1 保证图面不相交，故分组求解与整体一次求解结果等价，可安全分片缓存。
这与 legacy `gen_exploded` 的「逐零件 HLR」不同：后者的等价性前提当年并不成立，
本设计是**先用后置条件把该前提证成，再享用它**。

### 11.4 `layout_assembly` 伪代码

```python
def layout_assembly(pieces):
    """kind='assembly'：零件保持投影原位，offset 全零。
    **不做重叠检查**——总装图里零件本来就互相遮挡，遮挡由 scene_curves 的全局 HLR 负责。
    §5.2 的 no-overlap 后置条件只约束 layout_exploded。"""
    if len(pieces) == 0:
        raise LayoutError("layout_assembly: 零件列表为空")
    pts_all = []
    for pc in pieces:
        if not pc.curves:
            raise LayoutError("layout_assembly: piece %s 无可见曲线" % pc.key)
        arr = np.vstack(pc.curves)
        if not np.all(np.isfinite(arr)):
            raise LayoutError("layout_assembly: piece %s 的曲线含 NaN/Inf" % pc.key)
        pts_all.append(arr)
        pc.offset = np.zeros(2)

    lo = np.array([min(float(a[:, 0].min()) for a in pts_all),
                   min(float(a[:, 1].min()) for a in pts_all)])
    hi = np.array([max(float(a[:, 0].max()) for a in pts_all),
                   max(float(a[:, 1].max()) for a in pts_all)])
    SCALE_REF = float(np.hypot(hi[0] - lo[0], hi[1] - lo[1]))
    if not (SCALE_REF > 0.0):
        raise LayoutError("layout_assembly: 全体几何退化为一点")

    ext, boxes = [], []
    for i, a in enumerate(pts_all):
        w = float(a[:, 0].max() - a[:, 0].min())
        hgt = float(a[:, 1].max() - a[:, 1].min())
        if max(w, hgt) < DEGEN_REL * SCALE_REF:
            raise LayoutError("layout_assembly: 退化零件 %s；请在 plan 的 source.exclude 里排除"
                              % pieces[i].key)
        ext.append(max(w, hgt))
        boxes.append({"key": pieces[i].key,
                      "lo": [float(a[:, 0].min()), float(a[:, 1].min())],
                      "hi": [float(a[:, 0].max()), float(a[:, 1].max())],
                      "members": [pieces[i].key]})

    diag = float(np.hypot(hi[0] - lo[0], hi[1] - lo[1]))
    return LayoutResult(
        pieces=pieces, lo=lo, hi=hi,
        slot=min(min(ext), SLOT_QA_CAP_K * diag / max(len(pieces), 1)),
        rows=1,
        diagnostics={"strategy": "in-place", "gap": 0.0, "overlaps": 0,
                     "scale_ref": SCALE_REF, "bodies": len(pieces), "rows": 1,
                     "body_boxes": boxes,
                     "extent_by_key": {pieces[i].key: ext[i] for i in range(len(pieces))},
                     "fill_usable": None, "axis_angle_opt": None, "dropped": []})
```

### 11.5 `fit_to_frame` 伪代码

**【架构师裁决 D5】§5.2 描述的 `s = min(s_fit, s_target)` 中的 `s_target` 分支在本节被废止，
`margin` 的默认值由 0.06 改为 0.0。** 三个理由，缺一不可：

1. `s_target` 把几何面积钉死在 `TARGET_OCCUPANCY = 0.62` 的可用区。而 §11.1 裁决 D1 已证明
   `geometry_occupancy` 是**尺度不变**的——等比缩放不改变它分毫。所以 `s_target` **改善不了
   任何一道 QA 闸门**，只会白白把线性比例尺压掉 `sqrt(0.62) = 21%`，进而把字高压掉 21%。
   实测：N=20 时 `0.45×slot` 由 3.83 降到 3.02，直接跌破 `TEXT_FLOOR_MM = 3.5`。
2. `margin=0.06` 是双重内缩：`FRAME_W×FRAME_H = 180×250` 本身已经是「A4 减 15 mm 边距」的可用区，
   而标记边槽与图题带由调用方按 §11.6 从 `frame_w/frame_h` 里**显式扣除**后再传进来。
   两者叠加会把可用面积压到 77.4%，几何再也够不到纸的边。
3. `s_target` 存在的目的（给标记留出余量）已经由 §11.6 的 `label_margin_mm/caption_band_mm` 承担，
   而且承担得更准：那是按**当前字号**算出来的实际需求，不是一个拍出来的 0.62。

```python
def fit_to_frame(result, frame_w=FRAME_W, frame_h=FRAME_H, margin=0.0):
    """返回等比缩放因子 s，使几何包围盒恰好装进 (frame_w, frame_h) 内缩 margin 之后的区域。
    **不修改 pieces**，由调用方施加。
    调用方契约：frame_w/frame_h 必须已经是扣掉标记边槽与图题带之后的可用区，margin 传 0.0。"""
    Gw = float(result.hi[0] - result.lo[0])
    Gh = float(result.hi[1] - result.lo[1])
    if not (Gw > 0.0 and Gh > 0.0) or not (math.isfinite(Gw) and math.isfinite(Gh)):
        raise LayoutError("fit_to_frame: 几何包围盒退化（%.6g × %.6g）" % (Gw, Gh))
    aw = frame_w * (1.0 - 2.0 * margin)
    ah = frame_h * (1.0 - 2.0 * margin)
    if aw <= 0.0 or ah <= 0.0:
        raise LayoutError("fit_to_frame: margin=%.3f 把可用区吃光了" % margin)
    return round(min(aw / Gw, ah / Gh), RANK_DEC)
```

### 11.6 `text_height_for` 的推导与字高求解

#### 11.6.1 冻结函数原样保留

```python
def text_height_for(slot: float, ratio: float = TEXT_RATIO) -> float:
    """§5.3 冻结：slot * ratio。QA 要求 height/slot <= 0.6，0.45 留 25% 余量。
    注意这是**原始**字高，还没有吸附到 GB 字号系列，也没有做绝对下限检查。"""
    return slot * ratio
```

#### 11.6.2 三个 slot 必须区分清楚（三个候选设计都在这里接错了线）

| 名字 | 定义 | 谁用 |
| --- | --- | --- |
| `LayoutResult.slot` | `min(min over **all** pieces of max(hi-lo), SLOT_QA_CAP_K*diag/N)` | 契约字面值 + 双口径夹紧，写进报告与诊断，**不直接喂给 text_height_for** |
| `slot_labelled`（模型单位） | `min over pieces **that carry a numeral** of max(hi-lo)`，由 renderer 从 `diagnostics["extent_by_key"]` 与 `Numbering` 交叉算出 | **喂给 `text_height_for` 的就是它 × s** |
| `slot_qa`（qa.py 观测） | 见 §11.10 对 qa.py 的修订要求 | qa.py |

理由：附图标记的字高只需相对**它所指的那个零件**可读。一个 `label:"none"` 的垫圈不该把全图数字压小。
契约自带的 `label` 三态就是为这个场景准备的逃生阀。这是对 §5.2 `slot` 语义的显式偏离，
必须写进实现报告的 `deviations`，并在 `diagnostics["slot_labelled"]` 留痕供 qa 交叉核对。

#### 11.6.3 字高的降序扫描（消除 h ↔ 边槽 ↔ s ↔ h 的循环依赖）

`h` 决定边槽与图题带宽 ⇒ 决定 `aw/ah` ⇒ 决定 `s` ⇒ 决定 `slot_sheet` ⇒ 决定 `h`。这是个环。
因为 `h` 的取值域是**有限有序集** `TEXT_SERIES`，且映射 `h ↦ snap(0.45 * slot_sheet(h))`
关于 `h` 单调非增，**降序扫描取第一个自洽值**即为最大可行解：至多 3 步，必然终止，
**不会像浮点不动点那样在两个值之间振荡**。

```python
def label_margin_mm(h):   return min(LABEL_MARGIN_K * h, LABEL_MARGIN_CAP_MM)
def caption_band_mm(h):   return min(CAPTION_K * h, CAPTION_CAP_MM)

def snap_text_height(raw_mm: float) -> float | None:
    """吸附到 GB/T 14691 字号系列中不超过 raw 的最大值；低于 TEXT_FLOOR_MM 时返回 None。"""
    for v in TEXT_SERIES:                     # 降序 (7.0, 5.0, 3.5)
        if v <= raw_mm + 10 ** (-RANK_DEC):
            return v
    return None


def solve_figure(pieces, axis2d, density, labelled_keys, n_labels, figure_id, kind):
    """render_patent_figure.py 内。返回 (result, s, h)。
    kind='assembly' 时把 layout_exploded 换成 layout_assembly（其余步骤完全相同：
    总装图同样要填满纸、同样要保证字高 >= 3.5mm）。"""
    last, fill_fail = None, None
    if not labelled_keys:
        # 整图零标记（全部 label:"none"）：字高仍需定，用全体件的最小外廓兜底
        labelled_keys = frozenset(pc.key for pc in pieces)
    for h in TEXT_SERIES:                                   # 7.0 -> 5.0 -> 3.5
        lm, cb = label_margin_mm(h), caption_band_mm(h)
        aw, ah = FRAME_W - 2.0 * lm, FRAME_H - lm - cb
        if aw <= 0.0 or ah <= 0.0:
            continue
        result = (layout_assembly(pieces) if kind == "assembly" else
                  layout_exploded(pieces, axis2d, density, sheet_aspect=aw / ah, max_rows=1))
        s = fit_to_frame(result, aw, ah, margin=0.0)

        # (1) 页面填充闸门（layout 判不了，它不知道毫米）
        # 分母是**可用区**，不是整张 FRAME。见 §11.1 裁决 D2。
        Gw = float(result.hi[0] - result.lo[0]) * s
        Gh = float(result.hi[1] - result.lo[1]) * s
        sheet_fill = round(Gw * Gh / (aw * ah), RANK_DEC)

        # (2) 字高自洽
        ext = result.diagnostics["extent_by_key"]
        slot_lab_model = min(ext[k] for k in sorted(labelled_keys))    # sorted，不遍历 set
        slot_sheet = s * slot_lab_model
        raw = text_height_for(slot_sheet)                              # = 0.45 * slot_sheet
        h_snap = snap_text_height(raw)
        floor_override = False
        if h_snap is None and round(slot_sheet, RANK_DEC) >= round(SLOT_FLOOR_MM, RANK_DEC):
            # floor override：0.45*slot 已经低于 3.5，但 3.5/slot 仍 <= 0.6，
            # 也就是 QA 的 text_slot_ratio 仍然过关。用满这段包络，不浪费。
            h_snap, floor_override = TEXT_FLOOR_MM, True
        last = (h, sheet_fill, slot_sheet, raw, result.diagnostics["axis_angle_opt"])
        if h_snap is not None and h <= h_snap + 10 ** (-RANK_DEC):
            if sheet_fill < SHEET_FILL_MIN:
                # 不当场抛：更小的字号档可用区更大、alpha 略有不同，留一线可能。
                # 记下第一个「除了填充之外都合格」的档，扫描结束后再报。
                if fill_fail is None:
                    fill_fail = (sheet_fill, Gw, Gh,
                                 result.diagnostics["axis_angle_opt"],
                                 result.diagnostics["axis_angle_deg"])
                continue
            result.diagnostics["slot_labelled"] = slot_lab_model
            result.diagnostics["text_height_mm"] = h
            result.diagnostics["text_height_floor_override"] = floor_override
            result.diagnostics["sheet_fill"] = sheet_fill
            return result, s, h

    if fill_fail is not None:
        sf, Gw, Gh, opt, cur = fill_fail
        # γ = Gw/Gh 与 α 的失配是旋转能不能救的关键：opt 与 cur 相差不到 ANGLE_GRID_DEG 时，
        # 说明当前角已经是最优（典型：近正方形几何，长宽比旋转不变），只能拆图或换 view。
        rotatable = (kind != "assembly" and opt is not None
                     and abs(float(opt) - float(cur)) >= ANGLE_GRID_DEG)
        fix = ("把 layout.axis_angle 改为 %s（当前 %s，本图闭式最优）；或 density 改 compact；"
               % (opt, cur)) if rotatable else \
              ("本图几何的长宽比旋转也救不回来（当前角已是最优）：换一个 layout.view；")
        raise LayoutError(
            "figure %s: 页面填充 %.3f < %.2f（几何 %.1f×%.1f mm，长宽比 %.3f，可用区长宽比 %.3f）。"
            "修复（按推荐顺序）：%s或按 assembly.json:split_suggestions 拆图。"
            % (figure_id, sf, SHEET_FILL_MIN, Gw, Gh, Gw / Gh,
               (FRAME_W - 2.0 * label_margin_mm(TEXT_FLOOR_MM))
               / (FRAME_H - label_margin_mm(TEXT_FLOOR_MM) - caption_band_mm(TEXT_FLOOR_MM)),
               fix))

    # 三档都不自洽 ⇒ 这张图装不下，必须拆
    if last is None:
        raise LayoutError("figure %s: 任何 GB 字号档下可用区都为空（FRAME 常量被改坏了？）"
                          % figure_id)
    h0, fill0, slot0, raw0, axis_opt0 = last
    raise LayoutError(
        "figure %s: 本图最大可用字高 %.2f mm < 交付下限 %.1f mm"
        "（被标注件最小图面外廓 %.2f mm，需 >= %.2f mm）。"
        "本图含 %d 个零件实例 / %d 个附图标记——**标记数没有超上限，是件数超了**，"
        "所以 E_TOO_MANY_LABELS 不会触发。修复（按推荐顺序）："
        "(a) 对标准件设 label:\"none\"；(b) 按 assembly.json:split_suggestions 拆图；"
        "(c) density 改 compact；(d) axis_angle 改为 %s。"
        % (figure_id, raw0, TEXT_FLOOR_MM, slot0, SLOT_FLOOR_MM,
           len(pieces), n_labels, axis_opt0))
```

#### 11.6.4 为什么 `text_height/slot <= 0.6` 是**构造性**成立的

`h = snap(0.45 * slot_sheet) ≤ 0.45 * slot_sheet`，故比值 ≤ 0.45 < 0.6，与零件数、纸张、
`axis_angle` 全部无关。走 floor override 分支时 `h = 3.5` 且 `slot_sheet ≥ 3.5/0.6`，
故比值 ≤ 0.6，仍然成立。**这道闸门不可能失败**——这也正是它没有判别力、必须补
`text_height_mm_min` 与 `sheet_fill` 两条的原因（§11.1 裁决 D2/D3）。

### 11.7 `place_labels` 伪代码

**坐标系：毫米图纸坐标。** `requests` 的 `lo/hi`、`obstacles`、`sheet_lo/sheet_hi` 都已由 renderer
乘过 `s` 并平移进图框。`sheet_lo/sheet_hi` 是整个 `FRAME`（180×250），不是几何可用区——
标记允许落进边槽，几何不允许。

```python
# ============================== labels.py ==============================
from __future__ import annotations
import math
import numpy as np

class LabelError(RuntimeError):
    """必须携带 unplaced: list[int]（标记号）与 tried: int（每个标记试过的候选数），§5.3 要求。"""
    def __init__(self, msg, unplaced, tried):
        super().__init__(msg); self.unplaced = unplaced; self.tried = tried


# 12 个候选方向，模块导入时算好并 round 到 12 位：
# libm 的 sin/cos 误差在 1 ULP（~1e-16）量级，round 到 1e-12 后被完全吸收 ⇒ 任何平台同一组常量。
# 相位 15° 使候选角为 15/45/…/345，没有一个是 0° 或 90°（GB/T 4457.2），
# 同时保证每个方向的两个分量都非零，_ray_exit_aabb 不需要除零分支。
_DIRS = tuple(
    (round(math.cos(math.radians(PHASE_DEG + 360.0 * k / N_DIRS)), DIR_ROUND_DEC),
     round(math.sin(math.radians(PHASE_DEG + 360.0 * k / N_DIRS)), DIR_ROUND_DEC))
    for k in range(N_DIRS))


# ---------------- 几何谓词：全部在这里定义，不留给实现者发明 ----------------
def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def _seg_cross(p0, p1, q0, q1, eps):
    """真交叉。共享端点、共线重合都判**不**交叉（引线起点本就贴在几何上）。
    四次定向谓词，符号判定前先 round —— 这是零交叉不变量的地基，不许改写。"""
    d1 = round(_cross(p1[0]-p0[0], p1[1]-p0[1], q0[0]-p0[0], q0[1]-p0[1]), RANK_DEC)
    d2 = round(_cross(p1[0]-p0[0], p1[1]-p0[1], q1[0]-p0[0], q1[1]-p0[1]), RANK_DEC)
    d3 = round(_cross(q1[0]-q0[0], q1[1]-q0[1], p0[0]-q0[0], p0[1]-q0[1]), RANK_DEC)
    d4 = round(_cross(q1[0]-q0[0], q1[1]-q0[1], p1[0]-q0[0], p1[1]-q0[1]), RANK_DEC)
    return ((d1 > 0.0) != (d2 > 0.0)) and ((d3 > 0.0) != (d4 > 0.0))


def _box_overlap(alo, ahi, blo, bhi, eps):
    return (round(alo[0], RANK_DEC) < round(bhi[0] - eps, RANK_DEC) and
            round(blo[0], RANK_DEC) < round(ahi[0] - eps, RANK_DEC) and
            round(alo[1], RANK_DEC) < round(bhi[1] - eps, RANK_DEC) and
            round(blo[1], RANK_DEC) < round(ahi[1] - eps, RANK_DEC))


def _box_inside(blo, bhi, slo, shi, eps):
    return (round(blo[0], RANK_DEC) >= round(slo[0] - eps, RANK_DEC) and
            round(blo[1], RANK_DEC) >= round(slo[1] - eps, RANK_DEC) and
            round(bhi[0], RANK_DEC) <= round(shi[0] + eps, RANK_DEC) and
            round(bhi[1], RANK_DEC) <= round(shi[1] + eps, RANK_DEC))


def _seg_hits_box(p0, p1, blo, bhi, eps):
    """线段与 AABB 是否相交（含端点在框内）。先端点测试，再与四条边做 _seg_cross。"""
    for pt in (p0, p1):
        if (blo[0] - eps <= pt[0] <= bhi[0] + eps and blo[1] - eps <= pt[1] <= bhi[1] + eps):
            return True
    c = ((blo[0], blo[1]), (bhi[0], blo[1]), (bhi[0], bhi[1]), (blo[0], bhi[1]))
    for k in range(4):
        if _seg_cross(p0, p1, c[k], c[(k + 1) % 4], eps):
            return True
    return False


def _ray_exit_aabb(c, d, lo, hi):
    """射线 c + t*d（t>0）离开 AABB 的交点。d 的两个分量都非零（PHASE_DEG=15 保证）。"""
    tx = (hi[0] - c[0]) / d[0] if d[0] > 0.0 else (lo[0] - c[0]) / d[0]
    ty = (hi[1] - c[1]) / d[1] if d[1] > 0.0 else (lo[1] - c[1]) / d[1]
    t = min(tx, ty)
    return (c[0] + d[0] * t, c[1] + d[1] * t)


# ============================================================================
def place_labels(requests, *, obstacles, text_height, sheet_lo, sheet_hi):
    h = float(text_height)
    if not (h > 0.0):
        raise LabelError("place_labels: text_height 必须为正，收到 %r" % (text_height,), [], 0)
    if not requests:
        return []
    EPS = EPS_SHEET_REL * float(np.hypot(sheet_hi[0] - sheet_lo[0], sheet_hi[1] - sheet_lo[1]))

    # ---------- L0 障碍索引：均匀网格 ----------
    # 不建索引也能得到**正确**答案，但 90 件全局 HLR 可产出 1e5 条线段，
    # 20 标记 × 48 候选 × 1e5 = 1e8 次线段测试，单图渲染从亚秒掉到分钟级。索引是必需项。
    SEG = []                                          # 按 obstacles 的下标序压入，list 不是 set
    for arr in obstacles:
        a = np.asarray(arr, dtype=np.float64)
        if a.ndim != 2 or a.shape[0] < 2:
            continue
        for t in range(a.shape[0] - 1):
            SEG.append((float(a[t, 0]), float(a[t, 1]), float(a[t+1, 0]), float(a[t+1, 1])))
    CELL = GRID_CELL_K * h
    GRID = {}                                         # dict 只做点查（get），**从不遍历**
    for si, (x0, y0, x1, y1) in enumerate(SEG):
        for gx in range(int(math.floor(min(x0, x1) / CELL)), int(math.floor(max(x0, x1) / CELL)) + 1):
            for gy in range(int(math.floor(min(y0, y1) / CELL)), int(math.floor(max(y0, y1) / CELL)) + 1):
                GRID.setdefault((gx, gy), []).append(si)

    def _box_hits_geometry(blo, bhi):
        for gx in range(int(math.floor(blo[0] / CELL)), int(math.floor(bhi[0] / CELL)) + 1):
            for gy in range(int(math.floor(blo[1] / CELL)), int(math.floor(bhi[1] / CELL)) + 1):
                for si in GRID.get((gx, gy), ()):     # list，下标序；存在性查询与顺序无关
                    x0, y0, x1, y1 = SEG[si]
                    if _seg_hits_box((x0, y0), (x1, y1), blo, bhi, EPS):
                        return True
        return False

    # ---------- L1 首选方向 ----------
    # 图形质心用固定顺序的 Python sum，不用 np.mean（成对归约的分块随 numpy 版本变）
    cx = sum(round(0.5 * (float(r.lo[0]) + float(r.hi[0])), RANK_DEC) for r in requests) / len(requests)
    cy = sum(round(0.5 * (float(r.lo[1]) + float(r.hi[1])), RANK_DEC) for r in requests) / len(requests)

    def _pref_dir(r):
        """首选方向：背离图形质心（向外）。若调用方给了 anchor_hint，改用它相对件中心的方向——
        这让 anchor_hint 真正参与决策，而不是被当装饰。"""
        c = (0.5 * (float(r.lo[0]) + float(r.hi[0])), 0.5 * (float(r.lo[1]) + float(r.hi[1])))
        if r.anchor_hint is not None:
            vx, vy = float(r.anchor_hint[0]) - c[0], float(r.anchor_hint[1]) - c[1]
        else:
            vx, vy = c[0] - cx, c[1] - cy
        n = math.hypot(vx, vy)
        if n <= EPS:                                   # 件正好在质心上：取 +x，确定
            return (1.0, 0.0), c
        return (vx / n, vy / n), c

    # ---------- L2 候选生成 ----------
    # 候选次序 = (方向按与首选方向的偏离升序, 圈层由内向外)，候选下标即最终并列决胜键。
    def _candidates(r):
        (px, py), c = _pref_dir(r)
        lo = (float(r.lo[0]), float(r.lo[1])); hi = (float(r.hi[0]), float(r.hi[1]))
        # 方向排序键：(-cosΔ 升序 = 偏离升序, 方向下标)。全序，无并列。
        order = sorted(range(N_DIRS),
                       key=lambda k: (round(-(px * _DIRS[k][0] + py * _DIRS[k][1]), RANK_DEC), k))
        # anchor_hint 只覆盖偏离最小的那一个方向的锚点（它是唯一能保证落在真实轮廓上的点）
        snap_dir = order[0]
        ndig = len(str(int(r.numeral)))
        tw = CHAR_W * h * ndig
        out = []
        for rank, k in enumerate(order):
            d = _DIRS[k]
            base = _ray_exit_aabb(c, d, lo, hi)
            if r.anchor_hint is not None and k == snap_dir:
                anchor = (float(r.anchor_hint[0]), float(r.anchor_hint[1]))
            else:
                anchor = base
            dth = math.acos(max(-1.0, min(1.0, px * d[0] + py * d[1])))     # 与首选方向的夹角
            for j in range(RING_COUNT):
                dist = RING_BASE_K * (RING_GROWTH ** j) * h
                elbow = (base[0] + d[0] * dist, base[1] + d[1] * dist)
                sgn = 1.0 if round(d[0], RANK_DEC) > 0.0 else -1.0           # **逐候选**判断落脚朝向
                land = (elbow[0] + sgn * LAND_K * h, elbow[1])               # 水平基准线（GB 惯例）
                tx = land[0] + sgn * RUNOUT_K * h                            # 数字骑在基准线**之外**
                x0 = tx if sgn > 0.0 else tx - tw
                blo = (x0 - PAD_X * h, land[1] - 0.5 * h - PAD_Y * h)
                bhi = (x0 + tw + PAD_X * h, land[1] + 0.5 * h + PAD_Y * h)
                out.append({
                    "idx": rank * RING_COUNT + j,      # 生成序下标 = 决胜键
                    "req": r, "dir": k, "ring": j, "dth": dth,
                    "anchor": anchor, "elbow": elbow, "land": land,
                    "text_pos": (tx, land[1]),
                    "align": "left" if sgn > 0.0 else "right",
                    "blo": blo, "bhi": bhi,
                    "llen": math.hypot(elbow[0]-anchor[0], elbow[1]-anchor[1]) + LAND_K * h,
                })
        return out

    CAND = [_candidates(r) for r in requests]          # 每个请求 12*4 = 48 个候选
    TRIED = N_DIRS * RING_COUNT

    # ---------- L3 静态硬否决（与落位次序无关，因此也决定了贪心次序）----------
    def _static_ok(cd, i):
        if not _box_inside(cd["blo"], cd["bhi"], sheet_lo, sheet_hi, EPS):
            return False                                       # 出图框
        if _box_hits_geometry(cd["blo"], cd["bhi"]):
            return False                                       # 数字压几何
        for j, rq in enumerate(requests):
            if j == i:
                continue
            rlo = (float(rq.lo[0]), float(rq.lo[1])); rhi = (float(rq.hi[0]), float(rq.hi[1]))
            if _box_overlap(cd["blo"], cd["bhi"], rlo, rhi, EPS):
                return False                                   # 数字落在别的零件框里
            # GB：引线不得穿过另一个被标注零件
            if _seg_hits_box(cd["anchor"], cd["elbow"], rlo, rhi, EPS):
                return False
        return True

    STATIC = [[cd for cd in CAND[i] if _static_ok(cd, i)] for i in range(len(requests))]

    # ---------- L4 动态硬否决（与已落位者的关系）----------
    def _dyn_ok(cd, placed):
        for q in placed:
            if _box_overlap(cd["blo"], cd["bhi"], q["blo"], q["bhi"], EPS):
                return False                                   # 数字框两两不重叠
            for A, B in ((cd["anchor"], cd["elbow"]), (cd["elbow"], cd["land"])):
                for C, D in ((q["anchor"], q["elbow"]), (q["elbow"], q["land"])):
                    if _seg_cross(A, B, C, D, EPS):
                        return False                           # 引线互不交叉
                # §5.3 明文：obstacles 随贪心循环增长，**已落位的数字框也是障碍**。
                # 新引线不得穿过旧数字框：
                if _seg_hits_box(A, B, q["blo"], q["bhi"], EPS):
                    return False
            for C, D in ((q["anchor"], q["elbow"]), (q["elbow"], q["land"])):
                # 旧引线不得穿过新数字框（另一个方向，同样要查）：
                if _seg_hits_box(C, D, cd["blo"], cd["bhi"], EPS):
                    return False
        return True

    # ---------- L5 打分（全部换算成「等价于多少 h 的引线长度」的显式汇率）----------
    def _score(cd, placed):
        s = W_LEN * (cd["llen"] / h)
        s += W_DIR * (1.0 - math.cos(cd["dth"]))
        infl_lo = (cd["blo"][0] - CLEAR_K * h, cd["blo"][1] - CLEAR_K * h)
        infl_hi = (cd["bhi"][0] + CLEAR_K * h, cd["bhi"][1] + CLEAR_K * h)
        if _box_hits_geometry(infl_lo, infl_hi):
            s += W_CLR                                          # 擦边
        bx = 0.5 * (cd["blo"][0] + cd["bhi"][0]); by = 0.5 * (cd["blo"][1] + cd["bhi"][1])
        for q in placed:
            qx = 0.5 * (q["blo"][0] + q["bhi"][0]); qy = 0.5 * (q["blo"][1] + q["bhi"][1])
            if abs(bx - qx) < CROWD_R_K * h and abs(by - qy) < CROWD_R_K * h:
                s += W_CROWD
        return round(s, RANK_DEC)

    # ---------- L6 贪心次序：最受约束者优先 ----------
    # 键只依赖静态可行数与零件自身，**与落位结果无关** ⇒ 可复现，且显著降低回退触发率。
    order = sorted(range(len(requests)), key=lambda i: (
        len(STATIC[i]),
        round(float((requests[i].hi[0]-requests[i].lo[0]) * (requests[i].hi[1]-requests[i].lo[1])),
              RANK_DEC),
        requests[i].numeral))

    # ---------- L7 贪心 + 有界回退 ----------
    # banned[i] 是「不许再选的候选**下标**」的集合，只做成员测试，从不遍历。
    # 用下标而不是候选对象：candidate 里装着元组/浮点，用 list.index 反查会在候选前缀相同时
    # 触发 numpy 数组的真值歧义，且依赖 dict 比较的短路顺序 —— 那是靠巧合成立的写法。
    banned = {i: set() for i in range(len(requests))}
    chosen = {}
    for attempt in range(MAX_REPAIR + 1):
        chosen, placed, failed = {}, [], []
        for i in order:
            best = None
            for cd in STATIC[i]:                       # 生成序，下标即决胜键
                if cd["idx"] in banned[i]:
                    continue
                if not _dyn_ok(cd, placed):
                    continue
                key = (_score(cd, placed), cd["idx"])
                if best is None or key < best[0]:
                    best = (key, cd)
            if best is None:
                failed.append(i)
            else:
                chosen[i] = best[1]; placed.append(best[1])
        if not failed:
            break
        if attempt == MAX_REPAIR:
            raise LabelError(
                "place_labels: 附图标记 %s 无法落位（每个已试 %d 个候选位，回退 %d 轮）。"
                "修复：对标准件设 label:\"none\"；或按 assembly.json:split_suggestions 拆图。"
                % (sorted(requests[i].numeral for i in failed), TRIED, MAX_REPAIR),
                unplaced=sorted(requests[i].numeral for i in failed), tried=TRIED)
        # 回退：找出真正挡住第一个失败者的那些已落位标记，禁掉它们**当前**的候选下标。
        # 判据是可判定的：把 j 拿掉之后，失败者是否至少多出一个可行候选。
        f = failed[0]
        progressed = False
        for j in sorted(chosen):                       # sorted，不用 dict 插入序
            rest = [chosen[q] for q in sorted(chosen) if q != j]
            if any(cd["idx"] not in banned[f] and _dyn_ok(cd, rest) for cd in STATIC[f]):
                if chosen[j]["idx"] not in banned[j]:
                    banned[j].add(chosen[j]["idx"]); progressed = True
        if not progressed:
            raise LabelError(
                "place_labels: 附图标记 %d 无解且无可禁的阻挡者——这张图过载。"
                "修复：对标准件设 label:\"none\"；或按 assembly.json:split_suggestions 拆图。"
                % requests[f].numeral, unplaced=[requests[f].numeral], tried=TRIED)
        # banned 单调增长且候选表有限 ⇒ 必终止

    # ---------- L8 后置条件复验（抓实现错误，不是抓算法）----------
    ks = sorted(chosen)
    if len(ks) != len(requests):
        raise LabelError("place_labels: 仅落位 %d/%d 个标记（内部错误）"
                         % (len(ks), len(requests)),
                         unplaced=sorted(requests[i].numeral for i in range(len(requests))
                                         if i not in chosen), tried=TRIED)
    for x in range(len(ks)):
        a = chosen[ks[x]]
        if _box_hits_geometry(a["blo"], a["bhi"]):
            raise LabelError("后置条件破坏：标记 %d 的数字框压住几何"
                             % requests[ks[x]].numeral, [requests[ks[x]].numeral], TRIED)
        if not _box_inside(a["blo"], a["bhi"], sheet_lo, sheet_hi, EPS):
            raise LabelError("后置条件破坏：标记 %d 越出图框"
                             % requests[ks[x]].numeral, [requests[ks[x]].numeral], TRIED)
        for y in range(x + 1, len(ks)):
            b = chosen[ks[y]]
            if _box_overlap(a["blo"], a["bhi"], b["blo"], b["bhi"], EPS):
                raise LabelError("后置条件破坏：标记 %d/%d 的数字框重叠"
                                 % (requests[ks[x]].numeral, requests[ks[y]].numeral),
                                 [requests[ks[x]].numeral, requests[ks[y]].numeral], TRIED)
            for A, B in ((a["anchor"], a["elbow"]), (a["elbow"], a["land"])):
                for C, D in ((b["anchor"], b["elbow"]), (b["elbow"], b["land"])):
                    if _seg_cross(A, B, C, D, EPS):
                        raise LabelError("后置条件破坏：标记 %d/%d 的引线交叉"
                                         % (requests[ks[x]].numeral, requests[ks[y]].numeral),
                                         [requests[ks[x]].numeral, requests[ks[y]].numeral], TRIED)
                if _seg_hits_box(A, B, b["blo"], b["bhi"], EPS):
                    raise LabelError("后置条件破坏：标记 %d 的引线穿过标记 %d 的数字框"
                                     % (requests[ks[x]].numeral, requests[ks[y]].numeral),
                                     [requests[ks[x]].numeral, requests[ks[y]].numeral], TRIED)
            for C, D in ((b["anchor"], b["elbow"]), (b["elbow"], b["land"])):
                if _seg_hits_box(C, D, a["blo"], a["bhi"], EPS):
                    raise LabelError("后置条件破坏：标记 %d 的引线穿过标记 %d 的数字框"
                                     % (requests[ks[y]].numeral, requests[ks[x]].numeral),
                                     [requests[ks[x]].numeral, requests[ks[y]].numeral], TRIED)

    # ---------- L9 输出，按 numeral 升序（与图层写入顺序解耦）----------
    out = []
    for i in sorted(ks, key=lambda t: requests[t].numeral):
        cd = chosen[i]
        out.append(LabelPlacement(
            numeral=requests[i].numeral, key=requests[i].key,
            text_pos=(round(cd["text_pos"][0], RANK_DEC), round(cd["text_pos"][1], RANK_DEC)),
            text_align=cd["align"],
            leader=[(round(cd["anchor"][0], RANK_DEC), round(cd["anchor"][1], RANK_DEC)),
                    (round(cd["elbow"][0], RANK_DEC),  round(cd["elbow"][1], RANK_DEC)),
                    (round(cd["land"][0], RANK_DEC),   round(cd["land"][1], RANK_DEC))],
            score=_score(cd, [chosen[q] for q in ks if q != i])))
    return out
```

#### 11.7.1 为什么「引线零交叉」是不变量而不是统计结果

`_dyn_ok` 把「与任一已落位引线相交」以及「穿过任一已落位数字框」都列为**硬否决**，
回退分支同样调用 `_dyn_ok`，所以最终集合里不可能存在交叉对。
**因此不需要 2-opt 交换阶段，也不需要论证它收敛**——候选设计里的相邻交换修复没有严格递减
不变量，可能在可解输入上跑满 `n²` 轮然后抛错。L8 的复验只用来抓实现错误。

#### 11.7.2 `anchor_hint` 与锚点悬空（诚实说明残留缺陷）

`LabelRequest` 只有 `lo/hi`，`obstacles` 又不归属零件，所以在没有 `anchor_hint` 时，
锚点只能取射线与零件 AABB 的交点——对凹形件（L 形、U 形）它可能悬在零件本体之外。
这是 legacy `gen_exploded.py` L120 的老毛病在冻结签名下的残留。缓解：

- **renderer 必须提供 `anchor_hint`**，配方固定为：取该件放置后自身可见曲线的全部采样点，
  选沿「件中心 − 图形质心」方向投影最大者；并列时取 `(round(x,9), round(y,9))` 字典序最小的点。
  该点必然落在真实轮廓上。
- `place_labels` 用它做两件事：定首选方向；并作为「偏离最小的那个方向」的锚点。
  其余方向仍走 AABB 射线（它们本来就是回退级候选，被选中的概率低）。
- `anchor_hint is None` 时，renderer 必须在 `report.open_issues` / `diagnostics` 里标记为降级。
- 修掉的 legacy 缺陷：L118 的 `abs((hi-lo)@perp)/2` 在 perp 两分量异号时坍缩到真值的 7%
  （锚点掉进零件内部）——本设计不再用包围盒代数推锚点；
  L123-129 的落脚线朝向判据恒等于 `perp[0] <= 0`（全图同向）——本设计的 `sgn` 逐候选判断；
  L134 把数字放在落脚线中点却用端对齐（文字压线）——本设计放在端点外 `RUNOUT_K*h` 处。

#### 11.7.3 `sheet.py` 侧的配合

- `leader` 的三个点写成一条 LWPOLYLINE，图层 `LEADER`，CONTINUOUS。
- 锚点处画 GB/T 4457.2 的小圆点（实心点，直径 `0.15*h`）——该常数属于 `sheet.py`，
  在那里定义，不进 `labels.py`。
- 数字用 `MIDDLE_LEFT` / `MIDDLE_RIGHT` 对齐，插入点即 `text_pos`；
  `text_align == "left"` ⇒ `MIDDLE_LEFT`。
- 斜引线永远不会是水平或竖直（`PHASE_DEG=15`）；水平的那一段是**基准线**，
  GB 惯例如此，不是违规。

### 11.8 不变量与异常

| # | 不变量 | 在哪里检查 | 违反时抛什么 | 消息里必须带 |
| --- | --- | --- | --- | --- |
| P0 | 零横向位移：任一 piece 的 `offset · p == 0` | `layout_exploded` S8 | `LayoutError` | piece key |
| P1 | body 之间图面包围盒两两不重叠（容差 `OVERLAP_TOL_REL*SCALE_REF`） | `layout_exploded` S8 | `LayoutError` | 重叠对数 + 首例两个 body key；注明「这是实现错误」 |
| P2 | 数字框两两不重叠 | `place_labels` L4 构造 + L8 复验 | `LabelError` | 两个 numeral |
| P3 | 数字框不压任何几何障碍 | L3 构造 + L8 复验 | `LabelError` | numeral |
| P4 | 数字框在 `sheet_lo/sheet_hi` 内 | L3 构造 + L8 复验 | `LabelError` | numeral |
| P5 | 引线两两不交叉 | L4 构造 + L8 复验 | `LabelError` | 两个 numeral |
| P5b | 引线不穿过**任何已落位的数字框**（§5.3 明文：obstacles 含已提交的引线与数字框） | L4 构造 + L8 复验 | `LabelError` | 两个 numeral |
| P6 | 引线不穿过另一个被标注零件的 AABB | L3 构造 | 无（候选被否决） | — |
| P7 | 页面填充 `sheet_fill >= SHEET_FILL_MIN` | `solve_figure`（renderer） | `LayoutError` | 实测 fill、几何 mm 尺寸、γ 与 α、`axis_angle_opt`、可执行的 plan 编辑 |
| P8 | 字高 `>= TEXT_FLOOR_MM` | `solve_figure`（renderer） | `LayoutError` | 最大可用字高、被标注件最小外廓、**件数与标记数并列**、四条 plan 编辑 |

**失败模式清单（每一条都必须有出口，不许静默降级）：**

| 输入 | 行为 |
| --- | --- |
| `pieces` 为空 / `density` 未知 / `sheet_aspect <= 0` | `LayoutError`，点名参数 |
| `max_rows != 1` | `LayoutError`，说明二维排布已废止 + 三条替代动作。**绝不降级成一维串** |
| 某 piece `curves` 为空（`occ_backend` 三处 `except Exception: pass` 会静默丢曲线） | `LayoutError`，点名 key，提示 exclude 或换 view。不当成零尺寸件塞进布局 |
| 曲线含 NaN/Inf | `LayoutError`，点名 key。**必须在排布前拦**：NaN 让所有比较返回 False，重叠检查会「假通过」 |
| 退化件（外廓 < `DEGEN_REL × SCALE_REF`） | `LayoutError`，列出全部 key，提示 `source.exclude`。legacy 在此处抛的是无信息的 `max() arg is an empty sequence` |
| `|axis2d| < AXIS_MIN_NORM` | `LayoutError`，报实测模长与阈值，提示改 view / explode_axis |
| 全体几何退化为一点 | `LayoutError` |
| `sheet_fill < 0.55` 且旋转能救（`axis_angle_opt` 与当前角差 ≥ 1°） | `LayoutError`（P7），首选建议改 `axis_angle`。**修复建议永远不准是横向位移** |
| `sheet_fill < 0.55` 但当前角已是最优（近正方形几何，长宽比旋转不变；`kind='assembly'` 常见） | `LayoutError`（P7），建议换 `layout.view` 或拆图，**不得推荐改 axis_angle**（那是一个什么都改变不了的编辑，会让用户原地打转） |
| 被标注件最小外廓 < `SLOT_FLOOR_MM` | `LayoutError`（P8），必须明说「标记数没超、件数超了，E_TOO_MANY_LABELS 不会触发」 |
| 标记落不下 | `LabelError(unplaced=[...], tried=48)`；调用方转 `E_LABELS_UNPLACEABLE`，hint 指向 `assembly.json:split_suggestions` |
| 同名实例跨工位被误并成一个 body | 由 S4 的**轴向区间聚类**规避。实现者若把它简化成「同名即一体」，壳体上下各 4 颗同型螺钉会并出一个横跨壳体的包围盒，位移荒谬——这是必现故障点 |
| `axis_angle` 落在 `[120,180]` 但闭式 `θ*` 无解（串太胖） | 不报错，`_closed_form_best_angle` 退回 1° 定步长扫描。**不要去解 `Gw/Gh = alpha` 的等式**：它在 `L/V = 1/alpha` 处奇异，会在合法输入上误抛 |

### 11.9 确定性检查清单（实现者逐条勾选）

- [ ] **1** 全代码无 `random` / `time` / `uuid` / `id()` / 对象 `hash()`。所有「选择」都是对**有限有序候选表**的下标扫描（`TEXT_SERIES` 3 档、`CAND[i]` 48 条、角度栅格 61 步）。
- [ ] **2** 没有一处裸的 `for k in some_dict` 或 `for x in some_set`。`GRID` 只做 `.get()` 点查；`banned` 只做 `in` 成员测试；`chosen` 每次遍历都写 `for j in sorted(chosen)`；零件名遍历写 `sorted({...})`；`labelled_keys` 遍历写 `sorted(...)`。
- [ ] **3** 每个排序键都是**全序**，末位是全局唯一量（`piece.key` / `body.key` / `numeral` / 候选下标 `idx`）。逐一核对：body 内实例排序 `(Q(alo), Q(ahi), key)`；bodies 排序 `(Q(ac), Q(alo), key)`；标记次序 `(len(STATIC), Q(area), numeral)`；候选择优 `(score, idx)`；方向排序 `(Q(-cosΔ), k)`。
- [ ] **4** 比较前一律量化。`layout.py` 用相对算子 `Q(v) = round(v/SCALE_REF, 9)`（包括 P0 断言、body 聚类合并判据、`_sep_required` 的 `t <= 0` 分支、P1 的包围盒相交判定）；`labels.py` 用 `round(v, 9)` 配 `EPS = 1e-9 × 图框对角`（包括 `_seg_cross` 的四个叉积符号）。**没有一处裸浮点相等或大小比较。**
- [ ] **5** 没有写过 `P @ e`。所有 (M,2)·(2,) 投影都走 `_dot2` 的逐元素形式（BLAS gemv 的分块与线程数随机器变，浮点加法不结合 ⇒ 末位会变）。同理不对大数组用 `np.sum` / `np.mean`；需要求和的地方用 Python `sum()` 对**已 round 的 float 列表**按固定顺序累加。
- [ ] **6** 没有用 `np.median`（introselect 分区路径随版本变）。中位数走 `_lower_median`：`sorted(...)[(n-1)//2]`，偶数长度**不做**两值平均。
- [ ] **7** 三角函数只在两处出现：`_DIRS` 常量（模块导入时 `round` 到 12 位）与 `_closed_form_best_angle`（结果 `round` 到 9 位再比较/输出）。`min`/`max`/`sqrt`/`hypot` 与四则运算是 IEEE-754 正确舍入的，不需要处理。
- [ ] **8** 若用到 `np.argsort` / `np.sort`，一律显式 `kind="stable"`（§7 规则 5，numpy 2.0.2 默认 quicksort 不稳定）。`np.argmax` 并列返回最小下标是文档保证的，但取值前先 `np.round`。
- [ ] **9** 所有循环都**有界且定次**：`TEXT_SERIES` 3 档；角度栅格 `int((180-120)/1)+1 = 61` 步；回退 `MAX_REPAIR+1 = 4` 轮且 `banned` 单调增长；没有任何 `while` 与容差退出判据（容差退出会因浮点累积在不同 CPU 上多跑或少跑一轮）。
- [ ] **10** 影响输出的常量全部在模块顶部命名定义，函数体内无裸数字（`0/1/2/0.5` 这类结构常数除外），且每个都能在 §11.2 找到反解来源。
- [ ] **11** 写进 `Piece.offset` 与 `LabelPlacement` 的坐标 `round` 到 9~12 位，而 `normalized_digest` 只保留 6 位 ⇒ 至少 3 个数量级的安全带。
- [ ] **12** **上游依赖已锚定**：本模块的确定性建立在 `PartShape.key` 稳定之上（§5.1：`load_assembly` 按 `(name, path, round(c,6))` 排序**之后**才分配 `instance_index`）。若上游没做到，本模块也保不住。写进 `open_issues`，并在测试里锚定。
- [ ] **13** `tests/test_layout.py` 断言 **bitwise** 相等，不是 `allclose`：同一输入在同进程跑两次 + 子进程跑一次，`np.array([p.offset for p in pieces]).tobytes()` 三者完全相同。
- [ ] **14** `tests/test_layout.py` 加**输入置换测试**：把 `pieces` 列表做 5 种置换（`key` 不变）重跑，要求输出逐位相同。这才验证得了第 3 条的全序键真的把插入序兜死了——重复跑同一输入验证不了。
- [ ] **15** `tests/test_labels.py` 同样做两条：100 次重跑序列化结果字符串完全相同；`requests` 列表置换后结果不变。

### 11.10 对冻结条目的偏离与对其它模块的修订要求（架构师裁决汇总）

实现 agent 必须把下表逐条抄进报告的 `deviations`。

| # | 条目 | 冻结原文 | 本节裁定 | 理由 |
| --- | --- | --- | --- | --- |
| 1 | `fit_to_frame` 的 `s_target` | §5.2 `s = min(s_fit, s_target)` | **废止 `s_target`，`s = s_fit`** | `geometry_occupancy` 尺度不变，`s_target` 改善不了任何闸门，只把字高压掉 21% 并跌破 3.5 mm 下限。见 §11.5 |
| 2 | `fit_to_frame` 的 `margin` 默认值 | §5.2 `margin = 0.06` | **改为 `0.0`**，调用方必须传 0.0 | 180×250 已经是可用区；边槽与图题带由 §11.6 显式扣除。双重内缩后几何再也够不到纸的边 |
| 3 | `LayoutResult.slot` | §5.2 `min over pieces of max(hi-lo)` | **`min(该值, SLOT_QA_CAP_K*diag/N)`**；另在 `diagnostics["slot_labelled"]` 上报「被标注件的最小外廓」，**由后者驱动字高** | 一颗 `label:"none"` 的垫圈不该把全图数字压小；双口径夹紧让两种可能的 qa 实现都过闸 |
| 4 | `layout_exploded` 的 `max_rows` | §5.2 默认 1，语义为上限 | **只接受 1，其它值抛 `LayoutError`** | §11.1 裁决：不实现二维排布，且绝不静默降级 |
| 5 | 重叠容差 | §5.2 `1e-6` | **`1e-6 × SCALE_REF`**（相对量） | `layout.py` 拿的是模型单位，绝对容差在 1e5 量级坐标上失效、在 1e-3 量级坐标上过度合并 |
| 6 | `part_bbox_overlap` 的粒度 | §5.5 逐 piece | **逐 body**；body 内成员允许重叠 | 强行拆散螺栓圈会把它抹成一条直线，对称性彻底丢失。见 §11.3 S8 |
| 7 | `layout.axis_angle` 默认值 | §4.2 示例 `152` | **改为 `124`** | §11.1 裁决 D4，闭式 `θ*`。152° 下 `sheet_fill` 只有 0.380 |

**对 `qa.py`（§5.5）的修订要求**（另一个实现 agent 的输入）：

1. `geometry_occupancy` 的口径钉死为 `area(GEOM∪HIDDEN 包围盒) / area(全部模型空间实体包围盒)`
   （裁决 D1，与失败基线的 0.1353 逐位吻合）。qa 实现者若采用别的口径，**必须报出不一致**，不许沉默。
2. **新增** `sheet_fill_min = 0.55`：`area(GEOM∪HIDDEN 包围盒) / (aw × ah)`（裁决 D2），
   其中 `h` 取自 `NUM` 图层 TEXT 实体的字高，`aw = 180 - 2*label_margin_mm(h)`、
   `ah = 250 - label_margin_mm(h) - caption_band_mm(h)`，两个函数与 §11.6 同定义。
   **分母不要取整张 FRAME**：正方形几何在任何字号档下都只能得到 0.487~0.513，会结构性地
   过不了闸门，而它的长宽比旋转不变，hint 里的「改 axis_angle」什么也改变不了。
   这是唯一一条真正度量「有没有填满纸」的闸门；`geometry_occupancy` 尺度不变，度量不了它。
3. **新增** `text_height_mm_min = 3.5`：直接读 `NUM` 图层 TEXT 实体的字高（裁决 D3）。
   没有这一条，`text_slot_ratio` 是自指的（`h ≡ 0.45·slot`），永远不会因为字太小而报警。
4. `text_slot_ratio` 的 slot 观测口径改为：沿每条 `LEADER` 的第一个点（锚点）找出它所在的
   **几何连通簇**，取该簇外廓的 `max(w, h)` 作为该标记的 slot，再取全体最小。
   这才是「标记相对它所指的零件是否可读」的正确度量，且完全可从 DXF 算出。
5. `part_bbox_overlap` 读 renderer 写出的 sidecar `out/<figure id>.layout.json`
   （内容 = `diagnostics["body_boxes"]` 缩放平移到毫米后的副本）；sidecar 缺失时退回几何连通簇聚类，
   并在 `detail` 里注明用的是退化口径。**不要试图从 DXF 曲线猜零件边界。**
6. **新增** `leader_hits_numeral_box_max = 0`：任一 `LEADER` 折线段不得与任一 `NUM` 文字的
   外接框相交（§5.3 明文把已提交的数字框列为 obstacles，§11.7 的 `_dyn_ok` 已构造性保证）。

**对 `plan.py`（§5.6）的修订要求**：~~`apply_defaults` 里 `axis_angle` 的默认值改为
`AXIS_ANGLE_DEFAULT = 124`（裁决 D4）~~ —— **已被 §4.2 的架构裁决取代：默认值是字符串
`"auto"`，由 renderer 逐图闭式求解 `axis_angle_opt`。** 理由见 §4.2：最优角依赖串的长宽比，
是图的属性而不是常数，把它固定成任何数字都等于让模型抄一个脚本已经算出来的版面常数。
`AXIS_ANGLE_DEFAULT = 124` 仍保留在 §11.2 常数表中，但只作为闭式解无解时的兜底起点。
夹紧区间 `[120,180]` 与 `W_CLAMPED` 对显式数值仍然不变。

**待核对项（写进 `open_issues`，不阻塞实现）**：`TEXT_FLOOR_MM = 3.5` 的规范依据为
GB/T 14691 字号系列 + CNIPA 对附图缩小后清晰度的要求，落地前需按 `iteration-plan-v2.md` §7.5
的保留核对现行《专利审查指南》原文。若该下限被证伪为 2.5 mm，§11.1 的件数上限由 29 升到约 41，
裁决方向不变（一维串更加够用）。


---

## 11.11 Anchor correction (2026-09-07, from real-assembly use)

§11.7.2 says the anchor must "really sit on the outline". The implementation achieved that for
exactly one direction — the snap direction, fed by `anchor_hint`. Every other candidate direction
anchored on `_ray_exit_aabb`, a point on the **bounding box**. For a ring, an L or any concave
part that point is in empty space, and the placer reaches those directions on every collision
repair. Measured on a 131-instance assembly: 29 of 40 leaders attached more than 0.5 mm off the
geometry, 20 of them more than 2 mm, worst case 28.8 mm — 12% of the sheet diagonal, i.e. leaders
visibly pointing at blank paper.

**Fix.** `LabelRequest` gains `outline` (the part's own sampled placed curves). Every candidate
direction now anchors at `_outline_anchor(outline, d)`: the outline point with the largest
projection on `d`, tie-broken lexicographically — the same recipe §11.7.2 already specified,
merely parameterised by direction instead of computed once. Being an extreme point it lies on the
convex hull, hence on the silhouette, for any part shape.

**What was tried and reverted.** Moving the elbow to `anchor + d*dist` as well, so the leader root
would run along `d`. It relocates the numeral box to wherever the part happens to reach furthest
along `d`; in a dense assembly view that lands the box on other geometry, and numeral 3 of the
synthetic assembly lost all 48 candidates (24 to `box_on_geom`). The elbow therefore stays on the
AABB ray-exit: **where a leader attaches and where its numeral sits want different reference
points.** A test asserting the along-`d` property was deleted with it — it pinned an implementation
detail, not a requirement.

**New gate.** `leader_anchor_gap_ratio_max = 0.01` of the sheet diagonal (§5.5). It is deliberately
not zero: in an assembly view a labelled part may be partly occluded, so its true silhouette anchor
can sit ~0.8% of the diagonal from the nearest *drawn* line. Snapping to the nearest visible line
would be worse — it can land on a neighbour and name the wrong part. Such figures now report the
gap instead of shipping silently; the honest fix is to label that part in an exploded figure.

---

## 12. Cross-model contract audit (2026-09-03)

Before implementation started, this contract was audited by five models from five vendors, each
given the identical adversarial brief: *falsify the claim that any competent LLM reading this
document produces the same behaviour.* Reports are outside the repo (they quote nothing
confidential, but they are working notes, not deliverables).

| Runtime | Model | Report |
| --- | --- | --- |
| CodeBuddy | GLM-5.1 (智谱) | 25 KB |
| CodeBuddy | Kimi-K2.5 (月之暗面) | 22 KB |
| CodeBuddy | DeepSeek-V3.2 | 26 KB |
| CodeBuddy | MiniMax-M2.7 | 27 KB |
| Codex CLI | GPT-5.x | **did not return** — hung at 0% CPU with no output after 4h25m and was killed |

**Acceptance rule: a finding counted only if ≥2 models raised it independently, or if one model
raised it and it was reproduced against the actual library.** Every library claim below was
re-verified locally rather than taken on trust — one of them turned out to be wrong.

| Finding | Models | Verified | Fix |
| --- | --- | --- | --- |
| §11 empty — the two hardest algorithms unspecified | 4/4 | known | pending the design review |
| `load_assembly` sort-key rounding precision unspecified | 4/4 | yes | fixed at `round(v, 6)`, §5.1 |
| `normalized_digest` sort key and rounding under-specified | 4/4 | yes | full recipe written out, §5.4 |
| zero-overlap postconditions unsatisfiable on dense input | 4/4 | mathematically | escape hatch made explicit, §5.2/5.3 |
| `fit_to_frame` target occupancy undefined | 3/4 | yes | ~~`TARGET_OCCUPANCY = 0.62`~~ — **superseded by §11.10 #1**: the area target was abolished entirely. `geometry_occupancy` is scale-invariant, so capping the scale could not improve it and only pushed text height 21% down through the 3.5 mm floor. Page filling is now the separate `sheet_fill` gate. |
| `np.argsort` default is not stable | 3/4 | `quicksort` | §7 rule 5 strengthened |
| `roll_for_axis` algorithm unspecified, legacy compares raw floats | 3/4 | yes | closed form specified, §5.1 |
| `DENSITY` values have no stated semantics | 3/4 | yes | defined against median part extent, §5.2 |
| OCCT traversal order not guaranteed | 3/4 | plausible | already covered by the §5.1 sort; rationale added |
| `$ACADVER` is not a volatile header var | 2/4 | `'AC1032'` | removed from `VOLATILE_HEADER_VARS` |
| `fnmatch` is case-sensitive per platform | 2/4 | yes | `fnmatchcase` mandated, §7 rule 6 |
| `split_suggestions` "always emitted" contradicts its own cap | 1/4 | logical | rewritten, §4.1 |
| `label: "once"` — which instance gets the numeral | 1/4 | yes | first by sorted key, §4.2 |
| `$INSUNITS` defaults to 6 (metres), not absent | 1/4 | `6` | `write_figure` must set 4, §5.4 |
| `list[int]` works at runtime on Python 3.9 | 1/4 | yes | §1 relaxed |
| **`jsonschema` is not installed** | 1/4 | **false — 4.25.1 is installed** | rejected |

Codex produced nothing. It was launched against the contract as it stood before the algorithm
section landed, ran for four and a half hours, and sat at 0% CPU with an empty output buffer until
it was killed. Its findings are simply absent from the table above; four models is what this audit
actually rests on, not five.

The last row is the reason for the verify-locally rule. A single model's confident factual claim
about the environment was simply wrong; had it been actioned, the plan validator would have been
written around a dependency that was there all along.

Two failure modes of the harness itself are worth recording, because they cost a full round:
`codebuddy --disallowedTools` is variadic and swallowed the prompt that followed it, producing four
empty reports with exit code 0 — a silent success. Use the single-valued `--tools` instead. And the
first local verification script called `importlib.metadata` without importing it, which is what
produced the bogus "jsonschema missing" reading on the first pass.
