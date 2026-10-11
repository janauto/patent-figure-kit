"""The single render-and-publish service (WP1 R1d convergence, completed in R1e).

Every render entry — REST async, REST ``?wait=1``, browser ``/api/render``, standalone Plan
Studio, MCP ``render_project`` **and the direct CLI** ``render_patent_figure.py`` — commits
through ``commit_version`` below, so there is one implementation of seal → verify → (ok-gated,
monotonic) commit pointer → view refresh, not one per entry point (R1d root cause / R1e #1).

* ``prepare(ws, rid)`` — synchronous, under the plan lock: begin the version dir, freeze the
  semantic inputs into it, hash the frozen copies, and allocate a project-level monotonic
  revision. Returns the snapshot (hashes + plan_etag + revision) to record on the job.
* ``run_and_publish(ws, rid, revision, ...)`` — the render itself: produce the whole set
  (figures + QA + reference-numerals + annotation + ``result.json``) in the version dir, then
  ``commit_version``.
* ``commit_version(vs, rid, revision, result, manifest, ...)`` — write ``result.json``, seal
  with integrity sizes + the declared artefact list, verify, and — only for an ``ok`` result
  that is publishable and a newer revision — commit the ``current.json`` pointer
  (``on_committed`` fires irreversibly inside the state lock). The ``out/`` view is refreshed
  separately afterwards, so a view failure never un-commits. ``ok=false`` never switches.
* ``cli_store(out_dir)`` — the direct CLI's version store: ``<out>.versions/`` beside ``-o``,
  with ``-o`` itself as the current-version view (so ``<out>/fig1.dxf`` keeps its path).
* ``reconcile_startup(ws_root, jobs, project_id)`` — the one startup reconciliation (R1e #4).

OCP-free: orchestrates paths + a caller-supplied ``run_cli``.
"""

from __future__ import annotations

import json
import os
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

    if inputs is not None:
        manifest["inputs"] = dict(inputs)
    result["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    became_current = commit_version(vs, rid, revision, result, manifest, ok=bool(ok),
                                    is_cancelled=is_cancelled, on_committed=on_committed)
    return {"ok": bool(ok), "result": result, "manifest": manifest,
            "became_current": became_current}


def commit_version(vs: "_V.VersionStore", rid: str, revision: int, result: Dict[str, Any],
                   manifest: Dict[str, Any], *, ok: bool,
                   is_cancelled: Optional[Callable[[], bool]] = None,
                   on_committed: Optional[Callable[[], None]] = None) -> bool:
    """Seal a rendered version and commit it — the ONE commit path for every entry, the direct
    CLI included (R1e #1).

    1. stamp + write the reader-facing ``result.json`` (same ``ok`` as the manifest);
    2. ``seal`` — fsync every artefact, record sizes, write ``manifest.json`` (with the declared
       ``required_artefacts`` list the caller built);
    3. ``ok`` → the version must be publishable (verify against the declared list + sealed ok),
       else ``RuntimeError`` (integrity failure); ``ok=false`` → never committed;
    4. under ``state_lock``: a cancel requested before this point wins (version marked
       cancelled, ``JobCancelled``); otherwise ``commit_pointer`` (monotonic revision) and
       ``on_committed`` inside the lock (irreversible);
    5. outside the lock: ``refresh_view`` — retryable, never un-commits.

    Returns True iff this version became current."""
    is_cancelled = is_cancelled or (lambda: False)
    on_committed = on_committed or (lambda: None)
    manifest["ok"] = bool(ok)
    manifest["render_job_id"] = rid
    manifest["revision"] = int(revision)
    result["ok"] = bool(ok)
    result["render_job_id"] = rid
    result["revision"] = int(revision)
    vs.write_result(rid, result)                     # reader-facing view, sealed with the set
    vs.seal(rid, manifest, revision)
    if ok and not vs.is_publishable(rid):
        raise RuntimeError("封口完整性校验失败（必需产物缺失、为空或被截断）：%s" % rid)

    became_current = False
    with _V.state_lock(vs.root):
        if is_cancelled():
            vs.mark_cancelled(rid)
            from patent_figure.jobs import JobCancelled
            raise JobCancelled()
        if ok:
            became_current = vs.commit_pointer(rid, revision)
            if became_current:
                on_committed()                       # irreversible, inside the lock
    # View refresh is OUTSIDE the lock, retryable, and never changes the commit (R1d #3). The
    # published result is the sealed result.json inside the version dir, read back via
    # snapshot() — there is no last_render.json cache (R1e #5), so no reader can drift from
    # the pointer.
    if became_current:
        vs.refresh_view()
    return became_current


# --------------------------------------------------------------------------- direct CLI


def cli_store(out_dir) -> "_V.VersionStore":
    """The direct CLI's version store for ``-o OUT`` (R1e #1).

    The store lives in the sibling ``OUT.versions/`` (``results/<rid>/``, ``current.json``,
    ``revision.json``) and ``OUT`` itself is the **view** of the current version (POSIX
    relative symlink, Windows copy) — so ``OUT/fig1.dxf`` keeps its path for every existing
    reader (N9/N10/N15, SKILL.md). The store cannot be ``OUT`` itself: the view would then be
    ``OUT/out/`` and every published path would move."""
    out_dir = Path(out_dir)
    name = out_dir.name or Path.cwd().name
    return _V.VersionStore(out_dir.parent / (name + ".versions"), out_view=out_dir)


def _is_own_view(vs: "_V.VersionStore") -> bool:
    """Is ``vs.out`` a view this store created (a symlink into it, or a copy of one of its
    versions)? Anything else at that path is user data and must never be deleted."""
    out = vs.out
    if out.is_symlink():
        try:
            target = (out.parent / os.readlink(str(out))).resolve()
        except OSError:
            return False
        try:
            target.relative_to(vs.results.resolve())
            return True
        except ValueError:
            return False
    if out.is_dir():
        try:
            rid = json.loads((out / "manifest.json").read_text(encoding="utf-8")) \
                .get("render_job_id")
        except (OSError, ValueError):
            return False
        return bool(rid) and vs.version_dir(str(rid)).is_dir()
    return False


def adopt_legacy_out(vs: "_V.VersionStore") -> Optional[Path]:
    """Before the first pointer publish into ``-o``, make room for the view WITHOUT deleting
    anything that is not ours: an empty directory is removed; a non-empty directory (a pre-R1e
    output dir, or a user's own folder) is renamed aside to ``OUT.pre-versions-<stamp>`` and
    the path returned so the CLI can say where it went. Our own view is left alone."""
    out = vs.out
    if not out.exists() and not out.is_symlink():
        return None
    if _is_own_view(vs):
        return None
    if out.is_dir() and not out.is_symlink():
        try:
            out.rmdir()                              # empty → nothing to keep
            return None
        except OSError:
            pass
    aside = out.parent / ("%s.pre-versions-%s-%s" % (out.name, time.strftime("%Y%m%d-%H%M%S"),
                                                     os.urandom(2).hex()))
    os.replace(str(out), str(aside))
    return aside


# --------------------------------------------------------------------------- startup


def reconcile_startup(ws_root, jobs, project_id: str) -> Dict[str, Any]:
    """The one startup reconciliation for a workspace (R1e #4). Order:

    1. ``reconcile_current`` — a ``current`` that is not publishable (fails verify, or sealed
       ``ok=false`` — e.g. a pointer left by an older build) is isolated: rolled back to the
       most recent publishable version, or cleared (and the ``out/`` view removed);
    2. ``recover`` — repair the view, list never-sealed version dirs;
    3. the isolated version's job → ``reconcile_failed`` (state failed, committed cleared,
       error rebuilt, ``error`` terminal event);
    4. the (new) current version's job → ``reconcile_succeeded`` (state succeeded, committed,
       error cleared, ``done`` terminal event) — only because it is publishable;
    5. any other unfinished render job whose version is sealed ``ok=false`` → failed.

    Returns ``{"isolated", "current", "changed": [job ids]}``."""
    from patent_figure import jobs as _J
    vs = _V.VersionStore(ws_root)
    isolated = vs.reconcile_current()
    vs.recover()
    changed = []
    if isolated:
        bj = jobs.get(isolated)
        if bj is not None and bj.kind == "render":
            if bj.reconcile_failed(
                    {"code": "E_RENDER_NOT_OK",
                     "message": "启动对账：该版本未通过完整性校验或未全部过闸（ok=false），"
                                "已从当前版本隔离（指针回退到上一可发布版本或置空）",
                     "hint": "按每张图的 QA 提示改计划后重渲"},
                    vs.read_result(isolated)):
                changed.append(bj.id)
    cur = vs.current_id()
    if cur and vs.is_publishable(cur):
        cj = jobs.get(cur)
        if cj is not None and cj.kind == "render" and cj.state != _J.CANCELLED:
            if cj.reconcile_succeeded(vs.read_result(cur)):
                changed.append(cj.id)
    for rj in jobs.list(project_id):
        if rj.kind != "render" or rj.id in (cur, isolated):
            continue
        if rj.state not in (_J.INTERRUPTED, _J.RUNNING, _J.QUEUED):
            continue
        res = vs.read_result(rj.id) if vs.is_complete(rj.id) else None
        if res is not None and not res.get("ok"):
            if rj.reconcile_failed(
                    {"code": "E_RENDER_NOT_OK",
                     "message": "出图未全部过闸（进程中断后按封口结果恢复）",
                     "hint": "按每张图的 QA 提示改计划后重渲"}, res):
                changed.append(rj.id)
    return {"isolated": isolated, "current": cur, "changed": changed}
