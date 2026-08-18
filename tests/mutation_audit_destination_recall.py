"""Mutation audit for spec 21 (ARCHITECTURE.md: mandatory before handback).

Not collected by pytest (the filename does not start with ``test_``): it is a
runner. Each entry breaks ONE guard that landed with spec 21, runs the tests
that are supposed to notice, and restores the file. A guard whose mutation
leaves the suite green is a guard with no test behind it.

    .venv/bin/python tests/mutation_audit_destination_recall.py

Prints one line per mutation and a final ``caught N/N``; exit code 1 if any
mutation survived.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "organize_core"
TESTS = REPO / "tests"

SUGGEST = SRC / "suggest.py"
INDEX = SRC / "index.py"
CONFIG = SRC / "config.py"
CLI = SRC / "cli.py"

RECALL = TESTS / "test_destination_recall.py"
INDEX_TESTS = TESTS / "test_index.py"
CONFIG_TESTS = TESTS / "test_config.py"
CLI_TESTS = TESTS / "test_cli_blackbox.py"
SUGGEST_TESTS = TESTS / "test_suggest.py"
PERF_TESTS = TESTS / "test_suggest_perf.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    path: Path
    old: str
    new: str
    targets: tuple[Path, ...]
    slow: bool = False


MUTATIONS: tuple[Mutation, ...] = (
    # --- index: the walk (21 §2.1) -----------------------------------------
    Mutation(
        "depth prune is off by one",
        INDEX,
        "if limit is not None and depth >= limit:",
        "if limit is not None and depth > limit:",
        (INDEX_TESTS, CLI_TESTS),
    ),
    Mutation(
        "the PARA root itself becomes a candidate",
        INDEX,
        "            if depth >= 1:",
        "            if depth >= 0:",
        (INDEX_TESTS,),
    ),
    Mutation(
        # `.backups` in the comment is what makes this the candidate walk's
        # copy rather than `scan()`'s.
        "dot-directories are walked",
        INDEX,
        'if name.startswith("."):  # .obsidian, .git, .backups …',
        "if False:  # .obsidian, .git, .backups …",
        (INDEX_TESTS,),
    ),
    Mutation(
        # Anchored on the CANDIDATE walk's own comment: `scan()` contains a
        # textually identical block, and a mutation that hit the wrong one
        # would report a survivor that never existed.
        "ignore_patterns stop being honored by the walk",
        INDEX,
        '                if name.startswith("."):  # .obsidian, .git, .backups …\n'
        "                    continue\n"
        "                child_rel = _relative_posix(root_str, str(Path(dirpath) / name))\n"
        "                if is_ignored(child_rel, patterns):\n"
        "                    continue",
        '                if name.startswith("."):  # .obsidian, .git, .backups …\n'
        "                    continue\n"
        "                child_rel = _relative_posix(root_str, str(Path(dirpath) / name))\n"
        "                if False:\n"
        "                    continue",
        (INDEX_TESTS,),
    ),
    Mutation(
        "the archives root becomes a candidate",
        INDEX,
        "        if key in _EXCLUDED_CANDIDATE_PARA_KEYS:\n            return []",
        "        if False:\n            return []",
        (INDEX_TESTS,),
    ),
    Mutation(
        "a depth of 0 or nonsense is accepted",
        INDEX,
        "    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:",
        "    if False:",
        (INDEX_TESTS,),
    ),
    # --- index: the cache and its invalidation (21 §2.4) --------------------
    Mutation(
        "invalidate_folder_cache stops invalidating",
        INDEX,
        "        self._folder_cache.clear()\n        self._candidate_generation += 1",
        "        pass",
        (INDEX_TESTS,),
    ),
    Mutation(
        "a note change no longer moves the candidate generation",
        INDEX,
        "        self._candidate_generation += 1\n        if self.flush_threshold",
        "        if self.flush_threshold",
        (INDEX_TESTS,),
    ),
    Mutation(
        "captures and archives become note candidates",
        INDEX,
        "                if record.para_type in CANDIDATE_NOTE_PARA_TYPES",
        "                if record.para_type or True",
        (INDEX_TESTS,),
    ),
    # --- suggest: note keys (21 §3.1) --------------------------------------
    Mutation(
        "capture_ aliases become note match keys",
        SUGGEST,
        "        if text.startswith(_CAPTURE_ALIAS_PREFIX):\n            continue",
        "        if False:\n            continue",
        (RECALL,),
    ),
    Mutation(
        "timestamp-shaped values become note match keys",
        SUGGEST,
        "        if _is_timestamp_shaped(text):\n            continue",
        "        if False:\n            continue",
        (RECALL,),
    ),
    Mutation(
        "a note's TAGS become match keys (the forbidden eighth signal)",
        SUGGEST,
        "    if record.title:\n        keys.add(frontmatter.normalize_tag(str(record.title)))",
        "    if record.title:\n        keys.add(frontmatter.normalize_tag(str(record.title)))\n"
        "    for tag in record.tags or ():\n        keys.add(frontmatter.normalize_tag(str(tag)))",
        (RECALL,),
    ),
    Mutation(
        "a note candidate keeps its .md in the name",
        SUGGEST,
        'return name[:-3] if name.endswith(".md") else name',
        "return name",
        (RECALL, CLI_TESTS),
    ),
    Mutation(
        "note keys stop going through the shared normalizer",
        SUGGEST,
        "    keys: set[str] = {frontmatter.normalize_tag(_note_stem(record))}",
        "    keys: set[str] = {_note_stem(record).lower()}",
        (RECALL,),
    ),
    # --- suggest: one fire per (token, candidate) (21 §3.1) -----------------
    Mutation(
        "signal #2 iterates the key set instead of testing it once",
        SUGGEST,
        "        if _tag_variant_matches_keys(tag, candidate.normalized_keys, config):\n"
        "            score += weights.normalized_tag_match\n"
        "            reasons.append(f\"Tag '{tag}' ~ folder '{candidate.name}'\")",
        "        for _key in candidate.normalized_keys:\n"
        "            if _tag_variant_matches_keys(tag, frozenset({_key}), config):\n"
        "                score += weights.normalized_tag_match\n"
        "                reasons.append(f\"Tag '{tag}' ~ folder '{candidate.name}'\")",
        (RECALL,),
    ),
    # --- suggest: stopwords (21 §3.4) --------------------------------------
    Mutation(
        "the stopword filter is dropped from signal #4",
        SUGGEST,
        "        if _is_stopword(source, stopwords):\n            continue",
        "        if False:\n            continue",
        (RECALL, CLI_TESTS),
    ),
    Mutation(
        "the stopword filter is dropped from signal #1",
        SUGGEST,
        "        if _is_stopword(tag, stopwords):\n            continue\n        if tag in candidate.match_keys:",
        "        if False:\n            continue\n        if tag in candidate.match_keys:",
        (RECALL, CLI_TESTS),
    ),
    # --- suggest: ranking, collapse, caps (21 §3.2, §3.3) ------------------
    Mutation(
        "kind_rank leaves the sort key",
        SUGGEST,
        "            _KIND_RANK.get(item[0].kind, len(_KIND_RANK)),\n",
        "",
        (RECALL,),
    ),
    Mutation(
        "the same-name collapse swallows FOLDERS too",
        SUGGEST,
        "        if candidate.kind != KIND_NOTE:\n            continue\n        key = (candidate.normalized_name, score)",
        "        key = (candidate.normalized_name, score)",
        (RECALL,),
    ),
    Mutation(
        "the collapse keeps the DEEPEST path",
        SUGGEST,
        "        if current is None or _depth_key(candidate.path) < _depth_key(current.path):",
        "        if current is None or _depth_key(candidate.path) > _depth_key(current.path):",
        (RECALL,),
    ),
    Mutation(
        "the collapse stops counting what it dropped",
        SUGGEST,
        "            suppressed += 1\n            continue",
        "            continue",
        (RECALL, CLI_TESTS),
    ),
    Mutation(
        "max_note_suggestions stops capping",
        SUGGEST,
        "            if notes >= cap:\n                continue",
        "            if False:\n                continue",
        (RECALL,),
    ),
    Mutation(
        "a note becomes a candidate for itself",
        SUGGEST,
        "        if exclude_path is not None and candidate.path == exclude_path:\n            continue",
        "        if False:\n            continue",
        (RECALL,),
    ),
    # --- suggest: the wire (21 §3.3) ---------------------------------------
    Mutation(
        "destination_kind is inferred from a trailing .md",
        SUGGEST,
        "            destination_kind=candidate.kind,",
        '            destination_kind=KIND_NOTE if candidate.path.endswith(".md") else KIND_FOLDER,',
        (RECALL, CLI_TESTS),
    ),
    Mutation(
        "destination_kind is dropped from the CLI payload",
        CLI,
        '        "destination_kind": suggestion.destination_kind,',
        '        "destination_kind": "folder",',
        (CLI_TESTS,),
    ),
    # --- suggest: §3.5's prefilters must stay OUTPUT-PRESERVING ------------
    Mutation(
        "the bigram bound demands one shared bigram too many",
        SUGGEST,
        "    return max(0, max_len - 1 - 2 * max_distance)",
        "    return max(0, max_len - 2 * max_distance)",
        (RECALL,),
    ),
    Mutation(
        "the alias length window is narrowed",
        SUGGEST,
        "    return range(3 * alias_length // 5 + 1, (5 * alias_length - 1) // 3 + 1)",
        "    return range(3 * alias_length // 5 + 1, alias_length + 1)",
        (RECALL,),
    ),
    Mutation(
        "the bigram prefilter is applied to 3-character aliases (the abc/axc hole)",
        SUGGEST,
        "_BIGRAM_PREFILTER_MIN_LENGTH = 4",
        "_BIGRAM_PREFILTER_MIN_LENGTH = 2",
        (RECALL,),
    ),
    Mutation(
        "the edit-distance cutoff is one edit too tight",
        SUGGEST,
        "    distance = _levenshtein(a, b, max_distance=max_distance)",
        "    distance = _levenshtein(a, b, max_distance=max_distance - 1)",
        (RECALL,),
    ),
    Mutation(
        "the character bound is applied where it is not sound",
        SUGGEST,
        "            if max_len - overlap > _max_alias_distance(max_len):",
        "            if max_len - overlap > _max_alias_distance(max_len) // 2:",
        (RECALL,),
    ),
    Mutation(
        "signal #5 loses its prefilter entirely (a COST regression, not a wrong answer)",
        SUGGEST,
        "            picked.update(self._alias_shortlist(alias.lower()))",
        "            picked.update(range(len(self.candidates)))",
        (PERF_TESTS,),
        slow=True,
    ),
    # --- the untouchables (21 §4) ------------------------------------------
    Mutation(
        "the type bonus stops being subtracted before the floor",
        SUGGEST,
        "        signal_score = score - _type_bonus(candidate.type, config)",
        "        signal_score = score",
        (RECALL, SUGGEST_TESTS),
    ),
    Mutation(
        "the zero-signal floor is relaxed to buy recall",
        SUGGEST,
        "        if signal_score <= 0.0 or signal_score < min_confidence:",
        "        if False:",
        (RECALL, SUGGEST_TESTS),
    ),
    Mutation(
        "an eighth weight is read",
        SUGGEST,
        "    # 7. folder-type bonus — always fires, no reason string (spec 04 §2 #7)",
        "    score += getattr(weights, 'note_bonus', 0.0)\n"
        "    # 7. folder-type bonus — always fires, no reason string (spec 04 §2 #7)",
        (RECALL,),
    ),
    # --- config (21 §3.7) ---------------------------------------------------
    Mutation(
        "max_candidate_depth accepts a depth of 0",
        CONFIG,
        "    if value < 1:",
        "    if False:",
        (CONFIG_TESTS,),
    ),
    Mutation(
        "note_candidates is ignored by the CLI",
        CLI,
        "    notes = index.candidate_notes() if config.suggestions.note_candidates else []",
        "    notes = index.candidate_notes()",
        (CLI_TESTS,),
    ),
    Mutation(
        "max_candidate_depth is ignored by the CLI's ballot",
        CLI,
        "        key: [str(p) for p in index.candidate_folders(key)]",
        "        key: [str(p) for p in index.candidate_folders(key, 1)]",
        (CLI_TESTS,),
    ),
)


def run(mutation: Mutation) -> bool:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-x",
        "-q",
        *[str(path) for path in mutation.targets],
    ]
    if mutation.slow:
        command += ["-m", "slow"]
    result = subprocess.run(command, cwd=REPO, capture_output=True, text=True, timeout=900)
    return result.returncode != 0


def main() -> int:
    caught = 0
    for mutation in MUTATIONS:
        original = mutation.path.read_text(encoding="utf-8")
        if mutation.old not in original:
            print(f"SKIP (anchor not found) {mutation.name}")
            continue
        mutation.path.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
        try:
            died = run(mutation)
        finally:
            mutation.path.write_text(original, encoding="utf-8")
        caught += died
        print(f"{'caught ' if died else 'SURVIVED'} {mutation.name}")
    print(f"\ncaught {caught}/{len(MUTATIONS)}")
    return 0 if caught == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
