"""``ActionRecorder.query_similar`` — the spec 13 §3 retrieval seam on doc 12.

Spec 13 §3, verbatim: "12: corpus queryable by tag/content similarity
(``actions query --similar-to <text>``); ``chosen_rank``/``verdict`` fields
populated."  Spec 13 §2 names the consumer: "**precedent retrieval from the
action corpus** (12): nearest past actions by tag/content similarity, 'when
Matt captured things like this he appended them to X'".

Every corpus here is synthetic and every expected score is HAND-COMPUTED from
the documented formula, so a change to the formula shows up as a red literal
rather than as a quietly different ranking.  The corpora are sized so that
every overlap coefficient is an exact binary fraction (denominators 1, 2 and
4) — that is why the golden assertions are ``==`` on floats and not
``approx``.

Test standards applied (ARCHITECTURE.md §"Test anti-vacuity standards",
PERMANENT, @35cf7c0):
1. refusal-predicate pins carry a guard-deleted check and a firing control;
2. constants are asserted as LITERALS with a separate agreement line;
3. must-not-be-connected invariants pin the CONNECTION, not today's
   consequence.

Recorders write to a tmp-dir ``actions/`` only — never a real home path,
never ``~/.local/share/organize-core`` (paths.py isolation contract).
"""

from __future__ import annotations

import ast
import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest

from organize_core.actions import (
    SIMILARITY_WEIGHTS,
    ActionContext,
    ActionRecord,
    ActionRecorder,
    ActionSchemaError,
    CaptureState,
    SimilarAction,
    TargetState,
    tokenize,
)

# --- corpus builders -------------------------------------------------------

#: The query used by the golden corpus. FOUR tokens, so every denominator
#: below is ``min(4, |record set|)`` ∈ {1, 2, 4} — all exact binary fractions.
GOLDEN_QUERY = "Bench press strength training"

#: The destination every fitness record files into. Stem tokens:
#: ``{areas, fitness, strength, training}`` — four, of which the query shares
#: ``{strength, training}``, so the destination component is exactly 2/4.
FITNESS_DEST = "areas/fitness/strength-training.md"


@pytest.fixture()
def actions_dir(tmp_path: Path) -> Path:
    """The injected ``<state>/actions/`` — deliberately NOT pre-created."""
    return tmp_path / "state" / "actions"


def make_record(
    *,
    id: str,
    ts: str,
    body: str,
    tags: Any = None,
    tags_after: Any = None,
    auto_tags: tuple[str, ...] = (),
    targets: tuple[TargetState, ...] = (),
    operation: str = "move",
    actor: str = "matt",
    dry_run: bool = False,
) -> ActionRecord:
    frontmatter_before: dict[str, Any] = {} if tags is None else {"tags": tags}
    frontmatter_after: dict[str, Any] | None = None if tags_after is None else {"tags": tags_after}
    return ActionRecord(
        id=id,
        ts=ts,
        actor=actor,
        operation=operation,  # type: ignore[arg-type]
        capture=CaptureState(
            path=f"capture/raw_capture/{id}.md",
            content_hash="sha256:" + id,
            frontmatter_before=frontmatter_before,
            body_before=body,
            frontmatter_after=frontmatter_after,
        ),
        targets=targets,
        context=ActionContext(auto_tags_present=auto_tags, dry_run=dry_run),
    )


def dest(path: str, *, description: str | None = None) -> TargetState:
    return TargetState(
        path=path,
        role="destination",
        before_hash=None,
        after_hash="sha256:after",
        diff="",
        description=description,
    )


def store(actions_dir: Path, records: list[ActionRecord]) -> ActionRecorder:
    recorder = ActionRecorder(actions_dir)
    for record in records:
        assert recorder.record(record) is True
    return recorder


# --- the golden corpus -----------------------------------------------------
#
# Query tokens Q = {bench, press, strength, training}, |Q| = 4.
# ``tags=None`` (the CLI's single-argument form), so the tag component reuses
# Q as its query set and Q_all = Q.
#
# | id | text component            | tag component        | destination        | score |
# |----|---------------------------|----------------------|--------------------|-------|
# | R3 | 4/min(4,4)=1.0            | {strength,training}  | 2/min(4,4)=0.5     | 3.25  |
# |    |                           | 2/min(4,2)=1.0       | (best of 2 targets)|       |
# | R2 | {squat,session}: 0/2=0.0  | {bench,press}=1.0    | none (a skip)      | 2.00  |
# | R7 | 4/min(4,5)=1.0            | no tags: 0.0         | 2/4=0.5            | 1.25  |
# | R6 | identical to R7           | 0.0                  | 0.5                | 1.25  |
# | R1 | 4/min(4,5)=1.0            | {fitness}: 0/1=0.0   | 2/4=0.5            | 1.25  |
# | R4 | 0.0                       | {reading}: 0.0       | 2/4=0.5            | 0.25  |
# | R5 | 0.0                       | 0.0                  | 0.0                | DROP  |
#
# score = 1.0·text + 2.0·tags + 0.5·destination  (SIMILARITY_WEIGHTS)


def golden_records() -> list[ActionRecord]:
    return [
        make_record(
            id="act_R1",
            ts="2026-08-01T10:00:00Z",
            body="Bench press strength training notes",
            tags=["fitness"],
            targets=(dest(FITNESS_DEST),),
        ),
        make_record(
            id="act_R2",
            ts="2026-08-02T10:00:00Z",
            body="Squat session",
            tags=["bench-press"],
            operation="skip",
            targets=(),
        ),
        make_record(
            id="act_R3",
            ts="2026-08-03T10:00:00Z",
            body="Bench press strength training",
            tags=["strength-training"],
            targets=(dest("projects/inbox.md"), dest(FITNESS_DEST)),
        ),
        make_record(
            id="act_R4",
            ts="2026-08-04T10:00:00Z",
            body="Reading list for the week",
            tags=["reading"],
            targets=(dest(FITNESS_DEST),),
        ),
        make_record(
            id="act_R5",
            ts="2026-08-05T10:00:00Z",
            body="Grocery list milk eggs",
            tags=["errands"],
            targets=(dest("areas/household/errands.md"),),
        ),
        make_record(
            id="act_R6",
            ts="2026-08-06T10:00:00Z",
            body="Bench press strength training notes",
            targets=(dest(FITNESS_DEST),),
        ),
        # Same timestamp as R6 to the second — only the id can order them.
        make_record(
            id="act_R7",
            ts="2026-08-06T10:00:00Z",
            body="Bench press strength training notes",
            targets=(dest(FITNESS_DEST),),
        ),
    ]


# --- weights: literal values, then agreement (standard 2) ------------------


def test_similarity_weights_are_the_documented_literals() -> None:
    assert SIMILARITY_WEIGHTS["text"] == 1.0
    assert SIMILARITY_WEIGHTS["tags"] == 2.0
    assert SIMILARITY_WEIGHTS["destination"] == 0.5
    assert sorted(SIMILARITY_WEIGHTS) == ["destination", "tags", "text"]


def test_the_weight_constants_agree_with_the_scores_the_engine_produces(
    actions_dir: Path,
) -> None:
    """The agreement line for standard 2: assert the module constant is what
    the engine actually multiplies by, so flipping a weight cannot pass by
    everyone reading the same constant."""
    recorder = store(
        actions_dir,
        [
            # text 1.0, tags 0.0, destination 0.0  ⇒  score == weight["text"]
            make_record(id="act_T", ts="2026-08-01T00:00:00Z", body="bench press"),
            # text 0.0, tags 1.0, destination 0.0  ⇒  score == weight["tags"]
            make_record(id="act_G", ts="2026-08-02T00:00:00Z", body="zzz", tags=["bench-press"]),
            # text 0.0, tags 0.0, destination 1.0  ⇒  score == weight["destination"]
            make_record(
                id="act_D",
                ts="2026-08-03T00:00:00Z",
                body="zzz",
                targets=(dest("bench/press.md"),),
            ),
        ],
    )
    by_id = {hit.record.id: hit for hit in recorder.query_similar("bench press", limit=None)}

    assert by_id["act_T"].score == 1.0
    assert by_id["act_G"].score == 2.0
    assert by_id["act_D"].score == 0.5
    assert by_id["act_T"].score == SIMILARITY_WEIGHTS["text"]
    assert by_id["act_G"].score == SIMILARITY_WEIGHTS["tags"]
    assert by_id["act_D"].score == SIMILARITY_WEIGHTS["destination"]


# --- golden ranking --------------------------------------------------------


def test_golden_ranking_over_the_synthetic_corpus(actions_dir: Path) -> None:
    """Hand-computed order AND hand-computed scores, both as literals."""
    recorder = store(actions_dir, golden_records())

    hits = recorder.query_similar(GOLDEN_QUERY, limit=None)

    assert [hit.record.id for hit in hits] == [
        "act_R3",  # 3.25 — text + tags + destination
        "act_R2",  # 2.00 — tag-only, and it is a `skip`
        "act_R7",  # 1.25 — ties R6 on ts, wins on the higher id
        "act_R6",  # 1.25 — ties R1 on score, wins on the later ts
        "act_R1",  # 1.25
        "act_R4",  # 0.25 — destination only
    ]
    assert [hit.score for hit in hits] == [3.25, 2.0, 1.25, 1.25, 1.25, 0.25]


def test_golden_components_are_hand_computable_one_by_one(actions_dir: Path) -> None:
    recorder = store(actions_dir, golden_records())
    by_id = {hit.record.id: hit for hit in recorder.query_similar(GOLDEN_QUERY, limit=None)}

    assert (by_id["act_R3"].text_score, by_id["act_R3"].tag_score) == (1.0, 1.0)
    assert by_id["act_R3"].destination_score == 0.5
    assert (by_id["act_R2"].text_score, by_id["act_R2"].tag_score) == (0.0, 1.0)
    assert by_id["act_R2"].destination_score == 0.0
    assert (by_id["act_R1"].text_score, by_id["act_R1"].tag_score) == (1.0, 0.0)
    assert by_id["act_R1"].destination_score == 0.5
    assert (by_id["act_R4"].text_score, by_id["act_R4"].tag_score) == (0.0, 0.0)
    assert by_id["act_R4"].destination_score == 0.5


def test_a_record_that_matches_nothing_is_omitted_not_returned_with_score_zero(
    actions_dir: Path,
) -> None:
    """R5 shares no token with the query on any component. Returning it with
    score 0.0 would let an arbitrary tail of the corpus fill a ``limit`` and
    read as 'your nearest precedents' — 09 §1.5's silently-wrong answer."""
    recorder = store(actions_dir, golden_records())

    hits = recorder.query_similar(GOLDEN_QUERY, limit=None)

    assert "act_R5" not in {hit.record.id for hit in hits}
    assert len(hits) == 6
    assert all(hit.score > 0.0 for hit in hits)
    # Firing control: R5 IS in the corpus and IS retrievable by its own words.
    assert [h.record.id for h in recorder.query_similar("grocery milk eggs")] == ["act_R5"]


def test_limit_truncates_from_the_top_of_the_ranking(actions_dir: Path) -> None:
    recorder = store(actions_dir, golden_records())

    assert [hit.record.id for hit in recorder.query_similar(GOLDEN_QUERY, limit=2)] == [
        "act_R3",
        "act_R2",
    ]
    assert len(recorder.query_similar(GOLDEN_QUERY, limit=1)) == 1
    assert len(recorder.query_similar(GOLDEN_QUERY)) == 6  # default limit is 10


def test_the_ranking_does_not_depend_on_the_order_records_were_written(
    actions_dir: Path,
) -> None:
    """Determinism pin: ordering comes from (score, ts, id), never from the
    order lines happen to sit in the month file."""
    forwards = store(actions_dir, golden_records())
    backwards_dir = actions_dir.parent / "reversed"
    backwards = store(backwards_dir, list(reversed(golden_records())))

    assert [hit.record.id for hit in forwards.query_similar(GOLDEN_QUERY, limit=None)] == [
        hit.record.id for hit in backwards.query_similar(GOLDEN_QUERY, limit=None)
    ]
    assert [hit.score for hit in forwards.query_similar(GOLDEN_QUERY, limit=None)] == [
        hit.score for hit in backwards.query_similar(GOLDEN_QUERY, limit=None)
    ]


def test_repeated_queries_return_the_identical_ranking(actions_dir: Path) -> None:
    recorder = store(actions_dir, golden_records())
    runs = [
        [(hit.record.id, hit.score) for hit in recorder.query_similar(GOLDEN_QUERY, limit=None)]
        for _ in range(5)
    ]
    assert runs[1:] == runs[:-1]


# --- prefix / substring traps (the M2 'cap-1' vs 'cap-10' class) -----------
#
# ARCHITECTURE.md §"Real-data findings — fix stage" ruling 2 (@35cf7c0):
#   "Signal 6 matches at TOKEN granularity — a sanctioned deviation from 04
#   §2 #6's literal 'case-insensitive substring, either way'. … The raw
#   substring test made short folder names match inside unrelated words:
#   `resources/ui` was the rank-1 suggestion for a capture whose entire
#   context was 'quitting toastmasters' (the `ui` inside q-UI-tting)."


def test_cap_1_does_not_score_as_a_full_match_against_cap_10(actions_dir: Path) -> None:
    """Query ``cap-1`` ⇒ {cap, 1}. ``cap-10`` ⇒ {cap, 10}; ``1 != 10``, so the
    overlap is 1/2, not the 2/2 a prefix or substring test would produce."""
    recorder = store(
        actions_dir,
        [
            make_record(id="act_P1", ts="2026-08-01T00:00:00Z", body="cap-1"),
            make_record(id="act_P10", ts="2026-08-02T00:00:00Z", body="cap-10"),
            make_record(id="act_P100", ts="2026-08-03T00:00:00Z", body="cap-100"),
        ],
    )

    hits = recorder.query_similar("cap-1", limit=None)

    # P10 and P100 tie at 0.5; the later timestamp wins the tiebreak.
    assert [hit.record.id for hit in hits] == ["act_P1", "act_P100", "act_P10"]
    assert [hit.score for hit in hits] == [1.0, 0.5, 0.5]
    # The mutation this pins: substring matching would make all three 1.0.
    assert {hit.record.id: hit.score for hit in hits}["act_P10"] != 1.0
    assert {hit.record.id: hit.score for hit in hits}["act_P100"] != 1.0


def test_a_cap_10_destination_never_outranks_the_cap_1_destination(actions_dir: Path) -> None:
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_D1",
                ts="2026-08-01T00:00:00Z",
                body="unrelated",
                targets=(dest("areas/cap-1.md"),),
            ),
            make_record(
                id="act_D10",
                ts="2026-08-02T00:00:00Z",
                body="unrelated",
                targets=(dest("areas/cap-10.md"),),
            ),
        ],
    )

    hits = recorder.query_similar("cap-1", limit=None)

    # dest tokens {areas, cap, 1}: |Q ∩ D| = 2, min(2, 3) = 2 ⇒ 1.0 ⇒ 0.5
    # dest tokens {areas, cap, 10}: |Q ∩ D| = 1, min(2, 3) = 2 ⇒ 0.5 ⇒ 0.25
    assert [(hit.record.id, hit.score) for hit in hits] == [("act_D1", 0.5), ("act_D10", 0.25)]
    assert hits[0].destination_score == 1.0
    assert hits[1].destination_score == 0.5


def test_a_short_token_never_matches_inside_an_unrelated_word(actions_dir: Path) -> None:
    """The recorded `resources/ui` vs 'q-UI-tting' finding, as a retrieval
    pin: 'ui' is not a token of 'quitting', so the record is not returned."""
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_QUIT",
                ts="2026-08-01T00:00:00Z",
                body="quitting toastmasters",
                targets=(dest("resources/ui.md"),),
            ),
            make_record(
                id="act_UI",
                ts="2026-08-02T00:00:00Z",
                body="ui polish for the picker",
                targets=(dest("resources/ui.md"),),
            ),
        ],
    )

    hits = {hit.record.id: hit for hit in recorder.query_similar("ui", limit=None)}

    # Both records share the destination, so both are retrievable — the pin is
    # on the TEXT component, which is where substring matching would fire.
    assert hits["act_QUIT"].text_score == 0.0
    assert hits["act_UI"].text_score == 1.0
    assert (hits["act_QUIT"].score, hits["act_UI"].score) == (0.5, 1.5)
    assert [hit.record.id for hit in recorder.query_similar("ui", limit=None)] == [
        "act_UI",
        "act_QUIT",
    ]


def test_project_does_not_match_projects_without_a_configured_mapping(
    actions_dir: Path,
) -> None:
    """Token equality, not stemming. Morphological variants are ``suggest``'s
    sanctioned deviation for FOLDER matching (ARCHITECTURE.md §"Real-data
    findings" ruling 3, @35cf7c0: "in ``suggest.py`` ONLY"); retrieval does
    not invent them, it honors the configured map instead — see the next
    test, which is this one's firing control."""
    recorder = store(
        actions_dir,
        [make_record(id="act_PJ", ts="2026-08-01T00:00:00Z", body="zzz", tags=["projects"])],
    )

    assert recorder.query_similar("", tags=["project"], limit=None) == []


def test_the_configured_tag_normalization_map_reaches_the_corpus(actions_dir: Path) -> None:
    """Firing control for the test above, and the reason ``normalize_tag`` is
    called at all: ``suggestions.tag_normalization = {project = "projects"}``
    is real vault config (spec 04 §2 #2)."""
    recorder = store(
        actions_dir,
        [make_record(id="act_PJ", ts="2026-08-01T00:00:00Z", body="zzz", tags=["projects"])],
    )

    hits = recorder.query_similar(
        "", tags=["project"], tag_normalization={"project": "projects"}, limit=None
    )

    assert [(hit.record.id, hit.score) for hit in hits] == [("act_PJ", 2.0)]
    assert hits[0].tag_score == 1.0


# --- dry-run exclusion (refusal pin + guard-deleted check + control) -------


def test_dry_run_records_are_excluded_by_default(actions_dir: Path) -> None:
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_REAL", ts="2026-08-01T00:00:00Z", body="bench press", dry_run=False
            ),
            make_record(
                id="act_DRY", ts="2026-08-02T00:00:00Z", body="bench press", dry_run=True
            ),
        ],
    )

    hits = recorder.query_similar("bench press", limit=None)

    # The dry run is IDENTICAL in content, so it would outrank the real record
    # on the ts tiebreak if it were included: this cannot pass by score.
    assert [hit.record.id for hit in hits] == ["act_REAL"]
    assert hits[0].score == 1.0


def test_include_dry_run_opts_them_back_in(actions_dir: Path) -> None:
    """Guard-deleted check: with the exclusion turned off the dry run appears,
    and appears FIRST — so the assertion above is the guard firing, not the
    dry-run record being unreachable for some other reason."""
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_REAL", ts="2026-08-01T00:00:00Z", body="bench press", dry_run=False
            ),
            make_record(
                id="act_DRY", ts="2026-08-02T00:00:00Z", body="bench press", dry_run=True
            ),
        ],
    )

    hits = recorder.query_similar("bench press", limit=None, include_dry_run=True)

    assert [hit.record.id for hit in hits] == ["act_DRY", "act_REAL"]
    assert [hit.score for hit in hits] == [1.0, 1.0]


# --- skip records carry negative signal and must stay retrievable ---------


def test_skip_records_are_returned_and_keep_their_operation(actions_dir: Path) -> None:
    """Spec 12 §2: "a rejection is as much signal as an acceptance" — and doc
    13 §4's acceptance item "Rejected proposal → recorded; identical text
    re-proposed later must rank that destination lower" is unbuildable if the
    retrieval layer hides skips."""
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_MOVE",
                ts="2026-08-01T00:00:00Z",
                body="bench press",
                operation="move",
                targets=(dest(FITNESS_DEST),),
            ),
            make_record(
                id="act_SKIP",
                ts="2026-08-02T00:00:00Z",
                body="bench press",
                operation="skip",
                targets=(),
            ),
        ],
    )

    hits = recorder.query_similar("bench press", limit=None)

    assert {hit.record.id for hit in hits} == {"act_MOVE", "act_SKIP"}
    assert {hit.record.id: hit.record.operation for hit in hits}["act_SKIP"] == "skip"
    # A skip has no targets, so its destination component is 0 and it never
    # manufactures a destination the caller could mistake for a precedent.
    skip_hit = next(hit for hit in hits if hit.record.id == "act_SKIP")
    assert skip_hit.destination_score == 0.0
    assert skip_hit.matched_targets == ()


def test_an_explicit_operation_filter_still_reaches_only_that_operation(
    actions_dir: Path,
) -> None:
    """Firing control for the test above: skips are included because nothing
    filters them, not because the operation filter is broken."""
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_MOVE", ts="2026-08-01T00:00:00Z", body="bench press", operation="move"
            ),
            make_record(
                id="act_SKIP", ts="2026-08-02T00:00:00Z", body="bench press", operation="skip"
            ),
        ],
    )

    assert [h.record.id for h in recorder.query_similar("bench press", operation="skip")] == [
        "act_SKIP"
    ]
    assert [h.record.id for h in recorder.query_similar("bench press", operation="move")] == [
        "act_MOVE"
    ]


# --- multi-target records rank by ANY target ------------------------------


def test_a_multi_target_record_ranks_by_its_best_target(actions_dir: Path) -> None:
    """Spec 12 §2 makes multi-destination first-class ("ONE ENTRY PER FILE
    TOUCHED"); a mean over targets would punish exactly the multi-file habit
    doc 12 exists to capture."""
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_MULTI",
                ts="2026-08-01T00:00:00Z",
                body="zzz",
                targets=(
                    dest("projects/inbox.md"),
                    dest("areas/journal.md"),
                    dest("areas/cap-1.md"),
                ),
            ),
            # Firing control: the SAME record without the matching target.
            make_record(
                id="act_ONLY_MISSES",
                ts="2026-08-02T00:00:00Z",
                body="zzz",
                targets=(dest("projects/inbox.md"), dest("areas/journal.md")),
            ),
        ],
    )

    hits = recorder.query_similar("cap-1", limit=None)

    assert [hit.record.id for hit in hits] == ["act_MULTI"]
    assert hits[0].destination_score == 1.0
    assert hits[0].matched_targets == ("areas/cap-1.md",)
    assert hits[0].score == 0.5


def test_every_tying_target_is_named_in_matched_targets(actions_dir: Path) -> None:
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_TIE",
                ts="2026-08-01T00:00:00Z",
                body="zzz",
                targets=(dest("areas/cap-1.md"), dest("resources/cap-1.md")),
            )
        ],
    )

    hits = recorder.query_similar("cap-1", limit=None)

    assert hits[0].matched_targets == ("areas/cap-1.md", "resources/cap-1.md")


def test_a_destination_description_is_matchable_vocabulary(actions_dir: Path) -> None:
    """Spec 12 §2 stores ``targets[].description`` — "the NL description of
    this destination, if any" — precisely so the corpus can be read back by
    what the destination is FOR (spec 11 §3)."""
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_DESCRIBED",
                ts="2026-08-01T00:00:00Z",
                body="zzz",
                targets=(dest("areas/f.md", description="barbell strength programming"),),
            ),
            make_record(
                id="act_BARE",
                ts="2026-08-02T00:00:00Z",
                body="zzz",
                targets=(dest("areas/f.md"),),
            ),
        ],
    )

    hits = recorder.query_similar("barbell programming", limit=None)

    assert [hit.record.id for hit in hits] == ["act_DESCRIBED"]


def test_the_md_extension_is_not_a_matchable_token(actions_dir: Path) -> None:
    """Every target ends in ``.md``; keeping it would give a query containing
    the word "md" a hit on the entire corpus."""
    recorder = store(
        actions_dir,
        [make_record(id="act_X", ts="2026-08-01T00:00:00Z", body="zzz", targets=(dest("a/b.md"),))],
    )

    assert recorder.query_similar("md", limit=None) == []


# --- tags: None vs [], scalar coercion, after-edit, auto_tags -------------


def test_tags_none_falls_back_to_the_query_text_tokens(actions_dir: Path) -> None:
    """The single-argument CLI form (``--similar-to "bench press"``) must
    still reach a record tagged ``bench-press``."""
    recorder = store(
        actions_dir,
        [make_record(id="act_TAGGED", ts="2026-08-01T00:00:00Z", body="zzz", tags=["bench-press"])],
    )

    hits = recorder.query_similar("bench press", tags=None, limit=None)

    assert [(hit.record.id, hit.tag_score, hit.score) for hit in hits] == [("act_TAGGED", 1.0, 2.0)]


def test_an_explicitly_empty_tag_list_zeroes_the_tag_component(actions_dir: Path) -> None:
    """``tags=[]`` means "this capture genuinely has no tags" — a DIFFERENT
    statement from ``tags=None`` ("I have no tag information"), and the pair
    above is its firing control."""
    recorder = store(
        actions_dir,
        [make_record(id="act_TAGGED", ts="2026-08-01T00:00:00Z", body="zzz", tags=["bench-press"])],
    )

    assert recorder.query_similar("bench press", tags=[], limit=None) == []


def test_a_scalar_tags_field_is_coerced_not_re_parsed(actions_dir: Path) -> None:
    """Matt's vault contains scalar ``tags:`` values; the Phase-3 ruling
    (ARCHITECTURE.md §"Phase-3 rulings", @35cf7c0) requires the shared
    frontmatter list coercion — "never a hand-rolled re-parse (08 §B9
    class)"."""
    recorder = store(
        actions_dir,
        [
            make_record(id="act_SCALAR", ts="2026-08-01T00:00:00Z", body="zzz", tags="bench-press"),
            make_record(
                id="act_LIST", ts="2026-08-02T00:00:00Z", body="zzz", tags=["bench-press"]
            ),
        ],
    )

    hits = recorder.query_similar("bench press", limit=None)

    assert {hit.record.id for hit in hits} == {"act_SCALAR", "act_LIST"}
    assert {hit.tag_score for hit in hits} == {1.0}
    # A hand-rolled re-parse of the string would tokenize "bench-press" the
    # same way but ALSO accept e.g. a dict; the coercion contract is what is
    # pinned, so the two records must be indistinguishable.
    assert len({hit.score for hit in hits}) == 1


def test_tags_added_by_the_action_itself_are_matchable(actions_dir: Path) -> None:
    """``frontmatter_after`` is spec 12 §2's "after any meta/tag edits" — for
    a ``tag_edit`` it is the decision Matt actually made, so it is the more
    valuable half of the record, not the less."""
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_EDIT",
                ts="2026-08-01T00:00:00Z",
                body="zzz",
                tags=["inbox"],
                tags_after=["inbox", "bench-press"],
                operation="tag_edit",
            )
        ],
    )

    hits = recorder.query_similar("bench press", limit=None)

    assert [(hit.record.id, hit.tag_score) for hit in hits] == [("act_EDIT", 1.0)]


def test_machine_tags_recorded_in_the_context_are_matchable(actions_dir: Path) -> None:
    """Spec 12 §2 records ``context.auto_tags_present`` on every action;
    spec 11 §2 writes machine tags to ``tags`` as well, so this is usually a
    no-op union — it earns its keep on records whose ``frontmatter_before``
    predates the tagger."""
    recorder = store(
        actions_dir,
        [
            make_record(
                id="act_AUTO",
                ts="2026-08-01T00:00:00Z",
                body="zzz",
                auto_tags=("bench-press",),
            )
        ],
    )

    hits = recorder.query_similar("bench press", limit=None)

    assert [(hit.record.id, hit.tag_score, hit.score) for hit in hits] == [("act_AUTO", 1.0, 2.0)]


# --- empty corpus and empty query -----------------------------------------


def test_an_empty_corpus_returns_an_empty_list(actions_dir: Path) -> None:
    recorder = ActionRecorder(actions_dir)

    assert recorder.query_similar("bench press") == []
    assert recorder.query_similar("bench press", limit=None) == []


def test_a_corpus_whose_only_records_are_dry_runs_returns_nothing(actions_dir: Path) -> None:
    recorder = store(
        actions_dir,
        [make_record(id="act_DRY", ts="2026-08-01T00:00:00Z", body="bench press", dry_run=True)],
    )

    assert recorder.query_similar("bench press", limit=None) == []
    assert len(recorder.query_similar("bench press", limit=None, include_dry_run=True)) == 1


@pytest.mark.parametrize("text", ["", "   ", "\n\t", "---", "!!! ??? ..."])
def test_a_query_with_no_tokens_returns_nothing_rather_than_everything(
    actions_dir: Path, text: str
) -> None:
    """Every record would tie at score 0, and calling that a ranking is the
    silently-wrong-answer class (09 §1.5)."""
    recorder = store(actions_dir, golden_records())

    assert recorder.query_similar(text, limit=None) == []
    # Firing control: the same corpus does answer a real query.
    assert len(recorder.query_similar(GOLDEN_QUERY, limit=None)) == 6


def test_a_tokenless_query_short_circuits_before_reading_the_corpus(
    actions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty-result behaviour is enforced by the zero-score drop, so a
    test on the return value alone cannot tell whether the fast path exists.
    Pin the fast path where it is observable: the corpus is never streamed.
    """
    recorder = store(actions_dir, golden_records())
    calls: list[int] = []
    real_query = recorder.query

    def counting_query(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real_query(*args, **kwargs)

    monkeypatch.setattr(recorder, "query", counting_query)

    assert recorder.query_similar("!!!", limit=None) == []
    assert calls == []
    # Firing control: a real query DOES stream the corpus, so an empty `calls`
    # cannot pass by the counter never being wired.
    assert len(recorder.query_similar(GOLDEN_QUERY, limit=None)) == 6
    assert calls == [1]


def test_a_limit_below_one_raises_loudly_instead_of_returning_nothing(
    actions_dir: Path,
) -> None:
    recorder = store(actions_dir, golden_records())

    for bad in (0, -1):
        with pytest.raises(ActionSchemaError) as excinfo:
            recorder.query_similar(GOLDEN_QUERY, limit=bad)
        assert str(bad) in str(excinfo.value)
        assert excinfo.value.hint


def test_a_missing_actions_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    recorder = ActionRecorder(tmp_path / "nope" / "actions")

    assert recorder.query_similar("anything", limit=None) == []


# --- must-not-be-connected invariants (standard 3) ------------------------


def test_query_similar_never_writes_to_the_corpus(actions_dir: Path) -> None:
    """Pin the CONNECTION: retrieval is a READER. A reader that creates its
    directory or rewrites a month file breaks doc 12 §2's append-only,
    never-rewritten law from the side nobody audits."""
    recorder = store(actions_dir, golden_records())
    month_file = actions_dir / "2026-08.jsonl"
    before = month_file.read_bytes()
    listing_before = sorted(p.name for p in actions_dir.iterdir())

    recorder.query_similar(GOLDEN_QUERY, limit=None)
    recorder.query_similar(GOLDEN_QUERY, limit=None, include_dry_run=True)

    assert month_file.read_bytes() == before
    assert sorted(p.name for p in actions_dir.iterdir()) == listing_before


def test_query_similar_on_a_missing_directory_does_not_create_it(tmp_path: Path) -> None:
    actions_dir = tmp_path / "state" / "actions"
    recorder = ActionRecorder(actions_dir)

    recorder.query_similar("bench press")

    assert not actions_dir.exists()


def test_actions_imports_only_errors_and_frontmatter_from_the_package() -> None:
    """The import-graph connection pin.

    ARCHITECTURE.md §"Module ownership" (@35cf7c0) lists this module's
    dependencies as "— (pure + file append)", and ``fileops`` imports it on
    every mutating operation. Retrieval added ``frontmatter`` — dependency-free,
    so acyclic, exactly as the recorded ruling allows for ``suggest``/``routes``
    ("both compare tags and must go through the ONE shared ``normalize_tag``
    (09 §2). ``frontmatter`` is dependency-free, so the edge is acyclic.").

    Importing ``suggest`` for its ``string_similarity`` would instead drag
    ``index`` + ``config`` + ``learn`` in behind it. This test is what makes
    that a red build rather than a quiet architecture change.
    """
    source = Path(__file__).resolve().parents[1] / "src" / "organize_core" / "actions.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("organize_core"):
            module = node.module or ""
            if module == "organize_core":
                imported.update(alias.name for alias in node.names)
            else:
                imported.add(module.split(".", 1)[1].split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("organize_core."):
                    imported.add(alias.name.split(".", 1)[1].split(".")[0])

    assert imported == {"errors", "frontmatter"}


# --- tokenizer unit pins ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cap-1", ["1", "cap"]),
        ("cap-10", ["10", "cap"]),
        ("Deep_Work", ["deep", "work"]),
        ("Bench Press!", ["bench", "press"]),
        ("areas/fitness/strength", ["areas", "fitness", "strength"]),
        ("", []),
        ("--- !!! ...", []),
        ("repeat repeat repeat", ["repeat"]),
        ("café", ["café"]),
    ],
)
def test_tokenize_literals(text: str, expected: list[str]) -> None:
    assert sorted(tokenize(text)) == expected


# --- performance sanity ----------------------------------------------------


def _write_corpus(actions_dir: Path, count: int) -> None:
    """Write ``count`` records straight into the month file.

    Deliberately NOT via :meth:`ActionRecorder.record`: that path ``fsync``s
    every line by design (spec 12 §2 durability note), which would make corpus
    GENERATION dominate a test about QUERY latency. The bytes are produced by
    the real ``ActionRecord.to_json`` serializer, so the file this reads back
    is the file the writer would have produced.
    """
    actions_dir.mkdir(parents=True, exist_ok=True)
    words = "bench press squat deadlift row curl plank lunge sprint rest".split()
    lines: list[str] = []
    for i in range(count):
        body = " ".join(words[(i + j) % len(words)] for j in range(40))
        record = make_record(
            id=f"act_{i:07d}",
            ts=f"2026-08-{(i % 28) + 1:02d}T12:00:00Z",
            body=f"{body} note number {i}",
            tags=[f"tag-{i % 97}", "fitness"],
            targets=(dest(f"areas/folder-{i % 53}/note.md", description=f"folder {i % 53} notes"),),
        )
        lines.append(json.dumps(record.to_json(), ensure_ascii=False, separators=(",", ":")))
    (actions_dir / "2026-08.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_ten_thousand_record_corpus_answers_in_under_a_second(actions_dir: Path) -> None:
    _write_corpus(actions_dir, 10_000)
    recorder = ActionRecorder(actions_dir)

    started = time.perf_counter()
    hits = recorder.query_similar("bench press squat deadlift", limit=10)
    elapsed = time.perf_counter() - started

    assert len(hits) == 10
    assert all(hit.score > 0.0 for hit in hits)
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)
    assert elapsed < 1.0, f"query_similar over 10k records took {elapsed:.3f}s (budget 1.0s)"


def test_the_ten_thousand_record_corpus_is_really_ten_thousand(actions_dir: Path) -> None:
    """Anti-vacuity for the budget above: a corpus that failed to generate
    would also answer in well under a second."""
    _write_corpus(actions_dir, 10_000)
    recorder = ActionRecorder(actions_dir)

    assert sum(1 for _ in recorder.query()) == 10_000
    assert len(recorder.query_similar("bench press squat deadlift", limit=None)) == 10_000


# --- returned shape --------------------------------------------------------


def test_a_hit_carries_the_whole_record_and_readable_reasons(actions_dir: Path) -> None:
    recorder = store(actions_dir, golden_records())

    top = recorder.query_similar(GOLDEN_QUERY, limit=1)[0]

    assert isinstance(top, SimilarAction)
    assert isinstance(top.record, ActionRecord)
    assert top.record.id == "act_R3"
    assert top.record.capture.body_before == "Bench press strength training"
    assert top.reasons == (
        "text: bench, press, strength, training",
        "tags: strength, training",
        f"destination '{FITNESS_DEST}': strength, training",
    )


def test_reasons_summarize_beyond_five_shared_tokens(actions_dir: Path) -> None:
    recorder = store(
        actions_dir,
        [make_record(id="act_LONG", ts="2026-08-01T00:00:00Z", body="a b c d e f g")],
    )

    top = recorder.query_similar("a b c d e f g", limit=1)[0]

    assert top.reasons == ("text: a, b, c, d, e (+2 more)",)


def test_a_hit_is_frozen_so_a_caller_cannot_rewrite_a_ranking(actions_dir: Path) -> None:
    recorder = store(actions_dir, golden_records())
    top = recorder.query_similar(GOLDEN_QUERY, limit=1)[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        top.score = 99.0  # type: ignore[misc]
