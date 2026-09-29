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
| `house_disclosures.py` | Shared library: fetch the index, pull a PTR PDF (cached as text), parse its transaction table, key a filer's identity |
| `monitor_common.py` | Shared monitor plumbing: webhook config, seen-DocID state, Discord delivery |
| `congress_monitor.py` | Alerts on **every** new PTR filing containing a stock trade |
| `build_leaderboard.py` | Estimates per-member returns and alpha vs SPY over the last 3 years → `leaderboard.json` |
| `build_watchlist.py` | Snapshots the top-N ranked members → `watchlist.json` |
| `watchlist_monitor.py` | Alerts only when a watchlist member files a **purchase**, with a theme note when the index is built |
| `daily_summary.py` | One consolidated daily digest of the top-N's trades, buys and sells, incl. themed roll-ups |
| `build_dashboard.py` | Renders `leaderboard.json` → self-contained `dashboard.html` |
| `build_search_index.py` | Embeds each ticker's business description → Milvus Lite `trades_index.db` (`tickers` + `positions`) |
| `search_trades.py` | Semantic + filtered CLI search over the trade index |
| `search_common.py` | Shared index plumbing: embeddings, clustering, and the read-only `ThemeIndex` the monitors use |

Typical order:

```bash
python3 congress_monitor.py --dry-run     # preview only; never writes monitor state
python3 congress_monitor.py               # first real run seeds a baseline, no alerts
python3 build_leaderboard.py              # ~70s cold, ~40s warm: 3 years of PTRs + prices
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

## The three-year window

The leaderboard scores **every buy and sell disclosed with a transaction date in the last
three years** (`--window-years`, default `PERFORMANCE_WINDOW_YEARS`), and `build_watchlist.py`
and `daily_summary.py` inherit that ranking — so "high performer" means a three-year
record, not whoever looked good in the archive that happened to be open.

One filing-year archive is not a period. It holds whatever members filed that year,
including trades from years earlier: the 2026 archive alone reaches back to October 2023
through late filers, while dropping every 2024 trade nobody re-disclosed. Ranking it
ranks recent filers.

So the window is measured on the **transaction date**, and the archives are read by
**filing year**: a filing is indexed in the year it was filed, never earlier than the
year the trade happened, so covering three years of trades means reading four archives
(2023–2026 today) and discarding the rows that fall outside. `leaderboard.json` states
the window it used (`window_start`, `window_end`, `index_years`) and counts what it threw
away (`totals.out_of_window_lines`, `totals.non_trade_lines`).

**Only `Purchase` and `Sale` rows are scored.** An `Exchange` is a share-class swap or a
merger conversion — no direction, no price to attribute to a pick — and the digest now
drops those too, so a filing containing nothing else no longer shows up as a member line
with neither a BOUGHT nor a SOLD row under it.

Reading four archives also exposed something one archive hid: **the index does not spell
a filer's name the same way twice.** The same representative files as
`Greene / Marjorie Taylor` and `Greene / Marjorie Taylor Mrs`, as `Franklin / C. Scott`
and `Franklin / Scott Scott`, as `Dunn, MD, FACS / Neal Patrick` and
`Dunn / Neal Patrick MD, Facs`. Keyed on the raw fields, Greene scored **twice** — 135
trades at +37% alpha and 268 at +27%, two of the top five slots for one person on half a
record each. Identity is therefore the normalized **(surname, district)** pair
(`member_key`, carried in `leaderboard.json` and `watchlist.json`, matched by both
monitors): titles and post-nominals are stripped, the first name is left out entirely
because `C. Scott` and `Scott Scott` cannot be reconciled, and the district is what keeps
two same-surname members (Tim Moore NC14, Blake Moore UT01) apart. Merged, Greene is one
member with 403 priced trades.

Four archives is ~1,800 PTRs per rebuild rather than ~390 (8 MB of text), so extracted
PDF text is cached under `cache/ptr_text/<year>/<DocID>.txt` (gitignored). A filing never
changes, so a warm rebuild re-downloads nothing but prices. Only a successful, non-empty
extraction is cached: caching a 404 — the Clerk publishes index rows slightly ahead of
some PDFs — or an unreadable PDF would turn a transient failure into a permanent one.
`--no-cache` forces the re-fetch.

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

`daily_summary.py` posts one digest a day covering the top-N leaderboard members:
a roll-up of tickers more than one member touched, then a per-member buy/sell breakdown
with PDF links. One digest is capped at three Discord messages; whole filings that do not
fit remain unseen for the next run. Where the two monitors alert per filing as it lands,
this is the once-a-day read across the wider field.

```bash
python3 daily_summary.py --top 50                  # default: ranked by alpha
python3 daily_summary.py --top 50 --rank-by return # raw return, ignoring the index
python3 daily_summary.py --top 50 --rank-by volume # rank by dollars traded instead
python3 daily_summary.py --post-empty              # post even on a quiet day
```

**`--top 50` will usually cover fewer than 50 members.** Ranking by alpha or return needs
a computable return, and many members are sell-only across the whole window — as of the
last build, 125 members disclosed a tickered buy or sell in the three years, 79 have a
computable return and 59 clear the leaderboard's `--min-invested`/`--min-trades`
thresholds. The digest states the shortfall in its own header rather than quietly
reporting on a smaller field. For a true 50, either use `--rank-by volume` (ranks by
dollars traded, which every filer has) or lower the leaderboard thresholds.

Scope is "filings not yet summarised", not "filings dated today": the Clerk's archive
publishes on its own schedule and a filing routinely appears days after its filing
date, so a fixed 24-hour window would drop it. A quiet day posts nothing unless
`--post-empty` is set.

Both monitors take `--verbose`, `--dry-run` (print, don't post or record state),
`--all-assets` (see *Stocks only* above), `--max-alerts N` (default 25; the oldest N
are handled and the excess is left for the next run rather than dropped) and
`--pending-grace H` (default 24 — see *Polling and the publish gap* below).
`watchlist_monitor.py` also takes `--baseline-window N` (default 7) — see below.

The first non-dry run of a monitor records every existing filing as "seen" and sends
nothing, so you don't get alerted on the whole back catalogue. State lives in
`state/` and is written atomically; JSON structure as well as syntax is validated.
A corrupt state file is quarantined to `*.corrupt` and the next run re-seeds a baseline
instead of re-alerting on everything.

Both monitors read the previous year's index as well as the current one for the first
60 days of a new year. Each year is a separate archive, so a PTR filed on Dec 30 stays
in the old one and a monitor reading only `datetime.now().year` goes blind to it the
moment the calendar rolls over.

## Why an alert might not fire

Alert volume is dominated by the watchlist filter — ten members out of the ~125 who file,
buys only — so a few a week is the expected rate, not a fault. The things that were
silently costing alerts, and what they do now:

- A **newly promoted** member's existing filings are baselined away so a watchlist
  rotation doesn't replay their back catalogue. But the trade that earned them the slot
  is often days old, so filings inside `--baseline-window` (default 7 days) still alert.
- A filing containing **no stock trades** is silent by design — see *Stocks only* above.
  A third of filings are in this category, so this is now the most common reason.
  `--all-assets` turns it off.
- A PTR whose **PDF won't fetch or parse** used to yield zero purchases, which is
  indistinguishable from a filing with no purchases in it. It now announces the filing
  and alerts the contents when the PDF publishes — see below.
- `--max-alerts` truncation now keeps the **oldest** filings; the archive's row order
  isn't chronological, so it previously kept an arbitrary slice.

## Polling and the publish gap

The monitors run **hourly**, the digest and the rebuild stay daily. Disclosure itself is
statutory, not technical: measured across the 3,066 transactions in the 2026 archive, the
median trade reaches the public 25 days after it happens (p10 8, p75 35, p90 46; 89.8%
inside the 45-day deadline, 8.2% within a week). Nothing makes that faster — polling
frequency only decides how much of the *remaining* lag is ours. Daily polling added up to
24 hours to every alert; hourly cuts that to one, and the index ZIP plus the extracted-text
cache make a quiet run cheap.

Two things had to change for hourly polling to be safe:

- **A run can outlive its slot.** A slow Clerk, a backlog of PDFs or a Discord 429 backoff
  can push a run past the hour, and two overlapping runs read the same seen-set and post
  the same alert twice. Each monitor now takes an exclusive `flock` on
  `state/<monitor>.lock` and a second run exits 0 — a skipped poll is not an error, and
  cron mailing about one every hour would be.
- **The index row publishes before the PDF.** The Clerk lists a filing in the archive
  before the document is reachable; a daily poll almost never saw that window, an hourly
  one hits it routinely. Alerting "transactions could not be read" and marking the filing
  handled would spend its only notification on the gap. So the first sighting sends an
  **index-row-first alert** — *"new trade disclosure filed · contents not published yet"* —
  and the filing stays unseen, retried every run, until the PDF parses and the
  transactions go out as a second alert. A PDF still unreadable after `--pending-grace`
  hours (default 24) is genuinely broken and gets the "could not be read" alert, once.
  `--pending-grace 0` restores the old single-alert behaviour.

The waiting set lives in the state file beside the seen-set (`{"seen": [...],
"pending": {"<DocID>": "<first sighted>"}}`), which also reads the old bare-list and
pending-less formats, so upgrading does not replay a back catalogue. `daily_summary.py`
holds an unpublished filing over to the next digest for the same reason.

## Schedule

```cron
 0 * * * * python3 congress_monitor.py    >> monitor.log 2>&1
20 * * * * python3 watchlist_monitor.py   >> watchlist_monitor.log 2>&1
30 12 * * * ( python3 build_leaderboard.py && python3 build_watchlist.py && python3 build_search_index.py ) >> rebuild.log 2>&1
 0 14 * * * python3 daily_summary.py       >> summary.log 2>&1
```

The two per-filing monitors poll hourly and are offset 20 minutes apart so they don't
fetch the index simultaneously; each holds its own lock, so an overrun is skipped rather
than doubled. The digest stays once a day, and the rebuild runs before it so the
watchlist and the digest's top-N reflect the current leaderboard rather than whoever was
ranked the day the files were last written by hand. Because that rotates the membership
daily, both the watchlist monitor and the digest record *last run's* membership and
re-baseline anyone who leaves and comes back — a growing all-time set would replay a
returning member's entire absence in one burst.

A daily rebuild over a three-year window only stays cheap because the extracted PDF text
is cached (see *The three-year window*): the first run pulls ~1,800 PDFs, and after that
each run fetches only the filings that are new plus the price series.

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
when it was really unreadable) — plus what hourly polling added: the publish-gap state
machine (announce once, wait silently, give up after the grace period, and resolve a
corrupt or naive timestamp rather than retrying forever), strict state shape validation
with both legacy formats, dry-run immutability, digest overflow deferral, and the run lock
releasing on both a clean exit and a crash.

`tests/test_leaderboard.py` covers the scoring and accounting engine: the FIFO matcher,
price lookup boundaries, benchmark comparison, all-asset traded volume, failed-PTR
reporting, and the three-year window itself — that a prior-year trade disclosed late is
still scored, and that a trade older than the window (or ancient through a misparse, or
future-dated) is dropped rather than priced. The parser had tests from the start and the
scorer had none, which is how a trade dated before the fetched price window came to be
priced at the first close of the filing year — silently wrong rather than merely missing,
for 23% of matched lots. Both halves of that fix have a regression test here.

`tests/test_house_disclosures.py` also pins the two things the multi-year window added:
filer identity — the real Greene/Franklin/Dunn spelling variants collapse to one member
while two same-surname members in different districts stay apart — and the extracted-text
cache, where a cached filing is not re-fetched and a 404 or an unreadable PDF is never
cached, so a transient failure cannot become permanent.

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
- Scope is the trailing **three years of transaction dates**, read across every archive
  that can hold one — see *The three-year window* above. Rows outside it are counted in
  `totals.out_of_window_lines` and scored nowhere.
- The price series opens at the **window's start**, or at the oldest trade inside it when
  every trade is newer, never at January 1. The download end is tomorrow because Yahoo's
  `end` is exclusive, so the latest completed close is included. A price the series does
  not cover is reported as `unpriced_trades`, not answered with the first held close.
- Sales exceeding matched buys were opened before the window, have unknown cost
  basis, and are excluded. Many members are sell-only across the whole window and so
  have no computable return at all.
- Unsold shares are marked to the latest close — hypothetical, not cashed out.
  All prices come from the same split/dividend-adjusted series, so history and
  "today" are comparable.
- Non-equity assets (treasuries, municipal bonds, funds, crypto) and unresolved tickers
  are counted in `total_trades` and `traded_total_estimate` but excluded from gains.
  Ticker resolution by company name is gated on the filing's asset-type code, because a
  name lookup will cheerfully match "US Treasury Note" to some unrelated equity.
- Every PTR filer appears in `leaderboard.json`, including members with no parsed or
  priceable trades. `totals.ptr_failures` exposes PDFs that could not be fetched or
  parsed rather than making a partial build look complete.
- Members are **ranked** only above `--min-invested` / `--min-trades` thresholds;
  otherwise a single lucky $15k trade tops the board. Both count across the whole
  window, so three years of trading clears them more easily than one year did.
  Unranked members still appear in `leaderboard.json` and greyed in the dashboard table.
- `build_watchlist.py` ranks on alpha by default and takes `--rank-by return` for the raw
  figure. `daily_summary.py --rank-by volume` instead ranks every filer by all in-window
  buy/sell volume, including non-equities and unresolved tickers.
- Filers have up to 45 days to report, so recent trades may not appear yet.

Not investment advice.
