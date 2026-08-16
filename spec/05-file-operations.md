# 05 — File Operations and Safety Guarantees

The safety story is the product's core promise: **no code path may lose note content.** Parity with the `d753672~1` semantics, with the type-confusion and logging bugs fixed.

## 1. Invariants

1. **Never delete.** Every "remove" is a move into the archive tree.
2. **Copy-then-archive.** A move = copy to destination, verify, then archive the original. If any step fails, earlier steps are logged and the original is never left missing: failure before archive leaves the original in place (a duplicate copy may exist — acceptable; data loss is not).
3. **Atomic writes.** Every file write goes through write-to-temp + rename on the same filesystem (temp file in the target's directory, unique name via pid+counter, not just epoch seconds); on failure the target is untouched; temp files are cleaned up. Preserve file permissions.
4. **Backups.** When `file_ops.create_backups`, before overwriting/merging into an existing note, copy it to `<vault>/<file_ops.backup_dir>/<%Y%m%d_%H%M%S>_<filename>` (default `.backups/`).
5. **Operation log.** When `file_ops.log_operations`, append one line per operation to `file_ops.log_file` (default `stdpath("data")/para-organize/operations.log`):
   `[<ts>] <type>: <src> -> <dst> [SUCCESS|FAILED] Error: <msg>` for types `move|archive|merge|create_folder|metadata`. The log must contain enough to manually undo any operation. (⚠ today the logger's `init` is never called so nothing reaches disk; the in-memory `get_recent_operations(n)` / `get_undo_info()` APIs read a global that doesn't exist. One logger module, initialized at setup, backing both the file and the recent-ops API.)
6. **No shelling out.** All filesystem work via `vim.uv`/`vim.fs`/plenary — never `io.popen("find …")`/`os.execute` (six call sites today, three with unquoted paths).

## 2. move_to_destination(capture, destination_folder)

Input: the capture **metadata record** (canonical decision for the table-vs-string contradiction: pass the record; a path-only convenience wrapper may look up the record in the index). Steps:

1. Validate source exists; else log + `false, reason`.
2. Destination folder must exist; if not and `file_ops.auto_create_folders`, mkdir -p; else fail.
3. Backup source (per invariant 4 — source backup optional but current behavior backs up the source too; keep).
4. Destination path = `<dest_folder>/<filename>`; on collision append `_1`, `_2`, … before the extension until free.
5. Copy (atomic write of read content).
6. Frontmatter update on the **copy**: add tag `<singular-type>/<folder-name>` (`project/x`, `area/y`, `resource/z` — singular = configured folder key minus trailing "s"), set `processing_status: organized`, set `last_edited_date` = today. Tag merge dedupes case-insensitively but **preserves existing order and casing** (⚠ today it re-sorts the whole list — don't), appending new tags at the end. All unknown frontmatter fields preserved (see 03 §8).
7. Archive the original (§3).
8. Log `move`, update index (remove capture entry, add destination entry), return `true, destination_path`.

## 3. archive_capture(capture)

Takes the capture metadata record (⚠ today half the call sites pass a string and crash — one signature). Destination: `config.get_archive_path(filename)` = `<vault>/<para_folders.archives>/<paths.archive_capture_path>/<filename>` — **keeping the original filename** (⚠ today it renames to `<id>.md`, destroying the name and breaking `[[wikilinks]]`; get_archive_path already exists and does this right), creating directories as needed, with `_%Y%m%d_%H%M%S` suffix on collision. Use a copy+verify+unlink fallback when rename crosses filesystems; check the result (⚠ `os.rename` return is ignored today). Log `archive`.

## 4. merge_into_note(capture, target_path)

Single implementation shared by both UI entry paths (see 03 §5 for the interactive flow):

- Backup target.
- Result frontmatter: target's fields preserved verbatim; `tags` = target's ∪ capture's (dedupe on normalized form, target-first order); `sources` = target's ∪ capture's (exact-string dedupe); `last_edited_date` = today.
- Result body (non-interactive/API path): `target_body .. "\n\n---\n\n## Merged from <capture filename> on <%Y-%m-%d %H:%M>\n\n" .. capture_body`. Interactive path: the user-edited buffer, which was seeded with exactly that content — instructions are virtual text and can never end up in the file.
- Atomic write, archive capture, log `merge`, reindex target.

## 5. update_frontmatter(path, changes)

General primitive (the tests expect it; today only `update_tags` exists): read file → parse → apply `changes` (map of field→value; `tags` merges per the rule above, other fields replace) → serialize round-trip-safe → atomic write. Used by move (step 6), the metadata-editing feature (07), and merge. Returns boolean. `update_tags(path, new_tags)` remains as a thin wrapper.

## 6. new_folder(type, name)

mkdir `<vault>/<para_folders[type]>/<name>` (validate name: no path separators; reject empty). Log `create_folder`. Refresh folder caches/index dirs. If `ui.auto_move_to_new_folder` and a session is active: `move_to_destination(current_capture, new_folder)`.

## 7. Left-pane editing semantics

The capture pane edits the real file's buffer. Explicit `:w` saves normally (and triggers the incremental reindex hook). Accept/merge/archive with unsaved changes: save the buffer first, then operate (never operate on stale disk content while the buffer differs). Closing the UI with unsaved changes prompts once (`vim.ui.confirm`-style), never discards silently.

## 8. Undo story (parity, not new scope)

`get_undo_info()` returns the last operation with enough detail to reverse it by hand (src, dst, backup path). No automated undo command is in scope — but the operation log plus backups must make every operation manually reversible, and `:ParaOrganize debug` surfaces the recent-operations tail.

## 9. Acceptance tests

- Move: original gone from capture dir, copy present at destination with new tag + `processing_status: organized`, original readable in archive under its **original filename**, operation logged to the real file on disk.
- Move failure injection (destination unwritable): original untouched, error surfaced, log line `FAILED`.
- Collision: second move of same-named file yields `_1` suffix; archive collision yields timestamp suffix.
- Merge: target contains both bodies + separator header exactly once; tags union; unknown target fields (`author:`, `no-ai:`…) byte-preserved; capture archived.
- Frontmatter round-trip property test: parse→serialize over a corpus of real vault files (including `no-ai: true`, nested `location`, `metadata: {}` and `[]`) is lossless for every field, known or unknown.
- Atomicity: kill during write leaves either old or new content, never partial; no temp droppings after success.
- No `find`/shell processes spawned during any operation (assert via mocked `io.popen`).
