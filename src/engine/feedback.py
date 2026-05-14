import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.db import get_connection
from config import AFFINITY_NUDGE


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

    Updates three things after persisting to the DB:
      - channel_satisfaction in the taste profile (Step 5)
      - channel_affinity in the taste profile (Step 5b)
      - the LRU cache, evicting any entries that contained this video (Step 6)

    Both in-memory updates take effect immediately so the very next query
    reflects the rating without requiring a server restart.

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
                positive_ratings  = positive_ratings + ?,
                total_ratings     = total_ratings + 1,
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

    # Step 5 — update channel_satisfaction in the taste profile so the next
    # query uses the freshly computed rate rather than the value from startup.
    taste_profile["channel_satisfaction"][channel_id] = satisfaction_rate

    # Step 5b — nudge channel_affinity in the taste profile.
    # Satisfaction and affinity are different signals: satisfaction tracks whether
    # content from a channel is good; affinity tracks how much the user wants to
    # see from it. A positive rating should strengthen both. A negative rating
    # weakens affinity at half the rate to avoid harshly penalising a channel the
    # user generally likes based on a single bad video.
    current_affinity = taste_profile["channel_affinity"].get(channel_id, 0.0)
    if rating == 1:
        taste_profile["channel_affinity"][channel_id] = min(1.0, current_affinity + AFFINITY_NUDGE)
    else:
        taste_profile["channel_affinity"][channel_id] = max(0.0, current_affinity - AFFINITY_NUDGE * 0.5)

    new_affinity = taste_profile["channel_affinity"][channel_id]

    # Step 6 — invalidate cached results containing this video.
    invalidated = lru_cache.invalidate_containing(video_id)

    print(f"[feedback] Logged rating={rating} for {video_id} "
          f"(channel: {channel_title}, "
          f"satisfaction: {positive_ratings}/{total_ratings} = {satisfaction_rate:.2f}, "
          f"affinity: {current_affinity:.3f} → {new_affinity:.3f})")

    return {
        "status":                    "logged",
        "channel_satisfaction_rate": satisfaction_rate,
        "channel_affinity":          new_affinity,
        "cache_entries_invalidated": invalidated,
    }