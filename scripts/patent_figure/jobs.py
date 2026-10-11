"""Async job layer: state machine, durable store, SSE event log (WP1 deliverable 4).

Codex R0 S1-09 showed the sync ``/v1/.../render`` could not express a long render honestly:
submit A, edit to B, and the one shared mutable workspace might draw A, number by B and export
a mix; a crash left "state on disk" that said nothing about whether the job could resume.

This module is plain stdlib — no FastAPI — so it is unit-testable without a server. It owns:

* an immutable **input snapshot** per job (STEP sha, plan hash, terms hash), frozen at submit;
* a **state machine** ``queued → running → {succeeded | failed | cancelled | interrupted}``;
* a per-job **append-only event log** with monotonic ids, so SSE can replay from
  ``Last-Event-ID`` after a reconnect;
* **durable** job records: on startup any job still marked ``queued``/``running`` is moved to
  ``interrupted`` — a crash is reported as a crash, never silently "resumed";
* **idempotency**: a submit carrying an ``idempotency_key`` already seen returns the first job.

The FastAPI layer (``workbench.py``) formats the SSE frames and runs the actual render inside
``run``; this module only sequences and records.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
TERMINAL = (SUCCEEDED, FAILED, CANCELLED, INTERRUPTED)

#: SSE event types (contract §6): stage/progress/log/done/error.
EV_STAGE = "stage"
EV_PROGRESS = "progress"
EV_LOG = "log"
EV_DONE = "done"
EV_ERROR = "error"


class JobCancelled(Exception):
    """Raised inside a job's run function when a cancel was requested."""


class JobConflict(Exception):
    """A reused idempotency key arrived with a different request fingerprint (R1c #8)."""


class Job:
    """One unit of async work and its observable history.

    Thread-safe: the event log and state are guarded by one lock, and SSE readers wait on a
    condition the producer notifies. A job never leaves a terminal state.
    """

    def __init__(self, kind: str, project_id: str, inputs: Optional[Dict[str, Any]] = None,
                 idempotency_key: Optional[str] = None, job_id: Optional[str] = None) -> None:
        self.id = job_id or uuid.uuid4().hex
        self.kind = str(kind)
        self.project_id = str(project_id)
        self.inputs = dict(inputs or {})
        self.idempotency_key = idempotency_key
        self.state = QUEUED
        self.stage = QUEUED
        self.progress = 0.0
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[Dict[str, str]] = None
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.events: List[Dict[str, Any]] = []
        self._next_event = 1
        self._cancel = threading.Event()
        self._committed = threading.Event()
        self._cond = threading.Condition()
        self._store: "Optional[JobStore]" = None

    # -- event log -----------------------------------------------------------
    def emit(self, type_: str, data: Any = None, *, stage: Optional[str] = None,
             progress: Optional[float] = None) -> Dict[str, Any]:
        with self._cond:
            if stage is not None:
                self.stage = str(stage)
            if progress is not None:
                self.progress = max(0.0, min(1.0, float(progress)))
            ev = {"id": self._next_event, "type": str(type_),
                  "stage": self.stage, "progress": round(self.progress, 4),
                  "data": data, "ts": time.time()}
            self._next_event += 1
            self.events.append(ev)
            self.updated_at = ev["ts"]
            self._cond.notify_all()
        self._persist()
        return ev

    def events_since(self, last_id: int) -> List[Dict[str, Any]]:
        with self._cond:
            return [e for e in self.events if e["id"] > last_id]

    def wait_for_event_after(self, last_id: int, timeout: float) -> List[Dict[str, Any]]:
        """Block until an event with id > ``last_id`` exists, or timeout. Returns new events."""
        with self._cond:
            deadline = time.monotonic() + timeout
            while True:
                fresh = [e for e in self.events if e["id"] > last_id]
                if fresh:
                    return fresh
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._cond.wait(timeout=remaining)

    # -- state ---------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        with self._cond:
            self.state = state
            self.updated_at = time.time()
            self._cond.notify_all()

    def request_cancel(self) -> None:
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def mark_committed(self) -> None:
        """Mark this job's result irreversibly committed (R1c #2). Must be set inside the
        workspace state lock at the commit point. Once set, a late cancel cannot undo it."""
        self._committed.set()

    @property
    def committed(self) -> bool:
        return self._committed.is_set()

    def raise_if_cancelled(self) -> None:
        # A committed job is past the point of no return — a cancel racing the commit loses.
        if self._cancel.is_set() and not self._committed.is_set():
            raise JobCancelled()

    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def to_dict(self) -> Dict[str, Any]:
        with self._cond:
            return {
                "id": self.id, "kind": self.kind, "project_id": self.project_id,
                "state": self.state, "stage": self.stage, "progress": round(self.progress, 4),
                "inputs": dict(self.inputs),
                "result": self.result, "error": self.error,
                "idempotency_key": self.idempotency_key,
                "committed": self.committed,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "logs": [e["data"] for e in self.events if e["type"] == EV_LOG],
                "last_event_id": self._next_event - 1,
            }

    def to_disk(self) -> Dict[str, Any]:
        """Full durable state — like ``to_dict`` but with the complete event log and the
        monotonic counter, so a restart can replay SSE from any ``Last-Event-ID`` (R1 #10)."""
        with self._cond:
            d = self.to_dict()
            d["events"] = [dict(e) for e in self.events]
            d["next_event"] = self._next_event
            return d

    def _persist(self) -> None:
        if self._store is not None:
            self._store._save(self)


class JobStore:
    """In-memory jobs with a durable JSON mirror and crash-honest recovery."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else None
        self._jobs: "Dict[str, Job]" = {}
        self._by_key: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()   # serialise disk writes (R1 #9)
        self._seq = 0
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            self._recover()

    # -- persistence ---------------------------------------------------------
    def _path(self, job_id: str) -> Path:
        assert self.root is not None
        return self.root / (job_id + ".json")

    def _save(self, job: Job) -> None:
        """Atomic durable write (R1 #9): unique temp → flush+fsync → ``os.replace``, under a
        store-wide lock so a crash mid-write cannot leave a half-file that later reads as
        corrupt. The full event log is persisted (R1 #10)."""
        if self.root is None:
            return
        with self._save_lock:
            # Snapshot INSIDE the save lock (R1b #10): this totally orders writes, so every
            # persisted state is at least as fresh as the previous one — a concurrent older
            # snapshot can never land after a newer one and roll the on-disk state back.
            payload = json.dumps(job.to_disk(), ensure_ascii=False, indent=1)
            self._seq += 1
            tmp = self._path(job.id).with_name(
                "%s.%d.%d.%s.tmp" % (job.id, os.getpid(), self._seq, os.urandom(4).hex()))
            try:
                with open(str(tmp), "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(str(tmp), str(self._path(job.id)))
            except OSError:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    def _recover(self) -> None:
        """Load persisted jobs. A job that was mid-flight when the process died is marked
        ``interrupted`` with an appended terminal event (so a reconnect with an old
        Last-Event-ID still gets a terminal frame, R1 #10). A record that will not parse is
        kept as a *queryable* error-state job rather than vanishing into a 404 (R1 #9)."""
        assert self.root is not None
        for path in sorted(self.root.glob("*.json")):
            jid = path.stem
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Corrupt/torn record: surface it as a failed job, not a 404.
                job = Job("unknown", "", job_id=jid)
                job.state = FAILED
                job.stage = FAILED
                job.error = {"code": "E_JOB_RECORD_CORRUPT",
                             "message": "作业记录损坏，无法读取（可能上次写入时崩溃）",
                             "hint": "重新提交一次。"}
                job.events = [{"id": 1, "type": EV_ERROR, "stage": FAILED, "progress": 0.0,
                               "data": job.error, "ts": time.time()}]
                job._next_event = 2
                self._jobs[job.id] = job
                continue
            job = Job(data.get("kind", "render"), data.get("project_id", ""),
                      inputs=data.get("inputs"), idempotency_key=data.get("idempotency_key"),
                      job_id=data.get("id", jid))
            job.events = list(data.get("events") or [])
            job._next_event = int(data.get("next_event",
                                           (job.events[-1]["id"] + 1) if job.events else 1))
            state = data.get("state", INTERRUPTED)
            job.result = data.get("result")
            job.error = data.get("error")
            job.created_at = data.get("created_at", time.time())
            job.updated_at = data.get("updated_at", job.created_at)
            job.stage = data.get("stage", state)
            job.progress = float(data.get("progress", 0.0))
            if data.get("committed"):
                job._committed.set()
            if state in (QUEUED, RUNNING) and job.committed:
                # Crashed AFTER the commit point but before the success transition persisted:
                # the version is published, so this job did succeed (R1c #5 / #2).
                state = SUCCEEDED
                job.state = SUCCEEDED
            if state in (QUEUED, RUNNING):
                # Crashed mid-flight: mark interrupted AND append a terminal event continuing
                # the id sequence, so Last-Event-ID replay sees the end (R1 #10).
                job.state = INTERRUPTED
                job.error = {"code": "E_INTERRUPTED", "message": "进程重启，作业被中断",
                             "hint": "重新提交一次渲染。"}
                job.events.append({"id": job._next_event, "type": EV_ERROR, "stage": INTERRUPTED,
                                   "progress": job.progress, "data": job.error, "ts": time.time()})
                job._next_event += 1
                job._store = self
                self._jobs[job.id] = job
                if job.idempotency_key:
                    self._by_key[(job.project_id, job.kind, job.idempotency_key)] = job.id
                job._persist()           # write the interrupted event back durably
                continue
            job.state = state
            self._jobs[job.id] = job
            if job.idempotency_key:
                self._by_key[(job.project_id, job.kind, job.idempotency_key)] = job.id

    # -- api -----------------------------------------------------------------
    def create(self, kind: str, project_id: str, *, inputs: Optional[Dict[str, Any]] = None,
               idempotency_key: Optional[str] = None,
               fingerprint: Optional[str] = None) -> "tuple[Job, bool]":
        """Create a queued job, or return the existing one for a repeated idempotency key.

        The key is scoped to ``(project_id, kind, key)`` so it cannot cross projects (R1c #8);
        a repeat with a different ``fingerprint`` raises ``JobConflict``. Returns
        ``(job, created)`` where ``created`` is False on an idempotent hit.
        """
        with self._lock:
            tk = (project_id, kind, idempotency_key) if idempotency_key else None
            if tk and tk in self._by_key:
                existing = self._jobs.get(self._by_key[tk])
                if existing is not None:
                    if (fingerprint is not None and existing.inputs.get("_fingerprint")
                            not in (None, fingerprint)):
                        raise JobConflict(
                            "幂等键 %r 已用于不同输入（%s）" % (idempotency_key, kind))
                    return existing, False
            job = Job(kind, project_id, inputs=inputs, idempotency_key=idempotency_key)
            if fingerprint is not None:
                job.inputs["_fingerprint"] = fingerprint
            job._store = self
            self._jobs[job.id] = job
            if tk:
                self._by_key[tk] = job.id
        job._persist()
        return job, True

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self, project_id: Optional[str] = None) -> List[Job]:
        jobs = list(self._jobs.values())
        if project_id is not None:
            jobs = [j for j in jobs if j.project_id == project_id]
        return sorted(jobs, key=lambda j: j.created_at)

    def cancel(self, job_id: str) -> Optional[Job]:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.is_terminal():
            return job
        job.request_cancel()
        if job.state == QUEUED:
            # Never started: cancel is immediate and clean.
            job.emit(EV_ERROR, "作业已取消", stage=CANCELLED)
            job._set_state(CANCELLED)
            job._persist()
        return job

    def run(self, job: Job, fn: "Callable[[Job], Optional[Dict[str, Any]]]") -> None:
        """Execute ``fn(job)`` synchronously, driving the state machine and events.

        ``fn`` does the real work and may call ``job.emit`` for stages/progress/logs and
        ``job.raise_if_cancelled()`` at checkpoints. Raise ``JobCancelled`` to cancel,
        anything else to fail. This is normally the target of a background thread.
        """
        if job.cancel_requested:
            job.emit(EV_ERROR, "作业已取消", stage=CANCELLED)
            job._set_state(CANCELLED)
            job._persist()
            return
        job._set_state(RUNNING)
        job.emit(EV_STAGE, "running", stage=RUNNING, progress=0.0)
        try:
            result = fn(job)
            job.raise_if_cancelled()
            job.result = result if isinstance(result, dict) else {"value": result}
            job._set_state(SUCCEEDED)
            job.emit(EV_DONE, job.result, stage=SUCCEEDED, progress=1.0)
        except JobCancelled:
            job._set_state(CANCELLED)
            job.emit(EV_ERROR, "作业已取消", stage=CANCELLED)
        except BaseException as exc:  # noqa: BLE001 — report, never leak a thread exception
            code = getattr(exc, "code", "E_JOB_FAILED")
            job.error = {"code": str(code), "message": str(exc),
                         "hint": getattr(exc, "hint", "") or ""}
            # A structured failure (e.g. a render that ran but did not pass QA) may carry a
            # result payload with the per-figure diagnostics — keep it queryable (R1 #5).
            payload = getattr(exc, "result", None)
            if isinstance(payload, dict):
                job.result = payload
            job._set_state(FAILED)
            job.emit(EV_ERROR, job.error, stage=FAILED)
        finally:
            job._persist()

    def start(self, job: Job, fn: "Callable[[Job], Optional[Dict[str, Any]]]") -> threading.Thread:
        """Run ``fn`` in a daemon thread. Returns the thread (join in tests)."""
        t = threading.Thread(target=self.run, args=(job, fn), daemon=True)
        t.start()
        return t


def sse_frame(event: Dict[str, Any]) -> str:
    """Format one job event as a Server-Sent-Events frame (with id + event name)."""
    payload = json.dumps(
        {"stage": event.get("stage"), "progress": event.get("progress"),
         "data": event.get("data")}, ensure_ascii=False)
    return "id: %d\nevent: %s\ndata: %s\n\n" % (
        int(event["id"]), str(event["type"]), payload)
