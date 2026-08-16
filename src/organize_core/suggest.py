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

The reference implementation (`d753672~1`) carried an eighth, unspecced
"fallback: filename similarity" signal.  Spec 04 §2 fixes the signal list at
seven and forbids adding signals, so it is NOT reproduced here.

Signal 7 contributes no reason string (parity): it fires for every candidate,
so a reason would be pure noise in the UI. For the same reason it is
SUBTRACTED before ``min_confidence`` is applied — see :func:`suggest`: a
signal that fires unconditionally must not be what decides which candidates
survive the floor.

Also exposed for ARBITRARY TEXT (spec 13 §3 cross-check): auto-organize
scores ad-hoc selections, not only indexed capture files.
"""

from __future__ import annotations

from dataclasses import dataclass

from organize_core import frontmatter
from organize_core.config import SuggestionsConfig
from organize_core.index import PARA_KEY_TO_TYPE, NoteRecord
from organize_core.learn import LearningData, get_association_score

ARCHIVE_SUGGESTION_NAME = "Archive Now"
ARCHIVE_SUGGESTION_SCORE = 0.1
ARCHIVE_SUGGESTION_REASON = "Safe default option"

#: ``Suggestion.type`` of the synthetic archive entry. Defined here, beside
#: the rest of that entry, and re-exported by ``routes`` (which cannot be
#: imported from here — routes imports suggest); one definition, so the
#: value cannot drift between the module that emits it and the one that
#: recognizes it.
ARCHIVE_SUGGESTION_TYPE = "archive"

# Spec 04 §2 #5: aliases that are really the capture id are not names.
_CAPTURE_ALIAS_PREFIX = "capture_"
_ALIAS_SIMILARITY_GATE = 0.6

# Spec 04 §1: archives are never a scored candidate.
_EXCLUDED_CANDIDATE_TYPES = frozenset({"archives", "archive"})


@dataclass(frozen=True)
class Candidate:
    """One destination candidate: an immediate subfolder of a PARA root,
    archives excluded (spec 04 §1). ``normalized_name`` via the shared
    ``frontmatter.normalize_tag`` normalizer."""

    path: str
    name: str
    normalized_name: str
    #: The ``vault.para_folders`` KEY this candidate came from, so PLURAL
    #: ("projects" | "areas" | "resources") — `generate_candidates` is fed
    #: that dict and `_type_bonus` is keyed by it. It is converted to the
    #: singular wire vocabulary when a `Suggestion` is built from it.
    type: str


@dataclass(frozen=True)
class Suggestion:
    """One ranked destination shown in the UI / returned by the API.
    ``route`` is set when a doc-11 route made this the top suggestion —
    routes outrank scored suggestions and render distinctly (11 §1)."""

    path: str
    name: str
    #: SINGULAR ParaType — "project" | "area" | "resource" | "archive" (and
    #: "other" for a route destination outside any PARA root). Every type
    #: VALUE on the wire is singular; plural names a `para_folders` KEY only.
    type: str
    score: float
    reasons: tuple[str, ...] = ()
    route: str | None = None  # route name, when route-sourced
    description: str | None = None  # NL description when known (11 §3)


@dataclass(frozen=True)
class CaptureFeaturesView:
    """The capture attributes scoring reads — constructed either from a
    NoteRecord (normal path) or from arbitrary text + optional tags
    (spec 13 §3). Keeping this explicit stops scoring from ever reaching
    back into the index (purity).

    ``modalities`` is carried even though it is NOT a signal (04 §2's
    exclusion list stands): ``learn.create_association_key`` includes a
    ``modalities:`` part, so a view that dropped it would compute a
    different association key than ``record_move`` stored and the learned
    signal would silently never fire for captures that have modalities.
    """

    tags: tuple[str, ...] = ()
    normalized_tags: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ()  # association-key parity only, never a signal
    aliases: tuple[str, ...] = ()
    capture_id: str | None = None
    context: tuple[str, ...] = ()

    @classmethod
    def from_record(cls, record: NoteRecord) -> CaptureFeaturesView:
        return cls(
            tags=tuple(str(tag) for tag in record.tags or ()),
            normalized_tags=tuple(str(tag) for tag in record.normalized_tags or ()),
            sources=tuple(str(source) for source in record.sources or ()),
            modalities=tuple(str(modality) for modality in record.modalities or ()),
            aliases=tuple(str(alias) for alias in record.aliases or ()),
            capture_id=record.capture_id,
            context=tuple(str(item) for item in record.context or ()),
        )

    @classmethod
    def from_text(cls, text: str, tags: list[str] | None = None) -> CaptureFeaturesView:
        """Ad-hoc text view (13 §3). Signal inputs are frontmatter-shaped,
        so an untagged selection scores on context/type-bonus only unless
        the caller supplies tags (e.g. from the auto-tagger).

        The text itself becomes the ``context`` — signal 6 is the only one
        that reads free prose, and inventing new text signals is forbidden
        by 04 §2.  No aliases, no sources, no modalities: arbitrary text has
        none, and fabricating them would fabricate score.
        """
        raw_tags = tuple(str(tag) for tag in (tags or ()))
        context = (text,) if text and text.strip() else ()
        return cls(
            tags=raw_tags,
            normalized_tags=tuple(frontmatter.normalize_tag(tag) for tag in raw_tags),
            sources=(),
            modalities=(),
            aliases=(),
            capture_id=None,
            context=context,
        )


def string_similarity(a: str, b: str) -> float:
    """Normalized Levenshtein similarity ``1 - dist/max_len`` ∈ [0, 1]
    (spec 04 §2 #5 — regression-tested with exact numeric cases; the
    original returned raw distance, 08 §A7). Empty-vs-empty ⇒ 1.0."""
    if a == b:
        return 1.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1.0
    distance = _levenshtein(a, b)
    similarity = 1.0 - (distance / max_len)
    # Distance is bounded by max_len, so this is already in [0, 1]; clamp
    # anyway so the alias signal can never exceed its weight (04 §7).
    return min(1.0, max(0.0, similarity))


def _levenshtein(a: str, b: str) -> int:
    """Classic two-row edit distance (stdlib only, O(len(a) * len(b)))."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            substitute = previous[j - 1] + (char_a != char_b)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]


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
    weights = config.weights
    score = 0.0
    reasons: list[str] = []

    # 1. exact tag match — 2.0 PER MATCHING TAG (spec 04 §2 #1)
    for tag in capture.tags:
        if tag == candidate.name or tag == candidate.normalized_name:
            score += weights.exact_tag_match
            reasons.append(f"Tag '{tag}' matches folder")

    # 2. normalized tag match — 1.5 per tag (spec 04 §2 #2). The normalizer
    #    (including the tag_normalization map) is applied where the view is
    #    built; scoring is a pure string comparison.
    for tag in capture.normalized_tags:
        if tag == candidate.normalized_name:
            score += weights.normalized_tag_match
            reasons.append(f"Tag '{tag}' (normalized) matches")
            continue
        # …and the same signal absorbs the MORPHOLOGICAL variation the
        # `tag_normalization` map exists for but cannot enumerate: Matt's
        # `<topic>-system` convention and singular/plural drift. On the real
        # backlog `productivity-system` (65 captures) never reached
        # `areas/productivity` and `principle` (39) never reached
        # `areas/principles`; each near-miss dropped the capture into the
        # no-signal fallback. Scored at the NORMALIZED weight, so a genuine
        # exact match still outranks a near-miss — signal #1 fires for it too.
        #
        # Variants are derived HERE, at match time. `frontmatter.normalize_tag`
        # is deliberately NOT touched: it also builds learning association
        # keys, the `<type>/<folder>` tag a move writes, and the frontmatter
        # that lands on disk, so morphing it would corrupt learning and vault
        # data (architect ruling, 2026-08-16).
        if _tag_variant_matches(tag, candidate.normalized_name, config):
            score += weights.normalized_tag_match
            reasons.append(f"Tag '{tag}' ~ folder '{candidate.name}'")

    # 3. learned association — 1.8 × learned score (spec 04 §2 #3 / §4)
    learned = get_association_score(
        learning, capture, candidate.path, config.learning, now=now
    )
    if learned > 0:
        score += learned * weights.learned_association
        reasons.append("Previously used destination")

    # 4. source match — 1.3 per source (spec 04 §2 #4)
    for source in capture.sources:
        if frontmatter.normalize_tag(source) == candidate.normalized_name:
            score += weights.source_match
            reasons.append(f"Source '{source}' matches")

    # 5. alias similarity — 1.1 × similarity when > 0.6 (spec 04 §2 #5)
    for alias in capture.aliases:
        if alias.startswith(_CAPTURE_ALIAS_PREFIX):
            continue
        if capture.capture_id is not None and alias == capture.capture_id:
            continue
        similarity = string_similarity(alias.lower(), candidate.name.lower())
        if similarity > _ALIAS_SIMILARITY_GATE:
            score += similarity * weights.alias_similarity
            reasons.append(f"Alias '{alias}' similar")

    # 6. context match — 1.0, either direction, at TOKEN granularity
    #    (spec 04 §2 #6). See `_contains_token_run`.
    if _context_matches(" ".join(capture.context), candidate.name):
        score += weights.context_match
        reasons.append("Context matches folder")

    # 7. folder-type bonus — always fires, no reason string (spec 04 §2 #7)
    score += _type_bonus(candidate.type, config)

    return score, reasons


def _tag_variant_matches(tag: str, folder: str, config: SuggestionsConfig) -> bool:
    """Does ``tag`` reach ``folder`` after a light morphological pass?

    Two rules, both symmetric, applied to the already-normalized forms:

    1. strip a configured suffix (``suggestions.tag_suffix_strip``, default
       ``-system`` / ``-systems``) from either side;
    2. naive singular/plural — a trailing ``s`` on either side.

    Both may apply together (``principles-system`` → ``principle``). This is
    deliberately conservative: no stemmer, no ``-es``/``-ies`` rules, nothing
    that could singularize an unrelated word into a folder name.
    """
    if not tag or not folder:
        return False
    return bool(_tag_variants(tag, config) & _tag_variants(folder, config))


def _tag_variants(value: str, config: SuggestionsConfig) -> set[str]:
    variants = {value}
    for suffix in config.tag_suffix_strip:
        clean = suffix.strip().lower()
        if clean and value.endswith(clean) and len(value) > len(clean):
            variants.add(value[: -len(clean)])
    for variant in list(variants):
        if variant.endswith("s") and len(variant) > 1:
            variants.add(variant[:-1])
        else:
            variants.add(variant + "s")
    return {v for v in variants if v}


def _tokens(text: str) -> list[str]:
    """Lower-cased alphanumeric runs; everything else separates tokens."""
    out: list[str] = []
    current: list[str] = []
    for char in text.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _contains_token_run(haystack: list[str], needle: list[str]) -> bool:
    """Is ``needle`` a CONTIGUOUS run of tokens inside ``haystack``?"""
    if not needle or len(needle) > len(haystack):
        return False
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return True
    return False


def _context_matches(context_text: str, folder_name: str) -> bool:
    """Spec 04 §2 #6's "case-insensitive substring, either way", anchored on
    TOKEN boundaries.

    A raw substring test made short folder names match inside unrelated
    words: `resources/ui` was the rank-1 suggestion for a capture whose whole
    context was "quitting toastmasters" — the ``ui`` inside ``q-ui-tting``.
    Context is the only signal that reads free prose and almost nothing else
    fires on the real corpus, so those misfires reliably reached rank 1
    (sanctioned deviation from the literal "substring" wording, architect
    ruling 2026-08-16).
    """
    context_tokens = _tokens(context_text)
    folder_tokens = _tokens(folder_name)
    if not context_tokens or not folder_tokens:
        return False
    return _contains_token_run(context_tokens, folder_tokens) or _contains_token_run(
        folder_tokens, context_tokens
    )


def _type_bonus(candidate_type: str, config: SuggestionsConfig) -> float:
    # Keyed by the PLURAL `para_folders` key, which is what `Candidate.type`
    # holds — not the singular wire type.
    bonus = config.weights.type_bonus
    return {
        "projects": bonus.projects,
        "areas": bonus.areas,
        "resources": bonus.resources,
    }.get(candidate_type, 0.0)


def generate_candidates(
    para_subfolders: dict[str, list[str]],
) -> list[Candidate]:
    """Build candidates from ``{para_type_key: [folder paths]}`` (the
    caller feeds ``VaultIndex.para_subfolders`` output — no I/O here).
    Archives excluded (spec 04 §1)."""
    candidates: list[Candidate] = []
    for para_type, paths in para_subfolders.items():
        if para_type in _EXCLUDED_CANDIDATE_TYPES:
            continue
        for path in sorted(paths or ()):
            name = _basename(path)
            if not name:
                continue
            candidates.append(
                Candidate(
                    path=path,
                    name=name,
                    normalized_name=frontmatter.normalize_tag(name),
                    type=para_type,
                )
            )
    return candidates


def _basename(path: str) -> str:
    """Last non-empty path segment, without touching the filesystem."""
    return path.rstrip("/").rpartition("/")[2]


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

    - score every candidate; drop those whose SIGNAL score (the total minus
      the always-firing type bonus) is below ``learning.min_confidence``
      (archive entry exempt) — see below;
    - STABLE sort, score descending, tiebreak name ascending;
    - truncate so the returned list is EXACTLY ≤ ``max_suggestions``
      INCLUDING the synthetic archive entry (off-by-one fixed, 04 §1);
    - when ``always_show_archive`` and ``archive_path``: append
      ``Suggestion(name="Archive Now", type="archive", score=0.1,
      reasons=("Safe default option",), path=archive_path)``.

    Route injection (11 §1) happens ABOVE this layer (routes.merge_route_
    suggestions) so scoring stays route-agnostic and pure.

    THE FLOOR APPLIES TO THE SIGNAL SCORE, NOT THE TOTAL (architect ruling,
    2026-08-16). ``min_confidence`` defaults to 0.3, which is >= the areas
    (0.2) and resources (0.1) type bonuses, so comparing it against the TOTAL
    made the always-firing signal #7 decide survival: on the real vault 83 of
    136 PARA folders could not be returned at ANY ``max_suggestions``, while
    the 53 ``projects/`` folders all came back at exactly 0.30 with an empty
    reason list — an identical 9-row list for 1450 of 1858 backlog captures,
    ordered by an ASCII accident (``projects/B2-polish`` won 1450 times
    because "B" sorts before "a"). Subtracting the type bonus first resolves
    spec 04 §2's self-contradiction ("every folder is technically a candidate
    — ranking does the real work" vs "apply min_confidence as the documented
    floor"): every folder is still SCORED and the bonus still RANKS, but a
    candidate with no evidence behind it is not offered. A capture that fires
    no signal therefore returns the archive entry alone — the honest "no
    confident destination" state — and every non-archive suggestion carries
    at least one reason.
    """
    min_confidence = config.learning.min_confidence

    scored: list[Suggestion] = []
    for candidate in candidates:
        if candidate.type in _EXCLUDED_CANDIDATE_TYPES:
            continue
        score, reasons = calculate_score(capture, candidate, config, learning, now=now)
        signal_score = score - _type_bonus(candidate.type, config)
        if signal_score <= 0.0 or signal_score < min_confidence:
            continue
        scored.append(
            Suggestion(
                path=candidate.path,
                name=candidate.name,
                # `Candidate.type` is the plural config KEY; the wire type is
                # singular. An unrecognized key (a vault with a custom PARA
                # root) passes through rather than being blanked.
                type=PARA_KEY_TO_TYPE.get(candidate.type, candidate.type),
                score=score,
                reasons=tuple(reasons),
            )
        )

    # list.sort is stable; the explicit name/path tiebreak makes the order
    # total, so identical inputs always produce an identical list (04 §2).
    scored.sort(key=lambda item: (-item.score, item.name, item.path))

    limit = max(0, int(config.max_suggestions))
    if limit == 0:
        return []
    include_archive = bool(config.always_show_archive) and archive_path is not None
    keep = limit - 1 if include_archive else limit

    result = scored[:keep]
    if include_archive:
        result.append(
            Suggestion(
                path=archive_path or "",
                name=ARCHIVE_SUGGESTION_NAME,
                type=ARCHIVE_SUGGESTION_TYPE,
                score=ARCHIVE_SUGGESTION_SCORE,
                reasons=(ARCHIVE_SUGGESTION_REASON,),
            )
        )
    return result
