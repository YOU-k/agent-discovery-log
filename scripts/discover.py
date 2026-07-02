#!/usr/bin/env python3
"""Daily GitHub discovery for agent/skill/framework repos.

Runs a curated set of queries against the GitHub search API, diffs against
state/seen.json, and writes new findings to discoveries/YYYY-MM-DD.md.

Optional: if ANTHROPIC_API_KEY is set, each new repo gets a relevance score
and one-line summary from Claude Haiku (cheap). Without a key, findings are
ranked by star velocity and recency only.

Usage:
    python3 scripts/discover.py [--dry-run]

Env:
    GH_TOKEN or GITHUB_TOKEN — required for gh API rate limits
    ANTHROPIC_API_KEY        — optional, enables LLM scoring
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
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


def score_with_claude(repos: list[Repo]) -> dict[str, tuple[int, str]]:
    """If ANTHROPIC_API_KEY is set, ask Claude to score each repo.

    Returns dict {full_name: (score, one_liner)}. Score is 1-10.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    try:
        from anthropic import Anthropic
    except ImportError:
        print("[INFO] anthropic SDK not installed, skipping LLM scoring", file=sys.stderr)
        return {}

    client = Anthropic()
    listing = "\n".join(
        f"{i+1}. {r.full_name} (★{r.stars}) — {r.description}"
        for i, r in enumerate(repos)
    )
    prompt = f"""Rate each GitHub repo below for relevance to a developer building
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
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]

    try:
        data = json.loads(text)
        return {item["full_name"]: (item["score"], item["one_liner"]) for item in data}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"[WARN] Claude scoring parse failed: {e}", file=sys.stderr)
        return {}


def load_seen() -> dict[str, dict[str, Any]]:
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_seen(seen: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")


def render_daily(new_repos: list[Repo], scores: dict[str, tuple[int, str]]) -> str:
    today = dt.date.today().isoformat()
    lines = [
        f"# Agent Discovery · {today}",
        "",
        f"Found **{len(new_repos)} new repos** across {len(QUERIES)} queries.",
        "",
    ]
    if scores:
        new_repos.sort(key=lambda r: scores.get(r.full_name, (0, ""))[0], reverse=True)
        lines += ["Sorted by Claude relevance score (higher = more relevant).", ""]
    else:
        new_repos.sort(key=lambda r: r.stars, reverse=True)
        lines += ["Sorted by stars (no LLM scoring; set ANTHROPIC_API_KEY to enable).", ""]

    for r in new_repos:
        score, one_liner = scores.get(r.full_name, (None, None))
        badge = f"[score {score}/10] " if score else ""
        lines += [
            f"## {badge}{r.full_name}  ·  ★{r.stars}",
            "",
            f"- {r.description}",
        ]
        if one_liner:
            lines += [f"- **Claude take**: {one_liner}"]
        lines += [
            f"- Updated: {r.updated_at[:10]}",
            f"- Query hit: `{r.matched_query}`",
            f"- <{r.url}>",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print instead of writing files.")
    parser.add_argument("--max-scored", type=int, default=20, help="Cap on LLM-scored repos.")
    args = parser.parse_args()

    seen = load_seen()

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

    if not new_repos:
        print("[INFO] no new repos today", file=sys.stderr)
        return 0

    # Score the top N
    to_score = new_repos[: args.max_scored]
    scores = score_with_claude(to_score)

    # Persist state (all new repos, whether written or not)
    today_iso = dt.date.today().isoformat()
    for r in new_repos:
        s, ol = scores.get(r.full_name, (None, None))
        seen[r.full_name] = {
            "first_seen": today_iso,
            "stars_at_first_seen": r.stars,
            "matched_query": r.matched_query,
            "score": s,
            "one_liner": ol,
        }

    # Render daily
    md = render_daily(new_repos, scores)

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
