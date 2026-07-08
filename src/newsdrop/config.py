import os
import re

from dotenv import load_dotenv

# Ensure values from `.env` take precedence over any stale OS-level variables.
load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if TELEGRAM_BOT_TOKEN:
    TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN.strip()
    if not re.match(r"^\d+:[A-Za-z0-9_-]+$", TELEGRAM_BOT_TOKEN):
        raise ValueError(
            "TELEGRAM_BOT_TOKEN format is invalid. Expected '<digits>:<alphanumeric>'. "
            "Please check your .env file or environment."
        )

NEWS_API_KEY = os.getenv("NEWS_API_KEY")
if not NEWS_API_KEY:
    raise ValueError(
        "NEWS_API_KEY not found in environment variables. "
        "Please set it in your .env file or environment."
    )

DAILY_NEWS_TIME = os.getenv("DAILY_NEWS_TIME", "08:00")
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "us")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "UTC")
DATABASE_PATH = os.getenv("DATABASE_PATH", "")

# Multi-source support. Set ENABLE_RSS=0 to disable RSS augmentation.
ENABLE_RSS = os.getenv("ENABLE_RSS", "1") not in ("0", "false", "False", "no")

# Reddit popularity boost. Set ENABLE_REDDIT=1 to rank stories higher when they
# also appear in curated subreddits. This is a trend signal, not a fact-check.
# Default off until explicitly enabled.
ENABLE_REDDIT = os.getenv("ENABLE_REDDIT", "0") not in ("0", "false", "False", "no")

# NewsData.io free tier request budget. Set to 0 to disable local request limiting.
DAILY_REQUEST_LIMIT = int(os.getenv("DAILY_REQUEST_LIMIT", "200"))

# Per-user command cooldowns (seconds). Protects against accidental spam that
# would burn the upstream daily budget. Set to 0 to disable a cooldown entirely
# (e.g. solo self-hosted deployments where the friction isn't useful).
NEWS_COOLDOWN_SECONDS = int(os.getenv("NEWS_COOLDOWN_SECONDS", "30"))
SEARCH_COOLDOWN_SECONDS = int(os.getenv("SEARCH_COOLDOWN_SECONDS", "10"))

# Breaking-news alert settings.
BREAKING_ALERT_INTERVAL_MINUTES = int(os.getenv("BREAKING_ALERT_INTERVAL_MINUTES", "30"))
BREAKING_ALERT_RETENTION_DAYS = int(os.getenv("BREAKING_ALERT_RETENTION_DAYS", "14"))
BREAKING_ALERT_MAX_PER_DAY = int(os.getenv("BREAKING_ALERT_MAX_PER_DAY", "5"))
# When True, followed topics are used as alert keywords for opted-in users.
BREAKING_USE_FOLLOWED_TOPICS = os.getenv("BREAKING_USE_FOLLOWED_TOPICS", "1") not in (
    "0",
    "false",
    "False",
    "no",
)
BREAKING_ALERT_KEYWORDS = [
    keyword.strip()
    for keyword in os.getenv(
        "BREAKING_ALERT_KEYWORDS",
        "breaking,urgent,alert,earthquake,flood,storm,war,attack,explosion,"
        "fire,crash,emergency,evacuation,shooting,terror,cyclone,hurricane",
    ).split(",")
    if keyword.strip()
]
MAX_BREAKING_KEYWORDS_PER_USER = int(os.getenv("MAX_BREAKING_KEYWORDS_PER_USER", "10"))

# Admin chat IDs allowed to use /health (comma-separated). Empty = nobody
# (diagnostics stay on HTTP /health and /metrics only).
ADMIN_CHAT_IDS: set[int] = {
    int(part.strip())
    for part in os.getenv("ADMIN_CHAT_IDS", "").split(",")
    if part.strip().lstrip("-").isdigit()
}

# Common IANA timezones offered in /settimezone (full IANA strings still accepted).
COMMON_TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Asia/Kolkata",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Australia/Sydney",
]

# Daily digest hour choices (local time) offered in /settime.
DAILY_HOUR_CHOICES = [6, 7, 8, 9, 12, 18, 20, 21]


def _default_daily_hour_from_time(value: str) -> int:
    try:
        hour_str, _, _ = value.partition(":")
        hour = int(hour_str)
        if 0 <= hour <= 23:
            return hour
    except ValueError:
        pass
    return 8


DEFAULT_DAILY_HOUR = _default_daily_hour_from_time(DAILY_NEWS_TIME)

COUNTRIES = {
    "🌐 World / International": "world",
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

# Codes that omit the NewsData.io country filter (global feed).
GLOBAL_COUNTRY_CODES = frozenset({"world", "global", "int", "international"})

CATEGORIES = [
    "general",
    "technology",
    "business",
    "sports",
    "entertainment",
    "health",
    "science",
]

# Centralized category keyword taxonomy and tokenizer regex. Moved out of
# news_fetcher.py so all tunable taxonomy lists live in one place. These were
# formerly `_WORD_RE` and `_CATEGORY_KEYWORDS` in news_fetcher.py and are now
# public module-level constants in config.
WORD_RE = re.compile(r"[a-z0-9]+")
CATEGORY_KEYWORDS = {
    "technology": [
        "tech",
        "technology",
        "software",
        "hardware",
        "ai",
        "artificial intelligence",
        "machine learning",
        "cyber",
        "digital",
        "app",
        "application",
        "startup",
        "gadget",
        "device",
        "internet",
        "cloud",
        "data",
        "algorithm",
        "coding",
        "programming",
        "developer",
        "innovation",
        "robot",
        "automation",
        "crypto",
        "blockchain",
        "5g",
        "wireless",
        "computing",
        "chip",
        "semiconductor",
    ],
    "business": [
        "business",
        "economy",
        "market",
        "stock",
        "finance",
        "economic",
        "company",
        "corporate",
        "industry",
        "trade",
        "investment",
        "investor",
        "bank",
        "banking",
        "fund",
        "revenue",
        "profit",
        "merger",
        "acquisition",
        "ipo",
        "startup",
        "entrepreneur",
        "ceo",
        "executive",
        "commercial",
        "retail",
        "sales",
    ],
    "sports": [
        "sport",
        "game",
        "match",
        "tournament",
        "championship",
        "league",
        "team",
        "player",
        "coach",
        "athlete",
        "football",
        "soccer",
        "cricket",
        "basketball",
        "tennis",
        "hockey",
        "baseball",
        "rugby",
        "olympic",
        "race",
        "win",
        "score",
        "goal",
        "medal",
        "cup",
        "final",
        "semi-final",
        "victory",
        "defeat",
    ],
    "entertainment": [
        "movie",
        "film",
        "actor",
        "actress",
        "celebrity",
        "music",
        "song",
        "album",
        "concert",
        "artist",
        "band",
        "hollywood",
        "bollywood",
        "tv",
        "television",
        "show",
        "series",
        "netflix",
        "streaming",
        "theater",
        "cinema",
        "award",
        "oscar",
        "grammy",
        "festival",
        "entertainment",
        "celeb",
        "star",
    ],
    "health": [
        "health",
        "medical",
        "doctor",
        "hospital",
        "disease",
        "virus",
        "covid",
        "vaccine",
        "treatment",
        "medicine",
        "drug",
        "patient",
        "healthcare",
        "wellness",
        "fitness",
        "exercise",
        "diet",
        "nutrition",
        "mental health",
        "pandemic",
        "symptom",
        "cure",
        "research",
        "clinical",
        "pharmaceutical",
        "surgery",
    ],
    "science": [
        "science",
        "scientific",
        "research",
        "study",
        "scientist",
        "discovery",
        "space",
        "nasa",
        "astronomy",
        "physics",
        "chemistry",
        "biology",
        "nature",
        "climate",
        "environment",
        "earth",
        "planet",
        "universe",
        "galaxy",
        "energy",
        "experiment",
        "laboratory",
        "innovation",
        "breakthrough",
        "genetic",
        "dna",
    ],
}

