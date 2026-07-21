import datetime
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
CLIENT_SECRETS_ENV = "WINNOW_CLIENT_SECRETS"
DEFAULT_CLIENT_SECRETS = "~/.config/winnow/client_secret.json"


def client_secrets_path():
    return os.environ.get(
        CLIENT_SECRETS_ENV, os.path.expanduser(DEFAULT_CLIENT_SECRETS)
    )


def load_client_config(path):
    with open(path) as f:
        return json.load(f)


def run_consent_flow(client_config, scopes=SCOPES):
    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    return flow.run_local_server(port=0)


def save_credentials(conn, creds, connected_at=None):
    conn.execute("DELETE FROM oauth_credentials WHERE provider = 'google'")
    conn.execute(
        """
        INSERT INTO oauth_credentials
            (provider, refresh_token, access_token, expires_at, scopes, connected_at)
        VALUES ('google', ?, ?, ?, ?, ?)
        """,
        (
            creds.refresh_token,
            creds.token,
            _format_expiry(creds.expiry),
            _format_scopes(creds.scopes),
            connected_at,
        ),
    )
    conn.commit()


def load_credentials(conn, client_config, request=None):
    row = conn.execute(
        """
        SELECT refresh_token, access_token, expires_at, scopes
        FROM oauth_credentials WHERE provider = 'google'
        """
    ).fetchone()
    if row is None:
        return None

    refresh_token, access_token, expires_at, scopes = row
    installed = _installed(client_config)
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=installed["token_uri"],
        client_id=installed["client_id"],
        client_secret=installed["client_secret"],
        scopes=_parse_scopes(scopes),
        expiry=_parse_expiry(expires_at),
    )

    if not creds.valid and creds.refresh_token:
        creds.refresh(request or Request())
        _persist_refresh(conn, creds)

    return creds


def _persist_refresh(conn, creds):
    conn.execute(
        """
        UPDATE oauth_credentials
        SET access_token = ?, expires_at = ?
        WHERE provider = 'google'
        """,
        (creds.token, _format_expiry(creds.expiry)),
    )
    conn.commit()


def _installed(client_config):
    return client_config.get("installed") or client_config.get("web") or client_config


def _format_expiry(expiry):
    return expiry.isoformat() if expiry else None


def _parse_expiry(value):
    return datetime.datetime.fromisoformat(value) if value else None


def _format_scopes(scopes):
    return " ".join(scopes) if scopes else None


def _parse_scopes(value):
    return value.split() if value else None
