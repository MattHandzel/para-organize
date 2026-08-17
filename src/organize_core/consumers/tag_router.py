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

Learning must not fold a route firing (ruling 4ffef89: "LEARNING FOLDS ONLY
MATT-DECIDED ACTIONS" — a route firing is CONFIG, not a decision Matt made;
folding it would make routes self-reinforcing and pollute the doc-12
accept-rate signal). Until Phase 5 that had to be a CALLER-side choice, so
this consumer built its own ``OperationContext`` with ``on_record`` unset
(ARCHITECTURE Phase-4 landing: "Omission ACCEPTED as a Phase-5 rider:
tag_router keeps its own OperationContext (on_record=None) until the
learn.record_action actor filter lands"). That filter has landed
(``learn.is_matt_decided``), so the enforcement is now CALLEE-side and this
module consumes the shared ``RunContext.op_context`` like every other
consumer — the conversion the rider called for. Two real bugs go with the
duplicate context: a second ``VaultIndex`` writing the same snapshot file
(``cmd_run_consumers``' end-of-run ``index.flush()`` never saw a routed
move, so the on-disk index still described the pre-run vault), and a second
``ActionRecorder``.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from organize_core import routes as routes_mod
from organize_core.config import Config, ConsumerConfig
from organize_core.consumers.base import (
    Consumer,
    ConsumerResult,
    NotePayload,
    RunContext,
    Status,
    register,
)
from organize_core.errors import ConfigError, ConsumerError, OrganizeError
from organize_core.fileops import OperationContext
from organize_core.index import NoteRecord
from organize_core.routes import RouteMatch

LOG = logging.getLogger(__name__)

#: The one ``[[routes]]`` mode that calls an LLM (spec 12 §1). ``move`` and
#: ``append`` are mechanical, which is why :meth:`TagRouterConsumer.wants_llm`
#: keys on this value and nothing else.
INTEGRATE_MODE = "integrate"

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
        # The run, set by bind(). None between runs; never None inside one
        # (the runner binds before any should_process — see bind()).
        self._run: RunContext | None = None

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

        Failure semantics (ruling 4ffef89, landed): a ``bind()`` that RAISES
        means the consumer is skipped for the run, counted as an ERROR in the
        06 §4 summary, and the run exits 1 — never "continue unbound", because
        an unbound tag_router silently filters every capture, which is the
        silent-outage class. Other consumers are unaffected. This
        implementation cannot raise.

        ``run_consumers`` calls this immediately after building the
        ``RunContext`` and before any ``should_process``; :meth:`handle` calls
        it too, so a direct caller gets the same guarantee.
        """
        self._run = ctx

    def wants_llm(self, config: Config | None = None) -> bool:
        """True iff some ``auto = true`` route runs in ``integrate`` mode.

        Doc 12 §1 makes ``integrate`` the one route mode that calls an LLM;
        ``move`` and ``append`` are mechanical and build no prompt. So this
        instance needs ``RunContext.llm`` exactly when an unattended route
        would integrate.

        THE STRUCTURAL BLOCKER, and how it is solved. ``wants_llm()`` drives
        CLIENT INJECTION (the record correction at 0f9e634: "``wants_llm()``
        […] drives LLM CLIENT INJECTION only; the class-level ``uses_llm``
        remains the runner's no-ai DENIAL flag"), and ``runner.run_consumers``
        calls it one line BEFORE it constructs the ``RunContext`` — so neither
        ``bind(ctx)`` (which runs after) nor ``ConsumerConfig`` (which carries
        only this consumer's own options) can hand the predicate
        ``Config.routes``. The Phase-4 docstring recorded the two ways out:
        "a config-aware ``wants_llm(config)`` or a two-step context build".

        This is the first: an OPTIONAL ``config`` parameter, which is a pure
        WIDENING of ``Consumer.wants_llm(self) -> bool``. Every existing
        zero-argument call site keeps working, and the runner can start
        passing the global config without a flag day. The change to
        ``base.py``/``runner.py`` that makes the runner pass it is the
        integrator's (both are shared files) and is raised as a seam; until it
        lands, a zero-argument call falls back to the bound run's config when
        there is one and otherwise answers ``False``.

        The unbound fallback is SAFE rather than merely convenient, and only
        because the ``no`` answer is currently PROVABLE: ``validate_config``
        rejects ``auto = true`` with ``mode = "integrate"`` outright
        (ARCHITECTURE "Phase-4 rulings, tag_router close": "auto=true +
        mode='integrate' fails AT CONFIG VALIDATION […] check lifts when
        Phase 5 lands"), so no loadable config can make the honest answer
        ``True`` while the check stands. It must not be left to a comment:
        ``test_wants_llm_unbound_fallback_expires_with_the_config_gate`` is a
        SELF-REMOVING seam probe (the pattern approved at fb62bab) that goes
        red the moment ``validate_config`` accepts such a route while the
        runner is still calling this with no argument — an unattended
        integrate route against ``ctx.llm = None`` is exactly the
        inert-in-production failure ``taskwarrior.llm_enabled`` had.
        """
        if config is None:
            run = self._run
            config = None if run is None else run.config
        if config is None:
            return False
        return any(
            bool(route.auto) and str(route.mode) == INTEGRATE_MODE
            for route in (config.routes or ())
        )

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
        run = self._run
        if run is None:
            # The interim "inert when unbound" branch is DELETED: the
            # ``Consumer.bind`` + ``run_consumers`` wiring landed (ARCHITECTURE
            # Phase-4 integration landing), and the same landing note asks for
            # this branch's cleanup. Returning False here would silently drop
            # every capture — the silent-outage class bind() exists to
            # prevent — so an unbound predicate is LOUD instead. Unreachable
            # through the runner, which binds before any should_process and
            # skips the whole consumer if bind raises (one error, not one per
            # note: the alert-storm rule from the tag_router-close ruling).
            raise ConsumerError(
                "tag_router.should_process was called before bind(): routes live "
                "on the global Config, which only bind() supplies",
                hint="call bind(RunContext) first; run_consumers does this "
                "immediately after building the context",
            )
        config = run.config

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

        # Checked FIRST so the message names the real problem: without the
        # recorded write path there is no index to look the capture up in
        # either, and "not indexable" would send the operator to [vault]
        # ignore_patterns for a wiring fault (09 §1.5 — an error must be
        # actionable). ERROR, never an unrecorded write: ARCHITECTURE
        # "Phase-4 rulings, auto_tagger batch" (f24ee2e) — "a consumer that
        # would write the vault with op_context=None emits Status.ERROR
        # rather than performing an unrecorded write […] errors retry, so the
        # run self-heals once the seam lands".
        op_context = self._operation_context(ctx)
        if op_context is None:
            return ConsumerResult(
                status=Status.ERROR,
                message=(
                    "no OperationContext on the RunContext — the operation log, "
                    "action corpus and backup dir have nowhere to go, and an "
                    "unrecorded vault write is never the answer (spec 12 §2)"
                ),
                metadata=metadata,
            )

        capture = self._capture_record(op_context, payload)
        if capture is None:
            return ConsumerResult(
                status=Status.ERROR,
                message=(
                    f"{payload.path} is inside the vault but not indexable — "
                    "check [vault] ignore_patterns / max_file_size"
                ),
                metadata=metadata,
            )

        # 12 §2 / 11 §2: record which of the capture's tags the MACHINE added,
        # at decision time. This is the training signal for tagger quality —
        # it is how "the route fired on a tag Matt never wrote" is
        # distinguishable, after the fact, from "the route fired on his own".
        op_context = replace(op_context, auto_tags_present=self._auto_tags(payload))

        try:
            # `llm` is consumed by `integrate`-mode matches ONLY (doc 12 §1 is
            # the one route mode that builds a prompt); move/append ignore it.
            # It is None unless `wants_llm(config)` said True, and routes
            # refuses an integrate route with no client BEFORE writing
            # anything — so a wiring fault is a loud pre-flight refusal, never
            # a half-applied batch.
            results = routes_mod.apply_all(op_context, capture, auto, llm=ctx.llm)
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

    @staticmethod
    def _capture_record(op_context: OperationContext, payload: NotePayload) -> NoteRecord | None:
        """The ``NoteRecord`` the doc-05 API requires (08 §A14: never a bare
        string). Indexed on demand — the pipeline walks the vault itself and
        does not require a warm index.

        Read from the COMPOSITION ROOT's index (``op_context.index``), which
        is the same object ``routes.apply_all`` mutates and the same one
        ``cmd_run_consumers`` flushes once at end of run. This consumer used
        to open a second ``VaultIndex`` over the same snapshot file, so a
        routed move updated an index nobody ever flushed and the on-disk
        snapshot still described the pre-run vault (ARCHITECTURE, "Phase-4
        rulings, tag_router close": "ONE end-of-run flush in
        cmd_run_consumers via op_context's index (composition root owns the
        index lifecycle once op_context lands)").
        """
        index = op_context.index
        record = index.get(payload.path)
        if record is None:
            record = index.update_file(payload.path)
        return record

    def _operation_context(self, ctx: RunContext) -> OperationContext | None:
        """The ONE recorded write path — supplied by the composition root.

        A one-line delegation, which is the whole point. It used to CONSTRUCT
        an ``OperationContext`` here, with its own ``VaultIndex``, its own
        ``OperationLog`` and its own ``ActionRecorder``, purely so it could
        leave ``on_record`` unset and stay out of ``learning.json``
        (ARCHITECTURE Phase-4 landing, "Omission ACCEPTED as a Phase-5
        rider"). ``learn.is_matt_decided`` now refuses a ``route:*`` /
        ``consumer:*`` record on the CALLEE side, so the shared context is
        safe to consume and the duplicate services are gone with it — the
        second index in particular, whose writes ``cmd_run_consumers``'
        end-of-run ``index.flush()`` could never see.

        ``run_consumers`` hands each consumer
        ``dataclasses.replace(op_context, actor="consumer:<type>",
        dry_run=dry_run)``, so :data:`ACTOR` is what arrives here; the
        composition root supplies ``describe`` (12 §2 ``targets[]
        .description``), the oplog, the recorder and the backup dir.

        ``suggestions_shown`` / ``chosen_rank`` stay EMPTY: doc 12 §2 stores
        the counterfactual the DECIDER was shown, and an unattended route
        firing showed nobody anything. Filling them with the routes
        themselves would report a rank-1 "accept" for every automated firing
        and corrupt ``organize actions stats``.
        """
        return ctx.op_context
