output "instance_name" {
  value       = google_compute_instance.newsdrop_bot.name
  description = "The name of the GCE VM instance running the bot."
}

output "instance_public_ip" {
  value       = google_compute_instance.newsdrop_bot.network_interface[0].access_config[0].nat_ip
  description = "The ephemeral public IP address of the bot VM."
}

output "ssh_command" {
  value       = "gcloud compute ssh ${google_compute_instance.newsdrop_bot.name} --zone ${google_compute_instance.newsdrop_bot.zone} --project ${google_compute_instance.newsdrop_bot.project}"
  description = "The gcloud CLI command to SSH directly into the bot VM."
}

output "docker_logs_command" {
  value       = "gcloud compute ssh ${google_compute_instance.newsdrop_bot.name} --zone ${google_compute_instance.newsdrop_bot.zone} --project ${google_compute_instance.newsdrop_bot.project} --command \"sudo docker compose -f /opt/newsdrop/newsdrop-code/docker-compose.yml logs -f\""
  description = "The gcloud command to view the live logs of the running container."
}
