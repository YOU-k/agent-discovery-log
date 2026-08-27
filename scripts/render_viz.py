#!/usr/bin/env python3
"""Render docs/index.html — a self-contained visualization of state/seen.json.

No dependencies, no external assets: plain HTML/CSS with data baked in.
Suitable for GitHub Pages (serve /docs on main) or opening locally.

Usage:
    python3 scripts/render_viz.py [--refresh]

--refresh re-fetches current stars for all seen repos in-memory (via
discover.refresh_stars) before rendering, without modifying seen.json.
The daily workflow runs this right after discover.py, so the state file
is already fresh and --refresh is unnecessary there.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "seen.json"
OUT = ROOT / "docs" / "index.html"

sys.path.insert(0, str(ROOT / "scripts"))
import discover  # noqa: E402  (daily_rate / is_watched / refresh_stars)

TOP_MOVERS = 15

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { background: #0d1117; color: #c9d1d9; font: 14px/1.5 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif; margin: 0 auto; max-width: 1100px; padding: 24px 16px 64px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 36px 0 12px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
.meta { color: #8b949e; margin: 0 0 8px; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.bar-row { display: grid; grid-template-columns: minmax(180px, 320px) 1fr 90px; gap: 10px; align-items: center; padding: 3px 0; }
.bar-row a { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bar-track { background: #161b22; border-radius: 4px; height: 16px; }
.bar { background: linear-gradient(90deg, #1f6feb, #3fb950); height: 16px; border-radius: 4px; min-width: 2px; }
.bar.hot { background: linear-gradient(90deg, #f0883e, #ff7b72); }
.delta { color: #3fb950; font-variant-numeric: tabular-nums; text-align: right; }
.cols { display: flex; align-items: flex-end; gap: 3px; height: 120px; margin-top: 8px; }
.col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; min-width: 0; }
.col .v { background: #1f6feb; width: 100%; border-radius: 3px 3px 0 0; min-height: 2px; }
.col .n { font-size: 11px; color: #8b949e; margin-top: 4px; }
.col .d { font-size: 10px; color: #484f58; writing-mode: vertical-rl; margin-top: 4px; max-height: 60px; overflow: hidden; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; }
th, td { border-bottom: 1px solid #21262d; padding: 6px 8px; text-align: left; vertical-align: top; }
th { color: #8b949e; font-weight: 600; white-space: nowrap; }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
td.pos { color: #3fb950; }
.score { display: inline-block; background: #1f6feb33; color: #58a6ff; border-radius: 10px; padding: 0 8px; font-size: 12px; }
.one { color: #8b949e; font-size: 13px; }
"""


def esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def repo_rows(seen: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten seen.json into display rows with then/now/delta stars + velocity."""
    rows = []
    for fn, e in seen.items():
        hist = e.get("stars_history") or []
        then = hist[0][1] if hist else e.get("stars_at_first_seen")
        now = hist[-1][1] if hist else then
        if then is None:
            continue
        rows.append({
            "full_name": fn,
            "first_seen": e.get("first_seen", ""),
            "then": then,
            "now": now,
            "delta": (now or then) - then,
            "rate": discover.daily_rate(e),
            "watched": discover.is_watched(e),
            "query": e.get("matched_query", ""),
            "score": e.get("score"),
            "category": e.get("category") or "",
            "one_liner": e.get("one_liner") or "",
            "use_for": e.get("use_for") or "",
        })
    rows.sort(key=lambda r: r["delta"], reverse=True)
    return rows


def bar_chart(rows: list[dict[str, Any]], value_key: str, hot: bool = False) -> str:
    """Render a horizontal bar chart for the given numeric row field."""
    if not rows:
        return '<p class="meta">No data yet — history accumulates daily.</p>'
    max_v = max(r[value_key] for r in rows) or 1
    cls = "bar hot" if hot else "bar"
    return "\n".join(
        f'<div class="bar-row"><a href="https://github.com/{esc(r["full_name"])}">{esc(r["full_name"])}</a>'
        f'<div class="bar-track"><div class="{cls}" style="width:{max(1, int(r[value_key] * 100 / max_v))}%"></div></div>'
        f'<span class="delta">{r["_bar_label"]}</span></div>'
        for r in rows
    )


def render(seen: dict[str, dict[str, Any]]) -> str:
    rows = repo_rows(seen)
    generated = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    first_dates = sorted(r["first_seen"] for r in rows if r["first_seen"])

    # Top movers (cumulative)
    top = [r for r in rows if r["delta"] > 0][:TOP_MOVERS]
    for r in top:
        r["_bar_label"] = f"+{r['delta']:,}"
    movers_html = bar_chart(top, "delta")

    # Velocity (avg stars/day over tracked span)
    rated = [r for r in rows if r["rate"] and r["rate"] > 0]
    rated.sort(key=lambda r: r["rate"], reverse=True)
    top_rated = rated[:TOP_MOVERS]
    for r in top_rated:
        r["_bar_label"] = f"+{r['rate']:,.0f}/天"
    rate_html = bar_chart(top_rated, "rate", hot=True)

    # Discoveries per day
    by_day: dict[str, int] = {}
    for r in rows:
        by_day[r["first_seen"]] = by_day.get(r["first_seen"], 0) + 1
    max_n = max(by_day.values(), default=1)
    timeline_html = "\n".join(
        f'<div class="col"><div class="v" style="height:{max(2, n * 100 // max_n)}%"></div>'
        f'<div class="n">{n}</div><div class="d">{esc(d[5:])}</div></div>'
        for d, n in sorted(by_day.items())
    )

    # Full table
    table_row_list = []
    for r in rows:
        delta_cls = "num pos" if r["delta"] > 0 else "num"
        delta_txt = f"+{r['delta']:,}" if r["delta"] > 0 else f"{r['delta']:,}"
        rate_txt = f"+{r['rate']:,.0f}/天" if r["rate"] else "—"
        score_txt = f'<span class="score">{r["score"]}/10</span>' if r["score"] else ""
        analysis = r["one_liner"] + (f"；{r['use_for']}" if r["use_for"] else "")
        flame = "🔥 " if r["watched"] else ""
        table_row_list.append(
            "<tr>"
            f'<td>{flame}<a href="https://github.com/{esc(r["full_name"])}">{esc(r["full_name"])}</a></td>'
            f'<td class="num">{esc(r["first_seen"])}</td>'
            f'<td class="num">{r["then"]:,} → {r["now"]:,}</td>'
            f'<td class="{delta_cls}">{delta_txt}</td>'
            f'<td class="num">{rate_txt}</td>'
            f"<td>{esc(r['category'])}</td>"
            f"<td>{score_txt}</td>"
            f'<td class="one">{esc(analysis)}</td>'
            "</tr>"
        )
    table_rows = "\n".join(table_row_list)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Discovery</title>
<style>{CSS}</style>
</head>
<body>
<h1>Agent Discovery</h1>
<p class="meta">Generated {esc(generated)} · tracking {len(rows)} repos · since {esc(first_dates[0] if first_dates else "—")} · 🔥 = 自动关注</p>

<h2>日均涨速 Top {TOP_MOVERS}（自首收起）</h2>
{rate_html}

<h2>累计涨幅 Top {TOP_MOVERS}</h2>
{movers_html}

<h2>Discoveries per day</h2>
<div class="cols">
{timeline_html}
</div>

<h2>All tracked repos</h2>
<table>
<thead><tr><th>Repo</th><th>First seen</th><th>Stars then → now</th><th>Δ</th><th>日均Δ</th><th>类型</th><th>Score</th><th>分析</th></tr></thead>
<tbody>
{table_rows}
</tbody>
</table>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="Re-fetch current stars in-memory first.")
    args = parser.parse_args()

    seen: dict[str, dict[str, Any]] = json.loads(STATE.read_text(encoding="utf-8"))

    if args.refresh:
        discover.refresh_stars(seen)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(seen), encoding="utf-8")
    print(f"[INFO] wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
