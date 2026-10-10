# --- secrets: only ever from the environment (TF_VAR_*) at apply time -------------------------
# deploy.ps1 sets them from the tokens file. They default to empty so that `terraform validate`,
# `plan -destroy` and teardown work without them; the droplet has a precondition that refuses to
# create or update anything while either is empty.

variable "claude_oauth_token" {
  description = "Claude subscription token made with `claude setup-token`. Written to the sandbox's token.env (what `sandbox.py token` writes). Set TF_VAR_claude_oauth_token; never put it in a file."
  type        = string
  default     = ""
  sensitive   = true
}

variable "github_token" {
  description = "Fork credential (tokens file key \"github\"): fine-grained PAT on fork_repo only, Contents read/write (pushes) and Pull requests read/write (PRs on the fork). Publisher only; never reaches the worker containers. Set TF_VAR_github_token; never put it in a file."
  type        = string
  default     = ""
  sensitive   = true
}

variable "openblack_pr_token" {
  description = "PR-only credential for upstream_repo (tokens file key \"github_openblack_pr\"): fine-grained PAT, resource owner openblack, only bw1-decomp, Pull requests read/write, NO Contents permission. Publisher only, used only for gh pr create/list/view against upstream_repo, never by git. Needed only when pr_target = \"openblack\". Set TF_VAR_openblack_pr_token."
  type        = string
  default     = ""
  sensitive   = true
}

# --- droplet ---------------------------------------------------------------------------------

variable "name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "bw1-sandbox"
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
  description = "Droplet size slug. The droplet stands in for the sandbox VM (upstream default 8 CPUs / 16 GB) and also runs the host side; each worker gets worker_cpus / worker_memory. s-8vcpu-16gb fits about 3 workers; c-8 (dedicated CPU) is faster; s-16vcpu-32gb fits about 6."
  type        = string
  default     = "s-8vcpu-16gb"
}

variable "droplet_image" {
  description = "Droplet image (Ubuntu LTS; Python >= 3.11 is needed by bw1-ai-sandbox)."
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "ssh_public_key_path" {
  description = "Public key installed on the droplet for root."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_private_key_path" {
  description = "Private key Terraform uses to upload files and run the setup script."
  type        = string
  default     = "~/.ssh/id_ed25519"
}

variable "ssh_allowed_cidrs" {
  description = "Addresses allowed to reach SSH (port 22), e.g. [\"203.0.113.7/32\"]. Include the machine running Terraform. Nothing else is open inbound; the portal is reached through an SSH tunnel."
  type        = list(string)
  validation {
    condition     = length(var.ssh_allowed_cidrs) > 0 && alltrue([for c in var.ssh_allowed_cidrs : can(cidrhost(c, 0))])
    error_message = "Give at least one CIDR block, e.g. 203.0.113.7/32."
  }
}

variable "tags" {
  description = "Tags added to the droplet and firewall."
  type        = list(string)
  default     = ["bw1-sandbox"]
}

# --- bw1-ai-sandbox -----------------------------------------------------------------------------

variable "sandbox_repo" {
  description = "bw1-ai-sandbox repository to run."
  type        = string
  default     = "https://github.com/bwrsandman/bw1-ai-sandbox.git"
}

variable "sandbox_ref" {
  description = "Commit of bw1-ai-sandbox to run (pinned: the droplet wrapper patches its Sandbox class, so check sandbox_droplet.py still fits before moving it)."
  type        = string
  default     = "3b6e2390ef9b898ee94ad6604efc1745b13e4179"
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.sandbox_ref))
    error_message = "A full 40-character commit SHA."
  }
}

variable "repo_source" {
  description = "\"local\" (default): the sandbox's host bw1-decomp clone is your local clone (local_repo_path), uploaded as a git bundle of all refs and checked out at your HEAD. \"upstream\": a fresh clone of upstream_repo at bw1_decomp_ref."
  type        = string
  default     = "local"
  validation {
    condition     = contains(["local", "upstream"], var.repo_source)
    error_message = "local or upstream."
  }
}

variable "local_repo_path" {
  description = "Your local bw1-decomp clone (repo_source = \"local\" and/or orig_source = \"local\"). deploy.ps1 packs it."
  type        = string
  default     = "E:/bw1-decomp/bw1-decomp"
}

variable "local_bundle_path" {
  description = "Set by deploy.ps1, not by hand: the git bundle of local_repo_path for this run (a temporary file, with repo-head.txt next to it)."
  type        = string
  default     = ""
}

variable "upstream_repo" {
  description = "bw1-decomp repository the sandbox clone and the publisher fetch from (read-only)."
  type        = string
  default     = "openblack/bw1-decomp"
}

variable "bw1_decomp_ref" {
  description = "repo_source = \"upstream\" only: branch or commit checked out in the sandbox's bw1-decomp clone (`sync` sends it to workers as their base). With repo_source = \"local\" your checked-out HEAD is used."
  type        = string
  default     = "main"
}

variable "worker_cpus" {
  description = "CPUs per worker container (sandbox.toml [worker] cpus)."
  type        = number
  default     = 2
}

variable "worker_memory" {
  description = "Memory per worker container (sandbox.toml [worker] memory), e.g. 4g."
  type        = string
  default     = "4g"
}

variable "api_nets" {
  description = "Anthropic API ranges worker containers may reach on TCP 443 (resolved api.anthropic.com addresses are added on top)."
  type        = list(string)
  default     = ["160.79.104.0/23"]
}

variable "autoresume_enabled" {
  description = "Run the sandbox's autoresume loop (resumes workers stopped by the usage limit once it resets)."
  type        = bool
  default     = true
}

variable "portal_enabled" {
  description = "Run the sandbox portal as a service, on 127.0.0.1:8765 only (reach it with an SSH tunnel)."
  type        = bool
  default     = false
}

variable "orchestrator_tools" {
  description = "Install Claude Code and the MCP package on the host so the sandbox-orchestrator skill can be run there (bw1-orchestrator)."
  type        = bool
  default     = true
}

variable "claude_code_version" {
  description = "Claude Code version for the host's orchestrator (native installer): stable, latest or x.y.z. Workers get the version their image installs."
  type        = string
  default     = "stable"
}

# --- original game files ----------------------------------------------------------------------

variable "orig_source" {
  description = "\"local\" (default): orig/ from the local clone (local_repo_path), packed by deploy.ps1 (game_version, or orig_upload_versions, plus libs/ and directx7.0/). \"archive\": upload orig_archive_path (bw1-original.zip or a pack-orig.ps1 .tar.gz). \"manual\": copy orig/ to the droplet yourself and re-run the setup."
  type        = string
  default     = "local"
  validation {
    condition     = contains(["local", "archive", "manual"], var.orig_source)
    error_message = "local, archive or manual."
  }
}

variable "orig_upload_versions" {
  description = "With orig_source = \"local\": the orig/<version> folders to upload. Empty: game_version only. libs/ and directx7.0/ always go too; other versions stay on your PC."
  type        = list(string)
  default     = []
}

variable "orig_upload_path" {
  description = "Set by deploy.ps1, not by hand: the packed orig/ archive to upload this run (a temporary file). Empty: orig_archive_path."
  type        = string
  default     = ""
}

variable "game_version" {
  description = "Game version whose original files are fetched and checked (config/<version>/build.sha1). The sandbox's worker settings target BW1W120."
  type        = string
  default     = "BW1W120"
  validation {
    condition     = contains(["BW1W100", "BW1W110", "BW1W120"], var.game_version)
    error_message = "BW1W100, BW1W110 or BW1W120 (the versions the sandbox README maps to a compiler)."
  }
}

variable "orig_archive_path" {
  description = "Local archive of the game files (orig_source = \"archive\"): a .zip or .tar.gz whose single top folder is bw1-original/ or orig/ and maps onto bw1-decomp's orig/. Uploaded to the droplet host, unpacked there, then deleted."
  type        = string
  default     = "E:/bw1-original.zip"
  validation {
    condition     = var.orig_archive_path == "" || can(regex("(?i)\\.(zip|tar\\.gz|tgz)$", var.orig_archive_path))
    error_message = "orig_archive_path must end in .zip, .tar.gz or .tgz (or be empty when orig_source is not archive)."
  }
}

# --- publisher (GitHub) -----------------------------------------------------------------------

variable "fork_repo" {
  description = "owner/name of your fork. The publisher pushes branches and opens draft PRs here only, never on openblack."
  type        = string
  default     = "raffclar/bw1-decomp"
  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.fork_repo)) && !startswith(lower(var.fork_repo), "openblack/")
    error_message = "owner/name of your own fork; openblack/* is refused."
  }
}

variable "pr_target" {
  description = "Where the publisher opens draft PRs: \"fork\" (fork_repo, head = branch) or \"openblack\" (upstream_repo, head = <fork owner>:<branch>). Branches are always pushed to fork_repo only."
  type        = string
  default     = "fork"
  validation {
    condition     = contains(["fork", "openblack"], var.pr_target)
    error_message = "fork or openblack."
  }
}

variable "publisher_enabled" {
  description = "Run the publisher timer."
  type        = bool
  default     = true
}

variable "publish_interval" {
  description = "Time between publisher runs (systemd time span)."
  type        = string
  default     = "30min"
}

variable "branch_prefix" {
  description = "Namespace for published branches on the fork; the pre-push hook only allows creating new branches under it."
  type        = string
  default     = "ai-sandbox/"
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]*/$", var.branch_prefix)) && !startswith(var.branch_prefix, "main") && !startswith(var.branch_prefix, "master")
    error_message = "A lower-case namespace ending in '/', e.g. ai-sandbox/."
  }
}

variable "max_prs_per_day" {
  description = "Draft PRs the publisher opens per UTC day, per PR target."
  type        = number
  default     = 3
}

variable "max_open_prs" {
  description = "The publisher waits while this many of its PRs are open on the current PR target."
  type        = number
  default     = 5
}

variable "allowed_paths" {
  description = "A worker branch is published only if every changed file matches one of these globs."
  type        = list(string)
  default     = ["src/*", "include/*", "tools/matching_ledger.jsonl"]
}

# --- DigitalOcean project ---------------------------------------------------------------------

variable "do_project_id" {
  description = "ID of the EXISTING DigitalOcean project \"for bw1-decomp\" that the droplet is assigned to. Terraform only assigns resources to it; it never creates, changes or deletes the project."
  type        = string
  default     = "0a017c67-5e46-4f17-9a26-d8f40de0ae7b"
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.do_project_id))
    error_message = "A DigitalOcean project ID (UUID), e.g. 0a017c67-5e46-4f17-9a26-d8f40de0ae7b."
  }
}

# --- reviewer feedback (optional) ---------------------------------------------------------------

variable "feedback_to_sandbox" {
  description = "Off by default. true: new comments on a harness PR from feedback_authors become a follow-up prompt that resumes the worker which made the PR (bw1-sandbox-feedback.timer)."
  type        = bool
  default     = false
}

variable "feedback_authors" {
  description = "GitHub logins whose PR comments may be fed back to workers. Empty: the fork's owner only. Comments from anyone else are stored but never reach a worker."
  type        = list(string)
  default     = []
}
