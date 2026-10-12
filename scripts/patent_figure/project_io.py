"""Single versioned-write entry for a project's figure-plan (WP1 R1 fix, finding 3).

Codex R1 showed the plan could be lost two ways: two REST PUTs carrying the same ETag both
passed the check-then-write and overwrote each other (the check and the write were not atomic),
and the sub-app `/api/plan`, terms, structure and flowchart paths wrote the plan with no
version guard at all. This module is the one place a plan is written, shared by every app in
the process (workbench + mounted plan_studio sub-apps), so a read→compare→atomic-replace runs
under one lock keyed by the plan file itself:

* ``write_plan`` replaces the whole plan with an optional ``If-Match`` precondition — two
  same-ETag writers get one success and one ``PlanConflict``;
* ``update_plan`` does a read-modify-write (terms / structure edits) under the same lock, so
  concurrent edits serialise instead of interleaving and corrupting the file.

The write itself is unique-temp + flush/fsync + ``os.replace`` — atomic and crash-safe.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_locks: Dict[str, threading.RLock] = {}
_guard = threading.Lock()


class PlanConflict(Exception):
    """Raised by ``write_plan`` when the supplied ETag does not match the current plan."""

    def __init__(self, current: str) -> None:
        super().__init__("plan ETag 不匹配（当前 %s）" % current)
        self.current = current


def plan_lock(plan_path) -> threading.RLock:
    """One lock per plan file (keyed by its resolved path), shared across apps in-process."""
    key = str(Path(plan_path).resolve())
    with _guard:
        return _locks.setdefault(key, threading.RLock())


def plan_etag(plan_path) -> str:
    """Strong-ish ETag: first 32 hex of sha256(plan bytes). Empty string when absent."""
    try:
        return hashlib.sha256(Path(plan_path).read_bytes()).hexdigest()[:32]
    except OSError:
        return ""


def atomic_write_json(path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    tmp = path.with_name("%s.%d.%s.tmp" % (path.name, os.getpid(), uuid.uuid4().hex[:8]))
    try:
        with open(str(tmp), "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def write_plan(plan_path, new_plan: dict, if_match: Optional[str] = None,
               snapshot_cb: Optional[Callable[[], None]] = None) -> str:
    """Replace the plan atomically under the per-plan lock, enforcing ``If-Match``.

    Returns the new ETag. Raises ``PlanConflict`` when ``if_match`` is a non-empty string that
    does not equal the current ETag — the comparison and the write are inside one lock, so two
    writers with the same stale ETag cannot both win.
    """
    with plan_lock(plan_path):
        current = plan_etag(plan_path)
        if if_match not in (None, "") and if_match != current:
            raise PlanConflict(current)
        if snapshot_cb is not None:
            try:
                snapshot_cb()
            except Exception:
                pass
        atomic_write_json(plan_path, new_plan)
        return plan_etag(plan_path)


def update_plan(plan_path, mutate: Callable[[dict], dict],
                snapshot_cb: Optional[Callable[[], None]] = None,
                if_match: Optional[str] = None) -> dict:
    """Read-modify-write the plan under the lock (for terms / structure edits).

    ``mutate(plan_dict) -> plan_dict`` runs with the current plan; the result is written
    atomically. Returns the written plan. Serialising this stops two term edits from racing on
    a read-modify-write and dropping one another's changes. ``if_match`` (WP2: AI 起名
    ``apply=true``、接受候选分图): a non-empty ETag that must equal the current one, checked
    inside the same lock → ``PlanConflict``. ``mutate`` may raise to abort without writing.
    """
    with plan_lock(plan_path):
        if if_match not in (None, ""):
            current = plan_etag(plan_path)
            if if_match != current:
                raise PlanConflict(current)
        try:
            plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan = {}
        new_plan = mutate(plan)
        if snapshot_cb is not None:
            try:
                snapshot_cb()
            except Exception:
                pass
        atomic_write_json(plan_path, new_plan)
        return new_plan
