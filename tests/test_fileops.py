"""fileops behavior suite (spec 05 all, 10 §4, 09 §5.6, 12 §2).

Every assertion here is an EXACT value or a real filesystem fact. The old
suite passed while the product was broken because it asserted ``> 0``
(spec 09 §3); nothing in this file may do that.

The index is built by another seat concurrently, so unit tests drive a
duck-typed :class:`FakeIndex` that records ``update_file``/``remove_file``
calls. Real-index wiring is the integrator's job.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES
from organize_core import fileops
from organize_core.actions import ActionRecorder
from organize_core.config import Config, FileOpsConfig, VaultConfig
from organize_core.errors import ConcurrentModificationError, ConfigError, OperationError
from organize_core.fileops import (
    LoggedOperation,
    OperationContext,
    OperationLog,
    append_to_note,
    archive_capture,
    atomic_write,
    backup_file,
    check_unmodified,
    collision_free_path,
    format_log_line,
    get_archive_path,
    merge_into_note,
    merge_preview,
    move_to_destination,
    new_folder,
    parse_log_line,
    snapshot_file,
    update_frontmatter,
    update_tags,
)
from organize_core.index import NoteRecord

# A pinned clock: every timestamp this module renders is derived from it, so
# every expectation below is a literal string rather than a regex.
FIXED_NOW = 1786000000.0
ISO = "2026-08-06T07:06:40Z"
STAMP = "20260806_070640"
TODAY = "2026-08-06"
MINUTE = "2026-08-06 07:06"
MONTH_FILE = "2026-08.jsonl"


class FakeIndex:
    """Duck-typed stand-in for VaultIndex (built concurrently by another
    seat). Records exactly the calls fileops makes."""

    def __init__(self, stats: dict[str, int] | None = None) -> None:
        self.updated: list[Path] = []
        self.removed: list[Path] = []
        self._stats = {"total": 12, "capture_backlog": 5} if stats is None else stats

    def update_file(self, path: Path) -> None:
        self.updated.append(Path(path))
        return None

    def remove_file(self, path: Path) -> None:
        self.removed.append(Path(path))

    def stats(self) -> dict[str, int]:
        return dict(self._stats)


def make_config(vault: Path, **file_ops: Any) -> Config:
    return Config(vault=VaultConfig(root=vault), file_ops=FileOpsConfig(**file_ops))


def make_ctx(
    vault: Path,
    state: Path,
    *,
    config: Config | None = None,
    dry_run: bool = False,
    actor: str = "matt",
    session_id: str | None = "ses_test",
    now: float = FIXED_NOW,
) -> OperationContext:
    config = config or make_config(vault)
    return OperationContext(
        config=config,
        index=FakeIndex(),  # type: ignore[arg-type]
        oplog=OperationLog(state / "operations.log"),
        recorder=ActionRecorder(state / "actions"),
        backup_dir=vault / config.file_ops.backup_dir,
        dry_run=dry_run,
        actor=actor,
        session_id=session_id,
        clock=lambda: now,
    )


def record_for(vault: Path, rel: str) -> NoteRecord:
    path = vault / rel
    return NoteRecord(
        path=str(path),
        filename=path.name,
        title=path.stem,
        para_type="capture",
        folder=path.parent.name,
        capture_id=path.stem,
        id=path.stem,
    )


def log_lines(ctx: OperationContext) -> list[str]:
    path = ctx.oplog.log_file
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def action_records(ctx: OperationContext) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for month in sorted(Path(ctx.recorder.actions_dir).glob("*.jsonl")):
        for line in month.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


@pytest.fixture()
def ctx(fixture_vault: Path, tmp_path: Path) -> OperationContext:
    return make_ctx(fixture_vault, tmp_path / "state")


# --- primitives: atomic_write (05 §1.3, 08 §A25) ---------------------------


def test_atomic_write_creates_file_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "notes" / "a.md"
    atomic_write(target, "hello\nworld\n")
    assert target.read_text(encoding="utf-8") == "hello\nworld\n"
    assert sorted(p.name for p in target.parent.iterdir()) == ["a.md"]


def test_atomic_write_replaces_and_preserves_permissions(tmp_path: Path) -> None:
    target = tmp_path / "a.md"
    target.write_text("old\n", encoding="utf-8")
    os.chmod(target, 0o640)
    atomic_write(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_atomic_write_same_second_writes_do_not_collide(tmp_path: Path) -> None:
    """08 §A25: the original's temp name collided on same-second writes."""
    names: list[str] = []
    real_replace = fileops._replace

    def spy(src: str, dst: str) -> None:
        names.append(Path(src).name)
        real_replace(src, dst)

    fileops._replace = spy  # type: ignore[assignment]
    try:
        for i in range(5):
            atomic_write(tmp_path / f"n{i}.md", f"body {i}\n")
    finally:
        fileops._replace = real_replace  # type: ignore[assignment]
    assert len(set(names)) == 5, names
    assert sorted(p.name for p in tmp_path.iterdir()) == ["n0.md", "n1.md", "n2.md", "n3.md", "n4.md"]


# --- primitives: collision / archive paths (05 §2.4, §3, 08 §A15) ----------


def test_collision_free_path_numbers_before_the_extension(tmp_path: Path) -> None:
    base = tmp_path / "2026-04-08T16:51:24.690160+00:00.md"
    assert collision_free_path(base) == base
    base.write_text("x", encoding="utf-8")
    first = collision_free_path(base)
    assert first.name == "2026-04-08T16:51:24.690160+00:00_1.md"
    first.write_text("x", encoding="utf-8")
    assert collision_free_path(base).name == "2026-04-08T16:51:24.690160+00:00_2.md"


def test_get_archive_path_keeps_the_filename_and_honors_config(fixture_vault: Path) -> None:
    """08 §A15: no rename to <id>.md, no hardcoded /archives/capture/raw_capture/."""
    config = make_config(fixture_vault)
    path = get_archive_path("meeting notes — café ☕.md", config, now=FIXED_NOW)
    assert path == fixture_vault / "archive/capture/raw_capture/meeting notes — café ☕.md"

    custom = Config(
        vault=VaultConfig(
            root=fixture_vault,
            para_folders={"projects": "projects", "archives": "attic"},
            archive_capture_path="old/captures",
        )
    )
    assert get_archive_path("a.md", custom, now=FIXED_NOW) == fixture_vault / "attic/old/captures/a.md"


def test_get_archive_path_collision_gets_a_timestamp_suffix(fixture_vault: Path) -> None:
    config = make_config(fixture_vault)
    first = get_archive_path("dup.md", config, now=FIXED_NOW)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("one", encoding="utf-8")
    second = get_archive_path("dup.md", config, now=FIXED_NOW)
    assert second.name == f"dup_{STAMP}.md"
    second.write_text("two", encoding="utf-8")
    third = get_archive_path("dup.md", config, now=FIXED_NOW)
    assert third.name == f"dup_{STAMP}_1.md"


def test_get_archive_path_without_an_archives_folder_fails_loudly(fixture_vault: Path) -> None:
    config = Config(vault=VaultConfig(root=fixture_vault, para_folders={"projects": "projects"}))
    with pytest.raises(OperationError) as excinfo:
        get_archive_path("a.md", config, now=FIXED_NOW)
    assert "archives" in str(excinfo.value)
    assert excinfo.value.hint is not None


# --- primitives: backups (05 §1.4) -----------------------------------------


def test_backup_file_names_and_bytes(fixture_vault: Path, tmp_path: Path) -> None:
    source = fixture_vault / QUIRK_FILES["current_schema"]
    backup_dir = tmp_path / "backups"
    first = backup_file(source, backup_dir, now=FIXED_NOW)
    assert first == backup_dir / f"{STAMP}_2026-06-10T21:33:05.379Z.md"
    assert first.read_bytes() == source.read_bytes()
    second = backup_file(source, backup_dir, now=FIXED_NOW)
    assert second.name == f"{STAMP}_2026-06-10T21:33:05.379Z_1.md"
    assert second.read_bytes() == source.read_bytes()


# --- primitives: concurrent-modification guard (10 §4) ---------------------


def test_snapshot_and_check_unmodified_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("one\n", encoding="utf-8")
    snap = snapshot_file(path)
    assert snap.path == str(path)
    assert snap.mtime == path.stat().st_mtime
    assert snap.sha256 == hashlib.sha256(b"one\n").hexdigest()
    check_unmodified(snap)  # unchanged -> silent


def test_check_unmodified_detects_a_touch(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("one\n", encoding="utf-8")
    snap = snapshot_file(path)
    os.utime(path, (snap.mtime + 5, snap.mtime + 5))
    with pytest.raises(ConcurrentModificationError) as excinfo:
        check_unmodified(snap)
    assert str(path) in str(excinfo.value)
    assert "mtime" in str(excinfo.value)


def test_check_unmodified_detects_a_content_change_at_the_same_mtime(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("one\n", encoding="utf-8")
    snap = snapshot_file(path)
    path.write_text("two\n", encoding="utf-8")
    os.utime(path, (snap.mtime, snap.mtime))
    with pytest.raises(ConcurrentModificationError) as excinfo:
        check_unmodified(snap)
    assert "sha256" in str(excinfo.value)


def test_check_unmodified_detects_deletion(tmp_path: Path) -> None:
    path = tmp_path / "a.md"
    path.write_text("one\n", encoding="utf-8")
    snap = snapshot_file(path)
    path.unlink()
    with pytest.raises(ConcurrentModificationError):
        check_unmodified(snap)


# --- the operation log (05 §1.5, §8; 08 §A5/§A16/§A21) ---------------------


def test_log_line_format_and_roundtrip() -> None:
    op = LoggedOperation(
        ts=ISO, type="move", src="/v/capture/a.md", dst="/v/projects/blog/a.md", success=True
    )
    assert format_log_line(op) == f"[{ISO}] move: /v/capture/a.md -> /v/projects/blog/a.md [SUCCESS]"
    assert parse_log_line(format_log_line(op)) == op

    failed = LoggedOperation(
        ts=ISO,
        type="merge",
        src="/v/a.md",
        dst="/v/b.md",
        success=False,
        error="destination unwritable",
        backup="/v/.backups/x.md",
    )
    assert format_log_line(failed) == (
        f"[{ISO}] merge: /v/a.md -> /v/b.md [FAILED] Backup: /v/.backups/x.md "
        "Error: destination unwritable"
    )
    assert parse_log_line(format_log_line(failed)) == failed

    dry = LoggedOperation(ts=ISO, type="archive", src="/v/a.md", dst="/v/x.md", success=True, dry_run=True)
    assert format_log_line(dry).endswith("[DRY-RUN]")
    assert parse_log_line(format_log_line(dry)) == dry


def test_log_line_never_spans_two_lines() -> None:
    op = LoggedOperation(
        ts=ISO, type="move", src="/v/a.md", dst="/v/b.md", success=False, error="boom\nsecond line"
    )
    rendered = format_log_line(op)
    assert "\n" not in rendered
    # One op == one line, AND the field survives: the newline is escaped, not
    # collapsed away, so `parse_log_line` gives back exactly what was logged.
    assert parse_log_line(rendered).error == "boom\nsecond line"  # type: ignore[union-attr]


def test_parse_log_line_returns_none_for_garbage() -> None:
    assert parse_log_line("this is not an operation line") is None


def test_oplog_writes_to_disk_and_serves_recent_and_undo(tmp_path: Path) -> None:
    """08 §A16 (logger init never called -> nothing on disk) and 08 §A5/§A21
    (recent/undo read a global that does not exist)."""
    log = OperationLog(tmp_path / "state" / "operations.log")
    assert log.recent() == []
    assert log.undo_info() is None

    ok = LoggedOperation(ts=ISO, type="move", src="/a", dst="/b", success=True, backup="/bk")
    bad = LoggedOperation(ts=ISO, type="merge", src="/c", dst="/d", success=False, error="nope")
    dry = LoggedOperation(ts=ISO, type="archive", src="/e", dst="/f", success=True, dry_run=True)
    for op in (ok, bad, dry):
        log.append(op)

    assert log.log_file.exists(), "operations.log must reach the real file on disk"
    assert len(log.log_file.read_text(encoding="utf-8").splitlines()) == 3
    assert log.recent(2) == [bad, dry]
    assert log.recent(10) == [ok, bad, dry]
    assert log.recent(0) == []
    # neither a FAILED nor a DRY-RUN line is reversible
    assert log.undo_info() == ok


def test_oplog_append_failure_is_survivable(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    log = OperationLog(blocker / "operations.log")
    log.append(LoggedOperation(ts=ISO, type="move", src="/a", dst="/b", success=True))
    # loud (logged) but not raised, and the in-memory fallback still answers
    assert log.recent(1)[0].src == "/a"


# --- move_to_destination (05 §2, §9) ---------------------------------------


def test_move_acceptance(ctx: OperationContext, fixture_vault: Path) -> None:
    """Spec 05 §9 acceptance test 1, every clause."""
    source = fixture_vault / QUIRK_FILES["current_schema"]
    original_bytes = source.read_bytes()
    dest_folder = fixture_vault / "projects" / "blog"

    result = move_to_destination(ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), dest_folder)

    assert result.ok is True
    assert result.operation == "move"
    assert result.dry_run is False
    assert result.error is None

    # original gone from the capture dir
    assert not source.exists()

    # copy present at the destination, with the organize edits
    dest = dest_folder / "2026-06-10T21:33:05.379Z.md"
    assert result.destination == str(dest)
    text = dest.read_text(encoding="utf-8")
    assert "\ntags:\n- impro\n- creativity\n- project/blog\n" in text
    assert "\nprocessing_status: organized\n" in text
    assert f"\nlast_edited_date: '{TODAY}'\n" in text
    # untouched fields survive byte-for-byte (03 §8 / 08 §A12)
    assert "\nlocation:\n  latitude: 40.7126\n" in text
    assert "\nmetadata: {}\n" in text
    assert text.endswith("## Content\nAn idea about improv warmups and creative flow.\n")

    # original readable in the archive UNDER ITS ORIGINAL FILENAME, byte-identical
    archived = fixture_vault / "archive/capture/raw_capture/2026-06-10T21:33:05.379Z.md"
    assert archived.exists()
    assert archived.read_bytes() == original_bytes
    assert result.details["archive_path"] == str(archived)
    assert result.details["tag_added"] == "project/blog"

    # backup of the source (05 §2.3)
    backup = Path(result.backup_path)  # type: ignore[arg-type]
    assert backup == fixture_vault / ".backups" / f"{STAMP}_2026-06-10T21:33:05.379Z.md"
    assert backup.read_bytes() == original_bytes

    # the operation log really reached disk (08 §A16), archive line then move line
    assert log_lines(ctx) == [
        f"[{ISO}] archive: {source} -> {archived} [SUCCESS] Backup: {backup}",
        f"[{ISO}] move: {source} -> {dest} [SUCCESS] Backup: {backup}",
    ]
    assert ctx.oplog.undo_info().dst == str(dest)  # type: ignore[union-attr]

    # index: capture entry removed, destination + archive entries added (05 §2.8)
    assert ctx.index.removed == [source]  # type: ignore[attr-defined]
    assert ctx.index.updated == [dest, archived]  # type: ignore[attr-defined]


def test_move_writes_exactly_one_complete_action_record(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    move_to_destination(
        ctx,
        record_for(fixture_vault, QUIRK_FILES["current_schema"]),
        fixture_vault / "projects" / "blog",
    )
    records = action_records(ctx)
    assert len(records) == 1
    rec = records[0]
    assert (Path(ctx.recorder.actions_dir) / MONTH_FILE).exists()
    assert rec["schema_version"] == 1
    assert rec["id"].startswith("act_")
    assert rec["ts"] == ISO
    assert rec["actor"] == "matt"
    assert rec["operation"] == "move"
    assert rec["context"]["session_id"] == "ses_test"
    assert rec["context"]["vault_stats"] == {"total": 12, "capture_backlog": 5}
    assert rec["context"]["filters"] == {}
    assert rec["context"]["dry_run"] is False  # a real operation is a precedent
    assert rec["capture"]["path"] == str(fixture_vault / QUIRK_FILES["current_schema"])
    assert rec["capture"]["frontmatter_before"]["tags"] == ["impro", "creativity"]
    assert rec["capture"]["frontmatter_before"]["processing_status"] == "raw"
    assert rec["capture"]["frontmatter_after"]["tags"] == ["impro", "creativity", "project/blog"]
    assert rec["capture"]["frontmatter_after"]["processing_status"] == "organized"
    assert rec["capture"]["body_before"].startswith("## Content\n")
    assert len(rec["targets"]) == 1
    target = rec["targets"][0]
    assert target["path"] == str(fixture_vault / "projects/blog/2026-06-10T21:33:05.379Z.md")
    assert target["role"] == "destination"
    assert target["before_hash"] is None
    assert target["diff"].startswith("--- a/")


def test_move_preserves_tag_order_and_casing(ctx: OperationContext, fixture_vault: Path) -> None:
    """08 §A24: update_tags re-sorted and re-cased the user's list."""
    rel = "capture/raw_capture/cased.md"
    (fixture_vault / rel).write_text(
        "---\nid: cased\ntags:\n- Zebra\n- Impro\n- alpha\n---\nbody\n", encoding="utf-8"
    )
    move_to_destination(ctx, record_for(fixture_vault, rel), fixture_vault / "areas" / "health")
    text = (fixture_vault / "areas/health/cased.md").read_text(encoding="utf-8")
    assert "tags:\n- Zebra\n- Impro\n- alpha\n- area/health\n" in text


def test_move_tag_uses_the_singular_config_key(ctx: OperationContext, fixture_vault: Path) -> None:
    for rel, dest, expected in (
        ("capture/raw_capture/a1.md", "areas/relationships", "area/relationships"),
        ("capture/raw_capture/a2.md", "resources/performing", "resource/performing"),
        ("capture/raw_capture/a3.md", "projects/kms", "project/kms"),
    ):
        (fixture_vault / rel).write_text("---\ntags:\n- x\n---\nb\n", encoding="utf-8")
        result = move_to_destination(ctx, record_for(fixture_vault, rel), fixture_vault / dest)
        assert result.ok is True
        assert result.details["tag_added"] == expected


def test_move_outside_a_para_root_adds_no_invented_tag(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    rel = "capture/raw_capture/untagged.md"
    (fixture_vault / rel).write_text("---\ntags:\n- x\n---\nb\n", encoding="utf-8")
    result = move_to_destination(ctx, record_for(fixture_vault, rel), fixture_vault / "dailies")
    assert result.ok is True
    assert result.details["tag_added"] is None
    assert (fixture_vault / "dailies/untagged.md").read_text(encoding="utf-8").startswith(
        "---\ntags:\n- x\nprocessing_status: organized\n"
    )


def test_move_collision_suffixes_destination_and_archive(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    """Spec 05 §9 acceptance test 3."""
    dest_folder = fixture_vault / "projects" / "blog"
    for i in range(2):
        (fixture_vault / "capture/raw_capture/dup.md").write_text(
            f"---\nid: dup{i}\n---\nbody {i}\n", encoding="utf-8"
        )
        result = move_to_destination(
            ctx, record_for(fixture_vault, "capture/raw_capture/dup.md"), dest_folder
        )
        assert result.ok is True

    assert (dest_folder / "dup.md").exists()
    assert (dest_folder / "dup_1.md").exists()
    archive_dir = fixture_vault / "archive/capture/raw_capture"
    assert (archive_dir / "dup.md").read_text(encoding="utf-8") == "---\nid: dup0\n---\nbody 0\n"
    assert (archive_dir / f"dup_{STAMP}.md").read_text(encoding="utf-8") == "---\nid: dup1\n---\nbody 1\n"


def test_move_missing_source_fails_without_touching_anything(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    ghost = record_for(fixture_vault, "capture/raw_capture/nope.md")
    result = move_to_destination(ctx, ghost, fixture_vault / "projects" / "blog")
    assert result.ok is False
    assert result.error == f"source note does not exist: {fixture_vault}/capture/raw_capture/nope.md"
    assert log_lines(ctx) == [
        f"[{ISO}] move: {fixture_vault}/capture/raw_capture/nope.md -> "
        f"{fixture_vault}/projects/blog [FAILED] Error: {result.error}"
    ]
    assert action_records(ctx) == []


def test_move_unwritable_destination_leaves_the_original_in_place(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    """Spec 05 §9 acceptance test 2 (failure injection)."""
    source = fixture_vault / QUIRK_FILES["current_schema"]
    original_bytes = source.read_bytes()
    dest_folder = fixture_vault / "projects" / "blog"
    os.chmod(dest_folder, 0o555)
    try:
        result = move_to_destination(
            ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), dest_folder
        )
    finally:
        os.chmod(dest_folder, 0o755)

    assert result.ok is False
    assert "could not write the copy" in (result.error or "")
    assert source.read_bytes() == original_bytes, "the original must never be lost (05 §1.2)"
    assert not (fixture_vault / "archive/capture/raw_capture/2026-06-10T21:33:05.379Z.md").exists()
    assert log_lines(ctx)[-1].endswith(f"[FAILED] Backup: {result.backup_path} Error: {result.error}")
    assert action_records(ctx) == []


def test_move_without_auto_create_folders_fails_loudly(
    fixture_vault: Path, tmp_path: Path
) -> None:
    ctx = make_ctx(
        fixture_vault,
        tmp_path / "state",
        config=make_config(fixture_vault, auto_create_folders=False),
    )
    dest = fixture_vault / "projects" / "brand-new"
    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), dest
    )
    assert result.ok is False
    assert result.error == f"destination folder does not exist: {dest}"
    assert not dest.exists()


def test_move_creates_the_destination_when_configured(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    dest = fixture_vault / "projects" / "brand-new"
    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), dest
    )
    assert result.ok is True
    assert (dest / "2026-06-10T21:33:05.379Z.md").is_file()


def test_move_of_a_note_without_frontmatter_creates_a_block(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    rel = QUIRK_FILES["no_frontmatter"]
    result = move_to_destination(ctx, record_for(fixture_vault, rel), fixture_vault / "resources/performing")
    assert result.ok is True
    text = (fixture_vault / "resources/performing" / Path(rel).name).read_text(encoding="utf-8")
    assert text == (
        "---\n"
        "tags:\n"
        "- resource/performing\n"
        "processing_status: organized\n"
        f"last_edited_date: '{TODAY}'\n"
        "---\n"
        "Just a plain markdown body, no frontmatter block.\n"
    )


# --- move: destinations that are not filing destinations -------------------


def test_move_into_the_notes_own_folder_is_a_noop(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    """Real-data finding: this silently renamed the note to `<name>_1.md` and
    archived the ORIGINAL filename, so the file no longer matched its own
    `id:`/`aliases:` and every [[wikilink]] to it broke (05 §3) — while
    returning rc=0."""
    rel = QUIRK_FILES["current_schema"]
    source = fixture_vault / rel
    before = source.read_bytes()
    folder = source.parent

    result = move_to_destination(ctx, record_for(fixture_vault, rel), folder)

    assert result.ok is True
    assert result.details["noop"] == f"the note is already in {folder}"
    # nothing renamed, nothing archived, nothing written
    assert source.read_bytes() == before
    assert not (folder / "2026-06-10T21:33:05.379Z_1.md").exists()
    assert not (fixture_vault / "archive/capture/raw_capture/2026-06-10T21:33:05.379Z.md").exists()
    assert log_lines(ctx) == []
    assert action_records(ctx) == []


@pytest.mark.parametrize("where", ["backup_dir", "inside_backup_dir", "vault_root"])
def test_move_refuses_destinations_outside_the_para_tree(
    ctx: OperationContext, fixture_vault: Path, where: str
) -> None:
    """All are inside the vault but outside `vault.scan_dirs`: the note would
    be archived as organized and then never indexed again — reported as a
    success. A subfolder of `.backups` is still the backup directory."""
    rel = QUIRK_FILES["current_schema"]
    source = fixture_vault / rel
    before = source.read_bytes()
    dest = {
        "backup_dir": ctx.backup_dir,
        "inside_backup_dir": ctx.backup_dir / "2026-08",
        "vault_root": fixture_vault,
    }[where]
    dest.mkdir(parents=True, exist_ok=True)

    result = move_to_destination(ctx, record_for(fixture_vault, rel), dest)

    assert result.ok is False
    assert "not a filing destination" in (result.error or "")
    assert source.read_bytes() == before
    assert not (dest / "2026-06-10T21:33:05.379Z.md").exists()
    assert not (fixture_vault / "archive/capture/raw_capture/2026-06-10T21:33:05.379Z.md").exists()
    assert action_records(ctx) == []


# --- move: CR-bearing notes (05 §1 byte fidelity, 05 §9 acceptance) --------
#
# Real-data finding: 569 of the 1,858 real backlog captures (30.6%) contain a
# carriage return, and `move` refused every one of them with a false "copy
# verification failed" — the copy on disk was perfect, but the read-back used
# universal-newline mode so `\r\n` came back as `\n` and the comparison could
# never succeed. The suite had no CR fixture at all; these are it.

CR_REL = "capture/raw_capture/cr-note.md"


def write_cr_capture(vault: Path, *, eol: str, rel: str = CR_REL) -> bytes:
    """A capture whose BODY uses ``eol`` line endings (frontmatter stays LF,
    which is exactly the real corpus's shape: CRs arrive with pasted body
    content). Returns the bytes written."""
    body = eol.join(["## Content", "pasted from a windows editor", "second line", ""])
    raw = (
        "---\ntags:\n- impro\nprocessing_status: raw\nlast_edited_date: '2026-06-10'\n---\n" + body
    ).encode("utf-8")
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


@pytest.mark.parametrize("eol", ["\r\n", "\r"], ids=["crlf", "lone-cr"])
def test_move_of_a_cr_bearing_note_succeeds_and_keeps_the_bytes(
    ctx: OperationContext, fixture_vault: Path, eol: str
) -> None:
    original_bytes = write_cr_capture(fixture_vault, eol=eol)
    assert b"\r" in original_bytes
    dest_folder = fixture_vault / "projects" / "blog"

    result = move_to_destination(ctx, record_for(fixture_vault, CR_REL), dest_folder)

    assert result.ok is True, result.error
    dest = dest_folder / "cr-note.md"
    written = dest.read_bytes()
    # the body's carriage returns survive the rewrite verbatim (05 §1)
    assert written.count(b"\r") == original_bytes.count(b"\r")
    assert eol.encode("utf-8") + b"second line" in written
    # and the operation completed: original archived under its own filename
    archived = fixture_vault / "archive/capture/raw_capture/cr-note.md"
    assert archived.read_bytes() == original_bytes
    assert not (fixture_vault / CR_REL).exists()
    assert log_lines(ctx)[-1].endswith("[SUCCESS] Backup: " + str(result.backup_path))


def test_dry_run_move_of_a_cr_note_predicts_the_real_move(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """A dry run that reports success for an operation the live path refuses
    is worse than no preview at all (09 §5.6)."""
    write_cr_capture(fixture_vault, eol="\r\n")
    dest_folder = fixture_vault / "projects" / "blog"
    record = record_for(fixture_vault, CR_REL)

    preview = move_to_destination(
        make_ctx(fixture_vault, tmp_path / "dry", dry_run=True), record, dest_folder
    )
    live = move_to_destination(make_ctx(fixture_vault, tmp_path / "live"), record, dest_folder)

    assert preview.ok is True
    assert live.ok == preview.ok
    assert live.destination == preview.destination


def _short_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next ``atomic_write`` land 5 bytes short — a genuine
    verification failure (short write / full disk / concurrent writer)."""
    real_write = fileops.atomic_write

    def short_write(path: Path, content: str, **kwargs: Any) -> None:
        real_write(path, content[:-5], **kwargs)

    monkeypatch.setattr(fileops, "atomic_write", short_write)


def test_move_rolls_back_the_copy_when_verification_fails(
    ctx: OperationContext, fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller is told the note was NOT filed, so a fully-organized copy
    sitting at the destination is a silent duplicate — and every retry adds
    another ``_1``, ``_2``… copy."""
    source = fixture_vault / QUIRK_FILES["current_schema"]
    original_bytes = source.read_bytes()
    dest_folder = fixture_vault / "projects" / "blog"
    _short_write(monkeypatch)

    result = move_to_destination(
        ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), dest_folder
    )

    assert result.ok is False
    assert "copy verification failed" in (result.error or "")
    assert "the copy was removed" in (result.error or "")
    # nothing left at the destination, nothing archived, original untouched
    assert not (dest_folder / "2026-06-10T21:33:05.379Z.md").exists()
    assert not (fixture_vault / "archive/capture/raw_capture/2026-06-10T21:33:05.379Z.md").exists()
    assert source.read_bytes() == original_bytes
    # a rolled-back operation changed nothing, so it is not a doc-12 precedent
    assert action_records(ctx) == []
    assert log_lines(ctx)[-1].endswith(f"Error: {result.error}")


def test_repeated_failing_moves_leave_no_orphan_copies(
    ctx: OperationContext, fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest_folder = fixture_vault / "projects" / "blog"
    before = sorted(p.name for p in dest_folder.iterdir())
    _short_write(monkeypatch)
    for _ in range(3):
        result = move_to_destination(
            ctx, record_for(fixture_vault, QUIRK_FILES["current_schema"]), dest_folder
        )
        assert result.ok is False
    assert sorted(p.name for p in dest_folder.iterdir()) == before


# --- archive_capture (05 §3, 08 §A14/§A15) ---------------------------------


def test_archive_keeps_the_original_filename(ctx: OperationContext, fixture_vault: Path) -> None:
    rel = QUIRK_FILES["iso_filename"]
    source = fixture_vault / rel
    original = source.read_bytes()
    result = archive_capture(ctx, record_for(fixture_vault, rel))

    archived = fixture_vault / "archive/capture/raw_capture/2026-04-08T16:51:24.690160+00:00.md"
    assert result.ok is True
    assert result.destination == str(archived)
    assert archived.read_bytes() == original, "archived bytes must equal the source bytes"
    assert not source.exists()
    assert log_lines(ctx) == [
        f"[{ISO}] archive: {source} -> {archived} [SUCCESS] Backup: {result.backup_path}"
    ]
    records = action_records(ctx)
    assert len(records) == 1
    assert records[0]["operation"] == "archive"
    assert records[0]["targets"][0]["path"] == str(archived)
    assert records[0]["targets"][0]["role"] == "destination"


def test_archive_across_filesystems_copies_verifies_then_unlinks(
    ctx: OperationContext, fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """05 §3: rename may cross filesystems; the fallback must verify."""
    rel = QUIRK_FILES["unicode_spaces"]
    source = fixture_vault / rel
    original = source.read_bytes()

    def exdev(src: str, dst: str) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(fileops, "_replace", exdev)
    result = archive_capture(ctx, record_for(fixture_vault, rel))

    archived = fixture_vault / "archive/capture/raw_capture/meeting notes — café ☕.md"
    assert result.ok is True
    assert archived.read_bytes() == original
    assert not source.exists()


def test_archive_reports_an_unverified_cross_filesystem_copy_without_deleting(
    ctx: OperationContext, fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rel = QUIRK_FILES["scalar_tags"]
    source = fixture_vault / rel
    original = source.read_bytes()

    def exdev(src: str, dst: str) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def bad_copy(src: Any, dst: Any, **kwargs: Any) -> None:
        Path(dst).write_text("CORRUPTED", encoding="utf-8")

    monkeypatch.setattr(fileops, "_replace", exdev)
    monkeypatch.setattr(fileops.shutil, "copy2", bad_copy)
    result = archive_capture(ctx, record_for(fixture_vault, rel))

    assert result.ok is False
    assert "did not verify" in (result.error or "")
    assert source.read_bytes() == original, "a failed verification must never unlink the original"


def test_archive_missing_source_fails(ctx: OperationContext, fixture_vault: Path) -> None:
    result = archive_capture(ctx, record_for(fixture_vault, "capture/raw_capture/ghost.md"))
    assert result.ok is False
    assert "does not exist" in (result.error or "")


# --- merge (05 §4, §9; 03 §5; 08 §A32) -------------------------------------


def test_merge_acceptance(ctx: OperationContext, fixture_vault: Path) -> None:
    """Spec 05 §9 acceptance test 4."""
    capture_rel = QUIRK_FILES["current_schema"]
    capture_bytes = (fixture_vault / capture_rel).read_bytes()
    target = fixture_vault / QUIRK_FILES["merge_target"]

    result = merge_into_note(ctx, record_for(fixture_vault, capture_rel), target)
    assert result.ok is True

    text = target.read_text(encoding="utf-8")
    # both bodies present, separator header exactly once
    assert text.count("## Merged from 2026-06-10T21:33:05.379Z.md on ") == 1
    assert f"## Merged from 2026-06-10T21:33:05.379Z.md on {MINUTE}" in text
    assert "- existing idea one" in text
    assert "An idea about improv warmups and creative flow." in text
    # tags union, target first; sources union; unknown target fields byte-preserved
    assert "tags:\n- blog-idea\n- impro\n- creativity\n" in text
    assert "sources:\n- me\n" in text
    assert "author: Matt Handzel\n" in text
    assert "title: Blog ideas\n" in text
    assert "created_date: '2025-11-02'\n" in text
    assert f"last_edited_date: '{TODAY}'\n" in text
    # capture archived, byte-identical, under its own filename
    archived = fixture_vault / "archive/capture/raw_capture/2026-06-10T21:33:05.379Z.md"
    assert archived.read_bytes() == capture_bytes
    assert not (fixture_vault / capture_rel).exists()
    # target backed up before the write
    assert Path(result.backup_path).name == f"{STAMP}_ideas.md"  # type: ignore[arg-type]

    records = action_records(ctx)
    assert len(records) == 1
    assert records[0]["operation"] == "merge"
    assert records[0]["edit_mode"] == "manual"
    assert records[0]["targets"][0]["role"] == "merge_target"
    assert records[0]["targets"][0]["before_hash"] is not None
    assert records[0]["targets"][0]["diff"].startswith("--- a/")


def test_merge_body_template_is_exact(ctx: OperationContext, fixture_vault: Path) -> None:
    capture_rel = "capture/raw_capture/tiny.md"
    (fixture_vault / capture_rel).write_text("---\nid: tiny\n---\ncaptured line\n", encoding="utf-8")
    target = fixture_vault / "projects/kms/notes.md"
    target.write_text("---\ntitle: Notes\n---\ntarget line\n", encoding="utf-8")

    merge_into_note(ctx, record_for(fixture_vault, capture_rel), target)
    assert target.read_text(encoding="utf-8") == (
        "---\n"
        "title: Notes\n"
        f"last_edited_date: '{TODAY}'\n"
        "---\n"
        "target line\n"
        "\n\n---\n\n"
        f"## Merged from tiny.md on {MINUTE}\n"
        "\n"
        "captured line\n"
    )


def test_merge_preview_is_readonly_and_seeds_the_committed_result(
    ctx: OperationContext, fixture_vault: Path, tmp_path: Path
) -> None:
    """One merge implementation, one semantics: committing the untouched
    preview must equal the non-interactive merge (08 §A32)."""
    capture_rel = QUIRK_FILES["current_schema"]
    target_rel = QUIRK_FILES["merge_target"]

    before = (fixture_vault / target_rel).read_bytes()
    content, snapshot = merge_preview(ctx, record_for(fixture_vault, capture_rel), fixture_vault / target_rel)
    assert (fixture_vault / target_rel).read_bytes() == before, "preview must not write"
    assert snapshot.path == str(fixture_vault / target_rel)
    assert content.startswith("---\ntitle: Blog ideas\n")
    assert "instruction" not in content.lower()
    assert content.count("## Merged from") == 1

    interactive_ctx = make_ctx(fixture_vault, tmp_path / "state2")
    merge_into_note(
        interactive_ctx,
        record_for(fixture_vault, capture_rel),
        fixture_vault / target_rel,
        edited_content=content,
        target_snapshot=snapshot,
    )
    interactive = (fixture_vault / target_rel).read_text(encoding="utf-8")

    # rebuild the same starting state and run the non-interactive path
    fresh = fixture_vault / "projects/blog/ideas2.md"
    fresh.write_bytes(before)
    (fixture_vault / capture_rel).parent.mkdir(parents=True, exist_ok=True)
    (fixture_vault / capture_rel).write_bytes(
        (fixture_vault / "archive/capture/raw_capture/2026-06-10T21:33:05.379Z.md").read_bytes()
    )
    plain_ctx = make_ctx(fixture_vault, tmp_path / "state3")
    merge_into_note(plain_ctx, record_for(fixture_vault, capture_rel), fresh)
    assert fresh.read_text(encoding="utf-8") == interactive


def test_merge_keeps_the_users_frontmatter_edits_from_the_buffer(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    capture_rel = "capture/raw_capture/tiny.md"
    (fixture_vault / capture_rel).write_text("---\nid: tiny\ntags:\n- new\n---\ncap\n", encoding="utf-8")
    target = fixture_vault / "projects/kms/notes.md"
    target.write_text("---\ntitle: Notes\n---\ntarget\n", encoding="utf-8")
    edited = "---\ntitle: Notes Renamed By Matt\n---\nhand written body\n"

    merge_into_note(ctx, record_for(fixture_vault, capture_rel), target, edited_content=edited)
    assert target.read_text(encoding="utf-8") == (
        "---\n"
        "title: Notes Renamed By Matt\n"
        "tags:\n"
        "- new\n"
        f"last_edited_date: '{TODAY}'\n"
        "---\n"
        "hand written body\n"
    )


def test_merge_refuses_a_stale_snapshot_and_writes_nothing(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    capture_rel = QUIRK_FILES["current_schema"]
    target = fixture_vault / QUIRK_FILES["merge_target"]
    _, snapshot = merge_preview(ctx, record_for(fixture_vault, capture_rel), target)

    # Syncthing (or Obsidian) rewrites the target while the merge editor is open
    target.write_text("---\ntitle: Blog ideas\n---\nsomeone else edited this\n", encoding="utf-8")
    after_external = target.read_bytes()

    with pytest.raises(ConcurrentModificationError):
        merge_into_note(
            ctx, record_for(fixture_vault, capture_rel), target, target_snapshot=snapshot
        )
    assert target.read_bytes() == after_external
    assert (fixture_vault / capture_rel).exists(), "a refused merge must not archive the capture"
    assert not (fixture_vault / ".backups").exists()


def test_merge_missing_target_fails(ctx: OperationContext, fixture_vault: Path) -> None:
    result = merge_into_note(
        ctx,
        record_for(fixture_vault, QUIRK_FILES["current_schema"]),
        fixture_vault / "projects/blog/nope.md",
    )
    assert result.ok is False
    assert "merge target does not exist" in (result.error or "")
    assert (fixture_vault / QUIRK_FILES["current_schema"]).exists()


# --- append (11 §1, 08 §B15) -----------------------------------------------


def test_append_uses_the_default_template_and_does_not_archive(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    capture_rel = QUIRK_FILES["current_schema"]
    target = fixture_vault / "projects/blog/ideas.md"
    result = append_to_note(ctx, record_for(fixture_vault, capture_rel), target)

    assert result.ok is True
    assert result.details["archived"] is False
    assert (fixture_vault / capture_rel).exists(), "11 §1: the route layer archives, not append"
    text = target.read_text(encoding="utf-8")
    assert text.endswith(
        "- existing idea one\n"
        "\n"
        f"## {TODAY} — from 2026-06-10T21:33:05.379Z\n"
        "\n"
        "## Content\n"
        "An idea about improv warmups and creative flow.\n"
        # The machine-owned delivered marker rides in the SAME write
        # (CRITICAL-1): it is what makes a retry a no-op instead of a
        # second copy.
        "<!-- organize:appended capture_id=2026-06-10T21:33:05.379Z route= -->\n"
    )
    # target frontmatter untouched except last_edited_date
    assert text.startswith(
        "---\n"
        "title: Blog ideas\n"
        "aliases:\n"
        "- ideas\n"
        "author: Matt Handzel\n"
        "tags:\n"
        "- blog-idea\n"
        "created_date: '2025-11-02'\n"
        f"last_edited_date: '{TODAY}'\n"
        "---\n"
    )
    records = action_records(ctx)
    assert len(records) == 1
    assert records[0]["operation"] == "append"
    assert records[0]["edit_mode"] == "append"
    assert records[0]["targets"][0]["role"] == "append_target"


def test_append_template_with_literal_braces_does_not_raise(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    """08 §B15: ``str.format`` on a template containing literal braces raises."""
    target = fixture_vault / "projects/blog/ideas.md"
    result = append_to_note(
        ctx,
        record_for(fixture_vault, QUIRK_FILES["current_schema"]),
        target,
        template='- {{"from": "{capture_id}"}} {body}',
    )
    assert result.ok is True
    assert '- {{"from": "2026-06-10T21:33:05.379Z"}} ## Content' in target.read_text(encoding="utf-8")


# --- frontmatter operations (05 §5, 07, 08 §A12/§A24) ----------------------


def test_update_frontmatter_preserves_every_unknown_field(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    """08 §A12 at the operation level: the whitelist writer destroyed
    ``no-ai``, ``title`` and every Obsidian property on first rewrite."""
    path = fixture_vault / QUIRK_FILES["no_ai"]
    result = update_frontmatter(ctx, path, {"importance": "high"})
    assert result.ok is True
    assert result.details["changed"] is True
    assert path.read_text(encoding="utf-8") == (
        "---\n"
        "id: private-thought\n"
        "no-ai: true\n"
        "tags:\n"
        "- journal\n"
        "processing_status: raw\n"
        "importance: high\n"
        "---\n"
        "Automated tooling must never write to this note.\n"
    )


def test_update_frontmatter_merges_tags_and_replaces_other_fields(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    path = fixture_vault / QUIRK_FILES["current_schema"]
    update_frontmatter(ctx, path, {"tags": ["Impro", "improv-games"], "processing_status": "triaged"})
    text = path.read_text(encoding="utf-8")
    # case-insensitive dedupe that keeps the ORIGINAL casing and order (08 §A24)
    assert "tags:\n- impro\n- creativity\n- improv-games\n" in text
    assert "processing_status: triaged\n" in text


def test_update_frontmatter_none_removes_a_field(ctx: OperationContext, fixture_vault: Path) -> None:
    path = fixture_vault / QUIRK_FILES["metadata_empty_map"]
    update_frontmatter(ctx, path, {"metadata": None})
    assert path.read_text(encoding="utf-8") == (
        "---\nid: meta-map\ntags:\n- todo\nprocessing_status: raw\n---\nmap-style metadata\n"
    )


def test_update_frontmatter_noop_does_not_rewrite_or_back_up(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    path = fixture_vault / QUIRK_FILES["scalar_tags"]
    before_mtime = path.stat().st_mtime_ns
    result = update_frontmatter(ctx, path, {"title": "Older manual note"})
    assert result.ok is True
    assert result.details["changed"] is False
    assert result.backup_path is None
    assert path.stat().st_mtime_ns == before_mtime
    assert not (fixture_vault / ".backups").exists()


def test_update_frontmatter_backs_up_before_mutating(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    path = fixture_vault / QUIRK_FILES["current_schema"]
    before = path.read_bytes()
    result = update_frontmatter(ctx, path, {"importance": "high"})
    backup = Path(result.backup_path)  # type: ignore[arg-type]
    assert backup == fixture_vault / ".backups" / f"{STAMP}_2026-06-10T21:33:05.379Z.md"
    assert backup.read_bytes() == before


def test_update_tags_records_a_tag_edit(ctx: OperationContext, fixture_vault: Path) -> None:
    path = fixture_vault / QUIRK_FILES["scalar_tags"]
    result = update_tags(ctx, path, ["Daily_Notes", "later"])
    assert result.ok is True
    # scalar tags coerce to a list; the existing value keeps its casing
    assert path.read_text(encoding="utf-8").startswith(
        "---\ntitle: Older manual note\ntags:\n- daily_notes\n- later\n"
    )
    records = action_records(ctx)
    assert [r["operation"] for r in records] == ["tag_edit"]
    assert log_lines(ctx) == [f"[{ISO}] metadata: {path} -> {path} [SUCCESS] Backup: {result.backup_path}"]


def test_update_frontmatter_refuses_unparseable_yaml(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    from organize_core.errors import FrontmatterError

    path = fixture_vault / QUIRK_FILES["broken_yaml"]
    before = path.read_bytes()
    with pytest.raises(FrontmatterError):
        update_frontmatter(ctx, path, {"importance": "high"})
    assert path.read_bytes() == before


def test_update_frontmatter_missing_file_fails(ctx: OperationContext, fixture_vault: Path) -> None:
    result = update_frontmatter(ctx, fixture_vault / "capture/raw_capture/ghost.md", {"a": 1})
    assert result.ok is False
    assert "does not exist" in (result.error or "")


# --- new_folder (05 §6) ----------------------------------------------------


def test_new_folder_creates_and_logs(ctx: OperationContext, fixture_vault: Path) -> None:
    result = new_folder(ctx, "projects", "new-thing")
    assert result.ok is True
    assert result.details == {"para_type": "projects", "created": True}
    folder = fixture_vault / "projects/new-thing"
    assert folder.is_dir()
    assert log_lines(ctx) == [f"[{ISO}] create_folder: {fixture_vault} -> {folder} [SUCCESS]"]
    assert [r["operation"] for r in action_records(ctx)] == ["create_folder"]


def test_new_folder_accepts_the_singular_ui_name(ctx: OperationContext, fixture_vault: Path) -> None:
    assert new_folder(ctx, "project", "singular").ok is True
    assert (fixture_vault / "projects/singular").is_dir()


def test_new_folder_rejects_separators_and_empty_names(
    ctx: OperationContext, fixture_vault: Path
) -> None:
    """An unusable folder name is an ADDRESSING failure, so it RAISES rather
    than returning ok=False — the same taxonomy class as an unknown PARA
    type, so a client sees ONE `error.data.kind` for every way of
    misaddressing `folder.create`."""
    for name in ("", "   ", "a/b", "..", "a\\b"):
        with pytest.raises(ConfigError):
            new_folder(ctx, "areas", name)
    assert not (fixture_vault / "areas/a").exists()


def test_new_folder_rejects_an_unknown_para_type(ctx: OperationContext, fixture_vault: Path) -> None:
    """An unknown PARA type is an ADDRESSING failure: it RAISES rather than
    returning ok=False, so it reaches a client as an error with a taxonomy
    kind. The hint names the keys that would have worked."""
    with pytest.raises(ConfigError) as excinfo:
        new_folder(ctx, "inbox", "x")
    assert "unknown PARA type 'inbox'" in str(excinfo.value)
    assert "areas" in (excinfo.value.hint or "")


def test_new_folder_is_idempotent(ctx: OperationContext, fixture_vault: Path) -> None:
    new_folder(ctx, "areas", "health")
    result = new_folder(ctx, "areas", "health")
    assert result.ok is True
    assert result.details["created"] is False


# --- log_operations = false ------------------------------------------------


def test_log_operations_false_writes_no_log(fixture_vault: Path, tmp_path: Path) -> None:
    ctx = make_ctx(
        fixture_vault, tmp_path / "state", config=make_config(fixture_vault, log_operations=False)
    )
    result = archive_capture(ctx, record_for(fixture_vault, QUIRK_FILES["scalar_tags"]))
    assert result.ok is True
    assert not ctx.oplog.log_file.exists()
    # the action corpus is independent of the operation log (12 §2)
    assert len(action_records(ctx)) == 1


def test_create_backups_false_skips_backups(fixture_vault: Path, tmp_path: Path) -> None:
    ctx = make_ctx(
        fixture_vault, tmp_path / "state", config=make_config(fixture_vault, create_backups=False)
    )
    result = update_frontmatter(ctx, fixture_vault / QUIRK_FILES["current_schema"], {"a": 1})
    assert result.ok is True
    assert result.backup_path is None
    assert not (fixture_vault / ".backups").exists()
