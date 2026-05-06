class Node:
    def __init__(self, key, value):
        self.key   = key
        self.value = value   # {"results": [...], "video_ids": set()}
        self.prev  = None
        self.next  = None


class LRUCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.cache: dict[tuple, Node] = {}

        # Sentinel nodes — never evicted, never stored in cache dict
        self.head = Node(None, None)   # most recent end
        self.tail = Node(None, None)   # least recent end
        self.head.next = self.tail
        self.tail.prev = self.head

        # Stats
        self.hits   = 0
        self.misses = 0

    def get(self, key: tuple) -> list | None:
        """
        O(1) cache lookup.
        On hit: move node to front (most recently used) and return results.
        On miss: return None.
        """
        if key not in self.cache:
            self.misses += 1
            return None

        node = self.cache[key]
        self.remove(node)
        self.insert_at_front(node)
        self.hits += 1
        return node.value["results"]

    def put(self, key: tuple, results: list, video_ids: set) -> None:
        """
        O(1) insert.
        If key exists, update it. If over capacity, evict LRU entry.
        value is stored as {"results": results, "video_ids": video_ids}
        so invalidation can check membership efficiently.
        """
        if key in self.cache:
            self.remove(self.cache[key])

        node = Node(key, {"results": results, "video_ids": video_ids})
        self.cache[key] = node
        self.insert_at_front(node)

        if len(self.cache) > self.capacity:
            # Evict the least recently used — the node just before tail
            lru_node = self.tail.prev
            self.remove(lru_node)
            del self.cache[lru_node.key]

    def invalidate_containing(self, video_id: str) -> int:
        """
        Evict all cached entries whose result set contains this video_id.
        Called when feedback is logged — ensures updated scores are reflected
        in future queries without flushing the entire cache.
        """
        to_evict = [
            key for key, node in self.cache.items()
            if video_id in node.value["video_ids"]
        ]
        for key in to_evict:
            self.remove(self.cache[key])
            del self.cache[key]

        return len(to_evict)

    def hit_rate(self) -> float:
        """Return cache hit rate as a fraction 0.0-1.0."""
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total > 0 else 0.0

    def remove(self, node: Node) -> None:
        """Detach a node from the linked list in O(1)."""
        node.prev.next = node.next
        node.next.prev = node.prev

    def insert_at_front(self, node: Node) -> None:
        """Insert a node right after head (most recently used) in O(1)."""
        node.next = self.head.next
        node.prev = self.head
        self.head.next.prev = node
        self.head.next = node