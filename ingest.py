#!/usr/bin/env python3
"""
First-time setup script. Run this once after dropping your Takeout files into data/:

    python3 ingest.py

Safe to re-run — watch history uses INSERT OR IGNORE (duplicates skipped),
subscriptions use INSERT OR REPLACE (rows refreshed if changed). No data is lost
on subsequent runs.

To pull fresh uploads from your channels without re-importing Takeout data, use
the refresh command instead:

    python3 src/cli.py refresh
"""
import os
import sys

WATCH_HISTORY_PATH = "data/watch-history.json"
SUBSCRIPTIONS_PATH = "data/subscriptions.csv"


def check_files() -> None:
    """
    Verify that both required Takeout files exist before touching the DB or API.

    Fails fast with a clear message rather than letting the ingest pipeline
    crash mid-run with a less obvious FileNotFoundError.
    """
    missing = []
    if not os.path.exists(WATCH_HISTORY_PATH):
        missing.append(
            f"  - {WATCH_HISTORY_PATH}\n"
            f"    (from Takeout/YouTube and YouTube Music/history/)"
        )
    if not os.path.exists(SUBSCRIPTIONS_PATH):
        missing.append(
            f"  - {SUBSCRIPTIONS_PATH}\n"
            f"    (from Takeout/YouTube and YouTube Music/subscriptions/)"
        )
    if missing:
        print("Missing Takeout files:\n")
        print("\n".join(missing))
        print("\nSee README step 2 for export instructions.")
        sys.exit(1)


if __name__ == "__main__":
    check_files()

    sys.path.insert(0, ".")
    from src.db import init_db
    from src.ingest.ingest import (
        ingest_watch_history,
        ingest_subscriptions,
        ingest_subscription_videos,
    )

    print("Initialising database...")
    init_db()

    print("\nImporting watch history...")
    ingest_watch_history(WATCH_HISTORY_PATH)

    print("\nImporting subscriptions...")
    ingest_subscriptions(SUBSCRIPTIONS_PATH)

    print("\nFetching recent uploads from subscribed channels...")
    print("(This makes YouTube API calls — takes 1–3 minutes depending on subscription count)")
    ingest_subscription_videos()

    print("\nIngest complete.")