"""``learn`` consumer: notes → spaced-repetition flashcards (spec 06 §3.2).

Trigger: ``.md``, ≥ min_content_length, and (opt-in tag ∈
{learn, remember, study, anki} OR triage score ≥ triage_threshold 0.7).
Scope: resources|areas|projects minus flashcards/readwise/prompts/
templates/answers.

Idempotency decision (06 §3.2, resolves B8): after successful generation,
write ``processing_status: learn-processed`` back to the source via the
ROUND-TRIP-SAFE frontmatter writer, honor the guard, and re-runs on edit
REPLACE the prior review file (deterministic filename from source slug)
instead of accumulating duplicates (901 files today).

That SOURCE-note write-back is a doc-12 ``meta_edit`` and goes through
``fileops.update_frontmatter`` + ``RunContext.op_context`` — ARCHITECTURE,
"Phase-4 rulings, auto_tagger batch" (f24ee2e), PHASE-5 CHECKLIST item (a).
The consumer's own NEW OUTPUT files (review files, the generation log) keep
using ``fileops.atomic_write``: the same ruling scopes the migration to the
source note ("new-output files — flashcards/answers — stay store-audited,
defensible as-is"), and they are this consumer's product rather than an edit
to a note Matt wrote.

Decisions this seat made where doc 06 is silent (the old code at
``../organize/scripts/automation/consumers/learn.py`` is the behavioral
reference; its section-B defects are NOT ported):

* **The review file is the record of what was processed.** 06 §3.2 wants
  both "honor the ``learn-processed`` guard" and "a rerun on an EDITED
  source regenerates and replaces its review file" (§7 acceptance list).
  A bare ``processing_status`` guard cannot do both, because our own
  write-back changes the note hash and therefore re-delivers the note on
  the very next run. So the review file carries ``source_hash`` (sha256 of
  the source BODY at generation time) and the guard is:
  ``processing_status == "learn-processed"`` AND a review file for this
  source exists AND its ``source_hash`` matches ⇒ SKIP. Anything else
  regenerates and replaces. Consequences, all intended: our own write-back
  round-trips to SKIP (stable, one extra delivery); a real edit
  regenerates; deleting the review file regenerates.
* **Deterministic output naming.** ``<YYYY-MM-DD>-<slug>.md`` per 06 §3.2.
  Because the date moves, "replace" is implemented by sweeping
  ``*-<slug>.md`` in the review dir and deleting only files whose
  ``source_note`` is THIS note — never a file belonging to another source
  (that case gets a source-derived suffix instead). This is what kills the
  ``-1,-2,…`` accumulation without ever destroying a third party's file.
  Known bound: identity is the slug, so renaming a source's alias orphans
  one old review file (a per-note full scan of the review dir is the only
  alternative, and at 900 files × 20 notes/run it is not worth it).
* **No second LLM path** (09 §2): generation goes through the ONE shared
  client on ``RunContext.llm``. Whisper and yt-dlp are transcription
  endpoints, not completions, so they are called directly here — with
  explicit timeouts and ``errors="replace"`` (06 §6, the B1 class).
* **B10 fixed**: the title comes from ``Frontmatter.get_list("aliases")``,
  never ``aliases[0]`` on a raw value (which yields one character when the
  value is a scalar string).
* **Self-defence scope filter**: the runner does include/exclude path
  filtering, but this consumer additionally refuses its own output tree
  (``exclude_dirs``) so a mis-set config can never make it eat the
  flashcards it just generated.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from organize_core import fileops, frontmatter
from organize_core.config import ConsumerConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    Status,
    register,
)
from organize_core.errors import ConfigError, ConsumerError, LLMError, OrganizeError
from organize_core.llm import extract_json

LOG = logging.getLogger(__name__)

#: Opt-in tags that always trigger generation (06 §3.2).
DEFAULT_TRIGGER_TAGS: tuple[str, ...] = ("learn", "remember", "study", "anki")

#: Topic-tag set feeding the triage *relevance* signal. 06 §3.2 requires it
#: to be config-exposed rather than the hardcoded literal it used to be.
DEFAULT_TOPIC_TAGS: tuple[str, ...] = (
    "science",
    "research",
    "learning",
    "productivity",
    "health",
    "finance",
    "career",
    "ai",
    "psychology",
    "systems",
)

#: Trees this consumer refuses even if include_paths would admit them
#: (06 §3.2 scope: "resources|areas|projects minus flashcards/readwise/
#: prompts/templates/answers"). Vault-relative prefixes.
DEFAULT_EXCLUDE_DIRS: tuple[str, ...] = (
    "resources/flashcards",
    "resources/readwise",
    "resources/prompts",
    "resources/templates",
    "resources/answers",
)

#: Triage weights (06 §3.2: 0.4·novelty + 0.4·relevance + 0.2·quality).
DEFAULT_TRIAGE_WEIGHTS: dict[str, float] = {
    "novelty": 0.4,
    "relevance": 0.4,
    "quality": 0.2,
}

_YOUTUBE_RE = re.compile(r"(youtube\.com/watch|youtu\.be/)")
_YOUTUBE_ID_RES = (
    re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([a-zA-Z0-9_-]{11})"),
)
_VTT_TIMECODE_RE = re.compile(r"^\d{2}:\d{2}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_NUMBERS_RE = re.compile(r"\d+\.?\d*%|\d{2,}")
_CITATION_RE = re.compile(r"https?://|et al\.|20\d{2}\)")
_LEARNED_RULES_RE = re.compile(r"## Learned Rules.*?\n(.*?)(?=\n## |\Z)", re.DOTALL)
_DATED_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")

#: The seven-tier card pedagogy, curiosity-framing rule and 10 quality rules
#: (06 §3.2). Ported verbatim in intent from the live consumer; the tier enum
#: additionally names ``synthesis_drawing``, which the old enum omitted while
#: describing the tier below it.
SYSTEM_PROMPT = """You are a spaced repetition and active learning expert. \
Generate flashcards and synthesis challenges from the provided content.

OUTPUT FORMAT: Valid JSON only.
{
  "cards": [
    {
      "tier": "thesis|tier1_factual|tier2_conceptual|synthesis_connection|\
synthesis_feynman|synthesis_devils_advocate|synthesis_drawing",
      "front": "question text",
      "back": "answer text (for tier1/thesis) OR '[WRITE YOUR ANSWER FIRST]' (for tier2/synthesis)",
      "model_answer": "the AI model answer (REQUIRED for tier2 and synthesis tiers, \
omit for tier1/thesis)",
      "tags": ["topic-tag"]
    }
  ]
}

IMPORTANT: For tier2 and synthesis cards, "model_answer" MUST be its own JSON field \
— NEVER inside tags. The model_answer must be plain text, NOT JSON.

CARD TIERS:
- thesis (1 per source): Core argument/insight in one sentence. Provide front + back.
- tier1_factual (~40%): Key facts/numbers. Provide front + back. Be SPECIFIC — name \
subscales, cite numbers, reference authors.
- tier2_conceptual (~25%): "Why/how" questions. back = "[WRITE YOUR ANSWER FIRST]", \
add "model_answer" field with plain text answer.
- synthesis_connection (~15%): Cross-domain link. back = "[WRITE YOUR ANSWER FIRST]", \
add "model_answer" field.
- synthesis_feynman (~10%): Explain simply. back = "[WRITE YOUR ANSWER FIRST]", \
add "model_answer" field.
- synthesis_devils_advocate (~10%): Best counterargument. back = \
"[WRITE YOUR ANSWER FIRST]", add "model_answer" field. Target SPECIFIC claims with \
SPECIFIC objections (e.g., "Why should we be skeptical of the d=0.80 effect size?").
- synthesis_drawing (~5%): ONLY when content involves systems, processes, or causal \
chains. Prompt = "Draw a diagram showing [relationship]." back = "[DRAW THIS]", add \
"model_answer" describing what the diagram should contain. Based on drawing-to-learn \
research (g = 0.69).

CURIOSITY FRAMING — MANDATORY for every card front:
NEVER start a question with "What is...", "What are...", "Define...", or "Describe...".
Instead, use ONE of these patterns:
- PARADOX: "Why does X happen despite Y?"
- SURPRISE: "Researchers expected X but found ___"
- COMPARISON: "X and Y seem similar, but what key difference matters most?"
- CHALLENGE: "The popular belief is X. What does the evidence actually show?"

BAD → GOOD examples:
- BAD: "What is the 4C model of mental toughness?"
  GOOD: "Why did researchers add a 4th C to Kobasa's hardiness model — what was missing?"
- BAD: "What percentage of mental toughness is heritable?"
  GOOD: "Which aspect of mental toughness has the strongest genetic basis — and which \
is most trainable?"
- BAD: "What is WOOP?"
  GOOD: "Why does fantasizing about your ideal future make you LESS likely to achieve \
it, and what technique fixes this?"

RULES:
1. Each card tests ONE thing (minimum information principle)
2. No yes/no questions, no enumeration questions
3. Answers: 1-3 sentences max
4. Cards must work WITHOUT the source content
5. Include source attribution in tags
6. Always include tags as an array of strings (never omit tags)
7. For thoughts/reflections: extract the INSIGHT, not the diary entry
8. For quotes: test understanding of meaning and application, not recall
9. For audio/video transcripts: focus on key arguments and surprising claims
10. Skip vague, motivational, or overly personal content"""

MODALITY_INSTRUCTIONS: dict[str, str] = {
    "article": (
        "This is an article or long-form content. Focus on key arguments, "
        "evidence, and implications."
    ),
    "youtube": (
        "This is a YouTube video transcript. Focus on the speaker's main arguments "
        "and surprising claims. Ignore filler words and repetition."
    ),
    "audio": (
        "This is a transcribed audio recording. Extract key insights and decisions "
        "discussed."
    ),
    "thought": (
        "This is a personal reflection or thought. Extract the INSIGHT — what did the "
        "person realize? Make it generalizable."
    ),
    "quote": (
        "This is a quote or reference. Test understanding of its meaning, context, "
        "and application — not just word-for-word recall."
    ),
    "note": "This is a knowledge note. Extract the most important concepts and relationships.",
    "screenshot": "This is text extracted from a screenshot. Focus on the factual content.",
}

#: Created when ``generation_log_path`` is missing (06 §3.2 — today the log
#: row is silently dropped unless the file already exists).
GENERATION_LOG_TEMPLATE = """# Flashcard generation log

Maintained by the `learn` automation consumer (spec 06 §3.2). Newest first.

| Date | Article | Cards | Kept | Edited | Deleted | Tier breakdown | Modality | Source |
|---|---|---|---|---|---|---|---|---|
"""

_LOG_SEPARATOR = "|---|---|---|---|---|---|---|---|---|"
_LOG_HEADER_PREFIX = "| Date | Article |"

_MISSING = object()


def _utc_now() -> datetime:
    """Single clock read for the whole module (tests monkeypatch this)."""
    return datetime.now(UTC)


# --- option plumbing --------------------------------------------------------


def _as_str(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string, got {type(value).__name__}")
    return value


def _as_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer, got {type(value).__name__}")
    if value < 0:
        raise ConfigError(f"{key} must not be negative")
    return value


def _as_float(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number, got {type(value).__name__}")
    return float(value)


def _as_str_list(value: Any, key: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings")
    return list(value)


def _as_weights(value: Any, key: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table of weight names to numbers")
    unknown = sorted(set(value) - set(DEFAULT_TRIAGE_WEIGHTS))
    if unknown:
        raise ConfigError(
            f"{key}: unknown weight(s) {', '.join(unknown)}",
            hint="valid weights: " + ", ".join(sorted(DEFAULT_TRIAGE_WEIGHTS)),
        )
    weights = dict(DEFAULT_TRIAGE_WEIGHTS)
    for name, raw in value.items():
        weights[name] = _as_float(raw, f"{key}.{name}")
    return weights


def _coerce_options(
    consumer: ConsumerConfig,
    schema: Mapping[str, tuple[Any, Callable[[Any, str], Any]]],
) -> dict[str, Any]:
    """Validate ``consumer.options`` against ``schema`` (spec 03 §1: every
    key is honored or fails loudly NAMING the key). Pure — no I/O (06 §1)."""
    unknown = sorted(key for key in consumer.options if key not in schema)
    if unknown:
        raise ConfigError(
            f"consumers.{consumer.name}: unknown option(s) {', '.join(unknown)}",
            hint="valid options: " + ", ".join(sorted(schema)),
        )
    resolved: dict[str, Any] = {}
    for key, (default, coerce) in schema.items():
        raw = consumer.options.get(key, _MISSING)
        if raw is _MISSING:
            resolved[key] = default() if callable(default) else default
        else:
            resolved[key] = coerce(raw, f"consumers.{consumer.name}.{key}")
    return resolved


# --- small shared helpers ---------------------------------------------------


def _document(payload: NotePayload) -> frontmatter.Document:
    """A Document view over the ALREADY-PARSED payload — the shared
    frontmatter module stays the only parser (structural decision 2)."""
    return frontmatter.Document(
        frontmatter=frontmatter.Frontmatter(fields=dict(payload.frontmatter or {})),
        body=payload.content or "",
    )


def _field_list(payload: NotePayload, key: str) -> list[Any]:
    return frontmatter.Frontmatter(fields=dict(payload.frontmatter or {})).get_list(key)


def _tags(payload: NotePayload) -> set[str]:
    """Frontmatter tags, casefolded — via the shared coercion, never a
    hand-rolled re-parse (08 §B9)."""
    return {str(tag).strip().casefold() for tag in _field_list(payload, "tags") if str(tag).strip()}


def _is_no_ai(payload: NotePayload) -> bool:
    return frontmatter.is_no_ai(_document(payload))


def _slugify(text: str, limit: int) -> str:
    safe = re.sub(r"[^\w\s-]", "", str(text)).strip()[:limit]
    safe = re.sub(r"\s+", "-", safe).lower()
    return safe or "untitled"


def _vault_relative(path: Path, root: Path) -> str:
    """Vault-relative POSIX path, tolerating symlinked scan dirs (08 §B18:
    ``relative_to`` raised ValueError and killed the run)."""
    for base, target in ((root, path), (root.resolve(), path.resolve())):
        try:
            return target.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def _excerpt(value: str, limit: int = 400) -> str:
    collapsed = " ".join((value or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _read_fields(path: Path) -> dict[str, Any]:
    """Frontmatter fields of an existing output file; {} when unreadable."""
    try:
        doc = frontmatter.load_file(path)
    except (OSError, ValueError, frontmatter.FrontmatterError):
        LOG.debug("learn: could not read %s while looking for prior output", path, exc_info=True)
        return {}
    return dict(doc.frontmatter.fields) if doc.frontmatter else {}


@register("learn")
class LearnConsumer(Consumer):
    uses_llm = True  # no-ai notes centrally excluded (02)

    #: option name → (default, coercer). Unknown keys fail at construction.
    OPTION_SCHEMA: dict[str, tuple[Any, Callable[[Any, str], Any]]] = {
        "min_content_length": (200, _as_int),
        "max_cards_per_note": (12, _as_int),
        "deck": ("Reading::Articles", _as_str),
        "card_tags": (lambda: ["learn-consumer"], _as_str_list),
        "flashcard_dir": ("resources/flashcards", _as_str),
        "review_dir": ("resources/flashcards/review", _as_str),
        "guidelines_path": ("", _as_str),
        "generation_log_path": ("", _as_str),
        "trigger_tags": (lambda: list(DEFAULT_TRIGGER_TAGS), _as_str_list),
        "triage_threshold": (0.7, _as_float),
        "triage_weights": (lambda: dict(DEFAULT_TRIAGE_WEIGHTS), _as_weights),
        "topic_tags": (lambda: list(DEFAULT_TOPIC_TAGS), _as_str_list),
        "exclude_dirs": (lambda: list(DEFAULT_EXCLUDE_DIRS), _as_str_list),
        "whisper_host": ("", _as_str),
        "yt_dlp_command": (lambda: ["yt-dlp"], _as_str_list),
        "llm_timeout_seconds": (60.0, _as_float),
        "transcript_timeout_seconds": (30.0, _as_float),
        "whisper_timeout_seconds": (120.0, _as_float),
        "max_prompt_chars": (8000, _as_int),
    }

    def __init__(self, config: ConsumerConfig) -> None:
        super().__init__(config)
        opts = _coerce_options(config, self.OPTION_SCHEMA)

        self.min_content_length: int = opts["min_content_length"]
        self.max_cards: int = opts["max_cards_per_note"]
        self.deck: str = opts["deck"]
        self.card_tags: list[str] = opts["card_tags"]
        self.flashcard_dir: str = opts["flashcard_dir"].strip("/")
        self.review_dir: str = opts["review_dir"].strip("/")
        self.guidelines_path: str = (
            opts["guidelines_path"].strip("/")
            or f"{self.flashcard_dir}/card-generation-guidelines.md"
        )
        self.generation_log_path: str = (
            opts["generation_log_path"].strip("/") or f"{self.flashcard_dir}/generation-log.md"
        )
        self.trigger_tags: set[str] = {t.strip().casefold() for t in opts["trigger_tags"]}
        self.triage_threshold: float = opts["triage_threshold"]
        self.triage_weights: dict[str, float] = opts["triage_weights"]
        self.topic_tags: set[str] = {t.strip().casefold() for t in opts["topic_tags"]}
        self.exclude_dirs: tuple[str, ...] = tuple(
            d.strip("/") for d in opts["exclude_dirs"] if d.strip("/")
        )
        self.whisper_host: str = opts["whisper_host"].rstrip("/")
        self.yt_dlp_command: list[str] = opts["yt_dlp_command"]
        self.llm_timeout_seconds: float = opts["llm_timeout_seconds"]
        self.transcript_timeout_seconds: float = opts["transcript_timeout_seconds"]
        self.whisper_timeout_seconds: float = opts["whisper_timeout_seconds"]
        self.max_prompt_chars: int = opts["max_prompt_chars"]

    # --- trigger ----------------------------------------------------------

    def should_process(self, payload: NotePayload) -> bool:
        if payload.path.suffix != ".md":
            return False
        if self._in_excluded_tree(payload.path):
            return False
        content = payload.content or ""
        if len(content.strip()) < self.min_content_length:
            return False
        if self.trigger_tags & _tags(payload):
            return True
        score = self.triage_score(payload)
        passed = score >= self.triage_threshold
        # 06 §3.2: log every candidate's score so the threshold is tunable
        # with evidence rather than by feel.
        LOG.debug(
            "learn triage %s (score=%.3f, threshold=%.2f): %s",
            "passed" if passed else "rejected",
            score,
            self.triage_threshold,
            payload.path,
        )
        return passed

    def _in_excluded_tree(self, path: Path) -> bool:
        posix = path.as_posix()
        return any(f"/{tree}/" in posix for tree in self.exclude_dirs)

    # --- steps (unit-test targets) ---------------------------------------

    def triage_score(self, payload: NotePayload) -> float:
        """0.4·novelty + 0.4·relevance + 0.2·quality, heuristics as
        implemented (06 §3.2); weights + topic-tag set exposed in config;
        each candidate's score logged at DEBUG."""
        content = payload.content or ""
        text_len = len(content.strip())

        quality = 0.0
        if text_len >= 500:
            quality += 0.4
        elif text_len >= 200:
            quality += 0.2
        if _NUMBERS_RE.search(content):
            quality += 0.3
        if _CITATION_RE.search(content):
            quality += 0.3
        quality = min(quality, 1.0)

        tag_set = _tags(payload)
        relevance = min(len(tag_set & self.topic_tags) * 0.3, 1.0)
        sources = [str(s).strip() for s in _field_list(payload, "sources") if str(s).strip()]
        if sources and not (len(sources) == 1 and sources[0].casefold() == "me"):
            relevance = min(relevance + 0.3, 1.0)

        novelty = 0.5
        if text_len > 1000 and quality > 0.5:
            novelty = 0.7

        weights = self.triage_weights
        return (
            novelty * weights["novelty"]
            + relevance * weights["relevance"]
            + quality * weights["quality"]
        )

    def detect_modality(self, payload: NotePayload) -> str:
        """Ordered detection: youtube → audio → screenshot → article(clip)
        → quote → thought → article(len) → note (06 §3.2)."""
        content = payload.content or ""
        modalities = {str(m).strip().casefold() for m in _field_list(payload, "modalities")}

        if _YOUTUBE_RE.search(content):
            return "youtube"
        if {"audio", "system-audio"} & modalities:
            return "audio"
        if "screenshot" in modalities:
            return "screenshot"
        if "clipboard" in modalities and len(content) > 500:
            return "article"

        stripped = content.strip()
        if stripped.startswith(">") or stripped.startswith('"'):
            return "quote"
        if len(stripped) < 200:
            return "thought"
        if len(stripped) > 1000:
            return "article"
        return "note"

    def normalize_content(self, payload: NotePayload, modality: str, ctx: RunContext) -> str:
        """youtube → yt-dlp auto-subs (30 s timeout); audio → first
        media/*.wav|mp3 POSTed to Whisper /v1/audio/transcriptions (120 s)
        (06 §3.2). All subprocess/HTTP: timeouts + errors="replace"."""
        content = payload.content or ""

        if modality == "youtube":
            video_id = _extract_youtube_id(content)
            if not video_id:
                return content
            transcript = self._fetch_youtube_transcript(video_id)
            if transcript:
                return f"YouTube video transcript:\n{transcript}\n\nOriginal notes:\n{content}"
            LOG.warning("learn: no YouTube transcript for %s (%s)", payload.path, video_id)
            return content

        if modality == "audio":
            audio = _first_audio_file(payload.path.parent / "media")
            if audio is None:
                LOG.warning("learn: audio note %s has no media/*.wav|mp3", payload.path)
                return content
            transcript = self._transcribe_audio(audio)
            if transcript:
                return f"Audio transcript:\n{transcript}\n\nOriginal notes:\n{content}"
            return content

        return content

    def build_prompts(
        self,
        payload: NotePayload,
        modality: str,
        content: str,
        ctx: RunContext | None = None,
    ) -> tuple[str, str]:
        """(system, user): the seven-tier card pedagogy + curiosity-framing
        rule + 10 quality rules; self-improving ``## Learned Rules`` from
        card-generation-guidelines.md when present; user prompt with
        ``Generate 3-{max_cards} cards…`` + content[:8000] (06 §3.2).

        ``ctx`` is optional so the prompt builder stays unit-testable
        without a vault; it is only needed to read the guidelines file.
        """
        # NB: the local is `system_prompt`, not `system` — the repo-hygiene
        # gate greps executed NAMES for `system`/`popen`/`subprocess` to catch
        # `os.system` (08 §A28), and a bare `system` local is a false hit.
        system_prompt = SYSTEM_PROMPT
        learned = self._learned_rules(ctx) if ctx is not None else ""
        if learned:
            system_prompt = (
                f"{system_prompt}\n\nADDITIONAL RULES FROM PAST FEEDBACK:\n{learned}"
            )

        title = self._title(payload)
        sources = [str(s) for s in _field_list(payload, "sources")]
        tags = [str(t) for t in _field_list(payload, "tags")]
        instruction = MODALITY_INSTRUCTIONS.get(modality, MODALITY_INSTRUCTIONS["note"])

        user = (
            f'Source: "{title}"\n'
            f"Attribution: {', '.join(sources)}\n"
            f"Existing tags: {', '.join(tags)}\n"
            f"Modality: {modality}\n"
            f"\n{instruction}\n"
            f"\nGenerate 3-{self.max_cards} cards. Always include exactly 1 thesis card.\n"
            f"\nContent:\n{content[: self.max_prompt_chars]}"
        )
        return system_prompt, user

    def write_review_file(
        self,
        payload: NotePayload,
        cards: list[dict[str, Any]],
        ctx: RunContext,
        modality: str = "note",
    ) -> str:
        """``resources/flashcards/review/<YYYY-MM-DD>-<slug>.md`` with the
        specced frontmatter and ``## Card N [tier]`` blocks; deterministic
        slug ⇒ replace on re-run; append to generation-log.md, creating it
        from a template when missing (06 §3.2).

        [Signature notes: the scaffold annotated ``cards`` as ``str`` — the
        only sane payload is the parsed card list, so the annotation is
        widened; ``modality`` is a new trailing default argument because the
        review frontmatter records it. Both reported to the integrator; no
        existing caller is affected.]
        """
        root = Path(ctx.config.vault.root)
        review_dir = root / self.review_dir
        source_rel = _vault_relative(payload.path, root)
        title = self._title(payload)
        stamp = _utc_now().strftime("%Y-%m-%d")

        target, stale = _resolve_output_path(
            review_dir,
            slug=_slugify(title, 50),
            stamp=stamp,
            owns=lambda fields: str(fields.get("source_note") or "") == source_rel,
            discriminator=_discriminator(source_rel),
        )

        fields: dict[str, Any] = {
            "article": title,
            "author": ", ".join(str(s) for s in _field_list(payload, "sources")),
            "source_note": source_rel,
            "source_hash": _content_hash(payload),
            "modality": modality,
            "generated": stamp,
            "status": "review",
            "deck": self.deck,
            "cards_generated": len(cards),
        }
        body = _render_card_body(title, cards)
        text = frontmatter.serialize(
            frontmatter.Document(frontmatter=frontmatter.Frontmatter(fields=fields), body=body)
        )
        fileops.atomic_write(target, text)

        for path in stale:
            try:
                path.unlink()
            except OSError:  # pragma: no cover - permissions/races only
                LOG.warning("learn: could not remove superseded review file %s", path)
            else:
                LOG.info("learn: replaced superseded review file %s", path)

        self._append_generation_log(ctx, title=title, cards=cards, modality=modality, stamp=stamp)
        return str(target)

    # --- orchestration ----------------------------------------------------

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        path = payload.path

        # Defence in depth: the runner denies no-ai notes to every uses_llm
        # consumer (06 §2), and this consumer refuses them too — the vault
        # law in 02 is absolute and a runner bug must not be able to breach
        # it. SKIP (terminal) so it is not retried until the note changes.
        if _is_no_ai(payload):
            LOG.warning("learn: refusing no-ai note %s", path)
            return ConsumerResult(Status.SKIP, "no-ai: true — LLM tooling refuses this note")

        content = payload.content or ""
        if not content.strip():
            return ConsumerResult(Status.SKIP, "empty content")

        root = Path(ctx.config.vault.root)
        if self._already_generated(payload, root):
            return ConsumerResult(Status.SKIP, "already processed (learn-processed)")

        modality = self.detect_modality(payload)
        normalized = self.normalize_content(payload, modality, ctx)

        if ctx.dry_run:
            return ConsumerResult(
                Status.SUCCESS,
                f"would generate cards (modality={modality})",
                {"dry_run": True, "modality": modality},
            )

        if ctx.llm is None:
            return ConsumerResult(
                Status.ERROR,
                "no LLM client available — cards cannot be generated",
            )

        # Checked BEFORE the LLM call, not at the write-back: without the
        # recorded write path the run cannot finish, and burning a generation
        # to discover that wastes the expensive half. ERROR (never an
        # unrecorded write) is the approved translation — ARCHITECTURE
        # "Phase-4 rulings, auto_tagger batch" (f24ee2e): "a consumer that
        # would write the vault with op_context=None emits Status.ERROR
        # rather than performing an unrecorded write […] errors retry, so the
        # run self-heals once the seam lands".
        if ctx.op_context is None:
            return ConsumerResult(
                Status.ERROR,
                "no OperationContext on the RunContext — the learn-processed "
                "write-back would be an UNRECORDED vault write (spec 12 §2)",
            )

        system_prompt, user = self.build_prompts(payload, modality, normalized, ctx)
        try:
            response = ctx.llm.generate(
                user,
                system=system_prompt,
                json_mode=True,
                temperature=0.3,
                max_tokens=2048,
                timeout_seconds=self.llm_timeout_seconds,
            )
        except LLMError as exc:
            # Ollama down / timing out is the EXPECTED failure (08 §B11):
            # an error emission (not checkpointed, retried next run), never
            # a crash that stops the other consumers.
            LOG.warning("learn: LLM call failed for %s: %s", path, exc)
            return ConsumerResult(Status.ERROR, f"LLM call failed: {exc}")

        cards, problem = _parse_cards(response.json, response.text)
        if problem is not None:
            LOG.warning(
                "learn: unusable LLM response for %s (%s); raw output: %s",
                path,
                problem,
                _excerpt(response.text),
            )
            return ConsumerResult(
                Status.ERROR,
                f"{problem}: {_excerpt(response.text, 200)}",
            )
        if not cards:
            return ConsumerResult(Status.SKIP, "LLM generated 0 cards")

        for card in cards:
            existing = card.get("tags", [])
            existing = [existing] if isinstance(existing, str) else list(existing or [])
            card["tags"] = [
                str(tag) for tag in [*existing, *self.card_tags, f"modality::{modality}"]
            ]

        try:
            review_path = self.write_review_file(payload, cards, ctx, modality=modality)
        except OSError as exc:
            LOG.error("learn: could not write review file for %s: %s", path, exc)
            return ConsumerResult(Status.ERROR, f"could not write review file: {exc}")

        try:
            self._mark_processed(payload, ctx)
        except (OSError, OrganizeError) as exc:
            # The cards exist; only the guard write failed. ERROR ⇒ retried,
            # and the retry replaces the same deterministic file, so nothing
            # duplicates. `OrganizeError` covers the whole taxonomy the
            # recorded write path raises for an ADDRESSING failure — a
            # FrontmatterError on an unparseable note, a
            # ConcurrentModificationError when the note changed under us, a
            # NoAiRefusal — none of which may escape `handle` (06 §1).
            LOG.error("learn: could not mark %s as learn-processed: %s", path, exc)
            return ConsumerResult(
                Status.ERROR,
                f"cards written to {Path(review_path).name} but write-back failed: {exc}",
            )

        tier_counts = _tier_counts(cards)
        LOG.info(
            "learn: %d cards (%s) → %s",
            len(cards),
            _tier_summary(tier_counts),
            Path(review_path).name,
        )
        return ConsumerResult(
            Status.SUCCESS,
            f"{len(cards)} cards generated → {Path(review_path).name}",
            {
                "cards_generated": len(cards),
                "modality": modality,
                "review_file": review_path,
                "tier_breakdown": tier_counts,
            },
        )

    # --- internals --------------------------------------------------------

    def _title(self, payload: NotePayload) -> str:
        """First alias, else ``title``, else the filename stem.

        08 §B10: ``aliases[0]`` on a SCALAR alias yields one character —
        ``get_list`` coerces first, so a scalar becomes a one-element list.
        """
        aliases = [str(a).strip() for a in _field_list(payload, "aliases") if str(a).strip()]
        if aliases:
            return aliases[0]
        title = payload.frontmatter.get("title") if payload.frontmatter else None
        if isinstance(title, str) and title.strip():
            return title.strip()
        return payload.path.stem

    def _already_generated(self, payload: NotePayload, root: Path) -> bool:
        """The 06 §3.2 guard, made compatible with the §7 "rerun on an
        edited source regenerates" acceptance test — see the module
        docstring for why the review file, not the note, holds the record.
        """
        status = payload.frontmatter.get("processing_status") if payload.frontmatter else None
        if not (isinstance(status, str) and status.strip() == "learn-processed"):
            return False
        source_rel = _vault_relative(payload.path, root)
        review_dir = root / self.review_dir
        for path in _slug_matches(review_dir, _slugify(self._title(payload), 50)):
            fields = _read_fields(path)
            if str(fields.get("source_note") or "") != source_rel:
                continue
            if str(fields.get("source_hash") or "") == _content_hash(payload):
                return True
        return False

    def _learned_rules(self, ctx: RunContext) -> str:
        """``## Learned Rules`` from card-generation-guidelines.md, when the
        file exists (06 §3.2 "self-improving")."""
        path = Path(ctx.config.vault.root) / self.guidelines_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        match = _LEARNED_RULES_RE.search(text)
        return match.group(1).strip() if match else ""

    def _mark_processed(self, payload: NotePayload, ctx: RunContext) -> None:
        """Write ``processing_status: learn-processed`` back to the source as
        a RECORDED ``meta_edit`` (06 §3.2 / 08 §B8 + spec 12 §2).

        ARCHITECTURE, "Phase-4 rulings, auto_tagger batch" (commit f24ee2e),
        PHASE-5 CHECKLIST item (a), verbatim: "learn consumer's SOURCE-note
        write-back (processing_status: learn-processed) is a meta_edit in the
        doc-12 enum and must migrate to update_frontmatter + op_context
        (new-output files — flashcards/answers — stay store-audited,
        defensible as-is)".

        So this goes through :func:`fileops.update_frontmatter` rather than
        the bare :func:`fileops.atomic_write` it used to use. What that buys,
        each of which the bare write silently skipped: an ActionRecord with
        ``operation: "meta_edit"`` and ``actor: "consumer:learn"``, an
        operations-log line, a backup, the ``check_unmodified`` snapshot guard
        (a note edited in Obsidian between our read and our write is no longer
        overwritten), the ``no-ai`` refusal as defence in depth, and the index
        update. The round-trip guarantee is unchanged — ``update_frontmatter``
        parses and serializes through the same ONE frontmatter module, so
        every other field (known, unknown, Obsidian-added) still survives
        byte-for-byte (08 §A12).

        A merge is not wanted here and none happens: only ``tags`` merges,
        and ``processing_status`` is a scalar that replaces (05 §5). The
        already-correct case is handled by ``update_frontmatter`` itself,
        which writes nothing when the rendered text is unchanged — so the
        old early return is not lost, it moved into the primitive.

        Raises rather than returning a result: the caller already translates
        a failed write-back into ``Status.ERROR`` (the cards exist; only the
        guard write failed, and the retry replaces the same deterministic
        file so nothing duplicates).
        """
        op_context = ctx.op_context
        if op_context is None:  # pragma: no cover - guarded in `handle`
            raise ConsumerError(
                "no OperationContext on the RunContext — the write-back would be "
                "an UNRECORDED vault write (spec 12 §2)",
                hint="run_consumers populates RunContext.op_context from the "
                "composition root; a hand-built context must supply one",
            )
        result = fileops.update_frontmatter(
            op_context, payload.path, {"processing_status": "learn-processed"}
        )
        if not result.ok:
            # WORLD-STATE failure (the error-line rule): already logged and
            # recorded by fileops; surfaced here so `handle` reports it.
            raise ConsumerError(result.error or f"could not update {payload.path}")

    def _append_generation_log(
        self,
        ctx: RunContext,
        *,
        title: str,
        cards: list[dict[str, Any]],
        modality: str,
        stamp: str,
    ) -> None:
        """Append a row, CREATING the log from a template when missing
        (06 §3.2 — today the row is silently dropped instead)."""
        path = Path(ctx.config.vault.root) / self.generation_log_path
        row = (
            f"| {stamp} | {title[:40]} | {len(cards)} | — | — | — | "
            f"{_tier_summary(_tier_counts(cards))} | {modality} | auto-consumer |"
        )
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            text = GENERATION_LOG_TEMPLATE
        except OSError as exc:  # pragma: no cover - permissions only
            LOG.warning("learn: could not read generation log %s: %s", path, exc)
            return

        try:
            fileops.atomic_write(path, _insert_log_row(text, row))
        except OSError as exc:  # pragma: no cover - permissions only
            LOG.warning("learn: could not update generation log %s: %s", path, exc)

    # --- external endpoints (transcription; NOT LLM completions) ----------

    def _fetch_youtube_transcript(self, video_id: str) -> str | None:
        """yt-dlp auto-subs → cleaned transcript (06 §3.2, 30 s timeout).
        Every failure degrades to None; nothing here may raise."""
        with tempfile.TemporaryDirectory() as tmpdir:
            argv = [
                *self.yt_dlp_command,
                "--write-auto-sub",
                "--sub-lang",
                "en",
                "--skip-download",
                "--sub-format",
                "vtt",
                "-o",
                f"{tmpdir}/%(id)s.%(ext)s",
                f"https://www.youtube.com/watch?v={video_id}",
            ]
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.transcript_timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                LOG.warning("learn: yt-dlp failed for %s: %s", video_id, exc)
                return None
            if proc.returncode != 0:
                LOG.warning("learn: yt-dlp exited %d: %s", proc.returncode, _excerpt(proc.stderr))
                return None
            subtitles = sorted(Path(tmpdir).glob("*.vtt"))
            if not subtitles:
                LOG.warning("learn: yt-dlp produced no subtitles for %s", video_id)
                return None
            return _clean_vtt(subtitles[0].read_text(encoding="utf-8", errors="replace"))

    def _transcribe_audio(self, audio: Path) -> str | None:
        """POST the file to Whisper ``/v1/audio/transcriptions`` (06 §3.2,
        120 s timeout). Transcription is not an LLM completion, so it does
        not go through llm.py; every failure degrades to None."""
        if not self.whisper_host:
            LOG.warning(
                "learn: %s needs transcription but consumers.%s.whisper_host is unset",
                audio,
                self.config.name,
            )
            return None
        try:
            body, content_type = _multipart(audio)
        except OSError as exc:
            LOG.warning("learn: could not read %s: %s", audio, exc)
            return None

        url = f"{self.whisper_host}/v1/audio/transcriptions"
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": content_type}, method="POST"
        )
        # Empty ProxyHandler: never let a stray http_proxy redirect vault
        # audio somewhere unexpected (same rule llm.py follows).
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=self.whisper_timeout_seconds) as response:
                text = response.read().decode("utf-8", errors="replace").strip()
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as exc:
            LOG.warning("learn: Whisper transcription failed at %s: %s", url, exc)
            return None
        if not text:
            LOG.warning("learn: Whisper returned an empty transcript for %s", audio)
            return None
        return text


# --- module-level helpers ---------------------------------------------------


def _discriminator(value: str) -> str:
    """Short, stable per-source token used only to break slug collisions."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _content_hash(payload: NotePayload) -> str:
    """sha256 of the source BODY. Frontmatter is excluded on purpose: our
    own ``processing_status`` write-back must not read as a content edit."""
    return hashlib.sha256((payload.content or "").encode("utf-8")).hexdigest()


def _extract_youtube_id(content: str) -> str | None:
    for pattern in _YOUTUBE_ID_RES:
        match = pattern.search(content)
        if match:
            return match.group(1)
    return None


def _first_audio_file(media_dir: Path) -> Path | None:
    try:
        candidates = sorted(
            p for p in media_dir.iterdir() if p.suffix.lower() in {".wav", ".mp3"} and p.is_file()
        )
    except OSError:
        return None
    return candidates[0] if candidates else None


def _multipart(path: Path) -> tuple[bytes, str]:
    """Minimal multipart/form-data body: the audio file plus
    ``response_format=text`` (the two fields the Whisper API needs)."""
    boundary = f"----organize{uuid.uuid4().hex}"
    marker = f"--{boundary}\r\n".encode()
    chunks = [
        marker,
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        path.read_bytes(),
        b"\r\n",
        marker,
        b'Content-Disposition: form-data; name="response_format"\r\n\r\ntext\r\n',
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _clean_vtt(vtt: str) -> str | None:
    """Strip WEBVTT headers, cue timings, positioning and inline tags, and
    drop consecutive duplicate lines (auto-subs repeat every cue)."""
    lines: list[str] = []
    for raw in vtt.split("\n"):
        line = raw.strip()
        if not line or line.startswith("WEBVTT") or "-->" in line:
            continue
        if _VTT_TIMECODE_RE.match(line):
            continue
        line = _HTML_TAG_RE.sub("", line).strip()
        if line and line not in lines[-1:]:
            lines.append(line)
    return " ".join(lines) if lines else None


def _parse_cards(
    parsed: dict[str, Any] | None, text: str
) -> tuple[list[dict[str, Any]], str | None]:
    """(cards, problem). ``problem`` non-None ⇒ malformed output: the caller
    reports ERROR with the raw text logged and writes NOTHING (06 §6)."""
    data = parsed if isinstance(parsed, dict) else extract_json(text)
    if not isinstance(data, dict):
        return [], "LLM response was not a JSON object"
    raw = data.get("cards")
    if raw is None:
        return [], "LLM response has no 'cards' field"
    if not isinstance(raw, list):
        return [], "LLM 'cards' field was not a list"
    cards = [dict(card) for card in raw if isinstance(card, dict)]
    if len(cards) != len(raw):
        LOG.warning("learn: dropped %d non-object card entries", len(raw) - len(cards))
    return cards, None


def _tier_counts(cards: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for card in cards:
        tier = str(card.get("tier") or "unknown")
        counts[tier] = counts.get(tier, 0) + 1
    return counts


def _tier_summary(counts: dict[str, int]) -> str:
    return ", ".join(f"{tier}:{n}" for tier, n in sorted(counts.items()))


def _card_answer(card: dict[str, Any]) -> tuple[str, list[str]]:
    """(model_answer, tags) with the "model put model_answer inside tags"
    LLM mistake un-done, exactly as the live consumer does."""
    raw_tags = card.get("tags", [])
    raw_tags = [raw_tags] if isinstance(raw_tags, str) else list(raw_tags or [])
    model_answer = str(card.get("model_answer") or "")
    tags: list[str] = []
    for tag in raw_tags:
        text = str(tag)
        if text.startswith("model_answer:"):
            if not model_answer:
                model_answer = text[len("model_answer:") :].strip()
            continue
        tags.append(text)
    if not model_answer:
        back = str(card.get("back") or "")
        if back and back != "[WRITE YOUR ANSWER FIRST]":
            model_answer = back
    return model_answer, tags


def _render_card_body(title: str, cards: list[dict[str, Any]]) -> str:
    lines = [
        "",
        f"# Cards: {title}",
        "",
        "Review these cards. Delete bad ones. Edit as needed.",
        "For tier2 cards: write your answer BEFORE looking at the model answer.",
        "When done, change `status: review` to `status: approved`.",
        "",
    ]
    for index, card in enumerate(cards, 1):
        tier = str(card.get("tier") or "tier1_factual")
        model_answer, tags = _card_answer(card)
        lines += [f"## Card {index} [{tier}]", "", f"**Q:** {card.get('front', '')}", ""]
        if tier == "synthesis_drawing":
            lines += [
                "**A:** *[DRAW THIS — sketch the diagram on paper or tablet]*",
                "",
                "<details><summary>What the diagram should show "
                "(click after drawing)</summary>",
                "",
                model_answer,
                "",
                "</details>",
            ]
        elif "tier2" in tier or tier.startswith("synthesis_"):
            lines += [
                "**A:** *[Write your answer here before reading below]*",
                "",
                "<details><summary>Model answer (click after writing yours)</summary>",
                "",
                model_answer,
                "",
                "</details>",
            ]
        else:
            lines.append(f"**A:** {card.get('back', '')}")
        lines += ["", f"Tags: {', '.join(tags)}", ""]
    return "\n".join(lines)


def _insert_log_row(text: str, row: str) -> str:
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.startswith(_LOG_HEADER_PREFIX) and index + 1 < len(lines):
            if lines[index + 1].startswith("|---"):
                lines.insert(index + 2, row)
                return "\n".join(lines)
    joined = text if text.endswith("\n") else text + "\n"
    return joined + GENERATION_LOG_TEMPLATE + row + "\n"


def _slug_matches(directory: Path, slug: str) -> list[Path]:
    """Existing ``<YYYY-MM-DD>-<slug>*.md`` files, oldest name first. The
    trailing wildcard catches the disambiguated form below, so a file this
    consumer wrote is always recognisable as its own on the next run."""
    try:
        return sorted(
            path
            for path in directory.glob(f"*-{slug}*.md")
            if _DATED_PREFIX_RE.match(path.name) and path.is_file()
        )
    except OSError:  # pragma: no cover - unreadable directory
        return []


def _resolve_output_path(
    directory: Path,
    *,
    slug: str,
    stamp: str,
    owns: Callable[[dict[str, Any]], bool],
    discriminator: str,
) -> tuple[Path, list[Path]]:
    """(target, stale): the deterministic output path plus the previously
    generated files for the SAME source that it supersedes.

    Files that merely share the slug but belong to a different source are
    never touched; when one already occupies the target name, this source's
    output moves to ``<stamp>-<slug>-<discriminator>.md``. The discriminator
    is derived from the source, not from a counter, so a rerun lands on the
    same name again — that is what makes "replace, don't accumulate"
    (06 §3.2) hold even in the slug-collision case that produced the
    ``-1,-2,…`` pile-up in the live vault.
    """
    target = directory / f"{stamp}-{slug}.md"
    stale: list[Path] = []
    foreign_at_target = False
    for path in _slug_matches(directory, slug):
        if owns(_read_fields(path)):
            stale.append(path)
        elif path == target:
            foreign_at_target = True
    if foreign_at_target:
        target = directory / f"{stamp}-{slug}-{discriminator}.md"
    return target, [path for path in stale if path != target]

