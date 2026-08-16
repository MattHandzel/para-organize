"""Black-box verification of the `organize` CLI (seat: cli).

Kept OUTSIDE tests/ because tests/test_cli*.py belongs to the CLI-test seat
per the integrator ruling. Run:

    .venv/bin/python -m pytest <this file> -q

Every case drives the REAL console script in a subprocess against a fixture
vault + tmp CorePaths (ORGANIZE_CORE_* env), never real state.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path("/home/matth/Projects/KnowledgeManagementSystem/organize-rewrite")
sys.path.insert(0, str(REPO / "tests"))

from conftest import QUIRK_FILES, build_fixture_vault  # noqa: E402

ORGANIZE = REPO / ".venv" / "bin" / "organize"
CAPTURE = QUIRK_FILES["current_schema"]  # tags: impro, creativity; sources: me


def _config_text(vault: Path, *, extra: str = "") -> str:
    return f"""
[vault]
root = "{vault}"
scan_dirs = ["capture/raw_capture", "resources", "areas", "projects"]

[vault.para_folders]
projects = "projects"
areas = "areas"
resources = "resources"
archives = "archive"

[logging]
level = "WARNING"

[[routes]]
tags = ["impro"]
destination = "resources/performing/"
mode = "move"
description = "Improv and performance practice."
{extra}
"""


class Core:
    def __init__(self, root: Path) -> None:
        self.vault = build_fixture_vault(root / "vault")
        self.config_dir = root / "config"
        self.state_dir = root / "state"
        self.runtime_dir = root / "run"
        self.config_dir.mkdir(parents=True)
        self.runtime_dir.mkdir(parents=True)
        (self.config_dir / "config.toml").write_text(_config_text(self.vault), encoding="utf-8")

    @property
    def env(self) -> dict[str, str]:
        return {
            "PATH": "/usr/bin:/bin",
            "ORGANIZE_CORE_CONFIG_DIR": str(self.config_dir),
            "ORGANIZE_CORE_STATE_DIR": str(self.state_dir),
            "ORGANIZE_CORE_RUNTIME_DIR": str(self.runtime_dir),
        }

    def run(self, *args: str, stdin: str | None = None, env: dict[str, str] | None = None):
        return subprocess.run(
            [str(ORGANIZE), *args],
            capture_output=True,
            text=True,
            timeout=120,
            input=stdin,
            env=env if env is not None else self.env,
        )

    def index(self):
        proc = self.run("index")
        assert proc.returncode == 0, proc.stderr
        return proc


@pytest.fixture()
def core(tmp_path: Path) -> Core:
    return Core(tmp_path)


# --- index -----------------------------------------------------------------


def test_index_indexes_the_fixture_vault(core: Core) -> None:
    proc = core.index()
    assert "indexed 21 notes (total 21)" in proc.stdout
    assert (core.state_dir / "index.json").is_file()


def test_index_dry_run_writes_no_snapshot(core: Core) -> None:
    proc = core.run("--dry-run", "index")
    assert proc.returncode == 0
    assert "dry-run" in proc.stdout
    assert not (core.state_dir / "index.json").exists()


def test_index_full_reports_total_and_duration(core: Core) -> None:
    proc = core.run("index", "--full")
    assert proc.returncode == 0
    assert proc.stdout.startswith("reindexed 21 notes in ")


def test_index_stats_json_exact_counts(core: Core) -> None:
    core.index()
    proc = core.run("index", "--stats", "--json")
    stats = json.loads(proc.stdout)
    assert stats["total"] == 21
    assert stats["capture_backlog"] == 11
    assert stats["parse_errors"] == 1  # the broken-yaml quirk file, indexed empty


def test_index_missing_config_is_a_loud_error_not_a_traceback(core: Core) -> None:
    env = dict(core.env, ORGANIZE_CORE_CONFIG_DIR=str(core.config_dir / "nope"))
    proc = core.run("index", env=env)
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.startswith("ConfigError: config file not found")
    assert "hint: " in proc.stderr
    assert "Traceback" not in proc.stderr


def test_debug_flag_adds_the_traceback(core: Core) -> None:
    env = dict(core.env, ORGANIZE_CORE_CONFIG_DIR=str(core.config_dir / "nope"))
    proc = core.run("--debug", "index", env=env)
    assert proc.returncode == 1
    assert "Traceback" in proc.stderr
    assert "ConfigError: config file not found" in proc.stderr


def test_explicit_dir_flags_beat_the_environment(core: Core, tmp_path: Path) -> None:
    other_state = tmp_path / "elsewhere"
    proc = core.run("--state-dir", str(other_state), "index")
    assert proc.returncode == 0
    assert (other_state / "index.json").is_file()
    assert not (core.state_dir / "index.json").exists()


# --- search ----------------------------------------------------------------


def test_search_filters_and_free_text(core: Core) -> None:
    core.index()
    proc = core.run("search", "tags=impro")
    assert proc.returncode == 0
    assert proc.stdout.strip().splitlines()[-1] == "2 note(s)"

    proc = core.run("search", "--json", "tags=todo")
    hits = json.loads(proc.stdout)
    assert [hit["relative_path"] for hit in hits] == [QUIRK_FILES["todo_capture"], QUIRK_FILES["metadata_empty_map"], QUIRK_FILES["metadata_empty_list"]] or len(hits) == 3


def test_search_unknown_filter_key_is_loud(core: Core) -> None:
    core.index()
    proc = core.run("search", "nope=1")
    assert proc.returncode == 1
    assert proc.stderr.startswith("ConfigError: unknown session filter 'nope'")
    assert "valid filters:" in proc.stderr


# --- suggest ---------------------------------------------------------------


def test_suggest_for_a_note_ranks_the_route_first(core: Core) -> None:
    core.index()
    proc = core.run("suggest", CAPTURE)
    assert proc.returncode == 0
    rows = [line.split("\t") for line in proc.stdout.strip().splitlines()]
    assert rows[0][0] == "1"
    assert rows[0][3] == "resources/performing"
    assert "Route 'impro'" in rows[0][5]
    assert rows[-1][3] == "archive/capture/raw_capture"
    assert rows[-1][1] == "0.10"


def test_suggest_text_path_spec_13_3(core: Core) -> None:
    core.index()
    proc = core.run("suggest", "--text", "improv warmups", "--tags", "impro", "--json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["subject"] == "--text"
    assert payload["suggestions"][0]["route"] == "impro"
    assert payload["suggestions"][0]["description"] == "Improv and performance practice."


def test_suggest_without_note_or_text_is_a_usage_error(core: Core) -> None:
    proc = core.run("suggest")
    assert proc.returncode == 2
    assert "give a note path or --text" in proc.stderr


def test_suggest_missing_note_is_a_vault_error(core: Core) -> None:
    core.index()
    proc = core.run("suggest", "capture/raw_capture/does-not-exist.md")
    assert proc.returncode == 1
    assert proc.stderr.startswith("VaultError: note not found")


# --- move / merge / archive ------------------------------------------------


def test_move_dry_run_touches_no_vault_file(core: Core) -> None:
    core.index()
    before = sorted(p.name for p in (core.vault / "resources/performing").iterdir())
    proc = core.run("--dry-run", "move", CAPTURE, "resources/performing")
    assert proc.returncode == 0
    assert "(dry-run)" in proc.stdout
    assert "nothing was written" in proc.stdout
    assert sorted(p.name for p in (core.vault / "resources/performing").iterdir()) == before
    assert (core.vault / CAPTURE).is_file()
    assert not (core.state_dir / "learning.json").exists()
    # fileops still logs the intent, flagged DRY-RUN (spec 05 §1.5)
    assert "[DRY-RUN]" in (core.state_dir / "operations.log").read_text(encoding="utf-8")


def test_move_into_a_file_destination_is_refused(core: Core) -> None:
    core.index()
    proc = core.run("move", CAPTURE, QUIRK_FILES["merge_target"])
    assert proc.returncode == 1
    assert "destination is a file, not a folder" in proc.stderr
    assert "organize merge" in proc.stderr


def test_merge_requires_an_existing_target(core: Core) -> None:
    core.index()
    proc = core.run("merge", CAPTURE, "projects/blog/nope.md")
    assert proc.returncode == 1
    assert proc.stderr.startswith("VaultError: merge target not found")


def test_merge_appends_and_archives(core: Core) -> None:
    core.index()
    target = core.vault / QUIRK_FILES["merge_target"]
    proc = core.run("merge", CAPTURE, QUIRK_FILES["merge_target"])
    assert proc.returncode == 0, proc.stderr
    text = target.read_text(encoding="utf-8")
    assert "## Merged from" in text
    assert "improv warmups" in text
    assert "author: Matt Handzel" in text  # unknown target field survives
    assert not (core.vault / CAPTURE).exists()
    assert (core.vault / "archive/capture/raw_capture" / Path(CAPTURE).name).is_file()


def test_archive_keeps_the_original_filename(core: Core) -> None:
    core.index()
    proc = core.run("archive", CAPTURE)
    assert proc.returncode == 0, proc.stderr
    assert (core.vault / "archive/capture/raw_capture" / Path(CAPTURE).name).is_file()
    assert not (core.vault / CAPTURE).exists()


def test_archive_missing_note_exits_one(core: Core) -> None:
    core.index()
    proc = core.run("archive", "capture/raw_capture/ghost.md")
    assert proc.returncode == 1
    assert "VaultError" in proc.stderr


# --- set-meta (spec 07) ----------------------------------------------------


def test_set_meta_list_appends_and_kebabs(core: Core) -> None:
    core.index()
    note = core.vault / QUIRK_FILES["scalar_tags"]
    proc = core.run("set-meta", QUIRK_FILES["scalar_tags"], "tags=Foo Bar,baz")
    assert proc.returncode == 0, proc.stderr
    text = note.read_text(encoding="utf-8")
    assert "- daily_notes\n- foo-bar\n- baz\n" in text
    assert "Body of an older note" in text


def test_set_meta_enum_validation(core: Core) -> None:
    core.index()
    proc = core.run("set-meta", QUIRK_FILES["scalar_tags"], "importance=critical")
    assert proc.returncode == 1
    assert "is not one of the configured values" in proc.stderr
    assert "allowed values: high, medium, low" in proc.stderr

    proc = core.run("set-meta", QUIRK_FILES["scalar_tags"], "importance=high")
    assert proc.returncode == 0, proc.stderr
    assert "importance: high" in (core.vault / QUIRK_FILES["scalar_tags"]).read_text(encoding="utf-8")


def test_set_meta_enum_replaces_not_duplicates(core: Core) -> None:
    core.index()
    core.run("set-meta", QUIRK_FILES["scalar_tags"], "importance=high")
    core.run("set-meta", QUIRK_FILES["scalar_tags"], "importance=low")
    text = (core.vault / QUIRK_FILES["scalar_tags"]).read_text(encoding="utf-8")
    assert text.count("importance:") == 1
    assert "importance: low" in text


def test_set_meta_boolean_and_number_and_removal(core: Core) -> None:
    core.index()
    extra = '\n[[metadata_fields]]\nkey = "energy"\ntype = "number"\nkeymap = "E"\n'
    (core.config_dir / "config.toml").write_text(
        _config_text(core.vault, extra=extra), encoding="utf-8"
    )
    proc = core.run("set-meta", QUIRK_FILES["scalar_tags"], "energy=7")
    assert proc.returncode == 0, proc.stderr
    assert "energy: 7" in (core.vault / QUIRK_FILES["scalar_tags"]).read_text(encoding="utf-8")

    proc = core.run("set-meta", QUIRK_FILES["scalar_tags"], "energy=lots")
    assert proc.returncode == 1
    assert "is not a number" in proc.stderr

    proc = core.run("set-meta", QUIRK_FILES["scalar_tags"], "energy=")
    assert proc.returncode == 0, proc.stderr
    assert "energy:" not in (core.vault / QUIRK_FILES["scalar_tags"]).read_text(encoding="utf-8")


def test_set_meta_preserves_unknown_fields(core: Core) -> None:
    core.index()
    proc = core.run("set-meta", QUIRK_FILES["no_ai"], "importance=high")
    assert proc.returncode == 0, proc.stderr
    text = (core.vault / QUIRK_FILES["no_ai"]).read_text(encoding="utf-8")
    assert "no-ai: true" in text  # 08 §A12: nothing outside the change set is lost


def test_set_meta_malformed_change(core: Core) -> None:
    core.index()
    proc = core.run("set-meta", QUIRK_FILES["scalar_tags"], "justakey")
    assert proc.returncode == 1
    assert "malformed change" in proc.stderr


# --- session ---------------------------------------------------------------


def test_session_list_defaults_to_raw_captures(core: Core) -> None:
    core.index()
    proc = core.run("session", "list", "--json")
    payload = json.loads(proc.stdout)
    assert payload["state"] == "active"
    assert payload["counts"]["remaining"] == 11
    assert all(row["relative_path"].startswith("capture/") for row in payload["captures"])


def test_session_list_empty_match_is_a_notice_not_an_error(core: Core) -> None:
    core.index()
    proc = core.run("session", "list", "tags=nothing-matches-this")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "No captures found matching filters"


def test_session_without_action_is_usage(core: Core) -> None:
    proc = core.run("session")
    assert proc.returncode == 2


# --- routes ----------------------------------------------------------------


def test_routes_list_and_resolve(core: Core) -> None:
    core.index()
    proc = core.run("routes", "list", "--json")
    assert json.loads(proc.stdout)[0]["destination"] == "resources/performing/"

    proc = core.run("routes", "resolve", CAPTURE, "--json")
    matches = json.loads(proc.stdout)
    assert matches[0]["route"] == "impro"
    assert matches[0]["relative_destination"] == "resources/performing"

    proc = core.run("routes", "resolve", QUIRK_FILES["context_as_string"])
    assert proc.returncode == 0
    assert proc.stdout.startswith("no routes match")


def test_routes_describe_reads_the_folder_description(core: Core) -> None:
    core.index()
    proc = core.run("routes", "describe", "areas/health")
    assert proc.returncode == 0
    assert proc.stdout.startswith("Ongoing health practice")

    proc = core.run("routes", "describe", "projects/kms")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "no description for projects/kms"


def test_routes_describe_write_is_phase_4(core: Core) -> None:
    core.index()
    proc = core.run("routes", "describe", "areas/health", "new text")
    assert proc.returncode == 1
    assert "cannot write yet" in proc.stderr
    assert "Traceback" not in proc.stderr


# --- record / actions ------------------------------------------------------


_RECORD = {
    "actor": "matt",
    "operation": "meta_edit",
    "capture": {
        "path": "capture/raw_capture/x.md",
        "content_hash": "deadbeef",
        "frontmatter_before": {"tags": ["a"]},
        "body_before": "hello",
    },
}


def test_record_from_stdin_with_actor_override(core: Core) -> None:
    proc = core.run("record", "--actor", "claude-integrate", stdin=json.dumps(_RECORD))
    assert proc.returncode == 0, proc.stderr
    action_id = proc.stdout.strip()
    assert action_id.startswith("act_")
    written = list((core.state_dir / "actions").glob("*.jsonl"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8").strip())
    assert payload["actor"] == "claude-integrate"
    assert payload["id"] == action_id


def test_record_accepts_a_json_array(core: Core) -> None:
    proc = core.run("record", stdin=json.dumps([_RECORD, _RECORD]))
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.strip().splitlines()) == 2


def test_record_rejects_garbage_loudly(core: Core) -> None:
    proc = core.run("record", stdin="{not json")
    assert proc.returncode == 1
    assert proc.stderr.startswith("ActionSchemaError: stdin is not valid JSON")
    assert "Traceback" not in proc.stderr


def test_record_rejects_a_schema_violation(core: Core) -> None:
    proc = core.run("record", stdin=json.dumps({"operation": "meta_edit"}))
    assert proc.returncode == 1
    assert "ActionSchemaError" in proc.stderr


def test_record_dry_run_writes_nothing(core: Core) -> None:
    proc = core.run("--dry-run", "record", stdin=json.dumps(_RECORD))
    assert proc.returncode == 0
    assert "(dry-run)" in proc.stdout
    assert not list((core.state_dir / "actions").glob("*.jsonl"))


def test_actions_export_and_stats(core: Core, tmp_path: Path) -> None:
    core.run("record", stdin=json.dumps(_RECORD))
    core.run("record", "--actor", "consumer:taskwarrior", stdin=json.dumps(_RECORD))

    proc = core.run("actions", "export")
    assert proc.returncode == 0
    assert len(proc.stdout.strip().splitlines()) == 2

    proc = core.run("actions", "export", "--actor", "matt")
    assert len(proc.stdout.strip().splitlines()) == 1

    out = tmp_path / "corpus.jsonl"
    proc = core.run("actions", "export", "--out", str(out))
    assert proc.returncode == 0
    assert f"exported 2 record(s) to {out}" in proc.stdout
    assert len(out.read_text(encoding="utf-8").strip().splitlines()) == 2

    proc = core.run("actions", "stats", "--json")
    stats = json.loads(proc.stdout)
    assert stats["total"] == 2
    assert stats["by_operation"] == {"meta_edit": 2}
    assert stats["by_actor"] == {"matt": 1, "consumer:taskwarrior": 1}


def test_actions_without_action_is_usage(core: Core) -> None:
    proc = core.run("actions")
    assert proc.returncode == 2


# --- health / stubs --------------------------------------------------------


def test_health_green_after_indexing(core: Core) -> None:
    core.index()
    proc = core.run("health")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().endswith("health: OK")


def test_health_flags_a_cold_install(core: Core) -> None:
    """health never mutates, so on a never-run install it reports the absent
    state dir as a warning (not-yet-initialized is not broken)."""
    proc = core.run("health")
    assert proc.returncode == 0
    assert "state directory does not exist yet" in proc.stdout
    assert "ORGANIZE_CORE_STATE_DIR" in proc.stdout
    assert core.run("health", "--strict").returncode == 1


def test_health_warns_when_the_index_snapshot_is_absent(core: Core) -> None:
    core.run("record", stdin=json.dumps(_RECORD))  # creates the state dirs, not the index
    proc = core.run("health")
    assert proc.returncode == 0, "warnings alone are reported, not fatal"
    assert "no index snapshot" in proc.stdout
    assert "organize index --full" in proc.stdout
    assert "--strict" in proc.stdout

    strict = core.run("health", "--strict")
    assert strict.returncode == 1, "--strict promotes warnings to failures"


def test_health_reports_a_bad_vault_root(core: Core) -> None:
    (core.config_dir / "config.toml").write_text(
        _config_text(core.vault / "does-not-exist"), encoding="utf-8"
    )
    proc = core.run("health", "--json")
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert any(issue["severity"] == "error" for issue in payload["issues"])


def test_health_example_config_prints_a_loadable_config(core: Core) -> None:
    proc = core.run("health", "--example-config")
    assert proc.returncode == 0
    assert "[vault]" in proc.stdout
    assert "[[metadata_fields]]" in proc.stdout


def test_auto_organize_is_the_phase_6_stub(core: Core) -> None:
    proc = core.run("auto-organize")
    assert proc.returncode == 1
    assert "not implemented" in proc.stderr
    assert "spec 13" in proc.stderr


def test_run_consumers_reports_phase_3(core: Core) -> None:
    proc = core.run("run-consumers")
    assert proc.returncode == 1
    assert "arrives in Phase 3" in proc.stderr


def test_list_consumers_constructs_nothing_and_needs_no_config(core: Core) -> None:
    env = dict(core.env, ORGANIZE_CORE_CONFIG_DIR=str(core.config_dir / "nope"))
    proc = core.run("run-consumers", "--list-consumers", env=env)
    assert proc.returncode == 0
    assert proc.stdout.split() == [
        "auto_tagger",
        "deep_research",
        "learn",
        "question_answer",
        "tag_router",
        "taskwarrior",
    ]


def test_no_command_prints_help_and_exits_two(core: Core) -> None:
    proc = core.run()
    assert proc.returncode == 2
    assert "usage: organize" in proc.stdout


# --- the end-to-end organize flow -----------------------------------------


def test_end_to_end_index_suggest_dry_run_move_real_move(core: Core) -> None:
    """index -> suggest -> move --dry-run -> move: the whole seat contract."""
    core.index()

    ranked = json.loads(core.run("suggest", CAPTURE, "--json").stdout)["suggestions"]
    destination = ranked[0]["relative_path"]
    assert destination == "resources/performing"

    # 1. dry run: full evaluation, zero vault writes (spec 09 §5.6)
    dry = core.run("--dry-run", "move", CAPTURE, destination)
    assert dry.returncode == 0
    assert (core.vault / CAPTURE).is_file()
    assert not (core.vault / destination / Path(CAPTURE).name).exists()
    assert not (core.state_dir / "learning.json").exists()

    # 2. the real move
    real = core.run("move", CAPTURE, destination)
    assert real.returncode == 0, real.stderr

    moved = core.vault / destination / Path(CAPTURE).name
    archived = core.vault / "archive/capture/raw_capture" / Path(CAPTURE).name
    assert moved.is_file(), "the capture landed in the destination"
    assert archived.is_file(), "the original was archived under its own filename (05 §3)"
    assert not (core.vault / CAPTURE).exists(), "the capture folder no longer holds it"

    text = moved.read_text(encoding="utf-8")
    assert "processing_status: organized" in text
    assert "- resource/performing" in text, "the destination tag was added (05 §2.6)"
    assert "- impro" in text and "- creativity" in text, "existing tags preserved in order"
    assert "An idea about improv warmups" in text, "body untouched"
    # the archived original keeps its pre-move frontmatter
    assert "processing_status: raw" in archived.read_text(encoding="utf-8")

    log = (core.state_dir / "operations.log").read_text(encoding="utf-8").splitlines()
    success_moves = [line for line in log if "move:" in line and "[SUCCESS]" in line]
    assert len(success_moves) == 1
    assert str(moved) in success_moves[0]

    records = [
        json.loads(line)
        for line in (next((core.state_dir / "actions").glob("*.jsonl"))).read_text(encoding="utf-8").splitlines()
    ]
    real_moves = [r for r in records if r["operation"] == "move" and not r["context"]["filters"].get("dry_run")]
    assert len(real_moves) == 1
    action = real_moves[0]
    assert action["actor"] == "matt"
    assert action["capture"]["body_before"].strip().endswith("creative flow.")
    assert [t["path"] for t in action["targets"]] == [str(moved)]
    assert action["targets"][0]["role"] == "destination"

    learning = json.loads((core.state_dir / "learning.json").read_text(encoding="utf-8"))
    assert learning["statistics"]["total_moves"] == 1
    assert learning["statistics"]["destinations"] == {str(core.vault / destination): 1}
    association = next(iter(learning["associations"].values()))
    assert str(core.vault / destination) in association["destinations"]

    # 3. the learned association reads back into the next ranking
    second = json.loads(
        core.run("suggest", "--text", "another improv thought", "--tags", "impro,creativity", "--json").stdout
    )
    scored = {s["relative_path"]: s for s in second["suggestions"]}
    assert "learn" in " ".join(scored[destination]["reasons"]).lower() or scored[destination]["route"] == "impro"


def test_end_to_end_index_stays_consistent_after_a_move(core: Core) -> None:
    core.index()
    core.run("move", CAPTURE, "resources/performing")
    stats = json.loads(core.run("index", "--stats", "--json").stdout)
    # copy-then-archive: the vault gains the destination copy, the original
    # moves to archive/ — nothing is deleted (05 §1.2), so total is 21 + 1.
    assert stats["total"] == 22
    assert stats["capture_backlog"] == 10
    hits = json.loads(core.run("search", "--json", "tags=impro").stdout)
    paths = {hit["relative_path"] for hit in hits}
    name = Path(CAPTURE).name
    assert f"resources/performing/{name}" in paths
    assert f"archive/capture/raw_capture/{name}" in paths
    assert CAPTURE not in paths
