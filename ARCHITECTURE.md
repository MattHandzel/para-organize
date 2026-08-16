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
frontmatter ◀── index, fileops, consumers/*
actions ◀── fileops ◀── routes, session-layer callers
llm ◀── consumers/*, (Phase-5 integrate)
store, runner ◀── cli.run-consumers
everything ◀── cli, server (composition roots)
```

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
| **runner** | `consumers/runner.py` | `tests/test_runner*.py` | 06 §1/§4/§6/§7, 08 §B2-B5/§B12-B13 | store, base, config, frontmatter |
| **taskwarrior** | `consumers/taskwarrior.py` | `tests/test_consumer_taskwarrior*.py` (fake `task`) | 06 §3.1, 08 §B1/§B6/§B7 | base, llm |
| **learn-consumer** | `consumers/learn.py` | `tests/test_consumer_learn*.py` | 06 §3.2, 08 §B8 | base, llm, frontmatter |
| **question_answer** | `consumers/question_answer.py` | `tests/test_consumer_qa*.py` | 06 §3.3, 08 §B9 | base, llm, frontmatter |
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
only, dry_run) -> RunSummary` (+ `ConsumerSummary`).

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
- **`--json` flag**: the per-subcommand `--json` (machine-readable output)
  added by the cli+server seat is an approved ADDITIVE surface change —
  spec 10 §2 makes other frontends (Claude agents) first-class CLI
  consumers, which needs machine-readable output. It must never change
  human-output defaults, and `SUBCOMMANDS`/`build_parser` remain the
  scaffold contract.
