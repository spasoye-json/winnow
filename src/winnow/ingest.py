import re
from datetime import UTC, datetime, timedelta

from winnow.sync import sync_subscriptions
from winnow.youtube import fetch_videos, first_uploads_page, uploads_playlist_id

BACKFILL_LIMIT = 10
BACKFILL_MAX_AGE = timedelta(days=30)
LAST_INGEST_KEY = "last_ingest_at"

THUMBNAIL_PREFERENCE = ("maxres", "standard", "high", "medium", "default")
DURATION = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

INSERT_VIDEO = """
INSERT INTO videos
    (yt_video_id, channel_id, title, description, duration_sec,
     published_at, view_count, thumbnail_url, ingested_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(yt_video_id) DO NOTHING
"""

RECORD_INGEST = """
INSERT INTO settings (key, value) VALUES (?, ?)
ON CONFLICT(key) DO UPDATE SET value = excluded.value
"""


def run_ingest(conn, client, now=None):
    now = now or datetime.now(UTC).isoformat()
    sync_subscriptions(conn, client, now=now)
    channels = conn.execute(
        "SELECT id, yt_channel_id FROM channels WHERE active = 1 AND excluded = 0"
    ).fetchall()
    for channel_id, yt_channel_id in channels:
        _ingest_channel(conn, client, channel_id, yt_channel_id, now)
    conn.execute(RECORD_INGEST, (LAST_INGEST_KEY, now))
    conn.commit()


def _ingest_channel(conn, client, channel_id, yt_channel_id, now):
    items = first_uploads_page(client, uploads_playlist_id(yt_channel_id))
    video_ids = _select_video_ids(conn, channel_id, items, now)
    for video in fetch_videos(client, video_ids):
        _store_video(conn, channel_id, video, now)


def _select_video_ids(conn, channel_id, items, now):
    entries = [(_video_id(item), _published_at(item)) for item in items]
    if _has_stored_videos(conn, channel_id):
        stored = _stored_video_ids(conn)
        return [video_id for video_id, _ in entries if video_id not in stored]
    cutoff = datetime.fromisoformat(now) - BACKFILL_MAX_AGE
    return [
        video_id
        for video_id, published in entries[:BACKFILL_LIMIT]
        if datetime.fromisoformat(published) >= cutoff
    ]


def _has_stored_videos(conn, channel_id):
    return (
        conn.execute(
            "SELECT 1 FROM videos WHERE channel_id = ? LIMIT 1", (channel_id,)
        ).fetchone()
        is not None
    )


def _stored_video_ids(conn):
    return {row[0] for row in conn.execute("SELECT yt_video_id FROM videos")}


def _store_video(conn, channel_id, video, now):
    snippet = video.get("snippet", {})
    content = video.get("contentDetails", {})
    statistics = video.get("statistics", {})
    conn.execute(
        INSERT_VIDEO,
        (
            video["id"],
            channel_id,
            snippet.get("title"),
            snippet.get("description"),
            _duration_seconds(content.get("duration")),
            snippet.get("publishedAt"),
            _view_count(statistics.get("viewCount")),
            _thumbnail_url(snippet.get("thumbnails", {})),
            now,
        ),
    )


def _video_id(item):
    content = item.get("contentDetails", {})
    if content.get("videoId"):
        return content["videoId"]
    return item.get("snippet", {}).get("resourceId", {}).get("videoId")


def _published_at(item):
    content = item.get("contentDetails", {})
    return content.get("videoPublishedAt") or item.get("snippet", {}).get("publishedAt")


def _duration_seconds(value):
    if not value:
        return None
    match = DURATION.fullmatch(value)
    if not match:
        return None
    days, hours, minutes, seconds = (
        int(part) if part else 0 for part in match.groups()
    )
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _view_count(value):
    return int(value) if value is not None else None


def _thumbnail_url(thumbnails):
    for size in THUMBNAIL_PREFERENCE:
        if size in thumbnails:
            return thumbnails[size].get("url")
    return None
