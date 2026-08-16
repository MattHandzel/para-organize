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
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from organize_core.config import Config
from organize_core.paths import CorePaths

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

# JSON-RPC 2.0 error codes (standard) + implementation range.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# -32000..-32099: OrganizeError subclasses map here with the taxonomy name
# in error.data.kind and any hint in error.data.hint (loud failure, 09 §1.5)
ORGANIZE_ERROR = -32000


@dataclass(frozen=True)
class RpcError:
    code: int
    message: str
    data: dict[str, Any] | None = None


class SingleInstanceLock:
    """Advisory lock on CorePaths.lock_path; a second ``organize serve``
    raises AlreadyRunning (spec 10 §1). Stale locks (dead pid) are
    reclaimed."""

    def __init__(self, lock_path: Path) -> None:
        raise NotImplementedError

    def acquire(self) -> None:
        raise NotImplementedError

    def release(self) -> None:
        raise NotImplementedError


class WriterQueue:
    """All mutating operations funnel through here, executed one at a time
    (spec 10 §1) — the vault-side counterpart of the concurrent-
    modification check (10 §4)."""

    def submit(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` when it reaches the head of the queue; blocks the
        calling connection handler until done, returns its result."""
        raise NotImplementedError


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
        raise NotImplementedError

    def serve_forever(self) -> None:
        """Bind socket, acquire single-instance lock, accept connections
        (each greeted with the apiVersion line), dispatch until idle
        timeout or signal; teardown removes socket + lock."""
        raise NotImplementedError

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """Route one request per RPC_METHODS. Unknown method ⇒
        METHOD_NOT_FOUND. Mutating methods (op.*, meta.set, folder.create,
        index.reindex, auto.apply) go through the WriterQueue. OrganizeError
        ⇒ ORGANIZE_ERROR response, never a connection drop."""
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError


def encode_response(id_: int | str | None, result: Any = None, error: RpcError | None = None) -> str:
    """One newline-terminated JSON-RPC 2.0 response line."""
    raise NotImplementedError


def decode_request(line: str) -> tuple[int | str | None, str, dict[str, Any]]:
    """Parse one request line → (id, method, params). Malformed ⇒ raises
    with PARSE_ERROR/INVALID_REQUEST semantics for the caller to encode."""
    raise NotImplementedError
