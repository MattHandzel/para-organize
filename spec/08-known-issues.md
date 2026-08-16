# 08 — Known Issues Catalog (what NOT to reproduce)

Complete defect inventory from the deep code audit (2026-08-15). Purpose: (a) the rewrite must fix every item; (b) each item marks a spot where current behavior ≠ intended behavior, so parity is measured against the *intended* column of this file, not against HEAD. Items verified by execution are marked ✔exec.

## A. Neovim plugin

### A-FATAL — why HEAD cannot run (refactor damage; reference impl is `d753672~1`)

1. `move.lua` lost its `local config_mod = require(...)` — all moves crash on nil index. ✔exec
2. `indexer.search` missing — called by session start, search pickers, live search, debug. `:ParaOrganize start` dies here. ✔exec
3. `indexer.full_reindex` missing — `reindex` and debug crash.
4. `learn.record_move` missing — every accepted move errors *after* the file moved (inconsistent state), and learning never records (live `learning.json`: `total_moves: 0`).
5. `move.get_recent_operations`/`get_undo_info` reference a nonexistent global `operation_log`. ✔exec
6. `utils/yaml.lua` is a hard Lua syntax error (unescaped newline in a string); orphaned along with `utils/frontmatter.lua` — delete both.

### A-SEVERE — silently wrong results / data loss

7. `string_similarity` returns Levenshtein **distance**, not similarity (refactor dropped the normalize step) — the alias signal is inverted and unbounded, so the *least* similar folder can dominate all other signals. ✔exec (identical→0, dissimilar→10)
8. `ui.render_suggestions` renders nothing (resets state, returns) — suggestion navigation appears frozen; only the initial load renders.
9. Capture pane renders empty: reads `capture.frontmatter`/`capture.body`, fields that don't exist on index records; the working renderer is dead code; and the format string would crash on table values anyway.
10. `indexer/query.lua` stub matches everything (`match` never set false).
11. `suggest/patterns.lua::suggest_new_folders` returns `{}` unconditionally (real impl in `d753672~1`).
12. **`build_frontmatter` silently drops every field outside its 13-key whitelist** — `no-ai`, `title`, Obsidian properties, anything user-added is destroyed on first rewrite. Worst data-loss bug in the codebase; spec fix in 03 §8.
13. `indexing.ignore_patterns` are shell globs applied as Lua patterns — never filter, can throw.
14. `archive_capture` type confusion: written for a metadata table, called with a string from 3 of 5 sites → crash; the two caller groups disagree.
15. `archive_capture` renames to `<id>.md` (destroying the filename and breaking wikilinks), hardcodes `/archives/capture/raw_capture/` ignoring config, and ignores `os.rename` failure (silently no-ops across filesystems).
16. `move/log.lua::init` never called — operations file logging entirely dead despite `file_ops.log_operations=true`.

### A-MODERATE

17. `change_sort_order` wipes its own rendered list with a header-only buffer write.
18. Sort mode "Intelligent Suggestions" calls nonexistent `suggest.get_suggestions` → always throws.
19. `search_inline` calls `get_type_letter`, a local in another module → nil call on top-level folder results.
20. `get_capture_notes` reads wrong config key (`capture_dir` vs `capture_folder`), globs uselessly, and filters on `moved_to`, a field nothing sets.
21. `get_undo_info` defined twice; the broken redefinition wins.
22. `get_association_score` divides by `log(1+total_moves)` → NaN when 0, poisoning every score; no nil guards on malformed learning data.
23. `apply_decay` runs O(n log n) on every accepted move.
24. `update_tags` re-sorts and re-cases the user's tag list.
25. `write_file_atomic` isn't cross-filesystem safe, drops perms, leaks temp files on crash, and collides on same-second writes.
26. `highlight_selection` off-by-one; hardcodes `Visual` ignoring `ui.highlights.selected`.
27. Deprecated APIs: `nvim_buf_set_option`, `nvim_buf_add_highlight` (0.10+).
28. Six `io.popen("find …")` sites, three with unquoted paths — breaks on spaces/metachars, shell-injectable. Replace with `vim.fs`/`vim.uv`.
29. `utils.run_async` references undeclared `Job`.
30. `config.get_value/set_value` can't address keys containing dots.
31. `<CR>` in organize pane double-bound (accept then open_item; open_item wins) — intended dispatch specced in 03 §3.
32. Two divergent merge implementations; the browse-path one writes its own instruction text into the target file (03 §5 / 05 §4).
33. Session/command arg parsing works by coincidence (`table.remove` then reading `args[1]`).
34. `keymaps.buffer.refresh` (`r`) and `toggle_preview` (`p`) documented but never bound; `s` double-purposed (skip vs sort-cycle) across panes.
35. ~25 dead config keys (list in 03 §1) — every one must be honored or deleted.
36. Tests describe a materially different API in five places (promise-vs-callback reindex, table-vs-string move, string-vs-array criteria, file-vs-folder suggestion paths, features-vs-capture association score) — resolutions are specced in 03/04/05; rewrite tests must match the spec, not the old tests.
37. Test infra: `minimal_init.lua` hardcodes an absolute repo path; `helpers.clean_test_output` can `rm -rf /output/*` if an env var is unset — guard it.
38. Doc drift: MANUAL says `j/k` navigate suggestions (actually `<A-j>/<A-k>`); README/defaults disagree on float size (0.9 vs 0.8); MANUAL omits `context_match` and the hardcoded type bonus; `indexing.backend="sqlite"` advertised, never implemented; CHANGELOG describes a working v0.1.0 that never worked end-to-end.

## B. Python automation pipeline

### B-CRITICAL — the 3-month outage

- **B1** `task export` output decoded as strict UTF-8 (`text=True`, no `errors=`) → `UnicodeDecodeError` every run since 2026-05-10; no subprocess timeouts either. ✔exec (live journal)
- **B2** All consumers constructed eagerly before `--consumer`/`--list-consumers` filtering, and constructors do I/O — one bad consumer kills all four.
- **The wrapper swallows failure**: vault-side `second-brain-automation.py` catches, prints, exits 0 — systemd saw "success" for 3 months. Fix: propagate exit codes + `OnFailure=` alert.

### B-SEVERE

- **B3** `limit`-status results are checkpointed → notes over the per-run cap are silently dropped forever (contradicts TASKWARRIOR.md's documented retry).
- **B4** Filter misses are persisted per-hash → config changes never apply retroactively (defeats the stated design goal in `person-research-agent.md`) and bloat the DB (~30k rows, 98% `filtered`, 15 MB).
- **B5** `purge_missing` hard-deletes emission history for any path not seen this run — narrowing `scan_dirs` or a transient empty dir permanently forgets LLM-run checkpoints.
- **B6** Unbounded Taskwarrior backups: 43 full copies of `~/.task`, 108 MB, no retention.
- **B7** `remove_unknown_tags` silently drops `additional_tags` (only `review_tag` is whitelisted); changes dedupe keys as a side effect.
- **B8** No frontmatter writer exists in Python; `learn`'s `processing_status == "learn-processed"` guard is dead code; edits re-run everything and duplicate output files (contributor to 901 files in `flashcards/review/`). Resolution: real write-back (06 §3.2).
- **B9** `question_answer` triggers on almost anything (`?` in <500 chars, leading "is/are/can/how"), re-parses frontmatter by hand (sweeping aliases/sources/modalities in as "tags"), reads each file three times, case-sensitive tag check. 874 mostly-noise answer files. Resolution: explicit-tag trigger, heuristic opt-in (06 §3.3).

### B-MODERATE

- **B10** `aliases[0]` on a string value yields its first character (titles degrade to one letter).
- **B11** LLM error handling misses `TimeoutError`/`JSONDecodeError` — the *expected* remote-Ollama failures crash the consumer.
- **B12** Checkpoint written in two places (consumer AND orchestrator) — single-owner rule in 06 §1.
- **B13** Skips consume `max_notes_per_run` budget.
- **B14** Operator-precedence bug in the no-scan-dirs error; generator-deferred raise caught by accident.
- **B15** `deep_research` env guard checks `notes_dir` but sets `NOTES_DIR` (clobbers caller's value); `str.format` on commands containing literal braces raises.
- **B16** `frontmatter.read_note` splits on the substring `---` anywhere (breaks on `---` in values); `capture_query.py` has the correct line-based implementation — unify on it.
- **B17** Docs describe nonexistent APIs (`with_transaction`, `NoteEmitter.sync`, `scripts.automation.registry`, `extract_project_tag`, a `metadata` table), reference a nonexistent `para-automation.sh`, and disagree on default model (`gemma3:12b-it-qat` vs code default `gemma4:e4b` — the latter isn't a real model tag).
- **B18** Assorted: dead code (`NoteState.is_new/.changed`, `iter_notes`, `iter_consumer_emissions`, `to_status_kwargs`, unused `triage_threshold` plumbing); `filtered` uncounted in summaries; daily-note exclusion undocumented; hardcoded hosts (`server.matthandzel.com:11434`/`:47770`) and home paths; dual 3.11/3.12 pycache; missing `encoding=` on many I/O calls; `relative_to(vault_root)` `ValueError` via symlinked scan dirs.

## C. Configuration/deployment defects (live environment)

1. Matt's nvim config: `vault_dir = "~/Obsidian/Main/notes"` — wrong tree (correct: `~/Obsidian/Main` or `~/notes`). Rewrite's health check must catch this class (capture folder absent under vault_dir ⇒ error).
2. `para_folders.archives = "archives"` configured everywhere; vault has `archive/` — same health-check class.
3. Repo `scripts/systemd/para-automation.service` ExecStarts a file that does not exist.
4. Repo vs live `automations.toml` diverge on scan_dirs, model, enabled consumers — single source of truth going forward (06 §2).
5. Stale `index.json` entries under the wrong root; regenerate index on first run of the rewrite.
