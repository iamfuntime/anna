"""DelegationReader — read layer over sub-agent transcript trailers (subtask 4).

The sub-agent runner (``anna.runtime.subagent.SubAgentRunner``) appends
JSONL transcript lines under
``cfg.subagent_transcript_dir/<slug>/<YYYY-MM-DD>.jsonl``. Each
delegation typically writes a ``task`` line on spawn and an
``outbound`` line on success; the ``outbound`` line carries the
cost/latency trailer this reader exists to aggregate:

* ``cost_usd`` — float, SDK-reported delegation cost.
* ``duration_seconds`` — float wall-clock (older writers may instead
  carry ``duration_ms``; both are accepted).
* ``tool_calls`` — list of tool names used by the run (an int count is
  also accepted for forward compatibility).
* ``model`` — raw resolved model string (e.g. ``claude-fable-5``).
  Older trailer lines predate the configurable-model work and omit it;
  those runs bucket under the ``"unknown"`` tier.
* ``audit_id`` — UUID shared with the matching ``audit.subagent.*``
  events so views can cross-reference.

Cost source of truth per the Mission Control plan ("Open Q4"): the
transcript trailer is primary; audit events are a cross-check handled
elsewhere (``AuditReader``).

Contract (shared across ``anna_web.readers``):

* **Bounded reads.** Only day-files whose filename date falls inside
  the requested window (default ``DEFAULT_WINDOW_DAYS``) are opened.
* **mtime+size-keyed cache.** Parsed runs cache per file against
  ``(st_mtime_ns, st_size)``; an unchanged file costs one ``stat()``.
* **Fail soft, never raise.** Missing root dir → empty history and
  zero-filled rollups. Malformed JSON lines, non-dict lines, and
  wrong-typed trailer fields are skipped or coerced; no public method
  ever raises into a route.

Synchronous and pure-stdlib by design — route handlers call it via
``run_in_threadpool`` (see the plan's read-path section).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps import cost zero
    from anna.config import AnnaConfig

# Default scan window in calendar days, inclusive of "today". Two weeks
# matches the dashboard's cost-rollup horizon without unbounded growth
# as transcript history accumulates.
DEFAULT_WINDOW_DAYS = 14

# Known model tiers, matched as substrings of the raw model string.
# Covers both bare aliases ("opus") and full IDs ("claude-opus-4-1",
# "us.anthropic.claude-fable-5-...").
_TIERS: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

# Tier label for runs whose trailer predates the model field (or whose
# model value is unusable). Kept distinct from "other" (a model string
# we saw but did not recognize) so the view can render them differently.
UNKNOWN_TIER = "unknown"
OTHER_TIER = "other"


def normalize_model(raw: str | None) -> str:
    """Map a raw model string to a coarse tier label.

    ``"claude-fable-5"`` → ``"fable"``, ``"claude-opus-4-1"`` →
    ``"opus"``, bare aliases pass through (``"sonnet"`` → ``"sonnet"``).
    Missing/empty/non-string → :data:`UNKNOWN_TIER`; a real string with
    no recognized tier substring → :data:`OTHER_TIER`. The raw string is
    preserved on :class:`DelegationRun` — this label is for grouping
    only.
    """
    if not isinstance(raw, str) or not raw.strip():
        return UNKNOWN_TIER
    lowered = raw.lower()
    if lowered == "<cli-default>":
        # The runner's placeholder for "no override, inherited the CLI
        # account default" (see SubAgentRunner's spawn audit) — there is
        # no way to know which tier actually served the run.
        return UNKNOWN_TIER
    for tier in _TIERS:
        if tier in lowered:
            return tier
    return OTHER_TIER


@dataclass(frozen=True)
class DelegationRun:
    """One completed delegation, extracted from an ``outbound`` trailer."""

    slug: str
    date: str  # YYYY-MM-DD, from the day-file name
    ts: str  # ISO timestamp from the line ("" when absent)
    model: str | None  # raw trailer value, None when the line predates it
    model_tier: str  # normalize_model(model)
    cost_usd: float
    duration_seconds: float
    tool_call_count: int
    audit_id: str


def _as_float(value: Any) -> float | None:
    """Coerce a trailer number defensively; None on anything unusable."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _zero_bucket() -> dict[str, Any]:
    return {"runs": 0, "cost_usd": 0.0}


def _week_label(day: date) -> str:
    """ISO-week bucket key, e.g. ``2026-W24``."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class DelegationReader:
    """Aggregate sub-agent transcript trailers into dashboard rollups.

    Parameters
    ----------
    root:
        ``cfg.subagent_transcript_dir`` — the per-slug transcript root.
        May point at a directory that does not exist yet (fresh install,
        no delegations run): every method then returns its empty/zeroed
        shape.
    window_days:
        Default scan window in calendar days, inclusive of today.
        Individual calls may override via their ``window_days`` kwarg.
    """

    def __init__(self, root: Path, *, window_days: int = DEFAULT_WINDOW_DAYS) -> None:
        self._root = Path(root)
        self._window_days = max(1, int(window_days))
        # path -> ((st_mtime_ns, st_size), parsed runs). Keyed on the
        # stat pair so an appended line (size change) or rewrite (mtime
        # change) re-parses, while an untouched file costs one stat().
        self._cache: dict[Path, tuple[tuple[int, int], tuple[DelegationRun, ...]]] = {}

    @classmethod
    def from_config(cls, cfg: AnnaConfig, *, window_days: int = DEFAULT_WINDOW_DAYS) -> DelegationReader:
        """Build a reader off the loaded :class:`AnnaConfig`."""
        return cls(cfg.subagent_transcript_dir, window_days=window_days)

    # ------------------------------------------------------------------
    # Public read surface (all fail-soft, none raise)
    # ------------------------------------------------------------------

    def runs(
        self,
        *,
        window_days: int | None = None,
        today: date | None = None,
    ) -> list[DelegationRun]:
        """All runs in the window, newest first.

        ``today`` exists for deterministic tests and defaults to the
        wall-clock date. Ordering key is ``(date, ts)`` descending —
        both are strings, so a malformed ``ts`` can never raise out of
        the sort; ISO-8601 UTC timestamps order correctly lexically.
        """
        try:
            collected = self._scan(self._resolve_window(window_days), today or date.today())
        except Exception:  # belt-and-braces: never raise into a route
            return []
        collected.sort(key=lambda run: (run.date, run.ts), reverse=True)
        return collected

    def history(
        self,
        *,
        window_days: int | None = None,
        today: date | None = None,
    ) -> dict[str, list[DelegationRun]]:
        """Per-agent run history, newest first within each slug.

        Slugs are keyed in most-recently-active order (the order their
        newest run appears in :meth:`runs`), which is also the order the
        delegations view renders them. Missing root → ``{}``.
        """
        grouped: dict[str, list[DelegationRun]] = {}
        for run in self.runs(window_days=window_days, today=today):
            grouped.setdefault(run.slug, []).append(run)
        return grouped

    def model_split(
        self,
        *,
        window_days: int | None = None,
        today: date | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Per-model cost/run split, keyed by normalized tier label.

        Each tier bucket carries ``runs``, ``cost_usd``, and a nested
        ``models`` dict keyed by the *raw* model string (so the view can
        show the exact ID behind a tier, including A/B variants).
        Tiers are ordered by descending cost. Missing root → ``{}``.
        """
        split: dict[str, dict[str, Any]] = {}
        for run in self.runs(window_days=window_days, today=today):
            bucket = split.setdefault(
                run.model_tier, {"runs": 0, "cost_usd": 0.0, "models": {}}
            )
            bucket["runs"] += 1
            bucket["cost_usd"] += run.cost_usd
            raw_key = run.model if run.model is not None else UNKNOWN_TIER
            raw_bucket = bucket["models"].setdefault(raw_key, _zero_bucket())
            raw_bucket["runs"] += 1
            raw_bucket["cost_usd"] += run.cost_usd
        return dict(
            sorted(split.items(), key=lambda item: item[1]["cost_usd"], reverse=True)
        )

    def daily_rollup(
        self,
        *,
        window_days: int | None = None,
        today: date | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Cost/run totals per calendar day, newest day first.

        Every day in the window is present and zero-filled even when no
        delegations ran (and therefore also when the transcript root is
        missing entirely) — the cost panel renders a fixed-width strip,
        not a sparse one.
        """
        anchor = today or date.today()
        days = self._resolve_window(window_days)
        rollup: dict[str, dict[str, Any]] = {
            (anchor - timedelta(days=offset)).isoformat(): _zero_bucket()
            for offset in range(days)
        }
        for run in self.runs(window_days=window_days, today=anchor):
            bucket = rollup.get(run.date)
            if bucket is None:  # defensive; runs are already window-bounded
                continue
            bucket["runs"] += 1
            bucket["cost_usd"] += run.cost_usd
        return rollup

    def weekly_rollup(
        self,
        *,
        window_days: int | None = None,
        today: date | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Cost/run totals per ISO week (``YYYY-Www``), newest week first.

        Like :meth:`daily_rollup`, every ISO week touched by the window
        is present and zero-filled, so a missing root yields zeroed
        buckets rather than an empty dict.
        """
        anchor = today or date.today()
        days = self._resolve_window(window_days)
        rollup: dict[str, dict[str, Any]] = {}
        for offset in range(days):
            label = _week_label(anchor - timedelta(days=offset))
            if label not in rollup:
                rollup[label] = _zero_bucket()
        for run in self.runs(window_days=window_days, today=anchor):
            try:
                label = _week_label(date.fromisoformat(run.date))
            except ValueError:  # defensive; run.date comes from a parsed name
                continue
            bucket = rollup.get(label)
            if bucket is None:
                continue
            bucket["runs"] += 1
            bucket["cost_usd"] += run.cost_usd
        return rollup

    # ------------------------------------------------------------------
    # Scan + parse internals
    # ------------------------------------------------------------------

    def _resolve_window(self, window_days: int | None) -> int:
        if window_days is None:
            return self._window_days
        return max(1, int(window_days))

    def _scan(self, window_days: int, today: date) -> list[DelegationRun]:
        """Collect runs from every day-file inside the window.

        The window covers ``window_days`` calendar days inclusive of
        ``today``; files dated outside it (including future-dated ones
        from clock skew) are never opened — this is the bounded-read
        guarantee.
        """
        start = today - timedelta(days=window_days - 1)
        collected: list[DelegationRun] = []
        try:
            slug_dirs = [entry for entry in self._root.iterdir() if entry.is_dir()]
        except OSError:  # missing root, permission problem, root-is-a-file
            return collected
        for slug_dir in sorted(slug_dirs):
            try:
                day_files = list(slug_dir.iterdir())
            except OSError:
                continue
            for day_file in day_files:
                if day_file.suffix != ".jsonl":
                    continue
                try:
                    file_day = date.fromisoformat(day_file.stem)
                except ValueError:
                    continue  # not a <YYYY-MM-DD>.jsonl day-file
                if not (start <= file_day <= today):
                    continue
                collected.extend(self._runs_for_file(day_file, slug_dir.name, file_day))
        return collected

    def _runs_for_file(self, path: Path, slug: str, day: date) -> tuple[DelegationRun, ...]:
        """Parse one day-file through the mtime+size cache."""
        try:
            stat = path.stat()
        except OSError:
            return ()
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        runs = self._parse_file(path, slug, day)
        self._cache[path] = (key, runs)
        return runs

    @staticmethod
    def _parse_file(path: Path, slug: str, day: date) -> tuple[DelegationRun, ...]:
        """Extract ``outbound`` trailer runs from one JSONL day-file.

        Malformed lines (bad JSON, non-dict payloads) and non-outbound
        directions are skipped silently; a half-written trailing line
        from a concurrent daemon append is the expected case, not an
        error worth surfacing per-poll.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ()
        runs: list[DelegationRun] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(record, dict):
                continue
            if record.get("direction") != "outbound":
                continue
            runs.append(_run_from_record(record, slug=slug, day=day))
        return tuple(runs)


def _run_from_record(record: dict[str, Any], *, slug: str, day: date) -> DelegationRun:
    """Build a :class:`DelegationRun` from one outbound record, coercing
    every trailer field defensively (absent/mistyped → zero/None)."""
    raw_model = record.get("model")
    model = raw_model if isinstance(raw_model, str) and raw_model.strip() else None

    cost = _as_float(record.get("cost_usd")) or 0.0

    duration = _as_float(record.get("duration_seconds"))
    if duration is None:
        duration_ms = _as_float(record.get("duration_ms"))
        duration = duration_ms / 1000.0 if duration_ms is not None else 0.0

    tool_calls = record.get("tool_calls")
    if isinstance(tool_calls, list):
        tool_call_count = len(tool_calls)
    elif isinstance(tool_calls, int) and not isinstance(tool_calls, bool):
        tool_call_count = max(0, tool_calls)
    else:
        tool_call_count = 0

    ts = record.get("ts")
    audit_id = record.get("audit_id")
    return DelegationRun(
        slug=slug,
        date=day.isoformat(),
        ts=ts if isinstance(ts, str) else "",
        model=model,
        model_tier=normalize_model(model),
        cost_usd=cost,
        duration_seconds=duration,
        tool_call_count=tool_call_count,
        audit_id=audit_id if isinstance(audit_id, str) else "",
    )
