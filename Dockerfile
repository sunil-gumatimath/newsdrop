FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ ./src/

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/bot_data.db

CMD ["python", "-m", "newsdrop"]
