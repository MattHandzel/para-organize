"""Contract tests for paths.py (spec 10 §3) — integrator-owned.

Locks the precedence order (explicit > ORGANIZE_CORE_* > XDG > home), the
derived-location layout, env isolation (a fake env dict is fully
authoritative), and symlink resolution (spec 02: ~/notes == ~/Obsidian/Main).
"""

from __future__ import annotations

from pathlib import Path

from organize_core.paths import (
    ENV_CONFIG_DIR,
    ENV_RUNTIME_DIR,
    ENV_STATE_DIR,
    CorePaths,
    expand,
)


def test_explicit_args_beat_env(tmp_path: Path) -> None:
    env = {
        ENV_CONFIG_DIR: str(tmp_path / "env-config"),
        ENV_STATE_DIR: str(tmp_path / "env-state"),
        ENV_RUNTIME_DIR: str(tmp_path / "env-runtime"),
        "HOME": str(tmp_path / "home"),
    }
    p = CorePaths.resolve(
        config_dir=tmp_path / "c",
        state_dir=tmp_path / "s",
        runtime_dir=tmp_path / "r",
        env=env,
    )
    assert p.config_dir == (tmp_path / "c").resolve()
    assert p.state_dir == (tmp_path / "s").resolve()
    assert p.runtime_dir == (tmp_path / "r").resolve()


def test_core_env_beats_xdg(tmp_path: Path) -> None:
    env = {
        ENV_CONFIG_DIR: str(tmp_path / "core-config"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "HOME": str(tmp_path / "home"),
    }
    p = CorePaths.resolve(env=env)
    assert p.config_dir == (tmp_path / "core-config").resolve()
    # XDG channels get the organize-core subdir; runtime dir is used as-is
    # (socket lives directly in $XDG_RUNTIME_DIR — spec 10 §1).
    assert p.state_dir == (tmp_path / "xdg-data" / "organize-core").resolve()
    assert p.runtime_dir == (tmp_path / "run").resolve()


def test_home_fallback_uses_env_home_not_real_home(tmp_path: Path) -> None:
    env = {"HOME": str(tmp_path / "home")}
    p = CorePaths.resolve(env=env)
    assert p.config_dir == (tmp_path / "home" / ".config" / "organize-core").resolve()
    assert p.state_dir == (tmp_path / "home" / ".local" / "share" / "organize-core").resolve()
    # nothing under the real home
    assert not str(p.state_dir).startswith(str(Path.home())) or str(Path.home()).startswith(
        str(tmp_path)
    )


def test_derived_locations(tmp_path: Path) -> None:
    p = CorePaths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
    )
    assert p.config_file == tmp_path / "config" / "config.toml"
    assert p.index_path == tmp_path / "state" / "index.json"
    assert p.learning_path == tmp_path / "state" / "learning.json"
    assert p.operations_log == tmp_path / "state" / "operations.log"
    assert p.actions_dir == tmp_path / "state" / "actions"
    assert p.automations_db == tmp_path / "state" / "automations.db"
    assert p.backups_dir == tmp_path / "state" / "backups"
    assert p.socket_path == tmp_path / "runtime" / "organize-core.sock"
    assert p.lock_path == tmp_path / "runtime" / "organize-core.lock"


def test_ensure_state_dirs(tmp_path: Path) -> None:
    p = CorePaths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
    )
    p.ensure_state_dirs()
    assert p.state_dir.is_dir()
    assert p.actions_dir.is_dir()
    assert p.backups_dir.is_dir()
    # idempotent
    p.ensure_state_dirs()
    # config/runtime dirs are NOT created — state only
    assert not p.config_dir.exists()
    assert not p.runtime_dir.exists()


def test_expand_vars_tilde_and_symlinks(tmp_path: Path) -> None:
    real = tmp_path / "Obsidian" / "Main"
    real.mkdir(parents=True)
    link = tmp_path / "notes"
    link.symlink_to(real)
    env = {"HOME": str(tmp_path), "MYVAR": str(link)}
    # symlink resolution: ~/notes == ~/Obsidian/Main (spec 02)
    assert expand("~/notes", env) == expand("~/Obsidian/Main", env)
    # $VAR expansion comes from the provided env only
    assert expand("$MYVAR/sub", env) == real.resolve() / "sub"
    assert expand("${MYVAR}/sub", env) == real.resolve() / "sub"
    # unknown vars are left intact, not silently pulled from os.environ
    assert "$DEFINITELY_NOT_SET" in str(expand("$DEFINITELY_NOT_SET/x", env))


def test_expand_accepts_path_objects(tmp_path: Path) -> None:
    assert expand(tmp_path / "x", {}) == (tmp_path / "x").resolve()


def test_unset_xdg_runtime_dir_expands_to_the_runtime_fallback(tmp_path: Path) -> None:
    # macOS never sets XDG_RUNTIME_DIR. The shipped config's
    # `socket_path = "$XDG_RUNTIME_DIR/organize-core.sock"` used to stay a
    # literal RELATIVE path there, and `organize serve` could not bind it.
    env = {"HOME": str(tmp_path)}
    sock = expand("$XDG_RUNTIME_DIR/organize-core.sock", env)
    assert sock.is_absolute()
    assert "$" not in str(sock)
    assert sock == CorePaths.resolve(env=env).socket_path
    assert len(str(sock).encode()) < 104  # AF_UNIX sun_path on macOS
    # a set XDG_RUNTIME_DIR still wins
    env["XDG_RUNTIME_DIR"] = str(tmp_path / "run")
    assert expand("$XDG_RUNTIME_DIR/x.sock", env) == (tmp_path / "run").resolve() / "x.sock"
