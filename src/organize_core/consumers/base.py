"""Consumer framework: payload, result, ABC, registry (spec 06 §1).

Orchestration law (spec 06 §1, the B2/B3/B4/B12/B13 fixes):
- Consumer CONSTRUCTORS ARE PURE — no I/O, no subprocesses, no network.
  External state (task export, tag list) is fetched lazily on first real
  work, once per run.
- CHECKPOINTING BELONGS TO THE ORCHESTRATOR ONLY (runner.py). Consumers
  return a ConsumerResult and never touch the store. Terminal statuses
  (success, skip) are checkpointed; error and limit are retried next run.
- Filter misses are NOT persisted — ``should_process`` is cheap and
  re-evaluated every run so config changes apply retroactively.
- ``max_notes_per_run`` counts SUCCESSES only.
- ``no-ai: true`` notes are excluded from all LLM-calling consumers
  (vault law, spec 02 / 06 §2) — enforced centrally by the runner via
  ``Consumer.uses_llm``.

SHARED FILE — only the architect/integrator edits this module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from organize_core.config import Config, ConsumerConfig
from organize_core.frontmatter import Frontmatter, fields_are_no_ai
from organize_core.llm import LLMClient
from organize_core.paths import CorePaths


@dataclass(frozen=True)
class NotePayload:
    """Immutable parsed note handed to consumers (spec 06 §1 ingestion).
    ``note_hash = sha256(raw_text)`` — THE idempotency key. ``path`` is
    absolute and symlink-resolved (06 §1: canonicalize identically to the
    live DB or all history orphans)."""

    path: Path
    frontmatter: dict[str, Any]
    content: str  # body only
    raw_text: str  # full file text
    note_hash: str

    @property
    def no_ai(self) -> bool:
        """``no-ai: true`` in frontmatter (vault law, spec 02).

        Delegates to the shared frontmatter predicate — ONE no-ai rule in
        the codebase. Ambiguous truthy values (the STRING ``"true"``) count
        as True: for a do-not-touch flag, over-matching is the safe error.
        """
        return fields_are_no_ai(self.frontmatter)

    def tags(self) -> list[str]:
        """Frontmatter tags coerced to a list via the shared frontmatter
        list coercion — NEVER a hand-rolled re-parse (08 §B9).

        Scalar ⇒ one element, list ⇒ list, missing/None ⇒ ``[]``; values are
        stringified. RAW spellings, deliberately: vault-tag normalization
        (``frontmatter.normalize_tag``) exists to match tags to FOLDERS, and
        the taskwarrior consumer needs a different, domain-specific
        normalization (06 §3.1) — normalizing here would silently rewrite
        every underscored tag in Matt's task history.
        """
        fields = dict(self.frontmatter) if self.frontmatter else {}
        return [str(value) for value in Frontmatter(fields=fields).get_list("tags")]


class Status(Enum):
    """Emission statuses (spec 06 §1). success/skip are terminal
    (checkpointed); error/limit are retried next run."""

    SUCCESS = "success"
    SKIP = "skip"
    ERROR = "error"
    LIMIT = "limit"


@dataclass(frozen=True)
class ConsumerResult:
    status: Status
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunContext:
    """Per-run services the orchestrator provides to ``handle``. Keeps
    constructors pure while giving consumers their lazily-created deps."""

    config: Config
    dry_run: bool = False
    llm: LLMClient | None = None  # None when disabled/unavailable
    #: Resolved state/config/runtime locations (spec 10 §3). Populated by the
    #: runner from the composition root so a consumer that needs a STATE
    #: path — taskwarrior's ``<state>/backups/taskwarrior/<UTC-ts>`` snapshot
    #: (06 §3.1) — can derive it instead of guessing or reading the
    #: environment itself. ``None`` only in unit tests that build a context
    #: by hand.
    paths: CorePaths | None = None


class Consumer(ABC):
    """One consumer type. Subclasses register with ``@register("name")``.

    ``uses_llm = True`` subclasses are centrally denied ``no-ai`` notes by
    the runner (spec 06 §2)."""

    uses_llm: ClassVar[bool] = False

    def __init__(self, config: ConsumerConfig) -> None:
        """PURE — validate ``config.options`` against this type's schema
        (unknown option ⇒ ConfigError naming the key) and store fields.
        No I/O of any kind (spec 06 §1; 08 §B2 was the outage)."""
        self.config = config

    @abstractmethod
    def should_process(self, payload: NotePayload) -> bool:
        """Cheap predicate over path + already-parsed frontmatter
        (spec 06 §1). Re-evaluated every run; never persisted. Path
        include/exclude filtering is done by the RUNNER before this."""

    @abstractmethod
    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        """Do the work for one note. Must be idempotent-safe to retry
        (error/limit re-fire next run). Never raises for per-note failures
        — return Status.ERROR; raising is reserved for consumer-fatal
        conditions the runner catches and isolates (06 §1)."""


# --- registry (implemented: runs at import time; structural, not behavioral)

_REGISTRY: dict[str, type[Consumer]] = {}


def register(name: str):
    """Class decorator: ``@register("taskwarrior")``. Duplicate names are a
    programming error (raise at import)."""

    def deco(cls: type[Consumer]) -> type[Consumer]:
        if name in _REGISTRY:
            raise ValueError(f"duplicate consumer type registered: {name!r}")
        _REGISTRY[name] = cls
        return cls

    return deco


def get_consumer_types() -> dict[str, type[Consumer]]:
    """Registered name → class (used by config validation and
    ``--list-consumers``, which must not CONSTRUCT anything — 06 §4)."""
    return dict(_REGISTRY)
