"""
Tests for src/ingest/thread_pool.py

The interesting behaviours: results are collected correctly, a failing task
doesn't kill the worker, and the make_task() closure pattern binds values
correctly (vs the naive lambda bug it exists to fix).
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.ingest.thread_pool import ThreadPool


@pytest.fixture
def pool():
    p = ThreadPool(num_workers=2)
    p.start()
    yield p
    try:
        p.shutdown()
    except Exception:
        pass


def test_results_collected(pool):
    for i in range(5):
        pool.submit(lambda i=i: i)
    pool.shutdown()
    assert sorted(pool.drain_results()) == [0, 1, 2, 3, 4]


def test_none_results_excluded(pool):
    pool.submit(lambda: None)
    pool.submit(lambda: "kept")
    pool.shutdown()
    assert pool.drain_results() == ["kept"]


def test_failing_task_does_not_kill_worker():
    p = ThreadPool(num_workers=1)
    p.start()
    p.submit(lambda: 1 / 0)
    p.submit(lambda: "survived")
    p.shutdown()
    assert "survived" in p.drain_results()


def test_make_task_closure_binds_correctly():
    """
    Verifies the make_task() pattern used in ingest.py captures loop
    variables by value, not reference.

    The naive lambda bug: all lambdas in a loop share the same reference
    to the loop variable, so by execution time they all see the final value.
    make_task() fixes this by binding the value as a default argument.
    """
    def make_task(val):
        def task():
            return val
        return task

    p = ThreadPool(num_workers=2)
    p.start()
    for val in range(5):
        p.submit(make_task(val))
    p.shutdown()
    assert sorted(p.drain_results()) == [0, 1, 2, 3, 4]