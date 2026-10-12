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
GOLDEN_EXT = REPO / "tests" / "fixtures" / "golden_digest_ext.txt"
PY = sys.executable or "python3"

RESULTS: list = []
_T0 = time.time()

# ------------------------------------------------------------------ 大模型通道隔离（WP2b 修复轮 1）
# 验收在任何机器上都不得外发大模型调用：不论本机配置了 DeepSeek 密钥、设了 DEEPSEEK_* 环境变量，还是装了会被
# provider=auto 自动探测到的本机 CodeBuddy CLI。main() 一开始就关掉通道（PFK_AI_PROVIDER=none）、把工作台配置
# 指向本次运行的临时目录、清掉 DEEPSEEK_*——同进程起的工作台（N17/N18）与子进程（N14 的 pytest、各 CLI）都继承；
# 再在同进程的大模型客户端（workbench_llm.chat_json）装哨兵：一被调用就记下并抛错，起工作台的节点结束时、以及
# 整次验收结束时都断言调用次数为 0。
LLM_CALLS: list = []


def isolate_llm(tmp: Path) -> None:
    import os as _os
    _os.environ["PFK_AI_PROVIDER"] = "none"
    cfg = tmp / "llm-config"
    cfg.mkdir(parents=True, exist_ok=True)
    _os.environ["PATENT_WORKBENCH_CONFIG"] = str(cfg)
    for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        _os.environ.pop(k, None)
    sys.path.insert(0, str(SCRIPTS))
    import workbench_llm as _L

    def sentinel(provider, purpose):
        LLM_CALLS.append((provider, purpose))
        raise _L.LLMError("验收期间禁止调用大模型客户端（哨兵）")
    _L.OUTBOUND_HOOKS.append(sentinel)
    assert _L.public_settings()["provider"] == "none", "大模型通道未能关闭"


def assert_no_llm_calls(where: str) -> None:
    assert not LLM_CALLS, "%s：验收期间调用了大模型客户端 %d 次：%r" % (
        where, len(LLM_CALLS), LLM_CALLS[:3])


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


@node("N9", "确定性 · 同一计划两次渲染规范化摘要一致")
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


@node("N10b", "扩展摘要 · 覆盖线色/线宽/线型/TEXT 旋转对齐")
def n10b(ctx):
    """N10 的伴生节点，不改动 N10。

    §5.4 冻结的基础摘要只取图层/类型/样式/文本/取整坐标，Codex R0 S0-01 复现了它的盲区：
    改线色、线宽、TEXT 旋转后摘要原封不动。N10b 用 normalized_digest_ext 把这些属性也纳入，
    与仓库 golden_digest_ext.txt 比对。它证明的口径与 N10 一致——“已验证环境下规范化摘要
    一致”，不是“任何机器逐字节相同”。另外断言基线所依据的原始输入文件（source STEP）SHA
    与当前夹具一致，保证比对的是同一份输入（该 SHA 稳定可断言；DXF 原始字节 SHA 因 $TDCREATE
    每次都变，不作基线）。
    """
    import hashlib
    sys.path.insert(0, str(SCRIPTS))
    from patent_figure.sheet import normalized_digest_ext

    assert GOLDEN_EXT.is_file(), "缺少扩展金样文件 %s" % GOLDEN_EXT
    expected = {}
    recorded_step_sha = None
    for line in GOLDEN_EXT.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("# source-step") and "sha256=" in s:
            recorded_step_sha = s.split("sha256=", 1)[1].strip()
            continue
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) == 2:
            expected[parts[0]] = parts[1]
    assert expected, "扩展金样文件里没有记录任何哈希"

    # 比对的是同一份输入吗？原始 STEP 的 SHA 应与基线记录一致。
    step_sha = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert recorded_step_sha, "扩展金样缺少 source-step SHA 记录"
    assert step_sha == recorded_step_sha, (
        "原始输入 STEP 变了：当前 %s，基线记录 %s。扩展金样是针对旧输入录的，先确认是有意改动。"
        % (step_sha[:12], recorded_step_sha[:12]))

    got = {name: normalized_digest_ext(ctx["out"] / name) for name in expected}
    bad = [name for name, want in expected.items() if got[name] != want]
    if bad:
        versions = []
        for check in (ctx.get("doctor") or {}).get("checks", []):
            if check["id"] in ("cadquery-ocp", "ezdxf", "numpy", "python"):
                versions.append("%s=%s" % (check["id"], check.get("detail", "?")[:24]))
        raise AssertionError(
            "扩展摘要与金样不一致：%s（本机 %s）。先核对依赖版本；若一致仍不同，说明线色/线宽/"
            "线型或 TEXT 旋转对齐变了，请把两边哈希一并反馈。"
            % ("、".join(bad), "；".join(versions)))
    return "%d 张图扩展摘要与金样一致（含线色/线宽/线型/TEXT 旋转对齐；输入 STEP SHA 吻合）" % len(expected)


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


@node("N15", "常驻 worker · 与 CLI 两条路径摘要一致，序列稳定")
def n15(ctx):
    """常驻单任务 worker 必须是透明的：同一 plan 经 CLI（子进程）和 worker（常驻、OCP 只导入
    一次、main 在进程内跑）两条路径，规范化摘要逐位一致；且 A→B→A、失败→成功序列中结果稳定、
    不串味（Codex R0 S1-06 / S1-08）。
    """
    sys.path.insert(0, str(SCRIPTS))
    from patent_figure.sheet import normalized_digest
    from patent_figure.worker import Worker

    RP = str(SCRIPTS / "render_patent_figure.py")
    tmp = ctx["tmp"]

    def dig(out):
        return {f: normalized_digest(out / (f + ".dxf")) for f in ("fig1", "fig2")}

    # CLI path
    cli = tmp / "n15_cli"
    pc = run([RP, ctx["plan"], "--assembly", ctx["assembly"], "-o", cli])
    assert pc.returncode == 0, "CLI 渲染失败：%s" % (pc.stdout + pc.stderr)[-300:]
    cli_dig = dig(cli)

    worker = Worker()
    try:
        # worker path, same plan
        wk = tmp / "n15_wk"
        rw = worker.run([RP, ctx["plan"], "--assembly", ctx["assembly"], "-o", str(wk)])
        assert rw.returncode == 0, "worker 渲染失败：%s" % (rw.stdout + rw.stderr)[-300:]
        assert dig(wk) == cli_dig, "worker 与 CLI 摘要不一致：%s vs %s" % (dig(wk), cli_dig)

        # A -> B -> A（B 只出一张图），回到 A 必须与首次 A 逐位一致
        a1 = tmp / "n15_a1"; worker.run([RP, ctx["plan"], "--assembly", ctx["assembly"], "-o", str(a1)])
        worker.run([RP, ctx["plan"], "--assembly", ctx["assembly"], "-o", str(tmp / "n15_b"),
                    "--only", "fig2"])
        a2 = tmp / "n15_a2"; worker.run([RP, ctx["plan"], "--assembly", ctx["assembly"], "-o", str(a2)])
        assert dig(a1) == dig(a2) == cli_dig, "A→B→A 序列后摘要漂移"

        # 失败 -> 成功：强制一次 QA 失败，再正常出图，结果不受污染
        thr = tmp / "n15_thr.json"
        thr.write_text(json.dumps({"text_height_mm_min": 99.0}), encoding="utf-8")
        rf = worker.run([RP, ctx["plan"], "--assembly", ctx["assembly"], "-o", str(tmp / "n15_fail"),
                         "--thresholds", str(thr)])
        assert rf.returncode == 1, "强制失败应退出码 1"
        s = tmp / "n15_succ"
        rs = worker.run([RP, ctx["plan"], "--assembly", ctx["assembly"], "-o", str(s)])
        assert rs.returncode == 0 and dig(s) == cli_dig, "失败后下一次成功渲染被污染"
    finally:
        worker.close()

    # Also verify the PFK_WORKER=0 fallback mode renders the same摘要 (completion standard).
    import os as _os
    fb = tmp / "n15_fallback"
    env = dict(_os.environ); env["PFK_WORKER"] = "0"
    pf = subprocess.run([PY, RP, str(ctx["plan"]), "--assembly", str(ctx["assembly"]),
                         "-o", str(fb)], capture_output=True, text=True, cwd=str(REPO), env=env)
    assert pf.returncode == 0, "PFK_WORKER=0 回退渲染失败"
    assert dig(fb) == cli_dig, "PFK_WORKER=0 回退摘要与 CLI 不一致"
    return "CLI==worker==PFK_WORKER=0 摘要一致；A→B→A 与 失败→成功 序列稳定（OCP 仅导入一次）"


@node("N16", "过闸后原子发布 · 失败件不得进输出目录")
def n16(ctx):
    """故意制造过不了闸的图，断言它不会出现在输出目录，而是被隔离进本次版本的 diag/。

    证明的是交付链的一条硬不变量（Codex R0 S0-04），按 R1e #1 的版本指针发布模型：渲染整套
    写进 ``<out>.versions/results/<id>/``，只有**整套全部过闸**才原子切换 ``current.json``，
    ``-o`` 目录只是当前版本的视图。未全部过闸 → 不切换：输出目录保持上一次成功的完整版本
    （首次即失败则不建立视图），未过闸的图与诊断留在本次版本目录的 diag/。用 --thresholds
    把字高下限抬到 99 mm，强制每张图在 text_height_mm_min 上判 FAIL——合法的审查口径旋钮，
    给本节点一个确定性的失败杠杆，不依赖事后篡改 DXF。
    """
    out = ctx["tmp"] / "out_n16"
    thr = ctx["tmp"] / "thresholds_fail.json"
    thr.write_text(json.dumps({"text_height_mm_min": 99.0}), encoding="utf-8")
    rep = ctx["tmp"] / "n16_report.json"
    proc = run([SCRIPTS / "render_patent_figure.py", ctx["plan"],
                "--assembly", ctx["assembly"], "-o", out,
                "--thresholds", thr, "--cache", ctx["tmp"] / "cache_n16", "--json", rep])
    assert proc.returncode == 1, \
        "强制 QA 失败后退出码应为 1，实得 %d：\n%s" % (proc.returncode, (proc.stdout + proc.stderr)[-500:])
    published = sorted(p.name for p in out.glob("*.dxf")) if out.exists() else []
    assert not published, "输出目录竟出现了未过闸的图：%s（S0-04 不变量被破坏）" % published
    report = json.loads(rep.read_text(encoding="utf-8"))
    assert report.get("published") is False and report.get("current_version") is None, \
        "首次即失败不得建立当前版本：%s" % {k: report.get(k) for k in ("published", "current_version")}
    diag = Path(report.get("diag_dir") or "")
    assert diag.is_dir(), "失败件应被隔离进本次版本的 diag/，但该目录不存在：%s" % diag
    quarantined = sorted(p.name for p in diag.glob("*.dxf"))
    assert quarantined, "diag/ 里应留有失败件的 DXF 供排查，实际为空"
    # 计划级的附图标记说明随本次版本保留（供排查），但不发布到输出目录。
    assert (diag.parent / "reference-numerals.json").is_file(), "失败版本应保留标记说明供排查"
    assert not (out / "reference-numerals.json").exists(), "失败的一套不得发布标记说明"
    # 不再有任何暂存/改名残留（R1e #1：没有 rename 对）。
    leftovers = sorted(p.name for p in out.parent.glob(out.name + ".render-*"))
    assert not leftovers, "暂存目录未清理：%s" % leftovers

    # R1c #7 / R1e #1：先成功、再（改 thresholds）失败重渲进同一目录——输出目录必须仍是上一次
    # 成功的**完整**版本（图与标记说明同属一个版本，不可能「旧图 + 新标记表」），失败件在新版本 diag/。
    out2 = ctx["tmp"] / "out_n16b"
    ok1 = run([SCRIPTS / "render_patent_figure.py", ctx["plan"], "--assembly", ctx["assembly"],
               "-o", out2, "--cache", ctx["tmp"] / "cache_n16b"])
    assert ok1.returncode == 0, "首次渲染应成功"
    good = {p.name: p.read_bytes() for p in out2.glob("*.dxf")}
    good_numerals = (out2 / "reference-numerals.json").read_bytes()
    assert good, "首次渲染应有图"
    rep2 = ctx["tmp"] / "n16b_report.json"
    bad = run([SCRIPTS / "render_patent_figure.py", ctx["plan"], "--assembly", ctx["assembly"],
               "-o", out2, "--thresholds", thr, "--cache", ctx["tmp"] / "cache_n16b",
               "--json", rep2])
    assert bad.returncode == 1, "二次（强制失败）重渲退出码应为 1"
    after = {p.name: p.read_bytes() for p in out2.glob("*.dxf")}
    assert after == good, "失败重渲改动了输出目录：%s → %s" % (sorted(good), sorted(after))
    assert (out2 / "reference-numerals.json").read_bytes() == good_numerals, \
        "失败重渲后标记说明与图不属同一版本（R1c #7 图文混配）"
    r2 = json.loads(rep2.read_text(encoding="utf-8"))
    assert r2.get("published") is False and r2.get("current_version") != r2.get("version")
    d2 = Path(r2.get("diag_dir") or "")
    assert d2.is_dir() and list(d2.glob("*.dxf")), "失败件应在本次版本的 diag/"
    return ("失败件 %d 张全部进本次版本 diag/，输出目录 0 张未过闸图；成功→改口径→失败重渲："
            "输出目录保持上一完整版本（%d 张图 + 同版本标记说明）" % (len(quarantined), len(good)))


# ------------------------------------------------------------------ WP2：导出包与候选分图


_WP2_TOKEN = "acc-wp2"
_WP2_EXAMPLES = ("gearbox", "vise", "valve")


def _wp2_env(ctx):
    """In-process workbench over a private data dir + the three public demo projects, created
    through the SAME REST entry the UI uses (POST /v1/projects/from-example → render job).
    Shared by N17/N18; built once."""
    if "wp2" in ctx:
        return ctx["wp2"]
    try:
        sys.path.insert(0, str(SCRIPTS))
        import workbench as W
        from starlette.testclient import TestClient
    except ImportError as exc:
        raise Skip("缺工作台依赖（fastapi/starlette/httpx）：%s" % exc)
    assert W.LLM.public_settings()["provider"] == "none", "N17/N18 的工作台必须在大模型通道关闭时启动"
    reg = W.Registry(ctx["tmp"] / "wp2-data", _WP2_TOKEN)
    c = TestClient(W.Dispatcher(W.build_workbench(reg, _WP2_TOKEN, 8802), reg),
                   base_url="http://127.0.0.1:8802")
    H = {"Authorization": "Bearer %s" % _WP2_TOKEN}

    def poll(job_id, timeout=1200):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = c.get("/v1/jobs/%s" % job_id, headers=H).json()
            if j["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
                return j
            time.sleep(0.5)
        raise AssertionError("作业 %s 超时" % job_id)

    projects = {}
    for ex in _WP2_EXAMPLES:
        r = c.post("/v1/projects/from-example", headers=H, json={"example": ex})
        assert r.status_code == 202, "from-example %s：%s" % (ex, r.text[:200])
        j = poll(r.json()["job_id"])
        assert j["state"] == "succeeded", "示例 %s 准备失败：%s" % (ex, j.get("error"))
        pid = r.json()["project_id"]
        rj = poll(c.post("/v1/projects/%s/render" % pid, headers=H).json()["job_id"])
        assert rj["state"] == "succeeded", "示例 %s 出图未全部过闸：%s" % (ex, rj.get("error"))
        projects[ex] = {"pid": pid, "render": rj}
    ctx["wp2"] = {"W": W, "reg": reg, "c": c, "H": H, "poll": poll, "projects": projects}
    return ctx["wp2"]


def _recheck_zip(env, export_job, dest):
    """Independent re-check from the DOWNLOADED zip: every PDF/Word in it is checked again by
    deliverable_check against the variant DXFs shipped in the same zip (not the server's report).
    R2j ruling — hash seal: every file of the zip must match the sha256 the SERVER recorded for it
    (result.sha256; the in-zip 核验报告.json must agree), and each document is re-checked with
    that digest as ``expected_sha256``; a PDF must pass the independent render (status ok)."""
    import io as _io
    import zipfile as _zip
    from patent_figure import deliverable as _D
    from patent_figure import deliverable_check as _C
    from patent_figure import export as _EXPORT
    r = env["c"].get("/v1/exports/%s/download" % export_job["id"], headers=env["H"])
    assert r.status_code == 200, "下载失败 %d" % r.status_code
    _zip.ZipFile(_io.BytesIO(r.content)).extractall(str(dest))
    sealed = export_job["result"]["sha256"]
    in_zip = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert sorted(sealed) == [n for n in in_zip if n != _EXPORT.REPORT_NAME], \
        "导出记录的 sha256 与包内文件不一致：%s" % sorted(set(sealed) ^ set(in_zip))
    for name, digest in sealed.items():
        assert _C.sha256_of(dest / name) == digest, "%s 与导出时记录的 sha256 不符" % name
    zrep = json.loads((dest / _EXPORT.REPORT_NAME).read_text(encoding="utf-8"))
    assert zrep["sha256"] == sealed, "包内《核验报告.json》的 sha256 与服务端记录不一致"
    sp = env["c"].get("/v1/capabilities", headers=env["H"]).json()["export"]
    spec = _D.spec(export_job["result"]["spec"]["preset"], export_job["result"]["spec"]["dpi"])
    assert spec["min_text_mm"] == sp["min_text_mm"]
    pages = 0
    for label, sub, suf in (("递交版", "递交版_仅图号", "_filing"),
                            ("交底版", "交底版_带件号表", "_annotated")):
        figs = sorted((dest / sub).glob("*%s.dxf" % suf))
        assert figs, "包内缺 %s 的 DXF" % sub
        rows = []
        for f in figs:
            src = _C.Source(f.stem, 0, f, spec["area_mm"])
            k = int(str(src.caption["text"]).replace("图", "")) if src.caption else 0
            rows.append({"figure": f.stem, "number": k, "dxf": f})
        for fmt in ("pdf", "docx"):
            doc = dest / ("说明书附图_%s.%s" % (label, fmt))
            assert doc.is_file(), "包内缺 %s" % doc.name
            rep = _C.check_document(doc, _C.sources_from(rows, spec["area_mm"]), spec,
                                    expected_sha256=sealed[doc.name])
            assert rep["ok"] and rep["status"] == "ok" and rep["sha256_verified"] is True, \
                "%s 复核不通过：%s %s" % (doc.name, rep["errors"],
                                     [p["errors"] for p in rep["page_checks"]])
            pages += rep["pages"]
            if fmt == "pdf":            # R2i: the writer's own PDF parses STRICTLY, every object
                from pypdf import PdfReader
                bad = _C._pdf_parse_all(PdfReader(str(doc), strict=True))
                assert not bad, "%s 严格解析不通过：%s" % (doc.name, bad)
                env.setdefault("strict_pdfs", []).append(doc.name)
    for name in ("附图说明.txt", "核对清单.txt", "核验报告.json", "reference-numerals.json"):
        assert (dest / name).is_file(), "包内缺 %s" % name
    return pages


@node("N17", "附图导出包 · 三个公开模型 PDF/Word 逐页核验；dpi 72 必须判不通过")
def n17(ctx):
    """F1：PDF/Word 是主交付。对三个公开演示模型各导出「两版 × PDF/Word/DXF」，服务端逐页核验
    全部通过，并从**下载的 zip** 独立复核每一页（页幅、页边、缩放后字高 mm/px、图号存在、页数 =
    图数、文件齐全）。再故意把 dpi 调到 72：栅格字高不达标，PDF/Word 必须判 fail、移到
    _未通过核验/，DXF 照常 ok——不得报成功。"""
    env = _wp2_env(ctx)
    c, H, poll = env["c"], env["H"], env["poll"]
    assert_no_llm_calls("N17 起工作台与示例准备")
    caps = c.get("/v1/capabilities", headers=H).json()["export"]
    # R2j ruling: a PDF passes only after an independent render — N17 runs WITH a renderer
    assert caps["pdf_check_available"] and caps["pdf_renderer"], \
        "N17 需要 pypdf + 独立渲染器（pypdfium2）：%s" % caps.get("unavailable_reasons")
    total_pages = 0
    for ex, info in env["projects"].items():
        pid = info["pid"]
        r = c.post("/v1/projects/%s/export" % pid, headers=H,
                   json={"variant": "both", "formats": ["pdf", "docx", "dxf"], "dpi": 300,
                         "sheet_preset": "standard"})
        assert r.status_code == 202, "%s 导出提交失败：%s" % (ex, r.text[:200])
        j = poll(r.json()["export_id"])
        res = j.get("result") or {}
        assert j["state"] == "succeeded" and res.get("ok"), \
            "%s 导出包未全部通过：%s" % (ex, res.get("reasons") or j.get("error"))
        nfig = len(info["render"]["result"]["figures"])
        for name, doc in res["documents"].items():
            assert doc["ok"] and doc["pages"] == doc["expected_pages"] == nfig, \
                "%s %s：%d/%d 页 %s" % (ex, name, doc["pages"], nfig, doc["errors"])
            assert doc["status"] == "ok" and doc["sha256"] == res["sha256"][doc["path"]]
            assert name.endswith(".docx") or doc["render_check"] == caps["pdf_renderer"]
            # R2k: the render matches the source page spatially (IoU / caption / background)
            assert name.endswith(".docx") or all(
                p["render_match"]["iou"] >= 0.9 for p in doc["page_checks"]), doc["page_checks"]
        # R2k: the export's own directory (bundle, zip, report, result) is sealed and intact
        from patent_figure import export as _EXPORT
        assert res.get("seal", {}).get("sha256"), "导出结果缺封口记录"
        problems = _EXPORT.verify_export_seal(env["reg"].data / "jobs" / j["id"],
                                              res["seal"]["sha256"])
        assert not problems, "%s 导出目录封口校验不通过：%s" % (ex, problems)
        total_pages += _recheck_zip(env, j, ctx["tmp"] / ("wp2-zip-" + ex))
        r72 = poll(c.post("/v1/projects/%s/export" % pid, headers=H,
                          json={"variant": "both", "formats": ["pdf", "docx", "dxf"],
                                "dpi": 72}).json()["export_id"])
        res72 = r72["result"]
        bad = {k: v for k, v in res72["manifest"].items() if k.endswith(("/pdf", "/docx"))}
        assert res72["ok"] is False and bad and all(v == "fail" for v in bad.values()), \
            "%s dpi 72 未判不通过：%s" % (ex, bad)
        assert all("px" in res72["reasons"][k] for k in bad), res72["reasons"]
        # R2i: the dpi-72 PDFs fail on raster text height ONLY — they too parse strictly, with
        # no document-level (structure / strict-parse) error
        for name, doc in res72["documents"].items():
            assert not (name.endswith(".pdf") and doc["errors"]), \
                "%s %s：%s" % (ex, name, doc["errors"])
        assert all(v == "ok" for k, v in res72["manifest"].items() if k.endswith("/dxf"))
        assert not any(f.startswith("说明书附图_") for f in res72["files"]), res72["files"]
    return ("3 个模型 × 两版 × PDF/Word 服务端核验 + 下载包按服务端 sha256 封印比对后独立复核共 %d 页"
            "全部通过（PDF 经 %s 渲染并与源图空间比对）；导出目录封口完好；"
            "%d 份 PDF 逐对象严格解析（pypdf strict）无误；"
            "dpi 72 时 PDF/Word 全部判 fail（栅格字高 < 24 px）并隔离"
            % (total_pages, caps["pdf_renderer"], len(env.get("strict_pdfs", []))))


@node("N18", "候选分图 · 三个公开模型至少各一张过闸；台虎钳整机被拦并给建议")
def n18(ctx):
    """F2-lite：每个候选都实际渲染 + 过 QA 后才有状态；产物进候选专属版本库，不改当前版本。
    三个公开模型各至少一张候选通过；台虎钳整机装配候选必须被闸门拦下并给白话原因与建议。"""
    env = _wp2_env(ctx)
    c, H, poll = env["c"], env["H"], env["poll"]
    out = []
    for ex, info in env["projects"].items():
        pid = info["pid"]
        cur = env["reg"].get(pid).root / "studio" / "current.json"
        before = cur.read_bytes()
        r = c.post("/v1/projects/%s/plan/suggest" % pid, headers=H, json={})
        assert r.status_code == 202, "%s suggest 提交失败：%s" % (ex, r.text[:200])
        j = poll(r.json()["job_id"])
        assert j["state"] == "succeeded", "%s 候选分图作业失败：%s" % (ex, j.get("error"))
        res = j["result"]
        assert cur.read_bytes() == before, "%s：候选试渲染改动了当前版本指针" % ex
        cands = res["candidates"]
        assert cands and all(x["status"] in ("pass", "fail") and x["reason"] for x in cands), \
            "%s：有候选没有渲染后的状态" % ex
        passed = [x for x in cands if x["status"] == "pass"]
        assert passed, "%s：没有候选过闸（%s）" % (ex, res.get("empty_reason"))
        assert all(m.get("part") for x in cands for m in x["members"])
        if ex == "vise":
            whole = next(x for x in cands if x["source"]["kind"] == "whole")
            assert whole["status"] == "fail" and whole["suggestion"], \
                "台虎钳整机候选未被拦下：%s" % whole
            out.append("台虎钳整机被拦：%s" % whole["reason"][:30])
        out.append("%s %d/%d 过闸" % (ex, len(passed), len(cands)))
    assert_no_llm_calls("N18")
    return "；".join(out)


NODES = [n1, n2, n3, n4, n5, n6, n7, n8, n9, n10, n10b, n11, n12, n13, n14, n15, n16, n17, n18]


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
    isolate_llm(tmp)
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

    print("\n外发哨兵：验收期间大模型客户端被调用 %d 次（通道 PFK_AI_PROVIDER=none）" % len(LLM_CALLS))
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
        print("\n全部通过。这台机器上的工具链功能完备，且规范化图元摘要与仓库金样一致"
              "（本机环境；口径见 acceptance/README.md）。")

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
    return 1 if failed or LLM_CALLS else 0


if __name__ == "__main__":
    raise SystemExit(main())
