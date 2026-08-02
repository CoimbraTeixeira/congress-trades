#!/usr/bin/env python3
"""Snapshot the current top-N ranked members from leaderboard.json into watchlist.json.

Identity is carried through as the raw (Last, First, StateDst) index fields that
build_leaderboard.py records, so watchlist_monitor.py can match future index rows
directly -- no round-tripping through a formatted display name.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
LEADERBOARD_PATH = REPO_DIR / "leaderboard.json"
WATCHLIST_PATH = REPO_DIR / "watchlist.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=10, help="How many members to watch")
    args = parser.parse_args()

    if not LEADERBOARD_PATH.exists():
        sys.exit(f"Missing {LEADERBOARD_PATH} -- run build_leaderboard.py first")
    leaderboard = json.loads(LEADERBOARD_PATH.read_text())

    ranked = [m for m in leaderboard["members"] if m.get("ranked")]
    if not ranked:
        sys.exit("No ranked members in the leaderboard -- nothing to watch. "
                 "Lower --min-invested/--min-trades in build_leaderboard.py.")
    top = sorted(ranked, key=lambda m: m["return_pct_estimate"], reverse=True)[:args.top]

    missing = [m["member"] for m in top if not m.get("last")]
    if missing:
        sys.exit(f"Leaderboard entries lack raw identity fields: {missing}. "
                 f"Regenerate leaderboard.json with the current build_leaderboard.py.")

    WATCHLIST_PATH.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_leaderboard_generated_at": leaderboard["generated_at"],
        "members": [
            {
                "member": m["member"],
                "last": m["last"],
                "first": m["first"],
                "state_dist": m["state_dist"],
                "return_pct_estimate": m["return_pct_estimate"],
            }
            for m in top
        ],
    }, indent=2))
    print(f"Wrote {len(top)} member(s) to {WATCHLIST_PATH} "
          f"(of {len(ranked)} ranked, {len(leaderboard['members'])} total)")


if __name__ == "__main__":
    main()
