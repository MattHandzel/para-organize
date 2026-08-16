"""``tag_router`` consumer: apply ``auto = true`` routes unattended
(spec 11 §1 "Where routes act" #2).

What this consumer is, in one line: the UNATTENDED half of doc 11. The
interactive half is ``routes.resolve`` + ``merge_route_suggestions``, which
the UI calls live; this module is what fires when nobody is watching.

Three laws it exists to enforce, each with a pinning test in
``tests/test_consumer_router.py``:

1. **``auto = false`` is the consent gate, and it is a FILTER.** A route
   only fires unattended when Matt has explicitly opted it in. A capture
   whose every match is non-auto does not reach :meth:`handle` at all and is
   never checkpointed — spec 11 §1: "non-auto routes only surface in the
   UI", which ``routes.resolve`` + ``merge_route_suggestions`` do live, with
   no store involvement (ruling 8c86c8a).

   That the miss is a FILTER rather than a terminal ``skip`` is the whole
   point: filter misses are re-evaluated every run (06 §1), so flipping a
   route to ``auto = true`` fires on the next run at an UNCHANGED note hash.
   Recording a proposal instead would write the terminal checkpoint that
   makes exactly that flip a no-op forever (the 08 §B3/§B4 class).
2. **``no-ai`` captures are refused outright, in every mode** (ARCHITECTURE
   "Phase-4 rulings", commit fb62bab — this CORRECTS the earlier
   "a mechanical move is allowed" reading). A doc-05 move WRITES the note's
   frontmatter (the ``<type>/<folder>`` tag and ``processing_status`` on the
   copy), and spec 02's vault law forbids automated tooling writing a no-ai
   note. The interactive exception is keyed to Matt's explicit keystrokes;
   an unattended consumer has none.
3. **No file effect is implemented here.** Everything goes through
   :func:`routes.apply_all`, which owns mode dispatch and the
   archive-exactly-once-after-all-destinations-succeed rule (ARCHITECTURE
   resolution #11). A second archiver in this module is how a capture ends
   up archived while its second destination failed.

``auto_tags`` and routing (spec 11 §2, ruling fb62bab): machine-added tags
participate in routing BY CONSTRUCTION and knob-free — 11 §2 writes them to
``tags`` *and* mirrors them into ``auto_tags``, and ``routes.resolve``
consumes ``tags``. So there is no opt-out key here and no second tag source:
routing evidence is ``tags`` and ONLY ``tags``. The consequence that makes
this safe is 11 §2's acceptance test 6 — a tag Matt DELETED from ``tags``
that is still listed in ``auto_tags`` does not route, because ``auto_tags``
is provenance metadata, never evidence. The consent gate for the whole
machine chain (tagger → route) is the per-route ``auto = false`` default,
and 12 §2's ``auto_tags_present`` records the provenance in every record.

Learning is deliberately NOT wired (``on_record`` left unset — ruling
4ffef89): a route firing is CONFIG, not a decision Matt made. Folding it
into ``learning.json`` would make routes self-reinforcing and would pollute
the doc-12 accept-rate signal with non-decisions. ``learn.record_action``
does not filter by actor, so this has to be a caller-side choice — and it is
pinned by a trap test, not left to a comment.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from organize_core import routes as routes_mod
from organize_core.actions import ActionRecorder
from organize_core.config import ConsumerConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    Status,
    register,
)
from organize_core.errors import ConfigError, OrganizeError
from organize_core.fileops import OperationContext, OperationLog
from organize_core.index import NoteRecord, VaultIndex
from organize_core.routes import RouteMatch

LOG = logging.getLogger(__name__)

#: ``OperationContext.actor`` for every operation this consumer drives — the
#: honest identity of WHO ran it, and a member of doc 12 §2's actor enum.
#:
#: Ruling 4ffef89 splits this from the RECORD's actor deliberately: the
#: aggregate ActionRecord carries ``route:<name>`` when exactly one route
#: fired (12 §2's enum member for unattended route firing) and falls back to
#: this value with a comma-joined ``context.route`` for a multi-route
#: capture. ROUTES sets that — it builds the record and it is the only layer
#: that knows how many destinations actually fired — so this module sets the
#: context actor and nothing else.
ACTOR = "consumer:tag_router"

#: This consumer takes no options. Spec 11 §1 defines the routing behaviour
#: entirely through ``[[routes]]``; there is no per-consumer knob, and the
#: every-key-honored law (03 §1) says an unrecognized key must fail loudly
#: rather than be silently ignored. Notably there is NO auto_tags opt-out
#: (ruling fb62bab) and NO proposal switch — see the module docstring.
_KNOWN_OPTIONS: frozenset[str] = frozenset()


def _match_payload(match: RouteMatch) -> dict[str, Any]:
    """One route match, JSON-shaped, for the emission metadata that the
    UI/CLI reads back through ``AutomationStore.get_emission`` (06 §1).

    Carries ``description`` because spec 11 §1 makes it load-bearing: it is
    what the UI shows when the destination is offered, so a proposal without
    it is not actionable.
    """
    return {
        "route": match.route_name,
        "destination": str(match.destination),
        "mode": str(match.route.mode),
        "auto": bool(match.route.auto),
        "is_folder": bool(match.is_folder),
        "description": (match.route.description or "").strip() or None,
    }


@register("tag_router")
class TagRouterConsumer(Consumer):
    """Applies ``auto = true`` routes; proposes everything else."""

    #: Routing itself is mechanical — move/append never build a prompt, so
    #: the runner's central no-ai DENIAL (which keys on this flag) is not
    #: what protects no-ai captures here; :meth:`should_process` is, and it
    #: refuses them in EVERY mode rather than only the LLM-shaped one.
    uses_llm = False

    implemented = True

    def __init__(self, config: ConsumerConfig) -> None:
        """PURE — option validation only, no I/O (06 §1 / 08 §B2)."""
        super().__init__(config)
        unknown = sorted(set(config.options or {}) - _KNOWN_OPTIONS)
        if unknown:
            raise ConfigError(
                f"unknown option(s) for consumer type 'tag_router' in "
                f"[consumers.{config.name}]: {', '.join(unknown)}",
                hint="tag_router takes no options — routing is configured "
                "entirely by [[routes]] entries (spec 11 §1)",
            )
        # Run-scoped services, populated by bind()/handle(). None between runs.
        self._run: RunContext | None = None
        self._index: VaultIndex | None = None
        self._op_context: OperationContext | None = None
        self._warned_unbound = False

    # --- run-scoped binding ------------------------------------------------

    def bind(self, ctx: RunContext) -> None:
        """Once-per-run hook: give :meth:`should_process` the global config.

        APPROVED seam (ARCHITECTURE ruling 4ffef89). ``should_process``
        receives only a ``NotePayload``, but routes live on ``Config.routes``
        — so without this the predicate cannot ask the one question it
        exists to answer. Deciding it in ``handle`` instead would force a
        TERMINAL ``skip`` checkpoint for every non-matching capture, and a
        route added later would then never fire on any already-seen note at
        an unchanged hash: exactly the 08 §B3/§B4 class the framework exists
        to prevent. A filter miss is not persisted, so the answer is
        re-derived every run and config changes stay retroactive (06 §1).

        Cheapness contract (ruling 4ffef89): binding grants CONFIG READS to
        the predicate and nothing more. ``should_process`` stays cheap —
        path, already-parsed frontmatter and pure config predicates; no I/O,
        no LLM. The expensive services (index, operation context) are built
        lazily on the first real apply, not here.

        Failure semantics once wired (ruling 4ffef89, for the integrator): a
        ``bind()`` that RAISES means the consumer is skipped for the run,
        counted as an ERROR in the 06 §4 summary, and the run exits 1 — never
        "continue unbound", because an unbound tag_router silently filters
        every capture, which is the silent-outage class. Other consumers are
        unaffected. This implementation cannot raise.

        Until the integrator lands the ``base.Consumer.bind`` +
        ``runner.run_consumers`` wiring, this is called by :meth:`handle` and
        by tests. An UNBOUND run is inert by design — see
        :meth:`should_process`.
        """
        self._run = ctx
        # Services are ctx-derived; drop any carried over from a previous run.
        self._index = None
        self._op_context = None

    def wants_llm(self) -> bool:
        """False in Phase 4: move/append are mechanical and build no prompt.

        The scaffold comment asked Phase 4 to return True when any configured
        auto route is ``integrate``. That is not implementable today and the
        reason is structural, not an oversight: the runner calls
        ``wants_llm()`` to DECIDE what goes on the ``RunContext``, i.e. one
        line BEFORE the context exists — so the predicate cannot see
        ``config.routes`` even once ``bind(ctx)`` is wired, because bind runs
        after. Phase 5 needs either a config-aware ``wants_llm(config)`` or a
        two-step context build; recorded here so the integrate path is not
        wired up against a permanently-``None`` ``ctx.llm`` (the same flaw
        that made taskwarrior's ``llm_enabled`` inert in production).
        """
        return False

    # --- predicate ---------------------------------------------------------

    def should_process(self, payload: NotePayload) -> bool:
        """Any tag resolves to a route with ``auto = true`` (11 §1). Cheap,
        pure, never persisted.

        A capture matching NO route, or matching only ``auto = false``
        routes, is FILTERED (ruling 8c86c8a). Filter misses are not
        persisted, so both cases are re-derived every run: adding a route,
        or flipping an existing one to ``auto = true``, fires on the next run
        at an unchanged note hash (06 §1 retroactivity). Non-auto routes
        surface in the UI, which computes them live — this consumer records
        nothing about them.
        """
        if payload.path.suffix != ".md":
            # Defence in depth, matching every sibling consumer. Ingestion
            # only yields `*.md`, but `capture/raw_capture` really does hold
            # .wav/.txt/.pdf next to the notes (spec 02), and every doc-05
            # path this consumer can reach parses frontmatter — routing one
            # of those would be a guaranteed failure, not a filter miss.
            return False
        config = None if self._run is None else self._run.config
        if config is None:
            # Inert rather than wrong. Approved interim (ruling 4ffef89)
            # until base/runner call bind(): a filter miss writes nothing, so
            # the moment the hook lands every capture is re-evaluated with no
            # stale checkpoints to undo.
            if not self._warned_unbound:
                self._warned_unbound = True
                LOG.warning(
                    "tag_router is unbound (no RunContext) — routing is INERT this "
                    "run. Wire Consumer.bind(ctx) in runner.run_consumers "
                    "(ARCHITECTURE ruling 4ffef89); no note is checkpointed "
                    "meanwhile, so routing resumes with full history once wired."
                )
            return False

        if payload.no_ai:
            # Law 2. Before the route lookup on purpose: the answer must not
            # depend on which routes happen to be configured, and 06 §6 wants
            # a WARN on every skipped file — a note the vault forbids us to
            # touch is the one skip that must never be silent.
            LOG.warning(
                "no-ai: %s never routed (spec 02 vault law; a doc-05 move writes "
                "the note's frontmatter, and an unattended consumer has none of "
                "Matt's keystrokes to authorize it)",
                payload.path,
            )
            return False

        return any(
            bool(match.route.auto) for match in routes_mod.resolve(payload.tags(), config)
        )

    # --- work --------------------------------------------------------------

    def handle(self, payload: NotePayload, ctx: RunContext) -> ConsumerResult:
        """Apply every ``auto`` match through :func:`routes.apply_all`, in ONE
        call so the original is archived exactly once (resolution #11).

        Non-auto matches are dropped WITHOUT a trace — deliberately not
        recorded as proposals (ruling 8c86c8a); law 1 in the module docstring
        explains why recording them would be actively harmful.

        Never raises for a per-note failure (06 §1): a failure is a
        ``Status.ERROR`` result, which the runner does NOT checkpoint, so the
        capture is retried next run.
        """
        self.bind(ctx)

        if payload.no_ai:
            # Defence in depth: should_process already filtered it. If a
            # future refactor ever calls handle directly, the vault law must
            # still hold rather than depend on call order.
            LOG.warning("no-ai: refusing to route %s (spec 02 vault law)", payload.path)
            return ConsumerResult(
                status=Status.SKIP,
                message="no-ai: true — never routed by an unattended consumer",
            )

        # ONLY auto routes are ever applied. Non-auto matches are dropped
        # here without a trace by design (ruling 8c86c8a) — they are the
        # UI's business, and recording them would checkpoint the capture and
        # kill the auto-flip retroactivity that the filter buys.
        auto = [
            match for match in routes_mod.resolve(payload.tags(), ctx.config) if match.route.auto
        ]
        if not auto:
            # Only reachable if config changed between predicate and handle.
            return ConsumerResult(status=Status.SKIP, message="no auto route matches")

        if ctx.dry_run:
            # Unreachable through the runner (it returns before `handle` on a
            # rehearsal) and kept anyway: 09 §5.6 says a dry run writes
            # NOTHING, and a direct caller must not discover that the hard way.
            LOG.info("[DRY-RUN] tag_router would apply %d route(s) to %s", len(auto), payload.path)
            return ConsumerResult(
                status=Status.SKIP,
                message=f"[DRY-RUN] would apply {len(auto)} route(s)",
                metadata={"would_apply": [_match_payload(m) for m in auto]},
            )

        return self._apply(payload, ctx, auto)

    # --- application -------------------------------------------------------

    def _apply(
        self, payload: NotePayload, ctx: RunContext, auto: list[RouteMatch]
    ) -> ConsumerResult:
        metadata: dict[str, Any] = {"applied": [_match_payload(match) for match in auto]}

        # Checked FIRST so the message names the real problem: without paths
        # the index cannot be opened either, and "not indexable" would send
        # the operator to [vault] ignore_patterns for a wiring fault (09 §1.5
        # — an error must be actionable).
        if ctx.paths is None:
            return ConsumerResult(
                status=Status.ERROR,
                message=(
                    "no CorePaths on the RunContext — the operation log, action "
                    "corpus and backup dir have nowhere to go (spec 10 §3)"
                ),
                metadata=metadata,
            )

        capture = self._capture_record(ctx, payload)
        if capture is None:
            return ConsumerResult(
                status=Status.ERROR,
                message=(
                    f"{payload.path} is inside the vault but not indexable — "
                    "check [vault] ignore_patterns / max_file_size"
                ),
                metadata=metadata,
            )

        op_context = self._operation_context(ctx)
        if op_context is None:  # pragma: no cover - paths were checked above
            return ConsumerResult(
                status=Status.ERROR,
                message="the operation context could not be built",
                metadata=metadata,
            )

        # 12 §2 / 11 §2: record which of the capture's tags the MACHINE added,
        # at decision time. This is the training signal for tagger quality —
        # it is how "the route fired on a tag Matt never wrote" is
        # distinguishable, after the fact, from "the route fired on his own".
        op_context = replace(op_context, auto_tags_present=self._auto_tags(payload))

        try:
            results = routes_mod.apply_all(op_context, capture, auto)
        except NotImplementedError as exc:
            # Now that apply_all has landed this branch means ONE thing: an
            # `auto` route in `integrate` mode, which routes refuses BEFORE
            # writing anything (doc 12 §1 is Phase 5). The message is the
            # refusal's own words — it names the route, the destination and
            # the phase — because "routes.apply_all is not available yet"
            # would be a silently wrong answer about a legal config.
            #
            # ERROR (not a raise) is the approved translation (ruling
            # fb62bab): the runner does not checkpoint an error, so the
            # capture is applied on the first run after the integrate path
            # lands — no backfill, no manual replay. The cost is one error
            # per matching note per run until then, which is deliberate:
            # `auto = true, mode = "integrate"` is a standing instruction
            # that cannot be carried out, and silence would leave Matt
            # believing it was.
            LOG.warning("tag_router: cannot apply a route to %s: %s", payload.path, exc)
            return ConsumerResult(
                status=Status.ERROR,
                message=str(exc),
                metadata=metadata,
            )
        except OrganizeError as exc:
            # ADDRESSING/validation failures raise (the error-line rule): a
            # no-ai target, an integrate refusal, a concurrent modification.
            LOG.warning("tag_router: apply_all refused %s: %s", payload.path, exc)
            return ConsumerResult(
                status=Status.ERROR, message=f"{type(exc).__name__}: {exc}", metadata=metadata
            )
        except Exception as exc:  # noqa: BLE001 - per-note failures never raise (06 §1)
            LOG.exception("tag_router: apply_all crashed on %s", payload.path)
            return ConsumerResult(
                status=Status.ERROR, message=f"{type(exc).__name__}: {exc}", metadata=metadata
            )

        results = list(results or ())
        metadata["results"] = [
            {
                "ok": bool(result.ok),
                "operation": str(result.operation),
                "destination": result.destination,
                "error": result.error,
                # CRITICAL-1 record honesty: a destination this run SKIPPED
                # because the vault already held it is visible here, never
                # silent. Without it a retried batch reports the same
                # successes as the first attempt and nothing distinguishes
                # "wrote it" from "found it already written".
                "already_delivered": bool(
                    (result.details or {}).get("already_delivered", False)
                ),
            }
            for result in results
        ]

        # WORLD-STATE failures come back as ok=False (the error-line rule): an
        # unwritable destination, a missing source. Spec 11 §1: any failure
        # leaves the capture UNARCHIVED and every attempt logged — apply_all
        # owns that; this reports it so the run exits 1 and retries next time.
        failures = [result for result in results if not result.ok]
        if failures:
            detail = "; ".join(
                f"{result.destination or '?'}: {result.error or 'failed'}" for result in failures
            )
            LOG.warning("tag_router: %d/%d destination(s) failed for %s — capture left "
                        "unarchived: %s", len(failures), len(results), payload.path, detail)
            return ConsumerResult(
                status=Status.ERROR,
                message=f"{len(failures)} of {len(results)} destination(s) failed: {detail}",
                metadata=metadata,
            )

        LOG.info(
            "tag_router: applied %d auto route(s) to %s",
            len(auto),
            payload.path,
        )
        return ConsumerResult(
            status=Status.SUCCESS,
            message="applied: " + ", ".join(match.route_name for match in auto),
            metadata=metadata,
        )

    # --- lazily-built run services ----------------------------------------

    @staticmethod
    def _auto_tags(payload: NotePayload) -> tuple[str, ...]:
        """The capture's ``auto_tags`` (11 §2), for the action record only.

        Deliberately NOT a routing input: routing evidence is ``tags`` and
        only ``tags``, so a tag Matt deleted from ``tags`` while it lingers
        in ``auto_tags`` cannot route (11 §2 acceptance 6).
        """
        raw = (payload.frontmatter or {}).get("auto_tags")
        if raw is None:
            return ()
        values = raw if isinstance(raw, list) else [raw]
        return tuple(str(value) for value in values if str(value).strip())

    def _capture_record(self, ctx: RunContext, payload: NotePayload) -> NoteRecord | None:
        """The ``NoteRecord`` the doc-05 API requires (08 §A14: never a bare
        string). Indexed on demand — the pipeline walks the vault itself and
        does not require a warm index."""
        index = self._vault_index(ctx)
        if index is None:
            return None
        record = index.get(payload.path)
        if record is None:
            record = index.update_file(payload.path)
        return record

    def _vault_index(self, ctx: RunContext) -> VaultIndex | None:
        if self._index is not None:
            return self._index
        if ctx.paths is None:
            return None
        index = VaultIndex(ctx.config, ctx.paths.index_path)
        index.load()
        self._index = index
        return index

    def _operation_context(self, ctx: RunContext) -> OperationContext | None:
        """The one mutating-path context (structural decision 1), built once
        per run on first real work.

        Built HERE rather than handed down because ``RunContext`` carries no
        operation context (Phase-3 shape) and ``cmd_run_consumers`` opens no
        ``VaultIndex``, so there is no second writer to clobber ``index.json``.
        The integrator has this queued as an ``op_context`` seam; when it
        lands this method becomes a one-line delegation.

        ``on_record`` is deliberately left unset — see the module docstring.
        """
        if self._op_context is not None:
            return self._op_context
        if ctx.paths is None:
            return None
        index = self._vault_index(ctx)
        if index is None:
            return None
        config = ctx.config
        self._op_context = OperationContext(
            config=config,
            index=index,
            oplog=OperationLog(ctx.paths.operations_log),
            recorder=ActionRecorder(ctx.paths.actions_dir),
            backup_dir=Path(config.vault.root) / config.file_ops.backup_dir,
            dry_run=bool(ctx.dry_run),
            actor=ACTOR,
            # 12 §2 targets[].description. fileops cannot import routes, so
            # the composing caller supplies the lookup (11 §3).
            describe=lambda folder: routes_mod.get_description(folder, index, config),
            # `suggestions_shown` / `chosen_rank` stay EMPTY on purpose: doc
            # 12 §2 stores the counterfactual the decider was shown, and an
            # unattended route firing showed nobody anything. Filling them
            # with the routes themselves would report a rank-1 "accept" for
            # every automated firing and corrupt `organize actions stats`.
        )
        return self._op_context
