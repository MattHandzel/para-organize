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

#: ``llm.py`` is the one module that may spawn a process — the ``claude-cli``
#: backend (06 §3.2), which is a network client, not filesystem work.
SUBPROCESS_ALLOWED: frozenset[str] = frozenset({"llm"})


@pytest.mark.parametrize("module", MODULES)
def test_no_module_shells_out_for_filesystem_work(module: str) -> None:
    """05 §1.6 / 08 §A28: the original had six ``io.popen("find …")`` sites,
    three with unquoted paths. All filesystem work happens in-process."""
    if module in SUBPROCESS_ALLOWED:
        return
    used = _attribute_names(_tree(module)) & {"subprocess", "system", "popen"}
    assert used == set(), f"{module}.py must not shell out (found {sorted(used)})"


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
    """
    this_file = Path(__file__).resolve()
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        if path.resolve() == this_file:
            continue  # this file names the forbidden prefixes to search for
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for text in _non_docstring_strings(tree):
            if "/home/" in text or "/Users/" in text:
                offenders.append(f"{path.name}: {text[:80]!r}")
    assert offenders == [], (
        "test files must locate the repo from __file__/sys.executable, never an "
        "absolute machine path (08 §A37):\n" + "\n".join(offenders)
    )
