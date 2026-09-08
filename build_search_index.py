#!/usr/bin/env python3
"""Build a semantic search index over the leaderboard's historical trades.

Reads `leaderboard.json`, enriches each ticker with its company name and sector (yfinance,
cached in `ticker_meta.json`), aggregates a member's repeat buys of the same ticker into a
single position, embeds a sector-rich description of each, and writes them to a Milvus Lite
store (`trades_index.db`). Query it with `search_trades.py`.

Run after `build_leaderboard.py`:

    python3 build_leaderboard.py
    python3 build_search_index.py

Ticker enrichment turns opaque symbols ("NVDA") into words a text embedder understands
("NVIDIA Corporation, a Technology company in Semiconductors"), which is what makes a query
like "semiconductor stocks" actually retrieve chip trades. Numeric superlatives ("biggest
winning trade") are not a vector-search problem -- use `search_trades.py --top` for those.
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from search_common import (COLLECTION, EMBED_DIM, INDEX_PATH, META_PATH, METRIC,
                           embed_documents, open_client)

REPO_DIR = Path(__file__).resolve().parent
LEADERBOARD_PATH = REPO_DIR / "leaderboard.json"
FETCH_WORKERS = 8

# Honorifics and titles that add no signal to a member's name and pollute the facet.
TITLES = {"hon.", "hon", "dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.",
          "rep.", "rep", "sen.", "sen"}

# yfinance logs a multi-line block per delisted/renamed ticker; expected, so silence it.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def clean_name(name: str) -> str:
    """Strip honorifics/titles: 'Hon. Richard Dean Dr McCormick' -> 'Richard Dean McCormick'."""
    return " ".join(t for t in name.split() if t.strip(".").lower() not in TITLES)


def iso_date(mmddyyyy: str) -> str | None:
    """'01/20/2026' -> '2026-01-20'; None if unparseable."""
    try:
        return datetime.strptime(mmddyyyy, "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def distinct_tickers(members: list[dict]) -> list[str]:
    return sorted({t["ticker"].upper() for m in members
                   for t in m.get("trade_detail", [])})


def load_ticker_meta(tickers: list[str], refresh: bool, verbose: bool) -> dict:
    """Return {ticker: {name, sector, industry}}, fetching missing ones from yfinance and
    caching to META_PATH. Failures degrade to the bare symbol with an 'Unknown' sector."""
    cache = {}
    if META_PATH.exists() and not refresh:
        cache = json.loads(META_PATH.read_text())
    todo = [t for t in tickers if t not in cache]
    if verbose:
        print(f"tickers: {len(tickers)} total, {len(todo)} to fetch", file=sys.stderr)

    if todo:
        import yfinance as yf

        def fetch(tk: str) -> tuple[str, dict]:
            name, sector, industry = tk, "Unknown", "Unknown"
            try:
                info = yf.Ticker(tk).info
                name = info.get("shortName") or info.get("longName") or tk
                sector = info.get("sector") or sector
                industry = info.get("industry") or industry
            except Exception:
                pass
            return tk, {"name": name, "sector": sector, "industry": industry}

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            for i, (tk, meta) in enumerate(ex.map(fetch, todo), 1):
                cache[tk] = meta
                if verbose and i % 50 == 0:
                    print(f"  enriched {i}/{len(todo)}", file=sys.stderr)
        META_PATH.write_text(json.dumps(cache, indent=0))

    known = sum(1 for t in tickers if cache.get(t, {}).get("sector", "Unknown") != "Unknown")
    if verbose:
        print(f"sector known for {known}/{len(tickers)} tickers", file=sys.stderr)
    return cache


def build_rows(members: list[dict], tmeta: dict) -> list[dict]:
    """Collapse each member's repeat buys of a ticker into one position and render a
    sector-rich description plus the scalar fields used for filtering and sorting."""
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"amount": 0.0, "gain": 0.0, "buys": 0, "dates": [], "realized": False})
    state: dict[tuple, str] = {}
    for m in members:
        for t in m.get("trade_detail", []):
            tk = t["ticker"].upper()
            key = (m["member"], tk)
            a = agg[key]
            a["amount"] += t["amount"]
            a["gain"] += t["gain"]
            a["buys"] += 1
            d = iso_date(t["buy_date"])
            if d:
                a["dates"].append(d)
            if t["type"] == "realized":
                a["realized"] = True
            state[key] = m["state_dist"]

    rows = []
    for (member, tk), a in agg.items():
        info = tmeta.get(tk, {"name": tk, "sector": "Unknown", "industry": "Unknown"})
        name = clean_name(member)
        last = max(a["dates"]) if a["dates"] else ""
        status = "realized/sold" if a["realized"] else "still held (unrealized)"
        text = (f"{name} ({state[(member, tk)]}) invested in {info['name']} ({tk}), "
                f"a {info['sector']} company in {info['industry']}. "
                f"{a['buys']} trade(s), about ${a['amount']:.0f} total invested, "
                f"estimated gain ${a['gain']:.0f}, {status}.")
        rows.append({
            "text": text,
            "member": name[:200],
            "state": state[(member, tk)][:16],
            "ticker": tk[:32],
            "company": info["name"][:128],
            "sector": info["sector"][:48],
            "industry": info["industry"][:64],
            "buys": a["buys"],
            "amount": round(a["amount"], 2),
            "gain": round(a["gain"], 2),
            "realized": a["realized"],
            "last_date": last,
            "last_ts": int(datetime.strptime(last, "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp()) if last else 0,
        })
    return rows


def create_collection(client) -> None:
    """(Re)create the collection with an explicit typed schema. Typed columns are returned
    reliably by query/search; the quick-setup's dynamic ($meta) fields can come back partial
    on a cold-process read, which is fatal for the numeric sort behind --top."""
    from pymilvus import DataType

    if client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)
    schema = client.create_schema(auto_id=True, enable_dynamic_field=False)
    schema.add_field("id", DataType.INT64, is_primary=True)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBED_DIM)
    schema.add_field("text", DataType.VARCHAR, max_length=1024)
    schema.add_field("member", DataType.VARCHAR, max_length=200)
    schema.add_field("state", DataType.VARCHAR, max_length=16)
    schema.add_field("ticker", DataType.VARCHAR, max_length=32)
    schema.add_field("company", DataType.VARCHAR, max_length=128)
    schema.add_field("sector", DataType.VARCHAR, max_length=48)
    schema.add_field("industry", DataType.VARCHAR, max_length=64)
    schema.add_field("buys", DataType.INT64)
    schema.add_field("amount", DataType.FLOAT)
    schema.add_field("gain", DataType.FLOAT)
    schema.add_field("realized", DataType.BOOL)
    schema.add_field("last_date", DataType.VARCHAR, max_length=16)
    schema.add_field("last_ts", DataType.INT64)
    index = client.prepare_index_params()
    index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type=METRIC)
    client.create_collection(COLLECTION, schema=schema, index_params=index)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--leaderboard", type=Path, default=LEADERBOARD_PATH,
                        help="path to leaderboard.json (default: alongside this script)")
    parser.add_argument("--refresh-meta", action="store_true",
                        help="re-fetch every ticker's company/sector, ignoring the cache")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress")
    args = parser.parse_args()
    verbose = not args.quiet

    if not args.leaderboard.exists():
        sys.exit(f"{args.leaderboard} not found -- run build_leaderboard.py first")
    members = json.loads(args.leaderboard.read_text())["members"]

    tmeta = load_ticker_meta(distinct_tickers(members), args.refresh_meta, verbose)
    rows = build_rows(members, tmeta)
    if verbose:
        print(f"aggregated {len(rows)} member+ticker positions", file=sys.stderr)

    vectors = embed_documents([r["text"] for r in rows])

    client = open_client(load=False)
    create_collection(client)
    client.insert(collection_name=COLLECTION,
                  data=[{"vector": vectors[i].tolist(), **rows[i]}
                        for i in range(len(rows))])
    print(f"indexed {len(rows)} positions into {INDEX_PATH.name} "
          f"(dim={EMBED_DIM}, {METRIC})")


if __name__ == "__main__":
    main()
