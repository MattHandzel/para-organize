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

Implementation notes (decisions this module makes, all spec-anchored):

* **Batching contract.** ``update_file``/``remove_file`` mutate memory and
  bump a pending counter; they never write. ``flush()`` is a no-op when
  nothing is pending, and writes exactly one snapshot otherwise (09 §4: no
  multi-MB rewrite per keystroke). As a crash bound, ``flush_threshold``
  (instance attribute, default 256 pending changes) triggers an automatic
  flush from the incremental paths only — ``scan``/``full_reindex`` write
  once at the end regardless of size.
* **Snapshot carries ``vault_root``.** A snapshot generated against a
  different root is rejected loudly and rebuilt, and any record path outside
  the current root is pruned — the exact defect behind the live index's
  stale ``…/Main/notes/capture`` entries (08 §C5).
* **``ignore_patterns`` are GLOBS** (08 §A13): matched against the
  vault-relative POSIX path, and — for patterns without a ``/`` — against
  every path component. Directories are pruned during the walk so ignored
  subtrees cost nothing.
* **Broken files are indexed, never fatal** (03 §7): unparseable YAML or an
  unreadable file yields a record with empty metadata and
  ``parse_error=True`` plus a loud log line; the scan continues.
* **``title``** is the first ``# heading`` of the body (outside fenced code)
  else the filename stem — never ``aliases[0]``, which on a scalar alias
  yields a single character (08 §B10).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from organize_core import frontmatter
from organize_core.config import CANDIDATE_DEPTH_ALL as _CANDIDATE_DEPTH_ALL
from organize_core.config import Config
from organize_core.errors import ConfigError, FrontmatterError, IndexingError

INDEX_SCHEMA_VERSION = 1

ParaType = Literal["project", "area", "resource", "archive", "capture", "other"]

logger = logging.getLogger(__name__)

#: PARA config keys (``vault.para_folders``) → the singular ParaType (03 §7).
PARA_KEY_TO_TYPE: dict[str, ParaType] = {
    "projects": "project",
    "areas": "area",
    "resources": "resource",
    "archives": "archive",
}
PARA_TYPE_TO_KEY: dict[str, str] = {value: key for key, value in PARA_KEY_TO_TYPE.items()}

#: Re-exported beside the walk that honors it; DEFINED in ``config.py`` with
#: the key itself, so the validator and the walk cannot drift (21 §2.1).
CANDIDATE_DEPTH_ALL = _CANDIDATE_DEPTH_ALL

#: PARA types a NOTE candidate may have (spec 21 §3.1): the non-archive PARA
#: types. Captures, archives, and everything else are never note candidates.
CANDIDATE_NOTE_PARA_TYPES: frozenset[str] = frozenset({"project", "area", "resource"})

#: The archives root is never a scored candidate, at any depth (spec 04 §1,
#: 21 §2.3). ONE definition — ``suggest`` binds its own name to this object
#: rather than repeating the pair, so the folder walk and the scorer cannot
#: disagree about what "archive" means.
EXCLUDED_CANDIDATE_PARA_KEYS: frozenset[str] = frozenset({"archives", "archive"})
_EXCLUDED_CANDIDATE_PARA_KEYS = EXCLUDED_CANDIDATE_PARA_KEYS

#: Frontmatter keys already promoted to first-class NoteRecord attributes.
_PROMOTED_FIELDS: frozenset[str] = frozenset(frontmatter.KNOWN_FIELD_ORDER) | {"description"}

#: Pending-change count that forces an incremental flush (crash bound).
_DEFAULT_FLUSH_THRESHOLD = 256

_VALID_FILTER_KEYS = (
    "tags",
    "sources",
    "modalities",
    "status",
    "para_type",
    "since",
    "until",
    "until_date",
    "text",
)


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
    # --- additive fields (documented extensions of the 03 §7 shape) ------
    #: True when the file's frontmatter could not be parsed (or the file
    #: could not be read): the record exists with empty metadata so the note
    #: is still visible/organizable, and the failure is loud rather than a
    #: silent skip (03 §7 tolerance + 09 §1.5 loud failure).
    parse_error: bool = False
    #: Frontmatter fields outside the known capture schema (``title``,
    #: ``no-ai``, Obsidian properties, and every user/metadata field added
    #: per spec 07). Required so ``values_of`` can offer
    #: ``complete = "existing"`` completion for ARBITRARY configured
    #: metadata keys (07), not just the built-in ones.
    extra: dict[str, Any] = field(default_factory=dict)


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
        criteria = cls()
        for raw_key, raw_value in (args or {}).items():
            key = str(raw_key).strip().lower()
            if key == "until_date":
                key = "until"
            if key not in _VALID_FILTER_KEYS:
                raise ConfigError(
                    f"unknown session filter {raw_key!r}",
                    hint="valid filters: " + ", ".join(_VALID_FILTER_KEYS),
                )
            if key in ("since", "until"):
                value = _single_value(raw_value)
                if value is not None:
                    setattr(criteria, key, _validated_date(key, value))
            elif key == "text":
                value = _single_value(raw_value)
                criteria.text = value or None
            else:
                setattr(criteria, key, _split_values(raw_value))
        return criteria


def _single_value(raw: str | list[str] | None) -> str | None:
    """Last-wins scalar view of a criterion that accepts str or list."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw if str(item).strip()]
        return values[-1] if values else None
    text = str(raw).strip()
    return text or None


def _split_values(raw: str | list[str] | None) -> list[str]:
    """Canonicalize a multi-value criterion to a list (03 §2 boundary rule)."""
    items: list[str] = []
    chunks = raw if isinstance(raw, (list, tuple)) else [raw]
    for chunk in chunks:
        if chunk is None:
            continue
        for part in str(chunk).split(","):
            value = part.strip()
            if value:
                items.append(value)
    return items


def _validated_date(key: str, value: str) -> str:
    """``YYYY-MM-DD`` or a loud failure naming the key (09 §1.5)."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ConfigError(
            f"session filter {key}={value!r} is not a YYYY-MM-DD date",
            hint="use e.g. since=2026-06-01",
        ) from exc
    return value


class VaultIndex:
    """In-memory index + persistence. One instance per core process; the
    server holds it warm (10 §1). All mutation goes through the writer
    queue when serving (10 §1)."""

    def __init__(self, config: Config, index_path: Path) -> None:
        self.config = config
        self.index_path = Path(index_path)
        # No ``.expanduser()`` here or anywhere else in this module:
        # structural decision 4 — nothing outside ``paths.py`` consults
        # ``os.environ``/``Path.home()``, and ``expanduser`` reads ``HOME``.
        # ``config.vault.root`` is already expanded by ``config._expand_path``
        # → ``paths.expand``, and note arguments are resolved by the
        # composition roots. Expanding here also made a vault-relative name
        # starting with ``~`` raise a bare ``RuntimeError`` (outside the
        # OrganizeError taxonomy) instead of addressing the real file.
        self.root = Path(config.vault.root)
        try:
            self.root = self.root.resolve()
        except OSError:  # pragma: no cover - resolve() is non-strict
            self.root = self.root.absolute()
        self.flush_threshold = _DEFAULT_FLUSH_THRESHOLD
        self._records: dict[str, NoteRecord] = {}
        self._pending = 0
        self._reindexing = False
        # Candidate-folder walk cache (spec 21 §2.4): the walk is one os.walk
        # per PARA root and must happen ONCE PER CANDIDATE-SET BUILD, never
        # per capture and never inside `suggest()` (04's purity constraint).
        # Keyed by the resolved depth token so browsing depth (16 §2) and
        # ranking depth stay two knobs.
        self._folder_cache: dict[str, list[Path]] = {}
        # Bumped by folder-cache invalidation and by any index mutation that
        # changed a note's CANDIDATE IDENTITY (`_candidate_identity`), so a
        # caller that caches a built candidate SET (folders + notes +
        # inverted index) has one integer to compare against — and archiving
        # a capture, which changes neither half of the ballot, does not move
        # it (21 §2.4).
        self._candidate_generation = 0

    # --- lifecycle -------------------------------------------------------

    def load(self) -> None:
        """Load the snapshot if present; wrong/corrupt schema_version ⇒ log
        loudly and fall back to an empty index (rebuild), never crash.
        Prunes records whose files no longer exist."""
        self._records = {}
        self._pending = 0
        self.invalidate_folder_cache()  # 21 §2.4: a reload replaces the ballot
        if not self.index_path.exists():
            logger.info("index: no snapshot at %s — starting empty", self.index_path)
            return
        try:
            raw = self.index_path.read_text(encoding="utf-8", errors="replace")
            payload = json.loads(raw)
        except (OSError, ValueError) as exc:
            logger.error(
                "index: snapshot %s is unreadable/corrupt (%s) — rebuilding from scratch",
                self.index_path,
                exc,
            )
            self._pending = 1
            return
        if not isinstance(payload, dict):
            logger.error(
                "index: snapshot %s is not a JSON object — rebuilding from scratch",
                self.index_path,
            )
            self._pending = 1
            return
        version = payload.get("schema_version")
        if version != INDEX_SCHEMA_VERSION:
            logger.error(
                "index: snapshot %s has schema_version %r (expected %d) — rebuilding from scratch",
                self.index_path,
                version,
                INDEX_SCHEMA_VERSION,
            )
            self._pending = 1
            return
        snapshot_root = payload.get("vault_root")
        if snapshot_root is not None and str(snapshot_root) != str(self.root):
            logger.error(
                "index: snapshot %s was built for vault_root %r (now %r) — rebuilding from scratch",
                self.index_path,
                snapshot_root,
                str(self.root),
            )
            self._pending = 1
            return

        notes = payload.get("notes")
        if not isinstance(notes, dict):
            logger.error("index: snapshot %s has no notes map — rebuilding", self.index_path)
            self._pending = 1
            return

        pruned = 0
        for key, entry in notes.items():
            record = _record_from_json(key, entry)
            if record is None:
                logger.warning("index: dropping malformed snapshot entry for %s", key)
                pruned += 1
                continue
            if not self._within_root(Path(record.path)):
                logger.warning(
                    "index: pruning stale entry outside vault root: %s", record.path
                )
                pruned += 1
                continue
            if not Path(record.path).exists():
                pruned += 1
                continue
            self._records[record.path] = record
        if pruned:
            logger.info("index: pruned %d dead/stale entries from %s", pruned, self.index_path)
            self._pending += pruned

    def flush(self) -> None:
        """Persist pending changes atomically (batched — see module note)."""
        if self._pending == 0:
            logger.debug("index: flush skipped (no pending changes)")
            return
        payload = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "generated_at": time.time(),
            "vault_root": str(self.root),
            "notes": {path: asdict(record) for path, record in sorted(self._records.items())},
        }
        try:
            write_snapshot(self.index_path, payload)
        except OSError as exc:
            raise IndexingError(
                f"could not write the index snapshot {self.index_path}: {exc}",
                hint="check that the state directory exists and is writable "
                "($ORGANIZE_CORE_STATE_DIR)",
            ) from exc
        logger.debug(
            "index: flushed %d records (%d pending changes) to %s",
            len(self._records),
            self._pending,
            self.index_path,
        )
        self._pending = 0

    def full_reindex(self) -> dict[str, Any]:
        """Rebuild from zero with a reentrancy guard; returns
        ``{"total": int, "duration": float}`` (spec 03 §2 ``reindex``)."""
        if self._reindexing:
            raise IndexingError(
                "a full reindex is already running",
                hint="wait for the running reindex to finish before starting another",
            )
        self._reindexing = True
        started = time.monotonic()
        try:
            self._records = {}
            self._pending = 1  # a cleared index is itself a change to persist
            total = self.scan()
            self.flush()
        finally:
            self._reindexing = False
        duration = time.monotonic() - started
        logger.info("index: full reindex of %s indexed %d notes in %.3fs", self.root, total, duration)
        return {"total": total, "duration": duration}

    # --- scanning --------------------------------------------------------

    def scan(self) -> int:
        """Recursive scan of vault root for ``**/*.md``, honoring
        ``ignore_patterns`` as GLOBS (08 §A13), skipping ``.obsidian``,
        files over ``max_file_size`` (warn), and tolerating non-markdown
        files interleaved in raw_capture (02). Returns records touched."""
        if not self.root.is_dir():
            raise IndexingError(
                f"vault root {self.root} does not exist or is not a directory",
                hint="set [vault] root in config.toml to the real vault "
                "(e.g. ~/Obsidian/Main — NOT ~/Obsidian/Main/notes)",
            )
        patterns = tuple(self.config.vault.ignore_patterns or ())
        max_size = int(self.config.vault.max_file_size)
        seen: set[str] = set()
        touched = 0
        root_str = str(self.root)
        for dirpath, dirnames, filenames in os.walk(root_str, topdown=True, followlinks=False):
            rel_dir = _relative_posix(root_str, dirpath)
            keep: list[str] = []
            for name in dirnames:
                if name.startswith("."):  # .obsidian, .git, .trash …
                    continue
                child_rel = f"{rel_dir}/{name}" if rel_dir else name
                if is_ignored(child_rel, patterns):
                    continue
                keep.append(name)
            dirnames[:] = sorted(keep)

            for name in sorted(filenames):
                if not name.endswith(".md"):
                    continue  # .wav/.txt/.pdf interleaved in raw_capture (02)
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if is_ignored(rel, patterns):
                    continue
                path = Path(dirpath) / name
                record = self._build_record(path, rel, max_size)
                if record is None:
                    continue
                seen.add(record.path)
                self._records[record.path] = record
                touched += 1

        stale = [key for key in self._records if key not in seen]
        for key in stale:
            del self._records[key]
        if stale:
            logger.info("index: pruned %d entries whose files are gone", len(stale))
        self._pending += touched + len(stale)
        # A scan is the one place directories are known to have been re-read,
        # so it is also where a stale candidate-folder walk is dropped
        # (21 §2.4 — `index.reindex` is one of the two named invalidators).
        self.invalidate_folder_cache()
        return touched

    def update_file(self, path: Path) -> NoteRecord | None:
        """Incremental single-file (re)index; removes the entry when the
        file is gone. Target < 500 ms (09 §4). Returns the new record."""
        resolved = self._resolve(path)
        key = str(resolved)
        before = self._records.get(key)
        if not self._within_root(resolved):
            logger.warning(
                "index: refusing to index %s — outside the vault root %s", resolved, self.root
            )
            if key in self._records:
                del self._records[key]
                self._touch()
                self._note_candidacy_changed(before, None)
            return None
        rel = _relative_posix(str(self.root), key)
        if not _indexable(rel, tuple(self.config.vault.ignore_patterns or ())):
            if key in self._records:
                del self._records[key]
                self._touch()
                self._note_candidacy_changed(before, None)
            return None
        record = self._build_record(resolved, rel, int(self.config.vault.max_file_size))
        if record is None:
            if key in self._records:
                del self._records[key]
                self._touch()
                self._note_candidacy_changed(before, None)
            return None
        self._records[record.path] = record
        self._touch()
        self._note_candidacy_changed(before, record)
        return record

    def remove_file(self, path: Path) -> None:
        key = str(self._resolve(path))
        if key in self._records:
            before = self._records[key]
            del self._records[key]
            self._touch()
            self._note_candidacy_changed(before, None)

    # --- reads -----------------------------------------------------------

    def get(self, path: Path | str) -> NoteRecord | None:
        return self._records.get(str(self._resolve(path)))

    def query(self, criteria: QueryCriteria) -> list[NoteRecord]:
        """Filter records per QueryCriteria semantics. Deterministic order:
        timestamp ascending (fallback mtime), tiebreak path ascending
        (spec 03 §2 session ordering)."""
        tags = _normalized_set(criteria.tags, self.config.suggestions.tag_normalization)
        sources = _normalized_set(criteria.sources, self.config.suggestions.tag_normalization)
        modalities = _normalized_set(criteria.modalities, None)
        statuses = {value.strip().casefold() for value in criteria.status or [] if value.strip()}
        para_types = {value.strip().casefold() for value in criteria.para_type or [] if value.strip()}
        text = (criteria.text or "").strip().casefold()

        matches: list[NoteRecord] = []
        for record in self._records.values():
            if tags and not (tags & _record_tag_keys(record, self.config)):
                continue
            if sources and not (
                sources
                & {
                    frontmatter.normalize_tag(str(item), self.config.suggestions.tag_normalization)
                    for item in record.sources
                }
            ):
                continue
            if modalities and not (
                modalities & {frontmatter.normalize_tag(str(item)) for item in record.modalities}
            ):
                continue
            if statuses and (record.processing_status or "").strip().casefold() not in statuses:
                continue
            if para_types and record.para_type.casefold() not in para_types:
                continue
            if criteria.since or criteria.until:
                day = _record_day(record)
                if criteria.since and day < criteria.since:
                    continue
                if criteria.until and day > criteria.until:
                    continue
            if text and text not in _haystack(record):
                continue
            matches.append(record)
        return sorted(matches, key=_order_key)

    def search(self, text: str, *, scope: Path | None = None) -> list[NoteRecord]:
        """Free-text search over title/filename/aliases/tags; ``scope``
        restricts to a folder subtree (03 §3 context-aware search)."""
        needle = (text or "").strip().casefold()
        if not needle:
            return []
        prefix: str | None = None
        if scope is not None:
            scope_path = self._resolve(scope)
            prefix = str(scope_path).rstrip(os.sep) + os.sep
        results = [
            record
            for record in self._records.values()
            if needle in _haystack(record) and (prefix is None or record.path.startswith(prefix))
        ]
        return sorted(results, key=_order_key)

    def para_subfolders(self, para_type: str) -> list[Path]:
        """Immediate subfolders of one PARA root — suggestion candidates
        (spec 04 §1)."""
        key = str(para_type).strip().casefold()
        key = PARA_TYPE_TO_KEY.get(key, key)
        folder_name = self.config.vault.para_folders.get(key)
        if folder_name is None:
            raise ConfigError(
                f"unknown PARA type {para_type!r}",
                hint="valid values: " + ", ".join(sorted(self.config.vault.para_folders)),
            )
        root = self.root / folder_name
        if not root.is_dir():
            logger.warning(
                "index: PARA folder %s does not exist — no candidates from %s", root, key
            )
            return []
        patterns = tuple(self.config.vault.ignore_patterns or ())
        children: list[Path] = []
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            rel = _relative_posix(str(self.root), str(entry))
            if is_ignored(rel, patterns):
                continue
            children.append(entry)
        return children

    # --- suggestion candidates (spec 21 §2, §3.1) -------------------------

    @property
    def candidate_generation(self) -> int:
        """Monotonic counter bumped when the CANDIDATE POPULATION changes —
        a folder-cache invalidation, or an index mutation that changed some
        note's :meth:`_candidate_identity`.

        Deliberately NOT "every index mutation": archiving a capture mutates
        the index twice and changes neither half of the ballot, and coupling
        the two made the review loop rebuild an 8,104-candidate set once per
        capture (~100 ms, inside the server's read lock).

        A caller that caches a built candidate SET (folders + notes + the
        §3.5 inverted index) compares this one integer instead of inventing
        its own invalidation rule — the same hook doc 16 §2 requires for
        ``state.folders``, shared rather than duplicated.
        """
        return self._candidate_generation

    def invalidate_folder_cache(self) -> None:
        """Drop the cached candidate-folder walk (``folder.create`` and
        anything else that creates a directory the index cannot see, because
        an empty folder holds no notes)."""
        self._folder_cache.clear()
        self._candidate_generation += 1

    def candidate_folders(self, para_type: str, depth: int | str | None = None) -> list[Path]:
        """Every SCORED destination folder under ONE PARA root (spec 21 §2.1)
        — the sibling of :meth:`para_subfolders`, with a depth knob.

        ``depth`` is levels below the PARA root, an immediate subfolder being
        depth 1; ``"all"`` is unbounded; ``None`` reads
        ``suggestions.max_candidate_depth``. Dot-directories are skipped and
        ``vault.ignore_patterns`` is honored, exactly as
        :meth:`para_subfolders` does at depth 1 — a walk that silently
        stopped honoring the ignore list would look like a recall win rather
        than a bug. ``depth = 1`` therefore returns exactly what
        :meth:`para_subfolders` returns.

        The ARCHIVES root is never a candidate at any depth (04 §1), so it
        answers with an empty list rather than its contents.

        THIS IS A SEPARATE ACCESSOR ON PURPOSE (21 §2.3):
        :meth:`para_subfolders` keeps its depth-1 meaning for ``folder.list``,
        ``cli``, and ``fileops``; browsing depth (16 §2) and RANKING depth are
        two knobs and nothing reconciles them.

        The walk is cached (21 §2.4) and copied out, so it runs once per
        candidate-set build rather than once per capture — and never inside
        ``suggest()``, which does no I/O at all (04's purity constraint).
        """
        key = str(para_type).strip().casefold()
        key = PARA_TYPE_TO_KEY.get(key, key)
        folder_name = self.config.vault.para_folders.get(key)
        if folder_name is None:
            raise ConfigError(
                f"unknown PARA type {para_type!r}",
                hint="valid values: " + ", ".join(sorted(self.config.vault.para_folders)),
            )
        if key in _EXCLUDED_CANDIDATE_PARA_KEYS:
            return []
        token = _depth_token(
            depth if depth is not None else self.config.suggestions.max_candidate_depth
        )
        cache_key = f"{key}\0{token}"
        cached = self._folder_cache.get(cache_key)
        if cached is None:
            cached = self._walk_candidate_folders(self.root / str(folder_name), _depth_limit(token))
            self._folder_cache[cache_key] = cached
        return list(cached)

    def candidate_notes(self) -> list[NoteRecord]:
        """Every indexed note that may be a NOTE destination (spec 21 §3.1):
        ``para_type`` is a non-archive PARA type. Captures, archives, and
        anything outside the PARA roots (``para_type`` ``other``) are never
        note candidates. Deterministic order: path ascending."""
        return sorted(
            (
                record
                for record in list(self._records.values())
                if record.para_type in CANDIDATE_NOTE_PARA_TYPES
            ),
            key=lambda record: record.path,
        )

    def _walk_candidate_folders(self, para_root: Path, limit: int | None) -> list[Path]:
        if not para_root.is_dir():
            logger.warning(
                "index: PARA folder %s does not exist — no candidates from it", para_root
            )
            return []
        patterns = tuple(self.config.vault.ignore_patterns or ())
        root_str = str(self.root)
        para_root_str = str(para_root)
        children: list[Path] = []
        for dirpath, dirnames, _filenames in os.walk(
            para_root_str, topdown=True, followlinks=False
        ):
            rel_to_para = _relative_posix(para_root_str, dirpath)
            # Depth is LEVELS BELOW THE PARA ROOT: the root itself is 0 and an
            # immediate subfolder is 1 (21 §2.1). Off-by-one in that
            # convention is the likeliest silent bug in this file, and it is
            # asserted directly at every shipped depth.
            depth = len(rel_to_para.split("/")) if rel_to_para else 0
            if depth >= 1:
                children.append(Path(dirpath))
            if limit is not None and depth >= limit:
                dirnames[:] = []
                continue
            keep: list[str] = []
            for name in dirnames:
                if name.startswith("."):  # .obsidian, .git, .backups …
                    continue
                child_rel = _relative_posix(root_str, str(Path(dirpath) / name))
                if is_ignored(child_rel, patterns):
                    continue
                keep.append(name)
            dirnames[:] = sorted(keep)
        return children

    def folder_children(self, folder: Path) -> tuple[list[Path], list[NoteRecord]]:
        """(subdirs, notes) for directory browsing (03 §3 state 2)."""
        target = self._resolve(folder)
        patterns = tuple(self.config.vault.ignore_patterns or ())
        subdirs: list[Path] = []
        if target.is_dir():
            for entry in sorted(target.iterdir(), key=lambda p: p.name):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                rel = _relative_posix(str(self.root), str(entry))
                if is_ignored(rel, patterns):
                    continue
                subdirs.append(entry)
        prefix = str(target).rstrip(os.sep) + os.sep
        notes = [
            record
            for record in self._records.values()
            if record.path.startswith(prefix) and os.sep not in record.path[len(prefix) :]
        ]
        return subdirs, sorted(notes, key=_order_key)

    def values_of(self, key: str) -> list[str]:
        """All distinct values of a frontmatter key across the index —
        completion source for ``complete = "existing"`` (spec 07)."""
        name = str(key).strip()
        if not name:
            return []
        seen: dict[str, None] = {}
        for record in self._records.values():
            for value in _values_for_key(record, name):
                text = value.strip()
                if text:
                    seen.setdefault(text, None)
        return sorted(seen)

    def stats(self) -> dict[str, Any]:
        """Counts for health/debug (03 §2 ``debug``): total, per para_type,
        capture backlog (also feeds ActionRecord.context.vault_stats, 12 §2)."""
        by_type: dict[str, int] = {}
        backlog = 0
        parse_errors = 0
        # Iterate a SNAPSHOT, not the live mapping: the server used to call
        # this outside its write lock, and a concurrent writer turned an
        # already-applied operation into a RuntimeError("dictionary changed
        # size during iteration"). The server now snapshots under the lock;
        # this is the defence in depth for every other caller.
        records = list(self._records.values())
        for record in records:
            by_type[record.para_type] = by_type.get(record.para_type, 0) + 1
            if record.para_type == "capture" and (record.processing_status or "") == "raw":
                backlog += 1
            if record.parse_error:
                parse_errors += 1
        return {
            "total": len(records),
            "by_para_type": by_type,
            "capture_backlog": backlog,
            "parse_errors": parse_errors,
            "pending_changes": self._pending,
            "index_path": str(self.index_path),
            "vault_root": str(self.root),
        }

    # --- internals -------------------------------------------------------

    def _touch(self) -> None:
        """Record one pending change; flush only when the batch is large
        enough to matter (09 §4 — never a snapshot per keystroke).

        Spec 09 §40 allows "batch, debounce, or switch format"; this is the
        BATCH, and it doubles as the crash bound (at most
        ``flush_threshold`` changes can be lost). A time debounce is
        deliberately NOT stacked on top of it here: it would delay the
        crash-bound write, which is the one thing this counter exists to
        guarantee. ``vault.incremental_debounce`` is the debounce on the
        client's incremental-reindex TRIGGER (spec 03 §7) — see the
        reserved-key allowlist in tests/test_config.py.
        """
        self._pending += 1
        if self.flush_threshold and self._pending >= self.flush_threshold:
            self.flush()

    @staticmethod
    def _candidate_identity(record: NoteRecord | None) -> tuple[Any, ...] | None:
        """Everything the BALLOT reads about one note (21 §3.1), or ``None``
        when the note is not a candidate at all.

        This is the whole of a note's contribution to a cached candidate set:
        its path (the identity and the learning key), its PARA type (the type
        bonus), and the four §3.1 key fields. Two records with equal
        identities produce byte-identical candidates, so a mutation that
        leaves this tuple unchanged CANNOT change the ballot and must not
        invalidate it.
        """
        if record is None or record.para_type not in CANDIDATE_NOTE_PARA_TYPES:
            return None
        return (
            record.path,
            record.para_type,
            record.filename,
            tuple(record.aliases or ()),
            record.id,
            record.title,
            record.capture_id,
        )

    def _note_candidacy_changed(
        self, before: NoteRecord | None, after: NoteRecord | None
    ) -> None:
        """Bump the candidate generation ONLY when this note's ballot
        contribution actually changed (21 §2.4).

        Coupling the ballot to every index mutation is what made the real
        review loop rebuild an 8,104-candidate set once per capture — ~100 ms
        inside the server's READ lock, per capture, for a mutation (archiving
        a capture) that changes neither half of the ballot. A capture is never
        a note candidate, so archiving one leaves both identities ``None``
        and the cached set stands.

        Note that this is deliberately NOT "did the file change": a note whose
        BODY was edited is the same candidate, because 21 §3.1 makes body,
        tags, folder and sources explicitly non-keys.
        """
        if self._candidate_identity(before) != self._candidate_identity(after):
            self._candidate_generation += 1

    def _resolve(self, path: Path | str) -> Path:
        candidate = Path(path)  # never .expanduser() — see __init__
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            return candidate.resolve()
        except OSError:  # pragma: no cover - resolve() is non-strict
            return candidate.absolute()

    def _within_root(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
        except ValueError:
            return False
        return True

    def _build_record(self, path: Path, rel: str, max_size: int) -> NoteRecord | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.error("index: cannot stat %s (%s) — skipping", path, exc)
            return None
        if max_size and stat.st_size > max_size:
            logger.warning(
                "index: skipping %s — %d bytes exceeds vault.max_file_size (%d)",
                path,
                stat.st_size,
                max_size,
            )
            return None

        parse_error = False
        body = ""
        fields: dict[str, Any] = {}
        try:
            document = frontmatter.load_file(path)
        except FileNotFoundError:
            return None
        except (FrontmatterError, OSError, UnicodeError) as exc:
            # 03 §7: parse failures never abort a scan — log loudly and index
            # the file with empty metadata so it is still organizable.
            logger.warning("index: %s has unusable frontmatter (%s) — indexed empty", path, exc)
            parse_error = True
            body = _body_only(path)
        else:
            body = document.body
            if document.frontmatter is not None:
                fields = document.frontmatter.fields

        view = frontmatter.Frontmatter(fields=fields)
        tag_map = self.config.suggestions.tag_normalization
        tags = _string_list(view.get_list("tags"))
        record = NoteRecord(
            path=str(path),
            filename=path.name,
            title=_title_for(body, path),
            para_type=_para_type_for_rel(rel, self.config),
            folder=path.parent.name,
            timestamp=_scalar(fields.get("timestamp")),
            id=_scalar(fields.get("id")),
            aliases=_string_list(view.get_list("aliases")),
            capture_id=_scalar(fields.get("capture_id")),
            tags=tags,
            sources=_string_list(view.get_list("sources")),
            modalities=_string_list(view.get_list("modalities")),
            context=_string_list(view.get_list("context")),
            location=fields.get("location") if isinstance(fields.get("location"), dict) else None,
            metadata=fields.get("metadata") if isinstance(fields.get("metadata"), dict) else {},
            processing_status=_scalar(fields.get("processing_status")),
            created_date=_scalar(fields.get("created_date")),
            last_edited_date=_scalar(fields.get("last_edited_date")),
            size=stat.st_size,
            modified=stat.st_mtime,
            indexed_at=time.time(),
            normalized_tags=[frontmatter.normalize_tag(tag, tag_map) for tag in tags],
            description=_scalar(fields.get("description")),
            parse_error=parse_error,
            extra={
                str(key): value
                for key, value in fields.items()
                if isinstance(key, str) and key not in _PROMOTED_FIELDS
            },
        )
        return record


def write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace ``path`` with ``payload`` as JSON.

    Module-level (rather than a method) so tests can count real writes: the
    batching contract in 09 §4 is "one snapshot per flush, never per update".
    ``default=str`` keeps an exotic YAML value from ever making a flush raise.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", errors="replace") as handle:
        json.dump(payload, handle, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def para_type_for(path: Path, config: Config) -> ParaType:
    """Derive the PARA type from a path relative to the vault root using the
    CONFIGURED folder names (spec 03 §7): projects|areas|resources|archives
    keys → singular ParaType, the capture folder → "capture", else "other"."""
    root = Path(config.vault.root)  # never .expanduser() — see VaultIndex.__init__
    try:
        root = root.resolve()
    except OSError:  # pragma: no cover - resolve() is non-strict
        root = root.absolute()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
    except OSError:  # pragma: no cover
        candidate = candidate.absolute()
    try:
        rel = candidate.relative_to(root).as_posix()
    except ValueError:
        return "other"
    return _para_type_for_rel(rel, config)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _para_type_for_rel(rel: str, config: Config) -> ParaType:
    """Longest configured-prefix wins, so ``archive/capture/raw_capture`` is
    an ``archive`` (not a ``capture``) no matter the config order."""
    prefixes: list[tuple[str, ParaType]] = []
    for key, folder in (config.vault.para_folders or {}).items():
        para = PARA_KEY_TO_TYPE.get(str(key).strip().casefold())
        if para is None:
            continue
        prefixes.append((_clean_rel(folder), para))
    prefixes.append((_clean_rel(config.vault.capture_folder), "capture"))

    best: tuple[str, ParaType] | None = None
    for prefix, para in prefixes:
        if not prefix:
            continue
        if rel == prefix or rel.startswith(prefix + "/"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, para)
    return best[1] if best else "other"


def _depth_token(depth: int | str) -> str:
    """Canonical cache key for a ``max_candidate_depth`` value (spec 21 §2.1):
    ``"all"`` or the decimal integer. Validation lives in ``config.py``; this
    is the last line of defence for a hand-built VaultIndex."""
    if isinstance(depth, str):
        if depth.strip().casefold() == CANDIDATE_DEPTH_ALL:
            return CANDIDATE_DEPTH_ALL
        raise ConfigError(
            f"suggestions.max_candidate_depth must be an integer >= 1 or {CANDIDATE_DEPTH_ALL!r}, "
            f"got {depth!r}",
            hint=f'set it to a number (e.g. 3) or to "{CANDIDATE_DEPTH_ALL}"',
        )
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ConfigError(
            f"suggestions.max_candidate_depth must be an integer >= 1 or {CANDIDATE_DEPTH_ALL!r}, "
            f"got {depth!r}",
            hint=f'set it to a number (e.g. 3) or to "{CANDIDATE_DEPTH_ALL}"',
        )
    return str(depth)


def _depth_limit(token: str) -> int | None:
    """``None`` means unbounded — the walk descends the whole subtree."""
    return None if token == CANDIDATE_DEPTH_ALL else int(token)


def _clean_rel(value: str | None) -> str:
    return str(value or "").strip().strip("/")


def _relative_posix(root: str, path: str) -> str:
    rel = os.path.relpath(path, root)
    if rel == ".":
        return ""
    return Path(rel).as_posix()


def _has_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def is_ignored(rel: str, patterns: Sequence[str]) -> bool:
    """``ignore_patterns`` are GLOBS over the vault-relative path (08 §A13).

    A pattern matches when it globs the whole relative path, when it is a
    path prefix of it (directory containment), or — for patterns without a
    ``/`` — when it globs any single path component.

    PUBLIC because the automation pipeline's ingestion walk applies the same
    ``vault.ignore_patterns`` (spec 06 §1) and had grown a second copy of
    this rule; two implementations of one rule drift. Consumer
    ``include_paths``/``exclude_paths`` are a DIFFERENT rule (06 §2:
    prefix-or-glob, no component match, so a bare ``resources`` means the
    top-level folder and nothing else) and deliberately stay in
    ``consumers/runner.py``.
    """
    if not rel:
        return False
    components = rel.split("/")
    for raw in patterns:
        pattern = _clean_rel(raw)
        if not pattern:
            continue
        if fnmatch.fnmatchcase(rel, pattern):
            return True
        if fnmatch.fnmatchcase(rel, pattern + "/*"):
            return True
        if "/" not in pattern and any(
            fnmatch.fnmatchcase(component, pattern) for component in components
        ):
            return True
        if not _has_glob(pattern) and rel.startswith(pattern + "/"):
            return True
    return False


def _indexable(rel: str, patterns: tuple[str, ...]) -> bool:
    """Same admission rule the walk applies, for the incremental path:
    markdown only, no dot-directories (``.obsidian``/``.git``/``.trash``),
    nothing matching ``ignore_patterns``."""
    if not rel or not rel.endswith(".md"):
        return False
    if any(component.startswith(".") for component in rel.split("/")[:-1]):
        return False
    return not is_ignored(rel, patterns)


def _body_only(path: Path) -> str:
    """Body text of a file whose frontmatter would not parse (never raises)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    _fm, body = frontmatter.split_frontmatter(text)
    return body


def _title_for(body: str, path: Path) -> str:
    """First ``# heading`` outside fenced code, else the filename stem.

    NEVER ``aliases[0]``: on a scalar alias that is the first CHARACTER of
    the string (08 §B10).
    """
    fenced = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if stripped.startswith("# "):
            heading = stripped[2:].strip()
            if heading:
                return heading
    return path.stem


def _scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, dict, tuple, set)):
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _string_list(values: list[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value is None:
            continue
        text = value if isinstance(value, str) else str(value)
        text = text.strip()
        if text:
            out.append(text)
    return out


def _record_tag_keys(record: NoteRecord, config: Config) -> set[str]:
    tag_map = config.suggestions.tag_normalization
    keys = {frontmatter.normalize_tag(str(tag), tag_map) for tag in record.tags}
    keys |= {frontmatter.normalize_tag(str(tag), tag_map) for tag in record.normalized_tags}
    return keys


def _normalized_set(values: list[str], tag_map: dict[str, str] | None) -> set[str]:
    return {
        frontmatter.normalize_tag(str(value), tag_map)
        for value in values or []
        if str(value).strip()
    }


def _haystack(record: NoteRecord) -> str:
    parts = [record.title, record.filename, *record.aliases, *record.tags]
    return "\n".join(str(part) for part in parts if part).casefold()


def _parse_epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for candidate in (text, text[:10]):
        normalized = candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None


def _order_key(record: NoteRecord) -> tuple[float, str]:
    """Oldest first by timestamp (fallback created_date, then mtime), with a
    deterministic path-ascending tiebreak (spec 03 §2)."""
    epoch = _parse_epoch(record.timestamp)
    if epoch is None:
        epoch = _parse_epoch(record.created_date)
    if epoch is None:
        epoch = record.modified
    return (epoch, record.path)


def _record_day(record: NoteRecord) -> str:
    """``YYYY-MM-DD`` used by since/until (timestamp → created_date → mtime)."""
    for value in (record.timestamp, record.created_date):
        if isinstance(value, str) and len(value.strip()) >= 10:
            head = value.strip()[:10]
            if _parse_epoch(head) is not None:
                return head
    return datetime.fromtimestamp(record.modified, tz=UTC).strftime("%Y-%m-%d")


_LIST_ATTRIBUTES = ("tags", "aliases", "sources", "modalities", "context", "normalized_tags")
_SCALAR_ATTRIBUTES = (
    "title",
    "filename",
    "para_type",
    "folder",
    "timestamp",
    "id",
    "capture_id",
    "processing_status",
    "created_date",
    "last_edited_date",
    "description",
)


def _values_for_key(record: NoteRecord, key: str) -> list[str]:
    """Values of one key on one record: promoted attributes first, then the
    same key from ``extra``/``metadata`` (spec 07 completion works for
    ARBITRARY configured metadata keys, not only the built-in schema)."""
    values: list[str] = []
    if key in _LIST_ATTRIBUTES:
        values.extend(str(item) for item in getattr(record, key))
    elif key in _SCALAR_ATTRIBUTES:
        value = getattr(record, key)
        if value is not None:
            values.append(str(value))
    for source in (record.extra, record.metadata):
        if not isinstance(source, dict) or key not in source:
            continue
        value = source[key]
        if value is None or isinstance(value, dict):
            continue
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value if item is not None)
        else:
            values.append(str(value))
    return values


def _record_from_json(key: str, entry: Any) -> NoteRecord | None:
    if not isinstance(entry, dict):
        return None
    known = {f.name for f in dataclass_fields(NoteRecord)}
    data = {name: value for name, value in entry.items() if name in known}
    data.setdefault("path", key)
    if not data.get("path") or not data.get("filename"):
        return None
    try:
        return NoteRecord(**data)
    except TypeError:
        return None
