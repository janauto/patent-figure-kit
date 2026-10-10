"""Figure-plan loading, schema validation and semantic validation.

This module is the cage the model's freedom is kept in. The plan is the only artefact an
LLM produces (impl-contract §0), so every rejection here must tell it, in one sentence,
*which JSON edit fixes this* -- that is the ``hint`` field of :class:`PlanIssue`, and it
is mandatory on every issue. A hint that describes the symptom instead of the edit is a
bug.

Pure module: no OCC, no ezdxf, no numpy. Only the standard library plus ``jsonschema``.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from jsonschema import Draft202012Validator

from . import numbering as _numbering

__all__ = [
    "SCHEMA_PATH",
    "PLAN_SCHEMA_ID",
    "VIEW_NAMES",
    "EXPLODE_AXES",
    "DENSITIES",
    "FIGURE_KINDS",
    "AXIS_ANGLE_AUTO",
    "ANGLE_LO",
    "ANGLE_HI",
    "LABELS_PER_FIGURE_DEFAULT",
    "LABELS_PER_FIGURE_HARD_MAX",
    "FORBIDDEN_TERM_PATTERNS",
    "LAYOUT_DEFAULTS",
    "PlanIssue",
    "PlanError",
    "load_plan",
    "apply_defaults",
    "validate",
    "figure_layout",
    "selected_part_names",
    "figure_members",
    "effective_label_counts",
    "numbering_for",
    "issues_to_json",
    "format_issues",
    "has_errors",
]

# --------------------------------------------------------------------------- constants

#: ``schemas/figure-plan.schema.json`` relative to this file (scripts/patent_figure/).
SCHEMA_PATH: Path = Path(__file__).resolve().parents[2] / "schemas" / "figure-plan.schema.json"

PLAN_SCHEMA_ID: str = "patent-figure-plan/1"

#: The 7 named views of ``occ_backend.VIEWS`` (impl-contract §5.1, same entries as the
#: legacy script). Duplicated as a literal on purpose: importing ``occ_backend`` would
#: drag OCP into a module the contract requires to be testable without CAD libraries.
VIEW_NAMES: Tuple[str, ...] = ("iso", "front", "back", "right", "left", "top", "bottom")

EXPLODE_AXES: Tuple[str, ...] = ("x", "y", "z", "auto")
DENSITIES: Tuple[str, ...] = ("compact", "normal", "loose")
FIGURE_KINDS: Tuple[str, ...] = ("assembly", "exploded")

#: ``layout.axis_angle`` sentinel: the renderer solves the sheet angle per figure in
#: closed form (impl-contract §4.2, superseding §11.10 #7's fixed 124).
AXIS_ANGLE_AUTO: str = "auto"

#: Clamp interval for an explicit numeric ``axis_angle`` (impl-contract §4.2 / §11.2).
ANGLE_LO: float = 120.0
ANGLE_HI: float = 180.0

#: impl-contract §4.2 default, and §5.5 ``labels_per_figure_max`` -- the hard QA cap a
#: plan cannot raise itself above.
LABELS_PER_FIGURE_DEFAULT: int = 20
LABELS_PER_FIGURE_HARD_MAX: int = 20

#: Internal part-code shapes (impl-contract §5.5 ``FORBIDDEN_TEXT_PATTERNS``, verbatim).
#: qa.py keeps its own copy because it lives on the other side of the ezdxf boundary;
#: the two lists must stay identical.
FORBIDDEN_TERM_PATTERNS: Tuple[str, ...] = (
    r"^[A-Z]{2,4}[0-9]{4,8}(-|_)",
    r"^[0-9]{4,6}-[A-Z][0-9]{2}",
    r"_[0-9]+_[0-9]+$",
)

_FORBIDDEN_RE = tuple(re.compile(p) for p in FORBIDDEN_TERM_PATTERNS)

#: Effective ``layout`` when the plan says nothing (impl-contract §4.2).
LAYOUT_DEFAULTS: Dict[str, Any] = {
    "view": "iso",
    "explode_axis": "auto",
    "axis_angle": AXIS_ANGLE_AUTO,
    "density": "normal",
    "max_labels_per_figure": LABELS_PER_FIGURE_DEFAULT,
    "engineering_table": False,
}

#: How many names an aggregated warning spells out before it says "等 N 个".
_SAMPLE_LIMIT = 10

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

_ISSUES_SCHEMA_ID = "patent-plan-issues/1"

_REQUIRED_NAME_RE = re.compile(r"^'([^']+)' is a required property$")


# --------------------------------------------------------------------------- data


@dataclass
class PlanIssue:
    """One finding. ``hint`` is not optional -- see the module docstring."""

    code: str
    severity: str
    message: str
    hint: str
    pointer: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "hint": self.hint,
            "pointer": self.pointer,
        }

    def sort_key(self) -> Tuple[str, str, str]:
        return (self.pointer, self.code, self.message)


class PlanError(ValueError):
    """Raised by :func:`load_plan` when the file is not a usable plan.

    Carries the same :class:`PlanIssue` objects :func:`validate` would produce, so the
    CLI reports schema failures through one code path.
    """

    def __init__(self, message: str, issues: Sequence[PlanIssue]) -> None:
        super().__init__(message)
        self.issues: List[PlanIssue] = list(issues)


# --------------------------------------------------------------------------- schema


_SCHEMA_CACHE: Optional[Dict[str, Any]] = None


def plan_schema() -> Dict[str, Any]:
    """The parsed ``figure-plan.schema.json`` (read once)."""
    global _SCHEMA_CACHE
    if _SCHEMA_CACHE is None:
        with SCHEMA_PATH.open("r", encoding="utf-8") as fh:
            _SCHEMA_CACHE = json.load(fh)
    return _SCHEMA_CACHE


def _pointer_of(path: Sequence[Any]) -> str:
    if not path:
        return "/"
    return "/" + "/".join(str(part) for part in path)


def _enum_hint(pointer: str, choices: Sequence[Any]) -> str:
    return "把 %s 改成以下之一：%s" % (
        pointer,
        " | ".join(json.dumps(c, ensure_ascii=False) for c in choices),
    )


def _schema_issue(error: Any) -> PlanIssue:
    """Turn one ``jsonschema`` error into a :class:`PlanIssue` with an actionable hint."""
    pointer = _pointer_of(list(error.absolute_path))
    last = str(error.absolute_path[-1]) if error.absolute_path else ""
    validator = str(error.validator)

    if last == "axis_angle":
        return PlanIssue(
            code="E_SCHEMA",
            severity=SEVERITY_ERROR,
            message="layout.axis_angle 取值非法：%s" % error.message,
            hint='把 %s 改成字符串 "auto"（推荐，渲染器逐图闭式求解最优图面角），'
                 "或改成 %g 到 %g 之间的数" % (pointer, ANGLE_LO, ANGLE_HI),
            pointer=pointer,
        )
    if validator == "enum":
        return PlanIssue(
            code="E_UNKNOWN_ENUM",
            severity=SEVERITY_ERROR,
            message="枚举值非法：%s" % error.message,
            hint=_enum_hint(pointer, list(error.validator_value)),
            pointer=pointer,
        )
    if validator == "const":
        return PlanIssue(
            code="E_SCHEMA",
            severity=SEVERITY_ERROR,
            message="常量字段取值非法：%s" % error.message,
            hint="把 %s 改成 %s" % (pointer, json.dumps(error.validator_value, ensure_ascii=False)),
            pointer=pointer,
        )
    if validator == "required":
        matched = _REQUIRED_NAME_RE.match(error.message)
        missing = matched.group(1) if matched else "缺失字段"
        return PlanIssue(
            code="E_SCHEMA",
            severity=SEVERITY_ERROR,
            message="缺少必需字段：%s" % error.message,
            hint='在 %s 下补上 "%s" 字段' % (pointer, missing),
            pointer=pointer,
        )
    if validator == "additionalProperties":
        return PlanIssue(
            code="E_SCHEMA",
            severity=SEVERITY_ERROR,
            message="出现不被接受的字段：%s" % error.message,
            hint="删除 %s 下这些字段；本 schema 全程 additionalProperties: false，"
                 "只有 figure-plan.schema.json 列出的键可用" % pointer,
            pointer=pointer,
        )
    return PlanIssue(
        code="E_SCHEMA",
        severity=SEVERITY_ERROR,
        message="不满足 schema 约束 %s：%s" % (validator, error.message),
        hint="把 %s 改成满足 %s=%s 的值" % (
            pointer, validator, json.dumps(error.validator_value, ensure_ascii=False, default=str)),
        pointer=pointer,
    )


def schema_issues(plan: Any) -> List[PlanIssue]:
    """Every schema violation of ``plan``, in a deterministic order."""
    if not isinstance(plan, dict):
        return [PlanIssue(
            code="E_SCHEMA",
            severity=SEVERITY_ERROR,
            message="plan 顶层不是 JSON 对象，实际是 %s" % type(plan).__name__,
            hint='把整个文件改成一个对象：{"schema": "%s", "source": {...}, '
                 '"terms": [...], "figures": [...]}' % PLAN_SCHEMA_ID,
            pointer="/",
        )]
    validator = Draft202012Validator(plan_schema())
    errors = list(validator.iter_errors(plan))
    errors.sort(key=lambda e: (_pointer_of(list(e.absolute_path)), str(e.validator), e.message))
    return [_schema_issue(err) for err in errors]


# --------------------------------------------------------------------------- load / defaults


def load_plan(path: Path) -> dict:
    """Read and schema-validate a figure plan.

    Raises :class:`PlanError` (carrying ``E_SCHEMA`` / ``E_UNKNOWN_ENUM`` issues) when
    the file is not valid JSON or does not satisfy ``figure-plan.schema.json``.
    Returns the plan exactly as written -- defaults are applied by
    :func:`apply_defaults`, never here, so :func:`validate` can still see the raw
    values it must warn about.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError(
            "无法读取 plan 文件 %s：%s" % (p, exc),
            [PlanIssue(
                code="E_SCHEMA",
                severity=SEVERITY_ERROR,
                message="无法读取 plan 文件 %s：%s" % (p, exc),
                hint="确认路径存在且可读，再重新运行校验",
                pointer="/",
            )],
        ) from exc
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanError(
            "plan 不是合法 JSON：%s" % exc,
            [PlanIssue(
                code="E_SCHEMA",
                severity=SEVERITY_ERROR,
                message="plan 不是合法 JSON：第 %d 行第 %d 列 %s" % (exc.lineno, exc.colno, exc.msg),
                hint="修正第 %d 行附近的 JSON 语法（多余逗号、缺引号、注释都不允许）" % exc.lineno,
                pointer="/",
            )],
        ) from exc
    issues = schema_issues(plan)
    if issues:
        raise PlanError("plan 不满足 figure-plan.schema.json（%d 处）" % len(issues), issues)
    return plan


def _clamp_layout(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp the numeric ranges of one layout dict. Never mutates ``raw``."""
    out = dict(raw)
    angle = out.get("axis_angle")
    if isinstance(angle, bool):
        pass
    elif isinstance(angle, (int, float)):
        out["axis_angle"] = float(min(max(float(angle), ANGLE_LO), ANGLE_HI))
    cap = out.get("max_labels_per_figure")
    if isinstance(cap, bool):
        pass
    elif isinstance(cap, int):
        out["max_labels_per_figure"] = min(max(cap, 1), LABELS_PER_FIGURE_HARD_MAX)
    return out


def apply_defaults(plan: dict) -> dict:
    """Merge the layout defaults and clamp ranges. Never mutates ``plan``.

    * ``layout`` becomes fully populated with every key of :data:`LAYOUT_DEFAULTS`;
    * every figure gets a fully resolved ``layout`` (figure override merged over the
      plan-level layout), so downstream modules never have to re-merge;
    * every term gets an explicit ``label``;
    * ``source.include`` / ``source.exclude`` become explicit lists.
    """
    out = copy.deepcopy(plan)

    source = out.get("source")
    if isinstance(source, dict):
        source.setdefault("include", [])
        source.setdefault("exclude", [])

    base = dict(LAYOUT_DEFAULTS)
    raw_layout = out.get("layout")
    if isinstance(raw_layout, dict):
        base.update(_clamp_layout(raw_layout))
    out["layout"] = base

    figures = out.get("figures")
    if isinstance(figures, list):
        for fig in figures:
            if not isinstance(fig, dict):
                continue
            merged = dict(base)
            fig_layout = fig.get("layout")
            if isinstance(fig_layout, dict):
                merged.update(_clamp_layout(fig_layout))
            fig["layout"] = merged

    terms = out.get("terms")
    if isinstance(terms, list):
        for term in terms:
            if isinstance(term, dict):
                term.setdefault("label", _numbering.LABEL_MODE_DEFAULT)

    return out


def figure_layout(plan: dict, figure: dict) -> Dict[str, Any]:
    """The effective layout of one figure: figure override over plan-level over defaults."""
    base = dict(LAYOUT_DEFAULTS)
    raw_layout = plan.get("layout")
    if isinstance(raw_layout, dict):
        base.update(_clamp_layout(raw_layout))
    fig_layout = figure.get("layout") if isinstance(figure, dict) else None
    if isinstance(fig_layout, dict):
        base.update(_clamp_layout(fig_layout))
    return base


# --------------------------------------------------------------------------- part selection


def _assembly_part_names(assembly: dict) -> List[str]:
    parts = assembly.get("parts") if isinstance(assembly, dict) else None
    names: List[str] = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("name"), str):
                names.append(part["name"])
    return sorted(set(names))


def _assembly_instances(assembly: dict) -> Dict[str, int]:
    out: Dict[str, int] = {}
    parts = assembly.get("parts") if isinstance(assembly, dict) else None
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("name"), str):
                continue
            count = part.get("instances", 1)
            out[part["name"]] = int(count) if isinstance(count, int) and not isinstance(count, bool) else 1
    return out


def _matches_any(name: str, globs: Sequence[Any]) -> bool:
    for pattern in globs:
        if isinstance(pattern, str) and fnmatchcase(name, pattern):
            return True
    return False


def selected_part_names(plan: dict, assembly: dict) -> List[str]:
    """Assembly part names surviving ``source.include`` / ``source.exclude``, sorted.

    ``include`` absent or empty means every part. ``exclude`` is applied afterwards.
    All globs go through ``fnmatchcase`` (§7 rule 6).
    """
    source = plan.get("source") if isinstance(plan, dict) else None
    include = source.get("include", []) if isinstance(source, dict) else []
    exclude = source.get("exclude", []) if isinstance(source, dict) else []
    include = include if isinstance(include, list) else []
    exclude = exclude if isinstance(exclude, list) else []
    out: List[str] = []
    for name in _assembly_part_names(assembly):
        if include and not _matches_any(name, include):
            continue
        if exclude and _matches_any(name, exclude):
            continue
        out.append(name)
    return out


def figure_members(figure: dict, selected: Sequence[str]) -> List[str]:
    """Selected part names matched by any of ``figure.members``, sorted."""
    globs = figure.get("members") if isinstance(figure, dict) else None
    globs = globs if isinstance(globs, list) else []
    out: List[str] = []
    for name in selected:
        if _matches_any(name, globs):
            out.append(name)
    return sorted(set(out))


def numbering_for(plan: dict, assembly: dict) -> "_numbering.Numbering":
    """The :class:`~patent_figure.numbering.Numbering` this plan implies."""
    terms = plan.get("terms") if isinstance(plan, dict) else None
    terms = terms if isinstance(terms, list) else []
    return _numbering.assign(terms, selected_part_names(plan, assembly))


def _label_count(name: str, num: "_numbering.Numbering", instances: Dict[str, int]) -> int:
    mode = num.label_mode(name)
    if mode == "none" or num.numeral_of(name) is None:
        return 0
    if mode == "once":
        return 1
    return max(int(instances.get(name, 1)), 1)


def effective_label_counts(plan: dict, assembly: dict) -> List[Tuple[str, int, int]]:
    """``(figure_id, label_count, max_labels_per_figure)`` per figure, in plan order."""
    selected = selected_part_names(plan, assembly)
    instances = _assembly_instances(assembly)
    num = numbering_for(plan, assembly)
    figures = plan.get("figures") if isinstance(plan, dict) else None
    figures = figures if isinstance(figures, list) else []
    out: List[Tuple[str, int, int]] = []
    for fig in figures:
        if not isinstance(fig, dict):
            continue
        members = figure_members(fig, selected)
        count = sum(_label_count(name, num, instances) for name in members)
        cap = int(figure_layout(plan, fig).get("max_labels_per_figure", LABELS_PER_FIGURE_DEFAULT))
        out.append((str(fig.get("id", "")), count, cap))
    return out


# --------------------------------------------------------------------------- hints


def _split_hint(figure_id: str, count: int, cap: int, assembly: dict) -> str:
    suggestions = assembly.get("split_suggestions") if isinstance(assembly, dict) else None
    suggestions = suggestions if isinstance(suggestions, list) else []
    head = "%s 有 %d 个标记，上限 %d：" % (figure_id, count, cap)
    if suggestions and isinstance(suggestions[0], dict):
        first = suggestions[0]
        figs = first.get("figures")
        n_fig = len(figs) if isinstance(figs, list) else 2
        strategy = first.get("strategy") or first.get("id") or "coaxial"
        return head + "按 assembly.json 的 split_suggestions[0]（strategy=%s，%d 张图）把 %s 的 " \
                      "members 拆成 %d 条 figures" % (strategy, n_fig, figure_id, n_fig)
    return head + "assembly.json 的 split_suggestions 为空，请手动把 %s 的 members 拆成两条 " \
                  "figures，或把标准件对应的 terms[].label 改成 \"none\"" % figure_id


def _sample(names: Sequence[str]) -> str:
    shown = list(names[:_SAMPLE_LIMIT])
    text = "、".join(shown)
    if len(names) > _SAMPLE_LIMIT:
        text += " 等 %d 个" % len(names)
    return text


# --------------------------------------------------------------------------- validate


def validate(plan: dict, assembly: dict) -> List[PlanIssue]:
    """Every problem with ``plan`` against ``assembly``, each carrying a repair hint.

    Schema violations are reported first and short-circuit the semantic checks: those
    checks read fields the schema is what guarantees the shape of. Warnings never
    short-circuit anything -- the caller decides (the CLI exits 1 only on ``error``).
    """
    issues: List[PlanIssue] = schema_issues(plan)
    if issues:
        return issues

    all_names = _assembly_part_names(assembly)
    selected = selected_part_names(plan, assembly)
    instances = _assembly_instances(assembly)
    terms = plan.get("terms") or []
    figures = plan.get("figures") or []

    # ---- W_CLAMPED: read the RAW values, before apply_defaults hides them ----------
    raw_layouts: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(plan.get("layout"), dict):
        raw_layouts.append(("/layout", plan["layout"]))
    for idx, fig in enumerate(figures):
        if isinstance(fig, dict) and isinstance(fig.get("layout"), dict):
            raw_layouts.append(("/figures/%d/layout" % idx, fig["layout"]))
    for pointer, raw in raw_layouts:
        angle = raw.get("axis_angle")
        if isinstance(angle, (int, float)) and not isinstance(angle, bool):
            if float(angle) < ANGLE_LO or float(angle) > ANGLE_HI:
                clamped = min(max(float(angle), ANGLE_LO), ANGLE_HI)
                issues.append(PlanIssue(
                    code="W_CLAMPED",
                    severity=SEVERITY_WARNING,
                    message="axis_angle=%g 超出 [%g, %g]，已夹紧为 %g"
                            % (float(angle), ANGLE_LO, ANGLE_HI, clamped),
                    hint='把 %s/axis_angle 改成 "auto"（推荐）或 %g 到 %g 之间的数'
                         % (pointer, ANGLE_LO, ANGLE_HI),
                    pointer=pointer + "/axis_angle",
                ))
        cap = raw.get("max_labels_per_figure")
        if isinstance(cap, int) and not isinstance(cap, bool) and cap > LABELS_PER_FIGURE_HARD_MAX:
            issues.append(PlanIssue(
                code="W_CLAMPED",
                severity=SEVERITY_WARNING,
                message="max_labels_per_figure=%d 超过 qa.py 的硬上限 %d，已夹紧"
                        % (cap, LABELS_PER_FIGURE_HARD_MAX),
                hint="把 %s/max_labels_per_figure 改成不大于 %d 的整数；这条上限由 qa.py 的 "
                     "labels_per_figure_max 强制，plan 抬不高它"
                     % (pointer, LABELS_PER_FIGURE_HARD_MAX),
                pointer=pointer + "/max_labels_per_figure",
            ))

    # ---- E_DUPLICATE_FIGURE_ID ----------------------------------------------------
    seen_ids: Dict[str, int] = {}
    for idx, fig in enumerate(figures):
        fid = fig.get("id")
        if not isinstance(fid, str):
            continue
        if fid in seen_ids:
            issues.append(PlanIssue(
                code="E_DUPLICATE_FIGURE_ID",
                severity=SEVERITY_ERROR,
                message="figures[%d].id=%r 与 figures[%d] 重复；输出文件 out/%s.dxf 会被覆盖"
                        % (idx, fid, seen_ids[fid], fid),
                hint="把 /figures/%d/id 改成一个未被使用的 id（例如 %r）" % (idx, fid + "b"),
                pointer="/figures/%d/id" % idx,
            ))
        else:
            seen_ids[fid] = idx

    # ---- E_SELECTOR_NO_MATCH / E_TERM_LOOKS_LIKE_PART_CODE ------------------------
    for idx, term in enumerate(terms):
        selector = term.get("selector")
        if isinstance(selector, str):
            hit_all = [n for n in all_names if fnmatchcase(n, selector)]
            if not hit_all:
                issues.append(PlanIssue(
                    code="E_SELECTOR_NO_MATCH",
                    severity=SEVERITY_ERROR,
                    message="terms[%d].selector=%r 在 assembly.json 的 %d 个零件里一个也没命中"
                            % (idx, selector, len(all_names)),
                    hint="把 /terms/%d/selector 改成 assembly.json:parts[].name 里真实存在的名字或"
                         "通配式（现有零件名例如：%s）"
                         % (idx, _sample(all_names) if all_names else "assembly.json 里没有零件"),
                    pointer="/terms/%d/selector" % idx,
                ))
            else:
                hit_selected = [n for n in selected if fnmatchcase(n, selector)]
                if not hit_selected:
                    issues.append(PlanIssue(
                        code="W_UNLABELLED_PART",
                        severity=SEVERITY_WARNING,
                        message="terms[%d].selector=%r 命中的零件（%s）全部被 source.include/"
                                "exclude 过滤掉了，这条术语不会出现在任何图上"
                                % (idx, selector, _sample(hit_all)),
                        hint="要么删掉 /terms/%d，要么放宽 /source/exclude（或 /source/include）"
                             "让这些零件重新进入选集" % idx,
                        pointer="/terms/%d/selector" % idx,
                    ))
        text = term.get("term")
        if isinstance(text, str):
            for pattern, regex in zip(FORBIDDEN_TERM_PATTERNS, _FORBIDDEN_RE):
                if regex.search(text):
                    issues.append(PlanIssue(
                        code="E_TERM_LOOKS_LIKE_PART_CODE",
                        severity=SEVERITY_ERROR,
                        message="terms[%d].term=%r 命中内部件号形态 %s；附图上只能出现中文技术名词，"
                                "件号属于不可外泄的内部信息" % (idx, text, pattern),
                        hint="把 /terms/%d/term 改成这个零件的中文技术名词（如「底座」「回转轴」），"
                             "不要写件号、图号或英文代号" % idx,
                        pointer="/terms/%d/term" % idx,
                    ))
                    break

    # ---- E_MEMBER_NO_MATCH ---------------------------------------------------------
    for idx, fig in enumerate(figures):
        fid = fig.get("id") if isinstance(fig.get("id"), str) else "figures[%d]" % idx
        members = fig.get("members") or []
        for m_idx, glob in enumerate(members):
            if not isinstance(glob, str):
                continue
            if not any(fnmatchcase(n, glob) for n in selected):
                excluded = [n for n in all_names if fnmatchcase(n, glob)]
                if excluded:
                    hint = ("把 /figures/%d/members/%d 改掉，或放宽 /source/exclude —— %r 只命中了"
                            "被 source 过滤掉的零件（%s）" % (idx, m_idx, glob, _sample(excluded)))
                else:
                    hint = ("把 /figures/%d/members/%d 改成选集里真实存在的名字或通配式"
                            "（现有可选零件：%s）"
                            % (idx, m_idx, _sample(selected) if selected else "选集为空，先检查 /source"))
                issues.append(PlanIssue(
                    code="E_MEMBER_NO_MATCH",
                    severity=SEVERITY_ERROR,
                    message="%s 的 members[%d]=%r 没有命中任何被选中的零件" % (fid, m_idx, glob),
                    hint=hint,
                    pointer="/figures/%d/members/%d" % (idx, m_idx),
                ))

    # ---- E_TOO_MANY_LABELS ---------------------------------------------------------
    num = numbering_for(plan, assembly)
    for idx, fig in enumerate(figures):
        if not isinstance(fig, dict):
            continue
        fid = fig.get("id") if isinstance(fig.get("id"), str) else "figures[%d]" % idx
        members = figure_members(fig, selected)
        count = sum(_label_count(name, num, instances) for name in members)
        cap = int(figure_layout(plan, fig).get("max_labels_per_figure", LABELS_PER_FIGURE_DEFAULT))
        if count > cap:
            issues.append(PlanIssue(
                code="E_TOO_MANY_LABELS",
                severity=SEVERITY_ERROR,
                message="%s 的有效标记数 %d 超过上限 %d（label:\"once\" 记 1 个，"
                        "label:\"all\" 按实例数记，label:\"none\" 记 0 个）" % (fid, count, cap),
                hint=_split_hint(str(fid), count, cap, assembly),
                pointer="/figures/%d/members" % idx,
            ))

    # ---- W_UNLABELLED_PART ---------------------------------------------------------
    figured: List[str] = []
    for fig in figures:
        if isinstance(fig, dict):
            figured.extend(figure_members(fig, selected))
    unlabelled = sorted({n for n in figured if num.numeral_of(n) is None})
    if unlabelled:
        issues.append(PlanIssue(
            code="W_UNLABELLED_PART",
            severity=SEVERITY_WARNING,
            message="%d 个出现在附图里的零件没有任何 terms[].selector 命中，它们将不带标记出现："
                    "%s" % (len(unlabelled), _sample(unlabelled)),
            hint="给它们补 /terms 条目（label:\"none\" 表示确实不标注），或在 /source/exclude 里"
                 "排除它们",
            pointer="/terms",
        ))

    issues.sort(key=lambda issue: issue.sort_key())
    return issues


# --------------------------------------------------------------------------- reporting


def has_errors(issues: Sequence[PlanIssue]) -> bool:
    return any(issue.severity == SEVERITY_ERROR for issue in issues)


def issues_to_json(issues: Sequence[PlanIssue], *, plan_path: str = "", assembly_path: str = "") -> dict:
    """The ``--json`` payload of ``validate_figure_plan.py``."""
    errors = sum(1 for i in issues if i.severity == SEVERITY_ERROR)
    return {
        "schema": _ISSUES_SCHEMA_ID,
        "plan": plan_path,
        "assembly": assembly_path,
        "pass": errors == 0,
        "issues": [issue.to_dict() for issue in issues],
        "summary": {"errors": errors, "warnings": len(issues) - errors},
    }


def format_issues(issues: Sequence[PlanIssue]) -> str:
    """Human-readable report: one issue per two lines, hint always shown."""
    lines: List[str] = []
    for issue in issues:
        mark = "错误" if issue.severity == SEVERITY_ERROR else "警告"
        lines.append("[%s] %s  %s" % (mark, issue.code, issue.pointer))
        lines.append("    %s" % issue.message)
        lines.append("    修复：%s" % issue.hint)
    return "\n".join(lines)
