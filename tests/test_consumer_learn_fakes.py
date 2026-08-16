"""Shared fakes for the ``learn`` + ``question_answer`` consumer suites.

This seat owns ``tests/test_consumer_learn*.py`` and
``tests/test_consumer_qa*.py``; the support module lives under the
``test_consumer_learn*`` glob so both suites can import it without touching
another seat's files. It contains no tests of its own.

Everything external is FAKE and local (spec 09 §1.4): Ollama is a
``http.server`` on 127.0.0.1, Whisper likewise, ``yt-dlp`` is a script
written into tmp. Nothing here reads the real vault, the real state dir,
the real network or the user's binaries.
"""

from __future__ import annotations

import hashlib
import json
import socket
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from organize_core import frontmatter
from organize_core.config import Config, ConsumerConfig, LLMConfig, VaultConfig
from organize_core.consumers.base import NotePayload, RunContext
from organize_core.llm import LLMClient, LLMResponse, get_client

# --- payload / config builders ---------------------------------------------


def write_note(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def payload_for(path: Path) -> NotePayload:
    """Build a NotePayload exactly as the runner's ingestion does (06 §1):
    parse through the ONE frontmatter module, hash the raw text."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    doc = frontmatter.parse(raw)
    fields = dict(doc.frontmatter.fields) if doc.frontmatter else {}
    return NotePayload(
        path=path,
        frontmatter=fields,
        content=doc.body,
        raw_text=raw,
        note_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def make_config(vault: Path, llm: LLMConfig | None = None) -> Config:
    return Config(vault=VaultConfig(root=vault), llm=llm or LLMConfig())


def consumer_config(name: str, ctype: str, **options: Any) -> ConsumerConfig:
    return ConsumerConfig(name=name, type=ctype, options=dict(options))


def run_context(vault: Path, llm: LLMClient | None = None, *, dry_run: bool = False) -> RunContext:
    return RunContext(config=make_config(vault), dry_run=dry_run, llm=llm)


# --- fake LLM client --------------------------------------------------------


@dataclass
class FakeLLM(LLMClient):
    """In-process ``LLMClient``: returns queued texts or raises queued
    exceptions. Records every call so tests can assert the 06 §3.2/§3.3
    call parameters (json_mode, temperature, max_tokens, timeout)."""

    responses: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "json_mode": json_mode,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("FakeLLM ran out of queued responses")
        nxt = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(nxt, BaseException):
            raise nxt
        text = nxt if isinstance(nxt, str) else json.dumps(nxt)
        from organize_core.llm import extract_json

        return LLMResponse(
            text=text,
            model="fake-model",
            backend="fake",
            duration_ms=1,
            json=extract_json(text) if json_mode else None,
        )

    def available(self) -> bool:
        return True


def cards_response(*cards: dict[str, Any]) -> str:
    return json.dumps({"cards": list(cards)})


# --- fake HTTP endpoints (Ollama, Whisper) ---------------------------------


@dataclass
class Reply:
    status: int = 200
    body: bytes = b"{}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: object) -> None:  # noqa: D102 - silence test noise
        return

    def _respond(self, body: bytes) -> None:
        server: FakeHTTPEndpoint = self.server.state  # type: ignore[attr-defined]
        server.requests.append({"path": self.path, "body": body})
        reply = server.next_reply()
        try:
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)
        except OSError:  # pragma: no cover - client hung up
            return

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        self._respond(self.rfile.read(length) if length else b"")

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._respond(b"")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request: object, client_address: object) -> None:
        return


@dataclass
class FakeHTTPEndpoint:
    """A localhost stand-in for Ollama or Whisper."""

    replies: list[Reply] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    _httpd: _Server | None = None
    _thread: threading.Thread | None = None

    def next_reply(self) -> Reply:
        if not self.replies:
            return Reply()
        if len(self.replies) == 1:
            return self.replies[0]
        return self.replies.pop(0)

    def queue_completion(self, text: str) -> None:
        """Queue an Ollama ``/api/generate`` envelope."""
        self.replies.append(
            Reply(body=json.dumps({"response": text, "model": "fake-model"}).encode("utf-8"))
        )

    def queue_raw(self, body: bytes, status: int = 200) -> None:
        self.replies.append(Reply(status=status, body=body))

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeHTTPEndpoint:
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.state = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@contextmanager
def http_endpoint() -> Iterator[FakeHTTPEndpoint]:
    """A started fake endpoint, stopped on exit.

    Deliberately a context manager rather than a pytest fixture: fixtures
    would have to be imported into each suite, where the parameter of the
    same name then shadows the import (ruff F811). ``with http_endpoint()``
    has no such problem and makes the lifetime obvious at the call site.
    """
    server = FakeHTTPEndpoint().start()
    try:
        yield server
    finally:
        server.stop()


def closed_port_url() -> str:
    """A 127.0.0.1 URL guaranteed to refuse connections (LLM-down tests)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def ollama_client(host: str, *, timeout: float = 5.0) -> LLMClient:
    return get_client(
        LLMConfig(
            backend="ollama",
            ollama_host=host,
            ollama_model="fake-model",
            timeout_seconds=timeout,
            retries=0,
        )
    )


# --- fake external binary ---------------------------------------------------


def write_fake_binary(directory: Path, name: str, script: str) -> Path:
    """A POSIX-sh stand-in for an external tool (never the real binary)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def fake_yt_dlp(directory: Path, vtt_body: str, *, exit_code: int = 0) -> list[str]:
    """argv for a yt-dlp stand-in that drops a .vtt next to the -o template."""
    payload = vtt_body.replace("'", "'\\''")
    script = f"""
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then shift; out="$1"; fi
  shift
done
dir=$(dirname "$out")
printf '%s' '{payload}' > "$dir/video.en.vtt"
exit {exit_code}
"""
    return [str(write_fake_binary(directory, "yt-dlp-fake", script))]
