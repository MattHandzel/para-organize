"""``tag_router`` consumer — the unattended half of spec 11 §1.

This suite owns the CONSUMER's contract. ``tests/test_routes.py`` owns
``resolve``/``apply_all`` themselves; nothing here re-tests route matching or
the archive-exactly-once rule for its own sake — it tests that the consumer
delegates to them and translates their outcomes into the doc-06 emission
vocabulary correctly.

The three laws the module docstring names, each pinned below:

1. ``auto = false`` is the consent gate AND a FILTER (never a checkpointed
   skip), so flipping a route to ``auto = true`` fires next run at an
   unchanged note hash. Pinned by ``test_flipping_auto_true_fires_at_an_
   unchanged_note_hash``, which is the retroactivity property that a
   proposal-emission design would have silently destroyed.
2. ``no-ai: true`` captures are refused in EVERY mode, including the purely
   mechanical ones, and refused BEFORE the route lookup so the answer cannot
   depend on which routes happen to exist.
3. No file effect lives in the consumer — every mutation goes through
   ``routes.apply_all`` with a real ``OperationContext``.

Anti-vacuity: every "nothing happened" assertion in this file is paired with
a FIRING CONTROL — the same assertion against a run that DID act — so a test
cannot pass because the harness was inert. The two that matter most are the
``learning.json`` trap (byte-identical after a routed run, and demonstrably
NOT byte-identical after a real move) and the dry-run test (both store tables
empty, against a control where they are not).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import QUIRK_FILES  # noqa: E402  (tests/ is on sys.path)
from organize_core import routes as routes_mod
from organize_core.actions import ActionRecorder
from organize_core.config import (
    Config,
    ConsumerConfig,
    RouteConfig,
    VaultConfig,
)
from organize_core.consumers.base import NotePayload, RunContext, Status
from organize_core.consumers.runner import run_consumers, scan_notes
from organize_core.consumers.store import AutomationStore
from organize_core.consumers.tag_router import ACTOR, TagRouterConsumer
from organize_core.fileops import OperationContext, OperationLog
from organize_core.index import VaultIndex
from organize_core.paths import CorePaths

#: Sentinel distinguishing "harness default" from an explicit ``None``.
_UNSET: Any = object()

CAPTURE_DIR = "capture/raw_capture"
TRAINING_LOG = "areas/health/training-log.md"
IDEAS = QUIRK_FILES["merge_target"]  # projects/blog/ideas.md
PERFORMING = "resources/performing/"


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def route(
    tags: list[str],
    destination: str,
    *,
    mode: str = "append",
    auto: bool = False,
    description: str = "",
    template: str | None = None,
) -> RouteConfig:
    return RouteConfig(
        tags=tags,
        destination=destination,
        mode=mode,  # type: ignore[arg-type]
        auto=auto,
        description=description,
        template=template,
    )


def make_config(vault: Path, *routes: RouteConfig, **kwargs: Any) -> Config:
    """A Config with routes. Built directly rather than through
    ``validate_config`` so a test can express a route combination the TOML
    layer would also accept, without maintaining a second copy of the schema."""
    return Config(
        vault=VaultConfig(root=vault),
        routes=list(routes),
        consumers=list(kwargs.pop("consumers", ()) or ()),
        **kwargs,
    )


def write_capture(
    vault: Path,
    name: str,
    *,
    tags: list[str] | None = None,
    auto_tags: list[str] | None = None,
    no_ai: bool = False,
    body: str = "A captured thought worth filing somewhere.",
) -> Path:
    """A capture in ``capture/raw_capture`` with the current-schema shape."""
    lines = ["---", f"id: '{name}'", f"capture_id: '{name}'"]
    if no_ai:
        lines.append("no-ai: true")
    lines.append("tags:")
    for tag in tags or []:
        lines.append(f"- {tag}")
    if not (tags or []):
        lines[-1] = "tags: []"
    if auto_tags is not None:
        lines.append("auto_tags:")
        for tag in auto_tags:
            lines.append(f"- {tag}")
        if not auto_tags:
            lines[-1] = "auto_tags: []"
    lines += ["processing_status: raw", "---", body, ""]
    path = vault / CAPTURE_DIR / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_target(vault: Path, rel: str, body: str = "# Log\n\nExisting content.\n") -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: {path.stem}\ntags:\n- log\n---\n{body}", encoding="utf-8")
    return path


@pytest.fixture()
def paths(core_paths: CorePaths) -> CorePaths:
    core_paths.ensure_state_dirs()
    return core_paths


def payload_for(config: Config, path: Path) -> NotePayload:
    """The payload the REAL ingestion walk would hand the consumer — never a
    hand-built one, so hash/rel/no_ai all come from the shipped code path."""
    resolved = path.resolve()
    for payload in scan_notes(config):
        if payload.path == resolved:
            return payload
    raise AssertionError(f"{path} was not produced by scan_notes")


def consumer(config: Config | None = None, *, name: str = "router") -> TagRouterConsumer:
    """A constructed consumer, optionally already bound to a run."""
    instance = TagRouterConsumer(ConsumerConfig(name=name, type="tag_router"))
    if config is not None:
        instance.bind(RunContext(config=config))
    return instance


def root_op_context(
    config: Config,
    paths: CorePaths,
    *,
    dry_run: bool = False,
    index: VaultIndex | None = None,
) -> OperationContext:
    """What the COMPOSITION ROOT builds — ``cli._op_context``, field for field.

    Including ``on_record``, wired to the REAL production callback
    (``cli._learn_from_action``). That is deliberate and load-bearing: since
    the Phase-5 filter moved into ``learn.record_action``, "learning folds
    nothing from a route firing" is a property of the CALLEE, and a harness
    that quietly left ``on_record`` unset would prove nothing about it. Every
    test in this file therefore drives a context whose learning callback is
    live.
    """
    from organize_core.cli import _learn_from_action

    idx = index if index is not None else VaultIndex(config, paths.index_path)
    if index is None:
        idx.load()
    return OperationContext(
        config=config,
        index=idx,
        oplog=OperationLog(paths.operations_log),
        recorder=ActionRecorder(paths.actions_dir),
        backup_dir=Path(config.vault.root) / config.file_ops.backup_dir,
        dry_run=dry_run,
        actor="matt",  # the root's default; the runner re-actors per consumer
        describe=lambda folder: routes_mod.get_description(folder, idx, config),
        on_record=lambda record: _learn_from_action(paths, config, record),
    )


def consumer_op_context(
    config: Config,
    paths: CorePaths,
    *,
    dry_run: bool = False,
    index: VaultIndex | None = None,
) -> OperationContext:
    """The per-consumer view the RUNNER hands ``tag_router``.

    ``runner.run_consumers`` does exactly this:
    ``dataclasses.replace(op_context, actor=f"consumer:{entry.type}",
    dry_run=dry_run)`` — the actor is the consumer TYPE, never the config
    section name (ARCHITECTURE Phase-4 integration landing: "section-name
    would misattribute every doc-12 record"). Spelled with a LITERAL here so
    a flipped ``ACTOR`` constant cannot make the suite agree with itself.
    """
    return dataclasses.replace(
        root_op_context(config, paths, dry_run=dry_run, index=index),
        actor="consumer:tag_router",
        dry_run=dry_run,
    )


def run_context(
    config: Config,
    paths: CorePaths,
    *,
    dry_run: bool = False,
    op_context: OperationContext | None = _UNSET,  # type: ignore[assignment]
) -> RunContext:
    """The ``RunContext`` the runner builds, op_context included.

    Pass ``op_context=None`` explicitly for the one test that pins the
    no-recorded-write-path refusal; everything else gets the real wiring.
    """
    ctx_op = (
        consumer_op_context(config, paths, dry_run=dry_run)
        if op_context is _UNSET
        else op_context
    )
    return RunContext(config=config, dry_run=dry_run, paths=paths, op_context=ctx_op)


def archived_copies(vault: Path, stem: str) -> list[Path]:
    archive_root = vault / "archive" / CAPTURE_DIR
    if not archive_root.is_dir():
        return []
    return sorted(p for p in archive_root.rglob("*.md") if p.stem.startswith(stem))


# ---------------------------------------------------------------------------
# construction + option validation (06 §1: constructors are PURE)
# ---------------------------------------------------------------------------


def test_the_type_is_registered_and_now_implemented() -> None:
    """The Phase-4 gate: config validation refuses types whose bodies have
    not landed, so ``implemented`` flipping is what makes ``tag_router`` a
    legal ``type`` value at all."""
    from organize_core.consumers import get_implemented_consumer_types

    assert "tag_router" in get_implemented_consumer_types()
    assert TagRouterConsumer.implemented is True


def test_constructor_is_pure_and_takes_no_options() -> None:
    from organize_core.errors import ConfigError

    with pytest.raises(ConfigError) as excinfo:
        TagRouterConsumer(
            ConsumerConfig(name="router", type="tag_router", options={"min_tags": 2})
        )
    message = str(excinfo.value)
    assert "min_tags" in message, "the unknown key must be NAMED (03 §1)"
    assert "[consumers.router]" in message


def test_uses_llm_is_false_so_a_no_ai_note_is_not_denied_by_the_runner() -> None:
    """The class flag drives the runner's CENTRAL no-ai denial. It stays
    False because routing builds no prompt — which means this consumer's own
    ``should_process`` is the ONLY thing protecting a no-ai capture. If
    someone flips this to True the protection moves house silently, so both
    halves are pinned (see the no-ai tests below)."""
    assert TagRouterConsumer.uses_llm is False
    assert consumer().wants_llm() is False


# ---------------------------------------------------------------------------
# wants_llm — CLIENT INJECTION, keyed on integrate routes (12 §1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "auto", "expected"),
    [
        ("integrate", True, True),
        ("integrate", False, False),  # non-auto never runs unattended (11 §1)
        ("append", True, False),  # mechanical: no prompt is ever built
        ("move", True, False),
    ],
)
def test_wants_llm_is_true_only_for_an_auto_integrate_route(
    fixture_vault: Path, mode: str, auto: bool, expected: bool
) -> None:
    """Doc 12 §1: ``integrate`` is the one route mode that calls an LLM.

    LITERAL expectations, and all four cells asserted rather than only the
    True one — a predicate that returns ``True`` unconditionally would inject
    a client for a purely mechanical run, and a predicate that returns
    ``False`` unconditionally is the ``taskwarrior.llm_enabled`` failure
    (``ctx.llm`` permanently ``None``, the 06 §3.1 path unreachable in
    production) reproduced for doc 12 §1.
    """
    config = make_config(fixture_vault, route(["rt-x"], TRAINING_LOG, mode=mode, auto=auto))
    assert TagRouterConsumer(
        ConsumerConfig(name="router", type="tag_router")
    ).wants_llm(config) is expected


def test_wants_llm_reads_the_bound_run_when_called_with_no_argument(
    fixture_vault: Path,
) -> None:
    """The zero-argument call the runner makes today still answers honestly
    ONCE the consumer is bound — the two-step half of the structural fix.

    Both cells, so a constant-``False`` implementation fails.
    """
    integrate = make_config(
        fixture_vault, route(["rt-x"], TRAINING_LOG, mode="integrate", auto=True)
    )
    mechanical = make_config(fixture_vault, route(["rt-x"], TRAINING_LOG, auto=True))
    assert consumer(integrate).wants_llm() is True
    assert consumer(mechanical).wants_llm() is False


def test_wants_llm_is_a_widening_so_a_zero_arg_call_still_works() -> None:
    """``wants_llm`` grew a ``config`` parameter at the Phase-5 landing (the
    runner now passes the global Config). It must stay a pure WIDENING: every
    parameter beyond ``self`` DEFAULTS, on the base class and on every
    override, or a zero-argument caller raises ``TypeError`` and silently
    falls back to the class flag — which is how ``taskwarrior.llm_enabled``
    became inert in production.

    Asserted against the LIVE classes, and then exercised for real: a
    signature check alone would pass against a default of the wrong kind.
    """
    import inspect

    from organize_core.consumers.base import Consumer

    for owner in (Consumer, TagRouterConsumer):
        signature = inspect.signature(owner.wants_llm)
        extra = [p for name, p in signature.parameters.items() if name != "self"]
        assert extra, (
            f"{owner.__name__}.wants_llm lost its config parameter — the runner "
            "passes one, and without it tag_router cannot see config.routes"
        )
        assert all(p.default is not inspect.Parameter.empty for p in extra), (
            f"{owner.__name__}.wants_llm has a non-defaulted parameter, so a "
            "zero-argument call raises TypeError and the run falls back to the "
            "class flag"
        )

    # The behaviour, not just the shape: both call forms answer, and both
    # answer the SAME thing for the same consumer.
    unbound = TagRouterConsumer(ConsumerConfig(name="router", type="tag_router"))
    assert unbound.wants_llm() is False
    assert unbound.wants_llm(None) is False


def test_wants_llm_sees_an_unattended_integrate_route_through_the_config(
    fixture_vault: Path,
) -> None:
    """THE EXPIRED SEAM PROBE, REPLACED BY THE LANDED PIN.

    ``test_wants_llm_unbound_fallback_expires_with_the_config_gate`` was a
    self-removing probe (pattern approved at fb62bab): while
    ``validate_config`` refused ``auto = true`` + ``mode = "integrate"``
    outright, an unbound ``wants_llm()`` answering ``False`` was PROVABLY
    safe, because no loadable config could make the honest answer ``True``.
    Phase 5 narrowed that refusal (an unattended integrate route is legal
    WITH ``review = "auto"``), so the probe expired exactly as designed and
    is replaced here by the pin for the seam it was guarding.

    The load-bearing claim: the config that is now loadable is also the one
    ``wants_llm`` must answer ``True`` for. If the runner ever stops passing
    the Config — or this predicate stops reading ``config.routes`` — the
    route runs against ``ctx.llm = None``, which is the inert-in-production
    failure ``taskwarrior.llm_enabled`` had.
    """
    from organize_core.config import validate_config

    raw = {
        "vault": {"root": str(fixture_vault)},
        "routes": [
            {
                "tags": ["rt-x"],
                "destination": TRAINING_LOG,
                "mode": "integrate",
                "auto": True,
                "review": "auto",
            }
        ],
    }
    config = validate_config(raw, source="<probe>")

    consumer_instance = TagRouterConsumer(ConsumerConfig(name="router", type="tag_router"))
    assert consumer_instance.wants_llm(config) is True

    # FIRING CONTROLS, so the True above cannot pass for the wrong reason.
    # (a) The same route interactive-only: no unattended integrate, no client.
    interactive = validate_config(
        {**raw, "routes": [{**raw["routes"][0], "auto": False}]}, source="<probe>"
    )
    assert consumer_instance.wants_llm(interactive) is False
    # (b) The same unattended route in a MECHANICAL mode: no prompt is built.
    mechanical = validate_config(
        {
            **raw,
            "routes": [
                {
                    "tags": ["rt-x"],
                    "destination": TRAINING_LOG,
                    "mode": "append",
                    "auto": True,
                    "template": "## {date}\n{body}",
                }
            ],
        },
        source="<probe>",
    )
    assert consumer_instance.wants_llm(mechanical) is False
    # (c) And without the Config the predicate cannot know — which is the
    #     whole reason the runner passes it.
    assert consumer_instance.wants_llm(None) is False


def test_the_runner_passes_the_global_config_to_wants_llm() -> None:
    """The CONNECTION, not just today's consequence (anti-vacuity standard 3).

    ``wants_llm(config)`` reading ``config.routes`` is useless if the runner
    calls it with nothing, and a consequence check would be blind: an
    unattended integrate route left un-integrated looks exactly like a route
    that did not match. So pin the call itself.
    """
    import inspect

    from organize_core.consumers import runner as runner_mod

    source = inspect.getsource(runner_mod.run_consumers)
    assert "consumer.wants_llm(config)" in source, (
        "runner.run_consumers must pass the global Config to wants_llm(); "
        "without it tag_router cannot see config.routes and an unattended "
        "integrate route runs against ctx.llm = None"
    )


# ---------------------------------------------------------------------------
# should_process — the consent gate and the filter law (11 §1, 06 §1)
# ---------------------------------------------------------------------------


def test_an_auto_route_match_is_processed(fixture_vault: Path) -> None:
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    path = write_capture(fixture_vault, "auto-hit", tags=["rt-workout"])

    assert consumer(config).should_process(payload_for(config, path)) is True


def test_an_unbound_predicate_is_LOUD_not_silently_false(fixture_vault: Path) -> None:
    """The interim "inert when unbound" branch is gone, and must not return.

    ARCHITECTURE Phase-4 landing, on the accepted omission: "Its dead
    unbound-inert branch + stale comment clean up in the same change" — and
    the bind ruling (4ffef89) names why it may not simply answer ``False``:
    "never 'continue unbound', because an unbound tag_router silently filters
    everything: the silent-outage class".

    Anti-vacuity: the capture used here MATCHES an ``auto = true`` route, so
    a bound consumer answers ``True`` (the firing control) — the raise cannot
    be mistaken for an ordinary filter miss.
    """
    from organize_core.errors import ConsumerError

    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    path = write_capture(fixture_vault, "unbound", tags=["rt-workout"])
    payload = payload_for(config, path)

    with pytest.raises(ConsumerError) as excinfo:
        TagRouterConsumer(ConsumerConfig(name="router", type="tag_router")).should_process(
            payload
        )
    assert "bind" in str(excinfo.value)

    assert consumer(config).should_process(payload) is True, (
        "the control did not fire — the raise above proves nothing"
    )


def test_a_non_auto_route_match_is_FILTERED_not_processed(fixture_vault: Path) -> None:
    """Spec 11 §1: "non-auto routes only surface in the UI". The consumer
    records NOTHING about them — no emission, no checkpoint — because a
    terminal row would kill the auto-flip retroactivity pinned below."""
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=False))
    path = write_capture(fixture_vault, "ui-only", tags=["rt-workout"])

    assert consumer(config).should_process(payload_for(config, path)) is False


def test_no_matching_route_is_filtered(fixture_vault: Path) -> None:
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    path = write_capture(fixture_vault, "unmatched", tags=["rt-nothing"])

    assert consumer(config).should_process(payload_for(config, path)) is False


def test_a_non_markdown_payload_is_filtered(fixture_vault: Path) -> None:
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    payload = NotePayload(
        path=fixture_vault / CAPTURE_DIR / "voice-memo.wav",
        frontmatter={"tags": ["rt-workout"]},
        content="",
        raw_text="",
        note_hash="deadbeef",
    )

    assert consumer(make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))).should_process(payload) is False
    assert config.routes  # the route really would have matched on tags


def test_an_all_of_route_reaches_the_consumer(fixture_vault: Path) -> None:
    """``"a+b"`` requires BOTH tags (11 §1). Asserted through the consumer so
    the predicate is proven to use the shared resolver rather than a local
    re-implementation of tag matching."""
    config = make_config(fixture_vault, route(["rt-deep+rt-work"], TRAINING_LOG, auto=True))
    both = write_capture(fixture_vault, "both-tags", tags=["rt-deep", "rt-work"])
    one = write_capture(fixture_vault, "one-tag", tags=["rt-deep"])

    router = consumer(config)
    assert router.should_process(payload_for(config, both)) is True
    assert router.should_process(payload_for(config, one)) is False


def test_bind_is_idempotent_and_drops_stale_run_services(fixture_vault: Path) -> None:
    """``bind`` is called once per consumer per run; a consumer instance that
    outlived a run must not answer with the previous run's config."""
    first = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    second = make_config(fixture_vault, route(["cooking"], TRAINING_LOG, auto=True))
    path = write_capture(fixture_vault, "rebind", tags=["rt-workout"])

    router = consumer(first)
    assert router.should_process(payload_for(first, path)) is True
    router.bind(RunContext(config=second))
    assert router.should_process(payload_for(second, path)) is False


# ---------------------------------------------------------------------------
# no-ai — law 2 (spec 02 vault law, ARCHITECTURE Phase-4 ruling)
# ---------------------------------------------------------------------------


def test_a_no_ai_capture_is_never_routed_even_in_a_mechanical_mode(
    fixture_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A doc-05 move WRITES the note (the ``<type>/<folder>`` tag,
    ``processing_status``), and spec 02 forbids automated tooling writing a
    no-ai note. The interactive exception is keyed to Matt's keystrokes; an
    unattended consumer has none. 06 §6 also wants the skip to be LOUD.

    NON-VACUITY (directive 11d4b1e — the standard for every refusal pin):
    the protected note's ``journal`` tag DOES match the ``auto = true`` route
    in this config, so the vault-law guard is the ONLY thing that can refuse
    it — not a route miss, not an eligibility check. The FIRING CONTROL below
    is the same note minus ``no-ai``, which IS processed. Verified by
    mutation: deleting the guard from ``should_process`` fails this test (and
    two others).
    """
    config = make_config(fixture_vault, route(["journal"], PERFORMING, mode="move", auto=True))
    payload = payload_for(config, fixture_vault / QUIRK_FILES["no_ai"])
    assert payload.no_ai is True, "fixture drift: this note must carry no-ai: true"

    with caplog.at_level("WARNING"):
        assert consumer(config).should_process(payload) is False
    assert any("no-ai" in r.getMessage() for r in caplog.records)

    # FIRING CONTROL: byte-identical apart from the `no-ai: true` line.
    twin_text = (
        (fixture_vault / QUIRK_FILES["no_ai"])
        .read_text(encoding="utf-8")
        .replace("no-ai: true\n", "")
    )
    twin = fixture_vault / CAPTURE_DIR / "public-thought.md"
    twin.write_text(twin_text, encoding="utf-8")
    twin_payload = payload_for(config, twin)
    assert twin_payload.no_ai is False
    assert consumer(config).should_process(twin_payload) is True, (
        "the control must fire, or the refusal above proves nothing"
    )


def test_no_ai_is_decided_before_the_route_lookup(fixture_vault: Path) -> None:
    """The refusal must not depend on which routes are configured — otherwise
    "is this note protected?" has a different answer per config."""
    payload = payload_for(
        make_config(fixture_vault), fixture_vault / QUIRK_FILES["no_ai"]
    )
    no_routes = make_config(fixture_vault)
    every_route = make_config(
        fixture_vault,
        route(["journal"], TRAINING_LOG, auto=True),
        route(["journal"], PERFORMING, mode="move", auto=True),
    )

    assert consumer(no_routes).should_process(payload) is False
    assert consumer(every_route).should_process(payload) is False


def test_handle_refuses_a_no_ai_capture_even_if_it_slips_past_the_predicate(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """Defence in depth: a future refactor that calls ``handle`` directly must
    not be able to route a no-ai note. SKIP (terminal) is right here — the
    vault law is not a transient failure to retry."""
    config = make_config(fixture_vault, route(["journal"], TRAINING_LOG, auto=True))
    write_target(fixture_vault, TRAINING_LOG)
    payload = payload_for(config, fixture_vault / QUIRK_FILES["no_ai"])
    before = payload.raw_text

    result = consumer().handle(payload, run_context(config, paths))

    assert result.status is Status.SKIP
    assert "no-ai" in result.message
    assert (fixture_vault / QUIRK_FILES["no_ai"]).read_text(encoding="utf-8") == before
    assert not archived_copies(fixture_vault, "private-thought")


# ---------------------------------------------------------------------------
# auto_tags (spec 11 §2) — THE rule this seat had to get exactly right
# ---------------------------------------------------------------------------


def test_a_machine_added_tag_routes_because_it_is_in_tags(fixture_vault: Path) -> None:
    """Spec 11 §2 writes each machine tag into ``tags`` AND mirrors it into
    ``auto_tags``. So an auto-tag routes BY CONSTRUCTION — there is no knob,
    and inventing one would be a config surface the spec does not define.
    The consent gate for the whole tagger→route chain is ``auto = false``."""
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    path = write_capture(
        fixture_vault, "machine-tagged", tags=["rt-workout"], auto_tags=["rt-workout"]
    )

    assert consumer(config).should_process(payload_for(config, path)) is True


def test_a_tag_only_in_auto_tags_does_NOT_route(fixture_vault: Path) -> None:
    """Spec 11 §2 acceptance 6: Matt removed the tag from ``tags``; the
    tagger's record that it once suggested it lingers in ``auto_tags``.
    ``auto_tags`` is PROVENANCE, never evidence — routing reads ``tags`` and
    only ``tags``, so his deletion sticks and the machine cannot re-assert a
    tag he rejected by routing on it.

    The firing control below is the same note WITH the tag restored to
    ``tags``, so this cannot pass because the route was misconfigured.
    """
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    removed = write_capture(
        fixture_vault, "tag-removed", tags=["rt-misc"], auto_tags=["rt-workout"]
    )
    control = write_capture(
        fixture_vault, "tag-kept", tags=["rt-misc", "rt-workout"], auto_tags=["rt-workout"]
    )

    router = consumer(config)
    assert router.should_process(payload_for(config, removed)) is False
    assert router.should_process(payload_for(config, control)) is True


def test_auto_tags_present_reaches_the_action_record(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """Doc 12 §2's ``auto_tags_present`` is 11 §2's training signal for tagger
    quality: it is how "the route fired on a tag Matt never wrote" stays
    distinguishable, after the fact, from "it fired on his own"."""
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    write_target(fixture_vault, TRAINING_LOG)
    path = write_capture(
        fixture_vault, "provenance", tags=["rt-workout"], auto_tags=["rt-workout"]
    )

    result = consumer().handle(payload_for(config, path), run_context(config, paths))
    assert result.status is Status.SUCCESS, result.message

    records = read_action_records(paths)
    assert records, "a vault mutation must emit an ActionRecord (12 §2)"
    assert any(
        "rt-workout" in (record.get("context") or {}).get("auto_tags_present", [])
        for record in records
    ), records


def read_action_records(paths: CorePaths) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for month in sorted(Path(paths.actions_dir).glob("*.jsonl")):
        for line in month.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# handle — the golden vault effects, through the REAL routes.apply_all
# ---------------------------------------------------------------------------


def test_an_auto_append_route_appends_and_archives_exactly_once(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """Spec 11 §1 acceptance 1, driven from the consumer: body appended to the
    target, capture archived, SUCCESS emitted."""
    config = make_config(
        fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True, description="Training log.")
    )
    target = write_target(fixture_vault, TRAINING_LOG)
    before = target.read_text(encoding="utf-8")
    path = write_capture(
        fixture_vault, "workout-1", tags=["rt-workout"], body="Ran 8k, felt strong."
    )

    result = consumer().handle(payload_for(config, path), run_context(config, paths))

    assert result.status is Status.SUCCESS, result.message
    after = target.read_text(encoding="utf-8")
    assert "Ran 8k, felt strong." in after
    assert after != before
    assert not path.exists(), "the capture must leave the scan set"
    assert len(archived_copies(fixture_vault, "workout-1")) == 1
    assert [entry["route"] for entry in result.metadata["applied"]] == ["rt-workout"]


def test_an_auto_move_route_files_the_capture(fixture_vault: Path, paths: CorePaths) -> None:
    config = make_config(fixture_vault, route(["rt-impro"], PERFORMING, mode="move", auto=True))
    path = write_capture(fixture_vault, "impro-1", tags=["rt-impro"])

    result = consumer().handle(payload_for(config, path), run_context(config, paths))

    assert result.status is Status.SUCCESS, result.message
    filed = list((fixture_vault / "resources" / "performing").glob("impro-1*.md"))
    assert len(filed) == 1, filed
    assert not path.exists()


def test_a_multi_route_capture_reaches_every_destination_and_archives_once(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """Spec 11 §1 acceptance 2, from the consumer's side: content lands in
    BOTH destinations and the original is archived exactly once. The consumer
    hands the whole match list to ``apply_all`` in ONE call precisely so that
    the archive-once rule stays owned by routes (ARCHITECTURE ruling #11) —
    a per-match loop here would archive twice."""
    config = make_config(
        fixture_vault,
        route(["rt-workout"], TRAINING_LOG, auto=True),
        route(["rt-workout"], IDEAS, auto=True),
    )
    log = write_target(fixture_vault, TRAINING_LOG)
    ideas = fixture_vault / IDEAS
    ideas_before = ideas.read_text(encoding="utf-8")
    path = write_capture(fixture_vault, "multi-1", tags=["rt-workout"], body="Two homes.")

    result = consumer().handle(payload_for(config, path), run_context(config, paths))

    assert result.status is Status.SUCCESS, result.message
    assert "Two homes." in log.read_text(encoding="utf-8")
    assert "Two homes." in ideas.read_text(encoding="utf-8")
    assert ideas.read_text(encoding="utf-8") != ideas_before
    assert len(archived_copies(fixture_vault, "multi-1")) == 1, "archived more than once"


def test_handle_applies_ONLY_the_auto_matches_of_a_mixed_capture(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """The consent gate at the ``handle`` level, where it is load-bearing
    rather than redundant.

    A capture that matches one ``auto`` route and one non-auto route DOES
    reach ``handle`` (the predicate said yes, correctly, because of the auto
    one). The non-auto destination must still not be written: ``auto = false``
    is Matt's opt-OUT of hands-free handling, and it has to survive being
    carried along by a sibling route that happens to share a tag.
    """
    config = make_config(
        fixture_vault,
        route(["rt-workout"], TRAINING_LOG, auto=True),
        route(["rt-workout"], IDEAS, auto=False),
    )
    log = write_target(fixture_vault, TRAINING_LOG)
    ideas = fixture_vault / IDEAS
    ideas_before = ideas.read_text(encoding="utf-8")
    path = write_capture(fixture_vault, "mixed-1", tags=["rt-workout"], body="Only the log.")

    result = consumer().handle(payload_for(config, path), run_context(config, paths))

    assert result.status is Status.SUCCESS, result.message
    assert "Only the log." in log.read_text(encoding="utf-8")
    assert ideas.read_text(encoding="utf-8") == ideas_before, (
        "the auto = false destination was written — the opt-out does not survive "
        "being matched alongside an auto route"
    )
    assert [entry["route"] for entry in result.metadata["applied"]] == ["rt-workout"]
    assert len(result.metadata["results"]) == 2, (
        "one destination + the single archive (apply_all's documented shape)"
    )


def test_a_route_whose_destination_does_not_exist_is_an_error_emission(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """A world-state failure (the error-line rule): the route was validly
    addressed and really attempted, so it comes back ``ok=False`` rather than
    raising. The consumer turns that into ERROR — which the runner does NOT
    checkpoint, so the capture is retried once Matt creates the file."""
    config = make_config(
        fixture_vault, route(["rt-workout"], "areas/health/missing-log.md", auto=True)
    )
    path = write_capture(fixture_vault, "no-dest", tags=["rt-workout"])

    result = consumer().handle(payload_for(config, path), run_context(config, paths))

    assert result.status is Status.ERROR
    assert "missing-log.md" in result.message
    assert path.exists(), "spec 11 §1: a failure leaves the capture unarchived"
    assert not archived_copies(fixture_vault, "no-dest")


def test_a_partial_multi_route_failure_leaves_the_capture_unarchived(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """Spec 11 §1 acceptance 2, second half: the second destination fails, so
    the capture is NOT archived and both attempts are reported."""
    config = make_config(
        fixture_vault,
        route(["rt-workout"], TRAINING_LOG, auto=True),
        route(["rt-workout"], "areas/health/nope.md", auto=True),
    )
    write_target(fixture_vault, TRAINING_LOG)
    path = write_capture(fixture_vault, "partial-1", tags=["rt-workout"])

    result = consumer().handle(payload_for(config, path), run_context(config, paths))

    assert result.status is Status.ERROR
    assert path.exists()
    assert not archived_copies(fixture_vault, "partial-1")
    assert len(result.metadata["results"]) == 2, "every attempt is reported"


def test_an_integrate_route_is_refused_and_writes_nothing(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """Doc 12 §1's handoff boundary: integrate EDITING is Phase 5. The refusal
    happens BEFORE anything is written — a batch that cannot be carried out as
    asked must not be half-carried-out — and the message must name the route
    and the phase, not a stale "apply_all is not landed"."""
    config = make_config(
        fixture_vault,
        route(["rt-workout"], TRAINING_LOG, mode="integrate", auto=True),
        route(["rt-workout"], IDEAS, auto=True),
    )
    log = write_target(fixture_vault, TRAINING_LOG)
    log_before = log.read_text(encoding="utf-8")
    ideas_before = (fixture_vault / IDEAS).read_text(encoding="utf-8")
    path = write_capture(fixture_vault, "integrate-1", tags=["rt-workout"])

    result = consumer().handle(payload_for(config, path), run_context(config, paths))

    assert result.status is Status.ERROR
    assert "integrate" in result.message
    assert log.read_text(encoding="utf-8") == log_before
    assert (fixture_vault / IDEAS).read_text(encoding="utf-8") == ideas_before, (
        "the sibling append must not have run — the refusal precedes every write"
    )
    assert path.exists()


def test_a_missing_op_context_is_an_error_and_never_an_unrecorded_write(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """No recorded write path ⇒ ``Status.ERROR``, and the vault untouched.

    ARCHITECTURE, "Phase-4 rulings, auto_tagger batch" (f24ee2e), verbatim:
    "a consumer that would write the vault with op_context=None emits
    Status.ERROR rather than performing an unrecorded write (refusing to
    become a second unrecorded write path is the doc-12 discipline; errors
    retry, so the run self-heals once the seam lands)".

    Anti-vacuity: the ERROR must come from the MISSING CONTEXT, not from a
    route miss or a missing target — so the target exists, the route is
    ``auto = true``, and the FIRING CONTROL at the end runs the identical
    capture through a context that DOES carry one and gets ``SUCCESS``.
    """
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    target = write_target(fixture_vault, TRAINING_LOG)
    before = target.read_text(encoding="utf-8")
    path = write_capture(fixture_vault, "no-op-ctx", tags=["rt-workout"], body="Held back.")

    result = consumer().handle(
        payload_for(config, path), run_context(config, paths, op_context=None)
    )

    assert result.status is Status.ERROR
    assert "OperationContext" in result.message
    assert path.exists(), "the capture is left in place so the retry can file it"
    assert target.read_text(encoding="utf-8") == before
    assert not archived_copies(fixture_vault, "no-op-ctx")
    assert not Path(paths.operations_log).exists() or not Path(
        paths.operations_log
    ).read_text(encoding="utf-8").strip()

    # firing control: the ONLY difference is the op_context
    control = consumer().handle(payload_for(config, path), run_context(config, paths))
    assert control.status is Status.SUCCESS, "the control did not fire — pin is vacuous"
    assert "Held back." in target.read_text(encoding="utf-8")


def test_missing_core_paths_no_longer_decides_anything_on_its_own(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """``RunContext.paths`` is no longer this consumer's dependency.

    It used to open its own ``VaultIndex``/``OperationLog``/``ActionRecorder``
    from ``ctx.paths``; those all come from ``ctx.op_context`` now, so a
    context carrying the recorded write path but no ``CorePaths`` is
    perfectly workable — and pinning that is how a reintroduced
    ``ctx.paths``-derived service gets caught."""
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    target = write_target(fixture_vault, TRAINING_LOG)
    path = write_capture(fixture_vault, "no-paths", tags=["rt-workout"], body="Still filed.")

    ctx = RunContext(
        config=config, paths=None, op_context=consumer_op_context(config, paths)
    )
    result = consumer().handle(payload_for(config, path), ctx)

    assert result.status is Status.SUCCESS, result.message
    assert "Still filed." in target.read_text(encoding="utf-8")


def test_handle_never_raises_when_apply_all_explodes(
    fixture_vault: Path, paths: CorePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06 §1: per-note failures are ERROR results, never exceptions — an
    exception escaping ``handle`` is what B12 was."""
    from organize_core.consumers import tag_router as module

    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    write_target(fixture_vault, TRAINING_LOG)
    path = write_capture(fixture_vault, "boom", tags=["rt-workout"])

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(module.routes_mod, "apply_all", explode)
    result = consumer().handle(payload_for(config, path), run_context(config, paths))

    assert result.status is Status.ERROR
    assert "disk on fire" in result.message


# ---------------------------------------------------------------------------
# the operation context the consumer builds (structural decision 1 + 12 §2)
# ---------------------------------------------------------------------------


def test_every_mutation_is_recorded_in_the_oplog_and_the_action_corpus(
    fixture_vault: Path, paths: CorePaths
) -> None:
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    write_target(fixture_vault, TRAINING_LOG)
    path = write_capture(fixture_vault, "recorded", tags=["rt-workout"])

    assert consumer().handle(
        payload_for(config, path), run_context(config, paths)
    ).status is Status.SUCCESS

    oplog = Path(paths.operations_log)
    assert oplog.is_file() and oplog.read_text(encoding="utf-8").strip()
    records = read_action_records(paths)
    assert records, "12 §2: every state-changing operation appends an ActionRecord"
    actors = {record["actor"] for record in records}
    assert actors, records
    assert all(
        actor == ACTOR or actor.startswith("route:") for actor in actors
    ), f"unexpected actor(s) {actors}; 12 §2's enum has route:<name> and consumer:<name>"


def test_the_action_record_carries_no_phantom_suggestions(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """``organize actions stats`` measures accept-rate against
    ``suggestions_shown``/``chosen_rank``. An unattended firing showed nobody
    anything, so filling them would report a rank-1 accept for every
    automated action and corrupt the one metric the corpus exists to give
    honestly."""
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    write_target(fixture_vault, TRAINING_LOG)
    path = write_capture(fixture_vault, "no-phantom", tags=["rt-workout"])

    consumer().handle(payload_for(config, path), run_context(config, paths))

    records = read_action_records(paths)
    assert records, "nothing was recorded — this assertion would be vacuous"
    for record in records:
        # Both live UNDER `context` in the 12 §2 wire shape; reading them off
        # the top level would make this test pass for any value at all.
        context = record["context"]
        assert not context["suggestions_shown"], record
        assert context["chosen_rank"] is None, record


def test_the_operation_context_is_actor_tagged_with_a_literal_and_is_the_roots_own(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """Structural pin on the context the consumer hands ``routes.apply_all``.

    THE TRIPWIRE SWAP. ARCHITECTURE, "Test anti-vacuity standards", verbatim:

        DESIGNED TRIPWIRE (Phase-5 implementer, do not misread as
        regression): tag_router's ``op_ctx.on_record is None`` pin is correct
        TODAY because learn.record_action does not filter by actor. The
        standing Phase-5 ruling moves enforcement INTO learn.record_action
        (folds only Matt-decided actions); when that lands, on_record becomes
        safely wireable globally and this connection-pin WILL fail BY DESIGN
        — swap it for the callee-side pin (wired on_record + route-actor
        record ⇒ learning.json unchanged) in the same change that lands the
        filter.

    The filter landed (``learn.is_matt_decided``), so the connection
    assertion is REPLACED by its callee-side counterpart in
    ``test_a_routed_run_teaches_the_learner_NOTHING`` below — which now
    asserts the opposite connection (``on_record`` IS wired) before asserting
    the bytes, so it cannot pass by the callback being absent.

    The actor half is unchanged and still compares against a LITERAL, not
    against ``ACTOR``: a mutation sweep found that flipping the constant to
    ``"matt"`` kept the whole suite green while every unattended firing was
    attributed to Matt, corrupting ``actions stats`` and doc-12's actor enum.
    """
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    ctx = run_context(config, paths)
    op_ctx = consumer(config)._operation_context(ctx)

    assert op_ctx is not None
    assert op_ctx.actor == "consumer:tag_router", (
        "an unattended route firing is never attributed to Matt (12 §2 actor enum)"
    )
    assert ACTOR == "consumer:tag_router", "the exported constant must agree"
    assert op_ctx is ctx.op_context, (
        "the consumer must CONSUME the composition root's context, not build a "
        "second one (ARCHITECTURE Phase-4 landing: the omission accepted as a "
        "Phase-5 rider) — a private context means a private index and a private "
        "recorder that `cmd_run_consumers` never flushes"
    )

    # The rest of the doc-05 invariants the context is REQUIRED to carry.
    assert op_ctx.oplog is not None and op_ctx.recorder is not None
    assert op_ctx.backup_dir == fixture_vault / config.file_ops.backup_dir
    assert op_ctx.dry_run is False
    assert op_ctx.describe is not None, "12 §2 targets[].description needs the 11 §3 lookup"
    # 12 §2 stores the counterfactual the DECIDER was shown; an unattended
    # firing showed nobody anything, so filling these would report a rank-1
    # "accept" for every automated move.
    assert op_ctx.suggestions_shown == ()
    assert op_ctx.chosen_rank is None


def test_the_capture_record_is_read_through_the_composition_roots_own_index(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """The doc-05 ``NoteRecord`` must be indexed on the ROOT's index.

    ARCHITECTURE, "Phase-4 rulings, tag_router close", verbatim: "**Index
    staleness after applies**: ONE end-of-run flush in cmd_run_consumers via
    op_context's index (composition root owns the index lifecycle once
    op_context lands). Per-note flush rejected (full-snapshot rewrite per
    note)."

    One flush can only be accurate if there is one index. This consumer used
    to open its own ``VaultIndex`` over the same snapshot path, so every
    routed change landed in an object nobody flushed. Pinning the OBJECT (a
    second index that reads the same file answers identically, so a
    consequence check alone is blind — the mutation audit proved it) is what
    catches a reintroduced second reader.
    """
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    path = write_capture(fixture_vault, "root-index", tags=["rt-workout"])
    op_ctx = consumer_op_context(config, paths)
    assert op_ctx.index.get(path) is None, "cold index — the assertion below is real"

    record = TagRouterConsumer._capture_record(op_ctx, payload_for(config, path))

    assert record is not None
    indexed = op_ctx.index.get(path)
    assert indexed is not None, (
        "the capture was indexed somewhere the composition root cannot flush — "
        "a second VaultIndex is exactly the staleness this ruling removed"
    )
    assert indexed is record, "and it is the SAME record, from the same store"


def test_a_routed_apply_leaves_the_roots_index_accurate_for_its_one_flush(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """End-to-end consequence of the same rule: after a routed run, the
    root's index no longer holds the capture, and its ONE flush persists
    that. With a private index inside the consumer the snapshot on disk
    still described the pre-run vault."""
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    write_target(fixture_vault, TRAINING_LOG)
    path = write_capture(fixture_vault, "flushed", tags=["rt-workout"], body="Filed.")
    ctx = run_context(config, paths)
    assert ctx.op_context is not None
    ctx.op_context.index.full_reindex()
    assert ctx.op_context.index.get(path) is not None, "control: the capture starts indexed"

    assert consumer().handle(payload_for(config, path), ctx).status is Status.SUCCESS

    assert ctx.op_context.index.get(path) is None, (
        "apply_all removed the capture entry from the root's index (05 §2 step 8)"
    )
    ctx.op_context.index.flush()
    reopened = VaultIndex(config, paths.index_path)
    reopened.load()
    assert reopened.get(path) is None, "the flushed snapshot still described the old vault"


def test_a_routed_run_teaches_the_learner_NOTHING(
    fixture_vault: Path, paths: CorePaths
) -> None:
    """TRAP TEST, in its CALLEE-SIDE form (the designed tripwire swap).

    ARCHITECTURE ruling 4ffef89, verbatim: "**LEARNING FOLDS ONLY
    MATT-DECIDED ACTIONS** […] a route firing is config, not a decision —
    folding it would make routes self-reinforcing and corrupt the accept-rate
    corpus. The routed-run learning-byte-identical trap test (with firing
    control) is permanent."

    Until Phase 5 that was enforced by tag_router leaving ``on_record``
    UNSET, and the sibling test pinned exactly that connection. The
    enforcement is now inside ``learn.record_action``, so the shape of the
    pin inverts: ``on_record`` must be WIRED (asserted first, so the byte
    assertion cannot pass because the callback was absent) and the bytes must
    still not move.

    TWO FIRING CONTROLS, because there are two ways this could go vacuous:

    1. *The callback is reachable and does write* — the SAME wired callback,
       fed the same route action re-actored to ``"matt"``, changes the file.
       This is the mutate-the-guard-away check: the actor filter is the only
       thing standing between this run and a learning write.
    2. *The learner is writable at all* — a plain ``record_move`` +
       ``save_learning`` against the same path changes the bytes.
    """
    import dataclasses as _dc

    from organize_core.learn import LearningData, record_move, save_learning

    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    write_target(fixture_vault, TRAINING_LOG)
    learning = Path(paths.learning_path)
    save_learning(learning, LearningData())
    before = learning.read_bytes()

    ctx = run_context(config, paths)
    assert ctx.op_context is not None and ctx.op_context.on_record is not None, (
        "the pin is vacuous unless the learning callback is actually wired — "
        "that is the whole point of the Phase-5 tripwire swap"
    )

    path = write_capture(fixture_vault, "no-teach", tags=["rt-workout"])
    assert consumer().handle(payload_for(config, path), ctx).status is Status.SUCCESS

    assert learning.read_bytes() == before, (
        "an unattended route firing must not become a learning precedent"
    )

    # --- firing control 1: the guard, mutated away ------------------------
    # The record the run just wrote, re-actored to Matt and fed to the SAME
    # wired callback. If this does not move the bytes, the trap above proves
    # nothing about the actor filter.
    from organize_core.actions import ActionRecord

    written = [ActionRecord.from_json(raw) for raw in read_action_records(paths)]
    routed = [rec for rec in written if rec.operation in {"append", "move", "merge"}]
    assert routed, "the run recorded no filing operation — nothing to control against"
    assert all(
        rec.actor.startswith("route:") or rec.actor == "consumer:tag_router"
        for rec in routed
    ), [rec.actor for rec in routed]

    ctx.op_context.on_record(_dc.replace(routed[0], actor="matt"))
    assert learning.read_bytes() != before, (
        "the wired callback never wrote — the byte assertion above was vacuous"
    )

    # --- firing control 2: the learner is writable at all ------------------
    save_learning(learning, LearningData())
    from organize_core.index import VaultIndex

    index = VaultIndex(config, paths.index_path)
    index.full_reindex()
    control_capture = write_capture(fixture_vault, "control", tags=["rt-workout"])
    record = index.update_file(control_capture)
    assert record is not None
    data = record_move(LearningData(), record, "areas/health", now=0.0)
    save_learning(learning, data)
    assert learning.read_bytes() != before, "the control did not fire — trap is vacuous"


def test_a_routed_run_through_the_runner_teaches_the_learner_NOTHING(
    fixture_vault: Path, paths: CorePaths, store: AutomationStore
) -> None:
    """The same trap, through the REAL chain — composition root → runner →
    consumer → ``routes.apply_all`` → recorder → ``on_record``.

    The handle-level trap above proves the consumer's own wiring; this proves
    that the wiring the RUNNER actually produces
    (``dataclasses.replace(op_context, actor="consumer:tag_router")``, which
    carries ``on_record`` straight through) still folds nothing. Firing
    control: the identical vault and callback, driven by an actor-``matt``
    record, does move the bytes.
    """
    from organize_core.learn import LearningData, save_learning

    config = router_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    write_target(fixture_vault, TRAINING_LOG)
    learning = Path(paths.learning_path)
    save_learning(learning, LearningData())
    before = learning.read_bytes()

    write_capture(fixture_vault, "runner-no-teach", tags=["rt-workout"], body="Not a decision.")
    summary = drive(config, store, paths)

    assert next(c for c in summary.consumers if c.name == "router").success == 1
    assert learning.read_bytes() == before, (
        "the runner's per-consumer context carries on_record through, so this "
        "is the wiring production uses — it must still fold nothing"
    )

    # firing control: same callback, Matt-actored record
    from organize_core.actions import ActionRecord

    written = [ActionRecord.from_json(raw) for raw in read_action_records(paths)]
    assert written, "the run recorded nothing — the assertion above was vacuous"
    root = root_op_context(config, paths)
    assert root.on_record is not None
    root.on_record(dataclasses.replace(written[0], actor="matt"))
    assert learning.read_bytes() != before, "the control did not fire — trap is vacuous"


# ---------------------------------------------------------------------------
# dry run (09 §5.6)
# ---------------------------------------------------------------------------


def test_dry_run_handle_writes_nothing(fixture_vault: Path, paths: CorePaths) -> None:
    config = make_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    target = write_target(fixture_vault, TRAINING_LOG)
    before = target.read_text(encoding="utf-8")
    path = write_capture(fixture_vault, "rehearsal", tags=["rt-workout"])
    capture_before = path.read_text(encoding="utf-8")

    result = consumer().handle(
        payload_for(config, path), run_context(config, paths, dry_run=True)
    )

    assert result.status is Status.SKIP
    assert "[DRY-RUN]" in result.message
    assert target.read_text(encoding="utf-8") == before
    assert path.read_text(encoding="utf-8") == capture_before
    assert not archived_copies(fixture_vault, "rehearsal")
    assert not Path(paths.operations_log).exists() or not Path(
        paths.operations_log
    ).read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# through the RUNNER, against the real SQLite store (06 §1, §7)
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path):  # noqa: ANN201 - same shape as the runner suites
    db = tmp_path / "state" / "automations.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with AutomationStore(db) as opened:
        opened.migrate()
        yield opened


def drive(
    config: Config, store: AutomationStore, paths: CorePaths, *, dry_run: bool = False
) -> Any:
    """``cli.cmd_run_consumers``' composition-root wiring, minus the CLI shell.

    ONE ``OperationContext``, ONE ``VaultIndex``, ONE end-of-run flush — the
    real chain, because ``tag_router`` now consumes ``RunContext.op_context``
    instead of building its own. Calling ``run_consumers`` without an
    ``op_context`` is a legitimate composition (other consumers do not need
    one), so it is not an error the runner raises; it makes THIS consumer
    return ``Status.ERROR``, which is pinned separately.
    """
    op_context = root_op_context(config, paths, dry_run=dry_run)
    summary = run_consumers(
        config, store, dry_run=dry_run, paths=paths, op_context=op_context
    )
    if not dry_run:
        op_context.index.flush()
    return summary


def router_config(vault: Path, *routes: RouteConfig) -> Config:
    return make_config(
        vault,
        *routes,
        consumers=[
            ConsumerConfig(
                name="router", type="tag_router", include_paths=[CAPTURE_DIR]
            )
        ],
    )


def test_golden_run_applies_the_auto_route_and_leaves_everything_else_alone(
    fixture_vault: Path, paths: CorePaths, store: AutomationStore
) -> None:
    """The 06 §7 golden run for this consumer: one auto-routed capture is
    filed and archived, an unmatched capture is FILTERED (no store row at
    all), and a non-auto match is filtered too."""
    config = router_config(
        fixture_vault,
        route(["rt-workout"], TRAINING_LOG, auto=True),
        route(["rt-idea"], IDEAS, auto=False),
    )
    target = write_target(fixture_vault, TRAINING_LOG)
    hit = write_capture(fixture_vault, "run-hit", tags=["rt-workout"], body="Squats.")
    proposal = write_capture(fixture_vault, "run-proposal", tags=["rt-idea"])
    miss = write_capture(fixture_vault, "run-miss", tags=["rt-nothing"])

    summary = drive(config, store, paths)

    consumer_summary = next(c for c in summary.consumers if c.name == "router")
    assert consumer_summary.success == 1, summary.consumers
    assert consumer_summary.error == 0, consumer_summary.failures
    assert "Squats." in target.read_text(encoding="utf-8")
    assert not hit.exists()
    assert len(archived_copies(fixture_vault, "run-hit")) == 1

    # the filtered ones are untouched AND unrecorded — a filter miss is never
    # persisted (06 §1), which is what keeps config changes retroactive
    assert proposal.exists() and miss.exists()
    assert store.get_emission("router", proposal.resolve()) is None
    assert store.get_emission("router", miss.resolve()) is None
    assert store.get_emission("router", hit.resolve()) is not None


def test_a_custom_append_template_still_carries_the_capture_body(
    fixture_vault: Path, paths: CorePaths, store: AutomationStore
) -> None:
    """The shipped example's template shape, driven through the REAL chain.

    A template is the WHOLE appended block. The example config used to ship
    ``"## {date} — from {capture_id}"`` with no ``{body}``, so following it
    appended a heading, DROPPED the capture's text, archived the capture and
    exited 0 — invisible at every layer. Config validation now refuses a
    body-less append template; this proves the other half, that a template
    carrying it actually lands the text in the destination file.
    """
    config = router_config(
        fixture_vault,
        route(
            ["rt-workout"],
            TRAINING_LOG,
            auto=True,
            template="## {date} — from {capture_id}\n\n{body}",
        ),
    )
    target = write_target(fixture_vault, TRAINING_LOG)
    capture = write_capture(
        fixture_vault, "tpl-body", tags=["rt-workout"], body="Squatted 140kg."
    )

    summary = drive(config, store, paths)

    assert next(c for c in summary.consumers if c.name == "router").success == 1
    text = target.read_text(encoding="utf-8")
    assert "Squatted 140kg." in text, "the capture's body must reach the destination"
    assert "— from" in text, "and the template's own heading is still rendered"
    assert not capture.exists()
    assert len(archived_copies(fixture_vault, "tpl-body")) == 1


def test_a_second_run_changes_nothing(
    fixture_vault: Path, paths: CorePaths, store: AutomationStore
) -> None:
    """Rerun idempotency. The applied capture is archived, so it leaves the
    scan set entirely — the strongest possible form of "does not re-fire",
    and it must not append to the target a second time."""
    config = router_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    target = write_target(fixture_vault, TRAINING_LOG)
    write_capture(fixture_vault, "once", tags=["rt-workout"], body="Only once.")

    drive(config, store, paths)
    after_first = target.read_text(encoding="utf-8")
    assert after_first.count("Only once.") == 1

    second = drive(config, store, paths)

    assert target.read_text(encoding="utf-8") == after_first
    assert next(c for c in second.consumers if c.name == "router").success == 0
    assert len(archived_copies(fixture_vault, "once")) == 1


def test_flipping_auto_true_fires_at_an_unchanged_note_hash(
    fixture_vault: Path, paths: CorePaths, store: AutomationStore
) -> None:
    """THE retroactivity property, and the reason a non-auto match must be a
    FILTER rather than a proposal emission (ruling 8c86c8a).

    Run 1 with ``auto = false`` must write NO terminal checkpoint. Run 2 flips
    the flag with the note's BYTES UNCHANGED — so ``needs_delivery`` still
    sees the same hash. If run 1 had checkpointed anything, run 2 would be a
    silent no-op forever and Matt's edit would appear to do nothing.
    """
    target = write_target(fixture_vault, TRAINING_LOG)
    path = write_capture(fixture_vault, "flip", tags=["rt-workout"], body="Deferred.")
    before_bytes = path.read_bytes()

    manual = router_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=False))
    drive(manual, store, paths)
    assert path.read_bytes() == before_bytes, "a non-auto route must not touch the note"
    assert store.get_emission("router", path.resolve()) is None, (
        "a non-auto match must leave NO checkpoint — otherwise the flip below "
        "can never fire (08 §B3/§B4)"
    )

    hands_free = router_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    summary = drive(hands_free, store, paths)

    assert next(c for c in summary.consumers if c.name == "router").success == 1
    assert "Deferred." in target.read_text(encoding="utf-8")


def test_a_failing_route_does_not_stop_the_run(
    fixture_vault: Path, paths: CorePaths, store: AutomationStore
) -> None:
    """A route pointing at a nonexistent destination is an ERROR emission and
    the run CONTINUES to the next capture (06 §1 isolation, 08 §B12). The
    error is not checkpointed, so the capture retries next run."""
    config = router_config(
        fixture_vault,
        route(["rt-broken"], "areas/health/absent.md", auto=True),
        route(["rt-workout"], TRAINING_LOG, auto=True),
    )
    target = write_target(fixture_vault, TRAINING_LOG)
    broken = write_capture(fixture_vault, "aaa-broken", tags=["rt-broken"])
    good = write_capture(fixture_vault, "zzz-good", tags=["rt-workout"], body="Still ran.")

    summary = drive(config, store, paths)
    consumer_summary = next(c for c in summary.consumers if c.name == "router")

    assert consumer_summary.error == 1, consumer_summary.failures
    assert consumer_summary.success == 1, "the healthy capture after it must still be filed"
    assert "Still ran." in target.read_text(encoding="utf-8")
    assert broken.exists(), "a failed route leaves the capture in place"
    assert not good.exists()
    assert store.get_emission("router", broken.resolve()) is None, (
        "an error must not be checkpointed (06 §1, 08 §B3) — it is retried next run"
    )


def test_a_dry_run_touches_neither_the_vault_nor_the_store(
    fixture_vault: Path, paths: CorePaths, store: AutomationStore
) -> None:
    """09 §5.6: a rehearsal evaluates everything and writes nothing. Both
    store tables are asserted, not just ``emissions`` — a ``mark_seen``-only
    leak passes the weaker assertion. The firing control is the real run at
    the end, which DOES change all three."""
    config = router_config(fixture_vault, route(["rt-workout"], TRAINING_LOG, auto=True))
    target = write_target(fixture_vault, TRAINING_LOG)
    before = target.read_text(encoding="utf-8")
    path = write_capture(fixture_vault, "rehearse", tags=["rt-workout"], body="Not yet.")

    summary = drive(config, store, paths, dry_run=True)

    assert next(c for c in summary.consumers if c.name == "router").would_process == 1
    assert target.read_text(encoding="utf-8") == before
    assert path.exists()
    assert store.get_emission("router", path.resolve()) is None
    assert not archived_copies(fixture_vault, "rehearse")
    assert not Path(paths.operations_log).exists() or not Path(
        paths.operations_log
    ).read_text(encoding="utf-8").strip()

    # firing control — the same setup, for real
    drive(config, store, paths)
    assert target.read_text(encoding="utf-8") != before
    assert store.get_emission("router", path.resolve()) is not None


def test_a_no_ai_capture_survives_a_full_run_untouched(
    fixture_vault: Path, paths: CorePaths, store: AutomationStore
) -> None:
    """End-to-end vault law: a route that matches the no-ai fixture note fires
    for nobody, writes nothing, and leaves no checkpoint. The control is the
    ordinary capture beside it, which the same route DOES file."""
    config = router_config(fixture_vault, route(["journal"], TRAINING_LOG, auto=True))
    write_target(fixture_vault, TRAINING_LOG)
    protected = fixture_vault / QUIRK_FILES["no_ai"]
    before = protected.read_bytes()
    control = write_capture(fixture_vault, "journal-ok", tags=["journal"], body="Fine.")

    drive(config, store, paths)

    assert protected.read_bytes() == before
    assert store.get_emission("router", protected.resolve()) is None
    assert not control.exists(), "control did not fire — the no-ai assertion is vacuous"
