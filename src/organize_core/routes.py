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

PURITY: :func:`resolve` and :func:`merge_route_suggestions` are pure over
config + tags + suggestions — no filesystem access, no clock. Destination
paths are joined LEXICALLY (no ``Path.resolve()``): symlink resolution is
I/O, and config validation already forbids ``..`` and absolute
destinations (11 §1 load-time contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from organize_core import frontmatter
from organize_core.config import Config, RouteConfig
from organize_core.fileops import OperationContext, OperationResult
from organize_core.index import PARA_KEY_TO_TYPE, NoteRecord, VaultIndex
from organize_core.suggest import ARCHIVE_SUGGESTION_TYPE, Suggestion

#: Routes are NOT scored — spec 11 §1 ranks them above scored suggestions
#: positionally, and :func:`merge_route_suggestions` is what enforces that.
#: This finite sentinel keeps a route on top anyway for any consumer that
#: re-sorts by score, and stays JSON-serializable for the action record's
#: ``suggestions_shown`` (12 §2) — ``math.inf`` is not.
ROUTE_SUGGESTION_SCORE = 1000.0

#: Vault folder name → the SINGULAR ``Suggestion.type`` vocabulary, for the
#: no-config path where the leading path segment IS the folder name: the PARA
#: roots are plural on disk (``areas/``) while the type value is singular, and
#: Matt's archive root is already singular (spec 02), which maps to itself.
_TYPE_ALIASES = {
    **PARA_KEY_TO_TYPE,
    "archive": ARCHIVE_SUGGESTION_TYPE,
}

#: Fallback type for a destination outside any recognizable PARA root.
_UNKNOWN_TYPE = "other"

#: ``"a+b"`` in a route's ``tags`` requires BOTH tags (spec 11 §1).
_ALL_OF_SEPARATOR = "+"

_PHASE_4_HINT = (
    "Phase 4 wires routes into the doc-05 file operations; "
    "resolve(), merge_route_suggestions() and get_description() work today."
)


@dataclass(frozen=True)
class RouteMatch:
    """One matched route for one capture."""

    route: RouteConfig
    route_name: str  # display name: first tag or explicit name
    destination: Path  # resolved absolute path
    is_folder: bool
    #: SINGULAR PARA type of the destination, resolved against the CONFIGURED
    #: folder names by :func:`resolve` (which holds the ``Config``).
    #: ``as_suggestion`` takes no config, so without this a vault that renames
    #: a PARA root (``projects = "p"``) would type its route suggestions
    #: ``"p"`` while every scored suggestion was typed ``"project"`` — and
    #: the UI groups on this field. Empty means "derive it from the leading
    #: path segment", which is what a hand-built RouteMatch gets.
    para_type: str = ""

    def as_suggestion(self) -> Suggestion:
        """Rendered distinctly above scored suggestions:
        ``[→] training-log.md (route: workout)`` with the description
        attached (spec 11 §1 "Where routes act")."""
        description = (self.route.description or "").strip()
        return Suggestion(
            path=str(self.destination),
            name=self.destination.name,
            type=self.para_type or _destination_type(self.route.destination),
            score=ROUTE_SUGGESTION_SCORE,
            reasons=(f"Route '{self.route_name}' ({self.route.mode})",),
            route=self.route_name,
            description=description or None,
        )


def _destination_type(destination: str, config: Config | None = None) -> str:
    """PARA type of a vault-relative route destination, from its leading
    segment (``areas/health/training-log.md`` → ``areas``).

    The answer is the SINGULAR type VALUE (``areas/health/x.md`` → ``area``),
    because this feeds ``Suggestion.type``.

    Given a ``config``, the leading segment is matched against the CONFIGURED
    ``vault.para_folders`` values, so a renamed PARA root still reports its
    canonical type. Without one (a hand-built ``RouteMatch``) the leading
    segment IS the folder name in every layout spec 02 describes, and
    ``_TYPE_ALIASES`` carries it to singular. Unrecognizable destinations
    report ``"other"`` rather than claiming a PARA type they do not have.
    """
    segments = [part for part in destination.strip().strip("/").split("/") if part]
    if not segments:
        return _UNKNOWN_TYPE
    head = segments[0].strip()
    if config is not None:
        for key, folder in (config.vault.para_folders or {}).items():
            if str(folder).strip().strip("/").casefold() == head.casefold():
                return PARA_KEY_TO_TYPE.get(key, key)
    lowered = head.lower()
    return _TYPE_ALIASES.get(lowered, lowered or _UNKNOWN_TYPE)


def _normalized(value: str, config: Config) -> str:
    """The ONE shared normalizer, with the configured tag_normalization map
    (spec 04 §1) — routes must match tags the same way scoring does."""
    return frontmatter.normalize_tag(value, config.suggestions.tag_normalization)


def _route_matches(route: RouteConfig, note_tags: set[str], config: Config) -> bool:
    """Any-of over ``route.tags``; an ``"a+b"`` entry requires every part."""
    for expression in route.tags or ():
        text = expression if isinstance(expression, str) else str(expression)
        parts = [part.strip() for part in text.split(_ALL_OF_SEPARATOR)]
        required = [_normalized(part, config) for part in parts if part]
        if not required:
            continue
        if all(part in note_tags for part in required):
            return True
    return False


def _route_display_name(route: RouteConfig) -> str:
    """First tag (spec 11 §1's ``(route: workout)`` rendering); the
    destination's basename when a route somehow carries no tags."""
    for expression in route.tags or ():
        text = (expression if isinstance(expression, str) else str(expression)).strip()
        if text:
            return text
    return Path(route.destination.rstrip("/")).name or route.destination


def resolve(note_tags: list[str], config: Config) -> list[RouteMatch]:
    """All routes matched by a tag set, in config order (spec 11 §1).
    Pure over config + tags; tag comparison uses the shared normalizer."""
    normalized_tags = {
        _normalized(tag if isinstance(tag, str) else str(tag), config)
        for tag in (note_tags or ())
        if str(tag).strip()
    }
    if not normalized_tags:
        return []

    matches: list[RouteMatch] = []
    for route in config.routes or ():
        if not _route_matches(route, normalized_tags, config):
            continue
        is_folder = route.destination.endswith("/")
        matches.append(
            RouteMatch(
                route=route,
                route_name=_route_display_name(route),
                destination=config.vault.root / route.destination,
                is_folder=is_folder,
                para_type=_destination_type(route.destination, config),
            )
        )
    return matches


def _path_key(path: str) -> str:
    """Comparison key for de-duplicating a route destination against a
    scored candidate that happens to be the same folder."""
    return (path or "").rstrip("/")


def merge_route_suggestions(
    matches: list[RouteMatch], scored: list[Suggestion]
) -> list[Suggestion]:
    """Route suggestions prepended above scored ones (spec 11 §1); the
    combined list still respects the UI's max length by truncating SCORED
    entries, never routes or the archive entry.

    The cap is ``len(scored)``: ``suggest.suggest`` has already truncated to
    ``suggestions.max_suggestions`` (archive entry included), so the list it
    hands over IS the UI's max length. Routes and the archive entry always
    survive (ARCHITECTURE resolution #10), so a caller with more routes than
    the cap gets a longer list rather than a silently dropped route.

    CONFIRMED by the integrator (the routes seat asked): both composition
    roots pass ``suggest()`` output unmodified — ``cli.cmd_suggest`` and
    ``server._suggest_for_note`` each call ``rank_suggestions(...)`` and hand
    the result straight here. No caller passes an untruncated candidate list,
    so no ``max_suggestions`` parameter is needed on this signature. A future
    caller that wants a different cap must truncate before calling.

    A scored entry pointing at a destination a route already offers is
    dropped — the route carries the same path plus its description, and
    showing one folder twice is a UI bug, not a feature.
    """
    route_suggestions = [match.as_suggestion() for match in (matches or ())]
    route_paths = {_path_key(item.path) for item in route_suggestions}

    archive_entries: list[Suggestion] = []
    others: list[Suggestion] = []
    for suggestion in scored or ():
        if suggestion.type == ARCHIVE_SUGGESTION_TYPE:
            archive_entries.append(suggestion)
        elif _path_key(suggestion.path) in route_paths:
            continue
        else:
            others.append(suggestion)

    limit = len(scored or ())
    keep = max(0, limit - len(route_suggestions) - len(archive_entries))
    return route_suggestions + others[:keep] + archive_entries


def apply_route(
    ctx: OperationContext, capture: NoteRecord, match: RouteMatch
) -> OperationResult:
    """Execute ONE route destination via the ordinary doc-05 paths:
    move → fileops.move_to_destination; append → fileops.append_to_note;
    integrate → the doc-12 integrate flow (deletion guard, review gate,
    ``no-ai`` refusal). Does NOT archive — see :func:`apply_all`.
    [Phase 4]"""
    raise NotImplementedError(f"routes.apply_route is Phase 4. {_PHASE_4_HINT}")


def apply_all(
    ctx: OperationContext, capture: NoteRecord, matches: list[RouteMatch]
) -> list[OperationResult]:
    """All destinations in config order; archive the original exactly once
    after ALL succeed; any failure ⇒ capture stays unarchived, every
    attempt logged (spec 11 §1, acceptance test 2). [Phase 4]"""
    raise NotImplementedError(f"routes.apply_all is Phase 4. {_PHASE_4_HINT}")


# --- natural-language descriptions beyond routes (spec 11 §3) --------------


def _index_note_candidates(path: Path) -> list[Path]:
    """Where a description may live for ``path`` (spec 11 §3), in order: the
    note itself, then the folder's ``index.md``, then ``<folder>.md``.

    Folder-vs-file is decided by lookup, not by ``stat`` — ``get_description``
    reads the warm index, never the disk.
    """
    return [path, path / "index.md", path / f"{path.name}.md"]


def _description_config_keys(path: Path, config: Config) -> list[str]:
    """``[descriptions]`` keys to try, vault-relative form first (the shipped
    example writes ``"areas/health"``), then the absolute path; each with and
    without a trailing slash."""
    candidates: list[str] = []
    try:
        relative = path.relative_to(config.vault.root).as_posix()
    except ValueError:
        relative = ""
    absolute = path.as_posix()
    for base in (relative, absolute):
        if not base:
            continue
        candidates.append(base)
        candidates.append(f"{base}/")

    ordered: list[str] = []
    for key in candidates:
        if key not in ordered:
            ordered.append(key)
    return ordered


def get_description(path: Path, index: VaultIndex, config: Config) -> str | None:
    """Description lookup order (spec 11 §3): ``description`` frontmatter
    field of the folder's ``index.md``/``<folder>.md`` if present, else the
    central ``[descriptions]`` config table. Surfaced in browse + suggestion
    reasons; input to docs 12/13."""
    if index is not None:
        for candidate in _index_note_candidates(path):
            record = index.get(candidate)
            if record is None:
                continue
            text = (record.description or "").strip()
            if text:
                return text

    for key in _description_config_keys(path, config):
        text = (config.descriptions.get(key) or "").strip()
        if text:
            return text
    return None


def set_description(
    ctx: OperationContext, path: Path, text: str
) -> OperationResult:
    """``organize routes describe <path> "<text>"`` (spec 11 §3): write the
    ``description`` into the folder's index-note frontmatter when one
    exists (round-trip-safe), else into the config ``[descriptions]``
    table. [Phase 4]"""
    raise NotImplementedError(f"routes.set_description is Phase 4. {_PHASE_4_HINT}")
