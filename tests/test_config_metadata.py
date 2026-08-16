"""``metadata_fields`` validation (spec 07).

Covers 07's acceptance tests 3 and 4 at the config layer:

* AT3 — adding a novel field (``energy``/number/``E``) is pure configuration,
  zero code changes.
* AT4 — a field whose keymap collides with a core organize binding is a
  validation error naming BOTH bindings.
"""

from __future__ import annotations

from typing import Any

import pytest

from organize_core.config import (
    CORE_KEYMAPS,
    MetadataFieldConfig,
    default_metadata_fields,
    validate_config,
)
from organize_core.errors import ConfigError


def raw_with(*fields: dict[str, Any]) -> dict[str, Any]:
    return {"vault": {"root": "/vault"}, "metadata_fields": list(fields)}


def test_default_fields_ship_tags_and_importance() -> None:
    """07 "Behavior": the feature works out of the box."""
    fields = default_metadata_fields()
    assert [f.key for f in fields] == ["tags", "importance"]

    tags, importance = fields
    assert tags == MetadataFieldConfig(
        key="tags",
        type="list",
        keymap="t",
        prompt="Add tag(s)",
        append=True,
        complete="existing",
        values=[],
        normalize="kebab",
    )
    assert importance == MetadataFieldConfig(
        key="importance",
        type="enum",
        keymap="i",
        prompt=None,
        append=True,
        complete=None,
        values=["high", "medium", "low"],
        normalize=None,
    )


def test_config_without_the_section_gets_the_defaults() -> None:
    config = validate_config({"vault": {"root": "/vault"}})
    assert config.metadata_fields == default_metadata_fields()


def test_explicit_empty_array_means_no_fields() -> None:
    config = validate_config({"vault": {"root": "/vault"}, "metadata_fields": []})
    assert config.metadata_fields == []


def test_default_field_keymaps_do_not_collide_with_core_keymaps() -> None:
    for f in default_metadata_fields():
        assert f.keymap not in CORE_KEYMAPS


def test_novel_field_needs_no_code_change() -> None:
    """07 acceptance test 3."""
    config = validate_config(raw_with({"key": "energy", "type": "number", "keymap": "E"}))
    assert config.metadata_fields == [
        MetadataFieldConfig(key="energy", type="number", keymap="E", prompt=None, append=True)
    ]


@pytest.mark.parametrize("field_type", ["list", "string", "boolean", "enum", "number"])
def test_every_documented_field_type_is_accepted(field_type: str) -> None:
    entry: dict[str, Any] = {"key": "k", "type": field_type, "keymap": "K"}
    if field_type == "enum":
        entry["values"] = ["a", "b"]
    config = validate_config(raw_with(entry))
    assert config.metadata_fields[0].type == field_type


def test_unknown_field_type_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw_with({"key": "k", "type": "date", "keymap": "K"}))
    assert "config key 'metadata_fields[0].type'" in str(excinfo.value)
    assert "must be one of" in str(excinfo.value)


def test_keymap_collision_with_core_binding_names_both() -> None:
    """07 acceptance test 4: `keymap = "a"` collides with archive."""
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw_with({"key": "author", "type": "string", "keymap": "a"}))
    message = str(excinfo.value)
    assert "config key 'metadata_fields[0].keymap'" in message
    assert "'a'" in message
    assert "archive" in message  # the OTHER binding, named
    assert "author" in (excinfo.value.hint or "")


@pytest.mark.parametrize("keymap", ["<CR>", "s", "S", "m", "r", "p", "?", "A", "<leader>mc"])
def test_every_core_keymap_is_reserved(keymap: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw_with({"key": "k", "type": "string", "keymap": keymap}))
    assert CORE_KEYMAPS[keymap] in str(excinfo.value)


def test_keymap_collision_between_two_fields_names_the_other_field() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config(
            raw_with(
                {"key": "energy", "type": "number", "keymap": "E"},
                {"key": "effort", "type": "number", "keymap": "E"},
            )
        )
    assert "config key 'metadata_fields[1].keymap'" in str(excinfo.value)
    assert "energy" in str(excinfo.value)


def test_duplicate_frontmatter_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config(
            raw_with(
                {"key": "energy", "type": "number", "keymap": "E"},
                {"key": "energy", "type": "string", "keymap": "G"},
            )
        )
    assert "config key 'metadata_fields[1].key'" in str(excinfo.value)
    assert "metadata_fields[0].key" in str(excinfo.value)


def test_enum_requires_values() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw_with({"key": "importance", "type": "enum", "keymap": "I"}))
    assert "config key 'metadata_fields[0].values'" in str(excinfo.value)
    assert "required" in str(excinfo.value)


def test_values_on_a_non_enum_field_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config(
            raw_with({"key": "k", "type": "string", "keymap": "K", "values": ["a"]})
        )
    assert "config key 'metadata_fields[0].values'" in str(excinfo.value)
    assert "only valid for type" in str(excinfo.value)


def test_complete_accepts_existing_or_an_explicit_list() -> None:
    config = validate_config(
        raw_with(
            {"key": "k", "type": "list", "keymap": "K", "complete": "existing"},
            {"key": "j", "type": "list", "keymap": "J", "complete": ["a", "b"]},
        )
    )
    assert config.metadata_fields[0].complete == "existing"
    assert config.metadata_fields[1].complete == ["a", "b"]


def test_complete_rejects_other_strings() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw_with({"key": "k", "type": "list", "keymap": "K", "complete": "all"}))
    assert "config key 'metadata_fields[0].complete'" in str(excinfo.value)


def test_normalize_only_accepts_kebab() -> None:
    assert (
        validate_config(
            raw_with({"key": "k", "type": "list", "keymap": "K", "normalize": "kebab"})
        ).metadata_fields[0].normalize
        == "kebab"
    )
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw_with({"key": "k", "type": "list", "keymap": "K", "normalize": "snake"}))
    assert "config key 'metadata_fields[0].normalize'" in str(excinfo.value)


def test_unknown_key_inside_a_field_names_the_indexed_path() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw_with({"key": "k", "type": "string", "keymap": "K", "promt": "x"}))
    assert "unknown config key 'metadata_fields[0].promt'" in str(excinfo.value)
    assert "prompt" in (excinfo.value.hint or "")


def test_required_field_keys_are_enforced() -> None:
    for missing in ("key", "type", "keymap"):
        entry = {"key": "k", "type": "string", "keymap": "K"}
        del entry[missing]
        with pytest.raises(ConfigError) as excinfo:
            validate_config(raw_with(entry))
        assert f"config key 'metadata_fields[0].{missing}' is required" in str(excinfo.value)


def test_metadata_fields_must_be_an_array_of_tables() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config({"vault": {"root": "/vault"}, "metadata_fields": {"tags": {}}})
    assert "config key 'metadata_fields'" in str(excinfo.value)
    assert "array of tables" in str(excinfo.value)


def test_append_defaults_true_and_is_honored() -> None:
    config = validate_config(
        raw_with(
            {"key": "k", "type": "list", "keymap": "K"},
            {"key": "j", "type": "list", "keymap": "J", "append": False},
        )
    )
    assert config.metadata_fields[0].append is True
    assert config.metadata_fields[1].append is False
