# --- secrets: only ever from the environment (TF_VAR_*) at apply time -------------------------
# deploy.ps1 sets them from the tokens file. They default to empty so that `terraform validate`,
# `plan -destroy` and teardown work without them, but the droplet has a precondition that
# refuses to create or update anything while either is empty.

variable "anthropic_api_key" {
  description = "Anthropic API key for Claude Code on the droplet. Set TF_VAR_anthropic_api_key; never put it in a file."
  type        = string
  default     = ""
  sensitive   = true
}

variable "github_token" {
  description = "GitHub token the harness pushes branches and opens PRs with. Set TF_VAR_github_token; never put it in a file."
  type        = string
  default     = ""
  sensitive   = true
}

# --- droplet ---------------------------------------------------------------------------------

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "bw1-harness"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.name))
    error_message = "Lower-case letters, digits and dashes, starting with a letter."
  }
}

variable "region" {
  description = "DigitalOcean region slug, e.g. lon1, ams3, fra1, nyc3, sfo3."
  type        = string
  default     = "lon1"
}

variable "droplet_size" {
  description = "Droplet size slug. The build compiles ~7000 MSVC 6 units under wibo, so CPU and RAM matter: s-8vcpu-16gb is a balanced default; c-8 (dedicated CPU) is faster, s-4vcpu-8gb is cheaper and slower."
  type        = string
  default     = "s-8vcpu-16gb"
}

variable "droplet_image" {
  description = "Droplet image (Ubuntu LTS)."
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "ssh_public_key_path" {
  description = "Public key installed on the droplet for root."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_private_key_path" {
  description = "Private key Terraform uses to upload the harness and run the setup script."
  type        = string
  default     = "~/.ssh/id_ed25519"
}

variable "ssh_allowed_cidrs" {
  description = "Addresses allowed to reach SSH (port 22), e.g. [\"203.0.113.7/32\"]. Include the machine running Terraform. Nothing else is open inbound."
  type        = list(string)
  validation {
    condition     = length(var.ssh_allowed_cidrs) > 0 && alltrue([for c in var.ssh_allowed_cidrs : can(cidrhost(c, 0))])
    error_message = "Give at least one CIDR block, e.g. 203.0.113.7/32."
  }
}

variable "tags" {
  description = "Tags added to the droplet and firewall."
  type        = list(string)
  default     = ["bw1-harness"]
}

# --- original game files ----------------------------------------------------------------------

variable "orig_source" {
  description = "Where the copyrighted original files come from: \"archive\" (upload orig_archive_path, made by scripts/pack-orig.ps1), \"ghcr\" (copy /orig out of the private CI image ghcr_image with the GitHub token) or \"manual\" (you scp them yourself)."
  type        = string
  default     = "archive"
  validation {
    condition     = contains(["archive", "ghcr", "manual"], var.orig_source)
    error_message = "archive, ghcr or manual."
  }
}

variable "orig_archive_path" {
  description = "Local .tar.gz holding orig/BW1W120, orig/libs and orig/directx7.0 (orig_source = \"archive\")."
  type        = string
  default     = ""
}

variable "ghcr_image" {
  description = "Private image whose /orig holds the original files (orig_source = \"ghcr\"). The CI uses ghcr.io/openblack/bw1-build:main."
  type        = string
  default     = "ghcr.io/openblack/bw1-build:main"
}

# --- GitHub ---------------------------------------------------------------------------------

variable "upstream_repo" {
  description = "Repository the harness clones and bases every branch on."
  type        = string
  default     = "openblack/bw1-decomp"
}

variable "fork_repo" {
  description = "owner/name of the fork the token can push to. Branches go here, never to upstream."
  type        = string
  default     = ""
}

variable "pr_repo" {
  description = "Repository the PRs are opened against. Empty: the fork itself (PRs into the fork's main, for you to review and forward). Set to openblack/bw1-decomp to open PRs upstream."
  type        = string
  default     = ""
}

variable "pr_draft" {
  description = "Open PRs as drafts."
  type        = bool
  default     = true
}

variable "branch_prefix" {
  description = "Namespace for harness branches. The pre-push hook only allows creating new branches under it."
  type        = string
  default     = "harness/"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]*/$", var.branch_prefix)) && !startswith(var.branch_prefix, "main") && !startswith(var.branch_prefix, "master")
    error_message = "A lower-case namespace ending in '/', e.g. harness/."
  }
}

variable "git_author_name" {
  description = "Author name on harness commits."
  type        = string
  default     = "bw1-decomp harness"
}

variable "git_author_email" {
  description = "Author email on harness commits, e.g. your GitHub noreply address."
  type        = string
  default     = "harness@example.invalid"
}

# --- the agent and its limits -----------------------------------------------------------------

variable "harness_enabled" {
  description = "Enable the systemd timer. false installs everything but leaves the timer off."
  type        = bool
  default     = true
}

variable "run_interval" {
  description = "Time between the end of one run and the start of the next (systemd time span)."
  type        = string
  default     = "2h"
}

variable "claude_code_version" {
  description = "Claude Code version for the native installer: stable, latest or an exact x.y.z."
  type        = string
  default     = "stable"
}

variable "claude_model" {
  description = "Model alias or ID passed to claude --model."
  type        = string
  default     = "claude-opus-5-5"
}

variable "max_turns" {
  description = "Agent turn cap per run."
  type        = number
  default     = 150
}

variable "max_usd_per_run" {
  description = "Agent spend cap per run in USD (claude --max-budget-usd)."
  type        = number
  default     = 5
}

variable "max_usd_per_day" {
  description = "No new run starts once the day's recorded agent spend reaches this (UTC day)."
  type        = number
  default     = 25
}

variable "max_runs_per_day" {
  description = "Runs per UTC day."
  type        = number
  default     = 12
}

variable "max_prs_per_day" {
  description = "PRs opened per UTC day."
  type        = number
  default     = 3
}

variable "max_open_prs" {
  description = "No new run starts while this many harness PRs are open."
  type        = number
  default     = 5
}

variable "run_timeout_min" {
  description = "Wall-clock cap for the agent in one run, in minutes."
  type        = number
  default     = 120
}

variable "max_functions" {
  description = "Functions handed to the agent per run (all from one unit)."
  type        = number
  default     = 4
}
