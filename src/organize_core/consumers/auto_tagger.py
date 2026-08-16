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

from organize_core.config import ConsumerConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    register,
)
from organize_core.errors import ConfigError


@register("auto_tagger")
class AutoTaggerConsumer(Consumer):
    uses_llm = True

    #: Registered for registry/config STABILITY, not for use. Config
    #: validation refuses the type by name (06 §2) and the constructor
    #: refuses a second time, so a config that reaches here fails ONCE and
    #: legibly instead of raising NotImplementedError on every scanned note
    #: (7.5k errors + exit 1 + an OnFailure alert every run — 08 §B2/§B14).
    implemented = False

    def __init__(self, config: ConsumerConfig) -> None:
        raise ConfigError(
            f"consumer section [consumers.{config.name}] uses type 'auto_tagger', "
            "which is registered but not implemented until Phase 4",
            hint="remove the section, or set enabled = false, until the "
            "auto_tagger consumer ships (spec 11 §2)",
        )

    def should_process(self, payload: NotePayload) -> bool:
        raise NotImplementedError

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        raise NotImplementedError
