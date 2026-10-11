"""Resident single-task render worker (WP1 deliverable 3, Codex R0 S1-06).

The web path used to shell out to ``render_patent_figure.py`` once per "出图" click, paying
the OCP import (≈1–2 s) and matplotlib import every time (baseline §2). This module keeps a
**persistent child process** that imports OCP exactly once and then runs each job in-process,
so the per-job fixed cost disappears while the gates, error messages and — crucially — the
deterministic output stay byte-for-byte what the CLI produces.

Design choices, each answering a specific Codex R0 / WP0 finding:

* **One resident single-task worker, not a pool** (S1-06): HLR occlusion is a whole-scene
  computation that cannot be split per part, and a pool would multiply RSS and leave native
  calls running after a cancel. One child, one job at a time, with a hard time limit and a
  kill-and-rebuild on breach.
* **Same CLI mains, in-process** (S1-08 / N15): the child sets ``sys.argv`` and calls the
  very ``main()`` the ``__main__`` block calls, capturing stdout/stderr, and returns a
  ``subprocess.CompletedProcess`` so ``plan_studio.run_cli`` swaps between the worker and a
  plain subprocess transparently. The output digest is therefore identical by construction.
* **Thread env before numpy/OCP** (WP0 D3 / §11.9 rule 5): ``OMP/MKL/OPENBLAS/...=1`` are set
  in the child's environment *and* at the very top of its entry point, before numpy or OCP is
  imported, so no threaded BLAS reduction order can creep into a result.
* **Per-task global reset** (S1-06): ``occ_backend.reset_hlr_drop_stats`` is called before
  every job so one job's dropped-edge count cannot leak into the next job's render gate. The
  font registration in ``sheet.py`` is idempotent and carries no per-task state.
* **Portable** (cross-platform red line): the request/response channel is newline-delimited
  JSON over the child's stdin/stdout, drained by a reader thread — no ``select`` on a pipe
  (unsupported on Windows), no ``fcntl``. The memory cap uses ``resource`` only where it
  exists (POSIX); elsewhere the time limit is the backstop.
* **Configurable fallback** (S1-06): set ``PFK_WORKER=0`` to disable the worker entirely and
  run every CLI as its own subprocess, exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Thread-count environment variables pinned to 1. Set before numpy / OCP import them.
THREAD_ENV = {
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}

#: CLI basenames the child runs in-process. Anything else falls back to a subprocess, so the
#: worker never has to understand every script — only the hot ones on the render chain.
DISPATCH = (
    "render_patent_figure.py",
    "analyze_assembly.py",
    "export_step_glb.py",
    "annotate_figure_sheet.py",
    "render_flowchart.py",
    "qa_patent_figure.py",
    "validate_figure_plan.py",
)

#: Default per-job wall-clock limit (seconds). On breach the child is killed and rebuilt.
DEFAULT_TIME_LIMIT_S = 1800.0
#: Seconds to wait for the child's readiness handshake (OCP import) at spawn.
STARTUP_TIMEOUT_S = 120.0
#: Env var that turns the worker off (fall back to a subprocess per call).
DISABLE_ENV = "PFK_WORKER"
#: Env var carrying the child's address-space cap in MiB (0/unset = no cap).
MEM_ENV = "PFK_WORKER_MEM_MB"

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"


class WorkerError(RuntimeError):
    """The resident worker could not serve a job and could not be rebuilt."""


def snapshot_inputs(plan_path: Path) -> Dict[str, str]:
    """Freeze a job's inputs into hashes (STEP sha, plan hash, terms hash).

    Codex R0 S1-09 wants a job bound to the exact bytes it was submitted with, so a later
    edit cannot make the running render draw a different plan than the one requested. The job
    layer stores this snapshot alongside the job; the hashes are plain sha256 so two paths
    (CLI vs worker) that read the same files agree.
    """
    plan_path = Path(plan_path)
    out = {"plan_hash": "", "terms_hash": "", "step_sha": ""}
    try:
        raw = plan_path.read_bytes()
        out["plan_hash"] = hashlib.sha256(raw).hexdigest()
        doc = json.loads(raw.decode("utf-8"))
    except Exception:
        return out
    terms = doc.get("terms") if isinstance(doc, dict) else None
    out["terms_hash"] = hashlib.sha256(
        json.dumps(terms, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    step = str((doc.get("source") or {}).get("step", "")) if isinstance(doc, dict) else ""
    if step:
        p = Path(step)
        if not p.is_file():
            p = plan_path.parent / step
        if p.is_file():
            h = hashlib.sha256()
            with open(str(p), "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out["step_sha"] = h.hexdigest()
    return out


# =============================================================================== parent side


class Worker:
    """A persistent child process that runs render-chain CLIs in-process.

    Thread-safe: ``run`` is serialised, matching the single-task design. A job that exceeds
    the time limit, or a child that dies, is handled by killing and rebuilding the child; the
    offending job returns a non-zero ``CompletedProcess`` rather than hanging.
    """

    def __init__(self, python: Optional[str] = None, repo: Optional[Path] = None,
                 time_limit_s: float = DEFAULT_TIME_LIMIT_S,
                 mem_limit_mb: int = 0) -> None:
        self.python = python or sys.executable
        self.repo = Path(repo) if repo else _REPO
        self.time_limit_s = float(time_limit_s)
        self.mem_limit_mb = int(mem_limit_mb)
        self._proc: Optional[subprocess.Popen] = None
        self._queue: "Optional[_Queue]" = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._abort_lock = threading.Lock()
        self._current_token: Optional[str] = None
        self.rebuilds = 0

    def abort_if_running(self, token: str) -> bool:
        """Kill the child iff it is currently running the job identified by ``token`` (R1 #6).

        Safe to call from another thread while ``run`` is blocked: it does not take the main
        lock, and killing the child makes the in-flight ``run`` return a non-zero result. The
        next ``run`` rebuilds the worker. Returns True if a kill was issued."""
        with self._abort_lock:
            proc = self._proc if self._current_token == token else None
        if proc is None:
            return False
        try:
            proc.kill()
        except Exception:
            pass
        return True

    # -- lifecycle -----------------------------------------------------------
    def _spawn(self) -> None:
        env = dict(os.environ)
        env.update(THREAD_ENV)                       # before numpy/OCP import in the child
        if self.mem_limit_mb > 0:
            env[MEM_ENV] = str(self.mem_limit_mb)
        env.pop(DISABLE_ENV, None)                   # the child must not disable itself
        proc = subprocess.Popen(
            [self.python, "-u", str(Path(__file__).resolve()), "--serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(self.repo), env=env, text=True, bufsize=1)
        queue: "_Queue" = _Queue()
        reader = threading.Thread(target=_pump, args=(proc.stdout, queue), daemon=True)
        reader.start()
        # Readiness handshake: the child reports once OCP is imported.
        line = queue.get(STARTUP_TIMEOUT_S)
        if line is None:
            self._hard_kill(proc)
            raise WorkerError("worker 子进程启动超时（未在 %gs 内完成 OCP 导入）" % STARTUP_TIMEOUT_S)
        try:
            msg = json.loads(line)
        except ValueError:
            self._hard_kill(proc)
            raise WorkerError("worker 子进程握手非法：%r" % line[:200])
        if not msg.get("ready"):
            self._hard_kill(proc)
            raise WorkerError("worker 子进程初始化失败：%s" % msg.get("error", "未知"))
        self._proc, self._queue, self._reader = proc, queue, reader

    def _ensure(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            if self._proc is not None:
                self.rebuilds += 1
            self._spawn()

    def _hard_kill(self, proc: Optional[subprocess.Popen] = None) -> None:
        proc = proc or self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if proc is self._proc:
            self._proc = self._queue = self._reader = None

    def close(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                    self._proc.stdin.flush()
                    self._proc.wait(timeout=5)
                except Exception:
                    self._hard_kill()
            self._proc = self._queue = self._reader = None

    # -- the one operation ---------------------------------------------------
    def run(self, args: List[Any], timeout: Optional[float] = None,
            token: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run one CLI invocation (``args`` = the same list ``run_cli`` would pass).

        Returns a ``CompletedProcess`` indistinguishable from ``subprocess.run``. A script the
        child does not dispatch in-process is run as a plain subprocess (transparent fallback).
        A timeout or a dead child kills and rebuilds the worker and returns a non-zero result.
        ``token`` tags the in-flight job so ``abort_if_running(token)`` can cancel exactly it.
        """
        argv = [str(a) for a in args]
        basename = Path(argv[0]).name if argv else ""
        limit = float(timeout) if timeout else self.time_limit_s
        if basename not in DISPATCH:
            return self._subprocess(argv, limit)

        with self._lock:
            try:
                self._ensure()
            except WorkerError as exc:
                # Could not get a worker at all — degrade to a subprocess rather than fail.
                return self._subprocess(argv, limit, note="worker 不可用，回退子进程：%s" % exc)
            assert self._proc is not None and self._queue is not None
            with self._abort_lock:
                self._current_token = token
            try:
                self._proc.stdin.write(json.dumps({"argv": argv}) + "\n")
                self._proc.stdin.flush()
            except Exception:
                with self._abort_lock:
                    self._current_token = None
                self._hard_kill()
                return self._subprocess(argv, limit, note="worker 写入失败，回退子进程")
            line = self._queue.get(limit)
            with self._abort_lock:
                self._current_token = None
            if line is None:
                # Timeout or the child died mid-job: kill, rebuild on next call, report.
                died = self._proc.poll() is not None
                self._hard_kill()
                reason = ("worker 子进程在任务中退出（可能超内存）" if died
                          else "worker 执行超时（>%gs），已杀掉并将重建" % limit)
                return subprocess.CompletedProcess(argv, 124, "", reason)
            try:
                resp = json.loads(line)
            except ValueError:
                self._hard_kill()
                return self._subprocess(argv, limit, note="worker 响应非法，回退子进程")

        if resp.get("unhandled"):
            return self._subprocess(argv, limit)
        return subprocess.CompletedProcess(
            argv, int(resp.get("rc", 1)), resp.get("stdout", ""), resp.get("stderr", ""))

    def _subprocess(self, argv: List[str], timeout: float,
                    note: str = "") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(THREAD_ENV)
        proc = subprocess.run([self.python] + argv, capture_output=True, text=True,
                              timeout=timeout, cwd=str(self.repo), env=env)
        if note:
            proc = subprocess.CompletedProcess(
                proc.args, proc.returncode, proc.stdout, (proc.stderr or "") + "\n" + note)
        return proc


class _Queue:
    """A tiny blocking single-item channel (avoids importing queue's heavier API)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._items: List[Optional[str]] = []

    def put(self, item: Optional[str]) -> None:
        with self._cond:
            self._items.append(item)
            self._cond.notify()

    def get(self, timeout: float) -> Optional[str]:
        with self._cond:
            if not self._items:
                self._cond.wait(timeout=timeout)
            if not self._items:
                return None
            return self._items.pop(0)


def _pump(stream, queue: "_Queue") -> None:
    """Forward each line the child prints on its protocol channel into the queue; signal EOF
    (the child exited) with a ``None``."""
    try:
        for line in iter(stream.readline, ""):
            queue.put(line.rstrip("\n"))
    except Exception:
        pass
    finally:
        queue.put(None)


# --------------------------------------------------------------- module-level shared worker

_SHARED: Optional[Worker] = None
_SHARED_LOCK = threading.Lock()


def worker_enabled() -> bool:
    return os.environ.get(DISABLE_ENV, "1").lower() not in ("0", "off", "no", "false")


def get_shared_worker() -> Optional[Worker]:
    """Return the process-wide worker, or None when disabled via ``PFK_WORKER``."""
    global _SHARED
    if not worker_enabled():
        return None
    with _SHARED_LOCK:
        if _SHARED is None:
            mem = 0
            try:
                mem = int(os.environ.get(MEM_ENV, "0"))
            except ValueError:
                mem = 0
            _SHARED = Worker(mem_limit_mb=mem)
        return _SHARED


def run_cli_via_worker(args: List[Any], timeout: int = 1800) -> subprocess.CompletedProcess:
    """Drop-in for ``plan_studio.run_cli``: route through the resident worker when enabled,
    otherwise run a plain subprocess. Always returns a ``CompletedProcess``."""
    worker = get_shared_worker()
    if worker is None:
        env = dict(os.environ)
        env.update(THREAD_ENV)
        return subprocess.run([sys.executable] + [str(a) for a in args],
                              capture_output=True, text=True, timeout=timeout, cwd=str(_REPO),
                              env=env)
    return worker.run(args, timeout=timeout)


# =============================================================================== child side


def _child_main() -> int:
    # Thread env MUST be set before numpy / OCP import. The parent already set it in our
    # environment; set it again here so a directly-launched worker is pinned too. (numpy reads
    # these at import time, so this only has teeth if it runs before the import below.)
    for key, value in THREAD_ENV.items():
        os.environ.setdefault(key, value)

    # Optional POSIX memory cap. Skipped on platforms without `resource` (e.g. Windows).
    mem_mb = 0
    try:
        mem_mb = int(os.environ.get(MEM_ENV, "0"))
    except ValueError:
        mem_mb = 0
    if mem_mb > 0:
        try:
            import resource  # POSIX only
            soft = mem_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (soft, soft))
        except Exception:
            pass  # best-effort; the parent's time limit is the portable backstop

    out = sys.stdout            # the protocol channel — never write CLI output here directly

    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))

    # Import the hot modules ONCE (this pulls OCP a single time for the worker's whole life).
    mains: Dict[str, Any] = {}
    try:
        import importlib
        for name in DISPATCH:
            mod = importlib.import_module(name[:-3])   # strip ".py"
            mains[name] = mod.main
        # Reset hook lives on occ_backend; import it directly for per-task cleanup.
        from patent_figure import occ_backend as _occ
    except Exception as exc:  # noqa: BLE001
        out.write(json.dumps({"ready": False, "error": repr(exc)}) + "\n")
        out.flush()
        return 1

    out.write(json.dumps({"ready": True, "pid": os.getpid()}) + "\n")
    out.flush()

    import io
    import contextlib

    for line in iter(sys.stdin.readline, ""):
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            out.write(json.dumps({"rc": 2, "stdout": "", "stderr": "worker: 请求非法"}) + "\n")
            out.flush()
            continue
        if req.get("cmd") == "shutdown":
            break
        argv = [str(a) for a in (req.get("argv") or [])]
        basename = Path(argv[0]).name if argv else ""
        main = mains.get(basename)
        if main is None:
            out.write(json.dumps({"unhandled": True}) + "\n")
            out.flush()
            continue

        # Per-task reset of process-global diagnostic state (S1-06).
        _occ.reset_hlr_drop_stats()

        buf_out, buf_err = io.StringIO(), io.StringIO()
        saved_argv = sys.argv
        sys.argv = [argv[0]] + argv[1:]
        rc = 1
        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                result = main()
            rc = int(result) if result is not None else 0
        except SystemExit as exc:          # argparse usage errors etc.
            code = exc.code
            rc = code if isinstance(code, int) else (0 if code is None else 1)
        except BaseException as exc:        # noqa: BLE001 — report, never crash the worker
            import traceback
            buf_err.write("worker: 任务抛出异常 %r\n%s" % (exc, traceback.format_exc()))
            rc = 1
        finally:
            sys.argv = saved_argv
        out.write(json.dumps({"rc": rc, "stdout": buf_out.getvalue(),
                              "stderr": buf_err.getvalue(), "unhandled": False}) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    if "--serve" in sys.argv[1:]:
        raise SystemExit(_child_main())
    sys.stderr.write("这是常驻渲染 worker 的子进程入口，由 patent_figure.worker.Worker 启动，"
                     "不单独运行。用法：python3 -m patent_figure.worker --serve\n")
    raise SystemExit(2)
