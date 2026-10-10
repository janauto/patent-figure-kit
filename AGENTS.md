# AGENTS.md

Entry point for **Codex CLI and any general agent runtime** that discovers this
repository as a working directory rather than as a Claude skill. Claude Code
loads `SKILL.md` directly; this file exists so other runtimes land in the same
place instead of inferring a workflow from the file tree.

**This file is a router. It deliberately contains no procedure.** Anything
describing *how* to do the work lives in exactly one place, and duplicating it
here would let different models drift apart — which defeats the purpose of this
iteration.

## Read in this order

1. **`SKILL.md`** — what this tool does, which route fits which input, and how to
   deliver. This is the workflow.
2. **`docs/impl-contract.md`** — the single source of truth for module
   boundaries, function signatures, JSON shapes, determinism rules, and layout
   algorithms. Where it and any other document disagree, **it wins**.
3. **`references/patent-figure-spec.md`** — the CNIPA form requirements a figure
   must satisfy, with sources. Read before putting any text on a sheet.
4. `references/cad-source-to-drawing.md`, `references/workflow.md` — background
   for the 3D-CAD and non-CAD routes.

## Hard prohibitions

1. **The only artifact a model authors is `figure-plan.json`.** No layout
   constant, coordinate, text height, sheet angle, or numeral may come from a
   model. Everything geometric is computed by the scripts. A number you typed
   into a plan because a script asked for one is a bug in the script, not a fix.
2. **Never write an ad-hoc rendering script.** If the CLI cannot express what you
   need, that is a contract gap: report it, do not route around it. The v1
   incident that this design exists to prevent was exactly a 170-line improvised
   script full of invented layout magic numbers.
3. **No part names, part codes, quantities, dimensions, title blocks, or parts
   tables on a patent figure.** See `references/patent-figure-spec.md` A5 / A10 /
   A20. The parts list belongs in the 说明书·附图说明 text, and the tool already
   emits it as `reference-numerals.json`.
4. **The engineering parts table is opt-in** via `layout.engineering_table: true`,
   and such files are written as `<figure id>_engineering.dxf` — internal review
   copies only, never a filing copy (`docs/impl-contract.md` §8).
5. **Do not modify the frozen files** listed in `docs/impl-contract.md` §2.
6. **Never use a customer STEP file or a real part name in this repository.**
   Fixtures are synthetic (`tests/fixtures/synthetic.stp`, all parts named
   `SYN-*`); real-assembly runs happen outside the repo.
7. **Do not invent a legal citation.** If a clause is not in
   `references/patent-figure-spec.md` section A, it has no 条号. Write
   【需人工核对】 instead.

## Entry commands

`--help` on each CLI is the contract for agents; exit 0 = pass, 1 = failure,
2 = usage error. The command surface is specified in `docs/impl-contract.md` §6.
Run `python3 scripts/doctor.py` first to see which capabilities this machine has.
