"""Unit tests for the ONE frontmatter module (spec 02, 03 §8, 05 §9, 08).

Every assertion here is an exact value or an exact string — the old suite's
``> 0`` style assertions are what let 08's defects ship (08 §A36).
"""

from __future__ import annotations

import pytest

from conftest import QUIRK_FILES
from organize_core.errors import FrontmatterError
from organize_core.frontmatter import (
    KNOWN_FIELD_ORDER,
    Document,
    Frontmatter,
    is_no_ai,
    load_file,
    merge_sources,
    merge_tags,
    normalize_tag,
    parse,
    serialize,
    split_frontmatter,
)

# --------------------------------------------------------------------------
# split_frontmatter — line-based extraction (03 §8, 08 §B16)
# --------------------------------------------------------------------------


def test_split_returns_block_and_body() -> None:
    fm, body = split_frontmatter("---\nid: x\n---\nbody line\n")
    assert fm == "id: x\n"
    assert body == "body line\n"


def test_split_no_frontmatter_when_first_line_is_not_delimiter() -> None:
    text = "# Heading\n\n---\n\nrule\n"
    assert split_frontmatter(text) == (None, text)


def test_split_tolerates_trailing_whitespace_on_delimiters() -> None:
    fm, body = split_frontmatter("---  \nid: x\n---\t\nbody\n")
    assert fm == "id: x\n"
    assert body == "body\n"


def test_split_requires_delimiter_at_column_zero() -> None:
    """An indented ``---`` (e.g. inside a block scalar) is not a terminator."""
    text = "---\ndesc: |\n  ---\n  still the value\n---\nbody\n"
    fm, body = split_frontmatter(text)
    assert fm == "desc: |\n  ---\n  still the value\n"
    assert body == "body\n"


def test_split_unterminated_block_is_all_body() -> None:
    text = "---\nid: x\nno terminator here\n"
    assert split_frontmatter(text) == (None, text)


def test_split_empty_text_never_crashes() -> None:
    assert split_frontmatter("") == (None, "")


def test_split_empty_block() -> None:
    assert split_frontmatter("---\n---\nbody\n") == ("", "body\n")


# --------------------------------------------------------------------------
# parse — quirk tolerance (02) + loud failure (09 §1.5)
# --------------------------------------------------------------------------


def test_parse_current_capture_schema(fixture_vault) -> None:
    doc = load_file(fixture_vault / QUIRK_FILES["current_schema"])
    fields = doc.frontmatter.fields
    assert list(fields) == [
        "timestamp",
        "id",
        "aliases",
        "capture_id",
        "modalities",
        "context",
        "sources",
        "tags",
        "location",
        "metadata",
        "processing_status",
        "created_date",
        "last_edited_date",
    ]
    assert fields["tags"] == ["impro", "creativity"]
    assert fields["context"] == []
    assert fields["metadata"] == {}
    assert fields["location"] == {
        "latitude": 40.7126,
        "longitude": -74.0066,
        "city": "New York",
        "country": "United States",
        "timezone": "America/New_York",
    }
    assert doc.body == "## Content\nAn idea about improv warmups and creative flow.\n"


def test_parse_keeps_timestamps_as_strings(fixture_vault) -> None:
    """06 §4: a YAML loader must never coerce these to datetime/date."""
    fields = load_file(fixture_vault / QUIRK_FILES["current_schema"]).frontmatter.fields
    assert fields["timestamp"] == "2026-06-10T21:37:42.809743+00:00"
    assert isinstance(fields["timestamp"], str)
    assert fields["created_date"] == "2026-06-10"
    assert isinstance(fields["created_date"], str)
    assert isinstance(fields["last_edited_date"], str)


def test_parse_unquoted_date_also_stays_a_string() -> None:
    fields = parse("---\ncreated_date: 2026-06-10\n---\nb\n").frontmatter.fields
    assert fields["created_date"] == "2026-06-10"
    assert isinstance(fields["created_date"], str)


def test_parse_scalar_tags_and_sources(fixture_vault) -> None:
    doc = load_file(fixture_vault / QUIRK_FILES["scalar_tags"])
    assert doc.frontmatter.fields["tags"] == "daily_notes"
    assert doc.frontmatter.fields["sources"] == "me"
    assert doc.frontmatter.fields["title"] == "Older manual note"


def test_parse_metadata_empty_map_vs_empty_list(fixture_vault) -> None:
    as_map = load_file(fixture_vault / QUIRK_FILES["metadata_empty_map"])
    as_list = load_file(fixture_vault / QUIRK_FILES["metadata_empty_list"])
    assert as_map.frontmatter.fields["metadata"] == {}
    assert isinstance(as_map.frontmatter.fields["metadata"], dict)
    assert as_list.frontmatter.fields["metadata"] == []
    assert isinstance(as_list.frontmatter.fields["metadata"], list)


def test_parse_context_as_string(fixture_vault) -> None:
    doc = load_file(fixture_vault / QUIRK_FILES["context_as_string"])
    assert doc.frontmatter.fields["context"] == "at the gym"


def test_parse_no_frontmatter_at_all(fixture_vault) -> None:
    doc = load_file(fixture_vault / QUIRK_FILES["no_frontmatter"])
    assert doc.frontmatter is None
    assert doc.has_frontmatter is False
    assert doc.body == "Just a plain markdown body, no frontmatter block.\n"


def test_parse_empty_block_is_not_absent_frontmatter() -> None:
    doc = parse("---\n---\nbody\n")
    assert doc.has_frontmatter is True
    assert doc.frontmatter.fields == {}


def test_parse_unicode_and_smart_quotes(fixture_vault) -> None:
    doc = load_file(fixture_vault / QUIRK_FILES["unicode_spaces"])
    assert doc.frontmatter.fields["id"] == "unicode-cafe"
    assert doc.body == "Notes from the café — “smart quotes” included.\n"


def test_parse_broken_yaml_raises_loudly(fixture_vault) -> None:
    path = fixture_vault / QUIRK_FILES["broken_yaml"]
    with pytest.raises(FrontmatterError) as excinfo:
        load_file(path)
    message = str(excinfo.value)
    assert str(path) in message, "the error must name the file (09 §1.5)"
    assert excinfo.value.hint, "user-fixable errors carry a hint"


def test_parse_broken_yaml_is_catchable_so_scans_continue(fixture_vault) -> None:
    """03 §7: the indexer catches this and indexes with empty metadata."""
    indexed: dict[str, dict] = {}
    for rel in (QUIRK_FILES["broken_yaml"], QUIRK_FILES["scalar_tags"]):
        try:
            doc = load_file(fixture_vault / rel)
        except FrontmatterError:
            indexed[rel] = {}
            continue
        indexed[rel] = dict(doc.frontmatter.fields) if doc.frontmatter else {}
    assert indexed[QUIRK_FILES["broken_yaml"]] == {}
    assert indexed[QUIRK_FILES["scalar_tags"]]["tags"] == "daily_notes"


def test_parse_non_mapping_frontmatter_raises() -> None:
    with pytest.raises(FrontmatterError):
        parse("---\n- just\n- a list\n---\nbody\n")


def test_load_file_decodes_invalid_utf8_with_replacement(fixture_vault) -> None:
    """06 §6 / 08 §B18: invalid bytes must not crash a scan."""
    doc = load_file(fixture_vault / QUIRK_FILES["invalid_utf8"])
    assert doc.frontmatter.fields == {"id": "bad-bytes", "tags": ["quirk"]}
    assert doc.body == "broken �� bytes inline\n"


# --------------------------------------------------------------------------
# 08 §B16 — '---' inside values and as a body horizontal rule
# --------------------------------------------------------------------------


def test_dashes_inside_values_and_body_survive(fixture_vault) -> None:
    path = fixture_vault / QUIRK_FILES["dashes_in_values"]
    original = path.read_text(encoding="utf-8")
    doc = load_file(path)
    assert doc.frontmatter.fields["title"] == "section --- with dashes"
    assert doc.frontmatter.fields["context"] == "before --- after"
    assert doc.body == (
        "Body starts here.\n"
        "\n"
        "---\n"
        "\n"
        "Text after a horizontal rule that a substring split would eat.\n"
    )
    assert serialize(doc) == original


def test_substring_split_would_have_been_wrong(fixture_vault) -> None:
    """Pins the exact defect: ``text.split('---', 2)`` truncates this file."""
    text = (fixture_vault / QUIRK_FILES["dashes_in_values"]).read_text(encoding="utf-8")
    naive_parts = text.split("---", 2)
    naive_fm, naive_body = naive_parts[1], naive_parts[2]
    doc = parse(text)

    # The substring split cuts inside ``title: 'section --- with dashes'``:
    # the block is truncated and the rest of the YAML leaks into the body.
    assert "tags" not in naive_fm
    assert naive_body.startswith(" with dashes'")
    assert "processing_status: raw" in naive_body

    # The line-based reader gets both halves right.
    assert "tags" in "".join(doc.frontmatter.fields)
    assert doc.body.startswith("Body starts here.")
    assert "processing_status" not in doc.body


# --------------------------------------------------------------------------
# 08 §A12 — the whitelist data-loss bug
# --------------------------------------------------------------------------


def test_unknown_fields_survive_a_rewrite(fixture_vault) -> None:
    path = fixture_vault / QUIRK_FILES["merge_target"]
    doc = load_file(path)
    doc.frontmatter.fields["tags"] = merge_tags(
        doc.frontmatter.get_list("tags"), ["project/blog"]
    )
    doc.frontmatter.fields["last_edited_date"] = "2026-08-15"
    assert serialize(doc) == (
        "---\n"
        "title: Blog ideas\n"
        "aliases:\n"
        "- ideas\n"
        "author: Matt Handzel\n"
        "tags:\n"
        "- blog-idea\n"
        "- project/blog\n"
        "created_date: '2025-11-02'\n"
        "last_edited_date: '2026-08-15'\n"
        "---\n"
        "# Blog ideas\n"
        "\n"
        "## Inbox\n"
        "- existing idea one\n"
    )


def test_no_ai_field_survives_a_rewrite(fixture_vault) -> None:
    """The vault-law field is exactly the kind the old whitelist destroyed."""
    doc = load_file(fixture_vault / QUIRK_FILES["no_ai"])
    doc.frontmatter.fields["processing_status"] = "organized"
    out = serialize(doc)
    assert "no-ai: true\n" in out
    assert out == (
        "---\n"
        "id: private-thought\n"
        "no-ai: true\n"
        "tags:\n"
        "- journal\n"
        "processing_status: organized\n"
        "---\n"
        "Automated tooling must never write to this note.\n"
    )


def test_every_non_whitelisted_field_survives() -> None:
    """A12 in the general case: nothing outside KNOWN_FIELD_ORDER is dropped."""
    text = (
        "---\n"
        "title: Kept\n"
        "no-ai: true\n"
        "author: Matt\n"
        "cssclasses:\n"
        "- wide\n"
        "publish: false\n"
        "importance: high\n"
        "tags:\n"
        "- a\n"
        "---\n"
        "body\n"
    )
    doc = parse(text)
    doc.frontmatter.fields["tags"] = ["a", "b"]
    out = serialize(doc)
    for line in (
        "title: Kept\n",
        "no-ai: true\n",
        "author: Matt\n",
        "cssclasses:\n- wide\n",
        "publish: false\n",
        "importance: high\n",
    ):
        assert line in out
    unknown = [k for k in parse(out).frontmatter.fields if k not in KNOWN_FIELD_ORDER]
    assert unknown == ["title", "no-ai", "author", "cssclasses", "publish", "importance"]


# --------------------------------------------------------------------------
# serialize — ordering, type stability, style preservation
# --------------------------------------------------------------------------


def test_new_known_field_lands_in_canonical_position() -> None:
    doc = parse("---\nid: x\ntags:\n- a\ncreated_date: '2026-01-01'\n---\nb\n")
    doc.frontmatter.fields["processing_status"] = "organized"
    doc.frontmatter.fields["timestamp"] = "2026-01-01T00:00:00Z"
    assert list(parse(serialize(doc)).frontmatter.fields) == [
        "timestamp",
        "id",
        "tags",
        "processing_status",
        "created_date",
    ]


def test_fresh_document_renders_canonical_order_then_unknowns() -> None:
    doc = Document(
        Frontmatter(
            fields={
                "author": "Matt",
                "tags": ["x"],
                "id": "1",
                "timestamp": "2026-01-01T00:00:00Z",
                "zeta": 1,
            }
        ),
        "body\n",
    )
    assert serialize(doc) == (
        "---\n"
        "timestamp: '2026-01-01T00:00:00Z'\n"
        "id: '1'\n"
        "tags:\n"
        "- x\n"
        "author: Matt\n"
        "zeta: 1\n"
        "---\n"
        "body\n"
    )


def test_serialize_never_invents_a_block() -> None:
    assert serialize(Document(None, "just a body\n")) == "just a body\n"
    assert serialize(Document(Frontmatter(), "just a body\n")) == "just a body\n"


def test_serialize_adds_a_block_to_a_note_that_had_none() -> None:
    """"…unless fields were added" — the metadata-editing path (07)."""
    doc = parse("A note with no frontmatter.\n")
    assert doc.frontmatter is None
    doc.frontmatter = Frontmatter(fields={"tags": ["a"], "processing_status": "raw"})
    out = serialize(doc)
    assert out == (
        "---\ntags:\n- a\nprocessing_status: raw\n---\nA note with no frontmatter.\n"
    )
    assert parse(out).body == "A note with no frontmatter.\n"


def test_load_file_propagates_io_errors(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_file(tmp_path / "does-not-exist.md")


def test_serialize_keeps_an_existing_empty_block() -> None:
    assert serialize(parse("---\n---\nbody\n")) == "---\n---\nbody\n"


def test_deleting_every_field_leaves_an_empty_block() -> None:
    doc = parse("---\nid: x\n---\nbody\n")
    doc.frontmatter.fields.clear()
    assert serialize(doc) == "---\n---\nbody\n"


def test_removing_one_field_keeps_the_rest_byte_identical() -> None:
    doc = parse("---\nid: x\ndrop_me: 1\ntags:\n- a\n---\nbody\n")
    del doc.frontmatter.fields["drop_me"]
    assert serialize(doc) == "---\nid: x\ntags:\n- a\n---\nbody\n"


def test_untouched_scalar_field_never_becomes_a_list(fixture_vault) -> None:
    """03 §8: a populated field never changes type behind the user's back."""
    doc = load_file(fixture_vault / QUIRK_FILES["scalar_tags"])
    doc.frontmatter.fields["processing_status"] = "organized"
    out = serialize(doc)
    assert "tags: daily_notes\n" in out
    assert parse(out).frontmatter.fields["tags"] == "daily_notes"


def test_empty_container_types_are_preserved_on_rewrite(fixture_vault) -> None:
    for quirk, expected in (
        ("metadata_empty_map", "metadata: {}\n"),
        ("metadata_empty_list", "metadata: []\n"),
    ):
        doc = load_file(fixture_vault / QUIRK_FILES[quirk])
        doc.frontmatter.fields["processing_status"] = "organized"
        assert expected in serialize(doc)


def test_flow_list_style_is_preserved_when_the_value_changes() -> None:
    doc = parse("---\ntags: [a, b]\nid: x\n---\nbody\n")
    doc.frontmatter.fields["tags"] = ["a", "b", "c"]
    assert serialize(doc) == "---\ntags: [a, b, c]\nid: x\n---\nbody\n"


def test_block_list_style_is_preserved_when_the_value_changes() -> None:
    doc = parse("---\ntags:\n- a\nid: x\n---\nbody\n")
    doc.frontmatter.fields["tags"] = ["a", "b"]
    assert serialize(doc) == "---\ntags:\n- a\n- b\nid: x\n---\nbody\n"


def test_quote_style_is_preserved_when_a_scalar_changes() -> None:
    doc = parse("---\nid: 'old'\ntitle: plain\n---\nbody\n")
    doc.frontmatter.fields["id"] = "new"
    doc.frontmatter.fields["title"] = "still plain"
    assert serialize(doc) == "---\nid: 'new'\ntitle: still plain\n---\nbody\n"


def test_single_quotes_inside_a_requoted_value_are_escaped() -> None:
    doc = parse("---\ntitle: 'a'\n---\nbody\n")
    doc.frontmatter.fields["title"] = "Matt's note"
    out = serialize(doc)
    assert out == "---\ntitle: 'Matt''s note'\n---\nbody\n"
    assert parse(out).frontmatter.fields["title"] == "Matt's note"


def test_comments_and_blank_lines_survive_a_neighbouring_edit() -> None:
    text = (
        "---\n"
        "# leading comment\n"
        "id: x\n"
        "\n"
        "# about tags\n"
        "tags:\n"
        "- a\n"
        "author: Matt\n"
        "---\n"
        "body\n"
    )
    doc = parse(text)
    doc.frontmatter.fields["author"] = "Matt H"
    assert serialize(doc) == text.replace("author: Matt\n", "author: Matt H\n")


def test_crlf_line_endings_are_preserved() -> None:
    text = "---\r\nid: x\r\ntags:\r\n- a\r\n---\r\nbody\r\n"
    doc = parse(text)
    assert doc.frontmatter.fields == {"id": "x", "tags": ["a"]}
    assert serialize(doc) == text


def test_file_without_trailing_newline_round_trips() -> None:
    text = "---\nid: x\n---"
    assert serialize(parse(text)) == text


def test_same_line_flow_mapping_round_trips_and_rebuilds_safely() -> None:
    text = "---\n{a: 1, b: 2}\n---\nbody\n"
    doc = parse(text)
    assert doc.frontmatter.fields == {"a": 1, "b": 2}
    assert serialize(doc) == text
    doc.frontmatter.fields["b"] = 3
    assert serialize(doc) == "---\na: 1\nb: 3\n---\nbody\n"


def test_duplicate_keys_do_not_corrupt_the_block() -> None:
    text = "---\nid: first\nid: second\ntags:\n- a\n---\nbody\n"
    doc = parse(text)
    assert doc.frontmatter.fields["id"] == "second"
    assert serialize(doc) == text
    doc.frontmatter.fields["id"] = "third"
    rebuilt = serialize(doc)
    assert rebuilt == "---\nid: third\ntags:\n- a\n---\nbody\n"


def test_boolean_is_not_confused_with_one() -> None:
    """``True == 1`` in Python — the change detector must still notice."""
    doc = parse("---\nno-ai: true\n---\nbody\n")
    doc.frontmatter.fields["no-ai"] = 1
    assert serialize(doc) == "---\nno-ai: 1\n---\nbody\n"


def test_body_is_never_touched_by_a_frontmatter_edit(fixture_vault) -> None:
    path = fixture_vault / QUIRK_FILES["current_schema"]
    original_body = load_file(path).body
    doc = load_file(path)
    doc.frontmatter.fields["tags"] = ["impro", "creativity", "resource/performing"]
    assert parse(serialize(doc)).body == original_body


# --------------------------------------------------------------------------
# Frontmatter.get_list — 03 §7 coercion (and the 08 §B10 first-character bug)
# --------------------------------------------------------------------------


def test_get_list_coerces_scalar_to_one_element_list(fixture_vault) -> None:
    doc = load_file(fixture_vault / QUIRK_FILES["scalar_tags"])
    assert doc.frontmatter.get_list("tags") == ["daily_notes"]
    assert doc.frontmatter.get_list("sources") == ["me"]


def test_get_list_scalar_alias_is_not_indexed_by_character() -> None:
    """08 §B10: ``aliases[0]`` on a string yielded its first character."""
    doc = parse("---\naliases: my-note-title\n---\nbody\n")
    aliases = doc.frontmatter.get_list("aliases")
    assert aliases == ["my-note-title"]
    assert aliases[0] == "my-note-title"


def test_get_list_missing_and_null_and_empty() -> None:
    doc = parse("---\ncontext: []\nempty:\n---\nbody\n")
    assert doc.frontmatter.get_list("context") == []
    assert doc.frontmatter.get_list("empty") == []
    assert doc.frontmatter.get_list("absent") == []


def test_get_list_never_mutates_or_aliases_fields(fixture_vault) -> None:
    doc = load_file(fixture_vault / QUIRK_FILES["current_schema"])
    before = doc.frontmatter.fields["tags"]
    got = doc.frontmatter.get_list("tags")
    got.append("mutated")
    assert doc.frontmatter.fields["tags"] == ["impro", "creativity"]
    assert doc.frontmatter.fields["tags"] is before
    assert doc.frontmatter.fields["tags"] == before


def test_get_list_does_not_sweep_neighbouring_fields_into_tags(fixture_vault) -> None:
    """08 §B9: the hand-rolled parser swept aliases/sources/modalities in."""
    doc = load_file(fixture_vault / QUIRK_FILES["current_schema"])
    assert doc.frontmatter.get_list("tags") == ["impro", "creativity"]
    assert doc.frontmatter.get_list("sources") == ["me"]
    assert doc.frontmatter.get_list("modalities") == ["text"]
    assert doc.frontmatter.get_list("aliases") == ["2026-06-10T21:33:05.379Z"]


# --------------------------------------------------------------------------
# normalize_tag (04 §1)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Impro", "impro"),
        ("  padded  ", "padded"),
        ("daily notes", "daily-notes"),
        ("daily_notes", "daily-notes"),
        ("Deep Work_Log", "deep-work-log"),
        ("already-kebab", "already-kebab"),
        ("project/blog", "project/blog"),
        ("", ""),
    ],
)
def test_normalize_tag_basic(raw: str, expected: str) -> None:
    assert normalize_tag(raw) == expected


def test_normalize_tag_applies_the_config_map() -> None:
    mapping = {"project": "projects", "area": "areas"}
    assert normalize_tag("Project", mapping) == "projects"
    assert normalize_tag("area", mapping) == "areas"
    assert normalize_tag("resource", mapping) == "resource"


def test_normalize_tag_map_output_is_itself_normalized() -> None:
    assert normalize_tag("wl", {"wl": "Work_Log"}) == "work-log"


def test_normalize_tag_is_idempotent() -> None:
    once = normalize_tag("Deep Work_Log")
    assert normalize_tag(once) == once


def test_normalize_tag_tolerates_non_string_values() -> None:
    assert normalize_tag(2026) == "2026"  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# merge_tags / merge_sources (05 §2.6, §4; 08 §A24)
# --------------------------------------------------------------------------


def test_merge_tags_preserves_existing_order_and_casing() -> None:
    """08 §A24: the original re-sorted and re-cased the user's whole list."""
    assert merge_tags(["Zebra", "Apple", "impro"], ["project/blog"]) == [
        "Zebra",
        "Apple",
        "impro",
        "project/blog",
    ]


def test_merge_tags_dedupes_case_insensitively_keeping_existing_casing() -> None:
    assert merge_tags(["Impro", "creativity"], ["impro", "IMPRO", "new"]) == [
        "Impro",
        "creativity",
        "new",
    ]


def test_merge_tags_is_idempotent() -> None:
    once = merge_tags(["Impro"], ["project/blog"])
    assert merge_tags(once, ["project/blog"]) == once


def test_merge_tags_handles_empty_inputs() -> None:
    assert merge_tags([], ["a"]) == ["a"]
    assert merge_tags(["a"], []) == ["a"]
    assert merge_tags([], []) == []


def test_merge_sources_is_exact_string_union_target_first() -> None:
    assert merge_sources(["me", "clipboard"], ["web page", "me"]) == [
        "me",
        "clipboard",
        "web page",
    ]


def test_merge_sources_is_case_sensitive_unlike_tags() -> None:
    assert merge_sources(["Me"], ["me"]) == ["Me", "me"]


# --------------------------------------------------------------------------
# is_no_ai — the vault law (02)
# --------------------------------------------------------------------------


def test_is_no_ai_true_for_the_vault_law_file(fixture_vault) -> None:
    assert is_no_ai(load_file(fixture_vault / QUIRK_FILES["no_ai"])) is True


def test_is_no_ai_false_for_ordinary_notes(fixture_vault) -> None:
    assert is_no_ai(load_file(fixture_vault / QUIRK_FILES["current_schema"])) is False
    assert is_no_ai(load_file(fixture_vault / QUIRK_FILES["no_frontmatter"])) is False


def test_is_no_ai_false_when_explicitly_disabled() -> None:
    assert is_no_ai(parse("---\nno-ai: false\n---\nbody\n")) is False


@pytest.mark.parametrize(
    "line",
    ["no-ai: true", "no_ai: true", "No-AI: true", "no-ai: 'true'", "no-ai: yes"],
)
def test_is_no_ai_tolerates_real_world_spellings(line: str) -> None:
    assert is_no_ai(parse(f"---\n{line}\n---\nbody\n")) is True
