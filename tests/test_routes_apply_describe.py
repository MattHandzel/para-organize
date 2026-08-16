"""``routes.set_description`` — natural-language folder/file descriptions
(spec 11 §3, with the doc-05 write apparatus and the doc-12 §2 record).

Phase-4 seat ``routes-apply``. The read half (``get_description``'s lookup
order over a stub index) is pinned in ``tests/test_routes.py``; this file
drives the WRITE against the fixture vault and a REAL ``VaultIndex``, because
"written, then retrievable" is the only assertion that proves the write lands
where the read looks — a stub index would let a write to the wrong file pass.

Every assertion is an exact value or a filesystem fact (09 §3). The clock is
pinned, so operations-log lines below are literal strings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES
from organize_core import routes
from organize_core.actions import ActionRecorder
from organize_core.config import Config, FileOpsConfig, VaultConfig
from organize_core.errors import NoAiRefusal, OperationError
from organize_core.fileops import OperationContext, OperationLog, parse_log_line
from organize_core.routes import description_note_for, get_description, set_description

FIXED_NOW = 1786000000.0
ISO = "2026-08-06T07:06:40Z"

HEALTH_FOLDER = "areas/health"  # ships an index.md with a description (02/11 §3)
UNDESCRIBED_FOLDER = "areas/relationships"  # ships no index note at all
IDEAS = QUIRK_FILES["merge_target"]  # a plain note, describable in itself


def make_config(vault: Path, *, descriptions: dict[str, str] | None = None) -> Config:
    return Config(
        vault=VaultConfig(root=vault),
        file_ops=FileOpsConfig(),
        descriptions=dict(descriptions or {}),
    )


def make_ctx(
    vault: Path,
    state: Path,
    config: Config,
    *,
    dry_run: bool = False,
    actor: str = "matt",
) -> OperationContext:
    """A REAL ``VaultIndex`` — see the module docstring."""
    from organize_core.index import VaultIndex

    return OperationContext(
        config=config,
        index=VaultIndex(config, state / "index.json"),
        oplog=OperationLog(state / "operations.log"),
        recorder=ActionRecorder(state / "actions"),
        backup_dir=vault / config.file_ops.backup_dir,
        dry_run=dry_run,
        actor=actor,
        session_id="ses_describe",
        clock=lambda: FIXED_NOW,
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


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


# ---------------------------------------------------------------------------
# where a description is written, and that a read finds it there
# ---------------------------------------------------------------------------


def test_a_folder_description_round_trips_through_its_index_note(
    fixture_vault: Path, state: Path
) -> None:
    """Spec 11 §3 end to end: written into the folder's ``index.md``
    frontmatter, and ``get_description`` returns exactly that text afterwards
    — the pair that proves the write and the read agree on the location."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    folder = fixture_vault / HEALTH_FOLDER
    index_note = fixture_vault / QUIRK_FILES["described_folder_index"]
    text = "Training log, sleep, injuries — the practice, not the research."

    result = set_description(ctx, folder, text)

    assert result.ok is True
    assert result.operation == "metadata"
    assert result.source == str(index_note)
    assert result.details["changed"] is True
    assert get_description(folder, ctx.index, config) == text


def test_the_index_note_keeps_every_other_field_and_its_body(
    fixture_vault: Path, state: Path
) -> None:
    """The round-trip law at the operation level (08 §A12): describing a
    folder rewrites ONE field. The whole file is asserted, not a substring —
    a substring check passes while the rest of the file is being reordered."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    index_note = fixture_vault / QUIRK_FILES["described_folder_index"]

    set_description(ctx, fixture_vault / HEALTH_FOLDER, "Health practice.")

    assert index_note.read_text(encoding="utf-8") == (
        "---\n"
        "title: Health\n"
        "description: Health practice.\n"
        "tags:\n"
        "- area\n"
        "---\n"
        "# Health\n"
    )


def test_describing_a_note_describes_that_note(fixture_vault: Path, state: Path) -> None:
    """11 §3 covers files as well as folders ("any PARA folder OR FILE")."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    target = fixture_vault / IDEAS

    set_description(ctx, target, "One-line blog post ideas, appended under ## Inbox.")

    body = target.read_text(encoding="utf-8")
    # Quoted by the serializer because the text contains `#` — the round-trip
    # law's job is to keep the VALUE, not a particular spelling of it.
    assert "description: 'One-line blog post ideas, appended under ## Inbox.'" in body
    assert "author: Matt Handzel" in body  # an unknown field, untouched
    assert get_description(target, ctx.index, config) == (
        "One-line blog post ideas, appended under ## Inbox."
    )


def test_the_write_target_is_the_first_candidate_the_read_consults(
    fixture_vault: Path, state: Path
) -> None:
    """``description_note_for`` and ``get_description`` must not drift apart:
    ``index.md`` first, then ``<folder>/<folder-name>.md``, then nothing."""
    health = fixture_vault / HEALTH_FOLDER
    assert description_note_for(health) == health / "index.md"

    performing = fixture_vault / "resources/performing"
    assert description_note_for(performing) is None
    (performing / "performing.md").write_text("---\ntags: []\n---\nx\n", encoding="utf-8")
    assert description_note_for(performing) == performing / "performing.md"

    assert description_note_for(fixture_vault / UNDESCRIBED_FOLDER) is None
    assert description_note_for(fixture_vault / "areas/nope") is None
    assert description_note_for(fixture_vault / IDEAS) == fixture_vault / IDEAS


# ---------------------------------------------------------------------------
# recording: one keystroke, one logical edit, one record (12 §2)
# ---------------------------------------------------------------------------


def test_describing_a_folder_is_one_logged_operation_and_one_record(
    fixture_vault: Path, state: Path
) -> None:
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    index_note = fixture_vault / QUIRK_FILES["described_folder_index"]

    result = set_description(ctx, fixture_vault / HEALTH_FOLDER, "Health practice.")

    assert log_lines(ctx) == [
        f"[{ISO}] metadata: {index_note} -> {index_note} [SUCCESS] Backup: {result.backup_path}"
    ]
    (record,) = records(ctx)
    assert record["operation"] == "meta_edit"
    assert record["capture"]["frontmatter_after"]["description"] == "Health practice."
    assert [t["path"] for t in record["targets"]] == [str(index_note)]
    assert "description: Health practice." in record["targets"][0]["diff"]
    assert record["context"]["dry_run"] is False


def test_a_backup_of_the_index_note_is_taken_before_it_is_rewritten(
    fixture_vault: Path, state: Path
) -> None:
    """05 §1.4: a description edit is a vault write like any other."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    before = (fixture_vault / QUIRK_FILES["described_folder_index"]).read_bytes()

    result = set_description(ctx, fixture_vault / HEALTH_FOLDER, "Health practice.")

    assert result.backup_path is not None
    assert Path(result.backup_path).read_bytes() == before


# ---------------------------------------------------------------------------
# clearing
# ---------------------------------------------------------------------------


def test_an_empty_description_removes_the_field_and_uncovers_the_config_table(
    fixture_vault: Path, state: Path
) -> None:
    """The only way to un-describe a folder — and proof that the central
    ``[descriptions]`` table really is the FALLBACK, not a parallel store."""
    config = make_config(fixture_vault, descriptions={"areas/health": "from the config table"})
    ctx = make_ctx(fixture_vault, state, config)
    folder = fixture_vault / HEALTH_FOLDER
    index_note = fixture_vault / QUIRK_FILES["described_folder_index"]

    set_description(ctx, folder, "written first")
    # The note's own field WINS over the config table while it is present.
    assert get_description(folder, ctx.index, config) == "written first"

    result = set_description(ctx, folder, "   ")

    assert result.ok is True
    assert "description:" not in index_note.read_text(encoding="utf-8")
    assert get_description(folder, ctx.index, config) == "from the config table"


# ---------------------------------------------------------------------------
# no-ai (vault law, spec 02) — absolute for automated actors
# ---------------------------------------------------------------------------


def test_an_automated_actor_may_not_describe_a_no_ai_note(
    fixture_vault: Path, state: Path
) -> None:
    config = make_config(fixture_vault)
    note = fixture_vault / QUIRK_FILES["no_ai"]
    before = note.read_bytes()
    ctx = make_ctx(fixture_vault, state, config, actor="consumer:auto_tagger")

    with pytest.raises(NoAiRefusal) as excinfo:
        set_description(ctx, note, "a description of a private thought")

    assert "no-ai" in str(excinfo.value)
    assert note.read_bytes() == before
    assert log_lines(ctx) == []
    assert records(ctx) == []


def test_a_human_actor_may_describe_a_no_ai_note(
    fixture_vault: Path, state: Path
) -> None:
    """The firing control for the test above: the refusal is about WHO is
    writing. Without this the no-ai assertion could pass because describing
    was broken for everyone."""
    config = make_config(fixture_vault)
    note = fixture_vault / QUIRK_FILES["no_ai"]
    ctx = make_ctx(fixture_vault, state, config, actor="matt")

    result = set_description(ctx, note, "a description of a private thought")

    assert result.ok is True
    text = note.read_text(encoding="utf-8")
    assert "description: a description of a private thought" in text
    assert "no-ai: true" in text  # the flag itself survives the rewrite


# ---------------------------------------------------------------------------
# seeding an index note (Phase-4 routes-apply ruling)
# ---------------------------------------------------------------------------


def test_describing_a_folder_without_an_index_note_seeds_one(
    fixture_vault: Path, state: Path
) -> None:
    """``organize routes describe`` is a keystroke Matt typed, so the note
    spec 11 §3 names as the primary storage is created — heading only, no
    invented prose — and then described through the ordinary recorded path."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config, actor="matt")
    folder = fixture_vault / UNDESCRIBED_FOLDER
    assert not (folder / "index.md").exists()

    result = set_description(ctx, folder, "People I keep up with.")

    assert result.ok is True
    assert result.details["created"] is True
    assert (folder / "index.md").read_text(encoding="utf-8") == (
        "---\ndescription: People I keep up with.\n---\n# relationships\n"
    )
    assert get_description(folder, ctx.index, config) == "People I keep up with."
    (record,) = records(ctx)
    assert record["operation"] == "meta_edit"


def test_an_automated_actor_may_not_invent_an_index_note(
    fixture_vault: Path, state: Path
) -> None:
    """Same reasoning as the no-ai law: tooling edits what Matt made, it does
    not decide that a folder should now have an index note."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config, actor="consumer:auto_tagger")
    folder = fixture_vault / UNDESCRIBED_FOLDER

    with pytest.raises(OperationError) as excinfo:
        set_description(ctx, folder, "People I keep up with.")

    assert "automated tooling" in str(excinfo.value)
    hint = excinfo.value.hint or ""
    assert str(folder / "index.md") in hint
    assert "[descriptions]" in hint
    assert not (folder / "index.md").exists()
    assert log_lines(ctx) == []
    assert records(ctx) == []


def test_an_automated_actor_may_still_describe_a_folder_that_has_an_index_note(
    fixture_vault: Path, state: Path
) -> None:
    """The boundary is CREATION, not description: the auto-tagger filling in a
    description that already has somewhere to live is allowed."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config, actor="consumer:auto_tagger")

    result = set_description(ctx, fixture_vault / HEALTH_FOLDER, "Filled in by tooling.")

    assert result.ok is True
    assert result.details.get("created") is None


def test_describing_a_path_that_does_not_exist_raises_rather_than_creating_it(
    fixture_vault: Path, state: Path
) -> None:
    """05 §1: the core never invents vault structure. ``areas/nope`` is not a
    folder, so there is nothing to describe and nothing is made up."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)

    with pytest.raises(OperationError) as excinfo:
        set_description(ctx, fixture_vault / "areas/nope", "x")

    assert "no such file or folder" in str(excinfo.value)
    assert not (fixture_vault / "areas/nope").exists()
    assert log_lines(ctx) == []


def test_a_path_outside_the_vault_is_refused(fixture_vault: Path, state: Path, tmp_path: Path) -> None:
    """05 §1 containment — ``require_in_vault`` is the one gate, and the
    describe path must go through it too."""
    from organize_core.errors import VaultError

    outside = tmp_path / "outside"
    outside.mkdir()
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)

    with pytest.raises(VaultError):
        set_description(ctx, outside, "not in the vault")

    assert not (outside / "index.md").exists()


# ---------------------------------------------------------------------------
# dry run (09 §5.6)
# ---------------------------------------------------------------------------


def test_a_dry_run_over_an_existing_index_note_writes_nothing(
    fixture_vault: Path, state: Path
) -> None:
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config, dry_run=True)
    index_note = fixture_vault / QUIRK_FILES["described_folder_index"]
    before = index_note.read_bytes()

    result = set_description(ctx, fixture_vault / HEALTH_FOLDER, "rehearsed")

    assert (result.ok, result.dry_run) == (True, True)
    assert index_note.read_bytes() == before
    assert not (fixture_vault / ".backups").exists()
    line = parse_log_line(log_lines(ctx)[0])
    assert line is not None and line.dry_run is True
    (record,) = records(ctx)
    assert record["context"]["dry_run"] is True
    # A rehearsal is never a doc-12 precedent.
    assert list(ctx.recorder.query()) == []


def test_a_dry_run_that_would_seed_an_index_note_creates_no_file(
    fixture_vault: Path, state: Path
) -> None:
    """The rehearsal must not report ``ok=False, note does not exist`` for an
    operation that would in fact succeed — that is the silently wrong answer
    09 §1.5 forbids — and it must not create the note either."""
    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config, dry_run=True)
    folder = fixture_vault / UNDESCRIBED_FOLDER

    result = set_description(ctx, folder, "People I keep up with.")

    assert (result.ok, result.dry_run, result.details["created"]) == (True, True, True)
    assert not (folder / "index.md").exists()
    line = parse_log_line(log_lines(ctx)[0])
    assert line is not None
    assert (line.type, line.success, line.dry_run) == ("metadata", True, True)
    # No before-state exists on disk, so there is nothing honest to record.
    assert records(ctx) == []


# ---------------------------------------------------------------------------
# structural
# ---------------------------------------------------------------------------


def test_descriptions_never_rewrite_the_config_file(
    fixture_vault: Path, state: Path, core_paths: Any
) -> None:
    """Phase-4 ruling: the central ``[descriptions]`` table is READ-ONLY from
    the core. A describe that lands in the vault must leave config alone —
    ``OperationContext`` cannot even see ``CorePaths``, and this pins that it
    stays that way."""
    core_paths.config_dir.mkdir(parents=True, exist_ok=True)
    core_paths.config_file.write_text('[vault]\nroot = "x"\n', encoding="utf-8")
    before = core_paths.config_file.read_bytes()

    config = make_config(fixture_vault)
    ctx = make_ctx(fixture_vault, state, config)
    set_description(ctx, fixture_vault / HEALTH_FOLDER, "Health practice.")

    assert core_paths.config_file.read_bytes() == before
    assert not hasattr(ctx, "paths")


def test_routes_never_writes_the_vault_except_through_fileops() -> None:
    """Structural (05 §1.6, ARCHITECTURE decision 1): ``routes`` must not grow
    a second writer. ``atomic_write`` is the ONE fileops primitive it calls
    directly (to seed an index note); everything else goes through an
    operation."""
    source = Path(routes.__file__).read_text(encoding="utf-8")
    for forbidden in ("write_text(", "write_bytes(", "os.replace", "shutil.", ".unlink("):
        assert forbidden not in source, f"routes.py must not call {forbidden} directly"
    assert source.count("atomic_write(") == 1
