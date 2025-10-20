"""SQLite-backed state store for automation pipelines."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence, Tuple

from .notes import NotePayload


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS notes (
    path TEXT PRIMARY KEY,
    note_hash TEXT NOT NULL,
    metadata_json TEXT,
    seen_at INTEGER NOT NULL
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
"""


def _now() -> int:
    return int(time.time())


class AutomationStore:
    """Persistence layer for note hashes and consumer emission checkpoints."""

    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._conn = sqlite3.connect(str(database_path))
        self._conn.row_factory = sqlite3.Row
        self._initialise()

    def _initialise(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def get_note_hash(self, path: Path) -> Optional[str]:
        cursor = self._conn.execute(
            "SELECT note_hash FROM notes WHERE path = ?",
            (str(path),),
        )
        row = cursor.fetchone()
        return str(row["note_hash"]) if row else None

    def upsert_note(self, note: NotePayload) -> Optional[str]:
        previous = self.get_note_hash(note.path)
        metadata_json = json.dumps(note.frontmatter, sort_keys=True)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO notes(path, note_hash, metadata_json, seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    note_hash = excluded.note_hash,
                    metadata_json = excluded.metadata_json,
                    seen_at = excluded.seen_at
                """,
                (str(note.path), note.note_hash, metadata_json, _now()),
            )
        return previous

    def purge_missing(self, existing_paths: Iterable[Path]) -> None:
        keep = {str(path) for path in existing_paths}
        cursor = self._conn.execute("SELECT path FROM notes")
        missing = [row["path"] for row in cursor if row["path"] not in keep]
        if not missing:
            return
        with self._conn:
            self._conn.executemany("DELETE FROM notes WHERE path = ?", ((path,) for path in missing))
            self._conn.executemany(
                "DELETE FROM emissions WHERE note_path = ?",
                ((path,) for path in missing),
            )

    def get_emission_hash(self, consumer: str, note_path: Path) -> Optional[str]:
        cursor = self._conn.execute(
            """
            SELECT note_hash FROM emissions
            WHERE consumer = ? AND note_path = ?
            """,
            (consumer, str(note_path)),
        )
        row = cursor.fetchone()
        return str(row["note_hash"]) if row else None

    def needs_emission(self, consumer: str, note_path: Path, note_hash: str) -> bool:
        recorded = self.get_emission_hash(consumer, note_path)
        return recorded != note_hash

    def mark_emitted(
        self,
        consumer: str,
        note_path: Path,
        note_hash: str,
        status: str = "success",
        metadata: Optional[dict] = None,
    ) -> None:
        metadata_json = json.dumps(metadata or {}, sort_keys=True) if metadata else None
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO emissions(consumer, note_path, note_hash, emitted_at, status, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(consumer, note_path) DO UPDATE SET
                    note_hash = excluded.note_hash,
                    emitted_at = excluded.emitted_at,
                    status = excluded.status,
                    metadata_json = excluded.metadata_json
                """,
                (consumer, str(note_path), note_hash, _now(), status, metadata_json),
            )

    def iter_notes(self) -> Iterator[tuple[str, str]]:
        cursor = self._conn.execute("SELECT path, note_hash FROM notes")
        for row in cursor:
            yield row["path"], row["note_hash"]

    def iter_consumer_emissions(self, consumer: str) -> Iterator[tuple[str, str]]:
        cursor = self._conn.execute(
            "SELECT note_path, note_hash FROM emissions WHERE consumer = ?",
            (consumer,),
        )
        for row in cursor:
            yield row["note_path"], row["note_hash"]
