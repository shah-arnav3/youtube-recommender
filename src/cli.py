import argparse
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.db import init_db, get_connection
from src.index.inverted_index import InvertedIndex
from src.profile.taste_profile import build_taste_profile, build_seen_video_ids
from src.engine.lru_cache import LRUCache
from src.engine.scorer import recommend
from src.engine.feedback import log_feedback
from src.ingest.ingest import (
    ingest_watch_history,
    ingest_subscriptions,
    ingest_subscription_videos,
)
from config import LRU_CAPACITY, VALID_MOODS


def bootstrap():
    """
    Initialize the database and build all in-memory structures needed for
    recommendation and feedback commands.

    Mirrors the bootstrap() in server.py but returns the state objects as local
    variables rather than assigning them to module-level globals, since the CLI
    is a short-lived process — there's no shared state to maintain across requests.

    Returns:
        idx           — InvertedIndex populated from subscription_videos
        taste_profile — dict of affinity, category, and satisfaction signals
        seen          — set of already-watched video IDs
        cache         — empty LRUCache (won't persist between CLI invocations)
    """
    init_db()
    idx           = InvertedIndex()
    idx.build()
    taste_profile = build_taste_profile()
    seen          = build_seen_video_ids()
    cache         = LRUCache(LRU_CAPACITY)
    return idx, taste_profile, seen, cache


def cmd_recommend(args):
    """
    Run the recommendation pipeline and print results to stdout.

    Bootstraps all in-memory state, then calls recommend() with the mood and
    time budget parsed from CLI args. Results are printed in a ranked list with
    title, channel, duration, score, and watch URL for each video.

    Args:
        args.mood    — mood tag string (validated by argparse against VALID_MOODS)
        args.minutes — time budget in minutes (positive int)
    """
    idx, taste_profile, seen, cache = bootstrap()
    results, cache_hit = recommend(
        args.mood, args.minutes, idx, taste_profile, seen, cache
    )
    print(f"\n{'(cache hit) ' if cache_hit else ''}Top {len(results)} videos for '{args.mood}' under {args.minutes} min:\n")
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r['title']}")
        print(f"     {r['channel']} — {r['duration']} — score: {r['score']}")
        print(f"     {r['url']}")
        print()


def cmd_rate(args):
    """
    Record a thumbs-up or thumbs-down rating for a video and print the feedback summary.

    Converts the "y"/"n" rating string from the CLI into the 0/1 integer that
    log_feedback() expects, then calls log_feedback() which writes to the DB,
    updates channel_scores, invalidates the relevant LRU cache entry, and updates
    the in-memory taste profile's satisfaction scores.

    Prints the summary dict returned by log_feedback() as formatted JSON.

    Args:
        args.video_id — ID of the video being rated
        args.rating   — "y" (liked) or "n" (disliked)
        args.mood     — mood tag that produced this recommendation
        args.minutes  — time budget used; converted to seconds before passing through
    """
    idx, taste_profile, seen, cache = bootstrap()
    # Convert "y"/"n" to 1/0 — log_feedback expects a binary int, not a string
    rating = 1 if args.rating.lower() == "y" else 0
    summary = log_feedback(
        video_id        = args.video_id,
        rating          = rating,
        mood_tag        = args.mood,
        time_budget_sec = args.minutes * 60,
        lru_cache       = cache,
        taste_profile   = taste_profile,
    )
    print(json.dumps(summary, indent=2))


def cmd_stats(args):
    """
    Print a summary of DB row counts to stdout.

    Unlike cmd_recommend and cmd_rate, this command does not need the full
    in-memory state — it only reads raw counts from the DB, so bootstrap()
    is not called. init_db() is still called to ensure the schema exists
    before querying.

    Args:
        args — unused, included for consistent command handler signature
    """
    init_db()
    conn = get_connection()
    try:
        total_videos   = conn.execute("SELECT COUNT(*) FROM subscription_videos").fetchone()[0]
        total_channels = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        total_history  = conn.execute("SELECT COUNT(*) FROM watch_history").fetchone()[0]
        total_feedback = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    finally:
        conn.close()

    print(f"\n── Stats ─────────────────────────────")
    print(f"  Videos indexed:    {total_videos}")
    print(f"  Channels:          {total_channels}")
    print(f"  Watch history:     {total_history}")
    print(f"  Feedback logged:   {total_feedback}")


def cmd_ingest(args):
    """
    Run a full ingest from Google Takeout exports.

    Executes all three ingestion phases in order:
      1. ingest_watch_history  — parse and write watch-history.json to DB
      2. ingest_subscriptions  — parse subscriptions.csv and fetch playlist IDs
      3. ingest_subscription_videos — fetch recent video metadata for all channels

    This is the initial setup command — intended to be run once when first
    populating the DB from a fresh Takeout export. For incremental updates to
    subscription videos only, use cmd_refresh instead.

    Args:
        args.history       — path to watch-history.json
        args.subscriptions — path to subscriptions.csv
    """
    init_db()
    ingest_watch_history(args.history)
    ingest_subscriptions(args.subscriptions)
    ingest_subscription_videos()


def cmd_refresh(args):
    """
    Re-fetch recent uploads from subscribed channels and update the DB.

    Runs only ingest_subscription_videos() — does not re-parse watch history
    or subscriptions, since those require a new Takeout export. Use this command
    to pick up new videos from channels you're already subscribed to.

    The in-memory index and taste profile are not rebuilt here, unlike
    refresh_endpoint() in server.py. Since the CLI is a short-lived process,
    the next invocation of cmd_recommend will call bootstrap() and pick up
    the updated DB automatically.

    Args:
        args — unused, included for consistent command handler signature
    """
    init_db()
    ingest_subscription_videos()
    print("[cli] Refresh complete")


def main():
    """
    Entry point — parse CLI arguments and dispatch to the appropriate command handler.

    Subcommands:
        recommend  --mood MOOD --minutes N
            Run the recommendation pipeline and print ranked results.

        rate  --video-id ID --rating y|n --mood MOOD --minutes N
            Record feedback for a video from a previous recommendation session.

        stats
            Print DB row counts.

        ingest  --subscriptions PATH --history PATH
            Run a full ingest from Google Takeout exports.

        refresh
            Re-fetch recent uploads from subscribed channels.

    If no subcommand is provided, prints the help message and exits.
    """
    parser = argparse.ArgumentParser(prog="cli", description="YouTube Meal-Time Recommender")
    sub    = parser.add_subparsers(dest="command")

    # recommend: requires a mood (validated against VALID_MOODS) and time budget
    p_rec = sub.add_parser("recommend")
    p_rec.add_argument("--minutes", type=int, required=True)
    p_rec.add_argument("--mood",    type=str, required=True, choices=VALID_MOODS)

    # rate: requires the video ID, a y/n rating, the mood context, and the time budget
    # --minutes is included so log_feedback() can record the session's time budget
    p_rate = sub.add_parser("rate")
    p_rate.add_argument("--video-id", dest="video_id", required=True)
    p_rate.add_argument("--rating",   required=True, choices=["y", "n"])
    p_rate.add_argument("--mood",     required=True, choices=VALID_MOODS)
    p_rate.add_argument("--minutes",  type=int, required=True)

    # stats: no arguments needed
    sub.add_parser("stats")

    # ingest: paths to both Takeout export files
    p_ingest = sub.add_parser("ingest")
    p_ingest.add_argument("--subscriptions", required=True)
    p_ingest.add_argument("--history",       required=True)

    # refresh: no arguments needed — operates on already-ingested subscriptions
    sub.add_parser("refresh")

    args = parser.parse_args()

    if args.command == "recommend":  cmd_recommend(args)
    elif args.command == "rate":     cmd_rate(args)
    elif args.command == "stats":    cmd_stats(args)
    elif args.command == "ingest":   cmd_ingest(args)
    elif args.command == "refresh":  cmd_refresh(args)
    else:
        # No subcommand provided — print usage and exit
        parser.print_help()


if __name__ == "__main__":
    main()