"""``[[routes]]`` validation (spec 11 §1).

The load-time contract: folder destinations (trailing ``/``) take
``mode = "move"``; file destinations take ``append``/``integrate``; violations
are a :class:`RouteConfigError` naming the offending route index and key.
``description`` is load-bearing natural language (UI, auto-tagger, doc 13) and
``auto`` opts a route into unattended handling — both must survive validation
byte-for-byte.
"""

from __future__ import annotations

import tomllib
from typing import Any

import pytest

from organize_core.config import RouteConfig, validate_config
from organize_core.errors import ConfigError, RouteConfigError

SPEC_EXAMPLE = """
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


def raw_with(*routes: dict[str, Any]) -> dict[str, Any]:
    return {"vault": {"root": "/vault"}, "routes": list(routes)}


def test_spec_11_example_validates_verbatim() -> None:
    config = validate_config(tomllib.loads(SPEC_EXAMPLE))
    assert len(config.routes) == 3

    training, blog, impro = config.routes
    assert training.tags == ["workout", "training"]
    assert training.destination == "areas/health/training-log.md"
    assert training.mode == "append"
    assert training.auto is False
    assert training.template is None
    assert training.description.startswith("My running/lifting training log.")
    assert training.description.endswith("not general health research.")

    assert blog.tags == ["blog-idea"]
    assert blog.mode == "append"

    assert impro.destination == "resources/performing/"
    assert impro.mode == "move"


def test_no_routes_section_means_no_routes() -> None:
    assert validate_config({"vault": {"root": "/vault"}}).routes == []


def test_route_defaults() -> None:
    config = validate_config(raw_with({"tags": ["x"], "destination": "a/b.md", "mode": "append"}))
    assert config.routes[0] == RouteConfig(
        tags=["x"], destination="a/b.md", mode="append", description="", template=None, auto=False
    )


def test_auto_and_template_are_honored() -> None:
    config = validate_config(
        raw_with(
            {
                "tags": ["x"],
                "destination": "a/b.md",
                "mode": "append",
                "template": "## {date} — from {capture_id}",
                "auto": True,
            }
        )
    )
    assert config.routes[0].auto is True
    assert config.routes[0].template == "## {date} — from {capture_id}"


def test_folder_destination_with_append_is_rejected() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(raw_with({"tags": ["x"], "destination": "resources/perf/", "mode": "append"}))
    assert "config key 'routes[0].mode'" in str(excinfo.value)
    assert "folders get \"move\"" in str(excinfo.value)
    assert isinstance(excinfo.value, ConfigError)


def test_folder_destination_with_integrate_is_rejected() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(
            raw_with({"tags": ["x"], "destination": "resources/perf/", "mode": "integrate"})
        )
    assert "config key 'routes[0].mode'" in str(excinfo.value)


def test_file_destination_with_move_is_rejected() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(raw_with({"tags": ["x"], "destination": "a/b.md", "mode": "move"}))
    assert "config key 'routes[0].mode'" in str(excinfo.value)
    assert '"append" or "integrate"' in str(excinfo.value)
    assert 'trailing "/"' in (excinfo.value.hint or "")


def test_integrate_mode_is_accepted_for_files() -> None:
    config = validate_config(raw_with({"tags": ["x"], "destination": "a/b.md", "mode": "integrate"}))
    assert config.routes[0].mode == "integrate"


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(raw_with({"tags": ["x"], "destination": "a/b.md", "mode": "weave"}))
    assert "config key 'routes[0].mode'" in str(excinfo.value)
    assert "must be one of ['append', 'integrate', 'move']" in str(excinfo.value)


@pytest.mark.parametrize("field", ["tags", "destination", "mode"])
def test_required_route_keys(field: str) -> None:
    entry = {"tags": ["x"], "destination": "a/b.md", "mode": "append"}
    del entry[field]
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(raw_with(entry))
    assert f"config key 'routes[0].{field}' is required" in str(excinfo.value)


def test_empty_tags_array_is_rejected() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(raw_with({"tags": [], "destination": "a/b.md", "mode": "append"}))
    assert "config key 'routes[0].tags'" in str(excinfo.value)


def test_and_syntax_is_accepted_and_preserved() -> None:
    config = validate_config(
        raw_with({"tags": ["workout+pr", "training"], "destination": "a/b.md", "mode": "append"})
    )
    assert config.routes[0].tags == ["workout+pr", "training"]


@pytest.mark.parametrize("expression", ["a+", "+b", "a++b", "+"])
def test_unparseable_tag_expression_is_rejected(expression: str) -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(raw_with({"tags": [expression], "destination": "a/b.md", "mode": "append"}))
    assert "config key 'routes[0].tags[0]'" in str(excinfo.value)
    assert "tag expression" in str(excinfo.value)


def test_absolute_destination_is_rejected() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(
            raw_with({"tags": ["x"], "destination": "/etc/passwd", "mode": "integrate"})
        )
    assert "config key 'routes[0].destination'" in str(excinfo.value)
    assert "relative to the vault root" in str(excinfo.value)


def test_escaping_destination_is_rejected() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(raw_with({"tags": ["x"], "destination": "../outside.md", "mode": "append"}))
    assert "must stay inside the vault" in str(excinfo.value)


def test_unknown_route_key_is_a_route_error_naming_the_index() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(
            raw_with({"tags": ["x"], "destination": "a/b.md", "mode": "append", "review": "auto"})
        )
    assert "unknown config key 'routes[0].review'" in str(excinfo.value)


def test_second_route_errors_are_indexed_correctly() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(
            raw_with(
                {"tags": ["x"], "destination": "a/b.md", "mode": "append"},
                {"tags": ["y"], "destination": "c/", "mode": "append"},
            )
        )
    assert "config key 'routes[1].mode'" in str(excinfo.value)


def test_routes_must_be_an_array_of_tables() -> None:
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config({"vault": {"root": "/vault"}, "routes": {"workout": {}}})
    assert "config key 'routes'" in str(excinfo.value)
    assert "array of tables" in str(excinfo.value)


def test_description_may_be_empty_but_must_be_a_string() -> None:
    assert (
        validate_config(
            raw_with({"tags": ["x"], "destination": "a/b.md", "mode": "append", "description": ""})
        ).routes[0].description
        == ""
    )
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(
            raw_with({"tags": ["x"], "destination": "a/b.md", "mode": "append", "description": 3})
        )
    assert "config key 'routes[0].description'" in str(excinfo.value)
