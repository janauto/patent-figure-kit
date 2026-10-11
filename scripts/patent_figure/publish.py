"""The single render-and-publish service (WP1 R1d convergence).

Every render entry — REST async, REST ``?wait=1``, browser ``/api/render``, standalone Plan
Studio, MCP ``render_project`` — goes through exactly these two functions, so there is one
implementation of freeze → revision → isolated version render → verify → commit pointer →
view refresh, not one per entry point (R1d root cause).

* ``prepare(ws, rid)`` — synchronous, under the plan lock: begin the version dir, freeze the
  semantic inputs into it, hash the frozen copies, and allocate a project-level monotonic
  revision. Returns the snapshot (hashes + plan_etag + revision) to record on the job.
* ``run_and_publish(ws, rid, revision, ...)`` — the render itself: produce the whole set
  (figures + QA + reference-numerals + annotation + ``result.json``) in the version dir, seal
  with integrity sizes, and — only for an ``ok`` result and a newer revision — commit the
  ``current.json`` pointer (``on_committed`` fires irreversibly inside the state lock). The
  ``out/`` view is refreshed separately afterwards, so a view failure never un-commits.

OCP-free: orchestrates paths + a caller-supplied ``run_cli``.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from patent_figure import project_io as _pio
from patent_figure import versions as _V
from patent_figure.worker import snapshot_inputs as _snapshot_inputs

_FREEZE_FILES = ("plan.json", "assembly.json", "glossary.json", "structure.json")


def prepare(ws, rid: str) -> Dict[str, Any]:
    """Freeze inputs + allocate a monotonic revision under the plan lock (R1c #3 / R1d #1).
    Synchronous; call at submit time before starting the background job."""
    vs = _V.VersionStore(ws.root)
    with _pio.plan_lock(ws.plan):
        vs.begin(rid)
        inputs = vs.inputs_dir(rid)
        for name in _FREEZE_FILES:
            src = Path(ws.root) / name
            if src.is_file():
                shutil.copy2(src, inputs / name)
        frozen_plan = inputs / "plan.json"
        snap = _snapshot_inputs(frozen_plan)
        snap["plan_etag"] = _pio.plan_etag(frozen_plan)
        snap["revision"] = vs.next_revision()
    return snap


def run_and_publish(ws, rid: str, revision: int, run_cli: Callable, svg_of: Callable, *,
                    inputs: Optional[dict] = None,
                    cancel_check: Optional[Callable[[], None]] = None,
                    is_cancelled: Optional[Callable[[], bool]] = None,
                    emit: Optional[Callable[..., None]] = None,
                    on_committed: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
    """Render the whole set into the version dir, seal + verify, then commit the pointer only
    for an ``ok`` result with a newer revision. Returns
    ``{ok, result, manifest, became_current}``. Raises ``JobCancelled`` if cancelled before the
    commit point, ``RuntimeError`` on integrity failure."""
    import studio_ext as _EXT  # lazy: studio_ext imports versions
    cancel_check = cancel_check or (lambda: None)
    is_cancelled = is_cancelled or (lambda: False)
    emit = emit or (lambda *a, **k: None)
    on_committed = on_committed or (lambda: None)

    vs = _V.VersionStore(ws.root)
    vd = vs.version_dir(rid)
    plan_p, asm_p = vd / "inputs" / "plan.json", vd / "inputs" / "assembly.json"

    cancel_check()                                   # before executing
    emit("stage", 0.2)
    ok, result, manifest = _EXT.render_version(vd, plan_p, asm_p, ws.cache, run_cli, svg_of)
    emit("log", 0.8, (result.get("log") or "")[-900:])
    cancel_check()                                   # before sealing/commit

    manifest["render_job_id"] = rid
    manifest["revision"] = int(revision)
    if inputs is not None:
        manifest["inputs"] = dict(inputs)
    result["render_job_id"] = rid
    result["ok"] = bool(ok)
    result["revision"] = int(revision)
    result["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    vs.write_result(rid, result)                     # reader-facing view, sealed with the set
    vs.seal(rid, manifest, revision)
    if not vs.verify(rid):
        raise RuntimeError("封口完整性校验失败（文件缺失或截断）：%s" % rid)

    became_current = False
    with _V.state_lock(ws.root):
        if is_cancelled():
            vs.mark_cancelled(rid)
            from patent_figure.jobs import JobCancelled
            raise JobCancelled()
        if ok:
            became_current = vs.commit_pointer(rid, revision)
            if became_current:
                on_committed()                       # irreversible, inside the lock
    # View refresh is OUTSIDE the lock, retryable, and never changes the commit (R1d #3).
    if became_current:
        vs.refresh_view()
        try:
            (Path(ws.root) / "last_render.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    return {"ok": ok, "result": result, "manifest": manifest, "became_current": became_current}
