import json
from types import SimpleNamespace

import pytest

from winnow.db import connect, init_db
from winnow.scoring import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    OMISSION_MARKER,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_client,
    model_name,
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
