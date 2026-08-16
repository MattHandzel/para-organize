"""File operations and safety guarantees (spec 05 — the core promise).

Invariants (spec 05 §1) made STRUCTURALLY unavoidable here:

- Every mutating operation takes an :class:`OperationContext` — you cannot
  call one without an operation log, a backup dir, an action recorder, and
  a dry-run flag. There is no bare ``move(src, dst)`` anywhere.
- Every mutating operation returns an :class:`OperationResult` carrying the
  backup path and the log line — callers can always undo by hand (05 §8).
- **Never delete** — every removal is a move into the archive tree.
- **Copy-then-archive** — failure before archive leaves the original in
  place; a stray duplicate is acceptable, data loss is not (05 §1.2).
- **Atomic writes** — temp file in the target's directory (unique via
  pid+counter), rename, permissions preserved, temp cleaned on failure,
  cross-filesystem fallback = copy+verify+unlink (05 §1.3, 05 §3).
- **Concurrent-modification check** (spec 10 §4) — mtime+hash captured at
  read; any mismatch at mutate time raises ConcurrentModificationError and
  touches nothing (the vault is Syncthing-synced).
- Global ``--dry-run`` (spec 09 §5.6): with ``ctx.dry_run`` no write, no
  archive, no log mutation — the returned OperationResult describes what
  WOULD happen (used for supervised first runs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from organize_core.actions import ActionRecorder
from organize_core.config import Config
from organize_core.index import NoteRecord, VaultIndex

OperationType = Literal["move", "archive", "merge", "append", "integrate", "create_folder", "metadata"]


@dataclass(frozen=True)
class LoggedOperation:
    """One operations.log line, parsed (spec 05 §1.5):
    ``[<ts>] <type>: <src> -> <dst> [SUCCESS|FAILED] Error: <msg>``."""

    ts: str
    type: OperationType
    src: str
    dst: str
    success: bool
    error: str | None = None
    backup: str | None = None


class OperationLog:
    """THE operation log (spec 05 §1.5). One instance, initialized at
    startup with the real file path (the original's logger init was never
    called — 08 §A16); backs both the file and the recent-ops API."""

    def __init__(self, log_file: Path) -> None:
        raise NotImplementedError

    def append(self, op: LoggedOperation) -> None:
        """Append one line; append failures are loud but never abort the
        already-completed file operation."""
        raise NotImplementedError

    def recent(self, n: int = 10) -> list[LoggedOperation]:
        """Tail for ``debug`` / health (05 §8)."""
        raise NotImplementedError

    def undo_info(self) -> LoggedOperation | None:
        """The last operation with enough detail to reverse it by hand
        (src, dst, backup path) — spec 05 §8."""
        raise NotImplementedError


@dataclass
class OperationContext:
    """Everything a mutating operation is REQUIRED to have. Constructed
    once per process/session by the core (or per-test from fixtures)."""

    config: Config
    index: VaultIndex
    oplog: OperationLog
    recorder: ActionRecorder  # every op emits an ActionRecord (12 §2)
    backup_dir: Path  # <vault>/<file_ops.backup_dir> (05 §1.4)
    dry_run: bool = False
    actor: str = "matt"  # ActionRecord actor (12 §2)
    session_id: str | None = None


@dataclass(frozen=True)
class OperationResult:
    """Uniform mutating-op result. ``ok=False`` ⇒ ``error`` set and the
    source file guaranteed still present (05 §1.2)."""

    ok: bool
    operation: OperationType
    source: str
    destination: str | None = None
    backup_path: str | None = None
    error: str | None = None
    dry_run: bool = False
    details: dict[str, Any] = field(default_factory=dict)


# --- primitives ------------------------------------------------------------


def atomic_write(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write-to-temp + rename per spec 05 §1.3. Temp in ``path.parent``,
    unique name via pid+counter; preserve existing file permissions; on any
    failure the target is untouched and the temp is removed."""
    raise NotImplementedError


def backup_file(path: Path, backup_dir: Path, *, now: float) -> Path:
    """Copy ``path`` to ``<backup_dir>/<%Y%m%d_%H%M%S>_<filename>``
    (spec 05 §1.4), creating the dir. Returns the backup path."""
    raise NotImplementedError


def collision_free_path(dest: Path) -> Path:
    """Append ``_1``, ``_2``… before the extension until free (05 §2.4)."""
    raise NotImplementedError


def get_archive_path(filename: str, config: Config, *, now: float) -> Path:
    """``<vault>/<para_folders.archives>/<archive_capture_path>/<filename>``
    KEEPING the original filename (wikilinks depend on it — 05 §3, 08 §A15);
    collision ⇒ ``_%Y%m%d_%H%M%S`` suffix."""
    raise NotImplementedError


@dataclass(frozen=True)
class FileSnapshot:
    """mtime + content hash captured at read time; the token the
    concurrent-modification check verifies (spec 10 §4)."""

    path: str
    mtime: float
    sha256: str


def snapshot_file(path: Path) -> FileSnapshot:
    raise NotImplementedError


def check_unmodified(snapshot: FileSnapshot) -> None:
    """Raise ConcurrentModificationError if the file no longer matches the
    snapshot (spec 10 §4). Called immediately before every mutation of an
    existing file."""
    raise NotImplementedError


# --- operations (each logs, records an ActionRecord, honors dry_run) -------


def move_to_destination(
    ctx: OperationContext,
    capture: NoteRecord,
    destination_folder: Path,
) -> OperationResult:
    """Spec 05 §2, exactly its 8 steps: validate source; ensure destination
    (mkdir -p iff ``auto_create_folders``); backup source; collision-free
    dest path; atomic copy; frontmatter update ON THE COPY (add tag
    ``<singular-type>/<folder-name>``, ``processing_status: organized``,
    ``last_edited_date`` today — tag merge preserves order/casing, 05 §2.6);
    archive the original (§3); log ``move`` + update index + ActionRecord.
    Takes the metadata RECORD (canonical decision, 05 §2); a path-only
    convenience lookup lives in the CLI layer."""
    raise NotImplementedError


def archive_capture(ctx: OperationContext, capture: NoteRecord) -> OperationResult:
    """Spec 05 §3: move original to :func:`get_archive_path` keeping its
    filename, mkdir as needed, rename with copy+verify+unlink fallback
    across filesystems, result CHECKED (08 §A15). Log ``archive``."""
    raise NotImplementedError


def merge_into_note(
    ctx: OperationContext,
    capture: NoteRecord,
    target_path: Path,
    *,
    edited_content: str | None = None,
    target_snapshot: FileSnapshot | None = None,
) -> OperationResult:
    """ONE merge implementation for both UI entry paths (spec 05 §4, 03 §5):
    backup target; result frontmatter = target's verbatim + tags union
    (normalized dedupe, target-first) + sources union (exact, target-first)
    + ``last_edited_date`` today; body = ``edited_content`` (interactive,
    already seeded per 03 §5) or the deterministic template
    ``target_body + "\\n\\n---\\n\\n## Merged from <fname> on <%Y-%m-%d %H:%M>\\n\\n" + capture_body``;
    atomic write; archive capture; log ``merge``; reindex target.
    ``target_snapshot`` (from ``merge_preview``) is verified before writing
    (10 §4)."""
    raise NotImplementedError


def merge_preview(
    ctx: OperationContext, capture: NoteRecord, target_path: Path
) -> tuple[str, FileSnapshot]:
    """The seeded merge-editor content (03 §5.1) + a snapshot for the later
    commit's concurrent-modification check. Read-only."""
    raise NotImplementedError


def append_to_note(
    ctx: OperationContext,
    capture: NoteRecord,
    target_path: Path,
    *,
    template: str | None = None,
) -> OperationResult:
    """Route ``append`` mode (spec 11 §1): capture body appended under
    ``## <date> — from <capture-id>`` (per-route ``template`` override);
    target frontmatter untouched except ``last_edited_date``; original
    archived BY THE ROUTE LAYER after all destinations succeed (11 §1
    multi-route rule — NOT here); logged as ``append``."""
    raise NotImplementedError


def update_frontmatter(
    ctx: OperationContext,
    path: Path,
    changes: dict[str, Any],
) -> OperationResult:
    """General primitive (spec 05 §5): read → parse → apply changes
    (``tags`` merges per the order-preserving rule; other fields replace) →
    round-trip-safe serialize → atomic write. Used by move step 6, metadata
    editing (07), merge. Logged as ``metadata``."""
    raise NotImplementedError


def update_tags(ctx: OperationContext, path: Path, new_tags: list[str]) -> OperationResult:
    """Thin wrapper over :func:`update_frontmatter` (spec 05 §5)."""
    raise NotImplementedError


def new_folder(ctx: OperationContext, para_type: str, name: str) -> OperationResult:
    """mkdir ``<vault>/<para_folders[para_type]>/<name>`` (spec 05 §6);
    validate name (no path separators, non-empty); log ``create_folder``;
    refresh index folder caches. The auto-move-current-capture behavior
    lives in the session layer."""
    raise NotImplementedError
