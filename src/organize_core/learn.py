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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from organize_core.config import LearningConfig
from organize_core.index import NoteRecord

LEARNING_SCHEMA_VERSION = 1

GENERIC_KEY = "generic"  # association key when all feature parts are empty


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


# --- persistence -----------------------------------------------------------


def load_learning(path: Path) -> LearningData:
    """Load learning.json; missing/malformed/legacy ⇒ empty LearningData +
    loud warning, never an exception (spec 04 §3)."""
    raise NotImplementedError


def save_learning(path: Path, data: LearningData) -> None:
    """Atomic write (temp + rename, spec 05 §1.3)."""
    raise NotImplementedError


# --- feature extraction ----------------------------------------------------


def extract_features(capture: NoteRecord) -> Features:
    """Spec 04 §3.1."""
    raise NotImplementedError


def create_association_key(features: Features) -> str:
    """``"tags:a,b|sources:c|modalities:d"`` — non-empty parts joined by
    ``|``; all empty ⇒ ``"generic"`` (spec 04 §3.2)."""
    raise NotImplementedError


# --- recording -------------------------------------------------------------


def record_move(
    data: LearningData,
    capture: NoteRecord,
    destination_path: str,
    *,
    now: float,
) -> LearningData:
    """Record one accepted move/merge (spec 04 §3): upsert association +
    destination stat, upsert per-tag and per-source patterns, bump
    statistics. Mutates and returns ``data``; the CALLER persists (and
    applies decay at most once per session/day — 04 §3.6, never per move).
    """
    raise NotImplementedError


# --- score readback (feeds suggest signal #3) ------------------------------


def get_association_score(
    data: LearningData,
    capture: NoteRecord,
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
    raise NotImplementedError


def get_pattern_score(data: LearningData, capture: NoteRecord, dest_path: str) -> float:
    """Σ over the capture's tag/source pattern keys matching ``dest_path``
    of ``count / 100``, capped at 1.0 (spec 04 §4)."""
    raise NotImplementedError


# --- decay / eviction ------------------------------------------------------


def apply_decay(data: LearningData, config: LearningConfig, *, now: float) -> LearningData:
    """Spec 04 §5: delete associations with ``last_used`` (and patterns with
    ``last_seen``) older than ``eviction_days`` (90); if associations exceed
    ``max_history`` (1000), evict oldest by last_used. Statistics untouched.
    Called periodically by the session/CLI layer, NOT per move."""
    raise NotImplementedError


# --- introspection API (spec 04 §6 parity) ---------------------------------


def get_statistics(data: LearningData) -> Statistics:
    raise NotImplementedError


def get_top_destinations(data: LearningData, n: int) -> list[dict[str, Any]]:
    """``[{"path": ..., "count": ...}]`` ordered by count desc (04 §6)."""
    raise NotImplementedError


def export_data(data: LearningData) -> dict[str, Any]:
    """JSON-shaped export; round-trips through :func:`import_data`."""
    raise NotImplementedError


def import_data(raw: dict[str, Any]) -> LearningData | None:
    """Returns None on invalid input (boolean-style contract, 04 §6)."""
    raise NotImplementedError


def analyze_patterns(captures: list[NoteRecord]) -> dict[str, Any]:
    """``{"common_tags": [{"tag", "count"}...], "common_sources": [...]}``
    sorted by count desc (spec 04 §6, restored from d753672~1)."""
    raise NotImplementedError


def suggest_new_folders(captures: list[NoteRecord]) -> list[dict[str, Any]]:
    """Folder-name proposals for tags appearing in ≥3 unorganized captures,
    ``confidence = count / len(captures)`` (spec 04 §6)."""
    raise NotImplementedError
