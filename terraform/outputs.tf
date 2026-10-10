output "droplet_ip" {
  description = "The droplet's public IPv4 address."
  value       = digitalocean_droplet.harness.ipv4_address
}

output "ssh" {
  description = "Log in to the droplet."
  value       = "ssh root@${digitalocean_droplet.harness.ipv4_address}"
}

output "status_commands" {
  description = "Check the harness is running."
  value = join("\n", [
    "ssh root@${digitalocean_droplet.harness.ipv4_address} systemctl list-timers bw1-harness.timer",
    "ssh root@${digitalocean_droplet.harness.ipv4_address} systemctl status bw1-harness.service --no-pager",
    "ssh root@${digitalocean_droplet.harness.ipv4_address} tail -n 50 /var/log/bw1-harness/harness.log",
    "ssh root@${digitalocean_droplet.harness.ipv4_address} journalctl -u bw1-harness.service -n 100 --no-pager",
  ])
}

output "stop_command" {
  description = "Kill switch: no new run starts while the stop file exists (a running one stops at its next checkpoint)."
  value       = "ssh root@${digitalocean_droplet.harness.ipv4_address} 'touch /etc/bw1-harness/STOP && systemctl disable --now bw1-harness.timer'"
}

output "pull_requests" {
  description = "Where the PRs appear."
  value       = "https://github.com/${local.pr_repo}/pulls?q=is%3Apr+head%3A${trimsuffix(var.branch_prefix, "/")}"
}
