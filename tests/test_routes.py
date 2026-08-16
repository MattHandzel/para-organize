"""Route resolution, route/scored merging, and NL descriptions (spec 11 §1/§3).

Phase-1 scope: :func:`routes.resolve`, :meth:`RouteMatch.as_suggestion`,
:func:`routes.merge_route_suggestions`, :func:`routes.get_description`.
``apply_route``/``apply_all``/``set_description`` are Phase 4 and are only
asserted to fail loudly.

Every assertion is an EXACT value — the old suite's ``> 0`` assertions are
what let the 08-known-issues defects survive (09 §3 testing bar).

No filesystem, no real vault: routes are pure over config + tags, and
``get_description`` reads the warm index through ``.get()`` only, so a stub
index stands in for the mid-build ``VaultIndex``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from organize_core import routes
from organize_core.config import (
    Config,
    RouteConfig,
    SuggestionsConfig,
    VaultConfig,
    validate_config,
)
from organize_core.index import NoteRecord
from organize_core.routes import (
    ARCHIVE_SUGGESTION_TYPE,
    ROUTE_SUGGESTION_SCORE,
    RouteMatch,
    get_description,
    merge_route_suggestions,
    resolve,
)
from organize_core.suggest import Suggestion

VAULT_ROOT = Path("/vault")

# The doc-11 §1 example, verbatim.
SPEC_11_ROUTES_TOML = """
[vault]
root = "/vault"

[[routes]]
tags = ["workout", "training"]
destination = "areas/health/training-log.md"
mode = "append"
description = \"\"\"My running/lifting training log. New entries are appended
chronologically under a date heading. Short workout notes, PRs, and how
sessions felt belong here — not general health research.\"\"\"

[[routes]]
tags = ["blog-idea"]
destination = "projects/blog/ideas.md"
mode = "append"
description = "One-line blog post ideas, appended as list items under ## Inbox."

[[routes]]
tags = ["impro", "theatre"]
destination = "resources/performing/"
mode = "move"
description = "Notes about improvisation, theatre, and performance practice."
"""

SPEC_11_TRAINING_DESCRIPTION = (
    "My running/lifting training log. New entries are appended\n"
    "chronologically under a date heading. Short workout notes, PRs, and how\n"
    "sessions felt belong here — not general health research."
)


@pytest.fixture()
def spec_config() -> Config:
    return validate_config(tomllib.loads(SPEC_11_ROUTES_TOML))


def make_config(
    *route_configs: RouteConfig,
    tag_normalization: dict[str, str] | None = None,
    descriptions: dict[str, str] | None = None,
) -> Config:
    return Config(
        vault=VaultConfig(root=VAULT_ROOT),
        suggestions=SuggestionsConfig(tag_normalization=dict(tag_normalization or {})),
        routes=list(route_configs),
        descriptions=dict(descriptions or {}),
    )


def route(
    tags: list[str],
    destination: str,
    mode: str = "append",
    description: str = "",
    auto: bool = False,
) -> RouteConfig:
    return RouteConfig(
        tags=tags,
        destination=destination,
        mode=mode,  # type: ignore[arg-type]
        description=description,
        auto=auto,
    )


class StubIndex:
    """Stand-in for the mid-build ``VaultIndex``.

    ``get_description`` is specified to read descriptions the index already
    loaded (11 §3), so ``.get()`` is the entire surface it needs. Recording
    lookups also proves the documented precedence order.
    """

    def __init__(self, records: dict[str, NoteRecord] | None = None) -> None:
        self._records = dict(records or {})
        self.lookups: list[str] = []

    def get(self, path: Path | str) -> NoteRecord | None:
        key = str(path)
        self.lookups.append(key)
        return self._records.get(key)


def described_note(path: str, description: str | None) -> NoteRecord:
    p = Path(path)
    return NoteRecord(
        path=path,
        filename=p.name,
        title=p.stem,
        para_type="area",
        folder=p.parent.name,
        description=description,
    )


def folder_suggestion(name: str, score: float, para_type: str = "areas") -> Suggestion:
    return Suggestion(
        path=f"/vault/{para_type}/{name}",
        name=name,
        type=para_type,
        score=score,
        reasons=(f"Tag '{name}' matches folder",),
    )


def archive_suggestion() -> Suggestion:
    """Byte-identical to what ``suggest.suggest`` appends (spec 04 §1)."""
    return Suggestion(
        path="/vault/archive/capture/raw_capture",
        name="Archive Now",
        type=ARCHIVE_SUGGESTION_TYPE,
        score=0.1,
        reasons=("Safe default option",),
    )


# --------------------------------------------------------------------------
# resolve — spec 11 §1
# --------------------------------------------------------------------------


def test_spec_example_single_tag_resolves_one_route(spec_config: Config) -> None:
    matches = resolve(["workout"], spec_config)

    assert len(matches) == 1
    match = matches[0]
    assert match.route_name == "workout"
    assert match.destination == Path("/vault/areas/health/training-log.md")
    assert match.is_folder is False
    assert match.route.mode == "append"
    assert match.route.description == SPEC_11_TRAINING_DESCRIPTION


def test_tags_list_is_any_of(spec_config: Config) -> None:
    """``tags = ["workout", "training"]`` — either tag alone matches."""
    by_first = resolve(["workout"], spec_config)
    by_second = resolve(["training"], spec_config)

    assert [m.destination for m in by_first] == [Path("/vault/areas/health/training-log.md")]
    assert [m.destination for m in by_second] == [Path("/vault/areas/health/training-log.md")]
    # Both tags present is still ONE match, not two.
    assert len(resolve(["workout", "training"], spec_config)) == 1


def test_plus_expression_requires_every_tag() -> None:
    config = make_config(route(["deep-work+monday"], "areas/focus/log.md"))

    assert resolve(["deep-work"], config) == []
    assert resolve(["monday"], config) == []
    assert len(resolve(["deep-work", "monday"], config)) == 1
    assert len(resolve(["monday", "deep-work", "unrelated"], config)) == 1


def test_plus_expression_mixed_with_any_of() -> None:
    """``["a+b", "c"]`` means ``(a AND b) OR c`` (spec 11 §1)."""
    config = make_config(route(["a+b", "c"], "areas/x/log.md"))

    assert resolve(["a"], config) == []
    assert len(resolve(["a", "b"], config)) == 1
    assert len(resolve(["c"], config)) == 1


def test_plus_expression_tolerates_spaces_around_the_separator() -> None:
    config = make_config(route(["deep work + monday"], "areas/focus/log.md"))

    assert len(resolve(["deep-work", "monday"], config)) == 1


def test_multi_route_capture_matches_every_route_in_config_order(
    spec_config: Config,
) -> None:
    """Spec 11 §1: a capture may match multiple routes; order = config order."""
    matches = resolve(["impro", "blog-idea", "workout"], spec_config)

    assert [m.route_name for m in matches] == ["workout", "blog-idea", "impro"]
    assert [str(m.destination) for m in matches] == [
        "/vault/areas/health/training-log.md",
        "/vault/projects/blog/ideas.md",
        "/vault/resources/performing",
    ]


def test_config_order_is_preserved_not_sorted() -> None:
    """Config order, not alphabetical — the order routes apply in (11 §1)."""
    config = make_config(
        route(["zulu"], "areas/z/log.md"),
        route(["alpha"], "areas/a/log.md"),
    )

    assert [m.route_name for m in resolve(["alpha", "zulu"], config)] == ["zulu", "alpha"]


def test_two_routes_to_the_same_destination_both_match() -> None:
    """Deduping here would silently drop a route Matt configured."""
    config = make_config(
        route(["one"], "areas/x/log.md"),
        route(["two"], "areas/x/log.md"),
    )

    matches = resolve(["one", "two"], config)
    assert [m.route_name for m in matches] == ["one", "two"]


def test_no_tags_and_no_matching_tags_resolve_to_nothing(spec_config: Config) -> None:
    assert resolve([], spec_config) == []
    assert resolve(["", "   "], spec_config) == []
    assert resolve(["nothing-here"], spec_config) == []


def test_tag_comparison_uses_the_shared_normalizer() -> None:
    """Case, spaces and underscores fold the same way as suggestion scoring."""
    config = make_config(route(["Blog_Idea"], "projects/blog/ideas.md"))

    assert len(resolve(["blog idea"], config)) == 1
    assert len(resolve(["BLOG-IDEA"], config)) == 1
    assert len(resolve(["Blog_Idea"], config)) == 1


def test_configured_tag_normalization_map_applies_to_routes() -> None:
    """``suggestions.tag_normalization`` is honored on BOTH sides (04 §1)."""
    config = make_config(
        route(["workout"], "areas/health/training-log.md"),
        tag_normalization={"lifting": "workout", "gym": "workout"},
    )

    assert len(resolve(["lifting"], config)) == 1
    assert len(resolve(["gym"], config)) == 1
    assert resolve(["cardio"], config) == []


def test_folder_destination_is_flagged_and_loses_its_trailing_slash(
    spec_config: Config,
) -> None:
    (match,) = resolve(["theatre"], spec_config)

    assert match.is_folder is True
    assert match.destination == Path("/vault/resources/performing")
    assert match.route.mode == "move"


def test_resolve_touches_no_filesystem(spec_config: Config) -> None:
    """Pure over config + tags: a vault root that does not exist still
    resolves (the destination file certainly does not exist either)."""
    assert not VAULT_ROOT.exists()
    (match,) = resolve(["workout"], spec_config)
    assert match.destination == Path("/vault/areas/health/training-log.md")


def test_auto_flag_is_carried_through_untouched() -> None:
    """``auto`` decides unattended handling (11 §1) — resolve must not
    filter on it; the tag_router consumer does that in Phase 4."""
    config = make_config(
        route(["hands-off"], "areas/x/log.md", auto=True),
        route(["ui-only"], "areas/y/log.md", auto=False),
    )

    matches = resolve(["hands-off", "ui-only"], config)
    assert [m.route.auto for m in matches] == [True, False]


# --------------------------------------------------------------------------
# RouteMatch.as_suggestion — spec 11 §1 "Where routes act"
# --------------------------------------------------------------------------


def test_as_suggestion_renders_the_spec_example(spec_config: Config) -> None:
    """``[→] training-log.md (route: workout)`` with the description."""
    (match,) = resolve(["workout"], spec_config)
    suggestion = match.as_suggestion()

    assert suggestion.path == "/vault/areas/health/training-log.md"
    assert suggestion.name == "training-log.md"
    assert suggestion.type == "area"
    assert suggestion.score == ROUTE_SUGGESTION_SCORE
    assert suggestion.route == "workout"
    assert suggestion.reasons == ("Route 'workout' (append)",)
    assert suggestion.description == SPEC_11_TRAINING_DESCRIPTION


def test_as_suggestion_keeps_the_full_file_path_not_the_parent_folder(
    spec_config: Config,
) -> None:
    """Doc 08 §36 regression (file-vs-folder suggestion paths): a file route
    suggests the FILE, a folder route suggests the FOLDER."""
    (file_match,) = resolve(["blog-idea"], spec_config)
    (folder_match,) = resolve(["impro"], spec_config)

    assert file_match.as_suggestion().path == "/vault/projects/blog/ideas.md"
    assert file_match.as_suggestion().name == "ideas.md"
    assert folder_match.as_suggestion().path == "/vault/resources/performing"
    assert folder_match.as_suggestion().name == "performing"
    assert folder_match.as_suggestion().type == "resource"


def test_as_suggestion_without_a_description_reports_none() -> None:
    config = make_config(route(["plain"], "projects/x/notes.md", description="   "))
    (match,) = resolve(["plain"], config)

    assert match.as_suggestion().description is None


def test_as_suggestion_maps_singular_archive_folder_to_plural_type() -> None:
    """Matt's vault folder is ``archive`` SINGULAR (spec 02 / 08 §C2) while
    the suggestion type vocabulary is ``archives``."""
    config = make_config(route(["done"], "archive/notes.md"))
    (match,) = resolve(["done"], config)

    assert match.as_suggestion().type == ARCHIVE_SUGGESTION_TYPE


def test_as_suggestion_reports_other_for_a_non_para_destination() -> None:
    """No PARA root ⇒ say ``other`` rather than claim a type."""
    config = make_config(route(["daily"], "dailies/log.md"))
    (match,) = resolve(["daily"], config)

    assert match.as_suggestion().type == "dailies"

    rootless = RouteMatch(
        route=route(["x"], "notes.md"),
        route_name="x",
        destination=Path("/vault/notes.md"),
        is_folder=False,
    )
    assert rootless.as_suggestion().type == "notes.md"


def test_route_display_name_is_the_first_tag(spec_config: Config) -> None:
    (match,) = resolve(["training"], spec_config)

    # Matched on "training", displayed as the route's first tag (11 §1).
    assert match.route_name == "workout"


# --------------------------------------------------------------------------
# merge_route_suggestions — spec 11 §1 + ARCHITECTURE resolution #10
# --------------------------------------------------------------------------


def test_routes_rank_above_every_scored_suggestion(spec_config: Config) -> None:
    matches = resolve(["workout", "blog-idea"], spec_config)
    scored = [
        folder_suggestion("health", 4.5),
        folder_suggestion("blog", 3.2),
        folder_suggestion("kms", 2.1),
        folder_suggestion("relationships", 1.4),
    ]

    merged = merge_route_suggestions(matches, scored)

    assert [item.route for item in merged] == ["workout", "blog-idea", None, None]
    assert [item.name for item in merged[2:]] == ["health", "blog"]
    assert merged[0].score == ROUTE_SUGGESTION_SCORE
    assert merged[2].score == 4.5


def test_cap_truncates_scored_entries_only_and_keeps_the_archive_entry(
    spec_config: Config,
) -> None:
    """Resolution #10: the incoming list length IS the UI cap; routes and the
    archive entry survive, scored entries are what give way."""
    matches = resolve(["workout", "blog-idea"], spec_config)
    scored = [folder_suggestion(f"f{i}", 9.0 - i) for i in range(9)]
    scored.append(archive_suggestion())
    assert len(scored) == 10

    merged = merge_route_suggestions(matches, scored)

    assert len(merged) == 10
    assert [item.route for item in merged[:2]] == ["workout", "blog-idea"]
    assert [item.name for item in merged[2:9]] == ["f0", "f1", "f2", "f3", "f4", "f5", "f6"]
    assert merged[-1].name == "Archive Now"
    assert merged[-1].type == ARCHIVE_SUGGESTION_TYPE


def test_more_routes_than_the_cap_never_drops_a_route_or_the_archive() -> None:
    config = make_config(*(route([f"t{i}"], f"areas/a{i}/log.md") for i in range(12)))
    matches = resolve([f"t{i}" for i in range(12)], config)
    scored = [folder_suggestion(f"f{i}", 5.0 - i) for i in range(4)]
    scored.append(archive_suggestion())

    merged = merge_route_suggestions(matches, scored)

    assert len(merged) == 13
    assert [item.route for item in merged[:12]] == [f"t{i}" for i in range(12)]
    assert merged[-1].name == "Archive Now"


def test_no_routes_returns_the_scored_list_unchanged() -> None:
    scored = [
        folder_suggestion("health", 4.5),
        folder_suggestion("blog", 3.2),
        archive_suggestion(),
    ]

    assert merge_route_suggestions([], scored) == scored


def test_routes_survive_an_empty_scored_list(spec_config: Config) -> None:
    """No candidate cleared ``min_confidence`` — the route is still offered."""
    matches = resolve(["workout"], spec_config)

    merged = merge_route_suggestions(matches, [])

    assert len(merged) == 1
    assert merged[0].route == "workout"


def test_empty_inputs_merge_to_an_empty_list() -> None:
    assert merge_route_suggestions([], []) == []


def test_a_scored_duplicate_of_a_route_destination_is_dropped() -> None:
    """A ``move`` route's folder can also be a scored candidate; showing the
    same destination twice is a UI bug. The freed slot goes to the next
    scored entry, so the list length is unchanged."""
    config = make_config(route(["impro", "theatre"], "resources/performing/", mode="move"))
    matches = resolve(["impro"], config)
    scored = [
        folder_suggestion("performing", 6.0, para_type="resources"),
        folder_suggestion("health", 4.5),
        folder_suggestion("blog", 3.2),
        archive_suggestion(),
    ]

    merged = merge_route_suggestions(matches, scored)

    assert len(merged) == 4
    assert [item.name for item in merged] == ["performing", "health", "blog", "Archive Now"]
    assert merged[0].route == "impro"
    assert [item.path for item in merged].count("/vault/resources/performing") == 1


def test_merge_does_not_mutate_its_inputs(spec_config: Config) -> None:
    matches = resolve(["workout"], spec_config)
    scored = [folder_suggestion("health", 4.5), archive_suggestion()]
    scored_snapshot = list(scored)

    merge_route_suggestions(matches, scored)

    assert scored == scored_snapshot
    assert len(matches) == 1


# --------------------------------------------------------------------------
# get_description — spec 11 §3
# --------------------------------------------------------------------------


def test_folder_index_note_description_is_used() -> None:
    config = make_config()
    index = StubIndex(
        {
            "/vault/areas/health/index.md": described_note(
                "/vault/areas/health/index.md",
                "Ongoing health practice — training log, sleep, injuries.",
            )
        }
    )

    assert get_description(Path("/vault/areas/health"), index, config) == (
        "Ongoing health practice — training log, sleep, injuries."
    )


def test_folder_named_note_is_the_second_lookup() -> None:
    """``<folder>.md`` is the Obsidian folder-note convention (11 §3)."""
    config = make_config()
    index = StubIndex(
        {
            "/vault/areas/health/health.md": described_note(
                "/vault/areas/health/health.md", "Health area."
            )
        }
    )

    assert get_description(Path("/vault/areas/health"), index, config) == "Health area."
    assert index.lookups == [
        "/vault/areas/health",
        "/vault/areas/health/index.md",
        "/vault/areas/health/health.md",
    ]


def test_a_files_own_description_is_used() -> None:
    """"Any PARA folder OR FILE may carry a description" (11 §3)."""
    config = make_config()
    index = StubIndex(
        {
            "/vault/areas/health/training-log.md": described_note(
                "/vault/areas/health/training-log.md", "The training log itself."
            )
        }
    )

    assert (
        get_description(Path("/vault/areas/health/training-log.md"), index, config)
        == "The training log itself."
    )


def test_config_table_is_the_fallback() -> None:
    config = make_config(
        descriptions={"areas/health": "Health, training, sleep and recovery."}
    )

    assert get_description(Path("/vault/areas/health"), StubIndex(), config) == (
        "Health, training, sleep and recovery."
    )


def test_index_note_wins_over_the_config_table() -> None:
    """Spec 11 §3 lookup ORDER: index-note frontmatter, ELSE the config table."""
    config = make_config(descriptions={"areas/health": "from config"})
    index = StubIndex(
        {"/vault/areas/health/index.md": described_note("/vault/areas/health/index.md", "from note")}
    )

    assert get_description(Path("/vault/areas/health"), index, config) == "from note"


def test_blank_index_description_falls_through_to_the_config_table() -> None:
    config = make_config(descriptions={"areas/health": "from config"})
    index = StubIndex(
        {"/vault/areas/health/index.md": described_note("/vault/areas/health/index.md", "   ")}
    )

    assert get_description(Path("/vault/areas/health"), index, config) == "from config"


def test_config_key_may_carry_a_trailing_slash() -> None:
    config = make_config(descriptions={"areas/health/": "trailing slash key"})

    assert get_description(Path("/vault/areas/health"), StubIndex(), config) == (
        "trailing slash key"
    )


def test_absolute_config_key_is_accepted() -> None:
    config = make_config(descriptions={"/vault/areas/health": "absolute key"})

    assert get_description(Path("/vault/areas/health"), StubIndex(), config) == "absolute key"


def test_description_is_stripped() -> None:
    config = make_config(descriptions={"areas/health": "  padded  \n"})

    assert get_description(Path("/vault/areas/health"), StubIndex(), config) == "padded"


def test_no_description_anywhere_is_none() -> None:
    config = make_config(descriptions={"projects/blog": "elsewhere"})

    assert get_description(Path("/vault/areas/health"), StubIndex(), config) is None


def test_path_outside_the_vault_still_checks_the_absolute_key() -> None:
    config = make_config(descriptions={"/elsewhere/notes": "outside the vault"})

    assert get_description(Path("/elsewhere/notes"), StubIndex(), config) == "outside the vault"


def test_get_description_touches_no_filesystem() -> None:
    """It reads the warm index, never the disk — none of these paths exist."""
    config = make_config(descriptions={"areas/health": "from config"})
    index = StubIndex()

    assert not Path("/vault/areas/health").exists()
    assert get_description(Path("/vault/areas/health"), index, config) == "from config"
    assert index.lookups == [
        "/vault/areas/health",
        "/vault/areas/health/index.md",
        "/vault/areas/health/health.md",
    ]


# --------------------------------------------------------------------------
# Phase-4 surface — must fail loudly, not silently no-op
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("apply_route", (None, None, None)),
        ("apply_all", (None, None, [])),
        ("set_description", (None, Path("/vault/areas/health"), "text")),
    ],
)
def test_phase_four_entry_points_raise_a_clear_not_implemented(
    name: str, args: tuple[object, ...]
) -> None:
    with pytest.raises(NotImplementedError) as excinfo:
        getattr(routes, name)(*args)

    message = str(excinfo.value)
    assert name in message
    assert "Phase 4" in message


# ---------------------------------------------------------------------------
# Suggestion.type for route entries (integrator seam ruling)
# ---------------------------------------------------------------------------
#
# `as_suggestion()` takes no Config, so it used to guess the PARA type from
# the destination's leading path segment. That is right for a default vault
# and wrong for one that renames a PARA root — the route entry would be typed
# with the raw folder name while every SCORED suggestion for the same folder
# was typed with the config KEY, and the UI groups on that field. `resolve()`
# does have the Config, so it now fills `RouteMatch.para_type`.


def test_route_suggestion_type_uses_the_configured_para_folder_names() -> None:
    config = Config(
        vault=VaultConfig(
            root=VAULT_ROOT,
            para_folders={
                "projects": "p",  # renamed roots
                "areas": "a",
                "resources": "r",
                "archives": "archive",
            },
        ),
        routes=[route(["workout"], "a/health/training-log.md", mode="append")],
    )
    (match,) = resolve(["workout"], config)
    assert match.para_type == "area"
    assert match.as_suggestion().type == "area"


def test_route_suggestion_type_still_works_for_the_default_layout() -> None:
    config = make_config(route(["workout"], "areas/health/training-log.md"))
    (match,) = resolve(["workout"], config)
    # The ROOT is plural on disk (`areas/`); the type VALUE is singular.
    assert match.para_type == "area"
    assert match.as_suggestion().type == "area"


def test_singular_archive_on_disk_is_reported_as_the_singular_type() -> None:
    """spec 02 / 08 §C2: the folder is `archive` and so is the type value.
    Both the configured and the fallback path must agree."""
    config = make_config(route(["done"], "archive/reference/notes.md"))
    (match,) = resolve(["done"], config)
    assert match.para_type == "archive"
    assert match.as_suggestion().type == "archive"


def test_a_hand_built_route_match_still_derives_its_type() -> None:
    """`para_type` defaults to empty, so a RouteMatch built without resolve()
    (tests, future callers) falls back to the leading-segment reading rather
    than reporting a blank type."""
    match = RouteMatch(
        route=route(["x"], "resources/performing/notes.md"),
        route_name="x",
        destination=VAULT_ROOT / "resources/performing/notes.md",
        is_folder=False,
    )
    assert match.para_type == ""
    assert match.as_suggestion().type == "resource"
