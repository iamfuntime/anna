"""ANNA tool surfaces.

The self-edit MCP server lives here. It is the only path through which the
SDK can mutate ANNA's identity files, sub-agent personas, or skill files.
Every mutation goes through the supervisor lock and emits an audit event.
"""

from anna.tools.self_edit_server import (
    SELF_EDIT_TOOL_NAMES,
    SelfEditTools,
    build_self_edit_server,
)

__all__ = [
    "SELF_EDIT_TOOL_NAMES",
    "SelfEditTools",
    "build_self_edit_server",
]
