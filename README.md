# para-organize — ground-up rebuild (branch: `rewrite`)

This branch is a clean slate for rebuilding the KMS organize stage. The old implementation was removed here (it remains intact on `main` and throughout git history — reference rev for original behavior: `d753672~1`).

**Everything an implementing agent needs is in [`spec/`](spec/README.md).** Start with `spec/README.md`, read docs in order, and follow the build order at the bottom of it. Do not start from the old code; start from the spec, consulting old code only where the spec directs.

Status: **spec complete, awaiting Matt's review. No implementation has begun.**

## Which destinations `organize suggest` offers (spec 21)

Candidates are folders under the non-archive PARA roots **and** the notes
inside them; accepting a note **merges** the capture into it (spec 05 §4)
instead of moving the file, and the wire says which via
`destination_kind: "folder" | "note"` — never inferred from a trailing `.md`.
The four core keys, all in `[suggestions]` of `config.toml` (the shipped
example config is the full schema reference):

| Key | Default | Meaning |
|---|---|---|
| `max_candidate_depth` | `3` | How many levels **below** a PARA root a folder may be and still be suggested; an immediate subfolder is `1`, `"all"` is unbounded. `1` is the pre-21 behaviour. Independent of the destination picker's browse depth. |
| `note_candidates` | `true` | Whether existing notes are offered as destinations at all. `false` restores the folders-only ballot. |
| `max_note_suggestions` | `3` | Cap on note rows in one list; folders fill the remainder and `max_suggestions` (the total) is unchanged. Set it to `max_suggestions` to disable. |
| `candidate_stopwords` | `["mind","self","me","text","voice","note","thought"]` | Capture tags/sources too generic to name a destination, filtered from the capture side before matching, for folders and notes alike. Match-time only — nothing here changes a tag written to a note or to `learning.json`. |

The seven scoring signals and their weights (spec 04 §2) are unchanged: a
note is scored by exactly the same arithmetic as a folder.
