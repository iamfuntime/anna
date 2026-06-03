"""Leading YAML-frontmatter splitter for persona / skill markdown.

A persona file may open with a fenced YAML block:

```
---
description: ...
grants:
  mcp_servers: [playwright]
---
# Persona body starts here
```

:func:`split_frontmatter` peels that fence off, parses the YAML into a
dict, and returns ``(body_without_fence, meta)``. It is intentionally a
separate, dependency-light module (only ``yaml`` + the project logger) so
it can be unit-tested in isolation and reused by both the persona loader
and any future skill-frontmatter consumer.

Failure is always soft: no fence, a malformed fence, or YAML that does not
parse to a mapping all degrade to ``(original_text, {})`` so a bad header
can never crash a delegation. Malformed YAML additionally logs a WARNING.
"""

from __future__ import annotations

from typing import Any

import yaml

from anna.log import get_logger

_log = get_logger("anna.frontmatter")


def split_frontmatter(text: str) -> tuple[str, dict[str, Any]]:
    """Split a leading ``---``-fenced YAML block off ``text``.

    Args:
        text: Raw file contents.

    Returns:
        A ``(body, meta)`` tuple. ``body`` is ``text`` with the fence
        removed (and the single newline after the closing fence consumed);
        ``meta`` is the parsed YAML mapping. When there is no leading
        fence, or the fence is unterminated, or the YAML is malformed or
        does not parse to a mapping, returns ``(text, {})``. Malformed
        YAML is logged at WARNING; the other no-fence / non-mapping cases
        are silent (they are normal, not errors).
    """
    # A fence must be the very first line: "---" alone on line 1.
    if not text.startswith("---"):
        return text, {}
    # Guard the "---" immediately followed by more dashes / text on the
    # same line (e.g. a horizontal rule "-----" or "--- foo"). The opening
    # fence line must be exactly "---".
    first_nl = text.find("\n")
    if first_nl == -1:
        return text, {}
    if text[:first_nl].rstrip() != "---":
        return text, {}

    # Find the closing fence: a line that is exactly "---" after the open.
    rest = text[first_nl + 1 :]
    close = _find_closing_fence(rest)
    if close is None:
        # Unterminated fence — treat as a no-fence body.
        return text, {}

    yaml_src = rest[:close.start]
    body = rest[close.end :]

    try:
        meta = yaml.safe_load(yaml_src)
    except yaml.YAMLError as exc:
        _log.warning(
            "frontmatter.parse.malformed",
            error=str(exc),
        )
        return text, {}

    if not isinstance(meta, dict):
        # An empty fence (``---\n---``) yields None; a scalar/list YAML
        # body is not a frontmatter mapping. Either way: no meta.
        return body, {}

    return body, meta


class _FenceSpan:
    """Half-open ``[start, end)`` span of the closing fence within a string."""

    __slots__ = ("start", "end")

    def __init__(self, start: int, end: int) -> None:
        self.start = start
        self.end = end


def _find_closing_fence(rest: str) -> _FenceSpan | None:
    """Locate the closing ``---`` line in ``rest`` (the post-open text).

    Returns a span whose ``start`` is the index of the closing fence line's
    first char (so ``rest[:start]`` is the YAML source) and whose ``end``
    is the index just past the newline following the closing fence (so
    ``rest[end:]`` is the body). Returns ``None`` if no closing fence line
    exists.
    """
    idx = 0
    n = len(rest)
    while idx <= n:
        nl = rest.find("\n", idx)
        line_end = n if nl == -1 else nl
        line = rest[idx:line_end]
        if line.rstrip() == "---":
            # Body starts after the newline following the fence (if any).
            body_start = n if nl == -1 else nl + 1
            return _FenceSpan(idx, body_start)
        if nl == -1:
            break
        idx = nl + 1
    return None
