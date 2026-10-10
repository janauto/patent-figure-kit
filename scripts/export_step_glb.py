#!/usr/bin/env python3
"""Export a STEP assembly as a named-node binary glTF for the Plan Studio 3D picker.

Usage:
    python3 scripts/export_step_glb.py ASM.stp -o model.glb

Preview-only: the GLB feeds the browser picker and nothing in the deterministic render
chain reads it. Node names carry the STEP component names, which is what plans address
parts by. Exit 0 on success, 1 on export failure, 2 on usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patent_figure.occ_backend import OccBackendError, export_glb  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    args = ap.parse_args()
    if not args.step.is_file():
        print("用法错误：STEP 文件不存在：%s" % args.step, file=sys.stderr)
        return 2
    try:
        out = export_glb(args.step, args.output)
    except OccBackendError as exc:
        print("导出失败：%s" % exc, file=sys.stderr)
        return 1
    print("%s  %d bytes" % (out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
