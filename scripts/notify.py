#!/usr/bin/env python3
"""Push the daily discovery summary to a Feishu (Lark) group bot.

Builds the message from state/seen.json — repos first_seen today are
"new", plus the top star movers. Posts an interactive card to the bot
webhook. No-ops quietly when FEISHU_WEBHOOK is unset, so the workflow
step is safe to run before the secret is configured.

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

MAX_NEW_IN_MSG = 5
MAX_MOVERS_IN_MSG = 5


def build_card(seen: dict[str, dict[str, Any]]) -> dict[str, Any]:
    today = dt.date.today().isoformat()

    new_today = [
        (fn, e) for fn, e in seen.items() if e.get("first_seen") == today
    ]
    new_today.sort(
        key=lambda kv: (kv[1].get("score") or 0, kv[1].get("stars_at_first_seen") or 0),
        reverse=True,
    )

    if new_today:
        lines = []
        for fn, e in new_today[:MAX_NEW_IN_MSG]:
            score = f" · score {e['score']}/10" if e.get("score") else ""
            line = f"[{fn}](https://github.com/{fn}) ★{(e.get('stars_at_first_seen') or 0):,}{score}"
            if e.get("one_liner"):
                line += f"\n{e['one_liner']}"
            lines.append(line)
        more = f"\n…以及另外 {len(new_today) - MAX_NEW_IN_MSG} 个" if len(new_today) > MAX_NEW_IN_MSG else ""
        new_md = f"**新发现 {len(new_today)} 个 repo**\n" + "\n".join(lines) + more
    else:
        new_md = "**今天没有新 repo 通过过滤**"

    movers = discover.top_movers(seen, limit=MAX_MOVERS_IN_MSG)
    if movers:
        mover_lines = [
            f"[{fn}](https://github.com/{fn}) {then:,} → {now:,}（+{delta:,}）"
            for fn, _fs, then, now, delta in movers
        ]
        movers_md = "**Trending since first seen**\n" + "\n".join(mover_lines)
    else:
        movers_md = ""

    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": new_md}},
    ]
    if movers_md:
        elements += [
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": movers_md}},
        ]
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": f"共追踪 {len(seen)} 个 repo · agent-discovery-log"}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"Agent Discovery · {today}"},
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
