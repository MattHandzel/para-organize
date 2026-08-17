"""Learning records: format, decay, scoring readback (spec 04 §3-6).

DESIGN CONSTRAINT (spec 09 §3): everything here is a PURE FUNCTION over
:class:`LearningData` + an explicit ``now`` — no I/O or clock inside the
math — so numeric goldens are trivial. Only :func:`load_learning` /
:func:`save_learning` touch disk.

Persistence: ``CorePaths.learning_path`` (learning.json), schema below
(spec 04 §3), ``schema_version: 1``, written atomically. Loading malformed
or legacy JSON DEGRADES TO EMPTY DATA with a loud warning — it never
crashes scoring (04 §3 ⚠). The live file is virgin (total_moves: 0); no
migration (09 §5.3).

One write path, two readers (spec 12 §2 "Uses" #2): ``record_move`` is
driven from the ActionRecord pipeline — the action log is the source of
truth, learning.json is a derived view. :func:`record_action` is that entry
point, and the composition roots call it from the recorder's own success
callback, never in parallel with it. ``record_move`` itself stays public and
pure for tests and for callers that already hold a ``NoteRecord``.

The corpus is WIDER than the learner: doc 12 §2 records every state-changing
operation by every actor, while doc 04 learns only from Matt's own decisions
(ARCHITECTURE ruling 4ffef89, "LEARNING FOLDS ONLY MATT-DECIDED ACTIONS").
:func:`is_matt_decided` is that gate, and it lives HERE — on the one write
path — so that a composition root can wire ``OperationContext.on_record``
unconditionally without every caller re-deriving the rule.

Implementation notes (deviations from the scaffold, disclosed to the
integrator):

* The ``capture`` parameter of :func:`extract_features`,
  :func:`record_move`, :func:`get_association_score` and
  :func:`get_pattern_score` is annotated :class:`CaptureLike` rather than
  ``NoteRecord``.  This is a *widening* only — ``NoteRecord`` satisfies it —
  and it is required because ``suggest.calculate_score`` holds a
  ``CaptureFeaturesView``, not an index record, and the scaffold's
  ``calculate_score`` docstring mandates that it call
  :func:`get_association_score`.  Association keys are byte-identical for
  either input (both expose raw ``tags``/``sources``/``modalities``).
* ``days_since`` is clamped at zero.  ``last_used`` is always set to ``now``
  at record time, so a negative age can only come from clock skew or
  imported data; without the clamp ``recency_decay ** negative_days``
  *inflates* the learned signal without bound.
* Spec 04 §6 says ``import(data)`` "returns boolean". :func:`import_data`
  returns ``LearningData | None`` instead — a strict WIDENING of that
  contract (``None`` is the falsey failure value, so every boolean-style
  caller still works) which additionally hands back the parsed data, so the
  caller does not have to re-parse the same dict to use it. Disclosed here
  rather than silently diverging; ``clear()`` from the same §6 list is
  implemented literally.
* :func:`save_learning` writes atomically via a private helper rather than
  ``fileops.atomic_write`` — importing ``fileops`` here would add an edge
  the ARCHITECTURE dependency table does not grant ``learn`` (see
  seam requests).
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from organize_core.config import LearningConfig
from organize_core.index import NoteRecord

_log = logging.getLogger(__name__)

LEARNING_SCHEMA_VERSION = 1

GENERIC_KEY = "generic"  # association key when all feature parts are empty

SECONDS_PER_DAY = 24 * 60 * 60

#: How often :func:`maybe_apply_decay` lets decay actually run — spec 04 §3
#: step 6 says "at most once per session or per day", and a day is the
#: cheaper of the two to define without holding session state.
DECAY_INTERVAL_SECONDS = SECONDS_PER_DAY

# Pattern-count normalizer and the weight the learned score gives the
# pattern term (spec 04 §4: "count / 100, capped at 1.0" and "+ 0.5 *
# pattern_score").
_PATTERN_COUNT_DIVISOR = 100.0
_PATTERN_TERM_WEIGHT = 0.5
_PATTERN_SCORE_CAP = 1.0

# Spec 04 §4: the frequency boost applies when count > 5.
_FREQUENCY_BOOST_THRESHOLD = 5


class CaptureLike(Protocol):
    """What learning reads off a capture.

    Satisfied by :class:`organize_core.index.NoteRecord` (the record path)
    and by ``suggest.CaptureFeaturesView`` (the scoring path).  Declared with
    read-only properties so both ``list[str]`` and ``tuple[str, ...]``
    attributes conform.
    """

    @property
    def tags(self) -> Sequence[str]: ...

    @property
    def sources(self) -> Sequence[str]: ...

    @property
    def modalities(self) -> Sequence[str]: ...

    @property
    def context(self) -> Sequence[str]: ...


@dataclass
class DestinationStat:
    """``associations[key].destinations[dest_path]`` entry (spec 04 §3)."""

    count: int = 0
    first_used: float = 0.0
    last_used: float = 0.0
    success_rate: float = 1.0  # parity: written, never adjusted (04 §3)


@dataclass
class Association:
    created_at: float = 0.0
    last_used: float = 0.0
    destinations: dict[str, DestinationStat] = field(default_factory=dict)


@dataclass
class PatternStat:
    """``patterns["tag:<t>->dest:<path>"]`` / ``source:`` entry (04 §3)."""

    count: int = 0
    created_at: float = 0.0
    last_seen: float = 0.0


@dataclass
class Statistics:
    """Lifetime counters. NOT reset by decay (spec 04 §5 resolution —
    zeroing them would break frequency_score denominators)."""

    total_moves: int = 0
    destinations: dict[str, int] = field(default_factory=dict)
    last_updated: float = 0.0


@dataclass
class LearningData:
    schema_version: int = LEARNING_SCHEMA_VERSION
    associations: dict[str, Association] = field(default_factory=dict)
    patterns: dict[str, PatternStat] = field(default_factory=dict)
    statistics: Statistics = field(default_factory=Statistics)


@dataclass(frozen=True)
class Features:
    """``extract_features`` output (spec 04 §3.1): tags/sources/modalities
    SORTED for order-independent keys."""

    tags: tuple[str, ...]
    sources: tuple[str, ...]
    modalities: tuple[str, ...]
    has_context: bool
    word_count: int


# --- key builders (byte-exact, spec 04 §3.2 / §3.4) ------------------------


def tag_pattern_key(tag: str, dest_path: str) -> str:
    """``tag:<tag>->dest:<dest_path>`` (spec 04 §3.4)."""
    return f"tag:{tag}->dest:{dest_path}"


def source_pattern_key(source: str, dest_path: str) -> str:
    """``source:<source>->dest:<dest_path>`` (spec 04 §3.4)."""
    return f"source:{source}->dest:{dest_path}"


# --- persistence -----------------------------------------------------------


def load_learning(path: Path) -> LearningData:
    """Load learning.json; missing/malformed/legacy ⇒ empty LearningData +
    loud warning, never an exception (spec 04 §3)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return LearningData()
    except OSError as exc:
        _log.warning("learning: cannot read %s (%s) — starting from empty data", path, exc)
        return LearningData()

    try:
        raw = json.loads(text)
    except (ValueError, RecursionError) as exc:
        _log.warning(
            "learning: %s is not valid JSON (%s) — degrading to empty data (spec 04 §3)",
            path,
            exc,
        )
        return LearningData()

    if not isinstance(raw, dict):
        _log.warning(
            "learning: %s does not contain a JSON object — degrading to empty data", path
        )
        return LearningData()

    version = raw.get("schema_version")
    if version != LEARNING_SCHEMA_VERSION:
        _log.warning(
            "learning: %s has schema_version %r (expected %d) — legacy/unknown format, "
            "degrading to empty data (spec 04 §3)",
            path,
            version,
            LEARNING_SCHEMA_VERSION,
        )
        return LearningData()

    data = _data_from_raw(raw)
    if data is None:
        _log.warning("learning: %s is structurally malformed — degrading to empty data", path)
        return LearningData()
    return data


def save_learning(path: Path, data: LearningData) -> None:
    """Atomic write (temp + rename, spec 05 §1.3)."""
    payload = export_data(data)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _atomic_write_text(path, text)


def _atomic_write_text(path: Path, text: str) -> None:
    """Temp file in the destination directory + ``os.replace`` (spec 05 §1.3).

    Same-directory temp keeps the rename on one filesystem; the temp file is
    removed if anything fails so we never leak partial state files.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# --- feature extraction ----------------------------------------------------


def extract_features(capture: CaptureLike) -> Features:
    """Spec 04 §3.1."""
    tags = _sorted_strings(getattr(capture, "tags", ()))
    sources = _sorted_strings(getattr(capture, "sources", ()))
    modalities = _sorted_strings(getattr(capture, "modalities", ()))
    has_context = bool(getattr(capture, "context", ()) or ())
    title = getattr(capture, "title", "") or ""
    word_count = len(str(title).split())
    return Features(
        tags=tags,
        sources=sources,
        modalities=modalities,
        has_context=has_context,
        word_count=word_count,
    )


def create_association_key(features: Features) -> str:
    """``"tags:a,b|sources:c|modalities:d"`` — non-empty parts joined by
    ``|``; all empty ⇒ ``"generic"`` (spec 04 §3.2)."""
    parts: list[str] = []
    if features.tags:
        parts.append("tags:" + ",".join(features.tags))
    if features.sources:
        parts.append("sources:" + ",".join(features.sources))
    if features.modalities:
        parts.append("modalities:" + ",".join(features.modalities))
    if not parts:
        return GENERIC_KEY
    return "|".join(parts)


# --- recording -------------------------------------------------------------


def record_move(
    data: LearningData,
    capture: CaptureLike,
    destination_path: str,
    *,
    now: float,
) -> LearningData:
    """Record one accepted move/merge (spec 04 §3): upsert association +
    destination stat, upsert per-tag and per-source patterns, bump
    statistics. Mutates and returns ``data``; the CALLER persists (and
    applies decay at most once per session/day — 04 §3.6, never per move).
    """
    key = create_association_key(extract_features(capture))

    association = data.associations.get(key)
    if association is None:
        association = Association(created_at=now, last_used=now)
        data.associations[key] = association

    stat = association.destinations.get(destination_path)
    if stat is None:
        stat = DestinationStat(count=0, first_used=now, last_used=now, success_rate=1.0)
        association.destinations[destination_path] = stat

    stat.count += 1
    stat.last_used = now
    association.last_used = now

    _record_patterns(data, capture, destination_path, now=now)

    data.statistics.total_moves += 1
    data.statistics.destinations[destination_path] = (
        data.statistics.destinations.get(destination_path, 0) + 1
    )
    # ``now`` is the only clock this module ever sees; save_learning is pure
    # serialization, so the lifetime timestamp is stamped here.
    data.statistics.last_updated = now
    return data


def _record_patterns(
    data: LearningData,
    capture: CaptureLike,
    destination_path: str,
    *,
    now: float,
) -> None:
    """Spec 04 §3.4 — one pattern per tag and per source, keyed byte-exactly."""
    keys = [tag_pattern_key(str(tag), destination_path) for tag in getattr(capture, "tags", ())]
    keys += [
        source_pattern_key(str(source), destination_path)
        for source in getattr(capture, "sources", ())
    ]
    for key in keys:
        pattern = data.patterns.get(key)
        if pattern is None:
            pattern = PatternStat(count=0, created_at=now, last_seen=now)
            data.patterns[key] = pattern
        pattern.count += 1
        pattern.last_seen = now


# --- the ActionRecord pipeline is the write path (spec 12 §2 "Uses" #2) ----

#: Operations that put a capture somewhere and therefore teach the ranker
#: (spec 03 §6's outcome table, spec 04 §3 "every successful accept/move/merge").
LEARNED_OPERATIONS: frozenset[str] = frozenset({"move", "merge", "append", "integrate"})

#: ``targets[].role`` values that name where the capture LANDED, best first.
_DESTINATION_ROLES: tuple[str, ...] = ("destination", "merge_target", "append_target")

#: Actors that ARE Matt at a keyboard (doc 12 §2's actor enum, plus the
#: ``human:*`` namespace ``fileops.is_ai_actor`` already honors).
#:
#: Duplicated rather than imported: ARCHITECTURE ruling 15 (Phase-1 close)
#: REJECTED a ``learn -> fileops`` edge — "making the pure scoring/learning
#: module import the whole mutation layer (which pulls in ``actions`` and
#: ``index``) to reuse ~15 lines would cost more than the duplication:
#: structural decision 3 ('scoring/learning are pure') stops being
#: enforceable". ``tests/test_learn.py`` pins that this set and
#: ``fileops.is_ai_actor`` answer identically for every actor in the doc 12 §2
#: enum, so the duplication cannot drift silently.
HUMAN_ACTORS: frozenset[str] = frozenset({"matt", "user", "human"})

#: Prefix reserved for named humans (``human:matt``), mirroring
#: ``fileops.is_ai_actor``.
_HUMAN_ACTOR_PREFIX = "human:"

#: The one AUTOMATED actor whose records can still be Matt-decided, and only
#: via :data:`MATT_DECIDED_VERDICTS`. Doc 12 §1's ``integrate`` review gate is
#: a human decision wearing a machine's name: Claude proposes, the nvim client
#: shows the unified diff and Matt presses ``<CR>`` (accepted) or edits the
#: proposal first (edited). ARCHITECTURE, "Phase-4 rulings, bind + actor
#: batch" (4ffef89): "integrate records use the verdict-based reading (verdict
#: accepted/edited = Matt-decided even though actor is claude-integrate)".
#:
#: Deliberately NOT extended to ``auto-organize``: spec 13 §2's trust ladder
#: has an ``auto_below`` rung that applies WITHOUT asking, and a machine-set
#: ``verdict`` on that path would make doc-13 proposals self-reinforcing —
#: exactly the failure the routes ruling names. Phase 6 owns that decision
#: (raised as a seam); until it rules, the safe answer is "does not fold".
LLM_EDIT_ACTORS: frozenset[str] = frozenset({"claude-integrate"})

#: ``llm.verdict`` values that mean a human approved the proposed edit
#: (spec 12 §2). ``"rejected"`` is excluded for the same reason
#: ``partial_failure`` is: 12 §3 says a rejected proposal leaves "target
#: untouched", so the destination is not where anything landed.
MATT_DECIDED_VERDICTS: frozenset[str] = frozenset({"accepted", "edited"})


def actor_is_human(actor: Any) -> bool:
    """True when ``actor`` names Matt at a keyboard (doc 12 §2 actor enum).

    The complement of ``fileops.is_ai_actor`` — ``consumer:<name>``,
    ``route:<name>``, ``auto-organize`` and ``claude-integrate`` are all
    automated tooling. See :data:`HUMAN_ACTORS` for why this is not imported.
    """
    name = str(actor or "").strip().lower()
    return name in HUMAN_ACTORS or name.startswith(_HUMAN_ACTOR_PREFIX)


def is_matt_decided(record: Any) -> bool:
    """Did MATT decide this action? The doc-04 learning gate (spec 12 §2).

    ARCHITECTURE, "Phase-4 rulings, bind + actor batch" (4ffef89), verbatim:

        **LEARNING FOLDS ONLY MATT-DECIDED ACTIONS** (principle recorded for
        Phase 5's learn.record_action filter): a route firing is config, not
        a decision — folding it would make routes self-reinforcing and
        corrupt the accept-rate corpus. […] Phase-5 nuances deferred:
        interactive route acceptance in the UI (actor matt) folds as a normal
        accept; integrate records use the verdict-based reading (verdict
        accepted/edited = Matt-decided even though actor is claude-integrate).

    Three answers, in order:

    1. A record carrying an ``llm`` trace whose ``verdict`` is not
       ``accepted``/``edited`` NEVER folds, whatever the actor. Spec 12 §3:
       a rejected proposal leaves the "target untouched", so teaching it
       would steer the next capture at a destination nothing was written to
       — the same reasoning that already excludes ``partial_failure``.
    2. A HUMAN actor folds. Interactive route acceptance in the UI arrives
       here as ``actor: "matt"`` and is an ordinary accept.
    3. An actor in :data:`LLM_EDIT_ACTORS` folds only WITH such a verdict —
       the review gate is the human decision. Every other automated actor
       (``consumer:*``, ``route:*``, ``auto-organize``) never folds.

    Why this lives in the CALLEE rather than in each caller: while it was a
    caller-side choice, ``tag_router`` had to build its own
    ``OperationContext`` with ``on_record`` unset just to stay out of
    learning.json (ARCHITECTURE Phase-4 landing, "Omission ACCEPTED as a
    Phase-5 rider"), which meant a second index writer and a second recorder
    in the pipeline. With the rule here, every composition root can wire
    ``on_record`` unconditionally and the filter cannot be forgotten by the
    next caller.
    """
    llm = getattr(record, "llm", None)
    verdict = getattr(llm, "verdict", None) if llm is not None else None
    if verdict is not None and str(verdict) not in MATT_DECIDED_VERDICTS:
        return False

    actor = getattr(record, "actor", "")
    if actor_is_human(actor):
        return True
    return verdict is not None and str(actor or "").strip().lower() in LLM_EDIT_ACTORS


@dataclass(frozen=True)
class ActionCapture:
    """A :class:`CaptureLike` rebuilt from an ActionRecord's ``capture`` block.

    The association key depends only on ``tags``/``sources``/``modalities``
    (see :func:`create_association_key`), and all three come straight from
    the record's ``frontmatter_before`` — so a key derived here is
    byte-identical to one derived from the ``NoteRecord``, provided the same
    scalar⇒list coercion is applied. That coercion is `index._string_list`'s
    rule, reproduced by :func:`_frontmatter_list` rather than imported so
    this module keeps its "pure over LearningData" shape.
    """

    tags: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    title: str = ""


def _frontmatter_list(fields: Any, key: str) -> tuple[str, ...]:
    """``Frontmatter.get_list`` + ``index._string_list``, over a plain dict."""
    if not isinstance(fields, dict) or key not in fields:
        return ()
    value = fields[key]
    if value is None:
        return ()
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        if item is None:
            continue
        text = (item if isinstance(item, str) else str(item)).strip()
        if text:
            out.append(text)
    return tuple(out)


def capture_from_action(record: Any) -> ActionCapture:
    """The :class:`CaptureLike` view of an ActionRecord's capture block."""
    capture = getattr(record, "capture", None)
    fields = getattr(capture, "frontmatter_before", None)
    return ActionCapture(
        tags=_frontmatter_list(fields, "tags"),
        sources=_frontmatter_list(fields, "sources"),
        modalities=_frontmatter_list(fields, "modalities"),
        context=_frontmatter_list(fields, "context"),
        title=str((fields or {}).get("title") or "") if isinstance(fields, dict) else "",
    )


def destinations_from_action(record: Any) -> list[str]:
    """EVERY destination FOLDER an ActionRecord filed its capture into.

    Multi-file is first-class in the corpus (12 §2): one capture can be filed
    into several folders by several routes, and each of those is a separate
    piece of evidence about where that kind of capture belongs. Reading only
    the first under-taught the learner on every multi-destination action.

    Folders, not files: ``suggest`` scores folder candidates coming out of
    ``VaultIndex.para_subfolders``, so an association keyed by a file path
    would never read back (spec 03 §6 "record_move fires with the target's
    folder"). De-duplicated with order preserved, so two targets landing in
    ONE folder are one piece of evidence, not two.
    """
    out: list[str] = []
    targets = list(getattr(record, "targets", ()) or ())
    for role in _DESTINATION_ROLES:
        for target in targets:
            if getattr(target, "role", None) != role:
                continue
            path = str(getattr(target, "path", "") or "")
            if not path:
                continue
            folder = str(Path(path).parent)
            if folder not in out:
                out.append(folder)
    return out


def destination_from_action(record: Any) -> str | None:
    """The FIRST destination folder of an ActionRecord, or ``None``.

    Kept for callers that genuinely want a single answer;
    :func:`record_action` folds ALL of them via
    :func:`destinations_from_action`.
    """
    destinations = destinations_from_action(record)
    return destinations[0] if destinations else None


def record_action(data: LearningData, record: Any, *, now: float) -> LearningData | None:
    """Fold one ActionRecord into ``data``; ``None`` when it teaches nothing.

    THE learning write path (spec 12 §2 "Uses" #2: "doc 04's learning layer
    records through this same pipeline — one write path, two readers"). The
    op handlers used to call ``record_move`` directly, in parallel with and
    independent of ``ActionRecorder.record``, so the two stores could
    disagree: a move whose ActionRecord was LOST still updated learning.json,
    and a move fed in through ``organize record`` never reached the learner
    at all.

    Skipped: actions MATT DID NOT DECIDE (see :func:`is_matt_decided`), dry
    runs (a rehearsal is not a precedent — the same rule every other corpus
    reader applies), PARTIALLY-APPLIED operations, operations outside
    :data:`LEARNED_OPERATIONS`, and records with no destination target.

    Spec 04 §33 records "on every **successful** accept/move/merge". A
    partially-applied move copied the note but never archived the original,
    so the destination is not where the note ended up — and teaching it makes
    the NEXT capture more likely to be steered at a destination that just
    failed. Three failed moves against one folder produced an association
    with ``count: 3, success_rate: 1.0``, and every retry compounded it.
    """
    # WHO decided, first: an unattended route firing is CONFIG, and folding it
    # would make routes self-reinforcing (ARCHITECTURE ruling 4ffef89,
    # "LEARNING FOLDS ONLY MATT-DECIDED ACTIONS"). Enforced here rather than
    # by each caller so no composition root can forget it.
    if not is_matt_decided(record):
        return None
    context = getattr(record, "context", None)
    if bool(getattr(context, "dry_run", False)):
        return None
    if getattr(context, "partial_failure", None) is not None:
        return None
    if str(getattr(record, "operation", "")) not in LEARNED_OPERATIONS:
        return None
    # EVERY destination, not just the first: a capture filed into two folders
    # is two pieces of evidence about where that kind of capture goes, and
    # folding one silently under-recorded every multi-destination action.
    destinations = destinations_from_action(record)
    if not destinations:
        return None
    capture = capture_from_action(record)
    for destination in destinations:
        data = record_move(data, capture, destination, now=now)
    return data


# --- score readback (feeds suggest signal #3) ------------------------------


def get_association_score(
    data: LearningData,
    capture: CaptureLike,
    dest_path: str,
    config: LearningConfig,
    *,
    now: float,
) -> float:
    """Spec 04 §4, exactly:

        frequency_score      = log(1+count) / log(1+total_moves)
                               (total_moves == 0 ⇒ contribute EXACTLY 0 —
                                the NaN guard, 08 §A22, regression-tested)
        recency_multiplier   = recency_decay ** days_since(last_used)
        frequency_multiplier = frequency_boost if count > 5 else 1.0
        score = frequency_score * recency_multiplier * frequency_multiplier
                + 0.5 * pattern_score

    Unknown destination ⇒ exactly 0. Malformed entries ⇒ 0, never raise.

    ``total_moves == 0`` short-circuits the WHOLE function, pattern term
    included. §4's pseudocode adds the pattern term unconditionally, but
    §7 bullet 6 is an acceptance gate and says "``total_moves==0`` ⇒
    association score contributes **exactly 0 to every candidate**"; the
    gate wins (CLAUDE.md: the acceptance lists are mandatory). This state
    is reachable in production — ``load_learning`` degrades a corrupt
    ``statistics`` block to ``Statistics()`` while keeping parsed patterns —
    so without the short-circuit a vault with a damaged learning.json still
    scored learned destinations.
    """
    score = 0.0
    total_moves = _as_int(getattr(data.statistics, "total_moves", 0))

    # 08 §A22: log(1 + 0) == 0 ⇒ division by zero ⇒ NaN poisoning every
    # candidate.  Guard the DENOMINATOR, not the result.
    if total_moves <= 0:
        return 0.0

    key = create_association_key(extract_features(capture))
    association = data.associations.get(key)
    stat = association.destinations.get(dest_path) if association is not None else None
    if stat is not None:
        try:
            count = _as_int(stat.count)
            if count > 0:
                denominator = math.log(1 + total_moves)
                frequency_score = math.log(1 + count) / denominator
                days = max(0.0, (now - _as_float(stat.last_used)) / SECONDS_PER_DAY)
                recency_multiplier = float(config.recency_decay) ** days
                frequency_multiplier = (
                    float(config.frequency_boost)
                    if count > _FREQUENCY_BOOST_THRESHOLD
                    else 1.0
                )
                score = frequency_score * recency_multiplier * frequency_multiplier
        except (ArithmeticError, TypeError, ValueError):
            # "Malformed entries ⇒ 0, never raise" (spec 04 §4).
            _log.warning(
                "learning: unusable association entry for %s — contributing 0", dest_path
            )
            score = 0.0

    score += _PATTERN_TERM_WEIGHT * get_pattern_score(data, capture, dest_path)
    if not math.isfinite(score):  # belt-and-braces: never let NaN/inf escape
        _log.warning("learning: non-finite association score for %s — contributing 0", dest_path)
        return 0.0
    return score


def get_pattern_score(data: LearningData, capture: CaptureLike, dest_path: str) -> float:
    """Σ over the capture's tag/source pattern keys matching ``dest_path``
    of ``count / 100``, capped at 1.0 (spec 04 §4)."""
    score = 0.0
    keys = [tag_pattern_key(str(tag), dest_path) for tag in getattr(capture, "tags", ())]
    keys += [
        source_pattern_key(str(source), dest_path) for source in getattr(capture, "sources", ())
    ]
    for key in keys:
        pattern = data.patterns.get(key)
        if pattern is None:
            continue
        score += _as_int(pattern.count) / _PATTERN_COUNT_DIVISOR
    if not math.isfinite(score):
        return 0.0
    return min(score, _PATTERN_SCORE_CAP)


# --- decay / eviction ------------------------------------------------------


def apply_decay(data: LearningData, config: LearningConfig, *, now: float) -> LearningData:
    """Spec 04 §5: delete associations with ``last_used`` (and patterns with
    ``last_seen``) older than ``eviction_days`` (90); if associations exceed
    ``max_history`` (1000), evict oldest by last_used. Statistics untouched.
    Called periodically by the session/CLI layer, NOT per move."""
    max_age = float(config.eviction_days) * SECONDS_PER_DAY

    for key in [
        key
        for key, association in data.associations.items()
        if (now - _as_float(association.last_used)) > max_age
    ]:
        del data.associations[key]

    for key in [
        key
        for key, pattern in data.patterns.items()
        if (now - _as_float(pattern.last_seen)) > max_age
    ]:
        del data.patterns[key]

    max_history = int(config.max_history)
    if max_history >= 0 and len(data.associations) > max_history:
        # Oldest first; key ascending breaks ties so eviction is deterministic
        # (the original relied on Lua's unordered pairs()).
        ordered = sorted(
            data.associations.items(), key=lambda item: (_as_float(item[1].last_used), item[0])
        )
        for key, _association in ordered[: len(data.associations) - max_history]:
            del data.associations[key]

    return data


def maybe_apply_decay(data: LearningData, config: LearningConfig, *, now: float) -> LearningData:
    """Spec 04 §3 step 6: apply decay **at most once per session or per day**,
    never per move. THE scheduling owner — every composition root calls this
    (not :func:`apply_decay`) right before it records a move, so the 90-day
    eviction and the ``max_history`` cap actually run in production instead of
    being a function only the unit tests ever call (08 §A23).

    The gate is ``statistics.last_updated``, which ``record_move`` sets to
    ``now``; call this BEFORE recording or the gate never opens. A virgin
    file has ``last_updated == 0`` and therefore decays on the first move,
    which is a no-op on empty data.
    """
    last_updated = _as_float(getattr(data.statistics, "last_updated", 0.0))
    if now - last_updated < DECAY_INTERVAL_SECONDS:
        return data
    return apply_decay(data, config, now=now)


def clear() -> LearningData:
    """Spec 04 §6 ``clear()`` — a fresh, empty :class:`LearningData`.

    Returns new state rather than mutating in place because everything in
    this module is pure over ``LearningData`` (spec 09 §3); the caller
    persists with :func:`save_learning`, exactly as with
    :func:`record_move` / :func:`apply_decay`.
    """
    return LearningData()


# --- introspection API (spec 04 §6 parity) ---------------------------------


def get_statistics(data: LearningData) -> Statistics:
    """A COPY of the lifetime counters — callers must not be able to mutate
    learning state through the introspection API (parity: the original
    deep-copied)."""
    return Statistics(
        total_moves=data.statistics.total_moves,
        destinations=dict(data.statistics.destinations),
        last_updated=data.statistics.last_updated,
    )


def get_top_destinations(data: LearningData, n: int) -> list[dict[str, Any]]:
    """``[{"path": ..., "count": ...}]`` ordered by count desc (04 §6)."""
    if n <= 0:
        return []
    ordered = sorted(
        data.statistics.destinations.items(), key=lambda item: (-item[1], item[0])
    )
    return [{"path": path, "count": count} for path, count in ordered[:n]]


def export_data(data: LearningData) -> dict[str, Any]:
    """JSON-shaped export; round-trips through :func:`import_data`."""
    return {
        "schema_version": data.schema_version,
        "associations": {
            key: {
                "created_at": association.created_at,
                "last_used": association.last_used,
                "destinations": {
                    dest: {
                        "count": stat.count,
                        "first_used": stat.first_used,
                        "last_used": stat.last_used,
                        "success_rate": stat.success_rate,
                    }
                    for dest, stat in association.destinations.items()
                },
            }
            for key, association in data.associations.items()
        },
        "patterns": {
            key: {
                "count": pattern.count,
                "created_at": pattern.created_at,
                "last_seen": pattern.last_seen,
            }
            for key, pattern in data.patterns.items()
        },
        "statistics": {
            "total_moves": data.statistics.total_moves,
            "destinations": dict(data.statistics.destinations),
            "last_updated": data.statistics.last_updated,
        },
    }


def import_data(raw: dict[str, Any]) -> LearningData | None:
    """Returns None on invalid input (boolean-style contract, 04 §6).

    Unlike :func:`load_learning`, a MISSING ``schema_version`` is accepted
    and read as v1: import is an explicit user action on a backup they hand
    over, whereas the automatic load path must degrade rather than guess.
    An explicit non-v1 version is rejected either way.
    """
    if not isinstance(raw, dict):
        return None
    # Parity gate (the original accepted anything carrying both keys).
    if not isinstance(raw.get("associations"), dict) or not isinstance(
        raw.get("statistics"), dict
    ):
        return None
    version = raw.get("schema_version", LEARNING_SCHEMA_VERSION)
    if version != LEARNING_SCHEMA_VERSION:
        return None
    return _data_from_raw(raw)


def analyze_patterns(captures: list[NoteRecord]) -> dict[str, Any]:
    """``{"common_tags": [{"tag", "count"}...], "common_sources": [...]}``
    sorted by count desc (spec 04 §6, restored from d753672~1).

    ``count`` is the number of CAPTURES carrying the tag/source (a tag
    repeated inside one note counts once), which is what "appears in N
    captures" means in §6.
    """
    tag_counts = _count_by_capture(captures, "tags")
    source_counts = _count_by_capture(captures, "sources")
    return {
        "common_tags": [
            {"tag": name, "count": count} for name, count in _sorted_by_count(tag_counts)
        ],
        "common_sources": [
            {"source": name, "count": count} for name, count in _sorted_by_count(source_counts)
        ],
    }


def suggest_new_folders(captures: list[NoteRecord]) -> list[dict[str, Any]]:
    """Folder-name proposals for tags appearing in ≥3 unorganized captures,
    ``confidence = count / len(captures)`` (spec 04 §6)."""
    if not captures:
        return []
    tag_counts = _count_by_capture(captures, "tags")
    total = len(captures)
    proposals = [
        {
            "name": tag,
            "type": "projects",  # parity: the original defaulted to projects
            "reason": f"Tag '{tag}' appears in {count} captures",
            "confidence": count / total,
        }
        for tag, count in tag_counts.items()
        if count >= 3
    ]
    proposals.sort(key=lambda item: (-float(item["confidence"]), str(item["name"])))
    return proposals


# --- internals -------------------------------------------------------------


def _sorted_strings(values: Iterable[Any] | None) -> tuple[str, ...]:
    """Sorted tuple of stringified values (spec 04 §3.1 order-independence)."""
    if not values:
        return ()
    return tuple(sorted(str(value) for value in values))


def _count_by_capture(captures: Sequence[NoteRecord], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for capture in captures:
        seen: set[str] = set()
        for value in getattr(capture, attribute, ()) or ():
            name = str(value)
            if name in seen:
                continue
            seen.add(name)
            counts[name] = counts.get(name, 0) + 1
    return counts


def _sorted_by_count(counts: dict[str, int]) -> list[tuple[str, int]]:
    """Count desc, name ascending — deterministic where Lua's pairs() was not."""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _as_int(value: Any) -> int:
    """Coerce to int, degrading to 0 for anything unusable.

    ``OverflowError`` is caught alongside ``TypeError``/``ValueError``
    because ``json.loads`` happily produces ``float('inf')`` for a literal
    ``Infinity`` or an out-of-range ``1e400``, and ``int(inf)`` raises
    ``OverflowError`` — which used to escape ``load_learning`` as a raw
    traceback, violating spec 04 §3 ⚠ ("loading malformed/legacy JSON must
    degrade to empty data, never crash scoring").
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _as_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _data_from_raw(raw: dict[str, Any]) -> LearningData | None:
    """Lenient structural parse. Individual malformed entries are skipped
    with a warning; only a non-dict top level is unrecoverable."""
    if not isinstance(raw, dict):
        return None

    data = LearningData()

    raw_associations = raw.get("associations")
    if isinstance(raw_associations, dict):
        for key, value in raw_associations.items():
            if not isinstance(value, dict):
                _log.warning("learning: skipping malformed association %r", key)
                continue
            association = Association(
                created_at=_as_float(value.get("created_at", 0.0)),
                last_used=_as_float(value.get("last_used", 0.0)),
            )
            raw_destinations = value.get("destinations")
            if isinstance(raw_destinations, dict):
                for dest, stat in raw_destinations.items():
                    if not isinstance(stat, dict):
                        _log.warning("learning: skipping malformed destination %r", dest)
                        continue
                    association.destinations[str(dest)] = DestinationStat(
                        count=_as_int(stat.get("count", 0)),
                        first_used=_as_float(stat.get("first_used", 0.0)),
                        last_used=_as_float(stat.get("last_used", 0.0)),
                        success_rate=_as_float(stat.get("success_rate", 1.0)),
                    )
            data.associations[str(key)] = association

    raw_patterns = raw.get("patterns")
    if isinstance(raw_patterns, dict):
        for key, value in raw_patterns.items():
            if not isinstance(value, dict):
                _log.warning("learning: skipping malformed pattern %r", key)
                continue
            data.patterns[str(key)] = PatternStat(
                count=_as_int(value.get("count", 0)),
                created_at=_as_float(value.get("created_at", 0.0)),
                last_seen=_as_float(value.get("last_seen", 0.0)),
            )

    raw_statistics = raw.get("statistics")
    if isinstance(raw_statistics, dict):
        destinations: dict[str, int] = {}
        raw_destinations = raw_statistics.get("destinations")
        if isinstance(raw_destinations, dict):
            destinations = {str(k): _as_int(v) for k, v in raw_destinations.items()}
        data.statistics = Statistics(
            total_moves=_as_int(raw_statistics.get("total_moves", 0)),
            destinations=destinations,
            last_updated=_as_float(raw_statistics.get("last_updated", 0.0)),
        )

    return data
