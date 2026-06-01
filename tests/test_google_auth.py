"""Validate google config parsing, account lookup, and credential dispatch.

The tests avoid making real Google API calls. Where we need a Credentials
object, we substitute a sentinel via patched module imports. The full
end-to-end happy path lands later in test_google_server.py with a fake
GoogleClients.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from anna.config import AnnaConfig, GoogleAccountConfig, GoogleConfig
from anna.tools.google_auth import (
    GoogleAuthError,
    TokenMissingError,
    find_account,
    load_credentials,
    load_oauth_credentials,
    load_service_account_credentials,
)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_google_account_slug_rejects_bad_chars() -> None:
    with pytest.raises(Exception) as exc:
        GoogleAccountConfig(
            slug="bad-slug!",
            email="x@y.com",
            auth_type="oauth",
            credentials_file="state/google/oauth_client.json",
        )
    assert "slug" in str(exc.value).lower()


def test_google_account_slug_allows_underscore_and_digits() -> None:
    acct = GoogleAccountConfig(
        slug="personal_2",
        email="x@y.com",
        auth_type="oauth",
        credentials_file="state/google/oauth_client.json",
    )
    assert acct.slug == "personal_2"


def test_google_config_rejects_duplicate_slugs() -> None:
    with pytest.raises(Exception) as exc:
        GoogleConfig(
            enabled=True,
            accounts=[
                GoogleAccountConfig(
                    slug="main", email="a@x.com", auth_type="oauth", credentials_file="x.json"
                ),
                GoogleAccountConfig(
                    slug="main", email="b@x.com", auth_type="oauth", credentials_file="y.json"
                ),
            ],
        )
    assert "duplicate" in str(exc.value).lower()


def test_google_config_defaults_off() -> None:
    cfg = AnnaConfig()
    assert cfg.google.enabled is False
    assert cfg.google.accounts == []
    assert cfg.google.write_enabled is False


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _cfg_with_home(tmp_path: Path) -> AnnaConfig:
    cfg = AnnaConfig()
    object.__setattr__(cfg, "anna_home", tmp_path / "anna_home")
    return cfg


def test_google_state_dir_lives_under_anna_home(tmp_path: Path) -> None:
    cfg = _cfg_with_home(tmp_path)
    assert cfg.google_state_dir == tmp_path / "anna_home" / "state" / "google"


def test_google_token_path_includes_slug(tmp_path: Path) -> None:
    cfg = _cfg_with_home(tmp_path)
    acct = GoogleAccountConfig(
        slug="personal_main",
        email="x@y.com",
        auth_type="oauth",
        credentials_file="state/google/oauth_client.json",
    )
    assert cfg.google_token_path(acct).name == "token_personal_main.json"


def test_resolve_google_credentials_path_relative(tmp_path: Path) -> None:
    cfg = _cfg_with_home(tmp_path)
    acct = GoogleAccountConfig(
        slug="x",
        email="x@y.com",
        auth_type="oauth",
        credentials_file="state/google/oauth_client.json",
    )
    resolved = cfg.resolve_google_credentials_path(acct)
    assert resolved == tmp_path / "anna_home" / "state" / "google" / "oauth_client.json"


def test_resolve_google_credentials_path_absolute(tmp_path: Path) -> None:
    cfg = _cfg_with_home(tmp_path)
    abs_path = tmp_path / "elsewhere" / "creds.json"
    acct = GoogleAccountConfig(
        slug="x",
        email="x@y.com",
        auth_type="oauth",
        credentials_file=str(abs_path),
    )
    assert cfg.resolve_google_credentials_path(acct) == abs_path


# ---------------------------------------------------------------------------
# find_account
# ---------------------------------------------------------------------------


def test_find_account_returns_match() -> None:
    cfg = AnnaConfig()
    cfg.google.enabled = True
    cfg.google.accounts.append(
        GoogleAccountConfig(
            slug="main", email="a@x.com", auth_type="oauth", credentials_file="x.json"
        )
    )
    acct = find_account(cfg, "main")
    assert acct.email == "a@x.com"


def test_find_account_raises_for_unknown_slug() -> None:
    cfg = AnnaConfig()
    cfg.google.enabled = True
    with pytest.raises(GoogleAuthError) as exc:
        find_account(cfg, "missing")
    assert "missing" in str(exc.value)


# ---------------------------------------------------------------------------
# Auth-type dispatch error paths (no real google deps needed)
# ---------------------------------------------------------------------------


def test_load_oauth_rejects_service_account_slug(tmp_path: Path) -> None:
    cfg = _cfg_with_home(tmp_path)
    acct = GoogleAccountConfig(
        slug="sa", email="x@y.com", auth_type="service_account", credentials_file="sa.json"
    )
    with pytest.raises(GoogleAuthError) as exc:
        load_oauth_credentials(cfg, acct)
    assert "expected 'oauth'" in str(exc.value)


def test_load_service_account_rejects_oauth_slug(tmp_path: Path) -> None:
    cfg = _cfg_with_home(tmp_path)
    acct = GoogleAccountConfig(
        slug="oa", email="x@y.com", auth_type="oauth", credentials_file="x.json"
    )
    with pytest.raises(GoogleAuthError) as exc:
        load_service_account_credentials(cfg, acct)
    assert "expected 'service_account'" in str(exc.value)


def test_load_credentials_dispatches_by_auth_type(tmp_path: Path) -> None:
    cfg = _cfg_with_home(tmp_path)
    acct_oauth = GoogleAccountConfig(
        slug="oa", email="x@y.com", auth_type="oauth", credentials_file="x.json"
    )
    acct_sa = GoogleAccountConfig(
        slug="sa", email="x@y.com", auth_type="service_account", credentials_file="sa.json"
    )
    sentinel = object()
    with patch("anna.tools.google_auth.load_oauth_credentials", return_value=sentinel) as oa, \
         patch("anna.tools.google_auth.load_service_account_credentials", return_value=sentinel) as sa:
        assert load_credentials(cfg, acct_oauth) is sentinel
        oa.assert_called_once_with(cfg, acct_oauth)
        sa.assert_not_called()
        oa.reset_mock()
        assert load_credentials(cfg, acct_sa) is sentinel
        sa.assert_called_once_with(cfg, acct_sa)
        oa.assert_not_called()


# ---------------------------------------------------------------------------
# load_oauth_credentials happy path (mocked google libs)
# ---------------------------------------------------------------------------


def test_load_oauth_raises_token_missing_for_unauthorized_slug(tmp_path: Path) -> None:
    """Skip the real google import path if the lib is not installed."""
    pytest.importorskip("google.oauth2.credentials")
    cfg = _cfg_with_home(tmp_path)
    cfg.google_state_dir.mkdir(parents=True, exist_ok=True)
    acct = GoogleAccountConfig(
        slug="oa", email="x@y.com", auth_type="oauth", credentials_file="oauth_client.json"
    )
    # No token file present.
    with pytest.raises(TokenMissingError) as exc:
        load_oauth_credentials(cfg, acct)
    assert "add oa" in str(exc.value)


def test_load_oauth_returns_existing_valid_creds(tmp_path: Path) -> None:
    pytest.importorskip("google.oauth2.credentials")
    cfg = _cfg_with_home(tmp_path)
    cfg.google_state_dir.mkdir(parents=True, exist_ok=True)
    acct = GoogleAccountConfig(
        slug="oa", email="x@y.com", auth_type="oauth", credentials_file="oauth_client.json"
    )
    # Drop a token file so the loader gets that far. Contents don't matter
    # because we mock `from_authorized_user_file` to return a fake creds.
    token_path = cfg.google_token_path(acct)
    token_path.write_text("{}", encoding="utf-8")

    fake_creds = MagicMock()
    fake_creds.valid = True
    with patch(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        return_value=fake_creds,
    ):
        result = load_oauth_credentials(cfg, acct)
    assert result is fake_creds


def test_load_oauth_refreshes_expired_creds_and_persists(tmp_path: Path) -> None:
    pytest.importorskip("google.oauth2.credentials")
    cfg = _cfg_with_home(tmp_path)
    cfg.google_state_dir.mkdir(parents=True, exist_ok=True)
    acct = GoogleAccountConfig(
        slug="oa", email="x@y.com", auth_type="oauth", credentials_file="oauth_client.json"
    )
    token_path = cfg.google_token_path(acct)
    token_path.write_text("{}", encoding="utf-8")

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.refresh_token = "abc"
    fake_creds.to_json.return_value = '{"refreshed": true}'

    with patch(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        return_value=fake_creds,
    ):
        result = load_oauth_credentials(cfg, acct)

    assert result is fake_creds
    fake_creds.refresh.assert_called_once()
    # The refreshed token should have been re-persisted.
    assert token_path.read_text(encoding="utf-8") == '{"refreshed": true}'


def test_load_oauth_errors_when_refresh_token_missing(tmp_path: Path) -> None:
    pytest.importorskip("google.oauth2.credentials")
    cfg = _cfg_with_home(tmp_path)
    cfg.google_state_dir.mkdir(parents=True, exist_ok=True)
    acct = GoogleAccountConfig(
        slug="oa", email="x@y.com", auth_type="oauth", credentials_file="oauth_client.json"
    )
    token_path = cfg.google_token_path(acct)
    token_path.write_text("{}", encoding="utf-8")

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.refresh_token = None

    with patch(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        return_value=fake_creds,
    ):
        with pytest.raises(GoogleAuthError) as exc:
            load_oauth_credentials(cfg, acct)
    assert "refresh_token" in str(exc.value)


# ---------------------------------------------------------------------------
# load_service_account_credentials happy path
# ---------------------------------------------------------------------------


def test_load_service_account_applies_with_subject(tmp_path: Path) -> None:
    pytest.importorskip("google.oauth2.service_account")
    cfg = _cfg_with_home(tmp_path)
    cfg.google_state_dir.mkdir(parents=True, exist_ok=True)
    key_path = cfg.google_state_dir / "sa.json"
    key_path.write_text(json.dumps({"type": "service_account"}), encoding="utf-8")
    acct = GoogleAccountConfig(
        slug="sa", email="me@workspace.com", auth_type="service_account", credentials_file="state/google/sa.json"
    )

    base = MagicMock()
    delegated = MagicMock()
    base.with_subject.return_value = delegated

    with patch(
        "google.oauth2.service_account.Credentials.from_service_account_file",
        return_value=base,
    ) as ctor:
        result = load_service_account_credentials(cfg, acct)

    assert result is delegated
    ctor.assert_called_once()
    base.with_subject.assert_called_once_with("me@workspace.com")


def test_load_service_account_raises_when_key_missing(tmp_path: Path) -> None:
    cfg = _cfg_with_home(tmp_path)
    cfg.google_state_dir.mkdir(parents=True, exist_ok=True)
    acct = GoogleAccountConfig(
        slug="sa", email="me@workspace.com", auth_type="service_account", credentials_file="state/google/nope.json"
    )
    with pytest.raises(GoogleAuthError) as exc:
        load_service_account_credentials(cfg, acct)
    assert "not found" in str(exc.value)
