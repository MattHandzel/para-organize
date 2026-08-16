# Instructions for agents working in this worktree

- This is the `rewrite` branch of the para-organize repo: a from-scratch rebuild. The binding specification is `spec/` — read `spec/README.md` first, in full, before writing any code.
- The original implementation lives on `main` (sibling worktree `../organize/`) and in git history (`git show d753672~1:<path>` for the last-working plugin code). Consult it only where the spec directs; never copy its bugs (`spec/08-known-issues.md`).
- The live production system still runs from the `main` worktree (`../organize/`) via systemd — do not modify `../organize/` or any live state (`~/.local/state/para-organize/`, `~/.config/para-organize/`) without an explicit migration step from `spec/09-rebuild-guidance.md` §5 and Matt's sign-off.
- The vault (`~/Obsidian/Main` = `~/notes`) is live, irreplaceable data. Develop and test exclusively against fixture vaults; the safety invariants in `spec/05-file-operations.md` are non-negotiable.
- Definition of done, testing bar, and performance targets: `spec/09-rebuild-guidance.md`. Acceptance-test lists in docs 04–07 and 11–13 are mandatory gates, not suggestions.
