locals {
  harness_src = abspath("${path.module}/../harness")
  build_dir   = "${path.module}/.build"
  pr_repo     = var.pr_repo != "" ? var.pr_repo : var.fork_repo

  # The guard: nothing is created or changed until both keys are supplied.
  # A Claude credential (subscription OAuth token, preferred, or API key) and the GitHub token.
  claude_auth_mode = trimspace(var.claude_oauth_token) != "" ? "oauth" : (trimspace(var.anthropic_api_key) != "" ? "api-key" : "none")
  keys_ready       = local.claude_auth_mode != "none" && trimspace(var.github_token) != ""

  # Only the credential in use goes to the droplet.
  secrets_env = join("", [
    local.claude_auth_mode == "oauth" ? "CLAUDE_CODE_OAUTH_TOKEN=${var.claude_oauth_token}\n" : "",
    local.claude_auth_mode == "api-key" ? "ANTHROPIC_API_KEY=${var.anthropic_api_key}\n" : "",
    "GH_TOKEN=${var.github_token}\n",
  ])

  # Non-secret settings, written to /etc/bw1-harness/config.env.
  config_env = join("\n", [
    "REPO_DIR=/srv/bw1-harness/bw1-decomp",
    "STATE_DIR=/var/lib/bw1-harness",
    "LOG_DIR=/var/log/bw1-harness",
    "STOP_FILE=/etc/bw1-harness/STOP",
    "VENV_DIR=/opt/bw1-harness/venv",
    "VERSION=BW1W120",
    "UPSTREAM_REPO=${var.upstream_repo}",
    "FORK_REPO=${var.fork_repo}",
    "PR_REPO=${local.pr_repo}",
    "PR_BASE=main",
    "PR_DRAFT=${var.pr_draft}",
    "BRANCH_PREFIX=${var.branch_prefix}",
    "GIT_AUTHOR_NAME=\"${var.git_author_name}\"",
    "GIT_AUTHOR_EMAIL=${var.git_author_email}",
    "CLAUDE_MODEL=${var.claude_model}",
    "MAX_TURNS=${var.max_turns}",
    "MAX_USD_PER_RUN=${var.max_usd_per_run}",
    "RUN_TIMEOUT_MIN=${var.run_timeout_min}",
    "CYCLES_PER_FUNCTION=12",
    "MAX_PRS_PER_DAY=${var.max_prs_per_day}",
    "MAX_OPEN_PRS=${var.max_open_prs}",
    "MAX_USD_PER_DAY=${var.max_usd_per_day}",
    "MAX_RUNS_PER_DAY=${var.max_runs_per_day}",
    "RATE_LIMIT_BACKOFF_MIN=${var.rate_limit_backoff_min}",
    "MAX_FUNCTIONS=${var.max_functions}",
    "MIN_SIZE=16",
    "MAX_SIZE=600",
    "MIN_FUZZY=50",
    "MAX_FUZZY=99.5",
    "COOLDOWN_DAYS=7",
    "MIN_FUZZY_GAIN=0.5",
    "ALLOWED_PATHS=src/*,include/*",
    "",
  ])

  setup_env = join(" ", [
    "ORIG_SOURCE=${var.orig_source}",
    "GHCR_IMAGE=${var.ghcr_image}",
    "CLAUDE_VERSION=${var.claude_code_version}",
    "HARNESS_ENABLED=${var.harness_enabled}",
    "RUN_INTERVAL=${var.run_interval}",
  ])
}

# --- the droplet ---------------------------------------------------------------------------

resource "digitalocean_ssh_key" "operator" {
  name       = "${var.name}-operator"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "digitalocean_droplet" "harness" {
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
      error_message = "Refusing to deploy: a Claude credential (TF_VAR_claude_oauth_token from `claude setup-token`, or TF_VAR_anthropic_api_key) and the GitHub token (TF_VAR_github_token) must be set. deploy.ps1 does this from the tokens file."
    }
    precondition {
      condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.fork_repo))
      error_message = "Set fork_repo (owner/name of the fork the GitHub token can push to)."
    }
    precondition {
      condition     = lower(split("/", var.fork_repo)[0]) != "openblack"
      error_message = "fork_repo must be your own fork (raffclar/bw1-decomp), never an openblack repository: branches are only pushed there."
    }
    precondition {
      condition     = var.orig_source != "archive" || (var.orig_archive_path != "" && fileexists(var.orig_archive_path))
      error_message = "orig_source = \"archive\" needs orig_archive_path pointing at an existing .tar.gz (scripts/pack-orig.ps1 makes one)."
    }
  }
}

resource "digitalocean_firewall" "harness" {
  name        = "${var.name}-ssh-only"
  droplet_ids = [digitalocean_droplet.harness.id]
  tags        = var.tags

  # Inbound: SSH only, from the given addresses.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.ssh_allowed_cidrs
  }

  # Outbound: apt, GitHub, the Anthropic API and the tool downloads the build makes.
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
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
  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}

# --- provisioning over SSH -------------------------------------------------------------------

data "archive_file" "harness" {
  type        = "zip"
  source_dir  = local.harness_src
  output_path = "${local.build_dir}/harness.zip"
  excludes    = ["**/__pycache__/**", "dry-run/**"]
}

resource "terraform_data" "orig_upload" {
  count = var.orig_source == "archive" ? 1 : 0

  triggers_replace = [
    digitalocean_droplet.harness.id,
    fileexists(var.orig_archive_path) ? filesha256(var.orig_archive_path) : "missing",
  ]

  connection {
    type        = "ssh"
    host        = digitalocean_droplet.harness.ipv4_address
    user        = "root"
    private_key = file(pathexpand(var.ssh_private_key_path))
    timeout     = "10m"
  }

  provisioner "remote-exec" {
    inline = ["cloud-init status --wait > /dev/null || true", "install -d -m 0755 /opt/bw1-harness/incoming"]
  }

  provisioner "file" {
    source      = var.orig_archive_path
    destination = "/opt/bw1-harness/incoming/orig.tar.gz"
  }
}

resource "terraform_data" "provision" {
  # The keys appear here only as a hash, so a new key re-runs the setup. The keys themselves are
  # written by the file provisioner below, which Terraform does not record in its state.
  triggers_replace = [
    digitalocean_droplet.harness.id,
    data.archive_file.harness.output_sha256,
    sha256(local.config_env),
    sha256(local.setup_env),
    sha256("${var.claude_oauth_token}\n${var.anthropic_api_key}\n${var.github_token}"),
    length(terraform_data.orig_upload) > 0 ? terraform_data.orig_upload[0].id : "no-archive",
  ]

  lifecycle {
    precondition {
      condition     = local.keys_ready
      error_message = "Refusing to provision: a Claude credential (OAuth token or API key) and the GitHub token must be set."
    }
  }

  connection {
    type        = "ssh"
    host        = digitalocean_droplet.harness.ipv4_address
    user        = "root"
    private_key = file(pathexpand(var.ssh_private_key_path))
    timeout     = "10m"
  }

  provisioner "remote-exec" {
    inline = [
      "cloud-init status --wait > /dev/null || true",
      "install -d -m 0700 /etc/bw1-harness", # root-only until setup-droplet.sh sets 0750 root:harness
      "install -d -m 0755 /opt/bw1-harness",
    ]
  }

  provisioner "file" {
    source      = data.archive_file.harness.output_path
    destination = "/opt/bw1-harness/harness.zip"
  }

  provisioner "file" {
    content     = local.config_env
    destination = "/etc/bw1-harness/config.env"
  }

  provisioner "file" {
    content     = local.secrets_env
    destination = "/etc/bw1-harness/secrets.env"
  }

  provisioner "remote-exec" {
    inline = [
      "set -e",
      "chmod 0600 /etc/bw1-harness/secrets.env",
      "rm -rf /opt/bw1-harness/harness && mkdir -p /opt/bw1-harness/harness",
      "unzip -q -o /opt/bw1-harness/harness.zip -d /opt/bw1-harness/harness && rm /opt/bw1-harness/harness.zip",
      "find /opt/bw1-harness/harness -type f -exec sed -i 's/\\r$//' {} +",
      "${local.setup_env} bash /opt/bw1-harness/harness/setup-droplet.sh > /var/log/bw1-harness-setup.log 2>&1 || (tail -n 40 /var/log/bw1-harness-setup.log; exit 1)",
      "tail -n 5 /var/log/bw1-harness-setup.log",
    ]
  }

  depends_on = [digitalocean_firewall.harness, terraform_data.orig_upload]
}
