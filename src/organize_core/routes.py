"""Route resolution: tags → destinations with integration modes (spec 11).

Phase-4 fills in the apply path; the resolution/data model ships now so the
UI, suggest layer, and server can compile against it.

Semantics (spec 11 §1):
- A route's ``tags`` list is any-of; an entry ``"a+b"`` requires both tags.
- ``move`` for folder destinations; ``append``/``integrate`` for files.
- A capture may match multiple routes → each destination gets its action;
  the original archives ONCE, after all succeed; failures leave the
  capture unarchived and reported. Order = config order.
- A matching route becomes the TOP suggestion in the UI, rendered
  distinctly, above scored suggestions.
- ``auto=True`` routes are applied unattended by the ``tag_router``
  consumer; non-auto routes only surface in the UI.
- ``description`` is Matt's natural language and is load-bearing: UI
  display, auto-tagger input, and doc-13 destination knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from organize_core.config import Config, RouteConfig
from organize_core.fileops import OperationContext, OperationResult
from organize_core.index import NoteRecord, VaultIndex
from organize_core.suggest import Suggestion


@dataclass(frozen=True)
class RouteMatch:
    """One matched route for one capture."""

    route: RouteConfig
    route_name: str  # display name: first tag or explicit name
    destination: Path  # resolved absolute path
    is_folder: bool

    def as_suggestion(self) -> Suggestion:
        """Rendered distinctly above scored suggestions:
        ``[→] training-log.md (route: workout)`` with the description
        attached (spec 11 §1 "Where routes act")."""
        raise NotImplementedError


def resolve(note_tags: list[str], config: Config) -> list[RouteMatch]:
    """All routes matched by a tag set, in config order (spec 11 §1).
    Pure over config + tags; tag comparison uses the shared normalizer."""
    raise NotImplementedError


def merge_route_suggestions(
    matches: list[RouteMatch], scored: list[Suggestion]
) -> list[Suggestion]:
    """Route suggestions prepended above scored ones (spec 11 §1); the
    combined list still respects the UI's max length by truncating SCORED
    entries, never routes or the archive entry."""
    raise NotImplementedError


def apply_route(
    ctx: OperationContext, capture: NoteRecord, match: RouteMatch
) -> OperationResult:
    """Execute ONE route destination via the ordinary doc-05 paths:
    move → fileops.move_to_destination; append → fileops.append_to_note;
    integrate → the doc-12 integrate flow (deletion guard, review gate,
    ``no-ai`` refusal). Does NOT archive — see :func:`apply_all`.
    [Phase 4]"""
    raise NotImplementedError


def apply_all(
    ctx: OperationContext, capture: NoteRecord, matches: list[RouteMatch]
) -> list[OperationResult]:
    """All destinations in config order; archive the original exactly once
    after ALL succeed; any failure ⇒ capture stays unarchived, every
    attempt logged (spec 11 §1, acceptance test 2). [Phase 4]"""
    raise NotImplementedError


# --- natural-language descriptions beyond routes (spec 11 §3) --------------


def get_description(path: Path, index: VaultIndex, config: Config) -> str | None:
    """Description lookup order (spec 11 §3): ``description`` frontmatter
    field of the folder's ``index.md``/``<folder>.md`` if present, else the
    central ``[descriptions]`` config table. Surfaced in browse + suggestion
    reasons; input to docs 12/13."""
    raise NotImplementedError


def set_description(
    ctx: OperationContext, path: Path, text: str
) -> OperationResult:
    """``organize routes describe <path> "<text>"`` (spec 11 §3): write the
    ``description`` into the folder's index-note frontmatter when one
    exists (round-trip-safe), else into the config ``[descriptions]``
    table. [Phase 4]"""
    raise NotImplementedError
