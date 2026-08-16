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

Two spec resolutions this module makes explicit:

* **Skip is per-session, and auto-advance honours it.** 03 §2 says ``skip``
  marks the capture "skipped for this session"; 03 §6 says skip has no file
  effect and no terminal outcome. So ``advance_to_next_unprocessed`` treats
  a capture as "done for now" when it is processed OR skipped — otherwise
  skipping would hand the same note straight back and the session could
  never finish. Skips are not persisted: the note is offered again in the
  next session.
* **Everything but ``counts``/``close`` requires ACTIVE.** Navigation or
  bookkeeping on an IDLE (never started) or CLOSED session raises
  ``SessionError`` rather than silently no-op'ing (09 §1.5). ``close`` is
  idempotent because ``WinClosed`` and ``stop`` legitimately race (03 §3),
  and ``counts`` stays readable after close so the client can render the
  completion message.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from organize_core.errors import SessionError
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


#: The three outcomes that finish a capture for good (03 §6). ``SKIPPED`` is
#: deliberately absent: it is a this-session-only marker.
TERMINAL_OUTCOMES: frozenset[Outcome] = frozenset(
    {Outcome.MOVED, Outcome.MERGED, Outcome.ARCHIVED}
)


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
        self._require_active("current")
        return self._at(self.current_index)

    def next(self) -> NoteRecord | None:
        """Advance; clamp at the end (returns current, caller notifies)."""
        self._require_active("next")
        if self.captures and self.current_index < len(self.captures) - 1:
            self.current_index += 1
        return self._at(self.current_index)

    def prev(self) -> NoteRecord | None:
        self._require_active("prev")
        if self.captures and self.current_index > 0:
            self.current_index -= 1
        return self._at(self.current_index)

    def skip(self) -> NoteRecord | None:
        """Mark current skipped for this session; advance (spec 03 §2)."""
        self._require_active("skip")
        record = self._at(self.current_index)
        if record is None:
            return None
        if record.path not in self.processed:
            self.skipped.add(record.path)
        return self.next()

    # --- outcome bookkeeping (spec 03 §6) --------------------------------

    def mark_processed(self, path: str, outcome: Outcome) -> None:
        """Record a terminal outcome; auto-advance happens via
        :meth:`advance_to_next_unprocessed`. Raises SessionError when the
        session is not ACTIVE."""
        self._require_active("mark_processed")
        if not isinstance(outcome, Outcome):
            raise SessionError(
                f"unknown session outcome {outcome!r}",
                hint="use one of: " + ", ".join(item.value for item in Outcome),
            )
        key = str(path)
        if key not in {record.path for record in self.captures}:
            raise SessionError(
                f"{key} is not part of session {self.session_id}",
                hint="mark_processed only accepts paths from this session's capture list",
            )
        if outcome not in TERMINAL_OUTCOMES:
            # SKIPPED is not terminal (03 §6): same bookkeeping as skip(),
            # without advancing the cursor.
            if key not in self.processed:
                self.skipped.add(key)
            return
        self.processed[key] = outcome
        self.skipped.discard(key)

    def advance_to_next_unprocessed(self) -> NoteRecord | None:
        """Next capture without a terminal outcome; None ⇒ exhausted —
        caller shows the completion message with counts and closes
        (spec 03 §6)."""
        self._require_active("advance_to_next_unprocessed")
        total = len(self.captures)
        if total == 0:
            return None
        # Forward from the current position first (the natural reading
        # order), then wrap once so captures left behind by prev()/next()
        # navigation are not stranded.
        order = list(range(self.current_index, total)) + list(range(0, self.current_index))
        for position in order:
            record = self.captures[position]
            if record.path in self.processed or record.path in self.skipped:
                continue
            self.current_index = position
            return record
        return None

    def counts(self) -> SessionCounts:
        processed = len(self.processed)
        skipped = len({path for path in self.skipped if path not in self.processed})
        remaining = max(len(self.captures) - processed - skipped, 0)
        return SessionCounts(processed=processed, skipped=skipped, remaining=remaining)

    def close(self) -> SessionCounts:
        """ACTIVE → CLOSED; idempotent close is allowed (WinClosed and
        ``stop`` may race — 03 §3 teardown)."""
        self.state = SessionState.CLOSED
        return self.counts()

    # --- internals -------------------------------------------------------

    def _at(self, position: int) -> NoteRecord | None:
        if not self.captures:
            return None
        if position < 0 or position >= len(self.captures):
            return None
        return self.captures[position]

    def _require_active(self, action: str) -> None:
        if self.state is SessionState.ACTIVE:
            return
        raise SessionError(
            f"cannot {action}: session {self.session_id} is {self.state.value}",
            hint="start a new session (:ParaOrganize start / organize session start)",
        )


def start_session(index: VaultIndex, filters: QueryCriteria | None = None) -> Session:
    """Build the capture list per filters (default ``status=raw`` +
    para_type ``capture``), ordered per module docstring. Zero matches ⇒
    returns an ACTIVE session with an empty list — the CLIENT decides how
    to notify ("No captures found matching filters", 03 §2) — clients never
    interpret an exception for a non-error."""
    criteria = filters if filters is not None else default_filters()
    captures = index.query(criteria)
    return Session(
        session_id=new_session_id(),
        filters=criteria,
        captures=list(captures),
        current_index=0,
        state=SessionState.ACTIVE,
    )


def default_filters() -> QueryCriteria:
    """``status=raw`` restricted to the capture folder (spec 03 §2)."""
    return QueryCriteria(status=["raw"], para_type=["capture"])


def new_session_id(*, now: float | None = None) -> str:
    """Unique session id (feeds ActionRecord.context.session_id, 12 §2)."""
    moment = time.time() if now is None else float(now)
    stamp = datetime.fromtimestamp(moment, tz=UTC).strftime("%Y%m%dT%H%M%S")
    return f"ses_{stamp}_{secrets.token_hex(3)}"
