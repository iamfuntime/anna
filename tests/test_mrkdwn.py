"""Unit tests for the outbound Slack mrkdwn normalizer.

:func:`anna.transports.mrkdwn.normalize_to_slack_mrkdwn` rewrites
GitHub-flavored Markdown into Slack mrkdwn deterministically at the
transport boundary. These tests pin every documented transform PLUS the
code-protection invariant: nothing inside a fenced block or an inline code
span may be rewritten.
"""

from __future__ import annotations

from anna.transports.mrkdwn import normalize_to_slack_mrkdwn as norm


# ---------------------------------------------------------------------------
# 1. Bold / italic
# ---------------------------------------------------------------------------


def test_bold_double_star() -> None:
    assert norm("a **bold** b") == "a *bold* b"


def test_bold_double_underscore() -> None:
    assert norm("a __bold__ b") == "a *bold* b"


def test_single_underscore_italic_left_as_is() -> None:
    # Slack already treats _x_ as italic — leave it.
    assert norm("a _italic_ b") == "a _italic_ b"


def test_single_star_left_as_is() -> None:
    # Single-asterisk is intentionally NOT converted to italic: Slack renders
    # *x* as bold and ANNA writes Slack bold that way, so leave it untouched.
    assert norm("a *bold* b") == "a *bold* b"


def test_bold_not_double_converted_to_italic() -> None:
    # The classic footgun: **bold** must end as *bold* (Slack bold), NOT
    # _bold_ (which would happen if the italic pass ate the collapsed form).
    assert norm("**bold**") == "*bold*"


def test_arithmetic_asterisk_not_italicized() -> None:
    # ``a * b`` has spaces around the asterisk and must not become italic.
    assert norm("2 * 3 = 6") == "2 * 3 = 6"


# ---------------------------------------------------------------------------
# 2. Headings
# ---------------------------------------------------------------------------


def test_heading_h1() -> None:
    assert norm("# Title") == "*Title*"


def test_heading_h3() -> None:
    assert norm("### Sub heading") == "*Sub heading*"


def test_heading_six_hashes() -> None:
    assert norm("###### Deep") == "*Deep*"


def test_seven_hashes_is_not_a_heading() -> None:
    # 7 hashes exceeds the 1-6 range, so it is left as a plain line.
    assert norm("####### NotAHeading") == "####### NotAHeading"


def test_hash_without_space_is_not_a_heading() -> None:
    assert norm("#hashtag stays") == "#hashtag stays"


# ---------------------------------------------------------------------------
# 3. Bullets
# ---------------------------------------------------------------------------


def test_dash_bullet() -> None:
    assert norm("- item") == "• item"


def test_star_bullet() -> None:
    assert norm("* item") == "• item"


def test_plus_bullet() -> None:
    assert norm("+ item") == "• item"


def test_nested_bullet_preserves_indentation() -> None:
    assert norm("  - nested") == "  • nested"


def test_bullet_content_is_inline_transformed() -> None:
    assert norm("- a **bold** item") == "• a *bold* item"


# ---------------------------------------------------------------------------
# 4. Links / images
# ---------------------------------------------------------------------------


def test_link_conversion() -> None:
    assert norm("see [docs](https://example.com)") == "see <https://example.com|docs>"


def test_image_conversion_with_alt() -> None:
    assert norm("![a cat](https://x/y.png)") == "<https://x/y.png|a cat>"


def test_image_conversion_empty_alt() -> None:
    assert norm("![](https://x/y.png)") == "<https://x/y.png>"


# ---------------------------------------------------------------------------
# 5. Strikethrough
# ---------------------------------------------------------------------------


def test_strikethrough() -> None:
    assert norm("~~gone~~") == "~gone~"


# ---------------------------------------------------------------------------
# 6. Fenced code blocks
# ---------------------------------------------------------------------------


def test_fence_strips_language_hint() -> None:
    src = "```python\nprint(1)\n```"
    assert norm(src) == "```\nprint(1)\n```"


def test_fence_content_is_verbatim() -> None:
    src = "```\nx = 1\ny = 2\n```"
    assert norm(src) == src


# ---------------------------------------------------------------------------
# 7. Pipe tables
# ---------------------------------------------------------------------------


def test_table_drops_separator_and_joins_cells() -> None:
    src = "| a | b |\n|---|---|\n| 1 | 2 |"
    assert norm(src) == "a  ·  b\n1  ·  2"


def test_table_with_alignment_separator() -> None:
    src = "| name | age |\n|:----|:---:|\n| sam | 9 |"
    assert norm(src) == "name  ·  age\nsam  ·  9"


def test_non_table_pipes_left_untouched() -> None:
    # No separator row -> not cleanly a table -> leave it alone.
    src = "a | b | c"
    assert norm(src) == "a | b | c"


# ---------------------------------------------------------------------------
# CRITICAL — code protection
# ---------------------------------------------------------------------------


def test_bold_inside_inline_code_survives() -> None:
    assert norm("use `**x**` literally") == "use `**x**` literally"


def test_bold_inside_fenced_block_survives() -> None:
    src = "```\nthese **stars** stay\n```"
    assert norm(src) == src


def test_bullet_inside_fenced_block_survives() -> None:
    src = "```\n- not a bullet\n```"
    assert norm(src) == src


def test_link_inside_inline_code_survives() -> None:
    assert norm("call `[x](y)` here") == "call `[x](y)` here"


def test_heading_marker_inside_fenced_block_survives() -> None:
    src = "```\n# not a heading\n```"
    assert norm(src) == src


def test_inline_code_with_pipe_not_treated_as_table() -> None:
    assert norm("run `a|b` now") == "run `a|b` now"


# ---------------------------------------------------------------------------
# Realistic mixed-content message
# ---------------------------------------------------------------------------


def test_mixed_content_full_message() -> None:
    src = (
        "# Heading One\n"
        "Some **bold** text and a [link](https://example.com).\n"
        "- first bullet\n"
        "- second **bold** bullet\n"
        "  - nested bullet\n"
        "Here is `inline code` and ~~struck~~.\n"
        "```python\n"
        'def f():\n'
        '    return "**not bold** - not a bullet"\n'
        "```\n"
        "Done."
    )
    expected = (
        "*Heading One*\n"
        "Some *bold* text and a <https://example.com|link>.\n"
        "• first bullet\n"
        "• second *bold* bullet\n"
        "  • nested bullet\n"
        "Here is `inline code` and ~struck~.\n"
        "```\n"
        'def f():\n'
        '    return "**not bold** - not a bullet"\n'
        "```\n"
        "Done."
    )
    assert norm(src) == expected


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_string_unchanged() -> None:
    assert norm("") == ""


def test_plain_text_unchanged() -> None:
    assert norm("just a normal sentence.") == "just a normal sentence."
