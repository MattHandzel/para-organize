"""``taskwarrior`` consumer: todo-tagged captures → Taskwarrior tasks
(spec 06 §3.1). [Phase 3 fills in bodies.]

Live defaults-of-record (06 §2): marker_tag="todo",
default_project="Inbox", additional_tags=["para","automation"],
review_tag="not_reviewed", annotation_template="Captured from {id}".

Non-negotiables (the B-series fixes):
- EVERY ``task`` subprocess: explicit timeout + decode with
  ``errors="replace"`` (B1 — the live 3-month outage).
- Constructor pure; ``task export`` / ``task _tags`` fetched lazily once
  per run (B2).
- ``remove_unknown_tags`` whitelists review_tag AND additional_tags (B7),
  logging every dropped tag.
- Backup ``~/.task`` before the first import of a run to
  ``<state>/backups/taskwarrior/<UTC-ts>`` with retention newest N=10 /
  30 days (B6).
- LLM enrichment optional; any LLM failure degrades to an unenriched task,
  never blocks (06 §3.1).
- Writes nothing back to the note (parity).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    register,
)


@dataclass(frozen=True)
class TaskPayload:
    """One ``task import`` JSON object (spec 06 §3.1): description, entry
    (note timestamp/created_date else now), tags, project, annotations
    (template placeholders ``{path} {relative_path} {id} {capture_id}``),
    and the four optional UDA enrichment fields."""

    description: str
    entry: str
    tags: list[str] = field(default_factory=list)
    project: str = "Inbox"
    annotations: list[dict[str, str]] = field(default_factory=list)
    next_action: str | None = None
    effort: float | None = None
    priority_estimate: str | None = None
    utility: int | None = None

    def to_import_json(self) -> dict[str, Any]:
        raise NotImplementedError


@register("taskwarrior")
class TaskwarriorConsumer(Consumer):
    uses_llm = False  # enrichment is optional and guarded separately

    def should_process(self, payload: NotePayload) -> bool:
        """Frontmatter tag == marker_tag, case-insensitive (06 §3.1)."""
        raise NotImplementedError

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        raise NotImplementedError

    # --- steps (unit-test targets; signatures fixed) ---------------------

    def build_description(self, payload: NotePayload) -> str:
        """Body minus headings, one line, whitespace-collapsed, ≤512 chars
        (``[:509] + "..."``); fallback title → filename stem (06 §3.1)."""
        raise NotImplementedError

    def build_tags(self, payload: NotePayload, known_tags: set[str] | None) -> tuple[list[str], str]:
        """(tags, project): normalize (strip, spaces→_, lowercase); drop
        strip_tags + marker; ``project:<x>`` prefix tag → project else
        default_project; add additional_tags + review_tag; apply
        remove_unknown_tags with the B7 whitelist, logging drops."""
        raise NotImplementedError

    def is_duplicate(self, task: TaskPayload, existing_export: list[dict[str, Any]]) -> bool:
        """Dedupe against full export incl. completed on (description,
        project) and (description, sorted-tags, project), case-insensitive."""
        raise NotImplementedError

    def enrich_with_llm(self, task: TaskPayload, payload: NotePayload, ctx: RunContext) -> TaskPayload:
        """Optional enrichment (06 §3.1): Fibonacci IMPORTANCE_GUIDE prompt,
        few-shot from Matt's export, single JSON object out; effort strings
        normalized (1.5h / 30 min / PT1H30M / bare hours); utility snapped
        to the scale. Failure ⇒ return task unchanged."""
        raise NotImplementedError

    def backup_task_data(self, ctx: RunContext) -> None:
        """Pre-import snapshot with retention (B6). Once per run."""
        raise NotImplementedError
