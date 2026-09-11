#!/usr/bin/env python3
"""Daily GitHub discovery for agent/skill/framework repos.

Sources (all diffed against state/seen.json):
1. GitHub REST search — curated queries, three channels (main / awesome / created-window)
2. Hacker News (Algolia API) — daily; Show HN often precedes GitHub trending by days
3. GraphQL census — Sundays; exhaustive sweep of repos created in the last 7 days

Findings go to discoveries/YYYY-MM-DD.md.

Optional: if an LLM API key is set, each new repo gets a relevance score
plus a plain-language Chinese analysis (类型 / 是什么 / 能做什么 /
大家怎么用 / 举个例子 / 和已有项目比), grounded with a README excerpt.
Any OpenAI-compatible endpoint works (default: DeepSeek); Anthropic is
supported as a fallback. Without a key, findings are ranked by stars only.

Each run also refreshes star counts for all previously seen repos
(stars_history in state/seen.json) and reports the biggest gainers.

Usage:
    python3 scripts/discover.py [--dry-run] [--backfill-days N [--force]] [--census]

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
import re
import subprocess
import sys
import urllib.parse
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
    # awesome-list 专属通道（主通道在本地用 NOISE_RE 降噪，见 is_noise）
    ("awesome agent skills", 4),
    ("awesome llm agents", 4),
    # 新生项目通道：主查询按 stars 排序永远偏向老项目，用 created:> 滚动窗口捞新
    ("claude code skill created:>{created_since}", 8),
    ("claude code subagent created:>{created_since}", 8),
    ("multi-agent orchestration framework created:>{created_since}", 7),
    ("agent orchestration cli created:>{created_since}", 7),
]

# 科研赛道：通用 agent 查询按 stars 排序会把 K-Dense（43k★）、academic-research-skills
# （46k★）这类科研库挤出 top-50，所以单开一条通道，且配额更大（RESEARCH_LIMIT）。
RESEARCH_QUERIES: list[tuple[str, int]] = [
    # 每条都实测过能捞到 ≥50★ 的目标库（注释里是验证时的头名）。
    ("scientific agent skills", 10),   # K-Dense-AI/scientific-agent-skills 43k
    ("academic research skills", 10),  # Imbad0202/academic-research-skills 46k
    ("bioinformatics agent", 9),       # GoekeLab/awesome-genomic-skills
    ("bioinformatics skills", 9),      # ClawBio
    ("scientific skills", 9),          # InternScience/Awesome-Scientific-Skills
    ("medical skills", 8),             # FreedomIntelligence/OpenClaw-Medical-Skills 3k
    ("academic skills", 8),            # codex-claude-academic-skills 3k
    ("science skills", 7),             # science-skills 2k
    ("paper skills", 7),               # academic-paper-skills 1k
    ("biology skills", 6),             # FigureOneLab 275
    ("lab skills", 6),                 # shareAI-lab/lab-skills 314
    ("data analysis skills", 5),
    # 新生项目通道
    ("scientific agent skills created:>{created_since}", 9),
    ("academic research skills created:>{created_since}", 9),
    ("bioinformatics skills created:>{created_since}", 8),
]
RESEARCH_LIMIT = 100  # 科研通道不与通用通道抢 50 条配额

# 降噪：主通道在本地排除 awesome/list 类合集 repo。
# 注意：GitHub 搜索的 `-term` NOT 语法对部分词（如 awesome）会静默返回 0 结果，
# 所以必须在本地过滤，不能写进查询串。
NOISE_RE = re.compile(r"\b(awesome|list)\b", re.IGNORECASE)

# HN 信源：按标题搜索，points 即质量门槛（HN 来的豁免 STAR_MIN）
HN_QUERIES: list[tuple[str, int]] = [
    ("claude code", 10),
    ("claude agent", 9),
    ("llm agent", 8),
    ("multi-agent", 7),
]
HN_MIN_POINTS = 5
HN_MAX_PER_DAY = 10
HN_SINCE_DAYS = 2  # 略大于 1 天，配合 seen 去重提高召回

# 周日普查：上周新建 repo 的全量扫描（GraphQL search，每关键词封顶 100）
CENSUS_DAY = 6  # Sunday
CENSUS_QUERIES: list[tuple[str, int]] = [
    ("claude code skill", 10),
    ("claude code subagent", 10),
    ("multi-agent framework", 9),
    ("agent orchestration", 8),
    ("llm agent framework", 6),
]

# Filters
STAR_MIN = 50
UPDATED_WITHIN_DAYS = 60
CREATED_WITHIN_DAYS = 60  # {created_since} 占位符的滚动窗口

# LLM scoring — any OpenAI-compatible endpoint (DeepSeek by default).
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1"
LLM_MODEL = os.environ.get("LLM_MODEL") or "deepseek-chat"

README_EXCERPT_CHARS = 1500

SCORE_PROMPT = """下面是一些 GitHub 仓库（名称、star 数、描述，部分附 README 摘要）。
请站在「**做算法开发 + 生物信息流程 + 论文写作的科研工作者**」的视角评估。
我的实际技术栈（按代码里 import 频次）：scanpy / anndata（单细胞，最主力）、
seaborn+matplotlib、pytorch+transformers、rdkit、torch_geometric、pertpy、
scvi-tools、statsmodels、squidpy（空间转录组）、pydeseq2、gseapy。
日常工作：算法开发、问题解析、生信流程搭建、思路整理、个人知识库、写 report、
下载文献、下载生物数据、做 PPT。
全程用大白话，避免术语黑话。

对每个仓库输出（全部用中文，score 除外）：
- score: 1-10 的相关性评分（Claude Code skills、agent 编排、prompt 工程模式、多智能体框架 = 高相关）
- fit: 1-10，跟**上面那个科研栈**的契合度。能直接用在单细胞/生信/论文/科研数据上 = 9-10；
  通用开发工具但科研也用得上 = 5-6；纯前端/游戏/运维/交易 = 1-2。score 高但 fit 低是常态，别混为一谈
- overlap: 它跟「已在追踪的项目」里哪个功能重复？重复到什么程度？（≤40字；
  写成「和 X 重复，X 已够用」或「和 X 部分重叠，它多了 Y」或「无重复」）
- verdict: 只能是「装」「观望」「不装」三选一，并在 ≤20 字内给理由
- category: 类型，如 Claude Code skill / subagent / 多智能体框架 / 资源合集 / 工具 / 其他
- what: 它是什么（≤25字，大白话）
- use_for: 能拿它做什么（≤45字）
- usage: 大家实际怎么用它（≤45字；不知道就根据 README 合理推断，不要编造具体用户或数字）
- example: 一个具体使用例子（≤60字：谁来用、输入什么、得到什么结果，例如「在 Claude Code 里输入 /xx，它会……」）
- compare: 和「已在追踪的项目」（见末尾列表）中同类的相比，它的差异或创新点是什么、
  为什么值得用它而不是已有的（≤50字；没有完全可比的，就写它填补了哪类空白）

只输出 JSON（不要任何其他文字、不要代码围栏）：
[
  {{"full_name": "owner/name", "score": 8, "fit": 9, "category": "...", "what": "...", "use_for": "...", "usage": "...", "example": "...", "compare": "...", "overlap": "...", "verdict": "装：..."}},
  ...
]

仓库：
{listing}

已在追踪的项目（仅供 compare 字段对比用）：
{tracked}"""

# A scored analysis for one repo.
Score = dict[str, Any]  # keys: score, category, what, use_for, usage, example, compare

GH_URL_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
GH_BAD_OWNERS = {
    "topics", "collections", "features", "login", "signup", "search",
    "orgs", "sponsors", "settings", "notifications", "marketplace", "explore",
}
GH_BAD_NAMES = {"issues", "pull", "pulls", "blob", "wiki", "releases", "actions", "discussions"}


@dataclass
class Repo:
    full_name: str
    description: str
    stars: int
    url: str
    updated_at: str
    matched_query: str
    matched_weight: int
    source: str = "github"   # github | hn | census
    hn_points: int = 0
    hn_url: str = ""


def gh_search(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Search GH via gh CLI. Returns list of repo dicts."""
    cmd = [
        "gh", "search", "repos",
        "--limit", str(limit),
        "--sort", "stars",
        "--json", "fullName,description,stargazersCount,url,updatedAt",
        query,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"[WARN] gh search failed for '{query}': {result.stderr}", file=sys.stderr)
        return []
    return json.loads(result.stdout)


def gh_search_graphql(query: str, limit: int = 100) -> list[dict[str, Any]]:
    """GraphQL search — used by the weekly census (exhaustive windows)."""
    gql = """query($q: String!, $n: Int!) {
  search(query: $q, type: REPOSITORY, first: $n) {
    repositoryCount
    nodes { ... on Repository { nameWithOwner description stargazerCount url updatedAt } }
  }
}"""
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={gql}", "-F", f"q={query}", "-F", f"n={limit}"],
        capture_output=True, text=True, timeout=60,
    )
    try:
        search = (json.loads(result.stdout).get("data") or {}).get("search") or {}
    except json.JSONDecodeError:
        print(f"[WARN] graphql search failed for {query!r}: {result.stderr[:200]}", file=sys.stderr)
        return []
    count = search.get("repositoryCount") or 0
    if count > limit:
        print(f"[WARN] census window overflow for {query!r}: {count} repos > {limit} cap", file=sys.stderr)
    return search.get("nodes") or []


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


def gh_readme_excerpt(full_name: str, limit: int = README_EXCERPT_CHARS) -> str:
    """Fetch the first chars of a repo's README (raw), '' on failure."""
    result = subprocess.run(
        ["gh", "api", f"repos/{full_name}/readme", "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return ""
    return result.stdout[:limit]


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


def is_noise(full_name: str, description: str) -> bool:
    """awesome/list 类合集噪音（主通道用；词边界匹配，不误伤 specialist 等）。"""
    return bool(NOISE_RE.search(full_name) or NOISE_RE.search(description or ""))


# 同类检测（新颖度过滤）：克隆项目的星数也是真的，星数门槛挡不住，
# 要靠"和已追踪项目的相似度"来识别。名字 token 是主信号。
_TOKEN_STOP = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "your",
    "you", "code", "claude", "skill", "skills", "agent", "agents", "framework",
    "ai", "llm", "multi", "based", "via",
}
SIMILAR_NAME_THRESHOLD = 0.5
SIMILAR_TEXT_THRESHOLD = 0.35


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _TOKEN_STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def find_similar(r: Repo, seen: dict[str, Any]) -> str | None:
    """Return the tracked repo this one looks like a clone of, else None."""
    name_t = _tokens(r.full_name.split("/")[-1])
    text_t = name_t | _tokens(r.description)
    if not name_t:
        return None
    for fn, e in seen.items():
        e_name_t = _tokens(fn.split("/")[-1])
        e_text_t = e_name_t | _tokens(e.get("one_liner") or e.get("matched_query") or "")
        if _jaccard(name_t, e_name_t) >= SIMILAR_NAME_THRESHOLD:
            return fn
        if _jaccard(text_t, e_text_t) >= SIMILAR_TEXT_THRESHOLD:
            return fn
    return None


def hn_search(query: str, since_days: int = HN_SINCE_DAYS) -> list[dict[str, Any]]:
    """Search Hacker News stories via the Algolia API (title only)."""
    since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)).timestamp())
    params = urllib.parse.urlencode({
        "query": query,
        "tags": "story",
        "restrictSearchableAttributes": "title",
        "numericFilters": f"created_at_i>{since},points>={HN_MIN_POINTS}",
        "hitsPerPage": 50,
    })
    try:
        with urllib.request.urlopen(f"https://hn.algolia.com/api/v1/search?{params}", timeout=30) as r:
            return json.load(r).get("hits", [])
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] HN search failed for {query!r}: {e}", file=sys.stderr)
        return []


def hn_candidates(seen: dict[str, Any], existing: dict[str, Repo]) -> list[Repo]:
    """HN stories linking to GitHub repos → candidates (HN points replace STAR_MIN)."""
    repos: list[Repo] = []
    for query, weight in HN_QUERIES:
        print(f"[INFO] HN search: {query!r}", file=sys.stderr)
        for hit in hn_search(query):
            m = GH_URL_RE.search(hit.get("url") or "")
            if not m:
                continue
            owner, name = m.group(1), m.group(2).removesuffix(".git")
            if owner.lower() in GH_BAD_OWNERS or name.lower() in GH_BAD_NAMES:
                continue
            fn = f"{owner}/{name}"
            if fn in seen or fn in existing or any(r.full_name == fn for r in repos):
                continue
            meta = gh_repo_meta(fn)
            if not meta or not meta.get("description"):
                continue
            repos.append(Repo(
                full_name=fn,
                description=meta["description"],
                stars=meta.get("stargazers_count") or 0,
                url=meta.get("html_url") or f"https://github.com/{fn}",
                updated_at=meta.get("updated_at") or "",
                matched_query=f"hn: {(hit.get('title') or '')[:60]}",
                matched_weight=weight,
                source="hn",
                hn_points=hit.get("points") or 0,
                hn_url=f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            ))
            if len(repos) >= HN_MAX_PER_DAY:
                return repos
    return repos


def census_candidates(seen: dict[str, Any], existing: dict[str, Repo]) -> list[Repo]:
    """Weekly exhaustive sweep: repos created in the last 7 days per keyword."""
    today = dt.date.today()
    window = f"{(today - dt.timedelta(days=7)).isoformat()}..{today.isoformat()}"
    repos: list[Repo] = []
    for query, weight in CENSUS_QUERIES:
        q = f"{query} created:{window} stars:>={STAR_MIN}"
        print(f"[INFO] census: {q!r}", file=sys.stderr)
        for n in gh_search_graphql(q):
            fn = n["nameWithOwner"]
            if fn in seen or fn in existing or any(r.full_name == fn for r in repos):
                continue
            if (n.get("stargazerCount") or 0) < STAR_MIN or not n.get("description"):
                continue
            if is_noise(fn, n["description"]):
                continue
            repos.append(Repo(
                full_name=fn,
                description=n["description"],
                stars=n["stargazerCount"],
                url=n["url"],
                updated_at=n["updatedAt"],
                matched_query=f"census: {query}",
                matched_weight=weight,
                source="census",
            ))
    return repos


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
        try:
            fit = int(float(item.get("fit") or 0))
        except (TypeError, ValueError):
            fit = 0
        out[item["full_name"]] = {
            "score": score,
            "fit": fit,
            "category": str(item.get("category") or ""),
            "what": str(item.get("what") or ""),
            "use_for": str(item.get("use_for") or ""),
            "usage": str(item.get("usage") or ""),
            "example": str(item.get("example") or ""),
            "compare": str(item.get("compare") or ""),
            "overlap": str(item.get("overlap") or ""),
            "verdict": str(item.get("verdict") or ""),
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


# 类型归一化：LLM 输出有时蹦英文变体，存储时统一（不在 prompt 里写死枚举，
# 保留模型表达自由，脏数据在入口清洗）
CATEGORY_MAP = {
    "tool": "工具",
    "resource collection": "资源合集",
    "collection": "资源合集",
    "awesome list": "资源合集",
    "claude code skill": "Claude Code skill",
    "skill": "Claude Code skill",
    "multi-agent framework": "多智能体框架",
    "agent framework": "多智能体框架",
    "llm agent framework": "多智能体框架",
    "other": "其他",
}


def normalize_category(cat: str) -> str:
    return CATEGORY_MAP.get((cat or "").strip().lower(), (cat or "").strip())


def apply_score(entry: dict[str, Any], s: Score) -> None:
    """Write an analysis into a seen.json entry."""
    entry["score"] = s.get("score")
    entry["one_liner"] = s.get("what") or entry.get("one_liner")
    entry["category"] = normalize_category(s.get("category") or "")
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
    similars: dict[str, str] | None = None,
) -> str:
    today = dt.date.today().isoformat()
    lines = [
        f"# Agent Discovery · {today}",
        "",
        f"Found **{len(new_repos)} new repos** across {len(QUERIES)} queries + HN" + (" + census." if dt.date.today().weekday() == CENSUS_DAY else "."),
        "",
    ]
    if new_repos:
        if scores:
            new_repos.sort(
                key=lambda r: (
                    (scores.get(r.full_name) or {}).get("fit", 0),
                    (scores.get(r.full_name) or {}).get("score", 0),
                ),
                reverse=True,
            )
            lines += ["Sorted by fit-to-my-stack, then relevance score.", ""]
        else:
            new_repos.sort(key=lambda r: r.stars, reverse=True)
            lines += ["Sorted by stars (no LLM scoring; set LLM_API_KEY to enable).", ""]

    for r in new_repos:
        # 同类跟进的只留一行，不展开（省篇幅，也省得重复讲同一个概念）
        similar_to = (similars or {}).get(r.full_name)
        if similar_to:
            lines += [f"- {r.full_name} · ★{r.stars} — 同类跟进 of [{similar_to}](https://github.com/{similar_to})", ""]
            continue
        s = scores.get(r.full_name) or {}
        badge = f"[score {s['score']}/10] " if s.get("score") else ""
        if s.get("fit"):
            badge += f"[fit {s['fit']}/10] "
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
            ("重复情况", "overlap"),
            ("装不装", "verdict"),
        ):
            if s.get(key):
                lines += [f"- **{label}**: {s[key]}"]
        lines += [
            f"- Updated: {r.updated_at[:10]}",
            f"- Source: `{r.source}` · Query hit: `{r.matched_query}`",
        ]
        if r.source == "hn":
            lines += [f"- HN: [{r.hn_points} points]({r.hn_url})"]
        lines += [
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
    parser.add_argument("--census", action="store_true",
                        help="Force the weekly census today (otherwise Sundays only).")
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
    created_since = (dt.date.today() - dt.timedelta(days=CREATED_WITHIN_DAYS)).isoformat()
    search_plan = [(q, w, 50, False) for q, w in QUERIES]
    # 科研通道 keep_lists=True：awesome-genomic-skills / Awesome-Scientific-Skills
    # 这类精选目录正是这条赛道要找的，不能被 NOISE_RE 当合集噪音滤掉。
    search_plan += [(q, w, RESEARCH_LIMIT, True) for q, w in RESEARCH_QUERIES]
    for query, weight, limit, keep_lists in search_plan:
        query = query.format(created_since=created_since)
        print(f"[INFO] searching: {query!r} (limit={limit})", file=sys.stderr)
        for r in gh_search(query, limit=limit):
            if not filter_repo(r):
                continue
            fn = r["fullName"]
            if not keep_lists and not query.startswith("awesome") and is_noise(fn, r["description"]):
                continue
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

    # HN 信源（每日）：Show HN 往往比 GitHub trending 早几天
    for r in hn_candidates(seen, candidates):
        candidates[r.full_name] = r

    # 周日普查：上周新建 repo 的全量扫描（也可 --census 手动触发）
    if args.census or dt.date.today().weekday() == CENSUS_DAY:
        for r in census_candidates(seen, candidates):
            candidates[r.full_name] = r

    new_repos = sorted(candidates.values(), key=lambda r: r.stars, reverse=True)
    print(f"[INFO] {len(new_repos)} new repos after dedup", file=sys.stderr)

    # Register new repos in state (analysis filled in below if scored).
    # 同类检测在注册时做：seen 随注册增长，同批次的克隆也能被抓到。
    n_similar = 0
    for r in new_repos:
        entry: dict[str, Any] = {
            "first_seen": today_iso,
            "stars_at_first_seen": r.stars,
            "matched_query": r.matched_query,
            "score": None,
            "one_liner": None,
            "stars_history": [[today_iso, r.stars]],
        }
        if r.source != "github":
            entry["source"] = r.source
        if r.hn_points:
            entry["hn_points"] = r.hn_points
        if r.hn_url:
            entry["hn_url"] = r.hn_url
        similar_to = find_similar(r, seen)
        if similar_to:
            entry["similar_to"] = similar_to
            n_similar += 1
        seen[r.full_name] = entry
    if n_similar:
        print(f"[INFO] {n_similar} repos flagged as similar-to-existing (skip scoring)", file=sys.stderr)

    # Score the top N new repos (with the tracked list as compare context)
    to_score = [r for r in new_repos if "similar_to" not in seen[r.full_name]]
    scores: dict[str, Score] = {}
    if to_score:
        print(f"[INFO] scoring {min(len(to_score), args.max_scored)} repos", file=sys.stderr)
        scores = score_with_llm(to_score[: args.max_scored], tracked_summary(seen))
        for fn, s in scores.items():
            if fn in seen:
                apply_score(seen[fn], s)

    # Snapshot current stars for everything we've ever seen
    refresh_stars(seen)
    movers = top_movers(seen)

    md = render_daily(
        new_repos, scores, movers,
        {r.full_name: seen[r.full_name]["similar_to"] for r in new_repos if "similar_to" in seen[r.full_name]},
    )

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
