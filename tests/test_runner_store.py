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
