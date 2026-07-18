import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_credentials (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'google',
    refresh_token TEXT,
    access_token TEXT,
    expires_at TEXT,
    scopes TEXT,
    connected_at TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    yt_channel_id TEXT NOT NULL UNIQUE,
    name TEXT,
    source TEXT NOT NULL CHECK (source IN ('subscription', 'manual')),
    excluded INTEGER NOT NULL DEFAULT 0,
    exempt_low_transcript INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    added_at TEXT,
    last_synced_at TEXT
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    query TEXT NOT NULL,
    added_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    yt_video_id TEXT NOT NULL UNIQUE,
    channel_id INTEGER REFERENCES channels(id),
    title TEXT,
    description TEXT,
    duration_sec INTEGER,
    published_at TEXT,
    view_count INTEGER,
    thumbnail_url TEXT,
    transcript_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (transcript_status IN ('pending', 'ok', 'no_transcript', 'fetch_failed')),
    caption_language TEXT,
    ingested_at TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    overall REAL,
    info_density REAL,
    originality REAL,
    clickbait_gap REAL,
    padding REAL,
    depth REAL,
    production REAL,
    hard_flags TEXT,
    summary TEXT,
    rationale TEXT,
    model TEXT,
    prompt_version INTEGER,
    scored_at TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL REFERENCES videos(id),
    verdict TEXT NOT NULL CHECK (verdict IN ('great', 'slop')),
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()
