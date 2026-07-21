import json

from fastapi.testclient import TestClient

from winnow.cli import main
from winnow.db import connect
from winnow.web import create_app


def test_app_serves_and_runs_loop_without_credentials(tmp_path):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])

    app = create_app(str(db_path), str(tmp_path / "missing_secrets.json"))
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


DIMENSIONS = ("info_density", "originality", "clickbait_gap", "padding", "depth",
              "production")


def _seed_db(tmp_path):
    db_path = tmp_path / "winnow.db"
    main(["init", "--db", str(db_path)])
    return db_path


def _channel(conn, name, yt_channel_id):
    cur = conn.execute(
        "INSERT INTO channels (yt_channel_id, name, source) VALUES (?, ?, 'manual')",
        (yt_channel_id, name),
    )
    return cur.lastrowid


def _video(conn, yt_video_id, channel_id, *, title="A title",
           transcript_status="ok", thumbnail="https://i.ytimg.com/vi/x/hq.jpg",
           published_at="2026-07-20T00:00:00+00:00"):
    cur = conn.execute(
        "INSERT INTO videos (yt_video_id, channel_id, title, thumbnail_url, "
        "transcript_status, published_at) VALUES (?, ?, ?, ?, ?, ?)",
        (yt_video_id, channel_id, title, thumbnail, transcript_status, published_at),
    )
    return cur.lastrowid


def _score(conn, video_id, dims, *, overall=0.0, hard_flags=None, summary="A summary.",
           confidence=1.0, scored_at="2026-07-20T01:00:00+00:00"):
    conn.execute(
        "INSERT INTO scores (video_id, overall, info_density, originality, "
        "clickbait_gap, padding, depth, production, hard_flags, summary, rationale, "
        "confidence, model, prompt_version, scored_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            video_id, overall, dims["info_density"], dims["originality"],
            dims["clickbait_gap"], dims["padding"], dims["depth"], dims["production"],
            json.dumps(hard_flags or []), summary, "rationale", confidence,
            "gemini-3.1-flash-lite", 1, scored_at,
        ),
    )


def _flat(value):
    return {d: value for d in DIMENSIONS}


def _set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _get(db_path):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.get("/")


def _get_filtered(db_path, params):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.get("/", params=params)


def _get_detail(db_path, yt_video_id):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.get(f"/video/{yt_video_id}")


def test_feed_card_shows_all_fields(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Deep Dives", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="On Compilers")
    _score(conn, video_id, _flat(8), overall=8.0, summary="Explains compiler passes.")
    conn.commit()
    conn.close()

    response = _get(db_path)
    body = response.text

    assert response.status_code == 200
    assert "On Compilers" in body
    assert "Deep Dives" in body
    assert "Explains compiler passes." in body
    assert "https://i.ytimg.com/vi/x/hq.jpg" in body
    assert "https://www.youtube.com/watch?v=vid123" in body
    assert "8.0" in body


def test_feed_ranks_above_threshold_by_effective_score(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    low = _video(conn, "low", channel_id, title="Lower quality")
    high = _video(conn, "high", channel_id, title="Higher quality")
    _score(conn, low, _flat(6.5), overall=6.5)
    _score(conn, high, _flat(9.0), overall=9.0)
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert body.index("Higher quality") < body.index("Lower quality")


def test_effective_score_recomputed_not_stored_overall(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid", channel_id, title="Recomputed")
    _score(conn, video_id, _flat(8.0), overall=1.0)
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert "8.0" in body
    assert "Recomputed" in body


def test_rescored_video_uses_latest_score(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid", channel_id, title="Rescored")
    _score(conn, video_id, _flat(9.0), overall=9.0, summary="Latest verdict.",
           scored_at="2026-07-20T02:00:00+00:00")
    _score(conn, video_id, _flat(2.0), overall=2.0, summary="Stale verdict.",
           scored_at="2026-07-19T01:00:00+00:00")
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert "Latest verdict." in body
    assert "Stale verdict." not in body


def test_missing_thumbnail_and_channel_render_without_placeholders(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    video_id = _video(conn, "bare", None, title="Bare video", thumbnail=None)
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert "Bare video" in body
    assert 'src="None"' not in body
    assert ">None<" not in body


def test_weights_change_moves_video_below_threshold(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid", channel_id, title="Weight sensitive")
    dims = {**_flat(9.0), "padding": 0.0}
    _score(conn, video_id, dims, overall=8.0)
    _set_setting(conn, "weights", json.dumps({
        "info_density": 0.0, "originality": 0.0, "clickbait_gap": 0.0,
        "padding": 1.0, "depth": 0.0, "production": 0.0,
    }))
    _set_setting(conn, "threshold", "6.0")
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert "Below threshold" in body
    below = body.split("Below threshold", 1)[1]
    assert "Weight sensitive" in below


def test_threshold_setting_controls_feed(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid", channel_id, title="Middling")
    _score(conn, video_id, _flat(7.0), overall=7.0)
    _set_setting(conn, "threshold", "8.0")
    conn.commit()
    conn.close()

    body = _get(db_path).text
    below = body.split("Below threshold", 1)[1]
    assert "Middling" in below


def test_hard_flagged_excluded_from_main_feed(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    flagged = _video(conn, "flagged", channel_id, title="Flagged high scorer")
    clean = _video(conn, "clean", channel_id, title="Clean high scorer")
    _score(conn, flagged, _flat(9.5), overall=9.5, hard_flags=["ai_voice"])
    _score(conn, clean, _flat(9.0), overall=9.0)
    conn.commit()
    conn.close()

    body = _get(db_path).text
    feed_section = body.split("Below threshold")[0].split("Hard-flagged")[0]
    assert "Clean high scorer" in feed_section
    assert "Flagged high scorer" not in feed_section
    assert "Flagged high scorer" in body


def test_unscored_section_lists_pending_and_failures(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    _video(conn, "pend", channel_id, title="Pending video",
           transcript_status="pending")
    _video(conn, "notx", channel_id, title="No captions video",
           transcript_status="no_transcript")
    _video(conn, "failx", channel_id, title="Fetch failed video",
           transcript_status="fetch_failed")
    conn.commit()
    conn.close()

    body = _get(db_path).text
    unscored = body.split("Unscored", 1)[1]
    assert "Pending video" in unscored
    assert "No captions video" in unscored
    assert "Fetch failed video" in unscored


def test_metadata_only_score_is_marked(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "meta", channel_id, title="Metadata only",
                      transcript_status="no_transcript")
    _score(conn, video_id, _flat(8.0), overall=8.0, confidence=0.5)
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert "Metadata only" in body
    assert "metadata-only" in body.lower()


def test_genre_bias_note_visible(tmp_path):
    db_path = _seed_db(tmp_path)
    body = _get(db_path).text
    assert "genre" in body.lower()


def test_detail_shows_full_score_breakdown(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Deep Dives", "chan1")
    dims = {
        "info_density": 8.0, "originality": 7.0, "clickbait_gap": 9.0,
        "padding": 6.0, "depth": 5.0, "production": 4.0,
    }
    video_id = _video(conn, "vid123", channel_id, title="On Compilers")
    _score(conn, video_id, dims, overall=7.0, summary="Explains compiler passes.")
    conn.commit()
    conn.close()

    response = _get_detail(db_path, "vid123")
    body = response.text

    assert response.status_code == 200
    assert "On Compilers" in body
    assert "Deep Dives" in body
    for label in ("Information density", "Originality", "Clickbait gap", "Padding",
                  "Depth", "Production integrity"):
        assert label in body
    assert "Explains compiler passes." in body
    assert "rationale" in body
    assert "gemini-3.1-flash-lite" in body
    assert "prompt v1" in body


def test_detail_links_to_youtube(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="Linked out")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get_detail(db_path, "vid123").text
    assert "https://www.youtube.com/watch?v=vid123" in body


def test_detail_shows_hard_flags(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "flagged", channel_id, title="Flagged one")
    _score(conn, video_id, _flat(9.0), overall=9.0, hard_flags=["ai_voice"])
    conn.commit()
    conn.close()

    body = _get_detail(db_path, "flagged").text
    assert "ai_voice" in body


def test_detail_reachable_from_feed_card(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="Card links here")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert "/video/vid123" in body


def test_detail_unscored_video_shows_reason_without_breakdown(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    _video(conn, "pend", channel_id, title="Pending video",
           transcript_status="pending")
    conn.commit()
    conn.close()

    body = _get_detail(db_path, "pend").text
    assert "Pending video" in body
    assert "awaiting scoring" in body
    assert "Score breakdown" not in body


def test_detail_unknown_video_is_404(tmp_path):
    db_path = _seed_db(tmp_path)
    response = _get_detail(db_path, "nope")
    assert response.status_code == 404


def test_detail_effective_score_recomputed_from_weights(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid", channel_id, title="Recomputed")
    _score(conn, video_id, _flat(8.0), overall=1.0)
    conn.commit()
    conn.close()

    body = _get_detail(db_path, "vid").text
    assert "8.0" in body


def test_feed_filters_by_channel(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    alpha = _channel(conn, "Alpha", "chanA")
    beta = _channel(conn, "Beta", "chanB")
    va = _video(conn, "vidA", alpha, title="Alpha video")
    vb = _video(conn, "vidB", beta, title="Beta video")
    _score(conn, va, _flat(8.0), overall=8.0)
    _score(conn, vb, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get_filtered(db_path, {"channel": "chanA"}).text
    assert "Alpha video" in body
    assert "Beta video" not in body


def test_feed_filters_by_since_date(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    old = _video(conn, "old", channel_id, title="Old video",
                 published_at="2026-07-01T00:00:00+00:00")
    new = _video(conn, "new", channel_id, title="New video",
                 published_at="2026-07-20T00:00:00+00:00")
    _score(conn, old, _flat(8.0), overall=8.0)
    _score(conn, new, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get_filtered(db_path, {"since": "2026-07-10"}).text
    assert "New video" in body
    assert "Old video" not in body


def test_feed_filters_by_until_date(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    old = _video(conn, "old", channel_id, title="Old video",
                 published_at="2026-07-01T00:00:00+00:00")
    new = _video(conn, "new", channel_id, title="New video",
                 published_at="2026-07-20T00:00:00+00:00")
    _score(conn, old, _flat(8.0), overall=8.0)
    _score(conn, new, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get_filtered(db_path, {"until": "2026-07-10"}).text
    assert "Old video" in body
    assert "New video" not in body


def test_until_date_is_inclusive_of_the_whole_day(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    same_day = _video(conn, "same", channel_id, title="Same day video",
                      published_at="2026-07-10T18:30:00+00:00")
    _score(conn, same_day, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get_filtered(db_path, {"until": "2026-07-10"}).text
    assert "Same day video" in body


def test_filters_compose_with_threshold_and_ranking(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    alpha = _channel(conn, "Alpha", "chanA")
    beta = _channel(conn, "Beta", "chanB")
    high = _video(conn, "high", alpha, title="Alpha high")
    mid = _video(conn, "mid", alpha, title="Alpha mid")
    low = _video(conn, "low", alpha, title="Alpha low")
    other = _video(conn, "other", beta, title="Beta other")
    _score(conn, high, _flat(9.0), overall=9.0)
    _score(conn, mid, _flat(8.0), overall=8.0)
    _score(conn, low, _flat(6.5), overall=6.5)
    _score(conn, other, _flat(9.0), overall=9.0)
    _set_setting(conn, "threshold", "7.0")
    conn.commit()
    conn.close()

    body = _get_filtered(db_path, {"channel": "chanA"}).text
    assert "Beta other" not in body

    feed_section = body.split("Below threshold", 1)[0]
    assert "Alpha high" in feed_section
    assert "Alpha mid" in feed_section
    assert "Alpha low" not in feed_section
    assert feed_section.index("Alpha high") < feed_section.index("Alpha mid")

    below = body.split("Below threshold", 1)[1]
    assert "Alpha low" in below


def test_filter_form_lists_channels(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    alpha = _channel(conn, "Alpha", "chanA")
    beta = _channel(conn, "Beta", "chanB")
    _score(conn, _video(conn, "vidA", alpha), _flat(8.0), overall=8.0)
    _score(conn, _video(conn, "vidB", beta), _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert 'name="channel"' in body
    assert 'name="since"' in body
    assert 'name="until"' in body
    assert 'value="chanA"' in body
    assert 'value="chanB"' in body


def test_no_cdn_assets(tmp_path):
    db_path = _seed_db(tmp_path)
    body = _get(db_path).text
    assert "cdn" not in body.lower()
    assert "unpkg" not in body.lower()
    assert "jsdelivr" not in body.lower()
    assert "/static/pico" in body
