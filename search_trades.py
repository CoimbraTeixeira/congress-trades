#!/usr/bin/env python3
"""Semantic + filtered search over the historical congress trades index.

Build the index first with `build_search_index.py`, then:

    python3 search_trades.py "semiconductor and chip stocks"
    python3 search_trades.py "profitable tech bets" --sector Technology --min-gain 5000
    python3 search_trades.py "energy holdings" --sector Energy --held
    python3 search_trades.py --member Pelosi                 # facet only, no query
    python3 search_trades.py --top 10                        # biggest gainers (metadata sort)

`--sector`, `--min-gain`/`--max-gain`, `--held`/`--sold` and `--member` compose a scalar
filter that runs alongside the vector query. `--top` ignores the embedding entirely: ranking
by magnitude is a metadata sort, not something a text embedder can do.
"""

import argparse
import sys
from datetime import datetime, timezone

from search_common import POSITIONS, embed_query, open_client, quote_literal

FIELDS = ["member", "state", "company", "ticker", "sector",
          "gain", "amount", "buys", "last_date", "realized", "generated_at"]


def build_filter(sector=None, min_gain=None, max_gain=None,
                 held=None, member=None) -> str:
    """Compose a Milvus boolean expression from the CLI facets (empty string = no filter)."""
    clauses = []
    if sector:
        clauses.append(f"sector == {quote_literal(sector)}")
    if min_gain is not None:
        clauses.append(f"gain >= {min_gain}")
    if max_gain is not None:
        clauses.append(f"gain <= {max_gain}")
    if held is True:
        clauses.append("realized == False")
    elif held is False:
        clauses.append("realized == True")
    if member:
        clauses.append(f"member like {quote_literal('%' + member + '%')}")
    return " && ".join(clauses)


def freshness(rows: list[dict]) -> str:
    """'(indexed 2026-09-12, 1 day old)' -- results look current whether or not they are,
    so the index's own age has to be printed beside them."""
    stamps = [r.get("generated_at", 0) for r in rows if r.get("generated_at")]
    if not stamps:
        return ""
    built = datetime.fromtimestamp(max(stamps), timezone.utc)
    days = (datetime.now(timezone.utc) - built).days
    age = "today" if days < 1 else f"{days} day{'s' if days != 1 else ''} old"
    return f"  (indexed {built:%Y-%m-%d}, {age})"


def fmt(hit_entity: dict, score: float | None) -> str:
    e = hit_entity
    held = "held" if not e["realized"] else "sold"
    lead = f"[{score:.3f}] " if score is not None else ""
    return (f"  {lead}{e['member']:24.24} {e['state']:5} "
            f"{e['company']:26.26} {e['sector']:20.20} "
            f"${e['gain']:>11,.0f}  {e['buys']}x {held}")


def run_top(client, args, flt):
    rows = client.query(POSITIONS, filter=flt or "gain > 0",
                        output_fields=FIELDS, limit=16000)
    rows.sort(key=lambda r: r["gain"], reverse=True)
    print(f"Top {args.top} gainers"
          + (f" (filter: {flt})" if flt else "") + ":" + freshness(rows))
    for r in rows[:args.top]:
        print(fmt(r, None))


def run_search(client, args, flt):
    res = client.search(POSITIONS, data=embed_query(args.query), limit=args.limit,
                        filter=flt, output_fields=FIELDS)[0]
    entities = [h["entity"] for h in res]
    print(f'Results for "{args.query}"' + (f" (filter: {flt})" if flt else "") + ":"
          + freshness(entities))
    if not res:
        print("  (no matches)")
    for h in res:
        print(fmt(h["entity"], h["distance"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", nargs="?", help="natural-language search text")
    parser.add_argument("--sector", help='exact sector, e.g. "Technology", "Energy"')
    parser.add_argument("--min-gain", type=float, help="minimum estimated gain in dollars")
    parser.add_argument("--max-gain", type=float, help="maximum estimated gain in dollars")
    parser.add_argument("--member", help="substring match on member name")
    held = parser.add_mutually_exclusive_group()
    held.add_argument("--held", dest="held", action="store_true", default=None,
                      help="only positions still held (unrealized)")
    held.add_argument("--sold", dest="held", action="store_false",
                      help="only realized/sold positions")
    parser.add_argument("-n", "--limit", type=int, default=5, help="max results (default 5)")
    parser.add_argument("--top", type=int, metavar="N",
                        help="skip the query; list the N biggest gainers by metadata sort")
    args = parser.parse_args()

    flt = build_filter(args.sector, args.min_gain, args.max_gain, args.held, args.member)
    client = open_client()
    if not client.has_collection(POSITIONS):
        sys.exit("no index found -- run build_search_index.py first")

    if args.top is not None:
        run_top(client, args, flt)
    elif args.query:
        run_search(client, args, flt)
    elif flt:                                   # facet-only browse, ranked by gain
        rows = client.query(POSITIONS, filter=flt, output_fields=FIELDS, limit=args.limit)
        rows.sort(key=lambda r: r["gain"], reverse=True)
        print(f"Matches (filter: {flt}):" + freshness(rows))
        for r in rows:
            print(fmt(r, None))
    else:
        parser.error("give a query, a --sector/--member/... filter, or --top N")


if __name__ == "__main__":
    main()
