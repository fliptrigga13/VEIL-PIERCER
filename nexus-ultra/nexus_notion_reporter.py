"""
nexus_notion_reporter.py — Thin polling wrapper for NEXUS ULTRA Notion sync.

Reads nexus_cycle_feed.json every 60 seconds and pushes new entries to Notion
via nexus_notion_bridge. Contains zero Notion API logic — all writes go through
the bridge.

Usage:
    python nexus_notion_reporter.py

Stop with Ctrl+C — shuts down cleanly.
"""

import json
import logging
import time
from pathlib import Path

from nexus_notion_bridge import push_entry

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [REPORTER] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths (script-relative — works from any working directory) ────────────────
_HERE         = Path(__file__).parent
FEED_PATH     = _HERE / "nexus_cycle_feed.json"
LOGGED_STATE  = _HERE / ".notion_logged_state.json"
POLL_INTERVAL = 60  # seconds


# ── State management ──────────────────────────────────────────────────────────
def load_logged() -> set:
    """
    Load already-synced cycle IDs.
    File format is a JSON list — convert to set for O(1) lookup.
    Returns empty set if file is missing or corrupt.
    """
    if LOGGED_STATE.exists():
        try:
            return set(json.loads(LOGGED_STATE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read logged state — starting fresh.")
            return set()
    return set()


def save_logged(ids: set) -> None:
    """
    Save synced IDs back as a sorted JSON list.
    Preserves existing file format — do not change to dict/object.
    """
    LOGGED_STATE.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


# ── Sync ──────────────────────────────────────────────────────────────────────
def sync_once() -> int:
    """
    Read the feed, push all unsynced entries, return count pushed.

    Race condition guard: nexus_swarm_loop.py and nexus_intel_seeker.py both
    write to nexus_cycle_feed.json concurrently. A mid-write read causes a
    JSONDecodeError — skip this poll and retry next cycle.

    Progress is saved every 10 entries so a kill mid-batch loses at most 9.
    Duplicate entries PATCH instead of creating new rows (bridge is idempotent).
    """
    logged = load_logged()

    try:
        feed = json.loads(FEED_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("Feed mid-write — skipping this poll.")
        return 0
    except OSError as e:
        log.error(f"Cannot read feed: {e}")
        return 0

    new_count = 0
    for entry in feed:
        # Skip guard — entries with blank cycle IDs are malformed
        if not entry.get("cycle", "").strip():
            continue

        cycle_id = entry["cycle"]
        if cycle_id in logged:
            continue

        try:
            page_id = push_entry(entry)
            logged.add(cycle_id)
            new_count += 1
            log.info(f"Synced {cycle_id} → {page_id}")
        except Exception as e:
            log.error(f"Failed {cycle_id}: {e}")

        # Save progress every 10 entries — prevents full re-sync if killed mid-batch
        if new_count > 0 and new_count % 10 == 0:
            save_logged(logged)

    if new_count:
        save_logged(logged)

    return new_count


# ── Run ───────────────────────────────────────────────────────────────────────
def run() -> None:
    log.info(f"NEXUS REPORTER started. Feed: {FEED_PATH}. Polling every {POLL_INTERVAL}s.")
    try:
        while True:
            n = sync_once()
            if n:
                log.info(f"Pushed {n} new entries this poll.")
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Stopped cleanly.")


if __name__ == "__main__":
    run()
