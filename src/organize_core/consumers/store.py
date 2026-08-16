"""SQLite state store: notes + emissions + migration (spec 06 §1).

THE EXISTING 15 MB LIVE DB (``~/.local/state/para-organize/automations.db``)
MUST BE MIGRATED, NOT DISCARDED — a year of emission history is the
idempotency contract; losing it refires every consumer on every past
capture (duplicate Taskwarrior tasks, duplicate LLM runs).

Target schema (spec 06 §1: existing schema + ``last_seen`` + schema_version
meta):

    CREATE TABLE notes (path TEXT PRIMARY KEY, note_hash TEXT NOT NULL,
                        metadata_json TEXT, seen_at INTEGER NOT NULL,
                        last_seen INTEGER);
    CREATE TABLE emissions (consumer TEXT NOT NULL, note_path TEXT NOT NULL,
                            note_hash TEXT NOT NULL, emitted_at INTEGER NOT NULL,
                            status TEXT NOT NULL DEFAULT 'success',
                            metadata_json TEXT,
                            PRIMARY KEY (consumer, note_path));
    CREATE INDEX idx_emissions_consumer_hash ON emissions (consumer, note_hash);

Connection settings: WAL, busy_timeout 5000 (06 §1). Paths stored are
``.resolve()``d absolute paths — canonicalize identically to the live DB
or all history orphans (06 §1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

STORE_SCHEMA_VERSION = 2  # v1 = live para-organize DB (implicit, no meta)


@dataclass(frozen=True)
class Emission:
    consumer: str
    note_path: str
    note_hash: str
    emitted_at: int
    status: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MigrationReport:
    """What :meth:`AutomationStore.migrate` did — surfaced in the run
    summary and the migration acceptance test (06 §7: no historical
    ``success`` checkpoint may be lost)."""

    from_version: int
    to_version: int
    kept_success: int
    kept_skip: int
    deleted_filtered: int
    reset_retryable: int  # limit/error rows now treated as retryable


class AutomationStore:
    """One store per run. Use as a context manager."""

    def __init__(self, db_path: Path) -> None:
        raise NotImplementedError

    def __enter__(self) -> AutomationStore:
        raise NotImplementedError

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        raise NotImplementedError

    # --- schema ----------------------------------------------------------

    def migrate(self) -> MigrationReport:
        """Bring the DB to STORE_SCHEMA_VERSION (spec 06 §1 migration):
        fresh DB ⇒ create schema; live v1 DB ⇒ add ``last_seen`` +
        version meta, KEEP success/skip rows as-is, DELETE ``filtered``
        rows (re-evaluated cheaply every run), treat limit/error as
        retryable. Aborts with StoreMigrationError on anything unexpected —
        never runs half-migrated. Caller backs the DB up first (09 §5.4)."""
        raise NotImplementedError

    # --- emission / checkpoint API (orchestrator-only writers, 06 §1) ----

    def needs_delivery(self, consumer: str, path: Path, note_hash: str) -> bool:
        """True iff ``note_hash`` differs from this consumer's last TERMINAL
        emission for ``path`` (spec 06 §1 delivery rule; error/limit rows do
        not block redelivery)."""
        raise NotImplementedError

    def checkpoint(
        self,
        consumer: str,
        path: Path,
        note_hash: str,
        status: str,
        metadata: dict[str, Any] | None = None,
        *,
        now: int,
    ) -> None:
        """Upsert the emission row. Callers pass ONLY terminal statuses
        (success/skip) — the runner enforces the error/limit-not-
        checkpointed rule (06 §1)."""
        raise NotImplementedError

    def get_emission(self, consumer: str, path: Path) -> Emission | None:
        raise NotImplementedError

    # --- notes table / soft purge (06 §1) --------------------------------

    def mark_seen(self, paths: list[Path], *, now: int) -> None:
        """Update ``last_seen`` for every path seen this run."""
        raise NotImplementedError

    def soft_purge(
        self, *, retention_days: int = 30, scan_dirs_ok: bool, now: int
    ) -> int:
        """Hard-delete rows unseen for ``retention_days`` — but ONLY when
        ``scan_dirs_ok`` (every configured scan dir existed and was
        non-empty this run); a transient mount must never forget a year of
        checkpoints (spec 06 §1, 08 §B5). Returns rows purged."""
        raise NotImplementedError
