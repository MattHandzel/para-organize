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

import logging
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from organize_core import frontmatter
from organize_core.config import SuggestionsConfig
from organize_core.index import (
    CANDIDATE_NOTE_PARA_TYPES,
    EXCLUDED_CANDIDATE_PARA_KEYS,
    PARA_KEY_TO_TYPE,
    PARA_TYPE_TO_KEY,
    NoteRecord,
)
from organize_core.learn import (
    LearningData,
    create_association_key,
    extract_features,
    get_association_score,
)

logger = logging.getLogger(__name__)

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

# Spec 04 §1: archives are never a scored candidate. ONE definition, in
# index.py beside the folder walk that also honors it (21 §2.3).
_EXCLUDED_CANDIDATE_TYPES = EXCLUDED_CANDIDATE_PARA_KEYS

#: ``Candidate.kind`` / ``Suggestion.destination_kind`` values (spec 21 §3.3).
#: A note is a MERGE target (doc 19's flow); a folder is a move destination.
#: The core STATES the kind — no client may infer it from a trailing ".md",
#: because a folder may legally be named ``foo.md``.
KIND_FOLDER = "folder"
KIND_NOTE = "note"

#: Sort rank per kind (spec 21 §3.2): on an exact tie of score AND name the
#: note wins, because a note is the more specific destination and merge is
#: backed up and undoable (17 §1).
_KIND_RANK = {KIND_NOTE: 0, KIND_FOLDER: 1}

#: Values that are timestamps rather than names, and so are never note match
#: keys (spec 21 §3.1 (b)/(c)): a leading ISO date, however the time part is
#: punctuated (`2025-08-19T09:47:51.213957+00:00`, `2025-11-07T18-47-09`).
_TIMESTAMP_SHAPED = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]|$)")


@dataclass(frozen=True)
class Candidate:
    """One destination candidate (spec 04 §1 as amended by 21 §1.4): a folder
    under a non-archive PARA root at depth ≤ ``suggestions.max_candidate_depth``,
    or — when ``suggestions.note_candidates`` — an indexed NOTE under one.
    ``normalized_name`` and every match key come from the shared
    ``frontmatter.normalize_tag`` normalizer, never a second one.

    ``name`` is the BASENAME (a note's stem, without ``.md``), never the
    relative path: signals #1/#2/#4 compare a TAG to it, and a relative path
    could never equal a tag (21 §2.2)."""

    path: str
    name: str
    normalized_name: str
    #: The ``vault.para_folders`` KEY this candidate came from, so PLURAL
    #: ("projects" | "areas" | "resources") — `generate_candidates` is fed
    #: that dict and `_type_bonus` is keyed by it. It is converted to the
    #: singular wire vocabulary when a `Suggestion` is built from it.
    type: str
    #: "folder" | "note" (21 §3.3). Folders keep the historical default so
    #: every existing construction site — and every existing test — means
    #: exactly what it meant before.
    kind: str = KIND_FOLDER
    #: The keys signal #1 (RAW tag) tests membership in. A folder's set is
    #: ``{name, normalized_name}``, which is precisely the old
    #: ``tag == candidate.name or tag == candidate.normalized_name``.
    match_keys: frozenset[str] = field(default_factory=frozenset)
    #: The keys signals #2 and #4 (NORMALIZED token) test membership in. A
    #: folder's set is ``{normalized_name}`` — again exactly the old
    #: comparison, so folder scoring is byte-identical (21 §3.1).
    normalized_keys: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # Derived rather than required, so the folder key sets can never
        # drift from `name`/`normalized_name` and no caller can build a
        # folder candidate whose keys say something else.
        if not self.match_keys:
            object.__setattr__(
                self, "match_keys", frozenset({self.name, self.normalized_name}) - {""}
            )
        if not self.normalized_keys:
            object.__setattr__(self, "normalized_keys", frozenset({self.normalized_name}) - {""})


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
    #: "folder" | "note" (spec 21 §3.3) — ADDITIVE and orthogonal to `type`:
    #: a note under `areas/` is type "area", destination_kind "note".
    #: Selecting a note starts doc 19's MERGE flow instead of a move, and the
    #: client dispatches on THIS, never on a trailing ".md" in the path.
    destination_kind: str = KIND_FOLDER


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
    assert distance is not None  # no cutoff was requested
    similarity = 1.0 - (distance / max_len)
    # Distance is bounded by max_len, so this is already in [0, 1]; clamp
    # anyway so the alias signal can never exceed its weight (04 §7).
    return min(1.0, max(0.0, similarity))


def _max_alias_distance(max_len: int) -> int:
    """The largest edit distance that can still clear signal #5's gate:
    ``1 - dist/max_len > 0.6`` ⇔ ``5·dist < 2·max_len``. Integer arithmetic,
    so no float rounding can move the gate."""
    return (2 * max_len - 1) // 5


def _gated_similarity(a: str, b: str) -> float:
    """:func:`string_similarity`, abandoned as soon as the distance cannot
    come in under signal #5's 0.6 gate.

    EXACT WHERE IT MATTERS: whenever the true similarity is above the gate
    this returns the identical float, so the score is unchanged; below the
    gate it returns 0.0, which the caller treats the same way it treated any
    other sub-gate value. That equivalence is asserted, both directions, over
    a randomized corpus (spec 21 §3.5's "output-preserving" requirement).
    """
    if a == b:
        return 1.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1.0
    max_distance = _max_alias_distance(max_len)
    if abs(len(a) - len(b)) > max_distance:
        return 0.0
    distance = _levenshtein(a, b, max_distance=max_distance)
    if distance is None:
        return 0.0
    return min(1.0, max(0.0, 1.0 - (distance / max_len)))


def _levenshtein(a: str, b: str, *, max_distance: int | None = None) -> int | None:
    """Classic two-row edit distance (stdlib only, O(len(a) * len(b))).

    ``max_distance`` abandons the table as soon as EVERY cell of the current
    row exceeds it — the distance can only grow from row to row, so no
    smaller result can still appear — and answers ``None``. Without a cutoff
    the answer is always the exact distance, so the historic behaviour is
    reached by simply not passing one.
    """
    if len(a) < len(b):
        a, b = b, a
    if not b:
        distance = len(a)
        return None if max_distance is not None and distance > max_distance else distance
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            substitute = previous[j - 1] + (char_a != char_b)
            current.append(min(insert, delete, substitute))
        if max_distance is not None and min(current) > max_distance:
            return None
        previous = current
    if max_distance is not None and previous[-1] > max_distance:
        return None
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
    stopwords = _stopwords(config)
    score = 0.0
    reasons: list[str] = []

    # 1. exact tag match — 2.0 PER MATCHING TAG (spec 04 §2 #1)
    #
    # A signal fires AT MOST ONCE per (capture token, candidate) pair (21
    # §3.1): the test is MEMBERSHIP in the candidate's key set, never an
    # iteration over it. Without that rule a note with two aliases that
    # normalize to its stem would silently outscore an identical note with
    # none. For a folder the key set is {name, normalized_name}, so this is
    # the old `or` chain, unchanged.
    for tag in capture.tags:
        if _is_stopword(tag, stopwords):
            continue
        if tag in candidate.match_keys:
            score += weights.exact_tag_match
            reasons.append(f"Tag '{tag}' matches folder")

    # 2. normalized tag match — 1.5 per tag (spec 04 §2 #2). The normalizer
    #    (including the tag_normalization map) is applied where the view is
    #    built; scoring is a pure string comparison.
    for tag in capture.normalized_tags:
        if _is_stopword(tag, stopwords):
            continue
        if tag in candidate.normalized_keys:
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
        #
        # A note gets the same leniency a folder gets, no more and no less
        # (21 §4 #5): the variants are matched against the candidate's
        # normalized KEY SET, which for a folder is {normalized_name}.
        if _tag_variant_matches_keys(tag, candidate.normalized_keys, config):
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
        if _is_stopword(source, stopwords):
            continue
        if frontmatter.normalize_tag(source) in candidate.normalized_keys:
            score += weights.source_match
            reasons.append(f"Source '{source}' matches")

    # 5. alias similarity — 1.1 × similarity when > 0.6 (spec 04 §2 #5)
    for alias in capture.aliases:
        if alias.startswith(_CAPTURE_ALIAS_PREFIX):
            continue
        if capture.capture_id is not None and alias == capture.capture_id:
            continue
        if _is_stopword(alias, stopwords):
            continue
        # `_gated_similarity` is `string_similarity` with the edit-distance
        # table abandoned once it cannot come in under the gate — identical
        # wherever the gate is cleared, and the difference between a 5 ms and
        # a 600 ms `suggest()` on a capture carrying a 60-character alias.
        similarity = _gated_similarity(alias.lower(), candidate.name.lower())
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


def _stopwords(config: SuggestionsConfig) -> frozenset[str]:
    """``suggestions.candidate_stopwords``, normalized through THE shared
    normalizer so the config may be written in any casing (spec 21 §3.4)."""
    return frozenset(
        frontmatter.normalize_tag(word)
        for word in (config.candidate_stopwords or ())
        if str(word).strip()
    )


def _is_stopword(token: str, stopwords: frozenset[str]) -> bool:
    """Spec 21 §3.4: the filter is on the CAPTURE-SIDE token — a tag, a
    normalized tag, a source, an alias — and it applies UNIFORMLY to both
    candidate kinds. A stopword is a property of the token, not of the
    destination kind; a rule that applied only to notes would make the same
    token mean two things.

    This is a MATCH-TIME filter (the SQ-4 pattern): ``normalize_tag`` is
    CALLED, never modified — it also builds learning association keys and the
    ``<type>/<folder>`` tag a move writes to disk (04 §2, architect ruling
    2026-08-16).
    """
    if not stopwords:
        return False
    return frontmatter.normalize_tag(token) in stopwords


def _tag_variant_matches_keys(
    tag: str, keys: frozenset[str], config: SuggestionsConfig
) -> bool:
    """Signal #2's morphological variants against a candidate KEY SET.
    Fires at most once per (token, candidate) — membership, not iteration."""
    if not tag or not keys:
        return False
    variants = _tag_variants(tag, config)
    return any(variants & _tag_variants(key, config) for key in keys if key)


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


def _is_timestamp_shaped(value: str) -> bool:
    """Spec 21 §3.1 (b)/(c): a capture timestamp is an identifier, not a
    name, and must never become a match key."""
    return bool(_TIMESTAMP_SHAPED.match(value.strip()))


def note_match_keys(record: NoteRecord) -> frozenset[str]:
    """The FOUR match keys of a note candidate (spec 21 §3.1), every one of
    them built by CALLING ``frontmatter.normalize_tag`` — there is no second
    normalizer:

    a. the stem (filename without ``.md``) — the primary key;
    b. each alias, excluding ``capture_``-prefixed ones and the note's own
       ``capture_id`` (the exclusion signal #5 already applies) and excluding
       timestamp-shaped values;
    c. ``id`` when set and not timestamp-shaped — Matt's ``id:`` values are
       what wikilinks resolve against;
    d. the title (04's rule: first ``# heading`` else stem; 08 §B10 stands —
       never ``aliases[0]``), which the index has already computed.

    DELIBERATELY NOT KEYS: the note's body, tags, folder, or ``sources``.
    Matching a capture tag against a note's tags is topical similarity — an
    EIGHTH signal, which 04 §2 forbids. Keys are NAMES only. This is the line
    that keeps "notes are candidates" from becoming "notes are search
    results".
    """
    keys: set[str] = {frontmatter.normalize_tag(_note_stem(record))}
    for alias in record.aliases or ():
        text = str(alias)
        if text.startswith(_CAPTURE_ALIAS_PREFIX):
            continue
        if record.capture_id is not None and text == record.capture_id:
            continue
        if _is_timestamp_shaped(text):
            continue
        keys.add(frontmatter.normalize_tag(text))
    identifier = str(record.id or "")
    if (
        identifier
        and not identifier.startswith(_CAPTURE_ALIAS_PREFIX)
        and identifier != (record.capture_id or None)
        and not _is_timestamp_shaped(identifier)
    ):
        keys.add(frontmatter.normalize_tag(identifier))
    if record.title:
        keys.add(frontmatter.normalize_tag(str(record.title)))
    return frozenset(keys) - {""}


def _note_stem(record: NoteRecord) -> str:
    name = record.filename or _basename(record.path)
    return name[:-3] if name.endswith(".md") else name


def generate_note_candidates(records: Iterable[NoteRecord]) -> list[Candidate]:
    """NOTE destinations (spec 21 §3, the amended 04 §1 (b)).

    Pure over data — the caller passes ``VaultIndex.candidate_notes()``, so
    scoring still does no I/O (04's purity constraint). A capture tagged with
    a person, a project or a book usually belongs IN an existing note, not
    next to it in a folder: on the real backlog 1,157 captures (47.3%) name a
    note and no folder at all.

    ``name`` is the STEM, not the filename: signal #6 tokenizes it and signal
    #5 measures edit distance against it, and a trailing ``.md`` would put a
    literal ``md`` token in every candidate name.
    """
    candidates: list[Candidate] = []
    for record in records:
        if record.para_type not in CANDIDATE_NOTE_PARA_TYPES:
            continue  # captures, archives and `other` are never candidates
        para_key = PARA_TYPE_TO_KEY.get(record.para_type)
        if para_key is None or para_key in _EXCLUDED_CANDIDATE_TYPES:
            continue
        stem = _note_stem(record)
        if not stem:
            continue
        keys = note_match_keys(record)
        if not keys:
            continue
        candidates.append(
            Candidate(
                path=record.path,
                name=stem,
                normalized_name=frontmatter.normalize_tag(stem),
                type=para_key,
                kind=KIND_NOTE,
                # A note's exact-tag and normalized-tag key sets are the SAME
                # four normalized keys: every one of them is already
                # normalized, so there is no raw-vs-normalized distinction to
                # preserve the way there is for a folder basename.
                match_keys=keys,
                normalized_keys=keys,
            )
        )
    return candidates


class CandidateSet:
    """The candidate ballot plus the inverted indexes spec 21 §3.5 requires.

    Built ONCE PER CANDIDATE-SET BUILD and cached with the folder walk
    (§2.4); ``suggest()`` never builds one per capture if it is handed one.
    Measured motivation: a linear scan over 9,638 candidates costs 88.7 ms
    per ``suggest()`` (68× the 135-candidate baseline), which is felt on
    every keystroke of a 1,862-capture backlog.

    Every index here is a PREFILTER, never a re-scoring: it selects the
    candidates that could possibly fire a signal, and the survivors go
    through the unmodified :func:`calculate_score`. A candidate this class
    excludes can only have scored the always-firing type bonus, whose signal
    score is 0 and which the ``signal_score <= 0.0`` floor already drops —
    so the output is IDENTICAL, which is what makes the numeric goldens the
    proof (§3.5, §7.4).
    """

    __slots__ = (
        "candidates",
        "tag_suffix_strip",
        "_by_match_key",
        "_by_normalized_key",
        "_by_variant",
        "_by_first_token",
        "_by_token",
        "_by_name_length",
        "_by_bigram",
        "_by_path",
        "_name_lengths",
        "_name_characters",
    )

    def __init__(self, candidates: Sequence[Candidate], config: SuggestionsConfig) -> None:
        self.candidates: tuple[Candidate, ...] = tuple(candidates)
        # Signal #2's variants are config-driven, so the set records the
        # suffix list it was built with and `suggest()` refuses to trust an
        # index built for a different one.
        self.tag_suffix_strip: tuple[str, ...] = tuple(config.tag_suffix_strip or ())
        self._by_match_key: dict[str, list[int]] = {}
        self._by_normalized_key: dict[str, list[int]] = {}
        self._by_variant: dict[str, list[int]] = {}
        self._by_first_token: dict[str, list[int]] = {}
        self._by_token: dict[str, list[int]] = {}
        self._by_name_length: dict[int, list[int]] = {}
        self._by_bigram: dict[str, list[int]] = {}
        self._by_path: dict[str, list[int]] = {}
        # Per-candidate scalars signal #5's prefilter reads once per pair;
        # recomputing `name.lower()` inside that loop was measurable.
        self._name_lengths: list[int] = []
        self._name_characters: list[Counter[str]] = []
        for position, candidate in enumerate(self.candidates):
            for key in candidate.match_keys:
                self._by_match_key.setdefault(key, []).append(position)
            for key in candidate.normalized_keys:
                self._by_normalized_key.setdefault(key, []).append(position)
                # The variant rule is SYMMETRIC (`principles-system` ~
                # `principle`), so the candidate side has to be expanded too;
                # looking a capture-side variant up in a raw-key index would
                # quietly lose `growth` → `growth-system`.
                for variant in _tag_variants(key, config):
                    self._by_variant.setdefault(variant, []).append(position)
            tokens = _tokens(candidate.name)
            if tokens:
                self._by_first_token.setdefault(tokens[0], []).append(position)
                for token in set(tokens):
                    self._by_token.setdefault(token, []).append(position)
            lowered = candidate.name.lower()
            self._name_lengths.append(len(lowered))
            self._name_characters.append(Counter(lowered))
            self._by_name_length.setdefault(len(lowered), []).append(position)
            for offset in range(len(lowered) - 1):
                # One posting PER OCCURRENCE, so the shared-bigram count is
                # never an under-estimate — see `_alias_shortlist`.
                self._by_bigram.setdefault(lowered[offset : offset + 2], []).append(position)
            self._by_path.setdefault(candidate.path, []).append(position)

    def __len__(self) -> int:
        return len(self.candidates)

    def shortlist(
        self,
        capture: CaptureFeaturesView,
        config: SuggestionsConfig,
        learning: LearningData,
    ) -> list[Candidate]:
        """Every candidate that could score a POSITIVE signal, in ballot
        order. A superset is always safe; this one is exact."""
        stopwords = _stopwords(config)
        picked: set[int] = set()

        # #1 exact tag → the raw tag against the match-key index.
        for tag in capture.tags:
            if _is_stopword(tag, stopwords):
                continue
            picked.update(self._by_match_key.get(tag, ()))

        # #2 normalized tag, plus its variant set derived ONCE from the
        #    capture token (§3.5) instead of per candidate.
        for tag in capture.normalized_tags:
            if _is_stopword(tag, stopwords):
                continue
            picked.update(self._by_normalized_key.get(tag, ()))
            for variant in _tag_variants(tag, config):
                picked.update(self._by_variant.get(variant, ()))

        # #4 source → normalized-key index.
        for source in capture.sources:
            if _is_stopword(source, stopwords):
                continue
            picked.update(self._by_normalized_key.get(frontmatter.normalize_tag(source), ()))

        # #5 alias similarity — the exact prefilters of §3.5.
        for alias in capture.aliases:
            if alias.startswith(_CAPTURE_ALIAS_PREFIX):
                continue
            if capture.capture_id is not None and alias == capture.capture_id:
                continue
            if _is_stopword(alias, stopwords):
                continue
            picked.update(self._alias_shortlist(alias.lower()))

        # #6 context — first-token dictionary prefilter. `_contains_token_run`
        #    needs the needle's FIRST token present in the haystack, in both
        #    directions, so both directions are indexed.
        context_tokens = _tokens(" ".join(capture.context))
        if context_tokens:
            for token in set(context_tokens):
                picked.update(self._by_first_token.get(token, ()))
            picked.update(self._by_token.get(context_tokens[0], ()))

        # #3 learned association — the destinations this capture's own
        #    association and tag/source patterns already name (04 §3-4).
        for path in _learned_destinations(capture, learning):
            picked.update(self._by_path.get(path, ()))

        return [self.candidates[position] for position in sorted(picked)]

    def _alias_shortlist(self, alias: str) -> set[int]:
        """Signal #5's prefilter: every candidate whose name could still clear
        the 0.6 similarity gate against ``alias``. TWO exact bounds, both
        arithmetically incapable of changing a score:

        1. **Length.** Normalized similarity is ``1 - dist/max_len`` and
           ``dist >= |len(a) - len(b)|``, so a length gap of 0.4 × max_len or
           more caps similarity at 0.6 (§3.5).
        2. **Bigrams.** One edit changes at most ``q`` of a string's q-grams,
           so two strings within edit distance ``d`` share at least
           ``max_len - q + 1 - q·d`` of them. The length bound alone left
           7,212 of 8,104 candidates on Matt's vault for a 13-character
           alias — 660 ms in one ``suggest()``; with the bigram bound the same
           alias keeps 207, and the true hits are unchanged.

        Counting is deliberately an OVER-estimate (candidate-side
        multiplicity, uncapped): over-counting can only keep a candidate that
        would have been dropped, never drop one the scorer would have kept.
        """
        lengths = _admissible_name_lengths(len(alias))
        if len(alias) < _BIGRAM_PREFILTER_MIN_LENGTH:
            # At max_len 3 the bound degenerates to "share 0 bigrams", and a
            # single middle substitution ("abc" → "axc", similarity 0.667)
            # shares none while still clearing the gate. Below that length the
            # length bucket alone is the exact answer — and it is tiny.
            picked: set[int] = set()
            for length in lengths:
                picked.update(self._by_name_length.get(length, ()))
            return picked
        # `Counter.update` over a posting LIST counts in C; the same loop
        # written in Python was the single most expensive line of the
        # prefilter on a 60-character alias.
        shared: Counter[int] = Counter()
        for bigram in {alias[offset : offset + 2] for offset in range(len(alias) - 1)}:
            postings = self._by_bigram.get(bigram)
            if postings:
                shared.update(postings)
        alias_characters = Counter(alias)
        keep: set[int] = set()
        for position, count in shared.items():
            name_length = self._name_lengths[position]
            if name_length not in lengths:
                continue
            max_len = max(len(alias), name_length)
            if count < _min_shared_bigrams(max_len):
                continue
            # 3. **Characters.** Every character of the longer string that the
            #    shorter one cannot supply costs at least one edit, so
            #    ``dist >= max_len - |multiset intersection|``. Cheap (one
            #    Counter intersection) next to a 60×60 edit-distance table,
            #    and on the real backlog it is what removes the long
            #    free-text aliases' last few hundred candidates.
            overlap = sum((alias_characters & self._name_characters[position]).values())
            if max_len - overlap > _max_alias_distance(max_len):
                continue
            keep.add(position)
        return keep


#: Shortest alias the bigram bound may be applied to. At ``max_len <= 3`` the
#: bound allows zero shared bigrams, and a pair that shares none can still
#: clear the gate, so below this the length bucket is used unfiltered.
_BIGRAM_PREFILTER_MIN_LENGTH = 4


def _min_shared_bigrams(max_len: int) -> int:
    """How many bigrams two strings MUST share to be within signal #5's gate.

    ``similarity > 0.6`` ⇔ ``5·dist < 2·max_len``, so the largest admissible
    distance is ``(2·max_len - 1) // 5``. One edit destroys at most 2 of the
    longer string's ``max_len - 1`` bigrams, hence the bound. Integer
    arithmetic throughout: a float here could round a real hit away.
    """
    max_distance = (2 * max_len - 1) // 5
    return max(0, max_len - 1 - 2 * max_distance)


def _admissible_name_lengths(alias_length: int) -> range:
    """Candidate-name lengths that can still clear signal #5's 0.6 gate
    against an alias of ``alias_length`` characters.

    ``1 - |a-b|/max(a,b) > 0.6`` ⇔ ``3·max < 5·min``, done in INTEGERS so no
    float rounding can drop a pair the scorer would have counted.
    """
    if alias_length <= 0:
        return range(0, 1)  # only the (empty, empty) pair scores 1.0
    return range(3 * alias_length // 5 + 1, (5 * alias_length - 1) // 3 + 1)


def _learned_destinations(capture: CaptureFeaturesView, learning: LearningData) -> set[str]:
    """Destination paths signal #3 could score above zero for this capture:
    the destinations of its own association key, plus the destinations named
    by its tag/source pattern keys (``tag:<t>->dest:<path>``, 04 §3.4)."""
    paths: set[str] = set()
    association = learning.associations.get(create_association_key(extract_features(capture)))
    if association is not None:
        paths.update(association.destinations)
    if not learning.patterns:
        return paths
    prefixes = {f"tag:{tag}->dest:" for tag in capture.tags}
    prefixes |= {f"source:{source}->dest:" for source in capture.sources}
    if not prefixes:
        return paths
    for key in learning.patterns:
        head, separator, destination = key.partition("->dest:")
        if separator and f"{head}->dest:" in prefixes:
            paths.add(destination)
    return paths


def build_candidate_set(
    para_subfolders: dict[str, list[str]],
    notes: Iterable[NoteRecord] = (),
    config: SuggestionsConfig | None = None,
) -> CandidateSet:
    """The whole ballot, indexed: folders (spec 21 §2) + notes (§3).

    ``notes`` empty reproduces the folders-only ballot exactly, which is what
    ``suggestions.note_candidates = false`` does (§3.6).
    """
    settings = config or SuggestionsConfig()
    candidates = generate_candidates(para_subfolders)
    candidates.extend(generate_note_candidates(notes))
    return CandidateSet(candidates, settings)


@dataclass(frozen=True)
class RankedSuggestions:
    """:func:`rank`'s full result. ``suggest()`` returns only the list, so
    every existing caller is untouched; the count of rows dropped by the
    same-name collapse is reported to ``--json`` (spec 21 §3.2)."""

    suggestions: tuple[Suggestion, ...]
    suppressed_duplicates: int = 0


def suggest(
    capture: CaptureFeaturesView,
    candidates: Sequence[Candidate] | CandidateSet,
    config: SuggestionsConfig,
    learning: LearningData,
    *,
    now: float,
    archive_path: str | None = None,
    exclude_path: str | None = None,
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

    ``exclude_path`` is the note being suggested FOR: a note is never a
    candidate for itself (21 §3.1). It is a pure string comparison on the
    already-resolved path, so scoring still touches no filesystem.
    """
    return list(
        rank(
            capture,
            candidates,
            config,
            learning,
            now=now,
            archive_path=archive_path,
            exclude_path=exclude_path,
        ).suggestions
    )


def rank(
    capture: CaptureFeaturesView,
    candidates: Sequence[Candidate] | CandidateSet,
    config: SuggestionsConfig,
    learning: LearningData,
    *,
    now: float,
    archive_path: str | None = None,
    exclude_path: str | None = None,
) -> RankedSuggestions:
    """:func:`suggest` plus the counters the CLI reports. See that docstring
    for the ranking law; this one documents only what 21 adds:

    - the ballot may be a :class:`CandidateSet` (indexed, cached) or a plain
      list (built here, which is what every existing caller and test does);
    - the sort key gains ``kind_rank`` BETWEEN name and path, so it can only
      reorder candidates that tie on both score and name — for a folders-only
      list the values are equal and the previous path tiebreak still decides,
      byte-identically (21 §3.2);
    - notes that are the same answer written twice collapse (§3.2);
    - ``max_note_suggestions`` caps note rows AFTER ranking, folders filling
      the remainder; ``max_suggestions`` (total, archive included) is
      unchanged (§3.3).
    """
    candidate_set = _as_candidate_set(candidates, config)
    min_confidence = config.learning.min_confidence

    scored: list[tuple[Candidate, float, tuple[str, ...]]] = []
    for candidate in candidate_set.shortlist(capture, config, learning):
        if candidate.type in _EXCLUDED_CANDIDATE_TYPES:
            continue
        if exclude_path is not None and candidate.path == exclude_path:
            continue
        score, reasons = calculate_score(capture, candidate, config, learning, now=now)
        signal_score = score - _type_bonus(candidate.type, config)
        if signal_score <= 0.0 or signal_score < min_confidence:
            continue
        scored.append((candidate, score, tuple(reasons)))

    # list.sort is stable; the explicit name/kind/path tiebreak makes the
    # order total, so identical inputs always produce an identical list
    # (04 §2). kind_rank puts a NOTE above a folder of the same name at the
    # same score: Matt asked for the file, a note is the more specific
    # destination, and merge is backed up and undoable (17 §1).
    scored.sort(
        key=lambda item: (
            -item[1],
            item[0].name,
            _KIND_RANK.get(item[0].kind, len(_KIND_RANK)),
            item[0].path,
        )
    )

    scored, suppressed = _collapse_same_name_notes(scored)
    scored = _cap_note_rows(scored, config)

    limit = max(0, int(config.max_suggestions))
    if limit == 0:
        return RankedSuggestions(suggestions=(), suppressed_duplicates=suppressed)
    include_archive = bool(config.always_show_archive) and archive_path is not None
    keep = limit - 1 if include_archive else limit

    result = [
        Suggestion(
            path=candidate.path,
            name=candidate.name,
            # `Candidate.type` is the plural config KEY; the wire type is
            # singular. An unrecognized key (a vault with a custom PARA
            # root) passes through rather than being blanked.
            type=PARA_KEY_TO_TYPE.get(candidate.type, candidate.type),
            score=score,
            reasons=reasons,
            destination_kind=candidate.kind,
        )
        for candidate, score, reasons in scored[:keep]
    ]
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
    return RankedSuggestions(suggestions=tuple(result), suppressed_duplicates=suppressed)


def _as_candidate_set(
    candidates: Sequence[Candidate] | CandidateSet, config: SuggestionsConfig
) -> CandidateSet:
    if not isinstance(candidates, CandidateSet):
        return CandidateSet(candidates, config)
    if candidates.tag_suffix_strip != tuple(config.tag_suffix_strip or ()):
        # Signal #2's variants are baked into the index. Re-indexing loudly is
        # the only safe answer: silently scoring against the old suffix list
        # would make `suggestions.tag_suffix_strip` a lie for as long as the
        # cached set lived.
        logger.warning(
            "suggest: candidate set was indexed for tag_suffix_strip=%r but the config now says "
            "%r — rebuilding it for this call",
            candidates.tag_suffix_strip,
            tuple(config.tag_suffix_strip or ()),
        )
        return CandidateSet(candidates.candidates, config)
    return candidates


def _collapse_same_name_notes(
    scored: list[tuple[Candidate, float, tuple[str, ...]]],
) -> tuple[list[tuple[Candidate, float, tuple[str, ...]]], int]:
    """Spec 21 §3.2: among NOTE candidates sharing a ``normalized_name`` AND
    an equal score, keep the shallowest path (fewest segments, then path
    ascending) and drop the rest.

    `eduardo-pontes-reis` resolves to two real files and three spellings of
    one answer in a ten-row list is the SQ-1 failure mode in miniature.
    FOLDERS ARE NEVER COLLAPSED (§2.2): two same-named folders are two
    destinations; two same-named notes at the same score are one answer
    written twice.
    """
    winners: dict[tuple[str, float], Candidate] = {}
    for candidate, score, _reasons in scored:
        if candidate.kind != KIND_NOTE:
            continue
        key = (candidate.normalized_name, score)
        current = winners.get(key)
        if current is None or _depth_key(candidate.path) < _depth_key(current.path):
            winners[key] = candidate
    kept: list[tuple[Candidate, float, tuple[str, ...]]] = []
    suppressed = 0
    for entry in scored:
        candidate, score, _reasons = entry
        if (
            candidate.kind == KIND_NOTE
            and winners[(candidate.normalized_name, score)].path != candidate.path
        ):
            suppressed += 1
            continue
        kept.append(entry)
    return kept, suppressed


def _depth_key(path: str) -> tuple[int, str]:
    return (len(path.strip("/").split("/")), path)


def _cap_note_rows(
    scored: list[tuple[Candidate, float, tuple[str, ...]]], config: SuggestionsConfig
) -> list[tuple[Candidate, float, tuple[str, ...]]]:
    """Spec 21 §3.3: ``suggestions.max_note_suggestions`` caps NOTE rows in
    the final list, folders filling the remainder. A person or project
    cluster can otherwise fill the visible list with one answer's
    neighbourhood — 1,150 of the 1,740 newly covered captures have a note at
    rank 1. Set it to ``max_suggestions`` to disable the cap."""
    cap = max(0, int(config.max_note_suggestions))
    kept: list[tuple[Candidate, float, tuple[str, ...]]] = []
    notes = 0
    for entry in scored:
        if entry[0].kind == KIND_NOTE:
            if notes >= cap:
                continue
            notes += 1
        kept.append(entry)
    return kept
