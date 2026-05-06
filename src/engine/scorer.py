import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import (
    W1_CHANNEL_AFFINITY,
    W2_CHANNEL_SATISFACTION,
    W3_CATEGORY_WEIGHT,
    W4_LIKE_RATIO,
    TOP_K,
)
from src.engine.heap import heap_push, heap_pop


def compute_score(video: dict, taste_profile: dict) -> float:
    """
    Compute a composite taste score for a single video.
    All components are normalised to 0.0-1.0 before weighting.
    """
    channel_id  = video["channel_id"]
    category_id = video["category_id"]
    like_count  = video["like_count"]  or 0
    view_count  = video["view_count"]  or 0

    channel_affinity     = taste_profile["channel_affinity"].get(channel_id, 0.0)
    channel_satisfaction = taste_profile["channel_satisfaction"].get(channel_id, 0.5)
    category_weight      = taste_profile["category_weights"].get(category_id, 0.0)
    like_ratio           = like_count / (view_count + 1)

    score = (
        W1_CHANNEL_AFFINITY     * channel_affinity
      + W2_CHANNEL_SATISFACTION * channel_satisfaction
      + W3_CATEGORY_WEIGHT      * category_weight
      + W4_LIKE_RATIO           * like_ratio
    )

    return round(score, 6)


def format_result(video: dict, score: float) -> dict:
    """Format a video metadata dict into the API response shape."""
    duration_sec = video["duration_sec"]
    minutes      = duration_sec // 60
    seconds      = duration_sec % 60

    return {
        "video_id":     video["video_id"],
        "title":        video["title"],
        "channel":      video["channel_title"],
        "duration":     f"{minutes}:{seconds:02d}",
        "category_id":  video["category_id"],
        "published_at": str(video["published_at"])[:10] if video["published_at"] else None,
        "score":        score,
        "url":          f"https://youtube.com/watch?v={video['video_id']}",
    }


def recommend(
    mood: str,
    time_budget_minutes: int,
    inverted_index,
    taste_profile: dict,
    seen_video_ids: set,
    lru_cache,
) -> tuple[list[dict], bool]:
    """
    Full query pipeline. Returns (results, cache_hit).

    Step 1: LRU cache check
    Step 2: Inverted index lookup
    Step 3: Unseen filter
    Step 4: Duration hard filter
    Step 5+6: Score each candidate, maintain top-K with min-heap
    Step 7: Extract results in descending order
    Step 8: Store in LRU cache
    """
    key              = (mood, time_budget_minutes)
    time_budget_sec  = time_budget_minutes * 60

    # Step 1 — cache check
    cached = lru_cache.get(key)
    if cached is not None:
        return cached, True

    # Step 2 — inverted index lookup
    candidates = inverted_index.get_candidates(mood)

    # Step 3 — unseen filter
    candidates = [v for v in candidates if v not in seen_video_ids]

    # Step 4 — duration hard filter
    video_store = inverted_index.video_store
    candidates  = [
        v for v in candidates
        if video_store[v]["duration_sec"] <= time_budget_sec
    ]

    # Steps 5+6 — score and heap
    heap             = []
    video_ids_in_result = set()

    for vid_id in candidates:
        score = compute_score(video_store[vid_id], taste_profile)
        heap_push(heap, (score, vid_id), capacity=TOP_K)

    # Step 7 — extract in descending order
    results = []
    while heap:
        score, vid_id = heap_pop(heap)
        results.append(format_result(video_store[vid_id], score))
        video_ids_in_result.add(vid_id)
    results.reverse()

    # Step 8 — store in cache
    lru_cache.put(key, results, video_ids_in_result)

    return results, False