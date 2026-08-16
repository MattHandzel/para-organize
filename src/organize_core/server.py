"""JSON-RPC 2.0 server over a unix socket (spec 10 §1-2).

Wire protocol:
- Socket: ``$XDG_RUNTIME_DIR/organize-core.sock`` (CorePaths.socket_path;
  ``--socket`` / config override).
- NEWLINE-DELIMITED JSON-RPC 2.0: one request or response object per line.
- Handshake: on connect the server sends ``{"apiVersion": 1}`` as its
  first line; clients refuse a major-version mismatch with a clear message
  (spec 10 §2). API_VERSION lives in ``organize_core.__init__``.
- Single-instance lock (AlreadyRunning when held); concurrent clients
  allowed; ALL mutating methods serialized through one writer queue
  (spec 10 §1).
- Idle timeout: exit cleanly after configurable inactivity (the nvim
  client auto-spawns the server when missing).

The server holds the warm index in memory so interactive calls meet the
latency targets (suggest < 100 ms, UI action < 50 ms — spec 09 §4).

Implementation notes (decisions this module makes, with their reasons):

* **Envelope versioning.** 10 §2 says "requests/responses are versioned
  (``{"apiVersion": 1}``)", so *every* response line — result, error and
  pushed event alike — carries ``apiVersion``, not just the handshake. A
  request MAY carry ``apiVersion``; when its MAJOR differs from
  :data:`organize_core.API_VERSION` the request is refused with
  :data:`INVALID_REQUEST` naming both versions rather than being served on
  a guess.
* **Mutating set.** The scaffold docstring for :meth:`OrganizeServer.dispatch`
  names ``op.*``, ``meta.set``, ``folder.create``, ``index.reindex`` and
  ``auto.apply`` as the writer-queue methods, so ``op.merge_preview`` is
  queued too even though it only reads — the queue then also guarantees the
  snapshot it hands back was taken while no writer was running (10 §4).
* **Reader/writer isolation.** Read methods run concurrently on their
  connection threads, but the in-memory index would tear if a read iterated
  it while a queued write mutated it. A writer-preferring read/write lock
  wraps the two paths: many readers, one writer, no reader/writer overlap.
* **Idle timeout** measures time since the last protocol activity (accept,
  request, response, pushed event) and never fires while a request is in
  flight. It is deliberately independent of whether connections are open:
  a client that connects and goes silent must not pin the process forever.
* **``auto.propose`` / ``auto.apply``** exist from day one and answer with
  :data:`NOT_IMPLEMENTED` until the doc-13 phase ships (13 §3), mirroring
  the ``organize auto-organize`` CLI stub.

Nothing in this module reads ``os.environ`` or ``Path.home()``; every
location arrives through :class:`~organize_core.paths.CorePaths` or an
explicit argument (ARCHITECTURE structural decision 4).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import itertools
import json
import logging
import os
import queue
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from organize_core import API_VERSION
from organize_core.actions import ActionRecord, ActionRecorder, SuggestionShown
from organize_core.config import (
    Config,
    MetadataFieldConfig,
    coerce_metadata_value,
    metadata_fields_by_key,
)
from organize_core.errors import AlreadyRunning, OrganizeError, ServerError, VaultError
from organize_core.fileops import (
    FileSnapshot,
    OperationContext,
    OperationLog,
    archive_capture,
    merge_into_note,
    merge_preview,
    move_to_destination,
    new_folder,
    update_frontmatter,
)
from organize_core.frontmatter import FrontmatterError, load_file
from organize_core.index import (
    PARA_KEY_TO_TYPE,
    PARA_TYPE_TO_KEY,
    NoteRecord,
    QueryCriteria,
    VaultIndex,
)
from organize_core.learn import (
    LearningData,
    load_learning,
    maybe_apply_decay,
    record_action,
    save_learning,
)
from organize_core.paths import CorePaths
from organize_core.routes import get_description, merge_route_suggestions
from organize_core.routes import resolve as resolve_routes
from organize_core.session import Session, start_session
from organize_core.suggest import CaptureFeaturesView, generate_candidates
from organize_core.suggest import suggest as rank_suggestions

logger = logging.getLogger(__name__)

# Method table — mirrors spec 10 §2 EXACTLY (names are the API contract;
# the nvim client and Claude agents dispatch on these strings).
RPC_METHODS: tuple[str, ...] = (
    "session.start",       # (filters) → {session_id, captures[]}     spec 03 §2
    "note.get",            # (path) → NoteRecord + body               spec 03 §3
    "suggest.for_note",    # (path) → Suggestion[] (routes merged)    spec 04, 11 §1
    "op.move",             # (path, destination) → OperationResult    spec 05 §2
    "op.merge_preview",    # (path, target) → {content, snapshot}     spec 03 §5, 05 §4
    "op.merge_commit",     # (path, target, content, snapshot) → OperationResult
    "op.archive",          # (path) → OperationResult                 spec 05 §3
    "meta.set",            # (path, changes) → OperationResult        spec 05 §5, 07
    "meta.fields",         # () → metadata_fields[] + completions      spec 07, 10 §3
    "meta.values",         # (key) → distinct values in the vault      spec 07 complete="existing"
    "folder.create",       # (para_type, name) → OperationResult      spec 05 §6
    "folder.list",         # (para_type?) → every PARA subfolder       spec 04 §1, 10 §1
    "folder.children",     # (path) → {dirs[], notes[]} for browsing   spec 03 §3
    "index.reindex",       # () → {total, duration}                   spec 03 §7
    "search.query",        # (criteria) → NoteRecord[]                spec 03 §2
    "routes.resolve",      # (path) → RouteMatch[]                    spec 11 §1
    "auto.propose",        # (text, source_context) → proposal        spec 13 §2 (stub until Phase 6)
    "auto.apply",          # (proposal) → OperationResult[]           spec 13 §2 (stub until Phase 6)
    "events.subscribe",    # () → stream of index/progress events     spec 10 §2
)

#: Methods that touch the vault (or the index snapshot) and therefore run
#: one-at-a-time through :class:`WriterQueue` (spec 10 §1). ``op.*`` is
#: included wholesale per the dispatch contract.
MUTATING_METHODS: frozenset[str] = frozenset(
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

#: The subset of :data:`MUTATING_METHODS` that can change the index, and so
#: emits ``index-updated``. ``op.merge_preview`` is queued for snapshot
#: consistency but writes nothing, so it must not claim the index moved.
INDEX_CHANGING_METHODS: frozenset[str] = MUTATING_METHODS - {"op.merge_preview"}

#: Events pushed to subscribed connections (spec 10 §2).
EVENT_INDEX_UPDATED = "index-updated"
EVENT_OP_PROGRESS = "op-progress"
EVENT_TYPES: tuple[str, ...] = (EVENT_INDEX_UPDATED, EVENT_OP_PROGRESS)

#: Method name of a server→client push. Events are JSON-RPC notifications
#: (no ``id``), so a client that ignores them stays protocol-correct.
EVENT_METHOD = "event"

# JSON-RPC 2.0 error codes (standard) + implementation range.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# -32000..-32099: OrganizeError subclasses map here with the taxonomy name
# in error.data.kind and any hint in error.data.hint (loud failure, 09 §1.5)
ORGANIZE_ERROR = -32000
#: Declared-but-unbuilt method (spec 13 §3: auto.* answer this until the
#: automatic-organize phase ships).
NOT_IMPLEMENTED = -32001

_NOT_IMPLEMENTED_MESSAGE = (
    "not implemented (ships in the automatic-organize phase, spec 13)"
)

#: How long the accept loop blocks before re-checking stop/idle state.
_ACCEPT_POLL_SECONDS = 0.05

#: Practical AF_UNIX ``sun_path`` limit (108 bytes on Linux, 104 on BSD).
#: Used only to explain a bind failure, never to pre-reject a path.
_AF_UNIX_PATH_MAX = 104
#: Cap on one request line; a client that never sends "\n" cannot OOM us.
_MAX_LINE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RpcError:
    code: int
    message: str
    data: dict[str, Any] | None = None


#: Every protocol-level error carries the same `data:{kind, hint}` shape the
#: -32000 domain errors do, so a client can render taxonomy + hint with ONE
#: code path. Without it `note.get` with a bad path gave
#: `{kind: "VaultError", hint: ...}` while a missing parameter gave a bare
#: message, and the client needed a special case per error class.
_PROTOCOL_ERROR_KINDS: dict[int, str] = {
    PARSE_ERROR: "ParseError",
    INVALID_REQUEST: "InvalidRequest",
    METHOD_NOT_FOUND: "MethodNotFound",
    INVALID_PARAMS: "InvalidParams",
    INTERNAL_ERROR: "InternalError",
}

_PROTOCOL_ERROR_HINTS: dict[int, str] = {
    PARSE_ERROR: "send one complete JSON object per line (newline-delimited JSON-RPC 2.0)",
    INVALID_REQUEST: 'a request is {"jsonrpc": "2.0", "id": ..., "method": ..., "params": {...}}',
    METHOD_NOT_FOUND: "see data.known_methods for the methods this core serves (spec 10 §2)",
    INVALID_PARAMS: "check the parameter names and shapes for this method (spec 10 §2)",
    INTERNAL_ERROR: "this is a core bug — check the server log and report it",
}


def _with_taxonomy(error: RpcError) -> RpcError:
    """Fill in `data.kind`/`data.hint` for a protocol error that lacks them."""
    if error.code == ORGANIZE_ERROR:
        return error  # domain errors already carry their own kind + hint
    kind = _PROTOCOL_ERROR_KINDS.get(error.code)
    if kind is None:
        return error
    data = dict(error.data or {})
    data.setdefault("kind", kind)
    hint = _PROTOCOL_ERROR_HINTS.get(error.code)
    if hint is not None:
        data.setdefault("hint", hint)
    return RpcError(error.code, error.message, data)


class RpcException(Exception):
    """Carries an :class:`RpcError` (and, when known, the request id) out of
    decode/dispatch so the connection loop can encode it. Never fatal to the
    connection — the contract is "error response, never a dropped socket"."""

    def __init__(self, error: RpcError, *, request_id: int | str | None = None) -> None:
        super().__init__(error.message)
        self.error = _with_taxonomy(error)
        self.request_id = request_id


# ---------------------------------------------------------------------------
# JSON encoding
# ---------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    """Make the domain dataclasses wire-safe without a second schema."""
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(str(item) for item in obj)
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")


def encode_response(
    id_: int | str | None, result: Any = None, error: RpcError | None = None
) -> str:
    """One newline-terminated JSON-RPC 2.0 response line."""
    payload: dict[str, Any] = {"jsonrpc": "2.0", "apiVersion": API_VERSION, "id": id_}
    if error is not None:
        err: dict[str, Any] = {"code": error.code, "message": error.message}
        if error.data is not None:
            err["data"] = error.data
        payload["error"] = err
    else:
        payload["result"] = result
    return json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n"


def encode_event(event: str, data: dict[str, Any]) -> str:
    """One newline-terminated server→client event notification line."""
    payload = {
        "jsonrpc": "2.0",
        "apiVersion": API_VERSION,
        "method": EVENT_METHOD,
        "params": {"event": event, "data": data},
    }
    return json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n"


def handshake_line() -> str:
    """First line every connection receives (spec 10 §2)."""
    return json.dumps({"apiVersion": API_VERSION}, ensure_ascii=False) + "\n"


def empty_array_as_object(raw: Any) -> Any:
    """An EMPTY JSON array, wherever an OBJECT is expected, reads as ``{}``.

    lua has one value for both shapes, so ``vim.json.encode({})`` emits ``[]``
    everywhere the nvim client means an empty object — for ``params`` itself
    and for object-typed fields inside it (``criteria``, ``filters``,
    ``changes``, ...). An empty array carries no positional argument, so
    reading it as ``{}`` costs nothing; a NON-empty array is a real
    positional/type error and is returned unchanged for the caller to reject.
    """
    return {} if isinstance(raw, list) and not raw else raw


def decode_request(line: str) -> tuple[int | str | None, str, dict[str, Any]]:
    """Parse one request line → (id, method, params). Malformed ⇒ raises
    with PARSE_ERROR/INVALID_REQUEST semantics for the caller to encode."""
    try:
        payload = json.loads(line)
    except ValueError as exc:
        raise RpcException(RpcError(PARSE_ERROR, f"invalid JSON: {exc}")) from exc

    if not isinstance(payload, dict):
        raise RpcException(
            RpcError(INVALID_REQUEST, "a JSON-RPC request must be a JSON object")
        )

    raw_id = payload.get("id")
    if raw_id is not None and not isinstance(raw_id, (int, str)) or isinstance(raw_id, bool):
        raise RpcException(RpcError(INVALID_REQUEST, "request id must be a string, number or null"))
    request_id: int | str | None = raw_id

    if payload.get("jsonrpc") != "2.0":
        raise RpcException(
            RpcError(
                INVALID_REQUEST,
                f"unsupported jsonrpc version {payload.get('jsonrpc')!r}; this server speaks 2.0",
            ),
            request_id=request_id,
        )

    if "apiVersion" in payload:
        _check_api_version(payload["apiVersion"], request_id)

    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise RpcException(
            RpcError(INVALID_REQUEST, "request is missing a string 'method'"),
            request_id=request_id,
        )

    params = payload.get("params", {})
    if params is None:
        params = {}
    params = empty_array_as_object(params)
    if not isinstance(params, dict):
        raise RpcException(
            RpcError(
                INVALID_PARAMS,
                "params must be an object; positional (array) params are not supported",
            ),
            request_id=request_id,
        )

    return request_id, method, params


def _check_api_version(raw: Any, request_id: int | str | None) -> None:
    """Refuse a MAJOR-version mismatch loudly (spec 10 §2)."""
    try:
        major = int(str(raw).split(".", 1)[0])
    except (TypeError, ValueError):
        raise RpcException(
            RpcError(INVALID_REQUEST, f"apiVersion {raw!r} is not a version number"),
            request_id=request_id,
        ) from None
    if major != API_VERSION:
        raise RpcException(
            RpcError(
                INVALID_REQUEST,
                f"apiVersion mismatch: client speaks {major}, this core speaks {API_VERSION}",
                {
                    "kind": "ApiVersionMismatch",
                    "serverApiVersion": API_VERSION,
                    "clientApiVersion": major,
                    "hint": "upgrade the client or the core so both share the major API version",
                },
            ),
            request_id=request_id,
        )


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


class SingleInstanceLock:
    """Advisory lock on CorePaths.lock_path; a second ``organize serve``
    raises AlreadyRunning (spec 10 §1). Stale locks (dead pid) are
    reclaimed."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            owner = self._read_owner_pid(fd)
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise AlreadyRunning(
                    f"another organize-core server holds {self.lock_path}"
                    + (f" (pid {owner})" if owner else ""),
                    hint="stop the running server, or point --socket/ORGANIZE_CORE_RUNTIME_DIR "
                    "at a different runtime directory",
                ) from exc
            raise ServerError(
                f"cannot lock {self.lock_path}: {exc}",
                hint="check permissions on the runtime directory",
            ) from exc
        # flock is released by the kernel when a holder dies, so reaching
        # here means any pid recorded in the file is gone: reclaim the file.
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("utf-8", errors="replace"))
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            self.lock_path.unlink()

    @staticmethod
    def _read_owner_pid(fd: int) -> int | None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 64).decode("utf-8", errors="replace").strip()
        except OSError:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Writer queue + reader/writer lock
# ---------------------------------------------------------------------------


class _WriteItem:
    __slots__ = ("fn", "done", "result", "exc")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.exc: BaseException | None = None


class WriterQueue:
    """All mutating operations funnel through here, executed one at a time
    (spec 10 §1) — the vault-side counterpart of the concurrent-
    modification check (10 §4)."""

    def __init__(self) -> None:
        self._queue: queue.Queue[_WriteItem | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._stopped = False
        #: number of completed submissions — observability + tests
        self.completed = 0

    def start(self) -> None:
        with self._start_lock:
            if self._stopped:
                raise ServerError(
                    "the writer queue has been stopped and cannot be restarted",
                    hint="construct a new OrganizeServer instead of reusing a shut-down one",
                )
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="organize-writer", daemon=True
                )
                self._thread.start()

    def submit(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` when it reaches the head of the queue; blocks the
        calling connection handler until done, returns its result."""
        self.start()
        item = _WriteItem(fn)
        self._queue.put(item)
        item.done.wait()
        if item.exc is not None:
            raise item.exc
        return item.result

    def stop(self, timeout: float = 5.0) -> None:
        with self._start_lock:
            if self._stopped:
                return
            self._stopped = True
            thread = self._thread
        self._queue.put(None)
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                # Fail anything queued behind the sentinel rather than
                # leaving a connection blocked forever on item.done.
                self._drain()
                return
            try:
                item.result = item.fn()
            except BaseException as exc:  # noqa: BLE001 - relayed to the caller verbatim
                item.exc = exc
            finally:
                self.completed += 1
                item.done.set()

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            item.exc = ServerError(
                "the server shut down before this operation reached the writer queue",
                hint="retry once the server is running again",
            )
            item.done.set()


class _ReadWriteLock:
    """Writer-preferring many-readers/one-writer lock.

    Guards the warm index: queued writers already run one at a time, but a
    read iterating the record map while a writer mutates it would tear. Read
    methods still run concurrently with each other (spec 10 §1).
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextlib.contextmanager
    def read(self) -> Iterator[None]:
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextlib.contextmanager
    def write(self) -> Iterator[None]:
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


class _Connection:
    """One accepted client: a reader thread and an outbound sender thread.

    The sender exists so an event push (which happens on the writer thread)
    can never block behind a slow reader — a stalled subscriber costs its own
    queue, not the vault writer.
    """

    def __init__(self, server: OrganizeServer, sock: socket.socket, conn_id: int) -> None:
        self.server = server
        self.sock = sock
        self.id = conn_id
        self.subscriptions: set[str] = set()
        self.outbox: queue.Queue[str | None] = queue.Queue()
        self._closed = threading.Event()
        self._sender = threading.Thread(
            target=self._send_loop, name=f"organize-conn{conn_id}-tx", daemon=True
        )
        self._reader = threading.Thread(
            target=self._read_loop, name=f"organize-conn{conn_id}-rx", daemon=True
        )

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._sender.start()
        self.send(handshake_line())
        self._reader.start()

    def send(self, line: str) -> None:
        if not self._closed.is_set():
            self.outbox.put(line)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self.outbox.put(None)
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.sock.close()

    def join(self, timeout: float = 2.0) -> None:
        for thread in (self._reader, self._sender):
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=timeout)

    # --- threads ---------------------------------------------------------

    def _send_loop(self) -> None:
        while True:
            line = self.outbox.get()
            if line is None:
                return
            try:
                self.sock.sendall(line.encode("utf-8", errors="replace"))
            except OSError:
                self.close()
                return

    def _read_loop(self) -> None:
        buffer = b""
        try:
            while not self._closed.is_set() and not self.server.stopping:
                try:
                    chunk = self.sock.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > _MAX_LINE_BYTES:
                    self.send(
                        encode_response(
                            None,
                            error=_with_taxonomy(
                                RpcError(
                                    INVALID_REQUEST,
                                    f"request line exceeds {_MAX_LINE_BYTES} bytes",
                                )
                            ),
                        )
                    )
                    return
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    self._handle_line(line)
        finally:
            self.server._connection_finished(self)

    def _handle_line(self, line: str) -> None:
        self.server._touch()
        request_id: int | str | None = None
        had_id = False
        try:
            request_id, method, params = decode_request(line)
            had_id = request_id is not None
            self.server._begin_request()
            try:
                result = self.server._dispatch_for(self, method, params)
            finally:
                self.server._end_request()
        except RpcException as exc:
            self._respond(exc.request_id if exc.request_id is not None else request_id, error=exc.error)
            return
        except OrganizeError as exc:
            self._respond(request_id, error=_organize_error(exc))
            return
        except Exception as exc:  # noqa: BLE001 - never drop a connection
            logger.exception("server: unhandled error serving a request")
            self._respond(
                request_id,
                error=_with_taxonomy(RpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")),
            )
            return
        if had_id:
            self._respond(request_id, result=result)

    def _respond(
        self, request_id: int | str | None, result: Any = None, error: RpcError | None = None
    ) -> None:
        if request_id is None and error is None:
            return  # notification: no response (JSON-RPC 2.0)
        self.send(encode_response(request_id, result=result, error=error))
        self.server._touch()


def _organize_error(exc: OrganizeError) -> RpcError:
    """Map the taxonomy onto ORGANIZE_ERROR, carrying kind + hint (09 §1.5)."""
    data: dict[str, Any] = {"kind": type(exc).__name__}
    hint = getattr(exc, "hint", None)
    if hint:
        data["hint"] = hint
    return RpcError(ORGANIZE_ERROR, str(exc), data)


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class OrganizeServer:
    """The long-running core process (``organize serve``)."""

    def __init__(
        self,
        config: Config,
        paths: CorePaths,
        *,
        socket_path: Path | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.socket_path = Path(
            socket_path
            if socket_path is not None
            else (config.server.socket_path or paths.socket_path)
        )
        self.idle_timeout_seconds = (
            float(idle_timeout_seconds)
            if idle_timeout_seconds is not None
            else float(config.server.idle_timeout_seconds)
        )

        self.index = VaultIndex(config, paths.index_path)
        self.oplog = OperationLog(paths.operations_log)
        self.recorder = ActionRecorder(paths.actions_dir)
        self.backup_dir = Path(config.vault.root) / config.file_ops.backup_dir

        self.lock = SingleInstanceLock(paths.lock_path)
        self.writer = WriterQueue()

        self._rwlock = _ReadWriteLock()
        self._learning: LearningData | None = None
        self._learning_lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._sessions_lock = threading.Lock()

        self._server_sock: socket.socket | None = None
        #: True only once THIS instance bound the socket. Teardown removes the
        #: socket file only then — a refused second start must never delete the
        #: running server's socket.
        self._bound = False
        self._connections: dict[int, _Connection] = {}
        self._connections_lock = threading.Lock()
        self._conn_ids = itertools.count(1)

        self._stop = threading.Event()
        self._ready = threading.Event()
        self._shutdown_lock = threading.RLock()
        self._shutdown_done = False
        self._last_activity = time.monotonic()
        self._inflight = 0
        self._inflight_lock = threading.Lock()

        self._handlers: dict[str, Callable[[_Connection | None, dict[str, Any]], Any]] = {
            "session.start": self._session_start,
            "note.get": self._note_get,
            "suggest.for_note": self._suggest_for_note,
            "op.move": self._op_move,
            "op.merge_preview": self._op_merge_preview,
            "op.merge_commit": self._op_merge_commit,
            "op.archive": self._op_archive,
            "meta.set": self._meta_set,
            "meta.fields": self._meta_fields,
            "meta.values": self._meta_values,
            "folder.create": self._folder_create,
            "folder.list": self._folder_list,
            "folder.children": self._folder_children,
            "index.reindex": self._index_reindex,
            "search.query": self._search_query,
            "routes.resolve": self._routes_resolve,
            "auto.propose": self._auto_not_implemented,
            "auto.apply": self._auto_not_implemented,
            "events.subscribe": self._events_subscribe,
        }
        missing = set(RPC_METHODS) - set(self._handlers)
        if missing:  # pragma: no cover - guards the contract at construction
            raise ServerError(
                f"RPC methods without handlers: {sorted(missing)}",
                hint="every name in RPC_METHODS must dispatch (spec 10 §2)",
            )

    # --- properties ------------------------------------------------------

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def ready(self) -> threading.Event:
        """Set once the socket is bound and listening (tests wait on it)."""
        return self._ready

    @property
    def connection_count(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    # --- lifecycle -------------------------------------------------------

    def serve_forever(self) -> None:
        """Bind socket, acquire single-instance lock, accept connections
        (each greeted with the apiVersion line), dispatch until idle
        timeout or signal; teardown removes socket + lock."""
        self.lock.acquire()
        try:
            self.paths.ensure_state_dirs()
            self._install_signal_handlers()
            self._bind()
            self._warm_index()
            self.writer.start()
            self._touch()
            self._ready.set()
            self._accept_loop()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
            self._stop.set()

        sock, self._server_sock = self._server_sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

        with self._connections_lock:
            connections = list(self._connections.values())
        for conn in connections:
            conn.close()
        for conn in connections:
            conn.join(timeout=2.0)

        self.writer.stop()
        with contextlib.suppress(Exception):
            self.index.flush()
        if self._bound:
            self._bound = False
            with contextlib.suppress(OSError):
                if self.socket_path.is_socket():
                    self.socket_path.unlink()
        self.lock.release()
        self._ready.clear()
        logger.info("server: shut down (socket %s released)", self.socket_path)

    # --- socket plumbing -------------------------------------------------

    def _bind(self) -> None:
        """Bind the AF_UNIX socket, converting every OS-level failure into a
        :class:`ServerError` with an actionable hint (09 §1.5).

        The caller is a CLI that cannot attach a hint to an exception it did
        not raise, so an unusable ``--socket`` used to surface as a bare
        ``PermissionError: [Errno 13]``. Refusing to start is correct; the
        message telling Matt WHY is the part that was missing.
        """
        try:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ServerError(
                f"cannot create the socket directory {self.socket_path.parent}: "
                f"{exc.strerror or exc}",
                hint="pass --socket with a path inside a writable directory "
                "(default: $XDG_RUNTIME_DIR/organize-core.sock, spec 10 §3)",
            ) from exc
        if self.socket_path.exists():
            if self._socket_is_live():
                raise AlreadyRunning(
                    f"a server is already listening on {self.socket_path}",
                    hint="stop it first, or serve on a different --socket path",
                )
            with contextlib.suppress(OSError):
                self.socket_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            sock.listen(64)
        except OSError as exc:
            with contextlib.suppress(OSError):
                sock.close()
            raise ServerError(
                f"cannot listen on {self.socket_path}: {exc.strerror or exc}",
                hint=self._bind_hint(exc),
            ) from exc
        sock.settimeout(_ACCEPT_POLL_SECONDS)
        self._server_sock = sock
        self._bound = True
        logger.info("server: listening on %s (api %d)", self.socket_path, API_VERSION)

    def _bind_hint(self, exc: OSError) -> str:
        parent = self.socket_path.parent
        if exc.errno == errno.EACCES:
            return f"{parent} is not writable by this user — choose a --socket path you own"
        if exc.errno == errno.ENOENT:
            return f"{parent} does not exist and could not be created"
        # AF_UNIX paths are capped near 108 bytes. CPython raises a bare
        # OSError("AF_UNIX path too long") with NO errno for this, so the
        # length is the reliable signal — and it is the failure a deep tmp
        # dir actually hits.
        length = len(str(self.socket_path).encode("utf-8"))
        if exc.errno == errno.ENAMETOOLONG or length >= _AF_UNIX_PATH_MAX:
            return (
                f"the socket path is {length} bytes; AF_UNIX allows about "
                f"{_AF_UNIX_PATH_MAX} — use a shorter --socket path"
            )
        return "pass a different --socket path (default: $XDG_RUNTIME_DIR/organize-core.sock)"

    def _socket_is_live(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(str(self.socket_path))
        except OSError:
            return False
        finally:
            with contextlib.suppress(OSError):
                probe.close()
        return True

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return  # tests run the server on a worker thread
        for signum in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, lambda *_: self._stop.set())

    def _warm_index(self) -> None:
        """Load the snapshot, then catch up with the disk (spec 09 §4: the
        server holds a warm index so interactive calls stay under 100 ms)."""
        self.index.load()
        with self._rwlock.write():
            self.index.scan()
            self.index.flush()

    def _accept_loop(self) -> None:
        sock = self._server_sock
        assert sock is not None
        while not self._stop.is_set():
            try:
                client, _ = sock.accept()
            except TimeoutError:
                if self._idle_expired():
                    logger.info(
                        "server: idle for %.1fs — shutting down", self.idle_timeout_seconds
                    )
                    return
                continue
            except OSError as exc:
                if self._stop.is_set() or exc.errno in (errno.EBADF, errno.EINVAL):
                    return
                raise
            self._touch()
            client.settimeout(_ACCEPT_POLL_SECONDS)
            conn = _Connection(self, client, next(self._conn_ids))
            with self._connections_lock:
                self._connections[conn.id] = conn
            conn.start()

    def _connection_finished(self, conn: _Connection) -> None:
        with self._connections_lock:
            self._connections.pop(conn.id, None)
        conn.close()

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _begin_request(self) -> None:
        with self._inflight_lock:
            self._inflight += 1

    def _end_request(self) -> None:
        with self._inflight_lock:
            self._inflight -= 1
        self._touch()

    def _idle_expired(self) -> bool:
        """Idle == no in-flight request AND no client attached.

        A connected client is not idle even when it is silent: a Neovim
        client holding an `events.subscribe` stream sends nothing for
        minutes at a time, and shutting down under it dropped the
        index-updated/op-progress stream with no notification. Only
        `_last_activity` and `_inflight` used to be consulted.
        """
        if self.idle_timeout_seconds is None or self.idle_timeout_seconds <= 0:
            return False
        with self._inflight_lock:
            if self._inflight:
                return False
        with self._connections_lock:
            if self._connections:
                return False
        return (time.monotonic() - self._last_activity) > self.idle_timeout_seconds

    # --- events ----------------------------------------------------------

    def emit(self, event: str, data: dict[str, Any]) -> int:
        """Push ``event`` to every subscribed connection; returns the number
        of connections it reached."""
        line = encode_event(event, data)
        with self._connections_lock:
            targets = [c for c in self._connections.values() if event in c.subscriptions]
        for conn in targets:
            conn.send(line)
        if targets:
            self._touch()
        return len(targets)

    # --- dispatch --------------------------------------------------------

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Route one request per RPC_METHODS. Unknown method ⇒
        METHOD_NOT_FOUND. Mutating methods (op.*, meta.set, folder.create,
        index.reindex, auto.apply) go through the WriterQueue. OrganizeError
        ⇒ ORGANIZE_ERROR response, never a connection drop."""
        return self._dispatch_for(None, method, params)

    def _dispatch_for(
        self, conn: _Connection | None, method: str, params: dict[str, Any]
    ) -> Any:
        handler = self._handlers.get(method)
        if handler is None:
            raise RpcException(
                RpcError(
                    METHOD_NOT_FOUND,
                    f"unknown method {method!r}",
                    {"known_methods": list(RPC_METHODS)},
                )
            )
        if method not in MUTATING_METHODS:
            with self._rwlock.read():
                return handler(conn, params)

        self.emit(EVENT_OP_PROGRESS, {"method": method, "phase": "start", "params": _echo(params)})

        def run() -> Any:
            with self._rwlock.write():
                return handler(conn, params)

        try:
            result = self.writer.submit(run)
        except Exception as exc:
            self.emit(
                EVENT_OP_PROGRESS,
                {"method": method, "phase": "failed", "error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        self.emit(
            EVENT_OP_PROGRESS,
            {"method": method, "phase": "complete", "result": _summarize(result)},
        )
        if method in INDEX_CHANGING_METHODS:
            self.emit(EVENT_INDEX_UPDATED, {"method": method, "stats": self.index.stats()})
        return result

    # --- handlers: reads -------------------------------------------------

    #: Params `session.start` understands besides the filters themselves.
    _SESSION_START_RESERVED: frozenset[str] = frozenset({"filters", "session_id", "actor", "dry_run"})

    def _session_start(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        """``session.start`` (spec 03 §2).

        Filters may be nested under ``filters`` or passed at the top level,
        matching ``search.query`` — and an unknown top-level key is a HARD
        ERROR naming the key. Previously a top-level ``{"tags": [...]}`` was
        silently ignored and the client got a full, unfiltered session back:
        a silent wrong answer where the rest of this API is loud.
        """
        raw_filters = empty_array_as_object(params.get("filters") or {})
        if not isinstance(raw_filters, dict):
            raise _invalid_params("'filters' must be an object of filter=value pairs")
        top_level = {k: v for k, v in params.items() if k not in self._SESSION_START_RESERVED}
        overlap = set(top_level) & set(raw_filters)
        if overlap:
            raise _invalid_params(
                f"filter(s) {sorted(overlap)} given both at the top level and inside 'filters'"
            )
        raw_filters = {**raw_filters, **top_level}
        criteria = QueryCriteria.from_filter_args(raw_filters) if raw_filters else None
        session = start_session(self.index, criteria)
        with self._sessions_lock:
            self._sessions[session.session_id] = session
        counts = session.counts()
        return {
            "session_id": session.session_id,
            "state": session.state,
            "captures": [asdict(record) for record in session.captures],
            "counts": asdict(counts),
        }

    def _note_get(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        # Goes through _record_for so note.get and the op.* methods cannot
        # drift on how a missing note is reported (this used to be a second,
        # subtly different copy of the same lookup).
        record = self._record_for(_require(params, "path"))
        path = Path(record.path)
        payload: dict[str, Any] = {"record": asdict(record), "parse_error": False}
        try:
            document = load_file(path)
        except FrontmatterError as exc:
            logger.warning("server: note.get %s has unparseable frontmatter (%s)", path, exc)
            payload["parse_error"] = True
            payload["frontmatter"] = {}
            payload["body"] = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ServerError(
                f"cannot read {path}: {exc}", hint="check file permissions"
            ) from exc
        else:
            payload["frontmatter"] = dict(document.frontmatter.fields)
            payload["body"] = document.body
        return payload

    def _suggest_for_note(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        path = self._vault_path(_require(params, "path"))
        record = self.index.get(path) or self.index.update_file(path)
        if record is None:
            raise ServerError(
                f"no note at {path}", hint="index the note (index.reindex) or check the path"
            )
        capture = CaptureFeaturesView.from_record(record)
        candidates = generate_candidates(self._candidate_folders())
        scored = rank_suggestions(
            capture,
            candidates,
            self.config.suggestions,
            self._learning_data(),
            now=time.time(),
            archive_path=str(self._archive_folder()),
        )
        matches = resolve_routes(list(record.normalized_tags or record.tags or []), self.config)
        merged = merge_route_suggestions(matches, scored)
        return [asdict(suggestion) for suggestion in merged]

    def _search_query(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        raw = empty_array_as_object(params.get("criteria", params))
        if not isinstance(raw, dict):
            raise _invalid_params("'criteria' must be an object of filter=value pairs")
        criteria = QueryCriteria.from_filter_args(
            {k: v for k, v in raw.items() if k not in ("session_id", "actor", "dry_run")}
        )
        return [asdict(record) for record in self.index.query(criteria)]

    def _routes_resolve(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        tags = params.get("tags")
        if tags is None:
            path = self._vault_path(_require(params, "path"))
            record = self.index.get(path) or self.index.update_file(path)
            if record is None:
                raise ServerError(f"no note at {path}", hint="check the path is inside the vault")
            tags = list(record.tags or [])
        if not isinstance(tags, list):
            raise _invalid_params("'tags' must be an array of strings")
        matches = resolve_routes([str(tag) for tag in tags], self.config)
        return [
            {
                "route_name": match.route_name,
                "destination": str(match.destination),
                "is_folder": match.is_folder,
                "mode": match.route.mode,
                "auto": match.route.auto,
                "description": match.route.description,
                "template": match.route.template,
                "suggestion": asdict(match.as_suggestion()),
            }
            for match in matches
        ]

    def _events_subscribe(self, conn: _Connection | None, params: dict[str, Any]) -> Any:
        if conn is None:
            raise ServerError(
                "events.subscribe needs a client connection",
                hint="call it over the socket, not through OrganizeServer.dispatch()",
            )
        requested = params.get("events") or list(EVENT_TYPES)
        if not isinstance(requested, list):
            raise _invalid_params("'events' must be an array of event names")
        unknown = [str(e) for e in requested if str(e) not in EVENT_TYPES]
        if unknown:
            raise _invalid_params(
                f"unknown event(s) {unknown}; known events: {list(EVENT_TYPES)}"
            )
        conn.subscriptions.update(str(event) for event in requested)
        return {"subscribed": sorted(conn.subscriptions), "connection_id": conn.id}

    # --- handlers: writes ------------------------------------------------

    def _op_move(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        record = self._record_for(_require(params, "path"))
        destination = self._vault_path(_require(params, "destination"))
        ctx = self._context(params)
        result = move_to_destination(ctx, record, destination)
        # Learning is recorded by `ctx.on_record`, off the ActionRecord that
        # was actually written (12 §2 "Uses" #2) — not by a second write here.
        return asdict(result)

    def _op_merge_preview(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        record = self._record_for(_require(params, "path"))
        target = self._vault_path(_require(params, "target"))
        content, snapshot = merge_preview(self._context(params), record, target)
        return {"content": content, "snapshot": asdict(snapshot)}

    def _op_merge_commit(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        record = self._record_for(_require(params, "path"))
        target = self._vault_path(_require(params, "target"))
        snapshot = _snapshot_from(params.get("snapshot"))
        result = merge_into_note(
            self._context(params),
            record,
            target,
            edited_content=params.get("content"),
            target_snapshot=snapshot,
        )
        return asdict(result)

    def _op_archive(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        record = self._record_for(_require(params, "path"))
        return asdict(archive_capture(self._context(params), record))

    def _meta_set(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        """``meta.set`` (spec 07 + 05 §5).

        Every incoming value goes through the SAME ``metadata_fields``
        coercion the CLI uses (``config.coerce_metadata_value``): list
        splitting, kebab normalization, enum membership, boolean/number
        typing. Spec 10 §3 puts ``metadata_fields`` in core config so that
        "CLI and UI can never disagree"; while this handler passed
        ``params['changes']`` straight to ``update_frontmatter``, the nvim
        client — whose only write path is RPC — could write
        ``tags: ['foo, Bar Baz']`` and ``importance: totally-invalid`` into
        the vault, failing doc 07 acceptance tests 1 and 2 on this boundary.

        ``append = false`` on a list field REPLACES rather than merges, same
        as ``organize set-meta`` (``replace_keys``).
        """
        path = self._vault_path(_require(params, "path"))
        changes = empty_array_as_object(_require(params, "changes"))
        if not isinstance(changes, dict):
            raise _invalid_params("'changes' must be an object of field→value pairs")
        fields = metadata_fields_by_key(self.config)
        coerced: dict[str, Any] = {}
        replace_keys: list[str] = []
        for key, value in changes.items():
            entry = fields.get(str(key))
            coerced[str(key)] = coerce_metadata_value(entry, str(key), value)
            if entry is not None and entry.type == "list" and not entry.append:
                replace_keys.append(str(key))
        return asdict(
            update_frontmatter(
                self._context(params), path, coerced, replace_keys=frozenset(replace_keys)
            )
        )

    def _meta_fields(self, _conn: _Connection | None, _params: dict[str, Any]) -> Any:
        """``meta.fields`` — the doc-07 field definitions, completion values
        resolved (spec 07 "Config schema"; spec 10 §3).

        Doc 07 calls completion "what makes tag entry fast and consistent",
        and doc 10 §3 puts ``metadata_fields`` in core config precisely so
        the UI reads it FROM the core. Neither surface exposed it, and
        ``VaultIndex.values_of`` — which computes ``complete = "existing"``
        — had zero callers outside its own tests, so the feature worked
        in-core and was unreachable from any client.
        """
        out: list[dict[str, Any]] = []
        for entry in self.config.metadata_fields:
            out.append(
                {
                    "key": entry.key,
                    "type": entry.type,
                    "keymap": entry.keymap,
                    "prompt": entry.prompt,
                    "append": entry.append,
                    "complete": entry.complete,
                    "values": list(entry.values),
                    "normalize": entry.normalize,
                    "completions": self._completions_for(entry),
                }
            )
        return {"fields": out}

    def _meta_values(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        """``meta.values`` — distinct values of one frontmatter key across the
        indexed vault (spec 07 ``complete = "existing"``)."""
        key = str(_require(params, "key"))
        return {"key": key, "values": list(self.index.values_of(key))}

    def _completions_for(self, entry: MetadataFieldConfig) -> list[str]:
        if entry.complete == "existing":
            return list(self.index.values_of(entry.key))
        if isinstance(entry.complete, list):
            return [str(value) for value in entry.complete]
        if entry.type == "enum":
            return list(entry.values)
        return []

    def _folder_create(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        para_type = str(_require(params, "para_type"))
        name = str(_require(params, "name"))
        return asdict(new_folder(self._context(params), para_type, name))

    def _folder_list(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        """``folder.list`` — every immediate PARA subfolder, with its spec 11
        §3 description where one exists. Read-only; never queued.

        Enumerated from DISK (``VaultIndex.para_subfolders``), not from the
        indexed notes, so a freshly created and still-empty folder is
        offerable as a destination — the picker's whole job is choosing where
        a note goes, and a folder is a legitimate answer before it holds
        anything. Uncapped for the same reason: capping is a RANKING concern
        (``suggest.for_note``), and a browse list that silently omits folders
        cannot serve as the 03 §3 "see everything" fallback.

        This exists because spec 10 §1 makes the core the only reader of the
        vault: a thin client may not walk the filesystem itself, so without
        this method the nvim destination picker has no source for the list
        at all.

        The optional ``para_type`` FILTER param addresses a
        ``vault.para_folders`` KEY, not a type value, so it is plural
        (``projects``); the singular is accepted and normalized to the key.
        The emitted ``type`` is the singular type VALUE.
        """
        requested = params.get("para_type")
        keys = (
            list(self.config.vault.para_folders)
            if requested is None
            else [PARA_TYPE_TO_KEY.get(str(requested).strip().casefold(), str(requested))]
        )
        folders: list[dict[str, Any]] = []
        for key in keys:
            # `type` is the SINGULAR ParaType: every type VALUE on the wire is
            # singular. The `para_type` REQUEST param above is the exception —
            # it addresses a `vault.para_folders` KEY, so it takes either form.
            para_type = PARA_KEY_TO_TYPE.get(key, key)
            for folder in self.index.para_subfolders(key):
                folders.append(self._folder_entry(folder, para_type=para_type))
        return {"folders": folders}

    def _folder_children(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        """``folder.children`` — one directory level for 03 §3 browsing:
        subfolders (with descriptions) plus the notes directly inside."""
        folder = self._vault_folder(_require(params, "path"))
        subdirs, notes = self.index.folder_children(folder)
        return {
            "dirs": [self._folder_entry(subdir) for subdir in subdirs],
            "notes": [
                {
                    "path": record.path,
                    "title": record.title,
                    "aliases": list(record.aliases),
                    "para_type": record.para_type,
                }
                for record in notes
            ],
        }

    def _folder_entry(self, folder: Path, *, para_type: str | None = None) -> dict[str, Any]:
        """One folder as the wire shape. ``description`` is OMITTED rather
        than null when the folder has none, so a client can test presence
        without a per-language null dance."""
        entry: dict[str, Any] = {"path": str(folder), "name": folder.name}
        if para_type is not None:
            entry["type"] = para_type
        description = get_description(folder, self.index, self.config)
        if description:
            entry["description"] = description
        return entry

    def _index_reindex(self, _conn: _Connection | None, _params: dict[str, Any]) -> Any:
        return self.index.full_reindex()

    def _auto_not_implemented(self, _conn: _Connection | None, _params: dict[str, Any]) -> Any:
        raise RpcException(
            RpcError(
                NOT_IMPLEMENTED,
                f"auto-organize: {_NOT_IMPLEMENTED_MESSAGE}",
                {
                    "kind": "NotImplemented",
                    "spec": "13",
                    "hint": "auto.propose/auto.apply exist from day one and answer this "
                    "until the automatic-organize phase ships (spec 13 §3)",
                },
            )
        )

    # --- helpers ---------------------------------------------------------

    def _context(self, params: dict[str, Any]) -> OperationContext:
        """The per-call :class:`OperationContext`.

        ``suggestions_shown`` / ``chosen_rank`` / ``durations_ms`` /
        ``auto_tags_present`` / ``filters`` are per-CALL decision context
        (spec 12 §2), so they are read off the op params. They used to be
        silently discarded, which made 12 §3's acceptance test — "a session
        of 5 actions yields 5 records with … correct chosen_ranks" —
        unsatisfiable through the product, and left
        ``actions stats --json`` reporting ``top_accept_rate: null`` forever.
        """
        return OperationContext(
            config=self.config,
            index=self.index,
            oplog=self.oplog,
            recorder=self.recorder,
            backup_dir=self.backup_dir,
            dry_run=bool(params.get("dry_run", False)),
            actor=str(params.get("actor") or "matt"),
            session_id=params.get("session_id"),
            suggestions_shown=_suggestions_shown_from(params.get("suggestions_shown")),
            chosen_rank=_opt_rank(params.get("chosen_rank")),
            durations_ms=_durations_from(params.get("durations_ms")),
            auto_tags_present=_string_list_from(
                params.get("auto_tags_present"), "auto_tags_present"
            ),
            filters=_filters_from(params.get("filters")),
            describe=lambda folder: get_description(folder, self.index, self.config),
            on_record=self._learn_from_action,
        )

    def _vault_path(self, value: Any) -> Path:
        """Resolve a client-supplied path against the vault, refusing any
        path that escapes it (spec 05 §1: the core is the only writer, so it
        is the only place this check can live)."""
        text = str(value).strip()
        if not text:
            raise _invalid_params("path must be a non-empty string")
        candidate = Path(text)
        root = Path(os.path.normpath(str(self.config.vault.root)))
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = Path(os.path.normpath(str(candidate)))
        if candidate != root and root not in candidate.parents:
            raise ServerError(
                f"{candidate} is outside the vault {root}",
                hint="paths are vault-relative or absolute inside the vault",
            )
        return candidate

    def _vault_folder(self, value: Any) -> Path:
        """Resolve a client-supplied path that must name a FOLDER in the
        vault.

        Both failures are ``VaultError``, for the reason spelled out in
        :meth:`_record_for`: ``error.data.kind`` is what a client branches
        on, and ``ServerError`` invites a reconnect/retry when the only
        useful response is to fix the path.
        """
        try:
            path = self._vault_path(value)
        except ServerError as exc:
            raise VaultError(str(exc), hint=exc.hint) from exc
        if not path.is_dir():
            raise VaultError(
                f"folder not found: {path}",
                hint=f"pass a folder path relative to the vault root ({self.config.vault.root}) "
                "or an absolute path inside it",
            )
        return path

    def _record_for(self, value: Any) -> NoteRecord:
        """Resolve a client-supplied path to the indexed record every fileop
        requires (05 §2 / 08 §A14 — never a bare string).

        A path that names no note is a ``VaultError``, NOT a ``ServerError``:
        the server is fine, the request was wrong. This matters because
        ``error.data.kind`` is what a client branches on — ``ServerError``
        invites a reconnect/retry, while the only useful response here is to
        fix the path. It also keeps the two frontends aligned; ``organize
        move`` already raised ``VaultError`` for the same mistake, and
        ``test_the_two_doors_reject_the_same_bad_move`` pins that they agree.
        """
        path = self._vault_path(value)
        record = self.index.get(path)
        if record is None and path.is_file():
            record = self.index.update_file(path)
        if record is None:
            if not path.is_file():
                raise VaultError(
                    f"note not found: {path}",
                    hint=f"pass a path relative to the vault root ({self.config.vault.root}) "
                    "or an absolute path",
                )
            raise VaultError(
                f"{path} is inside the vault but not indexable",
                hint="check [vault] ignore_patterns / max_file_size, or reindex",
            )
        return record

    def _candidate_folders(self) -> dict[str, list[str]]:
        folders: dict[str, list[str]] = {}
        for key in self.config.vault.para_folders:
            if key == "archives":
                continue
            folders[key] = [str(path) for path in self.index.para_subfolders(key)]
        return folders

    def _archive_folder(self) -> Path:
        # `.resolve()` matches `cli._archive_folder`; without it a symlinked
        # vault root made the two doors emit DIFFERENT archive-suggestion
        # paths for the same vault (04 §1).
        archives = self.config.vault.para_folders.get("archives", "archive")
        path = Path(self.config.vault.root) / archives / self.config.vault.archive_capture_path
        try:
            return path.resolve()
        except OSError:  # pragma: no cover - resolve() is non-strict
            return path.absolute()

    def _learning_data(self) -> LearningData:
        with self._learning_lock:
            if self._learning is None:
                self._learning = load_learning(self.paths.learning_path)
            return self._learning

    def _learn_from_action(self, record: ActionRecord) -> None:
        """Fold one WRITTEN ActionRecord into learning.json (12 §2 "Uses" #2).

        Wired to ``OperationContext.on_record``, so it fires exactly once per
        persisted record instead of running as a second, independent write
        alongside the recorder — the two stores could otherwise disagree
        (a lost action line still updated learning.json). Spec 03 §6:
        accept/merge fire ``record_move`` with the destination FOLDER, which
        ``learn.destination_from_action`` derives from the record's targets.

        Decay runs here too, through ``learn.maybe_apply_decay`` — 04 §3 step
        6 says "at most once per session or per day", NOT "never". Nothing in
        production called ``apply_decay``, so the 90-day eviction and the
        ``max_history`` cap were dead code. The day gate lives in ``learn``
        so this root and the CLI share one schedule.
        """
        with self._learning_lock:
            data = self._learning
            if data is None:
                data = load_learning(self.paths.learning_path)
            now = time.time()
            data = maybe_apply_decay(data, self.config.suggestions.learning, now=now)
            updated = record_action(data, record, now=now)
            if updated is None:
                self._learning = data
                return
            self._learning = updated
            save_learning(self.paths.learning_path, self._learning)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _require(params: dict[str, Any], key: str) -> Any:
    if key not in params or params[key] is None:
        raise _invalid_params(f"missing required parameter {key!r}")
    return params[key]


def _invalid_params(message: str) -> RpcException:
    return RpcException(RpcError(INVALID_PARAMS, message))


def _suggestions_shown_from(raw: Any) -> tuple[SuggestionShown, ...]:
    """Validate ``params['suggestions_shown']`` (spec 12 §2 counterfactual)."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _invalid_params("'suggestions_shown' must be an array of {path, score, rank, reasons}")
    try:
        return tuple(SuggestionShown.from_json(item) for item in raw)
    except OrganizeError as exc:
        raise _invalid_params(f"'suggestions_shown' is malformed: {exc}") from exc


def _opt_rank(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _invalid_params("'chosen_rank' must be an integer (1 == the top suggestion)")
    return raw


def _filters_from(raw: Any) -> dict[str, Any]:
    """``params['filters']`` as recorded on the ActionRecord (spec 12 §2).

    This was ``dict(raw or {})``, which raised ValueError on a non-empty
    ARRAY — a client TYPE error escaping as -32603 INTERNAL_ERROR, i.e. the
    core reporting its own fault for a malformed request. Same shape and
    message as ``session.start``'s ``filters``, so one field name never
    validates two ways.
    """
    if raw is None:
        return {}
    raw = empty_array_as_object(raw)
    if not isinstance(raw, dict):
        raise _invalid_params("'filters' must be an object of filter=value pairs")
    return dict(raw)


def _string_list_from(raw: Any, field: str) -> tuple[str, ...]:
    """A LIST-typed request field of plain strings (spec 12 §2 context).

    This was inlined as ``tuple(str(tag) for tag in (raw or ()))``, which
    failed two ways on a malformed request: a SCALAR raised TypeError and
    escaped as -32603 INTERNAL_ERROR — the core reporting its own fault for
    a client type error — and a bare STRING silently exploded into one tag
    per CHARACTER, a wrong answer written straight into the action record.

    The rules mirror ``actions._str_tuple``, which validates this same field
    when a record is read back, so the door cannot accept a shape the record
    layer would later reject.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _invalid_params(f"{field!r} must be an array of strings")
    for item in raw:
        if not isinstance(item, str):
            raise _invalid_params(
                f"{field!r} must contain only strings, got {type(item).__name__}"
            )
    return tuple(raw)


def _durations_from(raw: Any) -> dict[str, int]:
    if raw is None:
        return {}
    raw = empty_array_as_object(raw)
    if not isinstance(raw, dict):
        raise _invalid_params("'durations_ms' must be an object of phase→milliseconds")
    out: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _invalid_params(f"'durations_ms.{key}' must be a number of milliseconds")
        out[str(key)] = int(value)
    return out


def _snapshot_from(raw: Any) -> FileSnapshot | None:
    if raw is None:
        return None
    # NOT routed through `empty_array_as_object`: an empty snapshot is never
    # a valid one, so coercing `[]` to `{}` would only swap this precise
    # message for a vaguer "missing or malformed field" on the same rejection.
    if not isinstance(raw, dict):
        raise _invalid_params("'snapshot' must be the object returned by op.merge_preview")
    try:
        return FileSnapshot(
            path=str(raw["path"]), mtime=float(raw["mtime"]), sha256=str(raw["sha256"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid_params(
            f"'snapshot' is missing or has a malformed field: {exc}"
        ) from exc


def _echo(params: dict[str, Any]) -> dict[str, Any]:
    """The subset of a request safe (and useful) to mirror in an event."""
    return {k: v for k, v in params.items() if k in ("path", "target", "destination", "name")}


def _summarize(result: Any) -> dict[str, Any]:
    """Compact event payload for a finished operation."""
    if isinstance(result, dict):
        return {
            k: v
            for k, v in result.items()
            if k in ("ok", "operation", "source", "destination", "dry_run", "total", "duration")
        }
    return {}
