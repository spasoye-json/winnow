import datetime
import json
from types import SimpleNamespace

from google.oauth2.credentials import Credentials

import winnow.auth as auth
import winnow.scoring as scoring
import winnow.transcript as transcript
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


def test_connect_client_secrets_flag_overrides_env(tmp_path, monkeypatch):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])

    secrets = tmp_path / "flag_secret.json"
    secrets.write_text(json.dumps(CLIENT_CONFIG))
    monkeypatch.setenv(auth.CLIENT_SECRETS_ENV, str(tmp_path / "env_secret.json"))

    captured = {}
    fake = SimpleNamespace(
        token="access-token",
        refresh_token="refresh-token",
        expiry=None,
        scopes=list(SCOPES),
    )

    def fake_flow(client_config, scopes=SCOPES):
        captured["client_config"] = client_config
        return fake

    monkeypatch.setattr(auth, "run_consent_flow", fake_flow)

    main(["connect", "--db", str(db_path), "--client-secrets", str(secrets)])

    assert captured["client_config"] == CLIENT_CONFIG


SCORE_PAYLOAD = {
    "scores": {
        "info_density": 8,
        "originality": 7,
        "clickbait_gap": 9,
        "padding": 3,
        "depth": 6,
        "production": 5,
    },
    "overall": 7.2,
    "hard_flags": [],
    "summary": "A summary.",
    "rationale": "A rationale.",
}


def test_score_command_scores_pending_videos(tmp_path, monkeypatch):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])

    conn = connect(str(db_path))
    conn.execute(
        "INSERT INTO channels (yt_channel_id, name, source, added_at) "
        "VALUES ('UC1', 'UC1', 'manual', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO videos (yt_video_id, channel_id, title) "
        "VALUES ('v1', 1, 'A title')"
    )
    conn.commit()
    conn.close()

    def create(*, model, messages, **kwargs):
        message = SimpleNamespace(content=json.dumps(SCORE_PAYLOAD))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    fake_llm = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(scoring, "build_client", lambda: fake_llm)
    monkeypatch.setattr(scoring, "model_name", lambda: "test-model")
    monkeypatch.setattr(
        transcript,
        "fetch_transcript",
        lambda video_id: transcript.Transcript(text="body", language_code="en"),
    )

    main(["score", "--db", str(db_path)])

    conn = connect(str(db_path))
    try:
        model, overall = conn.execute(
            "SELECT model, overall FROM scores"
        ).fetchone()
    finally:
        conn.close()
    assert model == "test-model"
    assert overall == 7.2
