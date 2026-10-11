"""``anna doctor``: honest, offline, read-only health report.

Everything here reads local files only: the daemon's status snapshot
(``state/runtime-status.json``), the runtime ledger and the memory database
(opened ``mode=ro``).  It never reads secret values (only whether a credential
variable is set), never calls a provider, never runs a model query and never
modifies state.  A subsystem is only ``ok`` when something *observed* says so;
"enabled in config" alone yields ``unknown``.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anna.config import AnnaConfig
from anna.runtime.state_read import ReadOnlyDB, Unavailable
from anna.runtime.status import read_snapshot, redact

OK, WARN, FAIL, UNKNOWN, DISABLED = "ok", "warn", "fail", "unknown", "disabled"
SDK_STALE_HOURS = 24.0
_ORDER = {OK: 0, DISABLED: 0, UNKNOWN: 1, WARN: 1, FAIL: 2}
_CRED_ENV = {"slack": ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"), "telegram": ("TELEGRAM_BOT_TOKEN",)}


@dataclass
class Check:
    id: str
    title: str
    status: str
    summary: str
    action: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _age(now: datetime, value: str | None) -> float | None:
    dt = _parse(value)
    return None if dt is None else max(0.0, (now - dt).total_seconds())


def human(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _pid_alive(pid: Any) -> bool | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def collect(config: AnnaConfig, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    snap = read_snapshot(config.state_dir / "runtime-status.json")
    stale_after = max(2 * config.watchdog.interval_seconds + 30, 120)
    checks: list[Check] = []
    checks.append(_daemon(snap, now, stale_after))
    checks.extend(_transports(config, snap, now, stale_after))
    checks.append(_sdk(config, snap, now))
    ledger = _open(config.state_dir / "runtime.sqlite3")
    checks.extend(_google(config, ledger, now))
    checks.append(_heartbeat(config, ledger, now))
    checks.extend(_runs_and_outbox(ledger, now))
    checks.append(_memory(config, snap, now))
    if isinstance(ledger, ReadOnlyDB):
        ledger.close()
    worst = max((_ORDER[c.status] for c in checks), default=0)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "overall": {0: OK, 1: WARN, 2: FAIL}[worst],
        "offline": True,
        "checks": [asdict(c) for c in checks],
    }


def _open(path: Path) -> ReadOnlyDB | str:
    try:
        return ReadOnlyDB(path)
    except Unavailable as exc:
        return str(exc)


# ------------------------------------------------------------------ daemon / transports / sdk

def _daemon(snap: dict[str, Any] | None, now: datetime, stale_after: float) -> Check:
    if snap is None:
        return Check("daemon", "Daemon", UNKNOWN, "no status file: the daemon has never reported (not started, "
                     "started before this version, or a different ANNA_HOME)",
                     "start or restart the daemon; it writes state/runtime-status.json")
    alive = _pid_alive(snap.get("pid"))
    written = _age(now, snap.get("written_at"))
    details = {"pid": snap.get("pid"), "started_at": snap.get("started_at"), "status_age": human(written)}
    if snap.get("stopped_at"):
        return Check("daemon", "Daemon", WARN, f"stopped cleanly {human(_age(now, snap['stopped_at']))}",
                     "start the daemon", details)
    if alive is False:
        return Check("daemon", "Daemon", FAIL, "process is gone and did not stop cleanly (crashed or killed)",
                     "start the daemon and check the journal", details)
    if written is None or written > stale_after:
        return Check("daemon", "Daemon", FAIL, f"alive but status is {human(written)} (stale): it may be hung",
                     "restart the daemon", details)
    return Check("daemon", "Daemon", OK, f"running (pid {snap.get('pid')}), status {human(written)}", "", details)


def _transports(config: AnnaConfig, snap: dict[str, Any] | None, now: datetime, stale_after: float) -> list[Check]:
    out: list[Check] = []
    recorded = (snap or {}).get("transports", {}) if snap else {}
    for name in ("slack", "telegram", "cli"):
        if not getattr(config.transports, name).enabled:
            out.append(Check(f"transport.{name}", f"{name.title()} transport", DISABLED, "disabled in config"))
            continue
        creds = {var: bool(os.environ.get(var)) for var in _CRED_ENV.get(name, ())}
        missing = [v for v, present in creds.items() if not present]
        details: dict[str, Any] = {"credentials_present": creds}
        if missing:
            out.append(Check(f"transport.{name}", f"{name.title()} transport", FAIL,
                             f"credentials missing: {', '.join(missing)}", "set them in .env and restart", details))
            continue
        entry = recorded.get(name)
        if not entry:
            out.append(Check(f"transport.{name}", f"{name.title()} transport", UNKNOWN,
                             "configured" + (", credentials present" if creds else "") +
                             ", but the daemon has never verified it", "start/restart the daemon", details))
            continue
        checked = _age(now, entry.get("last_checked_at"))
        details.update(receiver_alive=entry.get("receiver_alive"), api_ok=entry.get("api_ok"),
                       last_checked=human(checked), last_healthy=human(_age(now, entry.get("last_healthy_at"))),
                       last_error=entry.get("last_error"))
        receiver, api = entry.get("receiver_alive"), entry.get("api_ok")
        if receiver is False:
            tail = "API credentials work" if api else ("API credentials failed" if api is False else "API not checked")
            out.append(Check(f"transport.{name}", f"{name.title()} transport", FAIL,
                             f"receiver is NOT running ({tail}): messages are not being received",
                             "restart the daemon (the watchdog restarts a failed receiver after 3 failed pings)", details))
        elif checked is None or checked > stale_after:
            out.append(Check(f"transport.{name}", f"{name.title()} transport", WARN,
                             f"last verified {human(checked)} (stale)", "check the daemon", details))
        elif api is False:
            out.append(Check(f"transport.{name}", f"{name.title()} transport", FAIL,
                             "receiver running but the API call failed (token revoked or network down)",
                             "re-check the token / connectivity", details))
        elif entry.get("healthy"):
            out.append(Check(f"transport.{name}", f"{name.title()} transport", OK,
                             f"receiver running, verified {human(checked)}", "", details))
        else:
            out.append(Check(f"transport.{name}", f"{name.title()} transport", WARN,
                             f"unhealthy at last check {human(checked)}", "check the daemon log", details))
    return out


def _sdk(config: AnnaConfig, snap: dict[str, Any] | None, now: datetime) -> Check:
    sdk = (snap or {}).get("sdk", {}) if snap else {}
    ok_at, err_at = sdk.get("last_success_at"), sdk.get("last_error_at")
    details = {"auth_mode": config.auth.mode, "last_success": human(_age(now, ok_at)) if ok_at else "never",
               "last_error": human(_age(now, err_at)) if err_at else "never",
               "last_error_detail": sdk.get("last_error")}
    note = "install check only constructs a client; it does not verify credentials"
    if snap is None or (not ok_at and not err_at):
        return Check("sdk", "Claude SDK", UNKNOWN, "untested: no real turn has completed since the daemon started "
                     f"({note})", "send a message, or run `anna ask hello` (this uses your subscription/API)", details)
    ok_dt, err_dt = _parse(ok_at), _parse(err_at)
    if err_dt and (not ok_dt or err_dt > ok_dt):
        return Check("sdk", "Claude SDK", FAIL, f"last turn FAILED {human(_age(now, err_at))}"
                     f"{' (after last success ' + human(_age(now, ok_at)) + ')' if ok_at else ''}",
                     "check auth (`claude` login or API key) and the daemon log", details)
    age = _age(now, ok_at)
    if age is not None and age > SDK_STALE_HOURS * 3600:
        return Check("sdk", "Claude SDK", WARN, f"last verified success {human(age)} (stale, >{int(SDK_STALE_HOURS)}h)",
                     "send a message to re-verify", details)
    return Check("sdk", "Claude SDK", OK, f"last real turn succeeded {human(age)}", "", details)


# ------------------------------------------------------------------ google / heartbeat

def _google(config: AnnaConfig, ledger: ReadOnlyDB | str, now: datetime) -> list[Check]:
    if not config.google.enabled or not config.google.accounts:
        return [Check("google", "Google accounts", DISABLED, "google integration disabled or no accounts")]
    out: list[Check] = []
    for acct in config.google.accounts:
        for service in ("gmail", "calendar"):
            cid = f"google.{acct.slug}.{service}"
            title = f"Google {acct.slug} / {service}"
            row = None
            if isinstance(ledger, ReadOnlyDB) and ledger.has_table("heartbeat_receipts"):
                row = ledger.one("SELECT status, checked_at, receipt_hash FROM heartbeat_receipts WHERE account=? "
                                 "AND service=? ORDER BY checked_at DESC LIMIT 1", (acct.slug, service))
            if row is None:
                why = "heartbeat is disabled" if not config.heartbeat.enabled else "no heartbeat receipt yet"
                out.append(Check(cid, title, UNKNOWN, f"never verified ({why}); being listed in config proves nothing",
                                 "enable the heartbeat and let it run once" if not config.heartbeat.enabled else "wait for the next heartbeat"))
                continue
            age = _age(now, row["checked_at"])
            details = {"last_receipt_status": row["status"], "checked": human(age), "receipt": row["receipt_hash"][:12]}
            st = row["status"]
            if st == "auth_required":
                out.append(Check(cid, title, FAIL, f"needs re-authorization (checked {human(age)})",
                                 f"run: python -m anna.setup.google_auth add {acct.slug}", details))
            elif st in ("rate_limited", "busy", "api_error"):
                out.append(Check(cid, title, WARN, f"last check: {st} ({human(age)})", "usually transient; see heartbeat", details))
            elif st in ("ok", "partial", "backpressure") and age is not None and age > 6 * 3600:
                out.append(Check(cid, title, WARN, f"last verified {st} {human(age)} (stale)", "is the heartbeat running?", details))
            else:
                out.append(Check(cid, title, OK if st == "ok" else WARN, f"last receipt {st}, {human(age)}", "", details))
    return out


def _heartbeat(config: AnnaConfig, ledger: ReadOnlyDB | str, now: datetime) -> Check:
    if not config.heartbeat.enabled:
        return Check("heartbeat", "Heartbeat", DISABLED, "heartbeat.enabled is false (opt-in)")
    if not isinstance(ledger, ReadOnlyDB):
        return Check("heartbeat", "Heartbeat", UNKNOWN, f"runtime ledger unavailable: {ledger}", "start the daemon once")
    row = ledger.one("SELECT id, exec_status, finished_at, detail FROM schedule_occurrences WHERE job_kind='google_heartbeat' "
                     "AND exec_status NOT IN ('pending','claimed','running','skipped_overlap','skipped_misfire') "
                     "ORDER BY id DESC LIMIT 1")
    if row is None:
        return Check("heartbeat", "Heartbeat", UNKNOWN, "enabled but no heartbeat run has finished yet",
                     "create a google_heartbeat schedule (see docs) and wait for a tick")
    age = _age(now, row["finished_at"])
    status = {"succeeded": (OK, "complete"), "partial": (WARN, "PARTIAL"), "auth_required": (FAIL, "auth required"),
              "failed": (FAIL, "FAILED"), "timeout": (FAIL, "timed out"), "unknown": (WARN, "unknown outcome")}.get(
        row["exec_status"], (WARN, row["exec_status"]))
    return Check("heartbeat", "Heartbeat", status[0], f"last run {status[1]} {human(age)}",
                 "" if status[0] == OK else "see Google account checks above", {"run": row["id"], "status": row["exec_status"]})


def _runs_and_outbox(ledger: ReadOnlyDB | str, now: datetime) -> list[Check]:
    if not isinstance(ledger, ReadOnlyDB):
        return [Check("runs", "Scheduled runs", UNKNOWN, f"runtime ledger unavailable ({ledger}); never created or "
                      "daemon not started", "start the daemon"),
                Check("outbox", "Deliveries", UNKNOWN, "runtime ledger unavailable")]
    out: list[Check] = []
    rows = ledger.rows("SELECT exec_status, COUNT(*) n, MIN(COALESCE(started_at, created_at)) oldest "
                       "FROM schedule_occurrences WHERE exec_status IN ('pending','claimed','running','unknown','blocked') "
                       "GROUP BY exec_status")
    counts = {r["exec_status"]: (r["n"], _age(now, r["oldest"])) for r in rows}
    attention = counts.get("unknown", (0, None))[0] + counts.get("blocked", (0, None))[0]
    detail = {k: {"count": n, "oldest": human(a)} for k, (n, a) in counts.items()}
    stuck = ledger.one("SELECT COUNT(*) n FROM schedule_occurrences WHERE exec_status IN ('claimed','running') "
                       "AND lease_expires_at<?", (now.isoformat(timespec="microseconds"),))
    if attention:
        out.append(Check("runs", "Scheduled runs", WARN, f"{attention} run(s) with UNKNOWN/blocked outcome need an operator decision "
                         "(side effects unverified; they are never re-run automatically)",
                         "open the dashboard /repairs and resolve each after checking", detail))
    elif stuck and stuck["n"]:
        out.append(Check("runs", "Scheduled runs", WARN, f"{stuck['n']} run(s) past their lease; the daemon will recover them",
                         "check the daemon is running", detail))
    else:
        total = sum(n for n, _ in counts.values())
        out.append(Check("runs", "Scheduled runs", OK, "no unknown/blocked runs" + (f"; {total} in flight/queued" if total else ""),
                         "", detail))
    if not ledger.has_table("schedule_outbox"):
        out.append(Check("outbox", "Deliveries", UNKNOWN, "outbox table missing"))
        return out
    orows = ledger.rows("SELECT status, COUNT(*) n, MIN(created_at) oldest FROM schedule_outbox "
                        "WHERE status IN ('pending','sending','uncertain','failed') GROUP BY status")
    oc = {r["status"]: (r["n"], _age(now, r["oldest"])) for r in orows}
    odetail = {k: {"count": n, "oldest": human(a)} for k, (n, a) in oc.items()}
    bad = oc.get("uncertain", (0, None))[0] + oc.get("failed", (0, None))[0]
    if bad:
        out.append(Check("outbox", "Deliveries", WARN, f"{bad} delivery(ies) uncertain/failed "
                         f"(oldest {human(max(a for k, (n, a) in oc.items() if k in ('uncertain', 'failed') and a is not None))}); "
                         "uncertain sends are never resent automatically",
                         "dashboard /repairs: confirm whether each arrived, then mark delivered / resend / abandon", odetail))
    elif oc.get("pending", (0, None))[0]:
        n, a = oc["pending"]
        status = WARN if a is not None and a > 3600 else OK
        out.append(Check("outbox", "Deliveries", status, f"{n} queued, oldest {human(a)}", "", odetail))
    else:
        out.append(Check("outbox", "Deliveries", OK, "nothing pending, uncertain or failed", "", odetail))
    return out


def _memory(config: AnnaConfig, snap: dict[str, Any] | None, now: datetime) -> Check:
    if not config.memory.enabled:  # kill switch: do not open (or even look for) the memory database
        return Check("memory", "Memory", DISABLED,
                     "disabled by memory.enabled: false (no memory database is opened; core/MEMORY.md file memory "
                     "is a separate mechanism and is unaffected)", "")
    try:
        db = ReadOnlyDB(config.state_dir / "memory.sqlite3")
    except Unavailable:
        return Check("memory", "Memory", UNKNOWN, "no memory database yet (nothing remembered)", "")
    try:
        facts = {r["status"]: r["n"] for r in db.rows("SELECT status, COUNT(*) n FROM facts GROUP BY status")}
        details: dict[str, Any] = {"facts": facts}
        status, bits = OK, [f"{facts.get('active', 0)} active fact(s)"]
        if db.has_table("extraction_jobs"):
            jobs = {r["status"]: (r["n"], _age(now, r["oldest"])) for r in db.rows(
                "SELECT status, COUNT(*) n, MIN(created_at) oldest FROM extraction_jobs GROUP BY status")}
            details["extraction_jobs"] = {k: {"count": n, "oldest": human(a)} for k, (n, a) in jobs.items()}
            cands = {r["status"]: r["n"] for r in db.rows("SELECT status, COUNT(*) n FROM memory_candidates GROUP BY status")}
            details["candidates"] = cands
            pend, dead = jobs.get("pending", (0, None)), jobs.get("dead", (0, None))[0]
            bits.append(f"extraction queue: {pend[0]} pending, {jobs.get('running', (0, None))[0]} running, {dead} dead")
            if dead:
                status = WARN
                errs = db.rows("SELECT last_error FROM extraction_jobs WHERE status='dead' AND last_error IS NOT NULL "
                               "ORDER BY id DESC LIMIT 3")
                details["recent_errors"] = [redact(r["last_error"], 60) for r in errs]
            if cands.get("conflict") or cands.get("held"):
                bits.append(f"{cands.get('conflict', 0)} conflict / {cands.get('held', 0)} held candidate(s) not stored")
        runner = (snap or {}).get("memory_extraction") if snap else None
        if config.memory.extraction.enabled:
            if not runner or not runner.get("running"):
                status = WARN
                reason = (runner or {}).get("blocked_reason") or "daemon has not reported the extraction runner"
                bits.append(f"extraction enabled in config but NOT running: {reason}")
        return Check("memory", "Memory", status, "; ".join(bits), "" if status == OK else "see details / dashboard /memory", details)
    except Unavailable as exc:
        return Check("memory", "Memory", UNKNOWN, f"memory database unreadable: {exc}")
    finally:
        db.close()


# ------------------------------------------------------------------ rendering

_MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", UNKNOWN: "[ ?? ]", DISABLED: "[ -- ]"}


def render_plain(report: dict[str, Any]) -> str:
    lines = [f"anna doctor - {report['generated_at']} (offline, read-only)", f"overall: {report['overall'].upper()}", ""]
    for c in report["checks"]:
        lines.append(f"{_MARK[c['status']]} {c['title']}: {c['summary']}")
        if c.get("action"):
            lines.append(f"        -> {c['action']}")
    lines += ["", "legend: ok=verified, warn=degraded, FAIL=broken, ??=not verified (not assumed healthy), --=disabled"]
    return "\n".join(lines)


def exit_code(report: dict[str, Any]) -> int:
    return {OK: 0, WARN: 1, FAIL: 2}[report["overall"]]
