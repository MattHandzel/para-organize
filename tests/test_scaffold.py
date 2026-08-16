"""Scaffold gate: every module imports; the console script and every
subcommand's ``--help`` exit 0; the fixture vault builds with all quirks.

This is the Phase-0 acceptance test — builders extend, never weaken it.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import NON_MARKDOWN_FILES, QUIRK_FILES, build_fixture_vault

MODULES = [
    "organize_core",
    "organize_core.errors",
    "organize_core.paths",
    "organize_core.config",
    "organize_core.frontmatter",
    "organize_core.index",
    "organize_core.suggest",
    "organize_core.learn",
    "organize_core.fileops",
    "organize_core.session",
    "organize_core.actions",
    "organize_core.routes",
    "organize_core.llm",
    "organize_core.cli",
    "organize_core.server",
    "organize_core.consumers",
    "organize_core.consumers.base",
    "organize_core.consumers.store",
    "organize_core.consumers.runner",
    "organize_core.consumers.taskwarrior",
    "organize_core.consumers.learn",
    "organize_core.consumers.question_answer",
    "organize_core.consumers.deep_research",
    "organize_core.consumers.tag_router",
    "organize_core.consumers.auto_tagger",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports(module: str) -> None:
    importlib.import_module(module)


def test_consumer_registry_populated() -> None:
    from organize_core.consumers import get_consumer_types

    types = get_consumer_types()
    for name in ("taskwarrior", "learn", "question_answer", "deep_research", "tag_router", "auto_tagger"):
        assert name in types, f"consumer type {name!r} not registered"


def _console_script() -> Path:
    exe = Path(sys.executable).parent / "organize"
    assert exe.exists(), f"console script not installed at {exe} — run `uv pip install -e '.[dev]'`"
    return exe


def _run_help(args: list[str]) -> None:
    proc = subprocess.run(
        [str(_console_script()), *args, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"organize {' '.join(args)} --help exited {proc.returncode}\n{proc.stderr}"
    assert proc.stdout.strip(), "help output was empty"


def test_cli_top_level_help() -> None:
    _run_help([])


def test_cli_subcommand_help() -> None:
    from organize_core.cli import SUBCOMMANDS

    for sub in SUBCOMMANDS:
        _run_help([sub])


def test_cli_version() -> None:
    proc = subprocess.run(
        [str(_console_script()), "--version"], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    assert "0.1.0" in proc.stdout


def test_fixture_vault_builds_with_all_quirks(tmp_path: Path) -> None:
    vault = build_fixture_vault(tmp_path / "vault")
    for name, rel in QUIRK_FILES.items():
        assert (vault / rel).exists(), f"quirk file missing: {name} ({rel})"
    for rel in NON_MARKDOWN_FILES:
        assert (vault / rel).exists()
    # archive is SINGULAR on disk (spec 02); no 'archives/' tree may appear
    assert (vault / "archive/capture/raw_capture").is_dir()
    assert not (vault / "archives").exists()
    # the invalid-utf8 exemplar really is invalid utf-8
    raw = (vault / QUIRK_FILES["invalid_utf8"]).read_bytes()
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")
