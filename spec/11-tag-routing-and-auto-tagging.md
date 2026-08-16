# 11 — Tag Routing, Natural-Language Destinations, and Auto-Tagging (NEW)

Directive from Matt (2026-08-15): *"For organize, create standard places for tagged things to go. Claude / home server should automatically tag things. I should be able to add natural-language descriptions of folders/files that tagged things should be added/appended to."*

## 1. Routes: standard places for tagged things

A **route** maps a tag (or tag set) to a destination and an integration mode. Routes live in the core config (or an included `routes.toml`) and are fully user-editable:

```toml
[[routes]]
tags = ["workout", "training"]        # any-of; ["a+b"] syntax = requires both
destination = "areas/health/training-log.md"
mode = "append"                        # append | move | integrate
description = """My running/lifting training log. New entries are appended
chronologically under a date heading. Short workout notes, PRs, and how
sessions felt belong here — not general health research."""

[[routes]]
tags = ["blog-idea"]
destination = "projects/blog/ideas.md"
mode = "append"
description = "One-line blog post ideas, appended as list items under ## Inbox."

[[routes]]
tags = ["impro", "theatre"]
destination = "resources/performing/"
mode = "move"                          # folders get move; files get append/integrate
description = "Notes about improvisation, theatre, and performance practice."
```

Semantics:

- `mode = "move"` (destination is a folder): identical to a doc 05 move.
- `mode = "append"` (destination is a file): capture body appended under a `## <date> — from <capture-id>` heading (exact template configurable per route via `template`); target's frontmatter untouched except `last_edited_date`; original capture archived; operation logged as `append`.
- `mode = "integrate"` (destination is a file): a Claude-edit per doc 12 §2 — content woven into the target in Matt's wording/style rather than mechanically appended.
- A capture may match multiple routes → multiple destinations (each gets its action; the original archives once, after all succeed). Order: config order; failures leave the capture unarchived and reported.
- `description` is **natural language, written by Matt**, and is load-bearing: it is (a) shown in the organize UI when the route's destination is suggested, (b) fed to the auto-tagger and to automatic organize (doc 13) as the definition of what belongs there, (c) usable for folders without routes too — see §3.

### Where routes act

1. **Interactive UI:** a matching route becomes the **top suggestion**, rendered distinctly (`[→] training-log.md (route: workout)`), above scored suggestions. `<CR>` executes the route's mode. Everything else in doc 03 unchanged.
2. **Unattended:** a new consumer `tag_router` (doc 06 framework) applies routes automatically to captures whose route has `auto = true` (per-route flag, default false — Matt opts each route into hands-free handling). Non-auto routes only surface in the UI.

## 2. Auto-tagging

A `auto_tagger` consumer (doc 06 framework; runs on the home server via the shared Ollama client, or by shelling to `claude -p` — `backend = "ollama" | "claude-cli"` in config):

- **Trigger:** captures with fewer than `min_tags` (default 1) tags, or with `auto_tag = "pending"`; respects `no-ai: true` (02) and skips notes already auto-tagged at the current content hash.
- **Prompt inputs:** capture content (truncated at `max_chars`), the existing vault tag vocabulary (top N by frequency from the index — reuse of capture-app convention: kebab-case, recall over precision), and **all route/folder descriptions** so tagging aligns with where things can actually go.
- **Output written to frontmatter** (via the round-trip-safe writer): suggested tags go to `tags`, but each machine-added tag is also recorded in `auto_tags` so Matt's tags and machine tags stay distinguishable (and removable in bulk). Sets `auto_tag: done`.
- The organize UI shows auto-tags visually distinct (e.g. dimmed) and doc 07's `t` keybinding edits them like any tags. Accepting a move with auto-tags present is implicit confirmation — recorded as such in the action log (doc 12), which is the training signal for tagger quality.

## 3. Folder descriptions beyond routes

Any PARA folder or file may carry a natural-language description without being a route target. Storage: a `description` field in the folder's `index.md`/`<folder>.md` frontmatter if present, else a central `[descriptions]` table in config. The index loads these; they are surfaced in the UI (folder browse + suggestion reasons) and are inputs to docs 12/13. A CLI helper `organize describe <path> "<text>"` writes them.

## 4. Acceptance tests

- Route with `mode=append`: capture tagged `workout` → body appended to `training-log.md` under the date template, capture archived, `append` operation logged; target frontmatter otherwise byte-identical.
- Multi-route capture: content lands in both destinations; original archived exactly once; failure of the second destination leaves the capture unarchived and both attempts logged.
- Route suggestion outranks scored suggestions in the UI and displays its description.
- `auto=true` route processes unattended via `run-consumers`; `auto=false` never does.
- Auto-tagger: untagged capture gains kebab-case `tags` + matching `auto_tags`, `no-ai: true` capture untouched; rerun at same hash is a no-op.
- Removing a tag from `tags` that exists in `auto_tags` and re-running does not re-add it (the hash check plus `auto_tag: done` prevents nagging).
