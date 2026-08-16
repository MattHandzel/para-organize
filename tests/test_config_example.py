"""The shipped example config (spec 06 §2: ONE example file, the live file is
the deploy target).

Two obligations:

1. The example must itself load cleanly — a shipped example that fails the
   unknown-key law would be the fastest way to burn Matt's trust.
2. Every key must be present, so the example doubles as the schema reference
   (08 §A35: keys are honored or deleted — there is no third, undocumented
   state). ``test_example_covers_every_dataclass_field`` walks the dataclasses
   and fails when a new field lands without an example entry.
"""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path
from typing import Any

import pytest

from organize_core.config import (
    AutoOrganizeConfig,
    Config,
    FileOpsConfig,
    IntegrateConfig,
    LearningConfig,
    LLMConfig,
    MetadataFieldConfig,
    RouteConfig,
    ServerConfig,
    SuggestionsConfig,
    SuggestionWeights,
    TypeBonus,
    VaultConfig,
    check_vault,
    example_config_toml,
    load_config,
    validate_config,
)
from organize_core.paths import CorePaths


def parsed() -> dict[str, Any]:
    return tomllib.loads(example_config_toml())


def test_example_is_valid_toml() -> None:
    assert isinstance(parsed(), dict)


def test_example_loads_cleanly() -> None:
    config = validate_config(parsed(), source="example_config_toml()")
    assert isinstance(config, Config)
    assert config.vault.root == Path("~/Obsidian/Main")
    # 08 §C2: the shipped example must not repeat the archives/archive defect.
    assert config.vault.para_folders["archives"] == "archive"


def test_example_ships_the_spec_defaults_where_it_is_not_demonstrating() -> None:
    config = validate_config(parsed())
    assert config.suggestions.weights == SuggestionWeights()
    assert config.suggestions.learning == LearningConfig()
    assert config.file_ops == FileOpsConfig()
    assert config.auto_organize == AutoOrganizeConfig()
    assert config.suggestions.max_suggestions == SuggestionsConfig().max_suggestions
    assert config.logging.level == "INFO"


def test_example_demonstrates_every_pluralised_section() -> None:
    config = validate_config(parsed())
    assert [f.key for f in config.metadata_fields] == ["tags", "importance", "remember"]
    assert [r.destination for r in config.routes] == [
        "areas/health/training-log.md",
        "projects/blog/ideas.md",
        "resources/performing/",
        "projects/kms/design-notes.md",
    ]
    # All three route modes are demonstrated, including `integrate` — the
    # quality-sensitive doc 12 §1 path (previously absent from the example).
    assert [r.mode for r in config.routes] == ["append", "append", "move", "integrate"]
    assert [r.review for r in config.routes] == ["diff", "diff", "diff", "diff"]
    assert [c.name for c in config.consumers] == [
        "taskwarrior",
        "learn",
        "question_answer",
        "deep_research",
        "auto_tagger",
    ]
    assert config.consumers[3].env == {"NOTES_DIR": "~/Obsidian/Main"}
    assert sorted(config.descriptions) == ["areas/health", "projects/blog"]


def test_example_llm_names_host_and_model_explicitly() -> None:
    """spec 06 §2: no hardcoded fallbacks; the model is the REAL tag, not the
    typo'd `gemma4:e4b` from the old code (08 §B17)."""
    llm = validate_config(parsed()).llm
    assert llm.ollama_model == "gemma3:12b-it-qat"
    assert llm.ollama_host == "http://server.matthandzel.com:11434"
    assert llm.integrate_backend == "claude-cli"  # spec 12 §1


SECTION_FIELDS: list[tuple[str, type, tuple[str, ...]]] = [
    ("vault", VaultConfig, ()),
    ("suggestions", SuggestionsConfig, ("weights", "learning")),
    ("suggestions.weights", SuggestionWeights, ("type_bonus",)),
    ("suggestions.weights.type_bonus", TypeBonus, ()),
    ("suggestions.learning", LearningConfig, ()),
    ("file_ops", FileOpsConfig, ()),
    ("llm", LLMConfig, ()),
    ("integrate", IntegrateConfig, ()),
    ("auto_organize", AutoOrganizeConfig, ()),
    ("server", ServerConfig, ()),
]


@pytest.mark.parametrize(("path", "cls", "nested"), SECTION_FIELDS)
def test_example_covers_every_dataclass_field(
    path: str, cls: type, nested: tuple[str, ...]
) -> None:
    table: Any = parsed()
    for part in path.split("."):
        table = table[part]
    expected = {f.name for f in dataclasses.fields(cls)}
    missing = expected - set(table)
    assert not missing, f"[{path}] in the example config is missing: {sorted(missing)}"
    # `nested` keys are sub-tables validated by their own parametrised case.
    assert set(nested) <= set(table)


def test_example_covers_every_route_and_metadata_field_key() -> None:
    raw = parsed()
    route_keys: set[str] = set()
    for route in raw["routes"]:
        route_keys |= set(route)
    assert route_keys == {f.name for f in dataclasses.fields(RouteConfig)}

    field_keys: set[str] = set()
    for entry in raw["metadata_fields"]:
        field_keys |= set(entry)
    assert field_keys == {f.name for f in dataclasses.fields(MetadataFieldConfig)}


def test_example_covers_every_top_level_section() -> None:
    expected = {f.name for f in dataclasses.fields(Config)}
    assert expected <= set(parsed())


def test_example_documents_each_section_with_comments() -> None:
    text = example_config_toml()
    for section in ("[vault]", "[suggestions]", "[file_ops]", "[llm]", "[server]", "[logging]"):
        assert section in text
    assert text.count("#") >= 30, "the example is the schema reference — keep it commented"
    assert "spec 03 §1" in text  # the unknown-key law is stated up front


def test_example_round_trips_through_load_config(
    tmp_path: Path, core_paths: CorePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the shipped text, written to disk, loads through the real
    entry point. ``~``/``$VAR`` expansion is delegated to paths.py, so it is
    stubbed here rather than re-implemented (10 §3)."""
    import organize_core.config as config_mod

    vault = tmp_path / "vault"
    vault.mkdir()

    def fake_expand(value: str) -> Path:
        text = str(value)
        if text.startswith("~/Obsidian/Main"):
            return vault
        return tmp_path / "runtime" / "organize-core.sock"

    monkeypatch.setattr(config_mod, "expand", fake_expand)
    path = tmp_path / "config.toml"
    path.write_text(example_config_toml(), encoding="utf-8")

    config = load_config(core_paths, config_file=path)
    assert config.vault.root == vault
    assert config.server.socket_path == tmp_path / "runtime" / "organize-core.sock"
    # check_vault runs against the example without raising (it reports, loudly).
    issues = check_vault(config)
    assert {i.message.split(":")[0] for i in issues} == {
        "vault.capture_folder",
        "vault.raw_capture_folder",
        "vault.para_folders.archives",
        "vault.para_folders.areas",
        "vault.para_folders.projects",
        "vault.para_folders.resources",
        "vault.scan_dirs",
    }
