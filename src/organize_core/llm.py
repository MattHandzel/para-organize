"""THE one shared LLM client (spec 09 §2: the original had three ad-hoc
LLM code paths — this is the single replacement; spec 10 §1 component list).

Backends (spec 11 §2, 12 §1): ``ollama`` (HTTP ``/api/generate`` against a
config-required host — no hardcoded fallbacks, 06 §2) and ``claude-cli``
(shelling to ``claude -p``). The quality-sensitive ``integrate`` path
defaults to ``claude-cli`` when available (12 §1).

Reliability contract (spec 02 integrations, 06 §6): LLM backends are
OPTIONAL AND FLAKY — every call has an explicit timeout; ``OSError``,
``TimeoutError`` and ``json.JSONDecodeError`` are caught and surfaced as
LLMError (the expected remote-Ollama failures, 08 §B11); HTTP calls get
bounded retry with backoff; callers must degrade gracefully (a failed
enrichment never blocks a task, 06 §3.1).

``no-ai: true`` enforcement (vault law, spec 02) happens in the CALLERS
(consumers, integrate, auto-tagger) before any prompt is built — this
module never sees vault files, only prompt strings.

Phase-1 ships signatures; consumers (Phase 3) and integrate (Phase 5) fill
in and exercise them.

Implementation notes (decisions this seat made where the spec is silent):

* **No default host anywhere.** ``llm.ollama_host`` / ``llm.ollama_model``
  are required for the ``ollama`` backend; a missing value is a
  ``ConfigError`` naming the dotted key. The module source deliberately
  contains no host literal, no port literal and no model literal — that is
  the 06 §2 / 08 §B17-B18 defect class (a hardcoded remote host plus a
  typo'd default model tag that never existed), and a regression test
  greps this file for them.
* **Proxy env is bypassed.** The urllib opener is built with an empty
  ``ProxyHandler`` so a stray ``http_proxy`` cannot silently redirect calls
  and so this module reads no environment (structural decision 4).
* **Failure taxonomy.** Connection refused / DNS / CLI missing ⇒
  ``LLMUnavailable``; timeout, HTTP error status, non-JSON envelope, empty
  completion, nonzero CLI exit ⇒ ``LLMError``. An empty completion IS a
  failure (the ABC contract) — callers degrade, they never persist "".
* **Retries** apply to transport failures and HTTP 5xx only
  (``config.retries`` extra attempts, exponential backoff from
  :data:`RETRY_BACKOFF_SECONDS`). 4xx, non-JSON envelopes and empty
  completions are not retried — repeating them cannot help.
* **``claude-cli`` prompt transport** is stdin, not argv: prompts are large
  and may contain anything. ``system`` is prefixed to the prompt and
  ``json_mode`` appends :data:`JSON_MODE_DIRECTIVE`; ``temperature`` /
  ``max_tokens`` have no CLI equivalent and are ignored rather than
  guessed into bogus flags. All subprocess text I/O uses
  ``encoding="utf-8", errors="replace"`` with a timeout (06 §6, 08 §B1).
* **Response text is stripped** of surrounding whitespace on both backends
  so callers get one shape regardless of backend.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from organize_core.config import LLMConfig
from organize_core.errors import ConfigError, LLMError, LLMUnavailable

#: Base of the exponential backoff between retried HTTP attempts (06 §6).
#: Tests set this to 0 — it is module state, never read from the env.
RETRY_BACKOFF_SECONDS = 0.5

#: Upper bound on the cheap reachability probes used by :meth:`available`.
PROBE_TIMEOUT_SECONDS = 5.0

#: Appended to ``claude-cli`` prompts when ``json_mode`` is requested; the
#: CLI has no ``format=json`` equivalent, so the parse side is handled by
#: :func:`extract_json` (which tolerates fences and prose).
JSON_MODE_DIRECTIVE = "Respond with a single JSON object and nothing else."

_FENCE_RE = re.compile(r"```[A-Za-z0-9_+.-]*[ \t]*\r?\n(.*?)```", re.DOTALL)

#: Bound on how many ``{`` positions :func:`extract_json` will probe, so a
#: pathological model response cannot turn parsing into O(n^2) work.
_MAX_BRACE_CANDIDATES = 200


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    backend: str
    duration_ms: int
    # Parsed object when json_mode was requested and parsing succeeded;
    # parse strategy per 06 §3.1: whole-string JSON else outermost {…}.
    json: dict[str, Any] | None = None


class LLMClient(ABC):
    """Uniform interface both backends implement."""

    @abstractmethod
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
        """One completion. Raises LLMError/LLMUnavailable on failure; never
        returns silently-empty output (an empty response IS a failure)."""

    @abstractmethod
    def available(self) -> bool:
        """Cheap reachability probe for health checks (``organize health``)
        — never raises."""


class OllamaClient(LLMClient):
    """HTTP ``/api/generate`` client (spec 06 §3.2-3.3 call parameters:
    ``format:"json"`` when json_mode, temp 0.3, explicit timeouts).
    Stdlib ``urllib`` only — no requests dependency."""

    backend_name = "ollama"

    def __init__(self, host: str, model: str, *, config: LLMConfig) -> None:
        if not host or not str(host).strip():
            raise ConfigError(
                "llm.ollama_host is required for the 'ollama' backend",
                hint="Set llm.ollama_host to your Ollama base URL, e.g. "
                'llm.ollama_host = "http://<your-host>:<port>". There is no '
                "default host (spec 06 §2).",
            )
        if not model or not str(model).strip():
            raise ConfigError(
                "llm.ollama_model is required for the 'ollama' backend",
                hint="Set llm.ollama_model to a model tag your Ollama server "
                "actually serves (check `ollama list`). There is no default "
                "model (spec 06 §2).",
            )
        self.host = str(host).strip().rstrip("/")
        self.model = str(model).strip()
        self.config = config
        # Explicit empty ProxyHandler: never consult proxy environment
        # variables (structural decision 4 — no env reads outside paths.py).
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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
        timeout = self._timeout(timeout_seconds)
        options: dict[str, Any] = {"temperature": float(temperature)}
        if max_tokens is not None:
            options["num_predict"] = int(max_tokens)
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": options,
        }
        if system is not None:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"

        started = time.monotonic()
        raw = self._post("/api/generate", payload, timeout)
        duration_ms = int((time.monotonic() - started) * 1000)

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"Ollama at {self.host} returned a non-JSON envelope from /api/generate",
                hint="The host is answering but is not an Ollama API endpoint "
                "(a proxy or error page?). Check llm.ollama_host.",
            ) from exc
        if not isinstance(envelope, dict):
            raise LLMError(
                f"Ollama at {self.host} returned a JSON {type(envelope).__name__}, "
                "expected an object",
                hint="Check llm.ollama_host — this does not look like /api/generate.",
            )

        text = str(envelope.get("response") or "").strip()
        if not text:
            raise LLMError(
                f"Ollama model {self.model!r} returned an empty completion",
                hint="An empty completion is a failure, not a result. Check the "
                "model tag (llm.ollama_model) and the server logs; callers "
                "should degrade gracefully (spec 06 §3.1).",
            )

        return LLMResponse(
            text=text,
            model=str(envelope.get("model") or self.model),
            backend=self.backend_name,
            duration_ms=duration_ms,
            json=extract_json(text) if json_mode else None,
        )

    def available(self) -> bool:
        try:
            request = urllib.request.Request(f"{self.host}/api/tags", method="GET")
            with self._opener.open(request, timeout=self._probe_timeout()) as response:
                return 200 <= int(getattr(response, "status", 0) or 0) < 300
        except (OSError, TimeoutError, ValueError):
            return False

    # -- internals ---------------------------------------------------------

    def _timeout(self, timeout_seconds: float | None) -> float:
        value = timeout_seconds if timeout_seconds is not None else self.config.timeout_seconds
        return float(value)

    def _probe_timeout(self) -> float:
        return min(PROBE_TIMEOUT_SECONDS, float(self.config.timeout_seconds))

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> str:
        """POST JSON with bounded retry + backoff (06 §6).

        Every failure mode named in 08 §B11 (``OSError``, ``TimeoutError``,
        ``json.JSONDecodeError``) is converted to the LLM taxonomy — no
        urllib/socket exception ever escapes this module.
        """
        url = f"{self.host}{path}"
        body = json.dumps(payload).encode("utf-8")
        attempts = max(1, int(self.config.retries) + 1)

        for attempt in range(attempts):
            is_last = attempt == attempts - 1
            try:
                return self._post_once(url, body, timeout)
            except urllib.error.HTTPError as exc:
                if exc.code >= 500 and not is_last:
                    self._backoff(attempt)
                    continue
                raise LLMError(
                    f"Ollama at {url} returned HTTP {exc.code}: {_http_detail(exc)}",
                    hint="4xx usually means a bad model tag or payload "
                    "(check llm.ollama_model); 5xx means the server failed.",
                ) from exc
            except TimeoutError as exc:
                if not is_last:
                    self._backoff(attempt)
                    continue
                raise LLMError(
                    f"Ollama request to {url} timed out after {timeout:g}s",
                    hint="Raise llm.timeout_seconds, or use a smaller model. "
                    "Remote-Ollama timeouts are expected (spec 08 §B11) — "
                    "callers must degrade, never crash.",
                ) from exc
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    if not is_last:
                        self._backoff(attempt)
                        continue
                    raise LLMError(
                        f"Ollama request to {url} timed out after {timeout:g}s",
                        hint="Raise llm.timeout_seconds, or use a smaller model.",
                    ) from exc
                if not is_last:
                    self._backoff(attempt)
                    continue
                raise LLMUnavailable(
                    f"Cannot reach Ollama at {self.host}: {exc.reason}",
                    hint="Is the Ollama server running and llm.ollama_host correct?",
                ) from exc
            except OSError as exc:
                if not is_last:
                    self._backoff(attempt)
                    continue
                raise LLMUnavailable(
                    f"Cannot reach Ollama at {self.host}: {exc}",
                    hint="Is the Ollama server running and llm.ollama_host correct?",
                ) from exc

        raise LLMError(  # pragma: no cover - loop always returns or raises
            f"Ollama request to {url} exhausted {attempts} attempts",
        )

    def _post_once(self, url: str, body: bytes, timeout: float) -> str:
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with self._opener.open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    @staticmethod
    def _backoff(attempt: int) -> None:
        delay = RETRY_BACKOFF_SECONDS * (2**attempt)
        if delay > 0:
            time.sleep(delay)


class ClaudeCLIClient(LLMClient):
    """Shells to ``claude -p`` (config ``llm.claude_command``); subprocess
    with explicit timeout, ``encoding="utf-8", errors="replace"`` (06 §6 —
    the B1 outage class)."""

    backend_name = "claude-cli"

    def __init__(self, command: list[str], *, config: LLMConfig) -> None:
        argv = [str(part) for part in (command or [])]
        if not argv or not argv[0].strip():
            raise ConfigError(
                "llm.claude_command must be a non-empty argv list for the 'claude-cli' backend",
                hint='Set llm.claude_command, e.g. llm.claude_command = ["claude", "-p"].',
            )
        self.command = argv
        self.config = config

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
        # temperature / max_tokens have no claude-CLI equivalent; they are
        # accepted for interface parity and deliberately not translated into
        # invented flags.
        timeout = float(
            timeout_seconds if timeout_seconds is not None else self.config.timeout_seconds
        )
        stdin_text = prompt if system is None else f"{system}\n\n{prompt}"
        if json_mode:
            stdin_text = f"{stdin_text}\n\n{JSON_MODE_DIRECTIVE}"

        started = time.monotonic()
        try:
            proc = subprocess.run(
                self.command,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(
                f"{self.command[0]!r} timed out after {timeout:g}s",
                hint="Raise llm.timeout_seconds or shorten the prompt; the "
                "caller must degrade gracefully (spec 06 §6).",
            ) from exc
        except FileNotFoundError as exc:
            raise LLMUnavailable(
                f"claude CLI {self.command[0]!r} not found",
                hint="Install the claude CLI or point llm.claude_command at it.",
            ) from exc
        except PermissionError as exc:
            raise LLMUnavailable(
                f"claude CLI {self.command[0]!r} is not executable",
                hint="chmod +x the command named by llm.claude_command.",
            ) from exc
        except OSError as exc:
            raise LLMUnavailable(
                f"Cannot run claude CLI {self.command[0]!r}: {exc}",
                hint="Check llm.claude_command.",
            ) from exc
        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            raise LLMError(
                f"{self.command[0]!r} exited {proc.returncode}: "
                f"{_excerpt(proc.stderr) or '<no stderr>'}",
                hint="Run the command by hand to see the failure; the caller "
                "must degrade gracefully rather than block on it.",
            )

        text = (proc.stdout or "").strip()
        if not text:
            raise LLMError(
                f"{self.command[0]!r} returned an empty completion",
                hint="An empty completion is a failure, not a result "
                f"(stderr: {_excerpt(proc.stderr) or '<empty>'}).",
            )

        return LLMResponse(
            text=text,
            model=self._model_label(),
            backend=self.backend_name,
            duration_ms=duration_ms,
            json=extract_json(text) if json_mode else None,
        )

    def available(self) -> bool:
        try:
            proc = subprocess.run(
                [self.command[0], "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return False
        return proc.returncode == 0

    def _model_label(self) -> str:
        for flag in ("--model", "-m"):
            if flag in self.command:
                position = self.command.index(flag) + 1
                if position < len(self.command):
                    return self.command[position]
        return Path(self.command[0]).name


def get_client(config: LLMConfig, *, purpose: str = "default") -> LLMClient:
    """Backend selection: ``purpose="integrate"`` honors
    ``config.integrate_backend`` (claude-cli preferred, 12 §1), everything
    else ``config.backend``. Missing required host/model for the selected
    backend ⇒ ConfigError naming the key (06 §2). Raises LLMUnavailable
    only at call time, not construction (constructors stay pure — 06 §1)."""
    if purpose == "integrate":
        key, backend = "llm.integrate_backend", config.integrate_backend
    else:
        key, backend = "llm.backend", config.backend

    if backend == "ollama":
        host, model = config.ollama_host, config.ollama_model
        if not host or not str(host).strip():
            raise ConfigError(
                f"llm.ollama_host is required (selected by {key} = 'ollama')",
                hint="Set llm.ollama_host to your Ollama base URL — there is no "
                "default host (spec 06 §2).",
            )
        if not model or not str(model).strip():
            raise ConfigError(
                f"llm.ollama_model is required (selected by {key} = 'ollama')",
                hint="Set llm.ollama_model to a model tag your server serves — "
                "there is no default model (spec 06 §2).",
            )
        return OllamaClient(host, model, config=config)

    if backend == "claude-cli":
        return ClaudeCLIClient(config.claude_command, config=config)

    raise ConfigError(
        f"{key} = {backend!r} is not a known LLM backend",
        hint="Valid backends: 'ollama', 'claude-cli'.",
    )


def extract_json(text: str) -> dict[str, Any] | None:
    """Whole-string JSON parse, else the outermost ``{…}`` block (spec 06
    §3.1 LLM-enrichment parse rule). None when nothing parses.

    Tolerant of the shapes real models emit: ```` ```json ```` fences, bare
    ```` ``` ```` fences, prose preambles ("Sure! Here's the JSON:"), prose
    epilogues, braces inside prose, and braces inside string values. Only a
    JSON *object* counts — a top-level array or scalar returns None, since
    every caller (06 §3.1 enrichment, 11 §2 tagging) wants a mapping.
    """
    if not text:
        return None
    for candidate in _json_candidates(text):
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _json_candidates(text: str) -> Iterator[str]:
    """Candidate JSON substrings, most-authoritative first."""
    stripped = text.strip()
    if stripped:
        yield stripped
    for block in _FENCE_RE.findall(text):
        block = block.strip()
        if block:
            yield block
    yield from _brace_blocks(text)


def _brace_blocks(text: str) -> Iterator[str]:
    """Balanced ``{…}`` substrings, outermost-first, string-literal aware."""
    seen = 0
    for start, char in enumerate(text):
        if char != "{":
            continue
        seen += 1
        if seen > _MAX_BRACE_CANDIDATES:
            return
        end = _match_brace(text, start)
        if end is not None:
            yield text[start : end + 1]


def _match_brace(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _http_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return _excerpt(exc.read().decode("utf-8", errors="replace")) or str(exc.reason or "")
    except (OSError, ValueError):
        # Reading the error body is best-effort diagnostics; it must never
        # mask the HTTP failure we are already reporting.
        return str(exc.reason or "")


def _excerpt(value: str | None, limit: int = 200) -> str:
    if not value:
        return ""
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"
