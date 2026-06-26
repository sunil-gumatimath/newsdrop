terraform {
  required_version = ">= 1.3.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# GCE Instance running the newsdrop bot
resource "google_compute_instance" "newsdrop_bot" {
  name         = var.instance_name
  machine_type = var.instance_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      # Debian 12 is a stable, lightweight choice for running Docker containers
      image = "debian-cloud/debian-12"
      # GCP Free Tier allows up to 30 GB of standard persistent disk
      size  = 30
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"

    # Request an ephemeral public IP (required for outbound internet access to Telegram/News APIs)
    access_config {}
  }

  # Pass configuration variables as VM metadata.
  # The startup script will query this metadata to construct the application `.env` file dynamically.
  metadata = {
    telegram_bot_token = var.telegram_bot_token
    news_api_key       = var.news_api_key
    daily_news_time    = var.daily_news_time
    default_country    = var.default_country
    git_repo           = var.git_repo
    git_branch         = var.git_branch
    startup-script     = file("${path.module}/templates/startup.sh")
  }

  # Service account with minimum scopes required to run the instance
  service_account {
    scopes = [
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring.write",
      "https://www.googleapis.com/auth/devstorage.read_only"
    ]
  }

  # Ensure the instance can be updated/replaced gracefully
  allow_stopping_for_update = true

  tags = ["newsdrop-bot"]
}
