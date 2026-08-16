"""Ingestion half of the orchestrator: ``runner.scan_notes`` (spec 06 §1).

The regression obligations exercised here:

* **08 §B1 class** — a note whose bytes are not valid UTF-8, or whose
  frontmatter does not parse, is logged and SKIPPED. It must never abort
  the run; strict decoding is what took the live pipeline down for three
  months.
* **08 §B14** — "no scan directory exists" raises EAGERLY (the original
  raise was deferred into a generator and its message had an
  operator-precedence bug, so nobody ever saw it).
* **08 §B18** — a scan dir reached through a symlink must not blow up on
  ``relative_to(vault_root)``; the daily-note exclusion is documented and
  tested rather than folklore.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from organize_core.config import Config, VaultConfig
from organize_core.consumers.runner import LEGACY_DAILY_PATTERN, scan_notes
from organize_core.errors import ConfigError


def _config(root: Path, **vault: object) -> Config:
    return Config(vault=VaultConfig(root=root, **vault))  # type: ignore[arg-type]


def _rels(root: Path, payloads: list) -> set[str]:
    resolved = root.resolve()
    return {p.path.relative_to(resolved).as_posix() for p in payloads}


# --- what is and is not a note ---------------------------------------------


def test_scan_yields_markdown_from_every_scan_dir(fixture_vault: Path) -> None:
    rels = _rels(fixture_vault, list(scan_notes(_config(fixture_vault))))

    assert "capture/raw_capture/2026-07-01T09:00:00.000Z.md" in rels
    assert "projects/blog/ideas.md" in rels
    assert "areas/health/index.md" in rels
    assert "resources/performing/impro.md" in rels


def test_scan_skips_non_markdown_and_dot_dirs(fixture_vault: Path) -> None:
    rels = _rels(fixture_vault, list(scan_notes(_config(fixture_vault))))

    assert not any(r.endswith((".wav", ".txt", ".pdf", ".json")) for r in rels)
    assert not any(r.startswith(".obsidian") for r in rels)


def test_scan_skips_legacy_daily_notes(fixture_vault: Path) -> None:
    """06 §1: ``\\d{4}-\\d{2}-\\d{2}.md`` is excluded — documented, not folklore."""
    rels = _rels(fixture_vault, list(scan_notes(_config(fixture_vault))))

    assert "capture/raw_capture/2026-08-01.md" not in rels
    # ...and the exclusion is exact: a dated name with a suffix is a real note.
    (fixture_vault / "capture/raw_capture/2026-08-02-standup.md").write_text(
        "---\nid: standup\n---\nbody\n", encoding="utf-8"
    )
    rels = _rels(fixture_vault, list(scan_notes(_config(fixture_vault))))
    assert "capture/raw_capture/2026-08-02-standup.md" in rels


def test_legacy_daily_pattern_is_a_full_match() -> None:
    assert LEGACY_DAILY_PATTERN.fullmatch("2026-08-01.md")
    assert not LEGACY_DAILY_PATTERN.fullmatch("2026-08-01-notes.md")
    assert not LEGACY_DAILY_PATTERN.fullmatch("prefix-2026-08-01.md")


def test_scan_honours_ignore_patterns_as_prefix_and_glob(fixture_vault: Path) -> None:
    rels = _rels(fixture_vault, list(scan_notes(_config(fixture_vault))))
    # default ignore_patterns carry the "resources/flashcards" prefix
    assert not any(r.startswith("resources/flashcards") for r in rels)

    (fixture_vault / "resources/performing/draft.tmp.md").write_text("x\n", encoding="utf-8")
    config = _config(fixture_vault, ignore_patterns=["*.tmp.md", "projects"])
    rels = _rels(fixture_vault, list(scan_notes(config)))
    assert "resources/performing/draft.tmp.md" not in rels
    assert not any(r.startswith("projects/") for r in rels)
    assert "resources/performing/impro.md" in rels


def test_scan_skips_oversize_files_loudly(
    fixture_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    big = fixture_vault / "resources/performing/huge.md"
    big.write_text("---\nid: huge\n---\n" + ("x" * 5000), encoding="utf-8")
    config = _config(fixture_vault, max_file_size=1000)

    with caplog.at_level(logging.WARNING):
        rels = _rels(fixture_vault, list(scan_notes(config)))

    assert "resources/performing/huge.md" not in rels
    assert "max_file_size" in caplog.text


# --- the B1 class: bad bytes and bad YAML must not crash a run -------------


def test_invalid_utf8_note_is_scanned_not_fatal(fixture_vault: Path) -> None:
    """08 §B1: strict decoding is the defect class this phase exists to kill."""
    payloads = {p.path.name: p for p in scan_notes(_config(fixture_vault))}

    payload = payloads["invalid-utf8.md"]
    assert "�" in payload.raw_text  # replaced, not raised
    assert payload.frontmatter["id"] == "bad-bytes"


def test_unparseable_frontmatter_is_logged_and_skipped(
    fixture_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        rels = _rels(fixture_vault, list(scan_notes(_config(fixture_vault))))

    assert "capture/raw_capture/broken-yaml.md" not in rels
    assert "broken-yaml.md" in caplog.text
    # the rest of the vault still arrived — one bad file is not a dead run
    assert "capture/raw_capture/scalar-tags.md" in rels


def test_unreadable_file_is_skipped_not_fatal(
    fixture_vault: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_read = Path.read_text
    victim = (fixture_vault / "resources/performing/impro.md").resolve()

    def flaky(self: Path, *args: object, **kwargs: object) -> str:
        if self.resolve() == victim:
            raise PermissionError("nope")
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", flaky)
    with caplog.at_level(logging.WARNING):
        rels = _rels(fixture_vault, list(scan_notes(_config(fixture_vault))))

    assert "resources/performing/impro.md" not in rels
    assert "unreadable" in caplog.text
    assert "projects/blog/ideas.md" in rels


# --- payload shape ---------------------------------------------------------


def test_note_hash_is_sha256_of_the_raw_text(fixture_vault: Path) -> None:
    """The hash is THE idempotency key and must match the live DB's recipe:
    sha256 over the file text decoded utf-8/replace (06 §1)."""
    payloads = {p.path.name: p for p in scan_notes(_config(fixture_vault))}
    payload = payloads["scalar-tags.md"]
    raw = (fixture_vault / "capture/raw_capture/scalar-tags.md").read_text(
        encoding="utf-8", errors="replace"
    )

    assert payload.raw_text == raw
    assert payload.note_hash == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_hash_changes_only_when_the_file_changes(fixture_vault: Path) -> None:
    target = fixture_vault / "capture/raw_capture/scalar-tags.md"
    before = {p.path: p.note_hash for p in scan_notes(_config(fixture_vault))}
    again = {p.path: p.note_hash for p in scan_notes(_config(fixture_vault))}
    assert before == again

    target.write_text(target.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
    after = {p.path: p.note_hash for p in scan_notes(_config(fixture_vault))}
    assert after[target.resolve()] != before[target.resolve()]
    assert {k: v for k, v in after.items() if k != target.resolve()} == {
        k: v for k, v in before.items() if k != target.resolve()
    }


def test_paths_are_absolute_and_resolved(fixture_vault: Path) -> None:
    """06 §1: paths in the DB are ``.resolve()``d absolutes — canonicalize
    identically or a year of history orphans."""
    for payload in scan_notes(_config(fixture_vault)):
        assert payload.path.is_absolute()
        assert payload.path == payload.path.resolve()


def test_frontmatter_and_body_come_from_the_shared_module(fixture_vault: Path) -> None:
    payloads = {p.path.name: p for p in scan_notes(_config(fixture_vault))}

    # '---' inside a value must not be a delimiter (08 §B16)
    dashes = payloads["dashes-in-values.md"]
    assert dashes.frontmatter["title"] == "section --- with dashes"
    assert "horizontal rule" in dashes.content

    # no frontmatter at all is a body-only note, not an error
    plain = payloads["plain note $ with no frontmatter.md"]
    assert plain.frontmatter == {}
    assert plain.content.startswith("Just a plain markdown body")


# --- scan-dir resolution (08 §B14, §B18) -----------------------------------


def test_all_scan_dirs_missing_raises_eagerly(tmp_path: Path) -> None:
    """08 §B14: the raise must happen at CALL time, not on first ``next()``
    — a generator-deferred raise is how this went unnoticed."""
    root = tmp_path / "vault"
    root.mkdir()
    config = _config(root, scan_dirs=["nope", "also-nope"])

    with pytest.raises(ConfigError) as excinfo:
        scan_notes(config)  # not wrapped in list() on purpose

    assert "nope" in str(excinfo.value)
    assert "also-nope" in str(excinfo.value)


def test_one_missing_scan_dir_warns_and_the_rest_still_scan(
    fixture_vault: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _config(fixture_vault, scan_dirs=["capture/raw_capture", "does-not-exist"])
    with caplog.at_level(logging.WARNING):
        rels = _rels(fixture_vault, list(scan_notes(config)))

    assert "does-not-exist" in caplog.text
    assert "capture/raw_capture/scalar-tags.md" in rels


def test_symlinked_scan_dir_does_not_explode(tmp_path: Path, fixture_vault: Path) -> None:
    """08 §B18: a scan dir symlinked out of the vault makes
    ``relative_to(vault_root)`` raise ValueError. Scanning must survive it,
    and the note must still be delivered."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("---\nid: outside\n---\nbody\n", encoding="utf-8")
    link = fixture_vault / "linked"
    link.symlink_to(outside, target_is_directory=True)

    config = _config(fixture_vault, scan_dirs=["capture/raw_capture", "linked"])
    payloads = list(scan_notes(config))
    names = {p.path.name for p in payloads}

    assert "note.md" in names
    assert "scalar-tags.md" in names
    outside_payload = next(p for p in payloads if p.path.name == "note.md")
    assert outside_payload.path == (outside / "note.md").resolve()


def test_nested_symlinks_are_not_followed(tmp_path: Path, fixture_vault: Path) -> None:
    """Symlink loops inside the vault must not hang the walk; the indexer
    makes the same choice (``followlinks=False``)."""
    (fixture_vault / "projects" / "loop").symlink_to(
        fixture_vault / "projects", target_is_directory=True
    )
    payloads = list(scan_notes(_config(fixture_vault)))
    assert len(payloads) == len({p.path for p in payloads})


def test_duplicate_scan_dirs_yield_each_note_once(fixture_vault: Path) -> None:
    config = _config(
        fixture_vault, scan_dirs=["capture/raw_capture", "capture/raw_capture", "projects"]
    )
    paths = [p.path for p in scan_notes(config)]
    assert len(paths) == len(set(paths))


def test_scan_is_deterministic(fixture_vault: Path) -> None:
    first = [p.path for p in scan_notes(_config(fixture_vault))]
    second = [p.path for p in scan_notes(_config(fixture_vault))]
    assert first == second
