#!/usr/bin/env python3
"""Plan Studio — a local browser UI for hand-editing figure-plan.json.

    python3 scripts/plan_studio.py ASM.stp

The UI is the plan's third author (LLM draft, human correction, scripts render). Its ONLY
output is figure-plan.json; every render goes through the same CLIs and QA gates the model
route uses, so a hand-made plan must reproduce bit-identically like any other. The page
exposes intent-level controls only — which part belongs to which figure, what it is called,
whether it carries a numeral. No coordinate, text-height or spacing control exists here,
for the same reason those were taken away from the model in v2.

Local-only by design: binds 127.0.0.1, verifies the Host header (DNS-rebinding guard), and
requires a per-session random token on every API call (hostile-webpage guard). The STEP,
the GLB and every artefact stay inside a work directory next to the STEP file.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from types import SimpleNamespace

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request, Response
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    print("Plan Studio 需要 fastapi 与 uvicorn（仅本工具需要，渲染链路不依赖）：\n"
          "  python3 -m pip install fastapi uvicorn", file=sys.stderr)
    raise SystemExit(1)

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import studio_ext  # noqa: E402  规范标注 / 流程图 / AI 起草 / Word 导出

REPO = SCRIPTS.parent
WEBUI = REPO / "webui"
PY = sys.executable or "python3"

#: SVG stroke widths in millimetres, preview-only (the DXF is the deliverable, this is a picture
#: of it). GEOM heavier than LEADER matches how the sheet is meant to read on paper.
SVG_STROKE = {"GEOM": 0.35, "HIDDEN": 0.2, "LEADER": 0.18, "TABLE": 0.25}
SVG_MARGIN_MM = 6.0
HISTORY_KEEP = 200


# ----------------------------------------------------------------------------- work dir


class Workspace:
    """Everything Plan Studio knows lives in ``<step stem>.plan-studio/`` beside the STEP."""

    def __init__(self, step: Path, workdir: Path | None) -> None:
        self.step = step.resolve()
        self.root = (workdir or self.step.parent / (self.step.stem + ".plan-studio")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.assembly = self.root / "assembly.json"
        self.plan = self.root / "plan.json"
        self.glb = self.root / "model.glb"
        self.out = self.root / "out"
        self.cache = self.root / "cache"
        self.history = self.root / ".plan-history"
        self.state = self.root / "state.json"

    def stale(self) -> bool:
        if not self.state.is_file():
            return True
        try:
            recorded = json.loads(self.state.read_text(encoding="utf-8"))
        except ValueError:
            return True
        return (recorded.get("step_mtime") != self.step.stat().st_mtime_ns
                or recorded.get("step_size") != self.step.stat().st_size)

    def remember(self) -> None:
        self.state.write_text(json.dumps({
            "step": str(self.step),
            "step_mtime": self.step.stat().st_mtime_ns,
            "step_size": self.step.stat().st_size,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def snapshot_plan(self) -> None:
        if not self.plan.is_file():
            return
        self.history.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + ("-%03d" % (time.time_ns() // 1_000_000 % 1000))
        shutil.copy2(self.plan, self.history / ("plan-%s.json" % stamp))
        old = sorted(self.history.glob("plan-*.json"))
        for path in old[:-HISTORY_KEEP]:
            path.unlink()


def run_cli(args: list, timeout: int = 1800) -> subprocess.CompletedProcess:
    """Every render-chain step goes through the same CLIs the model route uses — never an
    import of package internals. That keeps the human path and the model path behind one
    set of gates with one set of error messages."""
    return subprocess.run([PY] + args, capture_output=True, text=True,
                          timeout=timeout, cwd=str(REPO))


def prepare(ws: Workspace, quiet: bool = False) -> None:
    def say(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    if ws.stale() or not ws.assembly.is_file():
        say("· analyze：解析装配体（大模型件首次约半分钟）…")
        proc = run_cli([str(SCRIPTS / "analyze_assembly.py"), str(ws.step),
                       "-o", str(ws.assembly)])
        if proc.returncode != 0:
            print(proc.stdout[-2000:] + proc.stderr[-2000:], file=sys.stderr)
            raise SystemExit("analyze 失败，无法启动 Studio")
    if ws.stale() or not ws.glb.is_file():
        say("· glb：导出 3D 预览模型…")
        proc = run_cli([str(SCRIPTS / "export_step_glb.py"), str(ws.step), "-o", str(ws.glb)])
        if proc.returncode != 0:
            print(proc.stdout[-2000:] + proc.stderr[-2000:], file=sys.stderr)
            raise SystemExit("GLB 导出失败，无法启动 Studio")
    if not ws.plan.is_file():
        say("· plan：生成空白计划（零件清单来自 assembly.json）…")
        ws.plan.write_text(json.dumps(blank_plan(ws), ensure_ascii=False, indent=2),
                           encoding="utf-8")
    ws.remember()


def blank_plan(ws: Workspace) -> dict:
    assembly = json.loads(ws.assembly.read_text(encoding="utf-8"))
    return {
        "schema": "patent-figure-plan/1",
        "source": {"step": str(ws.step), "include": ["*"], "exclude": []},
        # One empty term row per part: the validator's W_UNLABELLED_PART / empty-term errors
        # then double as the user's to-do list instead of a blank page.
        "terms": [{"selector": part["name"], "term": "", "label": "once"}
                  for part in assembly.get("parts", [])],
        "figures": [{"id": "fig1", "caption": "整体结构示意图",
                     "kind": "assembly", "members": ["*"]}],
        "layout": {"view": "iso", "explode_axis": "auto", "axis_angle": "auto",
                   "density": "normal", "max_labels_per_figure": 20,
                   "engineering_table": False},
    }


# ----------------------------------------------------------------------------- BOM

#: Header keywords for locating the code / name / qty columns in a BOM sheet. Chinese
#: manufacturing BOMs vary wildly; these cover the common house styles. Matching is
#: substring, first hit wins, scanned over the first BOM_HEADER_SCAN rows.
BOM_CODE_HEADERS = ("物料编码", "物料代码", "物料编号", "图号", "件号", "编码", "代号",
                    "料号", "零件号", "part no", "p/n", "partnumber", "code")
BOM_NAME_HEADERS = ("物料名称", "零件名称", "名称", "品名", "描述", "规格名称",
                    "description", "name")
BOM_QTY_HEADERS = ("数量", "用量", "qty", "quantity")
BOM_HEADER_SCAN = 30


def _bom_rows_from_table(rows: list) -> dict:
    """Locate the header row and pull (code -> {name, qty}) out of a 2-D table."""
    header_idx = code_col = name_col = qty_col = None
    for i, row in enumerate(rows[:BOM_HEADER_SCAN]):
        cells = [str(c or "").strip().lower() for c in row]
        c_col = n_col = q_col = None
        for j, cell in enumerate(cells):
            if c_col is None and any(k in cell for k in BOM_CODE_HEADERS):
                c_col = j
            elif n_col is None and any(k in cell for k in BOM_NAME_HEADERS):
                n_col = j
            elif q_col is None and any(k in cell for k in BOM_QTY_HEADERS):
                q_col = j
        if c_col is not None and n_col is not None:
            header_idx, code_col, name_col, qty_col = i, c_col, n_col, q_col
            break
    if header_idx is None:
        raise ValueError("找不到表头：需要同时含「件号/物料编码」列与「名称/品名」列")
    out = {}
    for row in rows[header_idx + 1:]:
        code = str(row[code_col] or "").strip() if code_col < len(row) else ""
        name = str(row[name_col] or "").strip() if name_col < len(row) else ""
        if not code or not name:
            continue
        qty = None
        if qty_col is not None and qty_col < len(row):
            try:
                qty = int(float(row[qty_col]))
            except (TypeError, ValueError):
                qty = None
        out.setdefault(code, {"name": name, "qty": qty})
    return out


def parse_bom(filename: str, data: bytes) -> dict:
    """xlsx via openpyxl（全部工作表都扫）, csv/tsv via stdlib. Returns code -> {name, qty}."""
    suffix = Path(filename).suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        merged, errors = {}, []
        for sheet in wb.worksheets:
            rows = [[c for c in r] for r in sheet.iter_rows(values_only=True)]
            try:
                found = _bom_rows_from_table(rows)
            except ValueError as exc:
                errors.append("%s: %s" % (sheet.title, exc))
                continue
            for code, info in found.items():
                merged.setdefault(code, info)
        if not merged:
            raise ValueError("；".join(errors) or "工作簿为空")
        return merged
    if suffix in (".csv", ".tsv", ".txt"):
        import csv
        import io
        text = data.decode("utf-8-sig", errors="replace")
        dialect = "excel-tab" if suffix == ".tsv" else "excel"
        rows = list(csv.reader(io.StringIO(text), dialect))
        return _bom_rows_from_table(rows)
    raise ValueError("不支持的格式 %s——请用 .xlsx 或 .csv" % suffix)


_INSTANCE_SUFFIX = re.compile(r"(?:[_-]\d+)+$")


def match_bom_to_parts(bom: dict, part_names: list) -> dict:
    """Match BOM codes to STEP part names — conservatively.

    The first draft also ran a longest-prefix pass. On a real assembly it filled fourteen
    distinct gears, shells and bearings sharing one export-tool stem (``<HASH>_<seq>`` style
    names) with a single bearing's name, and every ``BREP_*`` blob with one cover's name:
    recall bought with wrong names, which on a patent figure is worse than no name. So only
    two passes survive:

    1. raw name == raw code;
    2. instance-suffix-stripped equality, accepted only when the stripped key is at least
       MIN_STEM chars AND unique on both sides (one part, one BOM row). Stripping ``_1_1``
       style suffixes is what lets ``<CODE>_1_1`` meet its drawing code ``<CODE>``; the
       uniqueness demand is what keeps ``BREP_<n>`` -> ``BREP`` from meeting every other
       ``BREP_*``.

    Whatever stays unmatched is reported honestly and left for the human or the model.
    """
    MIN_STEM = 6
    matched = {}
    codes = set(bom)
    for name in part_names:
        if name in codes:
            matched[name] = {"code": name, "name": bom[name]["name"], "via": "exact"}

    def stem_index(values):
        index = {}
        for v in values:
            stem = _INSTANCE_SUFFIX.sub("", v)
            if len(stem) >= MIN_STEM:
                index.setdefault(stem, []).append(v)
        return index

    name_stems = stem_index(n for n in part_names if n not in matched)
    code_stems = stem_index(codes)
    for stem, names in name_stems.items():
        cands = code_stems.get(stem, [])
        if len(names) == 1 and len(cands) == 1:
            matched[names[0]] = {"code": cands[0], "name": bom[cands[0]]["name"],
                                 "via": "stripped"}
    return matched


# ----------------------------------------------------------------------------- DXF -> SVG


def dxf_to_svg(dxf: Path) -> str:
    """Minimal DXF-to-SVG for THIS renderer's known output vocabulary.

    Hand-rolled instead of ezdxf's SVGBackend for one reason: the preview must be
    interactive — every NUM numeral needs ``data-numeral`` so a click can highlight the
    part in 3D — and a generic backend gives no per-entity hooks. The sheet only ever
    contains LINE / LWPOLYLINE / CIRCLE / TEXT on known layers, so sixty lines cover it.

    Y handling per the sheet's +Y-up convention: geometry sits in a ``scale(1,-1)`` group;
    text is emitted OUTSIDE that group at ``y' = -y``, because a flipped group mirrors
    glyphs. All coordinates are millimetres straight from the DXF.
    """
    import ezdxf

    doc = ezdxf.readfile(str(dxf))
    msp = doc.modelspace()
    xs, ys = [], []

    def track(x: float, y: float) -> None:
        xs.append(float(x)); ys.append(float(y))

    shapes, texts = [], []
    for e in msp:
        kind = e.dxftype()
        layer = e.dxf.layer
        if kind == "LINE":
            (x1, y1, _), (x2, y2, _) = e.dxf.start, e.dxf.end
            track(x1, y1); track(x2, y2)
            shapes.append('<line class="ly-%s" x1="%.3f" y1="%.3f" x2="%.3f" y2="%.3f"/>'
                          % (layer, x1, y1, x2, y2))
        elif kind == "LWPOLYLINE":
            pts = [(float(p[0]), float(p[1])) for p in e.get_points()]
            for x, y in pts:
                track(x, y)
            d = " ".join("%.3f,%.3f" % p for p in pts)
            tag = "polygon" if e.closed else "polyline"
            shapes.append('<%s class="ly-%s" points="%s"/>' % (tag, layer, d))
        elif kind == "CIRCLE":
            (cx, cy, _) = e.dxf.center
            r = float(e.dxf.radius)
            track(cx - r, cy - r); track(cx + r, cy + r)
            shapes.append('<circle class="ly-%s dot" cx="%.3f" cy="%.3f" r="%.3f"/>'
                          % (layer, cx, cy, r))
        elif kind == "SOLID":
            pts = [(float(e.dxf.get("vtx%d" % i)[0]), float(e.dxf.get("vtx%d" % i)[1]))
                   for i in range(3)]
            for x, y in pts:
                track(x, y)
            shapes.append('<polygon class="ly-%s solid" points="%s"/>'
                          % (layer, " ".join("%.3f,%.3f" % q for q in pts)))
        elif kind == "TEXT":
            align, p1, _ = e.get_placement()   # ezdxf: (alignment, p1, p2) — 对齐枚举在首位
            x, y = float(p1[0]), float(p1[1])
            h = float(e.dxf.height)
            track(x, y - h); track(x, y + h)
            name = align.name if hasattr(align, "name") else str(align)
            anchor = "end" if name.endswith("RIGHT") else (
                "start" if name.endswith("LEFT") else "middle")
            value = (e.dxf.text.replace("&", "&amp;").replace("<", "&lt;"))
            extra = ""
            if layer == "NUM" and value.strip().isdigit():
                extra = ' data-numeral="%s"' % value.strip()
            texts.append('<text class="ly-%s" x="%.3f" y="%.3f" font-size="%.3f" '
                         'text-anchor="%s"%s>%s</text>'
                         % (layer, x, -y, h, anchor, extra, value))
    if not xs:
        return '<svg xmlns="http://www.w3.org/2000/svg"/>'
    m = SVG_MARGIN_MM
    x0, x1 = min(xs) - m, max(xs) + m
    y0, y1 = min(ys) - m, max(ys) + m
    styles = "".join(".ly-%s{stroke-width:%.2f}" % (k, v) for k, v in SVG_STROKE.items())
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="%.3f %.3f %.3f %.3f" '
        'font-family="\'Songti SC\', \'STSong\', \'SimSun\', serif">'
        '<style>line,polyline,polygon,circle{stroke:#1a1a1a;fill:none;stroke-linecap:round}'
        'circle.dot,polygon.solid{fill:#1a1a1a;stroke:none}'
        'text.ly-NUM{font-family:Helvetica,Arial,sans-serif}'
        'text{fill:#1a1a1a;dominant-baseline:central}'
        'text[data-numeral]{cursor:pointer}text[data-numeral]:hover{fill:#0a6cbd}%s</style>'
        '<g transform="scale(1,-1)">%s</g>%s</svg>'
        % (x0, -y1, x1 - x0, y1 - y0, styles, "".join(shapes), "".join(texts))
    )


# ----------------------------------------------------------------------------- app


def _llm_status() -> dict:
    try:
        import workbench_llm
        return workbench_llm.public_settings()
    except Exception as exc:  # pragma: no cover
        return {"provider": "none", "provider_label": "大模型模块不可用：%s" % exc}


async def _run_sync(fn):
    """在线程池里跑阻塞调用（大模型、CLI），不卡住事件循环。"""
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(fn)


def build_app(ws: Workspace, token: str, home_url: str | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    render_lock = threading.Lock()
    structure_lock = threading.Lock()
    structure_job: dict = {"running": False, "started": None, "error": None, "finished": None}
    render_state: dict = {"running": False, "result": None, "started": None}
    try:
        render_state["result"] = json.loads((ws.root / "last_render.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return Response("仅限本机访问", status_code=403)
        # 挂在工作台 /p/<id>/ 之下时，scope.path 是完整路径、root_path 是前缀——
        # 只看 url.path 会让 /p/<id>/api/* 绕过 token 校验，所以按相对路径判断。
        path = request.scope.get("path", "")
        root = request.scope.get("root_path", "") or ""
        rel = path[len(root):] if root and path.startswith(root) else path
        if rel.startswith("/api/"):
            auth = request.headers.get("authorization", "")
            supplied = request.headers.get("x-studio-token") or \
                request.query_params.get("token") or \
                (auth[7:] if auth.lower().startswith("bearer ") else None)
            if not secrets.compare_digest(str(supplied or ""), token):
                return Response("token 无效——请从终端打印的完整地址进入", status_code=401)
        response = await call_next(request)
        if not rel.startswith("/api/"):
            # 前端是本机文件、无构建版本号：升级 Studio 后浏览器的 ES 模块缓存会继续
            # 跑旧代码（同页导航尤甚），排障时症状诡异。本地服务器带宽为零成本，
            # 直接禁缓存是最省心的正确解。
            response.headers["Cache-Control"] = "no-store"
        return response

    # ---- state -------------------------------------------------------------

    def load_json(path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    def validate_now() -> dict:
        proc = run_cli([str(SCRIPTS / "validate_figure_plan.py"), str(ws.plan),
                       "--assembly", str(ws.assembly),
                       "--json", str(ws.root / "issues.json")], timeout=120)
        issues = []
        if (ws.root / "issues.json").is_file():
            try:
                issues = load_json(ws.root / "issues.json").get("issues", [])
            except ValueError:
                issues = []
        return {"ok": proc.returncode == 0, "exit": proc.returncode,
                "issues": issues, "stdout": proc.stdout[-4000:]}

    @app.get("/api/state")
    def state():
        return {
            "step": str(ws.step),
            "workdir": str(ws.root),
            "assembly": load_json(ws.assembly),
            "plan": load_json(ws.plan),
            "validate": validate_now(),
            "render": render_state["result"],
            "rendering": render_state["running"],
            "home_url": home_url,
            "flowcharts": studio_ext.list_flowcharts(ws),
            "llm": _llm_status(),
            "structure": studio_ext.load_structure(ws),
            "structure_job": dict(structure_job),
        }

    @app.put("/api/plan")
    async def put_plan(request: Request):
        body = await request.json()
        plan = body.get("plan")
        if not isinstance(plan, dict):
            raise HTTPException(400, "body.plan 必须是对象")
        ws.snapshot_plan()
        ws.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"validate": validate_now()}

    @app.post("/api/bom")
    async def import_bom(request: Request):
        filename = request.query_params.get("name", "bom.xlsx")
        data = await request.body()
        if not data:
            raise HTTPException(400, "空文件")
        try:
            bom = parse_bom(filename, data)
        except ValueError as exc:
            raise HTTPException(422, "BOM 解析失败：%s" % exc)
        assembly = load_json(ws.assembly)
        names = [p["name"] for p in assembly.get("parts", [])]
        matched = match_bom_to_parts(bom, names)
        plan = load_json(ws.plan)
        ws.snapshot_plan()
        filled, kept = [], []
        by_selector = {t.get("selector"): t for t in plan.get("terms", [])}
        for part, hit in matched.items():
            row = by_selector.get(part)
            if row is None:
                continue
            if (row.get("term") or "").strip():
                kept.append(part)          # 人已填的绝不覆盖
            else:
                row["term"] = hit["name"]
                filled.append(part)
        ws.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        (ws.root / "bom.json").write_text(
            json.dumps({"file": filename, "rows": len(bom), "matched": matched},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        unmatched_parts = sorted(set(names) - set(matched))
        return {
            "plan": plan,
            "validate": validate_now(),
            "report": {
                "bom_rows": len(bom), "matched_parts": len(matched),
                "filled": len(filled), "kept_human": len(kept),
                "unmatched_parts": unmatched_parts[:40],
                "unmatched_count": len(unmatched_parts),
            },
        }

    # ---- render ------------------------------------------------------------

    def resolve_numeral_parts(numerals: dict, part_names: list) -> None:
        for entry in numerals.get("numerals", []):
            selector = entry.get("selector", "")
            entry["parts"] = sorted(n for n in part_names
                                    if fnmatch.fnmatchcase(n, selector))

    def render_job() -> None:
        result: dict = {"figures": [], "ok": False, "log": "", "numerals": None,
                        "finished": None}
        try:
            if ws.out.is_dir():
                shutil.rmtree(ws.out)
            check = validate_now()
            if not check["ok"]:
                result["log"] = "计划未通过校验，未渲染。"
                result["validate"] = check
                return
            proc = run_cli([str(SCRIPTS / "render_patent_figure.py"), str(ws.plan),
                           "--assembly", str(ws.assembly), "-o", str(ws.out),
                           "--cache", str(ws.cache)])
            result["log"] = (proc.stdout[-6000:] + "\n" + proc.stderr[-2000:]).strip()
            result["ok"] = proc.returncode == 0
            part_names = [p["name"] for p in load_json(ws.assembly).get("parts", [])]
            numerals_file = ws.out / "reference-numerals.json"
            if numerals_file.is_file():
                result["numerals"] = load_json(numerals_file)
                resolve_numeral_parts(result["numerals"], part_names)
            for dxf in sorted(ws.out.glob("*.dxf")):
                fig_id = dxf.stem
                if fig_id.endswith(("_annotated", "_filing", "_engineering")):
                    continue
                (ws.out / (fig_id + ".svg")).write_text(dxf_to_svg(dxf), encoding="utf-8")
                qa_file = ws.out / (fig_id + ".qa.json")
                qa = load_json(qa_file) if qa_file.is_file() else None
                result["figures"].append({
                    "id": fig_id,
                    "pass": bool(qa and qa.get("pass")),
                    "qa": qa,
                    "svg": "/api/preview/%s.svg" % fig_id,
                })
            if any(f["pass"] for f in result["figures"]):
                studio_ext.annotate_all(ws, run_cli, dxf_to_svg, result)
        except Exception as exc:  # surfaced to the UI, never swallowed
            result["log"] += "\n渲染进程异常：%r" % exc
        finally:
            result["finished"] = time.strftime("%H:%M:%S")
            result["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:   # 落盘：重启后首页轮播与外部 API 仍能取到最近一次结果
                (ws.root / "last_render.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError:
                pass
            render_state["result"] = result
            render_state["running"] = False
            render_lock.release()

    def start_render() -> bool:
        if not render_lock.acquire(blocking=False):
            return False
        render_state.update(running=True, started=time.strftime("%H:%M:%S"))
        threading.Thread(target=render_job, daemon=True).start()
        return True

    def render_and_wait(timeout: float = 1800.0) -> dict:
        """给工作台 API / MCP 用：同步跑完一次渲染（含规范标注）并返回结果。"""
        if not start_render():
            deadline = time.time() + timeout
            while render_state["running"] and time.time() < deadline:
                time.sleep(1.0)
            if not start_render():
                raise RuntimeError("已有一次渲染在进行")
        deadline = time.time() + timeout
        time.sleep(0.2)
        while render_state["running"] and time.time() < deadline:
            time.sleep(1.0)
        return render_state["result"] or {}

    @app.post("/api/render")
    def render():
        if not start_render():
            raise HTTPException(409, "已有一次渲染在进行——几何缓存不支持并发写入")
        return {"started": render_state["started"]}

    @app.get("/api/render/status")
    def render_status():
        return {"running": render_state["running"], "result": render_state["result"],
                "started": render_state["started"]}

    @app.get("/api/preview/{name}")
    def preview(name: str):
        if not re.fullmatch(r"[A-Za-z0-9_\-]+\.(svg|png|dxf)", name):
            raise HTTPException(400, "非法文件名")
        path = ws.out / name
        if not path.is_file():
            raise HTTPException(404)
        media = {"svg": "image/svg+xml", "png": "image/png",
                 "dxf": "application/octet-stream"}[path.suffix[1:]]
        return FileResponse(str(path), media_type=media)

    @app.get("/api/model.glb")
    def model():
        return FileResponse(str(ws.glb), media_type="model/gltf-binary")

    # ---- export ------------------------------------------------------------

    def export_now(want_dwg: bool) -> dict:
        result = render_state["result"]
        if not (result and result.get("ok") and result["figures"]
                and all(f["pass"] for f in result["figures"])):
            raise HTTPException(409, "存在未通过 QA 的图，不能导出——按提示改计划后重渲")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = ws.root / ("export-%s" % stamp)
        dest.mkdir(parents=True)
        bundle = studio_ext.export_bundle(ws, result, dest, run_cli, want_dwg)
        # 不用 shutil.make_archive：它会 os.getcwd()，服务进程的 cwd 不可读时直接抛错
        import zipfile
        archive = dest.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(dest.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(dest).as_posix())
        bundle.update(dir=str(dest), zip=archive.name)
        return bundle

    @app.post("/api/export")
    async def export(request: Request):
        body = await request.json()
        return await _run_sync(lambda: export_now(bool(body.get("dwg", False))))

    @app.get("/api/export-file/{name}")
    def export_file(name: str):
        if not re.fullmatch(r"export-\d{8}-\d{6}\.zip", name):
            raise HTTPException(400, "非法文件名")
        path = ws.root / name
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(str(path), media_type="application/zip", filename=name)

    # ---- flowcharts ---------------------------------------------------------

    @app.get("/api/flow-preview/{name}")
    def flow_preview(name: str):
        if not re.fullmatch(r"[A-Za-z0-9_\-]+\.(svg|png|dxf)", name):
            raise HTTPException(400, "非法文件名")
        path = studio_ext.flow_out(ws) / name
        if not path.is_file():
            raise HTTPException(404)
        media = {"svg": "image/svg+xml", "png": "image/png",
                 "dxf": "application/octet-stream"}[path.suffix[1:]]
        return FileResponse(str(path), media_type=media)

    @app.get("/api/flowcharts")
    def flowcharts():
        return {"flowcharts": studio_ext.list_flowcharts(ws)}

    @app.post("/api/flowcharts")
    async def flowchart_new(request: Request):
        body = await request.json()
        fid = body.get("id") or studio_ext.next_flow_id(ws)
        spec = body.get("spec") or {
            "schema": "patent-flowchart/1", "title": "方法流程图",
            "nodes": [{"id": "s", "kind": "start", "text": "开始"},
                      {"id": "a", "kind": "process", "text": "第一步"},
                      {"id": "e", "kind": "end", "text": "结束"}],
            "edges": [{"from": "s", "to": "a"}, {"from": "a", "to": "e"}]}
        try:
            saved = studio_ext.save_flowchart(ws, fid, spec)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"id": fid, **saved, "flowcharts": studio_ext.list_flowcharts(ws)}

    @app.put("/api/flowcharts/{fid}")
    async def flowchart_save(fid: str, request: Request):
        body = await request.json()
        try:
            saved = studio_ext.save_flowchart(ws, fid, body.get("spec") or {})
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return saved

    @app.post("/api/flowcharts/{fid}/render")
    def flowchart_render(fid: str):
        try:
            res = studio_ext.render_flowchart(ws, fid, run_cli, dxf_to_svg)
        except FileNotFoundError:
            raise HTTPException(404, "没有这张流程图")
        return {"result": res, "flowcharts": studio_ext.list_flowcharts(ws)}

    @app.delete("/api/flowcharts/{fid}")
    def flowchart_delete(fid: str):
        studio_ext.delete_flowchart(ws, fid)
        return {"flowcharts": studio_ext.list_flowcharts(ws)}

    @app.post("/api/flowcharts-ai")
    async def flowchart_ai(request: Request):
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if len(text) < 6:
            raise HTTPException(422, "请先写一段方法步骤描述")
        import workbench_llm
        from patent_figure import flowchart as FC
        try:
            spec = await _run_sync(lambda: workbench_llm.flowchart_from_text(
                text, str(body.get("title", "")), validate=FC.validate))
        except workbench_llm.LLMError as exc:
            raise HTTPException(502, "大模型调用失败：%s" % exc)
        fid = body.get("id") or studio_ext.next_flow_id(ws)
        saved = studio_ext.save_flowchart(ws, fid, spec)
        res = None
        if not [i for i in saved["issues"] if i["severity"] == "error"]:
            res = await _run_sync(lambda: studio_ext.render_flowchart(ws, fid, run_cli, dxf_to_svg))
        return {"id": fid, "spec": spec, **saved, "result": res,
                "flowcharts": studio_ext.list_flowcharts(ws)}

    # ---- 展示图（3D 视图截图，首页功能介绍用） ---------------------------------

    @app.post("/api/snapshot")
    async def snapshot(request: Request):
        name = (request.query_params.get("name") or "view").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", name):
            raise HTTPException(422, "名字只能用小写字母、数字、连字符")
        data = await request.body()
        if not data.startswith(b"\x89PNG") or len(data) > 20 * 1024 * 1024:
            raise HTTPException(422, "不是 PNG 图片或过大")
        path = ws.root / "snapshots" / (name + ".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"name": path.name, "bytes": len(data)}

    # ---- 结构识别 -------------------------------------------------------------

    def structure_job_run() -> None:
        import workbench_llm
        try:
            data = workbench_llm.analyze_structure(
                load_json(ws.assembly), load_json(ws.plan),
                glossary=studio_ext.glossary_of(ws), bom=studio_ext.bom_names_of(ws))
            studio_ext.structure_file(ws).write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            structure_job["error"] = None
        except Exception as exc:  # 交给前端显示，不吞
            structure_job["error"] = str(exc)
        finally:
            structure_job["running"] = False
            structure_job["finished"] = time.strftime("%H:%M:%S")
            structure_lock.release()

    def start_structure() -> bool:
        if not structure_lock.acquire(blocking=False):
            return False
        structure_job.update(running=True, started=time.strftime("%H:%M:%S"), error=None)
        threading.Thread(target=structure_job_run, daemon=True).start()
        return True

    @app.get("/api/structure")
    def structure_get():
        return {"structure": studio_ext.load_structure(ws), "job": dict(structure_job)}

    @app.post("/api/structure/analyze")
    def structure_analyze():
        if _llm_status().get("provider") in (None, "none"):
            raise HTTPException(409, "没有可用的大模型通道：请在工作台设置里填 DeepSeek 密钥")
        start_structure()
        return {"job": dict(structure_job)}

    @app.put("/api/structure")
    async def structure_put(request: Request):
        body = await request.json()
        return {"structure": studio_ext.rename_groups(ws, body.get("groups") or [])}

    @app.post("/api/structure/apply-names")
    async def structure_apply(request: Request):
        body = await request.json()
        applied = studio_ext.apply_terms(ws, studio_ext.name_rows(ws),
                                         overwrite=bool(body.get("overwrite")))
        return {"applied": {k: v for k, v in applied.items() if k != "plan"},
                "plan": applied["plan"], "validate": validate_now()}

    # ---- AI 起草术语 ----------------------------------------------------------

    @app.get("/api/llm/status")
    def llm_status():
        return _llm_status()

    @app.post("/api/llm/draft-terms")
    async def llm_draft_terms(request: Request):
        body = await request.json()
        import workbench_llm
        try:
            rows = await _run_sync(lambda: workbench_llm.draft_terms(
                load_json(ws.assembly), load_json(ws.plan),
                glossary=studio_ext.glossary_of(ws), bom=studio_ext.bom_names_of(ws),
                only_empty=not body.get("overwrite", False)))
        except workbench_llm.LLMError as exc:
            raise HTTPException(502, "大模型调用失败：%s" % exc)
        applied = None
        if body.get("apply", True):
            applied = studio_ext.apply_terms(ws, rows, overwrite=bool(body.get("overwrite")))
        return {"suggestions": rows, "applied": applied, "plan": load_json(ws.plan),
                "validate": validate_now()}

    @app.post("/api/terms")
    async def put_terms(request: Request):
        """外部调用（千问办公 / API）直接写术语：同样只填空白，除非 overwrite。"""
        body = await request.json()
        rows = body.get("terms") or []
        applied = studio_ext.apply_terms(ws, rows, overwrite=bool(body.get("overwrite")))
        return {"applied": {k: v for k, v in applied.items() if k != "plan"},
                "validate": validate_now()}

    app.state.studio = SimpleNamespace(
        ws=ws, render_and_wait=render_and_wait, render_state=render_state,
        validate_now=validate_now, export_now=export_now, start_render=start_render,
        start_structure=start_structure, structure_job=structure_job)

    app.mount("/", StaticFiles(directory=str(WEBUI), html=True), name="webui")
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", type=Path, help="STEP 装配体路径")
    ap.add_argument("--workdir", type=Path, default=None,
                    help="工作目录（默认：STEP 旁的 <名字>.plan-studio/）")
    ap.add_argument("--port", type=int, default=8425)
    ap.add_argument("--token", default=None,
                    help="固定 API token（默认每次随机；仅测试时使用）")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if not args.step.is_file():
        print("用法错误：STEP 文件不存在：%s" % args.step, file=sys.stderr)
        return 2
    if not WEBUI.is_dir():
        print("缺少前端目录 %s" % WEBUI, file=sys.stderr)
        return 1

    ws = Workspace(args.step, args.workdir)
    try:   # cwd 不可读（如从 iCloud 目录启动）时 httpx/rich 等库的 os.getcwd() 会抛错
        import os
        os.chdir(str(ws.root))
    except OSError:
        pass
    prepare(ws)
    token = args.token or secrets.token_urlsafe(16)
    url = "http://127.0.0.1:%d/?token=%s" % (args.port, token)
    print("\nPlan Studio 已就绪：\n  %s\n工作目录：%s\n（仅本机可访问；关闭终端即停止）\n"
          % (url, ws.root), flush=True)
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(build_app(ws, token), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
