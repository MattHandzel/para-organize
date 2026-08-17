# Post-cutover feedback — measured evidence

## 0. Authoritative document map (cite these numbers, do not guess)

| doc | owns |
|---|---|
| `spec/14-built-for-others.md` | principles: two users (Matt + a stranger), zero-config promise, de-Matt-ification list, configuration law, extension seams, documentation/discoverability obligations, distribution readiness, privacy/safety |
| `spec/15-capture-presentation.md` | capture-pane redesign: fold defaults, pinned/hidden/rest field policy, per-field formatters, compact→full→raw toggle, custom-renderer seam |
| `spec/16-navigation-and-throughput.md` | hint-jump ("prefix" labels) + throughput: repeat-last-destination, sticky batch destination, multi-select, numeric accept, MRU, queue filtering, progress/resume, destination preview |
| `spec/17-undo-and-action-history.md` | undo semantics, refusal predicates, learning interaction, history view, RPC/CLI surface |
| `spec/18-teach-mode.md` | the sandboxed first-run tutorial |

Doc 14 governs all of 15–18: every behaviour they specify ships with a default,
a config key, a documented meaning, and a test that the key is honored.


Gathered 2026-08-16, the evening of the live cutover, from the real install and
from the read-only golden mirror. Spec 14–18 are written against these numbers;
builders and verifiers cite this file rather than re-deriving them.

## 1. What the capture pane actually shows today

Probed headless against the live core (read-only: `session.start` + `suggest`,
no keys sent, no writes). The left pane is the real note buffer, so the user
sees the file's own bytes:

```
 1| ---
 2| timestamp: '2025-08-17T06:11:28.567311+00:00'
 3| id: '2025-08-17T06:11:28.567311+00:00'
 4| aliases:
 5| - '2025-08-17T06:11:28.567311+00:00'
 6| capture_id: '2025-08-17T06:11:28.567311+00:00'
 7| modalities:
 8| - text
 9| context:
10| - reflecting
11| sources: []
12| tags: []
13| location:
14|   latitude: 37.7642
15|   longitude: -122.3993
16|   city: San Francisco
17|   country: United States
18|   timezone: America/Los_Angeles
19| metadata: {}
20| processing_status: raw
21| created_date: '2025-08-17'
22| last_edited_date: '2025-08-17'
23| ---
24| ## Content
25| I realized that I think I need to actually try and do recruiting. …
```

**23 lines of frontmatter before the first line of thought**, and the same
timestamp string repeated four times (`timestamp`, `id`, `aliases[1]`,
`capture_id`). Of those 22 keys exactly three carry decision-relevant signal for
"where does this note belong": `context`, `tags`, `sources`.

The virtual header drawn beside it today is:

```
Capture 1 of 1862
Aug 17, 07:11 AM
context: reflecting
```

— i.e. the pane already knows what matters, and still makes the user scroll
past the noise to reach the note. That gap is what spec 15 closes.

## 2. Field frequency across real captures

1,000-note sample of `capture/raw_capture` on the golden mirror, counting
top-level frontmatter keys:

| key | notes | signal for filing? |
|---|---|---|
| `tags` | 999 | **yes** |
| `id` | 739 | no — duplicate of `timestamp` |
| `aliases` | 739 | no — duplicate of `timestamp` |
| `last_edited_date` | 735 | no |
| `created_date` | 735 | no — duplicate of `timestamp`'s date |
| `timestamp` | 624 | **yes**, but only when rendered readably |
| `sources` | 624 | **yes** |
| `processing_status` | 624 | no — always `raw` in the backlog |
| `modalities` | 624 | rarely — `text` in almost every case |
| `metadata` | 624 | no — empty map in almost every case |
| `location` | 624 | no — a 5-key nested map, 5 rendered lines |
| `context` | 624 | **yes** |
| `capture_id` | 624 | no — duplicate of `timestamp` |
| `source` / `date` / `time` | 240 | sometimes (older capture schema) |
| `title` / `summary` | 21 | **yes** when present |
| `promoted_to` | 5 | no |

This is the empirical basis for the shipped default field policy: pin
`title`, `summary`, `tags`, `context`, `sources`, `timestamp`; hide the
duplicate-identity and bookkeeping keys; keep everything reachable through the
expand toggle (Matt: "allow me to look at all metadata fields").

The defaults are only defaults — per spec 14 nothing here may be hardcoded, and
a user whose captures carry different keys configures their own policy.

## 3. Fold defect — cause and fix (already landed, `1686de7`)

Reported as "for both windows in ParaOrganize, by default they are folded on
both sides, which is annoying." Cause: neither mount path set window-local fold
options, so both panes inherited the user's globals — here
`~/.config/nvim/lua/options.lua:174` sets `foldmethod=expr` with
`nvim_treesitter#foldexpr()`, which folds every markdown section.

Fixed by forcing `ui.PANE_WIN_OPTIONS` at mount; verified on the live install:

```
organize: foldenable=false foldmethod=manual foldclosed(1)=-1
capture:  foldenable=false foldmethod=manual foldclosed(1)=-1
```

## 4. Live install facts the new work must respect

- `:checkhealth para-organize` reports all green: plenary, telescope, nui,
  **which-key.nvim** and nvim-web-devicons present; core reachable over the
  socket in 3.8 ms; apiVersion handshake 1.
- Index: 13,252 notes, **1,862-capture backlog**, 23 parse errors. Throughput
  features (spec 16) are sized against that backlog, not against a demo vault.
- which-key is installed, so the keymap surface has a discoverability path
  beyond the `?` popup — spec 14's discoverability obligation should use it
  when present and never require it.
- `:checkhealth` cannot find the plugin's healthcheck until the plugin is
  loaded (lazy.nvim only adds the plugin dir to `runtimepath` at load). Real
  interactive sessions load it on `User VeryLazy`, so this bites only headless
  probes — but a stranger's install instructions must say so.

## 4b. The 15 §3 rendering model is empirically proven (not just argued)

Spec 15 draws the field card as `virt_lines` above line 1 and collapses the raw
frontmatter with a manual fold — in a buffer the user edits and `:w`s. That is a
strong claim about a real vault file, so it was tested headlessly before any
builder committed to it (Neovim 0.11.6, a fixture note, no plugin loaded):

| Probe | Result |
|---|---|
| `1,9fold` on the frontmatter block | `foldclosed(1) = 1`, `foldclosedend(1) = 9` — collapsed |
| custom `foldtext` | renders `▸ frontmatter (7 fields) — zi cycles, zo opens` |
| buffer line count / `modified` | 11 lines, `modified = false` — fold + extmarks dirty nothing |
| `virt_lines` with `virt_lines_above` | card renders above line 1, is not buffer text |
| after `:w` | fold still closed, extmark still present — **both survive a write** |
| file bytes after `:w` | only the edited body line changed; **no card or foldtext text leaked into the file** |
| after `:e!` reload | `foldclosed(1) = -1` — **manual folds and extmarks are destroyed by a reload** |

The last row is the one implementers get wrong. Spec 15 §3 already requires the
`BufReadPost` re-apply that covers it; this table is the proof that the
requirement is load-bearing rather than defensive.

## 4c. The 16 §1 hint mechanism is empirically proven

The other mechanism a builder could get badly wrong. Same method — headless
probe, six suggestion rows, no plugin loaded:

| Probe | Result |
|---|---|
| six labels drawn as `virt_text` with `virt_text_pos = "overlay"` | 6 extmarks, labels sit **on** the row's first cell with no reflow |
| buffer text after labelling | byte-identical; `modified = false` |
| `vim.fn.getcharstr()` after feeding `d` | returns `"d"` → resolves to row 3 (`[R] misc`) |
| abort key | `<Esc>` reads as `"\27"`, so cancel is a plain comparison |
| clearing labels | `nvim_buf_clear_namespace` leaves 0 extmarks |

So hint mode needs no third-party library and no transient keymaps: draw
overlay extmarks in a dedicated namespace, block on `getcharstr()`, clear the
namespace on resolve or abort. Multi-character labels are the same loop run
twice with the prefix filtered.

## 5. Keymap surface as it stands (generated from the live table)

25 bindings today: `<CR>` accept, `<Esc>` cancel, `<Tab>`/`<S-Tab>` next/prev
capture, `s` skip, `S` sort cycle, `a` archive, `m` merge, `/` search, `r`
refresh, `p` toggle preview, `?` help, `<A-j>`/`<A-k>` suggestion cursor,
`<C-h>`/`<C-l>` pane focus, `<BS>` back, `<leader>n{p,a,r}` new folder,
`<leader>m{c,x,i,m}` merge/integrate, `e` edit proposed diff.

**Collision constraint for new bindings**: the metadata fields served by the
core carry their own single-letter keymaps (`t` for tags, `i` for importance in
the shipped example), and those are user-configurable — so a new default
binding may not silently claim a letter a user's `metadata_fields` might want.
New defaults must be checked against both tables, and a collision must be
reported by `:checkhealth`, not discovered by a keypress doing the wrong thing.
