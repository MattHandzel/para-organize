"""Spec 21 — nested folder candidates and note-level merge targets.

Every test here FAILS against the pre-21 code (§7.1): before this change no
code path made a note rankable at all, and folder candidates stopped one
level below each PARA root.

The anti-vacuity obligations this file carries (§7, ARCHITECTURE.md):

* the eduardo case is asserted EXACTLY — path, ``destination_kind``,
  ``type``, ``score`` and the ``reasons`` tuple. A membership assertion
  (``"eduardo" in paths``) would also pass for an implementation that
  returned every note in the vault, and is explicitly not acceptable (§7.2);
* the negative twin: a capture that matches nothing still returns the
  archive entry ALONE — SQ-1's guard in unit form (§7.3);
* refusal predicates are asserted at a parameter where the other branch
  fires, each with a firing control;
* the prefilters of §3.5 are pinned as OUTPUT-PRESERVING against a
  brute-force reference over a randomized corpus, not merely exercised.

Weight constants are restated as literals from spec 04 §2's table rather
than imported, so a silent config edit fails here (anti-vacuity standard 2).
"""

from __future__ import annotations

import random

import pytest

from organize_core import frontmatter
from organize_core.config import LearningConfig, SuggestionsConfig
from organize_core.index import NoteRecord
from organize_core.learn import LearningData
from organize_core.suggest import (
    ARCHIVE_SUGGESTION_NAME,
    KIND_FOLDER,
    KIND_NOTE,
    Candidate,
    CandidateSet,
    CaptureFeaturesView,
    build_candidate_set,
    calculate_score,
    generate_candidates,
    generate_note_candidates,
    note_match_keys,
    rank,
    string_similarity,
    suggest,
)
from organize_core.suggest import _gated_similarity as gated_similarity

NOW = 1_760_000_000.0
DEFAULTS = SuggestionsConfig()
EMPTY_LEARNING = LearningData()

# Spec 04 §2's table, restated as literals (never imported from config).
W_EXACT = 2.0
W_NORMALIZED = 1.5
W_SOURCE = 1.3
B_PROJECTS = 0.3
B_AREAS = 0.2
B_RESOURCES = 0.1


def note(
    path: str,
    *,
    para_type: str = "area",
    aliases: list[str] | None = None,
    identifier: str | None = None,
    title: str | None = None,
    capture_id: str | None = None,
    tags: list[str] | None = None,
) -> NoteRecord:
    filename = path.rsplit("/", 1)[-1]
    return NoteRecord(
        path=path,
        filename=filename,
        title=title if title is not None else filename[:-3],
        para_type=para_type,  # type: ignore[arg-type]
        folder=path.rsplit("/", 2)[-2] if "/" in path else "",
        aliases=list(aliases or []),
        id=identifier,
        capture_id=capture_id,
        tags=list(tags or []),
    )


def view(**kwargs: object) -> CaptureFeaturesView:
    return CaptureFeaturesView(**kwargs)  # type: ignore[arg-type]


def folders(**mapping: list[str]) -> dict[str, list[str]]:
    return dict(mapping)


# ---------------------------------------------------------------------------
# §3.1 — which notes are candidates, and on which keys
# ---------------------------------------------------------------------------


def test_a_note_becomes_a_candidate_with_its_stem_as_the_name() -> None:
    """§2.2: the name is the BASENAME (a note's stem), never the relative
    path and never the filename — signals #1/#2/#4 compare a TAG to it, and
    ``.md`` in the name would put a literal `md` token in every candidate."""
    [candidate] = generate_note_candidates([note("/v/areas/relationships/eduardo-pontes-reis.md")])
    assert candidate.name == "eduardo-pontes-reis"
    assert candidate.path == "/v/areas/relationships/eduardo-pontes-reis.md"
    assert candidate.kind == KIND_NOTE
    assert candidate.type == "areas"  # the PLURAL para_folders key, as folders use


def test_note_match_keys_are_exactly_the_four_documented_ones() -> None:
    record = note(
        "/v/areas/people/ed.md",
        aliases=["Eduardo Pontes Reis", "capture_123", "2025-08-19T09:47:51.213957+00:00"],
        identifier="eduardo-id",
        title="Eduardo",
        capture_id="cap-1",
    )
    assert note_match_keys(record) == frozenset(
        {"ed", "eduardo-pontes-reis", "eduardo-id", "eduardo"}
    )


def test_note_match_keys_exclude_the_capture_alias_the_capture_id_and_timestamps() -> None:
    """§3.1 (b)/(c): the same exclusions signal #5 already applies. Guard
    deleted ⇒ these three values would appear as keys."""
    record = note(
        "/v/areas/x/n.md",
        aliases=["capture_x", "cap-1", "2026-04-08T16:51:24.690160+00:00"],
        identifier="2026-04-08 16:51:24",
        capture_id="cap-1",
        title="n",
    )
    assert note_match_keys(record) == frozenset({"n"})
    # Firing control: the same shapes WITHOUT the excluded property are keys.
    plain = note("/v/areas/x/n.md", aliases=["real-alias"], identifier="real-id", title="n")
    assert note_match_keys(plain) == frozenset({"n", "real-alias", "real-id"})


def test_note_keys_are_built_by_calling_the_shared_normalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4 #2 / §7.10 (b): note keys must CALL ``frontmatter.normalize_tag``,
    never re-implement it. Patch the normalizer and the keys must change."""
    import organize_core.suggest as suggest_mod

    monkeypatch.setattr(suggest_mod.frontmatter, "normalize_tag", lambda value, m=None: "PATCHED")
    assert note_match_keys(note("/v/areas/x/eduardo.md", aliases=["e"])) == frozenset({"PATCHED"})


def test_a_note_is_not_a_candidate_unless_it_is_under_a_non_archive_para_root() -> None:
    records = [
        note("/v/areas/a.md", para_type="area"),
        note("/v/projects/b.md", para_type="project"),
        note("/v/resources/c.md", para_type="resource"),
        note("/v/archive/d.md", para_type="archive"),
        note("/v/capture/raw_capture/e.md", para_type="capture"),
        note("/v/dailies/f.md", para_type="other"),
    ]
    assert [c.path for c in generate_note_candidates(records)] == [
        "/v/areas/a.md",
        "/v/projects/b.md",
        "/v/resources/c.md",
    ]


def test_a_notes_tags_are_not_match_keys() -> None:
    """§3.1: keys are NAMES only. Matching a capture tag against a note's
    tags is topical similarity — an EIGHTH signal, which 04 §2 forbids."""
    record = note("/v/areas/x/some-note.md", tags=["productivity", "health"])
    assert note_match_keys(record) == frozenset({"some-note"})
    candidate = generate_note_candidates([record])[0]
    score, reasons = calculate_score(
        view(tags=("productivity",), normalized_tags=("productivity",)),
        candidate,
        DEFAULTS,
        EMPTY_LEARNING,
        now=NOW,
    )
    assert reasons == []
    assert score == pytest.approx(B_AREAS)


# ---------------------------------------------------------------------------
# §3.1 — one fire per (token, candidate) pair
# ---------------------------------------------------------------------------


def test_a_signal_fires_once_per_token_even_with_redundant_keys() -> None:
    """§7.5: a note with aliases that all normalize to its stem scores
    IDENTICALLY to a note with none. Sabotage check: making the key set
    iterate rather than test membership breaks this."""
    plain = generate_note_candidates([note("/v/areas/x/impro.md", title="impro")])[0]
    redundant = generate_note_candidates(
        [
            note(
                "/v/areas/y/impro.md",
                aliases=["Impro", "impro", "IMPRO"],
                identifier="impro",
                title="Impro",
            )
        ]
    )[0]
    capture = view(tags=("impro",), normalized_tags=("impro",), sources=("impro",))
    a = calculate_score(capture, plain, DEFAULTS, EMPTY_LEARNING, now=NOW)
    b = calculate_score(capture, redundant, DEFAULTS, EMPTY_LEARNING, now=NOW)
    assert a[0] == b[0] == pytest.approx(W_EXACT + W_NORMALIZED + W_SOURCE + B_AREAS)  # 5.0
    assert a[1] == b[1]


def test_signal_2_fires_once_even_when_SEVERAL_keys_variant_match() -> None:
    """The sharp edge of §7.5, since keys that normalize identically merge
    into one set entry: the MORPHOLOGICAL rule can have two DISTINCT keys
    both reach one token (`reflections` and `reflection-system` both reach
    `reflection`). The weight is still contributed once — otherwise a note
    with redundant aliases silently outscores an identical note without."""
    one_key = generate_note_candidates([note("/v/areas/x/reflections.md", title="reflections")])[0]
    many_keys = generate_note_candidates(
        [
            note(
                "/v/areas/y/reflections.md",
                aliases=["reflection-system", "reflection-systems"],
                title="reflections",
            )
        ]
    )[0]
    assert len(many_keys.normalized_keys) == 3
    capture = view(normalized_tags=("reflection",))
    a = calculate_score(capture, one_key, DEFAULTS, EMPTY_LEARNING, now=NOW)
    b = calculate_score(capture, many_keys, DEFAULTS, EMPTY_LEARNING, now=NOW)
    assert a[0] == b[0] == pytest.approx(W_NORMALIZED + B_AREAS)  # 1.7, once
    assert len(b[1]) == 1


# ---------------------------------------------------------------------------
# §3.2 — notes score identically to folders; the tiebreak; the collapse
# ---------------------------------------------------------------------------


def test_a_note_and_a_folder_of_the_same_name_score_the_same_number() -> None:
    """§3.2: identically — same signals, same weights, same type bonus. No
    note bonus, no note penalty (refused on the record)."""
    folder = generate_candidates(folders(areas=["/v/areas/relationships/eduardo-pontes-reis"]))[0]
    note_candidate = generate_note_candidates(
        [note("/v/areas/relationships/eduardo-pontes-reis.md")]
    )[0]
    capture = view(sources=("eduardo-pontes-reis",))
    folder_score, folder_reasons = calculate_score(
        capture, folder, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    note_score, note_reasons = calculate_score(
        capture, note_candidate, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert folder_score == note_score == pytest.approx(W_SOURCE + B_AREAS)  # 1.5
    assert folder_reasons == note_reasons == ["Source 'eduardo-pontes-reis' matches"]


def test_the_note_wins_a_tie_on_score_and_name() -> None:
    """§3.2's kind_rank: it can only reorder candidates that tie on BOTH
    score and name, and it is asserted where the PATH tiebreak would have
    decided the other way — a folder whose path sorts first. (With the
    eduardo pair the note happens to win on path too, so that pair alone
    cannot tell a working kind_rank from a deleted one.)"""
    candidates = generate_candidates(
        folders(areas=["/v/areas/a-first/eduardo-pontes-reis"])
    ) + generate_note_candidates([note("/v/areas/z-last/eduardo-pontes-reis.md")])
    ranked = suggest(
        view(sources=("eduardo-pontes-reis",)), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert [(s.path, s.destination_kind, s.score) for s in ranked] == [
        ("/v/areas/z-last/eduardo-pontes-reis.md", KIND_NOTE, 1.5),
        ("/v/areas/a-first/eduardo-pontes-reis", KIND_FOLDER, 1.5),
    ]
    # …and the real pair from §3.2, which ties on score AND name too.
    pair = generate_candidates(
        folders(areas=["/v/areas/relationships/relationship-data/eduardo-pontes-reis"])
    ) + generate_note_candidates([note("/v/areas/relationships/eduardo-pontes-reis.md")])
    assert [
        s.destination_kind
        for s in suggest(
            view(sources=("eduardo-pontes-reis",)), pair, DEFAULTS, EMPTY_LEARNING, now=NOW
        )
    ] == [KIND_NOTE, KIND_FOLDER]


def test_kind_rank_never_reorders_candidates_that_differ_on_name() -> None:
    """The other branch of the refusal: kind_rank sits BELOW name in the sort
    key, so a folder that sorts first by name stays first even against a
    note of equal score."""
    candidates = generate_candidates(folders(areas=["/v/areas/aaa"])) + generate_note_candidates(
        [note("/v/areas/zzz.md")]
    )
    ranked = suggest(
        view(sources=("aaa", "zzz")), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert [s.name for s in ranked] == ["aaa", "zzz"]


def test_two_same_named_folders_are_never_collapsed() -> None:
    """§2.2 / §7.9's second half: two same-named FOLDERS are two
    destinations. Collapsing them would silently hide a real one."""
    candidates = generate_candidates(folders(projects=["/v/projects/a/dist", "/v/projects/b/dist"]))
    result = rank(view(tags=("dist",)), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW)
    assert [s.path for s in result.suggestions] == ["/v/projects/a/dist", "/v/projects/b/dist"]
    assert result.suppressed_duplicates == 0


def test_same_named_notes_at_the_same_score_collapse_to_the_shallowest() -> None:
    """§7.9 on the real eduardo pair: one row survives, it is the SHALLOWER
    path, and ``suppressed_duplicates`` reports 1."""
    candidates = generate_note_candidates(
        [
            note("/v/areas/relationships/eduardo-pontes-reis.md"),
            note(
                "/v/areas/relationships/relationship-data/eduardo-pontes-reis/"
                "eduardo-pontes-reis.md"
            ),
        ]
    )
    result = rank(
        view(sources=("eduardo-pontes-reis",)), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW
    )
    assert [s.path for s in result.suggestions] == [
        "/v/areas/relationships/eduardo-pontes-reis.md"
    ]
    assert result.suppressed_duplicates == 1


def test_same_named_notes_at_DIFFERENT_scores_are_two_answers() -> None:
    """The refusal predicate's other branch: the collapse keys on
    (normalized_name, score), so a genuinely better-scoring namesake is not
    swallowed by a shallower one."""
    candidates = generate_note_candidates(
        [
            note("/v/areas/reflection.md", para_type="area"),
            note("/v/projects/deep/reflection.md", para_type="project"),
        ]
    )
    result = rank(view(tags=("reflection",)), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW)
    assert [round(s.score, 6) for s in result.suggestions] == [
        W_EXACT + B_PROJECTS,
        W_EXACT + B_AREAS,
    ]
    assert result.suppressed_duplicates == 0


# ---------------------------------------------------------------------------
# §3.3 — the wire, and how many notes appear
# ---------------------------------------------------------------------------


def test_destination_kind_is_never_inferred_from_a_trailing_dot_md() -> None:
    """§7.8: a folder may legally be named ``foo.md`` and doc 14's stranger
    is exactly the person whose vault contains one. The core STATES the
    kind. Both halves fail if anyone reintroduces an ``endswith('.md')``."""
    candidates = generate_candidates(folders(areas=["/v/areas/foo.md"])) + generate_note_candidates(
        [note("/v/areas/v1.2.notes.md")]
    )
    ranked = suggest(
        view(tags=("foo.md", "v1.2.notes"), normalized_tags=("foo.md", "v1.2.notes")),
        candidates,
        DEFAULTS,
        EMPTY_LEARNING,
        now=NOW,
    )
    kinds = {s.path: s.destination_kind for s in ranked}
    assert kinds["/v/areas/foo.md"] == KIND_FOLDER
    assert kinds["/v/areas/v1.2.notes.md"] == KIND_NOTE


def test_max_note_suggestions_caps_note_rows_and_folders_fill_the_remainder() -> None:
    """§3.3: the cap is on NOTE rows, applied after ranking. Honored-key
    test for ``suggestions.max_note_suggestions``."""
    # Distinct stems (so the §3.2 same-name collapse is not what is being
    # measured here), each reaching the tag through an alias key.
    candidates = generate_note_candidates(
        [note(f"/v/areas/n{i}/impro-{i}.md", aliases=["impro"]) for i in range(6)]
    ) + generate_candidates(folders(projects=["/v/projects/impro"]))
    capture = view(tags=("impro",))
    default = suggest(capture, candidates, DEFAULTS, EMPTY_LEARNING, now=NOW)
    assert [s.destination_kind for s in default].count(KIND_NOTE) == 3
    assert [s.destination_kind for s in default].count(KIND_FOLDER) == 1

    widened = suggest(
        capture,
        candidates,
        SuggestionsConfig(max_note_suggestions=6),
        EMPTY_LEARNING,
        now=NOW,
    )
    assert [s.destination_kind for s in widened].count(KIND_NOTE) == 6
    # …and 0 disables note rows entirely without touching the folder rows.
    off = suggest(
        capture, candidates, SuggestionsConfig(max_note_suggestions=0), EMPTY_LEARNING, now=NOW
    )
    assert [s.path for s in off] == ["/v/projects/impro"]


def test_note_candidates_false_restores_the_folders_only_ballot() -> None:
    """§3.6, the off switch — an honored-key test for
    ``suggestions.note_candidates``. The flag lives at the ballot boundary,
    so this asserts the boundary the server and CLI actually use."""
    records = [note("/v/areas/impro.md")]
    with_notes = build_candidate_set(folders(areas=["/v/areas/x"]), records, DEFAULTS)
    without = build_candidate_set(folders(areas=["/v/areas/x"]), [], DEFAULTS)
    assert len(with_notes) == 2
    assert len(without) == 1
    assert suggest(view(tags=("impro",)), without, DEFAULTS, EMPTY_LEARNING, now=NOW) == []


def test_a_note_is_never_a_candidate_for_itself() -> None:
    """§3.1's identity guard, on the resolved path."""
    candidates = generate_note_candidates(
        [note("/v/areas/impro.md"), note("/v/areas/other/impro.md")]
    )
    ranked = suggest(
        view(tags=("impro",)),
        candidates,
        DEFAULTS,
        EMPTY_LEARNING,
        now=NOW,
        exclude_path="/v/areas/impro.md",
    )
    assert [s.path for s in ranked] == ["/v/areas/other/impro.md"]
    # Firing control: without the exclusion it is offered.
    assert "/v/areas/impro.md" in [
        s.path for s in suggest(view(tags=("impro",)), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW)
    ]


# ---------------------------------------------------------------------------
# §3.4 — stopwords, both directions
# ---------------------------------------------------------------------------


def test_a_stopword_token_does_not_reach_its_namesake_note() -> None:
    """§7.6 (a): `source: mind` must not drag every capture to a note called
    `mind`. 195 of the 1,740 newly covered real captures did exactly that."""
    candidates = generate_note_candidates(
        [note("/v/areas/personal-brand/life-inventory/mind.md")]
    )
    assert suggest(view(sources=("mind",)), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW) == []
    # Firing control: the same token with the list emptied DOES reach it.
    permissive = SuggestionsConfig(candidate_stopwords=[])
    assert [
        s.path for s in suggest(view(sources=("mind",)), candidates, permissive, EMPTY_LEARNING, now=NOW)
    ] == ["/v/areas/personal-brand/life-inventory/mind.md"]


def test_a_capture_whose_only_token_is_a_stopword_gets_archive_only() -> None:
    """§7.6 (b): the filter must not be replaced by a fabricated hit
    somewhere else — the honest answer is the archive row alone."""
    candidates = generate_candidates(folders(areas=["/v/areas/mind"])) + generate_note_candidates(
        [note("/v/areas/mind.md"), note("/v/areas/thought.md")]
    )
    ranked = suggest(
        view(tags=("mind",), normalized_tags=("mind",), sources=("mind",)),
        candidates,
        DEFAULTS,
        EMPTY_LEARNING,
        now=NOW,
        archive_path="/v/archive/capture/raw_capture",
    )
    assert [(s.name, s.type) for s in ranked] == [(ARCHIVE_SUGGESTION_NAME, "archive")]


def test_stopwords_apply_uniformly_to_folders_and_notes() -> None:
    """§3.4: a stopword is a property of the TOKEN, not of the destination
    kind; a rule that applied only to notes would make the same token mean
    two things."""
    folder = generate_candidates(folders(areas=["/v/areas/voice"]))[0]
    note_candidate = generate_note_candidates([note("/v/areas/voice.md")])[0]
    capture = view(tags=("voice",), normalized_tags=("voice",))
    for candidate in (folder, note_candidate):
        score, reasons = calculate_score(capture, candidate, DEFAULTS, EMPTY_LEARNING, now=NOW)
        assert reasons == []
        assert score == pytest.approx(B_AREAS)


def test_the_stopword_list_is_the_spec_default_and_is_normalized() -> None:
    assert SuggestionsConfig().candidate_stopwords == [
        "mind",
        "self",
        "me",
        "text",
        "voice",
        "note",
        "thought",
    ]
    # Casing/underscores are normalized THROUGH the shared normalizer, so a
    # tag written `Mind` is filtered too — and the config may be written in
    # any casing.
    candidates = generate_note_candidates([note("/v/areas/mind.md")])
    assert suggest(view(tags=("Mind",)), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW) == []


def test_stopwords_never_touch_the_shared_normalizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """§4 #2 / SQ-4: the filter is MATCH-TIME. ``normalize_tag`` also builds
    learning keys and the frontmatter a move writes to disk, so a filter that
    mutated it would corrupt data on disk. It is CALLED, never modified."""
    import organize_core.suggest as suggest_mod

    calls: list[str] = []
    original = frontmatter.normalize_tag

    def spy(value: str, extra: dict[str, str] | None = None) -> str:
        calls.append(value)
        return original(value, extra)

    monkeypatch.setattr(suggest_mod.frontmatter, "normalize_tag", spy)
    calculate_score(
        view(tags=("mind",)),
        generate_note_candidates([note("/v/areas/mind.md")])[0],
        DEFAULTS,
        EMPTY_LEARNING,
        now=NOW,
    )
    assert "mind" in calls  # the filter went through the shared normalizer
    assert frontmatter.normalize_tag("Deep_Work Habits") == "deep-work-habits"


# ---------------------------------------------------------------------------
# §3.5 — the prefilters are OUTPUT-PRESERVING
# ---------------------------------------------------------------------------


def _reference_shortlist(
    candidates: list[Candidate], capture: CaptureFeaturesView, config: SuggestionsConfig
) -> list[Candidate]:
    """Brute force: score EVERY candidate the way the pre-index ranker did
    and keep the ones that scored a positive signal."""
    kept = []
    for candidate in candidates:
        score, _ = calculate_score(capture, candidate, config, EMPTY_LEARNING, now=NOW)
        bonus = {"projects": B_PROJECTS, "areas": B_AREAS, "resources": B_RESOURCES}.get(
            candidate.type, 0.0
        )
        if score - bonus > 0:
            kept.append(candidate)
    return kept


def test_the_inverted_index_never_loses_a_candidate_that_would_score() -> None:
    """§3.5: the prefilters are a COST fix, not a behaviour fix. Randomized
    corpus, fixed seed, brute-force reference — a prefilter that dropped a
    scoring candidate fails here rather than in Matt's backlog."""
    rng = random.Random(20260816)
    words = ["impro", "health", "growth-system", "mind", "eduardo", "reflection", "ab", "abc"]
    candidates = generate_candidates(
        folders(
            areas=[f"/v/areas/{rng.choice(words)}-{i}" for i in range(40)],
            projects=[f"/v/projects/{rng.choice(words)}" for i in range(40)],
        )
    ) + generate_note_candidates(
        [
            note(f"/v/areas/n{i}/{rng.choice(words)}.md", aliases=[rng.choice(words)])
            for i in range(60)
        ]
    )
    indexed = CandidateSet(candidates, DEFAULTS)
    for _ in range(120):
        capture = view(
            tags=tuple(rng.sample(words, k=rng.randint(0, 3))),
            normalized_tags=tuple(rng.sample(words, k=rng.randint(0, 3))),
            sources=tuple(rng.sample(words, k=rng.randint(0, 2))),
            aliases=tuple(rng.sample(words, k=rng.randint(0, 2))),
            context=tuple(rng.sample(words, k=rng.randint(0, 2))),
        )
        expected = _reference_shortlist(candidates, capture, DEFAULTS)
        got = set(indexed.shortlist(capture, DEFAULTS, EMPTY_LEARNING))
        assert set(expected) <= got, [c.path for c in expected if c not in got]


def test_the_alias_prefilter_keeps_every_pair_that_clears_the_gate() -> None:
    """§3.5's signal-#5 row, as a property: the length AND bigram AND
    character bounds together may never drop a pair whose true similarity is
    above 0.6. Brute-forced against ``string_similarity`` itself."""
    rng = random.Random(4242)
    alphabet = "abcde -x"
    names = [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14))) for _ in range(600)
    ]
    candidates = [
        Candidate(path=f"/v/areas/{i}", name=name, normalized_name=name, type="areas")
        for i, name in enumerate(names)
    ]
    indexed = CandidateSet(candidates, DEFAULTS)
    for _ in range(120):
        alias = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
        shortlisted = indexed._alias_shortlist(alias)
        truth = {i for i, name in enumerate(names) if string_similarity(alias, name) > 0.6}
        assert truth <= shortlisted, [names[i] for i in truth - shortlisted]


def test_the_gated_similarity_agrees_with_the_documented_formula() -> None:
    """The cutoff edit distance is EXACT wherever the gate is cleared —
    equal floats, not merely the same verdict."""
    rng = random.Random(99)
    alphabet = "abc de"
    for _ in range(2000):
        a = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        b = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        exact = string_similarity(a, b)
        gated = gated_similarity(a, b)
        assert (exact > 0.6) == (gated > 0.6)
        if exact > 0.6:
            assert exact == gated


def test_string_similarity_is_unchanged_by_the_cutoff_parameter() -> None:
    """The historic numeric goldens (08 §A7) still hold — the cutoff is
    opt-in, and the public function does not opt in."""
    assert string_similarity("health", "health") == 1.0
    assert string_similarity("health", "zzzzzzzzzz") == 0.0
    assert string_similarity("helth", "health") == pytest.approx(5 / 6, rel=1e-12)
    assert string_similarity("kitten", "sitting") == pytest.approx(4 / 7, rel=1e-12)


# ---------------------------------------------------------------------------
# §4 — the parity guard, and §7.3's negative twin
# ---------------------------------------------------------------------------


def test_folder_only_scoring_is_byte_identical_to_the_pre_change_rule() -> None:
    """§3.1's "refactor with byte-identical folder behaviour": the folder key
    sets ARE the old comparisons. Asserted against the literal old rule for a
    folder whose raw name and normalized name differ."""
    candidate = Candidate(
        path="/v/areas/Deep_Work Habits",
        name="Deep_Work Habits",
        normalized_name=frontmatter.normalize_tag("Deep_Work Habits"),
        type="areas",
    )
    assert candidate.match_keys == frozenset({"Deep_Work Habits", "deep-work-habits"})
    assert candidate.normalized_keys == frozenset({"deep-work-habits"})


def test_a_capture_that_matches_nothing_still_gets_the_archive_entry_alone() -> None:
    """§7.3, the negative twin: widening the ballot must not buy recall by
    relaxing the floor. This is SQ-1's guard in unit form."""
    candidates = generate_candidates(
        folders(projects=["/v/projects/blog"], areas=["/v/areas/health"])
    ) + generate_note_candidates([note("/v/areas/health/index.md"), note("/v/projects/blog/x.md")])
    ranked = suggest(
        view(tags=("zzz-nothing",), normalized_tags=("zzz-nothing",)),
        candidates,
        DEFAULTS,
        EMPTY_LEARNING,
        now=NOW,
        archive_path="/v/archive/capture/raw_capture",
    )
    assert [(s.name, s.type, s.score) for s in ranked] == [
        (ARCHIVE_SUGGESTION_NAME, "archive", 0.1)
    ]


def test_the_type_bonus_alone_never_carries_a_note_onto_the_list() -> None:
    """The ``signal_score <= 0`` floor (the 2026-08-16 ruling that killed
    SQ-1) is unchanged in value and position, and now guards 7,000 notes
    instead of 135 folders."""
    candidates = generate_note_candidates([note(f"/v/projects/n{i}.md", para_type="project") for i in range(50)])
    assert suggest(view(), candidates, DEFAULTS, EMPTY_LEARNING, now=NOW) == []
    assert (
        suggest(
            view(),
            candidates,
            SuggestionsConfig(learning=LearningConfig(min_confidence=0.0)),
            EMPTY_LEARNING,
            now=NOW,
        )
        == []
    )


def test_every_non_archive_suggestion_still_carries_at_least_one_reason() -> None:
    candidates = generate_candidates(folders(areas=["/v/areas/impro"])) + generate_note_candidates(
        [note("/v/areas/impro.md"), note("/v/areas/other.md")]
    )
    ranked = suggest(
        view(tags=("impro",), normalized_tags=("impro",)),
        candidates,
        DEFAULTS,
        EMPTY_LEARNING,
        now=NOW,
        archive_path="/v/archive",
    )
    assert len(ranked) == 3
    for suggestion in ranked:
        assert suggestion.reasons


# ---------------------------------------------------------------------------
# §7.10 — structural pins (the SQ-4 pattern)
# ---------------------------------------------------------------------------


def test_suggest_reads_exactly_the_seven_documented_weights() -> None:
    """§7.10 (c): 04 §2 fixes the signal list at SEVEN and forbids additions.
    An eighth weight would have to be read from `config.weights`, so the set
    of weight attributes the module touches is pinned to the literal seven."""
    import ast
    import inspect

    import organize_core.suggest as suggest_mod

    tree = ast.parse(inspect.getsource(suggest_mod))
    read: set[str] = set()

    def is_weights(node: ast.expr) -> bool:
        return (isinstance(node, ast.Name) and node.id == "weights") or (
            isinstance(node, ast.Attribute) and node.attr == "weights"
        )

    for node in ast.walk(tree):
        # `weights.exact_tag_match` (the local) and `config.weights.type_bonus`
        # (the long form) are both weight reads.
        if isinstance(node, ast.Attribute) and is_weights(node.value):
            read.add(node.attr)
        # …and so is `getattr(weights, "note_bonus", 0.0)`, which is how an
        # eighth weight would sneak past an attribute-only pin.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and is_weights(node.args[0])
        ):
            name = node.args[1] if len(node.args) > 1 else None
            read.add(name.value if isinstance(name, ast.Constant) else "<dynamic>")
    assert read == {
        "exact_tag_match",
        "normalized_tag_match",
        "learned_association",
        "source_match",
        "alias_similarity",
        "context_match",
        "type_bonus",
    }


def test_the_floor_subtracts_the_type_bonus_before_comparing_it() -> None:
    """§7.10 (d): the 2026-08-16 ruling that killed SQ-1, pinned in VALUE and
    in POSITION — the subtraction must happen, and it must happen before the
    comparison, or the always-firing signal #7 decides survival again."""
    import inspect

    from organize_core.suggest import rank as rank_fn

    source = inspect.getsource(rank_fn)
    subtraction = source.index("signal_score = score - _type_bonus(candidate.type, config)")
    comparison = source.index("if signal_score <= 0.0 or signal_score < min_confidence:")
    assert subtraction < comparison


def test_the_archives_exclusion_is_one_definition_shared_with_the_walk() -> None:
    """21 §2.3: the folder walk and the scorer must agree about what
    "archive" means. Two frozensets that happened to be equal today would
    drift; this asserts they are the SAME object."""
    from organize_core.index import EXCLUDED_CANDIDATE_PARA_KEYS
    from organize_core.suggest import _EXCLUDED_CANDIDATE_TYPES

    assert _EXCLUDED_CANDIDATE_TYPES is EXCLUDED_CANDIDATE_PARA_KEYS
    assert EXCLUDED_CANDIDATE_PARA_KEYS == frozenset({"archives", "archive"})


# ---------------------------------------------------------------------------
# §5.2 — the named regression case, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "person", ["eduardo-pontes-reis", "james-fang", "jennifer-kesteloot", "flor-laorga"]
)
def test_the_person_class_is_suggested_with_its_exact_arithmetic(person: str) -> None:
    """§5.2: Matt's complaint becomes an assertion, with its arithmetic
    visible — 1.3 source_match + 0.2 areas bonus = 1.5 exactly. A membership
    assertion is explicitly not acceptable (§7.2)."""
    candidates = build_candidate_set(
        folders(areas=[f"/v/areas/relationships/relationship-data/{person}"]),
        [note(f"/v/areas/relationships/{person}.md")],
        DEFAULTS,
    )
    ranked = suggest(
        view(sources=("mind", person), tags=("growth", "things-i-enjoy")),
        candidates,
        DEFAULTS,
        EMPTY_LEARNING,
        now=NOW,
        archive_path="/v/archive/capture/raw_capture",
    )
    top = ranked[0]
    assert top.path == f"/v/areas/relationships/{person}.md"
    assert top.destination_kind == KIND_NOTE
    assert top.type == "area"
    assert top.score == 1.5
    assert top.reasons == (f"Source '{person}' matches",)
    # …and the sibling FOLDER is still offered, one row down (§2.2).
    assert (ranked[1].path, ranked[1].destination_kind) == (
        f"/v/areas/relationships/relationship-data/{person}",
        KIND_FOLDER,
    )
    assert ranked[-1].name == ARCHIVE_SUGGESTION_NAME
