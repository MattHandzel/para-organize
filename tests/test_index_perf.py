"""Hard performance gates for the indexer (spec 09 §4).

    full index of 10k notes < 5 s (1k < 2 s)
    incremental single-file update < 500 ms
    persistence must not rewrite the snapshot per single-file update

These are gates, not benchmarks: they assert wall-clock budgets against a
generated vault. They run in the normal suite (a 1k-note vault builds in
well under a second) — a perf regression must FAIL, not be skipped.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from organize_core import index as index_module
from organize_core.config import Config, VaultConfig
from organize_core.index import QueryCriteria, VaultIndex

NOTE_COUNT = 1000
FULL_REINDEX_BUDGET_SECONDS = 2.0
INCREMENTAL_BUDGET_SECONDS = 0.5

_TEMPLATE = """---
timestamp: '2026-0{month}-{day:02d}T0{hour}:00:00.000000+00:00'
id: 'gen-{n:04d}'
aliases:
- 'gen-{n:04d}'
capture_id: 'gen-{n:04d}'
modalities:
- text
context: []
sources:
- me
tags:
- generated
- topic-{topic}
location:
  latitude: 40.7126
  longitude: -74.0066
  city: New York
metadata: {{}}
processing_status: raw
created_date: '2026-0{month}-{day:02d}'
last_edited_date: '2026-0{month}-{day:02d}'
---
# Generated note {n}

Body text for note {n}, long enough to be realistic. Spaced repetition,
improv warmups, training logs, and other vault-flavoured prose.
"""


@pytest.fixture(scope="module")
def generated_vault(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("perf-vault")
    raw = root / "capture" / "raw_capture"
    raw.mkdir(parents=True, exist_ok=True)
    for folder in ("projects/blog", "areas/health", "resources/performing", "archive"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    for n in range(NOTE_COUNT):
        text = _TEMPLATE.format(
            n=n,
            month=(n % 9) + 1,
            day=(n % 28) + 1,
            hour=(n % 9),
            topic=n % 25,
        )
        (raw / f"gen-{n:04d}.md").write_text(text, encoding="utf-8")
    return root


def make_index(vault: Path, state: Path) -> VaultIndex:
    return VaultIndex(Config(vault=VaultConfig(root=vault)), state / "index.json")


def test_full_reindex_of_1000_notes_is_under_two_seconds(
    generated_vault: Path, tmp_path: Path
) -> None:
    index = make_index(generated_vault, tmp_path)
    index.load()
    result = index.full_reindex()

    assert result["total"] == NOTE_COUNT
    assert result["duration"] < FULL_REINDEX_BUDGET_SECONDS, (
        f"full_reindex of {NOTE_COUNT} notes took {result['duration']:.3f}s "
        f"(budget {FULL_REINDEX_BUDGET_SECONDS}s, spec 09 §4)"
    )
    # the whole call, snapshot write included, stays inside the budget
    assert index.stats()["total"] == NOTE_COUNT


def test_incremental_update_is_under_500ms_and_writes_nothing(
    generated_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = make_index(generated_vault, tmp_path)
    index.load()
    index.full_reindex()

    writes = 0

    def counting(path: Path, payload: dict) -> None:
        nonlocal writes
        writes += 1

    monkeypatch.setattr(index_module, "write_snapshot", counting)

    target = generated_vault / "capture" / "raw_capture" / "gen-0500.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace("- generated", "- generated\n- edited"),
        encoding="utf-8",
    )

    started = time.monotonic()
    record = index.update_file(target)
    elapsed = time.monotonic() - started

    assert record is not None
    assert "edited" in record.tags
    assert elapsed < INCREMENTAL_BUDGET_SECONDS, (
        f"update_file took {elapsed:.3f}s (budget {INCREMENTAL_BUDGET_SECONDS}s, spec 09 §4)"
    )
    assert writes == 0, "a single-file update must not rewrite the snapshot (09 §4)"


def test_queries_over_1000_notes_stay_interactive(generated_vault: Path, tmp_path: Path) -> None:
    """Suggestion generation has a 100 ms budget (09 §4) and starts from a
    query, so the query itself must be far cheaper than that."""
    index = make_index(generated_vault, tmp_path)
    index.load()
    index.full_reindex()

    started = time.monotonic()
    raw_captures = index.query(QueryCriteria(status=["raw"], para_type=["capture"]))
    elapsed = time.monotonic() - started

    assert len(raw_captures) == NOTE_COUNT
    assert elapsed < 0.1, f"query over {NOTE_COUNT} notes took {elapsed:.3f}s"
