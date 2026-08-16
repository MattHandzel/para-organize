"""``auto_tagger`` driven through the REAL pipeline (spec 06 §1 + 11 §2).

The unit suite lives in ``test_consumer_tagger.py``; this file drives the
actual ``run_consumers`` orchestrator, a real SQLite ``AutomationStore``, the
real ``OllamaClient`` and a 127.0.0.1 fake Ollama, twice, and asserts against
bytes on disk and the endpoint's request log.

What only an end-to-end run can hold down:

* the ``no-ai`` note is denied by the RUNNER (it is never offered to a
  ``uses_llm`` consumer at all), which is a different guard from the
  consumer's own — asserted by the model never seeing the note's text;
* run 2 changes nothing: no request, no vault byte, no new ActionRecord.
  Both halves of the idempotency story have to hold at once — the store's
  hash checkpoint AND the note's ``auto_tag: done`` marker — because the
  tagger's own write changes the hash the store checkpoints on;
* a rehearsal (``--dry-run``) spends no inference and writes nothing.

The one seam this file stands in for: ``RunContext`` does not carry the
``op_context`` the consumer needs yet (the integrator wires it). The shim
below builds the RunContext exactly as the runner does and attaches it, so
these assertions are the ones that will hold once the field lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from organize_core.actions import ActionRecorder
from organize_core.config import Config, ConsumerConfig, LLMConfig, VaultConfig
from organize_core.consumers import runner as runner_mod
from organize_core.consumers.base import RunContext
from organize_core.consumers.store import AutomationStore
from organize_core.fileops import OperationContext, OperationLog
from organize_core.index import VaultIndex
from organize_core.paths import CorePaths
from test_consumer_learn_fakes import http_endpoint, write_note

ACTOR = "consumer:auto_tagger"
SCOPE = "capture/raw_capture/tagger-e2e"
SECRET = "Automated tooling must never read or write this private thought."


def note_text(body: str, **fields) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(body)
    return "\n".join(lines) + "\n"


def seed(vault: Path) -> dict[str, Path]:
    """Three captures in one scope: one untagged, one already tagged, one
    protected by the vault law."""
    return {
        "untagged": write_note(
            vault,
            f"{SCOPE}/untagged.md",
            note_text("Improv warmups and creative flow.", id="e2e-untagged", processing_status="raw"),
        ),
        "tagged": write_note(
            vault,
            f"{SCOPE}/tagged.md",
            note_text("Already filed.", id="e2e-tagged", tags=["impro"], processing_status="raw"),
        ),
        "protected": write_note(
            vault,
            f"{SCOPE}/private.md",
            note_text(SECRET, id="e2e-private", processing_status="raw", **{"no-ai": "true"}),
        ),
    }


def pipeline_config(vault: Path, host: str) -> Config:
    return Config(
        vault=VaultConfig(root=vault),
        llm=LLMConfig(
            backend="ollama", ollama_host=host, ollama_model="fake-model", retries=0,
            timeout_seconds=5.0,
        ),
        consumers=[
            ConsumerConfig(
                name="auto_tagger",
                type="auto_tagger",
                include_paths=[SCOPE],
                options={"max_tags": 3},
            )
        ],
    )


def attach_op_context(
    monkeypatch: pytest.MonkeyPatch, config: Config, paths: CorePaths
) -> OperationContext:
    """Stand in for the integrator's ``RunContext.op_context`` wiring.

    Builds the RunContext exactly as ``runner.run_consumers`` does and hangs
    the OperationContext a composition root would supply off it. Setting the
    attribute (rather than passing it) is what keeps this test correct on
    both sides of the seam landing.
    """
    index = VaultIndex(config, paths.index_path)
    index.full_reindex()
    op_ctx = OperationContext(
        config=config,
        index=index,
        oplog=OperationLog(paths.operations_log),
        recorder=ActionRecorder(paths.actions_dir),
        backup_dir=Path(config.vault.root) / config.file_ops.backup_dir,
        actor=ACTOR,
    )

    def factory(**kwargs: object) -> RunContext:
        ctx = RunContext(**kwargs)  # type: ignore[arg-type]
        ctx.op_context = op_ctx  # type: ignore[attr-defined]
        return ctx

    monkeypatch.setattr(runner_mod, "RunContext", factory)
    return op_ctx


def run_once(config: Config, paths: CorePaths, *, dry_run: bool = False):
    with AutomationStore(paths.automations_db) as automations:
        automations.migrate()
        return runner_mod.run_consumers(config, automations, dry_run=dry_run, paths=paths)


def vault_bytes(vault: Path) -> dict[str, bytes]:
    """Every note, EXCLUDING ``.backups`` — a pre-write backup is a required
    artefact of the operation (05 §1.4), not a change to the vault's notes.
    It is asserted for on its own below."""
    return {
        str(path.relative_to(vault)): path.read_bytes()
        for path in sorted(vault.rglob("*.md"))
        if ".backups" not in path.parts
    }


def prompts_seen(endpoint) -> str:
    return "\n".join(
        json.loads(request["body"]).get("prompt", "")
        for request in endpoint.requests
        if request["body"]
    )


def test_the_pipeline_tags_the_untagged_capture_and_nothing_else(
    fixture_vault: Path, core_paths: CorePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes = seed(fixture_vault)
    core_paths.ensure_state_dirs()
    before = vault_bytes(fixture_vault)

    with http_endpoint() as endpoint:
        endpoint.queue_completion(json.dumps({"tags": ["impro", "creative-flow"]}))
        config = pipeline_config(fixture_vault, endpoint.base_url)
        attach_op_context(monkeypatch, config, core_paths)

        summary = run_once(config, core_paths)

        assert [request["path"] for request in endpoint.requests] == ["/api/generate"]
        assert SECRET not in prompts_seen(endpoint), "the no-ai note reached the model (02)"

    per_consumer = {entry.name: entry for entry in summary.consumers}["auto_tagger"]
    assert per_consumer.success == 1, summary
    assert per_consumer.no_ai == 1, "the protected note must be counted, not silently dropped"
    assert per_consumer.error == 0

    after = vault_bytes(fixture_vault)
    changed = [rel for rel in after if after[rel] != before.get(rel)]
    assert changed == [str(notes["untagged"].relative_to(fixture_vault))]

    written = list(ActionRecorder(core_paths.actions_dir).query())
    assert [record.actor for record in written] == [ACTOR]
    assert [record.operation for record in written] == ["meta_edit"]

    # The pre-write backup of the note we changed (05 §1.4) — the undo path
    # a machine-authored edit needs most.
    backups = list((fixture_vault / config.file_ops.backup_dir).glob("*untagged.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before[str(notes["untagged"].relative_to(fixture_vault))]


def test_a_second_run_changes_nothing(
    fixture_vault: Path, core_paths: CorePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06 §1 checkpointing plus 11 §4's ``auto_tag: done`` guard. The store
    alone cannot carry this: the tagger's own write changes the raw-text hash
    the store checkpoints on, so without the note-side marker run 2 would
    re-tag, and every run after it."""
    seed(fixture_vault)
    core_paths.ensure_state_dirs()

    with http_endpoint() as endpoint:
        endpoint.queue_completion(json.dumps({"tags": ["impro"]}))
        config = pipeline_config(fixture_vault, endpoint.base_url)
        attach_op_context(monkeypatch, config, core_paths)

        run_once(config, core_paths)
        after_first = vault_bytes(fixture_vault)
        requests_after_first = len(endpoint.requests)

        second = run_once(config, core_paths)

        assert len(endpoint.requests) == requests_after_first, "run 2 spent inference"

    assert vault_bytes(fixture_vault) == after_first
    per_consumer = {entry.name: entry for entry in second.consumers}["auto_tagger"]
    assert per_consumer.success == 0
    assert per_consumer.error == 0
    assert len(list(ActionRecorder(core_paths.actions_dir).query())) == 1


def test_a_rehearsal_spends_no_inference_and_writes_nothing(
    fixture_vault: Path, core_paths: CorePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(fixture_vault)
    core_paths.ensure_state_dirs()
    before = vault_bytes(fixture_vault)

    with http_endpoint() as endpoint:
        endpoint.queue_completion(json.dumps({"tags": ["impro"]}))
        config = pipeline_config(fixture_vault, endpoint.base_url)
        attach_op_context(monkeypatch, config, core_paths)

        summary = run_once(config, core_paths, dry_run=True)

        assert endpoint.requests == []

    assert vault_bytes(fixture_vault) == before
    assert list(ActionRecorder(core_paths.actions_dir).query(include_dry_run=True)) == []
    per_consumer = {entry.name: entry for entry in summary.consumers}["auto_tagger"]
    assert per_consumer.would_process == 1
    assert per_consumer.success == 0


def test_the_type_is_configurable_now_that_the_body_ships(fixture_vault: Path) -> None:
    """The Phase-4 gate in reverse: a config naming ``auto_tagger`` used to
    fail validation by name. Constructing the consumer from a plain config
    section must now succeed."""
    from organize_core.consumers.auto_tagger import AutoTaggerConsumer

    entry = ConsumerConfig(name="auto_tagger", type="auto_tagger")
    assert isinstance(AutoTaggerConsumer(entry), AutoTaggerConsumer)
