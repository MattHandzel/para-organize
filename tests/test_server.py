"""End-to-end suite for the JSON-RPC server over a REAL unix socket.

Every test here drives :class:`OrganizeServer` through an actual socket with
the tiny stdlib client below — no mocked transport, no in-process shortcut —
and asserts EXACT values (spec 09 §3: the old suite passed while the product
was broken because it asserted ``> 0``).

Isolation (non-negotiable, spec 09 §1.4 + paths.py contract):

* the vault is always the ``fixture_vault`` tmp copy;
* the socket, lock, index, learning, operations log and actions dir always
  live under ``tmp_path`` — :func:`make_core_paths` never consults
  ``$XDG_RUNTIME_DIR``, so the real ``organize-core.sock`` cannot be touched.
  :func:`test_socket_lives_under_the_injected_runtime_dir` asserts that.
"""

from __future__ import annotations

import itertools
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES
from organize_core import API_VERSION
from organize_core.config import Config, RouteConfig, VaultConfig
from organize_core.fileops import parse_log_line
from organize_core.paths import CorePaths
from organize_core.server import (
    EVENT_INDEX_UPDATED,
    EVENT_OP_PROGRESS,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    NOT_IMPLEMENTED,
    ORGANIZE_ERROR,
    OrganizeServer,
)

# ---------------------------------------------------------------------------
# CorePaths for tests
# ---------------------------------------------------------------------------


class _ResolvedCorePaths(CorePaths):
    """Stand-in for the integrator-owned derived paths.

    ``paths.py`` is a SHARED file and its derived properties still
    ``raise NotImplementedError``; this subclass implements exactly the
    layout its docstrings specify (spec 10 §3) so the server suite can run
    today. :func:`make_core_paths` prefers the real implementation the moment
    that seat lands, so these tests convert to it without edits.
    """

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def index_path(self) -> Path:
        return self.state_dir / "index.json"

    @property
    def learning_path(self) -> Path:
        return self.state_dir / "learning.json"

    @property
    def operations_log(self) -> Path:
        return self.state_dir / "operations.log"

    @property
    def actions_dir(self) -> Path:
        return self.state_dir / "actions"

    @property
    def automations_db(self) -> Path:
        return self.state_dir / "automations.db"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def socket_path(self) -> Path:
        return self.runtime_dir / "organize-core.sock"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "organize-core.lock"

    def ensure_state_dirs(self) -> None:
        # Deliberately does NOT create runtime_dir: the docstring scopes this
        # to state, so the server must create the socket/lock parents itself.
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.actions_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)


def make_core_paths(tmp_path: Path) -> CorePaths:
    """Tmp-dir CorePaths; real implementation when available, else the
    documented stand-in above. NEVER reads the process environment."""
    kwargs = {
        "config_dir": tmp_path / "config",
        "state_dir": tmp_path / "state",
        "runtime_dir": tmp_path / "runtime",
    }
    real = CorePaths(**kwargs)
    try:
        _ = (real.socket_path, real.lock_path, real.index_path, real.learning_path)
        _ = (real.operations_log, real.actions_dir, real.backups_dir)
    except NotImplementedError:
        return _ResolvedCorePaths(**kwargs)
    return real


# ---------------------------------------------------------------------------
# The in-test JSON-RPC client (stdlib socket only)
# ---------------------------------------------------------------------------


_MISSING = object()


class RpcClient:
    """Newline-delimited JSON-RPC 2.0 client — the contract the nvim plugin
    will implement, exercised here so the wire format is actually tested."""

    def __init__(self, socket_path: Path, *, timeout: float = 10.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(socket_path))
        self._buffer = b""
        self._ids = itertools.count(1)
        self.handshake: dict[str, Any] = self.read_message()

    # --- transport ---

    def read_message(self) -> dict[str, Any]:
        while b"\n" not in self._buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("server closed the connection")
            self._buffer += chunk
        raw, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(raw.decode("utf-8", errors="replace"))

    def send_raw(self, line: str) -> None:
        self.sock.sendall(line.encode("utf-8"))

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: Any = _MISSING,
        api_version: Any = None,
    ) -> Any:
        rid = next(self._ids) if request_id is _MISSING else request_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if rid is not None:
            payload["id"] = rid
        if params is not None:
            payload["params"] = params
        if api_version is not None:
            payload["apiVersion"] = api_version
        self.send_raw(json.dumps(payload) + "\n")
        return rid

    # --- calls ---

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        """Full response envelope (result OR error)."""
        rid = self.send(method, params or None)
        while True:
            message = self.read_message()
            if message.get("id") == rid:
                return message

    def result(self, method: str, **params: Any) -> Any:
        response = self.call(method, **params)
        assert "error" not in response, response["error"]
        return response["result"]

    def error(self, method: str, **params: Any) -> dict[str, Any]:
        response = self.call(method, **params)
        assert "error" in response, response
        return response["error"]

    def next_event(self, *, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.read_message()
            if message.get("method") == "event":
                return message["params"]
        raise AssertionError("no event arrived before the deadline")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> RpcClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


ROUTE_QUESTION = RouteConfig(
    tags=["question"],
    destination="resources/answers/",
    mode="move",
    description="Answered questions land in the answers resource folder.",
)


def make_config(vault: Path, *, routes: list[RouteConfig] | None = None) -> Config:
    return Config(vault=VaultConfig(root=vault), routes=list(routes or []))


def start_server(server: OrganizeServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="organize-test-server")
    thread.daemon = True
    thread.start()
    assert server.ready.wait(timeout=15.0), "server never became ready"
    return thread


@pytest.fixture()
def paths(tmp_path: Path) -> CorePaths:
    return make_core_paths(tmp_path)


@pytest.fixture()
def config(fixture_vault: Path) -> Config:
    return make_config(fixture_vault, routes=[ROUTE_QUESTION])


@pytest.fixture()
def server(config: Config, paths: CorePaths) -> Iterator[OrganizeServer]:
    srv = OrganizeServer(config, paths, idle_timeout_seconds=0)
    thread = start_server(srv)
    try:
        yield srv
    finally:
        srv.shutdown()
        thread.join(timeout=10.0)
        assert not thread.is_alive(), "serve_forever did not exit after shutdown()"


@pytest.fixture()
def client(server: OrganizeServer) -> Iterator[RpcClient]:
    conn = RpcClient(server.socket_path)
    try:
        yield conn
    finally:
        conn.close()


def vault_of(server: OrganizeServer) -> Path:
    return Path(server.config.vault.root)


# ---------------------------------------------------------------------------
# Isolation guards
# ---------------------------------------------------------------------------


def test_socket_lives_under_the_injected_runtime_dir(
    server: OrganizeServer, tmp_path: Path
) -> None:
    """The suite may never bind the real $XDG_RUNTIME_DIR/organize-core.sock."""
    assert server.socket_path == tmp_path / "runtime" / "organize-core.sock"
    assert server.socket_path.is_socket()
    assert str(server.socket_path).startswith(str(tmp_path))
    assert str(server.paths.state_dir).startswith(str(tmp_path))


def test_state_files_are_created_only_under_tmp(server: OrganizeServer, tmp_path: Path) -> None:
    assert server.index.index_path == tmp_path / "state" / "index.json"
    assert server.oplog.log_file == tmp_path / "state" / "operations.log"
    assert server.recorder.actions_dir == tmp_path / "state" / "actions"


# ---------------------------------------------------------------------------
# Handshake, envelope, versioning (spec 10 §2)
# ---------------------------------------------------------------------------


def test_handshake_is_the_first_line_and_carries_api_version(server: OrganizeServer) -> None:
    with RpcClient(server.socket_path) as conn:
        assert conn.handshake == {"apiVersion": API_VERSION}


def test_every_response_envelope_carries_api_version(client: RpcClient) -> None:
    response = client.call("index.reindex")
    assert response["jsonrpc"] == "2.0"
    assert response["apiVersion"] == API_VERSION
    assert response["id"] == 1


def test_client_with_a_matching_major_version_is_served(client: RpcClient) -> None:
    rid = client.send("index.reindex", {}, api_version=API_VERSION)
    response = client.read_message()
    assert response["id"] == rid
    assert "error" not in response


def test_major_api_version_mismatch_is_refused_with_both_versions(client: RpcClient) -> None:
    client.send("index.reindex", {}, api_version=API_VERSION + 1)
    response = client.read_message()
    error = response["error"]
    assert error["code"] == INVALID_REQUEST
    assert error["data"]["serverApiVersion"] == API_VERSION
    assert error["data"]["clientApiVersion"] == API_VERSION + 1
    assert "mismatch" in error["message"]


def test_unknown_method_returns_method_not_found(client: RpcClient) -> None:
    error = client.error("session.begin")
    assert error["code"] == METHOD_NOT_FOUND
    assert "session.start" in error["data"]["known_methods"]


def test_malformed_line_does_not_drop_the_connection(client: RpcClient) -> None:
    client.send_raw("{not json at all\n")
    response = client.read_message()
    assert response["error"]["code"] == -32700
    assert response["id"] is None
    # the very same connection still works
    assert client.result("index.reindex")["total"] == 21


def test_notification_receives_no_response(client: RpcClient) -> None:
    client.send("index.reindex", {}, request_id=None)
    rid = client.send("search.query", {"criteria": {"tags": "impro"}})
    response = client.read_message()
    assert response["id"] == rid, "a notification must not produce a response line"


def test_missing_required_parameter_is_invalid_params(client: RpcClient) -> None:
    error = client.error("note.get")
    assert error["code"] == INVALID_PARAMS
    assert "'path'" in error["message"]


# ---------------------------------------------------------------------------
# Read methods
# ---------------------------------------------------------------------------


def test_session_start_returns_the_exact_raw_capture_backlog(
    client: RpcClient, server: OrganizeServer
) -> None:
    result = client.result("session.start")
    vault = vault_of(server)
    relative = [str(Path(record["path"]).relative_to(vault)) for record in result["captures"]]
    assert relative == [
        "capture/raw_capture/2026-04-08T16:51:24.690160+00:00.md",
        "capture/raw_capture/2026-06-10T21:33:05.379Z.md",
        "capture/raw_capture/2026-07-01T09:00:00.000Z.md",
        "capture/raw_capture/2026-07-02T10:00:00.000Z.md",
        "capture/raw_capture/scalar-tags.md",
        "capture/raw_capture/metadata-map.md",
        "capture/raw_capture/metadata-list.md",
        "capture/raw_capture/private-thought.md",
        "capture/raw_capture/dashes-in-values.md",
        "capture/raw_capture/meeting notes — café ☕.md",
        "capture/raw_capture/context-string.md",
    ]
    assert result["state"] == "active"
    assert result["counts"] == {"processed": 0, "skipped": 0, "remaining": 11}
    assert result["session_id"].startswith("ses_")
    assert result["session_id"] in server._sessions


def test_session_start_honors_filters(client: RpcClient) -> None:
    result = client.result("session.start", filters={"tags": "todo"})
    names = sorted(Path(r["path"]).name for r in result["captures"])
    assert names == ["2026-07-01T09:00:00.000Z.md", "metadata-list.md", "metadata-map.md"]


def test_session_start_with_zero_matches_is_an_active_empty_session(client: RpcClient) -> None:
    """Spec 03 §2 / ARCHITECTURE resolution 9: not an error."""
    result = client.result("session.start", filters={"tags": "no-such-tag"})
    assert result["captures"] == []
    assert result["state"] == "active"


def test_session_start_rejects_an_unknown_filter(client: RpcClient) -> None:
    error = client.error("session.start", filters={"colour": "red"})
    assert error["code"] == ORGANIZE_ERROR
    assert error["data"]["kind"] == "ConfigError"
    assert "valid filters" in error["data"]["hint"]


def test_note_get_returns_record_and_body(client: RpcClient, server: OrganizeServer) -> None:
    rel = QUIRK_FILES["current_schema"]
    payload = client.result("note.get", path=rel)
    assert payload["parse_error"] is False
    assert payload["record"]["tags"] == ["impro", "creativity"]
    assert payload["record"]["para_type"] == "capture"
    assert payload["record"]["path"] == str(vault_of(server) / rel)
    assert payload["frontmatter"]["id"] == "2026-06-10T21:33:05.379Z"
    assert payload["body"] == "## Content\nAn idea about improv warmups and creative flow.\n"


def test_note_get_tolerates_broken_frontmatter(client: RpcClient) -> None:
    payload = client.result("note.get", path=QUIRK_FILES["broken_yaml"])
    assert payload["parse_error"] is True
    assert payload["frontmatter"] == {}
    assert "body survives parser failure" in payload["body"]


def test_note_get_on_a_missing_note_is_an_organize_error(client: RpcClient) -> None:
    error = client.error("note.get", path="capture/raw_capture/nope.md")
    assert error["code"] == ORGANIZE_ERROR
    assert error["data"]["kind"] == "ServerError"
    assert error["data"]["hint"]


def test_path_escaping_the_vault_is_refused(client: RpcClient) -> None:
    error = client.error("note.get", path="../../etc/passwd")
    assert error["code"] == ORGANIZE_ERROR
    assert "outside the vault" in error["message"]


def test_suggest_for_note_ranks_the_matching_folder_first(client: RpcClient) -> None:
    suggestions = client.result("suggest.for_note", path=QUIRK_FILES["iso_filename"])
    assert [(s["name"], s["type"], s["score"]) for s in suggestions] == [
        ("health", "areas", 3.7),
        ("blog", "projects", 0.3),
        ("kms", "projects", 0.3),
        ("Archive Now", "archives", 0.1),
    ]
    assert suggestions[0]["reasons"] == [
        "Tag 'health' matches folder",
        "Tag 'health' (normalized) matches",
    ]


def test_suggest_for_note_puts_a_matching_route_above_scored_suggestions(
    client: RpcClient, server: OrganizeServer
) -> None:
    suggestions = client.result("suggest.for_note", path=QUIRK_FILES["question_capture"])
    assert suggestions[0]["route"] == "question"
    assert suggestions[0]["path"] == str(vault_of(server) / "resources/answers")
    assert suggestions[0]["score"] == 1000.0
    assert suggestions[-1]["name"] == "Archive Now"


def test_search_query_returns_matching_records(client: RpcClient) -> None:
    records = client.result("search.query", criteria={"tags": "todo"})
    assert sorted(Path(r["path"]).name for r in records) == [
        "2026-07-01T09:00:00.000Z.md",
        "metadata-list.md",
        "metadata-map.md",
    ]


def test_routes_resolve_reports_the_matching_route(
    client: RpcClient, server: OrganizeServer
) -> None:
    matches = client.result("routes.resolve", path=QUIRK_FILES["question_capture"])
    assert len(matches) == 1
    assert matches[0]["route_name"] == "question"
    assert matches[0]["destination"] == str(vault_of(server) / "resources/answers")
    assert matches[0]["is_folder"] is True
    assert matches[0]["mode"] == "move"
    assert matches[0]["suggestion"]["score"] == 1000.0


def test_routes_resolve_returns_empty_for_an_unrouted_note(client: RpcClient) -> None:
    assert client.result("routes.resolve", path=QUIRK_FILES["current_schema"]) == []


# ---------------------------------------------------------------------------
# Mutating methods — real vault effects (spec 05)
# ---------------------------------------------------------------------------


def test_op_move_copies_tags_archives_original_and_logs(
    client: RpcClient, server: OrganizeServer
) -> None:
    vault = vault_of(server)
    name = "2026-04-08T16:51:24.690160+00:00.md"
    result = client.result(
        "op.move", path=f"capture/raw_capture/{name}", destination="areas/health"
    )
    assert result["ok"] is True
    assert result["operation"] == "move"
    assert result["destination"] == str(vault / "areas/health" / name)

    moved = vault / "areas/health" / name
    assert moved.is_file()
    body = moved.read_text(encoding="utf-8")
    assert "area/health" in body
    assert "processing_status: organized" in body

    assert not (vault / "capture/raw_capture" / name).exists()
    assert (vault / "archive/capture/raw_capture" / name).is_file()

    lines = [
        line
        for line in server.oplog.log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # fileops archives the original before logging the completed move (05 §2.7-8)
    kinds = [parse_log_line(line).type for line in lines]
    assert kinds == ["archive", "move"]
    assert all(parse_log_line(line).success for line in lines)


def test_op_move_records_learning(client: RpcClient, server: OrganizeServer) -> None:
    client.result(
        "op.move",
        path=QUIRK_FILES["iso_filename"],
        destination="areas/health",
    )
    learning = json.loads(server.paths.learning_path.read_text(encoding="utf-8"))
    assert learning["statistics"]["total_moves"] == 1
    destination = str(vault_of(server) / "areas/health")
    assert learning["statistics"]["destinations"] == {destination: 1}


def test_op_move_dry_run_leaves_the_vault_untouched(
    client: RpcClient, server: OrganizeServer
) -> None:
    vault = vault_of(server)
    name = "2026-04-08T16:51:24.690160+00:00.md"
    result = client.result(
        "op.move",
        path=f"capture/raw_capture/{name}",
        destination="areas/health",
        dry_run=True,
    )
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert (vault / "capture/raw_capture" / name).is_file()
    assert not (vault / "areas/health" / name).exists()
    assert not server.paths.learning_path.exists()


def test_op_archive_keeps_the_original_filename(
    client: RpcClient, server: OrganizeServer
) -> None:
    vault = vault_of(server)
    result = client.result("op.archive", path=QUIRK_FILES["context_as_string"])
    assert result["ok"] is True
    assert (vault / "archive/capture/raw_capture/context-string.md").is_file()
    assert not (vault / QUIRK_FILES["context_as_string"]).exists()


def test_op_merge_preview_then_commit(client: RpcClient, server: OrganizeServer) -> None:
    vault = vault_of(server)
    preview = client.result(
        "op.merge_preview",
        path=QUIRK_FILES["current_schema"],
        target=QUIRK_FILES["merge_target"],
    )
    assert "existing idea one" in preview["content"]
    assert "An idea about improv warmups" in preview["content"]
    assert set(preview["snapshot"]) == {"path", "mtime", "sha256"}

    result = client.result(
        "op.merge_commit",
        path=QUIRK_FILES["current_schema"],
        target=QUIRK_FILES["merge_target"],
        content=preview["content"],
        snapshot=preview["snapshot"],
    )
    assert result["ok"] is True
    merged = (vault / QUIRK_FILES["merge_target"]).read_text(encoding="utf-8")
    assert merged.count("existing idea one") == 1
    assert "An idea about improv warmups" in merged
    assert "author: Matt Handzel" in merged, "unknown target fields must survive (05 §9)"
    assert (vault / "archive/capture/raw_capture/2026-06-10T21:33:05.379Z.md").is_file()


def test_op_merge_commit_refuses_a_stale_snapshot(
    client: RpcClient, server: OrganizeServer
) -> None:
    vault = vault_of(server)
    preview = client.result(
        "op.merge_preview",
        path=QUIRK_FILES["current_schema"],
        target=QUIRK_FILES["merge_target"],
    )
    target = vault / QUIRK_FILES["merge_target"]
    target.write_text(
        target.read_text(encoding="utf-8") + "\n- someone else edited this\n", encoding="utf-8"
    )
    error = client.error(
        "op.merge_commit",
        path=QUIRK_FILES["current_schema"],
        target=QUIRK_FILES["merge_target"],
        content=preview["content"],
        snapshot=preview["snapshot"],
    )
    assert error["code"] == ORGANIZE_ERROR
    assert error["data"]["kind"] == "ConcurrentModificationError"


def test_meta_set_updates_frontmatter_in_place(
    client: RpcClient, server: OrganizeServer
) -> None:
    vault = vault_of(server)
    result = client.result(
        "meta.set",
        path=QUIRK_FILES["scalar_tags"],
        changes={"processing_status": "organized"},
    )
    assert result["ok"] is True
    text = (vault / QUIRK_FILES["scalar_tags"]).read_text(encoding="utf-8")
    assert "processing_status: organized" in text
    assert "title: Older manual note" in text


def test_meta_set_rejects_non_object_changes(client: RpcClient) -> None:
    error = client.error("meta.set", path=QUIRK_FILES["scalar_tags"], changes=["nope"])
    assert error["code"] == INVALID_PARAMS


def test_meta_set_refuses_a_no_ai_note_for_an_ai_actor(client: RpcClient) -> None:
    error = client.error(
        "meta.set",
        path=QUIRK_FILES["no_ai"],
        changes={"processing_status": "organized"},
        actor="auto-organize",
    )
    assert error["code"] == ORGANIZE_ERROR
    assert error["data"]["kind"] == "NoAiRefusal"


def test_folder_create_makes_the_directory(client: RpcClient, server: OrganizeServer) -> None:
    result = client.result("folder.create", para_type="projects", name="newsletter")
    assert result["ok"] is True
    assert (vault_of(server) / "projects/newsletter").is_dir()


def test_index_reindex_reports_total_and_duration(client: RpcClient) -> None:
    result = client.result("index.reindex")
    assert result["total"] == 21
    assert isinstance(result["duration"], float)
    assert result["duration"] >= 0.0


# ---------------------------------------------------------------------------
# spec 13 stubs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["auto.propose", "auto.apply"])
def test_auto_methods_answer_the_spec_13_not_implemented_error(
    client: RpcClient, method: str
) -> None:
    error = client.error(method, text="some highlighted text")
    assert error["code"] == NOT_IMPLEMENTED
    assert error["message"] == (
        "auto-organize: not implemented (ships in the automatic-organize phase, spec 13)"
    )
    assert error["data"]["kind"] == "NotImplemented"
    assert error["data"]["spec"] == "13"


# ---------------------------------------------------------------------------
# Concurrency: one writer, many readers (spec 10 §1)
# ---------------------------------------------------------------------------


def test_concurrent_op_move_from_two_connections_never_interleaves(
    server: OrganizeServer,
) -> None:
    vault = vault_of(server)
    first = "2026-04-08T16:51:24.690160+00:00.md"
    second = "context-string.md"
    results: dict[str, Any] = {}
    barrier = threading.Barrier(2, timeout=15.0)

    def move(name: str) -> None:
        with RpcClient(server.socket_path) as conn:
            barrier.wait()
            results[name] = conn.call(
                "op.move",
                path=f"capture/raw_capture/{name}",
                destination="areas/health",
            )

    threads = [threading.Thread(target=move, args=(n,)) for n in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20.0)
        assert not thread.is_alive()

    assert set(results) == {first, second}
    for name, response in results.items():
        assert "error" not in response, (name, response)
        assert response["result"]["ok"] is True
        assert response["apiVersion"] == API_VERSION

    for name in (first, second):
        assert (vault / "areas/health" / name).is_file()
        assert (vault / "archive/capture/raw_capture" / name).is_file()
        assert not (vault / "capture/raw_capture" / name).exists()

    lines = [
        line
        for line in server.oplog.log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    parsed = [parse_log_line(line) for line in lines]
    assert [op.type for op in parsed].count("move") == 2
    assert [op.type for op in parsed].count("archive") == 2
    assert all(op.success for op in parsed), "a torn log line means writes interleaved"
    assert server.writer.completed == 2


def test_read_methods_run_concurrently(
    server: OrganizeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two search.query calls must be in flight at the same instant; if reads
    were serialized behind one another the barrier would time out."""
    barrier = threading.Barrier(2, timeout=5.0)
    original = server.index.query

    def slow_query(criteria: Any) -> Any:
        barrier.wait()
        return original(criteria)

    monkeypatch.setattr(server.index, "query", slow_query)
    outcomes: list[Any] = []

    def run() -> None:
        with RpcClient(server.socket_path) as conn:
            outcomes.append(conn.call("search.query", criteria={"tags": "todo"}))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)
        assert not thread.is_alive()

    assert len(outcomes) == 2
    for response in outcomes:
        assert "error" not in response, response
        assert len(response["result"]) == 3


def test_read_methods_never_enter_the_writer_queue(
    client: RpcClient, server: OrganizeServer
) -> None:
    client.result("session.start")
    client.result("note.get", path=QUIRK_FILES["current_schema"])
    client.result("suggest.for_note", path=QUIRK_FILES["iso_filename"])
    client.result("search.query", criteria={"tags": "todo"})
    client.result("routes.resolve", path=QUIRK_FILES["question_capture"])
    assert server.writer.completed == 0


def test_only_mutating_methods_use_the_writer_queue(
    client: RpcClient, server: OrganizeServer
) -> None:
    client.result("session.start")
    client.result("search.query", criteria={"tags": "todo"})
    assert server.writer.completed == 0
    client.result("index.reindex")
    assert server.writer.completed == 1
    client.result("folder.create", para_type="areas", name="sleep")
    assert server.writer.completed == 2


# ---------------------------------------------------------------------------
# events.subscribe (spec 10 §2)
# ---------------------------------------------------------------------------


def test_subscriber_receives_op_progress_and_index_updated(server: OrganizeServer) -> None:
    with RpcClient(server.socket_path) as subscriber, RpcClient(server.socket_path) as actor:
        assert subscriber.result("events.subscribe") == {
            "subscribed": [EVENT_INDEX_UPDATED, EVENT_OP_PROGRESS],
            "connection_id": 1,
        }
        actor.result("op.archive", path=QUIRK_FILES["context_as_string"])

        events = [subscriber.next_event() for _ in range(3)]

    assert [(e["event"], e["data"].get("phase")) for e in events] == [
        (EVENT_OP_PROGRESS, "start"),
        (EVENT_OP_PROGRESS, "complete"),
        (EVENT_INDEX_UPDATED, None),
    ]
    assert events[0]["data"]["method"] == "op.archive"
    assert events[1]["data"]["result"]["ok"] is True
    assert events[2]["data"]["stats"]["total"] == 21


def test_subscription_is_per_connection(server: OrganizeServer) -> None:
    with RpcClient(server.socket_path) as subscriber, RpcClient(server.socket_path) as quiet:
        subscriber.result("events.subscribe", events=[EVENT_INDEX_UPDATED])
        rid = quiet.send("index.reindex", {})
        response = quiet.read_message()
        assert response["id"] == rid, "an unsubscribed connection must see no event lines"
        event = subscriber.next_event()
        assert event["event"] == EVENT_INDEX_UPDATED
        assert event["data"]["method"] == "index.reindex"


def test_events_subscribe_rejects_an_unknown_event(client: RpcClient) -> None:
    error = client.error("events.subscribe", events=["everything"])
    assert error["code"] == INVALID_PARAMS
    assert "unknown event" in error["message"]


# ---------------------------------------------------------------------------
# Full session lifecycle (spec 10 §2 consequence 1)
# ---------------------------------------------------------------------------


def test_full_session_lifecycle_over_the_socket(
    client: RpcClient, server: OrganizeServer
) -> None:
    """session.start → suggest.for_note → op.move, with real vault effects."""
    vault = vault_of(server)
    session = client.result("session.start")
    capture = next(
        record
        for record in session["captures"]
        if Path(record["path"]).name == "2026-04-08T16:51:24.690160+00:00.md"
    )

    suggestions = client.result("suggest.for_note", path=capture["path"])
    top = suggestions[0]
    assert (top["name"], top["type"], top["score"]) == ("health", "areas", 3.7)

    result = client.result(
        "op.move",
        path=capture["path"],
        destination=top["path"],
        session_id=session["session_id"],
    )
    assert result["ok"] is True

    name = Path(capture["path"]).name
    assert (vault / "areas/health" / name).is_file()
    assert (vault / "archive/capture/raw_capture" / name).is_file()
    assert not (vault / "capture/raw_capture" / name).exists()

    # the warm index reflects the move without an explicit reindex
    assert client.result("note.get", path=f"areas/health/{name}")["record"]["para_type"] == "area"
    remaining = client.result("session.start")["captures"]
    assert len(remaining) == 10

    # and the action corpus recorded it (spec 12 §2)
    months = sorted(server.recorder.actions_dir.glob("*.jsonl"))
    assert months, "every operation records an ActionRecord"
    records = [
        json.loads(line)
        for month in months
        for line in month.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["operation"] for r in records] == ["move"]
    assert records[0]["context"]["session_id"] == session["session_id"]


# ---------------------------------------------------------------------------
# Single instance + idle timeout (spec 10 §1)
# ---------------------------------------------------------------------------


def test_second_serve_attempt_fails_loudly(
    server: OrganizeServer, config: Config, paths: CorePaths, tmp_path: Path
) -> None:
    from organize_core.errors import AlreadyRunning

    second = OrganizeServer(
        config, paths, socket_path=tmp_path / "runtime" / "other.sock", idle_timeout_seconds=0
    )
    with pytest.raises(AlreadyRunning) as excinfo:
        second.serve_forever()
    assert str(paths.lock_path) in str(excinfo.value)
    assert excinfo.value.hint
    # the running server is untouched
    assert server.socket_path.is_socket()
    with RpcClient(server.socket_path) as conn:
        assert conn.handshake == {"apiVersion": API_VERSION}


def test_binding_a_live_socket_is_refused(
    server: OrganizeServer, config: Config, tmp_path: Path
) -> None:
    from organize_core.errors import AlreadyRunning

    other_paths = make_core_paths(tmp_path / "second")
    second = OrganizeServer(
        config, other_paths, socket_path=server.socket_path, idle_timeout_seconds=0
    )
    with pytest.raises(AlreadyRunning) as excinfo:
        second.serve_forever()
    assert "already listening" in str(excinfo.value)
    # the refused starter must NOT have torn down the running server's socket
    assert server.socket_path.is_socket()
    with RpcClient(server.socket_path) as conn:
        assert conn.result("index.reindex")["total"] == 21


def test_idle_timeout_shuts_the_server_down_cleanly(config: Config, tmp_path: Path) -> None:
    paths = make_core_paths(tmp_path)
    srv = OrganizeServer(config, paths, idle_timeout_seconds=0.4)
    thread = start_server(srv)
    try:
        with RpcClient(srv.socket_path) as conn:
            assert conn.result("index.reindex")["total"] == 21
        thread.join(timeout=15.0)
        assert not thread.is_alive(), "the server did not exit after the idle timeout"
    finally:
        srv.shutdown()
        thread.join(timeout=5.0)

    assert not srv.socket_path.exists(), "teardown must remove the socket"
    assert not paths.lock_path.exists(), "teardown must release the lock"
    # and the freed lock lets a fresh server take over
    replacement = OrganizeServer(config, paths, idle_timeout_seconds=0)
    replacement_thread = start_server(replacement)
    try:
        with RpcClient(replacement.socket_path) as conn:
            assert conn.handshake == {"apiVersion": API_VERSION}
    finally:
        replacement.shutdown()
        replacement_thread.join(timeout=10.0)


def test_idle_timeout_does_not_fire_while_requests_keep_arriving(
    config: Config, tmp_path: Path
) -> None:
    paths = make_core_paths(tmp_path)
    srv = OrganizeServer(config, paths, idle_timeout_seconds=0.5)
    thread = start_server(srv)
    try:
        with RpcClient(srv.socket_path) as conn:
            for _ in range(6):
                assert conn.result("search.query", criteria={"tags": "todo"})
                time.sleep(0.15)
            assert thread.is_alive()
    finally:
        srv.shutdown()
        thread.join(timeout=10.0)
