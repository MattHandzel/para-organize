"""``taskwarrior`` consumer: todo-tagged captures → Taskwarrior tasks
(spec 06 §3.1).

Live defaults-of-record (06 §2): marker_tag="todo",
default_project="Inbox", additional_tags=["para","automation"],
review_tag="not_reviewed", annotation_template="Captured from {id}".

Non-negotiables (the B-series fixes):
- EVERY ``task`` subprocess: explicit timeout + decode with
  ``errors="replace"`` (B1 — the live 3-month outage).
- Constructor pure; ``task export`` / ``task _tags`` fetched lazily once
  per run (B2).
- ``remove_unknown_tags`` whitelists review_tag AND additional_tags (B7),
  logging every dropped tag.
- Backup ``~/.task`` before the first import of a run to
  ``<state>/backups/taskwarrior/<UTC-ts>`` with retention newest N=10 /
  30 days (B6).
- LLM enrichment optional; any LLM failure degrades to an unenriched task,
  never blocks (06 §3.1).
- Writes nothing back to the note (parity).

Instance lifetime is ONE RUN: the runner constructs consumers per run, so
"lazily, once" and "once per run" are the same thing here. :meth:`reset_run`
exists for callers that choose to reuse an instance.

Two deliberate departures from the old implementation, both recorded here
because a reviewer will otherwise read them as drift:

1. **Tag normalization is taskwarrior-domain, not vault-domain.** Spec 06
   §3.1 mandates ``strip, spaces→_, lowercase``; the shared
   ``frontmatter.normalize_tag`` (spec 04 §1) maps underscores → hyphens,
   which would silently rewrite Matt's ``not_reviewed`` review tag and every
   underscored task tag in his 10-year task history. The ONE-normalizer law
   (09 §2) is about matching vault tags to vault folders; Taskwarrior's tag
   namespace is a foreign system's. Frontmatter is still parsed only through
   the shared module — no second parser (08 §B9/§B16).
2. **Deterministic ordering.** The old code extracted the ``project:`` tag
   while iterating a ``set``, so a note with two project tags picked a
   different project per interpreter run. Here note tags are consumed in
   frontmatter order.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from organize_core.config import ConsumerConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    Status,
    register,
)
from organize_core.errors import ConfigError, ConsumerError, OrganizeError
from organize_core.frontmatter import Frontmatter
from organize_core.paths import default_env, expand

LOG = logging.getLogger("organize_core.consumers.taskwarrior")

#: Fibonacci importance scale shown to the LLM and snapped to on the way
#: back (spec 06 §3.1).
UTILITY_SCALE: tuple[int, ...] = (1, 2, 3, 5, 8, 13, 21)

IMPORTANCE_GUIDE = textwrap.dedent(
    """
    Importance (also called Utility) scale:
    - 1 (Trivially important): Example: Reading an article that could inspire future ideas.
    - 2 (Slightly important): Example: Organizing your digital files for easier access later.
    - 3 (Moderately important): Example: Learning a new keyboard shortcut to improve workflow.
    - 5 (Important): Example: Setting up regular backups to prevent future data loss.
    - 8 (Very important): Example: Investing time in professional development such as taking a course.
    - 13 (Critically important): Example: Building a habit of regular exercise for long-term health.
    - 21 (Extremely important): Example: Planning your financial future or retirement.
    Estimate importance as the difference between doing and not doing the task, considering opportunity
    cost and likely future scenarios.
    """
).strip()

#: Description cap (spec 06 §3.1: ``[:509] + "..."``).
DESCRIPTION_LIMIT = 512
_DESCRIPTION_KEEP = DESCRIPTION_LIMIT - 3

#: Canonical UDA field names for the four optional enrichment values.
DEFAULT_UDA_FIELDS: dict[str, str] = {
    "next_action": "next_action",
    "effort": "effort",
    "priority": "priority_estimate",
    "utility": "utility",
}

#: Options that existed in the old pipeline and are deliberately gone. Config
#: keys are honored or deleted, and deleted ones fail loudly naming the key
#: (spec 03 §1 / 08 §A35) — silently ignoring them is how the live config and
#: the code drifted apart in the first place.
REMOVED_OPTIONS: dict[str, str] = {
    "backup": (
        "the [consumers.<name>.backup] sub-table is flattened: use "
        "backup_enabled / backup_directory / backup_keep / backup_max_age_days"
    ),
    "llm": (
        "consumer-local LLM commands are gone: there is ONE shared client "
        "configured in [llm] (spec 09 §2). Use llm_enabled = true here and "
        "set llm.backend / llm.ollama_host / llm.ollama_model globally"
    ),
    "max_new_tasks_per_run": (
        "renamed to the framework key max_notes_per_run, which counts "
        "successes only and is enforced by the runner (spec 06 §1)"
    ),
}


class TaskCommandError(ConsumerError):
    """A ``task`` invocation failed. Carries the decoded streams so the
    per-note ERROR result and the log line can quote them."""

    def __init__(self, message: str, *, hint: str = "", stdout: str = "", stderr: str = "") -> None:
        super().__init__(message, hint=hint or None)
        self.message = message
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


def task_tag(value: Any) -> str:
    """Taskwarrior tag normalization (spec 06 §3.1): strip, whitespace → ``_``,
    lowercase. NOT ``frontmatter.normalize_tag`` — see the module docstring."""
    text = str(value).strip()
    if not text:
        return ""
    return re.sub(r"\s+", "_", text).lower()


def task_timestamp(source: Any = None, *, now: datetime | None = None) -> str:
    """Taskwarrior's ``YYYYMMDDTHHMMSSZ`` stamp.

    Parses ``source`` (frontmatter ``timestamp`` / ``created_date``) when it is
    an ISO-8601 string or a datetime; anything unparseable falls back to
    ``now``. A NAIVE input is read as UTC rather than as machine-local time —
    the old code's ``astimezone()`` made a task's ``entry`` depend on the
    timezone of whichever machine ran the pipeline.
    """
    dt: datetime | None = None
    if isinstance(source, datetime):
        dt = source
    elif source is not None:
        text = str(source).strip()
        if text:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                dt = None
    if dt is None:
        dt = now or datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_duration_to_hours(value: Any) -> float | None:
    """``1.5h`` / ``30 min`` / ``PT1H30M`` / bare hours → hours (06 §3.1)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        hours = float(value)
        if not math.isfinite(hours) or hours <= 0:
            return None
        return hours
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text.startswith("pt"):
        iso = _parse_iso_duration(text)
        if iso is not None:
            return iso
    matches = list(
        re.finditer(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\b", text)
    )
    if matches:
        total_minutes = 0.0
        for match in matches:
            amount = float(match.group(1))
            total_minutes += amount * 60 if match.group(2).startswith("h") else amount
        if total_minutes > 0:
            return total_minutes / 60
    compact = re.fullmatch(r"(\d+(?:\.\d+)?)(h|m|min)", text)
    if compact:
        amount = float(compact.group(1))
        return amount if compact.group(2).startswith("h") else amount / 60
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        hours = float(text)
        if hours > 0:
            return hours
    return None


def _parse_iso_duration(text: str) -> float | None:
    match = re.fullmatch(
        r"pt(?:(?P<hours>\d+(?:\.\d+)?)h)?"
        r"(?:(?P<minutes>\d+(?:\.\d+)?)m)?"
        r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?",
        text.lower(),
    )
    if not match:
        return None
    total = (
        float(match.group("hours") or 0)
        + float(match.group("minutes") or 0) / 60
        + float(match.group("seconds") or 0) / 3600
    )
    return total if total > 0 else None


def format_duration_from_hours(hours: float) -> str:
    """Canonical hour/minute notation (parity with the live pipeline)."""
    if hours >= 1:
        rounded = round(hours)
        if abs(hours - rounded) < 0.05:
            return f"{int(rounded)}h"
        return f"{round(hours, 1)}h"
    minutes = max(1, int(round(hours * 60)))
    if minutes > 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    if minutes % 5 != 0:
        minutes = int(round(minutes / 5) * 5)
    return f"{minutes} min"


def snap_utility(value: Any) -> int | None:
    """Snap a model's number to the Fibonacci scale (06 §3.1)."""
    score = _numeric(value)
    if score is None:
        return None
    return min(UTILITY_SCALE, key=lambda option: abs(option - score))


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            match = re.search(r"-?\d+(?:\.\d+)?", text)
            if match:
                try:
                    return float(match.group(0))
                except ValueError:
                    return None
    return None


def _note_tags(payload: NotePayload) -> list[str]:
    """Frontmatter tags, in file order, via the ONE shared coercion.

    Uses ``Frontmatter.get_list`` (scalar ⇒ one-element list, missing ⇒ [])
    rather than a hand-rolled re-parse (08 §B9/§B16). ``NotePayload.tags()``
    is not used because its normalization contract is not fixed and this
    consumer needs the RAW spellings before applying 06 §3.1's own rules.
    """
    return [str(tag) for tag in Frontmatter(fields=dict(payload.frontmatter)).get_list("tags")]


# `_note_is_no_ai` used to live here, rebuilding a `Document` just to reach
# `frontmatter.is_no_ai`. It is DELETED (ARCHITECTURE "Phase-4 rulings,
# auto_tagger batch", PHASE-5 CHECKLIST item (b), commit f24ee2e:
# "taskwarrior.py's redundant `_note_is_no_ai` delegating helper simplifies to
# payload.no_ai"). `NotePayload.no_ai` is the ONE no-ai rule — it delegates to
# `frontmatter.fields_are_no_ai`, which `is_no_ai(Document)` also calls
# (Phase-3 ruling: "no_ai MUST delegate to the frontmatter module's no-ai
# predicate — ONE rule in the codebase"). The wrapper was a second door onto
# the same rule and the last thing keeping `Document`/`is_no_ai` imported here.


# ---------------------------------------------------------------------------
# option parsing (pure; every failure names the key)
# ---------------------------------------------------------------------------


def _fail(name: str, key: str, message: str, hint: str = "") -> None:
    raise ConfigError(f"consumers.{name}.{key}: {message}", hint=hint or None)


def _opt_str(opts: dict[str, Any], name: str, key: str, default: str) -> str:
    value = opts.get(key, default)
    if value is None:
        return ""
    if not isinstance(value, str):
        _fail(name, key, f"must be a string, got {type(value).__name__}")
    return str(value).strip()


def _opt_optional_str(opts: dict[str, Any], name: str, key: str) -> str | None:
    value = opts.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(name, key, f"must be a string, got {type(value).__name__}")
    text = str(value).strip()
    return text or None


def _opt_bool(opts: dict[str, Any], name: str, key: str, default: bool) -> bool:
    value = opts.get(key, default)
    if not isinstance(value, bool):
        _fail(name, key, f"must be a boolean, got {type(value).__name__}")
    return bool(value)


def _opt_int(opts: dict[str, Any], name: str, key: str, default: int, *, minimum: int) -> int:
    value = opts.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(name, key, f"must be an integer, got {type(value).__name__}")
    number = int(value)
    if number < minimum:
        _fail(name, key, f"must be >= {minimum}, got {number}")
    return number


def _opt_float(opts: dict[str, Any], name: str, key: str, default: float) -> float:
    value = opts.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(name, key, f"must be a number, got {type(value).__name__}")
    number = float(value)
    if number <= 0:
        _fail(name, key, f"must be positive, got {number}")
    return number


def _opt_str_list(opts: dict[str, Any], name: str, key: str, default: list[str]) -> list[str]:
    value = opts.get(key, default)
    if not isinstance(value, list):
        _fail(name, key, f"must be an array of strings, got {type(value).__name__}")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            _fail(name, key, f"[{index}] must be a string, got {type(item).__name__}")
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _opt_uda_fields(opts: dict[str, Any], name: str) -> dict[str, str]:
    raw = opts.get("uda_fields", {})
    if not isinstance(raw, dict):
        _fail(name, "uda_fields", f"must be a table, got {type(raw).__name__}")
    fields = dict(DEFAULT_UDA_FIELDS)
    for key, value in raw.items():
        if key not in DEFAULT_UDA_FIELDS:
            _fail(
                name,
                f"uda_fields.{key}",
                "is not one of the four enrichment fields",
                hint="valid keys: " + ", ".join(sorted(DEFAULT_UDA_FIELDS)),
            )
        if not isinstance(value, str) or not value.strip():
            _fail(name, f"uda_fields.{key}", "must be a non-empty string")
        fields[key] = str(value).strip()
    return fields


#: Every option this consumer honors. Anything else is a loud ConfigError.
KNOWN_OPTIONS: frozenset[str] = frozenset(
    {
        "marker_tag",
        "strip_tags",
        "remove_unknown_tags",
        "project_tag_prefix",
        "default_project",
        "additional_tags",
        "review_tag",
        "annotation_template",
        "task_binary",
        "taskrc_path",
        "data_directory",
        "timeout_seconds",
        "backup_enabled",
        "backup_directory",
        "backup_keep",
        "backup_max_age_days",
        "llm_enabled",
        "llm_max_context_tasks",
        "llm_timeout_seconds",
        "max_note_body_chars",
        "uda_fields",
    }
)


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskPayload:
    """One ``task import`` JSON object (spec 06 §3.1): description, entry
    (note timestamp/created_date else now), tags, project, annotations
    (template placeholders ``{path} {relative_path} {id} {capture_id}``),
    and the four optional UDA enrichment fields.

    ``effort`` is a Taskwarrior duration STRING (``"45 min"``, ``"1.5h"``),
    not a float: the UDA is parsed by Taskwarrior's duration grammar, where a
    bare ``1.5`` means 1.5 *seconds*. (The scaffold annotated it ``float``;
    widening it here is the one change to this dataclass, and it stays inside
    this seat's own file.)
    """

    description: str
    entry: str
    tags: list[str] = field(default_factory=list)
    project: str = "Inbox"
    annotations: list[dict[str, str]] = field(default_factory=list)
    next_action: str | None = None
    effort: str | None = None
    priority_estimate: str | None = None
    utility: int | None = None

    def to_import_json(self, uda_fields: dict[str, str] | None = None) -> dict[str, Any]:
        """The exact object handed to ``task import``. Empty/None values are
        omitted so Taskwarrior never records blank UDAs."""
        names = uda_fields or DEFAULT_UDA_FIELDS
        out: dict[str, Any] = {"description": self.description, "entry": self.entry}
        if self.tags:
            out["tags"] = list(self.tags)
        if self.project:
            out["project"] = self.project
        if self.annotations:
            out["annotations"] = [dict(a) for a in self.annotations]
        for logical, value in (
            ("next_action", self.next_action),
            ("effort", self.effort),
            ("priority", self.priority_estimate),
            ("utility", self.utility),
        ):
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            out[names.get(logical, DEFAULT_UDA_FIELDS[logical])] = value
        return out


def _dedupe_keys(
    description: str, tags: list[str], project: str
) -> tuple[tuple[str, str], tuple[str, tuple[str, ...], str]]:
    """The two case-insensitive dedupe keys of 06 §3.1: ``(description,
    project)`` and ``(description, sorted-tags, project)``."""
    desc = description.strip().lower()
    proj = (project or "").strip().lower()
    canonical = tuple(sorted(str(tag).strip().lower() for tag in tags if str(tag).strip()))
    return (desc, proj), (desc, canonical, proj)


# ---------------------------------------------------------------------------
# consumer
# ---------------------------------------------------------------------------


@register("taskwarrior")
class TaskwarriorConsumer(Consumer):
    uses_llm = False  # enrichment is optional and guarded separately

    def wants_llm(self) -> bool:
        """This consumer needs ``ctx.llm`` iff ``llm_enabled`` is on.

        ``uses_llm`` must stay False — it is the runner's no-ai DENIAL flag,
        and a ``no-ai: true`` capture still has to become a task (the
        enrichment branch guards the vault law itself, in
        :meth:`enrich_with_llm`). But injection is a different question:
        keyed on the class flag alone, ``ctx.llm`` was always None, every
        run logged "llm_enabled but no LLM client is configured", and the
        06 §3.1 enrichment path could not execute through the pipeline at
        all — only through tests that hand-built a RunContext.
        """
        return bool(self.llm_enabled)

    def __init__(self, config: ConsumerConfig) -> None:
        super().__init__(config)
        opts = dict(config.options)
        name = config.name

        for key in sorted(opts):
            if key in REMOVED_OPTIONS:
                _fail(name, key, "is no longer a taskwarrior option", REMOVED_OPTIONS[key])
            if key not in KNOWN_OPTIONS:
                _fail(
                    name,
                    key,
                    "is not a taskwarrior consumer option",
                    hint="known options: " + ", ".join(sorted(KNOWN_OPTIONS)),
                )

        self.marker_tag = _opt_str(opts, name, "marker_tag", "todo")
        self.strip_tags = {
            task_tag(tag) for tag in _opt_str_list(opts, name, "strip_tags", []) if task_tag(tag)
        }
        if self.marker_tag:
            self.strip_tags.add(task_tag(self.marker_tag))
        self.remove_unknown_tags = _opt_bool(opts, name, "remove_unknown_tags", True)
        self.project_tag_prefix = _opt_str(opts, name, "project_tag_prefix", "project:")
        self.default_project = _opt_str(opts, name, "default_project", "Inbox")
        self.additional_tags = [
            task_tag(tag)
            for tag in _opt_str_list(opts, name, "additional_tags", ["para", "automation"])
            if task_tag(tag)
        ]
        review_tag = task_tag(_opt_str(opts, name, "review_tag", "not_reviewed"))
        self.review_tag = review_tag or None
        # Absent ⇒ the default-of-record (06 §2); present-but-empty ⇒ the
        # operator turned annotations off, which is not the same thing.
        if "annotation_template" in opts:
            self.annotation_template = _opt_optional_str(opts, name, "annotation_template")
        else:
            self.annotation_template = "Captured from {id}"

        self.task_binary = _opt_str(opts, name, "task_binary", "task") or "task"
        self.taskrc_path_option = _opt_optional_str(opts, name, "taskrc_path")
        self.data_directory_option = _opt_optional_str(opts, name, "data_directory")
        self.timeout_seconds = _opt_float(opts, name, "timeout_seconds", 30.0)

        self.backup_enabled = _opt_bool(opts, name, "backup_enabled", True)
        self.backup_directory_option = _opt_optional_str(opts, name, "backup_directory")
        self.backup_keep = _opt_int(opts, name, "backup_keep", 10, minimum=1)
        self.backup_max_age_days = _opt_int(opts, name, "backup_max_age_days", 30, minimum=1)

        self.llm_enabled = _opt_bool(opts, name, "llm_enabled", False)
        self.llm_max_context_tasks = _opt_int(opts, name, "llm_max_context_tasks", 6, minimum=1)
        self.llm_timeout_seconds = _opt_float(opts, name, "llm_timeout_seconds", 45.0)
        self.max_note_body_chars = _opt_int(opts, name, "max_note_body_chars", 1500, minimum=1)
        self.uda_fields = _opt_uda_fields(opts, name)

        self.reset_run()

    # --- per-run state ---------------------------------------------------

    def reset_run(self) -> None:
        """Drop everything fetched from Taskwarrior. Called from the (pure)
        constructor and available to callers that reuse an instance across
        runs — no I/O happens here (08 §B2)."""
        self._loaded = False
        self._load_error: TaskCommandError | None = None
        self._known_tags: set[str] = set()
        self._existing_tasks: list[dict[str, Any]] = []
        self._summary_keys: set[tuple[str, str]] = set()
        self._task_keys: set[tuple[str, tuple[str, ...], str]] = set()
        self._backed_up = False
        self._data_directory: Path | None = None
        self._taskrc_path: Path | None = None
        self._enrichment_checked = False
        self._enrichment_ok = True

    # --- framework hooks -------------------------------------------------

    def should_process(self, payload: NotePayload) -> bool:
        """Frontmatter tag == marker_tag, case-insensitive (06 §3.1)."""
        if not self.marker_tag:
            return True
        marker = self.marker_tag.strip().lower()
        return any(tag.strip().lower() == marker for tag in _note_tags(payload))

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        try:
            return self._handle(payload, ctx)
        except TaskCommandError as exc:
            LOG.error(
                "taskwarrior: %s for %s (stdout=%r stderr=%r)",
                exc.message,
                payload.path,
                exc.stdout[:500],
                exc.stderr[:500],
            )
            return ConsumerResult(
                status=Status.ERROR,
                message=str(exc),
                metadata={"stderr": exc.stderr[:2000], "note": str(payload.path)},
            )
        except OSError as exc:
            LOG.error("taskwarrior: I/O failure for %s: %s", payload.path, exc)
            return ConsumerResult(
                status=Status.ERROR,
                message=f"taskwarrior I/O failure: {exc}",
                metadata={"note": str(payload.path)},
            )
        except Exception as exc:  # noqa: BLE001 - see below
            # ONE malformed note must not take the consumer (and with it every
            # remaining note) down: spec 06 §1 says per-file failures are
            # logged and skipped, never abort the run, and B2 is the story of
            # what a single unhandled exception costs. ERROR is not
            # checkpointed, so the note is retried next run.
            LOG.exception("taskwarrior: unexpected failure for %s", payload.path)
            return ConsumerResult(
                status=Status.ERROR,
                message=f"taskwarrior failed unexpectedly: {exc!r}",
                metadata={"note": str(payload.path)},
            )

    # --- the work --------------------------------------------------------

    def _handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        if not self.should_process(payload):
            return ConsumerResult(status=Status.SKIP, message="marker tag missing")

        description = self.build_description(payload)
        if not description:
            return ConsumerResult(status=Status.SKIP, message="empty description")

        self._ensure_loaded()
        tags, project = self.build_tags(payload, self._known_tags)

        task = TaskPayload(
            description=description,
            entry=task_timestamp(
                payload.frontmatter.get("timestamp") or payload.frontmatter.get("created_date")
            ),
            tags=tags,
            project=project,
            annotations=self._build_annotations(payload, ctx),
        )
        summary_key, task_key = _dedupe_keys(task.description, task.tags, task.project)
        if summary_key in self._summary_keys or task_key in self._task_keys:
            return ConsumerResult(
                status=Status.SKIP,
                message="duplicate task",
                metadata={"description": description, "tags": tags, "project": project},
            )

        task = self.enrich_with_llm(task, payload, ctx)

        if ctx.dry_run:
            LOG.info("taskwarrior: dry-run, not importing %r", description)
            return ConsumerResult(
                status=Status.SUCCESS,
                message="dry-run: would create task",
                metadata={
                    "dry_run": True,
                    "description": description,
                    "tags": tags,
                    "project": project,
                    "task": task.to_import_json(self.uda_fields),
                },
            )

        self.backup_task_data(ctx)
        body = task.to_import_json(self.uda_fields)
        self._import_task(body)
        self._remember(task, body)

        metadata: dict[str, Any] = {
            "description": description,
            "tags": tags,
            "project": project,
        }
        enrichment = {key: body[name] for key, name in self.uda_fields.items() if name in body}
        if enrichment:
            metadata["llm_attributes"] = enrichment
        return ConsumerResult(status=Status.SUCCESS, message="task created", metadata=metadata)

    # --- steps (unit-test targets; signatures fixed) ---------------------

    def build_description(self, payload: NotePayload) -> str:
        """Body minus headings, one line, whitespace-collapsed, ≤512 chars
        (``[:509] + "..."``); fallback title → filename stem (06 §3.1)."""
        lines = [line.strip() for line in payload.content.splitlines()]
        kept = [line for line in lines if line and not line.startswith("#")]
        title_raw = payload.frontmatter.get("title")
        title = str(title_raw).strip() if title_raw not in (None, "") else ""
        if kept:
            candidate = " ".join(kept)
        elif title:
            candidate = title
        else:
            candidate = payload.path.stem
        candidate = " ".join(candidate.split())
        if len(candidate) > DESCRIPTION_LIMIT:
            candidate = candidate[:_DESCRIPTION_KEEP] + "..."
        return candidate.strip()

    def build_tags(
        self, payload: NotePayload, known_tags: set[str] | None
    ) -> tuple[list[str], str]:
        """(tags, project): normalize (strip, spaces→_, lowercase); drop
        strip_tags + marker; ``project:<x>`` prefix tag → project else
        default_project; add additional_tags + review_tag; apply
        remove_unknown_tags with the B7 whitelist, logging drops."""
        prefix = task_tag(self.project_tag_prefix) if self.project_tag_prefix else ""
        project = ""
        collected: list[str] = []
        seen: set[str] = set()
        for raw in _note_tags(payload):  # frontmatter order — deterministic
            tag = task_tag(raw)
            if not tag or tag in self.strip_tags:
                continue
            if prefix and tag.startswith(prefix):
                if not project:
                    project = tag[len(prefix) :].strip()
                continue
            if tag in seen:
                continue
            seen.add(tag)
            collected.append(tag)
        if not project and self.default_project:
            project = self.default_project

        for extra in self.additional_tags:
            if extra and extra not in seen:
                seen.add(extra)
                collected.append(extra)
        if self.review_tag and self.review_tag not in seen:
            seen.add(self.review_tag)
            collected.append(self.review_tag)

        final = sorted(collected)
        if self.remove_unknown_tags:
            # B7: the old code whitelisted ONLY review_tag, so every run
            # silently dropped "para"/"automation" until they happened to
            # exist — and dropping them changed the dedupe key too.
            whitelist = {tag for tag in self.additional_tags if tag}
            if self.review_tag:
                whitelist.add(self.review_tag)
            known = {str(tag).strip().lower() for tag in (known_tags or set())}
            kept: list[str] = []
            for tag in final:
                if tag in whitelist or tag in known:
                    kept.append(tag)
                else:
                    LOG.warning(
                        "taskwarrior: dropping unknown tag %r for %s (remove_unknown_tags=true)",
                        tag,
                        payload.path,
                    )
            final = kept
        return final, project

    def is_duplicate(self, task: TaskPayload, existing_export: list[dict[str, Any]]) -> bool:
        """Dedupe against full export incl. completed on (description,
        project) and (description, sorted-tags, project), case-insensitive."""
        summary_keys, task_keys = self._index_export(existing_export)
        summary, full = _dedupe_keys(task.description, task.tags, task.project)
        return summary in summary_keys or full in task_keys

    def enrich_with_llm(
        self, task: TaskPayload, payload: NotePayload, ctx: RunContext
    ) -> TaskPayload:
        """Optional enrichment (06 §3.1): Fibonacci IMPORTANCE_GUIDE prompt,
        few-shot from Matt's export, single JSON object out; effort strings
        normalized (1.5h / 30 min / PT1H30M / bare hours); utility snapped
        to the scale. Failure ⇒ return task unchanged."""
        if not self.llm_enabled:
            return task
        # ``uses_llm`` is False (the task itself is created for every note),
        # so the runner's central guard does not cover this branch — the
        # prompt-building path is guarded here instead (spec 02 vault law,
        # ARCHITECTURE resolution #12's reasoning). ``payload.no_ai`` is the
        # ONE shared rule (Phase-5 checklist item (b), f24ee2e).
        if payload.no_ai:
            LOG.info("taskwarrior: no-ai note, skipping LLM enrichment for %s", payload.path)
            return task
        if ctx.llm is None:
            LOG.warning(
                "taskwarrior: llm_enabled but no LLM client is configured; "
                "creating %s without enrichment",
                payload.path,
            )
            return task
        if not self._enrichment_supported():
            return task

        prompt = self.build_llm_prompt(task, payload)
        try:
            response = ctx.llm.generate(
                prompt,
                json_mode=True,
                temperature=0.3,
                timeout_seconds=self.llm_timeout_seconds,
            )
        except (OrganizeError, OSError, TimeoutError, ValueError) as exc:
            LOG.warning("taskwarrior: LLM enrichment failed for %s: %s", payload.path, exc)
            return task

        raw = response.json
        if not isinstance(raw, dict):
            try:
                parsed = json.loads(response.text)
            except json.JSONDecodeError:
                parsed = None
            raw = parsed if isinstance(parsed, dict) else None
        if not raw:
            LOG.warning("taskwarrior: LLM returned no usable JSON for %s", payload.path)
            return task

        attributes = self.normalize_llm_attributes(raw)
        if not attributes:
            return task
        return replace(
            task,
            next_action=attributes.get("next_action", task.next_action),
            effort=attributes.get("effort", task.effort),
            priority_estimate=attributes.get("priority", task.priority_estimate),
            utility=attributes.get("utility", task.utility),
        )

    def backup_task_data(self, ctx: RunContext) -> None:
        """Pre-import snapshot with retention (B6). Once per run."""
        if self._backed_up or not self.backup_enabled:
            return
        root = self._backup_root(ctx)
        if root is None:
            raise TaskCommandError(
                "taskwarrior backup directory cannot be resolved",
                hint=(
                    "set consumers.<name>.backup_directory to an absolute path, "
                    "or disable with backup_enabled = false (spec 06 §3.1)"
                ),
            )
        source = self._resolve_data_directory()
        if not source.is_dir():
            raise TaskCommandError(
                f"Taskwarrior data directory not found: {source}",
                hint="set consumers.<name>.data_directory (spec 06 §3.1)",
            )
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = root / stamp
        counter = 1
        while target.exists():
            counter += 1
            target = root / f"{stamp}-{counter}"
        shutil.copytree(source, target)
        self._backed_up = True
        LOG.info("taskwarrior: backed up %s to %s", source, target)
        self.prune_backups(root)

    def prune_backups(self, root: Path) -> list[Path]:
        """Retention (B6 — today: 43 snapshots, 108 MB, unbounded): keep the
        newest ``backup_keep`` and nothing older than ``backup_max_age_days``.
        The newest snapshot is ALWAYS kept, so a long-idle pipeline can never
        prune itself down to zero backups. Returns what was removed."""
        try:
            entries = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
        except OSError as exc:
            LOG.warning("taskwarrior: cannot list backups in %s: %s", root, exc)
            return []
        newest_first = list(reversed(entries))
        cutoff = datetime.now(UTC) - timedelta(days=self.backup_max_age_days)
        removed: list[Path] = []
        for index, path in enumerate(newest_first):
            if index == 0:
                continue
            taken_at = _backup_timestamp(path)
            too_many = index >= self.backup_keep
            too_old = taken_at is not None and taken_at < cutoff
            if not (too_many or too_old):
                continue
            try:
                shutil.rmtree(path)
            except OSError as exc:
                LOG.warning("taskwarrior: cannot prune backup %s: %s", path, exc)
                continue
            removed.append(path)
            LOG.info("taskwarrior: pruned backup %s", path)
        return removed

    # --- LLM prompt ------------------------------------------------------

    def build_llm_prompt(self, task: TaskPayload, payload: NotePayload) -> str:
        """Preamble + Fibonacci IMPORTANCE_GUIDE + few-shot exemplars mined
        from the real task export + task fields + frontmatter JSON + a body
        excerpt, demanding a single JSON object (06 §3.1)."""
        sections: list[str] = [
            "You are assisting with enriching Taskwarrior tasks for a personal "
            "productivity system.",
            "For the task below, provide estimates for:",
            '- "next_action": the very next physical action to advance the task.',
            '- "effort": time estimate using hour/minute notation (examples: "30 min", '
            '"1h", "1.5h").',
            '- "priority": qualitative priority (examples: "low", "medium", "high").',
            '- "utility": numeric importance using the Fibonacci-style scale.',
            "",
            "Importance guidance:",
            textwrap.indent(IMPORTANCE_GUIDE, "  "),
            "",
        ]
        sections += self._example_section("Effort", self._effort_examples())
        sections += self._example_section("Priority", self._priority_examples())
        sections += self._example_section("Utility", self._utility_examples())

        try:
            frontmatter_repr = json.dumps(payload.frontmatter, indent=2, sort_keys=True, default=str)
        except (TypeError, ValueError):
            frontmatter_repr = "\n".join(
                f"{key}: {value}" for key, value in sorted(payload.frontmatter.items())
            )
        body = _truncate(payload.content, self.max_note_body_chars) or "[no additional body]"
        sections += [
            "Task to assess:",
            f"  Description: {task.description}",
            f"  Project: {task.project or 'none'}",
            f"  Tags: {', '.join(task.tags) if task.tags else 'none'}",
            "  Frontmatter:",
            textwrap.indent(frontmatter_repr or "{}", "    "),
            "  Note body:",
            textwrap.indent(body, "    "),
            "",
            "Respond with a single JSON object exactly in this form:",
            textwrap.indent(
                '{\n'
                '  "next_action": "string",\n'
                '  "effort": "time string",\n'
                '  "priority": "string",\n'
                '  "utility": number\n'
                "}",
                "  ",
            ),
            "No additional commentary is allowed before or after the JSON.",
        ]
        return "\n".join(sections)

    def normalize_llm_attributes(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Model output → the four canonical values (06 §3.1)."""
        lowered = {
            key.lower().replace("-", "_"): value
            for key, value in raw.items()
            if isinstance(key, str)
        }

        def pick(*names: str) -> Any:
            for candidate in names:
                if candidate in lowered:
                    return lowered[candidate]
            return None

        out: dict[str, Any] = {}
        next_action = pick("next_action", "nextaction")
        if next_action is not None and str(next_action).strip():
            out["next_action"] = str(next_action).strip()

        effort = pick("effort", "effort_estimate", "estimated_effort")
        if effort is not None:
            hours = parse_duration_to_hours(effort)
            if hours is not None:
                out["effort"] = format_duration_from_hours(hours)
            elif str(effort).strip():
                out["effort"] = str(effort).strip()

        priority = pick("priority", "priority_estimate")
        if priority is not None and str(priority).strip():
            out["priority"] = str(priority).strip()

        utility = pick("utility", "importance")
        if utility is not None:
            snapped = snap_utility(utility)
            if snapped is not None:
                out["utility"] = snapped
        return out

    # --- Taskwarrior I/O -------------------------------------------------

    def _run_task(
        self, args: list[str], *, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Every ``task`` invocation goes through here.

        B1 — the 3-month outage — was a strict-UTF-8 decode of ``task
        export`` plus no timeout. Both are structural here: ``encoding``,
        ``errors="replace"`` and ``timeout`` are not optional kwargs a caller
        can forget.
        """
        command = [
            self.task_binary,
            f"rc.data.location={self._resolve_data_directory()}",
            "rc.confirmation=no",
            "rc.hooks=off",
            *args,
        ]
        env = default_env()
        taskrc = self._resolve_taskrc_path()
        if taskrc is not None:
            env["TASKRC"] = str(taskrc)
        try:
            proc = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TaskCommandError(
                f"Taskwarrior binary not found: {self.task_binary!r}",
                hint="install taskwarrior or set consumers.<name>.task_binary",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TaskCommandError(
                f"`{' '.join(command)}` timed out after {self.timeout_seconds}s",
                hint="raise consumers.<name>.timeout_seconds if the task DB is huge",
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
            ) from exc
        if proc.returncode != 0:
            raise TaskCommandError(
                f"`{' '.join(command)}` exited {proc.returncode}",
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
            )
        return proc

    def _ensure_loaded(self) -> None:
        """Fetch ``task _tags`` + ``task export`` ONCE per run, lazily — the
        constructor stays pure (08 §B2). A failure is cached so the remaining
        notes fail fast instead of re-shelling out per note."""
        if self._load_error is not None:
            raise self._load_error
        if self._loaded:
            return
        try:
            self._known_tags = self._load_known_tags()
            self._existing_tasks = self._load_existing_tasks()
        except TaskCommandError as exc:
            self._load_error = exc
            raise
        if self.review_tag:
            self._known_tags.add(self.review_tag)
        self._summary_keys, self._task_keys = self._index_export(self._existing_tasks)
        self._loaded = True

    def _load_known_tags(self) -> set[str]:
        proc = self._run_task(["_tags"])
        return {line.strip().lower() for line in proc.stdout.splitlines() if line.strip()}

    def _load_existing_tasks(self) -> list[dict[str, Any]]:
        proc = self._run_task(["rc.json.array=1", "export"])
        text = (proc.stdout or "").strip() or "[]"
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TaskCommandError(
                "Taskwarrior export is not valid JSON",
                hint=(
                    "the export was decoded with errors='replace' (08 §B1), so this is a "
                    "real syntax problem, not an encoding crash; run `task export` by hand"
                ),
                stdout=text[:2000],
                stderr=proc.stderr or "",
            ) from exc
        if not isinstance(raw, list):
            raise TaskCommandError(
                f"Taskwarrior export returned {type(raw).__name__}, expected a list",
                stdout=text[:2000],
            )
        return [entry for entry in raw if isinstance(entry, dict)]

    def _import_task(self, body: dict[str, Any]) -> None:
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", errors="replace", suffix=".json", delete=False
        )
        try:
            json.dump([body], handle, ensure_ascii=False)
            handle.flush()
        finally:
            handle.close()
        tmp_path = Path(handle.name)
        try:
            self._run_task(["import", str(tmp_path)])
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def _remember(self, task: TaskPayload, body: dict[str, Any]) -> None:
        """Fold a just-created task into the in-memory export so a second
        note in the SAME run cannot create a duplicate."""
        self._existing_tasks.append(dict(body))
        summary, full = _dedupe_keys(task.description, task.tags, task.project)
        self._summary_keys.add(summary)
        self._task_keys.add(full)
        for tag in task.tags:
            self._known_tags.add(tag.strip().lower())

    # --- resolution helpers ---------------------------------------------

    def _resolve_taskrc_path(self) -> Path | None:
        if self._taskrc_path is not None:
            return self._taskrc_path
        if self.taskrc_path_option:
            self._taskrc_path = expand(self.taskrc_path_option)
            return self._taskrc_path
        return None

    def _resolve_data_directory(self) -> Path:
        if self._data_directory is not None:
            return self._data_directory
        if self.data_directory_option:
            self._data_directory = expand(self.data_directory_option)
            return self._data_directory
        from_taskrc = self._data_location_from_taskrc()
        self._data_directory = from_taskrc or expand("~/.task")
        return self._data_directory

    def _data_location_from_taskrc(self) -> Path | None:
        taskrc = self._resolve_taskrc_path()
        if taskrc is None or not taskrc.is_file():
            return None
        for line in _read_text(taskrc).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip().lower() != "data.location":
                continue
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            if value:
                return expand(value)
        return None

    def _backup_root(self, ctx: RunContext) -> Path | None:
        if self.backup_directory_option:
            return expand(self.backup_directory_option)
        # ``RunContext`` does not carry CorePaths yet (seam request filed);
        # honor it the moment the orchestrator supplies one.
        base = getattr(getattr(ctx, "paths", None), "backups_dir", None)
        if base is not None:
            return Path(base) / "taskwarrior"
        return None

    def _build_annotations(self, payload: NotePayload, ctx: RunContext) -> list[dict[str, str]]:
        """``annotation_template`` rendered with ``{path} {relative_path}
        {id} {capture_id}`` (06 §3.1). ``relative_path`` is the vault-relative
        wiki-link target, so the task points back at the capture."""
        template = self.annotation_template
        if not template:
            return []
        path = payload.path
        relative = path
        root = getattr(getattr(ctx.config, "vault", None), "root", None)
        if root is not None:
            try:
                relative = path.relative_to(Path(root))
            except ValueError:
                relative = path
        context = {
            "path": str(path),
            "relative_path": str(relative),
            "id": str(payload.frontmatter.get("id") or ""),
            "capture_id": str(payload.frontmatter.get("capture_id") or ""),
            "wikilink": f"[[{relative.with_suffix('').as_posix()}]]",
        }
        try:
            text = template.format(**context)
        except (KeyError, IndexError, ValueError):
            LOG.warning(
                "taskwarrior: annotation_template %r has an unknown placeholder; "
                "using it verbatim",
                template,
            )
            text = template
        text = text.strip()
        if not text:
            return []
        return [{"entry": task_timestamp(), "description": text}]

    def _index_export(
        self, tasks: list[dict[str, Any]]
    ) -> tuple[set[tuple[str, str]], set[tuple[str, tuple[str, ...], str]]]:
        summaries: set[tuple[str, str]] = set()
        keys: set[tuple[str, tuple[str, ...], str]] = set()
        for entry in tasks:
            description = str(entry.get("description", "") or "")
            raw_tags = entry.get("tags") or []
            tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else [str(raw_tags)]
            project = str(entry.get("project", "") or "")
            summary, full = _dedupe_keys(description, tags, project)
            summaries.add(summary)
            keys.add(full)
        return summaries, keys

    def _enrichment_supported(self) -> bool:
        """The four enrichment fields are UDAs; importing them when the
        ``.taskrc`` does not declare them makes Taskwarrior reject the import.
        Health-check once per run and degrade to no enrichment (06 §3.1)."""
        if self._enrichment_checked:
            return self._enrichment_ok
        self._enrichment_checked = True
        taskrc = self._resolve_taskrc_path()
        if taskrc is None or not taskrc.is_file():
            # Nothing to check against — trust the operator rather than
            # silently disabling a feature they turned on.
            self._enrichment_ok = True
            return True
        text = _read_text(taskrc)
        missing = [
            name
            for name in sorted(set(self.uda_fields.values()))
            if f"uda.{name}." not in text
        ]
        if missing:
            LOG.warning(
                "taskwarrior: %s does not declare UDA(s) %s; creating tasks without "
                "LLM enrichment this run (spec 06 §3.1)",
                taskrc,
                ", ".join(missing),
            )
            self._enrichment_ok = False
        return self._enrichment_ok

    # --- few-shot exemplars ---------------------------------------------

    def _example_section(self, label: str, rows: list[str]) -> list[str]:
        if not rows:
            return [f"{label} reference tasks: no comparable entries available.", ""]
        return [f"{label} reference tasks:", *rows, ""]

    def _effort_examples(self) -> list[str]:
        field_name = self.uda_fields["effort"]
        entries: list[tuple[float, str, str]] = []
        for task in self._existing_tasks:
            value = _first_present(task, (field_name, "effort"))
            if value is None:
                continue
            hours = parse_duration_to_hours(value)
            if hours is None:
                continue
            entries.append((hours, str(value), str(task.get("description", "")).strip()))
        entries.sort(key=lambda item: item[0])
        rows: list[str] = []
        for hours, value, description in _sample(entries, self.llm_max_context_tasks):
            canonical = format_duration_from_hours(hours)
            label = value if canonical == value else f"{value} (~{canonical})"
            rows.append(f"- {label}: {description or '[no description]'}")
        return rows

    def _utility_examples(self) -> list[str]:
        field_name = self.uda_fields["utility"]
        entries: list[tuple[float, str]] = []
        for task in self._existing_tasks:
            value = _first_present(task, (field_name, "utility", "importance"))
            if value is None:
                continue
            score = _numeric(value)
            if score is None:
                continue
            entries.append((score, str(task.get("description", "")).strip()))
        entries.sort(key=lambda item: item[0])
        return [
            f"- {int(round(score))}: {description or '[no description]'}"
            for score, description in _sample(entries, self.llm_max_context_tasks)
        ]

    def _priority_examples(self) -> list[str]:
        field_name = self.uda_fields["priority"]
        rows: list[str] = []
        seen: set[str] = set()
        for task in self._existing_tasks:
            value = _first_present(task, (field_name, "priority"))
            if value is None:
                continue
            text = str(value).strip()
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            rows.append(f"- {text}: {str(task.get('description', '')).strip() or '[no description]'}")
            if len(rows) >= self.llm_max_context_tasks:
                break
        return rows


# ---------------------------------------------------------------------------
# module-private helpers
# ---------------------------------------------------------------------------


def _first_present(task: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name and name in task:
            return task[name]
    return None


def _sample(entries: list[Any], limit: int) -> list[Any]:
    """Evenly-spaced sample so the exemplars span the whole range."""
    if not entries or limit <= 0:
        return []
    if len(entries) <= limit:
        return list(entries)
    if limit == 1:
        return [entries[len(entries) // 2]]
    step = (len(entries) - 1) / (limit - 1)
    indexes: list[int] = []
    for index in range(limit):
        candidate = int(round(index * step))
        if indexes and candidate <= indexes[-1]:
            candidate = min(len(entries) - 1, indexes[-1] + 1)
        indexes.append(candidate)
    return [entries[i] for i in indexes]


def _truncate(content: str, limit: int) -> str:
    text = content.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _as_text(value: Any) -> str:
    """TimeoutExpired streams can be bytes, str or None — B1 says decode is
    never strict."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        LOG.warning("taskwarrior: cannot read %s: %s", path, exc)
        return ""


def _backup_timestamp(path: Path) -> datetime | None:
    """Snapshot timestamp from the directory NAME (``<UTC-ts>`` per 06 §3.1),
    falling back to mtime; None when neither is readable."""
    stem = path.name.split("-")[0]
    try:
        return datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


__all__ = [
    "DEFAULT_UDA_FIELDS",
    "DESCRIPTION_LIMIT",
    "IMPORTANCE_GUIDE",
    "KNOWN_OPTIONS",
    "REMOVED_OPTIONS",
    "UTILITY_SCALE",
    "TaskCommandError",
    "TaskPayload",
    "TaskwarriorConsumer",
    "format_duration_from_hours",
    "parse_duration_to_hours",
    "snap_utility",
    "task_tag",
    "task_timestamp",
]
