"""Config loading + the UNKNOWN-KEY LAW (spec 03 §1, 06 §2, 10 §3).

Regression obligations covered here:

* **08 §A35** — "~25 dead config keys, every one honored or deleted": every key
  in the deleted list fails loudly naming the key, and every key that survived
  is provably honored (``test_every_config_key_is_honored`` sets all of them to
  a non-default value and asserts the value arrives on the ``Config``).
* **01 §success #4 / 09 §1.5** — a missing config file is a loud ``ConfigError``,
  never a silent fall-back to defaults.
* **08 §B14** — the no-scan-dirs condition is a real, reachable error.

Assertions are on exact values; ">0"-style assertions are what made the old
suite worthless (09 §3).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from organize_core.config import (
    Config,
    FileOpsConfig,
    LearningConfig,
    LLMConfig,
    SuggestionsConfig,
    SuggestionWeights,
    TypeBonus,
    VaultConfig,
    load_config,
    validate_config,
)
from organize_core.errors import ConfigError, OrganizeError
from organize_core.paths import CorePaths


def minimal_raw(**vault: Any) -> dict[str, Any]:
    """The smallest config that validates: a vault root and nothing else."""
    table = {"root": "/tmp/does-not-need-to-exist"}
    table.update(vault)
    return {"vault": table}


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# defaults-of-record
# ---------------------------------------------------------------------------


def test_minimal_config_produces_documented_defaults() -> None:
    config = validate_config(minimal_raw())

    assert isinstance(config, Config)
    assert config.vault.root == Path("/tmp/does-not-need-to-exist")
    assert config.vault.capture_folder == "capture"
    assert config.vault.raw_capture_folder == "capture/raw_capture"
    # spec 02 / 08 §C2: the archive folder is SINGULAR on disk.
    assert config.vault.para_folders == {
        "projects": "projects",
        "areas": "areas",
        "resources": "resources",
        "archives": "archive",
    }
    assert config.vault.archive_capture_path == "capture/raw_capture"
    assert config.vault.scan_dirs == [
        "capture/raw_capture",
        "resources",
        "areas",
        "projects",
    ]
    assert config.vault.ignore_patterns == [".obsidian", ".git", "resources/flashcards"]
    assert config.vault.max_file_size == 1_048_576
    assert config.vault.incremental_debounce == 500

    assert config.suggestions == SuggestionsConfig()
    assert config.suggestions.weights == SuggestionWeights()
    assert config.suggestions.weights.type_bonus == TypeBonus()
    assert config.suggestions.learning == LearningConfig()
    assert config.file_ops == FileOpsConfig()
    assert config.llm == LLMConfig()
    assert config.routes == []
    assert config.consumers == []
    assert config.descriptions == {}
    assert config.auto_organize.trust == "propose"
    assert config.auto_organize.confidence_threshold == 0.8
    assert config.auto_organize.min_precedents == 5
    assert config.server.socket_path is None
    assert config.server.idle_timeout_seconds == 600.0
    assert config.logging.level == "INFO"


def test_default_weights_are_the_spec_04_numbers() -> None:
    weights = validate_config(minimal_raw()).suggestions.weights
    assert weights.exact_tag_match == 2.0
    assert weights.normalized_tag_match == 1.5
    assert weights.learned_association == 1.8
    assert weights.source_match == 1.3
    assert weights.alias_similarity == 1.1
    assert weights.context_match == 1.0
    assert (weights.type_bonus.projects, weights.type_bonus.areas, weights.type_bonus.resources) == (
        0.3,
        0.2,
        0.1,
    )


def test_default_learning_knobs_are_the_spec_04_numbers() -> None:
    learning = validate_config(minimal_raw()).suggestions.learning
    assert learning.recency_decay == 0.9
    assert learning.frequency_boost == 1.2
    assert learning.max_history == 1000
    assert learning.eviction_days == 90
    assert learning.min_confidence == 0.3


# ---------------------------------------------------------------------------
# the unknown-key law
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_names_the_key() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config({**minimal_raw(), "sugestions": {}}, source="cfg.toml")
    assert "unknown config key 'sugestions'" in str(excinfo.value)
    assert "cfg.toml" in str(excinfo.value)
    assert excinfo.value.hint is not None
    assert "suggestions" in excinfo.value.hint  # close-match hint


def test_unknown_nested_key_names_the_dotted_path() -> None:
    raw = minimal_raw()
    raw["suggestions"] = {"weights": {"typo_match": 1.0}}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "unknown config key 'suggestions.weights.typo_match'" in str(excinfo.value)


def test_unknown_key_deep_in_type_bonus_is_named_in_full() -> None:
    raw = minimal_raw()
    raw["suggestions"] = {"weights": {"type_bonus": {"archives": 0.4}}}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "unknown config key 'suggestions.weights.type_bonus.archives'" in str(excinfo.value)
    assert "always_show_archive" in (excinfo.value.hint or "")


def test_unknown_para_folder_is_rejected() -> None:
    raw = minimal_raw(para_folders={"projects": "projects", "inbox": "inbox"})
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "unknown config key 'vault.para_folders.inbox'" in str(excinfo.value)


def test_unknown_key_hint_lists_allowed_keys_when_no_close_match() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config({**minimal_raw(), "zzz": {}})
    assert "keys allowed here:" in (excinfo.value.hint or "")
    assert "auto_organize" in (excinfo.value.hint or "")


# 08 §A35 — the deleted half of "every key honored or deleted" (03 §1 list).
@pytest.mark.parametrize(
    ("section", "payload"),
    [
        ("ui", {"display": {"show_scores": True}}),
        ("ui", {"highlights": {"selected": "Visual"}}),
        ("keymaps", {"global": {"start": "<leader>oo"}}),
        ("keymaps", {"buffer": {"refresh": "r"}}),
        ("telescope", {"layout_strategy": "horizontal", "picker_opts": {}}),
        ("patterns", {"alias_extraction": "x", "case_sensitive": False}),
        ("indexing", {"backend": "sqlite"}),
        ("debug", {"profile": True}),
    ],
)
def test_deleted_plugin_keys_fail_loudly_naming_the_key(section: str, payload: dict) -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config({**minimal_raw(), section: payload})
    assert f"unknown config key '{section}'" in str(excinfo.value)
    assert excinfo.value.hint, f"deleted key {section!r} must carry a migration hint (03 §1)"


def test_deleted_file_ops_confirm_operations_is_named_with_a_hint() -> None:
    raw = minimal_raw()
    raw["file_ops"] = {"confirm_operations": True}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "unknown config key 'file_ops.confirm_operations'" in str(excinfo.value)
    assert "dead key" in (excinfo.value.hint or "")


def test_state_section_points_at_the_new_state_layout() -> None:
    raw = minimal_raw()
    raw["state"] = {"dir": "~/.local/state/para-organize", "database": "automations.db"}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "unknown config key 'state'" in str(excinfo.value)
    assert "ORGANIZE_CORE_STATE_DIR" in (excinfo.value.hint or "")


def test_old_flat_vault_dir_key_is_renamed_not_ignored() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config({**minimal_raw(), "vault_dir": "~/Obsidian/Main"})
    assert "unknown config key 'vault_dir'" in str(excinfo.value)
    assert "[vault] root" in (excinfo.value.hint or "")


def test_llm_host_and_model_renames_carry_hints() -> None:
    for old, new in (("host", "ollama_host"), ("model", "ollama_model")):
        raw = minimal_raw()
        raw["llm"] = {old: "x", "ollama_host": "h", "ollama_model": "m"}
        with pytest.raises(ConfigError) as excinfo:
            validate_config(raw)
        assert f"unknown config key 'llm.{old}'" in str(excinfo.value)
        assert new in (excinfo.value.hint or "")


# 08 §A35 — the honored half. Keys the old plugin shipped but never read are
# read here, and every remaining key demonstrably reaches the Config.
def test_previously_dead_keys_are_now_honored() -> None:
    raw = minimal_raw()
    raw["file_ops"] = {"log_operations": False}
    raw["suggestions"] = {"learning": {"min_confidence": 0.55}}
    config = validate_config(raw)
    assert config.file_ops.log_operations is False
    assert config.suggestions.learning.min_confidence == 0.55


FULL_RAW: dict[str, Any] = {
    "vault": {
        "root": "/vault",
        "capture_folder": "inbox",
        "raw_capture_folder": "inbox/raw",
        "archive_capture_path": "old/raw",
        "scan_dirs": ["inbox/raw"],
        "ignore_patterns": [".git"],
        "max_file_size": 2048,
        "incremental_debounce": 250,
        "para_folders": {
            "projects": "p",
            "areas": "a",
            "resources": "r",
            "archives": "archive",
        },
    },
    "suggestions": {
        "max_suggestions": 7,
        "always_show_archive": False,
        "tag_normalization": {"project": "projects"},
        "weights": {
            "exact_tag_match": 9.0,
            "normalized_tag_match": 8.0,
            "learned_association": 7.0,
            "source_match": 6.0,
            "alias_similarity": 5.0,
            "context_match": 4.0,
            "type_bonus": {"projects": 0.9, "areas": 0.8, "resources": 0.7},
        },
        "learning": {
            "recency_decay": 0.99,
            "frequency_boost": 2.5,
            "max_history": 42,
            "eviction_days": 7,
            "min_confidence": 0.11,
        },
    },
    "file_ops": {
        "create_backups": False,
        "backup_dir": "backups",
        "log_operations": False,
        "auto_create_folders": False,
    },
    "metadata_fields": [{"key": "energy", "type": "number", "keymap": "E"}],
    "routes": [
        {
            "tags": ["workout"],
            "destination": "areas/health/log.md",
            "mode": "append",
            "description": "d",
            "template": "## {date}",
            "auto": True,
        }
    ],
    "consumers": {
        "tw": {
            "type": "taskwarrior",
            "enabled": False,
            "include_paths": ["inbox/raw"],
            "exclude_paths": ["inbox/raw/media"],
            "max_notes_per_run": 3,
            "marker_tag": "todo",
            "env": {"TASKRC": "/etc/taskrc"},
        }
    },
    "llm": {
        "backend": "claude-cli",
        "ollama_host": "http://h:1",
        "ollama_model": "m",
        "claude_command": ["claude", "-p", "--json"],
        "integrate_backend": "ollama",
        "timeout_seconds": 12.5,
        "retries": 0,
    },
    "auto_organize": {
        "trust": "auto_below",
        "confidence_threshold": 0.42,
        "min_precedents": 11,
    },
    "server": {"socket_path": "/run/x.sock", "idle_timeout_seconds": 30.0},
    "logging": {"level": "debug"},
    "descriptions": {"areas/health": "health stuff"},
}


def test_every_config_key_is_honored() -> None:
    """08 §A35: no key may be silently ignored. Every leaf below is set to a
    non-default value and must arrive on the Config."""
    config = validate_config(FULL_RAW)

    assert config.vault == VaultConfig(
        root=Path("/vault"),
        capture_folder="inbox",
        raw_capture_folder="inbox/raw",
        para_folders={"projects": "p", "areas": "a", "resources": "r", "archives": "archive"},
        archive_capture_path="old/raw",
        scan_dirs=["inbox/raw"],
        ignore_patterns=[".git"],
        max_file_size=2048,
        incremental_debounce=250,
    )
    assert config.suggestions.max_suggestions == 7
    assert config.suggestions.always_show_archive is False
    assert config.suggestions.tag_normalization == {"project": "projects"}
    assert config.suggestions.weights == SuggestionWeights(
        exact_tag_match=9.0,
        normalized_tag_match=8.0,
        learned_association=7.0,
        source_match=6.0,
        alias_similarity=5.0,
        context_match=4.0,
        type_bonus=TypeBonus(projects=0.9, areas=0.8, resources=0.7),
    )
    assert config.suggestions.learning == LearningConfig(
        recency_decay=0.99,
        frequency_boost=2.5,
        max_history=42,
        eviction_days=7,
        min_confidence=0.11,
    )
    assert config.file_ops == FileOpsConfig(
        create_backups=False,
        backup_dir="backups",
        log_operations=False,
        auto_create_folders=False,
    )
    assert [f.key for f in config.metadata_fields] == ["energy"]
    assert config.routes[0].auto is True
    assert config.routes[0].template == "## {date}"
    assert config.routes[0].description == "d"

    consumer = config.consumers[0]
    assert consumer.name == "tw"
    assert consumer.type == "taskwarrior"
    assert consumer.enabled is False
    assert consumer.include_paths == ["inbox/raw"]
    assert consumer.exclude_paths == ["inbox/raw/media"]
    assert consumer.max_notes_per_run == 3
    assert consumer.env == {"TASKRC": "/etc/taskrc"}
    assert consumer.options == {"marker_tag": "todo"}

    assert config.llm == LLMConfig(
        backend="claude-cli",
        ollama_host="http://h:1",
        ollama_model="m",
        claude_command=["claude", "-p", "--json"],
        integrate_backend="ollama",
        timeout_seconds=12.5,
        retries=0,
    )
    assert config.auto_organize.trust == "auto_below"
    assert config.auto_organize.confidence_threshold == 0.42
    assert config.auto_organize.min_precedents == 11
    assert config.server.socket_path == Path("/run/x.sock")
    assert config.server.idle_timeout_seconds == 30.0
    assert config.logging.level == "DEBUG"
    assert config.descriptions == {"areas/health": "health stuff"}


# ---------------------------------------------------------------------------
# leaf types, enums, ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "key", "expected"),
    [
        ({"vault": {"root": 5}}, "vault.root", "must be a string, got integer"),
        ({"vault": {"root": ""}}, "vault.root", "must not be empty"),
        ({"vault": "x"}, "vault", "must be a table, got string"),
        (
            {"vault": {"root": "/v", "scan_dirs": "resources"}},
            "vault.scan_dirs",
            "must be an array of strings, got string",
        ),
        (
            {"vault": {"root": "/v", "scan_dirs": [1]}},
            "vault.scan_dirs[0]",
            "must be a string, got integer",
        ),
        (
            {"vault": {"root": "/v", "max_file_size": "big"}},
            "vault.max_file_size",
            "must be an integer, got string",
        ),
        (
            {"vault": {"root": "/v", "para_folders": {"projects": 3}}},
            "vault.para_folders.projects",
            "must be a string, got integer",
        ),
        (
            {"vault": {"root": "/v"}, "suggestions": {"always_show_archive": "yes"}},
            "suggestions.always_show_archive",
            "must be true or false, got string",
        ),
        (
            {"vault": {"root": "/v"}, "suggestions": {"weights": {"context_match": "1"}}},
            "suggestions.weights.context_match",
            "must be a number, got string",
        ),
        (
            {"vault": {"root": "/v"}, "logging": {"level": "LOUD"}},
            "logging.level",
            "must be one of",
        ),
        (
            {"vault": {"root": "/v"}, "auto_organize": {"trust": "always"}},
            "auto_organize.trust",
            "must be one of",
        ),
        (
            {"vault": {"root": "/v"}, "llm": {"backend": "openai"}},
            "llm.backend",
            "must be one of",
        ),
        (
            {"vault": {"root": "/v"}, "descriptions": {"areas/health": 3}},
            'descriptions."areas/health"',
            "must be a string, got integer",
        ),
    ],
)
def test_leaf_type_and_enum_violations_name_the_key(
    raw: dict[str, Any], key: str, expected: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    message = str(excinfo.value)
    assert f"config key '{key}'" in message
    assert expected in message


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ({"suggestions": {"max_suggestions": 0}}, "suggestions.max_suggestions"),
        ({"suggestions": {"learning": {"recency_decay": 0.0}}}, "suggestions.learning.recency_decay"),
        ({"suggestions": {"learning": {"recency_decay": 1.5}}}, "suggestions.learning.recency_decay"),
        ({"suggestions": {"learning": {"min_confidence": 1.5}}}, "suggestions.learning.min_confidence"),
        ({"suggestions": {"learning": {"max_history": 0}}}, "suggestions.learning.max_history"),
        ({"suggestions": {"weights": {"context_match": -1.0}}}, "suggestions.weights.context_match"),
        ({"vault": {"root": "/v", "max_file_size": 0}}, "vault.max_file_size"),
        ({"vault": {"root": "/v", "incremental_debounce": -1}}, "vault.incremental_debounce"),
        ({"llm": {"retries": -1}}, "llm.retries"),
        ({"llm": {"timeout_seconds": 0}}, "llm.timeout_seconds"),
        ({"server": {"idle_timeout_seconds": 0.0}}, "server.idle_timeout_seconds"),
        ({"auto_organize": {"confidence_threshold": 1.2}}, "auto_organize.confidence_threshold"),
        ({"auto_organize": {"min_precedents": -1}}, "auto_organize.min_precedents"),
    ],
)
def test_out_of_range_values_name_the_key(raw: dict[str, Any], key: str) -> None:
    merged = {**minimal_raw(), **raw}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(merged)
    assert f"config key '{key}'" in str(excinfo.value)


def test_booleans_are_not_accepted_where_numbers_are_expected() -> None:
    """TOML `true` is a Python bool, and bool subclasses int — an unguarded
    isinstance check would accept `max_suggestions = true`."""
    raw = minimal_raw()
    raw["suggestions"] = {"max_suggestions": True}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "must be an integer, got boolean" in str(excinfo.value)


def test_missing_vault_section_is_an_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config({})
    assert "config key 'vault' is required" in str(excinfo.value)


def test_missing_vault_root_is_an_error() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config({"vault": {}})
    assert "config key 'vault.root' is required" in str(excinfo.value)


def test_empty_scan_dirs_is_a_loud_error() -> None:
    """08 §B14: the no-scan-dirs condition must be a real, reachable error."""
    with pytest.raises(ConfigError) as excinfo:
        validate_config(minimal_raw(scan_dirs=[]))
    assert "config key 'vault.scan_dirs'" in str(excinfo.value)
    assert "empty array" in str(excinfo.value)


def test_absolute_backup_dir_is_rejected() -> None:
    raw = minimal_raw()
    raw["file_ops"] = {"backup_dir": "/var/backups"}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "config key 'file_ops.backup_dir'" in str(excinfo.value)
    assert "relative to the vault root" in str(excinfo.value)


def test_top_level_must_be_a_table() -> None:
    with pytest.raises(ConfigError) as excinfo:
        validate_config([])  # type: ignore[arg-type]
    assert "config must be a table" in str(excinfo.value)


# ---------------------------------------------------------------------------
# [consumers.<name>] (spec 06 §2)
# ---------------------------------------------------------------------------


def test_consumer_type_must_be_registered() -> None:
    raw = minimal_raw()
    raw["consumers"] = {"tw": {"type": "taskwarior"}}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "config key 'consumers.tw.type'" in str(excinfo.value)
    assert "'taskwarior' is not a registered consumer type" in str(excinfo.value)
    assert "taskwarrior" in (excinfo.value.hint or "")


def test_every_registered_consumer_type_is_configurable() -> None:
    """The check reads the live ``@register`` registry — a new consumer type
    must not require a config-module edit (06 §1)."""
    from organize_core.consumers import get_consumer_types

    names = sorted(get_consumer_types())
    assert names, "the consumer registry is empty"
    raw = minimal_raw()
    raw["consumers"] = {name: {"type": name} for name in names}
    config = validate_config(raw)
    assert [c.type for c in config.consumers] == names


def test_consumer_type_is_required() -> None:
    raw = minimal_raw()
    raw["consumers"] = {"tw": {"enabled": True}}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "config key 'consumers.tw.type' is required" in str(excinfo.value)


def test_consumer_defaults_match_spec_06() -> None:
    raw = minimal_raw()
    raw["consumers"] = {"qa": {"type": "question_answer"}}
    consumer = validate_config(raw).consumers[0]
    assert consumer.enabled is True
    assert consumer.max_notes_per_run == 50
    assert consumer.include_paths == []
    assert consumer.exclude_paths == []
    assert consumer.options == {}
    assert consumer.env == {}


def test_consumer_specific_options_are_collected_not_rejected() -> None:
    """The framework keys are validated here; consumer-specific options are the
    consumer class's schema (ARCHITECTURE "public interfaces": ConsumerConfig)."""
    raw = minimal_raw()
    raw["consumers"] = {
        "tw": {"type": "taskwarrior", "marker_tag": "todo", "additional_tags": ["para"]}
    }
    consumer = validate_config(raw).consumers[0]
    assert consumer.options == {"marker_tag": "todo", "additional_tags": ["para"]}


def test_consumer_env_values_must_be_strings() -> None:
    raw = minimal_raw()
    raw["consumers"] = {"dr": {"type": "deep_research", "env": {"NOTES_DIR": 3}}}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "config key 'consumers.dr.env.NOTES_DIR'" in str(excinfo.value)


def test_consumer_max_notes_per_run_must_be_positive() -> None:
    raw = minimal_raw()
    raw["consumers"] = {"tw": {"type": "taskwarrior", "max_notes_per_run": 0}}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "config key 'consumers.tw.max_notes_per_run'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# [llm] (spec 06 §2: no hardcoded host/model fallbacks)
# ---------------------------------------------------------------------------


def test_ollama_backend_requires_host_and_model_when_configured() -> None:
    raw = minimal_raw()
    raw["llm"] = {"backend": "ollama"}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "config key 'llm.ollama_host'" in str(excinfo.value)
    assert "gemma3:12b-it-qat" in (excinfo.value.hint or "")


def test_ollama_model_required_when_only_host_given() -> None:
    raw = minimal_raw()
    raw["llm"] = {"backend": "ollama", "ollama_host": "http://h:11434"}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "config key 'llm.ollama_model'" in str(excinfo.value)


def test_integrate_backend_ollama_also_requires_host_and_model() -> None:
    raw = minimal_raw()
    raw["llm"] = {"backend": "claude-cli", "integrate_backend": "ollama"}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "config key 'llm.ollama_host'" in str(excinfo.value)


def test_claude_cli_only_config_needs_no_ollama_settings() -> None:
    raw = minimal_raw()
    raw["llm"] = {"backend": "claude-cli", "integrate_backend": "claude-cli"}
    llm = validate_config(raw).llm
    assert llm.ollama_host is None
    assert llm.claude_command == ["claude", "-p"]


def test_empty_claude_command_is_rejected() -> None:
    raw = minimal_raw()
    raw["llm"] = {"backend": "claude-cli", "integrate_backend": "claude-cli", "claude_command": []}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    assert "config key 'llm.claude_command'" in str(excinfo.value)


# ---------------------------------------------------------------------------
# load_config (file handling, spec 10 §3)
# ---------------------------------------------------------------------------


def test_missing_config_file_fails_loudly(tmp_path: Path, core_paths: CorePaths) -> None:
    """spec 01 success #4: never silently run on defaults."""
    missing = tmp_path / "nope" / "config.toml"
    with pytest.raises(ConfigError) as excinfo:
        load_config(core_paths, config_file=missing)
    assert str(missing) in str(excinfo.value)
    assert "example" in (excinfo.value.hint or "")
    assert isinstance(excinfo.value, OrganizeError)


def test_config_path_pointing_at_a_directory_is_explained(
    tmp_path: Path, core_paths: CorePaths
) -> None:
    directory = tmp_path / "confdir"
    directory.mkdir()
    with pytest.raises(ConfigError) as excinfo:
        load_config(core_paths, config_file=directory)
    assert "is a directory" in str(excinfo.value)


def test_invalid_toml_names_the_file(tmp_path: Path, core_paths: CorePaths) -> None:
    path = write_config(tmp_path, "[vault\nroot = 'x'\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(core_paths, config_file=path)
    assert str(path) in str(excinfo.value)
    assert "invalid TOML" in str(excinfo.value)


def test_load_config_reads_and_validates(tmp_path: Path, core_paths: CorePaths) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    path = write_config(
        tmp_path,
        f'[vault]\nroot = "{vault}"\n\n[suggestions]\nmax_suggestions = 4\n',
    )
    config = load_config(core_paths, config_file=path)
    assert config.vault.root == vault.resolve()
    assert config.suggestions.max_suggestions == 4


def test_load_config_error_message_names_the_offending_file(
    tmp_path: Path, core_paths: CorePaths
) -> None:
    path = write_config(tmp_path, '[vault]\nroot = "/v"\n\n[nope]\nx = 1\n')
    with pytest.raises(ConfigError) as excinfo:
        load_config(core_paths, config_file=path)
    assert str(path) in str(excinfo.value)
    assert "unknown config key 'nope'" in str(excinfo.value)


def test_load_config_reads_utf8_with_replacement(tmp_path: Path, core_paths: CorePaths) -> None:
    """Every file read is utf-8/errors=replace (ARCHITECTURE ground rules)."""
    path = tmp_path / "config.toml"
    path.write_bytes(b'[vault]\nroot = "/vault"\n# caf\xe9 (latin-1 byte)\n')
    config = load_config(core_paths, config_file=path)
    assert config.vault.root == Path("/vault")


def test_load_config_delegates_tilde_expansion_to_paths(
    tmp_path: Path, core_paths: CorePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No module outside paths.py may consult the environment (10 §3), so
    ``~``/``$VAR`` expansion is delegated — never re-implemented here."""
    import organize_core.config as config_mod

    seen: list[str] = []

    def fake_expand(value: str) -> Path:
        seen.append(str(value))
        return tmp_path / "expanded-vault"

    monkeypatch.setattr(config_mod, "expand", fake_expand)
    path = write_config(
        tmp_path,
        '[vault]\nroot = "~/Obsidian/Main"\n\n[server]\nsocket_path = "$XDG_RUNTIME_DIR/x.sock"\n',
    )
    config = load_config(core_paths, config_file=path)
    assert seen == ["~/Obsidian/Main", "$XDG_RUNTIME_DIR/x.sock"]
    assert config.vault.root == tmp_path / "expanded-vault"


def test_load_config_defaults_to_paths_config_file(
    tmp_path: Path, core_paths: CorePaths
) -> None:
    try:
        target = core_paths.config_file
    except NotImplementedError:  # pragma: no cover - until the paths seat lands
        pytest.skip("CorePaths.config_file not implemented yet (integrator seat)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('[vault]\nroot = "/vault"\n', encoding="utf-8")
    assert load_config(core_paths).vault.root == Path("/vault")


def test_config_is_immutable() -> None:
    config = validate_config(minimal_raw())
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.vault.root = Path("/elsewhere")  # type: ignore[misc]
