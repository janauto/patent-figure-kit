"""Build `assembly.json` (impl-contract §4.1) from a STEP assembly.

This module is the only consumer of `occ_backend.load_assembly` in the analysis path.
Everything after the load is pure numpy/stdlib arithmetic on plain dicts, so the whole
analysis can be unit tested without CAD libraries: `build_assembly(..., loader=...)`
accepts any callable returning objects with the `PartShape` surface (§5.1)
(`name`, `path`, `key`, `instance_index`, `lo`, `hi`, `center`, `degenerate`).

Determinism (§7): no randomness, no set/dict iteration over unsorted input, every sort
key is a total order ending in a globally unique field, every float comparison is made on
values rounded to `RANK_DEC` decimals, and every glob goes through `fnmatch.fnmatchcase`.
"""

from __future__ import annotations

import hashlib
import json
import math
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

# --------------------------------------------------------------------------- constants
# §7 rule 7: every output-affecting constant is named, defined once, and traceable.

SCHEMA_ID = "patent-assembly/1"
SCHEMA_FILE = "assembly.schema.json"

#: §4.2 `layout.max_labels_per_figure` default — the cap `split_suggestions` works against.
DEFAULT_MAX_LABELS = 20

#: §4.1: `bbox_size` is rounded to 3 decimals. `max_dim`, `bbox` and every other
#: model-space length in the document use the same precision (micron on a mm model).
SIZE_DEC = 3
#: §5.1 fixes the `load_assembly` sort key at 6 decimals; instance centres are reported at
#: the same precision so two implementations cannot disagree about identity.
CENTER_DEC = 6
#: Unit vectors are dimensionless; 6 decimals is well below any drawing-visible difference.
VECTOR_DEC = 6
#: Dimensionless ratios reported for humans (`spread_ratio`).
RATIO_DEC = 3
#: §7 rule 4: round before comparing.
RANK_DEC = 9

#: §5.1 `PartShape.degenerate`: max extent < 1e-6 (model units, declared mm by `UNITS`).
DEGENERATE_MAX_DIM = 1e-6

#: STEP/XCAF geometry arrives in millimetres; §4.1 declares the field.
UNITS = "mm"

#: Snap the PCA direction to a coordinate axis when it is within this angle of one.
#: 15 deg is a quarter of the 60 deg that separates "nearest to x" from "nearest to y" for
#: an arbitrary direction, so a snapped axis is unambiguous by a factor of four.
AXIS_SNAP_TOL_DEG = 15.0
AXIS_NAMES = ("x", "y", "z")

#: `spread_ratio` = lambda_max / lambda_2. A second eigenvalue below this fraction of the
#: first means the centres are collinear to better than 1e-3 in length terms (eigenvalues
#: are squared lengths), which is finer than any drawing can show; the ratio is capped
#: there instead of dividing by ~0.
EIG_REL_FLOOR = 1e-6
SPREAD_RATIO_MAX = 1.0 / EIG_REL_FLOOR

#: Revolution / coaxiality tolerance = absolute + relative (both terms are required):
#: the absolute term is the degeneracy floor, so parts smaller than that never
#: accidentally compare "equal by relative tolerance"; the relative term is 2 % of the
#: part's own largest extent — two cross-axis extents differing by less than 2 % are not
#: distinguishable in a patent line drawing, and the relative form keeps the test
#: independent of whether the model was exported in mm or in metres.
COAX_ABS_TOL = DEGENERATE_MAX_DIM
COAX_REL_TOL = 0.02
#: A coaxial "group" needs at least two instances; a single part is trivially coaxial with
#: itself and reporting it would flood the document with singletons.
COAX_MIN_INSTANCES = 2

#: `stack` split strategy: a spacing at least this multiple of the median spacing between
#: consecutive parts along the principal axis is a real break between sub-assemblies
#: rather than stack jitter. 2.0 = "twice the typical spacing".
STACK_BREAK_K = 2.0

#: §4.1 `size_tiers` — three tiers, largest first.
TIER_NAMES = ("large", "medium", "small")
TIER_COUNT = 3

#: Warnings name at most this many parts before switching to "等 N 个".
WARN_NAME_LIMIT = 10

STRATEGIES = ("coaxial", "stack", "size")


class AnalyzeError(RuntimeError):
    """Raised for input the analysis cannot proceed on. Message is user-facing Chinese."""


# --------------------------------------------------------------------------- helpers


def _q(v: float, dec: int = RANK_DEC) -> float:
    """Quantise before any comparison (§7 rule 4)."""
    return round(float(v), dec)


def _lower_median(vals: Sequence[float]) -> float:
    """Lower median: sort and take index (n-1)//2. No two-value averaging, no np.median
    (its introselect path varies with the numpy build)."""
    s = sorted(float(v) for v in vals)
    if not s:
        raise AnalyzeError("内部错误：对空序列求中位数")
    return s[(len(s) - 1) // 2]


def _sum(vals: Iterable[float]) -> float:
    """Fixed-order Python summation over already-rounded floats (§11.9 rule 5)."""
    total = 0.0
    for v in vals:
        total += float(v)
    return total


def _num(v: float, dec: int) -> float:
    """Round for output. `+ 0.0` collapses IEEE negative zero, which would otherwise put
    a `-0.0` in the JSON for any coordinate that rounds to zero from below."""
    return round(float(v), dec) + 0.0


def _round_list(vals: Iterable[float], dec: int) -> list:
    return [_num(v, dec) for v in vals]


def _name_list(names: Sequence[str]) -> str:
    if len(names) <= WARN_NAME_LIMIT:
        return "、".join(names)
    return "、".join(names[:WARN_NAME_LIMIT]) + " 等 %d 个" % (len(names),)


def sha256_of(path: Path) -> str:
    """SHA-256 of the file's bytes."""
    h = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def matches_any(text: str, patterns: Sequence[str]) -> bool:
    """`fnmatchcase` only (§7 rule 6): `fnmatch` lowercases on Windows."""
    for pat in patterns:
        if fnmatchcase(text, pat):
            return True
    return False


# --------------------------------------------------------------------------- records


def instance_records(parts: Sequence[Any]) -> list:
    """Convert `PartShape`-like objects into plain dicts. This is the only place that
    touches the backend object surface; everything downstream is pure data."""
    out = []
    for p in parts:
        lo = np.asarray(p.lo, dtype=np.float64).reshape(3)
        hi = np.asarray(p.hi, dtype=np.float64).reshape(3)
        if not (np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
            raise AnalyzeError("零件 %s 的包围盒含 NaN/Inf，STEP 载入结果不可用" % (p.key,))
        size = hi - lo
        raw_max = float(max(size[0], size[1], size[2]))
        center = (lo + hi) / 2.0
        out.append({
            "key": str(p.key),
            "name": str(p.name),
            "path": str(p.path),
            "instance_index": int(p.instance_index),
            "lo": lo,
            "hi": hi,
            "size": size,
            "raw_max": raw_max,
            "center": center,
            "degenerate": raw_max < DEGENERATE_MAX_DIM,
        })
    # `key` is globally unique (§5.1), so this is a total order.
    out.sort(key=lambda r: r["key"])
    return out


def filter_records(records: Sequence[dict], include: Sequence[str],
                   exclude: Sequence[str]) -> list:
    """Apply `source.include` / `source.exclude` globs against the part name and its
    assembly path (a pattern containing "/" is naturally a path pattern)."""
    kept = []
    for r in records:
        if include and not (matches_any(r["name"], include) or matches_any(r["path"], include)):
            continue
        if exclude and (matches_any(r["name"], exclude) or matches_any(r["path"], exclude)):
            continue
        kept.append(r)
    return kept


def part_entries(records: Sequence[dict]) -> list:
    """Group instances by part name into the §4.1 `parts` entries.

    `bbox_size` / `max_dim` are taken from the instance whose `key` sorts first, which is
    stable because `key` is assigned after the §5.1 global sort. A part is reported
    degenerate when that representative instance is degenerate.
    """
    names = sorted({r["name"] for r in records})   # never iterate a set (§7 rule 2)
    by_name = {}
    for r in records:
        by_name.setdefault(r["name"], []).append(r)
    entries = []
    for nm in names:
        inst = sorted(by_name[nm], key=lambda r: r["key"])
        rep = inst[0]
        centers = sorted(tuple(_round_list(r["center"], CENTER_DEC)) for r in inst)
        entries.append({
            "name": nm,
            "instances": len(inst),
            "bbox_size": _round_list(rep["size"], SIZE_DEC),
            "max_dim": _num(rep["raw_max"], SIZE_DEC),
            "centers": [list(c) for c in centers],
            "degenerate": bool(rep["degenerate"]),
            "path_sample": rep["path"],
        })
    return entries


# --------------------------------------------------------------------------- principal axis


def principal_axis(records: Sequence[dict]) -> tuple:
    """PCA over every instance centre.

    Returns `(axis_dict, warnings)` where `axis_dict` is the §4.1
    `{"vector", "nearest", "spread_ratio"}` shape. The direction is snapped to the nearest
    coordinate axis when the deviation is <= `AXIS_SNAP_TOL_DEG`; beyond that the raw
    eigenvector is kept and the deviation is reported in warnings.
    """
    warnings = []
    n = len(records)
    fallback = {"vector": [0.0, 0.0, 1.0], "nearest": "z", "spread_ratio": 1.0}
    if n < 2:
        warnings.append("零件实例数为 %d，无法做主轴 PCA，主轴回退为 z 轴。" % (n,))
        return fallback, warnings

    cx = [round(float(r["center"][0]), CENTER_DEC) for r in records]
    cy = [round(float(r["center"][1]), CENTER_DEC) for r in records]
    cz = [round(float(r["center"][2]), CENTER_DEC) for r in records]
    mx, my, mz = _sum(cx) / n, _sum(cy) / n, _sum(cz) / n
    dx = [v - mx for v in cx]
    dy = [v - my for v in cy]
    dz = [v - mz for v in cz]

    # Covariance accumulated with fixed-order Python sums (no BLAS reduction order).
    cov = np.zeros((3, 3), dtype=np.float64)
    comps = (dx, dy, dz)
    for i in range(3):
        for j in range(i, 3):
            v = _sum(comps[i][k] * comps[j][k] for k in range(n)) / n
            cov[i, j] = v
            cov[j, i] = v

    trace = float(cov[0, 0] + cov[1, 1] + cov[2, 2])
    if _q(trace) <= 0.0:
        warnings.append("全部零件中心重合，主轴 PCA 退化，主轴回退为 z 轴。")
        return fallback, warnings

    # Normalise by the trace and quantise, so the eigensolver sees identical input on
    # every machine; only the last bits of the eigenvector remain solver dependent and
    # those are absorbed by rounding the reported vector to VECTOR_DEC.
    cov_n = np.round(cov / trace, RANK_DEC)
    eigvals, eigvecs = np.linalg.eigh(cov_n)     # ascending, symmetric input
    lam1 = float(eigvals[2])
    lam2 = float(eigvals[1])
    vec = np.asarray(eigvecs[:, 2], dtype=np.float64).reshape(3)

    if _q(lam1) <= 0.0:
        warnings.append("主轴 PCA 的最大特征值为 0，主轴回退为 z 轴。")
        return fallback, warnings

    # Sign convention: the dominant component is positive. argmax on rounded magnitudes
    # returns the smallest index on ties, which is the tie-break.
    mags = [abs(_q(float(v), VECTOR_DEC)) for v in vec]
    dom = int(np.argmax(np.asarray(mags)))
    if _q(float(vec[dom]), VECTOR_DEC) < 0.0:
        vec = -vec
    norm = float(math.sqrt(_sum(float(v) * float(v) for v in vec)))
    if _q(norm) <= 0.0:
        warnings.append("主轴特征向量退化为零向量，主轴回退为 z 轴。")
        return fallback, warnings
    vec = vec / norm

    floor = lam1 * EIG_REL_FLOOR
    if _q(lam2) <= _q(floor):
        spread = SPREAD_RATIO_MAX
    else:
        spread = min(lam1 / lam2, SPREAD_RATIO_MAX)

    # Nearest coordinate axis and its deviation.
    dots = [abs(_q(float(vec[k]), VECTOR_DEC)) for k in range(3)]
    nearest = int(np.argmax(np.asarray(dots)))
    dev_deg = math.degrees(math.acos(min(1.0, max(0.0, dots[nearest]))))
    if _q(dev_deg) <= _q(AXIS_SNAP_TOL_DEG):
        out_vec = [0.0, 0.0, 0.0]
        out_vec[nearest] = 1.0
    else:
        out_vec = _round_list(vec, VECTOR_DEC)
        warnings.append(
            "主轴方向偏离最近坐标轴 %s 达 %.2f 度（超过 %.1f 度阈值），"
            "保留 PCA 原向量未做吸附。" % (AXIS_NAMES[nearest], dev_deg, AXIS_SNAP_TOL_DEG))

    return ({"vector": _round_list(out_vec, VECTOR_DEC),
             "nearest": AXIS_NAMES[nearest],
             "spread_ratio": _num(spread, RATIO_DEC)}, warnings)


# --------------------------------------------------------------------------- coaxial groups


def _revolve_axis(size: np.ndarray, raw_max: float):
    """Return the index of the axis of revolution, or None.

    A part is treated as a body of revolution when exactly one pair of cross-axis extents
    is equal within `tol`; the remaining index is the axis. Isotropic parts (all three
    extents equal — spheres, cubes) have no distinguished axis and return None.
    """
    tol = COAX_ABS_TOL + COAX_REL_TOL * float(raw_max)
    e = [float(size[0]), float(size[1]), float(size[2])]
    eq = []
    for k in range(3):
        i, j = (k + 1) % 3, (k + 2) % 3
        if _q(abs(e[i] - e[j])) <= _q(tol):
            eq.append(k)
    if len(eq) == 1:
        return eq[0]
    return None


def coaxial_groups(records: Sequence[dict]) -> list:
    """Cluster bodies of revolution by axis direction and axis position.

    Clustering is leader-based over a fully sorted candidate list: each candidate joins
    the first existing group with the same axis whose leader's two perpendicular centre
    coordinates match within `tol` (absolute + relative, see `COAX_ABS_TOL`). Leader
    comparison (rather than a running mean) keeps the result independent of arrival order
    beyond the sort, which is a total order ending in `key`.
    """
    cands = []
    for r in records:
        if r["degenerate"]:
            continue
        k = _revolve_axis(r["size"], r["raw_max"])
        if k is None:
            continue
        i, j = (k + 1) % 3, (k + 2) % 3
        cands.append({
            "axis": k,
            "p": float(r["center"][i]), "q": float(r["center"][j]),
            "alo": float(r["lo"][k]), "ahi": float(r["hi"][k]),
            "ref": float(r["raw_max"]),
            "name": r["name"], "key": r["key"],
        })
    cands.sort(key=lambda c: (c["axis"], _q(c["p"]), _q(c["q"]), c["key"]))

    groups = []
    for c in cands:
        placed = False
        for g in groups:
            if g["axis"] != c["axis"]:
                continue
            tol = COAX_ABS_TOL + COAX_REL_TOL * max(g["ref"], c["ref"])
            if _q(abs(c["p"] - g["p"])) <= _q(tol) and _q(abs(c["q"] - g["q"])) <= _q(tol):
                g["members"].append(c)
                placed = True
                break
        if not placed:
            groups.append({"axis": c["axis"], "p": c["p"], "q": c["q"],
                           "ref": c["ref"], "members": [c]})

    out = []
    for g in groups:
        if len(g["members"]) < COAX_MIN_INSTANCES:
            continue
        k = g["axis"]
        i, j = (k + 1) % 3, (k + 2) % 3
        n = len(g["members"])
        pm = _sum(round(m["p"], CENTER_DEC) for m in g["members"]) / n
        qm = _sum(round(m["q"], CENTER_DEC) for m in g["members"]) / n
        origin = [0.0, 0.0, 0.0]
        origin[i] = _num(pm, CENTER_DEC)
        origin[j] = _num(qm, CENTER_DEC)
        axis_vec = [0.0, 0.0, 0.0]
        axis_vec[k] = 1.0
        out.append({
            "id": "",
            "axis": axis_vec,
            "origin": origin,
            "members": sorted({m["name"] for m in g["members"]}),
            "instances": n,
            "extent": [_num(min(m["alo"] for m in g["members"]), SIZE_DEC),
                       _num(max(m["ahi"] for m in g["members"]), SIZE_DEC)],
            "_sort": (k, _q(min(m["alo"] for m in g["members"])), g["members"][0]["key"]),
        })
    out.sort(key=lambda g: g["_sort"])
    for idx, g in enumerate(out):
        g["id"] = "g%d" % (idx + 1,)
        del g["_sort"]
    return out


# --------------------------------------------------------------------------- order & tiers


def part_centers(records: Sequence[dict], entries: Sequence[dict]) -> dict:
    """Representative centre per part name = mean of its instance centres (fixed-order
    summation over rounded coordinates)."""
    by_name = {}
    for r in records:
        by_name.setdefault(r["name"], []).append(r)
    out = {}
    for e in entries:
        inst = by_name[e["name"]]
        n = len(inst)
        out[e["name"]] = [
            _num(_sum(round(float(r["center"][k]), CENTER_DEC) for r in inst) / n, CENTER_DEC)
            for k in range(3)]
    return out


def stack_order(entries: Sequence[dict], axis_vector: Sequence[float],
                centers: dict) -> tuple:
    """Part names ordered by the projection of their representative centre onto the
    principal axis. Returns `(names, projection_by_name)`. Ties break on the name."""
    v = [float(x) for x in axis_vector]
    proj = {}
    for e in entries:
        c = centers[e["name"]]
        proj[e["name"]] = _num(c[0] * v[0] + c[1] * v[1] + c[2] * v[2], CENTER_DEC)
    names = sorted((e["name"] for e in entries), key=lambda nm: (_q(proj[nm]), nm))
    return names, proj


def size_tiers(entries: Sequence[dict]) -> list:
    """Split part names into three tiers by `max_dim` tertiles.

    Thresholds are the values at indices floor(n/3) and floor(2n/3) of the ascending
    `max_dim` list, and assignment is by value, so parts of equal size always land in the
    same tier (the counts are only approximately equal, which is the intended trade).
    All three tiers are always present, possibly with empty `members`.
    """
    vals = sorted(_q(e["max_dim"]) for e in entries)
    n = len(vals)
    buckets = {t: [] for t in TIER_NAMES}
    if n == 0:
        return [{"tier": t, "members": []} for t in TIER_NAMES]
    t_lo = vals[n // TIER_COUNT]
    t_hi = vals[(2 * n) // TIER_COUNT]
    for e in sorted(entries, key=lambda x: x["name"]):
        m = _q(e["max_dim"])
        if m >= t_hi:
            buckets["large"].append(e["name"])
        elif m >= t_lo:
            buckets["medium"].append(e["name"])
        else:
            buckets["small"].append(e["name"])
    return [{"tier": t, "members": sorted(buckets[t])} for t in TIER_NAMES]


# --------------------------------------------------------------------------- split suggestions


def _figure(caption_hint: str, members: Sequence[str]) -> dict:
    """One proposed figure. `labels` is the number of distinct parts in it — the label
    count of the strictest legal plan (`label: "once"` on every part). A plan that sets
    `label: "none"` on standard parts needs fewer, never more."""
    ms = sorted(members)
    return {"caption_hint": caption_hint, "members": ms, "labels": len(ms)}


def _coaxial_figures(groups: Sequence[dict], all_names: Sequence[str]) -> tuple:
    """One figure per coaxial group (a part name is assigned to its first group), plus one
    residual figure for everything ungrouped."""
    assigned = set()
    figures = []
    for i, g in enumerate(groups):
        members = [nm for nm in g["members"] if nm not in assigned]
        if not members:
            continue
        assigned.update(members)
        figures.append(_figure("同轴组 %d 分解示意图" % (i + 1,), members))
    rest = [nm for nm in all_names if nm not in assigned]
    if rest:
        figures.append(_figure("其余零件示意图", rest))
    if len(figures) < 2:
        return [], "未找到可用于拆分的同轴组（只能得到 1 张图）"
    return figures, ""


def _stack_figures(order: Sequence[str], proj: dict) -> tuple:
    """Cut `stack_order` at spacings >= STACK_BREAK_K x the median spacing."""
    n = len(order)
    if n < 2:
        return [], "零件数不足 2，无法按主轴间距拆分"
    gaps = [_q(proj[order[i + 1]] - proj[order[i]]) for i in range(n - 1)]
    med = _q(_lower_median(gaps))
    if med <= 0.0:
        return [], "主轴方向相邻零件间距的中位数为 0，无间距断点可用"
    thr = _q(STACK_BREAK_K * med)
    cuts = [i for i in range(n - 1) if gaps[i] >= thr]
    if not cuts:
        return [], "主轴方向未发现间距断点（无间距 >= %.1f 倍中位间距）" % (STACK_BREAK_K,)
    figures = []
    start = 0
    bounds = [c + 1 for c in cuts] + [n]
    for b in bounds:
        seg = list(order[start:b])
        start = b
        if seg:
            figures.append(_figure("沿主轴第 %d 段分解示意图" % (len(figures) + 1,), seg))
    if len(figures) < 2:
        return [], "按间距断点拆分后只剩 1 张图"
    return figures, ""


def _size_figures(tiers: Sequence[dict]) -> tuple:
    caption = {"large": "大尺寸零件示意图", "medium": "中等尺寸零件示意图",
               "small": "小尺寸零件示意图"}
    figures = [_figure(caption[t["tier"]], t["members"]) for t in tiers if t["members"]]
    if len(figures) < 2:
        return [], "尺寸分档后只剩 1 张非空图"
    return figures, ""


def split_suggestions(entries: Sequence[dict], groups: Sequence[dict],
                      order: Sequence[str], proj: dict, tiers: Sequence[dict],
                      max_labels: int) -> tuple:
    """Attempt all three strategies; emit only those whose every figure is within the cap.

    §4.1: a strategy that cannot reach the cap is reported in `warnings` with the count it
    got to and is never emitted. If none succeeds the result is `[]` plus a warning —
    a legitimate outcome, the human then chooses the grouping.
    """
    all_names = sorted(e["name"] for e in entries)
    warnings = []
    if len(all_names) <= max_labels:
        return [], warnings

    built = {
        "coaxial": _coaxial_figures(groups, all_names),
        "stack": _stack_figures(order, proj),
        "size": _size_figures(tiers),
    }
    out = []
    for strategy in STRATEGIES:                     # fixed order, never dict iteration
        figures, reason = built[strategy]
        if not figures:
            warnings.append("拆分策略 %s 不可用：%s。" % (strategy, reason))
            continue
        worst = max(f["labels"] for f in figures)
        if worst <= max_labels:
            out.append({"id": strategy, "strategy": strategy, "figures": figures})
        else:
            warnings.append(
                "拆分策略 %s 未达标记上限：拆成 %d 张图后最多的一张仍有 %d 个标记"
                "（上限 %d），不作为建议输出。" % (strategy, len(figures), worst, max_labels))
    if not out:
        warnings.append(
            "coaxial / stack / size 三种拆分策略均无法把每张图压到 %d 个标记以内，"
            "split_suggestions 为空，需要人工指定分组。" % (max_labels,))
    return out, warnings


# --------------------------------------------------------------------------- assembly doc


def _default_loader(step: Path) -> list:
    from .occ_backend import load_assembly     # the single OCC boundary (§0 rule 3)
    return load_assembly(step)


def build_assembly(step: Path, *, include: Sequence[str] = (),
                   exclude: Sequence[str] = (),
                   max_labels_per_figure: int = DEFAULT_MAX_LABELS,
                   loader: Callable[[Path], list] = None) -> dict:
    """Build the §4.1 `assembly.json` document for `step`.

    `loader` defaults to `occ_backend.load_assembly`; injecting one keeps the analysis
    testable without OCC.
    """
    step = Path(step)
    if not step.is_file():
        raise AnalyzeError("找不到 STEP 文件：%s" % (step,))
    if int(max_labels_per_figure) < 1:
        raise AnalyzeError("max_labels_per_figure 必须 >= 1，收到 %r" % (max_labels_per_figure,))

    parts = (loader or _default_loader)(step)
    if not parts:
        raise AnalyzeError("STEP 里没有读到任何零件：%s" % (step,))
    records_all = instance_records(parts)
    records = filter_records(records_all, list(include), list(exclude))
    if not records:
        raise AnalyzeError("include/exclude 过滤后没有剩下任何零件，请检查过滤 glob。")

    warnings = []
    dropped = len(records_all) - len(records)
    if dropped:
        warnings.append("include/exclude 过滤掉了 %d 个零件实例（共 %d 个）。"
                        % (dropped, len(records_all)))

    entries = part_entries(records)
    degen = [e["name"] for e in entries if e["degenerate"]]
    if degen:
        warnings.append("%d 个零件的包围盒退化（max_dim < %.0e）：%s。"
                        "它们不参与同轴聚类，建议在 plan 的 source.exclude 中排除。"
                        % (len(degen), DEGENERATE_MAX_DIM, _name_list(degen)))

    axis, axis_warnings = principal_axis(records)
    warnings.extend(axis_warnings)
    groups = coaxial_groups(records)
    centers = part_centers(records, entries)
    order, proj = stack_order(entries, axis["vector"], centers)
    tiers = size_tiers(entries)
    splits, split_warnings = split_suggestions(entries, groups, order, proj, tiers,
                                               int(max_labels_per_figure))
    warnings.extend(split_warnings)

    lo = [_num(min(float(r["lo"][k]) for r in records), SIZE_DEC) for k in range(3)]
    hi = [_num(max(float(r["hi"][k]) for r in records), SIZE_DEC) for k in range(3)]
    size = [_num(hi[k] - lo[k], SIZE_DEC) for k in range(3)]

    return {
        "schema": SCHEMA_ID,
        "source": {"step": str(step), "sha256": sha256_of(step),
                   "instances": len(records), "distinct": len(entries)},
        "units": UNITS,
        "bbox": {"lo": lo, "hi": hi, "size": size},
        "parts": entries,
        "principal_axis": axis,
        "coaxial_groups": groups,
        "stack_order": list(order),
        "size_tiers": tiers,
        "split_suggestions": splits,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- schema


def schema_path() -> Path:
    """`schemas/assembly.schema.json`, resolved relative to the repository root."""
    return Path(__file__).resolve().parents[2] / "schemas" / SCHEMA_FILE


def validate_assembly(doc: dict, schema: Path = None) -> list:
    """Validate `doc` against `assembly.schema.json`.

    Returns a list of human-readable Chinese error strings (empty when valid). A missing
    `jsonschema` package is reported as one such string rather than raising, so the CLI
    still produces its document.
    """
    path = Path(schema) if schema is not None else schema_path()
    if not path.is_file():
        return ["找不到 schema 文件：%s" % (path,)]
    try:
        import jsonschema
    except ImportError:
        return ["未安装 jsonschema，已跳过 assembly.json 的 schema 校验。"]
    with open(str(path), "r", encoding="utf-8") as fh:
        sch = json.load(fh)
    validator = jsonschema.Draft202012Validator(sch)
    errs = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    return ["assembly.json 不符合 schema：%s 处 %s"
            % ("/" + "/".join(str(p) for p in e.absolute_path), e.message) for e in errs]


def write_assembly(doc: dict, out: Path) -> None:
    """Write the document as UTF-8 JSON with a trailing newline (stable byte output)."""
    out = Path(out)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    with open(str(out), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
