"""Immutable render-version store with an atomic current-version pointer (WP1 R1b/R1c).

Model (coordinator-specified, threaded through every path in R1c):

* each render writes into an **immutable version directory** ``<ws>/results/<render_job_id>/``
  (figures, reference-numerals, annotation variants, qa reports, ``inputs/`` snapshot + hashes,
  a sealed ``result.json`` for readers, and ``manifest.json`` whose ``complete: true`` plus the
  recorded per-file sizes is the completion + integrity marker);
* ``<ws>/current.json`` is the **current-version pointer** — ``{render_job_id, revision}`` written
  atomically; replacing it IS the commit point, guarded by ``state_lock`` (shared with cancel) and
  **monotonic**: a stale job with a lower revision is refused and only kept as history;
* ``<ws>/out/`` is only a **view** of the current version (POSIX symlink / Windows copy), so legacy
  ``ws.out`` readers still resolve the current version; new readers resolve a version directory
  once via ``current_dir()`` and use only that directory for the whole operation.

Durability order on seal (R1c #6): fsync every artefact + the version dir, then write+fsync the
manifest (recording sizes), then replace+fsync the pointer's parent dir. Recovery and export
verify artefacts against the manifest. Version dirs are immutable except derived preview PNGs
(regenerated from the DXFs, not in the manifest).

OCP-free: only orchestrates paths; ``occ_backend`` is the only OCP module.
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
_rev_locks: Dict[str, threading.Lock] = {}
_guard = threading.Lock()

#: Default retention: keep this many most-recent successful (non-current) versions; older
#: successful versions and all stale failed/cancelled versions are reclaimable (R1c #11).
KEEP_SUCCESS = 5


def state_lock(ws_root) -> threading.RLock:
    """One state lock per workspace, shared by commit (pointer swap) and cancellation."""
    key = str(Path(ws_root).resolve())
    with _guard:
        return _state_locks.setdefault(key, threading.RLock())


def _rev_lock(ws_root) -> threading.Lock:
    key = str(Path(ws_root).resolve())
    with _guard:
        return _rev_locks.setdefault(key, threading.Lock())


def _fsync_path(path: Path) -> None:
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY") and path.is_dir():
            flags |= os.O_DIRECTORY
        fd = os.open(str(path), flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, PermissionError):
        pass        # best effort (e.g. directory fsync unsupported on some platforms)


class VersionStore:
    def __init__(self, ws_root) -> None:
        self.root = Path(ws_root)
        self.results = self.root / "results"
        self.current_file = self.root / "current.json"
        self.revision_file = self.root / "revision.json"
        self.out = self.root / "out"

    # -- revisions (R1c #3) --------------------------------------------------
    def next_revision(self) -> int:
        """Allocate a project-level monotonic revision, assigned when inputs are frozen."""
        with _rev_lock(self.root):
            try:
                n = int(json.loads(self.revision_file.read_text(encoding="utf-8"))["n"])
            except (OSError, ValueError, KeyError):
                n = 0
            n += 1
            _pio.atomic_write_json(self.revision_file, {"n": n})
            return n

    # -- version directories -------------------------------------------------
    def version_dir(self, rid: str) -> Path:
        return self.results / str(rid)

    def inputs_dir(self, rid: str) -> Path:
        return self.version_dir(rid) / "inputs"

    def begin(self, rid: str) -> Path:
        vd = self.version_dir(rid)
        if vd.exists():
            shutil.rmtree(vd, ignore_errors=True)
        (vd / "inputs").mkdir(parents=True, exist_ok=True)
        return vd

    def manifest_path(self, rid: str) -> Path:
        return self.version_dir(rid) / "manifest.json"

    def write_result(self, rid: str, result: dict) -> None:
        """The reader-facing result (figures/numerals/variants/ok), sealed into the version so
        every reader resolves one consistent view via ``current.json`` (R1c #1)."""
        _pio.atomic_write_json(self.version_dir(rid) / "result.json", result)

    def read_result(self, rid: str) -> Optional[dict]:
        try:
            return json.loads((self.version_dir(rid) / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def seal(self, rid: str, manifest: dict, revision: int) -> None:
        """Seal a version with the durability order of R1c #6: fsync all artefacts + the dir,
        then write+fsync a manifest that records each file's size, so recovery/export can verify
        the version was not truncated by a crash."""
        vd = self.version_dir(rid)
        files = {}
        for p in sorted(vd.rglob("*")):
            if p.is_file() and p.name != "manifest.json":
                try:
                    files[str(p.relative_to(vd).as_posix())] = p.stat().st_size
                    _fsync_path(p)
                except OSError:
                    pass
        _fsync_path(vd)
        data = dict(manifest)
        data["complete"] = True
        data["revision"] = int(revision)
        data.setdefault("render_job_id", str(rid))
        data["files"] = files
        _pio.atomic_write_json(self.manifest_path(rid), data)
        _fsync_path(vd)

    # back-compat alias
    def write_manifest(self, rid: str, manifest: dict) -> None:
        self.seal(rid, manifest, int(manifest.get("revision", 0)))

    def read_manifest(self, rid: str) -> Optional[dict]:
        try:
            return json.loads(self.manifest_path(rid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def is_complete(self, rid: str) -> bool:
        m = self.read_manifest(rid)
        return bool(m and m.get("complete"))

    def verify(self, rid: str) -> bool:
        """Complete AND every recorded file present at its recorded size (R1c #6)."""
        m = self.read_manifest(rid)
        if not (m and m.get("complete")):
            return False
        vd = self.version_dir(rid)
        for rel, size in (m.get("files") or {}).items():
            p = vd / rel
            if not p.is_file() or p.stat().st_size != size:
                return False
        return True

    def revision_of(self, rid: str) -> int:
        return int((self.read_manifest(rid) or {}).get("revision", 0))

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

    def current_revision(self) -> int:
        try:
            return int(json.loads(self.current_file.read_text(encoding="utf-8")).get("revision", 0))
        except (OSError, ValueError):
            return 0

    def current_dir(self) -> Optional[Path]:
        """The one version directory every reader should resolve to (R1c #1)."""
        rid = self.current_id()
        if rid and self.is_complete(rid):
            return self.version_dir(rid)
        return None

    def current_result(self) -> Optional[dict]:
        rid = self.current_id()
        return self.read_result(rid) if rid and self.is_complete(rid) else None

    def publish(self, rid: str, revision: Optional[int] = None) -> bool:
        """Commit: atomically point ``current.json`` at a complete, verified version — but only
        if ``revision`` is newer than what is current (monotonic, R1c #3). Returns True if it
        became current, False if refused as stale (the version stays as history). Caller holds
        ``state_lock``."""
        if revision is None:
            revision = self.revision_of(rid)
        if not self.verify(rid):
            raise ValueError("拒绝发布未通过完整性校验的版本 %s" % rid)
        if revision <= self.current_revision() and self.current_id() is not None:
            return False                                 # stale: keep as history, don't overwrite
        _pio.atomic_write_json(self.current_file, {"render_job_id": str(rid), "revision": int(revision)})
        _fsync_path(self.root)                           # fsync the pointer's parent dir (R1c #6)
        self.sync_out(rid)
        return True

    # -- the out/ view -------------------------------------------------------
    def sync_out(self, rid: str) -> None:
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
        rel = os.path.relpath(str(target), str(self.root))
        tmp = self._unique("out.lnk")
        os.symlink(rel, str(tmp))
        try:
            if self.out.is_symlink() or (self.out.exists() and not self.out.is_dir()):
                os.replace(str(tmp), str(self.out))
            elif self.out.is_dir():
                trash = self._unique("out.old")
                os.replace(str(self.out), str(trash))
                os.replace(str(tmp), str(self.out))
                shutil.rmtree(trash, ignore_errors=True)
            else:
                os.replace(str(tmp), str(self.out))
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

    # -- recovery (R1c #5) ---------------------------------------------------
    def recover(self) -> List[str]:
        """Re-point ``out/`` from a complete current version (an interrupted swap is retried
        independently and never undoes the commit), and return the ids of version directories
        that were never sealed (their jobs are reconciled to interrupted by the caller)."""
        incomplete: List[str] = []
        if self.results.is_dir():
            for vd in sorted(self.results.iterdir()):
                if (vd.is_dir() and not self.is_complete(vd.name)
                        and not self.is_cancelled(vd.name)):
                    incomplete.append(vd.name)
        rid = self.current_id()
        if rid and self.is_complete(rid):
            try:
                self.sync_out(rid)
            except OSError:
                pass        # view refresh is retried on the next call; commit stays intact
        return incomplete

    # -- reclamation (R1c #11) ----------------------------------------------
    def reclaim(self, keep_success: int = KEEP_SUCCESS, protected: Optional[set] = None) -> List[str]:
        """Delete reclaimable version directories: all stale failed/cancelled ones, and
        successful ones beyond the most-recent ``keep_success`` — never the current version or
        any id in ``protected`` (running jobs, export references). Returns removed ids."""
        protected = set(protected or set())
        cur = self.current_id()
        if cur:
            protected.add(cur)
        if not self.results.is_dir():
            return []
        dirs = [d for d in self.results.iterdir() if d.is_dir()]
        dirs.sort(key=lambda d: d.stat().st_mtime)
        successes = [d.name for d in dirs if self.is_complete(d.name)]
        keep = set(successes[-keep_success:]) | protected
        removed = []
        for d in dirs:
            if d.name in keep:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
        return removed
