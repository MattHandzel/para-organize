# Spec — para-organize rebuild

A complete specification for rebuilding the KMS *organize* stage **from scratch**: exact feature parity with the original (minus its bugs), re-architected as a core engine + thin Neovim client, plus four new capabilities. Written 2026-08-15 from a deep audit of the code (git history and HEAD), the live deployment (systemd units, state databases, Matt's real configs), and the real vault data.

**Read in order.** Docs 01–09 define the parity baseline; docs 10–13 are the new directives, docs 14–18 the post-cutover directives. A later doc overrides an earlier one where they conflict, and each override names exactly where. The complete register:

| Doc | Overrides |
|---|---|
| **10** | The component split: the core owns all behaviour, the Neovim plugin is a thin client. Supersedes every place 03/07 puts vault reads or writes in the plugin. |
| **14** | Principles — **normative over all** (two users, the zero-config promise, configuration law, the seam stability tiers). Tie-break: where 14 and a later doc name the same config key or seam, **the later doc's spelling wins**, and 14's citation is corrected in the same change; 14 stays normative on principles, tiers and laws. |
| **15** | 03 §3's left-pane header list, and the whole `ui.display.*` block, which 15 deletes. No doc adds a key under `ui.display.*`. |
| **16** | 03 §2's default session ordering (`unseen_first`, declared as a deviation in 16 §3.7; `order = "oldest"` restores 03's exact ordering). |
| **17** | 05 §8's "no automated undo command is in scope" sentence (marked superseded at 05:55; the rest of 05 §8 stands). |

No other sentence in 01–09 is superseded. A conflict that is not in this register is a defect in the specs — report it, do not resolve it by choosing.

| Doc | Contents |
|---|---|
| [01-purpose-and-user.md](01-purpose-and-user.md) | What the software is, who uses it and how, why the rewrite, success criteria |
| [02-system-context.md](02-system-context.md) | The vault, the capture-note frontmatter schema (the central contract), every external integration, live state inventory |
| [03-plugin-functional-spec.md](03-plugin-functional-spec.md) | UI behavior: commands, filters, two-pane UI, keymaps, merge, indexer, YAML round-tripping, config surface |
| [04-suggestions-and-learning.md](04-suggestions-and-learning.md) | Scoring signals & exact weights, learning record format, decay, acceptance tests |
| [05-file-operations.md](05-file-operations.md) | Safety invariants: copy-then-archive, atomic writes, backups, operation log, merge semantics |
| [06-automation-pipeline.md](06-automation-pipeline.md) | Emitter/consumer pipeline: store schema & migration, all four consumers, config, systemd, observability |
| [07-metadata-editing.md](07-metadata-editing.md) | Configurable metadata fields editable during organizing (tags, importance/"worth remembering", …) |
| [08-known-issues.md](08-known-issues.md) | Full defect catalog (60+ items) of the original — what not to reproduce; parity is measured against *intended* behavior |
| [09-rebuild-guidance.md](09-rebuild-guidance.md) | Testing strategy, performance targets, migration/cutover, definition of done |
| [10-architecture-core-and-clients.md](10-architecture-core-and-clients.md) | **NEW ARCHITECTURE**: `organize-core` (CLI + server) owns all behavior; Neovim is a thin client; API contract; state layout |
| [11-tag-routing-and-auto-tagging.md](11-tag-routing-and-auto-tagging.md) | Standard destinations for tags, natural-language folder/file descriptions, auto-tagging consumer |
| [12-edit-modes-and-action-recording.md](12-edit-modes-and-action-recording.md) | manual/append/integrate edit modes (Claude edits in Matt's wording + target style); total state/diff recording of every action as a learning corpus |
| [13-automatic-organize.md](13-automatic-organize.md) | FUTURE: highlight/write text in nvim → Claude files it to the correct place(s); trust ladder; built on 10–12 |

Docs 14–18 were written after the 2026-08-16 cutover, from Matt's feedback on
first contact with the running system. **14 governs 15–18**: it is the doc that
turns "Matt's tool" into something a stranger can install, so every behaviour
15–18 specify ships with a default, a config key, a documented meaning, and a
test that the key is honored. The measured evidence behind all four —
the real capture-pane "before", frontmatter key frequency across 1,000 real
captures, the fold defect, the live keymap surface — is
[`doc/FEEDBACK-EVIDENCE-2026-08-16.md`](../doc/FEEDBACK-EVIDENCE-2026-08-16.md).

| Doc | Contents |
|---|---|
| [14-built-for-others.md](14-built-for-others.md) | **PRINCIPLES**: two users (Matt, and a stranger with a different vault), the zero-config promise, the de-Matt-ification work list, configuration law, extension seams and their stability contract, discoverability/documentation and distribution obligations, privacy toward a stranger |
| [15-capture-presentation.md](15-capture-presentation.md) | The capture pane redesign: pane fold defaults, the pinned/hidden/rest field policy, per-field formatters (readable timestamps), the compact→full→raw cycle that keeps *all* metadata reachable, the custom-renderer escape hatch |
| [16-navigation-and-throughput.md](16-navigation-and-throughput.md) | Reaching the exact folder fast: tmux-thumbs style hint labels, whole-vault hint jump, and the throughput set for a 1,862-capture backlog — repeat-destination, sticky pin, batch marking, numeric accept, MRU, queue filtering, progress/resume, destination preview |
| [17-undo-and-action-history.md](17-undo-and-action-history.md) | Undoing the previous action: what is undoable and what honestly is not, reversal-as-forward-operation, the refusal predicates, the learning firewall, the history view, the RPC/CLI surface |
| [18-teach-mode.md](18-teach-mode.md) | The sandboxed first-run tutorial: mechanically provable isolation (no real data, ever), a generated stranger-safe corpus, and a lesson curriculum that doubles as the acceptance checklist |

**Provenance facts an implementer must know before reading any old code** (full detail in 01/08):

1. The original plugin's **HEAD never worked** (refactor left stubs). Behavioral reference: `git show d753672~1:<path>` for `lua/para-organize/{indexer,suggest,learn,move,utils}.lua` — the old code was deleted on this branch but is fully available from git history, and the `main` branch/worktree still has the complete original tree.
2. The Python pipeline crashed on every scheduled run from 2026-05-10 (masked by a wrapper swallowing exit codes).
3. Live configs ≠ repo configs: the operative pipeline config is `~/.config/para-organize/automations.toml`; Matt's plugin setup is in `~/.config/nvim/lua/plugins/init.lua` and contains two misconfigurations (08 §C).

**Suggested build order** (each phase independently shippable): ① core engine: index + frontmatter + file ops + suggestions/learning, CLI only, acceptance suites 04/05 green → ② nvim thin client, parity with 03/07 → ③ consumers migrated into the core (06) + systemd cutover (09 §5) → ④ routes + auto-tagging (11) → ⑤ edit modes + action recording (12; recording ideally lands with ①–② so the corpus starts growing early — at minimum the ActionRecord write path ships in ①) → ⑥ automatic organize (13).
