# Capture Automation Architecture

This document outlines the architecture for para-organize automation pipelines that react to raw capture notes and fan out updates to downstream tools like Taskwarrior. The goal is to keep the system modular, fault-tolerant, and easy to extend with new automations.

## Overview

The system comprises three layers:

1. **Ingestion** – Scans capture notes, parses YAML frontmatter, and normalizes note content. It produces immutable `NotePayload` objects that include the path, metadata, and a stable hash of the entire note.
2. **Emitter** – Maintains a lightweight SQLite store that remembers the last hash processed for each `(note, consumer)` pair. When a hash changes (or a new note appears), the emitter generates events only for consumers that opt in to the note via tag/context filters.
3. **Consumers** – Small, single-responsibility modules that implement a `Consumer` interface. Each consumer receives new note payloads and decides whether to mutate an external system. Consumers are free to do additional dedupe/validation before committing changes.

```text
+------------------+       +--------------------+       +-------------------+
|  Capture Notes   |       |    Event Emitter   |       |    Consumers      |
| ~/notes/.../*.md | ----> | sqlite-backed diff | ----> | taskwarrior, etc. |
+------------------+       +--------------------+       +-------------------+
           ^                          |                               |
           |                          v                               |
           +------------------- state database ------------------------+
```

## Key Modules

| Module | Responsibility |
| ------ | -------------- |
| `scripts.automation.config` | Load defaults and user overrides (TOML) for vault paths, state directories, and consumer-specific settings. |
| `scripts.automation.notes` | Parses Markdown + frontmatter into `NotePayload` dataclasses (using PyYAML when available, with a built-in fallback) and computes content hashes while skipping legacy daily files (`YYYY-mm-dd.md`). |
| `scripts.automation.store` | Provides `AutomationStore`, a thin layer over SQLite for persisting note hashes and consumer emission checkpoints. |
| `scripts.automation.emitter` | Encapsulates diffing logic. Produces a stream of `(note, is_new)` events for each registered consumer without double-emitting unchanged notes. |
| `scripts.automation.consumers.base` | Defines the `Consumer` protocol and reusable helpers for tag filtering, logging, and error handling. |
| `scripts.automation.consumers.taskwarrior` | Adds Taskwarrior-specific behaviour: state backups, duplicate detection, tag reconciliation, and CLI integration. |
| `scripts.automation.cli` | Entry point invoked by systemd timers or manual runs. Bootstraps config, opens the store, wires the emitter to all configured consumers, and reports summary statistics. |

All modules are deliberately framework-free; PyYAML is optional, and the fallback parser keeps deployments lightweight when the dependency is unavailable.

## Data Model

SQLite schema maintained by `AutomationStore`:

- `notes(path TEXT PRIMARY KEY, note_hash TEXT, seen_at INTEGER)` – last-seen hash for each capture note.
- `emissions(consumer TEXT, note_path TEXT, note_hash TEXT, emitted_at INTEGER, PRIMARY KEY (consumer, note_path))` – tracks which consumer has handled a specific hash.
- `metadata(key TEXT PRIMARY KEY, value TEXT)` – room for future state (schema versioning, counters).

The store also exposes `with_transaction()` to guarantee atomic updates when consumers commit. If a consumer raises an exception, the emitter leaves its emission checkpoint untouched, allowing a future retry.

## Event Flow

1. The CLI loads config, ensures the state directory exists, and opens the SQLite database.
2. `NoteEmitter.sync()` enumerates capture files (default `~/notes/capture/raw_capture`), constructs `NotePayload` objects, and inserts/updates the `notes` table.
3. For each registered consumer:
   - `Consumer.matches(note)` decides whether the note is relevant (e.g., tag `todo`).
   - The emitter compares `note_hash` with the consumer's last emitted hash. If different, the note is yielded to `Consumer.handle(note, store)`.
4. Consumers perform idempotent work (Taskwarrior dedupe, file append, etc). If successful, they call `store.mark_emitted(...)`. If they skip or fail, the emitter records the status for logging but will retry on the next run until success.

## Extensibility

- **Configuration** – Consumers are registered via the TOML config file (default `~/.config/para-organize/automations.toml`). Each section maps to a concrete consumer module and exposes consumer-specific options, letting users add new automations without touching core code.
- **Consumers** – Implementations subclass `Consumer` and register themselves in `scripts.automation.registry`. They can declare required tags, maintain their own per-note metadata, and leverage shared helpers (e.g., `extract_project_tag()`).
- **Testing** – Consumers return structured `Result` objects (success/skip/failure) so unit tests can assert precise outcomes without interacting with external systems.

## Reliability Considerations

- **Hashing** – SHA256 over the full raw note text ensures any body or frontmatter change triggers a new emission.
- **Backups** – Taskwarrior consumer performs timestamped backups of `~/.task` before mutating data and stores them under `~/.local/state/para-organize/backups/taskwarrior/`.
- **Idempotency** – Duplicate detection occurs at two layers: the emitter will not double-send the same note hash, and consumers verify their downstream state (Taskwarrior export) before creating records.
- **Observability** – The CLI logs structured summaries (counts per consumer, failures) to STDOUT and optional log files, making it safe for systemd timers.

## Future Automations

To add a new automation (e.g., append productivity feedback notes to `~/notes/areas/systems/productivity-system-feedback.md`):

1. Create `scripts/automation/consumers/productivity_feedback.py` implementing the `Consumer` interface and writing to the aggregation file while avoiding duplicate lines (e.g., by hashing entries).
2. Add a `[consumers.productivity_feedback]` section in the TOML config with the desired tag filter (`tag = "productivity-system"`).
3. The existing CLI will discover the consumer via the registry and handle the rest (hash diffing, retries, logging).

This layered design keeps cross-cutting concerns (diffing, persistence, logging) centralised while letting individual automations stay small, deterministic, and testable.

## Systemd Automation

Sample units are provided under `scripts/systemd/`:

- `para-automation.service` runs `python -m scripts.automation.cli` from the repository root.
- `para-automation.timer` schedules the service hourly.
- `para-automation.path` watches `~/notes/capture/raw_capture` for changes and triggers the service immediately when files land.

To install them:

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd/para-automation.{service,timer,path} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now para-automation.timer
systemctl --user enable --now para-automation.path
```

The timer handles periodic execution, while the path unit ensures low-latency reactions to new captures. Both trigger the same service, so downstream consumers continue to dedupe work safely.
