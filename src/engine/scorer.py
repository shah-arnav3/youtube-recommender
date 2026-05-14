import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import (
    W1_CHANNEL_AFFINITY,
    W2_CHANNEL_SATISFACTION,
    W3_CATEGORY_WEIGHT,
    W4_LIKE_RATIO,
    TOP_K,
    MAX_PER_CHANNEL,
)
from src.engine.heap import heap_push, heap_pop


def compute_score(video: dict, taste_profile: dict) -> tuple[float, dict]:
    """
    Compute a composite taste score for a single video, returning both the
    final score and the raw component values used to produce it.

    All components are normalised to 0.0–1.0 before weighting. The weighted
    sum is the score; the raw components are returned separately so callers
    can surface an explanation alongside the result.

    Components:
        channel_affinity     — recency-decayed watch frequency for this channel,
                               normalized 0–1. Defaults to 0.0 for untracked channels.
        channel_satisfaction — explicit y/n feedback rate for this channel.
                               Defaults to 0.5 (neutral) so unrated channels aren't
                               penalised relative to rated ones.
        category_weight      — fraction of watch history in this video's category.
                               Defaults to 0.0 for unseen categories.
        like_ratio           — like_count / (view_count + 1). Objective quality
                               signal; +1 avoids division by zero.

    Args:
        video:        Metadata dict from the video store.
        taste_profile: The in-memory taste profile built at startup.

    Returns:
        Tuple of (score, components) where:
            score:      Weighted composite float, rounded to 6 decimal places.
            components: Dict of the four raw component values before weighting.
    """
    channel_id  = video["channel_id"]
    category_id = video["category_id"]
    like_count  = video["like_count"] or 0
    view_count  = video["view_count"] or 0

    components = {
        "channel_affinity":     taste_profile["channel_affinity"].get(channel_id, 0.0),
        "channel_satisfaction": taste_profile["channel_satisfaction"].get(channel_id, 0.5),
        "category_weight":      taste_profile["category_weights"].get(category_id, 0.0),
        "like_ratio":           like_count / (view_count + 1),
    }

    score = (
        W1_CHANNEL_AFFINITY     * components["channel_affinity"]
      + W2_CHANNEL_SATISFACTION * components["channel_satisfaction"]
      + W3_CATEGORY_WEIGHT      * components["category_weight"]
      + W4_LIKE_RATIO           * components["like_ratio"]
    )

    return round(score, 6), components


def _top_reason(components: dict) -> str:
    """
    Return a human-readable label for the highest-contributing score component.

    Multiplies each raw component value by its weight to find which term
    contributed most to the final score, then maps it to a readable label.
    Used to populate the "why" field in formatted results.

    Args:
        components: The raw component dict returned by compute_score().

    Returns:
        A short string label, e.g. "channel affinity" or "satisfaction history".
    """
    weighted = {
        "channel affinity":     W1_CHANNEL_AFFINITY     * components["channel_affinity"],
        "satisfaction history": W2_CHANNEL_SATISFACTION * components["channel_satisfaction"],
        "category match":       W3_CATEGORY_WEIGHT      * components["category_weight"],
        "like ratio":           W4_LIKE_RATIO           * components["like_ratio"],
    }
    return max(weighted, key=lambda k: weighted[k])


def format_result(video: dict, score: float, components: dict) -> dict:
    """
    Format a video metadata dict and its score into the API response shape.

    Includes a "why" field (the single highest-contributing score component)
    and a "score_breakdown" dict (all four weighted contributions) so callers
    can understand and communicate why a video ranked where it did.

    Args:
        video:      Metadata dict from the video store.
        score:      Composite score from compute_score().
        components: Raw component dict from compute_score().

    Returns:
        Dict with keys: video_id, title, channel, duration, category_id,
        published_at, score, url, why, score_breakdown.
    """
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
        # Human-readable label for the single largest contributor to this score
        "why":          _top_reason(components),
        # All four weighted contributions, rounded for readability
        "score_breakdown": {k: round(v, 3) for k, v in components.items()},
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
    Step 7b: Per-channel diversity cap (MAX_PER_CHANNEL from config)
    Step 8: Store in LRU cache
    """
    key             = (mood, time_budget_minutes)
    time_budget_sec = time_budget_minutes * 60

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

    # Steps 5+6 — score each candidate and maintain a min-heap of the top K.
    # heap_push evicts the lowest-scoring entry whenever the heap exceeds capacity,
    # so after this loop the heap contains exactly the top-K scoring video IDs.
    heap = []
    for vid_id in candidates:
        score, _ = compute_score(video_store[vid_id], taste_profile)
        heap_push(heap, (score, vid_id), capacity=TOP_K)

    # Step 7 — extract in descending order.
    # heap_pop returns the minimum each time, so we collect in ascending order
    # then reverse. Components are re-computed here rather than stored on the heap
    # to keep heap entries as lightweight (score, vid_id) tuples.
    raw_results = []
    video_ids_in_result = set()
    while heap:
        score, vid_id = heap_pop(heap)
        _, components = compute_score(video_store[vid_id], taste_profile)
        raw_results.append(format_result(video_store[vid_id], score, components))
        video_ids_in_result.add(vid_id)
    raw_results.reverse()

    # Step 7b — per-channel diversity cap.
    # The heap selects purely by score, which can result in several videos from
    # the same high-affinity channel dominating the results. This pass enforces
    # MAX_PER_CHANNEL, preserving score order within each channel's allocation.
    channel_counts: dict[str, int] = {}
    results = []
    for r in raw_results:
        ch = r["channel"]
        if channel_counts.get(ch, 0) < MAX_PER_CHANNEL:
            results.append(r)
            channel_counts[ch] = channel_counts.get(ch, 0) + 1

    # Step 8 — store in cache
    lru_cache.put(key, results, video_ids_in_result)

    return results, False