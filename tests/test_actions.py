"""ActionRecord schema, JSON round-trip, and ULID id tests (spec 12 §2).

Exact-value assertions only — the old suite's ``> 0`` habit (spec 09 §3) is
what let a broken product ship green.
"""

from __future__ import annotations

import json

import pytest

from organize_core.actions import (
    ACTIONS_SCHEMA_VERSION,
    ActionContext,
    ActionRecord,
    ActionSchemaError,
    CaptureState,
    LLMTrace,
    SuggestionShown,
    TargetState,
    new_action_id,
)
from organize_core.errors import OrganizeError

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def decode_ulid_ms(action_id: str) -> int:
    """Decode the 48-bit millisecond timestamp out of ``act_<ulid>``."""
    ulid = action_id.removeprefix("act_")
    value = 0
    for ch in ulid:
        value = (value << 5) | CROCKFORD.index(ch)
    return value >> 80


def maximal_record() -> ActionRecord:
    """A record exercising every field in the spec 12 §2 schema."""
    return ActionRecord(
        id="act_01J0000000000000000000000A",
        ts="2026-08-15T17:40:00Z",
        actor="claude-integrate",
        operation="integrate",
        capture=CaptureState(
            path="capture/raw_capture/meeting notes — café ☕.md",
            content_hash="sha256:abc123",
            frontmatter_before={
                "id": "unicode-cafe",
                "tags": ["meeting"],
                "location": {"city": "New York", "latitude": 40.7126},
                "metadata": {},
            },
            body_before="Body with ☕ and a --- horizontal rule\nand a second line.\n",
            frontmatter_after={"id": "unicode-cafe", "tags": ["meeting", "organized"]},
        ),
        targets=(
            TargetState(
                path="projects/blog/ideas.md",
                role="merge_target",
                before_hash="sha256:before1",
                after_hash="sha256:after1",
                diff="@@ -1 +1,2 @@\n line\n+added\n",
                description="Blog post ideas and drafts",
            ),
            TargetState(
                path="areas/health/index.md",
                role="append_target",
                before_hash=None,
                after_hash="sha256:after2",
                diff="@@ -0,0 +1 @@\n+appended\n",
            ),
        ),
        context=ActionContext(
            session_id="ses_20260815T174000",
            filters={"tags": ["impro"], "limit": 20},
            suggestions_shown=(
                SuggestionShown(path="projects/blog", score=3.25, rank=1, reasons=("tag", "recency")),
                SuggestionShown(path="areas/health", score=1.5, rank=2),
            ),
            chosen_rank=2,
            route="workout",
            auto_tags_present=("auto/impro",),
            vault_stats={"capture_backlog": 2299, "index_size": 7516},
            durations_ms={"decision": 8400, "operation": 120},
        ),
        edit_mode="integrate",
        llm=LLMTrace(
            backend="claude-cli",
            model="claude-opus",
            prompt_hash="sha256:prompt",
            proposed_diff="+proposed\n",
            final_diff="+final\n",
            verdict="edited",
        ),
    )


def minimal_record() -> ActionRecord:
    return ActionRecord(
        id="act_01J0000000000000000000000B",
        ts="2026-08-15T18:00:00Z",
        actor="matt",
        operation="skip",
        capture=CaptureState(
            path="capture/raw_capture/plain.md",
            content_hash="sha256:zero",
            frontmatter_before={},
            body_before="",
        ),
    )


# --- schema shape ----------------------------------------------------------


def test_to_json_has_exactly_the_spec_12_2_top_level_fields_in_order() -> None:
    payload = maximal_record().to_json()
    assert list(payload) == [
        "schema_version",
        "id",
        "ts",
        "actor",
        "operation",
        "edit_mode",
        "capture",
        "targets",
        "context",
        "llm",
    ]
    assert payload["schema_version"] == ACTIONS_SCHEMA_VERSION == 1


def test_to_json_block_fields_match_the_spec_field_for_field() -> None:
    payload = maximal_record().to_json()
    assert list(payload["capture"]) == [
        "path",
        "content_hash",
        "frontmatter_before",
        "body_before",
        "frontmatter_after",
    ]
    assert list(payload["targets"][0]) == [
        "path",
        "role",
        "before_hash",
        "after_hash",
        "diff",
        # spec 12 §2 annotates `diff` with "full before-text stored when the
        # file is new or small (<64 KB)"; that text is a field of its own,
        # next to the diff it completes.
        "before_text",
        "description",
    ]
    assert list(payload["context"]) == [
        "session_id",
        "filters",
        "suggestions_shown",
        "chosen_rank",
        "route",
        "auto_tags_present",
        "vault_stats",
        "durations_ms",
        "dry_run",
    ]
    assert list(payload["context"]["suggestions_shown"][0]) == ["path", "score", "rank", "reasons"]
    assert list(payload["llm"]) == [
        "backend",
        "model",
        "prompt_hash",
        "proposed_diff",
        "final_diff",
        "verdict",
    ]


def test_to_json_values_are_exact() -> None:
    payload = maximal_record().to_json()
    assert payload["actor"] == "claude-integrate"
    assert payload["operation"] == "integrate"
    assert payload["edit_mode"] == "integrate"
    assert payload["capture"]["body_before"] == (
        "Body with ☕ and a --- horizontal rule\nand a second line.\n"
    )
    assert payload["capture"]["frontmatter_before"]["location"]["city"] == "New York"
    assert len(payload["targets"]) == 2
    assert payload["targets"][1]["before_hash"] is None
    assert payload["targets"][1]["description"] is None
    assert payload["context"]["chosen_rank"] == 2
    assert payload["context"]["suggestions_shown"][0]["reasons"] == ["tag", "recency"]
    assert payload["context"]["suggestions_shown"][1]["reasons"] == []
    assert payload["context"]["vault_stats"] == {"capture_backlog": 2299, "index_size": 7516}
    assert payload["llm"]["verdict"] == "edited"
    assert payload["llm"]["proposed_diff"] != payload["llm"]["final_diff"]


def test_optional_blocks_serialize_as_null_not_omitted() -> None:
    payload = minimal_record().to_json()
    assert payload["edit_mode"] is None
    assert payload["llm"] is None
    assert payload["capture"]["frontmatter_after"] is None
    assert payload["targets"] == []
    assert payload["context"]["suggestions_shown"] == []
    assert payload["context"]["chosen_rank"] is None


def test_to_json_returns_copies_not_aliases() -> None:
    record = maximal_record()
    payload = record.to_json()
    payload["capture"]["frontmatter_before"]["tags"].append("mutated")
    payload["context"]["vault_stats"]["index_size"] = 0
    payload["context"]["filters"]["limit"] = 999
    assert record.capture.frontmatter_before["tags"] == ["meeting"]
    assert record.context.vault_stats["index_size"] == 7516
    assert record.context.filters["limit"] == 20


# --- round trip ------------------------------------------------------------


@pytest.mark.parametrize("factory", [maximal_record, minimal_record], ids=["maximal", "minimal"])
def test_round_trip_through_json_text_is_identity(factory) -> None:
    original = factory()
    text = json.dumps(original.to_json(), ensure_ascii=False)
    assert "\n" not in text  # one JSONL line, always
    restored = ActionRecord.from_json(json.loads(text))
    assert restored == original
    assert restored.to_json() == original.to_json()


def test_round_trip_preserves_tuple_types() -> None:
    restored = ActionRecord.from_json(maximal_record().to_json())
    assert isinstance(restored.targets, tuple)
    assert isinstance(restored.context.suggestions_shown, tuple)
    assert isinstance(restored.context.suggestions_shown[0].reasons, tuple)
    assert isinstance(restored.context.auto_tags_present, tuple)


def test_round_trip_survives_invalid_utf8_replacement_and_control_chars() -> None:
    body = b"caf\xe9 raw bytes".decode("utf-8", errors="replace") + "\ttab\r\nnewline"
    record = ActionRecord(
        id="act_x",
        ts="2026-08-15T00:00:00Z",
        actor="matt",
        operation="move",
        capture=CaptureState(path="a.md", content_hash="h", frontmatter_before={}, body_before=body),
    )
    text = json.dumps(record.to_json(), ensure_ascii=False)
    assert "\n" not in text
    assert ActionRecord.from_json(json.loads(text)).capture.body_before == body


# --- strict parse ----------------------------------------------------------


@pytest.mark.parametrize("missing", ["id", "ts", "actor", "operation", "capture"])
def test_from_json_rejects_missing_required_field_naming_it(missing: str) -> None:
    payload = minimal_record().to_json()
    del payload[missing]
    with pytest.raises(ActionSchemaError) as excinfo:
        ActionRecord.from_json(payload)
    assert missing in str(excinfo.value)


def test_from_json_rejects_missing_capture_path() -> None:
    payload = minimal_record().to_json()
    del payload["capture"]["path"]
    with pytest.raises(ActionSchemaError) as excinfo:
        ActionRecord.from_json(payload)
    assert "capture.path" in str(excinfo.value)


def test_schema_error_is_both_taxonomy_and_value_error() -> None:
    assert issubclass(ActionSchemaError, OrganizeError)
    assert issubclass(ActionSchemaError, ValueError)
    with pytest.raises(OrganizeError):
        ActionRecord.from_json({})
    with pytest.raises(ValueError):
        ActionRecord.from_json({})


@pytest.mark.parametrize(
    ("field_path", "bad_value", "needle"),
    [
        (("operation",), "teleport", "operation"),
        (("edit_mode",), "telepathy", "edit_mode"),
        (("targets", 0, "role"), "sidekick", "role"),
        (("llm", "verdict"), "shrug", "verdict"),
    ],
)
def test_from_json_rejects_unknown_enum_values(field_path, bad_value: str, needle: str) -> None:
    payload = maximal_record().to_json()
    target = payload
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = bad_value
    with pytest.raises(ActionSchemaError) as excinfo:
        ActionRecord.from_json(payload)
    assert needle in str(excinfo.value)


def test_from_json_rejects_wrong_types() -> None:
    payload = minimal_record().to_json()
    payload["targets"] = {"not": "a list"}
    with pytest.raises(ActionSchemaError):
        ActionRecord.from_json(payload)

    payload = minimal_record().to_json()
    payload["schema_version"] = "1"
    with pytest.raises(ActionSchemaError):
        ActionRecord.from_json(payload)

    payload = minimal_record().to_json()
    payload["context"]["chosen_rank"] = "second"
    with pytest.raises(ActionSchemaError):
        ActionRecord.from_json(payload)


def test_from_json_tolerates_newer_schema_version_and_unknown_keys() -> None:
    """Forward compat for the query path (12 §2 append-only corpus)."""
    payload = minimal_record().to_json()
    payload["schema_version"] = 99
    payload["operation"] = "teleport"  # a future operation
    payload["brand_new_block"] = {"anything": [1, 2, 3]}
    payload["context"]["future_field"] = "ok"
    restored = ActionRecord.from_json(payload)
    assert restored.schema_version == 99
    assert restored.operation == "teleport"
    assert restored.id == payload["id"]


def test_from_json_ignores_unknown_keys_at_current_version() -> None:
    payload = minimal_record().to_json()
    payload["written_by"] = "some future client"
    restored = ActionRecord.from_json(payload)
    assert restored == minimal_record()


# --- action ids ------------------------------------------------------------


def test_new_action_id_format() -> None:
    action_id = new_action_id()
    assert action_id.startswith("act_")
    ulid = action_id.removeprefix("act_")
    assert len(ulid) == 26
    assert set(ulid) <= set(CROCKFORD)
    assert not (set(ulid) & set("ILOU"))  # Crockford excludes these


def test_new_action_id_encodes_the_injected_timestamp() -> None:
    now = 1_786_000_000.123
    assert decode_ulid_ms(new_action_id(now=now)) == int(now * 1000)


def test_new_action_ids_are_unique_and_sortable_under_rapid_calls() -> None:
    """08 §A25 regression: same-instant generation must not collide."""
    ids = [new_action_id() for _ in range(5000)]
    assert len(set(ids)) == 5000
    assert ids == sorted(ids)


def test_new_action_ids_are_unique_for_an_identical_injected_now() -> None:
    ids = [new_action_id(now=1_786_000_000.0) for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert ids == sorted(ids)
    assert {decode_ulid_ms(i) for i in ids} == {1_786_000_000_000}


def test_new_action_id_is_thread_safe() -> None:
    import threading

    produced: list[list[str]] = []
    lock = threading.Lock()

    def worker() -> None:
        local = [new_action_id() for _ in range(500)]
        with lock:
            produced.append(local)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    flat = [i for chunk in produced for i in chunk]
    assert len(flat) == 2000
    assert len(set(flat)) == 2000
