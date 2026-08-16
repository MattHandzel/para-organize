"""The consumer orchestrator: ingestion → emission → checkpointing
(spec 06 §1; invoked by ``organize run-consumers``, spec 10 §1).

Owns ALL checkpointing (single-owner rule, 08 §B12 fix) and all the
orchestration law documented in base.py. Performance gate: full-vault run,
7.5k notes, no LLM work < 30 s (spec 09 §4).

[Architect note: this module is not in doc 06's file list by name — the
orchestrator needed a home inside the fixed ``consumers/`` package and
base.py (shared) must stay framework-only. Decision recorded in
ARCHITECTURE.md.]
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from organize_core.config import Config
from organize_core.consumers.base import NotePayload
from organize_core.consumers.store import AutomationStore


@dataclass
class ConsumerSummary:
    """Per-consumer counts for the summary line
    ``Consumer X: success=N skip=N limit=N error=N filtered=N`` (06 §4 —
    ``filtered`` IS counted, 08 §B18)."""

    name: str
    success: int = 0
    skip: int = 0
    limit: int = 0
    error: int = 0
    filtered: int = 0


@dataclass
class RunSummary:
    """Structured per-run summary (spec 06 §6): counts per consumer +
    duration, emitted to stdout/journal. ``exit_code``: 0 clean, 1 any
    consumer error (06 §4)."""

    consumers: list[ConsumerSummary] = field(default_factory=list)
    duration_seconds: float = 0.0
    notes_scanned: int = 0
    exit_code: int = 0


def scan_notes(config: Config) -> Iterator[NotePayload]:
    """Ingestion (spec 06 §1): walk ``vault.scan_dirs``, parse ``*.md`` via
    THE shared frontmatter module into NotePayloads. Skips legacy daily
    notes ``\\d{4}-\\d{2}-\\d{2}\\.md`` (documented exclusion); per-file
    parse errors are logged and skipped, never abort the run; explicit
    ``encoding="utf-8", errors="replace"`` everywhere (06 §6)."""
    raise NotImplementedError


def run_consumers(
    config: Config,
    store: AutomationStore,
    *,
    only: list[str] | None = None,
    dry_run: bool = False,
) -> RunSummary:
    """One full run (spec 06 §1 orchestration rules):

    1. Instantiate ONLY the enabled/selected consumers (``only`` matches
       config section names case-insensitively; unknown ⇒ usage error, exit
       2 — 06 §4). Construction is pure; a consumer failing to construct is
       isolated: logged, counted as error, others still run.
    2. For each payload × consumer: path include/exclude filter, then
       ``should_process`` (both re-evaluated every run, never persisted),
       then the hash check (``store.needs_delivery``), then ``handle``.
    3. Checkpoint ONLY terminal results (success/skip). error/limit retried
       next run. ``max_notes_per_run`` counts successes only; notes past
       the cap yield LIMIT (not checkpointed).
    4. ``no-ai: true`` payloads never reach a ``uses_llm`` consumer (02).
    5. mark_seen + soft_purge (store), per its guard.
    6. ``dry_run`` (09 §5.6): full evaluation, no ``handle`` side effects,
       no checkpoints; summary says what would fire.
    """
    raise NotImplementedError
