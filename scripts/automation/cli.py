"""CLI entrypoint for capture-driven automations."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from . import AutomationStore, NoteEmitter, iter_note_payloads, load_config
from .consumers import build_consumers
from .emitter import NoteState


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process capture notes and fan out updates to automation consumers.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to automations TOML config (defaults to ~/.config/para-organize/automations.toml).",
    )
    parser.add_argument(
        "--consumer",
        action="append",
        dest="consumers",
        metavar="NAME",
        help="Limit the run to specific consumers (repeatable).",
    )
    parser.add_argument(
        "--list-consumers",
        action="store_true",
        help="Print the configured consumers and exit.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override the log level (default from config).",
    )
    return parser.parse_args(argv)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    log_level = args.log_level or config.log_level
    setup_logging(log_level)

    consumers = build_consumers(config)
    if args.list_consumers:
        for consumer in consumers:
            print(consumer.name)
        return 0

    if args.consumers:
        requested = {name.lower() for name in args.consumers}
        consumers = [consumer for consumer in consumers if consumer.name.lower() in requested]
        if not consumers:
            logging.error("No matching consumers for filters: %s", ", ".join(args.consumers))
            return 2

    store = AutomationStore(config.database_path)
    emitter = NoteEmitter(store)

    try:
        payloads = list(iter_note_payloads(config.vault_root, config.capture_dir))
    except FileNotFoundError as exc:
        logging.error("Capture directory missing: %s", exc)
        return 1

    states = emitter.refresh(payloads)
    summary: dict[str, dict[str, int]] = {}
    failure = False

    for consumer in consumers:
        summary[consumer.name] = {"success": 0, "skip": 0, "error": 0}
        pending_states = list(emitter.pending_for_consumer(consumer.name, states))
        if not pending_states:
            logging.debug("No updates for consumer %s", consumer.name)
            continue

        logging.info(
            "Dispatching %d notes to consumer %s",
            len(pending_states),
            consumer.name,
        )

        for state in pending_states:
            if not consumer.matches(state):
                continue
            try:
                result = consumer.handle(state, store)
            except Exception:  # noqa: BLE001 - bubble up after logging
                failure = True
                summary[consumer.name]["error"] += 1
                logging.exception(
                    "Consumer %s failed on note %s",
                    consumer.name,
                    state.note.path,
                )
            else:
                summary[consumer.name][result.status] += 1

    for name, counts in summary.items():
        logging.info(
            "Consumer %s: success=%d skip=%d error=%d",
            name,
            counts["success"],
            counts["skip"],
            counts["error"],
        )

    store.close()
    return 1 if failure else 0


if __name__ == "__main__":
    sys.exit(main())
