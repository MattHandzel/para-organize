"""Numeric goldens and regression tests for ``organize_core.learn`` (spec 04 §3-6).

Every expected value in this file is derived BY HAND from the formulae
written out in ``spec/04-suggestions-and-learning.md`` §4 — never by running
``learn.py`` and pasting what it printed.  Where a golden is a log ratio the
literal decimal is asserted AND cross-checked against the spec formula
re-transcribed with :mod:`math`, so a typo in either one fails the test.

Doc-08 regression obligations covered here:

* §A22 ``get_association_score`` divided by ``log(1 + total_moves)`` and
  produced NaN when ``total_moves == 0``, poisoning every candidate.
* §A23 ``apply_decay`` ran on every accepted move; it is now a pure,
  separately-callable function the caller schedules (04 §3.6).
* §A22 "no nil guards on malformed learning data".
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from organize_core import learn as learn_mod
from organize_core.config import LearningConfig
from organize_core.index import NoteRecord
from organize_core.learn import (
    GENERIC_KEY,
    LEARNING_SCHEMA_VERSION,
    Association,
    DestinationStat,
    Features,
    LearningData,
    PatternStat,
    Statistics,
    analyze_patterns,
    apply_decay,
    create_association_key,
    export_data,
    extract_features,
    get_association_score,
    get_pattern_score,
    get_statistics,
    get_top_destinations,
    import_data,
    load_learning,
    record_move,
    save_learning,
    source_pattern_key,
    suggest_new_folders,
    tag_pattern_key,
)

#: Declared HERE, independently, exactly like tests/test_suggest.py does.
#: Importing `learn.SECONDS_PER_DAY` and building every time-based
#: expectation from it made this whole file a tautology: setting the
#: production constant to 3600 ("a day is an hour") left the recency-decay,
#: 30-day and 89/90/91-day eviction goldens ALL green, because both sides of
#: every assertion moved together. Doc 04 §7 bullet 5 then had no genuine
#: independent check anywhere in the suite.
SECONDS_PER_DAY = 24 * 60 * 60

NOW = 1_760_000_000.0  # a fixed epoch; `now` is always injected (spec 09 §3)
DEST = "/vault/projects/project-x"
OTHER_DEST = "/vault/areas/health"


def make_record(
    *,
    path: str = "/vault/capture/raw_capture/c1.md",
    title: str = "A capture note",
    tags: list[str] | None = None,
    sources: list[str] | None = None,
    modalities: list[str] | None = None,
    context: list[str] | None = None,
) -> NoteRecord:
    return NoteRecord(
        path=path,
        filename=Path(path).name,
        title=title,
        para_type="capture",
        folder=Path(path).parent.name,
        tags=list(tags or []),
        sources=list(sources or []),
        modalities=list(modalities or []),
        context=list(context or []),
    )


# --- features and keys (spec 04 §3.1-3.2, byte-exact) ----------------------


def test_a_day_is_a_day() -> None:
    """The one assertion that makes every time-based golden in this file real.

    `learn.SECONDS_PER_DAY` feeds `days_since` in the recency multiplier and
    `eviction_days` in `apply_decay`; nothing else in the module states how
    long a day is, so this is the only thing standing between a typo in that
    constant and a silently-green suite (doc 04 §7 bullets 4 and 5).
    """
    assert learn_mod.SECONDS_PER_DAY == 86400
    assert learn_mod.SECONDS_PER_DAY == SECONDS_PER_DAY
    assert learn_mod.DECAY_INTERVAL_SECONDS == SECONDS_PER_DAY


def test_extract_features_sorts_every_multi_value_field() -> None:
    record = make_record(
        tags=["project", "meeting"],
        sources=["slack", "email"],
        modalities=["voice", "text"],
        context=["standup"],
        title="one two three",
    )
    features = extract_features(record)
    assert features.tags == ("meeting", "project")
    assert features.sources == ("email", "slack")
    assert features.modalities == ("text", "voice")
    assert features.has_context is True
    assert features.word_count == 3


def test_extract_features_is_order_independent() -> None:
    a = extract_features(make_record(tags=["b", "a"], sources=["y", "x"]))
    b = extract_features(make_record(tags=["a", "b"], sources=["x", "y"]))
    assert create_association_key(a) == create_association_key(b)


def test_extract_features_empty_capture() -> None:
    features = extract_features(make_record(title=""))
    assert features == Features(
        tags=(), sources=(), modalities=(), has_context=False, word_count=0
    )


def test_association_key_is_byte_exact() -> None:
    """Spec 04 §3.2 shows this exact string."""
    features = extract_features(
        make_record(tags=["project", "meeting"], sources=["email"])
    )
    assert create_association_key(features) == "tags:meeting,project|sources:email"


def test_association_key_includes_modalities_part_last() -> None:
    features = extract_features(
        make_record(tags=["a"], sources=["b"], modalities=["d", "c"])
    )
    assert create_association_key(features) == "tags:a|sources:b|modalities:c,d"


def test_association_key_omits_empty_parts() -> None:
    assert (
        create_association_key(extract_features(make_record(sources=["email"])))
        == "sources:email"
    )
    assert (
        create_association_key(extract_features(make_record(modalities=["voice"])))
        == "modalities:voice"
    )


def test_association_key_all_empty_is_generic() -> None:
    assert create_association_key(extract_features(make_record())) == GENERIC_KEY
    assert GENERIC_KEY == "generic"


def test_pattern_keys_are_byte_exact() -> None:
    """Spec 04 §3 persistence block shows these exact keys."""
    assert tag_pattern_key("project", DEST) == "tag:project->dest:/vault/projects/project-x"
    assert (
        source_pattern_key("email", DEST) == "source:email->dest:/vault/projects/project-x"
    )


# --- record_move (spec 04 §3.3-3.5, acceptance test 3) ---------------------


def test_record_move_twice_same_destination_exact_counters() -> None:
    """Spec 04 §7: total_moves==2, destinations[dest]==2, pattern counts==2."""
    data = LearningData()
    capture = make_record(tags=["meeting"], sources=["email"])

    record_move(data, capture, DEST, now=NOW)
    record_move(data, capture, DEST, now=NOW + 60)

    assert data.statistics.total_moves == 2
    assert data.statistics.destinations == {DEST: 2}
    assert data.patterns[tag_pattern_key("meeting", DEST)].count == 2
    assert data.patterns[source_pattern_key("email", DEST)].count == 2

    key = "tags:meeting|sources:email"
    assert list(data.associations) == [key]
    stat = data.associations[key].destinations[DEST]
    assert stat.count == 2
    assert stat.first_used == NOW  # first_used never moves
    assert stat.last_used == NOW + 60
    assert stat.success_rate == 1.0
    assert data.associations[key].created_at == NOW
    assert data.associations[key].last_used == NOW + 60


def test_record_move_returns_the_same_object_it_mutates() -> None:
    data = LearningData()
    assert record_move(data, make_record(tags=["x"]), DEST, now=NOW) is data


def test_record_move_does_not_decay_or_touch_disk(tmp_path: Path) -> None:
    """08 §A23: decay used to run inside every accepted move (O(n log n))."""
    data = LearningData()
    stale_age = NOW - 500 * SECONDS_PER_DAY
    data.associations["tags:ancient"] = Association(
        created_at=stale_age, last_used=stale_age
    )

    record_move(data, make_record(tags=["fresh"]), DEST, now=NOW)

    assert "tags:ancient" in data.associations, "record_move must not apply decay"
    assert list(tmp_path.iterdir()) == [], "record_move must not persist anything"


def test_record_move_separate_destinations_share_one_association() -> None:
    data = LearningData()
    capture = make_record(tags=["meeting"])
    record_move(data, capture, DEST, now=NOW)
    record_move(data, capture, OTHER_DEST, now=NOW)

    assert list(data.associations) == ["tags:meeting"]
    assert set(data.associations["tags:meeting"].destinations) == {DEST, OTHER_DEST}
    assert data.statistics.total_moves == 2
    assert data.statistics.destinations == {DEST: 1, OTHER_DEST: 1}


def test_record_move_stamps_last_updated_from_injected_now() -> None:
    data = LearningData()
    record_move(data, make_record(tags=["x"]), DEST, now=NOW)
    assert data.statistics.last_updated == NOW


# --- association score goldens (spec 04 §4, acceptance tests 4 and 6) ------


def _seeded(count: int, *, total_moves: int, last_used: float) -> LearningData:
    """One association ``tags:meeting`` → DEST with the given counters."""
    data = LearningData()
    data.associations["tags:meeting"] = Association(
        created_at=last_used,
        last_used=last_used,
        destinations={
            DEST: DestinationStat(
                count=count, first_used=last_used, last_used=last_used, success_rate=1.0
            )
        },
    )
    data.statistics = Statistics(total_moves=total_moves, destinations={DEST: count})
    return data


@pytest.fixture()
def meeting_capture() -> NoteRecord:
    return make_record(tags=["meeting"])


def test_unknown_destination_scores_exactly_zero(meeting_capture: NoteRecord) -> None:
    """Spec 04 §7: unknown destination association score == 0 exactly."""
    data = _seeded(3, total_moves=10, last_used=NOW)
    score = get_association_score(
        data, meeting_capture, "/vault/projects/never-seen", LearningConfig(), now=NOW
    )
    assert score == 0.0


def test_fresh_association_golden_value(meeting_capture: NoteRecord) -> None:
    """count=3, total_moves=10, 0 days old, decay 0.9, no boost.

    doc 04 §4:  log(1+3)/log(1+10) * 0.9**0 * 1.0
              = 1.3862943611198906 / 2.3978952727983707
              = 0.5781296526357756
    """
    data = _seeded(3, total_moves=10, last_used=NOW)
    score = get_association_score(data, meeting_capture, DEST, LearningConfig(), now=NOW)
    assert score == pytest.approx(0.5781296526357756, rel=1e-12)
    assert score == pytest.approx(math.log(4) / math.log(11), rel=1e-12)


def test_frequency_boost_applies_above_five(meeting_capture: NoteRecord) -> None:
    """count=6 > 5 ⇒ ×1.2:  log(7)/log(11) * 1.2 = 0.9738090755486987."""
    data = _seeded(6, total_moves=10, last_used=NOW)
    score = get_association_score(data, meeting_capture, DEST, LearningConfig(), now=NOW)
    assert score == pytest.approx(0.9738090755486987, rel=1e-12)
    assert score == pytest.approx(math.log(7) / math.log(11) * 1.2, rel=1e-12)


def test_frequency_boost_does_not_apply_at_exactly_five(
    meeting_capture: NoteRecord,
) -> None:
    """The gate is ``count > 5``, not ``>=``: log(6)/log(11), unmultiplied."""
    data = _seeded(5, total_moves=10, last_used=NOW)
    score = get_association_score(data, meeting_capture, DEST, LearningConfig(), now=NOW)
    assert score == pytest.approx(math.log(6) / math.log(11), rel=1e-12)


def test_count_six_outranks_count_one(meeting_capture: NoteRecord) -> None:
    """Spec 04 §7: count 6 > count 1 (frequency boost)."""
    six = get_association_score(
        _seeded(6, total_moves=10, last_used=NOW), meeting_capture, DEST,
        LearningConfig(), now=NOW,
    )
    one = get_association_score(
        _seeded(1, total_moves=10, last_used=NOW), meeting_capture, DEST,
        LearningConfig(), now=NOW,
    )
    assert one == pytest.approx(math.log(2) / math.log(11), rel=1e-12)
    assert six > one


def test_thirty_day_old_association_golden_and_below_fresh(
    meeting_capture: NoteRecord,
) -> None:
    """Spec 04 §7: 30-day-old < fresh.

    doc 04 §4:  0.5781296526357756 * 0.9**30
                0.9**30 = 0.04239115827521624   ("worth 4% after 30 days")
              = 0.02450758560847895
    """
    stale = _seeded(3, total_moves=10, last_used=NOW - 30 * SECONDS_PER_DAY)
    score = get_association_score(stale, meeting_capture, DEST, LearningConfig(), now=NOW)
    assert score == pytest.approx(0.02450758560847895, rel=1e-12)
    assert score < 0.5781296526357756


def test_recency_decay_config_is_genuinely_honored(meeting_capture: NoteRecord) -> None:
    """04 §4 decay-curve note: Matt must be able to flatten the curve to 0.99."""
    stale = _seeded(3, total_moves=10, last_used=NOW - 30 * SECONDS_PER_DAY)
    flat = get_association_score(
        stale, meeting_capture, DEST, LearningConfig(recency_decay=0.99), now=NOW
    )
    # 0.99**30 = 0.7397003733882802
    assert flat == pytest.approx(
        0.5781296526357756 * 0.99**30, rel=1e-12
    )
    assert flat == pytest.approx(0.42764271992152, rel=1e-12)


def test_future_last_used_cannot_inflate_the_score(meeting_capture: NoteRecord) -> None:
    """Clock skew must not make ``0.9 ** negative_days`` amplify learning."""
    skewed = _seeded(3, total_moves=10, last_used=NOW + 400 * SECONDS_PER_DAY)
    score = get_association_score(skewed, meeting_capture, DEST, LearningConfig(), now=NOW)
    assert score == pytest.approx(0.5781296526357756, rel=1e-12)


# --- 08 §A22: the NaN regression ------------------------------------------


def test_total_moves_zero_contributes_exactly_zero(meeting_capture: NoteRecord) -> None:
    """08 §A22 / spec 04 §7: total_moves==0 ⇒ contributes exactly 0.

    The original divided by ``log(1 + 0) == 0`` and returned NaN, which
    poisoned EVERY candidate's score (NaN propagates through +, and every
    comparison against NaN is False, so ranking collapsed).
    """
    data = _seeded(3, total_moves=0, last_used=NOW)
    score = get_association_score(data, meeting_capture, DEST, LearningConfig(), now=NOW)
    assert score == 0.0
    assert not math.isnan(score)


def test_total_moves_zero_across_many_candidates_is_all_zero(
    meeting_capture: NoteRecord,
) -> None:
    data = _seeded(3, total_moves=0, last_used=NOW)
    dests = [DEST, OTHER_DEST, "/vault/resources/reading"]
    scores = [
        get_association_score(data, meeting_capture, d, LearningConfig(), now=NOW)
        for d in dests
    ]
    assert scores == [0.0, 0.0, 0.0]
    assert sum(scores) == 0.0  # a single NaN would make this NaN


def test_total_moves_zero_contributes_exactly_zero_including_the_pattern_term(
    meeting_capture: NoteRecord,
) -> None:
    """Spec 04 §7 bullet 6, literally: "`total_moves==0` ⇒ association score
    contributes **exactly 0 to every candidate**".

    §4's pseudocode adds `0.5 * pattern_score` outside the `total_moves`
    guard, so this used to return 0.02 for a pattern-only state. §7 is an
    acceptance gate and wins. The state is reachable in production, not
    synthetic — see the companion test below.
    """
    data = _seeded(3, total_moves=0, last_used=NOW)
    data.patterns[tag_pattern_key("meeting", DEST)] = PatternStat(
        count=4, created_at=NOW, last_seen=NOW
    )
    score = get_association_score(data, meeting_capture, DEST, LearningConfig(), now=NOW)
    assert score == 0.0


def test_total_moves_zero_with_patterns_is_reachable_through_load_learning(
    tmp_path: Path, meeting_capture: NoteRecord
) -> None:
    """The partial-degradation path that makes the bullet above load-bearing:
    a corrupt `statistics` block degrades to `Statistics()` (total_moves 0)
    while the `patterns` table parses cleanly, so a damaged learning.json was
    still steering suggestions."""
    path = tmp_path / "learning.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "associations": {},
                "patterns": {tag_pattern_key("meeting", DEST): {"count": 40, "last_seen": 0}},
                "statistics": "CORRUPT",
            }
        ),
        encoding="utf-8",
    )
    data = load_learning(path)
    assert data.statistics.total_moves == 0
    assert len(data.patterns) == 1
    assert get_association_score(data, meeting_capture, DEST, LearningConfig(), now=NOW) == 0.0


def test_malformed_learning_data_never_raises() -> None:
    """08 §A22 'no nil guards on malformed learning data'."""
    data = LearningData()
    data.associations["tags:meeting"] = Association(
        created_at=NOW,
        last_used=NOW,
        destinations={DEST: DestinationStat(count="not-a-number", last_used=None)},  # type: ignore[arg-type]
    )
    data.statistics = Statistics(total_moves=10)
    score = get_association_score(
        data, make_record(tags=["meeting"]), DEST, LearningConfig(), now=NOW
    )
    assert score == 0.0


# --- pattern score (spec 04 §4) -------------------------------------------


def test_pattern_score_sums_tag_and_source_hits() -> None:
    data = LearningData()
    data.patterns[tag_pattern_key("meeting", DEST)] = PatternStat(count=3)
    data.patterns[source_pattern_key("email", DEST)] = PatternStat(count=2)
    capture = make_record(tags=["meeting"], sources=["email"])
    assert get_pattern_score(data, capture, DEST) == pytest.approx(0.05, rel=1e-12)


def test_pattern_score_is_capped_at_one() -> None:
    data = LearningData()
    data.patterns[tag_pattern_key("meeting", DEST)] = PatternStat(count=250)
    assert get_pattern_score(data, make_record(tags=["meeting"]), DEST) == 1.0


def test_pattern_score_ignores_other_destinations() -> None:
    data = LearningData()
    data.patterns[tag_pattern_key("meeting", OTHER_DEST)] = PatternStat(count=9)
    assert get_pattern_score(data, make_record(tags=["meeting"]), DEST) == 0.0


def test_pattern_term_is_half_weighted_inside_association_score() -> None:
    """doc 04 §4: ``+ 0.5 * pattern_score``."""
    data = _seeded(3, total_moves=10, last_used=NOW)
    data.patterns[tag_pattern_key("meeting", DEST)] = PatternStat(count=3)
    data.patterns[source_pattern_key("email", DEST)] = PatternStat(count=2)
    capture = make_record(tags=["meeting"], sources=["email"])
    # The association key changes once sources exist, so the frequency term
    # is 0 here and the pattern term is measured in isolation: 0.5 * 0.05.
    assert get_association_score(
        data, capture, DEST, LearningConfig(), now=NOW
    ) == pytest.approx(0.025, rel=1e-12)


def test_record_move_then_score_round_trips_through_the_same_key() -> None:
    """The keys written by record_move are the keys read back by scoring."""
    data = LearningData()
    capture = make_record(tags=["meeting"], sources=["email"], modalities=["text"])
    for _ in range(3):
        record_move(data, capture, DEST, now=NOW)
    score = get_association_score(data, capture, DEST, LearningConfig(), now=NOW)
    # frequency: log(4)/log(4) == 1.0 (count 3, total_moves 3); patterns:
    # tag:meeting (3) + source:email (3) = 0.06 → 0.5 * 0.06 = 0.03
    assert score == pytest.approx(1.0 + 0.03, rel=1e-12)


# --- decay / eviction (spec 04 §5, acceptance test 5) ---------------------


def test_apply_decay_removes_hundred_day_old_association_and_keeps_statistics() -> None:
    """Spec 04 §7: apply_decay removes a 100-day-old association; statistics
    unchanged (the old learn_spec asserted total_moves==0 after decay — that
    contradicts the implementation and 04 §5 resolves it: stats persist)."""
    data = LearningData()
    old = NOW - 100 * SECONDS_PER_DAY
    data.associations["tags:old"] = Association(created_at=old, last_used=old)
    data.associations["tags:new"] = Association(created_at=NOW, last_used=NOW)
    data.patterns[tag_pattern_key("old", DEST)] = PatternStat(count=1, last_seen=old)
    data.patterns[tag_pattern_key("new", DEST)] = PatternStat(count=1, last_seen=NOW)
    data.statistics = Statistics(total_moves=7, destinations={DEST: 7}, last_updated=NOW)

    apply_decay(data, LearningConfig(), now=NOW)

    assert list(data.associations) == ["tags:new"]
    assert list(data.patterns) == [tag_pattern_key("new", DEST)]
    assert data.statistics.total_moves == 7
    assert data.statistics.destinations == {DEST: 7}
    assert data.statistics.last_updated == NOW


def test_apply_decay_boundary_is_strictly_older_than_the_horizon() -> None:
    data = LearningData()
    exactly = NOW - 90 * SECONDS_PER_DAY
    one_second_more = exactly - 1
    data.associations["keep"] = Association(last_used=exactly)
    data.associations["drop"] = Association(last_used=one_second_more)

    apply_decay(data, LearningConfig(), now=NOW)

    assert list(data.associations) == ["keep"]


def test_apply_decay_honors_configured_eviction_days() -> None:
    data = LearningData()
    data.associations["a"] = Association(last_used=NOW - 10 * SECONDS_PER_DAY)
    apply_decay(data, LearningConfig(eviction_days=5), now=NOW)
    assert data.associations == {}


def test_apply_decay_evicts_oldest_beyond_max_history_deterministically() -> None:
    data = LearningData()
    for i in range(5):
        data.associations[f"k{i}"] = Association(last_used=NOW - i)
    apply_decay(data, LearningConfig(max_history=3), now=NOW)
    # oldest = largest i; k4 and k3 go.
    assert sorted(data.associations) == ["k0", "k1", "k2"]


def test_apply_decay_ties_evict_by_key_ascending() -> None:
    data = LearningData()
    for key in ("b", "a", "c"):
        data.associations[key] = Association(last_used=NOW)
    apply_decay(data, LearningConfig(max_history=1), now=NOW)
    assert list(data.associations) == ["c"]


def test_apply_decay_patterns_are_only_bounded_by_the_horizon() -> None:
    """Parity (04 §5): max_history caps associations only."""
    data = LearningData()
    for i in range(20):
        data.patterns[f"tag:t{i}->dest:{DEST}"] = PatternStat(count=1, last_seen=NOW)
    apply_decay(data, LearningConfig(max_history=1), now=NOW)
    assert len(data.patterns) == 20


def test_apply_decay_returns_the_same_object() -> None:
    data = LearningData()
    assert apply_decay(data, LearningConfig(), now=NOW) is data


# --- introspection (spec 04 §6) -------------------------------------------


def test_get_statistics_returns_a_defensive_copy() -> None:
    data = LearningData()
    data.statistics = Statistics(total_moves=3, destinations={DEST: 3})
    stats = get_statistics(data)
    assert stats.total_moves == 3
    stats.destinations[DEST] = 999
    assert data.statistics.destinations == {DEST: 3}


def test_get_top_destinations_orders_by_count_desc() -> None:
    data = LearningData()
    data.statistics = Statistics(destinations={"/a": 1, "/b": 5, "/c": 3})
    assert get_top_destinations(data, 2) == [
        {"path": "/b", "count": 5},
        {"path": "/c", "count": 3},
    ]


def test_get_top_destinations_ties_break_on_path() -> None:
    data = LearningData()
    data.statistics = Statistics(destinations={"/z": 2, "/a": 2})
    assert [d["path"] for d in get_top_destinations(data, 2)] == ["/a", "/z"]


def test_get_top_destinations_non_positive_n_is_empty() -> None:
    data = LearningData()
    data.statistics = Statistics(destinations={"/a": 1})
    assert get_top_destinations(data, 0) == []


def test_export_import_round_trip() -> None:
    data = LearningData()
    record_move(data, make_record(tags=["meeting"], sources=["email"]), DEST, now=NOW)
    exported = export_data(data)
    assert exported["schema_version"] == LEARNING_SCHEMA_VERSION
    assert json.loads(json.dumps(exported)) == exported  # JSON-shaped

    restored = import_data(exported)
    assert restored is not None
    assert restored == data


def test_import_data_rejects_invalid_input() -> None:
    assert import_data({}) is None
    assert import_data({"associations": {}}) is None
    assert import_data({"statistics": {}}) is None
    assert import_data({"associations": [], "statistics": {}}) is None
    assert import_data({"associations": {}, "statistics": {}, "schema_version": 99}) is None


def test_import_data_accepts_the_minimal_valid_shape() -> None:
    restored = import_data({"associations": {}, "statistics": {"total_moves": 4}})
    assert restored is not None
    assert restored.statistics.total_moves == 4


def test_analyze_patterns_sorted_by_count_desc() -> None:
    captures = [
        make_record(tags=["meeting", "work"], sources=["email"]),
        make_record(tags=["meeting"], sources=["email"]),
        make_record(tags=["meeting", "idea"], sources=["voice"]),
    ]
    assert analyze_patterns(captures) == {
        "common_tags": [
            {"tag": "meeting", "count": 3},
            {"tag": "idea", "count": 1},
            {"tag": "work", "count": 1},
        ],
        "common_sources": [
            {"source": "email", "count": 2},
            {"source": "voice", "count": 1},
        ],
    }


def test_analyze_patterns_empty_input() -> None:
    assert analyze_patterns([]) == {"common_tags": [], "common_sources": []}


def test_suggest_new_folders_threshold_and_confidence() -> None:
    captures = [make_record(tags=["impro", "misc"]) for _ in range(3)]
    captures.append(make_record(tags=["other"]))
    proposals = suggest_new_folders(captures)
    assert proposals == [
        {
            "name": "impro",
            "type": "projects",
            "reason": "Tag 'impro' appears in 3 captures",
            "confidence": 0.75,
        },
        {
            "name": "misc",
            "type": "projects",
            "reason": "Tag 'misc' appears in 3 captures",
            "confidence": 0.75,
        },
    ]


def test_suggest_new_folders_below_threshold_is_empty() -> None:
    captures = [make_record(tags=["impro"]) for _ in range(2)]
    assert suggest_new_folders(captures) == []


def test_suggest_new_folders_no_captures_is_empty() -> None:
    assert suggest_new_folders([]) == []


# --- persistence (spec 04 §3) ---------------------------------------------


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "state" / "learning.json"
    data = LearningData()
    record_move(data, make_record(tags=["meeting"], sources=["email"]), DEST, now=NOW)

    save_learning(path, data)

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1
    assert on_disk["statistics"]["total_moves"] == 1
    assert load_learning(path) == data


def test_save_learning_writes_atomically_and_leaves_no_temp_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "learning.json"
    save_learning(path, LearningData())
    save_learning(path, LearningData())
    assert [p.name for p in tmp_path.iterdir()] == ["learning.json"]


def test_load_learning_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_learning(tmp_path / "nope.json") == LearningData()


def test_load_learning_malformed_json_degrades_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "learning.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert load_learning(path) == LearningData()


def test_load_learning_non_object_degrades_to_empty(tmp_path: Path) -> None:
    path = tmp_path / "learning.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_learning(path) == LearningData()


def test_load_learning_legacy_unversioned_file_degrades_to_empty(tmp_path: Path) -> None:
    """The live file is virgin (total_moves: 0) and carries no
    schema_version — 04 §3 says legacy input degrades, never crashes."""
    path = tmp_path / "learning.json"
    path.write_text(
        json.dumps(
            {
                "associations": {},
                "patterns": {},
                "statistics": {"total_moves": 0, "destinations": {}, "last_updated": 0},
            }
        ),
        encoding="utf-8",
    )
    assert load_learning(path) == LearningData()


def test_load_learning_skips_malformed_entries_but_keeps_the_rest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "learning.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "associations": {
                    "tags:good": {
                        "created_at": 1,
                        "last_used": 2,
                        "destinations": {DEST: {"count": 4}},
                    },
                    "tags:bad": "not a dict",
                },
                "patterns": {"tag:x->dest:/d": {"count": 2}, "broken": 7},
                "statistics": {"total_moves": 9, "destinations": {DEST: 4}},
            }
        ),
        encoding="utf-8",
    )
    data = load_learning(path)
    assert list(data.associations) == ["tags:good"]
    assert data.associations["tags:good"].destinations[DEST].count == 4
    assert data.associations["tags:good"].destinations[DEST].success_rate == 1.0
    assert list(data.patterns) == ["tag:x->dest:/d"]
    assert data.statistics.total_moves == 9


def test_load_learning_handles_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "learning.json"
    path.write_bytes(b'{"schema_version": 1, "associations": {"\xff\xfe": {}}}')
    # errors="replace" keeps this readable; the result is still valid data.
    assert load_learning(path).statistics.total_moves == 0


def test_scoring_survives_a_corrupt_learning_file(tmp_path: Path) -> None:
    """04 §3 ⚠ 'must degrade to empty data, never crash scoring'."""
    path = tmp_path / "learning.json"
    path.write_text("garbage", encoding="utf-8")
    data = load_learning(path)
    assert (
        get_association_score(data, make_record(tags=["x"]), DEST, LearningConfig(), now=NOW)
        == 0.0
    )


# ===========================================================================
# Phase-1 fix pass
# ===========================================================================


@pytest.mark.parametrize(
    "field_path",
    [
        ("associations", "count"),
        ("patterns", "count"),
        ("statistics", "total_moves"),
        ("statistics", "destination_count"),
    ],
)
@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "1e400"])
def test_a_non_finite_integer_degrades_instead_of_crashing(
    tmp_path: Path, field_path: tuple[str, str], literal: str
) -> None:
    """Spec 04 §3 ⚠: "loading malformed/legacy JSON must degrade to empty
    data, never crash scoring".

    `json.loads` produces `float('inf')` for a literal `Infinity` or an
    out-of-range `1e400`, and `int(inf)` raises `OverflowError` — which
    `_as_int` did not catch, so `load_learning` blew up with a raw traceback
    that did not even go through the OrganizeError path. `organize suggest`
    exited 1 with a stack trace.
    """
    section, leaf = field_path
    payload: dict[str, Any] = {
        "schema_version": 1,
        "associations": {},
        "patterns": {},
        "statistics": {"total_moves": 3, "destinations": {}, "last_updated": 0},
    }
    if section == "associations":
        payload["associations"] = {
            "tags:meeting": {
                "created_at": 0,
                "last_used": 0,
                "destinations": {DEST: {"count": literal, "last_used": 0}},
            }
        }
    elif section == "patterns":
        payload["patterns"] = {tag_pattern_key("meeting", DEST): {"count": literal, "last_seen": 0}}
    elif leaf == "total_moves":
        payload["statistics"]["total_moves"] = literal
    else:
        payload["statistics"]["destinations"] = {DEST: literal}

    text = json.dumps(payload).replace(f'"{literal}"', literal)
    path = tmp_path / "learning.json"
    path.write_text(text, encoding="utf-8")

    data = load_learning(path)  # must not raise
    assert isinstance(data, LearningData)
    # And scoring off it stays finite.
    score = get_association_score(
        data,
        NoteRecord(
            path="/v/c.md",
            filename="c.md",
            title="c",
            para_type="capture",
            folder="raw_capture",
            tags=["meeting"],
        ),
        DEST,
        LearningConfig(),
        now=NOW,
    )
    assert math.isfinite(score)


def test_import_data_also_survives_a_non_finite_integer() -> None:
    """`import_data` goes through the same `_data_from_raw` and used to raise
    OverflowError instead of returning its documented failure value."""
    raw = {
        "schema_version": 1,
        "associations": {
            "tags:meeting": {
                "created_at": 0,
                "last_used": 0,
                "destinations": {DEST: {"count": float("inf"), "last_used": 0}},
            }
        },
        "statistics": {"total_moves": float("inf")},
    }
    data = import_data(raw)
    assert data is not None
    assert data.statistics.total_moves == 0


def test_clear_returns_empty_learning_state() -> None:
    """Spec 04 §6 lists `clear()` in the public API; it was not implemented."""
    assert learn_mod.clear() == LearningData()


# --- 08 §A23 / 04 §3 step 6: decay is actually SCHEDULED -------------------


def test_maybe_apply_decay_runs_once_a_day_not_once_a_move() -> None:
    """04 §3 step 6: "at most once per session or per day". `apply_decay` was
    pure, correct, unit-tested — and called by NOTHING in production, so the
    90-day eviction and the max_history cap never ran at all."""
    config = LearningConfig(eviction_days=90, max_history=1000)
    data = LearningData()
    data.associations["stale"] = Association(last_used=NOW - 200 * SECONDS_PER_DAY)
    data.associations["fresh"] = Association(last_used=NOW)
    data.statistics = Statistics(total_moves=1, last_updated=NOW - 2 * SECONDS_PER_DAY)

    learn_mod.maybe_apply_decay(data, config, now=NOW)
    assert "stale" not in data.associations
    assert "fresh" in data.associations


def test_maybe_apply_decay_is_a_no_op_inside_the_day_window() -> None:
    config = LearningConfig(eviction_days=90, max_history=1000)
    data = LearningData()
    data.associations["stale"] = Association(last_used=NOW - 200 * SECONDS_PER_DAY)
    data.statistics = Statistics(total_moves=1, last_updated=NOW - 60.0)

    learn_mod.maybe_apply_decay(data, config, now=NOW)
    assert "stale" in data.associations, "decay must not run per move (04 §3 step 6)"


def test_a_virgin_learning_file_decays_on_its_first_move() -> None:
    """`last_updated == 0` opens the gate, so the very first accepted move on
    an imported/stale file cleans it up."""
    config = LearningConfig(eviction_days=90, max_history=1000)
    data = LearningData()
    data.associations["stale"] = Association(last_used=NOW - 200 * SECONDS_PER_DAY)
    learn_mod.maybe_apply_decay(data, config, now=NOW)
    assert data.associations == {}


# --- 12 §2 "Uses" #2: learning is derived FROM the ActionRecord ------------


def _action_record(tags: list[str], destination_file: str, operation: str = "move") -> Any:
    from organize_core.actions import ActionRecord, CaptureState, TargetState

    return ActionRecord(
        id="act_test",
        ts="2026-08-06T07:06:40Z",
        actor="matt",
        operation=operation,  # type: ignore[arg-type]
        capture=CaptureState(
            path="/v/capture/raw_capture/c.md",
            content_hash="h",
            frontmatter_before={"tags": tags, "sources": ["me"]},
            body_before="body",
        ),
        targets=(
            TargetState(
                path=destination_file,
                role="destination" if operation == "move" else "merge_target",
                before_hash=None,
                after_hash="a",
                diff="",
            ),
        ),
    )


def test_record_action_keys_on_the_destination_folder() -> None:
    """Spec 03 §6: "record_move fires with the target's FOLDER" — `suggest`
    scores folder candidates, so a file-keyed association never reads back."""
    data = learn_mod.record_action(
        LearningData(), _action_record(["meeting"], f"{DEST}/note.md"), now=NOW
    )
    assert data is not None
    assert list(data.statistics.destinations) == [DEST]


def test_record_action_derives_the_same_key_as_record_move() -> None:
    """The corpus-derived path and the NoteRecord path must agree, or
    learning.json stops being rebuildable from the action log."""
    capture = NoteRecord(
        path="/v/capture/raw_capture/c.md",
        filename="c.md",
        title="c",
        para_type="capture",
        folder="raw_capture",
        tags=["meeting", "impro"],
        sources=["me"],
    )
    from_record = record_move(LearningData(), capture, DEST, now=NOW)
    from_action = learn_mod.record_action(
        LearningData(), _action_record(["meeting", "impro"], f"{DEST}/note.md"), now=NOW
    )
    assert from_action is not None
    assert list(from_action.associations) == list(from_record.associations)
    assert list(from_action.patterns) == list(from_record.patterns)


def test_record_action_coerces_a_scalar_tags_field_like_the_index_does() -> None:
    """The fixture vault has a note with `tags: daily_notes` (a scalar).
    `NoteRecord.tags` coerces it to a one-element list, so the corpus-derived
    view must too or the two paths produce different association keys."""
    record = _action_record([], f"{DEST}/note.md")
    record.capture.frontmatter_before["tags"] = "daily_notes"
    view = learn_mod.capture_from_action(record)
    assert view.tags == ("daily_notes",)


def test_record_action_ignores_a_dry_run() -> None:
    """A rehearsal is not a precedent — the same rule every corpus reader
    applies (ActionContext.dry_run)."""
    from organize_core.actions import ActionContext

    record = _action_record(["meeting"], f"{DEST}/note.md")
    record = replace_context(record, ActionContext(dry_run=True))
    assert learn_mod.record_action(LearningData(), record, now=NOW) is None


def test_record_action_ignores_a_partially_applied_operation() -> None:
    """Spec 04 §33 learns on every SUCCESSFUL accept/move/merge. A move that
    copied the note but never archived the original did not put the note
    where the record says — and on real data three such failures taught an
    association with ``count: 3, success_rate: 1.0``, steering later captures
    at a destination that had just failed."""
    from organize_core.actions import ActionContext

    record = _action_record(["meeting"], f"{DEST}/note.md")
    incomplete = replace_context(
        record, ActionContext(partial_failure="archiving the original failed: EACCES")
    )
    assert learn_mod.record_action(LearningData(), incomplete, now=NOW) is None
    # the identical record without the marker still teaches — the guard is
    # the marker, not the shape of the record
    assert learn_mod.record_action(LearningData(), record, now=NOW) is not None


def test_record_action_ignores_the_legacy_filters_spelling_of_the_marker() -> None:
    """Records written before ``partial_failure`` was promoted are already on
    disk in Matt's corpus; they must keep being recognized as non-precedents
    when they are read back."""
    from organize_core.actions import ActionRecord

    record = _action_record(["meeting"], f"{DEST}/note.md")
    payload = record.to_json()
    payload["context"]["partial_failure"] = None
    payload["context"]["filters"] = {"partial_failure": "archiving the original failed"}
    reread = ActionRecord.from_json(payload)
    assert reread.context.partial_failure == "archiving the original failed"
    assert learn_mod.record_action(LearningData(), reread, now=NOW) is None


def replace_context(record: Any, context: Any) -> Any:
    import dataclasses

    return dataclasses.replace(record, context=context)


@pytest.mark.parametrize("operation", ["archive", "create_folder", "meta_edit", "skip"])
def test_record_action_ignores_operations_that_file_nothing(operation: str) -> None:
    record = _action_record(["meeting"], f"{DEST}/note.md", operation=operation)
    assert learn_mod.record_action(LearningData(), record, now=NOW) is None


def test_record_action_learns_from_a_merge() -> None:
    """Spec 03 §6's outcome table and 04 §3 both fire on merge."""
    data = learn_mod.record_action(
        LearningData(), _action_record(["meeting"], f"{DEST}/ideas.md", operation="merge"), now=NOW
    )
    assert data is not None
    assert data.statistics.total_moves == 1
    assert list(data.statistics.destinations) == [DEST]
