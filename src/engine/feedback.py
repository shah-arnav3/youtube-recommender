import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.db import get_connection


def log_feedback(
    video_id: str,
    rating: int,
    mood_tag: str,
    time_budget_sec: int,
    lru_cache,
    taste_profile: dict,
) -> dict:
    """
    Log a y/n rating for a video and update all downstream state.

    rating: 1 = satisfied, 0 = not satisfied

    Returns a summary dict for the API response.
    """
    conn = get_connection()
    try:
        # Step 1 — verify video exists in subscription_videos
        video = conn.execute(
            "SELECT channel_id, channel_title FROM subscription_videos WHERE video_id = ?",
            (video_id,)
        ).fetchone()

        if not video:
            print(f"[feedback] WARNING: video {video_id} not found in subscription_videos")
            return {"status": "error", "message": "video not found"}

        channel_id    = video["channel_id"]
        channel_title = video["channel_title"]

        # Step 2 — log to feedback table
        conn.execute("""
            INSERT INTO feedback (video_id, rating, mood_tag, time_budget_sec, rated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (video_id, rating, mood_tag, time_budget_sec, datetime.now(timezone.utc)))

        # Step 3 — update channel_scores
        conn.execute("""
            INSERT INTO channel_scores
                (channel_id, channel_title, positive_ratings, total_ratings,
                 satisfaction_rate, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                positive_ratings = positive_ratings + ?,
                total_ratings    = total_ratings + 1,
                satisfaction_rate = CAST(positive_ratings + ? AS REAL) / (total_ratings + 1),
                updated_at        = ?
        """, (
            channel_id, channel_title, rating,
            float(rating),
            datetime.now(timezone.utc),
            rating, rating,
            datetime.now(timezone.utc),
        ))

        conn.commit()

        # Step 4 — read back updated satisfaction rate
        updated = conn.execute(
            "SELECT satisfaction_rate, positive_ratings, total_ratings FROM channel_scores WHERE channel_id = ?",
            (channel_id,)
        ).fetchone()

        satisfaction_rate = updated["satisfaction_rate"]
        positive_ratings  = updated["positive_ratings"]
        total_ratings     = updated["total_ratings"]

    finally:
        conn.close()

    # Step 5 — update taste profile in memory so next query uses fresh scores
    taste_profile["channel_satisfaction"][channel_id] = satisfaction_rate

    # Step 6 — invalidate cached results containing this video
    invalidated = lru_cache.invalidate_containing(video_id)

    print(f"[feedback] Logged rating={rating} for {video_id} "
          f"(channel: {channel_title}, "
          f"satisfaction: {positive_ratings}/{total_ratings} = {satisfaction_rate:.2f})")

    return {
        "status":                   "logged",
        "channel_satisfaction_rate": satisfaction_rate,
        "cache_entries_invalidated": invalidated,
    }