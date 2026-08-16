# 03 — Neovim Plugin Functional Specification

This is the parity contract for the interactive plugin. **Important provenance note:** the current git HEAD is non-functional — a mechanical "split modules" refactor (commits between `d753672~1` and HEAD, Sep–Oct 2025) left stubs and dropped declarations, so `:ParaOrganize start` crashes before showing UI. The behavioral reference for this spec is **commit `d753672~1`** (the last state with complete implementations) plus the *intent* documented in README/MANUAL/PLAN and the test suite. Where those sources conflict, the resolution is stated explicitly here. Bug inventory: `08-known-issues.md`.

## 1. Plugin surface

- Name/require: `para-organize`; `require("para-organize").setup(config)`; version string `0.1.0` in `init.lua`.
- Load guard `vim.g.loaded_para_organize`; hard error below Neovim 0.9 (target 0.10+ APIs in the rewrite: `vim.bo[buf]`, extmarks, `vim.fs`/`vim.uv` — no `io.popen("find …")` anywhere).
- Dependencies: plenary.nvim, telescope.nvim, nui.nvim (required); which-key, nvim-web-devicons (optional).
- Full config schema and defaults: keep exactly the schema in README/`config/defaults.lua` (reproduced in the explorer notes; treat README's block as normative), **plus** the new `metadata_fields` section (`07-metadata-editing.md`). Rule for the rewrite: **every config key must be either honored or deleted.** The current tree ships ~25 dead keys (all of `ui.display.*`, `ui.highlights.*`, `telescope.layout_*`/`picker_opts`, `patterns.alias_extraction`, `patterns.case_sensitive`, `file_ops.log_operations`, `file_ops.confirm_operations`, `keymaps.buffer.refresh`, `keymaps.buffer.toggle_preview`, `suggestions.learning.min_confidence`, `indexing.backend="sqlite"`, `debug.profile`, `keymaps.global`). Wire them up per their documented meaning (preferred where the meaning is obvious — e.g. `ui.display.show_scores` gates score rendering, `ui.highlights.*` feed the highlighter) or drop them from the schema with a validation error naming the removed key.
- Config validation must check leaf types/enums/ranges (current `validate.lua` only asserts six sections are tables) and must verify at setup/health time that `vault_dir`, capture folder, and each `para_folders` entry exist — warning loudly on mismatch (this would have caught both live misconfigurations: `vault_dir` pointing at `~/Obsidian/Main/notes` and `archives` vs the real `archive`).

## 2. Commands

`:ParaOrganize [subcommand] [args…]`, `nargs="*"`, with completion. No subcommand ⇒ `start`.

| Subcommand | Behavior |
|---|---|
| `start [k=v …]` | Begin session over captures matching filters (below). Builds capture list, opens UI, loads capture 1. If zero captures match: notify "No captures found matching filters", do not open UI. |
| `stop` | Close UI, discard session (files already processed stay processed). |
| `next` / `prev` (alias `previous`) | Move to adjacent capture; clamp at ends with a notice. |
| `skip` | Mark current capture skipped for this session; advance. |
| `move <path>` | Move current capture to an explicit destination folder path (relative to vault or absolute). Requires active session. |
| `merge` | Enter merge mode via pickers (folder → note). |
| `archive` | Archive current capture immediately. |
| `reindex` | Full rebuild of the index; report `{total, duration}` on completion. |
| `search <query>` | Open Telescope search picker over the index. |
| `debug` | Show diagnostics: config paths + existence, index stats, capture detection counts, session state. |
| `help` | Help popup listing all active keymaps (must be generated from the actual keymap table, never hand-maintained). |
| `new-project [name]`, `new-area [name]`, `new-resource [name]` | Create folder under the corresponding PARA root (prompt for name if absent). With `ui.auto_move_to_new_folder=true` and an active capture: move it there. |

Argument parsing: subcommand = argv[1]; filters parse `([^=]+)=(.+)`; `search` joins remaining argv with spaces; `move`/`new-*` take argv[2] (the current code re-reads argv[1] after a `table.remove` and works only by coincidence — implement deliberately).

`:ParaOrganizeHealth` → `:checkhealth para-organize`. Health checks: nvim version, required/optional plugins, vault + each PARA folder + capture folder existence and writability, data/log dirs, index stats, note count with a >5000 performance warning. (Drop the `find`/`grep` executable checks — the rewrite must not shell out.)

`<Plug>` mappings (13): `ParaOrganizeStart/Stop/Reindex/Search/Accept/Merge/Archive/Next/Prev/Skip/NewProject/NewArea/NewResource`. No global keymaps by default.

### Session filters

`tags=a,b` `sources=a,b` `modalities=a,b` (comma lists, OR within a filter, AND across filters), `since=YYYY-MM-DD`, `until_date=YYYY-MM-DD` (compare against note timestamp/created_date), `status=raw` (matches `processing_status`). Defaults when no filters: `status=raw`, restricted to the capture folder (`para_type=="capture"`). Filter values are lists; the query layer must accept both a string and a list for each criterion (a current string-vs-array mismatch between tests and callers — resolve by canonicalizing to lists at the boundary).

Capture ordering within a session: oldest first by `timestamp` (fall back to file mtime). Deterministic order is required; document whatever tiebreak is chosen (path ascending).

## 3. The two-pane UI

Two side-by-side nui popups in one layout, 50/50: left **" Capture "**, right **" Organize "**. Float mode (default): 80%×80% of the editor (config `ui.float_opts.width/height`; note README documents 0.9/0.8 while `defaults.lua` has 0.8/0.8 — make README and defaults agree at 0.8), centered, rounded border. Split mode supported via `ui.layout="split"`. Both buffers `filetype=markdown`, no swapfile, cursorline on.

### Left pane (capture pane)

Shows the current capture, **fully editable** — it must behave as a real buffer on the real file (see File Ops for save semantics). Rendered header before the body, controlled by `ui.display.*`:

- Session position: "Capture i of n".
- Timestamp in `ui.display.timestamp_format` (`%b %d, %I:%M %p`).
- Aliases (excluding the capture_id alias when `hide_capture_id=true`), tags, sources, context.
- Modalities and location hidden by default (`hide_modalities`, `hide_location`).
- Metadata summary line for `metadata_fields` values (new, see 07).

(The current code has two capture renderers, one dead and one reading fields that don't exist, so the pane renders empty — the rewrite has ONE renderer, fed by the real note file + index metadata.)

### Right pane (organize pane) — four states

1. **Suggestions list** (initial): header ("Suggestions — sort: <mode>"), then for each suggestion: icon/type letter `[P]`/`[A]`/`[R]`/`[🗑]`, folder name, score (rendered when `ui.display.show_scores`, colored via `score_high/medium/low` highlight groups at thresholds ≥2.0 / ≥1.0 / else), and indented reason lines. Selection is highlighted (`ui.highlights.selected`); `<A-j>/<A-k>` move the selection from either pane; `<CR>` on a suggestion accepts it.
2. **Directory browse**: after `<CR>` on a folder line, show its children: `[D] name` for dirs, `[F] alias-or-name` for files (alias = frontmatter `aliases[1]`, fall back to filename). A directory stack supports `<BS>` back-to-parent. Sort cycles with `s` between: Alphabetical (dirs first, case-insensitive), Last Modified (dirs first), Intelligent Suggestions (score-ordered — this mode must work; it currently crashes on a wrong function name).
3. **Search results**: `/` prompts inline; results shown with type letters; context-aware — scoped to the currently browsed folder if inside one, else vault-wide.
4. **Merge editor**: see §5.

`<CR>` dispatch in the right pane is by line prefix: `[P]/[A]/[R]/[D]` descend; `[F]` start merge; suggestion line ⇒ accept. `<C-h>`/`<C-l>` switch panes. `<Esc>` closes the whole UI from either pane (equivalent to `stop`). Closing either window (WinClosed) tears the session down cleanly — no orphan buffers/windows/autocmds.

### Keymaps (buffer-local, from `keymaps.buffer`, all rebindable; collision-check at setup)

| Key (default) | Config key | Action |
|---|---|---|
| `<CR>` | accept | Accept selection / open item (context-dependent as above) |
| `<Esc>` | cancel | Close UI |
| `<Tab>` / `<S-Tab>` | next / prev | Next / previous capture |
| `s` | skip | Skip capture (capture pane). In the organize pane `s` cycles sort — resolve this overload: keep `s`=skip globally and move sort-cycle to `S` (documented change; the old double-binding was accidental) |
| `a` | archive | Archive capture now |
| `m` | merge | Merge via pickers |
| `/` | search | Inline destination search |
| `r` | refresh | Re-run suggestion generation for current capture (must actually be bound) |
| `p` | toggle_preview | Toggle rendering of the right pane preview of the selected item (must actually be bound, or delete the option) |
| `?` | help | Help popup |
| `<A-j>` / `<A-k>` | (fixed) | Next / previous suggestion |
| `<C-h>` / `<C-l>` | (fixed) | Focus capture / organize pane |
| `<BS>` | (fixed) | Back to parent while browsing |
| `<leader>np/na/nr` | new_project/new_area/new_resource | Create + optionally move |
| `<leader>mc` / `<leader>mx` | (merge mode only) | Complete / cancel merge (ONE completion binding — drop the stray `<C-s>` variant) |
| metadata field keys | `metadata_fields[*].keymap` | See 07 |

MANUAL.md's claim that `j/k` navigate suggestions is wrong; `j/k` remain plain cursor movement (the in-app help is the accurate source — keep docs generated or cross-checked).

## 4. Telescope integration

- `open_search_picker(query)` — search index with criteria; dropdown theme (respect `telescope.theme`, `layout_strategy`, `layout_config`, `previewer`, `multi_select`).
- `open_folder_picker(on_select)` — pick a PARA destination folder (used by merge method 2 and `move`).
- `open_folder_notes_picker(folder, on_select)` — pick a note within a folder (merge method 2, step 2).
- Saved searches picker with the nine built-ins: Unprocessed Captures, Today's Notes, This Week, With Audio, Meeting Notes, No Tags, Projects, Areas, Resources.
- `live_search()` — live-updating query over the index.
- Folder listing cache keyed by root path, invalidated on directory mtime change (keep this; it's the only caching layer and it works).

## 5. Merge

One implementation, one semantics (today there are two divergent ones — the picker path does frontmatter merging, the browse path pastes raw buffers and even writes its instruction header into the target; that is a bug, not a feature).

Entry points: `m` (pickers) or `<CR>` on a `[F]` line while browsing. Both converge on the same merge session:

1. Right pane becomes an editor containing: the **target note's frontmatter + body**, a visible separator, and the capture's body under a header `## Merged from <capture filename> on <YYYY-MM-DD HH:MM>`. Instructional text is displayed as **virtual text / winbar only** — never as buffer lines that could be saved.
2. Matt edits freely.
3. `<leader>mc` completes: frontmatter of the result = target's frontmatter with tags = target ∪ capture (deduped case-insensitively, target's first), sources = target ∪ capture, `last_edited_date` = today, all other target fields preserved; body = the edited buffer content. Target backed up first; write atomic; capture archived; operation logged as `merge`.
4. `<leader>mx` cancels: right pane returns to the previous state, nothing written.

## 6. Session outcome per capture

| Action | File effect | Learning | processing_status |
|---|---|---|---|
| Accept / `move` | Copied to destination folder (collision ⇒ `_1`, `_2`… suffix), tag `<type>/<folder>` added (singular type: `project/x`, `area/y`, `resource/z`), original archived | `record_move` fires | set to `organized` on the moved copy |
| Merge | Target updated, capture archived | `record_move` fires with the target's folder | target `last_edited_date` updated |
| Archive | Original moved to archive under `archive_capture_path`, keeping its filename (collision ⇒ timestamp suffix via `get_archive_path`) | none | unchanged |
| Skip | none | none | unchanged |
| Metadata edit (07) | frontmatter updated in place | none | unchanged |

After any of the first three, auto-advance to the next unprocessed capture; when the session is exhausted show a completion message with counts (processed/skipped) and close or idle per current behavior (close).

*(Design note, deliberate parity decision: `processing_status: organized` is written by the plugin on the moved copy; the pre-refactor code intended this via `update_frontmatter` — keep it, since the Python pipeline and future tooling filter on `processing_status`.)*

## 7. Indexer contract (plugin-side)

- Scope: recursive scan of `vault_dir` for `patterns.file_glob` (`**/*.md`), honoring `.gitignore` semantics (plenary `respect_gitignore=true` today) and `indexing.ignore_patterns` — which are **globs**; convert glob→Lua-pattern (or use `vim.glob`) instead of the current broken raw `:match`. Skip files larger than `indexing.max_file_size` (1 MiB) with a warning.
- Per-file metadata record (the shared shape used by UI, search, suggest, learn):
  `{ path, filename, title, para_type, folder, timestamp, id, aliases[], capture_id, tags[], sources[], modalities[], context, location, metadata, processing_status, created_date, last_edited_date, size, modified, indexed_at, normalized_tags[] }`
  — title = first `# heading` else filename stem; scalar frontmatter values coerced to single-element lists for tags/aliases/sources/modalities; parse failures never abort the scan (log + index with empty metadata).
- `para_type`: derived from the path relative to vault: one of `projects|areas|resources|archives` (the configured folder names' keys), `capture`, or `other`.
- Persistence: JSON snapshot in `stdpath("data")/para-organize/index.json`. Rewrite requirements: include a schema-version field; prune entries whose files no longer exist on load/scan; don't rewrite the whole file on every single-file update (batch/debounce saves). Full `reindex` command rebuilds from zero with a reentrancy guard.
- Incremental: debounced (`indexing.incremental_debounce` ms) `BufWritePost *.md` hook re-indexes the written file when `indexing.auto_reindex` — and also removes entries for deleted files.
- API contract decisions (resolving test/code contradictions): `full_reindex(callback)` callback-style (the promise-style in two specs was aspirational; callback is what `init.reindex` consumes); `search(criteria)` takes lists for multi-value criteria; queries return metadata records.
- The `indexing.backend="sqlite"` option is vaporware — delete the option (JSON only) unless the rebuild chooses to actually implement it.

## 8. YAML / frontmatter handling (plugin-side)

- Extract: file starts with `---` line; frontmatter runs to the next `---` line; else no frontmatter, whole file is body. Line-based scan (never `split("---",2)`-style, which breaks on `---` horizontal rules).
- Parse: lyaml when available; otherwise the built-in fallback parser must support: nested maps, `- ` block lists, inline `[a, b]` / `{k: v}`, quoted strings, booleans/numbers, comments, and the real-world quirks in `02-system-context.md` (scalar-vs-list, `metadata: {}` vs `[]`).
- **Serialize: round-trip safe.** Known fields render in the canonical order `timestamp, id, aliases, capture_id, tags, sources, modalities, context, location, metadata, processing_status, created_date, last_edited_date` — and **all unknown fields are preserved** (appended after, original relative order), never dropped. The current writer silently destroys any field outside the whitelist (`no-ai`, `title`, Obsidian properties…) — this is the single worst data-loss bug and priority #1 for correctness tests.
- Preserve list formatting style per field where feasible; at minimum, never turn a populated field into a different type.
