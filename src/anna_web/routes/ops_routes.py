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

from fastapi import APIRouter, Form, Request
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

router = APIRouter(tags=["ops"])


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
async def get_repairs(request: Request, msg: str = "", err: str = "") -> Response:
    data = await run_in_threadpool(list_attention, request.app.state.cfg.state_dir)
    return _render(request, "repairs.html", {"active_nav": "repairs", "data": data, "labels": LABELS,
                                              "msg": msg[:200], "err": err[:200],
                                              "phrase": expected_confirmation})


@router.post("/repairs/{kind}/{target_id}")
async def post_repair(request: Request, kind: str, target_id: int,
                      action: Annotated[str, Form()], expected_status: Annotated[str, Form()],
                      confirm: Annotated[str, Form()] = "") -> Response:
    cfg = request.app.state.cfg
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
    return RedirectResponse(f"/repairs?msg=Queued+for+the+daemon+(request+{request_id})", status_code=303)


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
                     msg: str = "", err: str = "") -> Response:
    cfg = request.app.state.cfg
    scopes = await run_in_threadpool(_scopes, cfg)
    selected = None
    facts: list[dict[str, Any]] = []
    if owner and scope:
        if not any(s["owner_id"] == owner and s["scope"] == scope for s in scopes):
            err = err or "Unknown owner/scope selection."
        else:
            selected = {"owner": owner, "scope": scope}
            facts = await run_in_threadpool(_facts, cfg, owner, scope, status if status in
                                            ("active", "forgotten", "superseded", "all") else "active")
    return _render(request, "memory.html", {"active_nav": "memory", "scopes": scopes, "selected": selected,
                                             "facts": facts, "status": status, "msg": msg[:200], "err": err[:200]})


def _back(owner: str, scope: str, *, msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    q = urlencode({"owner": owner, "scope": scope, **({"msg": msg} if msg else {}), **({"err": err} if err else {})})
    return RedirectResponse(f"/memory?{q}", status_code=303)


def _store(cfg: Any) -> MemoryStore:
    return MemoryStore(cfg.state_dir / "memory.sqlite3")


@router.post("/memory/{fact_id}/forget")
async def post_memory_forget(request: Request, fact_id: int, owner: Annotated[str, Form()],
                             scope: Annotated[str, Form()]) -> Response:
    cfg = request.app.state.cfg
    if not owner or not scope:
        return _back(owner, scope, err="Select an owner and scope first.")

    def _do() -> str:
        store = _store(cfg)
        try:
            store.forget(fact_id, owner_id=owner, visibility_scope=scope)
            return ""
        except KeyError:
            return "No such active memory in this owner/scope."
        finally:
            store.close()

    error = await run_in_threadpool(_do)
    web_audit.emit("memory_forget", request=request, fact_id=fact_id, scope=scope, ok=not error)
    return _back(owner, scope, msg="" if error else f"Forgot memory #{fact_id}.", err=error)


@router.post("/memory/{fact_id}/correct")
async def post_memory_correct(request: Request, fact_id: int, owner: Annotated[str, Form()],
                              scope: Annotated[str, Form()], content: Annotated[str, Form()]) -> Response:
    cfg = request.app.state.cfg
    if not owner or not scope or not content.strip():
        return _back(owner, scope, err="Owner, scope and new content are required.")

    def _do() -> str:
        store = _store(cfg)
        try:
            store.correct(fact_id, content=content.strip()[:500], source_event_id=f"web:{request.state.request_id}"
                          if hasattr(request.state, "request_id") else "web",
                          owner_id=owner, visibility_scope=scope)
            return ""
        except KeyError:
            return "No such memory in this owner/scope."
        except FactNotActive:
            return "That memory is no longer active (forgotten or already corrected)."
        finally:
            store.close()

    error = await run_in_threadpool(_do)
    web_audit.emit("memory_correct", request=request, fact_id=fact_id, scope=scope, ok=not error)
    return _back(owner, scope, msg="" if error else f"Corrected memory #{fact_id}.", err=error)


