#!/usr/bin/env python3
"""Render leaderboard.json into a self-contained HTML dashboard artifact.

The template is injected via a single sentinel rather than str.format so that CSS
and JS braces need no doubling -- the previous approach made every template edit a
chance to break the page.
"""

import json
import sys

from house_disclosures import DATA_DIR
from monitor_common import LEADERBOARD_PATH

DATA_PATH = LEADERBOARD_PATH
OUTPUT_PATH = DATA_DIR / "dashboard.html"
DATA_SENTINEL = "/*__LEADERBOARD_DATA__*/null"

TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Congress Stock Trading Leaderboard</title>
<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --page: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --grid: #e1e0d9;
    --baseline: #c3c2b7;
    --border: rgba(11,11,11,0.10);
    --good: #256abf;
    --bad: #e34948;
    --good-bg: #cde2fb;
    --bad-bg: #fbdada;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1: #1a1a19;
      --page: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --grid: #2c2c2a;
      --baseline: #383835;
      --border: rgba(255,255,255,0.10);
      --good: #3987e5;
      --bad: #e66767;
      --good-bg: #143253;
      --bad-bg: #4a1f1f;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --page: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --good: #3987e5;
    --bad: #e66767;
    --good-bg: #143253;
    --bad-bg: #4a1f1f;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px 16px 64px;
  }
  .wrap { max-width: 900px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin: 0 0 4px; }
  h2 { margin-top: 0; font-size: 1.05rem; }
  .subtitle { color: var(--text-secondary); font-size: 0.9rem; margin: 0 0 20px; }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 20px;
  }
  .stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; }
  .stat-value { font-size: 1.6rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .stat-label { color: var(--text-muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; }
  .callout {
    font-size: 0.85rem;
    color: var(--text-secondary);
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-left: 3px solid var(--text-muted);
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 20px;
    line-height: 1.5;
  }
  .callout ul { margin: 8px 0 0; padding-left: 18px; }
  .bar-row { display: grid; grid-template-columns: 200px 1fr 90px; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--grid); }
  .bar-row:last-child { border-bottom: none; }
  .bar-name { font-size: 0.85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar-name .dist { color: var(--text-muted); font-size: 0.75rem; }
  .bar-track { position: relative; height: 20px; background: var(--grid); border-radius: 3px; }
  .bar-fill { position: absolute; top: 0; height: 100%; }
  /* Round only the data end; the baseline end stays square against the midline. */
  .bar-fill.good { background: var(--good); left: 50%; border-radius: 0 4px 4px 0; }
  .bar-fill.bad { background: var(--bad); right: 50%; border-radius: 4px 0 0 4px; }
  .bar-mid { position: absolute; left: 50%; top: -2px; bottom: -2px; width: 1px; background: var(--baseline); }
  .bar-pct { font-variant-numeric: tabular-nums; font-size: 0.85rem; text-align: right; }
  .bar-pct.good { color: var(--good); }
  .bar-pct.bad { color: var(--bad); }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  th { color: var(--text-muted); font-weight: 500; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.03em; cursor: pointer; user-select: none; }
  th:hover, th:focus-visible { color: var(--text-primary); }
  th[aria-sort]:not([aria-sort="none"]) { color: var(--text-primary); }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr.unranked td { color: var(--text-muted); }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 0.78rem; font-variant-numeric: tabular-nums; }
  .pill.good { background: var(--good-bg); color: var(--good); }
  .pill.bad { background: var(--bad-bg); color: var(--bad); }
  details { margin-top: 2px; }
  details summary { cursor: pointer; color: var(--text-muted); font-size: 0.78rem; }
  .trade-list { margin: 6px 0 0; padding-left: 16px; font-size: 0.78rem; color: var(--text-secondary); }
  .table-wrap { overflow-x: auto; }
  .source-note { color: var(--text-muted); font-size: 0.75rem; margin-top: 24px; text-align: center; }
  .empty { color: var(--text-muted); font-size: 0.9rem; }
  #tooltip {
    position: fixed; z-index: 10; pointer-events: none; opacity: 0;
    transition: opacity 90ms ease;
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 10px; font-size: 0.78rem; line-height: 1.45;
    box-shadow: 0 4px 16px rgba(0,0,0,0.18); max-width: 260px;
  }
  #tooltip .t-name { font-weight: 600; margin-bottom: 3px; }
  #tooltip dl { display: grid; grid-template-columns: auto auto; gap: 1px 10px; margin: 0; }
  #tooltip dt { color: var(--text-muted); }
  #tooltip dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
  @media (max-width: 560px) {
    .bar-row { grid-template-columns: 120px 1fr 66px; }
  }
</style>
<div class="wrap">
  <h1>Congress Stock Trading Leaderboard</h1>
  <p class="subtitle" id="subtitle"></p>

  <div class="callout" id="callout"></div>

  <div class="card">
    <div class="stat-row" id="stats"></div>
  </div>

  <div class="card">
    <h2 id="chart-heading">Top 10 ranked members by estimated alpha</h2>
    <div id="chart"></div>
  </div>

  <div class="card">
    <h2 id="table-heading">All members with priced trades</h2>
    <div class="table-wrap">
      <table id="full-table">
        <thead>
          <tr>
            <th data-key="member" tabindex="0" aria-sort="none">Member</th>
            <th data-key="state_dist" tabindex="0" aria-sort="none">District</th>
            <th class="num" data-key="priced_trades" tabindex="0" aria-sort="none">Priced Trades</th>
            <th class="num" data-key="invested_total_estimate" tabindex="0" aria-sort="none">Est. Basis</th>
            <th class="num" data-key="realized_gain_estimate" tabindex="0" aria-sort="none">Realized</th>
            <th class="num" data-key="unrealized_gain_estimate" tabindex="0" aria-sort="none">Unrealized</th>
            <th class="num" data-key="return_pct_estimate" tabindex="0" aria-sort="none">Return %</th>
            <th class="num" data-key="benchmark_return_pct_estimate" tabindex="0" aria-sort="none">Index %</th>
            <th class="num" data-key="alpha_pct_estimate" tabindex="0" aria-sort="descending">Alpha %</th>
          </tr>
        </thead>
        <tbody id="full-table-body"></tbody>
      </table>
    </div>
  </div>

  <p class="source-note">Source: disclosures-clerk.house.gov public PTR filings · prices via Yahoo Finance · not investment advice</p>
</div>
<div id="tooltip" role="tooltip" aria-hidden="true"></div>

<script>
const DATA = /*__LEADERBOARD_DATA__*/null;

const MEMBERS = DATA.members || [];
const RANKED = MEMBERS.filter(m => m.ranked);
const PRICED = MEMBERS.filter(m => m.return_pct_estimate !== null);
const TOTALS = DATA.totals || {};
const BENCH = DATA.benchmark_ticker || 'the index';
// Ranking metric. A member the benchmark could not be priced against falls back to the
// raw return rather than dropping off the board.
const score = m => m.alpha_pct_estimate !== null && m.alpha_pct_estimate !== undefined
  ? m.alpha_pct_estimate : m.return_pct_estimate;

const esc = s => String(s).replace(/[&<>"']/g, c => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));
const fmtMoney = v => (v < 0 ? '-$' : '$') + Math.round(Math.abs(v)).toLocaleString();
const fmtSigned = v => (v >= 0 ? '+' : '-') + '$' + Math.round(Math.abs(v)).toLocaleString();
const fmtPct = v => (v >= 0 ? '+' : '') + v.toFixed(1) + '%';

// Members who file on paper submit scans with no text layer, so their trades are in none
// of the figures. Named rather than counted: the heaviest paper filers are among the most
// active traders in the House, and their absence from the table would otherwise read as
// inactivity.
function scannedNote() {
  const scanned = MEMBERS.filter(m => m.scanned_filings > 0)
    .sort((a, b) => b.scanned_filings - a.scanned_filings);
  if (!scanned.length) return '';
  const names = scanned.slice(0, 8)
    .map(m => `${esc(m.member)} (${esc(m.scanned_filings)})`).join(', ');
  const more = scanned.length > 8 ? `, and ${scanned.length - 8} more` : '';
  return `<li><strong>${esc(TOTALS.scanned_ptrs ?? 0)} filings are scanned paper forms</strong>
      with no machine-readable text, so their trades are missing from every figure here.
      Members affected, with scanned filings: ${names}${more}.</li>`;
}

function renderHeader() {
  const span = DATA.window_start && DATA.window_end
    ? `trades ${DATA.window_start} to ${DATA.window_end}`
    : 'disclosed trades';
  document.getElementById('subtitle').textContent =
    `House members, ${span} · estimated returns from disclosed Periodic Transaction ` +
    `Reports · generated ${String(DATA.generated_at).slice(0, 10)}`;

  const minInvested = fmtMoney(DATA.min_invested || 0);
  document.getElementById('callout').innerHTML = `
    <strong>How to read this.</strong> The STOCK Act requires disclosure of only a dollar
    <em>range</em> (e.g. "$15,001-$50,000"), never an exact amount or share count. Figures
    here are estimates:
    <ul>
      <li>Each trade's dollar value is the <strong>midpoint</strong> of its disclosed range,
          converted to estimated shares at that day's close. Buys and sells are FIFO-matched
          in share space.</li>
      <li><strong>Unrealized</strong> gains mark still-held shares to the latest close —
          hypothetical, not cashed out.</li>
      <li><strong>Realized</strong> gains only count buy/sell pairs where both sides were
          disclosed; sales of positions opened before the disclosed history have unknown
          cost basis and are excluded.</li>
      <li><strong>Alpha</strong> is the estimated return less what ${esc(BENCH)} returned over
          the <em>same holding windows</em>, weighted by the same dollars. It is the ranking
          metric here: a raw return says nothing on its own, since +9% in a year the index
          returned +15% is underperformance.</li>
      <li>${esc(TOTALS.tradeable_lines ?? 0)} of ${esc(TOTALS.transaction_lines ?? 0)}
          disclosed trade lines resolved to an equity ticker; the rest (treasuries, municipal
          bonds, funds, crypto) count toward activity but not toward gain estimates.</li>
      <li>Members are <strong>ranked</strong> only with at least ${esc(DATA.min_trades)} priced
          trades and ${esc(minInvested)} estimated cost basis — otherwise one lucky trade
          tops the board. Unranked members are shown greyed in the table.</li>
      <li>Members can file up to 45 days after a trade — recent trades may not appear yet.
          House only; Senate disclosures aren't included.</li>
      ${scannedNote()}
    </ul>`;

  const withAlpha = RANKED.filter(m => m.alpha_pct_estimate !== null &&
                                       m.alpha_pct_estimate !== undefined);
  const avgAlpha = withAlpha.length
    ? withAlpha.reduce((a, m) => a + m.alpha_pct_estimate, 0) / withAlpha.length : 0;
  const beat = withAlpha.filter(m => m.alpha_pct_estimate > 0).length;
  const top = RANKED[0];
  const tiles = [
    [RANKED.length, 'Ranked members'],
    [top ? fmtPct(score(top)) : '—',
     top ? `Top alpha: ${top.member} (${top.state_dist})` : 'Top estimated alpha'],
    [withAlpha.length ? fmtPct(avgAlpha) : '—', `Average alpha vs ${BENCH} (ranked)`],
    [withAlpha.length ? `${beat} / ${withAlpha.length}` : '—', `Beat ${BENCH}`],
  ];
  document.getElementById('stats').innerHTML = tiles.map(([v, l]) =>
    `<div><div class="stat-value">${esc(v)}</div><div class="stat-label">${esc(l)}</div></div>`
  ).join('');

  document.getElementById('table-heading').textContent =
    `All members with priced trades (${PRICED.length})`;
  document.getElementById('chart-heading').textContent =
    `Top 10 ranked members by estimated alpha vs ${BENCH}`;
}

function renderChart() {
  const chart = document.getElementById('chart');
  const top10 = RANKED.slice(0, 10);
  if (!top10.length) {
    chart.innerHTML = '<p class="empty">No members met the ranking threshold.</p>';
    return;
  }
  const maxAbs = Math.max(...top10.map(m => Math.abs(score(m))), 1);
  chart.innerHTML = top10.map((m, i) => {
    const pct = score(m);
    const good = pct >= 0;
    const width = (Math.abs(pct) / maxAbs) * 50;
    const trades = m.trade_detail.map(t => {
      const when = t.sell_date ? `${esc(t.buy_date)} → ${esc(t.sell_date)}`
                               : `${esc(t.buy_date)} (open)`;
      const vs = (t.benchmark_gain === null || t.benchmark_gain === undefined) ? ''
        : ` (${esc(BENCH)} ${esc(fmtSigned(t.benchmark_gain))})`;
      return `<li>${esc(t.ticker)} — ${esc(t.type)} — ${when} — ` +
             `${esc(fmtMoney(t.amount))} → ${esc(fmtSigned(t.gain))}${vs}</li>`;
    }).join('');
    return `
      <div class="bar-row">
        <div class="bar-name">${esc(m.member)}<br><span class="dist">${esc(m.state_dist)}</span></div>
        <div>
          <div class="bar-track" data-idx="${i}" tabindex="0">
            <div class="bar-mid"></div>
            <div class="bar-fill ${good ? 'good' : 'bad'}" style="width:${width}%;"></div>
          </div>
          <details><summary>${m.trade_detail.length} priced trade(s)</summary>
            <ul class="trade-list">${trades}</ul></details>
        </div>
        <div class="bar-pct ${good ? 'good' : 'bad'}">${esc(fmtPct(pct))}</div>
      </div>`;
  }).join('');
  attachTooltips(top10);
}

function attachTooltips(rows) {
  const tip = document.getElementById('tooltip');
  const show = (el, m) => {
    tip.innerHTML =
      `<div class="t-name">${esc(m.member)} (${esc(m.state_dist)})</div><dl>` +
      `<dt>Est. return</dt><dd>${esc(fmtPct(m.return_pct_estimate))}</dd>` +
      (m.benchmark_return_pct_estimate === null || m.benchmark_return_pct_estimate === undefined
        ? '' : `<dt>${esc(BENCH)}, same windows</dt>` +
               `<dd>${esc(fmtPct(m.benchmark_return_pct_estimate))}</dd>` +
               `<dt>Alpha</dt><dd>${esc(fmtPct(m.alpha_pct_estimate))}</dd>`) +
      `<dt>Est. cost basis</dt><dd>${esc(fmtMoney(m.invested_total_estimate))}</dd>` +
      `<dt>Realized</dt><dd>${esc(fmtSigned(m.realized_gain_estimate))}</dd>` +
      `<dt>Unrealized</dt><dd>${esc(fmtSigned(m.unrealized_gain_estimate))}</dd>` +
      `<dt>Priced trades</dt><dd>${esc(m.priced_trades)}</dd>` +
      `</dl>`;
    const r = el.getBoundingClientRect();
    tip.style.opacity = '1';
    tip.setAttribute('aria-hidden', 'false');
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8) + 'px';
    tip.style.top = (r.top - h - 8 < 8 ? r.bottom + 8 : r.top - h - 8) + 'px';
  };
  const hide = () => { tip.style.opacity = '0'; tip.setAttribute('aria-hidden', 'true'); };
  document.querySelectorAll('.bar-track').forEach(el => {
    const m = rows[Number(el.dataset.idx)];
    el.addEventListener('mouseenter', () => show(el, m));
    el.addEventListener('focus', () => show(el, m));
    el.addEventListener('mouseleave', hide);
    el.addEventListener('blur', hide);
  });
}

let sortKey = 'alpha_pct_estimate';
let sortDir = -1;

function compare(a, b, key) {
  const x = a[key], y = b[key];
  if (x === y) return 0;
  if (x === null || x === undefined) return 1;   // nulls always last
  if (y === null || y === undefined) return -1;
  if (typeof x === 'number' && typeof y === 'number') return x - y;
  return String(x).localeCompare(String(y));
}

function renderTable() {
  // Ranked first, then the sort key -- so the threshold stays visible under any sort.
  const rows = PRICED.slice().sort((a, b) =>
    (b.ranked - a.ranked) || compare(a, b, sortKey) * sortDir);
  document.getElementById('full-table-body').innerHTML = rows.map(m => `
    <tr class="${m.ranked ? '' : 'unranked'}">
      <td>${esc(m.member)}${m.ranked ? '' : ' <span class="dist">(unranked)</span>'}${m.scanned_filings > 0 ? ` <span class="dist">(+${esc(m.scanned_filings)} scanned, not counted)</span>` : ''}</td>
      <td>${esc(m.state_dist)}</td>
      <td class="num">${esc(m.priced_trades)}</td>
      <td class="num">${esc(fmtMoney(m.invested_total_estimate))}</td>
      <td class="num">${esc(fmtSigned(m.realized_gain_estimate))}</td>
      <td class="num">${esc(fmtSigned(m.unrealized_gain_estimate))}</td>
      <td class="num"><span class="pill ${m.return_pct_estimate >= 0 ? 'good' : 'bad'}">${esc(fmtPct(m.return_pct_estimate))}</span></td>
      <td class="num">${m.benchmark_return_pct_estimate === null || m.benchmark_return_pct_estimate === undefined ? '—' : esc(fmtPct(m.benchmark_return_pct_estimate))}</td>
      <td class="num">${m.alpha_pct_estimate === null || m.alpha_pct_estimate === undefined ? '—' : `<span class="pill ${m.alpha_pct_estimate >= 0 ? 'good' : 'bad'}">${esc(fmtPct(m.alpha_pct_estimate))}</span>`}</td>
    </tr>`).join('');
}

function sortBy(th) {
  const key = th.dataset.key;
  sortDir = (sortKey === key) ? -sortDir : -1;
  sortKey = key;
  document.querySelectorAll('#full-table th').forEach(o => o.setAttribute('aria-sort', 'none'));
  th.setAttribute('aria-sort', sortDir === -1 ? 'descending' : 'ascending');
  renderTable();
}

document.querySelectorAll('#full-table th').forEach(th => {
  th.addEventListener('click', () => sortBy(th));
  th.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sortBy(th); }
  });
});

renderHeader();
renderChart();
renderTable();
</script>
"""


def main() -> None:
    if not DATA_PATH.exists():
        sys.exit(f"Missing {DATA_PATH} -- run build_leaderboard.py first")
    data = json.loads(DATA_PATH.read_text())
    if not data.get("members"):
        print(f"warning: {DATA_PATH} has no members; rendering an empty dashboard",
              file=sys.stderr)

    # Escape '<' so a member name or asset string can never close the <script> block.
    payload = json.dumps(data).replace("<", "\\u003c")
    html = TEMPLATE.replace(DATA_SENTINEL, payload)
    OUTPUT_PATH.write_text(html)

    ranked = sum(1 for m in data.get("members", []) if m.get("ranked"))
    print(f"Wrote {OUTPUT_PATH} ({ranked} ranked of {len(data.get('members', []))} members)")


if __name__ == "__main__":
    main()
