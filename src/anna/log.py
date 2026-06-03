"""Structured logging.

Per v3 section 7. Three streams:

1. **Operational events.** structlog wrapping stdlib logging, JSON to stdout,
   journald-backed. Any library that uses stdlib logging gets routed into the
   same structured pipeline for free.
2. **Audit log.** Daily append-only JSONL files at
   ``$ANNA_HOME/audit/audit-YYYY-MM-DD.jsonl``. Every audit event is also
   mirrored to the operational stream at INFO so the operator sees it in
   journalctl alongside surrounding context.
3. **Transcripts.** Per-conversation daily JSONL files at
   ``$ANNA_HOME/transcripts/<channel>-<conv_key>/YYYY-MM-DD.jsonl``. One JSON
   line per inbound or outbound message.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog


# ---------------------------------------------------------------------------
# Operational stream
# ---------------------------------------------------------------------------


_LOGGING_CONFIGURED = False
_AUDIT_LOCK = threading.Lock()
_TRANSCRIPT_LOCKS: dict[str, threading.Lock] = {}
_TRANSCRIPT_LOCKS_GUARD = threading.Lock()


def configure_logging(level: str = "INFO", format: str = "json") -> None:
    """Wire structlog with stdlib logging.

    The processor chain is shared between structlog-originated records and
    foreign records (slack_bolt, telegram, anthropic SDK, etc.), so the
    operator sees one coherent JSON stream.
    """
    global _LOGGING_CONFIGURED

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))

    # Quiet a few libraries that are too chatty at INFO.
    for noisy in ("urllib3", "httpx", "telegram.ext.Application", "slack_bolt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> Any:
    """Return a bound structlog logger for the given name."""
    return structlog.get_logger(name)


# ---------------------------------------------------------------------------
# Audit stream
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _audit_path_for_today(audit_dir: Path) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return audit_dir / f"audit-{today}.jsonl"


def audit_event(
    name: str,
    *,
    audit_dir: Path,
    actor: str = "anna",
    conv_key: str | None = None,
    fsync_on_write: bool = True,
    level: str = "INFO",
    **fields: Any,
) -> None:
    """Emit an audit event.

    The event is written to today's append-only JSONL file under
    ``audit_dir``, mirrored to the operational stream at ``level``, and the
    file is fsynced when ``fsync_on_write`` is true.

    If the audit write fails for any reason, the failure is logged at
    CRITICAL with the original event content so the operator can still
    reconstruct what happened.
    """
    record = {
        "ts": _now_iso(),
        "level": level,
        "event": name,
        "actor": actor,
        "conv_key": conv_key,
        **fields,
    }
    log = get_logger("anna.audit")

    # Mirror to operational stream first. Even if the file write fails the
    # operator still sees the event in journald.
    log_method = getattr(log, level.lower(), log.info)
    log_method(name, **{k: v for k, v in record.items() if k not in ("ts", "level", "event")})

    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        path = _audit_path_for_today(audit_dir)
        line = json.dumps(record, ensure_ascii=False)
        with _AUDIT_LOCK:
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
                if fsync_on_write:
                    fp.flush()
                    os.fsync(fp.fileno())
    except Exception as exc:
        # The audit stream is broken. Surface a CRITICAL with the original
        # event embedded so nothing is lost.
        log.critical(
            "audit_write_failed",
            failed_event=record,
            error=str(exc),
            audit_dir=str(audit_dir),
        )


# ---------------------------------------------------------------------------
# Transcript stream
# ---------------------------------------------------------------------------


def _transcript_dir_for(transcripts_dir: Path, channel: str, conv_key: str) -> Path:
    # The conv_key embeds the channel for cross-process clarity, but we keep
    # them both for filesystem readability per v3 section 7.
    safe = conv_key.replace(":", "-").replace("/", "_")
    return transcripts_dir / safe


def _transcript_path_for_today(transcripts_dir: Path, channel: str, conv_key: str) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _transcript_dir_for(transcripts_dir, channel, conv_key) / f"{today}.jsonl"


def _lock_for(conv_key: str) -> threading.Lock:
    with _TRANSCRIPT_LOCKS_GUARD:
        lock = _TRANSCRIPT_LOCKS.get(conv_key)
        if lock is None:
            lock = threading.Lock()
            _TRANSCRIPT_LOCKS[conv_key] = lock
        return lock


def transcript_event(
    channel: str,
    conv_key: str,
    *,
    transcripts_dir: Path,
    direction: str,
    text: str,
    **fields: Any,
) -> None:
    """Append a transcript line for the given conversation.

    Each line carries the timestamp, direction (inbound or outbound), the raw
    text, and any caller-supplied fields. Writes are serialized per conv_key
    to preserve inbound-before-outbound ordering within a single conversation.
    """
    record = {
        "ts": _now_iso(),
        "direction": direction,
        "conv_key": conv_key,
        "text": text,
        **fields,
    }
    log = get_logger("anna.transcript")
    try:
        path = _transcript_path_for_today(transcripts_dir, channel, conv_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _lock_for(conv_key):
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
    except Exception as exc:
        log.error(
            "transcript_write_failed",
            channel=channel,
            conv_key=conv_key,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Housekeeping helpers
# ---------------------------------------------------------------------------


def sweep_audit_retention(audit_dir: Path, retention_days: int) -> int:
    """Delete audit files older than retention_days. Return count deleted.

    Zero means keep forever. Idempotent: safe to call multiple times.
    """
    if retention_days <= 0 or not audit_dir.is_dir():
        return 0
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - retention_days * 86400
    deleted = 0
    for path in audit_dir.glob("audit-*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError:
            # Best-effort sweep. The next run will retry.
            continue
    return deleted


def sweep_transcript_retention(transcripts_dir: Path, retention_days: int) -> tuple[int, int]:
    """Gzip transcripts older than retention_days, delete those older than 3x.

    Returns (gzipped, deleted). Zero retention_days means do nothing.
    """
    if retention_days <= 0 or not transcripts_dir.is_dir():
        return (0, 0)
    now = datetime.now(timezone.utc).timestamp()
    gzip_cutoff = now - retention_days * 86400
    delete_cutoff = now - 3 * retention_days * 86400
    gzipped = 0
    deleted = 0

    for conv_dir in transcripts_dir.iterdir():
        if not conv_dir.is_dir():
            continue
        for path in conv_dir.iterdir():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if path.suffix == ".gz":
                if mtime < delete_cutoff:
                    try:
                        path.unlink()
                        deleted += 1
                    except OSError:
                        pass
                continue
            if path.suffix == ".jsonl" and mtime < gzip_cutoff:
                gz_path = path.with_suffix(path.suffix + ".gz")
                try:
                    with path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                        dst.writelines(src)
                    path.unlink()
                    gzipped += 1
                except OSError:
                    # Leave the original in place; next sweep retries.
                    if gz_path.exists():
                        try:
                            gz_path.unlink()
                        except OSError:
                            pass
    return (gzipped, deleted)


def sweep_voice_retention(transcripts_dir: Path, retention_days: int) -> int:
    """Delete persisted voice audio files older than retention_days.

    Voice notes live under ``$ANNA_HOME/transcripts/voice/<conv_key>/
    <msg_id>.{ogg,webm,...}`` (Phase 2.5). They are treated as transcript
    artifacts but, unlike the JSONL transcripts, are not gzipped — Opus is
    already compressed, so the files delete outright at ``retention_days``
    (the same window as :func:`sweep_transcript_retention`'s gzip cutoff).

    Returns the count of audio files deleted. Zero ``retention_days`` means
    keep forever. Idempotent and best-effort: a file that can't be stat'd or
    unlinked is skipped and retried on the next sweep.
    """
    if retention_days <= 0:
        return 0
    voice_dir = transcripts_dir / "voice"
    if not voice_dir.is_dir():
        return 0
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - retention_days * 86400
    deleted = 0
    for conv_dir in voice_dir.iterdir():
        if not conv_dir.is_dir():
            continue
        for path in conv_dir.iterdir():
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError:
                # Best-effort sweep. The next run retries.
                continue
    return deleted
