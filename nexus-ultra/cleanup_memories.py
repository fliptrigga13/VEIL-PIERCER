"""
cleanup_memories.py — One-time script to archive duplicate CLOSER_CONVERSION memories.

Run once from any directory. Safe: sets archived=1, does NOT delete rows.
After running, active memories drop from ~9,690 to ~640, improving recall quality.

Usage:
    python cleanup_memories.py
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "nexus_mind.db"
CUTOFF  = "2026-03-29T21:30:00"

WHERE = """
    tags LIKE '%closer_conversion%'
    AND agent = 'INTEL_SEEKER'
    AND created_at < ?
"""


def main() -> None:
    if not DB_PATH.exists():
        print(f"[ERROR] Database not found: {DB_PATH}")
        print("Make sure you copied this script into your nexus-ultra/ directory.")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        total  = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE archived IS NULL OR archived != 1"
        ).fetchone()[0]
        target = conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE {WHERE}", (CUTOFF,)
        ).fetchone()[0]

        print(f"Total memories  : {total}")
        print(f"Active memories : {active}")
        print(f"Rows to archive : {target}  (CLOSER_CONVERSION bloat from INTEL_SEEKER v1)")

        if target == 0:
            print("\nNothing to archive — already clean.")
            return

        confirm = input(f"\nArchive {target} rows? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted. No changes made.")
            return

        conn.execute(f"UPDATE memories SET archived = 1 WHERE {WHERE}", (CUTOFF,))
        conn.commit()

        active_after = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE archived IS NULL OR archived != 1"
        ).fetchone()[0]
        print(f"\nDone.")
        print(f"Active memories before : {active}")
        print(f"Active memories after  : {active_after}")
        print(f"Archived               : {active - active_after}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
