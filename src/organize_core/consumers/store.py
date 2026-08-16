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

plus ``meta(key, value)`` carrying ``schema_version`` (v1 had no version
marker at all — ARCHITECTURE.md resolution 13), and the two ``purged_*``
archive tables that give :meth:`AutomationStore.soft_purge` a restore path
(08 §B5: a transient empty mount must never be able to destroy history).

Connection settings: WAL, busy_timeout 5000 (06 §1). Paths stored are
``.resolve()``d absolute paths — canonicalize identically to the live DB
or all history orphans (06 §1); a relative path is rejected loudly rather
than silently written under a key nothing will ever match again.

B1 defence: ``text_factory`` decodes every TEXT column with
``errors="replace"``. sqlite3's default text factory is a STRICT utf-8
decode, so one byte of mojibake in a ``metadata_json`` blob written by the
old pipeline would raise ``UnicodeDecodeError`` on ``SELECT`` — the exact
failure class that took the pipeline down for three months.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from organize_core.errors import StoreError, StoreMigrationError

log = logging.getLogger(__name__)

STORE_SCHEMA_VERSION = 2  # v1 = live para-organize DB (implicit, no meta)

#: Statuses that checkpoint a (consumer, note) pair (spec 06 §1). A note is
#: redelivered iff its hash differs from the last TERMINAL emission.
TERMINAL_STATUSES = frozenset({"success", "skip"})

#: Statuses the runner must NOT checkpoint — retried next run (08 §B3).
RETRYABLE_STATUSES = frozenset({"error", "limit"})

#: ``filtered`` existed only in v1: filter misses are no longer persisted
#: (08 §B4), so migration DROPS those rows (09 §5.4).
V1_ONLY_STATUSES = frozenset({"filtered"})

KNOWN_STATUSES = TERMINAL_STATUSES | RETRYABLE_STATUSES | V1_ONLY_STATUSES

DEFAULT_RETENTION_DAYS = 30

#: How long the soft-purge ARCHIVE keeps a row before it is really gone.
#: Deliberately much longer than the purge window: the archive exists to
#: survive an incident nobody noticed for a while. Bounded, though — nothing
#: else deletes from ``purged_*``, so without a sweep the 15 MB bloat 08 §B4
#: removed from ``emissions`` would just move house.
ARCHIVE_RETENTION_DAYS = 180

_SECONDS_PER_DAY = 86400

_V1_NOTES_COLUMNS = {"path", "note_hash", "metadata_json", "seen_at"}
_V1_EMISSIONS_COLUMNS = {
    "consumer",
    "note_path",
    "note_hash",
    "emitted_at",
    "status",
    "metadata_json",
}

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS notes (
    path TEXT PRIMARY KEY,
    note_hash TEXT NOT NULL,
    metadata_json TEXT,
    seen_at INTEGER NOT NULL,
    last_seen INTEGER
);

CREATE TABLE IF NOT EXISTS emissions (
    consumer TEXT NOT NULL,
    note_path TEXT NOT NULL,
    note_hash TEXT NOT NULL,
    emitted_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'success',
    metadata_json TEXT,
    PRIMARY KEY (consumer, note_path)
);

CREATE INDEX IF NOT EXISTS idx_emissions_consumer_hash
    ON emissions (consumer, note_hash);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purged_notes (
    path TEXT PRIMARY KEY,
    note_hash TEXT NOT NULL,
    metadata_json TEXT,
    seen_at INTEGER NOT NULL,
    last_seen INTEGER,
    purged_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS purged_emissions (
    consumer TEXT NOT NULL,
    note_path TEXT NOT NULL,
    note_hash TEXT NOT NULL,
    emitted_at INTEGER NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT,
    purged_at INTEGER NOT NULL,
    PRIMARY KEY (consumer, note_path)
);
"""

#: ``executescript`` implicitly COMMITs any pending transaction, which would
#: silently break migrate()'s all-or-nothing guarantee — so the schema is
#: applied statement by statement inside the caller's transaction instead.
_SCHEMA_V2_STATEMENTS = tuple(
    statement.strip() for statement in _SCHEMA_V2.split(";") if statement.strip()
)


def _create_schema(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V2_STATEMENTS:
        conn.execute(statement)


def hash_note_text(raw_text: str) -> str:
    """``note_hash = sha256(raw_text)`` (spec 06 §1) — THE idempotency key.

    Byte-identical to the live pipeline's hash (``scripts/automation/notes.py``:
    ``sha256(raw_text.encode("utf-8"))``) so migrated history keeps matching;
    ``errors="replace"`` only matters for text that could not have produced a
    valid v1 hash in the first place (06 §6).
    """
    digest = hashlib.sha256()
    digest.update(raw_text.encode("utf-8", errors="replace"))
    return digest.hexdigest()


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
    ``success`` checkpoint may be lost).

    Count semantics, so a report can be reconciled against pre-migration
    queries (09 §5.4):

    - ``kept_success`` / ``kept_skip`` / ``notes_kept`` / ``reset_retryable``
      are a census of the store **after** the call.
    - ``deleted_filtered`` counts rows removed by **this** call (so a second,
      no-op migrate reports 0).
    - ``anomalies`` lists everything unexpected but non-fatal that was found
      and PRESERVED (unknown status values, orphaned emission rows, …).
    """

    from_version: int
    to_version: int
    kept_success: int
    kept_skip: int
    deleted_filtered: int
    reset_retryable: int  # limit/error rows now treated as retryable
    notes_kept: int = 0
    #: Surviving emission rows whose status this build does not recognise
    #: (a hand-edited DB, a status from a future build). They are kept and
    #: named in ``anomalies``, but without this field they were counted in NO
    #: numeric field, so ``kept_success + kept_skip + reset_retryable``
    #: silently under-counted the store — and deploy/README tells the
    #: operator to paste ``summary()`` into the cutover log AS the
    #: reconciliation artefact.
    kept_unknown: int = 0
    anomalies: tuple[str, ...] = ()
    created: bool = False  # fresh DB: schema created, nothing to migrate
    no_op: bool = False  # already at STORE_SCHEMA_VERSION: nothing changed

    @property
    def changed(self) -> bool:
        return not (self.no_op or self.created)

    @property
    def emissions_kept(self) -> int:
        """Every emission row still in the store. Reconciles exactly against
        ``SELECT count(*) FROM emissions`` without parsing prose anomalies."""
        return self.kept_success + self.kept_skip + self.reset_retryable + self.kept_unknown

    def summary(self) -> str:
        """One structured line for the run summary / journal (06 §6), and the
        artefact deploy/README tells the operator to paste into the cutover
        log — so its numbers have to add up."""
        what = "no-op" if self.no_op else ("created" if self.created else "migrated")
        text = (
            f"automations.db {what}: v{self.from_version}->v{self.to_version} "
            f"notes={self.notes_kept} success={self.kept_success} skip={self.kept_skip} "
            f"retryable={self.reset_retryable}"
        )
        if self.kept_unknown:
            text += f" unknown_status={self.kept_unknown}"
        return (
            f"{text} filtered_dropped={self.deleted_filtered} "
            f"anomalies={len(self.anomalies)}"
        )


@dataclass
class _Census:
    notes: int = 0
    by_status: dict[str, int] = field(default_factory=dict)

    def count(self, status: str) -> int:
        return self.by_status.get(status, 0)

    @property
    def retryable(self) -> int:
        return sum(n for s, n in self.by_status.items() if s in RETRYABLE_STATUSES)

    @property
    def unknown(self) -> int:
        """Rows whose status this build does not recognise. Preserved, named
        in ``anomalies`` — and now COUNTED, so the report reconciles against
        ``SELECT count(*) FROM emissions``."""
        return sum(n for s, n in self.by_status.items() if s not in KNOWN_STATUSES)


class AutomationStore:
    """One store per run. Use as a context manager.

    Only the orchestrator (``consumers/runner.py``) writes through this
    object — checkpointing is single-owner (06 §1, 08 §B12). ``__init__`` is
    pure: the connection opens in ``__enter__``.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._ready = False

    # --- lifecycle -------------------------------------------------------

    def __enter__(self) -> AutomationStore:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> None:
        """Connect and apply the 06 §1 connection settings (WAL,
        busy_timeout 5000). Idempotent."""
        if self._conn is not None:
            return
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        except (OSError, sqlite3.Error) as exc:  # pragma: no cover - env dependent
            raise StoreError(
                f"cannot open automations db at {self.db_path}: {exc}",
                hint="check the state dir exists and is writable (spec 10 §3)",
            ) from exc
        # B1 class: never let one bad byte of legacy metadata raise on SELECT.
        conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
        conn.row_factory = sqlite3.Row
        conn.isolation_level = None  # explicit BEGIN/COMMIT below
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            # WAL + synchronous=NORMAL: no fsync per COMMIT. The default
            # (FULL) costs ~1.4 ms per checkpoint on this machine, i.e.
            # ~10 s for a full 7.5k-note vault run against spec 09 §4's 30 s
            # budget — measured 1.40 s vs 0.06 s per 1000 checkpoints.
            # The trade is bounded and correct FOR THIS DB: under WAL,
            # NORMAL can lose only the most recent transactions on a power
            # cut, never corrupt the file, and a lost checkpoint means a
            # handful of notes are re-offered next run — which every
            # consumer is already required to survive (error/limit retry,
            # 06 §1). ``migrate()`` raises it back to FULL for its own
            # transaction, because the migration is a cutover artefact.
            conn.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.Error as exc:  # pragma: no cover - env dependent
            conn.close()
            raise StoreError(f"cannot configure automations db: {exc}") from exc
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
                self._ready = False

    # --- internals -------------------------------------------------------

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError(
                "AutomationStore used before open()",
                hint="use it as a context manager: `with AutomationStore(path) as store:`",
            )
        return self._conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._db
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _table_names(self) -> set[str]:
        rows = self._db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row["name"] for row in rows}

    def _columns(self, table: str) -> set[str]:
        rows = self._db.execute(f"PRAGMA table_info({table})")
        return {row["name"] for row in rows}

    def _meta_get(self, key: str) -> str | None:
        if "meta" not in self._table_names():
            return None
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _detect_version(self) -> int:
        """0 = empty file (nothing to migrate), 1 = live pre-rewrite schema,
        2 = current. Anything else aborts loudly (never half-migrate)."""
        tables = self._table_names()
        user_tables = {t for t in tables if not t.startswith("sqlite_")}
        if not user_tables:
            return 0

        raw_version = self._meta_get("schema_version")
        if raw_version is not None:
            try:
                version = int(raw_version)
            except ValueError as exc:
                raise StoreMigrationError(
                    f"meta.schema_version is not an integer: {raw_version!r}",
                    hint="refusing to touch the db; restore from the pre-migration backup",
                ) from exc
            if version > STORE_SCHEMA_VERSION:
                raise StoreMigrationError(
                    f"automations.db is schema v{version}, newer than this build "
                    f"(v{STORE_SCHEMA_VERSION})",
                    hint="upgrade organize-core; a downgrade would silently lose columns",
                )
            if version != STORE_SCHEMA_VERSION:
                raise StoreMigrationError(
                    f"unsupported schema_version {version} in {self.db_path}",
                    hint="no migration path is defined for this version",
                )
            return version

        missing = {"notes", "emissions"} - user_tables
        if missing:
            raise StoreMigrationError(
                f"{self.db_path} is not an automations db: missing table(s) "
                f"{sorted(missing)} (found {sorted(user_tables)})",
                hint="aborting rather than half-migrating; check state.dir/state.database",
            )
        return 1

    def _assert_v1_shape(self) -> list[str]:
        """Validate the v1 tables column-for-column. Returns anomalies that
        are tolerable; raises StoreMigrationError for anything that is not."""
        anomalies: list[str] = []
        notes_cols = self._columns("notes")
        emissions_cols = self._columns("emissions")

        missing_notes = _V1_NOTES_COLUMNS - notes_cols
        missing_emissions = _V1_EMISSIONS_COLUMNS - emissions_cols
        if missing_notes or missing_emissions:
            raise StoreMigrationError(
                "automations.db has an unexpected v1 shape: "
                f"notes missing {sorted(missing_notes)}, "
                f"emissions missing {sorted(missing_emissions)}",
                hint="aborting rather than half-migrating; restore the backup (09 §5.4)",
            )

        extra_notes = notes_cols - _V1_NOTES_COLUMNS - {"last_seen"}
        extra_emissions = emissions_cols - _V1_EMISSIONS_COLUMNS
        if extra_notes or extra_emissions:
            raise StoreMigrationError(
                "automations.db has unrecognised extra columns: "
                f"notes {sorted(extra_notes)}, emissions {sorted(extra_emissions)}",
                hint="aborting rather than half-migrating; this db was written by "
                "something other than the para-organize pipeline",
            )
        if "last_seen" in notes_cols:
            anomalies.append(
                "notes.last_seen already present on an unversioned db "
                "(a previous migration attempt?) — reused, not recreated"
            )
        return anomalies

    def _census(self) -> _Census:
        census = _Census()
        census.notes = int(self._db.execute("SELECT count(*) FROM notes").fetchone()[0])
        for row in self._db.execute("SELECT status, count(*) AS n FROM emissions GROUP BY status"):
            census.by_status[str(row["status"])] = int(row["n"])
        return census

    def _data_anomalies(self, *, check_empty_hash: bool = False) -> list[str]:
        anomalies: list[str] = []
        known = tuple(sorted(KNOWN_STATUSES))
        placeholders = ",".join("?" * len(known))
        unknown = self._db.execute(
            "SELECT status, count(*) AS n FROM emissions "
            f"WHERE status NOT IN ({placeholders}) GROUP BY status ORDER BY status",
            known,
        ).fetchall()
        for row in unknown:
            anomalies.append(
                f"emissions with unknown status {row['status']!r}: {row['n']} "
                "(kept; NOT treated as terminal, so those notes will be redelivered)"
            )
        orphans = int(
            self._db.execute(
                "SELECT count(*) FROM emissions e WHERE NOT EXISTS "
                "(SELECT 1 FROM notes n WHERE n.path = e.note_path)"
            ).fetchone()[0]
        )
        if orphans:
            anomalies.append(
                f"emission rows whose note_path has no notes row: {orphans} "
                "(kept — they are still valid checkpoints)"
            )
        relative = int(
            self._db.execute("SELECT count(*) FROM notes WHERE path NOT LIKE '/%'").fetchone()[0]
        )
        if relative:
            anomalies.append(
                f"notes rows with a non-absolute path: {relative} "
                "(kept, but they can never match a scanned note — 06 §1 canonicalisation)"
            )
        if check_empty_hash:
            # v1 declared note_hash NOT NULL and always wrote a real digest, so
            # an empty one there is corruption. In v2 `mark_seen` legitimately
            # inserts a hash-less placeholder row, hence the gate.
            empty_hash = int(
                self._db.execute("SELECT count(*) FROM notes WHERE note_hash = ''").fetchone()[0]
            )
            if empty_hash:
                anomalies.append(f"notes rows with an empty note_hash: {empty_hash}")
        return anomalies

    def _ensure_ready(self) -> None:
        """Guard every data method: a fresh file gets the v2 schema, an
        un-migrated v1 db is refused loudly (never silently queried)."""
        if self._ready:
            return
        version = self._detect_version()
        if version == 0:
            with self._transaction() as conn:
                _create_schema(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(STORE_SCHEMA_VERSION),),
                )
            self._db.execute(f"PRAGMA user_version = {STORE_SCHEMA_VERSION}")
        elif version == STORE_SCHEMA_VERSION:
            if "last_seen" not in self._columns("notes"):
                raise StoreMigrationError(
                    "automations.db claims schema v2 but notes.last_seen is missing",
                    hint="the db is inconsistent; restore the pre-migration backup",
                )
        else:
            raise StoreError(
                f"automations.db at {self.db_path} is schema v{version}, not "
                f"v{STORE_SCHEMA_VERSION}",
                hint="run the migration first (organize run-consumers migrates on start; "
                "see spec 09 §5.4 — back the db up before migrating)",
            )
        self._ready = True

    @staticmethod
    def _key(path: Path | str) -> str:
        """DB key for a note path. Paths in the DB are resolved ABSOLUTE
        paths (06 §1) — a relative key would orphan all history for that
        note, so it is rejected instead of written."""
        p = Path(path)
        if not p.is_absolute():
            raise StoreError(
                f"note path must be absolute and resolved, got {str(p)!r}",
                hint="the runner resolves paths before touching the store (06 §1)",
            )
        return str(p)

    @staticmethod
    def _load_metadata(raw: Any) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("unparseable metadata_json in automations.db; treating as empty")
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _dump_metadata(metadata: dict[str, Any] | None) -> str:
        return json.dumps(metadata or {}, default=str, ensure_ascii=False)

    # --- schema ----------------------------------------------------------

    def migrate(self) -> MigrationReport:
        """Bring the DB to STORE_SCHEMA_VERSION (spec 06 §1 migration):
        fresh DB ⇒ create schema; live v1 DB ⇒ add ``last_seen`` +
        version meta, KEEP success/skip rows as-is, DELETE ``filtered``
        rows (re-evaluated cheaply every run), treat limit/error as
        retryable. Aborts with StoreMigrationError on anything unexpected —
        never runs half-migrated. Caller backs the DB up first (09 §5.4).

        Idempotent: on an already-v2 DB nothing is written and the report
        says so (``no_op=True``, ``deleted_filtered=0``).
        """
        self.open()
        self._preflight_integrity()
        # A migration is a CUTOVER ARTEFACT (09 §5.4) — unlike a checkpoint
        # it is not cheaply redone from the vault, so it gets the durable
        # fsync that ``open()`` trades away for the per-run write path.
        try:
            self._db.execute("PRAGMA synchronous = FULL")
        except sqlite3.Error:  # pragma: no cover - env dependent
            pass
        try:
            return self._migrate_locked()
        finally:
            try:
                self._db.execute("PRAGMA synchronous = NORMAL")
            except sqlite3.Error:  # pragma: no cover - env dependent
                pass

    def _preflight_integrity(self) -> None:
        """``PRAGMA quick_check`` before a migration touches anything.

        A malformed image must abort with a NAMED, actionable message. Without
        this gate the corruption only surfaced deep inside the census pass as
        a bare ``sqlite3.DatabaseError``, which the CLI's last-resort handler
        reported as "internal error … this is a bug in organize, not
        something you did" — misdirecting the operator during the one
        operation where the right move is "restore the backup" (09 §5.4).

        ``quick_check`` (not ``integrity_check``) deliberately: it skips the
        expensive index cross-checks, so it stays cheap enough to run on
        every ``migrate()`` — measured well under the 15 MB live database's
        0.25 s migration.
        """
        try:
            rows = self._db.execute("PRAGMA quick_check(1)").fetchall()
        except sqlite3.Error as exc:
            raise StoreMigrationError(
                f"integrity check on {self.db_path} could not run: {exc}",
                hint="the database could not be read at all; restore the "
                "pre-migration backup (09 §5.4) and check the disk",
            ) from exc
        results = [str(row[0]) for row in rows if row]
        if results and results != ["ok"]:
            detail = "; ".join(results[:3])
            raise StoreMigrationError(
                f"{self.db_path} failed PRAGMA quick_check: {detail}",
                hint="nothing was written. This is a corrupt database file, not a "
                "schema problem — restore the pre-migration backup (09 §5.4); "
                "if there is none, `sqlite3 <db> .recover` salvages what it can.",
            )

    def _migrate_locked(self) -> MigrationReport:
        version = self._detect_version()

        if version == 0:
            self._ensure_ready()
            report = MigrationReport(
                from_version=0,
                to_version=STORE_SCHEMA_VERSION,
                kept_success=0,
                kept_skip=0,
                deleted_filtered=0,
                reset_retryable=0,
                notes_kept=0,
                anomalies=(),
                created=True,
            )
            log.info(report.summary())
            return report

        if version == STORE_SCHEMA_VERSION:
            self._ensure_ready()
            census = self._census()
            report = MigrationReport(
                from_version=version,
                to_version=STORE_SCHEMA_VERSION,
                kept_success=census.count("success"),
                kept_skip=census.count("skip"),
                deleted_filtered=0,
                reset_retryable=census.retryable,
                kept_unknown=census.unknown,
                notes_kept=census.notes,
                anomalies=tuple(self._data_anomalies()),
                no_op=True,
            )
            log.info(report.summary())
            return report

        # --- v1 -> v2, one transaction, all-or-nothing --------------------
        #
        # The inspection pass below TABLE-SCANS the database, so it is where
        # a partially-corrupt image first shows itself — and it runs before
        # the transaction, where a bare `sqlite3.DatabaseError` used to
        # escape as an unhandled exception. The CLI then told the operator
        # "internal error … this is a bug in organize, not something you did
        # … report it", with no mention of the backup, for the one failure
        # most likely to occur during a real cutover. Same taxonomy as every
        # other migration abort: a named StoreMigrationError that says
        # restore the backup.
        try:
            anomalies = self._assert_v1_shape()
            anomalies.extend(self._data_anomalies(check_empty_hash=True))
            before = self._census()
        except sqlite3.Error as exc:
            raise StoreMigrationError(
                f"reading {self.db_path} before migrating from v{version} failed: {exc}",
                hint="nothing was written — the database could not be read, which "
                "usually means a corrupt image. Restore the pre-migration backup "
                "(09 §5.4) and check the disk before retrying.",
            ) from exc
        stale_filtered = sum(n for s, n in before.by_status.items() if s in V1_ONLY_STATUSES)

        try:
            with self._transaction() as conn:
                if "last_seen" not in self._columns("notes"):
                    conn.execute("ALTER TABLE notes ADD COLUMN last_seen INTEGER")
                # Pre-v2 rows have no last_seen: seed it from seen_at so the
                # 30-day soft-purge window starts from real observation time
                # instead of the epoch (which would purge everything at once).
                conn.execute("UPDATE notes SET last_seen = seen_at WHERE last_seen IS NULL")
                dropped_statuses = tuple(sorted(V1_ONLY_STATUSES))
                placeholders = ",".join("?" * len(dropped_statuses))
                cursor = conn.execute(
                    f"DELETE FROM emissions WHERE status IN ({placeholders})",
                    dropped_statuses,
                )
                deleted = int(cursor.rowcount if cursor.rowcount is not None else 0)
                _create_schema(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(STORE_SCHEMA_VERSION),),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('migrated_from', ?)",
                    (str(version),),
                )
        except sqlite3.Error as exc:
            raise StoreMigrationError(
                f"migrating {self.db_path} from v{version} failed: {exc}",
                hint="the db was rolled back untouched; restore the backup (09 §5.4)",
            ) from exc

        self._db.execute(f"PRAGMA user_version = {STORE_SCHEMA_VERSION}")
        self._ready = True

        if deleted != stale_filtered:  # pragma: no cover - defensive
            anomalies.append(
                f"expected to drop {stale_filtered} filtered rows, dropped {deleted}"
            )
        if deleted:
            try:
                self._db.execute("VACUUM")
            except sqlite3.Error as exc:  # pragma: no cover - env dependent
                anomalies.append(f"VACUUM after dropping filtered rows failed: {exc}")

        after = self._census()
        report = MigrationReport(
            from_version=version,
            to_version=STORE_SCHEMA_VERSION,
            kept_success=after.count("success"),
            kept_skip=after.count("skip"),
            deleted_filtered=deleted,
            reset_retryable=after.retryable,
            kept_unknown=after.unknown,
            notes_kept=after.notes,
            anomalies=tuple(anomalies),
        )
        log.info(report.summary())
        for anomaly in report.anomalies:
            log.warning("automations.db migration anomaly: %s", anomaly)
        return report

    def schema_version(self) -> int:
        """Version recorded in ``meta`` (0 for an empty file)."""
        self.open()
        return self._detect_version()

    # --- emission / checkpoint API (orchestrator-only writers, 06 §1) ----

    def needs_delivery(self, consumer: str, path: Path, note_hash: str) -> bool:
        """True iff ``note_hash`` differs from this consumer's last TERMINAL
        emission for ``path`` (spec 06 §1 delivery rule; error/limit rows do
        not block redelivery)."""
        self._ensure_ready()
        row = self._db.execute(
            "SELECT note_hash, status FROM emissions WHERE consumer = ? AND note_path = ?",
            (consumer, self._key(path)),
        ).fetchone()
        if row is None:
            return True
        if str(row["status"]) not in TERMINAL_STATUSES:
            return True
        return str(row["note_hash"]) != note_hash

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
        checkpointed rule (06 §1), and this method refuses anything else so
        08 §B3 (``limit`` checkpointed ⇒ note dropped forever) cannot come
        back through a second door."""
        if status not in TERMINAL_STATUSES:
            raise StoreError(
                f"refusing to checkpoint non-terminal status {status!r} for "
                f"consumer {consumer!r}",
                hint="only success/skip are checkpointed; error/limit are retried "
                "next run (spec 06 §1, 08 §B3)",
            )
        self._ensure_ready()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO emissions(
                    consumer, note_path, note_hash, emitted_at, status, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(consumer, note_path) DO UPDATE SET
                    note_hash = excluded.note_hash,
                    emitted_at = excluded.emitted_at,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json
                """,
                (
                    consumer,
                    self._key(path),
                    note_hash,
                    int(now),
                    status,
                    self._dump_metadata(metadata),
                ),
            )

    def get_emission(self, consumer: str, path: Path) -> Emission | None:
        self._ensure_ready()
        row = self._db.execute(
            "SELECT consumer, note_path, note_hash, emitted_at, status, metadata_json "
            "FROM emissions WHERE consumer = ? AND note_path = ?",
            (consumer, self._key(path)),
        ).fetchone()
        if row is None:
            return None
        return Emission(
            consumer=str(row["consumer"]),
            note_path=str(row["note_path"]),
            note_hash=str(row["note_hash"]),
            emitted_at=int(row["emitted_at"]),
            status=str(row["status"]),
            metadata=self._load_metadata(row["metadata_json"]),
        )

    # --- notes table / soft purge (06 §1) --------------------------------

    def mark_seen(
        self,
        paths: list[Path],
        *,
        now: int,
        hashes: Mapping[Path, str] | None = None,
    ) -> None:
        """Record every path seen this run: ``last_seen``, and the note hash
        when the caller has it.

        Paths with no ``notes`` row are inserted so that a note the store has
        never recorded still counts as *seen* and can never be purged.

        ``hashes`` matters: this is the ONLY writer of the ``notes`` table on
        the run path, so without it every v2 row carried ``note_hash = ''``
        forever and the column was permanently useless for diagnostics ("did
        this file change since we last walked it?"). It is still not a
        delivery decision — that is :meth:`needs_delivery`, per consumer,
        against ``emissions``.

        The placeholder can never clobber a real hash: the upsert keeps the
        stored value whenever the incoming one is empty, so a caller that
        omits ``hashes`` is exactly as conservative as before.

        One batched statement, deliberately: the runner calls this once with
        every scanned path (7 500 on the real vault) inside a 30 s budget
        (09 §4), so a per-note round trip is not available.
        """
        if not paths:
            return
        self._ensure_ready()
        lookup = {} if hashes is None else {self._key(k): v for k, v in hashes.items()}
        rows = [
            (key, str(lookup.get(key, "")), int(now), int(now))
            for key in (self._key(p) for p in paths)
        ]
        with self._transaction() as conn:
            conn.executemany(
                """
                INSERT INTO notes(path, note_hash, metadata_json, seen_at, last_seen)
                VALUES (?, ?, NULL, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    note_hash = CASE
                        WHEN excluded.note_hash = '' THEN notes.note_hash
                        ELSE excluded.note_hash
                    END
                """,
                rows,
            )

    def soft_purge(
        self,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        scan_dirs_ok: bool,
        now: int,
        scan_roots: Sequence[Path] | None = None,
    ) -> int:
        """Retire rows unseen for ``retention_days`` — but ONLY when
        ``scan_dirs_ok`` (every configured scan dir existed and was
        non-empty this run); a transient mount must never forget a year of
        checkpoints (spec 06 §1, 08 §B5). Returns rows purged.

        ``scan_roots`` (the resolved directories actually walked this run)
        bounds WHICH rows are even candidates. A row is only purged if it
        lies under one of them, because ``last_seen`` means "we looked and
        it was not there" — and we only looked inside the scan dirs. Without
        this bound, NARROWING ``vault.scan_dirs`` silently destroyed the
        removed directory's emission history 30 days later (every remaining
        dir is healthy, so ``scan_dirs_ok`` is True), and re-adding the
        directory re-ran every LLM emission for those notes. That is the
        duplicate-output harm 08 §B5 exists to prevent, and it is the 06 §7
        acceptance bullet "removing a scan dir from config does not delete
        its emission history".

        ``None`` means "no bound" — every row is a candidate, the pre-
        existing behaviour, kept for callers that are not the runner.

        The purge is SOFT in the sense that matters after an incident: rows
        leave ``notes``/``emissions`` (so they no longer participate in
        delivery decisions) but are archived into ``purged_notes`` /
        ``purged_emissions`` with a ``purged_at`` stamp, and
        :meth:`restore_purged` puts them back. Nothing this store does is
        unrecoverable.
        """
        if retention_days < 0:
            raise StoreError(
                f"retention_days must be >= 0, got {retention_days}",
                hint="a negative window would purge notes seen in the future",
            )
        if not scan_dirs_ok:
            log.warning(
                "skipping purge: a configured scan dir was missing or empty this run "
                "(spec 06 §1 / 08 §B5 guard)"
            )
            return 0
        self._ensure_ready()
        cutoff = int(now) - retention_days * _SECONDS_PER_DAY
        candidates = [
            str(row["path"])
            for row in self._db.execute(
                "SELECT path FROM notes WHERE COALESCE(last_seen, seen_at) < ?",
                (cutoff,),
            )
        ]
        if scan_roots is None:
            stale = candidates
        else:
            prefixes = tuple(f"{self._key(root)}/" for root in scan_roots)
            stale = [p for p in candidates if p.startswith(prefixes)]
            withheld = len(candidates) - len(stale)
            if withheld:
                log.info(
                    "purge: %d stale row(s) left alone because they sit outside the "
                    "currently configured scan dirs — removing a scan dir must not "
                    "delete its emission history (06 §7 / 08 §B5)",
                    withheld,
                )
        if not stale:
            return 0

        with self._transaction() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _purge_paths (path TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _purge_paths")
            conn.executemany(
                "INSERT OR IGNORE INTO _purge_paths(path) VALUES (?)", ((p,) for p in stale)
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO purged_notes(
                    path, note_hash, metadata_json, seen_at, last_seen, purged_at)
                SELECT n.path, n.note_hash, n.metadata_json, n.seen_at, n.last_seen, ?
                FROM notes n JOIN _purge_paths p ON p.path = n.path
                """,
                (int(now),),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO purged_emissions(
                    consumer, note_path, note_hash, emitted_at, status, metadata_json, purged_at)
                SELECT e.consumer, e.note_path, e.note_hash, e.emitted_at, e.status,
                       e.metadata_json, ?
                FROM emissions e JOIN _purge_paths p ON p.path = e.note_path
                """,
                (int(now),),
            )
            conn.execute("DELETE FROM emissions WHERE note_path IN (SELECT path FROM _purge_paths)")
            conn.execute("DELETE FROM notes WHERE path IN (SELECT path FROM _purge_paths)")
            conn.execute("DROP TABLE _purge_paths")

        log.warning(
            "purged %d note(s) unseen for >%d days into the restore archive "
            "(restore_purged() undoes this)",
            len(stale),
            retention_days,
        )
        return len(stale)

    def list_purged(self) -> list[str]:
        """Paths currently sitting in the purge archive (the restore path).

        Reachable from the CLI as ``organize purged list`` — an archive an
        operator cannot enumerate is not a restore path, and ``soft_purge``'s
        own WARN advertises one.
        """
        self._ensure_ready()
        return [str(row["path"]) for row in self._db.execute("SELECT path FROM purged_notes")]

    def sweep_purged(self, *, retention_days: int = ARCHIVE_RETENTION_DAYS, now: int) -> int:
        """Hard-delete archive rows older than ``retention_days``.

        Without this the archive is the only table nothing ever deletes from
        except :meth:`restore_purged`, so the 15 MB bloat 08 §B4 removed from
        ``emissions`` simply migrated into ``purged_*``.

        Returns the number of archived NOTE rows removed.
        """
        if retention_days < 0:
            raise StoreError(
                f"retention_days must be >= 0, got {retention_days}",
                hint="a negative window would delete rows archived in the future",
            )
        self._ensure_ready()
        cutoff = int(now) - retention_days * _SECONDS_PER_DAY
        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM purged_notes WHERE purged_at < ?", (cutoff,))
            removed = int(cursor.rowcount if cursor.rowcount is not None else 0)
            conn.execute("DELETE FROM purged_emissions WHERE purged_at < ?", (cutoff,))
        if removed:
            log.info(
                "purge archive: dropped %d note(s) archived more than %d days ago",
                removed,
                retention_days,
            )
        return removed

    def restore_purged(self, paths: list[Path] | None = None) -> int:
        """Undo :meth:`soft_purge` for ``paths`` (default: everything
        archived). Existing live rows win — a restore never clobbers newer
        state. Returns the number of note rows restored."""
        self._ensure_ready()
        keys = None if paths is None else [self._key(p) for p in paths]
        with self._transaction() as conn:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _restore_paths (path TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _restore_paths")
            if keys is None:
                conn.execute("INSERT INTO _restore_paths(path) SELECT path FROM purged_notes")
            else:
                conn.executemany(
                    "INSERT OR IGNORE INTO _restore_paths(path) VALUES (?)",
                    ((k,) for k in keys),
                )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO notes(path, note_hash, metadata_json, seen_at, last_seen)
                SELECT n.path, n.note_hash, n.metadata_json, n.seen_at, n.last_seen
                FROM purged_notes n JOIN _restore_paths r ON r.path = n.path
                """
            )
            restored = int(cursor.rowcount if cursor.rowcount is not None else 0)
            conn.execute(
                """
                INSERT OR IGNORE INTO emissions(
                    consumer, note_path, note_hash, emitted_at, status, metadata_json)
                SELECT e.consumer, e.note_path, e.note_hash, e.emitted_at, e.status,
                       e.metadata_json
                FROM purged_emissions e JOIN _restore_paths r ON r.path = e.note_path
                """
            )
            conn.execute("DELETE FROM purged_notes WHERE path IN (SELECT path FROM _restore_paths)")
            conn.execute(
                "DELETE FROM purged_emissions WHERE note_path IN (SELECT path FROM _restore_paths)"
            )
            conn.execute("DROP TABLE _restore_paths")
        return restored
