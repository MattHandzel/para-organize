# 09 — Rebuild Guidance for the Implementing Agent

How to execute the rewrite well. This file is advice + acceptance gates; the binding behavior is in 03–07.

## 1. Ground rules

1. **Parity first.** Same product, same surfaces (commands, keymaps, config schema, file formats, consumer effects). Deviations are allowed only where this spec explicitly says so (marked ⚠ or "decision"), and each is already decided in these documents — do not re-litigate or invent new features. The one scope addition is `07-metadata-editing.md`.
2. **Reference implementations.** Plugin: git rev `d753672~1` in this repo (HEAD is refactor-damaged; treat it only as a module-layout suggestion). Pipeline: current `scripts/automation/` HEAD is behaviorally complete; its defects are enumerated in 08 §B. When this spec is silent on a detail, read the reference implementation and match it.
3. **Fix every item in `08-known-issues.md`.** Each fix needs a regression test. Where a bug's "intended" behavior is ambiguous, the resolution is written in 03–06.
4. **Never lose data.** The vault is live and irreplaceable. All development/testing against fixture vaults; anything touching `~/Obsidian/Main` goes through the safety invariants of 05. The Syncthing-synced vault means any file can change under you mid-operation — read-modify-write windows must be small and writes atomic.
5. **Loud failure.** Misconfiguration, missing folders, unparseable YAML, LLM/server errors: log + surface, never silently continue with wrong behavior. This principle would have prevented all three live outages (wrong vault_dir, archives/archive mismatch, 3-month pipeline crash).

## 2. Suggested architecture (non-binding, but respect the boundaries)

Keep the two components separate (Lua plugin, Python pipeline) with the vault + frontmatter schema as their only coupling — that boundary has proven right. Within each:

**Plugin** — keep the module decomposition idea from HEAD (config / indexer / suggest / learn / move / ui / search / health / utils) but rebuild each module complete; the refactor's failure was mechanical splitting without tests, not the layout. Key structural corrections:
- One frontmatter parse/serialize module, round-trip safe, used by every reader/writer.
- One merge implementation, one archive implementation, one capture renderer.
- No `io.popen`/`os.execute`; `vim.uv`/`vim.fs`/plenary only. Async scan; UI never blocks >50 ms.
- Explicit session state machine (idle → session{captures, index, current, processed, skipped} → closed) with UI teardown on every exit path (WinClosed, `<Esc>`, `stop`).
- Load persisted state (index, learning) at `setup()`, not at `require()` time; no side effects on require.

**Pipeline** — keep emitter/store/consumer; corrections from 06: pure constructors, orchestrator-owned checkpointing, non-persisted filter misses, soft purge, one shared frontmatter module (absorb `capture_query.py`'s stricter parsing), one shared Ollama client (today there are three ad-hoc LLM code paths), config-required hosts.

## 3. Testing strategy (the old suite's lesson)

The old tests were almost all `require`-checks and `>0` assertions — they passed while the product was completely broken, and several encoded a different API than the code. Requirements:

- **Numeric scoring goldens:** fixed capture + candidate fixtures with exact expected scores per signal and end-to-end ranking (catches the inverted-similarity class of bug that `>0` tests cannot).
- **Frontmatter round-trip property tests** over a corpus copied from real vault files (include `no-ai: true`, `metadata: {}` and `[]`, scalar tags, `---` inside values, ISO-timestamp filenames).
- **End-to-end session test** in headless Neovim: build fixture vault → `start` → accept top suggestion → assert file moved, archived original, tag added, `processing_status: organized`, learning.json updated, operations.log line written → `next` auto-loaded.
- **Pipeline golden run** per consumer against a fixture vault + fake `task`/Ollama endpoints (record/replay); rerun-idempotency, checkpoint semantics (`success`/`skip` terminal; `error`/`limit` retried), config-change retroactivity, DB migration against a copy of the live `automations.db`.
- **Failure injection:** unwritable destination, invalid UTF-8 from `task export`, Ollama timeout, kill-during-write, vault path with spaces/`:`/unicode.
- Acceptance test lists at the bottom of 04, 05, 06, 07 are mandatory gates.

## 4. Performance targets (from PLAN.md, kept)

- Full index of 10k notes < 5 s (1k < 2 s); incremental single-file update < 500 ms; suggestion generation < 100 ms; UI action response < 50 ms; pipeline full-vault run (7.5k notes, no LLM work) < 30 s.
- Index persistence must not rewrite a multi-MB JSON on every keystroke-triggered update (batch, debounce, or switch format — internal format is free per 02).

## 5. Migration / cutover checklist

1. Correct Matt's live configs as part of delivery: nvim `vault_dir = "~/notes"` (or `~/Obsidian/Main`), `para_folders.archives = "archive"`; verify `:checkhealth para-organize` is green.
2. Regenerate the plugin index from scratch; carry over nothing from the stale `index.json`.
3. `learning.json` is empty — start fresh with the versioned schema.
4. Migrate `automations.db` per 06 §1 (preserve `success` history; drop `filtered` rows). Back it up first.
5. Install the new systemd units; remove/mask the old `second-brain-automation.*` chain's PARA step (leave Beeper sync independent); verify a failing run actually reports failure (test `OnFailure` fires).
6. First supervised run: pipeline in a dry-run/log-only mode over the real vault; diff intended actions against expectations before enabling writes. (A `--dry-run` flag on the CLI is in scope for this reason.)
7. Update README/MANUAL/TASKWARRIOR/architecture docs to match the rebuilt reality — every doc claim in 08 §A38/§B17 currently drifts; docs ship in the same PR as behavior.

## 6. Definition of done

- All acceptance gates in 04–07 pass; every 08 item has a fix + regression test; `make lint`/`make test` green; headless E2E session test green.
- Matt organizes 20 real captures in a live session: suggestions render, accept/skip/archive/merge all work, learning.json shows 20 recorded moves re-ranking a repeat destination upward, operations.log intact, zero data loss (verified by counting vault files before/after: nothing missing, originals archived).
- Pipeline runs from the timer for 48 h: processes new captures, correct summaries in journal, no duplicate Taskwarrior tasks, failures (if any) alert visibly.
