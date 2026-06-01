"""ANNA tool surfaces.

Three in-process MCP servers live here:

* ``anna_self_edit`` — identity / persona / schedule mutations under the
  supervisor lock with audit events. The only path the SDK can mutate
  ANNA's core files through.
* ``anna_google`` — read-only Gmail + Calendar across configured accounts.
* ``anna_web`` — Phase 2 §2 slim tool surface: ``web_search`` (Brave),
  ``web_fetch`` (httpx + future Playwright fallback), and
  ``vault_download`` (URL → ``~/Obsidian/ANNA/Inbox``).
"""

from anna.tools.self_edit_server import (
    SELF_EDIT_TOOL_NAMES,
    SelfEditTools,
    build_self_edit_server,
)
from anna.tools.vault_tools import VaultTools
from anna.tools.web_server import WEB_TOOL_NAMES, build_web_server
from anna.tools.web_tools import WebTools

__all__ = [
    "SELF_EDIT_TOOL_NAMES",
    "SelfEditTools",
    "build_self_edit_server",
    "WEB_TOOL_NAMES",
    "WebTools",
    "VaultTools",
    "build_web_server",
]
