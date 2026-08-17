# 16 — Fast Navigation and High-Throughput Organizing (NEW)

Directive from Matt (2026-08-16): *"what are the shortcuts for using this program for the para organize program? for example, in the sorting index, i would like to have it so that I can VERY quickly navitage to the exact folder (using something like prefaces, like for example with tmux thumbs, it allows me to use prefixes to select or go to folders)"* and *"think about other things id want to do"*.

Sizing fact, measured on the live install the evening of cutover (`doc/FEEDBACK-EVIDENCE-2026-08-16.md` §4): the index holds 13,252 notes and a **1,862-capture backlog**. Every keystroke this document removes from the per-capture loop is worth ~1,862 keystrokes. Every ambiguous keystroke it *adds* is worth ~1,862 hesitations. Both directions are specified against that number, not against a demo vault.

This doc adds bindings and views. It changes no file-operation semantics: doc 05's invariants and doc 10 §1's thin-client law bind everything here, and every operation below is an ordinary doc-05 operation reached by a shorter path.

## 1. Hint jump — labels on rows (`f`)

`f` (config `keymaps.buffer.hint_row`) enters **hint mode** over the right pane. Every selectable row in the current view — suggestion, root, dir, or file, whatever `ui/render.lua` put in its `map` — is overlaid with a short label; one or two keys then take that row.

### Label generation

| Rule | Specification |
|---|---|
| Alphabet | `ui.hint.alphabet`, default `"asdfghjklqwertyuiopzxcvbnm"` (**26 keys**) — home row left-to-right first, then top row, then bottom row. Labels are assigned from the front, so the common case is a home-row key. ⚠ `;` is **not** in the default alphabet and may not be added: step 3 of the interaction loop below makes the *uppercase* form of a label the jump-only binding, and `string.upper(";") == ";"` while the shifted glyph is layout-dependent (`:` on a US layout, `+` on a German one — and 14 §1 names a German user). Every character of `ui.hint.alphabet` must have a **distinct shifted form**; one that does not is a loud `ConfigError` at setup naming the offending character, beside the min-4 / all-distinct check below. |
| Ordering | Labels are assigned in **render order**, never in score order. A label is a function of a row's position in the pane, so re-entering hint mode on an unchanged list yields identical labels and the muscle memory holds. |
| One char first | With `n ≤ #alphabet` candidates, every candidate gets a one-char label. |
| Widening | With `n > #alphabet`, reserve the **smallest** `k` keys from the alphabet's tail as two-char prefixes such that `(#alphabet − k) + k·#alphabet ≥ n`. Smallest `k` maximises the count of rows still reachable in one keystroke, and because assignment is in render order those rows are the top-ranked suggestions. Worked example with the 26-key default and `n = 40`: `26 + 25k ≥ 40` ⇒ `k = 1`, so 25 rows keep one-char labels and 15 take two-char ones. Beyond `#alphabet²` (676) candidates the same rule recurses to three chars. |
| Prefix-free | A key is either a terminal label or a prefix, **never both**. There is therefore no ambiguity and **no timeout** — the classic easymotion "wait to see whether a second key is coming" stall is designed out rather than tuned. |
| Collisions | Impossible by construction, not detected at runtime. A candidate set larger than the tri-graph capacity of a user-shortened alphabet is a loud `ConfigError` at setup naming `ui.hint.alphabet` (minimum 4 keys, all distinct). |

### Drawing

Labels are extmark `virt_text` with `virt_text_pos = "overlay"` at column 0 of each candidate row, in a **dedicated namespace `NS_HINT`** owned by a new `ui/hint.lua`. ⚠ This is a new drawing path: `ui.lua`'s local `apply_marks` understands only `{line, col, end_col, hl}` and cannot emit virtual text, and reusing `NS_ORGANIZE` would make clearing hints also clear the selection highlight. `NS_HINT` is cleared and rebuilt on every keystroke of the loop and is empty at every other moment.

Overlay at column 0 covers the type marker (`[P]`, `[D]`) — redundant while hinting — so **no row's text moves**. ⚠ The overlay is padded to `vim.fn.strdisplaywidth(marker)` cells **computed from the rendered row**, never assumed to be 3, and a one-char label is padded to that same width so labels stay column-aligned. The 3-cell assumption is wrong on delivery: `ui.icons` is a shipped **stable** seam (14) whose first no-fork scenario replaces `[P]` with a 1–2 cell devicon, and a fixed 3-cell overlay would then eat into the row's name. If the marker is narrower than the label the overlay is not widened — the label is drawn `virt_text_pos = "eol"` for that row instead, because covering a name is worse than losing alignment. `ui.hint.position = "overlay" | "eol"` for users who want the marker kept everywhere. Labels are **never buffer text**: the organize pane is a scratch buffer and `f` binds only in the views where it is not modifiable (§6), but the law in ARCHITECTURE "Structural safety decisions" is uniform and the test is cheap (§8, obligation 3).

Non-candidate lines are dimmed with `ui.highlights.row_dim` while the loop runs, so the eye goes to the labels.

### The interaction loop

1. `f` draws labels, issues an explicit `vim.cmd("redraw")`, and enters a `vim.fn.getcharstr()` read loop. Hint mode is **modal**, not a set of keymaps — so a label letter that is also a bound action (`s`, `a`, `m`) is unambiguous inside the loop and unaffected outside it. ⚠ **Every extmark rebuild inside the loop is followed by an explicit `redraw`.** A `getcharstr()` loop does not return to the main loop between reads, so Neovim never repaints on its own: without the explicit call the initial labels and every narrowing in step 4 are invisible, and the user is typing blind into a pane that still shows the pre-hint frame.
2. **A lowercase terminal label selects and acts** — identical to moving the cursor to that row and pressing `<CR>` (accept the suggestion / descend the folder / start the merge, per 03 §3's dispatch on `item.kind`).
3. **The uppercase form of the same label jumps only** — moves `state.selected` to that row, redraws, and stays in the view. Matt gets both behaviours from one label with zero extra keystrokes. This works because `ui.hint.case_sensitive = false` (default) leaves the shifted keyspace free; with `case_sensitive = true` the shifted forms become distinct labels and the modifier is gone, so `ui.hint.default_action = "select" | "jump"` then decides what every press does. The trade-off is stated in the config comment, not hidden.
4. A prefix key narrows: labels not under that prefix are erased, the survivors are redrawn showing their **second character only**, `redraw` is issued (step 1), and the loop reads again.
5. `<Esc>` aborts — clear `NS_HINT`, no state change, no notification. ⚠ **`<C-c>` never arrives as a key.** `getcharstr()` *raises* on interrupt rather than returning `"\3"`, so the call is `pcall`-wrapped and the error is inspected: a message matching `Vim:Interrupt` maps to the same abort path as `<Esc>`, and any other error is re-raised after the cleanup below. **Every** exit from hint mode — terminal label, jump, abort, unknown key, interrupt, re-raised error — clears `NS_HINT`, and the clear lives in the failure branch as well as the success branch (a `finally`-shaped cleanup, not a line at the bottom of the happy path). An interrupted hint session that leaves labels painted over the pane forever is the failure this clause exists to prevent, and §7 test 3 drives it.
6. A key that is neither label nor prefix **aborts with a one-line notify** (`ui.hint.unknown_key = "abort" | "ignore"`, default `abort`). It is never replayed as a motion: replaying into a pane that can be switched to an editable buffer is how a hint implementation types characters into a vault file.
7. `ui.hint.persist = false` (default) erases labels on exit; `true` keeps them drawn after a jump-only press so the next jump costs one key.

## 2. Hint jump over the whole vault (`F`)

`f` labels what is on screen. Matt asked to reach *"the exact folder"*, which is usually not on screen. `F` (`keymaps.buffer.hint_vault`) opens **vault hint**: the candidate set is every folder in the vault, filtered as he types, labelled as soon as the set is small.

- **Filter-then-label, never both at once.** While the candidate set is larger than `#alphabet`, every printable key appends to a fuzzy filter string and the list re-renders; **labels appear only once the set fits one-char labels**. There is therefore never a keystroke that could be read as either a filter character or a label. `<BS>` deletes a filter character — and so does `<C-h>`, because they are the same byte in many terminals (§6, set 2) — `<CR>` takes the top candidate, `<Esc>` aborts, `<C-n>`/`<C-p>` move without labels. ⚠ `<C-n>`/`<C-p>` are doc 18's narrator keys outside this loop; inside it they move the candidate cursor, which is the modal precedence of §6 item 6 working as specified, not a collision.
- **Candidate source.** ⚠ `folder.list` today returns only the *immediate* subfolders of each PARA root. It gains an additive `depth` param, typed `integer >= 1 | "all"`, default `1`, **counted from each PARA root** (so depth `1` is today's exact behaviour). Deliberately a **parameter, not a new method**, so this delta adds no name to `EXTRA_METHODS` in `tests/test_server_protocol.py` and needs no ruling of its own.
- **Fetched once, invalidated properly, and there is only one cache.** The vault folder list is fetched asynchronously at `session.start` (before the user can press `F`, so the press is served from cache) into `state.folders`, and invalidated by `folder.create` and `index.reindex`. ⚠ **`load_roots` stops issuing its own call**: the roots are *derived* from the single depth-parameterised fetch, and `state.roots` becomes a projection of `state.folders` rather than a second cache of the same truth. `actions.lua:403` already issues one `folder.list` at session start, so without this unification the session would make two and acceptance test 4's "exactly one `folder.list` call" would be false on delivery. It also fixes the existing defect that `state.roots` is cached for a whole session with no invalidation hook — a folder created with `<leader>np` is invisible to the browse tree until a new session — in one place instead of two.
- `F` labels **folders**. There is no scope knob: `folder.list depth="all"` returns folders only, so `"folders+files"` would have no data source and no reader — a dead key under 03 §1 — and 13,252 notes is not a label-candidate set. Notes are what merge and the pickers are for.

**Relationship to telescope (03 §4) — the rule, so neither surface is reimplemented badly.** Telescope owns *multi-criteria, preview-heavy, note-level* selection: the nine saved searches, merge-target selection, `open_folder_notes_picker`. Hint mode owns *single-destination selection where the answer is on screen or one word away*. `f`/`F` never open a floating picker and never leave the two-pane layout; that is their entire advantage. **`F` does not implement fuzzy matching**: it uses **`require("telescope.config").values.generic_sorter({})`** when telescope is present and a documented substring-then-subsequence fallback when `pickers.telescope_enabled` is false, so ranking never disagrees between the picker and the hint list. ⚠ It must be `generic_sorter`, not `telescope.sorters.get_fzy_sorter()`: `pickers.lua:734` and `:944` construct their sorters through `telescope.config`'s `values`, which honours the user's own `defaults.generic_sorter` override — so only `generic_sorter({})` actually achieves the stated goal, and a hardcoded fzy sorter would silently rank differently from the picker for any user who configured telescope. Pinned by a test asserting the **same function object** backs both surfaces. A third bespoke matcher is forbidden — it would train two different muscle memories for the same query.

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

- **Three simultaneous indicators, because a silently-pinned destination is the most dangerous feature in this document** — it misfiles N captures in a row before anyone notices. (i) the organize pane border title becomes `" Organize — PINNED: areas/health "`; (ii) a `virt_lines` banner above line 1 in `ui.highlights.pinned_destination`; (iii) the pinned row is marked in the list. All three, always; none is configurable off.
- **An accidental pin is reachable by `u`.** Per 17 §7, the client-local view snapshot carries `pinned_destination` and `batch_marks`, so `P` followed by `u` leaves nothing pinned, issues **zero** RPCs, and toasts `undid: pin → areas/health`. A pin is closer to Matt's "previous sort" than a browse descent is, and a feature this document calls its most dangerous one must not be the one thing `u` cannot reach.
- The pin clears when the session ends and **does not survive a restart**. A pin restored from yesterday is precisely the misfile risk the indicators exist to prevent.
- A pin whose destination is a *file* is refused unless the route mode is `append` or `integrate`; a pinned `integrate` is allowed only with `review = "diff"` and never with `review = "auto"`.
- **Learning integrity.** A pinned accept is human-decided (`is_human_decided`), so it folds into learning (04 §5) — but the user did not pick from the shown list, so `chosen_rank` is recorded as `null`, not `1`. ⚠ `ActionContext` gains a first-class `pinned: bool`, **not** a `filters["pinned"]` entry — the precedent is explicit in `actions.ActionContext`'s own docstring, where `dry_run` and `partial_failure` were both promoted out of `filters` after smuggling taught `learning.json` a destination association with `count: 3, success_rate: 1.0`.
- Wire: none new beyond the `pinned` context field. **Client** orchestration, **core** recording.

### 3.3 Batch across captures — `x`, `<leader>ba`, `<leader>bc`

`x` (`keymaps.buffer.mark_capture`) toggles the current capture into a batch. With a non-empty batch, the next accept applies the chosen destination to **every marked capture**. `<leader>ba` marks all captures matching the current session filters; `<leader>bc` clears the batch.

- *Why `x` and not `<Space>`*: `<Space>` is a very common `mapleader`, and claiming it would collide with every `<leader>…` sequence in the table for a large fraction of users. *Why not `v`*: `v` is the letter doc 07 uses in its boolean-field example, and `doc/FEEDBACK-EVIDENCE-2026-08-16.md` §5 forbids a new default silently claiming a letter a user's `metadata_fields` might want.
- Confirmation (`ui.batch.confirm = true`) shows the literal count and destination before anything is written. `ui.batch.max = 100` refuses a larger batch with a message telling the user to narrow filters first — `<leader>ba` over an unfiltered 1,862-capture session is not a feature.
- **Applied sequentially through the ordinary per-capture path**, never through a new bulk primitive: every doc-05 invariant, every `ActionRecord` and every learning fold is the **same kind of record** as a hand-filed one. Batch mode is a keystroke saving, not a second write path.
- ⚠ **The records are not identical to hand-filing, and must not claim to be.** Matt saw and ranked exactly one capture — the one on screen when accept fired. For the other N−1 members the decision context is **explicitly null**: `chosen_rank = null`, `suggestions_shown = []`, `durations_ms.decision` omitted, `batch_size = N`. The **marking** capture keeps its real `chosen_rank` and its real `suggestions_shown`. Without this, learning folds N accepts as N rank-1 hits and one `<leader>ba` over 100 captures manufactures a 100-sample confirmation of a suggestion the engine never made — the same self-reinforcement class the pinned accept's `chosen_rank: null` exists to prevent. Asserted in acceptance test 6.
- Partial failure stops at the first failure, reports `applied k of n`, and **leaves the remainder marked** so retry is one keypress. Per the existing ruling, partially-applied records keep `partial_failure` and are excluded from learning and from the accept-rate corpus.
- ⚠ `ActionContext` gains `batch_size: int | null` for the same first-class reason as `pinned`.
- **Undo seam (17), decided.** Because a batch is N ordinary operations, it is N ordinary `ActionRecord`s and N ordinary oplog lines, so it needs no new reversal machinery — and `batch_size` plus a shared `session_id` are what make the group recoverable. Per **17 §7**, one `u` press walks the **whole batch as an all-or-nothing group**: every member's preconditions are evaluated first, any member's refusal refuses the whole group and names that member, and a single confirmation shows the literal count. This document no longer defers the question. The reason it must be decided here too: `<leader>ba` over 100 captures with per-record undo would produce 100 records of which `u` reaches at most the ring depth, one press each — the panic case undo exists for would be the one case it could not serve.
- Wire: none new (n × `op.move`). **Client**; requests are issued through the existing async RPC path with a progress render — the editor is never blocked.

### 3.4 Direct numeric accept — `1`–`9` (`ui.organize.numeric_accept`, default `true`)

Press `3` to take the third suggestion. In browse and search views the digits address the **Nth item in `ui.map`, in render order** — never "the Nth visible row". ⚠ Reason lines (`render.lua:352`) and preview lines (`render.lua:357-364`) are visible rows that are not items, so a screen-line count would drift from the hint labels of §1, which are also assigned over `map` in render order. One ordering, two surfaces.

- Digits bind in the **organize pane only, never the capture pane** — a count prefix is meaningful in a real file buffer and meaningless where `j`/`k` are plain cursor movement. `0` is never claimed.
- ⚠ They are additionally **view-scoped**, for the reason set out in §6: the organize pane is *not* a read-only scratch pane in every state, and a digit typed into the merge buffer must insert a digit. `views = {"suggestions", "browse", "search", "mru", "history"}`, bound and unbound by the same `bind_gate`/`unbind_gate` mechanism `e` already uses.
- `N` greater than the item count ⇒ notify, no-op (pinned refusal predicate).
- Discoverability: when the flag is on, suggestion rows render their index (`1. [P] areas/health`) — an invisible binding is not a feature.
- Not rebindable, and deliberately not nine rows in `keymaps.buffer`: there is no sane way to rebind "the digits", so the honest surface is one boolean. `actions.keymap_table()` emits **one synthetic help row** (`1-9 / accept Nth suggestion / organize`) when the flag is on and zero rows when off, so the generated `?` overlay (03 §2: help "must be generated from the actual keymap table") stays truthful. The nine digits are nevertheless nine separate `CORE_KEYMAPS` rows (§6 collision proof, item 7) — the help surface may collapse them, the collision surface may not.
- The redundancy with hint labels is deliberate and recorded rather than denied: digits address *ranked* suggestions where rank is stable and meaningful; hints address *arbitrary* rows including deep browse listings.

### 3.5 Recent destinations — `<leader>d` (`throughput.mru_size`, `throughput.mru_order`)

`<leader>d` renders the recent-destinations list **in the right pane** (not a floating picker — the layout is the point) with hint labels already applied, so a recent destination costs `<leader>d` plus one key.

It is a right-pane view, and it registers through the `render.VIEWS[name] = fn` dispatch table — **not** as another hardcoded branch in `render.right_pane`. 03 §3's four states are already five with 12 §1's review gate; adding two more by if/elseif is how that function becomes unreadable. ⚠ **Sequencing**: `lua/para-organize/ui/render.lua` is owned by doc **15**, which lands the `VIEWS` registry first as a standalone refactor; only then does this document register `mru` and doc 17 register `history`. Neither later doc lands the registry itself.

- ⚠ Requires one new read-only RPC, **`dest.recent`**. **Entry into `EXTRA_METHODS` is GRANTED** by the arbitration ruling, on the house justification: 10 §1 forbids a thin client from reading vault state itself, and `learn.get_top_destinations` / **`ActionRecorder.query`** (`src/organize_core/actions.py:1106`) exist in the core with **no wire surface**, so the feature is unreachable from any thin client. (The class is `ActionRecorder`, not `ActionLog` — no symbol named `ActionLog` exists in `src/`, `lua/` or `tests/`, and an implementer who searches for it finds nothing.) `tests/test_server_protocol.py` is edited **once**, by doc 17's seat, for all three granted names.
- **Wire shape**, complete:
  - params `{ limit: null, order: null, session_id: null }` — `limit` null ⇒ `throughput.mru_size`; `order` null ⇒ `throughput.mru_order`; `session_id` null ⇒ the whole corpus.
  - result `{ destinations: [ { path, last_used, count } ] }`, most-recent-first, each destination **distinct**. `last_used` is an ISO-8601 UTC string, the same spelling the corpus already uses.
  - an unreadable or absent corpus returns `{ destinations: [] }` — **never an error**.
- CLI sibling `organize dest recent --json` ships with it, so the feature is testable without Neovim (10 §2). ⚠ `dest` is a **new top-level subcommand**, which means all three `cli.py` declaration sites — `SUBCOMMANDS`, `build_parser` and `_HANDLERS` — not two; the drift guard in §8 obligation 6 exists because two-of-three is the failure mode.
- **Core** config, because the core computes it and the CLI must agree with the UI: `throughput.mru_size` (default `10`), `throughput.mru_order = "recent" | "frequent"` (default `"recent"`; frequency is `learn.get_top_destinations`, recency is the action corpus).
- Empty corpus ⇒ an empty list and a one-line "no recent destinations" render, never an error.

### 3.6 Filter the session queue — `<leader>f` (`keymaps.buffer.filter_session`)

A session over all 1,862 captures is unfocused. `<leader>f` prompts for doc 03 §2 filters (`tags= sources= modalities= since= until_date= status= text=`) and restarts the session with them, carrying the processed/skipped counts forward.

- **The new session is installed only after the core returns a non-empty list.** Zero matches ⇒ notify and keep the current session intact. This is 03 §2's "empty session is active-with-empty-list" resolution read for a live swap: an empty result is not an error, but it is also not a reason to throw away the queue Matt was working.
- The nine saved searches (03 §4) are offered as presets. No second filter vocabulary is invented.
- Wire: none new (`session.start`). **Client**.

### 3.7 Progress and resume — `ui.organize.show_progress`, `session.order`

**Progress.** For a 1,862-item backlog the motivating number is the burn-down, not the index. The organize pane header carries a right-aligned virtual-text line: `142 done · 1,720 left · 6.2/min · ~4h37m`. Default on; `ui.organize.show_progress = false` turns it off.

⚠ **Which code draws it.** The progress line is emitted by **`ui/render.lua`** as an extmark `virt_text` with `virt_text_pos = "right_align"` in **`NS_ORGANIZE`**. `ui.lua`'s `apply_marks` is **unchanged** — it emits `{line, col, end_col, hl}` highlight marks only and cannot carry virtual text — and `NS_HINT` is not used, because that namespace belongs to labels and is cleared on every keystroke of the §1 loop. So the line costs no capture-header line and no buffer text.

**Resume.** Quitting mid-backlog and returning must not restart at capture 1.

- Resume is **derived, not persisted**. An organized capture leaves the default session filter by itself — under either spelling of that default, since organizing both clears `status: raw` and moves the note into a PARA folder — so a fresh session already excludes it. (The default filter itself is doc 14's key, not this document's; §3.6's `<leader>f` reads whatever it is.) The real defect is *skipped* captures: they match the default filter unchanged and return to the front of the queue every session, forever.
- ⚠ `session.start` gains an additive `order` param — `"oldest" | "newest" | "unseen_first"`, default **`"unseen_first"`** — where "seen" means the capture has an `op.skip` ActionRecord. Never-shown captures sort before previously-skipped ones, oldest-first within each group. This is satisfied entirely from the existing corpus: no new state file, no new method, and it is exactly what `op.skip` was approved for ("the counterfactual silently lost every time a user pressed `s`").

> ⚠ **Deviation from 03 §2.** 03 §2 states: *"oldest first by timestamp … deterministic order is required."* This document changes the shipped **default** from `"oldest"` to `"unseen_first"`, and declares it rather than smuggling it.
>
> - **Determinism is untouched.** `unseen_first` is a two-group partition (never-shown, then previously-skipped) and **oldest-first by timestamp survives *within* each group**. The order is still a total function of the vault and the corpus, still reproducible, still identical across two runs on the same inputs. What changes is the grouping, not the tie-breaking.
> - **03's exact ordering is one flag away.** `[session] order = "oldest"` (or `--order oldest`) restores it byte-for-byte, and acceptance test 8 asserts that it does.
> - **Why the deviation is worth taking.** With `"oldest"`, skipped captures stay `raw` and return to the front of the queue every session forever, so a 1,862-capture backlog re-presents the same rejected items at every sitting and never converges. A deterministic order that never terminates is not the property 03 §2 was protecting.

- **The skip scan is bounded.** ⚠ New core key **`[session] seen_scan_months`** (default `2`) caps how far back the `op.skip` scan reads the corpus; skips older than that are treated as unseen. Budgeted in §8 obligation 9: `session.start` under 200 ms on the 13,252-note / 1,862-capture corpus. Exceeding the budget falls back to the same target as the empty-corpus case below — `"oldest"` — but **loudly**: one `vim.notify` plus a row in `organize health`. The empty-corpus fallback may be silent because nothing was lost; a *budget* fallback silently changes the queue order the user is working, so it must announce itself. Neither is a silent slow path on the default of every session.
- Missing or empty corpus ⇒ silent fallback to `"oldest"`. An unrecognised `order` value is a loud `-32602`.
- **Core** config default (`session.order`) with a per-invocation override (`--order` on the CLI, `order` on the RPC), because `organize session` and the UI must produce the same queue.
- Rejected alternative, recorded: persisting a session cursor to a state file. It duplicates a truth the vault and the corpus already hold, and a Syncthing sync or an out-of-band edit (02: "any file can change under you mid-operation") leaves the cursor pointing at a note that has moved.

### 3.8 Destination preview — `p` (`toggle_preview`, bound here for the first time)

03 §3 flags `toggle_preview` as a binding that "must actually be bound, or delete the option". This is what it is for — and it is therefore a **new** binding, not a grandfathered one (§6). With preview on, the **selected** destination expands inline (virtual lines) to show: its doc 11 §3 description, its note count, and the `ui.organize.preview_notes` (default `5`) most-recently-modified notes inside it — *what already lives there, before committing*.

- ⚠ `preview_notes` is **nvim-side**, a leaf appended to 15 §7's `ui.organize` record, and is documented here. It is not `[throughput] preview_notes`: there is no CLI destination preview, so no CLI can disagree with it (14 §4.1's one question), and a core key with no core reader is a dead key under 03 §1. `throughput.mru_size`, `throughput.mru_order` and `[session] order` stay core because `organize dest recent` and `organize session` must reproduce them.
- Data: `folder.children` (exists) plus the description already carried by `folder.list`. ⚠ `_folder_entry` gains a `note_count` field — additive, read-only, no new method, and **direct children only** (§5).
- **Lazily fetched for the selected row only**, debounced `ui.organize.preview_debounce_ms = 120`, cached per session. A preview per cursor move is a `folder.children` per cursor move; the keypress is never blocked on it, and a slow reply renders when it lands or not at all.

### 3.9 Keystroke accounting — session-local, never a corpus field

Keystroke accounting is a **session-local counter surfaced by `:ParaOrganize debug`**; it is not written to the action corpus. The falsifiability question this document owes — did hint mode and pinning actually shorten the loop? — is answered from the **existing `durations_ms.decision`** by `organize actions stats`, which needs no new field at all. ⚠ An earlier draft added `context.keystrokes` to `ActionContext`; it is struck. A corpus field is forever (14 §8.5: the corpus stays a record of *decisions*), nothing depends on it, and writing a client-supplied instrumentation counter into a permanent append-only record purely to measure whether this document worked is the wrong trade.

## 4. Considered and rejected

| Rejected | Reason |
|---|---|
| Vim-style counts (`3<CR>`) instead of bare digits | Digits are already claimed by §3.4, and in the views where they bind (§6: `suggestions`, `browse`, `search`, `mru`, `history`) the organize buffer is not modifiable, so a count prefix has nothing to count; the count form is strictly more keystrokes for the same result. In the views where a count *would* mean something — merge and integrate-edit — the digits are unbound entirely. |
| Auto-accept above a confidence threshold during an interactive session | That is doc 13's trust ladder. Two independent auto-apply paths with different gates is exactly the class of divergence doc 10 §3 exists to prevent. Interactive sessions never auto-apply. |
| Transactional rollback of a whole batch | Cross-file atomic *rollback* contradicts doc 05's never-delete, atomic-per-file model. The honest primitive is N independently reversible operations. That is not the same thing as one `u` press: per 17 §7 a single `u` walks the whole batch as an all-or-nothing **group of ordinary undos**, checking every member's preconditions before reversing any (§3.3) — a grouped traversal of N reversals, not a transaction. |
| Persisting a session cursor for resume | Duplicated truth; goes stale under Syncthing. See §3.7. |
| A second fuzzy matcher for `F` | Would rank differently from the telescope pickers and train two muscle memories for one query. §2. |
| Hint labels in the capture pane | It is a real editable vault file. A modal key-capture loop over a buffer the user may be mid-edit in means any loop bug types characters into the vault. Navigation there is vim's own. |
| Global (non-buffer-local) throughput keymaps | 03 §2's "No global keymaps by default" is unqualified. `<Plug>` mappings are the supported path. |
| Mouse-clickable hint targets | The user is keyboard-driven (01), and it would be the plugin's only mouse surface. |
| A drag-to-reorder queue UI | `<leader>f` filters plus `order=` cover the real need at a fraction of the surface. |
| A "quiz mode" that hides suggestions to train the user | No throughput value, and it writes actions that were not real decisions into a corpus doc 12 defines as a record of what was actually done. |

## 5. Configuration surface

Client-side, `setup()` — these exist only where rows are rendered, so no CLI can disagree with them.

⚠ **Sectioning is the arbitrated one**, `section = subsystem` per 14 §4.2: `ui.capture.*` and `ui.organize.*` (doc 15), `ui.hint.*` and `ui.batch.*` (this document), `ui.undo.*` (17), `ui.teach.*` (18), plus the existing top-level `ui.highlights` / `icons` / `float_opts` / `win_options` / `capture_pane_keymaps` / `close_on_complete`. **`ui.display.*` does not exist** — doc 15 owns that surface and deletes the section, and no document may add a key under it. **Ordering**: doc 15 lands the closed `ui.capture` / `ui.organize` records *first*; this document appends leaves to `ui.organize` and adds `ui.hint` / `ui.batch` afterwards.

```lua
ui = {
  organize = {                    -- ⚠ leaves appended to 15's ui.organize record
    numeric_accept = true,        -- bind 1-9 to the Nth item in ui.map (organize pane, view-scoped)
    show_progress = true,         -- done/left/rate/ETA, right_align on the header row
    preview_debounce_ms = 120,    -- destination preview fetch debounce
    preview_notes = 5,            -- notes shown per destination preview (§3.8)
  },
  batch = {
    confirm = true,               -- confirm before applying a batch
    max = 100,                    -- refuse a larger batch; narrow filters instead
  },
  hint = {
    alphabet = "asdfghjklqwertyuiopzxcvbnm",  -- 26 keys, home row first; min 4, all distinct,
                                  -- every character must have a distinct shifted form
    case_sensitive = false,       -- false ⇒ shifted label = jump-without-select
    default_action = "select",    -- only consulted when case_sensitive = true
    position = "overlay",         -- "overlay" (covers the type marker) | "eol"
    persist = false,              -- keep labels drawn after a jump-only press
    unknown_key = "abort",        -- "abort" | "ignore"; never replayed as a motion
  },
  highlights = {                  -- ⚠ five additions: the closed record grows 7 → 12 leaves
    label = "Search", label_dim = "Comment", row_dim = "NonText",
    pinned_destination = "DiffText", batch_marked = "Visual",
  },
}
```

⚠ The pre-existing **`ui.highlights.hint`** — dim secondary text in the capture card and in the integrate hint (`ui.lua:524`, `integrate.lua:873`) — keeps that meaning and is **untouched by this document**. It is neither renamed nor repurposed, which is why the five additions above use the stems `label` / `row_dim` rather than `hint_label` / `hint_dim`: a `hint_dim` sitting beside a `hint` with an unrelated meaning is unresolvable by a reader. For the same reason the sticky destination is `pinned_destination`, not `pinned` — doc 15 uses "pinned" for a pinned *field*.

Core-side, `~/.config/organize-core/config.toml` — behaviour the CLI must reproduce:

```toml
[throughput]
mru_size = 10                     # dest.recent list length
mru_order = "recent"              # "recent" | "frequent"

[session]
order = "unseen_first"            # "oldest" | "newest" | "unseen_first"; --order overrides
seen_scan_months = 2              # how far back the op.skip scan reads for "unseen_first"
```

⚠ `preview_notes` is **not** here — it is `ui.organize.preview_notes` (§3.8). There is no CLI destination preview, so no CLI can disagree with it and no core reader would exist for it.

**Every fallback highlight group the plugin names must be defined.** ⚠ `ParaOrganizeSelected` and `ParaOrganizeScore{High,Medium,Low}` are named in the current tree and defined nowhere, which is why `integrate.lua` had to hardcode `DiffAdd`/`DiffDelete`. The new groups above ship with real defaults, and `setup()` calls `nvim_set_hl(0, name, { default = true, link = … })` for every `ParaOrganize*` name the plugin can emit.

### Wire deltas (the complete blast radius)

| Delta | Kind | Needs an architect ruling? |
|---|---|---|
| `folder.list` gains `depth`, typed `integer >= 1 \| "all"`, default `1`, counted **from each PARA root** | additive param | No — `EXTRA_METHODS` unchanged |
| `session.start` gains `order` (default `"unseen_first"`) | additive param | No — `EXTRA_METHODS` unchanged |
| ⚠ **`_SESSION_START_RESERVED` gains `"order"`** | reserved-key list edit | No — but **not optional**: `server.py:1286` rejects an unknown top-level key to `session.start` with a hard `-32602`, so the "additive" param above is not additive without this row |
| folder entries gain `note_count` — **direct children only, never recursive** | additive result field | No — a recursive count would be a full vault walk per `folder.list` call over a 13,252-note tree |
| `dest.recent` | **new read-only method** | **GRANTED.** Added to `EXTRA_METHODS` with the justification in §3.5; `tests/test_server_protocol.py` is edited once, by doc 17's seat, for all three granted names |
| `ActionContext` gains `pinned`, `batch_size` | first-class fields, never `filters` entries | Records the `dry_run`/`partial_failure` precedent |

## 6. The complete keymap table

Pane column: **both** = bound in the organize pane and, subject to `ui.capture_pane_keymaps`, in the capture pane; **organize** = organize pane only. Rows are added through `actions.register_key{ name, default, desc, panes, views, fn }` — never by editing `CORE_KEYS` directly (14 owns `actions.lua`). The scoping field is **`views`**, a list of view names; there is no `mode` field, because in Neovim "mode" means the Vim mode and the live table already scopes by view. Every single-character row this document adds is additionally view-scoped — see "Neither pane is safe to shadow in", below.

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
| Navigation | `<BS>` | `back` | organize | Back to parent while browsing |
| Navigation | `S` | `sort_cycle` | both | Cycle sort mode |
| Navigation | `/` | `search` | both | Inline destination search |
| Navigation | `p` | `toggle_preview` | organize | Toggle destination preview (§3.8) |
| **Hint** | `f` | `hint_row` | organize | **Label visible rows; jump/select (§1)** |
| **Hint** | `F` | `hint_vault` | organize | **Filter + label every vault folder (§2)** |
| **Throughput** | `.` | `repeat_destination` | organize | **Send where the last one went (§3.1)** |
| **Throughput** | `P` | `pin_destination` | organize | **Pin / unpin sticky destination (§3.2)** |
| **Throughput** | `x` | `mark_capture` | organize | **Toggle capture in batch (§3.3)** |
| **Throughput** | `<leader>ba` | `batch_all` | both | **Mark all captures matching filters** |
| **Throughput** | `<leader>bc` | `batch_clear` | both | **Clear the batch** |
| **Throughput** | `<leader>d` | `mru` | both | **Recent destinations, hint-labelled (§3.5)** |
| **Throughput** | `<leader>f` | `filter_session` | both | **Re-filter the session queue (§3.6)** |
| **Throughput** | `1`–`9` | `ui.organize.numeric_accept` (boolean) | organize | **Accept the Nth item in `ui.map` (§3.4)** |
| Merge/integrate | `m` | `merge` | both | Merge via pickers |
| Merge/integrate | `<leader>mc` / `<leader>mx` | `merge_complete` / `merge_cancel` | both | Complete / cancel merge |
| Merge/integrate | `<leader>mi` / `<leader>mm` | `integrate` / `integrate_mode` | both | Integrate / choose mode |
| Merge/integrate | `e` | `integrate_edit` | organize | Edit the proposed diff (view-scoped) |
| New folders | `<leader>np` / `na` / `nr` | `new_project` / `new_area` / `new_resource` | both | Create + optionally move |
| Metadata (07) | `t`, `i`, … | `metadata_fields[*].keymap` (core config) | organize | Set a metadata field |
| Undo (17) | `u` / `U` / `H` | `undo` / `redo` / `history` | organize | Undo / redo / history view |
| Fields (15) | `zi` | `cycle_fields` | organize | Cycle compact → full → raw |
| Auto (13) | `A` | `auto_organize` | both | Auto-organize the current capture |
| Teach (18) | `<C-n>` / `<C-p>` / `<C-g>` / `<C-r>` / `<C-q>` | `teach_next` / `teach_prev` / `teach_hint` / `teach_replay` / `teach_quit` | organize | Teach-mode narrator controls |

### Collision proof

The proof is over the **whole** table above, including the rows docs 13, 15, 17 and 18 own — a per-document proof would prove nothing, since collisions are exactly what happens between documents. Sets **(1)–(4) partition every claimed keystroke** — terminal printables, special/modified keys, leader sequences, prefix keys — with nothing handled outside them; (5) discharges the disjointness; (6) states the modal precedence; (7) records the reservation patch.

1. **Terminal single keystrokes**, in ASCII order: `. / 1 2 3 4 5 6 7 8 9 ? A F H P S U a e f i m p r s t u x` — **29** entries, no repeats. (`i` and `t` are the shipped `metadata_fields` defaults; `e` is view-scoped to `integrate`; `A` is 13 §1; `u`/`U`/`H` are 17; `f F . P x` and the digits are this document's.) `z` is **not** in this set — see (4).
2. **Special and modified keys claimed**: `<CR> <Esc> <Tab> <S-Tab> <A-j> <A-k> <C-h> <C-l> <BS>` (9, from 03/12) plus doc 18's narrator group `<C-n> <C-p> <C-g> <C-r> <C-q>` (5) — **14** entries, no repeats, and disjoint from (1) because no member is a bare printable character. The 18 group is additionally disjoint from `<C-h>`/`<C-l>`: `{n,p,g,r,q} ∩ {h,l} = ∅`. ⚠ One byte-level note that is not a collision but reads like one: `<C-h>` (`focus_capture`) and `<BS>` (`back`) are **the same byte** (`0x08`) in many terminals, so Neovim may deliver either as the other. §2's filter-delete builds on exactly this — `F`'s modal loop accepts either byte as "delete a filter character", so no user has to know which their terminal sends. Outside the loop both rows are navigation-class and neither is destructive, so the worst case is a focus change where a parent-descent was meant. It must **not** be "fixed" by rebinding one of them into set (1), which would trade a harmless ambiguity for a real one.
3. **Leader sequences**: the second keystroke is one of `{n, m, b, d, f}`; third keystrokes are `n:{p,a,r}`, `m:{c,x,i,m}`, `b:{a,c}`; `d` and `f` are terminal. No sequence is a prefix of another, and nothing else begins with `<leader>d` or `<leader>f`, so **no leader binding waits on `timeoutlen`**.
4. **Prefix keys** — a first-class set, not an aside: `z` (15's `zi`) is the table's only non-leader prefix. `z` is not itself a terminal binding, so `zi` is unambiguous. ⚠ **`zi` is `panes = {"organize"}`**, so `z` is a prefix **in the organize pane only**. Consequences, both load-bearing: native `zz`/`zt`/`zb` wait on `timeoutlen` **inside the organize pane only** — the table's single `timeoutlen` cost, in the pane where centring the view matters least — and `zo`/`za` in the **capture pane stay instant**. The second half is not a nicety: 15 §3's fold-recovery path is the user reopening the frontmatter fold in the capture buffer with `zo`/`za`, and that claim is true as written only because `z` is unbound there.
5. **Cross-set**: (1)–(4) are pairwise disjoint by construction — (1) is terminal printables, (2) contains no bare printable, (4) contains only `z` which (1) excludes by name, and (3) is reachable only after `<leader>`, which is itself in neither (1) nor (2) unless `mapleader` is one of them (which §3.3 handles by not claiming `<Space>` at all).
6. **Modal precedence, stated once**: **hint loop > teach narrator > pane keymaps.** The §1 `getcharstr` loop is modal, so while it runs *nothing else in this table is reachable* — including doc 18's `<C-q>`. That is by design, not an oversight: a modal loop that let a narrator key through would be a loop with an escape hatch that is not `<Esc>`. `<Esc>` and `Vim:Interrupt` exit the loop (§1 step 5); the narrator is reachable again immediately afterwards.
7. **Metadata reservation — one patch, one owner.** The complete `CORE_KEYMAPS` reservation for docs 12/13/15/16/17/18 lands as **ONE patch owned by doc 14** (14 §3), which also carries the drift gate (`CORE_KEYMAPS`'s key set equals the set of non-metadata `lhs` values in `keymap_table()`). This document **contributes** `f`, `F`, `.`, `P`, `x`, the **nine** digits as nine separate rows, `<leader>ba`, `<leader>bc`, `<leader>d`, `<leader>f`. The pre-existing gap — `CORE_KEYMAPS` today is missing `e`, `<leader>mi` and `<leader>mm` (doc 12's rows), so `[[metadata_fields]] keymap = "e"` is currently accepted and one of the two bindings loses silently — is fixed **in that patch**, not "in the same change" as this document's. The point of the reservation is unchanged: a colliding `metadata_fields` entry fails **config validation** and is reported by `:checkhealth`, never discovered by a keypress doing the wrong thing.

### Neither pane is safe to shadow in — the capture pane always, the organize pane sometimes

**A new default binding that shadows a native normal-mode command may bind in the organize pane only.** Every new single-character binding does shadow one — `f` find-char, `F` backwards find-char, `.` repeat, `P` **paste-before**, `x` delete-char, `p` **paste-after**, digits count prefixes — and the capture pane is the real capture file that Matt edits and `:w`s himself (10 §4). `P` shadowing paste-before in a live vault file is a data-editing hazard, not an inconvenience. So all of them are `panes = {"organize"}`.

⚠ **`panes = {"organize"}` is necessary but not sufficient, and an earlier draft of this section was wrong about why.** The rationale said the organize pane is a read-only `nofile` scratch pane where a shadowed editing command has no meaning. **That is false in two states.** `ui.lua:603-604` sets

```lua
vim.bo[ui.organize_buf].modifiable = (view == "merge") or editing
```

so in the **merge** view, and while **integrate-editing**, the organize buffer holds text the user is typing — text that `<leader>mc` then writes into a **real vault note**. In those states `x` deletes a character of that text, `P` pastes into it, `p` pastes after it, `.` repeats the last edit, a digit is a count, and `u` (17) fires `op.undo` against an unrelated vault action **while the typed merge text becomes unrecoverable**. This is the only data-loss path this document's bindings could open, and it is closed structurally:

**Every new single-character organize-pane binding is VIEW-SCOPED.** `f`, `F`, `.`, `P`, `x`, `p`, the nine digits — and 17's `u`, `U`, `H` — carry

```lua
views = { "suggestions", "browse", "search", "mru", "history" }
```

and are **bound and unbound** by the same `bind_gate`/`unbind_gate` mechanism `e` already uses (`actions.lua:1866-1876`, `integrate.lua:762-787`). They are *absent from the buffer's keymap table* whenever the organize buffer is modifiable — not merely inert, not guarded by an early return inside the handler, because a guard inside a handler still swallows the keypress instead of letting Vim do its native thing.

**Exempt, and only these**: `zi` (a fold toggle; non-destructive in any view), `e` (already view-scoped), and every `<leader>` sequence (a leader prefix shadows nothing, which is the whole reason `<leader>ba`/`bc`/`d`/`f` may bind in both panes).

The grandfathered exceptions in the **capture** pane are the existing `s`/`a`/`m`/`r` rows, which 03 §3 binds in both panes and which `ui.capture_pane_keymaps` exists to control: `"core"` (spec parity, default) binds them, `"navigation"` binds only rows flagged `navigation = true`, `"none"` leaves the capture buffer untouched. ⚠ **`p` is not among them.** 03 §1 lists `toggle_preview` among the ~25 dead keys that must be bound or deleted, so §3.8 binds it for the **first** time — it is a new binding, the shadowing rule applies unqualified, and `p` is `panes = {"organize"}` and view-scoped like the rest. Calling it grandfathered would be false against the spec, and it shadows paste-after in exactly the buffer this section exists to protect. Of the new rows, only `<leader>f` is flagged `navigation = true`. **`ui.capture_pane_keymaps = "none"` must leave zero new bindings on the capture buffer** — pinned as an obligation, not assumed.

Discoverability is **doc 14 §6's** obligation, not this document's: 14 §6 registers every buffer binding with which-key using its `desc` from `keymap_table()`, plus a group label for every leader prefix — including `<leader>b` — under 14 §6's drift gate against the `?` pane, and the plugin must never *require* which-key. Every row added here is taught by doc 18, whose keymap-coverage gate goes red if any of them ships without a lesson, and whose new precondition test requires each `teaches` entry to resolve in `actions.keymap_table()`. Hint mode and the pin need that lesson most: they are the two features here that change what a subsequent keypress does.

## 7. Acceptance tests

1. **One-key jump.** A suggestions view with 6 rows; `f` → 6 labels drawn as `virt_text` in `NS_HINT`, first label is the literal `"a"`; pressing `a` selects row 1 and applies it (one `op.move` for row 1's path). Buffer lines are byte-identical before and during hint mode.
2. **Two-char widening.** 40 rows with the default **26-key** alphabet → exactly **25** rows keep one-char labels and **15** get two-char labels (smallest `k = 1`, capacity `25 + 26 = 51 ≥ 40`); no label is a prefix of another; pressing the first char of a two-char label narrows the drawn set and does not act. Separately: `ui.hint.alphabet = "asd;"` is a `ConfigError` naming the literal character `;`, and `"asdf"` is accepted (firing control).
3. **Jump vs select, and every exit clears.** With `case_sensitive = false`, `f` then `S` (uppercase of label `s`) moves `state.selected` to that row and issues **zero** RPCs; `f` then `s` applies it. `<Esc>` mid-loop leaves `state.selected` and the vault unchanged. Then, one per exit path — terminal label, uppercase jump, `<Esc>`, unknown key, and a `getcharstr` stub raising `Vim:Interrupt` — assert `nvim_buf_get_extmarks(organize_buf, NS_HINT, …)` is **empty** afterwards. Mutation: move the `NS_HINT` clear out of the failure branch ⇒ the interrupt case fails.
4. **Vault reach.** With a folder `resources/performing/impro-games` five levels deep and not on screen, `F` then `impro` narrows to ≤ 25 candidates, labels appear, one keypress moves the capture there — total 7 keystrokes, no telescope window opened, and **exactly one** `folder.list` call for the whole session (the session-start fetch; `load_roots` issues none of its own, and `state.roots` is asserted to be a projection of `state.folders`).
5. **Pin.** `P` on `areas/health`, then three accepts on captures whose top suggestion is something else → three notes land in `areas/health`; all three ActionRecords carry `pinned: true` and `chosen_rank: null`; the border title contains the literal `"PINNED"` throughout; ending the session and starting a new one leaves nothing pinned.
6. **Batch with partial failure, and honest batch records.** Mark 5 captures, accept a destination, make the third fail → records show `applied 2 of 5`, captures 3–5 remain marked, the failed record carries `partial_failure`, `learning.json` gained exactly 2 associations (not 5, not 3). Separately, on a clean 5-capture batch: every record carries `batch_size = 5`; the **marking** capture's record carries its real `chosen_rank` and a non-empty `suggestions_shown`; the other four carry `chosen_rank = null`, `suggestions_shown = []`, and **no** `durations_ms.decision` key. Assert `learning.json`'s rank histogram gained exactly **one** rank-1 sample, not five.
7. **Repeat refuses cleanly.** `.` as the very first action of a session whose corpus is empty → one notify, zero RPCs, session state unchanged. With one prior move, `.` sends the next capture to that same destination.
8. **Resume skips the skipped.** Session A skips captures 1–3 and organizes 4; session B with `order = "unseen_first"` starts at capture 5, and captures 1–3 appear after every never-shown capture, oldest-first within each group. With `order = "oldest"` the original ordering returns.
9. **Numeric accept.** With `ui.organize.numeric_accept = true` and 4 suggestions, `3` applies suggestion 3 and `7` produces one notify and zero RPCs; rows render the literal prefix `"3. "`. With the flag `false`, `3` is unbound in the organize pane and the `?` overlay contains no `1-9` row. With a view whose render inserts a reason line and a preview line above suggestion 3, `3` still applies **suggestion 3** — the item index, not the screen line.
10. **Capture pane stays clean.** `nvim_buf_get_keymap(capture_buf, "n")` contains none of `f`, `F`, `.`, `P`, `p`, `x`, `zi`, `1`…`9` under **every** value of `ui.capture_pane_keymaps` (`"core"`, `"navigation"`, `"none"`). Firing control: `zo` and `za` **are** unmapped in the capture buffer, so 15 §3's fold recovery works and no `z` prefix waits on `timeoutlen` there.
11. **Editing is never intercepted.** Enter the **merge** view, type text, then press each of `u`, `x`, `P`, `p`, `.`, `3`, `f`, `F`, `H` in turn — in every case the buffer is edited (or Vim-undone) exactly as native Vim would, and **zero** RPCs are issued. Repeat the whole sequence in the **integrate-edit** view (`editing = true`). Assert positively that `nvim_buf_get_keymap(organize_buf, "n")` contains none of those lhs values while `vim.bo[organize_buf].modifiable` is true, and contains all of them again after the view returns to `suggestions`. **Mutation: drop the `views` field from any one row ⇒ this test fails.**
12. **Overlay width follows the marker.** Render a suggestions view with `ui.icons` set to a **1-column** value, then press `f`: no row's name is covered by a label, every label is padded to the same width, and clearing `NS_HINT` leaves zero marker debris (the row bytes are identical to the pre-hint bytes). Repeat with the default 3-column `[P]` marker.

## 8. Test obligations

Per ARCHITECTURE "Test anti-vacuity standards (PERMANENT)", binding on every seat and verifier for this document:

1. **Refusal-predicate pins** — mutate the guard away and confirm red, asserting at a parameter where the other branch would fire, **and a firing control at an adjacent parameter where the operation must succeed** (a guard that refuses *everything* passes a guard-deleted test, which is what the firing control exists to catch). Required for: `.` with no previous destination; numeric accept with `N >` the item count; pin on a file destination with `mode = "move"`; `<leader>ba` over `ui.batch.max`; an unrecognised `session.start` `order`; `ui.hint.alphabet` shorter than 4 letters; an `ui.hint.alphabet` character with no distinct shifted form; `<leader>f` whose filter matches zero notes (the guard is *the current session survives*, so the pin must assert the old session is still installed, not merely that no error was raised).
2. **Constant assertions** — assert the **literal** value, never the imported module constant: the literal `"asdfghjklqwertyuiopzxcvbnm"`, the literal default `depth` of `1`, the literal default `order` of `"unseen_first"`, the literal `2` for `seen_scan_months`, the literal `10` for `mru_size`. Separately assert each constant agrees with its literal.
3. **Must-not-be-connected invariants** — pin the *connection*, not today's consequence. (a) `NS_HINT` is a distinct namespace: clearing it leaves `NS_ORGANIZE` and `NS_CAPTURE` extmark counts unchanged. (b) Hint mode never binds in the capture pane, and no single-character row is bound while the organize buffer is modifiable: assert the *absence* of the mappings (tests 10 and 11), not merely that nothing bad happened. (c) `pinned` and `batch_size` are absent from `context.filters` — a first-class-field test that only checks the value is present passes equally with the value smuggled into `filters`.
4. **Every new config leaf has a reader.** Core leaves: a reader, or an entry in `RESERVED_CONFIG_LEAVES` in `tests/test_config.py` with a reason. ⚠ Nvim leaves discharge 14 §4.3(d) through **14 §10.11**'s Lua mirror gate (`config.lua` SCHEMA leaf ⇒ reader) and Lua honored-key gate (default ⇒ behaviour A; non-default ⇒ behaviour B ≠ A), parametrised over SCHEMA — a leaf added only on the Lua side adds no Python leaf, so the core-side gate alone would let this document ship every new key untested. This document's rows for that count: `ui.organize.numeric_accept`, `ui.organize.show_progress`, `ui.organize.preview_debounce_ms`, `ui.organize.preview_notes`, `ui.batch.confirm`, `ui.batch.max`, `ui.hint.alphabet`, `ui.hint.case_sensitive`, `ui.hint.default_action`, `ui.hint.position`, `ui.hint.persist`, `ui.hint.unknown_key`, and the five `ui.highlights` additions — **17 leaves, 17 honored-key rows**, countable against the suite by a reviewer. Core rows: `throughput.mru_size`, `throughput.mru_order`, `session.order`, `session.seen_scan_months` — **4**.
5. **`EXTRA_METHODS` grows by exactly the granted names.** `test_rpc_methods_are_exactly_the_spec_10_names_in_order` must remain green after the `folder.list` and `session.start` param additions (they add no method name). `dest.recent` is **granted** and enters the list with its justification comment written in the existing house shape; the edit to `tests/test_server_protocol.py` is made **once**, by doc 17's seat, covering `dest.recent`, `op.undo` and `history.list` together. No other name is added by this document.
6. **CLI drift guard.** Every `organize …` invocation quoted in this document (`organize dest recent --json`, `organize session --order …`) is covered by a drift-guard test that executes the literal string, per the pattern established for the deep_research seat.
7. **Bytes-on-disk E2E.** The spec-09 §3 headless-nvim gate gains one hint-jump session and one 5-capture batch, asserting against bytes on disk — not against the plugin's own state table.
8. **One sorter, two surfaces.** A test asserts that the function object `F` ranks with **is** the one `pickers.lua` ranks with — both obtained from `require("telescope.config").values.generic_sorter({})` — so a user's `defaults.generic_sorter` override moves both or neither. Asserting only that "some sorter is used" would pass with the fzy sorter hardcoded, which is the drift this pins.
9. **Performance** (09 §4 budgets, `-m slow`): label draw ≤ 16 ms for 200 rows; `F` filter re-render ≤ 16 ms per keystroke over 5,000 cached folders; `dest.recent` < 50 ms; **`session.start` with `order = "unseen_first"` < 200 ms on the 13,252-note / 1,862-capture corpus with the default `seen_scan_months = 2`** — and a test that a corpus large enough to exceed it produces the loud fallback to `"oldest"` (one notify plus the `organize health` row), never a silent slow path; `folder.list depth="all"` < 200 ms cold on the 13,252-note index and never on the keypress path; no batch step blocks the editor > 50 ms.
10. **Mutation audit before handback**, seat standard: report the count of mutations introduced and killed for every pin above.
