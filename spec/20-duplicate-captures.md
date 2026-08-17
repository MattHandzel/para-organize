# 20 — Duplicate and Near-Duplicate Captures (NEW)

Directive from Matt (2026-08-16), verbatim:

> *"there is this behavior where sometimes i have many duplicated or nearly duplicated captures quickly after each other. please make a command `ParaOrganize dedup` that solves this problem, asking me to review before removing the duplicates"*

**"Removing" here can never mean deleting, and that is not a compromise — it is the property that makes the feature safe enough to run over a 1,858-capture backlog at all.** 05 §1.1 is absolute: every "remove" is a move into the archive tree, and `tests/test_fileops_safety.py::test_only_atomic_write_and_archive_may_remove_a_path` is an AST guard naming the only three functions in the codebase that may remove a path. Dedup adds none. A confirmed duplicate is **folded**: marked in its own frontmatter, archived under its own filename (05 §3), and cross-linked to the keeper, which absorbs its metadata. The note is still there, still readable, still resolvable by `[[wikilink]]`, and the fold is undoable (17). What Matt actually asked for — *"i have a lot of captures right now"* (19 §4) — is the backlog getting shorter, and folding delivers that without a single byte leaving the vault.

This doc owns **detection, the review gate, and the fold**. It owns no file primitive: the fold is `update_frontmatter` (05 §5) + `archive_capture` (05 §3) + `update_frontmatter`, in that order, through the ordinary `fileops` context. Doc 05 owns those. Doc 12 owns the records it writes, doc 17 owns undoing them, doc 14 governs every knob below.

---

## 1. The measurement — what the duplication actually is

Nothing below was designed against an imagined problem. Every number is from the read-only golden mirror `vault-mirror-golden` (chmod `a-w`, checksum-verified against `~/Obsidian/Main`, 70,357 `.md` files). The measurement pass **wrote nothing anywhere** — it opens files read-only; the disposable-working-copy pattern (21 §5.1) is required for the *apply* path's tests, not for a read-only census. Scripts: `scratchpad/measure_dupes.py`, `measure2.py`, `measure3.py`, `measure4.py`.

### 1.1 The corpus

| | Count |
|---|---:|
| `.md` files under `capture/raw_capture/**` (the backlog dedup scans by default) | **2,445** |
| …still `processing_status: raw` | **1,858** |
| …carrying an `id` or `capture_id` | 1,971 |
| Median body length | 57 tokens |
| Captures with < 5 tokens of body | 27 |
| `.sync-conflict-*` files, whole vault / under `capture/` / under `capture/raw_capture/` | 66 / 17 / **2** |

### 1.2 How much duplication there is

Under the detection rules of §2, on that corpus:

| Layer | Groups | Notes | Pairs |
|---|---:|---:|---:|
| **D1** byte-identical files (whole file, frontmatter included) | 3 (sizes 11, 3, 2) | 16 | 59 |
| **D2** identical *normalized body*, frontmatter differing | 34 (largest 14, 8, 6, 6, 5, 5, 5) | 114 | 172 |
| **D3** same `capture_id`/`id`, written more than once | 12 (sizes 13, 8, 6, 6, 3, 2×7) | 50 | 86 |
| **D4** near-duplicate (§2.3) | — | — | 249 |
| **Union, after grouping** | **44** | **174** (7.1% of captures) | **566** |

174 notes in 44 groups means **130 fold candidates** — a 7% shorter backlog, and the largest single group is 20 notes.

The step from D1 to D2 is the whole argument for normalizing before comparing: byte comparison finds **3** duplicate groups; normalizing whitespace/Unicode and ignoring frontmatter finds **34**, an 11× difference, because the duplicates differ in exactly the fields an ingest necessarily rewrites (`timestamp`, `last_edited_date`) and in NBSP/CRLF noise.

### 1.3 "Quickly after each other" — quantified

Gap between the two members of a duplicate pair (n = 566), using the highest-resolution timestamp available (frontmatter `timestamp`/`id` for 1,954 notes, `date`+`time` for 240, filename for 210, mtime for 41):

| Gap ≤ | 1 s | 10 s | 1 min | 15 min | 1 h | 24 h | 7 d | 30 d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| pairs | 204 (36%) | 268 (47%) | 315 (56%) | 321 (57%) | 329 (58%) | 435 (77%) | 454 (80%) | 513 (91%) |

Median **15 s**. Matt's word "quickly" is exactly right for the mode — over a third of duplicate pairs are under one second apart. **But 20% of them are more than seven days apart and the maximum is 180 days.** That single fact decides a design question: **the time window is a display signal and a confidence input, never a filter.** `dedup.window_hours` defaults to `0` = disabled, because any window tight enough to describe the mode discards a fifth of the real duplicates.

### 1.4 The causes, from the evidence

| Cause | Evidence | Share |
|---|---|---|
| **Ingest retry loop / double-write.** The same capture event written 2–13 times, seconds apart, landing under the `_1`, `_2`, … collision suffix of 05 §2.4 with an identical `capture_id` and an identical body — only `timestamp` differs. | `2026-04-01T04:05:02.255Z.md` and `…_1.md`: 1,297 bytes each, identical body, identical `id`/`capture_id`/`aliases`, `timestamp` 21:08:58.55 vs 21:09:03.69. 26 files in the backlog carry a `_N` suffix. Gaps *inside* a `capture_id` group: p50 **3.6 s**, 34 of 38 under 60 s. | D3, and the sub-second half of §1.3 |
| **Fixed-interval re-emission.** A source re-writes the same capture on a timer. | `2025-08-19T02:04:50 / :04:55 / :05:00 / :05:05 / :05:10 / :05:15 / :05:17` — seven notes, identical bodies, **exactly 5 s apart**. | the 47% ≤ 10 s |
| **Automated notification re-send.** An ntfy alert fired repeatedly; bodies differ only in an embedded timestamp. | 20 × `ntfy-2026-06-30-XXXX-linear-watcher-scope-blind.md` over 19.8 h; pairwise Jaccard 0.83, **0 novel words**. | the largest group |
| **Genuine re-capture / re-import.** Matt says or clips the same thing again days or months later, or a source is re-imported. | 117 pairs with identical bodies and gaps > 15 min; one byte-identical pair 12 days apart (`2025-11-16T20:41:05` / `2025-11-28T19:54:20`). | the 20% > 7 d |
| **Syncthing conflicts.** Real, but **not** a capture-backlog problem. | 66 vault-wide, 17 under `capture/`, **2** under `capture/raw_capture/`, and **zero** of them appear in any duplicate group at any threshold — the conflicted files live in `capture/*.md` (dailies), not the raw backlog. | ~0 |

**Consequence for scope:** dedup is aimed at the raw capture backlog. `.sync-conflict-` files are worth a mention in `organize health`, not a feature — and §7 records why folding them here would be wrong.

### 1.5 The trap: templates are not duplicates

The single most important measurement. At raw Jaccard ≥ 0.8 the corpus contains a **20-note component spanning 193 days** which is *not* duplication at all — it is Matt's kaizen time-block planning template, filled in differently each time:

```
**Time block**: 08:05-8:30          **Time block**:
**Activity**: Apply for SPC Commons **Activity**: Restructure my business, realign myself
**Plan**: - What is the goal…       **Plan**: - What is the goal…
```

281 tokens of shared scaffold, a handful of tokens of real content each. A naive similarity threshold sweeps 20 legitimately distinct notes. At Jaccard ≥ 0.8, template pairs and true near-duplicates are **not separable by similarity alone**: the ntfy re-sends score 0.83 and the kaizen pairs score 0.82–0.84.

They *are* separable on the symmetric difference. Over the 566 pairs at raw Jaccard ≥ 0.8:

| Family | n | novel words (min / p50 / p90 / max) |
|---|---:|---|
| ntfy same-slug repeats (true duplicates) | 207 | 0 / 0 / 0 / **0** |
| identical body, unrelated filenames | 171 | 0 / 0 / 0 / 0 |
| same `capture_id` | 68 | 0 / 0 / 1 / 15 |
| everything else (template family + real re-captures) | 120 | 0 / **8** / 16 / 16 |

Hence §2.3's second criterion, and hence a novel-token budget of **6**: it admits the whole of the first three families and excludes the kaizen fills. The residual is named honestly in §7 and handled by the review gate: 14 of the 16 kaizen-family notes have a **byte-identical unfilled body** (they are genuinely the same blank template saved 14 times); the other 2 differ by ~5 real words and *are* pulled in at budget 6. That is a false positive, it is visible in the diff the gate shows, and rejecting it costs one keystroke.

---

## 2. Detection

### 2.1 What is scanned

Notes under `dedup.scan_paths` (default `["capture/raw_capture"]`, vault-relative, recursive), resolved through the existing `VaultIndex` — dedup reads the index, it does not re-walk the vault. Excluded, always:

1. Anything under the archive root (a fold's own output must never re-enter the scan).
2. Any note already carrying `dedup.marker_key` in its frontmatter — the **vault-as-truth** rule of `ARCHITECTURE.md:1559` (CRITICAL-1) applied here exactly as 19 §2.4 applies it to merge: "already folded" is read from the vault, not from a store, so a re-run after a crash cannot fold twice.
3. Notes whose normalized body is shorter than `dedup.min_body_tokens` (default 5) — measured: 27 captures qualify, and two 3-word captures being identical says nothing.
4. Notes whose frontmatter has `no-ai: true` — they are *listed* in the report with a refusal note, and folding one refuses with the existing `NoAiRefusal` shape unless the acting actor is human (02's vault law; `HUMAN_ACTORS` already exists in `fileops.py`).

### 2.2 Normalization (the one definition, used by D2 and D4)

Applied to the **body only**, frontmatter stripped by the shared `frontmatter` module:

1. Unicode NFKC; U+00A0 → U+0020.
2. Every whitespace run → one space; strip.
3. Lowercase.

`body_hash = sha256(normalized)`. For D4 only: tokenize on `[a-z0-9']+`, then take the set of `dedup.shingle_size`-word shingles (default **5**; a document shorter than that contributes its single whole-token-string shingle).

Frontmatter is deliberately excluded from the comparison and deliberately *reported*: §1.2 shows that including it collapses 34 groups to 3, and §4 shows the reviewer every frontmatter difference the fold would consign to the archive.

### 2.3 The four criteria

Two captures are duplicates when **any** of these holds:

| | Rule | Config | Rationale from §1 |
|---|---|---|---|
| **D1** | identical file bytes | — (never disabled) | 3 groups, 16 notes |
| **D2** | identical `body_hash` | — (never disabled) | 34 groups, 114 notes; the 11× step over D1 |
| **D3** | same non-empty `capture_id`, else same non-empty `id` | `dedup.same_capture_id` (default `true`) | the ingest wrote one capture event twice; 12 groups, and **7 of those 12 have body differences**, so this must not be folded into D2 |
| **D4** | `jaccard ≥ dedup.similarity` **and** `novelty ≤ dedup.novel_token_budget` | `similarity` 0.80, `novel_token_budget` 6 | §1.5 |

**Jaccard** = `|A ∩ B| / |A ∪ B|` over the shingle sets. Chosen over edit distance (O(n·m) per pair, and a 6,000-token clipboard capture makes that the whole cost of the run), over cosine/TF-IDF (§7 — measured to fail on this corpus), and over SimHash (a 64-bit sketch is a *blocking* device, not a decision device; using it for both makes the threshold un-auditable).

**Novelty** = `max(|W(a) \ W(b)|, |W(b) \ W(a)|)` where `W(x)` is the set of distinct tokens of the normalized body with length ≥ 3, not purely numeric, and not in the stopword set (a built-in ~100-word English list ∪ `dedup.stopwords`). It answers the question similarity cannot: *does either note say anything the other does not?* Pure-digit tokens are excluded precisely because that is what the ntfy re-sends differ by.

Both numbers, per pair, appear in the review gate. A threshold nobody can see is a threshold nobody can calibrate.

**`dedup.window_hours`** (default `0` = off): when > 0, D4 additionally requires the timestamp gap to be within the window. D1/D2/D3 are **never** time-gated — a byte-identical re-import 180 days later is still a duplicate (§1.3).

### 2.4 Blocking (why this is not 1.7M comparisons)

All-pairs over 2,445 captures is 2,987,790 comparisons; over the 1,858-note raw backlog, 1,725,753. Neither is run. Candidate pairs come from the union of four buckets, each O(n):

1. **`file_hash`** buckets → D1, exact, no misses.
2. **`body_hash`** buckets → D2, exact, no misses.
3. **`capture_id` / `id`** buckets → D3, exact, no misses.
4. **Bottom-k sketch** for D4: hash every shingle once (BLAKE2b-64), keep the 64 smallest, bucket the note under each of its **16 smallest**. Two notes with Jaccard ≥ 0.8 share ≥ 80% of their shingles and therefore collide on a smallest-hash with probability ≈ 1 − 0.2¹⁶.

Measured recall of the bottom-k blocker against a 128-permutation MinHash + 32-band LSH ground truth at Jaccard ≥ 0.8: **566 / 566, exact**. Measured cost: bottom-k sketching is **0.05 s** where the 128-perm MinHash is **10.9 s** — 200× cheaper for the same recall on this corpus, because it hashes each shingle once instead of 128 times.

A bucket holding more than `dedup.max_bucket` (default 400) members is a boilerplate bucket, not a duplicate bucket; it is skipped for candidate generation and **counted in the report's `skipped_buckets`** so the skip is never silent (09 §1.5).

Result: **14,492 candidate pairs** instead of 2,987,790 — a 206× reduction — of which 566 survive scoring.

### 2.5 Grouping and the keeper

Candidate pairs that satisfy §2.3 form an undirected graph; a **group** is a connected component. Measured group-size histogram: 26×2, 5×3, 2×4, 4×5, 2×6, 1×7, 1×8, 2×16, 1×20.

Transitive chaining is a real hazard in principle (a → b → c where a and c are unrelated). Measured on this corpus it does not occur: recomputing groups as *stars* (every member must satisfy §2.3 against the keeper directly, not transitively) yields **identical groups — 44 groups, 174 notes, identical size histogram**. The spec therefore requires the cheaper connected-component form **and** requires the report to carry, per member, its similarity **to the keeper** (not to whichever neighbour pulled it in), so a chained member is visible as a low number in the gate. A group larger than `dedup.max_group_size` (default 25) is reported with `refusal: "group too large"` and cannot be folded until Matt splits it — the 20-note ntfy group sits deliberately just under that line.

**Keeper choice** (`dedup.keeper`, default `"longest_then_earliest"`): most tokens wins; tie → earliest timestamp; tie → lexicographically first vault-relative path (total order, so the choice is deterministic and testable).

- *Longest first*, because 7 of the 12 `capture_id` groups have body differences and the retry sometimes captured **more** (a clipboard that finished loading). Keeping the shorter one would be the only way this feature could lose content that is not archived.
- *Earliest on a tie*, because in every `_N` collision family the un-suffixed original is the earliest, and it is the filename that `[[wikilinks]]` and the `id` point at. Measured: longest == latest in 39 of 44 groups, but **39 of 44 are length ties**, so the tie-break is doing all the work, and it must resolve to the original.
- Alternatives `"earliest"`, `"latest"`, `"longest_then_latest"` exist for other people's ingest shapes (14 §1's stranger).

Measured cost of the keeper rule, reported per group in the gate: **20 of 130 losers (15%) hold at least one meaningful word the keeper does not.** They are flagged `⚠ unique content` and are excluded from bulk acceptance (§3.3).

---

## 3. The review gate

Matt's words are *"asking me to review before removing the duplicates"*. The gate is **law, not a knob**. There is no config key that turns it off — mirroring 19 §2.4's ruling that an override which can silently duplicate (here: silently fold) vault content is not a knob. The only bypass is per-invocation and explicit: `organize dedup --apply --yes`, typed by a human each time.

### 3.1 Nothing is written until the whole plan is committed

`dedup.scan` is a **read-only** RPC: not in `MUTATING_METHODS`, takes the read lock, writes no file, writes no operation-log line, writes no `ActionRecord`. Review happens entirely in memory in the client (or on stdout for the CLI). Aborting — `q`, `<Esc>`, `<C-c>`, SIGINT, a crash — leaves the vault **byte-identical**, and §11 makes that a pinned test rather than a claim.

`organize dedup` with no flags **is** the dry run. `--apply` is required to write. The global `--dry-run` (17 §6's precedent: one global flag, no per-command duplicate) always wins: `organize --dry-run dedup --apply` prints the plan and writes nothing, and says so on the last line.

### 3.2 What a group looks like

One block per group, newest-scanned first, groups with the highest confidence first (all-identical bodies before near-duplicates):

```
dup_01JAV3K7  ·  8 notes  ·  identical body  ·  span 5.2 s
  KEEPER  capture/raw_capture/2026-04-01T04:05:02.255Z.md
          143 tokens · 2026-04-01 21:08:58 · chosen: longest body; tie → earliest of 8
  fold    …/2026-04-01T04:05:02.255Z_1.md   sim 1.00  novel 0  +5.1 s   [identical body]
  fold    …/2026-04-01T04:05:02.255Z_2.md   sim 1.00  novel 0  +10.3 s  [identical body]
  …
  matched by: same capture_id (2026-04-01T04:05:02.255Z), identical body
  keeper absorbs: tags +[public]  sources +[https://…]      (2 fields, 3 values)
  archived only: location{lat 30.1463, lon −94.6424} on _2   ⚠ not absorbed
```

Per group, the reviewer sees, without pressing anything: the group id, size, why it grouped (which of D1–D4 fired), the time span, **which note is the keeper and the exact rule that chose it** ("longest body; tie → earliest of 8"), and per member the similarity to the keeper, the novelty count, the signed time offset, and the criterion that admitted it.

Per group, one keystroke away (`d`): the **unified diff** between the keeper and the selected member, generated by the same `_unified_diff` helper `fileops` already uses for `TargetState.diff`, so the gate and the corpus show byte-for-byte the same thing.

Per member, always shown when non-empty: **the metadata that would survive only in the archive.** Measured over the 130 losers — 11 carry a tag the keeper lacks, 3 a source, 4 a different `location` block, 2 a frontmatter key the keeper does not have. Fields in `dedup.absorb` are rendered as `keeper absorbs`; everything else as `archived only ⚠`. A reviewer must be able to see, before saying yes, exactly what stops being visible in the live vault.

### 3.3 Accepting, rejecting, and partial acceptance

| Act | CLI | nvim (dedup view) | Effect |
|---|---|---|---|
| accept group | `--group dup_01JAV3K7` (repeatable) | `<CR>` / `y` | marks the group for folding |
| reject group | omit it, or `--reject dup_…` | `x` / `n` | group untouched, and the rejection is remembered for the session only |
| change keeper | `--keeper dup_…=<path>` | `K` (cycles members) | re-renders the group with the new keeper and recomputed per-member numbers |
| **keep one member** | `--keep dup_…=<path>` | `m` on the member | that member leaves the group and stays in the backlog; the rest may still fold. This is the answer to the 15% of losers with unique content |
| accept every all-identical group | `--all-identical` | `A` | applies only to groups where every member's `body_hash` equals the keeper's **and** no member is flagged `⚠ unique content` — measured: **30 of 44 groups, 89 notes**. Near-duplicate groups are never bulk-acceptable |
| commit | `--apply` (+ `--yes` to skip the prompt) | `<leader>dc` | the single mutating call |
| abort | `<C-c>` / EOF | `q` / `<Esc>` | nothing written, exit 0 with `no groups folded` |

Interactive CLI (a TTY, no `--group`/`--all-identical`) walks groups one at a time with `y/n/k/m/d/A/q`; non-TTY or `--json` never prompts and never applies without explicit group selection.

`--all-identical` is deliberately *not* `--all`. There is no flag that folds near-duplicates unreviewed.

### 3.4 The commit is verified against what was reviewed

`dedup.scan` returns a `scan_token` and, per note, its `sha256`. `op.dedup_fold` takes both back. Before the first write it re-hashes **every** file in the plan — keepers and losers — and refuses the whole call with `ConcurrentModificationError` (existing class, existing message shape, `error.data.kind`) if any byte changed since the scan. This is the Syncthing rule of 09 §1.4 stated as a precondition: the vault changes under you, and a plan reviewed against yesterday's bytes is not consent for today's.

Precondition-complete before the first write, all-or-nothing per call, exactly as 17 §2's invariant 1 requires of undo. A refusal writes no oplog line and no `ActionRecord`.

---

## 4. The fold — what actually happens

For each group, in this order, all through the ordinary `OperationContext` (backups on, atomic writes, operation log, index update):

**Per loser, step 1 — mark it.** `update_frontmatter(loser, changes)` writing:

```yaml
duplicate_of: capture/raw_capture/2026-04-01T04:05:02.255Z.md   # vault-relative path of the keeper
duplicate_of_id: '2026-04-01T04:05:02.255Z'                     # keeper's id/capture_id, may be null
duplicate_group: dup_01JAV3K7
duplicate_reason: identical_body            # identical_bytes | identical_body | same_capture_id | near
duplicate_similarity: 1.0                   # jaccard to the keeper, 3 dp
duplicate_folded_at: '2026-08-16T09:12:04Z'
processing_status: duplicate                # terminal; removes it from the raw backlog
```

The key **name** `duplicate_of` is `dedup.marker_key` (14 §D7's precedent: the law is not configurable, the spelling is); the other five keys are derived from it by suffix, so one knob renames the family. Machine-owned bookkeeping in frontmatter, like `auto_tag_hash` — curation tooling leaves it alone. Written to the loser *before* archiving, so the archived copy carries its own explanation forever.

`processing_status: duplicate` is a **new value** of an existing field. Required edits: the `processing_status` vocabulary in 02's frontmatter contract, and `QueryCriteria.status` docs. Sessions filter it out by default the same way `organized` is filtered.

**Per loser, step 2 — archive it.** `archive_capture(loser)` exactly as 05 §3: `<vault>/<para_folders.archives>/<paths.archive_capture_path>/<filename>`, **original filename kept**, `_%Y%m%d_%H%M%S` on collision, copy+verify+unlink fallback across filesystems, result checked. Dedup adds **no new archive path and no new removal call site.**

**Per group, step 3 — the keeper absorbs.** One `update_frontmatter` on the keeper:

- Every field named in `dedup.absorb` (default `["tags", "sources"]`) becomes the union across the group, deduped and ordered **keeper-first, existing order and casing preserved, new values appended** — the identical rule 05 §4 already specifies for merge, reusing `merge_tags`. Measured need: 11 losers carry a tag, 3 a source, that would otherwise only exist in the archive.
- `duplicates_folded`: a list, appended (never replaced), of the archive-relative paths of this fold's losers. This is the cross-link back; the archived loser points forward via `duplicate_of`, so the link is navigable in both directions with no body write.
- `last_edited_date` = today, per 05 §5.

**The keeper's body is never touched.** No merge, no appended block, no `<!-- organize:merged -->` marker. Folding is not merging: if Matt wants a near-duplicate's unique sentence in the keeper, the tool for that is `m` (19 §2), and the gate tells him which members have unique content so he can `m`-out and merge them deliberately. `dedup.absorb = []` turns absorption off entirely; `absorb` may name any frontmatter field, and unknown fields are unioned as lists / replaced as scalars by `update_frontmatter`'s existing rules.

### 4.1 What is recorded (12 §2)

**No new `Operation` value, and therefore no `ACTIONS_SCHEMA_VERSION` bump from this doc.** A fold is spelled entirely in operations that already exist, which is also what makes it undoable for free:

| Act | `ActionRecord.operation` | `targets[]` |
|---|---|---|
| mark a loser | `meta_edit` | the loser, role `destination`, `details["keys"]` = the six marker keys |
| archive a loser | `archive` | the loser |
| keeper absorbs | `meta_edit` | the keeper, `details["keys"]` = absorbed fields + `duplicates_folded` + `last_edited_date` |

An 8-note group therefore writes 15 records: 7×(mark, archive) + 1 keeper edit. That is verbose on purpose — 12's directive is *"Store as much as possible about the state of the system when doing every action"*, and each record carries `capture.body_before` and `frontmatter_before`, which is precisely the training signal for "these two captures were the same thing".

**One new correlation field:** `ActionContext.dedup_group: str | None`, the `dup_<ulid>`, set on every record a fold writes. Trailing, defaulted `None`, omitted from `to_json()` when empty, parsed fail-closed — the exact shape of 17 §4's `context.undoes` and of `LLMTrace.proposal_id`. Every pre-existing record reads back as "not a fold". Additionally `context.details` carries `similarity`, `novelty`, `reason`, `keeper`, and `group_size`, so the corpus records *why* the machine proposed it and that a human accepted it.

**Learning:** a fold teaches the destination model **nothing**. `learn.record_action` skips every record whose `context.dedup_group` is set — one guard beside 17 §4's `undoes` guard. Folding a capture is not a statement about where captures belong, and teaching "captures like this belong in the archive" would poison the ranker for the exact captures Matt takes most often. Stated as a hard rule because the failure would be silent and slow.

**Actor:** whoever ran it — `matt` from the plugin/CLI review. A fold has no machine-initiated path in this doc (see §7 and §11.4 on auto-dedup).

### 4.2 Undo (17)

Every record a fold writes is of a type 17 §1 lists as **fully undoable**: `archive` (verified relocate back) and `meta_edit` (key-scoped frontmatter restore of exactly `details["keys"]`, never a body restore). So the fold is undoable with **zero new undo machinery** — this is the payoff of §4.1's spelling.

Undoing a whole group means reversing its records in **exactly reverse order** (keeper absorb → un-archive each loser → un-mark each loser); undoing them out of order can leave a loser un-archived but still marked. Two consequences, both binding:

1. **Required addition to 17 §6:** `organize undo --dedup-group dup_01JAV3K7` and `op.undo(dedup_group=…)`, which expands to that group's records newest-first, evaluates **every** precondition of 17 §3 across all of them before the first write (17 §2 invariant 1's batch rule), and refuses the whole batch if any one fails. Single-record `organize undo <action_id>` stays available and is honest about doing only its half.
2. The gate's commit output prints the group's undo command verbatim, so the safety net is one copy-paste away at the moment of maximum doubt.

`file_ops.create_backups = false` does not make a fold non-undoable (unlike merge) — nothing is overwritten; the loser is relocated and its frontmatter is key-scoped-restorable from the record.

---

## 5. Surfaces

### 5.1 CLI

`dedup` joins **all three** declaration sites — `SUBCOMMANDS`, `build_parser`, `_HANDLERS`. (17 §6 already records that `purged` is in only two of the three, which is proof these lists are not machine-checked; §11 requires the test that checks them.)

| Command | Flags |
|---|---|
| `organize dedup` | `--path P` (repeatable, overrides `dedup.scan_paths`) · `--since D` `--until D` · `--similarity F` `--novel-budget N` `--window-hours F` (per-run overrides of the config, 14 §4.4 precedence) · `--exact-only` · `--limit N` (groups shown) · `--json` |
| …selection | `--group ID` (repeatable) · `--reject ID` · `--keeper ID=PATH` · `--keep ID=PATH` · `--all-identical` |
| …writing | `--apply` · `--yes` |

Exit codes: `0` = ran (including "nothing to fold" and "aborted"), `1` = refusal/error, per the existing convention. Human output follows `_print_result`'s shape; `--json` goes through `_json_out` (sorted keys, indent 2) and emits the RPC result objects verbatim.

```jsonc
// dedup.scan — result
{ "scanned": 2445, "scan_token": "scn_01JAV…", "duration_ms": 1270,
  "skipped_buckets": 0, "truncated": false,
  "groups": [
    { "id": "dup_01JAV3K7", "size": 8, "reason": "identical_body",
      "span_seconds": 5.2, "bulk_acceptable": true,
      "keeper": { "path": "capture/raw_capture/2026-04-01T04:05:02.255Z.md",
                  "sha256": "4256dd51…", "tokens": 143, "ts": "2026-04-01T21:08:58Z",
                  "why": "longest body; tie -> earliest of 8" },
      "members": [
        { "path": "capture/raw_capture/2026-04-01T04:05:02.255Z_1.md", "sha256": "b9205734…",
          "similarity": 1.0, "novelty": 0, "offset_seconds": 5.1,
          "matched_by": ["identical_body", "same_capture_id"],
          "unique_content": false,
          "absorbed":     { "tags": ["public"] },
          "archived_only": { "location": { "latitude": 30.1463, "longitude": -94.6424 } },
          "diff": "--- keeper\n+++ member\n…" } ],
      "refusal": null } ] }

// op.dedup_fold — params
{ "scan_token": "scn_01JAV…",
  "plan": [ { "group": "dup_01JAV3K7",
              "keeper": "capture/raw_capture/2026-04-01T04:05:02.255Z.md",
              "fold":   ["…_1.md", "…_2.md"],
              "expect": { "…_1.md": "b9205734…", "…_2.md": "…" } } ],
  "dry_run": false }
// op.dedup_fold — result
{ "ok": true, "dry_run": false, "groups": 1, "folded": 2, "kept": 6,
  "archived": [ { "path": "…_1.md", "to": "archive/capture/raw_capture/…_1.md" } ],
  "keepers":  [ { "path": "…255Z.md", "absorbed": { "tags": ["public"] } } ],
  "records":  ["act_01JB…", "act_01JC…"],
  "undo":     "organize undo --dedup-group dup_01JAV3K7",
  "partial_failure": null }
```

### 5.2 RPC

Two methods, added to `RPC_METHODS`, `self._handlers` and `EXTRA_METHODS` in `tests/test_server_protocol.py`:

- **`dedup.scan`** — read-only, in the read path, **not** in `MUTATING_METHODS`. Named in the noun-first read-only style of `search.query` / `folder.list` / `routes.resolve`.
- **`op.dedup_fold`** — in `MUTATING_METHODS` (it writes vault files) and in `INDEX_CHANGING_METHODS` (it moves notes), in the `op.<verb>` style of `op.merge_commit`. The `scan_token` + per-file `expect` hashes are the same snapshot-checking pattern `op.merge_commit` already uses.

Errors: `-32000` with `data.kind ∈ {OperationError, ConcurrentModificationError, NoAiRefusal, ConfigError}`; `-32602` for a malformed plan (unknown group, keeper not in its group, a path in both `fold` and `keep`). **No new error class.**

A scan taking >1 s must not block the UI: `dedup.scan` streams `progress` events on the existing `events.subscribe` channel (`{phase: "scan"|"block"|"score", done, total}`), the same channel `index.reindex` uses.

### 5.3 The plugin

`{ name = "dedup", action = "dedup" }` in `commands.SUBCOMMANDS`. **No session required** — dedup is a backlog-hygiene pass, not a per-capture act; run outside a session it opens standalone, run inside one it opens as a right-pane view and restores the previous view on close (19 §2.5's `view_stack`, pushed on entry, popped by `<BS>`).

The dedup review is a **view in the `render.VIEWS[name]` registry** that 17 §7 already requires the right pane to become — not a third hardcoded branch, not a floating window with its own key handling. It is **not editable**, so 19 §6.2's `<leader>`-only rule does not apply and plain keys are free: `j`/`k` member, `J`/`K`… no — the bindings are exactly §3.3's, in `keymaps.dedup` (client-side, 14 §4.1: they press keys, they change no vault byte):

```lua
keymaps = { dedup = { accept = "<CR>", reject = "x", keeper = "K", keep_member = "m",
                      diff = "d", accept_identical = "A", commit = "<leader>dc",
                      abort = "q" } },
ui = { dedup = { diff_context_lines = 3, max_groups = 200, show_absorbed = true } },
```

While the scan runs the view shows a progress line and stays responsive (09 §4's 50 ms rule); the scan is issued async and the UI is never blocked on it. After a commit: a toast naming the counts (`folded 12 captures into 5 keepers`), the session's queue re-filtered so folded captures disappear from it, and `u` (17 §7) bound to the group undo of the fold just committed.

### 5.4 Config (14 §4.1, §4.3)

Every key changes a vault byte, a similarity score, or which notes are proposed — so **all of them are core config** (`~/.config/organize-core/config.toml`), and the CLI and the UI can never disagree about what is a duplicate. Each has exactly one reader; none may sit in `RESERVED_CONFIG_LEAVES`. Defaults are chosen for 14 §1's stranger with a fresh vault: on, conservative, review-gated.

```toml
[dedup]
enabled = true                      # false ⇒ the command refuses with ConfigError, naming the key
scan_paths = ["capture/raw_capture"]  # vault-relative roots scanned, recursive
similarity = 0.80                   # Jaccard over 5-word shingles of the normalized body (§2.3 D4)
novel_token_budget = 6              # max meaningful words either note may hold alone (§1.5)
shingle_size = 5                    # words per shingle
min_body_tokens = 5                 # shorter bodies are never near-duplicate candidates
same_capture_id = true              # two notes with one capture_id are the same capture event (D3)
window_hours = 0                    # 0 = no time gate; >0 gates D4 only (20% of real dupes are >7d apart)
max_group_size = 25                 # larger groups are reported with a refusal, never folded
max_bucket = 400                    # blocking buckets bigger than this are boilerplate; skipped and counted
keeper = "longest_then_earliest"    # | "longest_then_latest" | "earliest" | "latest"
absorb = ["tags", "sources"]        # frontmatter fields the keeper unions from the folded notes; [] = none
marker_key = "duplicate_of"         # spelling of the machine-owned marker family (§4)
stopwords = []                      # extra words ignored by the novelty count, on top of the built-in list
```

There is **no** `dedup.auto`, no `dedup.confirm`, and no `dedup.skip_review`. The review gate is not configurable (§3, §7).

---

## 6. Performance, on the real numbers

Budgets from 09 §4: UI action response < 50 ms, incremental index update < 500 ms, full index of 10k notes < 5 s. Measured on the golden mirror with the venv interpreter, mean of three runs:

| Phase | 2,445 captures (the default scope) |
|---|---:|
| read + parse + normalize + shingle | 0.42 s |
| blocking (4 bucket families, bottom-16-of-64) | 0.30 s |
| scoring 14,492 candidate pairs | 0.55 s |
| **total scan** | **1.27 s** |
| peak RSS | 108 MB |
| comparisons avoided | 2,987,790 → 14,492 (**206×**) |

**Budget:** a scan of the default scope must complete in **≤ 5 s** and **≤ 250 MB** on a corpus of 3,000 captures; a regression past either is a build failure. It runs off the UI thread, holds only the read lock, and reports progress — so the 50 ms rule applies to the keystroke that starts it, not to the scan.

The fold itself is O(files touched): three atomic operations per loser plus one per group — 130 losers across all 44 groups is 273 file operations, comfortably inside the per-operation budgets 05 already meets.

**The whole-vault case, measured, and why it is not the default.** Running the same pipeline over all 70,357 `.md` files: 38 s parse + 64 s blocking + 137 s scoring = **238 s, 6.0 GB RSS, 2,029,779 candidate pairs**. That is 8× the 09 §4 pipeline budget and an out-of-memory risk on a laptop. Therefore: `scan_paths` defaults to the capture backlog; a user who widens it gets a **loud pre-flight estimate** (`organize dedup --path .` prints `scanning 70,357 notes; estimated 4 min and 6 GB — continue? [y/N]`) and, above `dedup.max_scan_notes` (10,000), a refusal naming the key. The estimate is computed from the measured per-note constants, not guessed.

---

## 7. Considered and rejected

| Rejected | Why, from the measurements |
|---|---|
| **Delete the duplicates** (the literal reading of "removing") | 05 §1.1, and the AST guard. The archive keeps the filename, so wikilinks survive; the fold is undoable; the backlog still shrinks. Nothing about the user-visible outcome requires a delete. |
| **IDF / document-frequency masking to beat the template problem** | Measured and it fails on exactly the case it was for. Masking shingles with df > 4 as boilerplate killed **219 of 265** pairs at raw Jaccard ≥ 0.8 (and 194 of 238 at ≥ 0.9) — including the genuine 20-note ntfy cluster, whose rare-shingle Jaccard collapses to **0.09** because a duplicate cluster big enough *is its own boilerplate*. Blocking on rare shingles also cut candidates from 14,492 to 664 and lost real groups. Novelty on the symmetric difference (§2.3) separates the same cases with no self-suppression. |
| **A time window as a filter** | 20% of measured duplicate pairs are > 7 days apart; the max is 180 days. A window tight enough to describe "quickly after each other" throws away a fifth of the real duplicates. Kept as an optional, default-off gate on D4 only. |
| **Merging near-duplicate bodies into the keeper automatically** | 20 of 130 losers hold content the keeper lacks; automatic body-merging would rewrite the keeper 130 times to salvage 20 cases, and 19 §2 already owns a reviewed merge that does it properly. The gate flags those 20 so Matt can `m` them out and merge them by hand. |
| **Folding `.sync-conflict-*` files** | Measured: 2 in the raw backlog, 0 in any duplicate group. They are a *sync* artifact whose two sides can legitimately both contain edits; conflating them with capture dedup would put a resolution decision behind a similarity threshold. `organize health` names them; that is the whole feature. |
| **An `--all` flag / a `dedup.auto = true` config** | Matt's sentence contains the word "review". A config key that skips it is a key whose wrong value quietly empties a third of the backlog into the archive, which 19 §2.4 already ruled is not a knob. `--all-identical` (byte-identical bodies only, no unique-content flags — 30 of 44 groups) is the fastest safe path and requires a human to type it. |
| **A new `dedup` `Operation` value in the corpus** | It would bump `ACTIONS_SCHEMA_VERSION` a second time (17 already takes it to 2) and would need bespoke undo. Spelling the fold as `meta_edit` + `archive` costs nothing in fidelity — `context.dedup_group` correlates them — and inherits 17's fully-undoable rows. |
| **MinHash + LSH** | 128-permutation MinHash costs 10.9 s where the bottom-k sketch costs 0.05 s for **identical recall (566/566)** on this corpus. LSH earns its cost at millions of documents, not at 2,445. |
| **Running dedup automatically after ingest** | Out of scope here and probably wrong: the cheapest fix for the dominant cause (an ingest writing the same `capture_id` 13 times) is in the ingest, not in a cleanup pass. §9 raises it. |

---

## 8. Required edits to existing documents

A code change without these forks the law.

| Doc | Edit |
|---|---|
| **02** frontmatter contract | `processing_status` gains the value `duplicate` (terminal, excluded from the raw backlog); the `duplicate_of` marker family is documented as machine-owned bookkeeping alongside `auto_tag_hash`. |
| **05 §1** | A sentence recording that dedup introduces no new removal call site: a fold is `update_frontmatter` + `archive_capture` + `update_frontmatter`, and the sanctioned-deleter set is unchanged. |
| **12 §2** | `ActionContext` gains `dedup_group: str \| None` (trailing, defaulted, omitted when empty, fail-closed). No schema-version change. |
| **12 §4 / learn** | `record_action` skips any record with `context.dedup_group` set, beside the existing `partial_failure` and (17) `undoes` guards. |
| **17 §6** | `organize undo --dedup-group ID` / `op.undo(dedup_group=…)`, batch, precondition-complete, all-or-nothing. |
| **10 §2** | `dedup.scan` (read-only) and `op.dedup_fold` (mutating, index-changing) added to the method inventory. |
| **03 §2** | Session queues exclude `processing_status: duplicate` by default. |
| **09 §4** | The dedup scan budget (≤ 5 s / ≤ 250 MB for 3,000 captures) added to the performance table. |

---

## 9. Acceptance criteria

1. **The census reproduces.** `organize dedup --json` over a working copy of the golden mirror reports **44 groups / 174 notes / 566 pairs**, with the D1–D4 breakdown 59 / 172 / 86 / 249 and the group-size histogram of §2.5. A change to any threshold changes these numbers; the test asserts the numbers, not that the list is non-empty.
2. **Rejection is byte-perfect.** Scan, reject every group, exit: a sha256 manifest of every file in the working copy is **identical** to the pre-run manifest, mtimes included; the operation log has zero new lines; the corpus has zero new records.
3. **Abort mid-review is byte-perfect.** Same assertion after SIGINT during the interactive walk, and after `q` in the nvim view.
4. **A fold folds exactly what was accepted.** Accept one 8-note group with one member `m`-kept: 6 notes are archived under their **original filenames**, 1 stays in the backlog untouched, the keeper is at its original path with `tags`/`sources` unioned and `duplicates_folded` listing 6 paths, and 13 `ActionRecord`s exist, all carrying the same `dedup_group`.
5. **The keeper's body is byte-identical after the fold.** Only its frontmatter changed, and only the keys `absorb` + `duplicates_folded` + `last_edited_date`.
6. **Undo restores the group.** `organize undo --dedup-group ID` leaves every file in the group byte-identical to its pre-fold state (sha256 manifest), the archive copies gone, `processing_status` back to `raw`, and no marker key anywhere.
7. **Changed-since-scan refuses.** Append one byte to a keeper between `dedup.scan` and `op.dedup_fold`: exit 1, stderr's first line matches `^ConcurrentModificationError: `, **nothing** in the plan was written, no record, no oplog line.
8. **Idempotence.** Re-running scan+fold immediately after a fold proposes **zero** groups (the markers exclude the folded notes and the archive is out of scope), and folds nothing.
9. **The template is not swept.** The kaizen family: the 14 byte-identical blank templates group; a filled kaizen capture with ≥ 7 novel words is **not** in any group; the 2 borderline fills appear with `unique_content: true` and are excluded from `--all-identical`.
10. **`no-ai` refuses.** A group containing a `no-ai: true` note folds when the actor is human and refuses with the existing `NoAiRefusal` shape when it is not.
11. **Budget.** The default-scope scan completes in ≤ 5 s and ≤ 250 MB on the 2,445-capture corpus (measured 1.27 s / 108 MB), and `organize dedup --path .` refuses above `max_scan_notes` naming the key.
12. **Learning is untouched.** `learning.json` is byte-identical before and after a fold of 12 captures.

## 10. Test obligations (anti-vacuity)

Binding on the seat that builds this and on its verifier. These are gates.

- **Never-delete guard, extended and mutation-audited.** `test_only_atomic_write_and_archive_may_remove_a_path` today parses `inspect.getsource(fileops)` **only** — a new `dedup.py` would be entirely unguarded, which is the single most dangerous fact in this document. The guard must be widened to every module under `src/organize_core/` with the same `allowed` set, and the mutation audit is: in a scratch copy, add `path.unlink()` to a dedup function, to a CLI handler, and to a server handler — **each must fail the guard**. Additionally assert positively that `allowed` is still exactly `{"atomic_write", "_archive_file", "_discard_unverified_copy"}` after this feature lands: if the fold needs a relocator it **reuses** `_archive_file`, it does not become a fourth deleter.
- **A rejected group leaves every file byte-identical.** Not "the files still exist" — a sha256 + `st_mtime_ns` manifest of the entire working-copy vault, compared whole. Same for abort, for `--dry-run`, and for a scan with no `--apply`. Assert also that no temp files (`find_orphaned_temp_files`) remain.
- **Every refusal predicate gets a guard-deleted pin.** For each of: `enabled = false`, `max_group_size` exceeded, `max_scan_notes` exceeded, hash mismatch at commit, unknown group id, keeper not in its group, path in both `fold` and `keep`, `no-ai` with a machine actor, already-marked note re-scanned — delete or invert that one guard in a scratch copy and the corresponding test **must fail**. Enumerate them in the test module docstring so a reviewer can count rows against tests.
- **Mutation audit of the thresholds.** For `similarity`, `novel_token_budget`, `min_body_tokens`, `max_group_size`, `window_hours`: mutate always-true, always-false, and off-by-one (`>=` → `>`), and record which test caught which. Any mutation caught by nothing is an untested predicate and blocks handback. The off-by-one on `novel_token_budget` must be caught by a *named real-corpus case*, not a synthetic one — the kaizen borderline pair is the fixture.
- **Constant assertions are literals.** Assert `0.80`, `6`, `5`, `25`, `"longest_then_earliest"`, `"duplicate_of"`, `"duplicate"` — never the imported config default, which makes the test agree with any future value of itself.
- **Both-ways seam pin.** The similarity/novelty computation used by `dedup.scan`'s report and by `op.dedup_fold`'s re-validation must be **one function**, table-driven-tested from both entry points with identical results. A gate that shows one number and folds on another is how a review gate starts lying.
- **Must-not-be-connected invariants.** Assert positively that: `dedup.scan` is **not** in `MUTATING_METHODS`; `op.dedup_fold` **is**, and is in `INDEX_CHANGING_METHODS`; the review path issues **no** mutating RPC; `learn.record_action` returns `None` for every dedup record; `LLM_EDIT_ACTORS` is still exactly `{"claude-integrate"}`; `ACTIONS_SCHEMA_VERSION` is unchanged **by this doc**.
- **Three-list agreement.** A test that `SUBCOMMANDS`, the parser's subparsers, and `_HANDLERS` contain the same names — the gate 17 §6 observed is missing, which is why `purged` is in two of three.
- **Real-data test, house pattern.** rsync the golden mirror to a disposable working copy, `chmod u+w`, point `ORGANIZE_CORE_CONFIG_DIR` / `ORGANIZE_CORE_STATE_DIR` / `ORGANIZE_CORE_RUNTIME_DIR` at it, generate config via `organize health --example-config`, sed the vault root. Marked `-m slow`. **A test that touches `~/Obsidian/Main` or `~/notes` is a build failure**; assert it by pointing the config's vault root at the working copy and asserting the real paths' mtimes are unchanged.
- **Torn-write and crash tests.** Kill between marking a loser and archiving it: the loser is marked but present, the next scan **excludes** it (marker present) and the next fold does not double-apply — assert the state is detectable and that `organize health` reports "N notes marked duplicate but not archived" rather than silently healing.
- **Property test on grouping.** Over randomized synthetic corpora: grouping is deterministic under input permutation, the keeper choice is a total order (no ties resolved by dict iteration), and every member's reported similarity is to the keeper.

## 11. Open questions — for Matt or the architect, not to be decided unilaterally

1. **Fix the cause, not just the symptom.** The dominant cause is an ingest writing the same `capture_id` up to 13 times seconds apart (§1.4). A one-line "refuse to write a capture whose `capture_id` already exists in the backlog" in the capture pipeline (06) would prevent most of what this command cleans up. Should that land alongside — and if it does, does the default `scan_paths` still need to be the whole backlog?
2. **The blank-template family.** 14 byte-identical unfilled kaizen templates are, strictly, duplicates of each other; folding them is defensible and so is keeping them as evidence of 14 planning sessions that did not happen. Matt's call — the gate shows them, so this is a question about the *default* ordering, not about safety.
3. **`processing_status: duplicate` vs a tag.** A new status value is a contract change (02) that touches every reader; a tag `duplicate/folded` would not. The status is proposed because it removes the note from the backlog automatically, which is the point. Confirm.
4. **Automatic dedup after ingest** (a consumer, 06) is deliberately out of scope: every fold currently carries a human actor, and 13's trust ladder is where an automated version belongs, if anywhere.
