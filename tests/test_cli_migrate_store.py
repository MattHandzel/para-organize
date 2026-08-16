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
