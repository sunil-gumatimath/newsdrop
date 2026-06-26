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
chmod 777 data

# Write the runtime environment file (.env)
echo "=== Writing .env file ==="
cat <<EOF > .env
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
NEWS_API_KEY=$NEWS_API_KEY
DAILY_NEWS_TIME=$DAILY_NEWS_TIME
DEFAULT_COUNTRY=$DEFAULT_COUNTRY
DATABASE_PATH=/app/data/bot_data.db
ENABLE_RSS=1
DAILY_REQUEST_LIMIT=200
BREAKING_ALERT_INTERVAL_MINUTES=30
BREAKING_ALERT_RETENTION_DAYS=14
EOF

# Ensure compose container is up-to-date and running
echo "=== Launching Bot with Docker Compose ==="
docker compose down || true
docker compose up -d --build

echo "=== newsdrop Startup Script Finished ==="
