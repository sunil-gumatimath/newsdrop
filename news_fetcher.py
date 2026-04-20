import html
import httpx
from datetime import datetime, timedelta
from config import NEWS_API_KEY, NEWS_API_URL, NEWS_SEARCH_URL


def format_relative_time(published_at: str) -> str:
    if not published_at:
        return ""
    try:
        pub_time = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        now = datetime.now(pub_time.tzinfo)
        delta = now - pub_time
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return "just now"
        elif total_seconds < 3600:
            mins = total_seconds // 60
            return f"{mins}m ago"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours}h ago"
        elif total_seconds < 172800:
            return "yesterday"
        else:
            days = total_seconds // 86400
            return f"{days}d ago"
    except Exception:
        return ""


# Simple in-memory cache: {cache_key: (timestamp, data)}
_cache: dict[str, tuple[datetime, dict]] = {}
CACHE_TTL_SECONDS = 300  # 5 minutes


def _get_cache_key(params: dict) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "apiKey")


def _get_from_cache(key: str) -> dict | None:
    if key in _cache:
        ts, data = _cache[key]
        if (datetime.now() - ts).total_seconds() < CACHE_TTL_SECONDS:
            return data
        del _cache[key]
    return None


def _set_cache(key: str, data: dict) -> None:
    _cache[key] = (datetime.now(), data)


def _classify_api_error(status_code: int, body: dict) -> str:
    """Return a user-friendly error message based on HTTP status and API response."""
    code = body.get("code", "")
    msg = body.get("message", "")

    if status_code == 401 or code == "apiKeyInvalid":
        return "⚠️ The news API key is invalid. Please check your configuration."
    if status_code == 429 or code == "rateLimited":
        return (
            "⏳ The news API rate limit has been reached. "
            "Please try again in a few minutes."
        )
    if status_code == 426 or code == "apiKeyExhausted":
        return "⚠️ The news API quota has been exhausted. Contact the bot admin."
    if status_code == 500:
        return "🔧 The news service is experiencing issues. Please try again later."
    if msg:
        return f"⚠️ News API error: {msg}"
    return "⚠️ An unexpected error occurred while fetching news. Please try again later."


async def fetch_top_headlines(country: str = "us", category: str = "general") -> dict:
    """Fetch top headlines from NewsAPI with caching."""
    params = {
        "apiKey": NEWS_API_KEY,
        "country": country,
        "category": category,
        "pageSize": 10,
    }

    cache_key = _get_cache_key(params)
    cached = _get_from_cache(cache_key)
    if cached:
        return cached

    async with httpx.AsyncClient() as client:
        response = await client.get(NEWS_API_URL, params=params)
        try:
            data = response.json()
        except Exception:
            raise RuntimeError(
                f"News API returned invalid response (HTTP {response.status_code})"
            )

        if response.status_code != 200 or data.get("status") == "error":
            raise APIClientError(
                _classify_api_error(response.status_code, data),
                status_code=response.status_code,
                api_code=data.get("code"),
            )

        _set_cache(cache_key, data)
        return data


async def search_news(query: str, country: str = "us") -> dict:
    """Search for news articles by keyword with caching."""
    params = {
        "apiKey": NEWS_API_KEY,
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 10,
        "from": (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
    }

    cache_key = _get_cache_key(params)
    cached = _get_from_cache(cache_key)
    if cached:
        return cached

    async with httpx.AsyncClient() as client:
        response = await client.get(NEWS_SEARCH_URL, params=params)
        try:
            data = response.json()
        except Exception:
            raise RuntimeError(
                f"News API returned invalid response (HTTP {response.status_code})"
            )

        if response.status_code != 200 or data.get("status") == "error":
            raise APIClientError(
                _classify_api_error(response.status_code, data),
                status_code=response.status_code,
                api_code=data.get("code"),
            )

        _set_cache(cache_key, data)
        return data


class APIClientError(Exception):
    """Custom exception for NewsAPI errors with structured info."""

    def __init__(self, message: str, status_code: int = None, api_code: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code


def generate_summary(article: dict) -> str:
    """Generate a 2-3 sentence summary of an article."""
    title = article.get("title", "No title")
    description = article.get("description", "")
    content = article.get("content", "")

    summary = description or content

    if not summary:
        return title

    # Truncate to ~2-3 sentences
    sentences = summary.replace("..", ".").split(". ")
    short = ". ".join(sentences[:3])

    if len(short) > 200:
        short = short[:197] + "..."

    return short


def format_briefing(data: dict, country: str = "us", category: str = "general") -> str:
    """Format news articles into a readable Telegram message."""
    articles = data.get("articles", [])

    if not articles:
        return (
            f"No {category} news articles found for {country.upper()}. Try again later."
        )

    published_at = articles[0].get("publishedAt", "") if articles else ""
    date_str = (
        published_at[:10] if published_at else datetime.now().strftime("%Y-%m-%d")
    )

    cat_label = category.capitalize() if category != "general" else "Top"
    message = f"📰 <b>Daily News Briefing — {date_str}</b>\n🌍 {cat_label} Headlines ({country.upper()})\n\n"

    for i, article in enumerate(articles[:10], 1):
        source = article.get("source", {}).get("name", "Unknown")
        title = article.get("title", "No title")
        url = article.get("url", "")
        description = article.get("description", "")
        pub_time = article.get("publishedAt", "")

        time_label = f" · {format_relative_time(pub_time)}" if pub_time else ""
        message += f'<b>{i}. <a href="{url}">{title}</a></b>{time_label}\n'
        if description:
            desc = description[:150] + "..." if len(description) > 150 else description
            message += f"<i>{desc}</i>\n"
        message += f"📍 {source}\n\n"

    message += "Stay informed! 🌍"
    return message


def format_search_results(data: dict, query: str) -> str:
    """Format search results into a readable Telegram message."""
    articles = data.get("articles", [])

    if not articles:
        return (
            f'No results found for "{html.escape(query)}". Try a different search term.'
        )

    message = f'🔍 <b>Search Results: "{html.escape(query)}"</b>\n\n'

    for i, article in enumerate(articles[:10], 1):
        source = article.get("source", {}).get("name", "Unknown")
        title = article.get("title", "No title")
        url = article.get("url", "")
        summary = generate_summary(article)

        message += f'<b>{i}. <a href="{url}">{title}</a></b>\n'
        message += f"<i>{summary}</i>\n"
        message += f"📍 {source}\n\n"

    message += f"Found {data.get('totalResults', 0)} results. Showing top 10."
    return message


def get_article_image(article: dict) -> str | None:
    """Extract image URL from article if available."""
    url = article.get("urlToImage", "")
    if url and not url.endswith("null"):
        return url
    return None


async def fetch_breaking_news(countries: list[str], keywords: list[str]) -> list[dict]:
    """Fetch breaking news from multiple countries based on keywords."""
    breaking_articles = []
    
    for country in countries:
        params = {
            "apiKey": NEWS_API_KEY,
            "country": country,
            "pageSize": 20,
        }
        
        cache_key = _get_cache_key(params)
        cached = _get_from_cache(cache_key)
        if cached:
            articles = cached.get("articles", [])
        else:
            async with httpx.AsyncClient() as client:
                response = await client.get(NEWS_API_URL, params=params)
                try:
                    data = response.json()
                except Exception:
                    continue
                
                if response.status_code == 200 and data.get("status") != "error":
                    _set_cache(cache_key, data)
                    articles = data.get("articles", [])
                else:
                    continue
        
        # Filter articles by keywords
        for article in articles:
            title = article.get("title", "").lower()
            description = article.get("description", "").lower()
            combined = title + " " + description
            
            if any(keyword.lower() in combined for keyword in keywords):
                article["country"] = country
                breaking_articles.append(article)
    
    return breaking_articles


def extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from text, filtering common words."""
    common_words = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
        "been", "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "must", "shall", "can",
        "this", "that", "these", "those", "it", "its", "they", "them", "their",
        "he", "him", "his", "she", "her", "hers", "we", "us", "our", "you",
        "your", "i", "me", "my", "what", "which", "who", "whom", "when",
        "where", "why", "how", "if", "then", "else", "while", "after", "before",
        "between", "into", "through", "during", "until", "against", "without",
        "within", "upon", "about", "above", "below", "over", "under", "again",
        "further", "once", "here", "there", "all", "any", "both", "each",
        "few", "more", "most", "other", "some", "such", "no", "nor", "not",
        "only", "own", "same", "so", "than", "too", "very", "just", "now",
        "says", "said", "new", "news", "report", "reports", "update", "updates"
    }
    
    # Clean and tokenize
    words = text.lower().replace("-", " ").replace("'", " ").split()
    # Filter common words and short words
    keywords = [word for word in words if len(word) > 2 and word not in common_words]
    return keywords


async def fetch_trending_topics(countries: list[str]) -> dict[str, int]:
    """Fetch trending topics across multiple countries."""
    keyword_counts = {}
    
    for country in countries:
        params = {
            "apiKey": NEWS_API_KEY,
            "country": country,
            "pageSize": 20,
        }
        
        cache_key = _get_cache_key(params)
        cached = _get_from_cache(cache_key)
        if cached:
            articles = cached.get("articles", [])
        else:
            async with httpx.AsyncClient() as client:
                response = await client.get(NEWS_API_URL, params=params)
                try:
                    data = response.json()
                except Exception:
                    continue
                
                if response.status_code == 200 and data.get("status") != "error":
                    _set_cache(cache_key, data)
                    articles = data.get("articles", [])
                else:
                    continue
        
        # Extract keywords from titles
        for article in articles:
            title = article.get("title", "")
            keywords = extract_keywords(title)
            for keyword in keywords:
                keyword_counts[keyword] = keyword_counts.get(keyword, 0) + 1
    
    # Sort by count and return top 10
    sorted_topics = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
    return dict(sorted_topics[:10])


async def check_api_health() -> dict[str, str]:
    """Check NewsAPI health by making a test request."""
    params = {
        "apiKey": NEWS_API_KEY,
        "country": "us",
        "pageSize": 1,
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(NEWS_API_URL, params=params, timeout=10.0)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "error":
                    return {
                        "status": "unhealthy",
                        "error": f"API error: {data.get('message', 'Unknown error')}",
                    }
                return {
                    "status": "healthy",
                    "response_time": f"{response.elapsed.total_seconds():.2f}s",
                }
            elif response.status_code == 401:
                return {
                    "status": "unhealthy",
                    "error": "Invalid API key",
                }
            elif response.status_code == 429:
                return {
                    "status": "unhealthy",
                    "error": "Rate limit exceeded",
                }
            else:
                return {
                    "status": "unhealthy",
                    "error": f"HTTP {response.status_code}",
                }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }
