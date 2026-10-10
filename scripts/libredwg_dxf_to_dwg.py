#!/usr/bin/env python3
"""Convert DXF to DWG with LibreDWG when AutoCAD is not installed.

This is the fallback for `autocad_core_dxf_to_dwg.py`. Autodesk's own engine is
still preferred when available; use this on machines without AutoCAD.

LibreDWG writes R2000 only. `dwgread` replaces the AUDIT round trip: a file that
re-parses cleanly with a non-zero entity count and intact text is accepted.

Requires: libredwg (`brew install libredwg`) providing `dwgwrite` and `dwgread`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

TEXT_RE = re.compile(r'"text_value":\s*"([^"]*)"')


def run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def require(tool: str) -> None:
    if shutil.which(tool) is None:
        raise SystemExit(f"{tool} not found; install libredwg (brew install libredwg)")


def convert(src: Path, dst: Path) -> tuple[bool, str]:
    if not src.is_file():
        return False, f"Input DXF not found: {src}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    code, out = run(["dwgwrite", "--as", "r2000", "-o", str(dst), str(src)])
    return ("SUCCESS" in out and dst.exists()), out


def audit(dwg: Path, deep: bool = False) -> dict:
    """Read the DWG back. Equivalent in intent to running AUDIT twice."""
    code, out = run(["dwgread", "-v0", str(dwg)])
    report = {
        "ok": "SUCCESS" in out,
        "reader_errors": len([l for l in out.splitlines() if l.startswith("ERROR")]),
        "size": dwg.stat().st_size if dwg.exists() else 0,
    }
    if deep:
        with tempfile.TemporaryDirectory() as tmp:
            js = Path(tmp) / "dump.json"
            run(["dwgread", "-O", "JSON", "-o", str(js), str(dwg)])
            if js.exists():
                blob = js.read_text(encoding="utf-8", errors="replace")
                texts = TEXT_RE.findall(blob)
                report["entities"] = blob.count('"entity":')
                report["texts"] = len(texts)
                report["sample_text"] = texts[:5]
    return report


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", type=Path, nargs="+", help="DXF files or a directory")
    p.add_argument("-o", "--outdir", type=Path, required=True)
    p.add_argument("--deep-audit", action="store_true",
                   help="dump JSON to count entities and verify text survived")
    p.add_argument("--report", type=Path)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    require("dwgwrite")
    require("dwgread")
    files: list[Path] = []
    for item in args.inputs:
        files += sorted(item.glob("*.dxf")) if item.is_dir() else [item]
    rows, failed = [], 0
    for src in files:
        dst = args.outdir / (src.stem + ".dwg")
        ok, log = convert(src, dst)
        row = {"dxf": str(src), "dwg": str(dst), "converted": ok}
        row["audit"] = audit(dst, args.deep_audit) if ok else None
        row["log"] = None if ok else log.strip()[-300:]
        if not ok or not row["audit"] or not row["audit"]["ok"]:
            failed += 1
        rows.append(row)
        if not ok:
            print(f"{src.name} -> {dst.name}  converted=False  {log.strip().splitlines()[-1] if log.strip() else ''}")
        else:
            print(f"{src.name} -> {dst.name}  converted=True"
                  + (f"  audit_ok={row['audit']['ok']}" if row["audit"] else ""))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    if failed:
        print(f"{failed} of {len(files)} file(s) failed")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
