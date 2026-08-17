# 21 — Destination Recall: Nested Folders and Note-Level Merge Targets (NEW)

Directive from Matt (2026-08-16), verbatim:

> *"i have this general thing where there are tags (for example, eduardo-pontes-reis as a tag) and there is a suggested file for eduardo, but that is not suggested. please fix this and other things like this"*

**The assignment is the last clause.** `eduardo-pontes-reis` is one instance; *"and other things like this"* is the class. This doc fixes the class, and §1 measures how big the class is on Matt's real vault before proposing anything.

**What this doc changes: WHICH destinations are on the ballot. Nothing else.** It adds no signal, changes no weight, touches no normalizer, and does not relax the `min_confidence` floor. Those are spec 04 §2 law with numeric parity obligations and a standing architect ruling behind them; §4 states the pinning that proves they were left alone. Every scoring rule below is 04 §2's arithmetic, unmodified, applied to a candidate that 04 §1 never put on the ballot.

Doc 05 §4 owns merge semantics, **doc 19 owns the interactive merge flow and its save/commit semantics**, doc 17 owns merge undo. This doc says only *how a note gets ranked and marked as a merge target*; what happens when Matt presses `<CR>` on one is doc 19's, cited, never respecified.

---

## 1. The defect, with the real numbers

### 1.1 What is on the ballot today

`VaultIndex.para_subfolders` (`index.py:529-555`) does a single non-recursive `root.iterdir()` per PARA root. There is no depth knob — the depth is structural. `suggest.generate_candidates` (`suggest.py:364-386`) is typed `dict[str, list[str]]` of **folder** paths and builds one `Candidate` per folder; **no code path anywhere makes a NOTE a candidate.**

Measured on the golden mirror (`vault-mirror-golden`, checksum-verified against `~/Obsidian/Main`, 70,357 notes; index = 13,362 notes):

| | Count | On the ballot today |
|---|---:|---|
| Candidates handed to `calculate_score` | **135** | yes — all of them |
| Candidate depth histogram (parts relative to vault root) | `{2: 135}` | every candidate is exactly `<para_root>/<one_name>` |
| Folders under the non-archive PARA roots, all depths | **1,562** | 136 at depth 1; **1,426 unreachable at any setting** |
| …by depth below the PARA root | 1→136, 2→217, 3→824, 4→139, 5→144, 6→70, 7→19, 8→13 | only the first row |
| Notes under the non-archive PARA roots | **7,834** (direct walk) / 8,077 as indexed | **zero** |
| Distinct note match keys (stem + aliases + `id`) | 7,422 | zero |
| …of which are relationship notes (people) | 910 | zero |

(The 136-vs-135 gap is real and accounted for: a direct walk finds 136 folders at depth 1, `vault.ignore_patterns` removes one, and the accessor hands the ranker 135. §7.7 asserts that the gap stays attributable to `ignore_patterns` and nothing else.)

**673 of the 824 depth-3 folders are `areas/relationships/relationship-data/<person>`.** The eduardo case is not an edge case; the folder shape it lives in is the single largest folder population in the vault, and every member of it is unreachable.

Corpus note, so no number here is mistaken for another: `capture/raw_capture` holds **2,445 files**, of which **1,860 carry `processing_status`** and **1,858** formed the stress-test backlog (2 have unparseable YAML). §1.2 uses 2,445 (every capture file) and labels it; the SQ-1 history in §5 uses 1,858 because that is what was measured then. Doc 16 §5's "1,862-capture backlog" is the live install on the evening of cutover. All three are the same pile counted at three moments.

### 1.2 What it costs — the reproduction, then the class

**The reproduction.** `capture/raw_capture/2025-08-19T09:47:51.213957+00:00.md` carries `sources: [mind, eduardo-pontes-reis]`, `tags: [growth, things-i-enjoy]`. The token is a **source**, so 04 §2 signal #4 (weight 1.3) should fire.

```
$ organize suggest .../capture/raw_capture/2025-08-19T09:47:51.213957+00:00.md
1	0.10	archive	archive/capture/raw_capture	Archive Now	Safe default option
```

That is the entire ranked output. `areas/relationships/eduardo-pontes-reis.md` exists. `areas/relationships/relationship-data/eduardo-pontes-reis/` exists. `areas/relationships` exists. None is offered at any rank or any score.

**It is the candidate set and nothing else.** At `max_suggestions = 500` and `min_confidence = 0.0` the output is **byte-identical**. Direct probe: `[c for c in candidates if "eduardo" in c.name.lower()] == []`; `[c for c in candidates if "relationship-data" in c.path] == []`; of all 135 candidates, **0 have `signal_score > 0`** for this capture. Signals #1/#2/#4 are equality against a `Candidate`; a candidate that does not exist cannot be compared to.

**The counterfactual — same weights, same floor, same `suggest()`, only the candidate list widened:**

```
--- BASE (today) ---
  1 0.10 archive archive/capture/raw_capture                                  ('Safe default option',)
--- BASE + deeper folders ---
  1 1.50 area    areas/relationships/relationship-data/eduardo-pontes-reis    ("Source 'eduardo-pontes-reis' matches",)
--- BASE + deeper folders + PARA notes ---
  1 1.70 area    areas/systems/growth-system.md               ("Tag 'growth' ~ folder 'growth-system'",)
  2 1.50 area    areas/relationships/eduardo-pontes-reis.md   ("Source 'eduardo-pontes-reis' matches",)
  3 1.50 area    areas/relationships/relationship-data/eduardo-pontes-reis
  5 1.50 area    areas/personal-brand/life-inventory/mind.md  ("Source 'mind' matches",)
```

`1.50 = 1.3 (source_match) + 0.2 (areas type bonus)` — spec 04 §2's arithmetic, unmodified. **No weight change fixes this and none is attempted.**

**The class, quantified over the whole real backlog** (2,445 captures; destination notes restricted to notes under a PARA root; generic tokens `{mind, self, me, text, voice, note, thought}` excluded so `mind` cannot inflate the note-hit rate):

| Measure | Captures | % |
|---|---:|---:|
| A non-generic tag/source exactly names a **NOTE** under a PARA root | 1,557 | 63.7% |
| …**note hit with NO folder hit** — the gap this doc closes | **1,157** | **47.3%** |
| A non-generic tag/source names a folder that exists but is **too deep** | 1,071 | 43.8% |
| Hits a depth-1 folder (scorable today) | 400 | 16.4% |
| Names an existing **relationship note (a person)** with no folder hit — the eduardo class exactly | 61 | 2.5% |
| No non-generic tag/source at all | 178 | 7.3% |
| No hit of any kind | 710 | 29.0% |

**What the shipping ranker returns today over that backlog:** 0 suggestions for **1,877 captures (76.8%)** — archive-only; 1 for 351; 2 for 160; 3 for 51; ≥4 for 6.

**Widened candidate set, weights and floor untouched:** captures with ≥1 real suggestion goes **568/2,445 (23.2%) → 1,740/2,445 (71.2%)**.

### 1.3 Three findings, and which is the cause

1. **P0 — folder enumeration is structurally depth-1.** 1,426 of 1,562 folders unreachable. Fixed by §2.
2. **P0 — suggestions rank folders only.** 7,834 notes structurally unrankable, although `fileops.merge_into_note` (`fileops.py:1714`) is fully implemented, `actions.py:106` already carries the `merge` action and `actions.py:124` the `merge_target` role. **The plumbing exists; only the suggester never proposes it.** Fixed by §3.
3. **P1 — the silence is total, and it is not the floor's fault.** With 135 shallow folders most captures fire zero signals, and the `signal_score <= 0.0` guard (`suggest.py:443`) then correctly drops everything, leaving the synthetic archive row. **That gate is the 2026-08-16 architect ruling that killed the SQ-1 regression and it is not touched** (§4). It is a symptom amplifier, not the cause: it converts a starved ballot into an honest "no idea" when the truth is "the right destination was never on the ballot." This doc makes it fire less often by fixing its input, never by relaxing it.

### 1.4 The amendment (P2 — the law encodes the defect)

Spec 04 §1 line 7 reads, today:

> "For the current capture, candidates are **every immediate subfolder** of each PARA root except archives (`projects/*`, `areas/*`, `resources/*`)."

`index.py` and `suggest.py` are faithful implementations of that sentence. **This is therefore not a code bug and must not be shipped as one** — a code fix without the amendment silently forks the law, and the parity/golden tests are written against a 135-candidate world. Spec 04 §1's candidate definition is amended to:

> For the current capture, candidates are: (a) **every folder** under each non-archive PARA root, from depth 1 down to `suggestions.max_candidate_depth` levels below that root, honoring `vault.ignore_patterns` and skipping dot-directories; **and** (b) when `suggestions.note_candidates` is true, **every indexed note** whose `para_type` is a non-archive PARA type. Each candidate carries `{path, name, normalized_name, type, kind}` where `kind ∈ {"folder", "note"}` and `normalized_name` comes from the one shared `normalize_tag()`. Scoring (04 §2) is identical for both kinds.

Everything after that sentence in 04 §1 — the synthetic archive entry, the `max_suggestions` total — is unchanged.

---

## 2. Folder candidate enumeration

### 2.1 The rule

A **candidate folder** is a directory `D` such that: `D` is under a PARA root whose `para_folders` key is not `archives`; `1 ≤ depth(D) ≤ suggestions.max_candidate_depth`, where **depth is levels below the PARA root and an immediate subfolder is depth 1**; no segment of `D` relative to the vault root starts with `.`; and `D` is not matched by `vault.ignore_patterns`. Nothing else qualifies — not the PARA root itself, not `.backups`, not the vault root (05 §9's refusal list is upstream of this and stays).

`suggestions.max_candidate_depth` — **core** config (it changes a score, so the CLI and the UI must agree; 14 §4.1). Integer ≥ 1, or the string `"all"`. **Default 3.**

Why 3, in the vault's own numbers: depth ≤3 admits **1,177 of 1,562** folders (75%) and is exactly what reaches `areas/relationships/relationship-data/<person>` — the 673-folder population that is the reported bug. The 385 folders beyond it are build outputs and dated sub-sub-folders (`projects/date-me-doc-games/dist`, `projects/inactive-projects/startup/*`), which are noise as *destinations*. `1` reproduces today's behaviour exactly and is the escape hatch for anyone who dislikes the change; `"all"` is one keystroke away for a deeper vault. The default is a default, not law (14 §1).

### 2.2 Candidate identity — what `name` means

`Candidate.name` stays the **basename**, never the relative path. Signals #1/#2/#4 compare a *tag* to `name`/`normalized_name`; a relative path would never equal a tag and the fix would enumerate 1,562 candidates that can never fire. `Candidate.path` (absolute) remains the identity — it is the learning key (04 §3-4) and it is already unique.

Basename collisions are therefore expected and are **not** deduplicated: `projects/a/dist` and `projects/b/dist` are two candidates with the same name and different paths. The existing sort key is already total (§3.3), so the list stays deterministic; two same-named folders that both fire will both be offered and the type bonus and the learned signal will separate them over time. Collapsing them would silently hide a real destination.

### 2.3 What this must NOT change

- **`VaultIndex.para_subfolders` keeps its depth-1 meaning, in place.** `server.py` and `cli.py` both call it, `fileops` documents its depth-1 semantics, and doc 16 §2 already gives `folder.list` an additive `depth` parameter for *browsing*. Add a separate accessor, `VaultIndex.candidate_folders(depth)`; do not redefine an existing one under its callers.
- **Browsing depth and ranking depth are two knobs, deliberately.** `folder.list`'s `depth` param (16 §2) is what Matt *browses*; `suggestions.max_candidate_depth` is what the ranker *scores*. Coupling them would mean widening a picker silently re-ranks his backlog. They may hold different values and nothing reconciles them.
- **The archives root is still never a candidate**, at any depth (`_EXCLUDED_CANDIDATE_TYPES`).

### 2.4 Cost

Enumeration is one `os.walk` per PARA root, performed **once per candidate-set build** and cached alongside the index — not per capture, and never inside `suggest()` (04's purity constraint: scoring does no I/O). The cache is invalidated by `folder.create` and `index.reindex`, which is the same invalidation hook doc 16 §2 already requires for `state.folders`; the two share it rather than each inventing one. Scoring cost is bounded separately, in §3.5.

---

## 3. Note-level destinations — the general fix

A capture tagged with a person, a project or a book usually belongs **in** an existing note, not next to it in a folder. 1,157 captures (47.3%) name a note and no folder. This section puts notes on the ballot.

### 3.1 Which notes are candidates, and on which keys they match

**Candidate notes** = every note in the index whose `para_type` is a non-archive PARA type (`project`/`area`/`resource`). Captures are never note candidates. Anything under the archives root, under `.backups`, or outside `vault.scan_dirs` is never a note candidate. A note is never a candidate for itself (identity guard on resolved path).

**Match keys for a note `N` — exactly these four, all built by *calling* `frontmatter.normalize_tag`, never by a second normalizer:**

| | Key | Notes |
|---|---|---|
| a | `normalize_tag(stem)` — filename without `.md` | the primary key; `eduardo-pontes-reis.md` → `eduardo-pontes-reis` |
| b | `normalize_tag(alias)` for each alias | **excluding** aliases starting `capture_` and excluding the note's own `capture_id` — the same exclusion signal #5 already applies (04 §2 #5) |
| c | `normalize_tag(id)` when `id` is set and is not timestamp-shaped | same timestamp exclusion as (b); Matt's `id:` values are what wikilinks resolve against |
| d | `normalize_tag(title)` — 04's title rule: first `# heading`, else stem | 08 §B10 stands: never `aliases[0]` of a string |

**Deliberately NOT keys: the note's body, its tags, its folder, its `sources`.** A note's tags describe what it is *about*; matching capture-tag against note-tag is a topical-similarity signal — an **eighth signal**, which 04 §2 forbids. Keys are *names* only. This is the line that keeps "notes are candidates" from becoming "notes are search results."

A **folder's** key set is `{name, normalized_name}` — which is precisely today's `tag == candidate.name or tag == candidate.normalized_name`. Stating both kinds as key sets is a refactor with byte-identical folder behaviour, not a change.

**A signal fires at most once per (capture token, candidate) pair.** It tests membership in the candidate's key set and contributes its weight **once**, even when two keys match (stem and an alias that normalize identically). This is exactly the semantics of today's `or` chain, generalised; without the rule, a note with redundant aliases would silently outscore an identical note without them. Pinned by test (§7.5).

### 3.2 How notes are scored relative to folders

**Identically. The same seven signals, the same weights, the same type bonus from the note's own PARA type, the same `min_confidence` floor on the signal score.** No note bonus. No note penalty.

This is a decision, not an omission. A kind-dependent score adjustment *is* an eighth signal wearing a hat, it would need parity evidence that does not exist, and it is unnecessary: the counterfactual shows the arithmetic already lands — `areas/relationships/eduardo-pontes-reis.md` scores 1.50 = 1.3 + 0.2, the same number its sibling folder scores, and the tiebreak decides between them. **Refused, on the record.**

**Tiebreak.** The sort key becomes `(-score, name, kind_rank, path)` with `kind_rank`: note = 0, folder = 1. This is a conservative extension: it can only reorder candidates that tie on **both** score and name, so for a folders-only list (two same-named folders) the `kind_rank` values are equal and the previous `path` tiebreak decides — byte-identical. Where it does fire it fires for the reported case: `areas/relationships/eduardo-pontes-reis.md` and `areas/relationships/relationship-data/eduardo-pontes-reis` tie at 1.50 with the same name, and Matt asked for **the file**. A note is the more specific destination and merge is backed up and undoable (17 §1), so preferring it on an exact tie is the cheaper mistake.

**Same-name collapse.** Among *note* candidates that share a `normalized_name` **and** an equal score, keep the shallowest path (fewest segments; tiebreak path ascending) and drop the rest, counting the drops in `--json` as `suppressed_duplicates`. `eduardo-pontes-reis` resolves to two real files — `areas/relationships/eduardo-pontes-reis.md` and `areas/relationships/relationship-data/eduardo-pontes-reis/eduardo-pontes-reis.md` — and three spellings of one answer in a ten-row list is the SQ-1 failure mode in miniature. Folders are **not** collapsed (§2.2): two same-named folders are two destinations, two same-named notes at the same score are one answer written twice.

### 3.3 How the UI marks a note, and how many appear

- **`Suggestion` gains `destination_kind: "folder" | "note"`** — an additive wire field beside the existing singular `type`. The two are orthogonal: a note under `areas/` is `type = "area"`, `destination_kind = "note"`.
- **`destination_kind` is NEVER inferred from a trailing `.md`.** A folder may legally be named `foo.md`, and doc 14's stranger is exactly the person whose vault contains one. The core states the kind; the client reads it. Pinned by test (§7.8).
- **Row rendering.** A note suggestion renders with `ui.icons.merge_target` (**nvim** config — purely how a row is drawn, 14 §4.1), default `[M]`, distinct from `[P]`/`[A]`/`[R]` and from browse's `[F]`. The containing folder is shown dim after the name, so `[M] eduardo-pontes-reis.md  ┊ areas/relationships  1.50` and its `relationship-data/` sibling are one glance apart. Scores, reasons, and `ui.organize.render_row` behave exactly as 15 §176 already specifies; nothing else about the suggestion row changes.
- **Selecting one starts the merge flow of doc 19** — not a move. Dispatch is on `item.kind`, never on rendered text (15 §184); no client may string-match the row. `<CR>`, a hint terminal label (16 §1), and `.` repeat (16 §3.1, which re-enters the review gate for merges) all route the same way. Doc 05 §4 owns what is written; doc 17 §1 owns the undo rule (restore-or-refuse against `after_hash`, and **not undoable at all** with `file_ops.create_backups = false`). None of that is restated here.
- **How many.** `suggestions.max_note_suggestions` — **core**, default **3** — caps note rows in the final list *after* ranking; folders fill the remainder and `max_suggestions` (total, archive row included — 04 §1) is unchanged. Why a cap: 1,150 of the 1,740 newly covered captures have a note at rank 1, and a person or project cluster can otherwise fill the visible list with one answer's neighbourhood. Set it to `max_suggestions` to disable the cap.

### 3.4 Generic tokens, and the honesty gate on the default list

Measured: **195 of the 1,740** newly covered captures get a rank-1 driven *solely* by a generic token — `source: mind` reaching `areas/personal-brand/life-inventory/mind.md`. Shipping without a guard trades one wrong answer for a different wrong answer.

`suggestions.candidate_stopwords` — **core**, default `["mind", "self", "me", "text", "voice", "note", "thought"]` — is applied to the **capture-side token** (tag, normalized tag, source, alias) before matching, **uniformly for both kinds**. A stopword is a property of the token, not of the destination kind; a rule that applied only to notes would make the same token mean two things.

**This is a match-time filter in `suggest.py`, in the SQ-4 pattern. `frontmatter.normalize_tag` is not touched** (§4).

**Evidence gate on the default value, because uniform application can perturb folder-only ranking:** the default list ships only with the measured proof that **folder-only rank-1 is unchanged for all 2,445 captures**. Any token that changes a folder-only rank-1 is **removed from the default list** and recorded in the evidence file with its capture count and the destination it moved. A default that cannot pass this ships empty, and the tokens are then handled by the `max_note_suggestions` cap alone. Falsifiable before merge, not argued in review.

### 3.5 Cost, the budget, and the stated fallback

Measured, same weights, same floor: **135 candidates → 1.30 ms per `suggest()`; 9,638 candidates → 88.7 ms (68×).** At a 1,862-capture backlog with per-keystroke re-ranking that is felt in the UI, and it must be fixed **before** shipping, not after.

Required design — all of it output-preserving:

| Term | Today | Required |
|---|---|---|
| Signals #1, #2, #4 | linear scan of every candidate, string equality | served from an **inverted index** `{normalized key → [candidate]}` built once per candidate-set build and cached with it. O(#capture tokens), not O(#candidates). |
| Signal #2 variants (SQ-4) | derived per candidate | derive the variant set from the **capture** token once, then look each variant up in the same inverted index. Identical results; the variant rules are unchanged. |
| Signal #3 | dict lookup on `candidate.path` | unchanged |
| Signal #5 (Levenshtein per alias per candidate) | the actual quadratic term | **exact** length prefilter: normalized similarity is `1 - dist/max_len`, and `dist ≥ |len(a) - len(b)|`, so `|len(a) - len(b)| / max_len ≥ 0.4` ⇒ similarity `≤ 0.6` ⇒ cannot pass the 0.6 gate. Skipping those pairs is arithmetically incapable of changing a score. |
| Signal #6 (token run) | per candidate | first-token dictionary prefilter over candidate names; only candidates sharing a token are tested |
| Signal #7 | constant | unchanged |

**Budget: `suggest()` p95 ≤ 5 ms at the shipped defaults on the 13,252-note index**, measured on the warm-server path; and the existing 10k-note perf gate stays inside its 5 s budget.

**Stated fallback, so the fix cannot be quietly abandoned if the budget is missed:** `max_candidate_depth` default drops to 2 and note candidates are restricted to notes whose containing folder is at depth ≤ 2. That still covers the depth-2 folder population (217) and **5,668 of the 7,834 notes** (folders at 1, 2 and 3 parts relative to the vault root hold 311 + 2,994 + 2,363 notes), and it still fixes `areas/relationships/eduardo-pontes-reis.md` — which is at depth 1 under `areas/`. It does **not** fix `relationship-data/<person>/`, and that loss is recorded in the evidence file rather than discovered later.

Because every item in the table is output-preserving, **the numeric goldens are the proof** that it is (§5.2, §7.4).

### 3.6 The off switch

`suggestions.note_candidates` — **core**, default `true`. `false` restores exactly today's folders-only ballot, and combined with `max_candidate_depth = 1` reproduces the pre-change output byte for byte. That combination is not a courtesy; it is the parity golden of §7.4.

### 3.7 Config summary (14 §4.1, §4.3)

| Key | Home | Default | Why that side |
|---|---|---|---|
| `suggestions.max_candidate_depth` | **core** | `3` | changes a score — CLI and UI must agree |
| `suggestions.note_candidates` | **core** | `true` | changes what is offered and which operation an accept performs |
| `suggestions.max_note_suggestions` | **core** | `3` | changes the returned list, not its rendering |
| `suggestions.candidate_stopwords` | **core** | `["mind","self","me","text","voice","note","thought"]` | changes a score |
| `ui.icons.merge_target` | **nvim** | `"[M]"` | purely which glyph a row is drawn with; the core has no view of it |

All five carry the full 14 §4.3 obligation — default, schema key, one documented sentence in the example config + README + `:help`, and a test that a non-default value changes observable behaviour. **None may be added to `RESERVED_CONFIG_LEAVES`**; a documented key with no reader is a defect (03 §1's honored-or-deleted law).

---

## 4. The parity guard — what is untouched, and how that is proven

This is the SQ-4 pattern: the constraint is **structurally pinned**, not merely promised in prose.

**Untouched, exhaustively:**

1. **The seven signals and their exact weights** — `exact_tag_match 2.0`, `normalized_tag_match 1.5`, `learned_association 1.8`, `source_match 1.3`, `alias_similarity 1.1`, `context_match 1.0`, `type_bonus {projects 0.3, areas 0.2, resources 0.1}`. **No eighth signal.** 04 §2 fixes the list at seven and forbids additions; the reference implementation's "filename similarity" fallback was dropped deliberately and stays dropped. This doc adds candidates, not signals — signal #4 already fires correctly the instant the candidate exists.
2. **`frontmatter.normalize_tag`** — not touched. It also builds learning association keys, the `<type>/<folder>` tag a move writes into frontmatter, and data on disk; morphing it would corrupt learning state and real notes (architect ruling, 2026-08-16). **Note match keys are built by calling it.** The existing pin `test_signal_2_variant_matching_never_touches_the_shared_normalizer` is extended to cover note-key derivation and the stopword filter.
3. **The `signal_score <= 0.0 or signal_score < min_confidence` gate and the type-bonus subtraction** (`suggest.py:442-443`) — the 2026-08-16 ruling that killed SQ-1. Unchanged in both value and position. Its three consequence tests stand: zero-signal ⇒ archive entry alone; every non-archive suggestion carries ≥1 reason; projects > areas > resources among survivors.
4. **`min_confidence = 0.3` and `max_suggestions = 10`** — unchanged, and directly disproven as causes: at `0.0` / `500` the eduardo output is byte-identical to the default.
5. **Signal #6's token granularity** and **signal #2's match-time morphological variants** (both SQ-4-era sanctioned deviations) — unchanged in rule and in weight, and applied to note keys exactly as they are applied to folder names. A note gets the same leniency a folder gets, no more and no less.
6. **Signals #1 and #2 double-firing on an already-normalized tag** — still rejected, still spec-literal, still pinned by the 3.8 / 3.7 / 3.6 numeric goldens.
7. **Learning keys.** `learn.record_move` keys on the destination **path string** (04 §3-4). A note destination is a path like any other and merge already records (04 §3: "every successful accept/`move`/merge"). No key format changes, and a note's key can never collide with its parent folder's key because the paths differ. `record_action` still returns `None` for a partially-applied operation (SQ/BS-2 ruling).

**Refused, on the record, so it is not re-litigated:** a note-vs-folder score bonus or penalty of any size (§3.2). It is an eighth signal in disguise; the measured arithmetic does not need it.

**If a future change to this design requires a new signal**, it must be **additive** (never a re-weighting of an existing one), **config-gated with a default that reproduces the previous behaviour**, and **measured on the real backlog before and after** with §5.3's full metric set. Anything less re-opens SQ-1.

---

## 5. Measurement — before and after, on real data

### 5.1 The harness (house pattern; the live vault is never written)

`~/Obsidian/Main` and `~/notes` are **never** touched, read-only inspection aside. Every run is a disposable rsync of the read-only golden mirror:

```
rsync -a --delete .../vault-mirror-golden/ $WORK/vault/ && chmod -R u+w $WORK/vault
organize health --example-config > $WORK/cfg/config.toml
sed -i 's|^root = "~/Obsidian/Main"|root = "'$WORK'/vault"|' $WORK/cfg/config.toml
ORGANIZE_CORE_CONFIG_DIR=$WORK/cfg ORGANIZE_CORE_STATE_DIR=$WORK/state \
ORGANIZE_CORE_RUNTIME_DIR=$WORK/runtime organize index --full
```

Every run reports, as part of its output: `git status` in `organize-rewrite` unchanged, and zero writable entries in the golden mirror. The sweep script lives in `tools/` and is re-runnable by anyone; its output is checked in as a dated evidence file the way `doc/FEEDBACK-EVIDENCE-2026-08-16.md` is.

### 5.2 Named regression cases — exact numbers, not membership

The eduardo capture becomes a permanent fixture case asserting the **exact** result: `areas/relationships/eduardo-pontes-reis.md` present, `destination_kind == "note"`, `type == "area"`, `score == 1.5` exactly, `reasons == ("Source 'eduardo-pontes-reis' matches",)`. The siblings `james-fang`, `jennifer-kesteloot`, `flor-laorga` (from the measured 61-capture person class) get the same treatment. Matt's complaint becomes an assertion, with its arithmetic visible: `1.3 source_match + 0.2 areas bonus`.

### 5.3 The backlog sweep — the anti-SQ-1 metric set

SQ-1's signature was **not** low coverage; it was *high* coverage made of garbage — 1,450 of 1,858 captures receiving an identical 9-row list, every row at the bare 0.30 type bonus with an empty reason list, rank 1 decided by an ASCII accident. Coverage alone would have called that a success. **Every metric below is reported before AND after, over all 2,445 captures:**

| Metric | Today | Gate |
|---|---:|---|
| Captures with ≥1 non-archive suggestion | 568 (23.2%) | **≥ 1,700 (≈70%)** |
| Archive-only rate | 1,877 (76.8%) | reported; the residual must be captures with genuinely no hit (measured floor: 710 no-hit + 178 no-token) |
| **Distinct rank-1 destinations** | reported | **no single destination is rank 1 for more than 5%** of captures that have suggestions; top-1 distribution entropy reported |
| **Largest identical-list cluster** (same paths, same order) | 1,450 at the SQ-1 peak | **no identical list shared by more than 25 captures** |
| **Suggestions with an empty reason list** | 0 | **exactly 0** — the 2026-08-16 invariant, unchanged |
| **Previous rank-1 survival** | n/a | for every capture that had ≥1 suggestion before, its previous rank-1 **still appears** in the after-list (it may be outranked; it may not vanish). Widening can only add candidates, so any disappearance is the per-kind cap or truncation and is reported per capture with a count. |
| Folder-only rank-1 unchanged under the stopword filter | n/a | **all 2,445** (§3.4's evidence gate) |
| Median / p95 list length; median top score | reported | reported before and after |
| Rank-1 driven **solely** by a stopword token | 195 (pre-filter) | **0** |
| Rank-1 is a note / is a folder | 0 / 590 | reported (counterfactual: 1,150 / 590) |

### 5.4 Performance

p50 and p95 of `suggest()` over the same 2,445 captures, before and after, **warm-server path and CLI path reported separately**. The CLI's known ~726 ms index deserialization (the declined per-op index cost) is excluded from the ranker measurement so it cannot mask a ranker regression. Budget per §3.5: p95 ≤ 5 ms warm.

### 5.5 The 30-capture human read

The stratified 30-capture sample is rendered as a table — capture tags/sources → before list → after list — and read by Matt. This exists because SQ-1 passed every aggregate it had. An aggregate cannot tell you the answers are meaningless; a person reading thirty of them can.

---

## 6. Acceptance criteria

1. `organize suggest` on `capture/raw_capture/2025-08-19T09:47:51.213957+00:00.md` returns `areas/relationships/eduardo-pontes-reis.md` with `destination_kind = "note"` and `score = 1.50`, above the archive row.
2. Accepting that row performs a **merge** via doc 19's flow into that note (05 §4 semantics, 17 §1 undo), not a move; the capture is archived exactly once.
3. `areas/relationships/relationship-data/<person>` folders are candidates at the default `max_candidate_depth = 3`, and 1,177 of the vault's 1,562 PARA folders are on the ballot (136 today).
4. Backlog coverage ≥ 70% of 2,445 captures with ≥1 non-archive suggestion (23.2% today), with **every** §5.3 gate met — coverage without the distribution gates is a failed run, not a partial success.
5. `suggestions.note_candidates = false` **and** `max_candidate_depth = 1` reproduce the pre-change output byte-for-byte on the 30-capture sample.
6. All seven spec-04 weights, `frontmatter.normalize_tag`, and the `min_confidence` floor are unchanged, proven structurally (§7.4, §7.10).
7. `suggest()` p95 ≤ 5 ms warm at shipped defaults on the 13,252-note index; the 10k-note perf gate stays under 5 s.
8. Spec 04 §1's candidate definition carries §1.4's amended wording, landed **in the same change** as the code.
9. Every one of the five new config keys is honored, documented in three places, and absent from `RESERVED_CONFIG_LEAVES`.
10. No suggestion anywhere in the sweep has an empty reason list.

---

## 7. Test obligations (anti-vacuity)

1. **Every new test must FAIL against the pre-change code**, and the failure output is recorded with the change. A test that passes both ways proves nothing and does not count toward any criterion above.
2. **The eduardo assertion is exact** — path, `destination_kind`, `type`, `score == 1.5`, and the exact `reasons` tuple. A membership-only assertion (`"eduardo" in [s.path for s in result]`) is **explicitly not acceptable**: it would also pass for an implementation that returned every note in the vault.
3. **The negative twin.** A capture whose tokens match no folder and no note under the widened set still returns **the archive entry alone**. This is SQ-1's guard in unit form and must fail loudly if anyone ever relaxes the floor to buy recall.
4. **The parity golden is captured BEFORE the change.** The 30-capture stratified sample is scored by today's implementation and checked in; after the change, `note_candidates = false` + `max_candidate_depth = 1` must reproduce it **byte-for-byte**. This is the structural pin that turns any weight, normalizer, floor, or sort perturbation into a test failure rather than a review argument.
5. **One fire per (token, candidate) per signal.** A note with three aliases that all normalize to its stem scores identically to a note with none. Sabotage check: making the key set iterate rather than test membership must break this test.
6. **Stopwords, both directions.** (a) A capture whose only token is `mind` does not get `.../life-inventory/mind.md` at rank 1. (b) A capture whose only token is a stopword that *exactly* names a real folder gets **archive-only** — the filter must not be replaced by a fabricated hit somewhere else.
7. **Depth is honored as a number.** `max_candidate_depth = 1, 2, 3, "all"` produce candidate sets of the measured sizes **136 / 353 / 1,177 / 1,562** on the mirror by direct walk — and of **135 / … / …** through the real accessor at depth 1, because `vault.ignore_patterns` removes exactly one depth-1 folder. Both numbers are asserted, and the *difference* between them is asserted to be attributable to `ignore_patterns` alone: a walk that silently stopped honoring the ignore list would otherwise look like a recall win. Off-by-one in the depth convention (levels below the PARA root, immediate child = 1) is the likeliest silent bug and is asserted directly, at each of the four settings.
8. **`destination_kind` is never inferred.** A fixture folder literally named `foo.md` is classified `"folder"`, and accepting it performs a **move**. A fixture note whose stem contains a dot is classified `"note"`. Both must fail if anyone reintroduces a `.endswith(".md")` test.
9. **Same-name collapse** asserted on the real eduardo pair: one row survives, it is the shallower path, and `suppressed_duplicates` reports 1. Asserted also *not* to fire for two same-named **folders**.
10. **Structural pins (the SQ-4 pattern).** (a) `frontmatter.normalize_tag`'s body is unchanged — AST/source pin; (b) note-key derivation *calls* it, proven by a test that patching it changes note keys; (c) `suggest.py` reads exactly the seven documented weights and no others; (d) the `signal_score` floor comparison and the type-bonus subtraction are asserted to exist in that order.
11. **The performance test runs against the real index size** (13,252 notes), not the fixture vault. A perf test on a fixture is how a 68× regression ships green.
12. **The sweep script is a test artifact, not a one-off.** It is checked in under `tools/`, re-runnable by anyone with the mirror, and its dated output is the evidence file cited by §5. A number in this doc that cannot be reproduced by running it is a defect in this doc.

---

## 8. Open questions — for Matt / the architect, not to be decided unilaterally

1. **`capture/google_keep` is absent from `vault.scan_dirs`** (`["capture/raw_capture", "resources", "areas", "projects"]`), so those captures are invisible to `suggest` entirely — including a real eduardo-mentioning capture at `capture/google_keep/2025-11-07T18-47-09.md`. Widening `scan_dirs` changes index size and pipeline scope, so it is **Matt's call which capture sources are in scope**. Flagged only; nothing here changes it. Note that every coverage figure in §1 and §5 is computed over `capture/raw_capture` and therefore *understates* the real backlog.
2. **`max_candidate_depth` default: 3 or `"all"`?** §2.1 argues 3 on the measured folder population. Once §3.5's inverted index lands and the p95 is known at `"all"` (1,562 folders + 7,834 notes), the default is worth revisiting with that number in hand rather than with today's 88.7 ms linear-scan figure.
3. **Should a route destination (11 §1) that is a file suppress the scored note suggestion for the same path?** Routes already outrank scored suggestions and render distinctly; showing the same destination twice, once as a route and once at 1.50, is redundant. Cheap to add, but it is a doc-11 rendering decision and belongs to whoever owns that surface.
