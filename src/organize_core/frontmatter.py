"""THE single frontmatter parse/serialize module (spec 03 §8, 09 §2).

Every reader and writer in the system — index, fileops, consumers, metadata
editing — goes through this module. There is exactly one parser and one
serializer; the original shipped three divergent parsers and a whitelist
serializer that destroyed unknown fields (08 §A12, the worst data-loss bug).

Contract (spec 03 §8, 02 "quirks"):

- **Extraction is line-based**: frontmatter exists iff the file's first line
  is ``---``; it runs to the next line that is exactly ``---``. Never a
  substring split (08 §B16: ``---`` inside YAML values / horizontal rules).
- **Parsing tolerates every real-world quirk** (02): scalar vs list for
  tags/sources/aliases, ``metadata: {}`` vs ``[]`` vs missing, ``context``
  string or list, nested maps (``location``), quoted/unquoted scalars,
  missing/partial/absent frontmatter, invalid UTF-8 (decode with
  ``errors="replace"``), broken YAML (raises FrontmatterError — the INDEXER
  catches it and indexes with empty metadata, 03 §7; mutating ops refuse).
- **Serialization is round-trip safe**: known fields render in canonical
  order (KNOWN_FIELD_ORDER); ALL unknown fields are preserved after them in
  their original relative order; a populated field never changes type; list
  formatting style is preserved per field where feasible (03 §8).
- The acceptance bar is the property test: parse→serialize over the fixture
  corpus is lossless for every field, known or unknown (05 §9).

PyYAML is the primary parser with a stdlib fallback (06 §1: PyYAML optional).
Timestamps stay STRINGS — never let a YAML loader coerce them to datetime
(the capture app writes quoted ISO strings; ``capture_query.py``'s
timestamp-as-string loader behavior is ported here, 06 §4).

SHARED FILE — only the architect/integrator edits this module's signatures.

Implementation notes (round-trip mechanics)
-------------------------------------------

Byte-level round-tripping cannot be had from a YAML emitter, so :func:`parse`
records the *source text of every top-level field* (its "chunk": the key line
plus every continuation/comment/blank line up to the next key) together with a
deep snapshot of the parsed values. :func:`serialize` re-emits a field's chunk
verbatim when its value is unchanged and only runs the emitter for fields the
caller actually touched. Consequence: comments, quoting, indentation, blank
lines, CRLF endings and field order all survive untouched fields exactly.

**Field ordering** (spec 03 §8 vs the byte-level round-trip law): fields that
came from the source keep their source position — reordering an existing
user's file is churn the round-trip law forbids, and 03 §8's ordering rule
exists to describe what the *writer* emits. Fields the caller ADDS are placed
by KNOWN_FIELD_ORDER (a new known field lands in canonical position among the
known fields already present; a new unknown field is appended last), so a
document built from scratch renders in exactly the canonical order.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import FrontmatterError

try:  # PyYAML is optional (06 §1); the stdlib fallback below covers its absence.
    import yaml as _yaml
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    _yaml = None

# Canonical render order for known capture-schema fields (spec 03 §8).
KNOWN_FIELD_ORDER: tuple[str, ...] = (
    "timestamp",
    "id",
    "aliases",
    "capture_id",
    "tags",
    "sources",
    "modalities",
    "context",
    "location",
    "metadata",
    "processing_status",
    "created_date",
    "last_edited_date",
)

_KNOWN_INDEX: dict[str, int] = {name: i for i, name in enumerate(KNOWN_FIELD_ORDER)}

DELIMITER = "---"

_PARSE_HINT = (
    "Fix the YAML frontmatter block by hand (the file is otherwise untouched); "
    "organize refuses to rewrite a note whose frontmatter it cannot round-trip."
)

# Emitter width: never fold long scalars onto continuation lines.
_EMIT_WIDTH = 1_000_000

_PLAIN_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-/]*")
_RESERVED_PLAIN = frozenset(
    {"true", "false", "null", "yes", "no", "on", "off", "y", "n", "none", "~"}
)


if _yaml is not None:  # pragma: no branch - trivial

    class _FrontmatterLoader(_yaml.SafeLoader):
        """SafeLoader that keeps timestamp-like scalars as STRINGS (06 §4).

        The implicit-resolver table is rebuilt as a *new* class attribute —
        mutating the inherited one would corrupt ``yaml.SafeLoader`` for every
        other user in the process.
        """

    _FrontmatterLoader.yaml_implicit_resolvers = {
        ch: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
        for ch, resolvers in _yaml.SafeLoader.yaml_implicit_resolvers.items()
    }

    class _FrontmatterDumper(_yaml.SafeDumper):
        """SafeDumper with the standard resolver table intact, so a date-like
        *string* (``'2026-06-10'``) is emitted quoted and survives a re-read by
        tools that do resolve timestamps (Obsidian, the capture app)."""

        def ignore_aliases(self, data: Any) -> bool:  # noqa: D102 - never emit &anchors
            return True


@dataclass
class Frontmatter:
    """Parsed frontmatter plus the style state needed to round-trip.

    ``fields`` preserves insertion order (dict ordering) of the source file.
    ``style`` is private per-field formatting hints (quote style, block vs
    inline list, empty-map vs empty-list) — implementation-defined, but the
    round-trip property test is the contract.
    """

    fields: dict[str, Any] = field(default_factory=dict)
    style: dict[str, Any] = field(default_factory=dict)

    def get_list(self, key: str) -> list[Any]:
        """Field coerced to list: scalar ⇒ one-element list, missing ⇒ []
        (spec 02 quirks; 03 §7 coercion). Never mutates ``fields``."""
        if key not in self.fields:
            return []
        value = self.fields[key]
        if value is None:
            return []
        if isinstance(value, list):
            return list(value)
        if isinstance(value, tuple):
            return list(value)
        return [value]


@dataclass
class Document:
    """One note = optional frontmatter + body.

    ``has_frontmatter`` distinguishes "no block at all" from "empty block";
    serialization must not invent a block that wasn't there unless fields
    were added.
    """

    frontmatter: Frontmatter | None
    body: str

    @property
    def has_frontmatter(self) -> bool:
        return self.frontmatter is not None


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Line-based split: ``(frontmatter_text or None, body)``.

    First line must be exactly ``---`` (trailing whitespace tolerated);
    terminator is the next such line. No terminator ⇒ treat the whole file
    as body (malformed block is not silently eaten). Never crashes.
    """
    opened, fm_text, _closed, body = _split_lines(text)
    if opened is None:
        return None, body
    return fm_text, body


def parse(text: str) -> Document:
    """Parse a full note's text. Broken YAML inside a well-delimited block
    raises FrontmatterError; everything else (no block, empty block, quirky
    values) parses. The parser never mutates values (no datetime coercion,
    no list normalization — normalization is the caller's explicit choice
    via :meth:`Frontmatter.get_list`)."""
    opened, fm_text, closed, body = _split_lines(text)
    if opened is None:
        return Document(frontmatter=None, body=body)
    fields, key_lines = _parse_mapping(fm_text)
    style = _build_style(opened, fm_text, closed, fields, key_lines)
    return Document(frontmatter=Frontmatter(fields=fields, style=style), body=body)


def serialize(doc: Document) -> str:
    """Render a document back to text, round-trip safe per module contract.
    ``serialize(parse(text)) == text`` must hold for every corpus file whose
    fields were not modified (byte-level target; at minimum value-lossless
    with stable field order and no type changes)."""
    fm = doc.frontmatter
    body = doc.body
    if fm is None:
        return body

    style = fm.style if isinstance(fm.style, dict) else {}
    open_line = style.get("open", DELIMITER + "\n")
    close_line = style.get("close", DELIMITER + "\n")
    original = style.get("text")
    snapshot = style.get("snapshot", {})
    raw: dict[str, str] = style.get("raw", {})
    hints: dict[str, dict[str, Any]] = style.get("hints", {})
    prefix = style.get("prefix", "")

    if not fm.fields:
        if original is None:
            # Constructed document with nothing in it: never invent a block.
            return body
        if not snapshot:
            return open_line + original + close_line + body
        # Caller emptied a real block — keep the block (and its comments).
        return open_line + prefix + close_line + body

    if original is not None and _same(fm.fields, snapshot):
        return open_line + original + close_line + body

    if original is not None and not style.get("chunkable", True):
        # Source could not be chunked safely (duplicate/same-line keys) and
        # something changed: re-emit the whole block rather than risk splicing.
        rendered = "".join(_dump_field(k, fm.fields[k], {}) for k in _ordered(fm.fields, style))
        return open_line + rendered + close_line + body

    parts = [prefix]
    for key in _ordered(fm.fields, style):
        value = fm.fields[key]
        if key in raw and key in snapshot and _same(value, snapshot[key]):
            parts.append(raw[key])
        else:
            parts.append(_dump_field(key, value, hints.get(key, {})))
    return open_line + "".join(parts) + close_line + body


def load_file(path: Path) -> Document:
    """Read + parse a note file. Decodes UTF-8 with ``errors="replace"``
    (06 §6 — invalid bytes must not crash a scan). I/O errors propagate."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        return parse(text)
    except FrontmatterError as exc:
        # Loud, with the file path — never a silent skip (09 §1.5).
        raise FrontmatterError(
            f"{path}: {exc}", hint=exc.hint or _PARSE_HINT
        ) from exc


# --- normalization + merge helpers (single home for these rules) -----------


def normalize_tag(value: str, extra_map: dict[str, str] | None = None) -> str:
    """THE shared normalizer (spec 04 §1): lowercase, trim, spaces and
    underscores → hyphens; then apply the ``tag_normalization`` config map
    (e.g. ``project`` → ``projects``). Used by candidates, tags, sources."""
    normalized = _basic_normalize(value)
    if extra_map:
        mapped = extra_map.get(normalized)
        if mapped is not None:
            # Re-normalize the mapping's output so a sloppy config entry can
            # never produce a token that fails to match a normalized folder.
            return _basic_normalize(mapped)
    return normalized


def merge_tags(existing: list[str], new: list[str]) -> list[str]:
    """Case-insensitive dedupe that PRESERVES existing order and casing,
    appending genuinely-new tags at the end (spec 05 §2.6 — the original
    re-sorted and re-cased the user's list, 08 §A24)."""
    merged: list[str] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(new or []):
        text = item if isinstance(item, str) else str(item)
        key = text.strip().casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append(text)
    return merged


def merge_sources(existing: list[str], new: list[str]) -> list[str]:
    """Exact-string union, target-first order (spec 05 §4 merge rule)."""
    merged: list[str] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(new or []):
        text = item if isinstance(item, str) else str(item)
        if text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged


def is_no_ai(doc: Document) -> bool:
    """True iff frontmatter contains ``no-ai: true`` (vault law, spec 02).
    Every automated/LLM write path must check this before touching a file."""
    fm = doc.frontmatter
    if fm is None:
        return False
    for key, value in fm.fields.items():
        if not isinstance(key, str):
            continue
        if key.strip().lower().replace("_", "-") != "no-ai":
            continue
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "on", "1"}
        return bool(value) and not isinstance(value, (list, dict))
    return False


# --- internals -------------------------------------------------------------


def _split_lines(text: str) -> tuple[str | None, str, str, str]:
    """``(open_line, frontmatter_text, close_line, body)``.

    ``open_line`` is None when there is no well-delimited block, in which case
    the whole text is the body (08 §B16: never a substring split, so a ``---``
    horizontal rule in the body can never be mistaken for a delimiter).
    """
    if not text:
        return None, "", "", text
    lines = text.splitlines(keepends=True)
    if lines[0].rstrip() != DELIMITER:
        return None, "", "", text
    for idx in range(1, len(lines)):
        if lines[idx].rstrip() == DELIMITER:
            return lines[0], "".join(lines[1:idx]), lines[idx], "".join(lines[idx + 1 :])
    return None, "", "", text


def _parse_mapping(fm_text: str) -> tuple[dict[str, Any], list[tuple[Any, int]]]:
    """``(fields, [(key, 0-based start line within fm_text), ...])``."""
    if _yaml is not None:
        return _parse_with_pyyaml(fm_text)
    return _parse_fallback(fm_text)


def _parse_with_pyyaml(fm_text: str) -> tuple[dict[str, Any], list[tuple[Any, int]]]:
    try:
        data = _yaml.load(fm_text, Loader=_FrontmatterLoader)
        node = _yaml.compose(fm_text, Loader=_FrontmatterLoader)
    except _yaml.YAMLError as exc:
        raise FrontmatterError(
            f"unparseable YAML frontmatter: {_one_line(exc)}", hint=_PARSE_HINT
        ) from exc
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        raise FrontmatterError(
            f"frontmatter must be a YAML mapping, got {type(data).__name__}",
            hint=_PARSE_HINT,
        )
    key_lines: list[tuple[Any, int]] = []
    if node is not None and isinstance(node, _yaml.MappingNode):
        constructor = _FrontmatterLoader("")
        for key_node, _value_node in node.value:
            try:
                key = constructor.construct_object(key_node, deep=True)
            except _yaml.YAMLError:  # pragma: no cover - yaml.load would have raised
                continue
            try:
                hash(key)
            except TypeError:  # pragma: no cover - yaml.load would have raised
                continue
            key_lines.append((key, key_node.start_mark.line))
    return data, key_lines


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())


def _build_style(
    open_line: str,
    fm_text: str,
    close_line: str,
    fields: dict[str, Any],
    key_lines: list[tuple[Any, int]],
) -> dict[str, Any]:
    lines = fm_text.splitlines(keepends=True)
    chunkable = True
    seen: set[Any] = set()
    previous = -1
    for key, line_no in key_lines:
        if key in seen or line_no <= previous or key not in fields:
            chunkable = False
        seen.add(key)
        previous = line_no
    if len(key_lines) != len(fields):
        chunkable = False

    prefix = "".join(lines[: key_lines[0][1]]) if key_lines else fm_text
    raw: dict[str, str] = {}
    hints: dict[str, dict[str, Any]] = {}
    if chunkable:
        for idx, (key, line_no) in enumerate(key_lines):
            end = key_lines[idx + 1][1] if idx + 1 < len(key_lines) else len(lines)
            chunk = "".join(lines[line_no:end])
            raw[key] = chunk
            hints[key] = _hint_for(chunk)
        order = [key for key, _ in key_lines]
    else:
        order = list(fields)

    return {
        "open": open_line,
        "close": close_line,
        "text": fm_text,
        "prefix": prefix if chunkable else "",
        "raw": raw,
        "hints": hints,
        "order": order,
        "snapshot": copy.deepcopy(fields),
        "chunkable": chunkable,
    }


_KEY_HEAD_RE = re.compile(r"^\s*(?:'(?:[^']|'')*'|\"(?:\\.|[^\"])*\"|[^:#]*?)\s*:\s*(.*)$")


def _hint_for(chunk: str) -> dict[str, Any]:
    """Formatting hints used when a field's VALUE changed and must be re-emitted."""
    first = chunk.splitlines()[0] if chunk else ""
    match = _KEY_HEAD_RE.match(first)
    rest = match.group(1).strip() if match else ""
    hint: dict[str, Any] = {}
    if rest[:1] in ("[", "{"):
        hint["flow"] = True
    elif rest[:1] in ("'", '"'):
        hint["quote"] = rest[0]
    return hint


def _ordered(fields: dict[str, Any], style: dict[str, Any]) -> list[Any]:
    """Source order for pre-existing fields; KNOWN_FIELD_ORDER for new ones."""
    source_order = [key for key in style.get("order", []) if key in fields]
    result = list(source_order)
    seen = set(source_order)
    added = [key for key in fields if key not in seen]
    # New KNOWN fields land in canonical position; new UNKNOWN fields are
    # appended after everything else (spec 03 §8).
    for key in added:
        if isinstance(key, str) and key in _KNOWN_INDEX:
            result.insert(_insert_position(result, key), key)
    for key in added:
        if not (isinstance(key, str) and key in _KNOWN_INDEX):
            result.append(key)
    return result


def _insert_position(current: list[Any], key: Any) -> int:
    index = _KNOWN_INDEX.get(key) if isinstance(key, str) else None
    if index is None:
        return len(current)
    before = -1
    after: int | None = None
    for i, existing in enumerate(current):
        other = _KNOWN_INDEX.get(existing) if isinstance(existing, str) else None
        if other is None:
            continue
        if other < index:
            before = i
        elif after is None:
            after = i
    if before >= 0:
        return before + 1
    if after is not None:
        return after
    return len(current)


def _same(left: Any, right: Any) -> bool:
    """Deep equality that also requires identical types (so ``True`` never
    compares equal to ``1`` and a field's type change is always detected)."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if len(left) != len(right):
            return False
        for key, value in left.items():
            if key not in right or not _same(value, right[key]):
                return False
        return True
    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return False
        return all(_same(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, float):
        if left != left and right != right:  # NaN == NaN for round-trip purposes
            return True
    return bool(left == right)


def _plain_key(key: Any) -> bool:
    return (
        isinstance(key, str)
        and _PLAIN_KEY_RE.fullmatch(key) is not None
        and key.lower() not in _RESERVED_PLAIN
    )


def _dump_field(key: Any, value: Any, hint: dict[str, Any]) -> str:
    """Emit one top-level ``key: value`` chunk (always newline-terminated)."""
    if _plain_key(key):
        quote = hint.get("quote")
        if quote and isinstance(value, str) and "\n" not in value:
            if quote == "'":
                return f"{key}: '" + value.replace("'", "''") + "'\n"
            return f"{key}: " + json.dumps(value, ensure_ascii=False) + "\n"
        if hint.get("flow") and isinstance(value, (list, dict)) and value:
            return f"{key}: {_dump_flow(value)}\n"
    return _dump_block({key: value})


def _dump_flow(value: Any) -> str:
    if _yaml is not None:
        text = _yaml.dump(
            value,
            Dumper=_FrontmatterDumper,
            default_flow_style=True,
            allow_unicode=True,
            sort_keys=False,
            width=_EMIT_WIDTH,
        )
        return text.rstrip("\n")
    return _fb_dump_flow(value)


def _dump_block(mapping: dict[Any, Any]) -> str:
    if _yaml is not None:
        return _yaml.dump(
            mapping,
            Dumper=_FrontmatterDumper,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=_EMIT_WIDTH,
        )
    return _fb_dump_block(mapping)


# --- stdlib fallback: emitter ---------------------------------------------

_FB_NEEDS_QUOTE_RE = re.compile(
    r"""^$|^[\s]|[\s]$|^[-?:,\[\]{}#&*!|>'"%@`]|:\s|\s#|[\n\t]"""
)
_FB_INT_RE = re.compile(r"^[-+]?\d+$")
_FB_FLOAT_RE = re.compile(r"^[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?$")
_FB_BOOLISH = {
    "true",
    "false",
    "yes",
    "no",
    "on",
    "off",
    "null",
    "none",
    "~",
    ".inf",
    "-.inf",
    "+.inf",
    ".nan",
}
# YAML 1.1 timestamp shape — such STRINGS must be emitted quoted or a reader
# with the timestamp resolver enabled (Obsidian, the capture app) turns them
# into dates (06 §4: timestamps stay strings).
_FB_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}(?:[Tt ].*)?$")


def _fb_scalar_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = value if isinstance(value, str) else str(value)
    needs_quote = (
        _FB_NEEDS_QUOTE_RE.search(text) is not None
        or text.lower() in _FB_BOOLISH
        or _FB_INT_RE.match(text) is not None
        or _FB_FLOAT_RE.match(text) is not None
        or _FB_TIMESTAMP_RE.match(text) is not None
    )
    if not needs_quote:
        return text
    if "\n" in text or "\\" in text:
        return json.dumps(text, ensure_ascii=False)
    return "'" + text.replace("'", "''") + "'"


def _fb_dump_flow(value: Any) -> str:
    if isinstance(value, dict):
        inner = ", ".join(f"{_fb_scalar_text(k)}: {_fb_dump_flow(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fb_dump_flow(v) for v in value) + "]"
    return _fb_scalar_text(value)


def _fb_dump_block(mapping: dict[Any, Any], indent: int = 0) -> str:
    out: list[str] = []
    pad = " " * indent
    for key, value in mapping.items():
        key_text = _fb_scalar_text(key)
        if isinstance(value, dict):
            if not value:
                out.append(f"{pad}{key_text}: {{}}\n")
            else:
                out.append(f"{pad}{key_text}:\n")
                out.append(_fb_dump_block(value, indent + 2))
        elif isinstance(value, (list, tuple)):
            if not value:
                out.append(f"{pad}{key_text}: []\n")
            else:
                out.append(f"{pad}{key_text}:\n")
                for item in value:
                    if isinstance(item, dict) and item:
                        nested = _fb_dump_block(item, indent + 2)
                        nested_lines = nested.splitlines(keepends=True)
                        first = nested_lines[0]
                        out.append(f"{pad}- " + first[indent + 2 :])
                        out.extend(nested_lines[1:])
                    elif isinstance(item, (list, tuple, dict)):
                        out.append(f"{pad}- {_fb_dump_flow(item)}\n")
                    else:
                        out.append(f"{pad}- {_fb_scalar_text(item)}\n")
        else:
            out.append(f"{pad}{key_text}: {_fb_scalar_text(value)}\n")
    return "".join(out)


# --- stdlib fallback: parser ----------------------------------------------


def _parse_fallback(fm_text: str) -> tuple[dict[str, Any], list[tuple[Any, int]]]:
    lines = fm_text.splitlines()
    start = _fb_next_significant(lines, 0)
    if start >= len(lines):
        return {}, []
    indent = _fb_indent(lines[start])
    content = _fb_strip_comment(lines[start].strip())
    if content.startswith("- ") or content == "-" or content.startswith("["):
        raise FrontmatterError(
            "frontmatter must be a YAML mapping, got list", hint=_PARSE_HINT
        )
    if content.startswith("{"):
        text, _ = _fb_collect_flow(list(lines), start, content)
        mapping = _fb_flow(text)
        if not isinstance(mapping, dict):
            raise FrontmatterError(
                "frontmatter must be a YAML mapping", hint=_PARSE_HINT
            )
        # Every key shares one line: not chunkable, which _build_style detects.
        return mapping, [(key, start) for key in mapping]
    key_lines: list[tuple[Any, int]] = []
    fields, _ = _fb_map(list(lines), start, indent, key_lines)
    return fields, key_lines


def _fb_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _fb_next_significant(lines: list[str], i: int) -> int:
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            return i
        i += 1
    return len(lines)


def _fb_strip_comment(text: str) -> str:
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if quote == "'" and ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                out.append(ch)
                i += 1
                if i < len(text):
                    out.append(text[i])
                    i += 1
                continue
            elif quote == '"' and ch == '"':
                quote = None
            out.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _fb_split_key(content: str) -> tuple[Any, str]:
    quote: str | None = None
    i = 0
    while i < len(content):
        ch = content[i]
        if quote is not None:
            if quote == "'" and ch == "'":
                if i + 1 < len(content) and content[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                i += 2
                continue
            elif quote == '"' and ch == '"':
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == ":" and (i + 1 >= len(content) or content[i + 1] in " \t"):
            return _fb_scalar(content[:i]), content[i + 1 :]
        i += 1
    raise FrontmatterError(
        f"unparseable YAML frontmatter: expected 'key: value', got {content!r}",
        hint=_PARSE_HINT,
    )


def _fb_map(
    lines: list[str], i: int, indent: int, key_lines: list[tuple[Any, int]] | None = None
) -> tuple[dict[Any, Any], int]:
    obj: dict[Any, Any] = {}
    while True:
        i = _fb_next_significant(lines, i)
        if i >= len(lines):
            break
        line_indent = _fb_indent(lines[i])
        if line_indent < indent:
            break
        content = _fb_strip_comment(lines[i].strip())
        if line_indent > indent or content.startswith("- ") or content == "-":
            raise FrontmatterError(
                f"unparseable YAML frontmatter: unexpected line {lines[i]!r}",
                hint=_PARSE_HINT,
            )
        key, rest = _fb_split_key(content)
        if key_lines is not None:
            key_lines.append((key, i))
        value, i = _fb_value(lines, i, indent, rest)
        obj[key] = value
    return obj, i


def _fb_seq(lines: list[str], i: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while True:
        i = _fb_next_significant(lines, i)
        if i >= len(lines):
            break
        if _fb_indent(lines[i]) != indent:
            break
        content = _fb_strip_comment(lines[i].strip())
        if content != "-" and not content.startswith("- "):
            break
        rest = content[1:].strip()
        if rest and _fb_looks_like_key(rest):
            # "- key: value" is a mapping starting at column indent+2.
            lines[i] = " " * (indent + 1) + lines[i].lstrip(" ")[1:]
            value, i = _fb_map(lines, i, indent + 2)
            items.append(value)
            continue
        if rest.startswith("[") or rest.startswith("{"):
            items.append(_fb_flow(rest))
            i += 1
            continue
        if rest:
            items.append(_fb_scalar(rest))
            i += 1
            continue
        nested = _fb_next_significant(lines, i + 1)
        if nested < len(lines) and _fb_indent(lines[nested]) > indent:
            nested_indent = _fb_indent(lines[nested])
            nested_content = _fb_strip_comment(lines[nested].strip())
            if nested_content.startswith("- ") or nested_content == "-":
                value, i = _fb_seq(lines, nested, nested_indent)
            else:
                value, i = _fb_map(lines, nested, nested_indent)
            items.append(value)
            continue
        items.append(None)
        i += 1
    return items, i


def _fb_looks_like_key(rest: str) -> bool:
    try:
        _fb_split_key(rest)
    except FrontmatterError:
        return False
    return True


def _fb_value(lines: list[str], i: int, indent: int, rest: str) -> tuple[Any, int]:
    rest = rest.strip()
    if rest[:1] in ("|", ">"):
        return _fb_block_scalar(lines, i, indent, rest)
    if rest[:1] in ("[", "{"):
        text, nxt = _fb_collect_flow(lines, i, rest)
        return _fb_flow(text), nxt
    if rest:
        return _fb_scalar(rest), i + 1
    nxt = _fb_next_significant(lines, i + 1)
    if nxt >= len(lines):
        return None, i + 1
    next_indent = _fb_indent(lines[nxt])
    next_content = _fb_strip_comment(lines[nxt].strip())
    if next_content.startswith("- ") or next_content == "-":
        if next_indent >= indent:
            return _fb_seq(lines, nxt, next_indent)
        return None, i + 1
    if next_indent > indent:
        return _fb_map(lines, nxt, next_indent)
    return None, i + 1


def _fb_block_scalar(lines: list[str], i: int, indent: int, rest: str) -> tuple[str, int]:
    style = rest[0]
    chomp = "strip" if "-" in rest[1:] else ("keep" if "+" in rest[1:] else "clip")
    collected: list[str] = []
    block_indent: int | None = None
    j = i + 1
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            collected.append("")
            j += 1
            continue
        line_indent = _fb_indent(line)
        if line_indent <= indent:
            break
        if block_indent is None:
            block_indent = line_indent
        collected.append(line[block_indent:] if len(line) > block_indent else "")
        j += 1
    while collected and collected[-1] == "":
        collected.pop()
    if style == "|":
        text = "\n".join(collected)
    else:
        folded: list[str] = []
        for piece in collected:
            if piece == "":
                folded.append("\n")
            elif folded and not folded[-1].endswith("\n"):
                folded.append(" " + piece)
            else:
                folded.append(piece)
        text = "".join(folded)
    if chomp != "strip" and text:
        text += "\n"
    return text, j


def _fb_flow_depth(text: str) -> int:
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if quote == "'" and ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
            elif quote == '"' and ch == "\\":
                i += 2
                continue
            elif quote == '"' and ch == '"':
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth


def _fb_collect_flow(lines: list[str], i: int, rest: str) -> tuple[str, int]:
    """A flow collection may wrap across lines; consume until brackets balance."""
    text = rest
    j = i + 1
    while _fb_flow_depth(text) > 0 and j < len(lines):
        text += " " + _fb_strip_comment(lines[j].strip())
        j += 1
    if _fb_flow_depth(text) != 0:
        raise FrontmatterError(
            f"unparseable YAML frontmatter: unterminated flow collection in {rest!r}",
            hint=_PARSE_HINT,
        )
    return text, j


def _fb_flow(text: str) -> Any:
    value, _ = _fb_flow_parse(text, 0)
    return value


def _fb_flow_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _fb_flow_parse(text: str, i: int) -> tuple[Any, int]:
    i = _fb_flow_ws(text, i)
    if i >= len(text):
        return None, i
    if text[i] == "[":
        items: list[Any] = []
        i += 1
        while True:
            i = _fb_flow_ws(text, i)
            if i >= len(text):
                break
            if text[i] == "]":
                i += 1
                break
            value, i = _fb_flow_parse(text, i)
            items.append(value)
            i = _fb_flow_ws(text, i)
            if i < len(text) and text[i] == ",":
                i += 1
        return items, i
    if text[i] == "{":
        obj: dict[Any, Any] = {}
        i += 1
        while True:
            i = _fb_flow_ws(text, i)
            if i >= len(text):
                break
            if text[i] == "}":
                i += 1
                break
            key, i = _fb_flow_parse(text, i)
            i = _fb_flow_ws(text, i)
            if i < len(text) and text[i] == ":":
                i += 1
            i = _fb_flow_ws(text, i)
            if i < len(text) and text[i] not in ",}":
                value, i = _fb_flow_parse(text, i)
            else:
                value = None
            try:
                obj[key] = value
            except TypeError:  # pragma: no cover - unhashable flow key
                obj[str(key)] = value
            i = _fb_flow_ws(text, i)
            if i < len(text) and text[i] == ",":
                i += 1
        return obj, i
    start = i
    if text[i] in "'\"":
        quote = text[i]
        i += 1
        while i < len(text):
            if quote == "'" and text[i] == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                i += 1
                break
            if quote == '"' and text[i] == "\\":
                i += 2
                continue
            if quote == '"' and text[i] == '"':
                i += 1
                break
            i += 1
        return _fb_scalar(text[start:i]), i
    while i < len(text) and text[i] not in ",]}":
        # In flow context a ':' followed by a separator ends a plain key.
        if text[i] == ":" and (i + 1 >= len(text) or text[i + 1] in " \t,}]"):
            break
        i += 1
    return _fb_scalar(text[start:i]), i


def _fb_scalar(text: str) -> Any:
    text = text.strip()
    if text == "" or text in ("~", "null", "Null", "NULL"):
        return None
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            return json.loads(text)
        except ValueError:
            return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if _FB_INT_RE.match(text):
        try:
            return int(text)
        except ValueError:  # pragma: no cover - regex guarantees parse
            pass
    if _FB_FLOAT_RE.match(text):
        try:
            return float(text)
        except ValueError:  # pragma: no cover - regex guarantees parse
            pass
    return text


def _basic_normalize(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    text = text.strip().lower()
    return text.replace(" ", "-").replace("_", "-")
