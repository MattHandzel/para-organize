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

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from organize_core import frontmatter
from organize_core.config import Config, RouteConfig
from organize_core.errors import OperationError, RouteConfigError
from organize_core.fileops import (
    LoggedOperation,
    OperationContext,
    OperationResult,
    append_to_note,
    archive_capture,
    atomic_write,
    is_ai_actor,
    move_to_destination,
    require_in_vault,
    update_frontmatter,
)
from organize_core.index import PARA_KEY_TO_TYPE, NoteRecord, VaultIndex
from organize_core.suggest import ARCHIVE_SUGGESTION_TYPE, Suggestion

if TYPE_CHECKING:  # pragma: no cover - typing only; no new runtime import edge
    from organize_core.actions import ActionRecord, ActionRecorder, TargetState

logger = logging.getLogger(__name__)

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

#: ``integrate`` is doc 12 §1's Claude-edit path (LLM proposal → deletion
#: guard → review gate → atomic apply). Phase 4 ships tag ROUTING; the
#: integrate EDITING half is Phase 5. Resolution and planning still show
#: integrate routes — only :func:`apply_route` refuses, and it says so.
_PHASE_5_HINT = (
    "'integrate' is the doc-12 §1 Claude-edit path (LLM proposal, deletion guard, "
    "review gate) and lands in Phase 5. Route resolution and the suggestion list "
    "already SHOW integrate routes; 'move' and 'append' routes apply today. "
    "Use mode = \"append\" for a mechanical append until Phase 5 ships."
)

#: STRONGEST MUTATION WINS — the ``operation`` a single ActionRecord claims
#: when one logical multi-route action mixed modes (Phase-4 routes-apply
#: ruling). A move consumes the original, an integrate rewrites a target, an
#: append only adds; naming the batch after its weakest member would let a
#: corpus reader mistake a relocation for an addition. ``targets[]`` still
#: carries every destination, so nothing is lost by the collapse.
_OPERATION_PRECEDENCE: tuple[str, ...] = ("move", "integrate", "append")


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


def _refuse_integrate(match: RouteMatch) -> None:
    """The doc-12 §1 handoff boundary, in ONE place so both entry points
    refuse identically and with the same words."""
    raise NotImplementedError(
        f"route {match.route_name!r} has mode 'integrate' and cannot be applied yet "
        f"(destination {match.destination}). {_PHASE_5_HINT}"
    )


def apply_route(
    ctx: OperationContext, capture: NoteRecord, match: RouteMatch
) -> OperationResult:
    """Execute ONE route destination via the ordinary doc-05 paths:
    ``move`` → :func:`fileops.move_to_destination`; ``append`` →
    :func:`fileops.append_to_note` (mechanical, doc 12 §1's ``append`` edit
    mode — the capture body under the route's ``template``, target
    frontmatter otherwise untouched except ``last_edited_date``);
    ``integrate`` → refused, Phase 5 (:data:`_PHASE_5_HINT`).

    ARCHIVING IS NOT THIS FUNCTION'S JOB. Spec 11 §1 archives the capture
    ONCE, after every destination succeeds, and only :func:`apply_all` can
    know that — so ``append_to_note`` leaves the capture in place
    (ARCHITECTURE ruling #11) and ``move_to_destination`` is called with
    ``archive=False`` (the Phase-4 routes-apply ruling). Callers filing a
    capture by route therefore go through :func:`apply_all` EVEN FOR A SINGLE
    MATCH; that is what turns a single route into spec 11 §4's acceptance
    test 1 ("capture archived"). Calling this function directly files the
    content and deliberately leaves the original where it was.

    Errors follow the ARCHITECTURE error-line rule: an unusable ROUTE (an
    unknown mode, an ``integrate`` route) RAISES, because the request never
    named an operation that could be attempted; a real attempt that fails
    (missing capture, unwritable destination) comes back as
    ``ok=False`` with a FAILED operations-log line.
    """
    mode = (match.route.mode or "").strip()
    if mode == "move":
        return move_to_destination(ctx, capture, match.destination, archive=False)
    if mode == "append":
        return append_to_note(
            ctx,
            capture,
            match.destination,
            template=match.route.template,
            # Names the route in the delivered marker, so a human reading the
            # target can tell WHICH route put the block there.
            route=match.route_name,
        )
    if mode == "integrate":
        _refuse_integrate(match)
    raise RouteConfigError(
        f"route {match.route_name!r} has unknown mode {match.route.mode!r}",
        hint="mode is one of 'move' (folder destination), 'append' or 'integrate' (file "
        "destination) — spec 11 §1",
    )


class _RouteRecorder:
    """Facade over the real :class:`ActionRecorder` that CAPTURES the
    per-destination records instead of appending them.

    Spec 12 §2 makes multi-file first-class through ONE record with one
    ``targets[]`` entry per file ("Multi-destination route action: one
    record, two targets entries, both diffs correct" — 12 §3). Each
    ``fileops`` operation builds its own record, so :func:`apply_all` runs
    them against this facade, harvests the ``TargetState`` objects fileops
    itself produced (which is how the diffs stay the REAL diffs rather than
    a second, drifting implementation), and appends exactly one merged
    record through the real recorder afterwards.

    ``capturing`` is lowered around the closing archive: archiving the
    original is not a destination, and ``move_to_destination`` does not list
    it as a target either — its story is the ``archive`` operations-log line
    plus ``details["archive_path"]``.

    Every read method (``query``/``stats``/``export``/``month_files``) and
    ``actions_dir`` delegate to the real recorder, so this is a write
    interceptor, not a second corpus.
    """

    def __init__(self, inner: ActionRecorder) -> None:
        self._inner = inner
        self.records: list[ActionRecord] = []
        self.capturing = True

    def record(self, record: ActionRecord) -> bool:
        if self.capturing:
            self.records.append(record)
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _iso(now: float) -> str:
    """Operations-log timestamp. Deliberately identical to the one
    ``fileops`` renders — pinned by a test that parses a routes-written line
    and a fileops-written line from the same clock and compares their ``ts``
    (spec 05 §1.5 is ONE line format, not two)."""
    return datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _described_targets(
    record: ActionRecord, match: RouteMatch | None
) -> tuple[TargetState, ...]:
    """``targets[].description`` — "the NL description of this destination,
    if any" (12 §2) — filled in from the ROUTE when the destination has none
    of its own.

    ``fileops`` resolves it through ``ctx.describe`` (→
    :func:`get_description`), which reads the destination's index note or the
    ``[descriptions]`` config table. A route's ``description`` is the third
    place that text lives and the only one a route destination is guaranteed
    to have — spec 11 §1 calls it load-bearing precisely because it is "the
    definition of what belongs there", which is what doc 13 will read this
    field FOR. Without this, every route action recorded ``description:
    null`` unless the destination happened to carry its own.

    The destination's own words WIN: they describe the place, while the
    route's describe why this capture was sent there. Only a missing one is
    filled.
    """
    if match is None:
        return record.targets
    text = (match.route.description or "").strip()
    if not text:
        return record.targets
    return tuple(
        target if target.description else replace(target, description=text)
        for target in record.targets
    )


def _merged_record(
    ctx: OperationContext,
    collected: list[tuple[RouteMatch | None, ActionRecord]],
    matches: list[RouteMatch],
    *,
    partial_failure: str | None,
) -> ActionRecord | None:
    """ONE ActionRecord for the logical multi-target action (12 §2).

    Built by ``dataclasses.replace`` over a record ``fileops`` already
    constructed, so the capture block, the decision context and every
    ``targets[]`` diff are the real ones. The base record is the one that
    carries a ``frontmatter_after`` if any does — a ``move`` rewrites the
    capture's frontmatter and an ``append`` does not, so that is the record
    holding the capture's post-state.

    ``actor`` (integrator addendum, 4ffef89): when exactly ONE route fired
    the record's actor is ``route:<name>`` — doc 12 §2's enum member for a
    route firing — so the corpus can be sliced by route without parsing
    ``context.route``. A multi-route aggregate keeps ``ctx.actor`` (no
    single route owns it) and identifies itself through
    ``context.route``, which is the comma-joined route names either way.
    This is RECORD-level only: the :class:`OperationContext` actor (e.g.
    ``consumer:tag_router``, ``matt``) is untouched, so the ``no-ai``
    refusals every fileops call makes still key off who is really driving.
    """
    if not collected:
        return None
    records = [record for _match, record in collected]
    base = next(
        (record for record in records if record.capture.frontmatter_after is not None),
        records[0],
    )
    targets = tuple(
        target
        for match, record in collected
        for target in _described_targets(record, match)
    )
    # STRONGEST MUTATION WINS (Phase-4 routes-apply ruling): move > integrate
    # > append. Derived from what ACTUALLY happened, never from what was
    # planned — a move that failed, or that was skipped after an earlier
    # failure, records nothing, so it cannot make the aggregate claim
    # `operation: move`.
    performed = {record.operation for record in records}
    operation = next(
        (name for name in _OPERATION_PRECEDENCE if name in performed), base.operation
    )
    edit_mode = next((record.edit_mode for record in records if record.edit_mode), None)
    route_label = ", ".join(match.route_name for match in matches) or None
    actor = f"route:{matches[0].route_name}" if len(matches) == 1 else base.actor
    return replace(
        base,
        actor=actor,
        operation=operation,  # type: ignore[arg-type]
        targets=targets,
        edit_mode=edit_mode,
        context=replace(
            base.context,
            route=route_label,
            partial_failure=partial_failure or base.context.partial_failure,
        ),
    )


def apply_all(
    ctx: OperationContext, capture: NoteRecord, matches: list[RouteMatch]
) -> list[OperationResult]:
    """Every matched destination, then the capture archived exactly ONCE —
    and only after all of them succeeded (spec 11 §1, acceptance test 2;
    ARCHITECTURE ruling #11).

    Returns one :class:`OperationResult` per entry of ``matches``, in
    ``matches`` order, PLUS a trailing ``archive`` result when this call
    archived the capture. A caller reads success per destination by zipping
    with ``matches`` and finds the archive as the trailing entry (there is
    never more than one).

    Execution is in CONFIG ORDER (spec 11 §1), and several ``move`` routes
    are legal — two copies filed, one original archived — because every
    destination runs with archiving suppressed and this function performs the
    single archive at the end.

    Failure (world state, not addressing): every destination is attempted and
    logged, the ones that succeeded STAND, and the capture is NOT archived.
    Failed destinations
    come back ``ok=False`` with a FAILED operations-log line each; the record
    (when anything reached the vault) carries ``context.partial_failure``
    naming what did not finish, which is what keeps a half-applied route
    action out of the learning corpus (04 §33).

    Index bookkeeping follows the archive: each destination is indexed by its
    own fileops call, and the capture's own entry is removed by
    ``archive_capture`` — i.e. after the final archive, never before it (05
    §2 step 8, deferred per the Phase-4 ruling).

    An ``integrate`` route raises BEFORE anything is written — a batch that
    cannot be carried out as asked must not be half-carried-out.
    """
    ordered = list(matches or ())
    if not ordered:
        return []

    for match in ordered:
        if match.route.mode == "integrate":
            _refuse_integrate(match)

    recorder = _RouteRecorder(ctx.recorder)
    child = replace(ctx, recorder=recorder, on_record=None)

    results: list[OperationResult | None] = [None] * len(ordered)
    attribution: list[tuple[RouteMatch | None, ActionRecord]] = []
    failed: list[str] = []
    for position in range(len(ordered)):
        match = ordered[position]
        mark = len(recorder.records)
        result = apply_route(child, capture, match)
        results[position] = result
        # Which route produced which record, so `_described_targets` can fall
        # back to THIS route's description for THIS destination.
        attribution.extend((match, record) for record in recorder.records[mark:])
        if not result.ok:
            failed.append(f"{match.route_name} -> {match.destination}")

    applied = [result for result in results if result is not None and result.ok]

    archive_result: OperationResult | None = None
    if not failed and applied:
        recorder.capturing = False
        try:
            archive_result = archive_capture(child, capture)
        finally:
            recorder.capturing = True
        if not archive_result.ok:
            failed.append(f"archive -> {archive_result.destination}")

    partial_failure: str | None = None
    if failed and applied:
        partial_failure = (
            f"{len(applied)} of {len(ordered)} route destinations applied; "
            f"failed: {'; '.join(failed)}"
        )
    elif failed:
        partial_failure = f"no destination was applied; failed: {'; '.join(failed)}"

    record = _merged_record(ctx, attribution, ordered, partial_failure=partial_failure)
    if record is not None and ctx.recorder.record(record) and ctx.on_record is not None:
        try:
            ctx.on_record(record)
        except Exception:  # noqa: BLE001 - a derived view never blocks the op
            logger.error(
                "ACTION RECORD CONSUMER FAILED for the route action on %s — the "
                "destinations that succeeded are applied and the record was written "
                "(spec 12 §2)",
                capture.path,
                exc_info=True,
            )

    out = [result for result in results if result is not None]
    if archive_result is not None:
        out.append(archive_result)
    return out


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


def description_note_for(path: Path) -> Path | None:
    """The note whose frontmatter holds ``path``'s description (spec 11 §3),
    or ``None`` when there is no such note on disk.

    A markdown FILE describes itself. A FOLDER is described by its
    ``index.md``, else by ``<folder>/<folder-name>.md`` — the same two
    candidates :func:`get_description` reads, in the same order, so a write
    always lands where the read looks.

    This one touches the disk (``is_file``/``is_dir``); the resolution half
    of this module stays pure.
    """
    path = Path(path)
    if path.is_file():
        return path
    if path.is_dir():
        for candidate in (path / "index.md", path / f"{path.name}.md"):
            if candidate.is_file():
                return candidate
    return None


def set_description(
    ctx: OperationContext, path: Path, text: str
) -> OperationResult:
    """``organize routes describe <path> "<text>"`` (spec 11 §3): write the
    ``description`` into the folder's index-note frontmatter (round-trip
    safe), or into the file's own frontmatter when ``path`` is a note.

    Goes through :func:`fileops.update_frontmatter`, which is what makes
    this ONE logical edit with the whole doc-05/doc-12 apparatus attached:
    the round-trip law (every other field, known or unknown, survives
    byte-for-byte — 08 §A12), a backup, an atomic write, a ``metadata``
    operations-log line, one ``meta_edit`` ActionRecord, dry-run support,
    and the ``no-ai`` refusal — an AI actor may not write a description onto
    a ``no-ai: true`` note any more than it may write anything else there
    (vault law, spec 02).

    An empty/whitespace ``text`` REMOVES the field, which is the only way to
    un-describe a folder once described.

    A FOLDER WITH NO INDEX NOTE GETS ONE (Phase-4 routes-apply ruling): spec
    11 §3 names ``index.md`` as the primary storage, and ``organize routes
    describe`` is a keystroke Matt typed, so the note is seeded (``#
    <folder>`` heading) and then described through the same recorded
    ``update_frontmatter`` call — ``details["created"]`` says so and the
    record's target diff carries the whole description edit. AUTOMATED ACTORS
    REFUSE to create: an unattended path may fill in a description that
    already has somewhere to live, but it may not decide that a folder should
    now have an index note (the same reasoning as the ``no-ai`` law — tooling
    edits what Matt made, it does not invent vault structure).

    Spec 11 §3's other storage location — the central ``[descriptions]``
    table — is READ by :func:`get_description` and is READ-ONLY from the core
    (same ruling): it lives in ``config.toml``, which is core configuration
    rather than vault content, ``OperationContext`` carries no ``CorePaths``
    to find it, and the stdlib has no TOML writer (the dependency budget bars
    adding one). It stays the hand-maintained fallback.

    ``OperationError`` is raised only for an addressing failure — a path that
    does not exist at all, or a creation an automated actor may not make.
    """
    path = Path(path)
    target = description_note_for(path)
    created = False
    if target is None:
        target = _create_index_note(ctx, path)
        created = True
    value: str | None = (text or "").strip() or None
    if created and ctx.dry_run:
        return _rehearse_index_note(ctx, target, value)
    result = update_frontmatter(ctx, target, {"description": value})
    if not created:
        return result
    return replace(result, details={**result.details, "created": True})


#: Seed body for an index note :func:`set_description` had to create. Only a
#: heading: the note exists to hold the folder's description, and inventing
#: any more content would be this module writing prose into Matt's vault.
_INDEX_NOTE_SEED = "# {name}\n"


def _create_index_note(ctx: OperationContext, folder: Path) -> Path:
    """``<folder>/index.md``, created empty-but-for-a-heading so
    :func:`set_description` can describe it through the ordinary recorded
    path (Phase-4 routes-apply ruling). Human actors only."""
    folder = require_in_vault(ctx.config, folder, "description target")
    if not folder.is_dir():
        raise OperationError(
            f"cannot describe {folder}: no such file or folder in the vault",
            hint="describe an existing note or an existing folder — spec 11 §3 stores the "
            "description in the note's (or the folder's index note's) frontmatter",
        )
    if is_ai_actor(ctx.actor):
        raise OperationError(
            f"{folder} has no index note and actor {ctx.actor!r} is automated tooling; "
            "refusing to create one",
            hint=f"a human actor (actor='matt') creates {folder / 'index.md'} by describing the "
            "folder, or add the path to the [descriptions] table in config.toml, which the core "
            "reads but never writes (spec 11 §3)",
        )
    target = folder / "index.md"
    if ctx.dry_run:
        # Nothing is written, so there is nothing for update_frontmatter to
        # read back. The caller still gets a truthful rehearsal below.
        return target
    try:
        atomic_write(target, _INDEX_NOTE_SEED.format(name=folder.name))
    except OSError as exc:
        raise OperationError(
            f"could not create {target}: {exc}",
            hint="the description is stored in the folder's index note (spec 11 §3); check the "
            "folder is writable",
        ) from exc
    return target


def _rehearse_index_note(
    ctx: OperationContext, target: Path, value: str | None
) -> OperationResult:
    """Dry-run form of "create the index note, then describe it".

    The note was NOT created (a dry run writes no vault byte), so there is no
    file for :func:`update_frontmatter` to read, diff or record against — and
    calling it anyway would report ``ok=False, "note does not exist"`` for an
    operation that would in fact succeed, which is the silently-wrong answer
    09 §1.5 forbids. The rehearsal therefore renders its own ``[DRY-RUN]``
    operations-log line (09 §5.6 wants the intended action on paper) and emits
    no ActionRecord: a record needs a before-state, and this file has none
    until a real run creates it.
    """
    now = ctx.clock()
    if ctx.config.file_ops.log_operations:
        ctx.oplog.append(
            LoggedOperation(
                ts=_iso(now),
                type="metadata",
                src=str(target),
                dst=str(target),
                success=True,
                dry_run=True,
            )
        )
    return OperationResult(
        ok=True,
        operation="metadata",
        source=str(target),
        destination=str(target),
        dry_run=True,
        details={"changed": True, "keys": ["description"], "created": True},
    )
