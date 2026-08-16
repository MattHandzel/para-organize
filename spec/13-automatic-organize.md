# 13 — Automatic Organize (FUTURE, build last)

Directive from Matt (2026-08-15): *"In the future, it may be nice to have an 'automatic organize' functionality so that within Neovim, I can just have Claude add whatever text I write there / whatever I highlight to the correct place."*

This is the capstone the rest of the system exists to enable. It ships **after** 03–12 are solid, but the earlier layers must be built with it in mind (they are: routes+descriptions are the destination knowledge, the action corpus is the behavior knowledge, the core API is the execution path).

## 1. User surface

- **Visual mode:** highlight text → `<leader>oa` (`<Plug>(ParaOrganizeAuto)`) → the selection is treated as an ad-hoc capture and routed to the correct place.
- **Normal mode:** `<leader>oa` on a capture buffer (or any buffer region under a heading) routes that unit.
- **CLI:** `organize auto-organize <file>` / `echo "text" | organize auto-organize -` — same brain, scriptable (this is also how a Claude agent or the pipeline can invoke it on the backlog).
- **Session integration:** inside a doc 03 session, `A` on the current capture = "just handle this one for me."

## 2. Behavior

`auto.propose(text, source_context) → proposal`:

1. Candidate destinations from, in order of authority: matching routes (11) → destination descriptions (11 §3) + doc 04 suggestion scores → **precedent retrieval from the action corpus** (12): nearest past actions by tag/content similarity, "when Matt captured things like this he appended them to X".
2. Chosen integration mode per destination: the route's mode if routed; else the mode Matt historically used for that destination (from the corpus); else `append`.
3. Produces a **proposal**: list of `{destination, mode, rendered_diff, confidence, rationale}` — possibly multiple destinations, exactly like Matt's own multi-file habit.

`auto.apply(proposal)` executes through the ordinary doc 05/11/12 operation paths (backups, atomic writes, deletion guard, `no-ai` refusal, full ActionRecord with `actor: "auto-organize"`).

**Trust ladder (config `auto_organize.trust`):**

- `propose` (default): always show the diff(s) in the UI; Matt confirms per destination (accept / edit / reject — all recorded as training signal).
- `auto_below` — apply without confirmation when confidence ≥ threshold AND the destination has `auto = true`; otherwise fall back to propose. Every auto-applied action is undoable via the operation log + backups, and surfaced in a daily digest note (`organize auto-organize --digest`).
- Confidence must be honest: calibrate against the corpus (fraction of past accepted proposals at similar similarity scores); never auto-apply for destinations with < N (default 5) accepted precedents.

## 3. What earlier docs must have done for this to work (cross-check list)

- 10: `auto.propose`/`auto.apply` RPC + CLI exist from day one (returning "not implemented" until this phase).
- 11: route descriptions and folder descriptions are retrievable via the API.
- 12: corpus queryable by tag/content similarity (`actions query --similar-to <text>`); `chosen_rank`/`verdict` fields populated.
- 04: suggestion scoring exposed for arbitrary text, not only indexed capture files.

## 4. Acceptance tests (for when it ships)

- Text matching an `auto=true` route with strong precedent: applied hands-free, correct file+section, ActionRecord written, digest lists it.
- Novel text with no precedent: never auto-applies regardless of route; proposal shown with rationale.
- Rejected proposal → recorded; identical text re-proposed later must rank that destination lower.
- End-to-end from visual selection in nvim: selected lines land appended in the target, source buffer untouched, undo path (backup + log) verified.
