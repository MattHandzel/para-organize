# Phase-6 Gate Report — the rewrite is built. Your decisions from here.

Date: 2026-08-16 · Branch `rewrite` @ `c18d905`+ · Status: **all build phases complete and conformance-passed. Nothing below happens without your sign-off.**

## What exists

The complete system specified in `spec/01–12`, rebuilt from scratch:

- **organize-core** — one Python program, CLI + JSON-RPC server: index (13,362 of your real notes in 2.7s), the exact 7-signal scorer + learning, safe file ops (never-delete, atomic, backup, TOCTOU-guarded), configurable metadata editing, tag routes with vault-as-truth retry idempotency, auto-tagging (both `tags` + `auto_tags`, grounded in your vault's vocabulary + folder descriptions), all four pipeline consumers with the corrected orchestration, the integrate engine (LLM edits in *your wording*, the target's style, structural deletion/verbatim/reorder guards, diff-review gate), and a complete ActionRecord corpus of every action — the training substrate you asked for.
- **para-organize.nvim** — a pure thin client: two-pane UI, full keymap surface, browse tree, metadata editing, telescope pickers, the integrate review pane (accept/edit/reject), checkhealth.
- **deploy/** — systemd units (10-min timer + path watcher + per-unit OnFailure alerting that provably fires) and the migration tool for your live `automations.db`.

**Verification story**: every phase passed parallel adversarial verification *plus* an independent whole-spec conformance pass; 60+ original defects have regression tests; the real-data campaign against a checksum-verified mirror of your entire vault found and fixed 4 P0 + 12 P1 defects the 2,000-test suite couldn't see. Final gate: **2,432 python tests + 246 plugin specs green, ruff clean, 10k-note reindex 1.92s (budget 5s), suggest 0.03ms (budget 100ms)**. Mutation-audit is the house standard (every guard proven by breaking it). Your vault was never touched: 19G faithful backup + integrity manifest at `~/vault-backup-20260816/`.

## Cutover — prepared, NOT executed (spec 09 §5)

~30 supervised minutes when you say go:
1. Back up + migrate `~/.local/state/para-organize/automations.db` via `organize migrate-store --backup-first` (dry-tested on a copy of your real DB: 7,516 notes kept, all 376 success rows preserved row-for-row, 22,497 dead filtered rows dropped, 15MB→5MB).
2. Write `~/.config/organize-core/config.toml` (generate with `organize health --example-config`; `doc/MIGRATION-from-old-setup.md` maps every old setting — including fixing the two live misconfigurations: wrong `vault_dir`, `archives`-vs-`archive`).
3. Point your nvim plugin spec at the worktree; `:checkhealth para-organize`.
4. Install the `deploy/` units; **mask the old `second-brain-automation` PARA step** (leave Beeper); run the OnFailure verification (a deliberate failing run must alert).
5. First run supervised: `organize run-consumers --dry-run` over the real vault, review the intended actions, then enable.
Then the spec's own definition-of-done needs you: **organize 20 real captures in a live session** and let the **pipeline soak 48h** on the timer.

## Six questions only you can answer

1. **no-ai archive**: may automation *relocate* (never write) a `no-ai` note? Currently allowed by the letter, unreachable in practice. Say "never touch" and one guard line tightens.
2. **Learning memory**: decay default is 0.9^days ≈ 3-week memory; your vault rhythms are monthly per the spec's own note. Flip to 0.99 (~2-month)? Ask-at-cutover.
3. **Zero-signal captures** now show *only* "Archive Now" (honest) instead of nine unexplained rows. Confirm you like this (browse is the fallback).
4. **question_answer** now fires only on explicit `question`/`q` tags (the 874-noise-file heuristic is opt-in config). You must tag questions.
5. **Double opt-in for unattended LLM edits** (route `auto=true` AND per-route `review="auto"`): recommend keeping. Confirm.
6. **Disk**: /home has been oscillating (1.6G→45G free today). Something large is churning; worth a look.

## Automatic organize (spec 13) — recommendation: NOT YET, corpus-gated

Everything it needs is built (routes+descriptions, the corpus with similarity query, the propose/apply seams, the trust ladder's config). Two gates before building it:
1. **Corpus**: confidence calibrates against *your accepted proposals* — an empty corpus can't calibrate. Your 20-capture session + a few weeks of normal use builds it. Frame the go/no-go by corpus size (~100+ decided actions), not calendar.
2. **Ruled precondition**: the LLMTrace must first distinguish machine-applied from human-confirmed verdicts, so auto-organize can never learn from its own actions (the self-reinforcement firewall, ruled and recorded).

When both hold, Phase 6 is one more workflow cycle on a proven base.
