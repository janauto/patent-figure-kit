"""Immutable render-version store with an atomic current-version pointer (WP1 R1b redo).

Codex R1/R1b kept finding the same root cause: ``out/`` was the *write target*, so every path
that rendered, annotated, published, cancelled or crashed could leave it half-written, stale, or
mixing two plans. This module replaces that with the model the coordinator specified:

* each render writes into an **immutable version directory** ``<ws>/results/<render_job_id>/``
  (figures, reference-numerals, annotation variants, qa reports, ``inputs/`` snapshot + hashes,
  and a ``manifest.json`` whose ``complete: true`` is the only completion marker);
* ``<ws>/current.json`` is the **current-version pointer** — a single file replaced atomically;
  replacing it IS the commit point, and it shares one state lock with cancellation;
* ``<ws>/out/`` is no longer a source of truth, only a **view** of the current version (a
  relative symlink on POSIX, a synced copy on Windows), so every existing reader of ``ws.out``
  keeps working unchanged. Startup recovery re-points it from ``current.json``.

Version directories are immutable **except for derived rasters**: preview PNGs are regenerated
from the DXFs on demand and are deliberately not part of the manifest.

OCP-free on purpose: this module only moves paths and shells out through a caller-supplied
``run_cli``; only ``occ_backend`` may import OCP.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from patent_figure import project_io as _pio

_state_locks: Dict[str, threading.RLock] = {}
_state_guard = threading.Lock()


def state_lock(ws_root) -> threading.RLock:
    """One state lock per workspace, shared by the commit (pointer swap) and cancellation, so
    'cancel before commit → never published' and 'cancel after commit → already committed' is a
    race-free decision (R1b finding 6)."""
    key = str(Path(ws_root).resolve())
    with _state_guard:
        return _state_locks.setdefault(key, threading.RLock())


class VersionStore:
    def __init__(self, ws_root) -> None:
        self.root = Path(ws_root)
        self.results = self.root / "results"
        self.current_file = self.root / "current.json"
        self.out = self.root / "out"

    # -- version directories -------------------------------------------------
    def version_dir(self, rid: str) -> Path:
        return self.results / str(rid)

    def inputs_dir(self, rid: str) -> Path:
        return self.version_dir(rid) / "inputs"

    def begin(self, rid: str) -> Path:
        """Create a fresh (empty) version directory + its ``inputs/`` subdir."""
        vd = self.version_dir(rid)
        if vd.exists():
            shutil.rmtree(vd, ignore_errors=True)
        (vd / "inputs").mkdir(parents=True, exist_ok=True)
        return vd

    def manifest_path(self, rid: str) -> Path:
        return self.version_dir(rid) / "manifest.json"

    def write_manifest(self, rid: str, manifest: dict) -> None:
        """Seal a version: writing ``manifest.json`` with ``complete: true`` is the marker a
        crash-recovery scan looks for (R1b finding 2)."""
        data = dict(manifest)
        data["complete"] = True
        data.setdefault("render_job_id", str(rid))
        _pio.atomic_write_json(self.manifest_path(rid), data)

    def read_manifest(self, rid: str) -> Optional[dict]:
        try:
            return json.loads(self.manifest_path(rid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def is_complete(self, rid: str) -> bool:
        m = self.read_manifest(rid)
        return bool(m and m.get("complete"))

    def mark_cancelled(self, rid: str) -> None:
        vd = self.version_dir(rid)
        if vd.is_dir():
            _pio.atomic_write_json(vd / "cancelled.json", {"cancelled": True})

    def is_cancelled(self, rid: str) -> bool:
        return (self.version_dir(rid) / "cancelled.json").is_file()

    # -- the current-version pointer (commit point) --------------------------
    def current_id(self) -> Optional[str]:
        try:
            return json.loads(self.current_file.read_text(encoding="utf-8")).get("render_job_id")
        except (OSError, ValueError):
            return None

    def current_dir(self) -> Optional[Path]:
        rid = self.current_id()
        if rid and self.is_complete(rid):
            return self.version_dir(rid)
        return None

    def publish(self, rid: str) -> None:
        """Commit: atomically point ``current.json`` at a *complete* version, then refresh the
        ``out/`` view. Caller must hold ``state_lock(self.root)``. Refusing to point at an
        incomplete version keeps the invariant 'current.json always names a complete version'."""
        if not self.is_complete(rid):
            raise ValueError("拒绝发布未完成的版本 %s（无 complete manifest）" % rid)
        _pio.atomic_write_json(self.current_file, {"render_job_id": str(rid)})
        self.sync_out(rid)

    # -- the out/ view -------------------------------------------------------
    def sync_out(self, rid: str) -> None:
        """Make ``out/`` resolve to version ``rid``. Atomic symlink swap on POSIX; a copy on
        platforms without usable symlinks. Handles a legacy real-directory ``out/`` (every
        pre-redo project has one) by moving it aside before the swap (R1b finding 2)."""
        target = self.version_dir(rid)
        if not target.is_dir():
            return
        try:
            self._symlink_swap(target)
        except (OSError, NotImplementedError, AttributeError):
            self._copy_swap(target)

    def _unique(self, stem: str) -> Path:
        return self.root / ("%s.%d.%s" % (stem, os.getpid(), uuid.uuid4().hex[:8]))

    def _symlink_swap(self, target: Path) -> None:
        rel = os.path.relpath(str(target), str(self.root))   # relative: out -> results/<rid>
        tmp = self._unique("out.lnk")
        os.symlink(rel, str(tmp))
        try:
            if self.out.is_symlink() or (self.out.exists() and not self.out.is_dir()):
                os.replace(str(tmp), str(self.out))          # replace a symlink/file atomically
            elif self.out.is_dir():
                trash = self._unique("out.old")              # real dir: move aside, then swap
                os.replace(str(self.out), str(trash))
                os.replace(str(tmp), str(self.out))
                shutil.rmtree(trash, ignore_errors=True)
            else:
                os.replace(str(tmp), str(self.out))          # nothing there
        except OSError:
            if tmp.is_symlink() or tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            raise

    def _copy_swap(self, target: Path) -> None:
        staging = self._unique("out.new")
        shutil.copytree(str(target), str(staging))
        if self.out.is_symlink() or (self.out.exists() and not self.out.is_dir()):
            os.replace(str(staging), str(self.out))
        elif self.out.is_dir():
            trash = self._unique("out.old")
            os.replace(str(self.out), str(trash))
            os.replace(str(staging), str(self.out))
            shutil.rmtree(trash, ignore_errors=True)
        else:
            os.replace(str(staging), str(self.out))

    # -- recovery ------------------------------------------------------------
    def recover(self) -> List[str]:
        """On startup, re-point ``out/`` from ``current.json`` (repairs an interrupted swap),
        and return the ids of version directories that were never sealed — the JobStore marks
        those jobs ``interrupted`` (R1b finding 2). ``current.json`` is only ever trusted when it
        names a complete version."""
        incomplete: List[str] = []
        if self.results.is_dir():
            for vd in sorted(self.results.iterdir()):
                if vd.is_dir() and not self.is_complete(vd.name) and not self.is_cancelled(vd.name):
                    incomplete.append(vd.name)
        rid = self.current_id()
        if rid and self.is_complete(rid):
            try:
                self.sync_out(rid)
            except OSError:
                pass
        return incomplete
