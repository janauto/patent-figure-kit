#!/usr/bin/env python3
"""Convert DXF to an audited DWG with Autodesk AutoCAD Core Console on macOS."""

from __future__ import annotations

import argparse
import glob
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


DEFAULT_PATTERNS = (
    "/Applications/Autodesk/AutoCAD */AutoCAD *.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole",
    "/Applications/AutoCAD *.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--version",
        default="2018",
        choices=["R14", "2000", "2004", "2007", "2010", "2013", "2018"],
    )
    parser.add_argument("--autocad-core", type=Path)
    parser.add_argument("--language", default="en-US")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def find_core(explicit: Path | None) -> Path:
    if explicit:
        candidate = explicit.expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise SystemExit(f"AutoCAD Core Console not found: {candidate}")
    candidates = []
    for pattern in DEFAULT_PATTERNS:
        candidates.extend(Path(path) for path in glob.glob(pattern))
    if not candidates:
        raise SystemExit("AutoCAD Core Console was not found. Pass --autocad-core explicitly.")
    return sorted(candidates, reverse=True)[0]


def build_script(version: str, temp_output: Path) -> str:
    # The temporary output is intentionally ASCII-only: the macOS prompt can corrupt CJK save paths.
    return "\n".join(
        [
            '(setvar "FILEDIA" 0)',
            "_.AUDIT",
            "_Y",
            "_.SAVEAS",
            version,
            temp_output.as_posix(),
            "_.AUDIT",
            "_Y",
            "_.QSAVE",
            '(setq ss (ssget "_X"))',
            '(princ (strcat "\\nVALIDATION_ENTITY_COUNT=" (itoa (if ss (sslength ss) 0))))',
            '(setq i 0 bad 0)',
            '(if ss (repeat (sslength ss) (setq e (ssname ss i) d (entget e) lt (cdr (assoc 6 d))) (if (and lt (/= (strcase lt) "CONTINUOUS") (/= (strcase lt) "BYLAYER") (/= (strcase lt) "BYBLOCK")) (setq bad (1+ bad))) (setq i (1+ i))))',
            '(princ (strcat "\\nVALIDATION_NONCONTINUOUS_COUNT=" (itoa bad)))',
            "_.QUIT",
            "_N",
            "",
        ]
    )


def last_int(pattern: str, text: str) -> int | None:
    matches = re.findall(pattern, text)
    return int(matches[-1]) if matches else None


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"Input DXF not found: {source}")
    if source.suffix.lower() != ".dxf" or output.suffix.lower() != ".dwg":
        raise SystemExit("Expected an input .dxf and output .dwg")
    if output.exists() and not args.overwrite:
        raise SystemExit(f"Output exists: {output}. Pass --overwrite to replace it.")
    core = find_core(args.autocad_core)

    with tempfile.TemporaryDirectory(prefix="patent_cad_") as temp_dir:
        temp_root = Path(temp_dir)
        temp_output = temp_root / "converted.dwg"
        script = temp_root / "convert.scr"
        script.write_text(build_script(args.version, temp_output), encoding="ascii")
        command = [str(core), "/i", str(source), "/s", str(script), "/l", args.language]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=args.timeout,
            check=False,
        )
        log = completed.stdout
        if completed.returncode != 0 or not temp_output.is_file():
            print(log)
            raise SystemExit(
                f"AutoCAD conversion failed (exit {completed.returncode}); no valid temporary DWG was produced."
            )
        audits = re.findall(r"Total errors found (\d+) fixed (\d+)", log)
        entity_count = last_int(r"VALIDATION_ENTITY_COUNT=(\d+)", log)
        noncontinuous = last_int(r"VALIDATION_NONCONTINUOUS_COUNT=(\d+)", log)
        if not audits or tuple(map(int, audits[-1])) != (0, 0):
            print(log)
            raise SystemExit("The final AutoCAD AUDIT did not report zero errors.")
        if not entity_count:
            print(log)
            raise SystemExit("The converted DWG has no reported entities.")
        if noncontinuous != 0:
            print(log)
            raise SystemExit(f"The converted DWG still has {noncontinuous} non-continuous entities.")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        shutil.move(str(temp_output), str(output))

    print(f"DWG: {output}")
    print(f"AutoCAD Core: {core}")
    print(f"Final AUDIT: errors={audits[-1][0]}, fixed={audits[-1][1]}")
    print(f"Entities: {entity_count}")
    print(f"Non-continuous entities: {noncontinuous}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
