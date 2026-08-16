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

One write path, two readers (spec 12 §2 "Uses" #2): in the integrated
system, ``record_move`` is driven from the ActionRecord pipeline — the
action log is the source of truth; learning.json is a derived view.

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
    """
    score = 0.0
    total_moves = _as_int(getattr(data.statistics, "total_moves", 0))

    # 08 §A22: log(1 + 0) == 0 ⇒ division by zero ⇒ NaN poisoning every
    # candidate.  Guard the DENOMINATOR, not the result.
    if total_moves > 0:
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
    try:
        return int(value)
    except (TypeError, ValueError):
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
