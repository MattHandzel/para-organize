#!/usr/bin/env python3
"""Mutation audit for the destination-recall measurement harness.

Each entry below BREAKS one guard or one metric in ``tools/recall_sweep.py``
— reverting it to the plausible-looking wrong thing — runs the harness's own
suite, and requires it to go RED.  A mutation that stays green means the
number that guard protects is unpinned, and an unpinned measurement is how
SQ-1 shipped green.

The harness measures the spec 21 change; nothing measures the harness.  This
does.  It is committed rather than left in a session scratchpad because a
"N/N caught" claim that cannot be re-run from the checkout is not evidence
(the same reason ``tools/mutate_phase5_verify.py`` is committed).

Usage (from the repo root, with the venv):

    .venv/bin/python tools/mutate_recall_sweep.py            # all
    .venv/bin/python tools/mutate_recall_sweep.py RS-1 RS-4  # by id
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTEST = [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-x", "-q"]

SWEEP = "tools/recall_sweep.py"
SUITE = "tests/test_recall_sweep.py"


@dataclass
class Mutation:
    """One broken guard plus the tests that must notice."""

    ident: str
    what: str
    path: str
    old: str
    new: str
    tests: list[str] = field(default_factory=lambda: [SUITE])


MUTATIONS: list[Mutation] = [
    Mutation(
        "RS-1",
        "the live-vault refusal never fires (a sweep could index ~/Obsidian/Main)",
        SWEEP,
        "        if resolved == forbidden or forbidden in resolved.parents:",
        "        if False:",
    ),
    Mutation(
        "RS-2",
        "only the exact root is refused — a subdirectory of the live vault passes",
        SWEEP,
        "        if resolved == forbidden or forbidden in resolved.parents:",
        "        if resolved == forbidden:",
    ),
    Mutation(
        "RS-3",
        "a BASELINE recorded from widened code is accepted (a fake before number)",
        SWEEP,
        '    if mode == "baseline" and widened:',
        "    if False:",
    ),
    Mutation(
        "RS-4",
        "a CANDIDATE recorded from unwidened code is accepted (a fake after number)",
        SWEEP,
        '    if mode == "candidate" and not widened:',
        "    if False:",
    ),
    Mutation(
        "RS-5",
        "an unknown candidate-builder shape silently falls back to the depth-1 ballot",
        SWEEP,
        '    raise SweepError(\n        "unrecognized suggest.generate_candidates signature "',
        '    if True:\n        return "fallback", list(generate(_candidate_folder_mapping(index, config)))\n'
        '    raise SweepError(\n        "unrecognized suggest.generate_candidates signature "',
    ),
    Mutation(
        "RS-6",
        "identical-list clustering ignores ORDER (SQ-1's rank-1 ASCII accident hides)",
        SWEEP,
        "    largest_cluster = Counter(tuple(row.paths) for row in covered)",
        "    largest_cluster = Counter(frozenset(row.paths) for row in covered)",
    ),
    Mutation(
        "RS-7",
        "empty reason lists counted at rank 1 only (SQ-1's 9-row lists pass)",
        SWEEP,
        "    empty_reason = sum(\n        1 for row in rows for count in row.reason_counts if count == 0\n    )",
        "    empty_reason = sum(1 for row in rows if row.reason_counts[:1] == [0])",
    ),
    Mutation(
        "RS-8",
        "a rank 1 counts as stopword-driven if ANY reason is generic, not every one",
        SWEEP,
        "        if all(frontmatter.normalize_tag(match.group(1)) in stop for match in tokens if match):",
        "        if any(frontmatter.normalize_tag(match.group(1)) in stop for match in tokens if match):",
    ),
    Mutation(
        "RS-9",
        "config routes counted as scored recall (a route buys the coverage gate)",
        SWEEP,
        "    scored_covered = [row for row in covered if not all(row.routes)]",
        "    scored_covered = list(covered)",
    ),
    Mutation(
        "RS-10",
        "'previous rank 1 survived' means 'is still rank 1' (outranked reads as vanished)",
        SWEEP,
        "    survived = sum(1 for rel in had_before if base_by[rel].paths[0] in set(cand_by[rel].paths))",
        "    survived = sum(1 for rel in had_before if base_by[rel].paths[:1] == cand_by[rel].paths[:1])",
    ),
    Mutation(
        "RS-11",
        "p95 computed as a mean (the alias-Levenshtein tail disappears)",
        SWEEP,
        "    rank = max(1, math.ceil(q * len(ordered)))\n    return round(float(ordered[rank - 1]), 4)",
        "    return round(float(statistics.fmean(ordered)), 4)",
    ),
    Mutation(
        "RS-12",
        "the reason parser reads the DESTINATION token instead of the capture's",
        SWEEP,
        "_REASON_TOKEN = re.compile(r\"^(?:Tag|Source|Alias) '([^']*)'\")",
        "_REASON_TOKEN = re.compile(r\"(?:Tag|Source|Alias) .*'([^']*)'\")",
    ),
    Mutation(
        "RS-13",
        "--limit defaults to a silent cap of 500 captures (house rule: no silent caps)",
        SWEEP,
        'parser.add_argument("--limit", type=int, default=0, help="sample N captures (0 = all)")',
        'parser.add_argument("--limit", type=int, default=500, help="sample N captures (0 = all)")',
    ),
    Mutation(
        "RS-14",
        "the capability probe drops the note-candidate config key",
        SWEEP,
        '        "config.note_candidates": "note_candidates" in suggestions_cfg,',
        "",
    ),
    Mutation(
        "RS-16",
        "the shipped cli._candidates door is skipped for a hand-rebuilt ballot",
        SWEEP,
        "    if shipped is not None and set(inspect.signature(shipped).parameters) == {\"index\", \"config\"}:",
        "    if False:",
    ),
    Mutation(
        "RS-17",
        "`note_candidates = false` still builds the note ballot (§3.6 off switch ignored)",
        SWEEP,
        '    if not bool(getattr(config.suggestions, "note_candidates", True)):\n        return []',
        "    if False:\n        return []",
    ),
    Mutation(
        "RS-15",
        "Row.from_json drops `kinds` (note-vs-folder rank 1 silently becomes 0/0)",
        SWEEP,
        '            kinds=list(payload.get("kinds", ())),',
        "            kinds=[],",
    ),
]


def run(mutation: Mutation) -> bool:
    path = ROOT / mutation.path
    original = path.read_text(encoding="utf-8")
    if original.count(mutation.old) != 1:
        print(f"  !! anchor not unique ({original.count(mutation.old)}x) — SKIPPED")
        return False
    path.write_text(original.replace(mutation.old, mutation.new), encoding="utf-8")
    try:
        for target in mutation.tests:
            result = subprocess.run(
                [*PYTEST, *target.split()], cwd=ROOT, capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"  CAUGHT by {target}")
                return True
        print("  *** NOT CAUGHT — the guard is decorative")
        return False
    finally:
        path.write_text(original, encoding="utf-8")


def main(argv: list[str]) -> int:
    wanted = set(argv[1:])
    chosen = [m for m in MUTATIONS if not wanted or m.ident in wanted]
    caught = 0
    for mutation in chosen:
        print(f"[{mutation.ident}] {mutation.what}")
        caught += run(mutation)
    print(f"\n{caught}/{len(chosen)} caught")
    return 0 if caught == len(chosen) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
