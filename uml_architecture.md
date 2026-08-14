# newsdrop — Codebase Review (UML)

## Project Overview
**newsdrop** is a Python Telegram news bot that aggregates headlines from **NewsData.io** and **RSS feeds** — then clusters, ranks, and delivers personalized digests via Telegram. SQLite (WAL) for persistence, optional Redis for multi-worker state.

## UML Class & Component Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        newsdrop.bot (Telegram Bot Layer)            │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  main.py  — Application builder, handler reg, job scheduling │  │
│  │  python-telegram-bot v22.x · long polling · graceful shutdown│  │
│  └────┬────────────┬──────────────┬─────────────────────────────┘  │
│       │            │              │                                │
│  ┌────┴────┐  ┌────┴────┐  ┌─────┴──────┐                         │
│  │commands │  │callbacks│  │   jobs.py   │                         │
│  │  .py    │  │  .py    │  │ Scheduled   │                         │
│  │18 slash │  │14 cb    │  │ daily digest│                         │
│  │handlers │  │actions  │  │ + breaking  │                         │
│  └────┬────┘  └────┬────┘  └──────┬──────┘                         │
│       │            │              │                                │
│  ┌────┴────────────┴──────────────┴──────┐                         │
│  │          helpers.py                    │                         │
│  │  _build_digest_payload · build_search │                         │
│  │  format_breaking_alert · keyboards    │                         │
│  │  _personalize_articles → whyTags     │                         │
│  └───────────────────────────┬──────────┘                          │
│                              │                                     │
│  ┌───────────────────────────┴───────────┐                         │
│  │       health_server.py                │                         │
│  │  GET /health · /ready · /metrics     │                         │
│  │  Stdlib HTTPServer, port 8080        │                         │
│  └───────────────────────────────────────┘                         │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
   ┌─────────────────────────────┼─────────────────────────────┐
   │             Core (newsdrop.*)                              │
   │  ┌──────────┐  ┌──────────┐  ┌────────────┐  ┌────────┐  │
   │  │ config   │  │ database │  │   state    │  │metrics │  │
   │  │ .py      │  │  .py     │  │    .py     │  │  .py   │  │
   │  │Env vars  │  │ SQLite   │  │StateBackend│  │14 named│  │
   │  │27 props  │  │ WAL mode │  │ABC ↦ Mem   │  │counters│  │
   │  │7 catag.  │  │4 tables  │  │   or Redis │  │+ window│  │
   │  └──────────┘  └──────────┘  └────────────┘  └────────┘  │
   │                                                           │
   │  ┌──────────────────────────────────────────────────┐     │
   │  │              news_fetcher.py                      │     │
   │  │ NewsData.io client · multi-source merge          │     │
   │  │ fetch_top_headlines · search_news                │     │
   │  │ fetch_trending_topics · fetch_breaking_news      │     │
   │  │ Whole-word search · APIClientError · HTTP retry  │     │
   │  └───────────┬──────────────┬──────────────┬────────┘     │
   │              │              │              │              │
   │  ┌───────────┴──┐  ┌───────────────┐  ┌───┴──────────┐  │
   │  │ story_ranker │  │ news_fetcher  │  │rss_feeds.py  │  │
   │  │  SOURCE_TRUST│  │  API client   │  │ category     │  │
   │  │  clustering  │  │  merge +      │  │ feeds        │  │
   │  │  ranking     │  │  dedupe       │  │              │  │
   │  └──────────────┘  └───────────────┘  └──────────────┘  │
   └─────────────────────────────────────────────────────┘
```

## Sequence Diagram — `/news` workflow

```
User    Telegram   commands.py   helpers   database   state   news_fetcher
 │         │           │           │         │         │          │
 │  /news  │           │           │         │         │          │
 ├────────►│           │           │         │         │          │
 │         │ Update    │           │         │         │          │
 │         ├──────────►│           │         │         │          │
 │         │           │──────────►│───────► │ ───────►│         │
 │         │           │ Rate lim, │prefs,   │ cache   │          │
 │         │           │ prefs,    │follows  │ check   │          │
 │         │           │ follows   │         │         │          │
 │         │           │           │         │         │          │
 │         │           │───────────────────────────────►          │
 │         │           │ fetch_top_headlines(country, cat)        │
 │         │           │           │         │         │          │
 │         │           │           │         │◄───────│          │
 │         │           │           │         │ cache miss         │
 │         │           │           │         │◄───────│          │
 │         │           │           │         │budget ok          │
 │         │           │           │         │         │          │
 │         │           │           │         │         ├──►NewsData.io
 │         │           │           │         │         │  HTTPS GET
 │         │           │◄─────────────────────────────────────────│
 │         │           │         ranked + clustered articles       │
 │         │           │──► _build_digest_payload                 │
 │         │           │    _personalize_articles → whyTags       │
 │         │           │◄── DigestResult(digest, keyboard)        │
 │         │           │         │         │         │          │
 │         │◄──────────│         │         │         │          │
 │         │ edit_message_text(HTML + keyboard)  │         │          │
 │◄────────│           │         │         │         │          │
 │ 📰 News │           │         │         │         │          │
```

## SQLite Data Model

```
subscribers              user_preferences            topic_follows
┌────────────────┐      ┌────────────────────────┐  ┌───────────────────────┐
│ chat_id  PK INT │      │ chat_id           PK   │  │ chat_id               │
└────────────────┘      │ country    TEXT 'us'   │  │ topic                 │
                         │ category   TEXT 'gen'  │  │ topic_normalized   PK │
  breaking_alerts         │ timezone   TEXT 'UTC'  │  │ created_at   TEXT ISO │
┌──────────────────────┐ │ daily_hour INT 8       │  └───────────────────────┘
│ chat_id              │ │ quiet_start INT NULL   │
│ article_key       PK │ │ quiet_end   INT NULL   │
│ article_url / title  │ │ breaking_keywords TEXT │
│ sent_at   TEXT ISO   │ │ breaking_use_follows   │
│ idx: sent_at         │ └────────────────────────┘
└──────────────────────┘
```

## Key Design Patterns

| Pattern | Where | Usage |
|---------|-------|-------|
| **Strategy** | `state.py` | `StateBackend` ABC → `_MemoryBackend` / `_RedisBackend` selected by `REDIS_URL` |
| **Singleton** | `state.py` | `get_backend()` — lazy-init singleton for cache/rate-limit/metrics |
| **Facade** | `news_fetcher.py` | `fetch_top_headlines()` orchestrates 3 data sources behind one call |
| **Template Method** | `database.py` | Each public `async` wraps `_*_sync()` via `asyncio.to_thread()` |
| **Batched Grouping** | `jobs.py` | Group subscribers by `(country, category)` — 1 API call per combo |

## File Stats

| File | Lines | Role |
|------|-------|------|
| `config.py` | 325 | All env vars, country/category/keyword constants |
| `database.py` | 938 | SQLite persistence + migration |
| `news_fetcher.py` | 1007 | API client, merging, search |
| `story_ranker.py` | 258 | Clustering + trust ranking |
| `state.py` | 337 | StateBackend ABC + backends |
| `bot/commands.py` | 907 | 18 slash command handlers |
| `bot/callbacks.py` | 496 | 14 callback action types |
| `bot/helpers.py` | 937 | Formatting + keyboards |
| `bot/jobs.py` | 394 | Scheduled job logic |
| `bot/main.py` | 205 | Application builder |
| `bot/health_server.py` | 86 | HTTP health endpoints |
| `metrics.py` | 90 | Named counters |
| `message_utils.py` | 37 | Message chunking |
