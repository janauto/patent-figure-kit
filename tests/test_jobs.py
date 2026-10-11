"""Job-layer tests (WP1 deliverable 4). Pure stdlib — no server needed."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from patent_figure import jobs as J  # noqa: E402


def test_happy_path_state_and_events(tmp_path):
    store = J.JobStore(tmp_path)
    job, created = store.create("render", "proj1", inputs={"plan_hash": "abc"})
    assert created and job.state == J.QUEUED

    def fn(j):
        j.emit(J.EV_STAGE, "解析", stage="analyze", progress=0.3)
        j.emit(J.EV_LOG, "渲染 fig1")
        return {"figures": 2}

    store.run(job, fn)
    assert job.state == J.SUCCEEDED
    assert job.result == {"figures": 2}
    types = [e["type"] for e in job.events]
    assert types[0] == J.EV_STAGE and types[-1] == J.EV_DONE
    assert job.to_dict()["logs"] == ["渲染 fig1"]


def test_failure_records_error(tmp_path):
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")

    def boom(j):
        raise RuntimeError("渲染炸了")

    store.run(job, boom)
    assert job.state == J.FAILED
    assert job.error["message"] == "渲染炸了"
    assert job.events[-1]["type"] == J.EV_ERROR


def test_cancel_queued_is_immediate(tmp_path):
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    store.cancel(job.id)
    assert job.state == J.CANCELLED


def test_cancel_during_run(tmp_path):
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")

    def fn(j):
        j.request_cancel()           # simulate a cancel arriving mid-flight
        j.raise_if_cancelled()
        return {"never": True}

    store.run(job, fn)
    assert job.state == J.CANCELLED
    assert job.result is None


def test_idempotency_key_returns_same_job(tmp_path):
    store = J.JobStore(tmp_path)
    a, created_a = store.create("render", "p", idempotency_key="k1")
    b, created_b = store.create("render", "p", idempotency_key="k1")
    assert created_a and not created_b
    assert a.id == b.id
    c, created_c = store.create("render", "p", idempotency_key="k2")
    assert created_c and c.id != a.id


def test_events_since_and_last_event_id(tmp_path):
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    e1 = job.emit(J.EV_LOG, "one")
    e2 = job.emit(J.EV_LOG, "two")
    assert [e["id"] for e in job.events_since(0)] == [e1["id"], e2["id"]]
    # Replay from Last-Event-ID = e1: only e2 and later.
    assert [e["data"] for e in job.events_since(e1["id"])] == ["two"]


def test_wait_for_event_blocks_then_wakes(tmp_path):
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    import threading
    got = {}

    def waiter():
        got["events"] = job.wait_for_event_after(0, timeout=2.0)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)
    job.emit(J.EV_LOG, "late")
    t.join(timeout=3)
    assert got["events"] and got["events"][0]["data"] == "late"


def test_wait_for_event_times_out(tmp_path):
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    t0 = time.monotonic()
    out = job.wait_for_event_after(0, timeout=0.2)
    assert out == [] and (time.monotonic() - t0) >= 0.15


def test_durable_recovery_marks_interrupted(tmp_path):
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p", inputs={"plan_hash": "h"})
    job._set_state(J.RUNNING)
    job._persist()
    # A fresh store over the same dir simulates a restart.
    store2 = J.JobStore(tmp_path)
    recovered = store2.get(job.id)
    assert recovered is not None
    assert recovered.state == J.INTERRUPTED
    assert recovered.error["code"] == "E_INTERRUPTED"


def test_terminal_job_not_recovered_as_interrupted(tmp_path):
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    store.run(job, lambda j: {"ok": True})
    assert job.state == J.SUCCEEDED
    store2 = J.JobStore(tmp_path)
    assert store2.get(job.id).state == J.SUCCEEDED


def test_save_is_atomic_and_leaves_no_temp(tmp_path):
    """R1 #9: durable write is unique-temp + fsync + os.replace; no .tmp survives."""
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    for i in range(5):
        job.emit(J.EV_LOG, "line %d" % i)
    assert not list(tmp_path.glob("*.tmp")), "no temp file may survive a successful save"
    # the on-disk record is complete JSON with the full event log
    import json
    data = json.loads((tmp_path / (job.id + ".json")).read_text(encoding="utf-8"))
    assert len(data["events"]) == 5 and data["next_event"] == 6


def test_corrupt_record_recovers_as_queryable_error(tmp_path):
    """R1 #9: a torn/corrupt record must come back as a failed, queryable job — not a 404."""
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    (tmp_path / (job.id + ".json")).write_text("{ this is not valid json", encoding="utf-8")
    store2 = J.JobStore(tmp_path)
    rec = store2.get(job.id)
    assert rec is not None, "corrupt record must still be queryable"
    assert rec.state == J.FAILED
    assert rec.error["code"] == "E_JOB_RECORD_CORRUPT"


def test_interrupted_job_keeps_event_sequence_for_replay(tmp_path):
    """R1 #10: a crashed job recovers with its events + a terminal interrupted event, so a
    client reconnecting with its old Last-Event-ID still receives a terminal frame."""
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    job.emit(J.EV_STAGE, "running", stage=J.RUNNING, progress=0.2)
    e2 = job.emit(J.EV_LOG, "halfway")
    job._set_state(J.RUNNING)
    job._persist()
    last_seen = e2["id"]
    store2 = J.JobStore(tmp_path)
    rec = store2.get(job.id)
    assert rec.state == J.INTERRUPTED
    # events survived AND a new terminal event beyond last_seen exists
    fresh = rec.events_since(last_seen)
    assert fresh, "a terminal event beyond the client's Last-Event-ID must exist"
    assert fresh[-1]["type"] == J.EV_ERROR and fresh[-1]["stage"] == J.INTERRUPTED
    assert fresh[-1]["id"] > last_seen


def test_committed_job_cannot_become_cancelled(tmp_path):
    """R1c #2: once a job is marked committed (inside the commit lock), a cancel racing the
    success transition must lose — the job stays succeeded."""
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")

    def fn(j):
        j.mark_committed()          # commit point reached
        j.request_cancel()          # a cancel arrives in the post-commit window
        return {"ok": True}         # fn returns normally

    store.run(job, fn)
    assert job.state == J.SUCCEEDED, "committed job must not be cancelled by a late cancel"


def test_idempotency_is_project_scoped_and_conflicts_on_different_input(tmp_path):
    """R1c #8: the key is scoped to (project, kind, key) so it can't cross projects, and a
    reuse with a different fingerprint is a conflict."""
    store = J.JobStore(tmp_path)
    a, ca = store.create("render", "projA", idempotency_key="k", fingerprint="e1")
    # same key, different project → a fresh job (no cross-project sharing)
    b, cb = store.create("render", "projB", idempotency_key="k", fingerprint="e1")
    assert ca and cb and a.id != b.id and b.project_id == "projB"
    # same key+project, same fingerprint → reuse
    a2, c2 = store.create("render", "projA", idempotency_key="k", fingerprint="e1")
    assert not c2 and a2.id == a.id
    # same key+project, different fingerprint → conflict
    with pytest.raises(J.JobConflict):
        store.create("render", "projA", idempotency_key="k", fingerprint="e2")


def test_recovery_marks_committed_running_job_succeeded(tmp_path):
    """R1c #5: a job persisted committed but still 'running' (crash between commit and the
    success transition) recovers as succeeded, not interrupted."""
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    job.mark_committed()
    job.result = {"ok": True}
    job._set_state(J.RUNNING)
    job._persist()
    rec = J.JobStore(tmp_path).get(job.id)
    assert rec.state == J.SUCCEEDED and rec.committed


def test_concurrent_saves_keep_terminal_state(tmp_path):
    """R1b #10: snapshotting inside the save lock totally orders writes, so a late older
    snapshot can never roll a terminal job back to running on disk."""
    import threading
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")

    def spam_emit():
        for _ in range(60):
            job.emit(J.EV_LOG, "tick")

    def finish():
        job._set_state(J.RUNNING)
        for _ in range(30):
            job.emit(J.EV_PROGRESS, None, progress=0.5)
        job.result = {"ok": True}
        job._set_state(J.SUCCEEDED)
        job.emit(J.EV_DONE, job.result, stage=J.SUCCEEDED, progress=1.0)

    t1 = threading.Thread(target=spam_emit)
    t2 = threading.Thread(target=finish)
    t1.start(); t2.start(); t1.join(); t2.join()
    # final persisted state must be the terminal one, with monotonic event ids preserved
    store2 = J.JobStore(tmp_path)
    rec = store2.get(job.id)
    assert rec.state == J.SUCCEEDED
    ids = [e["id"] for e in rec.events]
    assert ids == sorted(ids) and len(ids) == len(set(ids))


def test_sse_frame_format(tmp_path):
    store = J.JobStore(tmp_path)
    job, _ = store.create("render", "p")
    ev = job.emit(J.EV_STAGE, {"msg": "hi"}, stage="running", progress=0.5)
    frame = J.sse_frame(ev)
    assert frame.startswith("id: %d\n" % ev["id"])
    assert "event: stage\n" in frame
    assert frame.endswith("\n\n")
    assert '"progress": 0.5' in frame
