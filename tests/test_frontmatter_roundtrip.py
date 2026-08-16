"""The ROUND-TRIP LAW: property-style tests over the whole fixture corpus.

Spec 05 §9 acceptance test: "Frontmatter round-trip property test: parse →
serialize over a corpus of real vault files (including ``no-ai: true``, nested
``location``, ``metadata: {}`` and ``[]``) is lossless for every field, known
or unknown."  This module holds the corpus-wide properties; ``test_frontmatter``
holds the per-quirk unit assertions.

Three properties, checked over every markdown file in the fixture vault plus a
table of hand-built pathological documents:

P1  serialize(parse(text)) == text                     (byte-level, untouched)
P2  parse(serialize(parse(text))).fields == parse(text).fields   (value-level)
P3  every source field survives an edit to any OTHER field       (08 §A12)

P4 additionally re-runs P1/P2 with PyYAML disabled, so the stdlib fallback
parser (06 §1: "PyYAML optional") is held to the same law.
"""

from __future__ import annotations

import copy

import pytest

from conftest import QUIRK_FILES
from organize_core import frontmatter as fm_module
from organize_core.errors import FrontmatterError
from organize_core.frontmatter import parse, serialize

# Documents that are NOT expected to parse (they are the loud-failure cases).
UNPARSEABLE = {QUIRK_FILES["broken_yaml"]}

# Pathological documents beyond what the fixture vault contains.
EXTRA_DOCUMENTS: dict[str, str] = {
    "empty_file": "",
    "body_only": "no frontmatter here\n",
    "body_starting_with_rule": "---\n\nnot frontmatter, no terminator\n",
    "empty_block": "---\n---\nbody\n",
    "comment_only_block": "---\n# just a comment\n---\nbody\n",
    "no_trailing_newline": "---\nid: x\n---",
    "empty_body": "---\nid: x\n---\n",
    "crlf": "---\r\nid: x\r\ntags:\r\n- a\r\n---\r\nbody\r\n",
    "flow_everything": "---\ntags: [a, b]\nlocation: {city: NYC, lat: 1.5}\n---\nb\n",
    "multiline_flow": "---\ntags: [a,\n  b]\nid: x\n---\nb\n",
    "block_scalar": "---\ndesc: |\n  line one\n  ---\n  line two\nid: x\n---\nb\n",
    "folded_scalar": "---\ndesc: >\n  folded text\n  continues\nid: x\n---\nb\n",
    "seq_of_maps": "---\nitems:\n- name: a\n  v: 1\n- name: b\n  v: 2\n---\nb\n",
    "deep_nesting": "---\na:\n  b:\n    c:\n    - 1\n    - 2\n---\nb\n",
    "comments_everywhere": (
        "---\n# top\nid: x  # inline\n\n# about tags\ntags:\n- a\n\nauthor: Matt\n---\nb\n"
    ),
    "quoted_keys": "---\n'quoted key': 1\n\"double key\": 2\n---\nb\n",
    "obsidian_properties": (
        "---\ncssclasses:\n- wide\npublish: false\npermalink: /notes/x\n---\nb\n"
    ),
    "unicode_values": "---\ntitle: café ☕ — “quoted”\ntags:\n- ünïcode\n---\nb\n",
    "empty_containers": "---\nmetadata: {}\ncontext: []\ntags:\n- a\n---\nb\n",
    "null_values": "---\nid:\ntags: ~\ncontext: null\n---\nb\n",
    "numbers": "---\nlat: 40.7126\nlon: -74.0066\ncount: 3\nflag: true\n---\nb\n",
    "dashes_everywhere": (
        "---\na: 'x --- y'\nb: \"p --- q\"\n---\nbody\n\n---\n\nafter rule\n"
    ),
    "trailing_blank_lines_in_block": "---\nid: x\n\n\n---\nbody\n",
    "same_line_flow_map": "---\n{a: 1, b: 2}\n---\nbody\n",
    "duplicate_keys": "---\nid: first\nid: second\n---\nbody\n",
}


def _vault_documents(vault) -> dict[str, str]:
    docs: dict[str, str] = {}
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault).as_posix()
        docs[rel] = path.read_text(encoding="utf-8", errors="replace")
    return docs


def _corpus(vault) -> dict[str, str]:
    docs = {k: v for k, v in _vault_documents(vault).items() if k not in UNPARSEABLE}
    docs.update(EXTRA_DOCUMENTS)
    return docs


def test_corpus_is_large_enough_to_be_a_property_test(fixture_vault) -> None:
    """Guards against the corpus silently shrinking to nothing."""
    vault_docs = _vault_documents(fixture_vault)
    assert len(vault_docs) == 21
    assert set(UNPARSEABLE) <= set(vault_docs)
    assert len(_corpus(fixture_vault)) == 20 + len(EXTRA_DOCUMENTS)


def test_p1_serialize_parse_is_byte_identical_for_every_document(fixture_vault) -> None:
    mismatches = {
        name: serialize(parse(text))
        for name, text in _corpus(fixture_vault).items()
        if serialize(parse(text)) != text
    }
    assert mismatches == {}


def test_p2_values_survive_a_parse_serialize_parse_cycle(fixture_vault) -> None:
    for name, text in _corpus(fixture_vault).items():
        first = parse(text)
        second = parse(serialize(first))
        if first.frontmatter is None:
            assert second.frontmatter is None, name
            continue
        assert second.frontmatter is not None, name
        assert second.frontmatter.fields == first.frontmatter.fields, name
        assert second.body == first.body, name


def test_p3_every_field_survives_an_edit_to_any_other_field(fixture_vault) -> None:
    """08 §A12: the whitelist serializer destroyed everything it didn't know."""
    for name, text in _corpus(fixture_vault).items():
        doc = parse(text)
        if doc.frontmatter is None or not doc.frontmatter.fields:
            continue
        original = copy.deepcopy(doc.frontmatter.fields)
        for edited_key in list(original):
            doc = parse(text)
            doc.frontmatter.fields[edited_key] = "EDITED-SENTINEL"
            reparsed = parse(serialize(doc))
            assert reparsed.frontmatter is not None, name
            assert list(reparsed.frontmatter.fields) == list(original), (name, edited_key)
            for key, value in original.items():
                if key == edited_key:
                    assert reparsed.frontmatter.fields[key] == "EDITED-SENTINEL"
                else:
                    assert reparsed.frontmatter.fields[key] == value, (name, key)
            assert reparsed.body == doc.body, name


def test_p3_adding_a_field_never_disturbs_existing_ones(fixture_vault) -> None:
    for name, text in _corpus(fixture_vault).items():
        doc = parse(text)
        if doc.frontmatter is None or not doc.frontmatter.fields:
            continue
        original = copy.deepcopy(doc.frontmatter.fields)
        doc.frontmatter.fields["processing_status"] = "organized"
        doc.frontmatter.fields["x-brand-new-field"] = ["a", "b"]
        reparsed = parse(serialize(doc))
        for key, value in original.items():
            if key == "processing_status":
                continue
            assert reparsed.frontmatter.fields[key] == value, (name, key)
        assert reparsed.frontmatter.fields["processing_status"] == "organized"
        assert reparsed.frontmatter.fields["x-brand-new-field"] == ["a", "b"]
        assert reparsed.body == doc.body, name


def test_broken_yaml_is_the_only_corpus_member_that_refuses(fixture_vault) -> None:
    refused = []
    for rel, text in _vault_documents(fixture_vault).items():
        try:
            parse(text)
        except FrontmatterError:
            refused.append(rel)
    assert refused == sorted(UNPARSEABLE)


@pytest.fixture()
def without_pyyaml(monkeypatch: pytest.MonkeyPatch):
    """Force the stdlib fallback parser/emitter (06 §1: PyYAML optional)."""
    monkeypatch.setattr(fm_module, "_yaml", None)
    return fm_module


def test_p4_fallback_parser_obeys_the_same_round_trip_law(
    fixture_vault, without_pyyaml
) -> None:
    mismatches = {
        name: serialize(parse(text))
        for name, text in _corpus(fixture_vault).items()
        if serialize(parse(text)) != text
    }
    assert mismatches == {}


def test_p4_fallback_parser_agrees_with_pyyaml_on_every_value(
    fixture_vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = _corpus(fixture_vault)
    reference = {
        name: (
            parse(text).frontmatter.fields if parse(text).frontmatter is not None else None
        )
        for name, text in corpus.items()
    }
    monkeypatch.setattr(fm_module, "_yaml", None)
    for name, text in corpus.items():
        doc = parse(text)
        got = doc.frontmatter.fields if doc.frontmatter is not None else None
        assert got == reference[name], name


def test_p4_fallback_also_refuses_broken_yaml(fixture_vault, without_pyyaml) -> None:
    text = (fixture_vault / QUIRK_FILES["broken_yaml"]).read_text(encoding="utf-8")
    with pytest.raises(FrontmatterError):
        parse(text)


def test_p4_fallback_emitter_quotes_date_like_strings(without_pyyaml) -> None:
    """A date-like STRING must stay a string for readers that resolve dates."""
    doc = parse("---\nid: x\n---\nb\n")
    doc.frontmatter.fields["created_date"] = "2026-08-15"
    out = serialize(doc)
    assert "created_date: '2026-08-15'\n" in out
    assert parse(out).frontmatter.fields["created_date"] == "2026-08-15"
