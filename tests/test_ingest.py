import pytest

from winnow.db import connect, init_db
from winnow.ingest import LAST_INGEST_KEY, run_ingest

NOW = "2026-07-20T00:00:00+00:00"
RECENT = "2026-07-19T00:00:00+00:00"
OLD = "2026-05-01T00:00:00+00:00"


def subscription(channel_id, title):
    return {"snippet": {"title": title, "resourceId": {"channelId": channel_id}}}


def upload(video_id, published_at):
    return {
        "snippet": {"resourceId": {"videoId": video_id}},
        "contentDetails": {"videoId": video_id, "videoPublishedAt": published_at},
    }


def video_detail(
    video_id,
    *,
    title="A title",
    description="A description",
    duration="PT10M",
    published_at=RECENT,
    view_count="100",
    thumbnail="https://i.ytimg.com/vi/x/hq.jpg",
):
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": description,
            "publishedAt": published_at,
            "thumbnails": {"high": {"url": thumbnail}},
        },
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": view_count},
    }


class FakeRequest:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class FakeSubscriptions:
    def __init__(self, items):
        self.items = items

    def list(self, **kwargs):
        return FakeRequest({"items": self.items})

    def list_next(self, request, response):
        return None


class FakePlaylistItems:
    def __init__(self, pages_by_playlist):
        self.pages_by_playlist = pages_by_playlist

    def list(self, *, playlistId, **kwargs):
        return FakeRequest({"items": self.pages_by_playlist.get(playlistId, [])})

    def list_next(self, request, response):
        return None


class FakeVideos:
    def __init__(self, details):
        self.details = details

    def list(self, *, id, **kwargs):
        ids = id.split(",")
        items = [self.details[v] for v in ids if v in self.details]
        return FakeRequest({"items": items})


class FakeYouTube:
    def __init__(self, subscriptions=(), playlists=None, videos=None):
        self._subscriptions = FakeSubscriptions(list(subscriptions))
        self._playlists = FakePlaylistItems(playlists or {})
        self._videos = FakeVideos(videos or {})

    def subscriptions(self):
        return self._subscriptions

    def playlistItems(self):
        return self._playlists

    def videos(self):
        return self._videos


def uploads_id(channel_id):
    return "UU" + channel_id[2:]


def add_channel(conn, channel_id, *, source="manual", active=1, excluded=0):
    conn.execute(
        "INSERT INTO channels "
        "(yt_channel_id, name, source, active, excluded, added_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (channel_id, channel_id, source, active, excluded),
    )
    conn.commit()


def add_video(conn, channel_id, yt_video_id):
    (internal_id,) = conn.execute(
        "SELECT id FROM channels WHERE yt_channel_id = ?", (channel_id,)
    ).fetchone()
    conn.execute(
        "INSERT INTO videos (yt_video_id, channel_id) VALUES (?, ?)",
        (yt_video_id, internal_id),
    )
    conn.commit()


def stored_video_ids(conn, channel_id):
    rows = conn.execute(
        "SELECT v.yt_video_id FROM videos v "
        "JOIN channels c ON c.id = v.channel_id "
        "WHERE c.yt_channel_id = ? ORDER BY v.yt_video_id",
        (channel_id,),
    ).fetchall()
    return [row[0] for row in rows]


@pytest.fixture
def conn():
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def test_sync_runs_first_then_ingests_uploads(conn):
    client = FakeYouTube(
        subscriptions=[subscription("UC1", "Channel One")],
        playlists={uploads_id("UC1"): [upload("v1", RECENT)]},
        videos={"v1": video_detail("v1")},
    )

    run_ingest(conn, client, now=NOW)

    assert conn.execute(
        "SELECT 1 FROM channels WHERE yt_channel_id = 'UC1'"
    ).fetchone() is not None
    assert stored_video_ids(conn, "UC1") == ["v1"]


def test_backfill_excludes_videos_older_than_thirty_days(conn):
    add_channel(conn, "UC1")
    client = FakeYouTube(
        playlists={
            uploads_id("UC1"): [
                upload("recent1", RECENT),
                upload("recent2", "2026-07-01T00:00:00+00:00"),
                upload("old1", OLD),
            ]
        },
        videos={
            "recent1": video_detail("recent1"),
            "recent2": video_detail("recent2"),
            "old1": video_detail("old1"),
        },
    )

    run_ingest(conn, client, now=NOW)

    assert stored_video_ids(conn, "UC1") == ["recent1", "recent2"]


def test_backfill_caps_at_ten_newest(conn):
    add_channel(conn, "UC1")
    uploads = [upload(f"v{i:02d}", RECENT) for i in range(15)]
    details = {f"v{i:02d}": video_detail(f"v{i:02d}") for i in range(15)}
    client = FakeYouTube(playlists={uploads_id("UC1"): uploads}, videos=details)

    run_ingest(conn, client, now=NOW)

    assert len(stored_video_ids(conn, "UC1")) == 10
    assert stored_video_ids(conn, "UC1") == [f"v{i:02d}" for i in range(10)]


def test_known_channel_takes_all_new_uploads_deduped(conn):
    add_channel(conn, "UC1")
    add_video(conn, "UC1", "known")
    client = FakeYouTube(
        playlists={
            uploads_id("UC1"): [
                upload("new1", RECENT),
                upload("new2", RECENT),
                upload("known", RECENT),
            ]
        },
        videos={
            "new1": video_detail("new1"),
            "new2": video_detail("new2"),
            "known": video_detail("known"),
        },
    )

    run_ingest(conn, client, now=NOW)

    assert stored_video_ids(conn, "UC1") == ["known", "new1", "new2"]


def test_video_metadata_is_stored(conn):
    add_channel(conn, "UC1")
    client = FakeYouTube(
        playlists={uploads_id("UC1"): [upload("v1", RECENT)]},
        videos={
            "v1": video_detail(
                "v1",
                title="Deep dive",
                description="A thorough look",
                duration="PT1H2M3S",
                published_at=RECENT,
                view_count="4567",
                thumbnail="https://i.ytimg.com/vi/v1/hq.jpg",
            )
        },
    )

    run_ingest(conn, client, now=NOW)

    row = conn.execute(
        "SELECT title, description, duration_sec, published_at, view_count, "
        "thumbnail_url FROM videos WHERE yt_video_id = 'v1'"
    ).fetchone()
    assert row == (
        "Deep dive",
        "A thorough look",
        3723,
        RECENT,
        4567,
        "https://i.ytimg.com/vi/v1/hq.jpg",
    )


def test_excluded_and_inactive_channels_are_skipped(conn):
    add_channel(conn, "UCactive")
    add_channel(conn, "UCexcluded", excluded=1)
    add_channel(conn, "UCinactive", active=0)
    client = FakeYouTube(
        playlists={
            uploads_id("UCactive"): [upload("a1", RECENT)],
            uploads_id("UCexcluded"): [upload("e1", RECENT)],
            uploads_id("UCinactive"): [upload("i1", RECENT)],
        },
        videos={
            "a1": video_detail("a1"),
            "e1": video_detail("e1"),
            "i1": video_detail("i1"),
        },
    )

    run_ingest(conn, client, now=NOW)

    assert stored_video_ids(conn, "UCactive") == ["a1"]
    assert stored_video_ids(conn, "UCexcluded") == []
    assert stored_video_ids(conn, "UCinactive") == []


def test_last_successful_ingest_timestamp_is_recorded(conn):
    client = FakeYouTube()

    run_ingest(conn, client, now=NOW)

    (value,) = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (LAST_INGEST_KEY,)
    ).fetchone()
    assert value == NOW
