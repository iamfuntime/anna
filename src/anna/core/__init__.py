"""Core identity files.

Five files with hard token caps per v3 section 6:

* SOUL.md (1500): the operator's core values and ANNA's relationship to them.
* CLAUDE.md (2500): high-level operating instructions.
* AGENTS.md (1500): registry of sub-agents ANNA has hired.
* MEMORY.md (3000): long-term facts and preferences.
* IDENTITY.md (1000): who ANNA is addressing right now and the conversational frame.

ANNA judges eviction at session close. Evicted content is archived to
``vault/Identity/<file>-archive-<date>.md``.
"""

from anna.core.identity import (
    CORE_FILES,
    CoreFile,
    CoreFileSpec,
    ensure_core_files,
    read_core_file,
)

__all__ = ["CORE_FILES", "CoreFile", "CoreFileSpec", "ensure_core_files", "read_core_file"]
