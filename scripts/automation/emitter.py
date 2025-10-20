"""Emitter that diff capture notes and notifies consumers of changes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence

from .notes import NotePayload
from .store import AutomationStore


@dataclass(slots=True)
class NoteState:
    """State wrapper describing how a note changed since the last run."""

    note: NotePayload
    previous_hash: Optional[str]

    @property
    def is_new(self) -> bool:
        return self.previous_hash is None

    @property
    def changed(self) -> bool:
        return self.previous_hash != self.note.note_hash


class NoteEmitter:
    """Synchronise notes into the store and enumerate updates for consumers."""

    def __init__(self, store: AutomationStore) -> None:
        self._store = store

    def refresh(self, payloads: Iterable[NotePayload]) -> List[NoteState]:
        """
        Upsert note payloads into the database and return change metadata.

        Args:
            payloads: Iterable of `NotePayload` records.
        Returns:
            List of `NoteState` objects describing previous hashes.
        """
        states: List[NoteState] = []
        seen_paths: List[Path] = []
        for payload in payloads:
            previous = self._store.upsert_note(payload)
            states.append(NoteState(note=payload, previous_hash=previous))
            seen_paths.append(payload.path)
        self._store.purge_missing(seen_paths)
        return states

    def pending_for_consumer(
        self,
        consumer_name: str,
        states: Iterable[NoteState],
    ) -> Iterator[NoteState]:
        """
        Yield note states that a consumer needs to process based on hashes.

        The caller is still responsible for applying consumer-specific filters
        (e.g., tags, modalities).
        """
        for state in states:
            if self._store.needs_emission(
                consumer_name,
                state.note.path,
                state.note.note_hash,
            ):
                yield state
