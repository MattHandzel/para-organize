"""State/config/runtime directory resolution (spec 10 §3).

Real defaults:
    config  ~/.config/organize-core/config.toml
    state   ~/.local/share/organize-core/   (index, learning.json,
            operations.log, actions/, automations.db, backups/)
    socket  $XDG_RUNTIME_DIR/organize-core.sock

HARD RULE: every location is ALWAYS overridable — explicit argument beats
environment variable beats XDG default. No function in this codebase may
default to writing a real home path without going through :class:`CorePaths`,
and tests always construct :func:`CorePaths.resolve` with explicit tmp dirs
(or a fake ``env`` mapping), so the suite can never touch real state.

Environment overrides (highest to lowest within the env channel):
    ORGANIZE_CORE_CONFIG_DIR, ORGANIZE_CORE_STATE_DIR, ORGANIZE_CORE_RUNTIME_DIR
    XDG_CONFIG_HOME, XDG_DATA_HOME, XDG_RUNTIME_DIR

SHARED FILE — only the architect/integrator edits this module.
"""

from __future__ import annotations

import os
import string
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ENV_CONFIG_DIR = "ORGANIZE_CORE_CONFIG_DIR"
ENV_STATE_DIR = "ORGANIZE_CORE_STATE_DIR"
ENV_RUNTIME_DIR = "ORGANIZE_CORE_RUNTIME_DIR"


@dataclass(frozen=True)
class CorePaths:
    """Every filesystem location the core reads or writes, resolved once.

    All other modules take a ``CorePaths`` (or one of its members) as an
    argument — they never consult ``os.environ`` or ``Path.home()``
    themselves. This is what makes fixture isolation structural.
    """

    config_dir: Path
    state_dir: Path
    runtime_dir: Path

    # --- derived locations (spec 10 §3 state layout) ---------------------

    @property
    def config_file(self) -> Path:
        """``<config_dir>/config.toml`` — the one behavioral config file."""
        return self.config_dir / "config.toml"

    @property
    def index_path(self) -> Path:
        """``<state_dir>/index.json`` — internal format, see index.py."""
        return self.state_dir / "index.json"

    @property
    def learning_path(self) -> Path:
        """``<state_dir>/learning.json`` — format per spec 04 §3."""
        return self.state_dir / "learning.json"

    @property
    def operations_log(self) -> Path:
        """``<state_dir>/operations.log`` — per spec 05 §1.5."""
        return self.state_dir / "operations.log"

    @property
    def actions_dir(self) -> Path:
        """``<state_dir>/actions/`` — YYYY-MM.jsonl files per spec 12 §2."""
        return self.state_dir / "actions"

    @property
    def automations_db(self) -> Path:
        """``<state_dir>/automations.db`` — spec 06 §1 (migrated from
        ``~/.local/state/para-organize/`` at cutover, spec 09 §5.4)."""
        return self.state_dir / "automations.db"

    @property
    def backups_dir(self) -> Path:
        """``<state_dir>/backups/`` — taskwarrior snapshots etc. (06 §3.1)."""
        return self.state_dir / "backups"

    @property
    def socket_path(self) -> Path:
        """``<runtime_dir>/organize-core.sock`` (spec 10 §1)."""
        return self.runtime_dir / "organize-core.sock"

    @property
    def lock_path(self) -> Path:
        """Single-instance lock file for ``organize serve`` (spec 10 §1)."""
        return self.runtime_dir / "organize-core.lock"

    # --- construction ----------------------------------------------------

    @classmethod
    def resolve(
        cls,
        *,
        config_dir: Path | str | None = None,
        state_dir: Path | str | None = None,
        runtime_dir: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> CorePaths:
        """Resolve all locations. Precedence per channel: explicit argument >
        ``ORGANIZE_CORE_*`` env > XDG env > home default. ``env`` defaults to
        ``os.environ`` — tests pass a plain dict and explicit dirs so nothing
        real is ever consulted. ``~`` and ``$VARS`` are expanded and symlinks
        resolved (spec 02: ``~/notes`` and ``~/Obsidian/Main`` must compare
        equal after resolution).
        """
        if env is None:
            env = default_env()

        def pick(
            explicit: Path | str | None,
            core_var: str,
            xdg_var: str,
            xdg_subdir: str | None,
            fallback: str,
        ) -> Path:
            if explicit is not None:
                return expand(explicit, env)
            core_val = env.get(core_var)
            if core_val:
                return expand(core_val, env)
            xdg_val = env.get(xdg_var)
            if xdg_val:
                base = expand(xdg_val, env)
                return base / xdg_subdir if xdg_subdir else base
            return expand(fallback, env)

        return cls(
            config_dir=pick(
                config_dir, ENV_CONFIG_DIR, "XDG_CONFIG_HOME", "organize-core", "~/.config/organize-core"
            ),
            state_dir=pick(
                state_dir, ENV_STATE_DIR, "XDG_DATA_HOME", "organize-core", "~/.local/share/organize-core"
            ),
            runtime_dir=pick(
                runtime_dir, ENV_RUNTIME_DIR, "XDG_RUNTIME_DIR", None, runtime_fallback()
            ),
        )

    def ensure_state_dirs(self) -> None:
        """Create state_dir and its subdirectories (actions/, backups/) if
        missing. The ONLY mkdir path for state; never called at import time.
        """
        for d in (self.state_dir, self.actions_dir, self.backups_dir):
            d.mkdir(parents=True, exist_ok=True)


def runtime_fallback() -> str:
    """The runtime dir when ``XDG_RUNTIME_DIR`` is unset: a per-uid tmp dir —
    sockets don't belong under $HOME (spec 10 §1 names XDG_RUNTIME_DIR).

    On macOS the per-user ``$TMPDIR`` is ``/var/folders/xx/…/T/`` (~50 bytes)
    and AF_UNIX caps ``sun_path`` at 104, so the fallback there is ``/tmp`` —
    the same base the Neovim client's default socket uses.
    """
    base = "/tmp" if sys.platform == "darwin" else tempfile.gettempdir()
    return str(Path(base) / f"organize-core-{os.getuid()}")


def expand(path: Path | str, env: dict[str, str] | None = None) -> Path:
    """Expand ``~`` and ``$VARS`` against ``env`` (default ``os.environ``)
    and resolve symlinks. Used for every path read from config (spec 06 §2:
    paths are expanded and resolved; DB paths canonicalize identically or
    history orphans)."""
    if env is None:
        env = default_env()
    s = str(path)
    # $XDG_RUNTIME_DIR is the one variable the shipped config names that a
    # whole platform never sets (macOS). Left intact it became a RELATIVE
    # socket path under the cwd, so `organize serve` failed with "AF_UNIX path
    # too long". Give it the same fallback CorePaths uses for runtime_dir.
    if not env.get("XDG_RUNTIME_DIR"):
        env = {**env, "XDG_RUNTIME_DIR": runtime_fallback()}
    # $VAR / ${VAR} against the provided env only; unknown vars left intact
    # (loud downstream, never silently substituted from the real process env).
    s = string.Template(s).safe_substitute(env)
    if s == "~" or s.startswith("~/"):
        home = env.get("HOME")
        if home:
            s = home + s[1:]
        else:
            s = os.path.expanduser(s)
    elif s.startswith("~"):
        # ~user form — no per-env override channel; defer to the OS.
        s = os.path.expanduser(s)
    # strict=False: resolves symlinks for the existing prefix, keeps the
    # non-existent tail lexically — ~/notes == ~/Obsidian/Main (spec 02).
    return Path(s).resolve()


def default_env() -> dict[str, str]:
    """Return ``dict(os.environ)`` — isolated here so it is the single
    process-environment touchpoint (grep target in reviews)."""
    return dict(os.environ)
