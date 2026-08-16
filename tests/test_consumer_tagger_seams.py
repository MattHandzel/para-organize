"""``auto_tagger`` — the seam and vault-law pins, each with a firing control.

Companion to ``tests/test_consumer_tagger.py`` (same seat). This file is
deliberately SELF-CONTAINED: it shares no builder with its sibling, so a
rewrite of either cannot quietly disarm the other.

Four properties live here because each one passes VACUOUSLY under the
obvious phrasing, and a pin that cannot fail is worse than no pin:

1. **The no-ai refusal is the no-ai guard**, not the tag count. The fixture
   vault's ``no-ai: true`` note carries one tag, so at the default
   ``min_tags = 1`` ``should_process`` returns False whether or not the
   vault-law guard exists at all. These tests set ``min_tags`` high enough
   that the tag-count branch would say YES, and carry a control note that
   proves it does.
2. **auto_tags participate in routing BY CONSTRUCTION** (architect ruling,
   Phase 4). This is the whole reason machine tags go to ``tags`` as well as
   ``auto_tags``; asserting the two fields' contents does not prove that
   ``routes.resolve`` can actually see them, so this asserts through routes.
3. **``op_context.dry_run`` tracks ``RunContext.dry_run``** — the invariant
   the integrator wires once, named in the op_context ruling.
4. **No index ⇒ degraded, not crashed.** The vocabulary falls back to the
   configured candidates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from organize_core import frontmatter, routes
from organize_core.actions import ActionRecorder
from organize_core.config import Config, RouteConfig, VaultConfig
from organize_core.consumers.auto_tagger import AUTO_TAGS_FIELD, AutoTaggerConsumer
from organize_core.consumers.base import RunContext, Status
from organize_core.fileops import OperationContext, OperationLog
from organize_core.index import VaultIndex
from test_consumer_learn_fakes import FakeLLM, consumer_config, payload_for, write_note

ACTOR = "consumer:auto_tagger"

#: High enough that the ``len(tags) < min_tags`` branch would say YES for
#: every note in this file — so a False answer can only come from another
#: branch. Without it the vault-law assertions are unfalsifiable.
TAG_HUNGRY = 99


def tagger(**options: Any) -> AutoTaggerConsumer:
    return AutoTaggerConsumer(consumer_config("auto_tagger", "auto_tagger", **options))


def config_for(vault: Path, *, routes_: list[RouteConfig] | None = None) -> Config:
    return Config(vault=VaultConfig(root=vault), routes=list(routes_ or []))


def op_context(config: Config, state: Path, *, dry_run: bool = False) -> OperationContext:
    index = VaultIndex(config, state / "index.json")
    index.full_reindex()
    return OperationContext(
        config=config,
        index=index,
        oplog=OperationLog(state / "operations.log"),
        recorder=ActionRecorder(state / "actions"),
        backup_dir=Path(config.vault.root) / config.file_ops.backup_dir,
        dry_run=dry_run,
        actor=ACTOR,
    )


def run_context(
    config: Config, state: Path, *, llm: Any = None, dry_run: bool = False
) -> RunContext:
    """``RunContext`` carrying the Phase-4 ``op_context`` seam.

    Attached as an attribute rather than passed to the constructor so this
    file is green both before and after the integrator lands the declared
    field — the consumer reads it with ``getattr(..., None)``, which behaves
    identically once the field exists with its ``None`` default.
    """
    ctx = RunContext(config=config, dry_run=dry_run, llm=llm)
    ctx.op_context = op_context(config, state, dry_run=dry_run)  # type: ignore[attr-defined]
    return ctx


def note_with(vault: Path, rel: str, body: str, **fields: Any) -> Path:
    text = frontmatter.serialize(
        frontmatter.Document(
            frontmatter=frontmatter.Frontmatter(fields=dict(fields)), body="\n" + body + "\n"
        )
    )
    return write_note(vault, rel, text)


def fields_of(path: Path) -> dict[str, Any]:
    doc = frontmatter.load_file(path)
    return dict(doc.frontmatter.fields) if doc.frontmatter else {}


# --- 1. the vault law, with its control ------------------------------------


def test_the_no_ai_guard_is_what_refuses_a_capture_not_the_tag_count(
    fixture_vault: Path,
) -> None:
    """Spec 02. The fixture's no-ai note has one tag, so the DEFAULT
    ``min_tags`` refuses it for the wrong reason; at ``min_tags = 99`` the
    tag-count branch says YES and only the vault law can say no."""
    from conftest import QUIRK_FILES

    protected = payload_for(fixture_vault / QUIRK_FILES["no_ai"])
    assert protected.no_ai is True
    assert len(protected.tags()) < TAG_HUNGRY, "the tag-count branch must want this note"

    assert tagger(min_tags=TAG_HUNGRY).should_process(protected) is False


def test_the_control_an_identical_capture_without_no_ai_is_processed(
    fixture_vault: Path,
) -> None:
    """The firing control for the test above: same shape, same tag count, no
    ``no-ai`` field ⇒ processed. If this ever fails, the assertion above has
    stopped proving anything."""
    control = note_with(
        fixture_vault,
        "capture/raw_capture/not-private.md",
        "Automated tooling may write to this note.",
        id="not-private",
        tags=["journal"],
        processing_status="raw",
    )
    assert tagger(min_tags=TAG_HUNGRY).should_process(payload_for(control)) is True


def test_a_truthy_string_no_ai_also_refuses(fixture_vault: Path) -> None:
    """For a do-not-touch flag, over-matching is the safe error (Phase-3
    ruling on the shared predicate) — pinned from this consumer's door."""
    guarded = note_with(
        fixture_vault,
        "capture/raw_capture/stringly-private.md",
        "Written by hand with a stringly-typed flag.",
        id="stringly",
        tags=["journal"],
        **{"no-ai": "true"},
    )
    assert tagger(min_tags=TAG_HUNGRY).should_process(payload_for(guarded)) is False


def test_handle_refuses_a_no_ai_note_before_the_model_is_asked(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Defence in depth: even called directly — past ``should_process`` and
    past the runner's central ``uses_llm`` denial — the note must not reach
    the model and must not be written."""
    from conftest import QUIRK_FILES

    note = fixture_vault / QUIRK_FILES["no_ai"]
    before = note.read_bytes()
    llm = FakeLLM(responses=[json.dumps({"tags": ["journal", "private"]})])
    ctx = run_context(config_for(fixture_vault), tmp_path / "state", llm=llm)

    result = tagger(min_tags=TAG_HUNGRY).handle(payload_for(note), ctx)

    assert result.status is Status.SKIP
    assert llm.calls == [], "a no-ai note must never reach the model (02)"
    assert note.read_bytes() == before
    assert list(ActionRecorder(tmp_path / "state" / "actions").query(include_dry_run=True)) == []


def test_the_control_the_same_call_on_an_unprotected_note_does_reach_the_model(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Firing control for the test above — otherwise ``llm.calls == []``
    could be true because the consumer never calls a model at all."""
    note = note_with(
        fixture_vault,
        "capture/raw_capture/reachable.md",
        "Automated tooling may write to this note.",
        id="reachable",
        tags=["journal"],
    )
    llm = FakeLLM(responses=[json.dumps({"tags": ["journal", "private"]})])
    ctx = run_context(config_for(fixture_vault), tmp_path / "state", llm=llm)

    result = tagger(min_tags=TAG_HUNGRY).handle(payload_for(note), ctx)

    assert result.status is Status.SUCCESS
    assert len(llm.calls) == 1


# --- 2. participation by construction (the reason for BOTH fields) ---------


def training_route() -> RouteConfig:
    return RouteConfig(
        tags=["workout", "training"],
        destination="areas/health/training-log.md",
        mode="append",
        description="My running/lifting training log.",
    )


def test_machine_tags_land_where_routes_can_actually_see_them(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Architect ruling: "auto_tags participate in routing BY CONSTRUCTION".
    ``routes.resolve`` reads ``tags``; a tagger that wrote only ``auto_tags``
    would be invisible to it and doc 11's pipeline would never fire. Asserted
    THROUGH routes rather than against the frontmatter, because the field
    assertions cannot distinguish the two designs."""
    note = note_with(
        fixture_vault, "capture/raw_capture/squats.md", "Squat session felt heavy.", id="squats"
    )
    config = config_for(fixture_vault, routes_=[training_route()])
    ctx = run_context(
        config,
        tmp_path / "state",
        llm=FakeLLM(responses=[json.dumps({"tags": ["workout"]})]),
    )

    assert tagger().handle(payload_for(note), ctx).status is Status.SUCCESS

    fields = fields_of(note)
    matches = routes.resolve(fields["tags"], config)
    assert [match.route.destination for match in matches] == ["areas/health/training-log.md"]
    # …and the provenance mirror still says the machine put it there, so the
    # ActionRecord's auto_tags_present can carry it (12 §2).
    assert fields[AUTO_TAGS_FIELD] == ["workout"]


def test_the_control_routing_finds_nothing_before_the_tagger_runs(
    fixture_vault: Path,
) -> None:
    """Firing control: the capture is untagged, so the assertion above is
    about what the tagger added and not about a route that matches anything."""
    config = config_for(fixture_vault, routes_=[training_route()])
    assert routes.resolve([], config) == []


def test_the_action_record_carries_the_notes_auto_tags_as_decision_context(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """12 §2 / 11 §2: ``auto_tags_present`` is the training signal for tagger
    quality — "accepting a move with auto-tags present is implicit
    confirmation, recorded as such". A record that stored an empty tuple
    would make that signal unreadable for exactly the notes it is about."""
    state = tmp_path / "state"
    note = note_with(
        fixture_vault, "capture/raw_capture/provenance.md", "Squat session.", id="provenance"
    )
    ctx = run_context(
        config_for(fixture_vault), state, llm=FakeLLM(responses=[json.dumps({"tags": ["workout"]})])
    )
    shared = ctx.op_context

    assert tagger().handle(payload_for(note), ctx).status is Status.SUCCESS

    (record,) = list(ActionRecorder(state / "actions").query(include_dry_run=True))
    assert record.context.auto_tags_present == ("workout",)
    assert record.actor == ACTOR
    # The RUN-scoped context the runner shares between consumers must not
    # have been mutated, or one note's provenance would leak into the next
    # note's record.
    assert shared.auto_tags_present == ()


def test_the_grounding_is_computed_once_per_run_and_never_reused_across_runs(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Both grounding inputs walk every index record; recomputing them per
    note would spend the 09 §4 pipeline budget rebuilding the same lists. The
    cache must still be keyed to the run, or a config change (or a second
    vault, in tests) would be answered from a stale list."""
    config = config_for(fixture_vault)
    consumer = tagger()

    first = run_context(config, tmp_path / "s1")
    grounding_a = consumer._grounding(config, first.op_context)
    assert consumer._grounding(config, first.op_context) is grounding_a  # same run, no rewalk

    write_note(fixture_vault, "resources/fresh.md", "---\ntags:\n- brand-new-tag\n---\nbody\n")
    second = run_context(config, tmp_path / "s2")
    grounding_b = consumer._grounding(config, second.op_context)

    assert grounding_b is not grounding_a
    assert "brand-new-tag" in grounding_b[0]
    assert "brand-new-tag" not in grounding_a[0]


# --- 3. the dry-run invariant the integrator wires once --------------------


def test_the_op_context_dry_run_flag_tracks_the_run_context(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Named in the op_context ruling: one flag, wired once, two carriers. A
    rehearsal that wrote through a live OperationContext would mutate the
    vault while reporting a dry run (09 §5.6)."""
    config = config_for(fixture_vault)
    for dry_run in (False, True):
        ctx = run_context(config, tmp_path / f"state-{dry_run}", dry_run=dry_run)
        assert ctx.op_context is not None
        assert ctx.op_context.dry_run is ctx.dry_run is dry_run


# --- 4. degraded, not crashed ----------------------------------------------


def test_without_an_index_the_vocabulary_is_the_configured_candidates() -> None:
    """A pure-logic unit context has no OperationContext and therefore no
    index. The grounding degrades to the configured candidates rather than
    raising — normalized through the ONE normalizer on the way (09 §2)."""
    assert tagger(extra_vocabulary=["Blog Idea", "blog_idea"]).vocabulary(None) == ["blog-idea"]
    assert tagger().vocabulary(None) == []
