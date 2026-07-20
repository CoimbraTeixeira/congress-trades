#!/usr/bin/env python3
"""Render leaderboard.json into a self-contained HTML dashboard artifact."""

import json
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
DATA_PATH = REPO_DIR / "leaderboard.json"
OUTPUT_PATH = REPO_DIR / "dashboard.html"

TEMPLATE = """<!doctype html>
<title>Congress Stock Trading Leaderboard</title>
<style>
  :root {{
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
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) {{
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
    }}
  }}
  :root[data-theme="dark"] {{
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
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 24px 16px 64px;
  }}
  .wrap {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 4px; }}
  .subtitle {{ color: var(--text-secondary); font-size: 0.9rem; margin: 0 0 20px; }}
  .card {{
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 20px;
  }}
  .stat-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 16px; }}
  .stat-tile {{ }}
  .stat-value {{ font-size: 1.6rem; font-weight: 600; font-variant-numeric: tabular-nums; }}
  .stat-label {{ color: var(--text-muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; }}
  .callout {{
    font-size: 0.85rem;
    color: var(--text-secondary);
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-left: 3px solid var(--text-muted);
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 20px;
    line-height: 1.5;
  }}
  .callout ul {{ margin: 8px 0 0; padding-left: 18px; }}
  .bar-row {{ display: grid; grid-template-columns: 200px 1fr 90px; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--grid); }}
  .bar-row:last-child {{ border-bottom: none; }}
  .bar-name {{ font-size: 0.85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .bar-name .dist {{ color: var(--text-muted); font-size: 0.75rem; }}
  .bar-track {{ position: relative; height: 20px; background: var(--grid); border-radius: 3px; overflow: visible; }}
  .bar-fill {{ position: absolute; top: 0; height: 100%; border-radius: 3px; }}
  .bar-fill.good {{ background: var(--good); left: 50%; }}
  .bar-fill.bad {{ background: var(--bad); right: 50%; }}
  .bar-mid {{ position: absolute; left: 50%; top: -2px; bottom: -2px; width: 1px; background: var(--baseline); }}
  .bar-pct {{ font-variant-numeric: tabular-nums; font-size: 0.85rem; text-align: right; }}
  .bar-pct.good {{ color: var(--good); }}
  .bar-pct.bad {{ color: var(--bad); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }}
  th {{ color: var(--text-muted); font-weight: 500; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.03em; cursor: pointer; user-select: none; }}
  th:hover {{ color: var(--text-primary); }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .pill {{ display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 0.78rem; font-variant-numeric: tabular-nums; }}
  .pill.good {{ background: var(--good-bg); color: var(--good); }}
  .pill.bad {{ background: var(--bad-bg); color: var(--bad); }}
  details {{ margin-top: 2px; }}
  details summary {{ cursor: pointer; color: var(--text-muted); font-size: 0.78rem; }}
  .trade-list {{ margin: 6px 0 0; padding-left: 16px; font-size: 0.78rem; color: var(--text-secondary); }}
  .table-wrap {{ overflow-x: auto; }}
  .source-note {{ color: var(--text-muted); font-size: 0.75rem; margin-top: 24px; text-align: center; }}
</style>
<div class="wrap">
  <h1>Congress Stock Trading Leaderboard</h1>
  <p class="subtitle">House members, {year} · estimated returns from disclosed Periodic Transaction Reports · generated {generated_at}</p>

  <div class="callout">
    <strong>How to read this.</strong> The STOCK Act requires disclosure of only a dollar <em>range</em>
    (e.g. "$15,001-$50,000"), never an exact amount or share count. Figures here are estimates:
    <ul>
      <li>Each trade's dollar value is the <strong>midpoint</strong> of its disclosed range.</li>
      <li><strong>Unrealized</strong> gains mark still-held positions to today's price — hypothetical, not cashed out.</li>
      <li><strong>Realized</strong> gains only count buy/sell pairs matched within {year}; sales of positions opened before {year} have unknown cost basis and are excluded.</li>
      <li>{priced_trades} of {total_trades} disclosed trade lines resolved to a priced ticker; the rest (bonds, treasury notes, private funds, or trades with no parseable ticker) count toward activity but not toward gain estimates.</li>
      <li>Members can file up to 45 days after a trade — recent trades may not be reflected yet. House only; Senate disclosures aren't included.</li>
    </ul>
  </div>

  <div class="card">
    <div class="stat-row">
      <div class="stat-tile"><div class="stat-value">{n_priced}</div><div class="stat-label">Members with priced trades</div></div>
      <div class="stat-tile"><div class="stat-value">{top_name_short}</div><div class="stat-label">Top estimated return</div></div>
      <div class="stat-tile"><div class="stat-value">{top_pct:+.1f}%</div><div class="stat-label">{top_name}</div></div>
      <div class="stat-tile"><div class="stat-value">{avg_pct:+.1f}%</div><div class="stat-label">Average return (priced members)</div></div>
    </div>
  </div>

  <div class="card">
    <h2 style="margin-top:0; font-size:1.05rem;">Top 10 by estimated return</h2>
    <div id="chart"></div>
  </div>

  <div class="card">
    <h2 style="margin-top:0; font-size:1.05rem;">All members with priced trades ({n_priced})</h2>
    <div class="table-wrap">
      <table id="full-table">
        <thead>
          <tr>
            <th data-key="member">Member</th>
            <th data-key="state_dist">District</th>
            <th class="num" data-key="total_trades">Trades</th>
            <th class="num" data-key="invested_total_estimate">Est. Invested</th>
            <th class="num" data-key="total_gain_estimate">Est. Gain</th>
            <th class="num" data-key="return_pct_estimate">Return %</th>
          </tr>
        </thead>
        <tbody id="full-table-body"></tbody>
      </table>
    </div>
  </div>

  <p class="source-note">Source: disclosures-clerk.house.gov public PTR filings · prices via Yahoo Finance · not investment advice</p>
</div>

<script>
const DATA = {data_json};

function fmtMoney(v) {{
  return '$' + Math.round(v).toLocaleString();
}}
function fmtPct(v) {{
  return (v > 0 ? '+' : '') + v.toFixed(1) + '%';
}}

function renderChart() {{
  const top10 = DATA.members
    .filter(m => m.return_pct_estimate !== null)
    .sort((a, b) => b.return_pct_estimate - a.return_pct_estimate)
    .slice(0, 10);
  const maxAbs = Math.max(...top10.map(m => Math.abs(m.return_pct_estimate)), 1);
  const chart = document.getElementById('chart');
  chart.innerHTML = top10.map(m => {{
    const pct = m.return_pct_estimate;
    const isGood = pct >= 0;
    const widthPct = (Math.abs(pct) / maxAbs) * 50;
    const trades = m.trade_detail.map(t => {{
      const dateInfo = t.sell_date ? `${{t.buy_date}} → ${{t.sell_date}}` : `${{t.buy_date}} (open)`;
      return `<li>${{t.ticker}} — ${{t.type}} — ${{dateInfo}} — ${{fmtMoney(t.amount)}} → ${{t.gain >= 0 ? '+' : ''}}${{fmtMoney(t.gain)}}</li>`;
    }}).join('');
    return `
      <div class="bar-row">
        <div class="bar-name">${{m.member}}<br><span class="dist">${{m.state_dist}}</span></div>
        <div>
          <div class="bar-track">
            <div class="bar-mid"></div>
            <div class="bar-fill ${{isGood ? 'good' : 'bad'}}" style="width:${{widthPct}}%;"></div>
          </div>
          <details><summary>${{m.trade_detail.length}} priced trade(s)</summary><ul class="trade-list">${{trades}}</ul></details>
        </div>
        <div class="bar-pct ${{isGood ? 'good' : 'bad'}}">${{fmtPct(pct)}}</div>
      </div>`;
  }}).join('');
}}

let sortKey = 'return_pct_estimate';
let sortDir = -1;
function renderTable() {{
  const rows = DATA.members
    .filter(m => m.return_pct_estimate !== null)
    .sort((a, b) => (a[sortKey] > b[sortKey] ? 1 : -1) * sortDir);
  document.getElementById('full-table-body').innerHTML = rows.map(m => `
    <tr>
      <td>${{m.member}}</td>
      <td>${{m.state_dist}}</td>
      <td class="num">${{m.total_trades}}</td>
      <td class="num">${{fmtMoney(m.invested_total_estimate)}}</td>
      <td class="num">${{fmtMoney(m.total_gain_estimate)}}</td>
      <td class="num"><span class="pill ${{m.return_pct_estimate >= 0 ? 'good' : 'bad'}}">${{fmtPct(m.return_pct_estimate)}}</span></td>
    </tr>`).join('');
}}

document.querySelectorAll('#full-table th').forEach(th => {{
  th.addEventListener('click', () => {{
    const key = th.dataset.key;
    sortDir = (sortKey === key) ? -sortDir : -1;
    sortKey = key;
    renderTable();
  }});
}});

renderChart();
renderTable();
</script>
"""


def main() -> None:
    data = json.loads(DATA_PATH.read_text())
    priced = [m for m in data["members"] if m["return_pct_estimate"] is not None]
    priced_sorted = sorted(priced, key=lambda m: m["return_pct_estimate"], reverse=True)
    top = priced_sorted[0]
    avg_pct = sum(m["return_pct_estimate"] for m in priced) / len(priced) if priced else 0.0
    total_trades = sum(m["total_trades"] for m in data["members"])
    total_priced_trades = sum(m["priced_trades"] for m in data["members"])

    html = TEMPLATE.format(
        year=data["year"],
        generated_at=data["generated_at"][:10],
        n_priced=len(priced),
        top_name_short=top["member"].replace("Hon. ", ""),
        top_name=f"{top['member']} ({top['state_dist']})",
        top_pct=top["return_pct_estimate"],
        avg_pct=avg_pct,
        priced_trades=total_priced_trades,
        total_trades=total_trades,
        data_json=json.dumps(data),
    )
    OUTPUT_PATH.write_text(html)
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
