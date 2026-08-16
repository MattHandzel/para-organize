# 12 — Edit Modes and Total Action Recording (NEW)

Directive from Matt (2026-08-15): *"I should have a mode for when Claude edits the file to add my thing (with as close to my original wording and the style of the file as possible), I edit the file myself, etc. … Claude should be able to learn how I take captures and add them to projects/edit files, by explicitly storing the state/diffs when I take captures and add them places. I may be adding them to multiple files. Store as much as possible about the state of the system when doing every action so eventually AI can learn how to do it."*

Two halves: (1) integration **edit modes**, (2) an append-only **action record** rich enough to train future automation.

## 1. Edit modes

Every operation that puts capture content into an existing file runs in one of three modes (per-invocation choice in the UI; per-route default via `mode` in doc 11; global default `manual`):

| Mode | Who edits | Behavior |
|---|---|---|
| `manual` | Matt | The doc 03 §5 merge editor / his own hand-editing. |
| `append` | machine, mechanical | Doc 11 append template. Deterministic, no LLM. |
| `integrate` | Claude | LLM rewrites the target to incorporate the capture. |

### `integrate` contract

- Inputs to the LLM: full target file, capture content + frontmatter, the destination's natural-language description (11), and the style directive: **preserve Matt's original wording from the capture as closely as possible, and match the target file's existing structure/voice/formatting conventions** — place content in the right section, don't paraphrase, don't editorialize, don't reformat untouched parts.
- Output: complete new target content. The core diffs it against the original and **hard-rejects** any result that deletes existing non-whitespace lines beyond a configurable threshold (default: zero deletions allowed outside the edited region) — integration adds and weaves; it never destroys.
- **Review gate:** default `review = "diff"` — the nvim client shows the unified diff in the right pane; `<CR>`/`<leader>mc` applies, `e` opens the proposed result for hand-editing before applying (edits captured — see §2), `<leader>mx` rejects. `review = "auto"` (per-route opt-in) applies without asking. In every case the target is backed up first and the write is atomic (doc 05); `no-ai: true` targets refuse `integrate` outright.
- Backend: the shared LLM client (10), `backend = "ollama" | "claude-cli"`; integrations are the quality-sensitive path, so default this one to `claude-cli` when available.

## 2. Action recording — store everything

**Every** state-changing operation appends one `ActionRecord` to `~/.local/share/organize-core/actions/YYYY-MM.jsonl` (append-only, atomic appends, never rewritten; `organize actions export` concatenates/filters). This includes actions taken *outside* sessions (CLI, consumers, routes). Recording failures must not block the operation — log and continue — but must be loud.

```jsonc
{
  "schema_version": 1,
  "id": "act_<ulid>",
  "ts": "2026-08-15T17:40:00Z",
  "actor": "matt" | "claude-integrate" | "route:<name>" | "consumer:<name>" | "auto-organize",
  "operation": "move|merge|append|integrate|archive|skip|meta_edit|create_folder|tag_edit",
  "edit_mode": "manual|append|integrate" ,        // when applicable
  "capture": {                                      // the note being organized
    "path": "...", "content_hash": "...",
    "frontmatter_before": { ... }, "body_before": "full text",
    "frontmatter_after": { ... } | null              // after any meta/tag edits
  },
  "targets": [                                      // ONE ENTRY PER FILE TOUCHED — multi-destination is first-class
    { "path": "...", "role": "destination|merge_target|append_target",
      "before_hash": "...", "after_hash": "...",
      "diff": "unified diff of the target file",     // full before-text stored when the file is new or small (<64 KB)
      "description": "the NL description of this destination, if any" }
  ],
  "context": {
    "session_id": "...|null",
    "filters": { ... },
    "suggestions_shown": [ {"path": "...", "score": 3.1, "rank": 1, "reasons": [...] } ],
    "chosen_rank": 2 | null,                         // which suggestion Matt picked — 1 means the engine was right
    "route": "workout|null",
    "auto_tags_present": ["..."],
    "vault_stats": {"capture_backlog": 2299, "index_size": 7516},
    "durations_ms": {"decision": 8400, "operation": 120}   // time from capture shown → action, when in a session
  },
  "llm": {                                           // integrate/auto actions only
    "backend": "claude-cli", "model": "...",
    "prompt_hash": "...",
    "proposed_diff": "what the LLM proposed",
    "final_diff": "what was actually applied",       // ≠ proposed when Matt hand-edited the proposal
    "verdict": "accepted|edited|rejected"
  }
}
```

Design intents an implementer must preserve:

- **Diffs of every touched file, per file** — Matt explicitly adds one capture to multiple files; each target gets its own before/after.
- **The counterfactual is stored, not just the choice**: `suggestions_shown` + `chosen_rank` turn every session action into a labeled ranking example; `proposed_diff` vs `final_diff` turns every reviewed integration into a labeled edit example; a rejection is as much signal as an acceptance.
- **Capture body stored in full** (captures are small); target files stored as hash + diff to bound growth. No retention cap by default — this corpus is the point. Rough budget: even 100 actions/day ≈ tens of MB/year.
- Privacy: the corpus stays local; it inherits the vault's sensitivity. Never ship it to any API except when Matt invokes a learning/auto-organize feature that reads it.

### Uses (now and later)

1. Now: `organize actions stats` — accept-rate of top suggestion, per-route volumes, integrate accept/edit/reject rates. Feeds tuning of doc 04 weights with evidence.
2. Now: doc 04's learning layer records through this same pipeline (its `record_move` consumes ActionRecords — one write path, two readers).
3. Later: the corpus is the few-shot/fine-tune substrate for doc 13's automatic organize ("when Matt captures something like X, he appends it to Y and rewords it like Z").

## 3. Acceptance tests

- Every operation type in the table produces exactly one valid-schema record; a session of 5 actions yields 5 records with consistent `session_id` and correct `chosen_rank`s.
- Multi-destination route action: one record, two `targets` entries, both diffs correct.
- `integrate`: record captures proposed vs final diff; hand-editing the proposal before applying sets `verdict: "edited"` and the two diffs differ.
- Deletion guard: an LLM proposal that removes existing lines is rejected and recorded with `verdict: "rejected"`, target untouched.
- Action log write failure (disk full): operation still completes; loud warning; no partial/corrupt JSONL line ever written (torn-write test).
- `no-ai: true` target refuses `integrate` with a clear error.
