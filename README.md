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
| `congress_monitor.py` | Alerts on **every** new PTR filing |
| `build_leaderboard.py` | Estimates per-member returns → `leaderboard.json` |
| `build_watchlist.py` | Snapshots the top-N ranked members → `watchlist.json` |
| `watchlist_monitor.py` | Alerts only when a watchlist member files a **purchase** |
| `daily_summary.py` | One consolidated daily digest of the top-N's trades, buys and sells |
| `build_dashboard.py` | Renders `leaderboard.json` → self-contained `dashboard.html` |

Typical order:

```bash
python3 congress_monitor.py --dry-run     # first run seeds a baseline, no alerts
python3 build_leaderboard.py              # ~1-2 min: fetches every PTR + prices
python3 build_watchlist.py
python3 build_dashboard.py
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
means anything on a phone screen: owner code `SP` becomes `spouse`, asset class `[GS]`
becomes `[govt bond]`, and `$15,001 - $50,000` becomes `$15k-$50k` — the disclosure is
only ever a bracket, so the `,001` is an artefact of how the brackets abut, not a figure.
Ticker leads the asset because that is what a reader scans for; the filing buries it at
the end of a long legal name. Buys are grouped before sells, since the PDF's row order is
however the filer's broker listed them. `⏰` marks a late disclosure and carries the days
taken; the deadline it missed is stated once at the foot rather than in every line.

A non-equity asset is tagged (`[govt bond]`, `[fund]`, `[crypto]`) so a Treasury note in a
buy alert doesn't read as a stock pick — those are counted as trades but excluded from
the leaderboard's returns, per *Reading the numbers* below.

## The daily digest

`daily_summary.py` posts one Discord message a day covering the top-N leaderboard
members: a roll-up of tickers more than one member touched, then a per-member buy/sell
breakdown with PDF links. Where the two monitors alert per filing as it lands, this is
the once-a-day read across the wider field.

```bash
python3 daily_summary.py --top 50                  # default
python3 daily_summary.py --top 50 --rank-by volume # rank by dollars traded instead
python3 daily_summary.py --post-empty              # post even on a quiet day
```

**`--top 50` will usually cover fewer than 50 members.** Ranking by return needs a
computable return, and most members are sell-only in a given year — as of the last
build, 36 of 74 members have one at all and 25 clear the leaderboard's
`--min-invested`/`--min-trades` thresholds. The digest states the shortfall in its own
header rather than quietly reporting on a smaller field. For a true 50, either use
`--rank-by volume` (ranks by dollars traded, which every filer has) or lower the
leaderboard thresholds.

Scope is "filings not yet summarised", not "filings dated today": the Clerk's archive
publishes on its own schedule and a filing routinely appears days after its filing
date, so a fixed 24-hour window would drop it. A quiet day posts nothing unless
`--post-empty` is set.

Both monitors take `--verbose`, `--dry-run` (print, don't post or record state) and
`--max-alerts N` (default 25; the oldest N are handled and the excess is left for the
next run rather than dropped). `watchlist_monitor.py` also takes `--baseline-window N`
(default 7) — see below.

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
- A PTR whose **PDF won't fetch or parse** used to yield zero purchases, which is
  indistinguishable from a filing with no purchases in it. It now sends an alert saying
  so, with the link, rather than nothing.
- `--max-alerts` truncation now keeps the **oldest** filings; the archive's row order
  isn't chronological, so it previously kept an arbitrary slice.

## Schedule

```cron
30 12 * * * ( python3 build_leaderboard.py && python3 build_watchlist.py ) >> rebuild.log 2>&1
 0 13 * * * python3 congress_monitor.py    >> monitor.log 2>&1
30 13 * * * python3 watchlist_monitor.py   >> watchlist_monitor.log 2>&1
 0 14 * * * python3 daily_summary.py       >> summary.log 2>&1
```

The rebuild runs first so the watchlist and the digest's top-N reflect the current
leaderboard rather than whoever was ranked the day the files were last written by hand.
Because that rotates the membership daily, both the watchlist monitor and the digest
record *last run's* membership and re-baseline anyone who leaves and comes back — a
growing all-time set would replay a returning member's entire absence in one burst.

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
