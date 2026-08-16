"""AutomationStore behaviour (spec 06 §1) — checkpoint semantics, the
delivery rule, soft purge + restore, and the B1/B3/B4/B5 regression gates.

Every test uses a tmp-dir DB. The live ``~/.local/state/para-organize``
database is never opened here (see test_store_migration.py, which works on a
COPY of a COPY).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from organize_core.consumers.store import (
    DEFAULT_RETENTION_DAYS,
    RETRYABLE_STATUSES,
    STORE_SCHEMA_VERSION,
    TERMINAL_STATUSES,
    AutomationStore,
    hash_note_text,
)
from organize_core.errors import StoreError

NOW = 1_700_000_000
DAY = 86400


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "automations.db"


@pytest.fixture()
def store(db_path: Path):
    with AutomationStore(db_path) as s:
        s.migrate()
        yield s


def note(tmp_path: Path, name: str = "a.md") -> Path:
    return tmp_path / "vault" / "capture" / "raw_capture" / name


# --- lifecycle ---------------------------------------------------------------


def test_constructor_is_pure_no_file_created(db_path: Path) -> None:
    """__init__ does no I/O — the file appears only on open()."""
    AutomationStore(db_path)
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_use_before_open_is_a_loud_error(db_path: Path) -> None:
    unopened = AutomationStore(db_path)
    with pytest.raises(StoreError) as excinfo:
        unopened.get_emission("taskwarrior", Path("/x/a.md"))
    assert "before open()" in str(excinfo.value)


def test_context_manager_opens_and_closes(db_path: Path) -> None:
    with AutomationStore(db_path) as s:
        s.migrate()
        assert db_path.exists()
    with pytest.raises(StoreError):
        s.get_emission("taskwarrior", Path("/x/a.md"))


def test_connection_settings_are_wal_and_busy_timeout(store: AutomationStore) -> None:
    conn = store._db
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_the_write_path_does_not_fsync_per_checkpoint(tmp_path: Path) -> None:
    """WAL + ``synchronous = NORMAL`` (1 == NORMAL in the pragma's encoding).

    Added by the integrator at Phase-3 close after the runner seat measured
    the default (FULL) at 1.40 s per 1 000 checkpoints — ~90 s pro-rata for
    the 7.5k-note vault against spec 09 §4's 30 s budget; NORMAL measured
    0.06 s, a 23x. Asserted as a PRAGMA rather than only as a wall-clock
    ceiling because a fast machine hides the regression: the end-to-end
    perf test still passed with FULL restored, so this is the assertion that
    actually fails when someone reverts the decision.

    The trade is stated in ``AutomationStore.open``: under WAL, NORMAL can
    lose only the most recent transactions on a power cut and can never
    corrupt the file, and a lost checkpoint re-offers a handful of notes —
    which every consumer must already survive (error/limit retry, 06 §1).
    ``migrate()`` raises it to FULL for its own transaction.
    """
    # Asserted on a bare open(), NOT on the migrated fixture: migrate() sets
    # the pragma itself on its way out, which would mask a reverted open().
    opened = AutomationStore(tmp_path / "bare.db")
    opened.open()
    try:
        assert conn_sync(opened) == 1, "expected PRAGMA synchronous = NORMAL on the run path"
    finally:
        opened.close()


def conn_sync(store: AutomationStore) -> int:
    return int(store._db.execute("PRAGMA synchronous").fetchone()[0])


def test_migration_restores_the_durable_setting_around_itself(tmp_path: Path) -> None:
    """A migration is a cutover artefact (09 §5.4), so it runs at FULL — and
    puts the run-path setting back afterwards, or every later checkpoint pays
    the fsync the decision above exists to avoid."""
    with AutomationStore(tmp_path / "a.db") as store:
        store.migrate()
        assert conn_sync(store) == 1


def test_fresh_db_is_created_at_current_schema_version(store: AutomationStore) -> None:
    assert store.schema_version() == STORE_SCHEMA_VERSION
    tables = store._table_names()
    assert {"notes", "emissions", "meta", "purged_notes", "purged_emissions"} <= tables
    assert "last_seen" in store._columns("notes")


def test_reopening_an_existing_store_is_a_no_op(db_path: Path, tmp_path: Path) -> None:
    path = note(tmp_path)
    with AutomationStore(db_path) as s:
        s.migrate()
        s.checkpoint("taskwarrior", path, "h1", "success", now=NOW)
    with AutomationStore(db_path) as s:
        assert s.get_emission("taskwarrior", path) is not None


# --- hashing (06 §1) ---------------------------------------------------------


def test_hash_note_text_matches_the_live_pipeline_digest() -> None:
    import hashlib

    text = "---\ntags: [todo]\n---\n\nbuy milk\n"
    assert hash_note_text(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(hash_note_text("")) == 64


def test_hash_note_text_survives_replacement_characters() -> None:
    # Text that came off disk with errors="replace" must still hash (B1 class).
    assert len(hash_note_text("caf�é \U0001f600")) == 64


# --- the delivery rule (06 §1) ----------------------------------------------


def test_unknown_note_needs_delivery(store: AutomationStore, tmp_path: Path) -> None:
    assert store.needs_delivery("taskwarrior", note(tmp_path), "h1") is True


def test_success_checkpoint_blocks_redelivery_of_the_same_hash(
    store: AutomationStore, tmp_path: Path
) -> None:
    path = note(tmp_path)
    store.checkpoint("taskwarrior", path, "h1", "success", now=NOW)
    assert store.needs_delivery("taskwarrior", path, "h1") is False


def test_edited_note_is_redelivered(store: AutomationStore, tmp_path: Path) -> None:
    path = note(tmp_path)
    store.checkpoint("taskwarrior", path, "h1", "success", now=NOW)
    assert store.needs_delivery("taskwarrior", path, "h2") is True


def test_skip_is_terminal_too(store: AutomationStore, tmp_path: Path) -> None:
    path = note(tmp_path)
    store.checkpoint("learn", path, "h1", "skip", now=NOW)
    assert store.needs_delivery("learn", path, "h1") is False


def test_checkpoints_are_per_consumer(store: AutomationStore, tmp_path: Path) -> None:
    path = note(tmp_path)
    store.checkpoint("taskwarrior", path, "h1", "success", now=NOW)
    assert store.needs_delivery("learn", path, "h1") is True


def test_checkpoint_refuses_non_terminal_statuses(
    store: AutomationStore, tmp_path: Path
) -> None:
    """B3: `limit` checkpointed = notes over the cap dropped forever. The
    store refuses so the defect cannot come back through a second door."""
    path = note(tmp_path)
    for status in sorted(RETRYABLE_STATUSES):
        with pytest.raises(StoreError) as excinfo:
            store.checkpoint("taskwarrior", path, "h1", status, now=NOW)
        assert status in str(excinfo.value)
    with pytest.raises(StoreError):
        store.checkpoint("taskwarrior", path, "h1", "filtered", now=NOW)
    assert store.get_emission("taskwarrior", path) is None
    assert store.needs_delivery("taskwarrior", path, "h1") is True


def test_terminal_statuses_are_exactly_success_and_skip() -> None:
    assert TERMINAL_STATUSES == {"success", "skip"}
    assert RETRYABLE_STATUSES == {"error", "limit"}


def test_a_pre_existing_error_row_never_blocks_delivery(
    store: AutomationStore, tmp_path: Path
) -> None:
    """Rows written by the OLD pipeline (which did checkpoint error/limit)
    must not suppress a retry after migration."""
    path = note(tmp_path)
    store._db.execute(
        "INSERT INTO emissions(consumer, note_path, note_hash, emitted_at, status) "
        "VALUES ('taskwarrior', ?, 'h1', ?, 'error')",
        (str(path), NOW),
    )
    assert store.needs_delivery("taskwarrior", path, "h1") is True


def test_unknown_status_row_does_not_block_delivery(
    store: AutomationStore, tmp_path: Path
) -> None:
    path = note(tmp_path)
    store._db.execute(
        "INSERT INTO emissions(consumer, note_path, note_hash, emitted_at, status) "
        "VALUES ('taskwarrior', ?, 'h1', ?, 'wat')",
        (str(path), NOW),
    )
    assert store.needs_delivery("taskwarrior", path, "h1") is True


# --- checkpoint / get_emission ----------------------------------------------


def test_checkpoint_roundtrips_metadata(store: AutomationStore, tmp_path: Path) -> None:
    path = note(tmp_path)
    store.checkpoint(
        "taskwarrior",
        path,
        "h1",
        "success",
        {"description": "buy milk", "tags": ["not_reviewed"], "project": None},
        now=NOW,
    )
    emission = store.get_emission("taskwarrior", path)
    assert emission is not None
    assert emission.consumer == "taskwarrior"
    assert emission.note_path == str(path)
    assert emission.note_hash == "h1"
    assert emission.emitted_at == NOW
    assert emission.status == "success"
    assert emission.metadata == {"description": "buy milk", "tags": ["not_reviewed"], "project": None}


def test_checkpoint_upserts_one_row_per_consumer_path(
    store: AutomationStore, tmp_path: Path
) -> None:
    path = note(tmp_path)
    store.checkpoint("taskwarrior", path, "h1", "success", now=NOW)
    store.checkpoint("taskwarrior", path, "h2", "skip", now=NOW + 10)
    rows = store._db.execute("SELECT count(*) FROM emissions").fetchone()[0]
    assert rows == 1
    emission = store.get_emission("taskwarrior", path)
    assert emission is not None
    assert (emission.note_hash, emission.status, emission.emitted_at) == ("h2", "skip", NOW + 10)


def test_get_emission_missing_returns_none(store: AutomationStore, tmp_path: Path) -> None:
    assert store.get_emission("taskwarrior", note(tmp_path)) is None


def test_metadata_with_non_json_types_does_not_raise(
    store: AutomationStore, tmp_path: Path
) -> None:
    path = note(tmp_path)
    store.checkpoint("learn", path, "h1", "success", {"when": Path("/tmp/x")}, now=NOW)
    emission = store.get_emission("learn", path)
    assert emission is not None
    assert emission.metadata["when"] == "/tmp/x"


def test_corrupt_metadata_json_reads_as_empty(store: AutomationStore, tmp_path: Path) -> None:
    path = note(tmp_path)
    store.checkpoint("learn", path, "h1", "success", now=NOW)
    store._db.execute("UPDATE emissions SET metadata_json = '{not json'")
    emission = store.get_emission("learn", path)
    assert emission is not None
    assert emission.metadata == {}


def test_invalid_utf8_in_the_db_does_not_crash_reads(
    store: AutomationStore, tmp_path: Path
) -> None:
    """B1 regression at the store layer: sqlite3's DEFAULT text factory is a
    STRICT utf-8 decode, so one bad byte in legacy metadata would raise
    UnicodeDecodeError on SELECT."""
    path = note(tmp_path)
    store.checkpoint("taskwarrior", path, "h1", "success", now=NOW)
    store._db.execute(
        "UPDATE emissions SET metadata_json = CAST(? AS TEXT)", (b'{"d": "caf\xe9"}',)
    )
    emission = store.get_emission("taskwarrior", path)
    assert emission is not None  # no UnicodeDecodeError
    assert store.needs_delivery("taskwarrior", path, "h1") is False


# --- path canonicalisation (06 §1: history orphans otherwise) ----------------


def test_relative_paths_are_refused(store: AutomationStore) -> None:
    with pytest.raises(StoreError) as excinfo:
        store.checkpoint("taskwarrior", Path("capture/raw_capture/a.md"), "h1", "success", now=NOW)
    assert "absolute" in str(excinfo.value)
    with pytest.raises(StoreError):
        store.needs_delivery("taskwarrior", Path("a.md"), "h1")


def test_paths_with_spaces_and_unicode_roundtrip(
    store: AutomationStore, tmp_path: Path
) -> None:
    path = tmp_path / "vault" / "capture" / "raw capture: ünïcode 🎉" / "a.md"
    store.checkpoint("taskwarrior", path, "h1", "success", now=NOW)
    assert store.needs_delivery("taskwarrior", path, "h1") is False
    emission = store.get_emission("taskwarrior", path)
    assert emission is not None and emission.note_path == str(path)


# --- notes table / last_seen -------------------------------------------------


def test_mark_seen_records_the_real_note_hash(store: AutomationStore, tmp_path: Path) -> None:
    """``mark_seen`` is the ONLY writer of the ``notes`` table on the run
    path. Without the hashes every v2 row carried ``note_hash = \'\'``
    forever and the column was permanently useless."""
    path = note(tmp_path)
    store.mark_seen([path], now=NOW, hashes={path: "h1"})
    row = store._db.execute("SELECT * FROM notes WHERE path = ?", (str(path),)).fetchone()
    assert row["note_hash"] == "h1"
    assert row["last_seen"] == NOW

    store.mark_seen([path], now=NOW + 5, hashes={path: "h2"})
    row = store._db.execute("SELECT * FROM notes WHERE path = ?", (str(path),)).fetchone()
    assert row["note_hash"] == "h2"  # a changed file updates it
    assert row["last_seen"] == NOW + 5


def test_mark_seen_without_a_hash_never_clobbers_a_recorded_one(
    store: AutomationStore, tmp_path: Path
) -> None:
    """The empty-string placeholder is conservative by construction: a caller
    that does not know the hash leaves the stored one alone."""
    path = note(tmp_path)
    store.mark_seen([path], now=NOW, hashes={path: "real"})
    store.mark_seen([path], now=NOW + 5)
    row = store._db.execute("SELECT * FROM notes WHERE path = ?", (str(path),)).fetchone()
    assert row["note_hash"] == "real"
    assert row["last_seen"] == NOW + 5


def test_mark_seen_updates_last_seen_and_tracks_new_paths(
    store: AutomationStore, tmp_path: Path
) -> None:
    known, fresh = note(tmp_path, "a.md"), note(tmp_path, "b.md")
    store.mark_seen([known], now=NOW, hashes={known: "h1"})
    store.mark_seen([known, fresh], now=NOW + DAY)
    rows = {
        row["path"]: row["last_seen"]
        for row in store._db.execute("SELECT path, last_seen FROM notes")
    }
    assert rows == {str(known): NOW + DAY, str(fresh): NOW + DAY}
    # the known note keeps its real hash; marking seen never clobbers it
    assert store._db.execute(
        "SELECT note_hash FROM notes WHERE path = ?", (str(known),)
    ).fetchone()[0] == "h1"


def test_mark_seen_empty_list_is_a_no_op(store: AutomationStore) -> None:
    store.mark_seen([], now=NOW)
    assert store._db.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


# --- soft purge (06 §1, 08 §B5) ---------------------------------------------


def _seed_for_purge(store: AutomationStore, tmp_path: Path) -> tuple[Path, Path]:
    stale, current = note(tmp_path, "gone.md"), note(tmp_path, "here.md")
    store.mark_seen([stale, current], now=NOW, hashes={stale: "h1", current: "h2"})
    store.checkpoint("taskwarrior", stale, "h1", "success", now=NOW)
    store.checkpoint("taskwarrior", current, "h2", "success", now=NOW)
    return stale, current


def test_purge_is_refused_when_a_scan_dir_was_missing(
    store: AutomationStore, tmp_path: Path
) -> None:
    """B5: a transient empty mount must never delete a year of checkpoints."""
    stale, _ = _seed_for_purge(store, tmp_path)
    store.mark_seen([], now=NOW + 400 * DAY)
    purged = store.soft_purge(scan_dirs_ok=False, now=NOW + 400 * DAY)
    assert purged == 0
    assert store.get_emission("taskwarrior", stale) is not None


def test_purge_keeps_rows_inside_the_retention_window(
    store: AutomationStore, tmp_path: Path
) -> None:
    stale, current = _seed_for_purge(store, tmp_path)
    store.mark_seen([current], now=NOW + 29 * DAY)
    assert store.soft_purge(scan_dirs_ok=True, now=NOW + 29 * DAY) == 0
    assert store.get_emission("taskwarrior", stale) is not None


def test_purge_retires_rows_past_the_window_and_they_are_restorable(
    store: AutomationStore, tmp_path: Path
) -> None:
    stale, current = _seed_for_purge(store, tmp_path)
    later = NOW + (DEFAULT_RETENTION_DAYS + 1) * DAY
    store.mark_seen([current], now=later)

    assert store.soft_purge(scan_dirs_ok=True, now=later) == 1
    assert store.get_emission("taskwarrior", stale) is None
    assert store.get_emission("taskwarrior", current) is not None
    assert store.list_purged() == [str(stale)]

    assert store.restore_purged() == 1
    assert store.list_purged() == []
    restored = store.get_emission("taskwarrior", stale)
    assert restored is not None
    assert (restored.note_hash, restored.status, restored.emitted_at) == ("h1", "success", NOW)
    assert store.needs_delivery("taskwarrior", stale, "h1") is False


def test_restore_never_clobbers_newer_live_state(
    store: AutomationStore, tmp_path: Path
) -> None:
    stale, current = _seed_for_purge(store, tmp_path)
    later = NOW + 90 * DAY
    store.mark_seen([current], now=later)
    store.soft_purge(scan_dirs_ok=True, now=later)

    # the note came back and was processed again before anyone restored
    store.mark_seen([stale], now=later, hashes={stale: "h9"})
    store.checkpoint("taskwarrior", stale, "h9", "success", now=later)
    store.restore_purged()

    emission = store.get_emission("taskwarrior", stale)
    assert emission is not None
    assert emission.note_hash == "h9"


def test_restore_can_target_specific_paths(store: AutomationStore, tmp_path: Path) -> None:
    a, b = note(tmp_path, "a.md"), note(tmp_path, "b.md")
    store.mark_seen([a, b], now=NOW, hashes={a: "h1", b: "h2"})
    later = NOW + 90 * DAY
    assert store.soft_purge(scan_dirs_ok=True, now=later) == 2
    assert store.restore_purged([a]) == 1
    assert store.list_purged() == [str(b)]


def test_negative_retention_is_refused(store: AutomationStore) -> None:
    with pytest.raises(StoreError):
        store.soft_purge(retention_days=-1, scan_dirs_ok=True, now=NOW)


def test_purge_scales_past_the_sqlite_variable_limit(
    store: AutomationStore, tmp_path: Path
) -> None:
    """The purge list is joined through a temp table, not an IN (?, ?, …)
    clause — 7.5k live notes would blow SQLITE_MAX_VARIABLE_NUMBER."""
    paths = [note(tmp_path, f"n{i}.md") for i in range(2500)]
    for i, path in enumerate(paths):
        store.mark_seen([path], now=NOW, hashes={path: f"h{i}"})
    assert store.soft_purge(scan_dirs_ok=True, now=NOW + 90 * DAY) == 2500
    assert store._db.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert store.restore_purged() == 2500


# --- concurrency-ish sanity --------------------------------------------------


def test_a_second_connection_can_read_committed_checkpoints(
    store: AutomationStore, db_path: Path, tmp_path: Path
) -> None:
    path = note(tmp_path)
    store.checkpoint("taskwarrior", path, "h1", "success", now=NOW)
    other = sqlite3.connect(str(db_path))
    try:
        count = other.execute(
            "SELECT count(*) FROM emissions WHERE note_path = ?", (str(path),)
        ).fetchone()[0]
    finally:
        other.close()
    assert count == 1


# --- the archive is reachable AND bounded (08 §B18 dead-code smell) ---------


def test_the_purge_archive_is_swept_after_its_own_retention_window(
    store: AutomationStore, tmp_path: Path
) -> None:
    """Nothing but ``restore_purged`` ever deleted from ``purged_*``, so the
    15 MB bloat 08 §B4 removed from ``emissions`` simply moved house."""
    stale, current = _seed_for_purge(store, tmp_path)
    purged_at = NOW + 40 * DAY
    store.mark_seen([current], now=purged_at)  # keep `current` alive
    assert store.soft_purge(scan_dirs_ok=True, now=purged_at) == 1
    assert store.list_purged() == [str(stale)]

    # Inside the archive window: still restorable.
    assert store.sweep_purged(now=purged_at + 30 * DAY) == 0
    assert store.list_purged() == [str(stale)]

    # Past it: really gone, emissions included.
    assert store.sweep_purged(now=purged_at + 200 * DAY) == 1
    assert store.list_purged() == []
    assert (
        store._db.execute("SELECT count(*) FROM purged_emissions").fetchone()[0] == 0  # noqa: SLF001
    )


def test_sweeping_the_archive_refuses_a_negative_window(store: AutomationStore) -> None:
    with pytest.raises(StoreError):
        store.sweep_purged(retention_days=-1, now=NOW)
