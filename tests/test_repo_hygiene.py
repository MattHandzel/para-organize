"""Repo-wide structural gates (ARCHITECTURE "structural safety decisions").

Every rule here was previously enforced for exactly ONE module — and the one
place a rule was ungated is the place it was already broken (`index.py` read
`HOME` through `Path.expanduser()`, which made a vault-relative note whose
name starts with `~` raise a bare `RuntimeError`, an exception outside the
whole `OrganizeError` taxonomy). These tests are parametrized over the whole
package so a future seat cannot regress a decision in a module nobody
happened to write a guard for.

Every check works on the AST, not on raw text: a rule that trips over its own
explanatory comment is a rule people delete.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "organize_core"
TESTS = Path(__file__).resolve().parent

#: Every module in the package, by import name.
MODULES: list[str] = sorted(
    str(path.relative_to(SRC).with_suffix("")).replace("/", ".")
    for path in SRC.rglob("*.py")
    if path.name != "__init__.py"
)


def _tree(module: str) -> ast.Module:
    path = SRC / (module.replace(".", "/") + ".py")
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _attribute_names(tree: ast.AST) -> set[str]:
    """``{"os.environ", "Path.home", "expanduser", ...}`` for every attribute
    access and bare name the module actually EXECUTES (docstrings, comments
    and string literals excluded by construction)."""
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


def _non_docstring_strings(tree: ast.Module) -> list[str]:
    """Every string CONSTANT in the module except docstrings."""
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                out.append(node.value)
    return out


# --- decision 4: only paths.py touches the environment ---------------------

#: Anything that reaches the process environment. ``expanduser`` is on the
#: list because it reads ``HOME``.
ENV_READERS: frozenset[str] = frozenset(
    {"environ", "getenv", "expanduser", "expandvars", "Path.home", "os.environ", "os.getenv"}
)


@pytest.mark.parametrize("module", MODULES)
def test_no_module_outside_paths_reads_the_environment(module: str) -> None:
    """Structural decision 4: nothing consults ``os.environ`` / ``Path.home()``
    outside ``paths.py``.

    ``config.vault.root`` is expanded exactly once, by
    ``config._expand_path`` → ``paths.expand``; note arguments are resolved
    by the composition roots. A second expansion inside a module is both a
    hidden environment read and a bug: it turns a legal vault filename
    beginning with ``~`` into a home-directory lookup. This used to be
    asserted for ``fileops`` and ``server`` only, and ``index`` — the one
    unguarded module that mattered — was already broken.
    """
    if module == "paths":
        return  # the single, deliberate touchpoint
    used = _attribute_names(_tree(module)) & ENV_READERS
    # A "home" attribute on something that is not `Path` is fine.
    used.discard("home")
    assert used == set(), (
        f"{module}.py must take paths from config/CorePaths (found {sorted(used)}); "
        "paths.py is the only module allowed to read the environment"
    )


# --- decision 2: ONE frontmatter parser ------------------------------------


@pytest.mark.parametrize("module", MODULES)
def test_only_the_frontmatter_module_imports_yaml(module: str) -> None:
    """Structural decision 2: "One frontmatter module … any second parser is
    a review reject". The round-trip law (05 §9, 08 §A12 — the worst
    data-loss bug in the original) is only a law if there is one parser to
    hold it. Currently satisfied, previously ungated."""
    if module == "frontmatter":
        return
    tree = _tree(module)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "yaml" not in imported, (
        f"{module}.py imports yaml; frontmatter.py is the ONE parser "
        "(structural decision 2)"
    )


# --- decision 3: the scoring layer never reads a clock ---------------------

CLOCK_READERS: frozenset[str] = frozenset(
    {"time.time", "time.monotonic", "datetime.now", "datetime.utcnow", "date.today", "utcnow"}
)


@pytest.mark.parametrize("module", ["suggest", "learn"])
def test_the_scoring_layer_never_reads_a_clock(module: str) -> None:
    """Structural decision 3 / spec 09 §3: ``now`` is always INJECTED into
    suggest and learn, which is what makes their numeric goldens exact
    assertions instead of ">0" hand-waving. Currently satisfied, previously
    ungated."""
    used = _attribute_names(_tree(module)) & CLOCK_READERS
    assert used == set(), (
        f"{module}.py must take `now` as a parameter (found {sorted(used)}); "
        "every timestamp is injected (spec 09 §3)"
    )


# --- decision 6: no shelling out for filesystem work (05 §1.6) -------------

#: The modules that may spawn a process (Phase-3 architect ruling). The
#: exemption is for spawning an EXTERNAL TOOL the spec names BY NAME —
#: ``llm.py``'s ``claude-cli`` backend (09 §2), ``task`` (06 §3.1),
#: ``yt-dlp`` (06 §3.2), the research agent command (06 §3.4). It is never
#: an exemption for FILESYSTEM work: no find/ls/cp/mv/rm equivalents, ever
#: (spec 05 §1.6 still binds). A module that genuinely needs ``Popen``
#: requests a ruling rather than editing this set.
SUBPROCESS_ALLOWED: frozenset[str] = frozenset(
    {
        "llm",
        "consumers.deep_research",
        "consumers.taskwarrior",
        "consumers.learn",
    }
)


@pytest.mark.parametrize("module", MODULES)
def test_no_module_shells_out_for_filesystem_work(module: str) -> None:
    """05 §1.6 / 08 §A28: the original had six ``io.popen("find …")`` sites,
    three with unquoted paths. All filesystem work happens in-process."""
    if module in SUBPROCESS_ALLOWED:
        return
    used = _attribute_names(_tree(module)) & {"subprocess", "system", "popen"}
    assert used == set(), f"{module}.py must not shell out (found {sorted(used)})"


def _subprocess_spawn_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``subprocess.run``/``.Popen``/``.call``/``.check_output`` call."""
    spawners = {"run", "Popen", "call", "check_call", "check_output"}
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in spawners
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            out.append(node)
    return out


@pytest.mark.parametrize("module", sorted(SUBPROCESS_ALLOWED))
def test_an_allowlisted_module_never_uses_a_shell(module: str) -> None:
    """The exemption buys ONE thing: argv-list spawning of a named tool.
    ``shell=True`` turns a config-supplied command into an injection surface
    (08 §A28), so it is structurally banned even where spawning is allowed."""
    offenders = [
        call.lineno
        for call in _subprocess_spawn_calls(_tree(module))
        for kw in call.keywords
        if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
    ]
    assert offenders == [], f"{module}.py passes shell= at line(s) {offenders} — argv lists only"


@pytest.mark.parametrize("module", sorted(SUBPROCESS_ALLOWED))
def test_an_allowlisted_module_never_passes_a_command_string(module: str) -> None:
    """First argument is a LIST, never a plain string: a string argv is the
    same injection class as ``shell=True`` on some platforms, and it is how a
    path with a space silently became two arguments (08 §A28)."""
    offenders = [
        call.lineno
        for call in _subprocess_spawn_calls(_tree(module))
        if call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    ]
    assert offenders == [], (
        f"{module}.py passes a command STRING at line(s) {offenders} — build an argv list"
    )


@pytest.mark.parametrize("module", sorted(SUBPROCESS_ALLOWED))
def test_every_spawn_carries_a_timeout_and_a_lenient_decode(module: str) -> None:
    """B1, made structural. The 3-month outage was one ``task export`` byte
    decoded strict-UTF-8 with no timeout. Every spawn in every allowlisted
    module must carry ``timeout=`` and, when it decodes text, ``encoding=``
    plus ``errors=`` — a future call site cannot forget (06 §6)."""
    missing: list[str] = []
    for call in _subprocess_spawn_calls(_tree(module)):
        kwargs = {kw.arg for kw in call.keywords if kw.arg}
        if "timeout" not in kwargs:
            missing.append(f"line {call.lineno}: no timeout=")
        decodes = "encoding" in kwargs or "text" in kwargs or "universal_newlines" in kwargs
        if decodes and "errors" not in kwargs:
            missing.append(f"line {call.lineno}: decodes text with no errors=")
        if decodes and "encoding" not in kwargs:
            missing.append(f"line {call.lineno}: text mode without an explicit encoding=")
    assert missing == [], f"{module}.py: " + "; ".join(missing)


# --- 08 §A37: test infra must not hardcode one machine's checkout ----------


def test_no_test_file_hardcodes_an_absolute_home_path() -> None:
    """08 §A37, generalized. ``tests/test_cli_blackbox.py`` hardcoded
    ``$HOME/Projects/.../organize-rewrite`` and derived both its conftest
    import path and the binary under test from it, so 49 black-box tests
    passed in any other worktree while exercising that one checkout's code —
    including a copy whose CLI could not be imported at all.

    Everything a test needs is reachable from ``Path(__file__)``,
    ``sys.executable`` or ``tmp_path``. Docstrings are exempt (they explain
    the rule); executable string literals are not.

    ANCHORED at the start of the string, not a bare substring. ``"/home/"``
    anywhere matched a VAULT-RELATIVE fixture path like
    ``areas/home/errands.md`` — and ``areas/home`` is a perfectly plausible
    real PARA folder, so the rule was rejecting correct test data. (Reported
    by the actions-similarity seat, which worked around it by renaming its
    fixture; the workaround is unnecessary now.) What the rule is actually
    about is an ABSOLUTE machine path, which by definition starts at the
    root.
    """
    this_file = Path(__file__).resolve()
    absolute_home = re.compile(r"^/(home|Users)/")
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        if path.resolve() == this_file:
            continue  # this file names the forbidden prefixes to search for
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for text in _non_docstring_strings(tree):
            if absolute_home.match(text):
                offenders.append(f"{path.name}: {text[:80]!r}")
    assert offenders == [], (
        "test files must locate the repo from __file__/sys.executable, never an "
        "absolute machine path (08 §A37):\n" + "\n".join(offenders)
    )


# --- 08 §B12: checkpointing has exactly ONE owner (the runner) -------------

#: Every consumer module — the classes that do the work — excluding the two
#: modules that ARE the store and its single owner.
CONSUMER_MODULES: list[str] = sorted(
    m
    for m in MODULES
    if m.startswith("consumers.") and m not in {"consumers.runner", "consumers.store"}
)

#: Names that only the orchestrator may execute. ``store`` is included
#: deliberately: a ``store`` parameter, attribute or local in a consumer is
#: the shape of the two-owner bug, whatever it is called downstream.
STORE_NAMES: frozenset[str] = frozenset(
    {
        "AutomationStore",
        "checkpoint",
        "mark_emitted",
        "mark_seen",
        "needs_delivery",
        "soft_purge",
        "restore_purged",
        "store",
    }
)


def test_the_consumer_module_list_is_not_empty() -> None:
    """A parametrized gate over an empty list is a green test that checks
    nothing — the exact vacuity this file exists to prevent."""
    assert len(CONSUMER_MODULES) >= 4, CONSUMER_MODULES


@pytest.mark.parametrize("module", CONSUMER_MODULES)
def test_no_consumer_reaches_for_the_store(module: str) -> None:
    """08 §B12: the old consumer called ``store.mark_emitted`` itself AND the
    orchestrator checkpointed — two owners, and that is how ``limit``/``error``
    rows got persisted (B3), which silently dropped every over-cap note
    forever.

    This was gated for ``deep_research`` alone. A second owner inserted into
    ``learn.handle`` — a real ``AutomationStore`` opened on
    ``ctx.paths.state_dir / "automations.db"``, writing its own checkpoint —
    passed the entire suite. Now every consumer is held to it.
    """
    found = _attribute_names(_tree(module)) & STORE_NAMES
    assert found == set(), (
        f"{module}.py reaches for the store ({sorted(found)}); checkpointing "
        "belongs to consumers/runner.py alone (06 §1, 08 §B12)"
    )


@pytest.mark.parametrize("module", CONSUMER_MODULES)
def test_no_consumer_handle_takes_a_store_parameter(module: str) -> None:
    """The other half of the single-owner shape: a consumer that is HANDED a
    store does not need to import one."""
    banned = {"store", "automations", "db", "conn", "connection"}
    for node in ast.walk(_tree(module)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in {"handle", "should_process", "__init__"}:
            continue
        args = node.args
        params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        assert params & banned == set(), (
            f"{module}.{node.name} takes {sorted(params & banned)}; consumers "
            "never receive persistence (06 §1, 08 §B12)"
        )


# --- 08 §B2: consumer CONSTRUCTORS ARE PURE --------------------------------


def test_every_registered_consumer_constructor_does_no_io() -> None:
    """06 §1: "Consumer constructors are pure; external I/O happens lazily on
    first real work". 08 §B2 is the three-month outage: a ``UnicodeDecodeError``
    in ``TaskwarriorConsumer.__init__`` took the whole pipeline down every ten
    minutes, and constructors run BEFORE ``--consumer`` filtering can isolate
    anything.

    The per-consumer purity tests each patch a different subset — the
    taskwarrior one, for the constructor that actually caused the outage, only
    patched ``subprocess.run``, so filesystem I/O reintroduced there (a taskrc
    read, a data-dir stat, a backup-dir scan) passed the whole suite. This
    gate patches every door, for every registered type, in one place.

    The patches are installed and removed by hand rather than through
    ``monkeypatch``: ``Path.exists`` is on the list, and pytest itself calls
    it while rendering a failure, so a patch still live at assertion time
    turns any violation into an INTERNALERROR instead of a readable report.
    """
    import builtins
    import subprocess
    from pathlib import Path as _Path

    from organize_core.config import ConsumerConfig
    from organize_core.consumers import get_consumer_types, get_implemented_consumer_types
    from organize_core.errors import ConfigError

    calls: list[str] = []

    def forbid(label: str):
        def boom(*args: object, **kwargs: object):
            calls.append(label)
            raise AssertionError(f"constructor called {label}()")

        return boom

    targets: list[tuple[object, str]] = [
        (subprocess, "run"),
        (subprocess, "Popen"),
        (subprocess, "check_output"),
        (_Path, "read_text"),
        (_Path, "read_bytes"),
        (_Path, "write_text"),
        (_Path, "exists"),
        (_Path, "is_file"),
        (_Path, "is_dir"),
        (_Path, "stat"),
        (_Path, "glob"),
        (_Path, "iterdir"),
        (_Path, "mkdir"),
        (builtins, "open"),
    ]

    implemented = get_implemented_consumer_types()
    assert implemented, "the consumer registry has no implemented types"

    #: Options a type REQUIRES before its constructor will run to completion.
    #: Kept minimal on purpose — the point is to reach the end of every
    #: __init__, not to configure the consumer.
    required_options: dict[str, dict[str, object]] = {
        "deep_research": {"command": ["agent", "{path}"]},
    }

    saved = [(obj, attr, getattr(obj, attr)) for obj, attr in targets]
    failures: list[str] = []
    try:
        for obj, attr in targets:
            setattr(obj, attr, forbid(f"{getattr(obj, '__name__', obj)}.{attr}"))
        for name, cls in sorted(get_consumer_types().items()):
            config = ConsumerConfig(
                name=name, type=name, options=dict(required_options.get(name, {}))
            )
            before = len(calls)
            try:
                cls(config)
            except ConfigError:
                # A registered-but-unimplemented type refuses loudly, and a
                # ConfigError is a PURE refusal by construction — what matters
                # is only whether it touched anything on the way out.
                if name in implemented:
                    failures.append(
                        f"{name}: implemented type raised ConfigError on its "
                        "minimal options (fix required_options above)"
                    )
            except AssertionError:
                pass  # the recorded call below is the real report
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
            touched = calls[before:]
            if touched:
                failures.append(f"{name}: constructor called {sorted(set(touched))}")
    finally:
        for obj, attr, original in saved:
            setattr(obj, attr, original)

    assert failures == [], (
        "consumer constructors must be PURE — no filesystem, no subprocess, no "
        "network (06 §1; 08 §B2 was the three-month outage, and constructors run "
        f"before --consumer filtering can isolate anything): {failures}"
    )
