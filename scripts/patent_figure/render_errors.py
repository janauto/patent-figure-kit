"""出图失败的结构化原因（WP2b F3/F4）。

渲染作业失败时，原来只有 ``E_RENDER_NOT_OK`` +「出图未全部过闸/有图缺失」，真正原因（计划校验没过——
上传的模型零件还没起名；排版阶段放不下；某项 QA 没过）只在日志里。本模块把一次失败的渲染**按冻结的
输入与渲染器自己的报告**归类成给人看的结构化错误，作业的 ``error``（以及 SSE ``error`` 事件）原样携带：

* 计划里有空白术语（零件未起名）→ ``code: E_TERMS_MISSING``、``hint_plain``「还有 N 个零件没有名称，去术语页
  填写或用 AI 起名」、``fix_action: open_terms``（focus=names）；
* 其他计划校验错误 → ``E_RENDER_NOT_OK``（伞形错误码不变，旧客户端与启动对账照旧），``cause_code`` = 第一条
  错误码，白话与修复动作取 ``qa.error_plain_fields``；
* 渲染跑了但有图没交付 → ``E_RENDER_NOT_OK``，``figures[]`` 逐图给出 ``{figure, code, title_zh, hint_plain,
  fix_action, detail}``：排版阶段错误（渲染器报告的 ``errors``）走 ``qa.error_plain_fields``，QA 未过取
  该图 qa.json 里第一项未过检查的白话层（与 qa.py F4 表同一来源）；顶层的 ``title_zh / hint_plain /
  fix_action`` 取第一张失败的图。

纯函数：不 import OCP、不写文件；只读调用方给的 plan / assembly / 渲染结果。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from patent_figure import plan as _plan
from patent_figure import qa as _qa
from patent_figure import suggest_plan as _sug

#: Umbrella code of a render that ran (or was refused) but did not deliver every figure. Kept
#: as is for every failure that is not "parts still unnamed" — startup reconciliation and older
#: clients key on it; the specific reason is in ``cause_code`` / ``figures[]``.
E_RENDER_NOT_OK = "E_RENDER_NOT_OK"
E_TERMS_MISSING = "E_TERMS_MISSING"
#: The plain text of E_TERMS_MISSING is fixed by the work order (WP2b F3).
TERMS_MISSING_PLAIN = "还有 %d 个零件没有名称，去术语页填写或用 AI 起名"
TERMS_ROWS_PLAIN = "术语表里还有 %d 行没有填写名称，去术语页填写或删掉这些行"
#: How many names / issues / figures an error carries (the job record stays small).
MAX_LISTED = 50


def plan_issues(plan: Any, assembly: Any) -> List[Dict[str, str]]:
    """Schema + semantic issues of ``plan`` (errors only), as dicts. Never raises."""
    try:
        issues = list(_plan.schema_issues(plan))
        if not issues and isinstance(assembly, dict):
            issues = list(_plan.validate(plan, assembly))
    except Exception as exc:  # noqa: BLE001 — classification must never mask the real failure
        return [{"code": "E_SCHEMA", "severity": "error", "message": str(exc), "hint": "",
                 "pointer": "/"}]
    return [i.to_dict() for i in issues if i.severity == _plan.SEVERITY_ERROR]


def blank_term_rows(plan: Any) -> List[int]:
    terms = plan.get("terms") if isinstance(plan, dict) else None
    return [i for i, t in enumerate(terms if isinstance(terms, list) else [])
            if isinstance(t, dict) and not str(t.get("term") or "").strip()]


def unnamed_parts(plan: Any, assembly: Any) -> List[str]:
    """Parts that will be drawn (non-degenerate, kept by ``source`` include/exclude) whose
    governing term row (numbering precedence: first matching selector) has a blank term."""
    if not isinstance(plan, dict) or not isinstance(assembly, dict):
        return []
    try:
        parts = _sug._parts(assembly, plan)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for p in parts:
        row = _sug.winning_row(plan, p["name"])
        if row is not None and not str(row.get("term") or "").strip():
            out.append(p["name"])
    return sorted(out)


def _first_figure(plan: Any) -> str:
    figs = plan.get("figures") if isinstance(plan, dict) else None
    for f in figs if isinstance(figs, list) else []:
        if isinstance(f, dict) and f.get("id"):
            return str(f["id"])
    return "fig1"


def _figure_of_issue(issue: Dict[str, str], plan: Any) -> str:
    """Best-effort figure id of a plan issue (``/figures/<k>/…`` pointer), else the first."""
    ptr = str(issue.get("pointer") or "")
    figs = plan.get("figures") if isinstance(plan, dict) else None
    if ptr.startswith("/figures/") and isinstance(figs, list):
        try:
            k = int(ptr.split("/")[2])
            if 0 <= k < len(figs) and isinstance(figs[k], dict) and figs[k].get("id"):
                return str(figs[k]["id"])
        except (ValueError, IndexError):
            pass
    msg = str(issue.get("message") or "")
    for f in figs if isinstance(figs, list) else []:
        if isinstance(f, dict) and f.get("id") and msg.startswith(str(f["id"]) + " "):
            return str(f["id"])
    return _first_figure(plan)


def terms_missing(plan: Any, assembly: Any,
                  issues: Optional[List[Dict[str, str]]] = None) -> Optional[Dict[str, Any]]:
    """The E_TERMS_MISSING error fields, or ``None`` when no term row is blank."""
    rows = blank_term_rows(plan)
    if not rows:
        return None
    names = unnamed_parts(plan, assembly)
    n = len(names)
    plain = TERMS_MISSING_PLAIN % n if n else TERMS_ROWS_PLAIN % len(rows)
    return {
        "code": E_TERMS_MISSING,
        "message": ("计划校验未通过：%d 个零件还没有名称（术语为空），未渲染任何图" % n if n else
                    "计划校验未通过：术语表里 %d 行名称为空，未渲染任何图" % len(rows)),
        "hint": "给 terms[] 里 term 为空的行填中文技术名词（或调 ai/draft-terms 起名）后重新出图",
        "title_zh": "零件未起名",
        "hint_plain": plain,
        "fix_action": {"kind": "open_terms",
                       "target": {"figure": _first_figure(plan), "focus": "names"}},
        "unnamed_count": n,
        "unnamed_parts": names[:MAX_LISTED],
        "blank_term_rows": len(rows),
        "issues": list(issues or [])[:MAX_LISTED],
    }


def _figure_rows(plan: Any, result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One row per figure that was not delivered, in plan order."""
    order = [str(f.get("id")) for f in (plan.get("figures") or [])
             if isinstance(f, dict)] if isinstance(plan, dict) else []
    errors = {}
    for e in ((result.get("render_report") or {}).get("errors") or []):
        if isinstance(e, dict) and e.get("figure") is not None:
            errors.setdefault(str(e["figure"]), e)
    figs = {str(f.get("id")): f for f in (result.get("figures") or []) if isinstance(f, dict)}
    rows = []
    for fid in order + sorted(set(errors) - set(order)):
        fig = figs.get(fid)
        if fig is not None and fig.get("pass"):
            continue
        if fid in errors:
            e = errors[fid]
            row = _qa.error_plain_fields(str(e.get("code") or ""), fid, str(e.get("message") or ""))
            row.update(figure=fid, code=str(e.get("code") or ""), detail=str(e.get("message") or ""))
        elif fig is not None and fig.get("qa"):
            failed = [c for c in (fig["qa"].get("checks") or []) if not c.get("pass")]
            if failed:
                c = failed[0]
                row = {"figure": fid, "code": "E_QA_FAILED", "check": c.get("id"),
                       "title_zh": c.get("title_zh") or c.get("id"),
                       "hint_plain": c.get("hint_plain") or c.get("hint") or "",
                       "fix_action": c.get("fix_action"),
                       "detail": "；".join("%s 未过" % (x.get("title_zh") or x.get("id"))
                                          for x in failed)}
            else:                                    # QA passed but the gate dropped it (HLR)
                row = {"figure": fid, "code": "E_HLR_DROPPED", "check": None,
                       "title_zh": "消隐丢边",
                       "hint_plain": "这张图在消隐时丢了线条，不能交付。换一个视角，"
                                     "或把被遮挡的零件放进分解图。",
                       "fix_action": {"kind": "change_view", "target": {"figure": fid}},
                       "detail": "QA 通过但消隐丢边，未过闸"}
        else:
            title, plain = _qa.ERROR_WRITER_SIDE
            row = {"figure": fid, "code": "E_FIGURE_MISSING", "check": None,
                   "title_zh": title, "hint_plain": plain, "fix_action": None,
                   "detail": "渲染没有产出这张图（见日志）"}
        # 图号 = 计划里的顺序（与标注步骤写进图面的「图N」一致）；白话里用它，不用内部 id。
        row["number"] = order.index(fid) + 1 if fid in order else None
        rows.append(row)
    return rows


def _fig_label(row: Dict[str, Any]) -> str:
    return "图%d" % row["number"] if row.get("number") else "有一张图"


def render_failure(plan: Any, assembly: Any, result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Classify a failed render into the job ``error`` fields (``code`` / ``message`` / ``hint``
    plus ``title_zh`` / ``hint_plain`` / ``fix_action`` and the per-figure / per-issue detail)."""
    result = result if isinstance(result, dict) else {}
    issues = plan_issues(plan, assembly)
    if issues:
        missing = terms_missing(plan, assembly, issues)
        if missing is not None:
            return missing
        first = issues[0]
        fid = _figure_of_issue(first, plan)
        row = _qa.error_plain_fields(first.get("code", ""), fid, first.get("message", ""))
        return {"code": E_RENDER_NOT_OK, "cause_code": first.get("code", ""),
                "message": "计划校验未通过（%d 项），未渲染任何图：%s"
                           % (len(issues), first.get("message", "")),
                "hint": first.get("hint") or "按 issues 逐条修正计划后重渲",
                "title_zh": row["title_zh"], "hint_plain": row["hint_plain"],
                "fix_action": row["fix_action"], "figure": fid,
                "issues": issues[:MAX_LISTED]}
    rows = _figure_rows(plan, result)
    failed = [r["figure"] for r in rows]
    out: Dict[str, Any] = {
        "code": E_RENDER_NOT_OK,
        "message": "出图未全部过闸/有图缺失：%s" % (failed or "（无合格图）"),
        "hint": "按每张图的 QA 提示改计划后重渲",
        "figures": rows[:MAX_LISTED],
    }
    if rows:
        head = rows[0]
        out.update(cause_code=head["code"], figure=head["figure"], title_zh=head["title_zh"],
                   hint_plain=("%s没有过闸。%s" % (_fig_label(head), head["hint_plain"])
                               if len(rows) == 1 else
                               "%d 张图没有过闸。先看%s：%s"
                               % (len(rows), _fig_label(head), head["hint_plain"])),
                   fix_action=head["fix_action"])
    else:
        missing = list(result.get("missing_artefacts") or [])
        title, plain = _qa.ERROR_WRITER_SIDE
        out.update(cause_code="E_ARTEFACTS_MISSING", title_zh=title,
                   hint_plain="图都过了闸，但标注或产物不完整，本次没有发布。" + plain,
                   fix_action=None, missing_artefacts=missing[:MAX_LISTED])
    return out
