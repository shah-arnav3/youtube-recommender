def heapify_up(heap: list, i: int) -> None:
    """
    After inserting at index i, bubble it up until the heap property
    is restored. A node must be >= its parent.
    """
    while i > 0:
        parent = (i - 1) // 2
        if heap[i] < heap[parent]:
            heap[i], heap[parent] = heap[parent], heap[i]
            i = parent
        else:
            break


def heapify_down(heap: list, i: int) -> None:
    """
    After removing the root and placing the last element at index 0,
    push it down until the heap property is restored.
    A node must be <= both its children.
    """
    n = len(heap)
    while True:
        smallest = i
        left     = 2 * i + 1
        right    = 2 * i + 2

        if left < n and heap[left] < heap[smallest]:
            smallest = left
        if right < n and heap[right] < heap[smallest]:
            smallest = right

        if smallest != i:
            heap[i], heap[smallest] = heap[smallest], heap[i]
            i = smallest
        else:
            break


def heap_push(heap: list, item: tuple, capacity: int) -> None:
    """
    Push item onto the heap.
    If the heap exceeds capacity, pop the minimum (worst score).
    This keeps only the top-K highest scoring items.

    Items are tuples of (score, video_id).
    Python compares tuples element-by-element, so score is compared first.
    """
    heap.append(item)
    heapify_up(heap, len(heap) - 1)

    if len(heap) > capacity:
        heap_pop(heap)


def heap_pop(heap: list) -> tuple:
    """
    Remove and return the minimum element (lowest score).
    Swap root with last element, shrink the list, heapify down.
    """
    if len(heap) == 1:
        return heap.pop()

    root = heap[0]
    heap[0] = heap.pop()   # move last element to root
    heapify_down(heap, 0)
    return root