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
    IntegrateConfig,
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
    # spec 12 §1 defaults-of-record: global edit mode `manual`, review gate
    # `diff`, and ZERO deletions tolerated from an integrate result.
    assert config.integrate == IntegrateConfig()
    assert config.integrate.default_mode == "manual"
    assert config.integrate.review == "diff"
    assert config.integrate.max_deleted_lines == 0
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
            # `integrate` so `review` can carry a NON-default value here — the
            # point of FULL_RAW is that every leaf differs from its default.
            "mode": "integrate",
            "description": "d",
            "template": "## {date}",
            "review": "auto",
        },
        # `auto` rides on a separate APPEND route rather than on the integrate
        # route above. It could now ride on it — Phase 5 narrowed the refusal
        # to `auto = true` + integrate + review != "auto", and that route sets
        # review = "auto" — but keeping them apart is what makes FULL_RAW
        # carry a non-default value for `auto` AND for a non-auto integrate
        # route at the same time. The narrowed rule has its own tests.
        {
            "tags": ["blog-idea"],
            "destination": "projects/blog/ideas.md",
            "mode": "append",
            "auto": True,
        },
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
    "integrate": {
        "default_mode": "integrate",
        "review": "auto",
        "max_deleted_lines": 3,
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
    # `auto` lives on the second (append) route — see the FULL_RAW comment.
    assert config.routes[0].auto is False
    assert config.routes[1].auto is True
    assert config.routes[0].template == "## {date}"
    assert config.routes[0].description == "d"
    assert config.routes[0].review == "auto"  # spec 12 §1 per-route opt-in

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
    assert config.integrate == IntegrateConfig(
        default_mode="integrate", review="auto", max_deleted_lines=3
    )
    assert config.auto_organize.trust == "auto_below"
    assert config.auto_organize.confidence_threshold == 0.42
    assert config.auto_organize.min_precedents == 11
    assert config.server.socket_path == Path("/run/x.sock")
    assert config.server.idle_timeout_seconds == 30.0
    assert config.logging.level == "DEBUG"
    assert config.descriptions == {"areas/health": "health stuff"}


#: Config leaves with no reader outside `config.py` TODAY, each with the
#: reason and its spec citation. This is the escape hatch that keeps
#: `test_every_config_leaf_has_a_reader` honest: "reserved for a later phase"
#: is a documented decision with a citation, not an accident. Anything not
#: listed here must be READ by some non-config module — which is the half of
#: 08 §A35 ("~25 dead config keys — every one must be honored or deleted")
#: that a parse-only assertion cannot check. The list may not rot either: a
#: key that grows a reader must come OFF it (asserted below).
RESERVED_CONFIG_LEAVES: dict[str, str] = {
    # Read by `config.check_vault` — i.e. it is honored, by `organize health`,
    # which is the only behavior it has. The folder that drives SCANNING is
    # `vault.scan_dirs`; the folder that drives PARA classification is
    # `vault.capture_folder` (of which this is a subfolder).
    "vault.raw_capture_folder": "spec 02 — health-check only (config.check_vault)",
    # (`vault.scan_dirs` left this list at Phase 3: consumers/runner.py walks
    # it and consumers/store.py's purge guard keys off it — spec 06 §2.)
    # spec 03 §7: "Incremental: debounced (`incremental_debounce` ms)
    # BufWritePost hook re-indexes the written file". Under doc 10 that
    # trigger lives in the nvim client (Phase 2), not in the core; the core's
    # own persistence honors spec 09 §40 through the BATCH half
    # (`VaultIndex.flush_threshold`), which is also its crash bound.
    "vault.incremental_debounce": "spec 03 §7 — client-side reindex trigger (Phase 2)",
    # ALL THREE `[integrate]` keys left this list at the Phase-5 landing, by
    # the allowlist's own anti-rot half — each now has a real reader:
    #   integrate.max_deleted_lines → integrate.check_guards (the deletion guard)
    #   integrate.review            → routes.effective_review (the 12 §1 gate)
    #   integrate.default_mode      → routes.effective_mode (12 §1's "global
    #                                 default", reported by `routes resolve`)
    # spec 13 automatic organize — Phase 6.
    "auto_organize.trust": "spec 13 §2 — automatic organize (Phase 6)",
    "auto_organize.confidence_threshold": "spec 13 §2 — automatic organize (Phase 6)",
    "auto_organize.min_precedents": "spec 13 §2 — automatic organize (Phase 6)",
}

def _config_leaves() -> list[str]:
    """Every scalar leaf of a real Config INSTANCE, dotted.

    Walks the instance rather than the annotations so `from __future__ import
    annotations` (which turns every field type into a string) cannot make the
    walk silently shallow.
    """
    import dataclasses

    leaves: list[str] = []

    def walk(obj: object, prefix: str) -> None:
        for f in dataclasses.fields(obj):  # type: ignore[arg-type]
            name = f"{prefix}.{f.name}" if prefix else f.name
            value = getattr(obj, f.name)
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                walk(value, name)
            else:
                leaves.append(name)

    walk(validate_config(FULL_RAW), "")
    return leaves


def test_every_config_leaf_has_a_reader() -> None:
    """08 §A35, the half `test_every_config_key_is_honored` cannot check.

    That test asserts every leaf PARSES onto the Config dataclass. A key that
    parses and is then read by nobody is the exact original defect and passes
    it unchanged. This one asserts each leaf is referenced by at least one
    module outside `config.py`, with a spec-cited allowlist for the keys
    deliberately reserved for a later phase.

    Reader = an ATTRIBUTE ACCESS or a bound NAME, never a bare string
    constant. That distinction is load-bearing (found in Phase 3): both
    Phase-3 consumers emit the spec-mandated flashcard frontmatter value
    `"status": "review"` (06 §3.2/§3.3), and counting string constants let
    that retire the allowlist entry for `[integrate] review` — a key that is
    still Phase 5 and still genuinely unread. Config is a dataclass tree, so
    every real reader goes through attribute access anyway; any consumer that
    ever wrote a word matching a config key would otherwise silently blank
    out this gate.
    """
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "organize_core"
    # AST, not text: a rule that trips over a comment EXPLAINING the rule is a
    # rule people delete. Only names the code actually touches count.
    referenced: set[str] = set()
    for path in sorted(src.rglob("*.py")):
        if path.name == "config.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.Name):
                referenced.add(node.id)

    unread = [leaf for leaf in _config_leaves() if leaf.rsplit(".", 1)[-1] not in referenced]

    unexpected = sorted(set(unread) - set(RESERVED_CONFIG_LEAVES))
    assert unexpected == [], (
        "config leaves that nothing outside config.py reads — honor them or delete "
        f"them with a REMOVED_KEYS entry (08 §A35): {unexpected}"
    )

    # And the allowlist may not rot: a reserved key that HAS grown a reader
    # must be taken off the list, or the list stops meaning anything.
    stale = sorted(set(RESERVED_CONFIG_LEAVES) - set(unread))
    assert stale == [], (
        f"these leaves now have readers and must leave RESERVED_CONFIG_LEAVES: {stale}"
    )


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
        # spec 12 §1 [integrate]. `diff` is a REVIEW gate, not an edit mode —
        # putting it in default_mode must fail loudly rather than land as a
        # mode nothing implements.
        (
            {"vault": {"root": "/v"}, "integrate": {"default_mode": "diff"}},
            "integrate.default_mode",
            "must be one of",
        ),
        (
            {"vault": {"root": "/v"}, "integrate": {"review": "manual"}},
            "integrate.review",
            "must be one of",
        ),
        (
            {"vault": {"root": "/v"}, "integrate": {"max_deleted_lines": True}},
            "integrate.max_deleted_lines",
            "must be an integer, got boolean",
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
        # 12 §1: the deletion threshold is a COUNT of tolerated deletions;
        # negative is meaningless and must not silently disable the guard.
        ({"integrate": {"max_deleted_lines": -1}}, "integrate.max_deleted_lines"),
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


def test_every_implemented_consumer_type_is_configurable() -> None:
    """The check reads the live ``@register`` registry — a new consumer type
    must not require a config-module edit (06 §1)."""
    from organize_core.consumers import get_implemented_consumer_types

    names = sorted(get_implemented_consumer_types())
    assert names, "the consumer registry has no implemented types"
    raw = minimal_raw()
    raw["consumers"] = {name: {"type": name} for name in names}
    config = validate_config(raw)
    assert [c.type for c in config.consumers] == names


SYNTHETIC_STUB_TYPE = "zz_synthetic_stub"


@pytest.fixture()
def synthetic_stub_type() -> Any:
    """Register an UNIMPLEMENTED consumer type for one test.

    These gates used to key on "whatever is currently pending" — the real
    Phase-4 stubs. That made them self-destructing: the moment auto_tagger
    and tag_router landed, the pending set emptied and the gate had nothing
    left to assert. A synthetic type keeps the refusal pinned PERMANENTLY,
    for every future consumer registered ahead of its bodies.
    """
    from organize_core.consumers import base as consumer_base

    class _SyntheticStub(consumer_base.Consumer):
        implemented = False

        def __init__(self, config: Any) -> None:  # noqa: ARG002 - never reached
            raise ConfigError(
                f"{SYNTHETIC_STUB_TYPE!r} is registered but not implemented yet"
            )

        def should_process(self, payload: Any) -> bool:  # pragma: no cover - unreachable
            raise AssertionError("an unimplemented consumer must never run")

        def handle(self, payload: Any, ctx: Any) -> Any:  # pragma: no cover - unreachable
            raise AssertionError("an unimplemented consumer must never run")

    consumer_base._REGISTRY[SYNTHETIC_STUB_TYPE] = _SyntheticStub
    try:
        yield _SyntheticStub
    finally:
        consumer_base._REGISTRY.pop(SYNTHETIC_STUB_TYPE, None)


def test_registered_but_unimplemented_types_are_refused_by_config(
    synthetic_stub_type: Any,
) -> None:
    """A type registered ahead of its bodies (``implemented = False``) keeps
    the registry stable but is NOT a usable config value.

    Accepting one turned a Phase-4 stub into an error per scanned note —
    7 500 on the real vault — plus exit 1 and an ``OnFailure=`` alert on
    every run, which is the opposite of the loud, isolated fail-fast
    08 §B2/§B14 ask for.
    """
    from organize_core.consumers import get_consumer_types, get_implemented_consumer_types

    pending = set(get_consumer_types()) - set(get_implemented_consumer_types())
    assert SYNTHETIC_STUB_TYPE in pending

    raw = minimal_raw()
    raw["consumers"] = {SYNTHETIC_STUB_TYPE: {"type": SYNTHETIC_STUB_TYPE}}
    with pytest.raises(ConfigError) as excinfo:
        validate_config(raw)
    message = str(excinfo.value)
    assert f"'consumers.{SYNTHETIC_STUB_TYPE}.type'" in message
    assert "not implemented" in message


def test_unimplemented_consumer_constructors_refuse_loudly(
    synthetic_stub_type: Any,
) -> None:
    """Belt and braces: even reached directly, the stub fails ONCE with a
    ConfigError the runner isolates — never NotImplementedError per note."""
    from organize_core.config import ConsumerConfig

    with pytest.raises(ConfigError) as excinfo:
        synthetic_stub_type(
            ConsumerConfig(name=SYNTHETIC_STUB_TYPE, type=SYNTHETIC_STUB_TYPE)
        )
    assert "not implemented" in str(excinfo.value)


def test_every_registered_consumer_type_is_now_implemented() -> None:
    """The Phase-4 boundary, re-pinned. auto_tagger and tag_router were the
    last two stubs; nothing ships unimplemented today. When a future phase
    registers a type ahead of its bodies, this fails and names it — the
    reminder to re-read the gate above rather than let a stub go unnoticed.
    """
    from organize_core.consumers import get_consumer_types, get_implemented_consumer_types

    pending = sorted(set(get_consumer_types()) - set(get_implemented_consumer_types()))
    assert pending == [], (
        f"registered but unimplemented: {pending} — confirm the refusal gate "
        "still covers them, then update this pin"
    )


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


def test_a_broken_consumer_registry_fails_loudly_instead_of_falling_back() -> None:
    """The deferred `organize_core.consumers` import (the `config <- consumers/base`
    back-edge, now recorded in ARCHITECTURE.md) used to be wrapped in a bare
    `except Exception` that silently substituted a HARDCODED type list. A
    broken `@register` in any consumer module would then make config
    validation quietly accept a stale set of consumer types — the opposite of
    this module's "bad config is banned" rule. Only `ImportError` falls back.
    """
    import organize_core.config as config_module

    original = config_module._registered_consumer_types

    def exploding() -> frozenset[str]:
        raise RuntimeError("a consumer module's @register is broken")

    raw = minimal_raw()
    raw["consumers"] = {"tw": {"type": "taskwarrior"}}
    config_module.__dict__["_registered_consumer_types"] = exploding
    try:
        with pytest.raises(RuntimeError):
            validate_config(raw)
    finally:
        config_module.__dict__["_registered_consumer_types"] = original

    # ...and with the real registry the same config is accepted.
    assert validate_config(raw).consumers[0].type == "taskwarrior"


def test_the_consumer_registry_is_not_imported_at_config_import_time() -> None:
    """The back-edge is broken by DEFERRING the import; if it ever became a
    module-level import the graph would have a real cycle."""
    import ast
    from pathlib import Path as _Path

    source = (
        _Path(__file__).resolve().parents[1] / "src" / "organize_core" / "config.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:  # module level only
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "organize_core.consumers"
        ):
            raise AssertionError("config.py imports consumers at module level — that is the cycle")
