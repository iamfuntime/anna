"""Outbound Slack mrkdwn normalizer.

ANNA's model frequently emits GitHub-flavored Markdown (``**bold**``,
``## headings``, ``- bullets``, ``[text](url)`` links, ```` ```lang ````
fenced code, and pipe tables) that Slack does NOT render — Slack speaks its
own *mrkdwn* dialect, so GFM shows up as literal characters. Prompting the
model not to emit GFM has failed repeatedly, so we fix it deterministically
at the transport: :func:`normalize_to_slack_mrkdwn` runs over every outbound
Slack message immediately before it posts.

The single public entry point is :func:`normalize_to_slack_mrkdwn`. It is a
pure function (no I/O, no config) so it is trivially unit-testable and safe
to call on the hot send path.

The hard constraint is **do not corrupt code**: the prose transforms (bold,
italic, headings, bullets, links, strikethrough, tables) must NOT fire
inside fenced code blocks (```` ``` ````...```` ``` ````) or inline code
spans (`` `...` ``). The text is segmented into code / non-code regions
first; only the non-code regions are transformed. Inside a fenced block the
only edit is stripping the language hint off the opening fence (Slack
ignores it and renders it as part of the code otherwise); the fenced content
itself passes through byte-for-byte.
"""

from __future__ import annotations

import re

# Opening / closing fence: three-or-more backticks, optionally indented,
# with an optional language hint trailing the opener.
_FENCE_RE = re.compile(r"^(\s*)(`{3,})(.*)$")

# A heading line: 1-6 leading hashes, whitespace, then the heading text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# A bullet line: optional indentation, a -, *, or + marker, whitespace,
# then the (possibly empty) item text. The indentation is captured so
# nested lists keep their depth.
_BULLET_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")

# Inline code span: a run delimited by single backticks. Used to split a
# prose line into code / non-code pieces so the prose transforms skip the
# code. ``re.split`` with this capturing group keeps the spans in the
# result at the odd indices.
_INLINE_CODE_RE = re.compile(r"(`[^`]*`)")

# Markdown image: ``![alt](url)``. Must run before the link transform
# because a link pattern is a substring of the image pattern.
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")

# Markdown link: ``[text](url)``.
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

# Strikethrough: ``~~x~~`` -> ``~x~``.
_STRIKE_RE = re.compile(r"~~([^~]+)~~")

# NOTE: single-asterisk ``*x*`` is intentionally NOT converted to Slack
# italic (``_x_``). In Slack mrkdwn ``*x*`` already renders as *bold*, and
# ANNA is instructed to write Slack bold as ``*x*`` — so rewriting it to
# italic would flip every intended bold into italic. We leave lone ``*x*``
# untouched and only normalize the unambiguous GFM bold forms below. The
# only cost is a genuine GFM italic rendering as bold, which is harmless.

# Bold: ``**x**`` and ``__x__`` -> ``*x*`` (Slack bold).
_BOLD_STAR_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_\n]+?)__")

# Table cell separator in the rendered output.
_CELL_SEP = "  ·  "


def normalize_to_slack_mrkdwn(text: str) -> str:
    """Rewrite GitHub-flavored Markdown into Slack mrkdwn.

    Pure function. Segments ``text`` into fenced-code and prose regions
    (line-based), strips the language hint off each opening fence, leaves
    fenced content verbatim, and applies the prose transforms only to the
    non-code regions. Inline code spans inside prose are likewise left
    untouched. An empty / falsy input is returned unchanged.
    """
    if not text:
        return text

    lines = text.split("\n")
    out: list[str] = []
    prose_buffer: list[str] = []
    in_fence = False

    def _flush_prose() -> None:
        if prose_buffer:
            transformed = _transform_prose("\n".join(prose_buffer))
            out.extend(transformed.split("\n"))
            prose_buffer.clear()

    for line in lines:
        fence = _FENCE_RE.match(line)
        if fence:
            if not in_fence:
                # Opening fence: flush any pending prose, then emit the
                # fence with the language hint stripped (indent + backticks
                # only). The fenced body that follows is verbatim.
                _flush_prose()
                out.append(f"{fence.group(1)}{fence.group(2)}")
                in_fence = True
            else:
                # Closing fence: emit verbatim and leave the fence.
                out.append(line)
                in_fence = False
            continue
        if in_fence:
            # Inside a fenced block: byte-for-byte passthrough.
            out.append(line)
        else:
            prose_buffer.append(line)

    _flush_prose()
    return "\n".join(out)


def _transform_prose(segment: str) -> str:
    """Apply the prose transforms to a non-code segment.

    Tables are collapsed first (they span multiple lines), then each
    resulting line gets the per-line structural + inline transforms.
    """
    lines = _transform_tables(segment.split("\n"))
    return "\n".join(_transform_line(line) for line in lines)


def _transform_line(line: str) -> str:
    """Transform a single prose line: heading, bullet, or plain inline."""
    heading = _HEADING_RE.match(line)
    if heading:
        return _format_heading(heading.group(2))

    bullet = _BULLET_RE.match(line)
    if bullet:
        indent, _marker, rest = bullet.group(1), bullet.group(2), bullet.group(3)
        return f"{indent}• {_apply_inline(rest)}"

    return _apply_inline(line)


def _format_heading(content: str) -> str:
    """Render a heading as bold text.

    The whole heading becomes Slack bold (``*...*``), so any inner bold
    markers are redundant and stripped to avoid stray asterisks; links and
    strikethrough are still converted so a heading carrying a link renders.
    """
    inner = _IMAGE_RE.sub(_image_repl, content)
    inner = _LINK_RE.sub(_link_repl, inner)
    inner = _STRIKE_RE.sub(r"~\1~", inner)
    inner = inner.replace("**", "").replace("__", "")
    inner = inner.strip()
    return f"*{inner}*" if inner else ""


def _apply_inline(text: str) -> str:
    """Apply inline transforms to ``text``, protecting inline code spans.

    The text is split on backtick-delimited spans; only the non-code pieces
    (even indices) are transformed. Inline code passes through unchanged so
    Slack still renders it.
    """
    parts = _INLINE_CODE_RE.split(text)
    for idx in range(0, len(parts), 2):
        parts[idx] = _inline_prose(parts[idx])
    return "".join(parts)


def _inline_prose(s: str) -> str:
    """Run the inline prose substitutions on a code-free string.

    Order is load-bearing: images before links (the link pattern is a
    substring of the image pattern). Single-asterisk italic is intentionally
    not converted (see the note by the bold regexes).
    """
    s = _IMAGE_RE.sub(_image_repl, s)
    s = _LINK_RE.sub(_link_repl, s)
    s = _STRIKE_RE.sub(r"~\1~", s)
    s = _BOLD_STAR_RE.sub(r"*\1*", s)
    s = _BOLD_UNDER_RE.sub(r"*\1*", s)
    return s


def _image_repl(m: re.Match[str]) -> str:
    alt, url = m.group(1).strip(), m.group(2)
    return f"<{url}|{alt}>" if alt else f"<{url}>"


def _link_repl(m: re.Match[str]) -> str:
    text, url = m.group(1).strip(), m.group(2)
    return f"<{url}|{text}>" if text else f"<{url}>"


def _is_table_separator(row: str) -> bool:
    """True if ``row`` is a GFM table separator (e.g. ``|---|:--:|``).

    A separator contains only pipes, dashes, colons, and spaces, and has at
    least one dash — that dash is what distinguishes it from an ordinary
    (all-text) row.
    """
    s = row.strip()
    if "-" not in s:
        return False
    return all(ch in "|-: " for ch in s)


def _split_table_cells(row: str) -> list[str]:
    """Split a table row into stripped cell values, dropping outer pipes."""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [cell.strip() for cell in s.split("|")]


def _transform_tables(lines: list[str]) -> list[str]:
    """Collapse pipe-table blocks into ``·``-joined rows.

    A table block is a maximal run of consecutive non-blank lines that all
    contain a ``|`` AND include at least one separator row. The separator
    row is dropped; every other row is rendered as its cells joined by
    ``  ·  `` with the outer pipes stripped. A run that does not contain a
    separator row (so it is not cleanly a table) is left untouched.
    """
    result: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if "|" in line and line.strip():
            j = i
            block: list[str] = []
            while j < n and "|" in lines[j] and lines[j].strip():
                block.append(lines[j])
                j += 1
            sep_idx = next(
                (k for k, row in enumerate(block) if _is_table_separator(row)),
                None,
            )
            if len(block) >= 2 and sep_idx is not None:
                for k, row in enumerate(block):
                    if k == sep_idx:
                        continue
                    result.append(_CELL_SEP.join(_split_table_cells(row)))
            else:
                result.extend(block)
            i = j
        else:
            result.append(line)
            i += 1
    return result
