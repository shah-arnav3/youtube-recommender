import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.db import get_connection


def build_taste_profile() -> dict:
    """
    Assemble a complete taste profile from the watch_history and channel_scores tables.

    Coordinates three sub-builders, each of which queries the DB independently
    and returns a single signal. The results are merged into a single dict that
    downstream scoring and ranking logic can consume.

    Returns:
        A dict with three keys:
            "channel_affinity"     — {channel_id: normalized_watch_frequency}
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


def build_seen_video_ids() -> set[str]:
    """
    Return the set of all video IDs present in the user's watch history.

    Pulls from the full watch history rather than just subscription_videos,
    since the user may have watched videos from non-subscribed channels.
    Deduplication is handled in SQL via DISTINCT.

    Returns:
        Set of video ID strings representing everything the user has watched.
    """
    conn = get_connection()
    try:
        rows = conn.execute("SELECT DISTINCT video_id FROM watch_history").fetchall()
    finally:
        conn.close()

    seen = {row["video_id"] for row in rows}
    print(f"[profile] {len(seen)} seen video IDs loaded")
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
    Compute a normalized watch-frequency score for each subscribed channel.

    Counts how many times each channel appears in watch_history, filters to
    subscribed channels only, then normalizes by dividing by the maximum count
    so all scores fall in the range 0.0–1.0. The channel watched most frequently
    receives a score of 1.0; all others are scaled relative to it.

    Args:
        conn:           An open database connection.
        subscribed_ids: Set of channel IDs to restrict scoring to.

    Returns:
        Dict mapping channel_id → affinity score (float, 0.0–1.0).
        Empty dict if no watch history exists for any subscribed channel.
    """
    rows = conn.execute("""
        SELECT channel_id, COUNT(*) as watch_count
        FROM watch_history
        WHERE channel_id != ''
        GROUP BY channel_id
    """).fetchall()

    # Filter out non-subscribed channels after the query rather than in SQL,
    # since subscribed_ids is a Python set and not available to SQLite directly
    counts = {
        row["channel_id"]: row["watch_count"]
        for row in rows
        if row["channel_id"] in subscribed_ids
    }

    if not counts:
        return {}

    # Normalize so the most-watched channel scores 1.0 and all others are relative
    max_count = max(counts.values())
    return {
        channel_id: round(count / max_count, 4)
        for channel_id, count in counts.items()
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