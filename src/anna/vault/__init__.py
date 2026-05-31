"""Vault writers.

Three flavors:

* :mod:`anna.vault.checkpoint`: per-conversation summary notes written at
  session boundaries.
* :mod:`anna.vault.transcripts`: JSONL transcript files per conversation (the
  raw implementation lives in :mod:`anna.log`; this module exposes a small
  helper for reading them back).
* :mod:`anna.vault.audit`: helpers for reading the audit log, used by
  ``anna-logs --audit``. The append-only writer lives in :mod:`anna.log`.
"""
