"""``organize migrate-store`` — the cutover command (spec 09 §5.4, 06 §1).

Two halves:

1. Behavior against tmp databases: report on stdout, ``--backup-first``,
   idempotency, the live-state-dir refusal, and the JSON door.
2. A REAL end-to-end migration of a writable copy of the read-only mirror of
   Matt's live 15 MB ``automations.db``, driven through ``cli.main`` exactly
   as the cutover will drive it. The mirror itself is never opened for
   writing; the copy lives in ``tmp_path``.

The live-state refusal is the belt on step 4 of the cutover checklist: this
is the one command in the repo that is *meant* to be aimed at production
data, so it must be aimed deliberately.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from organize_core import cli
from organize_core.consumers.store import STORE_SCHEMA_VERSION, AutomationStore

REPO_ROOT = Path(__file__).resolve().parents[1]
MIRROR_DIR = REPO_ROOT.parent / ".mirror-work" / "live-db-copy"
LIVE_DB_COPY = MIRROR_DIR / "automations.db"

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
"""


def make_v1_db(path: Path) -> Path:
    """A miniature of the live schema: successes to preserve, filtered rows
    to drop, an error row to keep as retryable."""
    conn = sqlite3.connect(str(path))
    conn.executescript(V1_SCHEMA)
    conn.executemany(
        "INSERT INTO notes(path, note_hash, metadata_json, seen_at) VALUES (?, ?, ?, ?)",
        [(f"/vault/n{i}.md", f"h{i}", None, 1_700_000_000) for i in range(4)],
    )
    conn.executemany(
        "INSERT INTO emissions(consumer, note_path, note_hash, emitted_at, status, metadata_json)"
        " VALUES (?, ?, ?, ?, ?, NULL)",
        [
            ("taskwarrior", "/vault/n0.md", "h0", 1_700_000_000, "success"),
            ("taskwarrior", "/vault/n1.md", "h1", 1_700_000_000, "skip"),
            ("learn", "/vault/n2.md", "h2", 1_700_000_000, "filtered"),
            ("learn", "/vault/n3.md", "h3", 1_700_000_000, "error"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def run(*argv: str) -> int:
    return cli.main(list(argv))


# ---------------------------------------------------------------------------
# behavior against tmp databases
# ---------------------------------------------------------------------------


def test_migrating_a_v1_database_reports_what_it_did(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = make_v1_db(tmp_path / "automations.db")

    assert run("migrate-store", "--db", str(db)) == 0
    out = capsys.readouterr().out

    assert "schema before: v1" in out
    assert "v1->v2" in out
    assert "success=1" in out and "skip=1" in out
    assert "filtered_dropped=1" in out

    with AutomationStore(db) as store:
        assert store.schema_version() == STORE_SCHEMA_VERSION
        rows = {
            (r["consumer"], r["note_path"]): r["status"]
            for r in store._db.execute("SELECT consumer, note_path, status FROM emissions")  # noqa: SLF001
        }
    assert rows == {
        ("taskwarrior", "/vault/n0.md"): "success",
        ("taskwarrior", "/vault/n1.md"): "skip",
        ("learn", "/vault/n3.md"): "error",  # kept, non-terminal ⇒ retried
    }


def test_a_second_migration_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = make_v1_db(tmp_path / "automations.db")
    assert run("migrate-store", "--db", str(db)) == 0
    capsys.readouterr()
    first = db.read_bytes()

    assert run("migrate-store", "--db", str(db)) == 0
    out = capsys.readouterr().out
    assert "schema before: v2" in out
    assert "no changes" in out.lower() or "no_op" in out.lower() or "v2->v2" in out
    assert db.read_bytes() == first, "an idempotent migration must not rewrite the file"


def test_backup_first_copies_the_database_before_touching_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = make_v1_db(tmp_path / "automations.db")
    original = db.read_bytes()

    assert run("migrate-store", "--db", str(db), "--backup-first") == 0
    err = capsys.readouterr().err

    backups = sorted(tmp_path.glob("automations.db.backup-*"))
    assert len(backups) == 1, backups
    assert backups[0].read_bytes() == original, "the backup is the PRE-migration file"
    assert str(backups[0]) in err
    assert db.read_bytes() != original, "...and the real file was migrated"


def test_without_backup_first_the_command_says_so_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """09 §5.4 says back it up first. Not backing up is allowed (a fresh dev
    DB), but it may never be silent."""
    db = make_v1_db(tmp_path / "automations.db")
    assert run("migrate-store", "--db", str(db)) == 0
    assert "--backup-first" in capsys.readouterr().err


def test_a_missing_database_is_created_empty_at_the_current_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "state" / "automations.db"
    assert run("migrate-store", "--db", str(db)) == 0
    captured = capsys.readouterr()
    assert "does not exist yet" in captured.err
    assert "created" in captured.out
    with AutomationStore(db) as store:
        assert store.schema_version() == STORE_SCHEMA_VERSION


def test_the_default_target_is_the_state_dirs_automations_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert run("--state-dir", str(state), "migrate-store") == 0
    assert str(state / "automations.db") in capsys.readouterr().out
    assert (state / "automations.db").exists()


def test_json_output_is_the_cutover_log_artefact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = make_v1_db(tmp_path / "automations.db")
    assert run("migrate-store", "--db", str(db), "--backup-first", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["from_version"] == 1
    assert payload["to_version"] == STORE_SCHEMA_VERSION
    assert payload["changed"] is True
    assert payload["kept_success"] == 1
    assert payload["kept_skip"] == 1
    assert payload["deleted_filtered"] == 1
    assert payload["reset_retryable"] == 1
    assert payload["notes_kept"] == 4
    assert payload["anomalies"] == []
    assert len(payload["backups"]) == 1


# ---------------------------------------------------------------------------
# the live-state-dir belt
# ---------------------------------------------------------------------------


def test_a_database_inside_a_live_state_dir_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cutover belt. ``$HOME`` is redirected into tmp so the check is
    exercised for real without going anywhere near Matt's actual state dir."""
    fake_home = tmp_path / "home"
    live = fake_home / ".local" / "state" / "para-organize"
    live.mkdir(parents=True)
    db = make_v1_db(live / "automations.db")
    original = db.read_bytes()
    monkeypatch.setattr(cli, "default_env", lambda: {"HOME": str(fake_home)})

    assert run("migrate-store", "--db", str(db)) == 1
    err = capsys.readouterr().err
    assert "refusing to migrate" in err
    assert "--yes-live" in err
    assert db.read_bytes() == original, "the refusal must not have touched the file"


def test_yes_live_permits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_home = tmp_path / "home"
    live = fake_home / ".local" / "share" / "organize-core"
    live.mkdir(parents=True)
    db = make_v1_db(live / "automations.db")
    monkeypatch.setattr(cli, "default_env", lambda: {"HOME": str(fake_home)})

    assert run("migrate-store", "--db", str(db), "--yes-live", "--backup-first") == 0
    assert "v1->v2" in capsys.readouterr().out


@pytest.mark.parametrize(
    "relative",
    [
        ".local/state/para-organize/automations.db",
        ".config/para-organize/automations.db",
        ".local/share/organize-core/automations.db",
        ".local/share/organize-core/nested/automations.db",
    ],
)
def test_every_known_live_location_is_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    fake_home = tmp_path / "home"
    db = fake_home / relative
    db.parent.mkdir(parents=True, exist_ok=True)
    make_v1_db(db)
    monkeypatch.setattr(cli, "default_env", lambda: {"HOME": str(fake_home)})
    assert run("migrate-store", "--db", str(db)) == 1


def test_a_tmp_path_is_never_treated_as_live(tmp_path: Path) -> None:
    """The guard must not be so eager that the test suite cannot migrate."""
    db = make_v1_db(tmp_path / "automations.db")
    assert run("migrate-store", "--db", str(db)) == 0


# ---------------------------------------------------------------------------
# the real thing: the live database mirror, through the CLI
# ---------------------------------------------------------------------------


def _census(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db))
    conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
    try:
        counts = {
            str(status): int(n)
            for status, n in conn.execute("SELECT status, COUNT(*) FROM emissions GROUP BY status")
        }
        counts["_notes"] = int(conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])
        return counts
    finally:
        conn.close()


def _success_rows(db: Path) -> set[tuple[str, str, str, int]]:
    conn = sqlite3.connect(str(db))
    conn.text_factory = lambda raw: raw.decode("utf-8", errors="replace")
    try:
        return {
            (str(c), str(p), str(h), int(t))
            for c, p, h, t in conn.execute(
                "SELECT consumer, note_path, note_hash, emitted_at FROM emissions "
                "WHERE status = 'success'"
            )
        }
    finally:
        conn.close()


def test_migrating_the_real_live_database_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cutover step 09 §5.4, rehearsed on the real 15 MB database.

    Every ``success`` checkpoint must survive ROW FOR ROW — losing one means
    a consumer refires over a note it already handled, which for ``learn``
    means a duplicate review file and for ``taskwarrior`` a duplicate task.
    """
    if not LIVE_DB_COPY.exists():  # pragma: no cover - machine dependent
        pytest.skip(f"live DB mirror not present at {LIVE_DB_COPY}")

    db = tmp_path / "automations.db"
    shutil.copy2(LIVE_DB_COPY, db)
    db.chmod(0o644)  # the mirror is chmod a-w on purpose

    before = _census(db)
    before_success = _success_rows(db)
    size_before = db.stat().st_size

    assert run("migrate-store", "--db", str(db), "--backup-first", "--json") == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["from_version"] == 1
    assert payload["to_version"] == STORE_SCHEMA_VERSION
    assert payload["anomalies"] == []
    assert payload["kept_success"] == before.get("success", 0)
    assert payload["kept_skip"] == before.get("skip", 0)
    assert payload["deleted_filtered"] == before.get("filtered", 0)
    assert payload["notes_kept"] == before["_notes"]
    # the counts reconcile exactly with the pre-migration SQL
    assert (
        payload["kept_success"]
        + payload["kept_skip"]
        + payload["deleted_filtered"]
        + payload["reset_retryable"]
        == sum(v for k, v in before.items() if k != "_notes")
    )

    # every success row is byte-identical, not merely equal in count
    assert _success_rows(db) == before_success

    after = _census(db)
    assert "filtered" not in after
    assert after["_notes"] == before["_notes"]
    assert db.stat().st_size < size_before, "the VACUUM should have shrunk the file"

    # the backup is the untouched original
    backups = sorted(tmp_path.glob("automations.db.backup-*"))
    assert len(backups) == 1
    assert _census(backups[0]) == before

    # and the store the pipeline will actually use opens at v2 and answers
    with AutomationStore(db) as store:
        assert store.schema_version() == STORE_SCHEMA_VERSION
        consumer, path, note_hash, _ = next(iter(before_success))
        assert store.needs_delivery(consumer, Path(path), note_hash) is False
        assert store.needs_delivery(consumer, Path(path), "0" * 64) is True


def test_the_live_db_mirror_is_still_read_only() -> None:
    """Nothing in this file may write the mirror — it is the only copy of the
    pre-migration production state we have."""
    if not LIVE_DB_COPY.exists():  # pragma: no cover - machine dependent
        pytest.skip("live DB mirror not present")
    assert LIVE_DB_COPY.stat().st_mode & 0o222 == 0, "the mirror must stay chmod a-w"


# ---------------------------------------------------------------------------
# `run-consumers` and the store migration (09 §5.4 / §5.6)
# ---------------------------------------------------------------------------


def _minimal_config(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "capture" / "raw_capture").mkdir(parents=True)
    (vault / "capture" / "raw_capture" / "n.md").write_text(
        "---\nid: n\n---\nbody\n", encoding="utf-8"
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f'[vault]\nroot = "{vault}"\nscan_dirs = ["capture/raw_capture"]\n'
        '\n[logging]\nlevel = "WARNING"\n',
        encoding="utf-8",
    )
    return config


def _migration_census(path: Path) -> dict[str, object]:
    """Everything a migration would change, read straight out of SQLite.

    Deliberately NOT a file digest: merely OPENING the store sets
    ``PRAGMA journal_mode = WAL``, which rewrites one header byte. That is
    idempotent and lossless; dropping 22 497 rows and VACUUMing is not, and
    it is the latter this asserts against.
    """
    with sqlite3.connect(str(path)) as conn:
        statuses = dict(conn.execute("SELECT status, COUNT(*) FROM emissions GROUP BY status"))
        notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        meta = "meta" in {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    return {"statuses": statuses, "notes": notes, "has_meta_table": meta}


def test_dry_run_refuses_to_migrate_the_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` documents itself as "evaluate everything, write nothing
    (spec 09 §5.6)" — and used to irreversibly migrate the database anyway,
    dropping 22 497 rows and VACUUMing 15.3 MB -> 5.3 MB on real data, with
    no backup, no confirmation, and while printing ``"dry_run": true``. It
    also walked straight past the ``--yes-live`` belt that ``migrate-store``
    enforces on the very same file.
    """
    state = tmp_path / "state"
    state.mkdir()
    db = make_v1_db(state / "automations.db")
    before = _migration_census(db)
    assert before["statuses"]["filtered"] == 1  # the rows a migration destroys
    config = _minimal_config(tmp_path)

    code = run("--config", str(config), "--state-dir", str(state), "--dry-run", "run-consumers")

    assert code == 1  # an OrganizeError, not a silent migration
    err = capsys.readouterr().err
    assert "refusing to migrate" in err
    assert "migrate-store --backup-first" in err
    assert _migration_census(db) == before, "a dry run migrated the database"


def test_dry_run_is_fine_on_a_fresh_or_already_current_store(tmp_path: Path) -> None:
    """The refusal is scoped to a REAL migration. A v0 (absent) database has
    nothing to lose and the run needs a schema to read through; a v2 database
    is a read-only census. Both must still rehearse."""
    state = tmp_path / "state"
    state.mkdir()
    config = _minimal_config(tmp_path)
    argv = ["--config", str(config), "--state-dir", str(state), "--dry-run", "run-consumers"]

    assert run(*argv) == 0  # v0: nothing there yet
    assert run(*argv) == 0  # v2: created by the first call, now a no-op


def test_a_real_run_backs_the_store_up_before_migrating(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """09 §5.4 says back the database up before migrating. Leaving that to
    the operator means the systemd unit — which is what actually reaches the
    database first at cutover — skips it entirely."""
    state = tmp_path / "state"
    state.mkdir()
    db = make_v1_db(state / "automations.db")
    before = _migration_census(db)
    config = _minimal_config(tmp_path)

    assert run("--config", str(config), "--state-dir", str(state), "run-consumers") == 0

    captured = capsys.readouterr()
    assert "backed up before migrating" in captured.out + captured.err
    backups = sorted(state.glob("automations.db.backup-*"))
    assert len(backups) == 1
    # The backup is the PRE-migration database: the dropped rows are in it.
    assert _migration_census(backups[0]) == before
    assert _migration_census(db) != before  # …and the live one really did migrate

    with AutomationStore(db) as store:
        assert store.schema_version() == STORE_SCHEMA_VERSION


def test_a_real_run_on_a_current_store_takes_no_backup(tmp_path: Path) -> None:
    """Only an actual v1->v2 step is worth a copy; every subsequent 10-minute
    run must not litter the state dir with snapshots of an unchanged file."""
    state = tmp_path / "state"
    state.mkdir()
    make_v1_db(state / "automations.db")
    config = _minimal_config(tmp_path)
    argv = ["--config", str(config), "--state-dir", str(state), "run-consumers"]

    assert run(*argv) == 0
    assert len(sorted(state.glob("automations.db.backup-*"))) == 1
    assert run(*argv) == 0
    assert len(sorted(state.glob("automations.db.backup-*"))) == 1
