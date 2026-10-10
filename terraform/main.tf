locals {
  droplet_src = abspath("${path.module}/../droplet")
  build_dir   = "${path.module}/.build"

  # The guard: nothing is created or changed until both secrets are supplied.
  keys_ready = trimspace(var.claude_oauth_token) != "" && trimspace(var.github_token) != ""

  # PRs on openblack need their own PR-only credential; PRs on the fork use the fork token.
  pr_ready = var.pr_target != "openblack" || trimspace(var.openblack_pr_token) != ""
  pr_repo  = var.pr_target == "openblack" ? var.upstream_repo : var.fork_repo


  # Uploaded archive keeps its type so the droplet knows how to unpack it.
  orig_archive_remote = "/opt/bw1-sandbox/incoming/orig-archive.${can(regex("(?i)\\.zip$", var.orig_archive_path)) ? "zip" : "tar.gz"}"

  publisher_env = join("\n", [
    "UPSTREAM_REPO=${var.upstream_repo}",
    "FORK_REPO=${var.fork_repo}",
    "PR_TARGET=${var.pr_target}",
    "PR_REPO=${local.pr_repo}",
    "PR_BASE=main",
    "BRANCH_PREFIX=${var.branch_prefix}",
    "MAX_PRS_PER_DAY=${var.max_prs_per_day}",
    "MAX_OPEN_PRS=${var.max_open_prs}",
    "ALLOWED_PATHS=${join(",", var.allowed_paths)}",
    "FEEDBACK_TO_SANDBOX=${var.feedback_to_sandbox}",
    "FEEDBACK_AUTHORS=${join(",", var.feedback_authors)}",
    "",
  ])

  egress_conf = join("\n", [
    "API_NETS=\"${join(" ", var.api_nets)}\"",
    "BRIDGE=docker0",
    "",
  ])

  setup_env = join(" ", [
    "SANDBOX_REPO=${var.sandbox_repo}",
    "SANDBOX_REF=${var.sandbox_ref}",
    "UPSTREAM_REPO=${var.upstream_repo}",
    "BW1_DECOMP_REF=${var.bw1_decomp_ref}",
    "FORK_REPO=${var.fork_repo}",
    "ORIG_SOURCE=${var.orig_source}",
    "GAME_VERSION=${var.game_version}",
    "WORKER_CPUS=${var.worker_cpus}",
    "WORKER_MEMORY=${var.worker_memory}",
    "PUBLISH_INTERVAL=${var.publish_interval}",
    "FEEDBACK_TO_SANDBOX=${var.feedback_to_sandbox}",
    "PUBLISHER_ENABLED=${var.publisher_enabled}",
    "AUTORESUME_ENABLED=${var.autoresume_enabled}",
    "PORTAL_ENABLED=${var.portal_enabled}",
    "ORCHESTRATOR_TOOLS=${var.orchestrator_tools}",
    "CLAUDE_VERSION=${var.claude_code_version}",
  ])
}

# --- the droplet ---------------------------------------------------------------------------

resource "digitalocean_ssh_key" "operator" {
  name       = "${var.name}-operator"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "digitalocean_droplet" "sandbox" {
  name       = var.name
  region     = var.region
  size       = var.droplet_size
  image      = var.droplet_image
  ssh_keys   = [digitalocean_ssh_key.operator.id]
  monitoring = true
  tags       = var.tags
  user_data  = file("${path.module}/cloud-init.yaml")

  lifecycle {
    ignore_changes = [user_data] # cloud-init only runs on first boot

    precondition {
      condition     = local.keys_ready
      error_message = "Refusing to deploy: the Claude subscription token (TF_VAR_claude_oauth_token, from `claude setup-token`) and the GitHub token (TF_VAR_github_token) must both be set. deploy.ps1 does this from the tokens file."
    }
    precondition {
      condition     = local.pr_ready
      error_message = "pr_target = \"openblack\" needs the PR-only credential for openblack (TF_VAR_openblack_pr_token, tokens file key \"github_openblack_pr\")."
    }
    precondition {
      condition     = var.orig_source != "archive" || (var.orig_archive_path != "" && fileexists(var.orig_archive_path))
      error_message = "orig_source = \"archive\" needs orig_archive_path pointing at an existing .zip or .tar.gz (default E:/bw1-original.zip)."
    }
  }
}

# The droplet-level layer of egress control. Worker containers are limited further, to TCP 443 to the
# Anthropic API, by bw1-egress (DOCKER-USER rules on docker0) on the droplet itself.
resource "digitalocean_firewall" "sandbox" {
  name        = "${var.name}-ssh-only"
  droplet_ids = [digitalocean_droplet.sandbox.id]
  tags        = var.tags

  # Inbound: SSH only, from the given addresses.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.ssh_allowed_cidrs
  }

  # Outbound, host side: HTTPS (GitHub, the Anthropic API, tool and image downloads), HTTP (Ubuntu
  # package mirrors), DNS and NTP. Nothing else leaves the droplet.
  outbound_rule {
    protocol              = "tcp"
    port_range            = "443"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "80"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "53"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "53"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "123"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}

# --- provisioning over SSH -------------------------------------------------------------------

data "archive_file" "droplet" {
  type        = "zip"
  source_dir  = local.droplet_src
  output_path = "${local.build_dir}/droplet.zip"
  excludes    = ["**/__pycache__/**"]
}

resource "terraform_data" "orig_upload" {
  count = var.orig_source == "archive" ? 1 : 0

  triggers_replace = [
    digitalocean_droplet.sandbox.id,
    fileexists(var.orig_archive_path) ? filesha256(var.orig_archive_path) : "missing",
  ]

  connection {
    type        = "ssh"
    host        = digitalocean_droplet.sandbox.ipv4_address
    user        = "root"
    private_key = file(pathexpand(var.ssh_private_key_path))
    timeout     = "10m"
  }

  provisioner "remote-exec" {
    inline = ["cloud-init status --wait > /dev/null || true", "install -d -m 0755 /opt/bw1-sandbox/incoming"]
  }

  provisioner "file" {
    source      = var.orig_archive_path
    destination = local.orig_archive_remote
  }
}

resource "terraform_data" "provision" {
  # The secrets appear here only as a hash, so a new token re-runs the setup. The secrets themselves are
  # written by the file provisioners below, which Terraform does not record in its state.
  triggers_replace = [
    digitalocean_droplet.sandbox.id,
    data.archive_file.droplet.output_sha256,
    sha256(local.publisher_env),
    sha256(local.egress_conf),
    sha256(local.setup_env),
    sha256("${var.claude_oauth_token}\n${var.github_token}\n${var.openblack_pr_token}"),
    length(terraform_data.orig_upload) > 0 ? terraform_data.orig_upload[0].id : "no-archive",
  ]

  lifecycle {
    precondition {
      condition     = local.keys_ready
      error_message = "Refusing to provision: the Claude subscription token and the GitHub token must both be set."
    }
  }

  connection {
    type        = "ssh"
    host        = digitalocean_droplet.sandbox.ipv4_address
    user        = "root"
    private_key = file(pathexpand(var.ssh_private_key_path))
    timeout     = "10m"
  }

  provisioner "remote-exec" {
    inline = [
      "cloud-init status --wait > /dev/null || true",
      "install -d -m 0700 /etc/bw1-sandbox", # root-only while the secrets land; setup-droplet.sh sets owners
      "install -d -m 0755 /opt/bw1-sandbox",
    ]
  }

  provisioner "file" {
    source      = data.archive_file.droplet.output_path
    destination = "/opt/bw1-sandbox/droplet.zip"
  }

  provisioner "file" {
    content     = local.publisher_env
    destination = "/etc/bw1-sandbox/publisher.env"
  }

  provisioner "file" {
    content     = local.egress_conf
    destination = "/etc/bw1-sandbox/egress.conf"
  }

  # Same format `sandbox.py token` writes; setup-droplet.sh moves it into the sandbox user's state dir.
  provisioner "file" {
    content     = "CLAUDE_CODE_OAUTH_TOKEN=${var.claude_oauth_token}\n"
    destination = "/etc/bw1-sandbox/claude-token.env"
  }

  provisioner "file" {
    content     = "GH_TOKEN=${var.github_token}\n"
    destination = "/etc/bw1-sandbox/github.env"
  }

  # PR-only credential for openblack: its own file, read only by the publisher's gh wrapper for
  # PRs and comments on harness PRs against upstream_repo; never given to git. Empty unless pr_target =
  # "openblack" (it is still written then, so comments on earlier openblack PRs can be read if set).
  provisioner "file" {
    content     = trimspace(var.openblack_pr_token) != "" ? "GH_OPENBLACK_PR_TOKEN=${var.openblack_pr_token}\n" : ""
    destination = "/etc/bw1-sandbox/github-openblack-pr.env"
  }

  provisioner "remote-exec" {
    inline = [
      "set -e",
      "chmod 0600 /etc/bw1-sandbox/claude-token.env /etc/bw1-sandbox/github.env /etc/bw1-sandbox/github-openblack-pr.env",
      "rm -rf /opt/bw1-sandbox/droplet && mkdir -p /opt/bw1-sandbox/droplet",
      "unzip -q -o /opt/bw1-sandbox/droplet.zip -d /opt/bw1-sandbox/droplet && rm /opt/bw1-sandbox/droplet.zip",
      "find /opt/bw1-sandbox/droplet -type f -exec sed -i 's/\\r$//' {} +",
      "chmod 0755 /etc/bw1-sandbox",
      "${local.setup_env} bash /opt/bw1-sandbox/droplet/setup-droplet.sh > /var/log/bw1-sandbox-setup.log 2>&1 || (tail -n 40 /var/log/bw1-sandbox-setup.log; exit 1)",
      "tail -n 5 /var/log/bw1-sandbox-setup.log",
    ]
  }

  depends_on = [digitalocean_firewall.sandbox, terraform_data.orig_upload]
}

# --- DigitalOcean project --------------------------------------------------------------------

# Assigns the droplet to the existing project by ID. The project itself is not a resource here (no
# data source, no digitalocean_project), so `terraform destroy` only unassigns and deletes the droplet
# and can never delete the project. The SSH key and the firewall cannot be assigned to projects; they
# are named bw1-sandbox-*. Add any volume or reserved IP created later to `resources`.
resource "digitalocean_project_resources" "bw1_decomp" {
  project   = var.do_project_id
  resources = [digitalocean_droplet.sandbox.urn]

  lifecycle {
    precondition {
      condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.do_project_id))
      error_message = "do_project_id must be the UUID of the existing DigitalOcean project."
    }
  }
}
