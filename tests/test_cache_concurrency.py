"""GeometryCache concurrency + versioning tests (WP1 deliverable 1, Codex R0 S0-03 / S1-05).

These exercise the on-disk cache directly with synthetic ``(N, 2)`` arrays — no OCC call is
needed, so the test is fast and deterministic. What they pin:

* a cache write is published atomically (unique temp + verify + ``os.replace``), so
  concurrent writers of the same key never tear one file and never leave a half-file that
  later reads as a hit;
* two projects over the same STEP share one path and do not corrupt each other;
* a process killed mid-write leaves only an orphan ``.tmp`` — never a committed bad file;
* a corrupted cache file reads as a miss and is recovered by the next write;
* the cache namespace carries the algorithm + OCCT-binding version, so a stale algorithm's
  cache can never be served as a hit;
* the per-entry HLR drop delta survives a round trip (so the render gate is cache-independent).
"""

from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import occ_backend as occ  # noqa: E402


VIEW = occ.ViewFrame(35.0, 20.0, 0.0)
DEFL = 0.02


def _curves(seed: int):
    """A deterministic, non-trivial curve list keyed by ``seed``."""
    rng = np.arange(seed, seed + 8, dtype=np.float64).reshape(4, 2)
    return [rng, rng * 2.0 + 0.5, np.array([[0.0, 0.0], [float(seed), 1.0]])]


def _same(a, b) -> bool:
    return len(a) == len(b) and all(np.array_equal(x, y) for x, y in zip(a, b))


def test_round_trip_and_drops(tmp_path):
    cache = occ.GeometryCache(tmp_path, "a" * 40)
    curves = _curves(1)
    cache.put("PRT#0", VIEW, DEFL, curves, drops=(3, 1))
    got = cache.get("PRT#0", VIEW, DEFL)
    assert got is not None and _same(got, curves)
    assert cache.get_drops("PRT#0", VIEW, DEFL) == (3, 1)
    # A hit is bit-identical, not merely close.
    assert got[0].tobytes() == curves[0].tobytes()


def test_missing_is_a_miss(tmp_path):
    cache = occ.GeometryCache(tmp_path, "b" * 40)
    assert cache.get("nope", VIEW, DEFL) is None
    assert cache.get_drops("nope", VIEW, DEFL) is None


def test_empty_curve_list_is_a_legitimate_hit(tmp_path):
    cache = occ.GeometryCache(tmp_path, "c" * 40)
    cache.put("empty", VIEW, DEFL, [], drops=(0, 0))
    got = cache.get("empty", VIEW, DEFL)
    assert got == []                      # a real hit, not a miss
    assert cache.get_drops("empty", VIEW, DEFL) == (0, 0)


def test_namespace_carries_algo_and_occt_version(tmp_path):
    cache = occ.GeometryCache(tmp_path, "d" * 40)
    assert cache.namespace == "fmt%d_algo%d_ocp%s" % (
        occ.CACHE_FORMAT, occ.ALGO_VERSION, occ.occt_version())
    cache.put("k", VIEW, DEFL, _curves(2))
    written = list((tmp_path / cache.namespace).rglob("*.npz"))
    assert written, "cache file should live under the versioned namespace"


def test_bumping_algo_version_busts_the_cache(tmp_path, monkeypatch):
    c1 = occ.GeometryCache(tmp_path, "e" * 40)
    c1.put("k", VIEW, DEFL, _curves(3))
    assert c1.get("k", VIEW, DEFL) is not None
    # A different algorithm version must land on a different path -> miss, never a stale hit.
    monkeypatch.setattr(occ, "ALGO_VERSION", occ.ALGO_VERSION + 1)
    c2 = occ.GeometryCache(tmp_path, "e" * 40)
    assert c2.namespace != c1.namespace
    assert c2.get("k", VIEW, DEFL) is None


def test_no_orphan_temp_after_successful_put(tmp_path):
    cache = occ.GeometryCache(tmp_path, "f" * 40)
    cache.put("k", VIEW, DEFL, _curves(4))
    leftovers = list(tmp_path.rglob("*.tmp"))
    assert not leftovers, "a successful put must leave no .tmp file: %s" % leftovers


def test_concurrent_writers_same_key_never_tear(tmp_path):
    """Many threads writing the SAME key concurrently: the published file is always a
    complete, loadable npz, and no .tmp orphan survives. This is the exact S0-03 race
    (duplicate instances / two figures sharing a part)."""
    cache = occ.GeometryCache(tmp_path, "g" * 40)
    curves = _curves(5)
    errors = []

    def writer(_i):
        try:
            for _ in range(20):
                cache.put("shared", VIEW, DEFL, curves, drops=(1, 0))
                got = cache.get("shared", VIEW, DEFL)
                assert got is not None and _same(got, curves)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(8)))
    assert not errors, errors
    final = cache.get("shared", VIEW, DEFL)
    assert final is not None and _same(final, curves)
    assert not list(tmp_path.rglob("*.tmp")), "no torn temp file may survive"


def test_two_projects_same_step_share_path_safely(tmp_path):
    """Two GeometryCache objects (two projects) over the same STEP sha resolve to the same
    file and may both write it without corruption."""
    sha = "h" * 40
    a = occ.GeometryCache(tmp_path, sha)
    b = occ.GeometryCache(tmp_path, sha)
    assert a._path("k", VIEW, DEFL) == b._path("k", VIEW, DEFL)
    curves = _curves(6)
    a.put("k", VIEW, DEFL, curves)
    b.put("k", VIEW, DEFL, curves)        # second project overwrites identically
    assert _same(b.get("k", VIEW, DEFL), curves)
    assert _same(a.get("k", VIEW, DEFL), curves)


def test_kill_mid_write_leaves_the_good_file_intact(tmp_path, monkeypatch):
    """Simulate a crash during publish: os.replace raises after the temp is written. The
    previously committed file must be untouched and the orphan temp removed by the finally."""
    cache = occ.GeometryCache(tmp_path, "i" * 40)
    good = _curves(7)
    cache.put("k", VIEW, DEFL, good)      # commit a good version first

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated crash during os.replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        cache.put("k", VIEW, DEFL, _curves(99))   # attempted overwrite crashes
    monkeypatch.setattr(os, "replace", real_replace)

    # The reader still sees the complete OLD file, never a half-written one.
    assert _same(cache.get("k", VIEW, DEFL), good)
    assert not list(tmp_path.rglob("*.tmp")), "the orphan temp must be cleaned up"


def test_corrupted_cache_reads_as_miss_then_recovers(tmp_path):
    cache = occ.GeometryCache(tmp_path, "j" * 40)
    cache.put("k", VIEW, DEFL, _curves(8))
    path = cache._path("k", VIEW, DEFL)
    path.write_bytes(b"not a valid npz at all")      # corrupt it
    assert cache.get("k", VIEW, DEFL) is None          # treated as a miss, never raises
    assert cache.get_drops("k", VIEW, DEFL) is None
    cache.put("k", VIEW, DEFL, _curves(8))             # next write recovers it
    assert _same(cache.get("k", VIEW, DEFL), _curves(8))


def test_single_flight_serialises_but_never_deadlocks(tmp_path):
    """Two threads contend for the same key's lock. Only one holds it at a time (True), and
    both make progress — the lock is advisory, so neither blocks forever."""
    cache = occ.GeometryCache(tmp_path, "k" * 40)
    order = []
    lock = threading.Lock()

    def work(i):
        with cache.single_flight("k", VIEW, DEFL) as held:
            with lock:
                order.append((i, held))
            # hold briefly so the peer must wait on the lockfile
            cache.put("k", VIEW, DEFL, _curves(10 + i))

    t1 = threading.Thread(target=work, args=(0,))
    t2 = threading.Thread(target=work, args=(1,))
    t1.start(); t2.start(); t1.join(timeout=10); t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive(), "single_flight must not deadlock"
    assert len(order) == 2
    assert cache.get("k", VIEW, DEFL) is not None
    # No lockfile may survive a clean release.
    assert not list(tmp_path.rglob("*.lock"))


def test_single_flight_timeout_computes_anyway(tmp_path, monkeypatch):
    """If a peer holds the lock past the timeout (here: a stale lockfile nobody releases),
    the waiter must give up and compute anyway (yield False), never hang."""
    monkeypatch.setattr(occ, "SINGLE_FLIGHT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(occ, "SINGLE_FLIGHT_POLL_S", 0.02)
    cache = occ.GeometryCache(tmp_path, "l" * 40)
    # Plant a stale lockfile that is never released.
    lock = cache._lock_path("k", VIEW, DEFL)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("stale")
    with cache.single_flight("k", VIEW, DEFL) as held:
        assert held is False, "a timed-out waiter must proceed with held=False"
