"""三个公开演示模型（减速器 / 台虎钳 / 球阀）的元数据与示例计划（WP2）。

STEP 由 ``scripts/make_demo_assemblies.py`` 现场生成（OCP 只在那个子进程里导入，本模块是纯数据），
与任何真实产品无关。``POST /v1/projects/from-example`` 与验收 N17/N18 共用这里：同一份零件名、
同一份中文名、同一份已验证能过闸的分图。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

REPO = Path(__file__).resolve().parents[2]
GENERATOR = REPO / "scripts" / "make_demo_assemblies.py"

_LAYOUT = {"view": "iso", "explode_axis": "auto", "axis_angle": "auto", "density": "normal",
           "max_labels_per_figure": 20, "engineering_table": False}

_VISE_TERMS = [
    ("VISE-BASE", "钳座", "once"), ("VISE-FIXED-JAW", "固定钳身", "once"),
    ("VISE-MOVABLE-JAW", "活动钳身", "once"), ("VISE-JAW-PLATE", "钳口板", "once"),
    ("VISE-LEAD-SCREW", "丝杠", "once"), ("VISE-LEAD-NUT", "丝杠螺母", "once"),
    ("VISE-THRUST-COLLAR", "止推环", "once"), ("VISE-HANDLE-HUB", "手柄座", "once"),
    ("VISE-HANDLE-BAR", "手柄杆", "once"), ("VISE-HANDLE-KNOB", "手柄球", "once"),
    ("VISE-BASE-BOLT", "底座螺栓", "none"), ("VISE-PLATE-SCREW", "钳口板螺钉", "none"),
]
_VALVE_TERMS = [
    ("VALVE-BODY", "阀体", "once"), ("VALVE-BALL", "球芯", "once"),
    ("VALVE-SEAT", "阀座", "once"), ("VALVE-STEM", "阀杆", "once"),
    ("VALVE-PACKING-RING", "填料环", "once"), ("VALVE-GLAND-NUT", "压盖螺母", "once"),
    ("VALVE-HANDLE", "手柄", "once"), ("VALVE-HANDLE-NUT", "手柄螺母", "none"),
    ("VALVE-FLANGE-BOLT", "法兰螺栓", "none"),
]


def _plan(step: str, include: str, terms, figures) -> dict:
    return {"schema": "patent-figure-plan/1",
            "source": {"step": step, "include": [include], "exclude": []},
            "terms": [{"selector": s, "term": t, "label": lab} for s, t, lab in terms],
            "figures": figures, "layout": dict(_LAYOUT)}


EXAMPLES: Dict[str, Dict[str, Any]] = {
    "gearbox": {
        "title": "行星齿轮减速器", "step": "demo-planetary-gearbox.stp", "order": 10,
        "description": "电机子装配 + 行星齿轮系 + 轴承端盖，同轴堆叠（公开演示模型）",
        "plan_file": REPO / "examples" / "planetary-gearbox" / "figure-plan.json",
    },
    "vise": {
        "title": "台虎钳", "step": "demo-bench-vise.stp", "order": 11,
        "description": "固定钳身 / 活动钳身 / 丝杠手柄三个子装配（公开演示模型）",
        "plan": _plan("demo-bench-vise.stp", "VISE-*", _VISE_TERMS, [
            {"id": "fig1", "caption": "台虎钳丝杠手柄组件分解示意图", "kind": "exploded",
             "members": ["VISE-LEAD-SCREW", "VISE-THRUST-COLLAR", "VISE-HANDLE-HUB",
                         "VISE-HANDLE-BAR", "VISE-HANDLE-KNOB"]},
            {"id": "fig2", "caption": "台虎钳活动钳身组件分解示意图", "kind": "exploded",
             "members": ["VISE-MOVABLE-JAW", "VISE-JAW-PLATE", "VISE-PLATE-SCREW",
                         "VISE-LEAD-NUT"]},
        ]),
    },
    "valve": {
        "title": "球阀", "step": "demo-ball-valve.stp", "order": 12,
        "description": "法兰阀体、开孔球芯、阀座、阀杆填料与手柄（公开演示模型）",
        "plan": _plan("demo-ball-valve.stp", "VALVE-*", _VALVE_TERMS, [
            {"id": "fig1", "caption": "球阀整体分解示意图", "kind": "exploded",
             "members": ["VALVE-*"]},
            {"id": "fig2", "caption": "球阀阀杆组件分解示意图", "kind": "exploded",
             "members": ["VALVE-STEM", "VALVE-PACKING-RING", "VALVE-GLAND-NUT"]},
        ]),
    },
}


def plan_for(name: str) -> dict:
    meta = EXAMPLES[name]
    if "plan_file" in meta:
        return json.loads(Path(meta["plan_file"]).read_text(encoding="utf-8"))
    return copy.deepcopy(meta["plan"])


def ensure_steps(out_dir: Path, run: Optional[Callable] = None) -> Dict[str, Path]:
    """Generate the three demo STEPs into ``out_dir`` once (all three in one generator run).
    ``run(argv)`` → CompletedProcess (defaults to a plain subprocess of this interpreter)."""
    out_dir = Path(out_dir)
    paths = {k: out_dir / v["step"] for k, v in EXAMPLES.items()}
    if all(p.is_file() and p.stat().st_size > 0 for p in paths.values()):
        return paths
    out_dir.mkdir(parents=True, exist_ok=True)
    if run is None:
        import subprocess
        proc = subprocess.run([sys.executable or "python3", str(GENERATOR), "-o", str(out_dir)],
                              capture_output=True, text=True, timeout=600)
    else:
        proc = run([str(GENERATOR), "-o", str(out_dir)], timeout=600)
    missing = [k for k, p in paths.items() if not p.is_file()]
    if proc.returncode != 0 or missing:
        raise RuntimeError("生成演示 STEP 失败（缺 %s）：%s" % (
            missing, ((proc.stderr or "") + (proc.stdout or ""))[-600:]))
    return paths
