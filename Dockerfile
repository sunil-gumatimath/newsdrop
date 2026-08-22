FROM python:3.12.13-slim-bookworm

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/bot_data.db

RUN adduser --disabled-password --gecos '' appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import os, urllib.request, sys; port=os.getenv('HEALTH_PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://localhost:{port}/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "newsdrop"]
