"""Helpers for turning capture notes into hashed payloads."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Tuple

from .frontmatter import NoteRecord, read_note

LEGACY_DAILY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\.md")


def _compute_hash(raw_text: str) -> str:
    digest = hashlib.sha256()
    digest.update(raw_text.encode("utf-8"))
    return digest.hexdigest()


@dataclass(slots=True)
class NotePayload:
    """Normalized representation of a capture note."""

    path: Path
    frontmatter: Dict[str, object]
    content: str
    raw_text: str
    note_hash: str

    @property
    def tags(self) -> Tuple[str, ...]:
        tags = self.frontmatter.get("tags")
        if tags is None:
            return ()
        if isinstance(tags, (list, tuple, set)):
            return tuple(str(tag) for tag in tags)
        return (str(tags),)

    def has_tag(self, tag: str) -> bool:
        tag_lower = tag.lower()
        return any(entry.lower() == tag_lower for entry in self.tags)


def to_payload(note: NoteRecord) -> NotePayload:
    note_hash = _compute_hash(note.raw_text)
    return NotePayload(
        path=note.path,
        frontmatter=note.frontmatter,
        content=note.content,
        raw_text=note.raw_text,
        note_hash=note_hash,
    )


def iter_note_payloads(root: Path, capture_dir: Path) -> Iterator[NotePayload]:
    """Yield `NotePayload` objects for every Markdown file under capture_dir."""
    capture_dir = capture_dir if capture_dir.is_absolute() else (root / capture_dir)
    if not capture_dir.exists():
        raise FileNotFoundError(f"Capture directory not found: {capture_dir}")
    for path in sorted(capture_dir.rglob("*.md")):
        if not path.is_file():
            continue
        if LEGACY_DAILY_PATTERN.fullmatch(path.name):
            continue
        raw = read_note(path)
        yield to_payload(raw)
