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
- **Atomic-write triplication (architect conformance pass, LOW)**: three
  implementations exist — `fileops.atomic_write` (vault files: perm/EXDEV
  handling), `learn._atomic_write_text` and index's inline mkstemp+replace
  (state files). The vault-vs-state split is INTENTIONAL for now (merging
  would give the pure scoring module an import of the mutation layer —
  structural decision 3). Disposition: when Phase 3 adds consumer state
  writers, lift ONE state-file atomic-write helper into a small shared
  module (not fileops) and migrate learn/index to it.
- **Phase gates run `make gate`** (test + perf + lint): the spec 09 §4
  full-scale perf tests are `-m slow` and excluded from the default suite,
  so a bare `make test` is NOT a complete phase gate.
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
- **suggest** — `CaptureFeaturesView.modalities`.
- **learn** — `CaptureLike` protocol (widened parameter annotations only).
- **actions** — `ActionContext.dry_run`; `include_dry_run=` on `query`,
  `stats` and `export`; `ActionSchemaError`; `OPERATIONS` / `EDIT_MODES` /
  `VERDICTS` / `TARGET_ROLES`; `ActionRecorder.month_files()`.
- **fileops** — `OperationContext.clock`; `LoggedOperation.backup` and
  `.dry_run`; `update_frontmatter(..., replace_keys=)`.
  (`DRY_RUN_FILTER_KEY` was removed — superseded by `ActionContext.dry_run`.)
- **routes** — `RouteMatch.para_type`, `ROUTE_SUGGESTION_SCORE`,
  `ARCHIVE_SUGGESTION_TYPE`.
- **session** — `default_filters()`, `TERMINAL_OUTCOMES`.
- **server** — `MUTATING_METHODS`, `INDEX_CHANGING_METHODS`,
  `EVENT_INDEX_UPDATED` / `EVENT_OP_PROGRESS` / `EVENT_TYPES` /
  `EVENT_METHOD`, `NOT_IMPLEMENTED = -32001`, `RpcException`,
  `encode_event()`, `handshake_line()`, `OrganizeServer.emit()` / `ready` /
  `connection_count` / `stopping`.

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
