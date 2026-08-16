"""``run_consumers`` against the REAL SQLite store, plus the performance
gate for a full run (spec 09 §4: 7.5k notes, no LLM work, < 30 s).

``test_runner.py`` proves the orchestration law against a fake store that
asserts the contract. This module proves the runner and the real
``AutomationStore`` agree about it — the seam where a checkpoint that is
"written" in memory but not committed to disk would silently refire every
consumer on every past capture.

If the store seat has not landed its implementation yet, these tests skip
rather than fail; the semantics are still fully covered by
``test_runner.py``.
"""

from __future__ import annotations

import hashlib
import inspect
import time
from pathlib import Path

import pytest

from organize_core.config import Config, ConsumerConfig, VaultConfig
from organize_core.consumers import base as consumer_base
from organize_core.consumers.base import ConsumerResult, NotePayload, Status
from organize_core.consumers.runner import run_consumers
from organize_core.consumers.store import AutomationStore
from test_runner import FakeStore, cc, make_config, make_consumer

IDEAS_NOTE = "projects/blog/ideas.md"

NOTE_COUNT = 1000
#: 09 §4 budgets a full 7.5k-note run at 30 s with no LLM work. The
#: runner's own share of that is gated in ``test_runner.py`` against the
#: zero-I/O fake store; THIS is the end-to-end ceiling, and it is now a real
#: gate rather than a catastrophe detector.
#:
#: History worth keeping: this was 90 s because the store fsynced once per
#: ``checkpoint`` (measured 1.40 s per 1 000 checkpoints, i.e. ~12 s for this
#: test and ~90 s pro-rata for the 7.5k vault — over budget). The integrator
#: set ``PRAGMA synchronous = NORMAL`` under WAL (0.06 s per 1 000, 23×), so
#: the ceiling comes down to something that would actually catch a
#: regression. 2 000 emissions here scale to 7.5k × 2 consumers well inside
#: the 30 s spec budget.
FULL_RUN_CEILING_SECONDS = 10.0

_TEMPLATE = """---
timestamp: '2026-06-10T21:37:42.809743+00:00'
id: 'gen-{n:04d}'
tags:
- generated
- topic-{topic}
sources:
- me
processing_status: raw
---
# Generated note {n}

Body text for note {n}, long enough to be realistic prose for a consumer
to chew on without being a synthetic one-liner.
"""


@pytest.fixture()
def registry():  # noqa: ANN201 - same snapshot/restore as test_runner.py
    saved = dict(consumer_base._REGISTRY)
    yield consumer_base._REGISTRY
    consumer_base._REGISTRY.clear()
    consumer_base._REGISTRY.update(saved)


@pytest.fixture()
def store(tmp_path: Path):
    db = tmp_path / "state" / "automations.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    try:
        with AutomationStore(db) as opened:
            opened.migrate()
            yield opened
    except NotImplementedError:  # pragma: no cover - store seat still in flight
        pytest.skip("AutomationStore is not implemented yet")


def by_name(summary, name):  # noqa: ANN001, ANN201
    return next(c for c in summary.consumers if c.name == name)


# ---------------------------------------------------------------------------
# The fake and the real store must speak the same API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method", ["needs_delivery", "checkpoint", "get_emission", "mark_seen", "soft_purge"]
)
def test_fake_store_matches_the_real_store_signature(method: str) -> None:
    """Guards against the fake drifting from ``AutomationStore`` — a fake
    that has diverged proves nothing."""
    real = inspect.signature(getattr(AutomationStore, method))
    fake = inspect.signature(getattr(FakeStore, method))

    def shape(sig: inspect.Signature) -> list[tuple[str, str]]:
        return [(p.name, str(p.kind)) for p in sig.parameters.values()]

    assert shape(fake) == shape(real)


# ---------------------------------------------------------------------------
# Round trips through real SQLite
# ---------------------------------------------------------------------------


def test_success_checkpoints_survive_into_sqlite(
    fixture_vault: Path, store: AutomationStore, registry  # noqa: ANN001
) -> None:
    make_consumer("sql_ok_fake")
    config = make_config(fixture_vault, cc("sqlok", "sql_ok_fake", include_paths=[IDEAS_NOTE]))

    run_consumers(config, store)

    emission = store.get_emission("sqlok", (fixture_vault / IDEAS_NOTE).resolve())
    assert emission is not None
    assert emission.status == "success"


def test_rerun_against_sqlite_processes_nothing_new(
    fixture_vault: Path, store: AutomationStore, registry  # noqa: ANN001
) -> None:
    rec = make_consumer("sql_idem_fake")
    config = make_config(fixture_vault, cc("idem", "sql_idem_fake"))

    first = run_consumers(config, store)
    second = run_consumers(config, store)

    assert first.consumers[0].success > 0
    assert second.consumers[0].success == 0
    assert len(rec.handled) == first.consumers[0].success


def test_edited_note_is_redelivered_by_sqlite(
    fixture_vault: Path, store: AutomationStore, registry  # noqa: ANN001
) -> None:
    rec = make_consumer("sql_edit_fake")
    config = make_config(fixture_vault, cc("edit", "sql_edit_fake", include_paths=[IDEAS_NOTE]))

    run_consumers(config, store)
    target = fixture_vault / IDEAS_NOTE
    target.write_text(target.read_text(encoding="utf-8") + "\n- another\n", encoding="utf-8")
    summary = run_consumers(config, store)

    assert by_name(summary, "edit").success == 1
    assert len(rec.handled) == 2


@pytest.mark.parametrize("status", [Status.ERROR, Status.LIMIT])
def test_non_terminal_results_write_no_sqlite_row(
    fixture_vault: Path, store: AutomationStore, registry, status: Status  # noqa: ANN001
) -> None:
    """08 §B3, at the real seam: nothing reaches the DB, so it retries."""
    make_consumer("sql_nt_fake", result=lambda p: ConsumerResult(status=status))
    config = make_config(fixture_vault, cc("nt", "sql_nt_fake", include_paths=[IDEAS_NOTE]))

    run_consumers(config, store)

    assert store.get_emission("nt", (fixture_vault / IDEAS_NOTE).resolve()) is None


def test_dry_run_leaves_the_database_file_byte_identical(
    fixture_vault: Path, tmp_path: Path, registry  # noqa: ANN001
) -> None:
    """09 §5.6: a rehearsal must be indistinguishable from not running."""
    rec = make_consumer("sql_dry_fake")
    config = make_config(fixture_vault, cc("dry", "sql_dry_fake"))
    db = tmp_path / "state" / "automations.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    try:
        with AutomationStore(db) as opened:
            opened.migrate()
            run_consumers(config, opened)
    except NotImplementedError:  # pragma: no cover
        pytest.skip("AutomationStore is not implemented yet")

    before = hashlib.sha256(db.read_bytes()).hexdigest()
    handled_before = len(rec.handled)

    with AutomationStore(db) as opened:
        summary = run_consumers(config, opened, dry_run=True)

    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert len(rec.handled) == handled_before  # zero consumer side effects
    assert summary.dry_run is True
    assert not list(db.parent.glob("*.db-wal"))  # nothing left uncommitted


def test_paths_are_stored_resolved(
    fixture_vault: Path, store: AutomationStore, registry  # noqa: ANN001
) -> None:
    """06 §1: the live DB holds ``.resolve()``d absolute paths — the rewrite
    must canonicalize identically or a year of history orphans."""
    make_consumer("sql_path_fake")
    config = make_config(fixture_vault, cc("paths", "sql_path_fake", include_paths=[IDEAS_NOTE]))

    run_consumers(config, store)

    emission = store.get_emission("paths", (fixture_vault / IDEAS_NOTE).resolve())
    assert emission is not None
    assert Path(emission.note_path).is_absolute()
    assert Path(emission.note_path) == Path(emission.note_path).resolve()


def test_soft_purge_is_reached_with_the_scan_dir_guard(
    fixture_vault: Path, store: AutomationStore, registry  # noqa: ANN001
) -> None:
    make_consumer("sql_purge_fake")
    config = make_config(fixture_vault, cc("purge", "sql_purge_fake", include_paths=[IDEAS_NOTE]))

    summary = run_consumers(config, store)

    # a healthy vault purges nothing on its first run, and must not error
    assert summary.purged == 0
    assert summary.exit_code == 0


# ---------------------------------------------------------------------------
# Performance gate (spec 09 §4)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def generated_vault(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("runner-perf-vault")
    raw = root / "capture" / "raw_capture"
    raw.mkdir(parents=True, exist_ok=True)
    for folder in ("projects/blog", "areas/health", "resources/performing"):
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / "index.md").write_text("---\ntags:\n- index\n---\nx\n", encoding="utf-8")
    for n in range(NOTE_COUNT):
        (raw / f"gen-{n:04d}.md").write_text(
            _TEMPLATE.format(n=n, topic=n % 25), encoding="utf-8"
        )
    return root


def test_full_run_over_a_thousand_notes_stays_correct_at_scale(
    generated_vault: Path, tmp_path: Path, registry  # noqa: ANN001
) -> None:
    def predicate(payload: NotePayload) -> bool:
        return "generated" in payload.frontmatter.get("tags", [])

    make_consumer("perf_a_fake", predicate=predicate)
    make_consumer("perf_b_fake", predicate=predicate)
    config = Config(
        vault=VaultConfig(root=generated_vault),
        consumers=[
            ConsumerConfig(name="a", type="perf_a_fake", max_notes_per_run=NOTE_COUNT),
            ConsumerConfig(name="b", type="perf_b_fake", max_notes_per_run=NOTE_COUNT),
        ],
    )
    db = tmp_path / "perf.db"

    try:
        with AutomationStore(db) as store:
            store.migrate()
            started = time.monotonic()
            summary = run_consumers(config, store)
            elapsed = time.monotonic() - started
    except NotImplementedError:  # pragma: no cover
        pytest.skip("AutomationStore is not implemented yet")

    assert summary.notes_scanned == NOTE_COUNT + 3
    assert by_name(summary, "a").success == NOTE_COUNT
    assert by_name(summary, "b").success == NOTE_COUNT
    assert by_name(summary, "a").filtered == 3  # the index.md notes
    assert elapsed < FULL_RUN_CEILING_SECONDS, (
        f"full run over {NOTE_COUNT} notes × 2 consumers took {elapsed:.2f}s "
        f"(ceiling {FULL_RUN_CEILING_SECONDS}s; spec 09 §4 wants 7.5k in 30 s, so "
        "profile AutomationStore.checkpoint before the runner)"
    )


# ---------------------------------------------------------------------------
# 06 §7 acceptance: "Removing a scan dir from config does not delete its
# emission history" (08 §B5) — against the REAL store
# ---------------------------------------------------------------------------

_DAY = 86_400


def _two_dir_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for folder in ("capture/raw_capture", "resources"):
        (vault / folder).mkdir(parents=True)
    (vault / "capture/raw_capture/a.md").write_text("---\nid: a\n---\nalpha\n", encoding="utf-8")
    # A second capture keeps raw_capture non-empty after `a.md` is deleted,
    # so the "scan dir empty ⇒ suppress the purge" guard does not mask the
    # behaviour under test.
    (vault / "capture/raw_capture/keep.md").write_text(
        "---\nid: keep\n---\nkeep\n", encoding="utf-8"
    )
    (vault / "resources/b.md").write_text("---\nid: b\n---\nbravo\n", encoding="utf-8")
    return vault


def test_narrowing_scan_dirs_does_not_delete_the_removed_dirs_emission_history(
    tmp_path: Path, registry: object, monkeypatch: object
) -> None:
    """The 06 §7 acceptance bullet, executed.

    Run with ``scan_dirs = [A, B]`` so both notes checkpoint. Then narrow to
    ``[A]`` and run again 40 days later. Every REMAINING dir is healthy, so
    ``_scan_dirs_ok()`` is True and the purge is not suppressed — which used
    to mean B's rows left ``emissions``, ``needs_delivery`` flipped back to
    True, and re-adding B re-ran every LLM emission for those notes. That is
    the duplicate-output harm B5 exists to prevent.
    """
    vault = _two_dir_vault(tmp_path)
    make_consumer("b5_narrow_fake")
    db = tmp_path / "automations.db"

    wide = make_config(
        vault, cc("b5", "b5_narrow_fake"), scan_dirs=["capture/raw_capture", "resources"]
    )
    with AutomationStore(db) as store:
        store.migrate()
        summary = run_consumers(wide, store)
        assert by_name(summary, "b5").success == 3
        assert store.needs_delivery("b5", (vault / "resources/b.md").resolve(), "different") is True
        kept = store.get_emission("b5", (vault / "resources/b.md").resolve())
        assert kept is not None and kept.status == "success"
        b_hash = kept.note_hash

    # …40 days later, with `resources` no longer scanned.
    narrow = make_config(vault, cc("b5", "b5_narrow_fake"), scan_dirs=["capture/raw_capture"])
    later = int(time.time()) + 40 * _DAY
    monkeypatch.setattr(time, "time", lambda: later)  # type: ignore[attr-defined]
    with AutomationStore(db) as store:
        run_consumers(narrow, store)
        emission = store.get_emission("b5", (vault / "resources/b.md").resolve())
        assert emission is not None, "the removed scan dir's emission history was purged"
        assert emission.status == "success"
        assert emission.note_hash == b_hash
        # …and therefore re-adding the dir does NOT re-run the LLM work.
        assert (
            store.needs_delivery("b5", (vault / "resources/b.md").resolve(), b_hash) is False
        )


def test_a_note_deleted_from_a_still_configured_scan_dir_is_purged(
    tmp_path: Path, registry: object, monkeypatch: object
) -> None:
    """The purge must still DO its job for the case it exists for — a note
    that really is gone from a directory we really are still walking. The
    B5 bound is 'outside the configured scan dirs', not 'never'."""
    vault = _two_dir_vault(tmp_path)
    make_consumer("b5_gone_fake")
    db = tmp_path / "automations.db"
    config = make_config(
        vault, cc("b5gone", "b5_gone_fake"), scan_dirs=["capture/raw_capture", "resources"]
    )
    gone = (vault / "capture/raw_capture/a.md").resolve()

    with AutomationStore(db) as store:
        store.migrate()
        run_consumers(config, store)
        assert store.get_emission("b5gone", gone) is not None

    gone.unlink()
    later = int(time.time()) + 40 * _DAY
    monkeypatch.setattr(time, "time", lambda: later)  # type: ignore[attr-defined]
    with AutomationStore(db) as store:
        summary = run_consumers(config, store)
        assert summary.purged == 1
        assert store.get_emission("b5gone", gone) is None
        assert str(gone) in store.list_purged()  # archived, not destroyed
