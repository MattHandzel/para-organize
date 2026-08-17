"""``learn`` consumer suite (spec 06 §3.2; regression obligations 08 §B8,
plus the B1/B2/B10/B11 classes that touch this consumer).

Everything external is faked and local: Ollama and Whisper are
``http.server`` instances on 127.0.0.1, ``yt-dlp`` is a tmp script. No real
vault, no real state, no real network, no real binaries.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from organize_core import frontmatter
from organize_core.consumers import learn as learn_mod
from organize_core.consumers.base import Status, get_consumer_types
from organize_core.consumers.learn import LearnConsumer
from organize_core.errors import ConfigError
from test_consumer_learn_fakes import (
    FakeLLM,
    cards_response,
    closed_port_url,
    consumer_config,
    fake_yt_dlp,
    http_endpoint,
    ollama_client,
    payload_for,
    run_context,
    write_note,
)

LONG_BODY = (
    "Spaced repetition schedules expand because forgetting is exponential. "
    "In 2019 researchers measured a 42% retention gain across 1200 learners "
    "(see https://example.org/study). The expanding interval keeps each review "
    "near the edge of recall, which is where the memory trace is strengthened "
    "most per unit of study time. Massed practice feels easier and performs worse.\n"
)


def freeze(monkeypatch: pytest.MonkeyPatch, module, stamp: str = "2026-08-16") -> None:
    when = datetime.fromisoformat(stamp).replace(tzinfo=UTC)
    monkeypatch.setattr(module, "_utc_now", lambda: when)


def make_learn(**options) -> LearnConsumer:
    return LearnConsumer(consumer_config("learn", "learn", **options))


def learn_note(vault: Path, rel: str = "resources/performing/spacing.md", **fm) -> Path:
    fields = {"tags": ["learn"], "aliases": ["Spacing effect"], "sources": ["Ada Lovelace"]}
    fields.update(fm)
    text = frontmatter.serialize(
        frontmatter.Document(
            frontmatter=frontmatter.Frontmatter(fields=fields), body="\n" + LONG_BODY
        )
    )
    return write_note(vault, rel, text)


CARDS = (
    {"tier": "thesis", "front": "Why do intervals expand?", "back": "Forgetting is exponential."},
    {
        "tier": "tier2_conceptual",
        "front": "Why does massed practice feel better than it works?",
        "back": "[WRITE YOUR ANSWER FIRST]",
        "model_answer": "Fluency during massed practice is mistaken for durable learning.",
        "tags": ["memory"],
    },
    {
        "tier": "synthesis_drawing",
        "front": "Draw a diagram showing the forgetting curve against review points.",
        "back": "[DRAW THIS]",
        "model_answer": "Decaying curve with review spikes resetting it higher each time.",
    },
)


# --- construction (B2: constructors are pure) -------------------------------


def test_constructor_is_pure_and_does_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """08 §B2: eager consumer construction with I/O in ``__init__`` is what
    took the whole pipeline down. Nothing may touch disk or a subprocess."""

    def explode(*args, **kwargs):
        raise AssertionError("constructor performed I/O")

    monkeypatch.setattr(Path, "read_text", explode)
    monkeypatch.setattr(Path, "exists", explode)
    monkeypatch.setattr(Path, "glob", explode)
    monkeypatch.setattr("subprocess.run", explode)
    monkeypatch.setattr("urllib.request.build_opener", explode)

    consumer = make_learn(min_content_length=10, whisper_host="http://example.invalid")
    assert consumer.min_content_length == 10


def test_registered_under_its_contract_name() -> None:
    assert get_consumer_types()["learn"] is LearnConsumer


def test_uses_llm_is_true_so_the_runner_denies_no_ai_notes() -> None:
    assert LearnConsumer.uses_llm is True


def test_unknown_option_fails_naming_the_key() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_learn(ollama_host="http://nope")
    assert "ollama_host" in str(excinfo.value)
    assert "consumers.learn" in str(excinfo.value)


def test_live_config_option_names_are_accepted() -> None:
    """The shipped example config (06 §2 defaults-of-record) must construct."""
    consumer = make_learn(
        flashcard_dir="resources/flashcards",
        review_dir="resources/flashcards/review",
        deck="Reading::Articles",
        card_tags=["learn-consumer"],
        min_content_length=200,
        max_cards_per_note=12,
        whisper_host="http://whisper.invalid:47770",
    )
    assert consumer.deck == "Reading::Articles"
    assert consumer.guidelines_path == "resources/flashcards/card-generation-guidelines.md"
    assert consumer.generation_log_path == "resources/flashcards/generation-log.md"


def test_triage_weights_validate_their_key_names() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_learn(triage_weights={"novelty": 0.5, "vibes": 0.5})
    assert "vibes" in str(excinfo.value)


# --- trigger ----------------------------------------------------------------


@pytest.mark.parametrize("tag", ["learn", "Remember", "STUDY", "anki"])
def test_opt_in_tags_trigger_case_insensitively(fixture_vault: Path, tag: str) -> None:
    note = learn_note(fixture_vault, tags=[tag])
    assert make_learn().should_process(payload_for(note)) is True


def test_short_notes_are_below_the_content_floor(fixture_vault: Path) -> None:
    note = write_note(fixture_vault, "resources/performing/tiny.md", "---\ntags:\n- learn\n---\nhi\n")
    assert make_learn().should_process(payload_for(note)) is False


def test_non_markdown_is_ignored(fixture_vault: Path) -> None:
    payload = payload_for(fixture_vault / "capture/raw_capture/clipboard-dump.txt")
    assert make_learn().should_process(payload) is False


def test_own_output_tree_is_refused_even_if_config_would_admit_it(fixture_vault: Path) -> None:
    """Self-defence: the consumer must never eat the flashcards it wrote."""
    note = learn_note(fixture_vault, rel="resources/flashcards/review/2026-08-16-spacing.md")
    assert make_learn().should_process(payload_for(note)) is False


def test_untagged_note_below_threshold_is_not_processed(fixture_vault: Path) -> None:
    note = write_note(
        fixture_vault,
        "resources/performing/diary.md",
        "---\ntags:\n- journal\nsources:\n- me\n---\n" + "I felt fine today. " * 20,
    )
    assert make_learn().should_process(payload_for(note)) is False


def test_untagged_note_above_threshold_is_processed(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault, tags=["research", "learning"])
    assert make_learn().should_process(payload_for(note)) is True


def test_triage_score_is_the_specced_weighted_sum(fixture_vault: Path) -> None:
    """06 §3.2: 0.4·novelty + 0.4·relevance + 0.2·quality."""
    note = learn_note(fixture_vault, tags=["research", "learning"])
    consumer = make_learn()
    payload = payload_for(note)
    # quality: >=200 chars (0.2) + numbers (0.3) + citation/URL (0.3) = 0.8
    # relevance: two topic tags (0.6) + non-self source (0.3) = 0.9
    # novelty: 0.5 (body is under 1000 chars)
    assert consumer.triage_score(payload) == pytest.approx(0.5 * 0.4 + 0.9 * 0.4 + 0.8 * 0.2)


def test_triage_weights_are_config_exposed(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault, tags=["research", "learning"])
    payload = payload_for(note)
    quality_only = make_learn(triage_weights={"novelty": 0.0, "relevance": 0.0, "quality": 1.0})
    assert quality_only.triage_score(payload) == pytest.approx(0.8)


def test_topic_tag_set_is_config_exposed(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault, tags=["impro"], sources=["me"])
    payload = payload_for(note)
    assert make_learn().triage_score(payload) < 0.7
    tuned = make_learn(topic_tags=["impro"])
    assert tuned.triage_score(payload) == pytest.approx(0.5 * 0.4 + 0.3 * 0.4 + 0.8 * 0.2)


def test_triage_scores_are_logged_for_tuning(
    fixture_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    note = learn_note(fixture_vault, tags=["journal"], sources=["me"])
    with caplog.at_level(logging.DEBUG, logger=learn_mod.LOG.name):
        make_learn().should_process(payload_for(note))
    assert any("learn triage" in record.getMessage() for record in caplog.records)


# --- modality ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "body", "expected"),
    [
        ({"modalities": ["audio"]}, "https://youtu.be/abcdefghijk notes", "youtube"),
        ({"modalities": ["audio"]}, "a talk I recorded", "audio"),
        ({"modalities": "system-audio"}, "a talk I recorded", "audio"),
        ({"modalities": ["screenshot"]}, "text from an image", "screenshot"),
        ({"modalities": ["clipboard"]}, "x" * 600, "article"),
        ({"modalities": ["clipboard"]}, "> a quoted line", "quote"),
        ({}, '"quoted"', "quote"),
        ({}, "short thought", "thought"),
        ({}, "y" * 1200, "article"),
        ({}, "z" * 500, "note"),
    ],
)
def test_modality_detection_order(fixture_vault: Path, fields, body, expected) -> None:
    text = frontmatter.serialize(
        frontmatter.Document(
            frontmatter=frontmatter.Frontmatter(fields=dict(fields)), body="\n" + body
        )
    )
    note = write_note(fixture_vault, "resources/performing/modality.md", text)
    assert make_learn().detect_modality(payload_for(note)) == expected


# --- prompts ----------------------------------------------------------------


def test_scalar_alias_yields_a_whole_title_not_one_character(fixture_vault: Path) -> None:
    """08 §B10: ``aliases[0]`` on a scalar string returned 'S'."""
    note = learn_note(fixture_vault, aliases="Spacing effect")
    _, user = make_learn().build_prompts(payload_for(note), "article", LONG_BODY)
    assert 'Source: "Spacing effect"' in user


def test_title_falls_back_to_title_field_then_stem(fixture_vault: Path) -> None:
    titled = learn_note(fixture_vault, rel="resources/performing/a.md", aliases=[], title="From title")
    _, user = make_learn().build_prompts(payload_for(titled), "note", LONG_BODY)
    assert 'Source: "From title"' in user

    bare = learn_note(fixture_vault, rel="resources/performing/bare-note.md", aliases=[])
    _, user = make_learn().build_prompts(payload_for(bare), "note", LONG_BODY)
    assert 'Source: "bare-note"' in user


def test_user_prompt_carries_the_card_budget_and_truncates_content(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault)
    consumer = make_learn(max_cards_per_note=7, max_prompt_chars=20)
    _, user = consumer.build_prompts(payload_for(note), "article", "Q" * 500)
    assert "Generate 3-7 cards. Always include exactly 1 thesis card." in user
    assert "Q" * 20 in user
    assert "Q" * 21 not in user


def test_system_prompt_carries_the_pedagogy_and_curiosity_rules(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault)
    system, _ = make_learn().build_prompts(payload_for(note), "article", LONG_BODY)
    for tier in (
        "thesis",
        "tier1_factual",
        "tier2_conceptual",
        "synthesis_connection",
        "synthesis_feynman",
        "synthesis_devils_advocate",
        "synthesis_drawing",
    ):
        assert tier in system
    assert "CURIOSITY FRAMING" in system
    assert "10. Skip vague, motivational, or overly personal content" in system


def test_learned_rules_are_appended_when_the_guidelines_file_exists(fixture_vault: Path) -> None:
    write_note(
        fixture_vault,
        "resources/flashcards/card-generation-guidelines.md",
        "# Guidelines\n\n## Learned Rules\n- Never ask about dates.\n\n## Other\nignored\n",
    )
    note = learn_note(fixture_vault)
    ctx = run_context(fixture_vault)
    system, _ = make_learn().build_prompts(payload_for(note), "article", LONG_BODY, ctx)
    assert "ADDITIONAL RULES FROM PAST FEEDBACK:" in system
    assert "- Never ask about dates." in system
    assert "ignored" not in system


def test_missing_guidelines_file_is_not_an_error(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault)
    system, _ = make_learn().build_prompts(payload_for(note), "article", LONG_BODY, run_context(fixture_vault))
    assert "ADDITIONAL RULES" not in system


# --- golden run -------------------------------------------------------------


def test_golden_run_writes_the_specced_review_file(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, learn_mod)
    note = learn_note(fixture_vault)
    with http_endpoint() as ollama:
        ollama.queue_completion(cards_response(*CARDS))
        ctx = run_context(fixture_vault, ollama_client(ollama.base_url))
        result = make_learn().handle(payload_for(note), ctx)

    assert result.status is Status.SUCCESS
    assert result.metadata["cards_generated"] == 3
    assert result.metadata["modality"] == "note"

    review = fixture_vault / "resources/flashcards/review/2026-08-16-spacing-effect.md"
    assert review.exists()
    text = review.read_text(encoding="utf-8")
    assert text == (
        "---\n"
        "article: Spacing effect\n"
        "author: Ada Lovelace\n"
        "source_note: resources/performing/spacing.md\n"
        f"source_hash: {learn_mod._content_hash(payload_for(note))}\n"
        "modality: note\n"
        "generated: '2026-08-16'\n"
        "status: review\n"
        "deck: Reading::Articles\n"
        "cards_generated: 3\n"
        "---\n"
        "\n"
        "# Cards: Spacing effect\n"
        "\n"
        "Review these cards. Delete bad ones. Edit as needed.\n"
        "For tier2 cards: write your answer BEFORE looking at the model answer.\n"
        "When done, change `status: review` to `status: approved`.\n"
        "\n"
        "## Card 1 [thesis]\n"
        "\n"
        "**Q:** Why do intervals expand?\n"
        "\n"
        "**A:** Forgetting is exponential.\n"
        "\n"
        "Tags: learn-consumer, modality::note\n"
        "\n"
        "## Card 2 [tier2_conceptual]\n"
        "\n"
        "**Q:** Why does massed practice feel better than it works?\n"
        "\n"
        "**A:** *[Write your answer here before reading below]*\n"
        "\n"
        "<details><summary>Model answer (click after writing yours)</summary>\n"
        "\n"
        "Fluency during massed practice is mistaken for durable learning.\n"
        "\n"
        "</details>\n"
        "\n"
        "Tags: memory, learn-consumer, modality::note\n"
        "\n"
        "## Card 3 [synthesis_drawing]\n"
        "\n"
        "**Q:** Draw a diagram showing the forgetting curve against review points.\n"
        "\n"
        "**A:** *[DRAW THIS — sketch the diagram on paper or tablet]*\n"
        "\n"
        "<details><summary>What the diagram should show (click after drawing)</summary>\n"
        "\n"
        "Decaying curve with review spikes resetting it higher each time.\n"
        "\n"
        "</details>\n"
        "\n"
        "Tags: learn-consumer, modality::note\n"
    )


def test_golden_run_uses_the_specced_llm_call_parameters(fixture_vault: Path) -> None:
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    note = learn_note(fixture_vault)
    make_learn().handle(payload_for(note), run_context(fixture_vault, llm))
    call = llm.calls[0]
    assert call["json_mode"] is True
    assert call["temperature"] == 0.3
    assert call["max_tokens"] == 2048
    assert call["timeout_seconds"] == 60.0


def test_generated_review_file_round_trips(fixture_vault: Path) -> None:
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    note = learn_note(fixture_vault)
    result = make_learn().handle(payload_for(note), run_context(fixture_vault, llm))
    text = Path(result.metadata["review_file"]).read_text(encoding="utf-8")
    assert frontmatter.serialize(frontmatter.parse(text)) == text


def test_source_gains_learn_processed_and_keeps_every_other_field(fixture_vault: Path) -> None:
    """06 §3.2 / 08 §B8 + the 03 §8 no-field-loss law."""
    note = write_note(
        fixture_vault,
        "resources/performing/rich.md",
        "---\n"
        "id: rich-note\n"
        "tags:\n"
        "- learn\n"
        "obsidian-custom: keep me\n"
        "location:\n"
        "  city: New York\n"
        "processing_status: organized\n"
        "created_date: '2026-01-01'\n"
        "---\n" + LONG_BODY,
    )
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    make_learn().handle(payload_for(note), run_context(fixture_vault, llm))

    after = frontmatter.parse(note.read_text(encoding="utf-8"))
    assert after.frontmatter is not None
    assert after.frontmatter.fields["processing_status"] == "learn-processed"
    assert after.frontmatter.fields["obsidian-custom"] == "keep me"
    assert after.frontmatter.fields["location"] == {"city": "New York"}
    assert after.frontmatter.fields["id"] == "rich-note"
    assert after.body == LONG_BODY


def test_write_back_creates_a_block_when_the_note_has_none(fixture_vault: Path) -> None:
    note = write_note(fixture_vault, "resources/performing/plain.md", LONG_BODY)
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    make_learn(min_content_length=10).handle(payload_for(note), run_context(fixture_vault, llm))
    doc = frontmatter.parse(note.read_text(encoding="utf-8"))
    assert doc.frontmatter is not None
    assert doc.frontmatter.fields["processing_status"] == "learn-processed"
    assert doc.body == LONG_BODY


# ---------------------------------------------------------------------------
# the write-back is a RECORDED meta_edit (spec 12 §2 / PHASE-5 checklist a)
# ---------------------------------------------------------------------------


def test_the_write_back_is_a_recorded_meta_edit_not_a_bare_atomic_write(
    fixture_vault: Path,
) -> None:
    """ARCHITECTURE, "Phase-4 rulings, auto_tagger batch" (f24ee2e), PHASE-5
    CHECKLIST item (a), verbatim: "learn consumer's SOURCE-note write-back
    (processing_status: learn-processed) is a meta_edit in the doc-12 enum
    and must migrate to update_frontmatter + op_context (new-output files —
    flashcards/answers — stay store-audited, defensible as-is)".

    So the corpus must show ONE ``meta_edit`` for the source note, actored to
    the consumer, and the operations log must have a line. Every value is a
    LITERAL (anti-vacuity standard 2): comparing against
    ``actions.OPERATIONS`` or an imported ACTOR constant would agree with a
    flipped constant.
    """
    import json

    from test_consumer_learn_fakes import core_paths_for

    note = learn_note(fixture_vault)
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    paths = core_paths_for(fixture_vault)

    assert (
        make_learn().handle(payload_for(note), run_context(fixture_vault, llm)).status
        is Status.SUCCESS
    )

    files = sorted(Path(paths.actions_dir).glob("*.jsonl"))
    records = [
        json.loads(line)
        for file in files
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta_edits = [r for r in records if r["operation"] == "meta_edit"]
    assert len(meta_edits) == 1, records
    record = meta_edits[0]
    assert record["actor"] == "consumer:learn"
    assert record["capture"]["path"] == str(note)
    assert record["capture"]["frontmatter_after"]["processing_status"] == "learn-processed"
    assert [t["role"] for t in record["targets"]] == ["destination"]

    oplog = Path(paths.operations_log)
    assert oplog.is_file() and oplog.read_text(encoding="utf-8").strip()


def test_the_write_back_teaches_the_learner_NOTHING(fixture_vault: Path) -> None:
    """TRAP TEST. A ``meta_edit`` files nothing, and its actor is a consumer:
    two independent reasons ``learn.record_action`` must return ``None``.
    Both are asserted, and the FIRING CONTROL proves the callback was live.
    """
    import dataclasses
    import json

    from organize_core.actions import ActionRecord
    from organize_core.cli import _learn_from_action
    from organize_core.learn import LearningData, save_learning
    from test_consumer_learn_fakes import core_paths_for, make_config, op_context_for

    paths = core_paths_for(fixture_vault)
    paths.ensure_state_dirs()
    learning = Path(paths.learning_path)
    save_learning(learning, LearningData())
    before = learning.read_bytes()

    config = make_config(fixture_vault)
    op_context = dataclasses.replace(
        op_context_for(fixture_vault),
        on_record=lambda record: _learn_from_action(paths, config, record),
    )
    assert op_context.on_record is not None, "the pin is vacuous without the callback"

    note = learn_note(fixture_vault)
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    result = make_learn().handle(
        payload_for(note), run_context(fixture_vault, llm, op_context=op_context)
    )
    assert result.status is Status.SUCCESS, result.message
    assert learning.read_bytes() == before, "a meta_edit is not a filing decision"

    # firing control: the SAME wired callback, fed a Matt-actored MOVE, writes
    files = sorted(Path(paths.actions_dir).glob("*.jsonl"))
    raw = [
        json.loads(line)
        for file in files
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert raw, "nothing was recorded — the assertion above was vacuous"
    record = ActionRecord.from_json(raw[0])
    op_context.on_record(
        dataclasses.replace(record, actor="matt", operation="move")  # type: ignore[arg-type]
    )
    assert learning.read_bytes() != before, "the control did not fire — trap is vacuous"


def test_without_an_op_context_the_consumer_refuses_rather_than_writing(
    fixture_vault: Path,
) -> None:
    """ARCHITECTURE, "Phase-4 rulings, auto_tagger batch" (f24ee2e), verbatim:
    "a consumer that would write the vault with op_context=None emits
    Status.ERROR rather than performing an unrecorded write (refusing to
    become a second unrecorded write path is the doc-12 discipline; errors
    retry, so the run self-heals once the seam lands)".

    Refused BEFORE the LLM call, so a wiring fault does not burn a
    generation: the fake client's call log must be empty. Firing control: the
    identical note through a context that HAS one succeeds and calls the LLM.
    """
    note = learn_note(fixture_vault)
    before = note.read_bytes()
    llm = FakeLLM(responses=[cards_response(*CARDS)])

    result = make_learn().handle(
        payload_for(note), run_context(fixture_vault, llm, op_context=None)
    )

    assert result.status is Status.ERROR
    assert "OperationContext" in result.message
    assert note.read_bytes() == before
    assert llm.calls == [], "the refusal must precede the expensive half"
    review_dir = fixture_vault / "resources/flashcards/review"
    assert not list(review_dir.glob("*.md")) if review_dir.is_dir() else True

    control = make_learn().handle(payload_for(note), run_context(fixture_vault, llm))
    assert control.status is Status.SUCCESS, "the control did not fire — pin is vacuous"
    assert llm.calls, "the control did not reach the LLM"


def test_the_write_back_backs_the_note_up_and_updates_the_index(
    fixture_vault: Path,
) -> None:
    """Two more things the bare ``atomic_write`` silently skipped: doc 05
    §1.4's backup, and the index entry the composition root later flushes."""
    from test_consumer_learn_fakes import op_context_for

    note = learn_note(fixture_vault)
    op_context = op_context_for(fixture_vault)
    llm = FakeLLM(responses=[cards_response(*CARDS)])

    assert (
        make_learn()
        .handle(payload_for(note), run_context(fixture_vault, llm, op_context=op_context))
        .status
        is Status.SUCCESS
    )

    backups = list(Path(op_context.backup_dir).rglob("*.md"))
    assert backups, f"no backup under {op_context.backup_dir} (05 §1.4)"
    record = op_context.index.get(note)
    assert record is not None, "the composition root's index never saw the edit"


def test_generation_log_is_created_from_a_template_when_missing(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06 §3.2: today the row is silently dropped unless the file exists."""
    freeze(monkeypatch, learn_mod)
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    note = learn_note(fixture_vault)
    make_learn().handle(payload_for(note), run_context(fixture_vault, llm))

    log = fixture_vault / "resources/flashcards/generation-log.md"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert "| Date | Article | Cards | Kept | Edited | Deleted | Tier breakdown | Modality | Source |" in lines
    row = next(line for line in lines if line.startswith("| 2026-08-16 |"))
    assert "| Spacing effect |" in row
    assert "| 3 |" in row
    assert "thesis:1" in row
    assert "| note |" in row


def test_generation_log_row_lands_under_the_header_of_an_existing_log(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, learn_mod)
    log = write_note(
        fixture_vault,
        "resources/flashcards/generation-log.md",
        "# Log\n\n| Date | Article | Cards | Kept | Edited | Deleted | Tier breakdown | Modality | Source |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
        "| 2026-01-01 | older | 1 | — | — | — | thesis:1 | note | auto-consumer |\n",
    )
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    make_learn().handle(payload_for(learn_note(fixture_vault)), run_context(fixture_vault, llm))
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[4].startswith("| 2026-08-16 |")
    assert lines[5].startswith("| 2026-01-01 |")


# --- idempotency (08 §B8) ---------------------------------------------------


def test_rerun_without_an_edit_skips_and_never_calls_the_llm(fixture_vault: Path) -> None:
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    note = learn_note(fixture_vault)
    consumer = make_learn()
    ctx = run_context(fixture_vault, llm)

    first = consumer.handle(payload_for(note), ctx)
    assert first.status is Status.SUCCESS
    calls_after_first = len(llm.calls)

    second = consumer.handle(payload_for(note), ctx)
    assert second.status is Status.SKIP
    assert "already processed" in second.message
    assert len(llm.calls) == calls_after_first
    review_dir = fixture_vault / "resources/flashcards/review"
    assert len(list(review_dir.glob("*.md"))) == 1


def test_rerun_on_an_edited_source_replaces_its_review_file(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06 §7 acceptance: replaces, never accumulates ``-1,-2,…``."""
    freeze(monkeypatch, learn_mod, "2026-08-16")
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    note = learn_note(fixture_vault)
    consumer = make_learn()

    consumer.handle(payload_for(note), run_context(fixture_vault, llm))
    note.write_text(
        note.read_text(encoding="utf-8") + "\nA new paragraph that changes the body.\n",
        encoding="utf-8",
    )

    freeze(monkeypatch, learn_mod, "2026-09-01")
    llm.responses = [cards_response(CARDS[0])]
    second = consumer.handle(payload_for(note), run_context(fixture_vault, llm))

    assert second.status is Status.SUCCESS
    review_dir = fixture_vault / "resources/flashcards/review"
    files = sorted(p.name for p in review_dir.glob("*.md"))
    assert files == ["2026-09-01-spacing-effect.md"]
    assert frontmatter.parse(
        (review_dir / files[0]).read_text(encoding="utf-8")
    ).frontmatter.fields["cards_generated"] == 1
    assert (
        frontmatter.parse(note.read_text(encoding="utf-8")).frontmatter.fields["processing_status"]
        == "learn-processed"
    )


def test_a_foreign_review_file_with_the_same_slug_is_never_clobbered(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, learn_mod)
    foreign = write_note(
        fixture_vault,
        "resources/flashcards/review/2026-08-16-spacing-effect.md",
        "---\nsource_note: areas/health/other.md\nstatus: review\n---\n\nsomeone else's cards\n",
    )
    before = foreign.read_text(encoding="utf-8")
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    note = learn_note(fixture_vault)

    result = make_learn().handle(payload_for(note), run_context(fixture_vault, llm))

    assert foreign.read_text(encoding="utf-8") == before
    written = Path(result.metadata["review_file"])
    assert written != foreign
    assert written.name.startswith("2026-08-16-spacing-effect-")


# --- failure modes ----------------------------------------------------------


def test_no_ai_note_never_reaches_the_llm_and_writes_nothing(fixture_vault: Path) -> None:
    """Vault law (02 / 06 §2). The runner guards centrally; so does this."""
    note = learn_note(fixture_vault, **{"no-ai": True})
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    before = note.read_text(encoding="utf-8")

    result = make_learn().handle(payload_for(note), run_context(fixture_vault, llm))

    assert result.status is Status.SKIP
    assert llm.calls == []
    assert note.read_text(encoding="utf-8") == before
    assert list((fixture_vault / "resources/flashcards/review").glob("*.md")) == []


def test_ollama_down_is_an_error_emission_not_a_crash(fixture_vault: Path) -> None:
    """08 §B11 — the expected remote failure. Error results are NOT
    checkpointed, so the note is retried on the next run."""
    note = learn_note(fixture_vault)
    ctx = run_context(fixture_vault, ollama_client(closed_port_url(), timeout=2.0))

    result = make_learn().handle(payload_for(note), ctx)

    assert result.status is Status.ERROR
    assert "LLM call failed" in result.message
    assert list((fixture_vault / "resources/flashcards/review").glob("*.md")) == []
    assert "learn-processed" not in note.read_text(encoding="utf-8")


def test_missing_llm_client_is_an_error_not_an_exception(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault)
    result = make_learn().handle(payload_for(note), run_context(fixture_vault, None))
    assert result.status is Status.ERROR
    assert "no LLM client" in result.message


@pytest.mark.parametrize(
    "raw",
    ["this is not JSON at all", '{"cards": "not a list"}', '{"notes": []}', '["a", "b"]'],
)
def test_malformed_llm_output_is_an_error_with_the_raw_output_logged(
    fixture_vault: Path, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    note = learn_note(fixture_vault)
    llm = FakeLLM(responses=[raw])
    with caplog.at_level(logging.WARNING, logger=learn_mod.LOG.name):
        result = make_learn().handle(payload_for(note), run_context(fixture_vault, llm))

    assert result.status is Status.ERROR
    assert any("raw output" in record.getMessage() for record in caplog.records)
    assert list((fixture_vault / "resources/flashcards/review").glob("*.md")) == []
    assert "learn-processed" not in note.read_text(encoding="utf-8")


def test_http_error_from_ollama_is_an_error_emission(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault)
    with http_endpoint() as ollama:
        ollama.queue_raw(b"<html>gateway</html>", status=502)
        ctx = run_context(fixture_vault, ollama_client(ollama.base_url))
        result = make_learn().handle(payload_for(note), ctx)
    assert result.status is Status.ERROR
    assert list((fixture_vault / "resources/flashcards/review").glob("*.md")) == []


def test_zero_cards_is_a_skip_not_an_error(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault)
    llm = FakeLLM(responses=['{"cards": []}'])
    result = make_learn().handle(payload_for(note), run_context(fixture_vault, llm))
    assert result.status is Status.SKIP
    assert "0 cards" in result.message


def test_empty_body_is_a_skip(fixture_vault: Path) -> None:
    note = write_note(fixture_vault, "resources/performing/empty.md", "---\ntags:\n- learn\n---\n")
    llm = FakeLLM(responses=[cards_response(*CARDS)])
    result = make_learn().handle(payload_for(note), run_context(fixture_vault, llm))
    assert result.status is Status.SKIP
    assert llm.calls == []


def test_dry_run_calls_no_llm_and_writes_nothing(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault)
    before = note.read_text(encoding="utf-8")
    llm = FakeLLM(responses=[cards_response(*CARDS)])

    result = make_learn().handle(payload_for(note), run_context(fixture_vault, llm, dry_run=True))

    assert result.status is Status.SUCCESS
    assert result.metadata["dry_run"] is True
    assert llm.calls == []
    assert note.read_text(encoding="utf-8") == before
    assert list((fixture_vault / "resources/flashcards/review").glob("*.md")) == []


# --- normalization (external endpoints) -------------------------------------


def test_youtube_notes_are_normalized_through_yt_dlp(
    fixture_vault: Path, tmp_path: Path
) -> None:
    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<c>Spacing</c> works\n\n"
        "00:00:03.000 --> 00:00:05.000\nSpacing works\nbecause of forgetting\n"
    )
    note = learn_note(fixture_vault, rel="resources/performing/yt.md")
    note.write_text(
        note.read_text(encoding="utf-8") + "\nhttps://youtu.be/abcdefghijk\n", encoding="utf-8"
    )
    consumer = make_learn(yt_dlp_command=fake_yt_dlp(tmp_path / "bin", vtt))
    payload = payload_for(note)

    assert consumer.detect_modality(payload) == "youtube"
    out = consumer.normalize_content(payload, "youtube", run_context(fixture_vault))
    assert out.startswith("YouTube video transcript:\nSpacing works because of forgetting")
    assert "Original notes:" in out


def test_yt_dlp_failure_degrades_to_the_original_content(
    fixture_vault: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    note = learn_note(fixture_vault, rel="resources/performing/yt2.md")
    note.write_text(
        note.read_text(encoding="utf-8") + "\nhttps://youtu.be/abcdefghijk\n", encoding="utf-8"
    )
    consumer = make_learn(yt_dlp_command=fake_yt_dlp(tmp_path / "bin", "", exit_code=3))
    payload = payload_for(note)
    with caplog.at_level(logging.WARNING, logger=learn_mod.LOG.name):
        out = consumer.normalize_content(payload, "youtube", run_context(fixture_vault))
    assert out == payload.content
    assert any("yt-dlp" in record.getMessage() for record in caplog.records)


def test_missing_yt_dlp_binary_never_raises(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault, rel="resources/performing/yt3.md")
    note.write_text(
        note.read_text(encoding="utf-8") + "\nhttps://youtu.be/abcdefghijk\n", encoding="utf-8"
    )
    consumer = make_learn(yt_dlp_command=["/nonexistent/yt-dlp-binary"])
    payload = payload_for(note)
    assert consumer.normalize_content(payload, "youtube", run_context(fixture_vault)) == payload.content


def test_audio_notes_are_transcribed_through_whisper(fixture_vault: Path) -> None:
    note = learn_note(
        fixture_vault,
        rel="resources/performing/audio-note.md",
        modalities=["audio"],
    )
    (note.parent / "media").mkdir(parents=True, exist_ok=True)
    (note.parent / "media" / "memo.wav").write_bytes(b"RIFFfake")

    with http_endpoint() as whisper:
        whisper.queue_raw(b"the transcript body")
        consumer = make_learn(whisper_host=whisper.base_url)
        payload = payload_for(note)
        out = consumer.normalize_content(payload, "audio", run_context(fixture_vault))
        request = whisper.requests[0]

    assert out.startswith("Audio transcript:\nthe transcript body")
    assert request["path"] == "/v1/audio/transcriptions"
    assert b'name="file"; filename="memo.wav"' in request["body"]
    assert b"response_format" in request["body"]


def test_whisper_down_degrades_instead_of_crashing(fixture_vault: Path) -> None:
    note = learn_note(
        fixture_vault, rel="resources/performing/audio2.md", modalities=["audio"]
    )
    (note.parent / "media").mkdir(parents=True, exist_ok=True)
    (note.parent / "media" / "memo.wav").write_bytes(b"RIFFfake")
    consumer = make_learn(whisper_host=closed_port_url())
    payload = payload_for(note)
    assert consumer.normalize_content(payload, "audio", run_context(fixture_vault)) == payload.content


def test_audio_without_whisper_host_warns_and_degrades(
    fixture_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    note = learn_note(
        fixture_vault, rel="resources/performing/audio3.md", modalities=["audio"]
    )
    (note.parent / "media").mkdir(parents=True, exist_ok=True)
    (note.parent / "media" / "memo.mp3").write_bytes(b"ID3fake")
    payload = payload_for(note)
    with caplog.at_level(logging.WARNING, logger=learn_mod.LOG.name):
        out = make_learn().normalize_content(payload, "audio", run_context(fixture_vault))
    assert out == payload.content
    assert any("whisper_host" in record.getMessage() for record in caplog.records)


def test_invalid_utf8_in_a_note_does_not_crash_and_is_never_silently_repaired(
    fixture_vault: Path,
) -> None:
    """08 §B1 class: undecodable bytes must not raise out of ``handle``.

    They must also not be written back MANGLED, and that is what the Phase-5
    migration of the write-back changed. While it used a bare
    ``fileops.atomic_write`` the note was re-read with ``errors="replace"``,
    so stamping ``processing_status`` rewrote every undecodable byte as
    U+FFFD and reported success — silent corruption of Matt's note, which
    spec 05 §1 ("no code path may lose note content") forbids.
    ``update_frontmatter`` refuses instead (``fileops._decode_for_mutation``:
    "refusing to rewrite it because the copy would silently differ from the
    original"), and that refusal's own hint is "fix the encoding of the note
    […] and retry" — i.e. ``Status.ERROR``, the status the runner retries.

    The load-bearing assertion is the last one: THE FILE IS UNCHANGED.
    """
    path = fixture_vault / "resources/performing/bad-bytes.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"---\ntags:\n- learn\n---\n" + b"caf\xe9 " * 60
    path.write_bytes(raw)
    payload = payload_for(path)
    llm = FakeLLM(responses=[cards_response(*CARDS)])

    assert make_learn().should_process(payload) is True
    result = make_learn().handle(payload, run_context(fixture_vault, llm))

    assert result.status is Status.ERROR, "retried, per the primitive's own hint"
    assert "UTF-8" in result.message
    assert path.read_bytes() == raw, (
        "the source must be byte-identical — the old bare-atomic_write path "
        "replaced every undecodable byte with U+FFFD and called it success"
    )


def test_a_bare_learn_processed_mark_with_no_output_regenerates(fixture_vault: Path) -> None:
    """The review file is the record (module docstring): a note marked
    ``learn-processed`` with nothing to show for it is not "done"."""
    note = learn_note(fixture_vault, processing_status="learn-processed")
    llm = FakeLLM(responses=[cards_response(*CARDS)])

    result = make_learn().handle(payload_for(note), run_context(fixture_vault, llm))

    assert result.status is Status.SUCCESS
    assert len(list((fixture_vault / "resources/flashcards/review").glob("*.md"))) == 1


def test_deleting_the_review_file_makes_the_next_run_regenerate(fixture_vault: Path) -> None:
    note = learn_note(fixture_vault)
    consumer = make_learn()
    first = consumer.handle(
        payload_for(note), run_context(fixture_vault, FakeLLM(responses=[cards_response(*CARDS)]))
    )
    Path(first.metadata["review_file"]).unlink()

    second = consumer.handle(
        payload_for(note), run_context(fixture_vault, FakeLLM(responses=[cards_response(*CARDS)]))
    )

    assert second.status is Status.SUCCESS
    assert Path(second.metadata["review_file"]).exists()
