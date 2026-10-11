#!/usr/bin/env python3
"""附图导出包命令行入口（F1）：figure-plan → 渲染 + 规范标注（单一发布服务）→ 导出包（单一导出服务）。

    python3 scripts/export_figure_package.py plan.json --assembly assembly.json -o 附图包/ \\
            [--variant both] [--formats pdf,docx,dxf] [--dpi 300] [--preset standard] [--zip 附图包.zip]

做的事与工作台「导出附图包」完全相同，只是没有界面：

1. 在工作目录（缺省 ``<-o>.work/``）里经 ``patent_figure.publish`` 把整套图渲染进不可变版本目录：
   原始出图 → QA 闸门 → 交底版（图号 + 件号表）/ 递交版（仅图号）→ 声明产物清单 → 封口提交。
   任何一张图没过闸 → 不导出，退出码 1，打印每张图的未过闸项。
2. 经 ``patent_figure.export`` 钉住这个版本，生成导出包：**PDF / Word 为主交付**（每个带图号的版本
   各一份，A4，页边 25/25/15/15 mm，图区预设 standard 170×257 / conservative 165×245），写出后对
   文件本身逐页核验（页幅、页边、缩放后最小字高、图号、页数 = 图数、文件齐全）；不通过的文件移到
   ``_未通过核验/``。DXF / DWG 是源文件。另附 ``附图说明.txt``、``核对清单.txt``、``核验报告.json``。

精确图片参数以当期《电子申请文件格式要求说明》为准。

退出码：0 全部请求项通过（DWG 无转换器为 skip，不算失败）；1 渲染未过闸或有导出项 fail；2 用法错误。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from patent_figure import deliverable as _D  # noqa: E402
from patent_figure import export as _EX  # noqa: E402
from patent_figure import project_io as _PIO  # noqa: E402
from patent_figure import publish as _PUB  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="export_figure_package.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("plan", help="figure-plan.json")
    p.add_argument("--assembly", required=True, help="assembly.json")
    p.add_argument("-o", "--out", required=True, help="导出包目录（必须不存在或为空）")
    p.add_argument("--variant", default="both", choices=sorted(_EX.REST_VARIANTS))
    p.add_argument("--formats", default="pdf,docx,dxf",
                   help="逗号分隔：pdf,docx,dxf,dwg,png（缺省 pdf,docx,dxf）")
    p.add_argument("--dpi", type=int, default=_D.DEFAULT_DPI)
    p.add_argument("--preset", default=_D.DEFAULT_PRESET, choices=sorted(_D.SHEET_PRESETS))
    p.add_argument("--zip", default=None, help="另把导出包打成 zip")
    p.add_argument("--workdir", default=None, help="渲染工作目录（缺省 <-o>.work/）")
    p.add_argument("--cache", default=None, help="几何缓存目录（缺省 <workdir>/cache）")
    p.add_argument("--json", dest="json_out", default=None, help="把导出清单与核验结果写成 JSON")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in ("pdf", "docx", "dxf", "dwg", "png")]
    if bad:
        print("用法错误：未知格式 %s" % bad, file=sys.stderr)
        return EXIT_USAGE
    try:
        _D.spec(args.preset, args.dpi)
    except _D.DeliverableError as exc:
        print("用法错误：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    plan_path, asm_path = Path(args.plan).resolve(), Path(args.assembly).resolve()
    out = Path(args.out).resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print("用法错误：-o %s 已存在且不为空（导出包目录必须是新的）" % out, file=sys.stderr)
        return EXIT_USAGE
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        json.loads(asm_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("用法错误：读不了 plan / assembly：%s" % exc, file=sys.stderr)
        return EXIT_USAGE
    raw_step = str((plan.get("source") or {}).get("step", ""))
    step = Path(raw_step)
    if not step.is_file() and (plan_path.parent / raw_step).is_file():
        step = plan_path.parent / raw_step
    if not step.is_file():
        print("用法错误：plan 的 source.step 指向的文件不存在：%s" % raw_step, file=sys.stderr)
        return EXIT_USAGE
    plan.setdefault("source", {})["step"] = str(step.resolve())

    work = Path(args.workdir).resolve() if args.workdir else out.with_name(out.name + ".work")
    work.mkdir(parents=True, exist_ok=True)
    ws = SimpleNamespace(root=work, plan=work / "plan.json", cache=Path(args.cache).resolve()
                         if args.cache else work / "cache")
    _PIO.atomic_write_json(ws.plan, plan)
    shutil.copy2(str(asm_path), str(work / "assembly.json"))

    from patent_figure.svg_preview import dxf_to_svg
    from patent_figure.worker import run_cli_via_worker as run_cli
    rid = "pkg-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), __import__("os").urandom(3).hex())
    snap = _PUB.prepare(ws, rid)
    print("· 渲染 + 规范标注（版本 %s）…" % rid, flush=True)
    res = _PUB.run_and_publish(ws, rid, int(snap["revision"]), run_cli, dxf_to_svg,
                               inputs=snap)
    if not res["ok"]:
        print("渲染未全部过闸，不导出：", file=sys.stderr)
        for f in res["result"].get("figures", []):
            if not f.get("pass"):
                failed = [c.get("title_zh") or c.get("id") for c in (f.get("qa") or {})
                          .get("checks", []) if not c.get("pass")]
                print("  %s：%s" % (f["id"], "、".join(failed) or "未产出（见日志）"),
                      file=sys.stderr)
        print((res["result"].get("log") or "")[-1500:], file=sys.stderr)
        return EXIT_FAIL

    print("· 生成导出包并逐页核验…", flush=True)
    with _EX.acquire(ws.root, rid) as pin:
        bundle = _EX.build_bundle(pin, out, variants=_EX.REST_VARIANTS[args.variant],
                                  formats=formats, dpi=args.dpi, run_cli=run_cli,
                                  sheet_preset=args.preset, description_txt=True,
                                  checklist_txt=True)
    if args.zip:
        _EX.zip_dir(out, Path(args.zip).resolve())
    for it in bundle["items"]:
        print("  [%s] %s %s%s" % ({"ok": "通过", "skip": "跳过", "fail": "不通过"}[it["status"]],
                                 it["variant"], it["format"],
                                 ("：" + it["reason"]) if it["reason"] else ""))
    print("导出包：%s（%s）" % (out, "全部通过" if bundle["ok"] else "有不通过项，见 核对清单.txt"))
    print(_D.CNIPA_NOTE)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {k: bundle[k] for k in ("ok", "manifest", "reasons", "items", "documents",
                                    "missing_files", "files", "spec", "render_job_id")},
            ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return EXIT_OK if bundle["ok"] else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
