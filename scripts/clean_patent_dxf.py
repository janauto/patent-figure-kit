#!/usr/bin/env python3
"""Clean patent reference labels and force DXF geometry to continuous lines."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import ezdxf


FIGURE_LABEL_RE = re.compile(r"^\s*(?:图|附图)\s*\d+\s*$", re.IGNORECASE)
STANDALONE_CANDIDATE_RE = re.compile(
    r"^\s*[（(]?\d{2,}[A-Za-z]?[）)]?(?:\s*[、,，;；/]\s*[（(]?\d{2,}[A-Za-z]?[）)]?)*\s*$"
)
INLINE_CANDIDATE_RE = re.compile(r"(?<![A-Za-z])\d{2,}(?![A-Za-z])")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean a DXF copy without blindly deleting engineering numbers."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--reference-number",
        action="append",
        default=[],
        help="Approved patent reference number to remove; repeat as needed.",
    )
    parser.add_argument(
        "--reference-file",
        type=Path,
        help="UTF-8 file containing approved reference numbers separated by whitespace or punctuation.",
    )
    parser.add_argument(
        "--remove-standalone-candidates",
        action="store_true",
        help="Remove every standalone multi-digit candidate. Review --dry-run first.",
    )
    parser.add_argument(
        "--strip-selected-inline-references",
        action="store_true",
        help="Also remove only the approved numbers when embedded in longer labels.",
    )
    parser.add_argument(
        "--remove-figure-labels",
        action="store_true",
        help="Remove captions such as 图1 or 附图1; they are preserved by default.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def load_approved(args: argparse.Namespace) -> set[str]:
    values = {str(value).strip() for value in args.reference_number if str(value).strip()}
    if args.reference_file:
        raw = args.reference_file.read_text(encoding="utf-8")
        values.update(token for token in re.split(r"[\s,，、;；]+", raw) if token)
    return values


def get_text(entity) -> str | None:
    if entity.dxftype() == "TEXT":
        return entity.dxf.text
    if entity.dxftype() == "MTEXT":
        return entity.text
    return None


def set_text(entity, value: str) -> None:
    if entity.dxftype() == "TEXT":
        entity.dxf.text = value
    else:
        entity.text = value


def cleanup_punctuation(value: str) -> str:
    value = re.sub(r"\s*[、，,;；]+\s*", "、", value)
    value = re.sub(r"、+", "、", value)
    value = re.sub(r"[（(]\s*[）)]", "", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" 、，,;；")


def selected_pattern(approved: set[str]) -> re.Pattern[str] | None:
    if not approved:
        return None
    choices = "|".join(re.escape(value) for value in sorted(approved, key=len, reverse=True))
    return re.compile(rf"(?<![A-Za-z0-9])(?:{choices})(?![A-Za-z0-9])")


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if source == output:
        raise SystemExit("Refusing to overwrite the source DXF; choose a new output path.")
    if output.exists() and not args.overwrite and not args.dry_run:
        raise SystemExit(f"Output exists: {output}. Pass --overwrite to replace it.")

    approved = load_approved(args)
    inline_re = selected_pattern(approved)
    if not source.is_file():
        raise SystemExit(f"Input DXF not found: {source}")
    doc = ezdxf.readfile(source)
    removed: list[dict[str, str]] = []
    changed: list[dict[str, str]] = []
    candidates: list[dict[str, str]] = []

    for block in doc.blocks:
        for entity in list(block):
            value = get_text(entity)
            if value is None:
                continue
            plain = entity.plain_text() if entity.dxftype() == "MTEXT" else value
            item = {
                "space": block.name,
                "handle": entity.dxf.handle or "",
                "type": entity.dxftype(),
                "text": plain,
            }
            is_figure = bool(FIGURE_LABEL_RE.fullmatch(plain))
            is_standalone = bool(STANDALONE_CANDIDATE_RE.fullmatch(plain))
            exact_selected = plain.strip(" （()）") in approved
            if is_standalone or INLINE_CANDIDATE_RE.search(plain):
                candidates.append(item)

            if (is_figure and args.remove_figure_labels) or exact_selected or (
                is_standalone and args.remove_standalone_candidates
            ):
                removed.append(item)
                if not args.dry_run:
                    block.delete_entity(entity)
                continue

            if args.strip_selected_inline_references and inline_re:
                cleaned = cleanup_punctuation(inline_re.sub("", value))
                if cleaned != value:
                    changed.append({**item, "new_text": cleaned})
                    if not args.dry_run:
                        if cleaned:
                            set_text(entity, cleaned)
                        else:
                            block.delete_entity(entity)

    noncontinuous_before = []
    for entity in doc.entitydb.values():
        if not getattr(entity, "is_alive", False):
            continue
        try:
            if entity.dxf.is_supported("linetype"):
                current = str(entity.dxf.get("linetype", "BYLAYER"))
                if current.upper() not in {"CONTINUOUS", "BYLAYER", "BYBLOCK"}:
                    noncontinuous_before.append(
                        {"handle": entity.dxf.handle or "", "linetype": current}
                    )
                if not args.dry_run:
                    entity.dxf.linetype = "CONTINUOUS"
        except (AttributeError, ezdxf.DXFError):
            pass

    for layer in doc.layers:
        if str(layer.dxf.linetype).upper() != "CONTINUOUS":
            noncontinuous_before.append({"layer": layer.dxf.name, "linetype": layer.dxf.linetype})
        if not args.dry_run:
            layer.dxf.linetype = "CONTINUOUS"

    report = {
        "input": str(source),
        "output": None if args.dry_run else str(output),
        "dry_run": args.dry_run,
        "approved_reference_numbers": sorted(approved),
        "candidate_count": len(candidates),
        "removed_count": len(removed),
        "changed_inline_count": len(changed),
        "noncontinuous_before_count": len(noncontinuous_before),
        "candidates": candidates,
        "removed": removed,
        "changed_inline": changed,
        "noncontinuous_before": noncontinuous_before,
    }

    if not args.dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        doc.saveas(output)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
