"""``learn`` consumer: notes → spaced-repetition flashcards (spec 06 §3.2).
[Phase 3 fills in bodies.]

Trigger: ``.md``, ≥ min_content_length, and (opt-in tag ∈
{learn, remember, study, anki} OR triage score ≥ triage_threshold 0.7).
Scope: resources|areas|projects minus flashcards/readwise/prompts/
templates/answers.

Idempotency decision (06 §3.2, resolves B8): after successful generation,
write ``processing_status: learn-processed`` back to the source via the
ROUND-TRIP-SAFE frontmatter writer, honor the guard, and re-runs on edit
REPLACE the prior review file (deterministic filename from source slug)
instead of accumulating duplicates (901 files today).
"""

from __future__ import annotations

from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    register,
)


@register("learn")
class LearnConsumer(Consumer):
    uses_llm = True  # no-ai notes centrally excluded (02)

    def should_process(self, payload: NotePayload) -> bool:
        raise NotImplementedError

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        raise NotImplementedError

    # --- steps (unit-test targets) ---------------------------------------

    def triage_score(self, payload: NotePayload) -> float:
        """0.4·novelty + 0.4·relevance + 0.2·quality, heuristics as
        implemented (06 §3.2); weights + topic-tag set exposed in config;
        each candidate's score logged at DEBUG."""
        raise NotImplementedError

    def detect_modality(self, payload: NotePayload) -> str:
        """Ordered detection: youtube → audio → screenshot → article(clip)
        → quote → thought → article(len) → note (06 §3.2)."""
        raise NotImplementedError

    def normalize_content(self, payload: NotePayload, modality: str, ctx: RunContext) -> str:
        """youtube → yt-dlp auto-subs (30 s timeout); audio → first
        media/*.wav|mp3 POSTed to Whisper /v1/audio/transcriptions (120 s)
        (06 §3.2). All subprocess/HTTP: timeouts + errors="replace"."""
        raise NotImplementedError

    def build_prompts(self, payload: NotePayload, modality: str, content: str) -> tuple[str, str]:
        """(system, user): the seven-tier card pedagogy + curiosity-framing
        rule + 10 quality rules; self-improving ``## Learned Rules`` from
        card-generation-guidelines.md when present; user prompt with
        ``Generate 3-{max_cards} cards…`` + content[:8000] (06 §3.2)."""
        raise NotImplementedError

    def write_review_file(self, payload: NotePayload, cards: str, ctx: RunContext) -> str:
        """``resources/flashcards/review/<YYYY-MM-DD>-<slug>.md`` with the
        specced frontmatter and ``## Card N [tier]`` blocks; deterministic
        slug ⇒ replace on re-run; append to generation-log.md, creating it
        from a template when missing (06 §3.2)."""
        raise NotImplementedError
