"""The consumer orchestrator: ingestion → emission → checkpointing
(spec 06 §1; invoked by ``organize run-consumers``, spec 10 §1).

Owns ALL checkpointing (single-owner rule, 08 §B12 fix) and all the
orchestration law documented in base.py. Performance gate: full-vault run,
7.5k notes, no LLM work < 30 s (spec 09 §4).

[Architect note: this module is not in doc 06's file list by name — the
orchestrator needed a home inside the fixed ``consumers/`` package and
base.py (shared) must stay framework-only. Decision recorded in
ARCHITECTURE.md.]

Per-note pipeline, in this exact order (each step's rationale is a
regression obligation from 08 §B):

1. **path include/exclude filter** — cheap, re-evaluated every run, NEVER
   persisted (08 §B4: persisted filter misses meant a widened
   ``include_paths`` never applied retroactively, and 98 % of the live
   15 MB DB was ``filtered`` rows).
2. **central ``no-ai`` guard** — a note with ``no-ai: true`` frontmatter is
   never handed to a ``uses_llm`` consumer (spec 02 vault law / 06 §2).
   Enforced HERE, once, so no consumer can forget it. Also not persisted:
   removing ``no-ai`` from a note changes its hash anyway, and the guard
   must re-evaluate if the vault law's reach ever widens.
3. **``should_process``** — the consumer's own cheap predicate over path +
   already-parsed frontmatter. Also never persisted.
4. **hash check** (``store.needs_delivery``) — a note is delivered iff its
   hash differs from that consumer's last TERMINAL emission (06 §1).
5. **``max_notes_per_run``** — counts SUCCESSES only (06 §1); a skip must
   not consume the budget (08 §B13). Notes past the cap yield ``limit``.
6. **``handle``** → checkpoint. ``success``/``skip`` are terminal and are
   checkpointed; ``error``/``limit`` are NOT, so they retry next run
   (08 §B3 — checkpointing ``limit`` silently dropped over-cap notes
   forever). A consumer raising is caught, counted as ``error``, and the
   run CONTINUES (08 §B12/§B2: one bad consumer must never kill the
   pipeline — that is the 3-month outage).

Nothing in this module writes the vault. The only writer of the store is
this module (06 §1 single-owner rule).
"""

from __future__ import annotations

import dataclasses
import fnmatch
import logging
import os
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from organize_core import frontmatter
from organize_core.config import Config, ConsumerConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    Status,
    get_consumer_types,
)
from organize_core.consumers.store import hash_note_text
from organize_core.errors import ConfigError, FrontmatterError, OrganizeError
from organize_core.index import is_ignored
from organize_core.llm import LLMClient, get_client
from organize_core.paths import CorePaths

if TYPE_CHECKING:  # the runner needs the store's CLASS, never its module state
    from organize_core.consumers.store import AutomationStore
    from organize_core.fileops import OperationContext

logger = logging.getLogger(__name__)

#: Legacy daily notes are skipped by the pipeline (06 §1 — documented
#: behaviour this time round; 08 §B18 flagged it as undocumented).
LEGACY_DAILY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\.md")

#: Soft-purge retention window (06 §1: hard-delete only after 30 days, and
#: never when a scan dir was missing/empty — 08 §B5).
#: TODO(seam): promote to config once a global automation section exists;
#: there is no config key for it today, so the spec's stated 30 d is the
#: single source of truth.
DEFAULT_PURGE_RETENTION_DAYS = 30

#: Cap on how many per-note failure messages a ConsumerSummary retains, so
#: a consumer failing on every one of 7 500 notes cannot exhaust memory.
MAX_RECORDED_FAILURES = 20


@dataclass
class ConsumerSummary:
    """Per-consumer counts for the summary line
    ``Consumer X: success=N skip=N limit=N error=N filtered=N`` (06 §4 —
    ``filtered`` IS counted, 08 §B18)."""

    name: str
    success: int = 0
    skip: int = 0
    limit: int = 0
    error: int = 0
    filtered: int = 0
    #: Notes that WOULD have been handled — only populated by ``dry_run``
    #: (a dry run never calls ``handle``, so the four status counters stay
    #: zero and this is the "what would fire" number, 09 §5.6).
    would_process: int = 0
    #: Notes withheld from an LLM consumer by the ``no-ai: true`` vault law
    #: (spec 02 / 06 §2). Its OWN counter, not folded into ``filtered``: an
    #: operator must be able to tell "the vault forbids this" from "not in
    #: my include_paths".
    no_ai: int = 0
    #: Notes that passed every filter but are already checkpointed at this
    #: hash — the steady-state majority. Counted so the line reconciles
    #: against ``notes_scanned`` instead of silently losing them.
    unchanged: int = 0
    duration_seconds: float = 0.0
    #: Human-readable failure lines (06 §7: failures must be visible in the
    #: summary, not only in the log stream). Capped, see MAX_RECORDED_FAILURES.
    failures: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        """Notes this consumer actually reached a status on."""
        return self.success + self.skip + self.limit + self.error

    @property
    def accounted(self) -> int:
        """Every note this consumer saw, in exactly one bucket. Equals the
        run's ``notes_scanned`` — the 06 §4 line is a reconciliation, not a
        sample (a note that passed every filter but was already checkpointed
        used to increment nothing at all)."""
        return (
            self.success
            + self.skip
            + self.limit
            + self.error
            + self.filtered
            + self.no_ai
            + self.unchanged
            + self.would_process
        )

    def line(self) -> str:
        """The 06 §4 summary line for this consumer, extended with the two
        buckets the spec's five counters left unaccounted (``no_ai``,
        ``unchanged``). The five spec-named fields keep their names, order
        and meaning; the additions only appear when non-zero."""
        text = (
            f"Consumer {self.name}: success={self.success} skip={self.skip} "
            f"limit={self.limit} error={self.error} filtered={self.filtered}"
        )
        if self.no_ai:
            text += f" no_ai={self.no_ai}"
        if self.unchanged:
            text += f" unchanged={self.unchanged}"
        if self.would_process:
            text += f" would_process={self.would_process}"
        return text


@dataclass
class RunSummary:
    """Structured per-run summary (spec 06 §6): counts per consumer +
    duration, emitted to stdout/journal. ``exit_code``: 0 clean, 1 any
    consumer error (06 §4)."""

    consumers: list[ConsumerSummary] = field(default_factory=list)
    duration_seconds: float = 0.0
    notes_scanned: int = 0
    exit_code: int = 0
    #: True when the run was a dry run — no store writes, no consumer side
    #: effects (09 §5.6). The summary is marked so a caller can never mistake
    #: a rehearsal for a real run.
    dry_run: bool = False
    #: Rows hard-deleted by the soft purge this run (0 when suppressed).
    purged: int = 0

    @property
    def errors(self) -> int:
        return sum(c.error for c in self.consumers)

    def lines(self) -> list[str]:
        """Every line the journal should carry for this run (06 §7)."""
        prefix = "[DRY-RUN] " if self.dry_run else ""
        out = [
            f"{prefix}run: scanned {self.notes_scanned} notes, "
            f"{len(self.consumers)} consumer(s), {self.duration_seconds:.2f}s"
        ]
        for summary in self.consumers:
            out.append(prefix + summary.line())
            out.extend(f"{prefix}  ! {summary.name}: {message}" for message in summary.failures)
        out.append(
            f"{prefix}run complete: errors={self.errors} purged={self.purged} "
            f"exit={self.exit_code}"
        )
        return out


# ---------------------------------------------------------------------------
# Ingestion (spec 06 §1)
# ---------------------------------------------------------------------------


def _clean_rel(pattern: str) -> str:
    """Normalize a config path pattern to a vault-relative posix string."""
    cleaned = str(pattern).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    if cleaned == ".":
        return ""
    return cleaned.rstrip("/")


def _has_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def _matches(rel: str, patterns: Sequence[str]) -> bool:
    """Does the vault-relative posix path ``rel`` match any CONSUMER path
    pattern (``include_paths``/``exclude_paths``)?

    Spec 06 §2 says these are "prefix (relative to root) or glob": a pattern
    with no glob metacharacter is a PREFIX (the path itself or anything
    under it), otherwise it is an ``fnmatch`` glob over the whole relative
    path.

    This is deliberately NOT ``vault.ignore_patterns``' rule. That one also
    lets a slash-free pattern match any single path COMPONENT (08 §A13, so
    ``.obsidian`` prunes at any depth) and lives, publicly and once, in
    ``index.is_ignored`` — the ingestion walk below calls it rather than
    carrying a second copy. Here a bare ``resources`` must mean the
    top-level folder and nothing else, which is why the two stay separate
    functions instead of one function with a mode flag.
    """
    if not rel:
        return False
    for raw in patterns:
        pattern = _clean_rel(raw)
        if not pattern:
            continue
        if _has_glob(pattern):
            if fnmatch.fnmatchcase(rel, pattern):
                return True
            if fnmatch.fnmatchcase(rel, pattern + "/*"):
                return True
        elif rel == pattern or rel.startswith(pattern + "/"):
            return True
    return False


def _vault_root(config: Config) -> Path:
    """``config.vault.root`` is expanded ONCE, by ``config._expand_path`` →
    ``paths.expand``. Re-expanding here would be a hidden environment read
    (ARCHITECTURE structural decision 4) and would turn a legal vault
    folder starting with ``~`` into a home-directory lookup."""
    return Path(config.vault.root).resolve()


@dataclass(frozen=True)
class _ScanTarget:
    rel: str  # vault-relative posix prefix, "" when the target IS the root
    base: Path  # resolved absolute directory to walk


def _resolve_scan_targets(config: Config) -> tuple[Path, list[_ScanTarget], list[str]]:
    """``(root, existing targets, missing scan_dirs)``.

    Symlinked scan dirs are resolved and their vault-relative prefix is
    taken from the CONFIGURED string, never from ``relative_to(root)``,
    which raises for a scan dir symlinked out of the vault (08 §B18).
    """
    root = _vault_root(config)
    targets: list[_ScanTarget] = []
    missing: list[str] = []
    seen_bases: set[Path] = set()
    for raw in config.vault.scan_dirs:
        rel = _clean_rel(raw)
        candidate = Path(raw)
        base = candidate if candidate.is_absolute() else root / rel
        try:
            resolved = base.resolve()
        except OSError:  # pragma: no cover - resolve() on a broken mount
            missing.append(rel or str(raw))
            continue
        if not resolved.is_dir():
            missing.append(rel or str(raw))
            continue
        if resolved in seen_bases:
            continue
        seen_bases.add(resolved)
        targets.append(_ScanTarget(rel=rel, base=resolved))
    return root, targets, missing


def _scan_dirs_ok(config: Config) -> bool:
    """Every configured scan dir exists AND is non-empty (06 §1 / 08 §B5).

    False suppresses the soft purge: a transient mount or a typo'd config
    must never let the pipeline forget a year of LLM-run checkpoints.
    """
    _root, targets, missing = _resolve_scan_targets(config)
    if missing or not targets:
        return False
    for target in targets:
        try:
            next(iter(os.scandir(target.base)))
        except StopIteration:
            logger.warning(
                "scan dir %s is empty — soft purge suppressed this run (06 §1, 08 §B5)",
                target.base,
            )
            return False
        except OSError as exc:
            logger.warning("scan dir %s is unreadable (%s) — soft purge suppressed", target.base, exc)
            return False
    return True


def _read_payload(path: Path, resolved: Path, rel: str = "") -> NotePayload | None:
    """Parse one note. Returns None (after logging) for anything unreadable
    or unparseable — a per-file failure never aborts the run (06 §1)."""
    try:
        raw_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("skipping %s: unreadable (%s)", path, exc)
        return None
    try:
        doc = frontmatter.parse(raw_text)
    except FrontmatterError as exc:
        logger.warning("skipping %s: frontmatter did not parse (%s)", path, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a parser bug must not kill the run
        logger.warning("skipping %s: unexpected parse failure (%s)", path, exc)
        return None
    fields: dict[str, Any] = dict(doc.frontmatter.fields) if doc.frontmatter else {}
    return NotePayload(
        path=resolved,
        frontmatter=fields,
        content=doc.body,
        raw_text=raw_text,
        # ONE hashing recipe in the codebase (store seam, granted): a second,
        # subtly different digest would orphan 376 migrated success
        # checkpoints and refire every consumer over the whole vault.
        note_hash=hash_note_text(raw_text),
        # The vault-relative path computed by the WALK (configured scan-dir
        # prefix + walk-relative subpath), carried rather than re-derived
        # from `resolved`: a scan dir symlinked outside the vault root has
        # no `relative_to(root)` answer, and the absolute-path fallback
        # matches no relative include_paths pattern (08 §B18).
        rel=rel,
    )


def _iter_payloads(
    targets: Sequence[_ScanTarget],
    patterns: Sequence[str],
    max_file_size: int,
) -> Iterator[NotePayload]:
    seen: set[Path] = set()
    collected: list[tuple[str, Path, Path]] = []  # (rel, path, resolved)
    for target in targets:
        base_str = str(target.base)
        for dirpath, dirnames, filenames in os.walk(base_str, topdown=True, followlinks=False):
            sub = os.path.relpath(dirpath, base_str)
            if sub == os.curdir:
                rel_dir = target.rel
            else:
                sub_posix = Path(sub).as_posix()
                rel_dir = f"{target.rel}/{sub_posix}" if target.rel else sub_posix

            keep: list[str] = []
            for name in dirnames:
                if name.startswith("."):  # .obsidian, .git, .trash …
                    continue
                child_rel = f"{rel_dir}/{name}" if rel_dir else name
                if is_ignored(child_rel, patterns):
                    continue
                keep.append(name)
            dirnames[:] = sorted(keep)

            for name in sorted(filenames):
                if not name.endswith(".md"):
                    continue  # .wav/.txt/.pdf interleaved in raw_capture (02)
                if LEGACY_DAILY_PATTERN.fullmatch(name):
                    continue  # legacy daily notes (06 §1)
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if is_ignored(rel, patterns):
                    continue
                path = Path(dirpath) / name
                try:
                    resolved = path.resolve()
                except OSError as exc:  # pragma: no cover - broken symlink
                    logger.warning("skipping %s: cannot resolve (%s)", path, exc)
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                collected.append((rel, path, resolved))

    for rel, path, resolved in sorted(collected, key=lambda item: item[0]):
        if max_file_size > 0:
            try:
                size = path.stat().st_size
            except OSError as exc:
                logger.warning("skipping %s: cannot stat (%s)", path, exc)
                continue
            if size > max_file_size:
                logger.warning(
                    "skipping %s: %d bytes exceeds vault.max_file_size (%d)",
                    path,
                    size,
                    max_file_size,
                )
                continue
        payload = _read_payload(path, resolved, rel)
        if payload is not None:
            yield payload


def scan_notes(config: Config) -> Iterator[NotePayload]:
    """Ingestion (spec 06 §1): walk ``vault.scan_dirs``, parse ``*.md`` via
    THE shared frontmatter module into NotePayloads. Skips legacy daily
    notes ``\\d{4}-\\d{2}-\\d{2}\\.md`` (documented exclusion); per-file
    parse errors are logged and skipped, never abort the run; explicit
    ``encoding="utf-8", errors="replace"`` everywhere (06 §6).

    The "no scan directory exists" check is EAGER — it raises before the
    generator is created, so it cannot be swallowed by a caller that never
    iterates (08 §B14: the original raise was generator-deferred and its
    message had an operator-precedence bug).
    """
    root, targets, missing = _resolve_scan_targets(config)
    if not targets:
        listed = ", ".join(missing) if missing else "none configured"
        raise ConfigError(
            f"no scan directory exists under {root}: {listed}",
            hint="check [vault] root and [vault] scan_dirs — every configured "
            "scan dir is missing, which usually means root points at the wrong "
            "folder or the vault is not mounted (spec 06 §2).",
        )
    for rel in missing:
        logger.warning(
            "vault.scan_dirs entry %r does not exist under %s — skipping it this run", rel, root
        )
    return _iter_payloads(
        targets,
        tuple(config.vault.ignore_patterns or ()),
        int(config.vault.max_file_size),
    )


# ---------------------------------------------------------------------------
# Orchestration (spec 06 §1)
# ---------------------------------------------------------------------------




def _relative_posix(root: Path, path: Path) -> str:
    """Vault-relative posix path, tolerant of a note reached through a
    symlinked scan dir that resolves outside the root (08 §B18).

    LAST RESORT ONLY. The absolute path this returns on failure can never
    match a relative ``include_paths`` prefix, so a caller that relies on it
    silently drops the note. Payloads produced by ingestion carry the
    walk-derived :attr:`NotePayload.rel`; use :func:`_payload_rel`, which
    prefers that and WARNs (once per run per note) when it has to fall back
    here.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _payload_rel(root: Path, payload: NotePayload, warned: set[Path]) -> str:
    """The vault-relative posix path the consumer path filters run against.

    Prefers the path computed during INGESTION from the configured scan-dir
    prefix (``NotePayload.rel``). Only a hand-built payload lacks one; then
    we re-derive it, and if that yields an absolute path — the 08 §B18
    symlinked-out-scan-dir case — we say so ONCE, because the consequence is
    that no relative ``include_paths`` pattern can match and the note is
    counted ``filtered`` for every consumer rather than handled.
    """
    if payload.rel:
        return payload.rel
    rel = _relative_posix(root, payload.path)
    if rel.startswith("/") and payload.path not in warned:
        warned.add(payload.path)
        logger.warning(
            "%s has no vault-relative path under %s (reached through a symlink "
            "out of the vault?) — consumer include_paths/exclude_paths cannot "
            "match it and it will be counted filtered (08 §B18)",
            payload.path,
            root,
        )
    return rel


class UnknownConsumerError(ConfigError):
    """``--consumer`` named a section that is not configured.

    A distinct type, not a message prefix: spec 06 §4 wants exit **2** for
    this (a usage error) while every other ``OrganizeError`` is exit 1, and
    the CLI must not sniff exception text to tell them apart.
    """


def _select_consumer_configs(config: Config, only: list[str] | None) -> list[ConsumerConfig]:
    """``--consumer`` matches config SECTION names case-insensitively;
    an unknown name is a usage error (06 §4 → exit 2)."""
    if only is None:
        return [c for c in config.consumers if c.enabled]

    known = {c.name.lower(): c for c in config.consumers}
    wanted: list[ConsumerConfig] = []
    unknown: list[str] = []
    for raw in only:
        entry = known.get(str(raw).strip().lower())
        if entry is None:
            unknown.append(str(raw))
            continue
        if entry not in wanted:
            wanted.append(entry)
    if unknown:
        names = ", ".join(sorted(known)) or "(none configured)"
        raise UnknownConsumerError(
            "unknown consumer(s): " + ", ".join(sorted(unknown)),
            hint=f"configured consumer sections: {names}",
        )
    enabled: list[ConsumerConfig] = []
    for entry in wanted:
        if not entry.enabled:
            logger.warning(
                "consumer %s was selected explicitly but is disabled in config "
                "(enabled = false) — not running it",
                entry.name,
            )
            continue
        enabled.append(entry)
    return enabled


class _LazyLLM:
    """The ONE shared LLM client for a run (09 §2), created on first use.

    Constructing it eagerly would put config-shaped failures in front of
    consumers that never need an LLM; failing to construct it degrades to
    ``None`` (consumers already contract to work unenriched, 06 §3.1).
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: LLMClient | None = None
        self._tried = False

    def get(self) -> LLMClient | None:
        if self._tried:
            return self._client
        self._tried = True
        try:
            self._client = get_client(self._config.llm)
        except (OrganizeError, OSError) as exc:
            logger.warning("LLM backend unavailable this run (%s) — consumers degrade", exc)
            self._client = None
        return self._client


def _construct(entry: ConsumerConfig) -> tuple[Consumer | None, str | None]:
    """Instantiate one consumer from validated config. Constructors are PURE
    (06 §1); a constructor that raises is isolated here so the other
    consumers still run (08 §B2 — this is exactly the 3-month outage)."""
    types = get_consumer_types()
    cls = types.get(entry.type)
    if cls is None:
        known = ", ".join(sorted(types)) or "(none registered)"
        return None, f"unknown consumer type {entry.type!r} (registered: {known})"
    try:
        return cls(entry), None
    except Exception as exc:  # noqa: BLE001 - isolation is the whole point
        logger.exception("consumer %s failed to construct", entry.name)
        return None, f"construction failed: {type(exc).__name__}: {exc}"


def _record_failure(summary: ConsumerSummary, message: str) -> None:
    if len(summary.failures) < MAX_RECORDED_FAILURES:
        summary.failures.append(message)
    elif len(summary.failures) == MAX_RECORDED_FAILURES:
        summary.failures.append("… further failures suppressed (see the log)")


def _run_one_consumer(
    entry: ConsumerConfig,
    consumer: Consumer,
    payloads: Sequence[NotePayload],
    *,
    root: Path,
    store: AutomationStore,
    ctx: RunContext,
    dry_run: bool,
    now: int,
) -> ConsumerSummary:
    summary = ConsumerSummary(name=entry.name)
    started = time.monotonic()
    include = tuple(entry.include_paths or ())
    exclude = tuple(entry.exclude_paths or ())
    cap = int(entry.max_notes_per_run)
    # The no-ai DENIAL is keyed on the class flag, not on `wants_llm()`:
    # `uses_llm = True` means "this consumer's whole job is an LLM call", so
    # the note must never be offered at all. taskwarrior has `uses_llm =
    # False` and creates the task for every matching note even with
    # enrichment on (ARCHITECTURE resolution #12); its enrichment branch
    # guards no-ai itself. See Consumer.wants_llm for the other half.
    denies_no_ai = bool(getattr(type(consumer), "uses_llm", False))
    warned_rel: set[Path] = set()

    for payload in payloads:
        rel = _payload_rel(root, payload, warned_rel)

        # 1. path filters — cheap, never persisted (08 §B4)
        if include and not _matches(rel, include):
            summary.filtered += 1
            continue
        if exclude and _matches(rel, exclude):
            summary.filtered += 1
            continue

        # 1b. still there? The payload list is scanned ONCE at the top of the
        # run, so an earlier consumer in the SAME run may already have moved
        # this note — a tag_router route files and archives it, and every
        # later consumer is still holding the old path. Without this check
        # the note reaches `handle`, which burns a real LLM inference and
        # THEN fails "note does not exist": a paid-for error, exit 1, and an
        # OnFailure alert every ten minutes for a pipeline that is working
        # exactly as designed. It is a FILTER, not an error — the note was
        # handled, just not by this consumer, and the next run rescans.
        if not payload.path.exists():
            summary.filtered += 1
            logger.debug(
                "consumer %s: %s no longer exists (moved earlier this run) — skipping",
                entry.name,
                rel,
            )
            continue

        # 2. central no-ai guard (spec 02 vault law / 06 §2)
        if denies_no_ai and payload.no_ai:
            # Its OWN counter, not `filtered`: an operator reading the 06 §4
            # summary must be able to tell "withheld under vault law" from
            # "not in my include_paths", and 06 §6 wants a WARN on every
            # skipped file — a note the vault forbids us to process is the
            # one skip that must never be silent.
            summary.no_ai += 1
            logger.warning(
                "no-ai: %s never offered to LLM consumer %s (vault law, spec 02)",
                rel,
                entry.name,
            )
            continue

        # 3. the consumer's own predicate — also never persisted
        try:
            wanted = consumer.should_process(payload)
        except Exception as exc:  # noqa: BLE001 - a bad predicate is a per-note error
            summary.error += 1
            logger.exception("consumer %s: should_process raised on %s", entry.name, payload.path)
            _record_failure(summary, f"{rel}: should_process raised {type(exc).__name__}: {exc}")
            continue
        if not wanted:
            summary.filtered += 1
            continue

        # 4. hash checkpoint — delivered iff the hash differs from the last
        #    TERMINAL emission (06 §1)
        try:
            if not store.needs_delivery(entry.name, payload.path, payload.note_hash):
                # Already checkpointed at this hash. Counted so the summary
                # reconciles: success+skip+limit+error+filtered+no_ai+
                # unchanged (+would_process on a rehearsal) == notes_scanned.
                summary.unchanged += 1
                continue
        except Exception as exc:  # noqa: BLE001 - a store read failure is fatal for this consumer
            summary.error += 1
            logger.exception("consumer %s: store lookup failed for %s", entry.name, payload.path)
            _record_failure(summary, f"{rel}: store lookup failed: {type(exc).__name__}: {exc}")
            break

        # 5. per-run cap — SUCCESSES only (06 §1); a skip must not consume
        #    the budget (08 §B13). `limit` is NOT checkpointed (08 §B3).
        #
        #    A dry run never increments `success` (it never calls `handle`),
        #    so counting successes alone made the rehearsal ignore the cap
        #    entirely and over-report `would_process` — and 09 §5.6 makes the
        #    dry run the cutover gate you diff against expectations. Counting
        #    would-be work here gives a rehearsal the same success/limit split
        #    the real run produces (live caps: learn 20, deep_research 5).
        if cap > 0 and summary.success + summary.would_process >= cap:
            summary.limit += 1
            logger.debug(
                "consumer %s: %s over max_notes_per_run=%d — retried next run",
                entry.name,
                rel,
                cap,
            )
            continue

        if dry_run:
            summary.would_process += 1
            logger.info("[DRY-RUN] consumer %s would handle %s", entry.name, rel)
            continue

        # 6. handle → checkpoint (runner-only, 06 §1 / 08 §B12)
        try:
            result = consumer.handle(payload, ctx)
        except Exception as exc:  # noqa: BLE001 - B12: never kill the pipeline
            summary.error += 1
            logger.exception("consumer %s raised handling %s", entry.name, payload.path)
            _record_failure(summary, f"{rel}: {type(exc).__name__}: {exc}")
            continue

        if not isinstance(result, ConsumerResult):
            summary.error += 1
            logger.error(
                "consumer %s returned %r for %s, not a ConsumerResult",
                entry.name,
                type(result).__name__,
                payload.path,
            )
            _record_failure(summary, f"{rel}: handle returned {type(result).__name__}")
            continue

        status = result.status
        if status is Status.SUCCESS:
            summary.success += 1
        elif status is Status.SKIP:
            summary.skip += 1
        elif status is Status.LIMIT:
            summary.limit += 1
        else:
            summary.error += 1

        if status in (Status.ERROR, Status.LIMIT):
            # NOT checkpointed: retried next run (06 §1, 08 §B3).
            level = logging.WARNING if status is Status.ERROR else logging.INFO
            logger.log(
                level,
                "consumer %s: %s on %s (%s) — retried next run",
                entry.name,
                status.value,
                rel,
                result.message or "no detail",
            )
            if status is Status.ERROR:
                _record_failure(summary, f"{rel}: {result.message or 'error'}")
            continue

        if dry_run:  # pragma: no cover - unreachable; see the comment
            # Defence in depth, not flow control: the dry-run branch above
            # `continue`s before `handle` is called, so nothing reaches here
            # today. It exists because the failure it prevents is permanent
            # and silent. Three consumers carry their own `ctx.dry_run`
            # guards inside `handle` (a direct caller must not dispatch the
            # research agent or bill an LLM), and two of them return a
            # TERMINAL status — deep_research SKIP, learn SUCCESS. If a
            # future refactor ever lets a rehearsal reach `handle`, those
            # returns would be checkpointed as done and the real dispatch or
            # generation would be suppressed FOREVER, with no error anywhere.
            # A checkpoint is the one thing a rehearsal must never write
            # (09 §5.6), so the runner refuses it here rather than trusting
            # every consumer's dry-run return value.
            logger.error(
                "consumer %s returned %s for %s during a DRY RUN — not checkpointing "
                "(a rehearsal must never mark work as done, 09 §5.6)",
                entry.name,
                status.value,
                rel,
            )
            continue

        try:
            store.checkpoint(
                entry.name,
                payload.path,
                payload.note_hash,
                status.value,
                dict(result.metadata) if result.metadata else None,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - a store write failure is this consumer's error
            summary.error += 1
            logger.exception("consumer %s: checkpoint failed for %s", entry.name, payload.path)
            _record_failure(summary, f"{rel}: checkpoint failed: {type(exc).__name__}: {exc}")

    summary.duration_seconds = time.monotonic() - started
    return summary


def run_consumers(
    config: Config,
    store: AutomationStore,
    *,
    only: list[str] | None = None,
    dry_run: bool = False,
    paths: CorePaths | None = None,
    op_context: OperationContext | None = None,
) -> RunSummary:
    """One full run (spec 06 §1 orchestration rules):

    1. Instantiate ONLY the enabled/selected consumers (``only`` matches
       config section names case-insensitively; unknown ⇒ usage error, exit
       2 — 06 §4). Construction is pure; a consumer failing to construct is
       isolated: logged, counted as error, others still run.
    2. For each payload × consumer: path include/exclude filter, then
       ``should_process`` (both re-evaluated every run, never persisted),
       then the hash check (``store.needs_delivery``), then ``handle``.
    3. Checkpoint ONLY terminal results (success/skip). error/limit retried
       next run. ``max_notes_per_run`` counts successes only; notes past
       the cap yield LIMIT (not checkpointed).
    4. ``no-ai: true`` payloads never reach a ``uses_llm`` consumer (02).
    5. mark_seen + soft_purge (store), per its guard.
    6. ``dry_run`` (09 §5.6): full evaluation, no ``handle`` side effects,
       no checkpoints; summary says what would fire.

    The store is opened (and migrated) by the CALLER — this function never
    migrates, so a run can never race a half-migrated schema (06 §1).
    Unknown ``only`` names raise :class:`~organize_core.errors.ConfigError`;
    the CLI maps that to exit 2.

    ``paths`` is the resolved :class:`~organize_core.paths.CorePaths`; it is
    handed to every consumer through ``RunContext.paths`` so a consumer
    needing a STATE location derives it instead of reading the environment
    (structural decision 4).

    ``op_context`` is the composition root's ONE recorded write path (spec
    12 §2). Each consumer receives its own view via
    ``dataclasses.replace(actor="consumer:<type>")`` — the doc 12 §2 actor
    format, never a bare name — so a record names the consumer that wrote
    it while there is still exactly one construction site. ``dry_run`` is
    RE-WIRED onto that copy rather than trusted to agree: one flag, set
    once, so a rehearsal can never be handed a context that writes.

    ``Consumer.bind(ctx)`` runs once per consumer before any
    ``should_process``. A bind that RAISES skips that consumer for the run,
    counts one error and makes the run exit 1 — never "continue unbound",
    since an unbound filter silently drops every note.
    """
    started = time.monotonic()
    now = int(time.time())
    summary = RunSummary(dry_run=dry_run)

    selected = _select_consumer_configs(config, only)

    payloads: list[NotePayload] = list(scan_notes(config))
    summary.notes_scanned = len(payloads)
    root = _vault_root(config)
    logger.info("scanned %d notes from %s", len(payloads), root)

    if not dry_run:
        try:
            # Hashes too: this is the only writer of the `notes` table on
            # the run path, and without them every row carried
            # `note_hash = ''` forever.
            store.mark_seen(
                [p.path for p in payloads],
                now=now,
                hashes={p.path: p.note_hash for p in payloads},
            )
        except Exception:  # noqa: BLE001 - bookkeeping must not abort the run
            logger.exception("mark_seen failed — continuing (soft purge will be conservative)")

    llm = _LazyLLM(config)

    for entry in selected:
        consumer, problem = _construct(entry)
        if consumer is None:
            failed = ConsumerSummary(name=entry.name, error=1)
            _record_failure(failed, problem or "construction failed")
            summary.consumers.append(failed)
            continue
        # ONE shared client per run (09 §2: exactly one LLM path), handed
        # only to consumers that declare they want it. A fresh RunContext per
        # consumer so no consumer can observe another's services.
        #
        # `wants_llm()` is an INSTANCE predicate, not the `uses_llm` class
        # flag: taskwarrior's enrichment is switched on by an OPTION
        # (`llm_enabled`) while its class flag stays False so that no-ai
        # notes still become tasks. Keying injection on the class flag made
        # `llm_enabled = true` inert in production — `ctx.llm` was always
        # None and the 06 §3.1 enrichment path could never run.
        try:
            wants_llm = bool(consumer.wants_llm())
        except Exception:  # noqa: BLE001 - a bad predicate must not kill the run
            logger.exception(
                "consumer %s: wants_llm() raised — falling back to the class flag",
                entry.name,
            )
            wants_llm = bool(getattr(type(consumer), "uses_llm", False))
        ctx = RunContext(
            config=config,
            dry_run=dry_run,
            llm=llm.get() if wants_llm else None,
            # State locations come from the composition root, never from a
            # consumer's own environment read (taskwarrior's
            # <state>/backups/taskwarrior/<UTC-ts>, 06 §3.1).
            paths=paths,
            # Per-consumer view of the ONE recorded write path. `dry_run` is
            # re-wired here rather than trusted to match: one flag, set
            # once, so a rehearsal cannot be handed a writing context.
            op_context=(
                None
                if op_context is None
                else dataclasses.replace(
                    op_context, actor=f"consumer:{entry.type}", dry_run=dry_run
                )
            ),
        )
        # BEFORE any should_process: the predicate's config view is what
        # bind() exists to grant. A raise skips this consumer (error + exit
        # 1) instead of running it unbound, because an unbound filter drops
        # every note silently — the outage class this hook prevents.
        try:
            consumer.bind(ctx)
        except Exception as exc:  # noqa: BLE001 - isolated, like construction
            logger.exception("consumer %s: bind() raised — skipping it this run", entry.name)
            failed = ConsumerSummary(name=entry.name, error=1)
            _record_failure(failed, f"bind failed: {exc}")
            summary.consumers.append(failed)
            continue
        summary.consumers.append(
            _run_one_consumer(
                entry,
                consumer,
                payloads,
                root=root,
                store=store,
                ctx=ctx,
                dry_run=dry_run,
                now=now,
            )
        )

    if not dry_run:
        try:
            # `scan_roots` bounds the purge to what we actually WALKED. A
            # note under a scan dir the operator removed from config is not
            # missing, it is unlooked-at — purging it would destroy its
            # emission history and re-run every LLM emission when the dir is
            # added back (06 §7 acceptance / 08 §B5).
            _root, purge_targets, _missing = _resolve_scan_targets(config)
            summary.purged = int(
                store.soft_purge(
                    retention_days=DEFAULT_PURGE_RETENTION_DAYS,
                    scan_dirs_ok=_scan_dirs_ok(config),
                    now=now,
                    scan_roots=[t.base for t in purge_targets],
                )
                or 0
            )
        except Exception:  # noqa: BLE001 - never let housekeeping fail a good run
            logger.exception("soft purge failed — no rows removed")
            summary.purged = 0
        try:
            # Bound the restore archive. Nothing else deletes from `purged_*`,
            # so without this the 15 MB bloat 08 §B4 removed from `emissions`
            # would just move house.
            store.sweep_purged(now=now)
        except Exception:  # noqa: BLE001 - housekeeping, never fatal
            logger.exception("purge-archive sweep failed — the archive keeps growing")

    summary.duration_seconds = time.monotonic() - started
    summary.exit_code = 1 if summary.errors else 0
    for line in summary.lines():
        logger.info("%s", line)
    return summary
