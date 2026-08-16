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

The parser tree below is fully built (structural contract — ``--help`` for
every subcommand is the scaffold gate); command handlers are Phase 1+.

Note: ``auto-organize`` exists from day one and reports "not implemented"
until Phase 6 (spec 13 §3 cross-check list).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from organize_core import __version__

# Subcommand names, single source of truth (test_scaffold iterates this).
SUBCOMMANDS: tuple[str, ...] = (
    "index",
    "search",
    "suggest",
    "move",
    "merge",
    "archive",
    "set-meta",
    "session",
    "routes",
    "record",
    "actions",
    "auto-organize",
    "run-consumers",
    "health",
    "serve",
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

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("index", help="scan the vault and update the index (spec 03 §7)")
    p.add_argument("--full", action="store_true", help="rebuild from zero (reindex)")
    p.add_argument("--stats", action="store_true", help="print index statistics only")

    p = sub.add_parser("search", help="query the index (spec 03 §2 filters + free text)")
    p.add_argument("query", nargs="*", help="free text and/or k=v filters (tags=a,b sources=... since=YYYY-MM-DD)")

    p = sub.add_parser("suggest", help="ranked destinations for a note (spec 04)")
    p.add_argument("note", nargs="?", help="path to the capture note")
    p.add_argument("--text", help="score arbitrary text instead of a note (spec 13 §3)")
    p.add_argument("--tags", help="comma list of tags to assume with --text")

    p = sub.add_parser("move", help="move a capture to a destination folder (spec 05 §2)")
    p.add_argument("note")
    p.add_argument("destination", help="folder path, vault-relative or absolute")

    p = sub.add_parser("merge", help="merge a capture into an existing note (spec 05 §4)")
    p.add_argument("note")
    p.add_argument("target", help="target note path")

    p = sub.add_parser("archive", help="archive a capture now (spec 05 §3)")
    p.add_argument("note")

    p = sub.add_parser("set-meta", help="set frontmatter fields (spec 05 §5, 07)")
    p.add_argument("note")
    p.add_argument("changes", nargs="+", metavar="KEY=VALUE")

    p = sub.add_parser("session", help="organizing sessions (spec 03 §2)")
    ssub = p.add_subparsers(dest="session_command", metavar="ACTION")
    sp = ssub.add_parser("list", help="list captures matching filters, session order")
    sp.add_argument("filters", nargs="*", metavar="K=V")

    p = sub.add_parser("routes", help="tag routing + NL descriptions (spec 11)")
    rsub = p.add_subparsers(dest="routes_command", metavar="ACTION")
    rsub.add_parser("list", help="list configured routes")
    rp = rsub.add_parser("resolve", help="routes matching a note's tags")
    rp.add_argument("note")
    rp = rsub.add_parser("describe", help="set a folder/file NL description (spec 11 §3)")
    rp.add_argument("path")
    rp.add_argument("text")

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
    asub.add_parser("stats", help="accept-rates, per-route volumes (spec 12 §2)")

    p = sub.add_parser("auto-organize", help="route text/notes automatically (spec 13; Phase 6)")
    p.add_argument("file", nargs="?", help="note path, or - for stdin")
    p.add_argument("--digest", action="store_true", help="daily digest of auto-applied actions")

    p = sub.add_parser("run-consumers", help="run the automation pipeline (spec 06)")
    p.add_argument("--consumer", action="append", default=None, metavar="NAME",
                   help="run only this consumer (repeatable; case-insensitive)")
    p.add_argument("--list-consumers", action="store_true",
                   help="list registered consumer types (constructs nothing)")

    sub.add_parser("health", help="config + vault + core diagnostics (spec 03 §2, 10)")

    p = sub.add_parser("serve", help="JSON-RPC server on a unix socket (spec 10 §1-2)")
    p.add_argument("--socket", metavar="PATH", help="socket path override")
    p.add_argument("--idle-timeout", type=float, default=None, metavar="SECONDS")

    return parser


# --- handlers (Phase 1+; signatures fixed) ---------------------------------


def cmd_index(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_search(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_suggest(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_move(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_merge(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_archive(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_set_meta(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_session(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_routes(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_record(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_actions(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_auto_organize(args: argparse.Namespace) -> int:
    """Exists from day one; not implemented until Phase 6 (spec 13 §3)."""
    print("auto-organize: not implemented (ships in the automatic-organize phase, spec 13)", file=sys.stderr)
    return 1


def cmd_run_consumers(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_health(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_serve(args: argparse.Namespace) -> int:
    raise NotImplementedError


_HANDLERS = {
    "index": cmd_index,
    "search": cmd_search,
    "suggest": cmd_suggest,
    "move": cmd_move,
    "merge": cmd_merge,
    "archive": cmd_archive,
    "set-meta": cmd_set_meta,
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
    # Phase 1 wires CorePaths/config/logging setup here, before dispatch.
    return _HANDLERS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
