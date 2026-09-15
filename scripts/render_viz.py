#!/usr/bin/env python3
"""Render docs/index.html — a self-contained visualization of state/seen.json.

No dependencies, no external assets: plain HTML/CSS with data baked in.
Suitable for GitHub Pages (serve /docs on main) or opening locally.

The tracking table is compact by default — each row shows only name /
sparkline / stars / Δ / 日均 / 评分; click (or Enter/Space) a row to expand
the full analysis inline. 「全部展开」按钮在表格区右上角。

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
/* 调色板取自 dataviz 参考实例，两模式都跑过 validate_palette：
   light #2a78d6/#eb6834 on #fcfcfb、dark #3987e5/#d95926 on #1a1a19，
   五项检查全 PASS（all-pairs CVD ΔE 24.7/26.8，normal 33.6/31.8，对比度 ≥3:1）。
   蓝 = 数据序列，橙 = 「自动关注」状态，两者不混用。 */
:root {
  color-scheme: light;
  --bg: #ffffff;
  --surface: #fcfcfb;
  --line: #e7e5e0;
  --line-soft: #f2f1ed;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --ink-3: #8a8880;
  --series: #2a78d6;
  --series-soft: #dce9f9;
  --hot: #eb6834;
  --hot-soft: #fbe4da;
  --pos: #0a6b3d;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #131312; --surface: #1a1a19; --line: #302f2c; --line-soft: #232322;
    --ink: #ffffff; --ink-2: #c3c2b7; --ink-3: #8a8880;
    --series: #3987e5; --series-soft: #1e3a5f; --hot: #d95926; --hot-soft: #3a241b;
    --pos: #4bbd85;
  }
}
* { box-sizing: border-box; }
body {
  background: var(--bg); color: var(--ink);
  font: 15px/1.6 ui-sans-serif, -apple-system, "Segoe UI", "Noto Sans SC", sans-serif;
  margin: 0 auto; max-width: 1120px; padding: 56px 24px 96px;
  -webkit-font-smoothing: antialiased;
}
a { color: inherit; text-decoration: none; }
a:hover { text-decoration: underline; text-underline-offset: 2px; }

/* 页头 */
.head { margin-bottom: 40px; }
h1 { font-size: 30px; letter-spacing: -0.02em; margin: 0 0 6px; font-weight: 600; }
.meta { color: var(--ink-3); font-size: 13px; margin: 0; font-variant-numeric: tabular-nums; }
.meta .fresh { color: var(--ink-2); }

/* 模块卡片 —— 「模块突出」靠留白 + 细边 + 标题层级，不靠重色块 */
section { background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
          padding: 24px 24px 26px; margin: 0 0 20px; }
section > h2 { font-size: 12px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
               color: var(--ink-3); margin: 0 0 2px; }
section > .sub { color: var(--ink-3); font-size: 13px; margin: 0 0 18px; }

/* Stat tiles —— 单个数字不该画成图 */
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px;
         background: var(--line); border: 1px solid var(--line); border-radius: 14px;
         overflow: hidden; margin: 0 0 20px; }
.tile { background: var(--surface); padding: 20px 22px; }
.tile .k { color: var(--ink-3); font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; }
.tile .v { font-size: 30px; font-weight: 600; letter-spacing: -0.02em; margin-top: 6px;
           font-variant-numeric: tabular-nums; }
.tile .v small { font-size: 14px; font-weight: 500; color: var(--ink-3); margin-left: 4px; }

/* 条形图：细 mark、4px 圆头、锚在基线 */
.bar-row { display: grid; grid-template-columns: minmax(150px, 260px) 1fr 96px; gap: 14px;
           align-items: center; padding: 5px 0; border-radius: 6px; }
.bar-row:hover { background: var(--line-soft); }
.bar-row .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13.5px; }
.bar-track { background: var(--line-soft); border-radius: 4px; height: 10px; }
.bar { background: var(--series); height: 10px; border-radius: 0 4px 4px 0; min-width: 3px; display: block; }
.bar.hot { background: var(--hot); }
.delta { color: var(--ink-2); font-variant-numeric: tabular-nums; text-align: right; font-size: 13px; }

/* 每日发现：列图 */
.cols { display: flex; align-items: flex-end; gap: 2px; height: 116px; }
.col { flex: 1; height: 100%; display: flex; flex-direction: column; justify-content: flex-end;
       align-items: stretch; min-width: 0; border-radius: 4px 4px 0 0; }
.col:hover { background: var(--line-soft); }
.col .v { background: var(--series); width: 100%; border-radius: 3px 3px 0 0; min-height: 2px; }
.axis { display: flex; justify-content: space-between; color: var(--ink-3);
        font-size: 11px; margin-top: 8px; font-variant-numeric: tabular-nums; }

/* 表格 */
.wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { border-bottom: 1px solid var(--line-soft); padding: 9px 10px; text-align: left;
         vertical-align: middle; }
thead th { color: var(--ink-3); font-weight: 600; white-space: nowrap; font-size: 11px;
           letter-spacing: 0.06em; text-transform: uppercase;
           border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--surface); }
tbody tr.r:hover { background: var(--line-soft); }
td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
td.pos { color: var(--pos); }
.spark { display: block; }
.pill { display: inline-block; border: 1px solid var(--line); border-radius: 999px;
        padding: 1px 9px; font-size: 11.5px; color: var(--ink-2); white-space: nowrap; }
.pill.f { border-color: var(--series); color: var(--series); }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
       background: var(--hot); margin-right: 7px; vertical-align: 1px; }
.one { color: var(--ink-2); font-size: 13px; }

/* 可展开行：默认只留摘要列，详情收进点击展开的下一行 */
tr.r { cursor: pointer; }
tr.r:focus-visible { outline: 2px solid var(--series); outline-offset: -2px; }
.car { display: inline-block; color: var(--ink-3); font-size: 11px;
       transition: transform .15s ease; }
tr.r.open .car { transform: rotate(90deg); color: var(--ink-2); }
tr.detail > td { background: var(--line-soft); padding: 14px 18px 16px;
                 border-bottom: 1px solid var(--line); cursor: default; }
.dl { display: grid; grid-template-columns: 88px 1fr; gap: 5px 18px; margin: 0; }
.dl dt { color: var(--ink-3); font-size: 12.5px; }
.dl dd { margin: 0; color: var(--ink-2); font-size: 13.5px; }
.toggle-all { float: right; border: 1px solid var(--line); background: transparent;
              color: var(--ink-3); border-radius: 999px; padding: 2px 12px;
              font-size: 12px; cursor: pointer; font-family: inherit; }
.toggle-all:hover { color: var(--ink-2); border-color: var(--ink-3); }

table { table-layout: fixed; }
th:nth-child(1), td:nth-child(1) { width: 32%; }
th:nth-child(2), td:nth-child(2) { width: 96px; }
th:nth-child(3), td:nth-child(3) { width: 104px; }
th:nth-child(4), td:nth-child(4) { width: 84px; }
th:nth-child(5), td:nth-child(5) { width: 88px; }
th:nth-child(6), td:nth-child(6) { width: 128px; }
th:nth-child(7), td:nth-child(7) { width: 30px; }
td:nth-child(1) { overflow-wrap: anywhere; }
.legend { display: flex; gap: 18px; align-items: center; color: var(--ink-3);
          font-size: 12px; margin-top: 14px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 3px;
            margin-right: 6px; vertical-align: -1px; }
"""

JS = """
document.querySelectorAll("tr.r").forEach(function (tr) {
  function toggle(e) {
    if (e && e.target && e.target.closest("a")) return;  // 点链接不展开
    var d = document.getElementById(tr.dataset.d);
    if (!d) return;
    d.hidden = !d.hidden;
    tr.classList.toggle("open", !d.hidden);
  }
  tr.addEventListener("click", toggle);
  tr.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(e); }
  });
});
var allBtn = document.getElementById("toggle-all");
if (allBtn) {
  allBtn.addEventListener("click", function () {
    var expand = !!document.querySelector("tr.detail[hidden]");
    document.querySelectorAll("tr.detail").forEach(function (d) { d.hidden = !expand; });
    document.querySelectorAll("tr.r").forEach(function (tr) { tr.classList.toggle("open", expand); });
    allBtn.textContent = expand ? "全部收起" : "全部展开";
  });
}
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
            "fit": e.get("fit"),
            "category": e.get("category") or "",
            "one_liner": e.get("one_liner") or "",
            "use_for": e.get("use_for") or "",
            "usage": e.get("usage") or "",
            "example": e.get("example") or "",
            "compare": e.get("compare") or "",
            "overlap": e.get("overlap") or "",
            "verdict": e.get("verdict") or "",
            "similar_to": e.get("similar_to") or "",
            "source": e.get("source") or "github",
            "hn_points": e.get("hn_points") or 0,
            "hn_url": e.get("hn_url") or "",
            "hist": hist,
        })
    rows.sort(key=lambda r: r["delta"], reverse=True)
    return rows


def sparkline(hist: list, w: int = 84, h: int = 22) -> str:
    """一行 star 历史的内联 SVG 折线。历史点不足 2 个就留空。

    形状本身就是信息（在涨 / 走平 / 拐头），比再放一个数字有用。用 SVG 而不是
    图表库：这个页面要自包含地跑在 GitHub Pages 上，不能有外部资源。
    """
    pts = [v for _, v in hist] if hist else []
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1
    n = len(pts) - 1
    coords = [(i * w / n, h - 2 - (v - lo) * (h - 4) / span) for i, v in enumerate(pts)]
    d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    lx, ly = coords[-1]
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
            f'aria-hidden="true"><path d="{d}" fill="none" stroke="var(--series)" '
            f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2" fill="var(--series)"/></svg>')


def bar_chart(rows: list[dict[str, Any]], value_key: str, hot: bool = False) -> str:
    """横向条形图。hot=True 时**按行**判断是否自动关注上橙色 —— 整张图涂橙会跟
    图例「橙色 = 已自动关注」自相矛盾，颜色就不再承载信息。"""
    if not rows:
        return '<p class="meta">还没有数据 —— 历史每天累积。</p>'
    max_v = max(r[value_key] for r in rows) or 1
    return "\n".join(
        f'<div class="bar-row">'
        f'<a class="nm" href="https://github.com/{esc(r["full_name"])}" title="{esc(r["full_name"])}">'
        f'{esc(r["full_name"])}</a>'
        f'<div class="bar-track"><span class="{"bar hot" if hot and r["watched"] else "bar"}" '
        f'style="width:{max(1, int(r[value_key] * 100 / max_v))}%"></span></div>'
        f'<span class="delta">{r["_bar_label"]}</span></div>'
        for r in rows
    )


def detail_html(r: dict[str, Any]) -> str:
    """展开行的完整内容：元信息 + 全字段分析。没分析的字段整行不出现。"""
    items: list[tuple[str, str]] = []
    items.append(("首次收录", f'{esc(r["first_seen"])}（当时 ★{r["then"]:,}）'))
    if r["category"]:
        items.append(("类型", esc(r["category"])))
    if r["source"] == "hn" and r["hn_url"]:
        items.append(("来源", f'Hacker News（{r["hn_points"]} 分）· <a href="{esc(r["hn_url"])}">讨论链接</a>'))
    elif r["source"] == "census":
        items.append(("来源", "周日普查"))
    if r["similar_to"]:
        items.append(("同类跟进", f'与 <a href="https://github.com/{esc(r["similar_to"])}">{esc(r["similar_to"])}</a> 同类'))
    if r["query"]:
        items.append(("命中查询", f'<code>{esc(r["query"])}</code>'))
    if r.get("fit") or r.get("verdict"):
        fit_bits = []
        if r.get("fit"):
            fit_bits.append(f'fit {r["fit"]}/10')
        if r.get("verdict"):
            fit_bits.append(esc(r["verdict"]))
        if r.get("overlap"):
            fit_bits.append(f'重复度：{esc(r["overlap"])}')
        items.append(("契合度", " · ".join(fit_bits)))
    for label, key in (
        ("是什么", "one_liner"),
        ("能做什么", "use_for"),
        ("大家怎么用", "usage"),
        ("举个例子", "example"),
        ("和已有项目比", "compare"),
    ):
        if r[key]:
            items.append((label, esc(r[key])))
    if not any(k in dict(items) for k in ("是什么",)):
        items.append(("分析", "（尚未生成分析）"))
    body = "\n".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in items)
    link = f'<a href="https://github.com/{esc(r["full_name"])}">GitHub ↗</a>'
    return f'<dl class="dl">{body}<dt>链接</dt><dd>{link}</dd></dl>'


def render(seen: dict[str, dict[str, Any]]) -> str:
    rows = repo_rows(seen)
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
    days = sorted(by_day.items())
    timeline_html = "\n".join(
        f'<div class="col" title="{esc(d)}：{n} 个"><div class="v" '
        f'style="height:{max(2, n * 100 // max_n)}%"></div></div>'
        for d, n in days
    )
    axis_html = (f'<span>{esc(days[0][0])}</span><span>峰值 {max_n} 个/天</span>'
                 f'<span>{esc(days[-1][0])}</span>') if days else ""

    # Stat tiles —— 单个数字画成图是反模式，直接给数字
    today = dt.date.today()
    week_ago = (today - dt.timedelta(days=7)).isoformat()
    new_7d = sum(1 for r in rows if r["first_seen"] >= week_ago)
    watched_n = sum(1 for r in rows if r["watched"])
    total_delta = sum(r["delta"] for r in rows if r["delta"] > 0)
    scored = [r for r in rows if r.get("fit")]
    fit_hi = sum(1 for r in scored if r["fit"] >= 8)
    tiles = [
        ("追踪中", f"{len(rows):,}", "个 repo"),
        ("近 7 天新发现", f"{new_7d:,}", "个"),
        ("自动关注", f"{watched_n:,}", "个"),
        ("累计涨星", f"{total_delta:,}", "★"),
    ]
    if scored:
        tiles.append(("fit ≥ 8", f"{fit_hi:,}", f"/ {len(scored)} 已打分"))
    tiles_html = "\n".join(
        f'<div class="tile"><div class="k">{esc(k)}</div>'
        f'<div class="v">{esc(v)}<small>{esc(u)}</small></div></div>'
        for k, v, u in tiles)

    # 全部追踪：紧凑主行 + 点击展开的详情行
    table_parts = []
    for i, r in enumerate(rows):
        delta_cls = "num pos" if r["delta"] > 0 else "num"
        delta_txt = f"+{r['delta']:,}" if r["delta"] > 0 else f"{r['delta']:,}"
        rate_txt = f"+{r['rate']:,.0f}/天" if r["rate"] else "—"
        score_txt = f'<span class="pill">{r["score"]}/10</span>' if r["score"] else ""
        if r.get("fit"):
            score_txt += f' <span class="pill f">fit {r["fit"]}</span>'
        flame = '<span class="dot" title="自动关注"></span>' if r["watched"] else ""
        table_parts.append(
            f'<tr class="r" data-d="d{i}" tabindex="0" title="点击展开完整分析">'
            f'<td>{flame}<a href="https://github.com/{esc(r["full_name"])}">{esc(r["full_name"])}</a></td>'
            f'<td>{sparkline(r["hist"])}</td>'
            f'<td class="num">{r["now"]:,}</td>'
            f'<td class="{delta_cls}">{delta_txt}</td>'
            f'<td class="num">{rate_txt}</td>'
            f"<td>{score_txt}</td>"
            f'<td><span class="car">▸</span></td>'
            "</tr>"
            f'<tr class="detail" id="d{i}" hidden><td colspan="7">{detail_html(r)}</td></tr>'
        )
    table_rows = "\n".join(table_parts)

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Discovery</title>
<style>{CSS}</style>
</head>
<body>
<header class="head">
  <h1>Agent Discovery</h1>
  <p class="meta"><span class="fresh">数据更新于 {esc(generated)}</span> · 每晚 22:00 UTC 自动更新 · 自 {esc(first_dates[0] if first_dates else "—")} 起追踪</p>
</header>

<div class="tiles">
{tiles_html}
</div>

<section>
  <h2>日均涨速</h2>
  <p class="sub">首次收录以来的平均 stars/天，Top {TOP_MOVERS}。橙色 = 已自动关注。</p>
{rate_html}
  <div class="legend"><span><i style="background:var(--hot)"></i>自动关注</span>
  <span><i style="background:var(--series)"></i>其余</span>
  <span>score ≥ 7 且日均 ≥ 20，或日均 ≥ 100</span></div>
</section>

<section>
  <h2>累计涨幅</h2>
  <p class="sub">从首次收录到现在涨了多少星，Top {TOP_MOVERS}。</p>
{movers_html}
</section>

<section>
  <h2>每日新发现</h2>
  <p class="sub">每天首次进入追踪表的 repo 数。</p>
  <div class="cols">
{timeline_html}
  </div>
  <div class="axis">{axis_html}</div>
</section>

<section>
  <h2>全部追踪</h2>
  <p class="sub"><button id="toggle-all" class="toggle-all">全部展开</button>
  {len(rows)} 个 repo。走势为首收至今的 star 曲线；点任意一行展开完整分析（类型 / 来源 / 是什么 / 能做什么 / 例子 / 对比）。</p>
  <div class="wrap">
  <table>
  <thead><tr><th>Repo</th><th>走势</th><th>Stars</th><th>Δ</th><th>日均</th><th>评分</th><th></th></tr></thead>
  <tbody>
{table_rows}
  </tbody>
  </table>
  </div>
</section>
<script>{JS}</script>
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
