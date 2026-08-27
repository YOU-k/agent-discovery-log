# agent-discovery-log

Daily GitHub scan for new agent / skill / multi-agent framework repos.
Runs autonomously via GitHub Actions; findings sink into `discoveries/`.

## What gets scanned

Curated queries (weighted by relevance):

| Query | Weight |
|---|---|
| `claude code skill` | 10 |
| `claude code subagent` | 10 |
| `multi-agent orchestration framework` | 9 |
| `multi agent framework claude` | 9 |
| `agent orchestration cli` | 8 |
| `ai coding agent framework` | 7 |
| `llm agent framework` | 6 |
| `prompt engineering agent` | 5 |

Edit `scripts/discover.py` to change.

## Filters

- ≥ 50 stars
- Updated in last 60 days
- Not already in `state/seen.json`

## LLM scoring (optional)

Any OpenAI-compatible API works — DeepSeek by default. Set `LLM_API_KEY`
as a GitHub secret; optionally override `LLM_BASE_URL` / `LLM_MODEL` as
repo variables. Each new repo gets a relevance score (1-10) and one-liner
(~$0.001/day). `ANTHROPIC_API_KEY` also works as a fallback scorer.

Without a key, findings are just sorted by stars.

## Star velocity

Every run snapshots current stars for all previously seen repos into
`stars_history` in `state/seen.json`. The daily report ends with a
"Trending since first seen" table of the biggest gainers, so the log
doubles as a trend tracker.

## Visualization

`scripts/render_viz.py` renders `docs/index.html` — a self-contained
page (no external assets) with top movers, discoveries per day, and the
full tracking table. The daily workflow regenerates and commits it.
Serve `/docs` via GitHub Pages, or just open the file locally.

```bash
python3 scripts/render_viz.py --refresh   # re-fetch stars in-memory first
```

## Feishu notification (optional)

Add a 自定义机器人 to a Feishu group, then set its webhook URL as the
`FEISHU_WEBHOOK` GitHub secret. After each run, `scripts/notify.py`
posts a summary card (new repos + top movers) to the group. If the bot
has 签名校验 enabled, also set `FEISHU_SECRET`. No secret → the step
no-ops quietly.

## Structure

```
scripts/discover.py           # core script (Python 3.11+)
state/seen.json               # persisted dedup memory
discoveries/YYYY-MM-DD.md     # each day's report
.github/workflows/discover.yml # cron: 22:00 UTC daily
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
