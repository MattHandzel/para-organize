# 14 — Built for Others (PRINCIPLES — governs every other doc)

Directive from Matt (2026-08-16): *"I want this to be an extension that potentially others can use. ensure that 'building for others to use' is part of the spec. so this allow what im telling you to be configurable."* and *"(ensure you store every additional feature i tell you able in the SPEC)"*.

Third directive from the same batch, quoted with its truncation intact because it is the general shape of the rule this doc generalises: *"allow me to look at all metadata fields, but"* [message ends mid-sentence — read as: **every** field stays reachable on demand, only the important ones show by default]. That is progressive disclosure; doc 15 owns its design, and §1's Two-Users Test is why it must be config rather than one user's taste hardcoded into a renderer.

This doc is **normative over every other doc in this series, earlier and later**. Docs 01–13 describe one vault, one keyboard, one tag vocabulary; they remain the behavioral contracts, but wherever any doc states a fact about Matt as if it were a fact about the software, **this doc's neutral rule wins and the Matt fact demotes to a config default or an example**. Docs 15+ are written under it from the start. It adds no features; it constrains how every feature is built. Nothing here licenses a behavior change that an earlier doc forbids — §8 in particular only tightens.

**Tie-break.** Where this doc and a later doc name the same **config key or seam**, **the later doc's spelling wins**, and this doc's citation is corrected in the same change rather than left to drift. 14 remains normative on **principles, tiers and laws** — the Two-Users Test, the three tiers, the four-part obligation, the privacy laws — and those a later doc may not override. This is the register under which doc 15's deletion of `ui.display`, its renderer seams and its score thresholds are settled: **the surface's owner names the key; this doc assigns its tier.**

---

## 1. Two users, three tiers

**The software has exactly two users and must be designed against both simultaneously.**

| | **Matt** (origin user) | **The stranger** (installs it tomorrow) |
|---|---|---|
| Vault | `~/Obsidian/Main`, Syncthing-synced Obsidian, ~2,300-file backlog | anywhere; maybe a git-synced plain Markdown tree with 40 notes |
| Folders | `projects/ areas/ resources/ archive/` (singular) | `1-Projects/ 2-Areas/ …`, or German, or non-PARA-shaped |
| Tags | kebab-case, `impro`, `workout`, `todo`, `productivity-system` | anything, including `CamelCase` and none at all |
| Frontmatter | the capture-app schema of 02 | `title:`/`kind:`/`status:`, or no frontmatter at all |
| Stack | NixOS, Taskwarrior, Anki, Ollama on a home server | macOS, no Taskwarrior, no LLM, no server |
| Relationship to the code | wrote the requirements | read the README once |

**The Two-Users Test — apply it to every line before writing it:** *would this line still be correct if the vault were `~/vault`, the folders `1-Projects/`, the tags German, and the frontmatter keys different?* If no, the line is not a behavior — it is a **default**, and it belongs in config with a name, a documented meaning, and a test that the override is honored (§4).

Every user-visible fact therefore lands in exactly one of three tiers, and the tier must be decidable by reading the code:

| Tier | Meaning | Where it lives | May a user change it? |
|---|---|---|---|
| **LAW** | true for all vaults, all users, forever | code, with the spec §ref in the docstring | No — changing it is a spec change |
| **DEFAULT** | one reasonable value out of many | a `field(default=…)` / `ui.DEFAULTS` entry, named in the config schema | Yes, via config |
| **EXAMPLE** | one user's actual value, shown to teach | `EXAMPLE_CONFIG_TOML`, README, spec prose | Never loaded — an example that is also a default is a bug |

Worked examples of the tier split: "originals are archived, never deleted" is LAW (05 §1). "The archive folder is called `archive`" is DEFAULT (`config.py:63-70`). "`resources/performing/` is where improv notes go" is EXAMPLE (11 §1). The three are currently mixed — §3 is the work list that separates them.

**Matt is user zero, not a special case.** *(Design note: this is the load-bearing half of the principle. Anything Matt needs that a stranger cannot express through the public config surface is a missing config key, not a privilege. His live config must be producible by a stranger who reads only the README — that is the cheapest continuous proof that the surface is complete, and it is why §3 demotes his vault out of the shipped defaults rather than merely documenting them.)*

⚠ **Deviation from 01 §"The user"**, which reads "There is exactly one user: Matt". Resolution: 01 keeps Matt's profile as the **origin user's workload description** — it is the empirical basis for the throughput targets (09 §4), the fixture scale, and the default tuning — and stops being a statement about who may run the software. Every later doc that says "Matt" as a role (12 §2's `actor` enum, 13 §2's confirmations, 07's "Matt can add/remove fields freely") reads as "the human at the keyboard".

---

## 2. The zero-config promise

**A fresh install with no config file must either work or refuse with an actionable message. It must never guess.** Guessing is what produced the origin system's worst outage: the plugin was pointed at a near-empty subfolder, indexed the wrong tree, and reported success (01 §"Why a rewrite"). A wrong vault root that runs is strictly worse than a missing vault root that refuses.

| Situation | Required behavior |
|---|---|
| `organize <anything>` **except `init` and `teach`** with no config file | **Refuse.** `ConfigError` naming the expected path plus "`organize health --example-config` prints a complete commented example" — the current `load_config` text (`config.py:1454-1460`) is the standard; "organize-core never runs on implicit defaults" is LAW. |
| `[vault] root` absent from an otherwise valid config | **Refuse**, naming the key. `root` is the **only** required key with no default — decision: every other key in the schema ships a working default, so a two-line config is a complete config. |
| `vault.root` set but nonexistent / not a directory / not writable | **Refuse at load and at `organize health`**, one issue per condition, with the path in the message (`config.check_vault`). |
| A `para_folders` entry names a folder that does not exist | **`health` error**, naming key, configured value and resolved path, with the singular/plural hint. Never auto-create — a silently created second archive tree is the `archive`/`archives` defect (02) with a friendlier face. |
| `require("para-organize").setup()` with **no argument** | **Works.** Every plugin key has a default, including `socket_path` (`config.default_socket_path()`) and `core_cmd`. `setup({})` and `setup()` are identical and neither may error. |
| Core not installed / not on `$PATH` / spawn fails | **Clear error naming the missing binary and the install command; no crash, no stack trace** (10 §1). `:checkhealth para-organize` says the same thing in more words. |
| No `$XDG_RUNTIME_DIR`, or socket path > 104 bytes | Fall back / refuse with the reason — already handled (`config.MAX_SOCKET_PATH`); this row exists so the fallback stays tested. |
| Vault configured, but zero captures match the session filters | **Not an error.** Notify, no UI (03 §6 / the "empty session is active-with-empty-list" resolution). |

**Forbidden guesses, exhaustively:** scanning `$HOME` for something Obsidian-shaped; defaulting `vault.root` to the current working directory; creating any folder outside an explicit `--create-folders`/`auto_create_folders` opt-in; reading a config file from a path the user did not configure; inferring the vault from `$PWD` at RPC time; treating a missing `[consumers]` table as "enable the obvious ones".

**Named carve-outs from the refusal, exhaustively two — `organize init` and `organize teach`.** Neither is an exception to "never guess"; both *supply* a config rather than inferring one.

- **`init`** is the first-run ergonomics below: the file it creates *is* the configuration, so there is nothing to guess about.
- **`teach`** (18 §6) **loads an explicit config file at an explicit path that it authored a moment earlier** inside its own sandbox — it supplies a config rather than guessing one. The only table it may read from the *user's* config is `[teach]`, through a dedicated loader whose return type structurally cannot carry a vault root or any `CorePaths` value (18 §6); with no user config file at all it falls back to the shipped `[teach]` defaults and still runs. The sandbox root it writes into is resolved by 18 §1.1's precedence chain (`--sandbox-dir` > `$ORGANIZE_TEACH_DIR` > `[teach] sandbox_dir` > tmpdir) and must be provably disjoint from the configured `[vault]` root.

A third carve-out may not be added without amending this paragraph, the §2 table row and §9.3 **together**. Before this ruling `organize init` was carved out in this section's prose while the table row and acceptance 3 both still said "any subcommand" — a spec that contradicted itself two screens apart. That is the failure this sentence exists to prevent.

**First-run ergonomics (new, small, required):** `organize init [--vault PATH]` writes the commented example config to `paths.config_file` with `[vault] root` filled from `--vault` (refusing to overwrite an existing file), and prints the next two commands (`organize health`, then the teach-mode entry point). This is the one place the software may create a file **at a user path** before being configured, because the file it creates *is* the configuration. (`teach`, the other carve-out, creates files too — but only inside a sandbox root proven disjoint from the configured vault, 18 §1.1.) `:checkhealth para-organize` must name `organize init` when the config is missing — the actionable half of "loud failure" (09 §1.5) is telling the user the exact command that fixes it.

---

## 3. The de-Matt-ification work list

This is a work list, not a sentiment. Each row: the Matt-specific fact, where it is today, the neutral rule, and where the default lives afterwards. **Status** is `demote` (move value from code to example/config), `neutralise` (replace the value), `document` (already config — say so and test the override), or `new key` (a knob that does not yet exist).

### A. Identity and user framing

| # | Matt-specific fact today | Neutral rule | Default lives | Status |
|---|---|---|---|---|
| A1 | `01 §"The user"`: "exactly one user: Matt … on NixOS" | 01 describes the **origin workload** (throughput, backlog scale, habits) and names no user as the only user | spec 01 prose | demote |
| A2 | `fileops.py:629`, `cli.py:529`, `server.py:1805` default `actor="matt"` on every unattributed write | Default actor is `"user"`; the identity is config: `[identity] actor` (free string, validated non-empty) | `IdentityConfig.actor = "user"` | new key |
| A3 | `learn.py:386` / `fileops.py:168` `HUMAN_ACTORS = {"matt","user","human"}` | Human-ness is the `human:` namespace plus the configured `[identity] actor`; `"matt"` stays a recognised legacy alias so the existing corpus keeps folding | same frozensets + config | document |
| A4 | `actions.py:462` actor-enum comment, `12 §2`'s literal `"matt"` | `actor` is an opaque string in the documented namespaces (`human:*`, `route:*`, `consumer:*`, `claude-integrate`, `auto-organize`) | doc 12 §2 table | neutralise |
| A5 | `integrate.py:1423` comment: "`_refuse_no_ai` exempts `actor="matt"`" | The exemption is keyed to **any human actor**, never to a name | `fileops.is_ai_actor` (already correct) | document |
| A6 | `learn.is_matt_decided` names the origin user in a **public interface** — the predicate the learning corpus is filtered on | Rename to **`is_human_decided`**: the question is "did a human decide this", never "did Matt decide this". Eight call sites move together — `integrate.py:1510,1518`; `routes.py:600`; `fileops.py:1071`; `consumers/tag_router.py:55,530`; plus the ARCHITECTURE public-interface list. The **Phase-6 firewall note travels with the rename** (the comment explaining why machine-decided rows never feed the corpus stays attached to the renamed symbol). Owned by **this doc**, not 17 | `learn` module | neutralise |

### B. Paths and machine

| # | Fact today | Neutral rule | Default lives | Status |
|---|---|---|---|---|
| B1 | `config.py:1758` and `index.py:389` put `~/Obsidian/Main` in **user-facing hint text** | Hints name the *shape* of the answer (`"the vault root — the folder that contains your PARA folders"`), never one user's path | error/hint strings | neutralise |
| B2 | `config.py:1812-1813` `EXAMPLE_CONFIG_TOML` opens with `root = "~/Obsidian/Main"` | `organize health --example-config` emits a **neutral** example (`~/vault`); Matt's real config ships as a separate case study (`examples/`), clearly labelled as one user's | example config | demote |
| B3 | `deploy/` units hardcode `%h/.config/organize-core/config.toml` (ARCHITECTURE:1211, deferred) | Units honor `$ORGANIZE_CORE_CONFIG_DIR`/`$ORGANIZE_CORE_STATE_DIR`, falling back to XDG | deploy unit files | neutralise |
| B4 | `~/notes` ↔ `~/Obsidian/Main` symlink equivalence (`paths.py:111,180`) | Already general: **resolve symlinks before comparing any two vault paths** — a stranger with a symlinked vault gets the same correctness | `paths` module | document |
| B5 | `doc/MIGRATION-from-old-setup.md` reads as everyone's upgrade path | It is the **origin user's** migration note; its first paragraph must say a fresh installer needs none of it | doc header | document |
| B6 | `README.md`: "Status: spec complete … No implementation has begun." | The README is the stranger's first and possibly only read; it must describe the shipped software (§6) | README | neutralise |
| B7 | `06 §"deep_research"`: command via `~/Projects/DeepResearchAgent`; `NOTES_DIR` passthrough | Consumer commands/env are **required keys with no default** — a consumer nobody configured cannot run | `[consumers.*]` | document |
| B8 | Residual origin-user paths the tables above miss: `deploy/README.md:38,312` shim paths, `errors.py:53`, `config.py:2020` `NOTES_DIR` | Same rule as B1/B2 — **no shipped string names one user's tree**, in code, in error text or in deploy documentation. These three are the rows the §10.5 gate finds once its string set is widened; they are listed here so "the gate went red" has a work item to point at | error/hint strings, deploy docs | neutralise |

### C. Folder vocabulary

| # | Fact today | Neutral rule | Default lives | Status |
|---|---|---|---|---|
| C1 | `config.py:63-70` `para_folders` defaults `projects/areas/resources/archive` | The four **keys** (`projects`,`areas`,`resources`,`archives`) are the schema and the type vocabulary; their **values** are free folder names — `projects = "1-Projects"` is a supported config, not a fork | `VaultConfig.para_folders` | document |
| C2 | `config.py:778` `_PARA_KEYS` fixes the taxonomy at four types | LAW for 0.x, stated openly: para-organize implements PARA. Renaming or extending the *type set* (Zettelkasten, Johnny.Decimal) is **out of scope** (§7) rather than half-supported | `_PARA_KEYS` + §7 | document |
| C3 | `archive` (singular) as the shipped default; `archives` the key | Neither spelling is universal — the default is a default, and the health check reports the mismatch instead of the software assuming (02) | `para_folders["archives"]` | document |
| C4 | `config.py:76` `scan_dirs` default lists literal `capture/raw_capture, resources, areas, projects` | The default **derives from the configured folder names**, not from literals — renaming `projects → 1-Projects` must not silently empty the scan scope | computed default in `VaultConfig` | neutralise |
| C5 | `config.py:80` `ignore_patterns` default includes `resources/flashcards` | Neutral default is `[".obsidian", ".git"]`; the flashcards exclusion moves to the example config | `VaultConfig.ignore_patterns` | demote |
| C6 | `05 §2`: a move writes the tag `<singular-type>/<folder-name>` | The template is config: `[file_ops] move_tag_template = "{type}/{folder}"`; empty string writes no tag | `FileOpsConfig` | new key |
| C7 | `02`: `dailies/` excluded, `YYYY-MM-DD.md` skipped in `raw_capture/` | Both are pattern config (`ignore_patterns`, a documented daily-note pattern), not a hardcoded folder name | `[vault]` | document |
| C8 | `capture_folder`, `raw_capture_folder` and `archive_capture_path` are written as facts about every vault — a vault without them is treated as broken | All three are **DEFAULTS derived from `para_folders`**, never literals, and **a vault with no capture folder is a SUPPORTED configuration**: `organize health` reports it as a **note**, never an error, and the session falls back to F6's default filter. The capture app of 02 is *one user's producer of notes*, not a precondition for running this software. This is the single largest Matt-shaped assumption in the system and §3 had no row for it | computed defaults in `VaultConfig` | neutralise |
| C9 | §2's "Forbidden guesses" names `auto_create_folders` as the folder-creation opt-in but no table gives it a section, type or default | **`[file_ops] auto_create_folders`**, type `bool`, **default `false`**. Reader: `fileops.move_to_destination` / `fileops.new_folder`, consulted before any `mkdir`. Honored-test (§4.3d): with `true`, moving to a missing destination creates it; with `false`, the same move **refuses naming the absent path**. `--create-folders` is the per-invocation override and obeys §4.4's precedence | `FileOpsConfig` | new key |

### D. Tag and content vocabulary

| # | Fact today | Neutral rule | Default lives | Status |
|---|---|---|---|---|
| D1 | `config.py:145` `tag_suffix_strip = ["-system","-systems"]` (tuned on 65 real captures) | Ships as a tuning default with the empirical justification in the docstring; a stranger empties the list and loses nothing but recall | `SuggestionsConfig` | document |
| D2 | `config.py:1963-1979` consumer defaults `marker_tag="todo"`, `review_tag="not_reviewed"`, `additional_tags=["para","automation"]` | Generic-English defaults stay defaults; each is a documented key | `[consumers.*] options` | document |
| D3 | `config.py:1978-1979` `deck="Reading::Articles"`, `card_tags=["learn-consumer"]` | **A default that names a user-specific external object is not a default.** Anki decks, Taskwarrior projects, hostnames: required keys, no default, loud error — writing into a stranger's deck because we guessed is unrecoverable | `[consumers.*] options` (required) | neutralise |
| D4 | `config.py:1914-1936` example routes (`workout`, `blog-idea`, `impro/theatre`) | Routes default to `[]` and are examples only. LAW: **no route, description or tag ships enabled** | `RouteConfig` list = `[]` | document |
| D5 | `config.py:2088` `[descriptions]` example naming `matthandzel.com` | Descriptions default `{}`; the neutral example uses placeholder folders | example config | demote |
| D6 | `06 §"taskwarrior"`: Fibonacci importance guide with few-shot examples mined from Matt's task export | Prompt few-shots that quote a private corpus are **config-supplied** (`options.examples_path`) with a neutral built-in fallback; one user's task history never ships as code | consumer options | neutralise |
| D7 | `02 §"Vault-level rule"`: the key is spelled `no-ai` (from Matt's `kms-system-rules.md`) | The **law** is not configurable (§8); the **spelling** is: `[safety] no_ai_key = "no-ai"` | `SafetyConfig` | new key |
| D8 | `04 §5` decay tuned to monthly rhythms; `07` ships `tags` + `importance` fields | Both already config (`learning.recency_decay`, `metadata_fields`) — the obligation is a test proving a changed value changes behavior (§4d) | core config | document |
| D9 | The same defect as D3, one layer down, in consumer **code** rather than in the schema: `consumers/learn.py` `deck` / `flashcard_dir` / `review_dir` / `_EXCLUDED`, `consumers/question_answer.py` `flashcard_review_dir`, `consumers/taskwarrior.py` `review_tag` | D3's rule applies wherever the default is written: a code default naming a **user-specific external object** is not a default. All six become **required keys, no default, loud error**. Fixing only `config.py` leaves the guessed Anki deck fully live — the unrecoverable write D3 exists to prevent, reached by a different import | `[consumers.*] options` (required) | neutralise |

### E. External stack

| # | Fact today | Neutral rule | Default lives | Status |
|---|---|---|---|---|
| E1 | `config.py:329,1325,1982,2046` `server.matthandzel.com` in comments, hint text and example | Hints and examples use `http://localhost:11434` / `:9000`; a real host appears only in the case-study config | hint + example strings | neutralise |
| E2 | 02's integration inventory assumes Taskwarrior, Anki, Obsidian, Syncthing, Ollama, NixOS installed | Every integration is a **consumer, absent from the default `[consumers]` list**; `organize health` checks binaries only for consumers the user enabled — a stranger with none of them gets a green health check | `Config.consumers = []` | document |
| E3 | 11 §2: the auto-tagger "runs on the home server" | Backend is config (`ollama` \| `claude-cli`), host is config, and the consumer is off by default (§8) | `[consumers.auto_tagger]` | document |
| E4 | 02: Obsidian wiki-link resolution assumed (do not rename on move) | Stays LAW — it is a property of the Markdown-vault ecosystem, not of one user; documented as such so nobody "fixes" it | 05 §2 | document |

### F. Workflow habits stated as requirements

| # | Fact today | Neutral rule | Where it lands | Status |
|---|---|---|---|---|
| F1 | "dozens of captures a day", "~2,300 backlog", "20 real captures" definition of done (01, 09 §6) | These are **fixture scales and performance evidence**, not requirements on the user. 09 §4's absolute budgets (suggest < 100 ms, UI < 50 ms) are the binding form | 09 §4 | document |
| F2 | Batch triage sessions as *the* mode (01) | One capture at a time and one capture per month must work identically — no code path may assume a warm session or a long queue | 03 §6 | document |
| F3 | Annotate-then-skip; multi-file filing (07, 12 §2) | Both are supported behaviors, not expectations; neither may be required to complete a session | 07, 12 | document |
| F4 | The capture card's timestamp ships `"%b %d, %I:%M %p"` — 12-hour, US | A default. A stranger sets `%Y-%m-%d %H:%M`; the renderer must not assume a width or a locale. The key is doc 15's **`ui.capture.formatters.timestamp`** — the old `ui.DEFAULTS.display.timestamp_format` spelling dies with the `ui.display` section (§5) and gets a `MOVED_KEYS` entry (§4.4) | `ui.capture.formatters` (doc 15) | document |
| F5 | Keymap defaults are Matt's fingers (`s`, `a`, `m`, `<leader>np`) | Defaults, every one rebindable by name, collisions detected at setup (03 §3, 07 acceptance 4) | `keymaps.buffer` | document |
| F6 | `03 §2`'s default session filter is `status = raw` restricted to `para_type == "capture"` — and `processing_status` is written **only** by the origin user's capture app | The default session filter becomes **`[session] default_filters`**. Its shipped default is **"unfiled notes", defined without reference to `processing_status`**: notes that live outside every configured PARA folder. `status = raw` demotes to **EXAMPLE** tier and ships in the commented example config as the capture-app user's filter. Without this row the stranger of §1 matches zero notes and — per §2's last table row — gets a successful install that does nothing | `SessionConfig.default_filters` | new key |

---

## 4. Configuration law for a public plugin

### 4.1 Where a knob lives (the one-question decision)

**One question decides it: could the CLI and the UI disagree if they held different values for this knob?** If yes, it is core config — that is 10 §3's rule stated as a test rather than a list.

| Kind of knob | Home | Because |
|---|---|---|
| Anything that changes a vault byte, a score, a record, or what a consumer does | **core** `~/.config/organize-core/config.toml` (10 §3) | two frontends holding different values would produce different vaults |
| How something looks, or which key presses it | **nvim** `setup()` | the core has no view of it |
| How the client reaches the core (socket, spawn, timeouts) | **nvim** `setup()` top level (+ the nested `core.*` spelling normalised up) | it is a property of this client's process, not of the vault |
| Where state/config files live | **environment** only — `$ORGANIZE_CORE_CONFIG_DIR`, `$ORGANIZE_CORE_STATE_DIR`, `$ORGANIZE_CORE_RUNTIME_DIR` (all three exist at `paths.py:30-32` and all three are required by 18 §1.1's env scrub), plus `$ORGANIZE_TEACH_DIR` (the teach sandbox root, 18 §1.1) | needed by tests and by packagers, and **each redirects a path and nothing else** |

**No behavior is ever configured from the environment** — structural decision #4 ("paths always injectable; nothing consults `os.environ` directly") exists so a stranger cannot get a different product by exporting a variable. Env vars redirect *where files are*, never *what the software does*.

Applied to every knob this doc introduces, with the reason recorded so the placement is not re-litigated:

| New knob | Home | Why that side |
|---|---|---|
| `[identity] actor` (A2) | **core** | It is written into every `ActionRecord` and read by the learning filter; a client-side value would let two frontends attribute the same vault differently. |
| `[safety] no_ai_key` (D7) | **core** | The refusal happens in `fileops`; a client that disagreed about the key name could ask the core to write an opted-out note. |
| `[file_ops] move_tag_template` (C6) | **core** | It changes bytes on disk (05 §2), so the CLI and the UI must produce identical tags. |
| `[consumers.*] examples_path` (D6) | **core** | Consumers are core-side entirely (10 §1). |
| `[file_ops] auto_create_folders` (C9) | **core** | It creates a directory in the vault, so the CLI and the UI must agree about whether that directory now exists. |
| `[session] default_filters` (F6) | **core** | It decides which notes a session contains; two frontends with different values would triage different vaults. |
| `ui.organize.score_thresholds.{high,medium}` — **numbers**, spelling owned by doc 15 | **nvim** | Purely which highlight group a rendered score gets; the scores themselves cross the wire unchanged, so no CLI counterpart can disagree. Not to be confused with `ui.highlights.score_high`/`score_medium`/`score_low`, which are group *names* (§5). |
| Capture/row rendering — `ui.capture.render`, `ui.capture.formatters`, `ui.capture.fields.*`, `ui.organize.render_row` (all doc 15's) — plus the view registry, `actions.register_key`, `on_notify`, `hooks.*` (§5) | **nvim** | All are how the client draws and dispatches; the core has no view of them and none of them can change a vault byte. |
| `ui.organize.preview_notes` (16) | **nvim** | 14 §4.1's one question, answered: there is **no CLI destination preview**, so the CLI cannot disagree. `throughput.mru_size`, `throughput.mru_order` and `[session] order` stay **core** for the opposite reason. |

### 4.2 Naming

`snake_case` throughout, both TOML and Lua. **Section = subsystem** (`[vault]`, `[suggestions]`, `ui.capture`). Booleans are named for their ON state and default in the direction that surprises least (`show_scores = true`, never `no_scores`). A key ending `_dir`/`_path` holds a path and is `~`-expanded; every other path-shaped value is **relative to the vault root** and refused if absolute (`config.py:957`). **Plural names a collection, singular names a scalar** — the standing PARA vocabulary ruling, applied to all keys. No abbreviations that a reader must decode (`recency_decay`, not `rdecay`).

Two carve-outs, stated so the rule is honest rather than quietly broken:

- **(a) Durations carry a unit suffix, not always `_ms`.** `_ms` for machine scale (debounces, timeouts, budgets); `_seconds` / `_days` / `_months` for human scale (`max_age_days`, `seen_scan_months`). The unit is in the name in every case; only the unit varies.
- **(b) A core-config value that references a file *outside* the vault is absolute.** `[teach] lessons_path` and `[consumers.*] examples_path` name files in an install prefix or the user's own tree, not in the vault; they are absolute (`~`-expanded) and the relative-to-vault-root rule of the previous paragraph applies **only to vault-relative path values**. Renaming follows from the `_path` rule and is required, not optional: `[teach] lessons` → **`lessons_path`**, `[consumers.*] examples_file` → **`examples_path`**, in every table in this series that names them.

**The canonical nvim `ui.*` sectioning** — one table so every later doc's key is mechanically checkable — is `ui.capture.*` and `ui.organize.*` (doc 15, extended by 16), `ui.hint.*` and `ui.batch.*` (16), `ui.undo.*` (17), `ui.teach.*` (18), alongside the existing top-level `ui.highlights`, `ui.icons`, `ui.float_opts`, `ui.win_options`, `ui.capture_pane_keymaps`, `ui.close_on_complete`. **There is no `ui.display` section**: doc 15 deletes it and no doc may add a key under it (§4.4 records the moves, §5 the seam).

### 4.3 The four-part obligation

**Every new user-visible behavior ships with all four or does not ship:**

| | Obligation | Enforcement that exists today |
|---|---|---|
| a | a **default** that is correct for the stranger of §1 | code review against the Two-Users Test |
| b | a **config key** in the closed schema (core `validate_config`, or the Lua `SCHEMA` record) | unknown-key `ConfigError` naming the dotted key |
| c | a **documented meaning** — one sentence in the example config, the README table, and `:help` | doc-drift gate (§6) |
| d | a **test that the key is honored** — set a non-default value, observe the changed behavior | `tests/test_config.py` unread-leaf gate + `RESERVED_CONFIG_LEAVES` |

The `RESERVED_CONFIG_LEAVES` gate is the live form of 03 §1's **honored-or-deleted** law: a documented key with no reader is a defect, and an entry on the reserve list must carry a reason and the phase that will clear it. **The Lua schema needs the mirror gate** — today `config.lua`'s `SCHEMA` has no test proving every leaf has a reader, and the origin plugin shipped ~25 dead keys precisely because no such gate existed (08 §A35). Naming the gap is not discharging it: **§10.11 obligates the build, and dates it before any doc-15/16/17/18 key ships**, because those docs add no Python leaf and would otherwise satisfy (d) by adding nothing.

### 4.4 Deprecation

| Event | Required behavior |
|---|---|
| A key is renamed or moves core↔nvim | The old spelling raises a `ConfigError` naming **both** the old dotted key and its new home — the `MOVED_KEYS`/`REMOVED_KEYS` tables. Silent acceptance is forbidden: a silently-accepted old spelling is exactly how the CLI and the UI come to disagree. |
| A key is deleted | `REMOVED_KEYS` entry saying *why*, permanently for the 0.x line. |
| An entry ages out | Only at a MAJOR. Table entries are cheap; a stranger upgrading from a two-year-old blog post is not. |
| A default changes value | CHANGELOG entry under "Changed", with the old value written out, because a user who liked the old default must be able to pin it. |
| **The `ui.display` section is deleted** (doc 15 owns the deletion) | Every leaf that lived under it gets a `MOVED_KEYS` entry naming its new home — `ui.display.show_progress` → `ui.organize.show_progress`, `ui.display.timestamp_format` → `ui.capture.formatters.timestamp`, `ui.display.score_high` / `score_medium` → `ui.organize.score_thresholds.high` / `.medium` — and `ui.display` itself gets a `REMOVED_KEYS` entry saying why. `setup{ ui = { display = … } }` raises a `ConfigError` naming both the old dotted key and its new home; it never silently no-ops, and it never renders as an unknown-key error that fails to say where the value went. The other renamings in the same sweep (`ui.numeric_accept`, `ui.preview_debounce_ms`, `ui.batch_confirm`, `ui.batch_max`, `ui.undo_confirm`, `ui.history_limit`, `ui.view_undo_depth`) are owned by 15/16/17 and follow this same rule. |

Precedence, always, no exceptions: **explicit argument > environment > config file > shipped default**. The environment rung sits where it does *because* of §4.1's rule, not against it: an env var may only redirect **where a file is**, never what the software does, so the only keys with an environment rung are path redirects. Worked example, 18 §1.1's sandbox root: `--sandbox-dir PATH` > `$ORGANIZE_TEACH_DIR` > `[teach] sandbox_dir` > `<tmpdir>/organize-teach-<uid>/`. A partially-applied config is never left behind — a failed validation rolls back (the `init.setup()` gate ruling).

---

## 5. Extension seams and their stability contract

**A third party must be able to change presentation and add behavior without forking.** Forks do not receive bug fixes; a fork is a support burden that arrives as a bug report two versions later.

Three stability tiers, and every seam declares one in its docstring:

- **STABLE** — breaking it requires a MAJOR. Documented in `:help`.
- **PROVISIONAL** — may change in a MINOR with a CHANGELOG entry. Documented, marked provisional.
- **INTERNAL** — no contract. Must be *documented as internal* so nobody builds on it by accident.

| Seam | Mechanism | Tier |
|---|---|---|
| Core behavior knobs | `config.toml`, closed schema | STABLE |
| UI knobs | `setup()` — `ui.capture.*`, `ui.organize.*` (15), `ui.hint.*`, `ui.batch.*` (16), `ui.undo.*` (17), `ui.teach.*` (18), `ui.float_opts`, `ui.icons`, `ui.win_options`, `ui.capture_pane_keymaps`, `ui.close_on_complete`. **`ui.display.*` is gone** — doc 15 deletes the section and no doc may add a key under it; §4.4 carries the `MOVED_KEYS`/`REMOVED_KEYS` obligation | STABLE |
| Keymaps | `keymaps.buffer.<name>` by action name; `""` unbinds. The reservation list `CORE_KEYMAPS` (`config.py`) and the action table (`actions.lua`) are **owned by this doc**, because a constant five documents write conflicts five ways: 15–18 submit their rows as **one consolidated patch**, and a new binding arrives through `actions.register_key` (§5's "Extra actions"), never by editing `CORE_KEYS` in place. §10.7 gates the two against each other | STABLE (the **name set** is the contract; adding names is a MINOR) |
| Command surface | `:ParaOrganize <sub>`, the 13 `<Plug>` mappings, **no global keymaps by default** (03 §2) | STABLE |
| CLI | subcommands + `--json` payload shapes | STABLE |
| RPC | method names + `apiVersion` handshake, major-mismatch refusal | STABLE, versioned |
| Metadata fields | `[[metadata_fields]]` — new field = zero code (07) | STABLE |
| Routes / descriptions | `[[routes]]`, `[descriptions]`, `organize routes describe` (`cli.py:276` — the invocation is the two-word subcommand, not `organize describe`) | STABLE |
| Highlight groups | `ui.highlights.*` **and** the `ParaOrganize*` default groups the plugin defines | STABLE — ⚠ *the fallback groups (`ParaOrganizeSelected`, `ParaOrganizeScoreHigh\|Medium\|Low`) are referenced but never defined by any `nvim_set_hl`; a colorscheme author has nothing to hook. Defining them with sensible links is required work.* |
| Capture-card composition | doc 15's mechanism, by its names: `ui.capture.fields.pinned` / `ui.capture.fields.hidden` (ordered field lists), `ui.capture.formatters.<field>` (per-field renderers), `ui.capture.render` (whole-card override) and `ui.organize.render_row` (suggestion / entry rows) — replacing the fixed pipeline and the literal `HEADER_KEYS` | PROVISIONAL — **new seam, designed and owned by doc 15**; this doc assigns the tier and states the no-fork obligation, nothing more |
| Right-pane views | `render.VIEWS[name] = fn` registry, generalising the duck-typed `para-organize.integrate` hook | PROVISIONAL — **new seam** |
| Extra actions | `actions.register_key{ name, default, desc, panes, views, fn }`, which also adds the row to the help pane and the rebindable name set. **`views` is a list of view names** (`"suggestions"`, `"browse"`, `"search"`, `"mru"`, `"history"`, `"merge"`, `"integrate"`); the field is **not** called `mode`, because in Neovim `mode` means the Vim mode and the live table already scopes by view. **View scoping is mandatory for any row whose default lhs shadows a native normal-mode command**: such a row binds in the organize pane only (16 §"capture pane") and must be *unbound* in every state where the organize buffer is modifiable (17 §7), through the same gate `e` already uses | PROVISIONAL — **new seam** (today a new action needs edits in three files) |
| Score thresholds | `ui.organize.score_thresholds = { high = 2.0, medium = 1.0 }` — **numbers**, owned by doc 15 (today literals `2.0` / `1.0` in `render.lua`). ⚠ **`ui.highlights.score_high` / `score_medium` / `score_low` remain highlight-GROUP NAME strings and are unchanged by this series** — three keys, one name-shape, and fusing them is what this row exists to prevent | PROVISIONAL — **new key**, tier assigned here, spelling owned by 15 |
| Lifecycle hooks | `state.on(fn)` (exists); `on_notify(level, msg)`; `hooks.before_move / after_move / after_session` — **observational only: a hook cannot veto or alter an operation**, because a client that could veto would make the CLI and the UI disagree about whether a move happened (10 §1). Both `hooks = {}` and `on_notify = nil` are **declared leaves in the Lua `SCHEMA`** (§4.3b), with fixed argument shapes: `before_move(capture, destination)`, `after_move(capture, destination, result)` (`result` carries `ok` and `archived_path`), `after_session(summary)` (the session-end payload), `on_notify(level, msg)` with `level ∈ {"info","warn","error"}`. **Return values are ignored** — a hook returning `false` stops nothing, which is the observational rule made mechanical | PROVISIONAL — `state.on` exists, the rest are **new seams** |
| Front-end substitution | `actions.setup{ ui = … }`, `callback_style`, `actions.request` | PROVISIONAL |
| Module injection | `commands.inject(name, mod)` | INTERNAL (test seam) |
| Buffer names, augroup, extmark namespaces | `para-organize://*`, `ParaOrganizeUI`, `NS_ORGANIZE`/`NS_CAPTURE` | INTERNAL |

**The no-fork test.** Each of these must be achievable by a stranger writing only config or a small Lua table — if one requires editing plugin source, the corresponding seam is not done:

1. Replace `[P]`/`[A]`/`[R]` with devicons. *(config: `ui.icons`)*
2. Show three metadata fields in the capture card, in a chosen order, and hide the rest until asked. *(config: `ui.capture.fields.pinned` and `ui.capture.fields.hidden`, doc 15)*
3. Add a right-pane view the plugin does not ship. *(the `render.VIEWS[name] = fn` registry — deliberately not written as "a sixth view", since 16 registers `mru` and 17 registers `history` on top of the shipped set)*
4. Bind `<leader>x` to a custom Lua function that receives the current capture record. *(`actions.register_key`)*
5. Ship a colorscheme that themes the plugin. *(defined highlight groups)*
6. Send a desktop notification after every move. *(`hooks.after_move`)*

---

## 6. Documentation and discoverability obligations

**Law: no hand-maintained second copy of a machine-derivable list.** The origin system's docs drifted from behavior on every enumerable surface (08 §A38, §B17) and MANUAL.md documented keymaps that did not exist (03 §3). Any document that enumerates keymaps, subcommands, config keys or RPC methods is either **generated** from the same table the code binds, or **gated by a drift test** that fails when they disagree — the pattern already used for the CLI-invocation drift guard.

| Artifact | Obligation |
|---|---|
| `README.md` | What it is in three sentences; a 60-second quickstart (install core → `organize init --vault` → `setup{}` → `:ParaOrganize start`); install snippets for lazy.nvim and packer; the generated keymap and subcommand tables; a link to `:help`; **and the five sections tabulated below**. It is the stranger's only guaranteed read. |
| `doc/para-organize.txt` | A real Vim help file with doctags (`*para-organize*`, `*para-organize-config*`, `*para-organize-keymaps*`), so `:help para-organize` works. **Does not exist today — required for distribution.** |
| `:checkhealth para-organize` | nvim version, required/optional deps, config validity, core reachability + version handshake, socket path length, vault + PARA folder existence. Every failure names the fix. |
| `?` help pane | Lists **every** binding, core and metadata, generated from `keymap_table()` — the single source both the help and the actual `vim.keymap.set` calls read (03 §2). A binding that exists and is not listed is a bug in the generator, never a missing line in a doc. |
| which-key (optional) | When `require("which-key")` resolves, register **every** buffer binding with its `desc` taken from the same `keymap_table()` the `?` pane reads, plus a **group label for every `<leader>` prefix** (`<leader>m…`, `<leader>b…`, `<leader>n…`). **Never a dependency**: absent which-key changes nothing and no code path may assume it. Drift-gated with the `?` pane against the one table (§10.7). Independently and unconditionally, **every `vim.keymap.set` carries a `desc`**, so `:map` and Telescope's keymap picker are truthful for free. This discharges the evidence doc's explicit instruction (FEEDBACK-EVIDENCE:125-127), which no other doc in the series carries. |
| Teach mode | Offered on first run; runs entirely against a temporary fixture vault, touching no configured vault (doc 18 owns the design; §8 owns the invariant). |
| `organize health --example-config` | Prints a complete, commented, **neutral** config — the canonical answer to "what can I configure?", and the thing `test_config_example.py` round-trips. |
| CHANGELOG.md | Keep-a-Changelog format; every default change, key rename and seam-tier change appears. |

**Required README sections, beyond the quickstart.** Each answers a question the stranger of §1 asks in their first hour and that no current document answers anywhere:

| README section | Must say |
|---|---|
| **Where captures come from** | The capture app is a **separate repo and is not required**. Point `vault.root` at your notes; the notes that are not yet in a PARA folder are your queue (C8, F6). Nothing in this software produces `processing_status`, and nothing in it needs that key to exist. |
| **Version compatibility** | Upgrade the core and the plugin **from the same tag**. The RPC handshake refuses on a major mismatch (§7) — quote the exact message and give the remedy in one line (upgrade the lagging half; `:checkhealth para-organize` names which half). |
| **Uninstall / where is my state** | What lives under `$XDG_CONFIG_HOME/organize-core/` and `$XDG_STATE_HOME/organize-core/` (config, action corpus, learning, backups, session state), what to delete for a clean removal, and **how to take the corpus with you** — it is the user's record of their own decisions (§8.5), not ours. |
| **The lazy.nvim `:checkhealth` caveat** | Under lazy-loading, `:checkhealth para-organize` reports nothing useful until the plugin has actually loaded; say so and give the one command that loads it first (FEEDBACK-EVIDENCE:128-131). |
| **"My vault is not PARA-shaped"** | Honest and short: create four folders under any names you like and set `para_folders` (C1), or this is not the tool for you. Non-PARA taxonomies are out of scope for 0.x (C2, §7) and will not be half-supported. |

---

## 7. Distribution readiness

**"Installable by a stranger" means: a person who has never met Matt gets a working organize session from a fresh clone, using only the README.** Required contents:

| Artifact | Requirement |
|---|---|
| Neovim install | lazy.nvim spec in the README: `dependencies = { "nvim-lua/plenary.nvim", "nvim-telescope/telescope.nvim", "MunifTanjim/nui.nvim" }`, optional which-key + nvim-web-devicons, `opts = {}`. Packer snippet alongside. |
| Core install | From the clone: `pipx install .` / `pip install -e .`, plus the nix flake 10 §1 specifies (the repo has no `flake.nix` today — required work). All must put `organize` on `$PATH`, since the plugin auto-spawns `core_cmd = {"organize","serve"}` and, on failure, must print the exact install line. Index publishing is out of scope below, so the README's install path must not assume it. |
| Runtime deps | Core: **stdlib + PyYAML only** (ARCHITECTURE manifest, "no new runtime deps"). Plugin: plenary/telescope/nui required, which-key/devicons optional. Adding a dependency is a spec change. |
| Minimum versions | Neovim ≥ 0.9 (hard error below, 03 §1); Python ≥ 3.11 (`tomllib`). Both asserted in `:checkhealth` and at import. |
| Versioning | Plugin `0.1.0` + RPC `apiVersion 1`, semver: **MAJOR** = removing a config key / renaming a keymap name / breaking an RPC method / changing an on-disk state format; **MINOR** = new key with a default, new subcommand, new PROVISIONAL seam, changed default value; **PATCH** = fixes only. RPC major mismatch refuses with a message naming both versions. |
| State migration | The state dir carries a version stamp; any MAJOR that changes an on-disk format ships a migrator that backs up first (05's never-delete law applies to state, not just notes). |
| License | MIT. `LICENSE` exists but its copyright line names only "para-organize.nvim contributors" — it must be widened to cover the `organize-core` tree it now ships alongside, and `pyproject.toml` must declare it. |
| Provenance | The README says plainly that the defaults were tuned on one real vault and points at the case-study config — honest, and it explains the `-system` suffix list to anyone who wonders. |

**Explicitly out of scope for 0.x** (stated so the work is bounded, and so a stranger's expectations are set): Windows support; non-PARA taxonomies (C2); vault link syntaxes other than Obsidian's; multi-vault sessions; a GUI or web frontend (the RPC permits one — we do not ship one); publishing to luarocks/PyPI indexes; i18n of UI strings; hosted/multi-user deployment.

---

## 8. Privacy and safety toward a stranger

Matt accepted a system that reads his whole vault because he wrote its requirements. **A stranger has extended no such trust, and the software must earn it by default.**

1. **No telemetry, ever.** No usage counters, no crash reporting, no version check, no analytics — not opt-out, *absent*. The only outbound network calls are to hosts the user wrote in their own config; the only local socket is the core's. This is LAW and is enforced by a repo-hygiene gate, not by discipline.
2. **LLM features are opt-in and off by default.** A fresh install makes zero LLM calls: `[consumers]` defaults to `[]`, `llm.ollama_host` defaults to unset, `integrate` defaults to `review = "diff"` (nothing applies unreviewed), `auto_organize.trust` defaults to `propose`. Enabling any of them is an explicit edit to the user's own config.
3. **`no-ai` is inviolable.** A note opted out is never written by automated tooling — consumers, routes, auto-tagger, integrate, auto-organize. The interactive exception is keyed to a **human actor's explicit keystroke** (A5), never to a name. ⚠ The key's *spelling* becomes config (D7); the *law* does not.
4. **Never write to a vault the user has not configured.** No default `vault.root`, no `$HOME` scan (§2). Every write is containment-checked against the configured root; state and backups live under XDG dirs; teach mode operates on a temporary fixture vault and is provably incapable of touching a configured one.
5. **The action corpus stays local.** It is a complete record of what the user wrote and where it went (12 §2). It is never transmitted; any future feature that would send it is a per-invocation, explicitly-confirmed opt-in, and says what it is sending.
6. **Destructive-operation law is inherited unchanged from 05** and applies with more force to a stranger's vault than to Matt's: nothing is deleted, originals are archived, writes are atomic and TOCTOU-guarded, every operation is reversible through the log and backups (doc 17), and `--dry-run` performs no vault write. A stranger's first session is the one most likely to go wrong, which is exactly why undo must exist before the software is public.

---

## 9. Acceptance criteria

1. A test environment with `$HOME` pointed at an empty directory, a fixture vault at `$HOME/vault`, `organize init --vault "$HOME/vault"`, and `setup({})` in headless nvim yields a working session: capture renders, suggestions render, `<CR>` moves the note, original archived — **with no file in the repo naming Matt, his vault, or his hosts having been read**. The mechanism for the read half of that assertion is **18 §1.6's `open()` audit hook**, reused here rather than reinvented: it records every path opened by the process, and the assertion is that no recorded path matches the §10.5 string set or resolves under `examples/`.
2. The §10.5 gate, run as an acceptance check: scanning `src/`, `lua/`, `plugin/`, `deploy/`, `share/` for the origin-user string set returns hits **only** in provenance comments and the labelled case-study example, each of which is named in the allowlist file `tests/data/de_matt_allowlist.txt` — never in a default value, a hint string, or a shipped config template. The allowlist file is the *only* place an exemption may be written, and it shrinks to zero as §3 is worked.
3. Deleting the config file and running **any `organize` subcommand other than `init` and `teach`** exits non-zero with a message containing the config path and `--example-config`; it never proceeds on defaults. The two named carve-outs of §2 are pinned separately and positively: with no config file, `organize init --vault …` writes the config it was asked for, and `organize teach --check` runs to completion in its sandbox on the shipped `[teach]` defaults.
4. Setting `[vault] para_folders` to `{projects="1-Projects", areas="2-Areas", resources="3-Resources", archives="4-Archive"}` on a fixture vault so shaped produces correct suggestions, moves, archive paths and the move-tag — no code path assumes the default names.
5. `setup()` with no argument and `setup({})` produce byte-identical effective config, and neither errors.
6. Every leaf in the core schema and in the Lua `SCHEMA` has a reader, or a `RESERVED_CONFIG_LEAVES`-style entry with a reason; the gates fail on a key added without one.
7. Every configured old/renamed key raises an error naming both the old dotted key and its new home; no old spelling is silently accepted.
8. The `?` help pane, the README keymap table and `keymap_table()` agree exactly; adding a `metadata_fields` entry with a keymap makes it appear in all surfaces that claim to be complete.
9. `:help para-organize` resolves, and its config section lists every STABLE key.
10. Each of the six no-fork scenarios (§5) is demonstrated by a test that writes only config or a user Lua table, never a patch to `lua/para-organize/`.
11. A fresh install performs zero outbound network calls during a full session (verified by a socket-refusing environment).
12. **`examples/case-study-origin-user.toml` exists, is the origin user's live config verbatim, and loads under `validate_config` with zero unknown keys and zero `MOVED_KEYS`/`REMOVED_KEYS` hits** — asserted by `test_case_study_config_loads_clean`. It is **never a fallback and never read at runtime**, pinned by an AST gate that fails if any module under `src/` or `lua/` references the `examples/` path. That is the checkable form of "the origin user's configuration is reproducible entirely through the public config surface, and no key exists that only he can reach": if a key he uses is missing from the schema, the file fails to load; if a spelling he uses has moved, the moved-key table catches it; if we ever quietly read it, the AST gate catches that.
13. **A stranger's first session renders something.** A fixture vault of **40 plain notes with no frontmatter and no capture folder** yields a **non-empty first session**: `organize health` reports the absent capture folder as a **note**, not an error (C8), and `session.start` on the shipped `[session] default_filters` returns the unfiled notes (F6). A successful install that does nothing is a failure of this criterion, not a configuration problem for the user to solve.

---

## 10. Test obligations

Written in the house anti-vacuity standard: refusal predicates pinned with guard-deleted checks, literal constants, must-not-be-connected invariants, mutation audit before handback.

1. **Refusal pins.** For each refusal in §2 (missing config, missing `vault.root`, nonexistent root, missing PARA folder, absolute path where relative is required, path escaping the root), assert the refusal **and** assert that deleting the guard makes the test fail, **and a firing control at an adjacent parameter where the operation must succeed**. A refusal test that still passes with its guard removed is testing nothing; and **a guard that refuses everything passes a guard-deleted test**, which is why the pin is two-sided (ARCHITECTURE.md:1516-1518).
2. **Literal constants.** Assert `"user"`, `"no-ai"`, `[".obsidian", ".git"]`, `104`, `"{type}/{folder}"`, `apiVersion 1` as **literals written in the test**, never as the imported module constant — importing the constant makes the test agree with any future value, including a wrong one.
3. **Honored-key tests are two-sided.** For every key: default value → observed default behavior, **and** a changed value → *different* observed behavior. A test that only exercises the default cannot distinguish "honored" from "ignored". Parametrise over the schema so a new key with no honored-test fails the gate.
4. **Must-not-be-connected invariants.** (a) No module in `src/` constructs a URL from anything but config — AST gate, extending `test_repo_hygiene.py`'s pattern. (b) With `[consumers]` empty and `llm.ollama_host` unset, no LLM client is ever instantiated. (c) Teach mode, run with the configured vault made read-only *and* watched, performs **zero writes** outside its sandbox, and **zero reads** under the decoy `$HOME`, the decoy vault, or the real `CorePaths` locations. The read half is scoped, not absolute — the process must open the interpreter, the stdlib, site-packages and the resolved `lessons_path` — and that read-allowlist is itself pinned by a literal-set test asserting it is never widened (doc 18 owns the mechanism; this row states the invariant 14 requires of it). An assertion no correct implementation can satisfy gets quietly rewritten into whatever passes, which is why this one is scoped here rather than left absolute.
5. **De-Matt-ification is enforced, not remembered.** A repo-wide gate scans `src/`, `lua/`, `plugin/`, `deploy/`, **`share/`** for the origin-user string set — `(?i)\bmatt\b`, `matth`, `handzel`, `Obsidian/Main`, `server\.matthandzel\.com` — and fails on any hit outside the allowlist file `tests/data/de_matt_allowlist.txt` (provenance comments and the labelled case-study file, each listed by path and line). **The bare word `matt` is the load-bearing addition**: it occurs 106 times in the scanned trees and contains neither `matth` nor `matt_`, so the earlier set passed a tree saturated with the origin user's name. **This set and doc 18 §2's banned-vocabulary list are ONE module constant with two callers** — a repo-wide gate weaker than the tutorial gate is backwards, and two lists with one purpose is how they diverge. The allowlist is the work list of §3 shrinking to zero.
6. **Foreign-vault fixture.** A second fixture vault with renamed PARA folders, no frontmatter on half its notes, non-kebab tags, and no capture-app schema, run through the full acceptance suites of 04–07. Passing on Matt's fixture only proves the software works for Matt.
7. **Doc-drift gates.** README keymap table ≡ `?` help pane ≡ which-key registrations ≡ `keymap_table()` (one table, every surface generated from it); README/`:help` config table ≡ the schema leaves; **`test_every_subcommand_is_documented`** — documented CLI invocations ≡ the real subcommand/flag set, checked against `README.md` **and** `doc/para-organize.txt` (this is the gate doc 18 §"coverage" defers subcommand coverage to; subcommands are *documentation* coverage, not tutorial coverage). Each gate fails on either direction of drift. Additionally, `CORE_KEYMAPS`'s key set ≡ the set of non-metadata `lhs` values in `keymap_table()`, exported to a fixture by the headless-nvim gate — this doc owns `config.py`, so it owns that drift gate.
8. **Seam-tier pins.** For every STABLE seam, a test that exercises it exactly as a third party would (config value, `<Plug>` mapping, RPC method name, `keymaps.buffer` name). Breaking a STABLE seam must break a test, which is what makes the semver promise of §7 real rather than aspirational.
9. **Zero-config E2E.** The §9.1 scenario as a single headless test, asserting **bytes on disk** (moved file present, original archived, operation logged) — the spec-09 §3 gate standard, run in a `$HOME` that contains nothing but the fixture.
10. **Mutation audit before handback.** For each new gate above, delete the thing it guards and confirm the gate goes red. Any gate that stays green is deleted or rewritten before the work is handed back.
11. **The Lua gates, built before the keys that need them.** Build **(a) the Lua mirror gate** — every leaf in `config.lua`'s `SCHEMA` has a reader, the exact mirror of the core `RESERVED_CONFIG_LEAVES` gate (§4.3b) — and **(b) the Lua honored-key gate** — parametrised over `SCHEMA`, each leaf asserting *default → behaviour A* and *non-default → behaviour B ≠ A* (§10.3's two-sidedness, on the Lua side). Both **before any doc-15/16/17/18 key ships.** Those four documents add roughly forty nvim-side keys and **no Python leaf**, so without this gate §4.3(d) is discharged for every one of them by doing nothing — the origin plugin's ~25 dead keys (08 §A35) reappearing at four times the scale. Its own mutation-audit line: delete one `SCHEMA` leaf's reader and the mirror gate must go red; make one leaf's reader ignore the configured value and return the default, and the honored-key gate must go red. **Cited by 15 §10, 16 §8, 17 §9 and 18 §8**, each of which adds one obligation enumerating that doc's own keys so a reviewer can count rows against tests.
