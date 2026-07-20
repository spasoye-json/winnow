from datetime import UTC, datetime

from winnow.youtube import iter_subscriptions

UPSERT_SUBSCRIPTION = """
INSERT INTO channels (yt_channel_id, name, source, active, added_at, last_synced_at)
VALUES (?, ?, 'subscription', 1, ?, ?)
ON CONFLICT(yt_channel_id) DO UPDATE SET
    name = excluded.name,
    active = 1,
    last_synced_at = excluded.last_synced_at
WHERE channels.source = 'subscription'
"""

DEACTIVATE_MISSING = """
UPDATE channels SET active = 0, last_synced_at = ?
WHERE source = 'subscription' AND active = 1 AND yt_channel_id NOT IN ({placeholders})
"""


def sync_subscriptions(conn, client, now=None):
    now = now or datetime.now(UTC).isoformat()
    seen = []
    for item in iter_subscriptions(client):
        snippet = item["snippet"]
        channel_id = snippet["resourceId"]["channelId"]
        conn.execute(UPSERT_SUBSCRIPTION, (channel_id, snippet["title"], now, now))
        seen.append(channel_id)

    placeholders = ",".join("?" for _ in seen)
    conn.execute(
        DEACTIVATE_MISSING.format(placeholders=placeholders),
        (now, *seen),
    )
    conn.commit()
