"""Identity file metadata and read helpers.

The five core files have hard token caps. The supervisor wraps writes; this
module only owns the static spec (name, purpose, cap) and a thin token
counter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib import resources
from pathlib import Path


class CoreFile(str, Enum):
    SOUL = "SOUL.md"
    CLAUDE = "CLAUDE.md"
    AGENTS = "AGENTS.md"
    MEMORY = "MEMORY.md"
    IDENTITY = "IDENTITY.md"
    CADENCE = "CADENCE.md"


@dataclass(frozen=True)
class CoreFileSpec:
    name: str
    token_cap: int
    purpose: str


CORE_FILES: dict[CoreFile, CoreFileSpec] = {
    CoreFile.SOUL: CoreFileSpec(
        name="SOUL.md",
        token_cap=1500,
        purpose="The operator's core values and ANNA's relationship to them.",
    ),
    CoreFile.CLAUDE: CoreFileSpec(
        name="CLAUDE.md",
        token_cap=2500,
        purpose="High-level operating instructions ANNA reads on every session.",
    ),
    CoreFile.AGENTS: CoreFileSpec(
        name="AGENTS.md",
        token_cap=1500,
        purpose="Registry of sub-agents ANNA has hired, with their purposes.",
    ),
    CoreFile.MEMORY: CoreFileSpec(
        name="MEMORY.md",
        token_cap=3000,
        purpose="Long-term facts and preferences ANNA carries across conversations.",
    ),
    CoreFile.IDENTITY: CoreFileSpec(
        name="IDENTITY.md",
        token_cap=1000,
        purpose="Who ANNA is addressing right now and the active conversational frame.",
    ),
    CoreFile.CADENCE: CoreFileSpec(
        name="CADENCE.md",
        token_cap=1000,
        purpose="Cadence rules for buffered transports — prepended to inbound text as a `<system-reminder>` block.",
    ),
}


def count_tokens(text: str) -> int:
    """Cheap word-based token estimate.

    The exact tokenizer the SDK uses is not exposed as a stable API for this
    purpose. The supervisor only needs a budget signal, so a word-count
    approximation is sufficient. Real token accounting happens in the SDK on
    the input side.
    """
    return len(text.split())


def ensure_core_files(core_dir: Path) -> None:
    """Populate the operator's core directory with empty templates.

    Called by the setup wizard. If a file already exists it is left in place.
    The packaged templates in ``anna.core_files`` are the source.
    """
    core_dir.mkdir(parents=True, exist_ok=True)
    package = "anna.core_files"
    for spec in CORE_FILES.values():
        target = core_dir / spec.name
        if target.exists():
            continue
        with resources.as_file(resources.files(package).joinpath(spec.name)) as src:
            target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def read_core_file(core_dir: Path, which: CoreFile) -> str:
    spec = CORE_FILES[which]
    path = core_dir / spec.name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")
