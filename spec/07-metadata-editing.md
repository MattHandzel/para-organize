# 07 — Configurable Metadata Editing During Organization (NEW requirement)

> This is the one deliberate scope addition to the parity rewrite, requested by Matt on 2026-08-15:
> *"During para organize, I should be able to add some other metadata … For example, I'd like to be able to add tags. I'd like to be able to add if this is something that's very valuable, meaning that I'd try to remember it or not. Make it easy for me to configure this software and add metadata."*

## Requirement

While a capture is open in the organize UI, Matt can annotate it with structured frontmatter metadata in one or two keystrokes, and the set of available metadata fields is **pure configuration** — adding a new field must require zero code changes.

## Design

### Config schema

New top-level config section in `setup()`:

```lua
metadata_fields = {
  -- Each entry defines one field Matt can set from the organize UI.
  {
    key = "tags",              -- frontmatter key to write
    type = "list",             -- "list" | "string" | "boolean" | "enum" | "number"
    keymap = "t",              -- buffer-local key in the organize UI (normal mode)
    prompt = "Add tag(s)",     -- vim.ui.input prompt
    append = true,             -- for lists: append (default) vs replace
    complete = "existing",     -- completion source: "existing" (values seen in index) | list of values | nil
  },
  {
    key = "importance",        -- Matt's "is this valuable / worth remembering" flag
    type = "enum",
    keymap = "i",
    values = { "high", "medium", "low" },   -- selected via vim.ui.select
  },
  {
    key = "remember",          -- boolean toggle example
    type = "boolean",
    keymap = "v",              -- toggles true/false on each press
  },
}
```

Rules:

- `type = "list"`: prompt accepts comma-separated values; normalized (trimmed; kebab-case if the existing capture-app convention `tags_kebab` is on for that key — make normalization per-field config `normalize = "kebab" | nil`). Appended to any existing list, deduplicated.
- `type = "enum"`: `vim.ui.select` over `values`.
- `type = "boolean"`: keypress toggles; display current state in the pane's metadata summary.
- `type = "string"` / `"number"`: `vim.ui.input`, number-validated for `"number"`.
- `complete = "existing"`: offer completion from all values of that key across the index (e.g. every tag in the vault) — this is what makes tag entry fast and consistent.
- Keymap collisions with core organize keymaps must be detected at setup and reported as a config validation error.

### Behavior

- Edits apply to the **in-memory frontmatter of the current capture** and are written when the note is saved/moved/merged/archived (same atomic write path as all frontmatter rewrites — see `05-file-operations.md`). If the capture is skipped after annotation, the annotation is still persisted to the file in place (annotating without organizing is a valid outcome: Matt may tag value on a note he isn't ready to file).
- The left pane's metadata summary re-renders immediately after each edit so state is visible (e.g. `importance: high ✓`).
- Fields written must round-trip through the frontmatter writer preserving all other fields and body exactly.
- The `?` help overlay lists metadata keymaps alongside core ones.
- The default configuration ships with the `tags` (list, `t`) and `importance` (enum high/medium/low, `i`) fields above, so the feature works out of the box; Matt can add/remove fields freely.

### Downstream note

Because fields are arbitrary frontmatter, they are automatically visible to Obsidian, the Python pipeline (which can filter on them — e.g. a future consumer sending `importance: high` notes to Anki generation), and agents reading the vault. No special handling needed beyond honest YAML round-tripping.

### Acceptance tests

1. With default config, press `t`, type `foo, Bar Baz` → frontmatter `tags` gains `foo`, `bar-baz` (if kebab normalization on for tags); body untouched; other fields byte-identical.
2. Press `i`, pick `high` → `importance: high` in frontmatter; re-press and pick `low` → replaced, not duplicated.
3. Add a novel field `{ key = "energy", type = "number", keymap = "E" }` in config only → works with no code change.
4. Configure a field with `keymap = "a"` (collides with archive) → setup() raises a clear validation error naming both bindings.
5. Annotate then skip → file on disk has the new metadata; note remains unorganized (still in capture folder, `processing_status` unchanged).
