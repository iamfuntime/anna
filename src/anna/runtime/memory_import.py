"""Conservative, idempotent import of legacy markdown memory into the fact store.

Never deletes or edits the source, never scans transcripts, and requires the
caller to name the owner and scope explicitly.  Group/shared scopes must use the
conversation key as owner (the worker's convention), so no user owner is
invented for shared history.  Items the user already forgot are not revived.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from anna.runtime.memory import MemoryStore, normalize_text

MIN_ITEM_CHARS = 8
MAX_ITEM_CHARS = 500
MAX_FILES = 200
MAX_ITEMS = 2000


def validate_scope_selection(owner_id: str, scope: str) -> None:
    if not owner_id.strip() or not scope.strip():
        raise ValueError("owner and scope must be given explicitly")
    if scope != "private" and not scope.startswith("group:"):
        raise ValueError("scope must be 'private' or 'group:<conversation_key>'")
    if scope.startswith("group:") and owner_id != scope[len("group:"):]:
        raise ValueError("group scope requires owner equal to the conversation key (no fabricated user owner)")
    if scope == "private" and owner_id.startswith("group"):
        raise ValueError("private scope needs a user owner, not a group key")


def _files(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)
    pattern = "**/*.md" if recursive else "*.md"
    return sorted(p for p in path.glob(pattern) if p.is_file() and not p.is_symlink())[:MAX_FILES]


def iter_legacy_items(path: Path, *, recursive: bool = False) -> list[tuple[str, str, str, str]]:
    """Return (source_label, subject, content, raw_line) bullet items; headings become subjects."""
    base = path if path.is_dir() else path.parent
    items: list[tuple[str, str, str, str]] = []
    for file in _files(path, recursive):
        label = file.relative_to(base).as_posix()
        subject = file.stem
        in_front = False
        for n, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines()):
            stripped = line.strip()
            if n == 0 and stripped == "---":
                in_front = True
                continue
            if in_front:
                in_front = stripped != "---"
                continue
            if stripped.startswith("#"):
                subject = stripped.lstrip("#").strip() or subject
            elif stripped[:2] in ("- ", "* "):
                text = stripped[2:].strip()
                if text.startswith(("[ ]", "[x]")):
                    text = text[3:].strip()
                items.append((label, subject[:120], text, stripped))
            if len(items) >= MAX_ITEMS:
                return items
    return items


def import_legacy(store: MemoryStore, path: Path, *, owner_id: str, scope: str, kind: str = "fact",
                  recursive: bool = False, apply: bool = False) -> Counter[str]:
    """Import bullet items from a MEMORY.md-style file or a directory of notes.

    Dry-run (default) reports what would happen without writing.
    """
    validate_scope_selection(owner_id, scope)
    result: Counter[str] = Counter()
    for label, subject, content, raw in iter_legacy_items(path, recursive=recursive):
        if len(content) < MIN_ITEM_CHARS or len(content) > MAX_ITEM_CHARS:
            result["skipped_invalid"] += 1
            continue
        item_hash = hashlib.sha256(f"{label}\x00{normalize_text(subject)}\x00{normalize_text(content)}".encode()).hexdigest()
        if not apply:
            result["would_import"] += 1
            continue
        result[store.import_legacy_item(owner_id=owner_id, visibility_scope=scope, kind=kind, subject=subject,
                                        content=content, source_label=label, item_hash=item_hash,
                                        evidence=raw)] += 1
    return result
