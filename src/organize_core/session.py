"""Explicit organizing-session state machine (spec 03 §2/§6, 09 §2).

    idle ──start(filters)──▶ active{captures, index, current,
                                    processed, skipped} ──close──▶ closed

Driven via the API (server methods / CLI ``session``); the Neovim client
renders it but owns none of it (spec 10 §1-2). Every exit path tears down
cleanly (09 §2); invalid transitions raise SessionError.

Capture ordering (spec 03 §2): oldest first by ``timestamp`` (fallback file
mtime), deterministic tiebreak path ascending. Default filters when none
given: ``status=raw`` restricted to the capture folder (para_type
``capture``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from organize_core.index import NoteRecord, QueryCriteria, VaultIndex


class SessionState(Enum):
    IDLE = "idle"
    ACTIVE = "active"
    CLOSED = "closed"


class Outcome(Enum):
    """Per-capture terminal outcomes (spec 03 §6). A capture is done when
    moved, merged, or archived; skip leaves it for a later session."""

    MOVED = "moved"
    MERGED = "merged"
    ARCHIVED = "archived"
    SKIPPED = "skipped"


@dataclass
class SessionCounts:
    processed: int = 0
    skipped: int = 0
    remaining: int = 0


@dataclass
class Session:
    """One organizing session. Constructed by :func:`start_session`."""

    session_id: str
    filters: QueryCriteria
    captures: list[NoteRecord] = field(default_factory=list)
    current_index: int = 0
    processed: dict[str, Outcome] = field(default_factory=dict)  # path → outcome
    skipped: set[str] = field(default_factory=set)
    state: SessionState = SessionState.ACTIVE

    # --- navigation (spec 03 §2 next/prev clamp at ends) -----------------

    def current(self) -> NoteRecord | None:
        raise NotImplementedError

    def next(self) -> NoteRecord | None:
        """Advance; clamp at the end (returns current, caller notifies)."""
        raise NotImplementedError

    def prev(self) -> NoteRecord | None:
        raise NotImplementedError

    def skip(self) -> NoteRecord | None:
        """Mark current skipped for this session; advance (spec 03 §2)."""
        raise NotImplementedError

    # --- outcome bookkeeping (spec 03 §6) --------------------------------

    def mark_processed(self, path: str, outcome: Outcome) -> None:
        """Record a terminal outcome; auto-advance happens via
        :meth:`advance_to_next_unprocessed`. Raises SessionError when the
        session is not ACTIVE."""
        raise NotImplementedError

    def advance_to_next_unprocessed(self) -> NoteRecord | None:
        """Next capture without a terminal outcome; None ⇒ exhausted —
        caller shows the completion message with counts and closes
        (spec 03 §6)."""
        raise NotImplementedError

    def counts(self) -> SessionCounts:
        raise NotImplementedError

    def close(self) -> SessionCounts:
        """ACTIVE → CLOSED; idempotent close is allowed (WinClosed and
        ``stop`` may race — 03 §3 teardown)."""
        raise NotImplementedError


def start_session(index: VaultIndex, filters: QueryCriteria | None = None) -> Session:
    """Build the capture list per filters (default ``status=raw`` +
    para_type ``capture``), ordered per module docstring. Zero matches ⇒
    returns an ACTIVE session with an empty list — the CLIENT decides how
    to notify ("No captures found matching filters", 03 §2) — clients never
    interpret an exception for a non-error."""
    raise NotImplementedError


def new_session_id(*, now: float | None = None) -> str:
    """Unique session id (feeds ActionRecord.context.session_id, 12 §2)."""
    raise NotImplementedError
