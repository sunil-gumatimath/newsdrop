import os

from dotenv import load_dotenv

# Ensure values from `.env` take precedence over any stale OS-level variables.
load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

NEWS_API_KEY = os.getenv("NEWS_API_KEY")
if not NEWS_API_KEY:
    raise ValueError(
        "NEWS_API_KEY not found in environment variables. "
        "Please set it in your .env file or environment."
    )

DAILY_NEWS_TIME = os.getenv("DAILY_NEWS_TIME", "08:00")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "us")

# Multi-source support. Set ENABLE_RSS=0 to disable RSS augmentation.
ENABLE_RSS = os.getenv("ENABLE_RSS", "1") not in ("0", "false", "False", "no")

# NewsData.io free tier request budget. Set to 0 to disable local request limiting.
DAILY_REQUEST_LIMIT = int(os.getenv("DAILY_REQUEST_LIMIT", "200"))

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

NEWS_API_URL = "https://newsdata.io/api/1/latest"
NEWS_SEARCH_URL = "https://newsdata.io/api/1/latest"
