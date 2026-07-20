import pytest

from winnow.db import connect, init_db
from winnow.sync import sync_subscriptions

NOW = "2026-07-20T00:00:00+00:00"


def subscription(channel_id, title):
    return {"snippet": {"title": title, "resourceId": {"channelId": channel_id}}}


class FakeRequest:
    def __init__(self, page):
        self.page = page

    def execute(self):
        return self.page


class FakeSubscriptions:
    def __init__(self, pages):
        self.pages = pages

    def list(self, **kwargs):
        return FakeRequest(self.pages[0])

    def list_next(self, request, response):
        index = self.pages.index(request.page)
        if index + 1 < len(self.pages):
            return FakeRequest(self.pages[index + 1])
        return None


class FakeYouTube:
    def __init__(self, pages):
        self._subscriptions = FakeSubscriptions(pages)

    def subscriptions(self):
        return self._subscriptions


def client_returning(*channels):
    return FakeYouTube([{"items": list(channels)}])


def paginated_client(*pages):
    payload = [
        {"items": list(page), "nextPageToken": f"token-{i}"}
        for i, page in enumerate(pages)
    ]
    payload[-1].pop("nextPageToken")
    return FakeYouTube(payload)


def channel_rows(conn):
    rows = conn.execute(
        "SELECT yt_channel_id, name, source, active, last_synced_at "
        "FROM channels ORDER BY yt_channel_id"
    ).fetchall()
    return rows


@pytest.fixture
def conn():
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


def test_sync_upserts_subscriptions_as_channels(conn):
    client = client_returning(
        subscription("UC1", "Channel One"),
        subscription("UC2", "Channel Two"),
    )

    sync_subscriptions(conn, client, now=NOW)

    assert channel_rows(conn) == [
        ("UC1", "Channel One", "subscription", 1, NOW),
        ("UC2", "Channel Two", "subscription", 1, NOW),
    ]


def test_sync_pages_through_all_results(conn):
    client = paginated_client(
        [subscription("UC1", "Channel One")],
        [subscription("UC2", "Channel Two")],
        [subscription("UC3", "Channel Three")],
    )

    sync_subscriptions(conn, client, now=NOW)

    ids = [row[0] for row in channel_rows(conn)]
    assert ids == ["UC1", "UC2", "UC3"]


def test_sync_updates_existing_subscription_name(conn):
    sync_subscriptions(conn, client_returning(subscription("UC1", "Old Name")), now=NOW)

    sync_subscriptions(
        conn, client_returning(subscription("UC1", "New Name")), now=NOW
    )

    assert channel_rows(conn) == [("UC1", "New Name", "subscription", 1, NOW)]


def test_missing_subscription_is_deactivated_not_deleted(conn):
    sync_subscriptions(
        conn,
        client_returning(
            subscription("UC1", "Channel One"),
            subscription("UC2", "Channel Two"),
        ),
        now=NOW,
    )

    sync_subscriptions(
        conn, client_returning(subscription("UC1", "Channel One")), now=NOW
    )

    rows = channel_rows(conn)
    active = {row[0]: row[3] for row in rows}
    assert active == {"UC1": 1, "UC2": 0}


def test_resubscribing_reactivates_channel(conn):
    one = subscription("UC1", "Channel One")
    sync_subscriptions(conn, client_returning(one), now=NOW)
    sync_subscriptions(conn, client_returning(), now=NOW)
    assert channel_rows(conn)[0][3] == 0

    sync_subscriptions(conn, client_returning(one), now=NOW)
    assert channel_rows(conn)[0][3] == 1


def test_manual_channels_are_untouched(conn):
    conn.execute(
        "INSERT INTO channels (yt_channel_id, name, source, active, added_at) "
        "VALUES ('UCM', 'Manual', 'manual', 1, '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    sync_subscriptions(conn, client_returning(), now=NOW)

    rows = channel_rows(conn)
    assert rows == [("UCM", "Manual", "manual", 1, None)]


def test_manual_channel_matching_subscription_id_is_untouched(conn):
    conn.execute(
        "INSERT INTO channels (yt_channel_id, name, source, active, added_at) "
        "VALUES ('UC1', 'Manual Name', 'manual', 1, '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()

    sync_subscriptions(conn, client_returning(subscription("UC1", "Sub Name")), now=NOW)

    assert channel_rows(conn) == [("UC1", "Manual Name", "manual", 1, None)]


def test_rerun_with_no_changes_is_a_noop(conn):
    client = client_returning(
        subscription("UC1", "Channel One"),
        subscription("UC2", "Channel Two"),
    )
    sync_subscriptions(conn, client, now=NOW)
    before = channel_rows(conn)

    sync_subscriptions(
        conn,
        client_returning(
            subscription("UC1", "Channel One"),
            subscription("UC2", "Channel Two"),
        ),
        now=NOW,
    )

    assert channel_rows(conn) == before
