"""``integrate`` COMMIT half: the three verdicts, the fresh safety checks and
the complete doc-12 §2 record (seat ``integrate-engine``, Phase 5).

Everything here drives real bytes on a fixture vault with a tmp state dir and
a fake LLM. Assertions are exact file contents, exact record fields and exact
literals — never "something was written somewhere".

The builders and the fake backend live in ``tests/test_integrate.py`` (the
same seat owns both files) so the two halves cannot drift apart on what a
golden proposal is.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest

from organize_core import integrate as integrate_mod
from organize_core import learn as learn_mod
from organize_core.actions import ActionRecord, LLMTrace
from organize_core.errors import (
    ConcurrentModificationError,
    IntegrationRejected,
    NoAiRefusal,
    OperationError,
)
from organize_core.fileops import OperationContext, append_to_note, parse_log_line
from organize_core.index import NoteRecord
from organize_core.integrate import IntegrationProposal, apply, propose, read_document
from test_integrate import (
    CAPTURE_BODY,
    CAPTURE_REL,
    FIXED_NOW,
    GOOD_CONTENT,
    GUTTING_CONTENT,
    TARGET_REL,
    TARGET_TEXT,
    FakeIndex,
    FakeLLM,
    llm_reply,
    make_config,
    make_ctx,
    propose_golden,
    records_in,
    write_capture,
)

#: What Matt's own edit produces when he takes the proposal and rewords HIS
#: OWN framing around the (still verbatim) capture — the `e` path of the
#: doc-12 §1 review gate.
EDITED_CONTENT = GOOD_CONTENT.replace(
    "- Idea: write about how spaced repetition ruined my note-taking, then fixed it.\n",
    "- Idea: write about how spaced repetition ruined my note-taking, then fixed it."
    " (draft this one first)\n",
)


def diff_of(before: str, after: str, path: Path) -> str:
    from organize_core.fileops import _unified_diff

    return _unified_diff(before, after, path)


def setup(
    fixture_vault: Path,
    tmp_path: Path,
    *,
    content: str = GOOD_CONTENT,
    max_deleted_lines: int = 0,
    dry_run: bool = False,
    actor: str = "claude-integrate",
    on_record: Any = None,
    index: Any = None,
    route: str | None = None,
    **file_ops: Any,
) -> tuple[OperationContext, IntegrationProposal, Path, Path]:
    """A ctx + a clean golden proposal + the two paths, ready to commit."""
    config = make_config(fixture_vault, max_deleted_lines=max_deleted_lines, **file_ops)
    ctx = make_ctx(
        fixture_vault,
        tmp_path / "state",
        config,
        actor=actor,
        dry_run=dry_run,
        on_record=on_record,
        index=index,
    )
    proposal, _client = propose_golden(fixture_vault, content=content, config=config, route=route)
    return ctx, proposal, fixture_vault / TARGET_REL, tmp_path / "state"


def oplog_lines(state: Path) -> list[str]:
    path = state / "operations.log"
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# verdict: accepted
# ---------------------------------------------------------------------------


def test_accepted_writes_exactly_the_reviewed_bytes(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """What Matt reviewed is what lands — integrate stamps nothing of its own
    on top of the diff he approved."""
    ctx, proposal, target, _state = setup(fixture_vault, tmp_path)

    result = apply(ctx, target, proposal, "accepted")

    assert target.read_text(encoding="utf-8") == GOOD_CONTENT
    assert result.ok is True
    assert result.operation == "integrate"
    assert result.source == str(fixture_vault / CAPTURE_REL)
    assert result.destination == str(target)
    assert result.dry_run is False
    assert result.details == {
        "verdict": "accepted",
        "proposal_id": proposal.proposal_id,
        "written": True,
        "edit_mode": "integrate",
    }


def test_accepted_records_one_action_with_the_full_trace(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Spec 12 §2: one record per state-changing operation, with the llm block
    carrying proposed AND final diffs."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path, route="blog")

    apply(ctx, target, proposal, "accepted")

    records = records_in(state)
    assert len(records) == 1
    record = records[0]
    assert record.operation == "integrate"
    assert record.edit_mode == "integrate"
    assert record.actor == "claude-integrate"
    assert record.context.route == "blog"
    assert record.context.dry_run is False
    assert record.llm is not None
    assert record.llm.verdict == "accepted"
    assert record.llm.backend == "claude-cli"
    assert record.llm.model == "fake-sonnet"
    assert record.llm.prompt_hash == proposal.prompt_hash
    # accepted means the proposal verbatim: the two diffs are byte-identical,
    # and both equal the target entry's diff (ONE renderer for the corpus).
    assert record.llm.proposed_diff == proposal.diff
    assert record.llm.final_diff == proposal.diff
    assert len(record.targets) == 1
    assert record.targets[0].diff == proposal.diff
    assert record.targets[0].role == "merge_target"
    assert record.targets[0].before_text == TARGET_TEXT
    assert record.capture.body_before == CAPTURE_BODY + "\n"
    # agreement lines for the literals above
    assert integrate_mod.OPERATION == "integrate"
    assert integrate_mod.EDIT_MODE == "integrate"
    assert integrate_mod.TARGET_ROLE == "merge_target"
    assert integrate_mod.ACTOR == "claude-integrate"


def test_accepted_backs_up_the_target_first_and_logs_the_operation(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Spec 12 §1: "In every case the target is backed up first and the write
    is atomic (doc 05)"."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)

    result = apply(ctx, target, proposal, "accepted")

    assert result.backup_path is not None
    backup = Path(result.backup_path)
    assert backup.read_text(encoding="utf-8") == TARGET_TEXT, "the PRE-edit bytes"
    assert backup.parent == fixture_vault / ".backups"

    lines = oplog_lines(state)
    assert len(lines) == 1
    logged = parse_log_line(lines[0])
    assert logged is not None
    assert logged.type == "integrate"
    assert logged.success is True
    assert logged.dry_run is False
    assert logged.src == str(fixture_vault / CAPTURE_REL)
    assert logged.dst == str(target)
    assert logged.backup == str(backup)


def test_accepted_updates_the_index_for_the_target_only(
    fixture_vault: Path, tmp_path: Path
) -> None:
    index = FakeIndex()
    ctx, proposal, target, _state = setup(fixture_vault, tmp_path, index=index)

    apply(ctx, target, proposal, "accepted")

    assert index.updated == [target]
    assert index.removed == [], "integrate never consumes the capture (05 §4 is merge's job)"


def test_a_no_backup_config_still_writes_and_records(
    fixture_vault: Path, tmp_path: Path
) -> None:
    ctx, proposal, target, state = setup(fixture_vault, tmp_path, create_backups=False)

    result = apply(ctx, target, proposal, "accepted")

    assert result.backup_path is None
    assert target.read_text(encoding="utf-8") == GOOD_CONTENT
    assert len(records_in(state)) == 1


# ---------------------------------------------------------------------------
# verdict: edited
# ---------------------------------------------------------------------------


def test_edited_applies_matts_diff_and_records_both(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Spec 12 §2: "``final_diff``: what was actually applied … ≠ proposed
    when Matt hand-edited". That pair IS the labeled edit example doc 13
    trains on, so both halves must survive."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    final = diff_of(TARGET_TEXT, EDITED_CONTENT, target)

    result = apply(ctx, target, proposal, "edited", final)

    assert target.read_text(encoding="utf-8") == EDITED_CONTENT
    assert result.details["verdict"] == "edited"

    record = records_in(state)[0]
    assert record.llm is not None
    assert record.llm.verdict == "edited"
    assert record.llm.proposed_diff == proposal.diff
    assert record.llm.final_diff == final
    assert record.llm.proposed_diff != record.llm.final_diff
    assert record.targets[0].diff == final


def test_edited_needs_the_final_diff(fixture_vault: Path, tmp_path: Path) -> None:
    """Without it the record would claim Matt edited the proposal while
    storing the proposal as the edit — a corpus that lies."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)

    with pytest.raises(OperationError, match="needs the final diff"):
        apply(ctx, target, proposal, "edited")

    assert target.read_text(encoding="utf-8") == TARGET_TEXT
    assert records_in(state) == []


def test_accepted_refuses_a_final_diff_that_is_not_the_proposal(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """"accepted" is a claim about WHICH bytes were reviewed. Applying
    different ones under that verdict poisons the corpus's only ground truth
    about what Matt approves."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    final = diff_of(TARGET_TEXT, EDITED_CONTENT, target)

    with pytest.raises(OperationError, match="applies the proposal unchanged"):
        apply(ctx, target, proposal, "accepted", final)

    assert target.read_text(encoding="utf-8") == TARGET_TEXT
    assert records_in(state) == []


def test_an_unknown_verdict_is_refused(fixture_vault: Path, tmp_path: Path) -> None:
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)

    with pytest.raises(OperationError, match="not an integrate verdict"):
        apply(ctx, target, proposal, "maybe")  # type: ignore[arg-type]

    assert target.read_text(encoding="utf-8") == TARGET_TEXT
    assert records_in(state) == []


# ---------------------------------------------------------------------------
# verdict: rejected
# ---------------------------------------------------------------------------


def test_rejected_records_the_verdict_and_writes_no_vault_byte(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """ARCHITECTURE Phase-5 wire contract: "Rejected verdicts COMMIT (record
    written, no vault write — the negative signal is the point)"."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    before = target.read_bytes()

    result = apply(ctx, target, proposal, "rejected")

    assert target.read_bytes() == before
    assert result.ok is True
    assert result.details == {
        "verdict": "rejected",
        "proposal_id": proposal.proposal_id,
        "written": False,
        "edit_mode": "integrate",
    }

    record = records_in(state)[0]
    assert record.llm is not None
    assert record.llm.verdict == "rejected"
    assert record.llm.proposed_diff == proposal.diff
    assert record.llm.final_diff == ""
    assert record.targets[0].path == str(target)
    assert record.targets[0].diff == ""
    assert record.targets[0].before_hash == record.targets[0].after_hash


def test_rejected_writes_no_operations_log_line(fixture_vault: Path, tmp_path: Path) -> None:
    """The operations log records what happened to the VAULT; an OK line for
    an operation that touched nothing would claim a mutation that never
    happened (the asymmetry `op.skip` already has)."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)

    apply(ctx, target, proposal, "rejected")
    assert oplog_lines(state) == []

    # FIRING CONTROL: the same ctx DOES log when something is written.
    apply(ctx, target, proposal, "accepted")
    assert len(oplog_lines(state)) == 1


def test_rejected_makes_no_backup(fixture_vault: Path, tmp_path: Path) -> None:
    ctx, proposal, target, _state = setup(fixture_vault, tmp_path)

    result = apply(ctx, target, proposal, "rejected")

    assert result.backup_path is None
    assert not (fixture_vault / ".backups").exists()


# ---------------------------------------------------------------------------
# TOCTOU (spec 10 §4 / the Phase-5 wire contract's fresh checks)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["accepted", "edited"])
def test_a_target_edited_between_propose_and_commit_is_refused(
    fixture_vault: Path, tmp_path: Path, verdict: str
) -> None:
    """The vault is Syncthing-synced and the review pane can sit open for
    minutes. Committing a diff against bytes that moved is exactly the
    clobber spec 10 §4 exists to prevent."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    meanwhile = TARGET_TEXT.replace("- existing idea one", "- existing idea one (edited by Matt)")
    target.write_text(meanwhile, encoding="utf-8")

    final = diff_of(meanwhile, meanwhile + "- extra\n", target) if verdict == "edited" else None
    with pytest.raises(ConcurrentModificationError):
        apply(ctx, target, proposal, verdict, final)  # type: ignore[arg-type]

    assert target.read_text(encoding="utf-8") == meanwhile
    assert records_in(state) == []
    assert oplog_lines(state) == []


def test_the_toctou_pin_has_a_firing_control(fixture_vault: Path, tmp_path: Path) -> None:
    """The identical call succeeds when nothing moved — so the refusal above
    is the snapshot check, not a broken proposal."""
    ctx, proposal, target, _state = setup(fixture_vault, tmp_path)

    apply(ctx, target, proposal, "accepted")

    assert target.read_text(encoding="utf-8") == GOOD_CONTENT


def test_a_touched_but_unchanged_target_is_still_refused(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Either signal is enough (fileops' rule): identical bytes with a new
    mtime still mean someone else wrote the file while we held a stale read."""
    ctx, proposal, target, _state = setup(fixture_vault, tmp_path)
    stat = target.stat()
    import os

    os.utime(target, (stat.st_atime, stat.st_mtime + 5))

    with pytest.raises(ConcurrentModificationError):
        apply(ctx, target, proposal, "accepted")


def test_a_rejection_survives_a_moved_target(fixture_vault: Path, tmp_path: Path) -> None:
    """DELIBERATE ASYMMETRY, recorded: the TOCTOU check guards a WRITE, and a
    rejection writes nothing. Refusing to record it because the file moved
    would throw away the negative signal doc 12 exists to collect — the very
    thing the "rejected verdicts COMMIT" ruling is protecting."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    moved = TARGET_TEXT + "- someone else's line\n"
    target.write_text(moved, encoding="utf-8")

    apply(ctx, target, proposal, "rejected")

    assert target.read_text(encoding="utf-8") == moved
    assert len(records_in(state)) == 1


# ---------------------------------------------------------------------------
# the deletion guard, re-run at commit
# ---------------------------------------------------------------------------


def test_an_edited_diff_that_deletes_is_refused_toward_the_manual_path(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """ARCHITECTURE Phase-5 wire contract: the guard re-runs at commit for
    accepted AND edited — "integration adds and weaves; it never destroys"
    binds the MODE, not just the LLM; "a buggy client truncating the buffer
    must not mass-delete under Matt's name". A guard violation on `edited`
    gets a DISTINCT error directing to the manual merge path, which is
    guard-free by design."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    destructive = diff_of(TARGET_TEXT, GUTTING_CONTENT, target)

    with pytest.raises(IntegrationRejected) as excinfo:
        apply(ctx, target, proposal, "edited", destructive)

    message = str(excinfo.value)
    assert "your edited version was refused" in message
    assert "deletes 3 existing non-whitespace line(s)" in message
    assert "organize merge" in (excinfo.value.hint or "")
    assert target.read_text(encoding="utf-8") == TARGET_TEXT
    assert oplog_lines(state) == []

    record = records_in(state)[0]
    assert record.llm is not None
    assert record.llm.verdict == "rejected"
    assert record.llm.final_diff == destructive, "the blocked edit is stored as signal"
    assert record.targets[0].diff == ""


def test_an_edited_diff_within_the_guard_applies(fixture_vault: Path, tmp_path: Path) -> None:
    """FIRING CONTROL: same call shape, a diff that only adds."""
    ctx, proposal, target, _state = setup(fixture_vault, tmp_path)
    final = diff_of(TARGET_TEXT, EDITED_CONTENT, target)

    apply(ctx, target, proposal, "edited", final)

    assert target.read_text(encoding="utf-8") == EDITED_CONTENT


def test_a_tampered_accepted_proposal_is_refused_without_the_manual_hint(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Defence in depth: the guard passed at propose, so a destructive diff
    arriving under `accepted` means the CLIENT changed it. Safety never trusts
    the client (the wire contract says trace fidelity may, safety may not) —
    and the error is the plain guard error, because no human edited anything.
    """
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    tampered = dataclasses.replace(proposal, diff=diff_of(TARGET_TEXT, GUTTING_CONTENT, target))

    with pytest.raises(IntegrationRejected) as excinfo:
        apply(ctx, target, tampered, "accepted")

    assert "deletes 3 existing non-whitespace line(s)" in str(excinfo.value)
    assert "your edited version" not in str(excinfo.value)
    assert target.read_text(encoding="utf-8") == TARGET_TEXT
    assert records_in(state)[0].llm.verdict == "rejected"  # type: ignore[union-attr]


def test_a_final_diff_that_does_not_apply_is_refused(
    fixture_vault: Path, tmp_path: Path
) -> None:
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    nonsense = "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-this line is not in the target\n+something\n"

    with pytest.raises(OperationError, match="does not apply"):
        apply(ctx, target, proposal, "edited", nonsense)

    assert target.read_text(encoding="utf-8") == TARGET_TEXT
    assert records_in(state) == []


# ---------------------------------------------------------------------------
# no-ai at commit — both directions, every actor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["matt", "claude-integrate", "consumer:tag_router"])
def test_a_target_that_became_no_ai_after_propose_refuses_at_commit(
    fixture_vault: Path, tmp_path: Path, actor: str
) -> None:
    """"no-ai refusal at BOTH propose and commit" (Phase-5 wire contract), and
    for EVERY actor (12 §1) — including ``matt``, whose keystroke is enough
    for a move but never for an LLM-authored rewrite."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path, actor=actor)
    target.write_text(TARGET_TEXT.replace("title:", "no-ai: true\ntitle:"), encoding="utf-8")

    with pytest.raises(NoAiRefusal, match="EVERY actor"):
        apply(ctx, target, proposal, "accepted")

    assert "no-ai: true" in target.read_text(encoding="utf-8")
    assert CAPTURE_BODY not in target.read_text(encoding="utf-8")
    assert records_in(state) == []


def test_a_capture_that_became_no_ai_after_propose_refuses_at_commit(
    fixture_vault: Path, tmp_path: Path
) -> None:
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    capture = fixture_vault / CAPTURE_REL
    capture.write_text(
        capture.read_text(encoding="utf-8").replace("id:", "no-ai: true\nid:"), encoding="utf-8"
    )

    with pytest.raises(NoAiRefusal, match="integrate from"):
        apply(ctx, target, proposal, "accepted")

    assert target.read_text(encoding="utf-8") == TARGET_TEXT
    assert records_in(state) == []


def test_the_commit_no_ai_pin_has_a_firing_control(fixture_vault: Path, tmp_path: Path) -> None:
    """Delete the commit-side guard and the two pins above go green on their
    own: this is the same call against the same files WITHOUT the flag."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path, actor="matt")

    apply(ctx, target, proposal, "accepted")

    assert target.read_text(encoding="utf-8") == GOOD_CONTENT
    assert len(records_in(state)) == 1


def test_a_rejected_verdict_also_refuses_a_no_ai_target(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """A rejection reads and records the note's content, so the refusal
    covers it too — recording a no-ai note's body into the corpus is a write
    of that note's content into a machine-readable store."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    target.write_text(TARGET_TEXT.replace("title:", "no-ai: true\ntitle:"), encoding="utf-8")

    with pytest.raises(NoAiRefusal):
        apply(ctx, target, proposal, "rejected")

    assert records_in(state) == []


# ---------------------------------------------------------------------------
# addressing failures (ARCHITECTURE "Where the error line falls")
# ---------------------------------------------------------------------------


def test_committing_a_proposal_against_a_different_target_is_refused(
    fixture_vault: Path, tmp_path: Path
) -> None:
    ctx, proposal, _target, state = setup(fixture_vault, tmp_path)
    other = fixture_vault / "areas/health/index.md"
    before = other.read_bytes()

    with pytest.raises(OperationError, match="but the commit names"):
        apply(ctx, other, proposal, "accepted")

    assert other.read_bytes() == before
    assert records_in(state) == []


def test_a_proposal_whose_snapshot_names_another_file_is_refused(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The snapshot is what the concurrent-modification check verifies; a
    snapshot of a DIFFERENT file would make that check pass vacuously."""
    ctx, proposal, target, _state = setup(fixture_vault, tmp_path)
    from organize_core.fileops import snapshot_file

    swapped = dataclasses.replace(
        proposal, target_snapshot=snapshot_file(fixture_vault / "areas/health/index.md")
    )

    with pytest.raises(OperationError, match="snapshot describes"):
        apply(ctx, target, swapped, "accepted")

    assert target.read_text(encoding="utf-8") == TARGET_TEXT


def test_a_target_outside_the_vault_is_refused(fixture_vault: Path, tmp_path: Path) -> None:
    from organize_core.errors import VaultError

    ctx, proposal, _target, _state = setup(fixture_vault, tmp_path)
    outside = tmp_path / "OUTSIDE.md"
    outside.write_text("not in the vault\n", encoding="utf-8")

    with pytest.raises(VaultError):
        apply(ctx, outside, proposal, "accepted")

    assert outside.read_text(encoding="utf-8") == "not in the vault\n"


def test_a_vanished_target_is_refused(fixture_vault: Path, tmp_path: Path) -> None:
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    target.unlink()

    with pytest.raises(OperationError, match="does not exist"):
        apply(ctx, target, proposal, "accepted")

    assert records_in(state) == []


def test_a_vanished_capture_is_refused(fixture_vault: Path, tmp_path: Path) -> None:
    """The record must name the capture that was integrated; without it the
    corpus entry would be an anonymous edit."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    (fixture_vault / CAPTURE_REL).unlink()

    with pytest.raises(OperationError, match="capture does not exist"):
        apply(ctx, target, proposal, "accepted")

    assert target.read_text(encoding="utf-8") == TARGET_TEXT
    assert records_in(state) == []


# ---------------------------------------------------------------------------
# dry run (09 §5.6 + ARCHITECTURE ruling (a))
# ---------------------------------------------------------------------------


def test_a_dry_run_writes_no_vault_byte_but_is_not_state_silent(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """ARCHITECTURE ruling (a): ``--dry-run`` is vault-write-free but NOT
    state-silent — the intended action goes on paper, marked as a rehearsal
    so no corpus reader mistakes it for a precedent."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path, dry_run=True)

    result = apply(ctx, target, proposal, "accepted")

    assert target.read_text(encoding="utf-8") == TARGET_TEXT
    assert result.dry_run is True
    assert result.details["written"] is False
    assert not (fixture_vault / ".backups").exists()

    logged = parse_log_line(oplog_lines(state)[0])
    assert logged is not None and logged.dry_run is True

    assert records_in(state, include_dry_run=False) == [], (
        "a rehearsal is never a doc-12 precedent, so every corpus reader "
        "excludes it by default"
    )
    dry = records_in(state)
    assert len(dry) == 1
    assert dry[0].context.dry_run is True
    assert dry[0].llm is not None and dry[0].llm.verdict == "accepted"


# ---------------------------------------------------------------------------
# LEARNING: the connection pin (anti-vacuity standard 3)
# ---------------------------------------------------------------------------


def learning_wiring(state: Path) -> Any:
    """The composition root's wiring, verbatim in shape: fold every persisted
    record into learning.json (``cli._learn_from_action``)."""
    path = state / "learning.json"

    def on_record(record: ActionRecord) -> None:
        data = learn_mod.load_learning(path)
        updated = learn_mod.record_action(data, record, now=FIXED_NOW)
        if updated is None:
            return
        learn_mod.save_learning(path, updated)

    return on_record


def test_an_accepted_integration_teaches_the_ranker(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """FIRING CONTROL for the pin below — and half the trace-attachment
    property.

    ``learn.is_matt_decided`` folds ``claude-integrate`` ONLY WITH a verdict
    ("An actor in LLM_EDIT_ACTORS folds only WITH such a verdict — the review
    gate is the human decision"). The record ``fileops`` builds has no ``llm``
    block; integrate's facade attaches it. Drop that and this precedent
    silently disappears — the learner would never see an integration again.
    """
    state = tmp_path / "state"
    ctx, proposal, target, _state = setup(
        fixture_vault, tmp_path, on_record=learning_wiring(state)
    )
    assert ctx.actor == "claude-integrate"
    assert "claude-integrate" in learn_mod.LLM_EDIT_ACTORS

    apply(ctx, target, proposal, "accepted")

    learning = json.loads((state / "learning.json").read_text(encoding="utf-8"))
    assert learning["associations"], "an accepted integration is a precedent"
    assert "integrate" in learn_mod.LEARNED_OPERATIONS


def test_a_rejected_integration_never_reaches_the_learner(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """CONNECTION PIN (anti-vacuity standard 3): pin the OUTCOME, not today's
    implementation location.

    ARCHITECTURE (Phase-4 bind+actor batch): "LEARNING FOLDS ONLY
    MATT-DECIDED ACTIONS … integrate records use the verdict-based reading
    (verdict accepted/edited = Matt-decided even though actor is
    claude-integrate)". That filter now lives in ``learn.is_matt_decided`` —
    the DESIGNED TRIPWIRE ARCHITECTURE predicted, already landed — so
    integrate must NOT keep a second copy of it. Its job is to hand the
    reader a record that carries the verdict at all.
    """
    state = tmp_path / "state"
    ctx, proposal, target, _state = setup(
        fixture_vault, tmp_path, on_record=learning_wiring(state)
    )

    apply(ctx, target, proposal, "rejected")

    assert not (state / "learning.json").exists(), "a rejection is not a precedent"

    # The firing control, on the SAME ctx and the SAME proposal: accepting it
    # does write learning.json — so the assertion above cannot pass because
    # the wiring is dead.
    apply(ctx, target, proposal, "accepted")
    assert (state / "learning.json").exists()


def test_a_rejection_by_a_human_actor_still_never_folds(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The OTHER direction of the trace attachment, and the corrupting one.

    ``learn.is_matt_decided`` reads the verdict off ``record.llm``: with no
    verdict, "a HUMAN actor folds" unconditionally. So a record for
    ``actor: "matt"`` that reached the learner WITHOUT its trace would teach
    the ranker that Matt files captures into the destination he just
    rejected — silently, and only for the interactive path. The facade must
    attach the trace before ``on_record``, not only before the recorder.
    """
    state = tmp_path / "state"
    ctx, proposal, target, _state = setup(
        fixture_vault, tmp_path, actor="matt", on_record=learning_wiring(state)
    )
    assert learn_mod.actor_is_human("matt") is True

    apply(ctx, target, proposal, "rejected")

    assert not (state / "learning.json").exists()

    # FIRING CONTROL: the same human actor accepting does fold.
    apply(ctx, target, proposal, "accepted")
    assert (state / "learning.json").exists()


def test_the_learning_wiring_is_connected_at_all(fixture_vault: Path, tmp_path: Path) -> None:
    """The other half of the connection pin: prove ``on_record`` really is the
    channel, by asserting the record it receives is the TRACED one (a wrapper
    that dropped the trace would be invisible in learning.json)."""
    seen: list[ActionRecord] = []
    ctx, proposal, target, _state = setup(fixture_vault, tmp_path, on_record=seen.append)

    apply(ctx, target, proposal, "accepted")

    assert len(seen) == 1
    assert seen[0].llm is not None
    assert seen[0].llm.verdict == "accepted"
    assert seen[0].llm.proposed_diff == proposal.diff


# ---------------------------------------------------------------------------
# the record's shape agrees with fileops' one record-building path
# ---------------------------------------------------------------------------


def test_the_integrate_record_context_agrees_with_a_fileops_record(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """ANTI-DRIFT: integrate reuses fileops' record builder through a facade
    rather than growing a second one. Same ctx, two operations, identical
    decision context — if someone re-implements the builder here, the two
    stop agreeing."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    ctx.suggestions_shown = ()
    ctx.chosen_rank = 2
    ctx.durations_ms = {"decision": 8400}
    ctx.auto_tags_present = ("machine-tag",)
    ctx.filters = {"tag": "blog-idea"}

    apply(ctx, target, proposal, "accepted")
    append_to_note(
        ctx,
        NoteRecord(
            path=str(fixture_vault / CAPTURE_REL),
            filename=Path(CAPTURE_REL).name,
            title="integrate-me",
            para_type="capture",
            folder="raw_capture",
            capture_id="integrate-me",
        ),
        fixture_vault / "areas/health/index.md",
    )

    integrate_record, append_record = records_in(state)
    assert integrate_record.operation == "integrate"
    assert append_record.operation == "append"
    for field_name in (
        "session_id",
        "chosen_rank",
        "durations_ms",
        "auto_tags_present",
        "filters",
        "vault_stats",
        "dry_run",
    ):
        assert getattr(integrate_record.context, field_name) == getattr(
            append_record.context, field_name
        ), field_name
    assert integrate_record.actor == append_record.actor
    assert integrate_record.ts == append_record.ts  # one pinned clock, one source


def test_the_proposal_id_is_echoed_into_the_record_when_the_schema_carries_it(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """SELF-REMOVING SEAM (the approved Phase-4 pattern).

    ARCHITECTURE Phase-5 wire contract: "proposal_id is a CORRELATION id
    echoed into the ActionRecord". The doc-12 §2 schema — owned by the actions
    seat — has no field for it, and smuggling first-class data into
    ``context.filters`` is exactly what ``dry_run`` and ``partial_failure``
    were promoted OUT of. So integrate echoes it only once ``LLMTrace`` grows
    the field, and this test activates ITSELF at that moment rather than
    waiting for someone to remember a skip mark.
    """
    if "proposal_id" not in {f.name for f in dataclasses.fields(LLMTrace)}:
        pytest.skip(
            "seam pending: actions.LLMTrace has no proposal_id field yet "
            "(seat integrate-engine requested it; this test activates itself when it lands)"
        )
    assert integrate_mod.TRACE_CARRIES_PROPOSAL_ID is True

    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    apply(ctx, target, proposal, "accepted")

    record = records_in(state)[0]
    assert dataclasses.asdict(record.llm)["proposal_id"] == proposal.proposal_id


def test_a_lost_record_never_blocks_the_write_and_never_reaches_a_reader(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Spec 12 §2: "Recording failures must not block the operation — log and
    continue — but must be loud". ``ActionRecorder.record`` reports a lost
    line by returning False (it never raises, by contract), and fileops'
    builder then skips ``on_record`` — the corpus is the source of truth, so a
    line nobody wrote is a line no derived view may hold either."""

    class LosesEveryLine:
        def __init__(self) -> None:
            self.seen: list[ActionRecord] = []

        def record(self, record: ActionRecord) -> bool:
            self.seen.append(record)
            return False

    derived: list[ActionRecord] = []
    ctx, proposal, target, _state = setup(fixture_vault, tmp_path, on_record=derived.append)
    recorder = LosesEveryLine()
    ctx.recorder = recorder  # type: ignore[assignment]

    result = apply(ctx, target, proposal, "accepted")

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == GOOD_CONTENT
    assert len(recorder.seen) == 1, "the record was built and offered"
    assert recorder.seen[0].llm is not None, "the facade attached the trace before the write"
    assert derived == [], "a lost line teaches nothing"


# ---------------------------------------------------------------------------
# end to end: propose → review → commit, through the real module surface
# ---------------------------------------------------------------------------


def test_propose_and_commit_round_trip_through_the_wire_shape(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The STATELESS contract in one test: the proposal is serialized, the
    core "forgets" everything, the client hands the object back, and the
    commit still lands the right bytes."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, tmp_path / "state", config)
    capture = read_document(write_capture(fixture_vault))
    target_doc = read_document(fixture_vault / TARGET_REL)

    proposal = propose(
        capture,
        target_doc,
        config,
        FakeLLM(llm_reply(GOOD_CONTENT)),
        route="blog",
        description="Blog ideas, one bullet each.",
        now=FIXED_NOW,
    )
    on_the_wire = json.dumps(proposal.to_json())

    revived = IntegrationProposal.from_json(json.loads(on_the_wire))
    result = apply(ctx, fixture_vault / TARGET_REL, revived, "accepted")

    assert result.ok is True
    assert (fixture_vault / TARGET_REL).read_text(encoding="utf-8") == GOOD_CONTENT
    record = records_in(tmp_path / "state")[0]
    assert record.context.route == "blog"
    assert record.targets[0].description == "Blog ideas, one bullet each."


def test_two_integrations_of_the_same_capture_are_two_records(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Doc 12 §2: "Matt explicitly adds one capture to multiple files; each
    target gets its own before/after". Integrate is per-target by
    construction, so two destinations are two records with two diffs."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, tmp_path / "state", config)
    capture = read_document(write_capture(fixture_vault))

    first = propose(
        capture,
        read_document(fixture_vault / TARGET_REL),
        config,
        FakeLLM(llm_reply(GOOD_CONTENT)),
        now=FIXED_NOW,
    )
    apply(ctx, fixture_vault / TARGET_REL, first, "accepted")

    health = fixture_vault / "areas/health/index.md"
    health_after = health.read_text(encoding="utf-8") + "\n" + CAPTURE_BODY + "\n"
    second = propose(
        capture,
        read_document(health),
        config,
        FakeLLM(llm_reply(health_after, "added at the end")),
        now=FIXED_NOW,
    )
    apply(ctx, health, second, "accepted")

    records = records_in(tmp_path / "state")
    assert len(records) == 2
    assert [r.targets[0].path for r in records] == [
        str(fixture_vault / TARGET_REL),
        str(health),
    ]
    assert records[0].targets[0].diff != records[1].targets[0].diff
    assert health.read_text(encoding="utf-8") == health_after


def test_the_clock_is_the_contexts_clock(fixture_vault: Path, tmp_path: Path) -> None:
    """One injected time source per operation (fileops' module contract), so
    a record's ts is not whatever `time.time()` said mid-write."""
    ctx, proposal, target, state = setup(fixture_vault, tmp_path)
    assert ctx.clock() == FIXED_NOW
    assert time.time() != FIXED_NOW

    apply(ctx, target, proposal, "accepted")

    assert records_in(state)[0].ts == "2026-08-06T07:06:40Z"
