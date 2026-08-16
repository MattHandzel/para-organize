"""Loud-failure error taxonomy (spec 09 §1.5, 01 "Success criteria" #4).

Every misconfiguration, missing folder, unparseable file, or backend failure
is surfaced as one of these — never silently swallowed. The three live
outages (wrong vault_dir, archives/archive mismatch, the 3-month pipeline
crash) were all silent-continue failures; this taxonomy is how the rewrite
makes that class impossible.

Conventions:
- Everything derives from ``OrganizeError`` so callers can catch the family.
- Messages must name the offending path/key/value — "bad config" is banned.
- Errors that a *user* fixes (config, vault layout) carry a ``hint`` with the
  concrete correction when one is known.

SHARED FILE — only the architect/integrator edits this module.
"""

from __future__ import annotations


class OrganizeError(Exception):
    """Base class for every organize-core failure.

    Spec 09 §1.5: log + surface, never silently continue with wrong behavior.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class ConfigError(OrganizeError):
    """Invalid, unknown, or missing configuration (spec 03 §1, 06 §2, 10 §3).

    Raised for: unknown config keys (every key honored or deleted — 03 §1),
    wrong leaf types/enums/ranges, missing required values (e.g. LLM hosts,
    06 §2), and metadata-field keymap collisions (07 acceptance test 4).
    Message must name the exact key.
    """


class RouteConfigError(ConfigError):
    """A ``[[routes]]`` entry is invalid (spec 11 §1): bad mode/destination
    combination (folders get move; files get append/integrate), unknown keys,
    or an unparseable tag expression."""


class VaultError(OrganizeError):
    """The vault does not match configuration (spec 02, 08 §C).

    Raised when ``vault.root``, the capture folder, or a configured PARA
    folder does not exist / is not writable — the class of check that would
    have caught both live misconfigurations (``~/Obsidian/Main/notes`` and
    ``archives`` vs ``archive``)."""


class FrontmatterError(OrganizeError):
    """Frontmatter could not be parsed or serialized (spec 03 §8, 02).

    NOTE the tolerance contract: vault *scans* never crash on a bad file —
    the indexer catches this, logs, and indexes with empty metadata (03 §7).
    Mutating operations, by contrast, MUST refuse to rewrite a file whose
    frontmatter they cannot round-trip (05 invariants)."""


class IndexingError(OrganizeError):
    """Index persistence is corrupt/unreadable or an incremental update
    failed (spec 03 §7). A corrupt snapshot degrades to a rebuild, loudly.
    (Named IndexingError to avoid shadowing the builtin IndexError.)"""


class OperationError(OrganizeError):
    """A file operation failed (spec 05).

    Per invariant 05 §1.2 the original is never left missing: any raiser of
    this error guarantees the source file still exists (a duplicate copy may
    exist — acceptable; data loss is not)."""


class ConcurrentModificationError(OperationError):
    """A file changed on disk (mtime/hash) between read and mutate
    (spec 10 §4). The core refuses with a clear error rather than clobbering
    — the vault is Syncthing-synced and can change under us at any time."""


class NoAiRefusal(OperationError):
    """Target note carries ``no-ai: true`` and an automated/LLM path tried to
    write it (vault law, spec 02; 12 §1 `integrate` refusal; 06 §2)."""


class IntegrationRejected(OperationError):
    """The `integrate` deletion guard fired (spec 12 §1): the LLM proposal
    deletes existing non-whitespace lines beyond the configured threshold.
    Target untouched; recorded with ``verdict: "rejected"``."""


class LearningDataError(OrganizeError):
    """learning.json is malformed (spec 04 §3).

    NOTE: loading degrades to empty data and must never crash scoring —
    this error is for *explicit* import/export paths, not the load path."""


class StoreError(OrganizeError):
    """automations.db (SQLite) failure (spec 06 §1)."""


class StoreMigrationError(StoreError):
    """The live automations.db could not be migrated (spec 06 §1). The 15 MB
    DB must be migrated, not discarded — a year of emission history is the
    idempotency contract. Migration aborts loudly; never runs consumers
    against a half-migrated store."""


class ConsumerError(OrganizeError):
    """A consumer failed for one note (spec 06 §1). Never prevents other
    consumers from running; counted and reported, run exits nonzero."""


class LLMError(OrganizeError):
    """An LLM backend call failed (spec 06 §6, 09 §2): timeout, empty or
    unparseable response, HTTP error. LLM backends are optional & flaky by
    contract — callers degrade gracefully (e.g. task created unenriched)."""


class LLMUnavailable(LLMError):
    """The configured backend is unreachable/not installed (Ollama server
    down, ``claude`` CLI absent)."""


class ServerError(OrganizeError):
    """JSON-RPC server-side failure (spec 10 §1-2)."""


class AlreadyRunning(ServerError):
    """The single-instance lock is held by another ``organize serve``
    (spec 10 §1)."""


class CoreUnavailable(OrganizeError):
    """A client could not reach the core, or the ``{"apiVersion": ...}``
    handshake reported an incompatible major version (spec 10 §2). Clients
    degrade gracefully with this clear error, no crash."""


class SessionError(OrganizeError):
    """Invalid session-state transition or an operation that requires an
    active session without one (spec 03 §2, 09 §2)."""
