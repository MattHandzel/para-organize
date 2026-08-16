# 02 — System Context and Integration Contracts

Everything in this file was verified against the live system on 2026-08-15. These are the external contracts the rewrite MUST honor — other software reads/writes these exact paths and formats.

## The vault

- Canonical location: `/home/matth/Obsidian/Main`, with a convenience symlink `~/notes -> Obsidian/Main`. Tooling variously refers to either; both must work (resolve symlinks before comparing paths).
- It is a live Obsidian vault, synced with Syncthing — expect `*.sync-conflict-*` duplicate files, an `.obsidian/` directory to ignore, and concurrent external modification of any file at any time.
- Relevant layout:

```
~/Obsidian/Main/
├── capture/                  # quick captures (meetings, drafts) — humans drop files here too
│   └── raw_capture/          # automated ingestion target (~2,300 files: .md, .wav, .txt, .pdf, .epub …)
│       └── media/            # media attachments saved by the capture app
├── projects/                 # PARA: active projects (subfolders per project)
├── areas/                    # PARA: ongoing responsibilities
├── resources/                # PARA: reference topics
├── archive/                  # PARA: inactive items — NOTE: SINGULAR on disk
│   └── capture/raw_capture/  # where organized/processed originals are archived
├── dailies/                  # daily notes (excluded from organize processing)
├── anki/, gtd/, life-logging/, events/, zettelkasten/, …   # other subsystems, not organize's concern
└── kms-system-rules.md       # vault-level rules (see below)
```

- **`archive/` vs `archives/`**: the plugin's default config names the folder `archives`; the real vault uses `archive`. The rewrite must treat the archive folder name as config (as today) but FAIL LOUDLY in `:checkhealth`/startup when a configured PARA folder doesn't exist, and Matt's config must be corrected to `archive`. Do not silently create a second archive tree.
- **Vault-level rule (from `kms-system-rules.md`):** any note whose frontmatter contains `no-ai: true` must NEVER be written to by automated/AI tooling. The Python pipeline's LLM consumers must honor this. (The interactive Neovim plugin acts on Matt's explicit keystrokes and may move such files, but must preserve the field.)

## Capture note frontmatter schema (the shared contract)

Produced by the capture app (parent repo: React/Vite web app + FastAPI server, and an Electron wrapper; also older scripts). Two generations exist in the wild and BOTH must parse:

**Current format (capture app, 2026):**

```yaml
---
timestamp: '2026-06-10T21:37:42.809743+00:00'   # ISO8601 UTC, quoted
id: '2026-06-10T21:33:05.379Z'                  # capture id == filename stem
aliases:
- '2026-06-10T21:33:05.379Z'
capture_id: '2026-06-10T21:33:05.379Z'
modalities:            # subset of: text, clipboard, screenshot, audio, files
- text
context: []            # list OR string — both occur
sources:               # e.g. me, clipboard, web page, email
- me
tags:                  # kebab-case, AI-suggested at capture time (Ollama) + manual
- impro
- creativity
location:              # optional; from browser geolocation
  latitude: 40.7126
  longitude: -74.0066
  city: New York
  country: United States
  timezone: America/New_York
metadata: {}           # free-form map — sometimes [] (empty list) in older notes!
processing_status: raw # raw → (organized when processed)
created_date: '2026-06-10'
last_edited_date: '2026-06-10'
---
## Content
<body text>
```

**Older/manual notes** may have any subset of these fields, different quoting, `tags: daily_notes` (scalar, not list), `title:` fields, or **no frontmatter at all**. Every field is optional; parsers must never crash on real-world files. Notable real-world quirks that MUST be tolerated:

- `metadata: {}` vs `metadata: []` vs missing.
- `context` as string or list.
- Scalar vs list for `tags`/`sources`/`aliases`.
- Filenames containing `:` and `+` (ISO timestamps as filenames, e.g. `2026-04-08T16:51:24.690160+00:00.md`), spaces, `$`, unicode quotes.
- Non-markdown files (`.wav`, `.txt`, `.pdf`, `.epub`) interleaved in `raw_capture/` — organize tooling processes only `*.md` but must not choke listing the directory.
- Daily-note files named `YYYY-MM-DD.md` live in `raw_capture/` historically; the automation pipeline explicitly SKIPS them (legacy exclusion) and the plugin sessions typically filter them out via `processing_status`/pattern rules.

## Integration inventory

| System | Direction | Contract |
|---|---|---|
| **Capture app** (KMS parent repo) | upstream | writes `{vault}/capture/raw_capture/*.md` in the schema above; also `media/` attachments. Organize never modifies the capture app. |
| **Obsidian** | bidirectional | vault is open in Obsidian concurrently; wiki-links `[[note-id]]` must keep resolving after moves (Obsidian resolves by filename/alias vault-wide, so moving a file preserves links as long as filename is unchanged — do not rename on move). `.obsidian/` ignored. |
| **Neovim plugin** (this repo) | interactive | full spec in `03`–`05`. State in `~/.local/share/nvim/para-organize/` (`index.json`, `learning.json`, `operations.log`); logs in `~/.cache/nvim/para-organize.log`. |
| **Python automation** (this repo) | unattended | spec in `06`. Config `~/.config/para-organize/automations.toml`; state `~/.local/state/para-organize/` (`automations.db` SQLite, `backups/`). Runs via systemd user units (`para-automation.service/.timer/.path`, plus `second-brain-automation.sh`). |
| **Taskwarrior** | downstream | `todo`-tagged captures become `task add …` items; annotations carry `[[wiki-links]]` back to the note. Backups of `~/.task` taken before mutation into `~/.local/state/para-organize/backups/taskwarrior/`. |
| **Ollama LLM** | downstream (automation) | remote server `http://server.matthandzel.com:11434`; models of the gemma/qwen family (capture app default `gemma3:4b-it-qat` era; config-driven). Used by question_answer / learn / deep_research consumers. Must be treated as optional & flaky: timeouts, empty responses, server down. |
| **Claude Code agents** | consumers of the vault | Matt's agent fleet reads/writes the vault under rules in the vault `CLAUDE.md` (PARA conventions, `no-ai` rule, captures triaged into projects/areas/resources during review). The organize system is how "review" happens. |

## Live state observed (for parity testing & migration)

- `~/.local/share/nvim/para-organize/index.json` — 2.9 MB flat JSON map `{abs_path: entry}`; entry fields: `filename, folder, aliases, sources, size, id, modified (epoch), para_type ("project"|"area"|"resource"|"archive"|"other"), tags, path, title, metadata, normalized_tags, indexed_at, modalities`. Contains stale paths under the wrong `…/Main/notes/capture` root — evidence the current code neither validates roots nor prunes dead entries. The rewrite may change the internal index format (it is not an external contract; regenerate on first run) but should provide `:ParaOrganize reindex` migration.
- `~/.local/share/nvim/para-organize/learning.json` — `{"patterns":[],"associations":[],"statistics":{"last_updated":…,"total_moves":0,"destinations":[]}}`. Effectively virgin; no migration burden, but the format is documented in `04`.
- `~/.local/state/para-organize/automations.db` — 15 MB SQLite; schema in `06`. This IS a real contract (a year of emission history provides idempotency); the rewrite must either keep the schema or migrate it, or every past capture will re-fire its consumers (duplicate Taskwarrior tasks, duplicate LLM runs).
- Matt's live plugin config is embedded in `~/.config/nvim/lua/plugins/init.lua` (plugin `MattHandzel/para-organize`, `event = "VeryLazy"`, full setup table ≈ the README defaults with `vault_dir = "~/Obsidian/Main/notes"` — which is WRONG, see `08`).
