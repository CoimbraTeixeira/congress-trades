#!/usr/bin/env python3
"""Snapshot the current top-N ranked members from leaderboard.json into watchlist.json.

Identity is carried through as the raw (Last, First, StateDst) index fields that
build_leaderboard.py records, so watchlist_monitor.py can match future index rows
directly -- no round-tripping through a formatted display name.

Ranked on alpha by default rather than raw return: in a rising market the raw figure
promotes whoever held the most beta, which is the opposite of what a watchlist is for.
--rank-by return restores the old behaviour.
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
    parser.add_argument("--rank-by", choices=("alpha", "return"), default="alpha",
                        help="Rank by estimated alpha against the benchmark (default) "
                             "or by raw estimated return")
    args = parser.parse_args()

    if not LEADERBOARD_PATH.exists():
        sys.exit(f"Missing {LEADERBOARD_PATH} -- run build_leaderboard.py first")
    leaderboard = json.loads(LEADERBOARD_PATH.read_text())

    ranked = [m for m in leaderboard["members"] if m.get("ranked")]
    if not ranked:
        sys.exit("No ranked members in the leaderboard -- nothing to watch. "
                 "Lower --min-invested/--min-trades in build_leaderboard.py.")
    # A member whose lots the benchmark could not price has no alpha; rank them on raw
    # return rather than dropping them off the board entirely.
    def rank_key(m):
        if args.rank_by == "alpha" and m.get("alpha_pct_estimate") is not None:
            return m["alpha_pct_estimate"]
        return m["return_pct_estimate"]

    top = sorted(ranked, key=rank_key, reverse=True)[:args.top]

    missing = [m["member"] for m in top if not m.get("last")]
    if missing:
        sys.exit(f"Leaderboard entries lack raw identity fields: {missing}. "
                 f"Regenerate leaderboard.json with the current build_leaderboard.py.")

    WATCHLIST_PATH.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_leaderboard_generated_at": leaderboard["generated_at"],
        "rank_by": args.rank_by,
        "benchmark_ticker": leaderboard.get("benchmark_ticker"),
        "members": [
            {
                "member": m["member"],
                "last": m["last"],
                "first": m["first"],
                "state_dist": m["state_dist"],
                "return_pct_estimate": m["return_pct_estimate"],
                "alpha_pct_estimate": m.get("alpha_pct_estimate"),
                "benchmark_return_pct_estimate": m.get("benchmark_return_pct_estimate"),
            }
            for m in top
        ],
    }, indent=2))
    print(f"Wrote {len(top)} member(s) to {WATCHLIST_PATH}, ranked by {args.rank_by} "
          f"(of {len(ranked)} ranked, {len(leaderboard['members'])} total)")


if __name__ == "__main__":
    main()
