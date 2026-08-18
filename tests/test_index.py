"""Vault scan / record shape / query semantics (spec 03 §2 filters, §7 indexer).

Every assertion here is an EXACT value: the old Lua suite asserted ``> 0``
and stayed green while ``indexer.search`` matched everything and
``full_reindex`` did not exist (08 §A2/§A3/§A10).

Doc-08 regression obligations covered in this file:

* §A10 ``indexer/query.lua`` stub matched every record — filters here assert
  exact result sets, including "no match ⇒ empty".
* §A13 ``ignore_patterns`` are shell GLOBS applied as Lua patterns, so they
  never filtered — asserted both ways (a glob that excludes, and the default
  patterns that do not).
* §A36 tests-vs-code disagreed on string-vs-array filter criteria — both
  shapes are accepted and canonicalized to lists (03 §2 boundary rule).
* §B10 ``aliases[0]`` on a scalar alias yielded its first CHARACTER — titles
  come from the first ``# heading`` else the filename stem, and a scalar
  alias becomes a one-element list.
* §C5 stale entries under a wrong root — refused on the incremental path
  (the load path is covered in ``test_index_persistence.py``).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from organize_core.config import Config, VaultConfig
from organize_core.errors import ConfigError, IndexingError
from organize_core.index import (
    NoteRecord,
    QueryCriteria,
    VaultIndex,
    para_type_for,
)

# The fixture vault holds exactly these markdown files (tests/conftest.py).
FIXTURE_MD_TOTAL = 21
FIXTURE_PARA_COUNTS = {"capture": 16, "project": 1, "area": 1, "resource": 1, "other": 2}
FIXTURE_RAW_CAPTURES = 11  # processing_status: raw inside the capture folder


def make_config(vault: Path, **vault_kwargs: object) -> Config:
    return Config(vault=VaultConfig(root=vault, **vault_kwargs))  # type: ignore[arg-type]


def make_index(vault: Path, tmp_path: Path, **vault_kwargs: object) -> VaultIndex:
    index = VaultIndex(make_config(vault, **vault_kwargs), tmp_path / "state" / "index.json")
    index.load()
    return index


def write_note(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def paths_of(records: list[NoteRecord]) -> list[str]:
    return [record.path for record in records]


def names_of(records: list[NoteRecord]) -> list[str]:
    return [record.filename for record in records]


@pytest.fixture()
def index(fixture_vault: Path, tmp_path: Path) -> VaultIndex:
    idx = make_index(fixture_vault, tmp_path)
    idx.scan()
    return idx


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def test_scan_indexes_every_markdown_file_exactly_once(index: VaultIndex) -> None:
    stats = index.stats()
    assert stats["total"] == FIXTURE_MD_TOTAL
    all_records = index.query(QueryCriteria())
    assert len(all_records) == FIXTURE_MD_TOTAL
    assert len(set(paths_of(all_records))) == FIXTURE_MD_TOTAL


def test_scan_ignores_non_markdown_files_without_choking(index: VaultIndex) -> None:
    """Spec 02: .wav/.txt/.pdf are interleaved in raw_capture."""
    suffixes = {Path(record.path).suffix for record in index.query(QueryCriteria())}
    assert suffixes == {".md"}


def test_scan_is_idempotent(index: VaultIndex) -> None:
    assert index.scan() == FIXTURE_MD_TOTAL
    assert index.stats()["total"] == FIXTURE_MD_TOTAL


def test_para_types_are_exact(index: VaultIndex) -> None:
    assert index.stats()["by_para_type"] == FIXTURE_PARA_COUNTS


def test_archive_subtree_is_archive_not_capture(index: VaultIndex, fixture_vault: Path) -> None:
    """``archive/capture/raw_capture`` matches two configured prefixes; the
    longest (the archives root) wins, so archived captures never re-enter a
    session's capture list."""
    path = write_note(fixture_vault, "archive/capture/raw_capture/old.md", "---\nid: old\n---\nbody\n")
    record = index.update_file(path)
    assert record is not None
    assert record.para_type == "archive"
    assert para_type_for(path, index.config) == "archive"


def test_para_type_for_uses_configured_folder_names(fixture_vault: Path) -> None:
    config = make_config(fixture_vault, para_folders={"projects": "projects", "archives": "archive"})
    assert para_type_for(fixture_vault / "projects/blog/ideas.md", config) == "project"
    assert para_type_for(fixture_vault / "archive/x.md", config) == "archive"
    assert para_type_for(fixture_vault / "capture/raw_capture/a.md", config) == "capture"
    # areas/resources are not configured here ⇒ "other", never a guess.
    assert para_type_for(fixture_vault / "areas/health/index.md", config) == "other"
    assert para_type_for(Path("/somewhere/else/note.md"), config) == "other"


def test_dot_obsidian_is_never_indexed(fixture_vault: Path, tmp_path: Path) -> None:
    write_note(fixture_vault, ".obsidian/plugin-notes.md", "---\nid: plugin\n---\nx\n")
    index = make_index(fixture_vault, tmp_path)
    assert index.scan() == FIXTURE_MD_TOTAL
    assert index.get(fixture_vault / ".obsidian/plugin-notes.md") is None


def test_ignore_patterns_are_globs_not_literals(fixture_vault: Path, tmp_path: Path) -> None:
    """08 §A13: the old code applied shell globs as Lua patterns, so nothing
    was ever filtered. A ``*`` glob must actually exclude files."""
    default_index = make_index(fixture_vault, tmp_path / "a")
    default_index.scan()
    conflict = fixture_vault / "capture/raw_capture/scalar-tags.sync-conflict-20260701-123456-ABCDEF.md"
    assert default_index.get(conflict) is not None

    globbed = make_index(
        fixture_vault,
        tmp_path / "b",
        ignore_patterns=[".obsidian", "*.sync-conflict-*"],
    )
    assert globbed.scan() == FIXTURE_MD_TOTAL - 1
    assert globbed.get(conflict) is None


def test_ignore_pattern_prunes_a_whole_subtree(fixture_vault: Path, tmp_path: Path) -> None:
    index = make_index(fixture_vault, tmp_path, ignore_patterns=["capture/raw_capture"])
    assert index.scan() == FIXTURE_MD_TOTAL - 16
    assert index.stats()["by_para_type"] == {"project": 1, "area": 1, "resource": 1, "other": 2}


def test_files_over_max_file_size_are_skipped_loudly(
    fixture_vault: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    big = write_note(fixture_vault, "resources/performing/huge.md", "x" * 5000)
    index = make_index(fixture_vault, tmp_path, max_file_size=1024)
    with caplog.at_level(logging.WARNING, logger="organize_core.index"):
        total = index.scan()
    assert total == FIXTURE_MD_TOTAL  # the 21 fixture notes, minus none, plus none
    assert index.get(big) is None
    assert any("max_file_size" in record.getMessage() for record in caplog.records)


def test_scan_raises_when_the_vault_root_is_missing(tmp_path: Path) -> None:
    """08 §C1: a vault_dir pointing at the wrong tree must fail loudly."""
    index = VaultIndex(make_config(tmp_path / "nope"), tmp_path / "index.json")
    with pytest.raises(IndexingError) as excinfo:
        index.scan()
    assert "nope" in str(excinfo.value)
    assert excinfo.value.hint is not None


# ---------------------------------------------------------------------------
# record shape (spec 03 §7)
# ---------------------------------------------------------------------------


def test_broken_frontmatter_is_indexed_with_a_parse_error_flag(
    fixture_vault: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    index = make_index(fixture_vault, tmp_path)
    with caplog.at_level(logging.WARNING, logger="organize_core.index"):
        total = index.scan()
    assert total == FIXTURE_MD_TOTAL  # the scan never aborted

    record = index.get(fixture_vault / "capture/raw_capture/broken-yaml.md")
    assert record is not None
    assert record.parse_error is True
    assert record.tags == []
    assert record.metadata == {}
    assert record.title == "broken-yaml"
    assert index.stats()["parse_errors"] == 1

    messages = [record.getMessage() for record in caplog.records]
    assert any("broken-yaml.md" in message for message in messages)


def test_invalid_utf8_bytes_do_not_break_indexing(index: VaultIndex, fixture_vault: Path) -> None:
    record = index.get(fixture_vault / "capture/raw_capture/invalid-utf8.md")
    assert record is not None
    assert record.parse_error is False
    assert record.tags == ["quirk"]
    assert record.id == "bad-bytes"


def test_title_is_first_h1_heading(index: VaultIndex, fixture_vault: Path) -> None:
    record = index.get(fixture_vault / "projects/blog/ideas.md")
    assert record is not None
    assert record.title == "Blog ideas"


def test_title_ignores_headings_inside_fenced_code(fixture_vault: Path, tmp_path: Path) -> None:
    path = write_note(
        fixture_vault,
        "resources/performing/fenced.md",
        "---\nid: fenced\n---\n```\n# not a title\n```\n\n# Real Title\n",
    )
    index = make_index(fixture_vault, tmp_path)
    record = index.update_file(path)
    assert record is not None
    assert record.title == "Real Title"


def test_title_never_comes_from_a_scalar_alias(fixture_vault: Path, tmp_path: Path) -> None:
    """08 §B10: ``aliases[0]`` on the STRING ``"some-alias"`` is ``"s"``."""
    path = write_note(
        fixture_vault,
        "capture/raw_capture/scalar-alias.md",
        "---\nid: scalar-alias\naliases: some-alias\ntags: solo\n---\nno heading here\n",
    )
    index = make_index(fixture_vault, tmp_path)
    record = index.update_file(path)
    assert record is not None
    assert record.title == "scalar-alias"  # filename stem, not "s"
    assert record.aliases == ["some-alias"]  # scalar coerced to a 1-element list
    assert record.tags == ["solo"]


def test_scalar_frontmatter_values_are_coerced_to_lists(index: VaultIndex, fixture_vault: Path) -> None:
    record = index.get(fixture_vault / "capture/raw_capture/scalar-tags.md")
    assert record is not None
    assert record.tags == ["daily_notes"]
    assert record.sources == ["me"]
    assert record.normalized_tags == ["daily-notes"]

    context_record = index.get(fixture_vault / "capture/raw_capture/context-string.md")
    assert context_record is not None
    assert context_record.context == ["at the gym"]


def test_empty_metadata_list_and_map_both_become_a_map(index: VaultIndex, fixture_vault: Path) -> None:
    as_map = index.get(fixture_vault / "capture/raw_capture/metadata-map.md")
    as_list = index.get(fixture_vault / "capture/raw_capture/metadata-list.md")
    assert as_map is not None and as_list is not None
    assert as_map.metadata == {}
    assert as_list.metadata == {}


def test_full_capture_schema_record(index: VaultIndex, fixture_vault: Path) -> None:
    record = index.get(fixture_vault / "capture/raw_capture/2026-06-10T21:33:05.379Z.md")
    assert record is not None
    assert record.timestamp == "2026-06-10T21:37:42.809743+00:00"
    assert record.id == "2026-06-10T21:33:05.379Z"
    assert record.capture_id == "2026-06-10T21:33:05.379Z"
    assert record.aliases == ["2026-06-10T21:33:05.379Z"]
    assert record.tags == ["impro", "creativity"]
    assert record.sources == ["me"]
    assert record.modalities == ["text"]
    assert record.context == []
    assert record.location is not None and record.location["city"] == "New York"
    assert record.processing_status == "raw"
    assert record.created_date == "2026-06-10"
    assert record.last_edited_date == "2026-06-10"
    assert record.para_type == "capture"
    assert record.folder == "raw_capture"
    assert record.size > 0
    assert record.modified > 0
    assert record.indexed_at > 0


def test_unknown_frontmatter_fields_are_kept_in_extra(index: VaultIndex, fixture_vault: Path) -> None:
    record = index.get(fixture_vault / "capture/raw_capture/private-thought.md")
    assert record is not None
    assert record.extra["no-ai"] is True


def test_folder_description_is_loaded_from_the_index_note(index: VaultIndex, fixture_vault: Path) -> None:
    """Spec 11 §3: a folder's NL description lives in its index.md."""
    record = index.get(fixture_vault / "areas/health/index.md")
    assert record is not None
    assert record.description is not None
    assert record.description.startswith("Ongoing health practice")
    assert "resources" in record.description


def test_notes_without_a_description_have_none(index: VaultIndex, fixture_vault: Path) -> None:
    record = index.get(fixture_vault / "resources/performing/impro.md")
    assert record is not None
    assert record.description is None


# ---------------------------------------------------------------------------
# query semantics (spec 03 §2)
# ---------------------------------------------------------------------------


def test_empty_criteria_returns_everything(index: VaultIndex) -> None:
    assert len(index.query(QueryCriteria())) == FIXTURE_MD_TOTAL


def test_tag_filter_is_or_within_and_and_across(index: VaultIndex) -> None:
    """08 §A10 regression: the query stub matched EVERY record."""
    impro = index.query(QueryCriteria(tags=["impro"]))
    assert sorted(names_of(impro)) == ["2026-06-10T21:33:05.379Z.md", "impro.md"]

    either = index.query(QueryCriteria(tags=["impro", "health"]))
    assert sorted(names_of(either)) == [
        "2026-04-08T16:51:24.690160+00:00.md",
        "2026-06-10T21:33:05.379Z.md",
        "impro.md",
    ]

    both_criteria = index.query(QueryCriteria(tags=["impro"], para_type=["capture"]))
    assert names_of(both_criteria) == ["2026-06-10T21:33:05.379Z.md"]


def test_unmatched_filter_returns_nothing(index: VaultIndex) -> None:
    assert index.query(QueryCriteria(tags=["no-such-tag"])) == []
    assert index.query(QueryCriteria(status=["organized"])) == []
    assert index.query(QueryCriteria(sources=["nobody"])) == []


def test_tag_matching_uses_the_shared_normalizer(index: VaultIndex) -> None:
    assert len(index.query(QueryCriteria(tags=["IMPRO"]))) == 2
    assert len(index.query(QueryCriteria(tags=["daily notes"]))) == len(
        index.query(QueryCriteria(tags=["daily_notes"]))
    )


def test_status_and_para_type_filters(index: VaultIndex) -> None:
    raw_captures = index.query(QueryCriteria(status=["raw"], para_type=["capture"]))
    assert len(raw_captures) == FIXTURE_RAW_CAPTURES
    assert {record.processing_status for record in raw_captures} == {"raw"}
    assert {record.para_type for record in raw_captures} == {"capture"}


def test_source_and_modality_filters(index: VaultIndex) -> None:
    assert len(index.query(QueryCriteria(sources=["me"]))) == 4
    assert len(index.query(QueryCriteria(modalities=["text"]))) == 1


def test_text_criterion_is_a_substring_over_title_filename_aliases_tags(index: VaultIndex) -> None:
    assert names_of(index.query(QueryCriteria(text="blog ideas"))) == ["ideas.md"]
    assert names_of(index.query(QueryCriteria(text="café"))) == ["meeting notes — café ☕.md"]


def test_since_until_are_inclusive_bounds(tmp_path: Path) -> None:
    vault = tmp_path / "dated"
    for day in ("2026-01-01", "2026-02-01", "2026-03-01"):
        write_note(
            vault,
            f"capture/raw_capture/{day}-note.md",
            f"---\ntimestamp: '{day}T12:00:00+00:00'\nprocessing_status: raw\n---\nbody\n",
        )
    index = VaultIndex(make_config(vault), tmp_path / "index.json")
    index.scan()

    assert len(index.query(QueryCriteria(since="2026-02-01"))) == 2
    assert len(index.query(QueryCriteria(until="2026-02-01"))) == 2
    assert len(index.query(QueryCriteria(since="2026-02-01", until="2026-02-01"))) == 1
    assert index.query(QueryCriteria(since="2027-01-01")) == []


def test_created_date_is_used_when_timestamp_is_absent(tmp_path: Path) -> None:
    vault = tmp_path / "dated2"
    write_note(
        vault,
        "capture/raw_capture/only-created.md",
        "---\ncreated_date: '2026-05-05'\nprocessing_status: raw\n---\nbody\n",
    )
    index = VaultIndex(make_config(vault), tmp_path / "index.json")
    index.scan()
    assert len(index.query(QueryCriteria(since="2026-05-05", until="2026-05-05"))) == 1
    assert index.query(QueryCriteria(until="2026-05-04")) == []


def test_ordering_is_oldest_first_with_a_path_tiebreak(tmp_path: Path) -> None:
    vault = tmp_path / "ordered"
    write_note(
        vault,
        "capture/raw_capture/b-same.md",
        "---\ntimestamp: '2026-02-02T00:00:00+00:00'\n---\nb\n",
    )
    write_note(
        vault,
        "capture/raw_capture/a-same.md",
        "---\ntimestamp: '2026-02-02T00:00:00+00:00'\n---\na\n",
    )
    write_note(
        vault,
        "capture/raw_capture/older.md",
        "---\ntimestamp: '2026-01-01T00:00:00+00:00'\n---\nolder\n",
    )
    index = VaultIndex(make_config(vault), tmp_path / "index.json")
    index.scan()
    assert names_of(index.query(QueryCriteria())) == ["older.md", "a-same.md", "b-same.md"]


def test_undated_notes_fall_back_to_mtime(tmp_path: Path) -> None:
    vault = tmp_path / "mtimes"
    first = write_note(vault, "capture/raw_capture/first.md", "no frontmatter\n")
    second = write_note(vault, "capture/raw_capture/second.md", "no frontmatter\n")
    os.utime(first, (1_700_000_000, 1_700_000_000))
    os.utime(second, (1_600_000_000, 1_600_000_000))
    index = VaultIndex(make_config(vault), tmp_path / "index.json")
    index.scan()
    assert names_of(index.query(QueryCriteria())) == ["second.md", "first.md"]


# ---------------------------------------------------------------------------
# QueryCriteria.from_filter_args (08 §A36 string-vs-array)
# ---------------------------------------------------------------------------


def test_from_filter_args_accepts_strings_and_lists() -> None:
    from_string = QueryCriteria.from_filter_args({"tags": "impro,creativity"})
    from_list = QueryCriteria.from_filter_args({"tags": ["impro", "creativity"]})
    assert from_string.tags == ["impro", "creativity"]
    assert from_list.tags == from_string.tags


def test_from_filter_args_full_criteria_set() -> None:
    criteria = QueryCriteria.from_filter_args(
        {
            "tags": "a, b ,",
            "sources": "me",
            "modalities": "text,audio",
            "status": "raw",
            "para_type": "capture",
            "since": "2026-01-01",
            "until_date": "2026-12-31",
            "text": "meeting notes",
        }
    )
    assert criteria.tags == ["a", "b"]
    assert criteria.sources == ["me"]
    assert criteria.modalities == ["text", "audio"]
    assert criteria.status == ["raw"]
    assert criteria.para_type == ["capture"]
    assert criteria.since == "2026-01-01"
    assert criteria.until == "2026-12-31"  # until_date is the documented alias
    assert criteria.text == "meeting notes"  # never comma-split


def test_from_filter_args_rejects_unknown_keys_loudly() -> None:
    with pytest.raises(ConfigError) as excinfo:
        QueryCriteria.from_filter_args({"tagz": "impro"})
    assert "tagz" in str(excinfo.value)
    assert excinfo.value.hint is not None and "tags" in excinfo.value.hint


def test_from_filter_args_rejects_malformed_dates() -> None:
    with pytest.raises(ConfigError) as excinfo:
        QueryCriteria.from_filter_args({"since": "last tuesday"})
    assert "since" in str(excinfo.value)


def test_from_filter_args_defaults_are_empty() -> None:
    criteria = QueryCriteria.from_filter_args({})
    assert criteria == QueryCriteria()


# ---------------------------------------------------------------------------
# search / browse / completion
# ---------------------------------------------------------------------------


def test_search_matches_title_filename_aliases_and_tags(index: VaultIndex) -> None:
    assert names_of(index.search("blog ideas")) == ["ideas.md"]
    assert names_of(index.search("café")) == ["meeting notes — café ☕.md"]
    assert names_of(index.search("21:33:05")) == ["2026-06-10T21:33:05.379Z.md"]
    assert len(index.search("impro")) == 2  # tag on one capture, tag+name on the resource


def test_search_scope_restricts_to_a_subtree(index: VaultIndex, fixture_vault: Path) -> None:
    assert names_of(index.search("impro", scope=fixture_vault / "resources")) == ["impro.md"]
    assert index.search("impro", scope=fixture_vault / "projects") == []


def test_empty_search_returns_nothing(index: VaultIndex) -> None:
    assert index.search("") == []
    assert index.search("   ") == []


def test_para_subfolders_are_the_candidate_source(index: VaultIndex, fixture_vault: Path) -> None:
    assert index.para_subfolders("projects") == [
        fixture_vault / "projects/blog",
        fixture_vault / "projects/kms",
    ]
    assert index.para_subfolders("areas") == [
        fixture_vault / "areas/health",
        fixture_vault / "areas/relationships",
    ]
    # the singular ParaType is accepted too (server/CLI callers use both), and
    # ignore_patterns apply here as well: resources/flashcards is excluded.
    assert index.para_subfolders("resource") == [
        fixture_vault / "resources/answers",
        fixture_vault / "resources/performing",
    ]
    assert index.para_subfolders("archives") == [fixture_vault / "archive/capture"]


def test_para_subfolders_of_a_missing_root_is_empty_and_warns(
    fixture_vault: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    index = make_index(fixture_vault, tmp_path, para_folders={"projects": "nope"})
    with caplog.at_level(logging.WARNING, logger="organize_core.index"):
        assert index.para_subfolders("projects") == []
    assert any("does not exist" in record.getMessage() for record in caplog.records)


def test_para_subfolders_rejects_an_unknown_type(index: VaultIndex) -> None:
    with pytest.raises(ConfigError):
        index.para_subfolders("nonsense")


# ---------------------------------------------------------------------------
# candidate folders / candidate notes (spec 21 §2, §3.1)
# ---------------------------------------------------------------------------


def _deep_vault(fixture_vault: Path) -> Path:
    """A PARA tree with known depths, so every count below is hand-counted.

    ``areas``: a1(1) a1/b1(2) a1/b1/c1(3) a1/b1/c1/d1(4) plus the fixture's
    health(1) and relationships(1); one dot-directory and one ignorable
    directory, both at depth 1.
    """
    for rel in (
        "areas/a1/b1/c1/d1",
        "areas/.hidden/deep",
        "areas/node_modules/deep",
        "projects/p1/q1",
        "archive/old/deeper",
    ):
        (fixture_vault / rel).mkdir(parents=True, exist_ok=True)
    return fixture_vault


def test_candidate_folders_depth_is_levels_below_the_para_root(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Spec 21 §7.7: the off-by-one in "levels below the PARA root, immediate
    child = 1" is the likeliest silent bug here, so it is asserted directly at
    every shipped setting against hand-counted literals."""
    vault = _deep_vault(fixture_vault)
    index = make_index(vault, tmp_path, ignore_patterns=["node_modules"])

    def offered(depth: object) -> list[str]:
        return sorted(
            str(p.relative_to(vault))
            for p in index.candidate_folders("areas", depth)  # type: ignore[arg-type]
        )

    assert offered(1) == ["areas/a1", "areas/health", "areas/relationships"]
    assert offered(2) == ["areas/a1", "areas/a1/b1", "areas/health", "areas/relationships"]
    assert offered(3) == [
        "areas/a1",
        "areas/a1/b1",
        "areas/a1/b1/c1",
        "areas/health",
        "areas/relationships",
    ]
    assert offered("all") == [
        "areas/a1",
        "areas/a1/b1",
        "areas/a1/b1/c1",
        "areas/a1/b1/c1/d1",
        "areas/health",
        "areas/relationships",
    ]


def test_candidate_folders_at_depth_1_is_exactly_para_subfolders(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Depth 1 reproduces today's ballot exactly (21 §3.6's parity half) —
    the same paths, not merely a similar count."""
    vault = _deep_vault(fixture_vault)
    index = make_index(vault, tmp_path)
    for key in ("projects", "areas", "resources"):
        assert sorted(index.candidate_folders(key, 1)) == sorted(index.para_subfolders(key))


def test_candidate_folders_gap_from_a_direct_walk_is_ignore_patterns_alone(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Spec 21 §7.7's attribution clause: a walk that silently stopped
    honoring ``ignore_patterns`` would look like a recall win, so the
    difference between the raw filesystem and the accessor is asserted to be
    EXACTLY the ignored directories, by name."""
    vault = _deep_vault(fixture_vault)
    index = make_index(vault, tmp_path, ignore_patterns=["node_modules"])

    on_disk = {
        str(p.relative_to(vault))
        for p in (vault / "areas").rglob("*")
        if p.is_dir() and not any(part.startswith(".") for part in p.relative_to(vault).parts)
    }
    offered = {str(p.relative_to(vault)) for p in index.candidate_folders("areas", "all")}
    assert on_disk - offered == {"areas/node_modules", "areas/node_modules/deep"}
    assert offered - on_disk == set()

    # Firing control: without the pattern the same walk offers them.
    unfiltered = make_index(vault, tmp_path, ignore_patterns=[])
    assert (
        on_disk - {str(p.relative_to(vault)) for p in unfiltered.candidate_folders("areas", "all")}
        == set()
    )


def test_candidate_folders_never_offers_the_archives_root_at_any_depth(
    fixture_vault: Path, tmp_path: Path
) -> None:
    vault = _deep_vault(fixture_vault)
    index = make_index(vault, tmp_path)
    for depth in (1, 2, 3, "all"):
        assert index.candidate_folders("archives", depth) == []
        assert index.candidate_folders("archive", depth) == []
    # …while `para_subfolders` — the BROWSING accessor — still lists them.
    assert index.para_subfolders("archives") == [vault / "archive/capture", vault / "archive/old"]


def test_candidate_folders_reads_the_configured_depth_when_none_is_passed(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """``suggestions.max_candidate_depth`` is honored (14 §4.3): the same call
    answers differently at 1 and at 3, and the shipped default is 3."""
    from organize_core.config import SuggestionsConfig

    vault = _deep_vault(fixture_vault)

    def index_at(depth: object) -> VaultIndex:
        config = Config(
            vault=VaultConfig(root=vault),
            suggestions=SuggestionsConfig(max_candidate_depth=depth),  # type: ignore[arg-type]
        )
        idx = VaultIndex(config, tmp_path / f"state-{depth}" / "index.json")
        idx.load()
        return idx

    # No ignore_patterns here, so `node_modules` (and at depth 3 its child)
    # are on the ballot too: 1 → {a1, health, relationships, node_modules};
    # 3 → those plus {a1/b1, a1/b1/c1, node_modules/deep}.
    assert len(index_at(1).candidate_folders("areas")) == 4
    assert len(index_at(3).candidate_folders("areas")) == 7
    assert SuggestionsConfig().max_candidate_depth == 3
    assert len(make_index(vault, tmp_path).candidate_folders("areas")) == 7


def test_candidate_folders_rejects_a_depth_that_is_not_a_depth(
    fixture_vault: Path, tmp_path: Path
) -> None:
    index = make_index(fixture_vault, tmp_path)
    for bad in (0, -1, "deep", "3.5", True):
        with pytest.raises(ConfigError):
            index.candidate_folders("areas", bad)  # type: ignore[arg-type]
    # Firing control: the legal values do not raise.
    assert index.candidate_folders("areas", 1) == [
        fixture_vault / "areas/health",
        fixture_vault / "areas/relationships",
    ]
    assert index.candidate_folders("areas", "all") is not None


def test_candidate_folders_rejects_an_unknown_para_type(index: VaultIndex) -> None:
    with pytest.raises(ConfigError):
        index.candidate_folders("nonsense")


def test_candidate_folder_walk_is_cached_and_invalidated(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Spec 21 §2.4: the walk happens once per candidate-set build, and
    ``folder.create`` invalidates it. A cache with no invalidation would keep
    a freshly created folder off the ballot for the life of the server."""
    vault = _deep_vault(fixture_vault)
    index = make_index(vault, tmp_path)
    first = index.candidate_folders("areas", 1)
    (vault / "areas/brand-new").mkdir()

    # Still cached — the accessor did NOT re-walk.
    assert index.candidate_folders("areas", 1) == first
    assert vault / "areas/brand-new" not in first

    generation = index.candidate_generation
    index.invalidate_folder_cache()
    assert index.candidate_generation > generation
    assert vault / "areas/brand-new" in index.candidate_folders("areas", 1)


def test_candidate_folders_hands_out_a_copy(fixture_vault: Path, tmp_path: Path) -> None:
    """A caller that mutates the returned list must not corrupt the cache."""
    index = make_index(fixture_vault, tmp_path)
    first = index.candidate_folders("areas", 1)
    first.clear()
    assert index.candidate_folders("areas", 1) == [
        fixture_vault / "areas/health",
        fixture_vault / "areas/relationships",
    ]


def test_candidate_generation_moves_when_a_note_changes(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The NOTE half of the ballot rides the same counter — a generation that
    tracked only directories would serve a cached candidate set still holding
    a deleted note."""
    index = make_index(fixture_vault, tmp_path)
    index.scan()
    generation = index.candidate_generation
    note = write_note(fixture_vault, "areas/health/new-note.md", "---\ntags: [x]\n---\nbody\n")
    index.update_file(note)
    assert index.candidate_generation > generation
    after_add = index.candidate_generation
    index.remove_file(note)
    assert index.candidate_generation > after_add


def test_candidate_notes_are_the_non_archive_para_notes(index: VaultIndex) -> None:
    """Spec 21 §3.1: captures, archives and ``other`` are never note
    candidates — an exact set, not a count."""
    records = index.candidate_notes()
    assert {record.para_type for record in records} == {"project", "area", "resource"}
    assert sorted(Path(record.path).name for record in records) == [
        "ideas.md",
        "impro.md",
        "index.md",
    ]
    assert [r.path for r in records] == sorted(r.path for r in records)


def test_folder_children_returns_subdirs_and_direct_notes(
    index: VaultIndex, fixture_vault: Path
) -> None:
    subdirs, notes = index.folder_children(fixture_vault / "projects")
    assert subdirs == [fixture_vault / "projects/blog", fixture_vault / "projects/kms"]
    assert notes == []

    subdirs, notes = index.folder_children(fixture_vault / "projects/blog")
    assert subdirs == []
    assert names_of(notes) == ["ideas.md"]

    subdirs, notes = index.folder_children(fixture_vault / "capture/raw_capture")
    assert subdirs == [fixture_vault / "capture/raw_capture/media"]
    assert len(notes) == 16  # direct children only, media/ is not recursed


def test_values_of_known_and_arbitrary_keys(fixture_vault: Path, tmp_path: Path) -> None:
    """Spec 07 ``complete = "existing"`` must work for CONFIGURED metadata
    fields (arbitrary frontmatter keys), not only the capture schema."""
    write_note(
        fixture_vault,
        "capture/raw_capture/annotated.md",
        "---\nid: annotated\ntags:\n- impro\nimportance: high\n---\nbody\n",
    )
    write_note(
        fixture_vault,
        "capture/raw_capture/annotated2.md",
        "---\nid: annotated2\nimportance: low\n---\nbody\n",
    )
    index = make_index(fixture_vault, tmp_path)
    index.scan()

    assert index.values_of("importance") == ["high", "low"]
    assert index.values_of("sources") == ["me"]
    assert index.values_of("modalities") == ["text"]
    assert index.values_of("processing_status") == ["raw"]
    assert index.values_of("tags") == [
        "area",
        "blog-idea",
        "creativity",
        "daily_notes",
        "health",
        "impro",
        "journal",
        "meeting",
        "project:blog",
        "question",
        "quirk",
        "todo",
        "workout",
    ]
    assert index.values_of("nothing-uses-this") == []
    assert index.values_of("") == []


# ---------------------------------------------------------------------------
# incremental updates (spec 03 §7, 09 §4)
# ---------------------------------------------------------------------------


def test_update_file_adds_then_refreshes_a_record(index: VaultIndex, fixture_vault: Path) -> None:
    path = write_note(
        fixture_vault, "projects/kms/new.md", "---\nid: new\ntags:\n- kms\n---\n# New note\n"
    )
    record = index.update_file(path)
    assert record is not None
    assert record.title == "New note"
    assert index.stats()["total"] == FIXTURE_MD_TOTAL + 1

    path.write_text("---\nid: new\ntags:\n- kms\n- edited\n---\n# Renamed\n", encoding="utf-8")
    refreshed = index.update_file(path)
    assert refreshed is not None
    assert refreshed.tags == ["kms", "edited"]
    assert refreshed.title == "Renamed"
    assert index.stats()["total"] == FIXTURE_MD_TOTAL + 1  # replaced, not duplicated


def test_update_file_removes_the_entry_when_the_file_is_gone(
    index: VaultIndex, fixture_vault: Path
) -> None:
    path = fixture_vault / "resources/performing/impro.md"
    assert index.get(path) is not None
    path.unlink()
    assert index.update_file(path) is None
    assert index.get(path) is None
    assert index.stats()["total"] == FIXTURE_MD_TOTAL - 1


def test_remove_file_is_idempotent(index: VaultIndex, fixture_vault: Path) -> None:
    path = fixture_vault / "resources/performing/impro.md"
    index.remove_file(path)
    index.remove_file(path)
    assert index.get(path) is None
    assert index.stats()["total"] == FIXTURE_MD_TOTAL - 1


def test_update_file_refuses_paths_outside_the_vault_root(
    index: VaultIndex, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """08 §C5: the live index accumulated entries under a wrong root."""
    outsider = tmp_path / "elsewhere" / "note.md"
    outsider.parent.mkdir(parents=True, exist_ok=True)
    outsider.write_text("---\nid: outsider\n---\nbody\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="organize_core.index"):
        assert index.update_file(outsider) is None
    assert index.get(outsider) is None
    assert any("outside the vault root" in record.getMessage() for record in caplog.records)


def test_update_file_ignores_non_markdown_and_ignored_paths(
    index: VaultIndex, fixture_vault: Path
) -> None:
    """The incremental path applies the same admission rule as the walk."""
    assert index.update_file(fixture_vault / "capture/raw_capture/voice-memo.wav") is None
    assert index.update_file(fixture_vault / ".obsidian/app.json") is None
    hidden = write_note(fixture_vault, ".trash/deleted.md", "---\nid: trashed\n---\nx\n")
    assert index.update_file(hidden) is None
    flashcard = write_note(
        fixture_vault, "resources/flashcards/review/card.md", "---\nid: card\n---\nx\n"
    )
    assert index.update_file(flashcard) is None
    assert index.stats()["total"] == FIXTURE_MD_TOTAL


def test_scan_prunes_records_whose_files_disappeared(index: VaultIndex, fixture_vault: Path) -> None:
    (fixture_vault / "resources/performing/impro.md").unlink()
    assert index.scan() == FIXTURE_MD_TOTAL - 1
    assert index.stats()["total"] == FIXTURE_MD_TOTAL - 1


def test_get_accepts_relative_and_absolute_paths(index: VaultIndex, fixture_vault: Path) -> None:
    absolute = index.get(fixture_vault / "projects/blog/ideas.md")
    relative = index.get("projects/blog/ideas.md")
    assert absolute is not None
    assert relative is absolute
    assert index.get("projects/blog/missing.md") is None


def test_stats_reports_the_capture_backlog(index: VaultIndex) -> None:
    stats = index.stats()
    assert stats["capture_backlog"] == FIXTURE_RAW_CAPTURES
    assert stats["total"] == FIXTURE_MD_TOTAL
    assert stats["parse_errors"] == 1
