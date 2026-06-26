#!/bin/bash
# Deploy newsdrop bot to your existing GCP VM instance (for Linux/macOS/Git Bash environments)

set -e

# Target VM parameters
VM_NAME="openclaw"
ZONE="us-central1-a"
PROJECT="project-a9da9837-bf64-4084-924"
DEPLOY_DIR="/opt/newsdrop"
ARCHIVE_NAME="newsdrop_deploy.tar.gz"

echo "=== Deploying to VM $VM_NAME ($ZONE) in project $PROJECT ==="

# Check if .env file exists locally
if [ ! -f ".env" ]; then
  echo "Error: Local .env file not found. Please create one (by copying .env.example) before deploying."
  exit 1
fi

# Create a temporary archive of the workspace files
echo "=== Packaging application files ==="
tar --exclude='.git' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='tests' \
    --exclude='terraform' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude="$ARCHIVE_NAME" \
    -czf "$ARCHIVE_NAME" .

# Create the deploy directory on the VM and set permissions
echo "=== Preparing remote directory on VM ==="
gcloud compute ssh "$VM_NAME" \
  --zone="$ZONE" \
  --project="$PROJECT" \
  --command="sudo mkdir -p $DEPLOY_DIR && sudo chown -R \$USER:\$USER $DEPLOY_DIR"

# Upload the archive to the VM
echo "=== Uploading archive to VM ==="
gcloud compute scp "$ARCHIVE_NAME" "$VM_NAME:$DEPLOY_DIR/$ARCHIVE_NAME" \
  --zone="$ZONE" \
  --project="$PROJECT"

# Clean up local archive
rm "$ARCHIVE_NAME"

# Unpack and start the container on the VM
echo "=== Extracting and starting bot on VM ==="
gcloud compute ssh "$VM_NAME" \
  --zone="$ZONE" \
  --project="$PROJECT" \
  --command="
    cd $DEPLOY_DIR
    tar -xzf $ARCHIVE_NAME
    rm $ARCHIVE_NAME
    
    # Ensure Docker is installed and running
    if ! command -v docker &> /dev/null; then
      echo 'Docker not found. Installing Docker...'
      sudo apt-get update -y
      sudo apt-get install -y curl docker.io docker-compose-plugin
      sudo systemctl enable --now docker
      sudo usermod -aG docker \$USER
      echo 'Docker installed successfully.'
    fi
    
    # Setup data folder permissions for SQLite
    mkdir -p data
    sudo chmod 777 data
    
    # Start container
    sudo docker compose down || true
    sudo docker compose up -d --build
    
    echo '=== Deployment completed successfully! ==='
  "
