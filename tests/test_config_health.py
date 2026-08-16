"""``check_vault`` — the health check that would have caught both live
misconfigurations (spec 03 §1, 08 §C1-2).

Regression obligations:

* **08 §C1** — ``vault_dir = "~/Obsidian/Main/notes"`` (one level too deep):
  the capture folder is absent under the configured root, and the hint names
  the directory that actually contains it.
* **08 §C2** — ``para_folders.archives = "archives"`` while the vault has
  ``archive/`` (SINGULAR): an error naming the key, with the correction.
* **08 §B18** — a scan dir that resolves outside the vault root (symlink) is
  reported instead of exploding later in ``relative_to()``.

Every assertion is on the exact issue list; a passing vault yields ``[]``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from organize_core.config import HealthIssue, check_vault, validate_config


def config_for(root: Path, **vault: object):
    table: dict[str, object] = {"root": str(root)}
    table.update(vault)
    return validate_config({"vault": table})


def errors(issues: list[HealthIssue]) -> list[HealthIssue]:
    return [i for i in issues if i.severity == "error"]


def test_healthy_fixture_vault_reports_nothing(fixture_vault: Path) -> None:
    assert check_vault(config_for(fixture_vault)) == []


def test_missing_vault_root_is_a_single_actionable_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    issues = check_vault(config_for(missing))
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert str(missing) in issues[0].message
    assert "vault.root" in issues[0].message
    assert "~/notes" in (issues[0].hint or "")


def test_vault_root_that_is_a_file_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "vault.md"
    target.write_text("not a vault\n", encoding="utf-8")
    issues = check_vault(config_for(target))
    assert len(issues) == 1
    assert "is not a directory" in issues[0].message


def test_c1_vault_root_one_level_too_deep(fixture_vault: Path) -> None:
    """08 §C1: `vault_dir = "~/Obsidian/Main/notes"` — the real defect."""
    wrong_root = fixture_vault / "notes"
    wrong_root.mkdir()

    issues = check_vault(config_for(wrong_root))
    capture_issues = [i for i in issues if i.message.startswith("vault.capture_folder")]
    assert len(capture_issues) == 1
    issue = capture_issues[0]
    assert issue.severity == "error"
    assert "'capture' does not exist under the vault root" in issue.message
    assert issue.hint is not None
    assert "vault.root may be one level too deep" in issue.hint
    assert str(fixture_vault) in issue.hint


def test_c2_archives_vs_archive_names_the_key_and_the_fix(fixture_vault: Path) -> None:
    """08 §C2: every deployment configured `archives`; the vault has `archive`."""
    config = config_for(
        fixture_vault,
        para_folders={
            "projects": "projects",
            "areas": "areas",
            "resources": "resources",
            "archives": "archives",  # the live misconfiguration
        },
    )
    issues = check_vault(config)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.message.startswith("vault.para_folders.archives:")
    assert "'archives' does not exist under the vault root" in issue.message
    assert issue.hint == "the vault has 'archive' — set vault.para_folders.archives = 'archive'"


def test_singular_plural_confusion_is_caught_in_both_directions(fixture_vault: Path) -> None:
    config = config_for(
        fixture_vault,
        para_folders={
            "projects": "project",  # vault has "projects"
            "areas": "areas",
            "resources": "resources",
            "archives": "archive",
        },
    )
    issues = check_vault(config)
    assert len(issues) == 1
    assert issues[0].hint == "the vault has 'projects' — set vault.para_folders.projects = 'projects'"


def test_missing_capture_folder_is_reported_with_the_configured_name(fixture_vault: Path) -> None:
    issues = check_vault(config_for(fixture_vault, capture_folder="inbox"))
    messages = [i.message for i in errors(issues)]
    assert any("vault.capture_folder: 'inbox' does not exist" in m for m in messages)


def test_missing_scan_dir_is_an_error(fixture_vault: Path) -> None:
    config = config_for(fixture_vault, scan_dirs=["capture/raw_capture", "notes/inbox"])
    issues = check_vault(config)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].message.startswith("vault.scan_dirs:")
    assert "'notes/inbox'" in issues[0].message


def test_folder_that_is_a_file_is_reported(fixture_vault: Path) -> None:
    (fixture_vault / "notafolder.md").write_text("x\n", encoding="utf-8")
    issues = check_vault(config_for(fixture_vault, scan_dirs=["notafolder.md"]))
    assert len(issues) == 1
    assert "exists but is not a directory" in issues[0].message


def test_scan_dir_resolving_outside_the_vault_is_flagged(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """08 §B18: symlinked scan dirs blow up `relative_to(vault_root)` later."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (fixture_vault / "linked").symlink_to(outside, target_is_directory=True)

    issues = check_vault(config_for(fixture_vault, scan_dirs=["linked"]))
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "resolves to" in issues[0].message
    assert str(outside) in issues[0].message


def test_symlinked_vault_root_is_resolved_before_comparison(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """spec 02: ``~/notes`` and ``~/Obsidian/Main`` are the same tree."""
    link = tmp_path / "notes-link"
    link.symlink_to(fixture_vault, target_is_directory=True)
    assert check_vault(config_for(link)) == []


def test_missing_archive_capture_path_warns_when_folders_are_auto_created(
    fixture_vault: Path,
) -> None:
    config = validate_config(
        {
            "vault": {"root": str(fixture_vault), "archive_capture_path": "capture/other"},
            "file_ops": {"auto_create_folders": True},
        }
    )
    issues = check_vault(config)
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert issues[0].message.startswith("vault.archive_capture_path:")
    assert "'archive/capture/other'" in issues[0].message


def test_missing_archive_capture_path_errors_without_auto_create(fixture_vault: Path) -> None:
    config = validate_config(
        {
            "vault": {"root": str(fixture_vault), "archive_capture_path": "capture/other"},
            "file_ops": {"auto_create_folders": False},
        }
    )
    issues = check_vault(config)
    assert len(issues) == 1
    assert issues[0].severity == "error"


@pytest.mark.skipif(os.getuid() == 0, reason="root ignores the write bit")
def test_unwritable_folder_is_an_error(fixture_vault: Path) -> None:
    target = fixture_vault / "resources"
    original = target.stat().st_mode
    target.chmod(0o500)
    try:
        issues = check_vault(config_for(fixture_vault))
        unwritable = [i for i in issues if "not writable" in i.message]
        # "resources" is both a PARA folder and a scan dir: both keys report it.
        assert [i.message.split(":")[0] for i in unwritable] == [
            "vault.para_folders.resources",
            "vault.scan_dirs",
        ]
        assert all(i.severity == "error" for i in unwritable)
        assert all(str(target) in i.message for i in unwritable)
    finally:
        target.chmod(original)


def test_issue_order_is_deterministic(fixture_vault: Path) -> None:
    empty = fixture_vault / "empty"
    empty.mkdir()
    first = check_vault(config_for(empty))
    second = check_vault(config_for(empty))
    assert [(i.severity, i.message) for i in first] == [(i.severity, i.message) for i in second]
    # capture folders first, then PARA folders alphabetically, then scan dirs.
    assert [i.message.split(":")[0] for i in first] == [
        "vault.capture_folder",
        "vault.raw_capture_folder",
        "vault.para_folders.archives",
        "vault.para_folders.areas",
        "vault.para_folders.projects",
        "vault.para_folders.resources",
        "vault.scan_dirs",
        "vault.scan_dirs",
        "vault.scan_dirs",
        "vault.scan_dirs",
    ]


def test_check_vault_does_not_raise_on_a_broken_vault(tmp_path: Path) -> None:
    """Health reporting never throws — callers decide fail-vs-warn."""
    issues = check_vault(config_for(tmp_path / "gone"))
    assert all(isinstance(i, HealthIssue) for i in issues)


# ---------------------------------------------------------------------------
# The cutover gate: an automations.db left behind at the OLD path (06 §1)
# ---------------------------------------------------------------------------


def _write_automations_db(path: Path, *, emission_rows: int) -> Path:
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE notes (path TEXT PRIMARY KEY, note_hash TEXT NOT NULL,
                            metadata_json TEXT, seen_at INTEGER NOT NULL);
        CREATE TABLE emissions (consumer TEXT NOT NULL, note_path TEXT NOT NULL,
                                note_hash TEXT NOT NULL, emitted_at INTEGER NOT NULL,
                                status TEXT NOT NULL DEFAULT 'success',
                                metadata_json TEXT,
                                PRIMARY KEY (consumer, note_path));
        """
    )
    conn.executemany(
        "INSERT INTO emissions(consumer, note_path, note_hash, emitted_at, status)"
        " VALUES ('learn', ?, 'h', 1700000000, 'success')",
        [(f"/vault/n{i}.md",) for i in range(emission_rows)],
    )
    conn.commit()
    conn.close()
    return path


def _paths_with_home(home: Path, state: Path):
    from organize_core.paths import CorePaths

    return CorePaths(
        config_dir=home / ".config" / "organize-core",
        state_dir=state,
        runtime_dir=home / "run",
    )


def test_health_errors_when_the_old_automations_db_was_never_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cutover runbook migrated ``~/.local/state/para-organize/automations.db``
    while the service opens ``<state-dir>/automations.db``. Followed verbatim it
    left 7 516 notes of history orphaned and started the pipeline on an EMPTY
    database — re-firing every past capture through its consumers, which is
    exactly what spec 06 §1 exists to prevent. Health must say so BEFORE the
    duplicates arrive."""
    from organize_core import cli

    home = tmp_path / "home"
    legacy = home / ".local" / "state" / "para-organize" / "automations.db"
    _write_automations_db(legacy, emission_rows=379)
    state = home / ".local" / "share" / "organize-core"
    state.mkdir(parents=True)
    monkeypatch.setattr(cli, "default_env", lambda: {"HOME": str(home)})

    issues = cli._stranded_automations_db_issues(_paths_with_home(home, state))

    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "379" in issues[0].message
    assert str(legacy) in issues[0].message
    assert "cp " in (issues[0].hint or "")


def test_health_is_silent_once_the_database_has_been_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from organize_core import cli

    home = tmp_path / "home"
    _write_automations_db(
        home / ".local" / "state" / "para-organize" / "automations.db", emission_rows=379
    )
    state = home / ".local" / "share" / "organize-core"
    _write_automations_db(state / "automations.db", emission_rows=379)
    monkeypatch.setattr(cli, "default_env", lambda: {"HOME": str(home)})

    assert cli._stranded_automations_db_issues(_paths_with_home(home, state)) == []


def test_health_is_silent_on_a_fresh_install_with_no_old_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No old database means nothing to strand; a first-time user must not
    be shown a cutover error."""
    from organize_core import cli

    home = tmp_path / "home"
    state = home / ".local" / "share" / "organize-core"
    state.mkdir(parents=True)
    monkeypatch.setattr(cli, "default_env", lambda: {"HOME": str(home)})

    assert cli._stranded_automations_db_issues(_paths_with_home(home, state)) == []


def test_health_is_silent_when_the_old_database_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old file with no emissions has no history to lose."""
    from organize_core import cli

    home = tmp_path / "home"
    _write_automations_db(
        home / ".local" / "state" / "para-organize" / "automations.db", emission_rows=0
    )
    state = home / ".local" / "share" / "organize-core"
    state.mkdir(parents=True)
    monkeypatch.setattr(cli, "default_env", lambda: {"HOME": str(home)})

    assert cli._stranded_automations_db_issues(_paths_with_home(home, state)) == []


# ---------------------------------------------------------------------------
# The external toolchain an enabled consumer shells out to (spec 06 §5/§6)
# ---------------------------------------------------------------------------


def _config_with_consumer(root: Path, name: str, ctype: str, **options: object):
    from organize_core.config import ConsumerConfig

    config = config_for(root)
    config.consumers.append(
        ConsumerConfig(name=name, type=ctype, enabled=True, options=dict(options))
    )
    return config


def test_health_warns_when_a_configured_external_binary_is_missing(
    fixture_vault: Path,
) -> None:
    """Spec 06 §5 asked for a ``shell.nix`` to pin the toolchain; the deploy
    seat deliberately shipped no wrapper, so the unit runs
    ``%h/.local/bin/organize`` with no ``Environment=`` and ``task`` /
    ``yt-dlp`` / the agent resolve against whatever PATH the systemd user
    manager inherited. Unchecked, that surfaces as a per-note ERROR at the far
    end of a ten-minute timer instead of a setup-time answer."""
    from organize_core import cli

    config = _config_with_consumer(
        fixture_vault, "tw", "taskwarrior", task_binary="definitely-not-a-real-binary"
    )
    issues = cli._toolchain_issues(config)

    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "definitely-not-a-real-binary" in issues[0].message
    assert "task_binary" in issues[0].message
    assert "PATH" in (issues[0].hint or "")


def test_health_accepts_an_absolute_path_that_exists(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """The documented fix — an absolute path in the config — must actually
    satisfy the check."""
    from organize_core import cli

    binary = tmp_path / "task"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    config = _config_with_consumer(fixture_vault, "tw", "taskwarrior", task_binary=str(binary))
    assert cli._toolchain_issues(config) == []


def test_health_checks_the_first_element_of_an_argv_option(fixture_vault: Path) -> None:
    """learn's ``yt_dlp_command`` and deep_research's ``command`` are argv
    lists; only element 0 is the binary."""
    from organize_core import cli

    config = _config_with_consumer(
        fixture_vault, "learn", "learn", yt_dlp_command=["no-such-yt-dlp", "--flag"]
    )
    issues = cli._toolchain_issues(config)
    assert len(issues) == 1
    assert "no-such-yt-dlp" in issues[0].message
    assert "--flag" not in issues[0].message


def test_health_ignores_a_disabled_consumers_toolchain(fixture_vault: Path) -> None:
    """A consumer that will not run cannot be missing anything."""
    from organize_core import cli
    from organize_core.config import ConsumerConfig

    config = config_for(fixture_vault)
    config.consumers.append(
        ConsumerConfig(
            name="tw",
            type="taskwarrior",
            enabled=False,
            options={"task_binary": "definitely-not-a-real-binary"},
        )
    )
    assert cli._toolchain_issues(config) == []


def test_health_checks_the_default_binary_when_the_option_is_absent(
    fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live config does not set ``task_binary``; the default ``task`` is
    exactly the one that has to resolve under systemd."""
    from organize_core import cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    config = _config_with_consumer(fixture_vault, "tw", "taskwarrior")
    issues = cli._toolchain_issues(config)
    assert len(issues) == 1
    assert "'task'" in issues[0].message


def test_health_does_not_cry_cutover_at_a_scratch_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a REAL deployment can be mid-cutover.

    A ``--state-dir`` pointed somewhere else is a developer scratch run, a
    test, or a rehearsal on a copy. Comparing those against the operator's
    actual ``~/.local/state/para-organize/automations.db`` made every such
    invocation report a broken cutover — a check that cries wolf on every run
    is a check people learn to ignore.
    """
    from organize_core import cli

    home = tmp_path / "home"
    _write_automations_db(
        home / ".local" / "state" / "para-organize" / "automations.db", emission_rows=379
    )
    scratch = tmp_path / "scratch-state"
    scratch.mkdir()
    monkeypatch.setattr(cli, "default_env", lambda: {"HOME": str(home)})

    assert cli._stranded_automations_db_issues(_paths_with_home(home, scratch)) == []
