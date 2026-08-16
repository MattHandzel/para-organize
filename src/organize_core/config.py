"""Load and validate ``config.toml`` (spec 10 §3: one file, all behavior).

Sections and their governing docs:
    [vault]                spec 02 (layout), 06 §2 (scan_dirs/ignore_patterns)
    [suggestions]          spec 04 §2 (weights), 04 §4-5 (learning knobs)
    [file_ops]             spec 05 (backups, log, auto_create)
    [metadata_fields]      spec 07 (list of field definitions)
    [[routes]]             spec 11 §1
    [consumers.<name>]     spec 06 §2
    [llm]                  spec 09 §2 / 11 §2 / 12 §1 (shared client, backends)
    [auto_organize]        spec 13 §2 (trust ladder)
    [server]               spec 10 §1 (socket, idle timeout)
    [descriptions]         spec 11 §3 (central path → NL description table)
    [logging]              spec 06 §2

NOTE (skeleton docstring vs spec): the scaffold listed ``[state]`` alongside
``[logging]`` per 06 §2, but 10 §3 ("SUPERSEDES the component split in 01/03/06
where they conflict") moves every state location into :class:`CorePaths`.
``[state]`` is therefore a DELETED key: it fails loudly with a hint naming
``$ORGANIZE_CORE_STATE_DIR`` rather than silently configuring nothing
(spec 03 §1 "honored or deleted"). See :data:`REMOVED_KEYS`.

VALIDATION LAW (spec 03 §1): **every config key is honored or deleted** — an
unknown key anywhere raises :class:`~organize_core.errors.ConfigError` naming
the exact dotted key (e.g. ``suggestions.weights.typo_match``). Missing
required keys, wrong leaf types, out-of-range values and enum violations fail
the same way. Path existence checks (vault root, PARA folders, capture
folder) live in :func:`check_vault` and run at startup/health, loudly
(spec 03 §1, 08 §C1-2).

Defaults below are the defaults-of-record from the live system (06 §2) and
the plugin schema (03 §1) — a builder changing a default is changing
behavior and must cite the spec line that permits it.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, NoReturn

from organize_core.errors import ConfigError, RouteConfigError
from organize_core.paths import CorePaths, expand

# ---------------------------------------------------------------------------
# [vault]
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VaultConfig:
    """Vault layout (spec 02). ``para_folders`` values are folder names
    relative to root; Matt's real archive folder is ``archive`` — SINGULAR
    (02 "archive vs archives"); the default here matches the vault, and
    :func:`check_vault` fails loudly when a configured folder is absent."""

    root: Path
    capture_folder: str = "capture"
    raw_capture_folder: str = "capture/raw_capture"
    para_folders: dict[str, str] = field(
        default_factory=lambda: {
            "projects": "projects",
            "areas": "areas",
            "resources": "resources",
            "archives": "archive",
        }
    )
    # Where organized/processed originals are archived, relative to the
    # archives folder (spec 02 layout; 05 §3 get_archive_path).
    archive_capture_path: str = "capture/raw_capture"
    # Pipeline scan scope (spec 06 §2, live values).
    scan_dirs: list[str] = field(
        default_factory=lambda: ["capture/raw_capture", "resources", "areas", "projects"]
    )
    # Prefix or glob, spec 06 §2; also applied by the indexer (03 §7).
    ignore_patterns: list[str] = field(
        default_factory=lambda: [".obsidian", ".git", "resources/flashcards"]
    )
    # Indexer knobs (spec 03 §7).
    max_file_size: int = 1_048_576  # bytes; larger files skipped with warning
    incremental_debounce: int = 500  # ms; used by server-side event handling


# ---------------------------------------------------------------------------
# [suggestions] (spec 04)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TypeBonus:
    """Folder-type bonus, promoted from hardcode to config (spec 04 §2 #7)."""

    projects: float = 0.3
    areas: float = 0.2
    resources: float = 0.1


@dataclass(frozen=True)
class SuggestionWeights:
    """EXACT default weights per spec 04 §2 — acceptance tests assert these
    numerically; do not tune without a spec change."""

    exact_tag_match: float = 2.0  # per matching tag
    normalized_tag_match: float = 1.5  # per tag
    learned_association: float = 1.8  # × learned score (04 §4)
    source_match: float = 1.3  # per source
    alias_similarity: float = 1.1  # × similarity when > 0.6
    context_match: float = 1.0
    type_bonus: TypeBonus = field(default_factory=TypeBonus)


@dataclass(frozen=True)
class LearningConfig:
    """Learning knobs (spec 04 §4-5). ``recency_decay`` must be genuinely
    honored so Matt can flatten it to e.g. 0.99 (04 §4 decay-curve note)."""

    recency_decay: float = 0.9  # per-day base: 0.9^days
    frequency_boost: float = 1.2  # applied when count > 5
    max_history: int = 1000  # association cap, evict oldest by last_used
    eviction_days: int = 90  # hard-delete horizon (04 §5)
    min_confidence: float = 0.3  # candidate floor, archive exempt (04 §2)


@dataclass(frozen=True)
class SuggestionsConfig:
    """Spec 04 §1-2. ``max_suggestions`` is the TOTAL list length including
    the archive entry (off-by-one in the original fixed per 04 §1)."""

    max_suggestions: int = 10
    always_show_archive: bool = True
    weights: SuggestionWeights = field(default_factory=SuggestionWeights)
    learning: LearningConfig = field(default_factory=LearningConfig)
    # tag_normalization map, e.g. {"project": "projects"} (spec 04 §2 #2).
    tag_normalization: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# [file_ops] (spec 05)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileOpsConfig:
    """Spec 05 invariants' knobs. ``backup_dir`` is relative to the vault
    root (05 §1.4); the operations log lives in state (05 §1.5, 10 §3)."""

    create_backups: bool = True
    backup_dir: str = ".backups"
    log_operations: bool = True
    auto_create_folders: bool = True


# ---------------------------------------------------------------------------
# [metadata_fields] (spec 07)
# ---------------------------------------------------------------------------

MetadataFieldType = Literal["list", "string", "boolean", "enum", "number"]


@dataclass(frozen=True)
class MetadataFieldConfig:
    """One user-defined frontmatter field editable during organizing
    (spec 07 "Config schema"). Adding a field is pure configuration —
    zero code changes (07 acceptance test 3). Keymap collisions with core
    keymaps are a validation error (07 acceptance test 4)."""

    key: str
    type: MetadataFieldType
    keymap: str
    prompt: str | None = None
    append: bool = True  # lists: append (default) vs replace
    complete: str | list[str] | None = None  # "existing" | explicit values
    values: list[str] = field(default_factory=list)  # enum choices
    normalize: Literal["kebab"] | None = None  # per-field normalization


#: Keys the organize UI binds itself (spec 03 §3 keymap table + 13 §1 ``A``).
#: A ``metadata_fields`` entry may not steal one of these — 07 acceptance
#: test 4 requires the error to name BOTH bindings.
CORE_KEYMAPS: dict[str, str] = {
    "<CR>": "accept",
    "<Esc>": "cancel",
    "<Tab>": "next",
    "<S-Tab>": "prev",
    "s": "skip",
    "S": "sort",
    "a": "archive",
    "m": "merge",
    "/": "search",
    "r": "refresh",
    "p": "toggle_preview",
    "?": "help",
    "A": "auto_organize",
    "<A-j>": "next_suggestion",
    "<A-k>": "prev_suggestion",
    "<C-h>": "focus_capture",
    "<C-l>": "focus_organize",
    "<BS>": "back_to_parent",
    "<leader>np": "new_project",
    "<leader>na": "new_area",
    "<leader>nr": "new_resource",
    "<leader>mc": "merge_complete",
    "<leader>mx": "merge_cancel",
}


def default_metadata_fields() -> list[MetadataFieldConfig]:
    """Ships with ``tags`` (list, ``t``) and ``importance`` (enum, ``i``)
    so the feature works out of the box (spec 07 "Behavior")."""
    return [
        MetadataFieldConfig(
            key="tags",
            type="list",
            keymap="t",
            prompt="Add tag(s)",
            append=True,
            complete="existing",
            normalize="kebab",  # 07 acceptance test 1: "Bar Baz" -> "bar-baz"
        ),
        MetadataFieldConfig(
            key="importance",
            type="enum",
            keymap="i",
            values=["high", "medium", "low"],
        ),
    ]


# ---------------------------------------------------------------------------
# [[routes]] (spec 11 §1)
# ---------------------------------------------------------------------------

RouteMode = Literal["append", "move", "integrate"]

#: The three doc-12 §1 edit modes. ``RouteMode`` is the same alphabet because a
#: route's ``mode`` IS the per-route default edit mode (12 §1 "per-route default
#: via ``mode`` in doc 11").
EditMode = Literal["manual", "append", "integrate"]

#: The doc-12 §1 review gate. ``"diff"`` shows the proposed result and waits for
#: Matt; ``"auto"`` applies without asking and is a deliberate per-route opt-in.
ReviewGate = Literal["diff", "auto"]


@dataclass(frozen=True)
class RouteConfig:
    """One tag→destination route (spec 11 §1). ``tags`` entries are any-of;
    an entry ``"a+b"`` requires both tags. Folder destinations get ``move``;
    file destinations get ``append``/``integrate`` — violations are a
    RouteConfigError at load time. ``description`` is Matt's natural
    language and is load-bearing (UI display, auto-tagger and doc-13 input).
    ``auto=True`` opts the route into unattended handling (11 §1).

    ``review`` is the doc-12 §1 review gate for this route's ``integrate``
    results: ``"diff"`` (the default) shows Matt the unified diff and waits;
    ``"auto"`` applies without asking. Spec 12 §1 makes ``"auto"`` an explicit
    PER-ROUTE opt-in, so it lives here rather than only in ``[integrate]``. It
    is meaningful only for ``mode = "integrate"``; validation says so rather
    than silently ignoring it (03 §1 every-key-honored)."""

    tags: list[str]
    destination: str  # relative to vault root; trailing "/" ⇒ folder
    mode: RouteMode
    description: str = ""
    template: str | None = None  # append-heading template override (11 §1)
    auto: bool = False
    review: ReviewGate = "diff"


# ---------------------------------------------------------------------------
# [consumers.<name>] (spec 06 §2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsumerConfig:
    """One configured consumer instance (spec 06 §2). ``options`` carries the
    consumer-specific keys; each consumer class validates its own options
    against its declared schema at construction (still pure — no I/O) so
    unknown consumer options fail the every-key-honored law too."""

    name: str  # config section name, e.g. "taskwarrior"
    type: str  # registered consumer type
    enabled: bool = True
    include_paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    max_notes_per_run: int = 50  # counts successes only (06 §1)
    options: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)  # deep_research passthrough


# ---------------------------------------------------------------------------
# [llm] (spec 09 §2, 11 §2, 12 §1)
# ---------------------------------------------------------------------------

LLMBackendName = Literal["ollama", "claude-cli"]


@dataclass(frozen=True)
class LLMConfig:
    """The ONE shared LLM client's config (spec 09 §2). Hosts/models are
    config-required where used — no hardcoded fallbacks (06 §2: the
    ``gemma4:e4b`` code default was a typo'd model). ``integrate`` defaults
    to ``claude-cli`` when available (12 §1)."""

    backend: LLMBackendName = "ollama"
    ollama_host: str | None = None  # e.g. "http://server.matthandzel.com:11434"
    ollama_model: str | None = None  # e.g. "gemma3:12b-it-qat"
    claude_command: list[str] = field(default_factory=lambda: ["claude", "-p"])
    integrate_backend: LLMBackendName = "claude-cli"
    timeout_seconds: float = 60.0
    retries: int = 2  # bounded retry with backoff for HTTP calls (06 §6)


# ---------------------------------------------------------------------------
# [integrate] (spec 12 §1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrateConfig:
    """Edit-mode and integrate-safety settings (spec 12 §1).

    - ``default_mode`` is the GLOBAL default edit mode. Spec 12 §1: "per-route
      default via ``mode`` in doc 11; global default ``manual``" — so a route's
      ``mode`` overrides this, and this overrides nothing but the bare UI
      default.
    - ``review`` is the global review-gate default; a route's ``review``
      overrides it (12 §1 makes ``"auto"`` a per-route opt-in).
    - ``max_deleted_lines`` is the configurable deletion threshold of the
      integrate hard-reject: "hard-rejects any result that deletes existing
      non-whitespace lines beyond a configurable threshold (default: zero
      deletions allowed outside the edited region)". Exceeding it is an
      :class:`~organize_core.errors.IntegrationRejected`. Zero means an
      integrate result may only add and weave — never destroy.

    Phase 5 implements the enforcement; the settings are contract and are
    validated from day one so a spec-conformant config never fails the
    unknown-key law (03 §1).
    """

    default_mode: EditMode = "manual"
    review: ReviewGate = "diff"
    max_deleted_lines: int = 0


# ---------------------------------------------------------------------------
# [auto_organize] (spec 13 §2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoOrganizeConfig:
    """Trust ladder (spec 13 §2). Phase 6; validated from day one."""

    trust: Literal["propose", "auto_below"] = "propose"
    confidence_threshold: float = 0.8
    min_precedents: int = 5  # never auto-apply below this many accepted precedents


# ---------------------------------------------------------------------------
# [server] (spec 10 §1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerConfig:
    socket_path: Path | None = None  # default: CorePaths.socket_path
    idle_timeout_seconds: float = 600.0


# ---------------------------------------------------------------------------
# [state] / [logging] (spec 06 §2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """The fully validated configuration. Immutable; constructed only by
    :func:`load_config` / :func:`validate_config` (or directly by tests)."""

    vault: VaultConfig
    suggestions: SuggestionsConfig = field(default_factory=SuggestionsConfig)
    file_ops: FileOpsConfig = field(default_factory=FileOpsConfig)
    metadata_fields: list[MetadataFieldConfig] = field(default_factory=list)
    routes: list[RouteConfig] = field(default_factory=list)
    consumers: list[ConsumerConfig] = field(default_factory=list)
    llm: LLMConfig = field(default_factory=LLMConfig)
    integrate: IntegrateConfig = field(default_factory=IntegrateConfig)
    auto_organize: AutoOrganizeConfig = field(default_factory=AutoOrganizeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    # Central [descriptions] table: path (vault-relative) → NL description
    # (spec 11 §3 fallback storage when a folder has no index.md).
    descriptions: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation machinery (the unknown-key law, spec 03 §1)
# ---------------------------------------------------------------------------

#: Keys that USED to exist and are now deliberately deleted (spec 03 §1: every
#: config key is honored or deleted; the deleted ones fail loudly naming the
#: key — 08 §A35). Value is the hint telling Matt where the setting went.
REMOVED_KEYS: dict[str, str] = {
    "ui": "UI settings (layout, display, highlights, icons) live in the Neovim client's setup() table, not in organize-core config.toml (spec 10 §3)",
    "keymaps": "keymaps live in the Neovim client's setup() table (spec 10 §3); only metadata_fields[*].keymap is core config (spec 07)",
    "telescope": "telescope.layout_*/picker_opts were dead keys and are deleted (08 §A35); picker configuration belongs to the Neovim client",
    "patterns": "patterns.alias_extraction / patterns.case_sensitive were dead keys and are deleted (08 §A35)",
    "debug": "debug.profile was a dead key and is deleted (08 §A35); use [logging] level instead",
    "indexing": "indexer knobs moved to [vault] (ignore_patterns, max_file_size, incremental_debounce); indexing.backend=\"sqlite\" was advertised but never implemented and is deleted (08 §A35/§A38)",
    "state": "state locations are resolved by organize-core, not config: ~/.local/share/organize-core (spec 10 §3). Override with $ORGANIZE_CORE_STATE_DIR",
    "vault_dir": "renamed: use [vault] root (spec 10 §3)",
    "capture_folder": "renamed: use [vault] capture_folder",
    "para_folders": "renamed: use [vault] para_folders",
    "data_dir": "state locations are resolved by organize-core (spec 10 §3); override with $ORGANIZE_CORE_STATE_DIR",
    "log_dir": "state locations are resolved by organize-core (spec 10 §3); override with $ORGANIZE_CORE_STATE_DIR",
    "file_ops.confirm_operations": "dead key, deleted (08 §A35); confirmation is a client concern",
    "vault.dir": "renamed: use [vault] root",
    "vault.archives": "archive folders are configured in [vault] para_folders (note: the vault folder is 'archive', SINGULAR — 08 §C2)",
    "llm.host": "renamed: use llm.ollama_host (no hardcoded host fallbacks — spec 06 §2)",
    "llm.model": "renamed: use llm.ollama_model (the 'gemma4:e4b' code default was a typo'd model — spec 06 §2)",
    "suggestions.weights.type_bonus.archives": "the archive entry is always shown (suggestions.always_show_archive) and is not scored (spec 04 §2)",
}

#: Consumer type names that are contract (ARCHITECTURE resolution #2). Used
#: only if the registry cannot be imported (a builder mid-flight); the live
#: registry is authoritative.
_FALLBACK_CONSUMER_TYPES: frozenset[str] = frozenset(
    {"taskwarrior", "learn", "question_answer", "deep_research", "tag_router", "auto_tagger"}
)

_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_MISSING = object()


def _typename(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    return {int: "integer", float: "float", str: "string", list: "array", dict: "table"}.get(
        type(value), type(value).__name__
    )


def _dotted(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _registered_consumer_types() -> frozenset[str]:
    """The live ``@register`` registry (spec 06 §1).

    Imported lazily because ``consumers/base.py`` imports this module: the
    edge is real and is recorded in ARCHITECTURE.md's dependency block, with
    the lazy import as the mitigation that keeps the module graph acyclic.

    Only ``ImportError`` falls back. A broken ``@register`` in a consumer
    module, or any other failure inside the registry, must surface — the
    module contract is "bad config is banned", and a blanket
    ``except Exception`` here quietly validated user config against a
    HARDCODED type list while the real registry was broken.
    """
    try:
        from organize_core.consumers import get_consumer_types
    except ImportError:  # pragma: no cover - only while another seat is mid-build
        return _FALLBACK_CONSUMER_TYPES
    return frozenset(get_consumer_types()) or _FALLBACK_CONSUMER_TYPES


class _Validator:
    """Typed reader over a raw TOML mapping.

    Every accessor names the exact dotted key in its error, so a bad config
    tells Matt precisely which line to edit (spec 03 §1, errors.py "bad
    config is banned").
    """

    def __init__(self, source: str) -> None:
        self.source = source

    # -- failure ---------------------------------------------------------

    def fail(
        self,
        key: str,
        message: str,
        *,
        hint: str | None = None,
        cls: type[ConfigError] = ConfigError,
    ) -> NoReturn:
        raise cls(f"{self.source}: config key '{key}' {message}", hint=hint)

    def check_unknown(
        self,
        table: dict[str, Any],
        allowed: set[str],
        prefix: str,
        *,
        cls: type[ConfigError] = ConfigError,
    ) -> None:
        """THE UNKNOWN-KEY LAW: any key not in ``allowed`` is a loud error
        naming the dotted key and its location (spec 03 §1)."""
        for key in table:
            if key in allowed:
                continue
            dotted = _dotted(prefix, key)
            hint = REMOVED_KEYS.get(dotted)
            if hint is None:
                close = difflib.get_close_matches(key, sorted(allowed), n=1, cutoff=0.6)
                if close:
                    hint = f"did you mean '{_dotted(prefix, close[0])}'?"
                elif allowed:
                    hint = "keys allowed here: " + ", ".join(sorted(allowed))
                else:
                    hint = "this table takes no keys"
                hint += " (every config key is honored or deleted — spec 03 §1)"
            raise cls(f"{self.source}: unknown config key '{dotted}'", hint=hint)

    # -- typed accessors --------------------------------------------------

    def table(
        self, parent: dict[str, Any], key: str, prefix: str, *, required: bool = False
    ) -> dict[str, Any]:
        dotted = _dotted(prefix, key)
        value = parent.get(key, _MISSING)
        if value is _MISSING:
            if required:
                self.fail(dotted, "is required")
            return {}
        if not isinstance(value, dict):
            self.fail(dotted, f"must be a table, got {_typename(value)}")
        return value

    def string(
        self,
        table: dict[str, Any],
        key: str,
        prefix: str,
        default: str | None = None,
        *,
        required: bool = False,
        allow_empty: bool = False,
        choices: frozenset[str] | None = None,
        cls: type[ConfigError] = ConfigError,
    ) -> str | None:
        dotted = _dotted(prefix, key)
        value = table.get(key, _MISSING)
        if value is _MISSING:
            if required:
                self.fail(dotted, "is required", cls=cls)
            return default
        if not isinstance(value, str):
            self.fail(dotted, f"must be a string, got {_typename(value)}", cls=cls)
        if not allow_empty and not value.strip():
            self.fail(dotted, "must not be empty", cls=cls)
        if choices is not None and value not in choices:
            self.fail(
                dotted,
                f"must be one of {sorted(choices)}, got {value!r}",
                cls=cls,
            )
        return value

    def boolean(self, table: dict[str, Any], key: str, prefix: str, default: bool) -> bool:
        value = table.get(key, _MISSING)
        if value is _MISSING:
            return default
        if not isinstance(value, bool):
            self.fail(_dotted(prefix, key), f"must be true or false, got {_typename(value)}")
        return value

    def integer(
        self,
        table: dict[str, Any],
        key: str,
        prefix: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        dotted = _dotted(prefix, key)
        value = table.get(key, _MISSING)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(dotted, f"must be an integer, got {_typename(value)}")
        if minimum is not None and value < minimum:
            self.fail(dotted, f"must be >= {minimum}, got {value}")
        if maximum is not None and value > maximum:
            self.fail(dotted, f"must be <= {maximum}, got {value}")
        return value

    def number(
        self,
        table: dict[str, Any],
        key: str,
        prefix: str,
        default: float,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        exclusive_minimum: bool = False,
    ) -> float:
        dotted = _dotted(prefix, key)
        value = table.get(key, _MISSING)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(dotted, f"must be a number, got {_typename(value)}")
        value = float(value)
        if minimum is not None:
            if exclusive_minimum and value <= minimum:
                self.fail(dotted, f"must be > {minimum}, got {value}")
            if not exclusive_minimum and value < minimum:
                self.fail(dotted, f"must be >= {minimum}, got {value}")
        if maximum is not None and value > maximum:
            self.fail(dotted, f"must be <= {maximum}, got {value}")
        return value

    def string_list(
        self,
        table: dict[str, Any],
        key: str,
        prefix: str,
        default: list[str] | None = None,
        *,
        required: bool = False,
        non_empty: bool = False,
        cls: type[ConfigError] = ConfigError,
    ) -> list[str]:
        dotted = _dotted(prefix, key)
        value = table.get(key, _MISSING)
        if value is _MISSING:
            if required:
                self.fail(dotted, "is required", cls=cls)
            return list(default or [])
        if not isinstance(value, list):
            self.fail(dotted, f"must be an array of strings, got {_typename(value)}", cls=cls)
        for i, item in enumerate(value):
            if not isinstance(item, str):
                self.fail(
                    f"{dotted}[{i}]",
                    f"must be a string, got {_typename(item)}",
                    cls=cls,
                )
            if not item.strip():
                self.fail(f"{dotted}[{i}]", "must not be empty", cls=cls)
        if non_empty and not value:
            self.fail(dotted, "must not be an empty array", cls=cls)
        return list(value)

    def string_map(
        self,
        table: dict[str, Any],
        key: str,
        prefix: str,
        default: dict[str, str] | None = None,
        *,
        required_keys: frozenset[str] | None = None,
    ) -> dict[str, str]:
        dotted = _dotted(prefix, key)
        value = table.get(key, _MISSING)
        if value is _MISSING:
            return dict(default or {})
        if not isinstance(value, dict):
            self.fail(dotted, f"must be a table, got {_typename(value)}")
        out: dict[str, str] = dict(default or {})
        for sub, item in value.items():
            if not isinstance(item, str):
                self.fail(_dotted(dotted, sub), f"must be a string, got {_typename(item)}")
            if not item.strip():
                self.fail(_dotted(dotted, sub), "must not be empty")
            if required_keys is not None and sub not in required_keys:
                raise ConfigError(
                    f"{self.source}: unknown config key '{_dotted(dotted, sub)}'",
                    hint="keys allowed here: " + ", ".join(sorted(required_keys)),
                )
            out[sub] = item
        return out

    def array_of_tables(
        self,
        parent: dict[str, Any],
        key: str,
        prefix: str,
        *,
        cls: type[ConfigError] = ConfigError,
    ) -> list[dict[str, Any]] | None:
        dotted = _dotted(prefix, key)
        value = parent.get(key, _MISSING)
        if value is _MISSING:
            return None
        if not isinstance(value, list):
            self.fail(
                dotted,
                f"must be an array of tables ([[{dotted}]]), got {_typename(value)}",
                cls=cls,
            )
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                self.fail(f"{dotted}[{i}]", f"must be a table, got {_typename(item)}", cls=cls)
        return list(value)


# -- section validators ------------------------------------------------------

_VAULT_KEYS = {
    "root",
    "capture_folder",
    "raw_capture_folder",
    "para_folders",
    "archive_capture_path",
    "scan_dirs",
    "ignore_patterns",
    "max_file_size",
    "incremental_debounce",
}
_PARA_KEYS = frozenset({"projects", "areas", "resources", "archives"})


def _validate_vault(v: _Validator, raw: dict[str, Any]) -> VaultConfig:
    table = v.table(raw, "vault", "", required=True)
    v.check_unknown(table, _VAULT_KEYS, "vault")
    defaults = VaultConfig(root=Path("."))
    root = v.string(table, "root", "vault", required=True)
    assert root is not None  # `required=True` never returns None
    scan_dirs = v.string_list(table, "scan_dirs", "vault", defaults.scan_dirs, non_empty=True)
    if not scan_dirs:  # unreachable via non_empty, kept for the 08 §B14 guard
        v.fail("vault.scan_dirs", "must list at least one directory")
    return VaultConfig(
        root=Path(root),
        capture_folder=v.string(table, "capture_folder", "vault", defaults.capture_folder),
        raw_capture_folder=v.string(
            table, "raw_capture_folder", "vault", defaults.raw_capture_folder
        ),
        para_folders=v.string_map(
            table, "para_folders", "vault", defaults.para_folders, required_keys=_PARA_KEYS
        ),
        archive_capture_path=v.string(
            table, "archive_capture_path", "vault", defaults.archive_capture_path
        ),
        scan_dirs=scan_dirs,
        ignore_patterns=v.string_list(
            table, "ignore_patterns", "vault", defaults.ignore_patterns
        ),
        max_file_size=v.integer(table, "max_file_size", "vault", defaults.max_file_size, minimum=1),
        incremental_debounce=v.integer(
            table, "incremental_debounce", "vault", defaults.incremental_debounce, minimum=0
        ),
    )


_TYPE_BONUS_KEYS = {"projects", "areas", "resources"}
_WEIGHT_KEYS = {
    "exact_tag_match",
    "normalized_tag_match",
    "learned_association",
    "source_match",
    "alias_similarity",
    "context_match",
    "type_bonus",
}
_LEARNING_KEYS = {
    "recency_decay",
    "frequency_boost",
    "max_history",
    "eviction_days",
    "min_confidence",
}
_SUGGESTION_KEYS = {
    "max_suggestions",
    "always_show_archive",
    "weights",
    "learning",
    "tag_normalization",
}


def _validate_suggestions(v: _Validator, raw: dict[str, Any]) -> SuggestionsConfig:
    table = v.table(raw, "suggestions", "")
    v.check_unknown(table, _SUGGESTION_KEYS, "suggestions")

    weights_raw = v.table(table, "weights", "suggestions")
    v.check_unknown(weights_raw, _WEIGHT_KEYS, "suggestions.weights")
    bonus_raw = v.table(weights_raw, "type_bonus", "suggestions.weights")
    v.check_unknown(bonus_raw, _TYPE_BONUS_KEYS, "suggestions.weights.type_bonus")
    d_bonus = TypeBonus()
    bonus = TypeBonus(
        projects=v.number(
            bonus_raw, "projects", "suggestions.weights.type_bonus", d_bonus.projects, minimum=0.0
        ),
        areas=v.number(
            bonus_raw, "areas", "suggestions.weights.type_bonus", d_bonus.areas, minimum=0.0
        ),
        resources=v.number(
            bonus_raw, "resources", "suggestions.weights.type_bonus", d_bonus.resources, minimum=0.0
        ),
    )
    d_w = SuggestionWeights()
    weights = SuggestionWeights(
        exact_tag_match=v.number(
            weights_raw, "exact_tag_match", "suggestions.weights", d_w.exact_tag_match, minimum=0.0
        ),
        normalized_tag_match=v.number(
            weights_raw,
            "normalized_tag_match",
            "suggestions.weights",
            d_w.normalized_tag_match,
            minimum=0.0,
        ),
        learned_association=v.number(
            weights_raw,
            "learned_association",
            "suggestions.weights",
            d_w.learned_association,
            minimum=0.0,
        ),
        source_match=v.number(
            weights_raw, "source_match", "suggestions.weights", d_w.source_match, minimum=0.0
        ),
        alias_similarity=v.number(
            weights_raw, "alias_similarity", "suggestions.weights", d_w.alias_similarity, minimum=0.0
        ),
        context_match=v.number(
            weights_raw, "context_match", "suggestions.weights", d_w.context_match, minimum=0.0
        ),
        type_bonus=bonus,
    )

    learning_raw = v.table(table, "learning", "suggestions")
    v.check_unknown(learning_raw, _LEARNING_KEYS, "suggestions.learning")
    d_l = LearningConfig()
    learning = LearningConfig(
        recency_decay=v.number(
            learning_raw,
            "recency_decay",
            "suggestions.learning",
            d_l.recency_decay,
            minimum=0.0,
            maximum=1.0,
            exclusive_minimum=True,
        ),
        frequency_boost=v.number(
            learning_raw,
            "frequency_boost",
            "suggestions.learning",
            d_l.frequency_boost,
            minimum=0.0,
            exclusive_minimum=True,
        ),
        max_history=v.integer(
            learning_raw, "max_history", "suggestions.learning", d_l.max_history, minimum=1
        ),
        eviction_days=v.integer(
            learning_raw, "eviction_days", "suggestions.learning", d_l.eviction_days, minimum=1
        ),
        min_confidence=v.number(
            learning_raw,
            "min_confidence",
            "suggestions.learning",
            d_l.min_confidence,
            minimum=0.0,
            maximum=1.0,
        ),
    )

    d_s = SuggestionsConfig()
    return SuggestionsConfig(
        max_suggestions=v.integer(
            table, "max_suggestions", "suggestions", d_s.max_suggestions, minimum=1
        ),
        always_show_archive=v.boolean(
            table, "always_show_archive", "suggestions", d_s.always_show_archive
        ),
        weights=weights,
        learning=learning,
        tag_normalization=v.string_map(table, "tag_normalization", "suggestions"),
    )


_FILE_OPS_KEYS = {"create_backups", "backup_dir", "log_operations", "auto_create_folders"}


def _validate_file_ops(v: _Validator, raw: dict[str, Any]) -> FileOpsConfig:
    table = v.table(raw, "file_ops", "")
    v.check_unknown(table, _FILE_OPS_KEYS, "file_ops")
    d = FileOpsConfig()
    backup_dir = v.string(table, "backup_dir", "file_ops", d.backup_dir)
    assert backup_dir is not None
    if Path(backup_dir).is_absolute():
        v.fail(
            "file_ops.backup_dir",
            f"must be relative to the vault root, got absolute path {backup_dir!r}",
            hint="e.g. backup_dir = \".backups\" (spec 05 §1.4)",
        )
    return FileOpsConfig(
        create_backups=v.boolean(table, "create_backups", "file_ops", d.create_backups),
        backup_dir=backup_dir,
        log_operations=v.boolean(table, "log_operations", "file_ops", d.log_operations),
        auto_create_folders=v.boolean(
            table, "auto_create_folders", "file_ops", d.auto_create_folders
        ),
    )


_METADATA_FIELD_KEYS = {
    "key",
    "type",
    "keymap",
    "prompt",
    "append",
    "complete",
    "values",
    "normalize",
}
_METADATA_TYPES = frozenset({"list", "string", "boolean", "enum", "number"})


def _validate_metadata_fields(v: _Validator, raw: dict[str, Any]) -> list[MetadataFieldConfig]:
    entries = v.array_of_tables(raw, "metadata_fields", "")
    if entries is None:
        return default_metadata_fields()

    fields: list[MetadataFieldConfig] = []
    seen_keys: dict[str, int] = {}
    seen_maps: dict[str, str] = {}
    for i, entry in enumerate(entries):
        prefix = f"metadata_fields[{i}]"
        v.check_unknown(entry, _METADATA_FIELD_KEYS, prefix)
        key = v.string(entry, "key", prefix, required=True)
        field_type = v.string(entry, "type", prefix, required=True, choices=_METADATA_TYPES)
        keymap = v.string(entry, "keymap", prefix, required=True)
        assert key is not None and field_type is not None and keymap is not None

        if key in seen_keys:
            v.fail(
                f"{prefix}.key",
                f"duplicates metadata_fields[{seen_keys[key]}].key ({key!r})",
                hint="each frontmatter key may be configured once (spec 07)",
            )
        seen_keys[key] = i

        # 07 acceptance test 4: name BOTH bindings.
        if keymap in CORE_KEYMAPS:
            v.fail(
                f"{prefix}.keymap",
                f"{keymap!r} collides with the core organize keymap {keymap!r} = "
                f"{CORE_KEYMAPS[keymap]!r}",
                hint=f"pick a key outside {sorted(CORE_KEYMAPS)} for metadata field {key!r}",
            )
        if keymap in seen_maps:
            v.fail(
                f"{prefix}.keymap",
                f"{keymap!r} collides with metadata field {seen_maps[keymap]!r}",
                hint="metadata field keymaps must be unique (spec 07)",
            )
        seen_maps[keymap] = key

        values = v.string_list(entry, "values", prefix)
        if field_type == "enum" and not values:
            v.fail(f"{prefix}.values", "is required for type = \"enum\"", hint="e.g. values = [\"high\", \"medium\", \"low\"]")
        if field_type != "enum" and values:
            v.fail(f"{prefix}.values", f"is only valid for type = \"enum\", not {field_type!r}")

        complete_raw = entry.get("complete", _MISSING)
        complete: str | list[str] | None
        if complete_raw is _MISSING:
            complete = None
        elif isinstance(complete_raw, str):
            if complete_raw != "existing":
                v.fail(
                    f"{prefix}.complete",
                    f"must be \"existing\" or an array of values, got {complete_raw!r}",
                )
            complete = complete_raw
        elif isinstance(complete_raw, list):
            complete = v.string_list(entry, "complete", prefix)
        else:
            v.fail(
                f"{prefix}.complete",
                f"must be \"existing\" or an array of values, got {_typename(complete_raw)}",
            )

        normalize = v.string(entry, "normalize", prefix, None, choices=frozenset({"kebab"}))
        fields.append(
            MetadataFieldConfig(
                key=key,
                type=field_type,  # type: ignore[arg-type]
                keymap=keymap,
                prompt=v.string(entry, "prompt", prefix, None),
                append=v.boolean(entry, "append", prefix, True),
                complete=complete,
                values=values,
                normalize=normalize,  # type: ignore[arg-type]
            )
        )
    return fields


_ROUTE_KEYS = {"tags", "destination", "mode", "description", "template", "auto", "review"}
_ROUTE_MODES = frozenset({"append", "move", "integrate"})
_REVIEW_GATES = frozenset({"diff", "auto"})
_EDIT_MODES = frozenset({"manual", "append", "integrate"})


def _validate_routes(v: _Validator, raw: dict[str, Any]) -> list[RouteConfig]:
    entries = v.array_of_tables(raw, "routes", "", cls=RouteConfigError)
    if entries is None:
        return []
    routes: list[RouteConfig] = []
    for i, entry in enumerate(entries):
        try:
            routes.append(_validate_route(v, entry, i))
        except RouteConfigError:
            raise
        except ConfigError as exc:  # every route problem is a RouteConfigError
            raise RouteConfigError(str(exc), hint=exc.hint) from exc
    return routes


def _validate_route(v: _Validator, entry: dict[str, Any], i: int) -> RouteConfig:
    prefix = f"routes[{i}]"
    v.check_unknown(entry, _ROUTE_KEYS, prefix, cls=RouteConfigError)
    tags = v.string_list(entry, "tags", prefix, required=True, non_empty=True, cls=RouteConfigError)
    for j, tag in enumerate(tags):
        parts = [part.strip() for part in tag.split("+")]
        if any(not part for part in parts):
            v.fail(
                f"{prefix}.tags[{j}]",
                f"is not a parseable tag expression: {tag!r}",
                hint='use "tag" for any-of or "a+b" to require both tags (spec 11 §1)',
                cls=RouteConfigError,
            )
    destination = v.string(entry, "destination", prefix, required=True, cls=RouteConfigError)
    mode = v.string(entry, "mode", prefix, required=True, choices=_ROUTE_MODES, cls=RouteConfigError)
    assert destination is not None and mode is not None
    if destination.startswith("/") or Path(destination).is_absolute():
        v.fail(
            f"{prefix}.destination",
            f"must be relative to the vault root, got {destination!r}",
            hint='e.g. destination = "areas/health/training-log.md" (spec 11 §1)',
            cls=RouteConfigError,
        )
    if ".." in Path(destination).parts:
        v.fail(
            f"{prefix}.destination",
            f"must stay inside the vault, got {destination!r}",
            cls=RouteConfigError,
        )
    is_folder = destination.endswith("/")
    if is_folder and mode != "move":
        v.fail(
            f"{prefix}.mode",
            f"is {mode!r} but destination {destination!r} is a folder — folders get \"move\"",
            hint='drop the trailing "/" to append/integrate into a file (spec 11 §1)',
            cls=RouteConfigError,
        )
    if not is_folder and mode == "move":
        v.fail(
            f"{prefix}.mode",
            f"is \"move\" but destination {destination!r} is a file — files get "
            f"\"append\" or \"integrate\"",
            hint='add a trailing "/" to move into a folder (spec 11 §1)',
            cls=RouteConfigError,
        )
    review = v.string(
        entry, "review", prefix, "diff", choices=_REVIEW_GATES, cls=RouteConfigError
    )
    # Every key is honored or it is an error (03 §1). `review` only has meaning
    # for an integrate route; accepting it silently on a move/append route would
    # be a dead key of exactly the 08 §A35 kind.
    if review != "diff" and mode != "integrate":
        v.fail(
            f"{prefix}.review",
            f"is {review!r} but mode is {mode!r} — the review gate applies to "
            f'integrate results only (spec 12 §1)',
            hint='set mode = "integrate" for this destination, or drop `review`',
            cls=RouteConfigError,
        )
    return RouteConfig(
        tags=[tag.strip() for tag in tags],
        destination=destination,
        mode=mode,  # type: ignore[arg-type]
        description=v.string(entry, "description", prefix, "", allow_empty=True) or "",
        template=v.string(entry, "template", prefix, None, allow_empty=True),
        auto=v.boolean(entry, "auto", prefix, False),
        review=review,  # type: ignore[arg-type]
    )


_CONSUMER_FRAMEWORK_KEYS = {
    "type",
    "enabled",
    "include_paths",
    "exclude_paths",
    "max_notes_per_run",
    "env",
}


def _validate_consumers(v: _Validator, raw: dict[str, Any]) -> list[ConsumerConfig]:
    table = v.table(raw, "consumers", "")
    known_types = _registered_consumer_types()
    consumers: list[ConsumerConfig] = []
    for name, section in table.items():
        prefix = f"consumers.{name}"
        if not isinstance(section, dict):
            v.fail(prefix, f"must be a table, got {_typename(section)}")
        ctype = v.string(section, "type", prefix, required=True)
        assert ctype is not None
        if ctype not in known_types:
            close = difflib.get_close_matches(ctype, sorted(known_types), n=1, cutoff=0.5)
            v.fail(
                f"{prefix}.type",
                f"{ctype!r} is not a registered consumer type",
                hint=(
                    f"did you mean {close[0]!r}?"
                    if close
                    else "registered types: " + ", ".join(sorted(known_types))
                ),
            )
        env = v.string_map(section, "env", prefix)
        options = {k: val for k, val in section.items() if k not in _CONSUMER_FRAMEWORK_KEYS}
        consumers.append(
            ConsumerConfig(
                name=name,
                type=ctype,
                enabled=v.boolean(section, "enabled", prefix, True),
                include_paths=v.string_list(section, "include_paths", prefix),
                exclude_paths=v.string_list(section, "exclude_paths", prefix),
                max_notes_per_run=v.integer(
                    section, "max_notes_per_run", prefix, 50, minimum=1
                ),
                options=options,
                env=env,
            )
        )
    return consumers


_LLM_KEYS = {
    "backend",
    "ollama_host",
    "ollama_model",
    "claude_command",
    "integrate_backend",
    "timeout_seconds",
    "retries",
}
_LLM_BACKENDS = frozenset({"ollama", "claude-cli"})


def _validate_llm(v: _Validator, raw: dict[str, Any]) -> LLMConfig:
    present = "llm" in raw
    table = v.table(raw, "llm", "")
    v.check_unknown(table, _LLM_KEYS, "llm")
    d = LLMConfig()
    backend = v.string(table, "backend", "llm", d.backend, choices=_LLM_BACKENDS)
    integrate_backend = v.string(
        table, "integrate_backend", "llm", d.integrate_backend, choices=_LLM_BACKENDS
    )
    ollama_host = v.string(table, "ollama_host", "llm", None)
    ollama_model = v.string(table, "ollama_model", "llm", None)
    claude_command = v.string_list(table, "claude_command", "llm", d.claude_command, non_empty=True)
    timeout_seconds = v.number(
        table, "timeout_seconds", "llm", d.timeout_seconds, minimum=0.0, exclusive_minimum=True
    )
    retries = v.integer(table, "retries", "llm", d.retries, minimum=0)
    # Spec 06 §2: no hardcoded host/model fallbacks in code. If the section is
    # configured at all and it selects Ollama, the host and model are required
    # here rather than silently defaulting to a typo'd model at call time.
    # Checked after the leaf validations so a leaf error wins.
    if present and "ollama" in (backend, integrate_backend):
        for key, value in (("ollama_host", ollama_host), ("ollama_model", ollama_model)):
            if value is None:
                v.fail(
                    f"llm.{key}",
                    "is required when an ollama backend is selected",
                    hint=(
                        'e.g. ollama_host = "http://server.matthandzel.com:11434", '
                        'ollama_model = "gemma3:12b-it-qat" (spec 06 §2 — no hardcoded fallbacks)'
                    ),
                )
    return LLMConfig(
        backend=backend,  # type: ignore[arg-type]
        ollama_host=ollama_host,
        ollama_model=ollama_model,
        claude_command=claude_command,
        integrate_backend=integrate_backend,  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
        retries=retries,
    )


_AUTO_ORGANIZE_KEYS = {"trust", "confidence_threshold", "min_precedents"}
_TRUST_LEVELS = frozenset({"propose", "auto_below"})


def _validate_integrate(v: _Validator, raw: dict[str, Any]) -> IntegrateConfig:
    table = v.table(raw, "integrate", "")
    v.check_unknown(table, {"default_mode", "review", "max_deleted_lines"}, "integrate")
    d = IntegrateConfig()
    return IntegrateConfig(
        default_mode=v.string(  # type: ignore[arg-type]
            table, "default_mode", "integrate", d.default_mode, choices=_EDIT_MODES
        ),
        review=v.string(  # type: ignore[arg-type]
            table, "review", "integrate", d.review, choices=_REVIEW_GATES
        ),
        max_deleted_lines=v.integer(
            table, "max_deleted_lines", "integrate", d.max_deleted_lines, minimum=0
        ),
    )


def _validate_auto_organize(v: _Validator, raw: dict[str, Any]) -> AutoOrganizeConfig:
    table = v.table(raw, "auto_organize", "")
    v.check_unknown(table, _AUTO_ORGANIZE_KEYS, "auto_organize")
    d = AutoOrganizeConfig()
    return AutoOrganizeConfig(
        trust=v.string(table, "trust", "auto_organize", d.trust, choices=_TRUST_LEVELS),  # type: ignore[arg-type]
        confidence_threshold=v.number(
            table,
            "confidence_threshold",
            "auto_organize",
            d.confidence_threshold,
            minimum=0.0,
            maximum=1.0,
        ),
        min_precedents=v.integer(
            table, "min_precedents", "auto_organize", d.min_precedents, minimum=0
        ),
    )


def _validate_server(v: _Validator, raw: dict[str, Any]) -> ServerConfig:
    table = v.table(raw, "server", "")
    v.check_unknown(table, {"socket_path", "idle_timeout_seconds"}, "server")
    d = ServerConfig()
    socket_path = v.string(table, "socket_path", "server", None)
    return ServerConfig(
        socket_path=Path(socket_path) if socket_path is not None else None,
        idle_timeout_seconds=v.number(
            table,
            "idle_timeout_seconds",
            "server",
            d.idle_timeout_seconds,
            minimum=0.0,
            exclusive_minimum=True,
        ),
    )


def _validate_logging(v: _Validator, raw: dict[str, Any]) -> LoggingConfig:
    table = v.table(raw, "logging", "")
    v.check_unknown(table, {"level"}, "logging")
    level = v.string(table, "level", "logging", LoggingConfig().level)
    assert level is not None
    if level.upper() not in _LOG_LEVELS:
        v.fail("logging.level", f"must be one of {sorted(_LOG_LEVELS)}, got {level!r}")
    return LoggingConfig(level=level.upper())


def _validate_descriptions(v: _Validator, raw: dict[str, Any]) -> dict[str, str]:
    table = v.table(raw, "descriptions", "")
    out: dict[str, str] = {}
    for key, value in table.items():
        if not isinstance(value, str):
            v.fail(
                f"descriptions.\"{key}\"",
                f"must be a string, got {_typename(value)}",
                hint="[descriptions] maps a vault-relative path to natural language (spec 11 §3)",
            )
        out[key] = value
    return out


_TOP_LEVEL_KEYS = {
    "vault",
    "suggestions",
    "file_ops",
    "metadata_fields",
    "routes",
    "consumers",
    "llm",
    "integrate",
    "auto_organize",
    "server",
    "logging",
    "descriptions",
}


def load_config(paths: CorePaths, *, config_file: Path | None = None) -> Config:
    """Read TOML from ``config_file`` (default ``paths.config_file``),
    expand/resolve all path values against the process env captured in
    ``paths`` (spec 06 §2), and return :func:`validate_config` of the result.
    Missing file ⇒ ConfigError with a hint pointing at the example config
    (loud failure, never silently run on defaults — spec 01 §success #4)."""
    path = Path(config_file) if config_file is not None else paths.config_file
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}",
            hint=(
                f"write one to {path} — `organize health --example-config` prints a complete "
                "commented example (organize_core.config.example_config_toml). organize-core "
                "never runs on implicit defaults."
            ),
        )
    if path.is_dir():
        raise ConfigError(
            f"config path is a directory, not a file: {path}",
            hint="point $ORGANIZE_CORE_CONFIG_DIR at the directory containing config.toml",
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}", hint="check quoting and table headers") from exc

    config = validate_config(raw, source=str(path))
    # Path-valued settings are expanded/resolved through paths.expand — the one
    # module allowed to consult the process environment (spec 10 §3).
    return replace(
        config,
        vault=replace(config.vault, root=_expand_path(config.vault.root)),
        server=replace(
            config.server,
            socket_path=(
                _expand_path(config.server.socket_path)
                if config.server.socket_path is not None
                else None
            ),
        ),
    )


def _expand_path(value: Path) -> Path:
    """Expand ``~``/``$VARS`` (delegated to :func:`organize_core.paths.expand`,
    the single process-environment touchpoint) and resolve symlinks so the DB
    and index canonicalize identically (spec 06 §2)."""
    text = str(value)
    if "~" in text or "$" in text:
        return expand(text)
    return Path(text).resolve()


def validate_config(raw: dict[str, Any], *, source: str = "<config>") -> Config:
    """Validate a raw TOML mapping into a :class:`Config`.

    Enforces (spec 03 §1, 06 §2, 07, 11 §1):
    - unknown key at any depth ⇒ ConfigError naming the dotted key,
    - leaf type/enum/range checks,
    - route mode/destination consistency (RouteConfigError),
    - metadata-field keymap collisions with core keymaps and each other,
    - consumer sections have a registered ``type``.
    Pure — no filesystem access (existence checks are :func:`check_vault`).
    """
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{source}: config must be a table, got {_typename(raw)}",
            hint="the file's top level is TOML tables like [vault] and [suggestions]",
        )
    v = _Validator(source)
    v.check_unknown(raw, _TOP_LEVEL_KEYS, "")
    return Config(
        vault=_validate_vault(v, raw),
        suggestions=_validate_suggestions(v, raw),
        file_ops=_validate_file_ops(v, raw),
        metadata_fields=_validate_metadata_fields(v, raw),
        routes=_validate_routes(v, raw),
        consumers=_validate_consumers(v, raw),
        llm=_validate_llm(v, raw),
        integrate=_validate_integrate(v, raw),
        auto_organize=_validate_auto_organize(v, raw),
        server=_validate_server(v, raw),
        logging=_validate_logging(v, raw),
        descriptions=_validate_descriptions(v, raw),
    )


# --- spec 07 metadata-field coercion (shared by BOTH doors) ----------------

_TRUE_WORDS = frozenset({"true", "yes", "on", "1"})
_FALSE_WORDS = frozenset({"false", "no", "off", "0"})


def metadata_fields_by_key(config: Config) -> dict[str, MetadataFieldConfig]:
    """``{key: MetadataFieldConfig}`` for the configured ``metadata_fields``."""
    return {entry.key: entry for entry in config.metadata_fields}


def coerce_metadata_value(entry: MetadataFieldConfig | None, key: str, raw: Any) -> Any:
    """Apply one ``[[metadata_fields]]`` rule to one incoming value (spec 07).

    This lives in ``config`` — not in ``cli`` where it started — because
    spec 10 §3 puts ``metadata_fields`` in CORE config precisely so that
    "CLI and UI can never disagree". While the coercion was CLI-private, the
    RPC ``meta.set`` handed the client's raw value straight to
    ``update_frontmatter``: the nvim client, whose only write path is RPC,
    could write ``tags: ["foo, Bar Baz"]``, ``importance: totally-invalid``
    and ``remember: maybe`` into the vault, failing doc 07 acceptance tests
    1 and 2 on that boundary while the CLI enforced them.

    Rules, per field ``type``:

    * ``list`` — split a string on commas, kebab-normalize when
      ``normalize = "kebab"``, dedupe preserving order. An already-list value
      (the natural JSON-RPC shape) skips the split and is normalized
      element-wise.
    * ``enum`` — membership in ``values`` or ``ConfigError``.
    * ``boolean`` / ``number`` — parsed from a string, or passed through when
      the caller already sent the right JSON type.
    * unconfigured key — returned unchanged; frontmatter is arbitrary by
      design (07 "Downstream note").

    An empty string REMOVES the field (``update_frontmatter``'s ``None``
    contract); an explicit ``None`` is already a removal and passes through.
    """
    if raw is None:
        return None
    if isinstance(raw, str) and raw == "":
        return None
    if entry is None:
        return raw

    if entry.type == "list":
        if isinstance(raw, (list, tuple)):
            values = [str(item).strip() for item in raw if str(item).strip()]
        else:
            values = [part.strip() for part in str(raw).split(",") if part.strip()]
        if entry.normalize == "kebab":
            from organize_core.frontmatter import normalize_tag

            values = [normalize_tag(value) for value in values]
        deduped: list[str] = []
        for value in values:
            if value not in deduped:
                deduped.append(value)
        return deduped

    if entry.type == "enum":
        text = str(raw)
        if text not in entry.values:
            raise ConfigError(
                f"{key}={text!r} is not one of the configured values",
                hint="allowed values: " + ", ".join(entry.values),
            )
        return text

    if entry.type == "boolean":
        if isinstance(raw, bool):
            return raw
        lowered = str(raw).strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
        raise ConfigError(
            f"{key}={raw!r} is not a boolean",
            hint="use one of: " + ", ".join(sorted(_TRUE_WORDS | _FALSE_WORDS)),
        )

    if entry.type == "number":
        if isinstance(raw, bool):
            raise ConfigError(
                f"{key}={raw!r} is not a number",
                hint=f'[[metadata_fields]] key = {key!r} declares type = "number"',
            )
        if isinstance(raw, (int, float)):
            return raw
        text = str(raw).strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            raise ConfigError(
                f"{key}={raw!r} is not a number",
                hint=f'[[metadata_fields]] key = {key!r} declares type = "number"',
            ) from None

    return raw


@dataclass(frozen=True)
class HealthIssue:
    """One startup/health finding (spec 03 §1-2 checkhealth)."""

    severity: Literal["error", "warning", "info"]
    message: str
    hint: str | None = None


def _is_writable(path: Path) -> bool:
    # os.access only — this module never reads os.environ (spec 10 §3 rule:
    # the process environment is paths.py's business alone).
    return os.access(path, os.W_OK)


def _subdir_names(directory: Path) -> list[str]:
    """Subdirectory names, or ``[]`` if the directory cannot be read. Health
    reporting never raises — an unreadable vault is itself reported, not a
    traceback."""
    try:
        return [child.name for child in directory.iterdir() if child.is_dir()]
    except OSError:  # pragma: no cover - unreadable directory
        return []


def _misconfiguration_hint(root: Path, rel: str, key: str) -> str | None:
    """Actionable hint for a configured-but-absent vault folder — the 08 §C
    class: ``archives`` vs ``archive`` (C2) and a ``vault.root`` pointing one
    level too deep (C1)."""
    rel_path = Path(rel)
    parent = root / rel_path.parent
    if parent.is_dir():
        siblings = sorted(_subdir_names(parent))
        wanted = rel_path.name
        close = difflib.get_close_matches(wanted, siblings, n=1, cutoff=0.6)
        # singular/plural is the exact live defect (archives vs archive)
        for candidate in (wanted.rstrip("s"), wanted + "s"):
            if candidate != wanted and candidate in siblings:
                close = [candidate]
                break
        if close:
            fixed = str(rel_path.parent / close[0]) if str(rel_path.parent) != "." else close[0]
            return f"the vault has {close[0]!r} — set {key} = {fixed!r}"
    if (root.parent / rel_path).is_dir():
        return (
            f"vault.root may be one level too deep: {root.parent} contains {rel!r}. "
            f"Set vault.root = {str(root.parent)!r}"
        )
    for name in sorted(_subdir_names(root)):
        child = root / name
        if (child / rel_path).is_dir():
            return (
                f"vault.root may be one level too high: {child} contains {rel!r}. "
                f"Set vault.root = {str(child)!r}"
            )
    return None


def _check_folder(
    issues: list[HealthIssue],
    root: Path,
    rel: str,
    key: str,
    *,
    severity: Literal["error", "warning"] = "error",
) -> None:
    target = root / rel
    if not target.exists():
        issues.append(
            HealthIssue(
                severity,
                f"{key}: {rel!r} does not exist under the vault root ({target})",
                hint=_misconfiguration_hint(root, rel, key),
            )
        )
        return
    if not target.is_dir():
        issues.append(
            HealthIssue("error", f"{key}: {target} exists but is not a directory", hint=None)
        )
        return
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        issues.append(
            HealthIssue(
                "warning",
                f"{key}: {target} resolves to {resolved}, outside the vault root {root}",
                hint="vault-relative paths are computed after symlink resolution; keep "
                "scan dirs and PARA folders inside the vault (08 §B18)",
            )
        )
    if not _is_writable(resolved):
        issues.append(
            HealthIssue("error", f"{key}: {resolved} is not writable", hint="check permissions")
        )


def check_vault(config: Config) -> list[HealthIssue]:
    """Verify vault root, capture folder, every ``para_folders`` entry and
    each scan dir exist and are writable, resolving symlinks before
    comparison (02: ``~/notes`` == ``~/Obsidian/Main``). Returns issues —
    callers decide fail-vs-warn; ``organize health`` prints all of them.
    Must catch both live misconfigurations (08 §C1-2)."""
    issues: list[HealthIssue] = []
    root = config.vault.root
    if not root.exists():
        issues.append(
            HealthIssue(
                "error",
                f"vault.root does not exist: {root}",
                hint="set [vault] root to the vault (e.g. \"~/Obsidian/Main\"); "
                "~/notes is a symlink to the same tree (spec 02)",
            )
        )
        return issues
    if not root.is_dir():
        issues.append(
            HealthIssue("error", f"vault.root is not a directory: {root}", hint=None)
        )
        return issues

    root = root.resolve()
    if not _is_writable(root):
        issues.append(
            HealthIssue("error", f"vault.root is not writable: {root}", hint="check permissions")
        )

    _check_folder(issues, root, config.vault.capture_folder, "vault.capture_folder")
    _check_folder(issues, root, config.vault.raw_capture_folder, "vault.raw_capture_folder")
    for name in sorted(config.vault.para_folders):
        _check_folder(issues, root, config.vault.para_folders[name], f"vault.para_folders.{name}")

    archives = config.vault.para_folders.get("archives")
    if archives:
        archive_target = f"{archives}/{config.vault.archive_capture_path}"
        if (root / archives).is_dir():
            _check_folder(
                issues,
                root,
                archive_target,
                "vault.archive_capture_path",
                severity="error" if not config.file_ops.auto_create_folders else "warning",
            )

    for rel in config.vault.scan_dirs:
        _check_folder(issues, root, rel, "vault.scan_dirs")

    return issues


#: The single shipped example (spec 06 §2: ONE example file). Kept as a
#: literal rather than generated so the comments explaining each key are real
#: prose; ``tests/test_config_example.py`` proves it loads cleanly and that
#: every dataclass field appears in it.
EXAMPLE_CONFIG_TOML = '''\
# organize-core configuration — the ONE behavioral config file (spec 10 §3).
# Location: ~/.config/organize-core/config.toml (override the directory with
# $ORGANIZE_CORE_CONFIG_DIR). The Neovim plugin's setup() keeps UI concerns
# only; everything below is shared by the CLI, the server and the pipeline.
#
# Every key here is honored. An unknown key is a hard error naming the key
# (spec 03 §1) — there are no silently-ignored settings.

[vault]
# The vault root. ~/notes is a symlink to the same tree (spec 02).
root = "~/Obsidian/Main"
# Capture folders, relative to root.
capture_folder = "capture"
raw_capture_folder = "capture/raw_capture"
# Where organized originals are archived, relative to para_folders.archives.
archive_capture_path = "capture/raw_capture"
# Pipeline + indexer scan scope (spec 06 §2).
scan_dirs = ["capture/raw_capture", "resources", "areas", "projects"]
# Prefix or glob; applied by the pipeline and the indexer.
ignore_patterns = [".obsidian", ".git", "resources/flashcards"]
# Files larger than this (bytes) are skipped by the indexer, loudly.
max_file_size = 1048576
# Debounce (ms) for incremental index updates in `organize serve`.
incremental_debounce = 500

# PARA folder names. NOTE: the vault's archive folder is SINGULAR — the live
# misconfiguration was `archives = "archives"` (08 §C2).
[vault.para_folders]
projects = "projects"
areas = "areas"
resources = "resources"
archives = "archive"

[suggestions]
# Total list length INCLUDING the always-shown archive entry (spec 04 §1).
max_suggestions = 10
always_show_archive = true

# Scoring weights (spec 04 §2). These are the defaults-of-record; acceptance
# tests assert them numerically.
[suggestions.weights]
exact_tag_match = 2.0
normalized_tag_match = 1.5
learned_association = 1.8
source_match = 1.3
alias_similarity = 1.1
context_match = 1.0

# Folder-type bonus (spec 04 §2 #7 — configurable, no longer hardcoded).
[suggestions.weights.type_bonus]
projects = 0.3
areas = 0.2
resources = 0.1

# Learning knobs (spec 04 §4-5).
[suggestions.learning]
recency_decay = 0.9      # per-day base: 0.9 ** days_since_use
frequency_boost = 1.2    # applied once an association is used > 5 times
max_history = 1000       # association cap; oldest by last_used evicted first
eviction_days = 90       # hard-delete horizon
min_confidence = 0.3     # candidate floor (the archive entry is exempt)

# Tag → PARA-folder normalization used by the normalized-tag signal.
[suggestions.tag_normalization]
project = "projects"
area = "areas"
resource = "resources"

[file_ops]
create_backups = true      # backup before every mutating op (spec 05 §1.4)
backup_dir = ".backups"    # relative to the vault root
log_operations = true      # append to <state>/operations.log (spec 05 §1.5)
auto_create_folders = true # create missing destination folders on demand

# Metadata fields editable from the organize UI (spec 07). Adding a field is
# pure configuration — no code changes. Keymaps may not collide with the core
# organize keymaps (<CR>, s, a, m, r, p, ?, A, ...).
[[metadata_fields]]
key = "tags"
type = "list"          # list | string | boolean | enum | number
keymap = "t"
prompt = "Add tag(s)"
append = true          # lists: append (default) vs replace
complete = "existing"  # "existing" (values seen in the index) or an array
normalize = "kebab"    # "Bar Baz" -> "bar-baz"

[[metadata_fields]]
key = "importance"
type = "enum"
keymap = "i"
values = ["high", "medium", "low"]

[[metadata_fields]]
key = "remember"
type = "boolean"
keymap = "v"

# Routes: standard places for tagged things (spec 11 §1). Folder destinations
# (trailing "/") use mode = "move"; file destinations use "append"/"integrate".
# `auto = true` opts the route into unattended handling by the tag_router
# consumer; `description` is natural language and is load-bearing.
[[routes]]
tags = ["workout", "training"]   # any-of; "a+b" requires both tags
destination = "areas/health/training-log.md"
mode = "append"
auto = false
template = "## {date} — from {capture_id}"
description = """My running/lifting training log. New entries are appended
chronologically under a date heading. Short workout notes, PRs, and how
sessions felt belong here — not general health research."""

[[routes]]
tags = ["blog-idea"]
destination = "projects/blog/ideas.md"
mode = "append"
description = "One-line blog post ideas, appended as list items under ## Inbox."

[[routes]]
tags = ["impro", "theatre"]
destination = "resources/performing/"
mode = "move"
description = "Notes about improvisation, theatre, and performance practice."

# An `integrate` route: Claude rewrites the target to weave the capture in
# (spec 12 §1). `review` is the per-route gate — "diff" shows you the change
# before it lands, "auto" applies it silently. Prefer "diff" until you trust a
# given destination; either way the target is backed up and the write is atomic,
# and a `no-ai: true` target refuses integrate outright.
[[routes]]
tags = ["kms", "second-brain"]
destination = "projects/kms/design-notes.md"
mode = "integrate"
review = "diff"                  # diff | auto
description = """Running design notes for the knowledge-management system.
New thinking is woven into the relevant existing section rather than appended,
so this one is worth an LLM pass."""

# Automation consumers (spec 06 §2). `type` must be a registered consumer
# type; keys outside the framework set below are consumer-specific options and
# are validated by the consumer itself.
[consumers.taskwarrior]
type = "taskwarrior"
enabled = true
include_paths = ["capture/raw_capture"]
exclude_paths = []
max_notes_per_run = 50
marker_tag = "todo"
default_project = "Inbox"
additional_tags = ["para", "automation"]
review_tag = "not_reviewed"
annotation_template = "Captured from {id}"
llm_enabled = false

[consumers.learn]
type = "learn"
enabled = true
include_paths = ["resources", "areas", "projects"]
exclude_paths = ["resources/flashcards"]
max_notes_per_run = 20
flashcard_dir = "resources/flashcards"
review_dir = "resources/flashcards/review"
deck = "Reading::Articles"
card_tags = ["learn-consumer"]
min_content_length = 200
max_cards_per_note = 12
whisper_host = "http://server.matthandzel.com:47770"

[consumers.question_answer]
type = "question_answer"
enabled = true
include_paths = ["capture/raw_capture"]
max_notes_per_run = 20
marker_tag = "question"
answers_dir = "resources/answers"

[consumers.deep_research]
type = "deep_research"
enabled = false
include_paths = ["areas/relationships"]
max_notes_per_run = 5
command = ["python", "main.py"]
cwd = "~/Projects/DeepResearchAgent"
timeout_seconds = 900

# Generic environment passthrough for the research subprocess (spec 06 §3.4).
[consumers.deep_research.env]
NOTES_DIR = "~/Obsidian/Main"

# The ONE shared LLM client (spec 09 §2). Hosts and models are config-required
# — there are no hardcoded fallbacks in code (06 §2).
[llm]
backend = "ollama"                                    # ollama | claude-cli
ollama_host = "http://server.matthandzel.com:11434"
ollama_model = "gemma3:12b-it-qat"
claude_command = ["claude", "-p"]
integrate_backend = "claude-cli"                      # quality-sensitive path (12 §1)
timeout_seconds = 60.0
retries = 2

# Edit modes and integrate safety (spec 12 §1). `default_mode` is the global
# default; a route's `mode` overrides it. `max_deleted_lines` is the integrate
# hard-reject threshold: an LLM result that deletes more than this many existing
# non-whitespace lines is refused outright (default zero — integration adds and
# weaves, it never destroys).
[integrate]
default_mode = "manual"      # manual | append | integrate
review = "diff"              # diff (show me the change) | auto (apply silently)
max_deleted_lines = 0

# Automatic organize trust ladder (spec 13 §2).
[auto_organize]
trust = "propose"            # propose | auto_below
confidence_threshold = 0.8
min_precedents = 5           # never auto-apply below this many accepted precedents

[server]
socket_path = "$XDG_RUNTIME_DIR/organize-core.sock"
idle_timeout_seconds = 600.0

[logging]
level = "INFO"               # DEBUG | INFO | WARNING | ERROR | CRITICAL

# Natural-language descriptions for folders/files that are not route targets
# (spec 11 §3). A folder's index.md frontmatter `description` wins over this.
[descriptions]
"areas/health" = "Health, training, sleep and recovery — the running log lives in training-log.md."
"projects/blog" = "Posts in flight for matthandzel.com, plus the raw idea inbox."
'''


def example_config_toml() -> str:
    """Render the ONE shipped example config (spec 06 §2: one example file;
    the live file is the deploy target). Every key present, commented."""
    return EXAMPLE_CONFIG_TOML
