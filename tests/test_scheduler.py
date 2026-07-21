import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from winnow.db import connect, init_db
from winnow.ingest import LAST_INGEST_KEY
from winnow.scheduler import (
    INGEST_INTERVAL,
    due_check_loop,
    ingest_due,
    scoring_due,
    tick,
)
from winnow.scoring import (
    DAILY_CAP,
    SCORING_COUNT_KEY,
    SCORING_DAY_KEY,
    pacific_day,
)
from winnow.transcript import Transcript

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

PAYLOAD = {
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
    "summary": "A neutral summary.",
    "rationale": "A rationale.",
}


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


class FakeCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create(self, *, model, messages, **kwargs):
        self.calls.append(model)
        content = json.dumps(self.payload)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeLLM:
    def __init__(self, payload=PAYLOAD):
        self.completions = FakeCompletions(payload)
        self.chat = SimpleNamespace(completions=self.completions)


def fetcher(transcript):
    def fetch(yt_video_id):
        return transcript

    return fetch


def add_pending_video(conn, yt_video_id):
    conn.execute(
        "INSERT INTO channels (yt_channel_id, name, source, added_at) "
        "VALUES (?, ?, 'manual', '2026-01-01T00:00:00+00:00')",
        (f"ch-{yt_video_id}", yt_video_id),
    )
    (channel_id,) = conn.execute(
        "SELECT id FROM channels WHERE yt_channel_id = ?", (f"ch-{yt_video_id}",)
    ).fetchone()
    conn.execute(
        "INSERT INTO videos (yt_video_id, channel_id, title, duration_sec) "
        "VALUES (?, ?, 'A title', 600)",
        (yt_video_id, channel_id),
    )
    conn.commit()


def set_day_count(conn, day, count):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)", (SCORING_DAY_KEY, day)
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (SCORING_COUNT_KEY, str(count)),
    )
    conn.commit()


def score_exists(conn, yt_video_id):
    return conn.execute(
        "SELECT 1 FROM scores s JOIN videos v ON v.id = s.video_id "
        "WHERE v.yt_video_id = ?",
        (yt_video_id,),
    ).fetchone() is not None


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


def test_due_check_loop_ticks_then_stops_cleanly():
    async def scenario():
        stop = asyncio.Event()
        ticks = 0

        async def on_tick():
            nonlocal ticks
            ticks += 1
            stop.set()

        await due_check_loop(on_tick, stop, interval=timedelta(minutes=5))
        return ticks

    assert asyncio.run(scenario()) == 1


def test_due_check_loop_repeats_until_stopped():
    async def scenario():
        stop = asyncio.Event()
        ticks = 0

        async def on_tick():
            nonlocal ticks
            ticks += 1
            if ticks == 3:
                stop.set()

        await due_check_loop(on_tick, stop, interval=timedelta(seconds=0.001))
        return ticks

    assert asyncio.run(scenario()) == 3


def test_scoring_due_when_pending_and_under_cap(conn):
    add_pending_video(conn, "v1")
    assert scoring_due(conn, NOW) is True


def test_scoring_not_due_when_no_pending(conn):
    assert scoring_due(conn, NOW) is False


def test_scoring_not_due_when_cap_reached(conn):
    add_pending_video(conn, "v1")
    set_day_count(conn, pacific_day(NOW.isoformat()), DAILY_CAP)
    assert scoring_due(conn, NOW) is False


def test_tick_runs_scoring_when_due(conn):
    seed_last_ingest(conn, NOW.isoformat())
    add_pending_video(conn, "v1")
    llm = FakeLLM()

    tick(
        conn,
        FakeYouTube(),
        now=NOW,
        fetch_transcript=fetcher(Transcript(text="body", language_code="en")),
        llm=llm,
        model="m",
        sleep=lambda _: None,
    )

    assert score_exists(conn, "v1") is True


def test_tick_skips_scoring_when_cap_reached(conn):
    seed_last_ingest(conn, NOW.isoformat())
    add_pending_video(conn, "v1")
    set_day_count(conn, pacific_day(NOW.isoformat()), DAILY_CAP)
    llm = FakeLLM()

    tick(
        conn,
        FakeYouTube(),
        now=NOW,
        fetch_transcript=fetcher(Transcript(text="body", language_code="en")),
        llm=llm,
        model="m",
        sleep=lambda _: None,
    )

    assert score_exists(conn, "v1") is False
    assert llm.completions.calls == []
