# 06 — Python Automation Pipeline Specification

Unattended emitter/consumer daemon reacting to vault changes. **Status quo caveat:** the pipeline has been hard-down since 2026-05-10 — every 10-minute run crashes with a `UnicodeDecodeError` in the Taskwarrior consumer's constructor, and the wrapper swallows the non-zero exit so systemd shows success. The design below is the intended behavior with those defects (catalogued in `08-known-issues.md` §B) fixed.

## 1. Architecture (parity)

Pure-stdlib Python (PyYAML optional with a fallback parser). Three layers:

1. **Ingestion** — walk `vault.scan_dirs`, parse `*.md` frontmatter+body into immutable `NotePayload(path, frontmatter, content, raw_text, note_hash)`; `note_hash = sha256(raw_text)`. Skip legacy daily notes `\d{4}-\d{2}-\d{2}\.md` (documented behavior — keep, and document it in the README this time). Per-file parse errors are logged and skipped, never abort the run.
2. **Emitter + store** — SQLite state DB; a note is delivered to a consumer iff its hash differs from that consumer's last **terminal** emission for that path.
3. **Consumers** — registered by name (`@register`), instantiated from config sections, each with path filters + a `matches()` predicate + `handle() → ConsumerResult(status ∈ success|skip|error|limit, message, metadata)`.

### Orchestration rules (the corrected contract — B2/B3/B4/B12/B13 fixes)

- Consumer **constructors are pure**; external I/O (Taskwarrior export, tag list) happens lazily on first real work, per run.
- A failure in one consumer **never prevents others from running**; it's logged, counted, and exits nonzero at the end.
- **Checkpointing belongs to the orchestrator only.** Terminal statuses (`success`, `skip`) are checkpointed; `error` and `limit` are NOT (retried next run).
- **Filter misses are not persisted.** `should_process()` is cheap (path prefix + tag predicate on already-parsed frontmatter); re-evaluate every run so config changes (widened `include_paths`, new tags) apply retroactively. (This kills ~98% of today's 30k-row, 15 MB DB.)
- `max_notes_per_run` (per consumer, default 50) counts **successes only**.
- `purge_missing` becomes soft: mark unseen paths with a `last_seen` timestamp, hard-delete only after a retention window (30 d), and never purge when a configured scan dir is missing/empty (protects against a transient mount making the DB forget a year of LLM-run checkpoints).

### State store

SQLite at `<state.dir>/<state.database>` (default `~/.local/state/para-organize/automations.db`), WAL, busy_timeout 5000. Schema (add `last_seen` and a `schema_version` pragma/meta; otherwise keep — **the existing 15 MB DB must be migrated, not discarded**, or every past capture refires its consumers):

```sql
CREATE TABLE notes (path TEXT PRIMARY KEY, note_hash TEXT NOT NULL,
                    metadata_json TEXT, seen_at INTEGER NOT NULL, last_seen INTEGER);
CREATE TABLE emissions (consumer TEXT NOT NULL, note_path TEXT NOT NULL,
                        note_hash TEXT NOT NULL, emitted_at INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'success', metadata_json TEXT,
                        PRIMARY KEY (consumer, note_path));
CREATE INDEX idx_emissions_consumer_hash ON emissions (consumer, note_hash);
```

Migration for existing rows: keep `success`/`skip` rows as-is; delete `filtered` rows (they'll be re-evaluated cheaply); treat `limit`/`error` rows as retryable. **Paths in the DB are `.resolve()`d absolute paths** (`/home/matth/Obsidian/Main/...`); the rewrite must canonicalize identically or all history orphans.

## 2. Configuration

TOML at `--config` else `~/.config/para-organize/automations.toml` else defaults. **The live file at `~/.config/para-organize/automations.toml` is the operative config — the repo's `automations.toml` is a stale variant; the rewrite ships ONE example file and treats the live one as the deploy target.**

Schema:

```toml
[vault]
root = "~/Obsidian/Main"
scan_dirs = ["capture/raw_capture", "resources", "areas", "projects"]   # live value
ignore_patterns = [".obsidian", ".git", "resources/flashcards"]          # prefix or glob
[state]
dir = "~/.local/state/para-organize"
database = "automations.db"
[logging]
level = "INFO"
[consumers.<name>]
type = "<registered type>"    # required
enabled = true
include_paths = [...]         # prefix (relative to root) or glob
exclude_paths = [...]
max_notes_per_run = 50
# ...consumer-specific options
# [consumers.<name>.env]      # generic env passthrough (deep_research)
```

Paths expand `~`/`$VARS` and are **resolved** (symlink-collapsed). Live consumer settings to preserve as defaults-of-record: taskwarrior `marker_tag="todo"`, `default_project="Inbox"`, `additional_tags=["para","automation"]`, `review_tag="not_reviewed"`, `annotation_template="Captured from {id}"`, llm disabled with command `["ollama","run","gemma3:12b-it-qat"]`; learn enabled with `ollama_host="http://server.matthandzel.com:11434"`, `ollama_model="gemma3:12b-it-qat"`, `whisper_host="http://server.matthandzel.com:47770"`, flashcard paths under `resources/flashcards/`, `deck="Reading::Articles"`, `card_tags=["learn-consumer"]`, `max_notes_per_run=20`, `min_content_length=200`, `max_cards_per_note=12`; question_answer enabled; deep_research disabled, `max_notes_per_run=5`, command via `~/Projects/DeepResearchAgent`. Hardcoded host fallbacks in code should be replaced by config-required values with clear errors (the `gemma4:e4b` code default is a typo'd model that doesn't exist).

**New honored rule:** any note with frontmatter `no-ai: true` is excluded from all LLM-calling consumers (vault law, see 02).

## 3. Consumers (parity behavior)

### 3.1 `taskwarrior` — capture → Taskwarrior task

- **Trigger:** frontmatter tag equals `marker_tag` (`todo`), case-insensitive; scoped `include_paths=["capture/raw_capture"]`.
- **Description:** body minus headings, joined to one line, whitespace-collapsed, ≤512 chars (`[:509]+"..."`); fallback title → filename stem.
- **Tags:** normalize (strip, spaces→`_`, lowercase); drop `strip_tags` + marker; extract `project:<x>` prefix tag → task project (else `default_project`); add `additional_tags` + `review_tag`. With `remove_unknown_tags=true`, drop tags absent from `task _tags` — but whitelist `review_tag` AND `additional_tags` (⚠ B7) and log every dropped tag.
- **Dedupe** against full `task export` (incl. completed) on `(description, project)` and `(description, sorted-tags, project)`, case-insensitive.
- **Backup:** before the first import of a run, snapshot the Taskwarrior data dir to `<state>/backups/taskwarrior/<UTC-ts>` — with retention (keep newest N=10 / 30 days; today: 43 snapshots, 108 MB, unbounded ⚠ B6).
- **Import:** `task rc.data.location=<dir> rc.confirmation=no rc.hooks=off import <tmpjson>` with `TASKRC` env; payload `{description, entry, tags, project, annotations:[{entry, description=annotation_template}], next_action?, effort?, priority_estimate?, utility?}`; `entry` from note `timestamp`/`created_date` else now. Annotation template placeholders: `{path} {relative_path} {id} {capture_id}`. The four optional fields are UDAs — health-check that the `.taskrc` declares them before importing when enrichment is on.
- **All `task` subprocesses:** `timeout` set, decode with `errors="replace"` (⚠ B1 — the live 3-month outage).
- **LLM enrichment** (optional, `llm.enabled`, off in live config): prompt = preamble + Fibonacci `IMPORTANCE_GUIDE` (utility scale 1/2/3/5/8/13/21) + few-shot examples mined from Matt's own task export (effort/priority/utility exemplars) + task fields + frontmatter JSON + body excerpt (`max_note_body_chars`), demanding a single JSON object `{next_action, effort, priority, utility}`. Parse whole-string JSON else outermost `{…}`; effort strings normalized (`1.5h`, `30 min`, `PT1H30M`, bare hours); utility snapped to the scale. Any LLM failure degrades to no enrichment, never blocks the task.
- Writes nothing back to the note (parity).

### 3.2 `learn` — note → spaced-repetition flashcards

- **Trigger:** `.md`, ≥`min_content_length` chars, and (opt-in tag ∈ {`learn`,`remember`,`study`,`anki`} OR triage score ≥ `triage_threshold` 0.7). Scope: `resources|areas|projects` minus flashcards/readwise/prompts/templates/answers.
- **Triage score** = 0.4·novelty + 0.4·relevance + 0.2·quality with the heuristics as implemented (length/numbers/citations for quality; topic-tag overlap + non-self sources for relevance; novelty 0.5→0.7 heuristic). Parity — but expose the weights and topic-tag set in config, and log each candidate's score at DEBUG so the threshold is tunable with evidence.
- **Modality detection** (ordered): youtube-URL → `youtube`; frontmatter modalities audio → `audio`; `screenshot`; clipboard+>500 → `article`; leading `>`/`"` → `quote`; <200 → `thought`; >1000 → `article`; else `note`.
- **Normalization:** youtube → `yt-dlp` auto-subs (30 s timeout) → cleaned transcript; audio → first `media/*.wav|mp3` POSTed to Whisper `/v1/audio/transcriptions` (120 s).
- **LLM:** Ollama `/api/generate`, `format:"json"`, temp 0.3, `num_predict:2048`, 60 s. System prompt = the seven-tier card pedagogy spec (thesis / tier1_factual 40% / tier2_conceptual 25% / synthesis_connection 15% / synthesis_feynman 10% / synthesis_devils_advocate 10% / synthesis_drawing 5%), the curiosity-framing rule (never "What is/are…", use PARADOX/SURPRISE/COMPARISON/CHALLENGE) and the 10 quality rules; **self-improving**: append `## Learned Rules` from `resources/flashcards/card-generation-guidelines.md` when present. User prompt: title/attribution/tags/modality + `Generate 3-{max_cards} cards. Always include exactly 1 thesis card.` + content[:8000].
- **Output:** `resources/flashcards/review/<YYYY-MM-DD>-<slug>.md` (frontmatter `article, author, source_note, modality, generated, status: review, deck, cards_generated`; `## Card N [tier]` blocks; `<details>`-hidden answers for tier2/synthesis; `[DRAW THIS]` for drawing cards). Append a row to `generation-log.md` — create the file from a template if missing (⚠ today silently skips unless it exists).
- **Idempotency decision (resolves B8):** after a successful generation, write `processing_status: learn-processed` back to the source note via a real frontmatter writer (port the plugin's round-trip-safe rules to Python or share a spec), and honor the existing guard. Re-run on edit replaces the prior review file for that source (deterministic filename from source slug) instead of accumulating `-1,-2,…` duplicates (901 files today).

### 3.3 `question_answer` — question captures → answered notes

- **Trigger (tightened per B9 — the loose heuristic produced 874 answer files of mostly noise):** explicit tag `question`/`q` (case-insensitive, from **parsed frontmatter**, not the current hand-rolled re-parse that swallows aliases/sources as tags). The interrogative-heuristic auto-detection becomes opt-in config `heuristic_detection=false` (default off; keep the implementation and its rules — `?` in <500 chars, or leading interrogative word — for when it's enabled).
- Extract up to `max_questions` (5) questions (lines ending `?` or starting with an interrogative; else whole body).
- Per question: Ollama `/api/generate`, temp 0.3, `num_predict:1024`, 90 s, research-assistant system prompt ("answer thoroughly but concisely… say so explicitly rather than guessing").
- **Output per question:** `resources/answers/<date>-<slug>.md` (frontmatter `id, tags:[ai-generated, question-answer], created_date, last_edited_date, source_capture`; body = question H1, answer, verify-independently disclaimer, `[[wikilink]]` to source) + a tier-2 flashcard in `resources/flashcards/review/` with `deck: Reading::Questions`.

### 3.4 `deep_research` — new relationship note → research subprocess

Generic dispatcher, no content matching; routing purely by `include_paths` (`areas/relationships`). Command template with `{path} {path_quoted} {notes_dir} {notes_dir_quoted}` placeholders (append path if none present); `working_directory`, `timeout_seconds` (600), `env` table + `NOTES_DIR` injection (⚠ fix the case-mismatched guard so a caller-supplied `NOTES_DIR` wins; format literal `{`/`}` safely). Exit 0 → checkpoint (never re-dispatch until the note changes); nonzero/timeout → `error`, retried. This exit-code-as-checkpoint contract is the explicitly stated requirement in `person-research-agent.md` — preserve it exactly.

## 4. CLI

`python -m scripts.automation.cli [--config PATH] [--consumer NAME]… [--list-consumers] [--log-level L]`. `--consumer` matches config section names case-insensitively (unknown → exit 2). `--list-consumers` must not construct consumers (B2). Summary line per consumer: `Consumer X: success=N skip=N limit=N error=N filtered=N`. Exit 0 clean; 1 any consumer error; 2 usage.

Also keep `scripts/capture_query.py` as-is in role: a standalone read-only query CLI over captures (same frontmatter schema; strict line-based delimiter scan and timestamp-as-string YAML loader — **port those two behaviors into the shared frontmatter module** and have both entry points use it).

## 5. Deployment (systemd, user units)

Replace today's two-generation tangle (repo units are dead — `para-automation.service` ExecStarts a nonexistent script; live units run a vault-side shim that swallows failures) with ONE set:

- `para-automation.service` (oneshot): runs the pipeline via the nix-shell wrapper (`scripts/systemd/second-brain-automation.sh` pattern: `set -euo pipefail`, default `OLLAMA_HOST`, `nix-shell shell.nix --run 'python -m scripts.automation.cli'`). **Exit codes propagate**; `OnFailure=` a notify unit (ntfy) so a 3-month silent outage cannot recur.
- `para-automation.timer`: `OnBootSec=5m`, `OnUnitActiveSec=10m`, `Persistent=true`.
- `para-automation.path`: `PathChanged=%h/notes/capture/raw_capture`, `PathChanged=%h/notes/resources`, `MakeDirectory=true`.
- The Beeper-sync step currently piggybacking in the vault-side shim is out of scope — leave it as its own unit; do not couple it to this pipeline.

`shell.nix` (parity): python311 + pyyaml, taskwarrior, jq, ollama, yt-dlp, curl; sets `PARA_ORGANIZE_ROOT`, `PYTHONPATH`. Pin one interpreter version (there are 3.11 and 3.12 pycache artifacts today).

## 6. Observability

- Structured per-run summary (counts per consumer + duration) to stdout/journal.
- WARN on every dropped tag, skipped file, LLM failure, purge candidate.
- All subprocess/LLM calls: explicit timeouts; catch `OSError`+`TimeoutError`+`json.JSONDecodeError` (⚠ B11 — remote-Ollama timeouts are the expected failure, currently uncaught); bounded retry with backoff for the HTTP calls.
- All file/subprocess text I/O: explicit `encoding="utf-8", errors="replace"`.

## 7. Acceptance tests

- Golden run over a fixture vault: `todo` capture → exactly one Taskwarrior import payload (snapshot-tested JSON); rerun → zero (hash checkpoint); edit note → re-emitted.
- One consumer raising in construction/handle does not stop the others; run exits 1; summary counts correct.
- `limit`/`error` results re-emit next run; `success`/`skip` don't.
- Widening `include_paths` in config causes previously-filtered old notes to process (B4 regression).
- Removing a scan dir from config does not delete its emission history (B5 regression).
- `task export` returning invalid UTF-8 does not crash (B1 regression).
- `no-ai: true` note is never sent to any LLM.
- Learn rerun on an edited source replaces (not duplicates) its review file and the source gains `processing_status: learn-processed`.
- DB migration test: run against a copy of the live 15 MB `automations.db`; no historical `success` checkpoint is lost.
