import datetime
import json

from google.oauth2.credentials import Credentials

import winnow.auth as auth
from winnow.auth import SCOPES
from winnow.cli import main
from winnow.db import connect

CLIENT_CONFIG = {
    "installed": {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

EXPECTED_TABLES = {
    "oauth_credentials",
    "channels",
    "topics",
    "videos",
    "scores",
    "feedback",
    "settings",
}


def table_names(path):
    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def test_init_command_creates_database(tmp_path):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])
    assert db_path.exists()
    assert EXPECTED_TABLES <= table_names(str(db_path))


def test_init_command_is_idempotent(tmp_path):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])
    main(["init", "--db", str(db_path)])
    assert EXPECTED_TABLES <= table_names(str(db_path))


def test_connect_command_persists_credentials(tmp_path, monkeypatch):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])

    secrets = tmp_path / "client_secrets.json"
    secrets.write_text(json.dumps(CLIENT_CONFIG))

    expiry = datetime.datetime.now(datetime.UTC).replace(
        tzinfo=None
    ) + datetime.timedelta(hours=1)
    installed = CLIENT_CONFIG["installed"]
    fake = Credentials(
        token="access-token",
        refresh_token="refresh-token",
        token_uri=installed["token_uri"],
        client_id=installed["client_id"],
        client_secret=installed["client_secret"],
        scopes=list(SCOPES),
        expiry=expiry,
    )

    captured = {}

    def fake_flow(client_config, scopes=SCOPES):
        captured["client_config"] = client_config
        captured["scopes"] = scopes
        return fake

    monkeypatch.setattr(auth, "run_consent_flow", fake_flow)

    main(["connect", "--db", str(db_path), "--client-secrets", str(secrets)])

    assert captured["client_config"] == CLIENT_CONFIG
    assert captured["scopes"] == SCOPES

    conn = connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT refresh_token, access_token, scopes, connected_at "
            "FROM oauth_credentials WHERE provider = 'google'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "refresh-token"
    assert row[1] == "access-token"
    assert row[2] == " ".join(SCOPES)
    assert row[3] is not None
