import json

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

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v="

UNSCORED_REASONS = {
    "pending": "awaiting scoring",
    "no_transcript": "no captions",
    "fetch_failed": "transcript fetch failed",
}

SELECT_VIDEOS = """
SELECT v.yt_video_id, v.title, v.thumbnail_url, v.transcript_status, c.name,
       s.info_density, s.originality, s.clickbait_gap, s.padding, s.depth,
       s.production, s.summary, s.hard_flags, s.confidence
FROM videos v
LEFT JOIN channels c ON c.id = v.channel_id
LEFT JOIN scores s ON s.id = (
    SELECT id FROM scores WHERE video_id = v.id
    ORDER BY scored_at DESC, id DESC LIMIT 1
)
ORDER BY v.published_at DESC, v.id DESC
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


def effective_score(dims, weights):
    total = sum(weights.values())
    if not total:
        return 0.0
    return sum(dims[d] * weights[d] for d in DIMENSIONS) / total


def build_feed(conn):
    weights = load_weights(conn)
    threshold = load_threshold(conn)
    feed, below, flagged, unscored = [], [], [], []
    for row in conn.execute(SELECT_VIDEOS).fetchall():
        (yt_id, title, thumbnail, status, channel, *score_cols) = row
        info = score_cols[0]
        if info is None:
            unscored.append(_unscored_card(yt_id, title, thumbnail, channel, status))
            continue
        card = _scored_card(yt_id, title, thumbnail, channel, score_cols, weights)
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
    }


def _scored_card(yt_id, title, thumbnail, channel, score_cols, weights):
    (info, orig, click, pad, depth, prod, summary, flags_json, conf) = score_cols
    dims = {
        "info_density": info, "originality": orig, "clickbait_gap": click,
        "padding": pad, "depth": depth, "production": prod,
    }
    score = effective_score(dims, weights)
    return {
        "youtube_url": YOUTUBE_WATCH_URL + yt_id,
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
        "title": title,
        "thumbnail_url": thumbnail,
        "channel": channel,
        "reason": UNSCORED_REASONS.get(status, status),
    }
