#!/usr/bin/env python3
"""Mutation audit for the Phase-5 verify-fix pass.

Each entry below REVERTS one fix to the exact shipped-and-broken behaviour the
finding described, runs the targeted tests, and requires them to go RED. A
mutation that stays green means the fix is unpinned — the regression is
decorative and the next refactor deletes it silently.

Usage (from the repo root, with the venv):

    .venv/bin/python tools/mutate_phase5_verify.py            # all
    .venv/bin/python tools/mutate_phase5_verify.py P5-1 ...   # by id

Committed rather than left in a session scratchpad, because a 34/34 claim that
cannot be re-run from the checkout is not evidence (the Phase-5 record cited a
`scratchpad/mutate_phase5.py` that does not exist in the repo).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTEST = [str(ROOT / ".venv/bin/python"), "-m", "pytest", "-x", "-q"]

INTEGRATE = "src/organize_core/integrate.py"
ROUTES = "src/organize_core/routes.py"
CONFIG = "src/organize_core/config.py"
ACTIONS = "src/organize_core/actions.py"
FILEOPS = "src/organize_core/fileops.py"
SERVER = "src/organize_core/server.py"

HARDENING = "tests/test_integrate_hardening.py"
GATE = "tests/test_integrate_review_gate.py"


@dataclass
class Mutation:
    """One reverted fix plus the tests that must notice."""

    ident: str
    what: str
    path: str
    old: str
    new: str
    tests: list[str] = field(default_factory=list)


MUTATIONS: list[Mutation] = [
    Mutation(
        "P5-1",
        "`summarize` read back from the client payload again",
        INTEGRATE,
        "                summarize=False,\n",
        "                summarize=bool(raw.get(\"summarize\", False)),\n",
        [HARDENING],
    ),
    Mutation(
        "P5-ADV-01",
        "verbatim containment checked against the WHOLE result again",
        INTEGRATE,
        'if wanted and wanted not in normalize_whitespace("\\n".join(added)):',
        "if wanted and wanted not in normalize_whitespace(after_body):",
        [HARDENING],
    ),
    Mutation(
        "P5-ADV-02",
        "the order guard removed (re-ordering passes with zero deletions)",
        INTEGRATE,
        "    moved = reordered_line_count(before, after)\n    if moved:",
        "    moved = reordered_line_count(before, after)\n    if False:",
        [HARDENING],
    ),
    Mutation(
        "P5-ADV-03a",
        "the growth bound removed (unbounded fabricated content)",
        INTEGRATE,
        "    if len(added) > growth_limit:",
        "    if False:",
        [HARDENING],
    ),
    Mutation(
        "P5-ADV-03b",
        "the duplication guard removed (Matt's content emitted twice)",
        INTEGRATE,
        "    if duplicated:",
        "    if False:",
        [HARDENING],
    ),
    Mutation(
        "P5-ADV-03c",
        "max_added_lines ignored in favour of a hardcoded number",
        INTEGRATE,
        "    slack = int(config.integrate.max_added_lines)",
        "    slack = 10",
        [HARDENING],
    ),
    Mutation(
        "P5-ADV-08",
        "`last_edited_date` accepted as free text again",
        INTEGRATE,
        '    if "last_edited_date" in touched:',
        "    if False:",
        [HARDENING],
    ),
    Mutation(
        "P5-6",
        "every commit-time refusal called 'the integrate deletion guard' again",
        INTEGRATE,
        '    if kind in _MERGE_PATH_KINDS:',
        "    if True:",
        [HARDENING],
    ),
    Mutation(
        "P5-ADV-07",
        "a raced rejection no longer marks its unreplayable trace",
        INTEGRATE,
        "            stale_target = True\n",
        "            stale_target = False\n",
        [HARDENING],
    ),
    Mutation(
        "P5-ADV-05",
        "the torn-tail heal removed (next record glued onto a crash remnant)",
        ACTIONS,
        '        if start > 0 and os.pread(fd, 1, start - 1) != b"\\n":',
        "        if False:",
        ["tests/test_actions_recorder.py"],
    ),
    Mutation(
        "P5-3a",
        "`_merged_record` takes edit_mode from the first non-null record again",
        ROUTES,
        "    edit_mode = winner.edit_mode or next(",
        "    edit_mode = None or next(",
        ["tests/test_routes_apply.py"],
    ),
    Mutation(
        "P5-3b",
        "`_merged_record` drops the llm block again",
        ROUTES,
        "    llm = winner.llm if winner.llm is not None else (traced[0].llm if traced else None)",
        "    llm = base.llm",
        ["tests/test_routes_apply.py"],
    ),
    Mutation(
        "P5-ADV-04",
        "the second integrate destination's trace dropped again",
        ROUTES,
        "    per_target_traces = len(traced) > 1",
        "    per_target_traces = False",
        ["tests/test_routes_apply.py"],
    ),
    Mutation(
        "P5-4",
        "the config gate judges the ROUTE's review instead of the resolved one",
        CONFIG,
        '    if auto and mode == "integrate" and review != "auto":',
        '    if auto and mode == "integrate" and (stated_review or "diff") != "auto":',
        [GATE],
    ),
    Mutation(
        "P5-5a",
        "the global un-gates a route that spelled review = \"diff\" (OR-of-auto)",
        ROUTES,
        "    stated = match.route.review\n    if stated is not None:\n"
        '        return _REVIEW_AUTO if str(stated) == _REVIEW_AUTO else _REVIEW_DIFF',
        "    stated = match.route.review\n    if str(stated) == _REVIEW_AUTO:\n"
        "        return _REVIEW_AUTO",
        [GATE],
    ),
    Mutation(
        "P5-5b",
        "the route's review is no longer resolved at load",
        CONFIG,
        '    review = stated_review if stated_review is not None else str(integrate.review)',
        '    review = stated_review if stated_review is not None else "diff"',
        [GATE],
    ),
    Mutation(
        "P5-10",
        "`durations_ms.operation` no longer produced by the core",
        FILEOPS,
        '    durations_ms.setdefault("operation", max(0, int((ctx.clock() - now) * 1000)))',
        "    pass",
        ["tests/test_server.py::test_op_skip_records_the_decision_and_marks_the_session"],
    ),
    Mutation(
        "P5-2",
        "propose dispatched under the read lock again (writers park behind the model)",
        SERVER,
        "        if method in NON_QUEUED_LLM_METHODS:",
        "        if False:",
        ["tests/test_server.py -k propose"],
    ),
    Mutation(
        "P5-9",
        "NON_QUEUED_LLM_METHODS emptied — the constant must be load-bearing",
        SERVER,
        'NON_QUEUED_LLM_METHODS: frozenset[str] = frozenset({"op.integrate_propose"})',
        "NON_QUEUED_LLM_METHODS: frozenset[str] = frozenset()",
        ["tests/test_server.py -k propose", "tests/test_server_protocol.py"],
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
            argv = [*PYTEST, *target.split()]
            result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"  CAUGHT by {target}")
                return True
        print("  *** NOT CAUGHT — the fix is unpinned")
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
