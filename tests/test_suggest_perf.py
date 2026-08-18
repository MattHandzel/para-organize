"""Hard performance gate for the widened ballot (spec 21 §3.5).

Measured before this landed: 135 candidates ⇒ 1.30 ms per ``suggest()``;
9,638 candidates ⇒ 88.7 ms — 68×, felt on every keystroke of a 1,862-capture
backlog. The prefilters of §3.5 are what pay for the recall, so they get a
gate rather than a benchmark.

THE SCALE IS THE REAL ONE (§7.11): 13,252 notes and 1,177 folders, the
measured shape of Matt's vault — a perf test on the 21-note fixture is how a
68× regression ships green. The corpus is built in memory (``NoteRecord`` +
folder path strings) because ``build_candidate_set`` and ``suggest`` are pure
over data: no I/O belongs in a scoring measurement.

Marked ``slow``: it runs in ``make perf``, which is part of every phase gate.
"""

from __future__ import annotations

import random
import time

import pytest

from organize_core.config import SuggestionsConfig
from organize_core.index import NoteRecord
from organize_core.learn import LearningData
from organize_core.suggest import CaptureFeaturesView, build_candidate_set, suggest

pytestmark = pytest.mark.slow

NOTE_COUNT = 13_252
FOLDER_COUNT = 1_177
CAPTURE_COUNT = 300

#: Spec 21 §3.5's budget, on the warm path.
P95_BUDGET_MS = 5.0
#: The long-free-text-alias tail, which p95 cannot see at its real 0.2% rate.
#: Not a spec number: a bound this seat sets so the pathology cannot silently
#: return. Measured after §3.5's prefilters: ~20 ms on the real vault's worst
#: capture, down from 573 ms.
WORST_CASE_BUDGET_MS = 60.0
#: The whole ballot is built ONCE per candidate-set build and cached with the
#: index generation (§2.4); this bounds that one build.
BUILD_BUDGET_SECONDS = 2.0

#: Names Matt's vault actually contains, plus a generated long tail — the
#: real index holds ~7,400 distinct note keys, and a 26-word vocabulary would
#: make every candidate name share bigrams with every alias, which is a
#: HARDER corpus than the real one rather than a representative one.
WORDS = (
    "productivity mindset relationships consulting learning-system growth-system reflection "
    "impro health training rehab writing website blog kms exocortex dashboard research public "
    "eduardo-pontes-reis james-fang jennifer-kesteloot flor-laorga reading-list todo mama"
).split()
_SYLLABLES = "ka lo min tar ves ju pho ned ris qua zel bun tri ock wem gaf hyl px sto uvi".split()


def _vocabulary(rng: random.Random, size: int = 4000) -> list[str]:
    words = list(WORDS)
    while len(words) < size:
        words.append("-".join(rng.choice(_SYLLABLES) for _ in range(rng.randint(1, 4))))
    return words


#: Measured on the real backlog (2,445 captures): 12.1% carry a non-capture
#: alias at all, 7.4% one of 20+ characters, 0.2% one of 40+. The gate is
#: only honest at those proportions.
LONG_ALIAS_RATE = 0.074
VERY_LONG_ALIAS_RATE = 0.002
ANY_ALIAS_RATE = 0.121


def _corpus(
    rng: random.Random, vocabulary: list[str]
) -> tuple[dict[str, list[str]], list[NoteRecord]]:
    folders: dict[str, list[str]] = {"projects": [], "areas": [], "resources": []}
    for i in range(FOLDER_COUNT):
        key = ("projects", "areas", "resources")[i % 3]
        depth = i % 3
        parts = "/".join(rng.choice(vocabulary) for _ in range(depth))
        folders[key].append(f"/v/{key}/{parts + '/' if parts else ''}{rng.choice(vocabulary)}-{i}")

    notes: list[NoteRecord] = []
    for i in range(NOTE_COUNT):
        para = ("project", "area", "resource")[i % 3]
        stem = f"{rng.choice(vocabulary)}-{i}" if i % 4 else rng.choice(vocabulary)
        notes.append(
            NoteRecord(
                path=f"/v/{para}s/{rng.choice(vocabulary)}/{stem}.md",
                filename=f"{stem}.md",
                title=stem.replace("-", " ").title(),
                para_type=para,  # type: ignore[arg-type]
                folder=rng.choice(vocabulary),
                aliases=[stem.replace("-", " ")] if i % 5 == 0 else [],
                id=f"id-{i}" if i % 7 == 0 else None,
            )
        )
    return folders, notes


def _long_alias(rng: random.Random, vocabulary: list[str]) -> str:
    """The real pathology: a capture whose alias is a whole sentence of
    filename. One of these cost 573 ms in a single ``suggest()`` before the
    §3.5 prefilters landed."""
    return "2026-06-29_14-31-01_" + "-".join(rng.choice(vocabulary) for _ in range(6))


def _captures(rng: random.Random, vocabulary: list[str]) -> list[CaptureFeaturesView]:
    captures: list[CaptureFeaturesView] = []
    for i in range(CAPTURE_COUNT):
        tags = tuple(rng.choice(WORDS) for _ in range(rng.randint(0, 3)))
        roll = (i + 0.5) / CAPTURE_COUNT
        aliases: tuple[str, ...] = ()
        if roll < VERY_LONG_ALIAS_RATE:
            aliases = (_long_alias(rng, vocabulary),)
        elif roll < LONG_ALIAS_RATE:
            aliases = (f"{rng.choice(vocabulary)} {rng.choice(vocabulary)} 2025 08 05",)
        elif roll < ANY_ALIAS_RATE:
            aliases = (f"{rng.choice(vocabulary)} {rng.choice(vocabulary)}",)
        captures.append(
            CaptureFeaturesView(
                tags=tags,
                normalized_tags=tags,
                sources=(rng.choice(WORDS),) if i % 2 else ("mind",),
                aliases=aliases,
                context=(rng.choice(WORDS),) if i % 6 == 0 else (),
            )
        )
    return captures


def test_suggest_p95_stays_inside_its_budget_at_real_vault_scale() -> None:
    rng = random.Random(20260816)
    vocabulary = _vocabulary(rng)
    folders, notes = _corpus(rng, vocabulary)
    config = SuggestionsConfig()
    learning = LearningData()

    started = time.perf_counter()
    candidates = build_candidate_set(folders, notes, config)
    build_seconds = time.perf_counter() - started

    assert len(candidates) == NOTE_COUNT + FOLDER_COUNT
    assert build_seconds < BUILD_BUDGET_SECONDS, (
        f"building the candidate set took {build_seconds:.3f}s "
        f"(budget {BUILD_BUDGET_SECONDS}s, spec 21 §2.4 — once per build, not per capture)"
    )

    durations: list[float] = []
    for capture in _captures(rng, vocabulary):
        began = time.perf_counter()
        suggest(capture, candidates, config, learning, now=1_760_000_000.0, archive_path="/v/a")
        durations.append((time.perf_counter() - began) * 1000.0)

    durations.sort()
    p95 = durations[int(len(durations) * 0.95)]
    assert p95 < P95_BUDGET_MS, (
        f"suggest() p95 was {p95:.2f} ms over {len(durations)} captures against "
        f"{len(candidates)} candidates (budget {P95_BUDGET_MS} ms, spec 21 §3.5)"
    )
    # And the WORST capture — the 60-character free-text alias that cost
    # 573 ms before §3.5 — stays inside a bound a keystroke can absorb. p95
    # alone would hide it: it is 0.2% of the backlog.
    assert durations[-1] < WORST_CASE_BUDGET_MS, (
        f"the slowest capture took {durations[-1]:.2f} ms "
        f"(budget {WORST_CASE_BUDGET_MS} ms — the long-alias pathology of spec 21 §3.5)"
    )
