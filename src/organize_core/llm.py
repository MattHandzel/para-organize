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
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from organize_core.config import LLMConfig


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

    def __init__(self, host: str, model: str, *, config: LLMConfig) -> None:
        raise NotImplementedError

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
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError


class ClaudeCLIClient(LLMClient):
    """Shells to ``claude -p`` (config ``llm.claude_command``); subprocess
    with explicit timeout, ``encoding="utf-8", errors="replace"`` (06 §6 —
    the B1 outage class)."""

    def __init__(self, command: list[str], *, config: LLMConfig) -> None:
        raise NotImplementedError

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
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError


def get_client(config: LLMConfig, *, purpose: str = "default") -> LLMClient:
    """Backend selection: ``purpose="integrate"`` honors
    ``config.integrate_backend`` (claude-cli preferred, 12 §1), everything
    else ``config.backend``. Missing required host/model for the selected
    backend ⇒ ConfigError naming the key (06 §2). Raises LLMUnavailable
    only at call time, not construction (constructors stay pure — 06 §1)."""
    raise NotImplementedError


def extract_json(text: str) -> dict[str, Any] | None:
    """Whole-string JSON parse, else the outermost ``{…}`` block (spec 06
    §3.1 LLM-enrichment parse rule). None when nothing parses."""
    raise NotImplementedError
