"""``auto_tagger`` consumer: LLM-tag under-tagged captures (spec 11 §2).
[Phase 4 fills in bodies; registered now for registry/config stability.]

Trigger: fewer than ``min_tags`` (default 1) tags, or ``auto_tag:
"pending"``; respects ``no-ai: true``; skips notes already auto-tagged at
the current content hash. Machine tags are written to BOTH ``tags`` and
``auto_tags`` (distinguishable, bulk-removable); sets ``auto_tag: done``.
Prompt inputs: content (truncated at max_chars), the vault tag vocabulary
(top N by frequency from the index), and all route/folder descriptions.
Backend: the shared LLM client, ``backend = "ollama" | "claude-cli"``.
"""

from __future__ import annotations

from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    register,
)


@register("auto_tagger")
class AutoTaggerConsumer(Consumer):
    uses_llm = True

    def should_process(self, payload: NotePayload) -> bool:
        raise NotImplementedError

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        raise NotImplementedError
