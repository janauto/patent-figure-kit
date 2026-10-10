#!/usr/bin/env python3
"""Validate a figure plan against an assembly description.

    python3 scripts/validate_figure_plan.py plan.json --assembly assembly.json [--json issues.json]

Exit codes (impl-contract §6):

    0  the plan is usable (warnings may still be printed)
    1  the plan is rejected -- at least one issue of severity "error"
    2  usage error: bad arguments, missing plan file, unreadable assembly.json

Every reported issue carries a ``hint`` that names the JSON edit which fixes it, because
editing ``figure-plan.json`` is the only thing the model is allowed to do.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from patent_figure import plan as plan_mod  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate_figure_plan.py",
        description="校验 figure-plan.json：schema + 语义 + 保密闸门。"
                    "每条问题都给出「改 JSON 的哪里」。",
        epilog="退出码：0 通过；1 校验失败；2 用法错误。",
    )
    parser.add_argument("plan", help="figure-plan.json 路径")
    parser.add_argument("--assembly", required=True, help="analyze_assembly.py 产出的 assembly.json")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="把 issues 写成机器可读 JSON 到该路径")
    return parser


def _load_assembly(path: Path) -> dict:
    """Read assembly.json. Raises ValueError with a Chinese message on any problem."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("无法读取 assembly 文件 %s：%s" % (path, exc)) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("assembly.json 不是合法 JSON：第 %d 行第 %d 列 %s"
                         % (exc.lineno, exc.colno, exc.msg)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("parts"), list):
        raise ValueError("assembly.json 顶层必须是对象且带 parts 数组；"
                         "请用 scripts/analyze_assembly.py 重新生成")
    return data


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    plan_path = Path(args.plan)
    assembly_path = Path(args.assembly)

    if not plan_path.is_file():
        print("用法错误：plan 文件不存在：%s" % plan_path, file=sys.stderr)
        return EXIT_USAGE
    if not assembly_path.is_file():
        print("用法错误：assembly 文件不存在：%s" % assembly_path, file=sys.stderr)
        return EXIT_USAGE

    try:
        assembly = _load_assembly(assembly_path)
    except ValueError as exc:
        print("用法错误：%s" % exc, file=sys.stderr)
        return EXIT_USAGE

    issues: List[plan_mod.PlanIssue]
    try:
        raw_plan = plan_mod.load_plan(plan_path)
    except plan_mod.PlanError as exc:
        issues = exc.issues
    else:
        issues = plan_mod.validate(raw_plan, assembly)

    payload = plan_mod.issues_to_json(
        issues, plan_path=str(plan_path), assembly_path=str(assembly_path)
    )
    if args.json_out:
        try:
            _write_json(Path(args.json_out), payload)
        except OSError as exc:
            print("用法错误：无法写出 --json 文件 %s：%s" % (args.json_out, exc), file=sys.stderr)
            return EXIT_USAGE

    failed = payload["summary"]["errors"]
    warned = payload["summary"]["warnings"]

    if issues:
        print(plan_mod.format_issues(issues))
        print("")

    if failed:
        print("校验失败：%d 个错误，%d 个警告。plan 未通过，请按上面的「修复」逐条改 %s。"
              % (failed, warned, plan_path))
        return EXIT_FAIL

    print("校验通过：0 个错误，%d 个警告。" % warned)
    plan = plan_mod.apply_defaults(raw_plan)
    for figure_id, count, cap in plan_mod.effective_label_counts(plan, assembly):
        print("  %s：有效标记 %d / 上限 %d" % (figure_id, count, cap))
    numbering = plan_mod.numbering_for(plan, assembly)
    print("  %s" % numbering.description_zh())
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    except Exception as exc:  # never let a stack trace be the only output (§6)
        print("内部错误：%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        print("这是 validate_figure_plan.py 的实现缺陷，不是 plan 的问题；请连同 plan.json 一并报告。",
              file=sys.stderr)
        sys.exit(EXIT_USAGE)
