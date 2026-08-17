# 19 — Merge Targets, Picker Providers and Session Control (NEW)

Directives from Matt (2026-08-16, second pass on the running system — quoted verbatim; the quotes are the requirement, this document is only its formalisation):

- **(A)** *"when trying to edit a file and save, i get `E382: Cannot write, 'buftype' option is set` when in the organize tab."*
- **(B)** *"also, there can be multiple files that i would want to merge the text into, so i should be able to merge into multiple files, by saving the file, going back (perhaps by using ctrl+o or another semantic for back like shift l), then going/finding another file."*
- **(C)** *"this might be too much, but it would be good if i can use my normal snacks based file pickers for the organize thing, so i can just find the specific file using the same semantics, and open it up in the Organize buffer and have this program learn that i wanted to place that info there."*
- **(D)** *"btw, i want to be able to change the order at which i go through my captures (for example, earliest first) or just be able to jump to different times, this is because i have a lot of captures right now"*
- **(E)** *"or, for example, to be able to filter through my captures for a specific tag"*
- **(F)** *"currently, when i press 'enter' on archive, that should be semantically the same as pressing 'a'."*
- **(G)** *"also when editing the buffer on the right, i should be able to go into visual mode and have all my commands that i typically have available"*
- **(I)** *"there is this behavior where sometimes i have many duplicated or nearly duplicated captures quickly after each other. please make a command `ParaOrganize dedup` that solves this problem, asking me to review before removing the duplicates"* — **specified in doc 20, not here.**

Item **(H)** (a tag `eduardo-pontes-reis` with an existing note for Eduardo that is never suggested) is a defect in candidate enumeration, diagnosed and specified in **the suggestion-fix doc** — the amendment to 04 §1's *"candidates are every immediate subfolder"*. This document depends on that fix in exactly two places (§3's note picker and §4's `order = "score"`) and restates none of it.

**Two claims this document makes up front, because they are the cheapest thing about it.** It adds **no RPC method** — `EXTRA_METHODS` is unchanged — and it claims **no new keystroke**: one existing binding (`<BS>`) has its meaning widened, and everything else rides `m`, `<CR>`, `a`, `:w`, `<leader>f` and the `key=value` filter grammar that 03 §2 already defines. At a 1,862-capture backlog (`doc/FEEDBACK-EVIDENCE-2026-08-16.md` §4) an ambiguous new key costs 1,862 hesitations (16 §5); this document pays none of them.

House law binding everything below: doc 10 §1's thin-client rule with 10 §4's real-buffer exception; doc 14 governs — every behaviour ships with a default, a config key, a documented meaning and a test that the key is honored (14 §4.3), and every knob's side is decided by 14 §4.1's one question. Docs 15/16/17/18 are **cited, never duplicated**.

---

## 1. Save semantics in the organize pane (item A) — landed in `fd0b3a9`, recorded here as law

`:w` is the key every Vim user reaches for when they mean *"apply what I just wrote"*. A pane that presents editable text and then answers `E382` is not a pane the user can trust.

### 1.1 The law

1. **`buftype` is a function of the VIEW, never of what the text looks like.** The organize pane is `acwrite` **exactly while it holds editable text** — the merge editor (03 §5) and the 12 §1 review gate's edit mode (`state.integrate.editing == true`) — and `nofile` in every other view (suggestions, browse, search, MRU (16 §3.5), history (17 §5)). Dispatching on the view rather than on the content is the 08 §A19 discipline.
2. **`:w` in an editable view commits through the CORE.** A **buffer-scoped** `BufWriteCmd` calls `actions.merge_complete()`, which splits on the view: `op.merge_commit` in `merge`, the review gate's accept verdict in `integrate`. The client still writes no vault byte — 10 §1 is intact, and this is not a second instance of 10 §4's exception. `:x`, `ZZ` and `:w!` route through the same command by construction.
3. **The autocmd is buffer-scoped or it is a bug.** A `BufWriteCmd` registered with `buffer = nil` hijacks `:w` for **every buffer in the editor**. This is pinned, not assumed (§10 obligation 3a).
4. **`:w` outside an editable view is neither an error nor a no-op.** One INFO line: *"nothing to save here — this pane is a view of the vault, not a file. Press ? for the keys that change it."* Silence would read as a failed write.
5. **Every render clears `modified`.** An `acwrite` buffer that believes it is modified blocks `:q` with `E37` over lines the *plugin* wrote, which is never unsaved user work.
6. **`E382` is never reachable again from this pane.** It is not a configurable outcome. `ui.write_action = "commit" | "notify"` (default `"commit"`) decides what `:w` *does*; with `"notify"` the pane is still `acwrite` and `:w` explains that `<leader>mc` commits. No value of any key restores the error.

### 1.2 The invariant: no rendered artifact may ever reach the vault

The organize pane's bytes can now be committed to a real note. Therefore **everything the plugin draws into that pane must not be buffer text.**

- Compliant mechanisms, all already in use: extmark `virt_text` / `virt_lines` (15 §3's field card, 16 §1's hint labels, `render.merge_hint`'s instruction line), `foldtext`, the winbar, the nui border title (16 §3.2's `PINNED` banner).
- Forbidden in an editable view: `nvim_buf_set_lines` of any label, header, separator, hint, score, reason, marker or instruction that the user did not type. 03 §5.1 already says this for the merge editor's instructions (*"virtual text / winbar only — never as buffer lines that could be saved"*); this generalises it to every decoration any document adds.
- The invariant is **load-bearing, not defensive**: `merge_complete` sends the buffer's lines verbatim as `edited_content`, and `merge_into_note` treats a leading frontmatter block in that content as the base fields. A leaked decoration is written to the vault; a leaked decoration that *looks like* frontmatter rewrites the target's frontmatter.
- The one deliberate exception is machine bookkeeping the core itself writes, never the client, never into the buffer: §2.4's merge marker.

### 1.3 Acceptance criteria

1. In the merge view, `vim.bo[organize_buf].buftype == "acwrite"`; `:w` issues **exactly one** `op.merge_commit` and the client writes zero bytes to disk. In the suggestions view `buftype == "nofile"`, the buffer is unmodifiable, and `:w` produces exactly one INFO notification and zero RPCs.
2. `:w` in an unrelated buffer opened in another window during a session behaves natively — the plugin's `BufWriteCmd` does not fire.
3. With every optional decoration enabled (15's field card, 16's hint labels drawn and cleared, scores, reasons, the merge hint), commit a merge and assert the target file's bytes contain **none** of the decoration strings, and `nvim_buf_line_count` before/after drawing them is unchanged.
4. `ui.write_action = "notify"`: `:w` in the merge view issues zero RPCs and one notification whose text names `<leader>mc`; `buftype` is still `acwrite`.

---

## 2. Multi-target merge (item B)

**This is the load-bearing section of the document.**

### 2.1 Why a second merge is impossible today

`fileops.merge_into_note` (`fileops.py:1714`) writes the target **and then archives the capture** (05 §4: *"Atomic write, archive capture, log `merge`, reindex target"*; 03 §6: *"Merge | Target updated, capture archived"*). `actions.merge_complete` then calls `mark("processed", …)`, `M.advance()` and `retire_capture_buffer(…)`. So after one merge the capture is in the archive tree, out of the session, and its buffer is gone. Matt's *"multiple files that i would want to merge the text into"* is not hard today — it is **impossible by construction**.

### 2.2 The amendment: merge writes the target; something else decides the capture's fate

**A merge no longer archives the capture, and no longer ends the capture.** The two decisions it conflated — *where does this content go* and *am I done with this capture* — become two acts.

| | Old (03 §6, 05 §4) | New |
|---|---|---|
| Target | written, backed up, tags/sources union, `last_edited_date` | **unchanged** |
| Capture file | archived | **left in place** |
| Session | marked processed, auto-advance, buffer retired | **stays on the capture**; the right pane returns to `merge.previous_view` |
| Learning | `record_move` with the target's folder | **unchanged**, once per merge that actually wrote (§2.6) |

**This is a deliberate amendment to 03 §6's Merge row and 05 §4's final step, and both must be edited to match** — a code change without the spec edit silently forks the law.

**Parity escape, per 14 §4.4's "a default that changes ships with the old value written out":** core config `[file_ops] merge_archives_capture` (bool, default **`false`**). Set `true` and merge behaves exactly as 03 §6 specified — archive, mark processed, advance. Per-invocation override wins (14 §4.4 precedence): `op.merge_commit(archive_capture: true | false | null)` where `null` means *use the config*, and `organize merge <note> <target> [--archive | --no-archive]`.

**Core side, not client side** (14 §4.1's one question): it changes which bytes exist where, so the CLI and the UI must never disagree.

**A named safety consequence.** `merge_into_note`'s worst partial-apply — *"the target has ALREADY been rewritten in place"* when archiving the capture fails — **does not exist** under the default. There is no archive step to fail after the write.

### 2.3 When the capture is finally archived, and by which explicit act

Only ever by an act the user took *about the capture*, never as a side effect of a merge:

| Act | Effect |
|---|---|
| `a`, or `<CR>` on the archive row (§6.1), or `:ParaOrganize archive` | 05 §3 archive. **This is the finisher.** Learning: none (03 §6) — so finishing is learning-neutral, which is exactly why it is safe to separate it. |
| `<CR>` on a folder suggestion / `:ParaOrganize move <path>` | 05 §2 move: the capture is filed as its own note *and* archived. A capture merged into three notes may still deserve to exist somewhere. |
| `s` skip | Stays `raw`, stays in the backlog, trail intact. |
| `<Tab>` / `<S-Tab>` | Advance without deciding. |
| session end / `:ParaOrganize stop` | Nothing. **There is no implicit archive-on-leave** — that is the side effect this section exists to remove. |

**The nudge, at zero new surface.** After ≥1 merge, the synthetic *"Archive Now"* row that 04 §1 already puts in every suggestion list gains one reason line: `3 merges recorded — archiving finishes this capture`. No new row, no new key, no new view.

**The honest cost, recorded rather than hidden.** A merged-but-unarchived capture is still `processing_status: raw`, so it returns in the next session. That is *true* — it has not been filed — and it is mitigated, not papered over: 16 §3.7's `order = "unseen_first"` sorts it behind every never-shown capture, and its header shows the trail (§2.5) so one `a` finishes it. The rejected alternative is §7's auto-archive-on-leave.

### 2.4 The duplicate-merge guard — CRITICAL-1 applied to merge

Merging the same capture into the same target twice must not silently append twice. Per the **CRITICAL-1 vault-as-truth ruling** (`ARCHITECTURE.md:1559`), *"the 'already delivered' truth lives in the VAULT, not the store."*

**The marker.** Every merge writes one machine-owned, namespaced HTML comment into the target, in the same atomic write:

```
<!-- organize:merged capture_id=<id> -->
```

- **Machine-owned.** The core writes it; the client never does; it is **not** present in the `merge_preview` seed's newly-added block and the user is never asked to preserve it. Markers live in one block at the end of the target body, one line per delivered capture, in delivery order.
- **Self-healing.** On every write the core strips every `organize:merged` line from the edited body and re-appends the union of *(markers found in the target's current bytes)* ∪ *(this capture's marker)*. A user who deletes one mid-edit gets it back; bookkeeping is not the user's job.
- **Delivered check = exact match on the literal `organize:merged capture_id=<id>`** in the target's current bytes.
  - **Never the rendered `## Merged from <file> on <%Y-%m-%d %H:%M>` header** — the timestamp drifts between attempts, so matching on it duplicates.
  - **Never the bare capture id** — a `[[<capture-id>]]` wikilink in the target false-positives, and a false positive here means **SILENT NON-DELIVERY**.
- **`<id>`** is `frontmatter.id`, else `capture_id`, else `sha256(<vault-relative path>)[:16]`. The fallback chain is required: only 739 of 1,000 sampled captures carry `id` (evidence §2). Recorded limitation: a capture that falls to the third form and is then *renamed* loses its delivered-truth.

**Behaviour on a hit.** No write, no backup, no marker, `ok = true`, `details["already_merged"] = true`, and the operation-log line and ActionRecord list the targets **actually written this run** with the skip visible in the record metadata — CRITICAL-1's *record honesty*, never a silent skip. **The learner is not taught** (§2.6). The client shows one line: `already merged into <target>`.

**The override is per-invocation and is never a config key.** `op.merge_commit(allow_duplicate: bool = false)` / `organize merge … --again`. A *configured* `allow_duplicate = true` would turn every crash-retry into a duplicated block — a knob whose wrong value silently duplicates vault content is not a knob. The only client-side knob is `ui.confirm_duplicate_merge` (default `true`): with it on, an `already_merged` reply prompts *"merge again into `<target>`? a second block will be appended"* and a yes re-issues with `allow_duplicate = true`; with it off the duplicate is skipped with one notify and **never appended**. Every value of every key is safe.

**The payoff of vault-as-truth.** Undoing a merge (17 §1's restore-or-refuse) restores the target's pre-merge bytes, which removes that capture's marker for free — so the target is automatically re-open to a fresh merge, with no store to reconcile.

**Required edit to 17 §1's `merge` row.** Under the new default there is no archived capture to restore, so undo is the *simpler* half only: restore the target iff its current bytes still hash to `targets[].after_hash`, else refuse. The two-half rule stays for `merge_archives_capture = true`. `file_ops.create_backups = false` still makes merge non-undoable, and `organize health` still warns.

### 2.5 Back-navigation, and the "merged into" trail

**Matt asked for `<C-o>` "or another semantic for back like shift l". The answer is that the back key already exists — `<BS>` — and what was actually missing is that a merge ended the capture (§2.2).** With that fixed, his described loop is: `m` → pick a file → edit → `:w` → the pane returns to where he was → `<BS>` to the parent → `f`/`/` to the next file. Every key in it is already bound.

`<BS>` (`keymaps.buffer.back`, default unchanged) has its **meaning widened** from *"back to parent while browsing"* (03 §3, 16 §6) to **"back one step in the organize pane's view stack"**. A client-local `state.view_stack` of `{view, browse, selected}` snapshots is pushed when descending a folder, entering search results, entering the MRU view (16 §3.5), entering the history view (17 §5) or entering the merge editor; popped by `<BS>`. In a browse tree the view stack *is* the directory stack, so today's behaviour is **byte-identical** — this is a strict superset. The stack is **cleared on capture advance**: a stack that survived would take `<BS>` back into a folder browsed for a different note. Empty stack ⇒ return to the suggestions view; already there ⇒ one notify, no state change (a pinned refusal predicate).

Rejected, with reasons, because Matt named them:

- **`<C-o>`** — it is Vim's jumplist. The capture pane is a real editable file (10 §4), so binding it there is the paste-shadowing hazard of 16 §"The capture pane is a real file buffer"; binding it only in the organize pane gives one key two different back-semantics in two adjacent panes.
- **`L` / `<S-l>`** — `H`/`M`/`L` are screen positioning, `H` is already claimed by 17's history view, and `L` reads as *forward* to a Vim user. A binding table can prove no collision of *keys*; this is a collision of *meaning*, which only reading catches.
- **A forward key.** Deliberately omitted. Re-descending costs ≤3 keys via `f`/`F`/`/` (16 §1–2), and `<S-BS>`/`<C-i>` are respectively unreliable in terminals and the jumplist again.

**The trail.** Two representations, one truth:

- **Delivered truth = the marker in each target** (§2.4). Survives corpus loss and is what the guard reads.
- **Display truth = the action corpus** — `ActionRecord`s with `operation = "merge"` for this capture. `note.get` gains an additive result field `merges: [{target, ts, action_id}]` (additive field, no new method, the same class as 16 §3.8's `note_count`), and `op.merge_commit`'s result carries the updated `details["merges"]` so the client refreshes with no extra round-trip.
- **Rejected: a `merged_into` list in the capture's own frontmatter.** A third copy that can drift, written into a note the user is editing in a live buffer mid-session — exactly the hazard 17 §1's `meta_edit` row exists to avoid.

Rendering (`ui.show_merge_trail`, default `true`, nvim side — it is only how something looks):

- Capture pane: one line in 15 §3's virtual field card — `merged into: areas/relationships/eduardo-pontes-reis.md +2`.
- Organize pane: an already-merged target, wherever it appears (browse listing, suggestion, picker line), carries an **end-of-line** `virt_text` `✓ merged` in `ui.highlights.merged`. End-of-line, not overlay, because 16 §1 owns column 0. Never buffer text (§1.2).
- 16 §3.7's progress line gains `· k merged` only while `k > 0`; a merge does **not** increment `done`, because the capture has not left the queue.

### 2.6 What each merge records, and what it teaches

**One `ActionRecord` per target.** 12 §2's `targets[]` is one entry per file touched *within one action*; N merges at N different moments are N actions. So: N records, each with a single `targets[]` entry, `role: "merge_target"`, its own before/after hash and diff. This is the same reasoning 16 §3.3 gives for batches (*"N ordinary `ActionRecord`s … so it is N undo units with no new reversal machinery"*), and it is what makes merge 2 of 3 independently undoable.

Per record: `operation: "merge"`, `edit_mode: "manual"`, `capture.body_before` in full, `context.chosen_rank` = the rank of the **target's folder** in the shown suggestions or `null` (03 §6: merge learns with the target's folder), `details["merge_index"]` = 1-based position in this capture's trail, `details["already_merged"]` when the guard fired.

Learning (04 §3 `record_move`, via 12 §2's single write path):

- **Every merge that actually WROTE teaches exactly once**, destination = the target's folder. N merges ⇒ N `record_move` calls ⇒ the same association key gains N destinations, `statistics.total_moves += N`. This is correct and is the signal Matt wants: *a capture like this belongs in all of these places*, which is precisely what makes the second and third destination surface unprompted next time.
- **A merge skipped by the duplicate guard teaches nothing.** No write, no new decision. This is the learning-side half of CRITICAL-1's exactly-once, and it is what stops a retry loop from inflating `learning.json`.
- **A merge with `partial_failure` is excluded from learning** and from the accept-rate corpus — the standing Phase-4 ruling, unchanged.
- **The archive that finishes the capture teaches nothing** (03 §6).

### 2.7 Acceptance criteria

1. **Two targets, one capture.** Merge the capture whose sources include `eduardo-pontes-reis` into `areas/relationships/eduardo-pontes-reis.md`, `:w`, then `<BS>` and merge into `areas/relationships/relationship-data/eduardo-pontes-reis/eduardo-pontes-reis.md`. Both files contain the capture body exactly once; the capture is still at its original path with `processing_status: raw`; the session index is unchanged; **two** ActionRecords exist; `learning.json` gained **two** destinations under one association key and `total_moves == 2`.
2. **The finisher.** Pressing `a` then archives the capture under `archive_capture_path` **keeping its filename** (05 §3), writes no learning, and advances.
3. **Duplicate guard.** Re-merging into target 1 with `allow_duplicate = false`: the target's bytes are **byte-identical**, `details["already_merged"] == true`, zero backups created, `total_moves` still `2`, and the record shows the skip. With `--again` a second block is appended and a **second** marker line for the same capture id is present exactly once each.
4. **Marker discipline.** Deleting the marker block by hand in the merge editor and committing restores it. A target containing `[[<capture-id>]]` as a wikilink but no marker **receives** the merge (no false positive). A target whose `## Merged from …` header was hand-edited to a different date still blocks a duplicate.
5. **Parity mode.** `merge_archives_capture = true` reproduces 03 §6 exactly: capture archived, marked processed, session advanced.
6. **Undo of merge 2 of 3** restores target 2's bytes, removes its marker, leaves targets 1 and 3 untouched, and a subsequent re-merge into target 2 is accepted.

---

## 3. Picker providers (item C)

Matt wants to drive destination selection with **his own picker** — snacks.nvim. A seam, not a snacks integration: telescope ships (03 §1), fzf-lua, mini.pick, `vim.ui.select` and an arbitrary function are all first-class. Doc 14 §5's no-fork test in one line: *choosing where a capture goes with your own picker must not require editing plugin source.*

### 3.1 The contract

```lua
pickers = {
  provider = nil,   -- nil = auto-detect | "snacks"|"telescope"|"fzf_lua"|"mini_pick"|"select"
                    -- | fun(request, done)
  provider_order = { "snacks", "telescope", "fzf_lua", "mini_pick", "select" },
  merge_flow = "flat",   -- "flat" (one note picker) | "drilldown" (03 §5's folder → note)
}
```

The provider is called as `provider(request, done)`.

**`request`** — a table the provider must not mutate:

| Field | Meaning |
|---|---|
| `kind` | `"folder" \| "note" \| "note_in_folder" \| "filter_value" \| "saved_search"` — which of 03 §4's pickers is being served |
| `prompt` | Title string |
| `items` | Array of `{ path, display, kind = "folder"\|"note", para_type, score?, reasons?, merged? }`. **Already fetched from the core** — the provider never reads the vault (10 §1). `display` is the pre-formatted line (`pickers.format_folder` / `format_note`), so a string-only picker uses `display` and a richer one composes from the fields. |
| `multi` | Whether multi-select is meaningful for this request |
| `capture` | The current capture record, or `nil` — so a provider can preview |

**`done(choice)`** — the provider **must** call it, and may usefully call it once:

- `done(nil)` ⇒ aborted. Zero RPCs, zero state change, **no notification** — an abort is not an event.
- `done(entry)` ⇒ one choice. `done({e1, e2, …})` ⇒ multi-select.
- **`done` also accepts a bare path string**, which is what a general-purpose file picker (`snacks.picker.files()`) hands back. This is deliberate: item C is *"just find the specific file using the same semantics"*, and a picker that globs the filesystem does not know about `request.items`.
- **`done` is idempotent by construction**: the second and later calls are ignored with one debug-level message. A provider calling `done` twice issues exactly one RPC.
- Anything else (a number, a table that is neither an entry nor an array of them) ⇒ one WARN, zero RPCs.

**The safety rule, and why it is not "the path must be in `items`".** Every path returned is **validated by the core** before anything happens — never against the client's cached list. The core resolves it (`require_in_vault`, then note-or-folder), and a path outside the vault, or one that does not exist, is refused with one notify and zero writes. Validating client-side would be both weaker (a cache can be stale) and narrower (it would forbid the file picker Matt asked for). Thin-client law, applied: **the client does not decide what is a legal destination.**

### 3.2 Auto-detection, and what it does not touch

With `provider = nil`, the first entry of `provider_order` whose module `require`s successfully wins. The default order puts **snacks first** (a user who installed snacks wanted snacks) and **`select` last** — `vim.ui.select` is always present, so the chain can never fall through to nothing. `:checkhealth` reports the resolved provider by name.

**The seam does not touch `f` / `F` (16 §1–2).** Hint mode is not a picker: it never opens a floating window and never leaves the two-pane layout, and 16 §5 makes that its entire advantage. Routing it through a third-party picker would delete the feature. `F`'s matcher likewise stays telescope's own sorter (or 16 §2's documented fallback) regardless of the provider — one ranking, one muscle memory.

### 3.3 What opening a FILE does versus a FOLDER

Resolved by **what the path is, per the core** — never by a trailing `.md` in the client, because a folder may legally be named `foo.md` (the suggestion-fix doc's `kind` discriminator rule, applied here).

- **A FOLDER ⇒ the move flow.** `op.move`, applied immediately (`ui.picker_accepts_immediately`, default `true`): `:ParaOrganize move <path>` applies, and a picker is the interactive form of the same command; a wrong destination is fully undoable (17 §1). Set `false` to make the picker *select* the folder and leave `<CR>` to accept.
- **A FILE ⇒ the merge flow.** The organize pane becomes the merge editor seeded by `op.merge_preview` on that target — identical to `<CR>` on an `[F]` line (03 §5). This is Matt's *"open it up in the Organize buffer"* literally, and §2.6's `record_move` is the *"have this program learn that i wanted to place that info there"* half.
- A file **outside** a PARA root is a legal merge target if it is in the vault (a capture merging into another capture is a real thing). The only refusal is `no-ai` (already enforced in `merge_into_note`, both directions).

**`m` opens one flat note picker.** `pickers.merge_flow = "flat"` (default) opens a single picker over every note under a non-archive PARA root — 8,077 on the real vault, fetched once per session and invalidated by `index.reindex`, exactly like 16 §2's folder cache. `"drilldown"` restores 03 §5/§24's folder → note two-step, and the `select` backend uses the drilldown regardless (a flat `vim.ui.select` over 8,077 rows is not a picker). **This is an amendment to 03 §24's *"Enter merge mode via pickers (folder → note)"* and 03 §5's entry points; both must be edited.** Rationale: a fuzzy picker over 8k items is one query, a drilldown is two, and Matt's request is exactly *"the same semantics"* as his file picker.

### 3.4 Multi-select → batch routing

| Selection | Result |
|---|---|
| **Several FILES** | **Several merges — item B in one gesture.** Applied **sequentially through the ordinary per-capture merge path**, never a new bulk primitive (16 §3.3's rule): one `op.merge_commit`, one ActionRecord, one `record_move` per target, the §2.4 guard per target. Stops at the first failure, reports `merged k of n`, names the un-attempted remainder. Because no editor can be open for three targets at once, a multi-select merge uses **05 §4's deterministic body template** — which is byte-identical to what the editor is seeded with, so the two paths cannot diverge. `ui.multi_merge_confirm` (default `true`) shows the literal count and the target list first. A user who wants to edit each one picks them one at a time; the message says so. |
| **Several FOLDERS** | **Refused**, one notify. A move archives the original (05 §1.2), so "move to two folders" has no meaning. The message names the two real alternatives: merge into a note in each, or move to one and merge into the other. |
| **Mixed** | Refused with the same message. |

### 3.5 Recording — a provider choice is not a second-class choice

Anything chosen through a provider goes through the **same** `actions.*` → `op.*` path, the same ActionRecord, the same `record_move`. Presentation changed; nothing else did.

Two fields differ, both for stated reasons:

- **`context.chosen_rank = null`.** The choice was not made from the ranked list — the identical ruling to 16 §3.2's pinned accept, and for the identical reason: recording `1` would teach the ranker it was right when it was never consulted.
- **`ActionContext` gains `picker: string | null`** (`"snacks"`, `"telescope"`, `"custom"`, …) as a **first-class field, never a `filters["picker"]` entry** — the `dry_run` / `partial_failure` / `pinned` / `batch_size` / `keystrokes` precedent. It earns its place by being falsifiable, which is the bar 16 §3.9 set: `organize actions stats` must be able to answer whether provider-chosen destinations have a different undo rate than ranked ones. If they do, the ranker is failing on exactly the queries people reach past it to answer.

### 3.6 Degradation

| Failure | Behaviour |
|---|---|
| Module missing at detect time | Skip silently to the next entry in `provider_order`. Auto-detection is allowed to not find things. |
| Provider raises (`pcall` fails) | One WARN naming the provider and the error's first line; fall through to the next provider, ultimately to `vim.ui.select`, which always exists. This generalises `pickers.select`'s existing telescope→`vim.ui.select` fallback to the whole chain. |
| Provider keeps raising | The failure is remembered for the session (`pickers._degraded[name]`), so the same stack trace does not print 1,862 times. |
| Provider never calls `done` | **Nothing happens. No timeout, no speculative fallback.** A timeout firing while the user is still typing would open a second picker on top of the first; a speculative fallback would double-apply when the real `done` arrived late. |

### 3.7 Acceptance criteria

1. With `provider = "snacks"` stubbed by a Lua function, `m` calls it once with `kind = "note"`, a non-empty `items`, and `capture` set; `done(items[3])` opens the merge editor on `items[3].path` and issues exactly one `op.merge_preview`.
2. `done("<vault>/areas/relationships/eduardo-pontes-reis.md")` — a bare string never present in `items` — opens the merge flow. `done("/etc/passwd")` produces one notify and **zero** RPCs.
3. `done({a, b, c})` with three notes writes three targets, three ActionRecords and three learning associations, in order, with one confirmation shown containing the literal count `3`. `done({folderA, folderB})` produces one notify and zero writes.
4. A provider that raises falls through to the next and finally to `vim.ui.select`; a provider that raises 50 times prints one WARN. A provider that calls `done` twice issues one RPC.
5. Every provider-driven operation's ActionRecord carries `picker` at top level of `context` and `chosen_rank == null`, and `context.filters` contains **no** `picker` key.
6. `f` and `F` behave identically under every `provider` value, and open no floating window.

---

## 4. Session order and time jump (item D)

*"i want to be able to change the order at which i go through my captures (for example, earliest first) or just be able to jump to different times, this is because i have a lot of captures right now"*

**16 §3.7 already owns the knob**: `session.start` takes `order`, core config `[session] order` defaults to `"unseen_first"`, `--order` overrides, an unrecognised value is a loud `-32602`, an empty corpus falls back silently to `"oldest"`. This section **extends the enum and adds the mid-session path**; it restates none of that.

### 4.1 The orderings

| `order` | Meaning | Owner |
|---|---|---|
| `"oldest"` | Oldest first by `timestamp`, falling back to file mtime, tiebreak path ascending (03 §2). Matt's *"earliest first"*. | 16 §3.7 |
| `"newest"` | The reverse. | 16 §3.7 |
| `"unseen_first"` | Never-shown before previously-skipped; oldest-first within each group. **Default.** | 16 §3.7 |
| `"random"` | **New.** Shuffled. | this doc |
| `"score"` | **New.** Descending by the top suggestion's score — the easy wins first. | this doc |

**`"random"` is seeded, because 03 §2 requires a deterministic order.** The seed is derived from the `session_id`, and `session.start`'s result gains additive `order` and `seed` fields so the same queue is reproducible from the CLI (`organize session --order random --seed <s>`) and pinnable in a test. An unseeded shuffle would make the acceptance test unwritable, which is how it would go unwritten.

**`"score"` costs one `suggest()` per capture, and that cost is the specification.** Measured (the item-H diagnosis): 1.30 ms per call against today's 135-candidate set, 88.7 ms against the widened one. Over 1,862 captures that is 2.4 s versus **165 s**. Therefore:

- `"score"` computes in a **background pass** that re-orders only the **not-yet-shown tail** — never the capture in front of the user, which would move under his cursor. Progress is reported over `events.subscribe`; the session is usable in `"oldest"` order the whole time.
- The pass is bounded by `[session] order_budget_ms` (default `5000`). On exceeding it the pass **stops**, the remaining tail keeps `"oldest"` order, and one honest line says so. A refusal predicate to pin, at a parameter where the other branch fires.
- **`"score"` is only worth choosing once the suggestion-fix doc's performance work lands** (dict-served exact-equality signals, a length/prefix prefilter before the per-alias Levenshtein). Until then it is honest but slow, and the budget makes it safe rather than good.

**One enum, not a cross-product.** `order` stays a single value. Rejected: a second `group_by` knob to get "newest-first within unseen-first" — a 2-D knob for a 1-D decision, when 16 §3.7's default already encodes the only grouping the corpus supports.

### 4.2 Changing order mid-session without losing progress

**No new key.** `order=` joins the `key=value` vocabulary of 16 §3.6's `<leader>f` prompt and of `:ParaOrganize start`. 16 §3.6's rule holds verbatim — *"No second filter vocabulary is invented"* — and the guard holds verbatim: the new session is installed **only** after the core returns a non-empty list; zero matches ⇒ notify and keep the current session intact.

Two rules this document adds to 16 §3.6, because a re-filter that silently dropped a criterion is worse than one that refuses:

1. **The prompt is seeded with the session's current filter-and-order string**, so re-filtering **composes** rather than replaces. Typing `order=newest` into a session filtered `tags=idea` yields `tags=idea order=newest`, not a vault-wide newest-first queue.
2. **An empty value clears exactly that key.** `tags=` removes the tag filter and nothing else.

Progress is preserved without any new state, exactly as 16 §3.7 argues: it is **derived**. Organized captures have left `status=raw` on their own, skipped captures carry an `op.skip` record, and merged-but-unarchived captures carry `merge` records — so the re-issued session lands on the first genuinely unprocessed capture in the new order. The processed/skipped counts carry forward per 16 §3.6.

### 4.3 The time jump

Reaching *"different times"* is the same grammar, widened — in the **CORE**, because it decides which captures a session contains and `organize session` must agree with the UI (14 §4.1). **The client never parses a date**; it passes the literal string through.

| Form | Accepted by | Means |
|---|---|---|
| `YYYY-MM-DD` | `since=`, `until_date=` | today's behaviour, unchanged |
| `YYYY-MM` | `since=`, `until_date=` | first / last day of that month |
| `YYYY` | `since=`, `until_date=` | first / last day of that year |
| `30d`, `3w`, `6mo`, `1y` | `since=`, `until_date=` | that long ago, relative to now |
| `at=<period>` | `at=` | sugar for a bounded window: `at=2025-11` ⇒ `since=2025-11-01 until_date=2025-11-30`; `at=2025` ⇒ that year; `at=2025-11-07` ⇒ that day |

`at=2025-11 order=newest` is the whole of *"jump to different times"* in one prompt. An unparseable period is a loud `-32602` naming every accepted form, and — per §4.2 — **the current session survives**.

### 4.4 The already-fixed defect this inherits — `c85ddf5`

`:ParaOrganize since=2025-01-01` answered with **12,873 of 13,252 indexed notes**: the whole vault, already-filed notes included and offered for filing again. Cause: 03 §2's *"Defaults when no filters: `status=raw`, restricted to the capture folder"* was read literally, so any supplied criterion replaced the defaults wholesale. Fixed in **`c85ddf5`** by `session.narrow_to_backlog`: a session filter **narrows the capture backlog and never widens it to the vault**; an explicit `status=` / `para_type=` still wins; the vault-wide surface remains `organize search`. Verified live: `since=2025-01-01` now answers 1,849.

**Every period form added above inherits that law**, and each ships with a test asserting its result is a **subset of the unfiltered backlog** — not merely non-empty, and not merely a subset of the vault. That distinction is the entire defect.

### 4.5 Acceptance criteria

1. `order=oldest` over a fixture reproduces 03 §2's order exactly (timestamp, then path ascending). `order=newest` is its exact reverse.
2. `order=random` with a pinned seed produces a byte-identical queue across two runs and a different one for a different seed; the seed appears in `session.start`'s result and in `organize session --json`.
3. `order=score` over 200 captures with a stubbed 200 ms-per-call ranker hits `order_budget_ms`, leaves the tail in `oldest` order, notifies once, and **never reorders the capture currently displayed**.
4. Mid-session `<leader>f` with `order=newest` in a session filtered `tags=idea` yields a queue that is still tag-filtered; the processed/skipped counts carry forward; a filter matching zero captures leaves the **old session installed** (asserted directly, not merely "no error raised").
5. `at=2025-11` returns exactly the captures whose timestamp falls in November 2025, and that set is a **subset** of the unfiltered backlog. `at=2025-13` raises `-32602` naming the accepted forms and leaves the session intact.
6. `since=30d` and `since=<the same date spelled YYYY-MM-DD>` return identical sets.

---

## 5. Interactive tag filtering (item E)

*"or, for example, to be able to filter through my captures for a specific tag"*

**This already works at session start.** The core accepts `tags` / `sources` / `modalities` / `status` / `since` / `until_date` / `text` (03 §2), so `:ParaOrganize tags=idea` files a session today, and `c85ddf5` (§4.4) is what makes it answer captures instead of the vault.

**The interactive version is 16 §3.6's `<leader>f`, and this document adds nothing to it but §4.2's two composition rules.** Cited, not duplicated: the prompt vocabulary is 03 §2's, the nine saved searches (03 §4) are offered as presets, the new session is installed only after a non-empty result, the counts carry forward, and the wire is the existing `session.start` — no new method.

Two clarifications that belong here rather than in 16, because they are about tags specifically:

1. **Tag values are offered, not typed blind.** The `filter_value` picker kind (§3.1) serves `tags=` from `meta.values`, so a provider — snacks included — completes over the tags that actually exist in the vault. A tag filter for a tag nobody uses is a zero-match session, and 16 §3.6's guard then correctly refuses to install it; completing is how the user avoids discovering that the hard way.
2. **Filtering and ordering compose in one prompt** (§4.2): `tags=idea order=newest at=2025-11` is one `<leader>f`.

**Acceptance criteria.** `:ParaOrganize tags=idea` and a mid-session `<leader>f` with `tags=idea` produce the **same capture list** for the same vault state; a tag with no captures leaves the running session installed; the `filter_value` picker for `tags` returns exactly `meta.values("tags")` and issues one RPC.

---

## 6. Archive and editable-pane rules (items F and G) — landed in `17611aa`, recorded here as law

### 6.1 `<CR>` on the archive row IS `a`

*"currently, when i press 'enter' on archive, that should be semantically the same as pressing 'a'."*

`<CR>` on that row used to issue `op.move` into the archive **root**, which is a different operation from archiving:

| | `op.archive` (what `a` does) | `op.move` into `archives/` |
|---|---|---|
| Destination | `<archives>/<archive_capture_path>/<filename>` (05 §3) | `<archives>/<filename>` |
| Filename | **kept** — `[[wikilinks]]` survive (05 §3) | kept |
| Frontmatter | untouched | gains an `archive/<folder>` tag, `processing_status: organized`, `last_edited_date` (05 §2.6) |
| Learning | none (03 §6) | `record_move` fires |
| Corpus | the 12 §2 negative label | a positive filing decision that never happened |

**The law, stated generally so it binds every future synthetic row: one visible choice is one operation, whichever key expressed it.** A row's `kind` decides what happens; the key that reached the row never does. `actions.is_archive_suggestion` is the discriminator, and `<CR>` on such a row calls `M.archive()`.

### 6.2 An editable organize pane keeps only `<leader>`-prefixed exits

*"also when editing the buffer on the right, i should be able to go into visual mode and have all my commands that i typically have available"*

While the merge editor was open the pane was modifiable and still carried every action keymap, so the keys a person edits with did something else entirely: `s` substitute, `a` append, `p` paste, `r` replace, `S` change-line, `/` and `?` search — **`v` was taken by a metadata field, so visual mode never reached Vim** (item G, exactly) — and `<Esc>` closed the whole session mid-merge.

**The law:**

1. **While the organize pane is modifiable, no single-key binding is live in it.** The surviving set is a **whitelist**, `actions.EDITABLE_KEEP = { merge_complete, merge_cancel }` — both `<leader>`-prefixed, so they shadow nothing — plus `:w` via §1's `BufWriteCmd`. Three ways out, none of which costs an editing command.
2. **A whitelist, not a blacklist, and that is the point.** Every single key any other document adds — `f`, `F`, `.`, `P`, `x`, `1`–`9` (16), `u`, `U`, `H` (17), `zi` (15), `A` (13), `e` (12), and every `metadata_fields` letter (07) — is automatically absent in an editable pane, with no edit to this list and no per-document proof. The test therefore pins the **property**, not today's key set (§10 obligation 3b).
3. **`bind` deletes what a previous view bound rather than layering over it**, and the re-bind happens on the **view transition only** — rebinding on every refresh would churn maps under the cursor.
4. **The organize pane binds normal mode only.** No `v` / `V` / `<C-v>` / visual-mode mapping has ever existed; item G's symptom was that `v` itself was bound in *normal* mode by a metadata field, so `v` never entered visual mode. Dropping the metadata keymaps with everything else fixes it, and rule 4 is what keeps it fixed.

### 6.3 Acceptance criteria

1. `<CR>` on the archive row and `a` produce **byte-identical** vault states and ActionRecords differing only in `id`/`ts`; neither issues `op.move`; `learning.json` is unchanged by both.
2. In the merge view, `nvim_buf_get_keymap(organize_buf, "n")` contains **exactly** the `<leader>mc` and `<leader>mx` lhs values and nothing else — asserted as set equality over the whole keymap table, so a key added by a future document fails this test if it survives.
3. `nvim_buf_get_keymap(organize_buf, "v")` is empty in every view. Feeding `v` in the merge view enters visual mode with a `metadata_fields` entry bound to `v`.
4. Leaving the merge view restores the full set; entering it twice does not double-bind.

---

## 7. Considered and rejected

| Rejected | Reason |
|---|---|
| `<C-o>` / `L` for back | §2.5 — jumplist collision in a real file buffer; `L` collides with 17's `H` in *meaning* and reads as forward. |
| A forward key to pair with `<BS>` | Re-descending costs ≤3 keys via `f`/`F`/`/`; `<S-BS>` is unreliable in terminals and `<C-i>` is the jumplist again. Deliberate omission, not an oversight. |
| Auto-archiving a merged capture when the user advances or ends the session | The implicit archive is precisely the side effect §2.2 exists to delete. A capture is archived because someone decided it was finished. |
| `merged_into` written into the capture's frontmatter | A third copy that drifts, written into a note the user is editing live (10 §4) — 17 §1's `meta_edit` hazard. §2.5. |
| `merge_duplicate = "append"` as a config key | A configured value that turns every crash-retry into a duplicated block. The override is per-invocation and human-confirmed only. §2.4. |
| Validating provider-returned paths against the client's cached `items` | Weaker (a cache goes stale) and narrower (it forbids the general file picker Matt asked for). The core validates. §3.1. |
| A timeout on a provider that never calls `done` | It would fire while the user is still typing and stack a second picker on the first. §3.6. |
| Routing `f`/`F` through the picker provider | Hint mode's whole advantage is that it opens no window and never leaves the two-pane layout (16 §5). §3.2. |
| Multi-select of several FOLDERS as a multi-destination move | A move archives the original (05 §1.2); "move to two folders" has no meaning under copy-then-archive. §3.4. |
| A second `group_by` knob alongside `order` | A 2-D knob for a 1-D decision; 16 §3.7's default already encodes the only grouping the corpus supports. §4.1. |
| An unseeded `order = "random"` | Contradicts 03 §2's determinism requirement and makes the acceptance test unwritable — which is how it goes unwritten. §4.1. |
| A `merges` state file / a session cursor to track multi-merge progress | Duplicated truth that goes stale under Syncthing — 16 §3.7's recorded rejection, and CRITICAL-1's whole point. The vault holds delivery; the corpus holds display. |
| Making `E382` reachable behind a config value | §1.1 rule 6. No value of any key may restore a bare error. |

---

## 8. Configuration surface and wire deltas

Client-side, `setup()` — these exist only where things are drawn or dispatched, so no CLI can disagree with them (14 §4.1):

```lua
ui = {
  write_action = "commit",           -- "commit" | "notify"; what :w does in an editable pane (§1.1)
  confirm_duplicate_merge = true,    -- ask before appending a second block to the same target (§2.4)
  multi_merge_confirm = true,        -- confirm count + target list of a multi-select merge (§3.4)
  picker_accepts_immediately = true, -- a folder chosen in a picker is applied, not just selected (§3.3)
  show_merge_trail = true,           -- "✓ merged" marks + the capture-header trail line (§2.5)
  highlights = { merged = "DiffAdd" },  -- ⚠ one addition to the closed highlight schema
},
pickers = {
  provider = nil,                    -- nil = auto-detect | name | fun(request, done)  (§3.1)
  provider_order = { "snacks", "telescope", "fzf_lua", "mini_pick", "select" },
  merge_flow = "flat",               -- "flat" | "drilldown" (03 §5's two-step)        (§3.3)
},
keymaps = { buffer = { back = "<BS>" } },  -- unchanged default, widened meaning        (§2.5)
```

Core-side, `~/.config/organize-core/config.toml` — behaviour the CLI must reproduce:

```toml
[file_ops]
merge_archives_capture = false     # false: merge leaves the capture for the next merge; `a` finishes it.
                                   # true:  spec 03 §6 parity — merge archives and the session advances.

[session]
order = "unseen_first"             # 16 §5 owns this key; this doc adds "random" and "score"
order_budget_ms = 5000             # score-ordering gives up and keeps `oldest` for the remaining tail
```

`ui.highlights.merged` must be **defined**, not merely named: `setup()` calls `nvim_set_hl(0, name, { default = true, link = … })` for every `ParaOrganize*` group the plugin can emit — the standing obligation from 16 §5 and 14 §5's flagged gap.

### Wire deltas (the complete blast radius)

| Delta | Kind | Architect ruling? |
|---|---|---|
| `op.merge_commit` gains `archive_capture` (`true` / `false` / `null` ⇒ config) | additive param | No — `EXTRA_METHODS` unchanged |
| `op.merge_commit` gains `allow_duplicate` (bool, default `false`) | additive param | No |
| `op.merge_commit` result `details` gains `already_merged`, `merge_index`, `merges[]` | additive result fields | No |
| `note.get` gains `merges: [{target, ts, action_id}]` | additive result field | No — same class as 16 §3.8's `note_count` |
| `session.start` `order` gains `"random"`, `"score"`; result gains `order`, `seed` | additive values + fields | No |
| `session.start` filters accept `at=`, and period forms on `since=` / `until_date=` | additive values | No |
| `ActionContext` gains `picker: string \| null` | **first-class field, never a `filters` entry** | Records the `dry_run` / `partial_failure` / `pinned` / `batch_size` precedent |
| `fileops.merge_into_note` gains `archive_capture` / `allow_duplicate` kwargs | core API | No |
| **No new RPC method** | — | `EXTRA_METHODS` frozen |

### Required edits to existing documents (a code change without these forks the law)

| Doc | Edit |
|---|---|
| 03 §6 | The Merge row's *"capture archived"* becomes the `merge_archives_capture` split (§2.2). |
| 03 §5 / §24 | Merge entry is one flat note picker by default; the folder → note drilldown is `merge_flow = "drilldown"` and the `select` backend (§3.3). |
| 05 §4 | *"atomic write, archive capture"* becomes *"atomic write; archive the capture only when `merge_archives_capture`"*; the marker block is added to the written body (§2.4). |
| 17 §1 | The `merge` row's two-half undo becomes target-only under the new default (§2.4). |
| 16 §3.6 | The `<leader>f` prompt is seeded and composes; `order=` and `at=` join its vocabulary (§4.2). |
| 16 §6 | The Navigation `<BS>` row's action becomes *"Back one step in the view stack"* (§2.5). |
| 04 §1 | Owned by **the suggestion-fix doc**, not this one. |

---

## 9. Cross-references, and what this document deliberately does not specify

- **Doc 20 — `ParaOrganize dedup`** (item I) owns duplicate-capture detection, the review gate before anything is removed, and the removal semantics. One interaction rule belongs here and only here: **a capture carrying ≥1 merge in its trail (§2.5) is never a silent dedup victim** — its content is already delivered to real notes, and the review gate must show the trail beside it. Everything else about dedup is doc 20's.
- **The suggestion-fix doc** (item H) owns the 04 §1 candidate-enumeration amendment, note candidates, the `kind` discriminator, the capture-side stop-list and the ranker's performance work. This document depends on it in exactly two places — §3.3's flat note picker is far more useful once notes are also *suggested*, and §4.1's `order = "score"` is only worth choosing once the performance work lands — and restates none of it. In particular: **the seven weights, the `min_confidence` floor and the `signal_score <= 0.0` gate are not touched by this document.**
- 15 owns the capture pane's rendering; §2.5's trail line is one field in **its** card, drawn by **its** mechanism.
- 16 owns hint mode, the throughput set, `session.order`'s key and `<leader>f`. 17 owns undo; §2.6's one-record-per-target is what makes multi-merge undoable without new machinery. 18 owns teaching, and `test_every_binding_and_subcommand_is_taught` goes red if `<BS>`'s widened meaning, `m`'s flat picker, `order=`/`at=` or `--again` ship without a lesson.

---

## 10. Test obligations

Per ARCHITECTURE *"Test anti-vacuity standards (PERMANENT)"*, binding on every seat and verifier for this document.

1. **Refusal-predicate pins** — mutate the guard away, confirm red, assert at a parameter where the other branch would fire, and include a firing control. Required for: the duplicate-merge guard (§2.4 — the control is a *different* capture id merging into the same target, which must be **accepted**); a provider path outside the vault (§3.1); multi-select of several folders (§3.4); `done` called twice (§3.6); an unparseable `at=` / period (§4.3); `order_budget_ms` exceeded (§4.1); `<BS>` at the bottom of the view stack (§2.5); `<leader>f` matching zero captures — where the guard is *the current session survives*, so the assertion is that the **old session is still installed**, not merely that no error was raised (16 §8.1's exact wording).
2. **Constant assertions** — assert the **literal** value, never the imported constant, and separately assert the constant agrees with its literal: the literal marker prefix `"<!-- organize:merged capture_id="`, the literal default `merge_archives_capture = false`, the literal default `provider_order` list, the literal `5000` for `order_budget_ms`, the literal `"flat"` for `merge_flow`, the literal `"commit"` for `write_action`.
3. **Must-not-be-connected invariants** — pin the *connection*, not today's consequence.
   a. The `BufWriteCmd` is **buffer-scoped**: writing an unrelated buffer during a session does not dispatch it. Asserting only that `:w` in the pane works passes equally with a global autocmd.
   b. §6.2's whitelist is a **property**: assert set equality between the editable pane's normal-mode keymaps and `EDITABLE_KEEP`'s lhs values over the *whole* keymap table — a test enumerating today's shadowed keys silently stops covering the next document's.
   c. `picker` is absent from `context.filters` — a test that only checks the value is present passes equally with it smuggled into `filters`.
   d. A duplicate-guarded merge writes **no backup and no marker** and leaves `learning.json` byte-identical — asserting only that the target is unchanged passes with a learner that was still taught.
4. **Every new config leaf has a reader**, or is listed in `RESERVED_CONFIG_LEAVES` with a reason and the phase that clears it (14 §4.3.d). The Lua `SCHEMA` needs the mirror gate — 14 §5 flags that it does not have one, and this document adds eight leaves to it.
5. **`EXTRA_METHODS` stays exact.** `test_rpc_methods_are_exactly_the_spec_10_names_in_order` must remain green: this document adds parameters and result fields only.
6. **CLI drift guard.** Every `organize …` invocation quoted here (`organize merge <note> <target> --no-archive`, `--again`, `organize session --order random --seed <s>`) is covered by a drift-guard test that executes the literal string, per the deep_research seat's pattern.
7. **Bytes-on-disk E2E.** The 09 §3 headless-nvim gate gains: one two-target merge session asserting both targets' bytes, the capture's continued existence at its original path, and the archive tree's emptiness; one `:w`-commits-a-merge run; one provider-driven merge through a stub provider. Asserted against disk, never against the plugin's state table.
8. **Real-data regression cases.** The eduardo capture and its siblings (`james-fang`, `jennifer-kesteloot`, `flor-laorga`) enter the real-data fixture as named multi-merge cases with their exact expected trails and marker lines. Matt's complaint becomes an assertion.
9. **Performance** (09 §4 budgets, `-m slow`): the flat note picker's first fetch < 300 ms cold over 8,077 notes and cached thereafter; the delivered-check adds < 5 ms to a merge; `note.get`'s `merges[]` adds < 10 ms; `order = "score"` never blocks `session.start` and never reorders the displayed capture.
10. **Mutation audit before handback**, seat standard: report the count of mutations introduced and killed for every pin above.
