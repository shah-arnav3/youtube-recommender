"""
Tests for src/engine/heap.py

The heap is used for top-K selection in the scorer. What matters:
- min stays at root
- capacity eviction keeps the highest-scoring K items
- pop order is ascending (so reversing gives descending rank)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.engine.heap import heap_push, heap_pop


def _build(scores, capacity=20):
    heap = []
    for i, s in enumerate(scores):
        heap_push(heap, (s, f"v{i}"), capacity=capacity)
    return heap


def test_min_at_root():
    heap = _build([3.0, 1.0, 2.0])
    assert heap[0][0] == 1.0


def test_capacity_keeps_highest_k():
    heap = _build([1.0, 2.0, 3.0, 4.0, 5.0], capacity=3)
    assert len(heap) == 3
    assert {item[0] for item in heap} == {3.0, 4.0, 5.0}


def test_pop_order_is_ascending():
    heap = _build([5.0, 1.0, 3.0, 2.0, 4.0])
    popped = [heap_pop(heap)[0] for _ in range(5)]
    assert popped == sorted(popped)


def test_capacity_one_keeps_best():
    heap = _build([1.0, 5.0, 3.0], capacity=1)
    assert heap[0] == (5.0, "v1")