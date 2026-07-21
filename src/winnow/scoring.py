import json
import logging
import os
import random
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from winnow.transcript import FailureClass, classify_transcript_error

logger = logging.getLogger("winnow.scoring")

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL = "gemini-3.1-flash-lite"

PROMPT_VERSION = 2

FULL_CONFIDENCE = 1.0
METADATA_ONLY_CONFIDENCE = 0.5

AI_VOICE_FLAG = "ai_voice"
LOW_TRANSCRIPT_FLAG = "low_transcript"
LOW_TRANSCRIPT_RATIO = 0.2
EXPECTED_WORDS_PER_MINUTE = 150

MAX_TRANSIENT_ATTEMPTS = 3
INITIAL_BACKOFF_SEC = 2
MAX_BACKOFF_SEC = 60
MAX_UNKNOWN_ATTEMPTS = 2

PACING_SEC = 10
DAILY_CAP = 200
PACIFIC = ZoneInfo("America/Los_Angeles")

LLM_INITIAL_BACKOFF_SEC = 2
LLM_BACKOFF_FACTOR = 2
LLM_MAX_BACKOFF_SEC = 60
LLM_MAX_ATTEMPTS = 5
MAX_CONSECUTIVE_RATE_LIMITS = 3
RETRYABLE_STATUS = (408, 429)

SCORING_DAY_KEY = "scoring_day"
SCORING_COUNT_KEY = "scoring_count"

UPSERT_SETTING = (
    "INSERT INTO settings (key, value) VALUES (?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
)

SAMPLE_TOKEN_TRIGGER = 15000
SAMPLE_WORD_TRIGGER = 10000
HEAD_TOKENS = 6000
MIDDLE_TOKENS = 5000
TAIL_TOKENS = 4000
OMISSION_MARKER = "[... transcript omitted ...]"

SYSTEM_PROMPT = f"""You are a strict quality curator for YouTube videos. You judge a \
video only from its transcript and the metadata provided, scoring content quality \
rather than popularity. Ignore view count entirely as a quality signal.

The transcript may be in any language. Always write the summary and rationale in \
English regardless of the transcript's language.

A long transcript is sampled rather than sent whole: you receive its head, then its \
middle, then its tail, and each omitted span between them is replaced by a line \
reading "{OMISSION_MARKER}". Treat every such marker as a gap where transcript was \
left out, not as content, and judge the video from the sampled spans that remain.

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
- summary: a neutral 2 to 3 sentence summary of what the video covers, in English.
- rationale: one paragraph explaining the scores, in English.

Your response must match the required schema exactly, with no prose outside it."""

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "video_score",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "scores": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "info_density": {"type": "integer"},
                        "originality": {"type": "integer"},
                        "clickbait_gap": {"type": "integer"},
                        "padding": {"type": "integer"},
                        "depth": {"type": "integer"},
                        "production": {"type": "integer"},
                    },
                    "required": [
                        "info_density",
                        "originality",
                        "clickbait_gap",
                        "padding",
                        "depth",
                        "production",
                    ],
                },
                "overall": {"type": "number"},
                "hard_flags": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": [
                "scores",
                "overall",
                "hard_flags",
                "summary",
                "rationale",
            ],
        },
    },
}

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
    "SELECT v.id, v.yt_video_id, v.title, v.duration_sec, v.view_count, "
    "v.transcript_attempts, COALESCE(c.exempt_low_transcript, 0) "
    "FROM videos v LEFT JOIN channels c ON c.id = v.channel_id "
    "WHERE v.transcript_status = 'pending' "
    "ORDER BY v.published_at DESC, v.id ASC"
)


def model_name():
    return os.environ.get("SCORING_MODEL", DEFAULT_MODEL)


def build_client():
    from openai import OpenAI

    return OpenAI(
        base_url=os.environ.get("SCORING_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.environ.get("SCORING_API_KEY"),
    )


class _ScoringDeferred(Exception):
    def __init__(self, rate_limited):
        self.rate_limited = rate_limited


def pacific_day(now_iso):
    return datetime.fromisoformat(now_iso).astimezone(PACIFIC).date().isoformat()


def day_count(conn, now_iso):
    return _load_day_count(conn, pacific_day(now_iso))


def run_scoring(conn, fetch_transcript, llm, model, now=None, sleep=time.sleep,
                rand=random.random):
    now = now or datetime.now(UTC).isoformat()
    day = pacific_day(now)
    count = _load_day_count(conn, day)
    pending = conn.execute(SELECT_PENDING).fetchall()
    consecutive_rate_limits = 0
    made_call = False

    def record_request():
        nonlocal count
        if count >= DAILY_CAP:
            return False
        count += 1
        _persist_day_count(conn, day, count)
        conn.commit()
        logger.info(
            "llm request on Pacific day %s, day count %d/%d", day, count, DAILY_CAP
        )
        return True

    for (video_id, yt_video_id, title, duration_sec, view_count, attempts,
         exempt) in pending:
        if count >= DAILY_CAP:
            break
        transcript, failure = _fetch(fetch_transcript, yt_video_id, sleep)
        if failure is FailureClass.IP_BLOCK:
            break
        plan = _transcript_plan(conn, failure, transcript, video_id, attempts)
        if plan is None:
            continue
        status_sql, params, confidence, score_transcript = plan
        if made_call:
            sleep(PACING_SEC)
        made_call = True
        try:
            result = _score_with_backoff(
                llm, model, title, duration_sec, view_count, score_transcript,
                sleep, rand, record_request,
            )
        except _ScoringDeferred as deferred:
            if deferred.rate_limited:
                consecutive_rate_limits += 1
                if consecutive_rate_limits >= MAX_CONSECUTIVE_RATE_LIMITS:
                    break
            continue
        consecutive_rate_limits = 0
        conn.execute(status_sql, params)
        hard_flags = _hard_flags(
            result["hard_flags"], score_transcript, duration_sec, exempt
        )
        _store_score(conn, video_id, result, hard_flags, model, confidence, now)
        conn.commit()
    conn.commit()


def _transcript_plan(conn, failure, transcript, video_id, attempts):
    if failure is None:
        return (
            STORE_TRANSCRIPT, (transcript.language_code, video_id),
            FULL_CONFIDENCE, transcript,
        )
    if failure is FailureClass.PERMANENT:
        return MARK_NO_TRANSCRIPT, (video_id,), METADATA_ONLY_CONFIDENCE, None
    if failure is FailureClass.TRANSIENT:
        return None
    attempts += 1
    if attempts >= MAX_UNKNOWN_ATTEMPTS:
        return (
            MARK_FETCH_FAILED, (attempts, video_id),
            METADATA_ONLY_CONFIDENCE, None,
        )
    conn.execute(RECORD_ATTEMPT, (attempts, video_id))
    return None


def _load_day_count(conn, day):
    stored_day = _setting(conn, SCORING_DAY_KEY)
    if stored_day != day:
        return 0
    stored_count = _setting(conn, SCORING_COUNT_KEY)
    return int(stored_count) if stored_count is not None else 0


def _persist_day_count(conn, day, count):
    conn.execute(UPSERT_SETTING, (SCORING_DAY_KEY, day))
    conn.execute(UPSERT_SETTING, (SCORING_COUNT_KEY, str(count)))


def _setting(conn, key):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def _score_with_backoff(llm, model, title, duration_sec, view_count, transcript,
                        sleep, rand, record_request):
    delay = LLM_INITIAL_BACKOFF_SEC
    rate_limited = False
    for attempt in range(LLM_MAX_ATTEMPTS):
        if not record_request():
            break
        try:
            return _score(llm, model, title, duration_sec, view_count, transcript)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if not _llm_retryable(status):
                raise
            rate_limited = status == 429
            if attempt < LLM_MAX_ATTEMPTS - 1:
                sleep(_backoff_delay(exc, delay, rand))
                delay = min(delay * LLM_BACKOFF_FACTOR, LLM_MAX_BACKOFF_SEC)
    raise _ScoringDeferred(rate_limited=rate_limited)


def _llm_retryable(status):
    if status is None:
        return False
    return status in RETRYABLE_STATUS or 500 <= status <= 599


def _backoff_delay(exc, delay, rand):
    retry_after = _retry_after(exc)
    if retry_after is not None:
        return retry_after
    return rand() * delay


def _retry_after(exc):
    response = getattr(exc, "response", None)
    if response is None:
        return None
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


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


def _hard_flags(rubric_flags, transcript, duration_sec, exempt):
    flags = [AI_VOICE_FLAG] if AI_VOICE_FLAG in rubric_flags else []
    if not exempt and _is_low_transcript(transcript, duration_sec):
        flags.append(LOW_TRANSCRIPT_FLAG)
    return flags


def _is_low_transcript(transcript, duration_sec):
    if transcript is None or not duration_sec:
        return False
    expected_words = duration_sec / 60 * EXPECTED_WORDS_PER_MINUTE
    return len(transcript.text.split()) < LOW_TRANSCRIPT_RATIO * expected_words


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
        response_format=RESPONSE_SCHEMA,
    )
    return json.loads(response.choices[0].message.content)


def _user_content(title, duration_sec, view_count, transcript):
    return (
        f"Title: {title}\n"
        f"Duration (seconds): {duration_sec}\n"
        f"View count (ignore as a quality signal): {view_count}\n\n"
        f"{_transcript_body(transcript, duration_sec)}"
    )


def _transcript_body(transcript, duration_sec):
    if transcript is None:
        return "Transcript:\n(unavailable; metadata only)"
    if not transcript.snippets or not _should_sample(transcript.text):
        return f"Transcript:\n{transcript.text}"
    return _sampled_body(transcript, duration_sec)


def _estimate_tokens(text):
    return len(text) // 4


def _should_sample(text):
    return (
        _estimate_tokens(text) > SAMPLE_TOKEN_TRIGGER
        or len(text.split()) > SAMPLE_WORD_TRIGGER
    )


def _sampled_body(transcript, duration_sec):
    snippets = transcript.snippets
    stats = _stats_line(len(transcript.text.split()), duration_sec)
    body = f"\n{OMISSION_MARKER}\n".join(
        _join(snippets, segment)
        for segment in _segments(snippets, duration_sec)
    )
    return f"{stats}\n\nTranscript:\n{body}"


def _segments(snippets, duration_sec):
    count = len(snippets)
    head = range(0, _fit(snippets, range(count), HEAD_TOKENS))
    tail = range(count - _fit(snippets, reversed(range(count)), TAIL_TOKENS), count)
    return _merge([head, _middle(snippets, duration_sec), tail])


def _merge(ranges):
    ordered = sorted(ranges, key=lambda r: r.start)
    merged = [ordered[0]]
    for r in ordered[1:]:
        last = merged[-1]
        if r.start <= last.stop:
            merged[-1] = range(last.start, max(last.stop, r.stop))
        else:
            merged.append(r)
    return merged


def _stats_line(word_count, duration_sec):
    wpm = word_count / (duration_sec / 60) if duration_sec else 0.0
    return (
        f"Transcript stats: {word_count} words, "
        f"{duration_sec}s duration, {wpm:.1f} words per minute"
    )


def _fit(snippets, indices, budget):
    used = 0
    count = 0
    for i in indices:
        cost = _estimate_tokens(snippets[i].text)
        if count and used + cost > budget:
            break
        count += 1
        used += cost
    return count


def _middle(snippets, duration_sec):
    center = _center_index(snippets, duration_sec)
    used = _estimate_tokens(snippets[center].text)
    lo = hi = center
    while True:
        prev_cost = _estimate_tokens(snippets[lo - 1].text) if lo > 0 else None
        next_cost = (
            _estimate_tokens(snippets[hi + 1].text)
            if hi + 1 < len(snippets)
            else None
        )
        prev_ok = prev_cost is not None and used + prev_cost <= MIDDLE_TOKENS
        next_ok = next_cost is not None and used + next_cost <= MIDDLE_TOKENS
        if not prev_ok and not next_ok:
            break
        if prev_ok and (not next_ok or (center - lo) <= (hi - center)):
            lo -= 1
            used += prev_cost
        else:
            hi += 1
            used += next_cost
    return range(lo, hi + 1)


def _center_index(snippets, duration_sec):
    target = duration_sec / 2
    best = 0
    best_dist = None
    for i, snippet in enumerate(snippets):
        dist = abs(snippet.start - target)
        if best_dist is None or dist < best_dist:
            best = i
            best_dist = dist
    return best


def _join(snippets, indices):
    return " ".join(snippets[i].text for i in indices)


def _store_score(conn, video_id, result, hard_flags, model, confidence, now):
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
            json.dumps(hard_flags),
            result["summary"],
            result["rationale"],
            confidence,
            model,
            PROMPT_VERSION,
            now,
        ),
    )
