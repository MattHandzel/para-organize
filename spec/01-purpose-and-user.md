# 01 — Purpose, User, and Success Criteria

## What this software is

**para-organize** is the *organize* stage of Matt Handzel's personal Knowledge Management System (KMS). The KMS follows a capture → organize → distill → express pipeline:

1. **Capture** (separate app, parent repo `KnowledgeManagementSystem/`): a web/Electron quick-capture app plus other ingestion paths (ntfy messages, audio transcriptions, clipboard dumps) write raw Markdown notes with YAML frontmatter into the Obsidian vault at `capture/raw_capture/`.
2. **Organize** (THIS project): move each raw capture to its correct long-term home in the vault's PARA structure (Projects / Areas / Resources / Archives), enriching or merging it along the way. It has two components:
   - A **Neovim plugin** (`lua/para-organize/`) — an interactive two-pane triage UI for a human working through the capture backlog.
   - A **Python automation pipeline** (`scripts/automation/`) — an unattended emitter/consumer daemon (systemd) that reacts to new/changed captures and fans out side effects (Taskwarrior tasks, LLM-answered questions, research notes, etc.).
3. Downstream: Anki card generation, weekly review, agents that read the vault.

The two components share one contract: **the vault layout and the capture-note frontmatter schema** (see `02-system-context.md`).

## The user

There is exactly one user: Matt — a keyboard-driven Neovim/terminal power user on NixOS who:

- Captures **dozens of notes a day** with near-zero friction (often voice-transcribed or one-line thoughts, frequently messy: missing frontmatter, sync-conflict duplicates, non-Markdown files like `.wav`, `.pdf`, `.epub` sitting in the same folder). The backlog is real: `capture/raw_capture/` currently holds ~2,300 files.
- Periodically sits down to **triage the backlog in batches** ("organizing sessions"), wanting to dispatch each note in seconds: glance at it, optionally edit it, pick a destination (ideally the top suggestion), and move on. Throughput is the whole game — every extra keystroke matters across hundreds of notes.
- Is **paranoid about data loss**. The system must never delete or clobber a note. Originals are archived, operations logged, writes atomic.
- Wants the system to **get smarter as he uses it** (learn tag→folder associations from accepted moves) but will not tolerate wrong "magic": suggestions must be transparent (show scores/reasons) and manual navigation must always be available as a fallback.
- Automates aggressively: anything a machine can do unattended (turn a `todo`-tagged capture into a Taskwarrior task, answer a question in a note with an LLM, kick off deep research on a person) should happen without him in the loop, idempotently, and safely retryable.

## The core interactive loop (what must feel great)

```
:ParaOrganize start [filters]
  ┌────────────────────────┬─────────────────────────┐
  │ LEFT: current capture  │ RIGHT: ranked            │
  │ (fully editable        │ destination suggestions  │
  │  normal buffer,        │ + browsable PARA folder  │
  │  rendered frontmatter  │ tree (P/A/R/archive),    │
  │  summary)              │ scores visible           │
  └────────────────────────┴─────────────────────────┘
  <CR> accept suggestion → note moves, original archived, learning updated, next capture loads
  a    archive it, next
  s    skip, next
  m / navigate-to-file    merge into an existing note (edit combined content, then commit)
  /    fuzzy-search destinations
  <leader>np/na/nr        create a new project/area/resource and file the note there
```

A capture is "done" when it has been **moved** (copy to destination + original archived), **merged** (content folded into an existing note + original archived), or **archived** directly. Skipping leaves it for a later session.

## Why a rewrite

The current implementation works in demo conditions but is buggy and unoptimized. The rewrite goal is **behavioral parity, not redesign**: rebuild the same product — same commands, same keymaps, same config surface, same file formats, same integrations — but correct, fast, and maintainable. Where current behavior is *accidentally* broken (bugs catalogued in `08-known-issues.md`), implement the documented *intended* behavior instead of the bug. Where behavior is intentional, preserve it exactly.

Concrete evidence of the quality problem (all verified on the live system, 2026-08-15):

- `learning.json` shows `total_moves: 0` — the flagship learning feature has never successfully recorded a move in over a year of existence.
- The live config points `vault_dir` at `~/Obsidian/Main/notes` (a near-empty subfolder) instead of the actual vault `~/Obsidian/Main`; the plugin silently indexed the wrong tree (stale entries in `index.json` prove it) rather than failing loudly. The rewrite must validate paths and complain.
- The vault's real archive folder is `archive/` (singular); plugin defaults assume `archives/`. Nothing warns about the mismatch.
- The index (`index.json`, 2.9 MB) grows without pruning deleted files and is fully rewritten on scans.

## Success criteria for the rewrite

1. **Parity**: every command, keymap, filter, config option, and file format documented in this spec works as specified. The Python pipeline processes the same `automations.toml` and produces the same downstream effects.
2. **Throughput**: organizing 50 real captures in a session with no perceptible UI lag; index of a 10k-file vault in seconds, incremental after that (see performance targets in `09-rebuild-guidance.md`).
3. **Safety**: it is impossible to lose note content through any code path — moves are copy-then-archive with atomic writes and an append-only operation log sufficient to manually undo anything.
4. **Loud failure**: misconfiguration (bad vault path, missing PARA folders, missing dependencies) is reported clearly at startup/`:checkhealth`, never silently ignored.
5. **Learning actually works**: accepted moves are recorded, persist across sessions, and demonstrably re-rank suggestions.
6. **Configurable metadata** (new requirement, see `07-metadata-editing.md`): during organizing, Matt can add/edit arbitrary user-defined frontmatter fields (e.g. extra tags, an "importance / worth remembering" flag) via quick keybindings, and defining a new metadata field is pure configuration — no code changes.
