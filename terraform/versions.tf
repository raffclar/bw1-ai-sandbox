terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "2.104.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "2.8.1"
    }
  }

  # State is local (terraform.tfstate, gitignored) by default. To keep it remotely, copy
  # backend-spaces.tf.example to backend.tf and read the README's "Remote state" section.
}

# The API token comes from the DIGITALOCEAN_TOKEN environment variable, never from a file.
provider "digitalocean" {}
