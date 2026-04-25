# newsdrop 📰

Your personalized daily news briefing, delivered straight to Telegram.

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-2CA5E0.svg)

## Features

- **Daily News Delivery** - Automated news briefing at your preferred time
- **Customizable Preferences** - Choose your country (10 regions) and category (7 topics)
- **Topic Search** - Search for specific news with rate limiting
- **Followed Topics** - Follow, list, and remove custom topics like AI, crypto, or climate
- **Rich Article Previews** - Article thumbnails with inline "Read more" buttons
- **Relative Timestamps** - See when articles were published ("2h ago", "30m ago")
- **Subscription Management** - Easy subscribe/unsubscribe commands
- **SQLite Backend** - Thread-safe concurrent storage for user data
- **Breaking News Alerts** - Opt into scheduled breaking-news checks with duplicate suppression
- **Trending Topics** - Discover what's trending globally across all regions
- **Health Monitoring** - Check bot status and API health anytime
- **Multi-Source Aggregation** - Combines NewsData.io + curated RSS feeds (Times of India, The Hindu, NDTV, BBC, Guardian, NPR, and more) with automatic deduplication and fallback

## Setup

### Prerequisites

- Python 3.10+
- A Telegram Bot Token ([@BotFather](https://t.me/botfather))
- A NewsData.io API Key ([newsdata.io](https://newsdata.io)) — free tier: 200 requests/day, real-time news

### Installation

#### Option 1: Docker (Recommended)

1. Clone the repository:
```bash
git clone https://github.com/yourusername/newsdrop.git
cd newsdrop
```

2. Configure environment variables:
```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
NEWS_API_KEY=your_newsdata_io_key_here
DAILY_NEWS_TIME=08:00
DEFAULT_COUNTRY=in
DATABASE_PATH=/app/data/bot_data.db
ENABLE_RSS=1
DAILY_REQUEST_LIMIT=200
BREAKING_ALERT_INTERVAL_MINUTES=30
BREAKING_ALERT_RETENTION_DAYS=14
BREAKING_ALERT_KEYWORDS=breaking,urgent,alert,earthquake,flood,storm,war,attack,explosion,fire,crash,emergency
```

3. Run with Docker Compose:
```bash
docker-compose up -d
```

To view logs:
```bash
docker-compose logs -f
```

To stop the bot:
```bash
docker-compose down
```

#### Option 2: Local Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/newsdrop.git
cd newsdrop
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Configure environment variables:
```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
NEWS_API_KEY=your_newsdata_io_key_here
DAILY_NEWS_TIME=08:00
DEFAULT_COUNTRY=in
DATABASE_PATH=bot_data.db
ENABLE_RSS=1
DAILY_REQUEST_LIMIT=200
BREAKING_ALERT_INTERVAL_MINUTES=30
BREAKING_ALERT_RETENTION_DAYS=14
BREAKING_ALERT_KEYWORDS=breaking,urgent,alert,earthquake,flood,storm,war,attack,explosion,fire,crash,emergency
```

4. Run the bot:
```bash
python bot.py
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Start the bot |
| `/news` | Get latest news briefing using your saved preferences |
| `/search <topic>` | Search news by topic |
| `/follow <topic>` | Follow a custom topic like AI or crypto |
| `/unfollow <topic>` | Stop following a topic |
| `/follows` | View followed topics |
| `/topics` | Alias for `/follows` |
| `/unfollowall` | Remove all followed topics |
| `/subscribe` | Enable daily news delivery |
| `/unsubscribe` | Disable daily news delivery |
| `/setcountry` | Choose your news region |
| `/setcategory` | Choose your news topic |
| `/prefs` | View your preferences |
| `/breaking` | Toggle scheduled breaking-news alerts |
| `/trending [category]` | View trending topics, optionally filtered by category |
| `/health` | Check bot, API, database, cache, and alert-tracking status |
| `/help` | Show all commands |
| `/commands` | Alias for `/help` |

## Supported Regions

🇺🇸 United States · 🇬🇧 United Kingdom · 🇮🇳 India · 🇨🇦 Canada · 🇦🇺 Australia · 🇩🇪 Germany · 🇫🇷 France · 🇯🇵 Japan · 🇧🇷 Brazil · 🇰🇷 South Korea

## Categories

General · Technology · Business · Sports · Entertainment · Health · Science

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Required | Telegram bot token from BotFather |
| `NEWS_API_KEY` | Required | NewsData.io API key |
| `DAILY_NEWS_TIME` | `08:00` | Daily delivery time in 24-hour `HH:MM` format |
| `DEFAULT_COUNTRY` | `us` | Default country code for new users |
| `DATABASE_PATH` | `bot_data.db` | SQLite database location. Docker uses `/app/data/bot_data.db` for persistence |
| `ENABLE_RSS` | `1` | Set to `0` to disable RSS fallback |
| `DAILY_REQUEST_LIMIT` | `200` | Local NewsData.io daily request guard. Set to `0` to disable local limiting |
| `BREAKING_ALERT_INTERVAL_MINUTES` | `30` | How often the bot checks for breaking-news matches. Set to `0` to disable scheduled alerts |
| `BREAKING_ALERT_RETENTION_DAYS` | `14` | How long sent breaking-alert records are kept for duplicate suppression |
| `BREAKING_ALERT_KEYWORDS` | Built-in urgent-news keywords | Comma-separated keywords used to detect breaking stories |

## Tech Stack

- **python-telegram-bot** - Telegram Bot API
- **httpx** - Async HTTP client
- **feedparser** - RSS feed parsing for multi-source aggregation
- **SQLite** - Lightweight database
- **python-dotenv** - Environment configuration

## Multi-Source News

The bot merges news from two sources for maximum coverage and resilience:

1. **[NewsData.io](https://newsdata.io)** — real-time API with 84k+ sources, category filtering, and keyword search
2. **Curated RSS feeds** — official feeds from top outlets per country (see `rss_feeds.py`)

### How it works
- Both sources are fetched **in parallel** on every `/news` and `/search` request
- Articles are **de-duplicated** by URL and normalized title
- If the API is rate-limited or fails, RSS keeps the bot working — **graceful degradation**
- Disable RSS with `ENABLE_RSS=0` in your `.env` to rely solely on NewsData.io
- Breaking alerts are checked on a schedule and tracked in SQLite so users do not receive the same alert repeatedly

### RSS feeds by country
| Country | Sources |
|---------|---------|
| 🇮🇳 India | Times of India, The Hindu, NDTV, Indian Express, Hindustan Times |
| 🇺🇸 United States | NPR, BBC, NYT World |
| 🇬🇧 United Kingdom | BBC News, The Guardian, Sky News |
| 🇨🇦 Canada | CBC |
| 🇦🇺 Australia | ABC News |
| 🇩🇪 Germany | Deutsche Welle |
| 🇫🇷 France | France 24 |
| 🇯🇵 Japan | Japan Times |
| 🇧🇷 Brazil | G1 Globo |
| 🇰🇷 South Korea | Chosun English |

## License

MIT License - see [LICENSE](LICENSE) for details.
