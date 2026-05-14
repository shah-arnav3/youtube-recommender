"""
Tests for src/engine/lru_cache.py

The cache sits in front of the full query pipeline. What matters:
- hits return the stored list; misses return None
- LRU eviction order is by use, not insertion
- targeted invalidation evicts only entries containing the rated video
- hit rate tracking is accurate
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.engine.lru_cache import LRUCache


def _put(cache, key, video_ids):
    results = [{"video_id": v} for v in video_ids]
    cache.put(key, results, set(video_ids))
    return results


def test_miss_returns_none():
    assert LRUCache(4).get(("laugh", 20)) is None


def test_hit_returns_results():
    cache = LRUCache(4)
    results = _put(cache, ("laugh", 20), ["v1"])
    assert cache.get(("laugh", 20)) == results


def test_evicts_lru_on_overflow():
    cache = LRUCache(2)
    _put(cache, ("a", 1), ["v1"])
    _put(cache, ("b", 2), ["v2"])
    cache.get(("a", 1))           # promote "a" — "b" is now LRU
    _put(cache, ("c", 3), ["v3"])
    assert cache.get(("b", 2)) is None
    assert cache.get(("a", 1)) is not None


def test_invalidate_removes_matching_entries():
    cache = LRUCache(8)
    _put(cache, ("laugh", 20), ["v1", "v2"])
    _put(cache, ("learn", 15), ["v3"])
    evicted = cache.invalidate_containing("v1")
    assert evicted == 1
    assert cache.get(("laugh", 20)) is None
    assert cache.get(("learn", 15)) is not None  # unrelated entry survives


def test_invalidate_evicts_multiple_entries():
    cache = LRUCache(8)
    _put(cache, ("laugh", 20), ["shared", "v1"])
    _put(cache, ("learn", 15), ["shared", "v2"])
    assert cache.invalidate_containing("shared") == 2


def test_hit_rate():
    cache = LRUCache(4)
    _put(cache, ("a", 1), ["v1"])
    cache.get(("a", 1))   # hit
    cache.get(("b", 2))   # miss
    assert cache.hit_rate() == 0.5