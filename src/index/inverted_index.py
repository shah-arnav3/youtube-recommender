import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from src.db import get_connection
from config import MOOD_CATEGORY_MAP  # Maps mood tags (e.g. "chill") to lists of YouTube category IDs


class InvertedIndex:
    """
    An in-memory inverted index over subscription_videos, keyed by category.

    Stores two parallel data structures built from the same DB rows:
      - index:       category_id to [video_ids], for fast candidate lookup by mood
      - video_store: video_id to metadata dict, for O(1) access during scoring

    The index is built once at startup by calling build(), then queried repeatedly
    by get_candidates() for each recommendation request. No DB access happens
    after build() completes.
    """

    def __init__(self):
        # Maps each category_id to the list of video_ids in that category.
        self.index: dict[int, list[str]] = {}

        # Maps each video_id to its full metadata dict.
        # complete data in O(1) given just its ID, without a second DB lookup.
        self.video_store: dict[str, dict] = {}

    def build(self) -> None:
        """
        Load all videos from subscription_videos and populate both data structures.

        Fetches every row from subscription_videos in a single query, then iterates
        once over the results to build both the index and video_store simultaneously.
        This is intentionally a one-pass build — iterating twice over the same rows
        to build each structure separately would double the work for no benefit.

        Should be called exactly once at server startup, before any calls to
        get_candidates(). Calling build() again would re-populate both structures
        from scratch, discarding any existing state.
        """
        conn = get_connection()
        try:
            rows = conn.execute("""
                SELECT video_id, title, channel_id, channel_title,
                       category_id, duration_sec, view_count,
                       like_count, comment_count, published_at
                FROM subscription_videos
            """).fetchall()
        finally:
            conn.close()

        for row in rows:
            video_id    = row["video_id"]
            category_id = row["category_id"]

            # Store the full metadata dict keyed by video_id for O(1) access during scoring
            # avoids a DB round-trip per candidate at query time
            self.video_store[video_id] = {
                "video_id":      video_id,
                "title":         row["title"],
                "channel_id":    row["channel_id"],
                "channel_title": row["channel_title"],
                "category_id":   category_id,
                "duration_sec":  row["duration_sec"],
                "view_count":    row["view_count"],
                "like_count":    row["like_count"],
                "comment_count": row["comment_count"],
                "published_at":  row["published_at"],
            }

            # Append this video's ID to its category's list in the index.
            # setdefault avoids the explicit `if cat_id not in self.index` check
            self.index.setdefault(category_id, []).append(video_id)

        print(f"[index] Built inverted index:")
        print(f"  {len(self.video_store)} videos indexed")
        print(f"  {len(self.index)} categories indexed")

    def get_candidates(self, mood: str) -> list[str]:
        """
        Return all video IDs that are candidates for a given mood.

        Looks up the category IDs associated with the mood in MOOD_CATEGORY_MAP,
        then unions the video ID lists for each of those categories from the index.
        A set is used for the union to deduplicate video IDs that appear under
        multiple categories.

        The "random" mood is a special case — it bypasses the index entirely and
        returns all indexed video IDs, since no category filtering is appropriate.
        It is represented in MOOD_CATEGORY_MAP as a missing key (returns None)
        rather than an explicit entry, so None is the signal for random mode.

        Args:
            mood: A mood tag string, e.g. "chill", "educational", "random".

        Returns:
            Deduplicated list of video ID strings matching the mood's categories.
            For mood="random", returns all video IDs in the index.
            Returns an empty list if the mood maps to categories with no indexed videos.
        """
        category_ids = MOOD_CATEGORY_MAP.get(mood)

        if category_ids is None:
            # "random" mood — no category filter, return everything
            return list(self.video_store.keys())

        # Union video IDs across all categories for this mood.
        # self.index.get(cat_id, []) handles categories that have no indexed videos gracefully,
        # rather than raising a KeyError.
        candidates = set()
        for cat_id in category_ids:
            for video_id in self.index.get(cat_id, []):
                candidates.add(video_id)

        return list(candidates)