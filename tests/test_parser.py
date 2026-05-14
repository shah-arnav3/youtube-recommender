"""
Tests for src/ingest/parser.py

Focuses on the non-trivial parsing logic: the CSV fallback when Channel ID
is missing, watch history filtering, and the seen-but-edge-case URL formats.
Basic regex happy-path cases are light — the interesting tests are the
failure modes and fallbacks.
"""
import sys
import os
import csv
import json
import tempfile
import pytest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.ingest.parser import (
    extract_video_id,
    parse_subscriptions,
    parse_watch_history,
)


# ── extract_video_id ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?feature=share&v=xyzXYZ12345", "xyzXYZ12345"),
    ("https://www.youtube.com/channel/UCxxx", None),   # no video ID
    ("https://www.youtube.com/watch?v=short", None),   # < 11 chars
    ("", None),
])
def test_extract_video_id(url, expected):
    assert extract_video_id(url) == expected


# ── parse_subscriptions ────────────────────────────────────────────────────────

def _write_csv(rows):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    writer = csv.DictWriter(f, fieldnames=["Channel ID", "Channel URL", "Channel title"])
    writer.writeheader()
    writer.writerows(rows)
    f.close()
    return f.name


def test_subscriptions_basic_parse():
    path = _write_csv([
        {"Channel ID": "UCaaa", "Channel URL": "", "Channel title": "Chan A"},
    ])
    result = parse_subscriptions(path)
    assert len(result) == 1
    assert result[0]["channel_id"] == "UCaaa"
    os.unlink(path)


def test_subscriptions_falls_back_to_url_when_id_missing():
    path = _write_csv([
        {"Channel ID": "", "Channel URL": "https://youtube.com/channel/UCfallback", "Channel title": "X"},
    ])
    result = parse_subscriptions(path)
    assert result[0]["channel_id"] == "UCfallback"
    os.unlink(path)


def test_subscriptions_skips_unresolvable_rows():
    path = _write_csv([
        {"Channel ID": "UCgood", "Channel URL": "", "Channel title": "Good"},
        {"Channel ID": "",       "Channel URL": "https://youtube.com/user/noid", "Channel title": "Bad"},
    ])
    result = parse_subscriptions(path)
    assert len(result) == 1
    os.unlink(path)


def test_subscriptions_file_not_found():
    with pytest.raises(FileNotFoundError):
        parse_subscriptions("/no/such/file.csv")


# ── parse_watch_history ────────────────────────────────────────────────────────

def _write_json(data):
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(data, f)
    f.close()
    return f.name


def _yt_entry(video_id="dQw4w9WgXcQ", channel_id="UCxxx"):
    return {
        "header": "YouTube",
        "titleUrl": f"https://www.youtube.com/watch?v={video_id}",
        "subtitles": [{"name": "Chan", "url": f"https://www.youtube.com/channel/{channel_id}"}],
        "time": "2024-01-01T10:00:00Z",
    }


def test_watch_history_filters_non_youtube():
    path = _write_json([
        _yt_entry(),
        {"header": "Google Search", "time": "2024-01-01T10:00:00Z"},
    ])
    result = parse_watch_history(path)
    assert len(result) == 1
    os.unlink(path)


def test_watch_history_skips_missing_url():
    path = _write_json([{"header": "YouTube", "time": "2024-01-01T10:00:00Z"}])
    assert parse_watch_history(path) == []
    os.unlink(path)


def test_watch_history_parsed_datetime():
    path = _write_json([_yt_entry()])
    result = parse_watch_history(path)
    assert isinstance(result[0]["watched_at"], datetime)
    assert result[0]["watched_at"].tzinfo == timezone.utc
    os.unlink(path)


def test_watch_history_missing_subtitles_gives_empty_channel():
    path = _write_json([{
        "header": "YouTube",
        "titleUrl": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "time": "2024-01-01T10:00:00Z",
    }])
    result = parse_watch_history(path)
    assert result[0]["channel_id"] == ""
    os.unlink(path)