# Destination recall — BASELINE evidence, 2026-08-16

Spec 21 §5 requires that the candidate-set widening be measured before and
after on real data, with the anti-SQ-1 metric set.  This file is the
**before** half.  The harness that produced it is `tools/recall_sweep.py`
and the raw per-capture result is committed beside this file as
`recall-sweep/sweep-baseline-2026-08-16.json`.

Committing the JSON is not housekeeping.  Once the spec-21 change lands, the
baseline can only be regenerated from a pre-change checkout — the exact
command is in "Reproducing the baseline" below — and a before number that
cannot be re-derived is not evidence.

## What was measured

| | |
|---|---|
| Code | `854ed97` (`spec: 19-21 — merge targets, duplicate captures, destination recall`), extracted with `git archive` so an in-progress working tree could not contaminate it |
| Vault | disposable rsync of `vault-mirror-golden`; 13,362 indexed notes |
| Backlog | **all 2,445** capture files under `capture/raw_capture` (not sampled) |
| Ballot | **135** candidates via `generate_candidates(para_subfolders)` |
| Clock | `--now 1755300000` (fixed, so learned-association decay is identical in both runs) |
| Learning | empty (`learning.json` absent from the isolated state dir); 0 associations, so signal #3 never fires |
| Routes | the 4 demo routes in `organize health --example-config` |
| Isolation | `ORGANIZE_CORE_{CONFIG,STATE,RUNTIME}_DIR` all under the work tree |
| Safety | golden mirror: **0 writable of 78,481 entries**; `~/Obsidian/Main` and `~/notes` never opened |
| Warm-path fidelity | `organize suggest --json` cross-check on 12 captures: **12/12 identical** on (path, type, score, reasons) |

## The numbers

| Metric | Baseline |
|---|---:|
| Captures with ≥1 real suggestion (routes included) | 570 (23.31%) |
| Captures with ≥1 **scored** suggestion (no route) | **568 (23.23%)** |
| Captures with **ZERO scored** suggestions — archive only | **1,877 (76.77%)** |
| Rank 1 carries ≥1 reason | 570 / 570 (100%) |
| Suggestions anywhere with an empty reason list | **0** |
| Distinct rank-1 destinations | 45 |
| Biggest rank-1 destination — `areas/productivity` | 100 (17.54% of covered) |
| Rank-1 entropy | 4.5543 bits |
| Largest identical-list cluster | 80 |
| Rank 1 driven **solely** by a stopword token | 0 |
| Rank 1 is a note / a folder | **0 / 570** |
| Median / p95 list length | 0 / 2 |
| Median top score | 3.70 |
| Suppressed duplicate note rows | 0 |
| `suggest()` warm p50 / p95 / max | **0.75 / 11.75 / 24.22 ms** |
| CLI path p50 / p95 (includes index deserialization) | 652 / 688 ms |

`568` and `1,877` reproduce spec 21 §1.2's measured figures exactly, which is
the check that the harness is counting what the doc counted.  The doc's
"23.2% / 76.8%" are the **scored** rows; the two extra captures in the
route-inclusive count are covered by a demo route and by nothing the ranker
found, which is why the two numbers are reported separately and why the
coverage gate reads off the scored one.

## Three baseline facts the after-run has to be read against

1. **The p95 budget is already missed, before any widening.**  §3.5 sets
   `suggest()` p95 ≤ 5 ms; today's 135-candidate ranker is at **11.75 ms**,
   with **190 of 2,445 captures over 5 ms** and a 24 ms worst case.  The
   tail is signal #5: the slowest capture has an empty context, two aliases,
   and one tag — 2 aliases × 135 candidates of Levenshtein.  §3.5's exact
   length prefilter is therefore fixing an existing defect, not merely
   preventing a new one, and "no worse than baseline" is not the bar.
2. **The largest identical-list cluster is already 80.**  §5.3's gate is
   ≤ 25.  A widened run that lands at, say, 40 has improved this metric and
   still failed the gate; both readings are true and the gate is the one
   that counts.
3. **`areas/productivity` is already rank 1 for 17.5% of covered captures.**
   §5.3's "no single destination is rank 1 for more than 5%" is not met by
   today's code either.  These three are recorded here so a verifier cannot
   be told that a post-change failure is a pre-existing condition without
   the pre-existing number being on the table.

## Reproducing the baseline

The harness refuses to record a baseline from widened code (it probes the
imported core for `index.candidate_folders`, `Candidate.kind`,
`Suggestion.destination_kind` and the four `suggestions.*` config keys), so
after the change lands the pre-change core has to be materialized:

```sh
cd <repo>
git archive 854ed97 | tar -x -C /tmp/head-checkout
cp tools/recall_sweep.py /tmp/head-checkout/tools/
ln -s "$PWD/.venv" /tmp/head-checkout/.venv

cd /tmp/head-checkout
.venv/bin/python tools/recall_sweep.py --mode baseline \
    --work /tmp/recall-work --prepare --repo <repo> \
    --now 1755300000 --cli-parity 12 \
    --out /tmp/recall-work/sweep-baseline.json
```

The after-run, and the comparison table, from the working checkout:

```sh
.venv/bin/python tools/recall_sweep.py --mode candidate \
    --work /tmp/recall-work-cand --prepare \
    --now 1755300000 --cli-parity 12 --human-sample 30 \
    --baseline /tmp/recall-work/sweep-baseline.json \
    --out /tmp/recall-work-cand/sweep-candidate.json
```

Two saved runs can be compared without re-running either:

```sh
.venv/bin/python tools/recall_sweep.py --compare BEFORE.json AFTER.json
```

Sampling, when a full sweep is not wanted, is `--limit N --seed S`; the
sample size and the seed are printed on every run and recorded in the JSON,
and the default is the whole backlog — there is no silent cap.
