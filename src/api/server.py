import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from flask import Flask, request, jsonify
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

app = Flask(__name__)

# In-memory state — built once at startup
# refresh_endpoint() is the only other place that mutates these after startup.
idx           = None   # InvertedIndex: category to video ID lookup
taste_profile = None   # Dict of affinity, category, and satisfaction signals
seen          = None   # Set of already-watched video IDs for candidate filtering
cache         = None   # LRUCache: memorizes recommend() results by (mood, budget)


def bootstrap():
    """
    Initialize the database and build all in-memory structures.

    Called once before the server starts accepting requests. Establishes the DB
    schema (no-op if already exists), then populates the four global state objects
    that all endpoints depend on. Order matters: init_db() must run before any
    DB reads, and the index/profile/seen structures must exist before the first
    request reaches recommend().
    """
    global idx, taste_profile, seen, cache

    init_db()

    idx = InvertedIndex()
    idx.build()

    taste_profile = build_taste_profile()
    seen          = build_seen_video_ids()
    cache         = LRUCache(LRU_CAPACITY)

    print("[server] Bootstrap complete — ready to serve requests")


# Endpoints

@app.route("/recommend", methods=["POST"])
def recommend_endpoint():
    """
    Return a ranked list of video recommendations for a given mood and time budget.

    Expected JSON body:
        mood                (str) — one of VALID_MOODS
        time_budget_minutes (int) — positive integer, minutes available to watch

    The pipeline runs as follows:
      1. Retrieve all candidate video IDs for the mood from the inverted index.
      2. Pass candidates through the scorer, which filters unseen/duration-fitting
         videos and ranks them using the taste profile.
      3. Check the LRU cache first — if this (mood, budget) pair was recently
         requested, return the cached result directly.

    The endpoint calls get_candidates() twice: once before the pipeline to report
    the raw candidate count, and once after to compute the post-filter count for
    the diagnostics payload. This is a minor redundancy kept for response clarity.

    Returns JSON:
        results                  — ranked list of recommended video dicts
        cache_hit                — whether the result was served from cache
        candidates_before_filter — number of videos matching the mood's categories
        candidates_after_filter  — number remaining after unseen + duration filters
        query_time_ms            — total wall-clock time for the pipeline in ms
    """
    body = request.get_json(silent=True) or {}

    mood                = body.get("mood")
    time_budget_minutes = body.get("time_budget_minutes")

    # Validate both fields before running the pipeline — return 400 early
    if mood not in VALID_MOODS:
        return jsonify({"error": f"mood must be one of {VALID_MOODS}"}), 400
    if not isinstance(time_budget_minutes, int) or time_budget_minutes <= 0:
        return jsonify({"error": "time_budget_minutes must be a positive integer"}), 400

    t0 = time.time()

    candidates_before = len(idx.get_candidates(mood))
    results, cache_hit = recommend(
        mood, time_budget_minutes, idx, taste_profile, seen, cache
    )

    query_time_ms = round((time.time() - t0) * 1000, 2)

    # Recompute post-filter candidate count for the diagnostics payload.
    time_budget_sec   = time_budget_minutes * 60
    candidates_raw    = idx.get_candidates(mood)
    candidates_unseen = [v for v in candidates_raw if v not in seen]
    candidates_after  = len([
        v for v in candidates_unseen
        if idx.video_store[v]["duration_sec"] <= time_budget_sec
    ])

    return jsonify({
        "results":                  results,
        "cache_hit":                cache_hit,
        "candidates_before_filter": candidates_before,
        "candidates_after_filter":  candidates_after,
        "query_time_ms":            query_time_ms,
    })


@app.route("/feedback", methods=["POST"])
def feedback_endpoint():
    """
    Record a thumbs-up (1) or thumbs-down (0) rating for a recommended video.

    Expected JSON body:
        video_id        (str)      — ID of the video being rated
        rating          (int)      — 0 (disliked) or 1 (liked)
        mood_tag        (str)      — mood that produced this recommendation
        time_budget_sec (int|None) — time budget used for the recommendation session

    Passes validated fields to log_feedback(), which writes to the feedback table,
    updates channel_scores, invalidates the relevant LRU cache entry, and updates
    the in-memory taste profile's satisfaction scores.

    Returns the summary dict produced by log_feedback() directly as JSON.
    """
    body = request.get_json(silent=True) or {}

    video_id        = body.get("video_id")
    rating          = body.get("rating")
    mood_tag        = body.get("mood_tag")
    time_budget_sec = body.get("time_budget_sec")

    if not video_id:
        return jsonify({"error": "video_id required"}), 400
    if rating not in (0, 1):
        return jsonify({"error": "rating must be 0 or 1"}), 400
    if mood_tag not in VALID_MOODS:
        return jsonify({"error": f"mood_tag must be one of {VALID_MOODS}"}), 400

    summary = log_feedback(
        video_id        = video_id,
        rating          = rating,
        mood_tag        = mood_tag,
        time_budget_sec = time_budget_sec,
        lru_cache       = cache,
        taste_profile   = taste_profile,
    )
    return jsonify(summary)


@app.route("/stats", methods=["GET"])
def stats_endpoint():
    """
    Return a snapshot of system state for monitoring and debugging.

    Combines live DB counts with in-memory cache and taste profile metrics.
    DB queries run in a single connection opened for this request; in-memory
    metrics are read directly from the global state objects.

    Returns JSON with:
        total_videos_indexed        — rows in subscription_videos
        total_channels              — rows in subscriptions
        total_watch_history         — rows in watch_history
        total_feedback              — rows in feedback
        cache_hit_rate              — hits / (hits + misses) since last bootstrap
        cache_hits / cache_misses   — raw counts
        top_channels_by_satisfaction — top 5 channels by satisfaction_rate (min 1 rating)
        top_channels_by_affinity     — top 5 channels by affinity score from taste profile
        rating_distribution          — count of 0 and 1 ratings across all feedback
    """
    conn = get_connection()
    try:
        total_videos   = conn.execute("SELECT COUNT(*) FROM subscription_videos").fetchone()[0]
        total_channels = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
        total_history  = conn.execute("SELECT COUNT(*) FROM watch_history").fetchone()[0]
        total_feedback = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]

        # Only include channels with at least one rating to avoid surfacing channels
        # with a default or unearned satisfaction score
        top_satisfaction = conn.execute("""
            SELECT channel_title, satisfaction_rate, total_ratings
            FROM channel_scores
            WHERE total_ratings > 0
            ORDER BY satisfaction_rate DESC
            LIMIT 5
        """).fetchall()

        rating_dist = conn.execute("""
            SELECT rating, COUNT(*) as count
            FROM feedback
            GROUP BY rating
        """).fetchall()

    finally:
        conn.close()

    # Sort the in-memory affinity dict rather than querying the DB, since
    # channel_affinity is already computed and available in the taste profile
    top_affinity = sorted(
        taste_profile["channel_affinity"].items(),
        key=lambda x: x[1],
        reverse=True
    )[:5]

    return jsonify({
        "total_videos_indexed":  total_videos,
        "total_channels":        total_channels,
        "total_watch_history":   total_history,
        "total_feedback":        total_feedback,
        "cache_hit_rate":        cache.hit_rate(),
        "cache_hits":            cache.hits,
        "cache_misses":          cache.misses,
        "top_channels_by_satisfaction": [
            {
                "channel":           row["channel_title"],
                "satisfaction_rate": row["satisfaction_rate"],
                "total_ratings":     row["total_ratings"],
            }
            for row in top_satisfaction
        ],
        "top_channels_by_affinity": [
            {"channel_id": cid, "affinity": score}
            for cid, score in top_affinity
        ],
        "rating_distribution": {
            # Cast rating to str since JSON object keys must be strings
            str(row["rating"]): row["count"]
            for row in rating_dist
        },
    })


@app.route("/refresh", methods=["POST"])
def refresh_endpoint():
    """
    Re-ingest new subscription videos, then rebuild all in-memory state.

    Pulls any new videos from subscribed channels via the YouTube API, rebuilds
    the inverted index and taste profile from the updated DB, resets the seen set,
    and clears the LRU cache. The cache is cleared because cached recommendations
    may now be stale — new videos could score higher than what was previously cached
    for a given (mood, budget) pair.

    Note: this only runs ingest_subscription_videos(), not a full re-ingest of
    watch history or subscriptions. Those require new Takeout exports and are
    expected to be run manually via the ingest pipeline.

    Returns JSON:
        status         — "refreshed"
        videos_indexed — total videos in the index after rebuild
        elapsed_sec    — wall-clock time for the full refresh in seconds
    """
    global idx, taste_profile, seen, cache

    t0 = time.time()

    ingest_subscription_videos()

    idx = InvertedIndex()
    idx.build()

    taste_profile = build_taste_profile()
    seen          = build_seen_video_ids()
    cache         = LRUCache(LRU_CAPACITY)

    elapsed = round(time.time() - t0, 2)

    return jsonify({
        "status":         "refreshed",
        "videos_indexed": len(idx.video_store),
        "elapsed_sec":    elapsed,
    })


if __name__ == "__main__":
    bootstrap()
    from config import FLASK_HOST, FLASK_PORT, FLASK_DEBUG
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=FLASK_DEBUG)