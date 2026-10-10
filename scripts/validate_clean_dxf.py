#!/usr/bin/env python3
"""Fail when a cleaned DXF still contains dashed geometry or suspect reference labels."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import ezdxf


FIGURE_LABEL_RE = re.compile(r"^\s*(?:图|附图)\s*\d+\s*$", re.IGNORECASE)
NUMERIC_TOKEN_RE = re.compile(r"(?<![A-Za-z])\d{2,}(?![A-Za-z])")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--allow-number", action="append", default=[])
    parser.add_argument("--json", dest="json_path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve()
    allowed = {str(value) for value in args.allow_number}
    if not source.is_file():
        raise SystemExit(f"Input DXF not found: {source}")
    doc = ezdxf.readfile(source)
    suspect_text = []
    text_count = 0

    for block in doc.blocks:
        for entity in block:
            if entity.dxftype() not in {"TEXT", "MTEXT"}:
                continue
            text_count += 1
            value = entity.plain_text() if entity.dxftype() == "MTEXT" else entity.dxf.text
            tokens = [token for token in NUMERIC_TOKEN_RE.findall(value) if token not in allowed]
            if tokens or FIGURE_LABEL_RE.fullmatch(value):
                suspect_text.append(
                    {
                        "space": block.name,
                        "handle": entity.dxf.handle or "",
                        "text": value,
                        "numeric_tokens": tokens,
                    }
                )

    noncontinuous_entities = []
    entity_count = 0
    for entity in doc.entitydb.values():
        if not getattr(entity, "is_alive", False):
            continue
        try:
            if not entity.dxf.is_supported("linetype"):
                continue
            entity_count += 1
            linetype = str(entity.dxf.get("linetype", "BYLAYER"))
            if linetype.upper() not in {"CONTINUOUS", "BYLAYER", "BYBLOCK"}:
                noncontinuous_entities.append(
                    {"handle": entity.dxf.handle or "", "linetype": linetype}
                )
        except (AttributeError, ezdxf.DXFError):
            pass

    noncontinuous_layers = [
        {"layer": layer.dxf.name, "linetype": layer.dxf.linetype}
        for layer in doc.layers
        if str(layer.dxf.linetype).upper() != "CONTINUOUS"
    ]
    report = {
        "input": str(source),
        "entity_count": entity_count,
        "text_count": text_count,
        "suspect_text_count": len(suspect_text),
        "noncontinuous_entity_count": len(noncontinuous_entities),
        "noncontinuous_layer_count": len(noncontinuous_layers),
        "suspect_text": suspect_text,
        "noncontinuous_entities": noncontinuous_entities,
        "noncontinuous_layers": noncontinuous_layers,
    }
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    failed = (
        entity_count == 0
        or bool(suspect_text)
        or bool(noncontinuous_entities)
        or bool(noncontinuous_layers)
    )
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
