FROM python:3.12.13-slim-bookworm

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

RUN adduser --disabled-password --gecos '' appuser && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "newsdrop"]
