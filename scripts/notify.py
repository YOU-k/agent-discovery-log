#!/usr/bin/env python3
"""Push the daily discovery summary to a Feishu (Lark) group bot.

Builds the message from state/seen.json — repos first_seen today are
"new", plus dynamic tracking (velocity / auto-watch / cooling), plus
full-block highlights from the last 7 days when today is thin. Posts an
interactive card to the bot webhook. No-ops quietly when FEISHU_WEBHOOK
is unset, so the workflow step is safe to run before the secret exists.

Usage:
    python3 scripts/notify.py [--dry-run]

Env:
    FEISHU_WEBHOOK — bot webhook URL (https://open.feishu.cn/open-apis/bot/v2/hook/...)
    FEISHU_SECRET  — optional; if the bot has "签名校验" enabled, its secret
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "seen.json"

sys.path.insert(0, str(ROOT / "scripts"))
import discover  # noqa: E402  (for top_movers)

WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
SECRET = os.environ.get("FEISHU_SECRET", "")
# If the bot uses 自定义关键词 verification, every message must contain it.
KEYWORD = os.environ.get("FEISHU_KEYWORD", "")

MAX_NEW_IN_MSG = 5
MAX_MOVERS_IN_MSG = 5
MAX_RECENT_IN_MSG = 3   # full blocks; bigger than the old one-liner list
RECENT_DAYS = 7

HUMANIZE_RULES = ""
_rules_path = ROOT / "scripts" / "humanize_rules.md"
if _rules_path.exists():
    HUMANIZE_RULES = _rules_path.read_text(encoding="utf-8")


def humanize_text(text: str) -> str:
    """去 AI 味改写（Humanizer-zh 规则）。失败或无 LLM 时返回原文。"""
    if not text or not RULES_OK():
        return text
    prompt = f"""你是中文文字编辑。把下面的日报文案改写得更像真人随手写的，遵循这些规则：
{HUMANIZE_RULES}

硬性要求：保留所有 [名称](链接)、★ 和数字、**加粗** 标记、原有换行分节结构；
不增删事实，不加 emoji；只输出改写后的文案本身。

文案：
{text}"""
    out = discover.llm_chat(prompt, max_tokens=2000, json_mode=False)
    return out or text


def RULES_OK() -> bool:
    return bool(HUMANIZE_RULES) and bool(discover.LLM_API_KEY)


def trend_summary(new_today: list[tuple[str, dict[str, Any]]], movers: list) -> str:
    """2-3 句大白话趋势小结，由 LLM 归纳方向（而不是罗列项目）。'' 如果不可用。"""
    movers_txt = "；".join(f"{fn} 涨了 {delta:,} 星" for fn, _fs, _t, _n, delta in movers[:5]) or "暂无数据"
    new_txt = "；".join(
        f"{fn}（{e.get('category') or '未知类型'}）" for fn, e in new_today[:5]
    ) or "今天无新发现"
    prompt = f"""你在维护一个追踪 GitHub 上 AI agent / Claude Code skill 项目的日报。
今天的新发现：{new_txt}
自首次收录以来涨星最多的项目：{movers_txt}
请用中文大白话写 2-3 句趋势小结（≤80字）：最近这个领域什么方向最火？有什么值得普通开发者关注的信号？
要求：归纳方向和现象，不要罗列项目名，不要用术语。只输出小结正文，不要标题。"""
    out = discover.llm_chat(prompt, max_tokens=400, json_mode=False)
    return humanize_text(out) if out else ""


def repo_block(i: int, fn: str, e: dict[str, Any]) -> str:
    """Full analysis block for one repo (新发现 and 本周精选 share this)."""
    head = f"**{i}. [{fn}](https://github.com/{fn})** ★{(e.get('stars_at_first_seen') or 0):,}"
    if e.get("score"):
        head += f" · {e['score']}/10"
    if e.get("category"):
        head += f" · {e['category']}"
    body = []
    if e.get("one_liner"):
        body.append(f"是什么：{e['one_liner']}")
    if e.get("use_for"):
        body.append(f"能做什么：{e['use_for']}")
    if e.get("usage"):
        body.append(f"大家怎么用：{e['usage']}")
    if e.get("example"):
        body.append(f"举个例子：{e['example']}")
    if e.get("compare"):
        body.append(f"和已有项目比：{e['compare']}")
    return head + ("\n" + "\n".join(body) if body else "")


def build_card(seen: dict[str, dict[str, Any]]) -> dict[str, Any]:
    today = dt.date.today().isoformat()
    week_ago = (dt.date.today() - dt.timedelta(days=RECENT_DAYS)).isoformat()

    new_today = [
        (fn, e) for fn, e in seen.items() if e.get("first_seen") == today
    ]
    new_today.sort(
        key=lambda kv: (kv[1].get("score") or 0, kv[1].get("stars_at_first_seen") or 0),
        reverse=True,
    )

    if new_today:
        blocks = [repo_block(i, fn, e) for i, (fn, e) in enumerate(new_today[:MAX_NEW_IN_MSG], 1)]
        more = f"\n\n…以及另外 {len(new_today) - MAX_NEW_IN_MSG} 个" if len(new_today) > MAX_NEW_IN_MSG else ""
        new_md = f"**新发现 {len(new_today)} 个 repo**\n" + "\n\n".join(blocks) + more
        new_md = humanize_text(new_md)
    else:
        new_md = "**今天没有新 repo 通过过滤**"

    # 动态追踪：涨速榜（日均）、降温榜、自动关注
    rated = []   # (fn, entry, daily_rate, stars_then, stars_now)
    cooling = []
    watched = []
    for fn, e in seen.items():
        if e.get("first_seen") == today:
            continue
        rate = discover.daily_rate(e)
        if rate is None:
            continue
        hist = e.get("stars_history") or []
        rated.append((fn, e, rate, hist[0][1], hist[-1][1]))
        if discover.is_cooling(e):
            cooling.append((fn, e, rate))
        if discover.is_watched(e):
            watched.append((fn, e, rate))
    rated.sort(key=lambda r: r[2], reverse=True)
    watched.sort(key=lambda r: r[2], reverse=True)

    trend_parts = []
    if rated:
        lines = [
            f"[{fn}](https://github.com/{fn}) 日均 +{rate:,.0f}（{then:,} → {now:,}）"
            for fn, _e, rate, then, now in rated[:MAX_MOVERS_IN_MSG]
        ]
        trend_parts.append("**涨速榜（自首收日均）**\n" + "\n".join(lines))
    if cooling:
        lines = [
            f"[{fn}](https://github.com/{fn}) 近一周基本持平（累计日均 +{rate:,.0f}）"
            for fn, _e, rate in cooling[:3]
        ]
        trend_parts.append("**降温（旧 trending 现状）**\n" + "\n".join(lines))
    if watched:
        lines = []
        for fn, e, rate in watched[:8]:
            line = f"🔥 [{fn}](https://github.com/{fn}) 日均 +{rate:,.0f}"
            if e.get("score"):
                line += f" · {e['score']}/10"
            lines.append(line)
        trend_parts.append("**自动关注（高分稳涨或涨速爆表）**\n" + "\n".join(lines))
    movers_md = "\n\n".join(trend_parts)
    # trend_summary 仍按累计口径取 top（保持原有上下文格式）
    movers = [
        (fn, e.get("first_seen", ""), then, now, now - then)
        for fn, e, _rate, then, now in rated[:MAX_MOVERS_IN_MSG]
    ]

    # 近 N 天精选：今天的新发现太少时用完整块展开（分析早已存在 state 里，
    # 渲染零成本），免得整张卡片只有一行带过的一条。
    recent_md = ""
    if len(new_today) < 3:
        recent = [
            (fn, e) for fn, e in seen.items()
            if week_ago <= e.get("first_seen", "") < today
        ]
        recent.sort(
            key=lambda kv: (kv[1].get("score") or 0, kv[1].get("first_seen", "")),
            reverse=True,
        )
        if recent:
            blocks = [repo_block(i, fn, e) for i, (fn, e) in enumerate(recent[:MAX_RECENT_IN_MSG], 1)]
            recent_md = f"**近 {RECENT_DAYS} 天精选**\n" + "\n\n".join(blocks)
            recent_md = humanize_text(recent_md)

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": new_md}},
    ]
    for section in (movers_md, recent_md):
        if section:
            elements += [
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content": section}},
            ]

    summary = trend_summary(new_today, movers)
    if summary:
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**趋势小结**\n{summary}"}},
        ]

    title = f"Agent Discovery · {today}"
    if KEYWORD:
        title = f"{KEYWORD} · {title}"
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": f"共追踪 {len(seen)} 个 repo · agent-discovery-log" + (f" · {KEYWORD}" if KEYWORD else "")}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def sign_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add Feishu signature fields (only when the bot requires 签名校验)."""
    timestamp = str(int(dt.datetime.now().timestamp()))
    string_to_sign = f"{timestamp}\n{SECRET}"
    digest = hmac.new(string_to_sign.encode(), b"", hashlib.sha256).digest()
    return {**payload, "timestamp": timestamp, "sign": base64.b64encode(digest).decode()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print the payload instead of sending.")
    args = parser.parse_args()

    seen: dict[str, dict[str, Any]] = json.loads(STATE.read_text(encoding="utf-8"))
    payload = build_card(seen)
    if SECRET:
        payload = sign_payload(payload)

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not WEBHOOK:
        print("[INFO] FEISHU_WEBHOOK not set — skipping notification", file=sys.stderr)
        return 0

    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.load(r)
    if resp.get("code") not in (0, None):
        print(f"[WARN] Feishu webhook returned: {resp}", file=sys.stderr)
        return 1
    print("[INFO] Feishu notification sent", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
