"""Frontmatter parsing utilities with optional PyYAML support."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - fallback to pure-Python parser
    yaml = None  # type: ignore[assignment]


@dataclass(slots=True)
class NoteRecord:
    """Represents a capture note parsed from disk."""

    path: Path
    frontmatter: Dict[str, Any]
    content: str
    raw_text: str


def _yaml_load(text: str) -> Dict[str, Any]:
    if yaml is None:
        return _fallback_parse(text)
    if not text.strip():
        return {}
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Frontmatter must be a mapping.")
    return data


def _parse_scalar(token: str) -> Any:
    token = token.strip()
    if token in {"", "~", "null", "Null", "NULL"}:
        return None
    if token.lower() in {"true", "false"}:
        return token.lower() == "true"
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1]
    if token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    if re.fullmatch(r"-?\d+", token):
        try:
            return int(token)
        except ValueError:
            pass
    if re.fullmatch(r"-?\d+\.\d+", token):
        try:
            return float(token)
        except ValueError:
            pass
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in inner.split(",")]
    if token.startswith("{") and token.endswith("}"):
        inner = token[1:-1].strip()
        if not inner:
            return {}
        result: Dict[str, Any] = {}
        for part in inner.split(","):
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            result[key.strip()] = _parse_scalar(value.strip())
        return result
    return token


def _parse_block(lines: List[str], start: int, indent: int) -> Tuple[Dict[str, Any], int]:
    mapping: Dict[str, Any] = {}
    index = start
    total = len(lines)

    while index < total:
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        current_indent = len(line) - len(line.lstrip(" "))
        if current_indent < indent:
            break
        if ":" not in stripped:
            break
        key_part, value_part = line.split(":", 1)
        key = key_part.strip()
        value = value_part.strip()
        index += 1
        if value:
            mapping[key] = _parse_scalar(value)
            continue
        next_indent = indent + 2
        seq, new_index = _parse_sequence(lines, index, next_indent)
        if seq is not None:
            mapping[key] = seq
            index = new_index
            continue
        nested, new_index = _parse_block(lines, index, next_indent)
        mapping[key] = nested
        index = new_index

    return mapping, index


def _parse_sequence(lines: List[str], start: int, indent: int) -> Tuple[Optional[List[Any]], int]:
    items: List[Any] = []
    index = start
    total = len(lines)
    consumed = False

    while index < total:
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        current_indent = len(line) - len(line.lstrip(" "))
        if current_indent < indent:
            break
        if not stripped.startswith("- "):
            break
        consumed = True
        item_value = stripped[2:].strip()
        index += 1
        if item_value:
            items.append(_parse_scalar(item_value))
            continue
        next_indent = current_indent + 2
        seq, new_index = _parse_sequence(lines, index, next_indent)
        if seq is not None:
            items.append(seq)
            index = new_index
            continue
        nested, new_index = _parse_block(lines, index, next_indent)
        items.append(nested)
        index = new_index

    if not consumed:
        return None, start
    return items, index


def _fallback_parse(text: str) -> Dict[str, Any]:
    lines = text.splitlines()
    mapping, _ = _parse_block(lines, 0, 0)
    return mapping


def read_note(path: Path) -> NoteRecord:
    raw = path.read_text(encoding="utf-8")
    frontmatter: Dict[str, Any] = {}
    content = raw

    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            _, fm_text, body = parts
            frontmatter = _yaml_load(fm_text)
            content = body.lstrip("\n")

    return NoteRecord(
        path=path,
        frontmatter=frontmatter,
        content=content,
        raw_text=raw,
    )
