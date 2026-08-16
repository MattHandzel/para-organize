"""``auto_tagger`` consumer: LLM-tag under-tagged captures (spec 11 §2).

Trigger (11 §2): fewer than ``min_tags`` (default 1) tags, or an explicit
``auto_tag: "pending"``; ``no-ai: true`` is never processed; a note already
auto-tagged at the current CONTENT hash is skipped.

Prompt inputs (11 §2, all three): the capture content truncated at
``max_chars``; the existing vault tag VOCABULARY (top N by frequency from
the index — kebab-case, recall over precision, the capture-app convention);
and **all route/folder descriptions**, so tagging aligns with where things
can actually go.

Output (11 §2 bullet 3, and the architect's Phase-4 correction of the seat
brief): machine tags are written to **BOTH** ``tags`` AND ``auto_tags``.
``tags`` is what routing and suggestion read — a tagger writing only
``auto_tags`` would be invisible to ``routes.resolve`` and doc 11's whole
pipeline (tag routing + auto-tagging) would never fire. ``auto_tags`` is the
provenance MIRROR: it records which of the note's tags the machine added, so
Matt's tags stay distinguishable and machine tags stay bulk-removable, and
so ``OperationContext.auto_tags_present`` can carry that provenance into
every ActionRecord (12 §2). The consent gate for the machine chain is the
per-route ``auto = false`` default, not an opt-out here.

Two design decisions this seat made where doc 11 is silent:

* **``auto_tag_hash`` (a BODY digest) accompanies ``auto_tag: done``.**
  11 §4's last acceptance item says the guard against re-adding a tag Matt
  deleted is "the hash check plus ``auto_tag: done``" — but the tagger's own
  write CHANGES the note's raw-text hash, so the runner's 06 §1 checkpoint
  cannot express "unchanged since I tagged it", exactly as the ``learn``
  seat found for B8. The note therefore carries the digest of the BODY the
  model was shown. Our write only ever touches frontmatter, so the digest is
  stable across it and moves only when Matt edits the prose. Consequences,
  all pinned: a rerun is a no-op; deleting an auto tag from ``tags`` does
  not re-add it; editing the body DOES re-tag; and a ``done`` marker with NO
  digest — one this consumer never writes, so a hand-written "leave this
  alone" or a leftover from the previous-generation pipeline — is honored as
  final rather than read as stale, which is what stops the first run after
  cutover from re-tagging every such note in the vault. It is deliberately
  16 hex chars of a body-only sha256 so it can never be confused with the
  runner's full-length ``note_hash`` over the raw text.
* **``backend`` is ``[llm] backend``, not a consumer option.** 11 §2's
  "``backend = "ollama" | "claude-cli"`` in config" is satisfied by the ONE
  shared client (09 §2), which the runner injects as ``RunContext.llm``. A
  second client-construction path inside a consumer is the second door 09 §2
  exists to prevent.

Every vault write goes through ``fileops.update_frontmatter`` with the
``OperationContext`` the runner supplies (``RunContext.op_context``), so one
logical edit produces one operations-log line and one ActionRecord with
actor ``consumer:auto_tagger`` (12 §2). With no ``op_context`` the consumer
returns ``Status.ERROR`` rather than performing an unrecorded write —
errors are retried next run (06 §1), so the backlog self-heals.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from organize_core import fileops, frontmatter
from organize_core.config import Config, ConsumerConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    Status,
    register,
)
from organize_core.errors import ConfigError, LLMError, LLMUnavailable, OrganizeError
from organize_core.index import QueryCriteria

if TYPE_CHECKING:  # pragma: no cover - typing only
    from organize_core.fileops import OperationContext

LOG = logging.getLogger(__name__)

#: Frontmatter field holding the machine-added tags (11 §2). Mirrors a
#: SUBSET of ``tags`` — never a replacement for it.
AUTO_TAGS_FIELD = "auto_tags"
#: State marker (11 §2): ``"pending"`` requests tagging, ``"done"`` records it.
AUTO_TAG_FIELD = "auto_tag"
#: Companion body digest — see the module docstring.
AUTO_TAG_HASH_FIELD = "auto_tag_hash"
STATE_PENDING = "pending"
STATE_DONE = "done"

#: 11 §2 "kebab-case, recall over precision". A proposal that does not
#: survive :func:`frontmatter.normalize_tag` INTO this shape is dropped
#: (model punctuation, stray quoting); a malformed RESPONSE SHAPE is a
#: different thing and is an error — see :meth:`AutoTaggerConsumer.propose`.
_TAG_RE = re.compile(r"^[a-z0-9]+(?:[-/][a-z0-9]+)*$")

#: Cap on INDEX-derived folder descriptions in the prompt. Route and
#: ``[descriptions]`` entries are hand-authored by Matt and are never capped;
#: index-derived ones come from any note carrying a ``description`` field, so
#: they are unbounded in a real vault and would otherwise dominate the prompt.
_MAX_INDEX_DESCRIPTIONS = 40

SYSTEM_PROMPT = (
    "You are a tagging assistant for a personal knowledge vault. Choose tags "
    "for the note using the vault's existing tag vocabulary wherever one fits; "
    "invent a new tag only when nothing in the vocabulary applies. Tags are "
    "lowercase kebab-case. Prefer recall over precision. "
    'Reply with JSON only, in the form {"tags": ["tag-one", "tag-two"]}.'
)

_MISSING = object()


# --- option plumbing (same shape as the Phase-3 consumers) ------------------


def _as_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer, got {type(value).__name__}")
    if value < 1:
        raise ConfigError(f"{key} must be at least 1")
    return value


def _as_float(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number, got {type(value).__name__}")
    return float(value)


def _as_str_list(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings")
    return list(value)


def _coerce_options(
    consumer: ConsumerConfig,
    schema: Mapping[str, tuple[Any, Callable[[Any, str], Any]]],
) -> dict[str, Any]:
    """Validate ``consumer.options`` (spec 03 §1: every key honored or a loud
    failure NAMING it). Pure — no I/O (06 §1, the B2 outage)."""
    unknown = sorted(key for key in consumer.options if key not in schema)
    if unknown:
        raise ConfigError(
            f"consumers.{consumer.name}: unknown option(s) {', '.join(unknown)}",
            hint="valid options: " + ", ".join(sorted(schema)),
        )
    resolved: dict[str, Any] = {}
    for key, (default, coerce) in schema.items():
        raw = consumer.options.get(key, _MISSING)
        if raw is _MISSING:
            resolved[key] = default() if callable(default) else default
        else:
            resolved[key] = coerce(raw, f"consumers.{consumer.name}.{key}")
    return resolved


# --- small helpers ----------------------------------------------------------


def body_hash(content: str) -> str:
    """Digest of the note BODY — the idempotency key for auto-tagging.

    Deliberately NOT the runner's ``note_hash`` (which covers the raw text,
    frontmatter included, and therefore changes when this consumer writes).
    Truncated so the two can never be confused on sight.
    """
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]


def _field(payload: NotePayload, key: str) -> str:
    value = (payload.frontmatter or {}).get(key)
    return "" if value is None else str(value).strip().casefold()


def _normalize(value: str, extra_map: dict[str, str] | None) -> str:
    """The ONE shared normalizer (09 §2), after stripping the ``#`` prefix
    models like to emit. Never a second normalization rule."""
    return frontmatter.normalize_tag(str(value).strip().lstrip("#").strip(), extra_map)


def _relative(path: Path, root: Path) -> str:
    """Vault-relative POSIX path, tolerating symlinked scan dirs (08 §B18)."""
    for base, target in ((root, path), (root.resolve(), path.resolve())):
        try:
            return target.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def _described_label(path: Path, root: Path) -> str:
    """Spec 11 §3 storage: a folder's description lives in its
    ``index.md``/``<folder>.md``, so label those with the FOLDER."""
    if path.stem == "index" or path.stem == path.parent.name:
        return _relative(path.parent, root)
    return _relative(path, root)


@register("auto_tagger")
class AutoTaggerConsumer(Consumer):
    uses_llm = True  # the runner's central no-ai denial keys on this (02, 06 §2)

    OPTION_SCHEMA: dict[str, tuple[Any, Callable[[Any, str], Any]]] = {
        # 11 §2: "captures with fewer than min_tags (default 1) tags".
        "min_tags": (1, _as_int),
        "max_tags": (5, _as_int),
        "max_chars": (4000, _as_int),
        # "the existing vault tag vocabulary (top N by frequency…)".
        "vocabulary_size": (50, _as_int),
        # Configured candidate tags: always offered, never crowded out of the
        # top-N cap, for vocabulary a young vault does not have yet.
        "extra_vocabulary": (list, _as_str_list),
        "llm_timeout_seconds": (60.0, _as_float),
    }

    def __init__(self, config: ConsumerConfig) -> None:
        super().__init__(config)
        opts = _coerce_options(config, self.OPTION_SCHEMA)
        self.min_tags: int = opts["min_tags"]
        self.max_tags: int = opts["max_tags"]
        self.max_chars: int = opts["max_chars"]
        self.vocabulary_size: int = opts["vocabulary_size"]
        self.extra_vocabulary: list[str] = opts["extra_vocabulary"]
        self.llm_timeout_seconds: float = opts["llm_timeout_seconds"]
        # Grounding is derived from the whole index; it is fetched lazily on
        # first real work and reused for the rest of the run (06 §1). Set in
        # __init__ only as an attribute — no I/O, so the constructor stays pure.
        self._grounding_cache: tuple[int, tuple[list[str], list[str]]] | None = None

    # --- trigger (11 §2) --------------------------------------------------

    def should_process(self, payload: NotePayload) -> bool:
        if payload.path.suffix != ".md":
            return False
        # The runner denies no-ai centrally for every `uses_llm` consumer;
        # pinned here too, because a local guard is what makes the vault law
        # survive a refactor of the central one (02).
        if payload.no_ai:
            return False

        state = _field(payload, AUTO_TAG_FIELD)
        if state == STATE_PENDING:
            return True
        if state == STATE_DONE:
            recorded = _field(payload, AUTO_TAG_HASH_FIELD)
            if not recorded:
                # `auto_tag: done` with NO companion digest is a marker this
                # consumer never writes: a hand-written "leave this alone", or
                # a note the previous-generation pipeline marked. Reading a
                # missing digest as "stale" would re-tag every such note on the
                # first run after cutover — exactly the nagging 11 §4 forbids,
                # and at vault scale. Matt re-opts a note in with
                # `auto_tag: pending`, which 11 §2 defines for that purpose.
                return False
            # Re-tag only when the BODY changed since we last tagged it.
            # See the module docstring: this is what makes a rerun a no-op,
            # keeps a deleted auto tag deleted (11 §4), and still re-tags a
            # note Matt has since edited.
            return recorded != body_hash(payload.content)
        return len(payload.tags()) < self.min_tags

    # --- prompt inputs (11 §2) -------------------------------------------

    def vocabulary(self, op_ctx: OperationContext | None) -> list[str]:
        """The vault tag vocabulary: top ``vocabulary_size`` tags by
        FREQUENCY across the index (11 §2), then the configured
        ``extra_vocabulary``. Ties break on the tag itself so the prompt is
        deterministic. Normalized through the ONE normalizer (09 §2)."""
        extra_map = self._tag_map(op_ctx)
        counts: dict[str, int] = {}
        index = getattr(op_ctx, "index", None) if op_ctx is not None else None
        if index is not None:
            for record in index.query(QueryCriteria()):
                for tag in getattr(record, "tags", None) or []:
                    key = _normalize(str(tag), extra_map)
                    if _TAG_RE.match(key):
                        counts[key] = counts.get(key, 0) + 1

        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        out: list[str] = [tag for tag, _ in ranked[: self.vocabulary_size]]
        seen = set(out)
        for raw in self.extra_vocabulary:
            key = _normalize(raw, extra_map)
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def descriptions(self, config: Config, op_ctx: OperationContext | None) -> list[str]:
        """"**all route/folder descriptions**" (11 §2), as prompt lines.

        Routes first (they are the destinations a tag can actually route to,
        11 §1), then the central ``[descriptions]`` table, then descriptions
        the index loaded from folder index-notes (11 §3).
        """
        lines: list[str] = []
        seen: set[str] = set()

        for route in config.routes or []:
            text = (route.description or "").strip()
            if not text:
                continue
            tags = ", ".join(route.tags or [])
            label = route.destination
            seen.add(label.rstrip("/"))
            lines.append(f"- {label} (tags: {tags}) — {text}")

        for label, raw in sorted((config.descriptions or {}).items()):
            text = (raw or "").strip()
            key = str(label).rstrip("/")
            if not text or key in seen:
                continue
            seen.add(key)
            lines.append(f"- {label} — {text}")

        index = getattr(op_ctx, "index", None) if op_ctx is not None else None
        if index is None:
            return lines

        root = Path(config.vault.root)
        found: list[tuple[str, str]] = []
        for record in index.query(QueryCriteria()):
            text = (getattr(record, "description", None) or "").strip()
            if not text:
                continue
            label = _described_label(Path(record.path), root)
            if label in seen:
                continue
            seen.add(label)
            found.append((label, text))
        for label, text in sorted(found)[:_MAX_INDEX_DESCRIPTIONS]:
            lines.append(f"- {label} — {text}")
        return lines

    def build_prompt(
        self, payload: NotePayload, vocabulary: list[str], descriptions: list[str]
    ) -> str:
        """Assemble the 11 §2 prompt. Deterministic given its inputs."""
        content = (payload.content or "").strip()
        truncated = content[: self.max_chars]
        parts = [
            "Existing vault tags (most used first):",
            "\n".join(f"- {tag}" for tag in vocabulary) or "- (none yet)",
            "",
            "Where tagged notes can be routed:",
            "\n".join(descriptions) or "- (no route or folder descriptions configured)",
            "",
            "Note content:",
            truncated,
            "",
            f"Return at most {self.max_tags} lowercase kebab-case tags as "
            'JSON: {"tags": ["tag-one", "tag-two"]}',
        ]
        return "\n".join(parts)

    def propose(self, prompt: str, ctx: RunContext) -> list[str]:
        """One LLM call → the raw proposed tag strings.

        A malformed response SHAPE — not JSON, no ``tags`` key, ``tags`` not
        a list, or any non-string element — raises :class:`LLMError`, which
        the caller turns into an error emission with NO partial write. That
        is different from an individual proposal that fails to normalize into
        kebab-case: those are dropped (11 §2 "recall over precision"), since
        one punctuated word is not evidence the response was garbage.
        """
        if ctx.llm is None:
            raise LLMUnavailable(
                "no LLM client available — captures cannot be auto-tagged",
                hint="Configure [llm] (backend + host/model) or disable "
                f"consumers.{self.config.name}.",
            )
        response = ctx.llm.generate(
            prompt,
            system=SYSTEM_PROMPT,
            json_mode=True,
            temperature=0.3,
            max_tokens=256,
            timeout_seconds=self.llm_timeout_seconds,
        )
        payload = response.json
        if not isinstance(payload, dict):
            raise LLMError(
                "auto_tagger: the model did not return a JSON object",
                hint='Expected {"tags": ["tag-one", …]}. Nothing was written.',
            )
        raw = payload.get("tags", _MISSING)
        if raw is _MISSING or not isinstance(raw, list):
            raise LLMError(
                "auto_tagger: the model's JSON has no 'tags' list",
                hint='Expected {"tags": ["tag-one", …]}. Nothing was written.',
            )
        if any(not isinstance(item, str) for item in raw):
            raise LLMError(
                "auto_tagger: the model's 'tags' list contains a non-string entry",
                hint="Every tag must be a string. Nothing was written.",
            )
        return list(raw)

    def clean(self, proposed: list[str], config: Config) -> list[str]:
        """Normalize through the ONE normalizer, drop junk, dedupe, cap at
        ``max_tags``. Order follows the model's (its first pick is its most
        confident)."""
        extra_map = config.suggestions.tag_normalization
        out: list[str] = []
        seen: set[str] = set()
        for raw in proposed:
            key = _normalize(raw, extra_map)
            if not key or key in seen or not _TAG_RE.match(key):
                continue
            seen.add(key)
            out.append(key)
            if len(out) >= self.max_tags:
                break
        return out

    # --- orchestration ----------------------------------------------------

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        path = payload.path

        # Defence in depth behind the runner's central uses_llm guard (02).
        if payload.no_ai:
            LOG.warning("auto_tagger: refusing no-ai note %s", path)
            return ConsumerResult(Status.SKIP, "no-ai: true — LLM tooling refuses this note")

        if ctx.dry_run:
            # Same rule as the other LLM consumers: a rehearsal spends no
            # inference and writes nothing (09 §5.6). The runner does not even
            # call handle() on a dry run; this is the belt to that braces.
            return ConsumerResult(
                Status.SUCCESS, "would auto-tag this capture", {"dry_run": True}
            )

        op_ctx: OperationContext | None = getattr(ctx, "op_context", None)
        if op_ctx is None:
            # Refusing to become a second, UNRECORDED vault-write path is the
            # doc-12 discipline (architect ruling, Phase 4). ERROR is not
            # checkpointed, so the note is retried and processes by itself on
            # the first run after the seam is wired.
            LOG.error("auto_tagger: no OperationContext on the run context — refusing %s", path)
            return ConsumerResult(
                Status.ERROR,
                "no OperationContext available — refusing an unrecorded vault write",
                {"reason": "missing_op_context"},
            )

        try:
            vocabulary, descriptions = self._grounding(ctx.config, op_ctx)
            proposed = self.propose(self.build_prompt(payload, vocabulary, descriptions), ctx)
        except (LLMError, LLMUnavailable) as exc:
            LOG.warning("auto_tagger: no usable tags for %s: %s", path, exc)
            return ConsumerResult(Status.ERROR, str(exc), {"tags_written": []})

        tags = self.clean(proposed, ctx.config)
        if not tags:
            # Well-formed and empty: the model had nothing to say. SKIP is
            # terminal, so we do not ask again until the note changes.
            return ConsumerResult(
                Status.SKIP, "the model proposed no usable tags", {"proposed": proposed}
            )

        extra_map = ctx.config.suggestions.tag_normalization
        existing = {_normalize(tag, extra_map) for tag in payload.tags()}
        # Only genuinely NEW tags are machine-added; a tag Matt already wrote
        # must never be claimed in `auto_tags` (that would steal provenance).
        added = [tag for tag in tags if tag not in existing]
        prior_auto = [
            str(value)
            for value in frontmatter.Frontmatter(
                fields=dict(payload.frontmatter or {})
            ).get_list(AUTO_TAGS_FIELD)
        ]

        changes: dict[str, Any] = {
            # `tags` MERGES by contract (order- and case-preserving, 08 §A24).
            "tags": added,
            # Everything else REPLACES, so pass the union explicitly — one
            # logical edit, one oplog line, one ActionRecord (12 §2).
            AUTO_TAGS_FIELD: frontmatter.merge_tags(prior_auto, added),
            AUTO_TAG_FIELD: STATE_DONE,
            AUTO_TAG_HASH_FIELD: body_hash(payload.content),
        }

        try:
            result = fileops.update_frontmatter(
                # `auto_tags_present` is per-NOTE decision context (12 §2), so
                # it cannot be set on the run-scoped context the runner shares
                # between consumers — a copy is the only way to record it
                # without leaking one note's provenance into the next one's
                # record. Everything expensive (index, recorder, oplog) is
                # shared by reference.
                replace(op_ctx, auto_tags_present=tuple(changes[AUTO_TAGS_FIELD])),
                path,
                changes,
            )
        except OrganizeError as exc:
            LOG.warning("auto_tagger: refused to tag %s: %s", path, exc)
            return ConsumerResult(Status.ERROR, str(exc), {"tags_written": []})
        except OSError as exc:
            LOG.error("auto_tagger: could not tag %s: %s", path, exc)
            return ConsumerResult(Status.ERROR, str(exc), {"tags_written": []})

        if not result.ok:
            return ConsumerResult(
                Status.ERROR, result.error or "frontmatter update failed", {"tags_written": []}
            )

        LOG.info("auto_tagger: tagged %s with %s", path, ", ".join(added) or "(nothing new)")
        return ConsumerResult(
            Status.SUCCESS,
            f"auto-tagged with {len(added)} new tag(s)",
            {"tags_written": added, "tags_proposed": tags},
        )

    # --- internals --------------------------------------------------------

    def _grounding(
        self, config: Config, op_ctx: OperationContext | None
    ) -> tuple[list[str], list[str]]:
        """``(vocabulary, descriptions)``, derived ONCE per run.

        Both walk every record in the index, and a 13k-note vault costs real
        time per walk — recomputing them for each of ``max_notes_per_run``
        notes would spend the pipeline's whole 09 §4 budget rebuilding the
        same two lists. Keyed on the OperationContext, which the runner
        creates per run and holds for its duration, so a second run (or a
        second vault, in tests) never reads a stale answer.
        """
        key = id(op_ctx)
        if self._grounding_cache is not None and self._grounding_cache[0] == key:
            return self._grounding_cache[1]
        value = (self.vocabulary(op_ctx), self.descriptions(config, op_ctx))
        self._grounding_cache = (key, value)
        return value

    def _tag_map(self, op_ctx: OperationContext | None) -> dict[str, str]:
        config = getattr(op_ctx, "config", None) if op_ctx is not None else None
        if config is None:
            return {}
        return config.suggestions.tag_normalization
