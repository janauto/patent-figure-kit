#!/usr/bin/env python3
"""「已验证环境」与「安装时长」两张表的数据源（WP4）：README 引用的表只由这里生成，不手填。

    # 1) 每个环境跑完验收后记一条（CI 矩阵的每个组合、或本机）：
    python3 scripts/verified_envs.py record --acceptance acc.json --label ubuntu-22.04 \\
        --install-seconds 143 --install-method "pip -r requirements（CI）" --network "GitHub Actions" \\
        --source "CI run 123" --out records/env-ubuntu-3.11.json
    # 2) 汇总成文档：
    python3 scripts/verified_envs.py render --records records/ --out-dir docs/deploy

``record`` 读 ``acceptance/run_acceptance.py --json`` 的结果，再从**当前解释器**采集平台、架构、Python、
glibc、cadquery-ocp 版本与 git 提交——所以必须在被验证的那个环境里运行。``render`` 只把**全部节点通过且
N10 通过**的组合写进「已验证环境」表；其余组合单列为「未通过 / 未完成」，不进表。

纯标准库。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _dist_version(name: str) -> str:
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:  # noqa: BLE001
        return ""


def _git_rev() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=12", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _last_install(path: str) -> Dict[str, Any]:
    """安装脚本写的 install-timing.json（每行一条）里最后一次真正安装（kind=install）的记录。"""
    rows = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    inst = [r for r in rows if r.get("kind") == "install"]
    return inst[-1] if inst else {}


def cmd_record(args) -> int:
    acc = json.loads(Path(args.acceptance).read_text(encoding="utf-8")) if args.acceptance else None
    results = {r["id"]: r for r in (acc or {}).get("results", [])}
    pytest_detail = (results.get("N14") or {}).get("detail", "")
    if args.install_timing:
        t = _last_install(args.install_timing)
        if t:
            args.install_seconds = args.install_seconds or int(t.get("seconds") or 0)
            args.install_method = args.install_method or t.get("script", "")
    rec: Dict[str, Any] = {
        "label": args.label,
        "os": platform.system(), "os_release": platform.platform(), "arch": platform.machine(),
        "python": platform.python_version(),
        "glibc": platform.libc_ver()[1] if platform.system() == "Linux" else "",
        "cadquery_ocp": _dist_version("cadquery-ocp"), "numpy": _dist_version("numpy"),
        "ezdxf": _dist_version("ezdxf"),
        "commit": args.commit or _git_rev(),
        "date": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
        "source": args.source, "install_method": args.install_method,
        "install_seconds": args.install_seconds, "network": args.network,
        "acceptance": None if acc is None else {
                       "passed": acc.get("passed"), "failed": acc.get("failed"),
                       "skipped": acc.get("skipped"), "total": len(results),
                       "n10": (results.get("N10") or {}).get("status"),
                       "n10b": (results.get("N10b") or {}).get("status"),
                       "failed_nodes": [k for k, r in results.items() if r.get("ok") is False],
                       "skipped_nodes": [k for k, r in results.items() if r.get("ok") is None],
                       "pytest": pytest_detail[:80],
                       "seconds": sum(float(r.get("seconds") or 0) for r in results.values())},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    a = rec["acceptance"] or {}
    print("已记录：%s（%s，Python %s，验收 %s/%s，安装 %s s）" % (
        out, rec["os_release"], rec["python"], a.get("passed", "—"), a.get("total", "—"),
        rec["install_seconds"] or "—"))
    return 0


def _verified(rec: dict) -> bool:
    a = rec.get("acceptance") or {}
    return bool(a.get("total")) and a.get("failed") == 0 and a.get("n10") == "通过"


def _platform_name(rec: dict) -> str:
    osn = {"Darwin": "macOS", "Linux": "Linux", "Windows": "Windows"}.get(rec["os"], rec["os"])
    if rec["os"] == "Darwin":
        ver = rec["os_release"].split("-")[1] if "-" in rec["os_release"] else ""
        return "macOS %s" % ver
    if rec["os"] == "Linux" and rec.get("glibc"):
        return "Linux（glibc %s）" % rec["glibc"]
    return osn


def render(records: List[dict]) -> Dict[str, str]:
    records = sorted(records, key=lambda r: (r["os"], r["label"], r["python"]))
    ok = [r for r in records if _verified(r)]
    bad = [r for r in records if not _verified(r) and r.get("acceptance") is not None]
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "# 已验证环境",
        "",
        "> 本文件由 `scripts/verified_envs.py render` 生成，**不要手改**；README「已验证环境」表引用这里。",
        "> 一个组合进表的条件：`acceptance/run_acceptance.py` 全部节点无失败，且 **N10（金样规范化摘要）通过**。",
        "> 「跳过」的节点（如本机没有 AutoCAD 时的 N13 DWG 转换）单独列出，不算失败。",
        "> 确定性口径：在下表环境下，同一份 plan 渲染出的图纸规范化图元摘要一致（坐标取 6 位小数，覆盖几何与文字实体，"
        "不含文件头与句柄）。不在表里的环境不作承诺。",
        "",
        "生成日期：%s　记录数：%d（进表 %d）" % (now, len(records), len(ok)),
        "",
        "| 环境 | 平台 | 架构 | Python | cadquery-ocp | 验收 | 跳过 | N10 | 单元测试 | 安装 | 提交 | 日期 | 来源 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in ok:
        a = r["acceptance"]
        inst = ("%d s（%s）" % (r["install_seconds"], r.get("install_method") or "")
                if r.get("install_seconds") else "—")
        lines.append("| %s | %s | %s | %s | %s | %s/%s | %s | %s | %s | %s | `%s` | %s | %s |" % (
            r["label"], _platform_name(r), r["arch"], r["python"], r.get("cadquery_ocp") or "?",
            a["passed"], a["total"], "、".join(a.get("skipped_nodes") or []) or "—", a["n10"],
            (a.get("pytest") or "").split(",")[0], inst, r.get("commit") or "?", r["date"],
            r.get("source") or ""))
    if not ok:
        lines.append("| （暂无：CI 矩阵尚未运行） | | | | | | | | | | | | |")
    lines += ["", "## 未通过 / 未完成（不进上表）", ""]
    if bad:
        lines += ["| 环境 | 平台 | Python | 验收 | 失败节点 | 来源 |", "| --- | --- | --- | --- | --- | --- |"]
        for r in bad:
            a = r.get("acceptance") or {}
            lines.append("| %s | %s | %s | %s/%s | %s | %s |" % (
                r["label"], _platform_name(r), r["python"], a.get("passed"), a.get("total"),
                "、".join(a.get("failed_nodes") or []) or "—", r.get("source") or ""))
    else:
        lines.append("无。")
    verified_md = "\n".join(lines) + "\n"

    t = [
        "# 安装时长（实测）",
        "",
        "> 由 `scripts/verified_envs.py render` 从各环境的记录生成，**不要手改**。时长 = 从建虚拟环境到依赖装完"
        "（不含下载 Python 本身）；强依赖网络，表里写明了测量时的网络与 pip 缓存状态。",
        "",
        "| 环境 | 平台 | Python | 方式 | 时长 | 网络 / 缓存 | 日期 | 来源 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    timed = [r for r in records if r.get("install_seconds")]
    for r in timed:
        t.append("| %s | %s | %s | %s | %d s（约 %.1f 分钟） | %s | %s | %s |" % (
            r["label"], _platform_name(r), r["python"], r.get("install_method") or "",
            r["install_seconds"], r["install_seconds"] / 60.0, r.get("network") or "",
            r["date"], r.get("source") or ""))
    if not timed:
        t.append("| （暂无记录） | | | | | | | |")
    return {"verified-environments.md": verified_md, "install-times.md": "\n".join(t) + "\n"}


def cmd_render(args) -> int:
    recs = []
    for p in sorted(Path(args.records).rglob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(d, dict) and "acceptance" in d and "label" in d and "os" in d:
            recs.append(d)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in render(recs).items():
        (out / name).write_text(text, encoding="utf-8")
        print("已生成：%s" % (out / name))
    print("记录 %d 条，进「已验证环境」表 %d 条" % (len(recs), sum(1 for r in recs if _verified(r))))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="在被验证的环境里记一条")
    r.add_argument("--acceptance", help="run_acceptance.py 输出的结果 JSON（只记安装时长时可不给）")
    r.add_argument("--install-timing", help="安装脚本写的 install-timing.json（取最后一次安装）")
    r.add_argument("--label", required=True, help="环境名，如 ubuntu-22.04 / macos-14 / 本机 MacBook")
    r.add_argument("--install-seconds", type=int, default=0)
    r.add_argument("--install-method", default="")
    r.add_argument("--network", default="")
    r.add_argument("--source", default="", help="来源：CI 运行链接 / 「本机实测」")
    r.add_argument("--commit", default="")
    r.add_argument("--out", required=True)
    g = sub.add_parser("render", help="把记录汇总成 docs/deploy 下的两张表")
    g.add_argument("--records", required=True)
    g.add_argument("--out-dir", default=str(REPO / "docs" / "deploy"))
    args = ap.parse_args(argv)
    return cmd_record(args) if args.cmd == "record" else cmd_render(args)


if __name__ == "__main__":
    raise SystemExit(main())
