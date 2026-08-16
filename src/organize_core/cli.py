"""The ``organize`` CLI (spec 10 §1): every capability scriptable.

Exit codes (spec 06 §4): 0 clean, 1 any error, 2 usage.

Global flags:
    --config PATH       config.toml override (else CorePaths.config_file)
    --config-dir/--state-dir/--runtime-dir
                        CorePaths overrides (spec 10 §3; tests always set
                        these — no command may touch real state without them)
    --dry-run           spec 09 §5.6: full evaluation, zero writes, print
                        what WOULD happen. Honored by every mutating
                        subcommand via OperationContext.dry_run and
                        run_consumers(dry_run=...).
    --log-level LEVEL
    --debug             full traceback for every failure (spec 09 §1.5: an
                        EXPECTED failure is a taxonomy name + message + hint
                        on stderr, never a traceback; --debug opts back in)

The parser tree below is fully built (structural contract — ``--help`` for
every subcommand is the scaffold gate).

This module is a COMPOSITION ROOT: it owns process-level wiring (paths →
config → index → OperationContext) and no domain behavior. Anything that
looks like a rule (scoring, merging, tag order, archive layout) lives in the
module that owns it; the CLI only calls it and renders the result.

Two wiring facts worth stating once:

- **Learning is persisted here.** ``learn.record_move`` is pure by design
  (ARCHITECTURE resolution #5) and ``fileops`` never touches learning.json —
  so the caller persists. Every successful, non-dry-run ``move`` AND
  ``merge`` is recorded (spec 03 §6's outcome table; 04 §3 "on every
  successful accept/move/merge"), keyed by the SAME destination string that
  ``suggest`` scores against (``VaultIndex.para_subfolders`` output) — the
  destination folder for a move, the TARGET'S FOLDER for a merge — or the
  association would never read back. ``maybe_apply_decay`` runs on the same
  path, once a day.
- **Dry-run is not silent.** ``OperationContext.dry_run`` still writes an
  operations.log ``[DRY-RUN]`` line and an ActionRecord flagged
  ``context.filters.dry_run`` (fileops' decision); the vault is untouched
  and the index snapshot and learning.json are not written.

Note: ``auto-organize`` exists from day one and reports "not implemented"
until Phase 6 (spec 13 §3 cross-check list); ``run-consumers`` likewise
until the pipeline lands (spec 06, Phase 3) — ``--list-consumers`` already
works because it constructs nothing (06 §4 / 08 §B2).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
import traceback
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from organize_core import __version__
from organize_core import learn as learn_mod
from organize_core import routes as routes_mod
from organize_core.actions import (
    ActionRecord,
    ActionRecorder,
    ActionSchemaError,
    SuggestionShown,
    new_action_id,
)
from organize_core.config import (
    Config,
    HealthIssue,
    MetadataFieldConfig,
    check_vault,
    coerce_metadata_value,
    example_config_toml,
    load_config,
    metadata_fields_by_key,
)
from organize_core.consumers import get_consumer_types
from organize_core.errors import (
    ConfigError,
    OperationError,
    OrganizeError,
    SessionError,
    VaultError,
)
from organize_core.fileops import (
    OperationContext,
    OperationLog,
    OperationResult,
    archive_capture,
    find_orphaned_temp_files,
    merge_into_note,
    move_to_destination,
    skip_capture,
    update_frontmatter,
)
from organize_core.frontmatter import load_file
from organize_core.index import NoteRecord, QueryCriteria, VaultIndex
from organize_core.paths import CorePaths, expand
from organize_core.session import start_session
from organize_core.suggest import CaptureFeaturesView, Suggestion, generate_candidates
from organize_core.suggest import suggest as rank_suggestions

logger = logging.getLogger(__name__)

# Subcommand names, single source of truth (test_scaffold iterates this).
SUBCOMMANDS: tuple[str, ...] = (
    "index",
    "search",
    "suggest",
    "move",
    "merge",
    "archive",
    "skip",
    "set-meta",
    "meta-fields",
    "session",
    "routes",
    "record",
    "actions",
    "auto-organize",
    "run-consumers",
    "health",
    "serve",
)

_FILTER_TOKEN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
# Frontmatter keys are not identifiers (`no-ai`, `created_date`, Obsidian
# properties with spaces) — set-meta takes anything up to the first '='.
_CHANGE_TOKEN = re.compile(r"^([^=]+)=(.*)$", re.DOTALL)


def _add_decision_context_flags(parser: argparse.ArgumentParser) -> None:
    """Spec 12 §2's ``context`` block, for a scripted/agent caller.

    "The counterfactual is stored, not just the choice" — an agent that ranks
    destinations and then files one knows which rank it took, and without a
    way to say so every record it produced stored a null ``chosen_rank``.
    The RPC door takes the same three as op params.
    """
    parser.add_argument(
        "--chosen-rank",
        type=int,
        metavar="N",
        help="which suggestion this action took; 1 means the engine was right (spec 12 §2)",
    )
    parser.add_argument(
        "--suggestions-json",
        metavar="JSON",
        help='the ranked list that was shown, as `organize suggest --json` emits it '
        "(a JSON array of {path, score, rank, reasons} or that command's whole payload)",
    )
    parser.add_argument(
        "--durations-json",
        metavar="JSON",
        help='e.g. \'{"decision": 8400, "operation": 120}\' — milliseconds (spec 12 §2)',
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="organize",
        description="organize-core: the KMS organize stage (index, suggest, safe file ops, automation).",
    )
    parser.add_argument("--version", action="version", version=f"organize-core {__version__}")
    parser.add_argument("--config", metavar="PATH", help="path to config.toml")
    parser.add_argument("--config-dir", metavar="DIR", help="override config directory")
    parser.add_argument("--state-dir", metavar="DIR", help="override state directory")
    parser.add_argument("--runtime-dir", metavar="DIR", help="override runtime (socket) directory")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate and report what would happen; write nothing (spec 09 §5.6)",
    )
    parser.add_argument("--log-level", default=None, metavar="LEVEL", help="DEBUG|INFO|WARNING|ERROR")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print a full traceback instead of the one-line error summary",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("index", help="scan the vault and update the index (spec 03 §7)")
    p.add_argument("--full", action="store_true", help="rebuild from zero (reindex)")
    p.add_argument("--stats", action="store_true", help="print index statistics only")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("search", help="query the index (spec 03 §2 filters + free text)")
    p.add_argument("query", nargs="*", help="free text and/or k=v filters (tags=a,b sources=... since=YYYY-MM-DD)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("suggest", help="ranked destinations for a note (spec 04)")
    p.add_argument("note", nargs="?", help="path to the capture note")
    p.add_argument("--text", help="score arbitrary text instead of a note (spec 13 §3)")
    p.add_argument("--tags", help="comma list of tags to assume with --text")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("move", help="move a capture to a destination folder (spec 05 §2)")
    p.add_argument("note")
    p.add_argument("destination", help="folder path, vault-relative or absolute")
    _add_decision_context_flags(p)

    p = sub.add_parser("merge", help="merge a capture into an existing note (spec 05 §4)")
    p.add_argument("note")
    p.add_argument("target", help="target note path")
    _add_decision_context_flags(p)

    p = sub.add_parser("archive", help="archive a capture now (spec 05 §3)")
    p.add_argument("note")

    p = sub.add_parser(
        "skip", help="record that a capture was skipped for a session (spec 03 §2/§6)"
    )
    p.add_argument("note")
    p.add_argument(
        "--session",
        metavar="ID",
        help="the session this skip belongs to, as `organize session list --json` reports it",
    )
    _add_decision_context_flags(p)

    p = sub.add_parser("set-meta", help="set frontmatter fields (spec 05 §5, 07)")
    p.add_argument("note")
    p.add_argument("changes", nargs="+", metavar="KEY=VALUE")

    p = sub.add_parser(
        "meta-fields",
        help="the configured metadata fields + their completion values (spec 07)",
    )
    p.add_argument(
        "--key",
        metavar="KEY",
        help="print only the distinct existing values of one frontmatter key",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("session", help="organizing sessions (spec 03 §2)")
    ssub = p.add_subparsers(dest="session_command", metavar="ACTION")
    sp = ssub.add_parser("list", help="list captures matching filters, session order")
    sp.add_argument("filters", nargs="*", metavar="K=V")
    sp.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("routes", help="tag routing + NL descriptions (spec 11)")
    rsub = p.add_subparsers(dest="routes_command", metavar="ACTION")
    rlp = rsub.add_parser("list", help="list configured routes")
    rlp.add_argument("--json", action="store_true", help="machine-readable output")
    rp = rsub.add_parser("resolve", help="routes matching a note's tags")
    rp.add_argument("note")
    rp.add_argument("--json", action="store_true", help="machine-readable output")
    rp = rsub.add_parser("describe", help="show (or, from Phase 4, set) a folder/file NL description (spec 11 §3)")
    rp.add_argument("path")
    rp.add_argument("text", nargs="?", help="new description (writing arrives in Phase 4)")

    p = sub.add_parser("record", help="append an ActionRecord from JSON on stdin (spec 12 §2)")
    p.add_argument("--actor", default=None, help="override the record's actor field")

    p = sub.add_parser("actions", help="the action corpus (spec 12 §2)")
    asub = p.add_subparsers(dest="actions_command", metavar="ACTION")
    ap = asub.add_parser("export", help="concatenate/filter records to stdout or a file")
    ap.add_argument("--out", metavar="PATH")
    ap.add_argument("--operation")
    ap.add_argument("--actor")
    ap.add_argument("--since", metavar="YYYY-MM-DD")
    ap.add_argument("--until", metavar="YYYY-MM-DD")
    ap.add_argument(
        "--include-dry-run",
        action="store_true",
        help="also emit records for actions that were only simulated (--dry-run); "
        "excluded by default so the doc-13 precedent corpus never learns from "
        "actions that never happened",
    )
    sp = asub.add_parser("stats", help="accept-rates, per-route volumes (spec 12 §2)")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.add_argument(
        "--include-dry-run",
        action="store_true",
        help="count simulated (--dry-run) actions too; excluded by default",
    )

    p = sub.add_parser("auto-organize", help="route text/notes automatically (spec 13; Phase 6)")
    p.add_argument("file", nargs="?", help="note path, or - for stdin")
    p.add_argument("--digest", action="store_true", help="daily digest of auto-applied actions")

    p = sub.add_parser("run-consumers", help="run the automation pipeline (spec 06)")
    p.add_argument("--consumer", action="append", default=None, metavar="NAME",
                   help="run only this consumer (repeatable; case-insensitive)")
    p.add_argument("--list-consumers", action="store_true",
                   help="list registered consumer types (constructs nothing)")

    p = sub.add_parser("health", help="config + vault + core diagnostics (spec 03 §2, 10)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--strict",
        action="store_true",
        help="promote warnings to failures (exit 1 on any finding); by default only "
        "ERROR findings fail, so a first-run 'no index snapshot yet' does not break "
        "a setup gate",
    )
    p.add_argument(
        "--example-config",
        action="store_true",
        help="print the complete commented example config.toml and exit (config.py points its "
        "missing-config hint here — spec 06 §2 ships ONE example)",
    )

    p = sub.add_parser("serve", help="JSON-RPC server on a unix socket (spec 10 §1-2)")
    p.add_argument("--socket", metavar="PATH", help="socket path override")
    p.add_argument("--idle-timeout", type=float, default=None, metavar="SECONDS")

    return parser


# --- wiring helpers --------------------------------------------------------


def _emit(line: str = "") -> None:
    print(line)


def _warn(line: str) -> None:
    print(line, file=sys.stderr)


def _json_out(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _configure_logging(args: argparse.Namespace, config: Config | None = None) -> None:
    """Level precedence: --log-level > config [logging] level > WARNING.

    The CLI stays quiet by default so stdout is parseable; the loud-failure
    law (09 §1.5) is served by the error path, not by chatty INFO logs.
    """
    name = (args.log_level or (config.logging.level if config is not None else "WARNING")).upper()
    level = logging.getLevelName(name)
    if not isinstance(level, int):
        raise ConfigError(
            f"unknown log level {name!r}",
            hint="use one of DEBUG, INFO, WARNING, ERROR, CRITICAL",
        )
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger().setLevel(level)


def _resolve_paths(args: argparse.Namespace) -> CorePaths:
    """Explicit flag beats ORGANIZE_CORE_* env beats XDG (paths.py contract)."""
    return CorePaths.resolve(
        config_dir=args.config_dir,
        state_dir=args.state_dir,
        runtime_dir=args.runtime_dir,
    )


def _load(args: argparse.Namespace) -> tuple[CorePaths, Config]:
    paths = _resolve_paths(args)
    config_file = expand(args.config) if args.config else None
    config = load_config(paths, config_file=config_file)
    _configure_logging(args, config)
    paths.ensure_state_dirs()
    return paths, config


def _open_index(paths: CorePaths, config: Config) -> VaultIndex:
    index = VaultIndex(config, paths.index_path)
    index.load()
    return index


def _op_context(
    args: argparse.Namespace,
    paths: CorePaths,
    config: Config,
    index: VaultIndex,
    *,
    actor: str = "matt",
    session_id: str | None = None,
) -> OperationContext:
    return OperationContext(
        config=config,
        index=index,
        oplog=OperationLog(paths.operations_log),
        recorder=ActionRecorder(paths.actions_dir),
        backup_dir=config.vault.root / config.file_ops.backup_dir,
        dry_run=bool(args.dry_run),
        actor=actor,
        session_id=session_id,
        **_decision_context(args),
        # spec 12 §2 `targets[].description`. fileops cannot import routes
        # (routes already imports fileops), so the composition root supplies
        # the lookup; without it every record stored `description: null`
        # even for folders that demonstrably have one.
        describe=lambda folder: routes_mod.get_description(folder, index, config),
        on_record=lambda record: _learn_from_action(paths, config, record),
    )


def _decision_context(args: argparse.Namespace) -> dict[str, Any]:
    """Parse the spec 12 §2 decision-context flags into OperationContext kwargs."""
    out: dict[str, Any] = {}
    rank = getattr(args, "chosen_rank", None)
    if rank is not None:
        out["chosen_rank"] = int(rank)

    raw_suggestions = getattr(args, "suggestions_json", None)
    if raw_suggestions:
        payload = _decision_json(raw_suggestions, "--suggestions-json")
        # Accept `organize suggest --json` verbatim, or just its list.
        items = payload.get("suggestions") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ConfigError(
                "--suggestions-json must be a JSON array of suggestions",
                hint="pass `organize suggest <note> --json` output, or just its "
                '"suggestions" array (spec 12 §2)',
            )
        shown: list[SuggestionShown] = []
        for position, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ConfigError(f"--suggestions-json entry {position} is not an object")
            shown.append(
                SuggestionShown(
                    path=str(item.get("path") or item.get("relative_path") or ""),
                    score=float(item.get("score") or 0.0),
                    rank=int(item.get("rank") or position),
                    reasons=tuple(str(r) for r in (item.get("reasons") or ())),
                )
            )
        out["suggestions_shown"] = tuple(shown)

    raw_durations = getattr(args, "durations_json", None)
    if raw_durations:
        payload = _decision_json(raw_durations, "--durations-json")
        if not isinstance(payload, dict):
            raise ConfigError("--durations-json must be a JSON object of phase -> milliseconds")
        out["durations_ms"] = {str(k): int(v) for k, v in payload.items()}
    return out


def _decision_json(raw: str, flag: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"{flag} is not valid JSON: {exc}", hint="see spec 12 §2") from exc


def _vault_path(config: Config, raw: str, *, what: str = "path") -> Path:
    """Vault-relative or absolute (spec 03 §2 ``move <path>``); ``~``/``$VAR``
    go through paths.expand so this module never reads the environment.

    Resolution order is deliberate (08 §A33 — the original's path handling
    worked by coincidence). ``$`` and ``~`` are legal characters in a vault
    filename (spec 02 quirks: ``plain note $ with no frontmatter.md``), so a
    literal vault-relative hit ALWAYS wins over env expansion; expansion is
    the fallback, not the first guess. Without this order, every capture
    whose name contains ``$`` resolves against the caller's cwd and reports
    "note not found" for a file that is sitting in the vault. That rule now
    covers ``~`` as well — it used to short-circuit to ``expanduser`` before
    the literal probe, so a note named ``~inbox.md`` was addressable through
    the RPC door and unreachable from the CLI.

    The result is CONTAINED: a path that resolves outside the vault root is
    a ``VaultError`` here, matching ``server._vault_path``. Without it
    ``organize move <note> ../OUTSIDE`` wrote note content outside the vault
    and exited 0 while the RPC door refused the identical destination
    (ARCHITECTURE ruling #19 — the two doors must agree). ``fileops``
    re-checks as the backstop; this layer exists to name the mistake in the
    user's own words.
    """
    text = str(raw)
    root = Path(config.vault.root).resolve()

    def contained(candidate: Path) -> Path:
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise VaultError(
                f"{resolved} is outside the vault {root}",
                hint=f"the {what} must be inside the vault; pass a vault-relative path "
                "or an absolute path inside the vault root",
            )
        return resolved

    literal = Path(text)
    if literal.is_absolute():
        return contained(literal)

    in_vault = (root / literal).resolve()
    if in_vault.exists():
        return contained(in_vault)
    if text.startswith("~"):
        expanded = expand(text)
        if expanded.exists():
            return contained(expanded)
    if "$" in text:
        # Vault-relative WITH variables first (``$PROJECT/notes.md``), then
        # the bare expansion (``$HOME/inbox/x.md`` expands to an absolute
        # path outside the vault). Both go through paths.expand, so this
        # module still never reads the environment itself.
        in_vault_expanded = expand(root / literal)
        if in_vault_expanded.exists():
            return contained(in_vault_expanded)
        expanded = expand(text)
        if expanded.exists():
            return contained(expanded)
    # Nothing exists yet (a new destination folder, or a genuine typo): the
    # vault-relative reading is the documented meaning, and naming it in the
    # error is what makes the failure actionable.
    return contained(in_vault)


def _rel(path: Path | str, config: Config) -> str:
    try:
        return str(Path(path).relative_to(config.vault.root))
    except ValueError:
        return str(path)


def _record_for(index: VaultIndex, config: Config, raw: str) -> NoteRecord:
    """The note argument every operation takes, resolved to the index record
    the fileops API requires (05 §2 / 08 §A14 — never a bare string).

    ``_vault_path`` has already refused anything outside the vault with the
    "outside the vault <root>" error, so the "inside the vault but not
    indexable" branch below can only be reached by a path that really is
    inside it. It used to catch both, because ``index.update_file`` returns
    ``None`` for "outside the root" AND for "ignored/too large" — so an
    out-of-vault note was reported as an ignore-pattern problem and the hint
    sent the user to two config keys that had nothing to do with it
    (spec 09 §1.5: an error must be actionable).
    """
    path = _vault_path(config, raw, what="note")
    if not path.is_file():
        raise VaultError(
            f"note not found: {path}",
            hint=f"pass a path relative to the vault root ({config.vault.root}) or an absolute path",
        )
    record = index.get(path)
    if record is None:
        record = index.update_file(path)
    if record is None:
        raise VaultError(
            f"{path} is inside the vault but not indexable",
            hint="check [vault] ignore_patterns / max_file_size, or run `organize index --full`",
        )
    return record


def _split_filter_args(tokens: Sequence[str]) -> tuple[dict[str, str], str]:
    """Split argv into ``k=v`` filters and free text (spec 03 §2)."""
    filters: dict[str, str] = {}
    words: list[str] = []
    for token in tokens or ():
        match = _FILTER_TOKEN.match(token)
        if match:
            filters[match.group(1)] = match.group(2)
        else:
            words.append(token)
    return filters, " ".join(words).strip()


def _split_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _candidate_folders(index: VaultIndex, config: Config) -> dict[str, list[str]]:
    """``{para_type: [folder path]}`` for suggest.generate_candidates. The
    strings here ARE the learning keys — see the module docstring."""
    return {key: [str(p) for p in index.para_subfolders(key)] for key in config.vault.para_folders}


def _archive_folder(config: Config) -> Path:
    archives = config.vault.para_folders.get("archives", "archive")
    return (config.vault.root / archives / config.vault.archive_capture_path).resolve()


def _suggestion_json(suggestion: Suggestion, config: Config, rank: int | None = None) -> dict[str, Any]:
    """One `--json` suggestion entry.

    ``rank`` is emitted because `organize move --suggestions-json` documents
    its input as ``{path, score, rank, reasons}``: a client that builds that
    shape by hand from this payload had no ``rank`` to copy, even though it
    is exactly the 1-based list position the human output already prints.
    """
    payload = {
        "path": suggestion.path,
        "relative_path": _rel(suggestion.path, config) if suggestion.path else "",
        "name": suggestion.name,
        "type": suggestion.type,
        "score": round(suggestion.score, 4),
        "reasons": list(suggestion.reasons),
        "route": suggestion.route,
        "description": suggestion.description,
    }
    if rank is not None:
        payload["rank"] = rank
    return payload


def _print_result(result: OperationResult, config: Config) -> None:
    """Uniform rendering; a failed op is raised as OperationError so the one
    error path in :func:`main` formats it (loud failure, 09 §1.5)."""
    if not result.ok:
        raise OperationError(
            f"{result.operation} failed: {result.error}",
            hint=f"the source note is still at {_rel(result.source, config)} — nothing was lost (spec 05 §1.2)",
        )
    suffix = " (dry-run)" if result.dry_run else ""
    destination = _rel(result.destination, config) if result.destination else ""
    _emit(f"{result.operation}{suffix}: {_rel(result.source, config)} -> {destination}")
    archive_path = result.details.get("archive_path")
    if archive_path and result.operation != "archive":
        _emit(f"  archived: {_rel(archive_path, config)}")
    if result.backup_path:
        _emit(f"  backup: {_rel(result.backup_path, config)}")
    tag_added = result.details.get("tag_added")
    if tag_added:
        _emit(f"  tag added: {tag_added}")
    noop = result.details.get("noop")
    if noop:
        # A successful op that deliberately changed nothing still says so —
        # silence here reads as "filed" (09 §1.5 loud failure's twin).
        _emit(f"  {noop} — nothing was written")
    if result.dry_run:
        _emit("  nothing was written")


def _learn_from_action(paths: CorePaths, config: Config, record: ActionRecord) -> None:
    """Fold one WRITTEN ActionRecord into learning.json (spec 12 §2 "Uses" #2).

    Wired to ``OperationContext.on_record``, so it fires exactly once per
    persisted record and never in parallel with the recorder: learning.json
    is a view derived from the action log, not a second independent store
    that can disagree with it. It fires for a `move` AND for a `merge`
    (spec 03 §6's outcome table; 04 §3 "on every successful accept/move/
    merge") — the CLI used to record only moves while the RPC door recorded
    both, so the two clients silently disagreed about the same user action.

    Decay runs here too, before recording, through
    :func:`learn.maybe_apply_decay`: ``apply_decay`` was implemented,
    unit-tested and then never called from any production path, so the
    90-day eviction and the ``max_history`` cap (04 §3 step 6, §5) never ran
    (08 §A23 traded "decays too often" for "never decays").
    """
    now = time.time()
    data = learn_mod.load_learning(paths.learning_path)
    data = learn_mod.maybe_apply_decay(data, config.suggestions.learning, now=now)
    updated = learn_mod.record_action(data, record, now=now)
    if updated is None:
        return
    learn_mod.save_learning(paths.learning_path, updated)


# --- handlers (spec 10 §1) -------------------------------------------------


def cmd_index(args: argparse.Namespace) -> int:
    paths, config = _load(args)
    index = _open_index(paths, config)

    if args.stats:
        # `--full --stats` used to print the stats of the UNTOUCHED index and
        # silently skip the reindex: on fresh state that is `total: 0` in
        # 0.09s where `--full` alone indexes 13,362 notes — a silently wrong
        # answer, which 09 §1.5 forbids. The two flags now compose: reindex
        # first (honoring --dry-run), then report.
        if args.full:
            if args.dry_run:
                total = index.scan()
                line = (
                    f"index (dry-run): would reindex {total} notes; "
                    f"{paths.index_path} not written"
                )
            else:
                result = index.full_reindex()
                line = (
                    f"reindexed {result['total']} notes in {result['duration']:.2f}s "
                    f"-> {paths.index_path}"
                )
            # `--json` must stay parseable: the progress line would prefix the
            # payload with non-JSON.
            if not args.json:
                _emit(line)
        stats = index.stats()
        if args.json:
            _json_out(stats)
        else:
            for key, value in stats.items():
                _emit(f"{key}: {json.dumps(value, sort_keys=True) if isinstance(value, dict) else value}")
        return 0

    if args.full:
        if args.dry_run:
            total = index.scan()
            _emit(f"index (dry-run): would reindex {total} notes; {paths.index_path} not written")
            return 0
        result = index.full_reindex()
        _emit(f"reindexed {result['total']} notes in {result['duration']:.2f}s -> {paths.index_path}")
        return 0

    touched = index.scan()
    if args.dry_run:
        _emit(f"index (dry-run): scanned {touched} notes; {paths.index_path} not written")
        return 0
    index.flush()
    _emit(f"indexed {touched} notes (total {index.stats()['total']}) -> {paths.index_path}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    paths, config = _load(args)
    index = _open_index(paths, config)
    filters, text = _split_filter_args(args.query)
    criteria = QueryCriteria.from_filter_args(filters)
    if text:
        criteria.text = text
    records = index.query(criteria)
    if args.json:
        _json_out(
            [
                {
                    "path": record.path,
                    "relative_path": _rel(record.path, config),
                    "title": record.title,
                    "para_type": record.para_type,
                    "tags": list(record.tags),
                    "processing_status": record.processing_status,
                    "timestamp": record.timestamp,
                }
                for record in records
            ]
        )
        return 0
    for record in records:
        _emit(f"{_rel(record.path, config)}\t{record.title}\t{','.join(record.tags)}")
    _emit(f"{len(records)} note(s)")
    return 0


def cmd_suggest(args: argparse.Namespace) -> int:
    if not args.note and args.text is None:
        _warn("suggest: give a note path or --text (spec 13 §3)")
        return 2
    if args.note and args.text is not None:
        _warn("suggest: --text scores arbitrary text; do not also pass a note path")
        return 2

    paths, config = _load(args)
    index = _open_index(paths, config)
    learning = learn_mod.load_learning(paths.learning_path)

    if args.text is not None:
        tags = _split_list(args.tags)
        capture = CaptureFeaturesView.from_text(args.text, tags=tags or None)
        note_tags = tags
        subject = "--text"
    else:
        record = _record_for(index, config, args.note)
        capture = CaptureFeaturesView.from_record(record)
        note_tags = list(record.tags)
        subject = _rel(record.path, config)
        if args.tags:
            _warn("suggest: --tags is only used with --text; ignoring it for an indexed note")

    candidates = generate_candidates(_candidate_folders(index, config))
    ranked = rank_suggestions(
        capture,
        candidates,
        config.suggestions,
        learning,
        now=time.time(),
        archive_path=str(_archive_folder(config)),
    )
    ranked = routes_mod.merge_route_suggestions(routes_mod.resolve(note_tags, config), ranked)
    ranked = [_with_description(item, index, config) for item in ranked]

    if args.json:
        _json_out(
            {
                "subject": subject,
                "suggestions": [
                    _suggestion_json(s, config, rank)
                    for rank, s in enumerate(ranked, start=1)
                ],
            }
        )
        return 0
    if not ranked:
        _emit(f"no suggestions for {subject}")
        return 0
    for rank, item in enumerate(ranked, start=1):
        _emit(
            f"{rank}\t{item.score:.2f}\t{item.type}\t{_rel(item.path, config)}\t"
            f"{item.name}\t{'; '.join(item.reasons)}"
        )
    return 0


def _with_description(suggestion: Suggestion, index: VaultIndex, config: Config) -> Suggestion:
    """Attach the destination's NL description (spec 11 §3) when the ranking
    layer did not (routes already carry theirs)."""
    if suggestion.description or not suggestion.path:
        return suggestion
    try:
        description = routes_mod.get_description(Path(suggestion.path), index, config)
    except OrganizeError:  # a bad description must never cost a suggestion
        logger.warning("suggest: could not read a description for %s", suggestion.path)
        return suggestion
    return replace(suggestion, description=description) if description else suggestion


def cmd_move(args: argparse.Namespace) -> int:
    paths, config = _load(args)
    index = _open_index(paths, config)
    record = _record_for(index, config, args.note)
    destination = _vault_path(config, args.destination)
    if destination.exists() and not destination.is_dir():
        raise VaultError(
            f"destination is a file, not a folder: {destination}",
            hint="use `organize merge <note> <target>` to put a capture INTO a note (spec 05 §4)",
        )
    ctx = _op_context(args, paths, config, index)
    result = move_to_destination(ctx, record, destination)
    _print_result(result, config)
    if not ctx.dry_run:
        index.flush()
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    paths, config = _load(args)
    index = _open_index(paths, config)
    record = _record_for(index, config, args.note)
    target = _vault_path(config, args.target)
    if not target.is_file():
        raise VaultError(
            f"merge target not found: {target}",
            hint="the target must be an existing note; use `organize move` for a folder destination",
        )
    ctx = _op_context(args, paths, config, index)
    result = merge_into_note(ctx, record, target)
    _print_result(result, config)
    if not ctx.dry_run:
        index.flush()
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    paths, config = _load(args)
    index = _open_index(paths, config)
    record = _record_for(index, config, args.note)
    ctx = _op_context(args, paths, config, index)
    result = archive_capture(ctx, record)
    _print_result(result, config)
    if not ctx.dry_run:
        index.flush()
    return 0


def cmd_skip(args: argparse.Namespace) -> int:
    """``organize skip <note> --session <id>`` — the CLI door onto ``op.skip``
    (spec 10: every capability is scriptable).

    ``--session`` is required, and its absence is a ``SessionError`` rather
    than an argparse usage error, because the reason is semantic, not a typo:
    spec 03 §6 scopes "skipped" to a session, so a skip belonging to none is
    not a decision anyone can read back.

    WHAT THIS DOOR CANNOT DO: sessions live only in the running core's
    memory (there is no session store on disk), so this process cannot
    resolve the id to a live ``Session`` and cannot update its ``skipped``
    set — that accounting stays core-owned and happens over RPC. The id is
    recorded as the client's claim, exactly as ``--chosen-rank`` and
    ``--suggestions-json`` already are. What this door DOES give you is the
    doc-12 record: a scripted agent that ranks and then declines can say so.
    """
    paths, config = _load(args)
    index = _open_index(paths, config)
    record = _record_for(index, config, args.note)
    session_id = (getattr(args, "session", None) or "").strip()
    if not session_id:
        raise SessionError(
            "skip is a session action: it needs the session it belongs to",
            hint="pass --session <id>; `organize session list --json` reports the id",
        )
    ctx = _op_context(args, paths, config, index, session_id=session_id)
    skip_capture(ctx, record)
    _emit(f"skipped {_rel(record.path, config)} (session {session_id})")
    return 0


def _metadata_fields(config: Config) -> dict[str, MetadataFieldConfig]:
    """Kept as a thin alias so this module's readers see one name; the rule
    itself is ``config.metadata_fields_by_key`` (spec 10 §3 — core config, so
    both doors read the SAME definitions)."""
    return metadata_fields_by_key(config)


def _coerce_meta_value(field: MetadataFieldConfig | None, key: str, raw: str) -> Any:
    """Coerce one ``KEY=VALUE`` argument per its ``metadata_fields`` type
    (spec 07). Delegates to :func:`config.coerce_metadata_value`, which the
    RPC ``meta.set`` handler calls too — the coercion used to live here and
    only here, so the two doors disagreed about what ``tags=foo, Bar Baz``
    means and the nvim client wrote malformed frontmatter."""
    return coerce_metadata_value(field, key, raw)


def cmd_set_meta(args: argparse.Namespace) -> int:
    paths, config = _load(args)
    index = _open_index(paths, config)
    record = _record_for(index, config, args.note)
    path = Path(record.path)
    fields = _metadata_fields(config)

    changes: dict[str, Any] = {}
    replaced_lists: list[str] = []
    unconfigured: list[str] = []
    for raw_change in args.changes:
        match = _CHANGE_TOKEN.match(raw_change)
        if not match:
            raise ConfigError(
                f"malformed change {raw_change!r}",
                hint="use KEY=VALUE (an empty VALUE removes the field)",
            )
        key, raw_value = match.group(1).strip(), match.group(2)
        if not key:
            raise ConfigError(
                f"malformed change {raw_change!r}: empty key",
                hint="use KEY=VALUE (an empty VALUE removes the field)",
            )
        field = fields.get(key)
        if field is None:
            unconfigured.append(key)
        value = _coerce_meta_value(field, key, raw_value)
        if field is not None and field.type == "list" and value is not None:
            if field.append:
                # A note may legitimately have NO frontmatter block at all
                # (spec 02 quirk file); annotating one is a supported flow
                # (07 "Behavior"), so the append base is [] — not a crash.
                document = load_file(path)
                existing = document.frontmatter.get_list(key) if document.frontmatter else []
                merged = list(existing)
                for item in value:
                    if item not in merged:
                        merged.append(item)
                value = merged
            else:
                replaced_lists.append(key)
        changes[key] = value

    ctx = _op_context(args, paths, config, index)
    # A list field declaring `append = false` (spec 07) must REPLACE.
    # `update_frontmatter` merges `tags` by contract (05 §5), so name it in
    # `replace_keys` — this used to be a clear-then-set pair, which wrote the
    # file twice and recorded TWO actions for one logical edit (12 §2).
    result = update_frontmatter(
        ctx, path, changes, replace_keys=frozenset(replaced_lists)
    )
    _print_result(result, config)
    for key, value in sorted(changes.items()):
        _emit(f"  {key}: {'(removed)' if value is None else value}")
    for key in unconfigured:
        _emit(f"  note: {key!r} is not a configured metadata field (written as a plain frontmatter value)")
    if not ctx.dry_run:
        index.flush()
    return 0


def _completions_for(
    entry: MetadataFieldConfig, index: VaultIndex
) -> list[str]:
    """Resolve one field's completion source (spec 07 ``complete``)."""
    if entry.complete == "existing":
        return list(index.values_of(entry.key))
    if isinstance(entry.complete, list):
        return [str(value) for value in entry.complete]
    if entry.type == "enum":
        return list(entry.values)
    return []


def cmd_meta_fields(args: argparse.Namespace) -> int:
    """``organize meta-fields`` — the doc-07 field definitions and their
    completion values (spec 07, spec 10 §3).

    Spec 07 calls completion "what makes tag entry fast and consistent" and
    spec 10 §3 puts ``metadata_fields`` in CORE config so the UI reads it
    from the core rather than re-declaring it. Neither the CLI nor the RPC
    surface exposed the definitions or ``VaultIndex.values_of``, so a thin
    client could not obtain them and the feature was unreachable. The RPC
    twin is ``meta.fields`` / ``meta.values``.
    """
    paths, config = _load(args)
    index = _open_index(paths, config)

    if args.key:
        values = list(index.values_of(args.key))
        if args.json:
            _json_out({"key": args.key, "values": values})
            return 0
        for value in values:
            _emit(value)
        return 0

    fields = [
        {
            "key": entry.key,
            "type": entry.type,
            "keymap": entry.keymap,
            "prompt": entry.prompt,
            "append": entry.append,
            "complete": entry.complete,
            "values": list(entry.values),
            "normalize": entry.normalize,
            "completions": _completions_for(entry, index),
        }
        for entry in config.metadata_fields
    ]
    if args.json:
        _json_out({"fields": fields})
        return 0
    if not fields:
        _emit("no metadata fields configured")
        return 0
    for entry in fields:
        _emit(
            f"{entry['key']}\t{entry['type']}\t{entry['keymap']}\t"
            f"{','.join(entry['completions'])}"
        )
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    if args.session_command is None:
        _warn("usage: organize session list [K=V ...]")
        return 2
    paths, config = _load(args)
    index = _open_index(paths, config)
    filters, text = _split_filter_args(args.filters)
    criteria = QueryCriteria.from_filter_args(filters) if filters else None
    if text:
        criteria = criteria or QueryCriteria()
        criteria.text = text
    session = start_session(index, criteria)
    counts = session.counts()

    if args.json:
        _json_out(
            {
                "session_id": session.session_id,
                "state": session.state.value,
                "counts": {
                    "processed": counts.processed,
                    "skipped": counts.skipped,
                    "remaining": counts.remaining,
                },
                "captures": [
                    {
                        "path": record.path,
                        "relative_path": _rel(record.path, config),
                        "title": record.title,
                        "tags": list(record.tags),
                        "timestamp": record.timestamp,
                    }
                    for record in session.captures
                ],
            }
        )
        return 0

    if not session.captures:
        # Spec 03 §2 / ARCHITECTURE resolution #9: an empty match is a notice,
        # not an error — the session is valid and the exit code stays 0.
        _emit("No captures found matching filters")
        return 0
    for position, record in enumerate(session.captures, start=1):
        _emit(
            f"{position}\t{_rel(record.path, config)}\t{record.timestamp or ''}\t"
            f"{','.join(record.tags)}"
        )
    _emit(f"session {session.session_id}: {counts.remaining} capture(s) remaining")
    return 0


def cmd_routes(args: argparse.Namespace) -> int:
    if args.routes_command is None:
        _warn("usage: organize routes {list|resolve <note>|describe <path> [text]}")
        return 2
    paths, config = _load(args)

    if args.routes_command == "list":
        if args.json:
            _json_out(
                [
                    {
                        "tags": list(route.tags),
                        "destination": route.destination,
                        "mode": route.mode,
                        "auto": route.auto,
                        "template": route.template,
                        "description": route.description,
                    }
                    for route in config.routes
                ]
            )
            return 0
        if not config.routes:
            _emit("no routes configured")
            return 0
        for route in config.routes:
            _emit(f"{','.join(route.tags)}\t{route.destination}\t{route.mode}\tauto={str(route.auto).lower()}")
        return 0

    index = _open_index(paths, config)

    if args.routes_command == "resolve":
        record = _record_for(index, config, args.note)
        matches = routes_mod.resolve(list(record.tags), config)
        if args.json:
            _json_out(
                [
                    {
                        "route": match.route_name,
                        "destination": str(match.destination),
                        "relative_destination": _rel(match.destination, config),
                        "mode": match.route.mode,
                        "is_folder": match.is_folder,
                        "auto": match.route.auto,
                        "description": match.route.description,
                    }
                    for match in matches
                ]
            )
            return 0
        if not matches:
            _emit(f"no routes match {_rel(record.path, config)} (tags: {','.join(record.tags) or 'none'})")
            return 0
        for match in matches:
            _emit(
                f"{match.route_name}\t{_rel(match.destination, config)}\t{match.route.mode}\t"
                f"{'folder' if match.is_folder else 'file'}"
            )
        return 0

    # describe
    target = _vault_path(config, args.path)
    if args.text is not None:
        try:
            result = routes_mod.set_description(_op_context(args, paths, config, index), target, args.text)
        except NotImplementedError as exc:
            raise OrganizeError(
                f"routes describe cannot write yet: {exc}",
                hint="reading a description works today; writing arrives with the routes phase (spec 11 §3)",
            ) from exc
        _print_result(result, config)
        return 0
    description = routes_mod.get_description(target, index, config)
    if description is None:
        _emit(f"no description for {_rel(target, config)}")
        return 0
    _emit(description)
    return 0


def _ndjson(raw: str, whole_buffer_error: ValueError) -> list[Any]:
    """Parse newline-delimited JSON, one object per non-blank line.

    Raised with the ORIGINAL whole-buffer error when a line fails, because
    "your stdin is not one JSON document" is the more useful message when the
    input was never meant to be NDJSON in the first place.
    """
    items: list[Any] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except ValueError as exc:
            raise ActionSchemaError(
                f"stdin is not valid JSON: {whole_buffer_error}; "
                f"read as newline-delimited JSON, line {number} also failed: {exc}",
                hint="expected one ActionRecord object, a JSON array, or newline-delimited objects",
            ) from exc
    if not items:
        raise ActionSchemaError(
            f"stdin is not valid JSON: {whole_buffer_error}",
            hint="expected one ActionRecord object, a JSON array, or newline-delimited objects",
        )
    return items


def cmd_record(args: argparse.Namespace) -> int:
    paths, config = _load(args)
    raw = sys.stdin.read()
    if not raw.strip():
        raise ActionSchemaError(
            "no JSON on stdin",
            hint="pipe one ActionRecord object (or a JSON array of them) into `organize record`",
        )
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        # NDJSON fallback. `organize actions export` emits exactly one JSON
        # object per line, so `export | record` — the natural corpus-transfer
        # path — was the one format the parser rejected, while its own hint
        # advertised "newline-delimited objects".
        items = _ndjson(raw, exc)
    else:
        items = payload if isinstance(payload, list) else [payload]
    recorder = ActionRecorder(paths.actions_dir)
    written = 0
    for item in items:
        if not isinstance(item, dict):
            raise ActionSchemaError(
                f"expected an ActionRecord object, got {type(item).__name__}",
                hint="see spec 12 §2 for the record schema",
            )
        item = dict(item)
        item.setdefault("id", new_action_id())
        item.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if args.actor:
            item["actor"] = args.actor
        record = ActionRecord.from_json(item)
        if args.dry_run:
            _emit(f"record (dry-run): {record.id} {record.operation} by {record.actor} (not written)")
            continue
        if not recorder.record(record):
            raise OrganizeError(
                f"could not append action {record.id} to {paths.actions_dir}",
                hint="check that the state directory exists and is writable (spec 12 §2)",
            )
        # Same consumer the op path uses: an externally supplied move record
        # is a filing decision too, and it used to reach the corpus without
        # ever reaching the learner (spec 12 §2 "Uses" #2).
        _learn_from_action(paths, config, record)
        _emit(f"{record.id}")
        written += 1
    if written and not args.dry_run:
        logger.info("record: appended %d action(s) to %s", written, paths.actions_dir)
    return 0


def cmd_actions(args: argparse.Namespace) -> int:
    """``organize actions export|stats``.

    Dry-run records are hidden by default, but that exclusion is NOT
    implemented here: ``ActionRecorder.query``/``stats``/``export`` default to
    ``include_dry_run=False``, so every corpus reader (this CLI, the server,
    the doc-13 retrieval still to come) inherits the rule instead of each
    re-deriving it. This used to be a local ``_CorpusView`` subclass; the
    integrator moved it into the recorder. ``--include-dry-run`` opts back in
    for debugging a rehearsal — never for learning from one.
    """
    if args.actions_command is None:
        _warn("usage: organize actions {export|stats}")
        return 2
    paths, config = _load(args)
    recorder = ActionRecorder(paths.actions_dir)

    if args.actions_command == "export":
        out = expand(args.out) if args.out else None
        count = recorder.export(
            out,
            operation=args.operation,
            actor=args.actor,
            since=args.since,
            until=args.until,
            include_dry_run=args.include_dry_run,
        )
        if out is not None:
            _emit(f"exported {count} record(s) to {out}")
        return 0

    stats = recorder.stats(include_dry_run=args.include_dry_run)
    if args.json:
        _json_out(stats)
        return 0
    for key, value in stats.items():
        _emit(f"{key}: {json.dumps(value, sort_keys=True) if isinstance(value, dict) else value}")
    return 0


def cmd_auto_organize(args: argparse.Namespace) -> int:
    """Exists from day one; not implemented until Phase 6 (spec 13 §3)."""
    print("auto-organize: not implemented (ships in the automatic-organize phase, spec 13)", file=sys.stderr)
    return 1


def cmd_run_consumers(args: argparse.Namespace) -> int:
    if args.list_consumers:
        # Spec 06 §4 / 08 §B2: listing constructs nothing — no config needed.
        for name in sorted(get_consumer_types()):
            _emit(name)
        return 0
    _warn("run-consumers: not implemented (the automation pipeline arrives in Phase 3, spec 06)")
    _warn("hint: `organize run-consumers --list-consumers` already lists the registered consumer types")
    return 1


def _state_issues(paths: CorePaths, config: Config) -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    state_dir = paths.state_dir
    if not state_dir.is_dir():
        # WARNING, not error: state dirs are created on demand, so "missing"
        # is the expected state of a correct first run. Failing here would
        # make `organize health` unusable as the setup/systemd gate it is
        # meant to be (spec 01 §success #4 wants loud reporting, not
        # nonzero-on-not-yet-initialized). `--strict` still fails on it.
        issues.append(
            HealthIssue(
                severity="warning",
                message=f"state directory does not exist yet: {state_dir}",
                hint="run any `organize` command (state dirs are created on demand) or set ORGANIZE_CORE_STATE_DIR",
            )
        )
        return issues
    probe = state_dir / ".organize-health-probe"
    try:
        probe.write_text("", encoding="utf-8", errors="replace")
        probe.unlink()
    except OSError as exc:
        issues.append(
            HealthIssue(
                severity="error",
                message=f"state directory is not writable: {state_dir} ({exc})",
                hint="fix the permissions — every index/learning/action write goes here",
            )
        )
    if not paths.index_path.exists():
        issues.append(
            HealthIssue(
                severity="warning",
                message=f"no index snapshot at {paths.index_path}",
                hint="run `organize index --full`",
            )
        )
    orphans = find_orphaned_temp_files(config.vault.root, now=time.time())
    if orphans:
        issues.append(
            HealthIssue(
                severity="warning",
                message=f"{len(orphans)} abandoned atomic-write temp file(s) in the vault "
                f"(e.g. {orphans[0]})",
                hint="a killed/power-lost write left them behind; they are safe to delete "
                "and they replicate over Syncthing (spec 05 §1.3)",
            )
        )
    backup_dir = config.vault.root / config.file_ops.backup_dir
    if config.file_ops.create_backups and backup_dir.exists() and not backup_dir.is_dir():
        issues.append(
            HealthIssue(
                severity="error",
                message=f"backup path is not a directory: {backup_dir}",
                hint="[file_ops] backup_dir must name a directory inside the vault (spec 05 §1.4)",
            )
        )
    return issues


def _socket_issues(paths: CorePaths, config: Config) -> list[HealthIssue]:
    """WARNING when the resolved socket path is too long for AF_UNIX.

    `bind()` fails with a bare "AF_UNIX path too long" and no errno, so a
    deep `$XDG_RUNTIME_DIR` (or a tmp-dir override) turns into a `serve` that
    cannot start for a reason nothing explains. Health is the place that is
    supposed to say so BEFORE the daemon is expected to come up. It stays a
    warning — the limit is platform-dependent (108 bytes on Linux, 104 on
    BSD) and this check is a prediction, not the bind itself, so per the
    recorded exit policy it prints and still exits 0.
    """
    from organize_core.server import _AF_UNIX_PATH_MAX

    socket_path = Path(config.server.socket_path or paths.socket_path)
    length = len(str(socket_path).encode("utf-8"))
    if length < _AF_UNIX_PATH_MAX:
        return []
    return [
        HealthIssue(
            severity="warning",
            message=f"socket path is {length} bytes, at or over the ~{_AF_UNIX_PATH_MAX}-byte "
            f"AF_UNIX limit: {socket_path}",
            hint="`organize serve` will fail to bind — shorten it with `--socket PATH`, "
            "[server] socket_path, or --runtime-dir / $ORGANIZE_CORE_RUNTIME_DIR",
        )
    ]


def cmd_health(args: argparse.Namespace) -> int:
    if args.example_config:
        _emit(example_config_toml())
        return 0
    paths = _resolve_paths(args)
    _configure_logging(args)
    issues: list[HealthIssue] = []
    config: Config | None = None
    try:
        config_file = expand(args.config) if args.config else None
        config = load_config(paths, config_file=config_file)
    except OrganizeError as exc:
        issues.append(HealthIssue(severity="error", message=str(exc), hint=exc.hint))

    stats: dict[str, Any] = {}
    if config is not None:
        issues.extend(check_vault(config))
        issues.extend(_state_issues(paths, config))
        issues.extend(_socket_issues(paths, config))
        if paths.index_path.exists():
            try:
                index = _open_index(paths, config)
                stats = index.stats()
            except OrganizeError as exc:
                issues.append(HealthIssue(severity="error", message=str(exc), hint=exc.hint))

    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    # Exit policy (spec 01 §success #4 asks for loud REPORTING, not
    # nonzero-on-warning): ERRORs fail, warnings are printed and pass, and
    # --strict promotes warnings so a CI/setup gate can demand a clean bill.
    # A first run legitimately has no index snapshot; failing on that would
    # make `organize health` useless as a systemd/setup precondition.
    failed = bool(errors) or (bool(warnings) and args.strict)
    payload = {
        "ok": not errors and not warnings,
        "failed": failed,
        "strict": bool(args.strict),
        "config_file": str(paths.config_file if args.config is None else expand(args.config)),
        "state_dir": str(paths.state_dir),
        "runtime_dir": str(paths.runtime_dir),
        "vault_root": str(config.vault.root) if config else None,
        "index": stats,
        "issues": [
            {"severity": issue.severity, "message": issue.message, "hint": issue.hint} for issue in issues
        ],
    }
    if args.json:
        _json_out(payload)
    else:
        _emit(f"config: {payload['config_file']}")
        _emit(f"state:  {paths.state_dir}")
        if config is not None:
            _emit(f"vault:  {config.vault.root}")
        if stats:
            _emit(f"index:  {stats.get('total', 0)} notes, {stats.get('capture_backlog', 0)} raw captures")
        for issue in issues:
            _emit(f"{issue.severity.upper()}: {issue.message}")
            if issue.hint:
                _emit(f"  hint: {issue.hint}")
        if payload["ok"]:
            _emit("health: OK")
        else:
            verdict = "FAIL" if failed else "OK (warnings only)"
            _emit(f"health: {len(errors)} error(s), {len(warnings)} warning(s) — {verdict}")
            if warnings and not errors and not args.strict:
                _emit("  (re-run with --strict to fail on warnings)")
    return 1 if failed else 0


def cmd_serve(args: argparse.Namespace) -> int:
    from organize_core.server import OrganizeServer

    paths, config = _load(args)
    socket_path = expand(args.socket) if args.socket else (config.server.socket_path or paths.socket_path)
    idle = args.idle_timeout if args.idle_timeout is not None else config.server.idle_timeout_seconds
    server = OrganizeServer(config, paths, socket_path=Path(socket_path), idle_timeout_seconds=idle)

    # Announce only once the socket is actually bound. Printing "listening on
    # X" before serve_forever() got there made a refused start (unwritable
    # path, another instance holding the lock) claim success on stderr and
    # then contradict itself — the ServerError/AlreadyRunning below is the
    # truth, so the banner has to wait for `ready`.
    def announce() -> None:
        if server.ready.wait(timeout=30.0):
            _warn(f"organize serve: listening on {server.socket_path}")

    banner = threading.Thread(target=announce, name="serve-banner", daemon=True)
    banner.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


_HANDLERS = {
    "index": cmd_index,
    "search": cmd_search,
    "suggest": cmd_suggest,
    "move": cmd_move,
    "merge": cmd_merge,
    "archive": cmd_archive,
    "skip": cmd_skip,
    "set-meta": cmd_set_meta,
    "meta-fields": cmd_meta_fields,
    "session": cmd_session,
    "routes": cmd_routes,
    "record": cmd_record,
    "actions": cmd_actions,
    "auto-organize": cmd_auto_organize,
    "run-consumers": cmd_run_consumers,
    "health": cmd_health,
    "serve": cmd_serve,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point. Parses, dispatches, maps OrganizeError
    to exit 1 with the message (+ hint) on stderr — loud, never a
    traceback for a user-fixable failure (spec 09 §1.5)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    debug = bool(getattr(args, "debug", False))
    try:
        _configure_logging(args)
        return _HANDLERS[args.command](args)
    except OrganizeError as exc:
        if debug:
            traceback.print_exc()
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return 1
    except NotImplementedError as exc:
        if debug:
            traceback.print_exc()
        print(f"not implemented: {exc or args.command}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:  # `organize actions export | head`
        return 0
    except OSError as exc:
        # Environment failures (permissions, full disk, socket path too long)
        # are user-fixable, so they get the loud one-liner too — not a
        # traceback (09 §1.5).
        if debug:
            traceback.print_exc()
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - the last line of the CLI
        # A REAL BUG. Still one attributable line rather than a raw traceback:
        # a batch run over the backlog emitted seven bare tracebacks in one
        # sweep, unattributable to any note because argparse's Namespace is
        # all the frame carries. `--debug` is the only way to see the stack.
        if debug:
            traceback.print_exc()
        subject = " ".join(
            str(getattr(args, name))
            for name in ("note", "path", "source", "capture")
            if getattr(args, name, None)
        )
        where = f"{args.command} {subject}".strip()
        print(f"internal error in `organize {where}`: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "hint: this is a bug in organize, not something you did — rerun with "
            "--debug for the traceback and report it",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
