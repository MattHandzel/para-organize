"""The shipped systemd units in ``deploy/`` (spec 06 §5, 09 §5.5).

These are gates on the deployment shape, not on Python behavior, and they
exist because the last outage was a *deployment* bug: the pipeline crashed
on every run for three months while systemd reported success, because a
wrapper script caught the failure and exited 0 (spec 08 §B). Every rule
below is one of the ways that swallow can come back:

- a ``-`` prefix on ``ExecStart=`` (systemd's own "ignore the exit code"),
- a wrapper script between systemd and the pipeline,
- ``|| true`` on the pipeline path,
- no ``OnFailure=``, so a real failure is only visible to someone who goes
  looking,
- no ``TimeoutStartSec=``, so a hung run stalls silently instead of failing.

The suite also pins the units and the README against the CLI's REAL surface,
because "docs describe nonexistent APIs" is its own catalogued defect
(08 §B17) — the dead repo unit that ExecStart'd a file which never existed
(08 §C3) is the same mistake in unit-file form.

Nothing here installs, enables, starts or reloads anything.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from organize_core.cli import SUBCOMMANDS, build_parser

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"

SERVICE = DEPLOY / "organize-pipeline.service"
TIMER = DEPLOY / "organize-pipeline.timer"
PATH_UNIT = DEPLOY / "organize-pipeline.path"
ALERTER = DEPLOY / "organize-pipeline-failure@.service"
FAILTEST = DEPLOY / "organize-pipeline-failtest.service"
ALERT_SCRIPT = DEPLOY / "organize-pipeline-alert.sh"
README = DEPLOY / "README.md"

UNIT_FILES = [SERVICE, TIMER, PATH_UNIT, ALERTER, FAILTEST]


def parse_unit(path: Path) -> configparser.RawConfigParser:
    """Unit files are INI with duplicate keys (two ``PathChanged=``) and
    ``%`` specifiers, so: Raw (no interpolation), non-strict, case-preserving."""
    parser = configparser.RawConfigParser(strict=False, allow_no_value=True)
    parser.optionxform = str  # type: ignore[method-assign]
    parser.read_string(path.read_text(encoding="utf-8"), source=str(path))
    return parser


def unit_values(path: Path, section: str, key: str) -> list[str]:
    """Every value for a repeatable directive (configparser keeps only the
    last, so repeated keys are read from the text)."""
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*(.*)$", re.MULTILINE)
    return [m.group(1).strip() for m in pattern.finditer(path.read_text(encoding="utf-8"))]


def exec_start_lines(path: Path) -> list[str]:
    return unit_values(path, "Service", "ExecStart")


# ---------------------------------------------------------------------------
# the files exist and parse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
def test_unit_files_parse_and_declare_their_sections(unit: Path) -> None:
    parsed = parse_unit(unit)
    assert "Unit" in parsed.sections()
    assert parsed.get("Unit", "Description", fallback="").strip()


def test_the_deploy_directory_ships_everything_the_readme_promises() -> None:
    for path in [*UNIT_FILES, ALERT_SCRIPT, README]:
        assert path.is_file(), f"deploy/{path.name} is missing"


def test_the_alert_script_is_executable() -> None:
    assert os.access(ALERT_SCRIPT, os.X_OK), (
        "organize-pipeline-alert.sh must be executable; the alerter unit "
        "ExecStart's it directly"
    )


# ---------------------------------------------------------------------------
# THE swallow gates (08 §B "the wrapper swallows failure")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", UNIT_FILES, ids=lambda p: p.name)
def test_no_exec_line_ignores_its_exit_code(unit: Path) -> None:
    """``ExecStart=-/usr/bin/thing`` tells systemd to treat a nonzero exit as
    success. That is the outage, expressed in one character."""
    for key in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop"):
        for value in unit_values(unit, "Service", key):
            assert not value.startswith("-"), (
                f"{unit.name}: {key}= starts with '-', which makes systemd "
                "ignore a nonzero exit (spec 08 §B)"
            )


def test_the_pipeline_is_invoked_directly_with_no_wrapper_and_no_swallow() -> None:
    lines = exec_start_lines(SERVICE)
    assert len(lines) == 1, "one run per activation; extra ExecStart lines hide failures"
    command = lines[0]

    assert "run-consumers" in command
    assert "--config" in command, (
        "the config path is stated explicitly so a background run's config is "
        "never in doubt (spec 08 §C4: the two automations.toml files had drifted)"
    )
    for swallow in ("||", "; true", "|| true", "set +e"):
        assert swallow not in command, f"{swallow!r} on the pipeline path (spec 08 §B)"
    assert not re.search(r"\.sh\b", command), (
        "no wrapper script between systemd and the pipeline — the vault-side "
        "shim is what swallowed three months of failures (spec 08 §B)"
    )
    # A shell would reintroduce the wrapper by another name.
    assert not re.match(r"\S*/(ba)?sh\s", command)


def test_the_pipeline_service_alerts_on_failure() -> None:
    on_failure = parse_unit(SERVICE).get("Unit", "OnFailure", fallback="")
    assert on_failure.startswith("organize-pipeline-failure@")
    assert "%n" in on_failure, (
        "the alerter is templated on the FAILED unit's name so the alert says "
        "which unit it is about"
    )


def test_a_hung_run_becomes_a_reported_failure() -> None:
    """Without a start timeout a wedged LLM/agent call is a silent stall —
    indistinguishable, from the outside, from a healthy idle pipeline."""
    timeout = parse_unit(SERVICE).get("Service", "TimeoutStartSec", fallback="")
    assert timeout, "organize-pipeline.service must set TimeoutStartSec="
    assert int(re.sub(r"\D", "", timeout)) > 0


def test_the_service_is_a_oneshot_that_nobody_enables_directly() -> None:
    parsed = parse_unit(SERVICE)
    assert parsed.get("Service", "Type", fallback="") == "oneshot"
    assert "Install" not in parsed.sections(), (
        "the service is started by the timer/path units; an [Install] section "
        "would additionally fire a run at every login"
    )


# ---------------------------------------------------------------------------
# cadence and triggers (06 §5)
# ---------------------------------------------------------------------------


def test_the_timer_is_the_ten_minute_cadence_the_spec_asks_for() -> None:
    parsed = parse_unit(TIMER)
    assert parsed.get("Timer", "OnUnitActiveSec") == "10m"
    assert parsed.get("Timer", "OnBootSec") == "5m"
    assert parsed.get("Timer", "Persistent") == "true"
    assert parsed.get("Timer", "Unit") == SERVICE.name
    assert parsed.get("Install", "WantedBy") == "timers.target"


def test_the_path_unit_watches_the_capture_and_resource_trees() -> None:
    watched = unit_values(PATH_UNIT, "Path", "PathChanged")
    assert any(w.endswith("capture/raw_capture") for w in watched), watched
    assert any(w.endswith("resources") for w in watched), watched
    parsed = parse_unit(PATH_UNIT)
    assert parsed.get("Path", "Unit") == SERVICE.name
    assert parsed.get("Path", "MakeDirectory") == "true"
    assert parsed.get("Install", "WantedBy") == "paths.target"


def test_the_units_use_home_relative_specifiers_not_one_machine_s_paths() -> None:
    """A user unit that hardcodes one person's home directory only works for
    that person — systemd spells it ``%h`` (the generalized form of 08 §A37,
    which is also why the needles below are assembled from fragments rather
    than written out: ``test_repo_hygiene`` bans that literal in test files,
    including this one)."""
    needles = ["/" + "home" + "/", "/" + "Users" + "/", str(Path.home()) + "/"]
    for unit in [*UNIT_FILES, ALERT_SCRIPT]:
        text = unit.read_text(encoding="utf-8")
        for needle in needles:
            assert needle not in text, f"{unit.name} hardcodes a home directory ({needle})"

    # …and the units that DO name a path use the specifier.
    for unit in (SERVICE, PATH_UNIT, ALERTER):
        assert "%h" in unit.read_text(encoding="utf-8"), f"{unit.name} should use %h"


# ---------------------------------------------------------------------------
# the alerter (06 §5: "failures alert visibly")
# ---------------------------------------------------------------------------


def test_the_alerter_receives_the_failed_units_name() -> None:
    command = exec_start_lines(ALERTER)
    assert len(command) == 1
    assert command[0].endswith("%i"), (
        "the template must pass %i through, or every alert is anonymous"
    )
    assert "organize-pipeline-alert" in command[0]
    assert "Install" not in parse_unit(ALERTER).sections(), (
        "an OnFailure= alerter is pulled in by the failing unit; enabling it "
        "would run it at login"
    )


def test_the_alerter_has_both_documented_channels() -> None:
    """Spec 06 §5 wants failures visible. ``notify-send`` is the channel that
    gets attention; the journal marker is the one that still works when the
    timer fires with no session bus. Both, or the alert is conditional on
    being logged in."""
    script = ALERT_SCRIPT.read_text(encoding="utf-8")
    assert "notify-send" in script
    assert "journalctl" in script
    assert re.search(r"^\s*unit=", script, re.MULTILINE), "the alert must name its subject"


def test_the_failtest_unit_fails_on_purpose_through_the_same_alerter() -> None:
    """The cutover checklist's "verify OnFailure fires" step (09 §5.5) needs
    something safe to fail. This unit must genuinely exit nonzero and must
    route to the same alerter the pipeline uses."""
    parsed = parse_unit(FAILTEST)
    assert parsed.get("Unit", "OnFailure", fallback="").startswith(
        "organize-pipeline-failure@"
    )
    command = exec_start_lines(FAILTEST)[0]
    assert re.search(r"exit\s+([1-9]\d*)", command), (
        "the failtest must exit nonzero; a passing failtest proves nothing"
    )


def test_the_alert_script_is_syntactically_valid() -> None:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - bash is present on every dev box
        pytest.skip("bash unavailable")
    proc = subprocess.run(
        [bash, "-n", str(ALERT_SCRIPT)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# 08 §B17 — the docs and units name only things that exist
# ---------------------------------------------------------------------------


def cli_option_strings() -> set[str]:
    parser = build_parser()
    options: set[str] = set()
    stack = [parser]
    while stack:
        current = stack.pop()
        for action in current._actions:
            options.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
    return options


def shell_lines(text: str) -> list[str]:
    """Every executable-looking line in a document: fenced code blocks plus
    inline ``code`` spans, with shell line-continuations joined first. Prose
    is deliberately excluded — "organize — PARA automation pipeline" is a
    description, not a command."""
    joined = re.sub(r"\\\n\s*", " ", text)
    lines: list[str] = []
    in_fence = False
    for raw in joined.splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            lines.append(raw)
    lines.extend(re.findall(r"`([^`\n]+)`", joined))
    return lines


def tokenize(line: str) -> list[str]:
    import shlex

    head = re.split(r"[;|>]", line.strip().lstrip("$").strip())[0]
    try:
        return shlex.split(head)
    except ValueError:
        return []  # an unbalanced quote — prose, not a command


def organize_args(line: str) -> list[str] | None:
    """``["--config", "x", "run-consumers"]`` if this line invokes the
    ``organize`` binary (by name, by path, or as a unit's ``ExecStart=``),
    else None."""
    tokens = tokenize(line)
    if not tokens:
        return None
    first = tokens[0]
    if first == "organize" or first.endswith("/organize"):
        return tokens[1:]
    return None


def organize_invocations(text: str) -> list[list[str]]:
    return [args for line in shell_lines(text) if (args := organize_args(line)) is not None]


def test_every_documented_organize_command_exists() -> None:
    """08 §B17: the old docs referenced ``with_transaction``,
    ``NoteEmitter.sync``, ``scripts.automation.registry`` and a
    ``para-automation.sh`` — none of which existed. Docs ship in the same PR
    as behavior, so they are tested like behavior."""
    known_options = cli_option_strings()
    invocations: list[tuple[str, list[str]]] = []
    for tokens in organize_invocations(README.read_text(encoding="utf-8")):
        invocations.append((README.name, tokens))
    for unit in UNIT_FILES:
        for line in exec_start_lines(unit):
            args = organize_args(line)
            if args is not None:
                invocations.append((unit.name, args))

    assert invocations, "no organize invocation found — the extractor is broken"

    problems: list[str] = []
    for name, tokens in invocations:
        if not tokens:
            continue  # a bare mention of the binary's path, not a command
        rendered = f"`organize {' '.join(tokens)}`"
        if not any(token in SUBCOMMANDS for token in tokens):
            problems.append(f"{name}: {rendered} names no subcommand")
        for token in tokens:
            if not token.startswith("-"):
                continue
            flag = token.split("=", 1)[0]
            if flag not in known_options:
                problems.append(f"{name}: unknown flag {flag!r} in {rendered}")
    assert problems == [], "\n".join(problems)


def test_the_unit_invokes_the_pipeline_the_way_the_parser_accepts_it() -> None:
    """Global flags precede the subcommand in this parser; an ExecStart with
    them in the wrong order fails at run time, in the background, where
    nobody is watching."""
    tokens = organize_args(exec_start_lines(SERVICE)[0])
    assert tokens is not None
    parsed = build_parser().parse_args(tokens)
    assert parsed.command == "run-consumers"
    assert parsed.config and parsed.config.endswith("config.toml")


# ---------------------------------------------------------------------------
# the cutover checklist (09 §5.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "needle, why",
    [
        ("second-brain-automation", "the old chain must be named to be retired"),
        ("Beeper", "09 §5.5: leave Beeper sync running, independently"),
        ("para-automation-watcher.path", "the old capture watcher points at the old chain"),
        ("automations.db", "09 §5.4: migrate the DB, backed up first"),
        ("--dry-run", "09 §5.6: the first supervised run is a rehearsal"),
        ("organize-pipeline-failtest", "09 §5.5: prove OnFailure actually fires"),
        ("systemctl --user enable", "the install step the checklist asks for"),
    ],
)
def test_the_readme_carries_the_cutover_checklist(needle: str, why: str) -> None:
    assert needle in README.read_text(encoding="utf-8"), why


def test_the_cutover_never_turns_off_the_unit_that_carries_beeper_sync() -> None:
    """The PARA step and the Beeper sync are welded into ONE vault-side
    script driven by ``second-brain-automation.service``. "Retire the old
    chain" must therefore mean "delete the PARA block from that script", not
    "disable that unit" — the second reading silently stops Matt's messaging
    sync (spec 06 §5: do not couple them; 09 §5.5: leave Beeper independent).

    This is a real trap, not a hypothetical: disabling the timer is the
    obvious way to stop the old pipeline and it is the wrong one.
    """
    text = README.read_text(encoding="utf-8")
    for line in shell_lines(text):
        tokens = tokenize(line)
        if not tokens or tokens[0] != "systemctl":
            continue
        words = [t for t in tokens[1:] if not t.startswith("-")]
        if not words:
            continue
        verb, targets = words[0], words[1:]
        if verb in {"disable", "stop", "mask"}:
            assert not any("second-brain-automation" in t for t in targets), (
                f"`{line.strip()}` would stop Beeper sync too (09 §5.5)"
            )

    assert re.search(
        r"leave\s+\S*second-brain-automation\S*\s+enabled",
        " ".join(text.split()),
        re.IGNORECASE,
    ), "the README must say in as many words that the Beeper unit stays enabled"


# ---------------------------------------------------------------------------
# systemd's own opinion
# ---------------------------------------------------------------------------


def test_systemd_analyze_verifies_every_unit(tmp_path: Path) -> None:
    """``systemd-analyze verify`` is the only checker that knows which
    directives exist in which section. It also insists ExecStart's binary is
    present, which is a fact about the machine rather than the unit — so the
    binaries are substituted with a real executable first."""
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze unavailable on this host")

    stand_in = tmp_path / "stand-in"
    stand_in.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stand_in.chmod(0o755)

    staged = tmp_path / "units"
    staged.mkdir()
    for unit in UNIT_FILES:
        rewritten = []
        for line in unit.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^(ExecStart=)(\S+)(.*)$", line)
            if match and not Path(match.group(2)).is_file():
                line = f"{match.group(1)}{stand_in}{match.group(3)}"
            rewritten.append(line)
        (staged / unit.name).write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    failures: list[str] = []
    for unit in UNIT_FILES:
        proc = subprocess.run(
            [analyze, "--user", "verify", f"./{unit.name}"],
            cwd=staged,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        # A misspelled directive is only a WARNING to systemd-analyze — it
        # says "Unknown key … ignoring" and exits 0. Silently ignored is
        # exactly the failure mode being guarded against, so any line naming
        # one of our units counts, whatever the exit status. Unrelated
        # dangling units in the caller's own unit directory are noise and
        # never mention our filenames.
        ours = [
            line
            for line in (proc.stdout + proc.stderr).splitlines()
            if any(other.name in line for other in UNIT_FILES)
        ]
        if ours:
            failures.append(f"{unit.name}: " + "; ".join(ours))
    assert failures == [], "\n".join(failures)
