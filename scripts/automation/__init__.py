"""Automation package for capture-driven workflows."""

from .config import AutomationConfig, ConsumerConfig, load_config
from .emitter import NoteEmitter
from .notes import NotePayload, iter_note_payloads
from .store import AutomationStore

__all__ = [
    "AutomationConfig",
    "ConsumerConfig",
    "AutomationStore",
    "NoteEmitter",
    "NotePayload",
    "iter_note_payloads",
    "load_config",
]
