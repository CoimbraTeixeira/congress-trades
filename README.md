# congress-trades

Tracks US House stock trades from the Clerk's public Periodic Transaction Report
(PTR) disclosures: Slack alerts on new filings, plus an estimated performance
leaderboard and a static dashboard.

Data comes from the House Clerk's bulk archive (`disclosures-clerk.house.gov`) —
no auth, no scraping of rate-limited pages. **House only**: the Senate's eFD site
prohibits bulk/automated access.

## Setup

```bash
pip install -r requirements.txt
sudo apt install poppler-utils          # provides pdftotext, used to read the PDFs
```

Slack delivery reads a webhook from `~/.config/fare-monitor/config.json`:

```json
{ "webhook_url": "https://hooks.slack.com/services/..." }
```

## The pieces

| Script | What it does |
|---|---|
| `house_disclosures.py` | Shared library: fetch the index, pull a PTR PDF, parse its transaction table |
| `monitor_common.py` | Shared monitor plumbing: webhook config, seen-DocID state, Slack delivery |
| `congress_monitor.py` | Alerts on **every** new PTR filing |
| `build_leaderboard.py` | Estimates per-member returns → `leaderboard.json` |
| `build_watchlist.py` | Snapshots the top-N ranked members → `watchlist.json` |
| `watchlist_monitor.py` | Alerts only when a watchlist member files a **purchase** |
| `build_dashboard.py` | Renders `leaderboard.json` → self-contained `dashboard.html` |

Typical order:

```bash
python3 congress_monitor.py --dry-run     # first run seeds a baseline, no alerts
python3 build_leaderboard.py              # ~1-2 min: fetches every PTR + prices
python3 build_watchlist.py
python3 build_dashboard.py
python3 watchlist_monitor.py --dry-run
```

Both monitors take `--verbose`, `--dry-run` (print, don't post or record state) and
`--max-alerts N` (default 25; the excess is left for the next run rather than dropped).

The first run of a monitor records every existing filing as "seen" and sends
nothing, so you don't get alerted on the whole back catalogue. State lives in
`state/` and is written atomically; a corrupt state file is quarantined to
`*.corrupt` and the next run re-seeds a baseline instead of re-alerting on everything.

## Tests

```bash
python3 -m unittest discover -s tests
```

`tests/fixtures/ptr_sample.txt` is real `pdftotext -layout` output stitched from
three 2026 filings, each of which breaks a naive line-oriented parser differently:
a wrapped asset name, an `S (partial)` transaction type, and a record split across
a page break.

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
- Filers have up to 45 days to report, so recent trades may not appear yet.

Not investment advice.
