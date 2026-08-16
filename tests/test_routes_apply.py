"""Route APPLICATION: ``apply_route`` / ``apply_all`` (spec 11 §1, 12 §1/§2).

Phase-4 seat ``routes-apply``. The resolution half lives in
``tests/test_routes.py``; this file only drives routes that actually touch a
vault, and it drives them against the fixture vault and tmp-dir state — never
real state, never ``~/Obsidian/Main`` (09 §1.4).

Every assertion is an EXACT value or a real filesystem fact. ``> 0`` is what
let the 08-known-issues defects survive (09 §3 testing bar), and a route that
"applied something somewhere" is precisely the shape of bug this suite
exists to catch.

Two clocks are pinned: ``FIXED_NOW`` feeds every timestamp the core renders,
so append headings, archive names and operations-log lines below are literal
strings rather than regexes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES
from organize_core.actions import ActionRecord, ActionRecorder
from organize_core.config import Config, FileOpsConfig, RouteConfig, VaultConfig
from organize_core.errors import RouteConfigError
from organize_core.fileops import OperationContext, OperationLog
from organize_core.index import NoteRecord
from organize_core.routes import apply_all, apply_route, merge_route_suggestions, resolve
from organize_core.suggest import Suggestion

FIXED_NOW = 1786000000.0
ISO = "2026-08-06T07:06:40Z"
TODAY = "2026-08-06"

# The two file destinations the fixture vault ships, and the one folder.
IDEAS = "projects/blog/ideas.md"
HEALTH_INDEX = "areas/health/index.md"
PERFORMING = "resources/performing/"

# The capture every test files: tags == ["workout"], body "leg day PR".
WORKOUT_CAPTURE = QUIRK_FILES["context_as_string"]

class FakeIndex:
    """Duck-typed stand-in for VaultIndex, recording exactly what the
    operations ask of it — the same fake the fileops suite uses, so route
    tests observe index traffic without a 13k-note reindex."""

    def __init__(self) -> None:
        self.updated: list[Path] = []
        self.removed: list[Path] = []

    def update_file(self, path: Path) -> None:
        self.updated.append(Path(path))

    def remove_file(self, path: Path) -> None:
        self.removed.append(Path(path))

    def stats(self) -> dict[str, int]:
        return {"total": 12, "capture_backlog": 5}


def route(
    tags: list[str],
    destination: str,
    mode: str = "append",
    *,
    description: str = "",
    template: str | None = None,
) -> RouteConfig:
    return RouteConfig(
        tags=tags,
        destination=destination,
        mode=mode,  # type: ignore[arg-type]
        description=description,
        template=template,
    )


def make_config(vault: Path, *route_configs: RouteConfig, **file_ops: Any) -> Config:
    return Config(
        vault=VaultConfig(root=vault),
        file_ops=FileOpsConfig(**file_ops),
        routes=list(route_configs),
    )


def make_ctx(
    vault: Path,
    state: Path,
    config: Config,
    *,
    dry_run: bool = False,
    actor: str = "matt",
    index: Any = None,
    on_record: Any = None,
) -> OperationContext:
    return OperationContext(
        config=config,
        index=index if index is not None else FakeIndex(),  # type: ignore[arg-type]
        oplog=OperationLog(state / "operations.log"),
        recorder=ActionRecorder(state / "actions"),
        backup_dir=vault / config.file_ops.backup_dir,
        dry_run=dry_run,
        actor=actor,
        session_id="ses_routes",
        clock=lambda: FIXED_NOW,
        on_record=on_record,
    )


def capture_record(vault: Path, rel: str = WORKOUT_CAPTURE) -> NoteRecord:
    path = vault / rel
    return NoteRecord(
        path=str(path),
        filename=path.name,
        title=path.stem,
        para_type="capture",
        folder=path.parent.name,
        capture_id=path.stem,
        id=path.stem,
        tags=["workout"],
    )


def log_lines(ctx: OperationContext) -> list[str]:
    path = ctx.oplog.log_file
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def records(ctx: OperationContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for month in sorted(Path(ctx.recorder.actions_dir).glob("*.jsonl")):
        for line in month.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def archived(vault: Path, rel: str = WORKOUT_CAPTURE) -> Path:
    return vault / "archive/capture/raw_capture" / Path(rel).name


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


# ---------------------------------------------------------------------------
# One route, one destination
# ---------------------------------------------------------------------------


def test_a_single_append_route_files_the_body_and_archives_the_capture_once(
    fixture_vault: Path, state: Path
) -> None:
    """Spec 11 §4 acceptance test 1, end to end: body appended under the date
    template, capture archived, `append` logged, and the target's frontmatter
    otherwise untouched."""
    config = make_config(fixture_vault, route(["workout"], IDEAS, "append"))
    ctx = make_ctx(fixture_vault, state, config)
    target = fixture_vault / IDEAS
    before = target.read_text(encoding="utf-8")
    source = fixture_vault / WORKOUT_CAPTURE

    matches = resolve(["workout"], config)
    results = apply_all(ctx, capture_record(fixture_vault), matches)

    assert [r.operation for r in results] == ["append", "archive"]
    assert [r.ok for r in results] == [True, True]

    after = target.read_text(encoding="utf-8")
    assert after.endswith(f"## {TODAY} — from context-string\n\nleg day PR\n")
    # Frontmatter: only `last_edited_date` moved (11 §1 "target's frontmatter
    # untouched except last_edited_date").
    assert after.split("---\n")[1] == (
        before.split("---\n")[1].rstrip("\n") + f"\nlast_edited_date: '{TODAY}'\n"
    )

    assert not source.exists()
    assert archived(fixture_vault).read_text(encoding="utf-8").endswith("leg day PR\n")
    assert log_lines(ctx) == [
        f"[{ISO}] append: {source} -> {target} [SUCCESS] Backup: {results[0].backup_path}",
        f"[{ISO}] archive: {source} -> {archived(fixture_vault)} [SUCCESS] "
        f"Backup: {results[1].backup_path}",
    ]


def test_the_single_route_record_is_one_record_attributed_to_the_route(
    fixture_vault: Path, state: Path
) -> None:
    """12 §2: one record per logical action, ``actor: route:<name>`` when one
    route fired, and the archive is NOT a target (it is the capture's own
    relocation, not a destination the content was filed into)."""
    config = make_config(fixture_vault, route(["workout"], IDEAS, "append"))
    ctx = make_ctx(fixture_vault, state, config)

    apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    (record,) = records(ctx)
    assert record["operation"] == "append"
    assert record["edit_mode"] == "append"
    assert record["actor"] == "route:workout"
    assert record["context"]["route"] == "workout"
    assert record["context"]["partial_failure"] is None
    assert [t["path"] for t in record["targets"]] == [str(fixture_vault / IDEAS)]
    assert [t["role"] for t in record["targets"]] == ["append_target"]


def test_apply_route_alone_files_the_content_and_deliberately_does_not_archive(
    fixture_vault: Path, state: Path
) -> None:
    """``apply_route`` is ONE destination; archiving belongs to ``apply_all``
    (ARCHITECTURE ruling #11 + the Phase-4 routes-apply ruling). A caller that
    wants doc-11 semantics calls ``apply_all`` even for a single match."""
    config = make_config(fixture_vault, route(["workout"], IDEAS, "append"))
    ctx = make_ctx(fixture_vault, state, config)
    (match,) = resolve(["workout"], config)

    result = apply_route(ctx, capture_record(fixture_vault), match)

    assert result.ok is True
    assert (fixture_vault / WORKOUT_CAPTURE).is_file(), "apply_route must not archive"
    assert not archived(fixture_vault).exists()
    # Called directly it is an ordinary single-target op, so it records itself.
    assert [r["operation"] for r in records(ctx)] == ["append"]


def test_a_route_template_overrides_the_default_append_heading(
    fixture_vault: Path, state: Path
) -> None:
    """11 §1: "exact template configurable per route via ``template``"."""
    config = make_config(
        fixture_vault,
        route(["workout"], IDEAS, "append", template="- {body} ({date})"),
    )
    ctx = make_ctx(fixture_vault, state, config)

    apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert (fixture_vault / IDEAS).read_text(encoding="utf-8").endswith(
        f"- leg day PR ({TODAY})\n"
    )


# ---------------------------------------------------------------------------
# Several routes, one capture
# ---------------------------------------------------------------------------


def multi_config(vault: Path) -> Config:
    """Two file destinations, distinct route names, both matched by
    ``workout`` (the second any-of matches on its SECOND tag)."""
    return make_config(
        vault,
        route(["workout"], IDEAS, "append"),
        route(["exercise", "workout"], HEALTH_INDEX, "append"),
    )


def test_a_capture_matching_two_routes_lands_in_both_and_archives_exactly_once(
    fixture_vault: Path, state: Path
) -> None:
    """Spec 11 §4 acceptance test 2, first half."""
    config = multi_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    source = fixture_vault / WORKOUT_CAPTURE

    results = apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert [r.operation for r in results] == ["append", "append", "archive"]
    assert [r.ok for r in results] == [True, True, True]
    assert [Path(r.destination or "") for r in results[:2]] == [
        fixture_vault / IDEAS,
        fixture_vault / HEALTH_INDEX,
    ]

    for rel in (IDEAS, HEALTH_INDEX):
        assert (fixture_vault / rel).read_text(encoding="utf-8").endswith(
            f"## {TODAY} — from context-string\n\nleg day PR\n"
        ), rel

    assert not source.exists()
    archives = sorted((fixture_vault / "archive/capture/raw_capture").iterdir())
    assert [p.name for p in archives] == ["context-string.md"], "archived exactly once"
    assert [line.split("] ")[1].split(":")[0] for line in log_lines(ctx)] == [
        "append",
        "append",
        "archive",
    ]


def test_the_multi_route_action_is_ONE_record_with_one_target_per_file(
    fixture_vault: Path, state: Path
) -> None:
    """Spec 12 §3: "Multi-destination route action: one record, two targets
    entries, both diffs correct". The diffs are the ones ``fileops`` itself
    built, which is why each one names its own file and shows its own body."""
    config = multi_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)

    apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    (record,) = records(ctx)
    assert record["operation"] == "append"
    assert record["context"]["route"] == "workout, exercise"
    # Two routes, so no single one owns the record; it identifies itself
    # through context.route instead (Phase-4 routes-apply ruling).
    assert record["actor"] == "matt"

    targets = record["targets"]
    assert [t["path"] for t in targets] == [
        str(fixture_vault / IDEAS),
        str(fixture_vault / HEALTH_INDEX),
    ]
    assert [t["role"] for t in targets] == ["append_target", "append_target"]
    for target, rel in zip(targets, (IDEAS, HEALTH_INDEX), strict=True):
        assert f"+++ b/{fixture_vault / rel}" in target["diff"]
        assert "+leg day PR" in target["diff"]
        assert target["before_hash"] != target["after_hash"]
    # The health folder's index note carries an NL description (11 §3), so the
    # record proves descriptions reach targets[].description as 12 §2 requires
    # — but only through the wired seam, which these unit contexts leave off.
    assert [t["description"] for t in targets] == [None, None]


def test_the_capture_body_is_stored_once_in_the_multi_route_record(
    fixture_vault: Path, state: Path
) -> None:
    """12 §2 "Capture body stored in full": the capture block is ONE block for
    the whole action even though two files were written."""
    config = multi_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)

    apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    (record,) = records(ctx)
    assert record["capture"]["path"] == str(fixture_vault / WORKOUT_CAPTURE)
    assert record["capture"]["body_before"] == "leg day PR\n"
    assert record["capture"]["frontmatter_before"]["tags"] == ["workout"]


# ---------------------------------------------------------------------------
# Failure mid-batch (world state, not addressing)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores the write bit")
def test_an_unwritable_second_destination_leaves_the_first_standing_and_no_archive(
    fixture_vault: Path, state: Path
) -> None:
    """Spec 11 §4 acceptance test 2, second half: "failure of the second
    destination leaves the capture unarchived and both attempts logged"."""
    config = multi_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    source = fixture_vault / WORKOUT_CAPTURE
    os.chmod(fixture_vault / "areas/health", 0o555)
    try:
        results = apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))
    finally:
        os.chmod(fixture_vault / "areas/health", 0o755)

    assert [r.ok for r in results] == [True, False]
    assert "could not write" in (results[1].error or "")

    # The destination that worked STANDS.
    assert (fixture_vault / IDEAS).read_text(encoding="utf-8").endswith("leg day PR\n")
    # The capture is NOT archived and is exactly where it was.
    assert source.is_file()
    assert not archived(fixture_vault).exists()
    assert not (fixture_vault / "archive/capture/raw_capture").exists() or not list(
        (fixture_vault / "archive/capture/raw_capture").iterdir()
    )

    lines = log_lines(ctx)
    assert lines[0].endswith(f"[SUCCESS] Backup: {results[0].backup_path}")
    assert lines[-1].endswith(f"[FAILED] Backup: {results[1].backup_path} Error: {results[1].error}")
    assert "archive:" not in " ".join(lines)


def test_a_missing_second_destination_is_a_world_state_failure_not_an_exception(
    fixture_vault: Path, state: Path
) -> None:
    """The ARCHITECTURE error-line rule: a validly-addressed op whose world
    state is wrong returns ``ok=False`` + a FAILED line, it does not raise.
    Callers must check ``result.ok``; a returned list is not proof of work."""
    config = make_config(
        fixture_vault,
        route(["workout"], IDEAS, "append"),
        route(["exercise", "workout"], "projects/blog/nope.md", "append"),
    )
    ctx = make_ctx(fixture_vault, state, config)

    results = apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert [r.ok for r in results] == [True, False]
    assert results[1].error == f"append target does not exist: {fixture_vault}/projects/blog/nope.md"
    assert (fixture_vault / WORKOUT_CAPTURE).is_file()
    assert log_lines(ctx)[-1].endswith(f"[FAILED] Error: {results[1].error}")


def test_a_partly_applied_route_action_records_and_names_what_did_not_finish(
    fixture_vault: Path, state: Path
) -> None:
    """The vault really changed, so 12 §2 demands a record — carrying
    ``context.partial_failure``, which is what keeps a half-applied action out
    of the learning corpus (04 §33) and out of the accept-rate stats."""
    config = make_config(
        fixture_vault,
        route(["workout"], IDEAS, "append"),
        route(["exercise", "workout"], "projects/blog/nope.md", "append"),
    )
    seen: list[ActionRecord] = []
    ctx = make_ctx(fixture_vault, state, config, on_record=seen.append)

    apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    (record,) = records(ctx)
    assert [t["path"] for t in record["targets"]] == [str(fixture_vault / IDEAS)]
    assert record["context"]["partial_failure"] == (
        "1 of 2 route destinations applied; failed: exercise -> "
        f"{fixture_vault}/projects/blog/nope.md"
    )
    # on_record fires exactly once, with the aggregate (12 §2 "one write path,
    # two readers") — not once per destination.
    assert len(seen) == 1
    assert seen[0].context.partial_failure is not None


def test_nothing_reaches_the_corpus_when_no_destination_was_applied(
    fixture_vault: Path, state: Path
) -> None:
    """fileops' rule, inherited: an operation that changed nothing does not
    record — its story is the FAILED operations-log line."""
    config = make_config(fixture_vault, route(["workout"], "projects/blog/nope.md", "append"))
    ctx = make_ctx(fixture_vault, state, config)

    (result,) = apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert result.ok is False
    assert records(ctx) == []
    assert log_lines(ctx)[-1].endswith(f"[FAILED] Error: {result.error}")


# ---------------------------------------------------------------------------
# integrate — the doc-12 §1 handoff boundary (Phase 5)
# ---------------------------------------------------------------------------


def test_an_integrate_route_still_resolves_and_still_ranks_above_scored(
    fixture_vault: Path
) -> None:
    """Phase 4 ships tag ROUTING; the integrate EDITING half is Phase 5. The
    route must still be visible everywhere it was before — refusing to SHOW it
    would hide a configured destination from the UI (11 §1)."""
    config = make_config(
        fixture_vault,
        route(["workout"], HEALTH_INDEX, "integrate", description="Woven into the log."),
    )
    (match,) = resolve(["workout"], config)
    assert match.route.mode == "integrate"

    scored = [Suggestion(path="/x", name="x", type="project", score=3.1, reasons=("t",))]
    merged = merge_route_suggestions([match], scored)
    assert merged[0].route == "workout"
    assert merged[0].description == "Woven into the log."
    assert merged[0].reasons == ("Route 'workout' (integrate)",)


def test_applying_an_integrate_route_refuses_cleanly_and_names_phase_five(
    fixture_vault: Path, state: Path
) -> None:
    config = make_config(fixture_vault, route(["workout"], HEALTH_INDEX, "integrate"))
    ctx = make_ctx(fixture_vault, state, config)
    (match,) = resolve(["workout"], config)

    with pytest.raises(NotImplementedError) as excinfo:
        apply_route(ctx, capture_record(fixture_vault), match)

    message = str(excinfo.value)
    assert "integrate" in message
    assert "Phase 5" in message
    assert str(fixture_vault / HEALTH_INDEX) in message


def test_an_integrate_route_in_a_batch_refuses_before_anything_is_written(
    fixture_vault: Path, state: Path
) -> None:
    """A batch that cannot be carried out as asked must not be
    half-carried-out: the refusal comes BEFORE the sibling append runs."""
    config = make_config(
        fixture_vault,
        route(["workout"], IDEAS, "append"),
        route(["exercise", "workout"], HEALTH_INDEX, "integrate"),
    )
    ctx = make_ctx(fixture_vault, state, config)
    ideas_before = (fixture_vault / IDEAS).read_bytes()

    with pytest.raises(NotImplementedError):
        apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert (fixture_vault / IDEAS).read_bytes() == ideas_before
    assert (fixture_vault / WORKOUT_CAPTURE).is_file()
    assert log_lines(ctx) == []
    assert records(ctx) == []


def test_an_unknown_route_mode_raises_rather_than_guessing(
    fixture_vault: Path, state: Path
) -> None:
    """Addressing failure per the error-line rule: the request never named an
    operation that could be attempted."""
    config = make_config(fixture_vault, route(["workout"], IDEAS, "weld"))
    ctx = make_ctx(fixture_vault, state, config)
    (match,) = resolve(["workout"], config)

    with pytest.raises(RouteConfigError) as excinfo:
        apply_route(ctx, capture_record(fixture_vault), match)
    assert "weld" in str(excinfo.value)


# ---------------------------------------------------------------------------
# dry run (09 §5.6 + ARCHITECTURE ruling (a))
# ---------------------------------------------------------------------------


def test_a_dry_run_applies_nothing_and_still_records_the_intended_action(
    fixture_vault: Path, state: Path
) -> None:
    """09 §5.6: vault-write-free but NOT state-silent. Zero VAULT bytes move;
    the intended action is on paper as a ``[DRY-RUN]`` line and as a
    dry-run-marked record that no corpus reader counts by default."""
    config = multi_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config, dry_run=True)
    before = {
        rel: (fixture_vault / rel).read_bytes()
        for rel in (IDEAS, HEALTH_INDEX, WORKOUT_CAPTURE)
    }

    results = apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert [r.ok for r in results] == [True, True, True]
    assert [r.dry_run for r in results] == [True, True, True]
    for rel, raw in before.items():
        assert (fixture_vault / rel).read_bytes() == raw, f"{rel} must be byte-identical"
    assert not archived(fixture_vault).exists()

    assert all("[DRY-RUN]" in line for line in log_lines(ctx))
    (record,) = records(ctx)
    assert record["context"]["dry_run"] is True
    assert len(record["targets"]) == 2
    # Excluded from every corpus reader by default (ActionContext.dry_run).
    assert list(ctx.recorder.query()) == []


# ---------------------------------------------------------------------------
# no-ai (vault law, spec 02) — absolute for automated actors
# ---------------------------------------------------------------------------


def test_an_automated_actor_may_not_route_a_no_ai_capture_anywhere(
    fixture_vault: Path, state: Path
) -> None:
    """02: an append duplicates the capture's body into another file, so a
    ``no-ai: true`` capture refuses an automated actor — and it refuses BEFORE
    the write, so nothing lands."""
    from organize_core.errors import NoAiRefusal

    config = make_config(fixture_vault, route(["journal"], IDEAS, "append"))
    ctx = make_ctx(fixture_vault, state, config, actor="consumer:tag_router")
    private = fixture_vault / QUIRK_FILES["no_ai"]
    ideas_before = (fixture_vault / IDEAS).read_bytes()
    capture = NoteRecord(
        path=str(private),
        filename=private.name,
        title=private.stem,
        para_type="capture",
        folder=private.parent.name,
        capture_id=private.stem,
        tags=["journal"],
    )

    with pytest.raises(NoAiRefusal):
        apply_all(ctx, capture, resolve(["journal"], config))

    assert (fixture_vault / IDEAS).read_bytes() == ideas_before
    assert private.is_file()
    assert records(ctx) == []


def test_a_human_actor_may_route_a_no_ai_capture(
    fixture_vault: Path, state: Path
) -> None:
    """The control that stops the test above passing vacuously: the refusal is
    about WHO is writing, not about the route."""
    config = make_config(fixture_vault, route(["journal"], IDEAS, "append"))
    ctx = make_ctx(fixture_vault, state, config, actor="matt")
    private = fixture_vault / QUIRK_FILES["no_ai"]
    capture = NoteRecord(
        path=str(private),
        filename=private.name,
        title=private.stem,
        para_type="capture",
        folder=private.parent.name,
        capture_id=private.stem,
        tags=["journal"],
    )

    results = apply_all(ctx, capture, resolve(["journal"], config))

    assert [r.ok for r in results] == [True, True]
    assert "Automated tooling must never write to this note." in (
        fixture_vault / IDEAS
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# move mode
# ---------------------------------------------------------------------------


def move_config(vault: Path) -> Config:
    return make_config(vault, route(["workout"], PERFORMING, "move"))


def test_a_move_route_files_the_capture_into_the_folder_and_archives_the_original(
    fixture_vault: Path, state: Path
) -> None:
    """11 §1: ``mode = "move"`` is "identical to a doc 05 move" — the organized
    copy lands in the destination folder with the ``<type>/<folder>`` tag and
    ``processing_status: organized``, and the original ends up in the archive.
    True under both the seam and the interim; only the plumbing differs."""
    config = move_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)

    results = apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert all(r.ok for r in results)
    filed = fixture_vault / "resources/performing/context-string.md"
    assert filed.is_file()
    text = filed.read_text(encoding="utf-8")
    assert "resource/performing" in text
    assert "processing_status: organized" in text
    assert not (fixture_vault / WORKOUT_CAPTURE).exists()
    assert archived(fixture_vault).is_file()
    assert [r["operation"] for r in records(ctx)] == ["move"]


def test_a_move_route_record_carries_the_destination_as_its_one_target(
    fixture_vault: Path, state: Path
) -> None:
    config = move_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)

    apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    (record,) = records(ctx)
    assert [t["path"] for t in record["targets"]] == [
        str(fixture_vault / "resources/performing/context-string.md")
    ]
    assert [t["role"] for t in record["targets"]] == ["destination"]
    assert record["capture"]["frontmatter_after"]["processing_status"] == "organized"


def test_a_mixed_batch_records_the_strongest_mutation_as_its_operation(
    fixture_vault: Path, state: Path
) -> None:
    """Phase-4 routes-apply ruling: move > integrate > append. Naming a batch
    that RELOCATED the original after its weakest member would let a corpus
    reader read a relocation as an addition."""
    config = make_config(
        fixture_vault,
        route(["workout"], IDEAS, "append"),
        route(["exercise", "workout"], PERFORMING, "move"),
    )
    ctx = make_ctx(fixture_vault, state, config)

    results = apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert all(r.ok for r in results)
    assert (fixture_vault / IDEAS).read_text(encoding="utf-8").endswith("leg day PR\n")
    assert (fixture_vault / "resources/performing/context-string.md").is_file()
    assert archived(fixture_vault).is_file()

    (record,) = records(ctx)
    assert record["operation"] == "move"
    assert [t["role"] for t in record["targets"]] == ["append_target", "destination"]
    assert record["context"]["route"] == "workout, exercise"


def test_destinations_run_in_config_order(fixture_vault: Path, state: Path) -> None:
    """Spec 11 §1: "Order: config order". With the archive seam a move no
    longer consumes the capture, so nothing has to be deferred."""
    config = make_config(
        fixture_vault,
        route(["workout"], PERFORMING, "move"),
        route(["exercise", "workout"], IDEAS, "append"),
    )
    ctx = make_ctx(fixture_vault, state, config)

    apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert [line.split("] ")[1].split(":")[0] for line in log_lines(ctx)] == [
        "move",
        "append",
        "archive",
    ]


def test_two_move_routes_file_two_copies_and_archive_the_original_once(
    fixture_vault: Path, state: Path
) -> None:
    """Spec 11 §1 permits several destinations regardless of mode; with the
    seam that means two organized copies and exactly one archive."""
    config = make_config(
        fixture_vault,
        route(["workout"], PERFORMING, "move"),
        route(["exercise", "workout"], "projects/blog/", "move"),
    )
    ctx = make_ctx(fixture_vault, state, config)

    results = apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))

    assert all(r.ok for r in results)
    assert (fixture_vault / "resources/performing/context-string.md").is_file()
    assert (fixture_vault / "projects/blog/context-string.md").is_file()
    assert [p.name for p in sorted((fixture_vault / "archive/capture/raw_capture").iterdir())] == [
        "context-string.md"
    ]
    (record,) = records(ctx)
    assert len(record["targets"]) == 2


# ---------------------------------------------------------------------------
# The learning reader of a multi-target record (04 §33 / 12 §2 "Uses" #2)
# ---------------------------------------------------------------------------


def test_learning_folds_every_destination_of_a_multi_target_record(
    fixture_vault: Path, state: Path
) -> None:
    """The latent bug the Phase-4 routes-apply ruling asked to be pinned.

    Multi-file is first-class in the corpus (12 §2), so the corpus's own
    reader must be too: a capture Matt filed into two folders is two pieces of
    evidence about where that kind of capture goes, and folding only the first
    silently under-records every route action he takes.

    NOT VACUOUS: the ONE assertion below is the whole test, and it is reached
    — the preceding lines are asserted first, so an import error, an empty
    corpus or a ``None`` fold would fail this test for the wrong reason and
    ``strict=True`` would still report it (as a failure, not an xfail).
    ``statistics.destinations`` is the per-destination counter
    ``record_move`` bumps, so it names exactly which destinations were
    learned."""
    from organize_core import learn

    config = multi_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    apply_all(ctx, capture_record(fixture_vault), resolve(["workout"], config))
    (raw,) = records(ctx)
    record = ActionRecord.from_json(raw)
    assert len(record.targets) == 2, "the record under test really is multi-target"

    data = learn.record_action(learn.LearningData(), record, now=FIXED_NOW)
    assert data is not None, "an append to a real destination is a learned operation"

    assert sorted(data.statistics.destinations) == [
        str(fixture_vault / "areas/health"),
        str(fixture_vault / "projects/blog"),
    ]
