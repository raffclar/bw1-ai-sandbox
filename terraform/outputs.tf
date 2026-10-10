output "droplet_ip" {
  description = "The droplet's public IPv4 address."
  value       = digitalocean_droplet.sandbox.ipv4_address
}

output "ssh" {
  description = "Log in to the droplet."
  value       = "ssh root@${digitalocean_droplet.sandbox.ipv4_address}"
}

output "status_commands" {
  description = "Check that the sandbox and the publisher are working."
  value = join("\n", [
    "ssh root@${digitalocean_droplet.sandbox.ipv4_address} tail -n 40 /var/log/bw1-sandbox-setup.log",
    "ssh root@${digitalocean_droplet.sandbox.ipv4_address} sudo -u sandbox -H bw1-sandbox check",
    "ssh root@${digitalocean_droplet.sandbox.ipv4_address} sudo -u sandbox -H bw1-sandbox status",
    "ssh root@${digitalocean_droplet.sandbox.ipv4_address} systemctl list-timers bw1-publisher.timer bw1-egress.timer",
    "ssh root@${digitalocean_droplet.sandbox.ipv4_address} tail -n 50 /var/log/bw1-publisher/publisher.log",
  ])
}

output "portal_tunnel" {
  description = "Reach the portal (portal_enabled = true) from your PC; then open the URL in /var/log or journalctl -u bw1-sandbox-portal."
  value       = "ssh -N -L 8765:127.0.0.1:8765 root@${digitalocean_droplet.sandbox.ipv4_address}"
}

output "stop_command" {
  description = "Kill switch: stop the publisher, autoresume and every worker."
  value       = "ssh root@${digitalocean_droplet.sandbox.ipv4_address} 'touch /etc/bw1-sandbox/STOP && systemctl disable --now bw1-publisher.timer bw1-publisher-comments.timer bw1-sandbox-feedback.timer bw1-sandbox-autoresume.service bw1-sandbox-portal.service && docker ps -q --filter name=^w- | xargs -r docker stop'"
}

output "pull_requests" {
  description = "Where the PRs appear."
  value       = "https://github.com/${local.pr_repo}/pulls?q=is%3Apr+is%3Adraft+author%3A%40me"
}

output "do_project" {
  description = "The existing DigitalOcean project the droplet is assigned to (never created or deleted here)."
  value       = "https://cloud.digitalocean.com/projects/${var.do_project_id}"
}
