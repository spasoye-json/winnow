from dataclasses import dataclass

from youtube_transcript_api import YouTubeTranscriptApi


@dataclass
class Transcript:
    text: str
    language_code: str


def fetch_transcript(video_id, api=None):
    api = api or YouTubeTranscriptApi()
    languages = [track.language_code for track in api.list(video_id)]
    fetched = api.fetch(video_id, languages=languages)
    text = " ".join(snippet.text for snippet in fetched.snippets)
    return Transcript(text=text, language_code=fetched.language_code)
