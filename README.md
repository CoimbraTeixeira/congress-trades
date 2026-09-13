# congress-trades

Tracks US House stock trades from the Clerk's public Periodic Transaction Report
(PTR) disclosures: Discord alerts on new filings, plus an estimated performance
leaderboard and a static dashboard.

Data comes from the House Clerk's bulk archive (`disclosures-clerk.house.gov`) —
no auth, no scraping of rate-limited pages. **House only**: the Senate's eFD site
prohibits bulk/automated access.

## Setup

```bash
pip install -r requirements.txt
pip install -r requirements-search.txt  # optional: semantic search (heavier, pulls torch)
sudo apt install poppler-utils          # provides pdftotext, used to read the PDFs
```

Discord delivery reads a webhook from `~/.config/fare-monitor/config.json` (shared with
the fare-monitor project). The webhook is a bearer credential — anyone holding it can
post to the channel — so keep it out of the repo and readable only by you:

```bash
mkdir -p ~/.config/fare-monitor
echo '{"webhook_url": "https://discord.com/api/webhooks/..."}' > ~/.config/fare-monitor/config.json
chmod 600 ~/.config/fare-monitor/config.json

git config core.hooksPath hooks   # enable the pre-commit secret guard
```

This repo is public, and the design keeps the webhook out of it entirely. `hooks/pre-commit`
enforces that: it scans staged content for webhook URLs, API tokens and private keys, and
refuses the commit if it finds one. Git does not enable repo-tracked hooks automatically, so
the `core.hooksPath` line above is required once per clone. Bypass a false positive with
`git commit --no-verify`.

## The pieces

| Script | What it does |
|---|---|
| `house_disclosures.py` | Shared library: fetch the index, pull a PTR PDF, parse its transaction table |
| `monitor_common.py` | Shared monitor plumbing: webhook config, seen-DocID state, Discord delivery |
| `congress_monitor.py` | Alerts on **every** new PTR filing containing a stock trade |
| `build_leaderboard.py` | Estimates per-member returns and alpha vs SPY → `leaderboard.json` |
| `build_watchlist.py` | Snapshots the top-N ranked members → `watchlist.json` |
| `watchlist_monitor.py` | Alerts only when a watchlist member files a **purchase**, with a theme note when the index is built |
| `daily_summary.py` | One consolidated daily digest of the top-N's trades, buys and sells, incl. themed roll-ups |
| `build_dashboard.py` | Renders `leaderboard.json` → self-contained `dashboard.html` |
| `build_search_index.py` | Embeds each ticker's business description → Milvus Lite `trades_index.db` (`tickers` + `positions`) |
| `search_trades.py` | Semantic + filtered CLI search over the trade index |
| `search_common.py` | Shared index plumbing: embeddings, clustering, and the read-only `ThemeIndex` the monitors use |

Typical order:

```bash
python3 congress_monitor.py --dry-run     # first run seeds a baseline, no alerts
python3 build_leaderboard.py              # ~1-2 min: fetches every PTR + prices
python3 build_watchlist.py
python3 build_dashboard.py
python3 build_search_index.py             # optional: ~20s, builds the semantic search index
python3 watchlist_monitor.py --dry-run
python3 daily_summary.py --dry-run
```

## What an alert looks like

```
🗳️ **Hon. Shri Thanedar** (MI13) — new trade disclosure
Filed 8/13/2026 · 2 transactions (2 sells)

🔴 **SELL** **AAPL** Apple · $100k-$250k · traded 01/09/2026 · partial · ⏰ 216d
🔴 **SELL** **MSTR** Strategy · $15k-$50k · traded 10/21/2025 · ⏰ 296d

_⏰ = disclosed more than 45 days after the trade, past the STOCK Act deadline_
Full PTR: https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20033910.pdf
```

The filing's own vocabulary is translated rather than passed through, because none of it
means anything on a phone screen: owner code `SP` becomes `spouse`, asset class `[OP]`
becomes `[option]`, and `$15,001 - $50,000` becomes `$15k-$50k` — the disclosure is
only ever a bracket, so the `,001` is an artefact of how the brackets abut, not a figure.
Ticker leads the asset because that is what a reader scans for; the filing buries it at
the end of a long legal name. Buys are grouped before sells, since the PDF's row order is
however the filer's broker listed them. `⏰` marks a late disclosure and carries the days
taken; the deadline it missed is stated once at the foot rather than in every line.

## Stocks only

**Every alert covers stocks and stock options only.** A PTR discloses a member's whole
portfolio activity, and most of that volume is not a stock pick: Treasury notes, municipal
bonds, agency paper, target-date funds, private credit vehicles. Across the 60 most recent
PTRs that is 43 of 411 transaction rows, and it silences 15 of 51 readable filings outright
— roughly a third of the alert traffic was somebody's bond ladder rolling over.

The filing's own asset-type code decides it: `ST` (stock) and `OP` (option) alert, every
other code is dropped. Where the PDF yields no code — it wraps onto a continuation line
often enough to matter — a printed ticker stands in, so an untyped `(AAPL)` row still
alerts and an untyped `US Treasury Note 3.5%` does not. A *known* non-equity code always
wins over a ticker, which is what keeps `SpaceX (SPCX) [OT]` out: that's the filer's own
shorthand for a private holding, not a listed symbol.

A filing whose trades were all non-equity sends nothing and is recorded as handled. A
filing whose **PDF wouldn't parse** is a different case and still alerts, because "no
stock trades" and "we couldn't tell" must not look the same from the outside.

Pass `--all-assets` to any of the three to get the unfiltered feed back:

```bash
python3 congress_monitor.py --dry-run --all-assets
```

Options still carry their `[option]` tag, since "bought Nvidia" and "bought Nvidia calls"
are different events. Non-equities remain counted as trades in the leaderboard's volume
figures and excluded from its returns, per *Reading the numbers* below — the filter is
about what gets pushed to you, not about what the numbers are built from.

## Semantic search

`build_search_index.py` turns the leaderboard's trade history into a vector index you can
query in plain English, and which the monitors read to add theme context to their alerts.
It enriches each distinct ticker with its company name, sector and **business
description** (yfinance, cached in `ticker_meta.json`), embeds that with
`BAAI/bge-small-en-v1.5`, and writes two collections to a local
[Milvus Lite](https://milvus.io/docs/milvus_lite.md) store (`trades_index.db`) — no server:

| Collection | Rows | Vector |
| --- | --- | --- |
| `tickers` | one per symbol (~500) | the company's business description |
| `positions` | one per member+ticker (~730) | a copy of its ticker's vector |

Embedding the *business description* rather than a rendered sentence is the whole point.
A sentence like "… invested in NVIDIA Corporation (NVDA), a Technology company …
estimated gain $4231 …" wastes the vector twice: every row shares that skeleton, so the
scores compress into a narrow band, and the dollar figures are exactly what a text
embedder cannot represent. Describing the business instead means "clean energy" retrieves
Enphase for what it *does*, while the dollar figures stay scalar columns for `--min-gain`
to filter on. It also cuts the embedding work to one vector per symbol rather than one per
member+ticker pair.

The deps are heavier than the monitors, so they live in their own file:

```bash
pip install -r requirements-search.txt
python3 build_leaderboard.py         # produces leaderboard.json
python3 build_search_index.py        # ~20s: enrich + embed + index
```

Then query it:

```bash
python3 search_trades.py "semiconductor and chip stocks"
python3 search_trades.py "profitable tech bets" --sector Technology --min-gain 5000
python3 search_trades.py "energy holdings" --sector Energy --held
python3 search_trades.py --member Pelosi        # facet browse, no query
python3 search_trades.py --top 10               # biggest gainers
```

Similarity is measured on **mean-centred** vectors. Raw cosine is unusable here: every
business summary shares the same corporate register, so unrelated pairs floor around 0.60
and `INTC`/`MSFT` (0.75) outscore `NVDA`/`AMD` (0.74). Subtracting the corpus mean removes
that common component — unrelated pairs fall to ~0.00 while same-industry pairs hold
0.25–0.46, which is what makes a threshold meaningful at all.

`--sector`, `--min-gain`/`--max-gain`, `--held`/`--sold` and `--member` add scalar filters
that run alongside the vector query, and results carry the index's own build date so a
stale answer says so. **`--top` deliberately ignores the embedding:** ranking by dollar
magnitude is a metadata sort, not something a text model can do — asking for "the biggest
winning trade" in prose returns semantically-similar trades, not the largest, so that
question is answered by sorting the `gain` field instead.

### Themes in the alerts

The index is not only for the CLI. When it is present:

- **`daily_summary.py`** adds a themed roll-up beside the most-traded line:
  `🟢 Bought across Semiconductors: 4 of 4 members, ~$130k (NVDA, AMD, AVGO, MU)`.
  The existing roll-up matches on *equality*, so four members buying four different chip
  names share no ticker and produce no line at all. Clustering catches exactly that case,
  and a denser digest is also less likely to hit the truncation ceiling.
- **`watchlist_monitor.py`** places a buy against what the member already holds:
  `🧭 More Healthcare — MRNA sits alongside their existing TEM`, or
  `🧭 New direction — nothing in this member's 10 existing position(s) resembles XOM`.
  An exact-ticker lookup cannot answer this; it has no way to know `ENPH` relates to `FSLR`.

Both are **optional context**. The alert paths never load the embedding model — they read
vectors already in the store — and a missing, unbuilt or unreadable index costs them the
extra line and nothing else. A theme is also only named when its members genuinely share
an industry, because a confident wrong label is worse than no label.

`leaderboard.json`, `trades_index.db` and `ticker_meta.json` are all gitignored, regenerated
artefacts.

## The daily digest

`daily_summary.py` posts one Discord message a day covering the top-N leaderboard
members: a roll-up of tickers more than one member touched, then a per-member buy/sell
breakdown with PDF links. Where the two monitors alert per filing as it lands, this is
the once-a-day read across the wider field.

```bash
python3 daily_summary.py --top 50                  # default: ranked by alpha
python3 daily_summary.py --top 50 --rank-by return # raw return, ignoring the index
python3 daily_summary.py --top 50 --rank-by volume # rank by dollars traded instead
python3 daily_summary.py --post-empty              # post even on a quiet day
```

**`--top 50` will usually cover fewer than 50 members.** Ranking by alpha or return
needs a computable return, and most members are sell-only in a given year — as of the
last build, 79 members have a priced trade and 26 clear the leaderboard's
`--min-invested`/`--min-trades` thresholds. The digest states the shortfall in its own
header rather than quietly reporting on a smaller field. For a true 50, either use
`--rank-by volume` (ranks by dollars traded, which every filer has) or lower the
leaderboard thresholds.

Scope is "filings not yet summarised", not "filings dated today": the Clerk's archive
publishes on its own schedule and a filing routinely appears days after its filing
date, so a fixed 24-hour window would drop it. A quiet day posts nothing unless
`--post-empty` is set.

Both monitors take `--verbose`, `--dry-run` (print, don't post or record state),
`--all-assets` (see *Stocks only* above) and `--max-alerts N` (default 25; the oldest N
are handled and the excess is left for the next run rather than dropped).
`watchlist_monitor.py` also takes `--baseline-window N` (default 7) — see below.

The first run of a monitor records every existing filing as "seen" and sends
nothing, so you don't get alerted on the whole back catalogue. State lives in
`state/` and is written atomically; a corrupt state file is quarantined to
`*.corrupt` and the next run re-seeds a baseline instead of re-alerting on everything.

Both monitors read the previous year's index as well as the current one for the first
60 days of a new year. Each year is a separate archive, so a PTR filed on Dec 30 stays
in the old one and a monitor reading only `datetime.now().year` goes blind to it the
moment the calendar rolls over.

## Why an alert might not fire

Alert volume is dominated by the watchlist filter — ten members out of ~60 who file,
buys only — so a few a week is the expected rate, not a fault. The things that were
silently costing alerts, and what they do now:

- A **newly promoted** member's existing filings are baselined away so a watchlist
  rotation doesn't replay their back catalogue. But the trade that earned them the slot
  is often days old, so filings inside `--baseline-window` (default 7 days) still alert.
- A filing containing **no stock trades** is silent by design — see *Stocks only* above.
  A third of filings are in this category, so this is now the most common reason.
  `--all-assets` turns it off.
- A PTR whose **PDF won't fetch or parse** used to yield zero purchases, which is
  indistinguishable from a filing with no purchases in it. It now sends an alert saying
  so, with the link, rather than nothing.
- `--max-alerts` truncation now keeps the **oldest** filings; the archive's row order
  isn't chronological, so it previously kept an arbitrary slice.

## Schedule

```cron
30 12 * * * ( python3 build_leaderboard.py && python3 build_watchlist.py && python3 build_search_index.py ) >> rebuild.log 2>&1
 0 13 * * * python3 congress_monitor.py    >> monitor.log 2>&1
30 13 * * * python3 watchlist_monitor.py   >> watchlist_monitor.log 2>&1
 0 14 * * * python3 daily_summary.py       >> summary.log 2>&1
```

The rebuild runs first so the watchlist and the digest's top-N reflect the current
leaderboard rather than whoever was ranked the day the files were last written by hand.
Because that rotates the membership daily, both the watchlist monitor and the digest
record *last run's* membership and re-baseline anyone who leaves and comes back — a
growing all-time set would replay a returning member's entire absence in one burst.

`build_search_index.py` is last in the chain because it reads `leaderboard.json`. It is
chained rather than scheduled separately so the index can never describe a leaderboard
that no longer exists; if the search deps are not installed, drop that one command and the
monitors simply run without theme lines. The rebuild writes to a staging store and swaps it
into place only on success, so a failure leaves the previous index serving rather than
deleting it and failing before the replacement is written.

## Tests

```bash
python3 -m unittest discover -s tests
```

`tests/fixtures/ptr_sample.txt` is real `pdftotext -layout` output stitched from
three 2026 filings, each of which breaks a naive line-oriented parser differently:
a wrapped asset name, an `S (partial)` transaction type, and a record split across
a page break.

`tests/test_monitors.py` covers the other half — the ways a real filing can end up
silently not alerted (wrong index year, baselined on promotion, read as "no purchases"
when it was really unreadable).

`tests/test_leaderboard.py` covers the scoring engine: the FIFO matcher, the price
lookup's boundaries, and the benchmark comparison. The parser had tests from the start
and the scorer had none, which is how a trade dated before the fetched price window
came to be priced at the first close of the filing year — silently wrong rather than
merely missing, for 23% of matched lots. Both halves of that fix have a regression test
here.

## Reading the numbers

Every figure is an **estimate**, and the estimate is loose by construction. The
STOCK Act only requires a dollar *range* (`$15,001 - $50,000`), never an exact
amount or a share count.

- A trade's value is the **midpoint** of its disclosed range, converted to
  estimated shares at that day's close.
- Buys and sells of a ticker are **FIFO-matched in share space**, so a realized
  gain is `shares × (sell_price − buy_price)`. (Matching in dollar space instead
  would net a buy's cost against a sale's proceeds — quoted at different prices,
  so not the same unit.)
- Every lot is also scored against **SPY over its own holding window**, weighted by
  the same dollars, giving `alpha_pct_estimate`. This is the ranking metric, and it is
  the one that makes the return legible at all: +9% in a year the index returned +15%
  is underperformance, and ranking on the raw figure promotes whoever held the most
  beta rather than whoever picked well. Cisneros is the standing example — +7.5%
  return, and SPY did +7.8% over the same windows.
- The price window opens at the **earliest transaction actually disclosed**, not at
  January 1. Filings routinely report prior-year trades (the 45-day deadline alone
  guarantees it, and late filers stretch it to months), and a price outside the window
  is reported as `unpriced_trades` rather than answered with the first close we happen
  to hold. Anything older than five years is treated as unpriceable rather than
  widening the fetch without bound.
- Sales exceeding matched buys were opened before the window, have unknown cost
  basis, and are excluded. Many members are sell-only in a given year and so have
  no computable return at all.
- Unsold shares are marked to the latest close — hypothetical, not cashed out.
  All prices come from the same split/dividend-adjusted series, so history and
  "today" are comparable.
- Non-equity assets (treasuries, municipal bonds, funds, crypto) are counted as
  trades but excluded from gains. Ticker resolution by company name is gated on
  the filing's asset-type code, because a name lookup will cheerfully match
  "US Treasury Note" to some unrelated equity.
- Members are **ranked** only above `--min-invested` / `--min-trades` thresholds;
  otherwise a single lucky $15k trade tops the board. Unranked members still
  appear in `leaderboard.json` and greyed in the dashboard table.
- `build_watchlist.py` and `daily_summary.py` both rank on alpha by default and take
  `--rank-by return` to go back to the raw figure. A member whose lots the benchmark
  could not price falls back to the raw return rather than dropping off the board.
- Filers have up to 45 days to report, so recent trades may not appear yet.

Not investment advice.
