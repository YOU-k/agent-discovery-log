#!/usr/bin/env python3
"""Weekly Xiaohongshu (小红书) draft generator.

Turns the week's tracking data (new high-score repos, velocity leaders,
auto-watched) into a ready-to-post XHS note draft: title / body / tags /
cover idea, all grounded strictly in the collected numbers. Writes
publish/YYYY-MM-DD-xhs.md and pushes a Feishu card for copy-paste.

Runs inside the daily workflow but no-ops except on Mondays (周报节奏);
use --force to generate any day.

Usage:
    python3 scripts/publish_xhs.py [--force] [--dry-run]

Env:
    LLM_API_KEY / DEEPSEEK_API_KEY — required
    FEISHU_WEBHOOK / FEISHU_KEYWORD — optional, enables the Feishu card
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "seen.json"
PUBLISH = ROOT / "publish"

sys.path.insert(0, str(ROOT / "scripts"))
import discover  # noqa: E402  (llm_chat, daily_rate)
import notify  # noqa: E402  (humanize_text, sign_payload)

DRAFT_DAY = 0  # Monday
WEEK_DAYS = 7
MAX_NEW_FACTS = 6
MAX_RATE_FACTS = 5

WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
KEYWORD = os.environ.get("FEISHU_KEYWORD", "")

XHS_PROMPT = """你是小红书博主，写 AI 工具/开发者向内容，读者是普通程序员和 AI 爱好者。
基于下面的真实数据写一条小红书笔记草稿。只允许使用给出的名字、数字和事实，不许编造。

本周数据：
{facts}

要求：
1. title: ≤20字，有钩子，可带 1-2 个 emoji
2. body: 250-450字。口语、短句、多分段，像真人随手分享。开头一句钩子；
   中间挑 2-4 个最有话题性的项目介绍（用给定数字，涨速比静态星数更有说服力）；
   结尾一句互动引导（比如"你们有用过类似的吗"）
3. tags: 5-8 个话题标签（#开头）
4. cover_idea: 一句话描述首图（大字标题写什么、怎么排版）

只输出 JSON（不要任何其他文字、不要代码围栏）：
{{"title": "...", "body": "...", "tags": ["#AI", "..."], "cover_idea": "..."}}"""


def collect_facts(seen: dict[str, dict[str, Any]]) -> str:
    """Gather this week's facts as plain text for the prompt."""
    week_ago = (dt.date.today() - dt.timedelta(days=WEEK_DAYS)).isoformat()

    new_week = [
        (fn, e) for fn, e in seen.items()
        if e.get("first_seen", "") >= week_ago and e.get("score") and not e.get("similar_to")
    ]
    new_week.sort(key=lambda kv: kv[1].get("score") or 0, reverse=True)

    rated = []
    for fn, e in seen.items():
        rate = discover.daily_rate(e)
        if rate and rate > 0:
            hist = e.get("stars_history") or []
            rated.append((fn, rate, hist[0][1] if hist else 0, hist[-1][1] if hist else 0))
    rated.sort(key=lambda r: r[1], reverse=True)

    parts = []
    if new_week:
        parts.append("本周新发现的高分项目：")
        for fn, e in new_week[:MAX_NEW_FACTS]:
            line = f"- {fn} ★{(e.get('stars_at_first_seen') or 0):,} · 评分 {e['score']}/10 · {e.get('category') or ''}"
            if e.get("one_liner"):
                line += f" — {e['one_liner']}"
            parts.append(line)
    if rated:
        parts.append("涨速榜（自首收起日均涨星）：")
        for fn, rate, then, now in rated[:MAX_RATE_FACTS]:
            parts.append(f"- {fn} 日均 +{rate:,.0f}（{then:,} → {now:,}）")
    watched = [fn for fn, e in seen.items() if discover.is_watched(e)][:5]
    if watched:
        parts.append("系统自动标记的关注对象：" + "、".join(watched))
    parts.append(f"（背景：这是一个自动追踪系统，当前共追踪 {len(seen)} 个 GitHub 上的 AI agent / Claude Code skill 项目）")
    return "\n".join(parts)


def make_draft(facts: str) -> dict[str, Any] | None:
    text = discover.llm_chat(XHS_PROMPT.format(facts=facts), max_tokens=3000)
    if not text:
        return None
    try:
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0]
        draft = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[WARN] draft parse failed: {e}", file=sys.stderr)
        return None
    body = notify.humanize_text(str(draft.get("body") or ""))
    if body:
        draft["body"] = body
    return draft


def render_md(draft: dict[str, Any], total: int) -> str:
    tags = " ".join(draft.get("tags") or [])
    return (
        f"# {draft.get('title', '')}\n\n"
        f"{draft.get('body', '')}\n\n"
        f"{tags}\n\n"
        f"---\n"
        f"> 首图建议：{draft.get('cover_idea', '')}\n"
        f"> 数据来源：agent-discovery-log（追踪 {total} 个 repo · 截至 {dt.date.today().isoformat()}）\n"
    )


def post_feishu(md: str) -> int:
    if not WEBHOOK:
        print("[INFO] FEISHU_WEBHOOK not set — skipping", file=sys.stderr)
        return 0
    today = dt.date.today().isoformat()
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"{KEYWORD + ' · ' if KEYWORD else ''}小红书草稿 · {today}"},
                "template": "red",
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md}}],
        },
    }
    if os.environ.get("FEISHU_SECRET"):
        payload = notify.sign_payload(payload)
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.load(r)
    if resp.get("code") not in (0, None):
        print(f"[WARN] Feishu returned: {resp}", file=sys.stderr)
        return 1
    print("[INFO] Feishu card sent", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Generate even when not Monday.")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of writing/sending.")
    args = parser.parse_args()

    if not args.force and not args.dry_run and dt.date.today().weekday() != DRAFT_DAY:
        print("[INFO] not draft day (Monday) — use --force to override", file=sys.stderr)
        return 0

    seen: dict[str, dict[str, Any]] = json.loads(STATE.read_text(encoding="utf-8"))
    facts = collect_facts(seen)
    draft = make_draft(facts)
    if not draft:
        print("[WARN] no draft generated", file=sys.stderr)
        return 1
    md = render_md(draft, len(seen))

    if args.dry_run:
        print(md)
        return 0

    PUBLISH.mkdir(parents=True, exist_ok=True)
    out = PUBLISH / f"{dt.date.today().isoformat()}-xhs.md"
    out.write_text(md, encoding="utf-8")
    print(f"[INFO] wrote {out.relative_to(ROOT)}", file=sys.stderr)
    return post_feishu(md)


if __name__ == "__main__":
    raise SystemExit(main())
