#!/usr/bin/env python3
"""Build the semantic index over the leaderboard's historical trades.

Reads `leaderboard.json`, enriches each distinct ticker with its company name, sector and
business description (yfinance, cached in `ticker_meta.json`), and writes two collections
to a Milvus Lite store (`trades_index.db`):

    tickers     one row per symbol, vector = the business description
    positions   one row per member+ticker, scalar facts + a copy of the ticker's vector

Run after `build_leaderboard.py`:

    python3 build_leaderboard.py
    python3 build_search_index.py

What gets embedded is the whole point. Embedding a rendered sentence
("... invested in NVIDIA Corporation (NVDA), a Technology company ... estimated gain
$4231 ...") wastes the vector twice over: every row shares that skeleton, so the scores
compress into a narrow band, and the dollar figures are exactly what a text embedder
cannot represent. Embedding the company's *business description* instead means "clean
energy" retrieves Enphase for what it does, while the dollar figures stay scalar columns
for `--min-gain` to filter on. It also cuts the embedding work to one vector per symbol
(~500) rather than one per member+ticker pair (~730).

Numeric superlatives ("biggest winning trade") are still not a vector-search problem --
use `search_trades.py --top` for those.
"""

import argparse
import json
import logging
import shutil
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from house_disclosures import parse_date
from monitor_common import display_name
from search_common import (EMBED_DIM, INDEX_PATH, META_PATH, METRIC, POSITIONS, TICKERS,
                           embed_documents, open_client, truncate_bytes)

REPO_DIR = Path(__file__).resolve().parent
LEADERBOARD_PATH = REPO_DIR / "leaderboard.json"
FETCH_WORKERS = 8
INSERT_BATCH = 500

# bge-small truncates at 512 tokens; anything past roughly this many characters is dropped
# by the tokeniser anyway, and the first paragraph of a business summary is the part that
# actually describes the company.
SUMMARY_EMBED_CHARS = 1200

# yfinance logs a multi-line block per delisted/renamed ticker; expected, so silence it.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def iso_date(mmddyyyy: str) -> str | None:
    """'01/20/2026' -> '2026-01-20'; None if unparseable."""
    parsed = parse_date(mmddyyyy)
    return parsed.strftime("%Y-%m-%d") if parsed else None


def distinct_tickers(members: list[dict]) -> list[str]:
    return sorted({t["ticker"].upper() for m in members
                   for t in m.get("trade_detail", [])})


def load_ticker_meta(tickers: list[str], refresh: bool, verbose: bool) -> dict:
    """Return {ticker: {name, sector, industry, summary}}, fetching missing ones from
    yfinance and caching to META_PATH. Failures degrade to the bare symbol with an
    'Unknown' sector and no summary."""
    cache = {}
    if META_PATH.exists() and not refresh:
        cache = json.loads(META_PATH.read_text())
    # Entries cached before summaries were collected carry no 'summary' key; re-fetch them
    # so an upgrade fills the field in rather than silently indexing sector strings forever.
    todo = [t for t in tickers if "summary" not in cache.get(t, {})]
    if verbose:
        print(f"tickers: {len(tickers)} total, {len(todo)} to fetch", file=sys.stderr)

    if todo:
        import yfinance as yf

        def fetch(tk: str) -> tuple[str, dict]:
            name, sector, industry, summary = tk, "Unknown", "Unknown", ""
            try:
                info = yf.Ticker(tk).info
                name = info.get("shortName") or info.get("longName") or tk
                sector = info.get("sector") or sector
                industry = info.get("industry") or industry
                summary = (info.get("longBusinessSummary") or "").strip()
            except Exception:
                pass
            return tk, {"name": name, "sector": sector, "industry": industry,
                        "summary": summary}

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            for i, (tk, meta) in enumerate(ex.map(fetch, todo), 1):
                cache[tk] = meta
                if verbose and i % 50 == 0:
                    print(f"  enriched {i}/{len(todo)}", file=sys.stderr)
        META_PATH.write_text(json.dumps(cache, indent=0))

    known = sum(1 for t in tickers if cache.get(t, {}).get("sector", "Unknown") != "Unknown")
    described = sum(1 for t in tickers if cache.get(t, {}).get("summary"))
    if verbose:
        print(f"sector known for {known}/{len(tickers)} tickers, "
              f"business description for {described}", file=sys.stderr)
    return cache


def ticker_meta_or_default(tmeta: dict, tk: str) -> dict:
    return tmeta.get(tk, {"name": tk, "sector": "Unknown",
                          "industry": "Unknown", "summary": ""})


def embed_text(tk: str, info: dict) -> str:
    """The string whose embedding represents this symbol.

    Leads with the identifiers so an exact-name query still matches, then the business
    description, which is what carries the thematic signal. A symbol with no description
    degrades to its sector words -- worse, but still better than the bare ticker.
    """
    head = (f"{info['name']} ({tk}), a {info['sector']} company "
            f"in {info['industry']}.")
    summary = info.get("summary") or ""
    return f"{head} {summary[:SUMMARY_EMBED_CHARS]}".strip()


def build_ticker_rows(tickers: list[str], tmeta: dict, generated_at: int) -> list[dict]:
    """One row per distinct symbol. `text` is what gets embedded."""
    rows = []
    for tk in tickers:
        info = ticker_meta_or_default(tmeta, tk)
        rows.append({
            "ticker": truncate_bytes(tk, 32),
            "text": embed_text(tk, info),
            "name": truncate_bytes(info["name"], 128),
            "sector": truncate_bytes(info["sector"], 48),
            "industry": truncate_bytes(info["industry"], 64),
            "summary": truncate_bytes(info.get("summary") or "", 2048),
            "generated_at": generated_at,
        })
    return rows


def build_position_rows(members: list[dict], tmeta: dict, generated_at: int) -> list[dict]:
    """Collapse each member's repeat buys of a ticker into one position, carrying the
    scalar fields used for filtering and sorting plus a human-readable summary line."""
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"amount": 0.0, "gain": 0.0, "buys": 0, "dates": [], "realized": False})
    ident: dict[tuple, tuple[str, str]] = {}
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
            ident[key] = (display_name(m), m["state_dist"])

    rows = []
    for (member, tk), a in agg.items():
        info = ticker_meta_or_default(tmeta, tk)
        name, state = ident[(member, tk)]
        last = max(a["dates"]) if a["dates"] else ""
        status = "realized/sold" if a["realized"] else "still held (unrealized)"
        text = (f"{name} ({state}) invested in {info['name']} ({tk}), "
                f"a {info['sector']} company in {info['industry']}. "
                f"{a['buys']} trade(s), about ${a['amount']:.0f} total invested, "
                f"estimated gain ${a['gain']:.0f}, {status}.")
        rows.append({
            "text": truncate_bytes(text, 1024),
            "member": truncate_bytes(name, 200),
            "state": truncate_bytes(state, 16),
            "ticker": truncate_bytes(tk, 32),
            "company": truncate_bytes(info["name"], 128),
            "sector": truncate_bytes(info["sector"], 48),
            "industry": truncate_bytes(info["industry"], 64),
            "buys": a["buys"],
            "amount": round(a["amount"], 2),
            "gain": round(a["gain"], 2),
            "realized": a["realized"],
            "last_date": last,
            "last_ts": int(datetime.strptime(last, "%Y-%m-%d")
                           .replace(tzinfo=timezone.utc).timestamp()) if last else 0,
            "generated_at": generated_at,
        })
    return rows


def create_collections(client) -> None:
    """Create both collections with explicit typed schemas.

    Typed columns are returned reliably by query/search; the quick-setup's dynamic ($meta)
    fields can come back partial on a cold-process read, which is fatal for the numeric
    sort behind --top.
    """
    from pymilvus import DataType

    index = client.prepare_index_params()
    index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type=METRIC)

    # `ticker` is the primary key so the monitors can fetch a symbol's vector directly
    # instead of searching for it.
    t = client.create_schema(auto_id=False, enable_dynamic_field=False)
    t.add_field("ticker", DataType.VARCHAR, max_length=32, is_primary=True)
    t.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBED_DIM)
    t.add_field("text", DataType.VARCHAR, max_length=4096)
    t.add_field("name", DataType.VARCHAR, max_length=128)
    t.add_field("sector", DataType.VARCHAR, max_length=48)
    t.add_field("industry", DataType.VARCHAR, max_length=64)
    t.add_field("summary", DataType.VARCHAR, max_length=2048)
    t.add_field("generated_at", DataType.INT64)
    client.create_collection(TICKERS, schema=t, index_params=index)

    p = client.create_schema(auto_id=True, enable_dynamic_field=False)
    p.add_field("id", DataType.INT64, is_primary=True)
    p.add_field("vector", DataType.FLOAT_VECTOR, dim=EMBED_DIM)
    p.add_field("text", DataType.VARCHAR, max_length=1024)
    p.add_field("member", DataType.VARCHAR, max_length=200)
    p.add_field("state", DataType.VARCHAR, max_length=16)
    p.add_field("ticker", DataType.VARCHAR, max_length=32)
    p.add_field("company", DataType.VARCHAR, max_length=128)
    p.add_field("sector", DataType.VARCHAR, max_length=48)
    p.add_field("industry", DataType.VARCHAR, max_length=64)
    p.add_field("buys", DataType.INT64)
    p.add_field("amount", DataType.FLOAT)
    p.add_field("gain", DataType.FLOAT)
    p.add_field("realized", DataType.BOOL)
    p.add_field("last_date", DataType.VARCHAR, max_length=16)
    p.add_field("last_ts", DataType.INT64)
    p.add_field("generated_at", DataType.INT64)
    client.create_collection(POSITIONS, schema=p, index_params=index)


def insert_batched(client, collection: str, rows: list[dict]) -> None:
    """Insert in batches. One `insert()` of every row trips Milvus' request-size limit as
    the corpus grows, and a failure halfway through a single call is indistinguishable
    from a failure at the start."""
    for i in range(0, len(rows), INSERT_BATCH):
        client.insert(collection_name=collection, data=rows[i:i + INSERT_BATCH])


def swap_index(staged: Path, live: Path) -> None:
    """Move a freshly built store into place, replacing any existing one.

    The build runs against a staging directory and only lands here, so a crash or an
    oversized field leaves the previous index serving instead of dropping the collection
    and failing before the re-insert -- which left no index at all and no way back except
    a full re-embed.
    """
    retired = live.with_name(live.name + ".old")
    if retired.exists():
        shutil.rmtree(retired)
    if live.exists():
        live.rename(retired)
    staged.rename(live)
    if retired.exists():
        shutil.rmtree(retired)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--leaderboard", type=Path, default=LEADERBOARD_PATH,
                        help="path to leaderboard.json (default: alongside this script)")
    parser.add_argument("--refresh-meta", action="store_true",
                        help="re-fetch every ticker's company/sector/description")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress progress")
    args = parser.parse_args()
    verbose = not args.quiet

    if not args.leaderboard.exists():
        sys.exit(f"{args.leaderboard} not found -- run build_leaderboard.py first")
    board = json.loads(args.leaderboard.read_text())
    members = board["members"]

    # Freshness travels with the index so a reader can say how stale its answer is; it is
    # the leaderboard's own stamp, because that is the data actually being described.
    stamp = parse_date_stamp(board.get("generated_at"))

    tickers = distinct_tickers(members)
    tmeta = load_ticker_meta(tickers, args.refresh_meta, verbose)
    trows = build_ticker_rows(tickers, tmeta, stamp)
    prows = build_position_rows(members, tmeta, stamp)
    if verbose:
        print(f"embedding {len(trows)} tickers for {len(prows)} member+ticker positions",
              file=sys.stderr)

    vectors = embed_documents([r["text"] for r in trows])
    by_ticker = {trows[i]["ticker"]: vectors[i].tolist() for i in range(len(trows))}

    # Milvus Lite validates the URI and insists on a `.db` suffix, so the staging store
    # is `trades_index.new.db`, not `trades_index.db.new`.
    staged = INDEX_PATH.with_name(INDEX_PATH.stem + ".new.db")
    if staged.exists():
        shutil.rmtree(staged)
    client = open_client(load=False, path=staged)
    try:
        create_collections(client)
        insert_batched(client, TICKERS,
                       [{**r, "vector": by_ticker[r["ticker"]]} for r in trows])
        insert_batched(client, POSITIONS,
                       [{**r, "vector": by_ticker[r["ticker"]]} for r in prows])
    finally:
        client.close()
    swap_index(staged, INDEX_PATH)

    print(f"indexed {len(trows)} tickers and {len(prows)} positions into "
          f"{INDEX_PATH.name} (dim={EMBED_DIM}, {METRIC})")


def parse_date_stamp(value) -> int:
    """The leaderboard's ISO `generated_at` as a unix timestamp; 0 when absent."""
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


if __name__ == "__main__":
    main()
