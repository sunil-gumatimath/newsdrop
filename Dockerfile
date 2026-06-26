FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code + tests
COPY bot.py .
COPY config.py .
COPY database.py .
COPY news_fetcher.py .
COPY message_utils.py .
COPY rss_feeds.py .
COPY tests/ ./tests/

# Create directory for database
RUN mkdir -p /app/data

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/bot_data.db

# Run the bot
CMD ["python", "bot.py"]
