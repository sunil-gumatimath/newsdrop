#!/bin/bash
# Startup script for newsdrop Telegram Bot VM in Google Cloud.
# This script installs Docker, clones the repository, configures the environment, and runs the bot.

set -e

echo "=== newsdrop Setup Startup Script Started ==="

# Update package lists and install Docker, Git, and curl
apt-get update -y
apt-get install -y curl git docker.io docker-compose-plugin

# Enable and start Docker service
systemctl enable docker
systemctl start docker

# Create deployment directory
DEPLOY_DIR="/opt/newsdrop"
mkdir -p "$DEPLOY_DIR"
cd "$DEPLOY_DIR"

# Helper function to fetch instance metadata attributes
fetch_metadata() {
  curl -s -f -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"
}

echo "=== Fetching configuration from GCP Metadata ==="
TELEGRAM_BOT_TOKEN=$(fetch_metadata "telegram_bot_token")
NEWS_API_KEY=$(fetch_metadata "news_api_key")
DAILY_NEWS_TIME=$(fetch_metadata "daily_news_time")
DEFAULT_COUNTRY=$(fetch_metadata "default_country")
GIT_REPO=$(fetch_metadata "git_repo")
GIT_BRANCH=$(fetch_metadata "git_branch")

# Fail fast on missing git metadata before attempting any git operations
if [ -z "$GIT_BRANCH" ] || [ -z "$GIT_REPO" ]; then
  echo "ERROR: Missing GIT_BRANCH or GIT_REPO from metadata. Aborting." >&2
  exit 1
fi

# Clone repository or update if already cloned
if [ -d "newsdrop-code" ]; then
  echo "Code directory exists. Pulling latest updates..."
  cd newsdrop-code
  git fetch origin
  git reset --hard "origin/$GIT_BRANCH"
else
  echo "Cloning repository: $GIT_REPO on branch $GIT_BRANCH..."
  git clone -b "$GIT_BRANCH" "$GIT_REPO" newsdrop-code
  cd newsdrop-code
fi

# Ensure data persistence directory exists with correct permissions for container access
mkdir -p data
chmod 750 data
if id -u appuser >/dev/null 2>&1; then
  chown appuser:appuser data
fi

# Validate required secrets before writing env file
if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$NEWS_API_KEY" ]; then
  echo "ERROR: Missing required tokens (TELEGRAM_BOT_TOKEN or NEWS_API_KEY) from metadata. Aborting." >&2
  exit 1
fi
# Basic format check for Telegram token (digits:alphanumeric) to catch empty/garbage metadata
if ! echo "$TELEGRAM_BOT_TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$'; then
  echo "WARNING: TELEGRAM_BOT_TOKEN format looks invalid; continuing anyway." >&2
fi

# Write the runtime environment file (.env) securely.
# Use printf with %s to avoid unquoted heredoc expansion / injection issues
# (previously `cat <<EOF > .env` without quoting would allow shell expansion
# and word-splitting of token values). Each value is written via printf '%s'
# which treats the variable as a literal string.
echo "=== Writing .env file ==="
{
  printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TELEGRAM_BOT_TOKEN"
  printf 'NEWS_API_KEY=%s\n' "$NEWS_API_KEY"
  printf 'DAILY_NEWS_TIME=%s\n' "$DAILY_NEWS_TIME"
  printf 'DEFAULT_COUNTRY=%s\n' "$DEFAULT_COUNTRY"
  printf 'DATABASE_PATH=%s\n' "/app/data/bot_data.db"
  printf 'ENABLE_RSS=%s\n' "1"
  printf 'DAILY_REQUEST_LIMIT=%s\n' "200"
  printf 'BREAKING_ALERT_INTERVAL_MINUTES=%s\n' "30"
  printf 'BREAKING_ALERT_RETENTION_DAYS=%s\n' "14"
} > .env
chmod 600 .env

# Ensure compose container is up-to-date and running
echo "=== Launching Bot with Docker Compose ==="
docker compose down || true
docker compose up -d --build
# Reclaim space from dangling build layers/images so the boot disk doesn't fill.
docker image prune -f

echo "=== newsdrop Startup Script Finished ==="
