"""``question_answer`` consumer suite (spec 06 §3.3; regression obligation
08 §B9 — the loose heuristic that produced 874 noise files).

All external endpoints are fake and local (a 127.0.0.1 ``http.server`` or an
in-process client). No real vault, state, network or binaries.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from organize_core import frontmatter
from organize_core.consumers import question_answer as qa_mod
from organize_core.consumers.base import Status, get_consumer_types
from organize_core.consumers.question_answer import QuestionAnswerConsumer
from organize_core.errors import ConfigError, LLMError, LLMUnavailable
from test_consumer_learn_fakes import (
    FakeLLM,
    closed_port_url,
    consumer_config,
    http_endpoint,
    ollama_client,
    payload_for,
    run_context,
    write_note,
)

QUESTION = "Why do spaced repetition intervals grow geometrically?"
ANSWER = "Because forgetting is exponential, so review points must spread out to match."


def freeze(monkeypatch: pytest.MonkeyPatch, stamp: str = "2026-08-16") -> None:
    when = datetime.fromisoformat(stamp).replace(tzinfo=UTC)
    monkeypatch.setattr(qa_mod, "_utc_now", lambda: when)


def make_qa(**options) -> QuestionAnswerConsumer:
    return QuestionAnswerConsumer(consumer_config("question_answer", "question_answer", **options))


def capture(vault: Path, body: str, rel: str = "capture/raw_capture/q.md", **fm) -> Path:
    fields = {"id": "q-capture", "tags": ["question"], "processing_status": "raw"}
    fields.update(fm)
    text = frontmatter.serialize(
        frontmatter.Document(
            frontmatter=frontmatter.Frontmatter(fields=fields), body="\n" + body + "\n"
        )
    )
    return write_note(vault, rel, text)


# --- construction (B2) ------------------------------------------------------


def test_constructor_is_pure_and_does_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("constructor performed I/O")

    monkeypatch.setattr(Path, "read_text", explode)
    monkeypatch.setattr(Path, "exists", explode)
    monkeypatch.setattr(Path, "glob", explode)
    monkeypatch.setattr("subprocess.run", explode)

    assert make_qa(max_questions=3).max_questions == 3


def test_registered_under_its_contract_name() -> None:
    assert get_consumer_types()["question_answer"] is QuestionAnswerConsumer


def test_uses_llm_is_true_so_the_runner_denies_no_ai_notes() -> None:
    assert QuestionAnswerConsumer.uses_llm is True


def test_unknown_option_fails_naming_the_key() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_qa(ollama_model="gemma4:e4b")
    assert "ollama_model" in str(excinfo.value)
    assert "consumers.question_answer" in str(excinfo.value)


def test_live_config_option_names_are_accepted() -> None:
    consumer = make_qa(marker_tag="question", answers_dir="resources/answers")
    assert consumer.answers_dir == "resources/answers"
    assert consumer.marker_tags == {"question", "q"}


def test_empty_marker_tag_is_refused() -> None:
    with pytest.raises(ConfigError):
        make_qa(marker_tag="  ", marker_tag_aliases=[])


# --- trigger (08 §B9) -------------------------------------------------------


def test_heuristic_detection_is_off_by_default() -> None:
    assert make_qa().heuristic_detection is False


def test_untagged_question_shaped_capture_is_not_processed_by_default(
    fixture_vault: Path,
) -> None:
    """08 §B9: '?' in a short capture used to be enough. It is not any more."""
    note = capture(fixture_vault, "Is this worth doing?", tags=["idea"])
    assert make_qa().should_process(payload_for(note)) is False


def test_untagged_interrogative_capture_is_not_processed_by_default(
    fixture_vault: Path,
) -> None:
    note = capture(fixture_vault, "How does compound interest actually work", tags=["idea"])
    assert make_qa().should_process(payload_for(note)) is False


def test_the_heuristic_is_still_available_as_an_opt_in(fixture_vault: Path) -> None:
    question_mark = capture(fixture_vault, "Is this worth doing?", tags=["idea"])
    interrogative = capture(
        fixture_vault,
        "How does compound interest actually work",
        rel="capture/raw_capture/q2.md",
        tags=["idea"],
    )
    consumer = make_qa(heuristic_detection=True)
    assert consumer.should_process(payload_for(question_mark)) is True
    assert consumer.should_process(payload_for(interrogative)) is True


def test_the_heuristic_still_respects_its_length_bound(fixture_vault: Path) -> None:
    note = capture(fixture_vault, "Lorem ipsum dolor sit amet? " * 40, tags=["idea"])
    assert make_qa(heuristic_detection=True).should_process(payload_for(note)) is False


@pytest.mark.parametrize("tag", ["question", "Question", "q", "Q"])
def test_explicit_tags_trigger_case_insensitively(fixture_vault: Path, tag: str) -> None:
    note = capture(fixture_vault, QUESTION, tags=[tag])
    assert make_qa().should_process(payload_for(note)) is True


def test_scalar_tag_value_is_coerced_not_string_indexed(fixture_vault: Path) -> None:
    note = capture(fixture_vault, QUESTION, tags="question")
    assert make_qa().should_process(payload_for(note)) is True


def test_aliases_and_sources_are_not_mistaken_for_tags(fixture_vault: Path) -> None:
    """08 §B9: the hand-rolled parse swept every ``- item`` line in the
    frontmatter — aliases, sources, modalities — into the tag list."""
    note = capture(
        fixture_vault,
        "A plain note with no interrogative content whatsoever.",
        tags=["idea"],
        aliases=["question"],
        sources=["q"],
        modalities=["question"],
    )
    assert make_qa().should_process(payload_for(note)) is False


def test_fixture_question_capture_triggers(fixture_vault: Path) -> None:
    note = fixture_vault / "capture/raw_capture/2026-07-02T10:00:00.000Z.md"
    assert make_qa().should_process(payload_for(note)) is True


def test_non_markdown_is_ignored(fixture_vault: Path) -> None:
    payload = payload_for(fixture_vault / "capture/raw_capture/clipboard-dump.txt")
    assert make_qa().should_process(payload) is False


# --- extraction -------------------------------------------------------------


def test_question_lines_are_extracted(fixture_vault: Path) -> None:
    note = capture(
        fixture_vault,
        "## Content\nWhy does spacing work?\nSome context here.\nHow do I schedule it\n",
    )
    assert make_qa().extract_questions(payload_for(note)) == [
        "Why does spacing work?",
        "How do I schedule it",
    ]


def test_extraction_is_capped_at_max_questions(fixture_vault: Path) -> None:
    body = "\n".join(f"Why number {index}?" for index in range(10))
    note = capture(fixture_vault, body)
    assert len(make_qa().extract_questions(payload_for(note))) == 5
    assert len(make_qa(max_questions=2).extract_questions(payload_for(note))) == 2


def test_whole_body_is_the_question_when_no_line_qualifies(fixture_vault: Path) -> None:
    note = capture(fixture_vault, "## Content\nThe thing I keep wondering about spacing")
    assert make_qa().extract_questions(payload_for(note)) == [
        "The thing I keep wondering about spacing"
    ]


def test_headings_and_rules_are_not_questions(fixture_vault: Path) -> None:
    note = capture(fixture_vault, "# Why a heading?\n---\nWhy a real question?")
    assert make_qa().extract_questions(payload_for(note)) == ["Why a real question?"]


# --- golden run -------------------------------------------------------------


def test_golden_run_writes_the_answer_note_and_its_flashcard(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch)
    note = capture(fixture_vault, QUESTION)
    with http_endpoint() as ollama:
        ollama.queue_completion(ANSWER)
        ctx = run_context(fixture_vault, ollama_client(ollama.base_url))
        result = make_qa().handle(payload_for(note), ctx)

    assert result.status is Status.SUCCESS
    assert result.metadata == {
        "questions_found": 1,
        "answered": 1,
        "answer_files": [
            str(
                fixture_vault
                / "resources/answers/2026-08-16-why-do-spaced-repetition-intervals-grow.md"
            )
        ],
    }

    answer = fixture_vault / "resources/answers/2026-08-16-why-do-spaced-repetition-intervals-grow.md"
    assert answer.read_text(encoding="utf-8") == (
        "---\n"
        "id: 2026-08-16-why-do-spaced-repetition-intervals-grow\n"
        "tags:\n"
        "- ai-generated\n"
        "- question-answer\n"
        "created_date: '2026-08-16'\n"
        "last_edited_date: '2026-08-16'\n"
        "source_capture: capture/raw_capture/q.md\n"
        "---\n"
        "\n"
        f"# {QUESTION}\n"
        "\n"
        f"{ANSWER}\n"
        "\n"
        "---\n"
        "\n"
        "*Answer generated by AI from a captured question. "
        "Verify important claims independently.*\n"
        "*Source capture: [[q]]*\n"
    )

    card = (
        fixture_vault
        / "resources/flashcards/review/2026-08-16-q-why-do-spaced-repetition-intervals-grow.md"
    )
    assert card.read_text(encoding="utf-8") == (
        "---\n"
        "article: Captured Question\n"
        "author: me\n"
        "source_note: capture/raw_capture/q.md\n"
        f"question: {QUESTION}\n"
        "generated: '2026-08-16'\n"
        "status: review\n"
        "deck: Reading::Questions\n"
        "cards_generated: 1\n"
        "---\n"
        "\n"
        "# Cards: Captured Question\n"
        "\n"
        "## Card 1 [tier2_conceptual]\n"
        "\n"
        f"**Q:** {QUESTION}\n"
        "\n"
        "**A:** *[Write your answer here before reading below]*\n"
        "\n"
        "<details><summary>Model answer (click after writing yours)</summary>\n"
        "\n"
        f"{ANSWER}\n"
        "\n"
        "</details>\n"
        "\n"
        "Tags: question-answer, captured-question\n"
    )


def test_golden_run_uses_the_specced_llm_call_parameters(fixture_vault: Path) -> None:
    llm = FakeLLM(responses=[ANSWER])
    note = capture(fixture_vault, QUESTION)
    make_qa().handle(payload_for(note), run_context(fixture_vault, llm))
    call = llm.calls[0]
    assert call["json_mode"] is False
    assert call["temperature"] == 0.3
    assert call["max_tokens"] == 1024
    assert call["timeout_seconds"] == 90.0
    assert "say so explicitly rather than guessing" in call["system"]


def test_generated_files_round_trip(fixture_vault: Path) -> None:
    llm = FakeLLM(responses=[ANSWER])
    note = capture(fixture_vault, QUESTION)
    make_qa().handle(payload_for(note), run_context(fixture_vault, llm))
    for produced in [
        *(fixture_vault / "resources/answers").glob("*.md"),
        *(fixture_vault / "resources/flashcards/review").glob("*.md"),
    ]:
        text = produced.read_text(encoding="utf-8")
        assert frontmatter.serialize(frontmatter.parse(text)) == text


def test_multiple_questions_each_get_their_own_answer(fixture_vault: Path) -> None:
    llm = FakeLLM(responses=["first answer", "second answer"])
    note = capture(fixture_vault, "Why does spacing work?\nHow should I schedule reviews?")
    result = make_qa().handle(payload_for(note), run_context(fixture_vault, llm))
    assert result.status is Status.SUCCESS
    assert result.metadata["answered"] == 2
    assert len(list((fixture_vault / "resources/answers").glob("*.md"))) == 2


def test_the_source_capture_is_never_modified(fixture_vault: Path) -> None:
    """This consumer must not touch ``processing_status``: ``raw`` is how the
    plugin's session query finds unorganized captures (03 §2/§7)."""
    note = capture(fixture_vault, QUESTION)
    before = note.read_bytes()
    llm = FakeLLM(responses=[ANSWER])
    make_qa().handle(payload_for(note), run_context(fixture_vault, llm))
    assert note.read_bytes() == before


# --- idempotency ------------------------------------------------------------


def test_rerun_replaces_its_own_output_instead_of_accumulating(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch)
    note = capture(fixture_vault, QUESTION)
    consumer = make_qa()

    first = consumer.handle(payload_for(note), run_context(fixture_vault, FakeLLM(responses=[ANSWER])))
    second = consumer.handle(
        payload_for(note), run_context(fixture_vault, FakeLLM(responses=["a revised answer"]))
    )

    assert first.status is Status.SUCCESS
    assert second.status is Status.SUCCESS
    answers = sorted(p.name for p in (fixture_vault / "resources/answers").glob("*.md"))
    cards = sorted(p.name for p in (fixture_vault / "resources/flashcards/review").glob("*.md"))
    assert answers == ["2026-08-16-why-do-spaced-repetition-intervals-grow.md"]
    assert cards == ["2026-08-16-q-why-do-spaced-repetition-intervals-grow.md"]
    assert "a revised answer" in (fixture_vault / "resources/answers" / answers[0]).read_text(
        encoding="utf-8"
    )


def test_rerun_on_a_later_date_moves_the_file_rather_than_duplicating_it(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch, "2026-08-16")
    note = capture(fixture_vault, QUESTION)
    consumer = make_qa()
    consumer.handle(payload_for(note), run_context(fixture_vault, FakeLLM(responses=[ANSWER])))

    freeze(monkeypatch, "2026-09-02")
    consumer.handle(payload_for(note), run_context(fixture_vault, FakeLLM(responses=["newer"])))

    answers = sorted(p.name for p in (fixture_vault / "resources/answers").glob("*.md"))
    assert answers == ["2026-09-02-why-do-spaced-repetition-intervals-grow.md"]


def test_a_foreign_answer_file_with_the_same_slug_is_never_clobbered(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch)
    foreign = write_note(
        fixture_vault,
        "resources/answers/2026-08-16-why-do-spaced-repetition-intervals-grow.md",
        "---\nsource_capture: capture/raw_capture/somebody-else.md\n---\n\n# Another question?\n\nkeep me\n",
    )
    before = foreign.read_text(encoding="utf-8")
    note = capture(fixture_vault, QUESTION)

    result = make_qa().handle(payload_for(note), run_context(fixture_vault, FakeLLM(responses=[ANSWER])))

    assert result.status is Status.SUCCESS
    assert foreign.read_text(encoding="utf-8") == before
    written = Path(result.metadata["answer_files"][0])
    assert written != foreign
    assert written.name.startswith("2026-08-16-why-do-spaced-repetition-intervals-grow-")


# --- failure modes ----------------------------------------------------------


def test_no_ai_note_never_reaches_the_llm_and_writes_nothing(fixture_vault: Path) -> None:
    note = capture(fixture_vault, QUESTION, **{"no-ai": True})
    llm = FakeLLM(responses=[ANSWER])
    before = note.read_bytes()

    result = make_qa().handle(payload_for(note), run_context(fixture_vault, llm))

    assert result.status is Status.SKIP
    assert llm.calls == []
    assert note.read_bytes() == before
    assert list((fixture_vault / "resources/answers").glob("*.md")) == []


def test_ollama_down_is_an_error_emission_and_the_run_continues(fixture_vault: Path) -> None:
    note = capture(fixture_vault, QUESTION)
    ctx = run_context(fixture_vault, ollama_client(closed_port_url(), timeout=2.0))

    result = make_qa().handle(payload_for(note), ctx)

    assert result.status is Status.ERROR
    assert result.metadata["answered"] == 0
    assert list((fixture_vault / "resources/answers").glob("*.md")) == []


def test_an_unavailable_backend_aborts_the_remaining_questions(fixture_vault: Path) -> None:
    """Hammering a dead host once per question only slows the run (06 §6)."""
    llm = FakeLLM(responses=[LLMUnavailable("ollama is down")])
    note = capture(fixture_vault, "Why one?\nWhy two?\nWhy three?")

    result = make_qa().handle(payload_for(note), run_context(fixture_vault, llm))

    assert result.status is Status.ERROR
    assert result.metadata["questions_found"] == 3
    assert len(llm.calls) == 1  # not one per question


def test_missing_llm_client_is_an_error_not_an_exception(fixture_vault: Path) -> None:
    note = capture(fixture_vault, QUESTION)
    result = make_qa().handle(payload_for(note), run_context(fixture_vault, None))
    assert result.status is Status.ERROR
    assert list((fixture_vault / "resources/answers").glob("*.md")) == []


def test_malformed_backend_response_is_an_error_with_nothing_written(
    fixture_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    note = capture(fixture_vault, QUESTION)
    with http_endpoint() as ollama:
        ollama.queue_raw(b"<html>not ollama</html>")
        ctx = run_context(fixture_vault, ollama_client(ollama.base_url))
        with caplog.at_level(logging.WARNING, logger=qa_mod.LOG.name):
            result = make_qa().handle(payload_for(note), ctx)

    assert result.status is Status.ERROR
    assert any("could not answer" in r.getMessage() for r in caplog.records)
    assert list((fixture_vault / "resources/answers").glob("*.md")) == []
    assert list((fixture_vault / "resources/flashcards/review").glob("*.md")) == []


def test_empty_completion_is_a_failure_not_an_empty_answer(fixture_vault: Path) -> None:
    note = capture(fixture_vault, QUESTION)
    with http_endpoint() as ollama:
        ollama.queue_completion("")
        ctx = run_context(fixture_vault, ollama_client(ollama.base_url))
        result = make_qa().handle(payload_for(note), ctx)
    assert result.status is Status.ERROR
    assert list((fixture_vault / "resources/answers").glob("*.md")) == []


def test_a_partly_answered_note_is_an_error_so_it_is_retried(fixture_vault: Path) -> None:
    llm = FakeLLM(responses=["only answer", LLMError("model exploded")])
    note = capture(fixture_vault, "Why one?\nWhy two?")

    result = make_qa().handle(payload_for(note), run_context(fixture_vault, llm))

    assert result.status is Status.ERROR
    assert result.metadata["answered"] == 1
    assert len(list((fixture_vault / "resources/answers").glob("*.md"))) == 1


def test_a_capture_with_no_questions_is_a_skip(fixture_vault: Path) -> None:
    note = capture(fixture_vault, "   ")
    llm = FakeLLM(responses=[ANSWER])
    result = make_qa().handle(payload_for(note), run_context(fixture_vault, llm))
    assert result.status is Status.SKIP
    assert llm.calls == []


def test_dry_run_calls_no_llm_and_writes_nothing(fixture_vault: Path) -> None:
    note = capture(fixture_vault, QUESTION)
    llm = FakeLLM(responses=[ANSWER])

    result = make_qa().handle(payload_for(note), run_context(fixture_vault, llm, dry_run=True))

    assert result.status is Status.SUCCESS
    assert result.metadata["dry_run"] is True
    assert llm.calls == []
    assert list((fixture_vault / "resources/answers").glob("*.md")) == []


def test_invalid_utf8_in_a_capture_does_not_crash_the_consumer(fixture_vault: Path) -> None:
    """08 §B1 class: undecodable bytes must degrade, never raise."""
    path = fixture_vault / "capture/raw_capture/bad-bytes-q.md"
    path.write_bytes(b"---\ntags:\n- question\n---\nWhy is this caf\xe9 broken?\n")
    llm = FakeLLM(responses=[ANSWER])
    payload = payload_for(path)

    assert make_qa().should_process(payload) is True
    result = make_qa().handle(payload, run_context(fixture_vault, llm))
    assert result.status is Status.SUCCESS
