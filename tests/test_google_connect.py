# ruff: noqa: E702, ASYNC251, S310
"""Dashboard Google connect/reconnect/remove/verify: real InstalledAppFlow on a loopback socket, everything else mocked.

No Google endpoint, no credential and no consent is involved: the token exchange and the identity lookup are patched,
and the "browser" is a urllib request to the 127.0.0.1 callback this process opened.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.runtime.controls import latest_control
from anna.runtime.ledger import RuntimeLedger
from anna.tools.google_auth import (
    GoogleAuthError,
    GoogleIdentityMismatch,
    import_client_file,
    persist_connected_token,
    required_scopes,
    token_status,
    validate_client_file,
)
from anna.tools.google_clients import GoogleClients
from anna_web.app import create_app
from anna_web.google_oauth import OAuthSessionManager

ORIGIN = {"Origin": "http://127.0.0.1:8765"}
EVIL = {"Origin": "http://evil.example"}
BOTH = "https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/gmail.readonly"
SECRET_REFRESH = "RT-SECRET-refresh-9f3a"
SECRET_ACCESS = "AT-SECRET-access-77c1"
CLIENT_SECRET = "CLIENT-SECRET-zzTop"
AUTH_CODE = "4/0AUTH-CODE-abc"
EXAMPLE = Path(__file__).resolve().parent.parent / "anna.yaml.example"


def client_json(secret: str = CLIENT_SECRET, kind: str = "installed") -> dict:
    return {kind: {"client_id": "123456-abcdef.apps.googleusercontent.com", "client_secret": secret,
                   "project_id": "p", "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"; (home / "state" / "google").mkdir(parents=True)
    shutil.copy(EXAMPLE, home / "anna.yaml")
    monkeypatch.setenv("HOME", str(tmp_path))                       # Path.home() == tmp_path: sandbox for import paths
    cfg = AnnaConfig(); object.__setattr__(cfg, "anna_home", home)
    cfg.google.enabled = True
    cfg.logging.audit.fsync_on_write = False
    cfg.google.accounts = [
        GoogleAccountConfig(slug="personal", email="me@gmail.test", auth_type="oauth", services=["gmail", "calendar"]),
        GoogleAccountConfig(slug="work", email="me@corp.test", auth_type="oauth", services=["calendar"]),
    ]
    shared = cfg.google_oauth_client_path()
    shared.write_text(json.dumps(client_json())); shared.chmod(0o600)
    state = SimpleNamespace(identity="me@gmail.test", token_scope=BOTH, exchanges=0)

    class FakeResponse:
        status_code = 200
        headers: dict = {}
        request = SimpleNamespace(url="x", headers={}, body="")
        def __init__(self, scope):
            self.text = json.dumps({"access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH, "expires_in": 3600,
                                    "token_type": "Bearer", "scope": scope})
        def json(self): return json.loads(self.text)

    def fake_request(self, *a, **k):
        state.exchanges += 1
        assert k["url"] == "https://oauth2.googleapis.com/token"   # the only thing ever "contacted"
        assert k["data"]["code_verifier"]                          # PKCE verifier is sent with the code
        scope = state.token_scope if state.token_scope != "ECHO" else " ".join(self.scope)
        return FakeResponse(scope)

    monkeypatch.setattr("requests_oauthlib.OAuth2Session.request", fake_request)
    monkeypatch.setattr("anna.tools.google_auth.verify_identity", lambda creds, account: state.identity)
    app = create_app(cfg)
    c = TestClient(app, follow_redirects=False, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))
    return SimpleNamespace(cfg=cfg, home=home, app=app, c=c, state=state, tmp=tmp_path)


def begin(env, slug="personal"):
    r = env.c.post(f"/google/accounts/{slug}/connect", headers=ORIGIN)
    assert r.status_code == 303, r.text
    sid = r.headers["location"].rsplit("/", 1)[1]
    page = env.c.get(f"/google/connect/{sid}")
    link = next(part for part in page.text.split('"') if part.startswith("https://accounts.google.com"))
    link = link.replace("&amp;", "&")
    return sid, page, link


def browser_callback(link, *, code=AUTH_CODE, state=None, path_only=False):
    q = parse_qs(urlparse(link).query)
    cb = q["redirect_uri"][0]
    url = f"{cb}?code={code}&state={state if state is not None else q['state'][0]}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.URLError as exc:
        return None, str(exc)


def wait_terminal(env, sid, timeout=8):
    mgr = env.app.state.google_oauth
    end = time.time() + timeout
    while time.time() < end:
        s = mgr.get(sid)
        if s is not None and s.status not in ("starting", "waiting", "verifying"):
            s.thread.join(5)
            return s
        time.sleep(0.02)
    raise AssertionError("session never finished")


def token_file(env, slug="personal"):
    return env.cfg.google_state_dir / f"token_{slug}.json"


# ---- happy path, state/PKCE, secrecy ------------------------------------------------------------

def test_connect_uses_loopback_installed_flow_with_state_and_pkce_and_stores_a_private_verified_token(env, capsys):
    ledger = RuntimeLedger(env.cfg.state_dir / "runtime.sqlite3"); ledger.close()
    sid, page, link = begin(env)
    q = parse_qs(urlparse(link).query)
    assert q["code_challenge_method"] == ["S256"] and len(q["code_challenge"][0]) >= 43 and len(q["state"][0]) >= 20
    assert q["redirect_uri"][0].startswith("http://127.0.0.1:") and q["access_type"] == ["offline"]
    assert set(q["scope"][0].split()) == set(required_scopes(env.cfg.google.accounts[0]))     # exactly the chosen services
    assert CLIENT_SECRET not in page.text and "client_secret" not in link and SECRET_REFRESH not in page.text
    status, body = browser_callback(link)
    assert status == 200 and "close this tab" in body
    sess = wait_terminal(env, sid)
    assert sess.status == "done" and sess.auth_url == "" and not sess.thread.is_alive()
    tf = token_file(env)
    assert tf.is_file() and (tf.stat().st_mode & 0o777) == 0o600 and (tf.parent.stat().st_mode & 0o777) == 0o700
    assert [p.name for p in tf.parent.iterdir() if p.name.startswith(".token")] == []                     # no temp leftovers
    data = json.loads(tf.read_text())
    assert data["refresh_token"] == SECRET_REFRESH and set(data["scopes"]) == set(BOTH.split())
    done = env.c.get(f"/google/connect/{sid}").text
    assert "Connected" in done and SECRET_REFRESH not in done and AUTH_CODE not in done and "accounts.google.com" not in done
    ctl = latest_control(env.cfg.state_dir, "google_invalidate", "personal")
    assert ctl and ctl["status"] == "pending"                                                             # daemon asked to drop its cache
    out = capsys.readouterr()
    for secret in (SECRET_REFRESH, SECRET_ACCESS, CLIENT_SECRET, AUTH_CODE, q["state"][0]):
        assert secret not in out.out + out.err
    audit = "".join(p.read_text() for p in (env.home / "audit").glob("*.jsonl"))
    assert "google_connect_started" in audit
    for secret in (SECRET_REFRESH, SECRET_ACCESS, CLIENT_SECRET, AUTH_CODE, "client_secret"):
        assert secret not in audit


def test_state_mismatch_is_rejected_and_nothing_is_saved(env):
    sid, _, link = begin(env)
    status, _ = browser_callback(link, state="attacker-chosen-state")
    sess = wait_terminal(env, sid)
    assert sess.status == "failed" and not token_file(env).exists() and env.state.exchanges == 0       # rejected BEFORE any exchange
    assert sess.error == "MismatchingStateError"


def test_replayed_callback_and_reused_session_do_nothing(env):
    sid, _, link = begin(env)
    browser_callback(link); sess = wait_terminal(env, sid)
    assert sess.status == "done"
    before = token_file(env).read_text()
    status, detail = browser_callback(link)                                                              # listener is gone
    assert status is None and "refused" in detail.lower()
    assert env.c.post(f"/google/connect/{sid}/cancel", headers=ORIGIN).status_code == 303
    assert env.app.state.google_oauth.cancel(sid) is False and token_file(env).read_text() == before
    assert "Continue to Google" not in env.c.get(f"/google/connect/{sid}").text
    assert env.state.exchanges == 1


def test_session_expires_and_cleans_up(env):
    env.app.state.google_oauth = OAuthSessionManager(env.cfg, ttl=1)
    sid, _, link = begin(env)
    sess = wait_terminal(env, sid, timeout=10)
    assert sess.status == "expired" and sess.auth_url == "" and not token_file(env).exists()
    assert browser_callback(link)[0] is None                                                              # port closed after timeout
    assert "expired" in env.c.get(f"/google/connect/{sid}").text


def test_cancel_stops_the_listener_and_saves_nothing(env):
    sid, _, link = begin(env)
    port = urlparse(parse_qs(urlparse(link).query)["redirect_uri"][0]).port
    r = env.c.post(f"/google/connect/{sid}/cancel", headers=ORIGIN)
    assert r.status_code == 303
    sess = wait_terminal(env, sid)
    assert sess.status == "cancelled" and not token_file(env).exists() and env.app.state.google_oauth.active() is None
    import socket
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
        pass


def test_only_one_connection_at_a_time(env):
    sid, _, link = begin(env)
    second = env.c.post("/google/accounts/work/connect", headers=ORIGIN)
    assert second.status_code == 303 and "flash=busy" in second.headers["location"]
    env.app.state.google_oauth.cancel(sid); wait_terminal(env, sid)


# ---- identity, scopes ---------------------------------------------------------------------------

def test_wrong_mailbox_is_a_hard_rejection_and_never_writes_a_token(env):
    env.state.identity = "someone-else@gmail.test"
    existing = token_file(env); existing.write_text('{"refresh_token": "OLD"}'); existing.chmod(0o600)
    sid, _, link = begin(env)
    browser_callback(link); sess = wait_terminal(env, sid)
    assert sess.status == "failed" and sess.error == "identity_mismatch"
    assert existing.read_text() == '{"refresh_token": "OLD"}' and not token_file(env, "work").exists()  # not under ANY slug
    page = env.c.get(f"/google/connect/{sid}").text
    assert "Wrong account" in page and "someone-else" not in page and SECRET_REFRESH not in page


def test_persist_requires_every_requested_scope_and_a_refresh_token(env):
    acct = env.cfg.google.accounts[0]
    partial = SimpleNamespace(scopes=["https://www.googleapis.com/auth/gmail.readonly"], refresh_token="r", to_json=lambda: "{}")
    with pytest.raises(GoogleAuthError, match="permission"):
        persist_connected_token(env.cfg, acct, partial)
    nor = SimpleNamespace(scopes=BOTH.split(), refresh_token=None, to_json=lambda: "{}")
    with pytest.raises(GoogleAuthError, match="refresh token"):
        persist_connected_token(env.cfg, acct, nor)
    env.state.identity = "x@y.test"
    ok = SimpleNamespace(scopes=BOTH.split(), refresh_token="r", to_json=lambda: "{}")
    with pytest.raises(GoogleIdentityMismatch):
        persist_connected_token(env.cfg, acct, ok)
    assert not token_file(env).exists()


def test_scopes_follow_the_selected_services_and_tools_enforce_them(env):
    work = env.cfg.google.accounts[1]
    assert required_scopes(work) == ["https://www.googleapis.com/auth/calendar.readonly"]
    sid, _, link = begin(env, "work")
    assert parse_qs(urlparse(link).query)["scope"] == ["https://www.googleapis.com/auth/calendar.readonly"]
    env.state.identity = "me@corp.test"; env.state.token_scope = "https://www.googleapis.com/auth/calendar.readonly"
    browser_callback(link); assert wait_terminal(env, sid).status == "done"
    st = token_status(env.cfg, work)
    assert st["present"] and st["missing_scopes"] == [] and "gmail" not in " ".join(st["granted_scopes"])
    clients = GoogleClients(env.cfg)

    async def attempt():
        with pytest.raises(GoogleAuthError, match="not authorized for gmail"):
            await clients.run("work", "gmail", lambda s: None)

    asyncio.run(attempt()); clients.close()
    from anna.runtime.heartbeat import HeartbeatJob
    env.cfg.heartbeat.enabled = True
    job = HeartbeatJob(config=env.cfg, clients=None, ledger=SimpleNamespace())
    assert ("work", "gmail") not in job.targets() and ("work", "calendar") in job.targets() and ("personal", "gmail") in job.targets()


def test_granting_fewer_permissions_than_requested_saves_nothing(env):
    env.state.token_scope = "https://www.googleapis.com/auth/gmail.readonly"       # user unticked calendar at consent
    sid, _, link = begin(env)
    browser_callback(link); sess = wait_terminal(env, sid)
    assert sess.status == "failed" and not token_file(env).exists()


# ---- shared client, many accounts -----------------------------------------------------------------

def test_one_shared_client_serves_every_account(env):
    assert env.cfg.resolve_google_credentials_path(env.cfg.google.accounts[0]) == env.cfg.google_oauth_client_path()
    assert env.cfg.resolve_google_credentials_path(env.cfg.google.accounts[1]) == env.cfg.google_oauth_client_path()
    sid, _, link = begin(env, "personal"); browser_callback(link); assert wait_terminal(env, sid).status == "done"
    env.state.identity = "me@corp.test"; env.state.token_scope = "ECHO"
    sid2, _, link2 = begin(env, "work"); browser_callback(link2); assert wait_terminal(env, sid2).status == "done"
    assert token_file(env).is_file() and token_file(env, "work").is_file()
    assert sorted(p.name for p in env.cfg.google_state_dir.glob("*client*")) == ["oauth_client.json"]


def test_import_client_validates_path_type_and_never_echoes_secrets(env):
    src = env.tmp / "Downloads"; src.mkdir()
    good = src / "client_secret.json"; good.write_text(json.dumps(client_json("S3CRET-IMPORT")))
    cfg = env.cfg; target = cfg.google_oauth_client_path(); target.unlink()
    r = env.c.post("/google/client", data={"path": str(good)}, headers=ORIGIN)
    assert r.status_code == 303 and target.is_file() and (target.stat().st_mode & 0o777) == 0o600
    assert json.loads(target.read_text())["installed"]["client_secret"] == "S3CRET-IMPORT"
    # second import refuses to overwrite silently
    again = env.c.post("/google/client", data={"path": str(good)}, headers=ORIGIN)
    assert again.status_code == 400 and "replace" in again.text and "S3CRET-IMPORT" not in again.text
    web = src / "web.json"; web.write_text(json.dumps(client_json("WEB-SECRET", kind="web")))
    r = env.c.post("/google/client", data={"path": str(web), "replace": "yes"}, headers=ORIGIN)
    assert r.status_code == 400 and "Desktop app" in r.text and "WEB-SECRET" not in r.text
    for bad in ("/etc/passwd", str(src / "missing.json"), "../../../../etc/passwd"):
        r = env.c.post("/google/client", data={"path": bad, "replace": "yes"}, headers=ORIGIN)
        assert r.status_code == 400
    link = src / "link.json"; link.symlink_to("/etc/passwd")
    assert env.c.post("/google/client", data={"path": str(link), "replace": "yes"}, headers=ORIGIN).status_code == 400
    big = src / "big.json"; big.write_text(json.dumps(client_json()) + " " * 70000)
    with pytest.raises(GoogleAuthError, match="too large"):
        validate_client_file(cfg, big)
    evil = src / "evil.json"; d = client_json(); d["installed"]["token_uri"] = "https://attacker.example/token"; evil.write_text(json.dumps(d))
    with pytest.raises(GoogleAuthError, match="token_uri"):
        validate_client_file(cfg, evil)
    other_home = Path("/tmp/anna-test-outside.json"); other_home.write_text(json.dumps(client_json()))
    try:
        with pytest.raises(GoogleAuthError, match="inside your home"):
            validate_client_file(cfg, other_home)
    finally:
        other_home.unlink()
    audit = "".join(p.read_text() for p in (env.home / "audit").glob("*.jsonl"))
    assert "S3CRET-IMPORT" not in audit and "client_secret.json" not in audit


# ---- browser protections --------------------------------------------------------------------------

def test_cross_origin_foreign_host_and_non_loopback_clients_cannot_start_a_flow(env):
    paths = ["/google/accounts/personal/connect", "/google/accounts/personal/verify", "/google/client",
             "/google/accounts/personal/revoke", "/google/accounts/personal/remove"]
    for p in paths:
        assert env.c.post(p, data={"path": "x"}, headers=EVIL).status_code == 403
        assert env.c.post(p, data={"path": "x"}, headers={"x-test-omit-origin": "1"}).status_code == 403
        assert env.c.post(p, data={"path": "x"}, headers={"host": "attacker.example:8765", "Origin": "http://attacker.example:8765"}).status_code == 403
    assert env.c.get("/google", headers={"host": "attacker.example:8765"}).status_code == 403
    assert getattr(env.app.state, "google_oauth", None) is None or env.app.state.google_oauth.active() is None
    remote = TestClient(env.app, follow_redirects=False, base_url="http://127.0.0.1:8765", client=("203.0.113.9", 4444))
    r = remote.post("/google/accounts/personal/connect", headers=ORIGIN)
    assert r.status_code == 303 and "flash=unsupported_browser" in r.headers["location"]
    page = remote.get("/google").text
    assert '<button type="submit" disabled>Connect</button>' in page
    assert getattr(env.app.state, "google_oauth", None) is None or env.app.state.google_oauth.active() is None
    assert "ssh -L 8765" in page                                                                          # honest tunnel instructions


def test_banner_text_comes_only_from_fixed_codes(env):
    page = env.c.get("/google?flash=Verify+your+account+at+evil.test&msg=evil.test").text
    assert "evil.test" not in page
    assert "Shared OAuth client imported." in env.c.get("/google?flash=client_imported").text


def test_callback_port_instructions_use_the_configured_fixed_port(env):
    env.cfg.google.oauth_callback_port = 49321
    page = env.c.get("/google").text
    assert "-L 49321:127.0.0.1:49321" in page


# ---- accounts, removal, revocation, daemon controls -------------------------------------------------

def test_adding_an_account_edits_anna_yaml_asks_for_restart_and_does_not_enable_anything(env):
    before = (env.home / "anna.yaml").read_text()
    r = env.c.post("/google/accounts", data={"slug": "side", "email": "side@gmail.test", "services": ["calendar"]}, headers=ORIGIN)
    assert r.status_code == 303 and "account_added" in r.headers["location"]
    after = (env.home / "anna.yaml").read_text()
    assert "side@gmail.test" in after and after != before
    import os

    from anna.config import load_config
    os.environ["ANNA_HOME"] = str(env.home)
    cfg2 = load_config()
    side = next(a for a in cfg2.google.accounts if a.slug == "side")
    assert side.services == ["calendar"] and side.credentials_file == "" and cfg2.google.enabled is False       # no silent enabling
    assert "Restart the daemon" in env.c.get("/google?flash=account_added").text
    for bad in ({"slug": "Bad Slug!", "email": "a@b.test", "services": ["gmail"]}, {"slug": "ok", "email": "nope", "services": ["gmail"]},
                {"slug": "ok2", "email": "a@b.test", "services": []}, {"slug": "side", "email": "a@b.test", "services": ["gmail"]}):
        assert "flash=bad_input" in env.c.post("/google/accounts", data=bad, headers=ORIGIN).headers["location"]


def test_local_removal_is_separate_from_provider_revocation(env, monkeypatch):
    RuntimeLedger(env.cfg.state_dir / "runtime.sqlite3").close()
    token_file(env).write_text(json.dumps({"refresh_token": SECRET_REFRESH, "scopes": BOTH.split()})); token_file(env).chmod(0o600)
    calls = []

    class Resp:
        status_code = 200

    monkeypatch.setattr("requests.post", lambda url, **k: calls.append((url, k["data"])) or Resp())
    no = env.c.post("/google/accounts/personal/remove", data={"mode": "token", "confirm": "wrong"}, headers=ORIGIN)
    assert "bad_confirm" in no.headers["location"] and token_file(env).exists()
    ok = env.c.post("/google/accounts/personal/remove", data={"mode": "token", "confirm": "REMOVE personal"}, headers=ORIGIN)
    assert "removed_local" in ok.headers["location"] and not token_file(env).exists()
    assert calls == []                                                                                    # local removal never contacts Google
    assert latest_control(env.cfg.state_dir, "google_invalidate", "personal")["status"] == "pending"
    token_file(env).write_text(json.dumps({"refresh_token": SECRET_REFRESH, "scopes": BOTH.split()}))
    assert "bad_confirm" in env.c.post("/google/accounts/personal/revoke", data={"confirm": "REMOVE personal"}, headers=ORIGIN).headers["location"]
    done = env.c.post("/google/accounts/personal/revoke", data={"confirm": "REVOKE personal"}, headers=ORIGIN)
    assert "revoked" in done.headers["location"] and not token_file(env).exists()
    assert calls == [("https://oauth2.googleapis.com/revoke", {"token": SECRET_REFRESH})]
    audit = "".join(p.read_text() for p in (env.home / "audit").glob("*.jsonl"))
    assert SECRET_REFRESH not in audit


def test_failed_revocation_keeps_the_local_token(env, monkeypatch):
    token_file(env).write_text(json.dumps({"refresh_token": SECRET_REFRESH, "scopes": BOTH.split()}))
    monkeypatch.setattr("requests.post", lambda url, **k: SimpleNamespace(status_code=400))
    r = env.c.post("/google/accounts/personal/revoke", data={"confirm": "REVOKE personal"}, headers=ORIGIN)
    assert "revoke_failed" in r.headers["location"] and token_file(env).exists()


def test_remove_token_and_config_entry_requires_restart_message(env):
    env.c.post("/google/accounts", data={"slug": "gone", "email": "g@gmail.test", "services": ["gmail"]}, headers=ORIGIN)
    import os
    os.environ["ANNA_HOME"] = str(env.home)
    from anna.config import load_config
    env.app.state.cfg.google.accounts.append(GoogleAccountConfig(slug="gone", email="g@gmail.test", auth_type="oauth", services=["gmail"]))
    done = env.c.post("/google/accounts/gone/remove", data={"mode": "token_and_config", "confirm": "REMOVE gone"}, headers=ORIGIN)
    assert "removed_all" in done.headers["location"]
    assert all(a.slug != "gone" for a in load_config().google.accounts)


async def test_daemon_applies_invalidate_and_verify_controls_on_the_owner_lane(env):
    from anna.runtime.controls import queue_control
    from anna.runtime.schedule_store import ScheduleStore
    from anna.runtime.scheduler import Scheduler
    from anna.runtime.supervisor import Supervisor
    from tests.test_scheduler import FakeAdapter, FakeAlerter
    env.cfg.scheduler.state_path = str(env.home / "schedules.yaml")
    invalidated = []

    class Clients:
        def invalidate(self, slug): invalidated.append(slug)
        async def run(self, slug, api, op):
            svc = SimpleNamespace(users=lambda: SimpleNamespace(getProfile=lambda **k: SimpleNamespace(execute=lambda: {"emailAddress": "ME@gmail.test"})),
                                  calendars=lambda: SimpleNamespace(get=lambda **k: SimpleNamespace(execute=lambda: {"id": "me@gmail.test"})))
            return op(svc)

    store = ScheduleStore(config=env.cfg, supervisor=Supervisor(config=env.cfg))
    class R:
        async def dispatch(self, ev): ...
    sched = Scheduler(config=env.cfg, store=store, router=R(), adapters={"slack": FakeAdapter()}, alerter=FakeAlerter(), google_clients=Clients())
    queue_control(env.cfg.state_dir, kind="google_invalidate", subject="personal")
    queue_control(env.cfg.state_dir, kind="google_verify", subject="personal")
    await sched._poll_once()
    assert invalidated == ["personal"]
    res = latest_control(env.cfg.state_dir, "google_verify", "personal")
    out = json.loads(res["result"])
    assert res["status"] == "applied" and out["identity"] == "match" and out["services"] == {"gmail": "ok", "calendar": "ok"}
    assert "gmail.test" not in res["result"]                                                              # no addresses stored
    # mismatch + no client
    env.cfg.google.accounts[0].email = "other@gmail.test"
    queue_control(env.cfg.state_dir, kind="google_verify", subject="personal")
    await sched._poll_once()
    assert json.loads(latest_control(env.cfg.state_dir, "google_verify", "personal")["result"])["identity"] == "MISMATCH"
    sched._google_clients = None
    queue_control(env.cfg.state_dir, kind="google_verify", subject="personal")
    await sched._poll_once()
    assert latest_control(env.cfg.state_dir, "google_verify", "personal")["status"] == "rejected"


def test_controls_are_not_queued_without_a_migrated_ledger_and_say_so(env):
    from anna.runtime.controls import ControlError, queue_control
    with pytest.raises(ControlError, match="ledger"):
        queue_control(env.cfg.state_dir, kind="google_verify", subject="personal")
    r = env.c.post("/google/accounts/personal/verify", headers=ORIGIN)
    assert "flash=busy" in r.headers["location"]


def test_cli_hard_rejects_a_mailbox_mismatch_and_imports_the_client(env, monkeypatch, capsys):
    from anna.setup import google_auth as cli
    src = env.tmp / "client.json"; src.write_text(json.dumps(client_json("CLI-SECRET")))
    env.cfg.google_oauth_client_path().unlink()
    ns = SimpleNamespace(path=str(src), replace=False)
    assert cli.cmd_import_client(ns, env.cfg) == 0
    out = capsys.readouterr()
    assert "CLI-SECRET" not in out.out + out.err and import_client_file.__name__
    fake_profile = {"emailAddress": "wrong@gmail.test", "messagesTotal": 1}
    token_file(env).write_text(json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                           "token_uri": "https://oauth2.googleapis.com/token", "scopes": BOTH.split()}))
    monkeypatch.setattr(cli, "load_credentials", lambda cfg, acct: SimpleNamespace())
    import googleapiclient.discovery as disc
    monkeypatch.setattr(disc, "build", lambda *a, **k: SimpleNamespace(users=lambda: SimpleNamespace(
        getProfile=lambda **kw: SimpleNamespace(execute=lambda: fake_profile))))
    assert cli.cmd_verify(SimpleNamespace(slug="personal"), env.cfg) == 3
    assert "wrong mailbox" in capsys.readouterr().err
