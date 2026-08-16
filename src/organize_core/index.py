"""Vault scan, note model, index persistence, query/search (spec 03 §7, 02).

Performance targets (spec 09 §4, hard gates):
    full index of 10k notes < 5 s (1k < 2 s); incremental single-file
    update < 500 ms; and persistence must NOT rewrite a multi-MB file on
    every single-file update (batch/debounce saves).

Persistence format decision (02 says the internal format is free): a single
JSON snapshot at ``CorePaths.index_path`` shaped
``{"schema_version": 1, "generated_at": <epoch>, "notes": {abs_path: record}}``
— chosen over SQLite because the whole index must be warm in memory anyway
for <100 ms suggestions (10 §1), the working set (~10k records) is a few MB,
and JSON keeps zero schema-migration machinery. Dirty records are batched and
the snapshot written atomically on flush (debounced), not per update. Entries
whose files no longer exist are pruned on load and on scan (03 §7 — the old
index kept stale wrong-root entries forever, 01 "evidence"). Regenerated
from scratch on first run; never migrated from the old plugin's index.json
(09 §5.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from organize_core.config import Config

INDEX_SCHEMA_VERSION = 1

ParaType = Literal["project", "area", "resource", "archive", "capture", "other"]


@dataclass
class NoteRecord:
    """The shared per-file metadata record (spec 03 §7) used by UI, search,
    suggest, and learn. Scalar frontmatter values are coerced to lists for
    tags/aliases/sources/modalities at index time; ``title`` = first
    ``# heading`` else filename stem (08 §B10: never ``aliases[0]`` of a
    string). Parse failures never abort a scan — the file is indexed with
    empty metadata and a logged warning."""

    path: str  # absolute, symlink-resolved
    filename: str
    title: str
    para_type: ParaType
    folder: str  # immediate parent folder name
    timestamp: str | None = None  # kept as string (frontmatter contract)
    id: str | None = None
    aliases: list[str] = field(default_factory=list)
    capture_id: str | None = None
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)  # canonicalized to list
    location: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    processing_status: str | None = None
    created_date: str | None = None
    last_edited_date: str | None = None
    size: int = 0
    modified: float = 0.0  # mtime epoch
    indexed_at: float = 0.0
    normalized_tags: list[str] = field(default_factory=list)
    # NL description when this is a folder's index note or a described file
    # (spec 11 §3); loaded by the index, surfaced in UI/suggestions.
    description: str | None = None


@dataclass
class QueryCriteria:
    """Search criteria (spec 03 §2 session filters + search picker).

    Multi-value criteria are LISTS (canonicalize-at-boundary decision,
    03 §2): OR within a criterion, AND across criteria. ``since``/``until``
    are ``YYYY-MM-DD`` strings compared against timestamp/created_date.
    """

    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)  # processing_status
    para_type: list[ParaType] = field(default_factory=list)
    since: str | None = None
    until: str | None = None
    text: str | None = None  # substring over title/filename/aliases/tags

    @classmethod
    def from_filter_args(cls, args: dict[str, str | list[str]]) -> QueryCriteria:
        """Parse ``k=v`` session-filter strings (``tags=a,b`` comma lists;
        ``until_date`` alias for ``until``), accepting both a string and a
        list per criterion and canonicalizing to lists (spec 03 §2)."""
        raise NotImplementedError


class VaultIndex:
    """In-memory index + persistence. One instance per core process; the
    server holds it warm (10 §1). All mutation goes through the writer
    queue when serving (10 §1)."""

    def __init__(self, config: Config, index_path: Path) -> None:
        raise NotImplementedError

    # --- lifecycle -------------------------------------------------------

    def load(self) -> None:
        """Load the snapshot if present; wrong/corrupt schema_version ⇒ log
        loudly and fall back to an empty index (rebuild), never crash.
        Prunes records whose files no longer exist."""
        raise NotImplementedError

    def flush(self) -> None:
        """Persist pending changes atomically (batched — see module note)."""
        raise NotImplementedError

    def full_reindex(self) -> dict[str, Any]:
        """Rebuild from zero with a reentrancy guard; returns
        ``{"total": int, "duration": float}`` (spec 03 §2 ``reindex``)."""
        raise NotImplementedError

    # --- scanning --------------------------------------------------------

    def scan(self) -> int:
        """Recursive scan of vault root for ``**/*.md``, honoring
        ``ignore_patterns`` as GLOBS (08 §A13), skipping ``.obsidian``,
        files over ``max_file_size`` (warn), and tolerating non-markdown
        files interleaved in raw_capture (02). Returns records touched."""
        raise NotImplementedError

    def update_file(self, path: Path) -> NoteRecord | None:
        """Incremental single-file (re)index; removes the entry when the
        file is gone. Target < 500 ms (09 §4). Returns the new record."""
        raise NotImplementedError

    def remove_file(self, path: Path) -> None:
        raise NotImplementedError

    # --- reads -----------------------------------------------------------

    def get(self, path: Path | str) -> NoteRecord | None:
        raise NotImplementedError

    def query(self, criteria: QueryCriteria) -> list[NoteRecord]:
        """Filter records per QueryCriteria semantics. Deterministic order:
        timestamp ascending (fallback mtime), tiebreak path ascending
        (spec 03 §2 session ordering)."""
        raise NotImplementedError

    def search(self, text: str, *, scope: Path | None = None) -> list[NoteRecord]:
        """Free-text search over title/filename/aliases/tags; ``scope``
        restricts to a folder subtree (03 §3 context-aware search)."""
        raise NotImplementedError

    def para_subfolders(self, para_type: str) -> list[Path]:
        """Immediate subfolders of one PARA root — suggestion candidates
        (spec 04 §1)."""
        raise NotImplementedError

    def folder_children(self, folder: Path) -> tuple[list[Path], list[NoteRecord]]:
        """(subdirs, notes) for directory browsing (03 §3 state 2)."""
        raise NotImplementedError

    def values_of(self, key: str) -> list[str]:
        """All distinct values of a frontmatter key across the index —
        completion source for ``complete = "existing"`` (spec 07)."""
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        """Counts for health/debug (03 §2 ``debug``): total, per para_type,
        capture backlog (also feeds ActionRecord.context.vault_stats, 12 §2)."""
        raise NotImplementedError


def para_type_for(path: Path, config: Config) -> ParaType:
    """Derive the PARA type from a path relative to the vault root using the
    CONFIGURED folder names (spec 03 §7): projects|areas|resources|archives
    keys → singular ParaType, the capture folder → "capture", else "other"."""
    raise NotImplementedError
