# organize-core Stress Test Report

Date: 2026-08-16
Environment:
- organize-core @ `a59fc16` (branch `rewrite`), Python 3.13.11, PyYAML, NixOS, Linux 6.19.2
- Neovim v0.11.6 (plugin campaign: see §Campaign 2)
- Test corpus: **read-only deep mirror of the real vault** (`~/Projects/KnowledgeManagementSystem/vault-mirror-golden/`, 70,357 md notes, checksum-verified against `~/Obsidian/Main`, taken 2026-08-16). Every run works on a disposable rsync working copy with isolated `ORGANIZE_CORE_*` state. The live vault was never touched; originals-integrity manifest at `~/vault-backup-20260816/originals-capture-manifest.txt`.
- Raw probe evidence: `~/vault-backup-20260816/stress-evidence/` (reports, 30-capture sample, timings, full results/comments jsonl.gz)

## Severity Rubric

- **P0**: Data loss/pollution, corrupted protocol responses, or a core feature unusable on real data
- **P1**: Wrong behavior, misleading output, latent correctness risk, resource starvation
- **P2**: UX/quality, non-blocking edge case
- **P3**: Note for the record

## Summary

| Severity | Found | Fixed | Rejected/Declined (ruled) |
|----------|-------|-------|---------------------------|
| P0       | 4     | 4     | 0 |
| P1       | 12    | 12    | 0 |
| P2/P3    | 4     | 1     | 3 (recorded) |

Suite after fixes: **1324 passed** (+32 new regression tests, 1 retired by ruling), ruff clean, 10k-note perf gate 1.9s (budget 5s). Every fix carries a regression test; the architect ruled every contested disposition (recorded in `ARCHITECTURE.md`).

## Campaign 1 — core engine against 70,357 real notes

Five probes ran against the mirror: full-corpus round-trip, suggestion quality (1,858-capture backlog + 30-capture stratified sample), 110-operation bulk soak, edge-hunter, and server soak (concurrency + crash-consistency).

### Headline verifications (what held)

- **Round-trip law on the full corpus**: 70,268/70,357 files byte-identical through `serialize(parse(t))`; 89 loud parse-refusals (broken YAML — refusal is the designed behavior); **0 value changes, 0 crashes, 0 serialization non-fixed-points, 0 of 3,775 frontmatter comments lost**.
- **Safety invariants under load**: 110/110 bulk ops succeeded with **zero invariant violations** (nothing deleted, originals archived under their own filenames, one ActionRecord per op, oplog complete); SIGKILL mid-burst left **no lost or half-applied writes**.
- **Learning loop on real data**: 100 recorded moves re-ranked a repeat destination from 0.30 (bare type bonus) to 0.345–0.588 across 71 moves — acceptance visibly teaches ranking.

### P0 findings (all fixed)

| ID | Finding | Fix |
|----|---------|-----|
| SQ-1 | **78% of the real backlog (1,450/1,858) got an identical, meaningless 9-row suggestion list** — every folder at the bare 0.30 type bonus, ASCII-sorted, empty reasons; 83/136 PARA folders unreachable at any cap | `min_confidence` now floors the *signal* score (type bonus excluded); zero-signal captures honestly yield only "Archive Now"; every non-archive suggestion carries ≥1 reason |
| BS-1 | **30.6% of moves (569/1,858) falsely refused** — "copy verification failed" on every CR-bearing note (universal-newline read-back vs written bytes) | byte-exact `_verbatim_text` verification; CR files added to the fixture vault |
| BS-3 | Each failed verification **left an organized duplicate** (`_1`, `_2`, … per retry) | destination rollback via a third sanctioned deleter, named in the never-delete AST guard |
| SOAK-02 | **Responses >208KB silently truncated mid-line** for any client >50ms behind draining (accept-timeout misapplied to accepted sockets) | partial-write resumption in `_send_bytes`; genuine drops logged |

### P1 findings (all fixed)

- SOAK-03: concurrent-writer race reported an *applied* move as `-32603` ("dictionary changed size"); stats now snapshotted under the write lock, post-write events best-effort.
- SOAK-04: reader starvation — `search.query` p95 216ms → **28.9s** under bulk writes; `_ReadWriteLock` now phase-fair (writer batches capped at 8).
- BS-2: three *failed* moves taught learning `count:3, success_rate:1.0`; partial failures are now first-class and excluded from learning + accept-rate stats.
- SQ-3: substring context matching made `resources/ui` rank-1 for "q**ui**tting toastmasters"; now token-granularity (sanctioned deviation from spec 04's literal wording).
- SQ-4: `productivity-system` (65 captures) never reached `areas/productivity`; match-time morphological variants (`suggestions.tag_suffix_strip` + singular/plural) in the scorer only — the shared tag normalizer is untouched (hard constraint, structurally pinned).
- EDGE-04: move into the note's own folder silently renamed it `_1` and archived the original (breaking `id:`/wikilinks); now an `ok=True` no-op.
- EDGE-07: `.backups` and the vault root accepted as destinations with false success; now refused with taxonomy+hint.
- EDGE-05: mutating ops on unparseable files now name the file in the error.
- EDGE-08: last-resort handler — one attributable error line, never a bare traceback (7/100 unattributable tracebacks in one sweep before).
- CLI-STATS: `index --full --stats` silently ignored `--full` (reported `total: 0` in 0.09s); now reindexes then reports.
- BS-5: `suggest --json` rank now 1-based, matching the documented `move --suggestions-json` input shape.
- RT-1: duplicate frontmatter keys with differing values (last-wins silently drops the earlier value on rewrite) now recorded in style metadata + warned with path and keys.

### Rejected / declined (ruled, on the record)

- SQ-2: signals 1+2 double-fire on pre-normalized tags — spec-literal, uniform, ordering-neutral; deduping would re-litigate parity (ruling F4).
- SQ-5 (P3): alias signal fired 0× and source signal 1× across the real backlog (timestamp aliases; `source: me` ×1,176) — recorded so nobody mistakes those weights for tuned.
- Per-op index cost (median ~860ms CLI wall time, ~726ms of it deserializing the 14MB index.json): a real cost, but an index-persistence *phase*, not a defect fix — declined for this pass; the warm server path (0.03ms suggest) is unaffected.

## Campaign 2 — para-organize.nvim (in progress)

Plugin suite (plenary, headless, isolated `minimal_init`) + the spec-09 §3 end-to-end gate driving the real core over a real socket. Findings and results will be appended when the Phase-2 fix pass lands. Known already: plenary's `:PlenaryBustedFile` child-nvim config leak (worked around in-process), AF_UNIX 104-byte socket-path limit surfaced as a health warning, `vim.json.encode({})`→`[]` wire class fixed at both ends.

## Provenance summary

Phase-1 build verification (fixture-based, before this campaign): 6 adversarial verifiers filed 37 findings — 25 critical/major — all fixed with sabotage-verified regression tests (TOCTOU write-guard, RPC metadata-validation bypass, merge-not-teaching-learning, vault containment, no-ai propagation, reversible oplog escaping among them). The real-data campaign above found the classes fixtures cannot: CR-bearing files, degenerate suggestion distributions, concurrency under real index sizes, and the vault's own filename zoo.
