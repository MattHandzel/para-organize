"""Organizing-session state machine (spec 03 §2/§6, 09 §2).

Every transition is asserted, including the ones the old plugin got wrong or
never had: clamping at both ends, skip-then-advance, auto-advance past
processed AND skipped captures, exhaustion, idempotent close, and the refusal
of every operation once the session is closed.

Ambiguity resolution #9 (ARCHITECTURE): zero matches is NOT an error — the
session is ACTIVE with an empty capture list and the client renders the "No
captures found matching filters" notice (03 §2).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from organize_core.config import Config, VaultConfig
from organize_core.errors import SessionError
from organize_core.index import NoteRecord, QueryCriteria, VaultIndex
from organize_core.session import (
    Outcome,
    Session,
    SessionCounts,
    SessionState,
    default_filters,
    new_session_id,
    start_session,
)

FIXTURE_RAW_CAPTURES = 11


def make_index(vault: Path, tmp_path: Path) -> VaultIndex:
    index = VaultIndex(Config(vault=VaultConfig(root=vault)), tmp_path / "index.json")
    index.load()
    index.scan()
    return index


def write_note(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def build_ordered_vault(root: Path, count: int = 4) -> Path:
    """A capture folder with ``count`` unambiguously ordered raw captures."""
    for n in range(count):
        write_note(
            root,
            f"capture/raw_capture/note-{n}.md",
            f"---\ntimestamp: '2026-0{n + 1}-01T00:00:00+00:00'\n"
            f"id: note-{n}\ntags:\n- t{n}\nprocessing_status: raw\n---\nbody {n}\n",
        )
    return root


@pytest.fixture()
def ordered_index(tmp_path: Path) -> VaultIndex:
    vault = build_ordered_vault(tmp_path / "ordered")
    return make_index(vault, tmp_path)


@pytest.fixture()
def session(ordered_index: VaultIndex) -> Session:
    return start_session(ordered_index)


def names(records: list[NoteRecord]) -> list[str]:
    return [record.filename for record in records]


# ---------------------------------------------------------------------------
# start (idle → active)
# ---------------------------------------------------------------------------


def test_start_session_uses_the_default_filters(fixture_vault: Path, tmp_path: Path) -> None:
    index = make_index(fixture_vault, tmp_path)
    session = start_session(index)

    assert session.state is SessionState.ACTIVE
    assert session.filters == QueryCriteria(status=["raw"], para_type=["capture"])
    assert len(session.captures) == FIXTURE_RAW_CAPTURES
    assert {record.para_type for record in session.captures} == {"capture"}
    assert {record.processing_status for record in session.captures} == {"raw"}


def test_start_session_orders_captures_oldest_first(session: Session) -> None:
    assert names(session.captures) == ["note-0.md", "note-1.md", "note-2.md", "note-3.md"]
    current = session.current()
    assert current is not None and current.filename == "note-0.md"


def test_start_session_honours_explicit_filters(ordered_index: VaultIndex) -> None:
    session = start_session(ordered_index, QueryCriteria(tags=["t2"]))
    assert names(session.captures) == ["note-2.md"]
    assert session.filters.tags == ["t2"]


def test_zero_matches_is_an_active_empty_session(ordered_index: VaultIndex) -> None:
    """ARCHITECTURE resolution #9 — a non-error is never an exception."""
    session = start_session(ordered_index, QueryCriteria(tags=["nothing-matches-this"]))

    assert session.state is SessionState.ACTIVE
    assert session.captures == []
    assert session.current() is None
    assert session.next() is None
    assert session.prev() is None
    assert session.skip() is None
    assert session.advance_to_next_unprocessed() is None
    assert session.counts() == SessionCounts(processed=0, skipped=0, remaining=0)


def test_each_session_gets_a_unique_id(ordered_index: VaultIndex) -> None:
    first = start_session(ordered_index)
    second = start_session(ordered_index)
    assert first.session_id != second.session_id


def test_new_session_id_shape_and_uniqueness() -> None:
    stamped = new_session_id(now=1_760_000_000.0)
    assert re.fullmatch(r"ses_\d{8}T\d{6}_[0-9a-f]{6}", stamped)
    assert stamped.startswith("ses_20251009T")  # UTC, injected clock
    assert new_session_id(now=1_760_000_000.0) != stamped  # random suffix


def test_default_filters_is_status_raw_in_the_capture_folder() -> None:
    assert default_filters() == QueryCriteria(status=["raw"], para_type=["capture"])


# ---------------------------------------------------------------------------
# navigation (03 §2: clamp at the ends)
# ---------------------------------------------------------------------------


def test_next_advances_and_clamps_at_the_end(session: Session) -> None:
    assert session.next().filename == "note-1.md"
    assert session.next().filename == "note-2.md"
    assert session.next().filename == "note-3.md"
    assert session.next().filename == "note-3.md"  # clamped, caller notifies
    assert session.current_index == 3


def test_prev_goes_back_and_clamps_at_the_start(session: Session) -> None:
    session.next()
    session.next()
    assert session.prev().filename == "note-1.md"
    assert session.prev().filename == "note-0.md"
    assert session.prev().filename == "note-0.md"  # clamped
    assert session.current_index == 0


def test_current_is_stable_between_calls(session: Session) -> None:
    assert session.current() is session.current()


# ---------------------------------------------------------------------------
# skip (03 §2) and outcomes (03 §6)
# ---------------------------------------------------------------------------


def test_skip_marks_the_capture_and_advances(session: Session) -> None:
    first = session.current()
    assert first is not None
    following = session.skip()

    assert following is not None and following.filename == "note-1.md"
    assert session.skipped == {first.path}
    assert session.counts() == SessionCounts(processed=0, skipped=1, remaining=3)


def test_skip_at_the_end_still_records_the_skip(session: Session) -> None:
    session.next()
    session.next()
    session.next()
    last = session.current()
    assert last is not None
    assert session.skip() is last  # clamped, but the skip was recorded
    assert last.path in session.skipped


def test_mark_processed_records_a_terminal_outcome(session: Session) -> None:
    current = session.current()
    assert current is not None
    session.mark_processed(current.path, Outcome.MOVED)

    assert session.processed == {current.path: Outcome.MOVED}
    assert session.counts() == SessionCounts(processed=1, skipped=0, remaining=3)


def test_every_terminal_outcome_counts_as_processed(session: Session) -> None:
    for record, outcome in zip(
        session.captures, (Outcome.MOVED, Outcome.MERGED, Outcome.ARCHIVED), strict=False
    ):
        session.mark_processed(record.path, outcome)
    assert session.counts() == SessionCounts(processed=3, skipped=0, remaining=1)


def test_mark_processed_with_skipped_outcome_is_not_terminal(session: Session) -> None:
    current = session.current()
    assert current is not None
    session.mark_processed(current.path, Outcome.SKIPPED)

    assert session.processed == {}
    assert session.skipped == {current.path}
    assert session.counts() == SessionCounts(processed=0, skipped=1, remaining=3)


def test_processing_a_skipped_capture_promotes_it(session: Session) -> None:
    first = session.current()
    assert first is not None
    session.skip()
    session.mark_processed(first.path, Outcome.ARCHIVED)

    assert session.skipped == set()
    assert session.counts() == SessionCounts(processed=1, skipped=0, remaining=3)


def test_mark_processed_rejects_a_path_outside_the_session(session: Session) -> None:
    with pytest.raises(SessionError) as excinfo:
        session.mark_processed("/tmp/not-in-this-session.md", Outcome.MOVED)
    assert "not part of session" in str(excinfo.value)


def test_mark_processed_rejects_a_non_outcome(session: Session) -> None:
    current = session.current()
    assert current is not None
    with pytest.raises(SessionError):
        session.mark_processed(current.path, "moved")  # type: ignore[arg-type]


def test_marking_the_same_capture_twice_is_not_double_counted(session: Session) -> None:
    current = session.current()
    assert current is not None
    session.mark_processed(current.path, Outcome.MOVED)
    session.mark_processed(current.path, Outcome.MERGED)
    assert session.processed == {current.path: Outcome.MERGED}
    assert session.counts().processed == 1


# ---------------------------------------------------------------------------
# auto-advance (03 §6)
# ---------------------------------------------------------------------------


def test_advance_skips_processed_and_skipped_captures(session: Session) -> None:
    first = session.current()
    assert first is not None
    session.mark_processed(first.path, Outcome.MOVED)
    second = session.advance_to_next_unprocessed()
    assert second is not None and second.filename == "note-1.md"

    session.skip()  # note-1 skipped, cursor now on note-2
    third = session.advance_to_next_unprocessed()
    assert third is not None and third.filename == "note-2.md"


def test_advance_wraps_to_captures_left_behind(session: Session) -> None:
    """prev()/next() navigation must not strand an unprocessed capture."""
    for record in session.captures[1:]:
        session.mark_processed(record.path, Outcome.MOVED)
    session.current_index = 3  # cursor parked at the end

    remaining = session.advance_to_next_unprocessed()
    assert remaining is not None and remaining.filename == "note-0.md"
    assert session.current_index == 0


def test_advance_returns_none_when_the_session_is_exhausted(session: Session) -> None:
    for record in session.captures:
        session.mark_processed(record.path, Outcome.MOVED)
    assert session.advance_to_next_unprocessed() is None
    assert session.counts() == SessionCounts(processed=4, skipped=0, remaining=0)


def test_exhaustion_via_skips_alone(session: Session) -> None:
    for _ in range(4):
        session.skip()
    assert session.advance_to_next_unprocessed() is None
    assert session.counts() == SessionCounts(processed=0, skipped=4, remaining=0)


def test_advance_keeps_returning_the_current_capture_when_nothing_changed(
    session: Session,
) -> None:
    current = session.current()
    assert session.advance_to_next_unprocessed() is current
    assert session.advance_to_next_unprocessed() is current


# ---------------------------------------------------------------------------
# close (active → closed)
# ---------------------------------------------------------------------------


def test_close_returns_counts_and_closes(session: Session) -> None:
    first = session.current()
    assert first is not None
    session.mark_processed(first.path, Outcome.MOVED)
    session.next()
    session.skip()

    counts = session.close()
    assert session.state is SessionState.CLOSED
    assert counts == SessionCounts(processed=1, skipped=1, remaining=2)


def test_close_is_idempotent(session: Session) -> None:
    """WinClosed and ``stop`` legitimately race (03 §3 teardown)."""
    first = session.close()
    second = session.close()
    assert first == second
    assert session.state is SessionState.CLOSED


def test_counts_remain_readable_after_close(session: Session) -> None:
    current = session.current()
    assert current is not None
    session.mark_processed(current.path, Outcome.ARCHIVED)
    session.close()
    assert session.counts() == SessionCounts(processed=1, skipped=0, remaining=3)


@pytest.mark.parametrize(
    "action",
    [
        lambda s: s.current(),
        lambda s: s.next(),
        lambda s: s.prev(),
        lambda s: s.skip(),
        lambda s: s.advance_to_next_unprocessed(),
        lambda s: s.mark_processed(s.captures[0].path, Outcome.MOVED),
    ],
)
def test_closed_sessions_refuse_every_operation(session: Session, action) -> None:
    session.close()
    with pytest.raises(SessionError) as excinfo:
        action(session)
    assert "closed" in str(excinfo.value)


@pytest.mark.parametrize(
    "action",
    [
        lambda s: s.current(),
        lambda s: s.next(),
        lambda s: s.skip(),
        lambda s: s.advance_to_next_unprocessed(),
    ],
)
def test_idle_sessions_refuse_every_operation(ordered_index: VaultIndex, action) -> None:
    idle = Session(
        session_id="ses_idle",
        filters=default_filters(),
        captures=[],
        state=SessionState.IDLE,
    )
    with pytest.raises(SessionError) as excinfo:
        action(idle)
    assert "idle" in str(excinfo.value)


def test_an_idle_session_can_still_be_torn_down(ordered_index: VaultIndex) -> None:
    idle = Session(session_id="ses_idle", filters=default_filters(), state=SessionState.IDLE)
    assert idle.close() == SessionCounts(processed=0, skipped=0, remaining=0)
    assert idle.state is SessionState.CLOSED


# ---------------------------------------------------------------------------
# end-to-end walk
# ---------------------------------------------------------------------------


def test_full_session_walk(ordered_index: VaultIndex) -> None:
    session = start_session(ordered_index)
    order: list[str] = []

    record = session.current()
    while record is not None:
        order.append(record.filename)
        if record.filename == "note-2.md":
            session.skip()
        else:
            session.mark_processed(record.path, Outcome.MOVED)
        record = session.advance_to_next_unprocessed()

    assert order == ["note-0.md", "note-1.md", "note-2.md", "note-3.md"]
    counts = session.close()
    assert counts == SessionCounts(processed=3, skipped=1, remaining=0)


def test_session_reflects_a_reindexed_vault(tmp_path: Path) -> None:
    """Sessions are built from the index, so a capture added after the scan
    only appears once the index knows about it (03 §2 + §7)."""
    vault = build_ordered_vault(tmp_path / "growing", count=2)
    index = make_index(vault, tmp_path)
    assert len(start_session(index).captures) == 2

    late = write_note(
        vault,
        "capture/raw_capture/note-late.md",
        "---\ntimestamp: '2026-09-01T00:00:00+00:00'\nprocessing_status: raw\n---\nlate\n",
    )
    assert len(start_session(index).captures) == 2
    index.update_file(late)
    assert names(start_session(index).captures) == ["note-0.md", "note-1.md", "note-late.md"]
