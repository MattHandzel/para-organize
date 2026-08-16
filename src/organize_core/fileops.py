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
  archive, no filesystem mutation of any kind — the returned
  OperationResult describes what WOULD happen (used for supervised first
  runs).

The ONE ``unlink``/``os.replace``-of-a-vault-file in this module is inside
:func:`_archive_file` and it runs only after the archived bytes have been
hash-verified at the archive path. Every other removal in the file is of a
temp file this module itself created microseconds earlier. That is the
never-delete invariant, enforced by construction and by
``tests/test_fileops_safety.py::test_no_vault_file_is_ever_destroyed``.

Builder decisions where the spec/skeleton left a choice
-------------------------------------------------------

*Clock injection.* ``OperationContext`` carries a ``clock`` callable
(default ``time.time``). Every timestamp in this module — oplog ``ts``,
backup filename, archive collision suffix, ``last_edited_date``, the merge
header, the ActionRecord ``ts``/``id`` — comes from it, so tests assert
EXACT strings instead of ">0" (spec 09 §3, the old suite's lesson). This is
an additive field with a default: no existing caller changes.

*UTC everywhere.* All rendered timestamps are UTC. Local time would make
the vault's archive/backup names depend on ``$TZ``; the spec does not
require local time and determinism is worth more.

*Dry-run still records.* ``ctx.dry_run`` suppresses every VAULT mutation
but the operation log line (state dir) and the ActionRecord are still
written, marked as dry runs — spec 09 §5.6 calls this "a dry-run/log-only
mode … diff intended actions against expectations", which is only useful if
the intended actions are written down. (The skeleton docstring said "no log
mutation"; the spec wins, per the ground rules.) The oplog marks them with
a third status token ``[DRY-RUN]``; the ActionRecord marks them with
``context.filters["dry_run"] = True`` because ``ActionContext`` (spec 12 §2,
owned by another module) has no dry-run field yet.

*Failure surface.* Ordinary operational failures (missing source,
unwritable destination, unknown PARA type) return ``OperationResult(ok=
False, error=…)`` and a ``[FAILED]`` log line — spec 05 §2.1 says "log +
``false, reason``". Only the two *refusals* raise, because they are
correctness stops rather than outcomes: :class:`ConcurrentModificationError`
(10 §4) and :class:`NoAiRefusal` (vault law, spec 02). A file whose
frontmatter cannot be round-tripped also raises (``FrontmatterError``, per
the tolerance contract in ``errors.py``: scans tolerate, mutations refuse).

*Copy+frontmatter-update are fused.* Spec 05 §2.5-2.6 writes the copy and
then rewrites its frontmatter. This module computes the updated content
first and performs ONE atomic write. Same end state, one fewer window in
which a half-organized copy exists, and the archived original still keeps
its untouched frontmatter (which is the point of "update on the copy").

*no-ai and actors.* The vault law (spec 02) forbids *automated/AI tooling*
from writing a ``no-ai: true`` note, while "the interactive plugin acts on
Matt's explicit keystrokes and may move such files". ``ctx.actor`` decides:
human actors (:data:`HUMAN_ACTORS`) may do everything; every other actor
(``claude-integrate``, ``consumer:*``, ``route:*``, ``auto-organize``) is
refused with :class:`NoAiRefusal` for any operation that WRITES note
content — move (it rewrites the copy's frontmatter), merge, append,
metadata/tag edits. Pure relocation (:func:`archive_capture`) and
:func:`new_folder` change no note content and stay allowed.
"""

from __future__ import annotations

import difflib
import errno
import hashlib
import itertools
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from organize_core.actions import (
    ActionContext,
    ActionRecord,
    ActionRecorder,
    CaptureState,
    TargetState,
    new_action_id,
)
from organize_core.config import Config
from organize_core.errors import ConcurrentModificationError, NoAiRefusal, OperationError
from organize_core.frontmatter import (
    Document,
    Frontmatter,
    is_no_ai,
    merge_sources,
    merge_tags,
    parse,
    serialize,
)
from organize_core.index import NoteRecord, VaultIndex

logger = logging.getLogger(__name__)

OperationType = Literal["move", "archive", "merge", "append", "integrate", "create_folder", "metadata"]

#: fileops op type -> spec 12 §2 ActionRecord operation name.
_ACTION_OPERATION: dict[str, str] = {
    "move": "move",
    "archive": "archive",
    "merge": "merge",
    "append": "append",
    "integrate": "integrate",
    "create_folder": "create_folder",
    "metadata": "meta_edit",
}

#: Actors that are Matt at a keyboard. Everything else is automated tooling
#: and is bound by the ``no-ai`` vault law (spec 02).
HUMAN_ACTORS: frozenset[str] = frozenset({"matt", "user", "human"})

#: Key under ``ActionContext.filters`` marking a dry run (see module docstring).
DRY_RUN_FILTER_KEY = "dry_run"

#: Default per-route append template (spec 11 §1). Placeholders are replaced
#: literally — NEVER ``str.format`` (08 §B15: templates contain literal ``{}``).
DEFAULT_APPEND_TEMPLATE = "## {date} — from {capture_id}\n\n{body}"

#: How many operations the in-memory recent-ops fallback keeps.
_RECENT_LIMIT = 500

#: Guard against a pathological collision loop (05 §2.4).
_MAX_COLLISION_SUFFIX = 10_000

_TMP_COUNTER = itertools.count()

#: Injection seam for the atomicity tests: the kill-window test replaces this
#: to fail *between* the temp write and the rename (05 §9 atomicity), and the
#: cross-filesystem test replaces it to raise ``EXDEV``.
_replace = os.replace


# --- small helpers ---------------------------------------------------------


def _one_line(text: str) -> str:
    """Collapse a value to a single log-line-safe fragment (one op == one line)."""
    return re.sub(r"\s+", " ", str(text)).strip()


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(now: float) -> str:
    return datetime.fromtimestamp(now, tz=UTC).strftime("%Y%m%d_%H%M%S")


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d")


def _minute(now: float) -> str:
    return datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: Path) -> str:
    """Every text read in this module: utf-8 with ``errors="replace"`` so a
    single bad byte can never crash an operation (06 §6 / 08 §B18)."""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _read_document(path: Path) -> tuple[Document, str]:
    """Read + parse. ``FrontmatterError`` propagates: a mutating operation
    must refuse to rewrite a note it cannot round-trip (errors.py contract)."""
    text = _read_text(path)
    return parse(text), text


def _fields(doc: Document) -> dict[str, Any]:
    return dict(doc.frontmatter.fields) if doc.frontmatter is not None else {}


def _style(doc: Document) -> dict[str, Any]:
    return doc.frontmatter.style if doc.frontmatter is not None else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_str_list(value: Any) -> list[str]:
    return [item if isinstance(item, str) else str(item) for item in _as_list(value)]


def _jsonable(value: Any) -> Any:
    """Coerce frontmatter values into JSON-safe shapes for the ActionRecord.
    A stray non-serializable scalar must never cost us the record."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def is_ai_actor(actor: str) -> bool:
    """True when ``actor`` is automated tooling bound by the ``no-ai`` vault
    law (spec 02). Human actors are :data:`HUMAN_ACTORS` and ``human:*``."""
    name = (actor or "").strip().lower()
    if name in HUMAN_ACTORS or name.startswith("human:"):
        return False
    return True


def _refuse_no_ai(ctx: OperationContext, path: Path, doc: Document, what: str) -> None:
    if not is_no_ai(doc) or not is_ai_actor(ctx.actor):
        return
    raise NoAiRefusal(
        f"{path} carries 'no-ai: true' and actor {ctx.actor!r} is automated tooling; "
        f"refusing to {what} it (vault law, spec 02)",
        hint="Only a human actor (actor='matt') may write this note; remove 'no-ai: true' to allow tooling.",
    )


# --- operation log ---------------------------------------------------------


@dataclass(frozen=True)
class LoggedOperation:
    """One operations.log line, parsed (spec 05 §1.5):
    ``[<ts>] <type>: <src> -> <dst> [SUCCESS|FAILED] Error: <msg>``.

    Two additive members beyond the spec's rendering: ``backup`` (spec 05 §8
    requires the undo info to carry the backup path, so the line has to
    carry it) and ``dry_run`` (rendered as the third status token
    ``[DRY-RUN]``). Both round-trip through :func:`format_log_line` /
    :func:`parse_log_line`.
    """

    ts: str
    type: OperationType
    src: str
    dst: str
    success: bool
    error: str | None = None
    backup: str | None = None
    dry_run: bool = False


_LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]*)\] (?P<type>[a-z_]+): (?P<body>.*) \[(?P<status>SUCCESS|FAILED|DRY-RUN)\](?P<rest>.*)$"
)


def format_log_line(op: LoggedOperation) -> str:
    """Render one operations.log line (spec 05 §1.5 format)."""
    if not op.success:
        status = "FAILED"
    elif op.dry_run:
        status = "DRY-RUN"
    else:
        status = "SUCCESS"
    line = f"[{_one_line(op.ts)}] {op.type}: {_one_line(op.src)} -> {_one_line(op.dst)} [{status}]"
    if op.backup:
        line += f" Backup: {_one_line(op.backup)}"
    if op.error:
        line += f" Error: {_one_line(op.error)}"
    return line


def parse_log_line(line: str) -> LoggedOperation | None:
    """Inverse of :func:`format_log_line`; ``None`` for anything unparseable
    (a corrupt line must never kill ``debug``/``health``)."""
    match = _LINE_RE.match(line.rstrip("\n"))
    if match is None:
        return None
    src, sep, dst = match.group("body").rpartition(" -> ")
    if not sep:
        src, dst = match.group("body"), ""
    rest = match.group("rest")
    backup: str | None = None
    error: str | None = None
    err_at = rest.find(" Error: ")
    if err_at >= 0:
        error = rest[err_at + len(" Error: ") :] or None
        rest = rest[:err_at]
    bak_at = rest.find(" Backup: ")
    if bak_at >= 0:
        backup = rest[bak_at + len(" Backup: ") :] or None
    status = match.group("status")
    return LoggedOperation(
        ts=match.group("ts"),
        type=match.group("type"),  # type: ignore[arg-type]
        src=src,
        dst=dst,
        success=status != "FAILED",
        error=error,
        backup=backup,
        dry_run=status == "DRY-RUN",
    )


class OperationLog:
    """THE operation log (spec 05 §1.5). One instance, initialized at
    startup with the real file path (the original's logger init was never
    called — 08 §A16); backs both the file and the recent-ops API.

    ``append`` is the only writer, so the file and the in-memory ring can
    never disagree about what this process did — 08 §A5's "recent ops read a
    global that doesn't exist" is structurally impossible here.
    """

    def __init__(self, log_file: Path) -> None:
        # Pure constructor: no mkdir, no open (08 §B2 — constructors that do
        # I/O take the whole process down). The file appears on first append.
        self.log_file = Path(log_file)
        self._recent: list[LoggedOperation] = []
        self._lock = threading.Lock()

    def append(self, op: LoggedOperation) -> None:
        """Append one line; append failures are loud but never abort the
        already-completed file operation."""
        with self._lock:
            self._recent.append(op)
            if len(self._recent) > _RECENT_LIMIT:
                del self._recent[: len(self._recent) - _RECENT_LIMIT]
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8", errors="replace", newline="\n") as handle:
                handle.write(format_log_line(op) + "\n")
        except OSError:
            logger.error(
                "OPERATION LOG WRITE FAILED for %s %s -> %s at %s — the file operation "
                "itself completed and is unaffected (spec 05 §1.5)",
                op.type,
                op.src,
                op.dst,
                self.log_file,
                exc_info=True,
            )

    def recent(self, n: int = 10) -> list[LoggedOperation]:
        """Tail for ``debug`` / health (05 §8). Reads the real file (so a
        fresh process sees history); falls back to this process's in-memory
        ring when the file is absent or unreadable."""
        if n <= 0:
            return []
        try:
            text = self.log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            with self._lock:
                return list(self._recent[-n:])
        parsed = [op for op in (parse_log_line(line) for line in text.splitlines()) if op is not None]
        return parsed[-n:]

    def undo_info(self) -> LoggedOperation | None:
        """The last operation with enough detail to reverse it by hand
        (src, dst, backup path) — spec 05 §8.

        "Last operation" means the last one that actually changed the vault:
        FAILED lines changed nothing and DRY-RUN lines by definition changed
        nothing, so neither is reversible and neither is returned.
        """
        for op in reversed(self.recent(_RECENT_LIMIT)):
            if op.success and not op.dry_run:
                return op
        return None


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
    #: Injected clock — the single time source for every timestamp this
    #: module renders (see module docstring). Tests pin it; production uses
    #: ``time.time``.
    clock: Callable[[], float] = time.time


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
    failure the target is untouched and the temp is removed.

    Fixes 08 §A25 wholesale: unique-per-write temp names (pid + process
    counter, not epoch seconds), permissions carried over, temp removed on
    every failure path including ``KeyboardInterrupt``.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    try:
        mode: int | None = os.stat(path).st_mode & 0o7777
    except OSError:
        mode = None

    tmp = directory / f".{path.name}.{os.getpid()}.{next(_TMP_COUNTER)}.organize-tmp"
    fd: int | None = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        handle = os.fdopen(fd, "w", encoding=encoding, errors="replace", newline="")
        fd = None  # ownership transferred to the file object
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        _replace(tmp, path)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - close failure on a fresh fd
                logger.debug("could not close temp fd for %s", tmp, exc_info=True)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - unlink of our own temp
            logger.error("could not remove temp file %s", tmp, exc_info=True)
        raise


def backup_file(path: Path, backup_dir: Path, *, now: float) -> Path:
    """Copy ``path`` to ``<backup_dir>/<%Y%m%d_%H%M%S>_<filename>``
    (spec 05 §1.4), creating the dir. Returns the backup path.

    Two backups in the same second get ``_1``, ``_2``… rather than
    overwriting each other (08 §A25 "collides on same-second writes").
    """
    path = Path(path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = collision_free_path(backup_dir / f"{_stamp(now)}_{path.name}")
    shutil.copy2(path, dest)
    return dest


def collision_free_path(dest: Path) -> Path:
    """Append ``_1``, ``_2``… before the extension until free (05 §2.4)."""
    dest = Path(dest)
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for index in range(1, _MAX_COLLISION_SUFFIX):
        candidate = dest.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise OperationError(
        f"could not find a free filename for {dest} after {_MAX_COLLISION_SUFFIX} attempts",
        hint="Clean up the numbered duplicates in that folder.",
    )


def get_archive_path(filename: str, config: Config, *, now: float) -> Path:
    """``<vault>/<para_folders.archives>/<archive_capture_path>/<filename>``
    KEEPING the original filename (wikilinks depend on it — 05 §3, 08 §A15);
    collision ⇒ ``_%Y%m%d_%H%M%S`` suffix.

    Nothing here renames to ``<id>.md`` and nothing hardcodes
    ``/archives/capture/raw_capture/`` — both are 08 §A15 defects.
    """
    archives = config.vault.para_folders.get("archives")
    if not archives:
        raise OperationError(
            "vault.para_folders has no 'archives' entry, so there is nowhere to archive to",
            hint="Set [vault].para_folders.archives (Matt's vault uses 'archive', SINGULAR — 08 §C2).",
        )
    base = Path(config.vault.root) / archives / config.vault.archive_capture_path / filename
    if not base.exists():
        return base
    return collision_free_path(base.with_name(f"{base.stem}_{_stamp(now)}{base.suffix}"))


@dataclass(frozen=True)
class FileSnapshot:
    """mtime + content hash captured at read time; the token the
    concurrent-modification check verifies (spec 10 §4)."""

    path: str
    mtime: float
    sha256: str


def snapshot_file(path: Path) -> FileSnapshot:
    path = Path(path)
    stat = path.stat()
    return FileSnapshot(path=str(path), mtime=stat.st_mtime, sha256=_hash_file(path))


def check_unmodified(snapshot: FileSnapshot) -> None:
    """Raise ConcurrentModificationError if the file no longer matches the
    snapshot (spec 10 §4). Called immediately before every mutation of an
    existing file.

    Either signal is enough: a changed mtime with identical bytes still
    means someone else wrote the file while we held a stale read, and the
    spec's rule is "refuse rather than clobber".
    """
    path = Path(snapshot.path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise ConcurrentModificationError(
            f"{path} disappeared while organize was working on it",
            hint="Re-read the note and retry; nothing was written.",
        ) from exc
    if stat.st_mtime != snapshot.mtime:
        raise ConcurrentModificationError(
            f"{path} changed on disk while organize was working on it "
            f"(mtime {snapshot.mtime!r} -> {stat.st_mtime!r})",
            hint="The vault is Syncthing-synced; re-read the note and retry. Nothing was written.",
        )
    current = _hash_file(path)
    if current != snapshot.sha256:
        raise ConcurrentModificationError(
            f"{path} changed on disk while organize was working on it "
            f"(sha256 {snapshot.sha256[:12]}… -> {current[:12]}…)",
            hint="The vault is Syncthing-synced; re-read the note and retry. Nothing was written.",
        )


# --- internal plumbing shared by every operation ---------------------------


def _log(
    ctx: OperationContext,
    op_type: OperationType,
    src: Path | str,
    dst: Path | str | None,
    *,
    success: bool,
    now: float,
    error: str | None = None,
    backup: Path | str | None = None,
) -> LoggedOperation | None:
    """Append the spec 05 §1.5 line when ``file_ops.log_operations``."""
    if not ctx.config.file_ops.log_operations:
        return None
    op = LoggedOperation(
        ts=_iso(now),
        type=op_type,
        src=str(src),
        dst=str(dst) if dst is not None else "",
        success=success,
        error=error,
        backup=str(backup) if backup is not None else None,
        dry_run=ctx.dry_run,
    )
    ctx.oplog.append(op)
    return op


def _vault_stats(ctx: OperationContext) -> dict[str, int]:
    """Best-effort ``context.vault_stats`` (spec 12 §2). The index is a
    collaborator we do not control; a failure here must never cost an op."""
    try:
        raw = ctx.index.stats()
    except Exception:  # noqa: BLE001 - stats is advisory, never load-bearing
        logger.debug("index.stats() unavailable for the action record", exc_info=True)
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _capture_state(
    path: Path,
    text: str,
    doc: Document,
    *,
    fields_after: Mapping[str, Any] | None = None,
) -> CaptureState:
    return CaptureState(
        path=str(path),
        content_hash=_hash_text(text),
        frontmatter_before=_jsonable(_fields(doc)),
        body_before=doc.body,
        frontmatter_after=_jsonable(dict(fields_after)) if fields_after is not None else None,
    )


def _unified_diff(before: str, after: str, path: Path) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _target_state(
    path: Path,
    before: str | None,
    after: str,
    role: str,
    *,
    description: str | None = None,
) -> TargetState:
    return TargetState(
        path=str(path),
        role=role,  # type: ignore[arg-type]
        before_hash=_hash_text(before) if before is not None else None,
        after_hash=_hash_text(after),
        diff=_unified_diff(before or "", after, path),
        description=description,
    )


def _record_action(
    ctx: OperationContext,
    *,
    op_type: OperationType,
    capture: CaptureState,
    targets: Sequence[TargetState] = (),
    now: float,
    edit_mode: str | None = None,
    route: str | None = None,
    action: str | None = None,
) -> None:
    """Emit the spec 12 §2 ActionRecord for a COMPLETED operation.

    Only successful operations are recorded: the schema has no "it failed"
    slot, and a corpus that claims moves which never happened is worse than
    a corpus with gaps. Failures live in the operation log's FAILED lines.
    """
    try:
        record = ActionRecord(
            id=new_action_id(now=now),
            ts=_iso(now),
            actor=ctx.actor,
            operation=action or _ACTION_OPERATION[op_type],  # type: ignore[arg-type]
            capture=capture,
            targets=tuple(targets),
            context=ActionContext(
                session_id=ctx.session_id,
                filters={DRY_RUN_FILTER_KEY: True} if ctx.dry_run else {},
                route=route,
                vault_stats=_vault_stats(ctx),
            ),
            edit_mode=edit_mode,  # type: ignore[arg-type]
        )
    except Exception:  # noqa: BLE001 - recording never blocks the operation
        logger.error(
            "ACTION RECORD BUILD FAILED for %s on %s — the operation itself completed "
            "and is unaffected (spec 12 §2)",
            op_type,
            capture.path if capture is not None else "<unknown>",
            exc_info=True,
        )
        return
    ctx.recorder.record(record)


def _index_update(ctx: OperationContext, path: Path) -> None:
    try:
        ctx.index.update_file(path)
    except Exception:  # noqa: BLE001 - the file op already succeeded
        logger.error("index update failed for %s (the file operation succeeded)", path, exc_info=True)


def _index_remove(ctx: OperationContext, path: Path) -> None:
    try:
        ctx.index.remove_file(path)
    except Exception:  # noqa: BLE001 - the file op already succeeded
        logger.error("index removal failed for %s (the file operation succeeded)", path, exc_info=True)


def _fail(
    ctx: OperationContext,
    op_type: OperationType,
    src: Path | str,
    dst: Path | str | None,
    message: str,
    now: float,
    *,
    backup: Path | None = None,
    details: dict[str, Any] | None = None,
) -> OperationResult:
    logger.error("%s failed: %s", op_type, message)
    _log(ctx, op_type, src, dst, success=False, now=now, error=message, backup=backup)
    return OperationResult(
        ok=False,
        operation=op_type,
        source=str(src),
        destination=str(dst) if dst is not None else None,
        backup_path=str(backup) if backup is not None else None,
        error=message,
        dry_run=ctx.dry_run,
        details=details or {},
    )


def _archive_file(source: Path, archive_path: Path) -> tuple[Path | None, str | None]:
    """The ONE removal in this module (spec 05 §1.1/§3).

    ``os.replace`` when the archive is on the same filesystem; on ``EXDEV``
    copy, hash-verify the copy, and only then unlink the original. Either
    way the archived bytes are hash-verified against the source *before*
    this returns success, and the result of the rename is CHECKED — the
    original ignored it and silently no-opped across filesystems (08 §A15).
    """
    try:
        source_hash = _hash_file(source)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _replace(source, archive_path)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.copy2(source, archive_path)
            copied = _hash_file(archive_path)
            if copied != source_hash:
                return None, (
                    f"cross-filesystem archive copy of {source} to {archive_path} did not verify "
                    f"(sha256 {source_hash[:12]}… != {copied[:12]}…); the original was NOT removed"
                )
            os.unlink(source)
        if not archive_path.is_file():
            return None, f"archive target {archive_path} does not exist after the move"
        archived_hash = _hash_file(archive_path)
        if archived_hash != source_hash:
            return None, (
                f"archived bytes at {archive_path} do not match the original "
                f"(sha256 {source_hash[:12]}… != {archived_hash[:12]}…)"
            )
        return archive_path, None
    except OSError as exc:
        return None, f"could not archive {source} to {archive_path}: {exc}"


def _para_key_for(rel_parts: Sequence[str], config: Config) -> tuple[str | None, int]:
    """Which ``para_folders`` key owns a vault-relative path, and how many
    leading components that folder consumed."""
    best_key: str | None = None
    best_len = 0
    for key, folder in config.vault.para_folders.items():
        parts = Path(str(folder)).parts
        if not parts or len(parts) > len(rel_parts):
            continue
        if tuple(rel_parts[: len(parts)]) == parts and len(parts) > best_len:
            best_key, best_len = key, len(parts)
    return best_key, best_len


def _destination_tag(destination_folder: Path, config: Config) -> str | None:
    """``<singular-type>/<folder-name>`` for a PARA destination (05 §2.6).

    Singular = the configured folder KEY minus a trailing "s"
    (``projects`` → ``project``), so a renamed folder value never breaks the
    tag. ``None`` when the destination is outside every PARA root — no tag
    is invented for a folder the config does not describe.
    """
    root = Path(config.vault.root).resolve()
    try:
        rel = destination_folder.resolve().relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    key, consumed = _para_key_for(parts, config)
    if key is None:
        return None
    singular = key[:-1] if key.endswith("s") else key
    if len(parts) == consumed:
        return singular
    return f"{singular}/{parts[-1]}"


def _organized_fields(
    doc: Document, destination_folder: Path, config: Config, now: float
) -> tuple[dict[str, Any], str | None]:
    """Spec 05 §2.6 applied to a parsed capture: add the destination tag,
    ``processing_status: organized``, ``last_edited_date`` = today.

    Tag merge dedupes case-insensitively but PRESERVES the existing order
    and casing, appending new tags at the end (08 §A24: the original
    re-sorted and re-cased Matt's list).
    """
    fields = dict(_fields(doc))
    tag = _destination_tag(destination_folder, config)
    if tag is not None:
        fields["tags"] = merge_tags(_as_str_list(fields.get("tags")), [tag])
    fields["processing_status"] = "organized"
    fields["last_edited_date"] = _today(now)
    return fields, tag


def _rendered(fields: Mapping[str, Any], style: Mapping[str, Any], body: str) -> str:
    return serialize(Document(frontmatter=Frontmatter(fields=dict(fields), style=dict(style)), body=body))


def _render_template(template: str, values: Mapping[str, str]) -> str:
    """Placeholder substitution WITHOUT ``str.format`` — a template
    containing literal braces (``{"a": 1}`` in a code fence) must not raise
    (08 §B15)."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _merged_body(target_body: str, capture_body: str, capture_filename: str, now: float) -> str:
    """Spec 05 §4 body template, literally."""
    header = f"## Merged from {capture_filename} on {_minute(now)}"
    return f"{target_body}\n\n---\n\n{header}\n\n{capture_body}"


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
    Takes the metadata RECORD (canonical decision, 05 §2; 08 §A14 — one
    signature, never a bare string); a path-only convenience lookup lives in
    the CLI layer.
    """
    now = ctx.clock()
    source = Path(capture.path)
    dest_folder = Path(destination_folder)

    if not source.is_file():
        return _fail(ctx, "move", source, dest_folder, f"source note does not exist: {source}", now)

    if not dest_folder.is_dir():
        if not ctx.config.file_ops.auto_create_folders:
            return _fail(
                ctx,
                "move",
                source,
                dest_folder,
                f"destination folder does not exist: {dest_folder}",
                now,
            )
        if not ctx.dry_run:
            try:
                dest_folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return _fail(
                    ctx, "move", source, dest_folder, f"could not create {dest_folder}: {exc}", now
                )

    doc, text = _read_document(source)
    _refuse_no_ai(ctx, source, doc, "move")

    snapshot = snapshot_file(source)
    dest_path = collision_free_path(dest_folder / source.name)
    fields_after, tag = _organized_fields(doc, dest_folder, ctx.config, now)
    new_text = _rendered(fields_after, _style(doc), doc.body)
    archive_path = get_archive_path(source.name, ctx.config, now=now)

    capture_state = _capture_state(source, text, doc, fields_after=fields_after)
    details: dict[str, Any] = {
        "archive_path": str(archive_path),
        "tag_added": tag,
        "processing_status": "organized",
        "last_edited_date": _today(now),
    }

    if ctx.dry_run:
        _log(ctx, "move", source, dest_path, success=True, now=now)
        _log(ctx, "archive", source, archive_path, success=True, now=now)
        _record_action(
            ctx,
            op_type="move",
            capture=capture_state,
            targets=[_target_state(dest_path, None, new_text, "destination")],
            now=now,
        )
        return OperationResult(
            ok=True,
            operation="move",
            source=str(source),
            destination=str(dest_path),
            dry_run=True,
            details=details,
        )

    check_unmodified(snapshot)

    backup: Path | None = None
    if ctx.config.file_ops.create_backups:
        try:
            backup = backup_file(source, ctx.backup_dir, now=now)
        except OSError as exc:
            return _fail(ctx, "move", source, dest_path, f"could not back up {source}: {exc}", now)

    check_unmodified(snapshot)

    try:
        atomic_write(dest_path, new_text)
    except OSError as exc:
        # Nothing was archived: the original is exactly where it was (05 §1.2).
        return _fail(
            ctx,
            "move",
            source,
            dest_path,
            f"could not write the copy to {dest_path}: {exc}",
            now,
            backup=backup,
            details=details,
        )

    written = _read_text(dest_path)
    if written != new_text:
        return _fail(
            ctx,
            "move",
            source,
            dest_path,
            f"copy verification failed at {dest_path}; the original was NOT archived",
            now,
            backup=backup,
            details=details,
        )

    archived, archive_error = _archive_file(source, archive_path)
    if archived is None:
        _log(ctx, "archive", source, archive_path, success=False, now=now, error=archive_error)
        return _fail(
            ctx,
            "move",
            source,
            dest_path,
            f"copied to {dest_path} but archiving the original failed: {archive_error}",
            now,
            backup=backup,
            details=details,
        )
    _log(ctx, "archive", source, archived, success=True, now=now, backup=backup)

    _index_remove(ctx, source)
    _index_update(ctx, dest_path)
    _index_update(ctx, archived)

    _log(ctx, "move", source, dest_path, success=True, now=now, backup=backup)
    _record_action(
        ctx,
        op_type="move",
        capture=capture_state,
        targets=[_target_state(dest_path, None, new_text, "destination")],
        now=now,
    )
    return OperationResult(
        ok=True,
        operation="move",
        source=str(source),
        destination=str(dest_path),
        backup_path=str(backup) if backup else None,
        details=details,
    )


def archive_capture(ctx: OperationContext, capture: NoteRecord) -> OperationResult:
    """Spec 05 §3: move original to :func:`get_archive_path` keeping its
    filename, mkdir as needed, rename with copy+verify+unlink fallback
    across filesystems, result CHECKED (08 §A15). Log ``archive``.

    Pure relocation: the note's bytes are unchanged, so this is allowed for
    automated actors even on ``no-ai`` notes (spec 02 forbids *writing*
    them).
    """
    now = ctx.clock()
    source = Path(capture.path)
    if not source.is_file():
        return _fail(ctx, "archive", source, None, f"source note does not exist: {source}", now)

    archive_path = get_archive_path(source.name, ctx.config, now=now)
    doc, text = _read_document(source)
    capture_state = _capture_state(source, text, doc)

    if ctx.dry_run:
        _log(ctx, "archive", source, archive_path, success=True, now=now)
        _record_action(
            ctx,
            op_type="archive",
            capture=capture_state,
            targets=[_target_state(archive_path, None, text, "destination")],
            now=now,
        )
        return OperationResult(
            ok=True,
            operation="archive",
            source=str(source),
            destination=str(archive_path),
            dry_run=True,
            details={"archive_path": str(archive_path)},
        )

    backup: Path | None = None
    if ctx.config.file_ops.create_backups:
        try:
            backup = backup_file(source, ctx.backup_dir, now=now)
        except OSError as exc:
            return _fail(
                ctx, "archive", source, archive_path, f"could not back up {source}: {exc}", now
            )

    archived, archive_error = _archive_file(source, archive_path)
    if archived is None:
        return _fail(ctx, "archive", source, archive_path, archive_error or "", now, backup=backup)

    _index_remove(ctx, source)
    _index_update(ctx, archived)
    _log(ctx, "archive", source, archived, success=True, now=now, backup=backup)
    _record_action(
        ctx,
        op_type="archive",
        capture=capture_state,
        targets=[_target_state(archived, None, text, "destination")],
        now=now,
    )
    return OperationResult(
        ok=True,
        operation="archive",
        source=str(source),
        destination=str(archived),
        backup_path=str(backup) if backup else None,
        details={"archive_path": str(archived)},
    )


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
    (10 §4).

    Spec resolution (03 §5.3 says "body = the edited buffer content" while
    §5.1 seeds that buffer WITH the frontmatter): when ``edited_content``
    carries a frontmatter block, that block is the base — Matt's own edits
    to it are kept — and the tags/sources union plus ``last_edited_date``
    are applied on top. Without a block the target's fields are the base.
    Either way no target field is ever dropped, and the browse path can no
    longer paste its instruction text into the file (08 §A32).
    """
    now = ctx.clock()
    source = Path(capture.path)
    target_path = Path(target_path)

    if not source.is_file():
        return _fail(ctx, "merge", source, target_path, f"capture does not exist: {source}", now)
    if not target_path.is_file():
        return _fail(ctx, "merge", source, target_path, f"merge target does not exist: {target_path}", now)

    target_doc, target_text = _read_document(target_path)
    _refuse_no_ai(ctx, target_path, target_doc, "merge into")
    capture_doc, capture_text = _read_document(source)

    if target_snapshot is not None:
        check_unmodified(target_snapshot)
    snapshot = target_snapshot or snapshot_file(target_path)

    if edited_content is not None:
        edited_doc = parse(edited_content)
        base_fields = dict(_fields(edited_doc)) if edited_doc.has_frontmatter else dict(_fields(target_doc))
        base_style = _style(edited_doc) if edited_doc.has_frontmatter else _style(target_doc)
        body = edited_doc.body
    else:
        base_fields = dict(_fields(target_doc))
        base_style = _style(target_doc)
        body = _merged_body(target_doc.body, capture_doc.body, source.name, now)

    capture_fields = _fields(capture_doc)
    tags = merge_tags(_as_str_list(base_fields.get("tags")), _as_str_list(capture_fields.get("tags")))
    if tags:
        base_fields["tags"] = tags
    sources = merge_sources(
        _as_str_list(base_fields.get("sources")), _as_str_list(capture_fields.get("sources"))
    )
    if sources:
        base_fields["sources"] = sources
    base_fields["last_edited_date"] = _today(now)

    new_text = _rendered(base_fields, base_style, body)
    archive_path = get_archive_path(source.name, ctx.config, now=now)
    capture_state = _capture_state(source, capture_text, capture_doc)
    targets = [_target_state(target_path, target_text, new_text, "merge_target")]
    details: dict[str, Any] = {"archive_path": str(archive_path), "tags": tags, "sources": sources}

    if ctx.dry_run:
        _log(ctx, "merge", source, target_path, success=True, now=now)
        _log(ctx, "archive", source, archive_path, success=True, now=now)
        _record_action(
            ctx, op_type="merge", capture=capture_state, targets=targets, now=now, edit_mode="manual"
        )
        return OperationResult(
            ok=True,
            operation="merge",
            source=str(source),
            destination=str(target_path),
            dry_run=True,
            details=details,
        )

    check_unmodified(snapshot)

    backup: Path | None = None
    if ctx.config.file_ops.create_backups:
        try:
            backup = backup_file(target_path, ctx.backup_dir, now=now)
        except OSError as exc:
            return _fail(
                ctx, "merge", source, target_path, f"could not back up {target_path}: {exc}", now
            )

    check_unmodified(snapshot)

    try:
        atomic_write(target_path, new_text)
    except OSError as exc:
        return _fail(
            ctx,
            "merge",
            source,
            target_path,
            f"could not write {target_path}: {exc}",
            now,
            backup=backup,
            details=details,
        )

    archived, archive_error = _archive_file(source, archive_path)
    if archived is None:
        _log(ctx, "archive", source, archive_path, success=False, now=now, error=archive_error)
        return _fail(
            ctx,
            "merge",
            source,
            target_path,
            f"merged into {target_path} but archiving the capture failed: {archive_error}",
            now,
            backup=backup,
            details=details,
        )
    _log(ctx, "archive", source, archived, success=True, now=now)

    _index_update(ctx, target_path)
    _index_remove(ctx, source)
    _index_update(ctx, archived)
    _log(ctx, "merge", source, target_path, success=True, now=now, backup=backup)
    _record_action(
        ctx, op_type="merge", capture=capture_state, targets=targets, now=now, edit_mode="manual"
    )
    return OperationResult(
        ok=True,
        operation="merge",
        source=str(source),
        destination=str(target_path),
        backup_path=str(backup) if backup else None,
        details=details,
    )


def merge_preview(
    ctx: OperationContext, capture: NoteRecord, target_path: Path
) -> tuple[str, FileSnapshot]:
    """The seeded merge-editor content (03 §5.1) + a snapshot for the later
    commit's concurrent-modification check. Read-only.

    The seed is the target's frontmatter + body, the separator, and the
    capture's body under the merge header — and NOTHING else. Instructional
    text is the client's virtual text and can never reach the buffer, so it
    can never reach the file (08 §A32).
    """
    now = ctx.clock()
    source = Path(capture.path)
    target_path = Path(target_path)
    if not source.is_file():
        raise OperationError(f"capture does not exist: {source}")
    if not target_path.is_file():
        raise OperationError(f"merge target does not exist: {target_path}")
    target_doc, _ = _read_document(target_path)
    capture_doc, _ = _read_document(source)
    body = _merged_body(target_doc.body, capture_doc.body, source.name, now)
    content = _rendered(_fields(target_doc), _style(target_doc), body)
    return content, snapshot_file(target_path)


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
    multi-route rule — NOT here); logged as ``append``.

    Placeholders (``{date}``, ``{capture_id}``, ``{body}``, ``{filename}``)
    are substituted by literal replacement, never ``str.format`` — a
    template containing a literal ``{`` must not raise (08 §B15).
    """
    now = ctx.clock()
    source = Path(capture.path)
    target_path = Path(target_path)

    if not source.is_file():
        return _fail(ctx, "append", source, target_path, f"capture does not exist: {source}", now)
    if not target_path.is_file():
        return _fail(ctx, "append", source, target_path, f"append target does not exist: {target_path}", now)

    target_doc, target_text = _read_document(target_path)
    _refuse_no_ai(ctx, target_path, target_doc, "append to")
    capture_doc, capture_text = _read_document(source)

    snapshot = snapshot_file(target_path)
    capture_id = capture.capture_id or capture.id or source.stem
    block = _render_template(
        template or DEFAULT_APPEND_TEMPLATE,
        {
            "date": _today(now),
            "capture_id": capture_id,
            "body": capture_doc.body.strip("\n"),
            "filename": source.name,
        },
    )
    if not block.endswith("\n"):
        block += "\n"
    base_body = target_doc.body
    if base_body and not base_body.endswith("\n"):
        base_body += "\n"
    new_body = f"{base_body}\n{block}"

    fields = dict(_fields(target_doc))
    fields["last_edited_date"] = _today(now)
    new_text = _rendered(fields, _style(target_doc), new_body)

    capture_state = _capture_state(source, capture_text, capture_doc)
    targets = [_target_state(target_path, target_text, new_text, "append_target")]
    details: dict[str, Any] = {"capture_id": capture_id, "archived": False}

    if ctx.dry_run:
        _log(ctx, "append", source, target_path, success=True, now=now)
        _record_action(
            ctx, op_type="append", capture=capture_state, targets=targets, now=now, edit_mode="append"
        )
        return OperationResult(
            ok=True,
            operation="append",
            source=str(source),
            destination=str(target_path),
            dry_run=True,
            details=details,
        )

    check_unmodified(snapshot)

    backup: Path | None = None
    if ctx.config.file_ops.create_backups:
        try:
            backup = backup_file(target_path, ctx.backup_dir, now=now)
        except OSError as exc:
            return _fail(
                ctx, "append", source, target_path, f"could not back up {target_path}: {exc}", now
            )

    check_unmodified(snapshot)

    try:
        atomic_write(target_path, new_text)
    except OSError as exc:
        return _fail(
            ctx,
            "append",
            source,
            target_path,
            f"could not write {target_path}: {exc}",
            now,
            backup=backup,
            details=details,
        )

    _index_update(ctx, target_path)
    _log(ctx, "append", source, target_path, success=True, now=now, backup=backup)
    _record_action(
        ctx, op_type="append", capture=capture_state, targets=targets, now=now, edit_mode="append"
    )
    return OperationResult(
        ok=True,
        operation="append",
        source=str(source),
        destination=str(target_path),
        backup_path=str(backup) if backup else None,
        details=details,
    )


def _apply_frontmatter_changes(
    ctx: OperationContext,
    path: Path,
    changes: Mapping[str, Any],
    *,
    op_type: OperationType,
    action: str,
) -> OperationResult:
    now = ctx.clock()
    path = Path(path)
    if not path.is_file():
        return _fail(ctx, op_type, path, path, f"note does not exist: {path}", now)

    doc, text = _read_document(path)
    _refuse_no_ai(ctx, path, doc, "edit the frontmatter of")
    snapshot = snapshot_file(path)

    fields = dict(_fields(doc))
    for key, value in changes.items():
        if value is None:
            fields.pop(key, None)
            continue
        if key == "tags":
            fields["tags"] = merge_tags(_as_str_list(fields.get("tags")), _as_str_list(value))
        else:
            fields[key] = value

    new_text = _rendered(fields, _style(doc), doc.body)
    capture_state = _capture_state(path, text, doc, fields_after=fields)
    targets = [_target_state(path, text, new_text, "destination")]
    details: dict[str, Any] = {"changed": new_text != text, "keys": sorted(changes)}

    if new_text == text:
        # Nothing to write: no backup, no mutation, no churned mtime.
        _log(ctx, op_type, path, path, success=True, now=now)
        _record_action(ctx, op_type=op_type, action=action, capture=capture_state, targets=targets, now=now)
        return OperationResult(
            ok=True,
            operation=op_type,
            source=str(path),
            destination=str(path),
            dry_run=ctx.dry_run,
            details=details,
        )

    if ctx.dry_run:
        _log(ctx, op_type, path, path, success=True, now=now)
        _record_action(ctx, op_type=op_type, action=action, capture=capture_state, targets=targets, now=now)
        return OperationResult(
            ok=True,
            operation=op_type,
            source=str(path),
            destination=str(path),
            dry_run=True,
            details=details,
        )

    check_unmodified(snapshot)

    backup: Path | None = None
    if ctx.config.file_ops.create_backups:
        try:
            backup = backup_file(path, ctx.backup_dir, now=now)
        except OSError as exc:
            return _fail(ctx, op_type, path, path, f"could not back up {path}: {exc}", now)

    check_unmodified(snapshot)

    try:
        atomic_write(path, new_text)
    except OSError as exc:
        return _fail(
            ctx, op_type, path, path, f"could not write {path}: {exc}", now, backup=backup, details=details
        )

    _index_update(ctx, path)
    _log(ctx, op_type, path, path, success=True, now=now, backup=backup)
    _record_action(ctx, op_type=op_type, action=action, capture=capture_state, targets=targets, now=now)
    return OperationResult(
        ok=True,
        operation=op_type,
        source=str(path),
        destination=str(path),
        backup_path=str(backup) if backup else None,
        details=details,
    )


def update_frontmatter(
    ctx: OperationContext,
    path: Path,
    changes: dict[str, Any],
) -> OperationResult:
    """General primitive (spec 05 §5): read → parse → apply changes
    (``tags`` merges per the order-preserving rule; other fields replace) →
    round-trip-safe serialize → atomic write. Used by move step 6, metadata
    editing (07), merge. Logged as ``metadata``.

    Every field outside the change set survives byte-for-byte, known or
    unknown (``no-ai``, ``title``, Obsidian properties…) — 08 §A12, the
    worst data-loss bug in the original, at the operation level. A change
    value of ``None`` REMOVES the field (07 needs a way to unset one).
    """
    return _apply_frontmatter_changes(
        ctx, path, changes, op_type="metadata", action="meta_edit"
    )


def update_tags(ctx: OperationContext, path: Path, new_tags: list[str]) -> OperationResult:
    """Thin wrapper over :func:`update_frontmatter` (spec 05 §5).

    Merge preserves the user's order and casing and appends new tags at the
    end — it does NOT re-sort or re-case the list (08 §A24).
    """
    return _apply_frontmatter_changes(
        ctx, path, {"tags": list(new_tags)}, op_type="metadata", action="tag_edit"
    )


def new_folder(ctx: OperationContext, para_type: str, name: str) -> OperationResult:
    """mkdir ``<vault>/<para_folders[para_type]>/<name>`` (spec 05 §6);
    validate name (no path separators, non-empty); log ``create_folder``;
    refresh index folder caches. The auto-move-current-capture behavior
    lives in the session layer.

    ``para_type`` accepts either the config KEY (``projects``) or its
    singular (``project``), because the UI's ``<leader>np`` speaks singular.
    """
    now = ctx.clock()
    folders = ctx.config.vault.para_folders
    key = para_type if para_type in folders else next(
        (k for k in folders if k[:-1] == para_type or k == f"{para_type}s"), None
    )
    root = Path(ctx.config.vault.root)
    if key is None:
        return _fail(
            ctx,
            "create_folder",
            root,
            None,
            f"unknown PARA type {para_type!r}; known types: {sorted(folders)}",
            now,
        )

    clean = (name or "").strip()
    if not clean:
        return _fail(ctx, "create_folder", root, None, "folder name is empty", now)
    if "/" in clean or "\\" in clean or clean in {".", ".."} or "\x00" in clean:
        return _fail(
            ctx,
            "create_folder",
            root,
            None,
            f"folder name {name!r} contains a path separator; only a single folder name is allowed",
            now,
        )

    folder = root / folders[key] / clean
    capture_state = CaptureState(
        path=str(folder), content_hash="", frontmatter_before={}, body_before=""
    )
    existed = folder.is_dir()
    details: dict[str, Any] = {"para_type": key, "created": not existed}

    if ctx.dry_run:
        _log(ctx, "create_folder", root, folder, success=True, now=now)
        _record_action(ctx, op_type="create_folder", capture=capture_state, now=now)
        return OperationResult(
            ok=True,
            operation="create_folder",
            source=str(root),
            destination=str(folder),
            dry_run=True,
            details=details,
        )

    if not existed:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(ctx, "create_folder", root, folder, f"could not create {folder}: {exc}", now)

    _log(ctx, "create_folder", root, folder, success=True, now=now)
    _record_action(ctx, op_type="create_folder", capture=capture_state, now=now)
    return OperationResult(
        ok=True,
        operation="create_folder",
        source=str(root),
        destination=str(folder),
        details=details,
    )


def vault_tree(root: Path, *, skip: Iterable[str] = ()) -> dict[str, str]:
    """Every file under ``root`` mapped to its sha256 — the shape the
    never-delete and dry-run invariants are asserted against. Exposed
    because those two invariants belong to callers as much as to tests."""
    skipped = tuple(skip)
    tree: dict[str, str] = {}
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        if any(rel.startswith(prefix) for prefix in skipped):
            continue
        tree[rel] = _hash_file(path)
    return tree


__all__ = [
    "DEFAULT_APPEND_TEMPLATE",
    "DRY_RUN_FILTER_KEY",
    "HUMAN_ACTORS",
    "FileSnapshot",
    "LoggedOperation",
    "OperationContext",
    "OperationLog",
    "OperationResult",
    "OperationType",
    "append_to_note",
    "archive_capture",
    "atomic_write",
    "backup_file",
    "check_unmodified",
    "collision_free_path",
    "format_log_line",
    "get_archive_path",
    "is_ai_actor",
    "merge_into_note",
    "merge_preview",
    "move_to_destination",
    "new_folder",
    "parse_log_line",
    "snapshot_file",
    "update_frontmatter",
    "update_tags",
    "vault_tree",
]
