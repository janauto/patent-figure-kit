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

import errno
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from patent_figure import project_io as _pio

_state_locks: Dict[str, "StoreLock"] = {}
_view_locks: Dict[str, "StoreLock"] = {}    # serialises out/ view swaps per store (R1e #6)
#: Leases this PROCESS holds: (store root, rid) -> [lease paths]. The truth is on disk
#: (``<root>/refs/<rid>/<pid>.<token>.lease``); this only drives the heartbeat and release.
_held: Dict[tuple, List[Path]] = {}
_heartbeat_started = False
_guard = threading.Lock()

#: A lockfile whose holder is gone is reclaimed after this long (O_EXCL fallback only — the
#: fcntl / msvcrt locks are released by the kernel when the holder dies).
STALE_LOCK_S = 600.0
LOCK_POLL_S = 0.05

#: Version leases (R1g #2): a holder refreshes its lease files every LEASE_HEARTBEAT_S; a lease
#: is stale when its pid is dead (same host) OR its heartbeat is older than LEASE_TTL_S (covers
#: pid reuse, another host, a frozen holder). Stale leases are removed under the store lock.
LEASE_HEARTBEAT_S = 30.0
LEASE_TTL_S = 600.0


def _platform() -> str:
    """``os.name`` read at CALL time, so tests can simulate the Windows branches on macOS /
    Linux by monkeypatching this function (R1g #3). Every platform switch below goes through it."""
    return os.name


def _fcntl():
    try:
        import fcntl
        return fcntl
    except ImportError:  # pragma: no cover - Windows
        return None


def _msvcrt():
    try:
        import msvcrt
        return msvcrt
    except ImportError:
        return None


def _excl_lock(path: Path):
    """Last-resort portable lock: ``O_CREAT|O_EXCL`` lockfile with pid + time, reclaimed when its
    holder is dead (same host) or the file is older than ``STALE_LOCK_S``."""
    excl = path.with_name(path.name + ".excl")
    while True:
        try:
            fd = os.open(str(excl), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, json.dumps({"pid": os.getpid(), "t": time.time()}).encode("ascii"))
            return ("excl", (fd, excl))
        except FileExistsError:
            try:
                info = json.loads(excl.read_text(encoding="ascii") or "{}")
            except (OSError, ValueError):
                info = {}
            try:
                age = time.time() - excl.stat().st_mtime
            except OSError:
                continue                                    # vanished: retry immediately
            if age > STALE_LOCK_S or (info.get("pid") and not _pid_alive(int(info["pid"]))):
                try:
                    excl.unlink()                           # stale holder: reclaim
                except OSError:
                    pass
                continue
            time.sleep(LOCK_POLL_S)


def _os_lock(path: Path):
    """Take an exclusive CROSS-PROCESS lock on ``path``; returns a handle for ``_os_unlock``.

    POSIX ``fcntl.flock`` / Windows (``_platform() == "nt"``) ``msvcrt.locking``: kernel locks,
    released automatically if the holder dies — no stale lock is possible. If the platform's
    module is unavailable: the ``O_CREAT|O_EXCL`` lockfile (``_excl_lock``) with stale
    reclamation. The Windows branch is exercised on macOS by monkeypatching ``_platform`` /
    ``_msvcrt`` (tests); it is **pending verification on a real Windows machine**."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if _platform() == "nt":
        msvcrt = _msvcrt()
        if msvcrt is not None:
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                while True:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(LOCK_POLL_S)
            except BaseException:
                os.close(fd)
                raise
            return ("msvcrt", fd)
        return _excl_lock(path)
    fcntl = _fcntl()
    if fcntl is not None:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        return ("fcntl", fd)
    return _excl_lock(path)


def _os_unlock(handle) -> None:
    kind, h = handle
    if kind == "fcntl":
        try:
            _fcntl().flock(h, _fcntl().LOCK_UN)
        finally:
            os.close(h)
    elif kind == "msvcrt":
        msvcrt = _msvcrt()
        try:
            os.lseek(h, 0, os.SEEK_SET)
            msvcrt.locking(h, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(h)
    else:
        fd, excl = h
        os.close(fd)
        try:
            excl.unlink()
        except OSError:
            pass


# -- process liveness (lease + lock staleness) ---------------------------------

_STILL_ACTIVE = 259                     # Windows GetExitCodeProcess "still running"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _kernel32():  # pragma: no cover - Windows only; monkeypatched in tests
    import ctypes
    return ctypes.windll.kernel32


def _pid_alive(pid: int) -> bool:
    """Is process ``pid`` (on this host) still running?

    POSIX: ``os.kill(pid, 0)`` (ESRCH → dead, EPERM → alive under another user). Windows: NEVER
    ``os.kill`` (any signal other than CTRL_* *terminates* the target) — ``OpenProcess`` +
    ``GetExitCodeProcess`` via ``_kernel32()``; simulated on macOS in tests, **pending real
    Windows verification**."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if _platform() == "nt":
        try:
            k32 = _kernel32()
            h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            try:
                import ctypes
                code = ctypes.c_ulong(0)
                ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == _STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True                  # cannot tell: treat as alive; the TTL still applies
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class StoreLock:
    """A reentrant lock that is exclusive across THREADS and across PROCESSES (R1f #1).

    In-process threads serialise on an ``RLock``; the outermost holder additionally takes the
    store's lockfile (``_os_lock``), so two CLI processes writing the same ``-o`` — or anything
    else sharing a version store — exclude each other for revision allocation, the current
    compare-and-replace, cancellation decisions, reference pinning and reclamation. Re-entry by
    the holding thread only bumps a depth counter (no second OS lock, which flock would treat
    as a separate, self-deadlocking owner)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._rlock = threading.RLock()
        self._depth = 0
        self._handle = None

    def acquire(self) -> bool:
        self._rlock.acquire()
        if self._depth == 0:
            try:
                self._handle = _os_lock(self.path)
            except BaseException:
                self._rlock.release()
                raise
        self._depth += 1
        return True

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            handle, self._handle = self._handle, None
            try:
                _os_unlock(handle)
            finally:
                self._rlock.release()
        else:
            self._rlock.release()

    def __enter__(self) -> "StoreLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

#: A version id as it may appear in a URL (``?v=<rid>``): no separators, no traversal.
RID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")

#: Store metadata that lives in a version dir but is not a deliverable — never carried over by
#: a ``--only`` merge and never part of an export.
META_FILES = ("manifest.json", "result.json", "cancelled.json", "render-report.json")


def _view_lock(ws_root) -> StoreLock:
    key = str(Path(ws_root).resolve())
    with _guard:
        lk = _view_locks.get(key)
        if lk is None:
            lk = _view_locks[key] = StoreLock(Path(key) / ".view.lock")
        return lk

#: Default retention: keep this many most-recent successful (non-current) versions; older
#: successful versions and all stale failed/cancelled versions are reclaimable (R1c #11).
KEEP_SUCCESS = 5


def is_unsealed_version_dir(path) -> bool:
    """Is ``path`` an in-progress version directory (``<store>/results/<rid>/``, not yet sealed,
    not cancelled)? The renderer's internal ``--in-place`` mode may ONLY write into such a
    directory — never into a published version, the ``out/`` view or an arbitrary folder — so
    it cannot bypass the publish service (R1f bypass list)."""
    p = Path(path)
    try:
        return (p.is_dir() and not p.is_symlink() and p.parent.name == "results"
                and bool(RID_RE.match(p.name))
                and not (p / "manifest.json").exists() and not (p / "cancelled.json").exists())
    except OSError:
        return False


#: Names directly under a store root that belong to the store (never a user write target).
STORE_RESERVED = ("results", "refs", "out", "current.json", "revision.json", "view-dirty.json",
                  ".store.lock", ".view.lock", ".store.lock.excl", ".view.lock.excl")


def is_store_root(d: Path) -> bool:
    d = Path(d)
    try:
        return (d / "results").is_dir() and any(
            (d / m).exists() for m in ("current.json", "revision.json", ".store.lock"))
    except OSError:
        return False


def protected_path_reason(path, resolve: bool = True) -> Optional[str]:
    """Why ``path`` must not be written by a tool run (None if it may). R1g #1: any path that —
    lexically OR after resolving symlink aliases — lies in a version store's reserved area:
    a version directory (``results/…``, sealed or not), the leases (``refs/…``), the ``out``
    view, or the store metadata (``current.json`` …)."""
    raw = os.fspath(path)
    raw = raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)   # keep ``..`` (R1i)
    lexical = Path(os.path.abspath(raw))
    real = Path(os.path.realpath(raw))          # resolve symlinks BEFORE applying ``..``
    for cand in ((lexical, real) if resolve else (lexical,)):
        for anc in [cand] + list(cand.parents):
            if anc.name in STORE_RESERVED and is_store_root(anc.parent):
                return "位于版本库保留区（%s）：%s" % (anc.name, anc)
    return None


def state_lock(ws_root) -> StoreLock:
    """THE lock of a version store — reentrant, cross-thread and cross-process (R1f #1): held
    for revision allocation, the current compare-and-replace (commit), cancellation, reference
    pinning (export) and reclamation. Located by the store root (``<root>/.store.lock``)."""
    key = str(Path(ws_root).resolve())
    with _guard:
        lk = _state_locks.get(key)
        if lk is None:
            lk = _state_locks[key] = StoreLock(Path(key) / ".store.lock")
        return lk


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


def _host() -> str:
    try:
        import socket
        return socket.gethostname()
    except Exception:  # pragma: no cover
        return ""


def _heartbeat_once() -> None:
    """Refresh the mtime of every lease this process holds (the liveness signal)."""
    with _guard:
        paths = [p for lst in _held.values() for p in lst]
    now = time.time()
    for path in paths:
        try:
            os.utime(str(path), (now, now))
        except OSError:
            pass


def _start_heartbeat() -> None:
    global _heartbeat_started
    with _guard:
        if _heartbeat_started:
            return
        _heartbeat_started = True

    def loop():
        while True:
            time.sleep(LEASE_HEARTBEAT_S)
            _heartbeat_once()

    threading.Thread(target=loop, name="pfk-lease-heartbeat", daemon=True).start()


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
        """Allocate a project-level monotonic revision, assigned when inputs are frozen. Under
        the cross-process store lock: two processes can never draw the same number (R1f #1)."""
        with state_lock(self.root):
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

    # -- version references: cross-process LEASES (R1d #6 / R1e #3,#7 / R1g #2) ----
    @property
    def refs_dir(self) -> Path:
        return self.root / "refs"

    def lease_path(self, rid: str, pid: int, token: str) -> Path:
        """``<root>/refs/<rid>/<pid>.<token>.lease`` — visible to every process sharing the store."""
        return self.refs_dir / str(rid) / ("%d.%s.lease" % (int(pid), token))

    def _lease_locked(self, rid: str) -> Path:
        """Write a lease for ``rid`` held by THIS process (caller holds the store lock)."""
        if not RID_RE.match(str(rid)):
            raise ValueError("非法版本 id：%r" % rid)
        token = uuid.uuid4().hex[:12]
        path = self.lease_path(rid, os.getpid(), token)
        _pio.atomic_write_json(path, {"pid": os.getpid(), "host": _host(), "rid": str(rid),
                                      "token": token, "created": time.time()})
        with _guard:
            _held.setdefault((str(self.root.resolve()), str(rid)), []).append(path)
        _start_heartbeat()
        return path

    def _lease_is_live(self, path: Path) -> bool:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            age = time.time() - path.stat().st_mtime
        except (OSError, ValueError):
            return False                                  # torn / vanished → not a live holder
        if age > LEASE_TTL_S:
            return False                                  # no heartbeat: pid reuse / frozen
        if info.get("host") not in (None, _host()):
            return True                                   # another host: only the TTL applies
        return _pid_alive(int(info.get("pid") or 0))

    def ref(self, rid: str) -> None:
        """Pin a version against reclamation (a cross-process lease) for a long read."""
        with state_lock(self.root):
            self._lease_locked(rid)

    def unref(self, rid: str) -> None:
        """Release ONE lease this process holds on ``rid`` (no-op if none)."""
        with state_lock(self.root):
            with _guard:
                lst = _held.get((str(self.root.resolve()), str(rid))) or []
                path = lst.pop() if lst else None
                if not lst:
                    _held.pop((str(self.root.resolve()), str(rid)), None)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
                try:
                    path.parent.rmdir()                   # last lease of this rid → tidy
                except OSError:
                    pass

    def referenced(self) -> set:
        """Version ids with at least one LIVE lease from ANY process. Stale leases (dead pid,
        no heartbeat within LEASE_TTL_S) are deleted here, under the store lock."""
        live = set()
        with state_lock(self.root):
            if not self.refs_dir.is_dir():
                return live
            for rdir in self.refs_dir.iterdir():
                if not rdir.is_dir():
                    continue
                for lease in rdir.glob("*.lease"):
                    if self._lease_is_live(lease):
                        live.add(rdir.name)
                    else:
                        try:
                            lease.unlink()
                        except OSError:
                            pass
                try:
                    rdir.rmdir()                          # only succeeds when now empty
                except OSError:
                    pass
        return live

    def acquire_verified_version(self, rid=None):
        """Atomically (R1e #3/#7) resolve + verify + pin a version under the store lock — the
        same cross-process lock ``reclaim`` decides under — so nothing can delete it between the
        check and the pin. The pin is a LEASE FILE (R1g #2), so a reclaim in ANOTHER process
        sees it too; if this process dies the lease goes stale and becomes reclaimable.
        ``rid=None`` resolves the current version. The version must be publishable
        (integrity-verified against its declared artefact list, sealed ``ok`` and result
        ``ok``). Returns ``(rid, version_dir)``; raises ``ValueError`` otherwise. Callers
        ``release(rid)`` on every exit path (see ``patent_figure.export.Pin``)."""
        with state_lock(self.root):
            if rid is None:
                rid = self.current_id()
            if not rid or not self.is_publishable(rid):
                raise ValueError("没有可导出的版本（缺失、未完成、未通过完整性校验或未全部过闸）：%r"
                                 % rid)
            self._lease_locked(rid)
            return rid, self.version_dir(rid)

    def release(self, rid: str) -> None:
        self.unref(rid)

    def commit_pointer(self, rid: str, revision: Optional[int] = None) -> bool:
        """Commit ONLY the pointer (R1d #3): verify integrity, enforce monotonic revision, write
        ``current.json`` atomically + fsync its parent dir. Does NOT touch the ``out/`` view —
        that is refreshed separately so a view-refresh failure can never un-commit. Returns True
        if this version became current, False if refused as stale."""
        if revision is None:
            revision = self.revision_of(rid)
        # The compare-and-replace runs under the cross-process store lock (reentrant: callers
        # that already hold it for the cancel decision just nest) — another process cannot
        # slip a newer commit in between the comparison and the write (R1f #1).
        with state_lock(self.root):
            # ok=false never switches (R1e #1/#2): the store itself refuses, not the callers.
            if not self.is_publishable(rid):
                raise ValueError("拒绝发布未通过完整性校验或未全部过闸（ok=false）的版本 %s" % rid)
            if revision <= self.current_revision() and self.current_id() is not None:
                return False                             # stale: keep as history, don't overwrite
            _pio.atomic_write_json(self.current_file,
                                   {"render_job_id": str(rid), "revision": int(revision)})
            _fsync_path(self.root)                       # fsync the pointer's parent (R1c #6)
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
