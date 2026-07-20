import json
import os
import time
from datetime import UTC, datetime

from winnow.transcript import FailureClass, classify_transcript_error

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-3.1-flash-lite"

PROMPT_VERSION = 1

FULL_CONFIDENCE = 1.0
METADATA_ONLY_CONFIDENCE = 0.5

MAX_TRANSIENT_ATTEMPTS = 3
INITIAL_BACKOFF_SEC = 2
MAX_BACKOFF_SEC = 60
MAX_UNKNOWN_ATTEMPTS = 2

SYSTEM_PROMPT = """You are a strict quality curator for YouTube videos. You judge a \
video only from its transcript and the metadata provided, scoring content quality \
rather than popularity. Ignore view count entirely as a quality signal.

Score each of these six dimensions from 0 to 10 (10 is best):

- info_density: substance per minute. High (8-10): a clear thesis with a dense \
payload of specifics, few wasted words. Low (0-3): filler, vague generalities, \
little actual information across a long runtime.
- originality: original research, analysis, or firsthand experience. High (8-10): \
novel argument, own experiments, primary reporting. Low (0-3): recycled takes, \
reaction content, list compilations of others' work.
- clickbait_gap: does the content deliver what the title promises. High (8-10): the \
title's promise is fully answered in the body. Low (0-3): the title poses a question \
or hook that the transcript never actually resolves.
- padding: how little of the runtime is stalling (higher is less padded). High \
(8-10): almost no intro fluff, sponsor stalling, or repetition. Low (0-3): long \
"before we start" preambles, repeated points, drawn-out sponsor reads.
- depth: rigor and nuance. High (8-10): sources cited, counterpoints acknowledged, \
careful reasoning. Low (0-3): surface-level hot takes with no support.
- production: integrity of the production (higher is more human and genuine). High \
(8-10): a genuine human voice and script. Low (0-3): signs of AI-generated narration, \
mass-produced content-farm templates.

Also produce:
- overall: a single 0 to 10 quality number reflecting the dimensions together.
- hard_flags: a list of strings, using "ai_voice" when the narration looks like an \
AI content farm and "low_transcript" when the transcript is far shorter than the \
runtime implies. Use an empty list when neither applies.
- summary: a neutral 2 to 3 sentence summary of what the video covers.
- rationale: one paragraph explaining the scores.

Return only valid JSON in exactly this shape, with no prose outside it:
{"scores": {"info_density": 0, "originality": 0, "clickbait_gap": 0, "padding": 0, \
"depth": 0, "production": 0}, "overall": 0.0, "hard_flags": [], "summary": "", \
"rationale": ""}"""

INSERT_SCORE = """
INSERT INTO scores
    (video_id, overall, info_density, originality, clickbait_gap, padding,
     depth, production, hard_flags, summary, rationale, confidence, model,
     prompt_version, scored_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

STORE_TRANSCRIPT = (
    "UPDATE videos SET transcript_status = 'ok', caption_language = ? WHERE id = ?"
)

MARK_NO_TRANSCRIPT = (
    "UPDATE videos SET transcript_status = 'no_transcript' WHERE id = ?"
)

MARK_FETCH_FAILED = (
    "UPDATE videos SET transcript_status = 'fetch_failed', "
    "transcript_attempts = ? WHERE id = ?"
)

RECORD_ATTEMPT = "UPDATE videos SET transcript_attempts = ? WHERE id = ?"

SELECT_PENDING = (
    "SELECT id, yt_video_id, title, duration_sec, view_count, transcript_attempts "
    "FROM videos WHERE transcript_status = 'pending'"
)


def model_name():
    return os.environ.get("SCORING_MODEL", DEFAULT_MODEL)


def build_client():
    from openai import OpenAI

    return OpenAI(
        base_url=os.environ.get("SCORING_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.environ.get("SCORING_API_KEY"),
    )


def run_scoring(conn, fetch_transcript, llm, model, now=None, sleep=time.sleep):
    now = now or datetime.now(UTC).isoformat()
    pending = conn.execute(SELECT_PENDING).fetchall()
    for video_id, yt_video_id, title, duration_sec, view_count, attempts in pending:
        transcript, failure = _fetch(fetch_transcript, yt_video_id, sleep)
        if failure is None:
            conn.execute(STORE_TRANSCRIPT, (transcript.language_code, video_id))
            _score_and_store(
                conn, video_id, llm, model, title, duration_sec, view_count,
                transcript.text, FULL_CONFIDENCE, now,
            )
        elif failure is FailureClass.IP_BLOCK:
            break
        elif failure is FailureClass.PERMANENT:
            conn.execute(MARK_NO_TRANSCRIPT, (video_id,))
            _score_and_store(
                conn, video_id, llm, model, title, duration_sec, view_count,
                None, METADATA_ONLY_CONFIDENCE, now,
            )
        elif failure is FailureClass.TRANSIENT:
            continue
        else:
            attempts += 1
            if attempts >= MAX_UNKNOWN_ATTEMPTS:
                conn.execute(MARK_FETCH_FAILED, (attempts, video_id))
                _score_and_store(
                    conn, video_id, llm, model, title, duration_sec, view_count,
                    None, METADATA_ONLY_CONFIDENCE, now,
                )
            else:
                conn.execute(RECORD_ATTEMPT, (attempts, video_id))
    conn.commit()


def _fetch(fetch_transcript, yt_video_id, sleep):
    delay = INITIAL_BACKOFF_SEC
    for attempt in range(MAX_TRANSIENT_ATTEMPTS):
        try:
            return fetch_transcript(yt_video_id), None
        except Exception as exc:
            failure = classify_transcript_error(exc)
            if failure is not FailureClass.TRANSIENT:
                return None, failure
            if attempt < MAX_TRANSIENT_ATTEMPTS - 1:
                sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SEC)
    return None, FailureClass.TRANSIENT


def _score_and_store(
    conn, video_id, llm, model, title, duration_sec, view_count,
    transcript, confidence, now,
):
    result = _score(llm, model, title, duration_sec, view_count, transcript)
    _store_score(conn, video_id, result, model, confidence, now)


def _score(llm, model, title, duration_sec, view_count, transcript):
    response = llm.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _user_content(title, duration_sec, view_count, transcript),
            },
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


def _user_content(title, duration_sec, view_count, transcript):
    body = transcript if transcript is not None else "(unavailable; metadata only)"
    return (
        f"Title: {title}\n"
        f"Duration (seconds): {duration_sec}\n"
        f"View count (ignore as a quality signal): {view_count}\n\n"
        f"Transcript:\n{body}"
    )


def _store_score(conn, video_id, result, model, confidence, now):
    scores = result["scores"]
    conn.execute(
        INSERT_SCORE,
        (
            video_id,
            result["overall"],
            scores["info_density"],
            scores["originality"],
            scores["clickbait_gap"],
            scores["padding"],
            scores["depth"],
            scores["production"],
            json.dumps(result["hard_flags"]),
            result["summary"],
            result["rationale"],
            confidence,
            model,
            PROMPT_VERSION,
            now,
        ),
    )
