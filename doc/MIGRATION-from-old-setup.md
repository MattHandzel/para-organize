# Migrating your `require("para-organize").setup{}` table

**Why this document exists.** Spec 10 §3 splits the old single configuration
table in two:

> The nvim `setup()` table keeps only UI concerns (layout, icons, highlights,
> keymaps) plus the socket path; everything behavioral lives in the core config
> so CLI and UI can never disagree. **Ship a migration note mapping every old
> `setup()` key to its new home.**

This is that note. It covers every key in the pre-rewrite defaults table
(`git show d753672~1:lua/para-organize/config.lua`) plus the additions of spec
07 and 11–13.

Two homes now exist:

| Home | File | Holds |
|---|---|---|
| **core** | `~/.config/organize-core/config.toml` | vault paths, indexing, suggestions, file operations, metadata fields, routes, consumers, LLM, edit modes |
| **plugin** | `require("para-organize").setup{}` in your Neovim config | layout, icons, highlights, display toggles, keymaps, telescope theme, and the socket path / core command |

Both sides enforce the same law (spec 03 §1): **every key is honored or
deleted.** An unknown key is a loud validation error naming the exact dotted
key — the plugin's error additionally tells you which of the two homes the key
moved to, so you can usually fix a config from the message alone.

---

## 1. The shortest possible migration

If you never customized much, this is the whole thing:

```lua
-- BEFORE (pre-rewrite)
require("para-organize").setup({
  paths = {
    vault_dir = vim.fn.expand("~/notes"),
    capture_folder = "capture/raw_capture",
    para_folders = { projects = "projects", areas = "areas",
                     resources = "resources", archives = "archive" },
  },
})
```

```lua
-- AFTER — nvim side
require("para-organize").setup({})   -- defaults are fine
```

```toml
# AFTER — ~/.config/organize-core/config.toml
[vault]
root = "~/notes"
capture_folder = "capture/raw_capture"

[vault.para_folders]
projects  = "projects"
areas     = "areas"
resources = "resources"
archives  = "archive"        # SINGULAR on disk — see §5
```

Generate a fully commented starting point with:

```sh
organize health --example-config > ~/.config/organize-core/config.toml
```

---

## 2. The complete key map

### 2.1 `paths.*` → core `[vault]`

| Old plugin key | New home |
|---|---|
| `paths.vault_dir` | `[vault] root` |
| `paths.capture_folder` | `[vault] capture_folder` |
| `paths.para_folders.{projects,areas,resources,archives}` | `[vault.para_folders]` |
| `paths.archive_capture_path` | `[vault] archive_capture_path` |

### 2.2 `patterns.*` → core `[vault]`, or deleted

| Old plugin key | New home |
|---|---|
| `patterns.file_glob` | `[vault] file_glob` |
| `patterns.frontmatter_delimiters` | **deleted** — the frontmatter reader is line-based by contract (spec 03 §8); the delimiter is `---`, not configurable |
| `patterns.tag_normalization` | `[vault] tag_normalization` |
| `patterns.alias_extraction` | **deleted** — dead key (08 §A35) |
| `patterns.case_sensitive` | **deleted** — dead key (08 §A35); tag matching is case-insensitive by contract (spec 04 §2) |

### 2.3 `indexing.*` → core `[vault]`, or deleted

| Old plugin key | New home |
|---|---|
| `indexing.ignore_patterns` | `[vault] ignore_patterns` (now real globs — the old raw `:match` was broken, 08 §A11) |
| `indexing.max_file_size` | `[vault] max_file_size` |
| `indexing.incremental_debounce` | `[vault] incremental_debounce` |
| `indexing.auto_reindex` | `[vault] auto_reindex` |
| `indexing.backend` | **deleted** — `"sqlite"` was advertised and never implemented (08 §A35/§A38). The index format is the core's internal business (spec 02) |

### 2.4 `suggestions.*` → core `[suggestions]`

| Old plugin key | New home |
|---|---|
| `suggestions.weights.*` | `[suggestions.weights]` — same six signals, same defaults (spec 04 §2) |
| `suggestions.learning.recency_decay` | `[suggestions.learning] recency_decay` |
| `suggestions.learning.frequency_boost` | `[suggestions.learning] frequency_boost` |
| `suggestions.learning.max_history` | `[suggestions.learning] max_history` |
| `suggestions.learning.min_confidence` | **deleted** — dead key (08 §A35) |
| `suggestions.max_suggestions` | `[suggestions] max_suggestions` |
| `suggestions.always_show_archive` | `[suggestions] always_show_archive` |

### 2.5 `file_ops.*` → core `[file_ops]`, or deleted

| Old plugin key | New home |
|---|---|
| `file_ops.atomic_writes` | `[file_ops] atomic_writes` |
| `file_ops.create_backups` | `[file_ops] create_backups` |
| `file_ops.backup_dir` | `[file_ops] backup_dir` |
| `file_ops.auto_create_folders` | `[file_ops] auto_create_folders` |
| `file_ops.log_operations` | **deleted** — the operation log is unconditional (spec 05 §1.5); an audit trail you can switch off is not an audit trail |
| `file_ops.log_file` | **deleted** — resolved by the core: `~/.local/share/organize-core/operations.log` (override the whole state dir with `$ORGANIZE_CORE_STATE_DIR`) |
| `file_ops.confirm_operations` | **deleted** — dead key (08 §A35); confirmation is a client concern |

### 2.6 `ui.*` → **stays in the plugin** (with renames)

| Old plugin key | New plugin key |
|---|---|
| `ui.layout` | `ui.layout` — `"float"` \| `"split"` |
| `ui.float_opts.width` / `.height` | `ui.float_opts.width` / `.height` — **default is now 0.8/0.8** (README said 0.9/0.8, defaults said 0.8/0.8; spec 03 §3 settles on 0.8) |
| `ui.float_opts.border` | `ui.float_opts.border` |
| `ui.float_opts.position` | **deleted** — the float is always centered |
| `ui.split_opts.*` | **deleted** — split mode has no size/direction knobs |
| `ui.icons.enabled` | **deleted** — an EMPTY `ui.icons` (the default) renders spec 03's literal `[P]`/`[A]`/`[R]`/`[🗑]`; set the per-type keys to opt into glyphs |
| `ui.icons.project/area/resource/archive` | unchanged |
| `ui.icons.folder` | `ui.icons.dir` |
| `ui.icons.file` | unchanged |
| `ui.icons.tag` | **deleted** — tags render in the capture header, not as icons |
| `ui.display.show_scores` | `ui.display.show_scores` — **now actually honored** (it gates score rendering; it was one of ~25 dead keys, 08 §A35) |
| `ui.display.show_counts` | `ui.display.show_position` ("Capture i of n") |
| `ui.display.show_timestamps` | **deleted** — the header timestamp always renders; format it with `timestamp_format` |
| `ui.display.timestamp_format` | unchanged |
| `ui.display.hide_capture_id` / `hide_modalities` / `hide_location` | unchanged, and now honored |
| *(new)* | `ui.display.show_reasons` — the indented reason lines under each suggestion |
| *(new)* | `ui.display.show_metadata_summary` — the spec 07 summary line |
| `ui.highlights.selected` | unchanged, and now honored |
| `ui.highlights.score_high/medium/low` | unchanged, and now honored |
| `ui.highlights.project/area/resource/archive/tag` | **deleted** — per-PARA-type highlight groups were dead keys (08 §A35) |
| *(new)* | `ui.highlights.header`, `.reason`, `.hint` |
| `ui.auto_move_to_new_folder` | unchanged — **default is now `false`** (creating a folder and silently relocating the open capture surprised more than it helped) |
| *(new)* | `ui.capture_pane_keymaps` — `"core"` (spec parity, default) \| `"navigation"` \| `"none"`. Spec 03 binds `a`/`s`/`m`/`r`/`p` in BOTH panes, which shadows those keys in the editable capture buffer (spec 10 §4). Set `"navigation"` to reclaim them |
| *(new)* | `ui.close_on_complete` — spec 03 §6 closes the UI when the session is exhausted; `false` keeps the completion notice on screen |

### 2.7 `telescope.*` → **stays in the plugin** (trimmed)

| Old plugin key | New plugin key |
|---|---|
| `telescope.theme` | unchanged (`"dropdown"` default, spec 03 §4) |
| `telescope.layout_strategy` | unchanged, and now honored |
| `telescope.layout_config` | unchanged, and now honored |
| `telescope.previewer` | unchanged |
| `telescope.multi_select` | unchanged |
| `telescope.picker_opts` | **deleted** — dead key (08 §A35) |

### 2.8 `keymaps.*` → **stays in the plugin**

| Old plugin key | New plugin key |
|---|---|
| `keymaps.global` | **deleted** — there are no global keymaps by default (spec 03 §2). Bind the 13 `<Plug>` mappings yourself, e.g. `vim.keymap.set("n", "<leader>oo", "<Plug>(ParaOrganizeStart)")` |
| `keymaps.buffer.accept/cancel/next/prev/skip/archive/merge/search/refresh/toggle_preview/help` | unchanged — and `refresh` (`r`) and `toggle_preview` (`p`) are now actually bound (08 §A34) |
| `keymaps.buffer.new_project/new_area/new_resource` | unchanged |
| *(new)* | `keymaps.buffer.sort_cycle` — default `S`. Spec 03 §3 resolves the old accidental `s`/`s` double-binding: `s` stays skip everywhere, sort-cycle moves to `S` |
| *(new)* | `keymaps.buffer.next_suggestion` / `prev_suggestion` (`<A-j>` / `<A-k>`) |
| *(new)* | `keymaps.buffer.focus_capture` / `focus_organize` (`<C-h>` / `<C-l>`) |
| *(new)* | `keymaps.buffer.back` (`<BS>`, back-to-parent while browsing) |
| *(new)* | `keymaps.buffer.merge_complete` / `merge_cancel` (`<leader>mc` / `<leader>mx`) — ONE completion binding; the stray `<C-s>` variant is gone |

Set any binding to `false` to unbind it. A collision between two bindings is a
**setup-time error** naming both (spec 07 acceptance test 4).

### 2.9 `debug.*` → deleted / core `[logging]`

| Old plugin key | New home |
|---|---|
| `debug.enabled` | **deleted** — use `:ParaOrganize debug` for client diagnostics |
| `debug.log_level` | core `[logging] level`, and `organize --debug <subcommand>` for a one-off |
| `debug.log_file` | **deleted** — the core logs to its state dir; the plugin's own core-process log is `core.core_log` if you want one |
| `debug.profile` | **deleted** — dead key (08 §A35) |

### 2.10 New in the plugin table: reaching the core

None of these existed before, because there was no core process.

```lua
require("para-organize").setup({
  -- The one non-UI value the plugin still owns (spec 10 §3).
  socket_path = vim.env.XDG_RUNTIME_DIR .. "/organize-core.sock",  -- the default
  core_cmd = { "organize", "serve" },                              -- the default

  core = {
    spawn = true,            -- auto-spawn the core when nothing is listening
    spawn_timeout_ms = 2000, -- how long a cold start may take
    timeout_ms = 5000,       -- per-request budget
    reconnect = true,        -- one reconnect attempt, then degrade loudly
    core_env = {},           -- extra env for the spawned core
    core_cwd = nil,
    core_log = nil,          -- redirect the spawned core's stdout/stderr here
  },
})
```

`socket_path` and `core_cmd` may also be given inside `core`; the top-level
spelling wins. **Keep the socket path short** — AF_UNIX caps it at ~104 bytes,
and both `setup()` and `:checkhealth para-organize` refuse a longer one,
because the core can only report the overrun as a bare `OSError`.

### 2.11 `metadata_fields` (spec 07) lives in the CORE

Spec 07 introduced `metadata_fields` as a `setup()` section. Spec 10 §3
supersedes that: it is core config, so that `organize set-meta` and the `t`/`i`
keys in the UI cannot disagree about what fields exist or how their values
normalize.

```toml
# ~/.config/organize-core/config.toml
[[metadata_fields]]
key      = "tags"
type     = "list"
keymap   = "t"
prompt   = "Add tag(s)"
append   = true
complete = "existing"
normalize = "kebab"

[[metadata_fields]]
key    = "importance"
type   = "enum"
keymap = "i"
values = ["high", "medium", "low"]
```

The plugin fetches this over RPC (`meta.fields`) and binds the keymaps in the
organize pane at session start — no plugin-side configuration, and adding a
field still requires zero code changes.

### 2.12 Sections that never existed in `setup{}`

`[[routes]]` (spec 11), `[consumers]` (spec 06), `[llm]` (spec 06),
`[integrate]` / edit modes (spec 12), `[auto]` (spec 13) are all core-only.
They are listed here so a search for a key name lands somewhere.

---

## 3. State and log locations

Everything the old plugin wrote under `stdpath("data")/para-organize/` now
lives in one place, `~/.local/share/organize-core/`:

| Old | New |
|---|---|
| `stdpath("data")/para-organize/index.json` | `~/.local/share/organize-core/index.json` |
| `stdpath("data")/para-organize/learning.json` | `~/.local/share/organize-core/learning.json` |
| `stdpath("data")/para-organize/operations.log` | `~/.local/share/organize-core/operations.log` |
| — | `~/.local/share/organize-core/actions/YYYY-MM.jsonl` (spec 12) |
| `~/.local/state/para-organize/automations.db` | `~/.local/share/organize-core/automations.db` (spec 06 §1 migration) |

Override the whole tree with `$ORGANIZE_CORE_STATE_DIR` (and
`$ORGANIZE_CORE_CONFIG_DIR` / `$ORGANIZE_CORE_RUNTIME_DIR`). Per spec 09 §5,
do NOT carry the stale `index.json` over — regenerate with
`:ParaOrganize reindex`, and start `learning.json` fresh.

---

## 4. A complete "after" example

```lua
require("para-organize").setup({
  socket_path = vim.env.XDG_RUNTIME_DIR .. "/organize-core.sock",

  ui = {
    layout = "float",
    float_opts = { width = 0.8, height = 0.8, border = "rounded" },
    capture_pane_keymaps = "navigation",  -- keep a/s/p usable while editing
    display = { show_scores = true, show_reasons = true, hide_location = true },
    highlights = { selected = "Visual", score_high = "DiagnosticOk" },
    icons = { project = "󰃀 ", area = "󰝰 ", resource = "󰈙 " },
  },

  keymaps = {
    buffer = {
      skip = "s",
      sort_cycle = "S",
      merge_complete = "<leader>mc",
    },
  },

  telescope = { theme = "dropdown", previewer = false },
})

vim.keymap.set("n", "<leader>oo", "<Plug>(ParaOrganizeStart)")
vim.keymap.set("n", "<leader>or", "<Plug>(ParaOrganizeReindex)")
```

---

## 5. Two live misconfigurations to fix while you are in here

Both were real, both caused outages, and both are now caught by
`:checkhealth para-organize` (spec 09 §5.1):

1. **`vault_dir` pointed at `~/Obsidian/Main/notes`**, which does not exist —
   the vault is `~/Obsidian/Main`, a.k.a. `~/notes`. Set `[vault] root` to the
   real path.
2. **`para_folders.archives` was `"archives"`**, but the folder on disk is
   `archive`, SINGULAR (08 §C2). The config key stays plural (it is the PARA
   *kind*); its VALUE must be the real directory name:

   ```toml
   [vault.para_folders]
   archives = "archive"
   ```

Run `:checkhealth para-organize` after migrating. It checks the plugin side
itself and relays `organize health --json` for everything vault-side, so the
two doors cannot give you different answers.
