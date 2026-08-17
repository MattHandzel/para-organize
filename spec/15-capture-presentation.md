# 15 — Capture Presentation, Field Policy, and Custom UI (NEW)

Directive from Matt (2026-08-16), three parts:
> *"btw, for both windows in ParaOrganize, by default they are folded on both sides, which is annoying."*
> *"can you make it so that there is a better UI? instead of opening the raw capture, i should be able to specify specific fields that I would want to keep open at all times (for example, context, tags, sources, etc.), the rest (location, processing_status, created_date, last_edited_date, id / aliases/capture_id, etc.) probably are not needed. you can also make a much better and custom ui, or allow for a custom ui. for example timestamp could be a calendar and you can parse it so its easier to read."*
> *"allow me to look at all metadata fields, but"* — the message was truncated mid-sentence; it is read here as **all metadata must stay reachable on demand, while only the important fields show by default**, and every rule below is written to satisfy the strict reading.

This doc **overrides 03 §3's left-pane header list and the whole `ui.display.*` block** where they conflict; the rest of 03 §3 (two 50/50 panes, the four right-pane states, `<CR>` dispatch, keymap table) stands unchanged. The thin-client law (10 §1) and the real-buffer exception (10 §4) bind every rule here: presentation is drawn **around** the capture buffer with extmarks, never into it. Doc 14 governs the vocabulary: **every field name, order, format and marker below is a DEFAULT** — one reasonable value in the config schema — and none is LAW. The shipped `pinned`/`hidden` lists are the capture schema of 02; a vault whose frontmatter keys are `captured_at`/`project`/`people` must get an equally correct pane with zero code changes (14 §1's Two-Users Test, exercised in §7's worked example).

## 1. The fold defect (Matt item 5)

**Root cause, verified on the live install:** neither mount path ever set window-local fold options, so both panes inherited the user's globals. `mount_float` set only `cursorline/wrap/number/signcolumn` through nui's `win_options`; `mount_split` set only `cursorline/number`. With the ordinary markdown setup (`foldmethod=expr` + `nvim_treesitter#foldexpr()`, `foldlevel=0`) **both panes opened folded** — the capture unreadable, and the organize pane's rows (which *are* the choice being made) collapsed.

**The fix (landed in `1686de7`; this section is the law it must keep satisfying).** One table, applied to **both** panes at mount, after the windows exist and before the first `refresh`:

| Option | Value | Why this value |
|---|---|---|
| `foldenable` | `false` | nothing is folded on open |
| `foldmethod` | `"manual"` | ⚠ not merely `foldenable=false`: with the user's `expr` method still live, any later `zx`/`zi`/`foldenable` flip — from the user or another plugin — re-applies the treesitter folds. `manual` means the only folds that can exist are the ones this plugin creates, which is also what makes §3's frontmatter fold safe. |
| `foldlevel` | `99` | a manual fold created later still opens by default |
| `foldcolumn` | `"0"` | no gutter; the panes are narrow |

Each option is set with `pcall(vim.api.nvim_set_option_value, name, value, { win = win })` — an unknown or removed option name must never break `mount`.

**The opt-back-in key** is `ui.win_options`: a free map of window-local option names, deep-merged **over** `PANE_WIN_OPTIONS` and applied to both panes. `ui = { win_options = { foldenable = true } }` restores folding; the same door sets `wrap`, `number`, `signcolumn`, `conceallevel`, `winblend` or anything else window-local. It is a free map (values typed `scalar`) precisely so it needs no schema edit per option.

**Regression tests** (`tests/plugin/ui_spec.lua`) reproduce the real symptom rather than asserting the setter ran: set `foldmethod=expr`, `foldexpr="1"`, `foldenable=true`, `foldlevel=0` globally, mount, then assert in **both** windows that `vim.fn.foldclosed(1) == -1` and `foldmethod == "manual"`, restoring the globals before the assertions so a failure cannot leak them. Mutation audit: deleting the apply call fails exactly those tests.

## 2. Field policy (Matt items 6 and 8)

Every frontmatter key of the current capture falls in exactly one of three buckets, and the bucket decides visibility per mode:

| Bucket | Config | compact (default) | full | raw |
|---|---|---|---|---|
| **PINNED** | `ui.capture.fields.pinned` (ordered list) | value row, in the declared order | value row | — |
| **REST** (everything else present in the frontmatter) | implicit | one collapsed line: `+ N more: key, key, … · zi` (keys, no values) | value row, keys sorted | — |
| **HIDDEN** | `ui.capture.fields.hidden` (list) | not shown, not counted in the `+ N more` line | value row, dimmed, sorted | — |

`raw` draws no field card at all (only the position line) and **opens the frontmatter fold**, so the metadata is the buffer text itself — reachable, and editable in place. That is the strict answer to item 8: nothing is ever unreachable, and the deepest level of "look at all metadata fields" is the file.

**The cycle.** One keymap steps `compact → full → raw → compact`. Default `zi`, config key `keymaps.buffer.cycle_fields`. `zi`'s native meaning (toggle `foldenable`) is the closest vim idiom to what this key does, and it is shadowed only inside the panes; `zo`/`za` still open the frontmatter fold by hand. The lhs is a DEFAULT, not LAW: the setup-time collision gate (`actions.detect_collisions` for core-vs-core, `CORE_KEYMAPS` for metadata-vs-core) is the arbiter, so if another doc's binding scheme claims the same prefix, only this default moves. The row is `navigation = true`, so it survives `ui.capture_pane_keymaps = "navigation"`; under `"none"` it is bound in the organize pane only. Adding it touches the three places a new action always touches: `ui.DEFAULTS.keymaps.buffer` (which is where `config.lua` derives the closed set of rebindable names), `actions.CORE_KEYS`, and — see §8 — the core's `CORE_KEYMAPS`.

**Mode state.** `state.field_mode`, initialised from `ui.capture.mode` at `session.start`, sticky across capture advance for the whole session, never persisted. When the mode is not `compact`, the left border title reads `" Capture — full "` / `" Capture — raw "`; in `split` layout (no border) the mode is the first line of the card instead, and in `raw` a single virt_line `-- raw --` above line 1.

**Schema rules, exactly:**

- `pinned` and `hidden` are **lists of strings and are replaced wholesale, never index-merged.** ⚠ `vim.tbl_deep_extend` merges array-like tables *by index* — `pinned = { "tags" }` over a four-entry default would otherwise yield `{ "tags", "context", "tags", "sources" }`. The config layer must treat both as leaf values. This is a pinned test (§10.4), not a comment.
- A key appearing in **both** `pinned` and `hidden` is a **`ConfigError` naming the dotted key and the field** at `setup()` — it is always a mistake, and 09 §1.5 forbids guessing. There is therefore no precedence rule to remember.
- A **pinned key absent from this capture** renders with the placeholder `—` when `show_empty_pinned = true` (default), so the card keeps a constant shape and the reader's eye lands in the same place on every capture of a 2,300-file backlog. Set `false` to omit the row.
- A **hidden key that never appears** in this vault is a no-op, not an error. `hidden` is a denylist of *possible* keys, and erroring would make configs vault-specific — the opposite of requirement 7.
- **Unknown keys** (in the frontmatter, in neither list) are REST. Sorted lexicographically so the card is deterministic across renders.
- **"Show everything"** has two spellings: `ui.capture.mode = "full"` (start there every session) or `hidden = {}` (nothing is ever suppressed; the `+ N more` line still collapses REST in compact mode).
- **Spec 07 fields are implicitly pinned.** Every `metadata_fields` key served by `meta.fields` is appended to the effective pinned list, after the explicit entries, in core order, unless the user lists it in `hidden`. Without this, 07's "the metadata summary re-renders immediately after each edit so state is visible (e.g. `importance: high ✓`)" would break the moment `importance` fell into REST. Governed by `pin_metadata_fields` (default `true`). A pinned metadata field renders its edit keymap as a dim suffix: `importance   high ✓   (i)`.

**What is NOT configurable here:** which fields *exist* and are *editable*. That is `[[metadata_fields]]` in the core config (07 + 10 §3). This doc configures what a pane *shows*; the core owns what a field *is*.

**`ui.display.*` is deleted as a section** — every key gets a new home, and any surviving `ui.display.*` key raises through `config.MOVED_KEYS` naming its replacement:

| Old key | New home |
|---|---|
| `ui.display.show_position` | `ui.capture.show_position` |
| `ui.display.timestamp_format` | `ui.capture.formatters.timestamp = { name = "datetime", format = … }` (the default is now `calendar`, §4) |
| `ui.display.show_metadata_summary` | `ui.capture.fields.pin_metadata_fields` |
| `ui.display.hide_capture_id` | `ui.capture.fields.hidden` (ships with `capture_id`, `id`, `aliases`) |
| `ui.display.hide_modalities` | `ui.capture.fields.hidden` (ships with `modalities`) |
| `ui.display.hide_location` | `ui.capture.fields.hidden` (ships with `location`) |
| `ui.display.show_scores` | `ui.organize.show_scores` |
| `ui.display.show_reasons` | `ui.organize.show_reasons` |

## 3. Rendering model

The left pane is the real capture file's buffer (10 §4). **Everything this doc draws is extmark virtual text.** The card is a single extmark at `(0,0)` in the `NS_CAPTURE` namespace with `virt_lines = <chunks>, virt_lines_above = true`, replacing the current `draw_capture_header`. No buffer line is ever added, moved or rewritten by the presentation layer.

**The raw frontmatter block is collapsed with a window-local fold, not conceal.** Justification, and why the alternative loses: concealed text still occupies its screen line before Neovim 0.11's `conceal_lines`, so a concealed 12-line frontmatter leaves twelve blank lines between the card and the body — worse than the defect being fixed; conceal also fights the user's own `conceallevel`/`concealcursor` and any markdown-conceal plugin, and it silently hides text that `:w` will still write. A fold costs one readable line, is a native editing affordance (`zo`/`za` open it, and `foldopen` opens it automatically when the cursor enters), and is *already* safe here because §1 pinned `foldmethod = "manual"`.

Mechanism, precisely:

1. **Region detection is a delimiter scan, never a YAML parse.** Buffer line 1 must be exactly `---`; the region ends at the first subsequent line matching `^---$` or `^\.\.\.$`. No match ⇒ no fold. The client parses nothing: values come from `note.get`'s `frontmatter` dict.
2. `vim.api.nvim_win_call(capture_win, function() vim.cmd(("%d,%dfold"):format(1, close_line)) end)`, then window-local `foldenable = true`, `foldlevel = 0`, `foldminlines = 0`, `foldtext = "v:lua.require'para-organize.ui'.foldtext()"`, and `fillchars` gains `fold: ` (all window-local, capture window only).
3. ⚠ This is the **one** place `foldenable` is turned back on. It is safe only because `foldmethod` is `manual`: the sole foldable region in the window is the one the plugin created.
4. Default foldtext: `▸ frontmatter (12 fields) — zi cycles, zo opens`. Field count comes from the `note.get` payload, so an unparseable file cannot produce a lying count (see below).
5. `ui.capture.frontmatter = "fold" | "none"`. `"none"` leaves the YAML visible and creates no fold.

**Failure modes — all specified, none silent:**

| Situation | Behaviour |
|---|---|
| User edits frontmatter in the buffer | The fold region is recomputed on `TextChanged`/`TextChangedI` (debounced 150 ms), `BufReadPost` and `BufWritePost`. Values in the card come from the last `note.get`, so between the edit and `:w` the card appends the dim marker `(edited — :w to refresh)` rather than showing values it can no longer vouch for. `BufWritePost` re-issues `note.get` and re-renders. |
| User opens the fold by hand | Never re-closed for that capture. The plugin re-applies the closed fold only on capture advance / reload. Fighting the user's `zo` is a bug. |
| File has no frontmatter | No fold. Card renders the position line, every pinned key as `—` (when `show_empty_pinned`), and one dim line `(no frontmatter)`. |
| YAML unparseable | `note.get` returns `parse_error: true`, `frontmatter: {}` and the raw text as body (`server.py:1319-1340`). **No fold is created** — hiding the broken YAML would hide the thing that needs fixing. The card renders exactly one loud line, `⚠ frontmatter unparseable — showing raw`, and no field rows. It must not show the previous capture's values (§10.6). |
| Buffer reloaded (`:e!`, `checktime`, Syncthing) | Extmarks and manual folds are both destroyed by a reload; `BufReadPost` re-applies card and fold. |
| User writes the file | Nothing changes on disk beyond the user's own edit — see below. |
| Frontmatter longer than `max_card_lines` | The card truncates with a final `+ N more · zi`; the fold is unaffected. |

**`:w` must never write a rendered artifact into the vault.** The card, the mode hint, the foldtext and the merge/integrate hints are extmarks and foldtext — none is buffer content. The proof obligation is byte-level, not structural: mount → refresh in every mode → `:w` → the file's bytes equal the bytes read before mount, and `nvim_buf_line_count()` equals the on-disk line count (§10.3).

## 4. Formatters

A formatter turns one frontmatter value into display text. `ui.capture.formatters` maps a frontmatter key to a formatter spec; `["*"]` is the fallback for unlisted keys.

**Named formatters:**

| Name | Input → output | Options |
|---|---|---|
| `raw` | `tostring`; lists joined with `, `; maps as `k=v k=v`, keys sorted | — |
| `list` | list → `a, b, c` | `sep` (default `", "`), `max` (default 0 = all; overflow renders `+N`) |
| `tags` | list → `#impro #blog-idea` | `prefix` (default `"#"`) |
| `link` | vault path → `resources/performing/impro` (extension dropped) | `basename` (default `false`) |
| `boolean` | truthy → `✓`, false → `✗`, absent → placeholder | `yes`, `no` |
| `number` | integers without a trailing `.0` | `precision` |
| `datetime` | ISO-ish string → `os.date(format, …)` | `format` (default `"%b %d, %I:%M %p"` — the old `timestamp_format`) |
| `relative` | ISO-ish string → `4 days ago` / `in 2 hours` / `just now` | `precision` (`"day"`\|`"hour"`\|`"minute"`) |
| `calendar` | see below | `date_format`, `time_format` |

**`calendar`, concretely** (Matt: *"timestamp could be a calendar and you can parse it so its easier to read"*). Parse with the existing tolerant matcher (`render.format_timestamp`: ISO-ish `YYYY-MM-DD[T ]HH:MM[:SS]`), then:

| Case | Output |
|---|---|
| same calendar day as now | `Today · 7:45 PM` |
| yesterday / tomorrow | `Yesterday · 7:45 PM` / `Tomorrow · 9:00 AM` |
| within 6 days either side | `Sat · 7:45 PM · 4 days ago` |
| otherwise | `Sat 16 Aug 2026 · 7:45 PM` |
| unparseable | the original string, verbatim — **never dropped** (the core keeps timestamps as opaque strings) |

Defaults: `calendar` for `timestamp`, `created_date`, `last_edited_date`; otherwise the formatter is derived from the core-declared `meta.fields[].type` (`list`→`list`, `boolean`→`boolean`, `number`→`number`, `enum`/`string`→`raw`); otherwise `raw`. `tags` ships with the `tags` formatter.

**Custom formatter contract.** A spec may be a string (`"calendar"`), a table (`{ name = "datetime", format = "%Y-%m-%d" }`), or a Lua function:

```lua
---@param value any        -- the frontmatter value, exactly as the core sent it
---@param ctx table        -- { key, record, frontmatter, mode, width, config }
---@return string|table    -- "text" | { "text", "HlGroup" } | { {"a","Hl"}, {"b"} }
```

It must be pure: no vault access (10 §1), no RPC, no buffer or window mutation, no `vim.cmd`. It runs inside the render path.

**A formatter that errors must never break the pane.** Every call is `pcall`ed. On error or on an invalid return (not a string/chunk list, or any text containing `\n` — Neovim rejects newlines in virtual text): fall back to `raw` for that key, emit **one** `WARN` notification per (key, session) carrying the error message, and record the failure in `:ParaOrganize debug`. Total formatter time per render is budgeted by `ui.capture.render_budget_ms` (default 50, matching 09 §4's "UI action response < 50 ms"); exceeding it logs once per session and never aborts the render.

## 5. Custom UI (the escape hatch)

`ui.capture.render` replaces the built-in card wholesale:

```lua
---@param ctx table
---   record       -- the NoteRecord as the core sent it
---   frontmatter  -- full frontmatter dict from note.get
---   parse_error  -- boolean
---   index, total -- session position
---   mode         -- "compact" | "full" | "raw"
---   fields       -- { pinned = {…}, rest = {…}, hidden = {…} } (resolved key lists)
---   format       -- function(key, value) -> string, the §4 formatter stack
---   width        -- current capture-window width in columns
---   config       -- the merged UI config
---@return table  -- list of: "text" | { "text", "HlGroup" } | { {"a","Hl"}, {"b"} }
```

Safety rules it must obey, each enforced by the plugin rather than trusted:

1. **Return-shape validation.** Anything that is not a string / chunk pair / chunk list is rejected.
2. **No newlines** in any chunk (an extmark with `\n` raises); rejected.
3. **At most `ui.capture.max_card_lines`** entries (default 40); the excess is dropped and a dim `+N more` line appended.
4. **It never becomes buffer text.** The return value is only ever passed to `nvim_buf_set_extmark` as `virt_lines`. There is no code path from this function to `nvim_buf_set_lines` or to disk.
5. **Purity** as in §4 — no RPC, no vault access, no window/buffer mutation. Use `state.on(fn)` to react to session changes instead.

**Fallback on error:** `pcall`; on the first failure the built-in card renders for that frame and one `WARN` fires; after **3** failures in one session the override is disabled for the remainder of the session (built-in card, no further notifications) so a broken user function cannot spam its way through a 200-capture backlog. `:ParaOrganize debug` reports `capture_render = "override-disabled (3 errors)"`.

The same seam exists for the right pane: `ui.organize.render_row` (§6), and — already shipped — a whole alternative front-end can be injected via `actions.setup{ ui = … }`.

## 6. The organize (right) pane

Rows are exactly 03 §3's four states; this doc adds the knobs and the two fixes.

| State | Row | Configurable |
|---|---|---|
| Suggestions | `[P] folder-name  → route: workout  2.40` + indented reason lines | `show_scores`, `show_reasons`, `max_reasons`, `score_thresholds`, `ui.icons.*`, `render_row` |
| Browse | `[D] name` / `[F] alias-or-name`, plus `┊ description` when preview is on | `ui.icons.*`, `render_row` |
| Search | type letter + label | `render_row` |
| Merge / review gate | buffer-editable content; instructions are **virt_lines only** (03 §5, 12 §1) | `ui.highlights.*` |

- `ui.organize.show_scores` / `show_reasons` (defaults `true`) replace `ui.display.*` and are the **single** source: the folder pickers must read the same key. `pickers.format_folder` currently reads an `opts.show_scores` that no call site ever passes, so the picker score column is unreachable — under 03 §1's "every config key is honored or deleted" that is a defect, and this doc closes it.
- `ui.organize.max_reasons` (default `3`, `0` = all) caps the indented reason lines. 04 §2 requires each fired signal to contribute a human-readable reason; six of them push the next suggestion off a short pane.
- `ui.organize.score_thresholds = { high = 2.0, medium = 1.0 }` promotes `render.SCORE_HIGH`/`SCORE_MEDIUM` out of the source. The defaults are 03 §3's literals; a differently-weighted vault (04 §1) needs different buckets.
- `ui.organize.render_row = function(row_ctx) -> nil | "text" | {text, hl}` — per-row override; returning `nil` falls back to the built-in row for *that* row. `row_ctx = { view, index, selected, item, marker, config, width }`. Same five safety rules and the same 3-failure disable as §5. Row *dispatch* stays keyed on `item.kind`, never on rendered text, so a custom row cannot break `<CR>`.

## 7. Config schema (nvim `setup()` side)

```lua
require("para-organize").setup({
  ui = {
    -- §1 — merged OVER ui.PANE_WIN_OPTIONS, applied to both panes at mount.
    win_options = {},                    -- free map: any window-local option

    capture = {
      show_position = true,              -- "Capture 3 of 47"
      mode = "compact",                  -- "compact" | "full" | "raw" (session start)
      frontmatter = "fold",              -- "fold" | "none"           (§3)
      max_card_lines = 40,
      render_budget_ms = 50,
      render = nil,                      -- function(ctx) -> virt-lines  (§5)
      fields = {
        pinned = { "timestamp", "context", "tags", "sources" },  -- ordered; REPLACED wholesale
        hidden = {                                               -- REPLACED wholesale
          "location", "processing_status", "created_date", "last_edited_date",
          "id", "aliases", "capture_id", "modalities",
        },
        pin_metadata_fields = true,      -- spec 07 fields are implicitly pinned
        show_empty_pinned = true,        -- absent pinned key renders "—"
        show_rest_keys = true,           -- "+ 4 more: id, location, …" vs "+ 4 more"
        labels = {},                     -- free map: key -> display label
      },
      formatters = {                     -- key -> "name" | {name=…, opts} | function
        timestamp = "calendar",
        created_date = "calendar",
        last_edited_date = "calendar",
        tags = "tags",
        ["*"] = nil,                     -- nil = derive from the core's meta.fields type
      },
    },

    organize = {
      show_scores = true,
      show_reasons = true,
      max_reasons = 3,                   -- 0 = all
      score_thresholds = { high = 2.0, medium = 1.0 },
      render_row = nil,                  -- function(row_ctx) -> nil|text|{text,hl}  (§6)
    },
  },
  keymaps = { buffer = { cycle_fields = "zi" } },  -- compact -> full -> raw
})
```

Types: `pinned`/`hidden` `string[]`; `labels`/`win_options`/`formatters` free maps; `mode`/`frontmatter` enums; `max_card_lines`/`render_budget_ms`/`max_reasons` numbers ≥ 0; `score_thresholds.*` numbers; `render`/`render_row`/formatter functions typed `function`. Unknown keys under `ui.capture`/`ui.organize` are a `ConfigError` naming the dotted key (closed records); `win_options`, `labels` and `formatters` are the three deliberate free maps.

**Worked example — a user who is not Matt** (different vocabulary, no code changes):

```lua
require("para-organize").setup({
  ui = {
    win_options = { wrap = false },                 -- keep folds off, kill wrapping
    capture = {
      mode = "compact",
      fields = {
        pinned = { "captured_at", "project", "people", "summary" },
        hidden = { "uuid", "device", "geo", "schema_version" },
        labels = { captured_at = "When", people = "With" },
      },
      formatters = {
        captured_at = "calendar",
        people = { name = "list", sep = " · ", max = 3 },
        summary = function(v) return { { tostring(v), "Comment" } } end,
        source_url = "link",
      },
    },
    organize = { max_reasons = 1, score_thresholds = { high = 3.0, medium = 1.5 } },
  },
  keymaps = { buffer = { cycle_fields = "<leader>tf" } },
})
```

Rendered compact card (virt_lines above line 1; the buffer below is the untouched file):

```
 Capture 3 of 47
 When       Today · 7:45 PM
 project    kms-rewrite
 With       ana · dev
 summary    folding both panes was the whole complaint
 + 4 more: device, geo, schema_version, uuid · <leader>tf
▸ frontmatter (9 fields) — <leader>tf cycles, zo opens
# the note body starts here
```

## 8. Core-side additions

Exactly one, and it is a collision-registry entry, not behaviour:

- **`CORE_KEYMAPS["zi"] = "cycle_fields"`** in `organize_core.config`. Per the Phase-2 close ruling *"Spec 07 acceptance test 4 is split"*, the **core** owns metadata-vs-core keymap collision detection. A core that does not know the plugin binds `zi` would accept `[[metadata_fields]] keymap = "zi"` and one of the two bindings would silently lose — the exact failure 07 acceptance test 4 exists to prevent. `A`/`auto_organize` (13 §1) is already in that table for the same reason.

**Everything else is UI-side, deliberately:**

- **No new RPC.** `note.get` already returns the complete `frontmatter` dict plus `parse_error` and `body` (`src/organize_core/server.py:1319-1340`), and `actions.load_current` already merges both onto the capture record. "Show all metadata" is therefore reachable from a thin client today, without a vault read.
- **Field classification (`pinned`/`hidden`/formatters/labels) stays in the nvim `setup()` table.** 10 §3 puts behaviour in the core so "CLI and UI can never disagree" — this is the class of knob they *cannot* disagree about: the CLI has no pane. Which fields *exist*, their types, their edit keymaps and their normalization remain core-side (`[[metadata_fields]]`, 07 + 10 §3); which of them a given frontend *shows first* is that frontend's business, and a second frontend (a TUI, an editor plugin for something else) must be free to choose differently.

## 9. Acceptance criteria

1. With the user's globals at `foldmethod=expr`, `foldexpr="1"`, `foldenable=true`, `foldlevel=0`, `:ParaOrganize start` opens **both** panes with no closed fold on line 1, and both windows report `foldmethod == "manual"`.
2. `ui = { win_options = { foldenable = true } }` restores folding in both panes and still reports `foldmethod == "manual"` — the escape hatch never restores the user's `expr` method.
3. A capture with 12 frontmatter keys shows, in compact mode: the position line, the four pinned rows in declared order, a `+ N more` line whose N counts REST only (hidden keys excluded), and one folded frontmatter line. `zi` → full shows every key including `processing_status`; `zi` → raw shows no card rows and an **open** frontmatter fold.
4. Mount, cycle all three modes, refresh, `:w`: the file's bytes are identical to the pre-mount bytes and the buffer's line count equals the file's line count.
5. A capture whose YAML is malformed renders the loud `⚠ frontmatter unparseable` line, no field rows, no fold, and the body is still readable and editable.
6. A user config with `pinned = { "tags" }` yields exactly one pinned row (list replacement, not index merge); a config listing `tags` in both `pinned` and `hidden` fails `setup()` with an error naming `ui.capture.fields` and `tags`.
7. A formatter that raises, and a `ui.capture.render` that returns text containing `\n`, both leave a complete, correct pane (built-in rendering) and emit one warning each.
8. A vault with a completely different frontmatter vocabulary (the §7 worked example) produces a correct card with zero code changes — 14 §1's Two-Users Test applied to this pane.
9. `[[metadata_fields]] keymap = "zi"` fails core config validation naming both bindings.

## 10. Test obligations (anti-vacuity house standards)

Every obligation names the mutation that must break it; mutation-audit before handback, per the permanent standard.

1. **Fold regression, both panes.** Assert the literal `-1` from `foldclosed(1)` and the literal `"manual"` — never `ui.PANE_WIN_OPTIONS.foldmethod`. Restore globals before asserting. *Mutation:* delete the apply call ⇒ exactly these two tests fail.
2. **Escape-hatch pin.** `win_options = { foldenable = true }` ⇒ `foldenable` true **and** `foldmethod` still `"manual"` (must-not-be-connected: opting into folds must not reconnect the user's `foldexpr`). *Mutation:* apply the user map *under* the defaults instead of over ⇒ fails.
3. **Byte-identity of `:w`.** Read the file into a string before mount; mount, render in all three modes, `:w`; assert the post-write string equals the pre-mount string **and** `nvim_buf_line_count() == #lines_on_disk`. *Mutation:* render the card with `nvim_buf_set_lines` ⇒ both assertions fail. This is the 10 §4 tripwire.
4. **List-replacement tripwire.** `pinned = { "tags" }` over the four-entry default ⇒ the card has exactly one pinned row and it is `tags`. *Mutation:* merge the lists with `vim.tbl_deep_extend` ⇒ four rows, test fails. Assert against the literal `{ "timestamp", "context", "tags", "sources" }` for the default case, never against the imported defaults table.
5. **Refusal predicates, guard-deleted.** (a) both-lists ⇒ `ConfigError` whose message contains the literal `"tags"`; (b) `cycle_fields = ""` ⇒ `maparg("zi", "n")` is empty in both panes; (c) `ui.display.show_scores = false` ⇒ error naming the literal `"ui.organize.show_scores"`. Each test must fail when its guard is deleted.
6. **Unparseable YAML.** Load capture A (valid, `context: alpha`), then capture B (`parse_error = true`): assert B's card contains the literal `"unparseable"`, contains **no** field rows, has no closed fold, and — must-not-be-connected — does **not** contain the literal `"alpha"`.
7. **Formatter isolation.** A formatter for `tags` that `error()`s ⇒ the `tags` row shows the raw value (literal `"impro, blog-idea"`), every other row is present, and the notify spy count is exactly `1` after three refreshes.
8. **Custom-renderer guards.** Returns containing `"\n"`, a non-list, and 100 lines each fall back cleanly; after the 3rd raising call the override is disabled — assert built-in output on the 4th refresh and a notify count of exactly `1`.
9. **Core collision.** `[[metadata_fields]] keymap = "zi"` ⇒ config load raises naming both `cycle_fields` and the field key (mirrors 07 acceptance test 4's both-bindings requirement).
10. **Headless end-to-end** (09 §3 gate): a real fixture vault session — start, cycle to `full`, edit a body line, `:w`, accept a suggestion — asserted against **bytes on disk**, with the destination file's frontmatter byte-identical apart from the tag written by 05 §2.
11. **Perf** (`-m slow`): rendering a 40-key frontmatter card, formatters included, stays under the 09 §4 UI budget of 50 ms.
