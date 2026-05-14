# YouTube Meal-Time Recommender

A personal video recommendation engine that solves a specific problem: YouTube's algorithm optimizes for time-on-platform, not your satisfaction. When you sit down to eat with 15–25 minutes, it surfaces long-form content and rabbit holes that are misaligned with what you actually want.

This tool recommends unseen videos from channels you explicitly subscribe to, filtered by a hard time budget and ranked by a taste profile derived from your real watch history — not YouTube's ad-revenue incentives.

---

## Architecture

The core design decision is separating candidate sourcing from taste modeling — two different problems that require two different data sources.

**Layer 1 — Candidate pool: your subscription feed.** Subscriptions are an explicit, intentional signal. You chose to follow those channels. Recent uploads from subscribed channels are unseen but high-probability matches for your taste. This is the only pool the engine recommends from.

**Layer 2 — Taste profile: your watch history.** Watch history is noisy — it contains rabbit hole content, algorithm-surfaced videos, things watched once and abandoned. It is never used as a candidate source. Instead it gets aggregated into a taste profile: which subscribed channels you actually watch most, which categories dominate your viewing, which channels you've rated positively over time. This profile is the personalization signal used to rank candidates.

At server startup, the engine loads all subscription videos into an inverted index and builds the taste profile in memory. A query runs in eight steps: LRU cache check, inverted index lookup by mood, unseen filter, duration hard filter, composite scoring, min-heap top-K selection, per-channel diversity cap, and cache store. Feedback invalidates only the cache entries that contained the rated video, so scores update immediately without a full cache flush.

---

## Data Structures

### Inverted Index

The inverted index maps YouTube category IDs to lists of video IDs from your subscription feed. At query time, a mood tag (e.g. "learn") maps to a set of category IDs. The index retrieves matching video IDs in O(1) per category — no scanning required.

```
category 28 (Science & Tech) : [vid_a, vid_b, vid_c, ...]
category 27 (Education)      : [vid_x, vid_y, ...]
```



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
| channel_affinity | recency-decayed watch sum, normalized 0–1 | Exponential decay with ~140 day half-life. Recent watches count more than old ones. Only counts watches from subscribed channels — algorithm-surfaced history is too noisy. |
| channel_satisfaction | From channel_scores table | Explicit y/n feedback rate. Defaults to 0.5 (neutral) — not 0.0, so unrated channels aren't penalised. Updated immediately when feedback is logged. |
| category_weight | category_watch_fraction from taste profile | How much of your history falls in this video's category. |
| like_ratio | like_count / (view_count + 1) | Objective quality signal. +1 avoids division by zero. |

**Weight rationale:** w1 + w2 = 0.70 — personal taste signals dominate. w4 = 0.10 — intentionally small to avoid viral bias. All candidates are already unseen, so no recency penalty is needed.

**Diversity cap:** After heap selection, results are filtered to a maximum of 2 videos per channel. This prevents a single high-affinity channel from dominating all 8 slots and ensures at least 4 distinct channels are represented per query.

**Seen video window:** Rather than blacklisting every video ever watched, the unseen filter covers only the last 90 days. Videos older than the window become candidates again, preventing the pool from silently shrinking over time.

**Score explanation:** Every result includes a `why` field naming the highest-contributing score component, and a `score_breakdown` dict with all four weighted values. Both are visible in CLI output and API responses.

---

## Thread Pool

Fetching recent uploads for 65 subscribed channels sequentially takes several minutes. The hand-rolled thread pool reduces this significantly by parallelizing API calls across 8 concurrent workers.

Workers pull tasks from a shared `queue.Queue`, make API calls, and push results to an output queue. The main thread never makes API calls — it only manages the queues. Graceful shutdown uses a poison pill pattern: one `None` sentinel per worker signals each to exit cleanly.

Progress is logged every 10 channels using a shared counter protected by `threading.Lock` — needed because `+=` on a plain integer is not atomic in Python.

---

## How to Run

### 1. Prerequisites

- Python 3.11+  
  Earlier versions will produce syntax errors due to type hint syntax (`list[str]`, `tuple[x, y]`). Check with `python3 --version`.
- A Google account with YouTube watch history

### 2. Get your Google Takeout export

> **Note:** Google may take several hours — sometimes up to a day — to prepare your export. You'll receive an email when it's ready. The zip file may be large; you only need two files from it.

1. Go to [takeout.google.com](https://takeout.google.com)
2. Deselect all, then select **YouTube and YouTube Music** only
3. Click **Multiple formats** and change History format from HTML to **JSON**
4. Export and download the zip
5. Extract these two files into the `data/` folder:
   - `Takeout/YouTube and YouTube Music/history/watch-history.json`
   - `Takeout/YouTube and YouTube Music/subscriptions/subscriptions.csv`

### 3. Get a YouTube Data API v3 key

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project
3. Enable **YouTube Data API v3**
4. Go to **APIs & Services → Credentials → Create Credentials → API key**
5. Copy the key

> **Quota:** The free tier gives you 10,000 units/day. A full ingest on ~65 subscriptions costs roughly 500–2,000 units. The weekly `refresh` command costs a similar amount. You are unlikely to hit the limit under normal use.

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
python3 ingest.py
```

This checks for your Takeout files, initialises the database, and fetches recent uploads from all your subscribed channels. Takes 1–3 minutes depending on how many subscriptions you have.

### 7. Use the CLI

```bash
# Get recommendations
python3 src/cli.py recommend --minutes 18 --mood laugh

# Available moods: laugh, learn, zone out, hobbies, stories, random

# View stats
python3 src/cli.py stats

# Pull fresh uploads from your channels (run weekly)
python3 src/cli.py refresh
```

After printing results, the recommend command prompts you to rate what you watched:

```
Which video did you watch? (1-8, or Enter to skip):
Did you enjoy it? (y/n, or Enter to skip):
```

Both prompts are optional — press Enter at either to exit cleanly. Ratings update channel scores immediately and refine future recommendations.

If you need to rate a video outside of a recommend session:

```bash
python3 src/cli.py rate --video-id VIDEO_ID --rating y --mood laugh --minutes 18
```

---

## Re-running & Refreshing

`python3 ingest.py` is safe to re-run at any time:
- Watch history uses `INSERT OR IGNORE` — duplicate entries are silently skipped
- Subscriptions use `INSERT OR REPLACE` — rows are refreshed if titles or playlist IDs have changed
- `ingest_subscription_videos()` only fetches metadata for video IDs not already in the DB, saving API quota

To pull fresh uploads without re-importing Takeout data:
```bash
python3 src/cli.py refresh
```

To import a newer Takeout export, drop the new files into `data/` and re-run `python3 ingest.py`. Existing watch history is preserved; new entries are appended.

---

## Project Structure

```
youtube-recommender/
├── config.py.example       # Copy to config.py and add your API key
├── requirements.txt
├── ingest.py               # First-time setup: run once after Takeout import
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