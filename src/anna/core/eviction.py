"""Core file eviction.

Per v3 section 6. When ANNA judges that one of the five core files has crept
past its token cap she runs an eviction at session close. The content she
picks is moved to ``vault/Identity/<file>-archive-<date>.md`` and the core
file is rewritten with the evicted content removed.

This module owns the archive write and emits the ``audit.eviction`` event.
The judgment of what to evict is ANNA's, not the runtime's, so the function
here accepts the ``keep_text`` and ``evict_text`` already chosen by the
caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from anna.core.identity import CORE_FILES, CoreFile, count_tokens
from anna.log import audit_event, get_logger


def perform_eviction(
    *,
    which: CoreFile,
    core_dir: Path,
    vault_root: Path,
    keep_text: str,
    evict_text: str,
    reason: str,
    session_close_conv: str,
    audit_dir: Path,
    fsync_on_write: bool = True,
) -> Path:
    """Archive evict_text, rewrite the core file with keep_text, emit audit.

    Returns the archive path. Raises on partial-failure modes after emitting
    a CRITICAL audit event; the supervisor poisons the file in that case.
    """
    log = get_logger("anna.eviction")
    spec = CORE_FILES[which]
    core_path = core_dir / spec.name

    tokens_before = count_tokens(core_path.read_text(encoding="utf-8")) if core_path.exists() else 0
    tokens_after = count_tokens(keep_text)
    tokens_evicted = count_tokens(evict_text)

    archive_dir = vault_root / "Identity"
    archive_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = spec.name.removesuffix(".md")
    archive_path = archive_dir / f"{base}-archive-{today}.md"

    archive_header = (
        f"---\n"
        f"created: {today}\n"
        f"tags:\n"
        f"  - domain/anna\n"
        f"  - type/archive\n"
        f"source_file: {spec.name}\n"
        f"reason: {reason!r}\n"
        f"session_close_conv: {session_close_conv}\n"
        f"---\n\n"
    )

    try:
        # Step 1: archive write. If this fails the core file is untouched.
        existing = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
        if existing:
            archive_path.write_text(existing + "\n\n---\n\n" + evict_text, encoding="utf-8")
        else:
            archive_path.write_text(archive_header + evict_text, encoding="utf-8")
    except OSError as exc:
        audit_event(
            "audit.eviction.archive_failed",
            audit_dir=audit_dir,
            actor="anna",
            conv_key=session_close_conv,
            level="CRITICAL",
            fsync_on_write=fsync_on_write,
            file=spec.name,
            archive_path=str(archive_path),
            error=str(exc),
        )
        log.critical("eviction.archive_failed", file=spec.name, error=str(exc))
        raise

    try:
        # Step 2: rewrite the core file. If this fails the supervisor poisons.
        core_path.write_text(keep_text, encoding="utf-8")
    except OSError as exc:
        audit_event(
            "audit.eviction.partial_failure",
            audit_dir=audit_dir,
            actor="anna",
            conv_key=session_close_conv,
            level="CRITICAL",
            fsync_on_write=fsync_on_write,
            file=spec.name,
            archive_path=str(archive_path),
            error=str(exc),
            note="archive succeeded; core rewrite failed; file is poisoned",
        )
        log.critical("eviction.partial_failure", file=spec.name, error=str(exc))
        raise

    audit_event(
        "audit.eviction",
        audit_dir=audit_dir,
        actor="anna",
        conv_key=session_close_conv,
        fsync_on_write=fsync_on_write,
        file=spec.name,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        tokens_evicted=tokens_evicted,
        archive_file=str(archive_path.relative_to(vault_root)) if archive_path.is_relative_to(vault_root) else str(archive_path),
        reason=reason,
        session_close_conv=session_close_conv,
    )

    return archive_path
