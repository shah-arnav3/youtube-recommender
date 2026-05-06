import csv
import json
import re
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def extract_video_id(url: str) -> str | None:
    """
        Extract an 11-character YouTube video ID from a URL.

        YouTube video URLs appear in two common forms in Takeout data:
          - "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
          - "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=..."

        The regex searches for "?v=" or "&v=" and captures the 11-character ID
        that follows. YouTube video IDs are always exactly 11 characters and may
        contain letters, digits, underscores, and hyphens.

        Args:
            url: A URL string, potentially containing a YouTube video ID.

        Returns:
            The 11-character video ID string, or None if no match is found.
    """
    match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    return match.group(1) if match else None


def extract_channel_id(channel_url: str) -> str | None:
    """
        Extract a YouTube channel ID from a /channel/ URL.

        YouTube channel IDs always start with "UC" and appear in URLs as:
          "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx"

        Args:
            channel_url: A YouTube channel URL string.

        Returns:
            The channel ID string (starting with "UC"), or None if not found.
        """
    match = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", channel_url)
    return match.group(1) if match else None


def parse_timestamp(ts: str) -> datetime | None:
    """
        Parse an ISO 8601 timestamp string into a timezone-aware datetime.

        Google Takeout timestamps are formatted as RFC 3339 strings ending in "Z",
        e.g. "2024-03-15T14:22:05Z". Python's fromisoformat() doesn't accept the
        "Z" suffix directly (it requires "+00:00"), so we strip it and then
        manually attach UTC timezone info.

        Args:
            ts: ISO 8601 timestamp string, possibly ending in "Z".

        Returns:
            A timezone-aware datetime in UTC, or None if the string is empty or
            cannot be parsed.
    """
    if not ts:
        return None
    try:
        # Strip "Z" suffix, parse as naive datetime, then explicitly mark as UTC
        return datetime.fromisoformat(ts.rstrip("Z")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_subscriptions(csv_path: str) -> list[dict]:
    """
        Parse a Google Takeout subscriptions CSV into a list of channel dicts.

        The Takeout subscriptions export is a CSV file with columns:
        "Channel ID", "Channel URL", "Channel title"

        Most rows include a Channel ID directly, but some (especially older
        subscriptions) may have an empty Channel ID field while still including a
        /channel/ URL. In that case, we extract the ID from the URL as a fallback.
        Channels where we can't resolve any ID are skipped and counted.

        Args:
         csv_path: Absolute or relative path to the subscriptions.csv file.

        Returns:
         List of dicts with keys: "channel_id", "channel_title", "channel_url".
         Rows with no resolvable channel ID are excluded.

        Raises:
         FileNotFoundError: If the CSV file does not exist at csv_path.
     """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Not found: {csv_path}")

    subscriptions = []
    skipped = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f) # Reads the header row automatically as column names
        for row in reader:
            # Strip whitespace to handle any trailing spaces in the CSV
            channel_id    = (row.get("Channel ID") or "").strip()
            channel_title = (row.get("Channel title") or "").strip()
            channel_url   = (row.get("Channel URL") or "").strip()

            # Fallback: if Channel ID column is empty, try to extract it from the URL
            if not channel_id and channel_url:
                channel_id = extract_channel_id(channel_url) or ""

            # If we still have no channel ID, this row is unusable — log and skip
            if not channel_id:
                print(f"[parser] WARNING: skipping '{channel_title}' — no resolvable channel ID")
                skipped += 1
                continue

            subscriptions.append({
                "channel_id":    channel_id,
                "channel_title": channel_title,
                "channel_url":   channel_url,
            })

    print(f"[parser] {len(subscriptions)} subscriptions parsed ({skipped} skipped)")
    return subscriptions


def parse_watch_history(json_path: str) -> list[dict]:
    """
        Parse a Google Takeout watch-history.json into a list of watch event dicts.

        The Takeout watch history export is a JSON array where each element
        represents one activity event across all Google products (YouTube, Search,
        etc.). Each item has a "header" field identifying the product, so we filter
        to only "YouTube" entries.

        Each YouTube entry looks like:
        {
          "header": "YouTube",
          "title": "Watched Some Video",
          "titleUrl": "https://www.youtube.com/watch?v=XXXXXXXXXXX",
          "subtitles": [{"name": "Channel Name", "url": "https://...channel/UC..."}],
          "time": "2024-03-15T14:22:05Z"
        }

        "Watched from Google Ads" entries and deleted videos may lack a titleUrl
        or have a malformed URL — these are skipped. The subtitles array may be
        absent or empty for deleted channels, in which case channel info defaults
        to empty strings.

        Args:
          json_path: Path to the watch-history.json file from Google Takeout.

        Returns:
          List of dicts with keys: "video_id", "channel_id", "channel_title",
          "watched_at" (datetime | None).

        Raises:
          FileNotFoundError: If the JSON file does not exist at json_path.
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Not found: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        raw = json.load(f) # Load the entire JSON array into memory

    entries = []
    skipped = 0

    for item in raw:
        # Filter to YouTube watch events only — Takeout includes Search, Maps, etc.
        if item.get("header") != "YouTube":
            skipped += 1
            continue

        # Extract the video ID from the titleUrl — skip the entry if it's missing
        # (e.g., deleted videos, "Watch Later" entries, or ad-view entries)
        video_id = extract_video_id(item.get("titleUrl", ""))
        if not video_id:
            skipped += 1
            continue

        # subtitles contains channel info; it may be absent or empty for deleted channels
        subtitles     = item.get("subtitles") or []
        channel_title = ""
        # Channel URL, fallback is extract_channel_id
        # may return None — default to empty string in that case
        channel_id    = ""
        if subtitles:
            channel_title = subtitles[0].get("name", "")
            channel_id    = extract_channel_id(subtitles[0].get("url", "")) or ""

        entries.append({
            "video_id":      video_id,
            "channel_id":    channel_id,
            "channel_title": channel_title,
            # parse_timestamp returns None if the field is missing or malformed
            "watched_at":    parse_timestamp(item.get("time", "")),
        })

    print(f"[parser] {len(entries)} watch history entries parsed ({skipped} skipped)")
    return entries