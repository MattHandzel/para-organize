"""Unit suite for the server's protocol layer and concurrency primitives.

No socket, no vault mutation: exact-value assertions on the wire format
(spec 10 §2), the single-instance lock, the writer queue and the
reader/writer lock that keep the warm index consistent (spec 10 §1).

The socket-level behaviour these primitives add up to lives in
``tests/test_server.py``.
"""

from __future__ import annotations

import ast
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from organize_core import API_VERSION
from organize_core.errors import AlreadyRunning, ServerError, VaultError
from organize_core.paths import CorePaths
from organize_core.server import (
    EVENT_INDEX_UPDATED,
    EVENT_OP_PROGRESS,
    EVENT_TYPES,
    INDEX_CHANGING_METHODS,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    MUTATING_METHODS,
    NOT_IMPLEMENTED,
    ORGANIZE_ERROR,
    PARSE_ERROR,
    RPC_METHODS,
    OrganizeServer,
    RpcError,
    RpcException,
    SingleInstanceLock,
    WriterQueue,
    _organize_error,
    _ReadWriteLock,
    decode_request,
    encode_event,
    encode_response,
    handshake_line,
)
from test_server import make_config, make_core_paths

SERVER_SOURCE = Path(__file__).resolve().parents[1] / "src" / "organize_core" / "server.py"


# ---------------------------------------------------------------------------
# The method table IS the API contract (spec 10 §2)
# ---------------------------------------------------------------------------


#: The names spec 10 §2 enumerates, in the order it enumerates them.
SPEC_10_METHODS: tuple[str, ...] = (
    "session.start",
    "note.get",
    "suggest.for_note",
    "op.move",
    "op.merge_preview",
    "op.merge_commit",
    "op.archive",
    "meta.set",
    "folder.create",
    "index.reindex",
    "search.query",
    "routes.resolve",
    "auto.propose",
    "auto.apply",
    "events.subscribe",
)

#: Read-only additions beyond spec 10 §2's list. Spec 07 calls completion
#: "what makes tag entry fast and consistent" and spec 10 §3 puts
#: `metadata_fields` in CORE config so the UI reads it from the core — but
#: neither surface exposed the definitions or `VaultIndex.values_of`, so the
#: doc-07 feature was unreachable from any thin client. `folder.list` /
#: `folder.children` are the same shape of gap for the SAME reason: spec 10
#: §1 forbids a thin client from walking the vault itself, so without them
#: the destination picker and the 03 §3 browse view have no source for the
#: folder tree (`VaultIndex.para_subfolders`/`folder_children` had no RPC
#: surface). Anything else added here fails this test, which is the point.
EXTRA_METHODS: frozenset[str] = frozenset(
    {"meta.fields", "meta.values", "folder.list", "folder.children"}
)


def test_rpc_methods_are_exactly_the_spec_10_names_in_order() -> None:
    assert set(SPEC_10_METHODS) <= set(RPC_METHODS)
    ordered = [name for name in RPC_METHODS if name in set(SPEC_10_METHODS)]
    assert tuple(ordered) == SPEC_10_METHODS
    assert set(RPC_METHODS) - set(SPEC_10_METHODS) == EXTRA_METHODS
    assert len(RPC_METHODS) == len(SPEC_10_METHODS) + len(EXTRA_METHODS)
    assert len(set(RPC_METHODS)) == len(RPC_METHODS)


def test_mutating_methods_are_exactly_the_dispatch_contract() -> None:
    assert MUTATING_METHODS == frozenset(
        {
            "op.move",
            "op.merge_preview",
            "op.merge_commit",
            "op.archive",
            "meta.set",
            "folder.create",
            "index.reindex",
            "auto.apply",
        }
    )
    assert MUTATING_METHODS <= set(RPC_METHODS)


def test_every_rpc_method_has_a_handler(fixture_vault: Path, tmp_path: Path) -> None:
    server = OrganizeServer(make_config(fixture_vault), make_core_paths(tmp_path))
    assert set(server._handlers) == set(RPC_METHODS)


def test_error_codes_are_the_json_rpc_standard_values() -> None:
    assert (PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR) == (
        -32700,
        -32600,
        -32601,
        -32602,
        -32603,
    )
    assert ORGANIZE_ERROR == -32000
    assert -32099 <= NOT_IMPLEMENTED <= -32000
    assert NOT_IMPLEMENTED != ORGANIZE_ERROR


def test_event_types_are_the_two_spec_10_streams() -> None:
    assert EVENT_TYPES == (EVENT_INDEX_UPDATED, EVENT_OP_PROGRESS)
    assert EVENT_INDEX_UPDATED == "index-updated"
    assert EVENT_OP_PROGRESS == "op-progress"


# ---------------------------------------------------------------------------
# encode_response / encode_event / handshake
# ---------------------------------------------------------------------------


def test_handshake_line_is_exactly_the_api_version_object() -> None:
    assert handshake_line() == '{"apiVersion": 1}\n'
    assert json.loads(handshake_line()) == {"apiVersion": API_VERSION}


def test_encode_response_result_envelope() -> None:
    line = encode_response(7, result={"total": 3})
    assert line.endswith("\n")
    assert line.count("\n") == 1, "newline-delimited: exactly one line per response"
    assert json.loads(line) == {
        "jsonrpc": "2.0",
        "apiVersion": API_VERSION,
        "id": 7,
        "result": {"total": 3},
    }


def test_encode_response_error_envelope_carries_data() -> None:
    line = encode_response(
        "abc", error=RpcError(ORGANIZE_ERROR, "boom", {"kind": "VaultError", "hint": "fix it"})
    )
    assert json.loads(line) == {
        "jsonrpc": "2.0",
        "apiVersion": API_VERSION,
        "id": "abc",
        "error": {
            "code": ORGANIZE_ERROR,
            "message": "boom",
            "data": {"kind": "VaultError", "hint": "fix it"},
        },
    }


def test_encode_response_omits_data_when_absent() -> None:
    payload = json.loads(encode_response(None, error=RpcError(PARSE_ERROR, "bad json")))
    assert payload["error"] == {"code": PARSE_ERROR, "message": "bad json"}
    assert payload["id"] is None


def test_encode_response_serializes_paths_and_dataclasses() -> None:
    from organize_core.fileops import FileSnapshot

    payload = json.loads(
        encode_response(
            1,
            result={
                "path": Path("/vault/notes/a.md"),
                "snapshot": FileSnapshot(path="/vault/a.md", mtime=1.5, sha256="deadbeef"),
            },
        )
    )
    assert payload["result"]["path"] == "/vault/notes/a.md"
    assert payload["result"]["snapshot"] == {
        "path": "/vault/a.md",
        "mtime": 1.5,
        "sha256": "deadbeef",
    }


def test_encode_response_keeps_unicode_readable() -> None:
    payload = encode_response(1, result={"name": "café ☕"})
    assert "café ☕" in payload


def test_encode_event_is_a_notification_with_no_id() -> None:
    payload = json.loads(encode_event(EVENT_INDEX_UPDATED, {"total": 21}))
    assert payload == {
        "jsonrpc": "2.0",
        "apiVersion": API_VERSION,
        "method": "event",
        "params": {"event": EVENT_INDEX_UPDATED, "data": {"total": 21}},
    }
    assert "id" not in payload


# ---------------------------------------------------------------------------
# decode_request
# ---------------------------------------------------------------------------


def test_decode_request_returns_id_method_params() -> None:
    line = json.dumps(
        {"jsonrpc": "2.0", "id": 4, "method": "op.move", "params": {"path": "a.md"}}
    )
    assert decode_request(line) == (4, "op.move", {"path": "a.md"})


def test_decode_request_defaults_missing_params_to_empty() -> None:
    assert decode_request('{"jsonrpc": "2.0", "id": 1, "method": "index.reindex"}') == (
        1,
        "index.reindex",
        {},
    )
    assert decode_request(
        '{"jsonrpc": "2.0", "id": 1, "method": "index.reindex", "params": null}'
    ) == (1, "index.reindex", {})


def test_decode_request_treats_a_missing_id_as_a_notification() -> None:
    request_id, method, _ = decode_request('{"jsonrpc": "2.0", "method": "index.reindex"}')
    assert request_id is None
    assert method == "index.reindex"


def test_decode_request_rejects_invalid_json() -> None:
    with pytest.raises(RpcException) as excinfo:
        decode_request("{not json")
    assert excinfo.value.error.code == PARSE_ERROR


def test_decode_request_rejects_a_non_object_payload() -> None:
    with pytest.raises(RpcException) as excinfo:
        decode_request("[1, 2, 3]")
    assert excinfo.value.error.code == INVALID_REQUEST


def test_decode_request_rejects_the_wrong_jsonrpc_version() -> None:
    with pytest.raises(RpcException) as excinfo:
        decode_request('{"jsonrpc": "1.0", "id": 2, "method": "note.get"}')
    assert excinfo.value.error.code == INVALID_REQUEST
    assert excinfo.value.request_id == 2
    assert "2.0" in excinfo.value.error.message


def test_decode_request_rejects_a_missing_method() -> None:
    with pytest.raises(RpcException) as excinfo:
        decode_request('{"jsonrpc": "2.0", "id": 3}')
    assert excinfo.value.error.code == INVALID_REQUEST
    assert excinfo.value.request_id == 3


def test_decode_request_rejects_positional_params() -> None:
    with pytest.raises(RpcException) as excinfo:
        decode_request('{"jsonrpc": "2.0", "id": 3, "method": "note.get", "params": ["a.md"]}')
    assert excinfo.value.error.code == INVALID_PARAMS
    assert excinfo.value.request_id == 3


def test_decode_request_rejects_a_boolean_id() -> None:
    with pytest.raises(RpcException) as excinfo:
        decode_request('{"jsonrpc": "2.0", "id": true, "method": "note.get"}')
    assert excinfo.value.error.code == INVALID_REQUEST


def test_decode_request_accepts_a_matching_api_version() -> None:
    line = json.dumps(
        {"jsonrpc": "2.0", "apiVersion": API_VERSION, "id": 1, "method": "note.get"}
    )
    assert decode_request(line)[1] == "note.get"


@pytest.mark.parametrize("client_version", [0, 2, 99, "2.4"])
def test_decode_request_refuses_a_major_api_version_mismatch(client_version: Any) -> None:
    line = json.dumps(
        {"jsonrpc": "2.0", "apiVersion": client_version, "id": 5, "method": "note.get"}
    )
    with pytest.raises(RpcException) as excinfo:
        decode_request(line)
    error = excinfo.value.error
    assert error.code == INVALID_REQUEST
    assert error.data is not None
    assert error.data["serverApiVersion"] == API_VERSION
    assert error.data["clientApiVersion"] == int(str(client_version).split(".")[0])
    assert error.data["hint"]


def test_decode_request_accepts_a_matching_minor_api_version() -> None:
    line = json.dumps(
        {"jsonrpc": "2.0", "apiVersion": f"{API_VERSION}.7", "id": 1, "method": "note.get"}
    )
    assert decode_request(line)[0] == 1


def test_decode_request_rejects_a_non_numeric_api_version() -> None:
    line = json.dumps({"jsonrpc": "2.0", "apiVersion": "banana", "id": 1, "method": "note.get"})
    with pytest.raises(RpcException) as excinfo:
        decode_request(line)
    assert excinfo.value.error.code == INVALID_REQUEST


# ---------------------------------------------------------------------------
# Taxonomy mapping (spec 09 §1.5 — loud failures)
# ---------------------------------------------------------------------------


def test_organize_error_carries_taxonomy_name_and_hint() -> None:
    error = _organize_error(VaultError("vault is gone", hint="check vault.root"))
    assert error.code == ORGANIZE_ERROR
    assert error.message == "vault is gone"
    assert error.data == {"kind": "VaultError", "hint": "check vault.root"}


def test_organize_error_without_a_hint_still_names_the_kind() -> None:
    error = _organize_error(ServerError("nope"))
    assert error.data == {"kind": "ServerError"}


# ---------------------------------------------------------------------------
# SingleInstanceLock (spec 10 §1)
# ---------------------------------------------------------------------------


def test_lock_acquire_writes_the_pid_and_blocks_a_second_holder(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "organize-core.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    try:
        assert first.held is True
        assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
        second = SingleInstanceLock(path)
        with pytest.raises(AlreadyRunning) as excinfo:
            second.acquire()
        assert str(path) in str(excinfo.value)
        assert str(os.getpid()) in str(excinfo.value)
        assert excinfo.value.hint
        assert second.held is False
    finally:
        first.release()


def test_lock_release_lets_the_next_server_take_over(tmp_path: Path) -> None:
    path = tmp_path / "organize-core.lock"
    first = SingleInstanceLock(path)
    first.acquire()
    first.release()
    assert first.held is False
    assert not path.exists()
    second = SingleInstanceLock(path)
    second.acquire()
    assert second.held is True
    second.release()


def test_lock_reclaims_a_stale_lock_file_from_a_dead_pid(tmp_path: Path) -> None:
    path = tmp_path / "organize-core.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("999999999\n", encoding="utf-8")  # a pid that cannot be running
    lock = SingleInstanceLock(path)
    lock.acquire()
    try:
        assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


def test_lock_release_is_idempotent(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "l.lock")
    lock.release()  # never acquired
    lock.acquire()
    lock.release()
    lock.release()
    assert lock.held is False


def test_lock_works_as_a_context_manager(tmp_path: Path) -> None:
    path = tmp_path / "ctx.lock"
    with SingleInstanceLock(path) as lock:
        assert lock.held is True
    assert not path.exists()


# ---------------------------------------------------------------------------
# WriterQueue (spec 10 §1: ALL mutating methods, one at a time)
# ---------------------------------------------------------------------------


def test_writer_queue_never_runs_two_submissions_at_once() -> None:
    queue = WriterQueue()
    overlaps: list[str] = []
    active = 0
    guard = threading.Lock()
    results: dict[int, int] = {}

    def job(n: int) -> int:
        nonlocal active
        with guard:
            active += 1
            if active != 1:
                overlaps.append(f"{n} overlapped")
        time.sleep(0.01)
        with guard:
            if active != 1:
                overlaps.append(f"{n} overlapped on exit")
            active -= 1
        return n * 2

    def run(n: int) -> None:
        results[n] = queue.submit(lambda: job(n))

    threads = [threading.Thread(target=run, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20.0)
        assert not thread.is_alive()

    queue.stop()
    assert overlaps == []
    assert results == {n: n * 2 for n in range(8)}
    assert queue.completed == 8


def test_writer_queue_runs_submissions_in_arrival_order() -> None:
    queue = WriterQueue()
    order: list[int] = []
    gate = threading.Event()

    queue.start()
    blocker = threading.Thread(target=lambda: queue.submit(gate.wait))
    blocker.start()
    time.sleep(0.05)  # ensure the blocker owns the writer thread

    threads = []
    for n in range(5):
        thread = threading.Thread(target=lambda n=n: queue.submit(lambda: order.append(n)))
        thread.start()
        thread.join(timeout=0.05)  # queued behind the blocker, so this times out
        threads.append(thread)

    gate.set()
    for thread in [blocker, *threads]:
        thread.join(timeout=10.0)
        assert not thread.is_alive()
    queue.stop()
    assert order == [0, 1, 2, 3, 4]


def test_writer_queue_propagates_the_exception_to_the_caller() -> None:
    queue = WriterQueue()

    def boom() -> None:
        raise VaultError("nope", hint="fix the vault")

    with pytest.raises(VaultError) as excinfo:
        queue.submit(boom)
    assert excinfo.value.hint == "fix the vault"
    assert queue.submit(lambda: "still alive") == "still alive"
    queue.stop()


def test_writer_queue_refuses_to_restart_after_stop() -> None:
    queue = WriterQueue()
    assert queue.submit(lambda: 1) == 1
    queue.stop()
    with pytest.raises(ServerError):
        queue.submit(lambda: 2)


# ---------------------------------------------------------------------------
# _ReadWriteLock
# ---------------------------------------------------------------------------


def test_read_write_lock_allows_readers_to_overlap() -> None:
    lock = _ReadWriteLock()
    barrier = threading.Barrier(3, timeout=5.0)
    reached: list[int] = []

    def reader(n: int) -> None:
        with lock.read():
            barrier.wait()  # only completes if all three readers hold it at once
            reached.append(n)

    threads = [threading.Thread(target=reader, args=(n,)) for n in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
        assert not thread.is_alive()
    assert sorted(reached) == [0, 1, 2]


def test_read_write_lock_excludes_a_reader_while_a_writer_holds_it() -> None:
    lock = _ReadWriteLock()
    events: list[str] = []
    writer_in = threading.Event()
    release = threading.Event()

    def writer() -> None:
        with lock.write():
            events.append("write-enter")
            writer_in.set()
            release.wait(timeout=5.0)
            events.append("write-exit")

    def reader() -> None:
        writer_in.wait(timeout=5.0)
        with lock.read():
            events.append("read-enter")

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    writer_in.wait(timeout=5.0)
    reader_thread.start()
    time.sleep(0.1)
    assert events == ["write-enter"], "a reader entered while a writer held the lock"
    release.set()
    for thread in (writer_thread, reader_thread):
        thread.join(timeout=10.0)
        assert not thread.is_alive()
    assert events == ["write-enter", "write-exit", "read-enter"]


def test_read_write_lock_excludes_a_writer_while_readers_hold_it() -> None:
    lock = _ReadWriteLock()
    events: list[str] = []
    reader_in = threading.Event()
    release = threading.Event()

    def reader() -> None:
        with lock.read():
            reader_in.set()
            release.wait(timeout=5.0)
            events.append("read-exit")

    def writer() -> None:
        reader_in.wait(timeout=5.0)
        with lock.write():
            events.append("write-enter")

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    reader_in.wait(timeout=5.0)
    writer_thread.start()
    time.sleep(0.1)
    assert events == []
    release.set()
    for thread in (reader_thread, writer_thread):
        thread.join(timeout=10.0)
        assert not thread.is_alive()
    assert events == ["read-exit", "write-enter"]


# ---------------------------------------------------------------------------
# Structural obligations (ARCHITECTURE decision 4, spec 08 §B18)
# ---------------------------------------------------------------------------


def _server_ast() -> ast.Module:
    return ast.parse(SERVER_SOURCE.read_text(encoding="utf-8", errors="replace"))


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def test_server_never_reads_the_process_environment_or_home() -> None:
    """ARCHITECTURE decision 4: paths.py is the single environment touchpoint.

    Structural (AST) rather than textual, so prose in the module docstring
    can discuss the rule without tripping it.
    """
    banned = {"environ", "getenv", "home", "expanduser", "expandvars"}
    offenders = [
        node.attr
        for node in ast.walk(_server_ast())
        if isinstance(node, ast.Attribute) and node.attr in banned
    ] + [
        node.id
        for node in ast.walk(_server_ast())
        if isinstance(node, ast.Name) and node.id in banned
    ]
    assert offenders == [], (
        f"server.py must take every location from CorePaths (found {sorted(set(offenders))})"
    )


def test_server_reads_and_writes_text_with_explicit_encoding() -> None:
    """Spec 08 §B18 / 06 §6: no implicit-locale text I/O anywhere."""
    checked = 0
    for node in ast.walk(_server_ast()):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        keywords = {kw.arg for kw in node.keywords}
        if name in ("read_text", "write_text", "open"):
            if ast.unparse(node.func) in ("os.open", "os.fdopen"):
                continue  # raw file descriptors, not text I/O
            assert "encoding" in keywords and "errors" in keywords, ast.unparse(node)
            checked += 1
        elif name in ("decode", "encode") and isinstance(node.func, ast.Attribute):
            rendered = ast.unparse(node)
            assert "utf-8" in rendered or "utf8" in rendered, rendered
            checked += 1
    assert checked >= 3, "the encoding guard found nothing to check — did the I/O move?"


def test_construction_performs_no_socket_or_state_io(
    fixture_vault: Path, tmp_path: Path
) -> None:
    """Spec 08 §B2: constructors that do I/O take the whole process down."""
    paths: CorePaths = make_core_paths(tmp_path)
    server = OrganizeServer(make_config(fixture_vault), paths)
    assert not paths.state_dir.exists()
    assert not paths.socket_path.exists()
    assert not paths.lock_path.exists()
    assert server.lock.held is False
    assert server.ready.is_set() is False


def test_socket_and_idle_timeout_come_from_config_when_not_injected(
    fixture_vault: Path, tmp_path: Path
) -> None:
    from dataclasses import replace

    from organize_core.config import ServerConfig

    paths = make_core_paths(tmp_path)
    config = make_config(fixture_vault)
    default = OrganizeServer(config, paths)
    assert default.socket_path == paths.socket_path
    assert default.idle_timeout_seconds == 600.0

    configured = replace(
        config,
        server=ServerConfig(socket_path=tmp_path / "custom.sock", idle_timeout_seconds=12.5),
    )
    from_config = OrganizeServer(configured, paths)
    assert from_config.socket_path == tmp_path / "custom.sock"
    assert from_config.idle_timeout_seconds == 12.5

    injected = OrganizeServer(
        configured, paths, socket_path=tmp_path / "override.sock", idle_timeout_seconds=1.0
    )
    assert injected.socket_path == tmp_path / "override.sock"
    assert injected.idle_timeout_seconds == 1.0


# ---------------------------------------------------------------------------
# dispatch() is usable without a socket (spec 10 §2 consequence 1:
# "everything testable without Neovim")
# ---------------------------------------------------------------------------


def _offline_server(fixture_vault: Path, tmp_path: Path) -> OrganizeServer:
    paths = make_core_paths(tmp_path)
    paths.ensure_state_dirs()
    server = OrganizeServer(make_config(fixture_vault), paths)
    server.index.load()
    server.index.scan()
    return server


def test_dispatch_serves_reads_and_writes_without_a_connection(
    fixture_vault: Path, tmp_path: Path
) -> None:
    server = _offline_server(fixture_vault, tmp_path)
    try:
        records = server.dispatch("search.query", {"criteria": {"tags": "todo"}})
        assert sorted(Path(r["path"]).name for r in records) == [
            "2026-07-01T09:00:00.000Z.md",
            "metadata-list.md",
            "metadata-map.md",
        ]
        assert server.dispatch("index.reindex", {})["total"] == 21
        assert server.writer.completed == 1
    finally:
        server.shutdown()


def test_dispatch_rejects_an_unknown_method(fixture_vault: Path, tmp_path: Path) -> None:
    server = OrganizeServer(make_config(fixture_vault), make_core_paths(tmp_path))
    try:
        with pytest.raises(RpcException) as excinfo:
            server.dispatch("op.delete", {"path": "a.md"})
        assert excinfo.value.error.code == METHOD_NOT_FOUND
        assert excinfo.value.error.data is not None
        assert excinfo.value.error.data["known_methods"] == list(RPC_METHODS)
    finally:
        server.shutdown()


def test_dispatch_events_subscribe_without_a_connection_fails_clearly(
    fixture_vault: Path, tmp_path: Path
) -> None:
    server = OrganizeServer(make_config(fixture_vault), make_core_paths(tmp_path))
    try:
        with pytest.raises(ServerError) as excinfo:
            server.dispatch("events.subscribe", {})
        assert "connection" in str(excinfo.value)
    finally:
        server.shutdown()


@pytest.mark.parametrize("method", ["auto.propose", "auto.apply"])
def test_dispatch_auto_methods_raise_the_not_implemented_rpc_error(
    fixture_vault: Path, tmp_path: Path, method: str
) -> None:
    server = _offline_server(fixture_vault, tmp_path)
    try:
        with pytest.raises(RpcException) as excinfo:
            server.dispatch(method, {"text": "anything"})
        assert excinfo.value.error.code == NOT_IMPLEMENTED
        assert excinfo.value.error.data is not None
        assert excinfo.value.error.data["kind"] == "NotImplemented"
    finally:
        server.shutdown()


def test_index_changing_methods_exclude_the_read_only_preview() -> None:
    assert "op.merge_preview" in MUTATING_METHODS
    assert "op.merge_preview" not in INDEX_CHANGING_METHODS
    assert INDEX_CHANGING_METHODS == MUTATING_METHODS - {"op.merge_preview"}
