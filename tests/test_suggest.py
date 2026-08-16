"""Numeric goldens for the 7-signal scorer (spec 04 §1-2, §7).

Expected values are hand-derived from the weight table in
``spec/04-suggestions-and-learning.md`` §2 and the learned-score formula in
§4 — not from running ``suggest.py``.  Each signal is asserted in isolation
at its exact documented weight, and the end-to-end ranking is asserted as
exact floats.

Doc-08 regression obligations covered here:

* §A7 ``string_similarity`` returned raw Levenshtein DISTANCE, not
  similarity — inverted (identical strings scored 0) and unbounded (a
  10-character mismatch contributed 10, dwarfing every other signal).
* §A22 a NaN learned score must not poison ranking.

``suggest.py`` normalizes through ``frontmatter.normalize_tag`` — THE shared
normalizer (ARCHITECTURE "one shared normalizer", spec 04 §1), owned by
another seat.  ``test_shared_normalizer_contract`` pins the behaviour this
module depends on; ``_reference_normalize_tag`` states expected values in the
tests themselves so a regression there fails loudly here.
"""

from __future__ import annotations

import math

import pytest

from organize_core import frontmatter
from organize_core.config import (
    LearningConfig,
    SuggestionsConfig,
    SuggestionWeights,
    TypeBonus,
)
from organize_core.index import NoteRecord
from organize_core.learn import (
    Association,
    DestinationStat,
    LearningData,
    PatternStat,
    Statistics,
    tag_pattern_key,
)
from organize_core.suggest import (
    ARCHIVE_SUGGESTION_NAME,
    Candidate,
    CaptureFeaturesView,
    Suggestion,
    calculate_score,
    generate_candidates,
    string_similarity,
    suggest,
)

NOW = 1_760_000_000.0
SECONDS_PER_DAY = 24 * 60 * 60


def _reference_normalize_tag(value: str, extra_map: dict[str, str] | None = None) -> str:
    """Spec 04 §1: lowercase, trim, spaces/underscores → hyphens, then the
    configured ``tag_normalization`` map."""
    normalized = str(value).strip().lower().replace(" ", "-").replace("_", "-")
    if extra_map:
        normalized = extra_map.get(normalized, normalized)
    return normalized


def test_shared_normalizer_contract() -> None:
    """Cross-seat contract: signals 2 and 4 and every ``normalized_name``
    depend on this exact behaviour (spec 04 §1)."""
    for raw in ("Deep_Work Habits", "  Impro  ", "email", "Project X"):
        assert frontmatter.normalize_tag(raw) == _reference_normalize_tag(raw)
    assert frontmatter.normalize_tag("project", {"project": "projects"}) == "projects"


# --- fixtures --------------------------------------------------------------


def cand(name: str, para_type: str, *, normalized: str | None = None) -> Candidate:
    return Candidate(
        path=f"/vault/{para_type}/{normalized or name.lower()}",
        name=name,
        normalized_name=normalized or _reference_normalize_tag(name),
        type=para_type,
    )


def view(**kwargs: object) -> CaptureFeaturesView:
    return CaptureFeaturesView(**kwargs)  # type: ignore[arg-type]


DEFAULTS = SuggestionsConfig()
EMPTY_LEARNING = LearningData()

# Weight constants, restated from spec 04 §2's table so a silent config edit
# is caught here rather than only in test_config.
W_EXACT = 2.0
W_NORMALIZED = 1.5
W_LEARNED = 1.8
W_SOURCE = 1.3
W_ALIAS = 1.1
W_CONTEXT = 1.0
B_PROJECTS = 0.3
B_AREAS = 0.2
B_RESOURCES = 0.1


def test_default_weights_match_the_spec_table_exactly() -> None:
    weights = SuggestionWeights()
    assert weights.exact_tag_match == W_EXACT
    assert weights.normalized_tag_match == W_NORMALIZED
    assert weights.learned_association == W_LEARNED
    assert weights.source_match == W_SOURCE
    assert weights.alias_similarity == W_ALIAS
    assert weights.context_match == W_CONTEXT
    assert weights.type_bonus == TypeBonus(projects=0.3, areas=0.2, resources=0.1)


# --- 08 §A7: string_similarity direction and bounds -----------------------


def test_similarity_identical_strings_is_one() -> None:
    """08 §A7 ✔exec: HEAD returned 0 here (distance, not similarity)."""
    assert string_similarity("health", "health") == 1.0


def test_similarity_totally_dissimilar_is_zero() -> None:
    """08 §A7 ✔exec: HEAD returned 10 here — unbounded, and larger than
    every real signal put together."""
    assert string_similarity("health", "zzzzzzzzzz") == 0.0


def test_similarity_is_a_normalized_distance() -> None:
    # 'helth' → 'health' is one insertion; max_len 6 ⇒ 1 - 1/6.
    assert string_similarity("helth", "health") == pytest.approx(5 / 6, rel=1e-12)
    # 'kitten' → 'sitting' is the classic distance 3; max_len 7 ⇒ 1 - 3/7.
    assert string_similarity("kitten", "sitting") == pytest.approx(4 / 7, rel=1e-12)


def test_similarity_is_symmetric_and_bounded() -> None:
    pairs = [("", ""), ("a", ""), ("abc", "abd"), ("impro", "improvisation")]
    for a, b in pairs:
        assert string_similarity(a, b) == string_similarity(b, a)
        assert 0.0 <= string_similarity(a, b) <= 1.0


def test_similarity_empty_vs_empty_is_one() -> None:
    assert string_similarity("", "") == 1.0


def test_similarity_empty_vs_nonempty_is_zero() -> None:
    assert string_similarity("", "health") == 0.0


def test_more_similar_pairs_score_higher_than_less_similar_ones() -> None:
    """The direction regression in one assertion: HEAD had this backwards."""
    assert string_similarity("health", "healthy") > string_similarity("health", "wealthy")
    assert string_similarity("health", "wealthy") > string_similarity("health", "zzzzzzz")


def test_alias_signal_can_never_exceed_its_weight() -> None:
    """Spec 04 §7: 'alias signal never exceeds alias_similarity weight'."""
    candidate = cand("health", "resources")
    for alias in ("health", "healt", "healthy", "heath", "zzzzzzzzzz"):
        score, _ = calculate_score(
            view(aliases=(alias,)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
        )
        assert score - B_RESOURCES <= W_ALIAS + 1e-12


# --- each signal in isolation, at its exact weight (spec 04 §7 test 1) ----
#
# Isolation uses a "resources" candidate so the always-on type bonus is a
# known +0.1 that is subtracted explicitly in each assertion.


def test_signal_1_exact_tag_match() -> None:
    candidate = cand("impro", "resources")
    score, reasons = calculate_score(
        view(tags=("impro",)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(W_EXACT + B_RESOURCES, rel=1e-12)  # 2.1
    assert reasons == ["Tag 'impro' matches folder"]


def test_signal_1_fires_per_matching_tag() -> None:
    """'2.0 PER matching tag' — two tags, one hitting `name`, one hitting
    `normalized_name`."""
    candidate = Candidate(
        path="/vault/resources/impro", name="Impro", normalized_name="impro",
        type="resources",
    )
    score, reasons = calculate_score(
        view(tags=("Impro", "impro")), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(2 * W_EXACT + B_RESOURCES, rel=1e-12)  # 4.1
    assert reasons == ["Tag 'Impro' matches folder", "Tag 'impro' matches folder"]


def test_signal_1_does_not_fire_on_a_miss() -> None:
    score, reasons = calculate_score(
        view(tags=("cooking",)), cand("impro", "resources"), DEFAULTS, EMPTY_LEARNING,
        now=NOW,
    )
    assert score == pytest.approx(B_RESOURCES, rel=1e-12)
    assert reasons == []


def test_signal_2_normalized_tag_match() -> None:
    candidate = cand("impro", "resources")
    score, reasons = calculate_score(
        view(normalized_tags=("impro",)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(W_NORMALIZED + B_RESOURCES, rel=1e-12)  # 1.6
    assert reasons == ["Tag 'impro' (normalized) matches"]


def test_signals_1_and_2_stack() -> None:
    candidate = cand("impro", "resources")
    score, reasons = calculate_score(
        view(tags=("impro",), normalized_tags=("impro",)),
        candidate, DEFAULTS, EMPTY_LEARNING, now=NOW,
    )
    assert score == pytest.approx(W_EXACT + W_NORMALIZED + B_RESOURCES, rel=1e-12)  # 3.6
    assert len(reasons) == 2


def test_signal_3_learned_association() -> None:
    """learned score (doc 04 §4, count=3 total=10 fresh) = log(4)/log(11)
    = 0.5781296526357756; contribution = × 1.8 = 1.040633374744396."""
    candidate = cand("project-x", "resources")
    learning = LearningData()
    learning.associations["tags:meeting"] = Association(
        created_at=NOW,
        last_used=NOW,
        destinations={
            candidate.path: DestinationStat(
                count=3, first_used=NOW, last_used=NOW, success_rate=1.0
            )
        },
    )
    learning.statistics = Statistics(total_moves=10, destinations={candidate.path: 3})

    score, reasons = calculate_score(
        view(tags=("meeting",)), candidate, DEFAULTS, learning, now=NOW
    )
    learned = math.log(4) / math.log(11)
    assert learned == pytest.approx(0.5781296526357756, rel=1e-12)
    assert score == pytest.approx(learned * W_LEARNED + B_RESOURCES, rel=1e-12)
    assert score == pytest.approx(1.1406333747443962, rel=1e-12)
    assert reasons == ["Previously used destination"]


def test_signal_3_absent_when_nothing_learned() -> None:
    score, reasons = calculate_score(
        view(tags=("meeting",)), cand("project-x", "resources"), DEFAULTS,
        EMPTY_LEARNING, now=NOW,
    )
    assert score == pytest.approx(B_RESOURCES, rel=1e-12)
    assert reasons == []


def test_signal_4_source_match() -> None:
    candidate = cand("email", "resources")
    score, reasons = calculate_score(
        view(sources=("Email",)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(W_SOURCE + B_RESOURCES, rel=1e-12)  # 1.4
    assert reasons == ["Source 'Email' matches"]


def test_signal_4_fires_per_matching_source() -> None:
    candidate = cand("email", "resources")
    # Both normalize to "email" — the signal is 1.3 PER matching source.
    score, reasons = calculate_score(
        view(sources=("Email", "email")),
        candidate, DEFAULTS, EMPTY_LEARNING, now=NOW,
    )
    assert score == pytest.approx(2 * W_SOURCE + B_RESOURCES, rel=1e-12)  # 2.7
    assert reasons == ["Source 'Email' matches", "Source 'email' matches"]


def test_signal_5_alias_similarity_golden() -> None:
    """similarity('helth', 'health') = 1 - 1/6 = 0.8333333333333334;
    contribution = × 1.1 = 0.9166666666666667."""
    candidate = cand("health", "resources")
    score, reasons = calculate_score(
        view(aliases=("helth",)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(5 / 6 * W_ALIAS + B_RESOURCES, rel=1e-12)
    assert score == pytest.approx(1.0166666666666668, rel=1e-12)
    assert reasons == ["Alias 'helth' similar"]


def test_signal_5_gate_is_strictly_above_point_six() -> None:
    """'health' vs 'zealot': distance 4, max_len 6 ⇒ similarity 1/3 < 0.6."""
    candidate = cand("health", "resources")
    score, reasons = calculate_score(
        view(aliases=("zealot",)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(B_RESOURCES, rel=1e-12)
    assert reasons == []


def test_signal_5_skips_capture_prefixed_aliases() -> None:
    """Spec 04 §2 #5: skip aliases matching ^capture_ ..."""
    candidate = cand("capture-20260815", "resources")
    score, reasons = calculate_score(
        view(aliases=("capture_20260815",)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(B_RESOURCES, rel=1e-12)
    assert reasons == []


def test_signal_5_skips_the_capture_id_alias() -> None:
    """... and the capture_id alias, whatever it is named."""
    candidate = cand("abc123", "resources")
    score, reasons = calculate_score(
        view(aliases=("abc123",), capture_id="abc123"),
        candidate, DEFAULTS, EMPTY_LEARNING, now=NOW,
    )
    assert score == pytest.approx(B_RESOURCES, rel=1e-12)
    assert reasons == []


def test_signal_6_context_match_folder_inside_context() -> None:
    candidate = cand("health", "resources")
    score, reasons = calculate_score(
        view(context=("Health tracking",)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(W_CONTEXT + B_RESOURCES, rel=1e-12)  # 1.1
    assert reasons == ["Context matches folder"]


def test_signal_6_context_match_context_inside_folder() -> None:
    """Substring EITHER direction (spec 04 §2 #6)."""
    candidate = cand("health-tracking", "resources", normalized="health-tracking")
    score, _ = calculate_score(
        view(context=("health",)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(W_CONTEXT + B_RESOURCES, rel=1e-12)


def test_signal_6_context_list_is_joined_and_case_folded() -> None:
    candidate = cand("standup", "resources")
    score, _ = calculate_score(
        view(context=("Morning", "STANDUP")), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(W_CONTEXT + B_RESOURCES, rel=1e-12)


def test_signal_6_fires_once_not_per_context_entry() -> None:
    candidate = cand("health", "resources")
    score, reasons = calculate_score(
        view(context=("health", "health", "health")),
        candidate, DEFAULTS, EMPTY_LEARNING, now=NOW,
    )
    assert score == pytest.approx(W_CONTEXT + B_RESOURCES, rel=1e-12)
    assert reasons == ["Context matches folder"]


def test_signal_6_empty_context_never_matches() -> None:
    score, reasons = calculate_score(
        view(context=("", "  ")), cand("health", "resources"), DEFAULTS,
        EMPTY_LEARNING, now=NOW,
    )
    assert score == pytest.approx(B_RESOURCES, rel=1e-12)
    assert reasons == []


def test_signal_7_type_bonus_exact_values() -> None:
    """The bonus always fires and carries no reason string (parity)."""
    for para_type, expected in (
        ("projects", B_PROJECTS),
        ("areas", B_AREAS),
        ("resources", B_RESOURCES),
    ):
        score, reasons = calculate_score(
            view(), cand("anything", para_type), DEFAULTS, EMPTY_LEARNING, now=NOW
        )
        assert score == pytest.approx(expected, rel=1e-12)
        assert reasons == []


def test_signal_7_is_config_driven_not_hardcoded() -> None:
    """08 §A-MODERATE / 04 §2 #7 ⚠: the bonus was hardcoded in the original."""
    config = SuggestionsConfig(
        weights=SuggestionWeights(type_bonus=TypeBonus(projects=9.0, areas=8.0, resources=7.0))
    )
    score, _ = calculate_score(
        view(), cand("anything", "projects"), config, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(9.0, rel=1e-12)


def test_unknown_candidate_type_gets_no_bonus() -> None:
    score, _ = calculate_score(
        view(), cand("anything", "other"), DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == 0.0


def test_body_text_and_modalities_are_not_signals() -> None:
    """Spec 04 §2: deliberately excluded — modalities exist on the view only
    so association KEYS match what record_move stored."""
    candidate = cand("voice", "resources")
    score, reasons = calculate_score(
        view(modalities=("voice",)), candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert score == pytest.approx(B_RESOURCES, rel=1e-12)
    assert reasons == []


def test_all_seven_signals_are_additive() -> None:
    """2.0 + 1.5 + 1.8×learned + 1.3 + 1.1×(5/6) + 1.0 + 0.3."""
    candidate = Candidate(
        path="/vault/projects/health", name="health", normalized_name="health",
        type="projects",
    )
    learning = LearningData()
    key = "tags:health|sources:health|modalities:text"
    learning.associations[key] = Association(
        created_at=NOW,
        last_used=NOW,
        destinations={
            candidate.path: DestinationStat(count=3, first_used=NOW, last_used=NOW)
        },
    )
    learning.patterns[tag_pattern_key("health", candidate.path)] = PatternStat(
        count=2, created_at=NOW, last_seen=NOW
    )
    learning.statistics = Statistics(total_moves=10, destinations={candidate.path: 3})

    capture = view(
        tags=("health",),
        normalized_tags=("health",),
        sources=("health",),
        modalities=("text",),
        aliases=("helth",),
        context=("health tracking",),
    )
    score, reasons = calculate_score(capture, candidate, DEFAULTS, learning, now=NOW)

    # doc 04 §4: log(4)/log(11) * 1 * 1 + 0.5 * (2/100 + 0/100)
    learned = math.log(4) / math.log(11) + 0.5 * 0.02
    expected = (
        W_EXACT
        + W_NORMALIZED
        + learned * W_LEARNED
        + W_SOURCE
        + (5 / 6) * W_ALIAS
        + W_CONTEXT
        + B_PROJECTS
    )
    assert score == pytest.approx(expected, rel=1e-12)
    # 3.5 + 1.8×0.5881296526357756 (=1.058633374744396) + 1.3 + 0.9166666666666667
    # + 1.0 + 0.3
    assert score == pytest.approx(8.075300041411063, rel=1e-12)
    assert reasons == [
        "Tag 'health' matches folder",
        "Tag 'health' (normalized) matches",
        "Previously used destination",
        "Source 'health' matches",
        "Alias 'helth' similar",
        "Context matches folder",
    ]


def test_a_nan_learned_score_cannot_poison_a_candidate() -> None:
    """08 §A22 seen from the scoring side: total_moves==0 with a recorded
    association used to make the whole candidate NaN."""
    candidate = cand("project-x", "resources")
    learning = LearningData()
    learning.associations["tags:meeting"] = Association(
        created_at=NOW,
        last_used=NOW,
        destinations={candidate.path: DestinationStat(count=3, last_used=NOW)},
    )
    learning.statistics = Statistics(total_moves=0)

    score, reasons = calculate_score(
        view(tags=("meeting",)), candidate, DEFAULTS, learning, now=NOW
    )
    assert not math.isnan(score)
    assert score == pytest.approx(B_RESOURCES, rel=1e-12)
    assert reasons == []


# --- ranking end-to-end (spec 04 §1, §7 tests 2 and 8) --------------------

IMPRO_CAPTURE = CaptureFeaturesView(tags=("impro",), normalized_tags=("impro",))
ARCHIVE_PATH = "/vault/archive/capture/raw_capture"


def test_ranking_projects_beats_areas_beats_resources_exact_scores() -> None:
    """Spec 04 §7 test 2, with exact floats: 2.0 + 1.5 + type bonus."""
    candidates = [
        cand("impro", "resources"),
        cand("impro", "areas"),
        cand("impro", "projects"),
    ]
    result = suggest(IMPRO_CAPTURE, candidates, DEFAULTS, EMPTY_LEARNING, now=NOW)

    assert [s.type for s in result] == ["projects", "areas", "resources"]
    assert result[0].score == pytest.approx(3.8, rel=1e-12)
    assert result[1].score == pytest.approx(3.7, rel=1e-12)
    assert result[2].score == pytest.approx(3.6, rel=1e-12)
    assert result[0].reasons == (
        "Tag 'impro' matches folder",
        "Tag 'impro' (normalized) matches",
    )


def test_min_confidence_drops_bare_areas_and_resources_but_keeps_projects() -> None:
    """04 §2 ⚠: min_confidence 0.3 is the documented floor. The type bonus
    alone puts projects exactly AT the floor (kept) and areas/resources
    below it (dropped)."""
    candidates = [
        cand("alpha", "projects"),
        cand("beta", "areas"),
        cand("gamma", "resources"),
    ]
    result = suggest(view(), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW)
    assert [s.name for s in result] == ["alpha"]
    assert result[0].score == pytest.approx(B_PROJECTS, rel=1e-12)


def test_min_confidence_is_configurable_and_honored() -> None:
    candidates = [cand("beta", "areas")]
    lenient = SuggestionsConfig(learning=LearningConfig(min_confidence=0.15))
    assert [s.name for s in suggest(view(), candidates, lenient, EMPTY_LEARNING, now=NOW)] == [
        "beta"
    ]
    strict = SuggestionsConfig(learning=LearningConfig(min_confidence=0.25))
    assert suggest(view(), candidates, strict, EMPTY_LEARNING, now=NOW) == []


def test_ties_break_on_name_then_path_deterministically() -> None:
    candidates = [
        Candidate(path="/vault/areas/zeta", name="zeta", normalized_name="zeta", type="areas"),
        Candidate(path="/vault/areas/alpha", name="alpha", normalized_name="alpha", type="areas"),
        Candidate(path="/vault/areas/mid", name="mid", normalized_name="mid", type="areas"),
    ]
    capture = view(context=("shared context",))
    forward = suggest(capture, candidates, DEFAULTS, EMPTY_LEARNING, now=NOW)
    backward = suggest(capture, list(reversed(candidates)), DEFAULTS, EMPTY_LEARNING, now=NOW)
    assert [s.name for s in forward] == [s.name for s in backward]


def test_archive_entry_is_appended_with_the_spec_shape() -> None:
    result = suggest(
        IMPRO_CAPTURE,
        [cand("impro", "projects")],
        DEFAULTS,
        EMPTY_LEARNING,
        now=NOW,
        archive_path=ARCHIVE_PATH,
    )
    assert result[-1] == Suggestion(
        path=ARCHIVE_PATH,
        name=ARCHIVE_SUGGESTION_NAME,
        type="archives",
        score=0.1,
        reasons=("Safe default option",),
    )
    assert ARCHIVE_SUGGESTION_NAME == "Archive Now"


def test_archive_entry_survives_even_when_nothing_scores() -> None:
    """Spec 04 §7: 'archive entry ... always present when configured'."""
    result = suggest(
        view(), [cand("beta", "areas")], DEFAULTS, EMPTY_LEARNING, now=NOW,
        archive_path=ARCHIVE_PATH,
    )
    assert [s.name for s in result] == [ARCHIVE_SUGGESTION_NAME]


def test_archive_entry_omitted_when_disabled() -> None:
    config = SuggestionsConfig(always_show_archive=False)
    result = suggest(
        IMPRO_CAPTURE, [cand("impro", "projects")], config, EMPTY_LEARNING, now=NOW,
        archive_path=ARCHIVE_PATH,
    )
    assert [s.name for s in result] == ["impro"]


def test_archive_entry_omitted_when_no_archive_path_supplied() -> None:
    result = suggest(IMPRO_CAPTURE, [cand("impro", "projects")], DEFAULTS,
                     EMPTY_LEARNING, now=NOW)
    assert [s.name for s in result] == ["impro"]


def test_list_length_is_exactly_max_suggestions_including_archive() -> None:
    """Spec 04 §1 ⚠ off-by-one: the original returned max_suggestions - 1
    scored entries PLUS the archive entry, i.e. 11 lines for max=10."""
    candidates = [cand(f"impro{i:02d}", "projects", normalized="impro") for i in range(25)]
    result = suggest(
        IMPRO_CAPTURE, candidates, DEFAULTS, EMPTY_LEARNING, now=NOW,
        archive_path=ARCHIVE_PATH,
    )
    assert len(result) == DEFAULTS.max_suggestions == 10
    assert len([s for s in result if s.type != "archives"]) == 9
    assert result[-1].name == ARCHIVE_SUGGESTION_NAME


def test_list_length_is_exactly_max_suggestions_without_archive() -> None:
    candidates = [cand(f"impro{i:02d}", "projects", normalized="impro") for i in range(25)]
    config = SuggestionsConfig(always_show_archive=False)
    result = suggest(IMPRO_CAPTURE, candidates, config, EMPTY_LEARNING, now=NOW,
                     archive_path=ARCHIVE_PATH)
    assert len(result) == 10
    assert all(s.type != "archives" for s in result)


@pytest.mark.parametrize("max_suggestions", [1, 2, 3, 5, 10, 11])
def test_list_never_exceeds_max_suggestions(max_suggestions: int) -> None:
    candidates = [cand(f"impro{i:02d}", "projects", normalized="impro") for i in range(30)]
    config = SuggestionsConfig(max_suggestions=max_suggestions)
    result = suggest(
        IMPRO_CAPTURE, candidates, config, EMPTY_LEARNING, now=NOW,
        archive_path=ARCHIVE_PATH,
    )
    assert len(result) == max_suggestions
    assert result[-1].name == ARCHIVE_SUGGESTION_NAME


def test_truncation_keeps_the_highest_scoring_entries() -> None:
    candidates = [cand("impro", "projects"), cand("impro", "areas"), cand("impro", "resources")]
    config = SuggestionsConfig(max_suggestions=3)
    result = suggest(
        IMPRO_CAPTURE, candidates, config, EMPTY_LEARNING, now=NOW,
        archive_path=ARCHIVE_PATH,
    )
    assert [s.type for s in result] == ["projects", "areas", "archives"]


def test_suggest_never_scores_an_archive_candidate() -> None:
    """Spec 04 §1: candidates are PARA subfolders except archives."""
    candidates = [cand("old-stuff", "archives"), cand("impro", "projects")]
    result = suggest(IMPRO_CAPTURE, candidates, DEFAULTS, EMPTY_LEARNING, now=NOW)
    assert [s.name for s in result] == ["impro"]


def test_suggest_is_pure_and_repeatable() -> None:
    candidates = [cand("impro", "projects"), cand("impro", "areas")]
    first = suggest(IMPRO_CAPTURE, candidates, DEFAULTS, EMPTY_LEARNING, now=NOW,
                    archive_path=ARCHIVE_PATH)
    second = suggest(IMPRO_CAPTURE, candidates, DEFAULTS, EMPTY_LEARNING, now=NOW,
                     archive_path=ARCHIVE_PATH)
    assert first == second
    assert EMPTY_LEARNING == LearningData()  # scoring mutates nothing


def test_learning_changes_the_ranking_end_to_end() -> None:
    """A learned destination outranks an equally-tagged rival: doc 04 §4
    fresh score 0.5781296526357756 × 1.8 = 1.040633374744396 on top of the
    2.0 + 1.5 + 0.2 both areas candidates share."""
    learned = cand("impro", "areas", normalized="impro")
    rival = Candidate(
        path="/vault/areas/impro-2", name="impro", normalized_name="impro", type="areas"
    )
    learning = LearningData()
    learning.associations["tags:impro"] = Association(
        created_at=NOW,
        last_used=NOW,
        destinations={learned.path: DestinationStat(count=3, first_used=NOW, last_used=NOW)},
    )
    learning.statistics = Statistics(total_moves=10, destinations={learned.path: 3})

    result = suggest(IMPRO_CAPTURE, [rival, learned], DEFAULTS, learning, now=NOW)
    assert [s.path for s in result] == [learned.path, rival.path]
    assert result[0].score == pytest.approx(
        2.0 + 1.5 + (math.log(4) / math.log(11)) * 1.8 + 0.2, rel=1e-12
    )
    assert result[0].score == pytest.approx(4.740633374744396, rel=1e-12)
    assert result[1].score == pytest.approx(3.7, rel=1e-12)


def test_decayed_learning_ranks_below_fresh_learning() -> None:
    """Both destinations were used 3 times; only recency separates them
    (doc 04 §4: ``0.9 ** days_since``)."""
    fresh = cand("alpha", "projects")
    stale = cand("beta", "projects")
    learning = LearningData()
    learning.associations["generic"] = Association(
        created_at=NOW,
        last_used=NOW,
        destinations={
            fresh.path: DestinationStat(count=3, last_used=NOW),
            stale.path: DestinationStat(count=3, last_used=NOW - 30 * SECONDS_PER_DAY),
        },
    )
    learning.statistics = Statistics(total_moves=10)

    result = suggest(view(), [stale, fresh], DEFAULTS, learning, now=NOW)
    assert [s.name for s in result] == ["alpha", "beta"]
    # fresh: 0.5781296526357756 × 1.8 + 0.3
    assert result[0].score == pytest.approx(1.340633374744396, rel=1e-12)
    # stale: 0.5781296526357756 × 0.9**30 (= 0.02450758560847895) × 1.8 + 0.3
    assert result[1].score == pytest.approx(0.3441136540952621, rel=1e-12)


# --- candidate generation (spec 04 §1) ------------------------------------


def test_generate_candidates_shape_and_archive_exclusion() -> None:
    candidates = generate_candidates(
        {
            "projects": ["/vault/projects/Project X"],
            "areas": ["/vault/areas/health"],
            "resources": [],
            "archives": ["/vault/archive/old"],
        }
    )
    assert [(c.name, c.normalized_name, c.type) for c in candidates] == [
        ("Project X", "project-x", "projects"),
        ("health", "health", "areas"),
    ]
    assert candidates[0].path == "/vault/projects/Project X"


def test_generate_candidates_uses_the_shared_normalizer() -> None:
    candidates = generate_candidates({"areas": ["/vault/areas/Deep_Work Habits"]})
    assert candidates[0].normalized_name == "deep-work-habits"


def test_generate_candidates_is_sorted_and_ignores_trailing_slashes() -> None:
    candidates = generate_candidates({"areas": ["/vault/areas/zeta/", "/vault/areas/alpha"]})
    assert [c.name for c in candidates] == ["alpha", "zeta"]


def test_generate_candidates_empty_input() -> None:
    assert generate_candidates({}) == []


# --- CaptureFeaturesView (spec 13 §3) -------------------------------------


def test_from_record_carries_every_scored_attribute() -> None:
    record = NoteRecord(
        path="/vault/capture/raw_capture/c1.md",
        filename="c1.md",
        title="A capture",
        para_type="capture",
        folder="raw_capture",
        aliases=["Improv Night"],
        capture_id="capture_20260815",
        tags=["impro"],
        sources=["email"],
        modalities=["text"],
        context=["rehearsal"],
        normalized_tags=["impro"],
    )
    v = CaptureFeaturesView.from_record(record)
    assert v.tags == ("impro",)
    assert v.normalized_tags == ("impro",)
    assert v.sources == ("email",)
    assert v.modalities == ("text",)
    assert v.aliases == ("Improv Night",)
    assert v.capture_id == "capture_20260815"
    assert v.context == ("rehearsal",)


def test_from_record_modalities_keep_association_keys_aligned() -> None:
    """Without ``modalities`` on the view, ``create_association_key`` would
    produce a different key at score time than ``record_move`` stored, and
    the learned signal would silently never fire."""
    from organize_core.learn import create_association_key, extract_features

    record = NoteRecord(
        path="/vault/capture/raw_capture/c1.md",
        filename="c1.md",
        title="A capture",
        para_type="capture",
        folder="raw_capture",
        tags=["impro"],
        sources=["email"],
        modalities=["text"],
    )
    v = CaptureFeaturesView.from_record(record)
    assert create_association_key(extract_features(v)) == create_association_key(
        extract_features(record)
    )
    assert create_association_key(extract_features(v)) == (
        "tags:impro|sources:email|modalities:text"
    )


def test_from_text_untagged_scores_context_and_type_bonus_only() -> None:
    """Spec 13 §3 + ARCHITECTURE resolution 14: no invented signals."""
    v = CaptureFeaturesView.from_text("Notes about health tracking")
    assert v.tags == ()
    assert v.normalized_tags == ()
    assert v.sources == ()
    assert v.aliases == ()
    assert v.modalities == ()
    assert v.context == ("Notes about health tracking",)

    hit, hit_reasons = calculate_score(
        v, cand("health", "projects"), DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert hit == pytest.approx(W_CONTEXT + B_PROJECTS, rel=1e-12)  # 1.3
    assert hit_reasons == ["Context matches folder"]

    miss, miss_reasons = calculate_score(
        v, cand("cooking", "projects"), DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert miss == pytest.approx(B_PROJECTS, rel=1e-12)
    assert miss_reasons == []


def test_from_text_with_supplied_tags_scores_the_tag_signals() -> None:
    v = CaptureFeaturesView.from_text("some prose", tags=["Deep Work"])
    assert v.tags == ("Deep Work",)
    assert v.normalized_tags == ("deep-work",)

    candidate = Candidate(
        path="/vault/areas/deep-work", name="Deep Work", normalized_name="deep-work",
        type="areas",
    )
    score, reasons = calculate_score(v, candidate, DEFAULTS, EMPTY_LEARNING, now=NOW)
    assert score == pytest.approx(W_EXACT + W_NORMALIZED + B_AREAS, rel=1e-12)  # 3.7
    assert reasons == [
        "Tag 'Deep Work' matches folder",
        "Tag 'deep-work' (normalized) matches",
    ]


def test_from_text_blank_text_has_no_context() -> None:
    assert CaptureFeaturesView.from_text("   ").context == ()


def test_from_text_end_to_end_ranking() -> None:
    """Spec 13 §3 cross-check: 'suggestion scoring exposed for arbitrary
    text, not only indexed capture files'."""
    v = CaptureFeaturesView.from_text("shoulder mobility for health")
    candidates = [
        cand("health", "areas"),
        cand("cooking", "projects"),
        cand("reading", "resources"),
    ]
    result = suggest(v, candidates, DEFAULTS, EMPTY_LEARNING, now=NOW,
                     archive_path=ARCHIVE_PATH)
    assert [s.name for s in result] == ["health", "cooking", ARCHIVE_SUGGESTION_NAME]
    assert result[0].score == pytest.approx(1.2, rel=1e-12)  # 1.0 + 0.2
    assert result[1].score == pytest.approx(0.3, rel=1e-12)  # bare projects bonus
