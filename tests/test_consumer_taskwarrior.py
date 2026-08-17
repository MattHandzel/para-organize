"""Tests for the ``taskwarrior`` consumer (spec 06 §3.1; 08 §B1/§B6/§B7).

Isolation rules for this suite:

* Taskwarrior is a **fake executable script** written into ``tmp_path``
  (:func:`fake_task`) that logs every invocation and keeps its own task
  database as JSON. The real ``task`` binary and Matt's real ``~/.task`` are
  never touched.
* The LLM is the local stdlib HTTP server / fake-CLI pattern from
  ``tests/test_llm.py`` — never a real Ollama host.
* Vault content comes from the fixture vault (``tests/conftest.py``); state
  paths come from tmp.

Assertions are exact values and real behavior (spec 09 §3).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from organize_core import frontmatter as fm
from organize_core.config import Config, ConsumerConfig, LLMConfig, VaultConfig
from organize_core.consumers.base import NotePayload, RunContext, Status
from organize_core.consumers.taskwarrior import (
    DEFAULT_UDA_FIELDS,
    KNOWN_OPTIONS,
    UTILITY_SCALE,
    TaskPayload,
    TaskwarriorConsumer,
    format_duration_from_hours,
    parse_duration_to_hours,
    snap_utility,
    task_tag,
    task_timestamp,
)
from organize_core.errors import ConfigError, LLMError
from organize_core.llm import LLMClient, LLMResponse

# ---------------------------------------------------------------------------
# Fake `task` binary
# ---------------------------------------------------------------------------

_FAKE_TASK = r'''#!%(python)s
"""Fake Taskwarrior. Logs invocations; keeps its DB in tasks.json."""
import base64
import json
import os
import sys
import time

STATE = %(state)r


def _path(name):
    return os.path.join(STATE, name)


def _load(name, default):
    try:
        with open(_path(name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


cfg = _load("config.json", {})
argv = sys.argv[1:]

with open(_path("invocations.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")

sub = None
for arg in argv:
    if not arg.startswith("rc"):
        sub = arg
        break

delay = float(cfg.get("delay") or 0)
if delay:
    time.sleep(delay)


def emit(key, fallback_text):
    raw = cfg.get(key)
    payload = base64.b64decode(raw) if raw is not None else fallback_text.encode("utf-8")
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


if sub == "_tags":
    emit("tags_stdout_b64", "".join(str(t) + "\n" for t in cfg.get("tags", [])))
    sys.exit(int(cfg.get("tags_exit") or 0))

if sub == "export":
    emit("export_stdout_b64", json.dumps(_load("tasks.json", [])))
    sys.exit(int(cfg.get("export_exit") or 0))

if sub == "import":
    code = int(cfg.get("import_exit") or 0)
    with open(argv[-1], "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    with open(_path("imports.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")
    if code == 0:
        tasks = _load("tasks.json", [])
        tasks.extend(payload)
        with open(_path("tasks.json"), "w", encoding="utf-8") as fh:
            json.dump(tasks, fh)
    sys.stderr.write(str(cfg.get("import_stderr") or ""))
    sys.stdout.write("Imported " + str(len(payload)) + " tasks\n")
    sys.exit(code)

sys.stderr.write("unknown subcommand: " + repr(sub) + "\n")
sys.exit(2)
'''


@dataclass
class FakeTask:
    """Handle on the fake binary: its argv log, its DB, and its knobs."""

    state: Path
    binary: Path
    data_dir: Path

    # --- knobs ---
    def configure(self, **options: Any) -> None:
        path = self.state / "config.json"
        current: dict[str, Any] = {}
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
        current.update(options)
        path.write_text(json.dumps(current), encoding="utf-8")

    def set_tasks(self, tasks: list[dict[str, Any]]) -> None:
        (self.state / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")

    def set_known_tags(self, tags: list[str]) -> None:
        self.configure(tags=list(tags))

    def set_export_bytes(self, raw: bytes) -> None:
        self.configure(export_stdout_b64=base64.b64encode(raw).decode("ascii"))

    def set_tags_bytes(self, raw: bytes) -> None:
        self.configure(tags_stdout_b64=base64.b64encode(raw).decode("ascii"))

    # --- observations ---
    @property
    def invocations(self) -> list[list[str]]:
        path = self.state / "invocations.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line
        ]

    def subcommands(self) -> list[str]:
        out: list[str] = []
        for argv in self.invocations:
            for arg in argv:
                if not arg.startswith("rc"):
                    out.append(arg)
                    break
        return out

    @property
    def imports(self) -> list[dict[str, Any]]:
        """Every task object handed to ``task import``, flattened."""
        path = self.state / "imports.jsonl"
        if not path.exists():
            return []
        flat: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                flat.extend(json.loads(line))
        return flat

    def reset_log(self) -> None:
        for name in ("invocations.jsonl", "imports.jsonl"):
            path = self.state / name
            if path.exists():
                path.unlink()


@pytest.fixture()
def fake_task(tmp_path: Path) -> FakeTask:
    state = tmp_path / "faketask"
    state.mkdir()
    data_dir = tmp_path / "taskdata"
    data_dir.mkdir()
    (data_dir / "pending.data").write_text("[]\n", encoding="utf-8")
    binary = tmp_path / "fake-task"
    binary.write_text(
        _FAKE_TASK % {"python": sys.executable, "state": str(state)}, encoding="utf-8"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    handle = FakeTask(state=state, binary=binary, data_dir=data_dir)
    handle.set_tasks([])
    handle.set_known_tags(["para", "automation", "not_reviewed", "blog"])
    return handle


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def make_consumer(fake: FakeTask, tmp_path: Path, **options: Any) -> TaskwarriorConsumer:
    base: dict[str, Any] = {
        "task_binary": str(fake.binary),
        "data_directory": str(fake.data_dir),
        "backup_directory": str(tmp_path / "state" / "backups" / "taskwarrior"),
        "timeout_seconds": 20.0,
    }
    base.update(options)
    return TaskwarriorConsumer(
        ConsumerConfig(
            name="taskwarrior",
            type="taskwarrior",
            include_paths=["capture/raw_capture"],
            options=base,
        )
    )


def make_context(vault_root: Path, *, dry_run: bool = False, llm: LLMClient | None = None) -> RunContext:
    return RunContext(config=Config(vault=VaultConfig(root=vault_root)), dry_run=dry_run, llm=llm)


def payload_from_file(path: Path) -> NotePayload:
    raw = path.read_text(encoding="utf-8", errors="replace")
    doc = fm.parse(raw)
    fields = dict(doc.frontmatter.fields) if doc.frontmatter else {}
    return NotePayload(
        path=path,
        frontmatter=fields,
        content=doc.body,
        raw_text=raw,
        note_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def write_capture(vault: Path, name: str, body: str, *, tags: list[str], note_id: str) -> Path:
    tag_block = "\n".join(f"- {tag!r}" for tag in tags)
    path = vault / "capture" / "raw_capture" / name
    path.write_text(
        f"---\ntimestamp: '2026-07-01T09:00:00.000000+00:00'\n"
        f"id: '{note_id}'\ntags:\n{tag_block}\nsources:\n- me\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def payload(
    vault: Path,
    *,
    body: str = "Draft the post about learning systems.",
    tags: list[str] | None = None,
    name: str = "note.md",
    note_id: str = "2026-07-01T09:00:00.000Z",
) -> NotePayload:
    path = write_capture(
        vault, name, body, tags=["todo"] if tags is None else tags, note_id=note_id
    )
    return payload_from_file(path)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_task_tag_normalization_is_taskwarrior_domain() -> None:
    # spaces → underscore, NOT hyphen: `not_reviewed` must survive verbatim.
    assert task_tag("  Deep Work ") == "deep_work"
    assert task_tag("not_reviewed") == "not_reviewed"
    assert task_tag("Project:Blog") == "project:blog"
    assert task_tag("   ") == ""


@pytest.mark.parametrize(
    ("value", "hours"),
    [
        ("1.5h", 1.5),
        ("30 min", 0.5),
        ("PT1H30M", 1.5),
        ("2", 2.0),
        ("1h 15min", 1.25),
        (0.25, 0.25),
        ("", None),
        ("soonish", None),
        (0, None),
        (True, None),
    ],
)
def test_parse_duration_to_hours(value: Any, hours: float | None) -> None:
    assert parse_duration_to_hours(value) == hours


@pytest.mark.parametrize(
    ("hours", "text"),
    [(1.0, "1h"), (1.5, "1.5h"), (0.5, "30 min"), (0.75, "45 min"), (2.0, "2h")],
)
def test_format_duration_from_hours(hours: float, text: str) -> None:
    assert format_duration_from_hours(hours) == text


@pytest.mark.parametrize(
    ("value", "snapped"),
    [(1, 1), (4, 3), (6, 5), (9, 8), (100, 21), ("8", 8), ("about 13", 13), ("none", None)],
)
def test_snap_utility_uses_the_fibonacci_scale(value: Any, snapped: int | None) -> None:
    assert snap_utility(value) == snapped
    if snapped is not None:
        assert snapped in UTILITY_SCALE


def test_task_timestamp_reads_naive_values_as_utc() -> None:
    # Deterministic regardless of the machine's timezone.
    assert task_timestamp("2026-06-10") == "20260610T000000Z"
    assert task_timestamp("2026-06-10T21:33:05.379Z") == "20260610T213305Z"
    assert task_timestamp("2026-06-10T21:33:05+02:00") == "20260610T193305Z"
    fixed = datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert task_timestamp("not a date", now=fixed) == "20300102T030405Z"


def test_task_payload_omits_empty_fields() -> None:
    task = TaskPayload(description="d", entry="20260701T090000Z", project="")
    assert task.to_import_json() == {"description": "d", "entry": "20260701T090000Z"}
    # the dataclass default project is the 06 §2 default-of-record
    assert TaskPayload(description="d", entry="e").to_import_json()["project"] == "Inbox"


def test_task_payload_renames_uda_fields() -> None:
    task = TaskPayload(
        description="d",
        entry="20260701T090000Z",
        next_action="call",
        effort="45 min",
        priority_estimate="high",
        utility=8,
    )
    body = task.to_import_json({**DEFAULT_UDA_FIELDS, "utility": "importance"})
    assert body["next_action"] == "call"
    assert body["effort"] == "45 min"
    assert body["priority_estimate"] == "high"
    assert body["importance"] == 8
    assert "utility" not in body


# ---------------------------------------------------------------------------
# Constructor purity + option schema (08 §B2, spec 03 §1)
# ---------------------------------------------------------------------------


def test_constructor_does_no_io(
    fake_task: FakeTask, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: constructors are pure. One bad consumer killed all four because
    ``__init__`` shelled out to Taskwarrior."""

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("constructor must not run a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    consumer = make_consumer(fake_task, tmp_path)
    assert consumer.marker_tag == "todo"
    assert fake_task.invocations == []


def test_constructor_does_not_require_the_data_directory_to_exist(
    fake_task: FakeTask, tmp_path: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path, data_directory=str(tmp_path / "nope"))
    assert consumer.data_directory_option.endswith("nope")


def test_unknown_option_is_a_loud_config_error(fake_task: FakeTask, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_consumer(fake_task, tmp_path, marker_tags=["todo"])
    assert "consumers.taskwarrior.marker_tags" in str(excinfo.value)


def test_removed_option_names_its_replacement(fake_task: FakeTask, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_consumer(fake_task, tmp_path, max_new_tasks_per_run=10)
    assert "max_new_tasks_per_run" in str(excinfo.value)
    assert "max_notes_per_run" in (excinfo.value.hint or "")


def test_removed_llm_subtable_points_at_the_shared_client(
    fake_task: FakeTask, tmp_path: Path
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_consumer(fake_task, tmp_path, llm={"enabled": True})
    assert "[llm]" in (excinfo.value.hint or "")


def test_wrong_option_type_names_the_key(fake_task: FakeTask, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        make_consumer(fake_task, tmp_path, backup_keep="ten")
    assert "consumers.taskwarrior.backup_keep" in str(excinfo.value)


def test_the_shipped_example_config_constructs_this_consumer() -> None:
    """06 §2 ships ONE example file; every key it sets for this consumer must
    be honored here, or the example is a lie the day it is deployed."""
    import tomllib

    from organize_core.config import example_config_toml, validate_config

    config = validate_config(tomllib.loads(example_config_toml()), source="example")
    section = next(c for c in config.consumers if c.type == "taskwarrior")
    consumer = TaskwarriorConsumer(section)
    assert consumer.marker_tag == "todo"
    assert consumer.default_project == "Inbox"
    assert set(section.options) <= set(KNOWN_OPTIONS)
    assert consumer.llm_enabled is False


def test_defaults_of_record_match_spec_06_section_2(fake_task: FakeTask, tmp_path: Path) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    assert consumer.marker_tag == "todo"
    assert consumer.default_project == "Inbox"
    assert consumer.additional_tags == ["para", "automation"]
    assert consumer.review_tag == "not_reviewed"
    assert consumer.annotation_template == "Captured from {id}"
    assert consumer.llm_enabled is False


# ---------------------------------------------------------------------------
# should_process (06 §3.1 trigger)
# ---------------------------------------------------------------------------


def test_should_process_matches_marker_tag_case_insensitively(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    assert consumer.should_process(payload(fixture_vault, tags=["TODO"], name="a.md")) is True
    assert consumer.should_process(payload(fixture_vault, tags=[" todo "], name="b.md")) is True
    assert consumer.should_process(payload(fixture_vault, tags=["todos"], name="c.md")) is False
    assert consumer.should_process(payload(fixture_vault, tags=[], name="d.md")) is False


def test_should_process_reads_scalar_tags_through_the_shared_coercion(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    path = fixture_vault / "capture" / "raw_capture" / "scalar.md"
    path.write_text("---\ntags: todo\n---\nBuy milk\n", encoding="utf-8")
    consumer = make_consumer(fake_task, tmp_path)
    assert consumer.should_process(payload_from_file(path)) is True


def test_should_process_never_shells_out(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """Filter evaluation is cheap and re-run every note (06 §1 / 08 §B4)."""
    consumer = make_consumer(fake_task, tmp_path)
    for index in range(5):
        consumer.should_process(payload(fixture_vault, name=f"n{index}.md"))
    assert fake_task.invocations == []


# ---------------------------------------------------------------------------
# Description (06 §3.1)
# ---------------------------------------------------------------------------


def test_build_description_drops_headings_and_collapses_whitespace(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    note = payload(
        fixture_vault,
        body="## Content\n\nCall   the   dentist\nand book a cleaning\n",
    )
    assert consumer.build_description(note) == "Call the dentist and book a cleaning"


def test_build_description_truncates_at_512_with_ellipsis(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    note = payload(fixture_vault, body="x" * 600)
    description = consumer.build_description(note)
    assert len(description) == 512
    assert description.endswith("...")
    assert description[:509] == "x" * 509


def test_build_description_falls_back_to_title_then_stem(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    titled = fixture_vault / "capture" / "raw_capture" / "titled.md"
    titled.write_text("---\ntitle: Renew passport\ntags:\n- todo\n---\n\n## Content\n", encoding="utf-8")
    assert consumer.build_description(payload_from_file(titled)) == "Renew passport"

    bare = fixture_vault / "capture" / "raw_capture" / "just-a-stem.md"
    bare.write_text("---\ntags:\n- todo\n---\n# Heading only\n", encoding="utf-8")
    assert consumer.build_description(payload_from_file(bare)) == "just-a-stem"


def test_empty_description_is_a_skip(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    path = fixture_vault / "capture" / "raw_capture" / "empty.md"
    path.write_text("---\ntags:\n- todo\n---\n", encoding="utf-8")
    note = NotePayload(
        path=Path(""), frontmatter={"tags": ["todo"]}, content="", raw_text="", note_hash="h"
    )
    result = consumer.handle(note, make_context(fixture_vault))
    assert result.status is Status.SKIP
    assert result.message == "empty description"
    assert fake_task.invocations == []


# ---------------------------------------------------------------------------
# Tags + project (06 §3.1; 08 §B7)
# ---------------------------------------------------------------------------


def test_build_tags_extracts_project_and_adds_configured_tags(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    note = payload(fixture_vault, tags=["todo", "project:blog", "Deep Work"])
    tags, project = consumer.build_tags(note, {"deep_work", "para", "automation", "not_reviewed"})
    assert project == "blog"
    assert tags == ["automation", "deep_work", "not_reviewed", "para"]


def test_build_tags_uses_default_project_without_a_project_tag(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    _tags, project = consumer.build_tags(payload(fixture_vault, tags=["todo"]), set())
    assert project == "Inbox"


def test_build_tags_project_extraction_is_deterministic(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """The old code iterated a set, so two project tags picked at random."""
    consumer = make_consumer(fake_task, tmp_path)
    note = payload(fixture_vault, tags=["todo", "project:alpha", "project:beta"])
    for _ in range(5):
        _tags, project = consumer.build_tags(note, set())
        assert project == "alpha"


def test_remove_unknown_tags_whitelists_additional_and_review_tags(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """08 §B7: only ``review_tag`` was whitelisted, so ``para``/``automation``
    were silently dropped — which ALSO changed the dedupe key."""
    consumer = make_consumer(fake_task, tmp_path, remove_unknown_tags=True)
    note = payload(fixture_vault, tags=["todo", "brandnew"])
    tags, _project = consumer.build_tags(note, known_tags=set())  # Taskwarrior knows nothing
    assert tags == ["automation", "not_reviewed", "para"]


def test_remove_unknown_tags_drops_unknown_note_tags_with_a_warning(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    consumer = make_consumer(fake_task, tmp_path, remove_unknown_tags=True)
    note = payload(fixture_vault, tags=["todo", "neverseen"])
    with caplog.at_level(logging.WARNING):
        tags, _project = consumer.build_tags(note, {"para", "automation", "not_reviewed"})
    assert "neverseen" not in tags
    assert any("neverseen" in record.getMessage() for record in caplog.records)


def test_remove_unknown_tags_disabled_keeps_everything(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path, remove_unknown_tags=False)
    note = payload(fixture_vault, tags=["todo", "neverseen"])
    tags, _project = consumer.build_tags(note, set())
    assert "neverseen" in tags


def test_strip_tags_and_marker_are_removed(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(
        fake_task, tmp_path, strip_tags=["inbox"], remove_unknown_tags=False
    )
    note = payload(fixture_vault, tags=["todo", "inbox", "keep"])
    tags, _project = consumer.build_tags(note, set())
    assert "todo" not in tags
    assert "inbox" not in tags
    assert "keep" in tags


# ---------------------------------------------------------------------------
# Golden run + dedupe (06 §7; 08 §B6)
# ---------------------------------------------------------------------------


def test_golden_run_two_captures_produce_exactly_two_task_creates(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    ctx = make_context(fixture_vault)
    first = payload(
        fixture_vault,
        body="Draft the post about learning systems.",
        tags=["todo", "project:blog"],
        name="one.md",
        note_id="cap-1",
    )
    second = payload(
        fixture_vault, body="Email the studio about rehearsal.", name="two.md", note_id="cap-2"
    )

    assert consumer.handle(first, ctx).status is Status.SUCCESS
    assert consumer.handle(second, ctx).status is Status.SUCCESS

    imports = fake_task.imports
    assert len(imports) == 2
    assert imports[0] == {
        "description": "Draft the post about learning systems.",
        "entry": "20260701T090000Z",
        "tags": ["automation", "not_reviewed", "para"],
        "project": "blog",
        "annotations": [{"entry": imports[0]["annotations"][0]["entry"], "description": "Captured from cap-1"}],
    }
    assert imports[1]["description"] == "Email the studio about rehearsal."
    assert imports[1]["project"] == "Inbox"
    assert imports[1]["annotations"][0]["description"] == "Captured from cap-2"

    # export + _tags fetched ONCE for the whole run (B2 laziness), then one
    # import per task.
    assert fake_task.subcommands().count("export") == 1
    assert fake_task.subcommands().count("_tags") == 1
    assert fake_task.subcommands().count("import") == 2


def test_rerun_creates_zero_duplicates(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """The fake keeps its DB, so the second run's ``task export`` really does
    contain the first run's tasks (06 §3.1 dedupe)."""
    ctx = make_context(fixture_vault)
    note = payload(fixture_vault, tags=["todo", "project:blog"], name="one.md")

    first = make_consumer(fake_task, tmp_path)
    assert first.handle(note, ctx).status is Status.SUCCESS
    assert len(fake_task.imports) == 1

    fake_task.reset_log()
    second = make_consumer(fake_task, tmp_path)  # a fresh run
    result = second.handle(note, ctx)
    assert result.status is Status.SKIP
    assert result.message == "duplicate task"
    assert fake_task.imports == []
    assert "import" not in fake_task.subcommands()


def test_duplicate_within_a_single_run_is_skipped(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    ctx = make_context(fixture_vault)
    one = payload(fixture_vault, body="Same thing", name="one.md")
    two = payload(fixture_vault, body="Same thing", name="two.md")
    assert consumer.handle(one, ctx).status is Status.SUCCESS
    assert consumer.handle(two, ctx).status is Status.SKIP
    assert len(fake_task.imports) == 1


def test_dedupe_is_case_insensitive_and_sees_completed_tasks(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    fake_task.set_tasks(
        [
            {
                "description": "DRAFT THE POST",
                "project": "inbox",
                "status": "completed",
                "tags": ["para"],
            }
        ]
    )
    consumer = make_consumer(fake_task, tmp_path)
    result = consumer.handle(payload(fixture_vault, body="Draft the post"), make_context(fixture_vault))
    assert result.status is Status.SKIP
    assert fake_task.imports == []


def test_note_with_several_action_lines_makes_one_task(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """Spec 06 §3.1 is explicit: the description is the whole body joined to
    ONE line, so a capture is one task — not one task per bullet."""
    consumer = make_consumer(fake_task, tmp_path)
    note = payload(fixture_vault, body="- call the vet\n- renew insurance\n")
    assert consumer.handle(note, make_context(fixture_vault)).status is Status.SUCCESS
    assert len(fake_task.imports) == 1
    assert fake_task.imports[0]["description"] == "- call the vet - renew insurance"


# ---------------------------------------------------------------------------
# B1 — the 3-month outage: encoding + timeouts on BOTH directions
# ---------------------------------------------------------------------------


def test_export_with_invalid_utf8_does_not_crash(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """08 §B1 ✔exec: ``task export`` emitted bytes that are not valid UTF-8
    and the strict decode raised ``UnicodeDecodeError`` every 10 minutes for
    three months. Decoding with errors='replace' keeps the run alive."""
    fake_task.set_export_bytes(
        b'[{"description":"caf\xe9 \xff broken","project":"inbox","tags":["para"]}]'
    )
    consumer = make_consumer(fake_task, tmp_path)
    result = consumer.handle(payload(fixture_vault, body="A brand new task"), make_context(fixture_vault))
    assert result.status is Status.SUCCESS
    assert len(fake_task.imports) == 1


def test_invalid_utf8_export_entry_still_dedupes(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """The mojibake row must remain usable as a dedupe key, not be dropped."""
    fake_task.set_export_bytes(b'[{"description":"caf\xe9 run","project":"Inbox","tags":[]}]')
    consumer = make_consumer(fake_task, tmp_path)
    replaced = b"caf\xe9 run".decode("utf-8", errors="replace")
    result = consumer.handle(
        payload(fixture_vault, body=replaced), make_context(fixture_vault)
    )
    assert result.status is Status.SKIP
    assert fake_task.imports == []


def test_tags_output_with_invalid_utf8_does_not_crash(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    fake_task.set_tags_bytes(b"para\nautomation\nnot_reviewed\nbr\xffken\n")
    consumer = make_consumer(fake_task, tmp_path)
    result = consumer.handle(payload(fixture_vault, body="Another task"), make_context(fixture_vault))
    assert result.status is Status.SUCCESS


def test_every_task_subprocess_declares_encoding_errors_and_timeout(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural B1 regression: it is not enough that today's calls happen to
    survive — every invocation must pass the three kwargs."""
    seen: list[dict[str, Any]] = []
    real_run = subprocess.run

    def recording_run(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    consumer = make_consumer(fake_task, tmp_path)
    consumer.handle(payload(fixture_vault), make_context(fixture_vault))

    assert len(seen) == 3  # _tags, export, import
    for kwargs in seen:
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert kwargs["timeout"] == 20.0


def test_task_timeout_becomes_an_error_result(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    fake_task.configure(delay=3.0)
    consumer = make_consumer(fake_task, tmp_path, timeout_seconds=0.3)
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert result.status is Status.ERROR
    assert "timed out" in result.message


def test_unparseable_export_is_an_error_not_a_crash(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    fake_task.set_export_bytes(b"<html>not taskwarrior</html>")
    consumer = make_consumer(fake_task, tmp_path)
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert result.status is Status.ERROR
    assert "not valid JSON" in result.message
    assert fake_task.imports == []


def test_export_failure_is_cached_not_reshelled_per_note(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    fake_task.configure(export_exit=3)
    consumer = make_consumer(fake_task, tmp_path)
    ctx = make_context(fixture_vault)
    for index in range(3):
        assert consumer.handle(payload(fixture_vault, name=f"n{index}.md"), ctx).status is Status.ERROR
    assert fake_task.subcommands().count("export") == 1


def test_missing_task_binary_is_an_error_result(tmp_path: Path, fixture_vault: Path) -> None:
    consumer = TaskwarriorConsumer(
        ConsumerConfig(
            name="taskwarrior",
            type="taskwarrior",
            options={
                "task_binary": str(tmp_path / "definitely-not-here"),
                "data_directory": str(tmp_path),
                "backup_enabled": False,
            },
        )
    )
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert result.status is Status.ERROR
    assert "not found" in result.message


def test_import_failure_becomes_an_error_result(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """Task creation failing is a per-note ERROR (retried next run), never a
    crash that takes the other consumers down (06 §1)."""
    fake_task.configure(import_exit=2, import_stderr="Malformed UDA\n")
    consumer = make_consumer(fake_task, tmp_path)
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert result.status is Status.ERROR
    assert "exited 2" in result.message
    assert "Malformed UDA" in result.metadata["stderr"]


def test_error_results_do_not_poison_later_notes(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    fake_task.configure(import_exit=2)
    consumer = make_consumer(fake_task, tmp_path)
    ctx = make_context(fixture_vault)
    assert consumer.handle(payload(fixture_vault, name="a.md", body="one"), ctx).status is Status.ERROR
    fake_task.configure(import_exit=0)
    assert consumer.handle(payload(fixture_vault, name="b.md", body="two"), ctx).status is Status.SUCCESS


# ---------------------------------------------------------------------------
# Backups + retention (06 §3.1; 08 §B6)
# ---------------------------------------------------------------------------


def backup_root(tmp_path: Path) -> Path:
    return tmp_path / "state" / "backups" / "taskwarrior"


def test_backup_snapshot_taken_before_the_first_import(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    (fake_task.data_dir / "pending.data").write_text("real data\n", encoding="utf-8")
    consumer = make_consumer(fake_task, tmp_path)
    assert not backup_root(tmp_path).exists()
    assert consumer.handle(payload(fixture_vault), make_context(fixture_vault)).status is Status.SUCCESS
    snapshots = sorted(backup_root(tmp_path).iterdir())
    assert len(snapshots) == 1
    assert (snapshots[0] / "pending.data").read_text(encoding="utf-8") == "real data\n"


def test_backup_happens_once_per_run(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    ctx = make_context(fixture_vault)
    consumer.handle(payload(fixture_vault, name="a.md", body="one"), ctx)
    consumer.handle(payload(fixture_vault, name="b.md", body="two"), ctx)
    assert len(list(backup_root(tmp_path).iterdir())) == 1


def test_no_backup_when_nothing_is_imported(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    fake_task.set_tasks([{"description": "already there", "project": "Inbox"}])
    consumer = make_consumer(fake_task, tmp_path)
    assert consumer.handle(
        payload(fixture_vault, body="already there"), make_context(fixture_vault)
    ).status is Status.SKIP
    assert not backup_root(tmp_path).exists()


def test_backup_can_be_disabled(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path, backup_enabled=False)
    assert consumer.handle(payload(fixture_vault), make_context(fixture_vault)).status is Status.SUCCESS
    assert not backup_root(tmp_path).exists()


def test_backup_retention_keeps_only_the_newest_n(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """08 §B6: 43 snapshots, 108 MB, no retention."""
    root = backup_root(tmp_path)
    root.mkdir(parents=True)
    now = datetime.now(UTC)
    for index in range(8):
        stamp = (now - timedelta(minutes=index + 1)).strftime("%Y%m%dT%H%M%SZ")
        (root / stamp).mkdir()
    consumer = make_consumer(fake_task, tmp_path, backup_keep=3)
    assert consumer.handle(payload(fixture_vault), make_context(fixture_vault)).status is Status.SUCCESS
    assert len(list(root.iterdir())) == 3


def test_backup_retention_drops_snapshots_past_the_age_window(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    root = backup_root(tmp_path)
    root.mkdir(parents=True)
    now = datetime.now(UTC)
    old = (now - timedelta(days=90)).strftime("%Y%m%dT%H%M%SZ")
    recent = (now - timedelta(days=2)).strftime("%Y%m%dT%H%M%SZ")
    (root / old).mkdir()
    (root / recent).mkdir()
    consumer = make_consumer(fake_task, tmp_path, backup_keep=10, backup_max_age_days=30)
    consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    names = sorted(p.name for p in root.iterdir())
    assert old not in names
    assert recent in names


def test_backup_retention_always_keeps_the_newest_snapshot(
    fake_task: FakeTask, tmp_path: Path
) -> None:
    """Even when every snapshot is past the age window, one survives — a
    retention rule that can delete the last backup is worse than none."""
    root = backup_root(tmp_path)
    root.mkdir(parents=True)
    now = datetime.now(UTC)
    for days in (100, 200, 300):
        (root / (now - timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")).mkdir()
    consumer = make_consumer(fake_task, tmp_path, backup_keep=10, backup_max_age_days=30)
    consumer.prune_backups(root)
    assert len(list(root.iterdir())) == 1


def test_missing_data_directory_is_an_error_not_a_crash(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path, data_directory=str(tmp_path / "absent"))
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert result.status is Status.ERROR
    assert "data directory not found" in result.message


def test_unresolvable_backup_directory_is_an_error(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = TaskwarriorConsumer(
        ConsumerConfig(
            name="taskwarrior",
            type="taskwarrior",
            options={
                "task_binary": str(fake_task.binary),
                "data_directory": str(fake_task.data_dir),
            },
        )
    )
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert result.status is Status.ERROR
    assert "backup directory" in result.message
    assert fake_task.imports == []


# ---------------------------------------------------------------------------
# Annotations + entry (06 §3.1)
# ---------------------------------------------------------------------------


def test_annotation_template_renders_id(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    consumer.handle(payload(fixture_vault, note_id="cap-42"), make_context(fixture_vault))
    annotations = fake_task.imports[0]["annotations"]
    assert annotations[0]["description"] == "Captured from cap-42"
    assert len(annotations[0]["entry"]) == len("20260701T090000Z")


def test_annotation_template_renders_a_wikilink_back_to_the_capture(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path, annotation_template="Captured from {wikilink}")
    consumer.handle(payload(fixture_vault, name="wiki.md"), make_context(fixture_vault))
    assert (
        fake_task.imports[0]["annotations"][0]["description"]
        == "Captured from [[capture/raw_capture/wiki]]"
    )


def test_annotation_template_renders_relative_path(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path, annotation_template="From {relative_path}")
    consumer.handle(payload(fixture_vault, name="rel.md"), make_context(fixture_vault))
    assert (
        fake_task.imports[0]["annotations"][0]["description"]
        == "From capture/raw_capture/rel.md"
    )


def test_unknown_annotation_placeholder_is_used_verbatim(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path, annotation_template="see {nope}")
    consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert fake_task.imports[0]["annotations"][0]["description"] == "see {nope}"


def test_empty_annotation_template_disables_annotations(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path, annotation_template="")
    consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert "annotations" not in fake_task.imports[0]


def test_entry_falls_back_to_created_date_then_now(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    path = fixture_vault / "capture" / "raw_capture" / "dated.md"
    path.write_text(
        "---\ntags:\n- todo\ncreated_date: '2026-03-04'\n---\nFile the taxes\n", encoding="utf-8"
    )
    consumer = make_consumer(fake_task, tmp_path)
    consumer.handle(payload_from_file(path), make_context(fixture_vault))
    assert fake_task.imports[0]["entry"] == "20260304T000000Z"


# ---------------------------------------------------------------------------
# Dry run (09 §5.6)
# ---------------------------------------------------------------------------


def test_dry_run_never_mutates_taskwarrior(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault, dry_run=True))
    assert result.status is Status.SUCCESS
    assert result.metadata["dry_run"] is True
    assert result.metadata["task"]["description"] == "Draft the post about learning systems."
    assert "import" not in fake_task.subcommands()
    assert not backup_root(tmp_path).exists()


# ---------------------------------------------------------------------------
# LLM enrichment (06 §3.1) — fake client only
# ---------------------------------------------------------------------------


class ScriptedLLM(LLMClient):
    """Records prompts; replays canned responses or raises."""

    def __init__(self, *, text: str = "{}", error: Exception | None = None) -> None:
        self.prompts: list[str] = []
        self.text = text
        self.error = error

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
        self.prompts.append(prompt)
        if self.error is not None:
            raise self.error
        parsed: dict[str, Any] | None
        try:
            candidate = json.loads(self.text)
            parsed = candidate if isinstance(candidate, dict) else None
        except json.JSONDecodeError:
            parsed = None
        return LLMResponse(
            text=self.text, model="fake", backend="fake", duration_ms=1, json=parsed
        )

    def available(self) -> bool:
        return True


def test_llm_disabled_never_calls_the_client(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    llm = ScriptedLLM(text='{"utility": 8}')
    consumer = make_consumer(fake_task, tmp_path)  # llm_enabled defaults to false
    consumer.handle(payload(fixture_vault), make_context(fixture_vault, llm=llm))
    assert llm.prompts == []
    assert "utility" not in fake_task.imports[0]


def test_llm_enrichment_fills_the_four_uda_fields(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    llm = ScriptedLLM(
        text='{"next_action": "Open the draft", "effort": "90 min", '
        '"priority": "high", "utility": 9}'
    )
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault, llm=llm))
    assert result.status is Status.SUCCESS
    body = fake_task.imports[0]
    assert body["next_action"] == "Open the draft"
    assert body["effort"] == "1.5h"  # normalized
    assert body["priority_estimate"] == "high"
    assert body["utility"] == 8  # snapped to the Fibonacci scale
    assert result.metadata["llm_attributes"]["utility"] == 8


def test_llm_prompt_carries_the_importance_guide_and_exemplars(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    fake_task.set_tasks(
        [
            {"description": "short chore", "effort": "15 min", "utility": 2, "priority_estimate": "low"},
            {"description": "big rewrite", "effort": "8h", "utility": 13, "priority_estimate": "high"},
        ]
    )
    llm = ScriptedLLM(text="{}")
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    consumer.handle(payload(fixture_vault), make_context(fixture_vault, llm=llm))
    prompt = llm.prompts[0]
    assert "Importance (also called Utility) scale:" in prompt
    assert "21 (Extremely important)" in prompt
    assert "Effort reference tasks:" in prompt
    assert "short chore" in prompt and "big rewrite" in prompt
    assert "Priority reference tasks:" in prompt
    assert "Utility reference tasks:" in prompt
    assert "Draft the post about learning systems." in prompt
    assert prompt.rstrip().endswith("No additional commentary is allowed before or after the JSON.")


def test_llm_failure_degrades_to_an_unenriched_task(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    llm = ScriptedLLM(error=LLMError("ollama unreachable"))
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault, llm=llm))
    assert result.status is Status.SUCCESS
    assert "utility" not in fake_task.imports[0]


def test_llm_timeout_degrades_to_an_unenriched_task(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """08 §B11: TimeoutError from a remote Ollama is the EXPECTED failure."""
    llm = ScriptedLLM(error=TimeoutError("read timed out"))
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    assert consumer.handle(
        payload(fixture_vault), make_context(fixture_vault, llm=llm)
    ).status is Status.SUCCESS
    assert len(fake_task.imports) == 1


def test_unparseable_llm_output_degrades(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    llm = ScriptedLLM(text="I am not JSON at all")
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    assert consumer.handle(
        payload(fixture_vault), make_context(fixture_vault, llm=llm)
    ).status is Status.SUCCESS
    assert "utility" not in fake_task.imports[0]


def test_llm_enabled_without_a_client_still_creates_the_task(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    result = consumer.handle(payload(fixture_vault), make_context(fixture_vault, llm=None))
    assert result.status is Status.SUCCESS
    assert len(fake_task.imports) == 1


def test_no_ai_note_is_never_sent_to_the_llm(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """Vault law (spec 02 / 06 §2). ``uses_llm`` is False for this consumer —
    the task itself is not an AI product — so the runner's central guard does
    not cover enrichment and this local guard must."""
    path = fixture_vault / "capture" / "raw_capture" / "private-todo.md"
    path.write_text("---\ntags:\n- todo\nno-ai: true\n---\nTherapy homework\n", encoding="utf-8")
    llm = ScriptedLLM(text='{"utility": 8}')
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    result = consumer.handle(payload_from_file(path), make_context(fixture_vault, llm=llm))
    assert llm.prompts == []
    assert result.status is Status.SUCCESS  # the task is still created
    assert "utility" not in fake_task.imports[0]


def test_no_ai_underscore_spelling_is_also_honored(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    path = fixture_vault / "capture" / "raw_capture" / "private-todo2.md"
    path.write_text("---\ntags:\n- todo\nno_ai: true\n---\nQuiet thing\n", encoding="utf-8")
    llm = ScriptedLLM(text='{"utility": 8}')
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    consumer.handle(payload_from_file(path), make_context(fixture_vault, llm=llm))
    assert llm.prompts == []


@pytest.mark.parametrize(
    ("front", "expected_prompted"),
    [
        ("no-ai: true\n", False),
        ("no_ai: true\n", False),
        ('no-ai: "true"\n', False),  # ambiguous truthy ⇒ over-match is the safe error
        ("no-ai: false\n", True),
        ("", True),
    ],
)
def test_the_enrichment_guard_is_NotePayload_no_ai_and_nothing_else(
    fake_task: FakeTask,
    tmp_path: Path,
    fixture_vault: Path,
    front: str,
    expected_prompted: bool,
) -> None:
    """The consumer's local no-ai guard is ``payload.no_ai`` — ONE rule.

    ARCHITECTURE, "Phase-4 rulings, auto_tagger batch" (f24ee2e), PHASE-5
    CHECKLIST item (b), verbatim: "taskwarrior.py's redundant
    ``_note_is_no_ai`` delegating helper simplifies to payload.no_ai". The
    deleted wrapper rebuilt a ``Document`` to reach ``frontmatter.is_no_ai``;
    both doors already end at ``frontmatter.fields_are_no_ai`` (Phase-3
    ruling: "no_ai MUST delegate to the frontmatter module's no-ai
    predicate — ONE rule in the codebase").

    Every cell is asserted, refusing AND firing, so a guard that answered
    ``True`` unconditionally (enrichment permanently dead — the
    ``llm_enabled``-is-inert failure) fails as loudly as one that answered
    ``False`` (the vault law breached). Agreement with the payload property
    is asserted alongside, so the two cannot drift.
    """
    stem = front.replace(":", "").replace(" ", "").replace('"', "").replace("\n", "") or "none"
    path = fixture_vault / "capture" / "raw_capture" / f"guard-{stem}.md"
    path.write_text(f"---\ntags:\n- todo\n{front}---\nGuarded thing\n", encoding="utf-8")
    note = payload_from_file(path)
    assert note.no_ai is not expected_prompted, "the fixture does not exercise this cell"

    llm = ScriptedLLM(text='{"utility": 8}')
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    result = consumer.handle(note, make_context(fixture_vault, llm=llm))

    assert result.status is Status.SUCCESS, "the TASK is created either way (#12)"
    assert bool(llm.prompts) is expected_prompted


def test_the_deleted_no_ai_wrapper_stays_deleted() -> None:
    """The de-dup obligation, made structural rather than a comment: a second
    module-level no-ai predicate here would be a second door onto the vault
    law, which is exactly what the ONE-rule ruling exists to prevent."""
    from organize_core.consumers import taskwarrior as module

    assert not hasattr(module, "_note_is_no_ai")
    assert [name for name in vars(module) if name.endswith("is_no_ai")] == [], sorted(
        name for name in vars(module) if "no_ai" in name
    )


def test_enrichment_is_skipped_when_the_taskrc_lacks_the_udas(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    taskrc = tmp_path / "taskrc"
    taskrc.write_text("data.location=" + str(fake_task.data_dir) + "\n", encoding="utf-8")
    llm = ScriptedLLM(text='{"utility": 8}')
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True, taskrc_path=str(taskrc))
    with caplog.at_level(logging.WARNING):
        result = consumer.handle(payload(fixture_vault), make_context(fixture_vault, llm=llm))
    assert llm.prompts == []
    assert result.status is Status.SUCCESS
    assert any("UDA" in record.getMessage() for record in caplog.records)


def test_enrichment_runs_when_the_taskrc_declares_the_udas(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    taskrc = tmp_path / "taskrc"
    taskrc.write_text(
        "\n".join(
            f"uda.{name}.label={name}" for name in sorted(set(DEFAULT_UDA_FIELDS.values()))
        )
        + "\n",
        encoding="utf-8",
    )
    llm = ScriptedLLM(text='{"utility": 8}')
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True, taskrc_path=str(taskrc))
    consumer.handle(payload(fixture_vault), make_context(fixture_vault, llm=llm))
    assert len(llm.prompts) == 1
    assert fake_task.imports[0]["utility"] == 8


def test_taskrc_is_passed_to_every_invocation(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taskrc = tmp_path / "taskrc"
    taskrc.write_text("data.location=" + str(fake_task.data_dir) + "\n", encoding="utf-8")
    seen: list[dict[str, str]] = []
    real_run = subprocess.run

    def recording_run(*args: Any, **kwargs: Any) -> Any:
        seen.append(dict(kwargs.get("env") or {}))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    consumer = make_consumer(fake_task, tmp_path, taskrc_path=str(taskrc))
    consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert seen and all(env.get("TASKRC") == str(taskrc) for env in seen)


def test_data_location_is_read_from_the_taskrc_when_not_configured(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    taskrc = tmp_path / "taskrc"
    taskrc.write_text('data.location="' + str(fake_task.data_dir) + '"\n', encoding="utf-8")
    consumer = TaskwarriorConsumer(
        ConsumerConfig(
            name="taskwarrior",
            type="taskwarrior",
            options={
                "task_binary": str(fake_task.binary),
                "taskrc_path": str(taskrc),
                "backup_directory": str(backup_root(tmp_path)),
            },
        )
    )
    assert consumer.handle(payload(fixture_vault), make_context(fixture_vault)).status is Status.SUCCESS
    expected = f"rc.data.location={fake_task.data_dir.resolve()}"
    assert any(expected in argv for argv in fake_task.invocations)


def test_one_bad_note_does_not_take_the_consumer_down(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06 §1: per-file failures are logged and skipped, never abort the run.
    B2 is the story of what one unhandled exception costs."""
    consumer = make_consumer(fake_task, tmp_path)
    ctx = make_context(fixture_vault)
    calls = {"n": 0}
    real_build = consumer.build_description

    def flaky(note: NotePayload) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("synthetic bug")
        return real_build(note)

    monkeypatch.setattr(consumer, "build_description", flaky)
    bad = consumer.handle(payload(fixture_vault, name="bad.md", body="one"), ctx)
    assert bad.status is Status.ERROR
    assert "synthetic bug" in bad.message
    good = consumer.handle(payload(fixture_vault, name="good.md", body="two"), ctx)
    assert good.status is Status.SUCCESS


def test_every_invocation_disables_confirmation_and_hooks(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    assert fake_task.invocations
    for argv in fake_task.invocations:
        assert "rc.confirmation=no" in argv
        assert "rc.hooks=off" in argv


def test_consumer_writes_nothing_back_to_the_note(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    note = payload(fixture_vault, name="untouched.md")
    before = note.path.read_text(encoding="utf-8")
    consumer = make_consumer(fake_task, tmp_path)
    consumer.handle(note, make_context(fixture_vault))
    assert note.path.read_text(encoding="utf-8") == before


def test_unicode_survives_the_import_round_trip(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """The write direction is the other half of B1: the payload file is
    written as UTF-8, so an emoji/accented capture is not mangled or fatal."""
    consumer = make_consumer(fake_task, tmp_path)
    note = payload(fixture_vault, body="Book a table at the café ☕ — non-negotiable", name="u.md")
    assert consumer.handle(note, make_context(fixture_vault)).status is Status.SUCCESS
    assert fake_task.imports[0]["description"] == "Book a table at the café ☕ — non-negotiable"


def test_note_with_invalid_utf8_bytes_is_handled(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    """The fixture vault's invalid-UTF-8 note, tagged todo."""
    path = fixture_vault / "capture" / "raw_capture" / "bad-bytes.md"
    path.write_bytes(b"---\ntags:\n- todo\n---\nRepair the caf\xe9 sign\n")
    consumer = make_consumer(fake_task, tmp_path)
    result = consumer.handle(payload_from_file(path), make_context(fixture_vault))
    assert result.status is Status.SUCCESS
    assert "Repair the caf" in fake_task.imports[0]["description"]


def test_import_uses_a_temp_json_file_that_is_cleaned_up(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    consumer.handle(payload(fixture_vault), make_context(fixture_vault))
    import_argv = [argv for argv in fake_task.invocations if "import" in argv][0]
    assert import_argv[-2] == "import"
    payload_file = Path(import_argv[-1])
    assert payload_file.suffix == ".json"
    assert not payload_file.exists()  # removed after the import


def test_reset_run_forces_a_fresh_export(
    fake_task: FakeTask, tmp_path: Path, fixture_vault: Path
) -> None:
    consumer = make_consumer(fake_task, tmp_path)
    ctx = make_context(fixture_vault)
    consumer.handle(payload(fixture_vault, name="a.md", body="one"), ctx)
    assert fake_task.subcommands().count("export") == 1
    consumer.reset_run()
    consumer.handle(payload(fixture_vault, name="b.md", body="two"), ctx)
    assert fake_task.subcommands().count("export") == 2
    # ...and the second run's backup is a second snapshot.
    assert len(list(backup_root(tmp_path).iterdir())) == 2


def test_llm_config_is_never_constructed_by_this_consumer(
    fake_task: FakeTask, tmp_path: Path
) -> None:
    """There is ONE shared client: the consumer takes ``ctx.llm`` and never
    builds an HTTP path of its own (09 §2)."""
    consumer = make_consumer(fake_task, tmp_path, llm_enabled=True)
    assert not hasattr(consumer, "llm_client")
    assert LLMConfig  # the consumer never instantiates one itself
