import time
import random
import urllib.request
import urllib.parse
import urllib.error
import json
import re
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import (
    YOUTUBE_API_KEY,
    YOUTUBE_API_BASE,
    MAX_UPLOADS_PER_CHANNEL,
    API_BATCH_SIZE,
    MAX_RETRIES,
    BACKOFF_BASE,
    BACKOFF_MAX,
)


def api_get(endpoint: str, params: dict) -> dict:
    """
        Generic HTTP GET wrapper for the YouTube Data API v3.

        Builds the full request URL by appending the API key and URL-encoding all
        query parameters, then attempts the request up to MAX_RETRIES times.

        Retry behavior:
          - 403 / 429 (quota exceeded or rate-limited): backs off exponentially
            with jitter (random 0–1s added) to spread out retries and avoid
            thundering-herd problems. Delay is clamped to BACKOFF_MAX.
          - 404: non-recoverable; raises immediately since retrying won't help.
          - Other HTTP errors: also non-recoverable; raises immediately.
          - Network/timeout errors: treated like rate limits — backs off and retries,
            since these are transient and may resolve on their own.

        Args:
            endpoint: YouTube API endpoint name, e.g. "videos", "channels", "playlistItems"
            params:   Query parameters as a dict (API key is injected automatically)

        Returns:
            Parsed JSON response as a Python dict.

        Raises:
            RuntimeError: if all retries are exhausted, or a non-retryable HTTP error occurs.
    """
    # Inject the API key into every request automatically so callers don't have to
    params["key"] = YOUTUBE_API_KEY
    url = f"{YOUTUBE_API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"

    for attempt in range(MAX_RETRIES):
        try:
            # Read and decode the full response body, then parse as JSON
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                # Quota exceeded or rate-limited, wait and retry.
                delay = min(BACKOFF_BASE ** attempt + random.uniform(0, 1), BACKOFF_MAX)
                print(f"[fetcher] HTTP {e.code} — backing off {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(delay)
            elif e.code == 404:
                # Resource not found, retrying won't change this, so fail fast
                raise RuntimeError(f"YouTube API 404 for {url}") from e
            else:
                # Any other HTTP error (also non-retryable)
                raise RuntimeError(f"YouTube API HTTP {e.code} for {url}") from e

        except (urllib.error.URLError, TimeoutError) as e:
            # Network-level failure
            delay = min(BACKOFF_BASE ** attempt + random.uniform(0, 1), BACKOFF_MAX)
            print(f"[fetcher] Network error ({e}) — backing off {delay:.1f}s")
            time.sleep(delay)

    # If we've exhausted all retry attempts without a successful response, give up
    raise RuntimeError(f"[fetcher] Exhausted {MAX_RETRIES} retries for {url}")


def parse_duration(iso_duration: str) -> int:
    """
        Convert a YouTube ISO 8601 duration string into a total number of seconds.

        YouTube returns durations in the format "PT#H#M#S", e.g.:
          "PT1H45M30S": 1 hour, 45 minutes, 30 seconds (6330 seconds)

        The "P" prefix stands for "Period"; "T" separates the date and time parts.

        Args:
            iso_duration: ISO 8601 duration string from the YouTube API.

        Returns:
            Total duration in seconds as an int. Returns 0 if the string is empty
            or doesn't match the expected pattern (e.g. live streams return "P0D").
    """

    if not iso_duration:
        return 0

    # Regex captures optional days, hours, minutes, and seconds individually.
    pattern = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
    m = pattern.match(iso_duration)
    if not m:
        return 0

    # Default missing groups to 0 using "or 0" before casting to int
    days    = int(m.group(1) or 0)
    hours   = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def safe_int(value) -> int:
    """
        Safely cast a value to int, returning 0 on any failure.

        YouTube stat fields (viewCount, likeCount, etc.) are returned as strings,
        and may be missing entirely if the uploader has disabled them. This helper
        centralizes that defensive conversion so callers stay clean.

        Args:
            value: Any value — expected to be a numeric string, but could be None
                   or any other unexpected type.

        Returns:
            Integer representation of value, or 0 if conversion fails for any reason.
    """
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def get_uploads_playlist_ids(channel_ids: list[str]) -> dict[str, str]:
    """
        Fetch the uploads playlist ID for each given channel.

        YouTube stores every channel's public videos as a hidden playlist whose ID
        starts with "UU" (vs. "UC" for channel IDs). To list a channel's videos,
        you must first retrieve this playlist ID, then query playlistItems.

        Channels are batched in groups of API_BATCH_SIZE (max 50) to minimize the
        number of API calls, since the channels.list endpoint accepts a comma-separated
        list of IDs.

        Args:
            channel_ids: List of YouTube channel IDs (strings starting with "UC").

        Returns:
            Dict mapping each channel ID → its uploads playlist ID.
            Channels that the API doesn't return (private, deleted, etc.) are omitted.
     """
    result = {}

    # Process channels in batches to stay within API limits
    for i in range(0, len(channel_ids), API_BATCH_SIZE):
        batch = channel_ids[i : i + API_BATCH_SIZE]
        params = {
            "part": "contentDetails", # We only need contentDetails
            "id":   ",".join(batch), # Comma-separated list of channel IDs
            "maxResults": API_BATCH_SIZE,
        }
        data = api_get("channels", params)

        # Extract the uploads playlist ID from each returned channel item
        for item in data.get("items", []):
            cid = item.get("id")
            playlist_id = (
                item.get("contentDetails", {})
                    .get("relatedPlaylists", {})
                    .get("uploads") # "uploads" is the key for the uploads playlist
            )
            if cid and playlist_id:
                result[cid] = playlist_id

    return result


def get_recent_uploads(playlist_id: str) -> list[str]:
    """
        Fetch the most recent video IDs from an uploads playlist.

        Queries the playlistItems endpoint for a single page of results, up to
        MAX_UPLOADS_PER_CHANNEL items (capped at 50, the API maximum per request).
        Only the first page is fetched — pagination is intentionally skipped here
        since we only want recent content, not a channel's full history.

        Args:
            playlist_id: The uploads playlist ID for a channel (starts with "UU").

        Returns:
            List of video ID strings, in the order returned by the API (newest first).
            Returns an empty list if the playlist is empty or an API error occurs.
    """
    params = {
        "part":       "snippet",
        "playlistId": playlist_id,
        # Clamp to 50 since the API rejects maxResults > 50 for playlistItems
        "maxResults": min(MAX_UPLOADS_PER_CHANNEL, 50),
    }
    try:
        data = api_get("playlistItems", params)
    except RuntimeError:
        # If the API call fails (e.g. playlist is private), return empty rather than crashing
        # the caller will log this channel as failed
        return []

    # Dig into the nested snippet, resourceId, videoId path for each item
    video_ids = []
    for item in data.get("items", []):
        vid_id = (
            item.get("snippet", {})
                .get("resourceId", {})
                .get("videoId")
        )
        if vid_id:
            video_ids.append(vid_id)
    return video_ids


def get_video_metadata(video_ids: list[str]) -> list[dict]:
    """
        Fetch full metadata for a list of video IDs.

        Queries the videos endpoint in batches of API_BATCH_SIZE, requesting three
        resource parts: snippet (title, channel, category), contentDetails (duration),
        and statistics (views, likes, comments).

        Videos are filtered out if they are missing a duration or category ID, since
        those fields are required for downstream analysis. This excludes live streams
        in progress (no duration) and certain special video types.

        Args:
            video_ids: List of YouTube video ID strings.

        Returns:
            List of dicts, one per valid video, with the following keys:
                video_id, title, channel_id, channel_title, category_id,
                duration_sec, view_count, like_count, comment_count,
                published_at (datetime | None), fetched_at (datetime)
    """
    results = []

    # Capture a single "now" timestamp for all videos in this batch run,
    # so fetched_at is consistent and doesn't drift across batches
    now = datetime.now(timezone.utc)

    for i in range(0, len(video_ids), API_BATCH_SIZE):
        batch = video_ids[i : i + API_BATCH_SIZE]
        params = {
            # Request all three parts needed for our metadata schema
            "part": "snippet,contentDetails,statistics",
            "id":   ",".join(batch),
        }
        data = api_get("videos", params)

        for item in data.get("items", []):
            video_id = item.get("id")
            snippet  = item.get("snippet", {})
            content  = item.get("contentDetails", {})
            stats    = item.get("statistics", {})

            # Convert the ISO 8601 duration string to integer seconds
            duration_sec = parse_duration(content.get("duration", ""))
            category_id  = snippet.get("categoryId")

            # Skip videos that lack a duration (live streams) or category (edge cases)
            # as these would produce incomplete/misleading rows downstream
            if not duration_sec or not category_id:
                continue

            # Parse the publish timestamp; fall back to None rather than crashing on malformed dates,
            # since this field is non-critical for most analysis
            published_at = None
            raw_pub = snippet.get("publishedAt", "")
            if raw_pub:
                try:
                    published_at = datetime.fromisoformat(
                        raw_pub.rstrip("Z")
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass

            results.append({
                "video_id":      video_id,
                "title":         snippet.get("title", ""),
                "channel_id":    snippet.get("channelId", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "category_id":   int(category_id),
                "duration_sec":  duration_sec,
                "view_count":    safe_int(stats.get("viewCount")),
                "like_count":    safe_int(stats.get("likeCount")),
                "comment_count": safe_int(stats.get("commentCount")),
                "published_at":  published_at,
                "fetched_at":    now,
            })

    return results