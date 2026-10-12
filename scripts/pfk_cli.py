#!/usr/bin/env python3
"""patent-figure-kit 统一入口（WP4 · T1）：启动工作台、环境自检、验收、命令行工具路由。

    patent-figure-kit [选项]        # 等同 pfk start：启动 Web 工作台并打开浏览器
    pfk start [--port N] [--auto-port] [--data 目录] [--no-browser] [部署参数…]
    pfk doctor [--profile core|web] [--json]
    pfk acceptance [--json 结果.json] [--keep]
    pfk paths [--check]              # 打印 / 校验随包资源（schemas、webui、examples、字体）的位置
    pfk version
    pfk <工具> [参数…]               # analyze / render / annotate / qa / validate / glb / export /
                                     # check / flowchart / suggest / demo-models / bench / studio

仓库检出里：``python3 scripts/pfk_cli.py …``。wheel 安装后本文件就是包 ``patent_figure_kit`` 的
``__init__.py``，入口点 ``patent-figure-kit`` / ``pfk`` 指向这里——资源一律按本文件位置定位（wheel 把
仓库树原样装进包目录），与从哪个目录启动无关。纯标准库；不 import OCP。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

__version__ = "0.1.0"

_HERE = Path(__file__).resolve().parent
#: 仓库根（或 wheel 里的包目录）：本文件在 scripts/ 下时取上一级；作为包 __init__.py 时就是包目录。
ROOT = _HERE if (_HERE / "scripts" / "workbench.py").is_file() else _HERE.parent
SCRIPTS = ROOT / "scripts"
DEFAULT_PORT = 8790

TOOLS = {
    "analyze": "analyze_assembly.py",
    "render": "render_patent_figure.py",
    "annotate": "annotate_figure_sheet.py",
    "qa": "qa_patent_figure.py",
    "validate": "validate_figure_plan.py",
    "glb": "export_step_glb.py",
    "export": "export_figure_package.py",
    "check": "deliverable_check.py",
    "flowchart": "render_flowchart.py",
    "suggest": "suggest_figure_plan.py",
    "demo-models": "make_demo_assemblies.py",
    "bench": "bench.py",
    "studio": "plan_studio.py",
}

#: Web 工作台与导出包「通过」所需的 doctor 检查项（doctor 把它们标为可选，因为纯 CLI 出图不需要；
#: 对双击安装的用户它们是必需的——WP2 裁决：缺 pypdfium2 时导出的 PDF 一律判「未核验」）。
WEB_REQUIRED = {
    "plan-studio-deps": "Web 工作台（fastapi / uvicorn / httpx / openpyxl / python-docx）",
    "deliverable-pdf-check": "导出包 PDF 逐页结构核验（pypdf）",
    "deliverable-pdf-render": "导出包 PDF 逐页渲染核验（pypdfium2，导出必需）",
}

#: 随包资源：(相对 ROOT 的路径, 用途)。`pfk paths --check` 与安装脚本据此确认 wheel / 检出完整。
RESOURCES = (
    ("schemas/assembly.schema.json", "装配体 JSON 结构"),
    ("schemas/figure-plan.schema.json", "附图计划 JSON 结构"),
    ("webui/home.html", "工作台首页"),
    ("webui/index.html", "规划台"),
    ("webui/js/api.js", "前端数据适配器"),
    ("webui/vendor/three.module.js", "3D 视图（three.js）"),
    ("examples/planetary-gearbox/figure-plan.json", "公开示例·减速器计划"),
    ("scripts/make_demo_assemblies.py", "公开示例 STEP 生成器（台虎钳 / 球阀 / 减速器）"),
    ("tests/fixtures/synthetic.stp", "合成装配体（验收夹具）"),
    ("tests/fixtures/golden_digest.txt", "N10 金样摘要"),
    ("docs/openapi.yaml", "HTTP 契约"),
    ("acceptance/run_acceptance.py", "自动验收"),
    ("references/patent-figure-spec.md", "CNIPA 形式要求摘录"),
)


def _py() -> str:
    return sys.executable or "python3"


def _script(name: str) -> Path:
    return SCRIPTS / name


def _scripts_on_path() -> None:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))


def _in_repo_checkout() -> bool:
    return (ROOT / ".git").exists() or (ROOT / "pyproject.toml").is_file()


def _install_hint() -> str:
    if _in_repo_checkout():
        return '"%s" -m pip install -r requirements-pinned.txt -r requirements-web.txt' % _py()
    return '"%s" -m pip install "patent-figure-kit[web]"' % _py()


# ----------------------------------------------------------------------------- doctor


def cmd_doctor(argv: Sequence[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="pfk doctor", description="环境自检（包装 scripts/doctor.py）")
    ap.add_argument("--profile", choices=("core", "web"), default="web",
                    help="core = 只看出图链路必需项；web（默认）= 再要求 Web 工作台与导出包依赖")
    # --json 只是「结果打到标准输出」的开关（同 doctor.py），不是输出路径，故不走 argparse 的路径型参数。
    as_json = "--json" in argv
    args = ap.parse_args([a for a in argv if a != "--json"])
    proc = subprocess.run([_py(), str(_script("doctor.py")), "--json"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    try:
        report = json.loads(proc.stdout)
    except ValueError:
        print("doctor 没有给出可解析的结果（退出码 %d）：\n%s" % (proc.returncode,
                                                                (proc.stderr or proc.stdout)[-800:]),
              file=sys.stderr)
        return 1
    checks = {c["id"]: c for c in report.get("checks", [])}
    missing_core = [c for c in report.get("checks", []) if c.get("required") and c["status"] != "ok"]
    missing_web = []
    if args.profile == "web":
        missing_web = [cid for cid in WEB_REQUIRED if checks.get(cid, {}).get("status") != "ok"]
    ok = not missing_core and not missing_web
    if as_json:
        report["profile"] = args.profile
        report["profile_ok"] = ok
        report["profile_missing"] = [c["id"] for c in missing_core] + missing_web
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    subprocess.run([_py(), str(_script("doctor.py"))])        # 人读版（原样）
    if args.profile == "web":
        print("\nWeb 工作台 / 导出包（安装脚本视为必需）：")
        for cid, what in WEB_REQUIRED.items():
            st = checks.get(cid, {}).get("status", "missing")
            print("  [%s] %s" % ("OK      " if st == "ok" else "缺失    ", what))
    if not ok:
        print("\n结论：还不能用。", file=sys.stderr)
        for c in missing_core:
            print("  · %s：%s" % (c["id"], c.get("fix") or c.get("detail")), file=sys.stderr)
        if missing_web:
            print("  · 缺 Web / 导出依赖（%s）。修复：%s" % ("、".join(missing_web), _install_hint()),
                  file=sys.stderr)
        return 1
    print("\n结论：可以启动工作台。")
    return 0


# ----------------------------------------------------------------------------- paths


def resource_report() -> dict:
    rows = [{"path": rel, "what": what, "ok": (ROOT / rel).is_file()} for rel, what in RESOURCES]
    fonts = []
    try:
        _scripts_on_path()
        from patent_figure import sheet as _sheet               # 只 import ezdxf，不碰 OCP
        fonts = [{"file": src, "face": face, "ok": Path(src).is_file()}
                 for src, face in _sheet.PREVIEW_CJK_CANDIDATES]
    except Exception as exc:  # noqa: BLE001 — 报告即可
        fonts = [{"file": "", "face": "", "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}]
    return {"root": str(ROOT), "version": __version__, "python": sys.version.split()[0],
            "resources": rows, "preview_cjk_fonts": fonts,
            "preview_cjk_available": any(f.get("ok") for f in fonts)}


def cmd_paths(argv: Sequence[str]) -> int:
    check = "--check" in argv
    rep = resource_report()
    if "--json" in argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print("资源根：%s（版本 %s，Python %s）" % (rep["root"], rep["version"], rep["python"]))
        for r in rep["resources"]:
            print("  [%s] %-48s %s" % ("OK" if r["ok"] else "缺", r["path"], r["what"]))
        cjk = [f for f in rep["preview_cjk_fonts"] if f.get("ok")]
        if cjk:
            print("  [OK] 预览中文字体：%s（%s）" % (cjk[0]["face"], cjk[0]["file"]))
        else:
            print("  [提示] 未在常见位置找到中文衬线/黑体字体：DXF 不受影响（只记字体名），"
                  "PNG/PDF 预览里的图题可能显示为方块。Linux 请安装 fonts-noto-cjk；Windows 由 ezdxf "
                  "在系统字体里查找 simfang.ttf。")
    missing = [r["path"] for r in rep["resources"] if not r["ok"]]
    if check and missing:
        print("随包资源缺失：%s——安装包不完整，请重新安装。" % "、".join(missing), file=sys.stderr)
        return 1
    return 0


# ----------------------------------------------------------------------------- start


def _port_free(host: str, port: int) -> bool:
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _token_for_probe() -> str:
    tok = os.environ.get("WORKBENCH_TOKEN", "")
    if tok:
        return tok
    try:
        _scripts_on_path()
        import workbench_llm as _llm                       # 只读配置，不生成
        return str(_llm.load_config().get("api_token") or "")
    except Exception:  # noqa: BLE001
        return ""


def _is_our_workbench(port: int, token: str) -> bool:
    """端口上已经是本机这份工作台（同一 token）？是就直接打开它，不再起第二个实例抢同一数据目录。

    先不带 token 看「像不像工作台」（未授权的数据接口应答 401），像才带 token 确认——别的程序占着
    端口时不把 token 发给它。"""
    if not token:
        return False
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/v1/capabilities" % port, timeout=3)
        return False                                   # 不要 token 就给数据：不是工作台
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            return False
    except Exception:  # noqa: BLE001
        return False
    req = urllib.request.Request("http://127.0.0.1:%d/v1/capabilities" % port,
                                 headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return resp.status == 200 and isinstance(body, dict) and "export" in body
    except Exception:  # noqa: BLE001
        return False


def cmd_start(argv: Sequence[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="pfk start", add_help=False)
    ap.add_argument("--data")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--auto-port", action="store_true",
                    help="端口被别的程序占用时自动换一个（8790→8809 之间）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("-h", "--help", action="store_true")
    args, rest = ap.parse_known_args(list(argv))
    _scripts_on_path()
    if args.help:
        import workbench as _wb
        return _wb.main(["serve", "--help"])
    port = args.port
    local = args.host in ("127.0.0.1", "localhost")
    if local and not _port_free("127.0.0.1", port):
        token = _token_for_probe()
        if _is_our_workbench(port, token):
            url = "http://127.0.0.1:%d/?token=%s" % (port, token)
            print("工作台已经在 %d 端口运行，直接为你打开：http://127.0.0.1:%d/" % (port, port))
            if "--no-browser" not in rest:
                import webbrowser
                webbrowser.open(url)
            return 0
        if not args.auto_port:
            print("端口 %d 被别的程序占用。换一个端口：pfk start --port 8791（或加 --auto-port 自动挑）"
                  % port, file=sys.stderr)
            return 1
        for cand in range(port + 1, port + 20):
            if _port_free("127.0.0.1", cand):
                print("端口 %d 被别的程序占用，改用 %d。" % (port, cand))
                port = cand
                break
        else:
            print("8790 附近的端口都被占用，请用 --port 指定一个空闲端口。", file=sys.stderr)
            return 1
    wb_argv: List[str] = []
    if args.data:
        wb_argv += ["--data", args.data]
    wb_argv += ["serve", "--port", str(port)]
    if args.host != "127.0.0.1":
        wb_argv += ["--host", args.host]
    wb_argv += list(rest)
    try:
        import workbench as _wb
    except ImportError as exc:
        print("缺少 Web 工作台依赖（%s）。修复：%s" % (exc, _install_hint()), file=sys.stderr)
        return 1
    return int(_wb.main(wb_argv) or 0)


def start_main(argv: Optional[Sequence[str]] = None) -> int:
    """入口点 ``patent-figure-kit``：直接启动工作台（参数同 ``pfk start``）。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("start", "serve"):
        args = args[1:]
    return cmd_start(args)


# ----------------------------------------------------------------------------- 路由


USAGE = __doc__.split("\n\n", 2)[1] if __doc__ else ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        print("\n工具：%s" % "、".join(sorted(TOOLS)))
        return 0 if args else 2
    cmd, rest = args[0], args[1:]
    if cmd in ("start", "serve"):
        return cmd_start(rest)
    if cmd == "doctor":
        return cmd_doctor(rest)
    if cmd == "paths":
        return cmd_paths(rest)
    if cmd == "version":
        print("patent-figure-kit %s（%s；Python %s）" % (__version__, ROOT, sys.version.split()[0]))
        return 0
    if cmd == "acceptance":
        return subprocess.call([_py(), str(ROOT / "acceptance" / "run_acceptance.py")] + rest)
    if cmd == "seed":
        _scripts_on_path()
        import workbench as _wb
        return int(_wb.main(["seed"] + rest) or 0)
    if cmd in TOOLS:
        return subprocess.call([_py(), str(_script(TOOLS[cmd]))] + rest)
    print("未知命令 %r。可用：start / doctor / acceptance / paths / version / seed / %s"
          % (cmd, " / ".join(sorted(TOOLS))), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
