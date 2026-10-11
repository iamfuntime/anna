"""Google OAuth + service-account credential management.

This module owns every read or write of the credential files under
``$ANNA_HOME/state/google/``. Two flavors:

* OAuth user-consent (personal Gmail accounts). The OAuth client JSON
  describes the GCP-side Desktop app; the per-account refresh token is
  captured by the setup CLI and persisted to disk. ``load_oauth_credentials``
  reads the refresh token, refreshes the access token if needed, and
  returns a ``google.oauth2.credentials.Credentials`` object.

* Service account with domain-wide delegation (Workspace accounts).
  ``load_service_account_credentials`` reads the SA key JSON, then
  applies ``with_subject(email)`` so requests act as the configured user.

The module is import-safe even if ``google-api-python-client`` is not
installed; the actual imports are deferred until a function is called.
This lets unit tests that mock the credential loaders run on a fresh
checkout without google deps installed.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anna.config import AnnaConfig, GoogleAccountConfig
from anna.log import get_logger

if TYPE_CHECKING:
    # Pure typing imports — these names are referenced in annotations only,
    # so the real package does not need to be installed at module load time.
    from google.oauth2.credentials import Credentials as OAuthCredentials
    from google.oauth2.service_account import Credentials as SACredentials

    AnyGoogleCredentials = OAuthCredentials | SACredentials


# The two scopes that cover phase-1 read-only use. Listed in alphabetical
# order so the comparison strings sent to Google match cached consents.
READONLY_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
)


SERVICE_SCOPES: dict[str, str] = {
    "calendar": "https://www.googleapis.com/auth/calendar.readonly",
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
}


def scopes_for(services: Any) -> list[str]:
    """OAuth scopes for exactly the chosen services (read-only), sorted. Never a global default."""
    return sorted(SERVICE_SCOPES[s] for s in services)


def required_scopes(account: GoogleAccountConfig) -> list[str]:
    return scopes_for(account.services)


class GoogleAuthError(RuntimeError):
    """Raised when credentials cannot be loaded or refreshed."""


class GoogleIdentityMismatch(GoogleAuthError):
    """The authorized Google identity is not the mailbox configured for this slug."""


class TokenMissingError(GoogleAuthError):
    """Raised when an OAuth slug has not been authorized yet.

    The setup CLI catches this and prints the ``add <slug>`` instructions.
    """


def _ensure_state_dir(config: AnnaConfig) -> Path:
    """Create the credential state dir if missing, lock it to 700."""
    path = config.google_state_dir
    path.mkdir(parents=True, exist_ok=True)
    try:
        # Best-effort perm tightening; only matters on POSIX.
        path.chmod(0o700)
    except OSError:
        pass
    return path


def find_account(config: AnnaConfig, slug: str) -> GoogleAccountConfig:
    """Look up an account by slug. Raises if not configured."""
    for acct in config.google.accounts:
        if acct.slug == slug:
            return acct
    raise GoogleAuthError(
        f"no google account with slug {slug!r} in anna.yaml; "
        f"configured slugs: {[a.slug for a in config.google.accounts] or '(none)'}"
    )


def load_oauth_credentials(
    config: AnnaConfig,
    account: GoogleAccountConfig,
    *,
    timeout_seconds: float = 30.0,
) -> OAuthCredentials:
    """Load and refresh OAuth credentials for a personal account.

    Reads the persisted token JSON at ``state/google/token_<slug>.json``,
    refreshes the access token if it has expired, writes the refreshed
    token back to disk (so the next call sees the new expiry), and
    returns the ready-to-use Credentials.

    Raises :class:`TokenMissingError` if the per-account token file does
    not exist yet — the caller (typically the setup CLI) should run the
    ``add`` flow.
    """
    if account.auth_type != "oauth":
        raise GoogleAuthError(
            f"account {account.slug!r} has auth_type={account.auth_type!r}, "
            f"expected 'oauth'"
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    log = get_logger("anna.google.auth").bind(slug=account.slug)
    token_path = config.google_token_path(account)

    if not token_path.is_file():
        raise TokenMissingError(
            f"no OAuth token for slug {account.slug!r}; "
            f"run: python -m anna.setup.google_auth add {account.slug}"
        )

    creds = Credentials.from_authorized_user_file(
        str(token_path),
        required_scopes(account),
    )

    # Refresh if needed. The library exposes a `valid` flag for tokens
    # that are present and not expired; otherwise we attempt a refresh.
    if not creds.valid:
        if not creds.refresh_token:
            raise GoogleAuthError(
                f"OAuth token for {account.slug!r} is invalid and has no "
                f"refresh_token; re-run: python -m anna.setup.google_auth add {account.slug}"
            )
        try:
            # ``Request`` accepts a timeout when it is *called*, not when it
            # is constructed. Credentials.refresh only receives a callable,
            # so wrap it to enforce this runtime deadline for the token HTTP
            # request as well as the discovery/API transport.
            request = Request()

            def bounded_request(*args: Any, **kwargs: Any) -> Any:
                kwargs["timeout"] = timeout_seconds
                return request(*args, **kwargs)

            creds.refresh(bounded_request)
        except Exception as exc:
            raise GoogleAuthError(
                f"failed to refresh OAuth token for {account.slug!r}: {exc}; "
                f"re-run: python -m anna.setup.google_auth add {account.slug}"
            ) from exc
        # Persist the refreshed expiry so we don't refresh on every call.
        try:
            _atomic_private_write(token_path, creds.to_json())
            log.debug("google.oauth.refresh_persisted")
        except OSError as exc:
            log.warning("google.oauth.refresh_persist_failed", error=str(exc))

    return creds


def load_service_account_credentials(
    config: AnnaConfig,
    account: GoogleAccountConfig,
) -> SACredentials:
    """Load a service-account key and apply domain-wide-delegation impersonation.

    Per-Workspace DWD must have been authorized in the Workspace admin
    console with the same scopes we request here. If not, every API call
    will return HTTP 403 ``unauthorized_client``.
    """
    if account.auth_type != "service_account":
        raise GoogleAuthError(
            f"account {account.slug!r} has auth_type={account.auth_type!r}, "
            f"expected 'service_account'"
        )

    # Check key file existence before importing google libs so a missing
    # file gives a clear runbook-friendly error even when the libs aren't
    # installed yet (e.g. fresh dev checkout before `uv sync`).
    key_path = config.resolve_google_credentials_path(account)
    if not key_path.is_file():
        raise GoogleAuthError(
            f"service-account key for {account.slug!r} not found at {key_path}; "
            f"see Inbox/2026-06-01-ANNA-Google-Workspace-OAuth-Setup.md"
        )

    from google.oauth2 import service_account

    base_creds = service_account.Credentials.from_service_account_file(
        str(key_path),
        scopes=required_scopes(account),
    )
    # Domain-wide delegation: impersonate the configured user.
    return base_creds.with_subject(account.email)


def load_credentials(
    config: AnnaConfig,
    account: GoogleAccountConfig,
    *,
    timeout_seconds: float = 30.0,
) -> AnyGoogleCredentials:
    """Auth-type-dispatching loader. The high-level entry point."""
    if account.auth_type == "oauth":
        if timeout_seconds == 30.0:
            return load_oauth_credentials(config, account)
        return load_oauth_credentials(config, account, timeout_seconds=timeout_seconds)
    if account.auth_type == "service_account":
        return load_service_account_credentials(config, account)
    raise GoogleAuthError(
        f"unknown auth_type {account.auth_type!r} for account {account.slug!r}"
    )


def _atomic_private_write(path: Path, content: str) -> None:
    """Replace credential JSON atomically and keep restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def run_oauth_install_flow(
    config: AnnaConfig,
    account: GoogleAccountConfig,
    *,
    open_browser: bool = True,
    timeout_seconds: int = 300,
) -> Path:
    """Drive the OAuth installed-app (loopback) flow for one account from the terminal.

    Uses ``InstalledAppFlow.run_local_server`` on 127.0.0.1 (state + PKCE are generated by the
    library; no hosted callback, no out-of-band flow).  The resulting credentials are only written
    after the granted scopes and the real Gmail/Calendar identity are checked against the slug.
    """
    if account.auth_type != "oauth":
        raise GoogleAuthError(
            f"run_oauth_install_flow called on non-oauth account {account.slug!r}"
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    _ensure_state_dir(config)
    client_path = config.resolve_google_credentials_path(account)
    if not client_path.is_file():
        raise GoogleAuthError(
            f"OAuth client JSON not found at {client_path}; import one with "
            f"`python -m anna.setup.google_auth import-client PATH` (or the dashboard /google page)"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), required_scopes(account))
    # ``access_type='offline'`` + ``prompt='consent'`` make Google return a refresh_token.
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=config.google.oauth_callback_port,
        open_browser=open_browser,
        access_type="offline",
        prompt="consent",
        timeout_seconds=timeout_seconds,
    )
    persist_connected_token(config, account, creds)
    return config.google_token_path(account)


def _api_http(creds: Any, timeout_seconds: float = 30.0) -> Any:
    import google_auth_httplib2
    import httplib2
    return google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout_seconds))


def verify_identity(creds: Any, account: GoogleAccountConfig) -> str:
    """Return the REAL authenticated mailbox address (Gmail profile, else primary calendar id)."""
    from googleapiclient.discovery import build

    http = _api_http(creds)
    if "gmail" in account.services:
        profile = build("gmail", "v1", http=http, cache_discovery=False).users().getProfile(userId="me").execute()
        email = profile.get("emailAddress")
    else:
        email = build("calendar", "v3", http=http, cache_discovery=False).calendars().get(calendarId="primary").execute().get("id")
    if not isinstance(email, str) or "@" not in email:
        raise GoogleAuthError("Google did not report which account was authorized")
    return email


def persist_connected_token(config: AnnaConfig, account: GoogleAccountConfig, creds: Any) -> str:
    """Store a freshly authorized token ONLY if it is the right mailbox with the right permissions.

    The token file for ``account.slug`` is written atomically (0600) after (1) every requested scope
    was actually granted and (2) the authenticated mailbox equals the configured address.  A mismatch
    raises and leaves no token on disk, so a token can never land under the wrong slug.
    """
    granted = set(getattr(creds, "scopes", None) or [])
    missing = [sc for sc in required_scopes(account) if sc not in granted]
    if missing:
        raise GoogleAuthError("not every requested permission was granted; connect again and leave the boxes ticked")
    if not getattr(creds, "refresh_token", None):
        raise GoogleAuthError("Google returned no refresh token; connect again (consent must be re-prompted)")
    actual = verify_identity(creds, account)
    if actual.lower() != account.email.lower():
        raise GoogleIdentityMismatch(
            f"signed in as {actual}, but slug {account.slug!r} is configured for {account.email}; "
            "nothing was saved. Sign in with the configured account or fix the email in the config."
        )
    _ensure_state_dir(config)
    _atomic_private_write(config.google_token_path(account), creds.to_json())
    return actual


# ---------------------------------------------------------------- shared client / token housekeeping

CLIENT_FILE_MAX_BYTES = 65536


def validate_client_file(config: AnnaConfig, source: Path) -> dict[str, str]:
    """Check a user-supplied OAuth client JSON WITHOUT echoing its contents.

    Only a Desktop/"installed" client is acceptable (the loopback flow needs it; a "web" client would
    need a hosted callback).  The path must resolve inside the operator's home or ANNA_HOME, be a
    regular file they own, and be small.  Returns only non-secret facts.
    """
    import json

    try:
        real = Path(os.path.realpath(os.path.expanduser(str(source))))
    except (OSError, ValueError) as exc:
        raise GoogleAuthError("cannot resolve that path") from exc
    roots = [Path(os.path.realpath(Path.home())), Path(os.path.realpath(config.anna_home))]
    if not any(real == r or r in real.parents for r in roots):
        raise GoogleAuthError("the client file must be inside your home directory or ANNA_HOME")
    if not real.is_file():
        raise GoogleAuthError("that path is not a regular file")
    st = real.stat()
    if st.st_uid != os.getuid():
        raise GoogleAuthError("the client file must be owned by the user running ANNA")
    if st.st_size > CLIENT_FILE_MAX_BYTES:
        raise GoogleAuthError("that file is too large to be an OAuth client")
    try:
        data = json.loads(real.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoogleAuthError("that file is not valid JSON") from exc
    if not isinstance(data, dict) or "installed" not in data:
        kind = "web" if isinstance(data, dict) and "web" in data else "unknown"
        raise GoogleAuthError(
            f"this is a {kind!r} OAuth client; ANNA needs a Desktop app client (JSON with an 'installed' section). "
            "Create one under APIs & Services > Credentials > Create credentials > OAuth client ID > Desktop app."
        )
    inst = data["installed"]
    for key in ("client_id", "client_secret", "auth_uri", "token_uri"):
        if not isinstance(inst, dict) or not isinstance(inst.get(key), str) or not inst[key]:
            raise GoogleAuthError(f"the client JSON is missing {key!r}")
    for key in ("auth_uri", "token_uri"):
        host = inst[key].split("/")[2] if inst[key].startswith("https://") and inst[key].count("/") >= 2 else ""
        if not (host == "google.com" or host.endswith(".google.com") or host.endswith(".googleapis.com")):
            raise GoogleAuthError(f"{key} does not point at Google")
    return {"client_id_suffix": inst["client_id"][-8:], "kind": "installed"}


def import_client_file(config: AnnaConfig, source: Path, *, replace: bool = False) -> Path:
    """Copy a validated Desktop OAuth client to the shared location (0600, atomic)."""
    validate_client_file(config, source)
    target = config.google_oauth_client_path()
    if target.exists() and not replace:
        raise GoogleAuthError("a shared OAuth client is already imported; tick 'replace' to overwrite it")
    _ensure_state_dir(config)
    _atomic_private_write(target, Path(os.path.realpath(os.path.expanduser(str(source)))).read_text(encoding="utf-8"))
    return target


def token_status(config: AnnaConfig, account: GoogleAccountConfig) -> dict[str, Any]:
    """Read-only facts about the stored token. Never refreshes, never returns secrets."""
    import json

    path = config.google_token_path(account)
    out: dict[str, Any] = {"present": False, "granted_scopes": [], "missing_scopes": required_scopes(account),
                           "private": None}
    if account.auth_type != "oauth" or not path.is_file():
        return out
    out["present"] = True
    out["private"] = (path.stat().st_mode & 0o077) == 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        granted = [s for s in (data.get("scopes") or []) if isinstance(s, str)]
    except (OSError, ValueError):
        return {**out, "unreadable": True}
    out["granted_scopes"] = sorted(granted)
    out["missing_scopes"] = [s for s in required_scopes(account) if s not in granted]
    return out


def delete_local_token(config: AnnaConfig, account: GoogleAccountConfig) -> bool:
    """Remove ONLY the local token file (the grant at Google stays valid until revoked)."""
    path = config.google_token_path(account)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def revoke_token(config: AnnaConfig, account: GoogleAccountConfig, *, timeout_seconds: float = 15.0) -> None:
    """Revoke the grant at Google (network call). Raises GoogleAuthError if Google refuses."""
    import json

    import requests

    path = config.google_token_path(account)
    try:
        token = json.loads(path.read_text(encoding="utf-8")).get("refresh_token")
    except (OSError, ValueError) as exc:
        raise GoogleAuthError("no readable local token to revoke") from exc
    if not token:
        raise GoogleAuthError("the local token has no refresh token to revoke")
    resp = requests.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=timeout_seconds,
                         headers={"content-type": "application/x-www-form-urlencoded"})
    if resp.status_code != 200:
        raise GoogleAuthError(f"Google did not confirm the revocation (HTTP {resp.status_code})")


def credentials_email(creds: Any) -> str:
    """Best-effort: pull the authenticated user email from a Credentials.

    Used by the verify CLI to confirm the impersonation/auth actually
    landed on the expected mailbox. For service-account creds the
    ``signer_email`` is the SA itself, not the impersonated subject, so
    we prefer the ``subject`` attribute when present.
    """
    subject = getattr(creds, "_subject", None) or getattr(creds, "subject", None)
    if subject:
        return str(subject)
    # OAuth user creds expose ``id_token`` claims after a refresh; the
    # ``email`` claim is not guaranteed but is usually there for the
    # scopes we request. Falling back to "(unknown)" is fine — verify()
    # makes a profile API call anyway.
    return "(unknown)"
