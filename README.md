# agent-discovery-log

Daily GitHub scan for new agent / skill / multi-agent framework repos.
Runs autonomously via GitHub Actions; findings sink into `discoveries/`.

## What gets scanned

Three sources (see `scripts/discover.py` for details):

- **GitHub REST 搜索**（每日）— 8 条主查询按 stars 排序 + `created:>滚动60天`
  新生项目通道（捞按 stars 排序永远轮不到的新 repo）+ awesome-list 专属通道。
  合集类噪音（awesome/list）在**本地**按词边界过滤——GitHub 的 `-term` NOT
  语法对部分词会静默返回 0 结果，不能写进查询串
- **Hacker News**（每日）— Algolia API 按标题搜 `claude code`、`llm agent` 等，
  ≥5 points 且链接到 GitHub repo 的收进来（HN 热度替代 STAR_MIN 门槛，
  Show HN 往往比 GitHub trending 早几天）
- **GraphQL 普查**（每周日）— 上周 `created:` 窗口内 `stars:>=50` 的全量扫描，
  补足关键词采样的盲区；也可 `python3 scripts/discover.py --census` 手动触发

Edit `scripts/discover.py` to change.

## Filters

- ≥ 50 stars
- Updated in last 60 days
- Not already in `state/seen.json`

## LLM scoring (optional)

Any OpenAI-compatible API works — DeepSeek by default. Set `LLM_API_KEY`
as a GitHub secret; optionally override `LLM_BASE_URL` / `LLM_MODEL` as
repo variables. Each new repo gets a relevance score (1-10) plus a short
Chinese analysis (类型 / 是什么 / 能做什么 / 大家怎么用), grounded with
a README excerpt (~$0.001/day). `ANTHROPIC_API_KEY` also works as a
fallback scorer.

Repos found before scoring was enabled can be analyzed retroactively:

```bash
python3 scripts/discover.py --backfill-days 14   # re-score last 14 days, state only
```

Without a key, findings are just sorted by stars.

## Dynamic tracking

Every run snapshots current stars for all previously seen repos into
`stars_history` in `state/seen.json`. On top of that history the daily
Feishu card and the viz report three transparent, rule-based signals
(no LLM judgment — thresholds live in `scripts/discover.py`):

- **涨速榜** — top repos by average stars/day since first seen
- **自动关注** 🔥 — `score ≥ 7 & 日均 ≥ 20`, or `日均 ≥ 100` regardless of score
- **降温** — hot overall but nearly flat over the last 7 days (needs ≥ 8
  days of history, so it appears about a week after tracking starts)

## Visualization

`scripts/render_viz.py` renders `docs/index.html` — a self-contained
page (no external assets) with velocity/mover charts, discoveries per
day, and the full tracking table (🔥 marks auto-watched repos). The
daily workflow regenerates and commits it. Serve `/docs` via GitHub
Pages, or just open the file locally.

```bash
python3 scripts/render_viz.py --refresh   # re-fetch stars in-memory first
```

## Biomed watch (weekly branch — 目前暂停)

`scripts/biomed.py` watches PubMed for anti-aging drug research
(senolytics, rapamycin, partial reprogramming, …), diffs against
`state/biomed_seen.json`, gets a plain-language Chinese take per paper
from the LLM, writes `discoveries/biomed/YYYY-MM-DD.md` and posts a
separate Feishu card. The weekly workflow is currently removed (branch
paused); run it manually or restore `.github/workflows/biomed.yml` from
git history to re-enable.

```bash
python3 scripts/biomed.py --dry-run
```

## Feishu notification (optional)

Add a 自定义机器人 to a Feishu group, then set its webhook URL as the
`FEISHU_WEBHOOK` GitHub secret. After each run, `scripts/notify.py`
posts a summary card (new repos + top movers) to the group. Security
settings supported both ways: 签名校验 → set the `FEISHU_SECRET`
secret; 自定义关键词 → set the `FEISHU_KEYWORD` repo variable (every
card carries it in the title). No webhook → the step no-ops quietly.

## Structure

```
scripts/discover.py            # GitHub track: search, LLM analysis, star tracking
scripts/render_viz.py          # docs/index.html generator
scripts/notify.py              # Feishu card (+ Humanizer-zh de-AI pass)
scripts/biomed.py              # PubMed track: weekly anti-aging drug watch
scripts/humanize_rules.md      # de-AI-slop rewrite rules
state/seen.json                # GitHub track dedup + star history
state/biomed_seen.json         # PubMed track dedup memory
discoveries/YYYY-MM-DD.md      # daily GitHub report
discoveries/biomed/YYYY-MM-DD.md # weekly biomed report
docs/index.html                # viz (GitHub Pages)
.github/workflows/discover.yml # cron: 22:00 UTC daily
.github/workflows/biomed.yml   # cron: Mon 22:05 UTC weekly
```

## Run locally

```bash
export GH_TOKEN=$(gh auth token)  # for rate limits
python3 scripts/discover.py --dry-run    # preview, don't write
python3 scripts/discover.py              # write to discoveries/ + update state
```

## Setup for your fork

1. Fork this repo
2. Settings → Secrets → add `ANTHROPIC_API_KEY` (optional, for LLM scoring)
3. Actions tab → enable workflows if disabled
4. Wait for 22:00 UTC — first run creates `discoveries/YYYY-MM-DD.md`
5. Or trigger manually: Actions → Daily Discovery → Run workflow

## License

MIT
