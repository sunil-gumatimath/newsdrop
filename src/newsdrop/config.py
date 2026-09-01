import logging
import os
import re

from dotenv import load_dotenv

# load_dotenv with override=True: values from `.env` take precedence over
# any pre-existing OS environment variables. This is intentional for local
# development where `.env` is authoritative. In production (Docker/GCE),
# the container's environment is injected via compose/metadata and may not
# have a `.env` file; override=True means a stale `.env` on disk would
# shadow the injected env — ensure `.env` is regenerated on deploy (see
# terraform/templates/startup.sh) or set override=False if you want
# OS-level env to win. Keeping override=True preserves local-dev ergonomics
# but operators should be aware. See docker-compose.yml and docs.
load_dotenv(override=True)

logger = logging.getLogger(__name__)


def _safe_int_env(name: str, default: int) -> int:
    """Parse int env var with fallback and warning on invalid values.

    Returns ``default`` if the variable is unset, empty, or not a valid
    integer. Logs a warning on invalid values so misconfigurations are
    visible without crashing import (important for mypy/tooling).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    raw = raw.strip()
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("Invalid integer for %s=%r, falling back to %s", name, raw, default)
        return default


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if TELEGRAM_BOT_TOKEN:
    TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN.strip()

NEWS_API_KEY = os.getenv("NEWS_API_KEY")
if NEWS_API_KEY:
    NEWS_API_KEY = NEWS_API_KEY.strip()


def validate_config() -> None:
    """Validate required configuration at startup (call explicitly from main()).

    Importing this module must stay side-effect-free so tooling, tests, and
    type-checkers can load config without a real environment. Missing or
    malformed values raise ``ValueError`` here, not at import time.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN not found in environment variables. "
            "Please set it in your .env file or environment."
        )
    if not re.match(r"^\d+:[A-Za-z0-9_-]+$", TELEGRAM_BOT_TOKEN):
        raise ValueError(
            "TELEGRAM_BOT_TOKEN format is invalid. Expected '<digits>:<alphanumeric>'. "
            "Please check your .env file or environment."
        )
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

# NewsData.io free tier request budget. Set to 0 to disable local request limiting.
# Uses _safe_int_env so invalid values (e.g. "abc") fall back to 200 with a warning
# instead of crashing import (which would break mypy/tooling).
# Note: 0 is allowed and means "no local limiting" — callers must check
# `if DAILY_REQUEST_LIMIT == 0` rather than `if DAILY_REQUEST_LIMIT > 0 else 200`.
DAILY_REQUEST_LIMIT = _safe_int_env("DAILY_REQUEST_LIMIT", 200)

# Per-user command cooldowns (seconds). Protects against accidental spam that
# would burn the upstream daily budget. Set to 0 to disable a cooldown entirely
# (e.g. solo self-hosted deployments where the friction isn't useful).
# Defaults 0 (off) for solo self-hosted use; set >0 if you share the bot.
NEWS_COOLDOWN_SECONDS = _safe_int_env("NEWS_COOLDOWN_SECONDS", 0)
SEARCH_COOLDOWN_SECONDS = _safe_int_env("SEARCH_COOLDOWN_SECONDS", 0)

# Breaking-news alert settings.
BREAKING_ALERT_INTERVAL_MINUTES = _safe_int_env("BREAKING_ALERT_INTERVAL_MINUTES", 30)
BREAKING_ALERT_RETENTION_DAYS = _safe_int_env("BREAKING_ALERT_RETENTION_DAYS", 14)
BREAKING_ALERT_MAX_PER_DAY = _safe_int_env("BREAKING_ALERT_MAX_PER_DAY", 5)
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
MAX_BREAKING_KEYWORDS_PER_USER = _safe_int_env("MAX_BREAKING_KEYWORDS_PER_USER", 10)

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

# Digest frequency options for /setfreq (T2).
DAILY_FREQUENCY_CHOICES = ["daily", "twice", "weekdays", "custom"]
DAILY_FREQUENCY_LABELS: dict[str, str] = {
    "daily": "Daily",
    "twice": "Twice daily (8am & 8pm)",
    "weekdays": "Weekdays (Mon–Fri)",
    "custom": "Custom days",
}
WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


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

SUPPORTED_LANGUAGES: dict[str, str] = {
    "🇬🇧 English": "en",
    "🇫🇷 French": "fr",
    "🇩🇪 German": "de",
    "🇪🇸 Spanish": "es",
    "🇮🇹 Italian": "it",
    "🇵🇹 Portuguese": "pt",
    "🇷🇺 Russian": "ru",
    "🇯🇵 Japanese": "ja",
    "🇰🇷 Korean": "ko",
    "🇨🇳 Chinese": "zh",
    "🇸🇦 Arabic": "ar",
    "🇮🇳 Hindi": "hi",
    "🇳🇱 Dutch": "nl",
    "🌐 All languages": "all",
}

# Valid language codes for validation (values of SUPPORTED_LANGUAGES).
SUPPORTED_LANGUAGE_CODES = frozenset(SUPPORTED_LANGUAGES.values())

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
