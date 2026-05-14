"""
Tests for src/engine/scorer.py

Covers the scoring formula and the full recommend() pipeline.
The scoring weights are intentional design decisions — tests verify
the *direction* of effects, not hardcoded weight values.
"""
import sys
import os
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.engine.scorer import compute_score, format_result, recommend
from src.engine.lru_cache import LRUCache


# Fixtures / helpers

def _video(video_id="v1", channel_id="ch1", category_id=27,
           duration_sec=600, like_count=100, view_count=1000):
    return {
        "video_id": video_id, "title": "T", "channel_id": channel_id,
        "channel_title": "Chan", "category_id": category_id,
        "duration_sec": duration_sec, "like_count": like_count,
        "view_count": view_count, "comment_count": 0, "published_at": "2024-01-01",
    }


def _profile(affinity=None, satisfaction=None, weights=None):
    return {
        "channel_affinity":     affinity or {},
        "channel_satisfaction": satisfaction or {},
        "category_weights":     weights or {},
    }


def _index(videos):
    idx = MagicMock()
    idx.video_store = {v["video_id"]: v for v in videos}
    idx.get_candidates.return_value = list(idx.video_store)
    return idx


def _recommend(videos, profile=None, seen=None, mood="learn", minutes=20):
    cache = LRUCache(32)
    results, hit = recommend(
        mood, minutes, _index(videos), profile or _profile(), seen or set(), cache
    )
    return results, hit, cache


# compute_score

def test_higher_affinity_raises_score():
    v = _video(channel_id="ch")
    low, _ = compute_score(v, _profile(affinity={"ch": 0.0}))
    high, _ = compute_score(v, _profile(affinity={"ch": 1.0}))
    assert high > low


def test_unknown_channel_defaults_neutral_satisfaction():
    _, components = compute_score(_video(channel_id="unknown"), _profile())
    assert components["channel_satisfaction"] == 0.5


def test_like_ratio_uses_view_count_plus_one():
    # +1 in denominator prevents division by zero and is part of the spec
    _, c = compute_score(_video(like_count=1000, view_count=999), _profile())
    assert c["like_ratio"] == pytest.approx(1000 / 1000, rel=1e-3)


# format_result

def test_duration_formatted_correctly():
    v = _video(duration_sec=754)  # 12:34
    score, components = compute_score(v, _profile())
    r = format_result(v, score, components)
    assert r["duration"] == "12:34"


def test_why_reflects_dominant_component():
    v = _video(channel_id="ch")
    profile = _profile(affinity={"ch": 1.0})
    score, components = compute_score(v, profile)
    assert format_result(v, score, components)["why"] == "channel affinity"


def test_url_contains_video_id():
    v = _video(video_id="abc123")
    score, components = compute_score(v, _profile())
    assert "abc123" in format_result(v, score, components)["url"]


# recommend() pipeline

def test_seen_videos_excluded():
    videos = [_video("seen"), _video("unseen")]
    results, _, _ = _recommend(videos, seen={"seen"})
    assert all(r["video_id"] != "seen" for r in results)


def test_duration_filter():
    short = _video("short", duration_sec=300)
    long_v = _video("long", duration_sec=99999)
    results, _, _ = _recommend([short, long_v], minutes=10)
    ids = [r["video_id"] for r in results]
    assert "short" in ids
    assert "long" not in ids


def test_results_descending_by_score():
    results, _, _ = _recommend([_video(f"v{i}", channel_id=f"ch{i}") for i in range(5)])
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_cache_hit_on_repeat_call():
    videos = [_video("v1")]
    idx = _index(videos)
    cache = LRUCache(32)
    _, hit1 = recommend("learn", 20, idx, _profile(), set(), cache)
    _, hit2 = recommend("learn", 20, idx, _profile(), set(), cache)
    assert not hit1
    assert hit2


def test_diversity_cap():
    from config import MAX_PER_CHANNEL
    videos = [_video(f"v{i}", channel_id="same") for i in range(10)]
    results, _, _ = _recommend(videos)
    assert sum(1 for r in results if r["channel"] == "Chan") <= MAX_PER_CHANNEL


def test_empty_candidates_returns_empty():
    idx = MagicMock()
    idx.get_candidates.return_value = []
    idx.video_store = {}
    results, _ = recommend("learn", 20, idx, _profile(), set(), LRUCache(32))
    assert results == []