# newsdrop 📰

Your personalized daily news briefing, delivered straight to Telegram.

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Telegram](https://img.shields.io/badge/Telegram-Bot-2CA5E0.svg)

## Features

- **Daily News Delivery** - Automated news briefing at your preferred time
- **Customizable Preferences** - Choose your country (10 regions) and category (7 topics)
- **Topic Search** - Search for specific news with rate limiting
- **Rich Article Previews** - Article thumbnails with inline "Read more" buttons
- **Relative Timestamps** - See when articles were published ("2h ago", "30m ago")
- **Subscription Management** - Easy subscribe/unsubscribe commands
- **SQLite Backend** - Thread-safe concurrent storage for user data
- **Breaking News Alerts** - Get instant notifications for urgent stories (opt-in)
- **Trending Topics** - Discover what's trending globally across all regions
- **Health Monitoring** - Check bot status and API health anytime

## Setup

### Prerequisites

- Python 3.10+
- A Telegram Bot Token ([@BotFather](https://t.me/botfather))
- A NewsAPI Key ([newsapi.org](https://newsapi.org))

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
NEWS_API_KEY=your_newsapi_key_here
DAILY_NEWS_TIME=08:00
DEFAULT_COUNTRY=in
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
NEWS_API_KEY=your_newsapi_key_here
DAILY_NEWS_TIME=08:00
DEFAULT_COUNTRY=in
```

4. Run the bot:
```bash
python bot.py
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Start the bot |
| `/news` | Get latest news briefing |
| `/search <topic>` | Search news by topic |
| `/subscribe` | Enable daily news delivery |
| `/unsubscribe` | Disable daily news delivery |
| `/setcountry` | Choose your news region |
| `/setcategory` | Choose your news topic |
| `/prefs` | View your preferences |
| `/breaking` | Toggle breaking news alerts |
| `/trending` | View trending topics |
| `/health` | Check bot health status |
| `/help` | Show all commands |

## Supported Regions

🇺🇸 United States · 🇬🇧 United Kingdom · 🇮🇳 India · 🇨🇦 Canada · 🇦🇺 Australia · 🇩🇪 Germany · 🇫🇷 France · 🇯🇵 Japan · 🇧🇷 Brazil · 🇰🇷 South Korea

## Categories

General · Technology · Business · Sports · Entertainment · Health · Science

## Tech Stack

- **python-telegram-bot** - Telegram Bot API
- **httpx** - Async HTTP client
- **SQLite** - Lightweight database
- **python-dotenv** - Environment configuration

## License

MIT License - see [LICENSE](LICENSE) for details.
