# Spec — para-organize rebuild

A complete specification for rebuilding the KMS *organize* stage **from scratch**: exact feature parity with the original (minus its bugs), re-architected as a core engine + thin Neovim client, plus four new capabilities. Written 2026-08-15 from a deep audit of the code (git history and HEAD), the live deployment (systemd units, state databases, Matt's real configs), and the real vault data.

**Read in order.** Docs 01–09 define the parity baseline; docs 10–13 are the new directives and override earlier docs where they conflict (only 10 does, and says exactly where).

| Doc | Contents |
|---|---|
| [01-purpose-and-user.md](01-purpose-and-user.md) | What the software is, who uses it and how, why the rewrite, success criteria |
| [02-system-context.md](02-system-context.md) | The vault, the capture-note frontmatter schema (the central contract), every external integration, live state inventory |
| [03-plugin-functional-spec.md](03-plugin-functional-spec.md) | UI behavior: commands, filters, two-pane UI, keymaps, merge, indexer, YAML round-tripping, config surface |
| [04-suggestions-and-learning.md](04-suggestions-and-learning.md) | Scoring signals & exact weights, learning record format, decay, acceptance tests |
| [05-file-operations.md](05-file-operations.md) | Safety invariants: copy-then-archive, atomic writes, backups, operation log, merge semantics |
| [06-automation-pipeline.md](06-automation-pipeline.md) | Emitter/consumer pipeline: store schema & migration, all four consumers, config, systemd, observability |
| [07-metadata-editing.md](07-metadata-editing.md) | Configurable metadata fields editable during organizing (tags, importance/"worth remembering", …) |
| [08-known-issues.md](08-known-issues.md) | Full defect catalog (60+ items) of the original — what not to reproduce; parity is measured against *intended* behavior |
| [09-rebuild-guidance.md](09-rebuild-guidance.md) | Testing strategy, performance targets, migration/cutover, definition of done |
| [10-architecture-core-and-clients.md](10-architecture-core-and-clients.md) | **NEW ARCHITECTURE**: `organize-core` (CLI + server) owns all behavior; Neovim is a thin client; API contract; state layout |
| [11-tag-routing-and-auto-tagging.md](11-tag-routing-and-auto-tagging.md) | Standard destinations for tags, natural-language folder/file descriptions, auto-tagging consumer |
| [12-edit-modes-and-action-recording.md](12-edit-modes-and-action-recording.md) | manual/append/integrate edit modes (Claude edits in Matt's wording + target style); total state/diff recording of every action as a learning corpus |
| [13-automatic-organize.md](13-automatic-organize.md) | FUTURE: highlight/write text in nvim → Claude files it to the correct place(s); trust ladder; built on 10–12 |

**Provenance facts an implementer must know before reading any old code** (full detail in 01/08):

1. The original plugin's **HEAD never worked** (refactor left stubs). Behavioral reference: `git show d753672~1:<path>` for `lua/para-organize/{indexer,suggest,learn,move,utils}.lua` — the old code was deleted on this branch but is fully available from git history, and the `main` branch/worktree still has the complete original tree.
2. The Python pipeline crashed on every scheduled run from 2026-05-10 (masked by a wrapper swallowing exit codes).
3. Live configs ≠ repo configs: the operative pipeline config is `~/.config/para-organize/automations.toml`; Matt's plugin setup is in `~/.config/nvim/lua/plugins/init.lua` and contains two misconfigurations (08 §C).

**Suggested build order** (each phase independently shippable): ① core engine: index + frontmatter + file ops + suggestions/learning, CLI only, acceptance suites 04/05 green → ② nvim thin client, parity with 03/07 → ③ consumers migrated into the core (06) + systemd cutover (09 §5) → ④ routes + auto-tagging (11) → ⑤ edit modes + action recording (12; recording ideally lands with ①–② so the corpus starts growing early — at minimum the ActionRecord write path ships in ①) → ⑥ automatic organize (13).
