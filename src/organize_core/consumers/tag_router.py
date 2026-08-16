"""``tag_router`` consumer: apply ``auto = true`` routes unattended
(spec 11 §1 "Where routes act" #2). [Phase 4 fills in bodies; the type is
registered now so config validation and the registry are stable.]

Non-auto routes NEVER fire here — they only surface in the UI. All file
effects go through routes.apply_all (single archive after all destinations
succeed) with actor ``route:<name>`` in the ActionRecord (12 §2).
"""

from __future__ import annotations

from organize_core.config import ConsumerConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    register,
)
from organize_core.errors import ConfigError


@register("tag_router")
class TagRouterConsumer(Consumer):
    uses_llm = False  # append/move modes are mechanical; integrate-mode
    # routes DO use the LLM — handle() must check no-ai before integrate
    # (12 §1 refusal) even though the consumer itself is not blanket-LLM.
    # Phase 4 must therefore ALSO override `wants_llm()` to return True when
    # any configured auto route is integrate-mode, or `ctx.llm` will be None
    # (base.Consumer.wants_llm defaults to `uses_llm`) and the integrate
    # path will silently degrade — the same wiring flaw that made
    # taskwarrior's `llm_enabled` inert in production.

    #: Registered for registry/config STABILITY, not for use. Config
    #: validation refuses the type by name (06 §2) and the constructor
    #: refuses a second time, so a config that reaches here fails ONCE and
    #: legibly instead of raising NotImplementedError on every scanned note
    #: (7.5k errors + exit 1 + an OnFailure alert every run — 08 §B2/§B14).
    implemented = False

    def __init__(self, config: ConsumerConfig) -> None:
        raise ConfigError(
            f"consumer section [consumers.{config.name}] uses type 'tag_router', "
            "which is registered but not implemented until Phase 4",
            hint="remove the section, or set enabled = false, until the "
            "tag_router consumer ships (spec 11 §1)",
        )

    def should_process(self, payload: NotePayload) -> bool:
        """Any tag matches a route with ``auto = true`` (11 §1)."""
        raise NotImplementedError

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        raise NotImplementedError
