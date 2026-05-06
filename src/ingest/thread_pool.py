import threading
import queue
import time
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import THREAD_POOL_WORKERS, PROGRESS_INTERVAL


class ThreadPool:
    """
        A simple, hand-rolled fixed-size thread pool.

        This implements the classic producer/consumer pattern using two queues:
          - task_queue:   producers (callers) push callables onto it;
                          workers pull and execute them
          - result_queue: workers push non-None return values onto it;
                          the caller drains it after all workers have finished

        The lifecycle is: instantiate → start() → submit() × N → shutdown() → drain_results()

        Shutdown is coordinated via "poison pills" — None values pushed onto the
        task queue, one per worker. Each worker exits its loop when it dequeues None.
        This guarantees clean shutdown without any shared "stop" flag or Event.

        This is intentionally simpler than concurrent.futures.ThreadPoolExecutor:
        there's no Future abstraction, no callback support, and no dynamic resizing.
        It's a straightforward demonstration of threading primitives (Queue, Lock,
        Thread) that's easy to reason about.

        Attributes:
            num_workers:   Number of worker threads to spawn.
            task_queue:    Thread-safe queue of callables to execute.
            result_queue:  Thread-safe queue of results returned by tasks.
    """
    def __init__(self, num_workers: int = THREAD_POOL_WORKERS):
        self.num_workers   = num_workers
        # Queue for tasks (callables). queue.Queue is thread-safe by default —
        # put() and get() both use an internal lock, so workers can safely
        # pull tasks concurrently without additional synchronization.
        self.task_queue    = queue.Queue()
        # Queue for results. Workers write here; the caller reads after shutdown.
        self.result_queue  = queue.Queue()
        self.workers      = [] # Track thread objects so we can join them
        self.counter      = 0 # Counts total tasks completed across all workers
        self.counter_lock = threading.Lock() # Guards _counter against concurrent writes

    def start(self):
        """
            Spawn all worker threads and start them.

            Each worker runs _worker_loop in its own thread, identified by a 0-based
            index for logging. Workers are marked as daemon threads so they don't
            block program exit if shutdown() is never called.

            Must be called before any submit() calls.
        """
        for i in range(self.num_workers):
            t = threading.Thread(target=self.worker_loop, args=(i,), daemon=True)
            t.start()
            self.workers.append(t)
        print(f"[pool] {self.num_workers} workers started")

    def submit(self, task):
        """
            Push a task onto the task queue.

            The task must be a callable (typically a closure/lambda) that takes no
            arguments. Workers will call it as `task()` and push the return value
            onto result_queue if it's not None.

            This method is safe to call from any thread, including the main thread
            while workers are already running.

            Args:
                task: A no-argument callable to execute on a worker thread.
        """
        self.task_queue.put(task)

    def shutdown(self):
        """
            Signal all workers to stop, then block until they all finish.

            Sends one "poison pill" (None) per worker. Each worker's loop will
            eventually dequeue a None and return, cleanly ending the thread.
            Because each worker consumes exactly one pill, this guarantees every
            worker shuts down exactly once — no worker gets two pills, no pill
            goes unclaimed.

            Blocks by calling join() on each worker thread, so this method returns
            only after all submitted tasks have been processed and all threads have
            exited. Always call this before drain_results().
        """
        for _ in range(self.num_workers):
            self.task_queue.put(None)
        for t in self.workers:
            t.join()
        print(f"[pool] All workers shut down")

    def drain_results(self):
        """
            Collect and return all results currently sitting in the result queue.

            Should be called after shutdown() to ensure no results are still in
            flight. Returns results in the order they landed in the queue, which
            is non-deterministic (depends on thread scheduling and API response times).

            Returns:
                List of all non-None return values from completed tasks.
        """
        results = []
        while not self.result_queue.empty():
            results.append(self.result_queue.get())
        return results

    def worker_loop(self, worker_id: int):
        """
            Main loop executed by each worker thread.

            Continuously pulls tasks off the task queue and executes them until
            it receives a poison pill (None), at which point it returns and the
            thread exits.

            If a task raises an exception, the error is logged and the worker
            continues — one bad task doesn't kill the whole worker. The progress
            counter is incremented in `finally` so it always fires, whether the
            task succeeds or fails.

            Non-None return values are pushed onto result_queue for the caller
            to retrieve via drain_results(). None return values are silently
            discarded — tasks use this to signal "no meaningful result" (e.g.,
            an empty channel or a failed fetch).

            Args:
                worker_id: Integer index of this worker (0-based), used in logs.
                """
        while True:
            # Block here until a task is available; queue.get() is thread-safe
            task = self.task_queue.get()

            # Poison pill — time to exit
            if task is None:
                return

            try:
                result = task()
                # Only store the result if it's meaningful (non-None)
                if result is not None:
                    self.result_queue.put(result)
            except Exception as e:
                # Catch-all so a single failing task doesn't kill the worker thread
                print(f"[pool] Worker {worker_id} error: {e}")
            finally:
                # Always increment the counter, even on failure, to track total
                # tasks processed. `finally` ensures this runs even if task() raises.
                self.increment_counter()

    def increment_counter(self):
        """Thread-safe progress counter."""
        with self.counter_lock:
            self.counter += 1
            if self.counter % PROGRESS_INTERVAL == 0:
                print(f"[pool] {self.counter} tasks completed")