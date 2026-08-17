# 16 — Fast Navigation and High-Throughput Organizing (NEW)

Directive from Matt (2026-08-16): *"what are the shortcuts for using this program for the para organize program? for example, in the sorting index, i would like to have it so that I can VERY quickly navitage to the exact folder (using something like prefaces, like for example with tmux thumbs, it allows me to use prefixes to select or go to folders)"* and *"think about other things id want to do"*.

Sizing fact, measured on the live install the evening of cutover (`doc/FEEDBACK-EVIDENCE-2026-08-16.md` §4): the index holds 13,252 notes and a **1,862-capture backlog**. Every keystroke this document removes from the per-capture loop is worth ~1,862 keystrokes. Every ambiguous keystroke it *adds* is worth ~1,862 hesitations. Both directions are specified against that number, not against a demo vault.

This doc adds bindings and views. It changes no file-operation semantics: doc 05's invariants and doc 10 §1's thin-client law bind everything here, and every operation below is an ordinary doc-05 operation reached by a shorter path.

## 1. Hint jump — labels on rows (`f`)

`f` (config `keymaps.buffer.hint_row`) enters **hint mode** over the right pane. Every selectable row in the current view — suggestion, root, dir, or file, whatever `ui/render.lua` put in its `map` — is overlaid with a short label; one or two keys then take that row.

### Label generation

| Rule | Specification |
|---|---|
| Alphabet | `ui.hint.alphabet`, default `"asdfghjkl;qwertyuiopzxcvbnm"` (**27 keys**) — home row left-to-right first, then top row, then bottom row. Labels are assigned from the front, so the common case is a home-row key. |
| Ordering | Labels are assigned in **render order**, never in score order. A label is a function of a row's position in the pane, so re-entering hint mode on an unchanged list yields identical labels and the muscle memory holds. |
| One char first | With `n ≤ #alphabet` candidates, every candidate gets a one-char label. |
| Widening | With `n > #alphabet`, reserve the **smallest** `k` keys from the alphabet's tail as two-char prefixes such that `(#alphabet − k) + k·#alphabet ≥ n`. Smallest `k` maximises the count of rows still reachable in one keystroke, and because assignment is in render order those rows are the top-ranked suggestions. Worked example with the 27-key default and `n = 40`: `27 + 26k ≥ 40` ⇒ `k = 1`, so 26 rows keep one-char labels and 14 take two-char ones. Beyond `#alphabet²` (729) candidates the same rule recurses to three chars. |
| Prefix-free | A key is either a terminal label or a prefix, **never both**. There is therefore no ambiguity and **no timeout** — the classic easymotion "wait to see whether a second key is coming" stall is designed out rather than tuned. |
| Collisions | Impossible by construction, not detected at runtime. A candidate set larger than the tri-graph capacity of a user-shortened alphabet is a loud `ConfigError` at setup naming `ui.hint.alphabet` (minimum 4 keys, all distinct). |

### Drawing

Labels are extmark `virt_text` with `virt_text_pos = "overlay"` at column 0 of each candidate row, in a **dedicated namespace `NS_HINT`** owned by a new `ui/hint.lua`. ⚠ This is a new drawing path: `ui.apply_marks` understands only `{line, col, end_col, hl}` and cannot emit virtual text, and reusing `NS_ORGANIZE` would make clearing hints also clear the selection highlight. `NS_HINT` is cleared and rebuilt on every keystroke of the loop and is empty at every other moment.

Overlay at column 0 covers the three-column type marker (`[P]`, `[D]`) — redundant while hinting and exactly wide enough for a two-char label plus a space, so **no row's text moves**. `ui.hint.position = "overlay" | "eol"` for users who want the marker kept. Labels are **never buffer text**: the organize pane is a scratch buffer, but the law in ARCHITECTURE "Structural safety decisions" is uniform and the test is cheap (§8, obligation 3).

Non-candidate lines are dimmed with `ui.highlights.hint_dim` while the loop runs, so the eye goes to the labels.

### The interaction loop

1. `f` draws labels and enters a `vim.fn.getcharstr()` read loop. Hint mode is **modal**, not a set of keymaps — so a label letter that is also a bound action (`s`, `a`, `m`) is unambiguous inside the loop and unaffected outside it.
2. **A lowercase terminal label selects and acts** — identical to moving the cursor to that row and pressing `<CR>` (accept the suggestion / descend the folder / start the merge, per 03 §3's dispatch on `item.kind`).
3. **The uppercase form of the same label jumps only** — moves `state.selected` to that row, redraws, and stays in the view. Matt gets both behaviours from one label with zero extra keystrokes. This works because `ui.hint.case_sensitive = false` (default) leaves the shifted keyspace free; with `case_sensitive = true` the shifted forms become distinct labels and the modifier is gone, so `ui.hint.default_action = "select" | "jump"` then decides what every press does. The trade-off is stated in the config comment, not hidden.
4. A prefix key narrows: labels not under that prefix are erased, the survivors redraw showing their **second character only**, and the loop reads again.
5. `<Esc>` / `<C-c>` aborts — clear `NS_HINT`, no state change, no notification.
6. A key that is neither label nor prefix **aborts with a one-line notify** (`ui.hint.unknown_key = "abort" | "ignore"`, default `abort`). It is never replayed as a motion: replaying into a pane that can be switched to an editable buffer is how a hint implementation types characters into a vault file.
7. `ui.hint.persist = false` (default) erases labels on exit; `true` keeps them drawn after a jump-only press so the next jump costs one key.

## 2. Hint jump over the whole vault (`F`)

`f` labels what is on screen. Matt asked to reach *"the exact folder"*, which is usually not on screen. `F` (`keymaps.buffer.hint_vault`) opens **vault hint**: the candidate set is every folder in the vault, filtered as he types, labelled as soon as the set is small.

- **Filter-then-label, never both at once.** While the candidate set is larger than `#alphabet`, every printable key appends to a fuzzy filter string and the list re-renders; **labels appear only once the set fits one-char labels**. There is therefore never a keystroke that could be read as either a filter character or a label. `<BS>` deletes a filter character, `<CR>` takes the top candidate, `<Esc>` aborts, `<C-n>`/`<C-p>` move without labels.
- **Candidate source.** ⚠ `folder.list` today returns only the *immediate* subfolders of each PARA root. It gains an additive `depth` param (`1` default — today's exact behaviour — or an integer or `"all"`). Deliberately a **parameter, not a new method**, so `EXTRA_METHODS` in `tests/test_server_protocol.py` stays frozen and no new architect ruling on the RPC name list is required.
- **Fetched once, invalidated properly.** The vault folder list is fetched asynchronously at `session.start` (before the user can press `F`, so the press is served from cache) into `state.folders`, and invalidated by `folder.create` and `index.reindex`. This also fixes the existing defect that `state.roots` is cached for a whole session with no invalidation hook, so a folder created with `<leader>np` is invisible to the browse tree until a new session.
- `ui.hint.vault_scope = "folders" | "folders+files"`, default `"folders"` — Matt asked for folders; notes are what merge and the pickers are for.

**Relationship to telescope (03 §4) — the rule, so neither surface is reimplemented badly.** Telescope owns *multi-criteria, preview-heavy, note-level* selection: the nine saved searches, merge-target selection, `open_folder_notes_picker`. Hint mode owns *single-destination selection where the answer is on screen or one word away*. `f`/`F` never open a floating picker and never leave the two-pane layout; that is their entire advantage. **`F` does not implement fuzzy matching**: it uses telescope's own sorter (`telescope.sorters.get_fzy_sorter()`) when telescope is present and a documented substring-then-subsequence fallback when `pickers.telescope_enabled` is false, so ranking never disagrees between the picker and the hint list. A third bespoke matcher is forbidden — it would train two different muscle memories for the same query.

## 3. Throughput features

Matt: *"think about other things id want to do"*. Each row below is specified with its interaction, its edge cases, whether it needs wire, and which side of the doc 10 §3 config split it lives on.

### 3.1 Repeat last destination — `.` (`keymaps.buffer.repeat_destination`)

Sends the current capture where the previous one went. Repeats the **(destination, mode) pair** of the last *applied* destination-taking operation.

- Excluded: `archive` (it has `a`) and `skip`. `.` is destination-only.
- If the previous operation was `merge` or `integrate`, `.` repeats the destination but **re-enters the review gate** (12 §1 `review = "diff"`). `.` never applies an LLM edit unattended; that ladder belongs to doc 13 and is not duplicated here.
- Edge cases: no previous destination in this session ⇒ one-line notify and **no state change** (a pinned refusal predicate, §8); destination no longer exists ⇒ the core's ordinary `result.ok = false` path, never an implicit `folder.create`.
- Wire: none new. **Client**, except that the first `.` of a fresh session is seeded from the core MRU (§3.5) so it repeats yesterday's last filing rather than refusing.

### 3.2 Sticky pinned destination — `P` (`keymaps.buffer.pin_destination`)

`P` pins the selected destination. While pinned, accept sends the capture to the pin regardless of what is selected, then auto-advances. `P` again unpins.

- **Three simultaneous indicators, because a silently-pinned destination is the most dangerous feature in this document** — it misfiles N captures in a row before anyone notices. (i) the organize pane border title becomes `" Organize — PINNED: areas/health "`; (ii) a `virt_lines` banner above line 1 in `ui.highlights.pinned`; (iii) the pinned row is marked in the list. All three, always; none is configurable off.
- The pin clears when the session ends and **does not survive a restart**. A pin restored from yesterday is precisely the misfile risk the indicators exist to prevent.
- A pin whose destination is a *file* is refused unless the route mode is `append` or `integrate`; a pinned `integrate` is allowed only with `review = "diff"` and never with `review = "auto"`.
- **Learning integrity.** A pinned accept is Matt-decided, so it folds into learning (04 §5) — but he did not pick from the shown list, so `chosen_rank` is recorded as `null`, not `1`. ⚠ `ActionContext` gains a first-class `pinned: bool`, **not** a `filters["pinned"]` entry — the precedent is explicit in `actions.ActionContext`'s own docstring, where `dry_run` and `partial_failure` were both promoted out of `filters` after smuggling taught `learning.json` a destination association with `count: 3, success_rate: 1.0`.
- Wire: none new beyond the `pinned` context field. **Client** orchestration, **core** recording.

### 3.3 Batch across captures — `x`, `<leader>ba`, `<leader>bc`

`x` (`keymaps.buffer.mark_capture`) toggles the current capture into a batch. With a non-empty batch, the next accept applies the chosen destination to **every marked capture**. `<leader>ba` marks all captures matching the current session filters; `<leader>bc` clears the batch.

- *Why `x` and not `<Space>`*: `<Space>` is a very common `mapleader`, and claiming it would collide with every `<leader>…` sequence in the table for a large fraction of users. *Why not `v`*: `v` is the letter doc 07 uses in its boolean-field example, and `doc/FEEDBACK-EVIDENCE-2026-08-16.md` §5 forbids a new default silently claiming a letter a user's `metadata_fields` might want.
- Confirmation (`ui.batch_confirm = true`) shows the literal count and destination before anything is written. `ui.batch_max = 100` refuses a larger batch with a message telling the user to narrow filters first — `<leader>ba` over an unfiltered 1,862-capture session is not a feature.
- **Applied sequentially through the ordinary per-capture path**, never through a new bulk primitive: every doc-05 invariant, every ActionRecord, every learning fold is byte-identical to doing it by hand. Batch mode is a keystroke saving, not a second write path.
- Partial failure stops at the first failure, reports `applied k of n`, and **leaves the remainder marked** so retry is one keypress. Per the existing ruling, partially-applied records keep `partial_failure` and are excluded from learning and from the accept-rate corpus.
- ⚠ `ActionContext` gains `batch_size: int | null` for the same first-class reason as `pinned`.
- **Undo seam (17).** Because a batch is N ordinary operations, it is N ordinary `ActionRecord`s and N ordinary oplog lines, so it is **N undo units** with no new reversal machinery. `batch_size` and a shared `session_id` are what let doc 17 decide whether one `u` press should walk the whole batch; this document takes no position beyond guaranteeing the records make that decidable.
- Wire: none new (n × `op.move`). **Client**; requests are issued through the existing async RPC path with a progress render — the editor is never blocked.

### 3.4 Direct numeric accept — `1`–`9` (`ui.numeric_accept`, default `true`)

Press `3` to take the third suggestion. In browse and search views the digits address the Nth visible row, consistently with hint labels.

- Digits bind in the **organize pane only, never the capture pane** — a count prefix is meaningful in a real file buffer and meaningless in a `nofile` scratch pane where `j`/`k` are plain cursor movement. `0` is never claimed.
- `N` greater than the row count ⇒ notify, no-op (pinned refusal predicate).
- Discoverability: when the flag is on, suggestion rows render their index (`1. [P] areas/health`) — an invisible binding is not a feature.
- Not rebindable, and deliberately not nine rows in `keymaps.buffer`: there is no sane way to rebind "the digits", so the honest surface is one boolean. `actions.keymap_table()` emits **one synthetic help row** (`1-9 / accept Nth suggestion / organize`) when the flag is on and zero rows when off, so the generated `?` overlay (03 §2: help "must be generated from the actual keymap table") stays truthful.
- The redundancy with hint labels is deliberate and recorded rather than denied: digits address *ranked* suggestions where rank is stable and meaningful; hints address *arbitrary* rows including deep browse listings.

### 3.5 Recent destinations — `<leader>d` (`throughput.mru_size`, `throughput.mru_order`)

`<leader>d` renders the recent-destinations list **in the right pane** (not a floating picker — the layout is the point) with hint labels already applied, so a recent destination costs `<leader>d` plus one key.

It is a right-pane view, and it registers through the `render.VIEWS[name] = fn` dispatch table that doc 17 requires for its history view — **not** as another hardcoded branch in `render.right_pane`. 03 §3's four states are already five with 12 §1's review gate; adding two more by if/elseif is how that function becomes unreadable.

- ⚠ Requires one new read-only RPC, **`dest.recent`**, and therefore an explicit architect ruling to enter `EXTRA_METHODS`. The justification has the same shape as the existing entries: spec 10 §1 forbids a thin client from reading core state, and `learn.get_top_destinations` / `ActionLog.query` exist in the core with **no wire surface**, so the feature is unreachable from any thin client. Returns the last `n` **distinct** destinations, most-recent-first, each `{path, last_used, count}`.
- CLI sibling `organize dest recent --json` ships with it, so the feature is testable without Neovim (10 §2).
- **Core** config, because the core computes it and the CLI must agree with the UI: `throughput.mru_size` (default `10`), `throughput.mru_order = "recent" | "frequent"` (default `"recent"`; frequency is `learn.get_top_destinations`, recency is the action corpus).
- Empty corpus ⇒ an empty list and a one-line "no recent destinations" render, never an error.

### 3.6 Filter the session queue — `<leader>f` (`keymaps.buffer.filter_session`)

A session over all 1,862 captures is unfocused. `<leader>f` prompts for doc 03 §2 filters (`tags= sources= modalities= since= until_date= status= text=`) and restarts the session with them, carrying the processed/skipped counts forward.

- **The new session is installed only after the core returns a non-empty list.** Zero matches ⇒ notify and keep the current session intact. This is 03 §2's "empty session is active-with-empty-list" resolution read for a live swap: an empty result is not an error, but it is also not a reason to throw away the queue Matt was working.
- The nine saved searches (03 §4) are offered as presets. No second filter vocabulary is invented.
- Wire: none new (`session.start`). **Client**.

### 3.7 Progress and resume — `ui.display.show_progress`, `session.order`

**Progress.** For a 1,862-item backlog the motivating number is the burn-down, not the index. The organize pane header carries a right-aligned virtual-text line: `142 done · 1,720 left · 6.2/min · ~4h37m`. Rendered as virtual text on the header row, so it costs no capture-header line and no buffer text. Default on; `ui.display.show_progress = false` turns it off.

**Resume.** Quitting mid-backlog and returning must not restart at capture 1.

- Resume is **derived, not persisted**. Organized captures leave the `status=raw` default filter by themselves, so a fresh session already excludes them. The real defect is *skipped* captures: they stay `raw` and return to the front of the queue every session, forever.
- ⚠ `session.start` gains an additive `order` param — `"oldest" | "newest" | "unseen_first"`, default **`"unseen_first"`** — where "seen" means the capture has an `op.skip` ActionRecord. Never-shown captures sort before previously-skipped ones, oldest-first within each group. This is satisfied entirely from the existing corpus: no new state file, no new method, and it is exactly what `op.skip` was approved for ("the counterfactual silently lost every time a user pressed `s`").
- Missing or empty corpus ⇒ silent fallback to `"oldest"`. An unrecognised `order` value is a loud `-32602`.
- **Core** config default (`session.order`) with a per-invocation override (`--order` on the CLI, `order` on the RPC), because `organize session` and the UI must produce the same queue.
- Rejected alternative, recorded: persisting a session cursor to a state file. It duplicates a truth the vault and the corpus already hold, and a Syncthing sync or an out-of-band edit (02: "any file can change under you mid-operation") leaves the cursor pointing at a note that has moved.

### 3.8 Destination preview — `p` (existing `toggle_preview`)

03 §3 flags `toggle_preview` as a binding that "must actually be bound, or delete the option". This is what it is for. With preview on, the **selected** destination expands inline (virtual lines) to show: its doc 11 §3 description, its note count, and the `throughput.preview_notes` (default `5`) most-recently-modified notes inside it — *what already lives there, before committing*.

- Data: `folder.children` (exists) plus the description already carried by `folder.list`. ⚠ `_folder_entry` gains a `note_count` field — additive, read-only, no new method.
- **Lazily fetched for the selected row only**, debounced `ui.preview_debounce_ms = 120`, cached per session. A preview per cursor move is a `folder.children` per cursor move; the keypress is never blocked on it, and a slow reply renders when it lands or not at all.

### 3.9 Keystroke accounting — `context.keystrokes`

⚠ `ActionContext` gains `keystrokes: int | null` — the count of keys the client consumed between showing this capture and applying the action, alongside the existing `durations_ms.decision`. A document whose entire premise is keystroke cost must be falsifiable: this is the field that lets `organize actions stats` answer whether hint mode and pinning actually shortened the loop, per capture and per destination. Client-supplied, core-recorded, excluded from learning (it is measurement, not a precedent).

## 4. Considered and rejected

| Rejected | Reason |
|---|---|
| Vim-style counts (`3<CR>`) instead of bare digits | Digits are already claimed by §3.4 and a count prefix has no other meaning in a `nofile` pane; the count form is strictly more keystrokes for the same result. |
| Auto-accept above a confidence threshold during an interactive session | That is doc 13's trust ladder. Two independent auto-apply paths with different gates is exactly the class of divergence doc 10 §3 exists to prevent. Interactive sessions never auto-apply. |
| Transactional rollback of a whole batch | Cross-file atomic rollback contradicts doc 05's never-delete, atomic-per-file model. The honest primitive is N independently reversible operations; whether doc 17 groups them into one `u` press is 17's call, not this one's. |
| Persisting a session cursor for resume | Duplicated truth; goes stale under Syncthing. See §3.7. |
| A second fuzzy matcher for `F` | Would rank differently from the telescope pickers and train two muscle memories for one query. §2. |
| Hint labels in the capture pane | It is a real editable vault file. A modal key-capture loop over a buffer the user may be mid-edit in means any loop bug types characters into the vault. Navigation there is vim's own. |
| Global (non-buffer-local) throughput keymaps | 03 §2's "No global keymaps by default" is unqualified. `<Plug>` mappings are the supported path. |
| Mouse-clickable hint targets | The user is keyboard-driven (01), and it would be the plugin's only mouse surface. |
| A drag-to-reorder queue UI | `<leader>f` filters plus `order=` cover the real need at a fraction of the surface. |
| A "quiz mode" that hides suggestions to train the user | No throughput value, and it writes actions that were not real decisions into a corpus doc 12 defines as a record of what was actually done. |

## 5. Configuration surface

Client-side, `setup()` — these exist only where rows are rendered, so no CLI can disagree with them:

```lua
ui = {
  numeric_accept = true,          -- bind 1-9 to the Nth row (organize pane only)
  batch_confirm = true,           -- confirm before applying a batch
  batch_max = 100,                -- refuse a larger batch; narrow filters instead
  preview_debounce_ms = 120,      -- destination preview fetch debounce
  display = { show_progress = true },       -- done/left/rate/ETA on the header row
  hint = {
    alphabet = "asdfghjkl;qwertyuiopzxcvbnm",  -- home row first; min 4 letters
    case_sensitive = false,       -- false ⇒ shifted label = jump-without-select
    default_action = "select",    -- only consulted when case_sensitive = true
    position = "overlay",         -- "overlay" (covers the type marker) | "eol"
    persist = false,              -- keep labels drawn after a jump-only press
    unknown_key = "abort",        -- "abort" | "ignore"; never replayed as a motion
    vault_scope = "folders",      -- "folders" | "folders+files" for F
  },
  highlights = {                  -- ⚠ five additions to the closed 7-key schema
    hint_label = "Search", hint_label_dim = "Comment", hint_dim = "NonText",
    pinned = "DiffText", marked = "Visual",
  },
}
```

Core-side, `~/.config/organize-core/config.toml` — behaviour the CLI must reproduce:

```toml
[throughput]
mru_size = 10                     # dest.recent list length
mru_order = "recent"              # "recent" | "frequent"
preview_notes = 5                 # notes shown per destination preview

[session]
order = "unseen_first"            # "oldest" | "newest" | "unseen_first"; --order overrides
```

**Every fallback highlight group the plugin names must be defined.** ⚠ `ParaOrganizeSelected` and `ParaOrganizeScore{High,Medium,Low}` are named in the current tree and defined nowhere, which is why `integrate.lua` had to hardcode `DiffAdd`/`DiffDelete`. The new groups above ship with real defaults, and `setup()` calls `nvim_set_hl(0, name, { default = true, link = … })` for every `ParaOrganize*` name the plugin can emit.

### Wire deltas (the complete blast radius)

| Delta | Kind | Needs an architect ruling? |
|---|---|---|
| `folder.list` gains `depth` (default `1`) | additive param | No — `EXTRA_METHODS` unchanged |
| `session.start` gains `order` (default `"unseen_first"`) | additive param | No — `EXTRA_METHODS` unchanged |
| folder entries gain `note_count` | additive result field | No |
| `dest.recent` | **new read-only method** | **Yes** — must be added to `EXTRA_METHODS` with its justification |
| `ActionContext` gains `pinned`, `batch_size`, `keystrokes` | first-class fields, never `filters` entries | Records the `dry_run`/`partial_failure` precedent |

## 6. The complete keymap table

Pane column: **both** = bound in the organize pane and, subject to `ui.capture_pane_keymaps`, in the capture pane; **organize** = organize pane only.

| Group | Key | Config key | Pane | Action |
|---|---|---|---|---|
| Session | `<CR>` | `accept` | both | Accept selection / open item |
| Session | `<Esc>` | `cancel` | both | Close UI |
| Session | `<Tab>` / `<S-Tab>` | `next` / `prev` | both | Next / previous capture |
| Session | `s` | `skip` | both | Skip capture |
| Session | `a` | `archive` | both | Archive capture now |
| Session | `r` | `refresh` | both | Re-run suggestions |
| Session | `?` | `help` | both | Help popup (generated) |
| Navigation | `<A-j>` / `<A-k>` | `next_suggestion` / `prev_suggestion` | both | Move selection |
| Navigation | `<C-h>` / `<C-l>` | `focus_capture` / `focus_organize` | both | Focus pane |
| Navigation | `<BS>` | `back` | both | Back to parent while browsing |
| Navigation | `S` | `sort_cycle` | both | Cycle sort mode |
| Navigation | `/` | `search` | both | Inline destination search |
| Navigation | `p` | `toggle_preview` | both | Toggle destination preview (§3.8) |
| **Hint** | `f` | `hint_row` | organize | **Label visible rows; jump/select (§1)** |
| **Hint** | `F` | `hint_vault` | organize | **Filter + label every vault folder (§2)** |
| **Throughput** | `.` | `repeat_destination` | organize | **Send where the last one went (§3.1)** |
| **Throughput** | `P` | `pin_destination` | organize | **Pin / unpin sticky destination (§3.2)** |
| **Throughput** | `x` | `mark_capture` | organize | **Toggle capture in batch (§3.3)** |
| **Throughput** | `<leader>ba` | `batch_all` | both | **Mark all captures matching filters** |
| **Throughput** | `<leader>bc` | `batch_clear` | both | **Clear the batch** |
| **Throughput** | `<leader>d` | `mru` | both | **Recent destinations, hint-labelled (§3.5)** |
| **Throughput** | `<leader>f` | `filter_session` | both | **Re-filter the session queue (§3.6)** |
| **Throughput** | `1`–`9` | `ui.numeric_accept` (boolean) | organize | **Accept the Nth row (§3.4)** |
| Merge/integrate | `m` | `merge` | both | Merge via pickers |
| Merge/integrate | `<leader>mc` / `<leader>mx` | `merge_complete` / `merge_cancel` | both | Complete / cancel merge |
| Merge/integrate | `<leader>mi` / `<leader>mm` | `integrate` / `integrate_mode` | both | Integrate / choose mode |
| Merge/integrate | `e` | `integrate_edit` | organize | Edit the proposed diff (view-scoped) |
| New folders | `<leader>np` / `na` / `nr` | `new_project` / `new_area` / `new_resource` | both | Create + optionally move |
| Metadata (07) | `t`, `i`, … | `metadata_fields[*].keymap` (core config) | organize | Set a metadata field |
| Undo (17) | `u` / `U` / `H` | `undo` / `redo` / `history` | organize | Undo / redo / history view |
| Fields (15) | `zi` | `cycle_fields` | both | Cycle compact → full → raw |
| Auto (13) | `A` | `auto_organize` | both | Auto-organize the current capture |

### Collision proof

The proof is over the **whole** table above, including the rows docs 13, 15 and 17 own — a per-document proof would prove nothing, since collisions are exactly what happens between documents.

1. **Single keystrokes claimed**, in ASCII order: `. / 1 2 3 4 5 6 7 8 9 ? A F H P S U a e f i m p r s t u x` — **29** entries, no repeats. (`i` and `t` are the shipped `metadata_fields` defaults; `e` is view-scoped to `integrate`; `A` is 13 §1; `u`/`U`/`H` are 17; `f F . P x` and the digits are this document's.)
2. **Special keys claimed**: `<CR> <Esc> <Tab> <S-Tab> <A-j> <A-k> <C-h> <C-l> <BS>` — 9 entries, no repeats, and disjoint from (1).
3. **Leader sequences**: the second keystroke is one of `{n, m, b, d, f}`; third keystrokes are `n:{p,a,r}`, `m:{c,x,i,m}`, `b:{a,c}`; `d` and `f` are terminal. No sequence is a prefix of another, and nothing else begins with `<leader>d` or `<leader>f`, so **no leader binding waits on `timeoutlen`**.
4. **Prefix keys**: `z` (15's `zi`) is the only non-leader prefix in the table, and `z` is not itself a terminal binding, so `zi` is unambiguous. It is also the table's one `timeoutlen` cost — native `zz`/`zt`/`zb` now wait inside the panes. That is 15's trade to make and is recorded here only so the whole-table view is honest.
5. **Cross-set**: (1)–(4) are pairwise disjoint by construction. (3) is reachable only after `<leader>`, which is itself in neither (1) nor (2) unless `mapleader` is one of them — which §3.3 handles by not claiming `<Space>` at all.
6. **Metadata reservation**: `f`, `F`, `P`, `x` and the digits are now reserved. `CORE_KEYMAPS` in `organize_core.config` — the constant that enforces metadata-vs-core collisions per the Phase-2 split of doc 07 acceptance test 4 — must gain them, so a colliding `metadata_fields` entry fails **config validation** and is reported by `:checkhealth`, never discovered by a keypress doing the wrong thing. ⚠ Pre-existing gap, named here because this document's additions land in the same table: `CORE_KEYMAPS` today is missing `e`, `<leader>mi` and `<leader>mm` (doc 12's rows), so `[[metadata_fields]] keymap = "e"` is currently accepted and one of the two bindings loses silently. Fix it in the same change.

### The capture pane is a real file buffer

**A new default binding that shadows a native normal-mode command may bind in the organize pane only.** Every new single-character binding does shadow one — `f` find-char, `F` backwards find-char, `.` repeat, `P` **paste-before**, `x` delete-char, digits count prefixes — and the capture pane is the real capture file that Matt edits and `:w`s himself (10 §4). `P` shadowing paste-before in a live vault file is a data-editing hazard, not an inconvenience. So all of them are `panes = {"organize"}`.

The grandfathered exceptions are the existing `s`/`a`/`m`/`p`/`r` rows, which 03 §3 binds in both panes and which `ui.capture_pane_keymaps` exists to control: `"core"` (spec parity, default) binds them, `"navigation"` binds only rows flagged `navigation = true`, `"none"` leaves the capture buffer untouched. Of the new rows, only `<leader>f` is flagged `navigation = true`; the leader-prefixed rows are safe in both panes because they shadow nothing. **`ui.capture_pane_keymaps = "none"` must leave zero new bindings on the capture buffer** — pinned as an obligation, not assumed.

Where which-key is installed (it is, on the live install) the plugin registers a `<leader>b` group label; it must never require which-key. Discoverability beyond that is doc 14 §"`?` help pane"'s obligation, and every row added here is taught by doc 18 — whose `test_every_binding_and_subcommand_is_taught` goes red if any of them ships without a lesson. Hint mode and the pin need that lesson most: they are the two features here that change what a subsequent keypress does.

## 7. Acceptance tests

1. **One-key jump.** A suggestions view with 6 rows; `f` → 6 labels drawn as `virt_text` in `NS_HINT`, first label is the literal `"a"`; pressing `a` selects row 1 and applies it (one `op.move` for row 1's path). Buffer lines are byte-identical before and during hint mode.
2. **Two-char widening.** 40 rows with the default 27-key alphabet → exactly **26** rows keep one-char labels and **14** get two-char labels (smallest `k = 1`, capacity `26 + 27 = 53 ≥ 40`); no label is a prefix of another; pressing the first char of a two-char label narrows the drawn set and does not act.
3. **Jump vs select.** With `case_sensitive = false`, `f` then `S` (uppercase of label `s`) moves `state.selected` to that row and issues **zero** RPCs; `f` then `s` applies it. `<Esc>` mid-loop leaves `state.selected` and the vault unchanged.
4. **Vault reach.** With a folder `resources/performing/impro-games` five levels deep and not on screen, `F` then `impro` narrows to ≤ 26 candidates, labels appear, one keypress moves the capture there — total 7 keystrokes, no telescope window opened, and exactly one `folder.list` call (from the session-start cache).
5. **Pin.** `P` on `areas/health`, then three accepts on captures whose top suggestion is something else → three notes land in `areas/health`; all three ActionRecords carry `pinned: true` and `chosen_rank: null`; the border title contains the literal `"PINNED"` throughout; ending the session and starting a new one leaves nothing pinned.
6. **Batch with partial failure.** Mark 5 captures, accept a destination, make the third fail → records show `applied 2 of 5`, captures 3–5 remain marked, the failed record carries `partial_failure`, `learning.json` gained exactly 2 associations (not 5, not 3).
7. **Repeat refuses cleanly.** `.` as the very first action of a session whose corpus is empty → one notify, zero RPCs, session state unchanged. With one prior move, `.` sends the next capture to that same destination.
8. **Resume skips the skipped.** Session A skips captures 1–3 and organizes 4; session B with `order = "unseen_first"` starts at capture 5, and captures 1–3 appear after every never-shown capture, oldest-first within each group. With `order = "oldest"` the original ordering returns.
9. **Numeric accept.** With `ui.numeric_accept = true` and 4 suggestions, `3` applies suggestion 3 and `7` produces one notify and zero RPCs; rows render the literal prefix `"3. "`. With the flag `false`, `3` is unbound in the organize pane and the `?` overlay contains no `1-9` row.
10. **Capture pane stays clean.** `nvim_buf_get_keymap(capture_buf, "n")` contains none of `f`, `F`, `.`, `P`, `x`, `1`…`9` under **every** value of `ui.capture_pane_keymaps` (`"core"`, `"navigation"`, `"none"`).

## 8. Test obligations

Per ARCHITECTURE "Test anti-vacuity standards (PERMANENT)", binding on every seat and verifier for this document:

1. **Refusal-predicate pins** — mutate the guard away and confirm red, asserting at a parameter where the other branch would fire, plus a firing control. Required for: `.` with no previous destination; numeric accept with `N >` row count; pin on a file destination with `mode = "move"`; `<leader>ba` over `ui.batch_max`; an unrecognised `session.start` `order`; `ui.hint.alphabet` shorter than 4 letters; `<leader>f` whose filter matches zero notes (the guard is *the current session survives*, so the pin must assert the old session is still installed, not merely that no error was raised).
2. **Constant assertions** — assert the **literal** value, never the imported module constant: the literal `"asdfghjkl;qwertyuiopzxcvbnm"`, the literal default `depth` of `1`, the literal default `order` of `"unseen_first"`, the literal `10` for `mru_size`. Separately assert each constant agrees with its literal.
3. **Must-not-be-connected invariants** — pin the *connection*, not today's consequence. (a) `NS_HINT` is a distinct namespace: clearing it leaves `NS_ORGANIZE` and `NS_CAPTURE` extmark counts unchanged. (b) Hint mode never binds in the capture pane: assert the *absence* of the mappings (test 10), not merely that nothing bad happened. (c) `keystrokes`/`pinned`/`batch_size` are absent from `context.filters` — a first-class-field test that only checks the value is present passes equally with the value smuggled into `filters`.
4. **Every new config leaf has a reader**, or it is listed in `RESERVED_CONFIG_LEAVES` in `tests/test_config.py` with a reason. A documented key with no reader is a defect, per the standing Phase-3 addendum.
5. **`EXTRA_METHODS` stays exact.** `test_rpc_methods_are_exactly_the_spec_10_names_in_order` must remain green after the `folder.list` and `session.start` param additions (they add no method name). `dest.recent` may be added only with the architect ruling and its justification comment written in the existing house shape.
6. **CLI drift guard.** Every `organize …` invocation quoted in this document (`organize dest recent --json`, `organize session --order …`) is covered by a drift-guard test that executes the literal string, per the pattern established for the deep_research seat.
7. **Bytes-on-disk E2E.** The spec-09 §3 headless-nvim gate gains one hint-jump session and one 5-capture batch, asserting against bytes on disk — not against the plugin's own state table.
8. **Performance** (09 §4 budgets, `-m slow`): label draw ≤ 16 ms for 200 rows; `F` filter re-render ≤ 16 ms per keystroke over 5,000 cached folders; `dest.recent` < 50 ms; `folder.list depth="all"` < 200 ms cold on the 13,252-note index and never on the keypress path; no batch step blocks the editor > 50 ms.
9. **Mutation audit before handback**, seat standard: report the count of mutations introduced and killed for every pin above.
