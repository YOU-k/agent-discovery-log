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
