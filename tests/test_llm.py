"""Tests for the ONE shared LLM client layer (spec 09 §2, 06 §3.2-3.3/§6,
11 §2, 12 §1, 08 §B1/§B11).

Isolation rules for this suite:

* The Ollama backend is exercised against a **local stdlib HTTP server**
  bound to ``127.0.0.1:0`` (ephemeral port) — never a real Ollama host.
* The claude-cli backend is exercised against a **fake executable script**
  generated into ``tmp_path`` — never the real ``claude`` CLI.
* No test reads or writes any real state directory or vault.

Assertions are exact values and real behavior (spec 09 §3: the old suite's
``>0`` assertions passed while the product was broken).
"""

from __future__ import annotations

import json
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from organize_core import llm as llm_module
from organize_core.config import LLMConfig
from organize_core.errors import ConfigError, LLMError, LLMUnavailable, OrganizeError
from organize_core.llm import (
    ClaudeCLIClient,
    LLMClient,
    LLMResponse,
    OllamaClient,
    get_client,
)

# ---------------------------------------------------------------------------
# Fake Ollama HTTP server (stdlib only, 127.0.0.1, ephemeral port)
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    method: str
    path: str
    body: bytes
    content_type: str | None


@dataclass
class Reply:
    status: int = 200
    body: bytes = b"{}"
    #: Stall BEFORE anything is sent — urllib has not seen a response yet, so
    #: the failure arrives wrapped in ``URLError``.
    delay: float = 0.0
    #: Stall AFTER the headers and a slice of the body — the realistic
    #: remote-Ollama failure (the model starts generating and wedges). urllib
    #: raises a bare ``TimeoutError`` here, which is a DIFFERENT arm of the
    #: client's taxonomy (08 §B11) and was never exercised.
    stall_after_headers: float = 0.0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: object) -> None:  # noqa: D102 - silence test noise
        return

    def _record_and_reply(self, body: bytes) -> None:
        state: FakeOllamaServer = self.server.state  # type: ignore[attr-defined]
        state.requests.append(
            RecordedRequest(
                method=self.command,
                path=self.path,
                body=body,
                content_type=self.headers.get("Content-Type"),
            )
        )
        reply = state.next_reply()
        if reply.delay:
            threading.Event().wait(reply.delay)
        if reply.stall_after_headers:
            try:
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                # Promise more than we send, so the client keeps reading.
                self.send_header("Content-Length", str(len(reply.body) + 64))
                self.end_headers()
                self.wfile.write(reply.body[: max(1, len(reply.body) // 2)])
                self.wfile.flush()
            except OSError:
                return
            threading.Event().wait(reply.stall_after_headers)
            return
        try:
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)
        except OSError:
            # The client gave up (timeout test) — not a server problem.
            return

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        self._record_and_reply(self.rfile.read(length) if length else b"")

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._record_and_reply(b"")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request: object, client_address: object) -> None:
        return  # broken pipes during timeout tests are expected


@dataclass
class FakeOllamaServer:
    replies: list[Reply] = field(default_factory=list)
    requests: list[RecordedRequest] = field(default_factory=list)
    _httpd: _Server | None = None
    _thread: threading.Thread | None = None

    def next_reply(self) -> Reply:
        if not self.replies:
            return Reply()
        if len(self.replies) == 1:
            return self.replies[0]
        return self.replies.pop(0)

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeOllamaServer:
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


@pytest.fixture()
def fake_ollama() -> Iterator[FakeOllamaServer]:
    server = FakeOllamaServer().start()
    try:
        yield server
    finally:
        server.stop()


def closed_port_url() -> str:
    """A 127.0.0.1 URL guaranteed to refuse connections."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def ollama_config(**overrides: object) -> LLMConfig:
    base: dict[str, object] = {
        "backend": "ollama",
        "ollama_model": "fake-model",
        "timeout_seconds": 5.0,
        "retries": 0,
    }
    base.update(overrides)
    return LLMConfig(**base)  # type: ignore[arg-type]


def generate_body(response: str, model: str = "fake-model") -> bytes:
    return json.dumps({"model": model, "response": response, "done": True}).encode("utf-8")


# ---------------------------------------------------------------------------
# Fake claude CLI (executable script in tmp — never the real CLI)
# ---------------------------------------------------------------------------

_FAKE_CLI_TEMPLATE = """#!{python}
import sys, time
argv = sys.argv[1:]
if "--version" in argv:
    sys.stdout.write({version!r})
    sys.exit({version_exit!r})
data = sys.stdin.read()
with open({record!r}, "w", encoding="utf-8") as fh:
    fh.write(repr(argv) + "\\n" + data)
time.sleep({sleep!r})
sys.stdout.buffer.write({out!r})
sys.stderr.buffer.write({err!r})
sys.exit({exit_code!r})
"""


def make_fake_cli(
    tmp_path: Path,
    *,
    name: str = "fake-claude",
    out: bytes = b"fake completion\n",
    err: bytes = b"",
    exit_code: int = 0,
    sleep: float = 0.0,
    version: str = "fake-claude 1.2.3\n",
    version_exit: int = 0,
) -> tuple[Path, Path]:
    """Write an executable fake ``claude`` and return (script, record_file)."""
    script = tmp_path / name
    record = tmp_path / f"{name}.record"
    script.write_text(
        _FAKE_CLI_TEMPLATE.format(
            python=sys.executable,
            version=version,
            version_exit=version_exit,
            record=str(record),
            sleep=sleep,
            out=out,
            err=err,
            exit_code=exit_code,
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script, record


def cli_config(**overrides: object) -> LLMConfig:
    base: dict[str, object] = {"backend": "claude-cli", "timeout_seconds": 10.0, "retries": 0}
    base.update(overrides)
    return LLMConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# OllamaClient — happy paths and exact request shape (06 §3.2-3.3)
# ---------------------------------------------------------------------------


def test_ollama_generate_returns_response_text(fake_ollama: FakeOllamaServer) -> None:
    fake_ollama.replies = [Reply(body=generate_body("hello world"))]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    result = client.generate("hello")

    assert isinstance(result, LLMResponse)
    assert result.text == "hello world"
    assert result.backend == "ollama"
    assert result.model == "fake-model"
    assert result.json is None
    assert result.duration_ms >= 0
    assert len(fake_ollama.requests) == 1


def test_ollama_request_payload_is_exactly_the_spec_shape(fake_ollama: FakeOllamaServer) -> None:
    """06 §3.2-3.3: stream off, explicit temperature, num_predict, format=json."""
    fake_ollama.replies = [Reply(body=generate_body('{"a": 1}'))]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    client.generate(
        "summarize this",
        system="be brief",
        json_mode=True,
        temperature=0.3,
        max_tokens=2048,
    )

    request = fake_ollama.requests[0]
    assert request.method == "POST"
    assert request.path == "/api/generate"
    assert request.content_type == "application/json"
    assert json.loads(request.body.decode("utf-8")) == {
        "model": "fake-model",
        "prompt": "summarize this",
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 2048},
        "system": "be brief",
        "format": "json",
    }


def test_ollama_omits_format_and_num_predict_when_not_requested(
    fake_ollama: FakeOllamaServer,
) -> None:
    fake_ollama.replies = [Reply(body=generate_body("plain"))]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    client.generate("hi")

    payload = json.loads(fake_ollama.requests[0].body.decode("utf-8"))
    assert "format" not in payload
    assert "system" not in payload
    assert payload["options"] == {"temperature": 0.3}


def test_ollama_json_mode_parses_the_completion(fake_ollama: FakeOllamaServer) -> None:
    fake_ollama.replies = [Reply(body=generate_body('{"utility": 8, "effort": "1.5h"}'))]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    result = client.generate("enrich", json_mode=True)

    assert result.json == {"utility": 8, "effort": "1.5h"}


def test_ollama_json_mode_survives_unparseable_completion(fake_ollama: FakeOllamaServer) -> None:
    """A model that ignores format=json degrades to json=None, not a crash."""
    fake_ollama.replies = [Reply(body=generate_body("I am not JSON at all"))]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    result = client.generate("enrich", json_mode=True)

    assert result.text == "I am not JSON at all"
    assert result.json is None


def test_ollama_reports_the_model_the_server_used(fake_ollama: FakeOllamaServer) -> None:
    fake_ollama.replies = [Reply(body=generate_body("x", model="other-model"))]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    assert client.generate("hi").model == "other-model"


# ---------------------------------------------------------------------------
# OllamaClient — 08 §B11 failure taxonomy (the crash class being fixed)
# ---------------------------------------------------------------------------


def test_ollama_timeout_raises_llm_error_not_timeout_error(
    fake_ollama: FakeOllamaServer,
) -> None:
    """08 §B11 regression: TimeoutError is the EXPECTED remote-Ollama failure
    and must never escape as a bare exception."""
    fake_ollama.replies = [Reply(body=generate_body("too late"), delay=3.0)]
    client = OllamaClient(
        fake_ollama.base_url, "fake-model", config=ollama_config(timeout_seconds=0.2)
    )

    with pytest.raises(LLMError) as excinfo:
        client.generate("hi")

    assert "timed out" in str(excinfo.value)
    assert excinfo.value.hint
    assert isinstance(excinfo.value.__cause__, (TimeoutError, OSError))


def test_ollama_non_json_envelope_raises_llm_error_not_json_decode_error(
    fake_ollama: FakeOllamaServer,
) -> None:
    """08 §B11 regression: json.JSONDecodeError is caught and translated."""
    fake_ollama.replies = [Reply(body=b"<html>502 Bad Gateway</html>")]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    with pytest.raises(LLMError) as excinfo:
        client.generate("hi")

    assert "non-JSON envelope" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


def test_ollama_connection_refused_raises_llm_unavailable() -> None:
    """08 §B11 regression: OSError (ECONNREFUSED) is caught, not propagated."""
    client = OllamaClient(closed_port_url(), "fake-model", config=ollama_config())

    with pytest.raises(LLMUnavailable) as excinfo:
        client.generate("hi")

    assert isinstance(excinfo.value, LLMError)  # taxonomy: LLMUnavailable ⊂ LLMError
    assert excinfo.value.hint


def test_ollama_http_error_status_raises_llm_error(fake_ollama: FakeOllamaServer) -> None:
    fake_ollama.replies = [Reply(status=404, body=b'{"error":"model not found"}')]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    with pytest.raises(LLMError) as excinfo:
        client.generate("hi")

    message = str(excinfo.value)
    assert "HTTP 404" in message
    assert "model not found" in message


def test_ollama_empty_completion_is_a_failure(fake_ollama: FakeOllamaServer) -> None:
    """The ABC contract: never return silently-empty output."""
    fake_ollama.replies = [Reply(body=generate_body("   \n  "))]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    with pytest.raises(LLMError, match="empty completion"):
        client.generate("hi")


def test_ollama_invalid_utf8_body_is_replaced_not_fatal(fake_ollama: FakeOllamaServer) -> None:
    """08 §B1 class on the HTTP seam: undecodable bytes must not crash."""
    fake_ollama.replies = [Reply(body=b'{"model": "fake-model", "response": "caf\xe9 latte"}')]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    result = client.generate("hi")

    assert result.text == "caf� latte"


def test_ollama_every_failure_is_an_organize_error(fake_ollama: FakeOllamaServer) -> None:
    """No raw urllib/socket/json exception may reach a caller."""
    fake_ollama.replies = [Reply(status=500, body=b"boom")]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    with pytest.raises(OrganizeError):
        client.generate("hi")


# ---------------------------------------------------------------------------
# OllamaClient — bounded retry with backoff (06 §6)
# ---------------------------------------------------------------------------


def test_ollama_retries_server_errors_then_succeeds(
    fake_ollama: FakeOllamaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_module, "RETRY_BACKOFF_SECONDS", 0.0)
    fake_ollama.replies = [
        Reply(status=503, body=b"unavailable"),
        Reply(body=generate_body("recovered")),
    ]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config(retries=2))

    assert client.generate("hi").text == "recovered"
    assert len(fake_ollama.requests) == 2


def test_ollama_retry_budget_is_exactly_retries_plus_one(
    fake_ollama: FakeOllamaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_module, "RETRY_BACKOFF_SECONDS", 0.0)
    fake_ollama.replies = [Reply(status=500, body=b"boom")]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config(retries=2))

    with pytest.raises(LLMError):
        client.generate("hi")

    assert len(fake_ollama.requests) == 3


def test_ollama_does_not_retry_client_errors(
    fake_ollama: FakeOllamaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_module, "RETRY_BACKOFF_SECONDS", 0.0)
    fake_ollama.replies = [Reply(status=400, body=b'{"error":"bad request"}')]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config(retries=2))

    with pytest.raises(LLMError):
        client.generate("hi")

    assert len(fake_ollama.requests) == 1


def test_ollama_does_not_retry_a_bad_envelope(
    fake_ollama: FakeOllamaServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_module, "RETRY_BACKOFF_SECONDS", 0.0)
    fake_ollama.replies = [Reply(body=b"not json")]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config(retries=2))

    with pytest.raises(LLMError):
        client.generate("hi")

    assert len(fake_ollama.requests) == 1


# ---------------------------------------------------------------------------
# OllamaClient — availability probe + config-required host/model (06 §2)
# ---------------------------------------------------------------------------


def test_ollama_available_true_when_server_answers(fake_ollama: FakeOllamaServer) -> None:
    fake_ollama.replies = [Reply(body=b'{"models": []}')]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    assert client.available() is True
    assert fake_ollama.requests[0].path == "/api/tags"
    assert fake_ollama.requests[0].method == "GET"


def test_ollama_available_false_and_silent_when_down() -> None:
    client = OllamaClient(closed_port_url(), "fake-model", config=ollama_config())

    assert client.available() is False


def test_ollama_available_false_on_error_status(fake_ollama: FakeOllamaServer) -> None:
    fake_ollama.replies = [Reply(status=500, body=b"nope")]
    client = OllamaClient(fake_ollama.base_url, "fake-model", config=ollama_config())

    assert client.available() is False


@pytest.mark.parametrize("bad_host", ["", "   ", None])
def test_ollama_requires_a_configured_host(bad_host: str | None) -> None:
    with pytest.raises(ConfigError) as excinfo:
        OllamaClient(bad_host, "fake-model", config=ollama_config())  # type: ignore[arg-type]

    assert "llm.ollama_host" in str(excinfo.value)


@pytest.mark.parametrize("bad_model", ["", "   ", None])
def test_ollama_requires_a_configured_model(bad_model: str | None) -> None:
    with pytest.raises(ConfigError) as excinfo:
        OllamaClient("http://127.0.0.1:1", bad_model, config=ollama_config())  # type: ignore[arg-type]

    assert "llm.ollama_model" in str(excinfo.value)


def test_ollama_host_trailing_slash_is_normalized(fake_ollama: FakeOllamaServer) -> None:
    fake_ollama.replies = [Reply(body=generate_body("ok"))]
    client = OllamaClient(fake_ollama.base_url + "/", "fake-model", config=ollama_config())

    client.generate("hi")

    assert fake_ollama.requests[0].path == "/api/generate"


def test_llm_module_contains_no_hardcoded_host_or_model_literals() -> None:
    """06 §2 / 08 §B18 regression: hardcoded hosts and the typo'd default
    model (`gemma4:e4b`) are exactly the defect being removed."""
    source = Path(llm_module.__file__).read_text(encoding="utf-8", errors="replace")
    for forbidden in (
        "matthandzel",
        "11434",
        "47770",
        "localhost",
        "127.0.0.1",
        "gemma",
    ):
        assert forbidden not in source, f"llm.py hardcodes {forbidden!r}"


# ---------------------------------------------------------------------------
# ClaudeCLIClient — 08 §B1 (encoding) and 06 §6 (timeout) contract
# ---------------------------------------------------------------------------


def test_claude_cli_sends_prompt_on_stdin_and_returns_stdout(tmp_path: Path) -> None:
    script, record = make_fake_cli(tmp_path, out=b"the answer\n")
    client = ClaudeCLIClient([str(script), "-p"], config=cli_config())

    result = client.generate("what is 2+2?")

    assert result.text == "the answer"
    assert result.backend == "claude-cli"
    assert result.model == "fake-claude"
    assert result.duration_ms >= 0
    recorded = record.read_text(encoding="utf-8").splitlines()
    assert recorded[0] == "['-p']"
    assert recorded[1] == "what is 2+2?"


def test_claude_cli_prefixes_the_system_prompt(tmp_path: Path) -> None:
    script, record = make_fake_cli(tmp_path)
    client = ClaudeCLIClient([str(script)], config=cli_config())

    client.generate("body text", system="you are terse")

    assert record.read_text(encoding="utf-8").split("\n", 1)[1] == "you are terse\n\nbody text"


def test_claude_cli_json_mode_appends_a_directive_and_parses_fenced_output(
    tmp_path: Path,
) -> None:
    script, record = make_fake_cli(
        tmp_path,
        out=b'Sure, here you go:\n```json\n{"next_action": "call bob", "utility": 8}\n```\n',
    )
    client = ClaudeCLIClient([str(script)], config=cli_config())

    result = client.generate("enrich this", json_mode=True)

    assert result.json == {"next_action": "call bob", "utility": 8}
    assert llm_module.JSON_MODE_DIRECTIVE in record.read_text(encoding="utf-8")


def test_claude_cli_invalid_utf8_stdout_does_not_crash(tmp_path: Path) -> None:
    """08 §B1 regression — the exact 3-month-outage crash class: subprocess
    text decoded strictly. errors='replace' means undecodable bytes degrade."""
    script, _ = make_fake_cli(tmp_path, out=b"caf\xe9 \x80\xff done\n")
    client = ClaudeCLIClient([str(script)], config=cli_config())

    result = client.generate("hi")

    assert "caf" in result.text
    assert "done" in result.text
    assert "�" in result.text


def test_claude_cli_timeout_raises_llm_error_and_does_not_hang(tmp_path: Path) -> None:
    """06 §6: every subprocess has a timeout (the pipeline had none)."""
    script, _ = make_fake_cli(tmp_path, sleep=30.0)
    client = ClaudeCLIClient([str(script)], config=cli_config(timeout_seconds=0.5))

    with pytest.raises(LLMError) as excinfo:
        client.generate("hi")

    assert "timed out" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, subprocess.TimeoutExpired)


def test_claude_cli_nonzero_exit_raises_llm_error_with_stderr(tmp_path: Path) -> None:
    script, _ = make_fake_cli(tmp_path, out=b"", err=b"not authenticated\n", exit_code=3)
    client = ClaudeCLIClient([str(script)], config=cli_config())

    with pytest.raises(LLMError) as excinfo:
        client.generate("hi")

    message = str(excinfo.value)
    assert "exited 3" in message
    assert "not authenticated" in message


def test_claude_cli_empty_output_is_a_failure(tmp_path: Path) -> None:
    script, _ = make_fake_cli(tmp_path, out=b"   \n")
    client = ClaudeCLIClient([str(script)], config=cli_config())

    with pytest.raises(LLMError, match="empty completion"):
        client.generate("hi")


def test_claude_cli_missing_executable_is_llm_unavailable(tmp_path: Path) -> None:
    client = ClaudeCLIClient([str(tmp_path / "no-such-claude")], config=cli_config())

    with pytest.raises(LLMUnavailable) as excinfo:
        client.generate("hi")

    assert excinfo.value.hint


def test_claude_cli_non_executable_file_is_llm_unavailable(tmp_path: Path) -> None:
    blocked = tmp_path / "not-executable"
    blocked.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    blocked.chmod(0o644)
    client = ClaudeCLIClient([str(blocked)], config=cli_config())

    with pytest.raises(LLMUnavailable):
        client.generate("hi")


def test_claude_cli_available_reflects_the_probe(tmp_path: Path) -> None:
    good, _ = make_fake_cli(tmp_path, name="good-claude")
    bad, _ = make_fake_cli(tmp_path, name="bad-claude", version_exit=127)

    assert ClaudeCLIClient([str(good), "-p"], config=cli_config()).available() is True
    assert ClaudeCLIClient([str(bad), "-p"], config=cli_config()).available() is False
    assert ClaudeCLIClient([str(tmp_path / "absent")], config=cli_config()).available() is False


def test_claude_cli_model_label_prefers_an_explicit_model_flag(tmp_path: Path) -> None:
    script, _ = make_fake_cli(tmp_path)
    client = ClaudeCLIClient([str(script), "-p", "--model", "opus"], config=cli_config())

    assert client.generate("hi").model == "opus"


@pytest.mark.parametrize("bad_command", [[], [""], ["   "], None])
def test_claude_cli_requires_a_command(bad_command: list[str] | None) -> None:
    with pytest.raises(ConfigError) as excinfo:
        ClaudeCLIClient(bad_command, config=cli_config())  # type: ignore[arg-type]

    assert "llm.claude_command" in str(excinfo.value)


# ---------------------------------------------------------------------------
# get_client — backend selection (06 §2, 12 §1)
# ---------------------------------------------------------------------------


def test_get_client_default_purpose_uses_backend() -> None:
    config = LLMConfig(backend="ollama", ollama_host="http://h:1", ollama_model="m")

    client = get_client(config)

    assert isinstance(client, OllamaClient)
    assert isinstance(client, LLMClient)
    assert client.host == "http://h:1"
    assert client.model == "m"


def test_get_client_integrate_prefers_claude_cli() -> None:
    """12 §1: integrations are the quality-sensitive path."""
    config = LLMConfig(backend="ollama", ollama_host="http://h:1", ollama_model="m")
    assert config.integrate_backend == "claude-cli"

    assert isinstance(get_client(config, purpose="integrate"), ClaudeCLIClient)
    assert isinstance(get_client(config, purpose="default"), OllamaClient)


def test_get_client_integrate_honors_an_ollama_override() -> None:
    config = LLMConfig(
        backend="claude-cli",
        integrate_backend="ollama",
        ollama_host="http://h:1",
        ollama_model="m",
    )

    assert isinstance(get_client(config, purpose="integrate"), OllamaClient)
    assert isinstance(get_client(config), ClaudeCLIClient)


def test_get_client_selects_claude_cli_command_from_config() -> None:
    config = LLMConfig(backend="claude-cli", claude_command=["my-claude", "-p", "--model", "x"])

    client = get_client(config)

    assert isinstance(client, ClaudeCLIClient)
    assert client.command == ["my-claude", "-p", "--model", "x"]


def test_get_client_missing_ollama_host_names_the_key() -> None:
    """06 §2: no hardcoded default host — misconfiguration fails loudly."""
    config = LLMConfig(backend="ollama", ollama_host=None, ollama_model="m")

    with pytest.raises(ConfigError) as excinfo:
        get_client(config)

    assert "llm.ollama_host" in str(excinfo.value)
    assert "llm.backend" in str(excinfo.value)
    assert excinfo.value.hint


def test_get_client_missing_ollama_model_names_the_key() -> None:
    config = LLMConfig(backend="ollama", ollama_host="http://h:1", ollama_model=None)

    with pytest.raises(ConfigError) as excinfo:
        get_client(config)

    assert "llm.ollama_model" in str(excinfo.value)


def test_get_client_integrate_error_names_integrate_backend() -> None:
    config = LLMConfig(backend="claude-cli", integrate_backend="ollama")

    with pytest.raises(ConfigError) as excinfo:
        get_client(config, purpose="integrate")

    assert "llm.integrate_backend" in str(excinfo.value)


def test_get_client_unknown_backend_raises_config_error() -> None:
    config = LLMConfig(backend="gpt-9000")  # type: ignore[arg-type]

    with pytest.raises(ConfigError) as excinfo:
        get_client(config)

    assert "llm.backend" in str(excinfo.value)
    assert "'gpt-9000'" in str(excinfo.value)
    assert "ollama" in (excinfo.value.hint or "")


def test_get_client_never_touches_the_network_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """06 §1: constructors are pure; LLMUnavailable only at call time."""
    monkeypatch.setattr(llm_module, "RETRY_BACKOFF_SECONDS", 0.0)
    config = LLMConfig(backend="ollama", ollama_host=closed_port_url(), ollama_model="m")

    client = get_client(config)  # must not raise

    with pytest.raises(LLMUnavailable):
        client.generate("hi")


def test_both_backends_implement_the_one_shared_interface() -> None:
    """09 §2: ONE shared client layer replacing three ad-hoc LLM code paths."""
    assert issubclass(OllamaClient, LLMClient)
    assert issubclass(ClaudeCLIClient, LLMClient)
    for name in ("generate", "available"):
        assert callable(getattr(OllamaClient, name))
        assert callable(getattr(ClaudeCLIClient, name))
    assert LLMClient.__abstractmethods__ == frozenset({"generate", "available"})
    with pytest.raises(TypeError):
        LLMClient()  # type: ignore[abstract]


def test_a_mid_stream_stall_is_a_timeout_not_an_unreachable_backend(
    fake_ollama: FakeOllamaServer,
) -> None:
    """08 §B11's literal ``except TimeoutError`` arm.

    The existing timeout test stalls the server BEFORE the response starts, so
    urllib wraps the failure in ``URLError`` and a different arm handles it —
    deleting the ``TimeoutError`` arm passed the whole suite. The realistic
    remote-Ollama failure is the other shape: headers sent, generation wedges
    mid-body. Handled by the wrong arm it degrades to ``LLMUnavailable``
    ("Cannot reach Ollama") with the wrong hint, sending an operator to check
    a server that is up and answering.
    """
    fake_ollama.replies = [
        Reply(body=generate_body("half a sentence"), stall_after_headers=3.0)
    ]
    client = OllamaClient(
        fake_ollama.base_url, "fake-model", config=ollama_config(timeout_seconds=0.3)
    )

    with pytest.raises(LLMError) as excinfo:
        client.generate("hi")

    # EXACTLY LLMError — LLMUnavailable is a subclass, so `isinstance` cannot
    # tell the two arms apart, which is why the old assertion missed this.
    assert type(excinfo.value) is LLMError, (
        f"a mid-stream stall was reported as {type(excinfo.value).__name__}: {excinfo.value}"
    )
    assert "timed out" in str(excinfo.value)
    assert "reach" not in str(excinfo.value).lower()
    assert isinstance(excinfo.value.__cause__, (TimeoutError, OSError))
