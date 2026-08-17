# 18 — Teach Mode: A Sandboxed First Run (NEW)

Directive from Matt (2026-08-16): *"can you make a ParaOrganize 'teach' or learn so I can learn how to use this program when i try the first time? it should be able to use a temporary database / files WITHOUT touching ANY data. NO DATA LOSS"*

Teach mode is an interactive tutorial that runs the **real** product against a **generated** vault inside a sandbox. Nothing about the lessons is simulated — every keystroke drives the actual keymaps, the actual core, the actual file operations — and nothing about the sandbox is real. It is also the first thing a stranger runs (14 §1), so it must work before any configuration exists. Doc 14 governs its vocabulary and zero-config promise; docs 15/16/17 supply the features several lessons teach.

## 1. The isolation guarantee

This section is the reason the document exists. Isolation is **mechanical and provable**, not careful.

### 1.1 The sandbox

One root, `$ORGANIZE_TEACH_DIR` else `<tmpdir>/organize-teach-<uid>/` (short by construction — a `sun_path` over ~104 bytes makes the core refuse to bind, and the session scratch dir already blows it). Layout:

```
<sandbox>/.organize-teach            marker: {schema_version, generator_version, created}
<sandbox>/gen-<n>/vault/             the generated corpus (§2)
<sandbox>/gen-<n>/config/config.toml written by teach, never read from the user
<sandbox>/gen-<n>/state/             index.json, learning.json, operations.log, actions/, backups/
<sandbox>/run/t.sock, t.lock         the teach core's socket and single-instance lock
<sandbox>/progress.json              §4.4
```

The teach core is spawned as `organize serve --teach-root <sandbox> --socket <sandbox>/run/t.sock` with a **scrubbed environment**: `ORGANIZE_CORE_{CONFIG,STATE,RUNTIME}_DIR` set to the generation's dirs, `XDG_{CONFIG,DATA,STATE,RUNTIME}_HOME` set **beneath the sandbox**, and `HOME` set to `<sandbox>/home/`. Unsetting the XDG vars is not sufficient and must never be done alone: `CorePaths.resolve`'s precedence is explicit > `ORGANIZE_CORE_*` > XDG > home, so removing the XDG channel *promotes* the home fallback (`~/.config/organize-core`, `~/.local/share/organize-core`) — the most real location there is. Redirecting `HOME` closes the last channel, and since `paths.expand` resolves both `~` and `$VARS` against the supplied env rather than the process environment, every derived path still lands inside the sandbox even if an `ORGANIZE_CORE_*` var is dropped by a bug. The builder additionally asserts the resolved socket path is under 100 bytes and reports the measured length, because an over-long `sun_path` otherwise surfaces as an unattributable bind failure.

### 1.2 Belt — the precondition check

`teach.preflight(sandbox, config, paths)` runs before the core is spawned and refuses (`TeachSandboxError`, naming the offending path) unless **all** hold:

1. `sandbox.resolve()` carries the `.organize-teach` marker with a readable `schema_version`. A directory without the marker is never used and never removed.
2. `config.vault.root`, `paths.config_dir`, `paths.state_dir`, `paths.runtime_dir` each satisfy `resolved.is_relative_to(sandbox.resolve())`. **`is_relative_to`, never `startswith`** — a sibling `<sandbox>-scratch` passes a prefix test and must fail this one.
3. The *real* locations, computed by `CorePaths.resolve(env=default_env())` against the untouched process environment, are **disjoint** from every sandbox path, and the real config file was not read during preflight.
4. `config.llm.backend == "scripted"` (§6) and `config.consumers == []` — teach never runs the pipeline.

Preflight is a pure predicate over resolved paths. It is not "the code is careful"; it is a gate a test can drive with a hostile argument.

### 1.3 Braces — the runtime confinement guard

A process-level `organize_core.confine` module: `set_root(Path|None)`, `check(path, what)`, `active()`. `check` raises `TeachConfinementError` when a root is set and `path.resolve()` is not relative to it; when no root is set it is a no-op, so normal operation is byte-for-byte unaffected and the guard's *presence* is what a mutation audit removes. Every write chokepoint calls it first: `fileops.atomic_write`, `backup_file`, `_archive_file`, `_discard_unverified_copy`, `OperationLog.append`, `ActionRecorder._append_line`, `index.flush`, `learn.save`, and `teach.sandbox` itself. `organize serve --teach-root` sets the root before the first handler is registered.

This is deliberately redundant with `fileops.require_in_vault` (05 §1): that check confines writes to the configured vault, this one confines the **whole process** to the sandbox, so a bug that corrupts the vault root still cannot escape.

### 1.4 The real core is never reused

The handshake line (`{"apiVersion": 1}`, 10 §2) gains `"teach": <bool>`. Three consequences, all mechanical:

- The teach client connects only to `<sandbox>/run/t.sock` and **refuses to proceed if the handshake reports `teach: false`** — it cannot be fooled into driving the production core by an environment variable or a stale symlink.
- A production client refuses a core whose handshake reports `teach: true`, which is what stops a half-torn-down sandbox from silently serving a real session.
- `:ParaOrganize teach` refuses to start while a real session is active (hint: `:ParaOrganize stop` first). One UI mount, one session — sharing it is a state hazard, not a feature.

### 1.5 Cleanup, crash, abort

Normal exit stops the teach core, removes `run/t.sock` and `run/t.lock`, and **keeps** the generation so §5 resume works. Crash or `SIGKILL` leaves the sandbox exactly as it was: the next `teach` validates the marker, clears the stale socket/lock, and resumes — teach never auto-deletes on failure, because deleting on the failure path destroys the evidence of what failed. `organize teach --clean` is the one removal path and it is guarded three ways (marker present, every removed path under the sandbox root, `confine.active()`); it has its own structural AST guard, `test_teach_only_clean_may_remove_a_path`, mirroring the never-delete guard in `tests/test_fileops_safety.py` rather than widening it.

### 1.6 The test that proves the real vault was untouched

House real-data style (the originals-integrity manifest of `STRESS-TEST-REPORT.md` §Campaign 1):

```
test_teach_session_writes_nothing_outside_the_sandbox
```

Build a decoy home — a fake `~/.config/organize-core/config.toml`, a fake `~/.local/share/organize-core/` with an index and an action log, and a fake vault of 40 notes. Take a manifest of `(relative_path, size, mtime_ns, sha256)` for **every** file under it. Drive the complete curriculum through the CLI check API (§3), not through keypresses. Re-manifest. Assert:

1. `after == before` as a set of 4-tuples, **and** the path sets are equal (a new file is a failure the digest comparison alone would miss).
2. Every `ActionRecord.capture.path` and `targets[].path` emitted during the run resolves inside the sandbox.
3. A firing control: the sandbox manifest **did** change, and its action log holds ≥ 12 records. Without this the test passes when teach does nothing at all.

A second variant discharges 14 §10.4(c) — **zero reads, not only zero writes**: run the same curriculum with the decoy vault `chmod`ed read-only and watched (inotify, or an `open()` audit hook), and assert not one path outside the sandbox was opened at all.

## 2. The sample corpus

`organize_core.teach.corpus.build(root, *, seed)` — **generated, never copied.** Deterministic: fixed seed, fixed timestamps, no `datetime.now()`, so the same generator version always produces the same bytes and the manifest in §1.6 is reproducible. Structure: `capture/raw_capture/`, `projects/{apartment-move,book-club}/`, `areas/{woodworking,finances}/`, `resources/{recipes,woodworking-reference}/`, `archive/capture/raw_capture/` (**`archive` singular**, 02).

Twelve captures, *shaped* like real captures per `doc/FEEDBACK-EVIDENCE-2026-08-16.md` §1–2 — the duplicated identity keys (`timestamp`/`id`/`aliases[1]`/`capture_id`), an empty `tags: []`, a five-key nested `location` map — with entirely invented content about woodworking and a book club:

| capture | shape | what it exists to teach |
|---|---|---|
| `01-clamp-sizes` | full current schema, `tags: [woodworking, tools]` | an obvious destination; accept |
| `02-book-club-pick` | `tags: [book-club]`, `context: [reading]` | project destination; route match |
| `03-passing-thought` | `tags: []`, `sources: []`, `context: []` | **zero-signal** — honestly yields only "Archive Now" (04 §7) |
| `04-glue-up-notes` | second woodworking capture | repeat destination — learning visibly re-ranks |
| `05-private-journal` | `no-ai: true` | the refusal lesson |
| `06-half-written` | unterminated YAML + a duplicate key with differing values | loud parse refusal (09 §1.5) |
| `07-older-schema` | `title`, scalar `tags: recipes`, `source`, `date` | scalar-vs-list; the `title` pin (15) |
| `08-hardware-list` | `tags: [todo]` | metadata edit, then skip |
| `09-rate-notes` | `tags: [finances]` | search and browse |
| `10-finish-tests` | 90-line body | the compact→full→raw toggle (15) |
| `11-crlf-note` | CRLF line endings | byte-exact handling |
| `12-both-topics` | `tags: [woodworking, book-club]` | competing destinations; multi-select (16) |

Plus `areas/woodworking/workbench-log.md` as a merge/append/integrate target, `index.md` descriptions on two folders (11 §3), and one route in the sandbox config (`tags = ["book-club"] → projects/book-club/reading-list.md`, `mode = "append"`).

**Stranger-friendly is a gate, not an aspiration.** `test_teach_corpus_contains_no_personal_vocabulary` greps the generated tree *and* the shipped lesson text for a banned list — `impro`, `theatre`, `workout`, `productivity-system`, `principle`, `not_reviewed`, `zettelkasten`, `dailies`, `life-logging`, `Obsidian/Main`, `matth`, `handzel`, `server.matthandzel.com`, `Syncthing`, `NixOS`, `Taskwarrior`, `Anki` — and fails on any hit. Two more structural tests: `test_teach_corpus_is_generated_not_copied` AST-walks `teach/corpus.py` for `shutil.copy*`, `copytree`, `read_text`, `read_bytes`, `Path.home`, `expanduser`, `os.environ` and requires zero; and `test_teach_corpus_is_byte_reproducible` builds twice into different roots and compares digests.

## 3. Lessons

A lesson is data, not code — `share/teach/lessons.toml`, overridable via `[teach] lessons = "<path>"`, so a curriculum is translatable and forkable by a file swap (a 14 §5 extension seam, with that section's stability contract):

```toml
[[lesson]]
id = "accept-a-move"
title = "Filing a note"
goal = "Send the clamp-sizes note to areas/woodworking."
teaches = ["accept", "next_suggestion", "prev_suggestion"]   # ACTION names, never key literals
check = { kind = "action_log", operation = "move", destination = "areas/woodworking" }
hint = "The top suggestion is already selected. Press {accept}."
```

`teaches` holds **action names**; the narrator renders the user's actual `lhs` by resolving `actions.keymap_table()` at display time, and metadata lessons render `metadata_fields[*].keymap`. A lesson may therefore never train a key a user's own config has claimed — the live tables are the source of truth, and the names used below for features specified in 15/16/17 bind to whatever those docs actually name their rows (the coverage test in §3 forces the alignment). `test_no_lesson_hardcodes_a_key_literal` enforces the no-literals rule, and any teach binding colliding with a core or metadata keymap is a `:checkhealth para-organize` failure, never a keypress that quietly does the wrong thing.

**Checks are assertions against sandbox state, never keypress counts.** Kinds, all but `ui` evaluated by the core so the curriculum is testable without Neovim (10 §2):

| kind | predicate |
|---|---|
| `vault` | a path exists / is absent / a frontmatter key holds a value (via `note.get`, `search.query`) |
| `action_log` | an `ActionRecord` since lesson start matching `{operation, actor, destination, verdict}` |
| `oplog` | an `operations.log` line of a given type since lesson start |
| `learning` | `learning.json` holds a destination with `count >= n` |
| `session` | session counters reached a value (`processed`, `skipped`) |
| `config` | the sandbox `config.toml` gained a key |
| `ui` | client-reported: focused pane, `state.view`, selection index, expand level, sort mode |
| `ack` | the narrator was advanced — **narrative lessons only** |

`ack` is the loophole, so it is bounded: only the first and last lesson may use it, pinned by `test_only_the_bookend_lessons_use_ack`.

### The curriculum

| # | id | goal | teaches | check |
|---|---|---|---|---|
| 1 | `welcome` | what the sandbox is; where it lives; that your real files are not here | — | `ack` |
| 2 | `two-panes` | left is the real note buffer, right is where you decide; `?` recovers everything | focus_capture, focus_organize, help | `ui`: both panes visited, `foldenable=false` on both (15 §1), help shown |
| 3 | `reading-a-capture` | pinned fields vs. the 23 lines of frontmatter underneath | cycle_fields (15 §2) | `ui`: `state.field_mode` visited `full` and `raw`, then returned to `compact` |
| 4 | `why-this-ranking` | every suggestion carries a reason; scores are not magic | next_suggestion, prev_suggestion, toggle_preview, refresh | `ui`: selection moved off rank 1; reasons rendered |
| 5 | `accept-a-move` | file the clamp note | accept | `action_log`: `move` → `areas/woodworking` |
| 6 | `what-just-happened` | the original was archived, tagged, and logged — nothing was deleted | — | `vault`: archived original has `processing_status: organized`; `oplog`: `archive` + `move` lines |
| 7 | `undo` | take it back, redo it, then find it in history (17 §7) | undo, redo, history | `action_log`: an `undo` record; `vault`: capture back at its original path; `ui`: history view opened |
| 8 | `skipping` | skipping is a decision the system records, not a silence | skip | `session`: `skipped == 1`; `action_log`: `skip` |
| 9 | `hint-jump` | jump straight to a folder by its label (16 §1) | hint_jump | `ui`: browse path equals the labelled target |
| 10 | `browsing` | descend, go back, open a note | accept, back | `ui`: stack depth ≥ 2, then back at root |
| 11 | `sort-cycle` | three orderings, and when each helps | sort_cycle | `ui`: all three modes visited |
| 12 | `search` | scoped inside a folder, vault-wide outside one | search | `ui`: query returned the planted note and it was opened |
| 13 | `metadata` | tag it, rate it — annotating without filing is a valid outcome (07) | metadata fields | `vault`: on-disk frontmatter gained `importance` and a tag |
| 14 | `edit-the-note` | the left pane is a real buffer; `:w` saves and the core re-reads | — | `vault`: the typed line is on disk *and* `note.get` returns it (the core re-read it). **Not an `oplog` check** — `index.full_reindex` writes no operation-log line; only operations do |
| 15 | `archiving` | the zero-signal note has nowhere to go, and that's fine | archive | `vault`: `03-passing-thought` under `archive/capture/raw_capture/` |
| 16 | `merge` | fold a capture into an existing note by hand | merge, merge_complete, merge_cancel | `vault`: target contains the text, capture archived, backup exists |
| 17 | `routes` | a tag can have a standing destination (11 §1) | accept on `[→]` | `action_log`: `append` with `route` set |
| 18 | `review-gate` | proposed edits are shown as a diff before they apply (12 §1) | integrate, integrate_edit, merge_complete, merge_cancel | `action_log`: `integrate` with a recorded `verdict` |
| 19 | `refusals` | `no-ai`, unparseable YAML, and why loud beats clever | — | `vault`: `05-private-journal` hash unchanged; `06-half-written` hash unchanged; both refusals surfaced with their `error.data.kind` (`NoAiRefusal`, `FrontmatterError`) |
| 20 | `throughput` | repeat destination, sticky batch, multi-select, numeric accept (16 §2) | repeat_destination, sticky, multi_select, numeric accept | `action_log`: ≥ 3 moves in one batch to one destination |
| 21 | `new-folders` | make a home that doesn't exist yet, and describe it | new_project, new_area, new_resource | `vault`: folder exists with a description (11 §3) |
| 22 | `diagnostics` | `:ParaOrganize debug`, `:checkhealth`, `reindex` when something looks stale | reindex | `session`: debug snapshot produced; index stats returned |
| 23 | `done` | how to quit, how to resume, and the real paths teach never touched | — | `ack` |

**Teach mode doubles as the acceptance checklist**, and that is enforced rather than asserted: `test_every_binding_and_subcommand_is_taught` requires that every row of `actions.keymap_table()` and every name in `commands.SUBCOMMANDS` appears in some lesson's `teaches` list (explicit, named exemptions only — `cancel` and `stop`, which lesson 23 covers in prose). Ship a new binding without a lesson and the suite goes red.

## 4. UX

### 4.1 Presentation

**No third pane** — 03 §3's 50/50 geometry is a parity contract and the left pane is a real file buffer (10 §4) that may never receive added buffer lines. The narrator is a **separate bottom-anchored float**: a scratch `para-organize://teach` buffer in its own `NS_TEACH` namespace, `nomodifiable`, `buftype=nofile`, non-focusable by default, layout-width and 5–9 lines tall. Both panes keep their exact geometry and the thin-client law is untouched — the narrator renders text the core supplied. `ui.teach.narrator = "float" | "virt" | "none"`; `virt` draws the lesson as `virt_lines` above the organize content for short terminals, `none` gives a bare sandbox.

### 4.2 Progress

`Lesson 5/23  ▓▓▓▓░░░░░░` in the narrator header, and the organize pane's border title becomes `" Organize — 5/23 "`. A completed check flashes the goal line and auto-advances after 1.5 s (`ui.teach.advance_delay_ms`, `0` disables auto-advance).

### 4.3 Stalls, skipping, repeating, quitting

Two-stage hints: after `[teach] stall_hint_seconds` (default 45) with the check still false, the lesson's `hint` appears dimmed; after 3× that, the resolved literal keys are spelled out. `<C-g>` shows the hint immediately. Teach's own controls are **view-scoped** — bound and unbound by the teach module the way the review gate binds its rows (12 §1) so they cannot leak into a normal session — and are rebindable via `keymaps.teach.*`:

| key | action |
|---|---|
| `<C-n>` / `<C-p>` | next / previous lesson (skip / repeat) |
| `<C-g>` | show the hint now |
| `<C-r>` | replay this lesson from a fresh generation (§5) |
| `<C-q>` | quit teach mode |

`<Esc>` and `:ParaOrganize stop` also exit. On exit the teach core stops, the socket and lock are removed, progress is saved, and the closing message names the real config, state and vault paths **and states that teach wrote to none of them**.

### 4.4 Persistence and resume

`<sandbox>/progress.json`: `{schema_version, corpus_fingerprint, generation, lesson_index, completed[], hints_shown{}, started_at, updated_at}`. `corpus_fingerprint` binds progress to the corpus that produced it — if the generator version changes, progress is invalidated with a notice rather than silently resuming into a different vault. If the sandbox is gone (a `/tmp` cleared by a reboot), teach says so plainly and starts at lesson 1; `[teach] sandbox_dir` pins a durable location for anyone who wants resume across reboots.

### 4.5 Entry and exit

`:ParaOrganize teach [lesson-id]` — a new row in `commands.SUBCOMMANDS` (`action = "teach"`, `takes = "lesson"`), plus `<Plug>ParaOrganizeTeach`. The CLI mirror is what makes the curriculum testable headlessly: `organize teach --list | --print <id> | --check <id> | --reset | --clean | --sandbox-dir`, each honoring `--json`.

**First-run auto-offer.** On the first `:ParaOrganize start` where `<state_dir>/.teach-offered` is absent **and** the index is empty, notify once — *"First run: `:ParaOrganize teach` walks you through it in a sandbox; your files are not touched. This offer won't repeat."* — then write the marker. It never blocks, never prompts modally, never enters teach by itself. Disabled by `ui.teach.auto_offer = false`. The empty-index condition means an existing install is never offered.

### 4.6 Which side each knob lives on

Per 10 §3, behavior lives in the core config so CLI and UI cannot disagree; the `setup()` table keeps UI-only concerns. **Core `[teach]`**: `sandbox_dir`, `corpus_seed`, `lessons`, `stall_hint_seconds`, `max_generations` — all of them are read by `organize teach --check` with no Neovim in the process, so none can live client-side. **Nvim `ui.teach`**: `narrator`, `advance_delay_ms`, `auto_offer`, and `keymaps.teach.*` — a headless CLI has no first run to offer and no float to place. Every key is honored or absent (03 §1); `RESERVED_CONFIG_LEAVES` covers the new leaves.

## 5. Reset and replay

An existing sandbox **is reused** across sessions when its marker validates and its `corpus_fingerprint` matches; otherwise a new generation is created. Reset never overwrites in place: `teach --reset` and `<C-r>` create `gen-<n+1>/` and repoint, so a crash mid-reset cannot lose the previous generation and **the never-delete law holds inside teach too**. Replay of a single lesson restores the vault by generating a fresh generation and fast-forwarding the recorded effects of the completed prefix through the same RPC calls the user would have made — deterministic, because the corpus is seeded.

Generations are capped at `[teach] max_generations` (default 5). Exceeding it removes the **oldest** generation through `teach --clean`'s single guarded path (§1.5) — one auditable deleter, three preconditions, no unbounded growth and no hostile refusal to start.

## 6. Built for others

Teach mode is a stranger's first contact with the product (14 §1), so:

- **No config required.** Teach never reads the user's `config.toml`; it writes its own inside the sandbox and loads that. A machine with no `~/.config/organize-core/` at all runs teach successfully. *(Adjudication against 14 §2: "organize-core never runs on implicit defaults" is LAW and teach does not break it — teach's core loads an explicit config file at an explicit path, which it authored a moment earlier. `organize teach` is therefore the one subcommand exempt from the missing-config refusal, and it is exempt because it supplies a config rather than because it guesses one. `organize init` names it as the next step.)*
- **No vault required.** §2 generates one.
- **No LLM, no network.** The sandbox config sets `[llm] backend = "scripted"` — a new deterministic backend returning canned proposals, so lesson 18 teaches the review gate with no server anywhere. The pairing is symmetric and both halves refuse loudly: `scripted` is rejected by config validation when `confine.active()` is false, and any other backend is rejected when it is true. Teach is also absent from `SUBPROCESS_ALLOWED` — it shells out to nothing but its own `organize serve`.
- **No personal vocabulary.** Enforced by the banned-word gate in §2 over both corpus and lesson text. Lesson prose says "your vault", "a folder you file things in" — never a folder name from anyone's real setup.
- **Degrades, never crashes.** Missing nui ⇒ the narrator falls back to `virt` mode with a warning; missing telescope ⇒ lesson 12 uses `vim.ui.select`; a terminal under 80×24 ⇒ narrator height clamps and the hint truncates with `…`.

## 7. Acceptance tests

1. **Isolation.** `test_teach_session_writes_nothing_outside_the_sandbox` (§1.6) — full curriculum driven headlessly, decoy home byte-identical before and after by path set and digest, sandbox provably changed.
2. **Preflight refuses a real vault.** Point `[vault] root` at the decoy vault with everything else sandboxed: `TeachSandboxError` names the path, no core is spawned, no file is created. Repeat with a `<sandbox>-scratch` sibling — the prefix-passing, `is_relative_to`-failing case must also refuse.
3. **The production core is not reused.** With a real core listening on `$XDG_RUNTIME_DIR/organize-core.sock`, teach spawns its own core on the sandbox socket, and a client handed the real socket refuses on `teach: false` without sending a single request.
4. **Every lesson check is satisfiable and honest.** For each of the 23 lessons: driving its documented actions turns the check true, and *not* driving them leaves it false. A check that is true before the lesson runs is a defect.
5. **Coverage.** Every `actions.keymap_table()` row and every `commands.SUBCOMMANDS` name appears in some lesson's `teaches`, minus the two named exemptions.
6. **Zero config, zero vault, zero network.** `organize teach --check welcome` succeeds with `HOME` pointed at an empty directory, no `config.toml` anywhere, and outbound sockets stubbed to raise — while `organize suggest` in the same environment still refuses per 14 §2, proving the exemption is scoped to `teach` and did not become a general default.
7. **Reset and resume.** Complete 5 lessons, kill the process with `SIGKILL`, restart: teach resumes at lesson 6 with the sandbox intact. Then `--reset` produces `gen-2`, leaves `gen-1` on disk, and restarts at lesson 1. A sixth generation removes `gen-1` and nothing else.
8. **Crash leaves nothing dangerous.** `SIGKILL` mid-move: the next start clears the stale socket and lock, the marker still validates, no path outside the sandbox changed.

## 8. Test obligations (anti-vacuity)

Per the permanent standards in `ARCHITECTURE.md` § *Test anti-vacuity standards*:

- **Mutation-audit the isolation guard — mandatory before handback.** Four mutations, each of which MUST turn the suite red: (a) make `confine.check` return unconditionally; (b) delete the `confine.check` call from `atomic_write`; (c) swap `is_relative_to` for `str.startswith` in both `confine.check` and `teach.preflight`; (d) drop the `teach` flag from the handshake. A green suite under any of these means the isolation is untested, and the isolation is the whole document.
- **Refusal-predicate pins.** Assert refusal one component outside the sandbox (`<sandbox>/../x.md`) and at the prefix-sibling (`<sandbox>-scratch/x.md`), each paired with a firing control inside the sandbox that must succeed — a guard that refuses everything passes a refusal test alone.
- **Constant assertions.** Assert the literal `"scripted"`, the literal marker filename `".organize-teach"`, and the literal lesson count `23` against the shipped `lessons.toml`; separately assert the module constants agree with those literals. Flipping a constant must not leave the suite green.
- **Must-not-be-connected invariants.** Pin the *connection*, not today's consequence: the teach `OperationContext` has `consumers == []` and the teach process holds no client for the production socket. A wired-but-currently-harmless connection survives a consequence-only check.
- **Firing controls on every "nothing happened" assertion.** Any test asserting a tree is unchanged must, in the same test, assert a tree that *should* have changed did.
- **Lesson drift guard.** `lessons.toml` embeds `organize …` invocations and key-action names; both are drift-checked against the live CLI parser and `keymap_table()` — the pattern established for doc-embedded CLI invocations by the deep_research seat's drift guard (`ARCHITECTURE.md` § *Phase-3 rulings, addendum*).
