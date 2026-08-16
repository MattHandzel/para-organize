# 04 — Suggestion Engine and Learning System

Reference implementation: `d753672~1` (`suggest.lua`, `learn.lua`); recovered copies staged by the exploration pass. Bug fixes to apply are marked ⚠; everything else is parity.

## 1. Candidate generation

For the current capture, candidates are **every immediate subfolder** of each PARA root except archives (`projects/*`, `areas/*`, `resources/*`). Each candidate carries `{path, name, normalized_name, type}` where `normalized_name` = lowercased, trimmed, spaces/underscores→hyphens (same normalizer used for tags — one shared `normalize_tag()`).

If `suggestions.always_show_archive` (default true), append a synthetic suggestion `{name="Archive Now", type="archives", score=0.1, reasons={"Safe default option"}, path=<archive capture dir>}`.

Result list: score-descending, truncated to `max_suggestions - 1` non-archive entries + the archive entry (≤ `max_suggestions` total, default 10 ⇒ up to 11 lines today — ⚠ off-by-one; make it exactly `max_suggestions` including archive).

## 2. Scoring — additive, per candidate

`calculate_score(capture, destination, folder_type) → (score, reasons[])`. Signals, all additive, weights from `suggestions.weights`:

| # | Signal | Default weight | Fires when |
|---|---|---|---|
| 1 | `exact_tag_match` | 2.0 **per matching tag** | a capture tag == candidate `name` or `normalized_name` |
| 2 | `normalized_tag_match` | 1.5 per tag | `normalized_tags[i]` == candidate `normalized_name` (normalization also applies `patterns.tag_normalization` map, e.g. `project`→`projects`) |
| 3 | `learned_association` | 1.8 × learned score | see §4 |
| 4 | `source_match` | 1.3 per source | `normalize_tag(source)` == candidate `normalized_name` |
| 5 | `alias_similarity` | 1.1 × similarity | similarity(alias, name) > 0.6, skipping aliases matching `^capture_` (and the capture_id alias); similarity = normalized Levenshtein `1 - dist/max_len` ∈ [0,1] ⚠ (HEAD returns raw distance — inverted and unbounded; fix and regression-test with exact numeric cases) |
| 6 | `context_match` | 1.0 | case-insensitive substring either direction between capture `context` and folder name |
| 7 | folder-type bonus | projects +0.3, areas +0.2, resources +0.1 | always ⚠ currently hardcoded — promote to config `suggestions.weights.type_bonus = {projects=0.3, areas=0.2, resources=0.1}` with these defaults |

Each fired signal contributes a human-readable reason string (shown in the UI, e.g. `Tag 'impro' matches folder`). Candidates with score > 0 survive; because the type bonus always fires, every folder is technically a candidate — ranking does the real work. ⚠ Apply `suggestions.learning.min_confidence` (0.3) as the documented floor: candidates below it are dropped (except the archive entry). Sort must be **stable** with a deterministic tiebreak (name ascending).

Signals deliberately NOT used (parity): note body text, modalities, location, folder note-counts, recency of folder. Don't add signals in the rewrite; fix the ones specced.

## 3. What learning records

On every successful accept/`move`/merge, `learn.record_move(capture, destination_path)`:

1. `features = extract_features(capture)` → `{tags, sources, has_context, modalities, word_count}` with tags/sources/modalities **sorted** (order-independent keys).
2. `association_key = "tags:a,b|sources:c|modalities:d"` — non-empty parts joined by `|`; all empty ⇒ literal `"generic"`.
3. Upsert `associations[key]` (`created_at`, `last_used`) and `associations[key].destinations[dest_path]` (`count`, `first_used`, `last_used`, `success_rate=1.0`); increment count.
4. `record_patterns`: for each tag, upsert `patterns["tag:<tag>->dest:<dest_path>"]`; for each source, `patterns["source:<src>->dest:<dest_path>"]` — each `{count, created_at, last_seen}`.
5. `statistics.total_moves += 1`; `statistics.destinations[dest_path] += 1`.
6. Apply decay (⚠ periodically — e.g. at most once per session or per day — not on every move as today: O(n log n) per accepted note).
7. Persist.

There is **no negative signal** (skip/reject records nothing) — parity; `adjust_scores_with_feedback` existed but was never called, do not carry it.

### Persistence format (`stdpath("data")/para-organize/learning.json`)

```jsonc
{
  "schema_version": 1,            // ⚠ new — add versioning
  "associations": {
    "tags:meeting,project|sources:email": {
      "created_at": 0, "last_used": 0,
      "destinations": {
        "/vault/projects/project-x": {"count": 3, "first_used": 0, "last_used": 0, "success_rate": 1.0}
      }
    }
  },
  "patterns": {
    "tag:project->dest:/vault/projects/project-x": {"count": 1, "created_at": 0, "last_seen": 0},
    "source:email->dest:/vault/projects/project-x": {"count": 1, "created_at": 0, "last_seen": 0}
  },
  "statistics": {"total_moves": 0, "destinations": {}, "last_updated": 0}
}
```

Written atomically. Live file is virgin (`total_moves: 0`) so no migration needed; still, loading malformed/legacy JSON must degrade to empty data, never crash scoring ⚠.

## 4. Learned score readback

`get_association_score(capture, dest_path)`:

```
key   = create_association_key(extract_features(capture))
d     = associations[key] && associations[key].destinations[dest_path]  (else contribute 0)
frequency_score      = log(1 + d.count) / log(1 + statistics.total_moves)   -- ⚠ guard total_moves==0 → 0 (today: NaN poisons all scores)
recency_multiplier   = learning.recency_decay ^ days_since(d.last_used)     -- 0.9^days
frequency_multiplier = d.count > 5 ? learning.frequency_boost : 1.0         -- 1.2
score = frequency_score * recency_multiplier * frequency_multiplier
      + 0.5 * pattern_score(capture, dest_path)
```

`pattern_score` = Σ over the capture's tag/source pattern keys matching `dest_path` of `pattern.count / 100`, capped at 1.0.

**Decay-curve parity note:** `0.9^days` means an association is worth 4% after 30 days; combined with the 90-day hard delete below, learning is effectively a ~3-week memory. This matches the original design and stays the default, but the rewrite should make the decay base a genuinely honored config (`suggestions.learning.recency_decay`) so Matt can flatten it (e.g. 0.99) — the vault's rhythms are monthly, not daily.

## 5. Decay / eviction

`apply_decay()`: delete associations with `last_used` and patterns with `last_seen` older than 90 days; if associations exceed `learning.max_history` (1000), evict oldest by `last_used`. Patterns are only bounded by the 90-day rule (parity). Statistics are NOT reset by decay (the learn_spec test asserting `total_moves==0` after decay contradicts the implementation; resolution: statistics persist — they are lifetime counters, and zeroing them would break `frequency_score` denominators).

## 6. Public API (parity)

`load_data() / save_data()` (load at setup, not require-time ⚠), `record_move(capture, dest)`, `extract_features(capture)`, `create_association_key(features)`, `get_association_score(capture, dest)`, `get_pattern_score(capture, dest)`, `apply_decay()`, `get_statistics()`, `get_top_destinations(n)` → `[{path, count}]` ordered by count, `clear()`, `export()` / `import(data)` round-trip (import returns boolean).

`analyze_patterns(captures)` → `{common_tags=[{tag,count}…], common_sources=[{source,count}…]}` sorted by count desc, and `suggest_new_folders(captures)` → folder-name proposals for tags appearing in ≥3 unorganized captures with `confidence = count/#captures` (both restored from `d753672~1`; the UI may surface `suggest_new_folders` in the new-folder prompt as today's code intended).

## 7. Acceptance tests (from learn_spec/suggest_spec, corrected)

- Each of the 7 signals produces >0 in isolation with the documented weight (exact numeric assertions, not just >0).
- Two identical candidates differing only in PARA type rank projects > areas > resources.
- `record_move` twice to the same destination: `statistics.total_moves==2`, `destinations[dest]==2`, pattern counts==2.
- Unknown destination association score == 0 exactly; known > 0; 30-day-old < fresh; count 6 > count 1 (frequency boost).
- `apply_decay` removes a 100-day-old association; statistics unchanged.
- `total_moves==0` ⇒ association score contributes exactly 0 to every candidate (NaN regression test).
- similarity("health","health")==1.0; similarity("health","zzzzzzzzzz")≈0; alias signal never exceeds `alias_similarity` weight.
- Suggestions list ≤ `max_suggestions` including the archive entry; archive entry always last-or-ranked but always present when configured.
