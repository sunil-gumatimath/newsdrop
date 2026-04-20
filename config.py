import os
from dotenv import load_dotenv

# override=True makes values in .env take precedence over pre-existing OS
# environment variables. Without this, a stale OS-level NEWS_API_KEY (e.g.
# left over from a previous `setx` on Windows) will silently shadow the
# value in .env and cause baffling 401 errors.
load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# NewsData.io API key. Falls back to the provided key if not set in env.
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "pub_4b43fbbe97134722985c60446ed86b62")

DAILY_NEWS_TIME = os.getenv("DAILY_NEWS_TIME", "08:00")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "in")

COUNTRIES = {
    "🇺🇸 United States": "us",
    "🇬🇧 United Kingdom": "gb",
    "🇮🇳 India": "in",
    "🇨🇦 Canada": "ca",
    "🇦🇺 Australia": "au",
    "🇩🇪 Germany": "de",
    "🇫🇷 France": "fr",
    "🇯🇵 Japan": "jp",
    "🇧🇷 Brazil": "br",
    "🇰🇷 South Korea": "kr",
}

# User-facing categories (kept same for UI compatibility)
CATEGORIES = [
    "general",
    "technology",
    "business",
    "sports",
    "entertainment",
    "health",
    "science",
]

# Map user-facing categories to NewsData.io category values.
# NewsData.io uses "top" instead of "general".
NEWSDATA_CATEGORY_MAP = {
    "general": "top",
    "technology": "technology",
    "business": "business",
    "sports": "sports",
    "entertainment": "entertainment",
    "health": "health",
    "science": "science",
}

# NewsData.io endpoints
# /latest  → real-time latest news (past 48h, supports q/country/category/language)
NEWS_API_URL = "https://newsdata.io/api/1/latest"

# Multi-source support: when enabled, RSS feeds (see rss_feeds.py) are
# merged with NewsData.io results and de-duplicated. Set ENABLE_RSS=0 to
# disable and rely solely on NewsData.io.
ENABLE_RSS = os.getenv("ENABLE_RSS", "1") not in ("0", "false", "False", "no")

BREAKING_NEWS_KEYWORDS = [
    "breaking",
    "urgent",
    "alert",
    "emergency",
    "critical",
    "developing",
    "just in",
]

BREAKING_CHECK_INTERVAL_MINUTES = 30
BREAKING_RATE_LIMIT_HOURS = 1
