"""Shared fixtures: the synthetic PARA fixture vault (spec 02 quirks).

ALL tests run against fixture vaults built here — never the real vault
(spec 09 §1.4) — and against CorePaths constructed from tmp dirs — never
real state (paths.py contract).

The vault exhibits EVERY real-world quirk in spec 02 so the frontmatter
round-trip property test (05 §9) and indexer tolerance tests (03 §7) have
their corpus from day one. ``QUIRK_FILES`` names each quirk's path
(vault-relative) so tests can target them individually.

SHARED FILE — only the architect/integrator edits this module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from organize_core.paths import CorePaths

# vault-relative path of each quirk exemplar (spec 02 "quirks" list)
QUIRK_FILES: dict[str, str] = {
    "current_schema": "capture/raw_capture/2026-06-10T21:33:05.379Z.md",
    "scalar_tags": "capture/raw_capture/scalar-tags.md",
    "metadata_empty_map": "capture/raw_capture/metadata-map.md",
    "metadata_empty_list": "capture/raw_capture/metadata-list.md",
    "iso_filename": "capture/raw_capture/2026-04-08T16:51:24.690160+00:00.md",
    "no_ai": "capture/raw_capture/private-thought.md",
    "dashes_in_values": "capture/raw_capture/dashes-in-values.md",
    "unicode_spaces": "capture/raw_capture/meeting notes — café ☕.md",
    "broken_yaml": "capture/raw_capture/broken-yaml.md",
    "no_frontmatter": "capture/raw_capture/plain note $ with no frontmatter.md",
    "invalid_utf8": "capture/raw_capture/invalid-utf8.md",
    "context_as_string": "capture/raw_capture/context-string.md",
    "daily_note": "capture/raw_capture/2026-08-01.md",
    "sync_conflict": "capture/raw_capture/scalar-tags.sync-conflict-20260701-123456-ABCDEF.md",
    "todo_capture": "capture/raw_capture/2026-07-01T09:00:00.000Z.md",
    "question_capture": "capture/raw_capture/2026-07-02T10:00:00.000Z.md",
    "merge_target": "projects/blog/ideas.md",
    "described_folder_index": "areas/health/index.md",
}

# non-markdown files interleaved in raw_capture (02: listing must not choke)
NON_MARKDOWN_FILES: tuple[str, ...] = (
    "capture/raw_capture/voice-memo.wav",
    "capture/raw_capture/clipboard-dump.txt",
    "capture/raw_capture/paper.pdf",
)

PARA_DIRS: tuple[str, ...] = (
    "projects/blog",
    "projects/kms",
    "areas/health",
    "areas/relationships",
    "resources/performing",
    "resources/answers",
    "resources/flashcards/review",
    "archive/capture/raw_capture",  # archive SINGULAR on disk (spec 02)
    "capture/raw_capture/media",
    "dailies",
    ".obsidian",
)


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def build_fixture_vault(root: Path) -> Path:
    """Create the synthetic PARA vault under ``root`` and return it."""
    for d in PARA_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)

    # Current capture-app schema, full field set incl. nested location
    # (spec 02 "Current format").
    _write(
        root,
        QUIRK_FILES["current_schema"],
        """---
timestamp: '2026-06-10T21:37:42.809743+00:00'
id: '2026-06-10T21:33:05.379Z'
aliases:
- '2026-06-10T21:33:05.379Z'
capture_id: '2026-06-10T21:33:05.379Z'
modalities:
- text
context: []
sources:
- me
tags:
- impro
- creativity
location:
  latitude: 40.7126
  longitude: -74.0066
  city: New York
  country: United States
  timezone: America/New_York
metadata: {}
processing_status: raw
created_date: '2026-06-10'
last_edited_date: '2026-06-10'
---
## Content
An idea about improv warmups and creative flow.
""",
    )

    # Scalar tags + title field (older/manual generation).
    _write(
        root,
        QUIRK_FILES["scalar_tags"],
        """---
title: Older manual note
tags: daily_notes
sources: me
processing_status: raw
---
Body of an older note with scalar tags and sources.
""",
    )

    # metadata: {} vs metadata: [] (both occur in the wild).
    _write(
        root,
        QUIRK_FILES["metadata_empty_map"],
        "---\nid: meta-map\ntags:\n- todo\nmetadata: {}\nprocessing_status: raw\n---\nmap-style metadata\n",
    )
    _write(
        root,
        QUIRK_FILES["metadata_empty_list"],
        "---\nid: meta-list\ntags:\n- todo\nmetadata: []\nprocessing_status: raw\n---\nlist-style metadata\n",
    )

    # ISO-timestamp filename containing ':' and '+' (spec 02 quirk).
    _write(
        root,
        QUIRK_FILES["iso_filename"],
        """---
timestamp: '2026-04-08T16:51:24.690160+00:00'
id: '2026-04-08T16:51:24.690160+00:00'
tags:
- health
processing_status: raw
---
Filename is a raw ISO timestamp with colon and plus characters.
""",
    )

    # no-ai: true — the vault law file (spec 02); unknown field must survive
    # every rewrite (03 §8).
    _write(
        root,
        QUIRK_FILES["no_ai"],
        """---
id: private-thought
no-ai: true
tags:
- journal
processing_status: raw
---
Automated tooling must never write to this note.
""",
    )

    # '---' inside YAML values AND as a body horizontal rule (08 §B16).
    _write(
        root,
        QUIRK_FILES["dashes_in_values"],
        """---
id: dashes
title: 'section --- with dashes'
context: 'before --- after'
tags:
- quirk
processing_status: raw
---
Body starts here.

---

Text after a horizontal rule that a substring split would eat.
""",
    )

    # Unicode + spaces in the path (spec 02 quirk).
    _write(
        root,
        QUIRK_FILES["unicode_spaces"],
        """---
id: unicode-cafe
tags:
- meeting
sources:
- me
processing_status: raw
---
Notes from the café — “smart quotes” included.
""",
    )

    # Broken YAML in a well-delimited block (indexer: log + empty metadata).
    _write(
        root,
        QUIRK_FILES["broken_yaml"],
        "---\ntags: [unclosed\n  bad: : :\n---\nbody survives parser failure\n",
    )

    # No frontmatter at all; '$' in the filename.
    _write(
        root,
        QUIRK_FILES["no_frontmatter"],
        "Just a plain markdown body, no frontmatter block.\n",
    )

    # context as a string, not a list (spec 02 quirk).
    _write(
        root,
        QUIRK_FILES["context_as_string"],
        "---\nid: ctx-string\ncontext: at the gym\ntags:\n- workout\nprocessing_status: raw\n---\nleg day PR\n",
    )

    # Legacy daily note living in raw_capture — pipeline SKIPS it (06 §1).
    _write(
        root,
        QUIRK_FILES["daily_note"],
        "---\ntags: daily_notes\n---\ndaily note body\n",
    )

    # Syncthing conflict duplicate (spec 02: expect *.sync-conflict-*).
    _write(
        root,
        QUIRK_FILES["sync_conflict"],
        "---\ntitle: Older manual note\ntags: daily_notes\n---\nconflict copy\n",
    )

    # todo-tagged capture (taskwarrior consumer trigger, 06 §3.1).
    _write(
        root,
        QUIRK_FILES["todo_capture"],
        """---
timestamp: '2026-07-01T09:00:00.000000+00:00'
id: '2026-07-01T09:00:00.000Z'
tags:
- todo
- 'project:blog'
sources:
- me
processing_status: raw
---
Draft the post about learning systems.
""",
    )

    # question-tagged capture (question_answer trigger, 06 §3.3).
    _write(
        root,
        QUIRK_FILES["question_capture"],
        """---
timestamp: '2026-07-02T10:00:00.000000+00:00'
id: '2026-07-02T10:00:00.000Z'
tags:
- question
processing_status: raw
---
Why do spaced repetition intervals grow geometrically?
""",
    )

    # Invalid UTF-8 bytes (06 §6 errors="replace" tolerance) — written raw.
    bad = root / QUIRK_FILES["invalid_utf8"]
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"---\nid: bad-bytes\ntags:\n- quirk\n---\nbroken \xff\xfe bytes inline\n")

    # Non-markdown files interleaved (02: only *.md processed, no choking).
    for rel in NON_MARKDOWN_FILES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x00\x01binary-ish payload")
    (root / "capture/raw_capture/media/clip.wav").write_bytes(b"RIFFfake")

    # Destination notes: merge target with unknown fields to byte-preserve
    # (05 §9) and aliases for the [F] browse label (03 §3).
    _write(
        root,
        QUIRK_FILES["merge_target"],
        """---
title: Blog ideas
aliases:
- ideas
author: Matt Handzel
tags:
- blog-idea
created_date: '2025-11-02'
---
# Blog ideas

## Inbox
- existing idea one
""",
    )

    # Folder with an NL description in its index note (spec 11 §3).
    _write(
        root,
        QUIRK_FILES["described_folder_index"],
        """---
title: Health
description: Ongoing health practice — training log, sleep, injuries. Not general health research (that goes to resources).
tags:
- area
---
# Health
""",
    )

    _write(root, "resources/performing/impro.md", "---\ntags:\n- impro\n---\nimprov resources\n")
    _write(root, "dailies/2026-08-14.md", "---\ntags: daily_notes\n---\nexcluded from organize\n")
    _write(root, ".obsidian/app.json", "{}\n")
    _write(root, "kms-system-rules.md", "Any note with `no-ai: true` must never be written to by AI tooling.\n")

    return root


@pytest.fixture()
def fixture_vault(tmp_path: Path) -> Path:
    """A fresh quirk-complete PARA vault."""
    return build_fixture_vault(tmp_path / "vault")


@pytest.fixture()
def core_paths(tmp_path: Path) -> CorePaths:
    """Isolated CorePaths — no test may construct one from the real env."""
    return CorePaths(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
    )
