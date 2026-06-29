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
DATABASE_PATH = os.getenv("DATABASE_PATH", "")

# Multi-source support. Set ENABLE_RSS=0 to disable RSS augmentation.
ENABLE_RSS = os.getenv("ENABLE_RSS", "1") not in ("0", "false", "False", "no")

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
BREAKING_ALERT_KEYWORDS = [
    keyword.strip()
    for keyword in os.getenv(
        "BREAKING_ALERT_KEYWORDS",
        "breaking,urgent,alert,earthquake,flood,storm,war,attack,explosion,"
        "fire,crash,emergency,evacuation,shooting,terror,cyclone,hurricane",
    ).split(",")
    if keyword.strip()
]

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

NEWS_API_URL = "https://newsdata.io/api/1/latest"
NEWS_SEARCH_URL = "https://newsdata.io/api/1/latest"
