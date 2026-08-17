# 17 — Undo and Action History (NEW)

Directive from Matt (2026-08-15): *"i would like to undo the previous action (previous move, previous sort)"*

Two layers hide in that one sentence and must never be confused. A **move** changed the vault and is recorded in the corpus (12 §2); a **sort** changed only what the right pane was showing and was never recorded at all. This doc specifies both: a core-owned, recorded, refusable **vault undo**, and a client-local **view undo** — presented to Matt as one `u` key, with a toast that always names which layer it just touched. Doc 05 §8 said "No automated undo command is in scope"; **this doc supersedes that sentence** and keeps everything else in 05 §8 (the operation log and backups remain the manual reversal path, and `undo_info()` is unchanged — automated undo cannot key off it, because a log line carries no action id and no content hashes).

The governing fear, stated once: **an undo that half-works is worse than no undo.** Every rule below exists to make partial application impossible or, where it is genuinely possible, loudly recorded.

## 1. What is undoable

"Undoable" means the core can return the vault to byte-identical pre-action state using only the sanctioned primitives of 05 §1. Anything less is named honestly.

| Action (`ActionRecord.operation`) | Undoable? | What undo does, exactly |
|---|---|---|
| `move` | **Yes, fully** | Restore the archived original to its capture path (verified relocate), then archive the organized copy. The pre-move frontmatter comes back for free: 05 §2 step 6 wrote the tag / `processing_status` / `last_edited_date` onto the **copy**, never onto the archived original. |
| `archive` | **Yes, fully** | Verified relocate from the archive path back to the original path. |
| `skip` | **Yes**, in two halves | It touched no file and wrote no oplog line — only an ActionRecord. Undo appends an inverse record so the counterfactual corpus stays honest (`op.skip` exists precisely to preserve that counterfactual), and, **if the originating session is still alive**, puts the capture back in front of Matt (§7). A skip from a dead session undoes its corpus half only, and says so. |
| `meta_edit` / `tag_edit` | **Yes, fully** | A **key-scoped** frontmatter write restoring `capture.frontmatter_before` for exactly the keys in the operation's `details["keys"]`, via `update_frontmatter(..., replace_keys=<those keys>)` so a list field is replaced, not appended. It does **not** restore the file from the backup — the left pane is a live editable buffer (10 §4) and Matt may have legitimately edited the body since; blowing that away to reverse a tag would be the data loss this doc exists to prevent. |
| `merge` | **Partially** — restore-or-refuse | Two things happened: the target was rewritten (backed up) and the capture was archived. Undo restores the target from its pre-merge backup **and only when the target's current bytes still hash to the record's `targets[].after_hash`**, then restores the capture from the archive. If the target changed since, undo **refuses**. It never attempts a three-way merge. With `file_ops.create_backups = false` the pre-merge bytes exist nowhere and merge is **not undoable at all** — `organize health` must warn about this, because the config silently costs a safety net. |
| `append` (tag-route, 11 §1) | **Partially** — two tiers, then refuse | Tier 1 (`restore`): target still hashes to `after_hash` ⇒ restore the pre-append backup, byte-exact. Tier 2 (`excise`, `undo.append_strategy = "restore_or_excise"`): the target changed elsewhere, but the recorded appended block **and** its machine-owned `append_marker` (CRITICAL-1) still appear verbatim exactly once ⇒ remove exactly those bytes, backup first, atomic write. Tier 3: neither holds ⇒ **refuse**. Note the append did *not* archive the capture — the route layer archives separately as its own record with its own undo. |
| `integrate` commit | **Partially** — restore-or-refuse | Identical rule to `merge`: restore the pre-integrate backup iff the target still hashes to `after_hash`, else refuse. The LLM is **never** re-run. |
| `create_folder` | **No** | Removing a directory is a delete, forbidden by 05 §1.1, and the log cannot even tell whether the folder pre-existed (`details["created"]` never reaches the oplog line). Undo refuses and names `rmdir` as the manual step. If `ui.auto_move_to_new_folder` fired a move into it, **that move is a separate record and is undoable** — undo it and the folder is left empty but harmless. |
| any `consumer:*` action (taskwarrior, anki/learn, question_answer, deep_research) | **No** for the foreign half; **yes** for the vault half | The consumer wrote into a system this core does not own (`~/.task`, Anki) and cannot transactionally revert. `organize undo` refuses a record whose actor is `consumer:*` **when it has a foreign effect**, naming the external system and the consumer's own backup dir. Its vault-side write-back (`meta_edit`, `actor: consumer:learn`) is an ordinary metadata undo and is allowed. |
| `auto-organize` application (13 §2 `auto_below`) | **Yes** — whatever it did underneath | It is a move/append/integrate and undoes by those rules. It also carries the strongest calibration signal doc 13 has (§4). |
| `op.merge_preview` | **Nothing to undo** | It is in `MUTATING_METHODS` for lock discipline only; it writes no file and emits no record. |
| a **view** change (sort cycle, selection, browse descent, search, pane focus) | **Yes**, client-locally | Never recorded, never an RPC. §7. |

## 2. The undo model

**Decision: an undo is a NEW forward operation, not a rollback.** It is executed through the ordinary `fileops` context, writes its own operation-log lines, appends its own `ActionRecord`, and is itself undoable — undoing an undo *is* redo, and the chain is visible in history.

Justification against the never-delete law: a rollback wants to move files backwards (`os.replace`/`rename`) and to amend history. Both are structurally forbidden here — the AST guard in `tests/test_fileops_safety.py` names the only three functions that may remove a path, and the corpus is strictly append-only (`_append_line`, cross-process lock, torn-tail healing). A forward inverse needs neither: every restore is `atomic_write` or a verified relocate into a free path, so **at no instant does a byte of note content exist in only one place**, and the corpus only ever grows.

Resulting invariants:

1. **Precondition-complete before the first write.** Every precondition of §3 is evaluated against the whole action — every target of a multi-destination route batch included — before anything is touched. A batch is all-or-nothing: one failing target refuses the whole undo.
2. **Undo never merges, never guesses, never partially applies by design.** It restores exactly, or it refuses.
3. **If it fails mid-flight anyway** (disk full after restoring target 1 of 2), it returns `ok=false`, writes `[FAILED]` oplog lines, and sets `context.partial_failure` on its own record naming exactly what did not finish. **A partially-applied undo does NOT mark the original as undone** (§5) — the original's effect is still partly present, so calling it undone would be a lie the learner would act on.
4. **Operation log**: one line per file the undo actually moved or rewrote, type `undo`, ordinary grammar `[<ts>] undo: <src> -> <dst> [SUCCESS|FAILED][ Backup: …]`. The single-line grammar and its escape table are **not extended** — "this undid act_X" lives in the corpus, and the join between the two logs is the shared `ts` plus paths, exactly as it already is for a move's paired archive line.
5. **Corpus**: exactly one inverse `ActionRecord` per undo, carrying the full `targets[]` before/after of the reversal. `operation = "undo"` — a new value in both `fileops.OperationType` and `actions.Operation`, plus `_ACTION_OPERATION["undo"] = "undo"`. Old readers reject unknown operations by design, so **this bumps `ACTIONS_SCHEMA_VERSION` to 2** (§4 migration). No existing line is ever rewritten.
6. **The oplog is not the undo input.** `op.undo` keys off the `ActionRecord` (which has `id`, `capture.content_hash`, `targets[].before_hash`/`after_hash`/`before_text`) and uses the oplog line only for the backup/archive paths.

## 3. Preconditions and refusals

**Decision (⚠ deviation from the "where the error line falls" ruling, deliberate):** every undo *refusal* raises `-32000` with a discriminable `error.data.kind` and writes **no oplog line and no ActionRecord** — nothing happened. `ok=false` is reserved for an undo that began and could not finish (invariant 3). The ruling's split is addressing-vs-world-state; an undo refusal is always a *precondition* failure evaluated before any write, so it is structurally an addressing failure even when the reason is world state. Recording a `[FAILED]` line for an attempt that touched nothing would pollute the one log whose purpose is manual reversibility.

One new error class, `UndoRefused(OperationError)`, exists so the client can branch on `error.data.kind` for its toast (the standing ruling: a client MUST check `result.ok` **and** branch on `error.data.kind`). Everything else reuses the existing taxonomy. User-facing shape is the house one-liner — `<ClassName>: <message>` then `hint: <hint>`, never a traceback.

| Precondition violated | Class / `data.kind` | Message shape |
|---|---|---|
| No such action id | `OperationError` | `OperationError: no action act_01J… in the corpus (scanned 2 month files)` · hint: `organize actions history --limit 20` |
| Already undone | `UndoRefused` | `UndoRefused: act_01J… was already undone by act_01K… at 2026-08-16T09:12:04Z` · hint: `undo that one to redo this action` |
| Operation type not undoable | `UndoRefused` | `UndoRefused: create_folder is not undoable — removing a directory would be a delete (spec 05 §1.1)` · hint: `rmdir <path>` by hand |
| Foreign-system effect | `UndoRefused` | `UndoRefused: consumer:taskwarrior wrote outside the vault (~/.task); organize cannot revert it` · hint names the consumer's backup dir |
| A touched file changed since the action | `ConcurrentModificationError` | `ConcurrentModificationError: areas/health/training-log.md changed since act_01J… (sha256 3f9c… != a71b…)` · hint: `inspect the file; undo will not overwrite later edits` |
| The path to restore to is occupied by a different note | `UndoRefused` | `UndoRefused: capture/raw_capture/2026-08-15T1140.md is occupied by a different note (id cap-0994)` |
| The archived original / the backup is gone | `UndoRefused` | `UndoRefused: the archived original <path> is missing; nothing to restore from` |
| No backup was taken (`file_ops.create_backups` was false) | `UndoRefused` | `UndoRefused: no backup exists for act_01J… (file_ops.create_backups was false when it ran)` |
| Append tier-3 (block no longer verbatim) | `UndoRefused` | `UndoRefused: the appended block is no longer verbatim in <path>; excising it could cut live text` |
| Target is `no-ai: true` **and** the undo actor is not human | `NoAiRefusal` | existing shape (02's vault law; a machine reversal is still automated tooling) |
| A machine actor undoing a Matt-decided action | `UndoRefused` | `UndoRefused: actor "auto-organize" may not undo an action decided by "matt"` — **not configurable**, see §4 |
| `undo.enabled = false` | `ConfigError` | `ConfigError: undo is disabled ([undo] enabled = false)` |
| Older than `undo.max_age_days` (when set) | `UndoRefused` | `UndoRefused: act_01J… is 41 days old (undo.max_age_days = 30)` |
| Session position half unavailable | *not a refusal* | The corpus half still applies; the result reports `session: null` and the toast says the session is gone. |

A concurrent writer is **not** a refusal: `op.undo` is in `MUTATING_METHODS` and waits its turn in the `WriterQueue` under the write lock, like every other mutating call.

## 4. Learning interaction

The undone action's teaching must go away, and the code cannot subtract it: `learning.json` is a lossy derived view with **no provenance** — `last_used`/`last_seen` are clobbered scalars, `total_moves` is a global denominator, and `apply_decay` may already have hard-deleted the association. So: **mark the corpus, then rebuild the view.** `learn.py` already declares the right hierarchy — "the action log is the source of truth, `learning.json` is a derived view".

**Representation — inverse record plus one correlation field.** `ActionContext` gains `undoes: str | None`, the `act_<ulid>` of the reverted action, on the **inverse** record. Trailing, defaulted, omitted from `to_json()` when empty, parsed **fail-closed** — exactly the shape of `LLMTrace.proposal_id` / `stale_target` and of the `dry_run` / `partial_failure` promotions. Rejected alternatives, and why: an *excluded flag mutated onto the original line* would require update-in-place on an append-only file whose locking is designed only for appends; a *pure tombstone* would be a second record for one event when the undo already changes the vault and must record anyway.

**What each reader must do:**

| Reader | Change |
|---|---|
| `learn.record_action` | Skip the **inverse** (one guard beside the `partial_failure` gate: `context.undoes is not None` ⇒ `None`) — the folder the note lands back in is usually the inbox, and teaching "captures like this belong in the inbox" is actively wrong. On replay, also skip any record in the undone set. |
| `learning.json` | **Rebuilt, not decremented.** Replay `ActionRecorder.query()` oldest-first through `record_action` with the undone set excluded, applying decay at each record's **own** `ts` so the rebuild is a pure function of (corpus, config). ⚠ This makes `learning.json` reproducible and testable, which the incremental path never was; it is a deliberate behavior change. Synchronous on the CLI path; on the server it runs on the writer thread under `_learning_lock` and replaces `self._learning` **wholesale** — 🔴 the server caches learning and never re-reads it, so it must additionally reload from disk whenever the file's mtime changed since load, or a CLI-side undo is silently overwritten. Gated by `undo.rebuild_learning` (default `true`); when false the corpus is still marked and `organize health` reports learning as stale-by-N-undos. |
| `ActionRecorder.query_similar` | **Highest severity.** Exclude undone originals — doc 13's precedent retrieval would otherwise re-propose exactly the thing Matt just reverted. |
| `ActionRecorder.stats` | Undone records stay in `total`, `by_operation`, `by_actor`, `by_route`; they leave `with_suggestions`, `top_accept_rate`, `rank_histogram`, and the `integrate.*` verdict rates. New counter `undone: N` beside `partial_failures`. **Adjudicated:** an undone rank-1 accept is *dropped*, not counted against the engine — the undo is normally followed by a corrective action that lands its own record with its own `chosen_rank`, and that corrective record is the real negative signal. Counting both would double-count. |
| `ActionRecorder.query` / `export` | **No change.** Both lines are the audit trail and must survive. Exclusion belongs to precedent readers, not to the stream. |

**Firewall proof (Phase-6 ruling: `LLM_EDIT_ACTORS` stays `frozenset({"claude-integrate"})`).** Three steps. (i) The inverse record's actor is whoever performed the undo, and it passes through `is_matt_decided` unchanged, so a machine-initiated rollback folds nothing — undo requires **zero** widening of `LLM_EDIT_ACTORS`. (ii) Undo introduces **no decrement primitive**: the only write to `learning.json` remains a full rebuild that replays through `record_action`, which already gates on `is_matt_decided`. Excluding an undone record can therefore only remove *that record's own* contribution, and only if it was foldable in the first place. (iii) The remaining hole — a machine erasing Matt's learned preferences by undoing his actions — is closed by the refusal in §3: **a machine actor may not undo a Matt-decided action.** That rule is deliberately not a config key; making it configurable would re-open the exact one-directional firewall the Phase-6 ruling protects. Corollary for whoever executes the Phase-6 precondition: the applied-via distinction (`"matt-confirmed" | "auto_below"`) that ruling demands is the *same* distinction undo needs to answer "who undid it" — the two must share one field, not invent two.

*(Doc 14's de-Matt-ification applies here as everywhere: the rule is "a machine actor may not undo a **human-decided** action", and `is_matt_decided` / `actor: "matt"` are named for one deployment. Whoever renames them renames this doc's uses in the same pass — the semantics are the owner's identity, not Matt's.)*

**Migration / compat.** No record on disk is ever rewritten. `context.undoes` absent ⇒ `None` (fail-closed), so every pre-existing record reads back as "not an undo" and no pre-existing record is in any undone set. `ACTIONS_SCHEMA_VERSION` becomes `2` because a new `operation` value is a reader-visible break; readers accept `schema_version <= 2`. A v1 corpus is fully undoable — v1 records carry `content_hash`, `before_hash`/`after_hash` and the oplog carries the backup paths, which is everything the preconditions need.

## 5. Depth, chains, redo, and history

**Undo is addressed, not stacked.** There is no depth limit and no "must undo in order" rule: any action in the corpus may be undone at any time, because safety comes from the §3 preconditions (which check the *current* state of every touched file), not from adjacency. A twenty-step-old move whose files nobody has touched is exactly as safe to undo as the last one; the action before it whose target was edited yesterday is refused, whether it is one step back or fifty. Age is therefore not a gate by default (`undo.max_age_days = null`); the corpus **scan** window is bounded for performance only (`undo.scan_months`, default `2`).

**Undone-set definition** (well-founded; every candidate `Y` is strictly later in the corpus than `X`, and the chain is finite):

> `undone(X)` ⇔ ∃ a record `Y` with `Y.context.undoes == X.id`, `Y.context.partial_failure is None`, and `undone(Y)` false.

So A-undone-by-B ⇒ A is undone; B-undone-by-C ⇒ B is not in effect ⇒ A is in effect again. **There is no separate redo verb in the core**: redo is `op.undo` addressed at the inverse record. The client binds `U` to that (§7) so Matt never types an id.

**The history view** lists, newest first, one row per record in the scan window: `ts` (relative — "4m ago"), `actor`, `operation`, the capture's display name, the destination(s), `route`, `chosen_rank`, and an **undoability verdict** — `undoable: true`, or `false` with the exact `refusal` string §3 would produce. That last column is the point: the view tells Matt what he can take back and, when he cannot, why, without him pressing anything. It is computed by the **same predicate function** `op.undo` runs, called in dry mode — one predicate, two callers, pinned both ways by test (§9).

## 6. The wire and CLI surface

**RPC — two new methods.** `op.undo` is the second WRITING addition beyond spec 10 §2 (after `op.skip`), justified the same way: without it the thin client cannot reach a core capability at all, and 10 §1 forbids it from touching vault files itself. Both names go in `RPC_METHODS`, `self._handlers`, and `EXTRA_METHODS` in `tests/test_server_protocol.py`; `op.undo` also joins `MUTATING_METHODS` and stays in `INDEX_CHANGING_METHODS` (it moves notes). `history.list` is read-only.

```jsonc
// op.undo  — params
{ "action_id": "act_01J…" | null,   // null ⇒ most recent undoable action, newest first
  "session_id": "…" | null,          // scopes the "most recent" search to one session
  "dry_run": false }                 // true ⇒ evaluate preconditions, write nothing
// op.undo  — result
{ "ok": true, "dry_run": false,
  "undone": "act_01J…",              // string — the action reversed
  "undo_record": "act_01K…",         // string|null — the inverse record's id (null on dry_run)
  "operation": "move",               // the reversed action's operation
  "restored":  [ { "path": "capture/raw_capture/2026-08-15T1140.md",
                   "from": "archive/capture/raw_capture/2026-08-15T1140.md",
                   "kind": "archive" | "backup" | "frontmatter" | "excise" } ],
  "retired":   [ { "path": "areas/health/2026-08-15T1140.md",
                   "to":   "archive/capture/raw_capture/2026-08-15T1140_20260816_091204.md" } ],
  "session":   { "restore": "capture" | "append" | null,   // §7
                 "capture_path": "…", "index": 12,
                 "unmark": "processed" | "skipped" | null } | null,
  "learning":  { "rebuilt": true, "records_replayed": 18412, "duration_ms": 940 },
  "partial_failure": null }          // string when ok=false — names exactly what did not finish
// history.list — params
{ "limit": 50, "since": "2026-08-01" | null, "until": null,
  "operation": null, "actor": null, "session_id": null, "undoable_only": false }
// history.list — result
{ "total_scanned": 4102, "entries": [
  { "id": "act_01J…", "ts": "2026-08-16T09:04:11Z", "actor": "matt", "operation": "move",
    "capture_path": "…", "capture_label": "Ladder drills felt easy", "targets": ["areas/health"],
    "route": null, "chosen_rank": 1, "edit_mode": null,
    "undoes": null, "undone_by": null,
    "undoable": true, "refusal": null, "refusal_kind": null } ] }
```

Errors: `-32000` with `data.kind ∈ {UndoRefused, ConcurrentModificationError, OperationError, NoAiRefusal, ConfigError}`; `-32602` for a malformed `action_id`.

**CLI — two new subcommands and one repair command.** Mirror all three declaration sites (`SUBCOMMANDS`, `build_parser`, `_HANDLERS`) — note `purged` is today in two of the three, proof the lists are not machine-checked.

| Command | Flags |
|---|---|
| `organize undo [ACTION_ID]` | `--session ID`, `--json`. Rehearsal uses the **global** `--dry-run` (`organize --dry-run undo`) — no per-command duplicate. |
| `organize actions history` | `--limit N` (50), `--since`, `--until`, `--operation`, `--actor`, `--session ID`, `--undoable`, `--json` |
| `organize learn rebuild` | `--json`. The repair path for `undo.rebuild_learning = false` and for any corpus edit. |

Human output follows `_print_result`'s shape — `undo (move): areas/health/2026-08-15T1140.md -> capture/raw_capture/2026-08-15T1140.md` with indented `restored:` / `retired:` / `learning:` lines. `--json` emits the RPC result objects verbatim through `_json_out` (sorted keys, indent 2).

**Config.** Applying 14 §4.1's one question — *would the CLI behave differently if this changed?* — behavioral keys live in the core (`~/.config/organize-core/config.toml`, 10 §3) so the CLI and UI can never disagree, while the confirmation prompt and the keymaps are UI-only and live in the nvim `setup()` table: a `confirm` knob in the core would be a dead key on the CLI path, and every key is honored or deleted. Each key below has exactly one reader, so none may sit in `RESERVED_CONFIG_LEAVES`. Every default is chosen for a stranger with a fresh vault — undo on, no age limit, learning rebuilt.

```toml
[undo]
enabled = true                          # read by the op.undo handler / cmd_undo
scan_months = 2                         # corpus months searched for "the previous action"
max_age_days = 0                        # 0 = no age limit; the state checks are the real gate
rebuild_learning = true                 # replay learning.json after marking the corpus
append_strategy = "restore_or_excise"   # or "restore_only" — never excises a changed target
```

```lua
-- nvim setup(): UI-only
keymaps = { buffer = { undo = "u", redo = "U", history = "H" } },
ui = { undo_confirm = "outside_session",  -- "never" | "outside_session" | "always"
       history_limit = 50,
       view_undo_depth = 20 },
```

## 7. The client surface

**One key, one ordered ring.** The client keeps a per-session ring (depth `ui.view_undo_depth`) that interleaves, in real time, view changes and vault operations. A view entry holds the previous view snapshot (sort mode, selection, right-pane view, browse stack + path, search query/scope, focused pane); a vault entry holds **only an `action_id` string** — the thin client stores no vault state (10 §1). `u` pops the ring: a view entry is undone locally with **no RPC**; a vault entry calls `op.undo`. `U` redoes symmetrically (a vault redo is `op.undo` addressed at the inverse). This is not magic — it is what every editor does — and the ambiguity Matt would otherwise face is removed by the toast, which always names the layer and the thing: `undid: sort → Alphabetical` versus `undid: move → areas/health`. When the session ends the ring dies; vault undo remains reachable through history.

**`u` is bound in the organize pane only, unconditionally.** The capture pane is the real capture file's buffer (10 §4) where `u` is Vim's own undo, and shadowing it would be indefensible. The `undo`/`redo` rows therefore carry `panes = {"organize"}` and are **not** subject to `ui.capture_pane_keymaps` — that knob may not promote them. `H` opens the history view, a **sixth** right-pane state (03 §3's four became five with the 12 §1 review gate); adding it as a third hardcoded branch in `render.right_pane` is refused — this doc requires the view dispatch become a `render.VIEWS[name] = fn` table, which is the seam the integrate view already needed.

**Confirmation: instant, no modal.** Default `ui.undo_confirm = "outside_session"` — an action taken in this session undoes on the keystroke with a toast; an action reached through history from hours ago prompts once with a one-line summary. A modal in front of every undo is backwards: undo *is* the safety net, and making it expensive makes people stop reaching for it. The cost of a mis-press is one keystroke (`U`), because undo is itself undoable. Refusals are the loud path: they surface as an error-level notify carrying the §3 one-liner, and — because they are precondition failures — nothing was touched, so there is nothing to clean up. `"never"` and `"always"` exist for other people's risk tolerance.

**After an undo the UI shows** the restored capture in the left pane (a real buffer on the restored path, re-resolved through `note.get`), regenerated suggestions in the right pane, the recomputed counts line, and the toast. `state.selected` resets to 1; `shown_at` resets so `durations_ms.decision` on the *next* action measures a fresh decision, not one contaminated by the detour.

**Session-state restoration** is what makes undoing a skip actually work, and the core sends the instructions in `result.session` rather than the client inferring them:

| Undone action | Client does |
|---|---|
| `skip` in this session | Remove the path from `session.skipped`; re-insert the capture at `index`; set `session.current` to it; reload suggestions. The capture is literally back in front of him. |
| `move` / `archive` / `merge` / `integrate` in this session | Remove from `session.processed`; re-insert at `index`; set `current`; re-`note.get` (the file is back at its original path). |
| Anything from a previous session | `restore: "append"` — the restored note is appended to the **end** of the queue if it matches the session's filters, and the toast says so. Injecting a note from a dead session mid-queue would silently renumber "Capture i of n" under him. |

Restoration requires `processed`/`skipped` in their default **list-of-paths** form. The numeric-aggregate form some readers tolerate carries no paths, so undo refuses the session half with `session: null` and a plain message rather than guessing a position.

## 8. Acceptance tests

1. **Move round trip, bytes on disk.** Move a capture, `organize undo`, assert: the capture file exists at its original path with a sha256 equal to the pre-move file's, its frontmatter has no `project/…` tag and `processing_status` is back to `raw`; the organized copy is gone from the destination and present under the archive; the operation log holds a `move` line, an `archive` line and two `undo` lines; the corpus holds exactly two records, the second with `context.undoes` equal to the first's `id`.
2. **Undo of undo is redo.** Undo the move, undo the inverse, assert the vault is byte-identical to the post-move state, three records exist, and `undone(A)` is **false** by the §5 recursion.
3. **Changed target refuses, touching nothing.** Merge a capture into a target, append one byte to the target, `organize undo`: exit 1, stderr's first line matches `^ConcurrentModificationError: `, the target's bytes and mtime are unchanged, the capture is still archived, and **no new oplog line and no new ActionRecord were written**.
4. **Append tier 2.** Route-append a capture, edit an unrelated paragraph of the target, undo: the appended block and its marker are gone, the unrelated edit survives verbatim, and the record's `targets[].diff` shows only the excision. With `append_strategy = "restore_only"` the same input refuses with `UndoRefused`.
5. **No backup, no undo.** With `file_ops.create_backups = false`, merge then undo: refuses with `UndoRefused: no backup exists for act_…`, and `organize health` names `file_ops.create_backups` as the reason merge/append/integrate undo is unavailable.
6. **Batch is all-or-nothing.** A two-destination route batch where the second target was edited since: undo refuses, and the **first** target is byte-unchanged.
7. **Skip returns the capture.** In a headless-nvim session, skip capture 3, press `u`: `session.skipped` no longer holds it, `session.current` points at it, the left pane buffer is its file, and the counts line reads the pre-skip numbers.
8. **`u` never shadows Vim undo.** In the same session, focus the capture pane, type text, press `u`: the buffer text is reverted by Vim and **no** `op.undo` RPC was issued (assert against the mock core's call log).
9. **Learning is rebuilt, not decremented.** Move three captures to the same folder, undo the second, assert `learning.json`'s `destinations[dest].count == 2` and `statistics.total_moves == 2`; rebuild twice and assert the two files are **byte-identical**.
10. **The undone action stops being a precedent.** After undoing a move, `organize actions query --similar-to "<that capture's text>"` does not return it, while `organize actions export` still emits both lines.
11. **`create_folder` and `consumer:taskwarrior` refuse** with the exact §3 message shapes; the taskwarrior refusal names `~/.task` and the consumer backup dir.
12. **Machine may not undo Matt.** An `op.undo` whose context actor is `auto-organize`, addressed at a record with `actor: "matt"`, refuses with `UndoRefused` — and `learning.json` is byte-unchanged.
13. **v1 corpus is undoable.** A fixture corpus written at `schema_version: 1` with no `undoes` key anywhere: every record reads back with `undoes == None`, the undone set is empty, and a v1 `move` record undoes successfully.
14. **History honesty.** `organize actions history --json` over a corpus containing one undoable move, one already-undone move and one `create_folder` returns `undoable` values `[true, false, false]` and `refusal_kind` values `[null, "UndoRefused", "UndoRefused"]`.

## 9. Test obligations (anti-vacuity)

Binding on the seat that builds this and on its verifier; these are gates, not suggestions.

- **Every refusal predicate in §3 gets a guard-deleted pin.** For each row: delete or invert that one guard in a scratch copy and the corresponding test must **fail**. A refusal test that still passes with its guard removed is proving nothing — and here it would be proving nothing about the exact code path that stands between Matt and data loss. Enumerate them in the test module docstring so a reviewer can count rows against tests.
- **Mutation audit before handback.** For each refusal, mutate the predicate three ways — always-true, always-false, and boundary-off-by-one (e.g. hash compare → prefix compare, `>` → `>=` on `max_age_days`) — and record which test caught which mutation. Any mutation caught by nothing is an untested predicate and blocks handback.
- **Constant assertions are literals.** Assert `"UndoRefused"`, `2` (the schema version), `"restore_or_excise"`, `50` — never the imported module constant, which makes the test agree with any future value of itself.
- **Must-not-be-connected invariants.** Assert positively that: the client issues **no** RPC for a view undo; a refusal writes **no** corpus line and **no** oplog line; `query`/`export` do **not** filter undone records; `LLM_EDIT_ACTORS` is still exactly `{"claude-integrate"}` after this feature lands.
- **Both-ways seam pin.** `op.undo`'s precondition evaluation and `history.list`'s `undoable`/`refusal` columns must be pinned to the same function from both sides: a table-driven test feeds each refusal scenario to both entry points and asserts identical `kind` and identical message. Two predicates that drift is how a history view starts lying.
- **Never-delete guard unchanged.** Assert the AST guard's `allowed` set still names exactly its sanctioned functions after the undo path lands. If undo needs the verified relocator, it must **reuse** it (renamed and re-documented in the same commit, guard set and both docstrings edited together), not add a fourth deleter.
- **Torn-write and crash tests** for the inverse record, matching 12 §3's obligation: an interrupted undo leaves no partial JSONL line, and a crash between the vault write and the corpus append is detectable (the file moved, no `undoes` marker) — assert the next `undo` of the same action refuses on state rather than silently double-applying.
- **Perf budget (09 §4).** `op.undo` must return within the 50 ms UI-response budget for the vault half; the learning rebuild is measured separately, marked `-m slow`, and must not hold the read lock — assert a concurrent `suggest.for_note` completes while a rebuild is running.
