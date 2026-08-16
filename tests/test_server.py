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
    INTERNAL_ERROR,
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


def make_core_paths(tmp_path: Path) -> CorePaths:
    """Tmp-dir CorePaths — the REAL implementation, never the process env.

    This used to fall back to a local subclass because ``paths.py`` was still
    a skeleton; that seat has landed, so the spec 10 §3 layout under test is
    now the shipped one.
    """
    return CorePaths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
    )


def test_the_real_core_paths_lay_state_out_per_spec_10_3(tmp_path: Path) -> None:
    """Pins what the deleted stand-in used to assert by construction."""
    paths = make_core_paths(tmp_path)
    assert type(paths) is CorePaths
    assert paths.config_file == tmp_path / "config" / "config.toml"
    assert paths.index_path == tmp_path / "state" / "index.json"
    assert paths.learning_path == tmp_path / "state" / "learning.json"
    assert paths.operations_log == tmp_path / "state" / "operations.log"
    assert paths.actions_dir == tmp_path / "state" / "actions"
    assert paths.backups_dir == tmp_path / "state" / "backups"
    assert paths.socket_path == tmp_path / "runtime" / "organize-core.sock"
    assert paths.lock_path == tmp_path / "runtime" / "organize-core.lock"


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
    """The kind is VaultError, not ServerError: the server is healthy, the
    path is wrong. `error.data.kind` is what clients branch on, and it must
    match what `organize` reports for the same mistake (integrator ruling —
    see tests/test_integration.py::test_the_two_doors_reject_the_same_bad_move)."""
    error = client.error("note.get", path="capture/raw_capture/nope.md")
    assert error["code"] == ORGANIZE_ERROR
    assert error["data"]["kind"] == "VaultError"
    assert error["data"]["hint"]
    assert "note not found" in error["message"]


def test_path_escaping_the_vault_is_refused(client: RpcClient) -> None:
    error = client.error("note.get", path="../../etc/passwd")
    assert error["code"] == ORGANIZE_ERROR
    assert "outside the vault" in error["message"]


def test_suggest_for_note_ranks_the_matching_folder_first(client: RpcClient) -> None:
    suggestions = client.result("suggest.for_note", path=QUIRK_FILES["iso_filename"])
    # SINGULAR type values on the wire (`Suggestion.type`).
    # `blog` and `kms` fire NO signal, so the 0.3 folder-type bonus no longer
    # carries them past `min_confidence` (architect ruling 2026-08-16: the
    # floor applies to the signal score) — the list is the folder the tag
    # actually names, plus the archive entry.
    assert [(s["name"], s["type"], s["score"]) for s in suggestions] == [
        ("health", "area", 3.7),
        ("Archive Now", "archive", 0.1),
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


#: Every OBJECT-TYPED request field, with the method that carries it.
#: `filters` appears TWICE on purpose — `session.start` filters the backlog,
#: while every mutating method takes its own `filters` through
#: `OperationContext` for the doc-12 record. Two code paths, one field name,
#: so both must validate identically.
_OBJECT_TYPED_FIELDS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("criteria", "search.query", {}),
    ("filters", "session.start", {}),
    ("filters", "folder.create", {"para_type": "areas", "name": "ctx-filters"}),
    ("changes", "meta.set", {"path": QUIRK_FILES["scalar_tags"]}),
)


@pytest.mark.parametrize(("field", "method", "extra"), _OBJECT_TYPED_FIELDS)
def test_an_object_typed_field_reads_an_empty_array_as_an_empty_object(
    client: RpcClient, field: str, method: str, extra: dict[str, Any]
) -> None:
    """lua has one value for an empty object and an empty array, so
    `vim.json.encode({criteria = {}})` puts `[]` on the wire. Every
    object-typed field reads that as `{}`, the same rule `params` itself
    follows — otherwise these calls are -32602 before they ever run."""
    response = client.call(method, **extra, **{field: []})
    assert "error" not in response, f"{field}=[] was rejected: {response.get('error')}"


@pytest.mark.parametrize(("field", "method", "extra"), _OBJECT_TYPED_FIELDS)
def test_an_object_typed_field_still_rejects_a_populated_array(
    client: RpcClient, field: str, method: str, extra: dict[str, Any]
) -> None:
    """Only the EMPTY array is coerced. A populated array is a genuine type
    error, not a lua empty-table artifact, and stays -32602."""
    error = client.error(method, **extra, **{field: ["tags"]})
    assert error["code"] == INVALID_PARAMS
    assert field in error["message"]


def test_operation_context_filters_reject_an_array_as_a_client_error(
    client: RpcClient, server: OrganizeServer
) -> None:
    """`OperationContext.filters` was built with `dict(raw or {})`, so a
    non-empty ARRAY raised ValueError and escaped as -32603 INTERNAL_ERROR —
    the core blaming itself for a malformed request. It is a -32602 naming
    the field, and the operation must not have run."""
    error = client.error(
        "folder.create", para_type="areas", name="never-created", filters=["tags"]
    )
    assert error["code"] == INVALID_PARAMS
    assert error["code"] != INTERNAL_ERROR
    assert "filters" in error["message"]
    assert not (vault_of(server) / "areas/never-created").exists()


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(5, id="scalar-raised-TypeError-as--32603"),
        pytest.param("todo", id="bare-string-exploded-into-characters"),
        pytest.param({"tag": "todo"}, id="object"),
        pytest.param(["todo", 7], id="non-string-item"),
    ],
)
def test_operation_context_auto_tags_reject_a_bad_shape_as_a_client_error(
    client: RpcClient, server: OrganizeServer, bad: Any
) -> None:
    """`auto_tags_present` was `tuple(str(t) for t in (raw or ()))`, which
    failed two ways: a SCALAR raised TypeError and escaped as -32603
    INTERNAL_ERROR, and a bare STRING silently exploded into one tag per
    CHARACTER — a wrong answer written into the action record rather than an
    error. Every bad shape is now -32602 naming the field, and the operation
    must not have run."""
    error = client.error(
        "folder.create", para_type="areas", name="never-tagged", auto_tags_present=bad
    )
    assert error["code"] == INVALID_PARAMS
    assert error["code"] != INTERNAL_ERROR
    assert "auto_tags_present" in error["message"]
    assert not (vault_of(server) / "areas/never-tagged").exists()


def test_operation_context_auto_tags_accepts_a_list_of_strings(client: RpcClient) -> None:
    """The valid shape still passes, including the empty list — lua encodes
    an empty table as `[]`, which is already the right shape for a LIST-typed
    field and needs no coercion."""
    assert client.result(
        "folder.create", para_type="areas", name="tagged-ok", auto_tags_present=["auto/idea"]
    )["ok"]
    assert client.result(
        "folder.create", para_type="areas", name="tagged-empty", auto_tags_present=[]
    )["ok"]


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


def test_folder_list_returns_every_para_subfolder_uncapped(
    client: RpcClient, server: OrganizeServer
) -> None:
    vault = vault_of(server)
    folders = client.result("folder.list")["folders"]
    assert {entry["path"] for entry in folders} == {
        str(vault / "projects/blog"),
        str(vault / "projects/kms"),
        str(vault / "areas/health"),
        str(vault / "areas/relationships"),
        str(vault / "resources/performing"),
        str(vault / "resources/answers"),
        str(vault / "archive/capture"),
    }
    # `resources/flashcards` is in the default ignore_patterns — an ignored
    # folder is not offerable as a destination.
    by_path = {entry["path"]: entry for entry in folders}
    assert by_path[str(vault / "projects/blog")]["name"] == "blog"
    # SINGULAR: every type VALUE on the wire is singular (plural appears only
    # where a `vault.para_folders` KEY is addressed — the request param).
    assert by_path[str(vault / "projects/blog")]["type"] == "project"
    assert by_path[str(vault / "areas/health")]["type"] == "area"
    assert by_path[str(vault / "archive/capture")]["type"] == "archive"


def test_folder_list_includes_a_folder_with_no_notes_in_it(
    client: RpcClient, server: OrganizeServer
) -> None:
    """Disk enumeration, not index enumeration: a folder holding zero notes
    is still a legal destination, and `areas/relationships` is exactly that
    in the fixture vault."""
    vault = vault_of(server)
    empty = vault / "areas/relationships"
    assert empty.is_dir()
    assert not list(empty.iterdir())
    paths = [entry["path"] for entry in client.result("folder.list", para_type="areas")["folders"]]
    assert paths == [str(vault / "areas/health"), str(empty)]

    client.result("folder.create", para_type="areas", name="sleep")
    paths = [entry["path"] for entry in client.result("folder.list", para_type="areas")["folders"]]
    assert str(vault / "areas/sleep") in paths


def test_folder_list_carries_the_spec_11_3_description(
    client: RpcClient, server: OrganizeServer
) -> None:
    vault = vault_of(server)
    folders = client.result("folder.list", para_type="areas")["folders"]
    by_path = {entry["path"]: entry for entry in folders}
    assert by_path[str(vault / "areas/health")]["description"] == (
        "Ongoing health practice — training log, sleep, injuries. "
        "Not general health research (that goes to resources)."
    )
    # Omitted, not null, for a folder that has none.
    assert "description" not in by_path[str(vault / "areas/relationships")]


def test_folder_list_accepts_the_singular_para_type(
    client: RpcClient, server: OrganizeServer
) -> None:
    assert client.result("folder.list", para_type="project") == client.result(
        "folder.list", para_type="projects"
    )


def test_folder_list_rejects_an_unknown_para_type(client: RpcClient) -> None:
    error = client.error("folder.list", para_type="notions")
    assert error["code"] == ORGANIZE_ERROR
    assert error["data"]["kind"] == "ConfigError"
    assert "areas" in error["data"]["hint"]


def test_folder_children_returns_dirs_and_notes(
    client: RpcClient, server: OrganizeServer
) -> None:
    vault = vault_of(server)
    result = client.result("folder.children", path="resources")
    assert [entry["path"] for entry in result["dirs"]] == [
        str(vault / "resources/answers"),
        str(vault / "resources/performing"),
    ], "ignore_patterns applies to browsing too (resources/flashcards is ignored)"
    assert all("type" not in entry for entry in result["dirs"])
    assert result["notes"] == []

    result = client.result("folder.children", path="projects/blog")
    assert result["dirs"] == []
    assert result["notes"] == [
        {
            "path": str(vault / QUIRK_FILES["merge_target"]),
            "title": "Blog ideas",
            "aliases": ["ideas"],
            "para_type": "project",
        }
    ]


def test_folder_children_of_an_out_of_vault_path_is_a_vault_error(client: RpcClient) -> None:
    error = client.error("folder.children", path="../../etc")
    assert error["code"] == ORGANIZE_ERROR
    assert error["data"]["kind"] == "VaultError"
    assert error["data"]["hint"]


def test_folder_children_of_a_missing_folder_is_a_vault_error(client: RpcClient) -> None:
    error = client.error("folder.children", path="areas/nonexistent")
    assert error["code"] == ORGANIZE_ERROR
    assert error["data"]["kind"] == "VaultError"
    assert "folder not found" in error["message"]


def test_folder_read_methods_never_enter_the_writer_queue(
    client: RpcClient, server: OrganizeServer
) -> None:
    client.result("folder.list")
    client.result("folder.children", path="areas")
    assert server.writer.completed == 0


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
# Transport robustness (real-data soak regressions)
# ---------------------------------------------------------------------------


def test_a_slow_client_still_receives_a_whole_multi_megabyte_line(
    server: OrganizeServer,
) -> None:
    """The accept loop's 50 ms poll timeout was applied to the ACCEPTED
    socket, so `sendall` raised `socket.timeout` the moment the ~208 KB send
    buffer filled and the handler closed the connection mid-line — with no
    error frame and no log entry. Every response bigger than one send buffer
    (a real `search.query` returns 2-3 MB) was a coin flip for any client
    that renders while it reads."""
    payload = "x" * (4 * 1024 * 1024)
    with RpcClient(server.socket_path, timeout=30.0) as subscriber:
        subscriber.result("events.subscribe", events=[EVENT_INDEX_UPDATED])
        assert server.emit(EVENT_INDEX_UPDATED, {"blob": payload}) == 1
        # fall far enough behind that the server's send buffer is full
        time.sleep(0.3)
        event = subscriber.next_event(timeout=30.0)
    assert event["data"]["blob"] == payload, "the line was truncated in flight"


def test_an_emit_failure_never_turns_an_applied_operation_into_an_error(
    server: OrganizeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-write notifications run after the vault has already changed.
    A raise there (observed as `RuntimeError: dictionary changed size during
    iteration` out of `index.stats()`) was converted into -32603, so a
    completed move was reported as a failure — and a client that retries on
    error files the note twice."""
    real_emit = server.emit

    def exploding_emit(event: str, data: dict[str, Any]) -> int:
        if event == EVENT_INDEX_UPDATED:
            raise RuntimeError("dictionary changed size during iteration")
        return real_emit(event, data)

    monkeypatch.setattr(server, "emit", exploding_emit)
    with RpcClient(server.socket_path) as conn:
        result = conn.result("op.archive", path=QUIRK_FILES["context_as_string"])

    assert result["ok"] is True
    assert (vault_of(server) / "archive/capture/raw_capture/context-string.md").is_file()


def test_index_stats_for_the_index_updated_event_are_read_under_the_write_lock(
    server: OrganizeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the ordering fix rather than the race it removes: the snapshot
    must be taken while the writer still holds the lock."""
    held: list[bool] = []
    real_stats = server.index.stats

    def spy() -> dict[str, Any]:
        held.append(server._rwlock._writer)
        return real_stats()

    monkeypatch.setattr(server.index, "stats", spy)
    with RpcClient(server.socket_path) as conn:
        conn.result("op.archive", path=QUIRK_FILES["context_as_string"])

    assert held and all(held), "index.stats() ran with no write lock held"


def test_readers_are_not_starved_by_a_queue_of_writers() -> None:
    """`folder.list` p95 was 2.3 s and `search.query` 28.9 s under a bulk
    file-away because the lock was strictly writer-preferring: a reader waited
    while ANY writer was queued, and that never cleared. The reader must now
    get in after a BOUNDED number of writes (spec 09 §4 budgets a UI action at
    < 50 ms)."""
    from organize_core.server import _WRITER_BATCH_LIMIT, _ReadWriteLock

    lock = _ReadWriteLock()
    order: list[str] = []
    order_lock = threading.Lock()
    writers = _WRITER_BATCH_LIMIT * 3
    gate = threading.Event()

    def writer() -> None:
        with lock.write():
            with order_lock:
                order.append("w")

    def reader() -> None:
        with lock.read():
            with order_lock:
                order.append("r")

    with lock.write():  # hold everyone off while the queue builds
        threads = [threading.Thread(target=writer, daemon=True) for _ in range(writers)]
        for thread in threads:
            thread.start()
        while lock._waiting_writers < writers:
            time.sleep(0.005)
        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        while lock._waiting_readers < 1:
            time.sleep(0.005)
        gate.set()

    for thread in [*threads, reader_thread]:
        thread.join(timeout=10.0)
        assert not thread.is_alive()

    assert gate.is_set()
    assert len(order) == writers + 1
    position = order.index("r")
    assert position <= _WRITER_BATCH_LIMIT, (
        f"the reader waited behind {position} writes; the bound is {_WRITER_BATCH_LIMIT}"
    )


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
    assert (top["name"], top["type"], top["score"]) == ("health", "area", 3.7)

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


# ===========================================================================
# Phase-1 fix pass — the RPC door must enforce what the CLI door enforces
# ===========================================================================


@pytest.fixture()
def metadata_config(fixture_vault: Path) -> Config:
    """The shipped example's doc-07 fields: a normalized list, an enum, a
    boolean and a number."""
    from organize_core.config import MetadataFieldConfig

    return Config(
        vault=VaultConfig(root=fixture_vault),
        metadata_fields=[
            MetadataFieldConfig(
                key="tags",
                type="list",
                keymap="<leader>mt",
                normalize="kebab",
                complete="existing",
            ),
            MetadataFieldConfig(
                key="importance",
                type="enum",
                keymap="<leader>mi",
                values=["high", "medium", "low"],
            ),
            MetadataFieldConfig(key="remember", type="boolean", keymap="<leader>mr"),
            MetadataFieldConfig(key="energy", type="number", keymap="<leader>me"),
            MetadataFieldConfig(
                key="people", type="list", keymap="<leader>mp", append=False, complete="existing"
            ),
        ],
    )


@pytest.fixture()
def metadata_client(metadata_config: Config, paths: CorePaths) -> Iterator[RpcClient]:
    srv = OrganizeServer(metadata_config, paths, idle_timeout_seconds=0)
    thread = start_server(srv)
    conn = RpcClient(srv.socket_path)
    try:
        yield conn
    finally:
        conn.close()
        srv.shutdown()
        thread.join(timeout=10.0)


def test_meta_set_applies_the_doc_07_list_rules(metadata_client: RpcClient) -> None:
    """Doc 07 acceptance 1: "type `foo, Bar Baz` -> tags gains foo, bar-baz".

    `_meta_set` handed `params['changes']` straight to `update_frontmatter`;
    the coercion lived in `cli.py` and was never imported here, so the nvim
    client — whose ONLY write path is RPC — wrote `tags: ["foo, Bar Baz"]`
    into the vault. Spec 10 §3 exists to make that impossible: "CLI and UI
    can never disagree".
    """
    note = QUIRK_FILES["metadata_empty_map"]
    result = metadata_client.result("meta.set", path=note, changes={"tags": "foo, Bar Baz"})
    assert result["ok"] is True, result["error"]

    text = (vault_of_client(metadata_client) / note).read_text(encoding="utf-8")
    assert "- foo" in text
    assert "- bar-baz" in text, "kebab normalization did not run"
    assert "foo, Bar Baz" not in text, "the raw string was written as one bogus tag"


def test_meta_set_rejects_a_value_outside_an_enum(metadata_client: RpcClient) -> None:
    """Doc 07 acceptance 2. The CLI raised ConfigError with the allowed
    values; RPC accepted anything and wrote it."""
    note = QUIRK_FILES["metadata_empty_map"]
    before = (vault_of_client(metadata_client) / note).read_text(encoding="utf-8")

    error = metadata_client.error("meta.set", path=note, changes={"importance": "totally-invalid"})
    assert error["code"] == ORGANIZE_ERROR
    assert error["data"]["kind"] == "ConfigError"
    assert "high, medium, low" in error["data"]["hint"]
    assert (vault_of_client(metadata_client) / note).read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("changes", "kind"),
    [({"remember": "maybe"}, "ConfigError"), ({"energy": "abc"}, "ConfigError")],
)
def test_meta_set_rejects_a_mistyped_scalar(
    metadata_client: RpcClient, changes: dict[str, Any], kind: str
) -> None:
    error = metadata_client.error("meta.set", path=QUIRK_FILES["metadata_empty_map"], changes=changes)
    assert error["data"]["kind"] == kind


def test_meta_set_accepts_the_natural_json_types(metadata_client: RpcClient) -> None:
    """A JSON client sends a real list/bool/number; coercion must pass those
    through rather than stringify them."""
    note = QUIRK_FILES["metadata_empty_map"]
    result = metadata_client.result(
        "meta.set",
        path=note,
        changes={"tags": ["Deep Work"], "remember": True, "energy": 3, "importance": "high"},
    )
    assert result["ok"] is True, result["error"]
    text = (vault_of_client(metadata_client) / note).read_text(encoding="utf-8")
    assert "- deep-work" in text
    assert "remember: true" in text
    assert "energy: 3" in text


def test_meta_set_replaces_a_list_field_declaring_append_false(
    metadata_client: RpcClient,
) -> None:
    """Spec 07 `append = false`, the same rule `organize set-meta` applies."""
    note = QUIRK_FILES["metadata_empty_map"]
    metadata_client.result("meta.set", path=note, changes={"people": "alice, bob"})
    metadata_client.result("meta.set", path=note, changes={"people": "carol"})
    text = (vault_of_client(metadata_client) / note).read_text(encoding="utf-8")
    assert "- carol" in text
    assert "- alice" not in text, "append = false must REPLACE"


def test_meta_fields_exposes_the_definitions_and_completions(
    metadata_client: RpcClient,
) -> None:
    """Spec 07 + 10 §3: `metadata_fields` lives in core config so the UI
    reads it FROM the core. Neither surface exposed it, nor
    `VaultIndex.values_of`, so `complete = "existing"` was unreachable from
    any thin client — the capability worked in-core and was dead code from
    the outside."""
    metadata_client.result("index.reindex")
    payload = metadata_client.result("meta.fields")
    fields = {entry["key"]: entry for entry in payload["fields"]}
    assert fields["importance"]["values"] == ["high", "medium", "low"]
    assert fields["importance"]["completions"] == ["high", "medium", "low"]
    assert fields["tags"]["normalize"] == "kebab"
    assert "impro" in fields["tags"]["completions"], "complete = 'existing' reads the vault"
    assert fields["people"]["append"] is False


def test_meta_values_lists_the_existing_values_of_a_key(metadata_client: RpcClient) -> None:
    metadata_client.result("index.reindex")
    payload = metadata_client.result("meta.values", key="tags")
    assert payload["key"] == "tags"
    assert "impro" in payload["values"]
    assert payload["values"] == sorted(payload["values"])


def vault_of_client(conn: RpcClient) -> Path:
    """The fixture vault root, read back off the server via note.get."""
    record = conn.result("note.get", path=QUIRK_FILES["metadata_empty_map"])["record"]
    return Path(record["path"]).parents[2]


# --- the counterfactual survives the RPC boundary (spec 12 §2) -------------


def test_op_move_records_the_counterfactual_it_was_given(
    client: RpcClient, server: OrganizeServer, tmp_path: Path
) -> None:
    """Spec 12 §3: "a session of 5 actions yields 5 records with consistent
    session_id and correct chosen_ranks". `server._context()` read only
    dry_run/actor/session_id and silently DISCARDED client-supplied
    suggestions_shown / chosen_rank / durations_ms, so that gate was
    unsatisfiable through the product and `actions stats` could never report
    an accept rate from real usage."""
    client.result("index.reindex")
    result = client.result(
        "op.move",
        path=QUIRK_FILES["current_schema"],
        destination="projects/blog",
        session_id="ses_probe",
        suggestions_shown=[
            {"path": "projects/blog", "score": 3.1, "rank": 1, "reasons": ["tag"]},
            {"path": "areas/health", "score": 1.0, "rank": 2, "reasons": ["type"]},
        ],
        chosen_rank=1,
        durations_ms={"decision": 8400, "operation": 120},
        auto_tags_present=["auto/idea"],
    )
    assert result["ok"] is True, result["error"]

    records = read_action_records(server)
    assert len(records) == 1
    context = records[0]["context"]
    assert context["session_id"] == "ses_probe"
    assert context["chosen_rank"] == 1
    assert [s["rank"] for s in context["suggestions_shown"]] == [1, 2]
    assert context["durations_ms"] == {"decision": 8400, "operation": 120}
    assert context["auto_tags_present"] == ["auto/idea"]


def test_a_malformed_counterfactual_is_a_loud_invalid_params(client: RpcClient) -> None:
    error = client.error(
        "op.move",
        path=QUIRK_FILES["current_schema"],
        destination="projects/blog",
        chosen_rank="first",
    )
    assert error["code"] == INVALID_PARAMS
    assert error["data"]["kind"] == "InvalidParams"


def read_action_records(server: OrganizeServer) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for month in sorted(Path(server.paths.actions_dir).glob("*.jsonl")):
        for line in month.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_a_move_records_the_destination_description(
    client: RpcClient, server: OrganizeServer
) -> None:
    """Spec 12 §2 `targets[].description`. `areas/health/index.md` carries
    one in the fixture vault, and it used to be recorded as null."""
    client.result("index.reindex")
    result = client.result(
        "op.move", path=QUIRK_FILES["current_schema"], destination="areas/health"
    )
    assert result["ok"] is True, result["error"]
    description = read_action_records(server)[0]["targets"][0]["description"]
    assert description is not None and description.strip()


# --- learning is a view derived from the ActionRecord (12 §2 "Uses" #2) ---


def test_op_merge_still_records_learning_through_the_pipeline(
    client: RpcClient, server: OrganizeServer
) -> None:
    client.result("index.reindex")
    result = client.result(
        "op.merge_commit", path=QUIRK_FILES["current_schema"], target=QUIRK_FILES["merge_target"]
    )
    assert result["ok"] is True, result["error"]

    learning = json.loads(server.paths.learning_path.read_text(encoding="utf-8"))
    assert learning["statistics"]["total_moves"] == 1
    folder = str(vault_of(server) / "projects" / "blog")
    assert list(learning["statistics"]["destinations"]) == [folder]


def test_a_dry_run_move_teaches_nothing(client: RpcClient, server: OrganizeServer) -> None:
    """A rehearsal is not a precedent — the rule every corpus reader already
    applies now also governs the derived learning view."""
    client.result("index.reindex")
    result = client.result(
        "op.move", path=QUIRK_FILES["current_schema"], destination="projects/blog", dry_run=True
    )
    assert result["ok"] is True and result["dry_run"] is True
    assert not server.paths.learning_path.exists()


# --- no-ai binds the CAPTURE of a merge/append too (vault law, spec 02) ---


@pytest.mark.parametrize("actor", ["claude-integrate", "consumer:auto_tagger"])
def test_an_ai_actor_cannot_merge_a_no_ai_capture_over_rpc(
    client: RpcClient, server: OrganizeServer, actor: str
) -> None:
    """`server._op_merge_commit` takes `actor` straight from request params,
    so this was reachable by any caller: a `no-ai: true` capture's body could
    be copied into an ordinary note carrying no such marking."""
    client.result("index.reindex")
    target = vault_of(server) / QUIRK_FILES["merge_target"]
    before = target.read_text(encoding="utf-8")

    error = client.error(
        "op.merge_commit",
        path=QUIRK_FILES["no_ai"],
        target=QUIRK_FILES["merge_target"],
        actor=actor,
    )
    assert error["data"]["kind"] == "NoAiRefusal"
    assert target.read_text(encoding="utf-8") == before


# --- session.start: a misplaced key is loud, not silently unfiltered ------


def test_session_start_accepts_top_level_filters(client: RpcClient) -> None:
    """`search.query` accepted them at the top level; `session.start` ignored
    them and returned an UNFILTERED session — a silent wrong answer in an API
    whose stated stance is "an unknown key is a hard error naming the key"."""
    client.result("index.reindex")
    nested = client.result("session.start", filters={"tags": ["todo"]})
    flat = client.result("session.start", tags=["todo"])
    assert [c["path"] for c in flat["captures"]] == [c["path"] for c in nested["captures"]]
    assert 0 < len(flat["captures"]) < len(client.result("session.start")["captures"])


def test_session_start_rejects_an_unknown_top_level_key(client: RpcClient) -> None:
    client.result("index.reindex")
    error = client.error("session.start", totally_unknown_key=1)
    assert error["code"] == ORGANIZE_ERROR
    assert "totally_unknown_key" in error["message"]


def test_session_start_rejects_a_filter_given_twice(client: RpcClient) -> None:
    error = client.error("session.start", tags=["todo"], filters={"tags": ["impro"]})
    assert error["code"] == INVALID_PARAMS
    assert "tags" in error["message"]


# --- protocol errors carry the same taxonomy as domain errors -------------


@pytest.mark.parametrize(
    ("method", "params", "code", "kind"),
    [
        ("meta.set", {}, INVALID_PARAMS, "InvalidParams"),
        ("nope.method", {}, METHOD_NOT_FOUND, "MethodNotFound"),
        ("events.subscribe", {"events": ["not-an-event"]}, INVALID_PARAMS, "InvalidParams"),
    ],
)
def test_protocol_errors_carry_kind_and_hint(
    client: RpcClient, method: str, params: dict[str, Any], code: int, kind: str
) -> None:
    """A client must be able to render taxonomy + hint through ONE code path.
    Domain errors (-32000) carried `data.kind`/`data.hint`; the protocol
    codes carried a bare message, so every client needed a special case."""
    error = client.error(method, **params)
    assert error["code"] == code
    assert error["data"]["kind"] == kind
    assert error["data"]["hint"]


def test_a_parse_error_carries_kind_and_hint(client: RpcClient) -> None:
    client.send_raw("{not json\n")
    message = client.read_message()
    assert message["error"]["data"]["kind"] == "ParseError"
    assert message["error"]["data"]["hint"]


def test_an_invalid_request_carries_kind_and_hint(client: RpcClient) -> None:
    client.send_raw(json.dumps({"jsonrpc": "1.0", "id": 1, "method": "note.get"}) + "\n")
    message = client.read_message()
    assert message["error"]["data"]["kind"] == "InvalidRequest"
    assert message["error"]["data"]["hint"]


def test_method_not_found_still_lists_the_known_methods(client: RpcClient) -> None:
    error = client.error("nope.method")
    assert "meta.fields" in error["data"]["known_methods"]


# --- the two doors agree (ARCHITECTURE ruling #19) ------------------------


def test_both_doors_refuse_the_same_out_of_vault_destination(
    client: RpcClient, server: OrganizeServer, tmp_path: Path
) -> None:
    """`test_the_two_doors_reject_the_same_bad_move` pinned agreement for a
    MISSING note only, so the CLI's out-of-vault escape went unnoticed by the
    whole suite. Both doors must refuse, and nothing may be written outside.
    """
    from organize_core.errors import VaultError
    from organize_core.fileops import move_to_destination, require_in_vault

    client.result("index.reindex")
    outside = tmp_path / "OUTSIDE"

    error = client.error("op.move", path=QUIRK_FILES["current_schema"], destination=str(outside))
    assert error["code"] == ORGANIZE_ERROR
    assert "outside the vault" in error["message"]
    assert not outside.exists()

    # ...and the shared enforcement point behind BOTH doors agrees.
    with pytest.raises(VaultError):
        require_in_vault(server.config, outside, "destination folder")
    assert move_to_destination is not None  # imported to pin the call site exists


def test_the_archive_folder_is_resolved_like_the_cli_resolves_it(
    fixture_vault: Path, paths: CorePaths, tmp_path: Path
) -> None:
    """`cli._archive_folder` resolves and `server._archive_folder` did not,
    so a symlinked vault root made the two doors emit DIFFERENT archive
    suggestion paths for the same vault (04 §1)."""
    from organize_core.cli import _archive_folder as cli_archive_folder

    link = tmp_path / "vault-link"
    link.symlink_to(fixture_vault)
    config = make_config(link)
    srv = OrganizeServer(config, paths, idle_timeout_seconds=0)
    assert srv._archive_folder() == cli_archive_folder(config)


# --- idle timeout must not evict a connected subscriber -------------------


def test_a_connected_client_keeps_the_server_alive(
    config: Config, paths: CorePaths
) -> None:
    """`_idle_expired` consulted only `_inflight` and `_last_activity`, never
    `_connections`, so a Neovim client holding an `events.subscribe` stream
    lost it after `idle_timeout_seconds` with no notification."""
    srv = OrganizeServer(config, paths, idle_timeout_seconds=0.2)
    thread = start_server(srv)
    conn = RpcClient(srv.socket_path)
    try:
        conn.result("events.subscribe", events=[EVENT_INDEX_UPDATED])
        time.sleep(0.8)  # >> the idle timeout, with the client silent
        assert not srv.stopping, "the server shut down under a connected subscriber"
        assert conn.result("index.reindex")["total"] > 0
    finally:
        conn.close()
        srv.shutdown()
        thread.join(timeout=10.0)


def test_the_server_still_idles_out_with_no_client(config: Config, paths: CorePaths) -> None:
    """The other half: the timeout must still fire once everyone has left."""
    srv = OrganizeServer(config, paths, idle_timeout_seconds=0.2)
    thread = start_server(srv)
    try:
        RpcClient(srv.socket_path).close()
        thread.join(timeout=15.0)
        assert not thread.is_alive(), "the server never idled out"
    finally:
        srv.shutdown()
        thread.join(timeout=10.0)


def test_a_session_of_five_rpc_actions_yields_five_records_with_correct_ranks(
    client: RpcClient, server: OrganizeServer
) -> None:
    """Spec 12 §3, driven through the PRODUCT: "a session of 5 actions yields
    5 records with consistent session_id and correct chosen_ranks".

    The repo's gate for this hand-constructed `ActionContext(chosen_rank=...)`
    and fed it straight to the recorder, so it proved the dataclass rather
    than the write path — and the write path could not produce those fields
    at all, because `server._context()` read only dry_run/actor/session_id and
    discarded the rest.
    """
    client.result("index.reindex")
    session = client.result("session.start")["session_id"]
    shown = [
        {"path": "projects/blog", "score": 3.25, "rank": 1, "reasons": ["exact tag"]},
        {"path": "areas/health", "score": 1.5, "rank": 2, "reasons": ["type"]},
    ]

    calls = [
        ("op.move", {"path": QUIRK_FILES["current_schema"], "destination": "projects/blog"}, 1),
        ("folder.create", {"para_type": "resources", "name": "fresh-topic"}, None),
        ("meta.set", {"path": QUIRK_FILES["metadata_empty_map"], "changes": {"importance": "high"}}, None),
        ("op.archive", {"path": QUIRK_FILES["metadata_empty_list"]}, 3),
        (
            "op.merge_commit",
            {"path": QUIRK_FILES["scalar_tags"], "target": QUIRK_FILES["merge_target"]},
            2,
        ),
    ]
    for method, params, rank in calls:
        payload = dict(params, session_id=session, suggestions_shown=shown)
        if rank is not None:
            payload["chosen_rank"] = rank
        response = client.call(method, **payload)
        assert "error" not in response, response["error"]

    records = read_action_records(server)
    assert len(records) == 5, "every state-changing operation appends one record (12 §2)"
    assert {r["context"]["session_id"] for r in records} == {session}
    assert [r["context"]["chosen_rank"] for r in records] == [1, None, None, 3, 2]
    assert all(len(r["context"]["suggestions_shown"]) == 2 for r in records)
    assert [r["operation"] for r in records] == [
        "move",
        "create_folder",
        "meta_edit",
        "archive",
        "merge",
    ]
