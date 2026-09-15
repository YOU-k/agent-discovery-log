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
import re
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

README_CHARS = 5000
PEER_README_CHARS = 800
MAX_PEERS = 4

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
{fit_line}
{compare_line}
【README 摘录】
{readme}

【同类横评素材】（我们追踪的同类项目，横评段落用：每个同类一句话讲定位差异，别展开成第二篇）
{peers}

【网络背景】
{web}

写一条小红书笔记：
1. title: ≤20字，符合上面的标题公式；若风格禁止 emoji 就一个都不要
2. body: 400-700字，多分段，严格按上面的正文结构和风格禁忌；
   "拆解"部分必须引用 README 里的具体证据（命令、文件结构、scripts/tests/CI 等）；
   "工程含量判断"必须明说：这是说明书/提示词包装，还是有真实工程，依据是什么
3. tags: 5-8 个（#开头）
4. cover_idea: 一句话描述首图

只输出以下四个部分，用标记分隔，不要输出任何其他内容（不用 JSON，长文正文放 JSON 里容易坏）：
<TITLE>标题</TITLE>
<BODY>
正文（多段）
</BODY>
<TAGS>#标签1 #标签2 …</TAGS>
<COVER>首图建议一句话</COVER>"""

STYLES: dict[str, dict[str, Any]] = {}
_styles_path = ROOT / "scripts" / "xhs_styles.json"
if _styles_path.exists():
    STYLES = json.loads(_styles_path.read_text(encoding="utf-8"))
DEFAULT_STYLE = "kepu"

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️←-⇿⌚⌛⌨⏏⏩-⏳⏰⏱⏲⏸-⏺☰☹☺♈-♓⚐⚑⚠⚡⚪⚫⛅⛔⛪⛰⛽✅✈✉✊-✍✏✒✔✖✝✡✨❄❇❌❎❓-❕❗➕-➗➡➰➿]+"
)


def strip_emoji(text: str) -> str:
    return EMOJI_RE.sub("", text)


def pick_style(name: str | None) -> tuple[str, dict[str, Any]]:
    """--style 指定优先；否则用 DEFAULT_STYLE（严肃科普体）。"""
    if not STYLES:
        return DEFAULT_STYLE, {}
    if name:
        if name not in STYLES:
            raise SystemExit(f"未知风格 {name!r}，可选：{', '.join(STYLES)}")
        return name, STYLES[name]
    if DEFAULT_STYLE in STYLES:
        return DEFAULT_STYLE, STYLES[DEFAULT_STYLE]
    return next(iter(STYLES.items()))


def load_covered() -> list[str]:
    if COVERED.exists():
        return json.loads(COVERED.read_text(encoding="utf-8"))
    return []


def save_covered(covered: list[str]) -> None:
    PUBLISH.mkdir(parents=True, exist_ok=True)
    COVERED.write_text(json.dumps(covered, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_story(seen: dict[str, dict[str, Any]], covered: list[str]) -> tuple[str, dict[str, Any]] | None:
    """每日一更：从全部未写过的非克隆项目里挑最值得讲的（分数 × 涨速）。"""
    candidates = [
        (fn, e) for fn, e in seen.items()
        if e.get("score") and not e.get("similar_to") and fn not in covered
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda kv: (
            kv[1].get("fit") or kv[1].get("score") or 0,
            discover.daily_rate(kv[1]) or 0,
            kv[1].get("stars_at_first_seen") or 0,
        ),
        reverse=True,
    )
    return candidates[0]


def gather_peers(full_name: str, seen: dict[str, dict[str, Any]]) -> str:
    """同类横评素材：追踪数据里最相似的 K 个项目（含定位差异 + 头部两个的 README 摘录）。

    克隆（similar_to）和未打分的项目也算——横评里"有哪些跟风/汉化版"本身就是内容。
    """
    e0 = seen.get(full_name, {})
    t0 = discover._tokens(full_name.split("/")[-1] + " " + (e0.get("one_liner") or ""))
    ranked = []
    for fn, e in seen.items():
        if fn == full_name:
            continue
        weight = 0.0
        if e.get("similar_to") == full_name:
            weight += 1.0  # 直接克隆，最该提
        t = discover._tokens(fn.split("/")[-1] + " " + (e.get("one_liner") or ""))
        j = discover._jaccard(t0, t) if t0 and t else 0.0
        same_cat = bool(e.get("category")) and e.get("category") == e0.get("category")
        if j >= 0.15 or same_cat or weight:
            weight += j + (0.2 if same_cat else 0.0) + (0.1 if e.get("score") else 0.0)
            ranked.append((weight, fn, e))
    ranked.sort(reverse=True)
    peers = ranked[:MAX_PEERS]
    if not peers:
        return "（追踪数据里没有明显的同类项目）"
    lines = []
    for i, (_w, fn, e) in enumerate(peers):
        hist = e.get("stars_history") or []
        now = hist[-1][1] if hist else e.get("stars_at_first_seen") or 0
        line = f"- {fn} ★{now:,}"
        if e.get("score"):
            line += f" · {e['score']}/10"
        if e.get("one_liner"):
            line += f" · {e['one_liner']}"
        if e.get("similar_to") == full_name:
            line += "（本项目的克隆/衍生）"
        elif e.get("compare"):
            line += f"；与同类差异：{e['compare']}"
        lines.append(line)
        if i < 2:  # 头部两个同类附上 README 摘录，让横评有据可依
            excerpt = discover.gh_readme_excerpt(fn, limit=PEER_README_CHARS)
            if excerpt:
                lines.append(f"  README 摘录: {excerpt[:PEER_README_CHARS]}")
    return "\n".join(lines)


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


def make_draft(
    full_name: str,
    e: dict[str, Any],
    web: str,
    readme: str,
    style: dict[str, Any],
    peers: str,
) -> dict[str, Any] | None:
    rate = discover.daily_rate(e)
    compare_line = ""
    if e.get("compare"):
        compare_line = f"【和同类比】{e['compare']}\n"
    fit_line = ""
    if e.get("fit") or e.get("verdict"):
        fit_line = f"【与科研栈契合度】fit {e.get('fit', '—')}/10 · 建议：{e.get('verdict', '—')} · 重复度：{e.get('overlap', '—')}\n"
    prompt = XHS_PROMPT.format(
        style_label=style.get("label", "严肃科普体"),
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
        fit_line=fit_line,
        readme=readme or "（README 获取失败）",
        peers=peers,
        web=web,
    )
    text = discover.llm_chat(prompt, max_tokens=3500, json_mode=False)
    if not text:
        return None
    # 分区标记解析：长正文走 JSON 容易被引号/换行撑坏（GLM 实测会），纯文本稳
    draft: dict[str, Any] = {}
    for tag, key in (("TITLE", "title"), ("BODY", "body"), ("TAGS", "tags"), ("COVER", "cover_idea")):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        if m:
            draft[key] = m.group(1).strip()
    if not draft.get("title") or not draft.get("body"):
        print(f"[WARN] draft parse failed, raw head: {text[:120]!r}", file=sys.stderr)
        return None
    if isinstance(draft.get("tags"), str):
        draft["tags"] = draft["tags"].split()
    body = notify.humanize_text(str(draft.get("body") or ""))
    if body:
        # 模型有时把 hashtags 同时写进 body 末尾和 tags 字段——去掉 body 里的纯标签行
        body = "\n".join(
            line for line in body.splitlines() if not re.fullmatch(r"(#\S+\s*)+", line.strip())
        ).strip()
        draft["body"] = body
    if style.get("no_emoji"):
        for key in ("title", "body", "cover_idea"):
            if draft.get(key):
                draft[key] = strip_emoji(str(draft[key]))
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
    peers = gather_peers(full_name, seen)
    draft = make_draft(full_name, entry, web, readme, style, peers)
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
