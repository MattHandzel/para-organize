"""``tag_router`` consumer: apply ``auto = true`` routes unattended
(spec 11 §1 "Where routes act" #2). [Phase 4 fills in bodies; the type is
registered now so config validation and the registry are stable.]

Non-auto routes NEVER fire here — they only surface in the UI. All file
effects go through routes.apply_all (single archive after all destinations
succeed) with actor ``route:<name>`` in the ActionRecord (12 §2).
"""

from __future__ import annotations

from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    register,
)


@register("tag_router")
class TagRouterConsumer(Consumer):
    uses_llm = False  # append/move modes are mechanical; integrate-mode
    # routes DO use the LLM — handle() must check no-ai before integrate
    # (12 §1 refusal) even though the consumer itself is not blanket-LLM.

    def should_process(self, payload: NotePayload) -> bool:
        """Any tag matches a route with ``auto = true`` (11 §1)."""
        raise NotImplementedError

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        raise NotImplementedError
