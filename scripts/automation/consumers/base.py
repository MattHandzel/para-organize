"""Base classes and utilities for automation consumers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import AutomationConfig, ConsumerConfig
from ..emitter import NoteState
from ..notes import NotePayload
from ..store import AutomationStore

LOG = logging.getLogger("automation.consumers")


@dataclass(slots=True)
class ConsumerResult:
    """Outcome of processing a note."""

    status: str  # "success", "skip", "error"
    note_path: Path
    message: str = ""
    metadata: Optional[Dict[str, Any]] = None

    def to_status_kwargs(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "metadata": self.metadata or {},
        }


class Consumer:
    """Interface implemented by concrete consumers."""

    def __init__(
        self,
        config: ConsumerConfig,
        global_config: AutomationConfig,
    ) -> None:
        self.name = config.name
        self.config = config
        self.global_config = global_config

    def matches(self, state: NoteState) -> bool:
        """Return True when the consumer wants to inspect this note."""
        return True

    def handle(self, state: NoteState, store: AutomationStore) -> ConsumerResult:
        """Process a note and return a result."""
        raise NotImplementedError

    def log(self, level: int, message: str, *args: object, **kwargs: object) -> None:
        LOG.log(level, "[%s] " + message, self.name, *args, **kwargs)
