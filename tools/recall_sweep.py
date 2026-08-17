#!/usr/bin/env python3
"""Destination-recall sweep — the spec 21 §5 before/after evidence harness.

Spec 21 widens WHICH destinations are on the ballot (nested folders, then
notes).  §5 requires that the change be *proven* to help and proven not to
regress, on Matt's real vault, with the anti-SQ-1 metric set — because SQ-1
passed every aggregate it had while handing 1,450 captures the same 9-row
list of scoreless folders.  This script is that measurement.

It runs in two modes against the SAME command line:

    --mode baseline     the current, folders-only, depth-1 ranker
    --mode candidate    the ranker after the core change

Each run writes a JSON result file; ``--mode candidate`` (or ``--compare``)
prints the before/after table from two of them.  Nothing about the run is
inferred from the mode flag: the mode is CHECKED against the code that is
actually imported (:func:`code_capabilities`), so a baseline number can never
be recorded from widened code, or vice versa.

Real-data law (ARCHITECTURE.md): ``~/Obsidian/Main`` and ``~/notes`` are never
touched.  Every run is a disposable ``rsync`` of the read-only golden mirror
into ``--work``, with isolated ``ORGANIZE_CORE_{CONFIG,STATE,RUNTIME}_DIR``.
:func:`refuse_live_vault` is a hard refusal, not a convention, and every run
reports the two §5.1 safety lines (mirror still read-only; repo git status).

Reproduce (from the repo root, with the venv):

    .venv/bin/python tools/recall_sweep.py --mode baseline \\
        --work /tmp/recall-work --prepare

    # …core change lands…
    .venv/bin/python tools/recall_sweep.py --mode candidate \\
        --work /tmp/recall-work --baseline /tmp/recall-work/sweep-baseline.json

    # or, from two saved runs:
    .venv/bin/python tools/recall_sweep.py --compare A.json B.json

Sampling is never silent: ``--limit`` requires nothing but always prints the
sample size AND the ``--seed`` that produced it, and the seed is recorded in
the JSON.  The default is the whole backlog.
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:  # runnable without an editable install
    sys.path.insert(0, str(ROOT / "src"))

#: The read-only mirror.  NEVER the live vault (ARCHITECTURE.md real-data rule).
DEFAULT_MIRROR = Path("/home/matth/Projects/KnowledgeManagementSystem/vault-mirror-golden")

#: Roots this harness refuses to run against at any verbosity.  The rule is
#: "the live vault is never written"; a sweep only reads, but it also calls
#: ``organize index --full``, which writes state, and one typo in --work is
#: all it takes.  Refuse structurally instead of promising in prose.
FORBIDDEN_VAULT_ROOTS: tuple[Path, ...] = (
    Path.home() / "Obsidian" / "Main",
    Path.home() / "notes",
)

#: Spec 21 §3.4's default list, used for the "rank-1 driven SOLELY by a
#: generic token" metric.  In candidate mode the value is read from
#: ``suggestions.candidate_stopwords`` when the key exists; this literal is
#: the fallback so the BASELINE number is computable against code that has no
#: such key.  Deliberately a literal, not an import (anti-vacuity standard 2).
SPEC_STOPWORDS: tuple[str, ...] = ("mind", "self", "me", "text", "voice", "note", "thought")

#: Reason strings whose first quoted group is the CAPTURE-side token
#: (suggest.calculate_score).  "Context matches folder" and "Previously used
#: destination" carry no token and can never be stopword-driven.
_REASON_TOKEN = re.compile(r"^(?:Tag|Source|Alias) '([^']*)'")

ARCHIVE_TYPE = "archive"


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class SweepError(RuntimeError):
    """One attributable error line, never a bare traceback (house law)."""


# --------------------------------------------------------------------------
# safety refusals — structural, with the guard-deleted test in tests/
# --------------------------------------------------------------------------


def refuse_live_vault(root: Path, mirror: Path) -> None:
    """Refuse a work vault that IS, or is inside, the live vault or the
    read-only golden mirror.

    The mirror is refused too: it is checksum-verified against
    ``~/Obsidian/Main`` and a sweep that indexed it in place would make it
    writable-in-practice and silently stop being a golden.
    """
    resolved = Path(root).expanduser().resolve()
    for forbidden in (*FORBIDDEN_VAULT_ROOTS, Path(mirror).expanduser().resolve()):
        if resolved == forbidden or forbidden in resolved.parents:
            raise SweepError(
                f"refusing to sweep against {resolved}: it is inside the live vault or the "
                f"read-only golden mirror ({forbidden}) — rsync a disposable copy and point "
                f"--work at that instead"
            )


def refuse_mode_mismatch(mode: str, caps: dict[str, bool]) -> None:
    """Refuse to label a run with a mode the imported code contradicts.

    A "baseline" recorded from widened code, or a "candidate" recorded from
    the old code, is worse than no measurement: it is a number that looks
    like evidence.  Both directions refuse.
    """
    widened = any(caps.values())
    if mode == "baseline" and widened:
        fired = sorted(name for name, present in caps.items() if present)
        raise SweepError(
            "refusing to record a BASELINE from widened code: "
            f"{', '.join(fired)} present — check out the pre-spec-21 core, or run "
            "--mode candidate (--no-mode-check overrides, and taints the JSON)"
        )
    if mode == "candidate" and not widened:
        raise SweepError(
            "refusing to record a CANDIDATE from unwidened code: none of "
            f"{', '.join(sorted(caps))} is present — land the spec 21 core change first "
            "(--no-mode-check overrides, and taints the JSON)"
        )


def code_capabilities() -> dict[str, bool]:
    """Which spec-21 widenings the IMPORTED core actually has.

    Each probe names a §-clause obligation, so a partial landing is visible
    rather than averaged away.  Every value False ⇒ pre-change code.
    """
    from organize_core import suggest as suggest_mod
    from organize_core.index import VaultIndex

    candidate_fields = {f.name for f in dataclasses.fields(suggest_mod.Candidate)}
    suggestion_fields = {f.name for f in dataclasses.fields(suggest_mod.Suggestion)}
    suggestions_cfg = _suggestions_config_fields()
    return {
        # §2.3 — a SEPARATE accessor, para_subfolders keeps its depth-1 meaning
        "index.candidate_folders": hasattr(VaultIndex, "candidate_folders"),
        # §1.4 — candidates carry {..., kind}
        "Candidate.kind": "kind" in candidate_fields,
        # §3.3 — additive wire field, never inferred from a trailing .md
        "Suggestion.destination_kind": "destination_kind" in suggestion_fields,
        # §3.7 — the four core config keys
        "config.max_candidate_depth": "max_candidate_depth" in suggestions_cfg,
        "config.note_candidates": "note_candidates" in suggestions_cfg,
        "config.max_note_suggestions": "max_note_suggestions" in suggestions_cfg,
        "config.candidate_stopwords": "candidate_stopwords" in suggestions_cfg,
    }


def _suggestions_config_fields() -> set[str]:
    from organize_core import config as config_mod

    cls = getattr(config_mod, "SuggestionsConfig", None)
    if cls is None or not dataclasses.is_dataclass(cls):
        return set()
    return {f.name for f in dataclasses.fields(cls)}


# --------------------------------------------------------------------------
# work-tree preparation (spec 21 §5.1, verbatim)
# --------------------------------------------------------------------------


def prepare_work(work: Path, mirror: Path, *, quiet: bool = False) -> None:
    """rsync the mirror, make the copy writable, write an isolated config
    pointed at it, and build a full index.  Idempotent and re-runnable."""
    mirror = Path(mirror).expanduser().resolve()
    if not mirror.is_dir():
        raise SweepError(f"golden mirror not found: {mirror}")
    vault = work / "vault"
    refuse_live_vault(vault, mirror)
    for sub in ("cfg", "state", "runtime"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)

    _run(["rsync", "-a", "--delete", f"{mirror}/", f"{vault}/"], quiet=quiet)
    _run(["chmod", "-R", "u+w", str(vault)], quiet=quiet)

    from organize_core.config import example_config_toml

    text = example_config_toml() if callable(example_config_toml) else str(example_config_toml)
    lines = []
    replaced = 0
    for line in text.splitlines():
        if line.startswith("root = ") and replaced == 0:
            lines.append(f'root = "{vault}"')
            replaced += 1
        else:
            lines.append(line)
    if replaced != 1:
        raise SweepError(
            f"example config had {replaced} `root = ` lines, expected exactly 1 — "
            "the config shape changed and this harness must be updated with it"
        )
    (work / "cfg" / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = _run(
        [sys.executable, "-m", "organize_core.cli", "index", "--full"],
        env=sweep_env(work),
        quiet=quiet,
        capture=True,
    )
    tail = (result.stdout or "").strip().splitlines()
    if not quiet and tail:
        print(f"  index: {tail[-1]}")


def sweep_env(work: Path) -> dict[str, str]:
    """The isolated environment every child process runs under."""
    env = dict(os.environ)
    env["ORGANIZE_CORE_CONFIG_DIR"] = str(work / "cfg")
    env["ORGANIZE_CORE_STATE_DIR"] = str(work / "state")
    env["ORGANIZE_CORE_RUNTIME_DIR"] = str(work / "runtime")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    return env


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    quiet: bool = False,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    if not quiet:
        print(f"  $ {' '.join(argv[:4])}{' …' if len(argv) > 4 else ''}")
    result = subprocess.run(
        argv,
        cwd=str(ROOT),
        env=env,
        capture_output=capture,
        text=True,
    )
    if result.returncode != 0:
        raise SweepError(f"command failed ({result.returncode}): {' '.join(argv)}")
    return result


# --------------------------------------------------------------------------
# the ranker under measurement
# --------------------------------------------------------------------------


@dataclass
class Rig:
    """Everything one warm sweep needs, built once."""

    work: Path
    paths: Any
    config: Any
    index: Any
    learning: Any
    candidates: list[Any]
    archive_path: str
    builder: str
    build_ms: float
    routes_defined: int
    learning_associations: int


def build_rig(work: Path, *, learning_file: Path | None) -> Rig:
    from organize_core import learn as learn_mod
    from organize_core.config import load_config
    from organize_core.index import VaultIndex
    from organize_core.paths import CorePaths

    paths = CorePaths.resolve(
        config_dir=str(work / "cfg"),
        state_dir=str(work / "state"),
        runtime_dir=str(work / "runtime"),
    )
    config = load_config(paths)
    refuse_live_vault(config.vault.root, DEFAULT_MIRROR)
    index = VaultIndex(config, paths.index_path)
    index.load()
    learning = learn_mod.load_learning(
        Path(learning_file) if learning_file else paths.learning_path
    )

    started = time.perf_counter()
    builder, candidates = build_candidates(index, config)
    build_ms = (time.perf_counter() - started) * 1000.0

    archives = config.vault.para_folders.get("archives", "archive")
    archive_path = str((config.vault.root / archives / config.vault.archive_capture_path).resolve())

    return Rig(
        work=work,
        paths=paths,
        config=config,
        index=index,
        learning=learning,
        # NOT coerced to a list: post-change this is a `CandidateSet` whose
        # inverted indexes are the whole point of §3.5, and flattening it
        # would measure the linear scan the change exists to remove.
        candidates=candidates,
        archive_path=archive_path,
        builder=builder,
        build_ms=build_ms,
        routes_defined=len(getattr(config, "routes", ()) or ()),
        learning_associations=len(getattr(learning, "associations", ()) or ()),
    )


def build_candidates(index: Any, config: Any) -> tuple[str, list[Any]]:
    """Build the ballot the SHIPPED code would build, across both worlds.

    The candidate-set builder is exactly what spec 21 changes, so this
    harness cannot hardcode its signature.  Each strategy below is a known
    shape; the first that fits wins and its NAME is recorded in the JSON, so
    a run can never be read without knowing which door it came through.  An
    unknown shape is a loud refusal — silently falling back to the depth-1
    builder would report the pre-change ballot as the after number.

    ``--cli-parity`` is the connection pin on this: whatever door fires here
    must produce the same ranked list as the real ``organize suggest``.
    """
    from organize_core import cli as cli_mod
    from organize_core import suggest as suggest_mod

    # (a) The ballot builder `organize suggest` itself walks through, once
    #     the core exposes one.  Preferred over every reconstruction below:
    #     a harness that rebuilt the ballot by hand would measure its own
    #     idea of the change rather than the shipped one.
    shipped = getattr(cli_mod, "_candidates", None)
    if shipped is not None and set(inspect.signature(shipped).parameters) == {"index", "config"}:
        return "cli._candidates(index, config)", shipped(index, config)

    # (b) the §3.5 indexed ballot, assembled here when the CLI door moved.
    build_set = getattr(suggest_mod, "build_candidate_set", None)
    if build_set is not None:
        params = inspect.signature(build_set).parameters
        mapping = _candidate_folder_mapping(index, config)
        kwargs: dict[str, Any] = {}
        if "notes" in params:
            kwargs["notes"] = _candidate_notes(index, config)
        if "config" in params:
            kwargs["config"] = config.suggestions
        return "suggest.build_candidate_set(...)", build_set(mapping, **kwargs)

    generate = getattr(suggest_mod, "generate_candidates", None)
    if generate is None:
        raise SweepError(
            "neither cli._candidates, suggest.build_candidate_set nor "
            "suggest.generate_candidates exists — the ballot builder moved and "
            "tools/recall_sweep.py build_candidates() must be taught the new door"
        )
    params = inspect.signature(generate).parameters

    # (c) today's shape: one positional mapping of PARA key -> folder paths.
    if len(params) == 1:
        mapping = _candidate_folder_mapping(index, config)
        return "generate_candidates(para_subfolders)", list(generate(mapping))

    raise SweepError(
        "unrecognized suggest.generate_candidates signature "
        f"({', '.join(params)}) — add the new shape to tools/recall_sweep.py "
        "build_candidates() rather than letting the sweep guess"
    )


def _candidate_folder_mapping(index: Any, config: Any) -> dict[str, list[str]]:
    """``{para key: [folder path]}``.

    Prefers the §2.3 accessor (``candidate_folders``, which honors
    ``suggestions.max_candidate_depth``) and falls back to today's depth-1
    ``para_subfolders``.  The CLI's own helper wins over both when it is
    still shaped ``(index, config)``, so the harness follows the shipped
    path rather than re-deriving it.
    """
    from organize_core import cli as cli_mod

    helper = getattr(cli_mod, "_candidate_folders", None)
    if helper is not None:
        try:
            return helper(index, config)
        except AttributeError:  # a stub index the helper's accessor needs
            pass

    accessor = getattr(index, "candidate_folders", None)
    if accessor is not None:
        return {key: [str(p) for p in paths] for key, paths in accessor().items()}

    return {key: [str(p) for p in index.para_subfolders(key)] for key in config.vault.para_folders}


def _candidate_notes(index: Any, config: Any) -> list[Any]:
    """The note ballot (§3.1), off when ``suggestions.note_candidates`` is
    false — the §3.6 switch, honored here so ``--mode candidate`` with the
    switch off really does reproduce the folders-only run."""
    if not bool(getattr(config.suggestions, "note_candidates", True)):
        return []
    accessor = getattr(index, "candidate_notes", None)
    if accessor is not None:
        return list(accessor())
    raise SweepError(
        "suggest.build_candidate_set wants notes but the index exposes no "
        "candidate_notes() — teach tools/recall_sweep.py the new accessor"
    )


def _all_notes_criteria() -> Any:
    """An unfiltered query — the backlog is selected by PATH, not by
    ``processing_status``, so ``--backlog all`` really is all 2,445 capture
    files and not the 1,858 the session filter keeps."""
    from organize_core.index import QueryCriteria

    return QueryCriteria()


# --------------------------------------------------------------------------
# the backlog
# --------------------------------------------------------------------------


def load_backlog(rig: Rig, which: str) -> list[Any]:
    """``all`` — every indexed note under ``vault.raw_capture_folder`` (spec
    21 §5.3's 2,445 denominator, the one every % in that table is over).
    ``session`` — exactly what ``session.start_session`` hands the UI
    (status=raw + para_type=capture; 1,858 on the mirror)."""
    from organize_core import session as session_mod

    if which == "session":
        return list(session_mod.start_session(rig.index).captures)
    if which != "all":
        raise SweepError(f"unknown --backlog {which!r} (expected 'all' or 'session')")
    raw_root = str((rig.config.vault.root / rig.config.vault.raw_capture_folder).resolve())
    records = [
        record
        for record in rig.index.query(_all_notes_criteria())
        if str(record.path).startswith(raw_root + os.sep)
    ]
    records.sort(key=lambda record: str(record.path))
    return records


# --------------------------------------------------------------------------
# per-capture ranking
# --------------------------------------------------------------------------


@dataclass
class Row:
    rel: str
    paths: list[str] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    routes: list[bool] = field(default_factory=list)
    reason_counts: list[int] = field(default_factory=list)
    top_reasons: list[str] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    ms: float = 0.0
    #: note rows the same-name collapse dropped (21 §3.2) — reported so a
    #: silent drop is never invisible; always 0 before the change.
    suppressed: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "rel": self.rel,
            "paths": self.paths,
            "scores": self.scores,
            "kinds": self.kinds,
            "types": self.types,
            "routes": self.routes,
            "reason_counts": self.reason_counts,
            "top_reasons": self.top_reasons,
            "tokens": self.tokens,
            "ms": round(self.ms, 4),
            "suppressed": self.suppressed,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Row:
        return cls(
            rel=payload["rel"],
            paths=list(payload.get("paths", ())),
            scores=list(payload.get("scores", ())),
            kinds=list(payload.get("kinds", ())),
            types=list(payload.get("types", ())),
            routes=list(payload.get("routes", ())),
            reason_counts=list(payload.get("reason_counts", ())),
            top_reasons=list(payload.get("top_reasons", ())),
            tokens=list(payload.get("tokens", ())),
            ms=float(payload.get("ms", 0.0)),
            suppressed=int(payload.get("suppressed", 0)),
        )


def rank_full(rig: Rig, record: Any, *, now: float) -> tuple[list[Any], float, int]:
    """Score one capture on the WARM path — the same calls
    ``cli.cmd_suggest`` makes, minus the per-invocation index load and the
    description lookup (neither touches path/type/score/reasons; the CLI
    parity check is what proves that).

    Returns the full ranked list (archive row included, as the CLI returns
    it) and the milliseconds spent inside ``suggest()`` alone — route
    merging is the layer above and is not what §3.5's budget bounds.
    """
    from organize_core import routes as routes_mod
    from organize_core import suggest as suggest_mod
    from organize_core.suggest import CaptureFeaturesView

    capture = CaptureFeaturesView.from_record(record)
    # `rank()` is `suggest()` plus the counters the CLI reports; when the
    # core has it, use it, so `suppressed_duplicates` (21 §3.2) is measured
    # rather than assumed to be zero.
    entry = getattr(suggest_mod, "rank", None) or suggest_mod.suggest
    kwargs: dict[str, Any] = {"now": now, "archive_path": rig.archive_path}
    if "exclude_path" in inspect.signature(entry).parameters:
        # A note is never a candidate for itself (21 §3.1) — cmd_suggest
        # passes exactly this, and omitting it would let the harness score a
        # capture into itself and report a suggestion the CLI never returns.
        kwargs["exclude_path"] = str(record.path)

    started = time.perf_counter()
    result = entry(
        capture,
        rig.candidates,
        rig.config.suggestions,
        rig.learning,
        **kwargs,
    )
    ranker_ms = (time.perf_counter() - started) * 1000.0

    suggestions = list(getattr(result, "suggestions", result))
    suppressed = int(getattr(result, "suppressed_duplicates", 0))
    ranked = routes_mod.merge_route_suggestions(
        routes_mod.resolve(list(record.tags), rig.config), suggestions
    )
    return list(ranked), ranker_ms, suppressed


def rank_one(rig: Rig, record: Any, *, now: float) -> tuple[Row, float]:
    """One capture's :class:`Row` — the compact per-capture record every
    metric is computed from.  The archive entry is EXCLUDED: "a real
    suggestion" means a non-archive one (spec 21 §5.3)."""
    ranked, ranker_ms, suppressed = rank_full(rig, record, now=now)
    root = str(rig.config.vault.root)
    row = Row(rel=_rel(str(record.path), root), ms=ranker_ms, suppressed=suppressed)
    for item in ranked:
        if item.type == ARCHIVE_TYPE:
            continue
        row.paths.append(_rel(item.path, root))
        row.scores.append(round(float(item.score), 4))
        row.types.append(str(item.type))
        row.kinds.append(str(getattr(item, "destination_kind", "folder") or "folder"))
        row.routes.append(bool(item.route))
        row.reason_counts.append(len(item.reasons))
        if not row.top_reasons:
            row.top_reasons = list(item.reasons)
    row.tokens = sorted({*(str(t) for t in record.tags), *(str(s) for s in record.sources)})
    return row, ranker_ms


def _rel(path: str, root: str) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# metrics (spec 21 §5.3)
# --------------------------------------------------------------------------


def summarize(rows: list[Row], stopwords: list[str]) -> dict[str, Any]:
    from organize_core import frontmatter

    total = len(rows)
    covered = [row for row in rows if row.paths]
    zero = total - len(covered)

    rank1 = Counter(row.paths[0] for row in covered)
    largest_cluster = Counter(tuple(row.paths) for row in covered)
    empty_reason = sum(
        1 for row in rows for count in row.reason_counts if count == 0
    )
    top_with_reason = sum(1 for row in covered if row.reason_counts and row.reason_counts[0] > 0)

    stop = {frontmatter.normalize_tag(word) for word in stopwords}
    solely_stopword = 0
    for row in covered:
        reasons = row.top_reasons
        if not reasons:
            continue
        tokens = [_REASON_TOKEN.match(reason) for reason in reasons]
        if any(match is None for match in tokens):
            continue
        if all(frontmatter.normalize_tag(match.group(1)) in stop for match in tokens if match):
            solely_stopword += 1

    lengths = [len(row.paths) for row in rows]
    top_scores = [row.scores[0] for row in covered]
    times = sorted(row.ms for row in rows)

    kind1 = Counter(row.kinds[0] for row in covered)
    biggest_dest, biggest_dest_n = (rank1.most_common(1) or [("", 0)])[0]
    biggest_list, biggest_list_n = (largest_cluster.most_common(1) or [((), 0)])[0]

    # Route suggestions (doc 11 §1) are prepended ABOVE the scored list and
    # are not recall: a route is a destination Matt already wrote into his
    # config.  They are kept in the headline numbers because they are what
    # `organize suggest` really returns, and reported separately so a route
    # row can never be counted as a candidate-set win.
    scored_covered = [row for row in covered if not all(row.routes)]
    route_rank1 = sum(1 for row in covered if row.routes and row.routes[0])
    route_only = sum(1 for row in covered if row.routes and all(row.routes))

    return {
        "captures": total,
        "with_suggestions": len(covered),
        "with_suggestions_pct": _pct(len(covered), total),
        "with_scored_suggestions": len(scored_covered),
        "with_scored_suggestions_pct": _pct(len(scored_covered), total),
        # The headline spec 21 §1.2 number: archive-only from the RANKER's
        # point of view.  568 / 1,877 on the mirror, and the figure the
        # doc's 23.2% / 76.8% are computed from.
        "zero_scored_suggestions": total - len(scored_covered),
        "zero_scored_suggestions_pct": _pct(total - len(scored_covered), total),
        "rank1_is_route": route_rank1,
        "covered_by_routes_only": route_only,
        "zero_suggestions": zero,
        "zero_suggestions_pct": _pct(zero, total),
        "distinct_rank1": len(rank1),
        "top_rank1_destination": biggest_dest,
        "top_rank1_destination_n": biggest_dest_n,
        "top_rank1_destination_pct": _pct(biggest_dest_n, len(covered)),
        "rank1_entropy_bits": _entropy(rank1),
        "largest_identical_list": biggest_list_n,
        "largest_identical_list_paths": list(biggest_list),
        "suggestions_with_empty_reasons": empty_reason,
        "top_has_reason": top_with_reason,
        "top_has_reason_pct_of_covered": _pct(top_with_reason, len(covered)),
        "top_has_reason_pct_of_all": _pct(top_with_reason, total),
        "rank1_solely_stopword": solely_stopword,
        "rank1_note": kind1.get("note", 0),
        "rank1_folder": kind1.get("folder", 0),
        "median_list_length": _median(lengths),
        "p95_list_length": _quantile(sorted(lengths), 0.95),
        "median_top_score": _median(top_scores),
        "suppressed_duplicates_total": sum(row.suppressed for row in rows),
        "captures_with_suppressed_duplicates": sum(1 for row in rows if row.suppressed),
        "ranker_ms_p50": _quantile(times, 0.50),
        "ranker_ms_p95": _quantile(times, 0.95),
        "ranker_ms_mean": round(statistics.fmean(times), 4) if times else 0.0,
        "ranker_ms_max": round(times[-1], 4) if times else 0.0,
    }


def compare_rows(base: list[Row], cand: list[Row]) -> dict[str, Any]:
    """Rank-1 stability and previous-rank-1 survival (§5.3), over the
    captures BOTH runs scored.  A capture missing from one side is reported,
    never quietly dropped from the denominator."""
    base_by = {row.rel: row for row in base}
    cand_by = {row.rel: row for row in cand}
    shared = sorted(set(base_by) & set(cand_by))

    had_before = [rel for rel in shared if base_by[rel].paths]
    same_rank1 = sum(1 for rel in had_before if cand_by[rel].paths[:1] == base_by[rel].paths[:1])
    survived = sum(1 for rel in had_before if base_by[rel].paths[0] in set(cand_by[rel].paths))
    vanished = [rel for rel in had_before if base_by[rel].paths[0] not in set(cand_by[rel].paths)]
    newly_covered = sum(1 for rel in shared if not base_by[rel].paths and cand_by[rel].paths)
    lost_coverage = [rel for rel in shared if base_by[rel].paths and not cand_by[rel].paths]

    return {
        "shared_captures": len(shared),
        "only_in_baseline": sorted(set(base_by) - set(cand_by))[:20],
        "only_in_candidate": sorted(set(cand_by) - set(base_by))[:20],
        "had_rank1_before": len(had_before),
        "rank1_unchanged": same_rank1,
        "rank1_unchanged_pct": _pct(same_rank1, len(had_before)),
        "previous_rank1_survived": survived,
        "previous_rank1_survived_pct": _pct(survived, len(had_before)),
        "previous_rank1_vanished": len(vanished),
        "previous_rank1_vanished_examples": vanished[:20],
        "newly_covered": newly_covered,
        "lost_coverage": len(lost_coverage),
        "lost_coverage_examples": lost_coverage[:20],
    }


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 2) if whole else 0.0


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 4) if values else 0.0


def _quantile(ordered: list[float], q: float) -> float:
    """Nearest-rank quantile over an ALREADY SORTED list."""
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(q * len(ordered)))
    return round(float(ordered[rank - 1]), 4)


def _entropy(counter: Counter[str]) -> float:
    total = sum(counter.values())
    if not total:
        return 0.0
    return round(
        -sum((n / total) * math.log2(n / total) for n in counter.values() if n), 4
    )


# --------------------------------------------------------------------------
# the CLI-path connection pin
# --------------------------------------------------------------------------


def cli_parity(
    rig: Rig, records: list[Any], sample: int, rng: random.Random
) -> dict[str, Any]:
    """Run the REAL ``organize suggest --json`` for a sample and require the
    warm path to agree exactly on (path, type, score, reasons).

    This is the must-not-drift pin: the warm loop exists only to be fast, and
    a harness that measured its own private ranker would produce numbers no
    shipped code ever produces.  Also yields the §5.4 CLI-path timing, which
    includes the ~726 ms index deserialization the warm path excludes.
    """
    if sample <= 0 or not records:
        return {"checked": 0, "matched": 0, "mismatches": [], "cli_ms_p50": 0.0, "cli_ms_p95": 0.0}

    chosen = rng.sample(records, min(sample, len(records)))
    env = sweep_env(rig.work)
    matched = 0
    mismatches: list[dict[str, Any]] = []
    durations: list[float] = []
    for record in chosen:
        started = time.perf_counter()
        result = subprocess.run(
            [sys.executable, "-m", "organize_core.cli", "suggest", "--json", str(record.path)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        durations.append((time.perf_counter() - started) * 1000.0)
        if result.returncode != 0:
            mismatches.append({"rel": str(record.path), "error": (result.stderr or "").strip()[:200]})
            continue
        payload = json.loads(result.stdout)
        cli_view = [
            (item["path"], item["type"], round(float(item["score"]), 4), tuple(item["reasons"]))
            for item in payload["suggestions"]
        ]
        ranked, _, _ = rank_full(rig, record, now=time.time())
        warm_view = [
            (item.path, item.type, round(float(item.score), 4), tuple(item.reasons))
            for item in ranked
        ]
        if cli_view == warm_view:
            matched += 1
        else:
            mismatches.append(
                {
                    "rel": str(record.path),
                    "cli": cli_view[:5],
                    "warm": warm_view[:5],
                }
            )
    durations.sort()
    return {
        "checked": len(chosen),
        "matched": matched,
        "mismatches": mismatches[:5],
        "cli_ms_p50": _quantile(durations, 0.50),
        "cli_ms_p95": _quantile(durations, 0.95),
    }


# --------------------------------------------------------------------------
# §5.1 safety report
# --------------------------------------------------------------------------


def safety_report(mirror: Path, repo: Path | None = None) -> dict[str, Any]:
    """The two lines every §5.1 run must print: the golden mirror still has
    zero writable entries, and the repo's git status is what it was.

    ``repo`` is separate from :data:`ROOT` on purpose: a BASELINE run is
    taken against a pristine ``git archive HEAD`` extraction (so an
    in-progress core change in the working tree cannot contaminate the
    before number), and that extraction is not a git worktree.  The rev
    still has to be recorded, so it is read from the real repo.
    """
    repo = Path(repo or ROOT)
    mirror = Path(mirror).expanduser()
    writable = 0
    checked = 0
    if mirror.is_dir():
        for dirpath, dirnames, filenames in os.walk(mirror):
            for name in (*dirnames, *filenames):
                checked += 1
                if os.access(os.path.join(dirpath, name), os.W_OK):
                    writable += 1
        checked += 1
        if os.access(mirror, os.W_OK):
            writable += 1
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    lines = [line for line in (status.stdout or "").splitlines() if line.strip()]
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(repo), capture_output=True, text=True
    )
    return {
        "repo": str(repo),
        "code_root": str(ROOT),
        "mirror": str(mirror),
        "mirror_entries_checked": checked,
        "mirror_writable_entries": writable,
        "git_head": (head.stdout or "").strip(),
        "git_status_lines": len(lines),
        "git_status": lines[:40],
    }


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def run_sweep(args: argparse.Namespace) -> dict[str, Any]:
    work = Path(args.work).expanduser().resolve()
    mirror = Path(args.mirror).expanduser().resolve()

    prepared = False
    if args.prepare or not (work / "state" / "index.json").exists():
        if args.no_prepare:
            raise SweepError(
                f"no index at {work / 'state' / 'index.json'} and --no-prepare was given — "
                "drop --no-prepare or point --work at a prepared tree"
            )
        print(f"preparing {work} from {mirror} …")
        prepare_work(work, mirror, quiet=args.quiet)
        prepared = True

    caps = code_capabilities()
    tainted = False
    try:
        refuse_mode_mismatch(args.mode, caps)
    except SweepError:
        if not args.no_mode_check:
            raise
        tainted = True
        print("!! --no-mode-check: this run's `mode` label is NOT backed by the imported code")

    rig = build_rig(work, learning_file=args.learning)
    records = load_backlog(rig, args.backlog)
    population = len(records)

    rng = random.Random(args.seed)
    sampled = False
    if args.limit and args.limit < population:
        records = sorted(rng.sample(records, args.limit), key=lambda r: str(r.path))
        sampled = True

    now = float(args.now) if args.now is not None else time.time()

    stopwords = list(
        getattr(rig.config.suggestions, "candidate_stopwords", None) or SPEC_STOPWORDS
    )

    print(
        f"mode={args.mode} backlog={args.backlog} candidates={len(rig.candidates)} "
        f"builder={rig.builder} build={rig.build_ms:.0f}ms"
    )
    if sampled:
        print(
            f"!! SAMPLED: {len(records)} of {population} captures, seed={args.seed} "
            f"— NOT the full backlog"
        )
    else:
        print(f"backlog: {len(records)} captures (the whole {args.backlog} population)")

    rows: list[Row] = []
    wall_started = time.perf_counter()
    for record in records:
        row, _ = rank_one(rig, record, now=now)
        rows.append(row)
    wall_s = time.perf_counter() - wall_started

    parity = cli_parity(rig, records, args.cli_parity, random.Random(args.seed))
    summary = summarize(rows, stopwords)

    result = {
        "meta": {
            "mode": args.mode,
            "mode_check_bypassed": tainted,
            "capabilities": caps,
            "widened_code": any(caps.values()),
            "candidate_builder": rig.builder,
            "candidate_count": len(rig.candidates),
            "candidate_build_ms": round(rig.build_ms, 2),
            "backlog": args.backlog,
            "backlog_population": population,
            "sampled": sampled,
            "sample_size": len(rows),
            "seed": args.seed,
            "now": now,
            "stopwords": stopwords,
            "routes_defined": rig.routes_defined,
            "learning_associations": rig.learning_associations,
            "learning_file": str(args.learning) if args.learning else None,
            "work": str(work),
            "vault_root": str(rig.config.vault.root),
            "indexed_notes": rig.index.stats().get("total"),
            "index_capture_backlog": rig.index.stats().get("capture_backlog"),
            "prepared_this_run": prepared,
            "wall_seconds": round(wall_s, 2),
            "python": sys.version.split()[0],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "max_suggestions": rig.config.suggestions.max_suggestions,
            "min_confidence": rig.config.suggestions.learning.min_confidence,
        },
        "safety": safety_report(mirror, args.repo),
        "cli_parity": parity,
        "summary": summary,
        "rows": [row.as_json() for row in rows],
    }
    return result


def print_report(result: dict[str, Any]) -> None:
    meta = result["meta"]
    summary = result["summary"]
    safety = result["safety"]
    parity = result["cli_parity"]

    print()
    print(f"=== recall sweep — {meta['mode'].upper()} " + "=" * 34)
    print(f"  code            : head {safety['git_head']}, widened={meta['widened_code']}")
    print(f"  candidates      : {meta['candidate_count']} via {meta['candidate_builder']}")
    print(f"  backlog         : {meta['sample_size']} of {meta['backlog_population']} "
          f"({meta['backlog']}){' SAMPLED seed=' + str(meta['seed']) if meta['sampled'] else ''}")
    print(f"  index           : {meta['indexed_notes']} notes; "
          f"learning assoc={meta['learning_associations']}; routes={meta['routes_defined']}")
    print()
    _metric("captures with >=1 real suggestion", summary["with_suggestions"], summary["with_suggestions_pct"])
    _metric("captures with ZERO real suggestions", summary["zero_suggestions"], summary["zero_suggestions_pct"])
    _metric("…SCORED only (no config route)",
            summary["with_scored_suggestions"], summary["with_scored_suggestions_pct"])
    _metric("ZERO scored suggestions (the §1.2 headline)",
            summary["zero_scored_suggestions"], summary["zero_scored_suggestions_pct"])
    _metric("rank-1 is a route / covered by routes only",
            f"{summary['rank1_is_route']} / {summary['covered_by_routes_only']}", None)
    _metric("top suggestion carries >=1 reason", summary["top_has_reason"], summary["top_has_reason_pct_of_covered"], "of covered")
    _metric("suggestions with an EMPTY reason list", summary["suggestions_with_empty_reasons"], None)
    _metric("distinct rank-1 destinations", summary["distinct_rank1"], None)
    _metric(f"biggest rank-1 destination ({summary['top_rank1_destination'] or '-'})",
            summary["top_rank1_destination_n"], summary["top_rank1_destination_pct"], "of covered")
    _metric("rank-1 entropy (bits)", summary["rank1_entropy_bits"], None)
    _metric("largest identical-list cluster", summary["largest_identical_list"], None)
    _metric("rank-1 driven SOLELY by a stopword", summary["rank1_solely_stopword"], None)
    _metric("rank-1 is a note / a folder",
            f"{summary['rank1_note']} / {summary['rank1_folder']}", None)
    _metric("median / p95 list length",
            f"{summary['median_list_length']} / {summary['p95_list_length']}", None)
    _metric("median top score", summary["median_top_score"], None)
    _metric("suppressed duplicate note rows (21 §3.2)",
            f"{summary['suppressed_duplicates_total']} over "
            f"{summary['captures_with_suppressed_duplicates']} captures", None)
    print()
    _metric("ranker ms p50 / p95 / max",
            f"{summary['ranker_ms_p50']} / {summary['ranker_ms_p95']} / {summary['ranker_ms_max']}", None)
    _metric("CLI path ms p50 / p95",
            f"{parity['cli_ms_p50']} / {parity['cli_ms_p95']}", None)
    _metric("CLI parity (warm == organize suggest)",
            f"{parity['matched']}/{parity['checked']}", None)
    if parity["mismatches"]:
        print("  !! CLI PARITY MISMATCH — the warm path is not the shipped path:")
        for item in parity["mismatches"]:
            print(f"     {item}")
    print()
    print(f"  golden mirror   : {safety['mirror_writable_entries']} writable of "
          f"{safety['mirror_entries_checked']} entries (must be 0)")
    print(f"  repo git status : {safety['git_status_lines']} changed path(s)")


def _metric(label: str, value: Any, pct: float | None, suffix: str = "") -> None:
    text = f"{value}" if pct is None else f"{value} ({pct}%{(' ' + suffix) if suffix else ''})"
    print(f"  {label:<44} {text}")


def print_comparison(base: dict[str, Any], cand: dict[str, Any]) -> None:
    base_summary, cand_summary = base["summary"], cand["summary"]
    rows = compare_rows(
        [Row.from_json(r) for r in base["rows"]],
        [Row.from_json(r) for r in cand["rows"]],
    )

    print()
    print("=== BEFORE / AFTER " + "=" * 52)
    print(f"  baseline : {base['meta']['mode']} @ {base['safety']['git_head']}, "
          f"{base['meta']['candidate_count']} candidates, {base['meta']['sample_size']} captures")
    print(f"  candidate: {cand['meta']['mode']} @ {cand['safety']['git_head']}, "
          f"{cand['meta']['candidate_count']} candidates, {cand['meta']['sample_size']} captures")
    if base["meta"]["sample_size"] != cand["meta"]["sample_size"]:
        print("  !! different sample sizes — the shared-capture set is what the deltas use")
    print()
    header = f"  {'metric':<44}{'before':>12}{'after':>12}{'delta':>12}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for label, key, pct_key in _COMPARISON_ROWS:
        before, after = base_summary.get(key), cand_summary.get(key)
        _comparison_line(label, before, after)
        if pct_key:
            _comparison_line("  …as % ", base_summary.get(pct_key), cand_summary.get(pct_key))
    print()
    print(f"  {'rank-1 unchanged vs baseline':<44}"
          f"{rows['rank1_unchanged']}/{rows['had_rank1_before']} "
          f"({rows['rank1_unchanged_pct']}%)")
    print(f"  {'previous rank-1 still present (§5.3 gate)':<44}"
          f"{rows['previous_rank1_survived']}/{rows['had_rank1_before']} "
          f"({rows['previous_rank1_survived_pct']}%)")
    print(f"  {'previous rank-1 VANISHED (must be 0)':<44}{rows['previous_rank1_vanished']}")
    if rows["previous_rank1_vanished_examples"]:
        for rel in rows["previous_rank1_vanished_examples"][:5]:
            print(f"     - {rel}")
    print(f"  {'newly covered captures':<44}{rows['newly_covered']}")
    print(f"  {'captures that LOST coverage (must be 0)':<44}{rows['lost_coverage']}")
    print()
    # The gate is read off the SCORED number on purpose: a config route is a
    # destination Matt already wrote down, so route rows must not be able to
    # buy the §6.4 coverage criterion.
    _gate("scored coverage >= 70% of the backlog",
          cand_summary["with_scored_suggestions_pct"] >= 70.0,
          f"{cand_summary['with_scored_suggestions_pct']}%")
    _gate("no suggestion with an empty reason list",
          cand_summary["suggestions_with_empty_reasons"] == 0,
          str(cand_summary["suggestions_with_empty_reasons"]))
    _gate("no rank-1 destination over 5% of covered",
          cand_summary["top_rank1_destination_pct"] <= 5.0,
          f"{cand_summary['top_rank1_destination_pct']}%")
    _gate("no identical list shared by > 25 captures",
          cand_summary["largest_identical_list"] <= 25,
          str(cand_summary["largest_identical_list"]))
    _gate("rank-1 solely stopword-driven == 0",
          cand_summary["rank1_solely_stopword"] == 0,
          str(cand_summary["rank1_solely_stopword"]))
    _gate("previous rank-1 never vanishes",
          rows["previous_rank1_vanished"] == 0, str(rows["previous_rank1_vanished"]))
    _gate("ranker p95 <= 5 ms (warm)",
          cand_summary["ranker_ms_p95"] <= 5.0, f"{cand_summary['ranker_ms_p95']} ms")
    _gate("CLI parity holds",
          cand["cli_parity"]["checked"] > 0
          and cand["cli_parity"]["matched"] == cand["cli_parity"]["checked"],
          f"{cand['cli_parity']['matched']}/{cand['cli_parity']['checked']}")


_COMPARISON_ROWS: tuple[tuple[str, str, str | None], ...] = (
    ("candidates on the ballot", "candidate_count", None),
    ("captures with >=1 real suggestion", "with_suggestions", "with_suggestions_pct"),
    ("captures with ZERO real suggestions", "zero_suggestions", "zero_suggestions_pct"),
    ("captures with >=1 SCORED suggestion", "with_scored_suggestions", "with_scored_suggestions_pct"),
    ("captures with ZERO scored suggestions", "zero_scored_suggestions", "zero_scored_suggestions_pct"),
    ("rank-1 is a config route", "rank1_is_route", None),
    ("top suggestion has >=1 reason", "top_has_reason", "top_has_reason_pct_of_covered"),
    ("suggestions with empty reasons", "suggestions_with_empty_reasons", None),
    ("distinct rank-1 destinations", "distinct_rank1", None),
    ("biggest rank-1 destination share", "top_rank1_destination_n", "top_rank1_destination_pct"),
    ("rank-1 entropy (bits)", "rank1_entropy_bits", None),
    ("largest identical-list cluster", "largest_identical_list", None),
    ("rank-1 solely stopword-driven", "rank1_solely_stopword", None),
    ("rank-1 is a note", "rank1_note", None),
    ("rank-1 is a folder", "rank1_folder", None),
    ("median list length", "median_list_length", None),
    ("p95 list length", "p95_list_length", None),
    ("median top score", "median_top_score", None),
    ("suppressed duplicate note rows", "suppressed_duplicates_total", None),
    ("ranker ms p50", "ranker_ms_p50", None),
    ("ranker ms p95", "ranker_ms_p95", None),
)


def _comparison_line(label: str, before: Any, after: Any) -> None:
    if before is None and after is None:
        return
    delta = ""
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        diff = round(after - before, 4)
        delta = f"{diff:+g}"
    print(f"  {label:<44}{str(before):>12}{str(after):>12}{delta:>12}")


def _gate(label: str, ok: bool, value: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:<48} {value}")


# --------------------------------------------------------------------------
# the 30-capture human read (§5.5)
# --------------------------------------------------------------------------


def human_sample_markdown(
    base: dict[str, Any] | None, cand: dict[str, Any], count: int, seed: int
) -> str:
    """Stratified sample rendered as a markdown table: tokens -> before ->
    after.  §5.5 exists because SQ-1 passed every aggregate it had."""
    cand_rows = {row["rel"]: row for row in cand["rows"]}
    base_rows = {row["rel"]: row for row in (base or {}).get("rows", ())}

    strata: dict[str, list[str]] = {"newly-covered": [], "changed-rank1": [], "unchanged": [], "still-empty": []}
    for rel, row in cand_rows.items():
        before = base_rows.get(rel)
        if before is None:
            strata["unchanged" if row["paths"] else "still-empty"].append(rel)
        elif not before["paths"] and row["paths"]:
            strata["newly-covered"].append(rel)
        elif not row["paths"]:
            strata["still-empty"].append(rel)
        elif before["paths"][:1] != row["paths"][:1]:
            strata["changed-rank1"].append(rel)
        else:
            strata["unchanged"].append(rel)

    rng = random.Random(seed)
    per = max(1, count // max(1, len([k for k, v in strata.items() if v])))
    chosen: list[tuple[str, str]] = []
    for name, members in strata.items():
        if not members:
            continue
        for rel in rng.sample(sorted(members), min(per, len(members))):
            chosen.append((name, rel))
    chosen = chosen[:count]

    out = [
        f"# Destination-recall — {len(chosen)}-capture human read (spec 21 §5.5)",
        "",
        f"seed={seed}; strata={{" + ", ".join(f"{k}: {len(v)}" for k, v in strata.items()) + "}",
        "",
        "| stratum | capture | tags+sources | before (rank 1..3) | after (rank 1..3) |",
        "|---|---|---|---|---|",
    ]
    for stratum, rel in chosen:
        row = cand_rows[rel]
        before = base_rows.get(rel, {"paths": [], "scores": []})
        out.append(
            f"| {stratum} | `{rel}` | {', '.join(row['tokens'][:6]) or '—'} "
            f"| {_render_top(before)} | {_render_top(row)} |"
        )
    return "\n".join(out) + "\n"


def _render_top(row: dict[str, Any]) -> str:
    paths = row.get("paths", [])
    scores = row.get("scores", [])
    if not paths:
        return "_archive only_"
    return "<br>".join(f"{s:.2f} `{p}`" for p, s in list(zip(paths, scores, strict=False))[:3])


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recall_sweep",
        description="spec 21 §5 destination-recall before/after evidence",
    )
    parser.add_argument("--mode", choices=("baseline", "candidate"), default="baseline")
    parser.add_argument(
        "--work",
        default=str(Path.home() / ".cache" / "organize-recall-sweep"),
        help="disposable working tree (vault/ cfg/ state/ runtime/)",
    )
    parser.add_argument("--mirror", default=str(DEFAULT_MIRROR), help="read-only golden mirror")
    parser.add_argument("--prepare", action="store_true", help="force rsync + reindex")
    parser.add_argument("--no-prepare", action="store_true", help="refuse to build the work tree")
    parser.add_argument("--backlog", choices=("all", "session"), default="all")
    parser.add_argument("--limit", type=int, default=0, help="sample N captures (0 = all)")
    parser.add_argument("--seed", type=int, default=20260816, help="sampling seed (printed+recorded)")
    parser.add_argument("--now", type=float, default=None, help="fixed clock for learning decay")
    parser.add_argument("--learning", type=Path, default=None, help="learning.json to score against")
    parser.add_argument("--cli-parity", type=int, default=12, help="captures cross-checked via the real CLI")
    parser.add_argument("--out", type=Path, default=None, help="result JSON (default <work>/sweep-<mode>.json)")
    parser.add_argument("--baseline", type=Path, default=None, help="baseline JSON to compare against")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), default=None)
    parser.add_argument("--human-sample", type=int, default=0, help="write an N-capture markdown read")
    parser.add_argument("--human-sample-out", type=Path, default=None)
    parser.add_argument("--no-mode-check", action="store_true", help="record a mode the code contradicts")
    parser.add_argument(
        "--repo",
        type=Path,
        default=ROOT,
        help="git worktree the run's rev/status is reported from (default: this file's repo; "
        "point it at the real checkout when measuring a `git archive HEAD` extraction)",
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.compare:
            base = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
            cand = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
            print_report(cand)
            print_comparison(base, cand)
            if args.human_sample:
                _write_human(args, base, cand)
            return 0

        result = run_sweep(args)
        out = args.out or (Path(args.work).expanduser().resolve() / f"sweep-{args.mode}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
        print_report(result)
        print(f"  written        : {out}")

        base = None
        if args.baseline:
            base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            if base["meta"]["now"] != result["meta"]["now"]:
                print("  !! baseline used a different --now; learned-association decay may differ")
            print_comparison(base, result)
        if args.human_sample:
            _write_human(args, base, result)
        return 0
    except SweepError as error:  # one attributable line, never a traceback
        print(f"recall_sweep: {error}", file=sys.stderr)
        return 2


def _write_human(args: argparse.Namespace, base: dict[str, Any] | None, cand: dict[str, Any]) -> None:
    text = human_sample_markdown(base, cand, args.human_sample, args.seed)
    target = args.human_sample_out or (
        Path(args.work).expanduser().resolve() / f"human-read-{args.human_sample}.md"
    )
    target.write_text(text, encoding="utf-8")
    print(f"  human read     : {target}")


if __name__ == "__main__":
    raise SystemExit(main())
