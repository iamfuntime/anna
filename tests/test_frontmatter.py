"""Tests for the leading-YAML-frontmatter splitter (subtask 3)."""

from __future__ import annotations

import pytest

from anna.runtime.frontmatter import split_frontmatter


def test_round_trips_a_general_md_style_fence() -> None:
    """A standard ---fence--- is peeled; body + meta come back correct."""
    text = (
        "---\n"
        "description: A general agent\n"
        "grants:\n"
        "  mcp_servers: [playwright]\n"
        "---\n"
        "# General\nDo the thing.\n"
    )
    body, meta = split_frontmatter(text)
    assert body == "# General\nDo the thing.\n"
    assert meta == {
        "description": "A general agent",
        "grants": {"mcp_servers": ["playwright"]},
    }


def test_no_fence_returns_body_unchanged() -> None:
    """A body that opens with a heading, not a fence, is returned verbatim."""
    text = "# Heading\nSome body text.\n"
    body, meta = split_frontmatter(text)
    assert body == text
    assert meta == {}


def test_malformed_yaml_returns_original_and_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed YAML degrades to (original, {}) and logs a WARNING.

    structlog renders to stdout via anna's configured handler, so the
    assertion uses ``capsys`` rather than ``caplog`` (the structlog ->
    stdlib bridge bypasses pytest's caplog hook).
    """
    text = "---\nfoo: [unclosed\n---\n# Body\n"
    body, meta = split_frontmatter(text)
    assert body == text
    assert meta == {}
    captured = capsys.readouterr()
    assert "frontmatter.parse.malformed" in (captured.out + captured.err)


def test_empty_fence_yields_empty_meta() -> None:
    """An empty ---\\n--- fence yields ({} meta, stripped body)."""
    body, meta = split_frontmatter("---\n---\n# Body\n")
    assert body == "# Body\n"
    assert meta == {}


def test_unterminated_fence_is_not_treated_as_frontmatter() -> None:
    """An opening fence with no closing fence is left as a plain body."""
    text = "---\ndescription: x\nbody with no close\n"
    body, meta = split_frontmatter(text)
    assert body == text
    assert meta == {}


def test_horizontal_rule_is_not_a_fence() -> None:
    """A leading '-----' rule (not exactly '---') is not a fence."""
    text = "-----\nnot a fence\n"
    body, meta = split_frontmatter(text)
    assert body == text
    assert meta == {}


def test_scalar_yaml_is_not_a_mapping() -> None:
    """A fence whose YAML parses to a scalar yields {} meta, stripped body."""
    body, meta = split_frontmatter("---\njust a string\n---\n# Body\n")
    assert body == "# Body\n"
    assert meta == {}
