#!/usr/bin/env python3
"""Weekly Xiaohongshu (小红书) deep-dive draft generator.

Picks ONE project worth talking about (highest-score novel find of the
week, or --repo override), gathers real material — full README, our
tracking data, HN discussions, and optional Tavily web context — and
writes a deep-dive note draft: what it is, why it's hot, how to actually
use it, who it's for. Facts only from the gathered material.

Writes publish/YYYY-MM-DD-xhs.md, records coverage in publish/.covered.json
(no repeat stories), and pushes a Feishu card for copy-paste.

Runs inside the daily workflow but no-ops except on Mondays (周报节奏).

Usage:
    python3 scripts/publish_xhs.py [--force] [--dry-run] [--repo owner/name]

Env:
    LLM_API_KEY / DEEPSEEK_API_KEY — required
    TAVILY_API_KEY                 — optional, enables web-context search
    FEISHU_WEBHOOK / FEISHU_KEYWORD — optional, enables the Feishu card
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "seen.json"
PUBLISH = ROOT / "publish"
COVERED = PUBLISH / ".covered.json"

sys.path.insert(0, str(ROOT / "scripts"))
import discover  # noqa: E402  (llm_chat, daily_rate, gh_readme_excerpt, gh_repo_meta)
import notify  # noqa: E402  (humanize_text, sign_payload)

DRAFT_DAY = 0  # Monday
WEEK_DAYS = 7
README_CHARS = 5000

WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
KEYWORD = os.environ.get("FEISHU_KEYWORD", "")

XHS_PROMPT = """你是小红书博主，写 AI 工具内容，读者是普通程序员和 AI 爱好者。
这周深扒一个项目。素材在下面：README 摘录、我们的追踪数据、网络讨论背景。
规则：只用素材里的事实和数字，不许编造；引用网络背景时在句子里自然带出来源。

【本次风格】{style_label}
人设与语气：{style_persona}
标题公式参考（选其一变形，别照抄）：{style_titles}
正文结构：{style_structure}
示例开头（找感觉，别照抄）：{style_example}
禁忌：{style_avoid}

【项目】{full_name} ★{stars}（{first_seen} 首次发现，日均涨星 +{rate}）
【我们的分析】{category} · {score}/10 · {one_liner}
{compare_line}
【README 摘录】
{readme}

【网络背景】
{web}

写一条小红书笔记：
1. title: ≤20字，符合上面的标题公式，可带 1-2 个 emoji
2. body: 400-600字，口语短句多分段，严格按上面的正文结构和风格禁忌
3. tags: 5-8 个（#开头）
4. cover_idea: 一句话描述首图

只输出 JSON（不要任何其他文字、不要代码围栏）：
{{"title": "...", "body": "...", "tags": ["#AI", "..."], "cover_idea": "..."}}"""

STYLES: dict[str, dict[str, str]] = {}
_styles_path = ROOT / "scripts" / "xhs_styles.json"
if _styles_path.exists():
    STYLES = json.loads(_styles_path.read_text(encoding="utf-8"))
DEFAULT_STYLE = "deep-dive"


def pick_style(name: str | None) -> tuple[str, dict[str, str]]:
    """--style 指定优先；否则按 ISO 周数轮换，免得每周一个味儿。"""
    if not STYLES:
        return DEFAULT_STYLE, {}
    if name:
        if name not in STYLES:
            raise SystemExit(f"未知风格 {name!r}，可选：{', '.join(STYLES)}")
        return name, STYLES[name]
    keys = sorted(STYLES)
    chosen = keys[dt.date.today().isocalendar().week % len(keys)]
    return chosen, STYLES[chosen]


def load_covered() -> list[str]:
    if COVERED.exists():
        return json.loads(COVERED.read_text(encoding="utf-8"))
    return []


def save_covered(covered: list[str]) -> None:
    PUBLISH.mkdir(parents=True, exist_ok=True)
    COVERED.write_text(json.dumps(covered, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_story(seen: dict[str, dict[str, Any]], covered: list[str]) -> tuple[str, dict[str, Any]] | None:
    """本周最高分的非克隆新发现（未写过）；没有就退到涨速最高的关注对象。"""
    week_ago = (dt.date.today() - dt.timedelta(days=WEEK_DAYS)).isoformat()
    candidates = [
        (fn, e) for fn, e in seen.items()
        if e.get("first_seen", "") >= week_ago
        and e.get("score") and not e.get("similar_to")
        and fn not in covered
    ]
    if candidates:
        candidates.sort(key=lambda kv: (kv[1].get("score") or 0, kv[1].get("stars_at_first_seen") or 0), reverse=True)
        return candidates[0]
    rated = [
        (fn, e) for fn, e in seen.items()
        if discover.is_watched(e) and fn not in covered and e.get("score")
    ]
    rated.sort(key=lambda kv: discover.daily_rate(kv[1]) or 0, reverse=True)
    return rated[0] if rated else None


def tavily_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        return []
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=json.dumps({
            "api_key": key, "query": query,
            "max_results": max_results, "search_depth": "basic",
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("results", [])
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] tavily failed: {e}", file=sys.stderr)
        return []


def hn_discussions(name: str, limit: int = 3) -> list[str]:
    """HN 上关于这个项目的讨论（免费，无需 key）。"""
    params = urllib.parse.urlencode({"query": name, "tags": "story", "hitsPerPage": limit})
    try:
        with urllib.request.urlopen(f"https://hn.algolia.com/api/v1/search?{params}", timeout=30) as r:
            hits = json.load(r).get("hits", [])
    except (OSError, json.JSONDecodeError):
        return []
    lines = []
    for h in hits:
        if (h.get("points") or 0) < 3:
            continue
        lines.append(
            f"HN 讨论（{h.get('points')} 分 / {h.get('num_comments') or 0} 条评论）: "
            f"{h.get('title')} — https://news.ycombinator.com/item?id={h.get('objectID')}"
        )
    return lines


def gather_web_context(full_name: str) -> str:
    """HN（免费）+ Tavily（可选）的合成背景文本。"""
    parts = hn_discussions(full_name.split("/")[-1]) or hn_discussions(full_name)
    for hit in tavily_search(full_name):
        parts.append(f"网页: {hit.get('title', '')} — {hit.get('content', '')[:200]} ({hit.get('url', '')})")
    return "\n".join(parts) or "（没有找到值得一提的网络讨论）"


def make_draft(full_name: str, e: dict[str, Any], web: str, readme: str, style: dict[str, str]) -> dict[str, Any] | None:
    rate = discover.daily_rate(e)
    compare_line = ""
    if e.get("compare"):
        compare_line = f"【和同类比】{e['compare']}\n"
    prompt = XHS_PROMPT.format(
        style_label=style.get("label", "深度攻略体"),
        style_persona=style.get("persona", ""),
        style_titles="；".join(style.get("title_formulas", [])),
        style_structure=style.get("structure", ""),
        style_example=style.get("example_opener", ""),
        style_avoid=style.get("avoid", ""),
        full_name=full_name,
        stars=f"{(e.get('stars_history') or [[0, e.get('stars_at_first_seen') or 0]])[-1][1]:,}",
        first_seen=e.get("first_seen", ""),
        rate=f"{rate:,.0f}" if rate else "—",
        category=e.get("category") or "",
        score=e.get("score") or "—",
        one_liner=e.get("one_liner") or "",
        compare_line=compare_line,
        readme=readme or "（README 获取失败）",
        web=web,
    )
    text = discover.llm_chat(prompt, max_tokens=3500)
    if not text:
        return None
    try:
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0]
        # 模型有时在 JSON 后面追加解释——raw_decode 只取第一个完整 JSON 对象
        start = text.index("{")
        draft, _ = json.JSONDecoder().raw_decode(text[start:])
    except (json.JSONDecodeError, ValueError) as err:
        print(f"[WARN] draft parse failed: {err}", file=sys.stderr)
        return None
    body = notify.humanize_text(str(draft.get("body") or ""))
    if body:
        # 模型有时把 hashtags 同时写进 body 末尾和 tags 字段——去掉 body 里的纯标签行
        import re as _re
        body = "\n".join(
            line for line in body.splitlines() if not _re.fullmatch(r"(#\S+\s*)+", line.strip())
        ).strip()
        draft["body"] = body
    return draft


def render_md(draft: dict[str, Any], full_name: str, total: int, style_name: str = "") -> str:
    tags = " ".join(draft.get("tags") or [])
    style_note = f"> 风格：{style_name}（scripts/xhs_styles.json 可改可换）\n" if style_name else ""
    return (
        f"# {draft.get('title', '')}\n\n"
        f"{draft.get('body', '')}\n\n"
        f"{tags}\n\n"
        f"---\n"
        f"> 首图建议：{draft.get('cover_idea', '')}\n"
        f"> 项目：https://github.com/{full_name}\n"
        f"{style_note}"
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
    parser.add_argument("--repo", default=None, help="Override story selection (owner/name).")
    parser.add_argument("--style", default=None, help=f"Style from xhs_styles.json (default: weekly rotation).")
    parser.add_argument("--list-styles", action="store_true", help="List available styles and exit.")
    args = parser.parse_args()

    if args.list_styles:
        for key, s in STYLES.items():
            print(f"{key:12s} {s.get('label', ''):8s} 适合：{s.get('best_for', '')}")
        return 0

    if not args.force and not args.dry_run and dt.date.today().weekday() != DRAFT_DAY:
        print("[INFO] not draft day (Monday) — use --force to override", file=sys.stderr)
        return 0

    style_name, style = pick_style(args.style)
    print(f"[INFO] style: {style_name}（{style.get('label', '—')}）", file=sys.stderr)

    seen: dict[str, dict[str, Any]] = json.loads(STATE.read_text(encoding="utf-8"))
    covered = load_covered()

    if args.repo:
        if args.repo in seen:
            story = (args.repo, seen[args.repo])
        else:
            meta = discover.gh_repo_meta(args.repo)
            if not meta:
                print(f"[ERROR] repo not found: {args.repo}", file=sys.stderr)
                return 1
            story = (args.repo, {
                "first_seen": dt.date.today().isoformat(),
                "stars_at_first_seen": meta.get("stargazers_count") or 0,
                "stars_history": [[dt.date.today().isoformat(), meta.get("stargazers_count") or 0]],
            })
    else:
        story = pick_story(seen, covered)
    if not story:
        print("[WARN] no story candidate", file=sys.stderr)
        return 1
    full_name, entry = story
    print(f"[INFO] story: {full_name}", file=sys.stderr)

    readme = discover.gh_readme_excerpt(full_name, limit=README_CHARS)
    web = gather_web_context(full_name)
    draft = make_draft(full_name, entry, web, readme, style)
    if not draft:
        print("[WARN] no draft generated", file=sys.stderr)
        return 1
    md = render_md(draft, full_name, len(seen), style_name)

    if args.dry_run:
        print(md)
        return 0

    PUBLISH.mkdir(parents=True, exist_ok=True)
    out = PUBLISH / f"{dt.date.today().isoformat()}-xhs.md"
    out.write_text(md, encoding="utf-8")
    if full_name not in covered:
        covered.append(full_name)
        save_covered(covered)
    print(f"[INFO] wrote {out.relative_to(ROOT)}", file=sys.stderr)
    return post_feishu(md)


if __name__ == "__main__":
    raise SystemExit(main())
