"""``deep_research`` consumer: new relationship note → research subprocess
(spec 06 §3.4). [Phase 3 fills in bodies.]

Generic dispatcher, no content matching; routing purely by include_paths
(live: ``areas/relationships``). The exit-code-as-checkpoint contract is
the explicitly stated requirement in person-research-agent.md — preserve
exactly: exit 0 → SUCCESS (never re-dispatch until the note's hash
changes); nonzero/timeout → ERROR, retried next run.
"""

from __future__ import annotations

from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    register,
)


@register("deep_research")
class DeepResearchConsumer(Consumer):
    uses_llm = False  # dispatches a subprocess; the subprocess handles AI.
    # NOTE: still honors no-ai via should_process — the dispatched agent is
    # AI tooling under the vault law (spec 02).

    def should_process(self, payload: NotePayload) -> bool:
        raise NotImplementedError

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        raise NotImplementedError

    # --- steps (unit-test targets) ---------------------------------------

    def build_command(self, payload: NotePayload) -> list[str]:
        """Template with ``{path} {path_quoted} {notes_dir}
        {notes_dir_quoted}`` placeholders, appending the path when none
        present; literal ``{``/``}`` formatted safely (B15)."""
        raise NotImplementedError

    def build_env(self, payload: NotePayload, base_env: dict[str, str]) -> dict[str, str]:
        """Config ``env`` table + NOTES_DIR injection — a caller-supplied
        NOTES_DIR WINS (B15 case-mismatch guard fixed)."""
        raise NotImplementedError
