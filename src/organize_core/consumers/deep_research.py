"""``deep_research`` consumer: new relationship note → research subprocess
(spec 06 §3.4).

Generic dispatcher, no content matching; routing purely by include_paths
(live: ``areas/relationships``). The exit-code-as-checkpoint contract is
the explicitly stated requirement in person-research-agent.md — preserve
exactly: exit 0 → SUCCESS (never re-dispatch until the note's hash
changes); nonzero/timeout → ERROR, retried next run.

Defects this module is written against (spec 08 §B — regression-tested in
``tests/test_consumer_research*.py``):

- **B1** every subprocess is decoded ``encoding="utf-8", errors="replace"``
  with an explicit timeout. An agent that prints a stray byte must not take
  the pipeline down for three months.
- **B2** the constructor is PURE: it validates option shapes and stores
  strings. No ``resolve()``, no ``exists()``, no env read, no subprocess.
  ``working_directory``/``notes_dir`` are expanded and checked on first real
  work, so a consumer whose agent checkout is missing cannot stop the other
  consumers from being constructed at all.
- **B3/B12** ``handle`` never touches the store — it has no reference to
  one. Checkpointing is the runner's, exclusively.
- **B14** no failure path raises out of ``handle``: a missing binary, a
  missing working directory, a timeout and a nonzero exit are all
  ``Status.ERROR`` results, so the run continues and the note is retried.
- **B15** two bugs, both fixed and both pinned by tests:
  1. the env guard tested ``"notes_dir" in env`` while setting ``NOTES_DIR``
     — i.e. never true, so the option ALWAYS clobbered a caller-supplied
     ``NOTES_DIR``. Precedence is now: config ``[consumers.X.env]`` table >
     inherited process environment > ``notes_dir`` option injection.
  2. ``str.format`` over the command template raised ``KeyError``/
     ``ValueError`` on any command containing literal braces (a jq filter, a
     JSON argument). Rendering is now a placeholder substitution that leaves
     every unrecognised brace run untouched.

Structural note: the process environment is read through
``paths.default_env()`` and config paths through ``paths.expand()`` — this
module never touches ``os.environ``/``expanduser`` itself (ARCHITECTURE
structural decision 4).
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from organize_core import paths as core_paths
from organize_core.config import ConsumerConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    Status,
    register,
)
from organize_core.errors import ConfigError

LOG = logging.getLogger("organize.consumers.deep_research")

#: Placeholders the command template understands (spec 06 §3.4).
PLACEHOLDERS: frozenset[str] = frozenset(
    {"path", "path_quoted", "notes_dir", "notes_dir_quoted"}
)

#: Config keys this consumer accepts beyond the framework set. ``cwd`` is the
#: name the shipped example config uses; ``working_directory`` is the name
#: doc 06 §3.4 and the live ``automations.toml`` use. Both are honored — a
#: rewrite that accepted only one of them would break one of the two files
#: that already exist.
KNOWN_OPTIONS: frozenset[str] = frozenset(
    {"command", "cwd", "working_directory", "notes_dir", "timeout_seconds"}
)

#: Doc 06 §3.4's default.
DEFAULT_TIMEOUT_SECONDS: float = 600.0

#: Cap on captured output stored in result metadata. The runner persists
#: metadata into the emissions table; an agent that prints a megabyte of
#: progress must not put a megabyte into the state DB (the 15 MB DB of 08
#: §B4 is the cautionary tale).
OUTPUT_PREVIEW_CHARS: int = 2000

#: A brace run is only a placeholder if it is exactly ``{name}`` with a
#: lowercase identifier inside. ``{"key": "value"}`` and ``${VAR}`` are left
#: alone by construction, which is the B15 fix.
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")

#: ``$VAR`` / ``${VAR}`` at the START of a command part — the only shape that
#: makes a part a path worth expanding.
_LEADING_VAR_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def render_template(part: str, context: dict[str, str]) -> str:
    """Substitute ``{placeholder}`` runs in one command part.

    Unlike ``str.format`` this never raises: an unknown placeholder and any
    other brace run are left verbatim (08 §B15). That is deliberate — a
    command is data from a config file, and a config file with a stray brace
    should run the agent, not crash the pipeline.
    """

    def sub(match: re.Match[str]) -> str:
        return context.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(sub, part)


def _placeholder_names(part: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(part))


def _preview(text: str | None) -> str:
    if not text:
        return ""
    stripped = text.strip()
    if len(stripped) <= OUTPUT_PREVIEW_CHARS:
        return stripped
    return stripped[:OUTPUT_PREVIEW_CHARS] + "…"


@register("deep_research")
class DeepResearchConsumer(Consumer):
    uses_llm = False  # dispatches a subprocess; the subprocess handles AI.
    # NOTE: still honors no-ai via should_process — the dispatched agent is
    # AI tooling under the vault law (spec 02), and because uses_llm is
    # False the runner's central guard will NOT cover this consumer
    # (ARCHITECTURE ambiguity ruling #12).

    def __init__(self, config: ConsumerConfig) -> None:
        super().__init__(config)
        options: dict[str, Any] = dict(config.options)

        unknown = sorted(set(options) - KNOWN_OPTIONS)
        if unknown:
            raise ConfigError(
                f"consumers.{config.name}: unknown option(s) "
                + ", ".join(repr(key) for key in unknown),
                hint="known deep_research options: " + ", ".join(sorted(KNOWN_OPTIONS)),
            )

        self.command_template: list[str] = self._validate_command(config.name, options)
        self.working_directory: str | None = self._validate_working_directory(
            config.name, options
        )
        self.notes_dir: str | None = self._validate_optional_string(
            config.name, options, "notes_dir"
        )
        self.timeout_seconds: float = self._validate_timeout(config.name, options)

        # Whether the caller already places the note path themselves; if not,
        # it is appended (parity with the live consumer).
        self.appends_path: bool = not any(
            _placeholder_names(part) & {"path", "path_quoted"}
            for part in self.command_template
        )

    # --- pure option validation (no I/O — 08 §B2) -------------------------

    @staticmethod
    def _validate_command(name: str, options: dict[str, Any]) -> list[str]:
        raw = options.get("command")
        if raw is None:
            raise ConfigError(
                f"consumers.{name}.command is required",
                hint='e.g. command = ["python", "people_research_cli.py", "{path_quoted}"]',
            )
        if isinstance(raw, str):
            parts = shlex.split(raw)
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                if not isinstance(item, str):
                    raise ConfigError(
                        f"consumers.{name}.command must be a list of strings; "
                        f"got {type(item).__name__} element {item!r}"
                    )
            parts = [item for item in raw if item.strip()]
        else:
            raise ConfigError(
                f"consumers.{name}.command must be a string or a list of strings, "
                f"got {type(raw).__name__}"
            )
        if not parts:
            raise ConfigError(f"consumers.{name}.command must not be empty")
        return parts

    @classmethod
    def _validate_working_directory(cls, name: str, options: dict[str, Any]) -> str | None:
        cwd = cls._validate_optional_string(name, options, "cwd")
        legacy = cls._validate_optional_string(name, options, "working_directory")
        if cwd is not None and legacy is not None and cwd != legacy:
            raise ConfigError(
                f"consumers.{name}: 'cwd' and 'working_directory' both set to "
                f"different values ({cwd!r} vs {legacy!r})",
                hint="they are aliases for the same directory — keep one",
            )
        return cwd if cwd is not None else legacy

    @staticmethod
    def _validate_optional_string(
        name: str, options: dict[str, Any], key: str
    ) -> str | None:
        value = options.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ConfigError(
                f"consumers.{name}.{key} must be a string, got {type(value).__name__}"
            )
        value = value.strip()
        return value or None

    @staticmethod
    def _validate_timeout(name: str, options: dict[str, Any]) -> float:
        raw = options.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ConfigError(
                f"consumers.{name}.timeout_seconds must be a number, "
                f"got {type(raw).__name__}"
            )
        value = float(raw)
        if value <= 0:
            raise ConfigError(
                f"consumers.{name}.timeout_seconds must be positive, got {value}"
            )
        return value

    # --- predicate --------------------------------------------------------

    def should_process(self, payload: NotePayload) -> bool:
        """Route by ``include_paths`` alone (the runner has already applied
        them) — no content matching, per 06 §3.4.

        The one content rule: ``no-ai: true`` is refused here. The runner's
        central guard keys off ``uses_llm``, which is False for this
        consumer because it builds no prompt; the agent it dispatches is
        nonetheless AI tooling, so the vault law (spec 02) applies
        (ARCHITECTURE ambiguity ruling #12).
        """
        if payload.no_ai:
            LOG.info(
                "[%s] refusing %s: no-ai frontmatter (spec 02 vault law)",
                self.config.name,
                payload.path,
            )
            return False
        return True

    # --- work -------------------------------------------------------------

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        base_env = core_paths.default_env()
        command = self.build_command(payload, env=base_env)
        env = self.build_env(payload, base_env)

        if ctx.dry_run:
            LOG.info("[%s] dry-run: would dispatch %s", self.config.name, payload.path)
            return ConsumerResult(
                status=Status.SKIP,
                message="dry-run: agent not dispatched",
                metadata={"command": command, "dry_run": True},
            )

        cwd = self.resolve_working_directory(base_env)
        if cwd is not None and not cwd.is_dir():
            LOG.error(
                "[%s] working directory does not exist: %s", self.config.name, cwd
            )
            return ConsumerResult(
                status=Status.ERROR,
                message=f"working directory does not exist: {cwd}",
                metadata={"command": command, "cwd": str(cwd)},
            )

        LOG.info("[%s] dispatching %s to the research agent", self.config.name, payload.path)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                capture_output=True,
                encoding="utf-8",  # B1: never a strict decode…
                errors="replace",  # …and never a UnicodeDecodeError.
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - started
            LOG.error(
                "[%s] research agent timed out for %s after %.0fs",
                self.config.name,
                payload.path,
                self.timeout_seconds,
            )
            return ConsumerResult(
                status=Status.ERROR,
                message=f"timeout after {self.timeout_seconds:g}s",
                metadata={
                    "command": command,
                    "timeout_seconds": self.timeout_seconds,
                    "duration_seconds": round(duration, 3),
                    "stdout": _preview(_as_text(exc.stdout)),
                    "stderr": _preview(_as_text(exc.stderr)),
                },
            )
        except OSError as exc:
            # Missing binary, unreadable cwd, permission denied. B14: this is
            # an error RESULT, never an exception escaping into the run.
            duration = time.monotonic() - started
            LOG.error(
                "[%s] could not launch research agent for %s: %s",
                self.config.name,
                payload.path,
                exc,
            )
            return ConsumerResult(
                status=Status.ERROR,
                message=f"could not launch {command[0]!r}: {exc}",
                metadata={
                    "command": command,
                    "duration_seconds": round(duration, 3),
                    "error": type(exc).__name__,
                },
            )

        duration = round(time.monotonic() - started, 3)
        stdout = _preview(completed.stdout)
        stderr = _preview(completed.stderr)

        if completed.returncode != 0:
            LOG.error(
                "[%s] research agent failed for %s (exit %d): %s",
                self.config.name,
                payload.path,
                completed.returncode,
                stderr[:500],
            )
            return ConsumerResult(
                status=Status.ERROR,
                message=f"command exited with {completed.returncode}",
                metadata={
                    "command": command,
                    "returncode": completed.returncode,
                    "duration_seconds": duration,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )

        LOG.info(
            "[%s] research agent completed %s in %.1fs",
            self.config.name,
            payload.path,
            duration,
        )
        return ConsumerResult(
            status=Status.SUCCESS,
            message="deep research completed",
            metadata={
                "command": command,
                "returncode": 0,
                "duration_seconds": duration,
                "stdout": stdout,
                "stderr": stderr,
            },
        )

    # --- steps (unit-test targets) ---------------------------------------

    def build_command(
        self, payload: NotePayload, *, env: dict[str, str] | None = None
    ) -> list[str]:
        """Render the command template for one note.

        Placeholders: ``{path} {path_quoted} {notes_dir} {notes_dir_quoted}``.
        When no ``{path…}`` placeholder appears anywhere in the template the
        note path is appended, which is how the live config's simpler forms
        work. Literal braces survive untouched (08 §B15).

        A template part is additionally ``~``/``$VAR`` expanded when — and
        only when — it begins with ``~`` or ``$`` and contains no
        placeholder. That keeps ``~/Projects/Agent/main.py`` working without
        letting an expansion mangle a flag like ``--person`` (``expand``
        resolves relative to the process CWD) or a part whose braces are
        about to be substituted.
        """
        if env is None:
            env = core_paths.default_env()
        notes_dir = self.resolve_notes_dir(env)
        note_path = str(payload.path)
        context = {
            "path": note_path,
            "path_quoted": shlex.quote(note_path),
            "notes_dir": str(notes_dir) if notes_dir else "",
            "notes_dir_quoted": shlex.quote(str(notes_dir)) if notes_dir else "",
        }
        rendered = [
            render_template(self._expand_part(part, env), context)
            for part in self.command_template
        ]
        if self.appends_path:
            rendered.append(note_path)
        return rendered

    def build_env(self, payload: NotePayload, base_env: dict[str, str]) -> dict[str, str]:
        """``base_env`` + the config ``env`` table + ``NOTES_DIR`` injection.

        Precedence, highest first (the 08 §B15 fix): the ``[consumers.X.env]``
        table, then whatever the caller already exported, then the
        ``notes_dir`` option. The old guard read ``"notes_dir" in env`` while
        writing ``NOTES_DIR``, so the option silently won every time.

        Env-table values are expanded only when they begin with ``~`` — a
        leading tilde is unambiguously a home-relative path (the shipped
        example passes ``NOTES_DIR = "~/Obsidian/Main"``), whereas a value
        containing ``$`` may well be a secret and is passed through verbatim.
        """
        env = dict(base_env)
        for key, value in self.config.env.items():
            env[str(key)] = self._expand_env_value(str(value), base_env)
        if "NOTES_DIR" not in env:
            notes_dir = self.resolve_notes_dir(base_env)
            if notes_dir is not None:
                env["NOTES_DIR"] = str(notes_dir)
        return env

    # --- lazy path resolution (first real work, not __init__ — 08 §B2) ----

    def resolve_working_directory(self, env: dict[str, str] | None = None) -> Path | None:
        return self._expand_option(self.working_directory, env)

    def resolve_notes_dir(self, env: dict[str, str] | None = None) -> Path | None:
        return self._expand_option(self.notes_dir, env)

    @staticmethod
    def _expand_option(value: str | None, env: dict[str, str] | None) -> Path | None:
        if value is None:
            return None
        return core_paths.expand(value, env if env is not None else core_paths.default_env())

    @staticmethod
    def _expand_part(part: str, env: dict[str, str]) -> str:
        if not part or _PLACEHOLDER_RE.search(part):
            return part
        if part.startswith("~"):
            return str(core_paths.expand(part, env))
        match = _LEADING_VAR_RE.match(part)
        if match and match.group(1) in env:
            # Only when the variable is actually SET: `expand` resolves, and
            # resolving an unsubstituted `${SHELL_VAR}` would silently turn
            # it into a path relative to the process's working directory.
            return str(core_paths.expand(part, env))
        return part

    @staticmethod
    def _expand_env_value(value: str, env: dict[str, str]) -> str:
        if value.startswith("~"):
            return str(core_paths.expand(value, env))
        return value


def _as_text(value: Any) -> str:
    """``TimeoutExpired`` carries whatever was captured before the kill; with
    ``encoding=`` set that is text, but the attribute is typed loosely and is
    ``None`` when nothing was read."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
