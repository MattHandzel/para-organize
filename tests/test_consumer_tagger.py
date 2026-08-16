"""``auto_tagger`` consumer suite (spec 11 §2, acceptance items in 11 §4).

Everything external is FAKE and local (spec 09 §1.4): the LLM is either an
in-process ``FakeLLM`` or a real ``OllamaClient`` pointed at a 127.0.0.1
``http.server``. No real vault, no real state directory, no network, no
binaries.

The four properties this file exists to hold down, each with a firing
control so it cannot pass vacuously:

1. machine tags reach BOTH ``tags`` and ``auto_tags`` (11 §2 bullet 3), and
   a tag Matt wrote himself is never claimed as a machine tag;
2. a ``no-ai: true`` note never reaches the endpoint AT ALL (02) — asserted
   against the fake server's request log, not against the vault;
3. a malformed model response writes NOTHING (the note is byte-identical)
   and emits an error, so the note is retried rather than marked done at a
   shape we could not read;
4. re-running is a no-op, deleting an auto tag does not bring it back, and
   editing the body DOES re-tag (11 §4).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from organize_core import fileops, frontmatter
from organize_core.actions import ActionRecorder
from organize_core.config import Config, LLMConfig, RouteConfig, VaultConfig
from organize_core.consumers import auto_tagger as tagger_mod
from organize_core.consumers.auto_tagger import (
    AUTO_TAG_FIELD,
    AUTO_TAG_HASH_FIELD,
    AUTO_TAGS_FIELD,
    STATE_DONE,
    STATE_PENDING,
    AutoTaggerConsumer,
    body_hash,
)
from organize_core.consumers.base import RunContext, Status, get_consumer_types
from organize_core.errors import ConfigError
from organize_core.fileops import OperationContext, OperationLog
from organize_core.index import VaultIndex
from test_consumer_learn_fakes import (
    FakeLLM,
    closed_port_url,
    consumer_config,
    http_endpoint,
    ollama_client,
    payload_for,
    write_note,
)

#: The actor every write this consumer makes must carry (12 §2).
ACTOR = "consumer:auto_tagger"


# --- builders ---------------------------------------------------------------


def make_tagger(**options) -> AutoTaggerConsumer:
    return AutoTaggerConsumer(consumer_config("auto_tagger", "auto_tagger", **options))


def make_config(
    vault: Path,
    *,
    routes: list[RouteConfig] | None = None,
    descriptions: dict[str, str] | None = None,
    tag_normalization: dict[str, str] | None = None,
) -> Config:
    config = Config(
        vault=VaultConfig(root=vault),
        routes=list(routes or []),
        descriptions=dict(descriptions or {}),
        llm=LLMConfig(),
    )
    if tag_normalization:
        # SuggestionsConfig is frozen; rebuild it with the map in place.
        suggestions = config.suggestions
        object.__setattr__(suggestions, "tag_normalization", dict(tag_normalization))
    return config


def make_index(config: Config, state: Path) -> VaultIndex:
    index = VaultIndex(config, state / "index.json")
    index.full_reindex()
    return index


def make_op_ctx(
    config: Config,
    index: VaultIndex,
    state: Path,
    *,
    dry_run: bool = False,
) -> OperationContext:
    """Exactly what a composition root builds (``cli._op_context``), minus
    the ``describe``/``on_record`` callables, which a metadata edit on a
    capture does not consult."""
    return OperationContext(
        config=config,
        index=index,
        oplog=OperationLog(state / "operations.log"),
        recorder=ActionRecorder(state / "actions"),
        backup_dir=Path(config.vault.root) / config.file_ops.backup_dir,
        dry_run=dry_run,
        actor=ACTOR,
    )


def make_run_ctx(
    config: Config,
    llm=None,
    *,
    op_ctx: OperationContext | None = None,
    dry_run: bool = False,
) -> RunContext:
    """A ``RunContext`` carrying the Phase-4 ``op_context`` seam.

    ``RunContext`` is a plain dataclass, so attaching the attribute here is
    exactly what the runner will do once the field lands — and these tests
    keep passing unchanged when it does.
    """
    ctx = RunContext(config=config, dry_run=dry_run, llm=llm)
    ctx.op_context = op_ctx  # type: ignore[attr-defined]
    return ctx


def tags_response(*tags: str) -> str:
    return json.dumps({"tags": list(tags)})


def capture(
    vault: Path,
    body: str = "An idea about improv warmups and creative flow.",
    rel: str = "capture/raw_capture/untagged.md",
    **fields,
) -> Path:
    base: dict[str, object] = {"id": "cap-1", "processing_status": "raw"}
    base.update(fields)
    text = frontmatter.serialize(
        frontmatter.Document(
            frontmatter=frontmatter.Frontmatter(fields=base), body="\n" + body + "\n"
        )
    )
    return write_note(vault, rel, text)


def fields_of(path: Path) -> dict:
    doc = frontmatter.load_file(path)
    return dict(doc.frontmatter.fields) if doc.frontmatter else {}


def records(state: Path) -> list:
    return list(ActionRecorder(state / "actions").query(include_dry_run=True))


# --- construction / registry (06 §1, 08 §B2) -------------------------------


def test_registered_under_its_contract_name() -> None:
    assert get_consumer_types()["auto_tagger"] is AutoTaggerConsumer


def test_the_type_is_a_valid_config_value_now_that_it_is_implemented() -> None:
    """The Phase-4 gate: config validation refuses a registered-but-stubbed
    type by name, so shipping the body is what makes the type configurable."""
    from organize_core.consumers import get_implemented_consumer_types

    assert "auto_tagger" in get_implemented_consumer_types()


def test_uses_llm_is_true_so_the_runner_denies_no_ai_notes() -> None:
    """The central guard (02 / 06 §2) keys on this class flag alone."""
    assert AutoTaggerConsumer.uses_llm is True


def test_wants_llm_follows_uses_llm() -> None:
    assert make_tagger().wants_llm() is True


def test_constructor_is_pure_and_does_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("constructor performed I/O")

    monkeypatch.setattr(Path, "read_text", explode)
    monkeypatch.setattr(Path, "exists", explode)
    monkeypatch.setattr(Path, "glob", explode)
    monkeypatch.setattr("subprocess.run", explode)

    assert make_tagger(min_tags=2).min_tags == 2


def test_spec_defaults() -> None:
    consumer = make_tagger()
    assert consumer.min_tags == 1  # 11 §2 "fewer than min_tags (default 1)"
    assert consumer.max_chars > 0  # 11 §2 "truncated at max_chars"
    assert consumer.vocabulary_size > 0  # 11 §2 "top N by frequency"


def test_unknown_option_fails_naming_the_key() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_tagger(vocab_size=10)
    assert "vocab_size" in str(excinfo.value)
    assert "consumers.auto_tagger" in str(excinfo.value)


def test_options_are_type_checked() -> None:
    for bad in ({"min_tags": "1"}, {"min_tags": 0}, {"extra_vocabulary": [1]}):
        with pytest.raises(ConfigError):
            make_tagger(**bad)


# --- trigger (11 §2) --------------------------------------------------------


def test_an_untagged_capture_is_processed(fixture_vault: Path) -> None:
    note = capture(fixture_vault)
    assert make_tagger().should_process(payload_for(note)) is True


def test_a_tagged_capture_is_not_processed(fixture_vault: Path) -> None:
    note = capture(fixture_vault, tags=["impro"])
    assert make_tagger().should_process(payload_for(note)) is False


def test_min_tags_two_makes_a_single_tag_capture_under_tagged(fixture_vault: Path) -> None:
    note = capture(fixture_vault, tags=["impro"])
    assert make_tagger(min_tags=2).should_process(payload_for(note)) is True


def test_auto_tag_pending_forces_processing_of_an_already_tagged_note(
    fixture_vault: Path,
) -> None:
    """11 §2 names ``auto_tag = "pending"`` as a trigger in its own right —
    it is how Matt re-opts a note in after the done-guard has closed it."""
    note = capture(fixture_vault, tags=["impro", "creativity"], **{AUTO_TAG_FIELD: STATE_PENDING})
    assert make_tagger().should_process(payload_for(note)) is True


def test_a_no_ai_capture_is_never_processed(fixture_vault: Path) -> None:
    """Local half of the vault law (02); the runner's central denial is the
    other half and is asserted separately against the endpoint log.

    The note is deliberately UNTAGGED: the fixture vault's ``no-ai`` exemplar
    carries a tag, so the ordinary under-tagged trigger would decline it on
    its own and the guard could be deleted with this test still green.
    """
    note = capture(
        fixture_vault, "A private thought.", rel="capture/raw_capture/no-ai-untagged.md",
        **{"no-ai": True},
    )
    payload = payload_for(note)
    assert payload.no_ai is True
    assert payload.tags() == [], "the trigger must not be what declines this note"
    assert make_tagger().should_process(payload) is False


def test_the_fixture_vaults_no_ai_exemplar_is_also_refused(fixture_vault: Path) -> None:
    note = fixture_vault / "capture/raw_capture/private-thought.md"
    assert make_tagger().should_process(payload_for(note)) is False


def test_a_non_markdown_file_is_never_processed(fixture_vault: Path) -> None:
    other = fixture_vault / "capture/raw_capture/clipboard-dump.txt"
    assert make_tagger().should_process(payload_for(other)) is False


def test_done_at_the_current_body_hash_is_skipped(fixture_vault: Path) -> None:
    body = "Ran 8k easy, felt good."
    note = capture(
        fixture_vault,
        body,
        tags=["running"],
        **{AUTO_TAG_FIELD: STATE_DONE, AUTO_TAG_HASH_FIELD: body_hash("\n" + body + "\n")},
    )
    payload = payload_for(note)
    assert body_hash(payload.content) == str(payload.frontmatter[AUTO_TAG_HASH_FIELD])
    assert make_tagger().should_process(payload) is False


def test_done_at_a_stale_body_hash_is_reprocessed(fixture_vault: Path) -> None:
    note = capture(
        fixture_vault,
        "Ran 8k easy, felt good.",
        tags=["running"],
        **{AUTO_TAG_FIELD: STATE_DONE, AUTO_TAG_HASH_FIELD: "0" * 16},
    )
    assert make_tagger().should_process(payload_for(note)) is True


def test_done_with_no_recorded_hash_does_not_nag(fixture_vault: Path) -> None:
    note = capture(fixture_vault, tags=["running"], **{AUTO_TAG_FIELD: STATE_DONE})
    assert make_tagger().should_process(payload_for(note)) is False


# --- grounding: vocabulary (11 §2) -----------------------------------------


def tag_vault(root: Path, notes: dict[str, list[str]]) -> Path:
    (root / "resources").mkdir(parents=True, exist_ok=True)
    for rel, tags in notes.items():
        body = "---\ntags:\n" + "".join(f"- {tag}\n" for tag in tags) + "---\nbody\n"
        write_note(root, rel, body)
    return root


def vocabulary_for(
    tmp_path: Path, notes: dict[str, list[str]], **options
) -> tuple[list[str], Config]:
    vault = tag_vault(tmp_path / "v", notes)
    config = make_config(vault)
    index = make_index(config, tmp_path / "state")
    op_ctx = make_op_ctx(config, index, tmp_path / "state")
    return make_tagger(**options).vocabulary(op_ctx), config


def test_vocabulary_is_the_vault_tags_ranked_by_frequency(tmp_path: Path) -> None:
    """11 §2: "the existing vault tag vocabulary (top N by frequency from the
    index)". Frequency, not alphabet, not insertion order."""
    vocab, _ = vocabulary_for(
        tmp_path,
        {
            "resources/a.md": ["zebra", "impro"],
            "resources/b.md": ["zebra", "impro"],
            "resources/c.md": ["zebra"],
            "resources/d.md": ["solo"],
        },
    )
    assert vocab[:3] == ["zebra", "impro", "solo"]


def test_vocabulary_ties_break_alphabetically_so_the_prompt_is_deterministic(
    tmp_path: Path,
) -> None:
    vocab, _ = vocabulary_for(
        tmp_path, {"resources/a.md": ["beta", "alpha"], "resources/b.md": ["beta", "alpha"]}
    )
    assert vocab == ["alpha", "beta"]


def test_vocabulary_is_capped_at_vocabulary_size(tmp_path: Path) -> None:
    notes = {f"resources/n{i}.md": [f"tag-{i:02d}"] for i in range(12)}
    vocab, _ = vocabulary_for(tmp_path, notes, vocabulary_size=4)
    assert len(vocab) == 4


def test_configured_candidates_survive_the_cap(tmp_path: Path) -> None:
    """A configured candidate is an instruction, not a suggestion: it must
    not be crowded out by whatever happens to be frequent this week."""
    notes = {f"resources/n{i}.md": [f"tag-{i:02d}"] for i in range(12)}
    vocab, _ = vocabulary_for(
        tmp_path, notes, vocabulary_size=4, extra_vocabulary=["blog-idea", "workout"]
    )
    assert vocab[-2:] == ["blog-idea", "workout"]
    assert len(vocab) == 6


def test_configured_candidates_go_through_the_shared_normalizer_too(tmp_path: Path) -> None:
    """A candidate spelled loosely in config must reach the model in the same
    shape routing will match on — otherwise the operator's instruction points
    at a tag the vault can never resolve (09 §2)."""
    vocab, _ = vocabulary_for(tmp_path, {}, extra_vocabulary=["Deep Work", "Blog_Idea"])
    assert vocab == [frontmatter.normalize_tag("Deep Work"), frontmatter.normalize_tag("Blog_Idea")]
    assert vocab == ["deep-work", "blog-idea"]


def test_vocabulary_uses_the_one_shared_normalizer(tmp_path: Path) -> None:
    """09 §2: a second normalization rule here would offer the model tags
    spelled differently from the ones routing and scoring later match on.
    Asserted against the shared function's own output, so a hand-rolled
    lowercase/replace that happens to agree today still fails when
    ``normalize_tag`` changes."""
    vocab, _ = vocabulary_for(tmp_path, {"resources/a.md": ["Daily_Notes", "Deep Work"]})
    assert vocab == sorted(
        {frontmatter.normalize_tag("Daily_Notes"), frontmatter.normalize_tag("Deep Work")}
    )
    assert "daily-notes" in vocab


def test_an_empty_vault_yields_an_empty_vocabulary_not_a_crash(tmp_path: Path) -> None:
    vocab, _ = vocabulary_for(tmp_path, {})
    assert vocab == []


# --- grounding: route + folder descriptions (11 §2 / §3) --------------------


def described_config(vault: Path) -> Config:
    return make_config(
        vault,
        routes=[
            RouteConfig(
                tags=["workout", "training"],
                destination="areas/health/training-log.md",
                mode="append",
                description="My running/lifting training log.",
            )
        ],
        descriptions={"projects/blog": "Half-written blog posts and their research."},
    )


def test_descriptions_carry_routes_the_config_table_and_index_folders(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """11 §2 asks for **all** route/folder descriptions; 11 §3 defines three
    storage locations for them, and all three must reach the prompt."""
    config = described_config(fixture_vault)
    index = make_index(config, tmp_path / "state")
    op_ctx = make_op_ctx(config, index, tmp_path / "state")

    lines = "\n".join(make_tagger().descriptions(config, op_ctx))

    assert "areas/health/training-log.md" in lines  # 1. a route (11 §1)
    assert "My running/lifting training log." in lines
    assert "Half-written blog posts" in lines  # 2. central [descriptions]
    assert "Ongoing health practice" in lines  # 3. areas/health/index.md (11 §3)


def test_a_folder_description_is_labelled_with_its_folder_not_its_index_note(
    fixture_vault: Path, tmp_path: Path
) -> None:
    config = make_config(fixture_vault)
    index = make_index(config, tmp_path / "state")
    op_ctx = make_op_ctx(config, index, tmp_path / "state")

    lines = make_tagger().descriptions(config, op_ctx)
    health = [line for line in lines if "Ongoing health practice" in line]

    assert health, lines
    assert "areas/health —" in health[0]
    assert "index.md" not in health[0]


def test_a_route_description_is_not_repeated_by_the_config_table(
    fixture_vault: Path, tmp_path: Path
) -> None:
    config = make_config(
        fixture_vault,
        routes=[
            RouteConfig(
                tags=["blog-idea"],
                destination="projects/blog/ideas.md",
                mode="append",
                description="One-line blog post ideas.",
            )
        ],
        descriptions={"projects/blog/ideas.md": "A duplicate description."},
    )
    index = make_index(config, tmp_path / "state")
    op_ctx = make_op_ctx(config, index, tmp_path / "state")

    lines = make_tagger().descriptions(config, op_ctx)

    assert sum("projects/blog/ideas.md" in line for line in lines) == 1
    assert not any("A duplicate description." in line for line in lines)


def test_the_prompt_carries_all_three_spec_inputs_and_truncates_the_content(
    fixture_vault: Path, tmp_path: Path
) -> None:
    config = described_config(fixture_vault)
    index = make_index(config, tmp_path / "state")
    op_ctx = make_op_ctx(config, index, tmp_path / "state")
    consumer = make_tagger(max_chars=40)
    note = capture(fixture_vault, "x" * 500)

    prompt = consumer.build_prompt(
        payload_for(note), consumer.vocabulary(op_ctx), consumer.descriptions(config, op_ctx)
    )

    assert "impro" in prompt  # 1. the vault vocabulary
    assert "My running/lifting training log." in prompt  # 2. the descriptions
    assert "x" * 40 in prompt  # 3. the (truncated) content
    assert "x" * 41 not in prompt


# --- the golden run ---------------------------------------------------------


def golden_setup(vault: Path, state: Path, response: str = tags_response("impro", "Creative-Flow")):
    config = make_config(vault)
    index = make_index(config, state)
    op_ctx = make_op_ctx(config, index, state)
    llm = FakeLLM(responses=[response])
    return config, make_run_ctx(config, llm, op_ctx=op_ctx), llm


def test_golden_run_writes_both_tags_and_auto_tags_and_records_one_action(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """11 §2 bullet 3 + 11 §4: "untagged capture gains kebab-case ``tags`` +
    matching ``auto_tags``", one logical edit, one ActionRecord (12 §2)."""
    state = tmp_path / "state"
    note = capture(fixture_vault, "Improv warmups and creative flow.", **{"no-ai": False})
    before_body = frontmatter.load_file(note).body
    _, ctx, llm = golden_setup(fixture_vault, state)

    result = make_tagger().handle(payload_for(note), ctx)

    assert result.status is Status.SUCCESS, result.message
    fields = fields_of(note)
    assert fields["tags"] == ["impro", "creative-flow"]
    assert fields[AUTO_TAGS_FIELD] == ["impro", "creative-flow"]
    assert fields[AUTO_TAG_FIELD] == STATE_DONE
    assert fields[AUTO_TAG_HASH_FIELD] == body_hash(before_body)
    # The body is not the tagger's business and must survive byte-for-byte.
    assert frontmatter.load_file(note).body == before_body
    # Unknown/unrelated fields survive the rewrite (08 §A12).
    assert fields["id"] == "cap-1" and fields["processing_status"] == "raw"

    written = records(state)
    assert len(written) == 1
    assert written[0].actor == ACTOR
    assert written[0].operation == "meta_edit"
    assert OperationLog(state / "operations.log").recent(5)
    assert len(llm.calls) == 1
    assert llm.calls[0]["json_mode"] is True


def test_the_model_is_asked_for_json_with_the_configured_timeout(
    fixture_vault: Path, tmp_path: Path
) -> None:
    note = capture(fixture_vault)
    config = make_config(fixture_vault)
    index = make_index(config, tmp_path / "state")
    llm = FakeLLM(responses=[tags_response("impro")])
    ctx = make_run_ctx(config, llm, op_ctx=make_op_ctx(config, index, tmp_path / "state"))

    make_tagger(llm_timeout_seconds=12.5).handle(payload_for(note), ctx)

    assert llm.calls[0]["timeout_seconds"] == 12.5
    assert llm.calls[0]["system"] == tagger_mod.SYSTEM_PROMPT


def test_matts_own_tags_keep_their_order_and_casing_and_are_never_claimed(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """05 §2.6 / 08 §A24 for the merge, and the provenance half of 11 §2:
    ``auto_tags`` names what the MACHINE added, so a tag Matt already wrote
    must not appear there even when the model proposes it too."""
    state = tmp_path / "state"
    note = capture(fixture_vault, tags=["Impro", "Journal"])
    _, ctx, _ = golden_setup(fixture_vault, state, tags_response("impro", "creative-flow"))

    result = make_tagger(min_tags=3).handle(payload_for(note), ctx)

    assert result.status is Status.SUCCESS, result.message
    fields = fields_of(note)
    assert fields["tags"] == ["Impro", "Journal", "creative-flow"]
    assert fields[AUTO_TAGS_FIELD] == ["creative-flow"]


def test_proposals_are_normalized_through_the_one_shared_normalizer(
    fixture_vault: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    note = capture(fixture_vault)
    _, ctx, _ = golden_setup(fixture_vault, state, tags_response("Deep Work", "Deep_Work", "#solo"))

    make_tagger().handle(payload_for(note), ctx)

    fields = fields_of(note)
    assert fields[AUTO_TAGS_FIELD] == [frontmatter.normalize_tag("Deep Work"), "solo"]
    assert fields[AUTO_TAGS_FIELD][0] == "deep-work"


def test_the_action_record_carries_the_auto_tag_provenance(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """12 §2 gives ``ActionContext.auto_tags_present`` for exactly this, and
    the record that CREATED the auto tags is the last one that should leave
    it empty — it is the corpus's only account of which tags were machine
    work. It must also be per-NOTE: the run-scoped OperationContext is shared
    between consumers, so writing onto it would leak into the next record."""
    state = tmp_path / "state"
    note = capture(fixture_vault)
    config = make_config(fixture_vault)
    index = make_index(config, state)
    op_ctx = make_op_ctx(config, index, state)
    llm = FakeLLM(responses=[tags_response("impro", "creative-flow")])

    make_tagger().handle(payload_for(note), make_run_ctx(config, llm, op_ctx=op_ctx))

    written = records(state)
    assert written[0].context.auto_tags_present == ("impro", "creative-flow")
    assert op_ctx.auto_tags_present == (), "the shared run context was mutated"


def test_a_proposal_matt_already_has_still_closes_the_note_out(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The model returning only tags the note already carries is a normal
    outcome, not a failure — but it must still set ``auto_tag: done``, or the
    note is under-tagged forever and burns one inference call every run."""
    state = tmp_path / "state"
    note = capture(fixture_vault, tags=["impro"])
    _, ctx, llm = golden_setup(fixture_vault, state, tags_response("impro"))
    consumer = make_tagger(min_tags=2)

    result = consumer.handle(payload_for(note), ctx)

    assert result.status is Status.SUCCESS
    fields = fields_of(note)
    assert fields["tags"] == ["impro"]
    assert fields[AUTO_TAGS_FIELD] == []
    assert fields[AUTO_TAG_FIELD] == STATE_DONE
    assert consumer.should_process(payload_for(note)) is False
    assert len(llm.calls) == 1


def test_the_grounding_is_derived_once_per_run_not_once_per_note(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both grounding inputs walk every record in the index. Recomputing them
    per note would spend the pipeline's 09 §4 budget rebuilding the same two
    lists ``max_notes_per_run`` times over."""
    state = tmp_path / "state"
    config = make_config(fixture_vault)
    index = make_index(config, state)
    op_ctx = make_op_ctx(config, index, state)
    llm = FakeLLM(responses=[tags_response("impro")])
    ctx = make_run_ctx(config, llm, op_ctx=op_ctx)
    consumer = make_tagger()

    walks = 0
    real_query = VaultIndex.query

    def counting_query(self, criteria):
        nonlocal walks
        walks += 1
        return real_query(self, criteria)

    monkeypatch.setattr(VaultIndex, "query", counting_query)

    for i in range(3):
        note = capture(fixture_vault, f"Capture number {i}.", rel=f"capture/raw_capture/g{i}.md")
        assert consumer.handle(payload_for(note), ctx).status is Status.SUCCESS

    assert walks == 2, "one walk for the vocabulary, one for the descriptions"


def test_max_tags_caps_what_one_call_may_add(fixture_vault: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    note = capture(fixture_vault)
    _, ctx, _ = golden_setup(fixture_vault, state, tags_response("a", "b", "c", "d", "e"))

    make_tagger(max_tags=2).handle(payload_for(note), ctx)

    assert fields_of(note)[AUTO_TAGS_FIELD] == ["a", "b"]


# --- idempotence (11 §4) ----------------------------------------------------


def test_a_rerun_at_the_same_content_is_a_no_op(fixture_vault: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    note = capture(fixture_vault)
    _, ctx, llm = golden_setup(fixture_vault, state)
    consumer = make_tagger()

    consumer.handle(payload_for(note), ctx)
    after_first = note.read_bytes()

    assert consumer.should_process(payload_for(note)) is False
    assert note.read_bytes() == after_first
    assert len(llm.calls) == 1
    assert len(records(state)) == 1


def test_deleting_an_auto_tag_from_tags_does_not_bring_it_back(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """11 §4's last acceptance item. The firing control is the test above:
    with no ``auto_tag: done`` marker the same note IS processed."""
    state = tmp_path / "state"
    note = capture(fixture_vault)
    _, ctx, _ = golden_setup(fixture_vault, state)
    consumer = make_tagger()
    consumer.handle(payload_for(note), ctx)

    doc = frontmatter.load_file(note)
    doc.frontmatter.fields["tags"] = [
        tag for tag in doc.frontmatter.fields["tags"] if tag != "impro"
    ]
    note.write_text(frontmatter.serialize(doc), encoding="utf-8")

    assert consumer.should_process(payload_for(note)) is False


def test_editing_the_body_re_tags_the_note(fixture_vault: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    note = capture(fixture_vault)
    _, ctx, llm = golden_setup(fixture_vault, state)
    consumer = make_tagger()
    consumer.handle(payload_for(note), ctx)

    doc = frontmatter.load_file(note)
    doc.body = doc.body + "\nA second thought about deliberate practice.\n"
    note.write_text(frontmatter.serialize(doc), encoding="utf-8")

    assert consumer.should_process(payload_for(note)) is True

    llm.responses[:] = [tags_response("deliberate-practice")]
    assert consumer.handle(payload_for(note), ctx).status is Status.SUCCESS
    assert len(llm.calls) == 2
    assert "deliberate-practice" in fields_of(note)[AUTO_TAGS_FIELD]
    assert "impro" in fields_of(note)[AUTO_TAGS_FIELD], "prior provenance must survive"


# --- failure modes ----------------------------------------------------------


MALFORMED = [
    pytest.param("not json at all", id="not-json"),
    pytest.param(json.dumps({"labels": ["impro"]}), id="no-tags-key"),
    pytest.param(json.dumps({"tags": "impro"}), id="tags-not-a-list"),
    pytest.param(json.dumps({"tags": [{"name": "impro"}]}), id="tags-not-strings"),
    pytest.param(json.dumps(["impro"]), id="not-an-object"),
]


@pytest.mark.parametrize("response", MALFORMED)
def test_a_malformed_response_is_an_error_and_writes_nothing(
    response: str, fixture_vault: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    note = capture(fixture_vault)
    before = note.read_bytes()
    _, ctx, _ = golden_setup(fixture_vault, state, response)

    result = make_tagger().handle(payload_for(note), ctx)

    assert result.status is Status.ERROR
    assert note.read_bytes() == before, "a malformed answer must not write a partial state"
    assert records(state) == []
    assert not (state / "operations.log").exists()


def test_an_empty_but_well_formed_answer_is_a_terminal_skip(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """SKIP is checkpointed, so "nothing to tag" does not re-ask forever;
    ERROR is not, so a malformed answer does retry. The two must not be
    conflated."""
    state = tmp_path / "state"
    note = capture(fixture_vault)
    before = note.read_bytes()
    _, ctx, _ = golden_setup(fixture_vault, state, tags_response())

    result = make_tagger().handle(payload_for(note), ctx)

    assert result.status is Status.SKIP
    assert note.read_bytes() == before


def test_a_response_of_only_unusable_proposals_is_a_skip(
    fixture_vault: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    note = capture(fixture_vault)
    _, ctx, _ = golden_setup(fixture_vault, state, tags_response("   ", "!!!"))

    assert make_tagger().handle(payload_for(note), ctx).status is Status.SKIP


def test_a_no_ai_note_is_skipped_by_handle_without_calling_the_model(
    fixture_vault: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    note = fixture_vault / "capture/raw_capture/private-thought.md"
    before = note.read_bytes()
    _, ctx, llm = golden_setup(fixture_vault, state)

    result = make_tagger().handle(payload_for(note), ctx)

    assert result.status is Status.SKIP
    assert llm.calls == [], "a no-ai note must never reach the model (02)"
    assert note.read_bytes() == before


def test_a_dry_run_spends_no_inference_and_writes_nothing(
    fixture_vault: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    note = capture(fixture_vault)
    before = note.read_bytes()
    config = make_config(fixture_vault)
    index = make_index(config, state)
    llm = FakeLLM(responses=[tags_response("impro")])
    ctx = make_run_ctx(
        config, llm, op_ctx=make_op_ctx(config, index, state, dry_run=True), dry_run=True
    )

    result = make_tagger().handle(payload_for(note), ctx)

    assert result.status is Status.SUCCESS
    assert result.metadata["dry_run"] is True
    assert llm.calls == []
    assert note.read_bytes() == before


def test_no_llm_client_is_an_error_not_a_crash(fixture_vault: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    note = capture(fixture_vault)
    config = make_config(fixture_vault)
    index = make_index(config, state)
    ctx = make_run_ctx(config, None, op_ctx=make_op_ctx(config, index, state))

    result = make_tagger().handle(payload_for(note), ctx)

    assert result.status is Status.ERROR
    assert note.read_bytes()  # untouched; the assertion below is the real one
    assert records(state) == []


def test_a_missing_operation_context_refuses_rather_than_writing_unrecorded(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Structural decision 1: every mutating path goes through an
    ``OperationContext``. Without one the honest answer is an error the
    runner retries — never a bare write that appears in no log and no
    ActionRecord (12 §2)."""
    note = capture(fixture_vault)
    before = note.read_bytes()
    config = make_config(fixture_vault)
    ctx = make_run_ctx(config, FakeLLM(responses=[tags_response("impro")]), op_ctx=None)

    result = make_tagger().handle(payload_for(note), ctx)

    assert result.status is Status.ERROR
    assert note.read_bytes() == before


def test_a_write_failure_is_an_error_not_an_exception(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06 §1: per-note failures are results, not raises — a raise would take
    the whole consumer down for the rest of the run."""
    state = tmp_path / "state"
    note = capture(fixture_vault)
    _, ctx, _ = golden_setup(fixture_vault, state)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fileops, "update_frontmatter", boom)

    assert make_tagger().handle(payload_for(note), ctx).status is Status.ERROR


# --- against a real (fake) Ollama endpoint ---------------------------------


def test_a_real_client_against_a_fake_ollama_endpoint_tags_the_note(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The same golden path driven through the REAL ``OllamaClient`` and a
    127.0.0.1 ``http.server``, so the JSON envelope, the ``format: json``
    request and the timeout plumbing are all exercised for real."""
    state = tmp_path / "state"
    note = capture(fixture_vault)
    config = make_config(fixture_vault)
    index = make_index(config, state)

    with http_endpoint() as endpoint:
        endpoint.queue_completion(tags_response("impro", "creative-flow"))
        ctx = make_run_ctx(
            config,
            ollama_client(endpoint.base_url),
            op_ctx=make_op_ctx(config, index, state),
        )
        result = make_tagger().handle(payload_for(note), ctx)

        assert result.status is Status.SUCCESS, result.message
        assert [request["path"] for request in endpoint.requests] == ["/api/generate"]
        body = json.loads(endpoint.requests[0]["body"])
        assert body["format"] == "json"
        assert "impro" in body["prompt"], "the vault vocabulary must reach the wire"

    assert fields_of(note)[AUTO_TAGS_FIELD] == ["impro", "creative-flow"]


def test_a_no_ai_note_never_reaches_the_endpoint(fixture_vault: Path, tmp_path: Path) -> None:
    """The strongest form of the 02 assertion: not "the note was not
    written" but "the model never saw it". The firing control is the test
    above, which drives the identical path and DOES reach ``/api/generate``.
    """
    state = tmp_path / "state"
    note = fixture_vault / "capture/raw_capture/private-thought.md"
    config = make_config(fixture_vault)
    index = make_index(config, state)

    with http_endpoint() as endpoint:
        endpoint.queue_completion(tags_response("journal"))
        ctx = make_run_ctx(
            config, ollama_client(endpoint.base_url), op_ctx=make_op_ctx(config, index, state)
        )
        consumer = make_tagger()

        assert consumer.should_process(payload_for(note)) is False
        assert consumer.handle(payload_for(note), ctx).status is Status.SKIP
        assert endpoint.requests == []


def test_an_unreachable_backend_is_an_error_not_a_traceback(
    fixture_vault: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    note = capture(fixture_vault)
    before = note.read_bytes()
    config = make_config(fixture_vault)
    index = make_index(config, state)
    ctx = make_run_ctx(
        config, ollama_client(closed_port_url(), timeout=1.0), op_ctx=make_op_ctx(config, index, state)
    )

    result = make_tagger().handle(payload_for(note), ctx)

    assert result.status is Status.ERROR
    assert note.read_bytes() == before
