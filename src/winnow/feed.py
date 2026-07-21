import json
from datetime import UTC, datetime

DEFAULT_WEIGHTS = {
    "info_density": 0.25,
    "originality": 0.20,
    "clickbait_gap": 0.20,
    "padding": 0.15,
    "depth": 0.15,
    "production": 0.05,
}
DEFAULT_THRESHOLD = 6.0
FULL_CONFIDENCE = 1.0

DIMENSIONS = tuple(DEFAULT_WEIGHTS)

DIMENSION_LABELS = {
    "info_density": "Information density",
    "originality": "Originality",
    "clickbait_gap": "Clickbait gap",
    "padding": "Padding",
    "depth": "Depth",
    "production": "Production integrity",
}

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v="

UNSCORED_REASONS = {
    "pending": "awaiting scoring",
    "no_transcript": "no captions",
    "fetch_failed": "transcript fetch failed",
}

VERDICT_COLUMN = """
       (SELECT verdict FROM feedback WHERE video_id = v.id
        ORDER BY id DESC LIMIT 1) AS verdict"""

SELECT_VIDEOS = f"""
SELECT v.yt_video_id, v.title, v.thumbnail_url, v.transcript_status, c.name,
       s.info_density, s.originality, s.clickbait_gap, s.padding, s.depth,
       s.production, s.summary, s.hard_flags, s.confidence,{VERDICT_COLUMN}
FROM videos v
LEFT JOIN channels c ON c.id = v.channel_id
LEFT JOIN scores s ON s.id = (
    SELECT id FROM scores WHERE video_id = v.id
    ORDER BY scored_at DESC, id DESC LIMIT 1
)
"""

SELECT_VIDEOS_ORDER = "\nORDER BY v.published_at DESC, v.id DESC\n"

SELECT_CHANNELS = """
SELECT yt_channel_id, name FROM channels WHERE name IS NOT NULL ORDER BY name
"""

SELECT_VIDEO = f"""
SELECT v.yt_video_id, v.title, v.thumbnail_url, v.transcript_status, c.name,
       s.info_density, s.originality, s.clickbait_gap, s.padding, s.depth,
       s.production, s.summary, s.rationale, s.hard_flags, s.confidence,
       s.model, s.prompt_version,{VERDICT_COLUMN}
FROM videos v
LEFT JOIN channels c ON c.id = v.channel_id
LEFT JOIN scores s ON s.id = (
    SELECT id FROM scores WHERE video_id = v.id
    ORDER BY scored_at DESC, id DESC LIMIT 1
)
WHERE v.yt_video_id = ?
"""


def load_weights(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = 'weights'").fetchone()
    if not row or row[0] is None:
        return dict(DEFAULT_WEIGHTS)
    stored = json.loads(row[0])
    return {d: float(stored.get(d, DEFAULT_WEIGHTS[d])) for d in DIMENSIONS}


def load_threshold(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = 'threshold'").fetchone()
    if not row or row[0] is None:
        return DEFAULT_THRESHOLD
    return float(row[0])


def save_settings(conn, threshold, weights):
    _set_setting(conn, "threshold", str(threshold))
    _set_setting(conn, "weights", json.dumps(weights))
    conn.commit()


def _set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


SELECT_SETTINGS_CHANNELS = """
SELECT yt_channel_id, name, source, excluded, exempt_low_transcript
FROM channels WHERE active = 1
ORDER BY source, LOWER(COALESCE(name, yt_channel_id))
"""

SELECT_ACTIVE_TOPICS = "SELECT id, query FROM topics WHERE active = 1 ORDER BY id"


def build_settings(conn):
    weights = load_weights(conn)
    threshold = load_threshold(conn)
    return {
        "threshold_display": f"{threshold:.1f}",
        "weights": [
            {
                "key": d,
                "label": DIMENSION_LABELS[d],
                "percent": round(weights[d] * 100),
            }
            for d in DIMENSIONS
        ],
        "channels": [
            {
                "yt_channel_id": cid,
                "name": name or cid,
                "source": source,
                "excluded": bool(excluded),
                "exempt": bool(exempt),
                "removable": source == "manual",
                "toggle_url": f"/settings/channels/{cid}",
                "remove_url": f"/settings/channels/{cid}/remove",
            }
            for cid, name, source, excluded, exempt
            in conn.execute(SELECT_SETTINGS_CHANNELS).fetchall()
        ],
        "topics": [
            {"id": tid, "query": query, "remove_url": f"/settings/topics/{tid}/remove"}
            for tid, query in conn.execute(SELECT_ACTIVE_TOPICS).fetchall()
        ],
    }


def set_channel_flags(conn, yt_channel_id, excluded, exempt):
    conn.execute(
        "UPDATE channels SET excluded = ?, exempt_low_transcript = ? "
        "WHERE yt_channel_id = ?",
        (int(excluded), int(exempt), yt_channel_id),
    )
    conn.commit()


def add_channel(conn, yt_channel_id, now=None):
    now = now or datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO channels (yt_channel_id, source, active, added_at) "
        "VALUES (?, 'manual', 1, ?) "
        "ON CONFLICT(yt_channel_id) DO UPDATE SET active = 1",
        (yt_channel_id, now),
    )
    conn.commit()


def remove_channel(conn, yt_channel_id):
    conn.execute(
        "UPDATE channels SET active = 0 "
        "WHERE yt_channel_id = ? AND source = 'manual'",
        (yt_channel_id,),
    )
    conn.commit()


def add_topic(conn, query, now=None):
    now = now or datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO topics (query, added_at, active) VALUES (?, ?, 1)",
        (query, now),
    )
    conn.commit()


def remove_topic(conn, topic_id):
    conn.execute("UPDATE topics SET active = 0 WHERE id = ?", (topic_id,))
    conn.commit()


def effective_score(dims, weights):
    total = sum(weights.values())
    if not total:
        return 0.0
    return sum(dims[d] * weights[d] for d in DIMENSIONS) / total


def build_feed(conn, channel=None, since=None, until=None):
    weights = load_weights(conn)
    threshold = load_threshold(conn)
    query, params = _filtered_query(channel, since, until)
    feed, below, flagged, unscored = [], [], [], []
    for row in conn.execute(query, params).fetchall():
        (yt_id, title, thumbnail, status, channel_name, *score_cols, verdict) = row
        info = score_cols[0]
        if info is None:
            unscored.append(
                _unscored_card(yt_id, title, thumbnail, channel_name, status))
            continue
        card = _scored_card(yt_id, title, thumbnail, channel_name, score_cols,
                            weights, verdict)
        if card["hard_flags"]:
            flagged.append(card)
        elif card["score"] >= threshold:
            feed.append(card)
        else:
            below.append(card)
    feed.sort(key=lambda c: c["score"], reverse=True)
    below.sort(key=lambda c: c["score"], reverse=True)
    flagged.sort(key=lambda c: c["score"], reverse=True)
    return {
        "threshold": threshold,
        "feed": feed,
        "below": below,
        "flagged": flagged,
        "unscored": unscored,
        "channels": [
            {"yt_channel_id": cid, "name": name}
            for cid, name in conn.execute(SELECT_CHANNELS).fetchall()
        ],
        "filters": {"channel": channel, "since": since, "until": until},
    }


def _filtered_query(channel, since, until):
    clauses, params = [], []
    if channel:
        clauses.append("c.yt_channel_id = ?")
        params.append(channel)
    if since:
        clauses.append("date(v.published_at) >= date(?)")
        params.append(since)
    if until:
        clauses.append("date(v.published_at) <= date(?)")
        params.append(until)
    query = SELECT_VIDEOS
    if clauses:
        query += "WHERE " + " AND ".join(clauses)
    return query + SELECT_VIDEOS_ORDER, params


def _scored_card(yt_id, title, thumbnail, channel, score_cols, weights, verdict):
    (info, orig, click, pad, depth, prod, summary, flags_json, conf) = score_cols
    dims = {
        "info_density": info, "originality": orig, "clickbait_gap": click,
        "padding": pad, "depth": depth, "production": prod,
    }
    score = effective_score(dims, weights)
    return {
        "youtube_url": YOUTUBE_WATCH_URL + yt_id,
        "detail_url": f"/video/{yt_id}",
        "verdict_url": f"/video/{yt_id}/verdict",
        "verdict": verdict,
        "title": title,
        "thumbnail_url": thumbnail,
        "channel": channel,
        "summary": summary,
        "score": score,
        "score_display": f"{score:.1f}",
        "hard_flags": json.loads(flags_json) if flags_json else [],
        "metadata_only": conf is not None and conf < FULL_CONFIDENCE,
    }


def _unscored_card(yt_id, title, thumbnail, channel, status):
    return {
        "youtube_url": YOUTUBE_WATCH_URL + yt_id,
        "detail_url": f"/video/{yt_id}",
        "title": title,
        "thumbnail_url": thumbnail,
        "channel": channel,
        "reason": UNSCORED_REASONS.get(status, status),
    }


def build_detail(conn, yt_video_id):
    row = conn.execute(SELECT_VIDEO, (yt_video_id,)).fetchone()
    if row is None:
        return None
    (yt_id, title, thumbnail, status, channel, info, orig, click, pad, depth,
     prod, summary, rationale, flags_json, conf, model, prompt_version,
     verdict) = row
    detail = {
        "youtube_url": YOUTUBE_WATCH_URL + yt_id,
        "title": title,
        "thumbnail_url": thumbnail,
        "channel": channel,
        "scored": info is not None,
    }
    if info is None:
        detail["reason"] = UNSCORED_REASONS.get(status, status)
        return detail
    dims = {
        "info_density": info, "originality": orig, "clickbait_gap": click,
        "padding": pad, "depth": depth, "production": prod,
    }
    weights = load_weights(conn)
    score = effective_score(dims, weights)
    detail.update({
        "score_display": f"{score:.1f}",
        "dimensions": [
            {
                "label": DIMENSION_LABELS[d],
                "score_display": f"{dims[d]:.1f}",
                "weight_display": f"{weights[d]:.2f}",
                "pct": max(0.0, min(dims[d] * 10, 100.0)),
            }
            for d in DIMENSIONS
        ],
        "hard_flags": json.loads(flags_json) if flags_json else [],
        "summary": summary,
        "rationale": rationale,
        "model": model,
        "prompt_version": prompt_version,
        "metadata_only": conf is not None and conf < FULL_CONFIDENCE,
        "verdict_url": f"/video/{yt_id}/verdict",
        "verdict": verdict,
    })
    return detail


def record_verdict(conn, yt_video_id, verdict, now=None):
    row = conn.execute(
        "SELECT id FROM videos WHERE yt_video_id = ?", (yt_video_id,)
    ).fetchone()
    if row is None:
        raise LookupError(yt_video_id)
    video_id = row[0]
    current = conn.execute(
        "SELECT verdict FROM feedback WHERE video_id = ? ORDER BY id DESC LIMIT 1",
        (video_id,),
    ).fetchone()
    conn.execute("DELETE FROM feedback WHERE video_id = ?", (video_id,))
    new = None if current and current[0] == verdict else verdict
    if new is not None:
        conn.execute(
            "INSERT INTO feedback (video_id, verdict, created_at) VALUES (?, ?, ?)",
            (video_id, new, now or datetime.now(UTC).isoformat()),
        )
    conn.commit()
    return new
