# AGENTS.md

## Project: newsdrop

A Telegram bot that delivers personalized news briefings to subscribers. It aggregates headlines from **NewsData.io**, curated **RSS** feeds (country + category), and optionally **Reddit** as a popularity signal. Features include per-user preferences (region, category, schedule), topic following, whole-word search, keyword-triggered breaking alerts, story clustering/ranking, guided onboarding, and an inline-button UI.

## Architecture

```
src/newsdrop/
├── bot/
│   ├── main.py          # Application entry point, PTB Application builder, bot command menu
│   ├── commands.py      # Slash-command handlers (/start, /news, /search, /clear, …)
│   ├── callbacks.py     # Inline-keyboard handler (onboarding, prefs, search, follow, clear)
│   ├── jobs.py          # Scheduled jobs: send_daily_news, send_breaking_news_alerts
│   ├── helpers.py       # Digests, search cards, breaking format, keyboards, /clear helper
│   └── health_server.py # Lightweight HTTP health/metrics endpoint
├── config.py            # Env-driven configuration (countries, categories, thresholds)
├── database.py          # SQLite: subscribers, prefs, topic follows, breaking-alert dedupe
├── news_fetcher.py      # NewsData.io client + merge path (top, search, trending, breaking)
├── story_ranker.py      # Source trust weights, near-duplicate clustering, ranking
├── cross_verify.py      # Optional Reddit title/URL match → popularity boost
├── rss_feeds.py         # Country + category RSS catalog, feed health cooldown
├── reddit_feeds.py      # Optional subreddit hot posts (public JSON / RSS)
├── message_utils.py     # Telegram message chunking
├── metrics.py           # Named counters (daily messages, breaking alerts, errors)
└── state.py             # Pluggable cache / rate-limit / API budget (Redis or in-memory)

tests/
├── conftest.py          # tmp_db fixture (SQLite), rate-limit isolation autouse
├── unit/                # Fast, fully-mocked unit tests
└── integration/         # Tests that exercise real I/O (network, DB)
```

## Key Design Decisions

- **Batched daily sends**: `send_daily_news` groups subscribers by `(country, category)`, fetches once per combo, then personalises the digest per user (followed topics first + why-tags). API usage scales with unique combos, not subscriber count.
- **Multi-source merge**: `fetch_top_headlines` runs NewsData.io + RSS (+ Reddit if enabled) in parallel, then `story_ranker.rank_and_cluster` clusters near-duplicates and ranks by trust, corroboration, freshness, and Reddit signal.
- **Category RSS**: Non-general categories use dedicated feeds (tech, business, sports, …) so RSS does not rely only on keyword filters of general headlines.
- **Search relevance**: `_filter_by_query` uses **whole-word** matching and relevance scoring so short queries like `AI` do not match `airport` / `against`.
- **Breaking alerts**: Compact single message with matched keyword reason, open-article button, and daily cap counter. Title hits preferred; body-only needs ≥2 keywords. Deduped via `breaking_alerts` table; pruned by `BREAKING_ALERT_RETENTION_DAYS`.
- **Onboarding**: `/start` is a 3-step inline flow (region → category → subscribe / get news now).
- **Rate limiting**: Per-user cooldowns in `state.py` (Redis when `REDIS_URL` is set). Shared HTTP client + 5‑minute API cache protect the free-tier budget.
- **RSS feed health**: After consecutive failures, a feed is skipped for a cooldown period.

## Running Tests

```bash
pytest tests/unit/                       # fast, no network
pytest tests/integration/                # requires API keys + network
pytest --cov=src/newsdrop --cov-report=term-missing
```

`asyncio_mode = "auto"` is set in `pyproject.toml`, so all `async def test_*` functions run automatically without explicit `@pytest.mark.asyncio`.

## Linting & Formatting

```bash
ruff check .                     # lint
ruff check --fix .              # auto-fix lint issues
ruff format .                    # format
mypy src/newsdrop                # type-check
```

Pre-commit hooks (`.pre-commit-config.yaml`) run `ruff` (lint + format) and `mypy` on commit.

## Environment

Required env vars (see `config.py` for defaults):
- `TELEGRAM_BOT_TOKEN` — BotFather token
- `NEWS_API_KEY` — NewsData.io API key
- `DATABASE_PATH` — optional, override SQLite location
- `REDIS_URL` — optional, enables Redis-backed rate limiting / cache / metrics
- `ENABLE_RSS` — default on; set `0` to disable RSS
- `ENABLE_REDDIT` — default off; set `1` for Reddit popularity boost
- `DAILY_REQUEST_LIMIT` — local NewsData.io budget (default `200`)
- `ADMIN_CHAT_IDS` — comma-separated chat IDs allowed to use `/health`
- Breaking-alert knobs: `BREAKING_ALERT_INTERVAL_MINUTES`, `BREAKING_ALERT_MAX_PER_DAY`, `BREAKING_ALERT_KEYWORDS`, `BREAKING_USE_FOLLOWED_TOPICS`

## Security (open source)

Do not commit `.env` or real tokens. Vulnerability reports: prefer private channel — see [SECURITY.md](./SECURITY.md).
