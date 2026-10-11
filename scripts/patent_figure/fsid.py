"""Path containment by FILE-SYSTEM IDENTITY, not by spelling (WP1 R1j).

Every earlier bypass of the write-target guard (R1g–R1j) was a second *spelling* of the same
place: a symlinked parent, a dangling link, ``alias/..``, and finally a different letter case on
a case-insensitive volume (macOS / Windows default). String comparison cannot win that race, so
containment is decided the way the OS decides where a write lands:

* ``locate(path)`` resolves the RAW path as the OS will (``realpath`` of the un-normalised path:
  symlinks first, then ``..``; dangling links followed) and splits it into the deepest EXISTING
  prefix — identified by ``(st_dev, st_ino)`` — plus the not-yet-existing tail;
* ``inside(target, root)`` — an existing root contains the target iff the root's identity is in
  the identity chain of the target's existing prefix; a root that does not exist yet contains it
  iff both reach the SAME existing directory and the target's tail starts with the root's tail
  under that directory's name-equivalence rule;
* the name-equivalence rule of a directory is PROBED on its file system (``case_insensitive``):
  an existing entry's other-case spelling is looked up read-only and compared by identity
  (nothing is written); only if no entry can be probed does it fall back to the platform
  default (macOS / Windows: insensitive). Names are NFC-normalised before comparing (APFS and
  HFS+ treat composed / decomposed forms as one name; over-matching on other systems only makes
  the guard stricter).

OCP-free, stdlib only.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Dict, List, NamedTuple, Optional, Tuple

_rules: Dict[int, bool] = {}          # st_dev -> case-insensitive?


class Loc(NamedTuple):
    existing: str                     # deepest existing prefix (resolved spelling)
    tail: Tuple[str, ...]             # components below it that do not exist yet


def raw_abs(path) -> str:
    """Absolute WITHOUT normalising (``..`` stays put, so realpath applies it after symlinks)."""
    raw = os.fspath(path)
    return raw if os.path.isabs(raw) else os.path.join(os.getcwd(), raw)


def _split_existing(resolved: str) -> Loc:
    p = resolved
    tail: List[str] = []
    while True:
        if os.path.lexists(p):
            return Loc(p, tuple(reversed(tail)))
        parent, name = os.path.split(p)
        if parent == p:                                   # reached the root
            return Loc(p, tuple(reversed(tail)))
        if name:
            tail.append(name)
        p = parent


def locate(path, follow_leaf: bool = True) -> Loc:
    """Where ``path`` lands. ``follow_leaf=False`` keeps an existing leaf symlink as itself
    (the location OF a view link rather than what it points to); its parent is still resolved."""
    raw = raw_abs(path)
    parent, name = os.path.split(raw.rstrip(os.sep) or raw)
    if follow_leaf or name in ("", os.curdir, os.pardir):
        return _split_existing(os.path.realpath(raw))
    return _split_existing(os.path.join(os.path.realpath(parent), name))


def _ident(p: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.lstat(p)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def id_chain(existing: str) -> List[Tuple[int, int]]:
    """Identities of ``existing`` and every ancestor up to the root."""
    out, p = [], existing
    while True:
        i = _ident(p)
        if i is not None:
            out.append(i)
        parent = os.path.dirname(p)
        if parent == p:
            return out
        p = parent


def _platform_default() -> bool:
    return sys.platform == "darwin" or os.name == "nt"


def case_insensitive(directory: str) -> bool:
    """Does the file system holding ``directory`` treat names case-insensitively? Probed
    read-only: an entry with cased letters is looked up under its swapped case and compared by
    identity. Cached per device. Falls back to the platform default if nothing can be probed."""
    try:
        dev = os.stat(directory).st_dev
    except OSError:
        return _platform_default()
    if dev in _rules:
        return _rules[dev]
    verdict = None
    probe = directory
    for _ in range(64):
        try:
            if os.stat(probe).st_dev != dev:
                break
            entries = os.listdir(probe)
        except OSError:
            entries = []
        for e in entries[:256]:
            alt = e.swapcase()
            if alt == e:
                continue
            a, b = os.path.join(probe, e), os.path.join(probe, alt)
            if not os.path.lexists(b):
                verdict = False                       # other spelling is absent: sensitive
            else:
                verdict = _ident(a) == _ident(b)      # same object → insensitive
            break
        if verdict is not None:
            break
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if verdict is None:
        verdict = _platform_default()
    _rules[dev] = verdict
    return verdict


def name_key(name: str, insensitive: bool) -> str:
    n = unicodedata.normalize("NFC", name)
    return n.casefold() if insensitive else n


def names_equal(a: str, b: str, insensitive: bool) -> bool:
    return name_key(a, insensitive) == name_key(b, insensitive)


def _tail_startswith(tail: Tuple[str, ...], prefix: Tuple[str, ...], insensitive: bool) -> bool:
    if len(tail) < len(prefix):
        return False
    return all(names_equal(t, p, insensitive) for t, p in zip(tail, prefix))


def inside(target, root, root_follow_leaf: bool = True) -> bool:
    """Is ``target`` (where a write to it lands) the ``root`` or anything below it — judged by
    identity of the existing parts and the probed name rule for the parts not created yet?"""
    t = locate(target, follow_leaf=True)              # a write follows symlinks
    r = locate(root, follow_leaf=root_follow_leaf)
    r_id = _ident(r.existing)
    if r_id is None:
        return False
    if not r.tail:                                    # the root exists: identity in the chain
        return r_id in id_chain(t.existing)
    # the root does not exist yet: the target must reach the SAME existing directory and then
    # walk down the root's (future) tail under that directory's name rule
    if _ident(t.existing) != r_id:
        return False
    return _tail_startswith(t.tail, r.tail, case_insensitive(r.existing))


def child_named(parent: str, name: str, candidates) -> Optional[str]:
    """If ``name`` (inside existing dir ``parent``) is one of ``candidates`` under the
    parent's name rule, return the matching candidate."""
    ins = case_insensitive(parent)
    for c in candidates:
        if names_equal(name, c, ins):
            return c
    return None
