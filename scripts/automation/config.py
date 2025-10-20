"""Configuration helpers for automation pipelines."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


@dataclass(slots=True)
class ConsumerConfig:
    """Configuration for a single automation consumer."""

    name: str
    type: str
    enabled: bool = True
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AutomationConfig:
    """Top-level automation configuration."""

    vault_root: Path
    capture_dir: Path
    state_dir: Path
    database_path: Path
    log_level: str
    consumers: tuple[ConsumerConfig, ...]

    def ensure_state_dirs(self) -> None:
        """Create state directories if they do not exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # Allow consumers to place backups/logs under the state dir.
        (self.state_dir / "backups").mkdir(exist_ok=True)


DEFAULTS: Dict[str, Any] = {
    "vault": {
        "root": str(Path("~/notes").expanduser()),
        "capture_dir": "capture/raw_capture",
    },
    "state": {
        "dir": str(Path("~/.local/state/para-organize").expanduser()),
        "database": "automations.db",
    },
    "logging": {"level": "INFO"},
    "consumers": {
        "taskwarrior": {
            "type": "taskwarrior",
            "enabled": True,
            "marker_tag": "todo",
            "strip_tags": ["todo"],
            "remove_unknown_tags": True,
            "project_tag_prefix": "project:",
            "default_project": None,
            "additional_tags": [],
            "review_tag": "not_reviewed",
            "max_new_tasks_per_run": None,
            "annotation_template": "Captured from {path}",
            "backup": {
                "enabled": True,
                "directory": "backups/taskwarrior",
            },
            "data_directory": str(Path("~/.task").expanduser()),
            "taskrc_path": str(Path("~/.taskrc").expanduser()),
        },
    },
}


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """Deep-merge overlay into base without mutating inputs."""
    merged: Dict[str, Any] = {**base}
    for key, value in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_file(path: Path) -> Dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _expand_path(raw: str, base: Optional[Path] = None) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve()


def _normalise_consumers(data: Mapping[str, Any]) -> tuple[ConsumerConfig, ...]:
    consumers: list[ConsumerConfig] = []
    for name, payload in sorted(data.items()):
        if not isinstance(payload, Mapping):
            raise ValueError(f"Consumer '{name}' must map to a table of options.")
        if "type" not in payload:
            raise ValueError(f"Consumer '{name}' is missing required 'type'.")
        consumers.append(
            ConsumerConfig(
                name=name,
                type=str(payload["type"]),
                enabled=bool(payload.get("enabled", True)),
                options={k: v for k, v in payload.items() if k not in {"type", "enabled"}},
            ),
        )
    return tuple(consumers)


def load_config(path: Optional[Path] = None) -> AutomationConfig:
    """
    Load automation config from disk, falling back to sensible defaults.

    Args:
        path: Explicit path to a TOML file. When omitted, the loader searches
            `~/.config/para-organize/automations.toml`. If no file exists,
            defaults are used.
    """
    if path is None:
        path = Path("~/.config/para-organize/automations.toml").expanduser()
    data = DEFAULTS
    if path.exists():
        override = _load_file(path)
        data = _deep_merge(data, override)

    vault_root = _expand_path(str(data["vault"]["root"]))
    capture_dir = _expand_path(str(data["vault"]["capture_dir"]), vault_root)

    state_dir = _expand_path(str(data["state"]["dir"]))
    database_path = _expand_path(
        str(data["state"].get("database", "automations.db")),
        state_dir,
    )

    log_level = str(data.get("logging", {}).get("level", "INFO")).upper()
    consumers = _normalise_consumers(data.get("consumers", {}))

    config = AutomationConfig(
        vault_root=vault_root,
        capture_dir=capture_dir,
        state_dir=state_dir,
        database_path=database_path,
        log_level=log_level,
        consumers=consumers,
    )
    config.ensure_state_dirs()
    return config
