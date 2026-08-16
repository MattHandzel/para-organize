"""`extract_json` against the messy shapes real models actually emit
(spec 06 §3.1: whole-string JSON else the outermost ``{…}`` block).

This is the parse rule the Taskwarrior enricher (06 §3.1), the auto-tagger
(11 §2) and every ``json_mode`` caller depend on. The old pipeline's
hand-rolled variants are the three ad-hoc LLM code paths 09 §2 removes.
"""

from __future__ import annotations

import pytest

from organize_core.llm import extract_json

# --- clean input -----------------------------------------------------------


def test_whole_string_object() -> None:
    assert extract_json('{"next_action": "email bob", "utility": 8}') == {
        "next_action": "email bob",
        "utility": 8,
    }


def test_surrounding_whitespace_and_newlines() -> None:
    assert extract_json('\n\n  {"a": 1}  \n\t') == {"a": 1}


def test_nested_objects_and_arrays_are_preserved() -> None:
    text = '{"tags": ["a", "b"], "meta": {"score": 0.75, "deep": {"x": null}}}'
    assert extract_json(text) == {
        "tags": ["a", "b"],
        "meta": {"score": 0.75, "deep": {"x": None}},
    }


# --- fenced output ---------------------------------------------------------


def test_json_fenced_block() -> None:
    text = '```json\n{"effort": "1.5h", "priority": "H"}\n```'
    assert extract_json(text) == {"effort": "1.5h", "priority": "H"}


def test_bare_fenced_block() -> None:
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_fence_with_prose_before_and_after() -> None:
    text = (
        "Sure! Here is the enrichment you asked for:\n\n"
        '```json\n{"utility": 13, "effort": "30 min"}\n```\n\n'
        "Let me know if you want it adjusted."
    )
    assert extract_json(text) == {"utility": 13, "effort": "30 min"}


def test_fence_with_language_tag_variants() -> None:
    for fence in ("```JSON", "```json5", "```jsonc", "```  "):
        assert extract_json(f'{fence}\n{{"a": 1}}\n```') == {"a": 1}


def test_indented_fenced_block() -> None:
    text = 'Result:\n\n```json\n  {\n    "a": 1\n  }\n```\n'
    assert extract_json(text) == {"a": 1}


# --- prefixed / suffixed prose --------------------------------------------


def test_prose_preamble_without_a_fence() -> None:
    text = 'Here is the JSON object:\n{"next_action": "call the dentist"}'
    assert extract_json(text) == {"next_action": "call the dentist"}


def test_prose_epilogue_containing_a_stray_brace() -> None:
    text = '{"a": 1}\n\nNote: the closing brace } above ends the object.'
    assert extract_json(text) == {"a": 1}


def test_prose_preamble_containing_a_decoy_brace() -> None:
    """A `{` in prose must not shadow the real object."""
    text = 'Use {curly} braces like this:\n{"a": 1, "b": 2}'
    assert extract_json(text) == {"a": 1, "b": 2}


def test_chat_wrapper_with_thinking_preamble() -> None:
    text = (
        "<thinking>The user wants {something} structured.</thinking>\n"
        'Final answer: {"tags": ["improv"], "auto_tag": "done"}\n'
        "Hope that helps!"
    )
    assert extract_json(text) == {"tags": ["improv"], "auto_tag": "done"}


# --- braces inside string values ------------------------------------------


def test_braces_inside_string_values_do_not_break_matching() -> None:
    text = 'Answer:\n{"template": "Captured from {id}", "note": "a } here"}'
    assert extract_json(text) == {"template": "Captured from {id}", "note": "a } here"}


def test_escaped_quotes_inside_string_values() -> None:
    text = r'{"quote": "he said \"hi\" then {left}", "n": 1}'
    assert extract_json(text) == {"quote": 'he said "hi" then {left}', "n": 1}


def test_trailing_backslash_before_closing_quote() -> None:
    text = r'{"path": "C:\\notes\\", "ok": true}'
    assert extract_json(text) == {"path": "C:\\notes\\", "ok": True}


def test_unicode_survives() -> None:
    assert extract_json('{"title": "café ☕ — naïve"}') == {"title": "café ☕ — naïve"}


# --- outermost-first preference -------------------------------------------


def test_outermost_object_wins_over_a_nested_one() -> None:
    result = extract_json('{"outer": {"inner": 1}}')
    assert result == {"outer": {"inner": 1}}
    assert "inner" not in result


def test_first_valid_object_wins_when_several_are_present() -> None:
    text = '{"first": 1}\nand also\n{"second": 2}'
    assert extract_json(text) == {"first": 1}


def test_falls_back_to_a_later_object_when_the_first_is_broken() -> None:
    text = '{not really json,}\nbut then: {"good": true}'
    assert extract_json(text) == {"good": True}


def test_unterminated_object_before_a_valid_one() -> None:
    text = '{"broken": [1, 2\n\n{"good": 1}'
    assert extract_json(text) == {"good": 1}


# --- nothing parses --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   \n\t ",
        "I could not determine an answer.",
        "{",
        "}",
        "{{{",
        '{"unterminated": "value',
        '{"trailing": 1,}',
        "{'single': 'quotes'}",
    ],
)
def test_returns_none_when_nothing_parses(text: str) -> None:
    assert extract_json(text) is None


@pytest.mark.parametrize(
    "text",
    ['["a", "b"]', "42", '"just a string"', "true", "null", "```json\n[1, 2, 3]\n```"],
)
def test_non_object_json_is_none(text: str) -> None:
    """Every caller wants a mapping; an array or scalar is not usable."""
    assert extract_json(text) is None


def test_array_wrapper_falls_through_to_an_inner_object() -> None:
    """A top-level array is rejected, but an object inside it still parses."""
    assert extract_json('[{"a": 1}]') == {"a": 1}


def test_pathological_input_terminates() -> None:
    """A brace storm must not turn parsing into unbounded work."""
    assert extract_json("{" * 5000) is None
    assert extract_json("{}" * 5000) == {}
