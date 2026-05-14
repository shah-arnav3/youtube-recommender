import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.db import get_connection
from config import AFFINITY_DECAY, SEEN_VIDEO_WINDOW_DAYS


def build_taste_profile() -> dict:
    """
    Assemble a complete taste profile from the watch_history and channel_scores tables.

    Coordinates three sub-builders, each of which queries the DB independently
    and returns a single signal. The results are merged into a single dict that
    downstream scoring and ranking logic can consume.

    Returns:
        A dict with three keys:
            "channel_affinity"     — {channel_id: recency-decayed affinity score, 0.0–1.0}
            "category_weights"     — {category_id: fraction_of_total_watches}
            "channel_satisfaction" — {channel_id: satisfaction_rate}
    """
    conn = get_connection()
    try:
        subscribed_ids       = _load_subscribed_channel_ids(conn)
        channel_affinity     = build_channel_affinity(conn, subscribed_ids)
        category_weights     = build_category_weights(conn)
        channel_satisfaction = build_channel_satisfaction(conn)
    finally:
        conn.close()

    profile = {
        "channel_affinity":     channel_affinity,
        "category_weights":     category_weights,
        "channel_satisfaction": channel_satisfaction,
    }

    print(f"[profile] Built taste profile:")
    print(f"  {len(channel_affinity)} channels with affinity scores")
    print(f"  {len(category_weights)} categories tracked")
    print(f"  {len(channel_satisfaction)} channels with satisfaction scores")

    return profile


def build_seen_video_ids(window_days: int = SEEN_VIDEO_WINDOW_DAYS) -> set[str]:
    """
    Return the set of video IDs the user has watched within the recent time window.

    Rather than loading the entire watch history (which permanently blacklists every
    video ever watched), this restricts the seen filter to the last `window_days` days.
    Videos watched outside the window become candidates again, preventing the candidate
    pool from silently shrinking to nothing over time.

    Pulls from the full watch history rather than just subscription_videos, since
    the user may have watched videos from non-subscribed channels. Deduplication is
    handled in SQL via DISTINCT.

    Args:
        window_days: Number of days back to consider a video "seen". Defaults to
                     SEEN_VIDEO_WINDOW_DAYS from config (90 days).

    Returns:
        Set of video ID strings watched within the window.
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT DISTINCT video_id FROM watch_history
            WHERE watched_at >= datetime('now', '-' || ? || ' days')
        """, (window_days,)).fetchall()
    finally:
        conn.close()

    seen = {row["video_id"] for row in rows}
    print(f"[profile] {len(seen)} seen video IDs loaded (last {window_days} days)")
    return seen


def _load_subscribed_channel_ids(conn) -> set[str]:
    """
    Load the set of channel IDs from the subscriptions table.

    Private helper — intended to be called once per build_taste_profile() run,
    reusing the same connection rather than opening a new one.

    Args:
        conn: An open database connection.

    Returns:
        Set of channel ID strings for all subscribed channels.
    """
    rows = conn.execute("SELECT channel_id FROM subscriptions").fetchall()
    return {row["channel_id"] for row in rows}


def build_channel_affinity(conn, subscribed_ids: set[str]) -> dict[str, float]:
    """
    Compute a recency-decayed affinity score for each subscribed channel.

    Rather than counting raw watches, each watch event is weighted by how recently
    it occurred using exponential decay: weight = exp(-AFFINITY_DECAY * days_ago).
    This means recent watches contribute much more than old ones — a channel watched
    heavily two years ago but rarely now will score lower than one watched consistently
    this month.

    Decay constant AFFINITY_DECAY = 0.005 gives a half-life of ~140 days: a watch
    from 140 days ago contributes half as much as one from today. This is tunable
    in config.py.

    After summing decayed weights per channel, scores are normalized by dividing by
    the maximum so all values fall in 0.0–1.0. The channel with the highest
    recency-weighted watch count scores 1.0; all others are scaled relative to it.

    Args:
        conn:           An open database connection.
        subscribed_ids: Set of channel IDs to restrict scoring to. Non-subscribed
                        channels are filtered out after the query since subscribed_ids
                        is a Python set and not directly available to SQLite.

    Returns:
        Dict mapping channel_id → affinity score (float, 0.0–1.0).
        Empty dict if no watch history exists for any subscribed channel.
    """
    rows = conn.execute("""
        SELECT channel_id, watched_at
        FROM watch_history
        WHERE channel_id != ''
    """).fetchall()

    now = datetime.now(timezone.utc)
    scores: dict[str, float] = {}

    for row in rows:
        cid = row["channel_id"]
        if cid not in subscribed_ids:
            continue

        # Compute days elapsed since this watch event; default to 365 if timestamp
        # is missing (treated as roughly a year old, contributing minimal weight)
        raw_ts = row["watched_at"]
        if raw_ts:
            try:
                watched_at = datetime.fromisoformat(str(raw_ts).rstrip("Z")).replace(tzinfo=timezone.utc)
                days_ago = (now - watched_at).days
            except (ValueError, TypeError):
                days_ago = 365
        else:
            days_ago = 365
        weight   = math.exp(-AFFINITY_DECAY * days_ago)
        scores[cid] = scores.get(cid, 0.0) + weight

    if not scores:
        return {}

    # Normalize so the highest-scoring channel is 1.0 and all others are relative
    max_score = max(scores.values())
    return {
        cid: round(s / max_score, 4)
        for cid, s in scores.items()
    }


def build_category_weights(conn) -> dict[int, float]:
    """
    Compute what fraction of the user's watch history falls into each category.

    Joins watch_history against subscription_videos to resolve category_id, since
    watch_history only stores video_id and channel info — category is not recorded
    at watch time. Videos in watch_history that don't appear in subscription_videos
    (e.g. from non-subscribed channels) are excluded by the inner join.

    Args:
        conn: An open database connection.

    Returns:
        Dict mapping category_id (int): fraction of total matched watches (float,
        0.0–1.0). Empty dict if no watch history can be joined to subscription_videos.
    """
    rows = conn.execute("""
        SELECT sv.category_id, COUNT(*) as watch_count
        FROM watch_history wh
        JOIN subscription_videos sv ON wh.video_id = sv.video_id
        GROUP BY sv.category_id
    """).fetchall()

    total = sum(row["watch_count"] for row in rows)
    if total == 0:
        return {}

    return {
        row["category_id"]: round(row["watch_count"] / total, 4)
        for row in rows
    }


def build_channel_satisfaction(conn) -> dict[str, float]:
    """
    Load per-channel satisfaction rates from the channel_scores table.

    channel_scores is populated by a separate feedback pipeline and may not
    contain every subscribed channel. Channels absent from this dict have no
    recorded feedback — callers should handle missing keys explicitly rather
    than assuming any default value.

    Args:
        conn: An open database connection.

    Returns:
        Dict mapping channel_id → satisfaction_rate (float).
        Only channels with at least one recorded feedback event are included.
    """
    rows = conn.execute("""
        SELECT channel_id, satisfaction_rate
        FROM channel_scores
    """).fetchall()

    return {
        row["channel_id"]: row["satisfaction_rate"]
        for row in rows
    }