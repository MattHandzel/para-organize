"""Index snapshot: schema, BATCHED atomic flush, prune-on-load (03 §7, 09 §4).

The load-bearing gate here is 09 §4: *"Index persistence must not rewrite a
multi-MB JSON on every keystroke-triggered update"*. Writes are counted by
spying on ``index.write_snapshot`` — asserting "N incremental updates produce
ZERO snapshot writes, and the following flush produces exactly ONE".

Doc-08 regression obligations covered in this file:

* §C5 the live ``index.json`` kept entries under a wrong root forever —
  a snapshot built for another vault is rejected, and stray records are
  pruned on load.
* §A3 ``full_reindex`` did not exist (``reindex``/debug crashed) — it exists,
  reports ``{total, duration}``, and refuses to run reentrantly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from organize_core import index as index_module
from organize_core.config import Config, VaultConfig
from organize_core.errors import IndexingError
from organize_core.index import INDEX_SCHEMA_VERSION, QueryCriteria, VaultIndex

FIXTURE_MD_TOTAL = 21


class WriteSpy:
    """Counts real snapshot writes while still performing them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        real = index_module.write_snapshot

        def counting(path: Path, payload: dict[str, Any]) -> None:
            self.count += 1
            real(path, payload)

        monkeypatch.setattr(index_module, "write_snapshot", counting)


def make_index(vault: Path, state: Path) -> VaultIndex:
    return VaultIndex(Config(vault=VaultConfig(root=vault)), state / "index.json")


def write_note(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def read_snapshot(index: VaultIndex) -> dict[str, Any]:
    return json.loads(index.index_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# snapshot format
# ---------------------------------------------------------------------------


def test_snapshot_has_schema_version_one_and_the_vault_root(
    fixture_vault: Path, tmp_path: Path
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    payload = read_snapshot(index)
    assert payload["schema_version"] == INDEX_SCHEMA_VERSION == 1
    assert payload["vault_root"] == str(fixture_vault)
    assert isinstance(payload["generated_at"], float)
    assert len(payload["notes"]) == FIXTURE_MD_TOTAL
    entry = payload["notes"][str(fixture_vault / "projects/blog/ideas.md")]
    assert entry["title"] == "Blog ideas"
    assert entry["para_type"] == "project"


def test_snapshot_round_trips_every_record(fixture_vault: Path, tmp_path: Path) -> None:
    first = make_index(fixture_vault, tmp_path)
    first.load()
    first.full_reindex()

    second = make_index(fixture_vault, tmp_path)
    second.load()
    assert second.stats()["total"] == FIXTURE_MD_TOTAL
    assert second.query(QueryCriteria()) == first.query(QueryCriteria())
    assert second.stats()["by_para_type"] == first.stats()["by_para_type"]


def test_load_without_a_snapshot_starts_empty(fixture_vault: Path, tmp_path: Path) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    assert index.stats()["total"] == 0
    assert index.query(QueryCriteria()) == []


# ---------------------------------------------------------------------------
# batching (spec 09 §4 — the perf gate)
# ---------------------------------------------------------------------------


def test_incremental_updates_coalesce_into_one_flush(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    spy = WriteSpy(monkeypatch)
    paths = [
        write_note(
            fixture_vault,
            f"capture/raw_capture/batch-{n:03d}.md",
            f"---\nid: batch-{n}\ntags:\n- batch\n---\nbody {n}\n",
        )
        for n in range(25)
    ]
    for path in paths:
        assert index.update_file(path) is not None

    assert spy.count == 0, "25 single-file updates must not rewrite the snapshot 25 times"
    assert index.stats()["pending_changes"] == 25

    index.flush()
    assert spy.count == 1
    assert index.stats()["pending_changes"] == 0
    assert len(read_snapshot(index)["notes"]) == FIXTURE_MD_TOTAL + 25


def test_flush_without_pending_changes_writes_nothing(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    spy = WriteSpy(monkeypatch)
    index.flush()
    index.flush()
    assert spy.count == 0


def test_flush_threshold_bounds_unflushed_work(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batching must not mean unbounded loss on a crash: the pending batch
    auto-flushes once it grows past ``flush_threshold``."""
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()
    index.flush_threshold = 3

    spy = WriteSpy(monkeypatch)
    for n in range(6):
        path = write_note(
            fixture_vault, f"capture/raw_capture/t-{n}.md", f"---\nid: t{n}\n---\nbody\n"
        )
        index.update_file(path)
    assert spy.count == 2  # exactly one write per full batch of three
    assert index.stats()["pending_changes"] == 0


def test_full_reindex_writes_exactly_one_snapshot(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    spy = WriteSpy(monkeypatch)
    result = index.full_reindex()
    assert spy.count == 1
    assert result["total"] == FIXTURE_MD_TOTAL
    assert result["duration"] > 0


def test_removals_are_batched_too(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    spy = WriteSpy(monkeypatch)
    index.remove_file(fixture_vault / "resources/performing/impro.md")
    index.remove_file(fixture_vault / "projects/blog/ideas.md")
    assert spy.count == 0
    index.flush()
    assert spy.count == 1
    assert len(read_snapshot(index)["notes"]) == FIXTURE_MD_TOTAL - 2


def test_flush_is_atomic_and_leaves_no_temp_files(fixture_vault: Path, tmp_path: Path) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()
    state_dir = index.index_path.parent
    assert index.index_path.exists()
    assert [p.name for p in state_dir.iterdir() if ".tmp" in p.name] == []


def test_flush_failure_is_loud(fixture_vault: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory\n", encoding="utf-8")
    index = VaultIndex(Config(vault=VaultConfig(root=fixture_vault)), blocker / "index.json")
    index.load()
    index.scan()
    with pytest.raises(IndexingError) as excinfo:
        index.flush()
    assert "index.json" in str(excinfo.value)


# ---------------------------------------------------------------------------
# prune / corruption tolerance (03 §7, 08 §C5)
# ---------------------------------------------------------------------------


def test_load_prunes_records_whose_files_are_gone(fixture_vault: Path, tmp_path: Path) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    (fixture_vault / "resources/performing/impro.md").unlink()

    reloaded = make_index(fixture_vault, tmp_path)
    reloaded.load()
    assert reloaded.stats()["total"] == FIXTURE_MD_TOTAL - 1
    assert reloaded.get(fixture_vault / "resources/performing/impro.md") is None
    # the pruned snapshot is itself stale ⇒ the next flush rewrites it
    assert reloaded.stats()["pending_changes"] == 1
    reloaded.flush()
    assert len(read_snapshot(reloaded)["notes"]) == FIXTURE_MD_TOTAL - 1


def test_load_prunes_entries_under_a_wrong_root(
    fixture_vault: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """08 §C5: the live index kept ``…/Main/notes/capture`` entries forever."""
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    payload = read_snapshot(index)
    stale_path = str(tmp_path / "elsewhere" / "ghost.md")
    payload["notes"][stale_path] = {
        "path": stale_path,
        "filename": "ghost.md",
        "title": "ghost",
        "para_type": "capture",
        "folder": "elsewhere",
    }
    index.index_path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = make_index(fixture_vault, tmp_path)
    with caplog.at_level(logging.WARNING, logger="organize_core.index"):
        reloaded.load()
    assert reloaded.stats()["total"] == FIXTURE_MD_TOTAL
    assert reloaded.get(stale_path) is None
    assert any("outside vault root" in record.getMessage() for record in caplog.records)


def test_snapshot_from_a_different_vault_is_rejected(
    fixture_vault: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    payload = read_snapshot(index)
    payload["vault_root"] = "/somewhere/else"
    index.index_path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = make_index(fixture_vault, tmp_path)
    with caplog.at_level(logging.ERROR, logger="organize_core.index"):
        reloaded.load()
    assert reloaded.stats()["total"] == 0
    assert any("rebuilding from scratch" in record.getMessage() for record in caplog.records)


def test_wrong_schema_version_rebuilds_loudly(
    fixture_vault: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    payload = read_snapshot(index)
    payload["schema_version"] = 99
    index.index_path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = make_index(fixture_vault, tmp_path)
    with caplog.at_level(logging.ERROR, logger="organize_core.index"):
        reloaded.load()
    assert reloaded.stats()["total"] == 0
    assert any("schema_version" in record.getMessage() for record in caplog.records)
    # and a rebuild is possible right away
    assert reloaded.full_reindex()["total"] == FIXTURE_MD_TOTAL


def test_corrupt_snapshot_degrades_to_a_rebuild(
    fixture_vault: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.index_path.parent.mkdir(parents=True, exist_ok=True)
    index.index_path.write_text("{not json at all", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="organize_core.index"):
        index.load()  # must not raise
    assert index.stats()["total"] == 0
    assert any("unreadable/corrupt" in record.getMessage() for record in caplog.records)


def test_malformed_entries_are_dropped_not_fatal(
    fixture_vault: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    payload = read_snapshot(index)
    payload["notes"]["/bogus"] = "not a record"
    payload["notes"]["/bogus2"] = {"path": "", "filename": ""}
    index.index_path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = make_index(fixture_vault, tmp_path)
    with caplog.at_level(logging.WARNING, logger="organize_core.index"):
        reloaded.load()
    assert reloaded.stats()["total"] == FIXTURE_MD_TOTAL
    assert any("malformed snapshot entry" in record.getMessage() for record in caplog.records)


def test_snapshot_entries_with_unknown_keys_survive_a_version_skew(
    fixture_vault: Path, tmp_path: Path
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    payload = read_snapshot(index)
    key = str(fixture_vault / "projects/blog/ideas.md")
    payload["notes"][key]["future_field"] = "ignored"
    index.index_path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = make_index(fixture_vault, tmp_path)
    reloaded.load()
    record = reloaded.get(key)
    assert record is not None and record.title == "Blog ideas"


# ---------------------------------------------------------------------------
# full_reindex (08 §A3)
# ---------------------------------------------------------------------------


def test_full_reindex_rebuilds_from_zero(fixture_vault: Path, tmp_path: Path) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    index.full_reindex()

    ghost = write_note(fixture_vault, "projects/kms/ghost.md", "---\nid: ghost\n---\nx\n")
    index.update_file(ghost)
    ghost.unlink()

    result = index.full_reindex()
    assert result == {"total": FIXTURE_MD_TOTAL, "duration": pytest.approx(result["duration"])}
    assert index.get(ghost) is None
    assert len(read_snapshot(index)["notes"]) == FIXTURE_MD_TOTAL


def test_full_reindex_has_a_reentrancy_guard(
    fixture_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = make_index(fixture_vault, tmp_path)
    index.load()
    seen: list[str] = []

    def reentrant_scan() -> int:
        seen.append("scan")
        with pytest.raises(IndexingError) as excinfo:
            index.full_reindex()
        assert "already running" in str(excinfo.value)
        return 0

    monkeypatch.setattr(index, "scan", reentrant_scan)
    index.full_reindex()
    assert seen == ["scan"]
    # the guard is released afterwards
    monkeypatch.undo()
    assert index.full_reindex()["total"] == FIXTURE_MD_TOTAL
