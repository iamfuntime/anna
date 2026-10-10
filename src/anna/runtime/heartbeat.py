"""Built-in read-only Gmail + Calendar heartbeat (``job_kind: google_heartbeat``).

Deterministic collectors read each configured account/service through the
serialized ``GoogleClients`` lanes (no new transport, keys or vendors, and no
write/send call anywhere).  No model is involved in collection, empty checks or
reminders.  Every required ``(account, service)`` pair yields a typed
``CheckResult`` receipt; an auth/API/rate-limit failure is a non-ok status and
can never read as an empty healthy inbox.

Persistence contract: the job returns a ``HeartbeatCommit`` (provider cursors,
notified-item keys, per-check receipts, alert state).  The scheduler applies it
in the same transaction that records the run result and queues the
notifications, so cursors advance iff the alerts that justify them are durable.
Delivery retries only touch the outbox and never re-run these checks.

Sync/bounds (documented behavior):
* Gmail keeps a ``historyId`` cursor.  No cursor, or a cursor Gmail rejects
  (HTTP 404), triggers a *bounded resync*: the newest unread inbox mail from the
  last ``gmail_bootstrap_hours`` hours, at most ``gmail_max_pages`` pages;
  older unread mail beyond that cap is not alerted.  Message-id dedup makes a
  resync safe.  Incremental history reading is also page-bounded; if pages
  remain, the cursor only advances to the last history record actually
  processed and the check is ``partial`` (``more_pending``).
* Calendar uses a rolling window (now .. now + lookahead) rather than a sync
  token (a token cannot be combined with a time window).  Items dedup on event
  id + ``updated`` version; "starting soon" reminders dedup on event id + start.
  Windows and all-day events use the schedule's timezone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from anna.config import AnnaConfig, HeartbeatConfig
from anna.log import get_logger
from anna.runtime.ledger import HeartbeatCommit, RuntimeLedger, iso, now_utc
from anna.runtime.schedule_types import Schedule
from anna.tools.google_auth import GoogleAuthError
from anna.tools.google_clients import GoogleBackpressureError

OK = "ok"
AUTH = "auth_required"
RATE = "rate_limited"
API = "api_error"
BUSY = "busy"
PARTIAL = "partial"  # check completed what it could but more is pending
BACKPRESSURE = "backpressure"
MISCONF = "misconfigured"  # job cannot run at all (no client / accounts / google disabled)
JOB_KEY = ("*", "job")  # heartbeat_state row for whole-job failures
REANNOUNCE_PREFIX = "⚠️ An earlier alert may not have reached you"
NON_OK = {AUTH, RATE, API, BUSY, PARTIAL, BACKPRESSURE}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")


class InvalidCursor(Exception):
    """The provider rejected the stored cursor (e.g. Gmail history too old)."""


def classify_error(exc: BaseException) -> str:
    """Map an exception to a check status.  Anything unknown is ``api_error``, never ok."""
    if isinstance(exc, GoogleBackpressureError):
        return BUSY
    if isinstance(exc, GoogleAuthError):
        return AUTH
    name = type(exc).__name__
    if name in {"RefreshError", "TransportError"} and "invalid_grant" in str(exc).lower():
        return AUTH
    if name == "RefreshError":
        return AUTH
    status = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    content = getattr(exc, "content", b"") or b""
    body = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
    if status == 429 or "ratelimitexceeded" in body.lower().replace(" ", ""):
        return RATE
    if status in (401, 403):
        return AUTH
    return API


def _status_of(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "resp", None), "status", None) or getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def escape_for_transport(transport: str, text: str) -> str:
    """Neutralise markup in untrusted provider text for the destination.

    Slack treats ``<!channel>``, ``<@U123>`` and ``<url|label>`` as live syntax;
    escaping ``& < >`` (Slack's documented rule) makes sender/subject/title text
    inert.  Telegram messages are sent as plain text (no parse_mode), where no
    escaping is needed or wanted.
    """
    if transport == "slack":
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return text


def heartbeat_min_timeout(cfg: HeartbeatConfig) -> float:
    """Smallest whole-job timeout that cannot cut off healthy per-check budgets (sequential per account)."""
    return cfg.check_timeout_seconds * len(cfg.services) + 30.0


def cfg_budget(cfg: HeartbeatConfig) -> float:
    return cfg.gmail_time_budget_seconds


def clean(text: str, limit: int = 160) -> str:
    """Untrusted provider text: strip control/bidi characters, collapse whitespace, bound length."""
    text = _CTRL.sub("", text or "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Item:
    key: str
    change: str
    text: str
    urgent: bool = False


@dataclass
class CheckResult:
    account: str
    service: str
    status: str
    checked_at: str
    cursor_before: str | None = None
    cursor_after: str | None = None
    items: list[Item] = field(default_factory=list)  # new, not yet notified
    seen: int = 0
    pages: int = 0
    resynced: bool = False
    more_pending: bool = False
    bootstrap_truncated: bool = False  # a bounded resync listed only the newest unread; older ones are NOT pending
    scan_incomplete: str | None = None
    error_class: str | None = None
    error: str | None = None
    alert: str | None = None
    state: dict[str, Any] | None = None

    def receipt(self, schedule_id: str, scheduled_for: str) -> dict[str, Any]:
        body = {
            "account": self.account, "service": self.service, "status": self.status,
            "checked_at": self.checked_at, "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after, "new_items": len(self.items), "seen": self.seen,
            "pages": self.pages, "resynced": self.resynced, "more_pending": self.more_pending,
            "bootstrap_truncated": self.bootstrap_truncated, "scan_incomplete": self.scan_incomplete,
            "error_class": self.error_class, "error": self.error,
        }
        digest = hashlib.sha256(
            json.dumps([schedule_id, scheduled_for, body], sort_keys=True).encode()).hexdigest()
        return {**body, "receipt_hash": digest}


@dataclass
class Notice:
    text: str
    urgent: bool = False
    preescaped: bool = False  # text was already escaped when first queued (re-announcements)


@dataclass
class HeartbeatOutcome:
    status: str  # success | partial | auth_required | failed
    checks: list[CheckResult]
    notices: list[Notice]
    commit: HeartbeatCommit
    summary: dict[str, Any]
    backpressure: bool = False


class Classifier(Protocol):
    """OPTIONAL, separate, budgeted path.  Never consulted to verify a collector."""

    async def annotate(self, items: list[Item]) -> dict[str, str]:  # item.key -> short label
        ...


def plan_alert(prev: dict[str, Any] | None, status: str, now: datetime, cfg: HeartbeatConfig) -> tuple[str | None, dict[str, Any]]:
    """State transition alerting: once on entry, bounded reminders, one recovery notice.

    Returns ``(alert_kind | None, new_state_fields)``.  Transient failures
    (rate limit / API / busy) only alert after N consecutive occurrences, and
    never disable anything.
    """
    prev = prev or {}
    prev_status = prev.get("status", OK)
    failures = int(prev.get("consecutive_failures", 0))
    sent = int(prev.get("alerts_sent", 0))
    last = prev.get("last_alert_at")
    fields: dict[str, Any] = {"status": status, "alerts_sent": sent, "last_alert_at": last}
    if status in (OK, PARTIAL, BACKPRESSURE):
        # ``partial``/backpressure mean the provider answered; they are not outages.
        fields["consecutive_failures"] = 0
        if prev_status in (AUTH, RATE, API, BUSY) and sent > 0:
            fields.update(alerts_sent=0, last_alert_at=None)
            return "recovered", fields
        if prev_status in (AUTH, RATE, API, BUSY):
            fields.update(alerts_sent=0, last_alert_at=None)
        return None, fields
    same = prev_status == status
    failures = failures + 1 if (same or prev_status != OK) else 1
    fields["consecutive_failures"] = failures
    if status in (AUTH, MISCONF):
        first = prev_status != status
    else:
        first = failures == cfg.transient_failures_before_alert and sent == 0
    if first:
        fields.update(alerts_sent=1, last_alert_at=iso(now))
        return "entered", fields
    if sent > 0 and last and sent <= cfg.max_reminders:
        if now - datetime.fromisoformat(last) >= timedelta(hours=cfg.reminder_interval_hours):
            fields.update(alerts_sent=sent + 1, last_alert_at=iso(now))
            return "reminder", fields
    return None, fields


def quiet_until(now: datetime, tz: ZoneInfo, cfg: HeartbeatConfig) -> datetime | None:
    """End of the current quiet window (UTC) if ``now`` falls inside it, else None."""
    q = cfg.quiet_hours
    if not q.enabled:
        return None
    local = now.astimezone(tz)
    sh, sm = map(int, q.start.split(":"))
    eh, em = map(int, q.end.split(":"))
    start_t, end_t = time(sh, sm), time(eh, em)
    cur = local.time()
    if start_t == end_t:
        return None
    if start_t < end_t:  # same-day window
        inside = start_t <= cur < end_t
        end_day = local.date()
    else:  # wraps midnight
        inside = cur >= start_t or cur < end_t
        end_day = local.date() + timedelta(days=1) if cur >= start_t else local.date()
    if not inside:
        return None
    end_local = datetime.combine(end_day, end_t, tzinfo=tz)
    return end_local.astimezone(UTC)


class HeartbeatJob:
    def __init__(self, *, config: AnnaConfig, clients: Any | None, ledger: RuntimeLedger,
                 classifier: Classifier | None = None, clock: Callable[[], datetime] = now_utc) -> None:
        self._config = config
        self._cfg = config.heartbeat
        self._clients = clients
        self._ledger = ledger
        self._classifier = classifier
        self._clock = clock
        self._monotonic = _time.monotonic
        self._log = get_logger("anna.heartbeat")

    # ----------------------------------------------------------------- driver

    def targets(self) -> list[tuple[str, str]]:
        accts = {a.slug: a for a in self._config.google.accounts}
        names = [a for a in accts if not self._cfg.accounts or a in self._cfg.accounts]
        # An account is only checked for the services it was actually authorized for.
        return [(a, s) for a in names for s in self._cfg.services if s in accts[a].services]

    async def run(self, schedule: Schedule, *, scheduled_for: str) -> HeartbeatOutcome:
        now = self._clock()
        tz = ZoneInfo(schedule.timezone)
        targets = self.targets()
        if not self._config.google.enabled or not targets or self._clients is None:
            reason = ("google integration is disabled" if not self._config.google.enabled else
                      "no google accounts selected" if not targets else "no Google client available")
            return self.job_failure(schedule, MISCONF, reason, now, tz)
        by_account: dict[str, list[str]] = {}
        for account, service in targets:
            by_account.setdefault(account, []).append(service)
        per_account = await asyncio.gather(*(self._check_account(schedule, a, svcs, now, tz)
                                             for a, svcs in by_account.items()))
        checks = [c for group in per_account for c in group]
        return await self._finish(schedule, scheduled_for, checks, now, tz)

    def job_failure(self, schedule: Schedule, status: str, reason: str, now: datetime, tz: ZoneInfo | None = None
                    ) -> HeartbeatOutcome:
        """A whole-job failure (timeout, exception, nothing to run) goes through the same
        transition alerting as a failed check, so it can never look like a quiet healthy tick."""
        cfg = self._cfg
        prev = self._ledger.heartbeat_state(schedule.id, *JOB_KEY)
        kind, fields = plan_alert(prev, status, now, cfg)
        state = {"account": JOB_KEY[0], "service": JOB_KEY[1], "cursor": (prev or {}).get("cursor"),
                 "last_checked_at": iso(now), "last_ok_at": (prev or {}).get("last_ok_at"),
                 "resync_count": int((prev or {}).get("resync_count", 0)), **fields}
        commit = HeartbeatCommit(states=[state], retention_days=cfg.notified_retention_days)
        notices = []
        if kind == "recovered":
            notices.append(Notice("✅ The Google heartbeat is working again."))
        elif kind:
            prefix = "⚠️ Reminder: " if kind == "reminder" else "⚠️ "
            notices.append(Notice(f"{prefix}The Google heartbeat could not complete ({clean(reason, 120)}). "
                                  "I can't confirm there is nothing new in your mail or calendar until that is fixed."))
        summary = {"status": "failed", "error": clean(reason, 200), "checks": [], "job_alert": kind,
                   "notifications": [n.text[:200] for n in notices]}
        return HeartbeatOutcome("failed", [], notices, commit, summary)

    async def _check_account(self, schedule: Schedule, account: str, services: list[str], now: datetime,
                             tz: ZoneInfo) -> list[CheckResult]:
        results: list[CheckResult] = []
        for service in services:  # same account lane: sequential
            results.append(await self._guarded(schedule, account, service, now, tz))
        return results

    async def _guarded(self, schedule: Schedule, account: str, service: str, now: datetime,
                       tz: ZoneInfo) -> CheckResult:
        state = self._ledger.heartbeat_state(schedule.id, account, service) or {}
        res = CheckResult(account, service, OK, iso(now), cursor_before=state.get("cursor"))
        try:
            fn = self._gmail if service == "gmail" else self._calendar
            await asyncio.wait_for(fn(schedule, res, state, now, tz), self._cfg.check_timeout_seconds)
        except InvalidCursor:  # defensive: handled inside the collector
            res.status, res.error_class = API, "InvalidCursor"
        except Exception as exc:  # noqa: BLE001 - every failure becomes a typed receipt
            res.status = classify_error(exc) if not isinstance(exc, TimeoutError) else API
            res.error_class = type(exc).__name__
            res.error = clean(str(exc), 200)
            res.items, res.cursor_after = [], state.get("cursor")
        return res

    async def _call(self, account: str, api: str, op: Callable[[Any], Any]) -> Any:
        return await self._clients.run(account, api, op)  # type: ignore[union-attr]

    # ------------------------------------------------------------------ gmail

    async def _gmail(self, schedule: Schedule, res: CheckResult, state: dict[str, Any], now: datetime,
                     tz: ZoneInfo) -> None:
        account = res.account
        cursor = state.get("cursor")
        ids: list[str] = []
        head: str | None = None
        rec_of: dict[str, int] = {}
        if cursor:
            try:
                ids, head, res.more_pending, rec_of = await self._gmail_history(account, cursor, res)
            except InvalidCursor:
                res.resynced, cursor = True, None
        if not cursor:
            profile = await self._call(account, "gmail", lambda s: s.users().getProfile(userId="me").execute())
            if not isinstance(profile, dict) or "historyId" not in profile:
                raise ValueError("malformed gmail profile response")
            head = str(profile["historyId"])  # read BEFORE listing so nothing falls in the gap
            ids, truncated = await self._gmail_bootstrap(account, res)
            # The cursor jumps to the profile head, so anything beyond the page cap is NOT
            # continued on the next tick; report that truthfully instead of "more pending".
            res.bootstrap_truncated = truncated
            res.more_pending = truncated
            res.cursor_after = head
        else:
            res.cursor_after = head
        res.seen = len(ids)
        found: list[dict[str, Any]] = []
        deadline = self._monotonic() + cfg_budget(self._cfg)
        for pos, mid in enumerate(ids):
            if self._monotonic() > deadline:
                # Total time budget: stop fetching.  On the history path the cursor must not pass the
                # unfetched messages, so step back to just before the first unfetched record.
                res.more_pending = True
                res.scan_incomplete = f"time budget reached after {pos}/{len(ids)} messages"
                if rec_of and not res.bootstrap_truncated:
                    first = min(rec_of.get(m, 10**18) for m in ids[pos:])
                    floor = int(cursor) if cursor else 0
                    res.cursor_after = str(max(floor, first - 1))
                break
            try:
                msg = await self._call(account, "gmail", lambda s, m=mid: s.users().messages().get(  # type: ignore[misc]
                    userId="me", id=m, format="metadata", metadataHeaders=["From", "Subject", "Date"]).execute())
            except Exception as exc:  # noqa: BLE001
                if _status_of(exc) == 404:
                    continue  # deleted between listing and fetching
                raise
            if not isinstance(msg, dict) or "id" not in msg:
                raise ValueError("malformed gmail message response")
            labels = set(msg.get("labelIds") or [])
            if "UNREAD" in labels and "INBOX" in labels:
                found.append(msg)
        pairs = [(f"gmail:{m['id']}", "added") for m in found]
        done = self._ledger.heartbeat_notified(schedule.id, account, "gmail", pairs)
        for m in found:
            if (f"gmail:{m['id']}", "added") in done:
                continue
            headers = {h.get("name", "").lower(): h.get("value", "") for h in (m.get("payload", {}).get("headers") or [])}
            line = f"{clean(headers.get('from', '(no sender)'), 60)} — {clean(headers.get('subject', '(no subject)'), 100)}"
            res.items.append(Item(f"gmail:{m['id']}", "added", line))
        if res.more_pending:
            res.status = PARTIAL

    async def _gmail_history(self, account: str, cursor: str, res: CheckResult
                             ) -> tuple[list[str], str | None, bool, dict[str, int]]:
        cfg = self._cfg
        ids: list[str] = []
        rec_of: dict[str, int] = {}
        token: str | None = None
        last_record: int | None = None
        head: str | None = None
        more = False
        record_pages = empty_pages = 0
        while True:
            try:
                resp = await self._call(account, "gmail", lambda s, t=token: s.users().history().list(  # type: ignore[misc]
                    userId="me", startHistoryId=cursor, historyTypes=["messageAdded"], labelId="INBOX",
                    maxResults=cfg.gmail_page_size, pageToken=t).execute())
            except Exception as exc:  # noqa: BLE001
                if _status_of(exc) == 404:
                    raise InvalidCursor(str(exc)) from exc
                raise
            if not isinstance(resp, dict):
                raise ValueError("malformed gmail history response")
            res.pages += 1
            records = resp.get("history") or []
            for rec in records:
                try:
                    rid = int(rec.get("id", 0))
                    last_record = max(last_record or 0, rid)
                except (TypeError, ValueError):
                    rid = 0
                for added in rec.get("messagesAdded") or []:
                    mid = (added.get("message") or {}).get("id")
                    if mid and mid not in ids:
                        ids.append(mid)
                        rec_of[mid] = rid
            head = str(resp["historyId"]) if resp.get("historyId") else head
            token = resp.get("nextPageToken")
            if not token:
                break
            # Pages without records cost little but must still be bounded, and are reported honestly.
            if records:
                record_pages += 1
            else:
                empty_pages += 1
            if record_pages >= cfg.gmail_max_pages or empty_pages >= cfg.gmail_max_empty_pages:
                more = True
                break
        if more:  # do not skip history we did not read
            head = str(last_record) if last_record is not None else cursor
            if last_record is None:
                res.scan_incomplete = f"{empty_pages} history pages without records; scan incomplete"
        return ids, head or cursor, more, rec_of

    async def _gmail_bootstrap(self, account: str, res: CheckResult) -> tuple[list[str], bool]:
        cfg = self._cfg
        query = f"is:unread in:inbox newer_than:{cfg.gmail_bootstrap_hours}h"
        ids: list[str] = []
        token: str | None = None
        more = False
        for _page_no in range(cfg.gmail_max_pages):
            resp = await self._call(account, "gmail", lambda s, t=token: s.users().messages().list(  # type: ignore[misc]
                userId="me", q=query, maxResults=cfg.gmail_page_size, pageToken=t).execute())
            if not isinstance(resp, dict):
                raise ValueError("malformed gmail list response")
            res.pages += 1
            ids.extend(m["id"] for m in resp.get("messages") or [] if "id" in m)
            token = resp.get("nextPageToken")
            if not token:
                break
        else:
            more = bool(token)
        return ids, more

    # --------------------------------------------------------------- calendar

    async def _calendar(self, schedule: Schedule, res: CheckResult, state: dict[str, Any], now: datetime,
                        tz: ZoneInfo) -> None:
        cfg, account = self._cfg, res.account
        window_end = now + timedelta(hours=cfg.calendar_lookahead_hours)
        events: list[dict[str, Any]] = []
        token: str | None = None
        for _page_no in range(cfg.calendar_max_pages):
            resp = await self._call(account, "calendar", lambda s, t=token: s.events().list(  # type: ignore[misc]
                calendarId="primary", timeMin=now.isoformat(), timeMax=window_end.isoformat(), singleEvents=True,
                orderBy="startTime", maxResults=cfg.calendar_page_size, pageToken=t).execute())
            if not isinstance(resp, dict):
                raise ValueError("malformed calendar response")
            res.pages += 1
            events.extend(e for e in resp.get("items") or [] if isinstance(e, dict))
            token = resp.get("nextPageToken")
            if not token:
                break
        else:
            res.more_pending = bool(token)
        res.seen = len(events)
        # "Checked through" marker; not a provider token (see module docstring).
        res.cursor_after = iso(window_end) if not res.more_pending else res.cursor_before
        live = [e for e in events if e.get("status") != "cancelled" and e.get("id")]
        pairs: list[tuple[str, str]] = []
        for e in live:
            pairs.append((f"cal:{e['id']}", str(e.get("updated") or e.get("etag") or "v0")))
            start = self._start(e, tz)
            if start is not None and e.get("start", {}).get("dateTime"):
                pairs.append((f"calrem:{e['id']}", iso(start)))
        done = self._ledger.heartbeat_notified(schedule.id, account, "calendar", pairs)
        for e in live:
            start = self._start(e, tz)
            title = clean(e.get("summary") or "(no title)", 100)
            when = start.astimezone(tz).strftime("%a %H:%M") if start and e.get("start", {}).get("dateTime") else (
                start.astimezone(tz).strftime("%a (all day)") if start else "?")
            change = str(e.get("updated") or e.get("etag") or "v0")
            if (f"cal:{e['id']}", change) not in done:
                res.items.append(Item(f"cal:{e['id']}", change, f"{when} {title}"))
            if start is not None and e.get("start", {}).get("dateTime"):
                until = start - now
                rem_key = (f"calrem:{e['id']}", iso(start))
                if timedelta(0) < until <= timedelta(minutes=cfg.reminder_minutes) and rem_key not in done:
                    mins = int(until.total_seconds() // 60)
                    res.items.append(Item(rem_key[0], rem_key[1], f"{when} {title} (in {mins} min)",
                                          urgent=until <= timedelta(minutes=cfg.urgent_minutes)))
        if res.more_pending:
            res.status = PARTIAL

    @staticmethod
    def _start(event: dict[str, Any], tz: ZoneInfo) -> datetime | None:
        start = event.get("start") or {}
        try:
            if start.get("dateTime"):
                dt = datetime.fromisoformat(str(start["dateTime"]).replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=tz)
            if start.get("date"):
                return datetime.combine(date.fromisoformat(str(start["date"])), time(0), tzinfo=tz)
        except ValueError:
            return None
        return None

    # ----------------------------------------------------------------- finish

    async def _finish(self, schedule: Schedule, scheduled_for: str, checks: list[CheckResult], now: datetime,
                      tz: ZoneInfo) -> HeartbeatOutcome:
        cfg = self._cfg
        due = self._ledger.pending_outbox_count(schedule.id, now=now)
        deferred = self._ledger.deferred_outbox_count(schedule.id, now=now)
        # Quiet-hours rows are deferred, not "due": they bound separately and never starve urgent
        # reminders or state-change alerts, which are exempt from backpressure.
        backpressure = due >= cfg.max_pending_notifications or deferred >= cfg.max_deferred_notifications
        commit = HeartbeatCommit(retention_days=cfg.notified_retention_days)
        quiet_end = quiet_until(now, tz, cfg)
        notices: list[Notice] = []
        classifier_labels: dict[str, str] = {}
        if self._classifier is not None:
            candidates = [i for c in checks for i in c.items]
            if candidates:
                try:  # optional, separate, failure-isolated
                    classifier_labels = await asyncio.wait_for(self._classifier.annotate(candidates), 30)
                except Exception as exc:  # noqa: BLE001
                    self._log.warning("heartbeat.classifier_failed", error=str(exc)[:200])
        for c in checks:
            prev = self._ledger.heartbeat_state(schedule.id, c.account, c.service)
            kind, fields = plan_alert(prev, c.status, now, cfg)
            c.alert = kind
            if kind:
                notices.append(Notice(self._alert_text(c, kind)))
            deliver, advance = list(c.items), c.status in (OK, PARTIAL)
            if backpressure and advance:
                # Ordinary backlog is bounded: withhold non-urgent items (re-read later, not lost),
                # but urgent reminders still go out.
                urgent = [i for i in c.items if i.urgent]
                if len(urgent) < len(c.items):
                    c.status = BACKPRESSURE
                    deliver, advance = urgent, False
            state = {
                "account": c.account, "service": c.service,
                "cursor": c.cursor_after if advance else (prev or {}).get("cursor"),
                "last_checked_at": c.checked_at,
                "last_ok_at": c.checked_at if c.status in (OK, PARTIAL) else (prev or {}).get("last_ok_at"),
                "resync_count": int((prev or {}).get("resync_count", 0)) + (1 if c.resynced else 0),
                **fields,
            }
            c.state = state
            commit.states.append(state)
            for it in deliver:
                commit.notified.append((c.account, c.service, it.key, it.change))
            if deliver:
                shown = c.items
                c.items = deliver
                notices.append(self._digest(c, classifier_labels))
                c.items = shown
            commit.receipts.append(c.receipt(schedule.id, scheduled_for))
        # Whole-job transition: a healthy tick closes any earlier job-level failure.
        prev_job = self._ledger.heartbeat_state(schedule.id, *JOB_KEY)
        if prev_job is not None:
            jkind, jfields = plan_alert(prev_job, OK, now, cfg)
            commit.states.append({"account": JOB_KEY[0], "service": JOB_KEY[1], "cursor": prev_job.get("cursor"),
                                  "last_checked_at": iso(now), "last_ok_at": iso(now),
                                  "resync_count": int(prev_job.get("resync_count", 0)), **jfields})
            if jkind == "recovered":
                notices.append(Notice("✅ The Google heartbeat is working again."))
        # Deliveries that ended uncertain/failed must not vanish silently: say so on the next tick.
        stranded = self._ledger.unannounced_undelivered(schedule.id)
        for row in stranded:
            if row["text"].startswith(REANNOUNCE_PREFIX):
                commit.announced_outbox_ids.append(row["id"])  # never re-announce a re-announcement
                continue
            commit.announced_outbox_ids.append(row["id"])
            dest = schedule.destination
            if (row["transport"], row["channel"]) != (dest.transport, dest.channel):
                # The destination changed since that message was queued: never copy its content to a
                # different channel (disclosure) or re-use text escaped for another transport.
                notices.append(Notice(f"{REANNOUNCE_PREFIX} ({row['status']} delivery to a previous destination). "
                                      "Its content is not repeated here."))
                continue
            notices.append(Notice(f"{REANNOUNCE_PREFIX} ({row['status']} delivery). Repeating it:\n{row['text'][:600]}",
                                  preescaped=True))
        status = self._job_status(checks, backpressure)
        summary = {
            "status": status,
            "checks": [{k: v for k, v in c.receipt(schedule.id, scheduled_for).items()} | {"alert": c.alert}
                       for c in checks],
            "notifications": [n.text[:200] for n in notices],
            "backpressure": backpressure,
            "quiet_until": iso(quiet_end) if quiet_end else None,
        }
        return HeartbeatOutcome(status, checks, notices, commit, summary, backpressure)

    @staticmethod
    def _job_status(checks: list[CheckResult], backpressure: bool) -> str:
        if not checks:
            return "failed"
        ok = [c for c in checks if c.status == OK]
        if len(ok) == len(checks) and not backpressure:
            return "success"
        usable = [c for c in checks if c.status in (OK, PARTIAL, BACKPRESSURE)]
        if usable:
            return "partial"
        if any(c.status == AUTH for c in checks) and all(c.status in (AUTH,) for c in checks):
            return "auth_required"
        return "failed"

    def _alert_text(self, c: CheckResult, kind: str) -> str:
        label = f"{c.service} ({c.account})"
        if kind == "recovered":
            return f"✅ Google {label} is working again."
        why = {AUTH: "needs re-authorization (run the setup CLI for this account)",
               RATE: "is being rate limited", API: "is failing", BUSY: "is busy"}.get(c.status, "has a problem")
        prefix = "⚠️ Reminder: " if kind == "reminder" else "⚠️ "
        return f"{prefix}Google {label} {why}. I can't confirm there is nothing new there until that is fixed."

    def _digest(self, c: CheckResult, labels: dict[str, str]) -> Notice:
        cfg = self._cfg
        icon = "📬" if c.service == "gmail" else "📅"
        head = (f"{icon} {c.account}: {len([i for i in c.items if not i.key.startswith('calrem')]) or len(c.items)} "
                f"{'new unread' if c.service == 'gmail' else 'calendar update(s)'}")
        lines = []
        for it in c.items[: cfg.max_listed_items]:
            tag = f"[{labels[it.key]}] " if it.key in labels else ""
            lines.append(f"• {tag}{it.text}")
        extra = len(c.items) - len(lines)
        if extra > 0:
            lines.append(f"…and {extra} more")
        if c.bootstrap_truncated:
            lines.append("(resync: only the newest unread are listed; older unread mail from this resync is not shown)")
        elif c.more_pending:
            lines.append("(more pending; continuing on the next check)")
        return Notice("\n".join([head, *lines]), urgent=any(i.urgent for i in c.items))

    # -------------------------------------------------------------- outbox

    def outbox_specs(self, schedule: Schedule, outcome: HeartbeatOutcome, now: datetime, tz: ZoneInfo
                     ) -> list[tuple[Any, ...]]:
        """Notices -> outbox rows; non-urgent ones are held until quiet hours end."""
        quiet_end = quiet_until(now, tz, self._cfg)
        dest = schedule.destination
        specs = []
        for n in outcome.notices:
            not_before = iso(quiet_end) if quiet_end and not n.urgent else None
            text = n.text if n.preescaped else escape_for_transport(dest.transport, n.text)
            specs.append((dest.transport, dest.channel, text, not_before))
        return specs


__all__ = ["Classifier", "HeartbeatJob", "HeartbeatOutcome", "classify_error", "plan_alert", "quiet_until", "clean"]
