#!/usr/bin/env python3
"""README「性能（实测）」第一张表的测量脚本：三个公开示例在真实工作台上的金路径分段计时。

    python3 docs/assets/measure_readme.py --reps 3 --out docs/assets/readme/runs/perf-golden.json
    python3 docs/assets/measure_readme.py --reps 3 --no-suggest --out docs/assets/readme/runs/perf-golden-cold.json

每次测量 = 全新临时数据目录 + 全新临时配置目录 + 刚启动的工作台进程（几何缓存为空），**AI 通道关闭**
（配置 provider: none，且环境变量 PFK_AI_PROVIDER=none），走 REST（与界面同一组接口），顺序与界面金路径一致：
  ① 载入示例（首次：现场生成演示 STEP + 解析装配 + 生成 3D 网格）
  ② 候选分图（生成候选 + 每个候选实际渲染并过 QA）；--no-suggest 时跳过，用来测几何缓存完全为空的首次出图
  ③ 首次出图（示例计划）　④ 重出图（计划不变）　⑤ 改计划后出图（改一个零件中文名）
  ⑥ 导出附图包（递交版 + 交底版；PDF + Word + DXF；300 dpi；standard 版心；含逐页核验）
计时取作业自身的 created_at → updated_at（服务端墙钟），另记客户端从提交到看到终态的时间。
输出 JSON 含：命令行、机型 / 核数 / 内存 / 系统 / Python / cadquery-ocp / 提交号 / 工作树是否有改动、每次样本、每项中位数。
不读写 ~/.patent-workbench 与 ~/.config/patent-workbench，不调用任何大模型；结束后删除临时目录。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TMP = Path(tempfile.mkdtemp(prefix="pfk-measure-"))
os.environ.setdefault("WP3B_TOKEN", "measure-" + secrets.token_hex(12))
os.environ.setdefault("WP3B_SCRATCH", str(TMP / "logs"))
sys.path.insert(0, str(REPO / "webui" / "dev"))
import e2e_live as E  # noqa: E402  已对真实后端验证过的后端工具

EXS = ["gearbox", "vise", "valve"]
STEPS = ("load_example", "suggest", "render_first", "render_again", "render_after_edit", "export")


def _sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def machine_env() -> dict:
    sysname = platform.system()
    if sysname == "Darwin":
        machine = _sh(["sysctl", "-n", "machdep.cpu.brand_string"])
        mem = int(_sh(["sysctl", "-n", "hw.memsize"]) or 0) // 2 ** 30
        os_name = "macOS " + platform.mac_ver()[0]
    else:
        machine = platform.processor() or platform.machine()
        try:
            mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2 ** 30
        except (ValueError, OSError, AttributeError):
            mem = 0
        os_name = platform.platform()
    env = {"machine": machine, "arch": platform.machine(), "ncpu": os.cpu_count(), "mem_gb": mem, "os": os_name,
           "python": platform.python_version(),
           "commit": _sh(["git", "-C", str(REPO), "rev-parse", "--short=7", "HEAD"]) or "?",
           "dirty_files": [l[3:] for l in _sh(["git", "-C", str(REPO), "status", "--short"]).splitlines()],
           "date": time.strftime("%Y-%m-%d %H:%M")}
    try:
        import OCP  # noqa: F401
        env["cadquery_ocp"] = getattr(OCP, "__version__", "?")
    except Exception:
        env["cadquery_ocp"] = "?"
    return env


def job_wait(be, jid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, j, _ = be.rest("GET", "/v1/jobs/%s" % jid)
        if st == 200 and j.get("state") in ("succeeded", "failed", "cancelled", "interrupted"):
            return j, time.time() - t0
        time.sleep(0.05)
    raise RuntimeError("作业超时：" + jid)


def srv(j):
    return round(float(j["updated_at"]) - float(j["created_at"]), 3)


def run_one(ex, rep, port, suggest=True):
    data, cfgd = TMP / ("data-%s-%d" % (ex, rep)), TMP / ("cfg-%s-%d" % (ex, rep))
    be = E.Backend("measure", port, data, cfgd, {"provider": "none"}, {"PFK_AI_PROVIDER": "none"}, ["--no-example"])
    be.up()
    pid = "example-" + ex
    out = {"example": ex, "rep": rep}
    try:
        st, caps, _ = be.rest("GET", "/v1/capabilities")
        assert st == 200 and not (caps.get("ai") or {}).get("configured"), "AI 通道必须关闭"
        st, j, _ = be.rest("POST", "/v1/projects/from-example", {"example": ex}); assert st == 202, j
        jj, cl = job_wait(be, j["job_id"]); assert jj["state"] == "succeeded", jj
        out["load_example"] = {"server_s": srv(jj), "client_s": round(cl, 3)}
        st, parts, _ = be.rest("GET", "/v1/projects/%s/parts?only_unnamed=false" % pid)
        if st == 200:
            rows = parts.get("parts") or []
            out["model"] = {"part_kinds": len(rows), "instances": sum(int(r.get("instances") or 1) for r in rows)}
        if suggest:
            st, j, _ = be.rest("POST", "/v1/projects/%s/plan/suggest" % pid, {}); assert st == 202, j
            jj, cl = job_wait(be, j["job_id"]); assert jj["state"] == "succeeded", jj
            cands = (jj.get("result") or {}).get("candidates") or []
            out["suggest"] = {"server_s": srv(jj), "client_s": round(cl, 3), "n": len(cands),
                              "pass": sum(1 for c in cands if c.get("status") == "pass")}
        else:
            out["suggest"] = {"server_s": 0.0, "client_s": 0.0, "n": 0, "pass": 0, "skipped": True}
        plan, _ = be.plan(pid)
        out["figures"] = len(plan["figures"])

        def render(label):
            _, et = be.plan(pid)
            st, j, _ = be.rest("POST", "/v1/projects/%s/render" % pid, {"plan_etag": et.strip('"')}); assert st == 202, j
            jj, cl = job_wait(be, j["job_id"])
            assert jj["state"] == "succeeded", (label, jj.get("error"))
            out[label] = {"server_s": srv(jj), "client_s": round(cl, 3)}
            return jj
        render("render_first")
        render("render_again")
        pl, et = be.plan(pid)
        t = next(t for t in pl["terms"] if t.get("term"))
        t["term"] = t["term"] + "组件"
        st, r, _ = be.rest("PUT", "/v1/projects/%s/plan" % pid, pl, {"If-Match": et}); assert st == 200, r
        rj = render("render_after_edit")
        _, et = be.plan(pid)
        body = {"variant": "both", "formats": ["pdf", "docx", "dxf"], "dpi": 300, "sheet_preset": "standard",
                "render_job_id": rj["id"], "plan_etag": et.strip('"')}
        st, j, _ = be.rest("POST", "/v1/projects/%s/export" % pid, body); assert st == 202, j
        jj, cl = job_wait(be, j["job_id"])
        res = jj.get("result") or {}
        assert res.get("ok"), ("导出未通过", res.get("error"))
        out["export"] = {"server_s": srv(jj), "client_s": round(cl, 3), "ok": res.get("ok"),
                         "items": len(res.get("items") or [])}
    finally:
        be.down()
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(cfgd, ignore_errors=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--port", type=int, default=8815)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--no-suggest", action="store_true", help="跳过候选分图：测几何缓存完全为空时的首次出图")
    a = ap.parse_args()
    env = machine_env()
    env["cache"] = ("每次测量：全新临时数据目录 + 新启动的工作台进程（几何缓存为空）；"
                    + ("首次出图前不跑候选分图" if a.no_suggest else "首次出图排在候选分图之后（候选试渲染已让部分零件的消隐结果进缓存）"))
    env["ai"] = "关闭（provider: none；PFK_AI_PROVIDER=none）"
    runs = []
    try:
        for rep in range(a.reps):
            for ex in EXS:
                r = run_one(ex, rep, a.port, suggest=not a.no_suggest)
                print(json.dumps(r, ensure_ascii=False), flush=True)
                runs.append(r)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    med = {}
    for ex in EXS:
        rs = [r for r in runs if r["example"] == ex]
        med[ex] = {k: round(statistics.median(r[k]["server_s"] for r in rs), 2) for k in STEPS}
        med[ex].update(figures=rs[0]["figures"], model=rs[0].get("model"),
                       candidates="%d/%d" % (rs[0]["suggest"]["pass"], rs[0]["suggest"]["n"]))
    doc = {"command": "python3 docs/assets/measure_readme.py " + " ".join(sys.argv[1:]),
           "timing": "服务端作业 created_at → updated_at（秒）；median = %d 次样本的中位数" % a.reps,
           "env": env, "runs": runs, "median": med}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"env": env, "median": med}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
