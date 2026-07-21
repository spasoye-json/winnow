import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from openai import (
    BadRequestError,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
)
from requests.exceptions import ConnectionError, HTTPError
from youtube_transcript_api import (
    AgeRestricted,
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeRequestFailed,
)

from winnow.db import connect, init_db
from winnow.scoring import (
    DAILY_CAP,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    FULL_CONFIDENCE,
    INITIAL_BACKOFF_SEC,
    LLM_INITIAL_BACKOFF_SEC,
    LLM_MAX_ATTEMPTS,
    MAX_CONSECUTIVE_RATE_LIMITS,
    MAX_TRANSIENT_ATTEMPTS,
    METADATA_ONLY_CONFIDENCE,
    OMISSION_MARKER,
    PACING_SEC,
    PROMPT_VERSION,
    SCORING_COUNT_KEY,
    SCORING_DAY_KEY,
    SYSTEM_PROMPT,
    build_client,
    day_count,
    model_name,
    pacific_day,
    run_scoring,
)
from winnow.transcript import Snippet, Transcript, fetch_transcript

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

PAYLOAD_NO_FLAGS = {**PAYLOAD, "hard_flags": []}


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


def api_error(cls, status, *, retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    response = httpx.Response(
        status, headers=headers, request=httpx.Request("POST", "http://test")
    )
    return cls("boom", response=response, body=None)


class FlakyCompletions:
    def __init__(self, errors, payload=PAYLOAD):
        self.errors = list(errors)
        self.payload = payload
        self.calls = []

    def create(self, *, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        content = json.dumps(self.payload)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FlakyLLM:
    def __init__(self, errors, payload=PAYLOAD):
        self.completions = FlakyCompletions(errors, payload)
        self.chat = SimpleNamespace(completions=self.completions)


def constant_rand(value):
    def rand():
        return value

    return rand


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
        self.snippets = [
            SimpleNamespace(text=text, start=float(i))
            for i, text in enumerate(texts)
        ]


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


def add_channel(conn, channel_id, *, exempt_low_transcript=0):
    conn.execute(
        "INSERT INTO channels "
        "(yt_channel_id, name, source, exempt_low_transcript, added_at) "
        "VALUES (?, ?, 'manual', ?, '2026-01-01T00:00:00+00:00')",
        (channel_id, channel_id, exempt_low_transcript),
    )
    conn.commit()


def add_pending_video(conn, channel_id, yt_video_id, *, title="A title",
                      duration_sec=600, view_count=100, published_at=None):
    (internal_id,) = conn.execute(
        "SELECT id FROM channels WHERE yt_channel_id = ?", (channel_id,)
    ).fetchone()
    conn.execute(
        "INSERT INTO videos "
        "(yt_video_id, channel_id, title, duration_sec, view_count, published_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (yt_video_id, internal_id, title, duration_sec, view_count, published_at),
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


def hard_flags(conn, yt_video_id):
    (raw,) = conn.execute(
        "SELECT s.hard_flags FROM scores s JOIN videos v ON v.id = s.video_id "
        "WHERE v.yt_video_id = ?",
        (yt_video_id,),
    ).fetchone()
    return json.loads(raw)


def dimensions(conn, yt_video_id):
    return conn.execute(
        "SELECT s.info_density, s.originality, s.clickbait_gap, s.padding, "
        "s.depth, s.production FROM scores s JOIN videos v ON v.id = s.video_id "
        "WHERE v.yt_video_id = ?",
        (yt_video_id,),
    ).fetchone()


def test_pending_video_scored_end_to_end(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600, view_count=42)
    llm = FakeLLM()

    run_scoring(
        conn,
        fetcher(Transcript(text=" ".join(["word"] * 400), language_code="en")),
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


def test_prompt_version_is_two():
    assert PROMPT_VERSION == 2


def test_system_prompt_requires_english_summary_and_rationale():
    lowered = SYSTEM_PROMPT.lower()
    assert "any language" in lowered
    assert "english" in lowered
    english_index = lowered.index("english")
    assert "summary" in lowered[english_index - 200:english_index + 200]
    assert "rationale" in lowered[english_index - 200:english_index + 200]


def test_system_prompt_explains_omission_marker():
    assert OMISSION_MARKER in SYSTEM_PROMPT
    lowered = SYSTEM_PROMPT.lower()
    assert "head" in lowered
    assert "middle" in lowered
    assert "tail" in lowered


def test_scoring_request_uses_schema_response_format(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    llm = FakeLLM()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
    )

    response_format = llm.completions.calls[0]["kwargs"]["response_format"]
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]["schema"]
    props = schema["properties"]
    assert set(props["scores"]["properties"]) == {
        "info_density",
        "originality",
        "clickbait_gap",
        "padding",
        "depth",
        "production",
    }
    assert "overall" in props
    assert "hard_flags" in props
    assert "summary" in props
    assert "rationale" in props


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
        sleep=recording_sleep(),
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


def long_transcript(count=1000):
    snippets = tuple(
        Snippet(start=float(i), text=f"seg{i:04d} " * 40) for i in range(count)
    )
    text = " ".join(snippet.text for snippet in snippets)
    return Transcript(text=text, language_code="en", snippets=snippets)


def user_content(llm):
    return llm.completions.calls[0]["messages"][-1]["content"]


def test_short_transcript_is_sent_whole_without_stats_or_markers(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600)
    llm = FakeLLM()

    run_scoring(
        conn,
        fetcher(Transcript(text="a short body", language_code="en")),
        llm,
        model="m",
        now=NOW,
    )

    content = user_content(llm)
    assert "a short body" in content
    assert OMISSION_MARKER not in content
    assert "words per minute" not in content


def test_long_transcript_is_sampled_head_middle_tail(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=1000)
    llm = FakeLLM()

    run_scoring(conn, fetcher(long_transcript()), llm, model="m", now=NOW)

    content = user_content(llm)
    assert content.count(OMISSION_MARKER) == 2
    assert "seg0000" in content
    assert "seg0500" in content
    assert "seg0999" in content
    assert "seg0200" not in content
    assert "seg0800" not in content


def test_word_triggered_transcript_within_token_budget_is_sent_once_whole(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=1000)
    llm = FakeLLM()
    snippets = tuple(
        Snippet(start=float(i), text=("pad " * 30).strip()) for i in range(335)
    )
    text = " ".join(snippet.text for snippet in snippets)
    transcript = Transcript(text=text, language_code="en", snippets=snippets)

    run_scoring(conn, fetcher(transcript), llm, model="m", now=NOW)

    content = user_content(llm)
    assert "10050 words" in content
    assert OMISSION_MARKER not in content
    assert content.count("pad") == 10050


def test_overlapping_segments_merge_without_duplication(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=188)
    llm = FakeLLM()
    snippets = tuple(
        Snippet(start=float(i), text=(f"seg{i:04d} " * 40).strip())
        for i in range(188)
    )
    text = " ".join(snippet.text for snippet in snippets)
    transcript = Transcript(text=text, language_code="en", snippets=snippets)

    run_scoring(conn, fetcher(transcript), llm, model="m", now=NOW)

    content = user_content(llm)
    assert content.count(OMISSION_MARKER) == 1
    assert content.count("seg0070") == 40
    assert "seg0000" in content
    assert "seg0130" not in content
    assert "seg0187" in content


def test_long_transcript_stats_line_uses_full_word_count(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=1000)
    llm = FakeLLM()

    run_scoring(conn, fetcher(long_transcript()), llm, model="m", now=NOW)

    content = user_content(llm)
    assert "40000 words" in content
    assert "1000s duration" in content
    assert "2400.0 words per minute" in content
    stats_index = content.index("words per minute")
    assert stats_index < content.index(OMISSION_MARKER)


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
        NoTranscriptFound("v1", ["en"], None),
        VideoUnavailable("v1"),
        VideoUnplayable("v1", "This video is unavailable", []),
        AgeRestricted("v1"),
    ],
)
def test_permanent_failure_sets_no_transcript_and_scores_metadata_only(conn, exc):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    fetch = raising_fetcher(exc)
    llm = FakeLLM()

    run_scoring(conn, fetch, llm, model="m", now=NOW)

    assert transcript_row(conn, "v1") == ("no_transcript", 0)
    assert confidence(conn, "v1") == (METADATA_ONLY_CONFIDENCE,)
    assert fetch.calls == ["v1"]
    content = user_content(llm)
    assert "Title: A title" in content
    assert "(unavailable; metadata only)" in content


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


def test_low_transcript_flag_fires_far_below_duration_ratio(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600)

    run_scoring(
        conn,
        fetcher(Transcript(text="a very short body", language_code="en")),
        FakeLLM(PAYLOAD_NO_FLAGS),
        model="m",
        now=NOW,
    )

    assert hard_flags(conn, "v1") == ["low_transcript"]


def test_no_low_transcript_flag_when_transcript_meets_ratio(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600)

    run_scoring(
        conn,
        fetcher(Transcript(text=" ".join(["word"] * 400), language_code="en")),
        FakeLLM(PAYLOAD_NO_FLAGS),
        model="m",
        now=NOW,
    )

    assert hard_flags(conn, "v1") == []


def test_ai_voice_flag_from_rubric_is_persisted(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600)

    run_scoring(
        conn,
        fetcher(Transcript(text=" ".join(["word"] * 400), language_code="en")),
        FakeLLM(PAYLOAD),
        model="m",
        now=NOW,
    )

    assert hard_flags(conn, "v1") == ["ai_voice"]


def test_exempt_channel_skips_low_transcript_but_scores_all_dimensions(conn):
    add_channel(conn, "UC1", exempt_low_transcript=1)
    add_pending_video(conn, "UC1", "v1", duration_sec=600)

    run_scoring(
        conn,
        fetcher(Transcript(text="a very short body", language_code="en")),
        FakeLLM(PAYLOAD_NO_FLAGS),
        model="m",
        now=NOW,
    )

    assert hard_flags(conn, "v1") == []
    assert dimensions(conn, "v1") == (8, 7, 9, 3, 6, 5)


def test_exempt_channel_still_keeps_ai_voice_flag(conn):
    add_channel(conn, "UC1", exempt_low_transcript=1)
    add_pending_video(conn, "UC1", "v1", duration_sec=600)

    run_scoring(
        conn,
        fetcher(Transcript(text="a very short body", language_code="en")),
        FakeLLM(PAYLOAD),
        model="m",
        now=NOW,
    )

    assert hard_flags(conn, "v1") == ["ai_voice"]


def test_transcript_exactly_at_ratio_is_not_flagged(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600)

    run_scoring(
        conn,
        fetcher(Transcript(text=" ".join(["word"] * 300), language_code="en")),
        FakeLLM(PAYLOAD_NO_FLAGS),
        model="m",
        now=NOW,
    )

    assert hard_flags(conn, "v1") == []


def test_rubric_low_transcript_is_ignored_when_ratio_is_met(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600)

    run_scoring(
        conn,
        fetcher(Transcript(text=" ".join(["word"] * 400), language_code="en")),
        FakeLLM({**PAYLOAD, "hard_flags": ["low_transcript"]}),
        model="m",
        now=NOW,
    )

    assert hard_flags(conn, "v1") == []


def test_ai_voice_and_low_transcript_flags_combine(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600)

    run_scoring(
        conn,
        fetcher(Transcript(text="a very short body", language_code="en")),
        FakeLLM(PAYLOAD),
        model="m",
        now=NOW,
    )

    assert hard_flags(conn, "v1") == ["ai_voice", "low_transcript"]


def test_metadata_only_video_gets_no_low_transcript_flag(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", duration_sec=600)

    run_scoring(
        conn,
        raising_fetcher(TranscriptsDisabled("v1")),
        FakeLLM(PAYLOAD_NO_FLAGS),
        model="m",
        now=NOW,
    )

    assert hard_flags(conn, "v1") == []


def set_day_count(conn, day, count):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)", (SCORING_DAY_KEY, day)
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (SCORING_COUNT_KEY, str(count)),
    )
    conn.commit()


def scored_ids(llm):
    return [
        call["messages"][-1]["content"].split("\n", 1)[0]
        for call in llm.completions.calls
    ]


def test_llm_rate_limit_retries_with_full_jitter_then_scores(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    llm = FlakyLLM([api_error(RateLimitError, 429), api_error(RateLimitError, 429)])
    sleep = recording_sleep()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
        sleep=sleep,
        rand=constant_rand(1.0),
    )

    assert len(llm.completions.calls) == 3
    assert sleep.delays == [
        LLM_INITIAL_BACKOFF_SEC,
        LLM_INITIAL_BACKOFF_SEC * 2,
    ]
    assert score_row(conn, "v1") is not None


def test_llm_backoff_honors_retry_after_header(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    llm = FlakyLLM([api_error(RateLimitError, 429, retry_after=7)])
    sleep = recording_sleep()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
        sleep=sleep,
        rand=constant_rand(1.0),
    )

    assert sleep.delays == [7.0]
    assert score_row(conn, "v1") is not None


def test_server_error_is_retried_and_scores(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    llm = FlakyLLM([api_error(InternalServerError, 503)])
    sleep = recording_sleep()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
        sleep=sleep,
        rand=constant_rand(0.5),
    )

    assert len(llm.completions.calls) == 2
    assert score_row(conn, "v1") is not None


def test_llm_bad_request_is_never_retried(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    llm = FlakyLLM([api_error(BadRequestError, 400)])

    with pytest.raises(BadRequestError):
        run_scoring(
            conn,
            fetcher(Transcript(text="body", language_code="en")),
            llm,
            model="m",
            now=NOW,
        )

    assert len(llm.completions.calls) == 1


def test_llm_permission_denied_is_never_retried(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    llm = FlakyLLM([api_error(PermissionDeniedError, 403)])

    with pytest.raises(PermissionDeniedError):
        run_scoring(
            conn,
            fetcher(Transcript(text="body", language_code="en")),
            llm,
            model="m",
            now=NOW,
        )

    assert len(llm.completions.calls) == 1


def test_three_exhausted_rate_limits_stop_batch_and_defer_remainder(conn):
    add_channel(conn, "UC1")
    for index in range(5):
        add_pending_video(conn, "UC1", f"v{index}")
    llm = FlakyLLM([api_error(RateLimitError, 429)] * 100)
    sleep = recording_sleep()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
        sleep=sleep,
        rand=constant_rand(0.0),
    )

    assert len(llm.completions.calls) == LLM_MAX_ATTEMPTS * MAX_CONSECUTIVE_RATE_LIMITS
    for index in range(5):
        assert transcript_row(conn, f"v{index}") == ("pending", 0)
        assert score_row(conn, f"v{index}") is None


def test_rate_limit_streak_resets_after_a_success(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", published_at="2026-07-05T00:00:00+00:00")
    add_pending_video(conn, "UC1", "v2", published_at="2026-07-04T00:00:00+00:00")
    add_pending_video(conn, "UC1", "v3", published_at="2026-07-03T00:00:00+00:00")
    exhaust = [api_error(RateLimitError, 429)] * LLM_MAX_ATTEMPTS
    llm = FlakyLLM([*exhaust, None, *exhaust])
    sleep = recording_sleep()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
        sleep=sleep,
        rand=constant_rand(0.0),
    )

    assert score_row(conn, "v1") is None
    assert score_row(conn, "v2") is not None
    assert score_row(conn, "v3") is None
    assert transcript_row(conn, "v3") == ("pending", 0)


def test_pacing_sleeps_ten_seconds_between_llm_calls(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", published_at="2026-07-05T00:00:00+00:00")
    add_pending_video(conn, "UC1", "v2", published_at="2026-07-04T00:00:00+00:00")
    sleep = recording_sleep()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        FakeLLM(),
        model="m",
        now=NOW,
        sleep=sleep,
    )

    assert sleep.delays == [PACING_SEC]


def test_backlog_drains_newest_first_by_publish_date(conn):
    add_channel(conn, "UC1")
    add_pending_video(
        conn, "UC1", "old", title="oldest",
        published_at="2026-07-01T00:00:00+00:00",
    )
    add_pending_video(
        conn, "UC1", "new", title="newest",
        published_at="2026-07-19T00:00:00+00:00",
    )
    add_pending_video(
        conn, "UC1", "mid", title="middle",
        published_at="2026-07-10T00:00:00+00:00",
    )
    llm = FakeLLM()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
        sleep=recording_sleep(),
    )

    assert scored_ids(llm) == ["Title: newest", "Title: middle", "Title: oldest"]


def test_daily_cap_stops_scoring_when_reached(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", published_at="2026-07-05T00:00:00+00:00")
    add_pending_video(conn, "UC1", "v2", published_at="2026-07-04T00:00:00+00:00")
    set_day_count(conn, "2026-07-19", DAILY_CAP - 1)
    llm = FakeLLM()

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now="2026-07-20T06:00:00+00:00",
        sleep=recording_sleep(),
    )

    assert len(llm.completions.calls) == 1
    assert score_row(conn, "v1") is not None
    assert score_row(conn, "v2") is None
    assert transcript_row(conn, "v2") == ("pending", 0)


def test_daily_count_persisted_and_logged(conn, caplog):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    llm = FakeLLM()

    with caplog.at_level(logging.INFO, logger="winnow.scoring"):
        run_scoring(
            conn,
            fetcher(Transcript(text="body", language_code="en")),
            llm,
            model="m",
            now="2026-07-20T06:00:00+00:00",
        )

    assert day_count(conn, "2026-07-20T06:00:00+00:00") == 1
    assert any("day count 1" in record.message for record in caplog.records)


def test_cap_resets_across_pacific_midnight_with_injected_clock(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "before")
    set_day_count(conn, "2026-07-19", DAILY_CAP)
    before_midnight = "2026-07-20T06:59:00+00:00"
    after_midnight = "2026-07-20T07:01:00+00:00"

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        FakeLLM(),
        model="m",
        now=before_midnight,
    )

    assert score_row(conn, "before") is None
    assert day_count(conn, before_midnight) == DAILY_CAP

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        FakeLLM(),
        model="m",
        now=after_midnight,
    )

    assert score_row(conn, "before") is not None
    assert day_count(conn, after_midnight) == 1


def test_retry_attempts_count_toward_daily_cap(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    set_day_count(conn, pacific_day(NOW), DAILY_CAP - 2)
    llm = FlakyLLM([api_error(RateLimitError, 429)] * LLM_MAX_ATTEMPTS)

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
        sleep=recording_sleep(),
        rand=constant_rand(0.0),
    )

    assert len(llm.completions.calls) == 2
    assert day_count(conn, NOW) == DAILY_CAP
    assert score_row(conn, "v1") is None
    assert transcript_row(conn, "v1") == ("pending", 0)


def test_exhausted_retries_count_toward_daily_cap(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    llm = FlakyLLM([api_error(InternalServerError, 503)] * LLM_MAX_ATTEMPTS)

    run_scoring(
        conn,
        fetcher(Transcript(text="body", language_code="en")),
        llm,
        model="m",
        now=NOW,
        sleep=recording_sleep(),
        rand=constant_rand(0.0),
    )

    assert day_count(conn, NOW) == LLM_MAX_ATTEMPTS
    assert score_row(conn, "v1") is None


def test_day_count_survives_non_retryable_error(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1")
    llm = FlakyLLM([api_error(BadRequestError, 400)])

    with pytest.raises(BadRequestError):
        run_scoring(
            conn,
            fetcher(Transcript(text="body", language_code="en")),
            llm,
            model="m",
            now=NOW,
        )

    conn.rollback()
    assert day_count(conn, NOW) == 1


def test_scores_commit_before_later_non_retryable_error(conn):
    add_channel(conn, "UC1")
    add_pending_video(conn, "UC1", "v1", published_at="2026-07-05T00:00:00+00:00")
    add_pending_video(conn, "UC1", "v2", published_at="2026-07-04T00:00:00+00:00")
    llm = FlakyLLM([None, api_error(BadRequestError, 400)])

    with pytest.raises(BadRequestError):
        run_scoring(
            conn,
            fetcher(Transcript(text="body", language_code="en")),
            llm,
            model="m",
            now=NOW,
            sleep=recording_sleep(),
        )

    conn.rollback()
    assert score_row(conn, "v1") is not None
    assert day_count(conn, NOW) == 2
