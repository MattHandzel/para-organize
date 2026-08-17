"""``integrate`` PROPOSAL half: prompt, LLM parsing, and every structural
guard of spec 12 §1 (seat ``integrate-engine``, Phase 5).

Fixture vault + tmp-dir ``CorePaths`` + a fake LLM only — never real state,
never ``~/Obsidian/Main`` (09 §1.4). No test in this file reaches the network:
:class:`FakeLLM` is the whole backend.

Testing standard (ARCHITECTURE "Test anti-vacuity standards", recorded at
6464a24 — permanent from Phase 5 on):

1. Refusal-predicate pins assert at a parameter where the OTHER branch would
   fire and ship a FIRING CONTROL beside them (a deletion-guard pin at a
   threshold that also has an accepting case; a no-ai pin whose note is
   otherwise perfectly integrable).
2. Constants are asserted as LITERALS ("integrate", "merge_target",
   "rejected"), with a separate agreement line pinning the module constant to
   the same literal. Asserting the imported constant against the module's own
   output is the check that stayed green while ACTOR flipped to "matt".
3. Diffs and counts are EXACT values. "> 0" is what let the 08-known-issues
   defects survive (09 §3).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES
from organize_core import integrate as integrate_mod
from organize_core.actions import ActionRecord, ActionRecorder
from organize_core.config import (
    Config,
    FileOpsConfig,
    IntegrateConfig,
    LLMConfig,
    VaultConfig,
)
from organize_core.errors import (
    ConfigError,
    IntegrationRejected,
    LLMError,
    NoAiRefusal,
    OperationError,
)
from organize_core.fileops import OperationContext, OperationLog, snapshot_file
from organize_core.integrate import (
    IntegrationProposal,
    apply_unified_diff,
    build_prompt,
    check_guards,
    deleted_line_count,
    integrate_client,
    prompt_hash,
    propose,
    read_document,
)
from organize_core.llm import ClaudeCLIClient, LLMClient, LLMResponse, OllamaClient, extract_json

FIXED_NOW = 1786000000.0

TARGET_REL = QUIRK_FILES["merge_target"]  # projects/blog/ideas.md
CAPTURE_REL = "capture/raw_capture/integrate-me.md"
NO_AI_REL = QUIRK_FILES["no_ai"]

#: The capture body every golden below weaves in, character for character.
CAPTURE_BODY = "Idea: write about how spaced repetition ruined my note-taking, then fixed it."

CAPTURE_TEXT = f"""---
id: integrate-me
tags:
- blog-idea
sources:
- me
processing_status: raw
---
{CAPTURE_BODY}
"""

#: The fixture vault's `projects/blog/ideas.md`, byte for byte (conftest).
TARGET_TEXT = """---
title: Blog ideas
aliases:
- ideas
author: Matt Handzel
tags:
- blog-idea
created_date: '2025-11-02'
---
# Blog ideas

## Inbox
- existing idea one
"""

#: What a good model returns: the capture, verbatim, as one more bullet in the
#: target's own list style. Nothing else in the file moves.
GOOD_CONTENT = """---
title: Blog ideas
aliases:
- ideas
author: Matt Handzel
tags:
- blog-idea
created_date: '2025-11-02'
---
# Blog ideas

## Inbox
- existing idea one
- Idea: write about how spaced repetition ruined my note-taking, then fixed it.
"""

GOOD_RATIONALE = "Appended under the existing Inbox list, matching its bullet style."


# ---------------------------------------------------------------------------
# fakes and builders (shared with tests/test_integrate_apply.py)
# ---------------------------------------------------------------------------


class FakeLLM(LLMClient):
    """The one backend these suites talk to. Records every call so the prompt
    contract can be asserted, and can be told to emit a pre-parsed ``json``
    field or only raw text (both real backends do the former; the text path is
    what ``llm.extract_json`` exists for)."""

    def __init__(
        self,
        *responses: str | Exception,
        backend: str = "claude-cli",
        model: str = "fake-sonnet",
        parse_json: bool = True,
    ) -> None:
        self.responses = list(responses) or [""]
        self.backend = backend
        self.model = model
        self.parse_json = parse_json
        self.prompts: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        self.calls.append(
            {"system": system, "json_mode": json_mode, "temperature": temperature}
        )
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return LLMResponse(
            text=item,
            model=self.model,
            backend=self.backend,
            duration_ms=7,
            json=extract_json(item) if (json_mode and self.parse_json) else None,
        )

    def available(self) -> bool:
        return True


def llm_reply(content: str, rationale: str = GOOD_RATIONALE) -> str:
    return json.dumps({"content": content, "rationale": rationale})


class FakeIndex:
    """Duck-typed VaultIndex, the same shape the fileops/routes suites use."""

    def __init__(self) -> None:
        self.updated: list[Path] = []
        self.removed: list[Path] = []

    def update_file(self, path: Path) -> None:
        self.updated.append(Path(path))

    def remove_file(self, path: Path) -> None:
        self.removed.append(Path(path))

    def stats(self) -> dict[str, int]:
        return {"total": 12, "capture_backlog": 5}


def make_config(vault: Path, *, max_deleted_lines: int = 0, **file_ops: Any) -> Config:
    return Config(
        vault=VaultConfig(root=vault),
        file_ops=FileOpsConfig(**file_ops),
        integrate=IntegrateConfig(max_deleted_lines=max_deleted_lines),
    )


def make_ctx(
    vault: Path,
    state: Path,
    config: Config,
    *,
    actor: str = "claude-integrate",
    dry_run: bool = False,
    on_record: Any = None,
    index: Any = None,
) -> OperationContext:
    return OperationContext(
        config=config,
        index=index if index is not None else FakeIndex(),  # type: ignore[arg-type]
        oplog=OperationLog(state / "operations.log"),
        recorder=ActionRecorder(state / "actions"),
        backup_dir=vault / config.file_ops.backup_dir,
        dry_run=dry_run,
        actor=actor,
        session_id="ses_integrate",
        clock=lambda: FIXED_NOW,
        on_record=on_record,
    )


def write_capture(vault: Path, text: str = CAPTURE_TEXT, rel: str = CAPTURE_REL) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def records_in(state: Path, *, include_dry_run: bool = True) -> list[ActionRecord]:
    """The corpus as written. Dry runs are INCLUDED by default here so a test
    asserting "nothing was recorded" cannot pass because the record was merely
    filtered out of the default view."""
    return list(ActionRecorder(state / "actions").query(include_dry_run=include_dry_run))


def propose_golden(
    vault: Path,
    *,
    content: str = GOOD_CONTENT,
    config: Config | None = None,
    ctx: OperationContext | None = None,
    route: str | None = None,
    description: str | None = None,
    summarize: bool = False,
    llm: FakeLLM | None = None,
) -> tuple[IntegrationProposal, FakeLLM]:
    capture = read_document(write_capture(vault))
    target = read_document(vault / TARGET_REL)
    client = llm if llm is not None else FakeLLM(llm_reply(content))
    proposal = propose(
        capture,
        target,
        config if config is not None else make_config(vault),
        client,
        route=route,
        description=description,
        ctx=ctx,
        summarize=summarize,
        now=FIXED_NOW,
    )
    return proposal, client


# ---------------------------------------------------------------------------
# the prompt (spec 12 §1 inputs + style directive)
# ---------------------------------------------------------------------------


def test_the_fixture_target_is_the_text_these_goldens_assume(fixture_vault: Path) -> None:
    """Anti-vacuity: every literal diff below is computed against THIS text.
    If conftest's `projects/blog/ideas.md` changes, the goldens must be
    re-derived rather than silently re-baselined."""
    assert (fixture_vault / TARGET_REL).read_text(encoding="utf-8") == TARGET_TEXT


def test_the_prompt_carries_both_documents_the_description_and_the_route(
    fixture_vault: Path,
) -> None:
    """Spec 12 §1: "Inputs to the LLM: full target file, capture content +
    frontmatter, the destination's natural-language description (11)"."""
    capture = read_document(write_capture(fixture_vault))
    target = read_document(fixture_vault / TARGET_REL)
    description = "Ideas for the blog — one bullet per idea, no drafts."

    prompt = build_prompt(capture, target, description=description, route="blog")

    assert TARGET_TEXT in prompt, "the FULL target file must be in the prompt"
    assert CAPTURE_TEXT in prompt, "capture content AND frontmatter must be in the prompt"
    assert CAPTURE_BODY in prompt
    assert description in prompt
    assert "ROUTE: blog" in prompt
    assert str(target.path) in prompt


def test_the_prompt_states_the_verbatim_and_target_style_directives(
    fixture_vault: Path,
) -> None:
    """The directive IS the feature (12 §1): Matt's wording preserved, the
    TARGET file's conventions matched. A prompt that stops asking for either
    would still produce plausible diffs — and quietly paraphrase him."""
    capture = read_document(write_capture(fixture_vault))
    target = read_document(fixture_vault / TARGET_REL)

    prompt = build_prompt(capture, target)

    assert "VERBATIM" in prompt
    assert "character for character" in prompt
    assert "may NOT paraphrase" in prompt
    assert "THE TARGET'S STYLE" in prompt
    assert "levels and their capitalization" in prompt
    assert "bullet/numbering characters" in prompt
    assert "ADD, NEVER DESTROY" in prompt
    # And the same three demands are in the shipped template, not just in a
    # string this test happened to build.
    for demand in ("VERBATIM", "THE TARGET'S STYLE", "ADD, NEVER DESTROY"):
        assert demand in integrate_mod.PROMPT_TEMPLATE


def test_the_prompt_omits_the_description_block_when_there_is_none(
    fixture_vault: Path,
) -> None:
    capture = read_document(write_capture(fixture_vault))
    target = read_document(fixture_vault / TARGET_REL)

    prompt = build_prompt(capture, target)

    assert "WHAT THIS DESTINATION IS FOR" not in prompt
    assert "ROUTE:" not in prompt


def test_summarize_is_only_mentioned_when_explicitly_configured(
    fixture_vault: Path,
) -> None:
    capture = read_document(write_capture(fixture_vault))
    target = read_document(fixture_vault / TARGET_REL)

    assert "Summarizing is EXPLICITLY enabled" not in build_prompt(capture, target)
    assert "Summarizing is EXPLICITLY enabled" in build_prompt(
        capture, target, summarize=True
    )


def test_the_prompt_hash_is_the_sha256_of_the_prompt_actually_sent(
    fixture_vault: Path,
) -> None:
    """12 §2 ``llm.prompt_hash``: the corpus must be able to tell two
    proposals apart by the prompt that produced them."""
    proposal, client = propose_golden(fixture_vault)

    sent = client.prompts[0]
    assert proposal.prompt_hash == hashlib.sha256(sent.encode("utf-8")).hexdigest()
    assert proposal.prompt_hash == prompt_hash(sent)
    assert len(proposal.prompt_hash) == 64


def test_propose_asks_for_json_and_sends_the_system_prompt(fixture_vault: Path) -> None:
    _proposal, client = propose_golden(fixture_vault)

    assert client.calls == [
        {"system": integrate_mod.SYSTEM_PROMPT, "json_mode": True, "temperature": 0.3}
    ]


def test_the_integrate_client_is_the_quality_sensitive_backend() -> None:
    """Spec 12 §1: "integrations are the quality-sensitive path, so default
    this one to claude-cli when available" — i.e. ``llm.integrate_backend``,
    NOT ``llm.backend``."""
    config = Config(
        vault=VaultConfig(root=Path("/nonexistent-vault")),
        llm=LLMConfig(
            backend="ollama",
            ollama_host="http://example.invalid:11434",
            ollama_model="fake:tag",
            integrate_backend="claude-cli",
        ),
    )
    assert isinstance(integrate_client(config), ClaudeCLIClient)
    assert config.llm.integrate_backend == "claude-cli"

    flipped = Config(
        vault=config.vault,
        llm=LLMConfig(
            backend="claude-cli",
            ollama_host="http://example.invalid:11434",
            ollama_model="fake:tag",
            integrate_backend="ollama",
        ),
    )
    assert isinstance(integrate_client(flipped), OllamaClient)


def test_the_integrate_client_names_the_missing_key(fixture_vault: Path) -> None:
    config = Config(
        vault=VaultConfig(root=fixture_vault),
        llm=LLMConfig(integrate_backend="ollama"),  # no host/model configured
    )
    with pytest.raises(ConfigError) as excinfo:
        integrate_client(config)
    assert "llm.ollama_host" in str(excinfo.value)


# ---------------------------------------------------------------------------
# the golden proposal
# ---------------------------------------------------------------------------

#: The exact unified diff `propose` must hand the review pane for the golden
#: pair above. Rendered by the ONE diff renderer the action corpus uses, so a
#: record's `targets[].diff` and `llm.proposed_diff` are comparable byte for
#: byte.
#: NOTE the leading space on the blank context line: a unified diff's context
#: marker is a space even for an empty line, and an editor that strips
#: trailing whitespace turns it into "". The applier tolerates both; this
#: golden pins what the RENDERER emits.
GOLDEN_DIFF_TAIL = (
    "@@ -11,3 +11,4 @@\n"
    " \n"
    " ## Inbox\n"
    " - existing idea one\n"
    "+- Idea: write about how spaced repetition ruined my note-taking, then fixed it.\n"
)


def test_propose_returns_a_golden_proposal(fixture_vault: Path) -> None:
    proposal, client = propose_golden(fixture_vault)
    target = fixture_vault / TARGET_REL

    assert proposal.diff == f"--- a/{target}\n+++ b/{target}\n" + GOLDEN_DIFF_TAIL
    assert proposal.rationale == GOOD_RATIONALE
    assert proposal.backend == "claude-cli"
    assert proposal.model == "fake-sonnet"
    assert proposal.capture_path == str(fixture_vault / CAPTURE_REL)
    assert proposal.target_path == str(target)
    assert proposal.summarize is False
    assert proposal.route is None
    assert proposal.proposal_id.startswith("prop_")
    assert len(proposal.proposal_id) == len("prop_") + 26  # ULID (12 §2)
    assert len(client.prompts) == 1


def test_the_proposals_diff_applies_cleanly_to_the_snapshot(fixture_vault: Path) -> None:
    """The STATELESS wire contract carries a diff, not the rendered result —
    so the diff must reconstruct the model's content exactly."""
    proposal, _client = propose_golden(fixture_vault)

    assert apply_unified_diff(TARGET_TEXT, proposal.diff) == GOOD_CONTENT


def test_propose_writes_no_vault_byte(fixture_vault: Path) -> None:
    before = (fixture_vault / TARGET_REL).read_bytes()

    propose_golden(fixture_vault)

    assert (fixture_vault / TARGET_REL).read_bytes() == before


def test_the_snapshot_describes_the_target_at_propose_time(fixture_vault: Path) -> None:
    proposal, _client = propose_golden(fixture_vault)

    live = snapshot_file(fixture_vault / TARGET_REL)
    assert proposal.target_snapshot.path == str(fixture_vault / TARGET_REL)
    assert proposal.target_snapshot.sha256 == live.sha256
    assert proposal.target_snapshot.mtime == live.mtime


def test_a_proposal_round_trips_through_json(fixture_vault: Path) -> None:
    """The client holds the proposal across the review (the core may idle out
    and exit), so the whole object must survive a JSON round trip."""
    proposal, _client = propose_golden(
        fixture_vault, route="blog", description="Blog ideas, one bullet each."
    )

    revived = IntegrationProposal.from_json(json.loads(json.dumps(proposal.to_json())))

    assert revived == proposal


@pytest.mark.parametrize(
    "mangle",
    [
        pytest.param(lambda raw: {k: v for k, v in raw.items() if k != "diff"}, id="no-diff"),
        pytest.param(
            lambda raw: {k: v for k, v in raw.items() if k != "target_snapshot"},
            id="no-snapshot",
        ),
        pytest.param(lambda raw: {**raw, "proposal_id": ""}, id="empty-id"),
        pytest.param(lambda raw: {**raw, "target_snapshot": {"path": "x"}}, id="partial-snapshot"),
        pytest.param(lambda raw: ["not", "an", "object"], id="not-an-object"),
    ],
)
def test_a_malformed_proposal_from_a_client_is_refused(
    fixture_vault: Path, mangle: Any
) -> None:
    proposal, _client = propose_golden(fixture_vault)

    with pytest.raises(OperationError):
        IntegrationProposal.from_json(mangle(proposal.to_json()))


# ---------------------------------------------------------------------------
# LLM output parsing — malformed output is never a partial proposal
# ---------------------------------------------------------------------------


def test_a_reply_wrapped_in_prose_and_fences_still_parses(fixture_vault: Path) -> None:
    """Real models prepend "Sure! Here's the JSON:" and fence the object;
    ``llm.extract_json`` is the shared tolerance and integrate uses it rather
    than growing a second parser."""
    fenced = "Sure! Here it is:\n```json\n" + llm_reply(GOOD_CONTENT) + "\n```\nHope that helps!"
    proposal, _client = propose_golden(
        fixture_vault, llm=FakeLLM(fenced, parse_json=False)
    )

    assert proposal.rationale == GOOD_RATIONALE
    assert apply_unified_diff(TARGET_TEXT, proposal.diff) == GOOD_CONTENT


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        pytest.param("not json at all", "no JSON object", id="prose"),
        pytest.param('["content", "rationale"]', "no JSON object", id="array"),
        pytest.param(json.dumps({"rationale": "did stuff"}), "'content'", id="no-content"),
        pytest.param(json.dumps({"content": GOOD_CONTENT}), "'rationale'", id="no-rationale"),
        pytest.param(
            json.dumps({"content": "   ", "rationale": "x"}), "'content'", id="blank-content"
        ),
        pytest.param(
            json.dumps({"content": ["a", "list"], "rationale": "x"}), "'content'", id="wrong-type"
        ),
    ],
)
def test_malformed_llm_output_is_an_llm_error_never_a_partial_proposal(
    fixture_vault: Path, reply: str, why: str
) -> None:
    before = (fixture_vault / TARGET_REL).read_bytes()

    with pytest.raises(LLMError) as excinfo:
        propose_golden(fixture_vault, llm=FakeLLM(reply, parse_json=False))

    assert why in str(excinfo.value)
    assert (fixture_vault / TARGET_REL).read_bytes() == before


def test_an_alias_key_is_accepted_but_the_contract_key_is_documented_first(
    fixture_vault: Path,
) -> None:
    """Tolerant parse, strict contract: ``new_content`` is accepted because
    models rename the field, but ``content`` is what the prompt asks for and
    what the error message names."""
    assert integrate_mod.CONTENT_KEYS[0] == "content"
    assert integrate_mod.RATIONALE_KEYS[0] == "rationale"

    reply = json.dumps({"new_content": GOOD_CONTENT, "reason": "aliased"})
    proposal, _client = propose_golden(fixture_vault, llm=FakeLLM(reply, parse_json=False))

    assert proposal.rationale == "aliased"


def test_an_llm_failure_propagates_untouched(fixture_vault: Path) -> None:
    """LLM backends are optional and flaky by contract (06 §6): integrate
    surfaces the failure rather than inventing a proposal."""
    boom = LLMError("backend exploded", hint="try later")
    with pytest.raises(LLMError, match="backend exploded"):
        propose_golden(fixture_vault, llm=FakeLLM(boom))


# ---------------------------------------------------------------------------
# THE DELETION GUARD (spec 12 §1)
# ---------------------------------------------------------------------------

#: A model that "tidies" the target: the whole Inbox section is gone, replaced
#: by the capture. Three existing non-whitespace lines vanish
#: (`# Blog ideas`, `## Inbox`, `- existing idea one`).
GUTTING_CONTENT = """---
title: Blog ideas
aliases:
- ideas
author: Matt Handzel
tags:
- blog-idea
created_date: '2025-11-02'
---
Idea: write about how spaced repetition ruined my note-taking, then fixed it.
"""


def test_the_deletion_guard_rejects_a_gutting_proposal_and_names_the_count(
    fixture_vault: Path,
) -> None:
    """Spec 12 §3 acceptance: "an LLM proposal that removes existing lines is
    rejected … target untouched". The count is in the message because "your
    integration was rejected" with no number is unactionable (09 §1.5)."""
    before = (fixture_vault / TARGET_REL).read_bytes()

    with pytest.raises(IntegrationRejected) as excinfo:
        propose_golden(fixture_vault, content=GUTTING_CONTENT)

    message = str(excinfo.value)
    assert "deletes 3 existing non-whitespace line(s)" in message
    assert "the limit is 0" in message
    assert (fixture_vault / TARGET_REL).read_bytes() == before


def test_the_deletion_guard_has_a_firing_control(fixture_vault: Path) -> None:
    """The pin above must fail for the RIGHT reason: the same fake, the same
    capture and the same target produce a clean proposal when the model adds
    instead of destroying."""
    proposal, _client = propose_golden(fixture_vault)
    assert apply_unified_diff(TARGET_TEXT, proposal.diff) == GOOD_CONTENT


def test_the_deletion_threshold_is_the_configured_value(fixture_vault: Path) -> None:
    """Boundary, in BOTH directions — a guard that never accepts and a guard
    that never rejects both pass a one-sided test."""
    one_deleted = GOOD_CONTENT.replace("- existing idea one\n", "")
    config_0 = make_config(fixture_vault, max_deleted_lines=0)
    config_1 = make_config(fixture_vault, max_deleted_lines=1)

    with pytest.raises(IntegrationRejected, match="deletes 1 existing"):
        propose_golden(fixture_vault, content=one_deleted, config=config_0)

    # Same bytes, threshold raised by one: accepted.
    proposal, _client = propose_golden(fixture_vault, content=one_deleted, config=config_1)
    assert apply_unified_diff(TARGET_TEXT, proposal.diff) == one_deleted

    # And one MORE deletion at the same threshold is refused: the comparison
    # is `>` against the configured number, not "any threshold means allow".
    two_deleted = one_deleted.replace("## Inbox\n", "")
    with pytest.raises(IntegrationRejected, match="deletes 2 existing"):
        propose_golden(fixture_vault, content=two_deleted, config=config_1)


def test_the_guard_counts_content_not_diff_minus_lines(fixture_vault: Path) -> None:
    """Spec 12 §1 lets the capture be REPOSITIONED. A target line that moved
    is still in the file, so it was not deleted — a naive count of the diff's
    `-` lines would reject every reorganization. A line whose WORDS changed is
    gone, and counts."""
    before = "alpha\nbravo\ncharlie\n"

    assert deleted_line_count(before, "charlie\nalpha\nbravo\n") == 0  # reordered
    assert deleted_line_count(before, "alpha\n  bravo\ncharlie\n") == 0  # re-indented
    assert deleted_line_count(before, "alpha\nbravo\n") == 1  # dropped
    assert deleted_line_count(before, "alpha\nbravo-ish\ncharlie\n") == 1  # reworded
    assert deleted_line_count(before, "alpha\nbravo\ncharlie\ndelta\n") == 0  # added
    assert deleted_line_count("a\na\n", "a\n") == 1  # multiset, not set
    assert deleted_line_count(before, "\n\nalpha\nbravo\ncharlie\n") == 0  # blank lines free


def test_the_guard_runs_on_the_result_not_on_what_the_model_claims(
    fixture_vault: Path,
) -> None:
    """"guard runs on the DIFF, before any review, not trusting the LLM": a
    reply whose rationale insists nothing was removed is still rejected."""
    reply = json.dumps(
        {
            "content": GUTTING_CONTENT,
            "rationale": "I did not delete anything, I only reorganized the file.",
        }
    )
    with pytest.raises(IntegrationRejected, match="deletes 3 existing"):
        propose_golden(fixture_vault, llm=FakeLLM(reply, parse_json=False))


# ---------------------------------------------------------------------------
# verbatim preservation (spec 12 §1 "don't paraphrase")
# ---------------------------------------------------------------------------

PARAPHRASED_CONTENT = GOOD_CONTENT.replace(
    "- Idea: write about how spaced repetition ruined my note-taking, then fixed it.",
    "- Blog post idea: spaced repetition's impact on note-taking (negative, then positive).",
)


def test_a_paraphrased_capture_is_rejected(fixture_vault: Path) -> None:
    before = (fixture_vault / TARGET_REL).read_bytes()

    with pytest.raises(IntegrationRejected) as excinfo:
        propose_golden(fixture_vault, content=PARAPHRASED_CONTENT)

    assert "does not contain the capture's text verbatim" in str(excinfo.value)
    assert (fixture_vault / TARGET_REL).read_bytes() == before


def test_verbatim_survives_rewrapping_and_indentation(fixture_vault: Path) -> None:
    """FIRING CONTROL for the pin above, at the parameter that matters: the
    words are identical and only WHITESPACE differs, which is the legal
    transformation ("normalized whitespace"). Reject this and integrate can
    never indent a capture under a heading."""
    rewrapped = GOOD_CONTENT.replace(
        "- Idea: write about how spaced repetition ruined my note-taking, then fixed it.",
        "  - Idea: write about how spaced repetition ruined my\n"
        "    note-taking, then fixed it.",
    )
    proposal, _client = propose_golden(fixture_vault, content=rewrapped)

    assert apply_unified_diff(TARGET_TEXT, proposal.diff) == rewrapped


def test_summarize_disables_the_verbatim_guard_only_when_explicitly_configured(
    fixture_vault: Path,
) -> None:
    """Spec 12 §1's escape hatch is EXPLICIT configuration, so the default
    must refuse the very same bytes summarize accepts."""
    with pytest.raises(IntegrationRejected, match="verbatim"):
        propose_golden(fixture_vault, content=PARAPHRASED_CONTENT, summarize=False)

    proposal, _client = propose_golden(
        fixture_vault, content=PARAPHRASED_CONTENT, summarize=True
    )
    assert proposal.summarize is True


def test_summarize_does_not_disable_the_deletion_guard(fixture_vault: Path) -> None:
    """The escape hatch is scoped to paraphrasing. "Integration adds and
    weaves; it never destroys" has no escape hatch at all."""
    with pytest.raises(IntegrationRejected, match="deletes 3 existing"):
        propose_golden(fixture_vault, content=GUTTING_CONTENT, summarize=True)


# ---------------------------------------------------------------------------
# empty-effect and frontmatter guards
# ---------------------------------------------------------------------------


def test_an_empty_effect_proposal_is_rejected(fixture_vault: Path) -> None:
    """A model that returns the target unchanged has not integrated
    anything — the capture is still homeless, and reporting success would
    lose it."""
    with pytest.raises(IntegrationRejected, match="changes nothing"):
        propose_golden(fixture_vault, content=TARGET_TEXT)


@pytest.mark.parametrize(
    ("content", "needle"),
    [
        pytest.param(
            GOOD_CONTENT.replace("title: Blog ideas", "title: Blog Ideas (2026)"),
            "['title']",
            id="rewrites-title",
        ),
        pytest.param(
            GOOD_CONTENT.replace("author: Matt Handzel\n", ""),
            "['author']",
            id="removes-author",
        ),
        pytest.param(
            GOOD_CONTENT.replace(
                "created_date: '2025-11-02'", "created_date: '2025-11-02'\nstatus: active"
            ),
            "['status']",
            id="invents-a-key",
        ),
        pytest.param(
            GOOD_CONTENT.replace("- blog-idea\ncreated_date", "- blog-idea\n- productivity\ncreated_date"),
            "invents tags",
            id="invents-a-tag",
        ),
        pytest.param(
            GOOD_CONTENT.replace("tags:\n- blog-idea\n", "tags:\n- something-else\n"),
            "drops tags",
            id="drops-a-tag",
        ),
    ],
)
def test_frontmatter_the_capture_does_not_justify_is_rejected(
    fixture_vault: Path, content: str, needle: str
) -> None:
    """Spec 12 §1 asks for a rewrite of the BODY. Metadata Matt curates by
    hand is not the LLM's to edit, and a silently-retitled note is exactly the
    kind of damage nobody notices for months."""
    before = (fixture_vault / TARGET_REL).read_bytes()

    with pytest.raises(IntegrationRejected) as excinfo:
        propose_golden(fixture_vault, content=content)

    assert needle in str(excinfo.value)
    assert (fixture_vault / TARGET_REL).read_bytes() == before


@pytest.mark.parametrize(
    ("content", "why"),
    [
        pytest.param(
            GOOD_CONTENT.replace(
                "created_date: '2025-11-02'",
                "created_date: '2025-11-02'\nlast_edited_date: '2026-08-06'",
            ),
            "last_edited_date is machine bookkeeping",
            id="last-edited-date",
        ),
        pytest.param(
            GOOD_CONTENT.replace("tags:\n- blog-idea\n", "tags:\n- blog-idea\n- Blog-Idea\n"),
            "a re-spelling of a tag the capture carries",
            id="capture-tag-added",
        ),
        pytest.param(
            GOOD_CONTENT.replace(
                "created_date: '2025-11-02'", "created_date: '2025-11-02'\nsources:\n- me"
            ),
            "the capture's own source",
            id="capture-source-added",
        ),
    ],
)
def test_frontmatter_the_capture_does_justify_is_allowed(
    fixture_vault: Path, content: str, why: str
) -> None:
    """FIRING CONTROL for the frontmatter guard: it must let the three
    justified changes through, or integrate can never carry a capture's tags
    onto its destination."""
    proposal, _client = propose_golden(fixture_vault, content=content)

    assert apply_unified_diff(TARGET_TEXT, proposal.diff) == content, why


def test_a_proposal_whose_frontmatter_does_not_parse_is_rejected(
    fixture_vault: Path,
) -> None:
    """05 §9: a mutating operation never writes a file it cannot round-trip."""
    broken = GOOD_CONTENT.replace("title: Blog ideas", "title: [unclosed\n  bad: : :")

    with pytest.raises(IntegrationRejected, match="frontmatter does not parse"):
        propose_golden(fixture_vault, content=broken)


def test_check_guards_is_reachable_on_its_own(fixture_vault: Path) -> None:
    """The guard is a pure function of (before, after, capture, config), which
    is what lets `apply` re-run it at commit against different bytes."""
    capture = read_document(write_capture(fixture_vault))
    config = make_config(fixture_vault)

    check_guards(TARGET_TEXT, GOOD_CONTENT, capture.doc, config=config)  # no raise

    with pytest.raises(IntegrationRejected):
        check_guards(TARGET_TEXT, GUTTING_CONTENT, capture.doc, config=config)


# ---------------------------------------------------------------------------
# no-ai — integrate refuses for EVERY actor (12 §1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["matt", "user", "human", "claude-integrate", "auto-organize"])
def test_a_no_ai_target_refuses_integrate_for_every_actor(
    fixture_vault: Path, tmp_path: Path, actor: str
) -> None:
    """Spec 12 §1: "``no-ai: true`` targets refuse ``integrate`` outright" —
    no actor qualifier. ARCHITECTURE (Phase-4 routes ruling) settles the
    lattice against the interactive exception: move/append may be Matt's
    keystroke, but "Integrate refuses for EVERY actor (12 §1)" because the
    proposal itself is an LLM reading the note.

    The refusal happens BEFORE the prompt is built: with a no-ai note the
    model must never see the text at all.
    """
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, tmp_path / "state", config, actor=actor)
    capture = read_document(write_capture(fixture_vault))
    target = read_document(fixture_vault / NO_AI_REL)
    client = FakeLLM(llm_reply(GOOD_CONTENT))

    with pytest.raises(NoAiRefusal) as excinfo:
        propose(capture, target, config, client, ctx=ctx, now=FIXED_NOW)

    assert "EVERY actor" in str(excinfo.value)
    assert client.prompts == [], "a no-ai note must never reach the model"
    assert records_in(tmp_path / "state") == []


def test_a_no_ai_capture_refuses_integrate_too(fixture_vault: Path) -> None:
    """Integrating COPIES the capture's text into another file, so a no-ai
    capture is as protected as a no-ai target (the same reasoning
    ``fileops.append_to_note`` applies to its source)."""
    capture = read_document(fixture_vault / NO_AI_REL)
    target = read_document(fixture_vault / TARGET_REL)
    client = FakeLLM(llm_reply(GOOD_CONTENT))

    with pytest.raises(NoAiRefusal, match="integrate from"):
        propose(capture, target, make_config(fixture_vault), client, now=FIXED_NOW)

    assert client.prompts == []


def test_the_no_ai_pin_has_a_firing_control(fixture_vault: Path) -> None:
    """Delete the guard and this file integrates: the SAME note, minus the
    `no-ai: true` line, proposes cleanly. Without this control the pin above
    could be passing because the note is unparseable, or missing, or because
    propose refuses everything."""
    raw = (fixture_vault / NO_AI_REL).read_text(encoding="utf-8")
    assert "no-ai: true\n" in raw
    permissive = fixture_vault / "capture/raw_capture/was-private.md"
    permissive.write_text(raw.replace("no-ai: true\n", ""), encoding="utf-8")

    capture = read_document(write_capture(fixture_vault))
    target = read_document(permissive)
    body = "Automated tooling must never write to this note."
    content = raw.replace("no-ai: true\n", "").replace(body, body + "\n\n" + CAPTURE_BODY)
    client = FakeLLM(llm_reply(content, "appended"))

    proposal = propose(capture, target, make_config(fixture_vault), client, now=FIXED_NOW)

    assert len(client.prompts) == 1
    assert proposal.rationale == "appended"


def test_a_target_read_without_a_snapshot_cannot_be_proposed(fixture_vault: Path) -> None:
    """Without a snapshot the commit could not detect a concurrent
    modification, so the proposal would be uncommittable-but-plausible."""
    capture = read_document(write_capture(fixture_vault))
    target = read_document(fixture_vault / TARGET_REL)
    snapshotless = integrate_mod.IntegrationDocument(
        path=target.path, text=target.text, doc=target.doc, snapshot=None
    )

    with pytest.raises(OperationError, match="no snapshot"):
        propose(
            capture,
            snapshotless,
            make_config(fixture_vault),
            FakeLLM(llm_reply(GOOD_CONTENT)),
            now=FIXED_NOW,
        )


# ---------------------------------------------------------------------------
# the rejection RECORD (spec 12 §3: "recorded with verdict: rejected")
# ---------------------------------------------------------------------------


def test_a_structural_rejection_is_recorded_with_the_full_trace(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """12 §2: "a rejection is as much signal as an acceptance". The record is
    the labeled negative example doc 13 trains on."""
    state = tmp_path / "state"
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    before = (fixture_vault / TARGET_REL).read_bytes()

    with pytest.raises(IntegrationRejected):
        propose_golden(fixture_vault, content=GUTTING_CONTENT, config=config, ctx=ctx, route="blog")

    records = records_in(state)
    assert len(records) == 1
    record = records[0]
    # LITERALS (anti-vacuity standard 2) …
    assert record.operation == "integrate"
    assert record.edit_mode == "integrate"
    assert record.actor == "claude-integrate"
    assert record.llm is not None
    assert record.llm.verdict == "rejected"
    assert record.llm.final_diff == ""
    assert record.llm.backend == "claude-cli"
    assert record.llm.model == "fake-sonnet"
    assert "-# Blog ideas" in record.llm.proposed_diff
    assert record.context.route == "blog"
    assert record.context.session_id == "ses_integrate"
    assert len(record.targets) == 1
    target_state = record.targets[0]
    assert target_state.role == "merge_target"
    assert target_state.path == str(fixture_vault / TARGET_REL)
    assert target_state.diff == "", "nothing was written, so the target's diff is empty"
    assert target_state.before_hash == target_state.after_hash
    assert record.capture.body_before == CAPTURE_BODY + "\n"
    # … and the agreement lines that keep the literals honest.
    assert integrate_mod.OPERATION == "integrate"
    assert integrate_mod.EDIT_MODE == "integrate"
    assert integrate_mod.ACTOR == "claude-integrate"
    assert integrate_mod.TARGET_ROLE == "merge_target"
    assert (fixture_vault / TARGET_REL).read_bytes() == before


def test_propose_without_a_ctx_records_nothing_and_still_refuses(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """``ctx`` is a RECORDING seam, not a permission: the guard fires either
    way. (A pure unit-test caller has no corpus to write to.)"""
    state = tmp_path / "state"
    with pytest.raises(IntegrationRejected):
        propose_golden(fixture_vault, content=GUTTING_CONTENT)

    assert not (state / "actions").exists()


def test_a_clean_proposal_records_nothing_at_propose_time(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Proposing is not a decision — the record is written when Matt rules on
    it (accept/edit/reject). Recording here would double-count every
    integration in `actions stats`."""
    state = tmp_path / "state"
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)

    propose_golden(fixture_vault, config=config, ctx=ctx)

    assert records_in(state) == []


def test_the_description_falls_back_to_the_contexts_describe_seam(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """11 §3's destination description reaches the prompt through
    ``OperationContext.describe`` — the callable seam fileops already uses,
    so integrate needs no import of ``routes`` (which imports fileops)."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, tmp_path / "state", config)
    ctx.describe = lambda folder: f"description of {folder.name}"

    proposal, client = propose_golden(fixture_vault, config=config, ctx=ctx)

    assert proposal.description == "description of blog"
    assert "WHAT THIS DESTINATION IS FOR: description of blog" in client.prompts[0]


# ---------------------------------------------------------------------------
# the unified-diff applier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(TARGET_TEXT, GOOD_CONTENT, id="append-a-bullet"),
        pytest.param(TARGET_TEXT, GUTTING_CONTENT, id="delete-a-section"),
        pytest.param("a\nb\nc\n", "a\nb\nc\n\nd\n", id="append-at-eof"),
        pytest.param("a\nb\nc\n", "z\na\nb\nc\n", id="prepend"),
        pytest.param("a\nb\nc\n", "a\nB\nc\n", id="replace-in-the-middle"),
        pytest.param("a\nb\nc\n", "a\n", id="truncate"),
        pytest.param("", "hello\n", id="from-empty"),
        pytest.param("first\nsecond\n", "first\nsecond", id="no-eol-after"),
        pytest.param("x\n" * 40, "x\n" * 20 + "y\n" + "x\n" * 20, id="multi-hunk"),
    ],
)
def test_apply_unified_diff_reverses_the_renderer(before: str, after: str) -> None:
    """The commit path reconstructs the result from the diff the client hands
    back, so applier and renderer must be exact inverses."""
    from organize_core.fileops import _unified_diff

    diff = _unified_diff(before, after, Path("t.md"))
    assert apply_unified_diff(before, diff) == after


def test_apply_unified_diff_tolerates_an_editor_stripped_context_line() -> None:
    """Matt hand-edits the proposal in nvim (`e` in the review pane), and
    editors strip trailing whitespace — so " \\n" context lines arrive as
    "\\n". Refusing those would make the `edited` verdict unusable."""
    before = "alpha\n\nbravo\n"
    diff = "--- a/t.md\n+++ b/t.md\n@@ -1,3 +1,4 @@\n alpha\n\n bravo\n+charlie\n"

    assert apply_unified_diff(before, diff) == "alpha\n\nbravo\ncharlie\n"


def test_apply_unified_diff_refuses_a_diff_that_does_not_match() -> None:
    diff = "--- a/t.md\n+++ b/t.md\n@@ -1,2 +1,3 @@\n alpha\n-nonexistent\n+new\n"

    with pytest.raises(OperationError, match="does not apply"):
        apply_unified_diff("alpha\nbravo\n", diff)


@pytest.mark.parametrize(
    ("diff", "needle"),
    [
        pytest.param("--- a/t.md\n+++ b/t.md\n", "no hunks", id="headers-only"),
        pytest.param("just some prose\n", "outside any hunk", id="prose"),
        pytest.param("@@ nonsense @@\n", "malformed", id="bad-header"),
        pytest.param(
            "@@ -3,1 +3,1 @@\n charlie\n@@ -1,1 +1,1 @@\n alpha\n", "out of order", id="reordered"
        ),
        pytest.param("@@ -9,1 +9,1 @@\n zulu\n", "does not have", id="past-eof"),
        pytest.param(
            "@@ -1,4 +1,4 @@\n alpha\n bravo\n charlie\n delta\n", "more lines", id="too-long"
        ),
    ],
)
def test_apply_unified_diff_refuses_a_broken_diff(diff: str, needle: str) -> None:
    with pytest.raises(OperationError, match=needle):
        apply_unified_diff("alpha\nbravo\ncharlie\n", diff)


def test_apply_unified_diff_refuses_a_mid_diff_line_with_no_newline() -> None:
    """Only the LAST line of a diff may lack a line terminator.

    Python's ``splitlines`` splits on more terminators than ``\\n`` (VT, FF,
    U+2028 …), so a hunk line ending in one of those splits into two diff
    lines while the file it describes may not split the same way. Applying
    that would silently write different bytes — refuse instead.
    """
    diff = "@@ -1,2 +1,3 @@\n alpha\n+new\x0bmore\n bravo\n"

    with pytest.raises(OperationError, match="no newline"):
        apply_unified_diff("alpha\nbravo\n", diff)


def test_a_target_with_no_final_newline_is_refused_loudly(fixture_vault: Path) -> None:
    """The unrepresentable case, refused rather than mangled.

    A unified diff of a file whose last line has no terminator puts a
    terminator-less line in the MIDDLE of the diff text, and nothing can tell
    where that line ended (difflib emits no ``\\ No newline`` marker). Rather
    than guess — which writes wrong bytes into Matt's vault — propose refuses
    and says why. Recorded as a known limitation, not a silent one.
    """
    target = fixture_vault / "projects/blog/no-eol.md"
    target.write_text("---\ntitle: No EOL\n---\n# No EOL\n\n- one", encoding="utf-8")
    capture = read_document(write_capture(fixture_vault))
    content = "---\ntitle: No EOL\n---\n# No EOL\n\n- one\n- " + CAPTURE_BODY + "\n"

    with pytest.raises(OperationError) as excinfo:
        propose(
            capture,
            read_document(target),
            make_config(fixture_vault),
            FakeLLM(llm_reply(content)),
            now=FIXED_NOW,
        )

    assert "cannot be represented as a unified diff" in str(excinfo.value)
    assert target.read_text(encoding="utf-8").endswith("- one")
