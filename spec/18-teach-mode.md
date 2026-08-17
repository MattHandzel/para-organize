# 18 — Teach Mode: A Sandboxed First Run (NEW)

Directive from Matt (2026-08-16): *"can you make a ParaOrganize 'teach' or learn so I can learn how to use this program when i try the first time? it should be able to use a temporary database / files WITHOUT touching ANY data. NO DATA LOSS"*

Teach mode is an interactive tutorial that runs the **real** product against a **generated** vault inside a sandbox. Nothing about the lessons is simulated — every keystroke drives the actual keymaps, the actual core, the actual file operations — and nothing about the sandbox is real. It is also the first thing a stranger runs (14 §1), so it must work before any configuration exists. Doc 14 governs its vocabulary and zero-config promise; docs 15/16/17 supply the features several lessons teach.

## 1. The isolation guarantee

This section is the reason the document exists. Isolation is **mechanical and provable**, not careful.

### 1.1 The sandbox

One root, resolved by an **explicit precedence**:

```
--sandbox-dir PATH  >  $ORGANIZE_TEACH_DIR  >  [teach] sandbox_dir  >  <tmpdir>/organize-teach-<uid>/
```

Short by construction — a `sun_path` over `config.MAX_SOCKET_PATH` bytes makes the core refuse to bind, and the session scratch dir already blows it. Wherever the root resolves, it must clear preflight condition 5 (§1.2): it may never lie inside the user's configured vault. Four location sources with no stated ordering is exactly the wrong thing to leave vague. Layout:

```
<sandbox>/.organize-teach            marker: {schema_version, generator_version, created}
<sandbox>/home/                      the scrubbed HOME the teach core is given
<sandbox>/gen-<n>/vault/             the generated corpus (§2)
<sandbox>/gen-<n>/config/config.toml written by teach, never read from the user
<sandbox>/gen-<n>/state/             index.json, learning.json, operations.log, actions/, backups/
<sandbox>/run/t.sock, t.lock         the teach core's socket and single-instance lock
<sandbox>/progress.json              §4.4
```

**Python spawns the teach core, on both entry paths.** `organize_core.teach.sandbox` runs `organize serve --teach-root <sandbox> --socket <sandbox>/run/t.sock`, whether the user typed `organize teach` or `:ParaOrganize teach`. **The nvim client never spawns it** — it connects to the socket teach reports back through the handshake (§1.4), which is what keeps 10 §1's thin client thin and what lets acceptance 7.6 run with no Neovim in the process at all. The child is given a **scrubbed environment**: `ORGANIZE_CORE_{CONFIG,STATE,RUNTIME}_DIR` set to the generation's dirs, `XDG_{CONFIG,DATA,STATE,RUNTIME}_HOME` set **beneath the sandbox**, and `HOME` set to `<sandbox>/home/`. Unsetting the XDG vars is not sufficient and must never be done alone: `CorePaths.resolve`'s precedence is explicit > `ORGANIZE_CORE_*` > XDG > home, so removing the XDG channel *promotes* the home fallback (`~/.config/organize-core`, `~/.local/share/organize-core`) — the most real location there is. Redirecting `HOME` closes the last channel, and since `paths.expand` resolves both `~` and `$VARS` against the supplied env rather than the process environment, every derived path still lands inside the sandbox even if an `ORGANIZE_CORE_*` var is dropped by a bug. The builder additionally asserts the resolved socket path is under `config.MAX_SOCKET_PATH` — **the literal 104**, the same literal 14 §10.2 asserts, not a stricter teach-local number — and reports the measured length, because an over-long `sun_path` otherwise surfaces as an unattributable bind failure.

### 1.2 Belt — the precondition check

`teach.preflight(sandbox, config, paths)` runs before the core is spawned and refuses (`TeachSandboxError`, naming the offending path) unless **all** hold:

1. `sandbox.resolve()` carries the `.organize-teach` marker with a readable `schema_version`. A directory without the marker is never used and never removed.
2. `config.vault.root`, `paths.config_dir`, `paths.state_dir`, `paths.runtime_dir` each satisfy `resolved.is_relative_to(sandbox.resolve())`. **`is_relative_to`, never `startswith`** — a sibling `<sandbox>-scratch` passes a prefix test and must fail this one.
3. The *real* locations, computed by `CorePaths.resolve(env=default_env())` against the untouched process environment, are **disjoint** from every sandbox path, and **the real `[vault]`/`[state]` values were never materialised** on this code path — not merely "the file was not read", but that no object carrying them was ever constructed (§4.6 makes that structural rather than careful: the only loader teach may call cannot return them).
4. `config.llm.backend == "scripted"` (§6) and `config.consumers == []` — teach never runs the pipeline.
5. The resolved sandbox root is **disjoint from the user's configured `[vault]` root**, and so is the resolved `lessons_path` (§3). That vault root is obtained by `config.read_vault_root_only(path) -> Path | None` — a dedicated reader used *solely* for these two assertions and never passed into the teach `Config`, pinned by a test over its call sites. With **no config file at all** the vault root is undefined, so the sandbox must then additionally be either under the system tmpdir or an explicitly-supplied `--sandbox-dir` directory that is empty or was created by teach.

**Marker rule.** Teach may write `.organize-teach` only into a directory it created itself, or into an existing **empty** directory. A marker is never dropped into a populated tree — otherwise `--clean`'s marker guard (§1.5) can be handed authority over someone else's files by a single `mkdir`-free mistake.

Condition 5 exists because conditions 1–2 are satisfiable by a sandbox *inside* the real vault: they check that sandbox paths are under the sandbox, which is trivially true, and the user's vault root is not among `CorePaths`. Without it, `ORGANIZE_TEACH_DIR=~/vault/teach` passes every other guard, twelve generated captures land in a tree the real 10-minute pipeline (06) consumes, and `--clean` later removes a directory inside the vault. That is the literal failure of *"WITHOUT touching ANY data"*.

Preflight is a pure predicate over resolved paths. It is not "the code is careful"; it is a gate a test can drive with a hostile argument.

### 1.3 Braces — the runtime confinement guard

A process-level `organize_core.confine` module: `set_root(Path|None)`, `check(path, what)`, `active()`. `check` raises `TeachConfinementError` when a root is set and `path.resolve()` is not relative to it; when no root is set it is a no-op, so normal operation is byte-for-byte unaffected and the guard's *presence* is what a mutation audit removes. Every write chokepoint calls it first — **twelve of them**, and directory creation counts, because a `mkdir` outside the sandbox is a write:

| # | chokepoint | what escapes without the call |
|---|---|---|
| 1 | `fileops.atomic_write` | any file body |
| 2 | `fileops.backup_file` | a copy of a real note into a real backup dir |
| 3 | `fileops._archive_file` | a real note **moved** out from under the user |
| 4 | `fileops._discard_unverified_copy` | a real file **removed** |
| 5 | `fileops.move_to_destination`'s `Path.mkdir` | a fabricated PARA folder in the real vault |
| 6 | `fileops.new_folder`'s `Path.mkdir` | ditto, driven by the `new-folders` lesson |
| 7 | `OperationLog.append` | real `operations.log` lines |
| 8 | `ActionRecorder._append_line` | real corpus records |
| 9 | `index.flush` | a real `index.json` overwritten with sandbox contents |
| 10 | `learn.save` | a real `learning.json` overwritten with tutorial counts |
| 11 | the `progress.json` writer (§4.4) | a teach file written wherever the sandbox *wasn't* |
| 12 | `teach.sandbox` itself | the sandbox tree materialised outside its own root |

`organize serve --teach-root` sets the root before the first handler is registered.

This is deliberately redundant with `fileops.require_in_vault` (05 §1): that check confines writes to the configured vault, this one confines the **whole process** to the sandbox, so a bug that corrupts the vault root still cannot escape.

### 1.4 The real core is never reused

The handshake line (`{"apiVersion": 1}`, 10 §2) gains `"teach": <bool>` **and `"sandbox_root": <path>`**. Five consequences, all mechanical:

- The teach client connects only to `<sandbox>/run/t.sock` and **refuses to proceed if the handshake reports `teach: false`** — it cannot be fooled into driving the production core by an environment variable or a stale symlink.
- A production client refuses a core whose handshake reports `teach: true`, which is what stops a half-torn-down sandbox from silently serving a real session.
- `sandbox_root` is what puts the **nvim writer** inside the isolation proof. `confine` is a core-process guard, but 10 §4 makes the left pane a real file buffer and lesson `edit-the-note` teaches `:w` on it — so the client is the one write path the core guard cannot see. The teach client therefore **refuses to open any buffer whose `resolve()` is not under `sandbox_root`**, and **unloads any out-of-sandbox buffer before mounting**, so 05 §7's "save the buffer first, then operate" can never turn a stray buffer the user already had open into an automatic write to their real vault.
- The client never derives the sandbox root itself. It is handed one, by the process that proved it (§1.2), which is what keeps a thin client from re-deciding an isolation question.
- `:ParaOrganize teach` refuses to start while a real session is active (hint: `:ParaOrganize stop` first). One UI mount, one session — sharing it is a state hazard, not a feature.

### 1.5 Cleanup, crash, abort

Normal exit stops the teach core, removes `run/t.sock` and `run/t.lock`, and **keeps** the generation so §5 resume works. Crash or `SIGKILL` leaves the sandbox exactly as it was: the next `teach` validates the marker, clears the stale socket/lock, and resumes — teach never auto-deletes on failure, because deleting on the failure path destroys the evidence of what failed. `organize teach --clean` is the one removal path, and its guards are **evidence read off the disk, not state the same process just set**. `confine.active()` is explicitly *not* one of them: `confine.set_root` is called by this very process, so asking `confine.active()` afterwards proves only that the process called a setter — two of the three original guards were really one. The replacement: `--clean` removes a tree only when

1. a `.organize-teach` marker is **read from disk** at that root and parses, carrying a known `schema_version` and a `generator_version`;
2. every removed path `is_relative_to` the sandbox root **derived from that marker file's own location** — never from a caller-supplied argument, an env var, or a module global;
3. no path beneath it is a symlink whose `resolve()` leaves that root.

The removal is **logged** (root, file count, byte count, marker versions) before it happens. `--clean` has its own structural AST guard, `test_teach_only_clean_may_remove_a_path`, mirroring the never-delete guard in `tests/test_fileops_safety.py` rather than widening it, and each of the three guards carries its own mutation row in §8.

### 1.6 The test that proves the real vault was untouched

House real-data style (the originals-integrity manifest of `STRESS-TEST-REPORT.md` §Campaign 1):

```
test_teach_session_writes_nothing_outside_the_sandbox
```

Build a decoy home — a fake `~/.config/organize-core/config.toml`, a fake `~/.local/share/organize-core/` with an index and an action log, and a fake vault of 40 notes. The manifest covers the **union** of three trees, because a guard that only watches `$HOME` is blind to the two locations a real user is most likely to have: the **decoy home**, the **real `CorePaths` locations** computed from the untouched process env, and an **absolute-path decoy vault sited outside `$HOME`** (the shape of every vault on a second disk). Take `(relative_path, size, mtime_ns, sha256)` for **every** file under that union.

Then drive the curriculum. Not all of it goes through one door: **the CLI-checkable curriculum runs through the check API (§3), and the `ui`-checked and buffer-writing lessons run through the 09 §3 headless-nvim gate** — because driving the whole proof through the CLI is by construction blind to the one write path outside `confine` (§1.4). Re-manifest. Assert:

1. `after == before` as a set of 4-tuples, **and** the path sets are equal (a new file is a failure the digest comparison alone would miss).
2. Every `ActionRecord.capture.path` and `targets[].path` emitted during the run resolves inside the sandbox.
3. A firing control: the sandbox manifest **did** change, and its action log holds ≥ 12 records. Without this the test passes when teach does nothing at all.

A second variant discharges 14 §10.4(c) — **zero reads, not only zero writes**. The assertion is *scoped*, because an unscoped one is unsatisfiable and an unsatisfiable assertion gets quietly rewritten into whatever passes: the process must open the interpreter, the stdlib, site-packages and `share/teach/lessons.toml`, which by §3 lives in the install prefix. So: run the same curriculum with the decoy vault `chmod`ed read-only and watched (inotify, or an `open()` audit hook), and assert **no path under the decoy home, the decoy vault, or the real `CorePaths` locations (computed from the untouched env) was opened**. The permitted reads are an explicit allowlist — interpreter, stdlib, site-packages, install prefix, and the resolved `lessons_path` — pinned by a **literal-set test asserting the allowlist is not widened**, so "make the test pass" cannot mean "add the failing path to the allowlist".

A third variant is the same test driven by **keypresses**: the headless-nvim gate replays lessons `metadata`, `edit-the-note` and `merge` — **including `:w`** — against the identical decoy manifest. Those three are the buffer-writing lessons; if the client-side refusal of §1.4 is absent, this is the test that goes red.

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

**Stranger-friendly is a gate, not an aspiration.** `test_teach_corpus_contains_no_personal_vocabulary` greps the generated tree *and* the shipped lesson text for a banned list — `impro`, `theatre`, `workout`, `productivity-system`, `principle`, `not_reviewed`, `zettelkasten`, `dailies`, `life-logging`, `Obsidian/Main`, `(?i)\bmatt\b`, `matth`, `handzel`, `server.matthandzel.com`, `Syncthing`, `NixOS`, `Taskwarrior`, `Anki` — and fails on any hit.

**This list and 14 §10.5's origin-user string set are ONE module constant with two callers**, so the repo-wide de-Matt-ification gate can never end up weaker than the tutorial gate — which is the backwards outcome two independently-maintained lists produce. The constant carries the two gates' **scopes explicitly**, because they differ: several banned words (`workout`, `impro`, `theatre`, `productivity-system`) are live and legitimate **EXAMPLE-tier** strings inside `src/` under 14's tiering, and must stay legal there while remaining banned in generated corpus and lesson prose. One constant, two scoped callers — not two lists. Two more structural tests: `test_teach_corpus_is_generated_not_copied` AST-walks `teach/corpus.py` for `shutil.copy*`, `copytree`, `read_text`, `read_bytes`, `Path.home`, `expanduser`, `os.environ` and requires zero; and `test_teach_corpus_is_byte_reproducible` builds twice into different roots and compares digests.

## 3. Lessons

A lesson is data, not code — `share/teach/lessons.toml`, overridable via `[teach] lessons_path = "<path>"` (the `_path` suffix is 14 §4.2's rule, not decoration), so a curriculum is translatable and forkable by a file swap (a 14 §5 extension seam, with that section's stability contract):

```toml
[[lesson]]
id = "accept-a-move"
title = "Filing a note"
goal = "Send the clamp-sizes note to areas/woodworking."
teaches = ["accept", "next_suggestion", "prev_suggestion"]   # ACTION names, never key literals
check = { kind = "action_log", operation = "move", destination = "areas/woodworking" }
hint = "The top suggestion is already selected. Press {accept}."
```

`teaches` holds **action names**, and nothing else — no key literals, no prose, no citations. A sibling lesson-schema boolean `teaches_metadata = true` covers **every `metadata_fields` row at once**, because those rows are the user's own and cannot be enumerated in a shipped file; the narrator renders `metadata_fields[*].keymap` for them. The narrator renders the user's actual `lhs` for everything else by resolving `actions.keymap_table()` at display time. A lesson may therefore never train a key a user's own config has claimed — the live tables are the source of truth, and the names used below for features specified in 15/16/17 bind to whatever those docs actually name their rows (the coverage gate below forces the alignment, and its resolution half is what catches a name no row carries). `test_no_lesson_hardcodes_a_key_literal` enforces the no-literals rule, and any teach binding colliding with a core or metadata keymap is a `:checkhealth para-organize` failure, never a keypress that quietly does the wrong thing.

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

**`ui` is the other exception, and it is not the core's to evaluate.** A headless core cannot observe which pane has focus, so `ui` checks are evaluated **by the 09 §3 headless-nvim gate via `state.describe()`** (`lua/para-organize/state.lua:229`). `organize teach --check <id>` on a `ui` lesson therefore returns `{"kind": "ui", "checkable": false, "reason": …}` — **never `false`**, because an unevaluable check reporting failure is indistinguishable from a failing one, and **nine of the lessons below carry a `ui` check**: reporting `false` for all nine is how a third of the curriculum quietly stops being covered. A **compound** check (lesson `undo` carries `action_log` + `vault` + `ui`) is evaluated by both harnesses — the core halves through the CLI, the `ui` half through the gate — and passes only when both agree.

### The curriculum

| # | id | goal | teaches | check |
|---|---|---|---|---|
| 1 | `welcome` | what the sandbox is; where it lives; that your real files are not here | — | `ack` |
| 2 | `two-panes` | left is the real note buffer, right is where you decide; `?` recovers everything | focus_capture, focus_organize, help | `ui`: both panes visited, `foldmethod == "manual"` on both, `foldclosed(<first body line>) == -1` in the capture pane, help shown (15 §1/§3) |
| 3 | `reading-a-capture` | pinned fields vs. the frontmatter underneath (15 §2) | cycle_fields | `ui`: `state.field_mode` visited `full` and `raw`, then returned to `compact` |
| 4 | `why-this-ranking` | every suggestion carries a reason; scores are not magic | next_suggestion, prev_suggestion, next, prev, toggle_preview, refresh | `ui`: selection moved off rank 1; reasons rendered |
| 5 | `accept-a-move` | file the clamp note | accept | `action_log`: `move` → `areas/woodworking` |
| 6 | `what-just-happened` | the original was archived byte-for-byte, the *filed copy* was tagged, and both were logged — nothing was deleted | — | `vault`: the archived original's frontmatter is **byte-identical to the pre-move capture** (`processing_status: raw`, no `area/…` tag) **and** the filed copy carries `processing_status: organized` and the new tag; `oplog`: `archive` + `move` lines |
| 7 | `undo` | take it back, redo it, then find it in history (17 §7) | undo, redo, history | `action_log`: an `undo` record; `vault`: capture back at its original path; `ui`: history view opened |
| 8 | `skipping` | skipping is a decision the system records, not a silence | skip | `session`: `skipped == 1`; `action_log`: `skip` |
| 9 | `hint-jump` | jump straight to a folder by its label, and vault-wide with the capital (16 §1) | hint_row, hint_vault | `ui`: browse path equals the labelled target |
| 10 | `browsing` | descend, go back, open a note | accept, back | `ui`: stack depth ≥ 2, then back at root |
| 11 | `sort-cycle` | three orderings, and when each helps | sort_cycle | `ui`: all three modes visited |
| 12 | `search` | scoped inside a folder, vault-wide outside one | search | `ui`: query returned the planted note and it was opened |
| 13 | `metadata` | tag it, rate it — annotating without filing is a valid outcome (07) | `teaches_metadata = true` | `vault`: on-disk frontmatter gained `importance` and a tag |
| 14 | `edit-the-note` | the left pane is a real buffer; `:w` saves and the core re-reads | — | `vault`: the typed line is on disk *and* `note.get` returns it (the core re-read it). **Not an `oplog` check** — `index.full_reindex` writes no operation-log line; only operations do |
| 15 | `archiving` | the zero-signal note has nowhere to go, and that's fine | archive | `vault`: `03-passing-thought` under `archive/capture/raw_capture/` |
| 16 | `merge` | fold a capture into an existing note by hand | merge, merge_complete, merge_cancel | `vault`: target contains the text, capture archived, backup exists |
| 17 | `routes` | a tag can have a standing destination (11 §1) — accept the suggestion carrying the `[→]` route marker | accept | `action_log`: `append` with `route` set |
| 18 | `review-gate` | proposed edits are shown as a diff before they apply (12 §1) | integrate, integrate_edit, integrate_mode, merge_complete, merge_cancel | `action_log`: `integrate` with a recorded `verdict` |
| 19 | `refusals` | `no-ai`, unparseable YAML, and why loud beats clever | — | `vault`: `05-private-journal` hash unchanged; `06-half-written` hash unchanged; both refusals surfaced with their `error.data.kind` (`NoAiRefusal`, `FrontmatterError`) |
| 20 | `throughput` | repeat the last destination, pin one, mark several, file them all at once (16 §2) | repeat_destination, pin_destination, mark_capture, batch_all, batch_clear, numeric_accept | `action_log`: ≥ 3 moves in one batch to one destination |
| 21 | `recent-and-filter` | the destinations you just used are one key away, and a session can be narrowed without restarting it (16 §3) | mru, filter_session | `ui`: the `mru` view opened and a destination was taken from it; the session filter changed the queue length |
| 22 | `auto-organize` | what the machine will file without you, what it will never touch, and how the trust ladder earns its way up (13 §1) | — | `action_log`: an auto-applied `move` whose `actor` is not human, with `applied_via` recorded; `vault`: the below-threshold capture was left where it was |
| 23 | `new-folders` | make a home that doesn't exist yet, and describe it | new_project, new_area, new_resource | `vault`: folder exists with a description (11 §3) |
| 24 | `diagnostics` | `:ParaOrganize debug`, `:checkhealth`, `organize reindex` when something looks stale | — | `session`: debug snapshot produced; index stats returned |
| 25 | `done` | how to quit, how to resume, and the real paths teach never touched — plus where captures come from (**the capture app is not required**; any note outside your PARA folders is fair game, 14 §6), where state lives and how to delete all of it, and how to upgrade both halves from the same tag | — | `ack` |

**Teach mode doubles as the acceptance checklist**, and that is enforced rather than asserted — but the gate is **split in two**, because bindings and subcommands are different obligations and fusing them made one unsatisfiable gate out of two satisfiable ones:

- **Keymap coverage stays a lesson obligation.** `test_every_binding_is_taught` requires every row of `actions.keymap_table()` to appear in some lesson's `teaches` list, with `teaches_metadata = true` covering every `metadata_fields` row at once. Explicit, named exemptions only: **`cancel`, `stop`, `teach`** — the first two covered by the `done` lesson in prose, the third being the thing you are already inside. Ship a new binding without a lesson and the suite goes red.
- **A new precondition test guards the other direction.** `test_every_teaches_entry_resolves` requires every `teaches` entry to **resolve in `actions.keymap_table()`**. That is the test that catches a plausible-looking name like `hint_jump`, `sticky` or `multi_select` that no row actually carries — a `teaches` list of fictions satisfies a coverage gate in one direction while teaching nothing.
- **Subcommand coverage is documentation coverage, not tutorial coverage**, and moves out of lessons entirely into `test_every_subcommand_is_documented`, which checks `commands.SUBCOMMANDS` against `README` + `doc/para-organize.txt` under 14 §6's drift gate. `organize reindex` is documented, not dramatised.

There is deliberately **no literal lesson-count assertion**. A number pins the curriculum at a size that cannot satisfy the coverage gate as the gate grows; the structural pins above are what a curriculum change must survive.

## 4. UX

### 4.1 Presentation

**No third pane** — 03 §3's 50/50 geometry is a parity contract and the left pane is a real file buffer (10 §4) that may never receive added buffer lines. The narrator is a **separate bottom-anchored float**: a scratch `para-organize://teach` buffer in its own `NS_TEACH` namespace, `nomodifiable`, `buftype=nofile`, non-focusable by default, layout-width and 5–9 lines tall. Both panes keep their exact geometry and the thin-client law is untouched — the narrator renders text the core supplied. `ui.teach.narrator = "float" | "virt" | "none"`; `virt` draws the lesson as `virt_lines` above the organize content for short terminals, `none` gives a bare sandbox.

### 4.2 Progress

`Lesson 5/25  ▓▓▓▓░░░░░░` in the narrator header, and the organize pane's border title becomes `" Organize — 5/25 "` (the denominator is `#lessons`, rendered — never a constant). A completed check flashes the goal line and auto-advances after 1.5 s (`ui.teach.advance_delay_ms`, `0` disables auto-advance).

### 4.3 Stalls, skipping, repeating, quitting

Two-stage hints: after `[teach] stall_hint_seconds` (default 45) with the check still false, the lesson's `hint` appears dimmed; after 3× that, the resolved literal keys are spelled out. `<C-g>` shows the hint immediately. Teach's own controls are **view-scoped** — bound and unbound by the teach module the way the review gate binds its rows (12 §1) so they cannot leak into a normal session — and **pane-scoped**, which is the separate question view-scoping does not answer: *view*-scoping says **when** a key is bound, never **which buffer** it is bound in. All five bind in the organize pane only. They are rebindable via `keymaps.teach.*`:

| key | action | panes |
|---|---|---|
| `<C-n>` / `<C-p>` | next / previous lesson (skip / repeat) | `{"organize"}` |
| `<C-g>` | show the hint now | `{"organize"}` |
| `<C-r>` | replay this lesson from a fresh generation (§5) | `{"organize"}` |
| `<C-q>` | quit teach mode | `{"organize"}` |

`panes = {"organize"}` is not a nicety. In the **capture pane** `<C-r>` is Vim's redo and `<C-g>` is file info, and the `edit-the-note` lesson has the user typing into exactly that buffer: a teach control bound there would silently eat a redo in a real file buffer. Pinned by a test mirroring 17 §8.8 — focus the capture pane during `edit-the-note`, type, press `<C-r>`: **Vim redoes and no teach action fires.**

**Modal precedence** is stated so a consumed key is documented behaviour rather than a collision: **hint loop (16 §1) > teach narrator > pane keymaps**. While the `f`/`F` `getcharstr` loop is running it owns every keystroke, so `<C-n>`/`<C-p>` pressed inside it label-cycle or abort rather than changing lesson; the loop's abort path returns control to the narrator.

The five controls are **reserved in `CORE_KEYMAPS`** by doc 14's single consolidated patch (`<C-n>`, `<C-p>`, `<C-g>`, `<C-r>`, `<C-q>` → `teach_next`, `teach_prev`, `teach_hint`, `teach_replay`, `teach_quit`). That reservation is what makes §3's `:checkhealth para-organize` claim true **at config-validation time** rather than at the first collision: a user's `[[metadata_fields]] keymap = "<C-g>"` is refused when the config is read, not discovered when a lesson misbehaves.

`<Esc>` and `:ParaOrganize stop` also exit. On exit the teach core stops, the socket and lock are removed, progress is saved, and the closing message names the real config, state and vault paths **and states that teach wrote to none of them**.

### 4.4 Persistence and resume

`<sandbox>/progress.json`: `{schema_version, corpus_fingerprint, generation, lesson_index, completed[], hints_shown{}, started_at, updated_at}`. `corpus_fingerprint` binds progress to the corpus that produced it — if the generator version changes, progress is invalidated with a notice rather than silently resuming into a different vault. If the sandbox is gone (a `/tmp` cleared by a reboot), teach says so plainly and starts at lesson 1; `[teach] sandbox_dir` pins a durable location for anyone who wants resume across reboots — subject to §1.2's condition 5 like every other rung of the precedence.

### 4.5 Entry and exit

`:ParaOrganize teach [lesson-id]` — a new row in `commands.SUBCOMMANDS` (`action = "teach"`, `takes = "lesson"`), plus `<Plug>ParaOrganizeTeach`. The CLI mirror is what makes the curriculum testable headlessly: `organize teach --list | --print <id> | --check <id> | --reset | --clean | --sandbox-dir <path> | --print-sandbox-dir`, each honoring `--json`.

`--sandbox-dir <path>` is a **setter** — the top rung of §1.1's precedence. The purely informational flag is named `--print-sandbox-dir`, so no one can read one spelling as the other; a flag that both sets and reports is how a tutorial ends up writing where it was only asked to look.

**First-run auto-offer.** On the first `:ParaOrganize start` where `<state_dir>/.teach-offered` is absent **and** the index is empty, notify once — *"First run: `:ParaOrganize teach` walks you through it in a sandbox; your files are not touched. This offer won't repeat."* — then write the marker. It never blocks, never prompts modally, never enters teach by itself. Disabled by `ui.teach.auto_offer = false`. The empty-index condition means an existing install is never offered.

### 4.6 Which side each knob lives on

Per 10 §3, behavior lives in the core config so CLI and UI cannot disagree; the `setup()` table keeps UI-only concerns. **Core `[teach]`**: `sandbox_dir`, `corpus_seed`, `lessons_path`, `stall_hint_seconds`, `max_generations` — all of them are read by `organize teach --check` with no Neovim in the process, so none can live client-side. **Nvim `ui.teach`**: `narrator`, `advance_delay_ms`, `auto_offer`, and `keymaps.teach.*` — a headless CLI has no first run to offer and no float to place.

**Each `[teach]` key below has exactly one reader and none may sit in `RESERVED_CONFIG_LEAVES`; each `ui.teach.*` key has an honored-key test in `tests/plugin/teach_spec.lua`**, built on the Lua mirror gate and Lua honored-key gate that 14 §10.11 obligates *before any doc-15/16/17/18 key ships* — `RESERVED_CONFIG_LEAVES` is the reserved-but-not-yet-read list, so a key that is both honored and reserved asserts P and ¬P, and `ui.teach.*` could not appear in a Python allowlist at all.

| key | the one reader |
|---|---|
| `[teach] sandbox_dir` | `teach.sandbox.resolve_root` — rung 3 of §1.1's precedence |
| `[teach] corpus_seed` | `teach.corpus.build(root, seed=…)` (§2) |
| `[teach] lessons_path` | `teach.lessons.load` — and the resolved path is preflight-checked against the vault root (§1.2 cond. 5) |
| `[teach] stall_hint_seconds` | the stall timer in `teach.session` (§4.3), both stages |
| `[teach] max_generations` | the reap in §5, which runs only after §1.5's guards pass |

**Honored obligations on the nvim side**, one row per key, countable against the tests: `ui.teach.narrator` must be honored in **all three** of its modes — `"float"` draws the bottom-anchored float, `"virt"` draws `virt_lines` above the organize content, `"none"` draws nothing and the sandbox still runs — **including the nui-missing fallback** (§6), where `"float"` degrades to `"virt"` with a warning and the test asserts the warning *and* the `virt_lines` extmark. `ui.teach.advance_delay_ms` gets a default-vs-non-default pair (`0` disables auto-advance; a positive value advances after it), and `ui.teach.auto_offer` gets the same (`true` notifies once and writes the marker; `false` never notifies). Every one of these is a distinct behaviour A ≠ behaviour B, not merely a key that parses.

## 5. Reset and replay

An existing sandbox **is reused** across sessions when its marker validates and its `corpus_fingerprint` matches; otherwise a new generation is created. Reset never overwrites in place: `teach --reset` and `<C-r>` create `gen-<n+1>/` and repoint, so a crash mid-reset cannot lose the previous generation. The precise claim — because the blanket one is not true once generations are capped — is: **no generation is ever overwritten in place; the only removal is the explicitly-guarded oldest-generation reap of machine-generated data, which is audited and logged.** Replay of a single lesson restores the vault by generating a fresh generation and fast-forwarding the recorded effects of the completed prefix through the same RPC calls the user would have made — deterministic, because the corpus is seeded.

Generations are capped at `[teach] max_generations` (default 5). Exceeding it removes the **oldest** generation through `teach --clean`'s single guarded path (§1.5) — one auditable deleter, no unbounded growth and no hostile refusal to start. **The reap runs only after all three §1.5 guards pass** (marker read from disk, every path under the marker-derived root, no escaping symlink) and is logged like any other removal. This matters more than the manual path does: the reap is the one deletion that happens *automatically during a normal run*, so it is the one that must not inherit a weaker guard than the flag the user typed. It carries its own row in §8's mutation list.

## 6. Built for others

Teach mode is a stranger's first contact with the product (14 §1), so:

- **No config required.** Teach never reads the user's `[vault]`, `[state]`, `[llm]` or `[consumers]` config. It reads **only the `[teach]` table**, and only if the file exists, through `teach.load_teach_config(path) -> TeachConfig` — a loader whose return type **structurally cannot carry a vault root or any `CorePaths` value**, pinned by a test over the type's field set rather than by an implementer's care. With no config file at all, the shipped `[teach]` defaults apply and teach still runs. Teach then writes its own config inside the sandbox and loads that. A machine with no `~/.config/organize-core/` at all runs teach successfully. *(The five `[teach]` keys of §4.6 have to be read from somewhere, or they are dead keys under 03 §1 — which this document itself cites. A whitelisted loader with a type that cannot express the dangerous answer is how both sentences are true at once.)* *(Adjudication against 14 §2: "organize-core never runs on implicit defaults" is LAW and teach does not break it — teach's core loads an explicit config file at an explicit path, which it authored a moment earlier. `organize teach` is therefore the one subcommand exempt from the missing-config refusal, and it is exempt because it supplies a config rather than because it guesses one. `organize init` names it as the next step.)*
- **No vault required.** §2 generates one.
- **No LLM, no network.** The sandbox config sets `[llm] backend = "scripted"` — a new deterministic backend returning canned proposals, so lesson 18 teaches the review gate with no server anywhere. The pairing is symmetric and both halves refuse loudly, **at use time, not at validation time**: `scripted` is an ordinary member of `_LLM_BACKENDS`, and `llm.py` raises when `backend == "scripted"` and `confine.active()` is false, and symmetrically when a non-`scripted` backend is used under confinement. **Config validation stays a pure function of the TOML.** Branching a closed-schema validator on process-global state breaks its purity, and it also deadlocks this document: `teach.preflight` receives a validated `Config` produced in the **parent**, where `confine.active()` is `False` — so a validator that rejected `scripted` there would mean preflight could never run at all.
- **One spawn, and it is this program.** `organize_core.teach.sandbox` spawns the teach core and is therefore **on `SUBPROCESS_ALLOWED`**, with the house-shaped justification: its only spawn target is this program's own `organize serve`, asserted **structurally** — `argv[0]` resolves to the running interpreter's `organize` entry point, `shell=False`, `timeout=` present, never a filesystem tool. **Teach shells out to no external tool.** ("Spawns its own core" and "absent from the allowlist" cannot both be true, and `tests/test_repo_hygiene.py` fails any unlisted module that so much as references `subprocess`.)
- **No personal vocabulary.** Enforced by the banned-word gate in §2 over both corpus and lesson text. Lesson prose says "your vault", "a folder you file things in" — never a folder name from anyone's real setup.
- **Degrades, never crashes.** Missing nui ⇒ the narrator falls back to `virt` mode with a warning; missing telescope ⇒ lesson 12 uses `vim.ui.select`; a terminal under 80×24 ⇒ narrator height clamps and the hint truncates with `…`.

## 7. Acceptance tests

1. **Isolation.** `test_teach_session_writes_nothing_outside_the_sandbox` (§1.6) — the CLI-checkable curriculum driven headlessly, the three-tree manifest byte-identical before and after by path set and digest, sandbox provably changed.
2. **Preflight refuses a real vault.** Point `[vault] root` at the decoy vault with everything else sandboxed: `TeachSandboxError` names the path, no core is spawned, no file is created. Repeat with a `<sandbox>-scratch` sibling — the prefix-passing, `is_relative_to`-failing case must also refuse.
   **Hostile sandbox locations (§1.2 cond. 5), each its own refusal test with a firing control:** `--sandbox-dir <vault>/teach`; `ORGANIZE_TEACH_DIR=<vault>/teach`; `[teach] sandbox_dir = "<vault>/teach"`; a `lessons_path` under the vault root; and — with **no config file at all** — a `--sandbox-dir` pointing at a populated directory outside the tmpdir. Each must refuse before anything is created, and the firing control is the same invocation against a tmpdir root, which must succeed. **Marker rule:** teach refuses to write `.organize-teach` into a non-empty directory it did not create.
3. **The production core is not reused.** With a real core listening on `$XDG_RUNTIME_DIR/organize-core.sock`, teach spawns its own core on the sandbox socket, and a client handed the real socket refuses on `teach: false` without sending a single request.
4. **Every lesson check is satisfiable and honest**, in the harness that can actually evaluate it. For each lesson: driving its documented actions turns the check true, and *not* driving them leaves it false. A check that is true before the lesson runs is a defect. **Split by kind:** `vault`/`action_log`/`oplog`/`learning`/`session`/`config`/`ack` lessons are driven through the CLI check API; **`ui` lessons are driven through the 09 §3 headless-nvim gate**, and `organize teach --check` on one of them must return `{"kind": "ui", "checkable": false, …}` — never `false`, which the same test asserts, because an unevaluable check reporting failure is how nine lessons quietly stop being covered. A compound-check lesson is driven in both harnesses and passes only when both agree.
5. **Coverage, in three parts.** Every `actions.keymap_table()` row appears in some lesson's `teaches` (with `teaches_metadata` covering the `metadata_fields` rows), minus the **three** named exemptions `cancel`, `stop`, `teach`; every `teaches` entry **resolves** in `actions.keymap_table()`; and every `commands.SUBCOMMANDS` name is documented in `README` + `doc/para-organize.txt` (14 §6), not taught.
6. **Zero config, zero vault, zero network.** `organize teach --check welcome` succeeds with `HOME` pointed at an empty directory, no `config.toml` anywhere, and outbound sockets stubbed to raise — while `organize suggest` in the same environment still refuses per 14 §2, proving the exemption is scoped to `teach` and did not become a general default.
7. **Reset and resume.** Complete 5 lessons, kill the process with `SIGKILL`, restart: teach resumes at lesson 6 with the sandbox intact. Then `--reset` produces `gen-2`, leaves `gen-1` on disk, and restarts at lesson 1. A sixth generation removes `gen-1` and nothing else.
8. **Crash leaves nothing dangerous.** `SIGKILL` mid-move: the next start clears the stale socket and lock, the marker still validates, no path outside the sandbox changed.
9. **The nvim writer is inside the proof.** The §1.6 keypress variant: the headless-nvim gate replays `metadata`, `edit-the-note` and `merge` — **including `:w`** — against the identical three-tree decoy manifest, with the same before/after assertion and the same firing control. Paired negative: pre-open a buffer on a decoy-vault note, then mount teach — the client **unloads it and refuses**, and the decoy note's digest is unchanged after a `:w` is attempted.
10. **Teach controls do not steal the capture pane.** Focus the capture pane during `edit-the-note`, type a line, press `<C-r>`: Vim redoes, the buffer changes accordingly, and **no teach action fires** (lesson index unchanged, no fresh generation). Same shape for `<C-g>`, which must show file info. Mirrors 17 §8.8.
11. **The teach controls are reserved.** A user config with `[[metadata_fields]] keymap = "<C-g>"` is refused at **config validation**, naming the dotted key and the reserved binding — not at first keypress. This is the test that makes §3's `:checkhealth` claim true, and it depends on doc 14's consolidated `CORE_KEYMAPS` patch having landed the five teach rows.

## 8. Test obligations (anti-vacuity)

Per the permanent standards in `ARCHITECTURE.md` § *Test anti-vacuity standards*:

- **Mutation-audit the isolation guard — mandatory before handback.** Each mutation MUST turn the suite red. A green suite under any of these means the isolation is untested, and the isolation is the whole document.
  - (a) make `confine.check` return unconditionally;
  - (b) **delete the `confine.check` call from EACH of the twelve chokepoints in turn** — twelve separate mutations, twelve separate red suites. Auditing only `atomic_write` proves one row of twelve, and leaves `_archive_file` and `_discard_unverified_copy` — the two that **move** and **remove** files — able to lose their call with a green suite;
  - (c) swap `is_relative_to` for `str.startswith` in both `confine.check` and `teach.preflight`;
  - (d) drop the `teach` flag from the handshake;
  - (e) drop `sandbox_root` from the handshake, and separately delete the client-side out-of-sandbox buffer refusal (§1.4);
  - (f) **one mutation per preflight condition 3, 4 and 5** — make each return `True` unconditionally — plus one that lets `read_vault_root_only`'s result reach the teach `Config`;
  - (g) **one mutation per `--clean` guard** (§1.5): trust a caller-supplied root instead of the marker-derived one; skip the marker parse; skip the symlink-escape check. Repeat (g) against the `max_generations` reap path, which today has none;
  - (h) remove `teach.sandbox` from `SUBPROCESS_ALLOWED` — the hygiene gate must go red, which is what proves the allowlist entry is load-bearing rather than decorative.

  The twelve-row (b) audit is recorded as a table in the handback, one row per chokepoint, each naming the test that went red.
- **Refusal-predicate pins.** Assert refusal one component outside the sandbox (`<sandbox>/../x.md`) and at the prefix-sibling (`<sandbox>-scratch/x.md`), each paired with a firing control inside the sandbox that must succeed — a guard that refuses everything passes a refusal test alone.
- **Constant assertions.** Assert the literal `"scripted"`, the literal marker filename `".organize-teach"`, and the literal socket ceiling `104` (`config.MAX_SOCKET_PATH`, the same literal 14 §10.2 asserts); separately assert the module constants agree with those literals. Flipping a constant must not leave the suite green. **There is deliberately no literal lesson-count assertion** — it is replaced by the structural pins of §3 (every keymap row taught, every `teaches` entry resolving, `ui` lessons reporting `checkable: false`), because a number pins the curriculum at a size that cannot satisfy its own coverage gate. Also pinned as a literal set: §1.6's read-allowlist, asserted **not widened**.
- **Must-not-be-connected invariants.** Pin the *connection*, not today's consequence: the teach `OperationContext` has `consumers == []` and the teach process holds no client for the production socket. A wired-but-currently-harmless connection survives a consequence-only check.
- **Firing controls on every "nothing happened" assertion.** Any test asserting a tree is unchanged must, in the same test, assert a tree that *should* have changed did.
- **Config-key gate (14 §10.11).** This document's keys ride the Lua mirror gate and the Lua honored-key gate that 14 §10.11 obligates *before any doc-15/16/17/18 key ships* — no doc-18 key may land ahead of it. **Eight rows, countable against the tests:** core `[teach] sandbox_dir`, `corpus_seed`, `lessons_path`, `stall_hint_seconds`, `max_generations`; nvim `ui.teach.narrator` (three modes plus the nui-missing fallback), `ui.teach.advance_delay_ms`, `ui.teach.auto_offer`. Each is a default-vs-non-default pair where behaviour A ≠ behaviour B (§4.6), and each carries a mutation line: delete the reader, the pair must stop differing.
- **Lesson drift guard.** `lessons.toml` embeds `organize …` invocations and key-action names; both are drift-checked against the live CLI parser and `keymap_table()` — the pattern established for doc-embedded CLI invocations by the deep_research seat's drift guard (`ARCHITECTURE.md` § *Phase-3 rulings, addendum*).
