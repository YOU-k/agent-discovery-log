#!/usr/bin/env python3
"""Daily GitHub discovery for agent/skill/framework repos.

Runs a curated set of queries against the GitHub search API, diffs against
state/seen.json, and writes new findings to discoveries/YYYY-MM-DD.md.

Optional: if an LLM API key is set, each new repo gets a relevance score
and one-line summary. Any OpenAI-compatible endpoint works (default:
DeepSeek); Anthropic is supported as a fallback. Without a key, findings
are ranked by stars only.

Each run also refreshes star counts for all previously seen repos
(stars_history in state/seen.json) and reports the biggest gainers.

Usage:
    python3 scripts/discover.py [--dry-run]

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
]

# Filters
STAR_MIN = 50
UPDATED_WITHIN_DAYS = 60

# LLM scoring — any OpenAI-compatible endpoint (DeepSeek by default).
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1"
LLM_MODEL = os.environ.get("LLM_MODEL") or "deepseek-chat"

SCORE_PROMPT = """Rate each GitHub repo below for relevance to a developer building
a Python multi-agent orchestration framework that runs on top of Claude Code.
High relevance: Claude Code skills, agent orchestration, prompt engineering
patterns, multi-agent frameworks, agent design patterns.

Repos:
{listing}

Output JSON only (no prose, no code fences):
[
  {{"full_name": "owner/name", "score": 1-10, "one_liner": "≤ 15 words"}},
  ...
]"""


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


def _parse_scores(text: str) -> dict[str, tuple[int, str]]:
    """Parse the LLM's JSON answer into {full_name: (score, one_liner)}."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    data = json.loads(text)
    return {item["full_name"]: (item["score"], item["one_liner"]) for item in data}


def score_with_llm(repos: list[Repo]) -> dict[str, tuple[int, str]]:
    """Score repos with whatever LLM API is configured.

    Priority: OpenAI-compatible endpoint (LLM_API_KEY / DEEPSEEK_API_KEY,
    LLM_BASE_URL, LLM_MODEL) → Anthropic (ANTHROPIC_API_KEY).
    Returns {full_name: (score, one_liner)}; {} if nothing is configured.
    """
    listing = "\n".join(
        f"{i+1}. {r.full_name} (★{r.stars}) — {r.description}"
        for i, r in enumerate(repos)
    )
    prompt = SCORE_PROMPT.format(listing=listing)
    try:
        if LLM_API_KEY:
            return _score_openai_compat(prompt)
        if os.environ.get("ANTHROPIC_API_KEY"):
            return _score_anthropic(prompt)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError) as e:
        print(f"[WARN] LLM scoring failed: {e}", file=sys.stderr)
    return {}


def _score_openai_compat(prompt: str) -> dict[str, tuple[int, str]]:
    req = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4000,
            "response_format": {"type": "json_object"},
        }).encode(),
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    return _parse_scores(resp["choices"][0]["message"]["content"])


def _score_anthropic(prompt: str) -> dict[str, tuple[int, str]]:
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


def load_seen() -> dict[str, dict[str, Any]]:
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_seen(seen: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


def render_daily(
    new_repos: list[Repo],
    scores: dict[str, tuple[int, str]],
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
            new_repos.sort(key=lambda r: scores.get(r.full_name, (0, ""))[0], reverse=True)
            lines += ["Sorted by LLM relevance score (higher = more relevant).", ""]
        else:
            new_repos.sort(key=lambda r: r.stars, reverse=True)
            lines += ["Sorted by stars (no LLM scoring; set LLM_API_KEY to enable).", ""]

    for r in new_repos:
        score, one_liner = scores.get(r.full_name, (None, None))
        badge = f"[score {score}/10] " if score else ""
        lines += [
            f"## {badge}{r.full_name}  ·  ★{r.stars}",
            "",
            f"- {r.description}",
        ]
        if one_liner:
            lines += [f"- **LLM take**: {one_liner}"]
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
    args = parser.parse_args()

    seen = load_seen()
    today_iso = dt.date.today().isoformat()

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

    # Register new repos in state (score/one_liner filled in below if scored)
    for r in new_repos:
        seen[r.full_name] = {
            "first_seen": today_iso,
            "stars_at_first_seen": r.stars,
            "matched_query": r.matched_query,
            "score": None,
            "one_liner": None,
            "stars_history": [[today_iso, r.stars]],
        }

    # Score the top N new repos
    scores: dict[str, tuple[int, str]] = {}
    if new_repos:
        scores = score_with_llm(new_repos[: args.max_scored])
        for fn, (s, ol) in scores.items():
            seen[fn]["score"] = s
            seen[fn]["one_liner"] = ol

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
