"""Deterministic reference-numeral assignment.

Pure module: no OCC, no ezdxf, no filesystem access. It turns the ``terms`` array of a
figure plan (see impl-contract §4.2) into numerals 1..n and answers, for any part name,
which numeral / term / label mode applies.

Determinism rules that apply here (impl-contract §7):

* numerals follow the ``terms`` order exactly -- index i gets numeral i + 1;
* a part matched by several selectors takes the FIRST matching term, "first" meaning
  lowest index in ``terms``;
* every glob goes through :func:`fnmatch.fnmatchcase`, never ``fnmatch.fnmatch``
  (``fnmatch`` lowercases through ``os.path.normcase`` on Windows, so the same plan
  would select different parts on different platforms);
* no ``set`` or ``dict`` is ever iterated -- dictionaries are used for point lookups
  only, and every listing is produced by ``sorted()`` on a total key.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "LABEL_MODES",
    "LABEL_MODE_DEFAULT",
    "INSTANCE_KEY_SEP",
    "TermEntry",
    "Numbering",
    "assign",
    "instance_key",
    "once_instance_key",
    "keys_to_label",
]

# --------------------------------------------------------------------------- constants

#: Allowed values of ``terms[].label`` (impl-contract §4.2).
LABEL_MODES: Tuple[str, ...] = ("once", "all", "none")

#: Default when ``terms[].label`` is absent (impl-contract §4.2).
LABEL_MODE_DEFAULT: str = "once"

#: Separator of ``PartShape.key`` -- ``f"{name}#{instance_index}"`` (impl-contract §5.1).
INSTANCE_KEY_SEP: str = "#"

#: Prefix and separators of the "附图标记说明" sentence (impl-contract §4.3).
_DESC_PREFIX = "附图标记说明："
_DESC_JOIN = "；"
_DESC_END = "。"
_DESC_DASH = "—"
_DESC_EMPTY = "无"


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class TermEntry:
    """One row of ``terms`` after numeral issue.

    ``index`` is the position in the plan's ``terms`` array; ``numeral`` is
    ``index + 1``. Both are kept because ``index`` is the documented precedence key
    ("first matching term wins") and ``numeral`` is what appears on the sheet.
    """

    index: int
    numeral: int
    selector: str
    term: str
    label: str


class Numbering:
    """Immutable answer table for one plan's ``terms``.

    Construct through :func:`assign`; the constructor is not part of the public API.
    """

    def __init__(self, entries: Sequence[TermEntry], part_names: Sequence[str]) -> None:
        self._entries: Tuple[TermEntry, ...] = tuple(entries)
        self._parts: Tuple[str, ...] = tuple(sorted({str(p) for p in part_names}))
        # point-lookup tables only; never iterated (§7 rule 2)
        by_part: Dict[str, TermEntry] = {}
        parts_by_numeral: Dict[int, List[str]] = {}
        for name in self._parts:
            for entry in self._entries:  # terms order == precedence order
                if fnmatchcase(name, entry.selector):
                    by_part[name] = entry
                    parts_by_numeral.setdefault(entry.numeral, []).append(name)
                    break
        self._by_part = by_part
        self._parts_by_numeral = parts_by_numeral

    # -- lookups ----------------------------------------------------------------

    def entry_of(self, part_name: str) -> Optional[TermEntry]:
        """The winning term row for ``part_name``, or ``None`` when no selector hits."""
        return self._by_part.get(part_name)

    def numeral_of(self, part_name: str) -> Optional[int]:
        entry = self._by_part.get(part_name)
        return None if entry is None else entry.numeral

    def label_mode(self, part_name: str) -> str:
        """``once`` | ``all`` | ``none``.

        An unmatched part has no term and therefore cannot carry a numeral, so it
        reports ``none``. The caller reports the unmatched part separately
        (``W_UNLABELLED_PART``); silently labelling it is not an option.
        """
        entry = self._by_part.get(part_name)
        return "none" if entry is None else entry.label

    def term_of(self, part_name: str) -> Optional[str]:
        entry = self._by_part.get(part_name)
        return None if entry is None else entry.term

    # -- listings ---------------------------------------------------------------

    @property
    def entries(self) -> Tuple[TermEntry, ...]:
        return self._entries

    @property
    def part_names(self) -> Tuple[str, ...]:
        """The deduplicated, sorted part names this table was built against."""
        return self._parts

    def parts_of(self, numeral: int) -> List[str]:
        """Every part name that resolves to ``numeral``, sorted."""
        return sorted(self._parts_by_numeral.get(int(numeral), []))

    def unmatched_parts(self) -> List[str]:
        """Selected parts that no selector hits, sorted."""
        return [name for name in self._parts if name not in self._by_part]

    def unmatched_selectors(self) -> List[str]:
        """Selectors that hit no part at all, in ``terms`` order.

        Reported, never silently dropped -- impl-contract §5.6 makes this the caller's
        error (``E_SELECTOR_NO_MATCH``).
        """
        out: List[str] = []
        for entry in self._entries:
            if not self._parts_by_numeral.get(entry.numeral):
                out.append(entry.selector)
        return out

    def table(self) -> List[Tuple[int, str, str]]:
        """``(numeral, term, selector)`` in numeral order."""
        return [(e.numeral, e.term, e.selector) for e in self._entries]

    def description_zh(self) -> str:
        """The "附图标记说明" sentence, ready to paste into the specification."""
        rows = self.table()
        if not rows:
            return _DESC_PREFIX + _DESC_EMPTY + _DESC_END
        body = _DESC_JOIN.join(
            "%d%s%s" % (numeral, _DESC_DASH, term) for numeral, term, _sel in rows
        )
        return _DESC_PREFIX + body + _DESC_END


# --------------------------------------------------------------------------- api


def assign(terms: Sequence[dict], part_names: Sequence[str]) -> Numbering:
    """Issue numerals ``1..n`` in ``terms`` order.

    Precedence: a part matched by several selectors takes the FIRST matching term,
    i.e. the one with the lowest index in ``terms``. Numerals are issued to *every*
    term, including ``label: "none"`` ones, because this function never sees the
    figures and so cannot know whether a part is labelled anywhere
    (impl-contract §5.6; the ``reference-numerals.json`` filter of §4.2 belongs to the
    renderer, which does know).

    Selectors matching nothing are NOT skipped here -- they keep their numeral and are
    reported by :meth:`Numbering.unmatched_selectors` so the caller can raise
    ``E_SELECTOR_NO_MATCH``.
    """
    entries: List[TermEntry] = []
    for index, raw in enumerate(terms):
        if not isinstance(raw, dict):
            raise ValueError("terms[%d] 不是对象：%r" % (index, raw))
        selector = raw.get("selector")
        term = raw.get("term")
        if not isinstance(selector, str) or not selector:
            raise ValueError("terms[%d].selector 必须是非空字符串" % index)
        if not isinstance(term, str) or not term:
            raise ValueError("terms[%d].term 必须是非空字符串" % index)
        label = raw.get("label", LABEL_MODE_DEFAULT)
        if label not in LABEL_MODES:
            raise ValueError(
                "terms[%d].label=%r 不是 %s 之一" % (index, label, " | ".join(LABEL_MODES))
            )
        entries.append(
            TermEntry(
                index=index,
                numeral=index + 1,
                selector=selector,
                term=term,
                label=label,
            )
        )
    return Numbering(entries, part_names)


# --------------------------------------------------------------------------- instance keys


def instance_key(name: str, instance_index: int) -> str:
    """``f"{name}#{instance_index}"`` -- the stable identity of impl-contract §5.1."""
    return "%s%s%d" % (name, INSTANCE_KEY_SEP, int(instance_index))


def once_instance_key(keys: Sequence[str]) -> str:
    """The instance a ``label: "once"`` numeral attaches to.

    impl-contract §4.2: *the one whose key sorts first among that part's instances in
    the figure*. That is a plain string sort of ``name#instance_index`` -- so
    ``"P#10"`` sorts before ``"P#2"``. Frozen here as a single function so the
    renderer and the validator cannot drift apart.
    """
    if not keys:
        raise ValueError("once_instance_key: 实例 key 列表为空")
    return sorted(str(k) for k in keys)[0]


def keys_to_label(mode: str, keys: Sequence[str]) -> List[str]:
    """Which instance keys carry a numeral under ``mode``.

    ``none`` -> ``[]``; ``once`` -> the single :func:`once_instance_key`;
    ``all`` -> every key, sorted.
    """
    if mode not in LABEL_MODES:
        raise ValueError("keys_to_label: 未知 label 模式 %r，可选 %s"
                         % (mode, " | ".join(LABEL_MODES)))
    ordered = sorted(str(k) for k in keys)
    if mode == "none" or not ordered:
        return []
    if mode == "once":
        return [ordered[0]]
    return ordered
