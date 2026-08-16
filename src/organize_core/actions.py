"""ActionRecord schema + append-only JSONL writer + query (spec 12 §2).

EVERY state-changing operation appends one record to
``<state>/actions/YYYY-MM.jsonl`` — including actions outside sessions
(CLI, consumers, routes). The write path ships in Phase 1 (spec README
build order note: recording lands with ① so the corpus grows early); this
is a Phase-1 GATE.

Hard rules (spec 12 §2):
- Append-only, atomic appends (one ``write()`` of a full line + newline,
  fsync policy documented by the builder), never rewritten. A torn write
  must never produce a partial JSONL line (12 §3 torn-write test).
- Recording failure must NOT block the operation — log loudly, continue.
- Capture body stored in full; targets stored as hash + unified diff
  (full before-text only when the file is new or < 64 KB).
- The counterfactual is stored, not just the choice: suggestions_shown +
  chosen_rank; proposed_diff vs final_diff; rejections are signal too.
- Privacy: corpus stays local; never shipped to any API except when Matt
  invokes a learning/auto-organize feature that reads it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ACTIONS_SCHEMA_VERSION = 1

Operation = Literal[
    "move",
    "merge",
    "append",
    "integrate",
    "archive",
    "skip",
    "meta_edit",
    "create_folder",
    "tag_edit",
]

EditMode = Literal["manual", "append", "integrate"]

Verdict = Literal["accepted", "edited", "rejected"]


@dataclass(frozen=True)
class CaptureState:
    """``capture`` block (spec 12 §2): the note being organized, body in
    full, frontmatter before and (when meta/tag edits happened) after."""

    path: str
    content_hash: str
    frontmatter_before: dict[str, Any]
    body_before: str
    frontmatter_after: dict[str, Any] | None = None


@dataclass(frozen=True)
class TargetState:
    """One ``targets[]`` entry — ONE PER FILE TOUCHED; multi-destination is
    first-class (spec 12 §2). ``diff`` is a unified diff; full before-text
    included when the file is new or small (<64 KB)."""

    path: str
    role: Literal["destination", "merge_target", "append_target"]
    before_hash: str | None
    after_hash: str | None
    diff: str
    description: str | None = None  # the NL description of this destination


@dataclass(frozen=True)
class SuggestionShown:
    """One line of the counterfactual (spec 12 §2 context.suggestions_shown)."""

    path: str
    score: float
    rank: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionContext:
    """``context`` block (spec 12 §2)."""

    session_id: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    suggestions_shown: tuple[SuggestionShown, ...] = ()
    chosen_rank: int | None = None  # 1 == the engine was right
    route: str | None = None
    auto_tags_present: tuple[str, ...] = ()
    vault_stats: dict[str, int] = field(default_factory=dict)
    durations_ms: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMTrace:
    """``llm`` block — integrate/auto actions only (spec 12 §2)."""

    backend: str
    model: str
    prompt_hash: str
    proposed_diff: str
    final_diff: str  # ≠ proposed when Matt hand-edited
    verdict: Verdict


@dataclass(frozen=True)
class ActionRecord:
    """One JSONL line (spec 12 §2 schema, field-for-field)."""

    id: str  # "act_<ulid>"
    ts: str  # ISO8601 UTC
    actor: str  # "matt" | "claude-integrate" | "route:<name>" | "consumer:<name>" | "auto-organize"
    operation: Operation
    capture: CaptureState
    targets: tuple[TargetState, ...] = ()
    context: ActionContext = field(default_factory=ActionContext)
    edit_mode: EditMode | None = None
    llm: LLMTrace | None = None
    schema_version: int = ACTIONS_SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ActionRecord:
        """Strict parse; unknown schema_version tolerated on read (forward
        compat for the query path)."""
        raise NotImplementedError


def new_action_id(*, now: float | None = None) -> str:
    """``act_<ulid>`` — 26-char Crockford-base32 ULID (48-bit ms timestamp +
    80 random bits), stdlib-only implementation (no ulid dependency —
    architect decision; format matches spec 12 §2's ``act_<ulid>``)."""
    raise NotImplementedError


class ActionRecorder:
    """The append-only writer + reader over ``<actions_dir>/YYYY-MM.jsonl``."""

    def __init__(self, actions_dir: Path) -> None:
        raise NotImplementedError

    def record(self, record: ActionRecord) -> bool:
        """Append one record to the current month's file. Returns False
        (after a LOUD log) instead of raising — recording failures never
        block the operation (spec 12 §2). Atomic single-line append."""
        raise NotImplementedError

    def query(
        self,
        *,
        operation: Operation | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> Iterator[ActionRecord]:
        """Stream matching records across month files, oldest first.
        Corrupt lines are skipped with a warning, never fatal."""
        raise NotImplementedError

    def export(self, out: Path | None = None, **filters: Any) -> int:
        """``organize actions export`` — concatenate/filter to ``out`` or
        stdout (spec 12 §2). Returns record count."""
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        """``organize actions stats`` (spec 12 §2 "Uses" #1): accept-rate of
        top suggestion, per-route volumes, integrate accept/edit/reject
        rates."""
        raise NotImplementedError
