import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import (
    THREAD_POOL_WORKERS,
    EMPTY_CHANNELS_LOG,
    FAILED_CHANNELS_LOG,
    LOGS_DIR,
)
from src.db import get_connection
from src.ingest.thread_pool import ThreadPool
from src.ingest.fetcher import (
    get_uploads_playlist_ids,
    get_recent_uploads,
    get_video_metadata,
)
from src.ingest.parser import parse_subscriptions, parse_watch_history


def ingest_watch_history(json_path: str) -> int:
    """
        Parse a Google Takeout watch-history.json and insert all entries into the DB.

        This is a single-threaded, no-API operation: the Takeout JSON already contains
        everything we need (video ID, channel, timestamp), so no network calls are made.

        Uses "INSERT OR IGNORE" so that re-running the ingest is idempotent — duplicate
        entries (same video_id + watched_at) are silently skipped rather than raising
        a uniqueness constraint error.

        Args:
            json_path: Path to the watch-history.json file from Google Takeout.

        Returns:
            The number of rows passed to INSERT (including both new and already-existing
            rows, since IGNORE suppresses errors but still counts the attempt).
    """
    entries = parse_watch_history(json_path)
    conn = get_connection()
    inserted = 0
    try:
        for e in entries:
            conn.execute("""
                INSERT OR IGNORE INTO watch_history
                    (video_id, channel_id, channel_title, watched_at)
                VALUES (?, ?, ?, ?)
            """, (
                e["video_id"],
                e["channel_id"],
                e["channel_title"],
                e["watched_at"],
            ))
            inserted += 1
        conn.commit()
    finally:
        conn.close()

    print(f"[ingest] {inserted} watch history entries written")
    return inserted


def ingest_subscriptions(csv_path: str) -> int:
    """
        Parse a Takeout subscriptions CSV, enrich it with playlist IDs, and write to DB.

        Two steps:
          1. Parse the local CSV to get channel IDs and titles (no API needed yet).
          2. Batch-fetch the uploads playlist ID for each channel from the YouTube API,
             since we'll need these IDs later to fetch each channel's recent videos.

        Uses "INSERT OR REPLACE" so that re-running updates existing rows rather than
        failing. This means if a channel's playlist ID changes or the title updates,
        the row is refreshed automatically.

        Note: subscribed_at is set to the current time rather than the real subscription
        date — Takeout's subscriptions.csv doesn't include that timestamp.

        Args:
            csv_path: Path to the subscriptions.csv file from Google Takeout.

        Returns:
            The number of channel rows written (inserted or replaced) to the DB.
    """
    channels = parse_subscriptions(csv_path)
    channel_ids = [c["channel_id"] for c in channels]

    print(f"[ingest] Fetching uploads playlist IDs for {len(channel_ids)} channels...")
    # This makes batched API calls (up to 50 IDs per request) to the channels endpoint
    playlist_map = get_uploads_playlist_ids(channel_ids)

    conn = get_connection()
    inserted = 0
    try:
        for c in channels:
            cid = c["channel_id"]
            # Some channels may not be returned by the API (private, terminated, etc.)
            # playlist_id will be None for those, stored as NULL in the DB
            playlist_id = playlist_map.get(cid)
            conn.execute("""
                INSERT OR REPLACE INTO subscriptions
                    (channel_id, channel_title, uploads_playlist_id, subscribed_at)
                VALUES (?, ?, ?, ?)
            """, (
                cid,
                c["channel_title"],
                playlist_id,
                datetime.now(timezone.utc),
            ))
            inserted += 1
        conn.commit()
    finally:
        conn.close()

    print(f"[ingest] {inserted} subscriptions written")
    return inserted


def ingest_subscription_videos() -> int:
    """
        Fetch recent videos from all subscribed channels and write them to the DB.

        This is the most complex ingestion step and the only one that uses threading.
        It proceeds in four phases:

        Phase 1 — Load channels from DB:
            Reads all rows from the subscriptions table that have a non-NULL
            uploads_playlist_id. Channels without one (private/terminated) are skipped.

        Phase 2 — Concurrent playlist fetching:
            Submits one task per channel to the ThreadPool. Each task calls
            get_recent_uploads(playlist_id) and returns the list of video IDs
            (or None if the channel is empty or errored). Workers run these in parallel,
            which dramatically reduces wall-clock time compared to fetching serially.

            Tasks are created using a make_task() closure to avoid the classic Python
            loop-closure bug: if we used a lambda directly inside the for loop, all
            lambdas would capture the same loop variable (pid) by reference, and by
            the time they execute, pid would hold the last iteration's value. The
            make_task() factory freezes the values by binding them as default arguments.

        Phase 3 — Deduplication:
            Collects all returned video IDs, deduplicates across channels (a video
            can appear in multiple channels' playlists if re-uploaded), then filters
            out IDs already present in the subscription_videos table. This avoids
            redundant API calls for metadata we already have.

        Phase 4 — Metadata fetching and DB write:
            Calls get_video_metadata() for all new video IDs (batched internally),
            then inserts the resulting rows into subscription_videos using
            INSERT OR IGNORE for idempotency.

        Returns:
            The number of new video rows inserted into subscription_videos.
        """
    os.makedirs(LOGS_DIR, exist_ok=True)

    # Load channels from DB
    conn = get_connection()
    rows = conn.execute("""
        SELECT channel_id, channel_title, uploads_playlist_id
        FROM subscriptions
        WHERE uploads_playlist_id IS NOT NULL
    """).fetchall()
    conn.close()

    print(f"[ingest] Fetching recent uploads for {len(rows)} channels...")

    # Use thread pool to fetch video IDs from all channels concurrently
    pool = ThreadPool(num_workers=THREAD_POOL_WORKERS)
    pool.start()

    # Collect channels that return no videos or error out, for diagnostics
    empty_channels = []
    failed_channels = []

    for row in rows:
        channel_id    = row["channel_id"]
        channel_title = row["channel_title"]
        playlist_id   = row["uploads_playlist_id"]

        def make_task(cid, title, pid):
            """
                Factory function that creates a task closure for a single channel.

                By passing cid/title/pid as function arguments, we bind their values
                at call time (when make_task() is invoked in the loop), so each task
                closure independently captures the correct values.
            """

            def task():
                try:
                    video_ids = get_recent_uploads(pid)
                    if not video_ids:
                        # Channel exists but has no recent uploads — note it and
                        # return None so the result is excluded from channel_results
                        empty_channels.append(f"{cid} — {title}")
                        return None
                    return {"channel_id": cid, "video_ids": video_ids}
                except Exception as e:
                    # Unexpected error (e.g. API failure not caught in get_recent_uploads)
                    failed_channels.append(f"{cid} — {title}: {e}")
                    return None
            return task

        pool.submit(make_task(channel_id, channel_title, playlist_id))

    # Send poison pills and wait for all workers to finish processing
    pool.shutdown()
    # Collect all non-None results (channels that returned ≥1 video ID)
    channel_results = [r for r in pool.drain_results() if r is not None]

    # Log empty and failed channels
    if empty_channels:
        with open(EMPTY_CHANNELS_LOG, "w") as f:
            f.write("\n".join(empty_channels))
        print(f"[ingest] {len(empty_channels)} empty channels logged")

    if failed_channels:
        with open(FAILED_CHANNELS_LOG, "w") as f:
            f.write("\n".join(failed_channels))
        print(f"[ingest] {len(failed_channels)} failed channels logged")

    # Flatten all video IDs from all channel results into one list, then
    # deduplicate with set() to avoid fetching metadata for the same video twice
    # (a video ID could appear in multiple channels if the API returns overlaps)
    all_video_ids = []
    for r in channel_results:
        all_video_ids.extend(r["video_ids"])
    all_video_ids = list(set(all_video_ids))

    # Filter out videos already in the DB to avoid redundant API calls
    # and duplicate rows (even with INSERT OR IGNORE, we save API quota)
    conn = get_connection()
    existing = {
        row[0] for row in
        conn.execute("SELECT video_id FROM subscription_videos").fetchall()
    }
    conn.close()

    new_video_ids = [v for v in all_video_ids if v not in existing]
    print(f"[ingest] {len(new_video_ids)} new videos to fetch metadata for")

    if not new_video_ids: # Nothing new to fetch
        return 0

    # Fetch metadata and write to DB
    print("[ingest] Fetching video metadata...")
    videos = get_video_metadata(new_video_ids)

    conn = get_connection()
    inserted = 0
    try:
        for v in videos:
            conn.execute("""
                INSERT OR IGNORE INTO subscription_videos
                    (video_id, title, channel_id, channel_title, category_id,
                     duration_sec, view_count, like_count, comment_count,
                     published_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                v["video_id"], v["title"], v["channel_id"], v["channel_title"],
                v["category_id"], v["duration_sec"], v["view_count"],
                v["like_count"], v["comment_count"], v["published_at"],
                v["fetched_at"],
            ))
            inserted += 1
        conn.commit()
    finally:
        conn.close()

    print(f"[ingest] {inserted} videos written to subscription_videos")
    return inserted