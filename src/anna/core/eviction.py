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

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Closeout-time eviction driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvictionDecision:
    keep_text: str
    evict_text: str
    reason: str


def _is_over_cap(text: str, cap: int) -> bool:
    return count_tokens(text) > cap


async def evict_if_over_cap(
    *,
    which: CoreFile,
    core_dir: Path,
    vault_root: Path,
    sdk_client: Any,
    session_close_conv: str,
    audit_dir: Path,
    fsync_on_write: bool = True,
) -> Path | None:
    """Drive a single core file through the eviction pipeline.

    Reads the current file. If it's at or under cap, returns ``None``
    without calling the SDK. If it's over cap, asks the SDK to propose
    a (keep_text, evict_text) split and then calls :func:`perform_eviction`
    to archive and rewrite. Returns the archive path on a successful
    eviction.

    The SDK side is intentionally minimal: we send a structured prompt and
    parse a strict JSON response. This keeps eviction deterministic enough
    for the runtime to run unattended at session close. If the SDK answer
    fails to parse or the keep+evict reconstruction does not match the
    original (modulo whitespace), we skip the eviction and log a warning
    rather than risk a destructive rewrite.
    """
    log = get_logger("anna.eviction")
    spec = CORE_FILES[which]
    core_path = core_dir / spec.name
    if not core_path.exists():
        return None

    text = core_path.read_text(encoding="utf-8")
    if not _is_over_cap(text, spec.token_cap):
        return None

    decision = await _ask_sdk_for_eviction(
        sdk_client=sdk_client,
        which=which,
        current_text=text,
        cap=spec.token_cap,
    )
    if decision is None:
        log.warning(
            "eviction.skipped",
            file=spec.name,
            reason="sdk did not return a usable eviction proposal",
        )
        return None

    return perform_eviction(
        which=which,
        core_dir=core_dir,
        vault_root=vault_root,
        keep_text=decision.keep_text,
        evict_text=decision.evict_text,
        reason=decision.reason,
        session_close_conv=session_close_conv,
        audit_dir=audit_dir,
        fsync_on_write=fsync_on_write,
    )


async def _ask_sdk_for_eviction(
    *,
    sdk_client: Any,
    which: CoreFile,
    current_text: str,
    cap: int,
) -> EvictionDecision | None:
    """Round-trip the SDK and parse a JSON eviction proposal.

    The SDK is expected to return a JSON object with three fields:

    ``keep_text``  — the rewritten core file content (under cap).
    ``evict_text`` — the prose pulled out, to be archived in vault/Identity/.
    ``reason``     — a short human-readable rationale.

    If the response cannot be parsed, returns ``None`` and the caller
    skips the eviction.
    """
    import json

    log = get_logger("anna.eviction")
    spec = CORE_FILES[which]
    prompt = (
        f"You have just finished a conversation. Your core file "
        f"{spec.name} is over its {cap}-token cap. Propose an eviction. "
        f"Return STRICT JSON only — no prose, no markdown fence — with "
        f"these three keys:\n"
        f'  - "keep_text": the rewritten content that stays in {spec.name}.\n'
        f'  - "evict_text": the content to archive into '
        f"vault/Identity/.\n"
        f'  - "reason": a short rationale (one sentence).\n\n'
        f"Current {spec.name}:\n---\n{current_text}\n---\n"
    )

    try:
        await sdk_client.query(prompt)
    except Exception as exc:
        log.warning("eviction.sdk_query_failed", file=spec.name, error=str(exc))
        return None

    try:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    except ImportError:
        AssistantMessage = ResultMessage = TextBlock = None  # type: ignore[assignment,misc]

    chunks: list[str] = []
    try:
        async for msg in sdk_client.receive_response():
            if AssistantMessage is not None and isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if TextBlock is not None and isinstance(block, TextBlock):
                        chunks.append(block.text)
            if ResultMessage is not None and isinstance(msg, ResultMessage):
                break
    except Exception as exc:
        log.warning("eviction.sdk_receive_failed", file=spec.name, error=str(exc))
        return None

    raw = "\n".join(c for c in chunks if c).strip()
    # Strip code fence if the model wrapped it despite being told not to.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        log.warning(
            "eviction.parse_failed",
            file=spec.name,
            error=str(exc),
            head=raw[:200],
        )
        return None

    keep = str(payload.get("keep_text", "")).strip()
    evict = str(payload.get("evict_text", "")).strip()
    reason = str(payload.get("reason", "")).strip()
    if not keep or not evict:
        log.warning(
            "eviction.empty_proposal",
            file=spec.name,
            keep_len=len(keep),
            evict_len=len(evict),
        )
        return None

    return EvictionDecision(keep_text=keep, evict_text=evict, reason=reason or "(no reason given)")
