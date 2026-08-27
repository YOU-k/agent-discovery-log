#!/usr/bin/env python3
"""Daily GitHub discovery for agent/skill/framework repos.

Runs a curated set of queries against the GitHub search API, diffs against
state/seen.json, and writes new findings to discoveries/YYYY-MM-DD.md.

Optional: if an LLM API key is set, each new repo gets a relevance score
plus a plain-language Chinese analysis (类型 / 是什么 / 能做什么 /
大家怎么用 / 举个例子 / 和已有项目比), grounded with a README excerpt.
Any OpenAI-compatible endpoint works (default: DeepSeek); Anthropic is
supported as a fallback. Without a key, findings are ranked by stars only.

Each run also refreshes star counts for all previously seen repos
(stars_history in state/seen.json) and reports the biggest gainers.

Usage:
    python3 scripts/discover.py [--dry-run] [--backfill-days N [--force]]

Env:
    GH_TOKEN or GITHUB_TOKEN      — required for gh API rate limits
    LLM_API_KEY / DEEPSEEK_API_KEY — optional, enables LLM scoring
    LLM_BASE_URL                  — optional, default https://api.deepseek.com/v1
    LLM_MODEL                     — optional, default deepseek-chat
    ANTHROPIC_API_KEY             — optional fallback scorer
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "seen.json"
DISCOVERIES = ROOT / "discoveries"

# Curated queries — each with a weight (higher = more relevant to us).
# Order matters: earlier queries dominate for repos that match multiple.
QUERIES: list[tuple[str, int]] = [
    ("claude code skill", 10),
    ("claude code subagent", 10),
    ("multi-agent orchestration framework", 9),
    ("multi agent framework claude", 9),
    ("agent orchestration cli", 8),
    ("ai coding agent framework", 7),
    ("llm agent framework", 6),
    ("prompt engineering agent", 5),
    ("awesome agent skills", 4),
    ("awesome llm agents", 4),
]

# Filters
STAR_MIN = 50
UPDATED_WITHIN_DAYS = 60

# LLM scoring — any OpenAI-compatible endpoint (DeepSeek by default).
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1"
LLM_MODEL = os.environ.get("LLM_MODEL") or "deepseek-chat"

README_EXCERPT_CHARS = 1500

SCORE_PROMPT = """下面是一些 GitHub 仓库（名称、star 数、描述，部分附 README 摘要）。
请站在「想给日常工作找好用 AI 工具的普通开发者」的视角评估，全程用大白话，
避免术语黑话，让不关注 AI 圈的人也能看懂。

对每个仓库输出（全部用中文，score 除外）：
- score: 1-10 的相关性评分（Claude Code skills、agent 编排、prompt 工程模式、多智能体框架 = 高相关）
- category: 类型，如 Claude Code skill / subagent / 多智能体框架 / 资源合集 / 工具 / 其他
- what: 它是什么（≤25字，大白话）
- use_for: 能拿它做什么（≤45字）
- usage: 大家实际怎么用它（≤45字；不知道就根据 README 合理推断，不要编造具体用户或数字）
- example: 一个具体使用例子（≤60字：谁来用、输入什么、得到什么结果，例如「在 Claude Code 里输入 /xx，它会……」）
- compare: 和「已在追踪的项目」（见末尾列表）中同类的相比，它的差异或创新点是什么、
  为什么值得用它而不是已有的（≤50字；没有完全可比的，就写它填补了哪类空白）

只输出 JSON（不要任何其他文字、不要代码围栏）：
[
  {{"full_name": "owner/name", "score": 8, "category": "...", "what": "...", "use_for": "...", "usage": "...", "example": "...", "compare": "..."}},
  ...
]

仓库：
{listing}

已在追踪的项目（仅供 compare 字段对比用）：
{tracked}"""

# A scored analysis for one repo.
Score = dict[str, Any]  # keys: score, category, what, use_for, usage, example, compare


@dataclass
class Repo:
    full_name: str
    description: str
    stars: int
    url: str
    updated_at: str
    matched_query: str
    matched_weight: int


def gh_search(query: str) -> list[dict[str, Any]]:
    """Search GH via gh CLI. Returns list of repo dicts."""
    cmd = [
        "gh", "search", "repos",
        "--limit", "50",
        "--sort", "stars",
        "--json", "fullName,description,stargazersCount,url,updatedAt",
        query,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"[WARN] gh search failed for '{query}': {result.stderr}", file=sys.stderr)
        return []
    return json.loads(result.stdout)


def gh_repo_meta(full_name: str) -> dict[str, Any] | None:
    """Fetch repo metadata (description, stars, url, updatedAt)."""
    result = subprocess.run(
        ["gh", "api", f"repos/{full_name}"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"[WARN] gh api failed for {full_name}: {result.stderr[:120]}", file=sys.stderr)
        return None
    return json.loads(result.stdout)


def gh_readme_excerpt(full_name: str) -> str:
    """Fetch the first chars of a repo's README (raw), '' on failure."""
    result = subprocess.run(
        ["gh", "api", f"repos/{full_name}/readme", "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return ""
    return result.stdout[:README_EXCERPT_CHARS]


def filter_repo(r: dict[str, Any]) -> bool:
    """Basic quality gate."""
    if r["stargazersCount"] < STAR_MIN:
        return False
    updated = dt.datetime.fromisoformat(r["updatedAt"].replace("Z", "+00:00"))
    age = (dt.datetime.now(dt.timezone.utc) - updated).days
    if age > UPDATED_WITHIN_DAYS:
        return False
    if not r.get("description"):
        return False
    return True


def llm_chat(prompt: str, max_tokens: int = 4000, json_mode: bool = True) -> str:
    """Raw chat completion via the OpenAI-compatible endpoint. '' if unconfigured/failed."""
    if not LLM_API_KEY:
        return ""
    body: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.load(r)
        return resp["choices"][0]["message"]["content"].strip()
    except (OSError, KeyError, json.JSONDecodeError) as e:
        print(f"[WARN] LLM chat failed: {e}", file=sys.stderr)
        return ""


def _parse_scores(text: str) -> dict[str, Score]:
    """Parse the LLM's JSON answer into {full_name: analysis}."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    out: dict[str, Score] = {}
    for item in json.loads(text):
        if not isinstance(item, dict) or "full_name" not in item:
            continue
        try:
            score = int(float(item.get("score") or 0))
        except (TypeError, ValueError):
            score = 0
        out[item["full_name"]] = {
            "score": score,
            "category": str(item.get("category") or ""),
            "what": str(item.get("what") or ""),
            "use_for": str(item.get("use_for") or ""),
            "usage": str(item.get("usage") or ""),
            "example": str(item.get("example") or ""),
            "compare": str(item.get("compare") or ""),
        }
    return out


def tracked_summary(seen: dict[str, dict[str, Any]], limit: int = 80) -> str:
    """One-line-per-repo list of everything already tracked (for compare prompts)."""
    entries = sorted(seen.items(), key=lambda kv: kv[1].get("stars_at_first_seen") or 0, reverse=True)
    parts = []
    for fn, e in entries[:limit]:
        parts.append(f"{fn}（{e['one_liner']}）" if e.get("one_liner") else fn)
    return "；".join(parts)


def score_with_llm(repos: list[Repo], tracked: str = "") -> dict[str, Score]:
    """Score + analyze repos with whatever LLM API is configured.

    Priority: OpenAI-compatible endpoint (LLM_API_KEY / DEEPSEEK_API_KEY,
    LLM_BASE_URL, LLM_MODEL) → Anthropic (ANTHROPIC_API_KEY).
    Returns {full_name: analysis}; {} if nothing is configured.
    """
    if not repos:
        return {}
    blocks = []
    for i, r in enumerate(repos):
        block = f"{i+1}. {r.full_name} (★{r.stars}) — {r.description}"
        excerpt = gh_readme_excerpt(r.full_name)
        if excerpt:
            block += f"\n   README 摘要: {excerpt}"
        blocks.append(block)
    prompt = SCORE_PROMPT.format(listing="\n".join(blocks), tracked=tracked or "（暂无）")
    try:
        if LLM_API_KEY:
            return _parse_scores(llm_chat(prompt))
        if os.environ.get("ANTHROPIC_API_KEY"):
            return _score_anthropic(prompt)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as e:
        print(f"[WARN] LLM scoring failed: {e}", file=sys.stderr)
    return {}


def _score_anthropic(prompt: str) -> dict[str, Score]:
    try:
        from anthropic import Anthropic
    except ImportError:
        print("[INFO] anthropic SDK not installed, skipping LLM scoring", file=sys.stderr)
        return {}

    client = Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return _parse_scores(text)


def apply_score(entry: dict[str, Any], s: Score) -> None:
    """Write an analysis into a seen.json entry."""
    entry["score"] = s.get("score")
    entry["one_liner"] = s.get("what") or entry.get("one_liner")
    entry["category"] = s.get("category")
    entry["use_for"] = s.get("use_for")
    entry["usage"] = s.get("usage")
    entry["example"] = s.get("example")
    entry["compare"] = s.get("compare")


def backfill_scores(
    seen: dict[str, dict[str, Any]],
    days: int,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """Re-score repos first_seen within the last N days.

    Skips entries that already have analysis unless force=True.
    """
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    tracked = tracked_summary(seen)
    targets: list[Repo] = []
    for fn, e in sorted(seen.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True):
        if not force and e.get("score"):
            continue
        if e.get("first_seen", "") < cutoff:
            continue
        meta = gh_repo_meta(fn)
        if not meta:
            continue
        targets.append(Repo(
            full_name=fn,
            description=meta.get("description") or "",
            stars=meta.get("stargazers_count") or e.get("stars_at_first_seen") or 0,
            url=meta.get("html_url") or f"https://github.com/{fn}",
            updated_at=meta.get("updated_at") or "",
            matched_query=e.get("matched_query", ""),
            matched_weight=0,
        ))
        if len(targets) >= 20:
            break
    if not targets:
        return 0
    print(f"[INFO] backfilling analysis for {len(targets)} repos", file=sys.stderr)
    results = score_with_llm(targets, tracked)
    if dry_run:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    for fn, s in results.items():
        if fn in seen:
            apply_score(seen[fn], s)
    return len(results)


def refresh_stars(seen: dict[str, dict[str, Any]]) -> None:
    """Snapshot current stars for every seen repo via batched GraphQL.

    Appends [today, stars] to each entry's stars_history, initializing the
    history from stars_at_first_seen for entries that predate star tracking.
    Repos that were renamed/deleted keep their old history untouched.
    """
    today = dt.date.today().isoformat()
    names = sorted(seen)
    current: dict[str, int] = {}
    for i in range(0, len(names), 50):
        chunk = names[i : i + 50]
        parts = []
        for j, fn in enumerate(chunk):
            owner, _, name = fn.partition("/")
            parts.append(
                f"r{j}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)})"
                " { stargazerCount }"
            )
        query = "query { " + " ".join(parts) + " }"
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True, text=True, timeout=120,
        )
        try:
            data = json.loads(result.stdout).get("data") or {}
        except json.JSONDecodeError:
            print(f"[WARN] star refresh failed: {result.stderr[:200]}", file=sys.stderr)
            continue
        for j, fn in enumerate(chunk):
            node = data.get(f"r{j}")
            if node:
                current[fn] = node["stargazerCount"]

    for fn, entry in seen.items():
        hist = entry.setdefault("stars_history", [])
        if not hist and entry.get("stars_at_first_seen") is not None:
            hist.append([entry["first_seen"], entry["stars_at_first_seen"]])
        if fn in current:
            if hist and hist[-1][0] == today:
                hist[-1][1] = current[fn]
            else:
                hist.append([today, current[fn]])


def top_movers(seen: dict[str, dict[str, Any]], limit: int = 10) -> list[tuple[str, str, int, int, int]]:
    """Biggest star gainers since first seen (excludes repos found today).

    Returns rows of (full_name, first_seen, stars_then, stars_now, delta).
    """
    today = dt.date.today().isoformat()
    rows = []
    for fn, e in seen.items():
        hist = e.get("stars_history") or []
        if e.get("first_seen") == today or len(hist) < 2:
            continue
        then, now = hist[0][1], hist[-1][1]
        if now > then:
            rows.append((fn, e.get("first_seen", ""), then, now, now - then))
    rows.sort(key=lambda r: r[4], reverse=True)
    return rows[:limit]


def daily_rate(entry: dict[str, Any]) -> float | None:
    """Average stars/day over the tracked span. None if not enough history."""
    hist = entry.get("stars_history") or []
    if len(hist) < 2:
        return None
    d0 = dt.date.fromisoformat(hist[0][0])
    d1 = dt.date.fromisoformat(hist[-1][0])
    days = (d1 - d0).days
    if days < 1:
        return None
    return (hist[-1][1] - hist[0][1]) / days


# Auto-watch thresholds (transparent rules, not LLM judgment).
WATCH_MIN_SCORE = 7    # high-relevance …
WATCH_MIN_RATE = 20.0  # … and steadily rising, or
WATCH_HOT_RATE = 100.0 # exploding regardless of score
COOLING_MIN_RATE = 20.0      # was hot overall …
COOLING_RECENT_RATIO = 0.2   # … but last-7d gain < 20% of expected


def is_watched(entry: dict[str, Any]) -> bool:
    """Auto-watch: high relevance + steady rise, or velocity explosion."""
    rate = daily_rate(entry)
    if rate is None:
        return False
    if rate >= WATCH_HOT_RATE:
        return True
    return (entry.get("score") or 0) >= WATCH_MIN_SCORE and rate >= WATCH_MIN_RATE


def is_cooling(entry: dict[str, Any]) -> bool:
    """Was hot overall but nearly flat over the last week. Needs ≥8 daily points."""
    hist = entry.get("stars_history") or []
    if len(hist) < 8:
        return False
    rate = daily_rate(entry)
    if rate is None or rate < COOLING_MIN_RATE:
        return False
    recent_gain = hist[-1][1] - hist[-8][1]
    return recent_gain < rate * 7 * COOLING_RECENT_RATIO


def load_seen() -> dict[str, dict[str, Any]]:
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_seen(seen: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


def render_daily(
    new_repos: list[Repo],
    scores: dict[str, Score],
    movers: list[tuple[str, str, int, int, int]],
) -> str:
    today = dt.date.today().isoformat()
    lines = [
        f"# Agent Discovery · {today}",
        "",
        f"Found **{len(new_repos)} new repos** across {len(QUERIES)} queries.",
        "",
    ]
    if new_repos:
        if scores:
            new_repos.sort(key=lambda r: (scores.get(r.full_name) or {}).get("score", 0), reverse=True)
            lines += ["Sorted by LLM relevance score (higher = more relevant).", ""]
        else:
            new_repos.sort(key=lambda r: r.stars, reverse=True)
            lines += ["Sorted by stars (no LLM scoring; set LLM_API_KEY to enable).", ""]

    for r in new_repos:
        s = scores.get(r.full_name) or {}
        badge = f"[score {s['score']}/10] " if s.get("score") else ""
        lines += [f"## {badge}{r.full_name}  ·  ★{r.stars}", ""]
        if s.get("category"):
            lines += [f"- **类型**: {s['category']}"]
        lines += [f"- {r.description}"]
        for label, key in (
            ("是什么", "what"),
            ("能做什么", "use_for"),
            ("大家怎么用", "usage"),
            ("举个例子", "example"),
            ("和已有项目比", "compare"),
        ):
            if s.get(key):
                lines += [f"- **{label}**: {s[key]}"]
        lines += [
            f"- Updated: {r.updated_at[:10]}",
            f"- Query hit: `{r.matched_query}`",
            f"- <{r.url}>",
            "",
        ]

    if movers:
        lines += [
            "## Trending since first seen",
            "",
            "| Repo | First seen | Stars then → now | Δ |",
            "|---|---|---|---|",
        ]
        for fn, first_seen, then, now, delta in movers:
            lines.append(f"| [{fn}](https://github.com/{fn}) | {first_seen} | {then} → {now} | +{delta} |")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print instead of writing files.")
    parser.add_argument("--max-scored", type=int, default=20, help="Cap on LLM-scored repos.")
    parser.add_argument("--backfill-days", type=int, default=0, metavar="N",
                        help="Re-score repos first_seen in the last N days that lack analysis; "
                             "updates state only, then exits.")
    parser.add_argument("--force", action="store_true",
                        help="With --backfill-days: re-score even entries that already have analysis.")
    args = parser.parse_args()

    seen = load_seen()
    today_iso = dt.date.today().isoformat()

    # Maintenance mode: fill in analysis for recent repos that predate scoring.
    if args.backfill_days:
        n = backfill_scores(seen, args.backfill_days, args.dry_run, args.force)
        print(f"[INFO] backfilled analysis for {n} repos", file=sys.stderr)
        if not args.dry_run and n:
            save_seen(seen)
        return 0

    # Collect + dedupe candidates
    candidates: dict[str, Repo] = {}
    for query, weight in QUERIES:
        print(f"[INFO] searching: {query!r}", file=sys.stderr)
        for r in gh_search(query):
            if not filter_repo(r):
                continue
            fn = r["fullName"]
            if fn in seen:
                continue
            if fn in candidates:
                continue
            candidates[fn] = Repo(
                full_name=fn,
                description=r["description"] or "",
                stars=r["stargazersCount"],
                url=r["url"],
                updated_at=r["updatedAt"],
                matched_query=query,
                matched_weight=weight,
            )

    new_repos = sorted(candidates.values(), key=lambda r: r.stars, reverse=True)
    print(f"[INFO] {len(new_repos)} new repos after dedup", file=sys.stderr)

    # Register new repos in state (analysis filled in below if scored)
    for r in new_repos:
        seen[r.full_name] = {
            "first_seen": today_iso,
            "stars_at_first_seen": r.stars,
            "matched_query": r.matched_query,
            "score": None,
            "one_liner": None,
            "stars_history": [[today_iso, r.stars]],
        }

    # Score the top N new repos (with the tracked list as compare context)
    scores: dict[str, Score] = {}
    if new_repos:
        print(f"[INFO] scoring {min(len(new_repos), args.max_scored)} repos", file=sys.stderr)
        scores = score_with_llm(new_repos[: args.max_scored], tracked_summary(seen))
        for fn, s in scores.items():
            if fn in seen:
                apply_score(seen[fn], s)

    # Snapshot current stars for everything we've ever seen
    refresh_stars(seen)
    movers = top_movers(seen)

    md = render_daily(new_repos, scores, movers)

    if args.dry_run:
        print(md)
        return 0

    DISCOVERIES.mkdir(parents=True, exist_ok=True)
    out = DISCOVERIES / f"{today_iso}.md"
    out.write_text(md, encoding="utf-8")
    save_seen(seen)
    print(f"[INFO] wrote {out.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
