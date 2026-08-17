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
| `foldenable` | `false` | nothing the *user's* fold settings would have folded is folded on open. ⚠ The capture window re-enables it, once, for the single frontmatter fold this plugin creates (§3 step 3); that fold is this doc's fix for item 6, not a survival of item 5, and §9/§10 assert the two windows separately for exactly that reason. |
| `foldmethod` | `"manual"` | ⚠ not merely `foldenable=false`: with the user's `expr` method still live, any later `zx`/`zi`/`foldenable` flip — from the user or another plugin — re-applies the treesitter folds. `manual` means the only folds that can exist are the ones this plugin creates, which is also what makes §3's frontmatter fold safe. |
| `foldlevel` | `99` | a manual fold created later still opens by default |
| `foldcolumn` | `"0"` | no gutter; the panes are narrow |

Each option is set with `pcall(vim.api.nvim_set_option_value, name, value, { win = win })` — an unknown or removed option name must never break `mount`.

**The opt-back-in key** is `ui.win_options`: a free map of window-local option names, deep-merged **over** `PANE_WIN_OPTIONS` and applied to both panes. `ui = { win_options = { foldenable = true } }` restores folding; the same door sets `wrap`, `number`, `signcolumn`, `conceallevel`, `winblend` or anything else window-local. It is a free map (values typed `scalar`) precisely so it needs no schema edit per option.

**Regression tests** (`tests/plugin/ui_spec.lua`) reproduce the real symptom rather than asserting the setter ran: set `foldmethod=expr`, `foldexpr="1"`, `foldenable=true`, `foldlevel=0` globally, mount, then assert — **per window, not "in both windows"** — the literals §9 acceptance 1 and §10 obligation 1 spell out: organize gets `foldclosed(1) == -1`; capture gets `foldclosed(1) == 1` with `foldclosed(<first body line>) == -1` under the shipped `frontmatter = "fold"`, and `foldclosed(1) == -1` under the `"none"` control. Both windows report `foldmethod == "manual"` in every case. Restore the globals before the assertions so a failure cannot leak them. A blanket `foldclosed(1) == -1` in both windows is **unsatisfiable** against §3 and must never be written: an implementer discharges it by deleting the frontmatter fold. Mutation audit is in §10.1.

## 2. Field policy (Matt items 6 and 8)

Every frontmatter key of the current capture falls in exactly one of three buckets, and the bucket decides visibility per mode:

| Bucket | Config | compact (default) | full | raw |
|---|---|---|---|---|
| **PINNED** | `ui.capture.fields.pinned` (ordered list) | value row, in the declared order | value row | — |
| **REST** (everything else present in the frontmatter) | implicit | one collapsed line: `+ N more: key, key, … · zi` (keys, no values) | value row, keys sorted | — |
| **HIDDEN** | `ui.capture.fields.hidden` (list) | not shown, not counted in the `+ N more` line | value row, dimmed, sorted | — |

`raw` draws no field card at all (only the position line) and **opens the frontmatter fold** — and re-closes it on the cycle back out of `raw`, which is the plugin re-closing a fold the plugin opened, not the plugin fighting the user's `zo` (§3's edge-case table scopes that rule to a user-opened fold). So the metadata is the buffer text itself — reachable, and editable in place. That is the strict answer to item 8: nothing is ever unreachable, and the deepest level of "look at all metadata fields" is the file.

Two further field-policy knobs belong to the same table and are documented here rather than only in §7's code block, because 14 §4.3(c) requires every key to carry a documented meaning:

| Key | Default | Meaning |
|---|---|---|
| `ui.capture.fields.labels` | `{}` | Free map, frontmatter key → display label. `{ captured_at = "When" }` renders the row as `When   Today · 7:45 PM`. An unlisted key is labelled with the key itself. A label is display-only: it never changes which key is pinned, hidden, sorted or formatted, and `pinned`/`hidden` always name the **frontmatter** key, never the label. |
| `ui.capture.fields.show_rest_keys` | `true` | `true` renders the REST collapse line with its key names — `+ 4 more: importance, project, source_url, status · zi`; `false` renders `+ 4 more · zi`. `false` is for narrow panes, where the key list wraps or truncates and buys nothing. The count is identical either way, and **hidden keys are in neither spelling** (they are not REST). |

**Card layout is a rule, not an accident of §7's example.** Labels are left-aligned and right-padded to the display width (`vim.fn.strdisplaywidth`, so multi-byte and double-width labels align) of the **widest rendered label in the current card**, recomputed per card and capped at 20 columns; a label wider than the cap is not truncated, it simply pushes its own value one column past the cap for that row alone. Values begin one column after the padded label. Nothing is aligned across captures — a card is laid out from the card it is.

**The cycle.** One keymap steps `compact → full → raw → compact`. Default `zi`, config key `keymaps.buffer.cycle_fields`, `panes = {"organize"}`, `navigation = false`. `zi`'s native meaning (toggle `foldenable`) is the closest vim idiom to what this key does, and because the row binds in the **organize pane only**, `z` is not a prefix in the capture pane at all: `zo`/`za` there stay instant and still open the frontmatter fold by hand. (The card's `+ N more · zi` hint and the foldtext name that same binding; it fires from the organize pane, which is where the session's hands already are.) The lhs is a DEFAULT, not LAW: the setup-time collision gate (`actions.detect_collisions` for core-vs-core, `CORE_KEYMAPS` for metadata-vs-core) is the arbiter, so if another doc's binding scheme claims the same prefix, only this default moves. Adding it touches the three places a new action always touches: `ui.DEFAULTS.keymaps.buffer` (which is where `config.lua` derives the closed set of rebindable names), `actions.CORE_KEYS`, and — see §8 — the core's `CORE_KEYMAPS`.

**Mode state.** `state.field_mode`, initialised from `ui.capture.mode` at `session.start`, sticky across capture advance for the whole session, never persisted. When the mode is not `compact`, the left border title reads `" Capture — full "` / `" Capture — raw "`; in `split` layout (no border) the mode is the first line of the card instead, and in `raw` — where the fold is open and there is no card — a single virt_line `-- raw --` above buffer line 1, subject to the same `winrestview` fill as §3's no-frontmatter case, because a virt_line above the topmost line is invisible without it.

**Schema rules, exactly:**

- `pinned` and `hidden` are **lists of strings and are replaced wholesale, never index-merged.** ⚠ `vim.tbl_deep_extend` merges array-like tables *by index* — `pinned = { "tags" }` over the six-entry default would otherwise yield `{ "tags", "summary", "timestamp", "context", "tags", "sources" }`: the user asked for one row, got six, and `tags` appears twice. The config layer must treat both as leaf values. This is a pinned test (§10.4), not a comment.
- A key appearing in **both** `pinned` and `hidden` is a **`ConfigError` naming the dotted key and the field** at `setup()` — it is always a mistake, and 09 §1.5 forbids guessing. There is therefore no precedence rule to remember.
- A **pinned key absent from this capture** renders with the placeholder `—` when `show_empty_pinned = true`; the row is omitted when it is `false`, **which is the shipped default**. The constant-shape argument (the reader's eye lands in the same place on every capture of a 2,300-file backlog) is real but loses here: the shipped `pinned` list has six entries of which two — `title` and `summary` — are absent from most backlog captures, so `true` would print two em-dashes on nearly every card. A stranger's card of real values beats a constant shape padded with nothing. Set `true` to restore constant shape.
- A **hidden key that never appears** in this vault is a no-op, not an error. `hidden` is a denylist of *possible* keys, and erroring would make configs vault-specific — the opposite of requirement 7.
- **Unknown keys** (in the frontmatter, in neither list) are REST. Sorted lexicographically so the card is deterministic across renders.
- **"Show everything"** has two spellings: `ui.capture.mode = "full"` (start there every session) or `hidden = {}` (nothing is ever suppressed; the `+ N more` line still collapses REST in compact mode).
- **Spec 07 fields are implicitly pinned.** Every `metadata_fields` key served by `meta.fields` is appended to the effective pinned list, after the explicit entries, in core order, unless the user lists it in `hidden`. Without this, 07's "the metadata summary re-renders immediately after each edit so state is visible (e.g. `importance: high ✓`)" would break the moment `importance` fell into REST. Governed by `pin_metadata_fields` (default `true`). A pinned metadata field renders its edit keymap as a dim suffix: `importance   high ✓   (i)`.

**What is NOT configurable here:** which fields *exist* and are *editable*. That is `[[metadata_fields]]` in the core config (07 + 10 §3). This doc configures what a pane *shows*; the core owns what a field *is*.

**`ui.display.*` is deleted as a section** — every key gets a new home, and any surviving `ui.display.*` key raises through `config.MOVED_KEYS` naming its replacement. This doc owns that deletion, and the rule that follows from it binds the whole series: **no document may introduce a NEW key under `ui.display.*`.** The section does not exist after this doc lands; a later doc that wants a display knob puts it in the section that owns the surface (`ui.capture.*` or `ui.organize.*`, §7), because `ui.display` is also a *closed record* in the live `config.lua` and a shipped `setup()` block naming it is a `ConfigError` on a fresh install even before the `MOVED_KEYS` gate fires.

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
| `ui.display.show_progress` | `ui.organize.show_progress` — **never shipped**, so it needs no `MOVED_KEYS` migration entry; it is listed here only so this table is the one list every doc in the series reads, and so doc 16 does not re-introduce it under the deleted section. |

## 3. Rendering model

The left pane is the real capture file's buffer (10 §4). **Everything this doc draws is extmark virtual text.** The card is a single extmark in the `NS_CAPTURE` namespace with `virt_lines = <chunks>, virt_lines_above = true`, anchored to the **first buffer line AFTER the frontmatter close delimiter**, replacing the current `draw_capture_header`. It is **NOT anchored to `(0,0)`**: virtual lines attached to a line inside a closed fold are never drawn, because Neovim forces `w_topfill = 0` over a closed fold. With the shipped default `frontmatter = "fold"` a card at `(0,0)` is inside the fold this doc creates on line 1 and renders *nothing at all* — which is exactly why the existing `draw_capture_header` is already invisible on mount, and why every extmark-count assertion in §10 can pass over a pane that shows the user nothing. When the file has no frontmatter, or `frontmatter = "none"`, the card anchors to line 1 and the window fill must be established after every draw — `vim.fn.winrestview({ topline = 1, topfill = #virt_lines })` inside `nvim_win_call` — because `topfill` is 0 after `:edit`, after `nvim_open_win` and after ordinary cursor motion. No buffer line is ever added, moved or rewritten by the presentation layer.

**Highlight groups.** This section replaces `draw_capture_header`, and in doing so it inherits — unchanged — the existing `ui.highlights.hint`, whose meaning stays exactly what it is today: **dim secondary text in the capture card and the integrate hint** (`ui.lua:524`, `integrate.lua:873`). It is neither renamed nor repurposed by this doc or any later one in the series; doc 16's hint-mode (jump-label) highlights are separate keys with distinct stems (16 §5), and `ui.highlights.score_high`/`score_medium`/`score_low` remain highlight-**group-name strings**, unrelated to §6's numeric `ui.organize.score_thresholds`.

**The raw frontmatter block is collapsed with a window-local fold, not conceal.** Justification, and why the alternative loses: concealed text still occupies its screen line before Neovim 0.11's `conceal_lines`, so a concealed 12-line frontmatter leaves twelve blank lines between the card and the body — worse than the defect being fixed; conceal also fights the user's own `conceallevel`/`concealcursor` and any markdown-conceal plugin, and it silently hides text that `:w` will still write. A fold costs one readable line, is a native editing affordance (`zo`/`za` open it, and `foldopen` opens it automatically when the cursor enters), and is *already* safe here because §1 pinned `foldmethod = "manual"`.

Mechanism, precisely:

1. **Region detection is a delimiter scan, never a YAML parse.** Buffer line 1 must be exactly `---`; the region ends at the first subsequent line matching `^---$` or `^\.\.\.$`. No match ⇒ no fold. The client parses nothing: values come from `note.get`'s `frontmatter` dict.
2. `vim.api.nvim_win_call(capture_win, function() vim.cmd(("%d,%dfold"):format(1, close_line)) end)`, then window-local `foldenable = true`, `foldlevel = 0`, `foldminlines = 0`, `foldtext = "v:lua.require'para-organize.ui'.foldtext()"`, and `fillchars` gains `fold: ` (all window-local, capture window only). **The card extmark is then placed on `close_line + 1`** — the first body line — never on line 1, which is now inside a closed fold and would swallow the card whole (see above). If `close_line` is the last line of the buffer, the extmark anchors to `close_line` itself with `virt_lines_above = false`, which is the only case where the card renders *below* its anchor.
3. ⚠ This is the **one** place `foldenable` is turned back on. It is safe only because `foldmethod` is `manual`: the sole foldable region in the window is the one the plugin created.
4. **Foldtext is configurable: `ui.capture.foldtext` (default `nil` = built-in), a format string or a `function(ctx) -> string`** with the same purity rules as §4's formatters. The built-in renders `▸ frontmatter (12 fields) — zi cycles, zo opens`, and it resolves the `{cycle_fields}` slot through `actions.keymap_table()` **at draw time**, so a rebind is reflected verbatim — under a user's `cycle_fields = "<leader>tf"` the line reads `… — <leader>tf cycles, zo opens`, which is what §7's worked example already shows. The foldtext must therefore never contain a hardcoded key literal; 14 §6 and 18 §3 both forbid one, and a hardcoded `zi` here would make the two halves of this document disagree. Field count comes from the `note.get` payload, so an unparseable file cannot produce a lying count (see below).
5. `ui.capture.frontmatter = "fold" | "none"`. `"none"` leaves the YAML visible and creates no fold — and, per the anchoring rule above, moves the card to line 1 with the `winrestview` fill.

**Failure modes — all specified, none silent:**

| Situation | Behaviour |
|---|---|
| User edits frontmatter in the buffer | The fold region is recomputed on **`BufReadPost` and `BufWritePost` only** — ⚠ never on `TextChanged`/`TextChangedI`. Manual folds self-extend as lines are inserted inside them (measured), so the region stays correct without help, and re-closing a fold mid-insert is a hostile edit experience that would contradict the *never re-closed* row directly below. The **card** still re-renders on `TextChanged`/`TextChangedI` (debounced 150 ms); only the fold is left alone. Values in the card come from the last `note.get`, so between the edit and `:w` the card appends the dim marker `(edited — :w to refresh)` rather than showing values it can no longer vouch for. `BufWritePost` re-issues `note.get`, recomputes the fold and re-renders. **Removal primitive, named explicitly:** inside `nvim_win_call`, guarded by `foldclosed(1) ~= -1`, place the cursor on line 1 and use `zd` — **never `zE`**, which eliminates every fold in the window including ones the user built by hand with `zf`. |
| User opens the fold by hand | A **user**-opened fold — `zo`, `za`, or `foldopen` firing as the cursor enters it — is never re-closed for that capture: the plugin re-applies the closed fold only on capture advance / reload. Fighting the user's `zo` is a bug. ⚠ The scoping to *user*-opened is deliberate and load-bearing: the fold that `raw` mode opens is the **plugin's own** (§2), and the `zi` that leaves `raw` re-closes it, because a closed frontmatter block is the whole difference between `raw` and the two modes either side of it. The renderer therefore tracks which of the two opened the fold; an unscoped rule would strand every capture the user ever cycled to `raw` in an open state until the next capture advance. |
| Frontmatter **not fetched yet** | `record.frontmatter == nil` — the async `note.get` reply has not landed, and it has not, because `actions.lua:349-359` calls `refresh()` before the reply arrives. The card renders the position line and one dim `(loading…)`, and **nothing else**: no `—` rows, no `+ N more` line, and above all **no `(no frontmatter)` line**, which at that instant is a statement the client cannot make. Without this row every capture advance flashes a false claim for one frame — 1,862 times over the backlog. `nil` (not fetched) and `{}` (fetched, empty) are different states and the renderer must branch on which. |
| File has no frontmatter | `record.frontmatter == {}` with `parse_error` false. No fold. Card renders the position line, every pinned key as `—` **only when `show_empty_pinned = true`** (it is `false` by default, so the shipped behaviour is position line + `(no frontmatter)` alone), and one dim line `(no frontmatter)`. |
| YAML unparseable | `note.get` returns `parse_error: true`, `frontmatter: {}` and the raw text as body (`server.py:1319-1340`). **No fold is created** — hiding the broken YAML would hide the thing that needs fixing. The card renders exactly one loud line, `⚠ frontmatter unparseable — showing raw`, and no field rows. It must not show the previous capture's values (§10.6). |
| Buffer reloaded (`:e!`, `checktime`, Syncthing) | Extmarks and manual folds are both destroyed by a reload; `BufReadPost` re-applies card and fold. |
| User writes the file | Nothing changes on disk beyond the user's own edit — see below. |
| Frontmatter longer than `max_card_lines` | The card truncates with a final `+ N more · zi`; the fold is unaffected. |
| **Unmount / `:ParaOrganize stop` on a buffer the plugin did not open** | `ui.capture_buffer` reports `owned = false` when the capture file was already loaded in the user's session. Teardown must then leave that buffer exactly as it found it: clear `NS_CAPTURE`, delete the plugin's fold (`zd` on line 1 inside `nvim_win_call`, only if `foldclosed(1)` still reports the plugin's region — a fold the *user* created is not the plugin's to delete), restore the window-local fold options the plugin overwrote, and delete the buffer-local `ParaOrganizeUI` autocmds. A buffer the plugin *did* open is wiped as before. Leaving a closed fold and a live `TextChanged` autocmd on someone's own buffer after `stop` is the same class of defect as Matt item 5, arriving from the other end. |

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

**`calendar`, concretely** (Matt: *"timestamp could be a calendar and you can parse it so its easier to read"*). Parse with the tolerant matcher, **extended by this doc to accept DATE-ONLY input**: `YYYY-MM-DD` with the time component absent parses, with the time defaulting to `00:00`, and renders without the `· 7:45 PM` half. ⚠ This extension is load-bearing, not a nicety: `created_date` and `last_edited_date` are written date-only by the core (`fileops.py:271` `_today`, `fileops.py:1463`), while `render.format_timestamp:162` today *requires* a time component — so the `calendar` formatter that §4 defaults onto those two keys would silently never apply to either of them, and two of the three keys it ships for would render raw. Then:

| Case | Output |
|---|---|
| same calendar day as now | `Today · 7:45 PM` |
| yesterday / tomorrow | `Yesterday · 7:45 PM` / `Tomorrow · 9:00 AM` |
| within 6 days either side | `Sat · 7:45 PM · 4 days ago` |
| otherwise | `Sat 16 Aug 2026 · 7:45 PM` |
| date-only input (`2026-08-16`) | `Today` / `Yesterday` / `Sat · 4 days ago` / `Sat 16 Aug 2026` — the same buckets, with the time half omitted rather than fabricated as `12:00 AM` |
| unparseable | the original string, verbatim — **never dropped** (the core keeps timestamps as opaque strings) |

**Timezone rule.** Bucketing into Today / Yesterday / N-days-ago is a *calendar-day* comparison, so it must happen in one zone. A value carrying a `Z` suffix or a numeric UTC offset is converted to **local** time before bucketing and before formatting; a naive value (no suffix, no offset) is treated as already local and is never shifted. Both halves matter: without the conversion a `…T02:00Z` capture is bucketed as its UTC day and a US user is told "Today" about something they wrote last night; with an unconditional conversion every naive local timestamp in the vault slides by the offset. `date_format` / `time_format` apply after the conversion.

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

`ui.capture.render` replaces the built-in card wholesale — and **only** the card. The seam has a stated ceiling: it returns `virt_lines` and nothing else, so it cannot change pane geometry, add or remove a window, or make the capture pane interactive. The 50/50 two-pane layout is a parity contract that 18 §4.1 teaches against, and 14 §7 puts a whole GUI frontend out of scope for this series; a renderer that could move the panes would break both. For anything past field rendering the seam is `actions.setup{ ui = … }` (14 §5), which replaces the front-end entire — that is the door for a TUI, a different editor, or a different layout, and it is already shipped.

Together with `ui.organize.render_row` (§6), `ui.capture.formatters` (§4) and `ui.capture.fields.*` (§2), these are the **canonical row and renderer seams for this series**; no second spelling of them exists.

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
- `ui.organize.show_progress`, `numeric_accept`, `preview_notes` and `preview_debounce_ms` also live in this record and ship with the defaults in §7. **This doc defines neither their behaviour nor their meaning — doc 16 §3 does**, and cites them under these names. They are declared here only because `ui.organize` is a closed record that this doc lands first; 16 appends nothing to the section, it fills in leaves that already exist.

This doc lands `render.VIEWS[name] = fn` as a **standalone refactor of `lua/para-organize/ui/render.lua`, BEFORE docs 16 and 17 touch the file**: the per-view render functions stop being a dispatch `if`-chain and become a table keyed by view name. Doc 16 then registers `mru` and doc 17 registers `history`, each as one table entry and no edit to the dispatcher. The registry is 14 §5's declared seam and 15 owns the file (R37); the ordering matters because three documents need the same refactor and only one may perform it.

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
      foldtext = nil,                    -- nil = built-in; string | function(ctx) (§3.4)
      max_card_lines = 40,
      render_budget_ms = 50,
      render = nil,                      -- function(ctx) -> virt-lines  (§5)
      fields = {
        pinned = {                                               -- ordered; REPLACED wholesale
          "title", "summary", "timestamp", "context", "tags", "sources",
        },
        hidden = {                                               -- REPLACED wholesale
          "location", "processing_status", "created_date", "last_edited_date",
          "id", "aliases", "capture_id", "modalities", "metadata",
        },
        pin_metadata_fields = true,      -- spec 07 fields are implicitly pinned
        show_empty_pinned = false,       -- true renders an absent pinned key as "—"  (§2)
        show_rest_keys = true,           -- "+ 4 more: importance, source_url, …" vs "+ 4 more"
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
      -- Leaves of this closed record whose BEHAVIOUR and documented meaning are
      -- owned by doc 16 §3.x; declared here only because 15 lands the record first.
      show_progress = true,              -- (16 §3.x owns the behaviour and its documented meaning)
      numeric_accept = true,             -- (16 §3.x owns the behaviour and its documented meaning)
      preview_notes = 5,                 -- (16 §3.x owns the behaviour and its documented meaning)
      preview_debounce_ms = 120,         -- (16 §3.x owns the behaviour and its documented meaning)
    },
  },
  keymaps = { buffer = { cycle_fields = "zi" } },  -- compact -> full -> raw
})
```

Types: `pinned`/`hidden` `string[]`; `labels`/`win_options`/`formatters` free maps; `mode`/`frontmatter` enums; `max_card_lines`/`render_budget_ms`/`max_reasons`/`preview_notes`/`preview_debounce_ms` numbers ≥ 0; `score_thresholds.*` numbers; `show_position`/`pin_metadata_fields`/`show_empty_pinned`/`show_rest_keys`/`show_scores`/`show_reasons`/`show_progress`/`numeric_accept` booleans; `foldtext` `nil | string | function`; `render`/`render_row`/formatter functions typed `function`. Unknown keys under `ui.capture`/`ui.organize` are a `ConfigError` naming the dotted key (closed records); `win_options`, `labels` and `formatters` are the three deliberate free maps.

**Canonical `ui.*` sectioning for the whole series** (R2), so that the two closed records this doc lands are complete before anyone appends to them: `ui.capture.*` and `ui.organize.*` (this doc; 16 fills organize leaves), `ui.hint.*` and `ui.batch.*` (16), `ui.undo.*` (17), `ui.teach.*` (18), plus the pre-existing top-level `ui.layout`, `ui.auto_move_to_new_folder`, `ui.highlights`, `ui.icons`, `ui.float_opts`, `ui.win_options`, `ui.capture_pane_keymaps` and `ui.close_on_complete` — all eight live leaves of `config.lua`'s `SCHEMA`, the first two being flat scalars this enumeration must carry rather than quietly drop (14 §4.2). There is no `ui.display`, and no **new** flat `ui.<scalar>` is added by any doc in the series.

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

Rendered compact card. Note the order, which is the whole of §3's anchoring rule made visible: the **closed frontmatter fold is the top line** (it is buffer lines 1..N), the card's `virt_lines` sit *above the first body line* and therefore *below* the fold line, and the untouched file body follows. Labels are padded to the widest rendered label in this card (`project`/`summary`, 7 columns); values begin one column later. The nine frontmatter keys are four pinned, four hidden and **one** REST — so the collapse line reads `+ 1 more`, not `+ 4 more`: hidden keys are not REST and are never counted there (§2).

```
▸ frontmatter (9 fields) — <leader>tf cycles, zo opens
 Capture 3 of 47
 When    Today · 7:45 PM
 project kms-rewrite
 With    ana · dev
 summary folding both panes was the whole complaint
 + 1 more: source_url · <leader>tf
# the note body starts here
```

With `frontmatter = "none"` the fold line is absent, the YAML is visible as ordinary buffer text, and the card moves above buffer line 1 — the one configuration in which the card is the top thing on screen, and the one that needs the `winrestview` fill of §3.

## 8. Core-side additions

Exactly one, and it is a collision-registry entry, not behaviour:

- **`CORE_KEYMAPS["zi"] = "cycle_fields"`** in `organize_core.config`. Per the Phase-2 close ruling *"Spec 07 acceptance test 4 is split"*, the **core** owns metadata-vs-core keymap collision detection. A core that does not know the plugin binds `zi` would accept `[[metadata_fields]] keymap = "zi"` and one of the two bindings would silently lose — the exact failure 07 acceptance test 4 exists to prevent. `A`/`auto_organize` (13 §1) is already in that table for the same reason.

  ⚠ **This doc does not write that entry itself.** `CORE_KEYMAPS` is written **once**, by doc 14, in one consolidated patch that adds every reservation docs 12/15/16/17/18 need (R9); five documents editing one constant conflict five ways. The `zi` row rides in that patch. While it is being written, note for the next reader that the table's **value vocabulary already disagrees with the Lua action names**: `CORE_KEYMAPS` holds `"S" -> "sort"` where the action is `sort_cycle`, and `"<BS>" -> "back_to_parent"` where the action is `back` (cited by key rather than by line: `config.py` is under concurrent edit and both rows have already moved once). Doc 14 corrects both to the Lua names in that same patch, and 14 §10.7's drift gate then pins the two sets equal. It is recorded here so nobody "fixes" the mismatch a second, different way from this doc's seat.

**Everything else is UI-side, deliberately:**

- **No new RPC.** `note.get` already returns the complete `frontmatter` dict plus `parse_error` and `body` (`src/organize_core/server.py:1319-1340`), and `actions.load_current` already merges both onto the capture record. "Show all metadata" is therefore reachable from a thin client today, without a vault read.
- **Field classification (`pinned`/`hidden`/formatters/labels) stays in the nvim `setup()` table.** 10 §3 puts behaviour in the core so "CLI and UI can never disagree" — this is the class of knob they *cannot* disagree about: the CLI has no pane. Which fields *exist*, their types, their edit keymaps and their normalization remain core-side (`[[metadata_fields]]`, 07 + 10 §3); which of them a given frontend *shows first* is that frontend's business, and a second frontend (a TUI, an editor plugin for something else) must be free to choose differently.

## 9. Acceptance criteria

1. With the user's globals at `foldmethod=expr`, `foldexpr="1"`, `foldenable=true`, `foldlevel=0`, `:ParaOrganize start` opens both panes, and the fold assertions are made **per window** — the two windows are not in the same state and never were:
   - **organize window:** `foldenable == false`, `foldmethod == "manual"`, `foldclosed(1) == -1`.
   - **capture window, shipped default `frontmatter = "fold"`:** `foldmethod == "manual"`, and the *only* closed fold in the window is the plugin's — `foldclosed(1) == 1` **and** `foldclosed(<first body line>) == -1`.
   - **capture window, control case `frontmatter = "none"`:** `foldclosed(1) == -1`.

   ⚠ The plugin-created frontmatter fold is **not the defect and must never be asserted away.** Matt item 5 is the user's `expr` folds collapsing the panes; §3's one-line frontmatter fold is this doc's fix for item 6. An assertion of `foldclosed(1) == -1` in the capture window under the shipped default is unsatisfiable, and an implementer discharges it by deleting the frontmatter fold — which reopens item 5's sibling and empties §2.
2. `ui = { win_options = { foldenable = true } }` restores folding in the organize pane and still reports `foldmethod == "manual"` in both — the escape hatch never restores the user's `expr` method. (The capture pane already has `foldenable = true` by §3 step 2; the knob changes nothing there, which is itself asserted.)
3. A capture with 12 frontmatter keys, all six shipped pinned keys present, shows in compact mode: the position line, the **six** pinned rows in declared order (`title`, `summary`, `timestamp`, `context`, `tags`, `sources`), a `+ N more` line whose N counts REST only (hidden keys excluded), and one closed frontmatter fold whose line sits **above** the card. Control: the same capture with `title` and `summary` absent shows **four** pinned rows under the shipped `show_empty_pinned = false`, and six rows — two of them `—` — when it is set `true`. `zi` → full shows every key including `processing_status`; `zi` → raw shows no card rows and an **open** frontmatter fold (`foldclosed(1) == -1`).
4. Mount, cycle all three modes, refresh, `:w`: the file's bytes are identical to the pre-mount bytes and the buffer's line count equals the file's line count.
5. A capture whose YAML is malformed renders the loud `⚠ frontmatter unparseable` line, no field rows, no fold, and the body is still readable and editable.
6. A user config with `pinned = { "tags" }` yields exactly one pinned row (list replacement, not index merge); a config listing `tags` in both `pinned` and `hidden` fails `setup()` with an error naming `ui.capture.fields` and `tags`.
7. A formatter that raises, and a `ui.capture.render` that returns text containing `\n`, both leave a complete, correct pane (built-in rendering) and emit one warning each.
8. A vault with a completely different frontmatter vocabulary (the §7 worked example) produces a correct card with zero code changes — 14 §1's Two-Users Test applied to this pane.
9. `[[metadata_fields]] keymap = "zi"` fails core config validation naming both bindings.

## 10. Test obligations (anti-vacuity house standards)

Every obligation names the mutation that must break it; mutation-audit before handback, per the permanent standard.

1. **Fold regression, per window.** Three cases, each asserting literals — never `ui.PANE_WIN_OPTIONS.foldmethod`. Restore the user's globals before asserting so a failure cannot leak them. (a) Organize window: `foldenable == false`, `foldmethod == "manual"`, `foldclosed(1) == -1`. (b) Capture window with the shipped `frontmatter = "fold"`: `foldmethod == "manual"`, `foldclosed(1) == 1`, `foldclosed(<first body line>) == -1` — the plugin's fold exists and is the *only* closed one. (c) Capture window with `frontmatter = "none"`: `foldclosed(1) == -1`. *Mutations, three now that the claim is three claims:* delete the `PANE_WIN_OPTIONS` apply call ⇒ (a) and the `"manual"` half of (b) fail; delete the §3 fold creation ⇒ (b)'s `foldclosed(1) == 1` fails; widen the fold to the whole buffer ⇒ (b)'s first-body-line assertion fails. Deleting the apply call is no longer the only way to turn this obligation red, and no mutation may turn (b) red by way of removing the frontmatter fold and calling it a fix.
2. **Escape-hatch pin.** `win_options = { foldenable = true }` ⇒ `foldenable` true **and** `foldmethod` still `"manual"` (must-not-be-connected: opting into folds must not reconnect the user's `foldexpr`). *Mutation:* apply the user map *under* the defaults instead of over ⇒ fails.
3. **Byte-identity of `:w`.** Read the file into a string before mount; mount, render in all three modes, `:w`; assert the post-write string equals the pre-mount string **and** `nvim_buf_line_count() == #lines_on_disk`. *Mutation:* render the card with `nvim_buf_set_lines` ⇒ both assertions fail. This is the 10 §4 tripwire.
4. **List-replacement tripwire.** `pinned = { "tags" }` over the six-entry default ⇒ the card has exactly one pinned row and it is `tags`. *Mutation:* merge the lists with `vim.tbl_deep_extend` ⇒ six rows, test fails. Assert against the literal `{ "title", "summary", "timestamp", "context", "tags", "sources" }` for the default case — in that order — and against the literal `hidden` list including `"metadata"`, never against the imported defaults table. A second literal pin: `show_empty_pinned` defaults to `false`.
5. **Refusal predicates, guard-deleted, each with a firing control.** (a) both-lists ⇒ `ConfigError` whose message contains the literal `"tags"`; (b) `cycle_fields = ""` ⇒ `maparg("zi", "n")` is empty in the organize pane (and `zi` was never bound in the capture pane); (c) `ui.display.show_scores = false` ⇒ error naming the literal `"ui.organize.show_scores"`; (d) any key under `ui.display.*`, including one no doc ever shipped, ⇒ `ConfigError` — the section does not exist. Each test must fail when its guard is deleted, **and each must be paired with a firing control at an adjacent parameter where the operation must succeed**: (a) the same key in `pinned` only loads clean; (b) `cycle_fields = "<leader>tf"` binds `<leader>tf` and leaves `zi` free; (c) `ui.organize.show_scores = false` loads clean and suppresses the score column; (d) `ui.capture.show_position = false` loads clean. A guard that refuses *everything* passes a guard-deleted test, which is precisely the blindness the firing-control half exists to catch (ARCHITECTURE.md:1516-1518).
6. **Unparseable YAML.** Load capture A (valid, `context: alpha`), then capture B (`parse_error = true`): assert B's card contains the literal `"unparseable"`, contains **no** field rows, has no closed fold, and — must-not-be-connected — does **not** contain the literal `"alpha"`.
7. **Formatter isolation.** A formatter for `tags` that `error()`s ⇒ the `tags` row shows the raw value (literal `"impro, blog-idea"`), every other row is present, and the notify spy count is exactly `1` after three refreshes.
8. **Custom-renderer guards.** Returns containing `"\n"`, a non-list, and 100 lines each fall back cleanly; after the 3rd raising call the override is disabled — assert built-in output on the 4th refresh and a notify count of exactly `1`.
9. **Core collision.** `[[metadata_fields]] keymap = "zi"` ⇒ config load raises naming both `cycle_fields` and the field key (mirrors 07 acceptance test 4's both-bindings requirement).
10. **Headless end-to-end** (09 §3 gate): a real fixture vault session — start, cycle to `full`, edit a body line, `:w`, accept a suggestion — asserted against **bytes on disk**, with the destination file's frontmatter byte-identical apart from the tag written by 05 §2.
11. **Perf** (`-m slow`): rendering a 40-key frontmatter card, formatters included, stays under the 09 §4 UI budget of 50 ms.
12. **Screen-level card assertion — the card is ON SCREEN, not merely in the extmark table.** With the shipped default `frontmatter = "fold"`, mount a fixture capture and assert the literal `"Capture 3 of 47"` is *visible*, via `vim.fn.screenstring` / `vim.fn.screenpos` over the capture window — never via `nvim_buf_get_extmarks`. Repeat with `frontmatter = "none"` (the `winrestview` path). *Mutation:* anchor the card to `(0,0)` ⇒ **this test fails and every extmark-count test in this section still passes.** That asymmetry is the entire reason the obligation exists: the pane the user sees and the pane the extmark table describes were not the same pane, and only one of them was tested.
13. **Teardown of a buffer the plugin did not open.** Open the capture file yourself, create a fold of your own with `zf` over a **body** region (never line 1), `:ParaOrganize start`, `:ParaOrganize stop`. Assert on that buffer: `foldclosed(1) == -1` (the plugin's frontmatter fold is gone), **zero** extmarks in `NS_CAPTURE`, **zero** `ParaOrganizeUI` autocmds (`nvim_get_autocmds{ group = …, buffer = … }` returns an empty list), the window's `foldmethod` back to its pre-mount value — and, must-not-be-connected, **the user's own `zf` fold still exists** (`foldclosed(<body line>)` reports it). *Mutation:* tear down with `zE` instead of a guarded `zd` ⇒ the last assertion fails; skip the autocmd deletion ⇒ the third fails; skip the `NS_CAPTURE` clear ⇒ the second fails.
14. **Calendar bucketing is local-time and date-only-tolerant.** With the fixture clock at 2026-08-16 09:00 in a **UTC−5** zone: `2026-08-16T02:00Z` renders `Yesterday · 9:00 PM` (it is 2026-08-15 locally) — assert that literal, not "contains Yesterday"; a naive `2026-08-16 08:00` renders `Today · 8:00 AM` and is **not** shifted by five hours; the date-only `2026-08-15` renders `Yesterday` with **no** time half and no fabricated `12:00 AM`. *Mutations:* bucket in UTC ⇒ the first fails; convert naive values as if UTC ⇒ the second fails; require a time component in the matcher ⇒ the third fails and, with it, `created_date`/`last_edited_date` fall back to `raw`.
15. **Honored-key gate for this document's keys** (14 §10.11). Every key this doc introduces is exercised twice — default ⇒ behaviour A, non-default ⇒ behaviour B, with `B ≠ A` asserted — by the parametrised Lua honored-key gate, and every leaf in `config.lua`'s SCHEMA for these sections is proven to have a reader by the Lua mirror gate. Both gates are built by doc 14 **before any key here ships**. The enumeration, so a reviewer can count rows against tests: `ui.win_options`; `ui.capture.` — `show_position`, `mode`, `frontmatter`, `foldtext`, `max_card_lines`, `render_budget_ms`, `render`, `formatters`; `ui.capture.fields.` — `pinned`, `hidden`, `pin_metadata_fields`, `show_empty_pinned`, `show_rest_keys`, `labels`; `ui.organize.` — `show_scores`, `show_reasons`, `max_reasons`, `score_thresholds`, `render_row` (the four leaves owned by 16 are counted in 16 §8, not here); `keymaps.buffer.cycle_fields`. That is **21 rows** owned by this document. *Mutation audit for the gate itself:* delete the reader for any one leaf ⇒ the mirror gate turns red naming that dotted key; make any one reader ignore its value (hardcode the default) ⇒ the honored-key gate turns red naming that dotted key.
