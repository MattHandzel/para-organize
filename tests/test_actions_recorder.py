"""ActionRecorder: append-only JSONL writer, query, export, stats (spec 12 §2)
plus the spec 12 §3 acceptance gates and the 08 regression obligations.

Every recorder here writes to a tmp-dir ``actions/`` — never a real home
path, never ``~/.local/share/organize-core`` (paths.py isolation contract).
"""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path

import pytest

from organize_core import actions as actions_mod
from organize_core.actions import (
    OPERATIONS,
    ActionContext,
    ActionRecord,
    ActionRecorder,
    CaptureState,
    LLMTrace,
    SuggestionShown,
    TargetState,
)


@pytest.fixture()
def actions_dir(tmp_path: Path) -> Path:
    """The injected ``<state>/actions/`` — deliberately NOT pre-created, so
    every test also proves the recorder needs no init call (08 §A16: the
    original logger's ``init`` was never called and file logging was dead)."""
    return tmp_path / "state" / "actions"


def make_record(
    *,
    id: str = "act_TEST",
    ts: str = "2026-08-15T17:40:00Z",
    actor: str = "matt",
    operation: str = "move",
    capture_path: str = "capture/raw_capture/note.md",
    body: str = "capture body\n",
    targets: tuple[TargetState, ...] = (),
    context: ActionContext | None = None,
    edit_mode: str | None = None,
    llm: LLMTrace | None = None,
) -> ActionRecord:
    return ActionRecord(
        id=id,
        ts=ts,
        actor=actor,
        operation=operation,  # type: ignore[arg-type]
        capture=CaptureState(
            path=capture_path,
            content_hash="sha256:" + id,
            frontmatter_before={"tags": ["impro"]},
            body_before=body,
        ),
        targets=targets,
        context=context if context is not None else ActionContext(),
        edit_mode=edit_mode,  # type: ignore[arg-type]
        llm=llm,
    )


def read_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").split("\n") if ln]


# --- write path ------------------------------------------------------------


def test_record_creates_the_month_file_lazily_and_appends_one_line(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    assert not actions_dir.exists()  # pure constructor: no I/O (08 §B2)

    assert recorder.record(make_record()) is True

    month_file = actions_dir / "2026-08.jsonl"
    assert month_file.exists()
    assert [p.name for p in actions_dir.iterdir()] == ["2026-08.jsonl"]
    lines = read_lines(month_file)
    assert len(lines) == 1
    assert ActionRecord.from_json(json.loads(lines[0])) == make_record()
    assert month_file.read_bytes().endswith(b"\n")


def test_month_file_comes_from_the_record_timestamp_not_the_clock(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    recorder.record(make_record(id="act_A", ts="2026-07-01T09:00:00Z"))
    recorder.record(make_record(id="act_B", ts="2026-08-15T17:40:00Z"))
    recorder.record(make_record(id="act_C", ts="2026-08-31T23:59:59.999999+00:00"))

    assert sorted(p.name for p in actions_dir.iterdir()) == ["2026-07.jsonl", "2026-08.jsonl"]
    assert len(read_lines(actions_dir / "2026-07.jsonl")) == 1
    assert len(read_lines(actions_dir / "2026-08.jsonl")) == 2
    assert [r.id for r in recorder.query()] == ["act_A", "act_B", "act_C"]


def test_appends_never_rewrite_earlier_bytes(actions_dir: Path) -> None:
    """Append-only law (12 §2): existing bytes are a strict prefix forever."""
    recorder = ActionRecorder(actions_dir)
    for n in range(3):
        recorder.record(make_record(id=f"act_{n}"))
    month_file = actions_dir / "2026-08.jsonl"
    before = month_file.read_bytes()

    recorder.record(make_record(id="act_3"))
    after = month_file.read_bytes()
    assert after.startswith(before)
    assert len(read_lines(month_file)) == 4


def test_record_file_is_private_0600(actions_dir: Path) -> None:
    """Privacy note in 12 §2: the corpus inherits the vault's sensitivity."""
    recorder = ActionRecorder(actions_dir)
    recorder.record(make_record())
    mode = (actions_dir / "2026-08.jsonl").stat().st_mode & 0o777
    assert mode == 0o600


def test_unicode_body_round_trips_through_the_file(actions_dir: Path) -> None:
    """08 §B18 regression: explicit encoding on every text I/O."""
    body = "meeting notes — café ☕\n日本語 line\nemoji 🧠\n"
    recorder = ActionRecorder(actions_dir)
    assert recorder.record(make_record(capture_path="capture/café ☕.md", body=body)) is True

    (restored,) = list(recorder.query())
    assert restored.capture.body_before == body
    assert restored.capture.path == "capture/café ☕.md"


def test_two_recorders_share_one_month_file(actions_dir: Path) -> None:
    ActionRecorder(actions_dir).record(make_record(id="act_A"))
    ActionRecorder(actions_dir).record(make_record(id="act_B"))
    assert [r.id for r in ActionRecorder(actions_dir).query()] == ["act_A", "act_B"]


# --- spec 12 §3 acceptance gates ------------------------------------------


def test_every_operation_type_produces_exactly_one_valid_record(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    ops = sorted(OPERATIONS)
    assert ops == [
        "append",
        "archive",
        "create_folder",
        "integrate",
        "merge",
        "meta_edit",
        "move",
        "skip",
        "tag_edit",
    ]
    for i, op in enumerate(ops):
        assert recorder.record(make_record(id=f"act_{i}", operation=op)) is True

    records = list(recorder.query())
    assert len(records) == len(ops)
    assert sorted(r.operation for r in records) == ops
    assert len(read_lines(actions_dir / "2026-08.jsonl")) == len(ops)


def test_session_of_five_actions_keeps_session_id_and_chosen_ranks(actions_dir: Path) -> None:
    """Unit half of spec 12 §3's session gate: the RECORDER round-trips what
    it is handed.

    On its own this proves the dataclass, not the write path — and for a long
    time nothing in the product could produce these fields at all, so the
    acceptance test was satisfiable only by hand-feeding it. The end-to-end
    half now lives in `tests/test_server.py::
    test_a_session_of_five_rpc_actions_yields_five_records_with_correct_ranks`,
    which drives five real RPC operations.
    """
    recorder = ActionRecorder(actions_dir)
    ranks = [1, 1, 3, None, 2]
    for i, rank in enumerate(ranks):
        recorder.record(
            make_record(
                id=f"act_{i}",
                operation="skip" if rank is None else "move",
                context=ActionContext(
                    session_id="ses_1",
                    suggestions_shown=(
                        SuggestionShown(path="projects/blog", score=3.25, rank=1),
                        SuggestionShown(path="areas/health", score=1.5, rank=2),
                        SuggestionShown(path="resources/performing", score=0.75, rank=3),
                    ),
                    chosen_rank=rank,
                ),
            )
        )

    records = list(recorder.query())
    assert len(records) == 5
    assert {r.context.session_id for r in records} == {"ses_1"}
    assert [r.context.chosen_rank for r in records] == ranks
    assert [r.id for r in records] == ["act_0", "act_1", "act_2", "act_3", "act_4"]
    assert records[0].context.suggestions_shown[2] == SuggestionShown(
        path="resources/performing", score=0.75, rank=3
    )


def test_multi_destination_action_is_one_record_with_two_targets(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    targets = (
        TargetState(
            path="projects/blog/ideas.md",
            role="append_target",
            before_hash="h1",
            after_hash="h2",
            diff="@@ -1 +1,2 @@\n line\n+first\n",
            description="Blog post ideas",
        ),
        TargetState(
            path="areas/health/index.md",
            role="append_target",
            before_hash="h3",
            after_hash="h4",
            diff="@@ -1 +1,2 @@\n line\n+second\n",
        ),
    )
    recorder.record(make_record(operation="append", edit_mode="append", targets=targets))

    assert len(read_lines(actions_dir / "2026-08.jsonl")) == 1
    (record,) = list(recorder.query())
    assert len(record.targets) == 2
    assert [t.path for t in record.targets] == ["projects/blog/ideas.md", "areas/health/index.md"]
    assert record.targets[0].diff.endswith("+first\n")
    assert record.targets[1].diff.endswith("+second\n")
    assert record.targets[0].description == "Blog post ideas"


def test_integrate_records_proposed_vs_final_diff_with_verdict_edited(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    llm = LLMTrace(
        backend="claude-cli",
        model="claude-opus",
        prompt_hash="sha256:p",
        proposed_diff="+machine wording\n",
        final_diff="+matt's wording\n",
        verdict="edited",
    )
    recorder.record(
        make_record(actor="claude-integrate", operation="integrate", edit_mode="integrate", llm=llm)
    )

    (record,) = list(recorder.query())
    assert record.llm is not None
    assert record.llm.verdict == "edited"
    assert record.llm.proposed_diff != record.llm.final_diff
    assert record.llm.proposed_diff == "+machine wording\n"
    assert record.llm.final_diff == "+matt's wording\n"


def test_rejected_integration_is_recorded_with_untouched_target(actions_dir: Path) -> None:
    """Deletion-guard case (12 §3): verdict rejected, no after_hash change."""
    recorder = ActionRecorder(actions_dir)
    llm = LLMTrace(
        backend="ollama",
        model="gemma3:12b-it-qat",
        prompt_hash="sha256:p",
        proposed_diff="-deleted line\n",
        final_diff="",
        verdict="rejected",
    )
    recorder.record(
        make_record(
            operation="integrate",
            edit_mode="integrate",
            actor="claude-integrate",
            targets=(
                TargetState(
                    path="projects/blog/ideas.md",
                    role="destination",
                    before_hash="h1",
                    after_hash="h1",
                    diff="",
                ),
            ),
            llm=llm,
        )
    )

    (record,) = list(recorder.query())
    assert record.llm is not None and record.llm.verdict == "rejected"
    assert record.targets[0].before_hash == record.targets[0].after_hash
    assert record.targets[0].diff == ""
    assert recorder.stats()["integrate"]["rejected"] == 1


# --- failure handling (12 §2 "never blocks the operation", 12 §3) ----------


def test_record_returns_false_and_logs_loudly_when_the_dir_is_unusable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "actions"
    blocker.write_text("not a directory\n", encoding="utf-8")
    recorder = ActionRecorder(blocker)

    with caplog.at_level(logging.ERROR, logger="organize_core.actions"):
        assert recorder.record(make_record(id="act_LOST")) is False

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "ACTION RECORD LOST" in errors[0].getMessage()
    assert "act_LOST" in errors[0].getMessage()
    assert errors[0].exc_info is not None  # loud: traceback attached
    assert blocker.read_text(encoding="utf-8") == "not a directory\n"


def test_record_returns_false_on_disk_full_and_writes_nothing(
    actions_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = ActionRecorder(actions_dir)
    assert recorder.record(make_record(id="act_GOOD")) is True
    month_file = actions_dir / "2026-08.jsonl"
    before = month_file.read_bytes()

    def enospc(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(actions_mod, "_raw_write", enospc)
    with caplog.at_level(logging.ERROR, logger="organize_core.actions"):
        assert recorder.record(make_record(id="act_DROPPED")) is False

    assert month_file.read_bytes() == before
    assert any("ACTION RECORD LOST" in r.getMessage() for r in caplog.records)


def test_torn_write_never_leaves_a_partial_jsonl_line(
    actions_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Spec 12 §3 torn-write gate: a half-written line is rolled back."""
    recorder = ActionRecorder(actions_dir)
    assert recorder.record(make_record(id="act_GOOD")) is True
    month_file = actions_dir / "2026-08.jsonl"
    before = month_file.read_bytes()

    def half_then_fail(fd: int, data: bytes) -> int:
        os.write(fd, data[: len(data) // 2])
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(actions_mod, "_raw_write", half_then_fail)
    with caplog.at_level(logging.ERROR, logger="organize_core.actions"):
        assert recorder.record(make_record(id="act_TORN")) is False

    assert month_file.read_bytes() == before
    text = month_file.read_text(encoding="utf-8")
    assert text.endswith("\n")
    lines = read_lines(month_file)
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "act_GOOD"
    assert any("partial line" in r.getMessage() for r in caplog.records)

    # the recorder keeps working after a rolled-back write
    monkeypatch.setattr(actions_mod, "_raw_write", os.write)
    assert recorder.record(make_record(id="act_AFTER")) is True
    assert [r.id for r in recorder.query()] == ["act_GOOD", "act_AFTER"]


def test_a_torn_tail_from_a_PREVIOUS_process_does_not_swallow_the_next_record(
    actions_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Spec 12 §3's torn-write gate, from the direction the rollback cannot
    reach: the rollback above only undoes a partial write in THIS process. A
    SIGKILL or a power loss leaves a line with no terminator, and the next
    append then concatenated a brand-new, otherwise-valid record onto it — the
    reader skipped ONE physical line and the new record was gone, silently.
    """
    actions_dir.mkdir(parents=True, exist_ok=True)
    month_file = actions_dir / "2026-08.jsonl"
    crashed = b'{"schema_version":1,"id":"act_TORN","operation":"mo'
    month_file.write_bytes(crashed)  # NO trailing newline — that is the shape

    recorder = ActionRecorder(actions_dir)
    with caplog.at_level(logging.WARNING, logger="organize_core.actions"):
        assert recorder.record(make_record(id="act_AFTER_CRASH")) is True

    lines = read_lines(month_file)
    assert len(lines) == 2, "the new record is its OWN physical line"
    assert lines[0].encode("utf-8") == crashed, "the torn line is preserved, not rewritten"
    assert json.loads(lines[1])["id"] == "act_AFTER_CRASH"
    # And the reader keeps the new record while skipping only the torn one.
    assert [r.id for r in recorder.query()] == ["act_AFTER_CRASH"]
    assert any("no terminator" in r.getMessage() for r in caplog.records), "loudly (12 §2)"


def test_firing_control_a_terminated_tail_is_appended_to_normally(
    actions_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The healing byte is added ONLY when the tail is torn — an ordinary log
    must not grow a blank line before every record."""
    actions_dir.mkdir(parents=True, exist_ok=True)
    month_file = actions_dir / "2026-08.jsonl"
    recorder = ActionRecorder(actions_dir)
    assert recorder.record(make_record(id="act_ONE")) is True

    with caplog.at_level(logging.WARNING, logger="organize_core.actions"):
        assert recorder.record(make_record(id="act_TWO")) is True

    assert b"\n\n" not in month_file.read_bytes()
    assert [r.id for r in recorder.query()] == ["act_ONE", "act_TWO"]
    assert not any("no terminator" in r.getMessage() for r in caplog.records)


def test_record_returns_false_on_an_unusable_timestamp(
    actions_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = ActionRecorder(actions_dir)
    with caplog.at_level(logging.ERROR, logger="organize_core.actions"):
        assert recorder.record(make_record(id="act_BADTS", ts="yesterday")) is False
    assert not actions_dir.exists()
    assert any("ACTION RECORD LOST" in r.getMessage() for r in caplog.records)


def test_rollback_refuses_to_truncate_bytes_that_are_not_ours(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rollback must never delete another appender's record."""
    path = tmp_path / "2026-08.jsonl"
    path.write_bytes(b'{"complete":true}\n')
    start = path.stat().st_size
    with path.open("ab") as fh:
        fh.write(b'{"partial": ')  # our torn bytes
        fh.write(b'{"other":"writer"}\n')  # somebody else's complete line

    fd = os.open(path, os.O_RDWR)
    try:
        with caplog.at_level(logging.ERROR, logger="organize_core.actions"):
            actions_mod._rollback_partial_line(fd, path, start, b'{"partial": "line"}\n')
    finally:
        os.close(fd)

    assert b'{"other":"writer"}' in path.read_bytes()
    assert any("refusing to truncate" in r.getMessage() for r in caplog.records)


def test_rollback_refuses_when_the_tail_is_not_our_prefix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "2026-08.jsonl"
    path.write_bytes(b"XXXX")
    fd = os.open(path, os.O_RDWR)
    try:
        with caplog.at_level(logging.ERROR, logger="organize_core.actions"):
            actions_mod._rollback_partial_line(fd, path, 0, b'{"a":1}\n')
    finally:
        os.close(fd)

    assert path.read_bytes() == b"XXXX"
    assert any("not our partial line" in r.getMessage() for r in caplog.records)


def test_concurrent_appends_produce_only_whole_lines(actions_dir: Path) -> None:
    """Four threads appending at once: 100 complete, parseable records."""
    import threading

    def worker(worker_id: int) -> None:
        recorder = ActionRecorder(actions_dir)
        for n in range(25):
            body = f"worker {worker_id} record {n}\n" + ("padding line\n" * 400)
            assert recorder.record(make_record(id=f"act_{worker_id}_{n}", body=body)) is True

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = read_lines(actions_dir / "2026-08.jsonl")
    assert len(lines) == 100
    ids = sorted(ActionRecord.from_json(json.loads(ln)).id for ln in lines)
    assert ids == sorted(f"act_{w}_{n}" for w in range(4) for n in range(25))


def test_fsync_failure_does_not_lose_a_complete_line(
    actions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = ActionRecorder(actions_dir)

    def boom(fd: int) -> None:
        raise OSError(errno.EIO, "fsync failed")

    monkeypatch.setattr(os, "fsync", boom)
    assert recorder.record(make_record(id="act_1")) is True
    monkeypatch.undo()

    assert [r.id for r in recorder.query()] == ["act_1"]


def test_record_returns_false_on_an_unserializable_payload(
    actions_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-OSError failures are swallowed too — record() never raises."""
    record = ActionRecord(
        id="act_BAD",
        ts="2026-08-15T17:40:00Z",
        actor="matt",
        operation="move",
        capture=CaptureState(
            path="a.md",
            content_hash="h",
            frontmatter_before={"not_json": object()},
            body_before="",
        ),
    )
    recorder = ActionRecorder(actions_dir)
    with caplog.at_level(logging.ERROR, logger="organize_core.actions"):
        assert recorder.record(record) is False
    assert not actions_dir.exists()
    assert any("act_BAD" in r.getMessage() for r in caplog.records)


def test_record_never_raises_for_any_failure_mode(tmp_path: Path) -> None:
    """The contract callers depend on: recording NEVER raises into an
    already-completed file operation (12 §2)."""
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    for recorder, record in [
        (ActionRecorder(blocker), make_record()),
        (ActionRecorder(tmp_path / "ok"), make_record(ts="not-a-timestamp")),
        (ActionRecorder(tmp_path / "ok"), make_record(ts="2026-13-01T00:00:00Z")),
    ]:
        assert recorder.record(record) is False


# --- read path -------------------------------------------------------------


def seed(recorder: ActionRecorder) -> None:
    recorder.record(make_record(id="act_1", ts="2026-07-15T10:00:00Z", operation="move"))
    recorder.record(
        make_record(id="act_2", ts="2026-08-01T10:00:00Z", operation="archive", actor="matt")
    )
    recorder.record(
        make_record(
            id="act_3", ts="2026-08-15T10:00:00Z", operation="move", actor="consumer:taskwarrior"
        )
    )


def test_query_returns_everything_oldest_first(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    seed(recorder)
    assert [r.id for r in recorder.query()] == ["act_1", "act_2", "act_3"]


def test_query_on_a_missing_directory_is_empty_not_an_error(actions_dir: Path) -> None:
    assert list(ActionRecorder(actions_dir).query()) == []
    assert ActionRecorder(actions_dir).month_files() == []


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"operation": "move"}, ["act_1", "act_3"]),
        ({"actor": "matt"}, ["act_1", "act_2"]),
        ({"actor": "consumer:taskwarrior"}, ["act_3"]),
        ({"since": "2026-08-01"}, ["act_2", "act_3"]),
        ({"until": "2026-08-01"}, ["act_1", "act_2"]),
        ({"since": "2026-08-01", "until": "2026-08-14"}, ["act_2"]),
        ({"since": "2026-08"}, ["act_2", "act_3"]),
        ({"operation": "move", "since": "2026-08-01"}, ["act_3"]),
        ({"since": "2027-01-01"}, []),
        ({"operation": "merge"}, []),
    ],
)
def test_query_filters(actions_dir: Path, filters: dict, expected: list[str]) -> None:
    recorder = ActionRecorder(actions_dir)
    seed(recorder)
    assert [r.id for r in recorder.query(**filters)] == expected


# --- dry-run records are logged but are NOT corpus (spec 12 §2 + 09 §5.6) ---
#
# `--dry-run` operations DO append an ActionRecord (ARCHITECTURE ruling (a):
# the log-only mode is only useful if the intended action is written down),
# but a rehearsal that never touched the vault is not evidence of anything.
# The exclusion lives in the RECORDER so every corpus reader inherits it —
# the CLI used to re-derive it in a local subclass, which is exactly the
# "each caller reimplements the rule" shape that lets one reader forget.


def seed_with_dry_runs(recorder: ActionRecorder) -> None:
    recorder.record(make_record(id="act_real1", ts="2026-08-02T09:00:00Z"))
    recorder.record(
        make_record(
            id="act_dry",
            ts="2026-08-03T09:00:00Z",
            context=ActionContext(dry_run=True),
        )
    )
    recorder.record(make_record(id="act_real2", ts="2026-08-04T09:00:00Z"))


def test_query_excludes_dry_run_records_by_default(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    seed_with_dry_runs(recorder)
    assert [r.id for r in recorder.query()] == ["act_real1", "act_real2"]


def test_query_include_dry_run_opts_them_back_in(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    seed_with_dry_runs(recorder)
    assert [r.id for r in recorder.query(include_dry_run=True)] == [
        "act_real1",
        "act_dry",
        "act_real2",
    ]


def test_the_dry_run_record_is_on_disk_even_though_it_is_hidden(actions_dir: Path) -> None:
    """It is excluded from the corpus, NOT dropped — 09 §5.6 wants the
    intended action on paper so it can be diffed against expectations."""
    recorder = ActionRecorder(actions_dir)
    seed_with_dry_runs(recorder)
    (month_file,) = recorder.month_files()
    ids = [json.loads(line)["id"] for line in read_lines(month_file)]
    assert ids == ["act_real1", "act_dry", "act_real2"]


def test_stats_excludes_dry_runs_so_a_rehearsal_cannot_move_an_accept_rate(
    actions_dir: Path,
) -> None:
    recorder = ActionRecorder(actions_dir)
    shown = (SuggestionShown(path="areas/health", score=3.7, rank=1, reasons=()),)
    # One real action where Matt took the top suggestion...
    recorder.record(
        make_record(
            id="act_real",
            ts="2026-08-02T09:00:00Z",
            context=ActionContext(suggestions_shown=shown, chosen_rank=1),
        )
    )
    # ...and three rehearsals where he took the third. If dry runs counted,
    # the top-accept-rate would read 1/4 = 0.25 instead of a perfect 1.0.
    for i in range(3):
        recorder.record(
            make_record(
                id=f"act_dry{i}",
                ts="2026-08-03T09:00:00Z",
                context=ActionContext(suggestions_shown=shown, chosen_rank=3, dry_run=True),
            )
        )

    stats = recorder.stats()
    assert stats["total"] == 1
    assert stats["by_operation"] == {"move": 1}
    assert stats["suggestions"]["with_suggestions"] == 1
    assert stats["suggestions"]["top_chosen"] == 1
    assert stats["suggestions"]["top_accept_rate"] == 1.0
    assert stats["suggestions"]["rank_histogram"] == {1: 1}

    everything = recorder.stats(include_dry_run=True)
    assert everything["total"] == 4
    assert everything["suggestions"]["top_accept_rate"] == 0.25
    assert everything["suggestions"]["rank_histogram"] == {1: 1, 3: 3}


def test_export_excludes_dry_runs_by_default(actions_dir: Path, tmp_path: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    seed_with_dry_runs(recorder)
    out = tmp_path / "corpus.jsonl"

    assert recorder.export(out) == 2
    assert [json.loads(ln)["id"] for ln in read_lines(out)] == ["act_real1", "act_real2"]

    assert recorder.export(out, include_dry_run=True) == 3
    assert [json.loads(ln)["id"] for ln in read_lines(out)] == [
        "act_real1",
        "act_dry",
        "act_real2",
    ]


def test_a_corrupt_dry_run_marker_fails_closed_and_stays_out_of_the_corpus(
    actions_dir: Path,
) -> None:
    """A record whose marker is neither absent nor exactly ``false`` is
    treated as a dry run. The failure mode we refuse is a rehearsal being
    mistaken for a precedent; the reverse costs one corpus entry."""
    recorder = ActionRecorder(actions_dir)
    recorder.record(make_record(id="act_real"))
    (month_file,) = recorder.month_files()
    payload = json.loads(read_lines(month_file)[0])
    payload["id"] = "act_weird"
    payload["context"]["dry_run"] = "no"  # truthy string, NOT False
    with month_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")

    assert [r.id for r in recorder.query()] == ["act_real"]
    assert [r.id for r in recorder.query(include_dry_run=True)] == ["act_real", "act_weird"]


def test_query_skips_corrupt_lines_with_a_warning(
    actions_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = ActionRecorder(actions_dir)
    recorder.record(make_record(id="act_1"))
    month_file = actions_dir / "2026-08.jsonl"
    with month_file.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
        fh.write('{"id":"act_x","ts":"2026-08-16T00:00:00Z"}\n')  # schema-invalid
        fh.write("\n")  # blank line
    recorder.record(make_record(id="act_2", ts="2026-08-17T00:00:00Z"))

    with caplog.at_level(logging.WARNING, logger="organize_core.actions"):
        assert [r.id for r in recorder.query()] == ["act_1", "act_2"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("corrupt" in r.getMessage() for r in warnings)


def test_query_tolerates_invalid_utf8_bytes_in_the_file(actions_dir: Path) -> None:
    """08 §B18/§B1 class: never a UnicodeDecodeError on read."""
    recorder = ActionRecorder(actions_dir)
    recorder.record(make_record(id="act_1"))
    with (actions_dir / "2026-08.jsonl").open("ab") as fh:
        fh.write(b'{"id": "act_bad\xff\xfe", "ts": "2026-08-16T00:00:00Z"}\n')
    recorder.record(make_record(id="act_2", ts="2026-08-17T00:00:00Z"))

    assert [r.id for r in recorder.query()] == ["act_1", "act_2"]


def test_query_ignores_non_month_files_in_the_directory(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    recorder.record(make_record(id="act_1"))
    (actions_dir / "export.jsonl").write_text(
        json.dumps(make_record(id="act_export").to_json()) + "\n", encoding="utf-8"
    )
    (actions_dir / "notes.txt").write_text("hello\n", encoding="utf-8")
    (actions_dir / "2026-09").mkdir()

    assert [p.name for p in recorder.month_files()] == ["2026-08.jsonl"]
    assert [r.id for r in recorder.query()] == ["act_1"]


# --- export ----------------------------------------------------------------


def test_export_to_file_returns_count_and_writes_parseable_jsonl(
    actions_dir: Path, tmp_path: Path
) -> None:
    recorder = ActionRecorder(actions_dir)
    seed(recorder)
    out = tmp_path / "export" / "corpus.jsonl"

    assert recorder.export(out) == 3
    lines = read_lines(out)
    assert len(lines) == 3
    assert [ActionRecord.from_json(json.loads(ln)).id for ln in lines] == ["act_1", "act_2", "act_3"]


def test_export_honors_filters(actions_dir: Path, tmp_path: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    seed(recorder)
    out = tmp_path / "subset.jsonl"

    assert recorder.export(out, operation="move", since="2026-08-01") == 1
    assert [json.loads(ln)["id"] for ln in read_lines(out)] == ["act_3"]


def test_export_to_stdout(actions_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    recorder = ActionRecorder(actions_dir)
    seed(recorder)

    assert recorder.export() == 3
    out = capsys.readouterr().out
    lines = [ln for ln in out.split("\n") if ln]
    assert len(lines) == 3
    assert [json.loads(ln)["id"] for ln in lines] == ["act_1", "act_2", "act_3"]


def test_export_of_an_empty_corpus_is_zero(actions_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "empty.jsonl"
    assert ActionRecorder(actions_dir).export(out) == 0
    assert out.read_text(encoding="utf-8") == ""


# --- stats -----------------------------------------------------------------


def test_partially_applied_operations_are_counted_but_never_an_accept(
    actions_dir: Path,
) -> None:
    """A move that copied the note but never archived the original DID touch
    the vault — so it stays in `total`/`by_operation` and is named by
    `partial_failures` — but it is not an accepted rank-1 suggestion. On real
    data three such failures reported `top_accept_rate: 1.0`."""
    recorder = ActionRecorder(actions_dir)
    shown = (SuggestionShown(path="projects/blog", score=3.25, rank=1),)
    recorder.record(
        make_record(
            id="act_ok",
            operation="move",
            context=ActionContext(suggestions_shown=shown, chosen_rank=1),
        )
    )
    for index in range(3):
        recorder.record(
            make_record(
                id=f"act_bad_{index}",
                operation="move",
                context=ActionContext(
                    suggestions_shown=shown,
                    chosen_rank=1,
                    partial_failure="archiving the original failed: EACCES",
                ),
            )
        )

    stats = ActionRecorder(actions_dir).stats()
    assert stats["total"] == 4
    assert stats["by_operation"] == {"move": 4}
    assert stats["partial_failures"] == 3
    assert stats["suggestions"] == {
        "with_suggestions": 1,
        "top_chosen": 1,
        "other_rank_chosen": 0,
        "none_chosen": 0,
        "top_accept_rate": 1.0,
        "rank_histogram": {1: 1},
    }


def test_stats_of_an_empty_corpus(actions_dir: Path) -> None:
    assert ActionRecorder(actions_dir).stats() == {
        "total": 0,
        "months": [],
        "first_ts": None,
        "last_ts": None,
        "by_operation": {},
        "by_actor": {},
        "by_route": {},
        "by_edit_mode": {},
        "partial_failures": 0,
        "suggestions": {
            "with_suggestions": 0,
            "top_chosen": 0,
            "other_rank_chosen": 0,
            "none_chosen": 0,
            "top_accept_rate": None,
            "rank_histogram": {},
        },
        "integrate": {
            "total": 0,
            "accepted": 0,
            "edited": 0,
            "rejected": 0,
            "accept_rate": None,
            "edit_rate": None,
            "reject_rate": None,
        },
    }


def test_stats_exact_values_over_a_known_corpus(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    shown = (
        SuggestionShown(path="projects/blog", score=3.25, rank=1),
        SuggestionShown(path="areas/health", score=1.5, rank=2),
    )
    recorder.record(
        make_record(
            id="act_1",
            ts="2026-07-10T09:00:00Z",
            operation="move",
            context=ActionContext(session_id="s1", suggestions_shown=shown, chosen_rank=1),
        )
    )
    recorder.record(
        make_record(
            id="act_2",
            ts="2026-07-11T09:00:00Z",
            operation="move",
            context=ActionContext(session_id="s1", suggestions_shown=shown, chosen_rank=2),
        )
    )
    recorder.record(
        make_record(
            id="act_3",
            ts="2026-07-12T09:00:00Z",
            operation="skip",
            context=ActionContext(session_id="s1", suggestions_shown=shown, chosen_rank=None),
        )
    )
    recorder.record(
        make_record(
            id="act_4",
            ts="2026-08-01T09:00:00Z",
            actor="claude-integrate",
            operation="integrate",
            edit_mode="integrate",
            context=ActionContext(route="workout"),
            llm=LLMTrace(
                backend="claude-cli",
                model="m",
                prompt_hash="p",
                proposed_diff="+a\n",
                final_diff="+b\n",
                verdict="edited",
            ),
        )
    )
    recorder.record(
        make_record(
            id="act_5",
            ts="2026-08-02T09:00:00Z",
            actor="route:workout",
            operation="append",
            edit_mode="append",
            context=ActionContext(route="workout"),
        )
    )
    recorder.record(
        make_record(
            id="act_6",
            ts="2026-08-03T09:00:00Z",
            actor="auto-organize",
            operation="integrate",
            edit_mode="integrate",
            llm=LLMTrace(
                backend="ollama",
                model="m",
                prompt_hash="p",
                proposed_diff="-x\n",
                final_diff="",
                verdict="rejected",
            ),
        )
    )

    assert recorder.stats() == {
        "total": 6,
        "months": ["2026-07", "2026-08"],
        "first_ts": "2026-07-10T09:00:00Z",
        "last_ts": "2026-08-03T09:00:00Z",
        "by_operation": {"move": 2, "skip": 1, "integrate": 2, "append": 1},
        "by_actor": {
            "matt": 3,
            "claude-integrate": 1,
            "route:workout": 1,
            "auto-organize": 1,
        },
        "by_route": {"workout": 2},
        "by_edit_mode": {"integrate": 2, "append": 1},
        "partial_failures": 0,
        "suggestions": {
            "with_suggestions": 3,
            "top_chosen": 1,
            "other_rank_chosen": 1,
            "none_chosen": 1,
            "top_accept_rate": 1 / 3,
            "rank_histogram": {1: 1, 2: 1},
        },
        "integrate": {
            "total": 2,
            "accepted": 0,
            "edited": 1,
            "rejected": 1,
            "accept_rate": 0.0,
            "edit_rate": 0.5,
            "reject_rate": 0.5,
        },
    }


def test_stats_top_accept_rate_is_one_when_the_engine_is_always_right(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)
    shown = (SuggestionShown(path="projects/blog", score=3.0, rank=1),)
    for i in range(4):
        recorder.record(
            make_record(id=f"act_{i}", context=ActionContext(suggestions_shown=shown, chosen_rank=1))
        )
    stats = recorder.stats()
    assert stats["suggestions"]["top_accept_rate"] == 1.0
    assert stats["suggestions"]["rank_histogram"] == {1: 4}
