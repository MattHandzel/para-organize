"""Cross-module integration tests (seat: integrator).

Every other suite tests one module, usually against a fake for its
neighbours: the fileops suite drives a duck-typed ``FakeIndex``, the routes
suite a ``StubIndex``, the server suite its own in-process client. That is
correct for unit tests and it leaves exactly one class of defect
undetectable — the one that lives in the SEAM, where two real modules
disagree about a contract each of them implements faithfully in isolation.

Two things are pinned here that no single seat could write:

1. **Real-index fileops round-trip** — the actual :class:`VaultIndex`, the
   actual frontmatter parser and the actual ActionRecorder, driven through
   real operations against a fixture vault, including the persisted snapshot
   (a second ``VaultIndex`` opened on the same file must agree with the
   first).
2. **CLI/server behavioral equivalence** — the same move performed through
   the shipped ``organize`` binary and through the JSON-RPC server must leave
   two identical vaults byte-identical. Spec 10 §1 makes these two frontends
   over ONE core; a behavioural difference between them means a caller's
   results depend on which door they came through, which is precisely the
   drift the core/client split exists to prevent.

Assertions are exact values (09 §3). Nothing here touches real state or
``~/Obsidian/Main``: every case builds a fresh fixture vault under tmp_path
and tmp ``CorePaths``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES, build_fixture_vault
from organize_core.actions import ActionRecorder
from organize_core.config import Config, VaultConfig
from organize_core.errors import FrontmatterError, NoAiRefusal
from organize_core.fileops import (
    OperationContext,
    OperationLog,
    archive_capture,
    merge_into_note,
    move_to_destination,
    new_folder,
    update_frontmatter,
)
from organize_core.frontmatter import load_file
from organize_core.index import NoteRecord, VaultIndex
from organize_core.paths import CorePaths
from organize_core.server import OrganizeServer

REPO = Path(__file__).resolve().parent.parent
#: The CLI of THIS checkout, whatever prefix it lives under (08 §A37). A
#: `.venv/bin/organize` console script can point at an INSTALLED copy of the
#: package, which is not what these parity tests are about.
ORGANIZE: tuple[str, ...] = (sys.executable, "-m", "organize_core.cli")

CAPTURE = QUIRK_FILES["current_schema"]  # tags: impro, creativity
MERGE_TARGET = QUIRK_FILES["merge_target"]  # projects/blog/ideas.md
NO_AI = QUIRK_FILES["no_ai"]
BROKEN = QUIRK_FILES["broken_yaml"]

FIXED_NOW = 1786000000.0  # 2026-08-06T07:06:40Z — same pinned clock as fileops


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def make_config(vault: Path, **overrides: Any) -> Config:
    """The config both frontends are given. Kept in ONE place so the
    equivalence test cannot accidentally compare two different setups."""
    return Config(
        vault=VaultConfig(
            root=vault,
            scan_dirs=["capture/raw_capture", "projects", "areas", "resources"],
            para_folders={
                "projects": "projects",
                "areas": "areas",
                "resources": "resources",
                "archives": "archive",  # SINGULAR on disk (spec 02, 08 §C2)
            },
        ),
        **overrides,
    )


CONFIG_TOML = """
[vault]
root = "{vault}"
scan_dirs = ["capture/raw_capture", "projects", "areas", "resources"]

[vault.para_folders]
projects = "projects"
areas = "areas"
resources = "resources"
archives = "archive"

[logging]
level = "WARNING"
"""


def make_paths(root: Path) -> CorePaths:
    return CorePaths(
        config_dir=root / "config",
        state_dir=root / "state",
        runtime_dir=root / "runtime",
    )


def make_ctx(
    config: Config,
    index: VaultIndex,
    paths: CorePaths,
    *,
    dry_run: bool = False,
    actor: str = "matt",
    now: float = FIXED_NOW,
) -> OperationContext:
    return OperationContext(
        config=config,
        index=index,
        oplog=OperationLog(paths.operations_log),
        recorder=ActionRecorder(paths.actions_dir),
        backup_dir=Path(config.vault.root) / config.file_ops.backup_dir,
        dry_run=dry_run,
        actor=actor,
        session_id="ses_integration",
        clock=lambda: now,
    )


def record_of(index: VaultIndex, vault: Path, rel: str) -> NoteRecord:
    """The REAL indexed record — not a hand-built stand-in. This is half the
    point of the suite: fileops must work with what the index actually
    produces."""
    record = index.get(vault / rel)
    assert record is not None, f"{rel} is not in the index"
    return record


def tree(root: Path, *, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """Vault-relative path → sha256 of contents, for every file."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(rel.startswith(prefix) for prefix in skip):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.fixture()
def env(tmp_path: Path) -> tuple[Path, VaultIndex, Config, CorePaths]:
    """A fixture vault with a REAL, fully scanned VaultIndex."""
    vault = build_fixture_vault(tmp_path / "vault")
    paths = make_paths(tmp_path)
    paths.ensure_state_dirs()
    config = make_config(vault)
    index = VaultIndex(config, paths.index_path)
    index.full_reindex()
    return vault, index, config, paths


# ===========================================================================
# 1. Real-index fileops round-trip
# ===========================================================================


def test_move_updates_the_real_index_in_place_without_a_rescan(
    env: tuple[Path, VaultIndex, Config, CorePaths],
) -> None:
    """05 §2 + 03 §7: after a move the warm index must already describe the
    new world. The fileops suite could only assert that ``update_file`` was
    CALLED; here we assert the index actually changed its mind."""
    vault, index, config, paths = env
    before_total = index.stats()["total"]
    source = vault / CAPTURE
    destination = vault / "areas/health"

    ctx = make_ctx(config, index, paths)
    result = move_to_destination(ctx, record_of(index, vault, CAPTURE), destination)
    assert result.ok, result.error

    moved = destination / source.name
    assert moved.is_file()
    assert not source.exists()

    # The index — with NO rescan — knows the note by its new path...
    assert index.get(source) is None
    record = index.get(moved)
    assert record is not None
    assert record.path == str(moved)
    assert record.para_type == "area"  # ParaType is SINGULAR (index.py)
    assert record.folder == "health"
    # ...and the frontmatter the move wrote is what the index parsed back.
    assert "area/health" in record.tags
    assert record.processing_status == "organized"

    archived = vault / "archive/capture/raw_capture" / source.name
    assert archived.is_file(), "the original is archived under its ORIGINAL name (08 §A15)"
    # Total is unchanged: one note moved, one archived copy added, one
    # capture removed from its old path.
    assert index.stats()["total"] == before_total + 1


def test_the_index_snapshot_round_trips_a_move_to_a_fresh_process(
    env: tuple[Path, VaultIndex, Config, CorePaths],
) -> None:
    """The in-memory index and the persisted snapshot must agree. A second
    VaultIndex over the same file stands in for the next `organize` run."""
    vault, index, config, paths = env
    ctx = make_ctx(config, index, paths)
    result = move_to_destination(
        ctx, record_of(index, vault, CAPTURE), vault / "resources/performing"
    )
    assert result.ok, result.error
    index.flush()

    reopened = VaultIndex(config, paths.index_path)
    reopened.load()

    moved = vault / "resources/performing" / Path(CAPTURE).name
    reloaded = reopened.get(moved)
    assert reloaded is not None
    assert reloaded.path == str(moved)
    assert reloaded.para_type == "resource"
    assert "resource/performing" in reloaded.tags
    assert reopened.get(vault / CAPTURE) is None
    assert reopened.stats()["total"] == index.stats()["total"]


def test_new_folder_is_immediately_a_suggestion_candidate(
    env: tuple[Path, VaultIndex, Config, CorePaths],
) -> None:
    """Spec 05 §6 says new_folder must "refresh folder caches/index dirs".

    The integrator's ruling on that seam: no hook is needed, because
    ``para_subfolders`` enumerates directories from DISK rather than deriving
    them from note records — so an EMPTY new folder is a candidate the moment
    mkdir returns. This test is the written confirmation; if anyone ever
    switches para_subfolders to a record-derived implementation it fails, and
    fileops will genuinely need the hook.
    """
    vault, index, config, paths = env
    before = [p.name for p in index.para_subfolders("areas")]
    before_total = index.stats()["total"]
    assert "woodworking" not in before

    ctx = make_ctx(config, index, paths)
    result = new_folder(ctx, "area", "woodworking")  # singular, as the UI speaks it
    assert result.ok, result.error
    assert (vault / "areas/woodworking").is_dir()

    after = [p.name for p in index.para_subfolders("areas")]
    assert after == sorted([*before, "woodworking"])
    # ...and it is empty, so it added no notes to the index.
    assert index.stats()["total"] == before_total


def test_merge_dedupes_tags_on_normalized_form_through_the_real_parser(
    env: tuple[Path, VaultIndex, Config, CorePaths],
) -> None:
    """Spec 05 §4: merge dedupes "on normalized form" — strictly stronger
    than the case-insensitive key 05 §2.6 uses for a move. Exercised through
    the real frontmatter round-trip so the stored file is the evidence."""
    vault, index, config, paths = env
    # The TARGET spells the tag one way...
    target = vault / MERGE_TARGET
    target.write_text(
        "---\n"
        "title: Blog ideas\n"
        "tags:\n"
        "  - Deep_Work\n"
        "  - blog-idea\n"
        "---\n\n## Inbox\n\n- an existing idea\n",
        encoding="utf-8",
    )
    # ...the CAPTURE spells the same tag another way, plus a genuinely new one.
    source = vault / CAPTURE
    source.write_text(
        "---\n"
        "id: merge-capture\n"
        "tags:\n"
        "  - deep-work\n"
        "  - impro\n"
        "processing_status: raw\n"
        "---\n\n## Content\n\nA thought to weave in.\n",
        encoding="utf-8",
    )
    index.update_file(target)
    index.update_file(source)

    ctx = make_ctx(config, index, paths)
    result = merge_into_note(ctx, record_of(index, vault, CAPTURE), target)
    assert result.ok, result.error

    tags = load_file(target).frontmatter.fields["tags"]
    # ONE deep-work tag, spelled the way the TARGET spelled it (target-first).
    assert tags.count("Deep_Work") == 1
    assert "deep-work" not in tags
    assert "impro" in tags, "genuinely new tags still arrive"


def test_the_no_ai_law_holds_end_to_end_with_a_real_index(
    env: tuple[Path, VaultIndex, Config, CorePaths],
) -> None:
    """Spec 02 vault law. The index happily indexes a `no-ai` note (it only
    reads); an automated actor must not be able to write to it, and the file
    must be byte-identical afterwards."""
    vault, index, config, paths = env
    path = vault / NO_AI
    before = path.read_bytes()
    record = record_of(index, vault, NO_AI)
    # `no-ai` is not a promoted NoteRecord attribute — it survives in `extra`,
    # and the ENFORCEMENT deliberately re-reads the file rather than trusting
    # the index, so a stale record can never unlock a protected note.
    assert record.extra["no-ai"] is True

    ctx = make_ctx(config, index, paths, actor="consumer:taskwarrior")
    with pytest.raises(NoAiRefusal):
        update_frontmatter(ctx, path, {"importance": "high"})
    assert path.read_bytes() == before

    # Matt himself is not blocked (05 §5 / 02: the law binds AI, not the human).
    human = make_ctx(config, index, paths, actor="matt")
    assert update_frontmatter(human, path, {"importance": "high"}).ok
    assert load_file(path).frontmatter.fields["importance"] == "high"
    assert load_file(path).frontmatter.fields["no-ai"] is True


def test_broken_yaml_is_indexed_but_never_silently_rewritten(
    env: tuple[Path, VaultIndex, Config, CorePaths],
) -> None:
    """The frontmatter seat asked the integrator to confirm the tolerance
    boundary (03 §7 + 09 §1.5): the INDEX degrades to a flagged record, while
    a MUTATING op refuses loudly rather than writing a guessed file."""
    vault, index, config, paths = env
    path = vault / BROKEN
    before = path.read_bytes()

    record = record_of(index, vault, BROKEN)
    assert record.parse_error is True
    assert record.tags == []

    ctx = make_ctx(config, index, paths)
    with pytest.raises(FrontmatterError):
        update_frontmatter(ctx, path, {"importance": "high"})
    assert path.read_bytes() == before, "a refused op leaves the file untouched"


def test_every_mutating_op_names_the_unparseable_file(
    env: tuple[Path, VaultIndex, Config, CorePaths],
) -> None:
    """"line 30, column 17" with no path is unactionable in a batch run over
    1,858 captures against the 23 real files with unparseable YAML. `set-meta`
    named the file; `move` and `archive` did not."""
    vault, index, config, paths = env
    path = vault / BROKEN
    before = path.read_bytes()
    record = record_of(index, vault, BROKEN)
    ctx = make_ctx(config, index, paths)

    operations = {
        "set-meta": lambda: update_frontmatter(ctx, path, {"importance": "high"}),
        "move": lambda: move_to_destination(ctx, record, vault / "projects" / "blog"),
        "archive": lambda: archive_capture(ctx, record),
    }
    for name, run in operations.items():
        with pytest.raises(FrontmatterError) as caught:
            run()
        assert str(path) in str(caught.value), f"{name} did not name the file"
        assert "unparseable YAML frontmatter" in str(caught.value)
    assert path.read_bytes() == before


def test_set_meta_replace_is_one_write_and_one_action(
    env: tuple[Path, VaultIndex, Config, CorePaths],
) -> None:
    """The `replace_keys` seam (spec 07 `append = false`). Replacing a list
    field must be ONE operation: two ActionRecords for one logical edit would
    make the doc-12 corpus misreport what Matt did."""
    vault, index, config, paths = env
    path = vault / CAPTURE
    ctx = make_ctx(config, index, paths)

    result = update_frontmatter(
        ctx, path, {"tags": ["only-this"]}, replace_keys=frozenset({"tags"})
    )
    assert result.ok, result.error
    assert load_file(path).frontmatter.fields["tags"] == ["only-this"]

    records = read_actions(paths)
    assert len(records) == 1
    assert records[0]["operation"] == "meta_edit"
    log = paths.operations_log.read_text(encoding="utf-8").splitlines()
    assert len([line for line in log if line.strip()]) == 1

    # ...and the default (append) path still merges, per 05 §2.6.
    merged = update_frontmatter(ctx, path, {"tags": ["and-this"]})
    assert merged.ok
    assert load_file(path).frontmatter.fields["tags"] == ["only-this", "and-this"]


def test_a_dry_run_move_changes_the_vault_and_the_index_not_at_all(
    env: tuple[Path, VaultIndex, Config, CorePaths],
) -> None:
    """09 §5.6 with a REAL index behind it: the fileops suite proved the vault
    is untouched against a fake; this proves the index did not move either,
    and that the rehearsal stays out of the corpus."""
    vault, index, config, paths = env
    before = tree(vault)
    before_stats = index.stats()

    ctx = make_ctx(config, index, paths, dry_run=True)
    result = move_to_destination(
        ctx, record_of(index, vault, CAPTURE), vault / "areas/health"
    )
    assert result.ok
    assert result.dry_run is True

    assert tree(vault) == before, "a dry run wrote to the vault"
    assert index.stats() == before_stats
    assert index.get(vault / CAPTURE) is not None

    # It IS logged (that is what makes --dry-run useful)...
    assert "[DRY-RUN]" in paths.operations_log.read_text(encoding="utf-8")
    # One record for the rehearsed move. (A dry run stops before the archive
    # step, so unlike a real move there is no second `archive` record.)
    rehearsed = read_actions(paths)
    assert [r["operation"] for r in rehearsed] == ["move"]
    assert rehearsed[0]["context"]["dry_run"] is True
    # ...but it is NOT a precedent.
    recorder = ActionRecorder(paths.actions_dir)
    assert list(recorder.query()) == []
    assert recorder.stats()["total"] == 0
    assert recorder.stats(include_dry_run=True)["total"] == 1


def read_actions(paths: CorePaths) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for month in sorted(Path(paths.actions_dir).glob("*.jsonl")):
        for line in month.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# ===========================================================================
# 2. CLI / server behavioral equivalence for `move`
# ===========================================================================


class RpcClient:
    """Minimal newline-delimited JSON-RPC client (stdlib only)."""

    def __init__(self, socket_path: Path, *, timeout: float = 20.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(socket_path))
        self._buf = b""
        self._next_id = 0
        self.handshake = self._read()

    def _read(self) -> dict[str, Any]:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("server closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8", errors="replace"))

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        while True:
            message = self._read()
            if message.get("id") == self._next_id:
                return message

    def result(self, method: str, **params: Any) -> Any:
        response = self.call(method, **params)
        assert "error" not in response, response["error"]
        return response["result"]

    def close(self) -> None:
        self.sock.close()


class Frontend:
    """One fully isolated organize-core deployment: its own vault, its own
    state, its own config file. Two of these are built identically so the
    only variable in the comparison is WHICH FRONTEND performed the move."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.vault = build_fixture_vault(root / "vault")
        self.paths = make_paths(root)
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.paths.ensure_state_dirs()
        self.paths.config_file.write_text(
            CONFIG_TOML.format(vault=self.vault), encoding="utf-8"
        )
        self.config = make_config(self.vault)

    def write_config(self, *, extra: str = "") -> None:
        """Rewrite the config file AND the in-memory Config the server uses,
        so both doors of a parity test really do read the same settings."""
        from organize_core.config import load_config

        self.paths.config_file.write_text(
            CONFIG_TOML.format(vault=self.vault) + extra, encoding="utf-8"
        )
        self.config = load_config(self.paths)

    @property
    def env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": os.pathsep.join(
                [str(REPO / "src"), *([p] if (p := os.environ.get("PYTHONPATH")) else [])]
            ),
            "ORGANIZE_CORE_CONFIG_DIR": str(self.paths.config_dir),
            "ORGANIZE_CORE_STATE_DIR": str(self.paths.state_dir),
            "ORGANIZE_CORE_RUNTIME_DIR": str(self.paths.runtime_dir),
        }

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*ORGANIZE, *args],
            capture_output=True,
            text=True,
            timeout=180,
            env=self.env,
        )


@pytest.fixture()
def two_frontends(tmp_path: Path) -> tuple[Frontend, Frontend]:
    cli_side = Frontend(tmp_path / "via-cli")
    rpc_side = Frontend(tmp_path / "via-rpc")
    # Sanity: the two vaults start identical, so any later difference is
    # attributable to the operation and not to the fixtures.
    assert tree(cli_side.vault) == tree(rpc_side.vault)
    return cli_side, rpc_side


@pytest.fixture()
def served(request: pytest.FixtureRequest) -> Iterator[Any]:
    """Start a server for a Frontend, on a SHORT socket path.

    tmp_path is far too long for AF_UNIX (~104 bytes), so the socket goes in
    a short-lived directory of its own.
    """
    started: list[tuple[OrganizeServer, threading.Thread, Path]] = []

    def start(frontend: Frontend) -> OrganizeServer:
        short = Path(f"/tmp/oc-it-{os.getpid()}-{len(started)}.sock")
        server = OrganizeServer(
            frontend.config, frontend.paths, socket_path=short, idle_timeout_seconds=0
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        assert server.ready.wait(timeout=30.0), "server never became ready"
        started.append((server, thread, short))
        return server

    yield start

    for server, thread, short in started:
        server.shutdown()
        thread.join(timeout=20.0)
        if short.exists():  # pragma: no cover - shutdown normally unlinks it
            short.unlink()


DESTINATION = "areas/health"


def test_cli_and_server_move_produce_byte_identical_vaults(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """Spec 10 §1: the CLI and the nvim client are two frontends over ONE
    core. If the same move through the two doors produces different vaults,
    the core is not the single source of truth it claims to be — and the
    difference would surface as "it works from the terminal but not from
    nvim", the hardest class of bug to chase.
    """
    cli_side, rpc_side = two_frontends

    # --- door 1: the shipped binary ---
    assert cli_side.cli("index", "--full").returncode == 0
    proc = cli_side.cli("move", CAPTURE, DESTINATION)
    assert proc.returncode == 0, proc.stderr

    # --- door 2: the JSON-RPC server ---
    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        result = client.result("op.move", path=CAPTURE, destination=DESTINATION)
    finally:
        client.close()
    assert result["ok"] is True, result.get("error")

    # `.backups` filenames carry a HH:MM:SS stamp, so they are compared by
    # content below rather than by name.
    cli_tree = tree(cli_side.vault, skip=(".backups",))
    rpc_tree = tree(rpc_side.vault, skip=(".backups",))
    assert cli_tree == rpc_tree

    # The comparison is only worth something if the move actually happened.
    moved = f"{DESTINATION}/{Path(CAPTURE).name}"
    assert moved in cli_tree
    assert CAPTURE not in cli_tree
    assert f"archive/capture/raw_capture/{Path(CAPTURE).name}" in cli_tree

    # ...and the same bytes were backed up on both sides.
    def backups(frontend: Frontend) -> list[str]:
        root = frontend.vault / ".backups"
        return sorted(
            hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()
        )

    assert backups(cli_side) == backups(rpc_side)


def test_cli_and_server_move_agree_on_the_written_frontmatter(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """The strongest single claim of 05 §2.6, checked on both doors at once:
    the destination copy gains its PARA tag and `processing_status`, and
    every pre-existing field survives (08 §A12)."""
    cli_side, rpc_side = two_frontends
    assert cli_side.cli("index", "--full").returncode == 0
    assert cli_side.cli("move", CAPTURE, DESTINATION).returncode == 0

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        client.result("op.move", path=CAPTURE, destination=DESTINATION)
    finally:
        client.close()

    name = Path(CAPTURE).name
    cli_fields = load_file(cli_side.vault / DESTINATION / name).frontmatter.fields
    rpc_fields = load_file(rpc_side.vault / DESTINATION / name).frontmatter.fields

    assert cli_fields == rpc_fields
    assert cli_fields["tags"] == ["impro", "creativity", "area/health"]
    assert cli_fields["processing_status"] == "organized"
    # Untouched fields survive verbatim on both doors.
    assert cli_fields["id"] == "2026-06-10T21:33:05.379Z"
    assert cli_fields["capture_id"] == "2026-06-10T21:33:05.379Z"
    assert cli_fields["location"]["city"] == "New York"


def test_cli_and_server_move_record_equivalent_actions(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """Spec 12 §2: every operation records. The two frontends must produce
    the SAME action shape — ids/timestamps/actors differ by construction, the
    rest must not."""
    cli_side, rpc_side = two_frontends
    assert cli_side.cli("index", "--full").returncode == 0
    assert cli_side.cli("move", CAPTURE, DESTINATION).returncode == 0

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        client.result("op.move", path=CAPTURE, destination=DESTINATION)
    finally:
        client.close()

    def moves(frontend: Frontend) -> list[dict[str, Any]]:
        return [r for r in read_actions(frontend.paths) if r["operation"] == "move"]

    cli_moves, rpc_moves = moves(cli_side), moves(rpc_side)
    assert len(cli_moves) == 1
    assert len(rpc_moves) == 1

    volatile = {"id", "ts", "actor", "context"}

    def stable(record: dict[str, Any], frontend: Frontend) -> dict[str, Any]:
        out = {k: v for k, v in record.items() if k not in volatile}
        # Paths are absolute and vault-rooted; compare them vault-relative.
        text = json.dumps(out).replace(str(frontend.vault), "<VAULT>")
        return json.loads(text)

    assert stable(cli_moves[0], cli_side) == stable(rpc_moves[0], rpc_side)
    # Neither is a rehearsal, and both belong in the corpus.
    assert cli_moves[0]["context"]["dry_run"] is False
    assert rpc_moves[0]["context"]["dry_run"] is False
    # A move is ONE logical action with multiple targets (12 §2) — the
    # archiving of the original is a target on that record, not a second
    # action. Both doors must agree on that too, or `actions stats` would
    # count moves differently depending on where they came from.
    for frontend in (cli_side, rpc_side):
        assert ActionRecorder(frontend.paths.actions_dir).stats()["by_operation"] == {
            "move": 1
        }
    roles = [t["role"] for t in cli_moves[0]["targets"]]
    assert roles == [t["role"] for t in rpc_moves[0]["targets"]]
    assert "destination" in roles


def test_cli_and_server_move_write_equivalent_operation_logs(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """05 §1.5: the operations log is the undo trail. Both doors must leave
    one that says the same thing."""
    cli_side, rpc_side = two_frontends
    assert cli_side.cli("index", "--full").returncode == 0
    assert cli_side.cli("move", CAPTURE, DESTINATION).returncode == 0

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        client.result("op.move", path=CAPTURE, destination=DESTINATION)
    finally:
        client.close()

    def ops(frontend: Frontend) -> list[str]:
        text = frontend.paths.operations_log.read_text(encoding="utf-8")
        out = []
        for line in text.splitlines():
            if not line.strip():
                continue
            # Drop the leading [timestamp], vault-root the paths, and blank
            # the %Y%m%d_%H%M%S stamp in backup filenames — the two doors run
            # seconds apart, and comparing wall clocks would make this flaky
            # without testing anything.
            body = line.split("] ", 1)[1] if "] " in line else line
            body = body.replace(str(frontend.vault), "<VAULT>")
            out.append(re.sub(r"\d{8}_\d{6}", "<STAMP>", body))
        return out

    cli_ops, rpc_ops = ops(cli_side), ops(rpc_side)
    assert cli_ops == rpc_ops
    assert len(cli_ops) == 2
    assert all("[SUCCESS]" in line for line in cli_ops)
    # The original is archived BEFORE the move line is written, on both doors.
    assert cli_ops[0].startswith("archive:")
    assert cli_ops[1].startswith("move:")
    assert (
        f"<VAULT>/{CAPTURE} -> <VAULT>/{DESTINATION}/{Path(CAPTURE).name}" in cli_ops[1]
    )


def test_a_move_through_either_door_teaches_the_same_lesson(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """ARCHITECTURE resolution #5: `record_move` is pure and the CALLER
    persists. There are two callers — cli.py and server.py — so this is
    exactly the seam where one of them can forget, and 08 §A4 is the bug
    where learning.json stayed at total_moves 0 forever."""
    cli_side, rpc_side = two_frontends
    assert cli_side.cli("index", "--full").returncode == 0
    assert cli_side.cli("move", CAPTURE, DESTINATION).returncode == 0

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        client.result("op.move", path=CAPTURE, destination=DESTINATION)
    finally:
        client.close()

    def learning(frontend: Frontend) -> dict[str, Any]:
        raw = json.loads(frontend.paths.learning_path.read_text(encoding="utf-8"))
        return json.loads(json.dumps(raw).replace(str(frontend.vault), "<VAULT>"))

    cli_learning, rpc_learning = learning(cli_side), learning(rpc_side)

    assert cli_learning["statistics"]["total_moves"] == 1
    assert rpc_learning["statistics"]["total_moves"] == 1
    assert cli_learning["statistics"]["destinations"] == {f"<VAULT>/{DESTINATION}": 1}
    assert (
        cli_learning["statistics"]["destinations"]
        == rpc_learning["statistics"]["destinations"]
    )
    # The association KEY is what a later suggestion looks up, so the two
    # doors agreeing on it is the whole point.
    assert sorted(cli_learning["associations"]) == sorted(rpc_learning["associations"])


def test_dry_run_move_is_vault_free_through_both_doors(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """ARCHITECTURE ruling (a) on both frontends at once."""
    cli_side, rpc_side = two_frontends
    before = tree(cli_side.vault)

    assert cli_side.cli("index", "--full").returncode == 0
    assert cli_side.cli("--dry-run", "move", CAPTURE, DESTINATION).returncode == 0

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        result = client.result(
            "op.move", path=CAPTURE, destination=DESTINATION, dry_run=True
        )
    finally:
        client.close()
    assert result["dry_run"] is True

    assert tree(cli_side.vault) == before
    assert tree(rpc_side.vault) == before
    # Both logged the rehearsal; neither admitted it to the corpus.
    for frontend in (cli_side, rpc_side):
        recorder = ActionRecorder(frontend.paths.actions_dir)
        assert recorder.stats()["total"] == 0
        assert recorder.stats(include_dry_run=True)["by_operation"] == {"move": 1}
        assert "[DRY-RUN]" in frontend.paths.operations_log.read_text(encoding="utf-8")
    # ...and no learning was written from a rehearsal (08 §A4 in reverse:
    # the bug was learning that never happened; this is learning that must not).
    for frontend in (cli_side, rpc_side):
        assert not frontend.paths.learning_path.exists()


def test_suggestions_agree_across_the_two_doors(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """04 + 11 §1 scoring is pure, so both frontends must rank identically —
    including the route entries merged on top."""
    cli_side, rpc_side = two_frontends
    assert cli_side.cli("index", "--full").returncode == 0
    proc = cli_side.cli("suggest", CAPTURE, "--json")
    assert proc.returncode == 0, proc.stderr
    cli_suggestions = json.loads(proc.stdout)

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        rpc_suggestions = client.result("suggest.for_note", path=CAPTURE)
    finally:
        client.close()

    # The ENVELOPES differ by design — `--json` names the subject it scored,
    # which a shell caller needs and an RPC caller already knows. The ranked
    # payload inside must be identical.
    assert cli_suggestions["subject"] == CAPTURE
    assert isinstance(rpc_suggestions, list)

    def shape(
        entries: list[dict[str, Any]], frontend: Frontend
    ) -> list[tuple[str, str, float, str]]:
        return [
            (
                str(e["path"]).replace(str(frontend.vault), "<VAULT>"),
                e["type"],
                round(float(e["score"]), 9),
                # `destination_kind` is part of the payload both doors must
                # agree on (21 §3.3): it decides whether an accept MOVES or
                # MERGES, so a door that omitted it would send the client to
                # the wrong operation.
                e["destination_kind"],
            )
            for e in entries
        ]

    cli_shape = shape(cli_suggestions["suggestions"], cli_side)
    assert cli_shape == shape(rpc_suggestions, rpc_side)

    # The exact ranking, not just "they match" — otherwise two identically
    # broken doors would pass. This capture's tags (impro, creativity) match
    # no FOLDER name at any depth, so on a DEFAULT install the honest answer
    # is the archive row alone (the SQ-1 ruling: a zero-signal capture gets
    # "Archive Now", not nine unexplained rows).
    #
    # `resources/performing/impro.md` is a note whose stem is `impro` and
    # would score 2.0 + 1.5 + 0.1 = 3.6 with `suggestions.note_candidates`
    # on — that is the value the note ballot will add once spec 21 §5.3's
    # quality gates pass and selecting a note MERGES (doc 19) instead of
    # moving. It ships off, so it is not in a default answer, and
    # `tests/test_destination_recall.py` covers it opted-in.
    assert cli_shape == [
        ("<VAULT>/archive/capture/raw_capture", "archive", 0.1, "folder"),
    ]


def test_the_two_doors_reject_the_same_bad_move(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """09 §1.5: a bad request fails loudly on both doors, with the SAME
    taxonomy class — not a traceback on one and a tidy message on the other."""
    cli_side, rpc_side = two_frontends
    assert cli_side.cli("index", "--full").returncode == 0

    proc = cli_side.cli("move", "capture/raw_capture/no-such-note.md", DESTINATION)
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert "VaultError:" in proc.stderr

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        response = client.call(
            "op.move", path="capture/raw_capture/no-such-note.md", destination=DESTINATION
        )
    finally:
        client.close()
    assert "error" in response
    assert response["error"]["data"]["kind"] == "VaultError"

    # Neither door touched either vault.
    assert tree(cli_side.vault) == tree(rpc_side.vault)


def test_server_refuses_an_unusable_socket_path_with_a_hint(
    tmp_path: Path,
) -> None:
    """The CLI seat asked for this: an unusable --socket used to surface as a
    bare `PermissionError: [Errno 13]`, which the CLI could not attach a hint
    to because it did not raise it (09 §1.5)."""
    from organize_core.errors import ServerError

    frontend = Frontend(tmp_path / "bad-socket")
    server = OrganizeServer(
        frontend.config,
        frontend.paths,
        socket_path=Path("/proc/definitely-not-a-directory/x.sock"),
        idle_timeout_seconds=0,
    )
    with pytest.raises(ServerError) as excinfo:
        server.serve_forever()
    assert "/proc/definitely-not-a-directory" in str(excinfo.value)
    assert excinfo.value.hint
    assert "--socket" in excinfo.value.hint


def test_a_long_socket_path_says_so_instead_of_af_unix_path_too_long(
    tmp_path: Path,
) -> None:
    """tmp_path is itself long enough to trigger this, and CPython raises a
    bare OSError with NO errno for it — so the hint is derived from the
    length, not from errno."""
    from organize_core.errors import ServerError

    frontend = Frontend(tmp_path / "long-socket")
    long_path = frontend.paths.runtime_dir / ("x" * 120 + ".sock")
    server = OrganizeServer(
        frontend.config, frontend.paths, socket_path=long_path, idle_timeout_seconds=0
    )
    with pytest.raises(ServerError) as excinfo:
        server.serve_forever()
    assert "AF_UNIX allows about" in (excinfo.value.hint or "")


# ===========================================================================
# 3. Performance smoke (spec 09 §4) — real numbers, recorded
# ===========================================================================

NOTE_TEMPLATE = """---
timestamp: '2026-06-10T21:33:05.379743+00:00'
id: perf-{i}
aliases:
  - perf-alias-{i}
capture_id: perf-{i}
modalities:
  - text
context: []
sources:
  - me
tags:
  - impro
  - creativity
  - perf-{bucket}
location:
  latitude: 40.7126
  longitude: -74.0066
  city: New York
metadata: {{}}
processing_status: raw
created_date: '2026-06-10'
last_edited_date: '2026-06-10'
---

## Content

Generated capture number {i} with a couple of lines of body text so the
parser has something realistic to chew on.
"""


def generate_vault(root: Path, count: int) -> Path:
    """A vault of `count` capture-shaped notes plus the PARA skeleton."""
    for folder in (
        "capture/raw_capture",
        "projects/blog",
        "areas/health",
        "resources/performing",
        "archive/capture/raw_capture",
    ):
        (root / folder).mkdir(parents=True, exist_ok=True)
    raw = root / "capture/raw_capture"
    for i in range(count):
        (raw / f"perf-{i:05d}.md").write_text(
            NOTE_TEMPLATE.format(i=i, bucket=i % 20), encoding="utf-8"
        )
    return root


@pytest.mark.parametrize("count", [1000])
def test_perf_full_reindex_of_a_generated_vault(
    count: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 09 §4: "full reindex of 10k notes < 5 s", checked at the 1k scale
    the index seat set as the always-on CI gate. The full-scale gate is
    :func:`test_perf_full_reindex_at_the_real_10k_spec_scale` below, marked
    ``slow`` so the default suite stays fast.

    Measured 2026-08-16 on this machine: 0.17 s for 1000 notes standalone,
    0.41 s under full-suite load. Before the frontmatter single-parse fix the
    same work took ~1.2 s, so the headroom is real and recent.
    """
    vault = generate_vault(tmp_path / "perfvault", count)
    paths = make_paths(tmp_path)
    paths.ensure_state_dirs()
    config = make_config(vault)
    index = VaultIndex(config, paths.index_path)

    start = time.perf_counter()
    stats = index.full_reindex()
    elapsed = time.perf_counter() - start

    assert stats["total"] == count
    with capsys.disabled():
        print(
            f"\n[perf] full_reindex({count} notes) = {elapsed:.3f}s "
            f"({elapsed / count * 1e3:.3f} ms/note; "
            f"10k extrapolates to {elapsed / count * 10000:.2f}s, gate 5s)"
        )
    # The 1k gate the index seat set, kept unmarked so it always runs. It is
    # deliberately the only hard assertion here: extrapolating a 1k run to the
    # 10k budget is contention-sensitive (a loaded box roughly doubles it), and
    # a perf gate that fails for the wrong reason gets muted — which is worse
    # than not having one. The real 10k number is measured below.
    assert elapsed < 2.0, f"full_reindex of {count} notes took {elapsed:.2f}s"


@pytest.mark.slow
def test_perf_full_reindex_at_the_real_10k_spec_scale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The literal spec 09 §4 gate, measured rather than extrapolated.

    Measured 2026-08-16 on this machine: **1.7 s** for 10 000 notes on an idle
    box (cold and warm-rebuild alike), 2.9 s immediately after a full suite
    run, against the 5 s budget. Generating the vault dominates this test's
    runtime, which is why it is opt-in (``pytest -m slow``).
    """
    vault = generate_vault(tmp_path / "perfvault", 10_000)
    paths = make_paths(tmp_path)
    paths.ensure_state_dirs()
    index = VaultIndex(make_config(vault), paths.index_path)

    start = time.perf_counter()
    stats = index.full_reindex()
    elapsed = time.perf_counter() - start

    assert stats["total"] == 10_000
    with capsys.disabled():
        print(f"\n[perf] full_reindex(10000 notes) = {elapsed:.3f}s (gate 5s)")
    assert elapsed < 5.0, f"spec 09 §4 gate: 10k reindex took {elapsed:.2f}s"


def test_perf_suggest_on_a_warm_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 09 §4: "suggestions < 100 ms". Measured on a warm 1k-note index,
    which is the state the server holds (10 §1)."""
    from organize_core.learn import LearningData
    from organize_core.suggest import CaptureFeaturesView, generate_candidates, suggest

    vault = generate_vault(tmp_path / "perfvault", 1000)
    paths = make_paths(tmp_path)
    paths.ensure_state_dirs()
    config = make_config(vault)
    index = VaultIndex(config, paths.index_path)
    index.full_reindex()

    record = index.get(vault / "capture/raw_capture/perf-00042.md")
    assert record is not None
    candidates = generate_candidates(
        {
            para: [str(p) for p in index.para_subfolders(para)]
            for para in ("projects", "areas", "resources")
        }
    )
    view = CaptureFeaturesView.from_record(record)
    learning = LearningData()
    archive = Path(config.vault.root) / "archive"

    timings: list[float] = []
    for _ in range(20):
        start = time.perf_counter()
        suggestions = suggest(
            view,
            candidates,
            config.suggestions,
            learning,
            now=FIXED_NOW,
            archive_path=str(archive),
        )
        timings.append((time.perf_counter() - start) * 1e3)
    assert suggestions, "the fixture must produce at least one suggestion"

    worst = max(timings)
    median = sorted(timings)[len(timings) // 2]
    with capsys.disabled():
        print(
            f"[perf] suggest() on a warm 1k index = {median:.3f} ms median, "
            f"{worst:.3f} ms worst of {len(timings)} (gate 100 ms)"
        )
    assert worst < 100.0, f"suggest() worst case {worst:.1f} ms exceeds the 100 ms gate"


# ===========================================================================
# Phase-1 fix pass — parity is what makes divergence impossible to reintroduce
# ===========================================================================


def test_a_merge_through_either_door_teaches_the_same_lesson(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """08 §A4 was only half-fixed.

    Spec 03 §6's outcome table requires `record_move` on Merge "with the
    target's folder", and spec 04 §3 says "on every successful accept/`move`/
    merge". The JSON-RPC server did exactly that; `cli.cmd_merge` did not —
    and NO test anywhere asserted that a merge produces learning, so the two
    clients silently disagreed about the same user action and the suite
    stayed green. The parity assertion below is the thing that makes that
    class of divergence impossible to reintroduce; the move-only version of
    this test is what let it in.
    """
    cli_side, rpc_side = two_frontends
    assert cli_side.cli("index", "--full").returncode == 0
    assert cli_side.cli("merge", CAPTURE, MERGE_TARGET).returncode == 0

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        result = client.result("op.merge_commit", path=CAPTURE, target=MERGE_TARGET)
        assert result["ok"] is True, result["error"]
    finally:
        client.close()

    def learning(frontend: Frontend) -> dict[str, Any]:
        raw = json.loads(frontend.paths.learning_path.read_text(encoding="utf-8"))
        return json.loads(json.dumps(raw).replace(str(frontend.vault), "<VAULT>"))

    cli_learning, rpc_learning = learning(cli_side), learning(rpc_side)
    folder = "<VAULT>/" + str(Path(MERGE_TARGET).parent)

    assert cli_learning["statistics"]["total_moves"] == 1
    assert rpc_learning["statistics"]["total_moves"] == 1
    assert cli_learning["statistics"]["destinations"] == {folder: 1}, (
        "the learning key is the target's FOLDER — `suggest` scores folder "
        "candidates, so a file-keyed association never reads back"
    )
    assert (
        cli_learning["statistics"]["destinations"]
        == rpc_learning["statistics"]["destinations"]
    )
    assert sorted(cli_learning["associations"]) == sorted(rpc_learning["associations"])
    assert sorted(cli_learning["patterns"]) == sorted(rpc_learning["patterns"])


@pytest.mark.parametrize("destination", ["../OUTSIDE", "<ABSOLUTE>"])
def test_the_two_doors_reject_the_same_out_of_vault_destination(
    destination: str, two_frontends: tuple[Frontend, Frontend], served: Any, tmp_path: Path
) -> None:
    """ARCHITECTURE ruling #19 and spec 05 §1 ("no code path may lose note
    content" / the core is the sole writer).

    `test_the_two_doors_reject_the_same_bad_move` pinned agreement for a
    MISSING note only. The CLI had NO containment check at all, so
    `organize move <note> ../OUTSIDE` wrote vault note content outside the
    vault and reported SUCCESS while the RPC door refused the identical
    request — and nothing in the suite noticed.
    """
    cli_side, rpc_side = two_frontends
    assert cli_side.cli("index", "--full").returncode == 0
    cli_before, rpc_before = tree(cli_side.vault), tree(rpc_side.vault)
    escape = tmp_path / "ESCAPED"
    destination = str(escape) if destination == "<ABSOLUTE>" else destination

    proc = cli_side.cli("move", CAPTURE, destination)
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr
    assert "VaultError:" in proc.stderr
    assert "outside the vault" in proc.stderr

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        response = client.call("op.move", path=CAPTURE, destination=destination)
    finally:
        client.close()
    assert "error" in response
    assert "outside the vault" in response["error"]["message"]

    assert tree(cli_side.vault) == cli_before
    assert tree(rpc_side.vault) == rpc_before
    assert not escape.exists(), "note content was written outside the vault"
    for frontend in (cli_side, rpc_side):
        if not destination.startswith("/"):
            escaped = (frontend.vault / destination).resolve()
            assert not escaped.exists(), f"note content was written outside the vault: {escaped}"


def test_both_doors_apply_the_doc_07_metadata_rules(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """Spec 10 §3: "everything behavioral lives in the core config so CLI and
    UI can never disagree". The `metadata_fields` coercion lived in `cli.py`
    and `server._meta_set` never called it, so doc 07 acceptance 1 held on
    one door and failed on the other."""
    cli_side, rpc_side = two_frontends
    note = QUIRK_FILES["metadata_empty_map"]
    for frontend in (cli_side, rpc_side):
        frontend.write_config(
            extra="""
[[metadata_fields]]
key = "tags"
type = "list"
keymap = "<leader>mt"
normalize = "kebab"

[[metadata_fields]]
key = "importance"
type = "enum"
keymap = "<leader>mi"
values = ["high", "medium", "low"]
"""
        )
    assert cli_side.cli("index", "--full").returncode == 0
    assert cli_side.cli("set-meta", note, "tags=foo, Bar Baz").returncode == 0

    server = served(rpc_side)
    client = RpcClient(server.socket_path)
    try:
        assert client.result("meta.set", path=note, changes={"tags": "foo, Bar Baz"})["ok"]
        rejected = client.call("meta.set", path=note, changes={"importance": "nonsense"})
    finally:
        client.close()

    cli_text = (cli_side.vault / note).read_text(encoding="utf-8")
    rpc_text = (rpc_side.vault / note).read_text(encoding="utf-8")
    assert "- foo" in cli_text and "- bar-baz" in cli_text
    assert "- foo" in rpc_text and "- bar-baz" in rpc_text
    assert "foo, Bar Baz" not in rpc_text

    # ...and both doors refuse an out-of-enum value with the same taxonomy.
    bad = cli_side.cli("set-meta", note, "importance=nonsense")
    assert bad.returncode != 0
    assert "ConfigError:" in bad.stderr
    assert rejected["error"]["data"]["kind"] == "ConfigError"


# ===========================================================================
# 5. `integrate` end to end — doc 12 §1's Claude-edit path (Phase 5)
# ===========================================================================
#
# The only suite that drives integrate through a REAL LLM client. The others
# inject a `FakeLLM` object; here the backend is `claude-cli` pointed at a
# fake `claude` SCRIPT, so `ClaudeCLIClient` (argv, stdin, timeout, utf-8
# decoding) is under test too — and, decisively, the CLI door can be driven
# as a SUBPROCESS, where no monkeypatch reaches. A wiring fault between
# config → get_client → propose is exactly the seam class this file exists
# for, and it is invisible to a suite that hands `propose` a client directly.

#: The capture integrated below is CAPTURE (tags [impro, creativity]); the
#: target is the fixture vault's one `resources/performing` note.
INTEGRATE_TARGET = "resources/performing/impro.md"


def install_fake_claude(root: Path, content: str, *, rationale: str = "Added under Ideas.") -> Path:
    """A `claude -p` stand-in that ignores its prompt and prints one fixed
    JSON object. The prompt CONTRACT is asserted by the integrate seat's own
    suite; what this fake is for is the plumbing around it."""
    script = root / "fake-claude"
    payload = json.dumps({"content": content, "rationale": rationale})
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({payload!r})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def integrate_config_toml(script: Path, *, review: str = "diff", tag: str = "impro") -> str:
    """An integrate route on `tag`, pointed at INTEGRATE_TARGET.

    `tag` is a parameter because of a fixture fact worth stating: the target
    note `resources/performing/impro.md` is ITSELF tagged `impro`, and this
    suite's config scans `resources/`. A route on `impro` therefore matches
    the target as well as the capture, so any test that lets the ROUTER pick
    what to process (rather than naming a note itself) must route on a tag
    only the capture carries — `creativity`. That is fixture geometry, not a
    product defect: a route whose tag appears on its own destination is a
    legal, if odd, config.
    """
    return f"""
[llm]
backend = "claude-cli"
integrate_backend = "claude-cli"
claude_command = ["{sys.executable}", "{script}"]

[integrate]
review = "{review}"

[[routes]]
tags = ["{tag}"]
destination = "{INTEGRATE_TARGET}"
mode = "integrate"
review = "{review}"
description = "Improv practice notes."
"""


def woven(vault: Path, body: str | None = None) -> str:
    """The target with the capture's body woven in VERBATIM — what a
    well-behaved model returns. Derived from the real file so the golden
    cannot drift from the fixture."""
    text = body if body is not None else capture_body(vault)
    return (vault / INTEGRATE_TARGET).read_text(encoding="utf-8") + f"\n## Ideas\n\n{text}\n"


def capture_body(vault: Path) -> str:
    return load_file(vault / CAPTURE).body.strip()


def test_the_interactive_integrate_flow_lands_the_reviewed_bytes(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """THE PHASE-5 ACCEPTANCE FLOW, through the real RPC server: propose →
    (a human accepts) → commit.

    This is the shape spec 12 §1 describes ("the nvim client shows the
    unified diff in the right pane; `<CR>` applies") and the shape the
    ARCHITECTURE wire contract makes STATELESS: the proposal returned by
    `op.integrate_propose` is handed straight back to `op.integrate_commit`,
    because review time is exactly when an idle core exits.

    Asserted end to end: the target holds the reviewed bytes EXACTLY, the
    capture's words survive verbatim, and ONE ActionRecord carries the full
    doc-12 §2 trace with proposed + final + verdict.
    """
    _cli_side, rpc_side = two_frontends
    script = install_fake_claude(rpc_side.root, "PLACEHOLDER")
    rpc_side.write_config(extra=integrate_config_toml(script))
    expected = woven(rpc_side.vault)
    install_fake_claude(rpc_side.root, expected)  # now that the target is known
    body = capture_body(rpc_side.vault)
    assert body and body in expected  # the fixture really carries the words

    server = served(rpc_side)
    server.index.full_reindex()
    client = RpcClient(server.socket_path)
    try:
        proposal = client.result(
            "op.integrate_propose", note=CAPTURE, target=INTEGRATE_TARGET, route="impro"
        )
        # PROPOSE WRITES NOTHING. This is the half a client is most likely to
        # get wrong, so it is asserted before the commit rather than inferred.
        assert (rpc_side.vault / INTEGRATE_TARGET).read_text(encoding="utf-8") != expected
        assert proposal["proposal_id"].startswith("prop_")
        assert proposal["review"] == "diff"  # the gate that made this two steps
        assert proposal["rationale"]
        assert proposal["diff"].startswith("--- ")
        assert proposal["target_snapshot"]["sha256"]
        # The route's description reached the proposal (12 §2 / 11 §1).
        assert proposal["description"] == "Improv practice notes."

        result = client.result("op.integrate_commit", proposal=proposal, verdict="accepted")
    finally:
        client.close()

    assert result["ok"] is True
    assert result["operation"] == "integrate"
    assert result["details"]["verdict"] == "accepted"
    assert result["details"]["proposal_id"] == proposal["proposal_id"]
    # EXACT bytes: what was reviewed is what landed. `integrate` deliberately
    # does NOT stamp last_edited_date of its own (unlike append_to_note), and
    # this equality is what pins that decision.
    assert (rpc_side.vault / INTEGRATE_TARGET).read_text(encoding="utf-8") == expected
    # 12 §1's VERBATIM directive, on the bytes actually on disk.
    assert body in (rpc_side.vault / INTEGRATE_TARGET).read_text(encoding="utf-8")

    (record,) = [r for r in read_actions(rpc_side.paths) if r["operation"] == "integrate"]
    assert record["actor"] == "claude-integrate"
    assert record["edit_mode"] == "integrate"
    assert record["capture"]["path"] == str(rpc_side.vault / CAPTURE)
    assert record["llm"]["verdict"] == "accepted"
    assert record["llm"]["backend"] == "claude-cli"
    assert record["llm"]["proposal_id"] == proposal["proposal_id"]
    # proposed == final for an accepted verdict, and BOTH are the real diff.
    assert record["llm"]["proposed_diff"] == record["llm"]["final_diff"]
    assert record["llm"]["proposed_diff"] == proposal["diff"]
    (target_state,) = record["targets"]
    assert target_state["role"] == "merge_target"
    assert target_state["description"] == "Improv practice notes."
    assert target_state["before_hash"] != target_state["after_hash"]


def test_a_rejected_integrate_records_the_verdict_and_leaves_the_vault_alone(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """Spec 12 §3 acceptance + the wire contract's "Rejected verdicts COMMIT
    (record written, no vault write — the negative signal is the point)".

    The record is the WHOLE deliverable here: a rejection that wrote nothing
    and recorded nothing is indistinguishable from a proposal that was never
    made, and doc 12's premise is that "a rejection is as much signal as an
    acceptance".
    """
    _cli_side, rpc_side = two_frontends
    script = install_fake_claude(rpc_side.root, "PLACEHOLDER")
    rpc_side.write_config(extra=integrate_config_toml(script))
    install_fake_claude(rpc_side.root, woven(rpc_side.vault))
    before = tree(rpc_side.vault)

    server = served(rpc_side)
    server.index.full_reindex()
    client = RpcClient(server.socket_path)
    try:
        proposal = client.result(
            "op.integrate_propose", note=CAPTURE, target=INTEGRATE_TARGET, route="impro"
        )
        result = client.result("op.integrate_commit", proposal=proposal, verdict="rejected")
    finally:
        client.close()

    assert result["ok"] is True
    assert result["details"]["verdict"] == "rejected"
    assert result["details"]["written"] is False
    # NOT ONE VAULT BYTE — every file, including the capture and the target.
    assert tree(rpc_side.vault) == before

    (record,) = [r for r in read_actions(rpc_side.paths) if r["operation"] == "integrate"]
    assert record["llm"]["verdict"] == "rejected"
    assert record["llm"]["proposed_diff"] == proposal["diff"]
    assert record["llm"]["final_diff"] == ""  # nothing was applied
    (target_state,) = record["targets"]
    assert target_state["before_hash"] == target_state["after_hash"]

    # And the negative signal must NOT become a positive one: learning.json
    # is untouched, because `learn.is_matt_decided` refuses any record whose
    # verdict is not accepted|edited. Byte-identical, with the ACCEPT case as
    # the firing control in the test above's sibling assertions.
    assert not rpc_side.paths.learning_path.exists() or (
        json.loads(rpc_side.paths.learning_path.read_text(encoding="utf-8"))["associations"] == {}
    )


def test_the_cli_two_step_integrate_proposes_then_commits_the_edited_diff(
    two_frontends: tuple[Frontend, Frontend]
) -> None:
    """The scripted door, as a SUBPROCESS: `organize integrate` proposes,
    `--commit-from … --verdict edited --final-diff …` commits Matt's own
    version. The `edited` verdict is what turns a reviewed integration into a
    LABELED EDIT EXAMPLE (12 §2: "`proposed_diff` vs `final_diff` … a
    rejection is as much signal as an acceptance"), so the two diffs must
    DIFFER in the record.
    """
    cli_side, _rpc_side = two_frontends
    script = install_fake_claude(cli_side.root, "PLACEHOLDER")
    cli_side.write_config(extra=integrate_config_toml(script))
    proposed_text = woven(cli_side.vault)
    install_fake_claude(cli_side.root, proposed_text)
    assert cli_side.cli("index", "--full").returncode == 0

    # 1. PROPOSE. Writes nothing; prints the proposal as JSON.
    before = tree(cli_side.vault)
    proc = cli_side.cli("integrate", CAPTURE, INTEGRATE_TARGET, "--route", "impro", "--json")
    assert proc.returncode == 0, proc.stderr
    proposal = json.loads(proc.stdout)
    assert tree(cli_side.vault) == before, "propose must not touch the vault"

    # 2. Matt edits the proposal: same capture body, his own heading.
    edited_text = proposed_text.replace("## Ideas", "## Improv ideas")
    assert edited_text != proposed_text
    from organize_core.fileops import _unified_diff

    final_diff = _unified_diff(
        (cli_side.vault / INTEGRATE_TARGET).read_text(encoding="utf-8"),
        edited_text,
        cli_side.vault / INTEGRATE_TARGET,
    )

    proposal_file = cli_side.root / "proposal.json"
    proposal_file.write_text(json.dumps(proposal), encoding="utf-8")
    diff_file = cli_side.root / "final.diff"
    diff_file.write_text(final_diff, encoding="utf-8")

    # 3. COMMIT the edit.
    proc = cli_side.cli(
        "integrate",
        "--commit-from", str(proposal_file),
        "--verdict", "edited",
        "--final-diff", str(diff_file),
    )
    assert proc.returncode == 0, proc.stderr

    # HIS version landed, not the model's.
    assert (cli_side.vault / INTEGRATE_TARGET).read_text(encoding="utf-8") == edited_text
    assert capture_body(cli_side.vault) in edited_text  # still verbatim

    (record,) = [r for r in read_actions(cli_side.paths) if r["operation"] == "integrate"]
    assert record["llm"]["verdict"] == "edited"
    assert record["llm"]["proposed_diff"] == proposal["diff"]
    assert record["llm"]["final_diff"] != record["llm"]["proposed_diff"], (
        "an `edited` verdict whose two diffs are equal is not an edit example"
    )
    assert "## Improv ideas" in record["llm"]["final_diff"]
    assert "## Improv ideas" not in record["llm"]["proposed_diff"]


def test_apply_is_refused_behind_the_review_gate_and_allowed_without_it(
    two_frontends: tuple[Frontend, Frontend]
) -> None:
    """`--apply` is the one-shot form, and the wire contract permits it "ONLY
    when review setting is auto". Both branches are exercised against the
    SAME command and the same fake model, so the refusal cannot be passing
    because integrate is broken.
    """
    cli_side, _rpc_side = two_frontends
    script = install_fake_claude(cli_side.root, "PLACEHOLDER")
    cli_side.write_config(extra=integrate_config_toml(script, review="diff"))
    expected = woven(cli_side.vault)
    install_fake_claude(cli_side.root, expected)
    assert cli_side.cli("index", "--full").returncode == 0
    before = tree(cli_side.vault)

    refused = cli_side.cli(
        "integrate", CAPTURE, INTEGRATE_TARGET, "--route", "impro", "--apply"
    )
    assert refused.returncode == 1
    assert "ConfigError:" in refused.stderr
    assert "review" in refused.stderr
    assert tree(cli_side.vault) == before
    assert read_actions(cli_side.paths) == []

    # FIRING CONTROL: ungate the route and the identical command applies.
    cli_side.write_config(extra=integrate_config_toml(script, review="auto"))
    applied = cli_side.cli(
        "integrate", CAPTURE, INTEGRATE_TARGET, "--route", "impro", "--apply"
    )
    assert applied.returncode == 0, applied.stderr
    assert (cli_side.vault / INTEGRATE_TARGET).read_text(encoding="utf-8") == expected
    (record,) = [r for r in read_actions(cli_side.paths) if r["operation"] == "integrate"]
    assert record["llm"]["verdict"] == "accepted"


def test_a_gutting_proposal_is_refused_and_recorded_through_the_real_stack(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """Spec 12 §3, verbatim: "Deletion guard: an LLM proposal that removes
    existing lines is rejected and recorded with `verdict: "rejected"`,
    target untouched."

    Driven through the RPC door with a real client so the guard is proven to
    run where it matters — before any human sees a diff — rather than only in
    the engine's own unit tests.
    """
    _cli_side, rpc_side = two_frontends
    # A model that returns a GUTTED target: the capture's line only.
    script = install_fake_claude(
        rpc_side.root, "---\ntags:\n- impro\n---\n" + capture_body(rpc_side.vault) + "\n"
    )
    rpc_side.write_config(extra=integrate_config_toml(script))
    before = tree(rpc_side.vault)

    server = served(rpc_side)
    server.index.full_reindex()
    client = RpcClient(server.socket_path)
    try:
        response = client.call(
            "op.integrate_propose", note=CAPTURE, target=INTEGRATE_TARGET, route="impro"
        )
    finally:
        client.close()

    assert "error" in response
    assert response["error"]["data"]["kind"] == "IntegrationRejected"
    assert "delete" in response["error"]["message"]
    assert tree(rpc_side.vault) == before

    # The refusal is RECORDED — that is the training signal doc 12 wants, and
    # the half a naive implementation drops on the floor.
    (record,) = [r for r in read_actions(rpc_side.paths) if r["operation"] == "integrate"]
    assert record["llm"]["verdict"] == "rejected"
    assert record["llm"]["final_diff"] == ""


def test_no_ai_refuses_integrate_through_both_doors_for_every_actor(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """Spec 12 §1: "`no-ai: true` targets refuse `integrate` outright", and
    the Phase-4 routes ruling: "Integrate refuses for EVERY actor (12 §1)" —
    unlike move/append, where actor "matt" may proceed.

    Both doors, one law. The assertion that the model is never even asked is
    the load-bearing one: a refusal that happens AFTER the prompt was sent has
    already shipped a protected note to an AI backend.
    """
    cli_side, rpc_side = two_frontends
    for frontend in (cli_side, rpc_side):
        marker = frontend.root / "was-called"
        script = frontend.root / "fake-claude"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('called')\n"
            "sys.stdin.read()\n"
            'sys.stdout.write("{}")\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        frontend.write_config(extra=integrate_config_toml(script))

    assert cli_side.cli("index", "--full").returncode == 0
    cli_refusal = cli_side.cli("integrate", CAPTURE, NO_AI)
    assert cli_refusal.returncode == 1
    assert "NoAiRefusal:" in cli_refusal.stderr

    server = served(rpc_side)
    server.index.full_reindex()
    client = RpcClient(server.socket_path)
    try:
        response = client.call("op.integrate_propose", note=CAPTURE, target=NO_AI)
    finally:
        client.close()
    assert response["error"]["data"]["kind"] == "NoAiRefusal"

    for frontend in (cli_side, rpc_side):
        assert not (frontend.root / "was-called").exists(), (
            "a no-ai note reached the LLM backend before being refused"
        )

    # FIRING CONTROL: the same command against a normal target DOES reach the
    # model — so the refusals above are the no-ai law, not a broken fake.
    install_fake_claude(cli_side.root, woven(cli_side.vault))
    ok = cli_side.cli("integrate", CAPTURE, INTEGRATE_TARGET)
    assert ok.returncode == 0, ok.stderr


def test_actions_query_finds_the_precedent_the_corpus_actually_holds(
    two_frontends: tuple[Frontend, Frontend]
) -> None:
    """spec 13 §3's named surface, through the shipped binary: "corpus
    queryable by tag/content similarity (`actions query --similar-to
    <text>`)".

    The composition root has ONE job the engine cannot do — pass
    `config.suggestions.tag_normalization` through — and it is invisible
    without a vault that HAS a mapping. So this drives a vault whose config
    maps `impro -> improv`, files a real capture to build a precedent, and
    asserts the query reaches it through the mapping. Dropping the
    `tag_normalization=` argument in `cli._actions_query` makes the tag
    component score 0 and the precedent disappears.
    """
    cli_side, _rpc_side = two_frontends
    cli_side.write_config(
        extra="""
[suggestions.tag_normalization]
impro = "improv"
"""
    )
    assert cli_side.cli("index", "--full").returncode == 0
    # A real precedent: this capture (tags impro, creativity) filed to areas/health.
    assert cli_side.cli("move", CAPTURE, DESTINATION).returncode == 0

    proc = cli_side.cli(
        "actions", "query", "--similar-to", "improv warmups", "--tags", "improv", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["tags"] == ["improv"]
    (hit,) = payload["results"]
    assert hit["rank"] == 1
    assert hit["operation"] == "move"
    assert hit["capture"].endswith(Path(CAPTURE).name)
    # The tag component is what the normalization map unlocks: the corpus
    # holds `impro`, the query asked for `improv`.
    assert hit["tag_score"] > 0.0
    assert hit["score"] > hit["text_score"], (
        "the tag signal did not contribute — tag_normalization never reached "
        "the engine (the one thing the composition root must supply)"
    )

    # An honest empty answer for a query with nothing in common, so the hit
    # above cannot be "everything matches everything".
    proc = cli_side.cli("actions", "query", "--similar-to", "carburetor rebuild", "--json")
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["results"] == []

    # Human output says so out loud rather than printing nothing (09 §1.5).
    proc = cli_side.cli("actions", "query", "--similar-to", "carburetor rebuild")
    assert proc.stdout.strip() == "no similar actions in the corpus"


# ---------------------------------------------------------------------------
# 5b. Holes found by the Phase-5 mutation audit, closed
# ---------------------------------------------------------------------------
#
# Each test below exists because a specific mutation of the Phase-5 wiring
# left the suite GREEN. They are grouped here rather than scattered because
# the config gate, the `wants_llm` seam and the routes dispatch are ONE
# landing: the config gate is only safe BECAUSE the seam lands with it, so
# pinning them apart would let a future change satisfy each half separately.


def test_an_unattended_integrate_route_needs_review_auto_to_load_at_all() -> None:
    """The Phase-5 NARROWING of the Phase-4 blanket refusal, both directions.

    `auto = true` + `mode = "integrate"` + the default `review = "diff"` is a
    standing instruction to integrate unattended that contradicts its own
    "ask a human first" gate. It can only ever produce a per-note ERROR, exit
    1, and an OnFailure alert every ten minutes — which is what trains an
    operator to ignore the channel. So it fails ONCE, at the door (03 §1).

    MUTATION THIS CATCHES: deleting the check entirely. Nothing else in the
    suite noticed, because the resulting config loads fine and only misbehaves
    ten minutes later on a real vault.
    """
    from organize_core.config import validate_config
    from organize_core.errors import RouteConfigError

    def raw(**route: Any) -> dict[str, Any]:
        return {
            "vault": {"root": "/tmp/does-not-need-to-exist"},
            "routes": [
                {
                    "tags": ["x"],
                    "destination": "areas/health/log.md",
                    "mode": "integrate",
                    **route,
                }
            ],
        }

    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(raw(auto=True), source="<test>")
    assert "auto" in str(excinfo.value)
    assert 'review = "auto"' in (excinfo.value.hint or "")

    # Explicit `review = "diff"` is refused for the same reason as the default.
    with pytest.raises(RouteConfigError):
        validate_config(raw(auto=True, review="diff"), source="<test>")

    # FIRING CONTROLS — the gate must not be "integrate routes cannot be auto".
    opted_in = validate_config(raw(auto=True, review="auto"), source="<test>")
    assert opted_in.routes[0].auto is True
    assert opted_in.routes[0].review == "auto"
    interactive = validate_config(raw(auto=False), source="<test>")
    assert interactive.routes[0].auto is False


def test_effective_mode_prefers_the_route_over_the_global_default() -> None:
    """Spec 12 §1's mode precedence: "per-route default via `mode` in doc 11;
    global default `manual`". Asserted at a parameter where the two DISAGREE,
    so a resolver that ignores either side is red.

    MUTATION THIS CATCHES: dropping the route branch, which is invisible when
    the route's mode happens to equal the global default.
    """
    from organize_core import routes as routes_mod
    from organize_core.config import IntegrateConfig, RouteConfig

    config = Config(
        vault=VaultConfig(root=Path("/vault")),
        integrate=IntegrateConfig(default_mode="append"),
        routes=[RouteConfig(tags=["x"], destination="a/b.md", mode="integrate")],
    )
    (match,) = routes_mod.resolve(["x"], config)
    assert routes_mod.effective_mode(match, config) == "integrate"  # route wins
    assert routes_mod.effective_mode(None, config) == "append"  # global answers


def test_routes_named_resolves_the_named_route_and_refuses_an_unknown_one() -> None:
    """`--route NAME` / `{"route": NAME}` for both composition roots. With
    TWO routes configured, so "returns the first one" is not a passing answer.

    MUTATION THIS CATCHES: a lookup that ignores the name — which silently
    attributes an integration to the wrong route, uses the wrong description
    in the prompt, and honors the wrong review gate.
    """
    from organize_core import routes as routes_mod
    from organize_core.config import RouteConfig
    from organize_core.errors import RouteConfigError

    config = Config(
        vault=VaultConfig(root=Path("/vault")),
        routes=[
            RouteConfig(tags=["alpha"], destination="a/one.md", mode="append", description="first"),
            RouteConfig(tags=["beta"], destination="b/two.md", mode="append", description="second"),
        ],
    )
    assert routes_mod.named(config, "beta").route.description == "second"
    assert routes_mod.named(config, "alpha").route.description == "first"

    with pytest.raises(RouteConfigError) as excinfo:
        routes_mod.named(config, "gamma")
    assert "gamma" in str(excinfo.value)
    assert "alpha" in (excinfo.value.hint or "")  # names the ones that exist


def test_routes_resolve_reports_the_route_mode_not_a_hardcoded_default(
    two_frontends: tuple[Frontend, Frontend]
) -> None:
    """`organize routes resolve --json` must report the EFFECTIVE mode, from
    a vault whose route mode and global default DIFFER — otherwise the field
    can be a constant and nobody notices.

    MUTATION THIS CATCHES: `default_mode` hardcoded to "manual", which is
    indistinguishable from the truth in a default config.
    """
    cli_side, _rpc_side = two_frontends
    script = install_fake_claude(cli_side.root, "unused")
    cli_side.write_config(
        extra=integrate_config_toml(script).replace(
            '[integrate]\nreview = "diff"', '[integrate]\nreview = "diff"\ndefault_mode = "append"'
        )
    )
    assert cli_side.cli("index", "--full").returncode == 0

    proc = cli_side.cli("routes", "resolve", CAPTURE, "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["default_mode"] == "append"  # the GLOBAL, honored
    (match,) = payload["matches"]
    assert match["mode"] == "integrate"  # the ROUTE, which overrides it
    assert match["review"] == "diff"


def test_the_pipeline_runs_an_unattended_integrate_route_end_to_end(
    two_frontends: tuple[Frontend, Frontend]
) -> None:
    """THE WHOLE PHASE-5 SAFETY CHAIN, through `organize run-consumers` as a
    subprocess: config validation accepts `auto = true` + `review = "auto"` →
    `wants_llm(config)` sees the integrate route → the runner injects
    `RunContext.llm` → `tag_router` hands it to `routes.apply_all` →
    `apply_route` proposes and commits → the capture is archived once.

    Every link is load-bearing and each one was mutable without a red suite
    before this test existed. The `taskwarrior.llm_enabled` failure — a
    feature that is correct in-module and inert in production — is exactly
    what this shape of test is for.
    """
    cli_side, _rpc_side = two_frontends
    script = install_fake_claude(cli_side.root, "PLACEHOLDER")
    expected = woven(cli_side.vault)
    install_fake_claude(cli_side.root, expected)
    cli_side.write_config(
        extra=integrate_config_toml(script, review="auto", tag="creativity").replace(
            'mode = "integrate"', 'mode = "integrate"\nauto = true'
        )
        + """
[consumers.router]
type = "tag_router"
"""
    )
    assert cli_side.cli("index", "--full").returncode == 0

    proc = cli_side.cli("run-consumers")
    assert proc.returncode == 0, proc.stderr + proc.stdout

    assert (cli_side.vault / INTEGRATE_TARGET).read_text(encoding="utf-8") == expected
    assert not (cli_side.vault / CAPTURE).is_file(), "the capture was not archived"
    (record,) = [r for r in read_actions(cli_side.paths) if r["operation"] == "integrate"]
    assert record["llm"]["verdict"] == "accepted"
    # One route fired, so the AGGREGATE record is actored to it (12 §2's enum
    # member for an unattended route firing) — not to claude-integrate.
    assert record["actor"] == "route:creativity"

    # AND THE LEARNING RULE HOLDS: a route firing is CONFIG, not a decision,
    # so it must not fold into learning.json even with an accepted verdict
    # (ARCHITECTURE: "LEARNING FOLDS ONLY MATT-DECIDED ACTIONS").
    learning = cli_side.paths.learning_path
    assert not learning.exists() or (
        json.loads(learning.read_text(encoding="utf-8"))["associations"] == {}
    )


def test_the_rpc_door_forces_the_llm_actor_and_ignores_a_client_claim(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """An LLM authored the edit whoever asked for it, and
    `learn.LLM_EDIT_ACTORS` keys the learning fold off exactly the string
    `claude-integrate`. A client that could claim `actor: "matt"` would make
    an unreviewed machine edit look like a human decision in the corpus — and
    a human actor folds into learning.json unconditionally.

    MUTATION THIS CATCHES: `self._context(params)` instead of
    `self._context({**params, "actor": integrate.ACTOR})`, on either handler.
    """
    _cli_side, rpc_side = two_frontends
    script = install_fake_claude(rpc_side.root, "PLACEHOLDER")
    rpc_side.write_config(extra=integrate_config_toml(script))
    install_fake_claude(rpc_side.root, woven(rpc_side.vault))

    server = served(rpc_side)
    server.index.full_reindex()
    client = RpcClient(server.socket_path)
    try:
        # The client LIES about who is acting, on BOTH calls.
        proposal = client.result(
            "op.integrate_propose", note=CAPTURE, target=INTEGRATE_TARGET, actor="matt"
        )
        client.result(
            "op.integrate_commit", proposal=proposal, verdict="accepted", actor="matt"
        )
    finally:
        client.close()

    (record,) = [r for r in read_actions(rpc_side.paths) if r["operation"] == "integrate"]
    assert record["actor"] == "claude-integrate", (
        "the client's actor claim reached the corpus — an LLM edit is recorded "
        "as a human decision and folds into learning.json"
    )


def test_the_rpc_door_applies_the_final_diff_for_an_edited_verdict(
    two_frontends: tuple[Frontend, Frontend], served: Any
) -> None:
    """`verdict: "edited"` means MATT's version lands, not the model's. A
    handler that drops `final_diff` writes the proposal instead — silently
    discarding the human's edit while recording that an edit happened.

    MUTATION THIS CATCHES: `final_diff = None` in `_op_integrate_commit`.
    (The engine then refuses an empty final diff, but a handler could just as
    easily have fallen back to the proposal, which is the dangerous shape.)
    """
    from organize_core.fileops import _unified_diff

    _cli_side, rpc_side = two_frontends
    script = install_fake_claude(rpc_side.root, "PLACEHOLDER")
    rpc_side.write_config(extra=integrate_config_toml(script))
    proposed_text = woven(rpc_side.vault)
    install_fake_claude(rpc_side.root, proposed_text)
    edited_text = proposed_text.replace("## Ideas", "## Improv ideas")
    assert edited_text != proposed_text

    server = served(rpc_side)
    server.index.full_reindex()
    final_diff = _unified_diff(
        (rpc_side.vault / INTEGRATE_TARGET).read_text(encoding="utf-8"),
        edited_text,
        rpc_side.vault / INTEGRATE_TARGET,
    )
    client = RpcClient(server.socket_path)
    try:
        proposal = client.result(
            "op.integrate_propose", note=CAPTURE, target=INTEGRATE_TARGET
        )
        result = client.result(
            "op.integrate_commit",
            proposal=proposal,
            verdict="edited",
            final_diff=final_diff,
        )
    finally:
        client.close()

    assert result["ok"] is True
    assert (rpc_side.vault / INTEGRATE_TARGET).read_text(encoding="utf-8") == edited_text
    (record,) = [r for r in read_actions(rpc_side.paths) if r["operation"] == "integrate"]
    assert record["llm"]["verdict"] == "edited"
    assert record["llm"]["final_diff"] != record["llm"]["proposed_diff"]
    assert "## Improv ideas" in record["llm"]["final_diff"]


def test_the_cli_door_records_the_llm_actor_too(
    two_frontends: tuple[Frontend, Frontend]
) -> None:
    """The same actor law on the OTHER door. Spec 10 §3: "CLI and UI can never
    disagree" — and `organize integrate` is the door a Claude agent uses, so
    an actor drift here is the one most likely to go unnoticed.

    MUTATION THIS CATCHES: `actor="matt"` in `cmd_integrate`.
    """
    cli_side, _rpc_side = two_frontends
    script = install_fake_claude(cli_side.root, "PLACEHOLDER")
    cli_side.write_config(extra=integrate_config_toml(script, review="auto"))
    install_fake_claude(cli_side.root, woven(cli_side.vault))
    assert cli_side.cli("index", "--full").returncode == 0

    assert cli_side.cli(
        "integrate", CAPTURE, INTEGRATE_TARGET, "--route", "impro", "--apply"
    ).returncode == 0

    (record,) = [r for r in read_actions(cli_side.paths) if r["operation"] == "integrate"]
    assert record["actor"] == "claude-integrate"
    # ...and therefore it DOES fold into learning (verdict accepted +
    # claude-integrate = Matt-decided), which is the other half of the rule.
    learning = json.loads(cli_side.paths.learning_path.read_text(encoding="utf-8"))
    assert learning["associations"], (
        "an accepted integration by claude-integrate must fold into learning.json "
        "(learn.MATT_DECIDED_VERDICTS) — the actor or the trace is not arriving"
    )


def test_commit_from_without_a_verdict_refuses_rather_than_guessing(
    two_frontends: tuple[Frontend, Frontend]
) -> None:
    """The verdict IS the training label (12 §2). There is no default,
    because guessing it would forge Matt's decision into the corpus that doc
    13 learns from.

    MUTATION THIS CATCHES: dropping the required-verdict check — argparse has
    no `required` on the flag, so the command would proceed with `None`.
    """
    cli_side, _rpc_side = two_frontends
    script = install_fake_claude(cli_side.root, "PLACEHOLDER")
    cli_side.write_config(extra=integrate_config_toml(script))
    install_fake_claude(cli_side.root, woven(cli_side.vault))
    assert cli_side.cli("index", "--full").returncode == 0

    proc = cli_side.cli("integrate", CAPTURE, INTEGRATE_TARGET, "--json")
    assert proc.returncode == 0, proc.stderr
    proposal_file = cli_side.root / "p.json"
    proposal_file.write_text(proc.stdout, encoding="utf-8")
    before = tree(cli_side.vault)

    refused = cli_side.cli("integrate", "--commit-from", str(proposal_file))
    assert refused.returncode == 1
    assert "verdict" in refused.stderr
    assert tree(cli_side.vault) == before

    # FIRING CONTROL: the identical command WITH a verdict commits.
    ok = cli_side.cli(
        "integrate", "--commit-from", str(proposal_file), "--verdict", "accepted"
    )
    assert ok.returncode == 0, ok.stderr
    assert tree(cli_side.vault) != before


def test_actions_query_keeps_absent_tags_and_empty_tags_apart(
    two_frontends: tuple[Frontend, Frontend]
) -> None:
    """`--tags` absent means "I have no tag information" (the tag component
    falls back to the query text's tokens); `--tags ""` means "this capture
    genuinely has no tags". Collapsing them makes the single-argument form
    silently lose the strongest signal in doc 04 §2.

    MUTATION THIS CATCHES: `tags = _split_list(args.tags)` unconditionally,
    which turns an absent flag into an empty list.
    """
    cli_side, _rpc_side = two_frontends
    assert cli_side.cli("index", "--full").returncode == 0
    assert cli_side.cli("move", CAPTURE, DESTINATION).returncode == 0

    absent = json.loads(
        cli_side.cli("actions", "query", "--similar-to", "impro", "--json").stdout
    )
    empty = json.loads(
        cli_side.cli(
            "actions", "query", "--similar-to", "impro", "--tags", "", "--json"
        ).stdout
    )
    assert absent["tags"] is None
    assert empty["tags"] == []
    # The corpus record is tagged `impro`, so the absent form reaches it
    # through the text→tags fallback and the empty form cannot.
    assert absent["results"], "the absent-tags fallback did not reach a tagged record"
    assert absent["results"][0]["tag_score"] > 0.0
    assert all(hit["tag_score"] == 0.0 for hit in empty["results"])


def test_a_standalone_integrate_leaves_the_capture_and_says_so(
    two_frontends: tuple[Frontend, Frontend]
) -> None:
    """A standalone `integrate` does NOT archive the capture — unlike
    `move`/`merge` (05 §2/§4) and unlike a ROUTE, where `apply_all` archives
    once after every destination succeeds (11 §1).

    The reason is doc 12's own premise, quoted from the directive that opens
    the spec: "I may be adding them to multiple files". The commit is
    STATELESS, so it cannot know whether another integration of this capture
    is coming, and archiving after the first would break the second.

    What must NOT happen is silence. A capture that is neither archived nor
    reported sits in the backlog forever with nothing saying why, which is the
    09 §1.5 silently-wrong-answer class from the other direction. So the
    asymmetry is asserted in BOTH halves: the file is still there, AND the
    command said so.
    """
    cli_side, _rpc_side = two_frontends
    script = install_fake_claude(cli_side.root, "PLACEHOLDER")
    cli_side.write_config(extra=integrate_config_toml(script, review="auto"))
    install_fake_claude(cli_side.root, woven(cli_side.vault))
    assert cli_side.cli("index", "--full").returncode == 0

    proc = cli_side.cli(
        "integrate", CAPTURE, INTEGRATE_TARGET, "--route", "impro", "--apply"
    )
    assert proc.returncode == 0, proc.stderr
    assert (cli_side.vault / CAPTURE).is_file(), "integrate archived the capture"
    assert "capture left at" in proc.stdout
    assert "organize archive" in proc.stdout

    # The named follow-up actually works — a hint pointing at a command that
    # does not do the job is worse than no hint (09 §1.5).
    assert cli_side.cli("archive", CAPTURE).returncode == 0
    assert not (cli_side.vault / CAPTURE).is_file()

    # CONTRAST, so the asymmetry is a decision and not an accident: the ROUTE
    # path through the same engine DOES archive (11 §1). Pinned in
    # `test_the_pipeline_runs_an_unattended_integrate_route_end_to_end`.
