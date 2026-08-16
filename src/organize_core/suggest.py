"""The 7-signal additive suggestion scorer (spec 04 §1-2).

DESIGN CONSTRAINT (spec 09 §3): scoring is PURE FUNCTIONS over data — no
I/O, no clock reads, no globals — so numeric goldens are trivial. Callers
pass the capture record, the candidate list, the learning data, and ``now``.

Signals and EXACT default weights (spec 04 §2 — acceptance tests assert
these numerically):

    1. exact_tag_match        2.0 per matching tag
    2. normalized_tag_match   1.5 per tag
    3. learned_association    1.8 × learned score (learn.get_association_score)
    4. source_match           1.3 per source
    5. alias_similarity       1.1 × similarity, when similarity > 0.6
    6. context_match          1.0 (case-insensitive substring, either way)
    7. type bonus             projects +0.3 / areas +0.2 / resources +0.1
                              (config: suggestions.weights.type_bonus)

Deliberately NOT signals (parity, 04 §2): body text, modalities, location,
folder note-counts, folder recency. Do not add signals.

Also exposed for ARBITRARY TEXT (spec 13 §3 cross-check): auto-organize
scores ad-hoc selections, not only indexed capture files.
"""

from __future__ import annotations

from dataclasses import dataclass

from organize_core.config import SuggestionsConfig
from organize_core.index import NoteRecord
from organize_core.learn import LearningData

ARCHIVE_SUGGESTION_NAME = "Archive Now"


@dataclass(frozen=True)
class Candidate:
    """One destination candidate: an immediate subfolder of a PARA root,
    archives excluded (spec 04 §1). ``normalized_name`` via the shared
    ``frontmatter.normalize_tag`` normalizer."""

    path: str
    name: str
    normalized_name: str
    type: str  # "projects" | "areas" | "resources"


@dataclass(frozen=True)
class Suggestion:
    """One ranked destination shown in the UI / returned by the API.
    ``route`` is set when a doc-11 route made this the top suggestion —
    routes outrank scored suggestions and render distinctly (11 §1)."""

    path: str
    name: str
    type: str  # "projects" | "areas" | "resources" | "archives"
    score: float
    reasons: tuple[str, ...] = ()
    route: str | None = None  # route name, when route-sourced
    description: str | None = None  # NL description when known (11 §3)


@dataclass(frozen=True)
class CaptureFeaturesView:
    """The capture attributes scoring reads — constructed either from a
    NoteRecord (normal path) or from arbitrary text + optional tags
    (spec 13 §3). Keeping this explicit stops scoring from ever reaching
    back into the index (purity)."""

    tags: tuple[str, ...] = ()
    normalized_tags: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    capture_id: str | None = None
    context: tuple[str, ...] = ()

    @classmethod
    def from_record(cls, record: NoteRecord) -> CaptureFeaturesView:
        raise NotImplementedError

    @classmethod
    def from_text(cls, text: str, tags: list[str] | None = None) -> CaptureFeaturesView:
        """Ad-hoc text view (13 §3). Signal inputs are frontmatter-shaped,
        so an untagged selection scores on context/type-bonus only unless
        the caller supplies tags (e.g. from the auto-tagger)."""
        raise NotImplementedError


def string_similarity(a: str, b: str) -> float:
    """Normalized Levenshtein similarity ``1 - dist/max_len`` ∈ [0, 1]
    (spec 04 §2 #5 — regression-tested with exact numeric cases; the
    original returned raw distance, 08 §A7). Empty-vs-empty ⇒ 1.0."""
    raise NotImplementedError


def calculate_score(
    capture: CaptureFeaturesView,
    candidate: Candidate,
    config: SuggestionsConfig,
    learning: LearningData,
    *,
    now: float,
) -> tuple[float, list[str]]:
    """Additive score + human-readable reason strings (one per fired
    signal, e.g. ``Tag 'impro' matches folder``) per spec 04 §2. The
    learned_association term calls ``learn.get_association_score`` (pure,
    NaN-guarded) with ``now``."""
    raise NotImplementedError


def generate_candidates(
    para_subfolders: dict[str, list[str]],
) -> list[Candidate]:
    """Build candidates from ``{para_type_key: [folder paths]}`` (the
    caller feeds ``VaultIndex.para_subfolders`` output — no I/O here).
    Archives excluded (spec 04 §1)."""
    raise NotImplementedError


def suggest(
    capture: CaptureFeaturesView,
    candidates: list[Candidate],
    config: SuggestionsConfig,
    learning: LearningData,
    *,
    now: float,
    archive_path: str | None = None,
) -> list[Suggestion]:
    """Full ranking (spec 04 §1-2):

    - score every candidate; drop those below ``learning.min_confidence``
      (archive entry exempt);
    - STABLE sort, score descending, tiebreak name ascending;
    - truncate so the returned list is EXACTLY ≤ ``max_suggestions``
      INCLUDING the synthetic archive entry (off-by-one fixed, 04 §1);
    - when ``always_show_archive`` and ``archive_path``: append
      ``Suggestion(name="Archive Now", type="archives", score=0.1,
      reasons=("Safe default option",), path=archive_path)``.

    Route injection (11 §1) happens ABOVE this layer (routes.merge_route_
    suggestions) so scoring stays route-agnostic and pure.
    """
    raise NotImplementedError
