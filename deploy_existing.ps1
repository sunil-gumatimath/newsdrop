# Deploy newsdrop bot to your existing GCP VM instance (PowerShell version for Windows)

$ErrorActionPreference = "Stop"

# Target VM parameters
$VM_NAME = "openclaw"
$ZONE = "us-central1-a"
$PROJECT = "project-a9da9837-bf64-4084-924"
$DEPLOY_DIR = "/opt/newsdrop"
$ARCHIVE_NAME = "newsdrop_deploy.zip"

Write-Host "=== Deploying to VM $VM_NAME ($ZONE) in project $PROJECT ===" -ForegroundColor Cyan

# Check if .env file exists locally
if (-not (Test-Path ".env")) {
    Write-Error "Error: Local .env file not found. Please create one (by copying .env.example) before deploying."
    exit 1
}

# Create a temporary zip archive of the workspace files
Write-Host "=== Packaging application files ===" -ForegroundColor Cyan
if (Test-Path $ARCHIVE_NAME) { Remove-Item $ARCHIVE_NAME -Force }

# Create a clean temp directory to zip up, avoiding permission/locking issues
$tempDir = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), [System.IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $tempDir | Out-Null

# Copy only deployment-essential files
Copy-Item -Path "src" -Destination "$tempDir/src" -Recurse -Force
Copy-Item -Path "Dockerfile" -Destination $tempDir -Force
Copy-Item -Path "docker-compose.yml" -Destination $tempDir -Force
Copy-Item -Path "pyproject.toml" -Destination $tempDir -Force
Copy-Item -Path ".env" -Destination $tempDir -Force
if (Test-Path ".dockerignore") { Copy-Item -Path ".dockerignore" -Destination $tempDir -Force }

# Create Zip
Compress-Archive -Path "$tempDir\*" -DestinationPath $ARCHIVE_NAME -Force
Remove-Item -Path $tempDir -Recurse -Force

# Create remote directory on VM
Write-Host "=== Preparing remote directory on VM ===" -ForegroundColor Cyan
gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT --command="sudo mkdir -p $DEPLOY_DIR && sudo chown -R `$USER:`$USER $DEPLOY_DIR"

# Upload zip to VM
Write-Host "=== Uploading archive to VM ===" -ForegroundColor Cyan
gcloud compute scp $ARCHIVE_NAME "$($VM_NAME):$DEPLOY_DIR/$ARCHIVE_NAME" --zone=$ZONE --project=$PROJECT

# Clean up local archive
Remove-Item $ARCHIVE_NAME

# Unzip and run on VM
Write-Host "=== Extracting and starting bot on VM ===" -ForegroundColor Cyan
gcloud compute ssh $VM_NAME --zone=$ZONE --project=$PROJECT --command="
    cd $DEPLOY_DIR
    
    # Install unzip if missing
    if ! command -v unzip &> /dev/null; then
      sudo apt-get update -y && sudo apt-get install -y unzip
    fi
    
    unzip -o $ARCHIVE_NAME
    rm $ARCHIVE_NAME
    
    # Ensure Docker is installed and running
    if ! command -v docker &> /dev/null; then
      echo 'Docker not found. Installing Docker...'
      sudo apt-get update -y
      sudo apt-get install -y curl docker.io docker-compose-plugin
      sudo systemctl enable --now docker
      sudo usermod -aG docker `$USER
    fi
    
    # Setup data folder permissions for SQLite
    mkdir -p data
    sudo chmod 777 data
    
    # Start container
    sudo docker compose down || true
    sudo docker compose up -d --build
    
    echo '=== Deployment completed successfully! ==='
"
