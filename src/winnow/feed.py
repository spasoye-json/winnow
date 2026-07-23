import json
from datetime import UTC, datetime, timedelta

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
       v.duration_sec, v.published_at, v.view_count,
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
       s.model, s.prompt_version, s.scored_at,{VERDICT_COLUMN}
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
    weight_rows = [
        {
            "key": d,
            "label": DIMENSION_LABELS[d],
            "percent": round(weights[d] * 100),
        }
        for d in DIMENSIONS
    ]
    weights_sum = sum(w["percent"] for w in weight_rows)
    return {
        "threshold_display": f"{threshold:.1f}",
        "weights": weight_rows,
        "weights_sum": weights_sum,
        "weights_ok": weights_sum == 100,
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
        "ON CONFLICT(yt_channel_id) DO UPDATE SET active = 1, "
        "source = CASE WHEN channels.active = 0 THEN 'manual' "
        "ELSE channels.source END",
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


CALIBRATION_FLOOR = 20

DIMENSION_ABBREV = {
    "info_density": "ID",
    "originality": "OR",
    "clickbait_gap": "CB",
    "padding": "PA",
    "depth": "DE",
    "production": "PR",
}

SELECT_CALIBRATION = """
SELECT v.yt_video_id, v.title, c.name,
       s.info_density, s.originality, s.clickbait_gap, s.padding, s.depth,
       s.production, f.verdict
FROM videos v
LEFT JOIN channels c ON c.id = v.channel_id
JOIN scores s ON s.id = (
    SELECT id FROM scores WHERE video_id = v.id
    AND model = ? AND prompt_version = ?
    ORDER BY scored_at DESC, id DESC LIMIT 1
)
JOIN feedback f ON f.id = (
    SELECT id FROM feedback WHERE video_id = v.id ORDER BY id DESC LIMIT 1
)
"""


def build_calibration(conn):
    from winnow.scoring import PROMPT_VERSION, model_name

    weights = load_weights(conn)
    threshold = load_threshold(conn)
    model = model_name()
    great_total = great_agree = 0
    slop_total = slop_agree = 0
    disagreements = []
    for row in conn.execute(SELECT_CALIBRATION, (model, PROMPT_VERSION)).fetchall():
        yt_id, title, channel, info, orig, click, pad, depth, prod, verdict = row
        dims = {
            "info_density": info, "originality": orig, "clickbait_gap": click,
            "padding": pad, "depth": depth, "production": prod,
        }
        score = effective_score(dims, weights)
        above = score >= threshold
        if verdict == "great":
            great_total += 1
            great_agree += above
        else:
            slop_total += 1
            slop_agree += not above
        if (verdict == "great") != above:
            disagreements.append({
                "detail_url": f"/video/{yt_id}",
                "title": title,
                "channel": channel,
                "verdict": verdict,
                "score_display": f"{score:.1f}",
                "distance": abs(score - threshold),
                "dimensions": [
                    {"abbr": DIMENSION_ABBREV[d], "label": DIMENSION_LABELS[d],
                     "score_display": f"{dims[d]:.1f}"}
                    for d in DIMENSIONS
                ],
            })
    disagreements.sort(key=lambda d: d["distance"])
    sample_valid = (great_total >= CALIBRATION_FLOOR
                    and slop_total >= CALIBRATION_FLOOR)
    provisional = not sample_valid
    tiles = [
        _agreement_tile("Greats above threshold", great_agree, great_total,
                        "great", "above", provisional),
        _agreement_tile("Slop below threshold", slop_agree, slop_total,
                        "slop", "below", provisional),
    ]
    return {
        "threshold_display": f"{threshold:.1f}",
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "tiles": tiles,
        "disagreements": disagreements,
        "dimension_headers": [
            {"abbr": DIMENSION_ABBREV[d], "label": DIMENSION_LABELS[d]}
            for d in DIMENSIONS
        ],
        "bar_failed": sample_valid and any(not t["met"] for t in tiles),
    }


def _agreement_tile(label, agree, total, noun, direction, provisional):
    pct = round(agree / total * 100) if total else None
    return {
        "label": label,
        "pct_display": f"{pct}%" if pct is not None else "—",
        "count_display": (
            f"{agree} of {total} {noun} verdicts scored {direction} threshold"),
        "provisional": provisional,
        "needed_display": (
            f"{total} of {CALIBRATION_FLOOR} verdicts needed"
            if total < CALIBRATION_FLOOR else None),
        "met": pct is not None and pct >= 80,
    }


def effective_score(dims, weights):
    total = sum(weights.values())
    if not total:
        return 0.0
    return sum(dims[d] * weights[d] for d in DIMENSIONS) / total


RANGE_DAYS = {"7": 7, "30": 30}

DATE_RANGES = [
    {"value": "7", "label": "Last 7 days"},
    {"value": "30", "label": "Last 30 days"},
    {"value": "all", "label": "All time"},
]


def build_feed(conn, channel=None, topic=None, date_range="all", show_below=False,
               now=None):
    weights = load_weights(conn)
    threshold = load_threshold(conn)
    if date_range not in RANGE_DAYS:
        date_range = "all"
    since = _range_since(date_range, now)
    query, params = _filtered_query(channel, topic, since)
    items, unscored = [], []
    for row in conn.execute(query, params).fetchall():
        (yt_id, title, thumbnail, status, channel_name, duration, published,
         views, *score_cols, verdict) = row
        info = score_cols[0]
        if info is None:
            unscored.append(_unscored_card(yt_id, title, thumbnail, channel_name,
                                           status, duration, published, views))
            continue
        items.append(_scored_card(yt_id, title, thumbnail, channel_name, score_cols,
                                  weights, threshold, verdict, duration, published,
                                  views))
    items.sort(key=lambda c: c["score"], reverse=True)
    passing = [c for c in items if c["passing"]]
    hidden_count = len(items) - len(passing)
    return {
        "threshold_display": f"{threshold:.1f}",
        "items": items if show_below else passing,
        "show_below": show_below,
        "hidden_count": hidden_count,
        "hidden_label": _hidden_label(hidden_count, threshold),
        "unscored": unscored,
        "channels": [
            {"yt_channel_id": cid, "name": name}
            for cid, name in conn.execute(SELECT_CHANNELS).fetchall()
        ],
        "topics": [
            {"id": tid, "query": topic_query}
            for tid, topic_query in conn.execute(SELECT_ACTIVE_TOPICS).fetchall()
        ],
        "date_ranges": DATE_RANGES,
        "filters": {
            "channel": channel, "topic": topic, "range": date_range,
        },
    }


def _range_since(date_range, now):
    days = RANGE_DAYS.get(date_range)
    if days is None:
        return None
    now = now or datetime.now(UTC)
    return (now - timedelta(days=days)).isoformat()


def _hidden_label(count, threshold):
    noun = "video" if count == 1 else "videos"
    return f"{count} {noun} hidden below {threshold:.1f}"


def _filtered_query(channel, topic, since):
    clauses, params = [], []
    if channel:
        clauses.append("c.yt_channel_id = ?")
        params.append(channel)
    if topic:
        clauses.append("v.topic_id = ?")
        params.append(topic)
    if since:
        clauses.append("v.published_at >= ?")
        params.append(since)
    query = SELECT_VIDEOS
    if clauses:
        query += "WHERE " + " AND ".join(clauses)
    return query + SELECT_VIDEOS_ORDER, params


def _scored_card(yt_id, title, thumbnail, channel, score_cols, weights, threshold,
                 verdict, duration, published, views):
    (info, orig, click, pad, depth, prod, summary, flags_json, conf) = score_cols
    dims = {
        "info_density": info, "originality": orig, "clickbait_gap": click,
        "padding": pad, "depth": depth, "production": prod,
    }
    score = effective_score(dims, weights)
    hard_flags = json.loads(flags_json) if flags_json else []
    return {
        "youtube_url": YOUTUBE_WATCH_URL + yt_id,
        "detail_url": f"/video/{yt_id}",
        "verdict_url": f"/video/{yt_id}/verdict",
        "verdict": verdict,
        "title": title,
        "thumbnail_url": thumbnail,
        "meta": _meta_line(channel, published, views),
        "duration_display": _format_duration(duration),
        "summary": summary,
        "score": score,
        "score_display": f"{score:.1f}",
        "hard_flags": hard_flags,
        "auto_failed": ", ".join(hard_flags) if hard_flags else None,
        "passing": not hard_flags and score >= threshold,
        "metadata_only": conf is not None and conf < FULL_CONFIDENCE,
    }


def _unscored_card(yt_id, title, thumbnail, channel, status, duration, published,
                   views):
    return {
        "youtube_url": YOUTUBE_WATCH_URL + yt_id,
        "detail_url": f"/video/{yt_id}",
        "title": title,
        "thumbnail_url": thumbnail,
        "meta": _meta_line(channel, published, views),
        "duration_display": _format_duration(duration),
        "reason": UNSCORED_REASONS.get(status, status),
    }


def _meta_line(channel, published, views):
    parts = []
    if channel:
        parts.append(channel)
    date = _format_date(published)
    if date:
        parts.append(date)
    view_label = _format_views(views)
    if view_label is not None:
        parts.append(f"{view_label} views")
    return " · ".join(parts)


def _format_duration(seconds):
    if seconds is None:
        return None
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_date(published):
    if not published:
        return None
    parsed = datetime.fromisoformat(published)
    return f"{parsed:%b} {parsed.day}"


def _format_views(views):
    if views is None:
        return None
    if views < 1_000:
        return str(views)
    for divisor, suffix in ((1_000, "K"), (1_000_000, "M"), (1_000_000_000, "B")):
        value = views / divisor
        # 999.5 and up would render as 1000; promote to the next unit instead
        if value >= 999.5 and suffix != "B":
            continue
        if value >= 100:
            return f"{value:.0f}{suffix}"
        return f"{value:.1f}".rstrip("0").rstrip(".") + suffix


def build_detail(conn, yt_video_id):
    row = conn.execute(SELECT_VIDEO, (yt_video_id,)).fetchone()
    if row is None:
        return None
    (yt_id, title, thumbnail, status, channel, info, orig, click, pad, depth,
     prod, summary, rationale, flags_json, conf, model, prompt_version,
     scored_at, verdict) = row
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
    threshold = load_threshold(conn)
    score = effective_score(dims, weights)
    hard_flags = json.loads(flags_json) if flags_json else []
    detail.update({
        "score_display": f"{score:.1f}",
        "passing": not hard_flags and score >= threshold,
        "dimensions": [
            {
                "label": DIMENSION_LABELS[d],
                "score_display": f"{dims[d]:.1f}",
                "weight_display": f"{round(weights[d] * 100)}%",
                "pct": max(0.0, min(dims[d] * 10, 100.0)),
                "passing": dims[d] >= threshold,
            }
            for d in DIMENSIONS
        ],
        "hard_flags": hard_flags,
        "auto_failed": ", ".join(hard_flags) if hard_flags else None,
        "summary": summary,
        "rationale": rationale,
        "model": model,
        "prompt_version": prompt_version,
        "scored_display": _format_date(scored_at),
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
