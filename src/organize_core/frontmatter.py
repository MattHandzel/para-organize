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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
        raise NotImplementedError


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
    raise NotImplementedError


def parse(text: str) -> Document:
    """Parse a full note's text. Broken YAML inside a well-delimited block
    raises FrontmatterError; everything else (no block, empty block, quirky
    values) parses. The parser never mutates values (no datetime coercion,
    no list normalization — normalization is the caller's explicit choice
    via :meth:`Frontmatter.get_list`)."""
    raise NotImplementedError


def serialize(doc: Document) -> str:
    """Render a document back to text, round-trip safe per module contract.
    ``serialize(parse(text)) == text`` must hold for every corpus file whose
    fields were not modified (byte-level target; at minimum value-lossless
    with stable field order and no type changes)."""
    raise NotImplementedError


def load_file(path: Path) -> Document:
    """Read + parse a note file. Decodes UTF-8 with ``errors="replace"``
    (06 §6 — invalid bytes must not crash a scan). I/O errors propagate."""
    raise NotImplementedError


# --- normalization + merge helpers (single home for these rules) -----------


def normalize_tag(value: str, extra_map: dict[str, str] | None = None) -> str:
    """THE shared normalizer (spec 04 §1): lowercase, trim, spaces and
    underscores → hyphens; then apply the ``tag_normalization`` config map
    (e.g. ``project`` → ``projects``). Used by candidates, tags, sources."""
    raise NotImplementedError


def merge_tags(existing: list[str], new: list[str]) -> list[str]:
    """Case-insensitive dedupe that PRESERVES existing order and casing,
    appending genuinely-new tags at the end (spec 05 §2.6 — the original
    re-sorted and re-cased the user's list, 08 §A24)."""
    raise NotImplementedError


def merge_sources(existing: list[str], new: list[str]) -> list[str]:
    """Exact-string union, target-first order (spec 05 §4 merge rule)."""
    raise NotImplementedError


def is_no_ai(doc: Document) -> bool:
    """True iff frontmatter contains ``no-ai: true`` (vault law, spec 02).
    Every automated/LLM write path must check this before touching a file."""
    raise NotImplementedError
