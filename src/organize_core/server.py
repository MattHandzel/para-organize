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
from organize_core.actions import ActionRecorder
from organize_core.config import Config
from organize_core.errors import AlreadyRunning, OrganizeError, ServerError
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
from organize_core.index import NoteRecord, QueryCriteria, VaultIndex
from organize_core.learn import LearningData, load_learning, record_move, save_learning
from organize_core.paths import CorePaths
from organize_core.routes import merge_route_suggestions
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
    "folder.create",       # (para_type, name) → OperationResult      spec 05 §6
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
#: Cap on one request line; a client that never sends "\n" cannot OOM us.
_MAX_LINE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class RpcError:
    code: int
    message: str
    data: dict[str, Any] | None = None


class RpcException(Exception):
    """Carries an :class:`RpcError` (and, when known, the request id) out of
    decode/dispatch so the connection loop can encode it. Never fatal to the
    connection — the contract is "error response, never a dropped socket"."""

    def __init__(self, error: RpcError, *, request_id: int | str | None = None) -> None:
        super().__init__(error.message)
        self.error = error
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
                            error=RpcError(
                                INVALID_REQUEST,
                                f"request line exceeds {_MAX_LINE_BYTES} bytes",
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
                error=RpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"),
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
            "folder.create": self._folder_create,
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
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            if self._socket_is_live():
                raise AlreadyRunning(
                    f"a server is already listening on {self.socket_path}",
                    hint="stop it first, or serve on a different --socket path",
                )
            with contextlib.suppress(OSError):
                self.socket_path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        sock.listen(64)
        sock.settimeout(_ACCEPT_POLL_SECONDS)
        self._server_sock = sock
        self._bound = True
        logger.info("server: listening on %s (api %d)", self.socket_path, API_VERSION)

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
        if self.idle_timeout_seconds is None or self.idle_timeout_seconds <= 0:
            return False
        with self._inflight_lock:
            if self._inflight:
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

    def _session_start(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        raw_filters = params.get("filters") or {}
        if not isinstance(raw_filters, dict):
            raise _invalid_params("'filters' must be an object of filter=value pairs")
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
        path = self._vault_path(_require(params, "path"))
        record = self.index.get(path)
        if record is None:
            record = self.index.update_file(path)
        if record is None:
            raise ServerError(
                f"no note at {path}",
                hint="check the path is inside the vault and the file exists",
            )
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
        raw = params.get("criteria", params)
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
        if result.ok and not result.dry_run:
            self._record_learning(record, destination)
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
        if result.ok and not result.dry_run:
            self._record_learning(record, target.parent)
        return asdict(result)

    def _op_archive(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        record = self._record_for(_require(params, "path"))
        return asdict(archive_capture(self._context(params), record))

    def _meta_set(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        path = self._vault_path(_require(params, "path"))
        changes = _require(params, "changes")
        if not isinstance(changes, dict):
            raise _invalid_params("'changes' must be an object of field→value pairs")
        return asdict(update_frontmatter(self._context(params), path, changes))

    def _folder_create(self, _conn: _Connection | None, params: dict[str, Any]) -> Any:
        para_type = str(_require(params, "para_type"))
        name = str(_require(params, "name"))
        return asdict(new_folder(self._context(params), para_type, name))

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
        return OperationContext(
            config=self.config,
            index=self.index,
            oplog=self.oplog,
            recorder=self.recorder,
            backup_dir=self.backup_dir,
            dry_run=bool(params.get("dry_run", False)),
            actor=str(params.get("actor") or "matt"),
            session_id=params.get("session_id"),
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

    def _record_for(self, value: Any) -> NoteRecord:
        path = self._vault_path(value)
        record = self.index.get(path)
        if record is None:
            record = self.index.update_file(path)
        if record is None:
            raise ServerError(
                f"no note at {path}",
                hint="check the path is inside the vault and the file exists",
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
        archives = self.config.vault.para_folders.get("archives", "archive")
        return Path(self.config.vault.root) / archives / self.config.vault.archive_capture_path

    def _learning_data(self) -> LearningData:
        with self._learning_lock:
            if self._learning is None:
                self._learning = load_learning(self.paths.learning_path)
            return self._learning

    def _record_learning(self, record: NoteRecord, destination_folder: Path) -> None:
        """Spec 03 §6: accept/merge fire ``record_move`` with the destination
        FOLDER. Decay is deliberately not applied here (04 §3.6)."""
        with self._learning_lock:
            data = self._learning
            if data is None:
                data = load_learning(self.paths.learning_path)
            self._learning = record_move(
                data, record, str(destination_folder), now=time.time()
            )
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


def _snapshot_from(raw: Any) -> FileSnapshot | None:
    if raw is None:
        return None
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
