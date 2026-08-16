"""``consumers/base.py`` — the shared framework's own contract.

Phase-3 architect ruling: ``NotePayload.no_ai`` and ``NotePayload.tags()``
must DELEGATE to the frontmatter module rather than re-implement its rules,
because a second parser is exactly the 08 §B9 defect (``question_answer``
hand-rolled a frontmatter split and swept ``aliases``/``sources``/
``modalities`` in as "tags", producing 874 noise files).

These tests therefore assert two different things and both matter:

1. the VALUES are right for every shape the vault actually contains, and
2. the values are the SAME ones the frontmatter module produces — pinned by
   asserting against ``frontmatter.is_no_ai`` / ``Frontmatter.get_list``
   directly, so the two can never drift apart silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES, build_fixture_vault  # noqa: E402  (tests/ is on sys.path)
from organize_core import frontmatter
from organize_core.config import Config, ConsumerConfig, VaultConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    Status,
)
from organize_core.paths import CorePaths


def payload(fields: dict[str, Any]) -> NotePayload:
    return NotePayload(
        path=Path("/vault/capture/raw_capture/x.md"),
        frontmatter=fields,
        content="body",
        raw_text="---\n---\nbody",
        note_hash="0" * 64,
    )


# --- no_ai -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, False),
        ({"no-ai": True}, True),
        ({"no-ai": False}, False),
        ({"no-ai": "true"}, True),  # ambiguous truthy ⇒ True (safe direction)
        ({"no-ai": "TRUE"}, True),
        ({"no-ai": "yes"}, True),
        ({"no-ai": "on"}, True),
        ({"no-ai": "1"}, True),
        ({"no-ai": "false"}, False),
        ({"no-ai": "no"}, False),
        ({"no-ai": None}, False),
        ({"no_ai": True}, True),  # underscore spelling
        ({"No-AI": True}, True),  # case
        ({"no-ai": []}, False),  # a list is not a flag
        ({"no-ai": {}}, False),
        ({"tags": ["no-ai"]}, False),  # a TAG named no-ai is not the flag
    ],
)
def test_no_ai_covers_every_shape_the_vault_contains(
    fields: dict[str, Any], expected: bool
) -> None:
    assert payload(fields).no_ai is expected


@pytest.mark.parametrize(
    "value", [True, False, "true", "yes", "1", "no", "off", None, [], {}, 0, 1]
)
def test_no_ai_agrees_with_the_frontmatter_module_exactly(value: Any) -> None:
    """ONE rule in the codebase. If these ever disagree, a vault-law bypass
    exists on one of the two paths and nobody would notice."""
    doc = frontmatter.Document(
        frontmatter=frontmatter.Frontmatter(fields={"no-ai": value}), body=""
    )
    assert payload({"no-ai": value}).no_ai is frontmatter.is_no_ai(doc)


def test_no_ai_is_read_from_the_real_fixture_note(tmp_path: Path) -> None:
    vault = build_fixture_vault(tmp_path / "vault")
    path = vault / QUIRK_FILES["no_ai"]
    doc = frontmatter.parse(path.read_text(encoding="utf-8"))
    fields = dict(doc.frontmatter.fields) if doc.frontmatter else {}
    assert NotePayload(
        path=path, frontmatter=fields, content=doc.body, raw_text="", note_hash=""
    ).no_ai is True


# --- tags() ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, []),
        ({"tags": None}, []),
        ({"tags": []}, []),
        ({"tags": "daily_notes"}, ["daily_notes"]),  # scalar ⇒ one element
        ({"tags": ["a", "b"]}, ["a", "b"]),
        ({"tags": ("a", "b")}, ["a", "b"]),
        ({"tags": 7}, ["7"]),  # stringified
        ({"tags": ["a", 2]}, ["a", "2"]),
    ],
)
def test_tags_coercion_matches_the_shared_rule(
    fields: dict[str, Any], expected: list[str]
) -> None:
    assert payload(fields).tags() == expected
    assert payload(fields).tags() == [
        str(v) for v in frontmatter.Frontmatter(fields=fields).get_list("tags")
    ]


def test_tags_never_sweeps_in_neighbouring_list_fields() -> None:
    """08 §B9 in one assertion: the old hand-rolled parser turned every
    list-valued frontmatter key into a "tag"."""
    fields = {
        "tags": ["todo"],
        "aliases": ["2026-06-10T21:33:05.379Z"],
        "sources": ["me"],
        "modalities": ["text"],
        "context": [],
    }
    assert payload(fields).tags() == ["todo"]


def test_tags_returns_raw_spellings_not_vault_normalized_ones() -> None:
    """Deliberate: ``frontmatter.normalize_tag`` maps ``_`` → ``-`` because
    it matches tags to FOLDERS. The taskwarrior consumer applies a different,
    domain-specific normalization (06 §3.1); normalizing here would silently
    rewrite every underscored tag in Matt's task history."""
    assert payload({"tags": ["not_reviewed", "Project:Blog"]}).tags() == [
        "not_reviewed",
        "Project:Blog",
    ]


def test_tags_reads_the_scalar_tags_quirk_file(tmp_path: Path) -> None:
    vault = build_fixture_vault(tmp_path / "vault")
    path = vault / QUIRK_FILES["scalar_tags"]
    doc = frontmatter.parse(path.read_text(encoding="utf-8"))
    fields = dict(doc.frontmatter.fields) if doc.frontmatter else {}
    assert NotePayload(
        path=path, frontmatter=fields, content=doc.body, raw_text="", note_hash=""
    ).tags() == ["daily_notes"]


def test_the_payload_never_mutates_the_frontmatter_it_was_given() -> None:
    fields = {"tags": "one"}
    note = payload(fields)
    assert note.tags() == ["one"]
    assert fields == {"tags": "one"}


# --- RunContext ------------------------------------------------------------


def test_run_context_carries_paths_for_state_derived_locations(tmp_path: Path) -> None:
    """06 §3.1 wants taskwarrior's backup snapshot under
    ``<state>/backups/taskwarrior/<UTC-ts>``. A consumer must not read the
    environment to find that (structural decision 4), so the runner hands the
    resolved CorePaths down."""
    paths = CorePaths(
        config_dir=tmp_path / "c", state_dir=tmp_path / "s", runtime_dir=tmp_path / "r"
    )
    ctx = RunContext(config=Config(vault=VaultConfig(root=tmp_path)), paths=paths)
    assert ctx.paths is paths
    assert ctx.paths.backups_dir == tmp_path / "s" / "backups"


def test_run_context_paths_default_to_none_so_unit_tests_need_no_state() -> None:
    ctx = RunContext(config=Config(vault=VaultConfig(root=Path("/v"))))
    assert ctx.paths is None
    assert ctx.llm is None
    assert ctx.dry_run is False


# --- the Consumer ABC ------------------------------------------------------


def test_a_consumer_subclass_must_implement_both_abstract_methods() -> None:
    class Incomplete(Consumer):
        def should_process(self, payload: NotePayload) -> bool:  # noqa: D102
            return True

    with pytest.raises(TypeError):
        Incomplete(ConsumerConfig(name="x", type="x"))  # type: ignore[abstract]


def test_uses_llm_defaults_to_false_so_a_consumer_opts_in_to_the_no_ai_guard() -> None:
    class Plain(Consumer):
        def should_process(self, payload: NotePayload) -> bool:  # noqa: D102
            return True

        def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:  # noqa: D102
            return ConsumerResult(status=Status.SUCCESS)

    assert Plain.uses_llm is False
    assert Plain(ConsumerConfig(name="x", type="x")).config.name == "x"
