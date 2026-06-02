"""Round-trip read/write of ``anna.yaml`` for the web dashboard.

Subtask 3 of the Phase 2.5 buildout. The :class:`ConfigStore` is the
sole writer of ``$ANNA_HOME/anna.yaml`` from the web surface. Two
load methods cover the two consumers:

* :meth:`ConfigStore.load_raw` — returns the raw ruamel
  ``CommentedMap`` so subsequent writes preserve operator comments,
  key ordering, and quote styles verbatim.
* :meth:`ConfigStore.load_validated` — runs the document through
  :class:`anna.config.AnnaConfig` and returns the pydantic model so
  callers can do type-safe field access.

The operative mutator is :meth:`ConfigStore.write_section`. It
acquires an asyncio lock, reloads the round-trip doc fresh from disk
(no stale cache), replaces one top-level section wholesale with the
caller's payload, validates the full document against
:class:`AnnaConfig`, and atomically writes the result via a
tempfile-and-rename in the same directory so the swap is crash-safe.

Audit-event emission for ``audit.web.dashboard.config_write`` is
deliberately out of scope for this subtask — that wiring lands with
subtask 12. The ``actor`` parameter is on the signature so the route
subtasks (7) can pass it through without ever touching call sites
again when subtask 12 wires the audit pipeline.

See Inbox/2026-06-02-ANNA-Web-Dashboard-Plan.md, "Architecture →
ConfigStore — ruamel round-trip" for the full design.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from anna.config import AnnaConfig


class ConfigStore:
    """Round-trip-preserving editor for ``$ANNA_HOME/anna.yaml``.

    Instances are intended to be per-process singletons hung off
    ``app.state.config_store`` by the FastAPI app factory. The
    write lock is an :class:`asyncio.Lock`, so concurrent POSTs from
    two browser tabs serialize cleanly on a single event loop. The
    store does NOT coordinate with the running daemon's reader — the
    daemon reads ``anna.yaml`` once at boot and never again, so the
    operator-presses-Restart loop is the only way edits take effect.
    """

    def __init__(self, *, anna_home: Path) -> None:
        self._path = anna_home / "anna.yaml"
        self._yaml = YAML(typ="rt")  # round-trip mode
        self._yaml.preserve_quotes = True
        self._yaml.indent(mapping=2, sequence=4, offset=2)
        self._write_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """Absolute path the store reads and writes."""
        return self._path

    def load_raw(self) -> CommentedMap:
        """Read ``anna.yaml`` and return the ruamel round-trip document.

        The returned :class:`CommentedMap` preserves every comment,
        key ordering, quote style, and indentation token from the
        on-disk file. Mutating it and dumping back through the same
        :class:`YAML` instance is byte-identical when no values
        change.

        An empty file (or a file containing only comments) loads as
        ``None``; we coerce that to an empty :class:`CommentedMap`
        so callers always get a mutable mapping back.
        """
        with self._path.open("r", encoding="utf-8") as fp:
            data = self._yaml.load(fp)
        if data is None:
            return CommentedMap()
        if not isinstance(data, CommentedMap):
            # ruamel returns a plain dict for mappings without comments
            # in some edge cases; force-promote so callers can rely on
            # the type without runtime branching.
            promoted = CommentedMap()
            promoted.update(data)
            return promoted
        return data

    def load_validated(self) -> AnnaConfig:
        """Read + validate through :class:`AnnaConfig`.

        Convenience for consumers that want the typed pydantic
        surface rather than the raw round-trip doc.
        """
        raw = self.load_raw()
        # ruamel's CommentedMap is dict-subclass-compatible, but
        # model_validate is happiest with a plain dict — convert to be
        # explicit and avoid any pydantic-vs-ruamel surprises.
        return AnnaConfig.model_validate(_to_plain(raw))

    async def write_section(
        self,
        section: str,
        payload: dict[str, Any],
        *,
        actor: str = "operator",
    ) -> AnnaConfig:
        """Replace one top-level section wholesale and persist atomically.

        Steps (per the plan):

        1. Acquire the write lock.
        2. Reload the round-trip doc from disk inside the lock — no
           stale cache.
        3. Replace the named section in-place. Unknown sections raise
           :class:`ValueError` before any mutation occurs.
        4. Validate the whole mutated doc against :class:`AnnaConfig`.
           Validation failure raises the pydantic
           :class:`ValidationError` without writing the file.
        5. Atomic write via :func:`tempfile.NamedTemporaryFile` in
           the same directory as the target, ``os.fsync``, then
           :func:`os.replace`.
        6. Return the freshly-validated :class:`AnnaConfig`.

        ``payload`` replaces the section's contents wholesale; field-
        level merge is deliberately out of scope for v1. The form
        POSTs the entire section's current state every time.

        Audit-event emission lives in subtask 12; the ``actor``
        argument is reserved on the signature so call sites do not
        change when that wiring lands.
        """
        # Allow-list of writable top-level sections, derived from the
        # AnnaConfig schema so future buildouts (new top-level
        # blocks) become writable automatically without touching this
        # file. We deliberately exclude private/derived fields like
        # ``anna_home`` — it's a Path computed from env vars, not a
        # YAML key.
        allowed = {
            name
            for name in AnnaConfig.model_fields
            if name != "anna_home"
        }
        if section not in allowed:
            raise ValueError(
                f"unknown config section: {section!r} "
                f"(known: {sorted(allowed)})"
            )

        async with self._write_lock:
            doc = self.load_raw()
            doc[section] = payload

            # Validate the full mutated doc. We convert to a plain
            # dict so the pydantic validator does not have to
            # contend with CommentedMap subclasses anywhere in the
            # tree. If validation fails the file is untouched.
            validated = AnnaConfig.model_validate(_to_plain(doc))

            self._atomic_write(doc)
            return validated

    def _atomic_write(self, doc: CommentedMap) -> None:
        """Dump ``doc`` to a tempfile in the same directory, fsync, rename.

        Same-directory tempfile keeps :func:`os.replace` atomic on
        every common filesystem (the rename only crosses inodes,
        never filesystems). If the dump raises after the tempfile is
        created but before the rename, we clean up the orphan so a
        validation regression in some future dependency does not
        leave litter behind.
        """
        parent = self._path.parent
        # delete=False because we manage cleanup manually around the
        # fsync + rename window.
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(parent),
            prefix=".anna.yaml.",
            suffix=".tmp",
            delete=False,
        )
        tmp_path = Path(tmp.name)
        try:
            try:
                self._yaml.dump(doc, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
            finally:
                tmp.close()
            os.replace(tmp_path, self._path)
        except BaseException:
            # Best-effort cleanup of the tempfile so we do not leak
            # ``.anna.yaml.*.tmp`` files on dump failure. The original
            # ``anna.yaml`` is untouched because os.replace never ran.
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            raise


def _to_plain(value: Any) -> Any:
    """Recursively convert ruamel ``CommentedMap`` / ``CommentedSeq`` to plain types.

    pydantic's ``model_validate`` accepts dict-likes, but ruamel's
    commented containers are also ordered-dict subclasses with some
    extra attrs that occasionally trip up validators expecting
    vanilla ``dict``/``list``. Stripping the wrappers up front avoids
    that whole class of bug.
    """
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    return value
