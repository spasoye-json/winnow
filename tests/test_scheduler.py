from datetime import UTC, datetime, timedelta

import pytest

from winnow.db import connect, init_db
from winnow.ingest import LAST_INGEST_KEY
from winnow.scheduler import INGEST_INTERVAL, ingest_due, tick

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class FakeRequest:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class FakeSubscriptions:
    def list(self, **kwargs):
        return FakeRequest({"items": []})

    def list_next(self, request, response):
        return None


class FakeYouTube:
    def subscriptions(self):
        return FakeSubscriptions()


@pytest.fixture
def conn():
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def seed_last_ingest(conn, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (LAST_INGEST_KEY, value),
    )
    conn.commit()


def last_ingest(conn):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (LAST_INGEST_KEY,)
    ).fetchone()
    return row[0] if row else None


def test_ingest_due_when_never_run(conn):
    assert ingest_due(conn, NOW) is True


def test_ingest_due_when_last_older_than_six_hours(conn):
    seed_last_ingest(conn, (NOW - INGEST_INTERVAL - timedelta(minutes=1)).isoformat())
    assert ingest_due(conn, NOW) is True


def test_ingest_due_at_exactly_six_hours(conn):
    seed_last_ingest(conn, (NOW - INGEST_INTERVAL).isoformat())
    assert ingest_due(conn, NOW) is True


def test_ingest_not_due_within_six_hours(conn):
    seed_last_ingest(conn, (NOW - timedelta(hours=5, minutes=59)).isoformat())
    assert ingest_due(conn, NOW) is False


def test_first_tick_after_wake_catches_up(conn):
    seed_last_ingest(conn, (NOW - timedelta(days=3)).isoformat())
    assert ingest_due(conn, NOW) is True


def test_tick_runs_ingest_when_due(conn):
    tick(conn, FakeYouTube(), now=NOW)
    assert last_ingest(conn) == NOW.isoformat()


def test_tick_skips_ingest_when_not_due(conn):
    recent = (NOW - timedelta(hours=1)).isoformat()
    seed_last_ingest(conn, recent)
    tick(conn, FakeYouTube(), now=NOW)
    assert last_ingest(conn) == recent
