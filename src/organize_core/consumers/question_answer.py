"""``question_answer`` consumer: question captures → answered notes
(spec 06 §3.3). [Phase 3 fills in bodies.]

Trigger TIGHTENED per B9: explicit tag ``question``/``q`` (case-insensitive,
from PARSED frontmatter via the shared module — never a hand-rolled
re-parse). The interrogative heuristic (``?`` in <500 chars, leading
interrogative word) is kept but opt-in: ``heuristic_detection = false``
default.
"""

from __future__ import annotations

from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    register,
)


@register("question_answer")
class QuestionAnswerConsumer(Consumer):
    uses_llm = True  # no-ai notes centrally excluded (02)

    def should_process(self, payload: NotePayload) -> bool:
        raise NotImplementedError

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        raise NotImplementedError

    # --- steps (unit-test targets) ---------------------------------------

    def extract_questions(self, payload: NotePayload) -> list[str]:
        """Up to max_questions (5): lines ending ``?`` or starting with an
        interrogative; else the whole body (06 §3.3)."""
        raise NotImplementedError

    def answer(self, question: str, ctx: RunContext) -> str:
        """Ollama /api/generate, temp 0.3, num_predict 1024, 90 s, the
        research-assistant system prompt (says-so-explicitly rule)
        (06 §3.3). LLM failures ⇒ ERROR result upstream, retried."""
        raise NotImplementedError

    def write_answer_note(self, payload: NotePayload, question: str, answer: str, ctx: RunContext) -> str:
        """``resources/answers/<date>-<slug>.md`` with the specced
        frontmatter (tags [ai-generated, question-answer], source_capture),
        body = question H1 + answer + verify-independently disclaimer +
        [[wikilink]] to source; plus a tier-2 flashcard in
        flashcards/review with deck Reading::Questions (06 §3.3)."""
        raise NotImplementedError
