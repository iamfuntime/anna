"""TaskNoteReader — read layer over Obsidian TaskNote frontmatter (MC-09).

The operator's TaskNotes plugin keeps one markdown file per task
directly under ``integrations.obsidian.tasknotes_path`` (e.g.
``~/Obsidian/Brain/TaskNotes/Tasks``). Each file opens with a YAML
frontmatter fence carrying the pipeline fields this reader extracts::

    ---
    created: 2026-06-10
    status: todo
    priority: normal
    dateModified: 2026-06-10T15:35:00.000-04:00
    assignee: anna
    completedDate: 2026-06-10
    ---

    ## Task title here

Statuses bucket into the four kanban columns the ``/tasks`` board
renders (plus a quiet fifth for strays):

* ``open`` / ``todo`` → **open**
* ``in-progress`` / ``in progress`` / ``doing`` → **in_progress**
* ``review`` → **review**
* ``done`` / ``cancelled`` / ``canceled`` → **done** (capped at the
  :data:`DONE_COLUMN_CAP` most recently modified — the column is a
  recent-completions strip, not an archive)
* anything else, or no status at all → **other**

Frontmatter parsing rides :func:`anna.runtime.frontmatter
.split_frontmatter` — the same tolerant splitter the persona loader
uses. Its soft-failure shape (malformed YAML → no metadata) maps to
this reader's contract: a malformed note lands in **other** titled by
its filename rather than crashing the board.

Contract (shared across ``anna_web.readers``):

* **Bounded reads.** Only ``*.md`` entries directly under the
  configured directory are opened (no recursion — an ``Archive/``
  subfolder stays invisible), and at most :data:`_MAX_READ_CHARS` of
  each file is read; frontmatter and the title heading live at the top.
* **mtime+size-keyed cache.** Parsed notes cache per file against
  ``(st_mtime_ns, st_size)``; an unchanged file costs one ``stat()``
  per 15s board poll.
* **Fail soft, never raise.** Unset/missing/invalid directory →
  :meth:`TaskNoteReader.board` returns ``None`` (the view renders "no
  task data yet"); unreadable or malformed individual files degrade
  per-file; no public method ever raises into a route.

Synchronous and dependency-light by design — route handlers call it
via ``run_in_threadpool`` (see the plan's read-path section).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anna.runtime.frontmatter import split_frontmatter

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps import cost zero
    from anna.config import AnnaConfig

# Kanban bucket keys. The route/templates key off these, so they are
# module constants rather than stringly literals scattered per layer.
BUCKET_OPEN = "open"
BUCKET_IN_PROGRESS = "in_progress"
BUCKET_REVIEW = "review"
BUCKET_DONE = "done"
BUCKET_OTHER = "other"

# Done-column depth: the board shows the most recently modified
# completions, not the full history of finished TaskNotes.
DONE_COLUMN_CAP = 15

# Per-file read bound. TaskNotes are small; everything the board needs
# (frontmatter + first heading) sits at the top of the file, so a
# pathological multi-megabyte note costs at most this much.
_MAX_READ_CHARS = 256 * 1024

# Normalized status string → bucket. Lookup happens after lowercasing
# and collapsing ``-``/``_`` to spaces, so ``In-Progress`` and
# ``in_progress`` both match "in progress".
_STATUS_BUCKETS: dict[str, str] = {
    "open": BUCKET_OPEN,
    "todo": BUCKET_OPEN,
    "in progress": BUCKET_IN_PROGRESS,
    "doing": BUCKET_IN_PROGRESS,
    "review": BUCKET_REVIEW,
    "done": BUCKET_DONE,
    "cancelled": BUCKET_DONE,
    "canceled": BUCKET_DONE,
}


def bucket_for_status(status: Any) -> str:
    """Map a raw frontmatter ``status`` value to its board bucket.

    Tolerant of case, surrounding whitespace, and ``-``/``_`` word
    separators. Anything unrecognized — including a missing value or a
    non-string (YAML can hand back lists/bools) — buckets as
    :data:`BUCKET_OTHER`, never an error.
    """
    if not isinstance(status, str):
        return BUCKET_OTHER
    normalized = " ".join(status.strip().lower().replace("_", " ").replace("-", " ").split())
    return _STATUS_BUCKETS.get(normalized, BUCKET_OTHER)


@dataclass(frozen=True)
class TaskNote:
    """One parsed TaskNote file, shaped for the pipeline board."""

    filename: str  # file name including .md, rendered as mono text
    title: str  # first H1/H2 of the body, else the filename stem
    status: str  # raw frontmatter status ("" when absent/non-string)
    bucket: str  # bucket_for_status(status)
    assignee: str  # "" when absent
    priority: str  # lowercased; "" when absent
    created: str  # ISO string from created/dateCreated ("" when absent)
    modified: str  # ISO string from dateModified ("" when absent)
    completed: str  # ISO string from completedDate ("" when absent)
    sort_ts: str  # recency key: modified, else completed/created/file mtime


@dataclass(frozen=True)
class TaskBoard:
    """The bucketed board: four columns plus the quiet stray bucket.

    Every column is newest-``sort_ts``-first. ``done`` is capped at the
    reader's ``done_limit``; ``done_total`` preserves the pre-cap count
    so the view can say "15 of 38".
    """

    open: tuple[TaskNote, ...]
    in_progress: tuple[TaskNote, ...]
    review: tuple[TaskNote, ...]
    done: tuple[TaskNote, ...]
    other: tuple[TaskNote, ...]
    done_total: int
    total: int


def _as_iso(value: Any) -> str:
    """Coerce a frontmatter date field to an ISO string, "" when unusable.

    yaml.safe_load hands back ``datetime.date`` for ``2026-06-10`` and
    ``datetime.datetime`` for full timestamps; quoted values stay
    strings. All three shapes sort correctly against each other
    lexically (ISO-8601 prefix property).
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return ""


def _as_str(value: Any) -> str:
    """Coerce a scalar frontmatter field to a stripped string, "" otherwise."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _first_heading(body: str) -> str | None:
    """First H1/H2 heading text in ``body``, or None when there is none."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped[3:].strip() or None
        if stripped.startswith("# "):
            return stripped[2:].strip() or None
    return None


class TaskNoteReader:
    """Parse the TaskNote directory into a :class:`TaskBoard`.

    Parameters
    ----------
    path:
        ``integrations.obsidian.tasknotes_path`` (already ``~``-expanded
        via the config property when built through :meth:`from_config`).
        ``None`` — the operator enabled the integration but never set
        the path — is a supported state: :meth:`board` returns ``None``
        and the view renders its empty state.
    done_limit:
        Done-column cap, default :data:`DONE_COLUMN_CAP`.
    """

    def __init__(self, path: Path | str | None, *, done_limit: int = DONE_COLUMN_CAP) -> None:
        self._path = Path(path).expanduser() if path else None
        self._done_limit = max(1, int(done_limit))
        # path -> ((st_mtime_ns, st_size), parsed note). Keyed on the
        # stat pair so an edited note re-parses while an untouched one
        # costs one stat() per poll.
        self._cache: dict[Path, tuple[tuple[int, int], TaskNote]] = {}

    @classmethod
    def from_config(cls, cfg: AnnaConfig) -> TaskNoteReader | None:
        """Build a reader off the loaded config, gate-checked.

        Returns ``None`` when the Obsidian/TaskNotes integration gate
        does not pass — readers gate themselves through the registry's
        :func:`anna_web.integrations.is_enabled` per the MC-08 contract,
        belt-and-braces on top of the route never mounting. Any failure
        probing the gate or config counts as unavailable.
        """
        try:
            from anna_web.integrations import OBSIDIAN_TASKNOTES, is_enabled

            if not is_enabled(cfg, OBSIDIAN_TASKNOTES):
                return None
            return cls(cfg.integrations.obsidian.resolved_tasknotes_path)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Public read surface (fail-soft, never raises)
    # ------------------------------------------------------------------

    def board(self) -> TaskBoard | None:
        """The bucketed board, or ``None`` when the directory is unusable.

        ``None`` (unset path, missing dir, dir-is-a-file, permission
        problem) and an all-empty board (directory exists but holds no
        parseable ``*.md``) both render the view's "no task data yet"
        state; the distinction exists for tests and future surfacing.
        """
        try:
            notes = self._scan()
        except Exception:  # belt-and-braces: never raise into a route
            return None
        if notes is None:
            return None

        buckets: dict[str, list[TaskNote]] = {
            BUCKET_OPEN: [],
            BUCKET_IN_PROGRESS: [],
            BUCKET_REVIEW: [],
            BUCKET_DONE: [],
            BUCKET_OTHER: [],
        }
        for note in notes:
            buckets[note.bucket].append(note)
        for column in buckets.values():
            # sort_ts strings are ISO-8601, so lexical descending ==
            # newest first; "" (no usable date at all) sinks to the end.
            column.sort(key=lambda n: n.sort_ts, reverse=True)

        done_all = buckets[BUCKET_DONE]
        return TaskBoard(
            open=tuple(buckets[BUCKET_OPEN]),
            in_progress=tuple(buckets[BUCKET_IN_PROGRESS]),
            review=tuple(buckets[BUCKET_REVIEW]),
            done=tuple(done_all[: self._done_limit]),
            other=tuple(buckets[BUCKET_OTHER]),
            done_total=len(done_all),
            total=len(notes),
        )

    # ------------------------------------------------------------------
    # Scan + parse internals
    # ------------------------------------------------------------------

    def _scan(self) -> list[TaskNote] | None:
        """Every parseable ``*.md`` directly under the directory.

        Non-recursive on purpose: an ``Archive/`` (or any) subfolder is
        out of scope unless the operator points ``tasknotes_path`` at
        it. Returns ``None`` when the directory itself is unusable.
        """
        if self._path is None:
            return None
        try:
            entries = sorted(self._path.iterdir())
        except OSError:  # missing dir, permission problem, path-is-a-file
            return None
        notes: list[TaskNote] = []
        for entry in entries:
            try:
                if entry.suffix.lower() != ".md" or not entry.is_file():
                    continue
                note = self._note_for_file(entry)
            except OSError:
                continue
            if note is not None:
                notes.append(note)
        return notes

    def _note_for_file(self, path: Path) -> TaskNote | None:
        """Parse one note through the mtime+size cache; None when unreadable."""
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        note = self._parse_file(path, mtime=stat.st_mtime)
        if note is not None:
            self._cache[path] = (key, note)
        return note

    @staticmethod
    def _parse_file(path: Path, *, mtime: float) -> TaskNote | None:
        """Extract one :class:`TaskNote` from a markdown file.

        Malformed frontmatter degrades per the module contract: no
        metadata → the note buckets as "other" and is titled by its
        filename. Only an unreadable file yields ``None``.
        """
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fp:
                text = fp.read(_MAX_READ_CHARS)
        except OSError:
            return None

        body, meta = split_frontmatter(text)
        # split_frontmatter signals "fence present but unusable" by
        # returning the text unchanged with empty meta. Such a note is
        # malformed by contract: filename title, no heading scan (the
        # "body" still contains the broken fence).
        malformed = not meta and body == text and text.startswith("---")

        status = _as_str(meta.get("status"))
        title = None if malformed else _first_heading(body)
        created = _as_iso(meta.get("created")) or _as_iso(meta.get("dateCreated"))
        modified = _as_iso(meta.get("dateModified"))
        completed = _as_iso(meta.get("completedDate"))
        sort_ts = (
            modified
            or completed
            or created
            or datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        )
        return TaskNote(
            filename=path.name,
            title=title or path.stem,
            status=status,
            bucket=bucket_for_status(status),
            assignee=_as_str(meta.get("assignee")),
            priority=_as_str(meta.get("priority")).lower(),
            created=created,
            modified=modified,
            completed=completed,
            sort_ts=sort_ts,
        )
