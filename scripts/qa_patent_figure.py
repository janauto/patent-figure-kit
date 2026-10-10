#!/usr/bin/env python3
"""专利附图 DXF 的可读性 / 合规闸门（impl-contract §4.4 / §5.5 / §11.10）。

用法：
    python3 scripts/qa_patent_figure.py out/fig1.dxf [--kind exploded] [--json qa.json]

退出码：0 全部通过；1 有闸门未通过（或文件读不出来）；2 用法错误。
每一条未通过的闸门都会打印「该改 plan 的哪里」。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from patent_figure.qa import check_figure
except Exception as _exc:                                    # pragma: no cover - env problem
    sys.stderr.write("错误：无法导入 patent_figure.qa（%s）。"
                     "请确认 scripts/patent_figure/qa.py 存在且 ezdxf 已安装。\n" % _exc)
    raise SystemExit(2)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qa_patent_figure.py",
        description="对一张已渲染的专利附图 DXF 跑全部可读性 / 合规闸门，"
                    "输出 qa.json（schema: patent-figure-qa/1）。",
        epilog="闸门清单：geometry_occupancy(>=0.55) sheet_fill(>=0.55) text_height_mm(>=3.5) "
               "text_slot_ratio(<=0.6) label_overlap_pairs(<=0) leader_hits_numeral_box(<=0) "
               "leader_crossing(<=0) part_bbox_overlap_pairs(<=0, 仅 exploded, 读 "
               "<图号>.layout.json) labels_per_figure(<=20) non_numeral_text_ratio(<=0.10) "
               "non_continuous(<=0) forbidden_text(==0)。")
    p.add_argument("dxf", help="要检查的 DXF 路径，例如 out/fig1.dxf")
    p.add_argument("--kind", default=None, choices=["exploded", "assembly"],
                   help="图的类型；assembly 时跳过 part_bbox_overlap_pairs（总装图零件本就遮挡）。"
                        "默认 exploded。")
    p.add_argument("--json", dest="json_out", default=None,
                   help="把机器可读的 qa.json 写到该路径（不传则只打印）。")
    p.add_argument("--quiet", action="store_true", help="只打印未通过的闸门。")
    return p


def _resolve_kind(dxf, explicit):
    """Decide which rule set applies to this sheet.

    `part_bbox_overlap_pairs` is an exploded-only check: in an assembly view the parts are
    SUPPOSED to occlude one another, so applying it there reports a failure that no edit to the
    plan can fix, and a model following the hint would loop. The renderer already records the
    figure kind in the sidecar it writes next to the DXF, so read it from there rather than
    guessing. An explicit --kind always wins; with no flag and no sidecar we fall back to
    "exploded" and say so, because that is the stricter of the two.
    """
    if explicit:
        return explicit, "--kind"
    sidecar = dxf.with_suffix(".layout.json")
    if sidecar.is_file():
        try:
            recorded = json.loads(sidecar.read_text(encoding="utf-8")).get("kind")
        except (ValueError, OSError):
            recorded = None
        if recorded in ("exploded", "assembly"):
            return recorded, "sidecar %s" % sidecar.name
    return "exploded", "默认值（未找到 sidecar，按更严的分解图口径判定）"


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    dxf = Path(args.dxf)
    if not dxf.exists():
        parser.error("找不到文件：%s" % dxf)          # argparse 以退出码 2 结束
    if dxf.is_dir():
        parser.error("%s 是目录，请指定单个 DXF 文件" % dxf)

    kind, kind_src = _resolve_kind(dxf, args.kind)
    report = check_figure(dxf, kind=kind)

    if args.json_out:
        out = Path(args.json_out)
        try:
            if out.parent and not out.parent.exists():
                out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        except OSError as exc:
            sys.stderr.write("错误：写不出 %s（%s）\n" % (out, exc))
            return 2

    checks = report["checks"]
    print("附图 QA：%s（kind=%s，来源：%s）" % (report["file"], kind, kind_src))
    for c in checks:
        if args.quiet and c["pass"]:
            continue
        print("  [%s] %-24s value=%s threshold=%s"
              % ("PASS" if c["pass"] else "FAIL", c["id"], c["value"], c["threshold"]))
        if not c["pass"]:
            print("        实测：%s" % c["detail"])
            print("        修复：%s" % c["hint"])
    print("小结：通过 %d 项，未通过 %d 项 —— %s"
          % (report["summary"]["passed"], report["summary"]["failed"],
             "可以出图" if report["pass"] else "不可出图，请按上面的「修复」改 plan 后重渲"))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
