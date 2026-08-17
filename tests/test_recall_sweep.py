"""Tests for the spec 21 §5 measurement harness (``tools/recall_sweep.py``).

The harness is evidence-producing code: a silent defect in it does not crash,
it produces a *number*, and a wrong number is how SQ-1 shipped.  So its
refusal predicates and its metric arithmetic are pinned here to the same
anti-vacuity standard as the core (ARCHITECTURE.md):

- every refusal is asserted at a parameter where the OTHER branch would fire
  (a firing control), so deleting the guard turns a test red rather than
  merely un-exercised;
- every metric assertion is against a LITERAL, hand-counted number, never a
  re-derivation of the implementation's own expression;
- ``--limit`` is pinned to *announce* the sample and the seed, because a
  silently truncated sweep is the house's named defect (no silent caps).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tools" / "recall_sweep.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("recall_sweep_under_test", HARNESS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sweep = _load_harness()


# --------------------------------------------------------------------------
# refusal: the live vault and the golden mirror
# --------------------------------------------------------------------------


def test_refuses_a_work_vault_inside_the_live_vault(tmp_path):
    live = Path.home() / "Obsidian" / "Main"
    with pytest.raises(sweep.SweepError) as excinfo:
        sweep.refuse_live_vault(live / "capture" / "scratch", tmp_path / "mirror")
    assert "live vault" in str(excinfo.value)


def test_refuses_the_live_vault_root_itself(tmp_path):
    with pytest.raises(sweep.SweepError):
        sweep.refuse_live_vault(Path.home() / "notes", tmp_path / "mirror")


def test_refuses_the_read_only_golden_mirror_itself(tmp_path):
    """The mirror is refused because indexing it in place would make it
    writable-in-practice and it would stop being a golden."""
    mirror = tmp_path / "vault-mirror-golden"
    mirror.mkdir()
    with pytest.raises(sweep.SweepError) as excinfo:
        sweep.refuse_live_vault(mirror, mirror)
    assert "golden mirror" in str(excinfo.value)


def test_a_disposable_copy_is_allowed(tmp_path):
    """The FIRING CONTROL for the three refusals above: with the guard in
    place a normal disposable work tree must still pass, so a test suite
    that merely asserted 'raises' could not be satisfied by a guard that
    refuses everything."""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    work = tmp_path / "work" / "vault"
    work.mkdir(parents=True)
    sweep.refuse_live_vault(work, mirror)  # must not raise


# --------------------------------------------------------------------------
# refusal: a mode the imported code contradicts
# --------------------------------------------------------------------------


_ALL_OFF = {"index.candidate_folders": False, "Candidate.kind": False}
_SOME_ON = {"index.candidate_folders": True, "Candidate.kind": False}


def test_baseline_from_widened_code_is_refused():
    with pytest.raises(sweep.SweepError) as excinfo:
        sweep.refuse_mode_mismatch("baseline", _SOME_ON)
    assert "index.candidate_folders" in str(excinfo.value)


def test_candidate_from_unwidened_code_is_refused():
    with pytest.raises(sweep.SweepError):
        sweep.refuse_mode_mismatch("candidate", _ALL_OFF)


def test_each_mode_is_accepted_against_its_own_code():
    """Firing control for both directions of the mode guard."""
    sweep.refuse_mode_mismatch("baseline", _ALL_OFF)
    sweep.refuse_mode_mismatch("candidate", _SOME_ON)


def test_capabilities_probe_names_the_spec_21_surfaces():
    """The probe keys are the §-clause obligations, asserted as LITERALS:
    a rename that quietly dropped one would make ``widened`` False and a
    candidate run would be refused — or worse, mislabelled."""
    caps = sweep.code_capabilities()
    assert set(caps) == {
        "index.candidate_folders",
        "Candidate.kind",
        "Suggestion.destination_kind",
        "config.max_candidate_depth",
        "config.note_candidates",
        "config.max_note_suggestions",
        "config.candidate_stopwords",
    }
    assert all(isinstance(value, bool) for value in caps.values())


# --------------------------------------------------------------------------
# metrics — literal, hand-counted
# --------------------------------------------------------------------------


def _row(rel, paths=(), scores=(), kinds=None, reasons=(), routes=None, ms=1.0):
    paths = list(paths)
    return sweep.Row(
        rel=rel,
        paths=paths,
        scores=list(scores) or [1.0] * len(paths),
        kinds=list(kinds) if kinds is not None else ["folder"] * len(paths),
        types=["area"] * len(paths),
        routes=list(routes) if routes is not None else [False] * len(paths),
        reason_counts=[1] * len(paths),
        top_reasons=list(reasons),
        tokens=[],
        ms=ms,
    )


def test_zero_suggestion_share_counts_only_the_archive_only_captures():
    rows = [
        _row("a.md", paths=["areas/x"]),
        _row("b.md"),
        _row("c.md"),
        _row("d.md"),
    ]
    summary = sweep.summarize(rows, list(sweep.SPEC_STOPWORDS))
    assert summary["captures"] == 4
    assert summary["with_suggestions"] == 1
    assert summary["zero_suggestions"] == 3
    assert summary["zero_suggestions_pct"] == 75.0


def test_largest_identical_list_cluster_is_order_sensitive():
    """SQ-1's signature was 1,450 captures sharing ONE list.  Two captures
    holding the same paths in a different order are two lists, not one — a
    set-based implementation would report 4 here."""
    rows = [
        _row("a.md", paths=["p/1", "p/2"]),
        _row("b.md", paths=["p/1", "p/2"]),
        _row("c.md", paths=["p/2", "p/1"]),
        _row("d.md", paths=["p/2", "p/1"]),
    ]
    summary = sweep.summarize(rows, list(sweep.SPEC_STOPWORDS))
    assert summary["largest_identical_list"] == 2
    assert summary["distinct_rank1"] == 2


def test_stopword_metric_needs_every_reason_to_be_generic():
    """`mind` alone counts; `mind` plus a real token does not — otherwise
    the stopword gate could be declared met by a capture that had a genuine
    reason all along."""
    rows = [
        _row("only-generic.md", paths=["areas/mind"], reasons=["Source 'mind' matches"]),
        _row(
            "mixed.md",
            paths=["areas/eduardo"],
            reasons=["Source 'mind' matches", "Tag 'eduardo' matches folder"],
        ),
        _row(
            "contextual.md",
            paths=["areas/other"],
            reasons=["Context matches folder"],
        ),
    ]
    summary = sweep.summarize(rows, list(sweep.SPEC_STOPWORDS))
    assert summary["rank1_solely_stopword"] == 1


def test_route_rows_are_reported_separately_from_scored_recall():
    """A config route is a destination Matt already wrote down; counting one
    as recall would let the coverage gate be met without widening anything."""
    rows = [
        _row("routed.md", paths=["areas/health/training-log.md"], routes=[True]),
        _row("scored.md", paths=["areas/x"], routes=[False]),
        _row("both.md", paths=["areas/health/training-log.md", "areas/x"], routes=[True, False]),
    ]
    summary = sweep.summarize(rows, list(sweep.SPEC_STOPWORDS))
    assert summary["with_suggestions"] == 3
    assert summary["with_scored_suggestions"] == 2
    assert summary["rank1_is_route"] == 2
    assert summary["covered_by_routes_only"] == 1
    # The §1.2 headline denominator: route-only coverage still counts as
    # zero RANKER recall, which is what spec 21's 1,877 / 76.8% measures.
    assert summary["zero_scored_suggestions"] == 1
    assert summary["zero_suggestions"] == 0


def test_empty_reason_list_is_counted_over_every_suggestion_not_just_rank_1():
    """The 2026-08-16 invariant is "no suggestion anywhere has an empty
    reason list" — a rank-1-only check passed for SQ-1's 9-row lists."""
    row = _row("a.md", paths=["p/1", "p/2", "p/3"])
    row.reason_counts = [2, 0, 0]
    summary = sweep.summarize([row], list(sweep.SPEC_STOPWORDS))
    assert summary["suggestions_with_empty_reasons"] == 2
    assert summary["top_has_reason"] == 1


def test_quantiles_are_nearest_rank_over_a_sorted_list():
    assert sweep._quantile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert sweep._quantile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert sweep._quantile([], 0.95) == 0.0


def test_entropy_of_a_single_destination_is_zero_and_of_four_is_two_bits():
    from collections import Counter

    assert sweep._entropy(Counter({"a": 9})) == 0.0
    assert sweep._entropy(Counter({"a": 1, "b": 1, "c": 1, "d": 1})) == 2.0


# --------------------------------------------------------------------------
# before/after comparison
# --------------------------------------------------------------------------


def test_previous_rank1_survival_and_vanishing_are_counted_separately():
    """§5.3's gate is that a previous rank 1 may be OUTRANKED but may not
    DISAPPEAR — the two are different numbers and are asserted as such."""
    base = [
        _row("outranked.md", paths=["areas/old"]),
        _row("vanished.md", paths=["areas/gone"]),
        _row("empty.md"),
    ]
    cand = [
        _row("outranked.md", paths=["areas/new", "areas/old"]),
        _row("vanished.md", paths=["areas/somewhere-else"]),
        _row("empty.md", paths=["areas/fresh"]),
    ]
    result = sweep.compare_rows(base, cand)
    assert result["had_rank1_before"] == 2
    assert result["rank1_unchanged"] == 0
    assert result["previous_rank1_survived"] == 1
    assert result["previous_rank1_vanished"] == 1
    assert result["previous_rank1_vanished_examples"] == ["vanished.md"]
    assert result["newly_covered"] == 1
    assert result["lost_coverage"] == 0


def test_comparison_reports_captures_missing_from_either_side():
    """A capture only one run scored is named, never averaged away."""
    result = sweep.compare_rows([_row("a.md", paths=["x"])], [_row("b.md", paths=["x"])])
    assert result["shared_captures"] == 0
    assert result["only_in_baseline"] == ["a.md"]
    assert result["only_in_candidate"] == ["b.md"]


def test_lost_coverage_is_its_own_number():
    result = sweep.compare_rows([_row("a.md", paths=["x"])], [_row("a.md")])
    assert result["lost_coverage"] == 1
    assert result["previous_rank1_vanished"] == 1


# --------------------------------------------------------------------------
# sampling honesty (house rule: no silent caps)
# --------------------------------------------------------------------------


def test_limit_and_seed_are_flags_with_a_recorded_default():
    parser = sweep.build_parser()
    args = parser.parse_args([])
    assert args.limit == 0, "the default must be the WHOLE backlog"
    assert isinstance(args.seed, int)
    sampled = parser.parse_args(["--limit", "30", "--seed", "7"])
    assert (sampled.limit, sampled.seed) == (30, 7)


def test_row_json_round_trips_every_field_the_metrics_read():
    """The before/after table is computed from a saved JSON, so a field the
    writer drops silently becomes a zero in the comparison."""
    row = _row("a.md", paths=["p/1"], scores=[1.5], kinds=["note"], reasons=["Tag 'x' matches folder"])
    restored = sweep.Row.from_json(row.as_json())
    assert restored.paths == ["p/1"]
    assert restored.scores == [1.5]
    assert restored.kinds == ["note"]
    assert restored.types == ["area"]
    assert restored.routes == [False]
    assert restored.reason_counts == [1]
    assert restored.top_reasons == ["Tag 'x' matches folder"]


def test_reason_token_pattern_reads_the_capture_side_token_only():
    """Pinned against the LITERAL reason strings ``calculate_score`` emits.
    ``Tag 'x' ~ folder 'y'`` must yield ``x`` (the capture token), never
    ``y`` (the destination) — reading the destination would make every
    match against a folder named `mind` look stopword-driven."""
    assert sweep._REASON_TOKEN.match("Tag 'growth' matches folder").group(1) == "growth"
    assert sweep._REASON_TOKEN.match("Tag 'growth' (normalized) matches").group(1) == "growth"
    assert sweep._REASON_TOKEN.match("Tag 'growth' ~ folder 'growth-system'").group(1) == "growth"
    assert sweep._REASON_TOKEN.match("Source 'mind' matches").group(1) == "mind"
    assert sweep._REASON_TOKEN.match("Alias 'eduardo' similar").group(1) == "eduardo"
    assert sweep._REASON_TOKEN.match("Context matches folder") is None
    assert sweep._REASON_TOKEN.match("Previously used destination") is None


def test_spec_stopword_default_is_the_literal_list_from_spec_21():
    assert sweep.SPEC_STOPWORDS == ("mind", "self", "me", "text", "voice", "note", "thought")


class _Vault:
    para_folders = {"areas": "areas"}


class _Config:
    vault = _Vault()


class _Index:
    def para_subfolders(self, key):
        return [Path("/vault/areas/x")]


def _hide_newer_doors(monkeypatch):
    """Strip the post-change ballot doors so the pre-change resolution order
    can be exercised deterministically, in either checkout."""
    from organize_core import cli as cli_mod
    from organize_core import suggest as suggest_mod

    monkeypatch.delattr(cli_mod, "_candidates", raising=False)
    monkeypatch.delattr(cli_mod, "_candidate_folders", raising=False)
    monkeypatch.delattr(suggest_mod, "build_candidate_set", raising=False)


def test_unknown_generate_candidates_signature_refuses_rather_than_guessing(monkeypatch):
    """The builder resolver must never fall back to the depth-1 mapping for
    a shape it does not recognize: that would report the BEFORE ballot as
    the AFTER number."""
    from organize_core import suggest as suggest_mod

    _hide_newer_doors(monkeypatch)

    def three_positional(a, b, c):  # an unrecognized shape
        return []

    monkeypatch.setattr(suggest_mod, "generate_candidates", three_positional)
    with pytest.raises(sweep.SweepError) as excinfo:
        sweep.build_candidates(_Index(), _Config())
    assert "generate_candidates" in str(excinfo.value)


def test_the_known_one_positional_shape_is_still_accepted(monkeypatch):
    """Firing control for the refusal above: today's single-mapping builder
    must resolve, and the resolver must NAME the door it came through so a
    saved run can never be read without knowing which ballot produced it."""
    from organize_core import suggest as suggest_mod

    _hide_newer_doors(monkeypatch)
    monkeypatch.setattr(suggest_mod, "generate_candidates", lambda mapping: ["c"])

    name, candidates = sweep.build_candidates(_Index(), _Config())
    assert name == "generate_candidates(para_subfolders)"
    assert candidates == ["c"]


def test_the_shipped_cli_ballot_wins_over_every_reconstruction(monkeypatch):
    """When the core exposes the door `organize suggest` itself uses, the
    harness must walk through THAT one — a reconstruction would measure the
    harness's idea of the change instead of the shipped ballot."""
    from organize_core import cli as cli_mod
    from organize_core import suggest as suggest_mod

    monkeypatch.setattr(cli_mod, "_candidates", lambda index, config: "SHIPPED", raising=False)
    monkeypatch.setattr(
        suggest_mod, "build_candidate_set", lambda *a, **k: "REBUILT", raising=False
    )
    name, candidates = sweep.build_candidates(_Index(), _Config())
    assert name == "cli._candidates(index, config)"
    assert candidates == "SHIPPED"


def test_note_candidates_false_hands_the_builder_an_empty_note_ballot(monkeypatch):
    """§3.6's off switch has to be honored by the harness too, or the
    "reproduces the folders-only run byte for byte" claim is untested."""

    class _Suggestions:
        note_candidates = False

    class _ConfigOff(_Config):
        suggestions = _Suggestions()

    class _IndexWithNotes(_Index):
        def candidate_notes(self):  # pragma: no cover - must NOT be called
            raise AssertionError("candidate_notes() called with note_candidates = false")

    assert sweep._candidate_notes(_IndexWithNotes(), _ConfigOff()) == []
