"""Operations pages: ``/health`` (doctor), ``/repairs`` and ``/memory``.

* ``/health`` renders the same offline, read-only report as ``anna doctor``.
* ``/repairs`` lists unknown runs and uncertain/failed deliveries.  Resolving one
  QUEUES an intent that the daemon applies; this process never edits scheduler
  state.  Each action needs the exact typed confirmation phrase.
* ``/memory`` requires an explicit owner + scope selection, then shows facts and
  offers correct/forget through ``MemoryStore`` (same transactional invariants as
  the agent tools: active facts only, scoped, atomic).

All mutations are POSTs covered by the existing same-origin middleware; nothing
here widens access.  Audit events carry ids and action names, never message
bodies, fact text or credentials.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from anna.doctor import collect
from anna.runtime.memory import FactNotActive, MemoryStore
from anna.runtime.repairs import (
    LABELS,
    RepairError,
    expected_confirmation,
    list_attention,
    queue_repair,
)
from anna.runtime.state_read import ReadOnlyDB, Unavailable
from anna_web import audit as web_audit
from anna_web.middleware import _origin_tuple, is_wildcard_host


def require_local_host(request: Request) -> None:
    """Reject requests whose Host header is not the dashboard's own address (DNS-rebinding guard).

    These pages expose memory facts and delivery metadata to any page that can make the browser talk to
    the dashboard.  A rebinding attack arrives with an attacker-controlled Host name, so only the bound
    address (and the loopback names for a loopback bind) are accepted.  A wildcard bind has no single
    canonical name; it is unchanged (use a loopback bind or a reverse proxy that pins Host).
    """
    cfg = request.app.state.cfg
    bound = str(cfg.web.host).strip("[]").lower()
    if is_wildcard_host(bound):
        return
    allowed = {bound}
    if bound in ("127.0.0.1", "::1", "localhost"):
        allowed |= {"127.0.0.1", "::1", "localhost"}
    host_header = (request.headers.get("host") or "").lower()
    parsed = _origin_tuple(f"http://{host_header}") if host_header else None
    if parsed is None or parsed[1] not in allowed or parsed[2] not in (None, cfg.web.port):
        raise HTTPException(status_code=403, detail="invalid Host header")


router = APIRouter(tags=["ops"], dependencies=[Depends(require_local_host)])

# Banner text comes ONLY from these fixed tables (never from the URL), so a crafted link cannot
# put attacker-written words into a trusted-looking confirmation banner.
_FLASH = {
    "queued": "Queued for the daemon; it applies the decision on its next cycle.",
    "forgot": "Memory forgotten.",
    "corrected": "Memory corrected.",
    "no_active": "No such active memory in this owner/scope.",
    "no_memory": "No such memory in this owner/scope.",
    "not_active": "That memory is no longer active (forgotten or already corrected).",
    "select_scope": "Select an owner and scope first.",
    "missing_fields": "Owner, scope and new content are required.",
    "bad_scope": "Unknown owner/scope selection.",
}


def _render(request: Request, name: str, ctx: dict[str, Any], status_code: int = 200) -> Response:
    response: Response = request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status_code)
    return response


# ---------------------------------------------------------------- health

@router.get("/health", response_class=HTMLResponse)
async def get_health(request: Request) -> Response:
    report = await run_in_threadpool(collect, request.app.state.cfg)
    return _render(request, "health.html", {"active_nav": "health", "report": report})


@router.get("/health.json")
async def get_health_json(request: Request) -> JSONResponse:
    return JSONResponse(await run_in_threadpool(collect, request.app.state.cfg))


# ---------------------------------------------------------------- repairs

@router.get("/repairs", response_class=HTMLResponse)
async def get_repairs(request: Request, flash: str = "", n: int = 0) -> Response:
    data = await run_in_threadpool(list_attention, request.app.state.cfg.state_dir)
    msg = f"{_FLASH['queued']} (request {int(n)})" if flash == "queued" and n > 0 else ""
    return _render(request, "repairs.html", {"active_nav": "repairs", "data": data, "labels": LABELS,
                                              "msg": msg, "err": "", "phrase": expected_confirmation})


@router.post("/repairs/{kind}/{target_id}")
async def post_repair(request: Request, kind: str, target_id: int,
                      action: Annotated[str, Form()], expected_status: Annotated[str, Form()],
                      confirm: Annotated[str, Form()] = "") -> Response:
    cfg = request.app.state.cfg
    if kind not in ("run", "outbox") or action not in LABELS:
        raise HTTPException(status_code=404, detail="unknown repair target")  # nothing attacker-chosen is echoed
    if confirm.strip() != expected_confirmation(kind, target_id, action):
        web_audit.emit("repair_rejected", request=request, kind=kind, target_id=target_id, action=action,
                       reason="confirmation phrase mismatch")
        return _render(request, "repairs.html", {
            "active_nav": "repairs", "data": await run_in_threadpool(list_attention, cfg.state_dir), "labels": LABELS,
            "msg": "", "err": f"Not queued: type exactly '{expected_confirmation(kind, target_id, action)}' to confirm.",
            "phrase": expected_confirmation}, status_code=400)
    try:
        request_id = await run_in_threadpool(
            lambda: queue_repair(cfg.state_dir, kind=kind, target_id=target_id, action=action,
                                 expected_status=expected_status, requested_by="web:operator"))
    except RepairError as exc:
        web_audit.emit("repair_rejected", request=request, kind=kind, target_id=target_id, action=action,
                       reason=str(exc)[:120])
        return _render(request, "repairs.html", {
            "active_nav": "repairs", "data": await run_in_threadpool(list_attention, cfg.state_dir), "labels": LABELS,
            "msg": "", "err": f"Not queued: {exc}", "phrase": expected_confirmation}, status_code=409)
    web_audit.emit("repair_queued", request=request, kind=kind, target_id=target_id, action=action,
                   repair_id=request_id)
    return RedirectResponse(f"/repairs?flash=queued&n={int(request_id)}", status_code=303)


# ---------------------------------------------------------------- memory

def _scopes(cfg: Any) -> list[dict[str, Any]]:
    try:
        db = ReadOnlyDB(cfg.state_dir / "memory.sqlite3")
    except Unavailable:
        return []
    try:
        return [dict(r) for r in db.rows("SELECT owner_id, visibility_scope scope, COUNT(*) n, "
                                         "SUM(status='active') active FROM facts GROUP BY owner_id, visibility_scope "
                                         "ORDER BY owner_id, scope")]
    except Unavailable:
        return []
    finally:
        db.close()


def _facts(cfg: Any, owner: str, scope: str, status: str) -> list[dict[str, Any]]:
    try:
        db = ReadOnlyDB(cfg.state_dir / "memory.sqlite3")
    except Unavailable:
        return []
    try:
        sql = ("SELECT id, kind, subject, content, status, origin, source_event_id, confidence, created_at, "
               "evidence IS NOT NULL has_evidence FROM facts WHERE owner_id=? AND visibility_scope=?")
        args: list[Any] = [owner, scope]
        if status != "all":
            sql += " AND status=?"
            args.append(status)
        return [dict(r) for r in db.rows(sql + " ORDER BY id DESC LIMIT 200", tuple(args))]
    except Unavailable:
        return []
    finally:
        db.close()


@router.get("/memory", response_class=HTMLResponse)
async def get_memory(request: Request, owner: str = "", scope: str = "", status: str = "active",
                     flash: str = "") -> Response:
    cfg = request.app.state.cfg
    scopes = await run_in_threadpool(_scopes, cfg)
    selected = None
    facts: list[dict[str, Any]] = []
    err = ""
    msg = _FLASH.get(flash, "") if flash in ("forgot", "corrected") else ""
    if flash in ("no_active", "no_memory", "not_active", "select_scope", "missing_fields"):
        err = _FLASH[flash]
    if owner and scope:
        if not any(s["owner_id"] == owner and s["scope"] == scope for s in scopes):
            err = _FLASH["bad_scope"]
        else:
            selected = {"owner": owner, "scope": scope}
            facts = await run_in_threadpool(_facts, cfg, owner, scope, status if status in
                                            ("active", "forgotten", "superseded", "all") else "active")
    return _render(request, "memory.html", {"active_nav": "memory", "scopes": scopes, "selected": selected,
                                             "facts": facts, "status": status, "msg": msg, "err": err})


def _back(owner: str, scope: str, *, flash: str) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse(f"/memory?{urlencode({'owner': owner, 'scope': scope, 'flash': flash})}", status_code=303)


def _store(cfg: Any) -> MemoryStore:
    return MemoryStore(cfg.state_dir / "memory.sqlite3")


@router.post("/memory/{fact_id}/forget")
async def post_memory_forget(request: Request, fact_id: int, owner: Annotated[str, Form()],
                             scope: Annotated[str, Form()]) -> Response:
    cfg = request.app.state.cfg
    if not owner or not scope:
        return _back(owner, scope, flash="select_scope")

    if not cfg.memory.enabled:  # kill switch: never create/open the DB from a write route
        return _back(owner, scope, flash="no_memory")

    def _do() -> str:
        store = _store(cfg)
        try:
            store.forget(fact_id, owner_id=owner, visibility_scope=scope)
            return "forgot"
        except KeyError:
            return "no_active"
        finally:
            store.close()

    outcome = await run_in_threadpool(_do)
    web_audit.emit("memory_forget", request=request, fact_id=fact_id, scope=scope, ok=outcome == "forgot")
    return _back(owner, scope, flash=outcome)


@router.post("/memory/{fact_id}/correct")
async def post_memory_correct(request: Request, fact_id: int, owner: Annotated[str, Form()],
                              scope: Annotated[str, Form()], content: Annotated[str, Form()]) -> Response:
    cfg = request.app.state.cfg
    if not owner or not scope or not content.strip():
        return _back(owner, scope, flash="missing_fields")

    if not cfg.memory.enabled:  # kill switch: never create/open the DB from a write route
        return _back(owner, scope, flash="no_memory")

    def _do() -> str:
        store = _store(cfg)
        try:
            store.correct(fact_id, content=content.strip()[:500], source_event_id=f"web:{request.state.request_id}"
                          if hasattr(request.state, "request_id") else "web",
                          owner_id=owner, visibility_scope=scope)
            return "corrected"
        except KeyError:
            return "no_memory"
        except FactNotActive:
            return "not_active"
        finally:
            store.close()

    outcome = await run_in_threadpool(_do)
    web_audit.emit("memory_correct", request=request, fact_id=fact_id, scope=scope, ok=outcome == "corrected")
    return _back(owner, scope, flash=outcome)


