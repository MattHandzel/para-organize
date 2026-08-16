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
    [state], [logging]     spec 06 §2

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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from organize_core.paths import CorePaths

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


def default_metadata_fields() -> list[MetadataFieldConfig]:
    """Ships with ``tags`` (list, ``t``) and ``importance`` (enum, ``i``)
    so the feature works out of the box (spec 07 "Behavior")."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# [[routes]] (spec 11 §1)
# ---------------------------------------------------------------------------

RouteMode = Literal["append", "move", "integrate"]


@dataclass(frozen=True)
class RouteConfig:
    """One tag→destination route (spec 11 §1). ``tags`` entries are any-of;
    an entry ``"a+b"`` requires both tags. Folder destinations get ``move``;
    file destinations get ``append``/``integrate`` — violations are a
    RouteConfigError at load time. ``description`` is Matt's natural
    language and is load-bearing (UI display, auto-tagger and doc-13 input).
    ``auto=True`` opts the route into unattended handling (11 §1)."""

    tags: list[str]
    destination: str  # relative to vault root; trailing "/" ⇒ folder
    mode: RouteMode
    description: str = ""
    template: str | None = None  # append-heading template override (11 §1)
    auto: bool = False


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
    auto_organize: AutoOrganizeConfig = field(default_factory=AutoOrganizeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    # Central [descriptions] table: path (vault-relative) → NL description
    # (spec 11 §3 fallback storage when a folder has no index.md).
    descriptions: dict[str, str] = field(default_factory=dict)


def load_config(paths: CorePaths, *, config_file: Path | None = None) -> Config:
    """Read TOML from ``config_file`` (default ``paths.config_file``),
    expand/resolve all path values against the process env captured in
    ``paths`` (spec 06 §2), and return :func:`validate_config` of the result.
    Missing file ⇒ ConfigError with a hint pointing at the example config
    (loud failure, never silently run on defaults — spec 01 §success #4)."""
    raise NotImplementedError


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
    raise NotImplementedError


@dataclass(frozen=True)
class HealthIssue:
    """One startup/health finding (spec 03 §1-2 checkhealth)."""

    severity: Literal["error", "warning", "info"]
    message: str
    hint: str | None = None


def check_vault(config: Config) -> list[HealthIssue]:
    """Verify vault root, capture folder, every ``para_folders`` entry and
    each scan dir exist and are writable, resolving symlinks before
    comparison (02: ``~/notes`` == ``~/Obsidian/Main``). Returns issues —
    callers decide fail-vs-warn; ``organize health`` prints all of them.
    Must catch both live misconfigurations (08 §C1-2)."""
    raise NotImplementedError


def example_config_toml() -> str:
    """Render the ONE shipped example config (spec 06 §2: one example file;
    the live file is the deploy target). Every key present, commented."""
    raise NotImplementedError
