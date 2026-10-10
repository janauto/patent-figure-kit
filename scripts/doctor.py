#!/usr/bin/env python3
"""Capability report for the v2 patent-figure toolchain.

The doctor is the first line of cross-machine consistency: an environment that
differs from `docs/impl-contract.md` §1 must be reported explicitly instead of
degrading silently at render time.

Probe policy:

- Packages are probed by importing a **real module** (and, for `cadquery-ocp`,
  real submodules), not by looking a name up in the distribution metadata. A
  broken wheel and a missing wheel are the same failure for us.
- No subprocess is ever spawned. Every probe is an import or a filesystem
  lookup, so the doctor cannot hang on a stuck external tool.
- Checks carry `required`. Required checks decide the exit code; optional ones
  are findings, printed with their fix but never fatal. `simfang.ttf` and
  `txt.shx` are optional on purpose: the DXF records only the *style name*, so a
  missing font degrades rendering on this machine but does not corrupt the file
  (see `references/workflow.md` §4 for the outlined-DWG route).

CLI: `python3 scripts/doctor.py [--json]`. Exit 0 when every required check
passes, 1 when one is missing or version-mismatched, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib
import json
import os
import shlex
import shutil
import sys
import unicodedata

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_WRONG_VERSION = "wrong-version"

MIN_PYTHON = (3, 9)

# Version table copied verbatim from docs/impl-contract.md §1. requirements-pinned.txt
# holds the same six pins; keep the two in step when the contract table changes.
PINNED = (
    ("cadquery-ocp", "7.7.2"),
    ("ezdxf", "1.4.2"),
    ("numpy", "2.0.2"),
    ("jsonschema", "4.25.1"),
    ("matplotlib", "3.9.4"),
    ("pytest", "8.4.2"),
)
PINNED_VERSION = dict(PINNED)

# This project uses the OCP namespace (pip `cadquery-ocp`), never the conda-only
# `pythonocc-core` (OCC.Core). Import the submodules occ_backend.py actually needs:
# STEP/XCAF load, tessellation, HLR, topology, geometry primitives.
OCP_SUBMODULES = (
    "OCP.STEPCAFControl",
    "OCP.XCAFDoc",
    "OCP.TopoDS",
    "OCP.BRepMesh",
    "OCP.HLRBRep",
    "OCP.gp",
)
RIVAL_OCC_MODULE = "OCC.Core"

# Filesystem probe roots. macOS first, then Linux; both are searched on either
# platform so a report from one machine reads the same as from the other.
FONT_ROOTS = (
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    "/Network/Library/Fonts",
    "~/Library/Fonts",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/usr/share/texmf/fonts",
    "~/.fonts",
    "~/.local/share/fonts",
)
FONT_WALK_DEPTH = 4

SIMFANG_NAMES = ("simfang.ttf", "simfang.ttc")
# Purely informational: what Chinese text would fall back to on this machine.
CJK_FALLBACK_PATTERNS = ("songti*", "*fangsong*", "simsun*", "notosanscjk*",
                         "notoserifcjk*", "pingfang*", "*wqy*", "*uming*", "*ukai*")

SHX_NAMES = ("txt.shx",)
# Same install patterns as scripts/autocad_core_dxf_to_dwg.py (frozen, §2), plus the
# font directory that ships beside the executable. See references/workflow.md §6.
AUTOCAD_CORE_PATTERNS = (
    "/Applications/Autodesk/AutoCAD */AutoCAD *.app/Contents/Helpers/AcCoreConsole.app"
    "/Contents/MacOS/AcCoreConsole",
    "/Applications/AutoCAD *.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole",
)
SHX_DIR_PATTERNS = (
    "/Applications/Autodesk/AutoCAD */AutoCAD *.app/Contents/Resources/Fonts",
    "/Applications/AutoCAD *.app/Contents/Resources/Fonts",
    "~/Library/Application Support/Autodesk/*/Fonts",
    "/opt/oda/*/Fonts",
    "/usr/share/bricscad*/Fonts",
)

DWGREAD_TOOL = "dwgread"

STATUS_LABEL = {STATUS_OK: "OK", STATUS_MISSING: "缺失", STATUS_WRONG_VERSION: "版本不符"}

MAX_LISTED_PATHS = 3


# --------------------------------------------------------------------------- #
# filesystem helpers                                                          #
# --------------------------------------------------------------------------- #
def _listdir(path: str) -> list[str]:
    """Directory entries, sorted. Unreadable directories are empty, not fatal."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _has_magic(segment: str) -> bool:
    return any(ch in segment for ch in "*?[")


def _expand_pattern(pattern: str) -> list[str]:
    """Expand a '/'-separated glob path into existing paths, sorted.

    Written on top of `fnmatch.fnmatchcase` rather than `glob.glob` because §7
    rule 6 of the contract bans the case-normalising `fnmatch.fnmatch`, and
    `glob` routes through it. Matching is against the real entry names, so on a
    case-insensitive volume the on-disk spelling is what wins.
    """
    expanded = os.path.expanduser(pattern)
    absolute = expanded.startswith(os.sep)
    segments = [s for s in expanded.split(os.sep) if s]
    current = [os.sep] if absolute else [os.curdir]
    for segment in segments:
        found: list[str] = []
        if _has_magic(segment):
            for base in current:
                for name in _listdir(base):
                    if fnmatch.fnmatchcase(name, segment):
                        found.append(os.path.join(base, name))
        else:
            for base in current:
                candidate = os.path.join(base, segment)
                if os.path.exists(candidate):
                    found.append(candidate)
        current = sorted(set(found))
        if not current:
            return []
    return current


def _walk_files(root: str, max_depth: int) -> list[tuple[str, str]]:
    """(directory, filename) pairs under `root`, depth-limited, deterministic order."""
    base = os.path.expanduser(root)
    if not os.path.isdir(base):
        return []
    base_depth = base.rstrip(os.sep).count(os.sep)
    out: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        if dirpath.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        for filename in sorted(filenames):
            out.append((dirpath, filename))
    return out


def _find_font_files(names: tuple[str, ...], roots: tuple[str, ...],
                     max_depth: int = FONT_WALK_DEPTH) -> list[str]:
    """Full paths of files whose name matches `names`, case-insensitively.

    Font file names are spelled inconsistently across distributions
    (`simfang.ttf` / `SimFang.ttf`), so this probe is deliberately
    case-insensitive. It is a filesystem lookup, not a part selector — §7 rule 6
    governs the latter.
    """
    wanted = tuple(n.lower() for n in names)
    hits: list[str] = []
    for root in roots:
        for dirpath, filename in _walk_files(root, max_depth):
            if filename.lower() in wanted:
                hits.append(os.path.join(dirpath, filename))
    return sorted(set(hits))


def _find_cjk_fallbacks(roots: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for root in roots:
        for _dirpath, filename in _walk_files(root, FONT_WALK_DEPTH):
            low = filename.lower()
            for pattern in CJK_FALLBACK_PATTERNS:
                if fnmatch.fnmatchcase(low, pattern):
                    hits.append(filename)
                    break
    return sorted(set(hits))


def _expand_all(patterns: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for pattern in patterns:
        out.extend(_expand_pattern(pattern))
    return sorted(set(out))


# --------------------------------------------------------------------------- #
# check construction                                                          #
# --------------------------------------------------------------------------- #
def _check(check_id: str, status: str, detail: str, fix: str = "",
           required: bool = True) -> dict:
    return {"id": check_id, "status": status, "detail": detail,
            "fix": fix if status != STATUS_OK else "", "required": required}


def _pip_fix(*specs: str) -> str:
    args = " ".join(shlex.quote(s) for s in specs)
    return "%s -m pip install %s" % (shlex.quote(sys.executable), args)


def _dist_version(dist: str) -> str | None:
    try:
        import importlib.metadata as metadata
    except Exception:  # pragma: no cover - stdlib on 3.9
        return None
    try:
        return metadata.version(dist)
    except Exception:
        return None


def _import_error(exc: BaseException) -> str:
    return "%s: %s" % (type(exc).__name__, exc)


def check_python() -> dict:
    got = "%d.%d.%d" % sys.version_info[:3]
    want = "%d.%d" % MIN_PYTHON
    detail = "Python %s · %s" % (got, sys.executable)
    if sys.version_info[:2] < MIN_PYTHON:
        return _check("python", STATUS_WRONG_VERSION,
                      "%s，低于要求的 %s" % (detail, want),
                      "安装 Python %s 及以上后重建虚拟环境；本项目按 3.9 编写"
                      "（不用 match、不用运行期的 X | Y 注解）" % want)
    return _check("python", STATUS_OK, "%s（要求 >= %s）" % (detail, want))


def check_package(dist: str, module: str, submodules: tuple[str, ...] = (),
                  extra_detail: str = "") -> dict:
    """Import-based probe for one pinned distribution."""
    want = PINNED_VERSION[dist]
    fix = _pip_fix("%s==%s" % (dist, want))
    try:
        importlib.import_module(module)
    except Exception as exc:
        # `extra_detail` describes what a SUCCESSFUL probe verified, so it must not
        # be appended here — nothing was verified.
        return _check(dist, STATUS_MISSING,
                      "无法 import %s（%s）" % (module, _import_error(exc)), fix)
    for sub in submodules:
        try:
            importlib.import_module(sub)
        except Exception as exc:
            return _check(dist, STATUS_MISSING,
                          "%s 已安装但子模块 %s 无法 import（%s）——"
                          "扩展模块与本机架构/Python 版本不匹配时会这样"
                          % (module, sub, _import_error(exc)), fix)
    got = _dist_version(dist)
    if got is None:
        return _check(dist, STATUS_WRONG_VERSION,
                      "%s 可以 import，但读不到 %s 的版本元数据，无法确认与契约 §1 的 %s 一致"
                      % (module, dist, want), fix)
    if got != want:
        return _check(dist, STATUS_WRONG_VERSION,
                      "本机 %s，契约 §1 锁定 %s" % (got, want), fix)
    suffix = "（%s）" % extra_detail if extra_detail else ""
    return _check(dist, STATUS_OK, "%s%s" % (got, suffix))


def check_ocp() -> dict:
    """`cadquery-ocp`, probed through real OCP submodules."""
    result = check_package("cadquery-ocp", "OCP", OCP_SUBMODULES,
                           extra_detail="已验证 %d 个子模块" % len(OCP_SUBMODULES))
    if result["status"] == STATUS_MISSING:
        try:
            importlib.import_module(RIVAL_OCC_MODULE)
        except Exception:
            pass
        else:
            result["detail"] += ("。注意：本机装的是 pythonocc-core（%s 命名空间），"
                                 "本项目要求 OCP 命名空间，两者不能互相替代"
                                 % RIVAL_OCC_MODULE)
    return result


def check_drawing_backend() -> dict:
    """ezdxf.addons.drawing + matplotlib backend — needed by render_preview()."""
    fix = _pip_fix("ezdxf==%s" % PINNED_VERSION["ezdxf"],
                   "matplotlib==%s" % PINNED_VERSION["matplotlib"])
    try:
        module = importlib.import_module("ezdxf.addons.drawing.matplotlib")
        importlib.import_module("ezdxf.addons.drawing")
    except Exception as exc:
        return _check("ezdxf-drawing-matplotlib", STATUS_MISSING,
                      "无法 import ezdxf.addons.drawing.matplotlib（%s）——"
                      "预览 PNG 渲染不可用" % _import_error(exc), fix)
    if not hasattr(module, "MatplotlibBackend"):
        return _check("ezdxf-drawing-matplotlib", STATUS_MISSING,
                      "ezdxf.addons.drawing.matplotlib 里没有 MatplotlibBackend，"
                      "该 ezdxf 版本的后端接口与本项目不符", fix)
    return _check("ezdxf-drawing-matplotlib", STATUS_OK, "MatplotlibBackend 可用")


def check_simfang() -> dict:
    """Chinese drawing font. Optional by design — see the module docstring."""
    roots = FONT_ROOTS + tuple(_expand_all(SHX_DIR_PATTERNS))
    hits = _find_font_files(SIMFANG_NAMES, roots)
    fix = ("把 simfang.ttf 装到 ~/Library/Fonts（macOS）或 ~/.local/share/fonts（Linux）；"
           "若拿不到该字体，按 references/workflow.md §4 走轮廓化 DWG 路线，"
           "让中文以几何而不是文字交付")
    if hits:
        return _check("font-simfang", STATUS_OK, hits[0], required=False)
    fallbacks = _find_cjk_fallbacks(FONT_ROOTS)[:MAX_LISTED_PATHS]
    fallback_text = "、".join(fallbacks) if fallbacks else "无中文字体"
    return _check("font-simfang", STATUS_MISSING,
                  "未找到 simfang.ttf（GB/T 14691 长仿宋体）。DXF 里记的仍是样式名 simfang.ttf，"
                  "所以文件本身正确；后果是本机回退到 %s 显示，换到没有该字体的机器上中文可能变方块"
                  % fallback_text,
                  fix, required=False)


def check_txt_shx() -> dict:
    """AutoCAD stick font for numerals. Optional for the same reason as simfang."""
    roots = tuple(_expand_all(SHX_DIR_PATTERNS)) + FONT_ROOTS
    hits = _find_font_files(SHX_NAMES, roots)
    if hits:
        return _check("font-txt-shx", STATUS_OK, hits[0], required=False)
    return _check("font-txt-shx", STATUS_MISSING,
                  "未找到 txt.shx（附图标记数字用的 AutoCAD 单线字体）。"
                  "DXF 里记的仍是样式名 txt.shx；本机预览会回退到别的字体，数字形状与出图稿不同",
                  "安装 AutoCAD（其 Resources/Fonts/shx/txt.shx 即可用），"
                  "或在 CAD 里把 NUM 样式改成本机可用的等宽字体",
                  required=False)


def check_plan_studio() -> dict:
    """Plan Studio's own dependencies — optional, and deliberately unpinned.

    fastapi/uvicorn serve the browser UI, openpyxl reads .xlsx BOMs. None of them touches the
    render chain, so a missing one never blocks a drawing and never changes a digest; it only
    means the manual route is unavailable. Reporting them here saves the user from discovering
    it as an ImportError at launch.
    """
    missing = []
    for module in ("fastapi", "uvicorn", "openpyxl"):
        try:
            importlib.import_module(module)
        except Exception:
            missing.append(module)
    fix = _pip_fix("fastapi", "uvicorn", "openpyxl")
    if not missing:
        return _check("plan-studio-deps", STATUS_OK,
                      "fastapi / uvicorn / openpyxl 齐备，可运行 scripts/plan_studio.py",
                      required=False)
    detail = ("缺 %s —— 人工点标界面（scripts/plan_studio.py）无法启动；"
              "命令行出图链路不受影响" % "、".join(missing))
    if missing == ["openpyxl"]:
        detail = "缺 openpyxl —— Plan Studio 的「导入 BOM」只能读 .csv，读不了 .xlsx"
    return _check("plan-studio-deps", STATUS_MISSING, detail, fix, required=False)


def check_autocad_core() -> dict:
    """AcCoreConsole — the preferred DXF->DWG route (references/workflow.md §6)."""
    hits = _expand_all(AUTOCAD_CORE_PATTERNS)
    if hits:
        newest = sorted(hits, reverse=True)[0]
        return _check("autocad-core-console", STATUS_OK, newest, required=False)
    return _check("autocad-core-console", STATUS_MISSING,
                  "未找到 AcCoreConsole：Autodesk 原生的 DXF->DWG 与 AUDIT 路线不可用",
                  "安装 AutoCAD（macOS 路径见 references/workflow.md §6）；"
                  "没有 AutoCAD 时用 scripts/libredwg_dxf_to_dwg.py，"
                  "以 dwgread 复解析代替 AUDIT",
                  required=False)


def check_dwgread() -> dict:
    """LibreDWG dwgread — the fallback DWG route and re-parse validator."""
    path = shutil.which(DWGREAD_TOOL)
    if path:
        return _check("libredwg-dwgread", STATUS_OK, path, required=False)
    return _check("libredwg-dwgread", STATUS_MISSING,
                  "PATH 上没有 dwgread：LibreDWG 的 DWG 复解析校验不可用",
                  "brew install libredwg（macOS）或 apt install libredwg-tools（Linux）",
                  required=False)


def run_checks() -> dict:
    """Every probe, in a fixed order. Deterministic: no time, no randomness."""
    checks = [
        check_python(),
        check_ocp(),
        check_package("ezdxf", "ezdxf"),
        check_package("numpy", "numpy"),
        check_package("jsonschema", "jsonschema"),
        check_package("matplotlib", "matplotlib"),
        check_package("pytest", "pytest"),
        check_drawing_backend(),
        check_simfang(),
        check_txt_shx(),
        check_autocad_core(),
        check_dwgread(),
        check_plan_studio(),
    ]
    ok = all(c["status"] == STATUS_OK for c in checks if c["required"])
    return {"checks": checks, "ok": ok}


# --------------------------------------------------------------------------- #
# reporting                                                                   #
# --------------------------------------------------------------------------- #
def _display_width(text: str) -> int:
    """Terminal columns of `text`, counting East Asian wide glyphs as two."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def format_report(report: dict) -> str:
    lines = ["patent-figure 环境自检（scripts/doctor.py）", ""]
    names = {c["id"]: c["id"] + ("" if c["required"] else "（可选）") for c in report["checks"]}
    name_width = max(_display_width(v) for v in names.values())
    mark_width = max(_display_width(v) for v in STATUS_LABEL.values())
    for check in report["checks"]:
        lines.append("  [%s] %s  %s"
                     % (_pad(STATUS_LABEL[check["status"]], mark_width),
                        _pad(names[check["id"]], name_width), check["detail"]))
        if check["fix"]:
            lines.append("  %s    修复：%s" % (" " * (mark_width + 2), check["fix"]))
    required = [c for c in report["checks"] if c["required"]]
    optional = [c for c in report["checks"] if not c["required"]]
    required_ok = sum(1 for c in required if c["status"] == STATUS_OK)
    optional_ok = sum(1 for c in optional if c["status"] == STATUS_OK)
    lines.append("")
    lines.append("必需项 %d/%d 通过，可选项 %d/%d 可用。"
                 % (required_ok, len(required), optional_ok, len(optional)))
    if not report["ok"]:
        lines.append("一次装齐契约 §1 的版本：%s -m pip install -r requirements-pinned.txt"
                     % shlex.quote(sys.executable))
    lines.append("结论：%s" % ("环境可用" if report["ok"] else "有必需项未满足，先修复再出图"))
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="doctor.py",
        description="检查 v2 专利附图工具链的运行环境（契约 §1）。"
                    "退出码：0 必需项全通过，1 有缺失或版本不符，2 用法错误。")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 输出 {\"checks\":[{id,status,detail,fix,required}],\"ok\":bool}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_checks()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
