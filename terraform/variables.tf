variable "project_id" {
  type        = string
  description = "The GCP Project ID where the bot will be deployed."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "The GCP region to deploy the instance (us-central1, us-west1, or us-east1 are eligible for Free Tier)."
}

variable "zone" {
  type        = string
  default     = "us-central1-a"
  description = "The GCP zone within the selected region."
}

variable "instance_name" {
  type        = string
  default     = "newsdrop-bot"
  description = "The name of the GCE virtual machine."
}

variable "instance_type" {
  type        = string
  default     = "e2-micro"
  description = "The VM instance type. 'e2-micro' is eligible for the GCP Free Tier."
}

variable "telegram_bot_token" {
  type        = string
  sensitive   = true
  description = "The Telegram Bot Token obtained from @BotFather."
}

variable "news_api_key" {
  type        = string
  sensitive   = true
  description = "The NewsData.io API Key."
}

variable "daily_news_time" {
  type        = string
  default     = "08:00"
  description = "The time (HH:MM) to deliver daily briefings."
}

variable "default_country" {
  type        = string
  default     = "in"
  description = "The default country code (e.g. 'us', 'in', 'gb') for news fetches."
}

variable "git_repo" {
  type        = string
  default     = "https://github.com/sunil-gumatimath/newsdrop.git"
  description = "The git repository URL of the newsdrop bot."
}

variable "git_branch" {
  type        = string
  default     = "main"
  description = "The git branch to clone and deploy."
}
