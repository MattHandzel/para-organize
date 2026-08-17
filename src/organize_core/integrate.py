"""The doc-12 §1 ``integrate`` engine: LLM proposal → structural guards →
reviewed application, with the complete doc-12 §2 trace.

Spec 12 §1, verbatim, is the whole contract:

    Inputs to the LLM: full target file, capture content + frontmatter, the
    destination's natural-language description (11), and the style directive:
    **preserve Matt's original wording from the capture as closely as
    possible, and match the target file's existing structure/voice/formatting
    conventions** — place content in the right section, don't paraphrase,
    don't editorialize, don't reformat untouched parts.

    Output: complete new target content. The core diffs it against the
    original and **hard-rejects** any result that deletes existing
    non-whitespace lines beyond a configurable threshold (default: zero
    deletions allowed outside the edited region) — integration adds and
    weaves; it never destroys.

    … ``no-ai: true`` targets refuse ``integrate`` outright.

Two halves, matching the ARCHITECTURE "Phase-5 integrate wire contract"
ruling (2026-08-16) that ``op.integrate_propose`` is STATELESS:

* :func:`propose` builds the prompt, calls the ONE shared LLM client, and
  returns a COMPLETE :class:`IntegrationProposal` — diff, rationale, the
  backend/model/prompt metadata, and the target's snapshot at propose time.
  It writes nothing to the vault. Every structural guard runs HERE, before
  any human sees the diff.
* :func:`apply` re-reads the target on the writer queue, re-checks the
  snapshot (``ConcurrentModificationError`` if the vault moved), RE-RUNS the
  deletion guard against what will actually be written, and applies or
  refuses — recording one ActionRecord in every case, including
  ``verdict: "rejected"``, which writes no vault byte at all.

Decisions this seat made where the spec is silent (each pinned by a test):

* **The deletion guard counts a multiset difference of non-blank lines**
  (``line.strip()`` as the key), not raw ``-`` lines in the diff. Spec 12 §1
  permits the capture to be *repositioned*; a target line that moved is still
  present, so it was not deleted. A line whose WORDS changed is gone, and
  counts. Comparing on ``strip()`` also means pure re-indentation is not a
  deletion — reformatting is policed by the prompt, not by the guard whose
  default threshold is zero.
* **Which frontmatter keys a capture "justifies"**:
  :data:`JUSTIFIED_FRONTMATTER_KEYS`. ``last_edited_date`` is machine
  bookkeeping; ``tags``/``sources`` may only GROW, and only with values the
  capture itself carries. Everything else — ``title``, ``aliases``, ``id``,
  ``no-ai``, ``description``, … — is a rewrite of Matt's metadata that no
  capture justifies, and is an :class:`IntegrationRejected`.
* **The applied bytes are exactly the reviewed bytes.** Unlike
  ``fileops.append_to_note``, integrate does NOT stamp ``last_edited_date``
  itself: what Matt reviewed in the diff pane is what lands on disk. (The
  guard permits the model to update that one key.)
* **``verbatim`` means whitespace-normalized containment** of the capture's
  BODY in the ADDED REGION of the result — the ``+`` side of the same
  line alignment the diff uses, never the whole result. Normalized
  containment is the strongest check that survives the legal transformations
  (re-wrapping, re-indenting under a heading) while catching the failure the
  directive exists to prevent: a paraphrase. Checking it against the whole
  result instead is what made it defeatable — a target that ALREADY held the
  capture's words satisfied it for free, so the model could return an
  editorialised paraphrase and pass.
* **Deleting nothing is not the same as destroying nothing.** Beside the
  deletion count sit three guards for the destruction shapes whose deletion
  count is zero: RE-ORDERING (:func:`reordered_line_count` — existing lines
  must still appear as a subsequence), unbounded GROWTH
  (``[integrate] max_added_lines`` beyond the capture's own line count) and
  DUPLICATION (an added line that re-emits an existing one the capture does
  not carry). All three are applied silently on a ``review = "auto"`` route,
  which is why the guard, not the prompt, has to hold.

Borrowed plumbing (deliberate, not laziness): the record/diff/log helpers
prefixed ``_`` are imported from :mod:`organize_core.fileops` rather than
re-implemented. ARCHITECTURE's structural decision 1 is "no bare file
mutation … every mutating fileop takes an OperationContext", and the
Phase-4 facade ruling insists recording stays "structurally unavoidable:
redirectable, never suppressible". A second record-building path in this
module is exactly the divergence those rulings forbid, so integrate reuses
fileops' one path and attaches its :class:`~organize_core.actions.LLMTrace`
through a recorder FACADE — the shape approved for multi-target route
records. Promoting those helpers to public names is a seam request for the
fileops seat; nothing here depends on their privacy.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import logging
import re
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from organize_core.actions import (
    VERDICTS,
    LLMTrace,
    Verdict,
    new_action_id,
)
from organize_core.config import Config
from organize_core.errors import (
    ConcurrentModificationError,
    FrontmatterError,
    IntegrationRejected,
    LLMError,
    NoAiRefusal,
    OperationError,
)
from organize_core.fileops import (
    FileSnapshot,
    OperationContext,
    OperationResult,
    _capture_state,
    _describe,
    _index_update,
    _log,
    _read_document,
    _record_action,
    _target_state,
    _unified_diff,
    atomic_write,
    backup_file,
    check_unmodified,
    require_in_vault,
)
from organize_core.frontmatter import Document, is_no_ai, normalize_tag, parse
from organize_core.llm import LLMClient, LLMResponse, extract_json, get_client

logger = logging.getLogger(__name__)

# --- vocabulary (LITERALS; the tests assert the literal AND this constant) --

#: ``ActionRecord.edit_mode`` for every record this module writes (12 §2).
EDIT_MODE = "integrate"

#: ``ActionRecord.operation`` for every record this module writes (12 §2).
OPERATION = "integrate"

#: ``targets[].role``. An integrate weaves a capture into an EXISTING note,
#: which is the merge role; ``learn.destinations_from_action`` reads it, so an
#: accepted integrate teaches the destination folder exactly like a merge
#: (``learn.LEARNED_OPERATIONS`` already contains ``"integrate"``).
TARGET_ROLE = "merge_target"

#: The doc-12 §2 actor for an LLM-authored integration. The composition root
#: sets ``OperationContext.actor``; this constant is what it must set it to.
ACTOR = "claude-integrate"

#: Frontmatter keys an integration may change, and the only ones.
#: ``last_edited_date`` is machine bookkeeping (``fileops.append_to_note``
#: writes it too); ``tags``/``sources`` may only GROW, and only with values
#: the capture carries — see :func:`_check_frontmatter`.
JUSTIFIED_FRONTMATTER_KEYS: frozenset[str] = frozenset({"last_edited_date", "tags", "sources"})

#: Keys :func:`_parse_response` accepts for the complete new target content,
#: most-authoritative first. Real models rename this field constantly; the
#: parse is tolerant, the CONTRACT is not (a missing one is an ``LLMError``).
CONTENT_KEYS: tuple[str, ...] = ("content", "new_content", "target_content", "result")

#: Same, for the human-readable justification (12 §1 review gate shows it).
RATIONALE_KEYS: tuple[str, ...] = ("rationale", "reason", "explanation")

SYSTEM_PROMPT = (
    "You weave a captured note into an existing note in a personal Obsidian vault. "
    "You preserve the author's exact words and you never delete his existing content."
)

#: The style directive of spec 12 §1, spelled out for the model. Kept as a
#: module constant so a test can assert the prompt really carries it — the
#: directive IS the feature.
PROMPT_TEMPLATE = """\
Integrate the CAPTURE below into the TARGET FILE below, and return the complete
new content of the target file.

Rules, in priority order:

1. VERBATIM: the capture's words are Matt's own. Copy the capture body into the
   target character for character. You may move it, put a heading above it,
   indent it into a list, or link it to nearby lines — you may NOT paraphrase,
   summarize, reword, correct, translate or editorialize it.{summarize_note}
2. THE TARGET'S STYLE: match the target file's existing conventions — heading
   levels and their capitalization, bullet/numbering characters, indentation,
   blank-line spacing, link style and the tone of the surrounding structure.
   The result must read as if Matt had typed it into that file himself.
3. THE RIGHT SECTION: put the capture where its subject already lives in the
   target. If nothing fits, add ONE new section in the target's own heading
   style, at the end.
4. ADD, NEVER DESTROY: every existing non-blank line of the target must still
   be present, unchanged, in your output. Do not rewrite, merge, tidy, re-order
   or delete existing lines. Do not reformat parts of the file you did not
   touch.
5. FRONTMATTER: return the target's YAML frontmatter unchanged. You may add the
   capture's tags to `tags` and update `last_edited_date`; you may change
   nothing else.

Respond with a single JSON object and nothing else:

{{"content": "<the COMPLETE new target file, frontmatter included>",
  "rationale": "<one or two sentences: where you put it and why>"}}

TARGET FILE: {target_path}
{description_block}<<<TARGET>>>
{target_text}
<<<END TARGET>>>

CAPTURE FILE: {capture_path}
<<<CAPTURE>>>
{capture_text}
<<<END CAPTURE>>>

The capture's body — this exact text must appear in your output:
<<<CAPTURE BODY>>>
{capture_body}
<<<END CAPTURE BODY>>>
"""

_SUMMARIZE_NOTE = (
    " (Summarizing is EXPLICITLY enabled for this destination, so condensing the"
    " capture is permitted here — keep Matt's phrasing wherever you can.)"
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_WHITESPACE_RE = re.compile(r"\s+")

#: The shape `fileops.append_to_note` stamps into ``last_edited_date`` — the
#: only shape an integration may write there (see :func:`_check_frontmatter`).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationDocument:
    """One side of an integration: the file's path, its exact text, its parsed
    document, and (for the target) the snapshot the concurrent-modification
    check verifies at commit (spec 10 §4)."""

    path: Path
    text: str
    doc: Document
    snapshot: FileSnapshot | None = None


def read_document(path: Path) -> IntegrationDocument:
    """Read + parse + snapshot in ONE read of ONE byte string.

    Delegates to fileops' reader, so the mtime is stat'd BEFORE the read and
    the snapshot describes the bytes we actually parsed — the ordering that
    makes the doc-10 §4 guarantee real rather than decorative.
    """
    doc, text, snapshot = _read_document(Path(path))
    return IntegrationDocument(path=Path(path), text=text, doc=doc, snapshot=snapshot)


# ---------------------------------------------------------------------------
# the proposal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationProposal:
    """A complete, self-contained integrate proposal (spec 12 §1 output +
    the ARCHITECTURE "STATELESS proposals" ruling).

    The whole object crosses the wire and comes back at commit time, because
    review happens exactly when an idle core exits: server-held state would
    yield "unknown proposal_id" at the moment Matt presses accept.
    ``proposal_id`` is therefore a CORRELATION id, never a lookup key.

    WHAT IS AND IS NOT TRUSTED (the wire contract's "trace fidelity trusts the
    single-user client; SAFETY never does", stated precisely — the blanket
    version overclaimed and one field falsified it):

    * ``summarize`` is NEVER read back (:meth:`from_json` forces ``False``).
      It disables 12 §1's verbatim guard, so a client that flipped one key —
      hostile, or merely buggy — landed a machine paraphrase on the vault
      under actor ``claude-integrate``. Like the actor, it is decided
      server-side or not at all.
    * ``target_snapshot`` protects against HONEST races only. It is
      client-supplied by design (the proposal is stateless), so a client that
      refreshes it gets a STALE proposal applied. That is a stale write, not
      data loss.
    * The CLIENT-INDEPENDENT properties are the ones that run against the
      re-read bytes: :func:`apply_unified_diff` refuses on any context
      mismatch, and :func:`check_guards` re-runs in full at commit for
      ``accepted`` AND ``edited``. Those are what make "integration adds and
      weaves; it never destroys" true of the MODE rather than of the model.
    """

    proposal_id: str
    capture_path: str
    target_path: str
    diff: str  # unified; applies cleanly to the text `target_snapshot` describes
    rationale: str
    backend: str
    model: str
    prompt_hash: str
    target_snapshot: FileSnapshot
    route: str | None = None
    description: str | None = None
    #: True only when the caller explicitly configured summarize mode, which
    #: is what disables the verbatim-preservation guard (spec 12 §1).
    summarize: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "capture_path": self.capture_path,
            "target_path": self.target_path,
            "diff": self.diff,
            "rationale": self.rationale,
            "llm": {
                "backend": self.backend,
                "model": self.model,
                "prompt_hash": self.prompt_hash,
                "proposed_diff": self.diff,
            },
            "target_snapshot": {
                "path": self.target_snapshot.path,
                "mtime": self.target_snapshot.mtime,
                "sha256": self.target_snapshot.sha256,
            },
            "route": self.route,
            "description": self.description,
            "summarize": self.summarize,
        }

    @classmethod
    def from_json(cls, raw: Any) -> IntegrationProposal:
        """Rebuild a proposal a client handed back. Malformed input is an
        ADDRESSING failure that raises (ARCHITECTURE "Where the error line
        falls") — there is nothing to attempt and no oplog line to write."""
        if not isinstance(raw, dict):
            raise OperationError(
                f"an integrate proposal must be a JSON object, got {type(raw).__name__}",
                hint="pass back the object op.integrate_propose returned, unmodified",
            )
        snapshot = raw.get("target_snapshot")
        if not isinstance(snapshot, dict):
            raise OperationError(
                "the integrate proposal is missing target_snapshot",
                hint="pass back the object op.integrate_propose returned, unmodified; "
                "the snapshot is what makes the concurrent-modification check possible",
            )
        llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
        try:
            return cls(
                proposal_id=_req_text(raw, "proposal_id"),
                capture_path=_req_text(raw, "capture_path"),
                target_path=_req_text(raw, "target_path"),
                diff=_req_text(raw, "diff"),
                rationale=str(raw.get("rationale") or ""),
                backend=str(llm.get("backend") or ""),
                model=str(llm.get("model") or ""),
                prompt_hash=str(llm.get("prompt_hash") or ""),
                target_snapshot=FileSnapshot(
                    path=_req_text(snapshot, "path"),
                    mtime=float(snapshot["mtime"]),
                    sha256=_req_text(snapshot, "sha256"),
                ),
                route=_opt_text(raw, "route"),
                description=_opt_text(raw, "description"),
                # `summarize` is DELIBERATELY NOT READ BACK. It is the one
                # field of this object that disables a SAFETY guard — spec
                # 12 §1's verbatim directive, the thing integrate exists to
                # enforce — and the wire contract's rule is that trace
                # fidelity trusts the single-user client while the guards
                # never do. Reading it here made the guard client-settable:
                # flipping one key in a proposal file (or one field in an
                # RPC payload) let a machine paraphrase land on the vault
                # under actor `claude-integrate` and fold into learning.json.
                # It is re-derived server-side instead, in `apply`, from the
                # route/config the commit resolves for itself — the same
                # reason the actor is FORCED to `integrate.ACTOR` rather
                # than read from params.
                summarize=False,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise OperationError(
                f"the integrate proposal is malformed: {exc}",
                hint="pass back the object op.integrate_propose returned, unmodified",
            ) from exc


def _req_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise OperationError(
            f"the integrate proposal is missing {key!r}",
            hint="pass back the object op.integrate_propose returned, unmodified",
        )
    return value


def _opt_text(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------


def integrate_client(config: Config) -> LLMClient:
    """The client for THIS path. Spec 12 §1: "integrations are the
    quality-sensitive path, so default this one to ``claude-cli`` when
    available" — which is what ``purpose="integrate"`` selects (it reads
    ``llm.integrate_backend``, not ``llm.backend``)."""
    return get_client(config.llm, purpose="integrate")


def build_prompt(
    capture: IntegrationDocument,
    target: IntegrationDocument,
    *,
    description: str | None = None,
    route: str | None = None,
    summarize: bool = False,
) -> str:
    """The spec 12 §1 prompt: full target file, capture content + frontmatter,
    the destination's natural-language description (11 §3) when there is one,
    and the style directive."""
    lines: list[str] = []
    if route:
        lines.append(f"ROUTE: {route}")
    if description:
        lines.append(f"WHAT THIS DESTINATION IS FOR: {description}")
    description_block = ("\n".join(lines) + "\n") if lines else ""
    return PROMPT_TEMPLATE.format(
        summarize_note=_SUMMARIZE_NOTE if summarize else "",
        target_path=target.path,
        description_block=description_block,
        target_text=target.text,
        capture_path=capture.path,
        capture_text=capture.text,
        capture_body=capture.doc.body.strip("\n"),
    )


def prompt_hash(prompt: str) -> str:
    """``llm.prompt_hash`` (12 §2) — sha256 of the exact prompt sent."""
    return hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()


def _parse_response(response: LLMResponse) -> tuple[str, str]:
    """``(content, rationale)`` or ``LLMError``.

    Robust in the shapes real models emit (``llm.extract_json`` handles
    fences, prose preambles and braces inside strings) and STRICT about the
    contract: a response missing either field is a failed call, never a
    partial proposal. A partial proposal is the dangerous shape — it would
    reach the review pane looking like a real integration.
    """
    payload = response.json if isinstance(response.json, dict) else extract_json(response.text)
    if not isinstance(payload, dict):
        raise LLMError(
            f"{response.backend} returned no JSON object for the integrate proposal",
            hint="the integrate prompt asks for a single JSON object with 'content' and "
            "'rationale'; nothing was applied",
        )
    content = _first_text(payload, CONTENT_KEYS)
    if content is None:
        raise LLMError(
            f"{response.backend} returned no {CONTENT_KEYS[0]!r} field for the integrate proposal",
            hint="'content' must be the COMPLETE new target file; a partial proposal is "
            "never applied",
        )
    rationale = _first_text(payload, RATIONALE_KEYS)
    if rationale is None:
        raise LLMError(
            f"{response.backend} returned no {RATIONALE_KEYS[0]!r} field for the integrate "
            "proposal",
            hint="the review gate shows Matt the rationale next to the diff (12 §1); a "
            "proposal without one is incomplete",
        )
    return content, rationale


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def normalize_whitespace(text: str) -> str:
    """Every run of whitespace collapsed to one space, ends stripped — the
    "verbatim (normalized whitespace)" reading of spec 12 §1's "don't
    paraphrase"."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def deleted_line_count(before: str, after: str) -> int:
    """Existing non-whitespace lines of ``before`` that ``after`` no longer
    holds (spec 12 §1's "deletes existing non-whitespace lines").

    A MULTISET difference keyed on ``line.strip()``: a line that merely MOVED
    is still present and is not a deletion (12 §1 lets the capture be
    repositioned), a line whose words changed IS gone and counts, and two
    identical lines reduced to one counts as one deletion.
    """
    kept: Counter[str] = Counter(line.strip() for line in after.splitlines() if line.strip())
    lost = 0
    for line in before.splitlines():
        key = line.strip()
        if not key:
            continue
        if kept[key] > 0:
            kept[key] -= 1
        else:
            lost += 1
    return lost


def added_lines(before: str, after: str) -> list[str]:
    """The lines of ``after`` that ``before`` did NOT contribute — the "added
    region" every growth-shaped guard reasons about.

    Taken from the same line-level alignment the unified diff uses
    (``difflib``'s ``insert``/``replace`` opcodes = the ``+`` lines), NOT from
    a multiset difference. The distinction is load-bearing for a target that
    ALREADY contains the capture's words: a multiset difference would credit
    the pre-existing copy for the new one and report nothing added, while the
    diff correctly reports the newly inserted duplicate. That is exactly the
    shape that let a paraphrase pass the verbatim guard (a repeat capture, or
    a route appending to a log where the same phrase recurs).
    """
    old = before.splitlines()
    new = after.splitlines()
    out: list[str] = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
        None, old, new, autojunk=False
    ).get_opcodes():
        if tag in {"insert", "replace"}:
            out.extend(new[j1:j2])
    return out


def reordered_line_count(before: str, after: str) -> int:
    """Existing non-blank lines of ``before`` that ``after`` still holds, but
    NOT in their original relative order (spec 12 §1: "don't reformat
    untouched parts"; the prompt's rule 4 spells it "do not … re-order").

    :func:`deleted_line_count` is an order-INSENSITIVE multiset, so a model
    that scrambles Matt's note across its own headings deletes nothing and
    passes it — semantic destruction with a deletion count of zero, applied
    without review on a ``review = "auto"`` route. This is the companion
    check: greedily match ``before``'s non-blank lines as a SUBSEQUENCE of
    ``after``'s, then subtract the genuine deletions, so a permitted deletion
    (``max_deleted_lines > 0``) is never counted twice. Every legal insertion
    or repositioning of the CAPTURE leaves this at zero, because the capture's
    lines are not in ``before``.

    O(n log n): the candidate positions of each line are indexed once and
    binary-searched, so a pathological target cannot make this quadratic.
    """
    old_keys = [line.strip() for line in before.splitlines() if line.strip()]
    new_keys = [line.strip() for line in after.splitlines() if line.strip()]
    positions: dict[str, list[int]] = {}
    for index, key in enumerate(new_keys):
        positions.setdefault(key, []).append(index)
    cursor = 0
    unmatched = 0
    for key in old_keys:
        spots = positions.get(key)
        if not spots:
            unmatched += 1
            continue
        spot = bisect_left(spots, cursor)
        if spot >= len(spots):
            unmatched += 1
        else:
            cursor = spots[spot] + 1
    # Every genuinely deleted line is also unmatchable, so the difference is
    # the count of lines that survived but MOVED.
    return max(0, unmatched - deleted_line_count(before, after))


def check_guards(
    before: str,
    after: str,
    capture: Document,
    *,
    config: Config,
    summarize: bool = False,
    path: Path | None = None,
) -> None:
    """Every structural rejection of spec 12 §1, in one place.

    Runs on the RESULT the diff produces, never on anything the model claims
    about itself, and runs BEFORE any review — the diff Matt is shown has
    already survived this. :func:`apply` runs it AGAIN at commit against the
    bytes that will actually be written (ARCHITECTURE Phase-5 wire contract:
    "integration adds and weaves; it never destroys" binds the MODE, not just
    the LLM; a buggy client truncating the buffer must not mass-delete under
    Matt's name).

    ORDER MATTERS ONLY FOR THE MESSAGE — every guard below refuses, so no
    ordering can let a bad proposal through. The frontmatter guard runs
    BEFORE the deletion guard because a rewritten ``title:`` is also a deleted
    line, and "changes frontmatter key ['title'] that the capture does not
    justify" is the actionable diagnosis where "deletes 1 line" is not
    (09 §1.5). The deletion guard remains the catch-all for the body, and the
    gutting-proposal pin exercises it through frontmatter that never changes.

    Every guard carries a ``kind`` so :func:`_guard_error` can keep that
    actionable diagnosis when it re-wraps a commit-time refusal, instead of
    calling a paraphrase "the deletion guard" and pointing at a merge editor
    for content that was never being deleted.

    The four guards below the deletion count are what make the mode's promise
    ("integration adds and weaves; it never destroys") true against a model
    that deletes NOTHING: re-ordering scrambles the note with a deletion count
    of zero, unbounded growth injects fabricated lines, duplication makes
    Matt's own content appear twice, and a paraphrase only looks verbatim if
    the check is allowed to credit text that was already in the target.
    """
    where = f" for {path}" if path is not None else ""

    if after == before:
        raise IntegrationRejected(
            f"the integrate proposal changes nothing{where}",
            hint="an empty-effect proposal is a failed integration, not a no-op: the "
            "capture never landed anywhere. Try a different destination, or file it "
            "with move/append.",
            kind="empty_effect",
        )

    _check_frontmatter(before, after, capture, where=where)

    limit = int(config.integrate.max_deleted_lines)
    deleted = deleted_line_count(before, after)
    if deleted > limit:
        raise IntegrationRejected(
            f"the integrate proposal deletes {deleted} existing non-whitespace line(s)"
            f"{where}; the limit is {limit}",
            hint="integration adds and weaves; it never destroys (spec 12 §1). Nothing was "
            "written. Raise [integrate] max_deleted_lines only if you really want an LLM "
            "removing your notes.",
            kind="deletion",
        )

    moved = reordered_line_count(before, after)
    if moved:
        raise IntegrationRejected(
            f"the integrate proposal re-orders {moved} existing line(s){where}",
            hint="integration weaves the capture IN; it does not rearrange what is already "
            "there (spec 12 §1: don't reformat untouched parts). Every existing line must "
            "still appear in its original relative order. Nothing was written.",
            kind="reorder",
        )

    before_body = _body_of(before, where=where)
    after_body = _body_of(after, where=where)
    added = [line for line in added_lines(before_body, after_body) if line.strip()]

    capture_body_lines = len([line for line in capture.body.splitlines() if line.strip()])
    slack = int(config.integrate.max_added_lines)
    growth_limit = capture_body_lines + slack
    if len(added) > growth_limit:
        raise IntegrationRejected(
            f"the integrate proposal adds {len(added)} non-blank line(s){where}, but the "
            f"capture has only {capture_body_lines}; the limit is {growth_limit} "
            f"({capture_body_lines} + [integrate] max_added_lines = {slack})",
            hint="an integration adds the capture and the little structure it needs — a "
            "heading, a bullet — not a body of its own. A proposal this much larger than "
            "the capture is a hallucinating model, and on a review = \"auto\" route no "
            "human would ever see it. Nothing was written.",
            kind="growth",
        )

    existing = Counter(line.strip() for line in before_body.splitlines() if line.strip())
    capture_text = normalize_whitespace(capture.body)
    duplicated = sorted(
        {
            line.strip()
            for line in added
            if existing.get(line.strip()) and not _carries_capture(line, capture_text)
        }
    )
    if duplicated:
        shown = duplicated[:3]
        raise IntegrationRejected(
            f"the integrate proposal duplicates {len(duplicated)} line(s) the target "
            f"already has{where}: {shown}",
            hint="re-emitting Matt's existing lines makes his note say everything twice, "
            "which the deletion guard cannot see (nothing was removed). Only the capture's "
            "own text may be added. Nothing was written.",
            kind="duplication",
        )

    if not summarize:
        wanted = capture_text
        # CONTAINMENT IS CHECKED AGAINST THE ADDED REGION, never the whole
        # result: a target that already holds the capture's words would
        # otherwise satisfy this guard for free, and the model could return an
        # editorialised paraphrase — defeating the one directive 12 §1 exists
        # to enforce, unattended, on the review = "auto" path.
        if wanted and wanted not in normalize_whitespace("\n".join(added)):
            raise IntegrationRejected(
                f"the integrate proposal does not contain the capture's text verbatim{where}",
                hint="spec 12 §1: preserve Matt's original wording from the capture — the "
                "model paraphrased it, or added nothing that carries it. Nothing was "
                "written.",
                kind="verbatim",
            )


def _carries_capture(line: str, capture_text: str) -> bool:
    """Whether ``line`` is part of the capture landing, in either direction:
    the capture's whole text may sit INSIDE one added line (the model put a
    ``- `` in front of it), or one added line may be a FRAGMENT of a multi-line
    capture. Re-emitting an existing target line is only justified when the
    capture is what put it there — otherwise it is Matt's own content said
    twice."""
    if not capture_text:
        return False
    normalized = normalize_whitespace(line)
    return bool(normalized) and (normalized in capture_text or capture_text in normalized)


def _body_of(text: str, *, where: str) -> str:
    return _parse_result(text, where=where).body


def _parse_result(text: str, *, where: str) -> Document:
    try:
        return parse(text)
    except FrontmatterError as exc:
        raise IntegrationRejected(
            f"the integrate proposal's frontmatter does not parse{where}: {exc}",
            hint="a mutating operation never writes a file it cannot round-trip "
            "(spec 05 §9). Nothing was written.",
            kind="unparseable",
        ) from exc


def _fields_of(doc: Document) -> dict[str, Any]:
    return dict(doc.frontmatter.fields) if doc.frontmatter is not None else {}


def _check_frontmatter(before: str, after: str, capture: Document, *, where: str) -> None:
    """Reject any frontmatter change the capture does not justify (12 §1)."""
    old = _fields_of(_parse_result(before, where=where))
    new = _fields_of(_parse_result(after, where=where))

    removed = sorted(set(old) - set(new))
    if removed:
        raise IntegrationRejected(
            f"the integrate proposal removes frontmatter key(s) {removed}{where}",
            hint="integrate may add and weave; it never removes Matt's metadata "
            "(spec 12 §1). Nothing was written.",
            kind="frontmatter",
        )

    touched = sorted(key for key in new if _jsonish(new[key]) != _jsonish(old.get(key)))
    unjustified = [key for key in touched if key not in JUSTIFIED_FRONTMATTER_KEYS]
    if unjustified:
        raise IntegrationRejected(
            f"the integrate proposal changes frontmatter key(s) {unjustified} that the "
            f"capture does not justify{where}",
            hint="only "
            + ", ".join(sorted(JUSTIFIED_FRONTMATTER_KEYS))
            + " may change, and tags/sources only by gaining values the capture carries "
            "(spec 12 §1). Nothing was written.",
            kind="frontmatter",
        )

    if "last_edited_date" in touched:
        # `last_edited_date` is on the justified list as MACHINE BOOKKEEPING —
        # the same stamp `fileops.append_to_note` writes. Without a shape
        # check the model may write arbitrary text into Matt's frontmatter and
        # it lands unreviewed on the auto path, where the whole vault is
        # indexed from that frontmatter and the value flows back into later
        # prompts as note metadata.
        value = new.get("last_edited_date")
        text = value if isinstance(value, str) else str(value)
        if not _DATE_RE.match(text.strip()):
            raise IntegrationRejected(
                f"the integrate proposal sets last_edited_date to {text.strip()!r}{where}, "
                "which is not a YYYY-MM-DD date",
                hint="last_edited_date is machine bookkeeping (the same stamp "
                "`organize` writes itself), not free text. Nothing was written.",
                kind="frontmatter",
            )

    capture_fields = _fields_of(capture)
    for key in ("tags", "sources"):
        if key not in touched:
            continue
        old_values = _str_list(old.get(key))
        new_values = _str_list(new.get(key))
        dropped = _missing_values(old_values, new_values, key=key)
        if dropped:
            raise IntegrationRejected(
                f"the integrate proposal drops {key} {sorted(dropped)} from the target{where}",
                hint=f"{key} may only GROW during an integration (spec 12 §1). Nothing was "
                "written.",
                kind="frontmatter",
            )
        allowed = {_match_key(v, key=key) for v in old_values} | {
            _match_key(v, key=key) for v in _str_list(capture_fields.get(key))
        }
        invented = sorted(
            {v for v in new_values if _match_key(v, key=key) not in allowed}
        )
        if invented:
            raise IntegrationRejected(
                f"the integrate proposal invents {key} {invented} that the capture does "
                f"not carry{where}",
                hint=f"an integration may copy the capture's {key} onto the target, not "
                "make up new ones (spec 12 §1). Nothing was written.",
                kind="frontmatter",
            )


def _missing_values(old_values: list[str], new_values: list[str], *, key: str) -> set[str]:
    """Values of ``old_values`` the proposal no longer holds, compared through
    :func:`_match_key` so a re-spelled tag is not read as a drop."""
    kept = {_match_key(v, key=key) for v in new_values}
    return {v for v in old_values if _match_key(v, key=key) not in kept}


def _match_key(value: str, *, key: str) -> str:
    """Tags compare through THE shared normalizer (09 §2); sources compare as
    written (they are free text, e.g. ``me``)."""
    return normalize_tag(value) if key == "tags" else value.strip()


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [item if isinstance(item, str) else str(item) for item in value]
    return [value if isinstance(value, str) else str(value)]


def _jsonish(value: Any) -> Any:
    """Comparable shape for a frontmatter value (lists/maps compare by
    content, everything else by its string form)."""
    if isinstance(value, (list, tuple)):
        return [_jsonish(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonish(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if value is None:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# unified-diff application
# ---------------------------------------------------------------------------


def apply_unified_diff(before: str, diff: str) -> str:
    """Apply a unified diff to ``before``, STRICTLY.

    Every context and removed line must match the file exactly (modulo the
    end-of-file newline); a diff that does not apply raises rather than
    guessing. This is the counterpart of the STATELESS wire contract: the
    proposal carries a diff, not the rendered result, so the commit path must
    be able to reconstruct the result from bytes it re-read itself.

    Tolerances, all deliberate: an empty context line may arrive as ``"\\n"``
    instead of ``" \\n"`` (editors strip trailing whitespace from a diff
    buffer), and ``\\ No newline at end of file`` markers are ignored. Only
    the LAST line of the diff may lack a trailing newline; anywhere else it
    is ambiguous and refused.
    """
    source = before.splitlines(keepends=True)
    diff_lines = diff.splitlines(keepends=True)
    out: list[str] = []
    cursor = 0
    hunks = 0

    index = 0
    while index < len(diff_lines):
        raw = diff_lines[index]
        stripped = raw.rstrip("\n")
        index += 1
        if stripped.startswith(("--- ", "+++ ", "diff ", "index ")):
            continue
        if not stripped.startswith("@@"):
            if stripped == "":
                continue
            raise OperationError(
                f"the diff has content outside any hunk: {stripped[:60]!r}",
                hint="expected a unified diff (@@ hunk headers); nothing was written",
            )
        match = _HUNK_RE.match(stripped)
        if match is None:
            raise OperationError(
                f"malformed unified-diff hunk header: {stripped[:60]!r}",
                hint="expected '@@ -<start>,<len> +<start>,<len> @@'; nothing was written",
            )
        hunks += 1
        start = int(match.group(1))
        begin = start - 1 if start > 0 else 0
        if begin < cursor:
            raise OperationError(
                f"the diff's hunks are out of order at line {start}",
                hint="hunks must be in ascending order and must not overlap; nothing was written",
            )
        if begin > len(source):
            raise OperationError(
                f"the diff expects a line {start} the target does not have "
                f"({len(source)} lines)",
                hint="the diff does not apply to this target; nothing was written",
            )
        out.extend(source[cursor:begin])
        cursor = begin

        while index < len(diff_lines):
            body = diff_lines[index]
            if body.startswith("@@"):
                break
            index += 1
            if body.startswith("\\"):  # "\ No newline at end of file"
                continue
            marker, content = (body[0], body[1:]) if body[:1] in {" ", "-", "+"} else (" ", body)
            last = index >= len(diff_lines)
            if not content.endswith("\n") and not last:
                raise OperationError(
                    "the diff has a line with no newline before its end",
                    hint="only the final line of a unified diff may lack a trailing "
                    "newline; nothing was written",
                )
            if marker in {" ", "-"}:
                if cursor >= len(source):
                    raise OperationError(
                        "the diff expects more lines than the target has",
                        hint="the diff does not apply to this target; nothing was written",
                    )
                have = source[cursor]
                if content.rstrip("\n") != have.rstrip("\n"):
                    raise OperationError(
                        f"the diff does not apply: expected {content.rstrip(chr(10))!r} at "
                        f"line {cursor + 1}, found {have.rstrip(chr(10))!r}",
                        hint="re-propose against the current file; nothing was written",
                    )
                cursor += 1
                if marker == " ":
                    out.append(have)
            else:
                out.append(content)

    if hunks == 0:
        raise OperationError(
            "the diff contains no hunks",
            hint="expected a unified diff with at least one '@@' hunk; nothing was written",
        )
    if out and not out[-1].endswith("\n") and cursor < len(source):
        raise OperationError(
            "the diff drops the newline of a line that is not the last one",
            hint="only the final line of a file may lack a trailing newline; nothing was written",
        )
    out.extend(source[cursor:])
    return "".join(out)


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------


def propose(
    capture_doc: IntegrationDocument,
    target_doc: IntegrationDocument,
    config: Config,
    llm: LLMClient,
    *,
    route: str | None = None,
    description: str | None = None,
    ctx: OperationContext | None = None,
    summarize: bool = False,
    now: float | None = None,
) -> IntegrationProposal:
    """Ask the LLM to weave ``capture_doc`` into ``target_doc`` (spec 12 §1).

    Writes NOTHING to the vault. ``ctx`` is a RECORDING seam only: when it is
    supplied, a structural rejection appends its ``verdict: "rejected"``
    ActionRecord before raising, which is spec 12 §3's acceptance item
    ("an LLM proposal that removes existing lines is rejected and recorded
    with ``verdict: "rejected"``, target untouched") and doc 12's whole point
    — "a rejection is as much signal as an acceptance".

    ``no-ai`` refuses here for EVERY actor, human included (12 §1 "``no-ai:
    true`` targets refuse ``integrate`` outright"; ARCHITECTURE Phase-4
    ruling: "Integrate refuses for EVERY actor (12 §1)"), on BOTH documents —
    integrating copies the capture's text into another file, so a no-ai
    CAPTURE is as protected as a no-ai target.
    """
    _refuse_no_ai(capture_doc.path, capture_doc.doc, "integrate from")
    _refuse_no_ai(target_doc.path, target_doc.doc, "integrate into")

    if target_doc.snapshot is None:
        raise OperationError(
            f"no snapshot for the integrate target {target_doc.path}",
            hint="build the target with integrate.read_document(); without a snapshot the "
            "commit could not detect a concurrent modification (spec 10 §4)",
        )
    if description is None and ctx is not None:
        description = resolve_description(ctx, Path(target_doc.path))

    prompt = build_prompt(
        capture_doc, target_doc, description=description, route=route, summarize=summarize
    )
    digest = prompt_hash(prompt)
    response = llm.generate(prompt, system=SYSTEM_PROMPT, json_mode=True)
    content, rationale = _parse_response(response)

    diff = _unified_diff(target_doc.text, content, Path(target_doc.path))
    try:
        check_guards(
            target_doc.text,
            content,
            capture_doc.doc,
            config=config,
            summarize=summarize,
            path=Path(target_doc.path),
        )
    except IntegrationRejected:
        if ctx is not None:
            _record_rejection(
                ctx,
                capture_doc=capture_doc,
                target_doc=target_doc,
                trace=LLMTrace(
                    backend=response.backend,
                    model=response.model,
                    prompt_hash=digest,
                    proposed_diff=diff,
                    final_diff="",
                    verdict="rejected",
                ),
                route=route,
                description=description,
            )
        raise

    # SELF-CHECK — the wire carries a diff, not the rendered result, so a diff
    # that does not reproduce the model's content must never reach the review
    # pane. It is also the one place the unrepresentable case surfaces: when
    # the target's last line has no trailing newline, the shared renderer
    # emits a hunk line with no line terminator in the MIDDLE of the diff, and
    # nothing can tell where that line ended. Refuse loudly; never guess.
    try:
        rebuilt = apply_unified_diff(target_doc.text, diff)
    except OperationError as exc:
        raise OperationError(
            f"the integration of {capture_doc.path} into {target_doc.path} cannot be "
            f"represented as a unified diff: {exc}",
            hint="the target's final line has no trailing newline, which makes a unified "
            "diff ambiguous. Add a trailing newline to the target and re-propose, or use "
            "the manual merge path (spec 03 §5). Nothing was written.",
        ) from exc
    if rebuilt != content:  # pragma: no cover - a bug in our own diff renderer
        raise OperationError(
            f"the proposed diff for {target_doc.path} does not reproduce the proposed content",
            hint="this is an organize bug: the diff handed to the review pane must apply "
            "cleanly to the target. Nothing was written.",
        )

    return IntegrationProposal(
        proposal_id=new_proposal_id(now=now),
        capture_path=str(capture_doc.path),
        target_path=str(target_doc.path),
        diff=diff,
        rationale=rationale,
        backend=response.backend,
        model=response.model,
        prompt_hash=digest,
        target_snapshot=target_doc.snapshot,
        route=route,
        description=description,
        summarize=summarize,
    )


def resolve_description(ctx: OperationContext, target: Path) -> str | None:
    """The destination's natural-language description (11) for the prompt and
    for ``targets[].description`` (12 §2).

    Public because a composition root may need to resolve it BEFORE calling
    :func:`propose` — the server does, so the folder's index note is read under
    the same read lock as every other index read, and the lock can then be
    released for the model call. Same one code path either way; ``propose``
    calls this itself when the caller passes no description.
    """
    return _describe(ctx, Path(target).parent)


def new_proposal_id(*, now: float | None = None) -> str:
    """``prop_<ulid>`` — the same stdlib ULID the action corpus uses, so a
    proposal id sorts beside the records it produced (12 §2 ``act_<ulid>``)."""
    return "prop_" + new_action_id(now=now).split("_", 1)[1]


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def apply(
    ctx: OperationContext,
    target: Path,
    proposal: IntegrationProposal,
    verdict: Verdict,
    final_diff: str | None = None,
) -> OperationResult:
    """Commit a reviewed proposal (spec 12 §1 review gate, §2 recording).

    ``accepted`` applies ``proposal.diff``; ``edited`` applies ``final_diff``
    (Matt's own modification of the proposal) and records BOTH diffs, which is
    what turns every reviewed integration into a labeled edit example (12 §2);
    ``rejected`` records the verdict and the full trace and writes NO vault
    byte.

    Fresh checks, in order (ARCHITECTURE "Phase-5 integrate wire contract"):
    ``no-ai`` refusal for every actor, the ``target_snapshot`` TOCTOU check,
    then the deletion guard re-run against what will actually be written —
    for ``accepted`` AND ``edited``. A guard violation on ``edited`` points at
    the MANUAL merge path, which is guard-free by design.

    The TOCTOU check is deliberately NOT applied to ``rejected``: nothing is
    written, and refusing to record a rejection because the target moved would
    throw away the negative signal that doc 12 exists to collect.
    """
    now = ctx.clock()
    if verdict not in VERDICTS:
        raise OperationError(
            f"{verdict!r} is not an integrate verdict",
            hint=f"expected one of {sorted(VERDICTS)}",
        )

    target = require_in_vault(ctx.config, Path(target), "integrate target")
    capture_path = require_in_vault(ctx.config, Path(proposal.capture_path), "capture")
    if not _same_path(target, Path(proposal.target_path)):
        raise OperationError(
            f"the proposal names target {proposal.target_path} but the commit names {target}",
            hint="commit the proposal against the file it was proposed for; nothing was written",
        )
    if not _same_path(target, Path(proposal.target_snapshot.path)):
        raise OperationError(
            f"the proposal's snapshot describes {proposal.target_snapshot.path}, not {target}",
            hint="the snapshot is what the concurrent-modification check verifies; nothing "
            "was written",
        )
    if not target.is_file():
        raise OperationError(
            f"integrate target does not exist: {target}",
            hint="re-propose against a file that exists; nothing was written",
        )
    if not capture_path.is_file():
        raise OperationError(
            f"capture does not exist: {capture_path}",
            hint="the record must name the capture that was integrated; nothing was written",
        )

    target_doc = read_document(target)
    capture_doc = read_document(capture_path)
    _refuse_no_ai(capture_path, capture_doc.doc, "integrate from")
    _refuse_no_ai(target, target_doc.doc, "integrate into")

    description = proposal.description or _describe(ctx, target.parent)
    capture_state = _capture_state(capture_path, capture_doc.text, capture_doc.doc)

    if verdict == "rejected":
        # The TOCTOU check is deliberately NOT enforced here (refusing to
        # record a rejection because the target moved would throw away the
        # negative signal doc 12 exists to collect) — but the record must be
        # HONEST about it. `before_text` below is the target as it is NOW,
        # while `proposed_diff` was rendered against the propose-time text, so
        # after a race the two do not go together and replaying the diff
        # against its own before-state fails. Mark it, so the corpus reader
        # can skip an example it cannot replay instead of learning from it.
        stale_target = False
        try:
            check_unmodified(proposal.target_snapshot)
        except ConcurrentModificationError:
            stale_target = True
            logger.warning(
                "integrate: %s changed between propose and this REJECTED commit; the "
                "record's proposed_diff cannot be replayed against its before_text and "
                "is marked stale_target (spec 12 §2)",
                target,
            )
        # No operations-log line: the oplog records what happened to the VAULT,
        # and an OK line for an operation that touched nothing would claim a
        # mutation that never happened (the asymmetry `op.skip` already has).
        _record(
            ctx,
            capture=capture_state,
            targets=[
                _target_state(
                    target, target_doc.text, target_doc.text, TARGET_ROLE, description=description
                )
            ],
            trace=_trace(
                proposal,
                proposed=proposal.diff,
                final="",
                verdict="rejected",
                stale_target=stale_target,
            ),
            route=proposal.route,
            now=now,
        )
        return OperationResult(
            ok=True,
            operation=OPERATION,
            source=str(capture_path),
            destination=str(target),
            dry_run=ctx.dry_run,
            details={
                "verdict": "rejected",
                "proposal_id": proposal.proposal_id,
                "written": False,
                "edit_mode": EDIT_MODE,
            },
        )

    check_unmodified(proposal.target_snapshot)

    if verdict == "edited":
        if not (final_diff or "").strip():
            raise OperationError(
                "verdict 'edited' needs the final diff that was actually applied",
                hint="pass --final-diff FILE (or `final_diff`); recording Matt's edit is the "
                "point of the 'edited' verdict (12 §2). Nothing was written.",
            )
        chosen = final_diff or ""
    else:
        if final_diff is not None and final_diff != proposal.diff:
            raise OperationError(
                "verdict 'accepted' applies the proposal unchanged, but a different final "
                "diff was supplied",
                hint="use verdict 'edited' to apply your own version; nothing was written",
            )
        chosen = proposal.diff

    new_text = apply_unified_diff(target_doc.text, chosen)
    try:
        check_guards(
            target_doc.text,
            new_text,
            capture_doc.doc,
            config=ctx.config,
            # SERVER-DERIVED, never client-supplied: a proposal that crossed a
            # wire always reads back `summarize=False`
            # (:meth:`IntegrationProposal.from_json` refuses the key), so this
            # can only be true for an in-process proposal a composition root
            # built with `propose(..., summarize=True)`. That is what keeps
            # 12 §1's verbatim directive un-disableable by the client.
            summarize=proposal.summarize,
            path=target,
        )
    except IntegrationRejected as exc:
        _record(
            ctx,
            capture=capture_state,
            targets=[
                _target_state(
                    target, target_doc.text, target_doc.text, TARGET_ROLE, description=description
                )
            ],
            trace=_trace(proposal, proposed=proposal.diff, final=chosen, verdict="rejected"),
            route=proposal.route,
            now=now,
        )
        raise _guard_error(exc, verdict) from exc

    # What Matt reviewed is what lands: the ONE diff renderer the corpus uses,
    # so `proposed_diff` and `final_diff` are comparable byte for byte (they
    # are identical for an accepted verdict — pinned).
    applied_diff = _unified_diff(target_doc.text, new_text, target)
    targets = [
        _target_state(target, target_doc.text, new_text, TARGET_ROLE, description=description)
    ]
    trace = _trace(proposal, proposed=proposal.diff, final=applied_diff, verdict=verdict)
    details: dict[str, Any] = {
        "verdict": verdict,
        "proposal_id": proposal.proposal_id,
        "written": not ctx.dry_run,
        "edit_mode": EDIT_MODE,
    }

    if ctx.dry_run:
        _log(ctx, OPERATION, capture_path, target, success=True, now=now)
        _record(ctx, capture=capture_state, targets=targets, trace=trace, route=proposal.route, now=now)
        return OperationResult(
            ok=True,
            operation=OPERATION,
            source=str(capture_path),
            destination=str(target),
            dry_run=True,
            details=details,
        )

    check_unmodified(target_doc.snapshot)  # type: ignore[arg-type]

    backup: Path | None = None
    if ctx.config.file_ops.create_backups:
        # 12 §1: "In every case the target is backed up first and the write is
        # atomic (doc 05)".
        try:
            backup = backup_file(target, ctx.backup_dir, now=now)
        except OSError as exc:
            return _failed(ctx, capture_path, target, f"could not back up {target}: {exc}", now)

    check_unmodified(target_doc.snapshot)  # type: ignore[arg-type]

    try:
        atomic_write(target, new_text)
    except OSError as exc:
        return _failed(
            ctx, capture_path, target, f"could not write {target}: {exc}", now, backup=backup
        )

    _index_update(ctx, target)
    _log(ctx, OPERATION, capture_path, target, success=True, now=now, backup=backup)
    _record(ctx, capture=capture_state, targets=targets, trace=trace, route=proposal.route, now=now)
    return OperationResult(
        ok=True,
        operation=OPERATION,
        source=str(capture_path),
        destination=str(target),
        backup_path=str(backup) if backup else None,
        details=details,
    )


def _failed(
    ctx: OperationContext,
    source: Path,
    target: Path,
    message: str,
    now: float,
    *,
    backup: Path | None = None,
) -> OperationResult:
    """A WORLD-STATE failure of a validly-addressed op: ``ok=False`` plus a
    FAILED oplog line, never an exception (ARCHITECTURE "Where the error line
    falls"). No ActionRecord — the vault did not change."""
    _log(ctx, OPERATION, source, target, success=False, now=now, error=message, backup=backup)
    return OperationResult(
        ok=False,
        operation=OPERATION,
        source=str(source),
        destination=str(target),
        backup_path=str(backup) if backup else None,
        error=message,
        dry_run=ctx.dry_run,
    )


#: The guard kinds whose fix really IS the manual merge editor: the user was
#: trying to remove or rearrange existing content, and the merge path is
#: guard-free by design. Every OTHER kind (a paraphrase, a fabricated body, an
#: empty-effect edit, an unjustified frontmatter rewrite) is not a
#: "you may not delete" problem, and sending the user to `organize merge`
#: "to delete or rewrite existing content" would name the wrong cause — the
#: 09 §1.5 actionable-diagnosis class ``check_guards`` orders its guards for.
_MERGE_PATH_KINDS: frozenset[str] = frozenset({"deletion", "reorder", "duplication"})


def _guard_error(exc: IntegrationRejected, verdict: str) -> IntegrationRejected:
    """A structural guard fired at COMMIT. On ``edited`` that is Matt's own
    version being refused, so the error says so — and points at the merge
    editor only when the refusal was actually about existing content
    (ARCHITECTURE Phase-5 wire contract)."""
    if verdict != "edited":
        return exc
    kind = getattr(exc, "kind", "guard")
    if kind in _MERGE_PATH_KINDS:
        hint = (
            "integrate never destroys or rearranges, whoever wrote the diff. To delete, "
            "re-order or rewrite existing content, use the manual merge path "
            "(`organize merge`, spec 03 §5), which has no guards. Nothing was written."
        )
    else:
        hint = (
            (exc.hint or "")
            + " The same guards bind an edited proposal as bind the model's (spec 12 §1); "
            "fix the edit and commit again, or reject it."
        ).strip()
    return IntegrationRejected(
        f"your edited version was refused by the integrate guards: {exc}",
        hint=hint,
        kind=kind,
    )


def _same_path(left: Path, right: Path) -> bool:
    return _resolve(left) == _resolve(right)


def _resolve(path: Path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:  # pragma: no cover - resolve() is non-strict
        return Path(path).absolute()


def _refuse_no_ai(path: Path, doc: Document, what: str) -> None:
    """``no-ai`` for integrate is ABSOLUTE — no human exception.

    ``fileops._refuse_no_ai`` exempts ``actor="matt"`` because a move or an
    append is Matt's own keystroke. Integrate is never Matt's keystroke: the
    text is written by an LLM that had to READ the note to write it, and spec
    12 §1 says "``no-ai: true`` targets refuse ``integrate`` outright" with no
    actor qualifier. ARCHITECTURE (Phase-4 routes ruling) settles the lattice
    explicitly: "Integrate refuses for EVERY actor (12 §1)".
    """
    if not is_no_ai(doc):
        return
    raise NoAiRefusal(
        f"{path} carries 'no-ai: true' and integrate refuses for EVERY actor "
        f"(spec 12 §1); refusing to {what} it",
        hint="No actor, human or automated, may run integrate against a no-ai note — the "
        "proposal itself is an LLM reading it. Use the manual merge editor (03 §5), or "
        "remove 'no-ai: true'.",
    )


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------


def _trace(
    proposal: IntegrationProposal,
    *,
    proposed: str,
    final: str,
    verdict: str,
    stale_target: bool = False,
) -> LLMTrace:
    """The doc-12 §2 ``llm`` block. ``proposed_diff`` is always what the model
    produced; ``final_diff`` is what was actually applied (empty when nothing
    was). ``stale_target`` marks the one case where the two cannot be read
    together — see :class:`~organize_core.actions.LLMTrace`."""
    trace = LLMTrace(
        backend=proposal.backend,
        model=proposal.model,
        prompt_hash=proposal.prompt_hash,
        proposed_diff=proposed,
        final_diff=final,
        verdict=verdict,  # type: ignore[arg-type]
        stale_target=stale_target,
    )
    return _with_proposal_id(trace, proposal.proposal_id)


#: SELF-REMOVING SEAM (the approved Phase-4 pattern): the ARCHITECTURE
#: Phase-5 wire contract says ``proposal_id`` is "a CORRELATION id echoed into
#: the ActionRecord", but the doc-12 §2 schema — owned by the actions seat —
#: has no field for it, and smuggling first-class data into ``context.filters``
#: is the exact mistake ``dry_run`` and ``partial_failure`` were promoted OUT
#: of. So: echo it when ``LLMTrace`` grows the field, and never invent a
#: place for it before then. The paired test skips only while the field is
#: absent and activates itself the moment the seam lands.
_TRACE_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(LLMTrace))
TRACE_CARRIES_PROPOSAL_ID = "proposal_id" in _TRACE_FIELDS


def _with_proposal_id(trace: LLMTrace, proposal_id: str) -> LLMTrace:
    if not TRACE_CARRIES_PROPOSAL_ID:  # pragma: no cover - the seam has landed
        return trace
    return dataclasses.replace(trace, proposal_id=proposal_id)


def _record(
    ctx: OperationContext,
    *,
    capture: Any,
    targets: list[Any],
    trace: LLMTrace,
    route: str | None,
    now: float,
) -> None:
    """ONE ActionRecord, through fileops' one record-building path.

    Note that a ``rejected`` verdict records even though the vault did not
    change — a deliberate exception to ``_record_action``'s "operations that
    changed nothing do NOT record", mandated twice: spec 12 §3 ("rejected and
    recorded with ``verdict: "rejected"``, target untouched") and the
    ARCHITECTURE Phase-5 wire contract ("Rejected verdicts COMMIT (record
    written, no vault write — the negative signal is the point)").

    THE TRACE IS LOAD-BEARING ON BOTH READERS, not decoration. It reaches
    ``ctx.recorder`` and ``ctx.on_record`` as ONE object because
    ``_record_action`` builds the record with it (the Phase-5 fileops seam —
    this used to be a recorder facade plus an ``on_record`` wrapper, i.e. two
    places that could disagree). ``learn.is_matt_decided`` reads the verdict
    off exactly this block, so dropping it silently INVERTS the fold rule in
    both directions: a rejection recorded by actor ``matt`` would fold (no
    verdict ⇒ "a HUMAN actor folds"), and every accepted integration by
    ``claude-integrate`` would stop folding (no verdict ⇒ an automated actor
    never folds). Both directions are pinned with firing controls.

    The verdict FILTER is deliberately not applied here.
    ``learn.is_matt_decided`` owns it (ARCHITECTURE, Phase-4 bind+actor batch:
    "integrate records use the verdict-based reading"), and its docstring says
    why the rule lives in the callee: "every composition root can wire
    ``on_record`` unconditionally and the filter cannot be forgotten". A
    second copy here would be a second policy site that can drift from it.
    """
    _record_action(
        ctx,
        op_type=OPERATION,  # type: ignore[arg-type]
        capture=capture,
        targets=targets,
        now=now,
        edit_mode=EDIT_MODE,
        route=route,
        llm=trace,
    )


def _record_rejection(
    ctx: OperationContext,
    *,
    capture_doc: IntegrationDocument,
    target_doc: IntegrationDocument,
    trace: LLMTrace,
    route: str | None,
    description: str | None,
) -> None:
    """The propose-time structural rejection's record (12 §3). Never raises —
    a failure to record must not mask the rejection the caller is about to
    see."""
    try:
        _record(
            ctx,
            capture=_capture_state(
                Path(capture_doc.path), capture_doc.text, capture_doc.doc
            ),
            targets=[
                _target_state(
                    Path(target_doc.path),
                    target_doc.text,
                    target_doc.text,
                    TARGET_ROLE,
                    description=description,
                )
            ],
            trace=trace,
            route=route,
            now=ctx.clock(),
        )
    except Exception:  # noqa: BLE001 - recording never blocks the refusal
        logger.error(
            "ACTION RECORD FAILED for the rejected integrate proposal on %s — the "
            "rejection itself stands (spec 12 §2)",
            target_doc.path,
            exc_info=True,
        )


__all__ = [
    "ACTOR",
    "CONTENT_KEYS",
    "EDIT_MODE",
    "JUSTIFIED_FRONTMATTER_KEYS",
    "OPERATION",
    "PROMPT_TEMPLATE",
    "RATIONALE_KEYS",
    "SYSTEM_PROMPT",
    "TARGET_ROLE",
    "TRACE_CARRIES_PROPOSAL_ID",
    "IntegrationDocument",
    "IntegrationProposal",
    "added_lines",
    "apply",
    "apply_unified_diff",
    "build_prompt",
    "check_guards",
    "deleted_line_count",
    "reordered_line_count",
    "integrate_client",
    "new_proposal_id",
    "normalize_whitespace",
    "prompt_hash",
    "propose",
    "read_document",
    "resolve_description",
]
