import sqlite3
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import DB_PATH

DDL = """
CREATE TABLE IF NOT EXISTS subscription_videos (
    video_id        TEXT PRIMARY KEY,
    title           TEXT,
    channel_id      TEXT,
    channel_title   TEXT,
    category_id     INTEGER,
    duration_sec    INTEGER,
    view_count      INTEGER,
    like_count      INTEGER,
    comment_count   INTEGER,
    published_at    TIMESTAMP,
    fetched_at      TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscriptions (
    channel_id          TEXT PRIMARY KEY,
    channel_title       TEXT,
    uploads_playlist_id TEXT,
    subscribed_at       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watch_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      TEXT,
    channel_id    TEXT,
    channel_title TEXT,
    watched_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id        TEXT,
    rating          INTEGER,
    mood_tag        TEXT,
    time_budget_sec INTEGER,
    rated_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channel_scores (
    channel_id          TEXT PRIMARY KEY,
    channel_title       TEXT,
    positive_ratings    INTEGER DEFAULT 0,
    total_ratings       INTEGER DEFAULT 0,
    satisfaction_rate   REAL    DEFAULT 0.5,
    updated_at          TIMESTAMP
);
"""


def get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row # to make rows behave as dicts
    conn.execute("PRAGMA journal_mode=WAL") # write-ahead-log
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(DDL)
        conn.commit()
        print(f"[db] Initialized at {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()