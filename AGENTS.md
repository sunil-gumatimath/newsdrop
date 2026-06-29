# AGENTS.md

## Project: newsdrop

A Telegram bot that delivers personalized news briefings to subscribers. It fetches headlines from NewsData.io and RSS feeds, supports per-user preferences (region, category, topic following), keyword-triggered breaking-news alerts, and an inline-button UI for onboarding and configuration.

## Architecture

```
src/newsdrop/
├── bot/
│   ├── main.py          # Application entry point, PTB Application builder
│   ├── commands.py      # Slash-command handlers (/start, /news, /search, /follow, …)
│   ├── callbacks.py     # Inline-keyboard button handler (country/category/breaking/search/follow)
│   ├── jobs.py          # Scheduled jobs: send_daily_news, send_breaking_news_alerts
│   ├── helpers.py       # Shared formatting, digest building, callback parsing
│   └── health_server.py # Lightweight HTTP health/metrics endpoint
├── config.py            # Env-driven configuration (countries, categories, thresholds)
├── database.py          # SQLite: subscribers, prefs, topic follows, breaking-alert dedupe
├── news_fetcher.py       # NewsData.io client (top headlines, search, trending, breaking)
├── rss_feeds.py         # Multi-source RSS fetcher → normalised article dicts
├── message_utils.py     # Telegram message chunking
├── metrics.py           # Named counters (daily messages, breaking alerts, errors)
└── state.py             # Pluggable rate-limit / metric backend (Redis or in-memory)

tests/
├── conftest.py          # tmp_db fixture (SQLite), _isolate_rate_limit_state autouse
├── unit/                # Fast, fully-mocked unit tests
└── integration/         # Tests that exercise real I/O (network, DB)
```

## Key Design Decisions

- **Batched daily sends**: `send_daily_news` groups subscribers by `(country, category)` and fetches once per combo, then personalises the digest per user (followed-topics highlight). This keeps API usage proportional to unique combos rather than subscriber count.
- **Breaking-alert dedupe**: A `breaking_alerts` table keyed by `(chat_id, normalized_key)` prevents re-sending the same alert. Rows are pruned by `BREAKING_ALERT_RETENTION_DAYS`.
- **RSS normalisation**: `rss_feeds.py` converts feedparser entries into the same NewsAPI-shaped dicts used everywhere else, so RSS and API results merge seamlessly.
- **Rate limiting**: Per-user cooldowns live in `state.py` (Redis when `REDIS_URL` is set, in-memory otherwise) and protect the upstream daily request budget.

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
- `REDIS_URL` — optional, enables Redis-backed rate limiting / metrics
