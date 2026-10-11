#!/usr/bin/env python3
"""分段基准：接收→解析→GLB→(候选)→渲染→标注→QA→导出（WP1 交付 6）。

设计（回应 Codex R0 S1-08「计时路径不一致」）：

* 在**同一台机器、同一组固定种子生成的样例**上测，每个分段单独计时，三次取中位。
* 四种缓存状态分开定义：无缓存 / HLR 全命中 / 改 plan 后重排（HLR 命中、版面重算）/ 重出图。
* 三档规模由 ``make_demo_assemblies.py --scaled`` 生成（50/200/1000 实例，可配，1000 档较慢，
  默认不跑）。**候选分图**分段 WP2 才有，这里留空位（null）。
* 用 ``--repo`` 指向另一棵工作树即可跑「改前」（pisen-ailab-v1），得到同机同样例的对照——
  本脚本只通过子进程调用 ``<repo>/scripts/*.py``，不 import 被测树的内部实现。
* 另有一段 **worker 摊销** 对照：同一工程连续渲染 K 次，比较常驻 worker 与每次起子进程两条路径
  （WP1-3 的真正收益在 Web 路径：OCP 只导入一次）。改前树没有 worker.py，则只测子进程一侧。

``--json`` 输出机器可读结果；默认再打印一张 Markdown 表，直接贴进报告。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent


def _run(repo: Path, args: List[str], env_extra: Optional[dict] = None,
         timeout: float = 1800.0) -> "subprocess.CompletedProcess":
    import os
    env = dict(os.environ)
    env.update({"OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"})
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable] + [str(a) for a in args],
                          capture_output=True, text=True, cwd=str(repo), env=env, timeout=timeout)


def _time(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def _median(xs: List[float]) -> float:
    return round(statistics.median(xs), 4) if xs else None


def _plan_for(assembly_path: Path, step_path: Path, caption: str = "整体结构示意图") -> dict:
    """A minimal valid figure-plan for a scaled assembly: one assembly figure over all parts,
    a Chinese term per prototype family (so numerals issue)."""
    asm = json.loads(assembly_path.read_text(encoding="utf-8"))
    families = sorted({p["name"].rsplit("-", 1)[0] for p in asm.get("parts", [])})
    term_zh = {"PLATE": "基板", "PIN": "销轴", "DISC": "垫片", "BALL": "球头",
               "RING": "密封圈", "SCREW": "螺钉"}
    terms = [{"selector": fam + "-*", "term": term_zh.get(fam, "构件%d" % i),
              "label": "none" if fam == "SCREW" else "once"}
             for i, fam in enumerate(families)]
    return {
        "schema": "patent-figure-plan/1",
        "source": {"step": str(step_path), "include": ["*"], "exclude": []},
        "terms": terms,
        "figures": [{"id": "fig1", "caption": caption, "kind": "assembly", "members": ["*"]}],
        "layout": {"view": "iso", "explode_axis": "auto", "axis_angle": "auto",
                   "density": "normal", "max_labels_per_figure": 20,
                   "engineering_table": False},
    }


def _fixture(repo: Path, work: Path, n: int, seed: int) -> Path:
    """Generate (once) a scaled STEP fixture using THIS repo's deterministic builder."""
    out = work / ("scaled-%d.stp" % n)
    if not out.is_file():
        proc = _run(_REPO, [_REPO / "scripts" / "make_demo_assemblies.py",
                            "-o", work, "--scaled", n, "--seed", seed])
        if proc.returncode != 0:
            raise RuntimeError("生成规模样例失败：%s" % (proc.stderr or proc.stdout)[-400:])
    return out


def bench_segments(repo: Path, step: Path, work: Path, reps: int) -> Dict[str, object]:
    """Per-segment timings × 4 cache states, median over ``reps``."""
    rp = repo / "scripts" / "render_patent_figure.py"
    seg: Dict[str, List[float]] = {k: [] for k in
                                   ("receive", "analyze", "glb", "render", "annotate", "qa", "export")}
    states: Dict[str, List[float]] = {"no_cache": [], "hlr_hit": [], "plan_partial": [], "rerender": []}

    for _ in range(reps):
        root = work / ("run-%d" % int(time.perf_counter() * 1e6))
        root.mkdir(parents=True, exist_ok=True)
        local_step = root / "model.stp"
        seg["receive"].append(_time(lambda: shutil.copy2(step, local_step)))

        asm = root / "assembly.json"
        seg["analyze"].append(_time(lambda: _run(
            repo, [repo / "scripts" / "analyze_assembly.py", local_step, "-o", asm]).check_returncode()))

        glb = root / "model.glb"
        seg["glb"].append(_time(lambda: _run(
            repo, [repo / "scripts" / "export_step_glb.py", local_step, "-o", glb])))

        plan = root / "plan.json"
        plan.write_text(json.dumps(_plan_for(asm, local_step), ensure_ascii=False, indent=2),
                        encoding="utf-8")
        cache = root / "cache"
        out = root / "out"

        rcs: List[int] = seg.setdefault("_render_rcs", [])  # type: ignore
        figcounts: List[int] = seg.setdefault("_render_figcounts", [])  # type: ignore

        def render(o, c):
            return _run(repo, [rp, plan, "--assembly", asm, "-o", o, "--cache", c])

        # 无缓存：空 cache 首渲
        t = time.perf_counter(); r1 = render(out, cache); dt = time.perf_counter() - t
        states["no_cache"].append(round(dt, 4)); seg["render"].append(round(dt, 4))
        rcs.append(r1.returncode)
        figcounts.append(len([p for p in out.glob("*.dxf")
                              if not p.stem.endswith(("_annotated", "_filing", "_engineering"))]))
        # HLR 全命中：同 plan 同 cache 再渲
        out2 = root / "out2"
        states["hlr_hit"].append(_time(lambda: render(out2, cache)))
        # 改 plan 后重排：改标题（几何/视角不变 → HLR 命中、版面重算）
        plan.write_text(json.dumps(_plan_for(asm, local_step, caption="整体结构示意图（修订）"),
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        out3 = root / "out3"
        states["plan_partial"].append(_time(lambda: render(out3, cache)))
        # 重出图：清空 cache 再冷渲
        shutil.rmtree(cache, ignore_errors=True)
        out4 = root / "out4"
        states["rerender"].append(_time(lambda: render(out4, cache)))

        # annotate / qa / export：以首渲产物为输入。密集装配图常过不了 QA 而被隔离到 diag/，
        # 但分段计时衡量的是操作成本而非是否过闸，所以 out/ 没有就从 diag/ 取一张。
        figs = sorted(out.glob("fig*.dxf")) or sorted((out / "diag").glob("fig*.dxf"))
        if figs:
            fig = figs[0]
            ann = root / "fig1_annotated.dxf"
            numerals = out / "reference-numerals.json"
            seg["annotate"].append(_time(lambda: _run(
                repo, [repo / "scripts" / "annotate_figure_sheet.py", fig,
                       "--numerals", numerals, "--figure-number", 1, "-o", ann])))
            seg["qa"].append(_time(lambda: _run(
                repo, [repo / "scripts" / "qa_patent_figure.py", fig, "--kind", "assembly"])))

            def do_zip():
                z = root / "bundle.zip"
                with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in sorted(out.rglob("*")):
                        if f.is_file():
                            zf.write(f, f.relative_to(out).as_posix())
            seg["export"].append(_time(do_zip))

    render_rcs = seg.pop("_render_rcs", [])
    render_figcounts = seg.pop("_render_figcounts", [])
    return {
        "segments_median_s": {k: _median(v) for k, v in seg.items()},
        "candidates_median_s": None,  # WP2
        "cache_states_render_median_s": {k: _median(v) for k, v in states.items()},
        "render_returncodes": render_rcs,       # R1 #13: record the actual exit path
        "render_figcounts": render_figcounts,
        "reps": reps,
    }


def bench_worker_amortization(repo: Path, step: Path, work: Path, k: int) -> Dict[str, object]:
    """K sequential renders of the same project: resident worker vs per-call subprocess.

    This is where WP1-3 pays off — the worker imports OCP once for all K renders, the
    subprocess path pays it K times. Reported as total and per-render seconds.
    """
    root = work / "amort"
    root.mkdir(parents=True, exist_ok=True)
    local_step = root / "model.stp"
    shutil.copy2(step, local_step)
    asm = root / "assembly.json"
    _run(repo, [repo / "scripts" / "analyze_assembly.py", local_step, "-o", asm]).check_returncode()
    plan = root / "plan.json"
    plan.write_text(json.dumps(_plan_for(asm, local_step), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    rp = repo / "scripts" / "render_patent_figure.py"

    def _figcount(d: Path) -> int:
        return len([p for p in d.glob("*.dxf")
                    if not p.stem.endswith(("_annotated", "_filing", "_engineering"))]) \
            if d.is_dir() else 0

    def subprocess_path():
        """Returns (total_s, [rc...], [figcount...]) so equivalence can be judged (R1 #13)."""
        rcs, figs = [], []
        t0 = time.perf_counter()
        for i in range(k):
            outdir = root / ("sp-%d" % i)
            pc = _run(repo, [rp, plan, "--assembly", asm, "-o", outdir],
                      env_extra={"PFK_WORKER": "0"})
            rcs.append(pc.returncode)
            figs.append(_figcount(outdir))
        return time.perf_counter() - t0, rcs, figs

    sp_total, sp_rcs, sp_figs = subprocess_path()
    out: Dict[str, object] = {"k": k, "subprocess_total_s": round(sp_total, 3),
                              "subprocess_per_render_s": round(sp_total / k, 3),
                              "subprocess_returncodes": sp_rcs, "subprocess_figcounts": sp_figs,
                              "subprocess_path": "per-call subprocess (PFK_WORKER=0)"}

    # resident worker — only if the tree under test has worker.py
    worker_py = repo / "scripts" / "patent_figure" / "worker.py"
    if worker_py.is_file():
        driver = (
            "import sys, time, json, pathlib; sys.path.insert(0, %r)\n"
            "from patent_figure.worker import Worker\n"
            "w = Worker(); rp, plan, asm, root = %r, %r, %r, %r\n"
            "rcs, figs = [], []\n"
            "t0 = time.perf_counter()\n"
            "for i in range(%d):\n"
            "    o = root + ('/wk-%%d' %% i)\n"
            "    p = w.run([rp, plan, '--assembly', asm, '-o', o]); rcs.append(p.returncode)\n"
            "    d = pathlib.Path(o); figs.append(len([q for q in d.glob('*.dxf')"
            " if not q.stem.endswith(('_annotated','_filing','_engineering'))]) if d.is_dir() else 0)\n"
            "w.close()\n"
            "print(json.dumps({'total': time.perf_counter() - t0, 'rcs': rcs, 'figs': figs}))\n"
            % (str(repo / "scripts"), str(rp), str(plan), str(asm), str(root), k)
        )
        proc = _run(repo, ["-c", driver])
        try:
            res = json.loads(proc.stdout.strip().splitlines()[-1])
            wk_total = res["total"]
            out["worker_total_s"] = round(wk_total, 3)
            out["worker_per_render_s"] = round(wk_total / k, 3)
            out["worker_returncodes"] = res["rcs"]
            out["worker_figcounts"] = res["figs"]
            out["worker_path"] = "resident worker (OCP imported once)"
            # Only report a speedup when the two paths did EQUIVALENT work — same exit codes
            # and same figure counts per render (R1 #13) — otherwise it would compare paths
            # that stopped at different stages.
            equivalent = (sp_rcs == res["rcs"] and sp_figs == res["figs"])
            out["equivalent_work"] = equivalent
            out["speedup_x"] = (round(sp_total / wk_total, 2)
                                if (equivalent and wk_total) else None)
            if not equivalent:
                out["note"] = "两条路径工作量不等价（退出码/图数不同），不计加速比"
        except Exception:
            out["worker_error"] = (proc.stderr or proc.stdout)[-300:]
    else:
        out["worker_total_s"] = None
        out["note"] = "该树无 worker.py（改前基线），只测子进程路径"
    return out


def _md_table(result: dict) -> str:
    lines = ["", "### 分段中位耗时（秒）", "", "| 分段 | 中位 |", "| --- | --- |"]
    for tier in result["tiers"]:
        lines.append("| **规模 %d 实例** | |" % tier["n_instances"])
        segs = tier["segments"]["segments_median_s"]
        for k in ("receive", "analyze", "glb", "render", "annotate", "qa", "export"):
            lines.append("| %s | %s |" % (k, segs.get(k)))
        cs = tier["segments"]["cache_states_render_median_s"]
        lines.append("| render·无缓存 | %s |" % cs["no_cache"])
        lines.append("| render·HLR全命中 | %s |" % cs["hlr_hit"])
        lines.append("| render·改plan重排 | %s |" % cs["plan_partial"])
        lines.append("| render·重出图 | %s |" % cs["rerender"])
        am = tier.get("worker_amortization") or {}
        lines.append("| worker 摊销 ×%s（子进程/worker 秒） | %s / %s |"
                     % (am.get("k"), am.get("subprocess_total_s"), am.get("worker_total_s")))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=_REPO,
                    help="被测工作树（默认本树）。指向 pisen-ailab-v1 工作树即得改前基线。")
    ap.add_argument("--tiers", type=int, nargs="+", default=[50, 200],
                    help="规模档（实例数）。1000 较慢，需要时显式加上。")
    ap.add_argument("--reps", type=int, default=3, help="每项重复次数，取中位（默认 3）")
    ap.add_argument("--amortize-k", type=int, default=3, help="worker 摊销对照的连续渲染次数")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--work", type=Path, default=None, help="工作目录（默认临时目录）")
    ap.add_argument("--json", dest="json_out", type=Path, default=None)
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    import tempfile
    work = (args.work or Path(tempfile.mkdtemp(prefix="pfk-bench-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)

    def _commit(r: Path) -> str:
        try:
            p = subprocess.run(["git", "-C", str(r), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True)
            return p.stdout.strip() or "?"
        except Exception:
            return "?"

    result: dict = {"repo": str(repo), "commit": _commit(repo),
                    "python": sys.version.split()[0], "reps": args.reps,
                    "seed": args.seed, "tiers": []}
    for n in args.tiers:
        step = _fixture(repo, work, n, args.seed)
        step_sha = hashlib.sha256(step.read_bytes()).hexdigest()
        print("  规模 %d 实例：分段计时…" % n, file=sys.stderr, flush=True)
        seg = bench_segments(repo, step, work / ("t%d" % n), args.reps)
        print("  规模 %d 实例：worker 摊销…" % n, file=sys.stderr, flush=True)
        am = bench_worker_amortization(repo, step, work / ("t%d-am" % n), args.amortize_k)
        result["tiers"].append({"n_instances": n, "input_step_sha256": step_sha,
                                "segments": seg, "worker_amortization": am})

    if args.json_out:
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(_md_table(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
