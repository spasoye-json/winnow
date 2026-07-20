import json
from types import SimpleNamespace

import pytest
from requests.exceptions import ConnectionError, HTTPError
from youtube_transcript_api import (
    IpBlocked,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeRequestFailed,
)

from winnow.db import connect, init_db
from winnow.scoring import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    FULL_CONFIDENCE,
    INITIAL_BACKOFF_SEC,
    MAX_TRANSIENT_ATTEMPTS,
    METADATA_ONLY_CONFIDENCE,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_client,
    model_name,
    run_scoring,
)
from winnow.transcript import Transcript, fetch_transcript

NOW = "2026-07-20T00:00:00+00:00"

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
    "hard_flags": ["ai_voice"],
    "summary": "A neutral summary.",
    "rationale": "Because the analysis is dense and original.",
}


class FakeCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        content = json.dumps(self.payload)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeLLM:
    def __init__(self, payload=PAYLOAD):
        self.completions = FakeCompletions(payload)
        self.chat = SimpleNamespace(completions=self.completions)


def fetcher(transcript):
    calls = []

    def fetch(yt_video_id):
        calls.append(yt_video_id)
        return transcript

    fetch.calls = calls
    return fetch


def raising_fetcher(exc, *, recover_after=None, transcript=None):
    calls = []

    def fetch(yt_video_id):
        calls.append(yt_video_id)
        if recover_after is not None and len(calls) > recover_after:
            return transcript
        raise exc

    fetch.calls = calls
    return fetch


def recording_sleep():
    delays = []

    def sleep(seconds):
        delays.append(seconds)

    sleep.delays = delays
    return sleep


def transcript_row(conn, yt_video_id):
    return conn.execute(
        "SELECT transcript_status, transcript_attempts FROM videos "
        "WHERE yt_video_id = ?",
        (yt_video_id,),
    ).fetchone()


def confidence(conn, yt_video_id):
    return conn.execute(
        "SELECT s.confidence FROM scores s JOIN videos v ON v.id = s.video_id "
        "WHERE v.yt_video_id = ?",
        (yt_video_id,),
    ).fetchone()


class FakeAvailable:
    def __init__(self, language_code):
        self.language_code = language_code


class FakeFetched:
    def __init__(self, language_code, texts):
        self.language_code = language_code
        self.snippets = [SimpleNamespace(text=text) for text in texts]


class FakeTranscriptApi:
    def __init__(self, available, fetched):
        self._available = available
        self._fetched = fetched
        self.requested_languages = None

    def list(self, video_id):
        return [FakeAvailable(code) for code in self._available]

    def fetch(self, video_id, languages=("en",)):
        self.requested_languages = list(languages)
        return self._fetched


@pytest.fixture
def conn():
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def add_channel(conn, channel_id):
    conn.execute(
        "INSERT INTO channels (yt_channel_id, name, source, added_at) "
        "VALUES (?, ?, 'manual', '2026-01-01T00:00:00+00:00')",
        (channel_id, channel_id),
    )
    conn.commit()


def add_pending_video(conn, channel_id, yt_video_id, *, title="A title",
                      duration_sec=600, view_count=100):
    (internal_id,) = conn.execute(
        "SELECT id FROM channels WHERE yt_channel_id = ?", (channel_id,)
    ).fetchone()
    conn.execute(
        "INSERT INTO videos "
        "(yt_video_id, channel_id, title, duration_sec, view_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (yt_video_id, internal_id, title, duration_sec, view_count),
    )
    conn.commit()


def score_row(conn, yt_video_id):
    return conn.execute(
        "SELECT s.overall, s.info_density, s.originality, s.clickbait_gap, "
        "s.padding, s.depth, s.production, s.hard_flags, s.summary, "
        "s.rationale, s.model, s.prompt_version, s.scored_at "
        "FROM scores s JOIN videos v ON v.id = s.video_id "
        "WHERE v.yt_video_id = ?",
        (yt_video_id,),
    ).fetchone()


def test_pending_video_scored_end_to_end(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600, view_count=42)
    llm = FakeLLM()

    run_scoring(
        conn,
        fetcher(Transcript(text="the transcript body", language_code="en")),
        llm,
        model="gemini-3.1-flash-lite",
        now=NOW,
    )

    row = score_row(conn, "v1")
    assert row == (
        7.2,
        8,
        7,
        9,
        3,
        6,
        5,
        json.dumps(["ai_voice"]),
        "A neutral summary.",
        "Because the analysis is dense and original.",
        "gemini-3.1-flash-lite",
        PROMPT_VERSION,
        NOW,
    )


def test_transcript_status_and_language_stored(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="es")),
        FakeLLM(),
        model="m",
        now=NOW,
    )

    status, language = conn.execute(
        "SELECT transcript_status, caption_language FROM videos "
        "WHERE yt_video_id = 'v1'"
    ).fetchone()
    assert status == "ok"
    assert language == "es"


def test_system_prompt_is_first_and_identical_across_calls(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", title="First")
    add_pending_video(conn, "UC1", "v2", title="Second")
    llm = FakeLLM()

    run_scoring(
        conn,
        fetcher(Transcript(text="unique body text", language_code="en")),
        llm,
        model="m",
        now=NOW,
    )

    calls = llm.completions.calls
    assert len(calls) == 2
    first_messages = calls[0]["messages"]
    second_messages = calls[1]["messages"]
    assert first_messages[0]["role"] == "system"
    assert first_messages[0]["content"] == SYSTEM_PROMPT
    assert first_messages[0]["content"] == second_messages[0]["content"]
    assert first_messages[-1]["role"] == "user"
    assert "unique body text" in first_messages[-1]["content"]


def test_score_row_records_model_and_prompt_version(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        FakeLLM(),
        model="deepseek-v4-flash",
        now=NOW,
    )

    model, prompt_version = conn.execute(
        "SELECT model, prompt_version FROM scores"
    ).fetchone()
    assert model == "deepseek-v4-flash"
    assert prompt_version == PROMPT_VERSION
    assert isinstance(prompt_version, int)


def test_only_pending_videos_are_scored(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "pending")
    add_pending_video(conn, "UC1", "done")
    conn.execute(
        "UPDATE videos SET transcript_status = 'ok' WHERE yt_video_id = 'done'"
    )
    conn.commit()
    llm = FakeLLM()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
    )

    assert score_row(conn, "pending") is not None
    assert score_row(conn, "done") is None


def test_fetch_transcript_spans_all_languages_and_returns_track_language():
    fetched = FakeFetched("es", ["hola", "mundo"])
    api = FakeTranscriptApi(available=["en", "es", "fr"], fetched=fetched)

    result = fetch_transcript("vid", api=api)

    assert api.requested_languages == ["en", "es", "fr"]
    assert result.language_code == "es"
    assert result.text == "hola mundo"


def test_client_config_defaults_from_env(monkeypatch):
    monkeypatch.delenv("SCORING_BASE_URL", raising=False)
    monkeypatch.delenv("SCORING_MODEL", raising=False)
    monkeypatch.setenv("SCORING_API_KEY", "test-key")

    assert model_name() == DEFAULT_MODEL
    assert DEFAULT_MODEL == "gemini-3.1-flash-lite"
    assert str(build_client().base_url) == DEFAULT_BASE_URL


def test_client_config_overridden_by_env(monkeypatch):
    monkeypatch.setenv("SCORING_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("SCORING_BASE_URL", "https://api.deepseek.com/")
    monkeypatch.setenv("SCORING_API_KEY", "test-key")

    assert model_name() == "deepseek-v4-flash"
    assert str(build_client().base_url) == "https://api.deepseek.com/"


def test_successful_fetch_scores_at_full_confidence(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        FakeLLM(),
        model="m",
        now=NOW,
    )

    assert confidence(conn, "v1") == (FULL_CONFIDENCE,)


@pytest.mark.parametrize(
    "exc",
    [
        TranscriptsDisabled("v1"),
        VideoUnavailable("v1"),
    ],
)
def test_permanent_failure_sets_no_transcript_and_scores_metadata_only(conn, exc):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    fetch = raising_fetcher(exc)

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW)

    assert transcript_row(conn, "v1") == ("no_transcript", 0)
    assert confidence(conn, "v1") == (METADATA_ONLY_CONFIDENCE,)
    assert fetch.calls == ["v1"]


def test_permanent_failure_is_never_retried(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    fetch = raising_fetcher(TranscriptsDisabled("v1"))

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW)
    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW)

    assert fetch.calls == ["v1"]


@pytest.mark.parametrize("exc", [RequestBlocked("v1"), IpBlocked("v1")])
def test_ip_block_trips_circuit_breaker_and_defers_batch(conn, exc):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "first")
    add_pending_video(conn, "UC1", "second")
    fetch = raising_fetcher(exc)

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW)

    assert fetch.calls == ["first"]
    assert transcript_row(conn, "first") == ("pending", 0)
    assert transcript_row(conn, "second") == ("pending", 0)
    assert score_row(conn, "first") is None


def test_ip_block_mid_batch_keeps_earlier_scores_and_defers_remainder(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "first")
    add_pending_video(conn, "UC1", "second")
    add_pending_video(conn, "UC1", "third")
    calls = []

    def fetch(yt_video_id):
        calls.append(yt_video_id)
        if yt_video_id == "first":
            return Transcript(text="body", language_code="en")
        raise IpBlocked(yt_video_id)

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW)

    assert calls == ["first", "second"]
    assert transcript_row(conn, "first") == ("ok", 0)
    assert score_row(conn, "first") is not None
    assert transcript_row(conn, "second") == ("pending", 0)
    assert transcript_row(conn, "third") == ("pending", 0)


def test_server_error_is_transient_and_defers(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    error = YouTubeRequestFailed(
        "v1", HTTPError("503 Server Error: Service Unavailable")
    )
    fetch = raising_fetcher(error)
    sleep = recording_sleep()

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW, sleep=sleep)

    assert len(fetch.calls) == MAX_TRANSIENT_ATTEMPTS
    assert transcript_row(conn, "v1") == ("pending", 0)
    assert score_row(conn, "v1") is None


def test_client_error_is_unknown_and_records_attempt(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    error = YouTubeRequestFailed("v1", HTTPError("404 Client Error: Not Found"))
    fetch = raising_fetcher(error)
    sleep = recording_sleep()

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW, sleep=sleep)

    assert fetch.calls == ["v1"]
    assert sleep.delays == []
    assert transcript_row(conn, "v1") == ("pending", 1)
    assert score_row(conn, "v1") is None


def test_transient_failure_backs_off_then_defers(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    error = YouTubeRequestFailed("v1", HTTPError("429 Client Error: Too Many Requests"))
    fetch = raising_fetcher(error)
    sleep = recording_sleep()

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW, sleep=sleep)

    assert len(fetch.calls) == MAX_TRANSIENT_ATTEMPTS
    assert sleep.delays == [
        INITIAL_BACKOFF_SEC * (2**attempt)
        for attempt in range(MAX_TRANSIENT_ATTEMPTS - 1)
    ]
    assert transcript_row(conn, "v1") == ("pending", 0)
    assert score_row(conn, "v1") is None


def test_transient_failure_that_recovers_is_scored(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    error = ConnectionError("temporary network glitch")
    fetch = raising_fetcher(
        error,
        recover_after=1,
        transcript=Transcript(text="body", language_code="en"),
    )
    sleep = recording_sleep()

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW, sleep=sleep)

    assert transcript_row(conn, "v1") == ("ok", 0)
    assert confidence(conn, "v1") == (FULL_CONFIDENCE,)
    assert sleep.delays == [INITIAL_BACKOFF_SEC]


def test_unknown_failure_retries_across_two_runs_then_fetch_failed(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    fetch = raising_fetcher(PoTokenRequired("v1"))

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW)

    assert transcript_row(conn, "v1") == ("pending", 1)
    assert score_row(conn, "v1") is None

    run_scoring(conn, fetch, FakeLLM(), model="m", now=NOW)

    assert transcript_row(conn, "v1") == ("fetch_failed", 2)
    assert confidence(conn, "v1") == (METADATA_ONLY_CONFIDENCE,)
