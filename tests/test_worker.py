"""Resident worker tests (WP1 deliverable 3, Codex R0 S1-06 / S1-08).

The worker's one job is to be invisible: a CLI run through it must produce exactly what a
subprocess produces, across a sequence of jobs and across a crash/rebuild. These tests pin
that — digest equality (base and extended), A→B→A and fail→success consistency, the
subprocess fallback, the timeout kill-and-rebuild, and the per-task drop-stat reset.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "tests"))

import test_golden as tg  # noqa: E402
from patent_figure import worker as W  # noqa: E402
from patent_figure.sheet import normalized_digest, normalized_digest_ext  # noqa: E402

RP = str(_ROOT / "scripts" / "render_patent_figure.py")


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    work = tmp_path_factory.mktemp("worker")
    plan, asm = tg.prepare(work)
    return {"work": work, "plan": plan, "asm": asm}


@pytest.fixture(scope="module")
def shared_worker():
    w = W.Worker()
    yield w
    w.close()


def _render(worker, plan, asm, out, extra=()):
    return worker.run([RP, str(plan), "--assembly", str(asm), "-o", str(out)] + list(extra))


def _dig(out):
    return {f: normalized_digest(out / (f + ".dxf")) for f in ("fig1", "fig2")}


def _ext(out):
    return {f: normalized_digest_ext(out / (f + ".dxf")) for f in ("fig1", "fig2")}


def test_worker_digest_matches_subprocess(prepared, shared_worker):
    """The core N15 claim: the same plan rendered through the worker and through a plain
    subprocess has an identical normalized digest — base AND extended."""
    cli = prepared["work"] / "cli"
    assert tg.render_in_subprocess(prepared["plan"], prepared["asm"], cli).returncode == 0
    wk = prepared["work"] / "wk"
    res = _render(shared_worker, prepared["plan"], prepared["asm"], wk)
    assert res.returncode == 0, res.stderr
    assert _dig(cli) == _dig(wk)
    assert _ext(cli) == _ext(wk)


def test_worker_returns_completed_process_shape(prepared, shared_worker):
    res = _render(shared_worker, prepared["plan"], prepared["asm"], prepared["work"] / "shape")
    assert res.returncode == 0
    assert isinstance(res.stdout, str) and isinstance(res.stderr, str)
    assert "共 2 张图" in res.stdout


def test_sequence_a_b_a_is_consistent(prepared, shared_worker):
    """A→B→A: rendering A, then a different job B, then A again yields A's exact digest both
    times — no leaked process state (S1-06)."""
    a1 = prepared["work"] / "a1"
    _render(shared_worker, prepared["plan"], prepared["asm"], a1)
    _render(shared_worker, prepared["plan"], prepared["asm"], prepared["work"] / "b",
            extra=["--only", "fig2"])
    a2 = prepared["work"] / "a2"
    _render(shared_worker, prepared["plan"], prepared["asm"], a2)
    assert _dig(a1) == _dig(a2)


def test_sequence_fail_then_success(prepared, shared_worker, tmp_path):
    """A forced QA failure must not poison the next good render."""
    thr = tmp_path / "thr.json"
    thr.write_text(json.dumps({"text_height_mm_min": 99.0}), encoding="utf-8")
    bad = tmp_path / "bad"
    res_bad = shared_worker.run([RP, str(prepared["plan"]), "--assembly", str(prepared["asm"]),
                                 "-o", str(bad), "--thresholds", str(thr)])
    assert res_bad.returncode == 1
    assert not list(bad.glob("*.dxf"))             # quarantined, not published
    good = tmp_path / "good"
    res_good = _render(shared_worker, prepared["plan"], prepared["asm"], good)
    assert res_good.returncode == 0
    ref = prepared["work"] / "ref"
    tg.render_in_subprocess(prepared["plan"], prepared["asm"], ref)
    assert _dig(good) == _dig(ref)


def test_unhandled_script_falls_back_to_subprocess(prepared, shared_worker):
    """A script the worker does not dispatch is run as a subprocess, transparently."""
    doctor = str(_ROOT / "scripts" / "doctor.py")
    res = shared_worker.run([doctor, "--json"])
    assert res.returncode in (0, 1)                 # doctor exits 0/1; either proves it ran
    assert res.stdout.strip().startswith("{")       # --json output, i.e. it really executed


def test_timeout_kills_and_rebuilds(prepared):
    """A job past the time limit returns 124 and leaves the worker able to serve the next
    job (kill + rebuild), never hanging."""
    w = W.Worker(time_limit_s=0.001)
    try:
        slow = w.run([RP, str(prepared["plan"]), "--assembly", str(prepared["asm"]),
                      "-o", str(prepared["work"] / "slow")])
        assert slow.returncode == 124
        w.time_limit_s = 1800.0
        ok = _render(w, prepared["plan"], prepared["asm"], prepared["work"] / "recovered")
        assert ok.returncode == 0
    finally:
        w.close()


def test_disabled_worker_uses_subprocess(prepared, monkeypatch):
    """PFK_WORKER=0 disables the resident worker; run_cli_via_worker still works."""
    monkeypatch.setenv("PFK_WORKER", "0")
    assert W.worker_enabled() is False
    out = prepared["work"] / "disabled"
    res = W.run_cli_via_worker([RP, str(prepared["plan"]), "--assembly", str(prepared["asm"]),
                                "-o", str(out)])
    assert res.returncode == 0
    ref = prepared["work"] / "ref_disabled"
    tg.render_in_subprocess(prepared["plan"], prepared["asm"], ref)
    assert _dig(out) == _dig(ref)


def test_snapshot_inputs_hashes(prepared):
    snap = W.snapshot_inputs(prepared["plan"])
    assert snap["plan_hash"] and snap["terms_hash"] and snap["step_sha"]
    # deterministic
    assert W.snapshot_inputs(prepared["plan"]) == snap


def test_abort_if_running_kills_the_tagged_render(prepared):
    """R1 #6: a render tagged with a token can be aborted mid-flight from another thread; the
    in-flight run returns non-zero, and the worker stays usable afterwards."""
    import threading
    import time as _t
    w = W.Worker()
    try:
        box = {}

        def do():
            box["res"] = w.run([RP, str(prepared["plan"]), "--assembly", str(prepared["asm"]),
                                "-o", str(prepared["work"] / "abort")], token="job-abort")

        t = threading.Thread(target=do)
        t.start()
        # wait until the worker reports it is running our token, then abort
        for _ in range(200):
            with w._abort_lock:
                cur = w._current_token
            if cur == "job-abort":
                break
            _t.sleep(0.02)
        killed = w.abort_if_running("job-abort")
        t.join(timeout=30)
        assert not t.is_alive()
        # either we aborted it (killed → non-zero), or it finished before we could; both are
        # acceptable, but if we issued the kill the result must be non-zero.
        if killed:
            assert box["res"].returncode != 0
        # worker recovers for the next job
        ok = w.run([RP, str(prepared["plan"]), "--assembly", str(prepared["asm"]),
                    "-o", str(prepared["work"] / "after-abort")])
        assert ok.returncode == 0
    finally:
        w.close()
