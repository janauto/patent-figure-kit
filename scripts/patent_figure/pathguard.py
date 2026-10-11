"""命令行写入目标的统一前置校验（WP2 修复轮 1 / Codex R2 #2）。

WP1 给渲染 CLI 立了规矩（``render_patent_figure._guard_side_paths``）：报告、缓存这类「旁路写入」在
渲染**之前**就要校验，按**文件系统身份**判定（``fsid``：符号链接、``..``、悬空链接、大小写别名都是
同一个地方），不得落进任何版本库的保留区（``versions.protected_path_reason``：``current.json``、
``results/…``、``refs/…``、``out`` 视图、锁文件……）。WP2 新增的 CLI（候选分图、导出包、逐页核验、
analyze 的逐实例旁路文件）在这里复用同一套判定，并补两条：

* 不得写到**输入文件**上，目录型输出也不得**包含**输入文件（会被覆盖或混进产物）；
* 声明为互斥的两个输出（例如 ``--json`` 与导出包目录 ``-o``）不得落在同一处或互相包含；
* 工作目录必须是新的 / 空的，或带本工具的标记文件（``claim_workdir``）——不能把一个现成的工作台
  工程目录、别的版本库当成工作目录去写（那里的 ``plan.json`` 不在保留区，却是别人的数据）。

所有判定都**不以存在为前提**（首次运行时目标还不存在）；调用方在真正写入前再调一次（纵深防御）。
纯标准库，OCP 无关。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Tuple

from patent_figure import fsid as _fsid
from patent_figure import versions as _V


def lands_in(target, root) -> bool:
    """Does a write to ``target`` land in ``root`` (or ``root`` itself)? By identity, not
    spelling; the root is tested both resolved and as the location of its own link."""
    if target is None or root is None:
        return False
    return _fsid.inside(target, root, root_follow_leaf=True) or \
        _fsid.inside(target, root, root_follow_leaf=False)


def check_outputs(outputs: Sequence[Tuple[str, Any]], inputs: Iterable[Any] = (),
                  disjoint: Sequence[Tuple[str, str]] = ()) -> Optional[str]:
    """``outputs`` = ``[(flag, path)]`` every path this run will write (a file, or a directory it
    writes into); ``inputs`` = files it reads; ``disjoint`` = pairs of output flags that must not
    overlap. Returns the first problem (Chinese, names the flag) or None."""
    outs = [(f, p) for f, p in outputs if p not in (None, "")]
    ins = [p for p in inputs if p not in (None, "")]
    for flag, path in outs:
        why = _V.protected_path_reason(path)
        if why:
            return "%s %s %s——不能写进版本库的保留区" % (flag, path, why)
        for inp in ins:
            if lands_in(path, inp):
                return "%s %s 与输入文件 %s 是同一处（会覆盖输入）" % (flag, path, inp)
            if lands_in(inp, path):
                return "%s %s 包含输入文件 %s（输入会被覆盖或混进产物）" % (flag, path, inp)
    by_flag = dict(outs)
    for a, b in disjoint:
        pa, pb = by_flag.get(a), by_flag.get(b)
        if pa is None or pb is None:
            continue
        if lands_in(pa, pb) or lands_in(pb, pa):
            return "%s %s 与 %s %s 指向同一处或互相包含" % (a, pa, b, pb)
    return None


def claim_workdir(path, marker: str) -> Optional[str]:
    """A work directory must be NEW, EMPTY, or one this tool created before (``marker`` file
    inside). Anything else — a workbench project folder, another version store, a user folder —
    is refused: files there that are not in a reserved area (``plan.json``…) are still someone
    else's data. On success the marker is written. Returns a problem string or None."""
    p = Path(path)
    if p.is_symlink() or (p.exists() and not p.is_dir()):
        return "工作目录 %s 不是普通目录" % p
    if p.is_dir():
        entries = [e for e in os.listdir(str(p)) if e != ".DS_Store"]
        if entries and marker not in entries:
            return ("工作目录 %s 已存在且不是本工具建的（缺标记 %s）——请换一个新目录"
                    % (p, marker))
    p.mkdir(parents=True, exist_ok=True)
    m = p / marker
    if not m.is_file():
        m.write_text("patent-figure-kit work dir\n", encoding="utf-8")
    return None
