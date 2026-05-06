# YouTube Meal-Time Recommender

A personal video recommendation engine that solves a specific problem: YouTube's algorithm optimizes for time-on-platform, not your satisfaction. When you sit down to eat with 15–25 minutes, it surfaces long-form content and rabbit holes that are misaligned with what you actually want.

This tool recommends unseen videos from channels you explicitly subscribe to, filtered by a hard time budget and ranked by a taste profile derived from your real watch history — not YouTube's ad-revenue incentives.

---

## Architecture

The core design decision is separating candidate sourcing from taste modeling — two different problems that require two different data sources.

**Layer 1 — Candidate pool: your subscription feed.** Subscriptions are an explicit, intentional signal. You chose to follow those channels. Recent uploads from subscribed channels are unseen but high-probability matches for your taste. This is the only pool the engine recommends from.

**Layer 2 — Taste profile: your watch history.** Watch history is noisy — it contains rabbit hole content, algorithm-surfaced videos, things watched once and abandoned. It is never used as a candidate source. Instead it gets aggregated into a taste profile: which subscribed channels you actually watch most, which categories dominate your viewing, which channels you've rated positively over time. This profile is the personalization signal used to rank candidates.

At server startup, the engine loads all subscription videos into an inverted index and builds the taste profile in memory. A query runs in seven steps: LRU cache check, inverted index lookup by mood, unseen filter, duration hard filter, composite scoring, min-heap top-K selection, and cache store. Feedback invalidates only the cache entries that contained the rated video, so scores update immediately without a full cache flush.

---

## Data Structures

### Inverted Index

The inverted index maps YouTube category IDs to lists of video IDs from your subscription feed. At query time, a mood tag (e.g. "interesting") maps to a set of category IDs. The index retrieves matching video IDs in O(1) per category — no scanning required.

```
category 28 (Science & Tech) : [vid_a, vid_b, vid_c, ...]
category 27 (Education)      : [vid_x, vid_y, ...]
```

At 3,000 videos, a linear scan would also be fast. The value is implementing the pattern correctly — this is the exact structure underlying Elasticsearch, Lucene, and every production search engine. The answer to "why not just scan?" is: at this scale it wouldn't matter, but the structure is identical to real search indexes and demonstrates the pattern correctly.

### Min-Heap (Top-K Selection)

After filtering candidates by mood, unseen status, and duration, a min-heap of fixed size K=8 extracts the top results without sorting the full candidate list.

The heap maintains a "worst of the best" tracker. For each candidate, if its score exceeds the current worst in the heap, the worst is evicted and the new candidate takes its place. At the end the heap contains exactly the top 8.

Complexity: O(n log k) vs O(n log n) for a full sort. Since k=8 is constant, log k ≈ 3 — effectively O(n). Implemented as a plain Python list with manual `heapify_up` and `heapify_down` — no `heapq`.

### LRU Cache

Sits in front of the full query pipeline. Cache key is `(mood_tag, time_budget_minutes)`. On a hit, results are returned in O(1) without touching the index, scorer, or heap.

Implemented as a Python dict + custom doubly linked list with head/tail sentinel nodes. The dict gives O(1) lookup; the linked list maintains recency order so the least recently used entry is always at the tail. Both get and put are O(1).

**Targeted invalidation:** When feedback is logged, `invalidate_containing(video_id)` evicts only cached entries whose stored video ID set contains the rated video. A mood-wide flush would be simpler but too aggressive — it would evict results unaffected by the rating. Each cached value stores both the result list and the set of video IDs it contains for O(1) membership checking at invalidation time.

---

## Scoring

Each candidate that passes the unseen and duration filters receives a composite score:

```
score = (0.35 × channel_affinity)
      + (0.35 × channel_satisfaction)
      + (0.20 × category_weight)
      + (0.10 × like_ratio)
```

| Component | Formula | Notes |
|---|---|---|
| channel_affinity | watch_count_for_channel / max_watch_count | Normalized 0–1. Only counts watches from subscribed channels — algorithm-surfaced history is too noisy. |
| channel_satisfaction | From channel_scores table | Explicit y/n feedback. Defaults to 0.5 (neutral) — not 0.0, so unrated channels aren't penalized. |
| category_weight | category_watch_fraction from taste profile | How much of your history falls in this video's category. |
| like_ratio | like_count / (view_count + 1) | Objective quality signal. +1 avoids division by zero. |

**Weight rationale:** w1 + w2 = 0.70 — personal taste signals dominate. w4 = 0.10 — intentionally small to avoid viral bias. All candidates are already unseen, so no recency penalty is needed.

---

## Thread Pool

Fetching recent uploads for 65 subscribed channels sequentially takes several minutes. The hand-rolled thread pool reduces this significantly by parallelizing API calls across 8 concurrent workers.

Workers pull tasks from a shared `queue.Queue`, make API calls, and push results to an output queue. The main thread never makes API calls — it only manages the queues. Graceful shutdown uses a poison pill pattern: one `None` sentinel per worker signals each to exit cleanly.

Progress is logged every 10 channels using a shared counter protected by `threading.Lock` — needed because `+=` on a plain integer is not atomic in Python.

---

## How to Run

### 1. Prerequisites

- Python 3.11+
- A Google account with YouTube watch history

### 2. Get your Google Takeout export

1. Go to [takeout.google.com](https://takeout.google.com)
2. Deselect all, select **YouTube and YouTube Music** only
3. Click **Multiple formats** and change History format from HTML to **JSON**
4. Export and download the zip
5. Extract these two files into the `data/` folder:
   - `Takeout/YouTube and YouTube Music/history/watch-history.json`
   - `Takeout/YouTube and YouTube Music/subscriptions/subscriptions.csv`

### 3. Get a YouTube Data API v3 key

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project
3. Enable **YouTube Data API v3**
4. Go to **APIs & Services**, then **Credentials**, then **Create Credentials**, then **API key**
5. Copy the key

### 4. Configure

```bash
cp config.py.example config.py
```

Open `config.py` and set:
```python
YOUTUBE_API_KEY = "your-key-here"
```

### 5. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install flask
```

### 6. Ingest your data

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from src.db import init_db
from src.ingest.ingest import ingest_watch_history, ingest_subscriptions, ingest_subscription_videos
init_db()
ingest_watch_history('data/watch-history.json')
ingest_subscriptions('data/subscriptions.csv')
ingest_subscription_videos()
"
```

This fetches recent uploads from all your subscribed channels. Takes 1–3 minutes depending on how many subscriptions you have.

### 7. Use the CLI

```bash
# Get recommendations
python3 src/cli.py recommend --minutes 18 --mood interesting

# Available moods: interesting, funny, chill, educational, random

# Rate a video after watching (y = satisfied, n = not)
python3 src/cli.py rate --video-id VIDEO_ID --rating y --mood interesting --minutes 18

# View stats
python3 src/cli.py stats

# Pull fresh uploads from your channels (run weekly)
python3 src/cli.py refresh
```

---

## Project Structure

```
youtube-recommender/
├── config.py.example       # Copy to config.py and add your API key
├── requirements.txt
├── data/                   # Drop Takeout files here (gitignored)
├── db/                     # SQLite database (gitignored)
├── logs/                   # Empty/failed channel logs
└── src/
    ├── db.py               # Schema + connection helper
    ├── ingest/
    │   ├── parser.py       # Parse Takeout files
    │   ├── fetcher.py      # YouTube API calls
    │   ├── thread_pool.py  # Hand-rolled thread pool
    │   └── ingest.py       # Full ingestion pipeline
    ├── profile/
    │   └── taste_profile.py  # Channel affinity, category weights, seen IDs
    ├── index/
    │   └── inverted_index.py # Build and query inverted index
    ├── engine/
    │   ├── heap.py           # Hand-rolled min-heap
    │   ├── lru_cache.py      # Hand-rolled LRU cache
    │   ├── scorer.py         # Composite scoring + full query pipeline
    │   └── feedback.py       # Log ratings, update channel scores
    ├── api/
    │   └── server.py         # Flask API
    └── cli.py                # Command-line interface
```