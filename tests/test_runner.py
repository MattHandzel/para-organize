"""Orchestration: ``runner.run_consumers`` (spec 06 §1/§4/§6/§7).

Every test here is a regression obligation from ``spec/08-known-issues.md``
§B — the section that exists because this pipeline was silently dead for
three months:

* **B2** consumer constructors are PURE and a constructor that raises is
  isolated; the other consumers still run.
* **B3** ``limit`` (and ``error``) are NOT checkpointed — checkpointing
  ``limit`` silently dropped every over-cap note forever.
* **B4** filter misses are NOT persisted, so widening ``include_paths``
  applies retroactively (and the DB does not grow 30k junk rows).
* **B5** the soft purge is suppressed when a scan dir is missing/empty.
* **B12** checkpointing has ONE owner (this module); a consumer raising
  mid-run never stops its siblings.
* **B13** skips do not consume the ``max_notes_per_run`` budget.

The store is faked in-process: ``FakeStore`` implements the documented
``AutomationStore`` semantics and ASSERTS the single-owner contract (it
refuses a non-terminal checkpoint), so a runner regression fails here
rather than in production. ``test_runner_store.py`` runs the same
orchestration against the real SQLite store when it is available.
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from organize_core.config import Config, ConsumerConfig, LLMConfig, VaultConfig
from organize_core.consumers import base as consumer_base
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    Status,
    register,
)
from organize_core.consumers.runner import run_consumers
from organize_core.errors import ConfigError

NO_AI_NOTE = "capture/raw_capture/private-thought.md"
IDEAS_NOTE = "projects/blog/ideas.md"


# ---------------------------------------------------------------------------
# Fake store — the documented AutomationStore semantics, in memory
# ---------------------------------------------------------------------------


class FakeStore:
    """In-memory stand-in for ``AutomationStore``.

    ``checkpoint`` asserts a terminal status: the runner is the single
    owner of checkpointing (06 §1) and must never hand the store an
    ``error``/``limit`` row (08 §B3).
    """

    def __init__(self) -> None:
        self.emissions: dict[tuple[str, str], dict[str, Any]] = {}
        self.last_seen: dict[str, int] = {}
        self.purge_calls: list[dict[str, Any]] = []
        self.writes: list[tuple[Any, ...]] = []
        self.fail_needs_delivery_for: set[str] = set()
        self.fail_checkpoint_for: set[str] = set()

    # --- emission / checkpoint API ---------------------------------------

    def needs_delivery(self, consumer: str, path: Path, note_hash: str) -> bool:
        if str(path) in self.fail_needs_delivery_for:
            raise RuntimeError("simulated store read failure")
        row = self.emissions.get((consumer, str(path)))
        return row is None or row["note_hash"] != note_hash

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
        assert status in {"success", "skip"}, (
            f"non-terminal status {status!r} was checkpointed — 08 §B3 regression"
        )
        if str(path) in self.fail_checkpoint_for:
            raise RuntimeError("simulated store write failure")
        self.emissions[(consumer, str(path))] = {
            "note_hash": note_hash,
            "status": status,
            "metadata": dict(metadata or {}),
            "emitted_at": now,
        }
        self.writes.append(("checkpoint", consumer, str(path), status))

    def get_emission(self, consumer: str, path: Path) -> dict[str, Any] | None:
        return self.emissions.get((consumer, str(path)))

    # --- notes table / soft purge ----------------------------------------

    def mark_seen(self, paths: list[Path], *, now: int) -> None:
        for path in paths:
            self.last_seen[str(path)] = now
        self.writes.append(("mark_seen", len(paths)))

    def soft_purge(self, *, retention_days: int = 30, scan_dirs_ok: bool, now: int) -> int:
        self.purge_calls.append(
            {"retention_days": retention_days, "scan_dirs_ok": scan_dirs_ok, "now": now}
        )
        self.writes.append(("soft_purge", scan_dirs_ok))
        return 0

    # --- test helpers -----------------------------------------------------

    def snapshot(self) -> Any:
        """Everything a real store would have persisted — used for the
        dry-run "byte-identical" assertion."""
        return copy.deepcopy((self.emissions, self.last_seen))

    def statuses(self, consumer: str) -> dict[str, str]:
        return {
            Path(path).name: row["status"]
            for (name, path), row in self.emissions.items()
            if name == consumer
        }


# ---------------------------------------------------------------------------
# Fake consumers, registered through the real registry
# ---------------------------------------------------------------------------


@dataclass
class Recorder:
    """What a fake consumer saw. Constructor purity is observable: the
    runner must build the class exactly once per run (08 §B2)."""

    constructed: int = 0
    handled: list[Path] = field(default_factory=list)
    predicated: list[Path] = field(default_factory=list)
    llms: list[Any] = field(default_factory=list)
    dry_runs: list[bool] = field(default_factory=list)

    @property
    def handled_names(self) -> list[str]:
        return [p.name for p in self.handled]


@pytest.fixture()
def registry() -> Any:
    """Snapshot/restore the global consumer registry so tests can register
    fakes without leaking into the real consumer set."""
    saved = dict(consumer_base._REGISTRY)
    yield consumer_base._REGISTRY
    consumer_base._REGISTRY.clear()
    consumer_base._REGISTRY.update(saved)


def make_consumer(
    type_name: str,
    *,
    result: Callable[[NotePayload], ConsumerResult] | None = None,
    predicate: Callable[[NotePayload], bool] | None = None,
    uses_llm: bool = False,
    construct_error: Exception | None = None,
) -> Recorder:
    """Register a fake consumer type and return its Recorder."""
    rec = Recorder()
    body_result = result or (lambda payload: ConsumerResult(status=Status.SUCCESS))
    body_predicate = predicate or (lambda payload: True)

    class _Fake(Consumer):
        def __init__(self, config: ConsumerConfig) -> None:
            rec.constructed += 1
            if construct_error is not None:
                raise construct_error
            super().__init__(config)

        def should_process(self, payload: NotePayload) -> bool:
            rec.predicated.append(payload.path)
            return body_predicate(payload)

        def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
            rec.handled.append(payload.path)
            rec.llms.append(ctx.llm)
            rec.dry_runs.append(ctx.dry_run)
            return body_result(payload)

    _Fake.uses_llm = uses_llm
    _Fake.__name__ = f"Fake_{type_name}"
    register(type_name)(_Fake)
    return rec


def make_config(
    root: Path,
    *consumers: ConsumerConfig,
    scan_dirs: list[str] | None = None,
    llm: LLMConfig | None = None,
) -> Config:
    vault = VaultConfig(root=root)
    if scan_dirs is not None:
        vault = VaultConfig(root=root, scan_dirs=scan_dirs)
    return Config(
        vault=vault,
        consumers=list(consumers),
        llm=llm or LLMConfig(),
    )


def cc(name: str, type_name: str | None = None, **kwargs: Any) -> ConsumerConfig:
    return ConsumerConfig(name=name, type=type_name or name, **kwargs)


def by_name(summary: Any, name: str) -> Any:
    return next(c for c in summary.consumers if c.name == name)


# ---------------------------------------------------------------------------
# The happy path and the summary contract (06 §4, §7)
# ---------------------------------------------------------------------------


def _has_tag(payload: NotePayload, tag: str) -> bool:
    raw = payload.frontmatter.get("tags")
    values = raw if isinstance(raw, list) else [raw]
    return any(str(v).strip().lower() == tag for v in values if v is not None)


def test_golden_run_delivers_matching_notes_only(fixture_vault: Path, registry: Any) -> None:
    """06 §7 acceptance, first line: a ``question``-tagged capture reaches
    the consumer and nothing else does."""
    rec = make_consumer("q_fake", predicate=lambda p: _has_tag(p, "question"))
    config = make_config(
        fixture_vault, cc("asker", "q_fake", include_paths=["capture/raw_capture"])
    )
    store = FakeStore()

    summary = run_consumers(config, store)

    assert rec.handled_names == ["2026-07-02T10:00:00.000Z.md"]
    counts = by_name(summary, "asker")
    assert (counts.success, counts.skip, counts.error, counts.limit) == (1, 0, 0, 0)
    assert counts.filtered > 0  # everything else was filtered, not persisted
    assert counts.filtered + counts.processed == summary.notes_scanned
    assert summary.exit_code == 0
    assert summary.notes_scanned > 5


def test_summary_line_matches_the_spec_format(fixture_vault: Path, registry: Any) -> None:
    """06 §4: ``Consumer X: success=N skip=N limit=N error=N filtered=N``
    — ``filtered`` IS reported (08 §B18)."""
    make_consumer("fmt_fake")
    config = make_config(fixture_vault, cc("fmt", "fmt_fake", include_paths=[IDEAS_NOTE]))

    summary = run_consumers(config, FakeStore())

    assert by_name(summary, "fmt").line() == (
        "Consumer fmt: success=1 skip=0 limit=0 error=0 filtered="
        f"{by_name(summary, 'fmt').filtered}"
    )
    assert any("Consumer fmt:" in line for line in summary.lines())


def test_run_logs_its_summary_to_the_journal(
    fixture_vault: Path, registry: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """06 §7 observability: counts and failures must be visible without a
    debugger — the outage was invisible because nothing was logged."""
    make_consumer(
        "loud_fake",
        result=lambda p: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    config = make_config(fixture_vault, cc("loud", "loud_fake", include_paths=[IDEAS_NOTE]))

    with caplog.at_level(logging.INFO):
        summary = run_consumers(config, FakeStore())

    assert "Consumer loud:" in caplog.text
    assert "run complete" in caplog.text
    assert summary.exit_code == 1
    assert by_name(summary, "loud").failures
    assert "boom" in " ".join(by_name(summary, "loud").failures)


def test_per_consumer_duration_and_processed_are_reported(
    fixture_vault: Path, registry: Any
) -> None:
    make_consumer("timed_fake")
    config = make_config(fixture_vault, cc("timed", "timed_fake", include_paths=[IDEAS_NOTE]))

    summary = run_consumers(config, FakeStore())

    entry = by_name(summary, "timed")
    assert entry.duration_seconds >= 0.0
    assert entry.processed == 1
    assert summary.duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# Checkpoint-semantics matrix (06 §1, 08 §B3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "checkpointed", "exit_code"),
    [
        (Status.SUCCESS, True, 0),
        (Status.SKIP, True, 0),
        (Status.ERROR, False, 1),
        (Status.LIMIT, False, 0),
    ],
)
def test_checkpoint_semantics_matrix(
    fixture_vault: Path, registry: Any, status: Status, checkpointed: bool, exit_code: int
) -> None:
    """success/skip are TERMINAL and checkpointed; error/limit are not and
    therefore retry next run (06 §1; 08 §B3 checkpointed ``limit``)."""
    make_consumer("matrix_fake", result=lambda p: ConsumerResult(status=status))
    config = make_config(fixture_vault, cc("matrix", "matrix_fake", include_paths=[IDEAS_NOTE]))
    store = FakeStore()

    summary = run_consumers(config, store)

    note = (fixture_vault / IDEAS_NOTE).resolve()
    assert (store.get_emission("matrix", note) is not None) is checkpointed
    assert summary.exit_code == exit_code
    counts = by_name(summary, "matrix")
    assert getattr(counts, status.value) == 1


@pytest.mark.parametrize("status", [Status.ERROR, Status.LIMIT])
def test_non_terminal_results_are_retried_next_run(
    fixture_vault: Path, registry: Any, status: Status
) -> None:
    rec = make_consumer("retry_fake", result=lambda p: ConsumerResult(status=status))
    config = make_config(fixture_vault, cc("retry", "retry_fake", include_paths=[IDEAS_NOTE]))
    store = FakeStore()

    run_consumers(config, store)
    run_consumers(config, store)

    assert len(rec.handled) == 2  # re-delivered, unchanged note


@pytest.mark.parametrize("status", [Status.SUCCESS, Status.SKIP])
def test_terminal_results_are_not_redelivered(
    fixture_vault: Path, registry: Any, status: Status
) -> None:
    rec = make_consumer("terminal_fake", result=lambda p: ConsumerResult(status=status))
    config = make_config(fixture_vault, cc("term", "terminal_fake", include_paths=[IDEAS_NOTE]))
    store = FakeStore()

    run_consumers(config, store)
    summary = run_consumers(config, store)

    assert len(rec.handled) == 1
    assert by_name(summary, "term").processed == 0


def test_rerun_is_idempotent_across_the_whole_vault(fixture_vault: Path, registry: Any) -> None:
    """06 §7 acceptance: rerun ⇒ zero work; edit ⇒ re-emitted."""
    rec = make_consumer("idem_fake")
    config = make_config(fixture_vault, cc("idem", "idem_fake"))
    store = FakeStore()

    first = run_consumers(config, store)
    after_first = store.snapshot()
    second = run_consumers(config, store)

    assert by_name(first, "idem").success > 0
    assert by_name(second, "idem").success == 0
    assert store.snapshot() == after_first
    handled_once = len(rec.handled)

    target = fixture_vault / IDEAS_NOTE
    target.write_text(target.read_text(encoding="utf-8") + "\n- new idea\n", encoding="utf-8")
    third = run_consumers(config, store)

    assert by_name(third, "idem").success == 1
    assert len(rec.handled) == handled_once + 1
    assert rec.handled[-1] == target.resolve()


def test_metadata_from_the_result_reaches_the_checkpoint(
    fixture_vault: Path, registry: Any
) -> None:
    make_consumer(
        "meta_fake",
        result=lambda p: ConsumerResult(status=Status.SUCCESS, metadata={"task_uuid": "abc"}),
    )
    config = make_config(fixture_vault, cc("meta", "meta_fake", include_paths=[IDEAS_NOTE]))
    store = FakeStore()

    run_consumers(config, store)

    row = store.get_emission("meta", (fixture_vault / IDEAS_NOTE).resolve())
    assert row is not None and row["metadata"] == {"task_uuid": "abc"}


# ---------------------------------------------------------------------------
# Filters are never persisted (08 §B4) and config changes are retroactive
# ---------------------------------------------------------------------------


def test_filter_misses_are_not_persisted(fixture_vault: Path, registry: Any) -> None:
    """08 §B4: the live DB is 15 MB / 30k rows because filter misses were
    checkpointed per hash. Nothing but a terminal result may be written."""
    make_consumer("narrow_fake")
    config = make_config(
        fixture_vault, cc("narrow", "narrow_fake", include_paths=["capture/raw_capture"])
    )
    store = FakeStore()

    summary = run_consumers(config, store)

    assert by_name(summary, "narrow").filtered > 0
    written = {Path(path).name for (_c, path) in store.emissions}
    assert not any(name == "ideas.md" for name in written)
    assert all(row["status"] in {"success", "skip"} for row in store.emissions.values())


def test_widening_include_paths_applies_retroactively(fixture_vault: Path, registry: Any) -> None:
    """06 §7 acceptance / 08 §B4: previously-filtered old notes process once
    the filter widens — no DB surgery required."""
    rec = make_consumer("wide_fake")
    store = FakeStore()

    narrow = make_config(
        fixture_vault, cc("wide", "wide_fake", include_paths=["capture/raw_capture"])
    )
    run_consumers(narrow, store)
    assert "ideas.md" not in rec.handled_names

    wider = make_config(
        fixture_vault, cc("wide", "wide_fake", include_paths=["capture/raw_capture", "projects"])
    )
    summary = run_consumers(wider, store)

    assert "ideas.md" in rec.handled_names
    assert by_name(summary, "wide").success == 1


def test_tightening_should_process_applies_retroactively(
    fixture_vault: Path, registry: Any
) -> None:
    """The predicate is re-evaluated every run; a note that stops matching
    is simply filtered, and its old terminal emission stays put."""
    allow = {"enabled": True}
    rec = make_consumer("pred_fake", predicate=lambda p: allow["enabled"])
    config = make_config(fixture_vault, cc("pred", "pred_fake", include_paths=[IDEAS_NOTE]))
    store = FakeStore()

    run_consumers(config, store)
    allow["enabled"] = False
    target = fixture_vault / IDEAS_NOTE
    target.write_text(target.read_text(encoding="utf-8") + "\n- edited\n", encoding="utf-8")
    summary = run_consumers(config, store)

    counts = by_name(summary, "pred")
    assert counts.processed == 0  # the edited note no longer matches
    assert counts.filtered == summary.notes_scanned
    assert len(rec.handled) == 1
    # the old terminal emission is untouched — a filter miss writes nothing
    assert store.get_emission("pred", target.resolve())["status"] == "success"


def test_exclude_paths_are_honoured(fixture_vault: Path, registry: Any) -> None:
    rec = make_consumer("excl_fake")
    config = make_config(
        fixture_vault,
        cc(
            "excl",
            "excl_fake",
            include_paths=["capture/raw_capture"],
            exclude_paths=["capture/raw_capture/private-thought.md"],
        ),
    )

    run_consumers(config, FakeStore())

    assert "private-thought.md" not in rec.handled_names
    assert "scalar-tags.md" in rec.handled_names


# ---------------------------------------------------------------------------
# The central no-ai guard (spec 02 vault law, 06 §2)
# ---------------------------------------------------------------------------


def test_no_ai_note_never_reaches_an_llm_consumer(fixture_vault: Path, registry: Any) -> None:
    llm_rec = make_consumer("ai_fake", uses_llm=True)
    plain_rec = make_consumer("plain_fake", uses_llm=False)
    config = make_config(
        fixture_vault,
        cc("ai", "ai_fake", include_paths=["capture/raw_capture"]),
        cc("plain", "plain_fake", include_paths=["capture/raw_capture"]),
    )

    run_consumers(config, FakeStore())

    assert "private-thought.md" not in llm_rec.handled_names
    # the guard runs BEFORE should_process — the payload is never offered
    assert not any(p.name == "private-thought.md" for p in llm_rec.predicated)
    # a non-LLM consumer is unaffected: the law is about AI, not automation
    assert "private-thought.md" in plain_rec.handled_names


def test_no_ai_denial_is_counted_and_not_persisted(fixture_vault: Path, registry: Any) -> None:
    rec = make_consumer("ai2_fake", uses_llm=True)
    config = make_config(fixture_vault, cc("ai2", "ai2_fake", include_paths=[NO_AI_NOTE]))
    store = FakeStore()

    summary = run_consumers(config, store)

    counts = by_name(summary, "ai2")
    assert counts.processed == 0
    assert counts.filtered == summary.notes_scanned  # incl. the no-ai denial
    assert rec.predicated == []  # never even offered to the consumer
    assert store.emissions == {}  # a denial is never persisted


def test_no_ai_guard_tolerates_string_and_underscore_spellings(
    fixture_vault: Path, registry: Any
) -> None:
    """``frontmatter.is_no_ai`` is THE authority: ``no_ai: 'true'`` is the
    same vault law as ``no-ai: true`` (spec 02)."""
    note = fixture_vault / "capture/raw_capture/quiet.md"
    note.write_text("---\nid: quiet\nno_ai: 'true'\n---\nbody\n", encoding="utf-8")
    rec = make_consumer("ai3_fake", uses_llm=True)

    run_consumers(make_config(fixture_vault, cc("ai3", "ai3_fake")), FakeStore())

    assert "quiet.md" not in rec.handled_names
    assert "ideas.md" in rec.handled_names


# ---------------------------------------------------------------------------
# max_notes_per_run: successes only (08 §B13), limit not checkpointed (§B3)
# ---------------------------------------------------------------------------


def test_skips_do_not_consume_the_budget(fixture_vault: Path, registry: Any) -> None:
    """08 §B13: a skip used to burn a slot, so a vault full of
    already-processed notes starved the ones that needed work."""

    # scan order is the vault-relative path, so this one comes LAST: under
    # the B13 bug the first skip burns the single slot and it never runs.
    last = "impro.md"

    def result(payload: NotePayload) -> ConsumerResult:
        if payload.path.name == last:
            return ConsumerResult(status=Status.SUCCESS)
        return ConsumerResult(status=Status.SKIP)

    rec = make_consumer("budget_fake", result=result)
    config = make_config(fixture_vault, cc("budget", "budget_fake", max_notes_per_run=1))
    store = FakeStore()

    summary = run_consumers(config, store)

    counts = by_name(summary, "budget")
    assert counts.success == 1
    assert counts.skip == summary.notes_scanned - 1
    assert counts.limit == 0
    assert rec.handled_names[-1] == last
    assert store.statuses("budget")[last] == "success"


def test_over_cap_notes_yield_limit_and_are_retried(fixture_vault: Path, registry: Any) -> None:
    rec = make_consumer("cap_fake")
    config = make_config(
        fixture_vault,
        cc("cap", "cap_fake", include_paths=["capture/raw_capture"], max_notes_per_run=2),
    )
    store = FakeStore()

    first = run_consumers(config, store)

    counts = by_name(first, "cap")
    assert counts.success == 2
    assert counts.limit > 0
    assert len(store.emissions) == 2  # limit rows were NOT written (08 §B3)

    handled_first = len(rec.handled)
    second = run_consumers(config, store)

    assert by_name(second, "cap").success == 2
    assert len(rec.handled) == handled_first + 2  # the backlog drains, it is not lost


def test_the_cap_is_per_consumer(fixture_vault: Path, registry: Any) -> None:
    make_consumer("a_fake")
    make_consumer("b_fake")
    config = make_config(
        fixture_vault,
        cc("a", "a_fake", include_paths=["capture/raw_capture"], max_notes_per_run=1),
        cc("b", "b_fake", include_paths=["capture/raw_capture"], max_notes_per_run=3),
    )

    summary = run_consumers(config, FakeStore())

    assert by_name(summary, "a").success == 1
    assert by_name(summary, "b").success == 3


# ---------------------------------------------------------------------------
# Isolation: one broken consumer must never kill the pipeline (08 §B2/§B12)
# ---------------------------------------------------------------------------


def test_a_consumer_raising_mid_run_does_not_stop_its_siblings(
    fixture_vault: Path, registry: Any
) -> None:
    """08 §B12 — and the shape of the actual 3-month outage."""

    def explode(payload: NotePayload) -> ConsumerResult:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    bad = make_consumer("bad_fake", result=explode)
    good = make_consumer("good_fake")
    config = make_config(
        fixture_vault,
        cc("bad", "bad_fake", include_paths=["capture/raw_capture"]),
        cc("good", "good_fake", include_paths=["capture/raw_capture"]),
    )
    store = FakeStore()

    summary = run_consumers(config, store)

    # the failing consumer kept going over ITS remaining notes …
    assert len(bad.handled) > 1
    # … and the next consumer ran in full
    assert len(good.handled) == len(bad.handled)
    assert by_name(summary, "bad").error == len(bad.handled)
    assert by_name(summary, "good").success == len(good.handled)
    assert summary.exit_code == 1
    assert all(row["status"] == "success" for row in store.emissions.values())


def test_a_constructor_that_raises_is_isolated(fixture_vault: Path, registry: Any) -> None:
    """08 §B2: constructors are pure, but if one still blows up it must not
    take the other three consumers down with it."""
    make_consumer("boom_fake", construct_error=RuntimeError("no taskrc"))
    good = make_consumer("ok_fake")
    config = make_config(
        fixture_vault,
        cc("boom", "boom_fake", include_paths=[IDEAS_NOTE]),
        cc("ok", "ok_fake", include_paths=[IDEAS_NOTE]),
    )

    summary = run_consumers(config, FakeStore())

    assert by_name(summary, "boom").error == 1
    assert "no taskrc" in " ".join(by_name(summary, "boom").failures)
    assert by_name(summary, "ok").success == 1
    assert len(good.handled) == 1
    assert summary.exit_code == 1


def test_each_consumer_is_constructed_exactly_once_per_run(
    fixture_vault: Path, registry: Any
) -> None:
    rec = make_consumer("once_fake")
    config = make_config(fixture_vault, cc("once", "once_fake"))

    run_consumers(config, FakeStore())

    assert rec.constructed == 1


def test_an_unregistered_consumer_type_is_isolated(fixture_vault: Path, registry: Any) -> None:
    good = make_consumer("real_fake")
    config = make_config(
        fixture_vault,
        cc("ghost", "not_registered_anywhere", include_paths=[IDEAS_NOTE]),
        cc("real", "real_fake", include_paths=[IDEAS_NOTE]),
    )

    summary = run_consumers(config, FakeStore())

    assert by_name(summary, "ghost").error == 1
    assert by_name(summary, "real").success == 1
    assert len(good.handled) == 1


def test_a_raising_predicate_is_a_per_note_error(fixture_vault: Path, registry: Any) -> None:
    def predicate(payload: NotePayload) -> bool:
        if payload.path.name == "ideas.md":
            raise ValueError("bad predicate")
        return True

    rec = make_consumer("pred2_fake", predicate=predicate)
    config = make_config(fixture_vault, cc("pred2", "pred2_fake"))

    summary = run_consumers(config, FakeStore())

    assert by_name(summary, "pred2").error == 1
    assert by_name(summary, "pred2").success > 0
    assert "ideas.md" not in rec.handled_names


def test_a_checkpoint_failure_is_recorded_not_fatal(fixture_vault: Path, registry: Any) -> None:
    make_consumer("ckpt_fake")
    config = make_config(fixture_vault, cc("ckpt", "ckpt_fake"))
    store = FakeStore()
    store.fail_checkpoint_for = {str((fixture_vault / IDEAS_NOTE).resolve())}

    summary = run_consumers(config, store)

    counts = by_name(summary, "ckpt")
    assert counts.error == 1
    assert counts.success > 0
    assert summary.exit_code == 1


def test_a_consumer_returning_a_non_result_is_an_error(fixture_vault: Path, registry: Any) -> None:
    make_consumer("junk_fake", result=lambda p: "not a ConsumerResult")  # type: ignore[return-value]
    config = make_config(fixture_vault, cc("junk", "junk_fake", include_paths=[IDEAS_NOTE]))
    store = FakeStore()

    summary = run_consumers(config, store)

    assert by_name(summary, "junk").error == 1
    assert store.emissions == {}


# ---------------------------------------------------------------------------
# Consumer selection (06 §4)
# ---------------------------------------------------------------------------


def test_only_selects_case_insensitively(fixture_vault: Path, registry: Any) -> None:
    make_consumer("sel_a_fake")
    make_consumer("sel_b_fake")
    config = make_config(
        fixture_vault,
        cc("Alpha", "sel_a_fake", include_paths=[IDEAS_NOTE]),
        cc("Beta", "sel_b_fake", include_paths=[IDEAS_NOTE]),
    )

    summary = run_consumers(config, FakeStore(), only=["alpha"])

    assert [c.name for c in summary.consumers] == ["Alpha"]


def test_unknown_only_name_is_a_usage_error(fixture_vault: Path, registry: Any) -> None:
    """06 §4: unknown ``--consumer`` ⇒ exit 2; the runner raises ConfigError
    and the CLI maps it."""
    make_consumer("sel_c_fake")
    config = make_config(fixture_vault, cc("Alpha", "sel_c_fake"))

    with pytest.raises(ConfigError) as excinfo:
        run_consumers(config, FakeStore(), only=["nope"])

    assert "nope" in str(excinfo.value)
    assert "alpha" in (excinfo.value.hint or "")


def test_disabled_consumers_do_not_run(fixture_vault: Path, registry: Any) -> None:
    rec = make_consumer("off_fake")
    config = make_config(fixture_vault, cc("off", "off_fake", enabled=False))

    summary = run_consumers(config, FakeStore())

    assert summary.consumers == []
    assert rec.constructed == 0


def test_explicitly_selecting_a_disabled_consumer_still_respects_the_switch(
    fixture_vault: Path, registry: Any, caplog: pytest.LogCaptureFixture
) -> None:
    rec = make_consumer("off2_fake")
    config = make_config(fixture_vault, cc("off2", "off2_fake", enabled=False))

    with caplog.at_level(logging.WARNING):
        summary = run_consumers(config, FakeStore(), only=["off2"])

    assert summary.consumers == []
    assert rec.constructed == 0
    assert "disabled" in caplog.text


def test_consumers_run_in_config_order(fixture_vault: Path, registry: Any) -> None:
    make_consumer("ord_a_fake")
    make_consumer("ord_b_fake")
    config = make_config(
        fixture_vault,
        cc("second", "ord_b_fake", include_paths=[IDEAS_NOTE]),
        cc("first", "ord_a_fake", include_paths=[IDEAS_NOTE]),
    )

    summary = run_consumers(config, FakeStore())

    assert [c.name for c in summary.consumers] == ["second", "first"]


# ---------------------------------------------------------------------------
# Dry run (09 §5.6)
# ---------------------------------------------------------------------------


def test_dry_run_leaves_the_store_byte_identical(fixture_vault: Path, registry: Any) -> None:
    rec = make_consumer("dry_fake")
    config = make_config(fixture_vault, cc("dry", "dry_fake"))
    store = FakeStore()
    before = store.snapshot()

    summary = run_consumers(config, store, dry_run=True)

    assert store.snapshot() == before
    assert store.writes == []  # not one call that would mutate the DB
    assert store.purge_calls == []
    assert rec.handled == []  # zero consumer side effects
    assert summary.dry_run is True
    assert by_name(summary, "dry").would_process > 0
    assert by_name(summary, "dry").success == 0


def test_dry_run_still_scans_and_decides(fixture_vault: Path, registry: Any) -> None:
    rec = make_consumer("dry2_fake")
    config = make_config(
        fixture_vault, cc("dry2", "dry2_fake", include_paths=["capture/raw_capture"])
    )

    summary = run_consumers(config, FakeStore(), dry_run=True)

    counts = by_name(summary, "dry2")
    assert summary.notes_scanned > 0
    assert counts.filtered > 0
    assert counts.would_process > 0
    assert rec.predicated  # the predicate was evaluated for real
    assert "[DRY-RUN]" in summary.lines()[0]


def test_dry_run_does_not_change_what_a_real_run_would_do(
    fixture_vault: Path, registry: Any
) -> None:
    make_consumer("dry3_fake")
    config = make_config(fixture_vault, cc("dry3", "dry3_fake", include_paths=[IDEAS_NOTE]))
    store = FakeStore()

    rehearsal = run_consumers(config, store, dry_run=True)
    real = run_consumers(config, store)

    assert by_name(rehearsal, "dry3").would_process == by_name(real, "dry3").success == 1


# ---------------------------------------------------------------------------
# Soft purge (06 §1, 08 §B5)
# ---------------------------------------------------------------------------


def test_soft_purge_runs_with_the_retention_window(fixture_vault: Path, registry: Any) -> None:
    make_consumer("purge_fake")
    config = make_config(fixture_vault, cc("purge", "purge_fake"))
    store = FakeStore()

    run_consumers(config, store)

    assert len(store.purge_calls) == 1
    call = store.purge_calls[0]
    assert call["retention_days"] == 30
    assert call["scan_dirs_ok"] is True


def test_soft_purge_is_suppressed_when_a_scan_dir_is_missing(
    fixture_vault: Path, registry: Any
) -> None:
    """08 §B5: a transient mount or a narrowed scan_dirs must never make the
    pipeline forget a year of LLM-run checkpoints."""
    make_consumer("purge2_fake")
    config = make_config(
        fixture_vault, cc("purge2", "purge2_fake"), scan_dirs=["capture/raw_capture", "gone"]
    )
    store = FakeStore()

    run_consumers(config, store)

    assert store.purge_calls[0]["scan_dirs_ok"] is False


def test_soft_purge_is_suppressed_when_a_scan_dir_is_empty(
    tmp_path: Path, fixture_vault: Path, registry: Any
) -> None:
    (fixture_vault / "empty_dir").mkdir()
    make_consumer("purge3_fake")
    config = make_config(
        fixture_vault, cc("purge3", "purge3_fake"), scan_dirs=["capture/raw_capture", "empty_dir"]
    )
    store = FakeStore()

    run_consumers(config, store)

    assert store.purge_calls[0]["scan_dirs_ok"] is False


def test_mark_seen_covers_every_scanned_note(fixture_vault: Path, registry: Any) -> None:
    make_consumer("seen_fake")
    config = make_config(fixture_vault, cc("seen", "seen_fake"))
    store = FakeStore()

    summary = run_consumers(config, store)

    assert len(store.last_seen) == summary.notes_scanned


def test_a_purge_failure_never_fails_a_good_run(fixture_vault: Path, registry: Any) -> None:
    make_consumer("purge4_fake")
    config = make_config(fixture_vault, cc("purge4", "purge4_fake", include_paths=[IDEAS_NOTE]))
    store = FakeStore()
    store.soft_purge = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db locked"))  # type: ignore[assignment]

    summary = run_consumers(config, store)

    assert summary.exit_code == 0
    assert summary.purged == 0
    assert by_name(summary, "purge4").success == 1


# ---------------------------------------------------------------------------
# The one shared LLM client (09 §2)
# ---------------------------------------------------------------------------


def test_only_llm_consumers_receive_a_client(fixture_vault: Path, registry: Any) -> None:
    ai = make_consumer("llm_a_fake", uses_llm=True)
    plain = make_consumer("llm_b_fake", uses_llm=False)
    config = make_config(
        fixture_vault,
        cc("ai", "llm_a_fake", include_paths=[IDEAS_NOTE]),
        cc("plain", "llm_b_fake", include_paths=[IDEAS_NOTE]),
        llm=LLMConfig(ollama_host="http://127.0.0.1:1", ollama_model="test-model"),
    )

    run_consumers(config, FakeStore())

    assert ai.llms and ai.llms[0] is not None
    assert plain.llms == [None]


def test_llm_consumers_share_one_client(fixture_vault: Path, registry: Any) -> None:
    """09 §2: ONE client per run — never a second HTTP path."""
    first = make_consumer("llm_c_fake", uses_llm=True)
    second = make_consumer("llm_d_fake", uses_llm=True)
    config = make_config(
        fixture_vault,
        cc("one", "llm_c_fake", include_paths=[IDEAS_NOTE]),
        cc("two", "llm_d_fake", include_paths=[IDEAS_NOTE]),
        llm=LLMConfig(ollama_host="http://127.0.0.1:1", ollama_model="test-model"),
    )

    run_consumers(config, FakeStore())

    assert first.llms[0] is second.llms[0]


def test_an_unconfigured_llm_degrades_instead_of_failing_the_run(
    fixture_vault: Path, registry: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """06 §2 forbids hardcoded host fallbacks; a missing host must degrade
    (consumers contract to work unenriched), not kill the pipeline."""
    ai = make_consumer("llm_e_fake", uses_llm=True)
    config = make_config(fixture_vault, cc("ai", "llm_e_fake", include_paths=[IDEAS_NOTE]))

    with caplog.at_level(logging.WARNING):
        summary = run_consumers(config, FakeStore())

    assert ai.llms == [None]
    assert summary.exit_code == 0
    assert "LLM backend unavailable" in caplog.text


def test_the_run_context_carries_the_dry_run_flag(fixture_vault: Path, registry: Any) -> None:
    rec = make_consumer("ctx_fake")
    config = make_config(fixture_vault, cc("ctx", "ctx_fake", include_paths=[IDEAS_NOTE]))

    run_consumers(config, FakeStore())

    assert rec.dry_runs == [False]


# ---------------------------------------------------------------------------
# Ingestion failures are never fatal to a run (08 §B1 class)
# ---------------------------------------------------------------------------


def test_a_broken_note_does_not_stop_the_run(fixture_vault: Path, registry: Any) -> None:
    rec = make_consumer("tolerant_fake")
    config = make_config(fixture_vault, cc("tolerant", "tolerant_fake"))

    summary = run_consumers(config, FakeStore())

    assert "broken-yaml.md" not in rec.handled_names
    assert "invalid-utf8.md" in rec.handled_names  # bad bytes are replaced, not fatal
    assert summary.exit_code == 0


def test_a_totally_missing_vault_raises_before_any_consumer_runs(
    tmp_path: Path, registry: Any
) -> None:
    rec = make_consumer("never_fake")
    root = tmp_path / "vault"
    root.mkdir()
    config = make_config(root, cc("never", "never_fake"), scan_dirs=["gone"])

    with pytest.raises(ConfigError):
        run_consumers(config, FakeStore())

    assert rec.handled == []


# ---------------------------------------------------------------------------
# Performance gate for the runner's own work (spec 09 §4)
# ---------------------------------------------------------------------------

PERF_NOTE_COUNT = 1000
#: 09 §4 budgets a full 7.5k-note run at 30 s with no LLM work. This gate
#: isolates the ORCHESTRATOR's share — scan, parse, filter, dispatch — by
#: running against the zero-I/O fake store, so a quadratic walk or a
#: per-note re-parse fails here instead of hiding behind SQLite fsyncs.
#: Measured 2026-08-16: ~0.15 s. The budget is 20x that so parallel CI load
#: cannot flake it.
PERF_BUDGET_SECONDS = 3.0


@pytest.fixture(scope="module")
def perf_vault(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("runner-orchestration-perf")
    raw = root / "capture" / "raw_capture"
    raw.mkdir(parents=True, exist_ok=True)
    for folder in ("projects", "areas", "resources"):
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / "index.md").write_text("---\ntags:\n- index\n---\nx\n", encoding="utf-8")
    for n in range(PERF_NOTE_COUNT):
        (raw / f"gen-{n:04d}.md").write_text(
            f"---\nid: 'gen-{n:04d}'\ntags:\n- generated\n- topic-{n % 25}\n"
            f"sources:\n- me\n---\n# Note {n}\n\nRealistic body prose for note {n}.\n",
            encoding="utf-8",
        )
    return root


def test_orchestration_over_a_thousand_notes_is_within_budget(
    perf_vault: Path, registry: Any
) -> None:
    make_consumer("perf_one_fake")
    make_consumer("perf_two_fake", predicate=lambda p: _has_tag(p, "generated"))
    config = make_config(
        perf_vault,
        cc("one", "perf_one_fake", max_notes_per_run=PERF_NOTE_COUNT + 10),
        cc("two", "perf_two_fake", max_notes_per_run=PERF_NOTE_COUNT + 10),
    )

    started = time.monotonic()
    summary = run_consumers(config, FakeStore())
    elapsed = time.monotonic() - started

    assert summary.notes_scanned == PERF_NOTE_COUNT + 3
    assert by_name(summary, "two").success == PERF_NOTE_COUNT
    assert elapsed < PERF_BUDGET_SECONDS, (
        f"orchestrating {PERF_NOTE_COUNT} notes x 2 consumers took {elapsed:.2f}s "
        f"(budget {PERF_BUDGET_SECONDS}s; spec 09 §4 wants 7.5k notes in 30 s)"
    )


def test_a_second_run_over_a_large_vault_is_cheap(perf_vault: Path, registry: Any) -> None:
    """The steady state — nothing changed — must be dominated by the scan,
    not by re-dispatching every note (06 §1 hash checkpoint)."""
    rec = make_consumer("perf_idem_fake")
    config = make_config(perf_vault, cc("idem", "perf_idem_fake", max_notes_per_run=10_000))
    store = FakeStore()

    run_consumers(config, store)
    handled = len(rec.handled)
    started = time.monotonic()
    summary = run_consumers(config, store)
    elapsed = time.monotonic() - started

    assert len(rec.handled) == handled  # not one extra handle
    assert summary.consumers[0].processed == 0
    assert elapsed < PERF_BUDGET_SECONDS
