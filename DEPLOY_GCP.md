# Google Cloud Platform (GCP) Deployment Guide

This guide describes how to deploy and host the `newsdrop` Telegram Bot on Google Cloud Platform (GCP) for free using **Google Compute Engine (GCE)** and **Terraform**.

## Table of Contents
1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [GCP Project Setup](#gcp-project-setup)
4. [Deployment Steps (New VM - Terraform)](#deployment-steps-new-vm---terraform)
5. [Deployment Steps (Existing VM)](#deployment-steps-existing-vm)
6. [Managing the Running Bot](#managing-the-running-bot)
7. [Updating the Bot](#updating-the-bot)
8. [Tearing Down](#tearing-down)

---

## Architecture Overview

The bot runs inside a single Docker container on a Google Compute Engine virtual machine (`e2-micro`).
* **Cost:** Completely free! An `e2-micro` VM (with 1 GB RAM, 30 GB standard disk, and 1 GB monthly outbound egress) falls under the [GCP Free Tier](https://cloud.google.com/free) (in regions `us-central1`, `us-east1`, or `us-west1`).
* **Persistence:** The SQLite database is written to a host directory (`/opt/newsdrop/newsdrop-code/data`) mapped to the container volume, persisting data across updates and VM reboots.
* **Security:** The bot communicates via Telegram **Long Polling**, which requires only *outbound* internet access. The GCE VM does not open any inbound ports, keeping your server isolated and secure from scanning or attacks.

---

## Prerequisites

Before starting, make sure you have the following installed locally:
1. **Google Cloud CLI (`gcloud`)** - [Install instructions](https://cloud.google.com/sdk/docs/install)
2. **Terraform (>= 1.3.0)** - [Install instructions](https://developer.hashicorp.com/terraform/downloads)

---

## GCP Project Setup

1. **Create a GCP Project**:
   Go to the [Google Cloud Console](https://console.cloud.google.com/), create a new project, and note down your **Project ID**.
   *(Note: You must attach a billing account to the project to enable API usage, but the resources used here fall under the Free Tier limits).*

2. **Authenticate your CLI**:
   Run the following commands in your local terminal to log in to GCP:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```

3. **Enable Compute Engine API**:
   Enable the API required to launch VMs:
   ```bash
   gcloud services enable compute.googleapis.com --project="YOUR_PROJECT_ID"
   ```

---

## Deployment Steps (New VM - Terraform)

All deployment code is located in the [terraform/](file:///c:/Users/Tedz/OneDrive/Desktop/FUN/newsdrop/terraform) directory.

1. Navigate to the Terraform directory:
   ```bash
   cd terraform
   ```

2. Create a `terraform.tfvars` file to store your credentials and configurations. Use the template below:
   ```hcl
   project_id          = "your-gcp-project-id"
   telegram_bot_token  = "your_telegram_bot_token_from_botfather"
   news_api_key        = "your_newsdata_io_api_key"
   
   # Optional configurations (default values shown below):
   # region            = "us-central1"
   # zone              = "us-central1-a"
   # daily_news_time   = "08:00"
   # default_country   = "in"
   # git_repo          = "https://github.com/sunil-gumatimath/newsdrop.git"
   # git_branch        = "main"
   ```
   > [!IMPORTANT]
   > Keep your `terraform.tfvars` file local and secure. Do **not** commit it to version control (it is excluded by `.gitignore`).

3. Initialize Terraform:
   ```bash
   terraform init
   ```

4. Preview the resources to be created:
   ```bash
   terraform plan
   ```

5. Deploy the resources to Google Cloud:
   ```bash
   terraform apply
   ```
   Confirm with `yes` when prompted. 

Terraform will create the VM and attach the startup script. The VM will take **2 to 3 minutes** to boot up, install Docker, clone your repository, and spin up the bot.

---

## Deployment Steps (Existing VM)

If you already have a running GCE VM (for example, `openclaw` in zone `us-central1-a` and project `project-a9da9837-bf64-4084-924`), you can deploy directly to it from your local workspace using the provided scripts.

### 1. Configure your local `.env` file
Make sure you have created and configured the local `.env` file in the root of your workspace:
```bash
cp .env.example .env
# Edit .env and set your TELEGRAM_BOT_TOKEN and NEWS_API_KEY
```
The deployment script will copy this local `.env` file directly to the VM, so you don't have to manually configure credentials on the remote server.

### 2. Run the deployment script
Depending on your local operating system, run one of the following scripts from the root directory of your workspace:

#### Option A: Windows (PowerShell)
Open PowerShell and run:
```powershell
.\deploy_existing.ps1
```

#### Option B: Linux/macOS/Git Bash
Open your terminal and run:
```bash
chmod +x deploy_existing.sh
./deploy_existing.sh
```

### What these scripts do automatically:
1. Bundle your local workspace code (excluding `.git`, virtual environments, cache files, etc.) and your active `.env` configuration file.
2. Ensure the remote deployment folder (`/opt/newsdrop`) exists on your VM.
3. Upload the archive using `gcloud compute scp`.
4. Connect to your VM using `gcloud compute ssh` and extract the files.
5. Install Docker and Docker Compose (if they are missing from the VM).
6. Build and start the bot in the background using `docker compose up -d --build`.

---

## Managing the Running Bot

To manage your instance (replace `openclaw` with your VM name if it changes):

### SSH into the VM
To securely log in to your bot VM:
```bash
gcloud compute ssh openclaw --zone us-central1-a --project project-a9da9837-bf64-4084-924
```

### View Live Logs
To watch the running bot's logs:
```bash
gcloud compute ssh openclaw --zone us-central1-a --project project-a9da9837-bf64-4084-924 --command "sudo docker compose -f /opt/newsdrop/docker-compose.yml logs -f"
```
*(Note: If deployed via the automated script, the path is `/opt/newsdrop/docker-compose.yml`. If deployed via Terraform, the path is `/opt/newsdrop/newsdrop-code/docker-compose.yml`).*

### Backup Database
The SQLite database is stored on the VM host at `/opt/newsdrop/data/bot_data.db` (or `/opt/newsdrop/newsdrop-code/data/bot_data.db` for Terraform).
To copy the database file from your VM to your local computer:
```bash
gcloud compute scp openclaw:/opt/newsdrop/data/bot_data.db ./backup_bot_data.db --zone us-central1-a --project project-a9da9837-bf64-4084-924
```

---

## Updating the Bot

To update the bot with your latest code changes:

### Method 1: Automatic Re-run (Simple)
Simply restart or reset the VM in Google Cloud. The VM's startup script runs automatically on every boot and will pull the latest commits from your git repository (`git_branch` configured in variables) and recreate the container:
```bash
gcloud compute instances reset newsdrop-bot --zone us-central1-a --project YOUR_PROJECT_ID
```

### Method 2: Manual Update (Fast)
SSH into the machine and run standard Docker Compose commands:
```bash
# 1. SSH into the VM
gcloud compute ssh newsdrop-bot --zone us-central1-a --project YOUR_PROJECT_ID

# 2. Navigate to code directory
cd /opt/newsdrop/newsdrop-code

# 3. Pull latest code
sudo git pull

# 4. Rebuild and restart container
sudo docker compose up -d --build
```

---

## Tearing Down

If you want to stop hosting and delete all resources in Google Cloud, run:
```bash
cd terraform
terraform destroy
```
Type `yes` to confirm. This will terminate the VM instance and release any associated ephemeral IPs, ensuring you do not incur unexpected costs.
