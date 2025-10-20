#!/usr/bin/env python3
"""Query raw capture notes using flexible filters for automation pipelines."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover - dependency hint
    sys.stderr.write(
        "capture_query.py requires PyYAML (pip install pyyaml) to parse note frontmatter.\n",
    )
    raise SystemExit(2) from exc


class FrontmatterLoader(yaml.SafeLoader):
    """YAML loader that keeps timestamp-like scalars as strings."""


# Remove the implicit resolver for timestamps so ISO strings stay as text.
for ch, resolvers in list(FrontmatterLoader.yaml_implicit_resolvers.items()):
    FrontmatterLoader.yaml_implicit_resolvers[ch] = [
        (tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"
    ]


def yaml_load(text: str) -> Dict[str, Any]:
    if not text.strip():
        return {}
    data = yaml.load(text, Loader=FrontmatterLoader)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Frontmatter must parse to a mapping")
    return data


@dataclass
class Note:
    path: Path
    frontmatter: Dict[str, Any]
    content: str
    raw_text: str


def read_note(path: Path) -> Note:
    raw = path.read_text(encoding="utf-8")
    frontmatter: Dict[str, Any] = {}
    content = raw

    if raw.startswith("---"):
        lines = raw.splitlines(keepends=True)
        if len(lines) > 0 and lines[0].strip() == "---":
            closing_idx: Optional[int] = None
            for idx in range(1, len(lines)):
                if lines[idx].strip() == "---":
                    closing_idx = idx
                    break
            if closing_idx is not None:
                fm_text = "".join(lines[1:closing_idx])
                frontmatter = yaml_load(fm_text)
                content = "".join(lines[closing_idx + 1 :])
    return Note(path=path, frontmatter=frontmatter, content=content, raw_text=raw)


def ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def normalize_str_list(values: Optional[Sequence[Any]]) -> List[str]:
    if not values:
        return []
    return [str(value) for value in values]


def parse_key_value(expr: str) -> Tuple[str, str]:
    if "=" not in expr:
        raise argparse.ArgumentTypeError(f"Expected KEY=VALUE, got: {expr}")
    key, value = expr.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        raise argparse.ArgumentTypeError("Filter key may not be empty")
    return key, value


def parse_value(value: str) -> Any:
    try:
        loaded = yaml.load(value, Loader=FrontmatterLoader)
    except yaml.YAMLError:
        return value
    return loaded


def get_by_path(data: Dict[str, Any], path: Sequence[str]) -> Any:
    current: Any = data
    for segment in path:
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return None
    return current


@dataclass
class Filters:
    any_tags: List[str]
    require_all_tags: bool
    timestamps: List[str]
    created_dates: List[str]
    last_edited_dates: List[str]
    capture_ids: List[str]
    processing_statuses: List[str]
    modalities: List[str]
    require_all_modalities: bool
    contexts: List[str]
    require_all_contexts: bool
    sources: List[str]
    require_all_sources: bool
    ids: List[str]
    aliases: List[str]
    where_clauses: List[Tuple[List[str], Any]]
    contains: List[str]
    case_sensitive: bool
    limit: Optional[int]


def build_filters(args: argparse.Namespace) -> Filters:
    where: List[Tuple[List[str], Any]] = []
    for key, value in args.where or []:
        where.append((key.split("."), parse_value(value)))
    filters = Filters(
        any_tags=normalize_str_list(args.tag),
        require_all_tags=args.require_all_tags,
        timestamps=normalize_str_list(args.timestamp),
        created_dates=normalize_str_list(args.created_date),
        last_edited_dates=normalize_str_list(args.last_edited_date),
        capture_ids=normalize_str_list(args.capture_id),
        processing_statuses=normalize_str_list(args.processing_status),
        modalities=normalize_str_list(args.modality),
        require_all_modalities=args.require_all_modalities,
        contexts=normalize_str_list(args.context),
        require_all_contexts=args.require_all_contexts,
        sources=normalize_str_list(args.source),
        require_all_sources=args.require_all_sources,
        ids=normalize_str_list(args.id),
        aliases=normalize_str_list(args.alias),
        where_clauses=where,
        contains=normalize_str_list(args.search),
        case_sensitive=args.case_sensitive,
        limit=args.limit,
    )
    return filters


def matches_filters(note: Note, filters: Filters) -> bool:
    fm = note.frontmatter

    if filters.ids:
        note_id = str(fm.get("id", ""))
        if note_id not in filters.ids:
            return False

    if filters.capture_ids:
        capture_value = str(fm.get("capture_id", ""))
        if capture_value not in filters.capture_ids:
            return False

    if filters.timestamps:
        timestamp_value = str(fm.get("timestamp", ""))
        if timestamp_value not in filters.timestamps:
            return False

    if filters.created_dates:
        created_value = str(fm.get("created_date", ""))
        if created_value not in filters.created_dates:
            return False

    if filters.last_edited_dates:
        edited_value = str(fm.get("last_edited_date", ""))
        if edited_value not in filters.last_edited_dates:
            return False

    if filters.processing_statuses:
        status_value = str(fm.get("processing_status", ""))
        if status_value not in filters.processing_statuses:
            return False

    if filters.aliases:
        alias_values = {str(value) for value in ensure_list(fm.get("aliases"))}
        if not any(alias in alias_values for alias in filters.aliases):
            return False

    if filters.any_tags:
        tags = {str(value) for value in ensure_list(fm.get("tags"))}
        if filters.require_all_tags:
            if not all(tag in tags for tag in filters.any_tags):
                return False
        else:
            if not any(tag in tags for tag in filters.any_tags):
                return False

    if filters.modalities:
        modality_values = {str(value) for value in ensure_list(fm.get("modalities"))}
        if filters.require_all_modalities:
            if not all(modality in modality_values for modality in filters.modalities):
                return False
        else:
            if not any(modality in modality_values for modality in filters.modalities):
                return False

    if filters.contexts:
        context_values = {str(value) for value in ensure_list(fm.get("context"))}
        if filters.require_all_contexts:
            if not all(context in context_values for context in filters.contexts):
                return False
        else:
            if not any(context in context_values for context in filters.contexts):
                return False

    if filters.sources:
        source_values = {str(value) for value in ensure_list(fm.get("sources"))}
        if filters.require_all_sources:
            if not all(source in source_values for source in filters.sources):
                return False
        else:
            if not any(source in source_values for source in filters.sources):
                return False

    for path_segments, expected in filters.where_clauses:
        actual = get_by_path(fm, path_segments)
        if actual is None:
            return False
        if isinstance(actual, list):
            actual_values = {str(item) for item in actual}
            if str(expected) not in actual_values:
                return False
        else:
            if str(actual) != str(expected):
                return False

    if filters.contains:
        haystack = note.content if filters.case_sensitive else note.content.lower()
        for needle in filters.contains:
            probe = needle if filters.case_sensitive else needle.lower()
            if probe not in haystack:
                return False

    return True


def iter_notes(capture_dir: Path) -> Iterable[Note]:
    if not capture_dir.exists():
        raise FileNotFoundError(f"Capture directory not found: {capture_dir}")
    for path in sorted(capture_dir.rglob("*.md")):
        if path.is_file():
            yield read_note(path)


def output_notes(notes: List[Note], fmt: str) -> None:
    for idx, note in enumerate(notes):
        if fmt == "markdown":
            sys.stdout.write(note.raw_text.rstrip("\n"))
            sys.stdout.write("\n")
            if idx != len(notes) - 1:
                sys.stdout.write("\n")
        elif fmt == "content":
            sys.stdout.write(note.content.rstrip("\n"))
            sys.stdout.write("\n")
            if idx != len(notes) - 1:
                sys.stdout.write("\n")
        elif fmt == "paths":
            sys.stdout.write(f"{note.path}\n")
        elif fmt == "json":
            payload = {
                "path": str(note.path),
                "frontmatter": note.frontmatter,
                "content": note.content,
            }
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            raise ValueError(f"Unsupported format: {fmt}")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Search frontmatter-driven capture notes like a lightweight database. "
            "Useful for piping matches into other CLI tools."
        ),
    )
    parser.add_argument(
        "--root",
        default=Path.cwd(),
        type=Path,
        help="Vault root directory; defaults to current working directory.",
    )
    parser.add_argument(
        "--capture-dir",
        default=Path("capture/raw_capture"),
        type=Path,
        help="Relative or absolute path to the raw capture folder.",
    )
    parser.add_argument(
        "--timestamp",
        action="append",
        help="Filter by exact ISO timestamp from the frontmatter (repeatable).",
    )
    parser.add_argument(
        "--created-date",
        dest="created_date",
        action="append",
        help="Filter by created_date (YYYY-MM-DD). Repeatable.",
    )
    parser.add_argument(
        "--last-edited-date",
        dest="last_edited_date",
        action="append",
        help="Filter by last_edited_date (YYYY-MM-DD). Repeatable.",
    )
    parser.add_argument(
        "--capture-id",
        dest="capture_id",
        action="append",
        help="Filter by capture_id (repeatable).",
    )
    parser.add_argument(
        "--processing-status",
        dest="processing_status",
        action="append",
        help="Filter by processing_status (repeatable).",
    )
    parser.add_argument(
        "--id",
        action="append",
        help="Filter by exact note id (frontmatter id field).",
    )
    parser.add_argument(
        "--alias",
        action="append",
        help="Filter by alias values.",
    )
    parser.add_argument(
        "--tag",
        action="append",
        help="Filter notes that contain at least one matching tag. Repeatable.",
    )
    parser.add_argument(
        "--require-all-tags",
        action="store_true",
        help="Require every --tag value to be present (AND instead of OR).",
    )
    parser.add_argument(
        "--modality",
        action="append",
        help="Filter by modality values (repeatable).",
    )
    parser.add_argument(
        "--require-all-modalities",
        action="store_true",
        help="Require every --modality value to be present (AND).",
    )
    parser.add_argument(
        "--context",
        action="append",
        help="Filter by context values (repeatable).",
    )
    parser.add_argument(
        "--require-all-contexts",
        action="store_true",
        help="Require every --context value to be present (AND).",
    )
    parser.add_argument(
        "--source",
        action="append",
        help="Filter by source values (repeatable).",
    )
    parser.add_argument(
        "--require-all-sources",
        action="store_true",
        help="Require every --source value to be present (AND).",
    )
    parser.add_argument(
        "--location",
        action="append",
        type=parse_key_value,
        metavar="FIELD=VALUE",
        help="Filter by location.<field> (e.g., city=Champaign).",
    )
    parser.add_argument(
        "--metadata",
        action="append",
        type=parse_key_value,
        metavar="FIELD=VALUE",
        help="Filter by metadata.<field> (e.g., source=web).",
    )
    parser.add_argument(
        "--where",
        action="append",
        type=parse_key_value,
        metavar="KEY=VALUE",
        help=(
            "Filter by arbitrary dotted frontmatter paths (e.g., processing_status=raw, "
            "location.city=Champaign)."
        ),
    )
    parser.add_argument(
        "--search",
        action="append",
        help="Substring match against the Markdown content. Repeatable.",
    )
    parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Make --search comparisons case-sensitive.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "content", "json", "paths"],
        default="markdown",
        help="Output format for matched notes (default: markdown).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Stop after emitting N matches.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="capture-query 0.1.0",
    )

    args = parser.parse_args(argv)

    # Expand convenience filters into the generic where clause list.
    if args.location:
        args.where = (args.where or []) + [
            (f"location.{key}", value) for key, value in args.location
        ]
    if args.metadata:
        args.where = (args.where or []) + [
            (f"metadata.{key}", value) for key, value in args.metadata
        ]

    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    capture_dir = args.capture_dir
    if not capture_dir.is_absolute():
        capture_dir = (args.root / capture_dir).resolve()

    filters = build_filters(args)

    matches: List[Note] = []
    try:
        for note in iter_notes(capture_dir):
            if matches_filters(note, filters):
                matches.append(note)
                if filters.limit and len(matches) >= filters.limit:
                    break
    except FileNotFoundError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    output_notes(matches, args.format)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
