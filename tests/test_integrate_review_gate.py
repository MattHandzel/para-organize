"""The doc-12 §1 review gate: ONE resolution, asked the same way by the config
layer and by the runtime (Phase-5 verify-fix).

Two shipped bugs met here, and they were mirror images:

* the config layer refused ``auto = true`` by looking at the ROUTE's ``review``
  while the runtime ORed in ``[integrate] review`` — so ``[integrate] review =
  "auto"`` plus a route ``auto = true`` was refused at load with a message that
  was factually false, and the shipped global key could not be combined with
  the one shape it exists for;
* the runtime's OR-of-``auto`` let a global ``[integrate] review = "auto"``
  silently remove the human gate from a route that spelled ``review = "diff"``
  on purpose — the difference between "Claude asks first" and "Claude writes
  silently".

The fix is that there is now exactly one place the answer is computed
(``config._validate_route``) and one place it is read back
(``routes.effective_review``). These tests assert the two AGREE, at every
combination, because a disagreement is invisible when each side is pinned
alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from organize_core import routes as routes_mod
from organize_core.config import Config, validate_config
from organize_core.errors import RouteConfigError

TAG = "kms"
DESTINATION = "projects/kms/design-notes.md"


def build(
    *, global_review: str | None = None, route_review: str | None = None, auto: bool = False
) -> Config:
    entry: dict[str, Any] = {
        "tags": [TAG],
        "destination": DESTINATION,
        "mode": "integrate",
    }
    if route_review is not None:
        entry["review"] = route_review
    if auto:
        entry["auto"] = True
    raw: dict[str, Any] = {"vault": {"root": "/vault"}, "routes": [entry]}
    if global_review is not None:
        raw["integrate"] = {"review": global_review}
    return validate_config(raw, source="<test>")


def gate(config: Config) -> str:
    (match,) = routes_mod.resolve([TAG], config)
    return routes_mod.effective_review(match, config)


# ---------------------------------------------------------------------------
# P5-5 — a route that states its gate KEEPS it
# ---------------------------------------------------------------------------


def test_a_global_auto_does_not_ungate_a_route_that_spelled_review_diff() -> None:
    """Spec 12 §1 calls ``review = "auto"`` a "(per-route opt-in)" and the
    shipped example calls ``review`` "the per-route gate". A global default
    that could silently un-gate a deliberately gated route is the worst
    failure this key has — and it was the shipped behaviour."""
    config = build(global_review="auto", route_review="diff")

    assert config.routes[0].review == "diff"
    assert gate(config) == "diff"


def test_firing_control_a_route_that_states_auto_keeps_auto_under_a_global_diff() -> None:
    """The route is authoritative in BOTH directions — otherwise the test
    above would pass against a rule that simply ignored the route."""
    config = build(global_review="diff", route_review="auto")

    assert config.routes[0].review == "auto"
    assert gate(config) == "auto"


def test_the_global_supplies_the_default_for_a_route_that_says_nothing() -> None:
    """That is what writing a GLOBAL key means — and it is the only way the
    global has any effect, which is why it is worth having."""
    assert gate(build(global_review="auto")) == "auto"
    assert gate(build(global_review="diff")) == "diff"
    assert gate(build()) == "diff", "12 §1's default gate when nobody says anything"


def test_the_loaded_route_carries_the_gate_actually_in_force() -> None:
    """`RouteConfig.review` is RESOLVED at load, so anything reading it — the
    config gate, `effective_review`, `organize routes resolve` — sees one
    answer. `None` means "nobody stated one" and survives only on a hand-built
    route that never went through validation."""
    assert build(global_review="auto").routes[0].review == "auto"
    assert build(global_review="auto", route_review="diff").routes[0].review == "diff"


# ---------------------------------------------------------------------------
# P5-4 — the config gate judges the SAME resolution
# ---------------------------------------------------------------------------


def test_a_global_auto_makes_an_unattended_integrate_route_loadable() -> None:
    """The exact shape the Phase-5 ruling says the global key exists for, and
    the exact shape that was refused at load with a message claiming
    ``review = 'diff'`` while the runtime resolved ``auto``."""
    config = build(global_review="auto", auto=True)

    assert config.routes[0].auto is True
    assert gate(config) == "auto", "and it really does route unattended"


def test_firing_control_the_same_route_without_the_global_is_still_refused() -> None:
    """The Phase-4 rationale survives: `auto = true` with an effective gate of
    "diff" is a standing instruction to integrate unattended that contradicts
    its own "ask a human first" gate, and would raise once per matching note."""
    with pytest.raises(RouteConfigError) as excinfo:
        build(auto=True)

    message = str(excinfo.value)
    assert "config key 'routes[0].auto'" in message
    assert "review = 'diff'" in message
    assert "inherited from [integrate] review" in message, "and it says where that came from"


def test_a_route_that_re_gates_itself_under_a_global_auto_is_refused() -> None:
    """The two layers agree in the awkward direction too: route `diff` beats
    global `auto`, so `auto = true` on that route is the contradictory config
    the gate exists to refuse — and the message names the ROUTE's value."""
    with pytest.raises(RouteConfigError) as excinfo:
        build(global_review="auto", route_review="diff", auto=True)

    assert "review = 'diff'" in str(excinfo.value)
    assert "inherited" not in str(excinfo.value), "this one was stated, not inherited"


@pytest.mark.parametrize(
    ("global_review", "route_review"),
    [
        (None, None),
        (None, "diff"),
        (None, "auto"),
        ("diff", None),
        ("diff", "diff"),
        ("diff", "auto"),
        ("auto", None),
        ("auto", "diff"),
        ("auto", "auto"),
    ],
)
def test_the_config_gate_and_the_runtime_gate_never_disagree(
    global_review: str | None, route_review: str | None
) -> None:
    """THE INVARIANT, over every combination: ``auto = true`` loads if and only
    if ``routes.effective_review`` will return ``"auto"`` for that route.

    Pinned as a biconditional rather than as two separate expectations,
    because that is precisely what the two independently-correct-looking
    implementations violated.
    """
    resolved = gate(build(global_review=global_review, route_review=route_review))

    try:
        build(global_review=global_review, route_review=route_review, auto=True)
        loaded = True
    except RouteConfigError:
        loaded = False

    assert loaded is (resolved == "auto"), (
        f"global={global_review!r} route={route_review!r}: the config gate "
        f"{'accepted' if loaded else 'refused'} auto=true while the runtime gate "
        f"resolves {resolved!r}"
    )


# ---------------------------------------------------------------------------
# the neighbouring key must not become collateral damage
# ---------------------------------------------------------------------------


def test_a_global_auto_does_not_turn_every_append_route_into_a_config_error() -> None:
    """`review` on a non-integrate route is a dead key (03 §1 / 08 §A35) and is
    refused — but on the STATED value only. Resolving the global into an
    append route and then refusing it would make `[integrate] review = "auto"`
    unusable in any vault that also has an append route."""
    config = validate_config(
        {
            "vault": {"root": "/vault"},
            "integrate": {"review": "auto"},
            "routes": [
                {"tags": ["x"], "destination": "areas/health/log.md", "mode": "append"},
                {"tags": [TAG], "destination": DESTINATION, "mode": "integrate"},
            ],
        },
        source="<test>",
    )

    assert [r.mode for r in config.routes] == ["append", "integrate"]
    assert gate(config) == "auto"


def test_review_auto_on_an_append_route_is_still_a_loud_error() -> None:
    """FIRING CONTROL for the test above: STATING it there is still refused."""
    with pytest.raises(RouteConfigError) as excinfo:
        validate_config(
            {
                "vault": {"root": "/vault"},
                "integrate": {"review": "auto"},
                "routes": [
                    {
                        "tags": ["x"],
                        "destination": "areas/health/log.md",
                        "mode": "append",
                        "review": "auto",
                    }
                ],
            },
            source="<test>",
        )
    assert "integrate results only" in str(excinfo.value)
