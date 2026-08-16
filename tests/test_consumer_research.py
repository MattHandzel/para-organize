"""``deep_research`` consumer — construction, routing and command building
(spec 06 §3.4; regressions 08 §B2/§B3/§B10/§B12/§B14/§B15).

The dispatch half (real subprocesses against a fake agent script) lives in
``test_consumer_research_dispatch.py``; the shipped systemd units are gated
by ``test_consumer_research_deploy.py``. Helpers below are shared by all
three.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import pytest

from organize_core.config import Config, ConsumerConfig, VaultConfig
from organize_core.consumers import deep_research as dr
from organize_core.consumers.base import (
    Consumer,
    NotePayload,
    RunContext,
    Status,
    get_consumer_types,
)
from organize_core.errors import ConfigError, OrganizeError

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def make_payload(
    path: Path | str,
    frontmatter: dict[str, Any] | None = None,
    content: str = "Some person worth researching.",
) -> NotePayload:
    """A NotePayload exactly as the runner's ingestion builds one."""
    fm = dict(frontmatter or {})
    raw_text = "---\n" + "\n".join(f"{k}: {v!r}" for k, v in fm.items()) + "\n---\n" + content
    return NotePayload(
        path=Path(path),
        frontmatter=fm,
        content=content,
        raw_text=raw_text,
        note_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
    )


def make_consumer(**options: Any) -> dr.DeepResearchConsumer:
    env = options.pop("_env", {})
    include = options.pop("_include_paths", ["areas/relationships"])
    return dr.DeepResearchConsumer(
        ConsumerConfig(
            name="deep_research",
            type="deep_research",
            include_paths=list(include),
            max_notes_per_run=5,
            options=dict(options),
            env=dict(env),
        )
    )


def make_ctx(tmp_path: Path, *, dry_run: bool = False) -> RunContext:
    return RunContext(config=Config(vault=VaultConfig(root=tmp_path)), dry_run=dry_run)


def executed_names(module_file: str) -> set[str]:
    """Every name/attribute/import the module actually EXECUTES (docstrings
    and comments excluded by construction) — same technique as
    ``tests/test_repo_hygiene.py``."""
    tree = ast.parse(Path(module_file).read_text(encoding="utf-8"), filename=module_file)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
            if isinstance(node.value, ast.Name):
                names.add(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
            if node.module:
                names.add(node.module.split(".")[0])
    return names


# ---------------------------------------------------------------------------
# registration + option validation
# ---------------------------------------------------------------------------


def test_registered_under_its_config_type_name() -> None:
    assert get_consumer_types()["deep_research"] is dr.DeepResearchConsumer
    assert issubclass(dr.DeepResearchConsumer, Consumer)


def test_uses_llm_is_false_but_no_ai_is_still_honored_locally() -> None:
    """Ambiguity ruling #12: the central runner guard keys off ``uses_llm``
    (prompt building), which this consumer does not do — so the vault law is
    enforced in ``should_process`` instead. Both halves are contract."""
    assert dr.DeepResearchConsumer.uses_llm is False
    consumer = make_consumer(command=["agent"])
    assert consumer.should_process(make_payload("x.md", {"no-ai": True})) is False


def test_command_is_required() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_consumer(timeout_seconds=5)
    assert "command" in str(excinfo.value)


@pytest.mark.parametrize(
    "options, needle",
    [
        ({"command": []}, "must not be empty"),
        ({"command": ""}, "must not be empty"),
        ({"command": 42}, "string or a list"),
        ({"command": ["agent", 7]}, "list of strings"),
        ({"command": ["agent"], "timeout_seconds": "soon"}, "must be a number"),
        ({"command": ["agent"], "timeout_seconds": 0}, "must be positive"),
        ({"command": ["agent"], "timeout_seconds": -1}, "must be positive"),
        ({"command": ["agent"], "timeout_seconds": True}, "must be a number"),
        ({"command": ["agent"], "notes_dir": 3}, "must be a string"),
        ({"command": ["agent"], "cwd": []}, "must be a string"),
    ],
)
def test_bad_options_raise_configerror_naming_the_key(
    options: dict[str, Any], needle: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_consumer(**options)
    message = str(excinfo.value)
    assert needle in message
    assert "consumers.deep_research" in message


def test_unknown_option_is_refused_by_name() -> None:
    """The every-key-honored law (03 §1) reaches consumer options too: a
    typo'd key must fail loudly rather than be silently ignored."""
    with pytest.raises(ConfigError) as excinfo:
        make_consumer(command=["agent"], timeout_second=30)
    assert "'timeout_second'" in str(excinfo.value)
    assert excinfo.value.hint and "timeout_seconds" in excinfo.value.hint


def test_every_configerror_is_in_the_taxonomy() -> None:
    with pytest.raises(OrganizeError):
        make_consumer(command=None)


def test_string_command_is_shell_split() -> None:
    consumer = make_consumer(command="agent --flag 'two words'")
    assert consumer.command_template == ["agent", "--flag", "two words"]


def test_blank_list_entries_are_dropped_but_meaningful_ones_kept() -> None:
    consumer = make_consumer(command=["agent", "  ", "--person", ""])
    assert consumer.command_template == ["agent", "--person"]


def test_timeout_defaults_to_the_spec_value() -> None:
    assert make_consumer(command=["agent"]).timeout_seconds == dr.DEFAULT_TIMEOUT_SECONDS
    assert dr.DEFAULT_TIMEOUT_SECONDS == 600.0


def test_cwd_and_working_directory_are_aliases() -> None:
    """The shipped example config says ``cwd``; doc 06 §3.4 and the live
    ``automations.toml`` say ``working_directory``. Accepting only one of
    them would break a file that already exists."""
    assert make_consumer(command=["a"], cwd="~/agent").working_directory == "~/agent"
    assert (
        make_consumer(command=["a"], working_directory="~/agent").working_directory
        == "~/agent"
    )
    assert (
        make_consumer(command=["a"], cwd="~/agent", working_directory="~/agent").working_directory
        == "~/agent"
    )


def test_conflicting_cwd_aliases_are_refused() -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_consumer(command=["a"], cwd="~/one", working_directory="~/two")
    assert "cwd" in str(excinfo.value) and "working_directory" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 08 §B2 — the constructor is PURE
# ---------------------------------------------------------------------------


def test_constructor_does_no_io_at_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """B2 was the outage's force multiplier: constructors did I/O, all
    consumers were constructed eagerly, and one bad consumer killed all
    four. Here every I/O door is booby-trapped and construction must still
    succeed."""
    import subprocess as subprocess_mod

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("constructor performed I/O")

    monkeypatch.setattr(subprocess_mod, "run", explode)
    monkeypatch.setattr(dr.core_paths, "default_env", explode)
    monkeypatch.setattr(dr.core_paths, "expand", explode)
    monkeypatch.setattr(Path, "exists", explode)
    monkeypatch.setattr(Path, "is_dir", explode)
    monkeypatch.setattr(Path, "resolve", explode)

    consumer = make_consumer(
        command=["/definitely/not/here/agent", "{path_quoted}"],
        cwd=str(tmp_path / "missing-checkout"),
        notes_dir=str(tmp_path / "missing-notes"),
        timeout_seconds=1,
    )
    assert consumer.command_template[0] == "/definitely/not/here/agent"


def test_missing_working_directory_does_not_break_construction(tmp_path: Path) -> None:
    """The live consumer raised ``ValueError`` from ``__init__`` when the
    agent checkout was absent — with eager construction that took the whole
    pipeline down. Now it is a per-note ERROR (see the dispatch suite)."""
    consumer = make_consumer(command=["agent"], cwd=str(tmp_path / "nope"))
    assert consumer.resolve_working_directory({}) == (tmp_path / "nope").resolve()


# ---------------------------------------------------------------------------
# 08 §B3/§B12 — checkpointing is the runner's, exclusively
# ---------------------------------------------------------------------------


def test_module_never_reaches_for_the_store() -> None:
    """B12: the old consumer called ``store.mark_emitted`` itself AND the
    orchestrator checkpointed — two owners, and ``limit``/``error`` results
    got persisted (B3). This module must not even be able to: no store
    import, no checkpoint call, no store parameter on ``handle``."""
    forbidden = {"AutomationStore", "mark_emitted", "checkpoint", "store", "needs_delivery"}
    assert executed_names(dr.__file__) & forbidden == set()

    import inspect

    params = list(inspect.signature(dr.DeepResearchConsumer.handle).parameters)
    assert params == ["self", "payload", "ctx"]


# ---------------------------------------------------------------------------
# routing / no-ai (06 §3.4, spec 02 vault law)
# ---------------------------------------------------------------------------


def test_dispatches_generically_without_content_matching() -> None:
    """"Generic dispatcher, no content matching; routing purely by
    include_paths" — an empty note in scope is still a job."""
    consumer = make_consumer(command=["agent"])
    assert consumer.should_process(make_payload("areas/relationships/ada.md", {}, "")) is True


@pytest.mark.parametrize(
    "frontmatter, expected",
    [
        ({"no-ai": True}, False),
        ({"no_ai": True}, False),
        ({"No-AI": True}, False),
        ({"no-ai": "true"}, False),
        ({"no-ai": "yes"}, False),
        ({"no-ai": False}, True),
        ({"no-ai": "false"}, True),
        ({}, True),
        ({"tags": ["person"]}, True),
    ],
)
def test_no_ai_frontmatter_blocks_dispatch(frontmatter: dict[str, Any], expected: bool) -> None:
    consumer = make_consumer(command=["agent"])
    assert consumer.should_process(make_payload("areas/relationships/x.md", frontmatter)) is expected


def test_the_shared_payload_property_is_what_this_consumer_consults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interim ``deep_research.payload_no_ai`` fallback is GONE (Phase-3
    architect ruling): ``NotePayload.no_ai`` landed in the shared base and is
    now the only door. Pinned in both directions so the guard can never be
    merely consulted and then ignored, and so nobody reintroduces a second
    local parser.
    """
    assert not hasattr(dr, "payload_no_ai"), "the interim fallback must stay deleted"

    consumer = make_consumer(command=["agent"])
    monkeypatch.setattr(NotePayload, "no_ai", property(lambda self: True))
    assert consumer.should_process(make_payload("areas/relationships/x.md", {})) is False

    monkeypatch.setattr(NotePayload, "no_ai", property(lambda self: False))
    assert (
        consumer.should_process(make_payload("areas/relationships/x.md", {"no-ai": True})) is True
    )


def test_the_no_ai_rule_is_the_shared_frontmatter_rule() -> None:
    """And it is not a hand-rolled re-parse (08 §B9 / 09 §2): the payload
    property agrees with ``frontmatter.is_no_ai`` on every shape that module
    accepts, because both call the same field-level helper."""
    from organize_core import frontmatter

    consumer = make_consumer(command=["agent"])
    for value, expected in [(True, True), ("yes", True), ("1", True), (False, False), ("no", False)]:
        doc = frontmatter.Document(
            frontmatter=frontmatter.Frontmatter(fields={"no-ai": value}), body=""
        )
        assert frontmatter.is_no_ai(doc) is expected
        payload = make_payload("areas/relationships/x.md", {"no-ai": value})
        assert payload.no_ai is expected
        assert consumer.should_process(payload) is (not expected)


def test_string_valued_aliases_do_not_degrade_anything() -> None:
    """08 §B10 in its general form: a scalar where a list was assumed. This
    consumer reads no title, and this test pins that it stays that way — a
    string ``aliases`` must be a complete non-event."""
    consumer = make_consumer(command=["agent"])
    payload = make_payload("areas/relationships/x.md", {"aliases": "Ada Lovelace"})
    assert consumer.should_process(payload) is True
    assert consumer.build_command(payload, env={})[-1] == str(payload.path)


# ---------------------------------------------------------------------------
# command building (06 §3.4; 08 §B15 literal braces)
# ---------------------------------------------------------------------------


def test_path_is_appended_when_no_placeholder_is_used() -> None:
    consumer = make_consumer(command=["agent", "--verbose"])
    payload = make_payload("/vault/areas/relationships/ada.md")
    assert consumer.build_command(payload, env={}) == [
        "agent",
        "--verbose",
        "/vault/areas/relationships/ada.md",
    ]


@pytest.mark.parametrize("placeholder", ["{path}", "{path_quoted}"])
def test_path_is_not_appended_twice_when_a_placeholder_is_used(placeholder: str) -> None:
    consumer = make_consumer(command=["agent", placeholder])
    command = consumer.build_command(make_payload("/vault/ada.md"), env={})
    assert len(command) == 2
    assert command[1] in {"/vault/ada.md", "'/vault/ada.md'"}


def test_all_four_placeholders_render(tmp_path: Path) -> None:
    notes = tmp_path / "notes dir"
    notes.mkdir()
    consumer = make_consumer(
        command=["agent", "--notes", "{notes_dir}", "--q", "{notes_dir_quoted}", "{path}"],
        notes_dir=str(notes),
    )
    command = consumer.build_command(make_payload("/vault/ada.md"), env={})
    assert command == [
        "agent",
        "--notes",
        str(notes.resolve()),
        "--q",
        f"'{notes.resolve()}'",
        "/vault/ada.md",
    ]


def test_placeholders_quote_paths_that_need_it() -> None:
    consumer = make_consumer(command=["sh", "-c", "agent --person {path_quoted}"])
    command = consumer.build_command(make_payload("/vault/areas/Ada Lovelace's note.md"), env={})
    assert command[2] == "agent --person '/vault/areas/Ada Lovelace'\"'\"'s note.md'"


def test_notes_dir_placeholders_are_empty_when_unset() -> None:
    consumer = make_consumer(command=["agent", "--notes", "{notes_dir}", "{path}"])
    assert consumer.build_command(make_payload("/v/a.md"), env={}) == [
        "agent",
        "--notes",
        "",
        "/v/a.md",
    ]


@pytest.mark.parametrize(
    "part",
    [
        '{"query": "person", "depth": 2}',
        "jq '.results[] | {name, url}'",
        "--format={{name}}",
        "${SHELL_VAR}",
        "{}",
        "{ }",
        "{PATH}",
        "{path-quoted}",
    ],
)
def test_literal_braces_never_raise_and_never_change(part: str) -> None:
    """08 §B15's second half: ``str.format`` over the template raised
    ``KeyError``/``ValueError``/``IndexError`` on any of these, so a
    perfectly good command could not be configured at all. Every one of them
    must now pass through byte-identical."""
    consumer = make_consumer(command=["agent", part])
    assert consumer.build_command(make_payload("/v/a.md"), env={})[1] == part


def test_unknown_placeholder_is_left_verbatim_rather_than_crashing() -> None:
    consumer = make_consumer(command=["agent", "{pathh}", "{path}"])
    assert consumer.build_command(make_payload("/v/a.md"), env={})[1] == "{pathh}"


def test_render_template_is_a_pure_function() -> None:
    assert dr.render_template("{a} {b}", {"a": "1"}) == "1 {b}"
    assert dr.render_template("{}", {}) == "{}"
    assert dr.render_template("no braces", {"a": "1"}) == "no braces"


def test_tilde_parts_expand_but_flags_are_left_alone(tmp_path: Path) -> None:
    """``expand`` resolves relative paths against the process CWD, so
    expanding indiscriminately would turn ``--person`` into
    ``<cwd>/--person``. Only a leading ``~``/``$`` marks a path."""
    consumer = make_consumer(command=["~/agent/main.py", "--person", "{path}"])
    command = consumer.build_command(make_payload("/v/a.md"), env={"HOME": str(tmp_path)})
    assert command[0] == str((tmp_path / "agent/main.py").resolve())
    assert command[1] == "--person"


def test_a_set_variable_expands_and_an_unset_one_is_left_verbatim(tmp_path: Path) -> None:
    """Caught by the literal-brace suite: ``expand`` resolves, so running an
    unsubstituted ``${SHELL_VAR}`` through it produced
    ``<process cwd>/${SHELL_VAR}`` — an argument silently rewritten into a
    path that depends on where systemd happened to start the run."""
    consumer = make_consumer(command=["$AGENT_HOME/main.py", "${MISSING_VAR}", "{path}"])
    command = consumer.build_command(
        make_payload("/v/a.md"), env={"AGENT_HOME": str(tmp_path / "agent")}
    )
    assert command[0] == str((tmp_path / "agent" / "main.py").resolve())
    assert command[1] == "${MISSING_VAR}"


def test_expansion_never_mangles_a_part_that_carries_a_placeholder(tmp_path: Path) -> None:
    consumer = make_consumer(command=["~/agent {path}"])
    command = consumer.build_command(make_payload("/v/a.md"), env={"HOME": str(tmp_path)})
    assert command == ["~/agent /v/a.md"]


def test_command_building_is_deterministic_across_instances(tmp_path: Path) -> None:
    """Rerun idempotency at the level this consumer owns: two runs of the
    same config produce the same argv, so the runner's hash checkpoint is
    the ONLY thing deciding whether the agent fires again."""
    payload = make_payload("/vault/areas/relationships/ada.md")
    first = make_consumer(command=["agent", "{path_quoted}"], notes_dir=str(tmp_path))
    second = make_consumer(command=["agent", "{path_quoted}"], notes_dir=str(tmp_path))
    assert first.build_command(payload, env={}) == second.build_command(payload, env={})


# ---------------------------------------------------------------------------
# environment building (08 §B15 first half)
# ---------------------------------------------------------------------------


def test_notes_dir_is_injected_when_the_caller_supplied_nothing(tmp_path: Path) -> None:
    consumer = make_consumer(command=["agent"], notes_dir=str(tmp_path))
    env = consumer.build_env(make_payload("/v/a.md"), {"PATH": "/usr/bin"})
    assert env["NOTES_DIR"] == str(tmp_path.resolve())
    assert env["PATH"] == "/usr/bin"


def test_caller_supplied_notes_dir_wins_over_the_option(tmp_path: Path) -> None:
    """THE B15 regression. The old guard read ``"notes_dir" in env`` while
    writing ``NOTES_DIR`` — a key that is never present — so the option
    clobbered the caller's value on every single run."""
    consumer = make_consumer(command=["agent"], notes_dir=str(tmp_path / "option"))
    env = consumer.build_env(make_payload("/v/a.md"), {"NOTES_DIR": "/caller/wins"})
    assert env["NOTES_DIR"] == "/caller/wins"


def test_a_lowercase_notes_dir_in_the_environment_does_not_suppress_injection(
    tmp_path: Path,
) -> None:
    """The inverse of the same bug: the guard must key off the variable it
    actually sets, not a lowercase near-miss."""
    consumer = make_consumer(command=["agent"], notes_dir=str(tmp_path))
    env = consumer.build_env(make_payload("/v/a.md"), {"notes_dir": "/unrelated"})
    assert env["NOTES_DIR"] == str(tmp_path.resolve())
    assert env["notes_dir"] == "/unrelated"


def test_config_env_table_outranks_both_environment_and_option(tmp_path: Path) -> None:
    consumer = make_consumer(
        command=["agent"],
        notes_dir=str(tmp_path / "option"),
        _env={"NOTES_DIR": "/from/config", "EXA_API_KEY": "secret"},
    )
    env = consumer.build_env(make_payload("/v/a.md"), {"NOTES_DIR": "/from/caller"})
    assert env["NOTES_DIR"] == "/from/config"
    assert env["EXA_API_KEY"] == "secret"


def test_env_table_expands_a_leading_tilde_but_never_a_secret(tmp_path: Path) -> None:
    """The shipped example passes ``NOTES_DIR = "~/Obsidian/Main"`` — an
    unexpanded tilde reaches the agent as a literal directory name. A value
    containing ``$`` may be a credential and is passed through verbatim."""
    consumer = make_consumer(
        command=["agent"],
        _env={"NOTES_DIR": "~/vault", "TOKEN": "$ecret$HOME-ish"},
    )
    env = consumer.build_env(make_payload("/v/a.md"), {"HOME": str(tmp_path)})
    assert env["NOTES_DIR"] == str((tmp_path / "vault").resolve())
    assert env["TOKEN"] == "$ecret$HOME-ish"


def test_build_env_does_not_mutate_the_caller_environment() -> None:
    consumer = make_consumer(command=["agent"], notes_dir="/n", _env={"A": "1"})
    base = {"PATH": "/usr/bin"}
    consumer.build_env(make_payload("/v/a.md"), base)
    assert base == {"PATH": "/usr/bin"}


# ---------------------------------------------------------------------------
# dry-run (09 §5.6)
# ---------------------------------------------------------------------------


def test_dry_run_reports_without_dispatching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as subprocess_mod

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry run spawned a process")

    monkeypatch.setattr(subprocess_mod, "run", explode)
    consumer = make_consumer(command=["agent", "{path}"])
    result = consumer.handle(
        make_payload("/vault/areas/relationships/ada.md"), make_ctx(tmp_path, dry_run=True)
    )
    assert result.status is Status.SKIP
    assert result.metadata["dry_run"] is True
    assert result.metadata["command"] == ["agent", "/vault/areas/relationships/ada.md"]
