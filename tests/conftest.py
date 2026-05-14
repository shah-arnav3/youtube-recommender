"""
Shared fixtures for the test suite.
"""
import sys
import os
import pytest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolate_db(tmp_path, monkeypatch):
    """
    Redirect DB_PATH to a throwaway file in pytest's tmp_path for every test.

    autouse=True means this runs automatically — no test needs to request it.
    Any call to get_connection(), patched or not, hits a fresh temp DB instead
    of the real recommender.db. pytest cleans up tmp_path after each test.
    """
    monkeypatch.setattr("config.DB_PATH", str(tmp_path / "test.db"))
    from src.db import init_db
    init_db()


@pytest.fixture
def db():
    """
    Returns an open connection to the isolated test DB, pre-populated with
    the schema. Tests that need to insert fixture data use this directly.
    """
    from src.db import get_connection
    return get_connection()


@pytest.fixture
def taste_profile():
    return {
        "channel_affinity":     {"ch1": 0.5},
        "channel_satisfaction": {"ch1": 0.5},
        "category_weights":     {},
    }


# DB helpers

def insert_video(conn, video_id="v1", channel_id="ch1", channel_title="Chan 1",
                 category_id=27, duration_sec=600, view_count=1000, like_count=100):
    conn.execute(
        "INSERT OR IGNORE INTO subscription_videos "
        "(video_id, title, channel_id, channel_title, category_id, "
        "duration_sec, view_count, like_count, comment_count, published_at, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'), datetime('now'))",
        (video_id, "Title " + video_id, channel_id, channel_title,
         category_id, duration_sec, view_count, like_count)
    )
    conn.commit()


def insert_subscription(conn, channel_id="ch1", channel_title="Chan 1"):
    conn.execute(
        "INSERT OR REPLACE INTO subscriptions "
        "(channel_id, channel_title, uploads_playlist_id, subscribed_at) "
        "VALUES (?, ?, 'PL' || ?, datetime('now'))",
        (channel_id, channel_title, channel_id)
    )
    conn.commit()


def insert_watch(conn, video_id="v1", channel_id="ch1", days_ago=1):
    watched_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    conn.execute(
        "INSERT INTO watch_history (video_id, channel_id, channel_title, watched_at) "
        "VALUES (?, ?, 'Chan', ?)",
        (video_id, channel_id, watched_at.isoformat())
    )
    conn.commit()