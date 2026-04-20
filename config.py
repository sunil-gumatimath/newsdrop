import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")
DAILY_NEWS_TIME = os.getenv("DAILY_NEWS_TIME", "08:00")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "us")

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

CATEGORIES = [
    "general",
    "technology",
    "business",
    "sports",
    "entertainment",
    "health",
    "science",
]

NEWS_API_URL = "https://newsapi.org/v2/top-headlines"
NEWS_SEARCH_URL = "https://newsapi.org/v2/everything"

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
