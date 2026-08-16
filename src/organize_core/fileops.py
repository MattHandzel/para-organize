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
  touches nothing (the vault is Syncthing-synced). "At read" is literal:
  :func:`_read_document` stats the mtime, reads the bytes ONCE, and builds
  the :class:`FileSnapshot` from those bytes. Nothing between the read and
  the commit may re-derive the snapshot from disk — a snapshot taken after
  the read silently blesses whatever landed in between.
- **Containment** (spec 05 §1) — :func:`require_in_vault` refuses any
  source/destination/target outside ``config.vault.root``. It lives here,
  not in a composition root, because fileops is the only writer and the two
  doors must agree (ARCHITECTURE ruling #19).
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
``context.dry_run = True``. That field is first-class in ``ActionContext``
(the integrator granted the seam request), and ``ActionRecorder.query`` /
``stats`` / ``export`` exclude such records by default — a rehearsal is
logged, but it is never a doc-12 precedent.

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

The rule is applied to BOTH files a merge/append touches. The exemption
above is justified by "pure relocation … changes no note content", which is
not true of the CAPTURE in a merge or append: those duplicate the capture's
body into a different note, which is exactly how ``no-ai`` content escapes
its container. Refusing only the target let an automated actor copy a
protected note's body into an ordinary one.
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
    SuggestionShown,
    TargetState,
    new_action_id,
)
from organize_core.config import Config
from organize_core.errors import (
    ConcurrentModificationError,
    ConfigError,
    FrontmatterError,
    NoAiRefusal,
    OperationError,
    VaultError,
)
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

OperationType = Literal[
    "move", "archive", "merge", "append", "integrate", "create_folder", "metadata", "skip"
]

#: fileops op type -> spec 12 §2 ActionRecord operation name.
#: ``skip`` is the one entry whose operation never reaches :func:`_log` —
#: it changes no file, so it has no operation-log line (see `skip_capture`).
_ACTION_OPERATION: dict[str, str] = {
    "skip": "skip",
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

#: Default per-route append template (spec 11 §1). Placeholders are replaced
#: literally — NEVER ``str.format`` (08 §B15: templates contain literal ``{}``).
DEFAULT_APPEND_TEMPLATE = "## {date} — from {capture_id}\n\n{body}"

#: Machine-owned append marker (CRITICAL-1 ruling). "Already delivered" is
#: read from the VAULT, not the store, so a retry after a crash cannot append
#: a second copy. Written in the SAME atomic write as the block it belongs
#: to, so the marker and the content can never disagree. It is the append
#: analog of the blessed ``auto_tag_hash`` bookkeeping: machine-owned
#: frontmatter-equivalent that curation tooling should leave alone.
APPEND_MARKER_PREFIX = "organize:appended"


def append_marker(capture_id: str, route: str | None = None) -> str:
    """The comment written beside an appended block."""
    return f"<!-- {APPEND_MARKER_PREFIX} capture_id={capture_id} route={route or ''} -->"


def append_delivery_token(capture_id: str) -> str:
    """The needle the delivered-check looks for.

    NOT the rendered template: ``{date}`` drifts between runs, so a
    template-based check reads "not delivered" the next day and duplicates.
    NOT the bare capture id either: a ``[[capture-id]]`` wikilink in the
    target would read as "already delivered" and SILENTLY skip a real
    append — non-delivery is the worse failure, because nothing anywhere
    reports it.

    The TRAILING SPACE is load-bearing: without it ``capture_id=cap-1``
    substring-matches a marker for ``cap-10`` and that capture is silently
    never appended. The marker always renders ``route=`` next, so the space
    is always there.
    """
    return f"{APPEND_MARKER_PREFIX} capture_id={capture_id} "

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


#: The three literal sequences that carry meaning in the spec 05 §1.5 line
#: grammar. A path is allowed to contain any of them (``a -> b.md`` is a
#: legal filename), so they are escaped rather than assumed absent.
_LOG_ESCAPES: tuple[tuple[str, str], ...] = (
    ("\\", "\\\\"),  # first: the escape character itself
    ("\n", "\\n"),
    ("\r", "\\r"),
    ("\t", "\\t"),
    (" -> ", " -\\> "),
    (" Backup: ", " Backup\\: "),
    (" Error: ", " Error\\: "),
)

_LOG_UNESCAPE = re.compile(r"\\(.)")
_LOG_UNESCAPE_MAP = {"\\": "\\", "n": "\n", "r": "\r", "t": "\t", ">": ">", ":": ":"}


def _escape_field(text: str) -> str:
    """Make one field safe for the single-line log grammar WITHOUT destroying
    it (spec 05 §1.5 "the log must contain enough to manually undo any
    operation"; §8 "every operation manually reversible").

    Only the characters that actually break the grammar are touched, and
    every one of them is reversible by :func:`_unescape_field`. The old
    implementation collapsed whitespace runs, so a note named
    ``two  spaces\\tand tab.md`` was logged under a path that does not
    exist — the log, ``parse_log_line`` and ``undo_info`` all named a file
    nobody could find.
    """
    out = str(text)
    for raw, escaped in _LOG_ESCAPES:
        out = out.replace(raw, escaped)
    return out


def _unescape_field(text: str) -> str:
    """Exact inverse of :func:`_escape_field`."""
    return _LOG_UNESCAPE.sub(lambda m: _LOG_UNESCAPE_MAP.get(m.group(1), m.group(1)), text)


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


def _file_mode(path: Path) -> int | None:
    """The permission bits of an existing file, or ``None`` if unreadable."""
    try:
        return os.stat(path).st_mode & 0o7777
    except OSError:  # pragma: no cover - the caller has just read the file
        return None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: Path) -> str:
    """Every text read in this module: utf-8 with ``errors="replace"`` so a
    single bad byte can never crash an operation (06 §6 / 08 §B18).

    READ-BACK ONLY, and NEVER for byte-fidelity verification: ``read_text``
    opens in UNIVERSAL-NEWLINE mode, so a ``\\r\\n`` or a lone ``\\r`` on disk
    comes back as ``\\n``. Comparing that against the string we wrote is
    guaranteed to differ for any note containing a CR — 23% of the real
    capture backlog. Verification goes through :func:`_verbatim_text`.
    The mutation path goes through :func:`_read_document`, which decodes
    STRICTLY — see there.
    """
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _verbatim_text(raw: bytes) -> str:
    """Decode read-back bytes WITHOUT newline translation.

    The counterpart of :func:`_read_text` for the one caller that compares
    what landed on disk against what it meant to write: ``atomic_write``
    opens with ``newline=""`` (verbatim), so the comparison must decode
    verbatim too.
    """
    return raw.decode("utf-8", errors="replace")


def _decode_for_mutation(raw: bytes, path: Path) -> str:
    """Decode a note we are about to REWRITE, strictly.

    ``errors="replace"`` is right for scans and read-backs, and wrong here:
    it turns every undecodable byte into U+FFFD, and the operation then
    writes that corruption into the destination while reporting ``ok=True``.
    The errors.py contract is "scans tolerate, mutations refuse" — broken
    YAML already refuses, and undecodable bytes now refuse the same way
    instead of silently damaging the copy (05 §1 "no code path may lose note
    content").
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrontmatterError(
            f"{path} is not valid UTF-8 (byte {exc.start} of {len(raw)}: {exc.reason}); "
            "refusing to rewrite it because the copy would silently differ from the original",
            hint=(
                "fix the encoding of the note (e.g. `iconv -f latin1 -t utf-8`) and retry; "
                "reading, scanning and archiving the note still work — only rewrites refuse."
            ),
        ) from exc


def _read_document(path: Path) -> tuple[Document, str, FileSnapshot]:
    """Read + parse + snapshot, from ONE read of ONE byte string.

    The snapshot is derived from the bytes that were actually read, with the
    mtime stat'd BEFORE the read — it is NOT a second stat+hash of the file
    afterwards. That ordering is the whole concurrent-modification guarantee
    (spec 10 §4, module docstring "mtime+hash captured at read"): the old
    code read the file and only then called ``snapshot_file()``, so any
    concurrent write landing in between was invisible, the operation
    overwrote the file from the stale read, and it reported ``ok=True``.
    The window was not theoretical — merge parsed a second document inside
    it.

    ``FrontmatterError`` propagates for unparseable YAML *and* for
    undecodable bytes: a mutating operation must refuse to rewrite a note it
    cannot round-trip (errors.py contract).
    """
    path = Path(path)
    mtime = path.stat().st_mtime
    raw = path.read_bytes()
    snapshot = FileSnapshot(path=str(path), mtime=mtime, sha256=hashlib.sha256(raw).hexdigest())
    text = _decode_for_mutation(raw, path)
    return _parse_named(text, path), text, snapshot


def _parse_named(text: str, path: Path) -> Document:
    """``parse`` with the offending FILE named in the error.

    ``frontmatter.parse`` takes text, so its message is only ever "line 30,
    column 17" — unactionable in a batch run over 1,858 captures against the
    23 real files with unparseable YAML. `frontmatter.load_file` already does
    this for the scan path; every MUTATION path now goes through here, so no
    call site can forget it (09 §1.5: an error must be actionable).
    """
    try:
        return parse(text)
    except FrontmatterError as exc:
        raise FrontmatterError(f"{path}: {exc}", hint=exc.hint) from exc


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


def _resolved(path: Path) -> Path:
    """``resolve()`` that never raises on a dangling path (non-strict)."""
    try:
        return Path(path).resolve()
    except OSError:  # pragma: no cover - resolve() is non-strict on 3.12
        return Path(path).absolute()


def require_in_vault(config: Config, path: Path, what: str) -> Path:
    """Refuse any path outside ``config.vault.root`` (spec 05 §1).

    THE containment check, and it lives here rather than in each composition
    root on purpose: fileops is the only writer, so this is the only place
    both doors inherit it from (ARCHITECTURE ruling #19 — the two doors must
    agree). The server used to enforce containment at its own boundary while
    ``organize move`` happily wrote note content to ``../OUTSIDE`` and
    reported success.
    """
    root = _resolved(config.vault.root)
    candidate = _resolved(path)
    if candidate != root and root not in candidate.parents:
        raise VaultError(
            f"{candidate} is outside the vault {root}",
            hint=f"the {what} must be inside the vault root; paths are vault-relative or "
            "absolute inside the vault (spec 05 §1 — the core never writes outside the vault)",
        )
    return candidate


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
    line = (
        f"[{_escape_field(op.ts)}] {op.type}: "
        f"{_escape_field(op.src)} -> {_escape_field(op.dst)} [{status}]"
    )
    if op.backup:
        line += f" Backup: {_escape_field(op.backup)}"
    if op.error:
        line += f" Error: {_escape_field(op.error)}"
    return line


def parse_log_line(line: str) -> LoggedOperation | None:
    """Inverse of :func:`format_log_line`; ``None`` for anything unparseable
    (a corrupt line must never kill ``debug``/``health``)."""
    match = _LINE_RE.match(line.rstrip("\n"))
    if match is None:
        return None
    # Every delimiter below is escaped inside a field by `_escape_field`, so
    # splitting on the raw sequence can no longer cut a path in half.
    src, sep, dst = match.group("body").rpartition(" -> ")
    if not sep:
        src, dst = match.group("body"), ""
    rest = match.group("rest")
    backup: str | None = None
    error: str | None = None
    err_at = rest.find(" Error: ")
    if err_at >= 0:
        error = _unescape_field(rest[err_at + len(" Error: ") :]) or None
        rest = rest[:err_at]
    bak_at = rest.find(" Backup: ")
    if bak_at >= 0:
        backup = _unescape_field(rest[bak_at + len(" Backup: ") :]) or None
    status = match.group("status")
    return LoggedOperation(
        ts=_unescape_field(match.group("ts")),
        type=match.group("type"),  # type: ignore[arg-type]
        src=_unescape_field(src),
        dst=_unescape_field(dst),
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

    # --- spec 12 §2 decision context -------------------------------------
    # These describe the DECISION, which only the caller knows, and they are
    # the whole reason the corpus exists ("the counterfactual is stored, not
    # just the choice"). They used to have no parameter and no call site, so
    # every real record stored an empty `suggestions_shown` and a null
    # `chosen_rank` and `organize actions stats` could never report an
    # accept-rate from real usage.
    #: The ranked list the user was shown, in rank order (12 §2).
    suggestions_shown: tuple[SuggestionShown, ...] = ()
    #: Which of them was picked; 1 means the engine was right (12 §2).
    chosen_rank: int | None = None
    #: e.g. ``{"decision": 8400, "operation": 120}`` (12 §2).
    durations_ms: dict[str, int] = field(default_factory=dict)
    #: Auto-tags present on the capture at decision time (12 §2, doc 11 §2).
    auto_tags_present: tuple[str, ...] = ()
    #: The session filters in force (12 §2).
    filters: dict[str, Any] = field(default_factory=dict)

    #: Resolves ``targets[].description`` — the destination's NL description
    #: (12 §2 / 11 §3). A callable, not an import: ``routes`` depends on
    #: ``fileops``, so ``fileops`` may not import ``routes`` back. The
    #: composition roots wire this to ``routes.get_description``.
    describe: Callable[[Path], str | None] | None = None

    #: Called with each ActionRecord that was successfully APPENDED to the
    #: corpus — spec 12 §2 "Uses" #2: "doc 04's learning layer records
    #: through this same pipeline (one write path, two readers)". The
    #: composition roots wire this to `learn.record_action`, so learning.json
    #: is a view derived from the action log rather than a second,
    #: independent write that can disagree with it.
    on_record: Callable[[ActionRecord], None] | None = None


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


def atomic_write(path: Path, content: str, *, encoding: str = "utf-8", mode: int | None = None) -> None:
    """Write-to-temp + rename per spec 05 §1.3. Temp in ``path.parent``,
    unique name via pid+counter; preserve file permissions; on any failure
    the target is untouched and the temp is removed.

    Fixes 08 §A25 wholesale: unique-per-write temp names (pid + process
    counter, not epoch seconds), permissions carried over, temp removed on
    every failure path including ``KeyboardInterrupt``.

    Permissions, in order (spec 05 §1.3 "Preserve file permissions"):

    1. an explicit ``mode`` — ``move_to_destination`` passes the SOURCE's
       mode, because the destination is a new file and the thing being
       preserved is the note's mode, not the (nonexistent) target's;
    2. else the existing target's mode, when we are overwriting;
    3. else the platform default (``0o666`` masked by the process umask),
       obtained by letting ``O_CREAT`` apply the umask itself — no
       ``os.umask()`` read, which is process-global and racy.

    Case 3 used to be missing entirely: the temp was always created 0o600
    and, with no existing target to copy a mode from, every file this
    function CREATED landed owner-only. Every note organized by ``move``
    silently diverged from the rest of the Syncthing-synced vault.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            mode = None

    # When the final mode is known, create restrictively and widen at the
    # end, so a note that should be 0600 is never briefly world-readable.
    # When it is not known, let O_CREAT + umask produce the platform default.
    tmp = directory / f".{path.name}.{os.getpid()}.{next(_TMP_COUNTER)}.organize-tmp"
    create_mode = 0o600 if mode is not None else 0o666
    fd: int | None = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create_mode)
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


def _discard_unverified_copy(dest_path: Path, source: Path) -> Path | None:
    """Remove a destination copy THIS operation just created and then failed
    to verify. Returns the path when it could NOT be removed (so the caller
    names the stray file), ``None`` when the vault is clean again.

    One of the three delete sites in this module — the structural
    never-delete guard in ``tests/test_fileops_safety.py`` lists them by
    name. It is admissible because ``dest_path`` came from
    ``collision_free_path``, so it did not exist before ``atomic_write``
    created it moments ago, and because the SOURCE is explicitly refused
    here: spec 05 §1's "no code path may lose note content" holds — the
    original is untouched and nothing has been archived yet.

    Not doing this is what produced, on real data, a fully-organized
    duplicate at the destination of a capture whose move had just been
    reported as failed, plus another ``_1``, ``_2``… copy per retry.
    """
    if _resolved(dest_path) == _resolved(source):  # pragma: no cover - defensive
        return dest_path
    try:
        dest_path.unlink()
    except OSError:
        return dest_path
    return None


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


#: Spec 12 §2: "full before-text stored when the file is new or small
#: (<64 KB)" — target files are otherwise kept as hash + diff to bound growth.
BEFORE_TEXT_LIMIT_BYTES = 64 * 1024


def _describe(ctx: OperationContext, folder: Path) -> str | None:
    """``targets[].description`` — the destination's NL description (12 §2).

    Resolved through ``ctx.describe``, which the composition roots wire to
    ``routes.get_description``. It is a callable rather than a direct import
    because ``routes`` already depends on ``fileops``; importing it back
    would be the one cycle the ARCHITECTURE dependency graph forbids.
    """
    if ctx.describe is None:
        return None
    try:
        text = ctx.describe(Path(folder))
    except Exception:  # noqa: BLE001 - a description is never load-bearing
        logger.debug("could not resolve a description for %s", folder, exc_info=True)
        return None
    text = (text or "").strip()
    return text or None


def _target_state(
    path: Path,
    before: str | None,
    after: str,
    role: str,
    *,
    description: str | None = None,
) -> TargetState:
    # `before_hash is None` already means "the file is new", so `before_text`
    # is only populated for an EXISTING small file — that is the case where
    # `before_hash` + a 3-context diff cannot reconstruct the pre-edit state
    # and doc 13 loses the file the edit was made against.
    before_text: str | None = None
    if before is not None and len(before.encode("utf-8", errors="replace")) < BEFORE_TEXT_LIMIT_BYTES:
        before_text = before
    return TargetState(
        path=str(path),
        role=role,  # type: ignore[arg-type]
        before_hash=_hash_text(before) if before is not None else None,
        after_hash=_hash_text(after),
        diff=_unified_diff(before or "", after, path),
        description=description,
        before_text=before_text,
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
    partial_failure: str | None = None,
) -> None:
    """Emit the spec 12 §2 ActionRecord for an operation that CHANGED THE VAULT.

    Spec 12 §2 is unconditional — "**Every** state-changing operation
    appends one ActionRecord" — and spec 05 §1.2 explicitly blesses a
    partially-applied operation (the destination copy exists, archiving the
    original failed). Those branches mutate the vault, so they record too,
    with ``context.partial_failure`` naming what did not finish — a
    FIRST-CLASS field, not a ``filters`` entry, because ``filters`` is the
    session's search filters and no reader could branch on it (learning
    folded three failed moves in as successful accepts). The corpus is the
    audit trail, and a real vault edit that appears nowhere in it is an
    untraceable mutation. Operations that changed
    nothing (a missing source, an unwritable destination) still do NOT
    record: the schema has no "it failed" slot and their story is the
    operation log's FAILED line.

    The decision context (``suggestions_shown``, ``chosen_rank``,
    ``durations_ms``, ``auto_tags_present``, ``filters``) comes off the
    :class:`OperationContext`, so the counterfactual doc 12 exists to
    capture is filled in by whichever door the user came through instead of
    being hardcoded empty on every real record.
    """
    filters: dict[str, Any] = dict(ctx.filters)
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
                dry_run=ctx.dry_run,
                partial_failure=partial_failure or None,
                route=route,
                vault_stats=_vault_stats(ctx),
                filters=filters,
                suggestions_shown=tuple(ctx.suggestions_shown),
                chosen_rank=ctx.chosen_rank,
                auto_tags_present=tuple(ctx.auto_tags_present),
                durations_ms=dict(ctx.durations_ms),
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
    if not ctx.recorder.record(record):
        # The corpus is the source of truth: if the line was LOST, no reader
        # derives state from it either. Previously learning.json was written
        # regardless, so the learner could hold a move the corpus never saw.
        return
    if ctx.on_record is None:
        return
    try:
        ctx.on_record(record)
    except Exception:  # noqa: BLE001 - a derived view never blocks the op
        logger.error(
            "ACTION RECORD CONSUMER FAILED for %s on %s — the operation itself "
            "completed and the record was written (spec 12 §2)",
            op_type,
            capture.path if capture is not None else "<unknown>",
            exc_info=True,
        )


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
        # `get_archive_path` already picked a free name, but that check and
        # this rename are not one operation: anything that appeared at
        # `archive_path` in between (another organize run, Syncthing, or —
        # before the guard in `move_to_destination` — this very operation's
        # own organized copy) would be silently obliterated by os.replace,
        # which overwrites unconditionally. Never delete (05 §1.1): fail.
        if archive_path.exists():
            return None, (
                f"archive target {archive_path} already exists; refusing to overwrite it "
                "(spec 05 §1.1: never delete). The original was NOT removed."
            )
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
    *,
    archive: bool = True,
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

    ``archive=False`` files the copy and STOPS: the original stays in the
    capture folder and its index entry stays too — step 8's removal is
    DEFERRED, not skipped. Only ``routes.apply_all`` passes it, because 11
    §1 mandates config-order execution and several move-mode routes may
    match one capture: spec-permitted, and the answer is N copies plus ONE
    archive. Archiving on the first route would leave every later route
    looking for a source that had already moved. The caller owes both
    halves — the archive and the index removal — after its final
    destination succeeds; skipping them strands the capture.
    """
    now = ctx.clock()
    source = require_in_vault(ctx.config, capture.path, "note")
    dest_folder = require_in_vault(ctx.config, destination_folder, "destination folder")

    if not source.is_file():
        return _fail(ctx, "move", source, dest_folder, f"source note does not exist: {source}", now)

    # The archive capture folder is not an ordinary destination: `dest_path`
    # and `archive_path` would both resolve to <archive>/<filename>, the
    # organized copy would be written there and then silently overwritten by
    # `_archive_file`'s os.replace of the un-organized original — while the
    # result claimed ok=True with tag_added / processing_status=organized.
    archive_folder = _resolved(
        Path(ctx.config.vault.root)
        / (ctx.config.vault.para_folders.get("archives") or "archive")
        / ctx.config.vault.archive_capture_path
    )
    resolved_dest = _resolved(dest_folder)
    if resolved_dest == archive_folder:
        return _fail(
            ctx,
            "move",
            source,
            dest_folder,
            f"{dest_folder} is the archive capture folder, not a filing destination",
            now,
            details={"archive_path": str(archive_folder)},
        )

    # Two destinations that are inside the vault but are not FILING
    # destinations: the note leaves the capture folder, is archived as
    # organized, and is then never indexed again because neither location is
    # under `vault.scan_dirs` — reported as a success. Moving a note into the
    # backup dir additionally writes that note's own backup beside it.
    backup_root = _resolved(ctx.backup_dir)
    if resolved_dest == backup_root or backup_root in resolved_dest.parents:
        return _fail(
            ctx,
            "move",
            source,
            dest_folder,
            f"{dest_folder} is inside the backup directory, not a filing destination",
            now,
        )
    if resolved_dest == _resolved(ctx.config.vault.root):
        return _fail(
            ctx,
            "move",
            source,
            dest_folder,
            f"{dest_folder} is the vault root, not a filing destination",
            now,
        )

    # Moving a note into the folder it ALREADY lives in is a no-op, not a
    # rename. `collision_free_path` would otherwise see the note itself as
    # the collision, file the copy as `<name>_1.md` and archive the original
    # FILENAME — divorcing the file from its own `id:`/`aliases:` and
    # breaking every [[wikilink]] to it (05 §3), while returning ok=True. It
    # is one mis-click away in the picker.
    if resolved_dest == _resolved(source.parent):
        return OperationResult(
            ok=True,
            operation="move",
            source=str(source),
            destination=str(source),
            dry_run=ctx.dry_run,
            details={"noop": f"the note is already in {dest_folder}"},
        )

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

    # ONE read: doc, text and the concurrent-modification token all come from
    # the same bytes, with mtime stat'd before the read (10 §4).
    doc, text, snapshot = _read_document(source)
    _refuse_no_ai(ctx, source, doc, "move")

    dest_path = collision_free_path(dest_folder / source.name)
    fields_after, tag = _organized_fields(doc, dest_folder, ctx.config, now)
    new_text = _rendered(fields_after, _style(doc), doc.body)
    archive_path = get_archive_path(source.name, ctx.config, now=now)
    description = _describe(ctx, dest_folder)

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
            targets=[
                _target_state(dest_path, None, new_text, "destination", description=description)
            ],
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
        # Spec 05 §1.3 "Preserve file permissions": the destination is a NEW
        # file, so the mode to preserve is the source note's — without it
        # every organized note landed 0600 while the rest of the vault stayed
        # 0644, and the vault is read by Syncthing/Obsidian/other tools.
        atomic_write(dest_path, new_text, mode=_file_mode(source))
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

    # BYTE-exact read-back. `atomic_write` wrote `new_text` verbatim
    # (newline=""), so the check must compare BYTES: a `read_text()` here
    # translates every CR on disk back to LF and then reports "verification
    # failed" for a copy that is in fact perfect. 23% of the real capture
    # backlog contains a CR, and every one of them was unmovable.
    written_bytes = dest_path.read_bytes()
    if written_bytes != new_text.encode("utf-8"):
        # ROLL THE COPY BACK. `dest_path` came from `collision_free_path`, so
        # it did not exist before `atomic_write` created it and removing it
        # restores the vault exactly. Leaving it behind produced a
        # fully-organized duplicate of a capture that the caller was told had
        # NOT been filed — and every retry added another `_1`, `_2`, … copy.
        written = _verbatim_text(written_bytes)
        stray = _discard_unverified_copy(dest_path, source)
        if stray is not None:
            # Rollback failed: the vault HAS changed, so this branch records
            # (spec 12 §2) and the message names the file left behind.
            _record_action(
                ctx,
                op_type="move",
                capture=capture_state,
                targets=[
                    _target_state(dest_path, None, written, "destination", description=description)
                ],
                now=now,
                partial_failure="copy verification failed; the original was NOT archived",
            )
            message = (
                f"copy verification failed at {dest_path}; the original was NOT archived "
                f"and the unverified copy could NOT be removed — delete {stray} by hand"
            )
        else:
            # Nothing changed: no ActionRecord (a rolled-back operation is not
            # a doc-12 precedent), just the FAILED oplog line `_fail` writes.
            message = (
                f"copy verification failed at {dest_path}; the copy was removed "
                "and the original was NOT archived"
            )
        return _fail(
            ctx,
            "move",
            source,
            dest_path,
            message,
            now,
            backup=backup,
            details=details,
        )

    if not archive:
        # Deferred half (see the docstring): the copy is filed and indexed;
        # the original AND its capture-entry in the index are left exactly
        # as they were, for the caller to finish after its last destination.
        _index_update(ctx, dest_path)
        _log(ctx, "move", source, dest_path, success=True, now=now, backup=backup)
        _record_action(
            ctx,
            op_type="move",
            capture=capture_state,
            targets=[
                _target_state(dest_path, None, new_text, "destination", description=description)
            ],
            now=now,
        )
        return OperationResult(
            ok=True,
            operation="move",
            source=str(source),
            destination=str(dest_path),
            backup_path=str(backup) if backup else None,
            details={**details, "archived": False},
        )

    archived, archive_error = _archive_file(source, archive_path)
    if archived is None:
        _log(ctx, "archive", source, archive_path, success=False, now=now, error=archive_error)
        # Partial apply (05 §1.2): the organized copy is on disk and the
        # original is still in place. That is a vault mutation, so it gets a
        # record (12 §2) naming what did not finish.
        _record_action(
            ctx,
            op_type="move",
            capture=capture_state,
            targets=[
                _target_state(dest_path, None, new_text, "destination", description=description)
            ],
            now=now,
            partial_failure=f"archiving the original failed: {archive_error}",
        )
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
        targets=[
            _target_state(dest_path, None, new_text, "destination", description=description)
        ],
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
    # Pure relocation: tolerate bytes we could not rewrite (this operation
    # never rewrites), so the read stays lenient here on purpose.
    text = _read_text(source)
    doc = _parse_named(text, source)
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
    source = require_in_vault(ctx.config, capture.path, "capture")
    target_path = require_in_vault(ctx.config, target_path, "merge target")

    if not source.is_file():
        return _fail(ctx, "merge", source, target_path, f"capture does not exist: {source}", now)
    if not target_path.is_file():
        return _fail(ctx, "merge", source, target_path, f"merge target does not exist: {target_path}", now)

    # `snapshot` comes from the SAME bytes we derive the merged content from,
    # and mtime was stat'd before the read (10 §4). It used to be taken after
    # BOTH reads below, and parsing the capture is not instant — a concurrent
    # write landing in that window was overwritten with ok=True.
    target_doc, target_text, snapshot = _read_document(target_path)
    _refuse_no_ai(ctx, target_path, target_doc, "merge into")
    capture_doc, capture_text, _capture_snapshot = _read_document(source)
    # A merge COPIES the capture's body into another file. That is a write of
    # the protected content out of its no-ai container, so an automated actor
    # is refused on the capture too — the module's "pure relocation" exemption
    # covers `archive_capture`, not this (vault law, spec 02).
    _refuse_no_ai(ctx, source, capture_doc, "merge from")

    if target_snapshot is not None:
        check_unmodified(target_snapshot)

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
    # Spec 05 §4 (merge) dedupes on NORMALIZED form — a strictly stronger key
    # than 05 §2.6's case-insensitive move rule, so `deep_work` from the
    # capture collapses into the target's existing `deep-work` instead of
    # landing beside it. Target-first order and the target's casing are kept.
    tags = merge_tags(
        _as_str_list(base_fields.get("tags")),
        _as_str_list(capture_fields.get("tags")),
        normalized=True,
        extra_map=ctx.config.suggestions.tag_normalization,
    )
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
    targets = [
        _target_state(
            target_path,
            target_text,
            new_text,
            "merge_target",
            description=_describe(ctx, target_path.parent),
        )
    ]
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
        atomic_write(target_path, new_text, mode=_file_mode(target_path))
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
        # The target has ALREADY been rewritten in place — the most
        # consequential partial apply in the module. It records (12 §2).
        _record_action(
            ctx,
            op_type="merge",
            capture=capture_state,
            targets=targets,
            now=now,
            edit_mode="manual",
            partial_failure=f"archiving the capture failed: {archive_error}",
        )
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
    target_doc, _target_text, snapshot = _read_document(target_path)
    capture_doc, _capture_text, _ = _read_document(source)
    body = _merged_body(target_doc.body, capture_doc.body, source.name, now)
    content = _rendered(_fields(target_doc), _style(target_doc), body)
    # The snapshot is the one taken WITH the target read above, not a fresh
    # stat afterwards, so a write during the capture read invalidates it.
    return content, snapshot


def append_to_note(
    ctx: OperationContext,
    capture: NoteRecord,
    target_path: Path,
    *,
    template: str | None = None,
    route: str | None = None,
) -> OperationResult:
    """Route ``append`` mode (spec 11 §1): capture body appended under
    ``## <date> — from <capture-id>`` (per-route ``template`` override);
    target frontmatter untouched except ``last_edited_date``; original
    archived BY THE ROUTE LAYER after all destinations succeed (11 §1
    multi-route rule — NOT here); logged as ``append``.

    Placeholders (``{date}``, ``{capture_id}``, ``{body}``, ``{filename}``)
    are substituted by literal replacement, never ``str.format`` — a
    template containing a literal ``{`` must not raise (08 §B15).

    IDEMPOTENT against the VAULT (CRITICAL-1 ruling): the block carries an
    :func:`append_marker`, and a target already holding this capture's
    marker returns ``ok=True`` with ``details["already_delivered"]`` and
    writes NOTHING. That is what makes a retried batch — after a crash, or
    after a later destination failed — land exactly once instead of
    appending a second copy on every attempt.
    """
    now = ctx.clock()
    source = require_in_vault(ctx.config, capture.path, "capture")
    target_path = require_in_vault(ctx.config, target_path, "append target")

    if not source.is_file():
        return _fail(ctx, "append", source, target_path, f"capture does not exist: {source}", now)
    if not target_path.is_file():
        return _fail(ctx, "append", source, target_path, f"append target does not exist: {target_path}", now)

    target_doc, target_text, snapshot = _read_document(target_path)
    _refuse_no_ai(ctx, target_path, target_doc, "append to")
    capture_doc, capture_text, _capture_snapshot = _read_document(source)
    # Same reasoning as merge: an append duplicates the capture's body into
    # another file, so a no-ai capture refuses an automated actor here too.
    _refuse_no_ai(ctx, source, capture_doc, "append from")
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
    targets = [
        _target_state(
            target_path,
            target_text,
            new_text,
            "append_target",
            description=_describe(ctx, target_path.parent),
        )
    ]
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
        atomic_write(target_path, new_text, mode=_file_mode(target_path))
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
    replace_keys: frozenset[str] = frozenset(),
) -> OperationResult:
    now = ctx.clock()
    path = require_in_vault(ctx.config, path, "note")
    if not path.is_file():
        return _fail(ctx, op_type, path, path, f"note does not exist: {path}", now)

    doc, text, snapshot = _read_document(path)
    _refuse_no_ai(ctx, path, doc, "edit the frontmatter of")

    fields = dict(_fields(doc))
    for key, value in changes.items():
        if value is None:
            fields.pop(key, None)
            continue
        if key == "tags" and key not in replace_keys:
            fields["tags"] = merge_tags(_as_str_list(fields.get("tags")), _as_str_list(value))
        elif key == "tags":
            # Explicit replace (spec 07 `append = false`). Still normalised
            # through merge_tags so the REPLACEMENT list is itself deduped and
            # order-preserving — replacing must not be a way to smuggle a
            # duplicated tag list past the 08 §A24 rule.
            fields["tags"] = merge_tags([], _as_str_list(value))
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
        atomic_write(path, new_text, mode=_file_mode(path))
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
    *,
    replace_keys: frozenset[str] = frozenset(),
) -> OperationResult:
    """General primitive (spec 05 §5): read → parse → apply changes
    (``tags`` merges per the order-preserving rule; other fields replace) →
    round-trip-safe serialize → atomic write. Used by move step 6, metadata
    editing (07), merge. Logged as ``metadata``.

    Every field outside the change set survives byte-for-byte, known or
    unknown (``no-ai``, ``title``, Obsidian properties…) — 08 §A12, the
    worst data-loss bug in the original, at the operation level. A change
    value of ``None`` REMOVES the field (07 needs a way to unset one).

    ``replace_keys`` names keys whose value REPLACES rather than merges.
    Only ``tags`` merges by default, so in practice this is
    ``frozenset({"tags"})``, and it exists because spec 07 lets a
    ``[[metadata_fields]]`` entry declare ``append = false`` on a list field:
    without it the caller had to clear the field and set it again, which is
    two writes, two operations-log lines and two ActionRecords for ONE
    logical edit — a corpus that misreports what Matt did (12 §2).
    """
    return _apply_frontmatter_changes(
        ctx, path, changes, op_type="metadata", action="meta_edit", replace_keys=replace_keys
    )


def update_tags(ctx: OperationContext, path: Path, new_tags: list[str]) -> OperationResult:
    """Thin wrapper over :func:`update_frontmatter` (spec 05 §5).

    Merge preserves the user's order and casing and appends new tags at the
    end — it does NOT re-sort or re-case the list (08 §A24).
    """
    return _apply_frontmatter_changes(
        ctx, path, {"tags": list(new_tags)}, op_type="metadata", action="tag_edit"
    )


def skip_capture(ctx: OperationContext, capture: NoteRecord) -> None:
    """Spec 03 §2/§6: record that the user skipped this capture.

    The one recorded operation that changes NO file, which drives two
    deliberate asymmetries:

    - **No operation-log line.** The oplog records what happened to the
      VAULT; an OK line for an operation that touched nothing would claim a
      mutation that never happened, and a reader reconciling the log against
      the vault would find nothing to match it to.
    - **An ActionRecord IS written.** Doc 12 §2 wants the counterfactual, and
      "these suggestions were on screen and the user chose none of them" is
      exactly the decision a skip carries. Skips are also what
      ``actions stats`` measures acceptance rate AGAINST.

    NOT a learning signal: doc 04 §3 gives a skip no negative weight, and
    ``learn.record_action`` already returns ``None`` for ``operation="skip"``,
    so the ``on_record`` wiring folds nothing into learning.json. That is
    load-bearing rather than incidental — pinned by the trap test asserting
    learning.json is byte-identical across a skip.

    It lives here, despite touching no file, because this is the ONE place an
    ActionRecord is built. A second builder in the server would duplicate
    capture-state, vault-stats and actor handling and could drift from it.

    Session bookkeeping (``Session.skipped``) is the CALLER's job: this
    function is the record half, and the session is the server's state.
    """
    now = ctx.clock()
    source = Path(capture.path)
    if source.is_file():
        # Lenient read on purpose: a skip never rewrites the note, so a byte
        # we could not decode strictly must not stop the user moving on.
        text = _read_text(source)
        doc = _parse_named(text, source)
        capture_state = _capture_state(source, text, doc)
    else:
        # A capture that vanished under an open session is still skippable —
        # skip is session bookkeeping, not a file operation — so the decision
        # is recorded with the same degenerate state `new_folder` uses.
        capture_state = CaptureState(
            path=str(source), content_hash="", frontmatter_before={}, body_before=""
        )
    _record_action(ctx, op_type="skip", capture=capture_state, now=now)


def new_folder(ctx: OperationContext, para_type: str, name: str) -> OperationResult:
    """mkdir ``<vault>/<para_folders[para_type]>/<name>`` (spec 05 §6);
    validate name (no path separators, non-empty); log ``create_folder``.
    The auto-move-current-capture behavior lives in the session layer.

    ``para_type`` addresses a ``vault.para_folders`` KEY, so it is PLURAL —
    but it also accepts the singular (``project``), because the UI's
    ``<leader>np`` speaks singular.

    Every ADDRESSING failure raises ``ConfigError``: an unknown ``para_type``,
    an empty ``name``, and a ``name`` carrying a path separator. Only
    WORLD-STATE failures (an unwritable PARA root) return ``ok=False``.

    Spec 05 §6 also says "refresh folder caches/index dirs". No index call is
    needed: ``VaultIndex.para_subfolders`` enumerates directories from DISK,
    not from the note records, so a brand-new EMPTY folder is a suggestion
    candidate the moment mkdir returns — with no scan and no cache to
    invalidate. Pinned by
    ``test_new_folder_is_immediately_a_suggestion_candidate``.
    """
    now = ctx.clock()
    folders = ctx.config.vault.para_folders
    key = para_type if para_type in folders else next(
        (k for k in folders if k[:-1] == para_type or k == f"{para_type}s"), None
    )
    root = Path(ctx.config.vault.root)
    if key is None:
        # ADDRESSING failure, not a world-state one: the caller named a PARA
        # type that does not exist, so there is no operation to attempt and
        # nothing to write a FAILED oplog line about. Raises rather than
        # returning ok=false, matching `VaultIndex.para_subfolders` on the
        # same bad input so `folder.create` and `folder.list` agree.
        raise ConfigError(
            f"unknown PARA type {para_type!r}",
            hint="valid values: " + ", ".join(sorted(folders)),
        )

    # Both name checks are ADDRESSING failures for the same reason the
    # unknown-PARA-type one above is: the request never named a folder that
    # could be created, so there is no operation to attempt and no FAILED
    # oplog line to write. Same taxonomy class, so a client branching on
    # `error.data.kind` sees one kind for every way of misaddressing
    # `folder.create`.
    clean = (name or "").strip()
    if not clean:
        raise ConfigError(
            "folder name is empty",
            hint="pass the name of the folder to create under the PARA root, e.g. 'newsletter'",
        )
    if "/" in clean or "\\" in clean or clean in {".", ".."} or "\x00" in clean:
        raise ConfigError(
            f"folder name {name!r} contains a path separator; only a single folder name is allowed",
            hint="create one folder at a time, naming it without '/', '\\', '.' or '..'",
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


#: Suffix `atomic_write` gives its in-flight temp files.
TEMP_SUFFIX = ".organize-tmp"

#: Grace period before an `.organize-tmp` file counts as an orphan. An
#: in-flight write is measured in milliseconds; anything older than this was
#: left behind by a killed process.
TEMP_ORPHAN_AGE_SECONDS = 300.0


def find_orphaned_temp_files(
    root: Path, *, now: float, older_than: float = TEMP_ORPHAN_AGE_SECONDS
) -> list[Path]:
    """Every abandoned ``atomic_write`` temp file under ``root`` (spec 05 §1.3
    "temp files are cleaned up").

    ``atomic_write`` removes its temp on every in-process failure path, but a
    SIGKILL or a power loss mid-write leaves a full-size hidden
    ``.<name>.<pid>.<n>.organize-tmp`` sitting inside the vault, and the vault
    is Syncthing-synced — so the dropping replicates to every device. Nothing
    swept them and ``organize health`` did not look, which made them
    invisible as well as permanent. Reporting is deliberately separated from
    deleting: this returns the list, ``organize health`` surfaces the count,
    and no code path in this module removes a file it did not create.
    """
    orphans: list[Path] = []
    for path in sorted(Path(root).rglob(f"*{TEMP_SUFFIX}")):
        if not path.is_file():
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError:  # pragma: no cover - raced with a real cleanup
            continue
        if age >= older_than:
            orphans.append(path)
    return orphans


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
    "BEFORE_TEXT_LIMIT_BYTES",
    "DEFAULT_APPEND_TEMPLATE",
    "HUMAN_ACTORS",
    "TEMP_ORPHAN_AGE_SECONDS",
    "TEMP_SUFFIX",
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
    "find_orphaned_temp_files",
    "format_log_line",
    "get_archive_path",
    "is_ai_actor",
    "merge_into_note",
    "merge_preview",
    "move_to_destination",
    "new_folder",
    "parse_log_line",
    "require_in_vault",
    "snapshot_file",
    "update_frontmatter",
    "update_tags",
    "vault_tree",
]
