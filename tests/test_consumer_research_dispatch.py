"""``deep_research`` — real subprocess dispatch against a FAKE agent
(spec 06 §3.4; regressions 08 §B1/§B14/§B15).

Nothing here touches a real research agent, the network, or the vault: the
agent is a Python script written into ``tmp_path`` and invoked through
``sys.executable``. Every case the live system can actually produce is
covered — success, hang-then-timeout, nonzero exit, output that is not valid
UTF-8, and a command that cannot be launched at all.

The contract under test is the one Matt stated in
``person-research-agent.md`` and doc 06 §3.4 preserves verbatim: *"if the CLI
tool returns success then do not send more information to the deep research
agent"*. Exit 0 → SUCCESS (terminal, the runner checkpoints it); anything
else → ERROR (retried next run).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from organize_core.consumers import deep_research as dr
from organize_core.consumers.base import Status
from test_consumer_research import make_consumer, make_ctx, make_payload

# The fake agent. ``mode`` selects the failure it simulates; every mode first
# appends a JSON line to the record file so the test can assert on the exact
# argv/cwd/env the consumer produced.
_AGENT = r'''
import json, os, pathlib, sys, time

mode = sys.argv[1]
record = pathlib.Path(sys.argv[2])
with record.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({
        "argv": sys.argv[3:],
        "cwd": os.getcwd(),
        "NOTES_DIR": os.environ.get("NOTES_DIR"),
        "RESEARCH_TOKEN": os.environ.get("RESEARCH_TOKEN"),
    }) + "\n")

if mode == "success":
    print("researched 1 person")
    sys.exit(0)
if mode == "hang":
    time.sleep(60)
    sys.exit(0)
if mode == "fail":
    sys.stderr.write("agent exploded: no API key\n")
    sys.exit(3)
if mode == "garbage":
    sys.stdout.flush()
    sys.stdout.buffer.write(b"\xff\xfe\x00 not utf-8 \xc3\x28 done\n")
    sys.stdout.buffer.flush()
    sys.exit(0)
if mode == "garbage-fail":
    sys.stderr.flush()
    sys.stderr.buffer.write(b"\xff\xfe traceback \x80\n")
    sys.stderr.buffer.flush()
    sys.exit(4)
sys.exit(9)
'''


@pytest.fixture()
def agent(tmp_path: Path) -> Path:
    script = tmp_path / "fake_research_agent.py"
    script.write_text(_AGENT, encoding="utf-8")
    return script


@pytest.fixture()
def record(tmp_path: Path) -> Path:
    return tmp_path / "invocations.jsonl"


def invocations(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [
        json.loads(line)
        for line in record.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def agent_command(agent: Path, record: Path, mode: str) -> list[str]:
    return [sys.executable, str(agent), mode, str(record)]


def note(tmp_path: Path, name: str = "ada-lovelace.md"):
    path = tmp_path / "vault" / "areas" / "relationships" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntags: [person]\n---\nMet at a conference.\n", encoding="utf-8")
    return make_payload(path, {"tags": ["person"]})


# ---------------------------------------------------------------------------
# the happy path and the checkpoint contract
# ---------------------------------------------------------------------------


def test_success_exit_zero_is_a_terminal_success(agent: Path, record: Path, tmp_path: Path) -> None:
    consumer = make_consumer(command=agent_command(agent, record, "success"), timeout_seconds=30)
    payload = note(tmp_path)

    result = consumer.handle(payload, make_ctx(tmp_path))

    assert result.status is Status.SUCCESS
    assert result.metadata["returncode"] == 0
    assert "researched 1 person" in result.metadata["stdout"]
    assert invocations(record) == [
        {
            "argv": [str(payload.path)],
            # no `cwd` configured ⇒ the agent inherits the pipeline's
            "cwd": os.getcwd(),
            "NOTES_DIR": None,
            "RESEARCH_TOKEN": None,
        }
    ]


def test_the_note_path_reaches_the_agent_through_a_placeholder(
    agent: Path, record: Path, tmp_path: Path
) -> None:
    consumer = make_consumer(
        command=[*agent_command(agent, record, "success"), "--person", "{path}"],
        timeout_seconds=30,
    )
    payload = note(tmp_path, "grace hopper's note.md")

    assert consumer.handle(payload, make_ctx(tmp_path)).status is Status.SUCCESS
    assert invocations(record)[0]["argv"] == ["--person", str(payload.path)]


def test_rerunning_handle_dispatches_again_with_identical_argv(
    agent: Path, record: Path, tmp_path: Path
) -> None:
    """This consumer keeps NO state of its own — rerun protection is the
    runner's hash checkpoint (06 §1), and this pins that a second call is
    byte-identical rather than subtly different. A consumer that varied its
    argv between runs would defeat any dedupe the agent does on its side."""
    consumer = make_consumer(command=agent_command(agent, record, "success"), timeout_seconds=30)
    payload = note(tmp_path)
    ctx = make_ctx(tmp_path)

    first = consumer.handle(payload, ctx)
    second = consumer.handle(payload, ctx)

    assert (first.status, second.status) == (Status.SUCCESS, Status.SUCCESS)
    calls = invocations(record)
    assert len(calls) == 2
    assert calls[0]["argv"] == calls[1]["argv"]


def test_no_ai_notes_never_reach_the_agent(agent: Path, record: Path, tmp_path: Path) -> None:
    """Spec 02 vault law, end to end: not merely "should_process returns
    False" but "no process was spawned"."""
    consumer = make_consumer(command=agent_command(agent, record, "success"), timeout_seconds=30)
    payload = make_payload(tmp_path / "private.md", {"no-ai": True})

    assert consumer.should_process(payload) is False
    assert invocations(record) == []


# ---------------------------------------------------------------------------
# failure modes — all of them ERROR, none of them raise (08 §B14)
# ---------------------------------------------------------------------------


def test_nonzero_exit_is_an_error_carrying_the_diagnosis(
    agent: Path, record: Path, tmp_path: Path
) -> None:
    consumer = make_consumer(command=agent_command(agent, record, "fail"), timeout_seconds=30)

    result = consumer.handle(note(tmp_path), make_ctx(tmp_path))

    assert result.status is Status.ERROR
    assert result.metadata["returncode"] == 3
    assert "no API key" in result.metadata["stderr"]
    assert "exited with 3" in result.message
    assert len(invocations(record)) == 1


def test_a_hanging_agent_is_killed_and_reported(agent: Path, record: Path, tmp_path: Path) -> None:
    """The live consumer had no timeout on ANY subprocess (08 §B1). A single
    hung agent would otherwise wedge the 10-minute timer forever."""
    consumer = make_consumer(command=agent_command(agent, record, "hang"), timeout_seconds=0.75)

    result = consumer.handle(note(tmp_path), make_ctx(tmp_path))

    assert result.status is Status.ERROR
    assert "timeout" in result.message
    assert result.metadata["timeout_seconds"] == 0.75
    assert result.metadata["duration_seconds"] < 30  # it did not wait out the sleep


def test_output_that_is_not_valid_utf8_does_not_crash_the_run(
    agent: Path, record: Path, tmp_path: Path
) -> None:
    """08 §B1 — THE defect. Three months of outage came from decoding a
    subprocess's output as strict UTF-8. Here the agent succeeds but prints
    bytes that cannot be decoded; the run must still report SUCCESS."""
    consumer = make_consumer(command=agent_command(agent, record, "garbage"), timeout_seconds=30)

    result = consumer.handle(note(tmp_path), make_ctx(tmp_path))

    assert result.status is Status.SUCCESS
    assert "�" in result.metadata["stdout"]  # replaced, not raised
    assert "done" in result.metadata["stdout"]


def test_invalid_utf8_on_stderr_of_a_failing_agent_is_also_survivable(
    agent: Path, record: Path, tmp_path: Path
) -> None:
    consumer = make_consumer(command=agent_command(agent, record, "garbage-fail"), timeout_seconds=30)

    result = consumer.handle(note(tmp_path), make_ctx(tmp_path))

    assert result.status is Status.ERROR
    assert result.metadata["returncode"] == 4
    assert "traceback" in result.metadata["stderr"]


def test_a_missing_agent_binary_is_an_error_not_an_exception(tmp_path: Path) -> None:
    consumer = make_consumer(
        command=[str(tmp_path / "does-not-exist"), "{path}"], timeout_seconds=5
    )

    result = consumer.handle(note(tmp_path), make_ctx(tmp_path))

    assert result.status is Status.ERROR
    assert "could not launch" in result.message
    assert result.metadata["error"] in {"FileNotFoundError", "PermissionError"}


def test_a_missing_working_directory_is_an_error_not_an_exception(
    agent: Path, record: Path, tmp_path: Path
) -> None:
    """The live consumer raised from ``__init__`` here, taking every other
    consumer down with it (08 §B2). Now the pipeline reports one bad
    consumer and carries on; the note retries next run."""
    consumer = make_consumer(
        command=agent_command(agent, record, "success"),
        cwd=str(tmp_path / "agent-checkout-gone"),
        timeout_seconds=30,
    )

    result = consumer.handle(note(tmp_path), make_ctx(tmp_path))

    assert result.status is Status.ERROR
    assert "working directory does not exist" in result.message
    assert invocations(record) == []


def test_error_results_carry_no_partial_success_marker(
    agent: Path, record: Path, tmp_path: Path
) -> None:
    """``error`` is retried and ``success`` is not, so a status that drifts
    between them is the difference between a person being researched twice
    and never (08 §B3)."""
    for mode in ("fail", "garbage-fail"):
        consumer = make_consumer(command=agent_command(agent, record, mode), timeout_seconds=30)
        assert consumer.handle(note(tmp_path), make_ctx(tmp_path)).status is Status.ERROR


# ---------------------------------------------------------------------------
# environment + working directory reach the agent
# ---------------------------------------------------------------------------


def test_the_config_env_table_reaches_the_agent_process(
    agent: Path, record: Path, tmp_path: Path
) -> None:
    consumer = make_consumer(
        command=agent_command(agent, record, "success"),
        notes_dir=str(tmp_path / "vault"),
        _env={"RESEARCH_TOKEN": "s3cret"},
        timeout_seconds=30,
    )

    assert consumer.handle(note(tmp_path), make_ctx(tmp_path)).status is Status.SUCCESS

    call = invocations(record)[0]
    assert call["RESEARCH_TOKEN"] == "s3cret"
    assert call["NOTES_DIR"] == str((tmp_path / "vault").resolve())


def test_an_exported_notes_dir_survives_all_the_way_into_the_agent(
    agent: Path, record: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """08 §B15 through the whole stack, not just ``build_env``: what the
    operator exported is what the agent sees."""
    monkeypatch.setenv("NOTES_DIR", str(tmp_path / "exported"))
    consumer = make_consumer(
        command=agent_command(agent, record, "success"),
        notes_dir=str(tmp_path / "option"),
        timeout_seconds=30,
    )

    assert consumer.handle(note(tmp_path), make_ctx(tmp_path)).status is Status.SUCCESS
    assert invocations(record)[0]["NOTES_DIR"] == str(tmp_path / "exported")


def test_the_agent_runs_in_its_configured_working_directory(
    agent: Path, record: Path, tmp_path: Path
) -> None:
    checkout = tmp_path / "DeepResearchAgent"
    checkout.mkdir()
    consumer = make_consumer(
        command=agent_command(agent, record, "success"),
        cwd=str(checkout),
        timeout_seconds=30,
    )

    assert consumer.handle(note(tmp_path), make_ctx(tmp_path)).status is Status.SUCCESS
    assert invocations(record)[0]["cwd"] == str(checkout.resolve())


def test_stored_output_is_bounded(agent: Path, record: Path, tmp_path: Path) -> None:
    """Result metadata is persisted by the runner into the emissions table.
    A chatty agent must not be able to grow the state DB without bound (the
    15 MB DB of 08 §B4 is what unbounded rows look like)."""
    noisy = tmp_path / "noisy_agent.py"
    noisy.write_text(
        "import sys\nsys.stdout.write('x' * 500000)\nsys.exit(0)\n", encoding="utf-8"
    )
    consumer = make_consumer(command=[sys.executable, str(noisy)], timeout_seconds=30)

    result = consumer.handle(note(tmp_path), make_ctx(tmp_path))

    assert result.status is Status.SUCCESS
    assert len(result.metadata["stdout"]) <= dr.OUTPUT_PREVIEW_CHARS + 1
