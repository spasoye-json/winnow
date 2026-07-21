import datetime

import pytest
from google.oauth2.credentials import Credentials

from winnow.auth import (
    CLIENT_SECRETS_ENV,
    SCOPES,
    client_secrets_path,
    load_credentials,
    save_credentials,
)
from winnow.db import connect, init_db

CLIENT_CONFIG = {
    "installed": {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}


def naive_utc(offset_hours):
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    return now + datetime.timedelta(hours=offset_hours)


def make_credentials(token, expiry):
    installed = CLIENT_CONFIG["installed"]
    return Credentials(
        token=token,
        refresh_token="refresh-token",
        token_uri=installed["token_uri"],
        client_id=installed["client_id"],
        client_secret=installed["client_secret"],
        scopes=list(SCOPES),
        expiry=expiry,
    )


def test_default_client_secrets_path_under_config(monkeypatch):
    monkeypatch.delenv(CLIENT_SECRETS_ENV, raising=False)
    monkeypatch.setenv("HOME", "/home/example")
    assert (
        client_secrets_path() == "/home/example/.config/winnow/client_secret.json"
    )


def test_client_secrets_env_override_takes_precedence(monkeypatch):
    monkeypatch.setenv(CLIENT_SECRETS_ENV, "/custom/secret.json")
    assert client_secrets_path() == "/custom/secret.json"


@pytest.fixture
def conn():
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def test_load_returns_none_when_no_credentials(conn):
    assert load_credentials(conn, CLIENT_CONFIG) is None


def test_credential_store_round_trip(conn):
    expiry = naive_utc(1)
    save_credentials(
        conn,
        make_credentials("access-token", expiry),
        connected_at="2026-07-20T00:00:00+00:00",
    )

    loaded = load_credentials(conn, CLIENT_CONFIG)

    assert loaded.token == "access-token"
    assert loaded.refresh_token == "refresh-token"
    assert loaded.scopes == list(SCOPES)
    assert loaded.expiry == expiry
    assert loaded.valid


def test_expired_credentials_refresh_and_persist(conn, monkeypatch):
    save_credentials(
        conn,
        make_credentials("stale-token", naive_utc(-1)),
        connected_at="2026-07-20T00:00:00+00:00",
    )

    calls = []

    def fake_refresh(self, request):
        calls.append(request)
        self.token = "fresh-token"
        self.expiry = naive_utc(1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)

    loaded = load_credentials(conn, CLIENT_CONFIG)
    assert loaded.token == "fresh-token"
    assert len(calls) == 1

    reloaded = load_credentials(conn, CLIENT_CONFIG)
    assert reloaded.token == "fresh-token"
    assert reloaded.refresh_token == "refresh-token"
    assert len(calls) == 1


def test_refresh_preserves_connected_at(conn, monkeypatch):
    save_credentials(
        conn,
        make_credentials("stale-token", naive_utc(-1)),
        connected_at="2026-07-20T00:00:00+00:00",
    )

    def fake_refresh(self, request):
        self.token = "fresh-token"
        self.expiry = naive_utc(1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)

    load_credentials(conn, CLIENT_CONFIG)

    connected_at = conn.execute(
        "SELECT connected_at FROM oauth_credentials WHERE provider = 'google'"
    ).fetchone()[0]
    assert connected_at == "2026-07-20T00:00:00+00:00"
