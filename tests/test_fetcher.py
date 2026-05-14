"""
Tests for src/ingest/fetcher.py

parse_duration and safe_int are pure functions worth unit-testing directly.
The HTTP functions are tested with mocked urlopen — the interesting cases
are retry behaviour, filtering of invalid videos, and field normalisation.
"""
import sys
import os
import json
import pytest
import urllib.error
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.ingest.fetcher import (
    parse_duration, safe_int, api_get,
    get_recent_uploads, get_video_metadata,
)


# parse_duration

@pytest.mark.parametrize("iso,expected", [
    ("PT1H45M30S", 6330),
    ("PT15M",      900),
    ("PT45S",      45),
    ("P0D",        0),    # live stream sentinel
    ("",           0),
    (None,         0),
])
def test_parse_duration(iso, expected):
    assert parse_duration(iso) == expected


# safe_int

@pytest.mark.parametrize("value,expected", [
    ("12345", 12345),
    (None,    0),
    ("",      0),
    ("n/a",   0),
])
def test_safe_int(value, expected):
    assert safe_int(value) == expected


# api_get

def _mock_urlopen(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: resp
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_api_get_returns_parsed_json():
    payload = {"items": []}
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(payload)):
        assert api_get("videos", {"id": "v1"}) == payload


def test_api_get_retries_on_429_then_raises():
    from config import MAX_RETRIES
    err = urllib.error.HTTPError(None, 429, "Rate limited", {}, None)
    calls = []
    with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: calls.append(1) or (_ for _ in ()).throw(err)):
        with patch("time.sleep"):
            with pytest.raises(RuntimeError):
                api_get("videos", {"id": "v1"})
    assert len(calls) == MAX_RETRIES


def test_api_get_404_raises_immediately():
    err = urllib.error.HTTPError(None, 404, "Not Found", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(RuntimeError):
            api_get("videos", {"id": "missing"})


# get_recent_uploads

def test_get_recent_uploads_extracts_video_ids():
    payload = {"items": [
        {"snippet": {"resourceId": {"videoId": "v1"}}},
        {"snippet": {"resourceId": {"videoId": "v2"}}},
    ]}
    with patch("src.ingest.fetcher.api_get", return_value=payload):
        assert get_recent_uploads("PLsomething") == ["v1", "v2"]


def test_get_recent_uploads_returns_empty_on_error():
    with patch("src.ingest.fetcher.api_get", side_effect=RuntimeError):
        assert get_recent_uploads("PLbad") == []


# get_video_metadata

def _item(video_id="v1", duration="PT10M", category="27", views="1000", likes="50"):
    return {
        "id": video_id,
        "snippet": {
            "title": "T", "channelId": "ch1", "channelTitle": "Chan",
            "categoryId": category, "publishedAt": "2024-01-01T00:00:00Z",
        },
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": views, "likeCount": likes, "commentCount": "0"},
    }


def test_get_video_metadata_parses_correctly():
    with patch("src.ingest.fetcher.api_get", return_value={"items": [_item()]}):
        result = get_video_metadata(["v1"])
    assert len(result) == 1
    assert result[0]["duration_sec"] == 600
    assert result[0]["category_id"] == 27   # cast to int
    assert result[0]["view_count"] == 1000


def test_get_video_metadata_excludes_live_streams():
    with patch("src.ingest.fetcher.api_get", return_value={"items": [_item(duration="P0D")]}):
        assert get_video_metadata(["v1"]) == []


def test_get_video_metadata_excludes_missing_category():
    item = _item()
    del item["snippet"]["categoryId"]
    with patch("src.ingest.fetcher.api_get", return_value={"items": [item]}):
        assert get_video_metadata(["v1"]) == []