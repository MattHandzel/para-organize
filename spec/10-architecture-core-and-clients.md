# 10 — Architecture: Core Engine + Thin Clients (SUPERSEDES the component split in 01/03/06 where they conflict)

Directive from Matt (2026-08-15): *"it should be a separate program that does the base organizing (a CLI, server, whatever) and then Neovim will just be the interface."*

This changes the **architecture**, not the **behavior**. Docs 03–07 remain the binding behavioral contracts; this doc reassigns where each behavior lives.

## 1. Components

```
┌─────────────────────────────────────────────────────────────┐
│  organize-core  (one program: CLI + long-running server)    │
│  • vault index + query          • suggestions + learning    │
│  • all file operations          • frontmatter round-trip    │
│  • operation log                • action recording (12)     │
│  • tag routing + auto-tag (11)  • automation consumers (06) │
│  • LLM client (one, shared)     • automatic organize (13)   │
└────────────┬───────────────────────────────┬────────────────┘
      JSON-RPC over unix socket        CLI subcommands
             │                               │
   ┌─────────┴─────────┐        ┌────────────┴───────────┐
   │ para-organize.nvim │        │ systemd timer/path;    │
   │ (thin UI client)   │        │ Claude agents; scripts │
   └────────────────────┘        └────────────────────────┘
```

**organize-core** — implement in Python 3.11+ (reuses the pipeline domain logic and LLM tooling; stdlib + PyYAML, no heavy frameworks; packaged via the repo's nix flake). One codebase, two entry modes:

- `organize <subcommand>` — batch CLI. Every capability is scriptable: `index`, `search`, `suggest <note>`, `move <note> <dest>`, `merge <note> <target>`, `archive <note>`, `set-meta <note> k=v`, `session list [filters]`, `routes …` (11), `record …`/`actions export` (12), `auto-organize` (13), `run-consumers [--consumer X] [--dry-run]` (06), `health`, `serve`.
- `organize serve` — long-running server on a unix socket (`$XDG_RUNTIME_DIR/organize-core.sock`), JSON-RPC 2.0, newline-delimited. Holds the warm index in memory so interactive calls meet the latency targets (suggest < 100 ms). Auto-spawned by the Neovim client if not running; idles out after configurable inactivity. Single-instance lock; concurrent clients allowed; all mutating operations serialized through one writer queue.

**para-organize.nvim** — pure interface. Renders the two panes, keymaps, pickers; every state change is an RPC call; it never touches vault files itself. All commands/keymaps/UX exactly as doc 03; `:checkhealth` additionally verifies core reachability/version handshake. The plugin degrades gracefully (clear error, no crash) when the core is missing.

**Automation** (doc 06) becomes `organize run-consumers` inside the same program — systemd units invoke the CLI; no separate Python package. Emitter/store/consumer semantics unchanged.

## 2. API contract (the load-bearing boundary)

RPC methods mirror doc 03's operations 1:1 — `session.start(filters) → {captures[]}`, `note.get`, `suggest.for_note`, `op.move`, `op.merge_preview`/`op.merge_commit`, `op.archive`, `meta.set`, `folder.create`, `index.reindex`, `search.query`, `routes.resolve`, `auto.propose`/`auto.apply` (13), plus `events.subscribe` (index updates, long-op progress). Requests/responses are versioned (`{"apiVersion": 1}`); the nvim client refuses a major-version mismatch with a clear message.

Two consequences the implementer must honor:

1. **Everything testable without Neovim.** The full acceptance suites of docs 04–07 run against the CLI/RPC alone; the nvim client's own tests only cover rendering/keymap dispatch against a mock core.
2. **Other frontends are first-class.** Claude agents drive the same CLI the human UI uses — this is a prerequisite for docs 11–13.

## 3. State layout (replaces plugin-local paths in 02/03)

All core state moves to `~/.local/share/organize-core/`: `index.json` (or sqlite — implementer's choice, internal format), `learning.json` (format per doc 04, unchanged), `operations.log` (per doc 05), `actions/` (per doc 12), `automations.db` (migrated from `~/.local/state/para-organize/`, per doc 06 §1), `backups/`. Config consolidates to `~/.config/organize-core/config.toml` — one file containing vault paths (02), suggestion weights (04), file-ops (05), consumers (06), metadata fields (07), routes (11), modes (12/13). The nvim `setup()` table keeps only UI concerns (layout, icons, highlights, keymaps) plus the socket path; everything behavioral lives in the core config so CLI and UI can never disagree. Ship a migration note mapping every old `setup()` key to its new home.

## 4. Safety unchanged

Doc 05's invariants bind the core: the nvim client cannot lose data because it cannot write files. The left-pane "fully editable buffer" flow (03 §3) adapts: the buffer edits the real capture file directly in nvim (normal `:w`), and the core re-reads on every operation — the one deliberate exception to "clients don't write", preserved because losing native buffer editing would break the UX. Core detects concurrent modification (mtime/hash check before mutating any file) and refuses with a clear error rather than clobbering.
