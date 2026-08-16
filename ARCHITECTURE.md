# ARCHITECTURE — builder manifest (Phase 0 scaffold)

This file is the contract between the architect/integrator and the parallel
module builders. The skeleton under `src/organize_core/` contains complete
type-annotated signatures with docstrings citing the governing spec
doc/section; bodies `raise NotImplementedError`. Builders implement bodies
**against these fixed signatures**. A builder who believes a signature is
wrong requests a change from the integrator — they do not edit shared files
or another builder's files.

Ground rules for every builder:

- Read `CLAUDE.md`, `spec/README.md`, and your module's spec docs (column
  below) IN FULL before writing code. Doc 08 items touching your module are
  regression-test obligations.
- Tests only against fixture vaults (`tests/conftest.py`) and tmp-dir
  `CorePaths` — never real state, never `~/Obsidian/Main`.
- No new runtime dependencies (stdlib + PyYAML only). Dev deps stay
  pytest + ruff. Disk is tight — install nothing else into `.venv`.
- `make test` and `make lint` green before handing back; `tests/test_scaffold.py`
  may be extended, never weakened.
- Every subprocess/file text I/O: explicit `encoding="utf-8", errors="replace"`
  + timeout (spec 06 §6 — the B1 outage class).

## SHARED FILES — architect/integrator ONLY

| File | Why shared |
|---|---|
| `pyproject.toml` | deps/entry-points affect everyone |
| `Makefile`, `.gitignore` | repo-wide tooling |
| `src/organize_core/__init__.py` | `__version__`, `API_VERSION` |
| `src/organize_core/errors.py` | the error taxonomy every module raises |
| `src/organize_core/paths.py` | the isolation contract (builder: integrator) |
| `src/organize_core/consumers/base.py` | consumer framework + registry |
| `tests/conftest.py` | the fixture vault every suite shares |
| `tests/test_scaffold.py` | the Phase-0 gate |

Builders may ADD test files freely (`tests/test_<module>*.py` per the
ownership table); they never edit another builder's files.

## Module ownership

Dependency direction (import graph, enforced — no cycles):

```
errors ◀── everything
paths  ◀── config ◀── frontmatter? (no: frontmatter is dep-free)
config ◀── index ◀── learn ◀── suggest ◀── routes
frontmatter ◀── index, fileops, suggest, routes, consumers/*
actions ◀── fileops ◀── routes, session-layer callers
llm ◀── consumers/*, (Phase-5 integrate)
store, runner ◀── cli.run-consumers
everything ◀── cli, server (composition roots)
```

`frontmatter ◀── suggest, routes` was added at Phase-1 close: both compare
tags and must go through the ONE shared `normalize_tag` (09 §2). `frontmatter`
is dependency-free, so the edge is acyclic.

Two more edges recorded at the Phase-1 fix pass:

- `frontmatter ◀── config` — `config.coerce_metadata_value` applies the doc-07
  `normalize = "kebab"` rule and must use the ONE `normalize_tag` (09 §2).
  The doc-07 coercion moved out of `cli.py` into `config.py` so BOTH doors
  enforce it; while it was CLI-private, RPC `meta.set` wrote unvalidated
  frontmatter (spec 10 §3: "CLI and UI can never disagree"). `frontmatter`
  is dependency-free, so this is acyclic; the import is function-local
  inside the one branch that needs it.
- `config ◀── consumers/base` — the back-edge that already existed in code:
  `config._registered_consumer_types()` imports `organize_core.consumers`,
  whose `base.py` imports `config`. It is broken by deferring the import
  into the function body, and the fallback is now `except ImportError` only,
  so a broken `@register` fails loudly instead of silently validating
  against a stale hardcoded type list.

`learn ◀── (duck-typed) actions`: `learn.record_action` folds an ActionRecord
into `LearningData` (spec 12 §2 "Uses" #2 — one write path, two readers). It
takes the record structurally (`getattr`), so there is no import edge at all;
`fileops` calls it through `OperationContext.on_record`, which the
composition roots wire.

`fileops` never imports `routes` (routes already imports fileops).
`OperationContext.describe` is the callable seam through which the
composition roots supply `routes.get_description` for
`targets[].description` (spec 12 §2).

| Builder | Owns (src) | Owns (tests) | Must read | Depends on |
|---|---|---|---|---|
| **frontmatter** | `frontmatter.py` | `tests/test_frontmatter*.py` | 02, 03 §8, 05 §9, 08 §A12/§B16 | — (pure; PyYAML + stdlib) |
| **index** | `index.py` | `tests/test_index*.py` | 02, 03 §7, 09 §4, 11 §3 (description loading) | frontmatter, config, paths |
| **suggest** | `suggest.py` | `tests/test_suggest*.py` (numeric goldens) | 04, 08 §A7/§A22, 13 §3 | learn (score readback), index (NoteRecord), config |
| **learn** | `learn.py` | `tests/test_learn*.py` (numeric goldens) | 04 §3-7, 08 §A22/§A23, 12 §2 (uses #2) | index (NoteRecord), config |
| **fileops** | `fileops.py` | `tests/test_fileops*.py` | 05 (all), 10 §4, 08 §A14-A16/§A24/§A25, 09 §5.6 | frontmatter, actions, index, config |
| **actions** | `actions.py` | `tests/test_actions*.py` | 12 (all), 13 §3 (query-by-similarity comes later) | — (pure + file append) |
| **session** | `session.py` | `tests/test_session*.py` | 03 §2/§6, 09 §2 | index |
| **routes** | `routes.py` | `tests/test_routes*.py` | 11 (all), 12 §1 (integrate handoff) | config, fileops, suggest, index |
| **llm** | `llm.py` | `tests/test_llm*.py` (fake endpoints) | 09 §2, 06 §3.2-3.3/§6, 11 §2, 12 §1, 08 §B11 | config |
| **config** | `config.py` | `tests/test_config*.py` | 03 §1, 06 §2, 07, 10 §3, 11 §1, 12, 13 §2, 08 §A35/§C | paths |
| **store** | `consumers/store.py` | `tests/test_store*.py` (incl. migration against a COPY of the live DB — coordinate with integrator; never open the live file directly) | 06 §1, 08 §B4/§B5 | paths |
| **runner** | `consumers/runner.py` | `tests/test_runner*.py` | 06 §1/§4/§6/§7, 08 §B2-B5/§B12-B13 | store, base, config, frontmatter, index (`is_ignored`), paths |
| **taskwarrior** | `consumers/taskwarrior.py` | `tests/test_consumer_taskwarrior*.py` (fake `task`) | 06 §3.1, 08 §B1/§B6/§B7 | base, llm |
| **learn-consumer** | `consumers/learn.py` | `tests/test_consumer_learn*.py` | 06 §3.2, 08 §B8 | base, llm, frontmatter, fileops (`atomic_write` — VAULT files, per the atomic-write ruling) |
| **question_answer** | `consumers/question_answer.py` | `tests/test_consumer_qa*.py` | 06 §3.3, 08 §B9 | base, llm, frontmatter, fileops (`atomic_write` — VAULT files) |
| **deep_research** | `consumers/deep_research.py` | `tests/test_consumer_research*.py` | 06 §3.4, 08 §B15 | base |
| **tag_router** *(Phase 4)* | `consumers/tag_router.py` | `tests/test_consumer_router*.py` | 11 §1 | base, routes |
| **auto_tagger** *(Phase 4)* | `consumers/auto_tagger.py` | `tests/test_consumer_tagger*.py` | 11 §2 | base, llm, frontmatter, index |
| **cli** (integration seat) | `cli.py` (no test_cli*) | — (defects found by the blackbox seat are reported, not self-tested) | 10 §1, 06 §4, 09 §5.6 | everything (composition root) |
| **server** (integration seat) | `server.py` | `tests/test_server*.py` | 10 §1-2, 09 §4 | everything (composition root) |
| **cli-blackbox** (test seat) | — (no src files; defects in cli.py are REPORTED to the integrator, never edited) | `tests/test_cli*.py` (subprocess-driven, real `organize` binary, fixture vault + ORGANIZE_CORE_* tmp paths) | 10 §1, 06 §4, 09 §5.6 | cli (behavior under test) |
| **paths** *(integrator)* | `paths.py` | `tests/test_paths*.py` | 10 §3 | — |

`cli` and `server` are integration seats — schedule them after the modules
they compose, or stub with fakes.

## Public interfaces (one line per symbol; full contracts in docstrings)

**errors** — `OrganizeError(message, hint)` base; `ConfigError`,
`RouteConfigError`, `VaultError`, `FrontmatterError`, `IndexingError`,
`OperationError`, `ConcurrentModificationError`, `NoAiRefusal`,
`IntegrationRejected`, `LearningDataError`, `StoreError`,
`StoreMigrationError`, `ConsumerError`, `LLMError`, `LLMUnavailable`,
`ServerError`, `AlreadyRunning`, `CoreUnavailable`, `SessionError`.

**paths** — `CorePaths(config_dir, state_dir, runtime_dir)` with derived
properties (`config_file`, `index_path`, `learning_path`, `operations_log`,
`actions_dir`, `automations_db`, `backups_dir`, `socket_path`, `lock_path`);
`CorePaths.resolve(..., env=...)` explicit > `ORGANIZE_CORE_*` > XDG >
home; `ensure_state_dirs()`; `expand(path, env)`; `default_env()`.

**config** — frozen dataclasses `VaultConfig`, `SuggestionWeights` (+
`TypeBonus`), `LearningConfig`, `SuggestionsConfig`, `FileOpsConfig`,
`MetadataFieldConfig` (+ `default_metadata_fields()`), `RouteConfig`,
`ConsumerConfig`, `LLMConfig`, `AutoOrganizeConfig`, `ServerConfig`,
`LoggingConfig`, `Config`; `load_config(paths, config_file=None)`;
`validate_config(raw, source=...)` (unknown-key law); `HealthIssue`;
`check_vault(config)`; `example_config_toml()`.

**frontmatter** — `KNOWN_FIELD_ORDER`; `Frontmatter(fields, style)` +
`.get_list(key)`; `Document(frontmatter, body)`; `split_frontmatter(text)`;
`parse(text)`; `serialize(doc)` (round-trip law); `load_file(path)`
(errors="replace"); `normalize_tag(value, extra_map)`; `merge_tags`,
`merge_sources`; `is_no_ai(doc)`.

**index** — `NoteRecord` (the shared record shape, 03 §7); `QueryCriteria`
+ `.from_filter_args`; `VaultIndex(config, index_path)` with `load`,
`flush`, `full_reindex`, `scan`, `update_file`, `remove_file`, `get`,
`query`, `search`, `para_subfolders`, `folder_children`, `values_of`,
`stats`; `para_type_for(path, config)`.

**suggest** — `Candidate`, `Suggestion`, `CaptureFeaturesView`
(`.from_record` / `.from_text` — the 13 §3 arbitrary-text path);
`string_similarity(a, b)` ∈ [0,1]; `calculate_score(capture, candidate,
config, learning, now)`; `generate_candidates(para_subfolders)`;
`suggest(capture, candidates, config, learning, now, archive_path)`.
All pure; `now` always injected.

**learn** — `LearningData` (+ `Association`, `DestinationStat`,
`PatternStat`, `Statistics`), `Features`; `load_learning` / `save_learning`;
`extract_features`; `create_association_key`; `record_move(data, capture,
dest, now)`; `get_association_score(data, capture, dest, config, now)`
(NaN-guarded); `get_pattern_score`; `apply_decay(data, config, now)`;
`get_statistics`, `get_top_destinations`, `export_data`, `import_data`,
`analyze_patterns`, `suggest_new_folders`. All pure over `LearningData`.

**fileops** — `OperationLog(log_file)` (`append`, `recent`, `undo_info`);
`OperationContext(config, index, oplog, recorder, backup_dir, dry_run,
actor, session_id)`; `OperationResult`; `LoggedOperation`; primitives
`atomic_write`, `backup_file`, `collision_free_path`, `get_archive_path`,
`FileSnapshot`/`snapshot_file`/`check_unmodified`; operations (all take
`ctx` first) `move_to_destination`, `archive_capture`, `merge_into_note`,
`merge_preview`, `append_to_note`, `update_frontmatter`, `update_tags`,
`new_folder`.

**session** — `SessionState`, `Outcome`, `SessionCounts`; `Session` with
`current`, `next`, `prev`, `skip`, `mark_processed`,
`advance_to_next_unprocessed`, `counts`, `close`; `start_session(index,
filters)`; `new_session_id()`.

**actions** — `ActionRecord` (+ `CaptureState`, `TargetState`,
`SuggestionShown`, `ActionContext`, `LLMTrace`) with `to_json`/`from_json`;
`new_action_id()` (stdlib ULID); `ActionRecorder(actions_dir)` with
`record` (never raises), `query`, `export`, `stats`.

**routes** — `RouteMatch` + `.as_suggestion()`; `resolve(note_tags,
config)`; `merge_route_suggestions(matches, scored)`; `apply_route(ctx,
capture, match)` [P4]; `apply_all(ctx, capture, matches)` [P4];
`get_description(path, index, config)`; `set_description(ctx, path, text)`
[P4].

**llm** — `LLMResponse`; `LLMClient` ABC (`generate`, `available`);
`OllamaClient(host, model, config)`; `ClaudeCLIClient(command, config)`;
`get_client(config, purpose)`; `extract_json(text)`.

**consumers/base** — `NotePayload` (+ `.no_ai`, `.tags()`); `Status`;
`ConsumerResult`; `RunContext(config, dry_run, llm)`; `Consumer` ABC
(`uses_llm` classvar, pure `__init__(ConsumerConfig)`, `should_process`,
`handle`); `register(name)` / `get_consumer_types()` (implemented).

**consumers/store** — `AutomationStore(db_path)` context manager with
`migrate() -> MigrationReport`, `needs_delivery`, `checkpoint`,
`get_emission`, `mark_seen`, `soft_purge`; `Emission`;
`STORE_SCHEMA_VERSION = 2`.

**consumers/runner** — `scan_notes(config)`; `run_consumers(config, store,
only, dry_run, paths) -> RunSummary` (+ `ConsumerSummary`);
`UnknownConsumerError`.

**cli** — `SUBCOMMANDS`; `build_parser()` (implemented); `main(argv)`;
`cmd_*` handlers (one per subcommand; `cmd_auto_organize` implemented as
the spec-13 "not implemented" stub).

**server** — `RPC_METHODS` (the 15 doc-10 §2 names, exact);
`OrganizeServer(config, paths, socket_path, idle_timeout_seconds)` with
`serve_forever`, `dispatch`, `shutdown`; `SingleInstanceLock`;
`WriterQueue.submit`; `encode_response` / `decode_request`; JSON-RPC error
constants (`ORGANIZE_ERROR = -32000` carries taxonomy name + hint in
`error.data`).

## Structural safety decisions (binding on builders)

1. **No bare file mutation.** Every mutating fileop takes an
   `OperationContext` — operation log, ActionRecorder, backup dir, dry-run
   flag are constructor-enforced, not optional kwargs. (Spec 05 invariants
   + 12 §2 every-op-records + 09 §5.6 dry-run.)
2. **One frontmatter module.** `index`, `fileops`, all consumers parse and
   serialize ONLY via `frontmatter.py` (09 §2). Any second parser is a
   review reject.
3. **Scoring/learning are pure.** No I/O, no `time.time()` inside
   `suggest.py`/`learn.py` — `now` is a parameter everywhere. Numeric
   goldens (04 §7) are exact-value assertions.
4. **Paths always injectable.** Nothing consults `os.environ` or
   `Path.home()` outside `paths.py`.
5. **Checkpointing is runner-only.** Consumers return results; only
   `runner.py` writes the store (06 §1 single-owner).
6. **`no-ai` enforced centrally** (runner via `uses_llm`; fileops/routes
   via `NoAiRefusal` for integrate) — plus locally where spec demands.

## Spec ambiguities hit during scaffolding — resolutions chosen

1. **Orchestrator home**: doc 06 names store/consumers but no module for
   the emitter/orchestrator; the task's fixed decomposition didn't either.
   → Added `consumers/runner.py` (ingestion + orchestration) so the shared
   `base.py` stays framework-only.
2. **Doc-11 consumers**: `tag_router` and `auto_tagger` (spec 11) are
   Phase 4 but their registry names and config `type` values are contract
   now. → Stub modules registered today; bodies Phase 4.
3. **`IndexError` name clash**: the taxonomy wanted `IndexError`, which
   shadows a builtin. → Named `IndexingError`.
4. **Index persistence format** (free per 02): JSON snapshot,
   `schema_version: 1`, batched atomic flush, prune-on-load. Rationale in
   `index.py` docstring (warm-in-memory requirement makes SQLite pointless
   for reads; ~10k records is a few MB).
5. **`record_move` purity**: spec 04 §3 implies persist-inside; that makes
   goldens and the 12 §2 one-write-path design awkward. → Pure mutation of
   `LearningData`; the CALLER persists and applies decay periodically
   (04 §3.6 already demands decay be lifted out of the per-move path).
6. **ULID dependency**: spec 12 §2 says `act_<ulid>`; no deps allowed
   beyond PyYAML. → stdlib ULID implementation in `actions.new_action_id`.
7. **`describe` CLI**: doc 11 §3 shows `organize describe …`; doc 10 §1's
   subcommand list (which the task fixed) has `routes …` but no
   `describe`. → `organize routes describe <path> <text>`.
8. **`record` vs `actions`**: task said "record/actions export". → Two
   subcommands: `record` (append an ActionRecord from stdin JSON — the
   external-actor write path) and `actions export|stats`.
9. **Empty session**: 03 §2 says notify-and-don't-open-UI on zero matches;
   that's client behavior. → `start_session` returns an ACTIVE session
   with an empty capture list; clients render the notice (no exception
   for a non-error).
10. **Route suggestions vs `max_suggestions`**: 11 §1 puts routes above
    scored suggestions but doesn't say how they count against the list
    cap. → `merge_route_suggestions` truncates SCORED entries only; routes
    and the archive entry always survive.
11. **Multi-route archive ownership**: `append_to_note` must NOT archive
    (11 §1 archives once after ALL destinations succeed). → Archiving
    lives in `routes.apply_all`; plain single-target ops archive in
    `move_to_destination`/`merge_into_note` per 05.
12. **`deep_research` and `no-ai`**: 06 §2's rule covers "LLM-calling
    consumers"; deep_research shells to an agent rather than calling an
    LLM itself. → It must still honor `no-ai` in `should_process` (the
    dispatched agent is AI tooling under the 02 vault law); `uses_llm`
    stays False because the central guard is about prompt-building.
13. **Store schema version**: live DB has no version marker. → It is
    implicitly v1; `STORE_SCHEMA_VERSION = 2` adds `last_seen` + meta;
    `migrate()` returns a `MigrationReport` and aborts loudly rather than
    half-migrating.
14. **`suggest --text` tags**: 13 §3 wants scoring for arbitrary text, but
    the seven signals are frontmatter-driven. → `CaptureFeaturesView.
    from_text(text, tags=None)`; untagged text scores on context +
    type-bonus only unless the caller supplies tags. No new signals
    invented (04 §2 prohibition).
15. **Python version**: spec says 3.11+; the system interpreter (and the
    venv) is 3.13. `requires-python = ">=3.11"` — builders must not use
    3.12+-only syntax.

## Integrator rulings (post-scaffold)

- **cli.py ownership (2026-08-16 collision, CORRECTED)**: the first
  version of this ruling described a combined "cli+server seat" — that was
  factually wrong. Actual state: the **cli** seat wrote and owns the
  current `cli.py`; a separate **server** seat owns `server.py` +
  `tests/test_server*.py`; ALL `tests/test_cli*.py` belong to the
  **cli-blackbox** seat, which tests the shipped binary via subprocess and
  reports defects to the integrator instead of editing `cli.py`. Neither
  seat touches the others' files.
- **CLI behavioral rulings (binding on test writers)**:
  (a) `--dry-run move` is vault-write-free but NOT state-silent: it writes
  `[DRY-RUN]` operations.log lines and a dry-run-marked ActionRecord
  (09 §5.6 log-only intent); index/learning updates are skipped;
  `actions stats/query` exclude dry-run records by default. Tests assert
  zero VAULT writes — not "no ActionRecord".
  (b) `suggest` output is routes-merged per ruling #10: combined list
  capped, routes + archive always survive, scored entries may truncate
  out. In `--json`, route entries are identified by the populated `route`
  field, not a sentinel score.
  (c) `health` exits 0 when warnings-only (still printed), 1 on any ERROR;
  `--strict` promotes warnings. First-run "no index snapshot" is a
  warning, never a failure.
- **Additive CLI surfaces approved**: per-subcommand `--json` (see above),
  global `--debug`, `routes describe` read-only form,
  `health --example-config`.
- **Additive read-only RPC extras approved**: `meta.fields` / `meta.values`
  (spec 07 completion, otherwise unreachable from a thin client) and now
  `folder.list` / `folder.children` — spec 10 §1 forbids the nvim client
  from scanning the vault, so the destination picker and the 03 §3 browse
  view need `VaultIndex.para_subfolders`/`folder_children` over the wire.
  `folder.list` enumerates from DISK (an empty folder is still a valid
  destination) and is uncapped; capping is `suggest.for_note`'s job. The
  `EXTRA_METHODS` set in `tests/test_server_protocol.py` pins the list and
  fails on any further addition — that failure IS the approval step.
- **`op.skip` approved — the one WRITING addition beyond spec 10 §2.** Spec
  03 §2/§6 makes skip a first-class session decision and doc 12 §2 already
  lists `skip` in the ActionRecord operation enum, but spec 10 §2 named no
  method for it, so the decision was unrecordable from a thin client: every
  press of `s` silently discarded the counterfactual that `actions stats`
  measures acceptance rate AGAINST.

  Contract: `op.skip {note, session_id REQUIRED, suggestions_shown?,
  durations_ms?}` → `{ok: true, outcome: "skipped"}`. The param is `note`,
  NOT `path` — it names the capture being decided about rather than a file
  being operated on. `session_id` is required because 03 §6 scopes "skipped"
  to a session; a skip belonging to none is not a decision anyone can read
  back. An unresolvable id raises `SessionError` (sessions are in-memory, so
  a core restart legitimately invalidates every id a client holds), as does
  a note absent from that session's capture list — while an unknown PATH is
  a `VaultError` from `_record_for`, before the session is consulted.

  Three deliberate asymmetries, each pinned by a test:
  1. **ActionRecord yes, oplog line NO.** The operation log records what
     happened to the VAULT; an OK line for an operation that touched nothing
     would claim a mutation that never happened.
  2. **In `MUTATING_METHODS`, NOT in `INDEX_CHANGING_METHODS`.** It is
     queued through the one writer because it appends to the actions corpus
     (a state file), but it moves no note — an `index-updated` per skip
     would make every client refetch on a keystroke that changed nothing.
  3. **No learning signal, positive or negative** (04 §3). Load-bearing
     rather than incidental: `fileops.skip_capture` goes through the SAME
     `_record_action` path as a move, so the only thing stopping a skip from
     teaching the learner is `learn.record_action` returning `None` for it.
     A TRAP TEST asserts the actions corpus gains exactly one record while
     `learning.json` stays BYTE-identical; both halves were checked against
     firing controls (a move does change those bytes, and the count does
     move), so the trap cannot pass vacuously.

  Dry-run records with `context.dry_run = true` and leaves session state
  alone, so a rehearsal cannot silently consume the backlog. Session
  membership is validated on BOTH paths, so a dry run cannot succeed where
  the real call would raise.
- **Known divergence for the integrator**: fileops/cli docstrings still
  say dry-run means "no log mutation" — reconcile docstrings to ruling (a)
  post-hold.
- **Seam rulings for the INTEGRATOR (approved, implement these)**:
  (1) actions.py: move `DRY_RUN_FILTER_KEY` into actions.py as the single
  home (cli imports it); add `ActionRecord.is_dry_run` property reading
  `context.filters[DRY_RUN_FILTER_KEY]`; `ActionRecorder.query()` gains
  keyword-only `include_dry_run: bool = False` with stats()/export()
  flowing through query. Then DELETE cli.py's `_CorpusView` subclass. No
  schema change.
  (2) fileops.update_frontmatter: add keyword-only
  `replace_keys: frozenset[str] = frozenset()` — listed keys REPLACE
  instead of merging (default merge behavior unchanged, update_tags
  untouched). Rationale: spec 07 `append = false` on a list field must be
  ONE logical edit = ONE ActionRecord/oplog line; the clear-then-set
  workaround corrupts the doc-12 one-action-one-record property.
  (3) server.py: socket/bind failures raise `ServerError(message, hint)`
  naming the offending path + override channels (--socket /
  [server] socket_path / --runtime-dir), never a bare OSError.
- **Regression tests owed** (cli-blackbox seat or integrator): (i) literal
  `$` in vault filenames resolvable (expansion-order defect, fixed); (ii)
  `set-meta` on a note with NO frontmatter block works per spec 07
  annotate-without-filing (raw AttributeError, fixed).
- **Atomic-write triplication (architect conformance pass, LOW)**: three
  implementations exist — `fileops.atomic_write` (vault files: perm/EXDEV
  handling), `learn._atomic_write_text` and index's inline mkstemp+replace
  (state files). The vault-vs-state split is INTENTIONAL for now (merging
  would give the pure scoring module an import of the mutation layer —
  structural decision 3). Disposition: when Phase 3 adds consumer state
  writers, lift ONE state-file atomic-write helper into a small shared
  module (not fileops) and migrate learn/index to it.
- **PARA VOCABULARY — singular is the type VALUE, plural names a config
  KEY.** One rule, applied everywhere:

  | Where | Form | Example |
  |---|---|---|
  | `Suggestion.type` | SINGULAR | `project`, `area`, `resource`, `archive` |
  | `folder.list` emitted `type` | SINGULAR | `area` |
  | `NoteRecord.para_type`, search criteria, `project/x` tag prefixes | SINGULAR | `project` (plus `capture`, `other`) |
  | `RouteMatch.para_type` / `_destination_type()` | SINGULAR | `area` |
  | `folder.create`'s `para_type` PARAM | PLURAL — a `vault.para_folders` key | `projects` |
  | `folder.list`'s optional `para_type` FILTER param | PLURAL — a `vault.para_folders` key | `projects` |

  Both PARAMS keep accepting what they accept today (either form; the
  singular is normalized to the key), because the UI's `<leader>np` speaks
  singular. They are documented as "a `para_folders` key, not a type value"
  precisely because they are the only plural on the wire. Internally
  `suggest.Candidate.type` is also the plural KEY — it is what
  `VaultIndex.para_subfolders` is keyed by and what the `type_bonus` weights
  are keyed by — and is converted to singular at `Suggestion` construction.

  This RESOLVES a contradiction between two spec docs: 03 §7 defines the
  PARA types with the plural folder-name keys, while 02's live-state
  inventory shows SINGULAR values in the real `index.json`. We side with
  **02 — parity with observed reality**, and read 03 §7 as describing the
  DERIVATION SOURCE (the folder keys the values are derived from) rather
  than the value vocabulary itself.
- **Where the error line falls (RPC error vs `result.ok = false`)** — the
  distinction is ADDRESSING vs WORLD STATE, and callers must handle both:
  - **ADDRESSING / VALIDATION failures RAISE** a taxonomy error and reach
    the wire as `-32000` with `error.data.kind` (unknown config key —
    including `folder.create` with an unknown `para_type`, invalid folder
    name, unknown method, no-AI refusal, concurrent modification). The
    request never named a real operation, so there is nothing to attempt
    and no oplog line to write.
  - **WORLD-STATE failures during a validly-addressed op return
    `ok = false`** plus a FAILED oplog line (source missing, unwritable
    destination, copy failure). The operation was real and was attempted;
    its failure is a normal outcome the UI renders, not an exception.
  - So a client MUST check `result.ok` AND branch on `error.data.kind` — a
    200-shaped response is not proof the operation happened.

  `fileops.new_folder` is the worked example: an unknown `para_type`, an
  empty name and a name carrying a path separator all raise `ConfigError`,
  so ONE `error.data.kind` covers every way of misaddressing
  `folder.create`; only an unwritable PARA root reaches `ok=false`.
- **Phase gates run `make gate`** (test + perf + lint): the spec 09 §4
  full-scale perf tests are `-m slow` and excluded from the default suite,
  so a bare `make test` is NOT a complete phase gate.

## Phase-2 integrator inbox (facts from the commands seat, verified)

- **actions.lua dispatch-name contract gap**: commands.lua + checkhealth
  expect 20 dispatch names; actions.lua is missing 7 — `start`, `next`,
  `prev`, `reindex`, `debug`, `refresh`, `set_meta` (it has
  `next_capture`/`prev_capture`/`refresh_suggestions`/`set_meta_field`
  instead and lacks start/reindex/debug entirely). `:ParaOrganize start`
  has nowhere to go until reconciled. Integrator: align actions.lua to the
  20-name contract (checkhealth asserts it).
- **Wire fact**: `vim.json.encode({})` emits `[]`, which the server
  rejects (-32602) before method lookup — every no-arg RPC must send
  `vim.empty_dict()`. Fixed in phc_support + pickers; any new call site
  must follow.
- **Wire fact — RESOLVED (core patch)**: the core used to mix vocabularies,
  with `folder.list` emitting singular `type` while `Suggestion.type` was
  plural. Settled by the PARA VOCABULARY ruling above: **core output is now
  uniformly SINGULAR** — `Suggestion.type` migrated plural → singular (the
  archive entry's type is now `archive`), and `folder.list` keeps emitting
  singular. Plural survives only on the two params that address a
  `para_folders` KEY, which still take either form. The client-side
  normalization shim in `pickers.suggestion_entry` is therefore redundant
  and is scheduled for DELETION in the Phase-2 fix stage — left in place
  here because lua is contended by another workflow.
- **plenary wart**: `PlenaryBustedFile` does not forward `minimal_init` to
  its child nvim (child loads the user's real config!). Run specs
  in-process via `-c "lua require('plenary.busted').run(<abs>)"` or
  `PlenaryBustedDirectory {minimal_init=...}`.
- **`--json` flag**: the per-subcommand `--json` (machine-readable output)
  added by the cli+server seat is an approved ADDITIVE surface change —
  spec 10 §2 makes other frontends (Claude agents) first-class CLI
  consumers, which needs machine-readable output. It must never change
  human-output defaults, and `SUBCOMMANDS`/`build_parser` remain the
  scaffold contract.

## Integrator seam rulings — Phase 1 close (2026-08-16)

Every seam the builders raised, with the decision and its reason. Rulings
that changed code are listed with the pinning test.

### Granted (implemented)

1. **`RouteConfig.review`** (config seat). Spec 12 §1 makes `review = "auto"`
   an explicit PER-ROUTE opt-in, so it is real config, not an unknown key.
   Added `review: ReviewGate = "diff"`. Because `review` steers integrate
   results only, setting it to `"auto"` on a `move`/`append` route is a loud
   `RouteConfigError` rather than a silently-ignored key (03 §1 / 08 §A35).
   The config seat's test that pinned `review` as unknown was replaced with
   one using a genuinely unknown key. → `tests/test_config_routes.py`.
2. **`[integrate]` config section** (config seat). `IntegrateConfig(
   default_mode="manual", review="diff", max_deleted_lines=0)` on
   `Config.integrate` — the global edit-mode default, the review-gate default
   and the configurable deletion threshold of the integrate hard-reject, all
   from spec 12 §1. Phase 5 implements enforcement; the keys are contract and
   validate from today so a spec-conformant config never trips the
   unknown-key law. The shipped example now demonstrates all three route
   modes, including `integrate`. → `tests/test_config*.py`.
3. **`ActionContext.dry_run`** (fileops + cli seats, one seam). Promoted from
   the `filters["dry_run"]` convention to a first-class field, and
   `ActionRecorder.query/stats/export` now default to
   `include_dry_run=False`. A dry run is logged (09 §5.6 wants the intended
   action on paper) but is never a doc-12 precedent. Putting the exclusion in
   the RECORDER means every corpus reader inherits it — cli.py's local
   `_CorpusView` subclass is deleted, and the doc-13 retrieval to come gets
   the rule for free. `from_json` fails CLOSED: anything but exactly `false`
   counts as a dry run. → `tests/test_actions_recorder.py`,
   `tests/test_integration.py`.
4. **`merge_tags(..., normalized=, extra_map=)`** (frontmatter seat). Spec
   05 §2.6 (move) says "dedupes case-insensitively"; spec 05 §4 (merge) says
   "dedupe on normalized form". Those are genuinely different keys and both
   are correct, so the key is a keyword instead of one implementation being
   wrong half the time. `merge_into_note` passes `normalized=True` with the
   configured `tag_normalization` map. → `tests/test_frontmatter.py`,
   `tests/test_integration.py`.
5. **`update_frontmatter(..., replace_keys=)`** (cli seat). Spec 07 lets a
   `[[metadata_fields]]` entry declare `append = false`; `tags` merges by
   contract, so `organize set-meta` had to clear-then-set — two writes, two
   log lines and TWO ActionRecords for one logical edit, i.e. a corpus that
   misreports what Matt did. Now one atomic op. → `tests/test_integration.py`.
6. **`ServerError` for an unusable socket path** (cli seat). `organize serve
   --socket <bad>` surfaced a bare `PermissionError`; the CLI could not
   attach a hint to an exception it did not raise (09 §1.5). `_bind` now
   converts every OS-level failure, with errno-specific hints — including the
   AF_UNIX ~104-byte path limit, which CPython reports as a bare `OSError`
   with no errno, so the hint is derived from the path length. The CLI also
   no longer prints "listening on X" before the bind has actually succeeded.
   → `tests/test_integration.py`.
7. **`RouteMatch.para_type`** (routes seat). `as_suggestion()` takes no
   Config and guessed the PARA type from the destination's leading path
   segment; a vault that renames a PARA root (`projects = "p"`) would type
   its route entries `"p"` while scored entries for the same folder were
   typed `"projects"`, and the UI groups on that field. `resolve()` has the
   Config and now fills the field; the leading-segment reading remains the
   fallback for a hand-built `RouteMatch`. → `tests/test_routes.py`.
8. **frontmatter single-parse + libyaml** (index seat, perf). `_parse_with_
   pyyaml` parsed every block TWICE (`yaml.load` then `yaml.compose`) on the
   pure-Python loader. It now composes once and constructs the document from
   that node graph, and derives from `CSafeLoader` when the build has
   libyaml. Both loaders consult the same Python `Resolver`, so the
   timestamp-stays-a-string surgery is unaffected — pinned, since switching
   the base class would otherwise be an invisible behaviour change.
   **10k full reindex went from ~11.8 s (extrapolated) to a measured 1.67 s
   against the 5 s gate.** → `tests/test_frontmatter.py`,
   `tests/test_integration.py`.

### Confirmed as-is (no code change; the seat's reading was right)

9. **Frontmatter field ordering on rewrite.** Fields that came FROM the
   source keep their source position; fields the caller ADDS are placed by
   `KNOWN_FIELD_ORDER`. Reordering a user's existing file is exactly the
   churn the round-trip law forbids. Downstream suites must assert per-field
   content, never canonical re-ordering of a pre-existing file.
10. **FrontmatterError tolerance boundary.** Unparseable YAML raises; the
    INDEX catches it and indexes the note with `parse_error=True` and a
    warning, while a MUTATING op refuses loudly rather than writing a guessed
    file (03 §7 + 09 §1.5). Confirmed end-to-end in
    `tests/test_integration.py`.
11. **`new_folder` needs no index hook.** Spec 05 §6 says "refresh folder
    caches/index dirs", but `VaultIndex.para_subfolders` enumerates
    directories from DISK, so an empty new folder is a suggestion candidate
    the moment mkdir returns. Pinned by
    `test_new_folder_is_immediately_a_suggestion_candidate`, which will fail
    if anyone makes `para_subfolders` record-derived.
12. **`merge_route_suggestions` needs no `max_suggestions`.** Both
    composition roots (`cli.cmd_suggest`, `server._suggest_for_note`) pass
    `suggest()` output unmodified, already truncated. Recorded in the
    docstring; a future caller wanting a different cap truncates first.
13. **`CaptureFeaturesView.modalities`** (suggest seat) — CONFIRMED, keep.
    Without it `record_move` writes an association key including
    `modalities:` that `calculate_score` could never reproduce, so the
    learned signal would silently never fire for any capture with
    modalities. It stays a NON-signal (04 §2); the field exists for key
    parity only.
14. **`CaptureLike` protocol widening in learn.py** (suggest seat) —
    CONFIRMED. Widening only, `NoteRecord` still conforms, and it avoids a
    `learn → suggest` import cycle.

### Rejected (with reason)

15. **`learn` → `fileops.atomic_write`.** The two atomic writers implement
    the same spec 05 §1.3 rule, but making the pure scoring/learning module
    import the whole mutation layer (which pulls in `actions` and `index`) to
    reuse ~15 lines would cost more than the duplication: structural decision
    3 ("scoring/learning are pure") stops being enforceable. `learn` keeps
    its local helper; its no-leftover-temp behaviour is already tested.
16. **`learn.clear()`.** Spec 04 §6 lists it in the parity API, but no CLI or
    RPC surface calls it and `LearningData()` is the constructor. Not added;
    revisit if a `organize learn --reset` surface appears.
17. **`ActionSchemaError` into `errors.py`.** It already derives from
    `OrganizeError`, so CLI/RPC error mapping works. Moving it would widen
    the shared taxonomy for one module's payload-validation error. Stays in
    `actions.py`.
18. **`env=` on `CorePaths`/`load_config`.** `config.py` reads no environment
    at all — it delegates to `paths.expand`, which keeps the single env
    touchpoint in `paths.py` (structural decision 4). Adding an env channel
    to `load_config` would reopen exactly that. Tests inject via `CorePaths`.

### Defect found by the new integration suite

19. **CLI and server disagreed on a missing note.** `organize move
    <nonexistent>` raised `VaultError`; the server's `op.move`/`note.get`
    raised `ServerError`. `error.data.kind` is what clients branch on, and
    `ServerError` invites a reconnect/retry when the only useful response is
    to fix the path. The server now raises `VaultError` and `note.get` goes
    through the same `_record_for` helper as the `op.*` methods (it had a
    second, subtly different copy of the lookup). → pinned by
    `test_the_two_doors_reject_the_same_bad_move`.

## Public-interface additions since the scaffold

Recorded so the manifest stays exhaustive. All are additive; no fixed
skeleton signature changed incompatibly.

- **config** — `IntegrateConfig`, `Config.integrate`, `RouteConfig.review`,
  type aliases `EditMode` / `ReviewGate`, `CORE_KEYMAPS`,
  `EXAMPLE_CONFIG_TOML`, `REMOVED_KEYS`.
- **frontmatter** — `merge_tags(existing, new, *, normalized=False,
  extra_map=None)`.
- **index** — `NoteRecord.parse_error` and `NoteRecord.extra` (documented
  extensions of the 03 §7 shape; both trailing and defaulted).
- **suggest** — `CaptureFeaturesView.modalities`; `ARCHIVE_SUGGESTION_TYPE`
  (defined here, beside the rest of the archive entry).
- **learn** — `CaptureLike` protocol (widened parameter annotations only).
- **actions** — `ActionContext.dry_run`; `include_dry_run=` on `query`,
  `stats` and `export`; `ActionSchemaError`; `OPERATIONS` / `EDIT_MODES` /
  `VERDICTS` / `TARGET_ROLES`; `ActionRecorder.month_files()`.
- **fileops** — `OperationContext.clock`; `LoggedOperation.backup` and
  `.dry_run`; `update_frontmatter(..., replace_keys=)`.
  (`DRY_RUN_FILTER_KEY` was removed — superseded by `ActionContext.dry_run`.)
- **routes** — `RouteMatch.para_type`, `ROUTE_SUGGESTION_SCORE`.
  (`ARCHIVE_SUGGESTION_TYPE` moved to `suggest` — routes imports and
  re-exports it, so `from organize_core.routes import ...` still resolves.)
- **session** — `default_filters()`, `TERMINAL_OUTCOMES`.
- **frontmatter (Phase 3)** — `fields_are_no_ai(fields)`: the field-level
  `no-ai` rule over a plain mapping. `is_no_ai(doc)` and
  `NotePayload.no_ai` both delegate to it — ONE rule (Phase-3 ruling).
- **index (Phase 3)** — `is_ignored(rel, patterns)`, promoted from private.
  The automation pipeline's ingestion walk applies the same
  `vault.ignore_patterns` and had grown a second copy; consumer
  `include_paths`/`exclude_paths` remain a DIFFERENT rule (06 §2,
  prefix-or-glob, no component match) and stay in `runner._matches`.
- **consumers/base (Phase 3)** — `NotePayload.no_ai` / `NotePayload.tags()`
  implemented (delegating, per the ruling); `RunContext.paths: CorePaths |
  None`, populated by the runner from the composition root so a consumer
  can derive a STATE location (taskwarrior's
  `<state>/backups/taskwarrior/<UTC-ts>`, 06 §3.1) without reading the
  environment.
- **consumers/store (Phase 3)** — `hash_note_text(raw_text)`;
  `AutomationStore.open()` / `.close()` / `.schema_version()` /
  `.upsert_note()` / `.list_purged()` / `.restore_purged()`; constants
  `TERMINAL_STATUSES` / `RETRYABLE_STATUSES` / `V1_ONLY_STATUSES` /
  `DEFAULT_RETENTION_DAYS`; `MigrationReport.notes_kept` / `.anomalies` /
  `.created` / `.no_op` / `.changed` / `.summary()`. All six scaffold
  signatures unchanged.
- **consumers/runner (Phase 3)** — `UnknownConsumerError(ConfigError)`, so
  the CLI can map an unknown `--consumer` to exit 2 (06 §4) without
  sniffing exception text; `DEFAULT_PURGE_RETENTION_DAYS`.
- **cli (Phase 3)** — subcommand `migrate-store`; `cmd_migrate_store`.
- **server** — `MUTATING_METHODS`, `INDEX_CHANGING_METHODS`,
  `EVENT_INDEX_UPDATED` / `EVENT_OP_PROGRESS` / `EVENT_TYPES` /
  `EVENT_METHOD`, `NOT_IMPLEMENTED = -32001`, `RpcException`,
  `encode_event()`, `handshake_line()`, `OrganizeServer.emit()` / `ready` /
  `connection_count` / `stopping`, `empty_array_as_object()` (the one
  empty-array→`{}` coercion every object-typed request field routes
  through).

## Cross-module tests and measured performance (Phase 1 close)

`tests/test_integration.py` (integrator seat) covers what no single seat
could: real-`VaultIndex` fileops round-trips (including the persisted
snapshot re-opened as a fresh process would) and CLI-vs-server behavioural
equivalence for `move` — same vault bytes, same frontmatter, same
ActionRecords, same operations log, same learning write, same rejection
taxonomy.

Measured 2026-08-16 (spec 09 §4 gates in brackets):

| Measurement | Result | Gate |
|---|---|---|
| `full_reindex`, 10 000 generated notes | **1.7 s** idle, 2.9 s under load | < 5 s |
| `full_reindex`, 1 000 notes (standalone / under suite load) | 0.17 s / 0.41 s | < 2 s (CI) |
| `update_file`, incremental, 10k-note index | 0.33 ms | < 500 ms |
| `suggest()` on a warm 1 000-note index | 0.03 ms median, 0.07 ms worst | < 100 ms |

The 10k case is `pytest -m slow` (deselected by default via `addopts`, since
generating the vault dominates its runtime); the 1k gate always runs.

## Phase-2 fix-stage dispositions (architect rulings, routed 2026-08-16)

- **rpc.lua normalize_params goes RECURSIVE** (the chokepoint fix): every
  nested empty table destined for an object-typed field is normalized via
  `vim.empty_dict()` at the one entry point. Per-call-site helpers
  (`pickers.obj()` etc.) are then retired — defensive per-site fixes are
  the same bug waiting in every future seat. (Core-side rider: the server
  also accepts `[]` as `{}` for object-typed FIELDS — bundled in the
  pending core patch.)
- **rpc calling conventions stay asymmetric but DOCUMENTED**:
  `Client:request(m, p, cb)` calls back `(err, result)` (node-style);
  `request_sync` returns `(result, err)` (Lua idiom). Mandatory docstring
  on each naming the other's order; the pin test (via real rpc.connect)
  is the permanent regression guard. REMOVE actions.lua's defensive
  both-order normalization once docstrings land — it masks exactly this
  bug class.
- **minimal_init consolidation MUST keep the in-process
  :PlenaryBustedFile override** — plenary's spawned child nvim loads the
  user's real config (a real-environment leak). The override is a
  correctness requirement, not style.
- **phc_support.lua folds into helpers.lua** (approved, mechanical).
- **state.has_session() is a required public addition** to state.lua;
  commands.lua's unknown-treated-as-yes gate fallback is correct fail-open
  behavior and stays.
- **actions.lua dispatch contract** (re-affirmed): commands.SUBCOMMANDS is
  the contract; actions adds aliases next/prev/refresh/set_meta to its
  existing impls and implements start/reindex/debug. The checkhealth
  "actions missing:" check stays a permanent health ERROR.

## Integrator seam rulings — Phase 2 close (nvim thin client)

Every seam the three lua seats raised, with the decision. Files written by the
integrator this phase: `lua/para-organize/{init,config,state}.lua`,
`tests/plugin/{minimal_init,helpers,e2e_spec}.lua`, the `test-plugin` Makefile
target, `doc/MIGRATION-from-old-setup.md`.

### Granted (implemented)

1. **`config.lua` exposes the core-process keys** (rpc+core seat). `get()` is
   the accessor every seat already probes for; `core_options()` returns the
   exact table `core.ensure_running` consumes. Both spellings — top-level
   `socket_path`/`core_cmd` and the nested `core.*` block — are accepted, and
   **`setup()` normalises a nested spelling UP to the top level**. Without
   that normalisation the top-level DEFAULT (`{"organize","serve"}`) silently
   beat a user's `core.core_cmd`: a silent wrong answer, and the first thing
   the E2E gate caught.
2. **`state.has_session()`** (commands seat). A real predicate, so
   `:ParaOrganize skip` with no session refuses cleanly instead of relying on
   shape-sniffing. `state.session` stays a real field for the fallback path.
3. **Three additive config keys blessed** (ui+actions seat):
   `keymaps.buffer.sort_cycle` (default `S` — spec 03's documented resolution
   of the accidental `s`/`s` double-binding), `ui.capture_pane_keymaps`
   (`core` | `navigation` | `none`), `ui.close_on_complete`. All three are in
   the schema and in the migration note.
4. **`tests/plugin/minimal_init.lua` and `tests/plugin/helpers.lua`
   consolidated.** The three per-seat minimal_inits and the two per-seat
   support modules are now shims onto one implementation each, so every seat's
   documented command line still works and there is one environment. The
   consolidated init keeps BOTH fixes the seats found: the user's real config
   is removed from `rtp`/`packpath`, and `:PlenaryBustedFile` is overridden to
   run in-process (plenary's own command spawns a child nvim without
   forwarding `-u`).
5. **`actions.lua` aligned to the 20-name dispatch contract.** Added `start`,
   `reindex`, `debug` (each delegating to the composition root, which owns the
   core connection) and the aliases `next`/`prev`/`previous`/`refresh`/
   `set_meta`. `:checkhealth para-organize` now reports "every command and
   keymap resolves to an action".
6. **Stale-reply guard in `actions.rpc`.** A reply arriving after its session
   was torn down (`<Esc>` mid-load, or `start` with new filters) is dropped:
   it neither drives the UI nor emits an error notification. Comparing the
   session identity at reply time is the only way to distinguish "the core
   failed" from "we stopped listening", and without it a stale
   `suggest.for_note` could write into the NEXT session's state.
7. **`pickers.resolve_client` table-error crash** (reported by the ui+actions
   seat) — already fixed in-seat by `M.error_message(cerr)` flattening
   `core.ensure_running`'s structured error to a string; verified by the
   pickers seat's own "core unreachable" specs, no further action.

### Confirmed as-is (no change; the seat's reading was right)

8. **`Client:request(m,p,cb)` is `cb(err, result)`** while `request_sync`
   returns `result, err`. Both entry points keep their orders; `actions`
   normalises defensively via `callback_style`. The asymmetry is pinned by
   tests on both sides.
9. **Nested empty objects must be `vim.empty_dict()`.** `rpc.lua` normalises
   the TOP-LEVEL params table only. `init.obj()` and `pickers.obj()` are the
   two helpers; every new call site follows.
10. **Spec 07 acceptance test 4 (keymap collision raises at setup) is split.**
    Metadata-vs-core collisions are enforced by the CORE (`CORE_KEYMAPS` in
    `organize_core.config`, because spec 10 §3 moved `metadata_fields` into
    `config.toml`); core-vs-core collisions from `keymaps.buffer` overrides
    raise in `init.setup()` via `actions.detect_collisions()`. A failed gate
    rolls the config back, so a rejected `setup()` never leaves a
    half-applied table behind.

### Deferred

11. **`folder.list` for the browse view** — already granted and shipped by the
    core seat (`folder.list` / `folder.children`). `actions.open_item`'s
    degraded `search.query` fallback stays as feature detection for an older
    core; the redundant `pickers.suggestion_entry` plural-normalisation is the
    remaining Phase-2 cleanup.

### Phase-2 measured results

`make test-plugin` — 202 specs across 8 files, all green, each spec in its own
headless nvim: rpc 25, core 12, ui 23, actions 44, commands 32, pickers 36,
health 23, **e2e 7**. `make test` — 1277 passed, untouched by this phase.

The E2E gate (spec 09 §3) drives the real stack: fixture vault built by
`tests/conftest.py` → `organize serve` spawned BY THE PLUGIN → `:ParaOrganize
start` → two panes rendered with the top suggestion → accept → assertions
against BYTES ON DISK (moved copy, original archived under its own filename,
`<type>/<folder>` tag added, `processing_status: organized`, `learning.json`,
`operations.log`, an `operation=move` ActionRecord) → next capture auto-loaded
→ `:ParaOrganize stop` → zero orphan buffers/windows/autocmds, and the core
still listening (stop closes the session, not the shared daemon).

## Real-data findings — fix stage (architect rulings, 2026-08-16)

Five probe seats drove the core against a read-only mirror of the live vault
(70,377 notes; 2,445 raw captures; 13,362 indexed under the test config). Every
finding below was reproduced on that corpus before it was fixed, and every fix
carries a FIXTURE-based regression test — no committed test references the
mirror.

### Suggestion scoring (spec 04 §2)

1. **`min_confidence` applies to the SIGNAL score, not the total** —
   `suggest.suggest` now compares `score - _type_bonus(...)` against
   `suggestions.learning.min_confidence`; the full score still ranks. The
   default floor (0.3) is >= the areas (0.2) and resources (0.1) type bonuses,
   so comparing it against the TOTAL made the always-firing signal #7 decide
   survival: **83 of the vault's 136 PARA folders were unreachable at any
   `max_suggestions`**, and 1450 of 1858 backlog captures (78%) received an
   identical 9-row list of `projects/` folders, every row scored 0.30 with an
   empty reason list, ordered by an ASCII accident (`projects/B2-polish` was
   rank 1 for all 1450 because "B" sorts before "a"). This RESOLVES spec 04
   §2's self-contradiction — "every folder is technically a candidate — ranking
   does the real work" vs "apply `min_confidence` 0.3 as the documented floor":
   every folder is still scored and the bonus still ranks, but a candidate with
   no evidence is not offered. Consequences, all pinned:
   a zero-signal capture returns the archive entry ALONE (the honest "no
   confident destination" state), every non-archive suggestion carries >= 1
   reason (which closes the "rank 1 with `reasons: []`" finding without
   inventing a reason string for signal 7), and projects > areas > resources
   still holds among survivors (04 §7 acceptance).
   `test_min_confidence_drops_bare_areas_and_resources_but_keeps_projects` —
   which pinned the ⚠-flagged literal reading, not an acceptance item — is
   RETIRED and replaced by
   `test_min_confidence_applies_to_the_signal_score_not_the_total`,
   `test_a_capture_with_no_signal_gets_the_archive_entry_alone` and
   `test_every_non_archive_suggestion_carries_at_least_one_reason`.
2. **Signal 6 matches at TOKEN granularity** — a sanctioned deviation from 04
   §2 #6's literal "case-insensitive substring, either way". Both sides are
   split on non-alphanumerics and one side's token list must be a CONTIGUOUS
   run inside the other's. The raw substring test made short folder names match
   inside unrelated words: `resources/ui` was the rank-1 suggestion for a
   capture whose entire context was "quitting toastmasters" (the `ui` inside
   q-UI-tting). Context is the only signal that reads free prose and it fired
   just 16 times across the whole backlog, so its precision matters far more
   than its recall. Weight and signal count unchanged.
3. **Signal 2 absorbs morphological variation, in `suggest.py` ONLY** — the
   normalized-tag signal now also fires (at its own 1.5 weight) when a
   configured suffix strip (`suggestions.tag_suffix_strip`, default
   `["-system", "-systems"]`) and/or a naive singular/plural reaches the folder
   name, with a reason string naming the derivation
   (`Tag 'productivity-system' ~ folder 'productivity'`). Matt's two dominant
   tag conventions systematically missed their obvious destination:
   `productivity-system` (65 captures) never reached `areas/productivity`,
   `principle` (39) never reached `areas/principles`. HARD CONSTRAINT, pinned
   by `test_signal_2_variant_matching_never_touches_the_shared_normalizer`:
   `frontmatter.normalize_tag` is NOT touched — it also builds learning
   association keys, the `<type>/<folder>` tag a move writes and the
   frontmatter that lands on disk, so morphing it would corrupt learning keys
   and vault data. Variants are derived at MATCH TIME. No 8th signal.
4. **Signals 1 and 2 double-firing on an already-normalized tag stays** —
   REJECTED. It is spec-literal, it inflates uniformly (so ordering is
   unaffected), and it is pinned by the 3.8/3.7/3.6 numeric goldens.
5. **Not defects, recorded so the weights are not mistaken for load-bearing
   tuning**: `alias_similarity` (1.1) fired 0 times and `source_match` (1.3)
   fired once across 1858 captures — captures carry timestamp-shaped aliases
   and the generic source `me` (1176 of them). Answered on the record: alias
   similarity is not dominating nonsensically; it never fires.

### File operations (spec 05)

6. **Move verifies BYTES** — `move_to_destination` compared
   `_read_text(dest_path)` (universal-newline mode) against the string it had
   just written, so the comparison could never succeed for a note containing a
   CR and `move` refused **569 of 1858 backlog captures (30.6%)** with a false
   "copy verification failed". `_read_text` is documented READ-BACK ONLY and is
   now documented as never admissible for byte fidelity; `_verbatim_text` is
   its counterpart for verification. The fixture vault had no CR anywhere,
   which is why the suite was blind — it has two now.
7. **A failed verification ROLLS THE COPY BACK** —
   `_discard_unverified_copy` is a third sanctioned deleter in `fileops`
   (named in the structural never-delete guard). It is admissible because the
   destination came from `collision_free_path`, so it did not exist before
   `atomic_write` created it, and because it refuses to touch the source. The
   old branch left a fully-organized duplicate of a capture the caller had
   just been told was NOT filed, and every retry added another `_1`, `_2`, …
   copy. If the unlink itself fails, the ActionRecord and the error message
   both name the stray file. This is independent of the CR defect: it would
   corrupt the vault on any real verification failure.
8. **Moving a note into the folder it is already in is an `ok=True` NO-OP** —
   it silently renamed the note to `<name>_1.md` (the note was its own
   collision) and archived the original filename, divorcing the file from its
   `id:`/`aliases:` and breaking every `[[wikilink]]` to it (05 §3) while
   returning rc=0. It is one mis-click away in the picker. Nothing is written,
   nothing archived, no oplog line, no ActionRecord; `details.noop` says so and
   the CLI prints it.
9. **`.backups` (including anything under it) and the VAULT ROOT are refused
   as move destinations**, in the taxonomy+hint style of the existing
   "destination is the archive capture folder" refusal. Both are inside the
   vault but outside `vault.scan_dirs`, so the note left the capture folder,
   was archived as organized, and was then never indexed again — reported as a
   success. The list is deliberately these two only.
10. **`FrontmatterError` from a mutation names the FILE** — `_parse_named` is
    the one chokepoint (`_read_document` + `archive_capture`), mirroring what
    `frontmatter.load_file` already did for scans. "line 30, column 17" with no
    path is unactionable in a batch run over 1,858 captures against the 23 real
    files with unparseable YAML.

### Action corpus and learning (spec 12 §2 / 04 §33)

11. **`ActionContext.partial_failure` is a FIRST-CLASS field**, promoted out of
    `context.filters["partial_failure"]` for exactly the reason `dry_run` was
    (integrator ruling #3): `filters` is defined as the SESSION's search
    filters, so no reader could branch on it. `from_json` still reads the
    legacy spelling — those records are already on disk in Matt's corpus.
    Consequences: `learn.record_action` returns `None` for a partially-applied
    operation (spec 04 §33 learns on every **successful** move/merge; three
    failed moves had taught an association with `count: 3, success_rate: 1.0`,
    and every retry compounded it), and `ActionRecorder.stats` counts them in
    `total`/`by_operation` and in a new `partial_failures` field but keeps them
    out of `suggestions` entirely — they had been reported as rank-1 accepts
    with `top_accept_rate: 1.0`.

### Server (spec 10 §1, 09 §4)

12. **The accept-poll timeout is off the ACCEPTED socket.** `_ACCEPT_POLL_
    SECONDS` (0.05) is the accept loop's stop/idle cadence; applied to the
    client it made `sendall` raise `socket.timeout` as soon as the ~208 KB send
    buffer filled — i.e. whenever a client was >50 ms behind on draining — and
    the handler closed the connection MID-LINE with no error frame and no log
    entry. Every response larger than one send buffer (a real `search.query`
    returns 2.2–3.2 MB) was a coin flip for any client that renders while it
    reads. Clients are now blocking (`shutdown()` closes every connection, so
    nothing can hang forever), `_send_bytes` resumes a partial write instead of
    abandoning the line, and a genuine drop is logged at ERROR with the byte
    count.
13. **Post-write emits cannot fail an applied operation.** The
    `index-updated` stats snapshot is taken INSIDE `run()`, under the write
    lock; taking it after `submit()` returned read the record map with no lock
    held while the next queued writer mutated it, and the resulting
    `RuntimeError("dictionary changed size during iteration")` escaped dispatch
    — turning a move that had already written the destination, archived the
    original and appended its oplog line into a `-32603` for the client (a
    client that retries on error then files the note twice). Every post-write
    notification now goes through `_emit_best_effort`, and `VaultIndex.stats`
    iterates a snapshot as defence in depth.
14. **`_ReadWriteLock` is PHASE-FAIR, not strictly writer-preferring.** Readers
    waited while ANY writer was queued, which under a bulk file-away never
    cleared: `folder.list` p95 went from 10.9 ms idle to a 2,349 ms max, and
    `search.query` from 216 ms to 28,899 ms — the picker freezing for seconds
    while `folder.list`'s docstring promised "never queued" (that docstring is
    corrected). Writers keep priority for at most `_WRITER_BATCH_LIMIT` (8)
    consecutive acquisitions; a releasing writer then hands the readers queued
    at that moment an explicit pass, which is a HARD gate on new writers (a
    soft hint degrades to "eventually" — measured: a reader still waited behind
    12 writes). Read latency is bounded by 8 writes; writers cannot be
    postponed indefinitely, because passes are granted in bounded batches and
    are never renewed by newly arriving readers.

### CLI

15. **`index --full --stats` reindexes, then reports.** It silently ignored
    `--full` and printed the stats of the untouched index — `total: 0` in 0.09s
    on fresh state where `--full` alone indexes 13,362 notes. A silently wrong
    answer, which 09 §1.5 forbids. `--json` output stays parseable (the
    progress line is suppressed) and `--dry-run` composes.
16. **A non-`OrganizeError` exception is one attributable line, not a raw
    traceback.** A sweep of `move` over the real backlog emitted seven bare
    tracebacks, unattributable to any note. The last-resort handler names the
    subcommand and its subject and points at `--debug`, which remains the only
    way to see the stack (09 §1.5).
17. **`suggest --json` emits `rank`** — `move --suggestions-json` documents its
    input as `{path, score, rank, reasons}`, and a client building that shape
    by hand had no `rank` to copy.

### Frontmatter (visibility only)

18. **Duplicate frontmatter keys with genuinely different values are named.**
    YAML last-wins is correct and is what PyYAML does, so the parse is
    unchanged — but re-emitting the block drops the earlier value a human
    currently reads in the file. `style["duplicate_keys"]` records the keys
    whose occurrences PARSE differently (quoting-only differences are not
    reported) and `load_file` warns with the path and the keys.

### Declined, with reasons

- **Per-op index cost** (~1.2 s floor: every mutating CLI op rewrites the whole
  14 MB `index.json`; `suggest` spends ~726 ms of its ~861 ms deserializing it).
  Real and worth doing, but it is an index-persistence redesign (incremental
  patch/journal, or routing the CLI through a running `organize serve`), not a
  defect fix — it belongs in its own phase with its own perf gates.
- **An unreproduced traceback class in `move`** (7 of 100 captures in one sweep
  against a state the reporter could not reconstruct; five reproduction
  attempts came back clean). Ruling 16 converts any such crash into an
  attributable one-line error naming the note, which is what makes the next
  occurrence diagnosable; hunting it further without a repro is not a fix.

## Phase-3 rulings (architect, routed 2026-08-16 — integrator applies)

- **SUBPROCESS_ALLOWED (tests/test_repo_hygiene.py)**: expand to
  `frozenset({"llm", "consumers.deep_research", "consumers.taskwarrior",
  "consumers.learn"})`. The exemption is for spawning an EXTERNAL TOOL the
  spec names by name (task, yt-dlp, the research agent command) — never
  for filesystem work (no find/ls/cp/mv/rm equivalents; spec 05 §1.6 still
  binds). ADD (not either/or) structural assertions over every allowlisted
  module including llm: (a) no `shell=True` anywhere; (b) first arg to
  subprocess.run/Popen is never a plain string — list argv only (08 §A28
  injection class, made structural); (c) every subprocess.run carries
  `timeout=` (06 §6 / B1 law, made structural). A consumer that genuinely
  needs Popen requests a ruling.
- **consumers/base.py NotePayload bodies** (no signature change):
  `no_ai` MUST delegate to the frontmatter module's no-ai predicate — ONE
  rule in the codebase (extract frontmatter.is_no_ai's field-level check
  into a shared helper; both call it). Ambiguous truthy values (e.g. the
  string "true") count as no_ai=True — for a do-not-touch flag,
  over-matching is the safe error. `tags()` coerces via the shared
  frontmatter list-coercion (scalar → [scalar], list → list, missing/None
  → []), values stringified — never a hand-rolled re-parse (08 §B9 class).
  Tests: no_ai for true/absent/false/"true"; tags for scalar/list/missing
  (the fixture vault's scalar-tags quirk file is the natural input).

## Phase-3 rulings, addendum (routed 2026-08-16 — integrator applies with the batch above)

- **Templated alerter APPROVED (deviation from the brief)**:
  `organize-pipeline-failure@.service` with
  `OnFailure=organize-pipeline-failure@%n.service` — one alerter serves the
  timer run, the path-triggered run, and the failtest, and every alert
  names its failing unit. Record in deploy/README. The deep_research
  seat's drift-guard test (parsing every documented `organize …`
  invocation against the live build_parser) is the PATTERN for any future
  doc embedding CLI invocations.
- **RESERVED_CONFIG_LEAVES**: remove `integrate.review` and
  `vault.scan_dirs` — Phase 3 landed their readers (scan_dirs in
  runner/store; review in routes + three consumers), so the entries are
  stale by the gate's own rule.
- **De-dup obligation**: when the base.py `no_ai` property lands (ruling
  above), DELETE both interim fallbacks — `runner._note_is_no_ai` and
  `deep_research.payload_no_ai`.
- **Cutover note for deploy/README**: next to the install steps, state
  explicitly that the OLD para-automation/second-brain-automation chain
  must be masked/removed at cutover (spec 09 §5.5) — otherwise both
  generations run against the same vault.


## Integrator seam rulings — Phase 3 close (2026-08-16)

Five builder seats (store+migration, runner, taskwarrior, learn+qa,
deep_research+deploy) shipped green and raised 24 seams. Dispositions:

### Granted (implemented by the integrator)

- **The composition root opens AND migrates the store.** `cmd_run_consumers`
  does `with AutomationStore(paths.automations_db) as store: report =
  store.migrate()` before `run_consumers`, exactly as the runner's docstring
  requires. Regression-tested by a mutation: deleting the `migrate()` call
  makes the pipeline e2e fail loudly (every store method refuses an
  un-migrated v1 DB), which is the behaviour that was asked for.
- **`NotePayload.no_ai` / `NotePayload.tags()` implemented**, delegating to
  the frontmatter module. The field-level `no-ai` check was extracted to
  `frontmatter.fields_are_no_ai(fields)`; `is_no_ai(doc)` now calls it too.
  BOTH interim fallbacks deleted (`runner._note_is_no_ai`,
  `deep_research.payload_no_ai`), per the de-dup obligation.
  `tests/test_consumer_base.py` (45 cases) pins values AND agreement with
  the frontmatter module; a mutation that hand-rolls either one fails.
- **`RunContext.paths: CorePaths | None`** added and populated by
  `run_consumers(..., paths=...)`, which the CLI passes. taskwarrior's
  `<state>/backups/taskwarrior/<UTC-ts>` (06 §3.1) is now derivable without
  a consumer-side environment read.
- **`runner` uses `store.hash_note_text()`** instead of its own `hashlib`
  site. A mutation to a second, subtly different digest fails the suite —
  which is the point: it would orphan the 376 migrated success checkpoints
  and refire every consumer over the whole vault.
- **SUBPROCESS_ALLOWED expanded** to the four modules the spec names, PLUS
  the three structural assertions the ruling asked for (no `shell=True`, no
  string argv, every spawn carries `timeout=`, and text mode implies
  explicit `encoding=` + `errors=`). All four mutations are caught.
- **`vault.scan_dirs` removed from RESERVED_CONFIG_LEAVES.**
  `integrate.review` was NOT removed — it is a false positive of the gate,
  not a real reader. The reader heuristic now counts attribute accesses and
  bound names only, never bare string constants: both Phase-3 consumers
  emit the spec-mandated flashcard frontmatter value `"status": "review"`
  (06 §3.2/§3.3), which has nothing to do with `[integrate] review`. This is
  a deliberate refinement of the architect ruling, and it makes the gate
  strictly sharper: with the fix the allowlist is exactly the eight keys
  that genuinely have no reader.
- **`index.is_ignored` promoted to public**, and `runner` calls it for
  `vault.ignore_patterns` instead of carrying a second implementation.
  `runner._matches` lost its `component_match` flag and is now solely the
  06 §2 consumer prefix-or-glob rule, documented as deliberately different.
- **Store write-path fsync removed** (`PRAGMA synchronous = NORMAL` under
  WAL). The runner seat measured the default at 1.40 s per 1 000
  checkpoints — ~90 s pro-rata for the 7.5k vault against spec 09 §4's 30 s
  budget. NORMAL measures 0.06 s (23x). `migrate()` raises it to FULL for
  its own transaction, because a migration is a cutover artefact and not
  cheaply redone. The end-to-end ceiling in `test_runner_store.py` came down
  from 90 s to 10 s, and `test_store.py` asserts the pragma directly — a
  fast machine hid the regression from the wall-clock gate.
- **`--dry-run` accepted after the subcommand** as well as before, via
  `default=argparse.SUPPRESS` (a plain subparser default would silently
  overwrite the global flag).
- **Example config** now shows learn's `trigger_tags` / `triage_threshold` /
  `topic_tags` / `[consumers.learn.triage_weights]` and qa's
  `heuristic_detection = false` — "tunable with evidence" is the point of
  exposing them, and B9's default belongs where an operator reads it.
- **deploy/README step 4** now drives the migration through
  `organize migrate-store` (rehearse on a copy, then `--backup-first
  --yes-live`) with the measured real-data report line.

### Confirmed as-is (no change; the seat's reading was right)

- **`checkpoint()` refuses non-terminal statuses** and **paths must be
  absolute** — both confirmed. B3 must not come back through a second door,
  and a relative key would orphan history (06 §1 canonicalisation).
- **`soft_purge` archives into `purged_*` with `restore_purged()`.** The
  spec says hard-delete after 30 d; the seat brief says never hard-delete.
  Reconciled correctly: rows leave `notes`/`emissions` (so they stop
  participating in delivery decisions, which is what the spec is *for*) and
  are recoverable. Confirmed.
- **`filtered` deleted, `error`/`limit` kept but non-terminal.** 09 §5.4 /
  B4. Confirmed.
- **taskwarrior's one-task-per-note reading** over the brief's
  "one-per-action-item". 06 §3.1 says the description is the body joined to
  one line and the LLM's role is enrichment; the old production code agrees.
  Confirmed — a per-bullet variant would be a spec change.
- **taskwarrior's domain-specific tag normalization** (spaces→`_`,
  lowercase) rather than `frontmatter.normalize_tag`. The one-normalizer law
  is about matching vault tags to vault FOLDERS; using it here would rewrite
  `not_reviewed` and every underscored tag in Matt's task history.
  `NotePayload.tags()` therefore returns RAW spellings, which is now
  documented on the property and pinned by a test.
- **learn's B8 resolution** (the review file carries `source_hash`; the guard
  is `learn-processed` AND a review file for this source AND a matching
  hash). A bare `processing_status` guard cannot satisfy both "honor the
  guard" and "a rerun on an edited source regenerates", because the
  write-back itself changes the note hash. Confirmed; the e2e suite pins the
  consequence (run 2 settles to a skip, no duplicate review file).
- **learn's Whisper call is not a second LLM path.** Transcription is not
  completion, and `llm.py` has no upload surface. Same reasoning for the
  `yt-dlp` subprocess. Confirmed.
- **question_answer does not write `processing_status` back.** In
  `capture/raw_capture` that field is load-bearing — it is how the
  unorganized-capture query finds captures (03 §40). Confirmed; if a marker
  is ever wanted it must be a DISTINCT field.
- **The runner never checkpoints on a dry run** — it never calls `handle`
  at all, so taskwarrior's "dry run returns SUCCESS with the payload in
  metadata" can never be mistaken for work done. Confirmed and pinned:
  the e2e dry-run test asserts BOTH store tables stay empty, not just
  `emissions` (a `mark_seen`-only leak passed the weaker assertion).
- **The templated alerter** `organize-pipeline-failure@.service`. Already
  approved by the architect; recorded here as shipped.

### Deferred, with reason

- **A retry cap for `error`/`limit`.** Not implementable under the current
  contracts: a cross-run attempt counter needs persisted non-terminal
  emissions, which B3/B4 forbid, and `checkpoint()` refuses them by design.
  06 §1 as written (always retried, volume bounded by `max_notes_per_run`)
  is what ships. A real cap needs an `attempts` column owned by the store
  seat — a Phase-4 decision, not a Phase-3 patch.
- **A config key for the soft-purge retention window.** Spec 06 §2's schema
  names no such key and 06 §1 states 30 days as a value, not a knob.
  `DEFAULT_PURGE_RETENTION_DAYS = 30` stays the single source of truth until
  a spec change asks otherwise.
- **`deploy/` hardcodes `%h/.config/organize-core/config.toml`.** That is
  today's `CorePaths.config_file` default and nothing in Phase 3 moved it.
  The drift-guard test parses every documented `organize …` invocation
  against the live parser, so a future move fails a test rather than a
  cutover.

### Phase-3 measured results (integrator gate, 2026-08-16)

- `pytest tests/` — **1893 passed, 0 failed**, 1 deselected (slow), 70 s.
- `ruff check src tests` — clean.
- `make perf` — 1 passed: `full_reindex(10000 notes) = 2.02 s` (gate 5 s).
- End-to-end pipeline golden run (`tests/test_pipeline_e2e.py`, 11 tests):
  fixture vault + all four REAL consumers + a real SQLite store + fake
  `task`/Ollama/agent, driven twice through `cli.main`. Run 1 produces 3
  Taskwarrior imports (one per `todo` capture, payload asserted field by
  field), 1 learn review file + write-back + generation-log row, 1 answer
  note + its tier-2 card, 1 agent dispatch; run 2 changes nothing — no vault
  byte, no task, no LLM call, no agent run. Config-change retroactivity
  (widened `include_paths`, unchanged bytes on disk) processes the
  previously-filtered note.
- Live-DB migration through `organize migrate-store` against a writable copy
  of the read-only mirror: **v1→v2, notes 7516, success 376 preserved row
  for row, skip 3, retryable 8, filtered_dropped 22497, anomalies 0**,
  15.3 MB → 5.3 MB, 0.25 s wall. The counts reconcile exactly with
  pre-migration SQL; the backup is byte-identical to the original.
- Store write path: 1 000 checkpoints in **0.06 s** (was 1.40 s).
  1 000 notes × 2 consumers end-to-end through the real store: **0.47 s**.
- Anti-vacuity: 17 mutations injected, 17 caught — no-ai guard removed,
  filter misses persisted, exit-2 mapping, missing `migrate()`, dry run
  stamping `last_seen`, `needs_delivery` always true, hand-rolled `no_ai`,
  `tags()` sweeping every list field, a second `sha256` recipe, the store
  pragma reverted, migrate leaving `synchronous=FULL`, `shell=True`, a
  string argv, a dropped `timeout=`, a strict-UTF-8 spawn decode, a strict
  store `text_factory`, and a stale RESERVED_CONFIG_LEAVES entry.

## Phase-3 fix pass — spec deviations recorded (2026-08-16)

Two places where the shipped system deliberately does not match a literal
reading of doc 06. Both were already true in the code; they are recorded
here so the deviation is a decision rather than a discrepancy a reviewer
rediscovers.

### `[state] dir` / `[state] database` are NOT config keys (doc 06 §1/§2)

Doc 06 §2's schema shows a `[state]` table and §1 says "SQLite at
`<state.dir>/<state.database>`". `config.py` REJECTS both keys by name, with
a hint, because state locations are resolved by organize-core through
`CorePaths` (spec 10 §3: `~/.local/share/organize-core`, overridable with
`$ORGANIZE_CORE_STATE_DIR` or `--state-dir`). Two sources of truth for the
state directory is how a migrated database ends up somewhere the service
never opens.

The consequence to know: a config written verbatim from doc 06 §2 fails to
load. That is intended, and the error names the key and points at the
override. Doc 06 is the older document; spec 10 §3 wins.

### `shell.nix` (doc 06 §5) is retired, replaced by a health check

Doc 06 §5 asks for a `shell.nix` (python311 + pyyaml, taskwarrior, jq,
ollama, yt-dlp, curl; `PARA_ORGANIZE_ROOT`, `PYTHONPATH`) as parity with the
old `second-brain-automation.sh` wrapper. It is not shipped, and should not
be: that requirement exists only because the old pipeline was
`python -m scripts.automation.cli` run through `nix-shell`, and the deploy
seat's no-wrapper design — `ExecStart=%h/.local/bin/organize`, exit codes
propagating — was approved precisely to delete that layer. A `shell.nix`
would reintroduce the wrapper whose failure-swallowing is 08 §B's whole
subject, and pin a second interpreter next to the installed console script.

What the requirement was really protecting — that `task`, `yt-dlp` and the
research agent actually resolve under the systemd user manager's PATH, which
on NixOS is not the interactive shell's — is now covered by `organize health`
(`_toolchain_issues`): every external binary named by an ENABLED consumer is
resolved, and a miss is a WARNING naming the consumer, the option and the
fix (an absolute path in the config). It is a setup-time answer instead of a
per-note ERROR at the far end of a ten-minute timer.

## Phase-4 rulings (architect, routed 2026-08-16 — CORRECTS the workflow briefs)

- **CRITICAL — tag_router no-ai (corrects the seat brief's "mechanical
  move allowed" reading)**: `tag_router.should_process` returns False for
  no_ai captures — ALL modes, unconditionally. A doc-05 move WRITES the
  note's frontmatter (type tag + processing_status on the copy); spec 02's
  vault law forbids AUTOMATED tooling writing a no-ai note; the
  interactive exception is keyed to Matt's explicit keystrokes and an
  unattended consumer has none. Pin with: no-ai capture bearing a matching
  auto=true route → filtered, zero emissions, vault untouched.
- **routes.apply_route/apply_all are ACTOR-AWARE on no-ai TARGETS**:
  automated actors (consumer:*, auto-organize) refuse append into a no-ai
  target (an unattended append writes the target — same law), not only
  integrate. Actor "matt" (UI acceptance = explicit keystroke) may
  move/append with the no-ai field preserved. Integrate refuses for EVERY
  actor (12 §1).
- **auto_tags participate in routing BY CONSTRUCTION, knob-free**: 11 §2
  writes machine tags to both tags and auto_tags; routes consume tags. The
  consent gate for the machine chain is the per-route `auto = false`
  default; 12 §2's auto_tags_present records provenance in every record.
  No opt-out config key.
- **Self-removing skipif pattern (approved)**: goldens gated on a stubbed
  seam use a probe that skips ONLY while the seam raises
  NotImplementedError — they auto-activate when it lands; never a bare
  skip mark someone must remember to delete. Bonus property to state in
  tests: a stub translated to Status.ERROR retries next run (06 §1), so
  pre-landing notes process automatically on the first run after.

## Phase-4 rulings, auto_tagger batch (architect, routed 2026-08-16)

- **CORRECTION to the seat brief**: machine tags are written to BOTH
  `tags` AND `auto_tags` (spec 11 §2 bullet 3 + §4 acceptance). The
  auto_tags field is the provenance MIRROR; tags-only-in-auto_tags would
  make the tagger invisible to routes.resolve and break the
  participation-by-construction ruling above.
- **op_context seam (APPROVED — Phase-4 integrator lands it; shared
  files)**: base.py RunContext gains `op_context: OperationContext | None
  = None` (field, not factory; TYPE_CHECKING import fine; docstring:
  populated by run_consumers, None only in pure-logic unit tests,
  consumers MUST refuse unrecorded vault writes when None). runner.py:
  run_consumers accepts op_context and hands each consumer a per-consumer
  copy via dataclasses.replace(op_context, actor="consumer:<name>") — the
  doc 12 §2 actor format is "consumer:auto_tagger", never a bare name.
  Invariant to pin: op_context.dry_run == RunContext.dry_run, one flag
  wired once. cli.py: cmd_run_consumers passes its existing _op_context
  product through.
- **No-op_context fallback**: a consumer that would write the vault with
  op_context=None emits Status.ERROR rather than performing an unrecorded
  write (refusing to become a second unrecorded write path is the doc-12
  discipline; errors retry, so the run self-heals once the seam lands).
- **PHASE-5 CHECKLIST (logged now so it isn't lost)**: (a) learn
  consumer's SOURCE-note write-back (processing_status: learn-processed)
  is a meta_edit in the doc-12 enum and must migrate to
  update_frontmatter + op_context (new-output files — flashcards/answers —
  stay store-audited, defensible as-is); (b) taskwarrior.py's redundant
  `_note_is_no_ai` delegating helper simplifies to payload.no_ai; (c)
  tag_router integrate-mode routes override wants_llm when Phase 5 lands.

## Phase-4 rulings, routes-apply batch (architect, routed 2026-08-16)

- **fileops.move_to_destination gains keyword-only `archive: bool = True`**
  (integrator lands it): default preserves 05 §2 verbatim; routes pass
  False; apply_all archives exactly once after ALL destinations succeed
  (ruling #11 refined). archive=False also DEFERS the index capture-entry
  removal (05 §2 step 8) — apply_all does both after the final archive.
  Rationale: config-order execution is mandated by 11 §1, and multiple
  move-mode routes are spec-permitted (two copies + one archive).
- **Multi-target recording via a collecting facade** (approved, preferred
  over any record:bool knob — recording stays structurally unavoidable:
  redirectable, never suppressible): facade implements the recorder
  interface, never touches JSONL; per-target diffs are fileops' own;
  EXACTLY one aggregate record reaches the real recorder; on_record fires
  once with the aggregate. PIN the latent bug: learn.record_action must
  fold EVERY destination-role target of a multi-target record, not
  targets[0]. Mixed-mode operation name: strongest-mutation-wins
  (move > integrate > append). context.route for multi-route =
  comma-joined route names (schema-compatible; targets[] carries detail).
  routes→actions import edge approved (acyclic).
- **set_description on a folder with no index note CREATES
  `<folder>/index.md`** (description frontmatter + heading) through the
  ordinary recorded path — but ONLY for actor "matt" (explicit keystroke;
  11 §3 names index.md as primary storage). Automated actors refuse
  creation. The `[descriptions]` config table is READ-ONLY from the core
  (hand-maintained fallback; stdlib has no TOML writer and the dep budget
  bars adding one). OperationError only for actual write failures.

## Record correction (orchestrator, 2026-08-16)

Two Phase-3 fixer decisions shipped in code at 0f9e634 but were never
recorded here; they ARE binding precedent:
- **wants_llm instance predicate**: `Consumer.wants_llm()` (defaults to
  `uses_llm`) drives LLM CLIENT INJECTION only; the class-level `uses_llm`
  remains the runner's no-ai DENIAL flag. TaskwarriorConsumer returns
  `self.llm_enabled`. Runner wraps the predicate call in try/except
  falling back to the class flag.
- **Phase-boundary stubs rejected by config**: consumer types whose
  implementations are stubs for a FUTURE phase are rejected at config
  validation until implemented (was: auto_tagger/tag_router in Phase 3;
  Phase-4 integrator flips those two and re-pins the next boundary).

## Phase-4 rulings, bind + actor batch (architect, routed 2026-08-16)

- **Consumer.bind(ctx) hook APPROVED** (integrator lands base.py+runner.py
  together with op_context — one pass): routes must be visible to
  should_process so no-match stays a FILTER (re-evaluated every run;
  adding a [[routes]] entry applies retroactively to the backlog — the
  08 §B4 lesson). AMENDMENTS: (a) bind() raising ⇒ consumer SKIPPED for
  the run, counted as an error in the summary, exit 1 — never "continue
  unbound" (an unbound tag_router silently filters everything: the
  silent-outage class); other consumers unaffected. (b) cheapness
  contract in the docstring: bind grants CONFIG reads for should_process;
  should_process stays cheap (path + parsed frontmatter + config
  predicates — no I/O, no LLM). Interim (bind on the consumer's own
  class; unbound ⇒ inert False + one warning) approved until the hook
  lands.
- **Record-level actor for routes** (addendum to the facade contract):
  OperationContext actor stays "consumer:tag_router"; the AGGREGATE
  record's actor is "route:<name>" when exactly one route fired (doc 12
  §2's enum member for unattended route firing); multi-route aggregate
  falls back to ctx.actor with context.route = comma-joined names.
  ROUTES sets it (the facade builds the record).
- **LEARNING FOLDS ONLY MATT-DECIDED ACTIONS** (principle recorded for
  Phase 5's learn.record_action filter): a route firing is config, not a
  decision — folding it would make routes self-reinforcing and corrupt
  the accept-rate corpus. The routed-run learning-byte-identical trap
  test (with firing control) is permanent. Phase-5 nuances deferred:
  interactive route acceptance in the UI (actor matt) folds as a normal
  accept; integrate records use the verdict-based reading (verdict
  accepted/edited = Matt-decided even though actor is claude-integrate).

## Phase-4 ruling, tag_router seam B (architect, routed 2026-08-16)

- **NO EMISSION for a non-auto route match** (corrects the seat brief's
  "proposal emission" — spec 11 §1: "Non-auto routes only surface in the
  UI"). The should_process gate is: matches an auto=true route. A capture
  matching ONLY non-auto routes never reaches handle — it is a FILTER
  miss, re-evaluated every run. Consequences: flipping a route to
  auto=true fires on the very next run at the unchanged hash (nothing was
  checkpointed); the UI/`organize routes resolve` is the proposal
  surface, not the store. Required pin: capture matching only non-auto
  routes ⇒ filtered, ZERO emission rows; flip to auto=true ⇒ next run
  applies at the unchanged hash.
- **tag_router's self-built OperationContext is INTERIM ONLY**: when the
  RunContext.op_context seam lands, tag_router consumes the shared field
  and deletes its own construction (one construction site in the
  composition root; per-consumer actor via dataclasses.replace).
- **Process rule (orchestrator-adopted after four brief divergences)**:
  workflow briefs must QUOTE ARCHITECTURE.md rulings and spec lines
  VERBATIM with commit hash / doc-section cites; seats treat any uncited
  brief mandate touching persistence or the vault as requiring a ruling
  before implementation.

## Phase-4 close-out directives (architect, routed 2026-08-16)

- **Integrator's FIRST action, one pass**: land the two shared-file seams
  exactly as specified above (RunContext.op_context field + per-consumer
  dataclasses.replace in the runner; Consumer.bind() with skip-on-raise =
  error + exit 1) and retire the four Phase-4 stub-gate tests per their
  embedded instructions, citing the phase close. Both consumers are
  complete but INERT at runtime until the seams land (their
  ERROR/filtered fallbacks self-heal on landing).
- **Vacuous-pin class (verifier/fixer checklist, permanent)**: every
  refusal-predicate pin gets the mutate-the-guard-away check. Concretely
  for tag_router's no-ai pin: the fixture no-ai note's tag must MATCH an
  auto=true route (so the guard is the only thing refusing), plus a
  firing control (same note minus no-ai IS processed). A pin that passes
  by route-miss or by min_tags is worse than none. Mutation-audit before
  handback (auto_tagger: 24/24 caught) is the seat standard.
