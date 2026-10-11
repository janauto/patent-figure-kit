"""候选分图（F2-lite，WP2）：给一个刚解析完的装配体提出若干「可以直接接受」的分图候选。

口径（架构 v1 §2.2 F2-lite）：**候选 + 人工框选**，不是零点击。每个候选必须**真的渲染并过 QA
闸门**之后才有状态（``pass`` / ``fail``），没渲染的一律 ``pending``；建议为空时如实返回
``empty_reason`` 与「请框选分组」引导，不假装有结果。

候选来源（固定顺序，去重后最多 ``max_candidates`` 个）：

1. ``whole``     整机装配图（兜底）：允许、也常常会被闸门拦下——拦下的原因与建议就是教学；
2. ``axis``      沿主轴的整机分解图（``layout.explode_axis = "auto"`` 即主轴）；
3. ``split``     ``assembly.json:split_suggestions`` 里每个拆分方案的每张图；
4. ``subassembly`` 每个含 ≥2 种零件的子装配一张分解图（需要逐实例装配路径，见下）；
5. ``coaxial``   每个含 ≥2 种零件的同轴组一张分解图。

**成员契约**（吸收 Codex R3c #6）：``members`` 只放**真实零件**引用 ``{"part": <assembly 零件名>}``，
显示名单独放 ``members_display``；来源组只作出处记录（``source.id`` 形如 ``sub:<装配路径>`` /
``coax:g2`` / ``split:coaxial/1``），接受时也可以用 ``{"group": <source.id>}`` 引用，由
:func:`resolve_members` 在**当前** assembly 上解析，解析不了即拒绝（HTTP 422）。

**同名跨子装配**：figure-plan 的成员按零件**名称**选择。某个名称在多个子装配里都有实例时，
按名称选中的是全部实例——候选附 ``ambiguous_members`` 提示「按名称选中 N 处」。识别它需要
每个实例的装配路径：``analyze_assembly.py --instances`` 的旁路文件（``assembly.json`` 每个名称
只有一条 ``path_sample``，看不出来）；没有旁路文件时退回 ``path_sample`` 并在 ``warnings`` 说明。

**标准件默认不发标记**（``label: "none"``）：规则见 :class:`StandardPartRule`，可配置；接受候选时
写进 plan 的术语行，用户可在术语页改回。

本模块是纯数据编排：不 import OCP、不写坐标；渲染经 ``patent_figure.publish.render_candidate``
（同一渲染服务，产物进**候选专属版本库** ``<工程>/candidates/``，从不改当前版本指针）。
"""

from __future__ import annotations

import copy
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from patent_figure import plan as _plan

SCHEMA_ID = "patent-figure-candidates/1"

#: Default / hard cap on the number of candidates one suggestion renders (each is a real render).
MAX_CANDIDATES_DEFAULT = 8
MAX_CANDIDATES_HARD = 16

#: 任务书 F2-lite 规定的标准件名称正则（原样）。按 IGNORECASE 匹配零件名与中文名。
STANDARD_PART_PATTERN = r"SCREW|BOLT|NUT|WASHER|PIN|BEARING|螺钉|螺栓|螺母|垫圈|销|轴承"
#: 默认例外（写进契约 §14.2，可配置为空）：传动丝杠 / 丝杠螺母不是紧固件，是机构的主件。
STANDARD_PART_EXCEPT = r"(?:LEAD|BALL)[-_ ]?(?:SCREW|NUT)|丝杠|丝母"

#: Placeholder term for a still-unnamed part in a TRIAL render only (never written to the plan).
PLACEHOLDER_PREFIX = "待命名零件"

_CN_DIGITS = "零一二三四五六七八九"


def cn_number(n: int) -> str:
    """1 → 一, 12 → 十二, 25 → 二十五 (n in 1..99; larger falls back to digits-free repeats)."""
    n = int(n)
    if n <= 0:
        return _CN_DIGITS[0]
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return _CN_DIGITS[n // 10] + "十" + (_CN_DIGITS[n % 10] if n % 10 else "")
    return "".join(_CN_DIGITS[int(d)] for d in str(n))


def glob_escape(name: str) -> str:
    """A selector that matches exactly ``name`` under ``fnmatchcase`` (escape ``* ? [``)."""
    out = []
    for ch in str(name):
        out.append("[%s]" % ch if ch in "*?[" else ch)
    return "".join(out)


# --------------------------------------------------------------------------- standard parts


class StandardPartRule:
    """标准件识别规则：``pattern`` 命中且 ``except`` 不命中 → 标准件。

    * 匹配对象：零件名（STEP 名）与中文名（术语，非空时）；不区分大小写。
    * **英文按整词**：一个纯字母的命中片段两侧不能紧挨字母——否则 ``SPINDLE`` 会命中 ``PIN``、
      ``PINION``（小齿轮）会命中 ``PIN``。数字、连字符、下划线、中文都算词边界。中文按子串。
    """

    def __init__(self, pattern: Optional[str] = None, except_pattern: Optional[str] = None,
                 whole_word_latin: bool = True) -> None:
        self.pattern = STANDARD_PART_PATTERN if pattern is None else str(pattern)
        self.except_pattern = STANDARD_PART_EXCEPT if except_pattern is None \
            else str(except_pattern)
        self.whole_word_latin = bool(whole_word_latin)
        try:
            self._re = re.compile(self.pattern, re.IGNORECASE) if self.pattern else None
            self._ex = re.compile(self.except_pattern, re.IGNORECASE) \
                if self.except_pattern else None
        except re.error as exc:
            raise ValueError("标准件规则不是合法正则：%s" % exc)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "StandardPartRule":
        d = d or {}
        if not isinstance(d, dict):
            raise ValueError("standard_part_rule 必须是对象")
        return cls(d.get("pattern"), d.get("except"), d.get("whole_word_latin", True))

    def to_dict(self) -> Dict[str, Any]:
        return {"pattern": self.pattern, "except": self.except_pattern, "flags": "IGNORECASE",
                "whole_word_latin": self.whole_word_latin, "match_on": ["part_name", "term"]}

    def _hit(self, text: str) -> bool:
        if not text or self._re is None:
            return False
        for m in self._re.finditer(text):
            frag = m.group(0)
            if not frag:
                continue
            if self.whole_word_latin and re.fullmatch(r"[A-Za-z]+", frag):
                before = text[m.start() - 1] if m.start() > 0 else ""
                after = text[m.end()] if m.end() < len(text) else ""
                if (before and before.isascii() and before.isalpha()) or \
                        (after and after.isascii() and after.isalpha()):
                    continue
            return True
        return False

    def matches(self, name: str, term: str = "") -> bool:
        texts = [t for t in (str(name or ""), str(term or "")) if t]
        if self._ex is not None and any(self._ex.search(t) for t in texts):
            return False
        return any(self._hit(t) for t in texts)


# --------------------------------------------------------------------------- inputs


def load_instances(path: Optional[Path], assembly: dict) -> Optional[dict]:
    """The per-instance sidecar if it exists AND belongs to this assembly's STEP (sha256)."""
    if path is None:
        return None
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    want = ((assembly.get("source") or {}).get("sha256") or "")
    if not isinstance(doc, dict) or (doc.get("source") or {}).get("sha256") != want:
        return None
    return doc


def _parts(assembly: dict, plan: dict) -> List[dict]:
    """Non-degenerate assembly parts that survive ``plan.source`` include/exclude, by name."""
    selected = set(_plan.selected_part_names(plan, assembly))
    return [p for p in assembly.get("parts", [])
            if isinstance(p, dict) and p.get("name") in selected and not p.get("degenerate")]


def _paths_by_name(assembly: dict, instances: Optional[dict],
                   names: Sequence[str]) -> Tuple[Dict[str, List[str]], bool]:
    """name → every instance's assembly path. Exact when the sidecar is present; otherwise an
    approximation from ``path_sample`` (every instance assumed to share the sample path)."""
    keep = set(names)
    out: Dict[str, List[str]] = {n: [] for n in names}
    if instances is not None:
        for r in instances.get("instances", []):
            if r.get("degenerate") or r.get("name") not in keep:
                continue
            out[r["name"]].append(str(r.get("path") or ""))
        for n in out:
            out[n].sort()
        return out, True
    for p in assembly.get("parts", []):
        if p.get("name") in keep:
            out[p["name"]] = [str(p.get("path_sample") or "")] * int(p.get("instances") or 1)
    return out, False


def _term_rows(plan: dict) -> List[dict]:
    terms = plan.get("terms") if isinstance(plan, dict) else None
    return [t for t in (terms if isinstance(terms, list) else []) if isinstance(t, dict)]


def winning_row(plan: dict, name: str) -> Optional[dict]:
    """The term row that governs ``name`` (numbering precedence: first matching selector)."""
    for row in _term_rows(plan):
        sel = row.get("selector")
        if isinstance(sel, str) and sel and fnmatchcase(name, sel):
            return row
    return None


def term_of(plan: dict, name: str) -> str:
    row = winning_row(plan, name)
    return str((row or {}).get("term") or "").strip()


# --------------------------------------------------------------------------- groups


def subassemblies(paths: Dict[str, List[str]]) -> List[dict]:
    """Every intermediate assembly node (a path prefix strictly between the root and a leaf)
    that contains ≥ 2 distinct part names. Deterministic: sorted by path."""
    nodes: Dict[str, Dict[str, int]] = {}
    for name in sorted(paths):
        for path in paths[name]:
            comps = [c for c in path.split("/") if c]
            # comps[0] is the root assembly, comps[-1] the part itself.
            for depth in range(2, len(comps)):
                prefix = "/".join(comps[:depth])
                bucket = nodes.setdefault(prefix, {})
                bucket[name] = bucket.get(name, 0) + 1
    out = []
    for prefix in sorted(nodes):
        members = nodes[prefix]
        if len(members) < 2:
            continue
        out.append({"id": "sub:" + prefix, "path": prefix, "label": prefix.split("/")[-1],
                    "names": sorted(members), "inside": dict(sorted(members.items()))})
    return out


def coaxial_sets(assembly: dict, names: Sequence[str]) -> List[dict]:
    keep = set(names)
    out = []
    for g in assembly.get("coaxial_groups", []) or []:
        members = sorted(n for n in (g.get("members") or []) if n in keep)
        if len(members) < 2:
            continue
        out.append({"id": "coax:%s" % g.get("id"), "names": members,
                    "instances": int(g.get("instances") or 0)})
    return out


# --------------------------------------------------------------------------- candidates


def _caption_for(kind: str, names: Sequence[str], plan: dict, parts_by_name: Dict[str, dict],
                 ordinal: int, noun: str) -> str:
    """A Chinese caption from the largest named member: sub-assembly ``<名>组件分解示意图``,
    coaxial group ``<名>及同轴零件分解示意图``; unnamed → ``<noun><序号>分解示意图``."""
    ranked = sorted(names, key=lambda n: (-float(parts_by_name.get(n, {}).get("max_dim") or 0.0), n))
    for n in ranked:
        t = term_of(plan, n)
        if t:
            return ("%s及同轴零件分解示意图" if noun == "同轴组" else "%s组件分解示意图") % t
    return "%s%s分解示意图" % (noun, cn_number(ordinal))


def _digits_to_cn(text: str) -> str:
    return re.sub(r"\s*(\d+)\s*", lambda m: cn_number(int(m.group(1))), str(text or ""))


def _ambiguous(names: Sequence[str], paths: Dict[str, List[str]],
               inside: Optional[Dict[str, int]], group_instances: Optional[int],
               scope_label: str) -> List[dict]:
    """Members whose name selects instances outside the group (selection is by name)."""
    out = []
    if inside is not None:                                   # sub-assembly: exact counts
        for n in names:
            total = len(paths.get(n) or [])
            ins = int(inside.get(n, 0))
            if total > ins:
                outside = sorted({p for p in paths.get(n, []) if not p.startswith(scope_label + "/")})
                out.append({"part": n, "selected": total, "in_group": ins,
                            "outside_paths": outside[:6],
                            "message": "按名称选中 %d 处（其中 %d 处不在本组内）" % (total, total - ins)})
        return out
    if group_instances is not None:                          # coaxial: only the group total
        total = sum(len(paths.get(n) or []) for n in names)
        if total > group_instances:
            for n in names:
                k = len(paths.get(n) or [])
                if k > 1:
                    out.append({"part": n, "selected": k, "in_group": None,
                                "outside_paths": [],
                                "message": "按名称选中 %d 处（同轴组只含其中一部分）" % k})
    return out


def build_candidates(assembly: dict, plan: dict, instances: Optional[dict] = None, *,
                     label_standard_parts: bool = False,
                     rule: Optional[StandardPartRule] = None,
                     max_candidates: int = MAX_CANDIDATES_DEFAULT) -> dict:
    """Propose candidates (status ``pending``) with a trial plan each. Pure; deterministic."""
    rule = rule or StandardPartRule()
    max_candidates = max(1, min(int(max_candidates or MAX_CANDIDATES_DEFAULT),
                                MAX_CANDIDATES_HARD))
    plan = plan if isinstance(plan, dict) else {}
    parts = _parts(assembly, plan)
    parts_by_name = {p["name"]: p for p in parts}
    names = sorted(parts_by_name)
    warnings: List[str] = []
    paths, exact = _paths_by_name(assembly, instances, names)
    if not exact:
        warnings.append("缺少逐实例装配路径（assembly.instances.json），子装配按每个零件的 "
                        "path_sample 近似，无法识别同名跨子装配。")
    std = sorted(n for n in names if rule.matches(n, term_of(plan, n)))
    doc: Dict[str, Any] = {
        "schema": SCHEMA_ID,
        "assembly_sha256": (assembly.get("source") or {}).get("sha256", ""),
        "standard_part_rule": rule.to_dict(),
        "label_standard_parts": bool(label_standard_parts),
        "standard_parts": [] if label_standard_parts else std,
        "instances_exact": exact,
        "warnings": warnings,
        "candidates": [],
        "empty_reason": None,
        "guide": None,
    }
    if not names:
        doc["empty_reason"] = "装配体里没有可出图的零件（全部被过滤或退化）。"
        doc["guide"] = box_select_guide()
        return doc

    raw: List[dict] = []
    raw.append({"kind": "assembly", "names": names, "caption": "整体结构示意图",
                "source": {"kind": "whole", "id": "whole", "label": "整机装配图（兜底候选）"}})
    if len(names) >= 2:
        raw.append({"kind": "exploded", "names": names, "caption": "整体分解示意图",
                    "source": {"kind": "axis", "id": "axis",
                               "label": "沿主轴（%s）整机分解" % ((assembly.get("principal_axis")
                                                              or {}).get("nearest", "?"))}})
    for sug in assembly.get("split_suggestions", []) or []:
        for i, fig in enumerate(sug.get("figures", []) or []):
            members = sorted(n for n in (fig.get("members") or []) if n in parts_by_name)
            if len(members) < 1:
                continue
            raw.append({"kind": "exploded", "names": members,
                        "caption": _digits_to_cn(fig.get("caption_hint") or "拆分示意图"),
                        "source": {"kind": "split", "id": "split:%s/%d" % (sug.get("id"), i + 1),
                                   "label": "拆分建议 %s 第 %d 张" % (sug.get("strategy"), i + 1)}})
    subs = subassemblies(paths)
    for k, sub in enumerate(subs, start=1):
        raw.append({"kind": "exploded", "names": sub["names"],
                    "caption": _caption_for("exploded", sub["names"], plan, parts_by_name, k,
                                            "子装配"),
                    "source": {"kind": "subassembly", "id": sub["id"],
                               "label": "子装配 %s" % sub["label"]},
                    "_inside": sub["inside"], "_scope": sub["path"]})
    for k, g in enumerate(coaxial_sets(assembly, names), start=1):
        raw.append({"kind": "exploded", "names": g["names"],
                    "caption": _caption_for("exploded", g["names"], plan, parts_by_name, k,
                                            "同轴组"),
                    "source": {"kind": "coaxial", "id": g["id"], "label": "同轴组 %s" % g["id"][5:]},
                    "_group_instances": g["instances"]})

    seen = set()
    out = []
    for c in raw:
        key = (c["kind"], tuple(c["names"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    dropped = len(out) - max_candidates
    if dropped > 0:
        out = out[:max_candidates]
        warnings.append("候选共 %d 个，只试渲染前 %d 个（max_candidates）。"
                        % (len(out) + dropped, max_candidates))

    for idx, c in enumerate(out, start=1):
        cid = "c%d" % idx
        names_c = c["names"]
        amb = _ambiguous(names_c, paths, c.get("_inside"), c.get("_group_instances"),
                         c.get("_scope", ""))
        trial, placeholders = trial_plan(plan, assembly, names_c, c["kind"], c["caption"],
                                         doc["standard_parts"])
        issues = _plan.validate(trial, assembly)
        errors = [i for i in issues if i.severity == _plan.SEVERITY_ERROR]
        labels = 0
        for fid, count, _cap in _plan.effective_label_counts(trial, assembly):
            labels = count
        doc["candidates"].append({
            "id": cid, "fig_id": cid, "caption": c["caption"], "kind": c["kind"],
            "source": c["source"],
            "members": [{"part": n} for n in names_c],
            "members_display": [term_of(plan, n) or n for n in names_c],
            "labels": labels,
            "standard_parts": [n for n in names_c if n in set(doc["standard_parts"])],
            "placeholder_terms": placeholders,
            "ambiguous_members": amb,
            "status": "pending",
            "reason": "尚未试渲染" if not errors else "计划校验未通过",
            "detail": None,
            "suggestion": None,
            "plan_errors": [{"code": i.code, "message": i.message} for i in errors],
            "qa": None, "version": None, "preview": None,
            "_trial_plan": trial,
        })
    return doc


def trial_plan(plan: dict, assembly: dict, names: Sequence[str], kind: str, caption: str,
               standard_parts: Sequence[str]) -> Tuple[dict, int]:
    """The one-figure plan a candidate is trial-rendered with — the user's terms (precedence and
    numeral order kept), every member guaranteed a term row, blank terms filled with UNIQUE
    placeholders (trial only: numbers/QA do not depend on the term text), standard parts
    ``label: "none"``. Returns ``(plan, placeholder_count)``."""
    p = copy.deepcopy(plan if isinstance(plan, dict) else {})
    p.setdefault("schema", "patent-figure-plan/1")
    all_names = [str(x.get("name")) for x in assembly.get("parts", []) if isinstance(x, dict)]
    # The candidate is judged on ITS figure: term rows that hit no part at all (stale rows of
    # the user's plan) are left out of the trial, and a term TEXT the validator would reject
    # (part-code shaped) is swapped for a placeholder — neither affects geometry or numerals.
    terms = [dict(t) for t in _term_rows(p)
             if isinstance(t.get("selector"), str) and t.get("selector")
             and any(fnmatchcase(n, t["selector"]) for n in all_names)]
    for t in terms:
        text = str(t.get("term") or "")
        if any(rx.search(text) for rx in _plan._FORBIDDEN_RE):
            t["term"] = ""
    p["terms"] = terms
    for n in names:
        if winning_row(p, n) is None:
            terms.append({"selector": glob_escape(n), "term": "", "label": "once"})
    std = set(standard_parts)
    selected = _plan.selected_part_names(p, assembly)
    for n in sorted(std):
        row = winning_row(p, n)
        if row is None:
            continue
        covered = [m for m in selected if fnmatchcase(m, str(row.get("selector")))]
        if all(m in std for m in covered):
            row["label"] = "none"
        else:                                   # a broad row also covers real parts: pin n
            terms.insert(terms.index(row), {"selector": glob_escape(n),
                                            "term": str(row.get("term") or ""), "label": "none"})
    placeholders = 0
    for row in terms:
        if not str(row.get("term") or "").strip():
            placeholders += 1
            row["term"] = PLACEHOLDER_PREFIX + cn_number(placeholders)
    p["figures"] = [{"id": "fig1", "caption": caption, "kind": kind,
                     "members": [glob_escape(n) for n in names]}]
    return p, placeholders


def box_select_guide() -> dict:
    """「请框选分组」引导（前端照此渲染三步示意）。"""
    return {"action": "box_select", "title": "请框选分组",
            "steps": ["在 3D 视图里按 B 进入框选，框住要放进同一张图的零件",
                      "在右栏点「新建分图」，把框选的零件加入这张图",
                      "点「试出图」看闸门结果；没过闸就按提示减少零件或换视角"]}


# --------------------------------------------------------------------------- render + verdict


def _qa_failed(qa: Optional[dict]) -> List[dict]:
    rows = []
    for c in (qa or {}).get("checks", []) or []:
        if not c.get("pass"):
            rows.append({"id": c.get("id"), "title_zh": c.get("title_zh") or c.get("id"),
                         "value": c.get("value"), "threshold": c.get("threshold"),
                         "hint_plain": c.get("hint_plain") or c.get("hint") or "",
                         "fix_action": c.get("fix_action")})
    return rows


_ERROR_ADVICE = {
    "E_LAYOUT_UNSOLVABLE": "一张图放不下这些零件（字太小或图形太扁）：改用分组更小的候选，"
                           "或在 3D 里框选分组；标准件已默认不标号。",
    "E_LABELS_UNPLACEABLE": "标号太多、无处可放：改用分组更小的候选，或框选分组。",
    "E_TOO_MANY_LABELS": "标号超过每张图上限：拆成更小的分组。",
}


def plain_error(code: str, message: str) -> str:
    """One plain-language sentence for a renderer layout error (no JSON field names); the raw
    message is kept separately as ``detail``."""
    m = re.search(r"页面填充 ([0-9.]+) < ([0-9.]+)", message)
    if code == "E_LAYOUT_UNSOLVABLE" and m:
        return "整组图形太扁或太长，铺不满纸面（填充 %s，要求不低于 %s）" % (m.group(1), m.group(2))
    m = re.search(r"最大可用字高 ([0-9.]+) mm < 交付下限 ([0-9.]+) mm", message)
    if code == "E_LAYOUT_UNSOLVABLE" and m:
        return ("有零件在图上太小（或这张图零件太多），标号字高只能到 %s mm（要求不低于 %s mm）"
                % (m.group(1), m.group(2)))
    if code == "E_LABELS_UNPLACEABLE":
        return "标号太多，找不到互不交叉、不压线的位置"
    if code == "E_TOO_MANY_LABELS":
        return "标号数量超过每张图的上限"
    return "这组零件放不进一张合格的图"


def verdict(candidate: dict, report: Optional[dict]) -> None:
    """Fill status / reason / suggestion / qa from the renderer's own ``--json`` report."""
    report = report or {}
    figs = [f for f in report.get("figures", []) or [] if str(f.get("id")) == "fig1"]
    errs = [e for e in report.get("errors", []) or [] if str(e.get("figure")) == "fig1"]
    fig = figs[0] if figs else None
    if fig is not None and fig.get("gate_pass"):
        candidate["status"] = "pass"
        candidate["reason"] = "%d 项质量检查全部通过：字高 %s mm，%d 个标号" % (
            len((fig.get("qa") or {}).get("checks") or []), fig.get("text_height_mm"),
            int(fig.get("labels") or 0))
        candidate["suggestion"] = None
        candidate["qa"] = {"pass": True, "failed": [],
                           "text_height_mm": fig.get("text_height_mm"),
                           "labels": fig.get("labels")}
        return
    candidate["status"] = "fail"
    if fig is not None:
        failed = _qa_failed(fig.get("qa"))
        dropped = sum(int(x) for x in (fig.get("hlr_dropped") or [0, 0]))
        titles = "、".join(r["title_zh"] for r in failed) or ("消隐丢边 %d 条" % dropped)
        candidate["reason"] = "未过闸：%s" % titles
        candidate["suggestion"] = (failed[0]["hint_plain"] if failed else
                                   "换一个视角或把被遮挡的零件放进分解图。")
        candidate["qa"] = {"pass": False, "failed": failed,
                           "text_height_mm": fig.get("text_height_mm"),
                           "labels": fig.get("labels")}
        return
    if errs:
        e = errs[0]
        msg = str(e.get("message") or "")
        candidate["reason"] = "被闸门拦下：%s" % plain_error(str(e.get("code")), msg)
        candidate["detail"] = msg
        candidate["suggestion"] = _ERROR_ADVICE.get(str(e.get("code")),
                                                    "改用分组更小的候选，或在 3D 里框选分组。")
        candidate["qa"] = {"pass": False, "failed": [], "error_code": e.get("code")}
        return
    candidate["reason"] = "渲染没有产出这张图（见日志）"
    candidate["suggestion"] = "重试；仍失败请把日志反馈给维护者。"


def run_candidates(doc: dict, *, ws_root: Path, cache: Path, job_id: str, assembly_path: Path,
                   run_cli: Callable, svg_of: Callable,
                   emit: Optional[Callable[[str, float], None]] = None,
                   cancel_check: Optional[Callable[[], None]] = None) -> dict:
    """Trial-render every candidate SERIALLY through the publish service's candidate path
    (``publish.render_candidate`` → candidate version store, never the current pointer), then
    set each candidate's status. ``empty_reason`` + guide when none passes."""
    from patent_figure import publish as _pub
    emit = emit or (lambda *a, **k: None)
    cancel_check = cancel_check or (lambda: None)
    cands = doc.get("candidates", [])
    n = max(len(cands), 1)
    store = _pub.candidate_store(ws_root)
    rids = ["%s-%s" % (job_id, c["id"]) for c in cands]
    # WP2 R2 #5: lease EVERY candidate id of this job for the whole run (WP1 cross-process
    # leases), so a concurrent job's — or another process's — reclaim can never delete a
    # version this job is about to write, is writing, or has just sealed.
    for rid in rids:
        store.ref(rid)
    current = None
    try:
        for i, c in enumerate(cands):
            cancel_check()
            emit("试渲染候选 %d/%d：%s" % (i + 1, n, c["caption"]), 0.1 + 0.85 * i / n)
            if c.get("plan_errors"):
                c["status"] = "fail"
                c["reason"] = "计划校验未通过：%s" % plain_error(c["plan_errors"][0]["code"], "")
                c["detail"] = "；".join(e["message"] for e in c["plan_errors"][:3])
                c["suggestion"] = _ERROR_ADVICE.get(c["plan_errors"][0]["code"],
                                                    "改用分组更小的候选，或在 3D 里框选分组。")
                continue
            current = rids[i]
            out = _pub.render_candidate(ws_root, cache, current, c["_trial_plan"],
                                        assembly_path, run_cli, svg_of)
            current = None
            verdict(c, out.get("report"))
            c["version"] = rids[i]
            if c["status"] == "pass" and out.get("svg"):
                c["preview"] = {"svg": out["svg"]}
    except BaseException:
        # cancelled / crashed mid-render: the half-written version is marked cancelled so the
        # reclaim may take it once this job's leases are gone (an unmarked unsealed dir is
        # never reclaimed — it could be another job's work in progress).
        if current is not None and store.version_dir(current).is_dir() \
                and not store.is_complete(current):
            store.mark_cancelled(current)
        raise
    finally:
        for rid in rids:
            store.unref(rid)
    finalize(doc)
    return doc


def finalize(doc: dict) -> dict:
    """Set ``empty_reason``/``guide`` honestly from the statuses (no pass → empty)."""
    cands = doc.get("candidates", [])
    passed = [c for c in cands if c.get("status") == "pass"]
    pending = [c for c in cands if c.get("status") == "pending"]
    doc["summary"] = {"total": len(cands), "pass": len(passed),
                      "fail": sum(1 for c in cands if c.get("status") == "fail"),
                      "pending": len(pending)}
    if doc.get("empty_reason"):
        return doc
    if cands and not passed and not pending:
        doc["empty_reason"] = ("%d 个候选都没有通过质量闸门：自动分组不适合这个装配体，"
                               "请在 3D 里框选分组。" % len(cands))
        doc["guide"] = box_select_guide()
    return doc


def public(doc: dict) -> dict:
    """The response shape: drop the private ``_trial_plan`` (it is in each candidate's version
    ``inputs/plan.json``)."""
    out = dict(doc)
    out["candidates"] = [{k: v for k, v in c.items() if not k.startswith("_")}
                         for c in doc.get("candidates", [])]
    return out


# --------------------------------------------------------------------------- accept


def resolve_members(members: Sequence[Any], assembly: dict, plan: dict,
                    instances: Optional[dict] = None) -> Tuple[List[str], List[dict]]:
    """Resolve ``[{"part": name} | {"group": "sub:…|coax:g…|split:<id>/<n>"}]`` against the
    CURRENT assembly + plan selection. Returns ``(names, unresolved)``; any unresolved entry
    means the candidate must not be accepted (HTTP 422)."""
    all_parts = {p.get("name"): p for p in assembly.get("parts", []) if isinstance(p, dict)}
    selected = set(_plan.selected_part_names(plan, assembly))
    names: List[str] = []
    bad: List[dict] = []

    def take(n: str, ref: Any) -> None:
        p = all_parts.get(n)
        if p is None:
            bad.append({"member": ref, "reason": "零件 %s 不在当前装配体里" % n})
        elif p.get("degenerate"):
            bad.append({"member": ref, "reason": "零件 %s 是退化几何（包围盒为零），不能出图" % n})
        elif n not in selected:
            bad.append({"member": ref, "reason": "零件 %s 被计划的 source.include/exclude 排除" % n})
        elif n not in names:
            names.append(n)

    for m in members or []:
        if isinstance(m, dict) and isinstance(m.get("part"), str) and m["part"]:
            take(m["part"], m)
            continue
        gid = m.get("group") if isinstance(m, dict) else None
        if not isinstance(gid, str) or not gid:
            bad.append({"member": m, "reason": "成员必须是 {part: 零件名} 或 {group: 组 id}"})
            continue
        group_names: Optional[List[str]] = None
        if gid.startswith("sub:"):
            pool = sorted(n for n in all_parts if not all_parts[n].get("degenerate"))
            paths, _exact = _paths_by_name(assembly, instances, pool)
            hit = [s for s in subassemblies(paths) if s["id"] == gid]
            group_names = hit[0]["names"] if hit else None
        elif gid.startswith("coax:"):
            hit = [g for g in assembly.get("coaxial_groups", []) or []
                   if "coax:%s" % g.get("id") == gid]
            group_names = sorted(hit[0].get("members") or []) if hit else None
        elif gid.startswith("split:"):
            m_ = re.fullmatch(r"split:([^/]+)/(\d+)", gid)
            if m_:
                for sug in assembly.get("split_suggestions", []) or []:
                    figs = sug.get("figures") or []
                    k = int(m_.group(2))
                    if sug.get("id") == m_.group(1) and 1 <= k <= len(figs):
                        group_names = sorted(figs[k - 1].get("members") or [])
        if not group_names:
            bad.append({"member": m, "reason": "组 %s 在当前装配体里解析不到" % gid})
            continue
        for n in group_names:
            take(n, m)
    return sorted(names), bad


def next_figure_id(plan: dict) -> Callable[[], str]:
    used = {str(f.get("id")) for f in (plan.get("figures") or []) if isinstance(f, dict)}

    def gen() -> str:
        k = 1
        while "fig%d" % k in used:
            k += 1
        used.add("fig%d" % k)
        return "fig%d" % k
    return gen


def accept_into(plan: dict, assembly: dict, accepted: Sequence[dict], *,
                mode: str = "append", standard_parts: Sequence[str] = ()) -> Tuple[dict, dict]:
    """Write accepted candidates into ``plan`` (a copy is returned): one figure per candidate
    with EXACT (glob-escaped) member selectors, a term row for every member that has none
    (blank term — the user names it), and ``label: "none"`` for the candidate's standard parts
    when the governing row covers only standard parts. ``accepted`` = ``[{caption, kind,
    names}]`` already resolved by :func:`resolve_members`."""
    if mode not in ("append", "replace"):
        raise ValueError("mode 只能是 append / replace")
    p = copy.deepcopy(plan if isinstance(plan, dict) else {})
    p.setdefault("schema", "patent-figure-plan/1")
    terms = [t for t in _term_rows(p)]
    p["terms"] = terms
    figures = [] if mode == "replace" else [f for f in (p.get("figures") or [])
                                            if isinstance(f, dict)]
    p["figures"] = figures
    gen = next_figure_id(p)
    added = []
    for a in accepted:
        fid = gen()
        figures.append({"id": fid, "caption": a["caption"], "kind": a["kind"],
                        "members": [glob_escape(n) for n in a["names"]]})
        added.append(fid)
        for n in a["names"]:
            if winning_row(p, n) is None:
                terms.append({"selector": glob_escape(n), "term": "", "label": "once"})
    std = set(standard_parts)
    selected = _plan.selected_part_names(p, assembly)
    labelled_none, skipped = [], []
    for n in sorted(std):
        row = winning_row(p, n)
        if row is None:
            continue
        covered = [m for m in selected if fnmatchcase(m, str(row.get("selector")))]
        if all(m in std for m in covered):
            if row.get("label") != "none":
                row["label"] = "none"
                labelled_none.append(n)
        else:
            skipped.append(n)
    return p, {"added_figures": added, "standard_labels_set": labelled_none,
               "standard_labels_skipped": skipped}
