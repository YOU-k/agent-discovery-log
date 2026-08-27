#!/usr/bin/env python3
"""Weekly anti-aging / longevity drug research watch (PubMed).

Queries PubMed E-utilities for curated anti-aging drug topics, diffs
against state/biomed_seen.json, asks the LLM for a plain-language Chinese
take on each new paper, writes discoveries/biomed/YYYY-MM-DD.md and posts
a Feishu card. Runs weekly via .github/workflows/biomed.yml.

Usage:
    python3 scripts/biomed.py [--dry-run]

Env:
    LLM_API_KEY / DEEPSEEK_API_KEY — optional, enables LLM analysis
    FEISHU_WEBHOOK / FEISHU_KEYWORD — optional, enables the Feishu card
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "biomed_seen.json"
DISCOVERIES = ROOT / "discoveries" / "biomed"

sys.path.insert(0, str(ROOT / "scripts"))
import discover  # noqa: E402  (llm_chat)
from notify import sign_payload  # noqa: E402

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
# NCBI etiquette: identify the tool; throttled to ~3 req/s below.
TOOL = "agent-discovery-log"
EMAIL = "discovery-bot@users.noreply.github.com"

# Curated anti-aging drug research topics.
QUERIES = [
    "senolytics",
    "cellular senescence drug",
    "rapamycin aging",
    "metformin aging",
    "partial reprogramming longevity",
    "longevity drug discovery",
]

DAYS_BACK = 7
RETMAX_PER_QUERY = 8
MAX_PAPERS = 20        # cap on papers sent to the LLM
MAX_CARD_PAPERS = 5

WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
KEYWORD = os.environ.get("FEISHU_KEYWORD", "")

PAPER_PROMPT = """你是抗衰老（aging/longevity）药物研发领域的分析师。下面是 PubMed 上最近 {days} 天的新论文（标题+摘要）。
请站在「关注抗衰老药物进展的普通读者」视角，用中文大白话评估每篇：
- score: 1-10 相关性（药物/干预/临床试验 = 高；纯机制基础研究 = 中低；与抗衰老无关 = 1-2）
- what: 研究了什么、发现了什么（≤45字，大白话，不要用术语）
- why: 为什么值得关注（≤40字；常规进展就直说"常规进展"）

只输出 JSON（不要任何其他文字、不要代码围栏）：
[
  {{"pmid": "12345678", "score": 8, "what": "...", "why": "..."}},
  ...
]

论文：
{listing}"""


def eutils_get(endpoint: str, **params: Any) -> str:
    params.update({"tool": TOOL, "email": EMAIL})
    url = f"{EUTILS}/{endpoint}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        time.sleep(0.4)  # stay well under 3 req/s
        return r.read().decode("utf-8", "replace")


def search_pmids(query: str) -> list[str]:
    text = eutils_get(
        "esearch.fcgi", db="pubmed", term=query,
        datetype="pdat", reldate=DAYS_BACK, retmax=RETMAX_PER_QUERY,
        retmode="json", sort="relevance",
    )
    return json.loads(text).get("esearchresult", {}).get("idlist", [])


def fetch_papers(pmids: list[str]) -> dict[str, dict[str, str]]:
    """Fetch title/abstract/journal/pubdate for the given PMIDs."""
    if not pmids:
        return {}
    xml = eutils_get("efetch.fcgi", db="pubmed", id=",".join(pmids), retmode="xml")
    papers: dict[str, dict[str, str]] = {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        print(f"[WARN] PubMed XML parse failed: {e}", file=sys.stderr)
        return {}
    for art in root.iter("PubmedArticle"):
        pmid = art.findtext(".//MedlineCitation/PMID") or ""
        if not pmid:
            continue
        title_el = art.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        abstract = " ".join(
            "".join(x.itertext()).strip() for x in art.findall(".//Abstract/AbstractText")
        )
        journal = art.findtext(".//Journal/Title") or ""
        pubdate = (
            art.findtext(".//JournalIssue/PubDate/Year")
            or art.findtext(".//JournalIssue/PubDate/MedlineDate")
            or ""
        )
        papers[pmid] = {
            "title": title, "abstract": abstract[:1200],
            "journal": journal, "pubdate": pubdate,
        }
    return papers


def analyze(papers: dict[str, dict[str, str]]) -> dict[str, dict[str, Any]]:
    """LLM score + plain-language take per paper. {pmid: {score, what, why}}."""
    if not papers or not discover.LLM_API_KEY:
        return {}
    listing = "\n".join(
        f"{i+1}. PMID {pmid} | {p['journal']} | {p['title']}\n   摘要: {p['abstract'] or '（无摘要）'}"
        for i, (pmid, p) in enumerate(papers.items())
    )
    text = discover.llm_chat(PAPER_PROMPT.format(days=DAYS_BACK, listing=listing))
    out: dict[str, dict[str, Any]] = {}
    try:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0]
        for item in json.loads(text):
            if isinstance(item, dict) and "pmid" in item:
                out[str(item["pmid"])] = {
                    "score": int(float(item.get("score") or 0)),
                    "what": str(item.get("what") or ""),
                    "why": str(item.get("why") or ""),
                }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print(f"[WARN] paper analysis parse failed: {e}", file=sys.stderr)
    return out


def render_md(today: str, new: list[tuple[str, dict[str, Any]]]) -> str:
    lines = [
        f"# Biomed Watch · {today}",
        "",
        f"PubMed 抗衰老药物方向最近 {DAYS_BACK} 天新论文 **{len(new)} 篇**（按相关性排序）。",
        "",
    ]
    for pmid, p in new:
        badge = f"[score {p['score']}/10] " if p.get("score") else ""
        lines += [
            f"## {badge}{p['title']}",
            "",
            f"- {p['journal']} · {p['pubdate']} · PMID {pmid} · <https://pubmed.ncbi.nlm.nih.gov/{pmid}/>",
        ]
        if p.get("what"):
            lines += [f"- **研究了什么**: {p['what']}"]
        if p.get("why"):
            lines += [f"- **为什么关注**: {p['why']}"]
        lines += [""]
    return "\n".join(lines)


def build_card(today: str, new: list[tuple[str, dict[str, Any]]], total: int) -> dict[str, Any]:
    if new:
        blocks = []
        for i, (pmid, p) in enumerate(new[:MAX_CARD_PAPERS], 1):
            title = p["title"] if len(p["title"]) <= 70 else p["title"][:67] + "…"
            head = f"**{i}. [{title}](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)**"
            if p.get("score"):
                head += f" · {p['score']}/10"
            body = []
            if p.get("what"):
                body.append(f"研究了什么：{p['what']}")
            if p.get("why"):
                body.append(f"为什么关注：{p['why']}")
            blocks.append(head + ("\n" + "\n".join(body) if body else ""))
        more = f"\n\n…以及另外 {len(new) - MAX_CARD_PAPERS} 篇" if len(new) > MAX_CARD_PAPERS else ""
        md = f"**本周新论文 {len(new)} 篇**\n" + "\n\n".join(blocks) + more
    else:
        md = "**本周没有新论文通过筛选**"

    title = f"Biomed Watch · {today}"
    if KEYWORD:
        title = f"{KEYWORD} · {title}"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "green"},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": md}},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"共追踪 {total} 篇 · biomed watch" + (f" · {KEYWORD}" if KEYWORD else "")}]},
            ],
        },
    }


def post_feishu(payload: dict[str, Any]) -> int:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print instead of writing/sending.")
    args = parser.parse_args()

    today = dt.date.today().isoformat()
    seen: dict[str, dict[str, Any]] = {}
    if STATE.exists():
        seen = json.loads(STATE.read_text(encoding="utf-8"))

    # Collect new PMIDs across queries
    pmids: dict[str, None] = {}
    for q in QUERIES:
        print(f"[INFO] pubmed search: {q!r}", file=sys.stderr)
        for pmid in search_pmids(q):
            if pmid not in seen:
                pmids[pmid] = None
    print(f"[INFO] {len(pmids)} new papers after dedup", file=sys.stderr)

    papers = fetch_papers(list(pmids)[:MAX_PAPERS])
    analysis = analyze(papers)

    new: list[tuple[str, dict[str, Any]]] = []
    for pmid, p in papers.items():
        a = analysis.get(pmid) or {}
        entry = {**p, "first_seen": today, **a}
        entry.pop("abstract", None)
        seen[pmid] = entry
        new.append((pmid, entry))
    new.sort(key=lambda kv: kv[1].get("score") or 0, reverse=True)

    md = render_md(today, new)
    card = build_card(today, new, len(seen))

    if args.dry_run:
        print(md)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    DISCOVERIES.mkdir(parents=True, exist_ok=True)
    (DISCOVERIES / f"{today}.md").write_text(md, encoding="utf-8")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[INFO] wrote {DISCOVERIES.relative_to(ROOT)}/{today}.md", file=sys.stderr)
    return post_feishu(card)


if __name__ == "__main__":
    raise SystemExit(main())
