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
from typing import TYPE_CHECKING, Any, ClassVar

from organize_core.config import Config, ConsumerConfig
from organize_core.frontmatter import Frontmatter, fields_are_no_ai
from organize_core.llm import LLMClient
from organize_core.paths import CorePaths

if TYPE_CHECKING:
    # Type-only: `fileops` imports the consumer framework nowhere, and a
    # runtime import here would put the mutation layer under every consumer.
    from organize_core.fileops import OperationContext


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
    #: Vault-relative posix path, as derived during INGESTION from the
    #: configured ``vault.scan_dirs`` prefix plus the walk-relative subpath.
    #:
    #: Carried on the payload rather than re-derived from ``path`` because
    #: ``path`` is symlink-RESOLVED: a scan dir symlinked outside the vault
    #: root resolves to an absolute path that ``relative_to(root)`` cannot
    #: express, and the old fallback (the absolute path) can never match a
    #: relative ``include_paths`` prefix — so every note under such a scan
    #: dir was silently counted ``filtered`` and never handled (08 §B18).
    #: Empty only for payloads built by hand in tests; the runner falls back
    #: to ``relative_to`` and WARNs when it cannot derive one.
    rel: str = ""

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

    #: The RECORDED write path (spec 12 §2) for a consumer that touches the
    #: vault. Populated by ``run_consumers`` from the composition root's
    #: single context, handed to each consumer with
    #: ``actor="consumer:<type>"``. ``None`` only in pure-logic unit tests
    #: that build a context by hand.
    #:
    #: A consumer that would write the vault with this ``None`` MUST emit
    #: ``Status.ERROR`` instead of writing. Becoming a second, UNRECORDED
    #: write path is the one thing doc 12 exists to prevent, and an error is
    #: retried next run — so the pipeline self-heals once the seam is wired
    #: instead of leaving an untraceable mutation behind. A field, not a
    #: factory: the composition root owns the lifecycle of what it carries
    #: (index, oplog, recorder), never a consumer.
    op_context: OperationContext | None = None


class Consumer(ABC):
    """One consumer type. Subclasses register with ``@register("name")``.

    Two DIFFERENT LLM questions, deliberately kept apart:

    - ``uses_llm`` (class flag) — "is this consumer's whole job an LLM
      call?" It drives the runner's central ``no-ai`` DENIAL: a
      ``no-ai: true`` note is never even offered to such a consumer (spec
      02 vault law / 06 §2).
    - :meth:`wants_llm` (instance predicate) — "does this INSTANCE need the
      run's shared LLM client on its ``RunContext``?" It drives CLIENT
      INJECTION only.

    They differ for taskwarrior: it creates a task for EVERY matching note
    (so a ``no-ai`` capture must still become a task — ARCHITECTURE
    resolution #12), but with ``llm_enabled = true`` it needs a client for
    the optional enrichment branch, which guards ``no-ai`` itself. Keying
    injection on the class flag alone made ``llm_enabled`` inert in
    production: ``ctx.llm`` was always ``None`` and the 06 §3.1 enrichment
    path could never execute through the pipeline.
    """

    uses_llm: ClassVar[bool] = False

    #: False for a type that is REGISTERED (so the registry and config
    #: schema are stable) but whose bodies land in a later phase. Config
    #: validation refuses such a type by name, and ``--list-consumers``
    #: marks it — otherwise enabling one produces an ERROR per scanned note
    #: (7.5k on the real vault) and exit 1 forever, instead of one legible
    #: fail-fast (08 §B2/§B14).
    implemented: ClassVar[bool] = True

    def wants_llm(self, config: Config | None = None) -> bool:
        """Does this instance need ``RunContext.llm``? Default: whatever
        ``uses_llm`` says. Override when an OPTION (not the type) decides —
        see ``TaskwarriorConsumer.wants_llm``. Must be pure and cheap: the
        runner calls it once per consumer per run.

        ``config`` is the GLOBAL :class:`~organize_core.config.Config`, passed
        by the runner. It is here because the runner must answer this question
        one line BEFORE it builds the ``RunContext`` (the answer decides
        whether that context gets a client at all) and before ``bind(ctx)``
        runs — so at the moment of asking, neither the context nor
        ``ConsumerConfig`` (which carries only this consumer's own options) can
        reach settings that live elsewhere in the file.

        ``tag_router`` is the case that forced it: whether an unattended route
        will INTEGRATE (doc 12 §1, the one route mode that calls an LLM) is a
        property of ``config.routes``, not of ``[consumers.tag_router]``.
        Without this parameter the predicate answered ``False`` for a legal
        config and the route ran against ``ctx.llm = None`` — the same
        inert-in-production failure ``taskwarrior.llm_enabled`` had.

        OPTIONAL and defaulted, so this is a pure WIDENING: every existing
        zero-argument override and call site keeps working, and the runner
        keeps its try/except fallback to the class flag for an override that
        never grew the parameter.
        """
        return bool(type(self).uses_llm)

    def bind(self, ctx: RunContext) -> None:
        """Once-per-run hook, called by the runner immediately after the
        ``RunContext`` is built and BEFORE any ``should_process`` call.
        Default: no-op.

        WHY IT EXISTS: ``should_process`` receives only a ``NotePayload``,
        so a consumer whose filter question is about CONFIG — "does this
        note match an ``auto = true`` route?" — cannot answer it. Deciding
        in ``handle`` instead would write a TERMINAL checkpoint for every
        non-matching note, and a route added later would then never fire on
        an already-seen note at an unchanged hash (the 08 §B3/§B4 class).
        A filter miss is never persisted, so the answer is re-derived every
        run and config changes stay retroactive (06 §1).

        CHEAPNESS CONTRACT: binding grants CONFIG READS to the predicate and
        nothing more. ``should_process`` stays cheap — path, already-parsed
        frontmatter, pure config predicates; no I/O, no LLM. Expensive
        services belong on the first real ``handle``, not here.

        FAILURE SEMANTICS: an implementation that RAISES means the consumer
        is SKIPPED for the run, counted as an error in the 06 §4 summary,
        and the run exits 1 — never "continue unbound", because an unbound
        filter silently drops every note, which is the silent-outage class.
        Other consumers are unaffected.
        """
        return None

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
    """Every registered name → class, INCLUDING types whose bodies land in a
    later phase (``implemented = False``). Used by ``--list-consumers``,
    which must not CONSTRUCT anything (06 §4)."""
    return dict(_REGISTRY)


def get_implemented_consumer_types() -> dict[str, type[Consumer]]:
    """Registered types a config may actually name. Config validation uses
    THIS, not :func:`get_consumer_types`: accepting a Phase-4 stub as a
    valid ``type`` turned one unimplemented consumer into an error per
    scanned note plus a permanently failing unit (08 §B2/§B14)."""
    return {name: cls for name, cls in _REGISTRY.items() if getattr(cls, "implemented", True)}
