"""Extract valid top-level JSON fields without repairing malformed documents."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def extract_top_level_field(path: Path, field: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    decoder = json.JSONDecoder()
    depth = 0
    index = 0
    while index < len(text):
        character = text[index]
        if character == '"':
            try:
                value, end = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                return None
            if depth == 1 and value == field:
                cursor = end
                while cursor < len(text) and text[cursor].isspace():
                    cursor += 1
                if cursor >= len(text) or text[cursor] != ":":
                    return None
                cursor += 1
                while cursor < len(text) and text[cursor].isspace():
                    cursor += 1
                try:
                    result, _ = decoder.raw_decode(text, cursor)
                except json.JSONDecodeError:
                    return None
                return result
            index = end
            continue
        if character in "{[":
            depth += 1
        elif character in "}]":
            depth = max(0, depth - 1)
        index += 1
    return None
