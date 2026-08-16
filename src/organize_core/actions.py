"""ActionRecord schema + append-only JSONL writer + query (spec 12 §2).

EVERY state-changing operation appends one record to
``<state>/actions/YYYY-MM.jsonl`` — including actions outside sessions
(CLI, consumers, routes). The write path ships in Phase 1 (spec README
build order note: recording lands with ① so the corpus grows early); this
is a Phase-1 GATE.

Hard rules (spec 12 §2):
- Append-only, atomic appends (one ``write()`` of a full line + newline,
  fsync policy documented by the builder), never rewritten. A torn write
  must never produce a partial JSONL line (12 §3 torn-write test).
- Recording failure must NOT block the operation — log loudly, continue.
- Capture body stored in full; targets stored as hash + unified diff
  (full before-text only when the file is new or < 64 KB).
- The counterfactual is stored, not just the choice: suggestions_shown +
  chosen_rank; proposed_diff vs final_diff; rejections are signal too.
- Privacy: corpus stays local; never shipped to any API except when Matt
  invokes a learning/auto-organize feature that reads it.

Implementation notes (builder decisions, spec 12 §2 leaves these free):

*Append atomicity.* One ``os.open(..., O_RDWR|O_CREAT|O_APPEND, 0o600)``
per record, guarded by a best-effort ``flock`` (the CLI, the server and the
consumer pipeline all append to the same file, and records carrying a full
capture body easily exceed the size a single ``write`` is atomic for); the
JSON line plus its newline is handed to a single ``os.write`` (the loop
only runs on a short write). If the line did not complete, the file is
truncated back to its pre-write size — but ONLY when the tail is still
exactly a prefix of our own line, so a concurrent appender is never
damaged. That is the 12 §3 torn-write guarantee: readers never see a
partial JSONL line, and a line that somehow does get mangled is skipped
with a warning rather than killing the query.

*fsync policy.* Each completed line is ``fsync``-ed. Volume is tiny
(order 100 records/day, spec 12 §2 budget) and the corpus is the training
substrate the whole design exists for, so durability beats throughput. An
``fsync`` failure is logged as a warning and does NOT fail the record —
the line is already complete on disk.

*File permissions.* ``0o600``: the corpus inherits the vault's
sensitivity (spec 12 §2 privacy note) and stays local.

*Timestamps.* The month file is chosen from ``record.ts``'s ``YYYY-MM``
prefix (never from a wall clock inside this module) so a record always
lands in its own month and this module stays clock-free and injectable.
"""

from __future__ import annotations

import copy
import errno
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

from organize_core.errors import OrganizeError

try:  # POSIX only; the core is a Linux/unix-socket service (spec 10 §1)
    import fcntl
except ImportError:  # pragma: no cover - no non-POSIX target
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: How long an append waits for the cross-process lock before giving up and
#: appending unlocked — recording must never block the calling operation.
_LOCK_TIMEOUT_S = 1.0

ACTIONS_SCHEMA_VERSION = 1

Operation = Literal[
    "move",
    "merge",
    "append",
    "integrate",
    "archive",
    "skip",
    "meta_edit",
    "create_folder",
    "tag_edit",
]

EditMode = Literal["manual", "append", "integrate"]

Verdict = Literal["accepted", "edited", "rejected"]

#: Runtime views of the Literal enums above (kept in sync by construction).
OPERATIONS: frozenset[str] = frozenset(get_args(Operation))
EDIT_MODES: frozenset[str] = frozenset(get_args(EditMode))
VERDICTS: frozenset[str] = frozenset(get_args(Verdict))
TARGET_ROLES: frozenset[str] = frozenset({"destination", "merge_target", "append_target"})

#: ``YYYY-MM.jsonl`` — the only file names :class:`ActionRecorder` reads.
_MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.jsonl$")
_TS_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})(?:$|[-T ])")


class ActionSchemaError(OrganizeError, ValueError):
    """An ActionRecord payload is not valid for :meth:`ActionRecord.from_json`.

    Derives from both the taxonomy base (so ``organize record`` maps it to a
    loud exit-1 with a hint, spec 09 §1.5) and ``ValueError`` (so ordinary
    parse-error handling catches it too). Messages always name the offending
    field — "bad record" is banned.
    """


# --- schema ----------------------------------------------------------------


@dataclass(frozen=True)
class CaptureState:
    """``capture`` block (spec 12 §2): the note being organized, body in
    full, frontmatter before and (when meta/tag edits happened) after."""

    path: str
    content_hash: str
    frontmatter_before: dict[str, Any]
    body_before: str
    frontmatter_after: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content_hash": self.content_hash,
            "frontmatter_before": copy.deepcopy(self.frontmatter_before),
            "body_before": self.body_before,
            "frontmatter_after": copy.deepcopy(self.frontmatter_after),
        }

    @classmethod
    def from_json(cls, raw: Any, *, strict: bool = True) -> CaptureState:
        data = _as_dict(raw, "capture")
        return cls(
            path=_req_str(data, "path", "capture"),
            content_hash=_opt_str(data, "content_hash", "capture") or "",
            frontmatter_before=_opt_dict(data, "frontmatter_before", "capture") or {},
            body_before=_opt_str(data, "body_before", "capture") or "",
            frontmatter_after=_opt_dict(data, "frontmatter_after", "capture"),
        )


@dataclass(frozen=True)
class TargetState:
    """One ``targets[]`` entry — ONE PER FILE TOUCHED; multi-destination is
    first-class (spec 12 §2). ``diff`` is a unified diff; full before-text
    included when the file is new or small (<64 KB)."""

    path: str
    role: Literal["destination", "merge_target", "append_target"]
    before_hash: str | None
    after_hash: str | None
    diff: str
    description: str | None = None  # the NL description of this destination

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "diff": self.diff,
            "description": self.description,
        }

    @classmethod
    def from_json(cls, raw: Any, *, strict: bool = True) -> TargetState:
        data = _as_dict(raw, "targets[]")
        role = _req_str(data, "role", "targets[]")
        if strict and role not in TARGET_ROLES:
            raise ActionSchemaError(
                f"targets[].role {role!r} is not a known role",
                hint=f"expected one of {sorted(TARGET_ROLES)}",
            )
        return cls(
            path=_req_str(data, "path", "targets[]"),
            role=role,  # type: ignore[arg-type]
            before_hash=_opt_str(data, "before_hash", "targets[]"),
            after_hash=_opt_str(data, "after_hash", "targets[]"),
            diff=_opt_str(data, "diff", "targets[]") or "",
            description=_opt_str(data, "description", "targets[]"),
        )


@dataclass(frozen=True)
class SuggestionShown:
    """One line of the counterfactual (spec 12 §2 context.suggestions_shown)."""

    path: str
    score: float
    rank: int
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "score": self.score,
            "rank": self.rank,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_json(cls, raw: Any) -> SuggestionShown:
        data = _as_dict(raw, "context.suggestions_shown[]")
        return cls(
            path=_req_str(data, "path", "context.suggestions_shown[]"),
            score=_req_float(data, "score", "context.suggestions_shown[]"),
            rank=_req_int(data, "rank", "context.suggestions_shown[]"),
            reasons=_str_tuple(data.get("reasons"), "context.suggestions_shown[].reasons"),
        )


@dataclass(frozen=True)
class ActionContext:
    """``context`` block (spec 12 §2)."""

    session_id: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    suggestions_shown: tuple[SuggestionShown, ...] = ()
    chosen_rank: int | None = None  # 1 == the engine was right
    route: str | None = None
    auto_tags_present: tuple[str, ...] = ()
    vault_stats: dict[str, int] = field(default_factory=dict)
    durations_ms: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "filters": copy.deepcopy(self.filters),
            "suggestions_shown": [s.to_json() for s in self.suggestions_shown],
            "chosen_rank": self.chosen_rank,
            "route": self.route,
            "auto_tags_present": list(self.auto_tags_present),
            "vault_stats": dict(self.vault_stats),
            "durations_ms": dict(self.durations_ms),
        }

    @classmethod
    def from_json(cls, raw: Any) -> ActionContext:
        if raw is None:
            return cls()
        data = _as_dict(raw, "context")
        shown = data.get("suggestions_shown") or []
        if not isinstance(shown, list):
            raise ActionSchemaError("context.suggestions_shown must be a list")
        return cls(
            session_id=_opt_str(data, "session_id", "context"),
            filters=_opt_dict(data, "filters", "context") or {},
            suggestions_shown=tuple(SuggestionShown.from_json(s) for s in shown),
            chosen_rank=_opt_int(data, "chosen_rank", "context"),
            route=_opt_str(data, "route", "context"),
            auto_tags_present=_str_tuple(
                data.get("auto_tags_present"), "context.auto_tags_present"
            ),
            vault_stats=_opt_dict(data, "vault_stats", "context") or {},
            durations_ms=_opt_dict(data, "durations_ms", "context") or {},
        )


@dataclass(frozen=True)
class LLMTrace:
    """``llm`` block — integrate/auto actions only (spec 12 §2)."""

    backend: str
    model: str
    prompt_hash: str
    proposed_diff: str
    final_diff: str  # ≠ proposed when Matt hand-edited
    verdict: Verdict

    def to_json(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "proposed_diff": self.proposed_diff,
            "final_diff": self.final_diff,
            "verdict": self.verdict,
        }

    @classmethod
    def from_json(cls, raw: Any, *, strict: bool = True) -> LLMTrace:
        data = _as_dict(raw, "llm")
        verdict = _req_str(data, "verdict", "llm")
        if strict and verdict not in VERDICTS:
            raise ActionSchemaError(
                f"llm.verdict {verdict!r} is not a known verdict",
                hint=f"expected one of {sorted(VERDICTS)}",
            )
        return cls(
            backend=_req_str(data, "backend", "llm"),
            model=_opt_str(data, "model", "llm") or "",
            prompt_hash=_opt_str(data, "prompt_hash", "llm") or "",
            proposed_diff=_opt_str(data, "proposed_diff", "llm") or "",
            final_diff=_opt_str(data, "final_diff", "llm") or "",
            verdict=verdict,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ActionRecord:
    """One JSONL line (spec 12 §2 schema, field-for-field)."""

    id: str  # "act_<ulid>"
    ts: str  # ISO8601 UTC
    actor: str  # "matt" | "claude-integrate" | "route:<name>" | "consumer:<name>" | "auto-organize"
    operation: Operation
    capture: CaptureState
    targets: tuple[TargetState, ...] = ()
    context: ActionContext = field(default_factory=ActionContext)
    edit_mode: EditMode | None = None
    llm: LLMTrace | None = None
    schema_version: int = ACTIONS_SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        """The JSONL payload, key-for-key and in the order of spec 12 §2.

        Optional blocks (``edit_mode``, ``llm``, ``frontmatter_after``,
        target hashes) are emitted as ``null`` rather than omitted so every
        line has the same shape for downstream readers. All mutable members
        are copied — the returned dict never aliases the record.
        """
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "ts": self.ts,
            "actor": self.actor,
            "operation": self.operation,
            "edit_mode": self.edit_mode,
            "capture": self.capture.to_json(),
            "targets": [t.to_json() for t in self.targets],
            "context": self.context.to_json(),
            "llm": self.llm.to_json() if self.llm is not None else None,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ActionRecord:
        """Strict parse; unknown schema_version tolerated on read (forward
        compat for the query path).

        Strict means: required fields must be present and of the right type,
        and enum-valued fields (``operation``, ``edit_mode``, ``llm.verdict``,
        ``targets[].role``) must be known values. When ``schema_version`` is
        NEWER than :data:`ACTIONS_SCHEMA_VERSION` the enum checks relax (a
        future writer may add operations) and unknown keys are ignored, so an
        old reader can still stream a newer corpus.
        """
        data = _as_dict(raw, "record")
        version = data.get("schema_version", ACTIONS_SCHEMA_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise ActionSchemaError(
                f"record.schema_version must be an int, got {type(version).__name__}"
            )
        strict = version <= ACTIONS_SCHEMA_VERSION

        operation = _req_str(data, "operation", "record")
        if strict and operation not in OPERATIONS:
            raise ActionSchemaError(
                f"record.operation {operation!r} is not a known operation",
                hint=f"expected one of {sorted(OPERATIONS)}",
            )

        edit_mode = _opt_str(data, "edit_mode", "record")
        if strict and edit_mode is not None and edit_mode not in EDIT_MODES:
            raise ActionSchemaError(
                f"record.edit_mode {edit_mode!r} is not a known edit mode",
                hint=f"expected one of {sorted(EDIT_MODES)}",
            )

        if "capture" not in data or data["capture"] is None:
            raise ActionSchemaError(
                "record.capture is required", hint="every action records the capture it acted on"
            )
        targets = data.get("targets") or []
        if not isinstance(targets, list):
            raise ActionSchemaError("record.targets must be a list")

        return cls(
            id=_req_str(data, "id", "record"),
            ts=_req_str(data, "ts", "record"),
            actor=_req_str(data, "actor", "record"),
            operation=operation,  # type: ignore[arg-type]
            capture=CaptureState.from_json(data["capture"], strict=strict),
            targets=tuple(TargetState.from_json(t, strict=strict) for t in targets),
            context=ActionContext.from_json(data.get("context")),
            edit_mode=edit_mode,  # type: ignore[arg-type]
            llm=(
                LLMTrace.from_json(data["llm"], strict=strict)
                if data.get("llm") is not None
                else None
            ),
            schema_version=version,
        )


# --- payload validation helpers -------------------------------------------


def _as_dict(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ActionSchemaError(f"{where} must be a JSON object, got {type(raw).__name__}")
    return raw


def _req_str(data: dict[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ActionSchemaError(
            f"{where}.{key} is required and must be a string"
            f" (got {type(value).__name__})",
        )
    return value


def _opt_str(data: dict[str, Any], key: str, where: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActionSchemaError(f"{where}.{key} must be a string or null, got {type(value).__name__}")
    return value


def _opt_dict(data: dict[str, Any], key: str, where: str) -> dict[str, Any] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ActionSchemaError(f"{where}.{key} must be an object or null, got {type(value).__name__}")
    return copy.deepcopy(value)


def _req_float(data: dict[str, Any], key: str, where: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionSchemaError(f"{where}.{key} is required and must be a number")
    return float(value)


def _req_int(data: dict[str, Any], key: str, where: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionSchemaError(f"{where}.{key} is required and must be an integer")
    return value


def _opt_int(data: dict[str, Any], key: str, where: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionSchemaError(f"{where}.{key} must be an integer or null")
    return value


def _str_tuple(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ActionSchemaError(f"{where} must be a list of strings")
    for item in value:
        if not isinstance(item, str):
            raise ActionSchemaError(f"{where} must contain only strings, got {type(item).__name__}")
    return tuple(value)


# --- action ids (stdlib ULID) ---------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MAX_MS = (1 << 48) - 1
_MAX_RAND = (1 << 80) - 1
_ULID_LOCK = threading.Lock()
_ULID_STATE: dict[str, int] = {"ms": -1, "rand": 0}


def _encode_crockford(value: int, length: int) -> str:
    out = bytearray(length)
    for i in range(length - 1, -1, -1):
        out[i] = ord(_CROCKFORD[value & 0x1F])
        value >>= 5
    return out.decode("ascii")


def new_action_id(*, now: float | None = None) -> str:
    """``act_<ulid>`` — 26-char Crockford-base32 ULID (48-bit ms timestamp +
    80 random bits), stdlib-only implementation (no ulid dependency —
    architect decision; format matches spec 12 §2's ``act_<ulid>``).

    Monotonic within a millisecond: repeated calls in the same millisecond
    (real clock or an injected ``now``) increment the random component
    instead of redrawing it, so ids are unique AND lexicographically sortable
    under rapid calls — the 08 §A25 "collides on same-second writes" class of
    bug cannot happen here. Thread-safe.
    """
    raw_ms = int((time.time() if now is None else now) * 1000)
    ms = max(0, min(raw_ms, _MAX_MS))
    with _ULID_LOCK:
        if ms == _ULID_STATE["ms"]:
            rand = _ULID_STATE["rand"] + 1
            if rand > _MAX_RAND:  # pragma: no cover - 2**80 ids in one ms
                ms = min(ms + 1, _MAX_MS)
                rand = secrets.randbits(80)
        else:
            rand = secrets.randbits(80)
        _ULID_STATE["ms"] = ms
        _ULID_STATE["rand"] = rand
    return "act_" + _encode_crockford((ms << 80) | rand, 26)


# --- append-only writer ----------------------------------------------------


def _raw_write(fd: int, data: bytes) -> int:
    """Indirection over ``os.write`` — the single seam the 12 §3 torn-write
    test patches. Never call ``os.write`` directly from this module."""
    return os.write(fd, data)


def _month_key(ts: Any) -> str | None:
    """``YYYY-MM`` from an ISO8601 timestamp, or None if unusable."""
    if not isinstance(ts, str):
        return None
    match = _TS_MONTH_RE.match(ts)
    if match is None:
        return None
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def _rollback_partial_line(fd: int, path: Path, start: int, data: bytes) -> None:
    """Truncate away a partial line so no reader ever sees one (12 §3).

    The decision is made from the file itself, not from our own write
    accounting, so a short/interrupted write is caught however it happened.
    Truncation only proceeds when the bytes past ``start`` are exactly a
    proper prefix of the line we were writing — if another appender got in
    between, truncating would destroy its record, so we log instead.
    """
    try:
        size = os.fstat(fd).st_size
        if size <= start:
            return  # nothing of ours reached the file
        tail_len = size - start
        if tail_len > len(data):
            logger.error(
                "action log %s: %d unexpected bytes past offset %d — refusing to truncate "
                "(another writer may have appended); file may contain a truncated line",
                path,
                tail_len,
                start,
            )
            return
        tail = os.pread(fd, tail_len, start)
        if tail != data[:tail_len]:
            logger.error(
                "action log %s: tail past offset %d is not our partial line — refusing to "
                "truncate; file may contain a truncated line",
                path,
                start,
            )
            return
        if tail_len == len(data):
            return  # the line completed after all; keep it
        os.ftruncate(fd, start)
        logger.error(
            "action log %s: rolled back a %d-byte partial line (no partial JSONL written)",
            path,
            tail_len,
        )
    except OSError:
        logger.error("action log %s: partial-write rollback failed", path, exc_info=True)


def _lock(fd: int, path: Path) -> bool:
    """Best-effort exclusive append lock (CLI, server and consumers all
    append to the same file). Never blocks the calling operation: retries
    non-blocking for at most :data:`_LOCK_TIMEOUT_S`, then appends unlocked
    (``O_APPEND`` still guarantees no overwrite; a reader tolerates a torn
    interleave by skipping the line)."""
    if fcntl is None:  # pragma: no cover - POSIX-only project
        return False
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                logger.warning(
                    "action log %s: could not take the append lock in %.1fs; appending unlocked",
                    path,
                    _LOCK_TIMEOUT_S,
                )
                return False
            time.sleep(0.005)


def _append_line(path: Path, line: str) -> None:
    """Append ``line`` + newline as one write; leave no partial line behind."""
    data = line.encode("utf-8", errors="replace") + b"\n"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _lock(fd, path)
        start = os.fstat(fd).st_size
        written = 0
        try:
            while written < len(data):
                n = _raw_write(fd, data[written:])
                if n <= 0:
                    raise OSError(errno.EIO, "write made no progress", str(path))
                written += n
        except BaseException:
            _rollback_partial_line(fd, path, start, data)
            raise
        try:
            os.fsync(fd)
        except OSError:
            logger.warning(
                "action log %s: fsync failed; the line is complete but not durably flushed",
                path,
                exc_info=True,
            )
    finally:
        os.close(fd)


def _within(ts: str, since: str | None, until: str | None) -> bool:
    """Bounds compare against the ISO prefix of the bound's own length, so
    ``--since 2026-08-01 --until 2026-08-15`` is inclusive of whole days."""
    if since is not None and ts[: len(since)] < since:
        return False
    return not (until is not None and ts[: len(until)] > until)


class ActionRecorder:
    """The append-only writer + reader over ``<actions_dir>/YYYY-MM.jsonl``."""

    def __init__(self, actions_dir: Path) -> None:
        # Pure constructor: no I/O, no mkdir (08 §B2 — constructors that do
        # I/O take the whole process down). The directory is created lazily
        # on the first successful record.
        self.actions_dir = Path(actions_dir)

    # --- write ------------------------------------------------------------

    def record(self, record: ActionRecord) -> bool:
        """Append one record to the current month's file. Returns False
        (after a LOUD log) instead of raising — recording failures never
        block the operation (spec 12 §2). Atomic single-line append."""
        try:
            line = json.dumps(record.to_json(), ensure_ascii=False, separators=(",", ":"))
            month = _month_key(record.ts)
            if month is None:
                raise ActionSchemaError(
                    f"record.ts {record.ts!r} does not start with YYYY-MM",
                    hint="ts must be an ISO8601 UTC timestamp (spec 12 §2)",
                )
            if "\n" in line or "\r" in line:  # pragma: no cover - json escapes these
                raise ActionSchemaError("serialized record contains a newline")
            self.actions_dir.mkdir(parents=True, exist_ok=True)
            _append_line(self.actions_dir / f"{month}.jsonl", line)
        except Exception:
            logger.error(
                "ACTION RECORD LOST: could not append action id=%s operation=%s to %s — "
                "the operation itself completed and is unaffected (spec 12 §2)",
                getattr(record, "id", "<unknown>"),
                getattr(record, "operation", "<unknown>"),
                self.actions_dir,
                exc_info=True,
            )
            return False
        return True

    # --- read -------------------------------------------------------------

    def month_files(self) -> list[Path]:
        """Existing ``YYYY-MM.jsonl`` files, oldest first (name-sorted ==
        chronological). Anything else in the directory is ignored."""
        try:
            entries = list(self.actions_dir.iterdir())
        except OSError:
            return []
        return sorted(p for p in entries if p.is_file() and _MONTH_FILE_RE.match(p.name))

    def query(
        self,
        *,
        operation: Operation | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> Iterator[ActionRecord]:
        """Stream matching records across month files, oldest first.
        Corrupt lines are skipped with a warning, never fatal.

        ``since``/``until`` accept ``YYYY-MM-DD`` (inclusive whole days) or a
        full ISO timestamp; comparison is on the ISO prefix of the bound.
        """
        for path in self.month_files():
            month = path.name[:7]
            if since is not None and month < since[:7]:
                continue
            if until is not None and month > until[:7]:
                continue
            yield from self._read_file(path, operation=operation, actor=actor, since=since, until=until)

    def _read_file(
        self,
        path: Path,
        *,
        operation: str | None,
        actor: str | None,
        since: str | None,
        until: str | None,
    ) -> Iterator[ActionRecord]:
        try:
            handle = path.open("r", encoding="utf-8", errors="replace", newline="")
        except OSError:
            logger.error("action log %s could not be opened; skipping", path, exc_info=True)
            return
        with handle:
            for lineno, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    raw = json.loads(text)
                    rec = ActionRecord.from_json(raw)
                except (ValueError, ActionSchemaError) as exc:
                    logger.warning("action log %s:%d is corrupt, skipping (%s)", path, lineno, exc)
                    continue
                if operation is not None and rec.operation != operation:
                    continue
                if actor is not None and rec.actor != actor:
                    continue
                if not _within(rec.ts, since, until):
                    continue
                yield rec

    def export(self, out: Path | None = None, **filters: Any) -> int:
        """``organize actions export`` — concatenate/filter to ``out`` or
        stdout (spec 12 §2). Returns record count."""
        count = 0
        if out is None:
            for rec in self.query(**filters):
                sys.stdout.write(json.dumps(rec.to_json(), ensure_ascii=False) + "\n")
                count += 1
            sys.stdout.flush()
            return count
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", errors="replace", newline="\n") as handle:
            for rec in self.query(**filters):
                handle.write(json.dumps(rec.to_json(), ensure_ascii=False) + "\n")
                count += 1
        return count

    def stats(self) -> dict[str, Any]:
        """``organize actions stats`` (spec 12 §2 "Uses" #1): accept-rate of
        top suggestion, per-route volumes, integrate accept/edit/reject
        rates.

        ``suggestions.top_accept_rate`` = records whose ``chosen_rank == 1``
        over records that had a non-empty ``suggestions_shown`` (a record
        where suggestions were shown and none was chosen — a skip — counts
        against the engine, per 12 §2 "a rejection is as much signal").
        Rates are ``None`` when their denominator is zero.
        """
        by_operation: dict[str, int] = {}
        by_actor: dict[str, int] = {}
        by_route: dict[str, int] = {}
        by_edit_mode: dict[str, int] = {}
        rank_histogram: dict[int, int] = {}
        verdicts: dict[str, int] = {"accepted": 0, "edited": 0, "rejected": 0}
        total = 0
        with_suggestions = 0
        top_chosen = 0
        other_rank_chosen = 0
        none_chosen = 0
        llm_total = 0
        first_ts: str | None = None
        last_ts: str | None = None

        for rec in self.query():
            total += 1
            by_operation[rec.operation] = by_operation.get(rec.operation, 0) + 1
            by_actor[rec.actor] = by_actor.get(rec.actor, 0) + 1
            if rec.context.route:
                by_route[rec.context.route] = by_route.get(rec.context.route, 0) + 1
            if rec.edit_mode:
                by_edit_mode[rec.edit_mode] = by_edit_mode.get(rec.edit_mode, 0) + 1
            if rec.context.suggestions_shown:
                with_suggestions += 1
                rank = rec.context.chosen_rank
                if rank is None:
                    none_chosen += 1
                elif rank == 1:
                    top_chosen += 1
                else:
                    other_rank_chosen += 1
            if rec.context.chosen_rank is not None:
                rank_histogram[rec.context.chosen_rank] = (
                    rank_histogram.get(rec.context.chosen_rank, 0) + 1
                )
            if rec.llm is not None:
                llm_total += 1
                verdicts[rec.llm.verdict] = verdicts.get(rec.llm.verdict, 0) + 1
            if first_ts is None or rec.ts < first_ts:
                first_ts = rec.ts
            if last_ts is None or rec.ts > last_ts:
                last_ts = rec.ts

        return {
            "total": total,
            "months": [p.name[:7] for p in self.month_files()],
            "first_ts": first_ts,
            "last_ts": last_ts,
            "by_operation": by_operation,
            "by_actor": by_actor,
            "by_route": by_route,
            "by_edit_mode": by_edit_mode,
            "suggestions": {
                "with_suggestions": with_suggestions,
                "top_chosen": top_chosen,
                "other_rank_chosen": other_rank_chosen,
                "none_chosen": none_chosen,
                "top_accept_rate": (top_chosen / with_suggestions) if with_suggestions else None,
                "rank_histogram": rank_histogram,
            },
            "integrate": {
                "total": llm_total,
                "accepted": verdicts["accepted"],
                "edited": verdicts["edited"],
                "rejected": verdicts["rejected"],
                "accept_rate": (verdicts["accepted"] / llm_total) if llm_total else None,
                "edit_rate": (verdicts["edited"] / llm_total) if llm_total else None,
                "reject_rate": (verdicts["rejected"] / llm_total) if llm_total else None,
            },
        }
