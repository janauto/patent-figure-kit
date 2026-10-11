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
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from patent_figure import project_io as _pio

_state_locks: Dict[str, threading.RLock] = {}
_rev_locks: Dict[str, threading.Lock] = {}
_view_locks: Dict[str, threading.Lock] = {}  # serialises out/ view swaps per workspace (R1e #6)
_refs: Dict[str, Dict[str, int]] = {}        # ws_root -> {rid: refcount}, guarded by state_lock
_guard = threading.Lock()

#: A version id as it may appear in a URL (``?v=<rid>``): no separators, no traversal.
RID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")

#: Store metadata that lives in a version dir but is not a deliverable — never carried over by
#: a ``--only`` merge and never part of an export.
META_FILES = ("manifest.json", "result.json", "cancelled.json", "render-report.json")


def _view_lock(ws_root) -> threading.Lock:
    key = str(Path(ws_root).resolve())
    with _guard:
        return _view_locks.setdefault(key, threading.Lock())

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
    def __init__(self, ws_root, out_view=None) -> None:
        self.root = Path(ws_root)
        self.results = self.root / "results"
        self.current_file = self.root / "current.json"
        self.revision_file = self.root / "revision.json"
        # The out/ view defaults to root/"out" (workbench), but the direct CLI points it at the
        # user's -o directory so published figures stay at ``<out>/fig1.dxf`` (R1e #1).
        self.out = Path(out_view) if out_view is not None else self.root / "out"

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
        """Integrity of a sealed version (R1c #6 / R1e #2):

        * complete, and every file the seal recorded is present at its recorded size;
        * if the version claims ``ok``, every artefact on its declared ``required_artefacts``
          list (the expected deliverable set — figures × raw/disclosure/filing × DXF/SVG/report
          + the numerals table) is present, recorded by the seal, and non-empty. Verification is
          by the declared list, not by whatever files the seal happened to find.

        A failed (``ok=false``) version is never deliverable; for it verify only answers "is
        what was sealed still intact" (its diag/ is kept for inspection)."""
        m = self.read_manifest(rid)
        if not (m and m.get("complete")):
            return False
        vd = self.version_dir(rid)
        files = m.get("files") or {}
        for rel, size in files.items():
            p = vd / rel
            try:
                if not p.is_file() or p.stat().st_size != size:
                    return False
            except OSError:
                return False
        if m.get("ok"):
            for rel in (m.get("required_artefacts") or []):
                p = vd / rel
                try:
                    if rel not in files or not p.is_file() or p.stat().st_size == 0:
                        return False
                except OSError:
                    return False
        return True

    def is_publishable(self, rid: str) -> bool:
        """The one gate for "may be current / may be exported" (R1e #2/#3/#4): integrity-verified
        AND the seal says ``ok`` AND the sealed reader result says ``ok``."""
        if not rid or not self.verify(rid):
            return False
        m = self.read_manifest(rid) or {}
        res = self.read_result(rid)
        return bool(m.get("ok")) and res is not None and bool(res.get("ok"))

    def sealed_dir(self, rid: str) -> Optional[Path]:
        """The directory of a sealed (complete) version named by an untrusted id (a ``?v=`` URL
        parameter), or None. Rejects ids that are not plain names — no path traversal."""
        if not rid or not RID_RE.match(str(rid)):
            return None
        if not self.is_complete(rid):
            return None
        return self.version_dir(rid)

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

    def snapshot(self):
        """Resolve the current version in ONE read and return ``(rid, vdir, result)`` — every
        reader/exporter uses this so figures, qa, numerals, descriptions and images all come
        from the same version, never racing the pointer (R1d #2). Returns (None, None, None)
        when there is no published version. Repairs the ``out/`` view lazily if it is dirty."""
        rid = self.current_id()
        if not rid or not self.is_complete(rid):
            return None, None, None
        self.ensure_view()
        return rid, self.version_dir(rid), self.read_result(rid)

    # -- out/ view + dirty marker (R1d #3/#4) --------------------------------
    @property
    def _view_dirty_file(self) -> Path:
        return self.root / "view-dirty.json"

    def mark_view_dirty(self, rid: str) -> None:
        try:
            _pio.atomic_write_json(self._view_dirty_file, {"render_job_id": str(rid)})
        except OSError:
            pass

    def refresh_view(self) -> bool:
        """Refresh the ``out/`` view to the current version. Serialised by a dedicated view
        lock and re-reading ``current.json`` INSIDE it, then re-checking current after the swap:
        if the pointer advanced while we were swapping (a newer commit raced us — another
        thread, or another process that does not share the in-process lock), loop and install
        the newer one, so the view can never settle on a stale revision (R1e #6). A swap failure
        persists a dirty marker and returns False; the commit always stands. With no current
        version the view is removed (a cleared pointer must not leave out/ naming a quarantined
        version, R1e #4)."""
        with _view_lock(self.root):
            for _ in range(16):
                rid = self.current_id()
                try:
                    if not rid:
                        self.clear_view()
                    else:
                        self.sync_out(rid)
                except OSError:
                    self.mark_view_dirty(rid or "")
                    return False
                if self.current_id() == rid:            # pointer unchanged → view is up to date
                    try:
                        self._view_dirty_file.unlink()
                    except OSError:
                        pass
                    return bool(rid)
                # else: a newer revision committed during our swap — loop and install it
            self.mark_view_dirty(self.current_id() or "")
            return False

    def view_is_dirty(self) -> bool:
        return self._view_dirty_file.is_file()

    def ensure_view(self) -> None:
        """Cheap: if a prior view refresh failed (dirty marker present), retry it now."""
        if self._view_dirty_file.is_file():
            self.refresh_view()

    # -- version references (R1d #6 / R1e #3,#7) -----------------------------
    def _ref_locked(self, rid: str) -> None:
        d = _refs.setdefault(str(self.root.resolve()), {})
        d[str(rid)] = d.get(str(rid), 0) + 1

    def ref(self, rid: str) -> None:
        """Pin a version against reclamation for the duration of a long read (export)."""
        with state_lock(self.root):
            self._ref_locked(rid)

    def unref(self, rid: str) -> None:
        with state_lock(self.root):
            d = _refs.get(str(self.root.resolve()), {})
            if d.get(str(rid), 0) <= 1:
                d.pop(str(rid), None)
            else:
                d[str(rid)] -= 1

    def referenced(self) -> set:
        with state_lock(self.root):
            return set(_refs.get(str(self.root.resolve()), {}).keys())

    def acquire_verified_version(self, rid=None):
        """Atomically (R1e #3/#7) resolve + verify + pin a version under the state lock — the
        same lock ``reclaim`` decides under — so nothing can delete it between the check and the
        pin. ``rid=None`` resolves the current version. The version must be publishable
        (integrity-verified against its declared artefact list, sealed ``ok`` and result
        ``ok``): a missing / truncated / zero-byte required artefact is refused. Returns
        ``(rid, version_dir)``; raises ``ValueError`` otherwise. Callers ``release(rid)`` on
        every exit path (see ``patent_figure.export.Pin``)."""
        with state_lock(self.root):
            if rid is None:
                rid = self.current_id()
            if not rid or not self.is_publishable(rid):
                raise ValueError("没有可导出的版本（缺失、未完成、未通过完整性校验或未全部过闸）：%r"
                                 % rid)
            self._ref_locked(rid)
            return rid, self.version_dir(rid)

    def release(self, rid: str) -> None:
        self.unref(rid)

    def commit_pointer(self, rid: str, revision: Optional[int] = None) -> bool:
        """Commit ONLY the pointer (R1d #3): verify integrity, enforce monotonic revision, write
        ``current.json`` atomically + fsync its parent dir. Does NOT touch the ``out/`` view —
        that is refreshed separately so a view-refresh failure can never un-commit. Returns True
        if this version became current, False if refused as stale. Caller holds ``state_lock``."""
        if revision is None:
            revision = self.revision_of(rid)
        # ok=false never switches (R1e #1/#2): the store itself refuses, not just the callers.
        if not self.is_publishable(rid):
            raise ValueError("拒绝发布未通过完整性校验或未全部过闸（ok=false）的版本 %s" % rid)
        if revision <= self.current_revision() and self.current_id() is not None:
            return False                                 # stale: keep as history, don't overwrite
        _pio.atomic_write_json(self.current_file,
                               {"render_job_id": str(rid), "revision": int(revision)})
        _fsync_path(self.root)                           # fsync the pointer's parent dir (R1c #6)
        return True

    def publish(self, rid: str, revision: Optional[int] = None) -> bool:
        """Back-compat convenience: commit the pointer, then (best-effort) refresh the view."""
        became = self.commit_pointer(rid, revision)
        if became:
            self.refresh_view()
        return became

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
        # Stage swap temporaries in the directory that CONTAINS the view, not the store root:
        # os.replace onto the view is then a same-directory atomic rename, and a relative symlink
        # target stays valid after the move. The view may live OUTSIDE the store root (R1e #1 —
        # direct-CLI sets out=the user's -o dir while the store sits in a sibling). For the
        # default view (root/out) this is still the root, so nothing changes there.
        self.out.parent.mkdir(parents=True, exist_ok=True)
        return self.out.parent / ("%s.%d.%s" % (stem, os.getpid(), uuid.uuid4().hex[:8]))

    def _install_view(self, staged: Path) -> None:
        """Install a staged view (a symlink or a copied directory) at ``out``.

        * ``out`` absent, a symlink, or a file → ONE ``os.replace`` (atomic on POSIX).
        * ``out`` a real directory (Windows copy view / legacy dir) → a directory cannot be
          atomically replaced, so it is moved aside first. If installing the new view then
          fails (the "second rename"), the old view is renamed BACK — nothing is deleted until
          the new view is in place, and on failure the previous view is restored intact and the
          staged temp removed (R1e #1). The aside copy is removed only after success.
        """
        if self.out.is_symlink() or (self.out.exists() and not self.out.is_dir()) \
                or not self.out.exists():
            os.replace(str(staged), str(self.out))
            return
        aside = self._unique("out.old")
        os.replace(str(self.out), str(aside))
        try:
            os.replace(str(staged), str(self.out))
        except OSError:
            try:
                os.replace(str(aside), str(self.out))      # roll the old view back
            except OSError:
                pass
            raise
        shutil.rmtree(aside, ignore_errors=True)

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    def _symlink_swap(self, target: Path) -> None:
        rel = os.path.relpath(str(target), str(self.out.parent))
        tmp = self._unique("out.lnk")
        os.symlink(rel, str(tmp))
        try:
            self._install_view(tmp)
        except OSError:
            self._discard(tmp)
            raise

    def _copy_swap(self, target: Path) -> None:
        staging = self._unique("out.new")
        try:
            shutil.copytree(str(target), str(staging))
            self._install_view(staging)
        except OSError:
            self._discard(staging)
            raise

    def clear_view(self) -> None:
        """Remove the ``out/`` view (no current version). A symlink is unlinked; a copy view is
        moved aside and then deleted (it is only a copy — the versions are untouched)."""
        if self.out.is_symlink() or (self.out.exists() and not self.out.is_dir()):
            self.out.unlink()
        elif self.out.is_dir():
            aside = self._unique("out.old")
            os.replace(str(self.out), str(aside))
            shutil.rmtree(aside, ignore_errors=True)

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
        if (self.current_id() and self.is_complete(self.current_id())) \
                or self._view_dirty_file.is_file():
            self.refresh_view()     # retries a dirty/interrupted swap; never un-commits
        return incomplete

    def _latest_ok_version(self, exclude: Optional[str] = None):
        """The most-recent (highest revision) publishable version (verified + sealed ok +
        result ok). Returns ``(rid, revision)`` or ``None``."""
        best = None
        if not self.results.is_dir():
            return None
        for d in self.results.iterdir():
            if not d.is_dir() or d.name == exclude or self.is_cancelled(d.name):
                continue
            if self.is_publishable(d.name):
                rev = self.revision_of(d.name)
                if best is None or rev > best[1]:
                    best = (d.name, rev)
        return best

    def reconcile_current(self) -> Optional[str]:
        """R1e #4: a committed ``current`` is only trustworthy if it is integrity-verified AND
        its sealed ``result.ok`` is true. Commits are ok-gated, so this normally holds; but a
        legacy pointer, a crash, or a tampered store could leave ``current`` naming a version
        that fails verify or whose result is not ok. In that case roll the pointer back to the
        most-recent verified-ok version, or clear it if none exists, and mark the view dirty so
        it is rebuilt off the healthy version. Returns the id of the isolated bad current (for
        the caller to reconcile that job to FAILED), or ``None`` when current was already sound.
        """
        with state_lock(self.root):
            cur = self.current_id()
            if not cur:
                return None
            if self.is_publishable(cur):
                return None                               # healthy — nothing to do
            replacement = self._latest_ok_version(exclude=cur)
            if replacement:
                rid, rev = replacement
                _pio.atomic_write_json(self.current_file,
                                       {"render_job_id": str(rid), "revision": int(rev)})
            else:
                try:
                    self.current_file.unlink()
                except OSError:
                    pass
            _fsync_path(self.root)
            # The view still names the quarantined version: mark it dirty so the next refresh
            # (recover(), or the next read) re-points it — or removes it when cleared.
            self.mark_view_dirty(self.current_id() or "")
        self.refresh_view()
        return cur

    # -- reclamation (R1c #11) ----------------------------------------------
    def reclaim(self, keep_success: int = KEEP_SUCCESS, protected: Optional[set] = None,
                protect_unsealed: bool = False) -> List[str]:
        """Delete reclaimable version directories: all stale failed/cancelled ones, and
        successful ones beyond the most-recent ``keep_success`` — never the current version or
        any id in ``protected`` (running jobs, export references). ``protect_unsealed`` keeps
        every not-yet-sealed dir (the direct CLI cannot know whether another process is still
        rendering into one). Returns removed ids."""
        # Run the whole decision under the state lock so a concurrent ref() (export pinning a
        # version) and the reclaim set are consistent (R1d #6).
        with state_lock(self.root):
            protected = set(protected or set())
            protected |= self.referenced()               # versions pinned by active reads
            cur = self.current_id()
            if cur:
                protected.add(cur)
            if not self.results.is_dir():
                return []
            dirs = [d for d in self.results.iterdir() if d.is_dir()]
            dirs.sort(key=lambda d: d.stat().st_mtime)
            successes = [d.name for d in dirs if self.is_complete(d.name)]
            # (``[-0:]`` would be the WHOLE list — keep_success=0 must keep none.)
            recent = successes[-keep_success:] if keep_success > 0 else []
            keep = set(recent) | protected
            if protect_unsealed:
                keep |= {d.name for d in dirs if not self.is_complete(d.name)
                         and not self.is_cancelled(d.name)}
            removed = []
            for d in dirs:
                if d.name in keep:
                    continue
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d.name)
            return removed
