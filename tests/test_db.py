import sqlite3

import pytest

from winnow.db import connect, init_db

EXPECTED_TABLES = {
    "oauth_credentials",
    "channels",
    "topics",
    "videos",
    "scores",
    "feedback",
    "settings",
}


def table_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


def column_names(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def test_init_creates_all_tables(conn):
    init_db(conn)
    assert EXPECTED_TABLES <= table_names(conn)


def test_channels_carry_curation_flags(conn):
    init_db(conn)
    columns = column_names(conn, "channels")
    assert {"source", "excluded", "exempt_low_transcript", "active"} <= columns


def test_videos_carry_transcript_status_and_caption_language(conn):
    init_db(conn)
    columns = column_names(conn, "videos")
    assert {"transcript_status", "caption_language"} <= columns


def test_scores_carry_model_and_prompt_version(conn):
    init_db(conn)
    columns = column_names(conn, "scores")
    assert {"model", "prompt_version"} <= columns


def test_new_video_defaults_to_pending_transcript_status(conn):
    init_db(conn)
    conn.execute(
        "INSERT INTO channels (yt_channel_id, source) VALUES ('c1', 'manual')"
    )
    conn.execute("INSERT INTO videos (yt_video_id) VALUES ('v1')")
    status = conn.execute(
        "SELECT transcript_status FROM videos WHERE yt_video_id = 'v1'"
    ).fetchone()[0]
    assert status == "pending"


@pytest.mark.parametrize("status", ["pending", "ok", "no_transcript", "fetch_failed"])
def test_transcript_status_accepts_vocabulary(conn, status):
    init_db(conn)
    conn.execute(
        "INSERT INTO videos (yt_video_id, transcript_status) VALUES (?, ?)",
        (status, status),
    )


def test_transcript_status_rejects_unknown_value(conn):
    init_db(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO videos (yt_video_id, transcript_status) VALUES ('v1', 'bogus')"
        )


def test_init_adds_topic_id_to_a_pre_topic_videos_table(conn):
    conn.execute(
        "CREATE TABLE videos (id INTEGER PRIMARY KEY, "
        "yt_video_id TEXT NOT NULL UNIQUE, channel_id INTEGER)"
    )
    init_db(conn)
    assert "topic_id" in column_names(conn, "videos")
    conn.execute("INSERT INTO topics (query) VALUES ('solar')")
    conn.execute("INSERT INTO videos (yt_video_id, topic_id) VALUES ('v1', 1)")


def test_init_twice_is_a_noop(conn):
    init_db(conn)
    conn.execute(
        "INSERT INTO channels (yt_channel_id, source) VALUES ('c1', 'manual')"
    )
    conn.commit()
    init_db(conn)
    count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    assert count == 1
    assert EXPECTED_TABLES <= table_names(conn)
