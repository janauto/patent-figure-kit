#!/usr/bin/env python3
"""专利方法流程图：flowchart.json（语义）→ DXF（+ PNG 预览）。

    python3 scripts/render_flowchart.py flow.json -o out/flow1.dxf --figure-number 6 --preview

flowchart.json 只写语义（schema ``patent-flowchart/1``）::

    {"schema": "patent-flowchart/1",
     "title": "箱体温度控制方法的流程图",
     "step_style": "S101",
     "nodes": [{"id": "a", "kind": "start", "text": "开始"},
               {"id": "b", "kind": "process", "text": "采集箱体内部温度"},
               {"id": "c", "kind": "decision", "text": "温度是否高于设定值？"},
               {"id": "d", "kind": "process", "text": "启动风扇降温"},
               {"id": "e", "kind": "end", "text": "结束"}],
     "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "c"},
               {"from": "c", "to": "d", "label": "是"},
               {"from": "c", "to": "b", "label": "否"},
               {"from": "d", "to": "e"}]}

``kind`` ∈ start / end / process / decision / io。步骤号、坐标、尺寸、图号不写——
脚本按流程顺序发 S101、S102…（``step_style: "S1"`` 则发 S1、S2…）。

``--check`` 只校验不出图。退出码：0 成功；1 校验失败或出图失败；2 用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from patent_figure import flowchart as FC  # noqa: E402
from patent_figure import sheet as _sheet  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", type=Path, help="flowchart.json")
    ap.add_argument("-o", "--out", type=Path, help="输出 DXF 路径")
    ap.add_argument("--figure-number", type=int, help="图号 N；不给则图面写 title")
    ap.add_argument("--preview", action="store_true", help="同时出 PNG 预览")
    ap.add_argument("--check", action="store_true", help="只校验，不出图")
    ap.add_argument("--json", type=Path, help="把结果（含步骤号对照）写成 JSON")
    args = ap.parse_args(argv)
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("用法错误：读不到 %s（%s）" % (args.spec, exc), file=sys.stderr)
        return 2
    issues = FC.validate(spec)
    errors = [i for i in issues if i["severity"] == "error"]
    for i in issues:
        print("[%s] %s  %s" % ("错误" if i["severity"] == "error" else "警告", i["code"],
                               i["message"]))
        if i.get("hint"):
            print("    修复：" + i["hint"])
    if errors:
        if args.json:
            args.json.write_text(json.dumps({"ok": False, "issues": issues},
                                            ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    if args.check:
        print("校验通过：%d 个节点、%d 条连线" % (len(spec["nodes"]), len(spec["edges"])))
        return 0
    if not args.out:
        print("用法错误：出图需要 -o", file=sys.stderr)
        return 2
    result = FC.write(spec, args.out, args.figure_number)
    if args.preview:
        png = args.out.with_suffix(".png")
        _sheet.render_preview(args.out, png)
        result["preview"] = str(png)
    result["ok"] = True
    result["issues"] = issues
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("%s：%s，%d 个步骤（%s）" % (args.out.name, result["caption"], len(result["steps"]),
                                   "、".join(s["step"] for s in result["steps"])))
    for w in result["warnings"]:
        print("警告：" + w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
