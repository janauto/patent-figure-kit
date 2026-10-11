#!/usr/bin/env python3
"""候选分图（F2-lite）命令行入口：给装配体提若干分图候选，并逐个**真渲染 + 过闸**后给出状态。

    python3 scripts/suggest_figure_plan.py --assembly assembly.json --step ASM.stp \\
            [--plan figure-plan.json] -o WORKDIR [--json candidates.json]

* 候选来源：整机装配图（兜底，允许被闸门拦下）、沿主轴整机分解图、``split_suggestions``、
  每个子装配 / 同轴组一张分解图。每个候选的 ``members`` 是真实零件名 ``{"part": …}``。
* 每个候选在 ``WORKDIR/candidates/results/<run>-c<k>/`` 里实际渲染并过 QA 闸门之后才有状态
  （``pass`` / ``fail`` + 原因 + 建议）；``--no-render`` 只出候选不渲染，状态一律 ``pending``。
* 标准件（名称正则见契约 §14.2，可用 ``--std-pattern/--std-except`` 改）默认 ``label: none``；
  ``--label-standard-parts`` 关掉这条规则。
* 本工具**不修改**任何 figure-plan：把选中的候选写进计划请用工作台「接受候选」
  （``POST /v1/projects/{id}/plan/candidates/accept``）或自己按 ``members`` 编辑计划。

退出码：0 至少一个候选通过（或 ``--no-render``）；1 没有候选通过（``empty_reason`` 已给出）；
2 用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from patent_figure import suggest_plan as SP  # noqa: E402

EXIT_OK, EXIT_EMPTY, EXIT_USAGE = 0, 1, 2


def _blank_plan(assembly: dict, step: str) -> dict:
    return {"schema": "patent-figure-plan/1",
            "source": {"step": step, "include": ["*"], "exclude": []},
            "terms": [{"selector": SP.glob_escape(p["name"]), "term": "", "label": "once"}
                      for p in assembly.get("parts", [])],
            "figures": [{"id": "fig1", "caption": "整体结构示意图", "kind": "assembly",
                         "members": ["*"]}],
            "layout": {"view": "iso", "explode_axis": "auto", "axis_angle": "auto",
                       "density": "normal", "max_labels_per_figure": 20,
                       "engineering_table": False}}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="suggest_figure_plan.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--assembly", required=True, help="assembly.json（analyze_assembly.py 的输出）")
    p.add_argument("--plan", default=None, help="现有 figure-plan.json（取术语与 source 过滤）；缺省用空白计划")
    p.add_argument("--step", default=None, help="STEP 装配体；渲染候选时必需（缺省取 plan.source.step）")
    p.add_argument("--instances", default=None,
                   help="逐实例旁路文件（analyze_assembly.py --instances 的输出）；缺省时若给了 --step "
                        "就现场生成到 WORKDIR，用来识别子装配与同名跨子装配")
    p.add_argument("-o", "--workdir", default=None, help="工作目录（候选版本库 + 几何缓存）；渲染时必需")
    p.add_argument("--cache", default=None, help="几何缓存目录（缺省 WORKDIR/cache）")
    p.add_argument("--no-render", action="store_true", help="只出候选，不试渲染（状态为 pending）")
    p.add_argument("--label-standard-parts", action="store_true", help="标准件也发标记（关闭默认的 label:none）")
    p.add_argument("--std-pattern", default=None, help="标准件正则（缺省见契约 §14.2）")
    p.add_argument("--std-except", default=None, help="标准件例外正则（缺省：传动丝杠/丝杠螺母）")
    p.add_argument("--max-candidates", type=int, default=SP.MAX_CANDIDATES_DEFAULT)
    p.add_argument("--json", dest="json_out", default=None, help="把候选结果写成 JSON")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        assembly = json.loads(Path(args.assembly).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("用法错误：读不了 assembly.json：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    plan = None
    if args.plan:
        try:
            plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print("用法错误：读不了 plan：%s" % exc, file=sys.stderr)
            return EXIT_USAGE
    step = args.step or ((plan or {}).get("source") or {}).get("step") \
        or (assembly.get("source") or {}).get("step")
    if plan is None:
        plan = _blank_plan(assembly, str(step or ""))
    try:
        rule = SP.StandardPartRule(args.std_pattern, args.std_except)
    except ValueError as exc:
        print("用法错误：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    render = not args.no_render
    workdir = Path(args.workdir).resolve() if args.workdir else None
    if render and (workdir is None or not step or not Path(step).is_file()):
        print("用法错误：试渲染需要 -o WORKDIR 与存在的 --step（或加 --no-render）", file=sys.stderr)
        return EXIT_USAGE
    if workdir is not None:
        workdir.mkdir(parents=True, exist_ok=True)

    from patent_figure.worker import run_cli_via_worker as run_cli
    inst_path = Path(args.instances) if args.instances else None
    instances = SP.load_instances(inst_path, assembly) if inst_path else None
    if instances is None and step and Path(step).is_file() and workdir is not None:
        tmp = workdir / "analyze"
        tmp.mkdir(exist_ok=True)
        proc = run_cli([str(SCRIPTS / "analyze_assembly.py"), str(step), "-o",
                        str(tmp / "assembly.json"), "--instances",
                        str(tmp / "assembly.instances.json")])
        if proc.returncode == 0:
            instances = SP.load_instances(tmp / "assembly.instances.json", assembly)
    doc = SP.build_candidates(assembly, plan, instances,
                              label_standard_parts=args.label_standard_parts, rule=rule,
                              max_candidates=args.max_candidates)
    if render:
        from patent_figure.svg_preview import dxf_to_svg
        run_id = "cli-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
        frozen = workdir / "candidates" / ("%s.assembly.json" % run_id)
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_text(json.dumps(assembly, ensure_ascii=False), encoding="utf-8")
        cache = Path(args.cache) if args.cache else workdir / "cache"

        def say(msg, progress):
            print("· %s" % msg, flush=True)

        SP.run_candidates(doc, ws_root=workdir, cache=cache, job_id=run_id,
                          assembly_path=frozen, run_cli=run_cli, svg_of=dxf_to_svg, emit=say)
        from patent_figure import publish as _pub
        _pub.prune_candidates(workdir, protect={run_id})
        doc["run_id"] = run_id
        doc["candidate_store"] = str(workdir / "candidates")
    else:
        SP.finalize(doc)
    out = SP.public(doc)
    for c in out["candidates"]:
        amb = "；".join(a["message"] + "：" + a["part"] for a in c["ambiguous_members"])
        print("%-3s %-4s %-8s %s（%d 件 / %d 标号）%s" % (
            c["id"], c["status"], c["source"]["kind"], c["caption"], len(c["members"]),
            c["labels"], ("  ⚠ " + amb) if amb else ""))
        if c["status"] != "pass":
            print("      原因：%s" % c["reason"])
            if c.get("suggestion"):
                print("      建议：%s" % c["suggestion"])
    if out.get("empty_reason"):
        print("建议为空：%s" % out["empty_reason"])
    for w in out.get("warnings", []):
        print("警告：%s" % w)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    if not render:
        return EXIT_OK
    return EXIT_OK if out["summary"]["pass"] else EXIT_EMPTY


if __name__ == "__main__":
    raise SystemExit(main())
