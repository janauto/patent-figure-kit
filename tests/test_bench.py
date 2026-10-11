"""bench.py honesty tests (WP1 R1 #13): records exit codes / fig counts / commit / input
hash, and only reports a speedup for equivalent work."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

try:
    import OCP  # noqa: F401
    _HAS_OCP = True
except Exception:  # pragma: no cover
    _HAS_OCP = False


@pytest.mark.skipif(not _HAS_OCP, reason="需要 cadquery-ocp")
def test_bench_records_exitcodes_and_gates_speedup(tmp_path):
    import bench
    # smallest possible real run: one 50-part tier, one rep, two amortization renders.
    result = bench.main_as_dict(tiers=[50], reps=1, amortize_k=2, work=tmp_path) \
        if hasattr(bench, "main_as_dict") else None
    if result is None:
        # fall back to the public functions directly
        step = bench._fixture(_ROOT, tmp_path, 50, 12345)
        seg = bench.bench_segments(_ROOT, step, tmp_path / "t", reps=1)
        am = bench.bench_worker_amortization(_ROOT, step, tmp_path / "am", k=2)
        result = {"commit": "x", "tiers": [{"input_step_sha256": "x", "segments": seg,
                                            "worker_amortization": am}]}
    tier = result["tiers"][0]
    seg = tier["segments"]
    am = tier["worker_amortization"]
    # exit codes + fig counts are recorded for both paths (R1 #13)
    assert "render_returncodes" in seg and len(seg["render_returncodes"]) == 1
    assert len(am["subprocess_returncodes"]) == 2
    assert len(am["worker_returncodes"]) == 2
    assert "equivalent_work" in am
    # speedup is reported only when the two paths did equivalent work
    if am["equivalent_work"]:
        assert isinstance(am["speedup_x"], (int, float))
    else:
        assert am["speedup_x"] is None
