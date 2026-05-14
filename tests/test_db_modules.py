"""
Tests for inverted_index, taste_profile, and feedback.

DB isolation is handled by the autouse `isolate_db` fixture in conftest.py —
every test gets a fresh throwaway DB automatically. No manual patching needed.
"""
import sys
import os
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.conftest import insert_video, insert_subscription, insert_watch


# InvertedIndex

@pytest.fixture
def built_index(db):
    insert_video(db, "v1", "ch1", category_id=27)
    insert_video(db, "v2", "ch1", category_id=27)
    insert_video(db, "v3", "ch2", category_id=28)
    db.close()
    from src.index.inverted_index import InvertedIndex
    idx = InvertedIndex()
    idx.build()
    return idx


def test_index_groups_videos_by_category(built_index):
    assert set(built_index.index[27]) == {"v1", "v2"}
    assert set(built_index.index[28]) == {"v3"}


def test_random_mood_returns_all(built_index):
    assert set(built_index.get_candidates("random")) == {"v1", "v2", "v3"}


def test_mood_returns_matching_categories(built_index):
    from config import MOOD_CATEGORY_MAP
    learn_cats = MOOD_CATEGORY_MAP["learn"]
    candidates = set(built_index.get_candidates("learn"))
    if 27 in learn_cats:
        assert "v1" in candidates
    if 28 in learn_cats:
        assert "v3" in candidates


# Taste Profile

@pytest.fixture
def profile_db(db):
    insert_subscription(db, "ch1")
    insert_subscription(db, "ch2")
    insert_video(db, "v1", "ch1", category_id=27)
    insert_video(db, "v2", "ch2", category_id=28)
    db.close()
    return db


def _build_profile():
    from src.profile.taste_profile import build_taste_profile
    return build_taste_profile()


def test_recently_watched_channel_scores_higher(profile_db):
    from src.db import get_connection
    conn = get_connection()
    insert_watch(conn, "v1", "ch1", days_ago=2)
    insert_watch(conn, "v2", "ch2", days_ago=500)
    conn.close()
    p = _build_profile()
    assert p["channel_affinity"]["ch1"] > p["channel_affinity"]["ch2"]


def test_affinity_normalized_to_1(profile_db):
    from src.db import get_connection
    conn = get_connection()
    insert_watch(conn, "v1", "ch1", days_ago=1)
    conn.close()
    p = _build_profile()
    assert p["channel_affinity"]["ch1"] == 1.0


def test_category_weights_sum_to_1(profile_db):
    from src.db import get_connection
    conn = get_connection()
    insert_watch(conn, "v1", "ch1")
    insert_watch(conn, "v2", "ch2")
    conn.close()
    p = _build_profile()
    assert sum(p["category_weights"].values()) == pytest.approx(1.0, abs=1e-3)


def test_seen_video_window_excludes_old_watches(db):
    insert_watch(db, "old_vid", days_ago=200)
    db.close()
    from src.profile.taste_profile import build_seen_video_ids
    assert "old_vid" not in build_seen_video_ids(window_days=90)


def test_seen_video_window_includes_recent(db):
    insert_watch(db, "new_vid", days_ago=5)
    db.close()
    from src.profile.taste_profile import build_seen_video_ids
    assert "new_vid" in build_seen_video_ids(window_days=90)


# Feedback

@pytest.fixture
def feedback_setup(db, taste_profile):
    insert_video(db, "v1", "ch1", "Chan 1")
    db.close()
    cache = MagicMock()
    cache.invalidate_containing.return_value = 1
    return cache, taste_profile


def _log(cache, taste_profile, video_id="v1", rating=1):
    from src.engine.feedback import log_feedback
    return log_feedback(
        video_id=video_id, rating=rating, mood_tag="learn",
        time_budget_sec=1200, lru_cache=cache, taste_profile=taste_profile,
    )


def test_feedback_persisted(feedback_setup):
    cache, profile = feedback_setup
    _log(cache, profile, rating=1)
    from src.db import get_connection
    row = get_connection().execute("SELECT rating FROM feedback WHERE video_id = 'v1'").fetchone()
    assert row["rating"] == 1


def test_unknown_video_returns_error(feedback_setup):
    cache, profile = feedback_setup
    assert _log(cache, profile, video_id="ghost")["status"] == "error"


def test_positive_rating_nudges_affinity_up(feedback_setup):
    cache, profile = feedback_setup
    before = profile["channel_affinity"]["ch1"]
    _log(cache, profile, rating=1)
    assert profile["channel_affinity"]["ch1"] > before


def test_negative_nudge_is_smaller_than_positive(feedback_setup):
    from config import AFFINITY_NUDGE
    cache, profile = feedback_setup
    profile["channel_affinity"]["ch1"] = 0.5
    _log(cache, profile, rating=1)
    up = profile["channel_affinity"]["ch1"] - 0.5

    profile["channel_affinity"]["ch1"] = 0.5
    _log(cache, profile, rating=0)
    down = 0.5 - profile["channel_affinity"]["ch1"]

    assert up == pytest.approx(AFFINITY_NUDGE)
    assert down == pytest.approx(AFFINITY_NUDGE * 0.5)


def test_cache_invalidated_on_feedback(feedback_setup):
    cache, profile = feedback_setup
    _log(cache, profile)
    cache.invalidate_containing.assert_called_once_with("v1")