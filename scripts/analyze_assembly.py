#!/usr/bin/env python3
"""STEP 装配体 -> assembly.json（impl-contract v2 §4.1、§6）。

用法：

    python3 scripts/analyze_assembly.py ASM.stp -o assembly.json \\
            [--include GLOB]... [--exclude GLOB]... [--max-labels N]

输出的 assembly.json 供 figure-plan 的作者阅读：它给出主轴方向、同轴组、沿轴装配序、
尺寸分档，以及在零件数超过每图标记上限时的拆图建议（split_suggestions）。
本工具只读几何，不产生任何版面常数。

退出码：0 成功；1 分析失败或输出不符合 schema；2 参数用法错误。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patent_figure.analyze import (  # noqa: E402
    AnalyzeError,
    DEFAULT_MAX_LABELS,
    build_assembly,
    validate_assembly,
    write_assembly,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="analyze_assembly.py",
        description="读取 STEP 装配体，生成 assembly.json（patent-assembly/1）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 scripts/analyze_assembly.py ASM.stp -o assembly.json\n"
               "  python3 scripts/analyze_assembly.py ASM.stp -o assembly.json "
               "--include 'SYN-*' --exclude '*SCREW*'\n")
    p.add_argument("step", help="输入 STEP 装配体路径")
    p.add_argument("-o", "--out", required=True, help="输出 assembly.json 路径")
    p.add_argument("--include", action="append", default=[], metavar="GLOB",
                   help="只保留名称或装配路径匹配该 glob 的零件，可重复；区分大小写")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="排除名称或装配路径匹配该 glob 的零件，可重复；区分大小写")
    p.add_argument("--max-labels", type=int, default=DEFAULT_MAX_LABELS, metavar="N",
                   help="每张图的标记上限，用于生成 split_suggestions（默认 %d，"
                        "与 figure-plan 的 layout.max_labels_per_figure 默认值一致）"
                        % DEFAULT_MAX_LABELS)
    p.add_argument("--no-validate", action="store_true",
                   help="跳过输出文档的 schema 校验（默认校验，不通过则退出码 1）")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    step = Path(args.step)
    if not step.is_file():
        print("用法错误：找不到 STEP 文件 %s" % (step,), file=sys.stderr)
        return EXIT_USAGE
    if args.max_labels < 1:
        print("用法错误：--max-labels 必须 >= 1，收到 %d" % (args.max_labels,),
              file=sys.stderr)
        return EXIT_USAGE

    try:
        doc = build_assembly(step, include=args.include, exclude=args.exclude,
                             max_labels_per_figure=args.max_labels)
    except AnalyzeError as exc:
        print("分析失败：%s" % (exc,), file=sys.stderr)
        return EXIT_FAIL
    except ImportError as exc:
        print("分析失败：缺少几何后端依赖（%s）。请先安装 cadquery-ocp。" % (exc,),
              file=sys.stderr)
        return EXIT_FAIL
    except (OSError, RuntimeError, ValueError) as exc:
        print("分析失败：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return EXIT_FAIL

    if not args.no_validate:
        errors = validate_assembly(doc)
        if errors:
            for e in errors:
                print(e, file=sys.stderr)
            return EXIT_FAIL

    out = Path(args.out)
    try:
        write_assembly(doc, out)
    except OSError as exc:
        print("写出失败：%s" % (exc,), file=sys.stderr)
        return EXIT_FAIL

    axis = doc["principal_axis"]
    print("已写出 %s" % (out,))
    print("零件实例 %d 个 / 不同零件 %d 个"
          % (doc["source"]["instances"], doc["source"]["distinct"]))
    print("主轴 %s（向量 %s，spread_ratio %.3f）"
          % (axis["nearest"], axis["vector"], axis["spread_ratio"]))
    print("同轴组 %d 个；沿轴装配序 %d 件；拆图建议 %d 条"
          % (len(doc["coaxial_groups"]), len(doc["stack_order"]),
             len(doc["split_suggestions"])))
    for w in doc["warnings"]:
        print("警告：%s" % (w,))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
