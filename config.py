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
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "us")
ENABLE_RSS = os.getenv("ENABLE_RSS", "1")

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

NEWS_API_URL = "https://newsdata.io/api/1/latest"
NEWS_SEARCH_URL = "https://newsdata.io/api/1/latest"
