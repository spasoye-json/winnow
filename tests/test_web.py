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


def _get(db_path, params=None):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.get("/", params=params)


def _get_detail(db_path, yt_video_id):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.get(f"/video/{yt_video_id}")


def _post_verdict(db_path, yt_video_id, verdict):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.post(f"/video/{yt_video_id}/verdict", data={"verdict": verdict})


def _get_settings(db_path):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.get("/settings")


def _post_settings(db_path, data):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.post("/settings", data=data)


DEFAULT_SETTINGS_FORM = {
    "threshold": "6.0",
    "info_density": "25",
    "originality": "20",
    "clickbait_gap": "20",
    "padding": "15",
    "depth": "15",
    "production": "5",
}


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

    body = _get(db_path, {"channel": "chanA"}).text
    assert "Alpha video" in body
    assert "Beta video" not in body


def _topic(conn, query):
    cur = conn.execute(
        "INSERT INTO topics (query, active) VALUES (?, 1)", (query,)
    )
    return cur.lastrowid


def _topic_video(conn, yt_video_id, topic_id, *, title="A title",
                 published_at="2026-07-20T00:00:00+00:00"):
    cur = conn.execute(
        "INSERT INTO videos (yt_video_id, topic_id, title, thumbnail_url, "
        "transcript_status, published_at) VALUES (?, ?, ?, ?, 'ok', ?)",
        (yt_video_id, topic_id, title, "https://i.ytimg.com/vi/x/hq.jpg",
         published_at),
    )
    return cur.lastrowid


def test_feed_filters_by_topic(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    solar = _topic(conn, "solar power")
    fusion = _topic(conn, "fusion")
    vs = _topic_video(conn, "vidS", solar, title="Solar video")
    vf = _topic_video(conn, "vidF", fusion, title="Fusion video")
    _score(conn, vs, _flat(8.0), overall=8.0)
    _score(conn, vf, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get(db_path, {"topic": str(solar)}).text
    assert "Solar video" in body
    assert "Fusion video" not in body


def test_filter_form_lists_topics(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    solar = _topic(conn, "solar power")
    _score(conn, _topic_video(conn, "vidS", solar), _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert 'name="topic"' in body
    assert "solar power" in body
    assert f'value="{solar}"' in body


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

    body = _get(db_path, {"since": "2026-07-10"}).text
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

    body = _get(db_path, {"until": "2026-07-10"}).text
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

    body = _get(db_path, {"until": "2026-07-10"}).text
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

    body = _get(db_path, {"channel": "chanA"}).text
    assert "Beta other" not in body

    feed_section = body.split("Below threshold", 1)[0]
    assert "Alpha high" in feed_section
    assert "Alpha mid" in feed_section
    assert "Alpha low" not in feed_section
    assert feed_section.index("Alpha high") < feed_section.index("Alpha mid")

    below = body.split("Below threshold", 1)[1]
    assert "Alpha low" in below


def test_empty_filter_params_return_unfiltered_feed(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid", channel_id, title="Unfiltered video")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get(db_path, {"channel": "", "since": "", "until": ""}).text
    assert "Unfiltered video" in body


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


def test_htmx_vendored_as_static_file(tmp_path):
    db_path = _seed_db(tmp_path)
    body = _get(db_path).text
    assert "/static/htmx" in body

    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        response = client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text


def test_feed_card_shows_verdict_buttons(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="Judged video")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get(db_path).text
    assert "Great" in body
    assert "Slop" in body
    assert 'hx-post="/video/vid123/verdict"' in body


def test_post_verdict_returns_button_state_snippet(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="Judged video")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    response = _post_verdict(db_path, "vid123", "great")
    body = response.text

    assert response.status_code == 200
    assert 'hx-post="/video/vid123/verdict"' in body
    assert 'value="great"' in body
    assert 'value="slop"' in body
    assert 'aria-pressed="true"' in body
    assert "<html" not in body.lower()


def test_verdict_persists_and_shows_on_feed(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="Judged video")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    _post_verdict(db_path, "vid123", "great")

    conn = connect(str(db_path))
    rows = conn.execute(
        "SELECT verdict FROM feedback WHERE video_id = ?", (video_id,)
    ).fetchall()
    conn.close()
    assert rows == [("great",)]

    body = _get(db_path).text
    great_button = body.split('value="great"', 1)[1].split(">", 1)[0]
    assert 'aria-pressed="true"' in great_button


def test_verdict_is_one_per_video_and_replaceable(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="Judged video")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    _post_verdict(db_path, "vid123", "great")
    response = _post_verdict(db_path, "vid123", "slop")

    conn = connect(str(db_path))
    rows = conn.execute(
        "SELECT verdict FROM feedback WHERE video_id = ?", (video_id,)
    ).fetchall()
    conn.close()
    assert rows == [("slop",)]

    slop_button = response.text.split('value="slop"', 1)[1].split(">", 1)[0]
    assert 'aria-pressed="true"' in slop_button


def test_reposting_same_verdict_clears_it(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="Judged video")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    _post_verdict(db_path, "vid123", "great")
    response = _post_verdict(db_path, "vid123", "great")

    conn = connect(str(db_path))
    rows = conn.execute(
        "SELECT verdict FROM feedback WHERE video_id = ?", (video_id,)
    ).fetchall()
    conn.close()
    assert rows == []
    assert 'aria-pressed="true"' not in response.text


def test_post_verdict_unknown_video_is_404(tmp_path):
    db_path = _seed_db(tmp_path)
    response = _post_verdict(db_path, "nope", "great")
    assert response.status_code == 404


def test_post_invalid_verdict_is_rejected(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="Judged video")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    response = _post_verdict(db_path, "vid123", "meh")
    assert response.status_code == 422


def test_detail_shows_verdict_buttons(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid123", channel_id, title="Judged video")
    _score(conn, video_id, _flat(8.0), overall=8.0)
    conn.commit()
    conn.close()

    body = _get_detail(db_path, "vid123").text
    assert 'hx-post="/video/vid123/verdict"' in body


def test_no_cdn_assets(tmp_path):
    db_path = _seed_db(tmp_path)
    body = _get(db_path).text
    assert "cdn" not in body.lower()
    assert "unpkg" not in body.lower()
    assert "jsdelivr" not in body.lower()
    assert "/static/pico" in body


def _score_count(db_path):
    conn = connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    conn.close()
    return count


def test_settings_page_is_plain_html_form_with_current_values(tmp_path):
    db_path = _seed_db(tmp_path)
    response = _get_settings(db_path)
    body = response.text

    assert response.status_code == 200
    assert '<form method="post" action="/settings"' in body
    assert 'name="threshold"' in body
    for dim in DIMENSIONS:
        assert f'name="{dim}"' in body


def test_settings_defaults_match_prd_rubric(tmp_path):
    db_path = _seed_db(tmp_path)
    body = _get_settings(db_path).text

    assert 'name="threshold"' in body
    assert 'value="6.0"' in body
    expected = {"info_density": "25", "originality": "20", "clickbait_gap": "20",
                "padding": "15", "depth": "15", "production": "5"}
    for dim, percent in expected.items():
        field = body.split(f'name="{dim}"', 1)[1].split(">", 1)[0]
        assert f'value="{percent}"' in field


def test_settings_form_persists_threshold_and_weights(tmp_path):
    db_path = _seed_db(tmp_path)

    _post_settings(db_path, {
        **DEFAULT_SETTINGS_FORM,
        "threshold": "7.5",
        "info_density": "0",
        "padding": "50",
    })

    conn = connect(str(db_path))
    threshold = conn.execute(
        "SELECT value FROM settings WHERE key = 'threshold'").fetchone()[0]
    weights = json.loads(
        conn.execute("SELECT value FROM settings WHERE key = 'weights'").fetchone()[0])
    conn.close()

    assert float(threshold) == 7.5
    assert weights["info_density"] == 0.0
    assert weights["padding"] == 0.5

    body = _get_settings(db_path).text
    assert 'value="7.5"' in body
    padding_field = body.split('name="padding"', 1)[1].split(">", 1)[0]
    assert 'value="50"' in padding_field


def test_settings_change_reranks_feed_instantly_without_rescoring(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    channel_id = _channel(conn, "Chan", "chan1")
    video_id = _video(conn, "vid", channel_id, title="Weight sensitive")
    _score(conn, video_id, {**_flat(9.0), "padding": 0.0}, overall=8.0)
    conn.commit()
    conn.close()

    before = _get(db_path).text
    assert "Weight sensitive" in before.split("Below threshold")[0]

    scores_before = _score_count(db_path)
    _post_settings(db_path, {
        **DEFAULT_SETTINGS_FORM,
        "info_density": "0", "originality": "0", "clickbait_gap": "0",
        "padding": "100", "depth": "0", "production": "0",
    })
    assert _score_count(db_path) == scores_before

    after = _get(db_path).text
    below = after.split("Below threshold", 1)[1]
    assert "Weight sensitive" in below


def test_settings_form_rejects_out_of_range_values(tmp_path):
    db_path = _seed_db(tmp_path)

    for bad in ({"threshold": "10.5"}, {"threshold": "-1"},
                {"padding": "-5"}, {"depth": "101"}):
        response = _post_settings(db_path, {**DEFAULT_SETTINGS_FORM, **bad})
        assert response.status_code == 422

    conn = connect(str(db_path))
    stored = conn.execute(
        "SELECT COUNT(*) FROM settings WHERE key IN ('threshold', 'weights')"
    ).fetchone()[0]
    conn.close()
    assert stored == 0


def test_feed_links_to_settings(tmp_path):
    db_path = _seed_db(tmp_path)
    body = _get(db_path).text
    assert 'href="/settings"' in body


def _settings_channel(conn, name, yt_channel_id, *, source="subscription",
                      excluded=0, exempt=0, active=1):
    conn.execute(
        "INSERT INTO channels (yt_channel_id, name, source, excluded, "
        "exempt_low_transcript, active, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (yt_channel_id, name, source, excluded, exempt, active),
    )
    conn.commit()


def _post(db_path, path, data=None):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.post(path, data=data or {})


def test_settings_lists_channels_with_excluded_and_exempt_checkboxes(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    _settings_channel(conn, "Deep Dives", "chan1")
    conn.close()

    body = _get_settings(db_path).text
    assert "Deep Dives" in body
    assert "Excluded" in body
    assert "Exempt" in body
    assert 'name="excluded"' in body
    assert 'name="exempt"' in body


def test_settings_toggles_channel_excluded_and_exempt(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    _settings_channel(conn, "Chan", "chan1")
    conn.close()

    _post(db_path, "/settings/channels/chan1", {"excluded": "1", "exempt": "1"})
    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT excluded, exempt_low_transcript FROM channels "
        "WHERE yt_channel_id = 'chan1'").fetchone()
    conn.close()
    assert row == (1, 1)

    _post(db_path, "/settings/channels/chan1", {})
    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT excluded, exempt_low_transcript FROM channels "
        "WHERE yt_channel_id = 'chan1'").fetchone()
    conn.close()
    assert row == (0, 0)


def test_settings_adds_manual_channel(tmp_path):
    db_path = _seed_db(tmp_path)

    _post(db_path, "/settings/channels", {"yt_channel_id": "UCnew"})

    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT source, active, excluded FROM channels "
        "WHERE yt_channel_id = 'UCnew'").fetchone()
    conn.close()
    assert row == ("manual", 1, 0)

    body = _get_settings(db_path).text
    assert "UCnew" in body


def test_settings_adding_lapsed_subscription_makes_it_manual(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    _settings_channel(conn, "Lapsed", "UClapsed", source="subscription", active=0)
    conn.close()

    _post(db_path, "/settings/channels", {"yt_channel_id": "UClapsed"})

    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT source, active FROM channels "
        "WHERE yt_channel_id = 'UClapsed'").fetchone()
    conn.close()
    assert row == ("manual", 1)


def test_settings_adding_active_subscription_keeps_its_source(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    _settings_channel(conn, "Subbed", "UCsub", source="subscription")
    conn.close()

    _post(db_path, "/settings/channels", {"yt_channel_id": "UCsub"})

    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT source, active FROM channels "
        "WHERE yt_channel_id = 'UCsub'").fetchone()
    conn.close()
    assert row == ("subscription", 1)


def test_settings_removing_channel_deactivates_not_deletes(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    _settings_channel(conn, "Manual One", "UCman", source="manual")
    conn.close()

    _post(db_path, "/settings/channels/UCman/remove")

    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT active FROM channels WHERE yt_channel_id = 'UCman'").fetchone()
    conn.close()
    assert row == (0,)

    body = _get_settings(db_path).text
    assert "Manual One" not in body


def test_settings_removing_subscription_channel_leaves_it_active(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    _settings_channel(conn, "Subbed", "UCsub", source="subscription")
    conn.close()

    _post(db_path, "/settings/channels/UCsub/remove")

    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT active FROM channels WHERE yt_channel_id = 'UCsub'").fetchone()
    conn.close()
    assert row == (1,)


def test_settings_only_manual_channels_are_removable(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    _settings_channel(conn, "Subbed", "UCsub", source="subscription")
    _settings_channel(conn, "Manual One", "UCman", source="manual")
    conn.close()

    body = _get_settings(db_path).text
    assert "/settings/channels/UCman/remove" in body
    assert "/settings/channels/UCsub/remove" not in body


def test_settings_add_channel_rejects_blank(tmp_path):
    db_path = _seed_db(tmp_path)

    response = _post(db_path, "/settings/channels", {"yt_channel_id": "   "})
    assert response.status_code == 422

    conn = connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
    conn.close()
    assert count == 0


def test_settings_adds_topic(tmp_path):
    db_path = _seed_db(tmp_path)

    _post(db_path, "/settings/topics", {"query": "energy storage"})

    conn = connect(str(db_path))
    rows = conn.execute("SELECT query, active FROM topics").fetchall()
    conn.close()
    assert rows == [("energy storage", 1)]

    body = _get_settings(db_path).text
    assert "energy storage" in body


def test_settings_removing_topic_deactivates(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    cur = conn.execute("INSERT INTO topics (query, active) VALUES ('gone', 1)")
    topic_id = cur.lastrowid
    conn.commit()
    conn.close()

    _post(db_path, f"/settings/topics/{topic_id}/remove")

    conn = connect(str(db_path))
    row = conn.execute(
        "SELECT active FROM topics WHERE id = ?", (topic_id,)).fetchone()
    conn.close()
    assert row == (0,)

    body = _get_settings(db_path).text
    assert "gone" not in body


def test_settings_add_topic_rejects_blank(tmp_path):
    db_path = _seed_db(tmp_path)

    response = _post(db_path, "/settings/topics", {"query": "  "})
    assert response.status_code == 422

    conn = connect(str(db_path))
    count = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    conn.close()
    assert count == 0


def _get_calibration(db_path):
    app = create_app(str(db_path), str(db_path.parent / "missing_secrets.json"))
    with TestClient(app) as client:
        return client.get("/calibration")


def _judged(conn, channel_id, yt_video_id, dims, verdict, *, title="A title",
            model="gemini-3.1-flash-lite", prompt_version=1):
    video_id = _video(conn, yt_video_id, channel_id, title=title)
    conn.execute(
        "INSERT INTO scores (video_id, overall, info_density, originality, "
        "clickbait_gap, padding, depth, production, hard_flags, summary, rationale, "
        "confidence, model, prompt_version, scored_at) "
        "VALUES (?, 0, ?, ?, ?, ?, ?, ?, '[]', 's', 'r', 1.0, ?, ?, "
        "'2026-07-20T01:00:00+00:00')",
        (
            video_id, dims["info_density"], dims["originality"], dims["clickbait_gap"],
            dims["padding"], dims["depth"], dims["production"], model, prompt_version,
        ),
    )
    conn.execute(
        "INSERT INTO feedback (video_id, verdict, created_at) "
        "VALUES (?, ?, '2026-07-20T02:00:00+00:00')",
        (video_id, verdict),
    )
    return video_id


def _seed_verdicts(conn, channel_id, greats, slops, *, great_dims=None,
                   slop_dims=None):
    great_dims = great_dims or _flat(8.0)
    slop_dims = slop_dims or _flat(4.0)
    for i in range(greats):
        _judged(conn, channel_id, f"g{i}", great_dims, "great")
    for i in range(slops):
        _judged(conn, channel_id, f"s{i}", slop_dims, "slop")


def test_calibration_shows_two_agreement_tiles_with_pct_and_counts(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _judged(conn, ch, "g1", _flat(8.0), "great")
    _judged(conn, ch, "g2", _flat(4.0), "great")
    _judged(conn, ch, "s1", _flat(4.0), "slop")
    _judged(conn, ch, "s2", _flat(8.0), "slop")
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert "Greats above threshold" in body
    assert "Slop below threshold" in body
    assert "50%" in body
    assert "1 of 2" in body


def test_calibration_tiles_provisional_below_floor(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _seed_verdicts(conn, ch, 5, 5)
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert "provisional" in body.lower()


def _seeded_calibration(tmp_path, name, greats, slops):
    root = tmp_path / name
    root.mkdir()
    db_path = _seed_db(root)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _seed_verdicts(conn, ch, greats, slops)
    conn.commit()
    conn.close()
    return _get_calibration(db_path).text


def test_calibration_provisional_at_19_but_not_at_20_per_class(tmp_path):
    assert "provisional" in _seeded_calibration(tmp_path, "a", 19, 20).lower()
    assert "provisional" in _seeded_calibration(tmp_path, "b", 20, 19).lower()
    assert "provisional" not in _seeded_calibration(tmp_path, "c", 20, 20).lower()


def test_calibration_needed_count_only_on_tiles_below_floor(tmp_path):
    body = _seeded_calibration(tmp_path, "a", 25, 5)
    assert "provisional" in body.lower()
    assert "5 of 20 verdicts needed" in body
    assert "25 of 20 verdicts needed" not in body


def test_calibration_lists_slop_scored_above_threshold_as_disagreement(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _judged(conn, ch, "s1", _flat(8.0), "slop", title="Overrated video")
    _judged(conn, ch, "s2", _flat(4.0), "slop", title="Agreeing video")
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert "Overrated video" in body
    assert "Agreeing video" not in body
    assert "1 of 2 slop verdicts scored below threshold" in body


def test_calibration_disagreement_list_sorted_by_distance_from_threshold(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _judged(conn, ch, "far", _flat(3.0), "great", title="Far video")
    _judged(conn, ch, "near", _flat(5.0), "great", title="Near video")
    _judged(conn, ch, "mid", _flat(4.0), "great", title="Mid video")
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert body.index("Near video") < body.index("Mid video")
    assert body.index("Mid video") < body.index("Far video")


def test_calibration_disagreement_row_shows_all_six_dimension_scores(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    dims = {
        "info_density": 9.0, "originality": 1.0, "clickbait_gap": 2.0,
        "padding": 3.0, "depth": 4.0, "production": 5.0,
    }
    _judged(conn, ch, "vid", dims, "great", title="Disagreeing video")
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    row = body.split("Disagreeing video", 1)[1]
    for value in ("9.0", "1.0", "2.0", "3.0", "4.0", "5.0"):
        assert value in row


def test_calibration_recomputes_agreement_from_current_weights(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _judged(conn, ch, "vid", {**_flat(9.0), "padding": 0.0}, "great",
            title="Weight sensitive")
    _set_setting(conn, "weights", json.dumps({
        "info_density": 0.0, "originality": 0.0, "clickbait_gap": 0.0,
        "padding": 1.0, "depth": 0.0, "production": 0.0,
    }))
    _set_setting(conn, "threshold", "6.0")
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert "Weight sensitive" in body
    assert "0 of 1" in body


def test_calibration_recomputes_agreement_from_current_threshold(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _judged(conn, ch, "vid", _flat(7.0), "great", title="Middling great")
    _set_setting(conn, "threshold", "8.0")
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert "Middling great" in body
    assert "0 of 1" in body


def test_calibration_scoped_to_current_model_and_prompt_version(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _judged(conn, ch, "cur", _flat(8.0), "great", title="Current scope")
    _judged(conn, ch, "othermodel", _flat(4.0), "great", title="Other model",
            model="other-model")
    _judged(conn, ch, "oldprompt", _flat(8.0), "slop", title="Old prompt",
            prompt_version=2)
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert "Other model" not in body
    assert "Old prompt" not in body
    assert "1 of 1" in body


def test_calibration_bar_failed_indicator_with_valid_sample(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _seed_verdicts(conn, ch, 20, 20, great_dims=_flat(4.0), slop_dims=_flat(4.0))
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert "provisional" not in body.lower()
    assert "agreement bar has failed" in body.lower()


def test_calibration_no_bar_indicator_when_sample_is_provisional(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _seed_verdicts(conn, ch, 5, 5, great_dims=_flat(4.0), slop_dims=_flat(4.0))
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert "agreement bar has failed" not in body.lower()


def test_calibration_no_disagreements_when_scores_align(tmp_path):
    db_path = _seed_db(tmp_path)
    conn = connect(str(db_path))
    ch = _channel(conn, "Chan", "chan1")
    _judged(conn, ch, "g1", _flat(8.0), "great")
    _judged(conn, ch, "s1", _flat(4.0), "slop")
    conn.commit()
    conn.close()

    body = _get_calibration(db_path).text
    assert "No disagreements" in body


def test_calibration_scopes_to_current_model_and_prompt_in_footer(tmp_path):
    db_path = _seed_db(tmp_path)
    body = _get_calibration(db_path).text
    assert "gemini-3.1-flash-lite" in body
    assert "prompt v1" in body


def test_feed_links_to_calibration(tmp_path):
    db_path = _seed_db(tmp_path)
    body = _get(db_path).text
    assert 'href="/calibration"' in body
