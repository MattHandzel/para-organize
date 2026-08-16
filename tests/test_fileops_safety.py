"""The safety invariants of spec 05 §1, asserted as filesystem facts.

"No code path may lose note content" is the product's core promise, so
these are whole-vault property tests rather than per-call spot checks:
every operation runs against a hashed snapshot of the entire fixture vault
and the invariant is checked over the whole tree.

Doc-08 regression obligations covered here: §A14 (one archive signature),
§A15 (archive keeps the filename, rename result checked, config honored),
§A16 (the operation log actually reaches disk), §A24 (tag order/casing),
§A25 (atomic write: cross-filesystem, permissions, temp leaks, same-second
collisions), §A12 (round-trip preservation at the operation level).
"""

from __future__ import annotations

import ast
import errno
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES
from organize_core import fileops
from organize_core.errors import ConcurrentModificationError, NoAiRefusal
from organize_core.fileops import (
    OperationContext,
    append_to_note,
    archive_capture,
    atomic_write,
    is_ai_actor,
    merge_into_note,
    move_to_destination,
    new_folder,
    update_frontmatter,
    update_tags,
    vault_tree,
)
from test_fileops import (
    FIXED_NOW,
    ISO,
    STAMP,
    TODAY,
    FakeIndex,
    action_records,
    log_lines,
    make_config,
    make_ctx,
    record_for,
)

# Each entry runs one operation against the fixture vault. Kept as a table so
# every invariant below is asserted for EVERY operation, not a favourite one.
Operation = Callable[[OperationContext, Path], Any]

OPERATIONS: dict[str, Operation] = {
    "move": lambda ctx, v: move_to_destination(
        ctx, record_for(v, QUIRK_FILES["current_schema"]), v / "projects" / "blog"
    ),
    "move_new_folder": lambda ctx, v: move_to_destination(
        ctx, record_for(v, QUIRK_FILES["scalar_tags"]), v / "projects" / "freshly-made"
    ),
    "archive": lambda ctx, v: archive_capture(ctx, record_for(v, QUIRK_FILES["iso_filename"])),
    "merge": lambda ctx, v: merge_into_note(
        ctx, record_for(v, QUIRK_FILES["current_schema"]), v / QUIRK_FILES["merge_target"]
    ),
    "append": lambda ctx, v: append_to_note(
        ctx, record_for(v, QUIRK_FILES["current_schema"]), v / QUIRK_FILES["merge_target"]
    ),
    "update_frontmatter": lambda ctx, v: update_frontmatter(
        ctx, v / QUIRK_FILES["current_schema"], {"importance": "high"}
    ),
    "update_tags": lambda ctx, v: update_tags(
        ctx, v / QUIRK_FILES["merge_target"], ["new-tag", "Blog-Idea"]
    ),
    "new_folder": lambda ctx, v: new_folder(ctx, "resources", "brand-new-topic"),
}


def tree_dirs(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir()}


# --- INVARIANT 1: never delete (05 §1.1/§1.2) ------------------------------


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_no_vault_file_is_ever_destroyed(
    name: str, fixture_vault: Path, tmp_path: Path
) -> None:
    """No path that existed before an operation may simply disappear: it is
    either still there (possibly edited in place, with a backup of the old
    bytes — see the backup invariant below) or its EXACT bytes are present
    somewhere else in the vault, which is the archive.

    ``.backups/`` is excluded from the "somewhere else" pool on purpose: a
    backup is a safety net, not the archive. If the only remaining copy of a
    removed note were its backup, the operation lost the note.
    """
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    backups = ctx.config.file_ops.backup_dir
    before = vault_tree(fixture_vault, skip=(backups,))
    assert before, "the fixture vault must not be empty"

    OPERATIONS[name](ctx, fixture_vault)

    after = vault_tree(fixture_vault, skip=(backups,))
    surviving_hashes = set(after.values())
    removed = [rel for rel in before if rel not in after]
    for rel in removed:
        assert before[rel] in surviving_hashes, (
            f"{name}: {rel} vanished from the vault and its bytes survive only in {backups}/ "
            "(or not at all) — the never-delete invariant (05 §1.1) is broken"
        )


def test_the_archived_original_is_a_verified_copy(fixture_vault: Path, tmp_path: Path) -> None:
    """The ONE removal in the system is allowed only after the bytes are
    provably present at the archive path (05 §1.2 copy-then-archive)."""
    import hashlib

    ctx = make_ctx(fixture_vault, tmp_path / "state")
    source = fixture_vault / QUIRK_FILES["current_schema"]
    source_bytes = source.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()

    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), fixture_vault / "areas/health"
    )
    assert result.ok is True

    archived = Path(result.details["archive_path"])
    assert archived.is_file()
    assert archived.name == source.name, "wikilinks resolve by filename (08 §A15)"
    assert archived.read_bytes() == source_bytes
    assert hashlib.sha256(archived.read_bytes()).hexdigest() == source_hash
    assert not source.exists()


def test_archiving_has_one_signature_and_one_implementation() -> None:
    """08 §A14: ``archive_capture`` was written for a metadata table and
    called with a bare string from 3 of 5 sites — the caller groups
    disagreed and it crashed. One signature, one implementation, forever."""
    signature = inspect.signature(fileops.archive_capture)
    assert list(signature.parameters) == ["ctx", "capture"]
    assert signature.parameters["capture"].annotation == "NoteRecord"

    tree = ast.parse(inspect.getsource(fileops))
    callers: set[str] = set()

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "_archive_file"
            ):
                callers.add(scope)
            walk(child, scope)

    walk(tree, "<module>")
    assert callers == {"move_to_destination", "archive_capture", "merge_into_note"}


def test_a_silently_failed_rename_is_reported_not_swallowed(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """08 §A15: the original ignored ``os.rename``'s result, so archiving
    silently no-opped across filesystems and the "archived" note was never
    archived."""
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    source = fixture_vault / QUIRK_FILES["iso_filename"]
    before = source.read_bytes()

    monkeypatch.setattr(fileops, "_replace", lambda src, dst: None)  # a silent no-op
    result = archive_capture(ctx, record_for(fixture_vault, QUIRK_FILES["iso_filename"]))

    assert result.ok is False
    assert "does not exist after the move" in (result.error or "")
    assert source.read_bytes() == before
    assert log_lines(ctx)[-1].endswith(f"Error: {result.error}")
    assert "[FAILED]" in log_lines(ctx)[-1]


def test_only_atomic_write_and_archive_may_remove_a_path() -> None:
    """Structural never-delete guard: a future edit that adds an unlink /
    rmtree / rename anywhere else in fileops.py fails this test."""
    tree = ast.parse(inspect.getsource(fileops))
    removers = {"unlink", "remove", "removedirs", "rmdir", "rmtree", "rename"}
    allowed = {"atomic_write", "_archive_file"}
    offenders: list[tuple[str, str, int]] = []

    def walk(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = None
                if isinstance(func, ast.Attribute):
                    if func.attr in removers:
                        name = func.attr
                    elif func.attr == "replace" and isinstance(func.value, ast.Name) and func.value.id in {
                        "os",
                        "shutil",
                    }:
                        name = "os.replace"  # str.replace is not a filesystem call
                elif isinstance(func, ast.Name) and func.id == "_replace":
                    name = "_replace"
                if name is not None and scope not in allowed:
                    offenders.append((scope, name, child.lineno))
            walk(child, scope)

    walk(tree, "<module>")
    assert offenders == [], (
        "removal/rename calls outside the two sanctioned functions: "
        f"{offenders} — see the never-delete invariant in the module docstring"
    )


# --- INVARIANT 2: atomic writes and the kill window (05 §1.3, §9) ----------


def test_kill_between_write_and_rename_leaves_the_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """05 §9: kill during write leaves either old or new content, never
    partial — and no temp droppings after the failure."""
    target = tmp_path / "a.md"
    target.write_text("old content\n", encoding="utf-8")

    def die(src: str, dst: str) -> None:
        raise OSError(errno.EIO, "simulated crash between write and rename")

    monkeypatch.setattr(fileops, "_replace", die)
    with pytest.raises(OSError):
        atomic_write(target, "brand new content that must not land\n")

    assert target.read_text(encoding="utf-8") == "old content\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.md"], "temp file was left behind"


def test_kill_between_write_and_rename_does_not_create_a_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "fresh.md"

    def die(src: str, dst: str) -> None:
        raise OSError(errno.EIO, "simulated crash between write and rename")

    monkeypatch.setattr(fileops, "_replace", die)
    with pytest.raises(OSError):
        atomic_write(target, "never lands\n")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_crashed_move_leaves_the_capture_untouched(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    source = fixture_vault / QUIRK_FILES["current_schema"]
    before = vault_tree(fixture_vault)

    def die(src: str, dst: str) -> None:
        raise OSError(errno.EIO, "simulated crash between write and rename")

    monkeypatch.setattr(fileops, "_replace", die)
    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), fixture_vault / "projects/blog"
    )

    assert result.ok is False
    assert source.exists()
    assert not (fixture_vault / "projects/blog/2026-06-10T21:33:05.379Z.md").exists()
    after = vault_tree(fixture_vault)
    # only the backup of the source was added; nothing was removed or altered
    added = set(after) - set(before)
    assert added == {f".backups/{STAMP}_2026-06-10T21:33:05.379Z.md"}
    assert {k: v for k, v in after.items() if k in before} == before


def test_atomic_write_of_unicode_and_invalid_bytes_never_crashes(tmp_path: Path) -> None:
    target = tmp_path / "café ☕.md"
    atomic_write(target, "smart “quotes” and an em—dash\n")
    assert target.read_text(encoding="utf-8") == "smart “quotes” and an em—dash\n"


# --- INVARIANT 3: concurrent modification (10 §4) --------------------------


def test_a_concurrent_writer_between_read_and_write_is_refused(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject the modification INSIDE the operation, between the read and
    the write, which is the window the guard exists for."""
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    path = fixture_vault / QUIRK_FILES["current_schema"]
    intruder = "---\nid: someone-else\n---\nSyncthing got here first\n"
    real_serialize = fileops.serialize
    fired: list[int] = []

    def sneaky(doc: Any) -> str:
        if not fired:
            fired.append(1)
            path.write_text(intruder, encoding="utf-8")
        return real_serialize(doc)

    monkeypatch.setattr(fileops, "serialize", sneaky)
    with pytest.raises(ConcurrentModificationError) as excinfo:
        update_frontmatter(ctx, path, {"importance": "high"})

    assert str(path) in str(excinfo.value)
    assert excinfo.value.hint is not None
    assert path.read_text(encoding="utf-8") == intruder, "the concurrent write must not be clobbered"
    assert not (fixture_vault / ".backups").exists(), "a refused op leaves no junk behind"
    assert action_records(ctx) == []


def test_a_touched_capture_is_refused_by_move(fixture_vault: Path, tmp_path: Path) -> None:
    """A write landing AFTER the operation's own read is refused (10 §4).

    The injection point is `_read_document` — the one place a snapshot is
    taken — because the snapshot is now derived from the bytes that read
    returned. The old version wrapped `snapshot_file`, which move no longer
    calls; that made the window it was pinning invisible to the test AND to
    the guard (see `test_a_write_between_the_read_and_the_commit_is_refused`).
    """
    import os

    ctx = make_ctx(fixture_vault, tmp_path / "state")
    path = fixture_vault / QUIRK_FILES["current_schema"]
    before = vault_tree(fixture_vault)
    real_read = fileops._read_document

    def touch_after_read(target: Path) -> Any:
        result = real_read(target)
        stat = Path(target).stat()
        os.utime(target, (stat.st_mtime + 10, stat.st_mtime + 10))
        return result

    fileops._read_document = touch_after_read  # type: ignore[assignment]
    try:
        with pytest.raises(ConcurrentModificationError):
            move_to_destination(
                ctx,
                record_for(fixture_vault, QUIRK_FILES["current_schema"]),
                fixture_vault / "projects/blog",
            )
    finally:
        fileops._read_document = real_read  # type: ignore[assignment]

    assert path.exists()
    assert set(vault_tree(fixture_vault)) == set(before)


# --- INVARIANT 4: dry run mutates NOTHING (09 §5.6) ------------------------


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_dry_run_leaves_the_vault_byte_identical(
    name: str, fixture_vault: Path, tmp_path: Path
) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state", dry_run=True)
    before_files = vault_tree(fixture_vault)
    before_dirs = tree_dirs(fixture_vault)

    result = OPERATIONS[name](ctx, fixture_vault)

    assert result.ok is True, result.error
    assert result.dry_run is True
    assert vault_tree(fixture_vault) == before_files
    assert tree_dirs(fixture_vault) == before_dirs
    assert not (fixture_vault / ".backups").exists()
    assert result.backup_path is None


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_dry_run_still_logs_and_records(name: str, fixture_vault: Path, tmp_path: Path) -> None:
    """09 §5.6 calls this "a dry-run/log-only mode … diff intended actions
    against expectations" — which requires the intended actions on paper."""
    ctx = make_ctx(fixture_vault, tmp_path / "state", dry_run=True)
    OPERATIONS[name](ctx, fixture_vault)

    lines = log_lines(ctx)
    assert lines, f"{name}: dry run wrote no operation log line"
    assert all(line.startswith(f"[{ISO}] ") for line in lines)
    assert all("[DRY-RUN]" in line for line in lines)

    records = action_records(ctx)
    assert len(records) == 1
    # The dry-run marker is a first-class ActionContext field (spec 12 §2 +
    # the integrator's seam ruling), not a convention buried in `filters`.
    assert records[0]["context"]["dry_run"] is True
    assert records[0]["context"]["filters"] == {}
    assert records[0]["ts"] == ISO


def test_dry_run_move_describes_what_would_happen(fixture_vault: Path, tmp_path: Path) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state", dry_run=True)
    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), fixture_vault / "areas/health"
    )
    assert result.destination == str(fixture_vault / "areas/health/2026-06-10T21:33:05.379Z.md")
    assert result.details == {
        "archive_path": str(fixture_vault / "archive/capture/raw_capture/2026-06-10T21:33:05.379Z.md"),
        "tag_added": "area/health",
        "processing_status": "organized",
        "last_edited_date": TODAY,
    }


def test_dry_run_never_touches_the_index(fixture_vault: Path, tmp_path: Path) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state", dry_run=True)
    for run in OPERATIONS.values():
        run(ctx, fixture_vault)
    index: FakeIndex = ctx.index  # type: ignore[assignment]
    assert index.updated == []
    assert index.removed == []


# --- INVARIANT 5: the no-ai vault law (spec 02) ----------------------------


@pytest.mark.parametrize(
    ("actor", "automated"),
    [
        ("matt", False),
        ("user", False),
        ("human:matt", False),
        ("claude-integrate", True),
        ("consumer:learn", True),
        ("route:workout", True),
        ("auto-organize", True),
    ],
)
def test_is_ai_actor_table(actor: str, automated: bool) -> None:
    assert is_ai_actor(actor) is automated


@pytest.mark.parametrize("actor", ["claude-integrate", "consumer:learn", "route:workout", "auto-organize"])
def test_automated_actors_may_not_write_a_no_ai_note(
    actor: str, fixture_vault: Path, tmp_path: Path
) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state", actor=actor)
    no_ai = fixture_vault / QUIRK_FILES["no_ai"]
    before = no_ai.read_bytes()
    capture = record_for(fixture_vault, QUIRK_FILES["current_schema"])

    with pytest.raises(NoAiRefusal):
        update_frontmatter(ctx, no_ai, {"importance": "high"})
    with pytest.raises(NoAiRefusal):
        update_tags(ctx, no_ai, ["extra"])
    with pytest.raises(NoAiRefusal):
        move_to_destination(ctx, record_for(fixture_vault, QUIRK_FILES["no_ai"]), fixture_vault / "areas/health")
    with pytest.raises(NoAiRefusal):
        merge_into_note(ctx, capture, no_ai)
    with pytest.raises(NoAiRefusal):
        append_to_note(ctx, capture, no_ai)

    assert no_ai.read_bytes() == before
    assert (fixture_vault / QUIRK_FILES["current_schema"]).exists()
    assert action_records(ctx) == []


def test_the_no_ai_refusal_names_the_file_and_the_actor(
    fixture_vault: Path, tmp_path: Path
) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state", actor="consumer:learn")
    with pytest.raises(NoAiRefusal) as excinfo:
        update_frontmatter(ctx, fixture_vault / QUIRK_FILES["no_ai"], {"a": 1})
    message = str(excinfo.value)
    assert str(fixture_vault / QUIRK_FILES["no_ai"]) in message
    assert "consumer:learn" in message
    assert "no-ai" in message
    assert excinfo.value.hint is not None


def test_matt_may_organize_a_no_ai_note_and_the_field_survives(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Spec 02: "the interactive plugin acts on Matt's explicit keystrokes
    and may move such files, but must preserve the field"."""
    ctx = make_ctx(fixture_vault, tmp_path / "state", actor="matt")
    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["no_ai"]), fixture_vault / "areas/health"
    )
    assert result.ok is True
    moved = (fixture_vault / "areas/health/private-thought.md").read_text(encoding="utf-8")
    assert "no-ai: true\n" in moved
    assert "tags:\n- journal\n- area/health\n" in moved


def test_an_automated_actor_may_still_archive_a_no_ai_note(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Pure relocation writes nothing into the note, so the vault law does
    not bite — and the bytes are provably unchanged."""
    ctx = make_ctx(fixture_vault, tmp_path / "state", actor="consumer:learn")
    source = fixture_vault / QUIRK_FILES["no_ai"]
    before = source.read_bytes()
    result = archive_capture(ctx, record_for(fixture_vault, QUIRK_FILES["no_ai"]))
    assert result.ok is True
    assert Path(result.destination).read_bytes() == before  # type: ignore[arg-type]


# --- INVARIANT 6: every completed op is recorded (12 §2) -------------------


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_every_operation_emits_exactly_one_action_record(
    name: str, fixture_vault: Path, tmp_path: Path
) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    result = OPERATIONS[name](ctx, fixture_vault)
    assert result.ok is True, result.error

    records = action_records(ctx)
    assert len(records) == 1
    rec = records[0]
    assert rec["schema_version"] == 1
    assert rec["id"].startswith("act_")
    assert rec["ts"] == ISO
    assert rec["actor"] == "matt"
    assert rec["operation"] in {
        "move",
        "merge",
        "append",
        "archive",
        "meta_edit",
        "tag_edit",
        "create_folder",
    }
    assert rec["context"]["session_id"] == "ses_test"
    assert isinstance(rec["capture"], dict)
    assert rec["capture"]["path"]


def test_a_broken_recorder_never_costs_the_operation(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """12 §2: recording failures must not block the operation."""
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    blocker = tmp_path / "state" / "actions"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("i am a file where the actions dir should be", encoding="utf-8")

    result = archive_capture(ctx, record_for(fixture_vault, QUIRK_FILES["iso_filename"]))
    assert result.ok is True
    assert Path(result.destination).is_file()  # type: ignore[arg-type]


def test_a_broken_index_never_costs_the_operation(fixture_vault: Path, tmp_path: Path) -> None:
    class ExplodingIndex:
        def update_file(self, path: Path) -> None:
            raise RuntimeError("index is mid-rebuild")

        def remove_file(self, path: Path) -> None:
            raise RuntimeError("index is mid-rebuild")

        def stats(self) -> dict[str, int]:
            raise NotImplementedError

    ctx = make_ctx(fixture_vault, tmp_path / "state")
    ctx.index = ExplodingIndex()  # type: ignore[assignment]
    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), fixture_vault / "projects/blog"
    )
    assert result.ok is True
    assert action_records(ctx)[0]["context"]["vault_stats"] == {}


# --- INVARIANT 7: backups exist for every mutation of an existing file -----


@pytest.mark.parametrize(
    "name", ["move", "move_new_folder", "archive", "merge", "append", "update_frontmatter", "update_tags"]
)
def test_every_mutation_of_an_existing_file_is_backed_up_first(
    name: str, fixture_vault: Path, tmp_path: Path
) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    result = OPERATIONS[name](ctx, fixture_vault)
    assert result.ok is True
    assert result.backup_path is not None, f"{name} mutated a file without a backup path"
    backup = Path(result.backup_path)
    assert backup.is_file()
    assert backup.parent == fixture_vault / ".backups"
    assert backup.name.startswith(f"{STAMP}_")


def test_the_backup_holds_the_pre_mutation_bytes(fixture_vault: Path, tmp_path: Path) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    target = fixture_vault / QUIRK_FILES["merge_target"]
    before = target.read_bytes()
    result = merge_into_note(
        ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), target
    )
    assert Path(result.backup_path).read_bytes() == before  # type: ignore[arg-type]
    assert target.read_bytes() != before


def test_the_backup_dir_is_configurable(fixture_vault: Path, tmp_path: Path) -> None:
    config = make_config(fixture_vault, backup_dir=".snapshots")
    ctx = make_ctx(fixture_vault, tmp_path / "state", config=config)
    result = update_frontmatter(ctx, fixture_vault / QUIRK_FILES["current_schema"], {"a": 1})
    assert Path(result.backup_path).parent == fixture_vault / ".snapshots"  # type: ignore[arg-type]


# --- INVARIANT 8: the operation log is real and undoable (05 §1.5/§8) ------


def test_the_operation_log_carries_everything_manual_undo_needs(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """08 §A16: file logging was entirely dead. 05 §8: the log plus backups
    must make every operation manually reversible."""
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), fixture_vault / "projects/blog"
    )

    assert ctx.oplog.log_file.is_file()
    undo = ctx.oplog.undo_info()
    assert undo is not None
    assert undo.type == "move"
    assert undo.src == str(fixture_vault / QUIRK_FILES["current_schema"])
    assert undo.dst == result.destination
    assert undo.backup == result.backup_path
    assert Path(undo.backup).is_file()  # type: ignore[arg-type]

    # the archive line names where the original actually went
    archive_line = ctx.oplog.recent(10)[0]
    assert archive_line.type == "archive"
    assert archive_line.dst == result.details["archive_path"]


def test_a_fresh_process_reads_the_log_from_disk(fixture_vault: Path, tmp_path: Path) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    archive_capture(ctx, record_for(fixture_vault, QUIRK_FILES["iso_filename"]))

    reopened = fileops.OperationLog(ctx.oplog.log_file)
    assert len(reopened.recent(50)) == 1
    assert reopened.undo_info() is not None
    assert reopened.undo_info().type == "archive"  # type: ignore[union-attr]


# --- INVARIANT 9: no shelling out (05 §1.6, 08 §A28) -----------------------


def test_fileops_never_shells_out() -> None:
    """05 §1.6 / 08 §A28: six ``io.popen("find …")`` sites, three with
    unquoted paths. The rewrite does all filesystem work in-process."""
    source = inspect.getsource(fileops)
    for banned in ("subprocess", "os.system", "os.popen", "shell=True", "popen("):
        assert banned not in source, f"fileops must not shell out (found {banned!r})"


def test_fileops_never_reads_the_environment_or_home() -> None:
    """Structural safety decision 4: nothing consults os.environ or
    Path.home() outside paths.py."""
    source = inspect.getsource(fileops)
    for banned in ("os.environ", "getenv", "Path.home()", "expanduser"):
        assert banned not in source, f"fileops must take paths from config/CorePaths (found {banned!r})"


def test_every_text_io_declares_encoding_and_replacement() -> None:
    """06 §6 / the B1 outage class: strict decoding of real-world bytes is
    how the pipeline died for three months."""
    tree = ast.parse(inspect.getsource(fileops))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"open", "read_text", "write_text", "fdopen"}:
            continue
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os":
            if func.attr == "open":
                continue  # os.open() is a file descriptor, not a text stream
        mode = next(
            (a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)),
            "",
        )
        if "b" in mode:
            continue  # binary I/O has no encoding
        kwargs = {kw.arg for kw in node.keywords}
        assert "encoding" in kwargs, f"{name}() at line {node.lineno} has no encoding="
        assert "errors" in kwargs, f"{name}() at line {node.lineno} has no errors="
        checked += 1
    assert checked >= 4, f"the I/O guard only inspected {checked} calls — it has gone blind"


def test_operations_survive_invalid_utf8_in_a_capture(
    fixture_vault: Path, tmp_path: Path
) -> None:
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    source = fixture_vault / QUIRK_FILES["invalid_utf8"]
    raw = source.read_bytes()
    result = archive_capture(ctx, record_for(fixture_vault, QUIRK_FILES["invalid_utf8"]))
    assert result.ok is True
    # archiving relocates BYTES, so even the undecodable ones survive intact
    assert Path(result.destination).read_bytes() == raw  # type: ignore[arg-type]


def test_the_clock_is_injected_not_ambient(fixture_vault: Path, tmp_path: Path) -> None:
    """Determinism check: two runs at the same pinned instant produce byte
    identical archive/backup names."""
    ctx_a = make_ctx(fixture_vault, tmp_path / "a", now=FIXED_NOW)
    ctx_b = make_ctx(fixture_vault, tmp_path / "b", now=FIXED_NOW)
    a = archive_capture(ctx_a, record_for(fixture_vault, QUIRK_FILES["iso_filename"]))
    b = archive_capture(ctx_b, record_for(fixture_vault, QUIRK_FILES["scalar_tags"]))
    assert Path(a.backup_path).name.startswith(STAMP)  # type: ignore[arg-type]
    assert Path(b.backup_path).name.startswith(STAMP)  # type: ignore[arg-type]
    assert log_lines(ctx_a)[0].startswith(f"[{ISO}] ")
    assert log_lines(ctx_b)[0].startswith(f"[{ISO}] ")


# ===========================================================================
# Phase-1 fix pass: each test below fails against the code as it was.
# ===========================================================================


# --- F1: the concurrent-modification guard is armed AT the read ------------


def test_a_write_between_the_read_and_the_commit_is_refused(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """spec 10 §4 / module docstring: "mtime+hash captured at read".

    Every mutating op used to READ the file and only THEN call
    `snapshot_file()`, so a concurrent write landing in that window was
    invisible: the op overwrote the file with content derived from the stale
    read and returned ok=True with no ConcurrentModificationError. With
    `create_backups = false` that is unrecoverable loss. The window was not
    theoretical — merge parsed a whole second document inside it.

    The injection is at `_read_document`, the one place a snapshot is now
    taken, and it changes CONTENT (not just mtime) so the hash half of the
    guard is exercised too.
    """
    marker = "CONCURRENT WRITE THAT MUST NOT BE LOST\n"
    target = fixture_vault / QUIRK_FILES["merge_target"]
    config = make_config(fixture_vault, create_backups=False)
    ctx = make_ctx(fixture_vault, tmp_path / "state", config=config)
    real_read = fileops._read_document

    def write_after_read(path: Path) -> Any:
        result = real_read(path)
        if Path(path) == target:  # simulate the other writer
            target.write_text(target.read_text(encoding="utf-8") + marker, encoding="utf-8")
        return result

    fileops._read_document = write_after_read  # type: ignore[assignment]
    try:
        with pytest.raises(ConcurrentModificationError):
            merge_into_note(
                ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), target
            )
    finally:
        fileops._read_document = real_read  # type: ignore[assignment]

    assert marker in target.read_text(encoding="utf-8"), "the concurrent write was clobbered"
    assert action_records(ctx) == []


@pytest.mark.parametrize("operation", ["merge", "append", "update_frontmatter"])
def test_every_rewriting_op_arms_the_guard_at_its_own_read(
    operation: str, fixture_vault: Path, tmp_path: Path
) -> None:
    """The same hole, for every operation that rewrites an existing file."""
    config = make_config(fixture_vault, create_backups=False)
    ctx = make_ctx(fixture_vault, tmp_path / "state", config=config)
    real_read = fileops._read_document
    touched: list[Path] = []

    def write_after_read(path: Path) -> Any:
        result = real_read(path)
        if not touched:  # only the FIRST read, i.e. the file being rewritten
            touched.append(Path(path))
            Path(path).write_text(
                Path(path).read_text(encoding="utf-8") + "INTRUDER\n", encoding="utf-8"
            )
        return result

    fileops._read_document = write_after_read  # type: ignore[assignment]
    try:
        with pytest.raises(ConcurrentModificationError):
            OPERATIONS[operation](ctx, fixture_vault)
    finally:
        fileops._read_document = real_read  # type: ignore[assignment]

    assert "INTRUDER" in touched[0].read_text(encoding="utf-8")


def test_the_snapshot_hash_is_the_bytes_that_were_read(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """`_read_document` returns a snapshot of the bytes it read, not a second
    stat+hash of the file — which is what closes the window above."""
    path = fixture_vault / QUIRK_FILES["merge_target"]
    import hashlib

    _doc, text, snapshot = fileops._read_document(path)
    assert snapshot.path == str(path)
    assert snapshot.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert snapshot.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    fileops.check_unmodified(snapshot)  # matches disk, so it must not raise


# --- F2: the archive must never be clobbered ------------------------------


def test_move_into_the_archive_capture_folder_is_refused(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """`dest_path` and `archive_path` both resolve to <archive>/<filename>
    when the destination IS the archive capture folder, so the organized copy
    was written there and then silently overwritten by `_archive_file`'s
    os.replace of the UN-organized original — while the result claimed
    ok=True with `tag_added` and `processing_status: organized`."""
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    archive_folder = fixture_vault / "archive" / "capture" / "raw_capture"
    before = vault_tree(fixture_vault)

    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["metadata_empty_map"]), archive_folder
    )

    assert result.ok is False
    assert "archive capture folder" in (result.error or "")
    assert vault_tree(fixture_vault) == before, "a refused move must change nothing"
    assert action_records(ctx) == []


def test_archiving_never_overwrites_an_existing_archive_file(fixture_vault: Path) -> None:
    """`_archive_file`'s os.replace is an unconditional overwrite.
    `get_archive_path` picks a free name, but that check and the rename are
    not one operation — anything that appears in between must not be
    destroyed (05 §1.1 never delete)."""
    source = fixture_vault / QUIRK_FILES["scalar_tags"]
    archive_path = fixture_vault / "archive" / "capture" / "raw_capture" / source.name
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text("SOMEONE ELSE'S FILE\n", encoding="utf-8")

    archived, error = fileops._archive_file(source, archive_path)

    assert archived is None
    assert "already exists" in (error or "")
    assert archive_path.read_text(encoding="utf-8") == "SOMEONE ELSE'S FILE\n"
    assert source.exists(), "the original must survive a refused archive"


# --- F3: no-ai protects the CAPTURE in merge/append too -------------------


@pytest.mark.parametrize("actor", ["claude-integrate", "consumer:auto_tagger", "route:workout"])
@pytest.mark.parametrize("operation", ["merge", "append"])
def test_an_ai_actor_cannot_copy_no_ai_content_into_another_note(
    actor: str, operation: str, fixture_vault: Path, tmp_path: Path
) -> None:
    """Vault law (spec 02). `_refuse_no_ai` was applied to the TARGET only,
    so an automated actor correctly could not MOVE a `no-ai: true` capture
    but could merge/append its body into an ordinary note — copying the
    protected content out of its container into a note carrying no such
    marking. "Pure relocation … changes no note content" justifies the
    exemption for `archive_capture`; it is simply not true of merge/append.
    """
    ctx = make_ctx(fixture_vault, tmp_path / "state", actor=actor)
    capture = record_for(fixture_vault, QUIRK_FILES["no_ai"])
    target = fixture_vault / QUIRK_FILES["merge_target"]
    before = vault_tree(fixture_vault)
    protected_body = (fixture_vault / QUIRK_FILES["no_ai"]).read_text(encoding="utf-8")

    with pytest.raises(NoAiRefusal):
        if operation == "merge":
            merge_into_note(ctx, capture, target)
        else:
            append_to_note(ctx, capture, target)

    assert vault_tree(fixture_vault) == before
    for line in protected_body.splitlines():
        if len(line) > 20 and not line.startswith(("---", "no-ai", "tags", "id")):
            assert line not in target.read_text(encoding="utf-8")


def test_a_human_actor_may_still_merge_a_no_ai_capture(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The law binds automated tooling, not Matt at a keyboard (spec 02:
    "the interactive plugin acts on Matt's explicit keystrokes")."""
    ctx = make_ctx(fixture_vault, tmp_path / "state", actor="matt")
    result = merge_into_note(
        ctx,
        record_for(fixture_vault, QUIRK_FILES["no_ai"]),
        fixture_vault / QUIRK_FILES["merge_target"],
    )
    assert result.ok is True, result.error


# --- F4: the operation log must name paths that actually exist ------------


@pytest.mark.parametrize(
    "filename",
    [
        "two  spaces\tand tab.md",
        "a -> b.md",
        "has Backup: colon.md",
        "has Error: colon.md",
    ],
)
def test_the_log_round_trips_a_path_that_contains_grammar_characters(
    filename: str, fixture_vault: Path, tmp_path: Path
) -> None:
    """spec 05 §1.5: "The log must contain enough to manually undo any
    operation"; §8: "every operation manually reversible".

    `format_log_line` used to push src/dst/backup through a whitespace
    collapser, so a note whose name held a tab, a newline or a doubled space
    was logged — and reported by `undo_info()` — under a path that does not
    exist. ` -> ` inside a filename additionally mis-split `parse_log_line`.
    """
    rel = f"capture/raw_capture/{filename}"
    (fixture_vault / rel).write_text("---\nid: weird\n---\nbody\n", encoding="utf-8")
    ctx = make_ctx(fixture_vault, tmp_path / "state")

    result = move_to_destination(
        ctx, record_for(fixture_vault, rel), fixture_vault / "projects" / "blog"
    )
    assert result.ok is True, result.error

    lines = log_lines(ctx)
    assert lines and all("\n" not in line for line in lines)

    undo = ctx.oplog.undo_info()
    assert undo is not None
    assert Path(undo.src).name == filename
    assert Path(undo.dst).exists(), f"the log names a destination that does not exist: {undo.dst}"
    assert undo.backup is not None and Path(undo.backup).exists()

    parsed = [fileops.parse_log_line(line) for line in lines]
    assert all(op is not None for op in parsed)
    archive_line = next(op for op in parsed if op is not None and op.type == "archive")
    assert Path(archive_line.dst).exists()


def test_log_escaping_round_trips_every_grammar_character() -> None:
    """Unit-level inverse property for the escape scheme itself."""
    hostile = "a -> b\tc\nd  e Backup: f Error: g \\h"
    op = fileops.LoggedOperation(
        ts=ISO, type="move", src=hostile, dst=hostile, success=True, backup=hostile
    )
    line = fileops.format_log_line(op)
    assert "\n" not in line and "\r" not in line
    parsed = fileops.parse_log_line(line)
    assert parsed is not None
    assert parsed.src == hostile
    assert parsed.dst == hostile
    assert parsed.backup == hostile


# --- F5: undecodable bytes refuse on the MUTATION path --------------------


@pytest.mark.parametrize("operation", ["move", "merge", "append"])
def test_a_note_with_invalid_utf8_refuses_to_be_rewritten(
    operation: str, fixture_vault: Path, tmp_path: Path
) -> None:
    """errors.py contract: "scans tolerate, mutations refuse".

    `_read_text` decoded with errors="replace" on the MUTATION path too, so
    moving a note containing non-UTF-8 bytes wrote a destination copy whose
    body bytes had been permanently replaced with U+FFFD — and reported
    ok=True with no warning of any kind. Broken YAML already refused; bad
    bytes silently did not.
    """
    from organize_core.errors import FrontmatterError

    ctx = make_ctx(fixture_vault, tmp_path / "state")
    capture = record_for(fixture_vault, QUIRK_FILES["invalid_utf8"])
    before = vault_tree(fixture_vault)

    with pytest.raises(FrontmatterError) as excinfo:
        if operation == "move":
            move_to_destination(ctx, capture, fixture_vault / "projects" / "blog")
        elif operation == "merge":
            merge_into_note(ctx, capture, fixture_vault / QUIRK_FILES["merge_target"])
        else:
            append_to_note(ctx, capture, fixture_vault / QUIRK_FILES["merge_target"])

    assert "not valid UTF-8" in str(excinfo.value)
    assert excinfo.value.hint is not None
    assert vault_tree(fixture_vault) == before, "a refused rewrite must change nothing"


def test_archiving_still_tolerates_invalid_utf8(fixture_vault: Path, tmp_path: Path) -> None:
    """The refusal above must NOT cost the pure-relocation path: archiving
    moves bytes and is the way such a note stays organizable."""
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    raw = (fixture_vault / QUIRK_FILES["invalid_utf8"]).read_bytes()
    result = archive_capture(ctx, record_for(fixture_vault, QUIRK_FILES["invalid_utf8"]))
    assert result.ok is True
    assert Path(result.destination).read_bytes() == raw  # type: ignore[arg-type]


# --- spec 05 §1.3: preserve file permissions ------------------------------


def test_a_move_preserves_the_source_notes_permissions(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """`atomic_write` read the mode from the DESTINATION, which does not
    exist yet for a move — so every file it created landed 0600 while the
    rest of the Syncthing-synced vault stayed 0644."""
    import os

    source = fixture_vault / QUIRK_FILES["metadata_empty_map"]
    # 0o640 on purpose: it is neither the old hardcoded 0o600 NOR the
    # umask default (0o644), so only genuine preservation passes.
    os.chmod(source, 0o640)
    ctx = make_ctx(fixture_vault, tmp_path / "state")

    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["metadata_empty_map"]), fixture_vault / "projects" / "blog"
    )

    assert result.ok is True, result.error
    assert os.stat(result.destination).st_mode & 0o777 == 0o640  # type: ignore[arg-type]
    assert os.stat(result.details["archive_path"]).st_mode & 0o777 == 0o640


def test_atomic_write_of_a_new_file_uses_the_platform_default_mode(tmp_path: Path) -> None:
    """With no explicit mode and no existing target, the temp is created with
    0o666 so the umask — not a hardcoded 0o600 — decides."""
    import os

    target = tmp_path / "fresh.md"
    atomic_write(target, "body\n")
    umask = os.umask(0o022)
    os.umask(umask)
    assert os.stat(target).st_mode & 0o777 == (0o666 & ~umask)


def test_atomic_write_honors_an_explicit_mode(tmp_path: Path) -> None:
    import os

    target = tmp_path / "explicit.md"
    atomic_write(target, "body\n", mode=0o600)
    assert os.stat(target).st_mode & 0o777 == 0o600


# --- spec 05 §1: containment. The core never writes outside the vault -----


@pytest.mark.parametrize(
    "destination",
    ["../OUTSIDE", "<ABSOLUTE>", "projects/../../OUTSIDE"],
)
def test_a_move_outside_the_vault_is_refused(
    destination: str, fixture_vault: Path, tmp_path: Path
) -> None:
    """`fileops` is the only writer, so containment lives here and BOTH doors
    inherit it (ARCHITECTURE ruling #19). The RPC door refused these; the CLI
    happily wrote note content to them and exited 0."""
    from organize_core.errors import VaultError

    ctx = make_ctx(fixture_vault, tmp_path / "state")
    before = vault_tree(fixture_vault)
    # Hermetic: never a shared /tmp name that a previous run could have left.
    target = tmp_path / "ESCAPED" if destination == "<ABSOLUTE>" else fixture_vault / destination

    with pytest.raises(VaultError) as excinfo:
        move_to_destination(ctx, record_for(fixture_vault, QUIRK_FILES["scalar_tags"]), target)

    assert "outside the vault" in str(excinfo.value)
    assert excinfo.value.hint is not None
    assert vault_tree(fixture_vault) == before
    assert not Path(str(target)).exists()


def test_a_merge_into_a_file_outside_the_vault_is_refused(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The same door, for the op that MUTATES an arbitrary existing file."""
    from organize_core.errors import VaultError

    outside = tmp_path / "private.md"
    outside.write_text("---\ntitle: private\n---\nMY PRIVATE NOTES\n", encoding="utf-8")
    ctx = make_ctx(fixture_vault, tmp_path / "state")

    with pytest.raises(VaultError):
        merge_into_note(ctx, record_for(fixture_vault, QUIRK_FILES["scalar_tags"]), outside)

    assert outside.read_text(encoding="utf-8") == "---\ntitle: private\n---\nMY PRIVATE NOTES\n"


# --- spec 05 §1.3: orphaned temp files are visible ------------------------


def test_orphaned_atomic_write_temps_are_findable(fixture_vault: Path) -> None:
    """A SIGKILL mid-write leaves a full-size hidden `.organize-tmp` in the
    vault, and the vault is Syncthing-synced — so the dropping replicates
    everywhere. Nothing swept them and `organize health` did not look."""

    fresh = fixture_vault / "projects" / ".note.md.999.0.organize-tmp"
    fresh.write_text("half a write\n", encoding="utf-8")
    now = fresh.stat().st_mtime

    assert fileops.find_orphaned_temp_files(fixture_vault, now=now) == []
    old = now + fileops.TEMP_ORPHAN_AGE_SECONDS + 1
    assert fileops.find_orphaned_temp_files(fixture_vault, now=old) == [fresh]


# ===========================================================================
# spec 12 §2 — "EVERY state-changing operation appends one ActionRecord"
# ===========================================================================


def test_a_partially_applied_move_still_records(fixture_vault: Path, tmp_path: Path) -> None:
    """spec 12 §2 is unconditional and spec 05 §1.2 explicitly blesses this
    path ("failure before archive leaves the original in place — a duplicate
    copy may exist"). `_record_action` used to be reached only on the
    all-green tail, so a file the tool CREATED had no corpus record at all.
    """
    import os

    ctx = make_ctx(fixture_vault, tmp_path / "state")
    archive_dir = fixture_vault / "archive" / "capture" / "raw_capture"
    archive_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(archive_dir, 0o500)
    try:
        result = move_to_destination(
            ctx,
            record_for(fixture_vault, QUIRK_FILES["current_schema"]),
            fixture_vault / "projects" / "blog",
        )
    finally:
        os.chmod(archive_dir, 0o755)

    assert result.ok is False
    assert "archiving the original failed" in (result.error or "")
    destination = fixture_vault / "projects" / "blog" / Path(QUIRK_FILES["current_schema"]).name
    assert destination.exists(), "the copy was written — this IS a vault mutation"

    records = action_records(ctx)
    assert len(records) == 1, "a mutated vault with zero corpus lines is untraceable"
    assert records[0]["operation"] == "move"
    assert "archiving the original failed" in records[0]["context"]["filters"]["partial_failure"]
    assert records[0]["targets"][0]["path"] == str(destination)


def test_a_partially_applied_merge_still_records(fixture_vault: Path, tmp_path: Path) -> None:
    """The worst case of the same shape: the TARGET has already been
    permanently rewritten when archiving the capture fails."""
    import os

    ctx = make_ctx(fixture_vault, tmp_path / "state")
    target = fixture_vault / QUIRK_FILES["merge_target"]
    before = target.read_text(encoding="utf-8")
    archive_dir = fixture_vault / "archive" / "capture" / "raw_capture"
    archive_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(archive_dir, 0o500)
    try:
        result = merge_into_note(
            ctx, record_for(fixture_vault, QUIRK_FILES["scalar_tags"]), target
        )
    finally:
        os.chmod(archive_dir, 0o755)

    assert result.ok is False
    assert target.read_text(encoding="utf-8") != before, "the target WAS rewritten"

    records = action_records(ctx)
    assert len(records) == 1
    assert records[0]["operation"] == "merge"
    assert "archiving the capture failed" in records[0]["context"]["filters"]["partial_failure"]
    # The record still carries what it takes to reverse the edit by hand.
    assert records[0]["targets"][0]["before_text"] == before


def test_the_counterfactual_reaches_the_record(fixture_vault: Path, tmp_path: Path) -> None:
    """spec 12 §2 design intent: "The counterfactual is stored, not just the
    choice: suggestions_shown + chosen_rank turn every session action into a
    labeled ranking example". These had no parameter and no call site, so
    every real record stored an empty list and a null rank, and 12 §3's
    acceptance test was unsatisfiable through the product.
    """
    from organize_core.actions import SuggestionShown

    shown = (
        SuggestionShown(path="/v/projects/blog", score=3.1, rank=1, reasons=("tag",)),
        SuggestionShown(path="/v/areas/health", score=1.2, rank=2, reasons=("type",)),
    )
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    ctx.suggestions_shown = shown
    ctx.chosen_rank = 2
    ctx.durations_ms = {"decision": 8400, "operation": 120}
    ctx.auto_tags_present = ("auto/meeting",)
    ctx.filters = {"tags": ["impro"]}

    result = move_to_destination(
        ctx,
        record_for(fixture_vault, QUIRK_FILES["current_schema"]),
        fixture_vault / "areas" / "health",
    )
    assert result.ok is True, result.error

    context = action_records(ctx)[0]["context"]
    assert [s["rank"] for s in context["suggestions_shown"]] == [1, 2]
    assert context["suggestions_shown"][0]["path"] == "/v/projects/blog"
    assert context["chosen_rank"] == 2
    assert context["durations_ms"] == {"decision": 8400, "operation": 120}
    assert context["auto_tags_present"] == ["auto/meeting"]
    assert context["filters"] == {"tags": ["impro"]}


def test_the_destination_description_reaches_the_record(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """spec 12 §2 `targets[].description` — "the NL description of this
    destination, if any". `_target_state` defaulted it to None and not one of
    its ~13 call sites passed it, so every record stored null even for
    folders that demonstrably have a description."""
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    ctx.describe = lambda folder: "Ongoing health practice." if folder.name == "health" else None

    result = move_to_destination(
        ctx,
        record_for(fixture_vault, QUIRK_FILES["current_schema"]),
        fixture_vault / "areas" / "health",
    )
    assert result.ok is True, result.error
    assert action_records(ctx)[0]["targets"][0]["description"] == "Ongoing health practice."


def test_a_small_target_stores_its_full_before_text(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """spec 12 §2 annotates `diff`: "full before-text stored when the file is
    new or small (<64 KB)". `TargetState` had no such field and
    `_unified_diff` uses the default 3 lines of context, so for any target
    longer than ~7 lines the pre-edit content was gone — and doc 13's stated
    use ("proposed_diff vs final_diff … labeled edit example") needs the file
    the edit was made against."""
    target = fixture_vault / QUIRK_FILES["merge_target"]
    body = "---\ntitle: big\ntags:\n- blog-idea\n---\n" + "".join(
        f"line {n}\n" for n in range(200)
    )
    target.write_text(body, encoding="utf-8")
    ctx = make_ctx(fixture_vault, tmp_path / "state")

    result = merge_into_note(ctx, record_for(fixture_vault, QUIRK_FILES["scalar_tags"]), target)
    assert result.ok is True, result.error

    entry = action_records(ctx)[0]["targets"][0]
    assert entry["before_text"] == body
    assert "line 100" not in entry["diff"], "the diff alone cannot reconstruct the before-text"
    assert entry["before_hash"] is not None


def test_a_target_over_the_size_limit_stays_hash_plus_diff(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The other half of the same rule — "target files stored as hash + diff
    to bound growth"."""
    target = fixture_vault / QUIRK_FILES["merge_target"]
    filler = "x" * 100
    target.write_text(
        "---\ntitle: huge\n---\n" + "".join(f"{filler}\n" for _ in range(1000)),
        encoding="utf-8",
    )
    assert target.stat().st_size > fileops.BEFORE_TEXT_LIMIT_BYTES
    ctx = make_ctx(fixture_vault, tmp_path / "state")

    result = merge_into_note(ctx, record_for(fixture_vault, QUIRK_FILES["scalar_tags"]), target)
    assert result.ok is True, result.error
    entry = action_records(ctx)[0]["targets"][0]
    assert entry["before_text"] is None
    assert entry["before_hash"] is not None and entry["diff"]


def test_a_new_destination_has_no_before_text(fixture_vault: Path, tmp_path: Path) -> None:
    """A move writes a file that did not exist: `before_hash` is null and so
    is `before_text` — the diff already carries the whole content."""
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    result = move_to_destination(
        ctx,
        record_for(fixture_vault, QUIRK_FILES["current_schema"]),
        fixture_vault / "projects" / "blog",
    )
    assert result.ok is True, result.error
    entry = action_records(ctx)[0]["targets"][0]
    assert entry["before_hash"] is None
    assert entry["before_text"] is None


def test_a_lost_action_record_does_not_feed_the_derived_view(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """spec 12 §2 "Uses" #2: "one write path, two readers". The consumer hook
    fires only for a record that was actually APPENDED, so a corpus write
    failure can no longer leave learning.json holding a move the corpus never
    saw."""
    seen: list[Any] = []
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    ctx.on_record = seen.append
    ctx.recorder.actions_dir = tmp_path / "state" / "actions" / "not-a-dir" / "x"
    (tmp_path / "state" / "actions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "actions" / "not-a-dir").write_text("blocked", encoding="utf-8")

    result = move_to_destination(
        ctx,
        record_for(fixture_vault, QUIRK_FILES["current_schema"]),
        fixture_vault / "projects" / "blog",
    )

    assert result.ok is True, "a corpus failure never blocks the operation (12 §2)"
    assert seen == [], "no record was written, so no reader may derive state from one"


def test_the_record_consumer_fires_once_per_written_record(
    fixture_vault: Path, tmp_path: Path
) -> None:
    seen: list[Any] = []
    ctx = make_ctx(fixture_vault, tmp_path / "state")
    ctx.on_record = seen.append

    result = move_to_destination(
        ctx,
        record_for(fixture_vault, QUIRK_FILES["current_schema"]),
        fixture_vault / "projects" / "blog",
    )
    assert result.ok is True, result.error
    assert len(seen) == 1
    assert seen[0].operation == "move"
