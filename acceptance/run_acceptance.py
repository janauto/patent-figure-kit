#!/usr/bin/env python3
"""专利附图工具链 · 自动验收

    python3 acceptance/run_acceptance.py

逐个功能节点跑真实命令、断言客观结果，最后打一张 PASS/FAIL 表。
退出码 0 = 全部通过，1 = 有节点未通过，2 = 无法开始（缺依赖或缺夹具）。

设计原则：**每一条都必须是客观可判定的**。这份脚本是给外部运行时（千问办公等）
自证用的，不能出现「看起来对」这种判据——否则等于让被测方自己给自己打分。

覆盖不到的部分（模型是否遵守禁令、是否自己写脚本）在 acceptance/行为验收.md 里，
那部分只能靠人看对话记录，脚本测不了，也不该假装能测。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
FIXTURE = REPO / "tests" / "fixtures" / "synthetic.stp"
GOLDEN = REPO / "tests" / "fixtures" / "golden_digest.txt"
PY = sys.executable or "python3"

RESULTS: list = []
_T0 = time.time()


class Skip(Exception):
    """节点不适用于本环境（例如没装 AutoCAD），不算失败。"""


def run(args: list, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run([PY] + [str(a) for a in args], capture_output=True,
                          text=True, timeout=timeout, cwd=str(REPO))


def node(node_id: str, title: str):
    """把一个检查函数登记成验收节点。"""
    def deco(fn):
        def wrapped(ctx):
            start = time.time()
            try:
                detail = fn(ctx) or ""
                status, ok = "通过", True
            except Skip as exc:
                detail, status, ok = str(exc), "跳过", None
            except AssertionError as exc:
                detail, status, ok = str(exc) or "断言失败", "未通过", False
            except Exception as exc:  # noqa: BLE001 — 验收脚本要把异常当结果报出来
                detail, status, ok = "%s: %s" % (type(exc).__name__, exc), "未通过", False
            RESULTS.append({"id": node_id, "title": title, "status": status,
                            "ok": ok, "detail": detail,
                            "seconds": round(time.time() - start, 1)})
            return ok
        wrapped._node = (node_id, title)
        return wrapped
    return deco


# ------------------------------------------------------------------ 节点定义


@node("N1", "环境自检 doctor")
def n1(ctx):
    proc = run([SCRIPTS / "doctor.py", "--json"])
    assert proc.returncode in (0, 1), "doctor 退出码异常：%d" % proc.returncode
    report = json.loads(proc.stdout)
    missing = [c["id"] for c in report["checks"]
               if c.get("required") and c["status"] != "ok"]
    assert not missing, "必需依赖缺失：%s（先按 doctor 的修复提示装好）" % "、".join(missing)
    ctx["doctor"] = report
    opt_ok = sum(1 for c in report["checks"] if not c.get("required") and c["status"] == "ok")
    opt_all = sum(1 for c in report["checks"] if not c.get("required"))
    return "必需项全通过；可选项 %d/%d 可用" % (opt_ok, opt_all)


@node("N2", "装配体解析 analyze")
def n2(ctx):
    assert FIXTURE.is_file(), "缺少测试夹具 %s" % FIXTURE
    out = ctx["tmp"] / "assembly.json"
    proc = run([SCRIPTS / "analyze_assembly.py", FIXTURE, "-o", out])
    assert proc.returncode == 0, "analyze 失败：%s" % proc.stderr[-400:]
    data = json.loads(out.read_text(encoding="utf-8"))
    ctx["assembly"] = out
    names = [p["name"] for p in data["parts"]]
    assert len(names) == 8, "零件种数应为 8，实得 %d" % len(names)
    assert sum(p["instances"] for p in data["parts"]) == 11, "实例数应为 11"
    assert data["principal_axis"]["nearest"] == "z", \
        "合成夹具沿 Z 堆叠，主轴应判为 z，实得 %s" % data["principal_axis"]["nearest"]
    try:
        import jsonschema
        schema = json.loads((REPO / "schemas" / "assembly.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(data, schema)
    except ImportError:
        return "8 种 / 11 实例 / 主轴 z（未装 jsonschema，跳过 schema 校验）"
    return "8 种 / 11 实例 / 主轴 z；输出通过 assembly.schema.json"


def _good_plan() -> dict:
    return {
        "schema": "patent-figure-plan/1",
        "source": {"step": str(FIXTURE), "include": ["SYN-*"], "exclude": []},
        "terms": [
            {"selector": "SYN-A01", "term": "底座"},
            {"selector": "SYN-B02", "term": "回转座"},
            {"selector": "SYN-C03", "term": "支撑轴"},
            {"selector": "SYN-D04", "term": "密封圈"},
            {"selector": "SYN-E05", "term": "调整垫片", "label": "once"},
            {"selector": "SYN-F06", "term": "球头", "label": "all"},
            {"selector": "SYN-G07", "term": "上盖"},
            {"selector": "SYN-H08*", "term": "紧固螺钉", "label": "none"},
        ],
        "figures": [
            {"id": "fig1", "caption": "整体结构示意图", "kind": "assembly", "members": ["*"]},
            {"id": "fig2", "caption": "回转组件分解示意图", "kind": "exploded",
             "members": ["SYN-A01", "SYN-B02", "SYN-C03", "SYN-G07"],
             "layout": {"explode_axis": "z"}},
        ],
        "layout": {"view": "iso", "explode_axis": "auto", "axis_angle": "auto",
                   "density": "normal", "max_labels_per_figure": 20,
                   "engineering_table": False},
    }


@node("N3", "计划校验 · 合法计划应通过")
def n3(ctx):
    plan = ctx["tmp"] / "plan.json"
    plan.write_text(json.dumps(_good_plan(), ensure_ascii=False, indent=2), encoding="utf-8")
    ctx["plan"] = plan
    proc = run([SCRIPTS / "validate_figure_plan.py", plan, "--assembly", ctx["assembly"]])
    assert proc.returncode == 0, "合法计划被拒：\n%s" % (proc.stdout + proc.stderr)[-600:]
    return "退出码 0"


@node("N4", "计划校验 · 三类错误必须被拦住")
def n4(ctx):
    """校验器不是摆设：每一类错误都要命中它自己的错误码，且带可执行的修复提示。"""
    cases = [
        ("选择器命不中", "E_SELECTOR_NO_MATCH",
         lambda p: p["terms"].append({"selector": "SYN-NOPE", "term": "不存在的件"})),
        ("术语写成件号", "E_TERM_LOOKS_LIKE_PART_CODE",
         lambda p: p["terms"].__setitem__(0, {"selector": "SYN-A01", "term": "PRT0001-A"})),
        ("标记数超上限", "E_TOO_MANY_LABELS",
         lambda p: p["layout"].__setitem__("max_labels_per_figure", 2)),
    ]
    hit = []
    for label, code, mutate in cases:
        bad = _good_plan()
        mutate(bad)
        path = ctx["tmp"] / ("bad_%s.json" % code)
        path.write_text(json.dumps(bad, ensure_ascii=False, indent=2), encoding="utf-8")
        issues_file = ctx["tmp"] / ("issues_%s.json" % code)
        proc = run([SCRIPTS / "validate_figure_plan.py", path,
                    "--assembly", ctx["assembly"], "--json", issues_file])
        assert proc.returncode == 1, "%s：应判失败（退出码 1），实得 %d" % (label, proc.returncode)
        codes, hints = [], []
        if issues_file.is_file():
            payload = json.loads(issues_file.read_text(encoding="utf-8"))
            for item in payload.get("issues", []):
                codes.append(item.get("code"))
                hints.append(item.get("hint") or "")
        assert code in codes, "%s：期望命中 %s，实得 %s" % (label, code, codes)
        idx = codes.index(code)
        assert hints[idx].strip(), "%s：%s 没有给出修复提示——模型只能靠 hint 改计划" % (label, code)
        hit.append(code)
    return "3/3 命中且均带修复提示：%s" % "、".join(hit)


@node("N5", "出图 render")
def n5(ctx):
    out = ctx["tmp"] / "out"
    proc = run([SCRIPTS / "render_patent_figure.py", ctx["plan"],
                "--assembly", ctx["assembly"], "-o", out, "--cache", ctx["tmp"] / "cache"])
    assert proc.returncode == 0, "渲染失败：\n%s" % (proc.stdout + proc.stderr)[-900:]
    ctx["out"] = out
    dxfs = sorted(out.glob("*.dxf"))
    assert len(dxfs) == 2, "应出 2 张图，实得 %d" % len(dxfs)
    return "出图 2 张：%s" % "、".join(p.name for p in dxfs)


@node("N6", "附图标记 · 发号与说明文本")
def n6(ctx):
    data = json.loads((ctx["out"] / "reference-numerals.json").read_text(encoding="utf-8"))
    nums = [e["numeral"] for e in data["numerals"]]
    assert nums == sorted(nums), "标记号未按顺序发放：%s" % nums
    assert len(set(nums)) == len(nums), "标记号有重复：%s" % nums
    terms = [e["term"] for e in data["numerals"]]
    for t in terms:
        assert not re.search(r"[A-Za-z]{2,}[0-9]{2,}", t), "术语里混进了件号形态：%s" % t
    desc = data.get("description_zh", "")
    assert desc.startswith("附图标记说明"), "说明文本格式不对：%s" % desc[:40]
    for n, t in zip(nums, terms):
        assert ("%d—%s" % (n, t)) in desc, "说明文本缺少 %d—%s" % (n, t)
    return "%d 个标记连续发号；说明文本与标记表逐条一致" % len(nums)


@node("N7", "质量闸门 · 所有图必须全项通过")
def n7(ctx):
    lines = []
    for qa_file in sorted(ctx["out"].glob("*.qa.json")):
        report = json.loads(qa_file.read_text(encoding="utf-8"))
        failed = [c["id"] for c in report["checks"] if not c["pass"]]
        assert report["pass"] and not failed, \
            "%s 未通过：%s" % (qa_file.stem, "、".join(failed))
        lines.append("%s %d 项" % (qa_file.stem.replace(".qa", ""), len(report["checks"])))
    assert lines, "没有找到任何 qa.json"
    return "全部通过（" + "，".join(lines) + "）"


@node("N8", "闸门有效性 · 坏图必须被判 FAIL")
def n8(ctx):
    """闸门自身要能证伪。把一张合格图故意弄坏，闸门若还放行，说明它是摆设。"""
    import ezdxf

    src = ctx["out"] / "fig2.dxf"
    doc = ezdxf.readfile(str(src))
    msp = doc.modelspace()
    dots = [(e.dxf.center[0], e.dxf.center[1]) for e in msp
            if e.dxf.layer == "LEADER" and e.dxftype() == "CIRCLE"]

    def is_anchor(x, y):
        return any(abs(x - cx) < 1e-6 and abs(y - cy) < 1e-6 for cx, cy in dots)

    # 引线是三点折线（锚点 / 肘点 / 基准线端），锚点端与 LEADER 图层的锚点圆同心。
    # 两种实体形态都处理：实测渲染器写 LWPOLYLINE，但别的写法不该让这条验收静默失效。
    moved = 0
    for e in list(msp):
        if e.dxf.layer != "LEADER":
            continue
        kind = e.dxftype()
        if kind == "LWPOLYLINE":
            pts = [(pt[0], pt[1]) for pt in e.get_points()]
            pts[0] = (pts[0][0] - 40.0, pts[0][1] - 40.0)
            e.set_points(pts, format="xy")
            moved += 1
        elif kind == "LINE":
            sx, sy = float(e.dxf.start.x), float(e.dxf.start.y)
            ex, ey = float(e.dxf.end.x), float(e.dxf.end.y)
            if is_anchor(sx, sy):
                e.dxf.start = (sx - 40.0, sy - 40.0, 0.0); moved += 1
            elif is_anchor(ex, ey):
                e.dxf.end = (ex - 40.0, ey - 40.0, 0.0); moved += 1
        elif kind == "CIRCLE":
            cx, cy = float(e.dxf.center.x), float(e.dxf.center.y)
            e.dxf.center = (cx - 40.0, cy - 40.0)
    assert moved > 0, "没能改到引线锚点，无法构造坏图"
    bad = ctx["tmp"] / "broken.dxf"
    doc.saveas(str(bad))

    proc = run([SCRIPTS / "qa_patent_figure.py", bad, "--kind", "exploded"])
    assert proc.returncode == 1, "把引线锚点搬离零件后闸门仍然放行——闸门无效"
    return "引线锚点被搬离 40mm 后闸门判 FAIL（退出码 1）"


@node("N9", "确定性 · 同一计划两次渲染必须逐位相同")
def n9(ctx):
    sys.path.insert(0, str(SCRIPTS))
    from patent_figure.sheet import normalized_digest

    second = ctx["tmp"] / "out2"
    proc = run([SCRIPTS / "render_patent_figure.py", ctx["plan"],
                "--assembly", ctx["assembly"], "-o", second,
                "--cache", ctx["tmp"] / "cache2"])
    assert proc.returncode == 0, "第二次渲染失败"
    digests = {}
    for name in ("fig1.dxf", "fig2.dxf"):
        a = normalized_digest(ctx["out"] / name)
        b = normalized_digest(second / name)
        assert a == b, "%s 两次渲染结果不同：%s vs %s" % (name, a[:16], b[:16])
        digests[name] = a
    ctx["digests"] = digests
    return "2 张图哈希一致：" + "、".join("%s=%s…" % (k, v[:12]) for k, v in digests.items())


@node("N10", "可复现性 · 与仓库记录的金样比对")
def n10(ctx):
    """仓库里存着一组哈希，是在某台机器上渲出来的。这条比对本机的渲染结果。

    **这条证明什么、不证明什么，要说准。** 它证明的是：一份全新克隆、在另一个运行时里、
    另一个进程中渲染，产出与记录值逐位相同。它**不**自动证明「跨机器」——如果跑在录制
    金样的同一台机器上，环境是相同的，跨机器一致性并没有被检验到。要真正验证跨机器，
    需要在**不同操作系统或不同 OCCT 构建**的机器上跑这条，并把结果对回来。

    不一致不一定是缺陷，最常见是 OCCT / ezdxf 版本与 requirements-pinned.txt 不符，
    所以失败时把本机版本一并打出来。
    """
    assert GOLDEN.is_file(), "缺少金样文件 %s" % GOLDEN
    expected = {}
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2:
            expected[parts[0]] = parts[1]
    assert expected, "金样文件里没有记录任何哈希"
    got = ctx.get("digests") or {}
    assert got, "N9 未产出哈希，无法比对"
    bad = [name for name, want in expected.items()
           if name in got and got[name] != want]
    if bad:
        versions = []
        for check in (ctx.get("doctor") or {}).get("checks", []):
            if check["id"] in ("cadquery-ocp", "ezdxf", "numpy", "python"):
                versions.append("%s=%s" % (check["id"], check.get("detail", "?")[:24]))
        raise AssertionError(
            "与金样不一致：%s。先核对依赖版本是否与 requirements-pinned.txt 相符（本机 %s）。"
            "若版本一致仍不同，说明渲染结果确实变了，请把两边哈希一并反馈。"
            % ("、".join(bad), "；".join(versions)))
    return "%d 张图与仓库金样逐位一致（本机环境下可复现；跨机器需在异构机器上另跑一次）" % len(expected)


@node("N11", "合规 · 图上不得出现明细表与厂内件号")
def n11(ctx):
    import ezdxf

    findings = []
    for dxf in sorted(ctx["out"].glob("*.dxf")):
        doc = ezdxf.readfile(str(dxf))
        msp = doc.modelspace()
        table = [e for e in msp if e.dxf.layer == "TABLE"]
        assert not table, "%s 上出现了明细表实体 %d 个——专利附图不允许" % (dxf.name, len(table))
        for e in msp:
            if e.dxftype() != "TEXT":
                continue
            text = e.dxf.text
            if re.search(r"[A-Za-z]{2,4}[0-9]{4,8}[-_]", text) or re.search(r"_[0-9]+_[0-9]+$", text):
                findings.append("%s: %s" % (dxf.name, text))
        for e in msp:
            lt = getattr(e.dxf, "linetype", "CONTINUOUS")
            assert lt in ("CONTINUOUS", "BYLAYER"), \
                "%s 上有非连续线型实体：%s" % (dxf.name, lt)
    assert not findings, "图上出现疑似厂内件号：%s" % "；".join(findings)
    return "无明细表、无件号、线型全连续"


@node("N12", "BOM 导入 · 件号到中文名的匹配")
def n12(ctx):
    sys.path.insert(0, str(SCRIPTS))
    try:
        from plan_studio import match_bom_to_parts, parse_bom
    except ImportError as exc:
        raise Skip("Plan Studio 依赖未装（%s），BOM 功能不参与本次验收" % exc)
    csv = ("物料编码,物料名称,数量\n"
           "SYN-A01,底座,1\n"
           "SYN-B02,回转座,1\n"
           "SYN-H08,紧固螺钉,4\n").encode("utf-8")
    bom = parse_bom("bom.csv", csv)          # -> {件号: {"name": ..., "qty": ...}}
    assert bom, "BOM 解析结果为空"
    assert bom.get("SYN-A01", {}).get("name") == "底座", \
        "BOM 未正确解析出「SYN-A01 → 底座」，实得 %r" % bom.get("SYN-A01")
    names = [p["name"] for p in json.loads(
        Path(ctx["assembly"]).read_text(encoding="utf-8"))["parts"]]
    matched = match_bom_to_parts(bom, names)  # -> {零件名: {"code","name","via"}}
    assert len(matched) >= 3, "BOM 应匹配上至少 3 个零件，实得 %d" % len(matched)
    for part, hit in matched.items():
        assert hit.get("name"), "%s 匹配到了 BOM 但没有名称" % part
    return "解析 %d 条件号，匹配上 %d 个零件（%s）" % (
        len(bom), len(matched),
        "、".join("%s→%s" % (k, v["name"]) for k, v in sorted(matched.items())[:3]))


@node("N13", "DWG 转换")
def n13(ctx):
    doctor = ctx.get("doctor") or {}
    have = {c["id"]: c["status"] for c in doctor.get("checks", [])}
    autocad = have.get("autocad-core-console") == "ok"
    libredwg = have.get("libredwg-dwgread") == "ok"
    if not (autocad or libredwg):
        raise Skip("本机既无 AutoCAD 也无 LibreDWG，DXF→DWG 不参与验收（DXF 仍是有效交付件）")
    dxf = ctx["out"] / "fig2.dxf"
    dwg = ctx["tmp"] / "fig2.dwg"
    if autocad:
        proc = run([SCRIPTS / "autocad_core_dxf_to_dwg.py", dxf, dwg], timeout=900)
        engine = "AutoCAD"
    else:
        proc = run([SCRIPTS / "libredwg_dxf_to_dwg.py", dxf, "-o", ctx["tmp"]], timeout=900)
        engine = "LibreDWG"
        dwg = ctx["tmp"] / "fig2.dwg"
    assert proc.returncode == 0, "%s 转换失败：%s" % (engine, (proc.stdout + proc.stderr)[-500:])
    assert dwg.is_file() and dwg.stat().st_size > 0, "%s 未产出 DWG" % engine
    return "%s 转换成功，%d bytes" % (engine, dwg.stat().st_size)


@node("N14", "单元测试全绿")
def n14(ctx):
    proc = subprocess.run([PY, "-m", "pytest", "tests/", "-q", "-p", "no:randomly"],
                          capture_output=True, text=True, cwd=str(REPO), timeout=1800)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr[-200:]
    assert proc.returncode == 0, "pytest 未全绿：%s" % tail
    return tail


NODES = [n1, n2, n3, n4, n5, n6, n7, n8, n9, n10, n11, n12, n13, n14]


# ------------------------------------------------------------------ 主流程


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", type=Path, help="把结果写成 JSON，便于粘贴回报")
    ap.add_argument("--keep", action="store_true", help="保留临时产物目录")
    args = ap.parse_args()

    if not FIXTURE.is_file():
        print("缺少测试夹具：%s\n请确认完整克隆了仓库。" % FIXTURE, file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="acceptance-"))
    ctx = {"tmp": tmp}
    print("专利附图工具链 · 自动验收")
    print("仓库：%s" % REPO)
    print("产物：%s\n" % tmp)

    stop_after_failure = {"N1", "N2", "N3", "N5"}   # 这几个垮了，后面的没有意义
    for fn in NODES:
        node_id, title = fn._node
        print("  [%-3s] %s …" % (node_id, title), end="", flush=True)
        ok = fn(ctx)
        row = RESULTS[-1]
        mark = {"通过": "✓", "未通过": "✗", "跳过": "—"}[row["status"]]
        print("\r  [%-3s] %s %s %s" % (node_id, mark, title, row["detail"][:60]))
        if ok is False and node_id in stop_after_failure:
            print("\n  %s 是后续节点的前提，已中止。" % node_id)
            break

    passed = sum(1 for r in RESULTS if r["ok"] is True)
    failed = [r for r in RESULTS if r["ok"] is False]
    skipped = sum(1 for r in RESULTS if r["ok"] is None)

    print("\n" + "=" * 72)
    print("%-5s %-6s %-34s %s" % ("节点", "结果", "内容", "说明"))
    print("-" * 72)
    for r in RESULTS:
        print("%-5s %-6s %-34s %s" % (r["id"], r["status"], r["title"], r["detail"][:70]))
    print("=" * 72)
    print("通过 %d ／ 未通过 %d ／ 跳过 %d ／ 共 %d 个节点，耗时 %.0f 秒"
          % (passed, len(failed), skipped, len(RESULTS), time.time() - _T0))

    if failed:
        print("\n未通过的节点：")
        for r in failed:
            print("  [%s] %s\n      %s" % (r["id"], r["title"], r["detail"]))
        print("\n请把上面这段原样反馈，不要只说「跑失败了」——每条 detail 都指明了原因。")
    else:
        print("\n全部通过。这台机器上的工具链功能完备，且与仓库金样逐位一致。")

    print("\n注意：脚本测不了模型行为（是否自己写脚本、是否手填标记号）。")
    print("那部分请按 acceptance/行为验收.md 走一遍，需要人看对话记录。")

    if args.json:
        args.json.write_text(json.dumps(
            {"repo": str(REPO), "passed": passed, "failed": len(failed),
             "skipped": skipped, "results": RESULTS},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n结果已写入 %s" % args.json)

    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
