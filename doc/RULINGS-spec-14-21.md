# Architect rulings — spec 14-21 harmonisation (2026-08-16)

Specs 14-18 were authored in PARALLEL by five agents that could not see each
other's work. The adversarial review found what that invites: colliding config
keys, two names for one seam, and rulings honoured in one document but not its
sibling. An architect arbitrated all five lens reports into the rulings below;
each owning document then applied its own work order.

**These rulings are law for the build.** A seat that finds a spec sentence
contradicting a ruling here follows the RULING and reports the contradiction —
the house learned this the hard way when paraphrased briefs diverged from the
record four times in one build.

43 rulings; 9 findings deliberately declined.

## Rulings

### R1 — 

**Ruling.** Doc 15 owns it and DELETES the section. No doc may add a key under `ui.display.*`. 14 cites 15's replacement sections; 16's `show_progress` relocates per R2.

*Affects:* `spec/14-built-for-others.md`, `spec/15-capture-presentation.md`, `spec/16-navigation-and-throughput.md`

### R2 — 

**Ruling.** Sections: ui.capture.* (15), ui.organize.* (15, extended by 16), ui.hint.* (16), ui.batch.* (16), ui.undo.* (17), ui.teach.* (18), plus existing top-level ui.highlights/icons/float_opts/win_options/capture_pane_keymaps/close_on_complete. Final renames: ui.display.show_progress->ui.organize.show_progress; ui.numeric_accept->ui.organize.numeric_accept; ui.preview_debounce_ms->ui.organize.preview_debounce_ms; [throughput] preview_notes->ui.organize.preview_notes; ui.batch_confirm->ui.batch.confirm; ui.batch_max->ui.batch.max; ui.undo_confirm->ui.undo.confirm; ui.history_limit->ui.undo.history_limit; ui.view_undo_depth->ui.undo.view_depth. 15 lands the ui.capture/ui.organize closed records first; 16 and 17 append leaves afterwards.

*Affects:* `spec/14-built-for-others.md`, `spec/15-capture-presentation.md`, `spec/16-navigation-and-throughput.md`, `spec/17-undo-and-action-history.md`

### R3 — 

**Ruling.** `ui.organize.score_thresholds = { high = 2.0, medium = 1.0 }` (numbers). Doc 15 owns it; doc 14 cites it and assigns the tier only. `ui.highlights.score_high|score_medium|score_low` remain HIGHLIGHT-GROUP NAME strings, unchanged and untouched. `ui.display.score_high`/`score_medium` are deleted from doc 14 entirely.

*Affects:* `spec/14-built-for-others.md`, `spec/15-capture-presentation.md`

### R4 — 

**Ruling.** DELETE 14's seam. The canonical row/renderer seams are 15's: `ui.capture.render`, `ui.capture.formatters`, `ui.capture.fields.*`, `ui.organize.render_row`. `ui.format.suggestion|entry|capture_line` is struck from doc 14. Doc 14 keeps only the stability tier and the no-fork scenarios, citing 15's names.

*Affects:* `spec/14-built-for-others.md`, `spec/15-capture-presentation.md`

### R5 — 

**Ruling.** The closed `ui.highlights` record grows from 7 to 12 leaves: `label`, `label_dim`, `row_dim`, `pinned_destination`, `batch_marked` (replacing 16's hint_label/hint_label_dim/hint_dim/pinned/marked). The pre-existing `ui.highlights.hint` KEEPS its meaning — dim secondary text in the capture card and the integrate hint — and is neither renamed nor repurposed; doc 15 must say so when it replaces draw_capture_header.

*Affects:* `spec/15-capture-presentation.md`, `spec/16-navigation-and-throughput.md`

### R6 — 

**Ruling.** nvim. It becomes `ui.organize.preview_notes` (default 5). `[throughput] preview_notes` is deleted from 16 §5's TOML block. `throughput.mru_size`, `throughput.mru_order` and `[session] order` stay core.

*Affects:* `spec/16-navigation-and-throughput.md`, `spec/15-capture-presentation.md`

### R7 — 

**Ruling.** Default stays `zi`; `panes = {"organize"}`; `navigation = false`. 16 §6's table row becomes `organize`. With zi organize-only, `z` is a prefix in the organize pane only, so `zo`/`za` in the capture pane stay instant and 15 §3's fold-recovery claim is true as written.

*Affects:* `spec/15-capture-presentation.md`, `spec/16-navigation-and-throughput.md`

### R8 — 

**Ruling.** No. `f F . P x 1-9 u U H` are VIEW-SCOPED: bound only in views {suggestions, browse, search, mru, history}, and unbound whenever the organize buffer is modifiable (view=="merge", or integrate with editing==true), through the same bind_gate/unbind_gate mechanism `e` already uses. Exempt: `zi` (fold toggle, non-destructive), `e` (already scoped), and all `<leader>` sequences. 16 §6's and 16 §'capture pane' rationale must be amended: the organize pane is NOT a read-only nofile pane in every state.

*Affects:* `spec/16-navigation-and-throughput.md`, `spec/17-undo-and-action-history.md`

### R9 — 

**Ruling.** Owner: doc 14 (it owns src/organize_core/config.py). ONE patch adds 30 rows to the 23 that exist. Additions: e=integrate_edit, <leader>mi=integrate, <leader>mm=integrate_mode (doc 12, pre-existing gap); zi=cycle_fields (15); f=hint_row, F=hint_vault, .=repeat_destination, P=pin_destination, x=mark_capture, 1..9=numeric_accept (nine rows), <leader>ba=batch_all, <leader>bc=batch_clear, <leader>d=mru, <leader>f=filter_session (16); u=undo, U=redo, H=history (17); <C-n>=teach_next, <C-p>=teach_prev, <C-g>=teach_hint, <C-r>=teach_replay, <C-q>=teach_quit (18). Total 53. In the same patch, correct the two drifted values to the Lua action names: "S"->sort_cycle (was "sort"), "<BS>"->back (was "back_to_parent"). Drift gate (14 §10.7): CORE_KEYMAPS's key set equals the set of non-metadata lhs values in keymap_table(), exported to a fixture by the headless-nvim gate.

*Affects:* `spec/14-built-for-others.md`, `spec/15-capture-presentation.md`, `spec/16-navigation-and-throughput.md`, `spec/17-undo-and-action-history.md`, `spec/18-teach-mode.md`

### R10 — 

**Ruling.** `views` — a list of view names. Signature: `actions.register_key{ name, default, desc, panes, views, fn }`. `mode` is struck (it means the Vim mode in Neovim and the live table already uses view scoping). This must be settled before any of 15/16/17 registers a key, because R8 depends on it.

*Affects:* `spec/14-built-for-others.md`, `spec/15-capture-presentation.md`, `spec/16-navigation-and-throughput.md`, `spec/17-undo-and-action-history.md`

### R11 — 

**Ruling.** Default alphabet becomes `"asdfghjklqwertyuiopzxcvbnm"` (26 keys, `;` dropped). `ui.hint.alphabet` must contain only characters with a distinct shifted form — a ConfigError at setup naming the offending character, beside the existing min-4/all-distinct check. Loop: explicit `vim.cmd("redraw")` after every label rebuild; `getcharstr()` is pcall-wrapped and `Vim:Interrupt` maps to abort; every abort path clears NS_HINT. Worked example and acceptance-test literals become 26+25k>=40 => k=1 => 25 one-char, 15 two-char.

*Affects:* `spec/16-navigation-and-throughput.md`

### R12 — 

**Ruling.** New. 16 §3.8 binds it for the first time, so 16 §'capture pane' rule applies: `p` is `panes = {"organize"}`. Remove `p` from 16's grandfathered list, correct 16 §6's table row to `organize`, and add `p` to 16 §7.10's absent-mapping list.

*Affects:* `spec/16-navigation-and-throughput.md`

### R13 — 

**Ruling.** The card extmark anchors to the FIRST BUFFER LINE AFTER the frontmatter close delimiter, with virt_lines_above = true — never to (0,0) — because virtual lines on a line inside a closed fold are not drawn (w_topfill is 0 over a closed fold). When there is no frontmatter, or frontmatter = "none", it anchors to line 1 AND the window fill is established after every draw via winrestview{topline=1, topfill=#virt_lines} inside nvim_win_call, because topfill is 0 after mount. 15 §7's rendered example is redrawn with the fold line ABOVE the card.

*Affects:* `spec/15-capture-presentation.md`

### R14 — 

**Ruling.** Per window. ORGANIZE: foldenable == false, foldmethod == "manual", foldclosed(1) == -1. CAPTURE with frontmatter="fold": foldmethod == "manual" and the ONLY closed fold is the plugin's — foldclosed(1) == 1 AND foldclosed(<first body line>) == -1. CAPTURE with frontmatter="none": foldclosed(1) == -1. The plugin-created frontmatter fold is not the defect and must never be asserted away. Doc 18 lesson 2's check adopts the same shape.

*Affects:* `spec/15-capture-presentation.md`, `spec/18-teach-mode.md`

### R15 — 

**Ruling.** pinned = { "title", "summary", "timestamp", "context", "tags", "sources" } (the evidence doc's measured list, in that order). hidden gains "metadata". show_empty_pinned DEFAULTS TO FALSE. 15's literal-assertion test, 15 §9 acceptance 3 and 15 §2's constant-shape rationale are updated to match.

*Affects:* `spec/15-capture-presentation.md`, `spec/18-teach-mode.md`

### R16 — 

**Ruling.** By derivation from the record, never from `details`. keys = { k : capture.frontmatter_before.get(k) != capture.frontmatter_after.get(k) }, computed at undo time. A record whose frontmatter_after is null is not a meta_edit/tag_edit and refuses. Every reference to details["keys"] is struck from doc 17.

*Affects:* `spec/17-undo-and-action-history.md`

### R17 — 

**Ruling.** No blanket exemption. The precondition is VALUE-SCOPED PER KEY: undo proceeds only where current_frontmatter[k] == capture.frontmatter_after[k] for every derived key; any drifted key refuses the WHOLE action, naming the key and its current value. The body is not hashed, which is what makes 17's 'does not restore from the backup' coherent.

*Affects:* `spec/17-undo-and-action-history.md`

### R18 — 

**Ruling.** RETIRE FIRST, RESTORE SECOND: archive the organized copy, then verified-relocate the archived original back to its capture path. State the crash-interim invariant: a crash between the steps leaves the note only in the archive — recoverable, no live duplicate, no re-delivery. Pin with a kill-between-steps test. Acceptance test 1's collision-suffixed archive path is now correct as written.

*Affects:* `spec/17-undo-and-action-history.md`

### R19 — 

**Ruling.** No — so the rebuild is DEFERRED and computed OFF-LOCK. op.undo marks the corpus, returns within the 50 ms budget with learning: {"rebuilt": "scheduled"}, and queues the rebuild. The rebuild computes a new learning object outside the rwlock (a pure function of corpus + config); only the final swap and learn.save take the write lock. records_replayed/duration_ms move to the `organize learn rebuild` / learn-rebuild result. organize health reports learning as stale until it completes. 17 §9's obligation becomes: a concurrent suggest.for_note completes during the compute phase.

*Affects:* `spec/17-undo-and-action-history.md`

### R20 — 

**Ruling.** An undone accept is RETAINED in the denominator of top_accept_rate and rank_histogram; when the undo's actor is not human it additionally counts as a NEGATIVE. The 'dropped, not counted against the engine' adjudication is struck. 17 §3 gains a refusal: a machine actor may not undo any action it did not itself perform in the same run. 17 §9 gains a must-not-be-connected invariant: after N machine undos of machine actions, stats.top_accept_rate is never higher.

*Affects:* `spec/17-undo-and-action-history.md`

### R21 — 

**Ruling.** No. Delete 17 §4's 'the two must share one field, not invent two' sentence. Replacement: undo READS applied_via when deciding whether an action was human-decided, and records the undo's actor in the inverse record's own `actor`. Doc 17 issues no directive to the unexecuted Phase-6 ruling.

*Affects:* `spec/17-undo-and-action-history.md`

### R22 — 

**Ruling.** Doc 17 decides: YES, as an all-or-nothing group. A vault ring entry becomes { action_ids: [...], batch_size: n }. One `u` evaluates every member's preconditions FIRST (17 §2 invariant 1 applied across the group); if any member refuses, none is undone and the refusal names the member. One confirmation shows the literal count. Ring depth does not bound it — the entry holds ids, not depth. history.list entries gain batch_size. 16 §3.3 cites 17 §7 instead of deferring.

*Affects:* `spec/16-navigation-and-throughput.md`, `spec/17-undo-and-action-history.md`

### R23 — 

**Ruling.** Yes, client-locally. 17 §7's view snapshot gains `pinned_destination` and `batch_marks`. Toasts: 'undid: pin -> areas/health', 'undid: batch mark (12 captures)'. Acceptance: `P` then `u` leaves nothing pinned and issues zero RPCs. 16 §3.2 and §3.3 cite 17 §7.

*Affects:* `spec/16-navigation-and-throughput.md`, `spec/17-undo-and-action-history.md`

### R24 — 

**Ruling.** PYTHON spawns it, on both paths. `organize_core.teach.sandbox` spawns `organize serve --teach-root … --socket …` with the scrubbed env and IS added to SUBPROCESS_ALLOWED, with the house-shaped justification: its only spawn target is this program's own entry point, asserted structurally (argv[0] resolves to the running interpreter's `organize` entry point, shell=False, timeout= present, never a filesystem tool). The nvim client never spawns the teach core; it connects to the socket teach reports. 18 §6's 'absent from SUBPROCESS_ALLOWED' clause is struck and replaced with 'teach shells out to no external tool'.

*Affects:* `spec/18-teach-mode.md`

### R25 — 

**Ruling.** Only the `[teach]` table, only if the file exists, through a dedicated whitelisted loader `teach.load_teach_config(path) -> TeachConfig` whose return type structurally CANNOT carry a vault root or any CorePaths value (pinned by a test over the type's field set). No config file => shipped [teach] defaults => teach still runs. 18 §6's 'Teach never reads the user's config.toml' becomes 'Teach never reads the user's [vault], [state], [llm] or [consumers] config; it reads only [teach], through a loader that cannot return any other section.' Preflight condition 3 becomes 'the real [vault]/[state] values were never materialised'.

*Affects:* `spec/18-teach-mode.md`

### R26 — 

**Ruling.** Precedence, stated in 18 §1.1: `--sandbox-dir PATH > $ORGANIZE_TEACH_DIR > [teach] sandbox_dir > <tmpdir>/organize-teach-<uid>/`. The informational flag is renamed `--print-sandbox-dir` so `--sandbox-dir PATH` is unambiguously a setter. New preflight condition 5: the resolved sandbox root must be DISJOINT from the user's configured [vault] root, read via a dedicated `config.read_vault_root_only(path) -> Path | None` used solely for this assertion and never passed into the teach Config (pinned by a test). No config file => vault root undefined => the sandbox must additionally be under the system tmpdir or an explicitly-supplied --sandbox-dir directory that is empty or created by teach. Marker rule: teach may write `.organize-teach` only into a directory it created itself or an existing EMPTY directory. `$ORGANIZE_TEACH_DIR` is added to 14 §4.1's environment row and 14 §4.4's precedence chain gains an env rung.

*Affects:* `spec/14-built-for-others.md`, `spec/18-teach-mode.md`

### R27 — 

**Ruling.** All. 18 §1.3's chokepoint list is extended to include directory creation (fileops.move_to_destination, new_folder) and the progress.json writer — twelve in total — and 18 §8 mutation (b) becomes 'delete the confine.check call from EACH chokepoint in turn; every deletion must turn the suite red', with a twelve-row audit table. Mutations are added for preflight conditions 3, 4 and 5 and for the --clean guards, which today have none.

*Affects:* `spec/18-teach-mode.md`

### R28 — 

**Ruling.** No — replace it. `teach --clean` removes a tree only when: (a) a `.organize-teach` marker is READ FROM DISK at that root with a parseable schema_version and generator_version; (b) every removed path is_relative_to the sandbox root DERIVED FROM THE MARKER FILE'S OWN LOCATION; (c) no path beneath it is a symlink resolving outside it. The removal is logged. The max_generations reap runs only after these guards pass.

*Affects:* `spec/18-teach-mode.md`

### R29 — 

**Ruling.** Scope it: assert that no path under the decoy HOME, the decoy vault, or the real CorePaths locations (computed from the untouched env) was opened. An explicit read-allowlist — interpreter, stdlib, site-packages, install prefix, the resolved lessons_path — is pinned by a literal-set test asserting it is not widened. Separately, a lessons_path resolving under the user's configured vault root is refused at preflight.

*Affects:* `spec/18-teach-mode.md`

### R30 — 

**Ruling.** It must be. (i) The sandbox root is added to the handshake result. (ii) The teach client REFUSES to open any buffer whose resolve() is not under that root, and unloads any out-of-sandbox buffer before mounting. (iii) 18 §7 gains a headless-nvim keypress variant of the isolation test driving lessons 13/14/16 including `:w`, with the same decoy manifest. 18 §1.6's 'complete curriculum through the CLI check API' becomes 'the CLI-checkable curriculum through the check API, plus the ui-checked and buffer-writing lessons through the headless-nvim gate'.

*Affects:* `spec/18-teach-mode.md`

### R31 — 

**Ruling.** By the 09 §3 headless-nvim gate via state.describe(), NOT by the CLI check API. `organize teach --check <id>` returns {"kind":"ui","checkable":false,"reason":…} for those lessons rather than false. Acceptance 4 splits accordingly: corpus/vault/oplog/learning/session/config lessons via the CLI, ui lessons via the headless gate.

*Affects:* `spec/18-teach-mode.md`

### R32 — 

**Ruling.** SPLIT THE GATE. Keymap coverage stays mandatory in lessons and gains a new precondition test: every `teaches` entry must RESOLVE in actions.keymap_table() (that is the test that catches `hint_jump`). Subcommand coverage moves out of lessons into `test_every_subcommand_is_documented`, checked against README + doc/para-organize.txt under 14 §6's drift gate. The literal lesson-count assertion `23` is DELETED and replaced by structural pins. Named exemptions become cancel, stop, teach.

*Affects:* `spec/16-navigation-and-throughput.md`, `spec/18-teach-mode.md`

### R33 — 

**Ruling.** No. Config validation stays a PURE function of the TOML. `scripted` is an ordinary member of _LLM_BACKENDS; the refusal moves to USE TIME — llm.py raises when backend == "scripted" and confine.active() is false, and symmetrically for a non-scripted backend under confinement. Delete the confine.active() coupling from 18 §6.

*Affects:* `spec/18-teach-mode.md`

### R34 — 

**Ruling.** GRANTED HERE, once, for all three: `dest.recent` (16), `op.undo` and `history.list` (17). One justification shape for all: 10 §1 forbids a thin client from reading or writing vault state itself, and each capability already exists core-side with no wire surface. The ruling is recorded in ARCHITECTURE.md, and tests/test_server_protocol.py is edited ONCE by doc 17's seat. Also granted: errors.py gains UndoRefused (17) and TeachSandboxError/TeachConfinementError (18) as one consolidated taxonomy change; paths.py gains $ORGANIZE_TEACH_DIR and CorePaths.resolve(env=…) for doc 18. Both files remain architect-owned; these are the only grants.

*Affects:* `spec/16-navigation-and-throughput.md`, `spec/17-undo-and-action-history.md`, `spec/18-teach-mode.md`, `ARCHITECTURE.md`

### R35 — 

**Ruling.** TWO ROWS, not sixteen. ConcurrentModificationError is already on the RAISE side of ARCHITECTURE.md:445 and is not a deviation. The genuine deviation is 'the archived original is missing' and 'no backup exists' raising instead of ok=false + a FAILED oplog line. That narrowed deviation is GRANTED and must be recorded in ARCHITECTURE.md as well as in 17 §3.

*Affects:* `spec/17-undo-and-action-history.md`, `ARCHITECTURE.md`

### R36 — 

**Ruling.** spec/README.md:5 is rewritten to enumerate the real set: 10 (component split), 14 (principles, normative over all), 15 (03 §3's left-pane header list + ui.display.*), 16 (03 §2's default session ordering), 17 (05 §8's 'no automated undo' sentence). spec/05:55 gains a superseded marker naming doc 17, with the rest of 05 §8 standing. Doc 14 §Intro gains the tie-break: where 14 and a later doc name the same config key or seam, THE LATER DOC'S SPELLING WINS and 14's citation is corrected in the same change; 14 remains normative on principles, tiers and laws.

*Affects:* `spec/README.md`, `spec/05-file-operations.md`, `spec/14-built-for-others.md`

### R37 — 

**Ruling.** src/organize_core/config.py -> 14 (others submit CORE_KEYMAPS rows and [section] dataclasses as one consolidated patch). lua/para-organize/actions.lua -> 14 (others add rows via actions.register_key, never by editing CORE_KEYS). lua/para-organize/ui/render.lua -> 15, which lands the render.VIEWS[name]=fn registry FIRST as a standalone refactor; then 16 registers `mru`, 17 registers `history`. lua/para-organize/config.lua -> 15 (others append leaves after). lua/para-organize/ui.lua -> 15 (16 adds ui/hint.lua, 17 adds ui/undo.lua, 18 adds ui/teach.lua as NEW files). src/organize_core/server.py -> 17. src/organize_core/actions.py -> 17 (16's pinned/batch_size ride in the same trailing-defaulted batch as undoes; one to_json/from_json edit; one version bump). src/organize_core/learn.py -> 17. src/organize_core/fileops.py -> 18 (17 REUSES the verified relocator, never a fourth deleter; 14's actor default is a one-line patch). src/organize_core/cli.py -> 17. tests/test_server_protocol.py -> 17. tests/test_config.py -> 14. tests/test_repo_hygiene.py -> 18. errors.py / paths.py / ARCHITECTURE.md -> architect.

*Affects:* `spec/14-built-for-others.md`, `spec/15-capture-presentation.md`, `spec/16-navigation-and-throughput.md`, `spec/17-undo-and-action-history.md`, `spec/18-teach-mode.md`

### R38 — 

**Ruling.** Yes, but declared and budgeted. (a) 16 §3.7 gains a '⚠ Deviation from 03 §2' block in the shape 15 §1 and 17 §1 use, naming 03 §2's sentence, stating that oldest-first survives WITHIN each group so determinism is untouched, and that order = "oldest" restores 03's exact ordering. (b) The skip scan is bounded by a new core key `[session] seen_scan_months` (default 2), budgeted in 16 §8 (session.start < 200 ms on the 13,252-note / 1,862-capture corpus), with a LOUD fallback to "oldest" when the budget is exceeded. (c) `_SESSION_START_RESERVED` gains "order" and is named in 16 §5's wire-delta table.

*Affects:* `spec/16-navigation-and-throughput.md`

### R39 — 

**Ruling.** 14 §10 gains obligation 10.11: 'Build the Lua mirror gate (config.lua SCHEMA leaf => reader) and a Lua honored-key gate (default => behaviour A; non-default => behaviour B != A), parametrised over SCHEMA, before any doc-15/16/17/18 key ships', with its own mutation-audit line. 15 §10, 16 §8, 17 §9 and 18 §8 each cite it and each add one obligation enumerating that doc's keys so a reviewer can count rows against tests. Separately, 14 §10.1, 15 §10.5 and 17 §9 each append '…and a firing control at an adjacent parameter where the operation must succeed', completing the ARCHITECTURE.md:1516-1518 standard that 16 and 18 already quote in full.

*Affects:* `spec/14-built-for-others.md`, `spec/15-capture-presentation.md`, `spec/16-navigation-and-throughput.md`, `spec/17-undo-and-action-history.md`, `spec/18-teach-mode.md`

### R40 — 

**Ruling.** One shared constant, two callers. 14 §10.5's set is extended to (?i)\bmatt\b, matth, handzel, Obsidian/Main, server\.matthandzel\.com and UNIFIED with 18 §2's banned vocabulary as a single module constant imported by both gates; `share/` is added to 14's scanned paths. Three new work-list rows are added to 14 §3: A6 (is_matt_decided -> is_human_decided, eight call sites, Phase-6 firewall note travelling with it, owned by 14 not 17); D9 (consumer CODE defaults: learn.py deck/flashcard_dir/review_dir/_EXCLUDED, question_answer.py flashcard_review_dir, taskwarrior.py review_tag); B8 (deploy/README.md shim paths, errors.py:53, config.py:2020 NOTES_DIR).

*Affects:* `spec/14-built-for-others.md`, `spec/17-undo-and-action-history.md`, `spec/18-teach-mode.md`

### R41 — 

**Ruling.** It must, and doc 14 owns the fix. Two work-list rows: C8 — capture_folder/raw_capture_folder/archive_capture_path are DEFAULTS derived from para_folders, and a vault with no capture folder is a SUPPORTED configuration (a health note, never an error). F6 — the default session filter becomes `[session] default_filters`, whose shipped default is 'unfiled notes' defined WITHOUT reference to processing_status (notes outside all PARA folders); status=raw ships as an EXAMPLE. New acceptance criterion: a fixture vault with 40 plain notes, no frontmatter and no capture folder yields a non-empty first session.

*Affects:* `spec/14-built-for-others.md`

### R42 — 

**Ruling.** No — CUT it from the corpus. It becomes a session-local counter surfaced by `:ParaOrganize debug`; `organize actions stats` answers the throughput question from the existing durations_ms.decision. Delete it from 16 §3.9, from §5's wire-delta table, and from §8's must-not-be-connected list (pinned and batch_size stay there).

*Affects:* `spec/16-navigation-and-throughput.md`

### R43 — 

**Ruling.** `max_age_days = 0`, `0 = no age limit` (TOML has no null). The gate is exactly: refuse when `max_age_days > 0 and age_days > max_age_days`. The `null` spelling is deleted from 17 §5. 17 §9's constant list pins the literal 0 and the boundary, with a firing control (a record one day inside a max_age_days=30 window must undo).

*Affects:* `spec/17-undo-and-action-history.md`

## Declined findings

Recorded so nobody re-files them as new defects.

- **Lens 1 MAJOR-6 / MINOR m12 — move doc 18's lesson navigation off <C-n>/<C-p> because doc 16's vault-hint loop also claims them** — Not a collision. The f/F loop is modal by design (16 §1.1) and consumes EVERY key, including keys bound to actions — that is the stated mechanism, not a defect. Teach controls being unreachable inside the loop is correct behaviour. The real gaps are that the precedence is unwritten and the keys are unreserved; both are fixed (16 §6 states hint loop > teach narrator > pane keymaps; R9 reserves the five keys in CORE_KEYMAPS). Renaming to <M-n>/<M-p> costs muscle memory for no safety gain.
- **Lens 1 MINOR m4 — rename [teach] stall_hint_seconds to stall_hint_ms so 14 §4.2's duration rule is satisfied** — Wrong direction, and it does not generalise: undo.scan_months and undo.max_age_days would need the same treatment, producing scan_ms and max_age_ms — absurd for human-scale knobs a user types. The smaller correction is to amend 14 §4.2 to permit human-scale unit suffixes (_seconds/_days/_months) alongside _ms, which is ordered.
- **Lens 2 M5 (alternative) — ship session order = "oldest" as the default and make unseen_first Matt's config value** — The substance of unseen_first is right and 16 proves it: skipped captures stay raw and return to the front of the queue every session forever, so a 1,862-item backlog never converges. Shipping "oldest" preserves a defect for every user to protect a contract that can be satisfied more cheaply — by declaring the deviation and keeping oldest-first within each group (R38). Determinism, which is what 03 §2 actually requires, is untouched.
- **Lens 5 MAJ-2 (second half) — make show_empty_pinned default false only when NO pinned key is present in the frontmatter** — A default whose value depends on the data being rendered is not a default; it is undocumented branching, and it makes the two-sided honored-key test (14 §10.3) unwritable because there is no single 'default behaviour' to assert. Ruled instead as a plain show_empty_pinned = false (R15), with true available for users who want constant shape.
- **Lens 3 C7(ii) — 'a machine actor may never undo an action whose undo changes an accept-rate statistic'** — Circular and unimplementable: every undo of an accept changes an accept-rate statistic by construction, so the predicate refuses all machine undos of accepts while claiming to be narrower. The protection is delivered instead by the two implementable halves — retain the undone accept in the denominator and count a machine undo as a negative, plus refuse any machine undo of an action that machine did not itself perform in the same run (R20).
- **Lens 4 perf notes on 16 §8 — 'F filter re-render <= 16 ms is optimistic; needs a virtualised render' and 'folder.list depth=all < 200 ms cold is unlikely on 13k notes'** — Build-time, not spec-time. The budget IS the gate; a missed budget is a build finding against a stated number, which is exactly how it should surface. Mandating virtualisation in the spec fixes an implementation technique before anyone has measured whether it is needed. The one spec-shaped half — that the depth=all prefetch is async and never on the keypress path — is already normative in 16 §2 and stays.
- **Lens 1 MAJOR-2 (as cited) — ui.highlights.hint is the merge/review-gate instruction hint at ui.lua:562-565** — The cite is wrong: draw_hint uses hl.header, not hl.hint. The key's real uses are ui.lua:524 (dim secondary lines of the capture header) and integrate.lua:873 (the integrate hint). The collision concern survives the correction and is ruled on (R5), but the correction is recorded here so the fix pass does not write the wrong meaning into doc 15 or 16.
- **Lens 1 CRITICAL-3 (as cited) — the modifiable assignment is at lua/para-organize/ui.lua:559** — Line drift only: the assignment is at ui.lua:603-604 ('local writable = (view == "merge") or editing'). The substance is confirmed exactly as claimed and is ruled CRITICAL (R8); the line number is corrected here so work orders cite the tree as it stands.
- **Lens 5 CRIT-5(i) — the general principle that a spec doc may never be superseded by prose in another spec doc** — Half accepted, half declined. The spec/05 edit is ordered (R36), but the blanket principle is declined: this series is explicitly built that way (10, 14, 15, 16 and 17 all override earlier docs), and the workable rule is a precedence register plus a marker at each superseded line — not a prohibition that would require renumbering the whole series.
