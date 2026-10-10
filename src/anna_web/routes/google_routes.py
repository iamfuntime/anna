"""``/google``: connect, reconnect, verify, remove and revoke Google accounts (localhost only).

* Browser consent uses Google's installed-app loopback flow (state + PKCE by the library); this process
  never sees a hosted callback.  Secrets (client secret, tokens, auth codes) never reach HTML or logs.
* The dashboard does not own the connector: after a connect/remove it QUEUES a control request that the
  daemon applies on the account's serialized lane (cache invalidation, live verification).
* Adding an account or changing its services edits ``anna.yaml``; the daemon reads config at start, so those
  changes say plainly that a restart is required.  Nothing here restarts anything.
* Local removal and provider revocation are separate, explicitly confirmed actions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from anna.config import GoogleAccountConfig
from anna.runtime.controls import ControlError, latest_control, queue_control
from anna.runtime.status import read_snapshot
from anna.tools.google_auth import (
    GoogleAuthError,
    delete_local_token,
    find_account,
    import_client_file,
    revoke_token,
    token_status,
)
from anna_web import audit as web_audit
from anna_web.google_oauth import OAuthSessionManager, SessionError
from anna_web.routes.ops_routes import require_local_host

router = APIRouter(tags=["google"], dependencies=[Depends(require_local_host)])

_SLUG = re.compile(r"^[a-z0-9_]{1,32}$")
_EMAIL = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,190}\.[^@\s<>]{2,}$")
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_FLASH = {
    "client_imported": "Shared OAuth client imported.",
    "account_added": "Account added to anna.yaml. Restart the daemon for it to take effect (nothing was restarted).",
    "services_changed": "Services changed in anna.yaml. Reconnect the account to grant exactly those permissions, "
                        "then restart the daemon.",
    "connected": "Connected. The daemon was asked to drop its cached Google session.",
    "cancelled": "Connection cancelled.",
    "verify_queued": "Verification queued; the daemon runs it on its next cycle.",
    "removed_local": "Local token removed. The grant at Google is still active until you revoke it.",
    "removed_all": "Local token and the anna.yaml entry removed (restart the daemon). The grant at Google is still "
                   "active until revoked.",
    "revoked": "Revoked at Google and the local token removed.",
    "bad_confirm": "Not done: the confirmation phrase did not match.",
    "bad_input": "Not done: that input was not valid.",
    "busy": "Not done: another Google connection is in progress, or the request is already queued.",
    "unsupported_browser": "Not available from this browser: the consent callback listens on this machine only.",
    "client_missing": "Import the shared OAuth client first.",
    "revoke_failed": "Google did not confirm the revocation; the local token was kept.",
}


def _render(request: Request, name: str, ctx: dict[str, Any], status_code: int = 200) -> Response:
    response: Response = request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status_code)
    return response


def _cfg(request: Request) -> Any:
    return request.app.state.cfg


def _manager(request: Request) -> OAuthSessionManager:
    mgr = getattr(request.app.state, "google_oauth", None)
    if mgr is None:
        cfg = _cfg(request)

        def after_connect(account: GoogleAccountConfig) -> None:
            try:
                queue_control(cfg.state_dir, kind="google_invalidate", subject=account.slug)
            except ControlError:
                pass  # the daemon picks the new token up at its next start / next cache rebuild

        mgr = OAuthSessionManager(cfg, on_connected=after_connect)
        request.app.state.google_oauth = mgr
    return mgr


def _go(flash: str) -> RedirectResponse:
    return RedirectResponse(f"/google?flash={flash}", status_code=303)


def _client_is_loopback(request: Request) -> bool:
    return bool(request.client and request.client.host in _LOOPBACK)


def _account(request: Request, slug: str) -> GoogleAccountConfig:
    try:
        return find_account(_cfg(request), slug)
    except GoogleAuthError:
        raise HTTPException(status_code=404, detail="unknown account") from None


def _client_summary(cfg: Any) -> dict[str, Any]:
    path = cfg.google_oauth_client_path()
    if not path.is_file():
        return {"present": False}
    try:
        inst = json.loads(path.read_text(encoding="utf-8")).get("installed", {})
        return {"present": True, "client_id_suffix": str(inst.get("client_id", ""))[-8:],
                "private": (path.stat().st_mode & 0o077) == 0}
    except (OSError, ValueError):
        return {"present": True, "unreadable": True}


def _page_data(request: Request) -> dict[str, Any]:
    cfg = _cfg(request)
    rows = []
    for acct in cfg.google.accounts:
        verify = latest_control(cfg.state_dir, "google_verify", acct.slug)
        parsed = None
        if verify and verify.get("result"):
            try:
                parsed = json.loads(verify["result"])
            except ValueError:
                parsed = None
        rows.append({"acct": acct, "token": token_status(cfg, acct), "verify": verify, "verify_parsed": parsed,
                     "client_ok": cfg.resolve_google_credentials_path(acct).is_file()})
    snap = read_snapshot(cfg.state_dir / "runtime-status.json") or {}
    sess = _manager(request).active()
    return {"rows": rows, "client": _client_summary(cfg), "enabled": cfg.google.enabled,
            "daemon_seen": bool(snap), "active": sess, "callback_port": cfg.google.oauth_callback_port,
            "web_port": cfg.web.port}


@router.get("/google", response_class=HTMLResponse)
async def get_google(request: Request, flash: str = "") -> Response:
    data = await run_in_threadpool(_page_data, request)
    return _render(request, "google.html", {"active_nav": "google", **data, "msg": _FLASH.get(flash, ""),
                                            "remote_hint": not _client_is_loopback(request)})


# ---------------------------------------------------------------- shared client

@router.post("/google/client")
async def post_client(request: Request, path: Annotated[str, Form()], replace: Annotated[str, Form()] = "") -> Response:
    cfg = _cfg(request)
    try:
        await run_in_threadpool(lambda: import_client_file(cfg, Path(path), replace=replace == "yes"))
    except GoogleAuthError as exc:
        web_audit.emit("google_client_rejected", request=request)
        data = await run_in_threadpool(_page_data, request)
        return _render(request, "google.html", {"active_nav": "google", **data, "msg": "", "error": str(exc)[:300],
                                                "remote_hint": not _client_is_loopback(request)}, 400)
    web_audit.emit("google_client_imported", request=request)  # the path and contents are never logged
    return _go("client_imported")


# ---------------------------------------------------------------- accounts (config edits => restart required)

def _google_section(request: Request) -> dict[str, Any]:
    from anna_web.config_store import _to_plain

    raw = request.app.state.config_store.load_raw()
    return dict(_to_plain(raw).get("google") or {})


@router.post("/google/accounts")
async def post_add_account(request: Request, slug: Annotated[str, Form()], email: Annotated[str, Form()],
                           services: Annotated[list[str], Form()] = []) -> Response:  # noqa: B006
    chosen = sorted({s for s in services if s in ("gmail", "calendar")})
    if not _SLUG.match(slug) or not _EMAIL.match(email) or not chosen:
        return _go("bad_input")
    section = await run_in_threadpool(_google_section, request)
    accounts = list(section.get("accounts") or [])
    if any(a.get("slug") == slug for a in accounts):
        return _go("bad_input")
    accounts.append({"slug": slug, "email": email, "auth_type": "oauth", "services": chosen})
    section["accounts"] = accounts
    await request.app.state.config_store.write_section("google", section)
    web_audit.emit("google_account_added", request=request, slug=slug, services=chosen)
    return _go("account_added")


@router.post("/google/accounts/{slug}/services")
async def post_services(request: Request, slug: str, services: Annotated[list[str], Form()] = []) -> Response:  # noqa: B006
    _account(request, slug)
    chosen = sorted({s for s in services if s in ("gmail", "calendar")})
    if not chosen:
        return _go("bad_input")
    section = await run_in_threadpool(_google_section, request)
    for acct in section.get("accounts") or []:
        if acct.get("slug") == slug:
            acct["services"] = chosen
    await request.app.state.config_store.write_section("google", section)
    web_audit.emit("google_services_changed", request=request, slug=slug, services=chosen)
    return _go("services_changed")


# ---------------------------------------------------------------- connect / reconnect

@router.post("/google/accounts/{slug}/connect")
async def post_connect(request: Request, slug: str) -> Response:
    account = _account(request, slug)
    if not _client_is_loopback(request):
        return _go("unsupported_browser")
    try:
        sess = await run_in_threadpool(_manager(request).start, account)
    except SessionError as exc:
        web_audit.emit("google_connect_refused", request=request, slug=slug, reason=str(exc)[:80])
        return _go("client_missing" if "import the shared" in str(exc) else "busy")
    web_audit.emit("google_connect_started", request=request, slug=slug)
    return RedirectResponse(f"/google/connect/{sess.id}", status_code=303)


@router.get("/google/connect/{sid}", response_class=HTMLResponse)
async def get_connect(request: Request, sid: str) -> Response:
    sess = _manager(request).get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail="unknown or expired session")
    cfg = _cfg(request)
    return _render(request, "google_connect.html", {
        "active_nav": "google", "sess": sess, "callback_port": cfg.google.oauth_callback_port,
        "web_port": cfg.web.port, "auth_url": sess.auth_url if sess.status == "waiting" else ""})


@router.post("/google/connect/{sid}/cancel")
async def post_cancel(request: Request, sid: str) -> Response:
    ok = await run_in_threadpool(_manager(request).cancel, sid)
    web_audit.emit("google_connect_cancelled", request=request, ok=ok)
    return _go("cancelled")


# ---------------------------------------------------------------- verify / remove / revoke

def _queue(request: Request, kind: str, slug: str) -> bool:
    try:
        queue_control(_cfg(request).state_dir, kind=kind, subject=slug)
        return True
    except ControlError:
        return False


@router.post("/google/accounts/{slug}/verify")
async def post_verify(request: Request, slug: str) -> Response:
    _account(request, slug)
    ok = await run_in_threadpool(_queue, request, "google_verify", slug)
    web_audit.emit("google_verify_queued", request=request, slug=slug, ok=ok)
    return _go("verify_queued" if ok else "busy")


@router.post("/google/accounts/{slug}/remove")
async def post_remove(request: Request, slug: str, mode: Annotated[str, Form()],
                      confirm: Annotated[str, Form()] = "") -> Response:
    account = _account(request, slug)
    if mode not in ("token", "token_and_config") or confirm.strip() != f"REMOVE {slug}":
        return _go("bad_confirm")
    cfg = _cfg(request)
    await run_in_threadpool(delete_local_token, cfg, account)
    await run_in_threadpool(_queue, request, "google_invalidate", slug)
    if mode == "token_and_config":
        section = await run_in_threadpool(_google_section, request)
        section["accounts"] = [a for a in (section.get("accounts") or []) if a.get("slug") != slug]
        await request.app.state.config_store.write_section("google", section)
    web_audit.emit("google_account_removed", request=request, slug=slug, mode=mode, revoked=False)
    return _go("removed_all" if mode == "token_and_config" else "removed_local")


@router.post("/google/accounts/{slug}/revoke")
async def post_revoke(request: Request, slug: str, confirm: Annotated[str, Form()] = "") -> Response:
    account = _account(request, slug)
    if confirm.strip() != f"REVOKE {slug}":
        return _go("bad_confirm")
    cfg = _cfg(request)
    try:
        await run_in_threadpool(revoke_token, cfg, account)
    except GoogleAuthError:
        web_audit.emit("google_revoke_failed", request=request, slug=slug)
        return _go("revoke_failed")
    await run_in_threadpool(delete_local_token, cfg, account)
    await run_in_threadpool(_queue, request, "google_invalidate", slug)
    web_audit.emit("google_account_removed", request=request, slug=slug, mode="token", revoked=True)
    return _go("revoked")
