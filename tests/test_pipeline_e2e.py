"""End-to-end pipeline golden run: the CLI, all four real consumers, a real
SQLite store, a real fixture vault (spec 06 §7 acceptance list).

This is the integration seat's gate, and it is deliberately the ONLY test in
the suite that wires the whole thing together the way systemd will:

    organize --config <toml> --state-dir <tmp> run-consumers

Nothing here is mocked at the seam between our own modules. What IS faked is
everything OUTSIDE the process, and only that:

* ``task`` — a Python script in ``tmp_path`` that logs every argv and keeps
  its "database" as JSON (borrowed from the taskwarrior suite, which owns it).
* Ollama — a ``http.server`` on 127.0.0.1 that records every request body and
  answers ``/api/generate`` differently depending on whether the caller asked
  for ``format: json`` (learn) or free text (question_answer).
* the research agent — a Python script in ``tmp_path`` that appends its argv
  and a slice of its environment to a log and exits 0.

The real vault, the real state dir, the real Taskwarrior data and the real
network are never touched: every path in the config points into ``tmp_path``.

The acceptance obligations covered here, by name from 06 §7:

- golden run: a ``todo`` capture produces EXACTLY one ``task import`` payload,
  snapshot-asserted field by field; a rerun produces zero.
- ``success``/``skip`` do not re-emit; the whole second run is a no-op.
- widening ``include_paths`` in config makes a previously-FILTERED old note
  process (08 §B4 — the retroactivity regression, at CLI level).
- ``no-ai: true`` is never sent to any LLM, asserted against the bytes the
  fake Ollama actually received.
- exit codes: 0 clean, 2 for an unknown ``--consumer`` (06 §4).
- ``--dry-run`` evaluates everything and writes nothing (09 §5.6).
"""

from __future__ import annotations

import json
import stat
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES, build_fixture_vault  # noqa: E402  (tests/ is on sys.path)
from organize_core import cli
from organize_core.consumers.store import AutomationStore

# ---------------------------------------------------------------------------
# Fake Ollama that routes on the request, so ONE endpoint serves both consumers
# ---------------------------------------------------------------------------

CARDS_RESPONSE: dict[str, Any] = {
    "cards": [
        {
            "tier": "thesis",
            "front": "PARADOX: why does spacing beat cramming when both spend the same minutes?",
            "back": "Retrieval difficulty is the signal that drives consolidation.",
            "tags": ["learning"],
        },
        {
            "tier": "tier1_factual",
            "front": "SURPRISE: what interval multiplier does SM-2 settle on?",
            "back": "Roughly 2.5x, modulated by the ease factor.",
            "tags": ["learning"],
        },
        {
            "tier": "tier2_conceptual",
            "front": "CHALLENGE: when is spaced repetition the wrong tool?",
            "back": "[WRITE YOUR ANSWER FIRST]",
            "model_answer": "When the material has no stable atomic facts to recall.",
            "tags": ["learning"],
        },
    ]
}

ANSWER_TEXT = (
    "Intervals grow geometrically because forgetting is roughly exponential, so each "
    "successful recall buys a multiplicatively longer safe window."
)


#: A phrase only the taskwarrior enrichment prompt carries (06 §3.1) — the
#: fake backend routes on it so one server can serve all three JSON callers.
TASKWARRIOR_PROMPT_MARKER = "enriching Taskwarrior tasks"

#: What the fake backend answers that prompt with. The four keys are the
#: 06 §3.1 UDAs; ``utility`` is deliberately off-scale (7) so the test also
#: pins the "snapped to the Fibonacci scale" normalization.
ENRICHMENT_RESPONSE = {
    "next_action": "Open the draft and outline three sections",
    "effort": "90 min",
    "priority": "high",
    "utility": 7,
}


@dataclass
class FakeOllama:
    """127.0.0.1 stand-in for ``/api/generate`` that records what it was sent."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    _httpd: Any = None
    _thread: threading.Thread | None = None

    # --- observation -----------------------------------------------------
    @property
    def prompts(self) -> list[str]:
        return [str(r.get("prompt") or "") + "\n" + str(r.get("system") or "") for r in self.requests]

    def saw_text(self, needle: str) -> bool:
        return any(needle in blob for blob in self.prompts)

    def reply_for(self, payload: dict[str, Any]) -> str:
        # Three JSON-mode callers are told apart by prompt content:
        # taskwarrior enrichment carries the Fibonacci IMPORTANCE GUIDE
        # (06 §3.1), learn asks for cards (§3.2), question_answer is plain
        # text (§3.3).
        prompt = str(payload.get("prompt") or "") + str(payload.get("system") or "")
        if TASKWARRIOR_PROMPT_MARKER in prompt:
            return json.dumps(ENRICHMENT_RESPONSE)
        if payload.get("format") == "json":
            return json.dumps(CARDS_RESPONSE)
        return ANSWER_TEXT

    # --- lifecycle -------------------------------------------------------
    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeOllama:
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.state = self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: object) -> None:  # noqa: D102 - silence test noise
        return

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        state: FakeOllama = self.server.state  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:  # pragma: no cover - defensive
            payload = {}
        state.requests.append(payload)
        body = json.dumps({"response": state.reply_for(payload), "model": "fake-model"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:  # pragma: no cover - client hung up
            return

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request: object, client_address: object) -> None:
        return


@contextmanager
def fake_ollama() -> Iterator[FakeOllama]:
    server = FakeOllama().start()
    try:
        yield server
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Fake external binaries
# ---------------------------------------------------------------------------

_FAKE_TASK = r'''#!%(python)s
"""Fake Taskwarrior for the e2e run: logs argv, keeps its DB in tasks.json."""
import json
import os
import sys

STATE = %(state)r


def _path(name):
    return os.path.join(STATE, name)


def _load(name, default):
    try:
        with open(_path(name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


argv = sys.argv[1:]
with open(_path("invocations.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")

sub = None
for arg in argv:
    if not arg.startswith("rc"):
        sub = arg
        break

if sub == "_tags":
    sys.stdout.write("".join(t + "\n" for t in _load("tags.json", [])))
    sys.exit(0)

if sub == "export":
    sys.stdout.write(json.dumps(_load("tasks.json", [])))
    sys.exit(0)

if sub == "import":
    with open(argv[-1], "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    with open(_path("imports.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")
    tasks = _load("tasks.json", [])
    tasks.extend(payload)
    with open(_path("tasks.json"), "w", encoding="utf-8") as fh:
        json.dump(tasks, fh)
    sys.stdout.write("Imported " + str(len(payload)) + " tasks\n")
    sys.exit(0)

sys.stderr.write("unknown subcommand: " + repr(sub) + "\n")
sys.exit(2)
'''

_FAKE_AGENT = r'''#!%(python)s
"""Fake research agent: logs argv + the env keys we care about, exits 0."""
import json
import os
import sys

with open(%(log)r, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({
        "argv": sys.argv[1:],
        "notes_dir": os.environ.get("NOTES_DIR"),
        "extra": os.environ.get("E2E_MARKER"),
        "cwd": os.getcwd(),
    }) + "\n")
sys.exit(0)
'''


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# ---------------------------------------------------------------------------
# The e2e world
# ---------------------------------------------------------------------------

#: A `todo` capture that lands under `projects/`, i.e. OUTSIDE taskwarrior's
#: initial include_paths. Short on purpose so the learn consumer's
#: min_content_length keeps its hands off it.
LATE_TODO_REL = "projects/blog/late-todo.md"
LATE_TODO = """---
timestamp: '2026-05-02T08:00:00.000000+00:00'
id: 'late-todo'
tags:
- todo
---
Ship the retroactivity fix.
"""

LEARN_NOTE_REL = "resources/spaced-repetition.md"
LEARN_NOTE = """---
title: Why spaced repetition works
tags:
- learn
- learning
sources:
- https://example.invalid/paper
---
Spaced repetition schedules reviews at growing intervals. The SM-2 family of
algorithms multiplies the previous interval by an ease factor of roughly 2.5
after each successful recall, and collapses it back to one day after a lapse.
The mechanism people usually cite is the spacing effect: retrieval that is
effortful but successful consolidates memory far more than rereading, because
the difficulty of the retrieval is itself the learning signal (Bjork, 2011).
Studies report 200% or larger gains over massed practice for durable recall at
one month, which is why every serious flashcard system schedules rather than
shuffles.
"""

PRIVATE_LEARN_REL = "resources/private-journal-analysis.md"
PRIVATE_LEARN = """---
title: Private analysis
no-ai: true
tags:
- learn
---
NEVERSENDTHIS: this note carries the vault's do-not-touch flag. It is long
enough to clear min_content_length and it is tagged `learn`, so the ONLY thing
keeping it away from the LLM is the runner's central no-ai guard. The spacing
effect, ease factors, consolidation and every other keyword here exists purely
so that a lazy trigger would fire on it. It must never reach the model, and the
assertion that proves it reads the bytes the fake Ollama actually received.
"""

RELATIONSHIP_REL = "areas/relationships/jane-doe.md"
RELATIONSHIP = """---
title: Jane Doe
tags:
- person
---
Met at the systems meetup. Works on retrieval infrastructure.
"""

PRIVATE_RELATIONSHIP_REL = "areas/relationships/quiet-friend.md"
PRIVATE_RELATIONSHIP = """---
title: Quiet Friend
no-ai: true
---
Do not research this person.
"""


@dataclass
class World:
    vault: Path
    state: Path
    config_file: Path
    task_state: Path
    agent_log: Path
    ollama: FakeOllama

    # --- observation -----------------------------------------------------
    def task_invocations(self) -> list[list[str]]:
        path = self.task_state / "invocations.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def task_subcommands(self) -> list[str]:
        out: list[str] = []
        for argv in self.task_invocations():
            for arg in argv:
                if not arg.startswith("rc"):
                    out.append(arg)
                    break
        return out

    def imported_tasks(self) -> list[dict[str, Any]]:
        path = self.task_state / "imports.jsonl"
        if not path.exists():
            return []
        flat: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                flat.extend(json.loads(line))
        return flat

    def agent_runs(self) -> list[dict[str, Any]]:
        if not self.agent_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.agent_log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def review_files(self) -> list[Path]:
        return sorted((self.vault / "resources/flashcards/review").glob("*.md"))

    def answer_files(self) -> list[Path]:
        return sorted((self.vault / "resources/answers").glob("*.md"))

    def vault_census(self) -> dict[str, str]:
        """Every vault file → its text, for byte-level no-op assertions."""
        out: dict[str, str] = {}
        for path in sorted(self.vault.rglob("*")):
            if path.is_file():
                out[str(path.relative_to(self.vault))] = path.read_text(
                    encoding="utf-8", errors="replace"
                )
        return out

    def emissions(self) -> dict[tuple[str, str], str]:
        with AutomationStore(self.state / "automations.db") as store:
            rows = store._db.execute(  # noqa: SLF001 - test introspection of real state
                "SELECT consumer, note_path, status FROM emissions"
            ).fetchall()
        return {
            (str(r["consumer"]), Path(str(r["note_path"])).name): str(r["status"]) for r in rows
        }

    def store_census(self) -> dict[str, int]:
        """Row counts for EVERY table the runner writes.

        ``emissions`` alone is not enough for the dry-run assertion: the
        runner also writes ``notes`` via ``mark_seen``, and a dry run that
        skipped only the checkpoints would have passed an emissions-only
        check while quietly stamping ``last_seen`` on the whole vault.
        """
        db = self.state / "automations.db"
        if not db.exists():
            return {"emissions": 0, "notes": 0}
        with AutomationStore(db) as store:
            return {
                table: int(
                    store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: SLF001
                )
                for table in ("emissions", "notes")
            }

    # --- driving ---------------------------------------------------------
    def run(self, *extra: str) -> int:
        return cli.main(
            ["--config", str(self.config_file), "--state-dir", str(self.state), *extra]
        )


CONFIG_TEMPLATE = """
[vault]
root = "{vault}"
scan_dirs = ["capture/raw_capture", "resources", "areas", "projects"]
ignore_patterns = [".obsidian", ".git", "resources/flashcards"]

[logging]
level = "WARNING"

[llm]
backend = "ollama"
ollama_host = "{ollama}"
ollama_model = "fake-model"
timeout_seconds = 10.0
retries = 0

[consumers.taskwarrior]
type = "taskwarrior"
enabled = true
include_paths = {tw_include}
max_notes_per_run = 50
marker_tag = "todo"
default_project = "Inbox"
additional_tags = ["para", "automation"]
review_tag = "not_reviewed"
annotation_template = "Captured from {{id}}"
task_binary = "{task_binary}"
data_directory = "{task_data}"
backup_directory = "{task_backups}"
timeout_seconds = 30.0

[consumers.learn]
type = "learn"
enabled = true
include_paths = ["resources", "areas", "projects"]
exclude_paths = ["resources/flashcards"]
max_notes_per_run = 20
min_content_length = 200
max_cards_per_note = 12
deck = "Reading::Articles"
card_tags = ["learn-consumer"]
llm_timeout_seconds = 10.0

[consumers.question_answer]
type = "question_answer"
enabled = true
include_paths = ["capture/raw_capture"]
max_notes_per_run = 20
marker_tag = "question"
answers_dir = "resources/answers"
llm_timeout_seconds = 10.0

[consumers.deep_research]
type = "deep_research"
enabled = true
include_paths = ["areas/relationships"]
max_notes_per_run = 5
command = ["{python}", "{agent}"]
cwd = "{agent_cwd}"
timeout_seconds = 60.0

[consumers.deep_research.env]
E2E_MARKER = "from-config"
"""


def build_world(
    tmp_path: Path,
    ollama: FakeOllama,
    *,
    taskwarrior_include: list[str] | None = None,
) -> World:
    vault = build_fixture_vault(tmp_path / "vault")
    for rel, text in (
        (LATE_TODO_REL, LATE_TODO),
        (LEARN_NOTE_REL, LEARN_NOTE),
        (PRIVATE_LEARN_REL, PRIVATE_LEARN),
        (RELATIONSHIP_REL, RELATIONSHIP),
        (PRIVATE_RELATIONSHIP_REL, PRIVATE_RELATIONSHIP),
    ):
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    state = tmp_path / "state"
    task_state = tmp_path / "faketask"
    task_state.mkdir(parents=True, exist_ok=True)
    (task_state / "tags.json").write_text(
        json.dumps(["para", "automation", "not_reviewed", "blog"]), encoding="utf-8"
    )
    (task_state / "tasks.json").write_text("[]", encoding="utf-8")
    task_binary = _executable(
        tmp_path / "bin" / "fake-task",
        _FAKE_TASK % {"python": sys.executable, "state": str(task_state)},
    )
    task_data = tmp_path / "taskdata"
    task_data.mkdir(parents=True, exist_ok=True)
    (task_data / "pending.data").write_text("[]\n", encoding="utf-8")

    agent_cwd = tmp_path / "agent"
    agent_log = agent_cwd / "runs.jsonl"
    agent = _executable(agent_cwd / "agent.py", _FAKE_AGENT % {"python": sys.executable,
                                                               "log": str(agent_log)})

    config_file = tmp_path / "config.toml"
    write_config(
        config_file,
        vault=vault,
        ollama=ollama,
        task_binary=task_binary,
        task_data=task_data,
        task_backups=state / "backups" / "taskwarrior",
        agent=agent,
        agent_cwd=agent_cwd,
        taskwarrior_include=taskwarrior_include,
    )
    return World(
        vault=vault,
        state=state,
        config_file=config_file,
        task_state=task_state,
        agent_log=agent_log,
        ollama=ollama,
    )


def write_config(
    config_file: Path,
    *,
    vault: Path,
    ollama: FakeOllama,
    task_binary: Path,
    task_data: Path,
    task_backups: Path,
    agent: Path,
    agent_cwd: Path,
    taskwarrior_include: list[str] | None = None,
) -> None:
    include = taskwarrior_include or ["capture/raw_capture"]
    config_file.write_text(
        CONFIG_TEMPLATE.format(
            vault=vault,
            ollama=ollama.base_url,
            tw_include=json.dumps(include),
            task_binary=task_binary,
            task_data=task_data,
            task_backups=task_backups,
            python=sys.executable,
            agent=agent,
            agent_cwd=agent_cwd,
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def world(tmp_path: Path) -> Iterator[World]:
    with fake_ollama() as ollama:
        yield build_world(tmp_path, ollama)


# ---------------------------------------------------------------------------
# The golden run
# ---------------------------------------------------------------------------


def test_the_first_run_produces_the_expected_side_effect_for_every_consumer(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """06 §7 golden run, all four consumers at once through the real CLI."""
    code = world.run("run-consumers")
    out = capsys.readouterr().out
    assert code == 0, out

    # --- taskwarrior: one import PER `todo` capture and not one more. The
    # fixture vault carries three (the dated capture plus the two
    # metadata-quirk notes), so the golden number is three, established by
    # reading the fixture rather than by whatever the code happened to do.
    imports = world.imported_tasks()
    assert sorted(t["description"] for t in imports) == [
        "Draft the post about learning systems.",
        "list-style metadata",
        "map-style metadata",
    ]
    task = next(t for t in imports if t["description"].startswith("Draft"))
    assert task["project"] == "blog"  # from the `project:blog` prefix tag
    assert sorted(task["tags"]) == ["automation", "not_reviewed", "para"]
    assert task["entry"].startswith("20260701T090000")
    assert [a["description"] for a in task["annotations"]] == [
        "Captured from 2026-07-01T09:00:00.000Z"
    ]
    # and it really shelled out to the fake binary — lazily, ONCE per run for
    # the two read subcommands (08 §B2), once per created task for import.
    assert world.task_subcommands().count("export") == 1
    assert world.task_subcommands().count("_tags") <= 1
    assert world.task_subcommands().count("import") == 3
    # 06 §3.1 backup: a snapshot exists because a mutation happened
    snapshots = sorted((world.state / "backups" / "taskwarrior").glob("*"))
    assert len(snapshots) == 1, snapshots

    # --- learn: one review file, the source note marked, a generation log row.
    # question_answer's tier-2 card lands in the SAME directory (06 §3.3), so
    # the learn file is identified by its source, not by being alone there.
    reviews = [
        p
        for p in world.review_files()
        if "spaced-repetition.md" in p.read_text(encoding="utf-8")
    ]
    assert len(reviews) == 1, [p.name for p in world.review_files()]
    review_text = reviews[0].read_text(encoding="utf-8")
    assert "source_note:" in review_text
    assert "Card 1" in review_text
    assert "PARADOX" in review_text
    source = (world.vault / LEARN_NOTE_REL).read_text(encoding="utf-8")
    assert "processing_status: learn-processed" in source
    log = world.vault / "resources/flashcards/generation-log.md"
    assert log.exists(), "06 §3.2: the generation log is CREATED when missing"
    log_text = log.read_text(encoding="utf-8")
    assert "Why spaced repetition works" in log_text
    assert "thesis:1" in log_text

    # --- question_answer: an answer note plus its tier-2 card
    answers = world.answer_files()
    assert len(answers) == 1, [p.name for p in answers]
    answer_text = answers[0].read_text(encoding="utf-8")
    assert "geometrically" in answer_text
    assert "question-answer" in answer_text
    assert any("Reading::Questions" in p.read_text(encoding="utf-8") for p in world.review_files())

    # --- deep_research: dispatched exactly once, for the one eligible note
    runs = world.agent_runs()
    assert len(runs) == 1, runs
    assert runs[0]["argv"] == [str((world.vault / RELATIONSHIP_REL).resolve())]
    assert runs[0]["extra"] == "from-config"  # [consumers.X.env] passthrough (B15)

    # --- the store recorded terminal emissions and nothing else
    emissions = world.emissions()
    assert emissions[("taskwarrior", Path(QUIRK_FILES["todo_capture"]).name)] == "success"
    assert emissions[("learn", "spaced-repetition.md")] == "success"
    assert emissions[("question_answer", Path(QUIRK_FILES["question_capture"]).name)] == "success"
    assert emissions[("deep_research", "jane-doe.md")] == "success"
    assert set(emissions.values()) <= {"success", "skip"}, "only terminal statuses are persisted"
    # B4: filter misses are never persisted, so the DB stays tiny
    assert ("taskwarrior", "scalar-tags.md") not in emissions
    assert len(emissions) < 12, f"{len(emissions)} rows — filter misses must not be persisted"

    # --- the summary line 06 §4 specifies is on stdout
    assert "Consumer taskwarrior: success=3 skip=0 limit=0 error=0 filtered=" in out


def test_no_ai_notes_never_reach_the_llm_or_the_agent(world: World) -> None:
    """Vault law (spec 02 / 06 §2), asserted against the bytes the fake Ollama
    actually received rather than against a counter the code controls."""
    assert world.run("run-consumers") == 0

    assert world.ollama.requests, "the fake Ollama should have been called at all"
    assert not world.ollama.saw_text("NEVERSENDTHIS"), (
        "a no-ai note reached the model — the runner's central guard is not working"
    )
    # ...and no output file was produced for it either
    assert not any("private-journal" in p.read_text(encoding="utf-8") for p in world.review_files())
    # deep_research is uses_llm=False, so the runner's central guard does NOT
    # cover it; its own should_process must refuse the no-ai relationship note.
    dispatched = {Path(r["argv"][0]).name for r in world.agent_runs()}
    assert dispatched == {"jane-doe.md"}


def test_the_second_run_is_a_complete_no_op(world: World) -> None:
    """06 §7: rerun → zero. Terminal checkpoints suppress redelivery, and the
    only thing the pipeline may do on an unchanged vault is nothing."""
    assert world.run("run-consumers") == 0
    census_after_first = world.vault_census()
    llm_calls_after_first = len(world.ollama.requests)
    task_calls_after_first = len(world.task_invocations())
    agent_runs_after_first = len(world.agent_runs())
    emissions_after_first = world.emissions()
    imports_after_first = world.imported_tasks()

    assert world.run("run-consumers") == 0

    assert world.imported_tasks() == imports_after_first, "a duplicate task was created"
    assert world.vault_census() == census_after_first, "the second run wrote to the vault"
    assert len(world.agent_runs()) == agent_runs_after_first, "the agent was re-dispatched"
    assert len(world.ollama.requests) == llm_calls_after_first, "the LLM was called again"
    assert len(world.task_invocations()) == task_calls_after_first, (
        "`task` was invoked again — an unchanged vault must not touch the binary at all"
    )
    # learn's own write-back changed the source hash on run 1, so learn IS
    # re-delivered once and must settle to a SKIP, not a regeneration.
    second = world.emissions()
    assert second[("learn", "spaced-repetition.md")] in {"success", "skip"}
    assert set(second) == set(emissions_after_first)


def test_editing_a_note_re_emits_it(world: World) -> None:
    """The hash checkpoint is the idempotency key: change the bytes, get a new
    emission (06 §1). The complement of the no-op test above."""
    assert world.run("run-consumers") == 0
    before = len(world.imported_tasks())

    todo = world.vault / QUIRK_FILES["todo_capture"]
    todo.write_text(
        todo.read_text(encoding="utf-8").replace(
            "Draft the post about learning systems.",
            "Draft the post about learning systems and ship it.",
        ),
        encoding="utf-8",
    )

    assert world.run("run-consumers") == 0
    imports = world.imported_tasks()
    assert len(imports) == before + 1, imports
    assert imports[-1]["description"] == "Draft the post about learning systems and ship it."


def test_widening_include_paths_processes_a_previously_filtered_note(
    tmp_path: Path,
) -> None:
    """08 §B4, the retroactivity regression, end to end through the CLI.

    ``projects/blog/late-todo.md`` is a ``todo`` capture that run 1 FILTERS
    because it is outside taskwarrior's ``include_paths``. The old pipeline
    persisted that filter miss per hash, so widening the config never applied
    retroactively. Here the config change alone must make it process — the
    note's bytes never change.
    """
    with fake_ollama() as ollama:
        world = build_world(tmp_path, ollama)
        assert world.run("run-consumers") == 0
        first = {t["description"] for t in world.imported_tasks()}
        assert "Ship the retroactivity fix." not in first
        assert ("taskwarrior", "late-todo.md") not in world.emissions(), (
            "a filter miss was persisted — this is exactly 08 §B4"
        )

        # widen include_paths; touch nothing else, and no note on disk changes
        before = world.vault_census()
        write_config(
            world.config_file,
            vault=world.vault,
            ollama=ollama,
            task_binary=tmp_path / "bin" / "fake-task",
            task_data=tmp_path / "taskdata",
            task_backups=world.state / "backups" / "taskwarrior",
            agent=tmp_path / "agent" / "agent.py",
            agent_cwd=tmp_path / "agent",
            taskwarrior_include=["capture/raw_capture", "projects"],
        )
        assert world.vault_census() == before

        assert world.run("run-consumers") == 0
        descriptions = {t["description"] for t in world.imported_tasks()}
        assert descriptions == first | {"Ship the retroactivity fix."}
        assert world.emissions()[("taskwarrior", "late-todo.md")] == "success"


def test_dry_run_evaluates_everything_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """09 §5.6: the first supervised cutover step. Full scan, full decision
    path, zero side effects — in the vault, in the store, and in every
    external tool."""
    with fake_ollama() as ollama:
        world = build_world(tmp_path, ollama)
        before = world.vault_census()

        assert world.run("run-consumers", "--dry-run") == 0
        out = capsys.readouterr().out

        assert world.vault_census() == before
        assert world.emissions() == {}
        # EVERY table, not just emissions: `mark_seen` and the soft purge are
        # store writes too, and a dry run performs neither (09 §5.6).
        assert world.store_census() == {"emissions": 0, "notes": 0}
        assert world.task_invocations() == []
        assert world.agent_runs() == []
        assert world.ollama.requests == []
        assert "[DRY-RUN]" in out
        assert "would_process" in out

        # and a real run afterwards still does the work — the rehearsal did
        # not checkpoint anything away
        assert world.run("run-consumers") == 0
        assert len(world.imported_tasks()) == 3


def test_selecting_one_consumer_runs_only_that_one(world: World) -> None:
    assert world.run("run-consumers", "--consumer", "TaskWarrior") == 0
    assert len(world.imported_tasks()) == 3
    assert world.ollama.requests == []
    assert world.agent_runs() == []
    assert set(c for c, _ in world.emissions()) == {"taskwarrior"}


def test_an_unknown_consumer_name_is_exit_2(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """06 §4: unknown ``--consumer`` is a USAGE error. Every other
    OrganizeError is exit 1, so this needs its own path."""
    assert world.run("run-consumers", "--consumer", "nope") == 2
    err = capsys.readouterr().err
    assert "unknown consumer(s): nope" in err
    assert "taskwarrior" in err  # the hint lists what IS configured


def test_list_consumers_constructs_nothing_and_needs_no_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """06 §4 / 08 §B2. No --config, no --state-dir, no vault: if this ever
    needs them, something is being constructed that should not be."""
    assert cli.main(["run-consumers", "--list-consumers"]) == 0
    listed = capsys.readouterr().out.split()
    assert {"taskwarrior", "learn", "question_answer", "deep_research"} <= set(listed)


def test_a_failing_consumer_does_not_stop_the_others_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """06 §7 / 08 §B2: the 3-month outage in one sentence. One consumer's
    external tool is broken; the other three must still do their work and the
    run must exit 1 loudly rather than 0 silently."""
    with fake_ollama() as ollama:
        world = build_world(tmp_path, ollama)
        # point the deep_research agent at a path that does not exist
        write_config(
            world.config_file,
            vault=world.vault,
            ollama=ollama,
            task_binary=tmp_path / "bin" / "fake-task",
            task_data=tmp_path / "taskdata",
            task_backups=world.state / "backups" / "taskwarrior",
            agent=tmp_path / "agent" / "does-not-exist.py",
            agent_cwd=tmp_path / "agent",
        )

        assert world.run("run-consumers") == 1
        out = capsys.readouterr().out

        assert len(world.imported_tasks()) == 3, "taskwarrior stopped because another seat broke"
        assert len(world.review_files()) >= 1, "learn stopped because another seat broke"
        assert "Consumer deep_research: success=0" in out
        # the broken consumer is NOT checkpointed, so it retries next run
        assert ("deep_research", "jane-doe.md") not in world.emissions()


def test_json_output_carries_the_same_numbers_as_the_summary_lines(world: World,
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """The scriptable door (spec 10 §1: every capability scriptable)."""
    assert world.run("run-consumers", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert payload["errors"] == 0
    assert payload["dry_run"] is False
    assert payload["migration"]["to_version"] >= 2
    by_name = {c["name"]: c for c in payload["consumers"]}
    assert by_name["taskwarrior"]["success"] == 3
    assert by_name["deep_research"]["success"] == 1
    assert by_name["taskwarrior"]["filtered"] > 0


# ---------------------------------------------------------------------------
# 06 §3.1 LLM enrichment, THROUGH the runner (not a hand-built RunContext)
# ---------------------------------------------------------------------------


def _enable_taskwarrior_llm(world: World) -> None:
    """Flip ``llm_enabled = true`` on the taskwarrior section, the way an
    operator would."""
    text = world.config_file.read_text(encoding="utf-8")
    marker = "[consumers.taskwarrior]\ntype = \"taskwarrior\"\n"
    assert marker in text
    world.config_file.write_text(
        text.replace(marker, marker + "llm_enabled = true\nllm_timeout_seconds = 10.0\n", 1),
        encoding="utf-8",
    )


def test_taskwarrior_llm_enabled_actually_reaches_the_llm_through_the_pipeline(
    world: World,
) -> None:
    """``llm_enabled = true`` was INERT in production.

    The runner handed a client only to consumers whose CLASS sets
    ``uses_llm = True``, and taskwarrior sets it False (so that a ``no-ai``
    capture still becomes a task). ``ctx.llm`` was therefore always None:
    every run logged "llm_enabled but no LLM client is configured", the
    backend received zero requests, and the 06 §3.1 enrichment path could
    not execute through the pipeline at all — only through tests that
    hand-built a RunContext. This drives the REAL composition root.
    """
    _enable_taskwarrior_llm(world)

    assert world.run("run-consumers") == 0

    enrichment_calls = [p for p in world.ollama.prompts if TASKWARRIOR_PROMPT_MARKER in p]
    assert enrichment_calls, "no enrichment prompt reached the backend"

    imports = world.imported_tasks()
    assert imports
    enriched = [t for t in imports if "next_action" in t]
    assert enriched, f"no import payload carried the 06 §3.1 UDAs: {imports}"
    task = enriched[0]
    assert task["next_action"] == ENRICHMENT_RESPONSE["next_action"]
    assert task["priority_estimate"] == "high"
    # utility is snapped to the Fibonacci scale: 7 is not on it, 8 is.
    assert task["utility"] == 8
    assert task["effort"]


def test_taskwarrior_llm_disabled_sends_nothing_and_still_imports(world: World) -> None:
    """The default (live config) path: enrichment off means no LLM traffic
    from taskwarrior and an unenriched — but still created — task."""
    assert world.run("run-consumers") == 0

    assert not [p for p in world.ollama.prompts if TASKWARRIOR_PROMPT_MARKER in p]
    imports = world.imported_tasks()
    assert imports
    assert not any("next_action" in t for t in imports)


def test_a_no_ai_todo_still_becomes_a_task_but_is_never_enriched(world: World) -> None:
    """Vault law and the task itself are DIFFERENT questions (ARCHITECTURE
    resolution #12): ``no-ai: true`` must suppress the LLM call, not the
    Taskwarrior import. Injecting the client must not have changed that."""
    _enable_taskwarrior_llm(world)
    note = world.vault / "capture/raw_capture/private-todo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        "---\nid: private-todo\ntags: [todo]\nno-ai: true\n---\nDo not send me to a model.\n",
        encoding="utf-8",
    )

    assert world.run("run-consumers") == 0

    descriptions = [str(t.get("description", "")) for t in world.imported_tasks()]
    assert any("Do not send me to a model" in d for d in descriptions)
    for blob in world.ollama.prompts:
        assert "Do not send me to a model" not in blob
