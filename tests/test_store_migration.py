"""v1 → v2 migration of ``automations.db`` (spec 06 §1, 09 §5.4).

Two layers:

1. Synthetic v1 fixtures for the edge shapes (empty file, unknown status
   values, missing/extra tables ⇒ loud abort, idempotency).
2. The mandatory acceptance test (06 §7) against a **copy of a copy** of the
   real 15 MB live database. The live file at
   ``~/.local/state/para-organize/automations.db`` is NEVER opened: the
   read-only mirror at ``.mirror-work/live-db-copy/`` is copied into
   ``tmp_path`` first, and only that copy is written to.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from organize_core.consumers.store import (
    STORE_SCHEMA_VERSION,
    AutomationStore,
)
from organize_core.errors import StoreError, StoreMigrationError

NOW = 1_700_000_000

#: Located from this file, never from a hardcoded machine path (08 §A37):
#: ``<repo>/../.mirror-work/live-db-copy/automations.db`` is the read-only
#: (chmod a-w) mirror the integrator took of the live database.
REPO_ROOT = Path(__file__).resolve().parents[1]
MIRROR_DIR = REPO_ROOT.parent / ".mirror-work" / "live-db-copy"
LIVE_DB_COPY = MIRROR_DIR / "automations.db"

#: Byte-for-byte the schema the live pipeline created
#: (``scripts/automation/store.py``) — no ``last_seen``, no ``meta``.
V1_SCHEMA = """
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


def make_v1_db(
    path: Path,
    notes: list[tuple[str, str]] | None = None,
    emissions: list[tuple[str, str, str, str]] | None = None,
    *,
    schema: str = V1_SCHEMA,
) -> Path:
    """Build a synthetic pre-rewrite DB. ``notes`` are (path, hash);
    ``emissions`` are (consumer, note_path, hash, status)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(schema)
        for note_path, note_hash in notes or []:
            conn.execute(
                "INSERT INTO notes(path, note_hash, metadata_json, seen_at) VALUES (?, ?, ?, ?)",
                (note_path, note_hash, "{}", NOW),
            )
        for consumer, note_path, note_hash, status in emissions or []:
            conn.execute(
                "INSERT INTO emissions(consumer, note_path, note_hash, emitted_at, status, "
                "metadata_json) VALUES (?, ?, ?, ?, ?, ?)",
                (consumer, note_path, note_hash, NOW, status, "{}"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def census(path: Path) -> dict[str, object]:
    conn = sqlite3.connect(str(path))
    conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
    conn.row_factory = sqlite3.Row
    try:
        return {
            "notes": conn.execute("SELECT count(*) FROM notes").fetchone()[0],
            "emissions": conn.execute("SELECT count(*) FROM emissions").fetchone()[0],
            "by_status": {
                str(row["status"]): int(row["n"])
                for row in conn.execute(
                    "SELECT status, count(*) AS n FROM emissions GROUP BY status"
                )
            },
            "terminal_rows": [
                tuple(row)
                for row in conn.execute(
                    "SELECT consumer, note_path, note_hash, emitted_at, status FROM emissions "
                    "WHERE status IN ('success', 'skip') ORDER BY consumer, note_path"
                )
            ],
        }
    finally:
        conn.close()


# --- synthetic v1 edge shapes ------------------------------------------------


def test_empty_file_is_created_not_migrated(tmp_path: Path) -> None:
    db = tmp_path / "automations.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.touch()
    with AutomationStore(db) as store:
        report = store.migrate()
    assert (report.from_version, report.to_version) == (0, STORE_SCHEMA_VERSION)
    assert report.created is True
    assert report.no_op is False
    assert (report.notes_kept, report.kept_success, report.deleted_filtered) == (0, 0, 0)
    assert report.anomalies == ()
    assert "created" in report.summary()


def test_missing_db_file_is_created(tmp_path: Path) -> None:
    with AutomationStore(tmp_path / "state" / "automations.db") as store:
        report = store.migrate()
    assert report.created is True


def test_empty_v1_db_migrates_to_v2(tmp_path: Path) -> None:
    db = make_v1_db(tmp_path / "automations.db")
    with AutomationStore(db) as store:
        report = store.migrate()
        assert store.schema_version() == STORE_SCHEMA_VERSION
    assert (report.from_version, report.to_version) == (1, 2)
    assert report.changed is True
    assert (report.notes_kept, report.kept_success, report.deleted_filtered) == (0, 0, 0)


def test_v1_migration_keeps_terminal_rows_and_drops_filtered(tmp_path: Path) -> None:
    db = make_v1_db(
        tmp_path / "automations.db",
        notes=[("/v/a.md", "h1"), ("/v/b.md", "h2"), ("/v/c.md", "h3")],
        emissions=[
            ("taskwarrior", "/v/a.md", "h1", "success"),
            ("taskwarrior", "/v/b.md", "h2", "skip"),
            ("taskwarrior", "/v/c.md", "h3", "filtered"),
            ("learn", "/v/a.md", "h1", "filtered"),
            ("learn", "/v/b.md", "h2", "error"),
        ],
    )
    before = census(db)
    with AutomationStore(db) as store:
        report = store.migrate()
    after = census(db)

    assert (report.kept_success, report.kept_skip) == (1, 1)
    assert report.deleted_filtered == before["by_status"]["filtered"] == 2
    assert report.reset_retryable == 1
    assert report.notes_kept == 3
    assert after["by_status"] == {"success": 1, "skip": 1, "error": 1}
    assert after["terminal_rows"] == before["terminal_rows"]  # row for row
    assert after["notes"] == before["notes"]


def test_filtered_rows_dropped_means_config_widening_applies_retroactively(
    tmp_path: Path,
) -> None:
    """B4 regression at the store layer: a v1 `filtered` checkpoint must not
    keep a note from being delivered once the config includes it."""
    db = make_v1_db(
        tmp_path / "automations.db",
        notes=[("/v/old.md", "h1")],
        emissions=[("learn", "/v/old.md", "h1", "filtered")],
    )
    with AutomationStore(db) as store:
        store.migrate()
        assert store.needs_delivery("learn", Path("/v/old.md"), "h1") is True


def test_v1_error_rows_are_kept_but_retryable(tmp_path: Path) -> None:
    db = make_v1_db(
        tmp_path / "automations.db",
        notes=[("/v/a.md", "h1")],
        emissions=[("deep_research", "/v/a.md", "h1", "error")],
    )
    with AutomationStore(db) as store:
        report = store.migrate()
        assert report.reset_retryable == 1
        assert store.needs_delivery("deep_research", Path("/v/a.md"), "h1") is True
        emission = store.get_emission("deep_research", Path("/v/a.md"))
        assert emission is not None and emission.status == "error"


def test_last_seen_is_seeded_from_seen_at(tmp_path: Path) -> None:
    """Otherwise every migrated row would look 'unseen since the epoch' and
    the first soft purge would retire the entire history at once."""
    db = make_v1_db(tmp_path / "automations.db", notes=[("/v/a.md", "h1")])
    with AutomationStore(db) as store:
        store.migrate()
        row = store._db.execute("SELECT seen_at, last_seen FROM notes").fetchone()
        assert row["last_seen"] == row["seen_at"] == NOW
        assert store.soft_purge(scan_dirs_ok=True, now=NOW + 86400) == 0


def test_unknown_status_values_are_reported_not_silently_dropped(tmp_path: Path) -> None:
    db = make_v1_db(
        tmp_path / "automations.db",
        notes=[("/v/a.md", "h1"), ("/v/b.md", "h2")],
        emissions=[
            ("taskwarrior", "/v/a.md", "h1", "weird"),
            ("taskwarrior", "/v/b.md", "h2", "WEIRD"),
        ],
    )
    with AutomationStore(db) as store:
        report = store.migrate()
        assert any("weird" in a for a in report.anomalies)
        assert any("WEIRD" in a for a in report.anomalies)
        # kept (never silently destroyed) but NOT treated as terminal
        assert store.get_emission("taskwarrior", Path("/v/a.md")) is not None
        assert store.needs_delivery("taskwarrior", Path("/v/a.md"), "h1") is True
    assert census(db)["emissions"] == 2


def test_orphan_emissions_are_reported_and_kept(tmp_path: Path) -> None:
    db = make_v1_db(
        tmp_path / "automations.db",
        emissions=[("taskwarrior", "/v/vanished.md", "h1", "success")],
    )
    with AutomationStore(db) as store:
        report = store.migrate()
        assert any("no notes row" in a for a in report.anomalies)
        assert report.kept_success == 1


def test_relative_paths_in_a_v1_db_are_reported(tmp_path: Path) -> None:
    db = make_v1_db(tmp_path / "automations.db", notes=[("relative/a.md", "h1")])
    with AutomationStore(db) as store:
        report = store.migrate()
    assert any("non-absolute path" in a for a in report.anomalies)


# --- idempotency -------------------------------------------------------------


def test_second_migrate_is_a_no_op(tmp_path: Path) -> None:
    db = make_v1_db(
        tmp_path / "automations.db",
        notes=[("/v/a.md", "h1"), ("/v/b.md", "h2")],
        emissions=[
            ("taskwarrior", "/v/a.md", "h1", "success"),
            ("taskwarrior", "/v/b.md", "h2", "filtered"),
        ],
    )
    with AutomationStore(db) as store:
        first = store.migrate()
        after_first = census(db)
        second = store.migrate()
    after_second = census(db)

    assert first.changed is True
    assert second.no_op is True
    assert (second.from_version, second.to_version) == (2, 2)
    assert second.deleted_filtered == 0
    assert (second.kept_success, second.kept_skip, second.notes_kept) == (
        first.kept_success,
        first.kept_skip,
        first.notes_kept,
    )
    assert after_second == after_first
    assert "no-op" in second.summary()


def test_migrate_in_a_fresh_process_is_still_a_no_op(tmp_path: Path) -> None:
    db = make_v1_db(tmp_path / "automations.db", notes=[("/v/a.md", "h1")])
    with AutomationStore(db) as store:
        store.migrate()
    with AutomationStore(db) as store:
        assert store.migrate().no_op is True


# --- loud aborts (never half-migrate) ---------------------------------------


def test_missing_emissions_table_aborts_loudly(tmp_path: Path) -> None:
    db = make_v1_db(
        tmp_path / "automations.db",
        schema="CREATE TABLE notes (path TEXT PRIMARY KEY, note_hash TEXT NOT NULL, "
        "metadata_json TEXT, seen_at INTEGER NOT NULL);",
    )
    with AutomationStore(db) as store, pytest.raises(StoreMigrationError) as excinfo:
        store.migrate()
    assert "emissions" in str(excinfo.value)
    assert "meta" not in census_tables(db)


def test_foreign_database_aborts_loudly(tmp_path: Path) -> None:
    db = tmp_path / "not-automations.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE bookmarks (url TEXT)")
    conn.commit()
    conn.close()
    with AutomationStore(db) as store, pytest.raises(StoreMigrationError) as excinfo:
        store.migrate()
    assert "not an automations db" in str(excinfo.value)


def test_unexpected_extra_column_aborts_loudly(tmp_path: Path) -> None:
    schema = V1_SCHEMA.replace(
        "seen_at INTEGER NOT NULL\n);", "seen_at INTEGER NOT NULL,\n    surprise TEXT\n);"
    )
    db = make_v1_db(tmp_path / "automations.db", schema=schema)
    with AutomationStore(db) as store, pytest.raises(StoreMigrationError) as excinfo:
        store.migrate()
    assert "surprise" in str(excinfo.value)


def test_future_schema_version_aborts_loudly(tmp_path: Path) -> None:
    db = make_v1_db(tmp_path / "automations.db")
    with AutomationStore(db) as store:
        store.migrate()
        store._db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
        with pytest.raises(StoreMigrationError) as excinfo:
            store.migrate()
    assert "newer than this build" in str(excinfo.value)


def test_non_integer_schema_version_aborts_loudly(tmp_path: Path) -> None:
    db = make_v1_db(tmp_path / "automations.db")
    with AutomationStore(db) as store:
        store.migrate()
        store._db.execute("UPDATE meta SET value = 'banana' WHERE key = 'schema_version'")
        with pytest.raises(StoreMigrationError):
            store.migrate()


def test_v2_claim_without_last_seen_aborts_loudly(tmp_path: Path) -> None:
    db = make_v1_db(tmp_path / "automations.db")
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    conn.commit()
    conn.close()
    with AutomationStore(db) as store, pytest.raises(StoreMigrationError) as excinfo:
        store.migrate()
    assert "last_seen" in str(excinfo.value)


def test_using_an_unmigrated_v1_db_is_refused(tmp_path: Path) -> None:
    """No silent queries against a v1 store — the missing `last_seen` column
    would make soft_purge behave differently than the operator expects."""
    db = make_v1_db(tmp_path / "automations.db", notes=[("/v/a.md", "h1")])
    with AutomationStore(db) as store, pytest.raises(StoreError) as excinfo:
        store.needs_delivery("taskwarrior", Path("/v/a.md"), "h1")
    assert "schema v1" in str(excinfo.value)


def test_a_failed_migration_leaves_the_db_untouched(tmp_path: Path) -> None:
    db = make_v1_db(
        tmp_path / "automations.db",
        notes=[("/v/a.md", "h1")],
        emissions=[
            ("taskwarrior", "/v/a.md", "h1", "success"),
            ("learn", "/v/a.md", "h1", "filtered"),
        ],
        schema=V1_SCHEMA.replace(
            "metadata_json TEXT,\n    PRIMARY KEY", "metadata_json TEXT,\n    extra TEXT,\n"
            "    PRIMARY KEY"
        ),
    )
    before = census(db)
    with AutomationStore(db) as store, pytest.raises(StoreMigrationError):
        store.migrate()
    assert census(db) == before  # filtered row still there: nothing half-done


def test_a_crash_mid_migration_rolls_the_whole_thing_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filtered-row DELETE happens before the meta table is written; if
    anything after it fails, the DB must come back exactly as it was rather
    than silently losing 22k rows AND not being marked as migrated."""
    db = make_v1_db(
        tmp_path / "automations.db",
        notes=[("/v/a.md", "h1")],
        emissions=[
            ("taskwarrior", "/v/a.md", "h1", "success"),
            ("learn", "/v/a.md", "h1", "filtered"),
        ],
    )
    before = census(db)

    import organize_core.consumers.store as store_mod

    def boom(_conn: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store_mod, "_create_schema", boom)
    with AutomationStore(db) as store, pytest.raises(StoreMigrationError) as excinfo:
        store.migrate()
    assert "rolled back" in str(excinfo.value.hint or "")
    assert census(db) == before
    assert "meta" not in census_tables(db)


def census_tables(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# --- the mandatory live-DB acceptance test (06 §7, 09 §5.4) -----------------


@pytest.fixture(scope="module")
def live_copy(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """A writable COPY of the read-only mirror of the live 15 MB DB, plus the
    pre-migration census and the post-migration report."""
    if not LIVE_DB_COPY.exists():  # pragma: no cover - machine dependent
        pytest.skip(f"live DB mirror not present at {LIVE_DB_COPY}")
    workdir = tmp_path_factory.mktemp("live-db")
    db = workdir / "automations.db"
    shutil.copy2(LIVE_DB_COPY, db)
    db.chmod(0o644)  # the mirror is chmod a-w on purpose
    before = census(db)
    with AutomationStore(db) as store:
        report = store.migrate()
    return {"db": db, "before": before, "report": report}


def test_live_db_mirror_is_read_only_and_untouched() -> None:
    """Guard rail: the only real-data file this suite may look at is the
    integrator's mirror, and that mirror must stay unwritable — every test
    works on a tmp_path copy of it."""
    if not LIVE_DB_COPY.exists():  # pragma: no cover - machine dependent
        pytest.skip("live DB mirror not present")
    assert LIVE_DB_COPY.stat().st_mode & 0o222 == 0, "the mirror must stay chmod a-w"
    assert LIVE_DB_COPY.parent == MIRROR_DIR


def test_live_db_is_recognised_as_v1(live_copy: dict[str, object]) -> None:
    report = live_copy["report"]
    assert (report.from_version, report.to_version) == (1, STORE_SCHEMA_VERSION)
    assert report.changed is True


def test_live_db_report_reconciles_with_pre_migration_queries(
    live_copy: dict[str, object],
) -> None:
    before = live_copy["before"]
    report = live_copy["report"]
    by_status = before["by_status"]

    assert report.notes_kept == before["notes"]
    assert report.kept_success == by_status.get("success", 0)
    assert report.kept_skip == by_status.get("skip", 0)
    assert report.deleted_filtered == by_status.get("filtered", 0)
    assert report.reset_retryable == by_status.get("error", 0) + by_status.get("limit", 0)
    # every pre-migration row is accounted for exactly once
    assert (
        report.kept_success
        + report.kept_skip
        + report.deleted_filtered
        + report.reset_retryable
        + sum(n for s, n in by_status.items() if s not in {"success", "skip", "filtered",
                                                           "error", "limit"})
        == before["emissions"]
    )
    # this is the real DB: it must actually contain the history we care about
    assert report.kept_success > 0
    assert report.deleted_filtered > 0


def test_live_db_success_history_preserved_row_for_row(live_copy: dict[str, object]) -> None:
    """06 §7: no historical `success` checkpoint may be lost."""
    after = census(live_copy["db"])
    assert after["terminal_rows"] == live_copy["before"]["terminal_rows"]
    assert after["notes"] == live_copy["before"]["notes"]


def test_live_db_filtered_rows_are_gone(live_copy: dict[str, object]) -> None:
    after = census(live_copy["db"])
    assert "filtered" not in after["by_status"]
    assert after["emissions"] == live_copy["before"]["emissions"] - (
        live_copy["report"].deleted_filtered
    )


def test_live_db_needs_delivery_sanity_after_migration(live_copy: dict[str, object]) -> None:
    """The migrated store answers the delivery question correctly for real
    rows: an unchanged success-checkpointed note stays quiet, an edited one
    re-fires, and a previously `filtered` note is now offered again."""
    db = live_copy["db"]
    conn = sqlite3.connect(str(db))
    conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
    conn.row_factory = sqlite3.Row
    try:
        successes = [
            (str(r["consumer"]), str(r["note_path"]), str(r["note_hash"]))
            for r in conn.execute(
                "SELECT consumer, note_path, note_hash FROM emissions "
                "WHERE status = 'success' ORDER BY note_path LIMIT 25"
            )
        ]
        never_emitted = [
            str(r["path"])
            for r in conn.execute(
                "SELECT n.path FROM notes n WHERE NOT EXISTS "
                "(SELECT 1 FROM emissions e WHERE e.note_path = n.path) LIMIT 25"
            )
        ]
    finally:
        conn.close()

    assert successes, "the live DB should carry success checkpoints"
    assert never_emitted, "filtered-only notes should have lost their rows"

    with AutomationStore(db) as store:
        for consumer, path, note_hash in successes:
            assert store.needs_delivery(consumer, Path(path), note_hash) is False
            assert store.needs_delivery(consumer, Path(path), "edited-" + note_hash) is True
        for path in never_emitted:
            assert store.needs_delivery("learn", Path(path), "whatever") is True


def test_live_db_second_migrate_is_a_no_op(live_copy: dict[str, object]) -> None:
    db = live_copy["db"]
    before = census(db)
    with AutomationStore(db) as store:
        again = store.migrate()
    assert again.no_op is True
    assert again.deleted_filtered == 0
    assert again.kept_success == live_copy["report"].kept_success
    assert census(db) == before


def test_live_db_purge_guard_holds_on_real_data(live_copy: dict[str, object]) -> None:
    """B5 on real volumes: an empty scan (transient mount) must not delete
    thousands of rows of history."""
    db = live_copy["db"]
    before = census(db)
    with AutomationStore(db) as store:
        assert store.soft_purge(scan_dirs_ok=False, now=NOW + 10**9) == 0
    assert census(db) == before
