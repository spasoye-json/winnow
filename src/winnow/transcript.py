import re
from dataclasses import dataclass
from enum import Enum

import requests
from youtube_transcript_api import (
    AgeRestricted,
    InvalidVideoId,
    NoTranscriptFound,
    NotTranslatable,
    RequestBlocked,
    TranscriptsDisabled,
    TranslationLanguageNotAvailable,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)


@dataclass
class Snippet:
    start: float
    text: str


@dataclass
class Transcript:
    text: str
    language_code: str
    snippets: tuple = ()


class FailureClass(Enum):
    PERMANENT = "permanent"
    IP_BLOCK = "ip_block"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


PERMANENT_ERRORS = (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    VideoUnplayable,
    AgeRestricted,
    InvalidVideoId,
    NotTranslatable,
    TranslationLanguageNotAvailable,
)


def fetch_transcript(video_id, api=None):
    api = api or YouTubeTranscriptApi()
    languages = [track.language_code for track in api.list(video_id)]
    fetched = api.fetch(video_id, languages=languages)
    snippets = tuple(
        Snippet(start=snippet.start, text=snippet.text)
        for snippet in fetched.snippets
    )
    text = " ".join(snippet.text for snippet in snippets)
    return Transcript(text=text, language_code=fetched.language_code, snippets=snippets)


def classify_transcript_error(exc):
    if isinstance(exc, RequestBlocked):
        return FailureClass.IP_BLOCK
    if isinstance(exc, PERMANENT_ERRORS):
        return FailureClass.PERMANENT
    if isinstance(exc, YouTubeRequestFailed):
        if _is_transient_http(exc):
            return FailureClass.TRANSIENT
        return FailureClass.UNKNOWN
    if isinstance(exc, requests.exceptions.RequestException):
        return FailureClass.TRANSIENT
    return FailureClass.UNKNOWN


def _is_transient_http(exc):
    match = re.search(r"\b(\d{3})\b", getattr(exc, "reason", "") or "")
    if not match:
        return False
    code = int(match.group(1))
    return code == 429 or 500 <= code <= 599
