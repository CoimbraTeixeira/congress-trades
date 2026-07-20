#!/usr/bin/env python3
"""Snapshot the current top-10 (by leaderboard.json) into watchlist.json, keyed by
the raw (Last, First, StateDst) fields so watchlist_monitor.py can match future
index rows without re-parsing names."""

import json
from datetime import datetime, timezone
from pathlib import Path

from house_disclosures import PTR_FILING_TYPE, fetch_index, format_filer_name

REPO_DIR = Path(__file__).resolve().parent
LEADERBOARD_PATH = REPO_DIR / "leaderboard.json"
WATCHLIST_PATH = REPO_DIR / "watchlist.json"


def main() -> None:
    leaderboard = json.loads(LEADERBOARD_PATH.read_text())
    priced = [m for m in leaderboard["members"] if m["return_pct_estimate"] is not None]
    top10 = sorted(priced, key=lambda m: m["return_pct_estimate"], reverse=True)[:10]

    rows = fetch_index(leaderboard["year"])
    ptr_rows = [r for r in rows if r.get("FilingType") == PTR_FILING_TYPE]
    by_name_state = {}
    for r in ptr_rows:
        by_name_state.setdefault((format_filer_name(r), r.get("StateDst")), r)

    watched = []
    unmatched = []
    for m in top10:
        row = by_name_state.get((m["member"], m["state_dist"]))
        if row:
            watched.append({
                "member": m["member"],
                "last": row.get("Last", ""),
                "first": row.get("First", ""),
                "state_dist": row.get("StateDst", ""),
                "return_pct_estimate": m["return_pct_estimate"],
            })
        else:
            unmatched.append(m["member"])

    WATCHLIST_PATH.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_leaderboard_generated_at": leaderboard["generated_at"],
        "members": watched,
    }, indent=2))
    print(f"Wrote {len(watched)} member(s) to {WATCHLIST_PATH}")
    if unmatched:
        print(f"WARNING: could not match {len(unmatched)} member(s): {unmatched}")


if __name__ == "__main__":
    main()
